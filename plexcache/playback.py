"""Watches Plex's active sessions and runs the two playback features.

* Now-playing preload: read ahead of the viewer so seeking hits cache.
* Next-episode prefetch: read the start of the next episode.

Replaces the Tautulli-triggered scripts; Plex itself reports what's playing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import zfs
from .layout import tail_ranges
from .pauses import busy_percent
from .plex import PlexClient, PlexError
from .reader import MiB, Pacer, Stopped, is_cache_hit, read_range

GiB = 1024 ** 3
RECENT_TTL = 6 * 3600  # don't prefetch the same file twice within 6 hours
SLICE_SEC = 10  # "first seconds" of a track read for other albums by the same artist
MAX_CHAIN_JOBS = 60  # reads queued per top-up (more follow as tracks play)
PLEX_CACHE_TTL = 600  # seconds to reuse an album's or artist's track list
DEFAULT_KBPS = 25000  # when Plex doesn't say: a typical 4K stream (as the original script assumed)
MIN_JOB = 64 * MiB  # not worth starting a read smaller than this
SKIP_AHEAD = 512 * MiB  # a jump this far past the reader starts over from the new position
RESTART_EVERY = 30  # seconds between restarts for one stream
SKIP_SECONDS = 30  # a jump in the play position bigger than this (beyond normal playback) is a skip
RETENTION_AFTER = 30 * 60  # check this long after a preload whether it's still cached
PROBES = 8  # small reads per retention check
PROBE_BYTES = 256 * 1024


def start_delay(cfg: dict, kbps: int, transcoding: bool, disk_mb_s: float) -> float:
    """Seconds to wait after Plex reports "playing" before Now-playing starts reading.

    Plex's player keeps filling its buffer after playback starts. The wait is
    the time the disks take to read that buffer — some seconds of playback at
    the file's bitrate — so our reads don't compete with it. A fixed delay in
    settings overrides this."""
    if cfg["delay_sec"]:
        return float(cfg["delay_sec"])
    buffer_sec = cfg["buffer_transcode_sec"] if transcoding else cfg["buffer_direct_sec"]
    nbytes = (kbps or DEFAULT_KBPS) * 125 * buffer_sec
    secs = nbytes / (max(1.0, disk_mb_s) * 1e6)
    return round(min(600.0, max(float(cfg["min_delay_sec"]), secs)), 1)


def plan_ranges(size: int, offset: int, want: int, budget: int, wrap: bool) -> list[tuple[int, int]]:
    """Which parts of a file to read, within ``budget`` bytes.

    From the play position to the end first (skips are usually forward and it's
    what Plex reads next), then — if ``wrap`` and there's room — from the start
    up to the play position, for skips back."""
    length = min(want, budget)
    if not size or length <= 0:
        return [(offset, length)] if length > 0 and not size else []
    offset = max(0, min(offset, size))
    forward = min(length, size - offset)
    ranges = [(offset, forward)] if forward > 0 else []
    back = min(offset, length - forward) if wrap else 0
    if back > 0:
        ranges.append((0, back))
    return ranges


@dataclass
class Job:
    feature: str  # "nowplaying" | "nextepisode"
    target: str  # human label
    path: str
    ranges: list  # [(offset, length), ...] read in order
    size: int = 0  # whole file
    state: str = "waiting"  # waiting | reading | done | failed | cancelled
    read: int = 0
    started: float = field(default_factory=time.time)
    finished: float | None = None
    message: str = ""
    delay: float = 0
    cancel: bool = False
    forward: tuple | None = None  # the main read from the play position (the index etc. may come first)
    rating_key: str = ""  # Plex item this reads (for Next-episode: the episode it gets ready)
    limited: bool = False  # only part of the file fit in the RAM limit
    disk_mb_s: float = 150  # uncached speed of its dataset, to tell cache hits from disk reads
    cached_bytes: int = 0  # of what was read, how much came from cache already ...
    disk_bytes: int = 0  # ... and how much from the disks
    slice: bool = False  # only the first seconds of a track (so it isn't "fully read" yet)

    def loaded(self, pos: int) -> bool:
        """Has the byte at ``pos`` been read (so it's in cache)?"""
        if self.state == "done":
            return self.covers(pos)
        if self.state != "reading":
            return False
        left = self.read
        for o, n in self.ranges:
            got = min(n, left)
            if o <= pos < o + got:
                return True
            left -= got
            if left <= 0:
                break
        return False

    @property
    def offset(self) -> int:
        return self.ranges[0][0] if self.ranges else 0

    @property
    def length(self) -> int:
        return sum(n for _, n in self.ranges)

    def covers(self, pos: int) -> bool:
        return any(o <= pos < o + n for o, n in self.ranges)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("feature", "target", "state", "read", "length", "size", "started",
                                           "finished", "message", "delay")}
        d["whole"] = bool(self.size) and self.length >= self.size
        return d


def resolve_start(start_at: str, l2arc: str) -> str:
    """What "default" means on this system: with an L2ARC, prefetch as soon as
    Now-playing preload is done; without one, wait until 90% so the next
    episode is read late enough to still be in RAM when it starts."""
    if start_at == "default":
        return "after_nowplaying" if l2arc == "present" else "90"
    return start_at


def next_episode_due(start_at: str, s, jobs: list, nowplaying_enabled: bool, l2arc: str = "unknown") -> bool:
    """Is it time to prefetch the next episode for session ``s``?

    * default: see resolve_start().
    * after_nowplaying: once this stream's Now-playing preload has finished, so
      the two don't compete for the disks (right away if Now-playing is off).
    * "80" / "90": that far through the episode. Plex marks an episode watched
      at its "Video played threshold", about 90% by default.
    """
    start_at = resolve_start(start_at, l2arc)
    if start_at == "after_nowplaying":
        if not nowplaying_enabled:
            return True
        now_jobs = [j for j in jobs if j.feature == "nowplaying"]
        # No job means Now-playing skipped this stream (e.g. low bitrate).
        return all(j.state in ("done", "failed", "cancelled") for j in now_jobs)
    try:
        return s.progress * 100 >= float(start_at)
    except ValueError:
        return s.progress >= 0.9


def _without(ranges: list, cuts: list) -> list:
    """``ranges`` in the same order, minus the bytes in ``cuts``."""
    out = []
    for off, n in ranges:
        pieces = [(off, off + n)]
        for c_off, c_n in cuts:
            nxt = []
            for a, b in pieces:
                if c_off + c_n <= a or c_off >= b:
                    nxt.append((a, b))
                    continue
                if a < c_off:
                    nxt.append((a, c_off))
                if c_off + c_n < b:
                    nxt.append((c_off + c_n, b))
            pieces = nxt
        out.extend((a, b - a) for a, b in pieces if b > a)
    return out


def with_index(path: str, size: int, ranges: list, budget: int) -> list:
    """Read the parts a player needs from the end of the file (index etc.) first,
    then ``ranges`` — without reading anything twice, and within ``budget``.

    First because they're tiny and the player can't seek (or, for some files,
    start) without them, so they should be cached even if playback begins
    before the rest has been read."""
    if not size:
        return ranges
    tail, _info = tail_ranges(path, size, 1024 * 1024)
    extra = sum(n for _, n in tail)
    if not tail or extra >= budget:
        return ranges
    rest = _without(ranges, tail)
    # Keep within the budget by trimming the end of the planned reads.
    over = sum(n for _, n in rest) + extra - budget
    while over > 0 and rest:
        off, n = rest[-1]
        cut = min(n, over)
        rest[-1] = (off, n - cut)
        over -= cut
        if rest[-1][1] <= 0:
            rest.pop()
    return tail + rest


def needs_restart(job: Job, pos: int) -> bool:
    """Has the viewer jumped somewhere this preload won't reach soon?

    True when ``pos`` (bytes) is outside what the job reads, or so far ahead of
    where it's reading now that starting over from there is quicker."""
    if job.state in ("failed", "waiting"):
        return False
    if not job.covers(pos):
        return True
    if job.state == "reading" and job.ranges:
        first_off, first_len = job.forward or job.ranges[0]
        # Ranges read before the main one (the index etc.) don't move the read point.
        i = job.ranges.index((first_off, first_len)) if (first_off, first_len) in job.ranges else 0
        before = sum(n for _, n in job.ranges[:i])
        reading_at = first_off + max(0, job.read - before)
        return reading_at + SKIP_AHEAD < pos < first_off + first_len
    return False


class PlaybackMonitor:
    def __init__(self, app):
        self.app = app
        self.stop = threading.Event()
        self.sessions: dict[str, dict] = {}  # session_key -> info for the UI
        self._seen: dict[str, dict] = {}  # session_key -> per-session flags
        self._recent: dict[str, float] = {}  # path -> time prefetched
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(20)
        self.last_error: str | None = None
        self.last_poll: float | None = None
        self.last_buffering: tuple[float, str] | None = None  # (when, what) a player last reported buffering
        self._prefetched: dict[str, Job] = {}  # rating key -> Next-episode job that got it ready
        # Music and audiobooks: per player (Plex session id), the reads queued for what plays next.
        # They outlive each track's session, so they're tracked here for the RAM limit.
        self._chains: dict[str, list[Job]] = {}
        self._plex_cache: dict[tuple, tuple[float, object]] = {}  # album/artist lookups, briefly
        self._retention: list[tuple[float, Job]] = []  # (when to check, job) still-cached checks
        self._disk_mark: tuple[float, dict] | None = None
        self.thread = threading.Thread(target=self._loop, name="playback", daemon=True)

    def start(self):
        self.thread.start()

    # -------------------------------------------------------------- polling
    def active_streams(self) -> int:
        with self._lock:
            return len(self.sessions)

    def preloading(self) -> str | None:
        """What Now-playing or Next-episode is reading right now, if anything."""
        with self._lock:
            for info in self.sessions.values():
                for j in info["jobs"]:
                    if j.state == "reading" and not j.cancel:
                        return ("Now-playing is loading " if j.feature == "nowplaying" else "Next-episode is loading ") + j.target
        return None

    def ram_in_use(self) -> dict:
        """Bytes each feature has read, or is about to, for streams still playing."""
        out = {"nowplaying": 0, "nextepisode": 0}
        seen = set()
        with self._lock:
            jobs = [j for info in self.sessions.values() for j in info["jobs"]] + [j for c in self._chains.values() for j in c]
            for j in jobs:
                if id(j) not in seen and j.state in ("waiting", "reading", "done") and not j.cancel:
                    seen.add(id(j))
                    out[j.feature] += j.length
        return out

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(s, jobs=[j.to_dict() for j in s["jobs"]]) for s in self.sessions.values()]

    def _loop(self):
        while not self.stop.is_set():
            settings = self.app.settings.get()
            try:
                self.poll(settings)
                self.last_error = None
            except PlexError as e:
                if self.last_error != str(e):
                    self.app.activity.event("playback", f"can't read Plex sessions: {e}", "warn")
                self.last_error = str(e)
            except Exception as e:  # never let the monitor die
                self.last_error = f"unexpected error: {e}"
                self.app.activity.event("playback", self.last_error, "warn")
            self.stop.wait(settings["general"]["poll_seconds"])

    def poll(self, settings: dict):
        plex_cfg = settings["plex"]
        if not plex_cfg["url"] or not plex_cfg["token"]:
            return
        client = PlexClient(plex_cfg["url"], plex_cfg["token"], timeout=10)
        current = client.sessions()
        self.last_poll = time.time()
        # A player that stopped altogether: drop what was queued for it.
        players = {s.session_key for s in current}
        with self._lock:
            for pid in [p for p in self._chains if p not in players]:
                for j in self._chains.pop(pid):
                    if j.state == "waiting":
                        j.state, j.cancel = "cancelled", True
        # Streams that ended (including a player that moved on to its next track) are wrapped up
        # first, so what they'd queued isn't counted twice against the RAM limits below.
        now_keys = {f"{s.session_key}:{s.rating_key}" for s in current}
        ended = []
        with self._lock:
            for key in list(self.sessions):
                if key not in now_keys:
                    for job in self.sessions[key]["jobs"]:
                        # Next-episode/next-track reads are for what plays next, so they carry on.
                        if job.state in ("waiting", "reading") and job.feature == "nowplaying":
                            job.state = "cancelled"
                    ended.append((self.sessions.pop(key), self._seen.pop(key, None) or {}))
        for info, tr in ended:
            busy = tr.get("busy") or []
            self.app.activity.record("streamend", label=info["label"], seconds=round(time.time() - info["started"]),
                                     busy_avg=round(sum(busy) / len(busy), 1) if busy else None,
                                     busy_peak=round(max(busy), 1) if busy else None)
        for s in current:
            key = f"{s.session_key}:{s.rating_key}"
            with self._lock:
                info = self.sessions.get(key)
                if info is None:
                    info = {"key": key, "label": s.label, "player": s.player, "jobs": [], "started": time.time()}
                    self.sessions[key] = info
                    self._seen[key] = {"now": False, "next": False, "restarted": 0.0, "no_room": False,
                                       "last_off": s.view_offset_ms, "last_t": time.time(), "last_state": s.state,
                                       "busy": []}
                    new = True
                else:
                    new = False
                if s.state == "buffering":
                    self.last_buffering = (time.time(), f"{s.label} on {s.player or 'a player'}")
                info.update(
                    progress=round(s.progress * 100), state=s.state, local=s.local,
                    transcoding=s.transcoding, type=s.type, kbps=s.kbps,
                )
            if new:
                where = "remote" if not s.local else "local"
                how = "transcoding" if s.transcoding else "direct play"
                self.app.activity.event("playback", f"started: {s.label} on {s.player or 'unknown player'} ({where}, {how})")
                self._stream_started(s)
            else:
                self._observe(key, s, settings)
            if self.app.library_cacheable(s.library_key):
                self._maybe_nowplaying(key, s, settings)
                self._maybe_nextepisode(key, s, settings, client)
        self._sample_disks()
        self._check_retention()
        cutoff = time.time() - RECENT_TTL
        self._recent = {p: t for p, t in self._recent.items() if t >= cutoff}

    # ---------------------------------------------------------- measuring
    def _stream_started(self, s):
        """A new stream: note it, and whether Next-episode had it ready."""
        self.app.activity.record("stream", label=s.label, player=s.player, transcoding=s.transcoding,
                                 local=s.local, media=s.type)
        job = self._prefetched.pop(s.rating_key, None)
        if job is not None:
            ready = job.state == "done"
            self.app.activity.record("next", label=s.label, ready=ready,
                                     lead=round(time.time() - job.finished) if ready and job.finished else None,
                                     pct=round(100 * job.read / job.length) if job.length else 0, state=job.state)

    def _observe(self, key, s, settings):
        """Spot skips by comparing the play position with the last poll."""
        tr = self._seen.get(key)
        if tr is None:
            return
        now = time.time()
        if tr["last_state"] in ("playing", "paused") and s.state == "playing":
            expected = tr["last_off"] + ((now - tr["last_t"]) * 1000 if tr["last_state"] == "playing" else 0)
            jump = s.view_offset_ms - expected
            if abs(jump) > SKIP_SECONDS * 1000:
                self._record_skip(key, s, settings, jump / 1000)
        tr.update(last_off=s.view_offset_ms, last_t=now, last_state=s.state)

    @staticmethod
    def _byte_pos(s):
        if not s.files or not s.files[0][1]:
            return None
        return int(s.files[0][1] * s.progress)

    def _record_skip(self, key, s, settings, seconds):
        cfg = settings["nowplaying"]
        job = self._now_job(key)
        pos = self._byte_pos(s)
        cached = bool(job and pos is not None and job.loaded(pos))
        reason = None
        if not cached:
            if not cfg["enabled"]:
                reason = "Now-playing preload is off"
            elif not self.app.library_cacheable(s.library_key):
                reason = "this library isn't being cached"
            elif job is None or job.state == "waiting":
                reason = "the preload hadn't started yet"
            elif job.state == "failed":
                reason = "the preload failed"
            elif job.covers(pos):
                reason = "the preload hadn't reached that part yet"
            elif job.limited:
                reason = "only part of the file fits in the RAM limit"
            else:
                reason = "it was outside what had been loaded"
        self.app.activity.record("skip", label=s.label, seconds=round(seconds), cached=cached, reason=reason)

    def _sample_disks(self):
        """How busy the media pools' disks were since the last poll, for each stream playing."""
        before = self._disk_mark
        now, after = time.monotonic(), zfs.disk_busy_ms()
        self._disk_mark = (now, after)
        if before is None or not self.sessions:
            return
        z = self.app.zfs or {}
        pools = {d["pool"] for d in z.get("datasets", []) if d.get("keys") and d.get("pool")}
        disks = [d for p in pools for d in (z.get("pools", {}).get(p) or {}).get("disks", [])]
        pct = busy_percent(before[1], after, (now - before[0]) * 1000, disks)
        if pct is None:
            return
        with self._lock:
            for tr in self._seen.values():
                tr["busy"].append(pct)

    def _check_retention(self):
        """Some time after each preload, check its data is still cached (tiny timed reads)."""
        now = time.time()
        due = [(t, j) for t, j in self._retention if t <= now]
        self._retention = [(t, j) for t, j in self._retention if t > now]
        for _t, job in due:
            threading.Thread(target=self._probe, args=(job,), daemon=True).start()

    def _probe(self, job: Job):
        spots = []
        total = job.length
        if not total:
            return
        for i in range(PROBES):
            target = int(total * (i + 0.5) / PROBES)
            for o, n in job.ranges:
                if target < n:
                    spots.append(o + max(0, min(target, n - PROBE_BYTES)))
                    break
                target -= n
        hits = 0
        for off in spots:
            res = read_range(job.path, off, PROBE_BYTES)
            if is_cache_hit(res, job.disk_mb_s):
                hits += 1
        if spots:
            self.app.activity.record("retention", label=job.target, feature=job.feature,
                                     pct=round(100 * hits / len(spots)),
                                     minutes=round((time.time() - (job.finished or time.time())) / 60))

    # ------------------------------------------------------------- features
    def _now_job(self, key):
        with self._lock:
            jobs = [j for j in self.sessions.get(key, {}).get("jobs", [])
                    if j.feature == "nowplaying" and not j.cancel and j.state != "cancelled"]
        return jobs[-1] if jobs else None

    def _maybe_nowplaying(self, key, s, settings):
        cfg = settings["nowplaying"]
        if not cfg["enabled"] or not s.files:
            return
        if s.state != "playing":
            return  # wait until Plex has enough buffered to play
        path, size = s.files[0]
        path = self.app.map_path(path)
        if s.type == "track" and path in self._recent and not self._seen[key]["now"]:
            self._seen[key]["now"] = True  # the whole track was just read ahead by next-track prefetch
            return
        pos = int(size * s.progress) if size else 0
        pos -= pos % MiB
        old = self._now_job(key)
        restart = False
        if self._seen[key]["now"]:
            # Already preloading: start over only if the viewer jumped somewhere we won't reach soon.
            if old is None or time.time() - self._seen[key]["restarted"] < RESTART_EVERY:
                return
            if not needs_restart(old, pos):
                return
            restart = True
        kbps = s.kbps or DEFAULT_KBPS
        want = size or int(kbps * 125 * 3600)  # unknown size: an hour of playback
        budget = self.app.ram_budget()
        mine = old.length if restart else 0  # the old read is being replaced
        room = budget["nowplaying"] - (budget["in_use"]["nowplaying"] - mine)
        base = plan_ranges(size, pos, want, room, wrap=True)
        ranges = with_index(path, size, base, room)
        if sum(n for _, n in ranges) < min(MIN_JOB, want or MIN_JOB):
            if not self._seen[key]["no_room"]:
                self._seen[key]["no_room"] = True
                self.app.activity.event("now-playing", f"no room left in its RAM limit for {s.label}", "warn")
                self.app.activity.record("limit", label=s.label, feature="nowplaying", pct=0)
            return
        self._seen[key]["now"] = True
        if restart:
            old.cancel = True
            self._seen[key]["restarted"] = time.time()
        job = Job("nowplaying", s.label, path, ranges, size, rating_key=s.rating_key,
                  disk_mb_s=self.app.disk_speed_for_library(s.library_key))
        job.forward = next((r for r in ranges if base and r[0] == base[0][0]), None)
        if size and job.length < size:
            job.limited = True
            self.app.activity.record("limit", label=s.label, feature="nowplaying", pct=round(100 * job.length / size))
        job.delay = 0 if restart else start_delay(cfg, kbps, s.transcoding,
                                                     self.app.disk_speed_for_library(s.library_key))
        if restart:
            self.app.activity.event("now-playing", f"{s.label}: viewer skipped ahead, reading from the new position")
        self._start(key, job, job.delay, cfg["pacing_percent"], cfg["max_streams"])

    def _maybe_nextepisode(self, key, s, settings, client):
        cfg = settings["nextepisode"]
        if not cfg["enabled"] or self._seen[key]["next"] or library_mode(cfg, s) == "off":
            return
        if s.type == "track":
            return self._maybe_next_tracks(key, s, settings, client)
        if s.type != "episode" or not s.grandparent_rating_key:
            return
        with self._lock:
            jobs = list(self.sessions.get(key, {}).get("jobs", []))
        if not next_episode_due(cfg["start_at"], s, jobs, settings["nowplaying"]["enabled"], self.app.l2arc_status()):
            return
        self._seen[key]["next"] = True
        try:
            nxt = client.next_episode(s.rating_key, s.grandparent_rating_key)
        except PlexError as e:
            self.app.activity.event("next-episode", f"couldn't look up the episode after {s.label}: {e}", "warn")
            return
        if not nxt or not nxt["files"]:
            self.app.activity.event("next-episode", f"no next episode after {s.label}")
            return
        files = nxt["files"]
        label = f"{s.grandparent_title} · S{nxt['season']:02d}E{nxt['episode']:02d}"
        budget = self.app.ram_budget()
        room = max(0, budget["nextepisode"] - budget["in_use"]["nextepisode"]) // max(1, len(files))
        for path, size, _kbps in files:
            path = self.app.map_path(path)
            if path in self._recent:
                continue
            # From the start: the opening is what makes it start instantly; the rest if there's room.
            ranges = with_index(path, size, plan_ranges(size, 0, size or room, room, wrap=False), room)
            if sum(n for _, n in ranges) < min(MIN_JOB, size or MIN_JOB):
                self.app.activity.event("next-episode", f"no room left in its RAM limit for {label}", "warn")
                continue
            job = Job("nextepisode", label, path, ranges, size, rating_key=nxt.get("rating_key", ""),
                      disk_mb_s=self.app.disk_speed_for_library(s.library_key))
            if size and job.length < size:
                job.limited = True
            self._prefetched[job.rating_key] = job
            self._start(key, job, 0, cfg["pacing_percent"], 20)

    def _maybe_next_tracks(self, key, s, settings, client):
        """Music and audiobooks: read what's likely to play next, per the library's choice.

        Starts once this track's own Now-playing read is done, tops up as each track
        starts, and always stays within Next-episode's RAM limit — so with a big choice
        (a whole album, everything by an artist) it may never get to the end."""
        cfg = settings["nextepisode"]
        if not s.parent_rating_key or s.state != "playing":
            return
        with self._lock:
            jobs = list(self.sessions.get(key, {}).get("jobs", []))
        if any(j.feature == "nowplaying" and j.state in ("waiting", "reading") for j in jobs):
            return
        self._seen[key]["next"] = True
        mode = library_mode(cfg, s)
        try:
            plan = self._track_plan(client, s, mode)
        except PlexError as e:
            self.app.activity.event("next-episode", f"couldn't look up the tracks after {s.label}: {e}", "warn")
            return
        pid = s.session_key
        wanted = [(self.app.map_path(path), size, t, sl) for t, sl in plan for path, size in t["files"] if size]
        want_keys = {(p, sl) for p, _, _, sl in wanted}
        with self._lock:
            old = self._chains.get(pid, [])
            # Keep what's being read, and what's done that's still ahead; re-plan everything else.
            keep = [j for j in old if j.state == "reading" or (j.state == "done" and (j.path, j.slice) in want_keys)]
            for j in old:
                if j.state == "waiting":
                    j.state, j.cancel = "cancelled", True
            self._chains[pid] = keep
        budget = self.app.ram_budget()
        room = max(0, budget["nextepisode"] - budget["in_use"]["nextepisode"])
        have = {(j.path, j.slice) for j in keep}
        new_jobs = []
        for path, size, t, sl in wanted:
            if len(new_jobs) >= MAX_CHAIN_JOBS:
                break
            if (path, sl) in have or (path, False) in have or (not sl and path in self._recent):
                continue
            n = slice_bytes(size, t["duration_ms"]) if sl else size
            if n > room:
                break  # in order: nothing after this gets ahead of it
            room -= n
            have.add((path, sl))
            label = f"{s.grandparent_title} · {t['title']}" if s.grandparent_title else t["title"]
            new_jobs.append(Job("nextepisode", label + (" (first seconds)" if sl else ""), path, [(0, n)], size,
                                rating_key=t["rating_key"], slice=sl,
                                disk_mb_s=self.app.disk_speed_for_library(s.library_key)))
        if not new_jobs:
            return
        with self._lock:
            self._chains[pid] = keep + new_jobs
            if key in self.sessions:
                self.sessions[key]["jobs"].extend(new_jobs)
        whole = sum(1 for j in new_jobs if not j.slice)
        self.app.activity.event("next-episode", f"after {s.label}: reading {whole} track{'s' if whole != 1 else ''}"
                                + (f" and the start of {len(new_jobs) - whole} more" if len(new_jobs) > whole else ""))

        def run():
            for job in new_jobs:
                if self.stop.is_set():
                    return
                if job.state == "waiting":
                    self._run_job(key, job, 0, cfg["pacing_percent"], 1)
        threading.Thread(target=run, name="next-tracks", daemon=True).start()

    def _cached(self, kind: str, rk: str, fetch):
        hit = self._plex_cache.get((kind, rk))
        if hit and time.time() - hit[0] < PLEX_CACHE_TTL:
            return hit[1]
        val = fetch(rk)
        self._plex_cache[(kind, rk)] = (time.time(), val)
        return val

    def _track_plan(self, client, s, mode: str) -> list[tuple[dict, bool]]:
        """What to read, in order: [(track, first_seconds_only), ...]."""
        tracks = self._cached("album", s.parent_rating_key, client.album_tracks)
        i = next((n for n, t in enumerate(tracks) if t["rating_key"] == s.rating_key), None)
        if i is None:
            return []
        after, before = tracks[i + 1:], tracks[:i]
        if mode.isdigit():
            out, covered = [], 0
            for t in after:
                if covered >= int(mode) * 60_000:
                    break
                out.append((t, False))
                covered += t["duration_ms"] or 0
            return out
        plan = [(t, False) for t in after]
        if mode in ("album", "artist"):
            plan += [(t, False) for t in before]  # the earlier tracks too, for going back or shuffle
        if mode == "artist" and s.grandparent_rating_key:
            others = [a for a in self._cached("artist", s.grandparent_rating_key, client.artist_albums) if a != s.parent_rating_key]
            albums = [self._cached("album", a, client.album_tracks) for a in others]
            # The first seconds of every other album first, so any of them starts instantly,
            # then those albums in full, one after another.
            plan += [(a[0], True) for a in albums if a]
            plan += [(t, False) for a in albums for t in a]
        return plan

    # ---------------------------------------------------------------- jobs
    def _start(self, key, job: Job, delay: float, pacing: float, max_parallel: int):
        with self._lock:
            if key in self.sessions:
                self.sessions[key]["jobs"].append(job)
        t = threading.Thread(target=self._run_job, args=(key, job, delay, pacing, max_parallel), daemon=True)
        t.start()

    def _record_read(self, job: Job):
        if job.read:
            self.app.activity.record("read", feature=job.feature, label=job.target,
                                     cached=job.cached_bytes, disk=job.disk_bytes)

    def _session_alive(self, key) -> bool:
        with self._lock:
            return key in self.sessions

    def _run_job(self, key, job: Job, delay: float, pacing: float, max_parallel: int):
        if delay and self.stop.wait(delay):
            return
        if not self._session_alive(key) and job.feature == "nowplaying":
            job.state = "cancelled"
            return
        # Limit concurrent jobs per feature.
        sem = self.app.job_slots(job.feature, max_parallel)
        with sem:
            if job.state == "cancelled":
                return
            job.state = "reading"
            stop = threading.Event()

            def pause():
                if job.cancel or (not self._session_alive(key) and job.feature == "nowplaying"):
                    stop.set()
                return False

            def progress(n):
                job.read += n

            def timed(nbytes, seconds):
                fast = seconds <= 0 or nbytes / seconds / 1e6 >= 4 * job.disk_mb_s
                if fast:
                    job.cached_bytes += nbytes
                else:
                    job.disk_bytes += nbytes

            pacer = Pacer(pacing, stop, pause)
            started = time.monotonic()
            res = None
            try:
                for off, n in job.ranges:
                    r = read_range(job.path, off, n, pacer, progress=progress, on_group=timed)
                    if res is None:
                        res = r
                    else:
                        res.bytes += r.bytes
                        res.error = res.error or r.error
                    if r.error:
                        break
            except Stopped:
                job.state = "cancelled"
                job.finished = time.time()
                self._record_read(job)
                if not job.cancel:
                    self.app.activity.event(job.feature, f"stopped (playback ended): {job.target}")
                return
            took = time.monotonic() - started
            job.finished = time.time()
            if res.error:
                job.state = "failed"
                job.message = res.error
                self.app.activity.event(job.feature, f"couldn't read {job.path}: {res.error}", "warn")
                return
            job.state = "done"
            if not job.slice:
                self._recent[job.path] = time.time()
            self._record_read(job)
            self._retention.append((time.time() + RETENTION_AFTER, job))
            gb = res.bytes / 1e9
            self.app.activity.event(job.feature, f"{job.target}: {gb:.1f} GB cached in {took:.0f} s")
            self.app.activity.count(job.feature)


def library_mode(cfg: dict, s) -> str:
    """This library's Next-episode choice (its type's default unless set)."""
    from .config import DEFAULT_MODE
    mode = (cfg.get("library_modes") or {}).get(s.library_key)
    return mode or DEFAULT_MODE["tv" if s.type == "episode" else "audio"]


def slice_bytes(size: int, duration_ms: int) -> int:
    """About SLICE_SEC seconds of a track, read in whole MiB."""
    if not size:
        return 0
    if not duration_ms:
        return min(size, MiB)
    n = int(size * SLICE_SEC * 1000 / duration_ms)
    return min(size, max(MiB, -(-n // MiB) * MiB))

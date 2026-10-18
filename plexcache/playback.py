"""Watches Plex's active sessions and runs the two playback features.

* Now Playing Cache: read the file from the play position (then from the start,
  if it fits) so seeking hits cache.
* Prefetch: read what's likely to play next — lead-ins after Now Playing Cache is done,
  whole files near the end — per library (see prefetch.py). Steps that need an L2ARC are
  background work: paced and paused like warm-up (pauses.py), in one queue shared by every
  player. The newest request goes to the front of it, and everything queued is read in the
  end, even after its player stops.

Everything read is noted in the cache log (cachelog.py), and nothing is read again within
its cooldown.

Replaces the Tautulli-triggered scripts; Plex itself reports what's playing.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from dataclasses import dataclass, field

from . import cachemode, prefetch, zfs
from .layout import min_head, tail_ranges
from .i18n import Fixed, M, duration_text, join, msg, size_text
from .pauses import L2arcPauses, busy_percent
from .plex import PlexClient, PlexError
from .reader import MiB, Pacer, Stopped, is_probe_hit, read_range
from .warmup import align_ranges, library_seconds, preload_bytes

GiB = 1024 ** 3
MAX_START_JOBS = 5000  # starts queued per top-up (the next episode or track queues more)
SHOWN_JOBS = 20  # of a top-up's reads, how many the page lists under the stream
PLEX_CACHE_TTL = 600  # seconds to reuse an album's or artist's track list
DEFAULT_KBPS = 25000  # when Plex doesn't say: a typical 4K stream (as the original script assumed)
MIN_JOB = 64 * MiB  # not worth starting a read smaller than this
SKIP_AHEAD = 512 * MiB  # a jump this far past the reader starts over from the new position
RESTART_EVERY = 30  # seconds between restarts for one stream
SKIP_SECONDS = 30  # a jump in the play position bigger than this (beyond normal playback) is a skip
RETENTION_AFTER = 30 * 60  # check this long after a preload whether it's still cached
PROBES = 8  # small reads per retention check
PROBE_BYTES = MiB  # each timed like the next-day check: a hit is back in a few ms (reader.is_probe_hit)
EXPECT_FOR = 24 * 3600  # how long a predicted next episode is watched for


def start_delay(cfg: dict, kbps: int, transcoding: bool, disk_mb_s: float) -> float:
    """Seconds to wait after Plex reports "playing" before Now Playing Cache starts reading.

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
    rating_key: str = ""  # Plex item this reads (for Prefetch: the episode it gets ready)
    limited: bool = False  # only part of the file fit in the RAM limit
    disk_mb_s: float = 150  # uncached speed of its dataset, to tell cache hits from disk reads
    cached_bytes: int = 0  # of what was read, how much came from cache already ...
    disk_bytes: int = 0  # ... and how much from the disks
    slice: bool = False  # only the start of the file (so it isn't "fully read" yet)
    ram: bool = True  # counts against its feature's RAM limit (starts headed for the SSD cache don't)
    background: bool = False  # an L2ARC-only step of Prefetch: L2ARC pacing and pauses
    player: str = ""  # the Plex session it was queued for
    library_key: str = ""  # the Plex library the file is in (for the cache log's per-library counts)
    paused: object = None  # why it's waiting, while it is
    stop_event: object = None  # set to stop it at once (while it reads, or waits between reads)

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
                                           "finished", "message", "delay", "background", "paused")}
        d["whole"] = bool(self.size) and self.length >= self.size
        return d


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


def start_bytes(path: str, size: int, kbps: int, seconds: float, record: int | None) -> int:
    """How much of a file's start to cache: its lead-in, never less than the metadata in front of
    the video and 60% of a record of video (layout.min_head)."""
    _ranges, info = tail_ranges(path, size, 0) if size else ([], {})
    return max(preload_bytes(kbps, seconds, size, record), min_head(info.get("video_start"), record, size))


def with_index(path: str, size: int, ranges: list, budget: int, record: int | None = None) -> list:
    """Read the file's metadata at the end (the index, tags, fonts…) first,
    then ``ranges`` — without reading anything twice, and within ``budget``.
    ``record``: the dataset's record size, so the end is read in whole records, as ZFS does.

    First because they're small and the player can't seek (or, for some files,
    start) without them, so they should be cached even if playback begins
    before the rest has been read."""
    if not size:
        return ranges
    tail, info = tail_ranges(path, size, 0)
    start = info.get("video_start")
    tail = [(o, n) for o, n in tail if o >= (start if start is not None else 1024 * 1024)]  # the end's, not the front's
    tail = align_ranges(tail, record, size)
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
        self.last_sessions: dict | None = None  # what Plex reported at the last poll (for the exported log)
        self._seen: dict[str, dict] = {}  # session_key -> per-session flags
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(20)
        self.last_error: str | None = None
        self.last_poll: float | None = None
        self.last_buffering: tuple[float, str] | None = None  # (when, what) a player last reported buffering
        self._prefetched: dict[str, Job] = {}  # rating key -> Prefetch job that got it ready
        # Rating keys Prefetch picked as what plays next (even when the cooldown meant there was
        # nothing to read), so "Next episodes ready" counts every one: rating key -> when picked
        self._expected: dict[str, float] = {}
        # Music and audiobooks: per player (Plex session id), the reads queued for what plays next.
        # They outlive each track's session, so they're tracked here for the RAM limit.
        self._chains: dict[str, list[Job]] = {}
        self._runners: set = set()  # players whose queue is being read
        # Prefetch's L2ARC-only reads, for every player: the newest request first. Read one at a
        # time, paced and paused like warm-up, until everything in it is done.
        self._bg_queue: list[Job] = []
        self._bg_running = False
        self._bg_pauses: L2arcPauses | None = None  # shared by Prefetch's L2ARC-only reads
        self.background_paused = None  # why they're waiting right now, if they are
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

    def preloading(self, foreground_only: bool = False) -> str | None:
        """What Now Playing Cache or Prefetch is reading right now, if anything. With
        ``foreground_only``, Prefetch's L2ARC-only reads don't count (they wait like warm-up)."""
        with self._lock:
            for j in self._all_jobs():
                if j.state == "reading" and not j.cancel and not j.paused and not (foreground_only and j.background):
                    return (M("Now Playing Cache is loading {title}", title=j.target) if j.feature == "nowplaying"
                            else M("Prefetch is loading {title}", title=j.target))
        return None

    def _all_jobs(self) -> list[Job]:
        """Every job the monitor knows about (call with the lock held)."""
        return ([j for info in self.sessions.values() for j in info["jobs"]] + [j for c in self._chains.values() for j in c]
                + self._bg_queue)

    def background_queue(self) -> dict:
        """Prefetch's L2ARC-only queue, for the page: how much is waiting and what's being read."""
        with self._lock:
            waiting = sum(1 for j in self._bg_queue if j.state == "waiting")
            now = next((j for j in self._bg_queue if j.state == "reading"), None)
        return {"waiting": waiting, "reading": now.target if now else None, "paused": self.background_paused}

    def _bg_pauses_for(self) -> L2arcPauses:
        if self._bg_pauses is None:
            self._bg_pauses = L2arcPauses(self.app, manual=False)
        return self._bg_pauses

    def _background_pause(self):
        """Why Prefetch's L2ARC-only reads should wait right now (None: carry on). Logged when it changes."""
        why = self._bg_pauses_for().reason()
        if why != self.background_paused:
            self.background_paused = why
            self.app.activity.event("next-episode", M("L2ARC-only reads paused: {why}", why=why) if why
                                    else M("L2ARC-only reads carrying on"))
        return why

    def ram_in_use(self) -> dict:
        """Bytes each feature has read, or is about to, for streams still playing."""
        out = {"nowplaying": 0, "nextepisode": 0}
        seen = set()
        with self._lock:
            for j in self._all_jobs():
                if id(j) not in seen and j.state in ("waiting", "reading", "done") and not j.cancel and j.ram:
                    seen.add(id(j))
                    out[j.feature] += j.length
        return out

    def playing_paths(self) -> set[str]:
        """Files being played now (as this app sees them)."""
        with self._lock:
            return {p for s in self.sessions.values() for p in s.get("paths", [])}

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
                    self.app.activity.event("playback", M("can't read Plex sessions: {why}", why=msg(e)), "warn")
                self.last_error = msg(e)
            except Exception as e:  # never let the monitor die
                self.last_error = M("unexpected error: {why}", why=str(e))
                self.app.activity.event("playback", self.last_error, "warn")
            self.stop.wait(settings["general"]["poll_seconds"])

    def poll(self, settings: dict):
        plex_cfg = settings["plex"]
        if not plex_cfg["url"] or not plex_cfg["token"]:
            return
        client = PlexClient(plex_cfg["url"], plex_cfg["token"], timeout=10)
        current = client.sessions()
        self.last_poll = time.time()
        # What Plex reported, as read (for the exported log, to look into streams that aren't cached)
        self.last_sessions = {"at": self.last_poll, "sessions": [dataclasses.asdict(x) for x in current]}
        # A player that stopped altogether: drop what was queued for it.
        players = {s.session_key for s in current}
        with self._lock:
            for pid in [p for p in self._chains if p not in players]:
                for j in self._chains.pop(pid):
                    if j.state == "waiting":
                        j.state, j.cancel = "cancelled", True
            # L2ARC-only reads carry on after their player stops; finished ones are let go then
            self._bg_queue = [j for j in self._bg_queue if j.state in ("waiting", "reading") or j.player in players]
        # Streams that ended (including a player that moved on to its next track) are wrapped up
        # first, so what they'd queued isn't counted twice against the RAM limits below.
        now_keys = {f"{s.session_key}:{s.rating_key}" for s in current}
        ended = []
        with self._lock:
            for key in list(self.sessions):
                if key not in now_keys:
                    for job in self.sessions[key]["jobs"]:
                        # Prefetch/next-track reads are for what plays next, so they carry on.
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
                    info = {"key": key, "label": s.label, "player": s.player, "poster": s.poster, "jobs": [], "started": time.time(),
                            "paths": [self.app.map_path(p) for p, _ in s.files]}  # the optimizer leaves these alone
                    self.sessions[key] = info
                    self._seen[key] = {"now": False, "start": False, "end": False, "restarted": 0.0, "no_room": False,
                                       "last_off": s.view_offset_ms, "last_t": time.time(), "last_state": s.state,
                                       "busy": []}
                    new = True
                else:
                    new = False
                if s.state == "buffering":
                    self.last_buffering = (time.time(), M("{title} on {player}", title=s.label, player=s.player)
                                           if s.player else M("{title} on a player", title=s.label))
                info.update(
                    progress=round(s.progress * 100), state=s.state, local=s.local,
                    transcoding=s.transcoding, type=s.type, kbps=s.kbps,
                )
            if new:
                where = M("remote") if not s.local else M("local")
                how = M("transcoding") if s.transcoding else M("direct play")
                self.app.activity.event("playback", M("started: {title} on {player} ({where}, {how})", title=s.label,
                                                      player=s.player or M("unknown player"), where=where, how=how))
                self._stream_started(s)
            else:
                self._observe(key, s, settings)
            if self.app.library_cacheable(s.library_key):
                self._maybe_nowplaying(key, s, settings)
                self._maybe_prefetch(key, s, settings, client)
            else:
                lib = self.app.library_info(s.library_key) or {}
                self._why(key, s, M("this library isn't being cached ({library})", library=lib.get("title") or s.library_key or M("no library given")))
        self._sample_disks()
        self._check_retention()

    # ---------------------------------------------------------- measuring
    def _stream_started(self, s):
        """A new stream: note it, and whether Prefetch had it ready."""
        # How likely it started from cache: its lead-in (or all of it) cached, and still there
        first = self.app.map_path(s.files[0][0]) if s.files else ""
        lead_p = round(max(self.app.cached_chance(first, True), self.app.cached_chance(first, False)), 3) if first else None
        self.app.activity.record("stream", label=s.label, player=s.player, transcoding=s.transcoding,
                                 local=s.local, media=s.type, lead_p=lead_p)
        job = self._prefetched.pop(s.rating_key, None)
        picked = self._expected.pop(s.rating_key, None)
        if picked is None and job is None:
            return
        # From the cache log, so it counts however it got cached (this time, or within the cooldown):
        # ready = it starts instantly (its lead-in, or all of it, was cached); full = all of it was
        path = self.app.map_path(s.files[0][0]) if s.files else (job.path if job else "")
        # How likely it was still cached when it started: from how long ago it was cached and how long
        # cached data lasts in its library — not the cooldown (with none, nothing would count as ready)
        p_full = self.app.cached_chance(path, True) if path else 0.0
        p = round(max(p_full, self.app.cached_chance(path, False)), 3) if path else 0.0
        full, ready = p_full >= 0.5, p >= 0.5
        when = self.app.cachelog.last(path, whole=full) if ready and path else 0
        pct = 100 if full else (round(100 * job.read / job.length) if job and not job.slice and job.length else 0)
        self.app.activity.record("next", label=s.label, ready=ready, full=full, p=p,
                                 lead=round(time.time() - when) if when else None, pct=pct,
                                 state=job.state if job else "logged")

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
        # p: how likely it really came from cache, from how long ago that part was cached and how
        # long cached data lasts in this library (just read by Now Playing Cache: nearly certain)
        p = 0.0
        if cached:
            p = self.app.cachelog.still_cached(self.app.cache_route(job.ram), s.library_key, time.time() - job.started)
            p = 1.0 if p is None else p
        if not cached and s.files:
            # Cached some other way: the whole file (Whole File Cache, or Now Playing Cache earlier),
            # or — for a jump back near the start — its lead-in
            # (from the cache log and how long data lasts — not the cooldown, which may be none)
            path, size = self.app.map_path(s.files[0][0]), s.files[0][1]
            p_whole = self.app.cached_chance(path, True)
            if p_whole:
                p = p_whole
                cached = p >= 0.5
            elif pos is not None and self.app.cachelog.last(path, whole=False):
                lib = self.app.library_info(s.library_key)
                kind = prefetch.warm_kind(prefetch.library_kind(lib) or {"episode": "tv", "movie": "movies"}.get(s.type, "music"))
                lead = preload_bytes(s.kbps or DEFAULT_KBPS, library_seconds(settings["warmup"], s.library_key, kind), size,
                                     self.app.record_for(s.library_key))
                p = self.app.cached_chance(path, False) if pos < lead else 0.0
                cached = p >= 0.5
        reason = None
        if not cached:
            if not cfg["enabled"]:
                reason = "Now-playing preload is off"  # a stored code (history keeps it), not shown as-is
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
        self.app.activity.record("skip", label=s.label, seconds=round(seconds), cached=cached, reason=reason, p=round(p, 3),
                                 lib=str(s.library_key or ""))

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
            if is_probe_hit(res):
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

    def _why(self, key, s, why):
        """Say once per stream, in Recent events, why it isn't being cached."""
        seen = self._seen.get(key)
        if seen is None or seen.get("why") == str(why):
            return
        seen["why"] = str(why)
        self.app.activity.event("now-playing", M("{title} on {player}: not cached — {why}", title=s.label,
                                                 player=s.player or M("unknown player"), why=why), "warn")

    def _maybe_nowplaying(self, key, s, settings):
        cfg = settings["nowplaying"]
        if not cfg["enabled"]:
            return
        if not s.files:
            self._why(key, s, M("Plex didn't say which file is playing"))
            return
        if s.state != "playing":
            if s.state not in ("paused", "buffering"):
                self._why(key, s, M("Plex reports it as “{state}”, not playing", state=s.state or "?"))
            return  # wait until Plex has enough buffered to play
        path, size = s.files[0]
        path = self.app.map_path(path)
        if "missing" not in self._seen[key]:  # (looked at once per stream)
            self._seen[key]["missing"] = not os.path.exists(path)
        if self._seen[key]["missing"]:
            self._why(key, s, M("its file isn't found here: {path} (see System → Media mappings)", path=path))
            return
        if not self._seen[key]["now"] and self.app.recently_cached(path, whole=True):
            self._seen[key]["now"] = True  # the whole file was cached within its cooldown (Prefetch, usually)
            ago = time.time() - self.app.cachelog.last(path, whole=True)
            self.app.activity.event("now-playing", M("{title}: whole file cached {age} ago, within its cooldown — not read again",
                                                     title=s.label, age=duration_text(ago)))
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
        ranges = with_index(path, size, base, room, self.app.record_for(s.library_key))
        if sum(n for _, n in ranges) < min(MIN_JOB, want or MIN_JOB):
            if not self._seen[key]["no_room"]:
                self._seen[key]["no_room"] = True
                self.app.activity.event("now-playing", M("no room left in its RAM limit for {title}", title=s.label), "warn")
                self.app.activity.record("limit", label=s.label, feature="nowplaying", pct=0)
            return
        self._seen[key]["now"] = True
        if restart:
            old.cancel = True
            self._seen[key]["restarted"] = time.time()
        job = Job("nowplaying", s.label, path, ranges, size, rating_key=s.rating_key,
                  disk_mb_s=self.app.disk_speed_for_library(s.library_key), library_key=s.library_key)
        job.forward = next((r for r in ranges if base and r[0] == base[0][0]), None)
        if size and job.length < size:
            job.limited = True
            self.app.activity.record("limit", label=s.label, feature="nowplaying", pct=round(100 * job.length / size))
        job.delay = 0 if restart else start_delay(cfg, kbps, s.transcoding,
                                                     self.app.disk_speed_for_library(s.library_key))
        if restart:
            self.app.activity.event("now-playing", M("{title}: viewer skipped ahead, reading from the new position", title=s.label))
        self._start(key, job, job.delay, cfg["pacing_percent"], cfg["max_streams"])

    def _maybe_prefetch(self, key, s, settings, client):
        """Prefetch (see prefetch.py): the start of what's likely to play next once this stream's
        Now Playing Cache is done, and whole files once it's 80% or 90% through. Both follow
        the library's ladder, so the next episode, track or chapter always comes first."""
        cfg = settings["nextepisode"]
        if s.type not in ("episode", "track", "movie") or s.state != "playing":
            return
        lib = self.app.library_info(s.library_key)
        kind = prefetch.library_kind(lib) or {"episode": "tv", "movie": "movies"}.get(s.type, "music")
        name = (lib or {}).get("title", "")
        mine = prefetch.library_settings(cfg, s.library_key)
        seen = self._seen[key]
        due_start = due_end = False
        if not seen["start"]:
            with self._lock:
                jobs = list(self.sessions.get(key, {}).get("jobs", []))
            now_busy = settings["nowplaying"]["enabled"] and any(
                j.feature == "nowplaying" and j.state in ("waiting", "reading") for j in jobs)
            due_start = not now_busy
        if not seen["end"] and mine["end"] != "none":
            due_end = s.progress * 100 >= float(mine["end_at"])
        if not (due_start or due_end):
            return
        # Looked up each time, so adding or losing an L2ARC (or a fallback) takes effect at once
        start = prefetch.resolve(self.app, s.library_key, "start", mine["start"], kind, name) if due_start else "off"
        end = prefetch.resolve(self.app, s.library_key, "end", mine["end"], kind, name) if due_end else None
        seen["start"] = seen["start"] or due_start
        seen["end"] = seen["end"] or due_end
        if start == "off" and end in (None, "none"):
            return  # nothing due now (e.g. lead-ins wait for Now Playing Cache, and whole files resolve to "Nothing")
        try:
            steps = self._steps(client, s, kind)
        except PlexError as e:
            self.app.activity.event("next-episode", M("couldn't look up what plays after {title}: {why}", title=s.label, why=msg(e)), "warn")
            return
        if steps is None:
            return
        whole_items = [(prefetch.background("end", st), it) for st, it in prefetch.plan_steps(steps, end)] if end else []
        lib_items = self._library_items(s.library_key) if start == "library" else None
        start_items = [(prefetch.background("start", st), it) for st, it in prefetch.plan_steps(steps, start, lib_items)]
        if not whole_items and not start_items:
            self.app.activity.event("next-episode", M("nothing plays after {title}", title=s.label))
            return
        # What plays next, for "Next episodes ready" (counted even if it's all within its cooldown)
        nxt = (whole_items or start_items)[0][1].get("rating_key")
        if nxt:
            now = time.time()
            self._expected = {k: t for k, t in self._expected.items() if now - t < EXPECT_FOR}
            self._expected[nxt] = now
        self._queue(key, s, settings, kind, whole_items, start_items, start, end)

    def _steps(self, client, s, kind):
        if s.type == "movie":
            cols = self._cached("collections", f"{s.library_key}/{s.rating_key}",
                                lambda k: client.movie_collections(s.rating_key, s.library_key))
            return prefetch.movie_steps(cols, s.rating_key)
        if s.type == "episode":
            if not s.grandparent_rating_key:
                return None
            eps = self._cached("show", s.grandparent_rating_key, client.show_episodes)
            return prefetch.tv_steps(eps, s.rating_key)
        if not s.parent_rating_key:
            return None
        album = self._cached("album", s.parent_rating_key, client.album_tracks)
        albums = []
        if s.grandparent_rating_key:
            albums = [self._cached("album", a, client.album_tracks)
                      for a in self._cached("artist", s.grandparent_rating_key, client.artist_albums)]
        here = next((n for n, a in enumerate(albums) if a and any(t["rating_key"] == s.rating_key for t in a)), None)
        next_album = albums[here + 1] if kind == "books" and here is not None and here + 1 < len(albums) else None
        return prefetch.audio_steps(album, s.rating_key, next_album, albums, kind)

    def _library_items(self, library_key: str) -> list[dict]:
        """Every file in a library (from the last scan), for the "whole library" step."""
        return [{"rating_key": "", "title": f.path.rsplit("/", 1)[-1], "files": [(f.path, f.size, f.kbps)]}
                for f in self.app.files if f.library_key == library_key]

    def _item_files(self, it):
        """(path, size, kbps) for each file of an item; tracks get their bitrate from size and duration."""
        out = []
        for f in it.get("files") or []:
            path, size = f[0], f[1]
            kbps = f[2] if len(f) > 2 else 0
            if not kbps and size and it.get("duration_ms"):
                kbps = int(size * 8 / it["duration_ms"])
            out.append((self.app.map_path(path), size, kbps))
        return out

    def _queue(self, key, s, settings, kind, whole_items, start_items, start="off", end=None):
        """Turn the plan ((background, item) pairs) into reads for this player: whole files first
        (within the RAM limit), then lead-ins. Anything in the cache log within its cooldown, or
        already queued, is skipped. L2ARC-only steps go to the front of the shared background queue."""
        cfg = settings["nextepisode"]
        pid = s.session_key
        seconds = library_seconds(settings["warmup"], s.library_key, prefetch.warm_kind(kind))
        record = self.app.record_for(s.library_key)  # reads come in whole records of the dataset
        # Starts land in the SSD cache with an L2ARC, so only in RAM-only mode do they count against the RAM limit
        starts_use_ram = self.app.cache_mode() != cachemode.L2ARC
        budget = self.app.ram_budget()
        room = max(0, budget["nextepisode"] - budget["in_use"]["nextepisode"])
        with self._lock:
            chain = self._chains.setdefault(pid, [])
            have = {(j.path, j.slice) for j in chain if not j.cancel and j.state != "failed"}
            # (L2ARC-only reads already waiting are queued again, so they move up with this request)
            have |= {(j.path, j.slice) for j in self._bg_queue if j.state == "reading"}
        disk = self.app.disk_speed_for_library(s.library_key)
        playing = {self.app.map_path(p) for p, _ in s.files}
        have |= {(p, False) for p in playing} | {(p, True) for p in playing}  # (Now Playing Cache reads that one)
        new_whole, new_start, full, cooled = [], [], False, 0
        for bg, it in whole_items:
            for path, size, _kbps in self._item_files(it):
                if not size or (path, False) in have:
                    continue
                if self.app.recently_cached(path, whole=True):
                    cooled += 1
                    continue
                # L2ARC-only whole files only pass through RAM on their way to the SSD, at the L2ARC
                # pacing, so they don't count against the RAM limit; the rest must fit it, in order
                if not bg:
                    if size > room:
                        full = True
                        break  # in order: nothing after this gets ahead of it
                    room -= size
                have.add((path, False))
                new_whole.append(Job("nextepisode", self._label(s, it), path, [(0, size)], size,
                                     rating_key=it.get("rating_key", ""), disk_mb_s=disk, background=bg, player=pid,
                                     ram=not bg, library_key=s.library_key))
            if full:
                break
        for bg, it in start_items:
            if len(new_start) >= MAX_START_JOBS:
                break
            for path, size, kbps in self._item_files(it):
                if not size or (path, False) in have or (path, True) in have:
                    continue
                if self.app.recently_cached(path, whole=False):
                    cooled += 1
                    continue
                n = start_bytes(path, size, kbps or DEFAULT_KBPS, seconds, record)
                ranges = with_index(path, size, [(0, n)], n + 8 * MiB, record)
                length = sum(x for _, x in ranges)
                if starts_use_ram:
                    if length > room:
                        full = True
                        break
                    room -= length
                have.add((path, True))
                new_start.append(Job("nextepisode", self._label(s, it), path, ranges, size,
                                     rating_key=it.get("rating_key", ""), slice=True, ram=starts_use_ram, disk_mb_s=disk,
                                     background=bg, player=pid, library_key=s.library_key))
            if full and starts_use_ram:
                break
        # Which steps of the library's Prefetch choices this was (e.g. "Rest of the season")
        names = prefetch.NAMES.get(kind, prefetch.NAMES["tv"])
        how = join([names[x] for x in ([end] if end and end != "none" else []) + ([start] if start != "off" else []) if x in names])
        if not new_whole and not new_start:
            if cooled:
                self.app.activity.event("next-episode", M("after {title} ({how}): {n} already cached within the cooldown — nothing to read",
                                                          one="after {title} ({how}): {n} already cached within the cooldown — nothing to read",
                                                          title=s.label, how=how, n=cooled))
            return
        first = (new_whole + new_start)[0]
        if first.rating_key:
            self._prefetched[first.rating_key] = first
        fg_whole = [j for j in new_whole if not j.background]
        fg_start = [j for j in new_start if not j.background]
        bg_new = [j for j in new_whole + new_start if j.background]
        with self._lock:
            # Whole files go ahead of any starts still waiting; the rest keep their place
            waiting = [j for j in chain if j.state == "waiting" and not j.cancel]
            busy = [j for j in chain if j not in waiting]
            self._chains[pid] = busy + fg_whole + waiting + fg_start
            # L2ARC-only: this request goes to the front of the line (anything of it already waiting moves up)
            mine = {(j.path, j.slice) for j in bg_new}
            for j in self._bg_queue:
                if j.state == "waiting" and (j.path, j.slice) in mine:
                    j.state, j.cancel = "cancelled", True
            self._bg_queue = bg_new + [j for j in self._bg_queue if j.state != "cancelled"]
            if key in self.sessions:
                self.sessions[key]["jobs"].extend((new_whole + new_start)[:SHOWN_JOBS])
        if new_whole and new_start:
            text = M("after {title} ({how}): caching {n} whole files and {more} lead-ins", one="after {title} ({how}): caching {n} whole file and {more} lead-ins",
                     title=s.label, how=how, n=len(new_whole), more=len(new_start))
        elif new_whole:
            text = M("after {title} ({how}): caching {n} whole files", one="after {title} ({how}): caching {n} whole file", title=s.label, how=how, n=len(new_whole))
        else:
            text = M("after {title} ({how}): caching {n} lead-ins", one="after {title} ({how}): caching {n} lead-in", title=s.label, how=how, n=len(new_start))
        if cooled:
            text = M("{text} · {n} skipped, cached within the cooldown", text=text, n=cooled)
        self.app.activity.event("next-episode", text)
        self._run_chain(pid, key, cfg["pacing_percent"])
        self._run_background()

    @staticmethod
    def _label(s, it) -> str:
        if s.type == "episode" and "season" in it:
            return f"{s.grandparent_title} · S{it['season']:02d}E{it['episode']:02d}"
        if it.get("collection"):
            return f"{it['collection']} · {it.get('title', '')}"
        title = it.get("title", "")
        return f"{s.grandparent_title} · {title}" if s.grandparent_title and it.get("rating_key") else title

    def _run_chain(self, pid, key, pacing):
        """One reader per player works through its queue in order (whole files first)."""
        with self._lock:
            if pid in self._runners:
                return  # the running one picks up what was just queued
            self._runners.add(pid)

        def run():
            try:
                while not self.stop.is_set():
                    with self._lock:
                        job = next((j for j in self._chains.get(pid, []) if j.state == "waiting" and not j.cancel), None)
                        if job is None:
                            self._runners.discard(pid)
                            return
                    self._run_job(key, job, 0, pacing, 1)
                    if job.state == "waiting":  # never started (stopping)
                        job.state = "cancelled"
            finally:
                with self._lock:
                    self._runners.discard(pid)
        threading.Thread(target=run, name="prefetch", daemon=True).start()

    def _run_background(self):
        """One reader for every player's L2ARC-only reads: always the front of the queue (the
        newest request), at the L2ARC pacing, until the queue is empty."""
        with self._lock:
            if self._bg_running:
                return
            self._bg_running = True

        def run():
            try:
                while not self.stop.is_set():
                    with self._lock:
                        job = next((j for j in self._bg_queue if j.state == "waiting" and not j.cancel), None)
                        if job is None:
                            self._bg_running = False
                            return
                    pacing = self.app.settings.section("warmup")["pacing_percent"]
                    self._run_job(f"{job.player}:", job, 0, pacing, 1)
                    if job.state == "waiting":  # never started (stopping)
                        job.state = "cancelled"
            finally:
                with self._lock:
                    self._bg_running = False
        threading.Thread(target=run, name="prefetch-l2arc", daemon=True).start()

    def _cached(self, kind: str, rk: str, fetch):
        hit = self._plex_cache.get((kind, rk))
        if hit and time.time() - hit[0] < PLEX_CACHE_TTL:
            return hit[1]
        val = fetch(rk)
        self._plex_cache[(kind, rk)] = (time.time(), val)
        return val

    # ---------------------------------------------------------------- jobs
    def _start(self, key, job: Job, delay: float, pacing: float, max_parallel: int):
        with self._lock:
            if key in self.sessions:
                self.sessions[key]["jobs"].append(job)
        t = threading.Thread(target=self._run_job, args=(key, job, delay, pacing, max_parallel), daemon=True)
        t.start()

    def _record_read(self, job: Job):
        if job.read:
            # "ssd": L2ARC-only reads on their way to the SSD; "ram": the rest, for viewers
            self.app.activity.record("read", feature=job.feature, label=job.target,
                                     cached=job.cached_bytes, disk=job.disk_bytes, route="ssd" if job.background else "ram")

    def _session_alive(self, key) -> bool:
        with self._lock:
            return key in self.sessions

    def _run_job(self, key, job: Job, delay: float, pacing: float, max_parallel: int):
        if delay and self.stop.wait(delay):
            return
        if not self._session_alive(key) and job.feature == "nowplaying":
            job.state = "cancelled"
            return
        # Limit concurrent jobs per feature (L2ARC-only reads have their own, so a pause doesn't hold others up).
        sem = self.app.job_slots("l2arc" if job.background else job.feature, max_parallel)
        with sem:
            if job.state == "cancelled":
                return
            job.state = "reading"
            stop = threading.Event()
            job.stop_event = stop
            if job.cancel:
                stop.set()

            def pause():
                if job.cancel or (not self._session_alive(key) and job.feature == "nowplaying"):
                    stop.set()
                    return False
                if not job.background:
                    return False
                why = self._background_pause()
                job.paused = why
                return why is not None

            def progress(n):
                job.read += n

            def timed(nbytes, seconds):
                fast = seconds <= 0 or nbytes / seconds / 1e6 >= 4 * job.disk_mb_s
                if fast:
                    job.cached_bytes += nbytes
                else:
                    job.disk_bytes += nbytes

            # L2ARC-only reads go no faster than ZFS can copy them to the SSD (when that's the pacing)
            pacer = Pacer(pacing, stop, pause, rate=self.app.l2arc_rate() if job.background else None)
            pacer.disk_mb_s = job.disk_mb_s
            if job.background:
                pacer.before_wait = self._bg_pauses_for().mark_idle
            started = time.monotonic()
            res = None
            try:
                if job.background:
                    pacer.check()  # wait here first if it's a time to pause
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
                    self.app.activity.event(job.feature, M("stopped (playback ended): {title}", title=job.target))
                return
            took = time.monotonic() - started
            job.finished = time.time()
            if res.error:
                job.state = "failed"
                job.message = res.error
                self.app.activity.event(job.feature, M("couldn't read {path}: {why}", path=job.path, why=res.error), "warn")
                return
            job.state = "done"
            job.paused = None
            whole = bool(job.size) and job.length >= job.size and not job.slice
            if job.slice or whole:  # (a whole file also counts as a lead-in)
                # Timed as it went: mostly back at cache speed means it was still cached
                hit = job.cached_bytes >= 0.9 * job.read if job.read else None
                self.app.cachelog.note(job.path, whole=whole, route=self.app.cache_route(job.ram), hit=hit,
                                       library=job.library_key)
            self._record_read(job)
            if job.ram and not job.background:  # (what's meant to leave RAM isn't checked for still being there)
                self._retention.append((time.time() + RETENTION_AFTER, job))
            if not job.background or job.length >= 32 * MiB:
                # Say what was read: a lead-in, the whole file, or part of it (from the play position)
                what = M("lead-in") if job.slice else (M("whole file") if whole else M("from the play position"))
                self.app.activity.event(job.feature, M("{title}: {what}, {size} cached in {time}", title=job.target, what=what,
                                                       size=size_text(res.bytes), time=duration_text(took)))
            self.app.activity.count(job.feature)
            if job.background and job.length < 32 * MiB:
                # Lead-ins are too small for the reader to pace itself: L2ARC pacing between them,
                # like warm-up (and a pause, if it's time for one). Holding the slot keeps it in order.
                try:
                    pacer.after(res.seconds, res.bytes)  # time spent reading (not waiting in a pause)
                except Stopped:
                    pass

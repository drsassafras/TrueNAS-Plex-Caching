"""Watches Plex's active sessions and runs the two playback features.

* Now-playing preload: read ahead of the viewer so seeking hits cache.
* Next-episode prefetch: read the start of the next episode.

Replaces the Tautulli-triggered scripts; Plex itself reports what's playing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .plex import PlexClient, PlexError
from .reader import MiB, Pacer, Stopped, read_range

GiB = 1024 ** 3
RECENT_TTL = 6 * 3600  # don't prefetch the same file twice within 6 hours
# Automatic now-playing delay: let Plex finish its own startup reads first.
AUTO_DELAY_LOCAL = 20
AUTO_DELAY_REMOTE = 45


@dataclass
class Job:
    feature: str  # "nowplaying" | "nextepisode"
    target: str  # human label
    path: str
    offset: int
    length: int
    state: str = "waiting"  # waiting | reading | done | failed | cancelled
    read: int = 0
    started: float = field(default_factory=time.time)
    finished: float | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("feature", "target", "state", "read", "length", "started", "finished", "message")}


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
        self.thread = threading.Thread(target=self._loop, name="playback", daemon=True)

    def start(self):
        self.thread.start()

    # -------------------------------------------------------------- polling
    def active_streams(self) -> int:
        with self._lock:
            return len(self.sessions)

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
        now_keys = set()
        for s in current:
            key = f"{s.session_key}:{s.rating_key}"
            now_keys.add(key)
            with self._lock:
                info = self.sessions.get(key)
                if info is None:
                    info = {"key": key, "label": s.label, "player": s.player, "jobs": [], "started": time.time()}
                    self.sessions[key] = info
                    self._seen[key] = {"now": False, "next": False}
                    new = True
                else:
                    new = False
                info.update(
                    progress=round(s.progress * 100), state=s.state, local=s.local,
                    transcoding=s.transcoding, type=s.type, kbps=s.kbps,
                )
            if new:
                where = "remote" if not s.local else "local"
                how = "transcoding" if s.transcoding else "direct play"
                self.app.activity.event("playback", f"started: {s.label} on {s.player or 'unknown player'} ({where}, {how})")
            self._maybe_nowplaying(key, s, settings)
            self._maybe_nextepisode(key, s, settings, client)
        with self._lock:
            for key in list(self.sessions):
                if key not in now_keys:
                    for job in self.sessions[key]["jobs"]:
                        if job.state in ("waiting", "reading"):
                            job.state = "cancelled"
                    del self.sessions[key]
                    self._seen.pop(key, None)
        cutoff = time.time() - RECENT_TTL
        self._recent = {p: t for p, t in self._recent.items() if t >= cutoff}

    # ------------------------------------------------------------- features
    def _maybe_nowplaying(self, key, s, settings):
        cfg = settings["nowplaying"]
        if not cfg["enabled"] or self._seen[key]["now"] or not s.files:
            return
        if cfg["min_kbps"] and s.kbps and s.kbps < cfg["min_kbps"]:
            self._seen[key]["now"] = True
            return
        self._seen[key]["now"] = True
        path, size = s.files[0]
        path = self.app.map_path(path)
        offset = int(size * s.progress) if size else 0
        offset -= offset % MiB
        if cfg["mode"] == "whole":
            length = max(0, size - offset)
        elif cfg["mode"] == "size":
            length = int(cfg["size_gb"] * GiB)
        else:
            length = int(max(1, s.kbps) * 125 * 60 * cfg["minutes"])
        if size:
            length = min(length, max(0, size - offset))
        if length <= 0:
            return
        delay = cfg["delay_sec"] or (AUTO_DELAY_REMOTE if (s.transcoding or not s.local) else AUTO_DELAY_LOCAL)
        what = "whole file" if cfg["mode"] == "whole" else (f"{cfg['minutes']} min ahead" if cfg["mode"] == "minutes" else f"{cfg['size_gb']} GB")
        job = Job("nowplaying", f"{s.label} ({what})", path, offset, length)
        self._start(key, job, delay, cfg["pacing_percent"], cfg["max_streams"])

    def _maybe_nextepisode(self, key, s, settings, client):
        cfg = settings["nextepisode"]
        if not cfg["enabled"] or self._seen[key]["next"] or s.type != "episode" or not s.grandparent_rating_key:
            return
        start_at = cfg["start_at"]
        if start_at == "credits":
            if s.credits_start_ms:
                due = s.view_offset_ms >= s.credits_start_ms - 5 * 60 * 1000
            else:
                due = s.progress >= 0.9
        else:
            due = s.progress * 100 >= float(start_at)
        if not due:
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
        budget = int(cfg["max_gb"] * GiB) if cfg["max_gb"] else 0
        files = nxt["files"]
        label = f"{s.grandparent_title} · S{nxt['season']:02d}E{nxt['episode']:02d}"
        for path, size, _kbps in files:
            path = self.app.map_path(path)
            if path in self._recent:
                continue
            length = size if not budget else min(size or budget, budget // len(files))
            if length <= 0:
                continue
            self._start(key, Job("nextepisode", label, path, 0, length), 0, cfg["pacing_percent"], 20)

    # ---------------------------------------------------------------- jobs
    def _start(self, key, job: Job, delay: float, pacing: float, max_parallel: int):
        with self._lock:
            if key in self.sessions:
                self.sessions[key]["jobs"].append(job)
        t = threading.Thread(target=self._run_job, args=(key, job, delay, pacing, max_parallel), daemon=True)
        t.start()

    def _session_alive(self, key) -> bool:
        with self._lock:
            return key in self.sessions

    def _run_job(self, key, job: Job, delay: float, pacing: float, max_parallel: int):
        if delay and self.stop.wait(delay):
            return
        if not self._session_alive(key):
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
                if not self._session_alive(key) and job.feature == "nowplaying":
                    stop.set()
                return False

            def progress(n):
                job.read += n

            pacer = Pacer(pacing, stop, pause)
            started = time.monotonic()
            try:
                res = read_range(job.path, job.offset, job.length, pacer, progress=progress)
            except Stopped:
                job.state = "cancelled"
                job.finished = time.time()
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
            self._recent[job.path] = time.time()
            gb = res.bytes / 1e9
            self.app.activity.event(job.feature, f"{job.target}: {gb:.1f} GB cached in {took:.0f} s")
            self.app.activity.count(job.feature)

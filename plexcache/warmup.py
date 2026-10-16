"""Warm-up: fill the Lead-in Cache — every file's lead-in (its first seconds and its seek
index) — on the L2ARC, per library on a schedule.

Port of plex_preload_l2arc2.sh with its unit and sizing bugs fixed:
bitrates are kbps, so bytes = kbps * 125 * seconds everywhere.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from . import cachemode
from .i18n import M
from .layout import tail_ranges
from .reader import MiB, Pacer, Stopped, is_cache_hit, is_probe_hit, read_range

NEXT_DAY = 24 * 3600  # how long after a run its sample is checked
SAMPLE_FILES = 30     # files a run keeps, to check a day later whether they're still on the SSD

GB = 1000 ** 3
KINDS = ("movie", "tv", "audio")
KIND_LABEL = {"movie": "Movies", "tv": "TV", "audio": "Audio"}

# Each library's warm-up schedule: every one but "none" runs at boot, some again on a timer
# (scheduler.py). With an L2ARC the default is at boot only; without one warm-up can't run, so
# every schedule needs an L2ARC.
SCHEDULES = ("none", "boot", "twice_monthly", "weekly", "twice_weekly", "daily")
SCHEDULE_NAMES = {"none": M("No warm-up"), "boot": M("At boot only"), "twice_monthly": M("At boot + twice monthly"),
                  "weekly": M("At boot + weekly"), "twice_weekly": M("At boot + twice weekly"), "daily": M("At boot + daily")}
SCHEDULE_DYN = cachemode.Dynamic(l2arc="boot", ram="none", l2arc_only=SCHEDULES[1:])
cachemode.DYNAMIC["warmup.schedule"] = SCHEDULE_DYN


def resolve_schedule(app_or_mode, key: str, value: str, lib_name: str = "") -> str:
    """A library's warm-up schedule now: Default per the caching mode; without an L2ARC, none
    (noted for Health if one was chosen)."""
    one = cachemode.Dynamic(SCHEDULE_DYN.l2arc, SCHEDULE_DYN.ram, SCHEDULE_DYN.l2arc_only,
                            M("{library}: {setting}", library=lib_name or key, setting=M("Warm-up")), SCHEDULE_NAMES)
    if isinstance(app_or_mode, str):
        return cachemode.resolve(f"warmup.{key}.schedule", value, app_or_mode, None, one)
    return cachemode.resolve(f"warmup.{key}.schedule", value, app_or_mode.cache_mode(), app_or_mode.fallbacks, one)


def preload_bytes(kbps: int, seconds: float, size: int = 0) -> int:
    """Bytes to read for ``seconds`` of playback, rounded up to whole MiB."""
    raw = max(1, int(kbps)) * 125 * seconds
    n = max(1, math.ceil(raw / MiB)) * MiB
    return min(n, size) if size > 0 else n


@dataclass
class LibraryStats:
    """Per-kind counts and average bitrates, used for sizing."""

    count: dict = field(default_factory=lambda: {k: 0 for k in KINDS})
    avg_kbps: dict = field(default_factory=lambda: {k: 0 for k in KINDS})
    scanned_at: float | None = None
    # The same per library, so the sizing table can show e.g. audiobooks and music apart
    # (their bitrates differ a lot): [{"key", "name", "kind", "count", "avg_kbps"}]
    libraries: list = field(default_factory=list)

    @classmethod
    def from_files(cls, files, names: dict | None = None) -> "LibraryStats":
        st = cls()
        totals = {k: 0 for k in KINDS}
        per: dict[str, dict] = {}
        for f in files:
            st.count[f.kind] += 1
            totals[f.kind] += f.kbps
            key = getattr(f, "library_key", "") or ""
            lib = per.setdefault(key, {"key": key, "name": (names or {}).get(key) or KIND_LABEL.get(f.kind, f.kind),
                                       "kind": f.kind, "count": 0, "total": 0})
            lib["count"] += 1
            lib["total"] += f.kbps
        for k in KINDS:
            st.avg_kbps[k] = int(totals[k] / st.count[k]) if st.count[k] else 0
        st.libraries = [{"key": v["key"], "name": v["name"], "kind": v["kind"], "count": v["count"],
                         "avg_kbps": int(v["total"] / v["count"])}
                        for v in sorted(per.values(), key=lambda v: (KINDS.index(v["kind"]), v["name"].lower()))]
        st.scanned_at = time.time()
        return st

    def to_dict(self) -> dict:
        return {"count": self.count, "avg_kbps": self.avg_kbps, "scanned_at": self.scanned_at, "libraries": self.libraries}


# Index (and other end-of-file parts) per file, until a run has measured your library.
DEFAULT_INDEX = {"movie": 512 * 1024, "tv": 256 * 1024, "audio": 0}


# Automatic L2ARC pacing reads from the disks at this share of l2arc_write_max: a little under,
# so ZFS keeps up with other data on its way to the SSD as well.
L2ARC_RATE_SHARE = 0.9

# Each library's lead-in length unless it has its own (the ↺ on the page goes back to these).
KIND_SECONDS = {"movie": 60, "tv": 30, "audio": 10}


def library_seconds(w: dict, key: str, kind: str) -> int:
    """Seconds of each file's start a library caches: its own choice, or its kind's default."""
    mine = (w.get("libraries") or {}).get(str(key)) or {}
    return int(mine.get("seconds") or KIND_SECONDS[kind])


def plan(stats: LibraryStats, w: dict, l2_total: int | None, index_avg: dict | None = None,
         keys: set | None = None) -> dict:
    """How much L2ARC the libraries' warm-ups need, and what seconds to actually use.

    ``keys``: the libraries warmed (None: all). Each library uses its own seconds. If the
    L2ARC (minus the reserve) can't hold them all, every library's seconds are reduced in
    proportion toward its kind's minimum, like the original script meant to.
    """
    pad = {"movie": w["padding_video"], "tv": w["padding_video"], "audio": w["padding_audio"]}
    idx_avg = dict(DEFAULT_INDEX, **(index_avg or {}))
    libs = [lib for lib in stats.libraries if keys is None or lib["key"] in keys]
    rows = []
    for lib in libs:
        k = lib["kind"]
        per_sec = lib["avg_kbps"] * 125 * lib["count"] * (100 + pad[k]) / 100
        hi, lo = library_seconds(w, lib["key"], k), min(w[f"{k}_min_sec"], library_seconds(w, lib["key"], k))
        rows.append({"key": lib["key"], "name": lib["name"], "kind": k, "count": lib["count"], "kbps": lib["avg_kbps"],
                     "per_sec": per_sec, "set_sec": hi, "min_sec": lo, "max": per_sec * hi, "min": per_sec * lo,
                     "index": lib["count"] * idx_avg[k]})  # the index etc. at the end of each file: always cached
    reserve = w["reserve_gb"] * GB
    index_total = sum(r["index"] for r in rows)
    need_max = sum(r["max"] for r in rows) + index_total
    need_min = sum(r["min"] for r in rows) + index_total
    factor = 0.0
    status, note = "unknown", M("L2ARC size unknown — add a TrueNAS API key in System to size this.")
    if l2_total is not None:
        available = l2_total - reserve
        if l2_total <= 0:
            status, note = "none", M("No L2ARC devices were found on the media pool.")
        elif need_max <= available * 0.9:
            status, note = "fits", M("Fits with room to spare.")
        elif need_max <= available:
            status, note = "tight", M("Fits, but with little room to spare.")
        elif need_min <= available:
            wiggle = need_max - need_min
            factor = min(1.0, (need_max - available) / wiggle) if wiggle > 0 else 1.0
            status, note = "reduced", M("Too big for your L2ARC — seconds are reduced automatically to fit.")
        else:
            factor = 1.0
            status, note = "insufficient", M("Even the minimum seconds don't fit. Lower the reserve or add L2ARC.")
    seconds, by_kind = {}, {k: {"need": 0.0, "used": 0.0, "index": 0.0} for k in KINDS}
    for r in rows:
        hi, lo = r["set_sec"], r["min_sec"]
        sec = float(hi) if not factor else (float(lo) if factor >= 1 else max(lo, math.floor((hi - (hi - lo) * factor) * 10) / 10))
        r["sec"] = sec
        r["used"] = r["per_sec"] * sec + r["index"]
        seconds[r["key"]] = sec
        by_kind[r["kind"]]["need"] += r["max"]
        by_kind[r["kind"]]["used"] += r["used"]
        by_kind[r["kind"]]["index"] += r["index"]
    return {
        "need_max": need_max,
        "need_min": need_min,
        "need_by_kind": {k: v["need"] for k, v in by_kind.items()},
        "planned_by_kind": {k: v["used"] for k, v in by_kind.items()},
        "index_by_kind": {k: v["index"] for k, v in by_kind.items()},
        "index_measured": bool(index_avg),
        "reserve": reserve,
        "l2_total": l2_total,
        "seconds": seconds,  # library key -> seconds to use
        "libraries": rows,  # per library, with per_sec (bytes per second of lead-in) for what-if sizing
        "status": status,
        "note": note,
    }


class WarmupRun:
    """One warm-up pass over the library."""

    def __init__(self, files, seconds: dict, pacing_percent: float, hdd_mb_per_sec,
                 stop, pause=None, arcstats=None, cache_index: bool = True, skip=None, note=None, rate=None):
        """``seconds``: per library key (or, failing that, per kind). ``hdd_mb_per_sec`` is the
        uncached disk speed: a number, or a function giving it for a file (datasets on
        different disks read at different speeds). ``skip(f)``: True for a file whose lead-in
        was cached within the cooldown (the cache log); ``note(f, hit)``: its lead-in was cached,
        and whether it was cached already (the read came back at cache speed).
        ``rate``: read from the disks at no more than this many bytes a second (instead of
        a share of disk time), to match how fast ZFS fills the L2ARC."""
        self.files = list(files)
        random.shuffle(self.files)  # spread reads across the disks
        self.seconds = seconds
        self.pacer = Pacer(pacing_percent, stop, pause, rate=rate)
        self.hdd = hdd_mb_per_sec if callable(hdd_mb_per_sec) else (lambda f, v=hdd_mb_per_sec: v)
        self.arcstats = arcstats or (lambda: None)
        self.done = 0
        self.skipped = 0  # lead-ins still in their cooldown
        self.sample: list[str] = []  # a few files read, picked at random (reservoir), for the next-day check
        self._read_files = 0
        self.skip = skip
        self.note = note
        self.bytes = 0
        self.by_kind = {k: {"files": 0, "hits": 0, "reads": 0, "errors": 0, "bytes": 0} for k in KINDS}
        self.errors: list[str] = []
        self.started = time.time()
        self.finished: float | None = None
        self.result = "running"
        self.paused: str | None = None  # why it's waiting, while it is
        self.cache_index = cache_index
        # What was found at the ends of files, per kind
        self.index = {k: {"files": 0, "bytes": 0, "largest": 0, "mp4_at_end": 0} for k in KINDS}

    @property
    def total(self) -> int:
        return len(self.files)

    def progress(self) -> dict:
        return {"done": self.done, "total": self.total, "bytes": self.bytes, "started": self.started,
                "paused": self.paused, "skipped": self.skipped}

    def _read_tail(self, f, head: int) -> float:
        """Read the index and anything else a player reads at the end of ``f``. Returns seconds taken."""
        ranges, info = tail_ranges(f.path, f.size, head)
        if not ranges:
            return 0.0
        st = self.index[f.kind]
        took = 0.0
        got = 0
        for off, length in ranges:
            res = read_range(f.path, off, length)
            if res.error:
                break
            got += res.bytes
            took += res.seconds
        if got:
            st["files"] += 1
            st["bytes"] += got
            st["largest"] = max(st["largest"], got)
            if info.get("index_at") == "end":
                st["mp4_at_end"] += 1
            self.bytes += got
        return took

    def run(self) -> dict:
        before = self.arcstats() or {}
        try:
            for f in self.files:
                self.pacer.check()
                k = self.by_kind[f.kind]
                if self.skip and self.skip(f):
                    self.done += 1
                    self.skipped += 1
                    continue
                secs = self.seconds.get(getattr(f, "library_key", ""), self.seconds.get(f.kind, 0))
                n = preload_bytes(f.kbps, secs, f.size)
                self.pacer.disk_mb_s = self.hdd(f)
                file_start = self.bytes
                res = read_range(f.path, 0, n)
                self.done += 1
                k["files"] += 1
                if res.error:
                    k["errors"] += 1
                    if len(self.errors) < 20:
                        self.errors.append(f"{f.path}: {res.error}")
                    continue
                self.bytes += res.bytes
                k["bytes"] += res.bytes
                if is_cache_hit(res, self.hdd(f)):
                    k["hits"] += 1
                else:
                    k["reads"] += 1
                took = res.seconds
                if self.cache_index:
                    took += self._read_tail(f, n)
                if self.note:
                    self.note(f, is_cache_hit(res, self.hdd(f)))
                self._read_files += 1
                if len(self.sample) < SAMPLE_FILES:
                    self.sample.append(f.path)
                else:
                    j = random.randrange(self._read_files)
                    if j < SAMPLE_FILES:
                        self.sample[j] = f.path
                self.pacer.after(took, self.bytes - file_start)
            self.result = "ok"
        except Stopped:
            self.result = "stopped"
        self.finished = time.time()
        after = self.arcstats() or {}
        ok_files = sum(v["hits"] + v["reads"] for v in self.by_kind.values())
        hits = sum(v["hits"] for v in self.by_kind.values())
        errors = sum(v["errors"] for v in self.by_kind.values())
        if self.result == "ok" and errors and errors >= max(1, self.done // 2):
            self.result = "errors"
        return {
            "index": self.index if self.cache_index else None,
            "started": self.started,
            "finished": self.finished,
            "files": self.done,
            "skipped": self.skipped,
            "total": self.total,
            "bytes": self.bytes,
            "hit_rate": hits / ok_files if ok_files else None,
            "errors": errors,
            "error_samples": self.errors,
            "by_kind": self.by_kind,
            "seconds": self.seconds,
            "l2_before": before.get("l2_asize"),
            "l2_after": after.get("l2_asize"),
            "l2_growth": (after.get("l2_asize") - before.get("l2_asize"))
            if before.get("l2_asize") is not None and after.get("l2_asize") is not None
            else None,
            "result": self.result,
            "sample": list(self.sample),  # checked a day later (next_day_check), then dropped
        }


def next_day_check(paths: list[str]) -> dict:
    """A day after a warm-up: how many of its sample's lead-ins are still cached (on the SSD).
    One 1 MiB read from the start of each file, timed: an SSD answers in a couple of ms, a hard
    drive takes several. Reading them puts the misses back on the way to the SSD, too."""
    hits = done = 0
    for path in paths:
        res = read_range(path, 0, MiB)
        if res.error:
            continue
        done += 1
        hits += is_probe_hit(res)
    return {"checked": done, "hits": hits, "pct": round(100 * hits / done) if done else None}

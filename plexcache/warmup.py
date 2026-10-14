"""Library warm-up: cache the first seconds of every file, and its seek index, in L2ARC.

Port of plex_preload_l2arc2.sh with its unit and sizing bugs fixed:
bitrates are kbps, so bytes = kbps * 125 * seconds everywhere.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from .layout import tail_ranges
from .reader import MiB, Pacer, Stopped, is_cache_hit, read_range

GB = 1000 ** 3
KINDS = ("movie", "tv", "audio")
KIND_LABEL = {"movie": "Movies", "tv": "TV", "audio": "Audio"}


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


def plan(stats: LibraryStats, w: dict, l2_total: int | None, index_avg: dict | None = None) -> dict:
    """How much L2ARC the settings need, and what seconds to actually use.

    If the L2ARC (minus the reserve) can't hold the maximum, seconds are
    reduced proportionally toward each kind's minimum, like the script meant to.
    """
    pad = {"movie": w["padding_video"], "tv": w["padding_video"], "audio": w["padding_audio"]}
    mx, mn = {}, {}
    for k in KINDS:
        per_sec = stats.avg_kbps[k] * 125 * stats.count[k]
        mx[k] = per_sec * w[f"{k}_sec"] * (100 + pad[k]) / 100
        mn[k] = per_sec * w[f"{k}_min_sec"] * (100 + pad[k]) / 100
    # The index etc. at the end of each file: a fixed amount whatever the seconds.
    idx_avg = dict(DEFAULT_INDEX, **(index_avg or {}))
    index = {k: stats.count[k] * idx_avg[k] for k in KINDS}  # always cached: players read it before playing
    reserve = w["reserve_gb"] * GB
    need_max = sum(mx.values()) + sum(index.values())
    need_min = sum(mn.values()) + sum(index.values())
    seconds = {k: float(w[f"{k}_sec"]) for k in KINDS}
    status, note = "unknown", "L2ARC size unknown — add a TrueNAS API key in Settings to size this."
    if l2_total is not None:
        available = l2_total - reserve
        if l2_total <= 0:
            status, note = "none", "No L2ARC devices were found on the media pool."
        elif need_max <= available * 0.9:
            status, note = "fits", "Fits with room to spare."
        elif need_max <= available:
            status, note = "tight", "Fits, but with little room to spare."
        elif need_min <= available:
            wiggle = need_max - need_min
            factor = min(1.0, (need_max - available) / wiggle) if wiggle > 0 else 1.0
            for k in KINDS:
                hi, lo = w[f"{k}_sec"], w[f"{k}_min_sec"]
                seconds[k] = max(lo, math.floor((hi - (hi - lo) * factor) * 10) / 10)
            status, note = "reduced", "Too big for your L2ARC — seconds are reduced automatically to fit."
        else:
            for k in KINDS:
                seconds[k] = float(w[f"{k}_min_sec"])
            status, note = "insufficient", "Even the minimum seconds don't fit. Lower the reserve or add L2ARC."
    used = {k: (mx[k] * seconds[k] / w[f"{k}_sec"]) if w[f"{k}_sec"] else 0 for k in KINDS}
    return {
        "need_max": need_max,
        "need_min": need_min,
        "need_by_kind": mx,
        "planned_by_kind": used,
        "index_by_kind": index,
        "index_measured": bool(index_avg),
        "reserve": reserve,
        "l2_total": l2_total,
        "seconds": seconds,
        "status": status,
        "note": note,
    }


class WarmupRun:
    """One warm-up pass over the library."""

    def __init__(self, files, seconds: dict, pacing_percent: float, hdd_mb_per_sec,
                 stop, pause=None, arcstats=None, cache_index: bool = True):
        """``hdd_mb_per_sec`` is the uncached disk speed: a number, or a function
        giving it for a file (datasets on different disks read at different speeds)."""
        self.files = list(files)
        random.shuffle(self.files)  # spread reads across the disks
        self.seconds = seconds
        self.pacer = Pacer(pacing_percent, stop, pause)
        self.hdd = hdd_mb_per_sec if callable(hdd_mb_per_sec) else (lambda f, v=hdd_mb_per_sec: v)
        self.arcstats = arcstats or (lambda: None)
        self.done = 0
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
                "paused": self.paused}

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
                n = preload_bytes(f.kbps, self.seconds[f.kind], f.size)
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
                self.pacer.after(took)
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
        }

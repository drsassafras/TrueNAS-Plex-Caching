"""Library warm-up: cache the first seconds of every file in L2ARC.

Port of plex_preload_l2arc2.sh with its unit and sizing bugs fixed:
bitrates are kbps, so bytes = kbps * 125 * seconds everywhere.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from .reader import MiB, Pacer, Stopped, is_cache_hit, read_range

GB = 1000 ** 3
KINDS = ("movie", "tv", "audio")


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

    @classmethod
    def from_files(cls, files) -> "LibraryStats":
        st = cls()
        totals = {k: 0 for k in KINDS}
        for f in files:
            st.count[f.kind] += 1
            totals[f.kind] += f.kbps
        for k in KINDS:
            st.avg_kbps[k] = int(totals[k] / st.count[k]) if st.count[k] else 0
        st.scanned_at = time.time()
        return st

    def to_dict(self) -> dict:
        return {"count": self.count, "avg_kbps": self.avg_kbps, "scanned_at": self.scanned_at}


def plan(stats: LibraryStats, w: dict, l2_total: int | None) -> dict:
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
    reserve = w["reserve_gb"] * GB
    need_max = sum(mx.values())
    need_min = sum(mn.values())
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
        "reserve": reserve,
        "l2_total": l2_total,
        "seconds": seconds,
        "status": status,
        "note": note,
    }


class WarmupRun:
    """One warm-up pass over the library."""

    def __init__(self, files, seconds: dict, pacing_percent: float, hdd_mb_per_sec: float,
                 stop, pause=None, arcstats=None):
        self.files = list(files)
        random.shuffle(self.files)  # spread reads across the disks
        self.seconds = seconds
        self.pacer = Pacer(pacing_percent, stop, pause)
        self.hdd = hdd_mb_per_sec
        self.arcstats = arcstats or (lambda: None)
        self.done = 0
        self.bytes = 0
        self.by_kind = {k: {"files": 0, "hits": 0, "reads": 0, "errors": 0, "bytes": 0} for k in KINDS}
        self.errors: list[str] = []
        self.started = time.time()
        self.finished: float | None = None
        self.result = "running"

    @property
    def total(self) -> int:
        return len(self.files)

    def progress(self) -> dict:
        return {"done": self.done, "total": self.total, "bytes": self.bytes, "started": self.started}

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
                if is_cache_hit(res, self.hdd):
                    k["hits"] += 1
                else:
                    k["reads"] += 1
                self.pacer.after(res.seconds)
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

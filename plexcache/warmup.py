"""Warm-up: fill the Lead-in Cache — every file's lead-in (its first seconds and its seek
index) — on the L2ARC, per library on a schedule.

Port of plex_preload_l2arc2.sh with its unit and sizing bugs fixed:
bitrates are kbps, so bytes = kbps * 125 * seconds everywhere.
"""

import math
from collections.abc import Callable
from typing import Any
import random
import time
from dataclasses import dataclass, field

from . import cachemode
from .i18n import M
from .layout import VIDEO_SHARE, min_head, tail_ranges
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


def preload_bytes(kbps: int, seconds: float, size: int = 0, record: int | None = None) -> int:
    """Bytes to read for ``seconds`` of playback, in whole records of the file's dataset (ZFS
    reads and caches whole records anyway), at least one. 0 seconds: just the first record.
    Without a known record size, whole MiB."""
    unit = record or MiB
    raw = max(1, int(kbps)) * 125 * max(0.0, seconds)
    n = max(1, math.ceil(raw / unit)) * unit
    return min(n, size) if size > 0 else n


def align_ranges(ranges: list, record: int | None, size: int) -> list:
    """(offset, length) ranges widened to whole records of the dataset, the way ZFS reads them,
    and merged where they meet. Unchanged without a known record size."""
    if not record:
        return list(ranges)
    out = []
    for off, length in sorted(ranges):
        start = off // record * record
        end = min(size, -(-(off + length) // record) * record) if size else -(-(off + length) // record) * record
        if out and start <= out[-1][0] + out[-1][1]:
            prev = out[-1]
            out[-1] = (prev[0], max(prev[0] + prev[1], end) - prev[0])
        else:
            out.append((start, end - start))
    return out


@dataclass
class LibraryStats:
    """Per-kind counts and average bitrates, used for sizing."""

    count: dict = field(default_factory=lambda: {k: 0 for k in KINDS})
    avg_kbps: dict = field(default_factory=lambda: {k: 0 for k in KINDS})
    scanned_at: float | None = None
    # The same per library, so the sizing table can show e.g. audiobooks and music apart
    # (their bitrates differ a lot): [{"key", "name", "kind", "count", "avg_kbps", "avg_size"}]
    libraries: list = field(default_factory=list)

    @classmethod
    def from_files(cls, files, names: dict | None = None) -> LibraryStats:
        st = cls()
        totals = {k: 0 for k in KINDS}
        per: dict[str, dict] = {}
        for f in files:
            st.count[f.kind] += 1
            totals[f.kind] += f.kbps
            key = getattr(f, "library_key", "") or ""
            lib = per.setdefault(key, {"key": key, "name": (names or {}).get(key) or KIND_LABEL.get(f.kind, f.kind),
                                       "kind": f.kind, "count": 0, "total": 0, "size": 0})
            lib["count"] += 1
            lib["total"] += f.kbps
            lib["size"] += getattr(f, "size", 0) or 0
        for k in KINDS:
            st.avg_kbps[k] = int(totals[k] / st.count[k]) if st.count[k] else 0
        st.libraries = [{"key": v["key"], "name": v["name"], "kind": v["kind"], "count": v["count"],
                         "avg_kbps": int(v["total"] / v["count"]), "avg_size": int(v["size"] / v["count"])}
                        for v in sorted(per.values(), key=lambda v: (KINDS.index(v["kind"]), v["name"].lower()))]
        st.scanned_at = time.time()
        return st

    def to_dict(self) -> dict:
        return {"count": self.count, "avg_kbps": self.avg_kbps, "scanned_at": self.scanned_at, "libraries": self.libraries}


# Metadata at the end of a file (index, tags, fonts…) per file, until a run has measured your library.
DEFAULT_INDEX = {"movie": 512 * 1024, "tv": 256 * 1024, "audio": 0}
# Metadata in front of the video (headers, attachments kept there), until a run has measured it.
DEFAULT_FRONT = {"movie": 64 * 1024, "tv": 64 * 1024, "audio": 0}

# ---- Record size: ZFS reads, caches and copies to the SSD in whole records, so a lead-in costs
# its bytes rounded up to records, and an index at the end of a file costs the records it touches
# there besides. Recommendations follow the usual guidance for media by bitrate — about half a
# second to a second of playback per record — with ZFS's own 128K for files under 1 MB, which
# fit in a block or two whatever the setting.
KiB, MB = 1024, 1000 * 1000
# (up to this many kbps, record size, the bitrate around the middle of the band)
RECORD_BANDS = [(1_000, 512 * KiB, 250), (15_000, 1024 * KiB, 3_873), (40_000, 2048 * KiB, 24_495),
                (None, 4096 * KiB, 80_000)]
RECORD_BELOW, RECORD_ABOVE = 128 * KiB, 8192 * KiB  # the neighbours past the first and last band
SMALL_FILE = MB
L2ARC_HEADER_BYTES = 96  # RAM the L2ARC uses per record it holds (roughly; 70–100 B by OpenZFS version)


def recommend_recordsize(kbps: float, avg_size: float = 0) -> dict:
    """The record size that suits a library, and the next best.

    The next best is the neighbouring band's size on the side the bitrate leans to, so a
    library near the line between two bands names the same two sizes on either side of it:
    a dataset set to either is fine, and it doesn't flip as titles come and go."""
    if avg_size and avg_size < SMALL_FILE:
        return {"best": RECORD_BELOW, "alt": RECORD_BANDS[0][1]}
    for i, (top, size, mid) in enumerate(RECORD_BANDS):
        if top is None or kbps < top:
            if kbps >= mid:
                alt = RECORD_BANDS[i + 1][1] if i + 1 < len(RECORD_BANDS) else RECORD_ABOVE
            else:
                alt = RECORD_BANDS[i - 1][1] if i else RECORD_BELOW
            return {"best": size, "alt": alt}
    raise AssertionError("unreachable")


def whole_records(n: float, record: int) -> float:
    """``n`` bytes rounded up to whole records (0 stays 0)."""
    return math.ceil(n / record) * record if n > 0 else 0


def cached_bytes(lead: float, index: float, index_share: float, record: int, avg_size: float,
                 index_first: bool, front: float = 0) -> float:
    """What one file's lead-in (and metadata) takes in the cache at a record size, on average.

    A file no bigger than a record is one block of its own size. Otherwise its start is the
    lead-in, never less than the metadata in front of the video (``front``) and 60% of a record of
    video (layout.min_head), rounded up to whole records (half a record more on average, at least
    one record); and metadata at the end adds the whole records it takes, for the share of files
    keeping it there — ZFS reads and caches nothing smaller; at the front it's part of the start."""
    head = max(lead, front + VIDEO_SHARE * record) + (index * index_share if index_first else 0)
    if avg_size and (avg_size <= record or head >= avg_size):
        return avg_size  # the whole file, index and all
    tail = 0.0 if index_first or not index else index_share * whole_records(index, record)
    total = (record if head <= record else head + record / 2) + tail
    return min(total, avg_size) if avg_size else total


# Automatic L2ARC pacing reads from the disks at this share of l2arc_write_max: a little under,
# so ZFS keeps up with other data on its way to the SSD as well.
L2ARC_RATE_SHARE = 0.9

# Each library's lead-in length unless it has its own (the ↺ on the page goes back to these).
KIND_SECONDS = {"movie": 60, "tv": 30, "audio": 10}


def library_seconds(w: dict, key: str, kind: str) -> int:
    """Seconds of each file's start a library caches: its own choice, or its kind's default."""
    mine = (w.get("libraries") or {}).get(str(key)) or {}
    secs = mine.get("seconds")
    return int(secs) if secs is not None else KIND_SECONDS[kind]  # 0: one record


def _record_info(rec) -> dict:
    """``records`` entries: a record size, or {"rs", "blocks" (zfs.block_sizes()), "dataset"}."""
    return rec if isinstance(rec, dict) else {"rs": rec}


def block_mix(rs: int | None, blocks: dict | None) -> list:
    """[(block size, share of files)] the library's files were actually written with: a new
    record size only applies to files written after it was set. Without a sample, the setting."""
    sizes = {int(k): n for k, n in ((blocks or {}).get("sizes") or {}).items() if n}
    total = sum(sizes.values())
    if total:
        return sorted((bs, n / total) for bs, n in sizes.items())
    return [(rs, 1.0)] if rs else []


def plan(stats: LibraryStats, w: dict, l2_total: int | None, index_avg: dict | None = None,
         keys: set | None = None, records: dict | None = None, index_layout: dict | None = None) -> dict:
    """How much L2ARC the libraries' warm-ups need, and what seconds to actually use.

    ``keys``: the libraries warmed (None: all). Each library uses its own seconds. Sizes are
    what the cache really holds: lead-ins in whole records of the library's dataset, as its
    files are written, plus the metadata for files keeping some at the end. If the L2ARC (minus
    the system buffer) can't hold them all, every library's seconds are cut by the same share,
    as little as fits, down to 0 — each file's metadata: its start through 60% of a record of
    video, and its end.

    ``records``: library key -> its dataset's record size, as checked (never guessed: a
    library whose dataset couldn't be checked has none, and is sized from its bitrate alone),
    or a dict with the files' block sizes and the dataset too. ``index_layout``: kind ->
    {"share" of files with metadata at the end, "avg" bytes there, "front" bytes before the
    video}. Each library also gets four sizes for the sizing table: ``target_min`` (optimized
    files at the recommended record size: the least start, and the metadata at the end),
    ``your_min`` (the same for the files as they are, at your record size), ``target`` (optimized,
    the recommended record size, at the library's seconds) and ``your`` (as the files are written now).
    """
    pad = {"movie": w["padding_video"], "tv": w["padding_video"], "audio": w["padding_audio"]}
    idx_avg = dict(DEFAULT_INDEX, **(index_avg or {}))
    layout = {k: {"share": 1.0 if idx_avg[k] else 0.0, "avg": idx_avg[k], "front": DEFAULT_FRONT[k]} for k in KINDS}
    for k, v in (index_layout or {}).items():
        layout[k] = dict(layout.get(k) or {}, **v)
    libs = [lib for lib in stats.libraries if keys is None or lib["key"] in keys]
    rows = []
    for lib in libs:
        k, n = lib["kind"], lib["count"]
        size = lib.get("avg_size") or 0
        per_sec = lib["avg_kbps"] * 125 * n * (100 + pad[k]) / 100
        bps = per_sec / n if n else 0  # bytes a second of one file, margin included
        info = _record_info((records or {}).get(lib["key"]))
        rs, ix = info.get("rs"), layout[k]
        hi = library_seconds(w, lib["key"], k)
        idx = ix["share"] * ix["avg"]  # index bytes at the end, per file on average
        mix = block_mix(rs, info.get("blocks"))
        rec = recommend_recordsize(lib["avg_kbps"], size)

        front = ix.get("front") or 0  # metadata in front of the video, per file on average

        def cost(sec, n=n, bps=bps, ix=ix, mix=mix, size=size, per_sec=per_sec, idx=idx, front=front):
            """What the library's lead-ins take at ``sec`` seconds each."""
            if mix:
                return n * sum(share * cached_bytes(bps * sec, ix["avg"], ix["share"], bs, size, False, front) for bs, share in mix)
            least = max(per_sec * sec, n * MiB) + n * idx  # record size unknown: whole MiB, as warm-up reads then
            return min(least, n * size) if size else least  # (never more than the whole files)

        row = {"key": lib["key"], "name": lib["name"], "kind": k, "count": n, "kbps": lib["avg_kbps"],
               "per_sec": per_sec, "set_sec": hi, "min_sec": 0.0, "max": cost(hi), "min": cost(0),
               "index": n * idx, "cost": cost,
               "avg_size": size, "bps": bps, "index_share": ix["share"], "index_avg": ix["avg"], "front": front,
               "rs": rs, "dataset": info.get("dataset"), "blocks": mix,
               "rs_best": rec["best"], "rs_alt": rec["alt"],
               # either of the two is fine, so a library on the line between them doesn't flip
               "rs_ok": bool(rs) and rs in (rec["best"], rec["alt"]),
               # Target: the files as the optimizer lays them out (its metadata at the end, next to nothing
               # in front of the video) at the recommended record size; Your: the files as they are
               "target_min": n * cached_bytes(0, ix["avg"], 1.0, rec["best"], size, False),
               "your_min": n * cached_bytes(0, ix["avg"], ix["share"], rs, size, False, front) if rs else None,
               "target": n * cached_bytes(bps * hi, ix["avg"], 1.0, rec["best"], size, False),
               "your": cost(hi) if mix else None}
        rows.append(row)
    reserve = w["reserve_gb"] * GB
    need_max = sum(r["max"] for r in rows)
    need_min = sum(r["min"] for r in rows)
    cut = 0.0  # the share every library's seconds are cut by
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
            lo, hi_cut = 0.0, 1.0  # the smallest cut that fits (sizes only grow with seconds)
            for _ in range(40):
                mid = (lo + hi_cut) / 2
                if sum(r["cost"](_cut_sec(r["set_sec"], mid)) for r in rows) <= available:
                    hi_cut = mid
                else:
                    lo = mid
            cut = hi_cut
            status, note = "reduced", M("Too big for your L2ARC — seconds are reduced automatically to fit.")
        else:
            cut = 1.0
            status, note = "insufficient", M("Even each file’s metadata doesn’t fit. Lower the system buffer, or add a larger L2ARC.")
    seconds, by_kind = {}, {k: {"need": 0.0, "used": 0.0, "index": 0.0} for k in KINDS}
    for r in rows:
        sec = _cut_sec(r["set_sec"], cut)
        r["sec"] = sec
        r["used"] = r["cost"](sec)
        seconds[r["key"]] = sec
        by_kind[r["kind"]]["need"] += r["max"]
        by_kind[r["kind"]]["used"] += r["used"]
        by_kind[r["kind"]]["index"] += r["index"]
        del r["cost"]
    return {
        "need_max": need_max,
        "need_min": need_min,
        "need_by_kind": {k: v["need"] for k, v in by_kind.items()},
        "planned_by_kind": {k: v["used"] for k, v in by_kind.items()},
        "index_by_kind": {k: v["index"] for k, v in by_kind.items()},
        "index_measured": bool(index_avg),
        "reserve": reserve,
        "l2_total": l2_total,
        "seconds": seconds,  # library key -> seconds to use (0: one record)
        "libraries": rows,  # per library, with per_sec (bytes per second of lead-in) for what-if sizing
        "status": status,
        "note": note,
    }


def _cut_sec(sec: float, cut: float) -> float:
    """``sec`` cut by the share ``cut``, rounded down to a tenth of a second (0: metadata only)."""
    if cut <= 0:
        return float(sec)
    return max(0.0, math.floor(sec * (1 - cut) * 10) / 10)


class WarmupRun:
    """One warm-up pass over the library."""

    def __init__(self, files, seconds: dict, pacing_percent: float, hdd_mb_per_sec: float | Callable[[Any], float],
                 stop, pause=None, arcstats=None, cache_index: bool = True, skip=None, note=None, rate=None,
                 record=None):
        """``seconds``: per library key (or, failing that, per kind). ``hdd_mb_per_sec`` is the
        uncached disk speed: a number, or a function giving it for a file (datasets on
        different disks read at different speeds). ``skip(f)``: True for a file whose lead-in
        was cached within the cooldown (the cache log); ``note(f, hit)``: its lead-in was cached,
        and whether it was cached already (the read came back at cache speed).
        ``rate``: read from the disks at no more than this many bytes a second (instead of
        a share of disk time), to match how fast ZFS fills the L2ARC. ``record(f)``: the record
        size of the file's dataset (None if unknown), so reads come in whole records of it."""
        self.files = list(files)
        random.shuffle(self.files)  # spread reads across the disks
        self.seconds = seconds
        self.pacer = Pacer(pacing_percent, stop, pause, rate=rate)
        self.hdd: Callable[[Any], float] = hdd_mb_per_sec if callable(hdd_mb_per_sec) else (lambda f, v=hdd_mb_per_sec: v)
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
        self.record = record or (lambda f: None)
        # What was found at the ends of files, per kind
        self.index = {k: {"files": 0, "bytes": 0, "largest": 0, "mp4_at_end": 0, "front_files": 0, "front": 0} for k in KINDS}

    @property
    def total(self) -> int:
        return len(self.files)

    def progress(self) -> dict:
        return {"done": self.done, "total": self.total, "bytes": self.bytes, "started": self.started,
                "paused": self.paused, "skipped": self.skipped}

    def _read_tail(self, f, head: int, ranges: list, info: dict) -> float:
        """Read the metadata at the end of ``f`` (the index, tags, fonts…: ``ranges`` from
        tail_ranges()), all but what the start (``head``) holds. Returns seconds taken."""
        start = info.get("video_start")
        ranges = [(o, n) for o, n in ranges if o + n > head and (start is None or o >= start)]
        if not ranges:
            return 0.0
        # The metadata's own size is what's measured, so the sizing table can work out the whole
        # records it takes at any record size...
        size = sum(n for _, n in ranges)
        # ... but it's read in whole records, the way ZFS reads them — minus what the lead-in already read
        ranges = [(o, n) for o, n in align_ranges(ranges, self.record(f), f.size) if o + n > head]
        ranges = [(max(o, head), o + n - max(o, head)) for o, n in ranges]
        st = self.index[f.kind]
        took = 0.0
        got = 0
        ok = True
        for off, length in ranges:
            res = read_range(f.path, off, length)
            if res.error:
                ok = False
                break
            got += res.bytes
            took += res.seconds
        if got:
            if ok:
                st["files"] += 1
                st["bytes"] += size
                st["largest"] = max(st["largest"], size)
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
                rec = self.record(f)
                # the file's metadata: at the end (read first), and where its video starts
                meta, info = tail_ranges(f.path, f.size, 0) if f.size else ([], {})
                if info.get("video_start") is not None:  # what's in front of the video, measured for the sizing
                    self.index[f.kind]["front_files"] += 1
                    self.index[f.kind]["front"] += info["video_start"]
                # its start: the lead-in, never less than the metadata in front of the video and 60% of a record of video
                n = max(preload_bytes(f.kbps, secs, f.size, rec), min_head(info.get("video_start"), rec, f.size))
                self.pacer.disk_mb_s = self.hdd(f)
                file_start = self.bytes
                took = self._read_tail(f, n, meta, info) if self.cache_index else 0.0
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
                took += res.seconds
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
        l2_before, l2_after = before.get("l2_asize"), after.get("l2_asize")
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
            "l2_growth": l2_after - l2_before if l2_before is not None and l2_after is not None else None,
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

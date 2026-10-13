"""Reads ZFS state that is visible from inside the container."""

from __future__ import annotations

import os

PROC = os.environ.get("PLEXCACHE_PROC", "/proc")
SYS = os.environ.get("PLEXCACHE_SYS", "/sys")


def arcstats() -> dict | None:
    """Parse /proc/spl/kstat/zfs/arcstats into {name: int}, or None if unreadable."""
    try:
        with open(os.path.join(PROC, "spl/kstat/zfs/arcstats"), encoding="ascii") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    out = {}
    for line in lines[2:]:
        parts = line.split()
        if len(parts) == 3:
            try:
                out[parts[0]] = int(parts[2])
            except ValueError:
                pass
    return out or None


def arc_summary(stats: dict | None) -> dict | None:
    if not stats:
        return None
    hits, misses = stats.get("hits", 0), stats.get("misses", 0)
    l2_hits, l2_misses = stats.get("l2_hits", 0), stats.get("l2_misses", 0)
    return {
        "arc_size": stats.get("size", 0),
        "arc_max": stats.get("c_max", 0),
        # Free RAM as ZFS sees it (whole NAS); ARC can grow into it.
        "memory_free": stats.get("memory_free_bytes"),
        "arc_hit_ratio": hits / (hits + misses) if hits + misses else None,
        "l2_size": stats.get("l2_size", 0),  # logical bytes cached
        "l2_asize": stats.get("l2_asize", 0),  # bytes used on the devices
        "l2_hit_ratio": l2_hits / (l2_hits + l2_misses) if l2_hits + l2_misses else None,
        "l2_present": stats.get("l2_size", 0) > 0 or (l2_hits + l2_misses) > 0,
    }


def meminfo() -> dict:
    """/proc/meminfo in bytes ({"MemTotal": ..., "MemAvailable": ...}); empty if unreadable."""
    out = {}
    try:
        with open(os.path.join(PROC, "meminfo"), encoding="ascii") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    out[parts[0].rstrip(":")] = int(parts[1]) * (1024 if len(parts) > 2 else 1)
    except OSError:
        pass
    return out


def disk_busy_ms() -> dict:
    """Milliseconds each whole disk has spent busy since boot ({"sda": 123456}).

    From /proc/diskstats (field 13, "time spent doing I/Os"); empty if unreadable."""
    out = {}
    try:
        with open(os.path.join(PROC, "diskstats"), encoding="ascii") as fh:
            for line in fh:
                f = line.split()
                if len(f) >= 13 and f[12].isdigit():
                    out[f[2]] = int(f[12])
    except OSError:
        pass
    return out


def module_param(name: str) -> str | None:
    try:
        with open(os.path.join(SYS, "module/zfs/parameters", name), encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _unescape(field: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as octal.
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def mounts() -> list[dict]:
    try:
        with open(os.path.join(PROC, "self/mountinfo"), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        left, _, right = line.partition(" - ")
        lf, rf = left.split(), right.split()
        if len(lf) < 5 or len(rf) < 2:
            continue
        out.append({"root": _unescape(lf[3]), "mountpoint": _unescape(lf[4]), "fstype": rf[0], "source": _unescape(rf[1])})
    return out


def mount_for(path: str, mount_list: list[dict] | None = None) -> dict | None:
    """The mount that contains ``path``; for ZFS ``source`` is the dataset."""
    path = os.path.abspath(path)
    best = None
    for m in mount_list if mount_list is not None else mounts():
        mp = m["mountpoint"]
        if path == mp or path.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) >= len(best["mountpoint"]):
                best = m
    if best and best["fstype"] == "zfs":
        best = dict(best, dataset=best["source"], pool=best["source"].split("/")[0])
    return best

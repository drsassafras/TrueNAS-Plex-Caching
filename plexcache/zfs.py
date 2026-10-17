"""Reads ZFS state that is visible from inside the container."""

from __future__ import annotations

import os
import re
import stat

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


def boot_time() -> float | None:
    """When the system last booted (epoch seconds), from /proc/stat; None if unreadable."""
    try:
        with open(os.path.join(PROC, "stat"), encoding="ascii") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


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
        out.append({"root": _unescape(lf[3]), "mountpoint": _unescape(lf[4]), "fstype": rf[0], "source": _unescape(rf[1]),
                    "options": lf[5].split(",") if len(lf) > 5 else []})
    return out


def own_container_id() -> str | None:
    """This container's Docker ID, from the files Docker mounts into it (/etc/hostname and
    friends live under /var/lib/docker/containers/<id>/). None outside Docker."""
    try:
        with open(os.path.join(PROC, "self/mountinfo"), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r"/containers/([0-9a-f]{64})/", text)
    return m.group(1) if m else None


PSEUDO_FS = {"proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "mqueue", "cgroup", "cgroup2", "overlay", "securityfs",
             "debugfs", "tracefs", "pstore", "bpf", "hugetlbfs", "configfs", "fusectl", "autofs", "binfmt_misc", "nsfs"}
SYSTEM_DIRS = ("/proc", "/sys", "/dev", "/run", "/tmp", "/etc", "/usr", "/app")


def writable_mounts(mount_list: list[dict] | None = None, exclude: tuple = ("/config",)) -> list[str]:
    """The folders mounted into this container read-write from the host — the media volumes a
    YAML gives a service — leaving out the container's own filesystems, single files (like
    /etc/hosts) and ``exclude``."""
    out = []
    for m in mount_list if mount_list is not None else mounts():
        mp = m["mountpoint"]
        if (m["fstype"] in PSEUDO_FS or "rw" not in (m.get("options") or []) or mp == "/" or mp in exclude
                or any(mp == d or mp.startswith(d + "/") for d in SYSTEM_DIRS) or not os.path.isdir(mp)):
            continue
        out.append(mp)
    return sorted(set(out))


def host_path(path: str, mount_list: list[dict] | None = None) -> str | None:
    """Where a folder in this container is on the TrueNAS host, for a ZFS mount: the dataset's
    mountpoint there (/mnt/<dataset>, TrueNAS's default) plus the folder inside it. None when
    it isn't on ZFS."""
    m = mount_for(path, mount_list)
    if not m or m["fstype"] != "zfs" or m["mountpoint"] == "/":
        return None
    root = "" if m["root"] in ("", "/") else m["root"]
    rest = os.path.abspath(path)[len(m["mountpoint"].rstrip("/")):]
    return "/mnt/" + m["source"] + root + rest


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


def recordsize(mountpoint: str | None) -> int | None:
    """A ZFS dataset's record size, read from the dataset itself: ZFS reports it as the
    filesystem's block size (statfs f_bsize). None if it can't be read."""
    if not mountpoint:
        return None
    try:
        bs = os.statvfs(mountpoint).f_bsize
    except OSError:
        return None
    return bs if bs and bs & (bs - 1) == 0 and bs >= 4096 else None


def block_sizes(paths) -> dict:
    """How the given files are stored: ZFS reports each file's block size as st_blksize.

    Files that fit in one block are stored in one block sized to fit, so they're
    counted apart (``single``): their block size says nothing about the recordsize
    they were written with. The rest are counted by block size."""
    out = {"checked": 0, "single": 0, "sizes": {}}
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        out["checked"] += 1
        if st.st_size <= st.st_blksize:
            out["single"] += 1
        else:
            key = str(st.st_blksize)
            out["sizes"][key] = out["sizes"].get(key, 0) + 1
    return out


def scan_block_sizes(root: str, progress=None, stop=None, examples: int = 5) -> dict:
    """Every file under ``root`` on the same dataset (child datasets are skipped: they
    have their own recordsize), counted like block_sizes(), with a few example paths
    per block size. ``progress(n)`` is called now and then; ``stop()`` ends it early."""
    out = {"checked": 0, "single": 0, "sizes": {}, "examples": {}, "complete": False}
    try:
        dev = os.stat(root).st_dev
    except OSError as e:
        out["error"] = str(e)
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        if stop and stop():
            return out
        keep = []
        for n in dirnames:
            try:
                if not n.startswith(".") and os.lstat(os.path.join(dirpath, n)).st_dev == dev:
                    keep.append(n)
            except OSError:
                continue
        dirnames[:] = keep
        for n in filenames:
            p = os.path.join(dirpath, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            out["checked"] += 1
            if st.st_size <= st.st_blksize:
                out["single"] += 1
            else:
                key = str(st.st_blksize)
                out["sizes"][key] = out["sizes"].get(key, 0) + 1
                ex = out["examples"].setdefault(key, [])
                if len(ex) < examples:
                    ex.append(os.path.relpath(p, root))
            if progress and out["checked"] % 500 == 0:
                progress(out["checked"])
    out["complete"] = True
    return out

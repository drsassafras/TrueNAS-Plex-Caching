#!/usr/bin/env python3
"""Rewrite media files so they use the dataset's current record size.

ZFS only applies a new ``recordsize`` to files written after the change; files
already there keep their old block size until they're copied again. This copies
each such file, one at a time, into a new file in the same folder (which gets
the current record size) and swaps it in place of the old one.

Run it in the TrueNAS shell (it needs root to keep owners and permissions):

    sudo python3 rewrite_blocks.py tank/plexfiles            # look only (the default)
    sudo python3 rewrite_blocks.py tank/plexfiles --apply    # rewrite

You can pass a dataset name or a folder. Safe to stop with Ctrl-C at any time and
run again later: files already rewritten are skipped.

How it avoids disrupting anything:
  * reads and writes at a gentle pace (--mb-per-sec, default 80);
  * skips files that any program has open (someone watching it, Plex reading it);
  * copies with plain reads and writes, so ZFS block cloning can't just reuse the
    old blocks (which would leave the block size unchanged);
  * checks the copy byte for byte before swapping it in (--no-verify to skip);
  * keeps owner, permissions, timestamps and extended attributes (ACLs);
  * swaps with an atomic rename: Plex sees the same path, never a half-written file;
  * leaves hard-linked files alone (a rename would split the link);
  * stops if the dataset is short of free space.

Things to know first:
  * Snapshots keep the old blocks: until snapshots taken before the rewrite
    expire, each rewritten file takes its space twice.
  * Replication sends every rewritten file again in full.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import stat
import subprocess
import sys
import time

CHUNK = 1024 * 1024
OPEN_FILES_TTL = 30  # seconds between re-scans of which files are open
MOUNTINFO = "/proc/self/mountinfo"


def human(n: float) -> str:
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} B"


def bs_label(n: int) -> str:
    return f"{n // (1024 * 1024)}M" if n >= 1024 * 1024 and n % (1024 * 1024) == 0 else f"{n // 1024}K"


def zfs(*args: str) -> str:
    return subprocess.run(["zfs", *args], check=True, capture_output=True, text=True).stdout.strip()


def resolve(target: str) -> tuple[str, str]:
    """(folder, dataset) for a dataset name or a folder."""
    if target.startswith("/"):
        folder = os.path.realpath(target)
        best = ("", "")
        try:
            with open(MOUNTINFO, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:  # not Linux (no /proc): nothing here can be on a ZFS dataset we can find
            lines = []
        for line in lines:
            left, _, right = line.partition(" - ")
            mp, (fstype, source) = left.split()[4].replace("\\040", " "), right.split()[:2]
            if fstype == "zfs" and (folder == mp or folder.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best[0]):
                best = (mp, source)
        if not best[1]:
            sys.exit(f"{folder} isn't on a ZFS dataset.")
        return folder, best[1]
    mp = zfs("get", "-H", "-o", "value", "mountpoint", target)
    if not mp.startswith("/"):
        sys.exit(f"{target} isn't mounted (mountpoint={mp}).")
    return mp, target


def open_files() -> set[tuple[int, int]]:
    """(device, inode) of every file any process has open, containers included."""
    out = set()
    if not os.path.isdir("/proc"):
        return out
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                st = os.stat(f"{fd_dir}/{fd}")
            except OSError:
                continue
            out.add((st.st_dev, st.st_ino))
    return out


class Pacer:
    def __init__(self, mb_per_sec: float):
        self.rate = mb_per_sec * 1e6
        self.start = time.monotonic()
        self.done = 0

    def add(self, n: int) -> None:
        self.done += n
        if self.rate > 0:
            ahead = self.done / self.rate - (time.monotonic() - self.start)
            if ahead > 0:
                time.sleep(ahead)


def copy_meta(src_st: os.stat_result, src_fd: int, dst_fd: int) -> None:
    os.fchown(dst_fd, src_st.st_uid, src_st.st_gid)
    os.fchmod(dst_fd, src_st.st_mode & 0o7777)
    for name in (os.listxattr(src_fd) if hasattr(os, "listxattr") else []):
        os.setxattr(dst_fd, name, os.getxattr(src_fd, name))
    os.utime(dst_fd, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))


def same_content(fa: int, fb: int, pacer: Pacer) -> bool:
    ha, hb = hashlib.sha256(), hashlib.sha256()
    os.lseek(fa, 0, os.SEEK_SET)
    os.lseek(fb, 0, os.SEEK_SET)
    while True:
        ca, cb = os.read(fa, CHUNK), os.read(fb, CHUNK)
        ha.update(ca)
        hb.update(cb)
        pacer.add(len(ca) + len(cb))
        if not ca and not cb:
            return ha.digest() == hb.digest()
        if len(ca) != len(cb):
            return False


def same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def rewrite(path: str, st: os.stat_result, dir_st: os.stat_result, pacer: Pacer, verify: bool) -> str:
    """Rewrite one file. Returns "" when done, or why it was skipped.

    Runs as root in folders other people may be able to write to, so everything happens
    through the folder and file found by the scan, opened without following links: if
    either was swapped for something else (say a link to a system file) since then, the
    file is skipped rather than copied.
    """
    folder, name = os.path.split(path)
    tmp = f".{name}.rewrite-tmp"
    try:
        dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        return f"can't open its folder ({e.strerror})"
    src = dst = -1
    created = ok = False
    try:
        if not same_file(os.fstat(dfd), dir_st):
            return "its folder changed since the scan"
        try:
            src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
        except OSError as e:
            return f"can't open it ({e.strerror})"
        now = os.fstat(src)
        if not same_file(now, st) or (now.st_size, now.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            return "it changed since the scan"
        try:
            dst = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            created = True
        except FileExistsError:
            return "a leftover temporary file is in the way (" + os.path.join(folder, tmp) + ")"
        # Plain reads and writes on purpose: cp/cat may use copy_file_range, which ZFS can
        # satisfy by cloning the existing blocks — and then nothing would be rewritten.
        while True:
            chunk = os.read(src, CHUNK)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                view = view[os.write(dst, view):]
            pacer.add(len(chunk))
        os.fsync(dst)
        now = os.fstat(src)
        if (now.st_size, now.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            return "it changed while being copied"
        if os.fstat(dst).st_size != st.st_size:
            return "the copy came out a different size"
        if verify and not same_content(src, dst, pacer):
            return "the copy didn't match the original"
        copy_meta(st, src, dst)
        # Atomic, and within the folder we checked: the path always points at a complete file.
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        ok = True
        return ""
    finally:
        for fd in (src, dst):
            if fd >= 0:
                os.close(fd)
        if created and not ok:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
        os.close(dfd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", help="dataset name (tank/plexfiles) or folder (/mnt/tank/plexfiles/Movies)")
    ap.add_argument("--apply", action="store_true", help="rewrite the files (without it, only reports)")
    ap.add_argument("--block", help="block size to aim for, e.g. 1M (default: the dataset's recordsize)")
    ap.add_argument("--mb-per-sec", type=float, default=80, help="pace of reads plus writes (default 80; 0 = no limit)")
    ap.add_argument("--no-verify", action="store_true", help="skip re-reading both copies to compare them")
    ap.add_argument("--min-free-gb", type=float, default=50, help="stop when free space drops below this (default 50)")
    ap.add_argument("--yes", action="store_true", help="don't ask before starting")
    args = ap.parse_args()

    folder, dataset = resolve(args.target)
    recordsize = int(zfs("get", "-Hp", "-o", "value", "recordsize", dataset))
    if args.block:
        mult = {"K": 1024, "M": 1024 * 1024}.get(args.block[-1].upper(), 1)
        target = int(args.block.rstrip("kKmM")) * mult
        if target > recordsize:
            sys.exit(f"{dataset} has recordsize={bs_label(recordsize)}; set it first: sudo zfs set recordsize={args.block} {dataset}")
    else:
        target = recordsize

    print(f"Dataset {dataset} (recordsize={bs_label(recordsize)}), folder {folder}")
    print("Looking for files written with smaller blocks…", flush=True)
    todo, by_bs, ok_files, total = [], {}, 0, 0
    root_dev = os.stat(folder).st_dev
    for dirpath, dirnames, filenames in os.walk(folder):
        # Stay on this dataset: child datasets have their own recordsize.
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and os.lstat(os.path.join(dirpath, d)).st_dev == root_dev]
        try:
            dir_st = os.lstat(dirpath)
        except OSError:
            continue
        for name in filenames:
            if name.startswith(".") and name.endswith(".rewrite-tmp"):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):  # links and anything else that isn't a plain file
                continue
            total += 1
            # A file that fits in one block is stored in one block sized to fit: nothing to fix.
            if st.st_size <= st.st_blksize or st.st_blksize >= target:
                ok_files += 1
                continue
            by_bs[st.st_blksize] = by_bs.get(st.st_blksize, 0) + 1
            todo.append((path, st, dir_st))
    size = sum(st.st_size for _, st, _ in todo)
    print(f"{total:,} files: {ok_files:,} already fine, {len(todo):,} to rewrite ({human(size)}).")
    for bs, n in sorted(by_bs.items()):
        print(f"  {n:,} written with {bs_label(bs)} blocks")
    if not todo:
        return 0

    snaps = [s for s in zfs("list", "-H", "-t", "snapshot", "-o", "name", "-d", "1", dataset).splitlines() if s]
    if snaps:
        print(f"\nNote: {dataset} has {len(snaps)} snapshot(s). Until those expire, each rewritten file uses its space "
              f"twice — up to {human(size)} more in total.")
    hours = (size * (1 if args.no_verify else 3)) / (args.mb_per_sec * 1e6) / 3600 if args.mb_per_sec else 0
    if hours:
        print(f"At {args.mb_per_sec:g} MB/s this takes about {hours:.1f} hours (it can be stopped and resumed).")
    if not args.apply:
        print("\nNothing was changed. Run again with --apply to rewrite these files.")
        return 0
    if not args.yes and input("\nRewrite them now? [y/N] ").strip().lower() not in ("y", "yes"):
        return 0

    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *a: stop.update(now=True))
    pacer = Pacer(args.mb_per_sec)
    busy, busy_at = set(), None
    done = skipped = 0
    for i, (path, st, dir_st) in enumerate(todo, 1):
        if stop["now"]:
            print("\nStopped. Run again to carry on.")
            break
        free = os.statvfs(folder)
        if free.f_bavail * free.f_frsize < st.st_size + args.min_free_gb * 1e9:
            print(f"\nStopped: less than {args.min_free_gb:g} GB would be left free.")
            break
        if st.st_nlink > 1:
            skipped += 1
            print(f"[{i}/{len(todo)}] skipped (hard-linked): {path}")
            continue
        if busy_at is None or time.monotonic() - busy_at > OPEN_FILES_TTL:  # (the clock may start near 0)
            busy, busy_at = open_files(), time.monotonic()
        if (st.st_dev, st.st_ino) in busy:
            skipped += 1
            print(f"[{i}/{len(todo)}] skipped (in use — run again later): {path}")
            continue
        try:
            why = rewrite(path, st, dir_st, pacer, not args.no_verify)
        except OSError as e:
            why = str(e)
        if why:
            skipped += 1
            print(f"[{i}/{len(todo)}] skipped ({why}): {path}")
        else:
            done += 1
            print(f"[{i}/{len(todo)}] {human(st.st_size):>8}  {path}", flush=True)
    print(f"\nRewrote {done:,} file(s); skipped {skipped:,}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

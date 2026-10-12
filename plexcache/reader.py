"""Reading file data so ZFS caches it, with timing and pacing."""

from __future__ import annotations

import mmap
import os
import random
import threading
import time
from dataclasses import dataclass

MiB = 1024 * 1024
CHUNK = MiB


class Stopped(Exception):
    """Raised when a read is cancelled."""


@dataclass
class ReadResult:
    bytes: int
    seconds: float
    error: str | None = None

    @property
    def mb_per_sec(self) -> float:
        return (self.bytes / MiB) / self.seconds if self.seconds > 0 else float("inf")


class Pacer:
    """Keeps background reads to a share of disk time.

    After a read that took ``t`` seconds we wait ``t * (100 - p) / p``, so the
    reader is busy roughly ``p`` percent of the time.
    """

    def __init__(self, percent: float, stop: threading.Event | None = None, pause=None):
        self.percent = max(1.0, min(100.0, float(percent)))
        self.stop = stop or threading.Event()
        self.pause = pause  # callable -> bool: True while we should hold off

    def check(self) -> None:
        if self.stop.is_set():
            raise Stopped()
        while self.pause and self.pause():
            if self.stop.wait(5):
                raise Stopped()

    def after(self, seconds: float) -> None:
        if self.percent < 100 and seconds > 0:
            if self.stop.wait(seconds * (100 - self.percent) / self.percent):
                raise Stopped()
        self.check()


def read_range(path: str, offset: int, length: int, pacer: Pacer | None = None, chunk: int = CHUNK,
               progress=None) -> ReadResult:
    """Read ``length`` bytes from ``offset`` and throw them away.

    Pacing is applied per chunk group (~32 MiB) so long reads yield to other
    I/O. Errors are reported, never counted as cache hits.
    """
    total, spent = 0, 0.0
    buf = bytearray(chunk)
    view = memoryview(buf)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        return ReadResult(0, 0.0, e.strerror or str(e))
    try:
        try:
            os.posix_fadvise(fd, offset, length, os.POSIX_FADV_SEQUENTIAL)
        except (AttributeError, OSError):
            pass
        pos, end = offset, offset + length
        group_start, group_bytes = time.monotonic(), 0
        while pos < end:
            want = min(chunk, end - pos)
            try:
                n = os.preadv(fd, [view[:want]], pos)
            except OSError as e:
                return ReadResult(total, spent + time.monotonic() - group_start, e.strerror or str(e))
            if n <= 0:
                break
            pos += n
            total += n
            group_bytes += n
            if progress:
                progress(n)
            if pacer and group_bytes >= 32 * MiB:
                took = time.monotonic() - group_start
                spent += took
                pacer.after(took)
                group_start, group_bytes = time.monotonic(), 0
        spent += time.monotonic() - group_start
    finally:
        os.close(fd)
    return ReadResult(total, spent)


def read_head(path: str, nbytes: int) -> ReadResult:
    return read_range(path, 0, nbytes)


def is_cache_hit(result: ReadResult, hdd_mb_per_sec: float) -> bool:
    """A read counts as a hit when it ran at least 4x faster than the disks.

    (Same rule as the original script.) Failed reads are never hits.
    """
    if result.error or result.bytes == 0:
        return False
    return result.mb_per_sec >= 4 * hdd_mb_per_sec


def benchmark_file(path: str, size: int, window: int = 64 * MiB) -> ReadResult:
    """Read a random window of a file, bypassing caches where ZFS allows it.

    Uses O_DIRECT (honoured by OpenZFS 2.3+) with page-aligned buffers and a
    random offset, so even on older versions the data is unlikely to be cached.
    """
    window = max(MiB, min(window, size - (size % 4096)))
    max_off = max(0, size - window)
    offset = (random.randrange(0, max_off + 1) // MiB) * MiB if max_off else 0
    flags = os.O_RDONLY | getattr(os, "O_DIRECT", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return read_range(path, offset, window)
    buf = mmap.mmap(-1, MiB)
    total = 0
    start = time.monotonic()
    try:
        pos = offset
        while total < window:
            try:
                n = os.preadv(fd, [buf], pos)
            except OSError:
                os.close(fd)
                fd = -1
                return read_range(path, offset, window)
            if n <= 0:
                break
            total += n
            pos += n
    finally:
        if fd >= 0:
            os.close(fd)
        buf.close()
    return ReadResult(total, time.monotonic() - start)

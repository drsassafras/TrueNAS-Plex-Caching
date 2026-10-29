"""Reading file data so ZFS caches it, with timing and pacing."""

import mmap
import os
import random
import threading
import time
from collections.abc import Callable
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
    """Keeps background reads gentle, in one of two ways.

    * A share of disk time: after a read that took ``t`` seconds we wait
      ``t * (100 - p) / p``, so the reader is busy roughly ``p`` percent of the time.
    * A speed (``rate``, bytes per second): reads from the disks are spread out so they
      average no more than that. Used for the L2ARC, which ZFS fills at a limited speed
      (l2arc_write_max): reading from the disks faster only wastes the reads, since data
      leaves RAM before it reaches the SSD. Reads that came from cache (faster than
      ``disk_mb_s`` could deliver) aren't held back: there's nothing to copy.
    """

    def __init__(self, percent: float, stop: threading.Event | None = None, pause=None, rate: float | None = None):
        self.percent = max(1.0, min(100.0, float(percent)))
        self.rate = rate if rate and rate > 0 else None
        self.disk_mb_s: float | None = None  # uncached disk speed, to tell cache hits apart
        self.stop = stop or threading.Event()
        self.pause = pause  # callable -> bool: True while we should hold off
        self.before_wait: Callable[[], None] | None = None  # an idle gap is about to start

    def check(self) -> None:
        if self.stop.is_set():
            raise Stopped()
        while self.pause and self.pause():
            if self.stop.wait(5):
                raise Stopped()

    def wait_for(self, seconds: float, nbytes: int = 0) -> float:
        """How long to wait after reading ``nbytes`` in ``seconds``."""
        if self.rate:
            if not nbytes:
                return 0.0
            if self.disk_mb_s and seconds > 0 and nbytes / MiB / seconds >= 4 * self.disk_mb_s:
                return 0.0  # came from cache
            return max(0.0, nbytes / self.rate - seconds)
        if self.percent < 100 and seconds > 0:
            return seconds * (100 - self.percent) / self.percent
        return 0.0

    def after(self, seconds: float, nbytes: int = 0) -> None:
        wait = self.wait_for(seconds, nbytes)
        if wait > 0:
            if self.before_wait:
                self.before_wait()
            if self.stop.wait(wait):
                raise Stopped()
        self.check()


def read_range(path: str, offset: int, length: int, pacer: Pacer | None = None, chunk: int = CHUNK,
               progress=None, on_group=None) -> ReadResult:
    """Read ``length`` bytes from ``offset`` and throw them away.

    Pacing is applied per chunk group (~32 MiB) so long reads yield to other
    I/O. Errors are reported, never counted as cache hits. ``on_group(bytes, seconds)``
    is told how long each group took, so the caller can tell cached parts from disk reads.
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
            if (pacer or on_group) and group_bytes >= 32 * MiB:
                took = time.monotonic() - group_start
                spent += took
                if on_group:
                    on_group(group_bytes, took)
                if pacer:
                    pacer.after(took, group_bytes)
                group_start, group_bytes = time.monotonic(), 0
        took = time.monotonic() - group_start
        spent += took
        if on_group and group_bytes:
            on_group(group_bytes, took)
    finally:
        os.close(fd)
    return ReadResult(total, spent)


def read_head(path: str, nbytes: int) -> ReadResult:
    return read_range(path, 0, nbytes)


# A 1 MiB read this quick came from an SSD or RAM: a hard drive needs longer just to move its
# head (a few ms) and spin to the data (about 4 ms on average at 7200 rpm).
PROBE_HIT_SECONDS = 0.004


def is_probe_hit(result: ReadResult) -> bool:
    """For a single small read at a spot the disks haven't just read: did it come from cache?"""
    return not result.error and result.bytes > 0 and result.seconds <= PROBE_HIT_SECONDS


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

"""When L2ARC-only caching should step aside (SSD Cache → L2ARC Cache Settings).

That's warm-up, and the steps of Prefetch that need an L2ARC (the whole library's lead-ins,
the rest of a season in full...). They're never urgent, so they pause — and later carry on
from the same file — whenever something more important needs the disks or the cache:

* a player reports buffering (and for a few minutes after),
* Now Playing Cache, or a step of Prefetch that doesn't need an L2ARC, is reading,
* a scrub or resilver is running on a media pool,
* the pool's disks are busy with other work (measured in their own idle gaps, so their
  reads don't count).

Caching that works without an L2ARC (Now Playing Cache, the next episode...) never waits.
"""

import re
import time

from . import zfs
from .i18n import M, msg
from .truenas import TrueNASError

BUFFER_HOLD = 180  # seconds to stay paused after a player last reported buffering
SCRUB_EVERY = 60  # seconds between scrub/resilver checks
WHOLE_DISK = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+)$")


def busy_percent(before: dict, after: dict, elapsed_ms: float, disks) -> float | None:
    """Highest share of ``elapsed_ms`` any of ``disks`` spent busy between two readings."""
    if elapsed_ms < 500:
        return None
    names = [d for d in (disks or after) if d in before and d in after]
    if not disks:
        names = [d for d in names if WHOLE_DISK.match(d)]
    if not names:
        return None
    return max(min(100.0, (after[d] - before[d]) / elapsed_ms * 100) for d in names)


class L2arcPauses:
    def __init__(self, app, manual: bool):
        self.app = app
        self.manual = manual  # "Run now": ignores busy disks
        self._mark: tuple[float, dict] | None = None
        self._scrub: str | None = None
        self._scrub_at: float | None = None  # when TrueNAS was last asked
        self.scrub_error: str | None = None

    # Called by the pacer just before an idle gap, so the next reading covers only others' work.
    def mark_idle(self):
        self._mark = (time.monotonic(), zfs.disk_busy_ms())

    def _pool_disks(self) -> list[str]:
        z = self.app.zfs or {}
        pools = {d["pool"] for d in z.get("datasets", []) if d["keys"] and d["pool"]}
        return [disk for name in pools for disk in (z.get("pools", {}).get(name) or {}).get("disks", [])]

    def _disk_busy(self) -> float | None:
        if not self._mark:
            return None
        t0, before = self._mark
        now = time.monotonic()
        after = zfs.disk_busy_ms()
        self._mark = (now, after)  # the next reading starts here (we're idle while paused)
        return busy_percent(before, after, (now - t0) * 1000, self._pool_disks())

    def _scrubbing(self) -> str | None:
        if self._scrub_at is not None and time.monotonic() - self._scrub_at < SCRUB_EVERY:
            return self._scrub
        self._scrub_at = time.monotonic()
        self._scrub = None
        t = self.app.settings.section("truenas")
        pools = {d["pool"] for d in (self.app.zfs or {}).get("datasets", []) if d["keys"] and d["pool"]}
        if not (t["url"] and t["api_key"] and pools):
            return None
        try:
            with self.app.truenas() as tn:
                for name in sorted(pools):
                    scan = (tn.pool(name) or {}).get("scan") or {}
                    if scan.get("state") == "SCANNING":
                        self._scrub = {"SCRUB": M("a scrub is running on {pool}", pool=name),
                                       "RESILVER": M("a resilver is running on {pool}", pool=name)}.get(
                            scan.get("function") or "", M("a scan is running on {pool}", pool=name))
                        break
            self.scrub_error = None
        except (TrueNASError, OSError) as e:
            self.scrub_error = msg(e)  # can't tell, so don't pause for it
        return self._scrub

    def reason(self) -> str | None:
        """Why L2ARC-only caching should wait right now, or None to carry on."""
        w = self.app.settings.section("warmup")
        if w["pause_buffering"]:
            last = self.app.monitor.last_buffering
            if last and time.time() - last[0] < BUFFER_HOLD:
                return M("a player was buffering ({player})", player=last[1])
        if w["pause_preload"]:
            what = self.app.monitor.preloading(foreground_only=True)
            if what:
                return what
        if w["pause_scrub"]:
            what = self._scrubbing()
            if what:
                return what
        busy = self._disk_busy()
        if w["pause_disk_busy"] and not self.manual and busy is not None and busy >= w["pause_disk_busy"]:
            return M("the pool’s disks are {pct}% busy with other work", pct=round(busy))
        return None


WarmupPauses = L2arcPauses  # its name before Prefetch's L2ARC-only steps used it too

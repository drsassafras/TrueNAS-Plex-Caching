"""When the next library warm-up is due."""

from __future__ import annotations

import datetime as dt


def next_run(w: dict, now: dt.datetime, last_run: dt.datetime | None) -> dt.datetime | None:
    """Next scheduled warm-up time (local time), or None for manual only."""
    sched = w["schedule"]
    if sched == "manual":
        return None
    if sched == "hours":
        step = dt.timedelta(hours=int(w["every_hours"]))
        if last_run is None:
            return now
        return max(now, last_run + step)
    hh, mm = (int(x) for x in w["time"].split(":"))
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if sched == "daily":
        if candidate <= now:
            candidate += dt.timedelta(days=1)
    elif sched == "weekly":
        days = (int(w["weekday"]) - candidate.weekday()) % 7
        candidate += dt.timedelta(days=days)
        if candidate <= now:
            candidate += dt.timedelta(days=7)
    return candidate


def due(w: dict, now: dt.datetime, last_run: dt.datetime | None, last_check: dt.datetime) -> bool:
    """True if a scheduled time fell between the last check and now."""
    if w["schedule"] == "manual":
        return False
    if w["schedule"] == "hours":
        return last_run is None or now - last_run >= dt.timedelta(hours=int(w["every_hours"]))
    nxt = next_run(w, last_check, None)
    return nxt is not None and last_check < nxt <= now

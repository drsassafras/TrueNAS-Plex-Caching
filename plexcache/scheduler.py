"""When library warm-ups are due.

Each library has its own schedule. Every one but "none" warms up at boot; on top of that
"twice_monthly" runs on the 1st and 15th, "weekly" on the chosen weekday, "twice_weekly" on
that weekday and three days later, and "daily" every day. The time of day and the weekday
are shared by every library.
"""

import datetime as dt

TIMED = ("daily", "twice_weekly", "weekly", "twice_monthly")


def run_days(sched: str, weekday: int) -> set[int] | None:
    """The weekdays a weekly kind of schedule runs on (0 = Monday)."""
    if sched == "weekly":
        return {weekday % 7}
    if sched == "twice_weekly":
        return {weekday % 7, (weekday + 3) % 7}
    return None


def next_run(w: dict, now: dt.datetime, last_run: dt.datetime | None = None, schedule: str | None = None) -> dt.datetime | None:
    """Next time a ``schedule`` warm-up starts (local time), or None if it only runs at boot."""
    sched = schedule or w.get("schedule", "none")
    if sched not in TIMED:
        return None
    hh, mm = (int(x) for x in w["time"].split(":"))
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    days = run_days(sched, int(w["weekday"]))
    for i in range(40):
        cand = start + dt.timedelta(days=i)
        if cand <= now:
            continue
        if sched == "daily" or (days is not None and cand.weekday() in days) or (days is None and cand.day in (1, 15)):
            return cand
    return None


def due(w: dict, now: dt.datetime, last_run: dt.datetime | None, last_check: dt.datetime, schedule: str | None = None) -> bool:
    """True if a ``schedule`` start time fell between the last check and now."""
    nxt = next_run(w, last_check, None, schedule)
    return nxt is not None and last_check < nxt <= now

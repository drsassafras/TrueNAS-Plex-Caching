"""The Activity report: is caching making playback better, and if not, why.

Built from the measurements the playback monitor records (see Activity.record):
streams, skips, next-episode readiness, still-cached checks ("retention"), how
much was read and from where, RAM-limit hits, and how busy the disks were while
people watched. Buffering isn't reported: most of it comes from the network or
the transcoder, which caching can't help.

Raw measurements are kept for a month; older ones are folded into one summary
per day (``summarize``), so a year of history stays small. Every report and
chart bar is a summary too, so both kinds add up the same way.
"""

from __future__ import annotations

import bisect
import collections
import datetime as dt
import time

RETENTION_LOW = 70  # % still cached below which preloads are being pushed out too soon
DISK_BUSY_HIGH = 85  # % busy (peak during a stream) worth a look

PERIODS = {  # name -> (seconds back, bucket size, bucket count)
    "today": None,  # since midnight, by hour
    "week": 7,
    "month": 30,
    "year": 365,
}

COUNTERS = ("streams", "skips", "skips_cached", "next", "next_ready", "lead_sum", "lead_n",
            "ret_sum", "ret_n", "read_cached", "read_disk", "busy_sum", "busy_n", "limited", "no_room")


def empty() -> dict:
    out = {k: 0 for k in COUNTERS}
    out.update(busy_peak=None, miss={}, late=[])
    return out


def add(a: dict, b: dict) -> dict:
    """Add summary ``b`` into ``a`` (in place) and return ``a``."""
    for k in COUNTERS:
        a[k] += b.get(k, 0)
    if b.get("busy_peak") is not None:
        a["busy_peak"] = b["busy_peak"] if a["busy_peak"] is None else max(a["busy_peak"], b["busy_peak"])
    miss = collections.Counter(a["miss"])
    miss.update(b.get("miss") or {})
    a["miss"] = dict(miss)
    a["late"] = (a["late"] + list(b.get("late") or []))[-10:]
    return a


def summarize(rows: list[dict]) -> dict:
    """One summary for a list of raw measurements."""
    s = empty()
    miss = collections.Counter()
    for r in rows:
        w = r["what"]
        if w == "stream":
            s["streams"] += 1
        elif w == "skip":
            s["skips"] += 1
            if r["cached"]:
                s["skips_cached"] += 1
            else:
                miss[r.get("reason") or "unknown"] += 1
        elif w == "next":
            s["next"] += 1
            if r["ready"]:
                s["next_ready"] += 1
                if r.get("lead") is not None:
                    s["lead_sum"] += r["lead"]
                    s["lead_n"] += 1
            else:
                s["late"].append(f"{r['label']} ({r.get('pct', 0)}% loaded)")
        elif w == "retention":
            s["ret_sum"] += r["pct"]
            s["ret_n"] += 1
        elif w == "read":
            s["read_cached"] += r.get("cached", 0)
            s["read_disk"] += r.get("disk", 0)
        elif w == "streamend":
            if r.get("busy_avg") is not None:
                s["busy_sum"] += r["busy_avg"]
                s["busy_n"] += 1
                s["busy_peak"] = r["busy_peak"] if s["busy_peak"] is None else max(s["busy_peak"], r["busy_peak"])
        elif w == "limit":
            if r.get("pct", 0) > 0:
                s["limited"] += 1
            else:
                s["no_room"] += 1
    s["miss"] = dict(miss)
    s["late"] = s["late"][-10:]
    return s


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


# ------------------------------------------------------------------ buckets
def _buckets(period: str, now: float) -> list[tuple[float, float, str]]:
    """[(start, end, label), ...] oldest first, for the chart under a period."""
    t = dt.datetime.fromtimestamp(now)
    if period == "today":
        midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
        return [((midnight + dt.timedelta(hours=h)).timestamp(), (midnight + dt.timedelta(hours=h + 1)).timestamp(),
                 f"{h:02d}:00") for h in range(t.hour + 1)]
    if period in ("week", "month"):
        days = PERIODS[period]
        today = t.replace(hour=0, minute=0, second=0, microsecond=0)
        out = []
        for i in range(days - 1, -1, -1):
            d = today - dt.timedelta(days=i)
            out.append((d.timestamp(), (d + dt.timedelta(days=1)).timestamp(),
                        d.strftime("%a") if period == "week" else d.strftime("%b %-d")))
        return out
    # year: by month, the last 12 including this one
    first = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(11, -1, -1):
        y, m = first.year, first.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        start = dt.datetime(y, m, 1)
        end = dt.datetime(y + (m == 12), m % 12 + 1, 1)
        out.append((start.timestamp(), end.timestamp(), start.strftime("%b")))
    return out


def _sum_between(raw: list[dict], daily: dict, start: float, end: float) -> dict:
    # raw is in time order (appended as things happen), so slice it rather than scan it
    times = [r["t"] for r in raw] if not hasattr(raw, "times") else raw.times
    s = summarize(raw[bisect.bisect_left(times, start):bisect.bisect_left(times, end)])
    for day, summ in daily.items():
        ts = dt.datetime.strptime(day, "%Y-%m-%d").timestamp()
        if start <= ts < end:
            add(s, summ)
    return s


def _point(s: dict) -> dict:
    """What one chart bar shows."""
    return {"skips_cached": s["skips_cached"], "skips_missed": s["skips"] - s["skips_cached"],
            "next_ready": s["next_ready"], "next_late": s["next"] - s["next_ready"],
            "retention": round(s["ret_sum"] / s["ret_n"]) if s["ret_n"] else None,
            "preloaded": s["read_cached"] + s["read_disk"],
            "busy": round(s["busy_sum"] / s["busy_n"]) if s["busy_n"] else None,
            "streams": s["streams"]}


class _Timed(list):
    """A time-ordered list that remembers its timestamps, for quick slicing."""

    def __init__(self, rows):
        super().__init__(rows)
        self.times = [r["t"] for r in rows]


# ------------------------------------------------------------------- report
def build(app, period: str = "week", now: float | None = None) -> dict:
    now = time.time() if now is None else now
    raw, daily = app.activity.history()
    raw = _Timed(sorted(raw, key=lambda r: r["t"]))
    buckets = _buckets(period, now)
    start = buckets[0][0]
    s = _sum_between(raw, daily, start, now + 1)
    series = []
    for b_start, b_end, label in buckets:
        p = _point(_sum_between(raw, daily, b_start, b_end))
        p["label"] = label
        series.append(p)
    span = {"today": "Today", "week": "Last 7 days", "month": "Last 30 days", "year": "Last 12 months"}[period]

    busy_avg = round(s["busy_sum"] / s["busy_n"]) if s["busy_n"] else None
    busy_peak = round(s["busy_peak"]) if s["busy_peak"] is not None else None
    ret_avg = round(s["ret_sum"] / s["ret_n"]) if s["ret_n"] else None
    tiles = {
        "skips": {"cached": s["skips_cached"], "total": s["skips"]},
        "next": {"ready": s["next_ready"], "total": s["next"],
                 "lead_min": round(s["lead_sum"] / s["lead_n"] / 60) if s["lead_n"] else None},
        "retention": {"pct": ret_avg, "checks": s["ret_n"]},
        "preloaded": {"bytes": s["read_cached"] + s["read_disk"],
                      "cached_pct": round(100 * s["read_cached"] / (s["read_cached"] + s["read_disk"]))
                      if s["read_cached"] + s["read_disk"] else None},
    }

    attention = []
    for reason, n in sorted(s["miss"].items(), key=lambda kv: -kv[1]):
        if reason == "only part of the file fits in the RAM limit":
            attention.append({"status": "warn", "title": f"{_plural(n, 'skip')} went to disk: RAM limit full",
                              "detail": "Only part of those files fit in Now-playing's RAM limit, and the viewer skipped "
                                        "past the part that was loaded.",
                              "fix": "Raise Now-playing's RAM limit (Features), or lower “ZFS cache to leave for the "
                                     "rest of the NAS” (Settings → Behaviour).", "link": "/features"})
        elif reason == "the preload hadn't started yet":
            attention.append({"status": "warn", "title": f"{_plural(n, 'skip')} before the preload started",
                              "detail": "The viewer skipped while Now-playing was still waiting to start.",
                              "fix": "Use the automatic start time, or a shorter fixed delay (Features → Now-playing).",
                              "link": "/features"})
        elif reason == "the preload hadn't reached that part yet":
            attention.append({"status": "info", "title": f"{_plural(n, 'skip')} ahead of the preload",
                              "detail": "The viewer skipped further than Now-playing had read so far. Normal in the first "
                                        "minute or two; if it's common, the disks may be busy with other work.",
                              "fix": "", "link": None})
        elif reason == "Now-playing preload is off":
            attention.append({"status": "info", "title": f"{_plural(n, 'skip')} without Now-playing",
                              "detail": "Now-playing preload is off, so skips read from the disks.",
                              "fix": "Turn it on in Features.", "link": "/features"})
    late = s["next"] - s["next_ready"]
    if late:
        examples = "; ".join(s["late"][-3:])
        attention.append({"status": "warn", "title": f"{_plural(late, 'next episode')} not ready in time",
                          "detail": (examples + ".") if examples else "They hadn't finished loading when they started.",
                          "fix": "Start Next-episode earlier: “After Now-playing preload finishes” or “At 80%” (Features).",
                          "link": "/features"})
    if ret_avg is not None and ret_avg < RETENTION_LOW:
        attention.append({"status": "warn", "title": f"Preloads aren't staying in RAM ({ret_avg}% still cached)",
                          "detail": "About 30 minutes after loading, much of the data had already been pushed out of "
                                    "the ZFS cache by other work.",
                          "fix": "Lower Now-playing's and Next-episode's RAM limits so preloads don't push each other out, "
                                 "leave more for the rest of the NAS, or add an L2ARC (SSD cache).", "link": "/features"})
    if busy_peak is not None and busy_peak >= DISK_BUSY_HIGH:
        attention.append({"status": "info", "title": f"Disks reached {busy_peak}% busy while people watched",
                          "detail": f"On average they were {busy_avg}% busy during streams.",
                          "fix": "There's little headroom left for skips that go to disk. Check what else uses the pool; "
                                 "Library warm-up pauses for busy disks (Settings → Library warm-up pauses).",
                          "link": "/settings#warmup-pauses"})

    has_data = any(s[k] for k in ("streams", "skips", "next", "ret_n", "read_cached", "read_disk"))
    if not has_data:
        verdict = {"status": "info", "text": f"{span}: nothing has played yet, so there's nothing to report."}
    else:
        bits = []
        if s["skips"]:
            bits.append(f"{s['skips_cached']} of {s['skips']} skips")
        if s["next"]:
            bits.append(f"{s['next_ready']} of {s['next']} next episodes")
        warns = sum(a["status"] == "warn" for a in attention)
        if warns:
            verdict = {"status": "warn", "text": f"{span}: {_plural(warns, 'thing')} could be better — see below."}
        else:
            tail = f": {' and '.join(bits)} came from cache" if bits else ", and caching had nothing to fix"
            verdict = {"status": "ok", "text": f"{span}, {_plural(s['streams'], 'stream')}{tail}."}

    ram = app.ram_budget()
    return {
        "period": period, "span": span, "streams": s["streams"], "verdict": verdict, "tiles": tiles,
        "attention": attention, "series": series,
        "disk": {"avg": busy_avg, "peak": busy_peak, "streams": s["busy_n"]},
        "ram": {"nowplaying": {"limit": ram["nowplaying"], "in_use": ram["in_use"]["nowplaying"]},
                "nextepisode": {"limit": ram["nextepisode"], "in_use": ram["in_use"]["nextepisode"]},
                "limited": s["limited"], "no_room": s["no_room"]},
        "misses": s["miss"],
    }


def build_all(app, now: float | None = None) -> dict:
    return {p: build(app, p, now) for p in PERIODS}

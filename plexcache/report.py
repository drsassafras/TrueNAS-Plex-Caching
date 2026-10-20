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

from .i18n import M

RETENTION_LOW = 70  # % still cached below which preloads are being pushed out too soon
DISK_BUSY_HIGH = 85  # % busy (peak during a stream) worth a look

PERIODS = {  # name -> (seconds back, bucket size, bucket count)
    "today": None,  # since midnight, by hour
    "week": 7,
    "month": 30,
    "year": 365,
}

COUNTERS = ("streams", "skips", "skips_cached", "next", "next_ready", "next_full", "lead_sum", "lead_n",
            "ret_sum", "ret_n", "read_cached", "read_disk", "read_ssd", "busy_sum", "busy_n", "limited", "no_room",
            # Expected from cache: each skip, next episode and stream start weighted by how likely it
            # really came from cache (how long ago it was cached, how long data lasts in its library)
            "skips_p", "next_p", "lead_p", "lead_pn")


def empty() -> dict:
    out = {k: 0 for k in COUNTERS}
    out.update(busy_peak=None, miss={}, late=[], skips_by={})  # skips_by: library key -> [skips, expected from cache]
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
    by = a.setdefault("skips_by", {})
    for k, (n, p) in (b.get("skips_by") or {}).items():
        mine = by.setdefault(k, [0, 0.0])
        mine[0] += n
        mine[1] += p
    return a


def summarize(rows: list[dict]) -> dict:
    """One summary for a list of raw measurements."""
    s = empty()
    miss = collections.Counter()
    for r in rows:
        w = r["what"]
        if w == "stream":
            s["streams"] += 1
            if r.get("lead_p") is not None:
                s["lead_p"] += r["lead_p"]
                s["lead_pn"] += 1
        elif w == "skip":
            s["skips"] += 1
            p = r.get("p", 1.0 if r["cached"] else 0.0)  # (older history: cached or not)
            s["skips_p"] += p
            if "lib" in r:  # (older history doesn't say which library)
                mine = s["skips_by"].setdefault(r["lib"], [0, 0.0])
                mine[0] += 1
                mine[1] += p
            if r["cached"]:
                s["skips_cached"] += 1
            else:
                miss[r.get("reason") or "unknown"] += 1
        elif w == "next":
            s["next"] += 1
            s["next_p"] += r.get("p", 1.0 if r["ready"] else 0.0)
            if r.get("full", r["ready"]):  # (older history: ready meant loaded in full)
                s["next_full"] += 1
            if r["ready"]:
                s["next_ready"] += 1
                if r.get("lead") is not None:
                    s["lead_sum"] += r["lead"]
                    s["lead_n"] += 1
            else:
                s["late"].append({"label": r["label"], "pct": r.get("pct", 0)})
        elif w == "retention":
            s["ret_sum"] += r["pct"]
            s["ret_n"] += 1
        elif w == "read":
            if r.get("route") == "ssd":  # on its way to the SSD, not read for a viewer
                s["read_ssd"] += r.get("cached", 0) + r.get("disk", 0)
            else:
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
            "skips": s["skips"], "skips_p": round(s["skips_p"], 2),
            "skips_by": {k: [n, round(p, 2)] for k, (n, p) in (s.get("skips_by") or {}).items()},
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


def fully_cached_tile(app, start: float, now: float) -> dict:
    """Fully cached age over the period, every library: the oldest re-read still cached before
    the first one that wasn't (cachelog.fully_cached_age)."""
    route = "ssd" if app.cache_mode() == "l2arc" else "ram"
    fc = app.cachelog.fully_cached(route, None, start, now + 1)
    return dict(fc or {}, route=route)


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
        p["start"] = b_start  # each browser writes its own label from this
        series.append(p)
    span = {"today": M("Today"), "week": M("Last 7 days"), "month": M("Last 30 days"), "year": M("Last 12 months")}[period]

    busy_avg = round(s["busy_sum"] / s["busy_n"]) if s["busy_n"] else None
    busy_peak = round(s["busy_peak"]) if s["busy_peak"] is not None else None
    ret_avg = round(s["ret_sum"] / s["ret_n"]) if s["ret_n"] else None
    tiles = {
        "skips": {"cached": s["skips_cached"], "total": s["skips"], "expected": round(s["skips_p"], 1),
                  # per library, in Plex's order (older history doesn't say which library: only in the total)
                  "by_lib": [{"key": lib["key"], "name": lib.get("title", ""), "total": (s["skips_by"].get(lib["key"]) or [0, 0])[0],
                              "expected": round((s["skips_by"].get(lib["key"]) or [0, 0])[1], 1)} for lib in getattr(app, "libraries", [])]},
        "next": {"ready": s["next_ready"], "full": s["next_full"], "total": s["next"], "expected": round(s["next_p"], 1),
                 "lead_min": round(s["lead_sum"] / s["lead_n"] / 60) if s["lead_n"] else None},
        "retention": {"pct": ret_avg, "checks": s["ret_n"]},
        "lead": {"pct": round(100 * s["lead_p"] / s["lead_pn"]) if s["lead_pn"] else None, "streams": s["lead_pn"]},
        "fully": fully_cached_tile(app, start, now),
        "preloaded": {"bytes": s["read_cached"] + s["read_disk"],
                      "cached_pct": round(100 * s["read_cached"] / (s["read_cached"] + s["read_disk"]))
                      if s["read_cached"] + s["read_disk"] else None,
                      "ssd_bytes": s["read_ssd"]},
    }

    attention = []
    for reason, n in sorted(s["miss"].items(), key=lambda kv: -kv[1]):
        if reason == "only part of the file fits in the RAM limit":
            attention.append({"status": "warn", "title": M("{n} skips went to disk: RAM limit full", one="{n} skip went to disk: RAM limit full", n=n),
                              "detail": M("Only part of those files fit in Now Playing Cache’s RAM limit, and the viewer skipped "
                                          "past the part that was loaded."),
                              "fix": M("Raise Now Playing Cache’s RAM limit, or lower “Reserve ZFS cache” "
                                       "(both in Caching → Cache Settings)."), "link": "/caching#arc"})
        elif reason == "the preload hadn't started yet":
            attention.append({"status": "warn", "title": M("{n} skips before Now Playing Cache started", one="{n} skip before Now Playing Cache started", n=n),
                              "detail": M("The viewer skipped while Now Playing Cache was still waiting to start."),
                              "fix": M("Use the automatic start time, or a shorter fixed delay (Caching → Now Playing Cache)."),
                              "link": "/caching"})
        elif reason == "the preload hadn't reached that part yet":
            attention.append({"status": "info", "title": M("{n} skips ahead of Now Playing Cache", one="{n} skip ahead of Now Playing Cache", n=n),
                              "detail": M("The viewer skipped further than Now Playing Cache had read so far. Normal in the first "
                                          "minute or two; if it’s common, the disks may be busy with other work."),
                              "fix": "", "link": None})
        elif reason == "Now-playing preload is off":
            attention.append({"status": "info", "title": M("{n} skips without Now Playing Cache", one="{n} skip without Now Playing Cache", n=n),
                              "detail": M("Now Playing Cache is off, so skips read from the disks."),
                              "fix": M("Turn it on in Caching."), "link": "/caching"})
    if ret_avg is not None and ret_avg < RETENTION_LOW:
        attention.append({"status": "warn", "title": M("Cached data isn’t staying in RAM ({pct}% still cached)", pct=ret_avg),
                          "detail": M("About 30 minutes after loading, much of the data had already been pushed out of "
                                      "the ZFS cache by other work."),
                          "fix": M("Lower Now Playing Cache’s and Prefetch’s RAM limits so they don’t push each other out, "
                                   "leave more for the rest of the NAS (Caching → Cache Settings), or add an L2ARC (SSD cache)."), "link": "/caching#arc"})
    if busy_peak is not None and busy_peak >= DISK_BUSY_HIGH:
        attention.append({"status": "info", "title": M("Disks reached {pct}% busy while people watched", pct=busy_peak),
                          "detail": M("On average they were {pct}% busy during streams.", pct=busy_avg),
                          "fix": M("There’s little headroom left for skips that go to disk. Check what else uses the pool; "
                                   "L2ARC-only caching pauses for busy disks (SSD Cache → L2ARC Cache Settings)."),
                          "link": "/ssd#l2arc-settings"})

    has_data = any(s[k] for k in ("streams", "skips", "next", "ret_n", "read_cached", "read_disk", "read_ssd"))
    if not has_data:
        verdict = {"status": "info", "text": M("{period}: nothing has played yet, so there’s nothing to report.", period=span)}
    else:
        # (the figures are in the tiles right under it: the verdict doesn't repeat them)
        warns = sum(a["status"] == "warn" for a in attention)
        if warns:
            verdict = {"status": "warn", "text": M("{period}: {n} things could be better — see below.",
                                                   one="{period}: {n} thing could be better — see below.", period=span, n=warns)}
        else:
            verdict = {"status": "ok", "text": M("{period}, {streams}: nothing to fix.", period=span,
                                                 streams=M("{n} streams", one="{n} stream", n=s["streams"]))}

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

"""Event log and run history shown on the Activity page."""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time

log = logging.getLogger("plexcache")

RAW_DAYS = 31  # measurements older than this are folded into one summary per day
DAILY_DAYS = 400  # daily summaries kept (a year, with room)
MAX_METRICS = 20000


TOTALS = ("streams", "skips", "skips_cached", "next", "next_ready", "preloaded_bytes")


def _count_total(totals: dict, what: str, f: dict) -> None:
    if what == "stream":
        totals["streams"] += 1
    elif what == "skip":
        totals["skips"] += 1
        totals["skips_cached"] += 1 if f.get("cached") else 0
    elif what == "next":
        totals["next"] += 1
        totals["next_ready"] += 1 if f.get("ready") else 0
    elif what == "read":
        totals["preloaded_bytes"] += int(f.get("cached", 0) or 0) + int(f.get("disk", 0) or 0)


class Activity:
    def __init__(self, config_dir: str, max_events: int = 300, max_runs: int = 60):
        self.path = os.path.join(config_dir, "history.json")
        self._lock = threading.Lock()
        self.events: collections.deque = collections.deque(maxlen=max_events)
        self.runs: list[dict] = []
        self.counters: list[dict] = []  # {"t", "kind"} for prefetch counts
        # What happened while people watched (streams, skips, buffering, ...), for the Activity report.
        self.metrics: list[dict] = []
        self.daily: dict[str, dict] = {}  # "2026-10-13" -> summary of that day (older than RAW_DAYS)
        # Running totals that only ever go up (for Home Assistant's long-term statistics).
        self.totals: dict[str, int] = dict.fromkeys(TOTALS, 0)
        self.max_runs = max_runs
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.runs = data.get("runs", [])[-max_runs:]
            self.counters = data.get("counters", [])
            self.metrics = data.get("metrics", [])
            self.daily = data.get("daily", {})
            if "totals" in data:
                self.totals.update({k: int(v) for k, v in data["totals"].items() if k in TOTALS})
            else:  # history from before totals were kept: start from what's still there
                for m in self.metrics:
                    _count_total(self.totals, m.get("what"), m)
            for e in data.get("events", [])[-max_events:]:
                self.events.append(e)
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        week_ago = time.time() - 7 * 86400
        self.counters = [c for c in self.counters if c["t"] >= week_ago]
        self._roll_up()
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"runs": self.runs, "counters": self.counters, "metrics": self.metrics,
                           "daily": self.daily, "totals": self.totals, "events": list(self.events)[-100:]}, fh)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save history: %s", e)

    def event(self, kind: str, message: str, level: str = "info") -> None:
        log.log(logging.WARNING if level == "warn" else logging.INFO, "%s: %s", kind, message)
        with self._lock:
            self.events.append({"t": time.time(), "kind": kind, "message": message, "level": level})

    def count(self, kind: str) -> None:
        with self._lock:
            self.counters.append({"t": time.time(), "kind": kind})
            self._save()

    def record(self, what: str, **fields) -> None:
        """Keep one measurement, e.g. record("skip", label="Dune", cached=True)."""
        with self._lock:
            self.metrics.append(dict(fields, t=time.time(), what=what))
            _count_total(self.totals, what, fields)
            self._save()

    def since(self, seconds: float) -> list[dict]:
        cutoff = time.time() - seconds
        with self._lock:
            return [m for m in self.metrics if m["t"] >= cutoff]

    def history(self) -> tuple[list[dict], dict]:
        """(raw measurements of the last month, daily summaries before that)."""
        with self._lock:
            return list(self.metrics), dict(self.daily)

    def _roll_up(self) -> None:
        """Fold measurements older than RAW_DAYS into per-day summaries, so a year stays small."""
        from .report import add, summarize
        cutoff = time.time() - RAW_DAYS * 86400
        old = [m for m in self.metrics if m["t"] < cutoff]
        if old:
            by_day: dict[str, list] = collections.defaultdict(list)
            for m in old:
                by_day[time.strftime("%Y-%m-%d", time.localtime(m["t"]))].append(m)
            for day, rows in by_day.items():
                self.daily[day] = add(self.daily.get(day) or summarize([]), summarize(rows))
            self.metrics = [m for m in self.metrics if m["t"] >= cutoff]
        self.metrics = self.metrics[-MAX_METRICS:]
        oldest = time.strftime("%Y-%m-%d", time.localtime(time.time() - DAILY_DAYS * 86400))
        self.daily = {d: v for d, v in self.daily.items() if d >= oldest}

    def add_run(self, run: dict) -> None:
        with self._lock:
            self.runs.append(run)
            self.runs = self.runs[-self.max_runs:]
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            week_ago = time.time() - 7 * 86400
            counts = collections.Counter(c["kind"] for c in self.counters if c["t"] >= week_ago)
            return {
                "events": list(reversed(self.events))[:100],
                "runs": list(reversed(self.runs)),
                "week": dict(counts),
            }

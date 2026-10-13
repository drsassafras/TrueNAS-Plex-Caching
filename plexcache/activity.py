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
        self.max_runs = max_runs
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.runs = data.get("runs", [])[-max_runs:]
            self.counters = data.get("counters", [])
            self.metrics = data.get("metrics", [])
            self.daily = data.get("daily", {})
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
                           "daily": self.daily, "events": list(self.events)[-100:]}, fh)
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

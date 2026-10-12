"""Event log and run history shown on the Activity page."""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time

log = logging.getLogger("plexcache")


class Activity:
    def __init__(self, config_dir: str, max_events: int = 300, max_runs: int = 60):
        self.path = os.path.join(config_dir, "history.json")
        self._lock = threading.Lock()
        self.events: collections.deque = collections.deque(maxlen=max_events)
        self.runs: list[dict] = []
        self.counters: list[dict] = []  # {"t", "kind"} for prefetch counts
        self.max_runs = max_runs
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.runs = data.get("runs", [])[-max_runs:]
            self.counters = data.get("counters", [])
            for e in data.get("events", [])[-max_events:]:
                self.events.append(e)
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        week_ago = time.time() - 7 * 86400
        self.counters = [c for c in self.counters if c["t"] >= week_ago]
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"runs": self.runs, "counters": self.counters, "events": list(self.events)[-100:]}, fh)
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

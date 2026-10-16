"""How much of each library has been cached, hour by hour.

Once an hour, the cache log (cachelog.py) and the last library scan give, for every library,
the share of its files — and of its bytes — this app cached within the last hour, 24 hours,
7 days, 30 days and 6 months (in RAM or on the SSD; whether it's still there isn't checked).
Only that result is kept, for 6 months: the cache log keeps just the latest time each file was
cached, so the past couldn't be worked out again later.

Each hour is {"t": start of the hour, "libs": {key: [files cached within each span (5), bytes
cached within each span (5), files in the library, bytes in the library]}}.
"""

from __future__ import annotations

import json
import os
import threading
import time

HOUR = 3600
SPANS = (HOUR, 24 * HOUR, 7 * 24 * HOUR, 30 * 24 * HOUR, 180 * 24 * HOUR)
KEEP = 180 * 24 * HOUR


class Coverage:
    def __init__(self, path: str | None = None):
        self.path = path
        self.hours: list[dict] = []
        self._lock = threading.Lock()
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    self.hours = [h for h in json.load(fh).get("hours", []) if isinstance(h, dict) and "t" in h]
            except (OSError, ValueError, AttributeError):
                pass

    def due(self, now: float | None = None) -> bool:
        """Is this hour still to be recorded?"""
        now = time.time() if now is None else now
        return not self.hours or self.hours[-1]["t"] < now - now % HOUR

    def record(self, files, written: dict, now: float | None = None) -> dict:
        """Record this hour. ``files``: the library scan ((path, size, library key) for every
        file); ``written``: path -> when it was last cached (its lead-in or all of it)."""
        now = time.time() if now is None else now
        libs: dict[str, list] = {}
        for path, size, key in files:
            row = libs.setdefault(str(key), [0] * 5 + [0] * 5 + [0, 0])
            row[10] += 1
            row[11] += size or 0
            t = written.get(path)
            if not t:
                continue
            age = now - t
            for i, span in enumerate(SPANS):
                if age < span:
                    row[i] += 1
                    row[5 + i] += size or 0
        hour = {"t": now - now % HOUR, "libs": libs}
        with self._lock:
            if self.hours and self.hours[-1]["t"] == hour["t"]:
                self.hours[-1] = hour
            else:
                self.hours.append(hour)
            self.hours = [h for h in self.hours if h["t"] >= now - KEEP]
        self.save()
        return hour

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = {"hours": list(self.hours)}
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass

    def series(self, names: dict) -> dict:
        """For the page: per library (in ``names``' order) and every library together, each
        hour's share cached within each span, in tenths of a percent — [[files ×5], [bytes ×5]]
        (None for an hour before the library was scanned)."""
        with self._lock:
            hours = list(self.hours)

        def share(rows):
            nf = sum(r[10] for r in rows)
            nb = sum(r[11] for r in rows)
            if not nf:
                return None
            return [[round(1000 * sum(r[i] for r in rows) / nf) for i in range(5)],
                    [round(1000 * sum(r[5 + i] for r in rows) / nb) if nb else 0 for i in range(5)]]
        out = {"spans": list(SPANS), "hours": [h["t"] for h in hours], "names": [[k, v] for k, v in names.items()],
               "all": [share(list(h["libs"].values())) for h in hours], "libs": {}}
        for key in names:
            out["libs"][key] = [share([h["libs"][key]]) if key in h["libs"] else None for h in hours]
        return out

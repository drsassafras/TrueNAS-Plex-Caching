"""What's been cached, when, and how long it lasted.

Two jobs, kept apart:

* **Policy** — the cooldown: nothing is read again within it (a lead-in, or a whole file).
* **Measurement** — every time something is read again, the read is timed, so we learn
  whether what was cached N hours earlier was still there. Added up by age, that's how long
  cached data really lasts on this NAS: one curve for RAM, one for the SSD (L2ARC). The
  Activity page shows it, and suggests cooldowns from it.

Writes are remembered for HISTORY (about 6 months), well past any cooldown, and saved to disk so a
restart of the app doesn't lose them. Each write remembers its route:

* "ram" — read to stay in RAM (Now Playing Cache, Prefetch's next episode, lead-ins on RAM only).
* "ssd" — read on its way to the L2ARC (warm-up, Prefetch's L2ARC-only steps, lead-ins with an L2ARC).

Every re-read is measured, its age counted from the last time this app read that data (a hit
counts too: it touches the data in the cache, and it's what the cooldown counts from). One
thing is left out: a reboot empties RAM, so a RAM write from before the last boot isn't measured.
(Measuring only the first re-read after a read from the disks, as before, measured next to
nothing on a NAS with plenty of RAM: there, nearly every read is a hit.)

A whole file holds its lead-in too: for a lead-in, the later of the two writes counts.

Every count is also kept per library, with finer ages (FINE) and whether each read hit or
missed, so the Activity page can split re-reads into fresh and stale at any age you pick.
"""

from __future__ import annotations

import bisect
import json
import os
import threading
import time

HOUR = 3600
HISTORY = 180 * 24 * HOUR
ROUTES = ("ram", "ssd")
# Age buckets for the survival table: each is "up to" this many seconds since the write
AGES = (10 * 60, 30 * 60, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, 24 * HOUR,
        2 * 24 * HOUR, 4 * 24 * HOUR, 7 * 24 * HOUR, 14 * 24 * HOUR, 30 * 24 * HOUR, HISTORY)
SUGGEST_SHARE = 0.98  # a cooldown is suggested up to the age where this share was still cached ...
SUGGEST_MIN = 3       # ... over at least this many re-reads per age bucket
# Fully cached age: of the re-reads in a window, ordered by age, the age of the oldest hit before
# the first miss — everything read again up to that age was still cached. Suggested cooldowns and
# the Automatic one use it over the last week (a window, so it follows the NAS up and down: counts
# kept forever would pin it in place once a cooldown stops shorter ages being measured).
WINDOW = 7 * 24 * HOUR
AUTO_EVERY = 10 * 60   # how often Automatic works it out again
AUTO_FLOOR = 10 * 60   # the shortest cooldown it uses (a miss at the shortest age measured)
MAX_READS = 50000      # re-reads kept (each: when, route, library, age, hit), well over 6 months' worth
# Finer ages, per library, for the "Stale after" slider: 72 steps, evenly spaced on a log scale
# from 1 minute to HISTORY (each about 19% longer than the last). Each is "up to" this many seconds.
FINE = tuple(round(60 * (HISTORY / 60) ** (i / 71)) for i in range(72))


def _new_stats() -> dict:
    return {r: {"first": 0, "ages": [[0, 0] for _ in AGES], "libs": {}} for r in ROUTES}  # [still cached, gone]


def _new_lib() -> dict:
    """One library's counts on one route; every pair is [hit, miss]."""
    return {"first": [0, 0], "ages": [[0, 0] for _ in AGES], "fine": [[0, 0] for _ in FINE]}


def _load_ages(ages: list) -> list | None:
    """Saved age counts, from this version or the one before (whose last bucket, 30 days, was the
    end of the history then: the new last bucket starts empty)."""
    if len(ages) == len(AGES) - 1:
        ages = list(ages) + [[0, 0]]
    return [[int(a), int(b)] for a, b in ages] if len(ages) == len(AGES) else None


def _load_lib(got: dict, fine_ok: bool) -> dict | None:
    """``fine_ok``: the file's finer ages are the same steps as FINE (if not, they start again)."""
    if not isinstance(got, dict):
        return None
    first, ages, fine = got.get("first") or [0, 0], _load_ages(got.get("ages") or []), got.get("fine") or []
    if not fine_ok or len(fine) != len(FINE):
        fine = [[0, 0] for _ in FINE]
    if len(first) != 2 or ages is None:
        return None
    return {"first": [int(first[0]), int(first[1])], "ages": ages, "fine": [[int(a), int(b)] for a, b in fine]}


def _bucket(edges: tuple, age: float) -> int:
    return next((n for n, edge in enumerate(edges) if age <= edge), len(edges) - 1)


class CacheLog:
    def __init__(self, path: str | None = None):
        self.path = path
        self._lock = threading.Lock()
        # path -> {"lead": t, "lead_route": r, "lead_fresh": b, "whole": t, "whole_route": r, "whole_fresh": b}
        self._files: dict[str, dict] = {}
        self.stats = _new_stats()
        self.since = time.time()
        self.boot_time = 0.0  # when the NAS last booted (RAM was emptied then)
        # Every measured re-read: [when, route, library key, age (s), hit (1/0)], oldest first
        self.reads: list[list] = []
        self._measured = 0  # bumped on every read noted (so the page's figures are worked out again)
        # Automatic's cooldowns, worked out at most every AUTO_EVERY: per route {"at", "all", "libs"}
        self.auto = {r: {"at": 0.0, "all": None, "libs": {}} for r in ROUTES}
        self._dirty = False
        if path:
            self._load()

    # ----------------------------------------------------------- storage
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._files = {p: e for p, e in (data.get("files") or {}).items() if isinstance(e, dict)}
            stats = data.get("stats") or {}
            fine_ok = data.get("fine_edges") == list(FINE)
            for r in ROUTES:
                got = stats.get(r) or {}
                ages = _load_ages(got.get("ages") or [])
                if ages is not None:
                    libs = {str(k): v for k, v in ((k, _load_lib(v, fine_ok)) for k, v in (got.get("libs") or {}).items()) if v}
                    self.stats[r] = {"first": int(got.get("first", 0)), "ages": ages, "libs": libs}
            self.since = float(data.get("since") or self.since)
            self.reads = [[float(t), str(r), str(k), float(a), int(h)] for t, r, k, a, h in (data.get("reads") or [])
                          if r in ROUTES][-MAX_READS:]
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def save(self, force: bool = False) -> None:
        """Write to disk if anything changed (call it now and then)."""
        if not self.path or not (self._dirty or force):
            return
        with self._lock:
            data = {"files": dict(self._files), "stats": self.stats, "since": self.since, "fine_edges": list(FINE),
                    "reads": list(self.reads)}
            self._dirty = False
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            self._dirty = True

    def export(self) -> dict:
        """Everything the log holds, as it's saved (for "Export log" on the page)."""
        with self._lock:
            return json.loads(json.dumps({"exported": time.time(), "boot_time": self.boot_time, "since": self.since,
                                          "history_days": HISTORY // 86400, "ages": list(AGES), "fine_edges": list(FINE),
                                          "stats": self.stats, "auto": self.auto, "reads": self.reads,
                                          "files": self._files}))

    def reset_stats(self) -> None:
        """Start the measuring again (the write history is kept)."""
        with self._lock:
            self.stats = _new_stats()
            self.reads = []
            self.auto = {r: {"at": 0.0, "all": None, "libs": {}} for r in ROUTES}
            self._measured += 1
            self.since = time.time()
            self._dirty = True

    # ------------------------------------------------------------ writes
    def _latest(self, e: dict, whole: bool) -> tuple[float, str, bool]:
        """(when, route, fresh) of the write that holds this data: for a lead-in the later of
        its own write and the whole file's."""
        kinds = ("whole",) if whole else ("lead", "whole")
        best = (0.0, "ram", False)
        for k in kinds:
            t = e.get(k) or 0.0
            if t > best[0]:
                best = (t, e.get(k + "_route", "ram"), bool(e.get(k + "_fresh", True)))
        return best

    def note(self, path: str, whole: bool, at: float | None = None, route: str = "ram", hit: bool | None = None,
             library: str = "") -> None:
        """``path``'s lead-in (or whole file) was just read, by ``route``. ``hit``: whether the
        read found it cached already (None: not known). ``library``: the Plex library key it's
        in. Measures the earlier write, then records this one."""
        at = time.time() if at is None else at
        kind = "whole" if whole else "lead"
        with self._lock:
            e = self._files.setdefault(path, {})
            prev_t, prev_route, _ = self._latest(e, whole)
            if hit is not None:
                side = 0 if hit else 1
                if not prev_t or at - prev_t > HISTORY:
                    self.stats[route]["first"] += 1
                    self._lib(route, library)["first"][side] += 1
                elif prev_route == "ssd" or prev_t >= self.boot_time:
                    age = at - prev_t
                    i = _bucket(AGES, age)
                    self.stats[prev_route]["ages"][i][side] += 1
                    lib = self._lib(prev_route, library)
                    lib["ages"][i][side] += 1
                    lib["fine"][_bucket(FINE, age)][side] += 1
                    self.reads.append([at, prev_route, str(library or ""), round(age, 1), 0 if side else 1])
                    if len(self.reads) > MAX_READS:
                        del self.reads[:len(self.reads) - MAX_READS]
            self._measured += 1
            e[kind] = at
            e[kind + "_route"] = route
            # Whether this read came from the disks (kept for the exported log; not used to measure)
            e[kind + "_fresh"] = route == "ssd" or not hit
            if library:
                e["lib"] = str(library)
            self._dirty = True

    def _lib(self, route: str, library: str) -> dict:
        return self.stats[route].setdefault("libs", {}).setdefault(str(library or ""), _new_lib())

    # ------------------------------------------------------------ policy
    def last(self, path: str, whole: bool) -> float:
        """When ``path`` (its whole file, or its lead-in) was last cached; 0 if not logged."""
        with self._lock:
            e = self._files.get(path)
            return self._latest(e, whole)[0] if e else 0.0

    def written(self, path: str, whole: bool) -> tuple[float, str, str]:
        """(when, route, library key) of the write that holds ``path`` (its whole file, or its
        lead-in); when is 0 if not logged."""
        with self._lock:
            e = self._files.get(path) or {}
            t, route, _ = self._latest(e, whole) if e else (0.0, "ram", False)
            return t, route, e.get("lib", "")

    def latest_writes(self) -> dict:
        """path -> when it was last cached (its lead-in or all of it), for every logged file."""
        with self._lock:
            return {p: max(e.get("lead") or 0, e.get("whole") or 0) for p, e in self._files.items()}

    def library_of(self, path: str) -> str:
        with self._lock:
            return (self._files.get(path) or {}).get("lib", "")

    def still_cached(self, route: str, library: str, age: float) -> float | None:
        """How likely data cached ``age`` seconds ago is still cached, from what's been measured:
        the library's own share at that age if it has at least SUGGEST_MIN re-reads there, else
        every library's. None if neither has enough."""
        i = _bucket(AGES, max(0.0, age))
        with self._lock:
            lib = (self.stats[route].get("libs") or {}).get(str(library or ""))
            for kept, gone in ([lib["ages"][i]] if lib else []) + [self.stats[route]["ages"][i]]:
                if kept + gone >= SUGGEST_MIN:
                    return kept / (kept + gone)
        return None

    def fully_cached(self, route: str, library: str | None = None, start: float = 0.0, end: float | None = None) -> dict | None:
        """Fully cached age of the re-reads on ``route`` made between ``start`` and ``end`` (one
        library, or every one with None): {"age", "bound", "n", "miss"}. ``bound``: no misses at
        all, so it lasted at least ``age``; ``miss``: the youngest miss's age. None if no re-reads."""
        end = float("inf") if end is None else end
        with self._lock:
            got = [(a, h) for t, r, k, a, h in self.reads
                   if r == route and start <= t < end and (library is None or k == str(library))]
        return fully_cached_age(got)

    def fully_cached_series(self, route: str, libraries: list[str], step: str = "hour", count: int = 168,
                            now: float | None = None) -> dict:
        """Fully cached age hour by hour (or day by day, ``step`` "day"), the last ``count`` of
        them, for every library and each one apart: {"step", "starts": [...], "all": [point | None,
        ...], "libs": {key: [...]}}. A point is [age, no-misses (1/0), re-reads, youngest miss age
        or None]. The peaks and valleys show how much what the NAS is doing changes it."""
        import datetime as dt
        now = time.time() if now is None else now
        here = dt.datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0)
        if step == "day":
            here = here.replace(hour=0)
        size = dt.timedelta(days=1) if step == "day" else dt.timedelta(hours=1)
        starts = [(here - size * k).timestamp() for k in range(count - 1, -1, -1)]
        buckets = [[] for _ in starts]
        with self._lock:
            mine = [x for x in self.reads if x[1] == route and x[0] >= starts[0]]
        for x in mine:  # one pass: reads are in time order, so the bucket only moves forward
            n = bisect.bisect_right(starts, x[0]) - 1
            if n >= 0:
                buckets[n].append(x)

        def point(reads):
            fc = fully_cached_age([(a, h) for _t, _r, _k, a, h in reads])
            return [fc["age"], int(fc["bound"]), fc["n"], fc["miss"]] if fc else None
        out = {"step": step, "starts": starts, "all": [point(b) for b in buckets], "libs": {}}
        for k in libraries:
            out["libs"][k] = [point([x for x in b if x[2] == k]) for b in buckets]
        return out

    def auto_cooldown(self, route: str, library: str, fallback: float, now: float | None = None) -> float:
        """The cooldown (seconds) Automatic uses for ``library`` on ``route``: the library's fully
        cached age over the last week, else every library's, else ``fallback`` (the usual
        default). A miss at the shortest age measured gives AUTO_FLOOR, so shorter ages get
        measured again; with no misses it grows as older re-reads come in."""
        now = time.time() if now is None else now
        with self._lock:
            a = self.auto[route]
            stale = now - a["at"] >= AUTO_EVERY or now < a["at"]
        if stale:
            self._replan(route, now)
        with self._lock:
            a = self.auto[route]  # (replanning replaces it)
            got = a["libs"].get(str(library or "")) or a["all"]
        return float(got) if got else float(fallback)

    def _replan(self, route: str, now: float) -> None:
        with self._lock:
            recent = [x for x in self.reads if x[1] == route and now - WINDOW <= x[0] <= now]

        def choose(pairs):
            fc = fully_cached_age(pairs)
            if not fc or fc["n"] < SUGGEST_MIN:
                return None
            return max(AUTO_FLOOR, fc["age"])
        libs = {}
        for k in {x[2] for x in recent}:
            v = choose([(a, h) for _t, _r, kk, a, h in recent if kk == k])
            if v:
                libs[k] = v
        with self._lock:
            self.auto[route] = {"at": now, "all": choose([(a, h) for _t, _r, _k, a, h in recent]), "libs": libs}

    def recent(self, path: str, whole: bool, cooldown_hours: float, now: float | None = None) -> bool:
        """Was it cached within ``cooldown_hours``?"""
        now = time.time() if now is None else now
        return now - self.last(path, whole) < cooldown_hours * HOUR

    def prune(self, now: float | None = None) -> None:
        """Forget writes older than HISTORY."""
        now = time.time() if now is None else now
        keep = now - HISTORY
        with self._lock:
            gone = [p for p, e in self._files.items() if max(e.get("lead") or 0, e.get("whole") or 0) < keep]
            for p in gone:
                del self._files[p]
            if gone:
                self._dirty = True

    def counts(self, lead_hours: float, whole_hours: float, now: float | None = None) -> dict:
        """How many files each log holds within its cooldown, for the page."""
        now = time.time() if now is None else now
        with self._lock:
            whole = lead = 0
            for e in self._files.values():
                w, ld = e.get("whole") or 0, max(e.get("lead") or 0, e.get("whole") or 0)
                whole += now - w < whole_hours * HOUR
                lead += now - ld < lead_hours * HOUR
        return {"lead_ins": lead, "whole_files": whole}

    # ----------------------------------------------------------- reports
    def survival(self, names: dict | None = None, cached: dict | None = None, l2arc: dict | None = None) -> dict:
        """For the Activity page: per route, the re-reads by age and what share was still cached,
        how many were cached for the first time, and a suggested cooldown (seconds) if there's
        enough data. The same per library (``names``: library key -> title), with each library's
        reads by FINE age and first-time reads as [hit, miss]. Every library in ``names`` is
        listed, even with nothing measured yet (that's worth knowing too); ``cached``: library
        key -> whether it's being cached at all; ``l2arc``: library key -> whether its pool has an
        L2ARC (None: not known yet). The cache is per dataset, so some libraries can have one and
        others not."""
        key = (json.dumps([names, cached, l2arc], sort_keys=True), len(self.reads), self._measured)
        memo = getattr(self, "_survival_memo", None)
        if memo and memo[0] == key and time.time() - memo[1] < 10:  # (the page asks every few seconds)
            return memo[2]
        with self._lock:
            stats = json.loads(json.dumps(self.stats))
        names, cached, l2arc = names or {}, cached or {}, l2arc or {}
        out = {"since": self.since, "ages": list(AGES), "fine": list(FINE), "routes": {}}
        for r in ROUTES:
            rows = _rows(stats[r]["ages"])
            libs = []
            have = dict(stats[r].get("libs") or {})
            for key in names:
                have.setdefault(str(key), _new_lib())
            order = {k: n for n, k in enumerate(names)}  # Plex's own order (as the libraries were listed)
            for key, lib in sorted(have.items(), key=lambda kv: (order.get(kv[0], len(order)), (names.get(kv[0]) or kv[0]).lower())):
                lrows = _rows(lib["ages"])
                libs.append({"key": key, "name": names.get(key) or "", "cached": cached.get(key, True), "l2arc": l2arc.get(key),
                             "first": lib["first"],
                             "rows": lrows, "fine": lib["fine"], "suggest": suggest(lrows)})
            now = time.time()
            with self._lock:
                auto = json.loads(json.dumps(self.auto[r]))
            for lib in libs:
                lib["auto"] = auto["libs"].get(lib["key"])
                lib["fully"] = self.fully_cached(r, lib["key"], now - WINDOW)
                lib["suggest"] = _suggest_from(lib["fully"])
            fully = self.fully_cached(r, None, now - WINDOW)
            out["routes"][r] = {"first": stats[r]["first"], "rows": rows, "suggest": _suggest_from(fully), "fully": fully,
                                "libs": libs, "auto": auto["all"],
                                # (hourly for the last week, daily for the last month)
                                "hours": self.fully_cached_series(r, [lib["key"] for lib in libs], "hour", 168, now),
                                "days": self.fully_cached_series(r, [lib["key"] for lib in libs], "day", 30, now)}
        self._survival_memo = (key, time.time(), out)
        return out


def fully_cached_age(reads: list) -> dict | None:
    """Of re-reads [(age, hit), ...]: the age of the oldest hit before the first miss, by age —
    everything read again up to it was still cached. {"age", "bound", "n", "miss"}: ``bound`` when
    there was no miss (it lasted at least that long); ``age`` 0 if even the youngest missed."""
    if not reads:
        return None
    ordered = sorted(reads)
    age, miss = 0.0, None
    for a, hit in ordered:
        if not hit:
            miss = a
            break
        age = a
    return {"age": round(age), "bound": miss is None, "n": len(ordered), "miss": round(miss) if miss is not None else None}


def _suggest_from(fc: dict | None) -> int | None:
    """A suggested cooldown from a fully cached age: with at least SUGGEST_MIN re-reads, and above 0."""
    return fc["age"] if fc and fc["n"] >= SUGGEST_MIN and fc["age"] > 0 else None


def _rows(ages: list) -> list[dict]:
    rows = []
    for edge, (kept, gone) in zip(AGES, ages):
        n = kept + gone
        rows.append({"upto": edge, "kept": kept, "gone": gone, "pct": round(100 * kept / n) if n else None})
    return rows


def suggest(rows: list[dict]) -> int | None:
    """The longest age up to which cached data was nearly always still there (re-reading earlier
    is wasted), counting only age buckets with enough re-reads. None until there's enough data,
    or if even the shortest ages often missed."""
    best = None
    for row in rows:
        n = row["kept"] + row["gone"]
        if n < SUGGEST_MIN:
            continue
        if row["kept"] / n >= SUGGEST_SHARE:
            best = row["upto"]
        else:
            break
    return best

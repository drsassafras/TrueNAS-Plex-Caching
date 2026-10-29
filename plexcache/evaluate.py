"""Every number a page shows, evaluated before it's shown — one standard for all of them, no page, tool or check let
off (CLAUDE.md).

**A count** comes in one of four states, and only one of them has a number:

* ``done`` — evaluated over the library's data as it is now: {"s": "done", "n"}.
* ``pending`` — not in yet: the data it reads still being read (libdata.py), or its evaluation still waiting its turn:
  {"s": "pending", "why", "progress"?, "partial"?}. The page draws a placeholder, never a number — not even 0 — but for
  a count that grows as it's read ("partial": File Checks while the inspector reads the files, Plex Checks while the
  items' pages are read): what's been found so far, with the turning wheel beside it.
* ``na`` — doesn't apply to the library: a tool that doesn't work on its kind, Sync Libraries or Mirror Checks on a
  library that isn't paired with one on the mirror, the optimizer switched off for it: {"s": "na", "why"}.
* ``failed`` — couldn't be evaluated: the data couldn't be read, or the evaluation went wrong: {"s": "failed", "why"}.

The state gives the page every count this way (App.state's "counts": every page of the Tools menu, each library's count
and the page's total) and the page draws every one of them with one function (app.js countChip), so a number can't show
before it's in. The tests hold every page of the Tools menu to it (test_every_count_is_evaluated_before_it_shows).

**How long a count stands.** A count is of the data as it is now: one a change touches is pending until it's worked
out again. Each unit is also worked out again after a stale period of its own (Unit.stale_after: a tool's, Library
Health's, Mirror Checks' every few hours, the optimizer's scan), and each kind of data read again after its own
(libdata's TTLs) and each file after filemap.INSPECT_EVERY — the count standing meanwhile, since nothing it reads
changed. An update that reads or checks more makes what was worked out before it stale at once: a tool's scan_version,
Mirror Checks' FORMAT, the inspector's MAP_VERSION.

**The order.** One queue for everything evaluated per library — its data read (libdata.py: the Gatherer reads in this
order), Mirror Checks (mirrorhealth.py), every tool's scan (tools/), Library Health's File, Plex and Mirror Checks, the
optimizer's scan — in the order a page needs them. The page open goes first (focus: its page and library, told with each
state the page asks for), with what it waits for before it: the data it reads, then Mirror Checks and Merge Duplicates,
whose findings stop other tools' rows (their red tags: Tool.mark_stops); then the page's other libraries; then
everything else, a library at a time, the ones opened lately first. The rest waits meanwhile: the data layer breaks off
between its steps for the page's library, and the evaluator finishes the step it's on. The file inspector (File Checks
read what it read) is steered the same way, in its own thread: the page's library first, then the rest.
"""

import threading
import time
from typing import cast

from .i18n import M

STATES = ("done", "pending", "na", "failed")
FOCUS_FOR = 30          # seconds a page's focus lasts without the page asking again (it asks with every state: a few seconds)
RETRY_FAILED = 600      # an evaluation that went wrong is tried again after this, or as soon as what it reads changes
KINDS = ("records", "files", "details", "mirror")  # (libdata.ALL, in the order it's read)


# ---------------------------------------------------------------- a count: the one shape every page's number comes in
def done(n: int) -> dict:
    return {"s": "done", "n": int(n or 0)}


def pending(why, progress: dict | None = None, partial: int | None = None) -> dict:
    out = {"s": "pending", "why": why}
    if progress and progress.get("total"):
        out["progress"] = {"done": int(progress.get("done") or 0), "total": int(progress.get("total") or 0)}
    if partial is not None:
        out["partial"] = int(partial or 0)  # (what's been found so far: the count grows as it's read)
    return out
# (a pending count's "last": True — its partial is what was found last time, shown while it's worked out again)


def na(why) -> dict:
    return {"s": "na", "why": why}


def failed(why) -> dict:
    return {"s": "failed", "why": why}


def total(counts: list[dict]) -> dict:
    """A page's count over its libraries: a number only once every library it applies to has one."""
    live = [c for c in counts if c["s"] != "na"]
    if not live:
        return na(counts[0]["why"]) if counts else na(M("No library this works on."))
    waiting = [c for c in live if c["s"] == "pending"]
    if waiting:  # (found so far: the libraries done, and those still counting what they've found so far — once any is)
        out = {k: v for k, v in waiting[0].items() if k != "partial"}
        known = [c for c in live if c["s"] == "done" or "partial" in c]
        if known:
            out["partial"] = sum(c.get("partial", c.get("n", 0)) for c in known)
            if any(c.get("last") for c in known):
                out["last"] = True
            else:
                out.pop("last", None)
        return out
    got = next((c for c in live if c["s"] == "failed"), None)
    if got:
        return dict(got)
    return done(sum(c["n"] for c in live))


READING = {"records": M("Reading the library’s records from Plex…"), "files": M("Reading the library’s files…"),
           "details": M("Reading each item’s page from Plex…"), "mirror": M("Reading the library on the mirror…")}
DATA_NAMES = {"records": M("the library’s records"), "files": M("the library’s files"), "details": M("the items’ pages"), "mirror": M("the library on the mirror")}


def data_waiting(app, key: str, kinds) -> dict | None:
    """What a count says while the library's data of these kinds (libdata.py) isn't in: pending — being read now (how far),
    or waiting its turn — or failed: read with an error (the records, the mirror: what's there is from before, or nothing),
    or not read for one and not being read again. None once every kind is in."""
    ld = app.libdata
    d = ld.data(key)
    busy = None
    for w in [w for w in KINDS if w in kinds]:
        at = getattr(d, w + "_at")
        err = d.errors.get(w) or (d.errors.get("gather") if not at else None)
        if at and not (err and w in ("records", "mirror")):
            continue
        if busy is None:
            busy = ld.busy(key)
        if err and w not in busy:
            return failed(M("{what} couldn’t be read: {why}", what=DATA_NAMES[w], why=err))
        with ld.lock:
            progress = dict(ld.progress) if ld.progress else None
        if progress and progress.get("library") == str(key):
            return pending(READING.get(progress.get("what") or "", READING[w]), progress)
        return pending(M("Waiting its turn to be read: {what}", what=DATA_NAMES[w]))
    return None


# ---------------------------------------------------------------- what's evaluated: a unit, per library
class Unit:
    """Something evaluated per library. ``needs``: the data it reads; ``before``: the units evaluated first for the same
    library — their findings stop its rows, so its count waits for them."""
    name = ""
    needs: tuple[str, ...] = ()
    before: tuple[str, ...] = ()
    title = M("")
    stale_after = 24 * 3600  # seconds: worked out again after this long, though nothing it reads changed (its count standing)

    def __init__(self, app):
        self.app = app

    def applies(self, key: str) -> M | None:
        """None when it applies to the library (or may: see waiting), else why not (M)."""
        return None

    def waiting(self, key: str) -> dict | None:
        """What it waits for besides the library's data (a count: pending, or failed when it couldn't be had) — whether it
        applies isn't known until then (Mirror Checks: the mirror's libraries, what's paired). None: nothing."""
        return None

    def sig(self, key: str):
        """What its evaluation is of: the state of the data and of the settings it goes by."""
        raise NotImplementedError

    def current(self, key: str) -> bool:
        raise NotImplementedError

    def evaluate(self, key: str) -> None:
        raise NotImplementedError

    def due(self, key: str) -> bool:
        """Current, but to be evaluated again anyway besides its stale period (Mirror Checks: a scan from before FORMAT)."""
        return False

    def again(self, key: str) -> None:
        """Worked out again though it's current (its stale period up): what it keeps of the last time dropped first."""
        self.evaluate(key)

    def busy(self, key: str) -> dict | bool | None:
        """Being evaluated elsewhere (in a thread of its own: the optimizer's scan): how far ({done, total}) or True; the
        evaluator leaves it meanwhile and is told when it's done (Evaluator.kick)."""
        return None

    def holds_off(self, key: str, focus) -> bool:
        """Whether it waits for now, though it could be evaluated (the optimizer's scan, of every library at once: until the
        data layer has nothing left to read, unless its page is open)."""
        return False


class ToolUnit(Unit):
    """A tool's scan (tools/): its analysis over the library's data."""

    def __init__(self, app, tool):
        super().__init__(app)
        self.tool, self.name, self.needs = tool, tool.name, tuple(tool.reads)
        self.before = tuple(tool.evaluated_after())
        self.title = tool.title
        self.stale_after = tool.stale_after

    def applies(self, key: str):
        if not self.tool.works_on(key):
            return M("{tool} doesn’t work on this kind of library.", tool=self.tool.title)
        return self.tool.not_applicable(key)

    def sig(self, key):
        return self.tool._akey(self.app.libdata.data(key))

    def current(self, key):
        return self.tool.analysis_if_current(self.app.libdata.data(key)) is not None

    def evaluate(self, key):
        self.tool.analysis(self.app.libdata.data(key))

    def again(self, key):
        with self.tool.lock:
            self.tool._analysis.pop(str(key), None)
        self.evaluate(key)


class MirrorUnit(Unit):
    """Mirror Checks (mirrorhealth.py): the two sites compared. It reads the mirror's Plex and the other site's files
    itself (as they are now), and the records and files here from the data layer."""
    name = "mirrorchecks"
    needs = ("records", "files")
    title = M("Mirror Checks")

    def applies(self, key: str):
        return self.app.mirror_health.not_applicable(key)

    def waiting(self, key):
        w = self.app.mirror_health.waiting()
        if not w:
            return None
        if w[0] == "failed":
            return failed(M("The mirror’s Plex couldn’t be reached: {why}", why=w[1]))
        return pending(M("Reading the mirror’s libraries…"))

    def busy(self, key: str):
        return True if getattr(self.app.mirror_health, "reading", False) else None

    def sig(self, key):
        return self.app.libdata.data(key).stamp

    def current(self, key):
        return self.app.mirror_health.current(key)

    @property
    def stale_after(self):
        from .mirrorhealth import CHECK_EVERY
        return CHECK_EVERY

    def due(self, key):
        return self.app.mirror_health.due_for(key)

    def evaluate(self, key):
        self.app.mirror_health.evaluate(key)


class HealthUnit(Unit):
    """Library Health (libhealth.py, App.library_health): File Checks, Plex Checks and Mirror Checks' findings, every
    tool's chips laid over them. Each check page's count is current on its own (App.health_current): a change that
    doesn't touch a category leaves its count as it is."""
    name = "health"
    needs = ("records", "files")
    title = M("the checks")

    def applies(self, key: str):
        lib = self.app.library_info(key) or {}
        return None if lib.get("kind") and not lib.get("excluded") else M("This library isn’t checked.")

    def sig(self, key):
        return self.app._health_sig(key)

    @property
    def stale_after(self):
        from .libhealth import STALE_AFTER
        return STALE_AFTER

    def current(self, key):
        return self.app.health_current(key)

    def evaluate(self, key):
        self.app.library_health(key)

    def again(self, key):
        self.app.forget_health(key)
        self.evaluate(key)


class OptimizerUnit(Unit):
    """The optimizer's scan (optimizing.survey): every library's files against its choices, in a thread of its own — the
    evaluator starts it when a library's data has changed since the last one, and is told when it's done."""
    name = "optimizer"
    needs = ("records", "files")
    title = M("the optimizer’s scan")

    def applies(self, key: str):
        return self.app.optimizing.not_applicable(key)

    def sig(self, key):
        return self.app.libdata.data(key).stamp

    @property
    def stale_after(self):
        from .optimizing import STALE_AFTER
        return STALE_AFTER

    def current(self, key):
        return self.app.optimizing.current(key)

    def busy(self, key: str):
        op = self.app.optimizing
        return (dict(op.survey_progress or {}) or True) if op.surveying else None

    def holds_off(self, key, focus):
        if focus and focus[0] == "optimizer":
            return False
        ld = self.app.libdata
        with ld.lock:
            return bool(ld.queue or ld.current)

    def evaluate(self, key):
        self.app.optimizing.survey_soon()


# the check pages of the Tools menu, and Library Health's category each counts (app.js CHECK_PAGES; libhealth.PAGES)
CHECK_PAGES = {"filechecks": "file", "plexchecks": "plex", "mirrorchecks": "mirror"}
# the units a page shows, worked out first while it's open (a tool's page: its own)
FOCUS_UNITS = {"filechecks": ("health",), "plexchecks": ("health",), "mirrorchecks": ("mirrorchecks", "health"), "library": ("health",)}
# the pages of the Tools menu whose count isn't a unit's (counted by their own parts): the dataset's candidates
OWN_COUNTS = ("dataset",)
# the pages that count but aren't in the Tools menu's: the Library page's Health view (the items its checks flag)
OTHER_COUNTS = ("library",)


class Evaluator:
    """The one queue (see the module's docstring): evaluates, in a thread of its own, the first unit in order() that
    isn't current and can be — its data in, what comes before it settled — and gives every page's count (count, counts)."""

    def __init__(self, app):
        self.app = app
        self.cond = threading.Condition()
        self.focused: tuple[str, str, float] | None = None  # (page, library, until)
        self.working = False
        self.running: tuple[str, str] | None = None  # (unit, library) being evaluated
        self.failures: dict[tuple[str, str], tuple] = {}  # (unit, library) -> (its sig when it went wrong, why, when)
        self.evaluated: dict[tuple[str, str], float] = {}  # (unit, library) -> when it was last worked out here
        self.started = False
        self.started_at = time.time()
        self._units: dict[str, Unit] | None = None
        self._local = threading.local()  # (.memo while the state's counts are worked out, in that thread: each library's Library Health sig once)

    # ------------------------------------------------------------ the units, the libraries, the order
    def units(self) -> dict[str, Unit]:
        """Every unit, in the order they're evaluated for a library: Mirror Checks and Merge Duplicates (their findings stop
        other tools' rows), the other tools, Library Health (which shows them all), the optimizer's scan."""
        if self._units is None:
            tools = getattr(self.app, "tools", None) or {}
            out: dict[str, Unit] = {"mirrorchecks": MirrorUnit(self.app)}
            for name in sorted(tools, key=lambda n: (n != "merge", list(tools).index(n))):
                out[name] = ToolUnit(self.app, tools[name])
            out["health"] = HealthUnit(self.app)
            if getattr(self.app, "optimizing", None) is not None:
                out["optimizer"] = OptimizerUnit(self.app)
            self._units = out
        return self._units

    def libraries(self) -> list[str]:
        """Every library evaluated: the one in focus first, then the ones opened lately, then the rest."""
        keys = [str(lib["key"]) for lib in getattr(self.app, "libraries", []) or [] if lib.get("kind") and not lib.get("excluded")]
        f = self.focus_now()
        viewed = [k for k in getattr(self.app.libdata, "viewed", []) if k in keys]
        first = ([f[1]] if f and f[1] in keys else []) + viewed
        return list(dict.fromkeys(first + keys))

    def closure(self, name: str) -> list[str]:
        """A unit and what comes before it, in the order they're evaluated."""
        units, out = self.units(), []

        def add(n):
            if n in out or n not in units:
                return
            for b in units[n].before:
                add(b)
            out.append(n)
        add(name)
        return out

    def focus_units(self, page: str) -> tuple[str, ...]:
        if page in FOCUS_UNITS:
            return FOCUS_UNITS[page]
        return (page,) if page in self.units() else ()

    def order(self) -> list[tuple[str, str]]:
        """(unit, library), first to last: the page in focus — its library, with what comes before it, then its other
        libraries — then every library's units, libraries in order."""
        libs, units, out = self.libraries(), list(self.units()), []
        f = self.focus_now()
        if f:
            names = list(dict.fromkeys(n for u in self.focus_units(f[0]) for n in self.closure(u)))
            for k in ([f[1]] if f[1] in libs else []) + [k for k in libs if k != f[1]]:
                out += [(n, k) for n in names]
        for k in libs:
            out += [(n, k) for n in units]
        return list(dict.fromkeys(out))

    # ------------------------------------------------------------ the page in focus
    def focus(self, page: str, key: str) -> None:
        """The page open, and its library: evaluated first (and its data read first), for as long as the page keeps asking."""
        page, key = str(page or ""), str(key or "")
        if not page or not key or not self.app.library_info(key):
            return
        with self.cond:
            was = self.focused
            self.focused = (page, key, time.time() + FOCUS_FOR)
        if not was or was[:2] != (page, key) or was[2] < time.time():
            units = self.units()
            needs = {w for u in self.focus_units(page) for n in self.closure(u) for w in units[n].needs}
            ld = self.app.libdata
            waiting = tuple(w for w in KINDS if w in needs and data_waiting(self.app, key, (w,)) is not None)
            if waiting:
                ld.fast_track(key, waiting)
            ld.order(self.libraries())
            self.steer()
        self.kick()

    def focus_now(self) -> tuple[str, str] | None:
        f = self.focused
        return (f[0], f[1]) if f and f[2] > time.time() else None

    # ------------------------------------------------------------ evaluating
    def settled(self, name: str, key: str) -> bool:
        """Whether a unit is as far as it can get for the library now: current, not applying, gone wrong at this state, or
        its data not to be had (couldn't be read)."""
        u = self.units().get(name)
        if u is None:
            return True
        w = u.waiting(key)
        if w:
            return w["s"] == "failed"
        if u.applies(key) is not None or u.current(key) or self.gone_wrong(name, key):
            return True
        w = data_waiting(self.app, key, u.needs)
        return bool(w and w["s"] == "failed")

    def ready(self, name: str, key: str) -> bool:
        """Whether a unit can be evaluated for the library now: it applies, its data is in, and what comes before it is
        settled."""
        u = self.units()[name]
        if u.waiting(key) or u.applies(key) is not None or data_waiting(self.app, key, u.needs) is not None or u.holds_off(key, self.focus_now()):
            return False
        return all(self.settled(b, key) for b in u.before)

    def gone_wrong(self, name: str, key: str):
        """Why the unit's evaluation went wrong for the library at its state now (None: it didn't, or the state moved on, or
        it's been a while)."""
        got = self.failures.get((name, str(key)))
        if not got:
            return None
        try:
            if got[0] == self.units()[name].sig(key) and time.time() - got[2] < RETRY_FAILED:
                return got[1]
        except Exception:  # noqa: BLE001
            return got[1]
        return None

    def next_job(self) -> tuple[str, str] | None:
        units = self.units()
        later = None
        for name, key in self.order():
            u = units[name]
            try:
                if u.waiting(key) or u.applies(key) is not None or self.gone_wrong(name, key) or u.busy(key):
                    continue
                cur = u.current(key)
                if cur and not u.due(key) and not self.aged(name, key):
                    continue
                if not self.ready(name, key):
                    continue
            except Exception:  # noqa: BLE001 — a unit that can't say: left for now
                continue
            if cur:  # (current, but due again: after everything that isn't current)
                later = later or (name, key)
                continue
            return name, key
        return later

    def aged(self, name: str, key: str) -> bool:
        """Whether a unit was worked out for the library longer ago than its stale period (Unit.stale_after)."""
        u = self.units()[name]
        return time.time() - self.evaluated.get((name, str(key)), self.started_at) > u.stale_after

    def kick(self) -> None:
        """Something may need evaluating: the thread started (or woken)."""
        if not self.started:
            return
        with self.cond:
            if self.working:
                self.cond.notify_all()
                return
            self.working = True
        threading.Thread(target=self._work, name="evaluate", daemon=True).start()

    def start(self) -> None:
        self.started = True
        self.kick()

    def _work(self) -> None:
        while True:
            job = self.next_job()
            if job is None:
                with self.cond:
                    self.working = False
                self.steer()
                return
            name, key = job
            with self.cond:
                self.running = job
            u = self.units()[name]
            sig = None
            try:
                sig = u.sig(key)
                if u.current(key):  # (due again: its stale period up, or Mirror Checks' scan from before FORMAT)
                    u.again(key)
                else:
                    u.evaluate(key)
                self.evaluated[job] = time.time()
                self.failures.pop(job, None)
            except Exception as e:  # noqa: BLE001 — said by its count (failed), tried again when its state moves on
                why = e.args[0] if e.args and isinstance(e.args[0], M) else str(e) or type(e).__name__
                self.failures[job] = (sig, why, time.time())
            finally:
                with self.cond:
                    self.running = None
                    self.cond.notify_all()
            if name == "health":
                self.steer()

    def data_changed(self, key, what) -> None:
        """A gathering done (libdata's listener): what reads it is evaluated again."""
        self.kick()

    def steer(self) -> None:
        """The file inspector (File Checks read what it read) set to the first library in order with files it hasn't read,
        or read by an older version — the page's library first — unless it's paused there by hand; with none, a library not
        looked over for INSPECT_ROUND, for files read long ago. A library paused for another (Inspector.start: one at a
        time) picks up where it was; once one has been gone through at a state of its data, what's left there couldn't be
        read, and isn't tried again until the data changes."""
        ins = getattr(self.app, "inspector", None)
        if ins is None or not self.started:
            return
        try:
            from .filemap import INSPECT_ROUND
            for key in self.libraries():
                hc = (getattr(self.app, "health_counts", None) or {}).get(key) or {}
                waiting = hc.get("unread") or hc.get("outdated")
                # (and, with none waiting, every INSPECT_ROUND: its files looked over for those read long ago — filemap.INSPECT_EVERY)
                round_due = bool(hc) and time.time() - ins.looked.get(key, 0) > INSPECT_ROUND
                if key in ins.held or not (waiting or round_due):
                    continue
                state = (self.app.libdata.data(key).stamp, ins.rev(key))
                if ins.running_for(key):
                    return
                if not round_due and ins.steered.get(key) == state and not ins.left(key):
                    continue
                ins.steered[key] = state
                ins.start(key)
                return
        except Exception:  # noqa: BLE001 — tried again at the next health evaluation
            return

    def bring(self, name: str, key: str, progress=None, refresh: bool = False, timeout: float = 1800):
        """The unit worked out for the library (a scan asked for from its page): its data read and what comes before it
        settled, by the evaluator's thread — this waits, telling ``progress`` how far the reading is; ``refresh``: its data
        read again first (Scan Again). Before the evaluator runs (the tests, the first moments), done here. The library's
        data; ValueError when its data couldn't be read, or the evaluation went wrong."""
        key = str(key)
        units, ld = self.units(), self.app.libdata
        u = units[name]
        if not self.started:
            data = ld.gather(key, u.needs, wait=True, progress=progress, refresh=refresh)
            for n in self.closure(name):
                un = units[n]
                try:
                    if un.applies(key) is None and not un.current(key) and data_waiting(self.app, key, un.needs) is None:
                        un.evaluate(key)
                except Exception as e:  # noqa: BLE001
                    if n == name:
                        raise ValueError(e.args[0] if e.args and isinstance(e.args[0], M) else str(e)) from None
            return data
        if refresh:
            ld.refresh(key, u.needs)
        waiting = tuple(w for w in KINDS if w in u.needs and data_waiting(self.app, key, (w,)) is not None)
        if waiting and not refresh:
            ld.fast_track(key, waiting)
        end = time.time() + timeout
        while time.time() < end:
            w = data_waiting(self.app, key, u.needs)
            if w is not None and w["s"] == "failed":
                raise ValueError(w["why"])
            if w is None:
                if u.applies(key) is not None or u.current(key):
                    return ld.data(key)
                wrong = self.gone_wrong(name, key)
                if wrong:
                    raise ValueError(wrong)
            elif progress is not None and w.get("progress"):
                with ld.lock:
                    p = dict(ld.progress) if ld.progress else None
                if p and p.get("library") == key:
                    progress(p)
            self.kick()
            with self.cond:
                self.cond.wait(0.2)
        raise ValueError(M("Still waiting for {what}: try again in a while.", what=DATA_NAMES.get(waiting[0]) if waiting else u.title))

    def settle(self, timeout: float = 60) -> bool:
        """Every library's data read and everything evaluated (the tests): whether it came to rest."""
        end = time.time() + timeout
        ld = self.app.libdata
        while time.time() < end:
            with ld.lock:
                reading = bool(ld.queue or ld.current)
            self.kick()
            if not reading and not self.working and self.next_job() is None and not any(u.busy(k) for u in self.units().values() for k in self.libraries()[:1]):
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------ the counts
    def count(self, page: str, key: str) -> dict:
        """A page's count for a library, as the page draws it (see the module's docstring)."""
        key = str(key)
        app = self.app
        lib = app.library_info(key) or {}
        if not lib.get("kind"):
            return na(M("Plex Cache doesn’t work on this kind of library."))
        if lib.get("excluded"):
            return na(M("This library is left out (System → Media Mappings)."))
        if page == "dataset":
            return self._dataset_count(key)
        if page == "library":  # (the Library page's Health view: the items flagged by every check and tool shown there)
            c = self._unit_count("health", key, M("Running the checks…"))
            if c["s"] != "done":
                return self._last_known(c, key, "flagged")
            got = app.health_counts.get(key) or {}
            return self._still_reading(key, got, ("details",), "flagged") or done(got.get("flagged") or 0)
        if page in CHECK_PAGES:
            cat = CHECK_PAGES[page]
            if page == "mirrorchecks":
                c = self._unit_count("mirrorchecks", key, M("Comparing the two sites…"))
                if c["s"] != "done":
                    return self._last_known(c, key, cat)
            c = self._unit_count("health", key, M("Running the checks…"), cat)
            if c["s"] != "done":
                return self._last_known(c, key, cat)
            got = app.health_counts.get(key) or {}
            if page in ("plexchecks", "filechecks"):  # (what they read as it comes: the items' pages, the files' insides)
                still = self._still_reading(key, got, ("details",) if page == "plexchecks" else (), cat)
                if still:
                    return still
            return done(got.get(cat) or 0)
        units = self.units()
        if page not in units:
            return na(M("Not counted per library."))
        c = self._unit_count(page, key, M("Counting…"))
        if c["s"] != "done":
            return c
        if page == "optimizer":
            return done(((app.optimizing.summary or {}).get(key) or {}).get("todo") or 0)
        return done(cast(ToolUnit, units[page]).tool.count_live(key))

    def _last_known(self, c: dict, key: str, cat: str) -> dict:
        """A check page's count while it's worked out again (pending): what was found last time beside the wheel (partial,
        "last": the page says so) — its findings as last known shown meanwhile, updated, not replaced, when they're in.
        As it is when nothing's known yet (or it couldn't be worked out, or doesn't apply)."""
        if c["s"] != "pending" or "partial" in c:
            return c
        got = self.app.health_counts.get(str(key)) or {}
        if cat not in got:
            return c
        return dict(c, partial=int(got.get(cat) or 0), last=True)

    def _still_reading(self, key: str, got: dict, kinds: tuple, cat: str = "file") -> dict | None:
        """Pending with what's been found so far (partial: Library Health's count ``cat`` as worked out now) while what a
        check page reads as it comes is still being read — the items' pages (``kinds``: the data layer's), the files' insides
        (the inspector: those not read yet, or read by an older version) — or failed when it couldn't be. None once it's in."""
        app = self.app
        part = int((got.get("flagged") if cat == "flagged" else got.get(cat)) or 0)
        if kinds:
            w = data_waiting(app, key, kinds)
            if w:
                return dict(w, partial=part) if w["s"] == "pending" else w
        if cat in ("file", "flagged"):
            left = int(got.get("unread") or 0) + int(got.get("outdated") or 0)
            if left:
                ins = getattr(app, "inspector", None)
                p = ins.progress_for(key) if ins is not None else None
                files = int(got.get("files") or 0)
                if p and p.get("running"):
                    return pending(M("Inspecting the files: {done} of {total}", done=p.get("done", 0), total=p.get("total", 0)), p, part)
                return pending(M("Waiting its turn for the inspector: {n} files to read", one="Waiting its turn for the inspector: {n} file to read", n=left),
                               {"done": files - left, "total": files}, part)
        return None

    def _unit_count(self, name: str, key: str, counting, cat: str | None = None) -> dict:
        """pending / na / failed while the unit isn't current for the library (and what it waits for), else done (0: the
        caller puts the number in)."""
        units = self.units()
        u = units[name]
        w = u.waiting(key)
        if w:
            return w
        why = u.applies(key)
        if why is not None:
            return na(why)
        w = data_waiting(self.app, key, u.needs)
        if w:
            return w
        for b in self.closure(name)[:-1]:
            if self.settled(b, key):
                continue
            ub = units[b]
            return data_waiting(self.app, key, ub.needs) or pending(M("Waiting for {what}", what=ub.title), self._busy_progress(ub, key))
        cur = u.current(key) if cat is None else self.app.health_current(key, cat)
        if not cur:
            wrong = self.gone_wrong(name, key)
            if wrong:
                return failed(M("Couldn’t be worked out: {why}", why=wrong))
            self.kick()
            return pending(counting, self._busy_progress(u, key))
        return done(0)

    @staticmethod
    def _busy_progress(u: Unit, key: str) -> dict | None:
        try:
            b = u.busy(key)
        except Exception:  # noqa: BLE001
            return None
        return b if isinstance(b, dict) else None

    def _dataset_count(self, key: str) -> dict:
        """Convert Folder to Child Dataset: the library's folders that could be datasets of their own — known once the
        datasets have been looked at (the first checks)."""
        app = self.app
        if not getattr(app, "zfs", None):
            return pending(M("Looking at the datasets…"))
        try:
            cands = app.migration.candidates()
        except Exception as e:  # noqa: BLE001
            return failed(M("{why}", why=str(e)))
        return done(sum(1 for c in cands if str(c.get("library")) == key and c.get("suggested") and c.get("child")))

    def pages(self) -> list[str]:
        """Every page of the Tools menu that counts: the check pages, every unit with a page of its own, the dataset's."""
        units = self.units()
        return list(CHECK_PAGES) + [n for n in units if n not in ("mirrorchecks", "health")] + list(OWN_COUNTS)

    def counts(self) -> dict:
        """Every page of the Tools menu, and the Library page: {page: {"libraries": {key: count}, "total": count}} — every
        library Plex has that Plex Cache works on, those a page doesn't apply to "na" — and the menu's own count, over its
        pages ("tools": {"total"})."""
        libs = [str(lib["key"]) for lib in getattr(self.app, "libraries", []) or [] if lib.get("kind")]
        out = {}
        if not libs and not getattr(self.app, "libraries_read", True):  # (Plex's libraries not read yet: nothing known, no dash)
            err = getattr(self.app, "scan_error", None)
            c = failed(M("Plex’s libraries couldn’t be read: {why}", why=err)) if err else pending(M("Reading the libraries from Plex…"))
            out = {page: {"libraries": {}, "total": dict(c)} for page in self.pages() + list(OTHER_COUNTS)}
            out["tools"] = {"libraries": {}, "total": dict(c)}
            return out
        self._local.memo = {}
        try:
            for page in self.pages() + list(OTHER_COUNTS):
                per = {}
                for key in libs:
                    try:
                        per[key] = self.count(page, key)
                    except Exception as e:  # noqa: BLE001
                        per[key] = failed(M("{why}", why=str(e)))
                out[page] = {"libraries": per, "total": total(list(per.values()))}
        finally:
            self._local.memo = None
        out["tools"] = {"libraries": {}, "total": total([out[p]["total"] for p in self.pages()])}
        return out

    def memo(self, what: str, key: str, fn):
        """A value worked out once while the state's counts are (Library Health's sig, asked by every check page)."""
        m = getattr(self._local, "memo", None)
        if m is None:
            return fn()
        k = (what, key)
        if k not in m:
            m[k] = fn()
        return m[k]

    def snapshot(self) -> dict:
        f = self.focus_now()
        r = self.running
        return {"focus": {"page": f[0], "library": f[1]} if f else None, "running": {"unit": r[0], "library": r[1]} if r else None}

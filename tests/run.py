"""Run the test suite with a progress bar, so you can see it isn't stuck — on every core at once.

    python3 tests/run.py                  # the whole suite (tests.test_plexcache)
    python3 tests/run.py <module>         # another test module, by its import name
    python3 tests/run.py -j 1             # one test at a time, in this process (also PLEXCACHE_TEST_JOBS=1)
    python3 tests/run.py -j 4             # four workers (the default: two per core)
    python3 tests/run.py --exclude WebBuildTests   # all but that class (tnsync runs it in a section of its own)

The tests are shared out among worker processes: a test at a time, except that a class set up once for all its tests
(setUpClass) goes as a whole, its tests together and in order in one worker. Each worker imports the module itself (its
own temporary folder, its own fake servers on their own ports) and asks for more when it's done. The slowest go first,
by how long each took last time (kept in ~/.cache/plex-cache/test-times.json), so the run isn't left waiting on one at
the end. Most tests spend their time waiting (on a fake server, a retry, a subprocess) rather than computing, so the
default is two workers per core. The warnings options the run was started with (-W, -X) are the workers' too. With one
test, or -j 1, it runs in this process, as unittest would.

PLEXCACHE_PROGRESS=bar draws the bar even when the output isn't a terminal (a log shown on one later); COLUMNS then says
how wide. PLEXCACHE_PROGRESS=feed sends it to tnsync's board instead (dev/board.py), which draws it in the tests' section.

GitHub runs the suite with unittest itself (build.yml), one test at a time: this file is for runs by hand, tnsync
and dev/future.py.

On a terminal, one line is redrawn as tests finish:

    [##########----------]  59/118  ServerTests.test_export_has_no_secrets

When the output isn't a terminal (CI, a log file), it prints a line every 10%.
Failures are listed in full at the end, as unittest normally does, and the
exit code is non-zero if anything failed.

A stop signal (SIGTERM: what tnsync sends when you press Enter) ends the run at
once, mid-test. The tests that were running when it came aren't counted as failed:
the run says how many tests ran and how many weren't run, and exits with 130.
"""

import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dev.progress import OFF, RED, Progress, drawn, feeding, short_version, tally, timed  # noqa: E402 — the bar the dev tools share (dev/upkeep.py's too)
from dev.progress import result as result_line  # noqa: E402 — (``result`` is run's: the result line every dev tool says)

FEED = feeding()  # (read before main() takes PLEXCACHE_PROGRESS out of the tests' environment)

TIMES = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "plex-cache", "test-times.json")


def ran_on() -> list[str]:
    """On tnsync's board, after a run's result: what it ran on, read now (so Current Tests and Future Tests, the same
    suite on this machine and in the next Python's image, show where they differ) — the software the app and its tests
    lean on most, the most first (the board leaves the last off when there's no room): Python, Node (the web's checks),
    MKVToolNix and ffmpeg (the optimizer's tools), OpenSSL (every https and TLS connection). One that isn't there is left
    out. Nothing off the board."""
    if not FEED:
        return []
    import platform
    import re
    import ssl

    def tool(cmd: list[str], pattern: str) -> str:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
        got = re.search(pattern, out)
        return got.group(1) if got else ""
    found = [("Python", platform.python_version()), ("Node", tool(["node", "--version"], r"v(\S+)")),
             ("MKVToolNix", tool(["mkvmerge", "--version"], r"mkvmerge v(\S+)")), ("ffmpeg", tool(["ffmpeg", "-version"], r"ffmpeg version (\S+)")),
             ("OpenSSL", (re.search(r"OpenSSL (\S+)", ssl.OPENSSL_VERSION) or [None, ""])[1])]
    return [f"{name} {short_version(version)}" for name, version in found if version]


def _name(test) -> str:
    return f"{type(test).__name__}.{getattr(test, '_testMethodName', '')}"


class ProgressResult(unittest.TextTestResult):
    """A run in this process (-j 1, or one class): unittest's result, drawing the bar as tests finish."""

    def __init__(self, stream, descriptions, verbosity, total: int, tty: bool):
        super().__init__(stream, descriptions, 0)
        self.progress = Progress(total, tty, feed=FEED, bar="tests")
        self.progress.workers = 1   # (this process alone: on the board, at the bar's right)
        self.on_stop = None   # the SIGTERM handler main() sets, kept in place between tests

    @property
    def done(self) -> int:
        return self.progress.done

    def _draw(self, test):
        self.progress.draw(_name(test), len(self.failures) + len(self.errors))

    def startTest(self, test):
        # A test can leave its own SIGTERM handler behind (the app's main() sets one); put ours back,
        # so a stop still stops the run.
        if self.on_stop and signal.getsignal(signal.SIGTERM) is not self.on_stop:
            signal.signal(signal.SIGTERM, self.on_stop)
        super().startTest(test)
        if isinstance(test, unittest.TestCase):
            self.progress.show(_name(test), ticking=True)   # (the test under way, its seconds counting)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._draw(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._draw(test)

    def addError(self, test, err):
        super().addError(test, err)
        # A setUpClass/tearDownModule error isn't one of the counted tests.
        if isinstance(test, unittest.TestCase):
            self._draw(test)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._draw(test)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._draw(test)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._draw(test)


# ----------------------------------------------------------------- one process
def _cleanup_module(module: str):
    tests_module = sys.modules.get(module)
    if tests_module is not None and getattr(tests_module, "TMP", None):
        shutil.rmtree(tests_module.TMP, ignore_errors=True)   # what tearDownModule would have removed


def run_here(module: str, suite, total: int, tty: bool, started: float) -> int:
    runner = unittest.TextTestRunner(
        stream=sys.stdout, verbosity=0,
        resultclass=lambda stream, desc, verb: ProgressResult(stream, desc, verb, total, tty))
    # TextTestRunner prints its own summary; keep ours instead.
    result = runner._makeResult()
    unittest.registerResult(result)

    def stop(*_):
        # Stopped from outside (tnsync's Enter): end now, without waiting for the test that's running.
        # That test didn't finish, so it isn't a failure; only the tests that finished are counted.
        if tty:
            sys.stdout.write("\r\033[K")
        bad = len(result.failures) + len(result.errors)
        if bad:
            result.printErrors()
            print(f"\n    {RED if tty else ''}{result_line(tally(bad, 'test', 'failed', of=result.done, feed=FEED))}{OFF if tty else ''}")
        print(f"    {result_line('stopped', tally(result.done, 'test', 'ran', of=total, feed=FEED), f'{total - result.done} not run')}")
        sys.stdout.flush()
        _cleanup_module(module)
        os._exit(130)
    result.on_stop = stop
    signal.signal(signal.SIGTERM, stop)
    result.startTestRun()
    try:
        suite(result)
    finally:
        result.stopTestRun()
    if tty:
        sys.stdout.write("\r\033[K")
    took = time.time() - started
    bad = len(result.failures) + len(result.errors)
    if bad:
        result.printErrors()
        print(f"\n    {RED if tty else ''}{result_line(tally(bad, 'test', 'failed', of=result.testsRun, feed=FEED), *ran_on())}{OFF if tty else ''}{timed(f'{took:.0f} s', feed=FEED)}")
        return 1
    print(f"    {result_line(tally(result.testsRun, 'test', 'passed', feed=FEED), *ran_on())}{timed(f'{took:.0f} s', feed=FEED)}")
    return 0


# ----------------------------------------------------------------- workers
def _class_fixtures(cls) -> bool:
    """Whether the class sets itself up once for all its tests (setUpClass, tearDownClass): its tests then stay together."""
    return any(getattr(cls, n).__func__ is not getattr(unittest.TestCase, n).__func__ for n in ("setUpClass", "tearDownClass"))


def _units(suite) -> list[tuple[str, list[str]]] | None:
    """The suite's tests by class, in the order unittest would run them: [(class, [test ids])]. None when it holds
    something that can't be shared out by id (a module that didn't import, a test that isn't a TestCase)."""
    units: dict[str, list[str]] = {}

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                if walk(t) is False:
                    return False
            elif isinstance(t, unittest.TestCase) and type(t).__module__ != "unittest.loader":
                cls = type(t)
                # a class set up once for its tests (setUpClass) is one unit; any other, a unit per test, so one long class
                # is shared out too and the run isn't left waiting on it at the end
                key = f"{cls.__module__}.{cls.__qualname__}" if _class_fixtures(cls) else t.id()
                units.setdefault(key, []).append(t.id())
            else:
                return False
        return True
    return list(units.items()) if walk(suite) else None


def _without(suite, classes: set[str]):
    """The suite without the tests of the classes named (by their names alone: WebBuildTests)."""
    kept = unittest.TestSuite()
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            kept.addTest(_without(t, classes))
        elif type(t).__name__ not in classes:
            kept.addTest(t)
    return kept


def _load_times() -> dict:
    try:
        with open(TIMES, encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_times(times: dict):
    try:
        os.makedirs(os.path.dirname(TIMES), exist_ok=True)
        with open(TIMES + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(times, fh)
        os.replace(TIMES + ".tmp", TIMES)
    except OSError:
        pass


def _python() -> list[str]:
    """This interpreter, with the warnings (-W) and -X options this run was started with."""
    args = [sys.executable, *(f"-W{w}" for w in sys.warnoptions)]
    for k, v in getattr(sys, "_xoptions", {}).items():
        args += ["-X", k if v is True else f"{k}={v}"]
    return args


class Worker:
    def __init__(self, module: str):
        to_r, self.to_w = os.pipe()     # classes to run, one per line
        self.from_r, from_w = os.pipe()  # what happened, one JSON line each
        env = dict(os.environ, PLEXCACHE_TEST_WORKER=f"{to_r},{from_w}")
        self.proc = subprocess.Popen([*_python(), os.path.abspath(__file__), "--worker", module], env=env,
                                     stdin=subprocess.DEVNULL, pass_fds=(to_r, from_w))
        os.close(to_r)
        os.close(from_w)
        self.buf = b""
        self.unit: tuple[str, list[str]] | None = None  # the class it's running
        self.reported: set[str] = set()                # its tests that have finished

    def give(self, unit: tuple[str, list[str]] | None):
        self.unit, self.reported = unit, set()
        try:
            if unit is None:
                os.close(self.to_w)
                self.to_w = -1
            else:
                os.write(self.to_w, (json.dumps(unit[1]) + "\n").encode())
        except OSError:
            pass  # (it has gone: its end is read as such)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


def run_parallel(module: str, units: list, total: int, jobs: int, tty: bool, started: float) -> int:
    times = _load_times()
    # the longest first (last run's times; a class not timed yet, by its number of tests), so none is left to the end
    queue = sorted(units, key=lambda u: -times.get(u[0], len(u[1]) * 0.5))
    progress = Progress(total, tty, feed=FEED, bar="tests")
    failures: list[str] = []
    workers: list[Worker] = []
    sel = selectors.DefaultSelector()
    unit_started: dict[str, float] = {}
    respawns = 3
    running: dict = {}   # worker -> (the test it's running, when it started)
    started_workers = min(jobs, len(queue))  # (what the summary says: a worker started again in one's place isn't another)
    progress.workers = started_workers   # (on the board: at the bar's right)

    def finish(code: int, stopped: bool = False) -> int:
        for w in workers:
            w.kill()
        if tty:
            sys.stdout.write("\r\033[K")
        bad = len(failures)
        if bad:
            print()
            for block in failures:
                print(block)
        took = time.time() - started
        if stopped:
            if bad:
                print(f"\n    {RED if tty else ''}{result_line(tally(bad, 'test', 'failed', of=progress.done, feed=FEED))}{OFF if tty else ''}")
            print(f"    {result_line('stopped', tally(progress.done, 'test', 'ran', of=total, feed=FEED), f'{total - progress.done} not run')}")
        elif bad:
            print(f"\n    {RED if tty else ''}{result_line(tally(bad, 'test', 'failed', of=progress.done, feed=FEED), *ran_on())}{OFF if tty else ''}{timed(f'{took:.0f} s', f'{started_workers} workers', feed=FEED)}")
        else:
            print(f"    {result_line(tally(progress.done, 'test', 'passed', feed=FEED), *ran_on())}{timed(f'{took:.0f} s', f'{started_workers} workers', feed=FEED)}")
        sys.stdout.flush()
        return code

    def stop(*_):
        # tnsync's Enter: every worker ends now, mid-test (they have the signal too, as its process group); the
        # tests that were running aren't failures.
        os._exit(finish(130, stopped=True))
    signal.signal(signal.SIGTERM, stop)

    def spawn():
        w = Worker(module)
        workers.append(w)
        sel.register(w.from_r, selectors.EVENT_READ, w)

    for _ in range(started_workers):
        spawn()
    live = len(workers)
    try:
        while live:
            for key, _ in sel.select():
                w: Worker = key.data
                chunk = os.read(w.from_r, 65536)
                if not chunk:  # the worker has ended
                    sel.unregister(w.from_r)
                    os.close(w.from_r)
                    live -= 1
                    code = w.proc.wait()
                    running.pop(w, None)
                    if w.unit is not None:  # mid-class: the tests it hadn't finished are errors, and someone else goes on
                        lost = [t for t in w.unit[1] if t not in w.reported]
                        failures.append(f"{'=' * 70}\nERROR: {w.unit[0]}\n{'-' * 70}\nThe worker running it ended (exit code {code}) "
                                        f"with {len(lost)} of its tests not finished:\n  " + "\n  ".join(lost) + "\n")
                        for t in lost:
                            progress.draw(t.rsplit(".", 2)[-2] + "." + t.rsplit(".", 1)[-1], len(failures))
                        w.unit = None
                        if queue and respawns:
                            respawns -= 1
                            spawn()
                            live += 1
                    continue
                w.buf += chunk
                while b"\n" in w.buf:
                    line, w.buf = w.buf.split(b"\n", 1)
                    msg = json.loads(line)
                    if msg["t"] == "want":
                        if w.unit is not None:
                            times[w.unit[0]] = round(time.time() - unit_started[w.unit[0]], 2)
                        unit = queue.pop(0) if queue else None
                        if unit:
                            unit_started[unit[0]] = time.time()
                        w.give(unit)
                    elif msg["t"] == "start":   # (a test under way: the one the bar names, the newest started)
                        running[w] = (msg["name"], time.time())
                        progress.show(msg["name"], ticking=True)
                    elif msg["t"] == "test":
                        if msg.get("text"):
                            failures.append(msg["text"])
                        if msg.get("id"):
                            w.reported.add(msg["id"])
                            running.pop(w, None)
                            progress.draw(msg["name"], len(failures))
                            if running:   # (another still under way: named again, its seconds from when it started)
                                name, since = max(running.values(), key=lambda r: r[1])
                                progress.show(name, ticking=True, since=since)
    except KeyboardInterrupt:
        return finish(130, stopped=True)
    _save_times(times)
    return finish(1 if failures else 0)


def worker(module: str) -> int:
    """A worker: runs the classes it's sent, one at a time, saying how each test went (run_parallel)."""
    to_r, from_w = (int(x) for x in os.environ.pop("PLEXCACHE_TEST_WORKER").split(","))
    jobs = os.fdopen(to_r, "rb", buffering=0)
    out = os.fdopen(from_w, "wb", buffering=0)

    def say(**msg):
        out.write((json.dumps(msg) + "\n").encode())

    def stop(*_):
        _cleanup_module(module)
        os._exit(130)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    class Result(unittest.TextTestResult):
        def __init__(self):
            super().__init__(unittest.runner._WritelnDecorator(sys.stderr), True, 0)

        def startTest(self, test):
            if signal.getsignal(signal.SIGTERM) is not stop:  # (a test can leave a handler of its own behind)
                signal.signal(signal.SIGTERM, stop)
            super().startTest(test)
            if isinstance(test, unittest.TestCase):
                say(t="start", name=_name(test))

        def _said(self, test, flavour=None, err=None):
            text = None
            if err is not None:
                text = f"{self.separator1}\n{flavour}: {self.getDescription(test)}\n{self.separator2}\n{self._exc_info_to_string(err, test)}"
            counted = isinstance(test, unittest.TestCase)  # (a setUpClass/tearDownModule error isn't a test)
            say(t="test", id=test.id() if counted else None, name=_name(test), text=text)

        def addSuccess(self, test):
            super().addSuccess(test)
            self._said(test)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._said(test, "FAIL", err)

        def addError(self, test, err):
            super().addError(test, err)
            self._said(test, "ERROR", err)

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._said(test)

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            self._said(test)

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self._said(test)

    loader = unittest.TestLoader()

    class Asked(unittest.TestSuite):
        """The classes as they're sent: one suite for the worker's whole life, so the module is set up and torn
        down once (its fake servers, its temporary folder), and each class once."""
        _cleanup = False

        def __iter__(self):
            while True:
                say(t="want")
                line = jobs.readline()
                if not line:
                    return
                yield loader.loadTestsFromNames(json.loads(line))

        def _removeTestAtIndex(self, index):
            pass

    result = Result()
    Asked()(result)
    jobs.close()
    out.close()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--worker"]:
        return worker(args[1])
    jobs = os.environ.get("PLEXCACHE_TEST_JOBS") or ""
    rest, excluded = [], set()
    while args:
        a = args.pop(0)
        if a in ("-j", "--jobs") and args:
            jobs = args.pop(0)
        elif a == "--exclude" and args:
            excluded.add(args.pop(0))
        elif a.startswith("-j") and a[2:].isdigit():
            jobs = a[2:]
        else:
            rest.append(a)
    module = rest[0] if rest else "tests.test_plexcache"
    try:
        jobs_n = max(1, int(jobs)) if jobs else 2 * (os.cpu_count() or 1)  # (two per core: most tests wait more than they compute)
    except ValueError:
        print(f"    -j takes a number of workers, not {jobs!r}")
        return 2
    started = time.time()
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    if excluded:
        suite = _without(suite, excluded)
    total = suite.countTestCases()
    tty = drawn(sys.stdout)
    os.environ.pop("PLEXCACHE_PROGRESS", None)  # (this run's, not the tests': one that runs tests/run.py itself says its own)
    units = _units(suite) if jobs_n > 1 else None
    if not units or len(units) < 2:
        return run_here(module, suite, total, tty, started)
    del suite  # (the workers load their own)
    # what importing the module set up here (its fake servers, its temporary folder) is the workers' now: taken down
    for name in {u[1][0].rsplit(".", 2)[0] for u in units}:  # (a test's id: module.Class.test)
        mod = sys.modules.get(name)
        try:
            getattr(mod, "tearDownModule", lambda: None)()
        except Exception:  # noqa: BLE001 — (the workers' runs say what's wrong)
            pass
        _cleanup_module(name)
    return run_parallel(module, units, total, jobs_n, tty, started)


if __name__ == "__main__":
    sys.exit(main())

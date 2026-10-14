"""Run the test suite with a progress bar, so you can see it isn't stuck.

    python3 tests/run.py

On a terminal, one line is redrawn as tests finish:

    [##########----------]  59/118  ServerTests.test_export_has_no_secrets

When the output isn't a terminal (CI, a log file), it prints a line every 10%.
Failures are listed in full at the end, as unittest normally does, and the
exit code is non-zero if anything failed.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

BAR = 24
GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


class ProgressResult(unittest.TextTestResult):
    def __init__(self, stream, descriptions, verbosity, total: int, tty: bool):
        super().__init__(stream, descriptions, 0)
        self.total = total
        self.tty = tty
        self.done = 0
        self.next_report = 10

    def _draw(self, test):
        self.done += 1
        pct = self.done / self.total if self.total else 1
        bad = len(self.failures) + len(self.errors)
        name = f"{type(test).__name__}.{getattr(test, '_testMethodName', '')}"
        if self.tty:
            filled = int(BAR * pct)
            colour = RED if bad else GREEN
            bar = f"{colour}{'#' * filled}{OFF}{DIM}{'-' * (BAR - filled)}{OFF}"
            note = f"  {RED}{bad} failed{OFF}" if bad else ""
            width = os.get_terminal_size(sys.stdout.fileno()).columns if sys.stdout.isatty() else 100
            # Keep the line narrower than the terminal: a line that wraps can't be redrawn with \r,
            # so each update would leave a broken copy of the bar behind.
            visible = len(f"    [{'#' * BAR}] {self.done:>3}/{self.total}") + (len(f"  {bad} failed") if bad else 0) + 2
            room = width - visible - 1
            name = name if len(name) <= room else (name[: max(0, room - 1)] + "…" if room > 1 else "")
            line = f"    [{bar}] {self.done:>3}/{self.total}{note}  {DIM}{name}{OFF}"
            sys.stdout.write("\r\033[K" + line)
            sys.stdout.flush()
        elif pct * 100 >= self.next_report or self.done == self.total:
            print(f"    {self.done}/{self.total} tests done" + (f", {bad} failed" if bad else ""), flush=True)
            self.next_report += 10

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


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_plexcache")
    total = suite.countTestCases()
    tty = sys.stdout.isatty()
    started = time.time()
    runner = unittest.TextTestRunner(
        stream=sys.stdout, verbosity=0,
        resultclass=lambda stream, desc, verb: ProgressResult(stream, desc, verb, total, tty))
    # TextTestRunner prints its own summary; keep ours instead.
    result = runner._makeResult()
    unittest.registerResult(result)
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
        print(f"\n    {RED if tty else ''}{bad} of {result.testsRun} tests failed{OFF if tty else ''} ({took:.0f} s)")
        return 1
    print(f"    {result.testsRun} tests passed ({took:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

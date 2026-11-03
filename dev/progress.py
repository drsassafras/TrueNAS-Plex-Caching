"""The progress bar the dev tools share: tests/run.py's (a test at a time) and dev/upkeep.py's (a dependency at a time),
so a long step never looks stuck.

    ▕██████████▏             ▏  59/118  ServerTests.test_export_has_no_secrets
    ▕█████                   ▏   5/24  looking up ffmpeg at ffmpeg.org  (12 s)

Its cells fill an eighth at a time (▏▎▍▌▋▊▉█) over a faint track; in a locale that isn't UTF-8, ``[#####-----]``.

On a terminal, one line is redrawn: ``draw`` when a step finishes (the count moves on), ``show`` for the step under way
(the count stays; with ``ticking``, the seconds it has taken are added every second, so a slow download or npm install
is seen to be going). When the stream isn't a terminal (CI, a log file; unless PLEXCACHE_PROGRESS=bar: one later) it prints a line every 10% instead, and ``show``
says nothing. ``clear`` takes the line away, before what's printed next.

Under tnsync's board (dev/board.py, PLEXCACHE_PROGRESS=feed) nothing is drawn here: each change goes to the stream as one
line the board reads (``FEED`` and a JSON record, ``record``), and the board draws the bar, in its section, with this same
code (``apply``, then ``render``). ``bar`` names it, for a tool with more than one (dev/future.py: the build, the tests).
"""

import json
import locale
import os
import re
import sys
import threading
import time

BAR = 24
GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
TRACK = "\033[48;5;237m"   # (the bar's empty part on a terminal: a faint track; on the board, its block's colour darkened)
EIGHTHS = " ▏▎▍▌▋▊▉"      # (the cell being filled: 1/8 to 7/8 of it)
GLYPHS = locale.getpreferredencoding(False).lower().replace("-", "") == "utf8"   # (else # and -: a locale without them)
FEED = "\x1e"   # (ASCII's record separator: what starts a line meant for the board, not for reading)


def feeding() -> bool:
    """Whether the bars go to tnsync's board (PLEXCACHE_PROGRESS=feed) rather than being drawn here."""
    return os.environ.get("PLEXCACHE_PROGRESS") == "feed"


def timed(took: str, *more: str, feed: bool | None = None) -> str:
    """What follows a result line: " (58 s, 16 workers)" — on tnsync's board nothing: its block shows the time, and
    the bar's ``workers`` (under the time)."""
    return "" if (feeding() if feed is None else feed) else f" ({', '.join([took, *more])})"


def counted(n: int, unit: str, plural: str = "", of: int | None = None) -> str:
    """A count as every dev tool says it: "1 test", "550 tests", "3/550 tests" — never "3 of 550" (``plural`` when it
    isn't unit + s)."""
    many = plural or unit + "s"
    return f"{n}/{of} {many if of != 1 else unit}" if of is not None else f"{n} {unit if n == 1 else many}"


def short_version(v: str) -> str:
    """A version as the board's result lines say it: a release or a pre-release's own tag as it is (3.16.0a1, 24.0.0-rc.1,
    2.0.0-beta.1), the build stamp of a nightly or dev build left off ("28.0.0-nightly202611022758c59105" → "28.0.0-nightly",
    "1.2-dev-20261102-abc123" → "1.2-dev", "+git.abc" dropped), and a git snapshot — ffmpeg's "N-121234-g1a2b3c4d" —
    "snapshot"."""
    v = v.split("+", 1)[0]
    if re.match(r"(N-\d+-g[0-9a-f]+|git-)", v):
        return "snapshot"
    return re.sub(r"-(nightly|dev|canary|snapshot)[-.]?\w*([-.][0-9a-f]+)*$", r"-\1", v)


class Shown(str):
    """A count the bar already shows, on tnsync's board: its outcome only, and only when the line has nothing else."""


def tally(n: int, unit: str, outcome: str, plural: str = "", of: int | None = None, feed: bool | None = None) -> str:
    """A count and what came of it, the fact of a result line: "550 tests passed", "3/550 tests failed". On tnsync's
    board the bar says the count already: the whole of it (n of n) is left out — "Python 3.15.2 · …", or just
    "Up to date" when there's nothing else to say — and part of it is said without the total: "3 failed"."""
    if not (feeding() if feed is None else feed):
        return f"{counted(n, unit, plural, of)} {outcome}"
    if of is not None and n != of:
        return f"{n} {outcome}"
    return Shown(outcome)


def result(*facts: str) -> str:
    """A section's result line, as every dev tool says it on tnsync's board (its last line) and after a run: its facts
    joined with " · ", in sentence case, no full stop — "550 tests passed", "0 errors · 0 warnings". No time (the block's
    timer has it), no workers (its bar's row has them: ``timed`` adds those off the board) and no count the bar shows
    (``tally``)."""
    facts = tuple(f for f in facts if f and f.strip())
    if any(not isinstance(f, Shown) for f in facts):   # (the bar's count: left out when there's anything else)
        facts = tuple(f for f in facts if not isinstance(f, Shown))
    text = " · ".join(f.strip().rstrip(".") for f in facts)
    return text[:1].upper() + text[1:]


def record(line: str) -> dict | None:
    """The board's side: the record a line of a tool's output carries, None for a line that's just output."""
    if not line.startswith(FEED):
        return None
    try:
        got = json.loads(line[len(FEED):])
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def said(text: str, stream=None):
    """On tnsync's board, the line under a section's bar: what came of it, short (dev/upkeep.py: what moved, from and to).
    Sent only when feeding; elsewhere the tool says it its own way."""
    if feeding():
        stream = stream or sys.stdout
        stream.write(FEED + json.dumps({"said": text}) + "\n")
        stream.flush()


def then(n: int, stream=None):
    """On tnsync's board, for a section of two steps (dev/upkeep.py: look, then move the pins once the checks are done):
    how much the second has to do. 0: there's nothing for it, and the section ends with the first."""
    if feeding():
        stream = stream or sys.stdout
        stream.write(FEED + json.dumps({"then": n}) + "\n")
        stream.flush()


def drawn(stream) -> bool:
    """Whether the bar is drawn on this stream (else a line every 10%): a terminal — or one later, PLEXCACHE_PROGRESS=bar
    (tnsync's log of dev/future.py's run, shown on its terminal once the current tests are done)."""
    if os.environ.get("PLEXCACHE_PROGRESS") == "bar":
        return True
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


class Progress:
    def __init__(self, total: int, tty: bool | None = None, stream=None, unit: str = "tests done", bar: str = "main",
                 feed: bool | None = None):
        self.stream = stream or sys.stdout
        self.total = total
        self.feed = feeding() if feed is None else feed   # (to the board: a record each change, nothing drawn)
        self.bar = bar
        self.tty = False if self.feed else self._isatty() if tty is None else tty
        self.unit = unit
        self.done = 0
        self.bad = 0
        self.next_report = 10
        self.label = ""
        self.since: float | None = None   # when the step under way started (ticking)
        self.workers: int | None = None   # (how many run it at once: tests/run.py's; on the board, at its bar's right)
        self.finished = ""   # (on the board, its label once done: what it counted and what did it — "dependencies · checked against upstream")
        self.lock = threading.RLock()
        self.ticker: threading.Thread | None = None

    def _isatty(self) -> bool:
        return drawn(self.stream)

    def _width(self) -> int:
        try:
            return os.get_terminal_size(self.stream.fileno()).columns or 100  # (0: a terminal that doesn't say)
        except (AttributeError, ValueError, OSError):
            return int(os.environ.get("COLUMNS") or 0) or 100  # (a log shown on a terminal later: tnsync says how wide)

    def render(self, width: int, track: str = TRACK, fill: str | None = None, alert: str = RED) -> str:
        """The bar's line, narrower than ``width`` (the board draws it in its section, on its block's ``track`` with its
        ``fill`` and ``alert`` colours; ``_line`` on the terminal)."""
        pct = min(1.0, self.done / self.total if self.total else 1)
        colour = fill or (RED if self.bad else GREEN)
        if GLYPHS:
            eighths = int(BAR * 8 * pct)
            cells = "█" * (eighths // 8) + (EIGHTHS[eighths % 8] if eighths % 8 else "")
            bar = f"▕{track}{colour}{cells}{' ' * (BAR - len(cells))}{OFF}▏"
        else:
            filled = min(BAR, int(BAR * pct))
            bar = f"[{colour}{'#' * filled}{OFF}{DIM}{'-' * (BAR - filled)}{OFF}]"
        note = f"  {alert}{self.bad} failed{OFF}" if self.bad else ""
        secs = f"  ({time.time() - self.since:.0f} s)" if self.since is not None else ""
        # Keep the line narrower than the terminal: a line that wraps can't be redrawn with \r,
        # so each update would leave a broken copy of the bar behind.
        visible = len(f"    [{'#' * BAR}] {self.done:>3}/{self.total}") + (len(f"  {self.bad} failed") if self.bad else 0) + 2
        room = width - visible - 1
        secs = secs if room - len(secs) > 1 else ""
        room -= len(secs)  # (the seconds stay in sight: it's the label that's cut short)
        name = self.label if len(self.label) <= room else (self.label[: max(0, room - 1)] + "…" if room > 1 else "")
        name += secs
        return f"    {bar} {self.done:>3}/{self.total}{note}  {DIM}{name}{OFF}"

    def _line(self):
        self.stream.write("\r\033[K" + self.render(self._width()))
        self.stream.flush()

    def _feed(self):
        rec = {"bar": self.bar, "done": self.done, "total": self.total, "bad": self.bad, "label": self.label,
               "ticking": self.since is not None, "unit": self.unit,
               "age": round(time.time() - self.since, 1) if self.since is not None else 0}
        if self.workers:
            rec["workers"] = self.workers
        self.stream.write(FEED + json.dumps(rec) + "\n")
        self.stream.flush()

    def apply(self, rec: dict):
        """The board's side of ``_feed``: this bar as the tool's record says (its seconds counted from when it said so)."""
        with self.lock:
            ticking = bool(rec.get("ticking"))
            label = str(rec.get("label", ""))
            if not ticking:
                self.since = None
            elif self.since is None or label != self.label:   # (counted from when it started, by the tool's clock)
                age = rec.get("age")
                self.since = time.time() - (age if isinstance(age, (int, float)) else 0)
            self.label = label
            for key in ("done", "total", "bad"):
                if isinstance(rec.get(key), int):
                    setattr(self, key, rec[key])
            self.unit = str(rec.get("unit") or self.unit)
            if isinstance(rec.get("workers"), int):
                self.workers = rec["workers"]

    def draw(self, name: str, bad: int | None = None):
        """A step finished: the count moves on, its name shown."""
        with self.lock:
            self.done += 1
            if bad is not None:
                self.bad = bad
            self.label, self.since = name, None
            pct = self.done / self.total if self.total else 1
            if self.feed:
                self._feed()
            elif self.tty:
                self._line()
            elif pct * 100 >= self.next_report or self.done == self.total:
                self.stream.write(f"    {self.done}/{self.total} {self.unit}" + (f", {self.bad} failed" if self.bad else "") + "\n")
                self.stream.flush()
                while self.next_report <= pct * 100:
                    self.next_report += 10

    def at(self, done: int, total: int, label: str, ticking: bool = False):
        """Where a step that counts its own stands (dev/future.py's image build: step 7 of 19), redrawn or sent on."""
        with self.lock:
            self.done, self.total = done, total
        self.show(label, ticking)

    def show(self, label: str, ticking: bool = False, since: float | None = None):
        """The step under way (the count stays): with ``ticking``, its seconds so far (from ``since``: when it started, if
        not now), redrawn every second."""
        with self.lock:
            self.label = label
            self.since = (since or time.time()) if ticking else None
            if self.feed:
                self._feed()  # (the board counts the seconds)
                return
            if not self.tty:
                return
            self._line()
            if ticking and self.ticker is None:
                self.ticker = threading.Thread(target=self._tick, name="progress", daemon=True)
                self.ticker.start()

    def _tick(self):
        while True:
            time.sleep(1)
            with self.lock:
                if self.since is None:  # (the step is over: a new one starts its own)
                    self.ticker = None
                    return
                self._line()

    def reset(self, total: int, unit: str | None = None):
        """A new count, from 0 (dev/upkeep.py: the dependencies looked up, then the pins moved)."""
        with self.lock:
            self.clear()
            self.total, self.done, self.bad, self.next_report, self.label = total, 0, 0, 10, ""
            self.unit = unit or self.unit
            if self.feed:
                self._feed()

    def clear(self):
        with self.lock:
            self.since = None
            if self.feed:   # (on the board the bar stays: its label what it counted and what did it, once done)
                self.label = self.finished or self.unit
                self._feed()
            elif self.tty:
                self.stream.write("\r\033[K")
                self.stream.flush()

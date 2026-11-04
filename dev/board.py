#!/usr/bin/env python3
"""tnsync's board: its steps run at once, each in a section of its own that's redrawn as it goes.

    python3 dev/board.py <plan.json> <results>

tnsync writes the plan ({"header": [its lines: what this run is], "commit": "a1b2c3d + uncommitted" (after the first
line, the first thing cut when there's no room), "keys": [{"key": "Enter", "does": "Cancel"}: the keys it offers, said
"[Enter] Cancel" on the header's next line with each section's key while it works, the tally of sections done on its
right], "sections": [...]}, or just the list) and reads the results (KEY=value lines) when the board is done; each
section's output is kept beside the results (their folder's logs/, which tnsync removes):

    {"id": "current", "title": "Current Tests", "cmd": "python3 tests/run.py", "cwd": "/the/code",
     "after": ["…"],        # (optional) starts only once these have passed; one that didn't (or a test of it failed): not run
     "stop_with": ["…"],    # (optional) stopped when one of these fails, at its first failing test (the future tests, with the current ones)
     "stoppable": true,     # (optional) Enter stops it (the tests; a copy under way is left to finish)
     "warn": {"rc": 1, "note": "…", "more": {"3": "…"}},   # (optional) that exit code is a warning (orange: the style guide's looks to approve), said in its block alone
     "not_run_rc": 3,       # (optional) the exit code that means "couldn't be set up" (yellow: nothing is known)
     "upkeep": true,        # (optional) the dependencies: its output's first line says current / behind / cves / else
     "key": {"key": "r", "cmd": "…", "cwd": "…", "does": "Review Style Changes", "opens": "the review page",
             "line": "press r to review them"},   # (optional) while the board runs, whatever the section's state
                            # (waiting, running or done), that key starts cmd (the style guide's review page), on the
                            # terminal but in a session of its own, so it outlives the board; it says what it has to
                            # say to a file of its own. Once it has warned, its last line says "line" in place of what
                            # follows its last ": " while the key works (the command to run instead, once the board
                            # is done)
     "finale": {"cmd": "…", "check": "…", "doing": "…", "label": "…", "enter": "…", "pass_note": "…"},   # (optional,
                            # Section's: its last step, once the rest is done, on a terminal)
     "then": {"cmd": "…", "after": ["…"],   # (optional) a second command, once those are done, however they went (after
              "cwd": "…", "doing": "…",     # []: at once), the first's result standing when it passes; its own folder, what
              "fail_note": "…"}}            # the header says while it runs, and when it fails, the first's result with that

Each section is a block — blue while it runs (or waits), then green, orange, red or grey (COLOUR) — with the bars its command sends
(dev/progress.py's, PLEXCACHE_PROGRESS=feed: drawn here with the same code) and the last line it printed. Its full
output is kept; a section that fails has it printed under the board at the end, as it came. Enter stops the sections
that can be stopped (the results say ``aborted=1``); a section's key, pressed at any time while the board runs (the
section still waiting or running, or done), starts its command. Without a terminal, a line as each section starts and ends.

The results, per section: ``<id>=pass|fail|warn|notrun|stopped|skipped`` (warn: the dependencies behind, or not
checked), ``<id>_rc=<exit code>``, ``<id>_log=<its output, a file>``, ``<id>_last=<its last line>``.
"""

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:  # (run as a script: dev/ is on the path, not the project)
    sys.path.insert(0, ROOT)

from dev.progress import DIM, OFF, RED, Progress, capsule, counted, fields, record, wider  # noqa: E402 — the bar the dev tools share

# blue: under way (running, or waiting its turn); then what came of it — green passed, orange a warning (stopped with
# Enter, the dependencies behind), red failed, grey for the record (not run, skipped, already in sync): tnsync's colours
BLUE = WAIT = "\033[1;97;48;2;10;90;128m"   # (Prussian blue #0A5A80, truecolour: running or waiting alike)
GREY, GOOD, MEH, BAD = "\033[1;97;48;5;240m", "\033[1;97;48;5;22m", "\033[1;30;48;5;214m", "\033[1;97;48;5;88m"
COLOUR = {"running": BLUE, "waiting": WAIT, "pass": GOOD, "warn": MEH, "stopped": MEH, "fail": BAD,
          "notrun": GREY, "skipped": GREY, "info": GREY}
HEAD = "\033[1;97;48;2;11;74;74m"   # (the board's header, deep teal #0B4A4A: what this run is, how to stop it)
OK = ("pass", "notrun", "info")   # (what a section waiting on another counts as done well enough to go on)
BODY = {k: v.replace("[1;", "[") for k, v in COLOUR.items()}   # (the block under the header: the same colours, not bold)
# a bar's track: its block's colour darkened 30% toward black (blue #073F59, forest #004200, yellow #B27A00, red #5E0000,
# grey #3D3D3D), its cells in the block's text colour (white; black on yellow)
SHADE = {BLUE: "\033[48;2;7;63;89m", GOOD: "\033[48;2;0;66;0m", MEH: "\033[48;2;178;122;0m", BAD: "\033[48;2;94;0;0m",
         GREY: "\033[48;2;61;61;61m"}
TRACK = {k: SHADE[v] for k, v in COLOUR.items()}
# its cells in deep grey #2E2E2E on yellow (black there was as heavy as the counts' capsule beside it)
FILL = {k: "\033[38;2;46;46;46m" if v == MEH else "\033[97m" for k, v in COLOUR.items()}
# a run of tests' counts (dev/progress.py's capsule): on a deep shade of its block's colour, the four at one depth —
# blue #0B3A52, green #0B3F0B, amber #5A3A00, red #520808, grey #2A2A2A
CAPSULES = {BLUE: "\033[48;2;11;58;82m", GOOD: "\033[48;2;11;63;11m", MEH: "\033[48;2;90;58;0m", BAD: "\033[48;2;82;8;8m",
            GREY: "\033[48;2;42;42;42m"}
CAPSULE = {k: CAPSULES[v] for k, v in COLOUR.items()}
HEAD_CAPSULE = "\033[48;2;6;46;46m"   # (the header's tally likewise, on deep teal #062E2E)
ROWS = 3   # each section's lines, kept from the start
TITLE = 24   # a block's title column: its status starts after it (and the header's commit, under it)
SAID = {"waiting": "waiting", "running": "running", "pass": "passed", "warn": "check above", "notrun": "not run",
        "skipped": "skipped", "info": "done",
        "stopped": "stopped", "fail": "FAILED"}
def status(text: str) -> str:
    """A block's status as every block says it: in capitals, all of it ("STOPPED: CURRENT TESTS FAILED"). So it's kept
    short — a few words, no host, path or option that capitals would change; what explains it goes in its last line."""
    return text.upper()


ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]|\033\][^\a]*\a")
WIDTH = 100     # (the board's widest; a narrower terminal gets a narrower board)
FRAME = 0.25    # seconds between redraws


def plain(text: str) -> str:
    """A line as it reads: colours gone, and of a line redrawn with \\r, what it ended as."""
    return ANSI.sub("", text.rsplit("\r", 1)[-1]).replace("\t", "  ").strip()


class Section:
    def __init__(self, spec: dict, logs: str):
        self.id = spec["id"]
        self.title = spec["title"]
        self.cmd = spec["cmd"]
        self.cwd = spec.get("cwd") or os.getcwd()
        self.env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        self.after = list(spec.get("after") or [])
        self.stop_with = list(spec.get("stop_with") or [])
        self.stoppable = bool(spec.get("stoppable"))
        self.not_run_rc = spec.get("not_run_rc")
        warn = spec.get("warn") or {}   # (optional) {"rc": 1, "note": "…"}: that exit code is a warning (orange), its output not printed
        self.warn_rc, self.warn_note = warn.get("rc"), warn.get("note")
        # (and other exit codes that are warnings too, each its own note: {"3": "not measured"})
        self.warn_more = {int(k): str(v) for k, v in (warn.get("more") or {}).items()}
        # (optional) its last step, once every other section is done, it's ended in a warning and none has failed, on a
        # terminal: an action of its own run as a step (blue: "doing", its bar's label "label") — tnsync's review page,
        # open until it's closed — then its own check again ("check", with "env"), which gives its end state; Enter skips it
        self.finale: dict = dict(spec.get("finale") or {})
        self.final_phase = 0   # 0 not yet, 1 the action, 2 the check again, 3 done (or skipped)
        self.kept: tuple = ()  # (its note and last line before the action: what a skip puts back)
        self.upkeep = bool(spec.get("upkeep"))
        self.doing = spec.get("doing")   # (what its header says while it's under way, for "running")
        self.info = spec.get("info")   # (a section that only tells: grey when it's done, this its header)
        self.then: dict = dict(spec.get("then") or {})
        self.key = dict(spec.get("key") or {})   # (its key, while the board runs: {"key", "cmd", "cwd", "does", "opens"})
        self.helper: subprocess.Popen | None = None   # (what its key started)
        self.live = False   # (the board is running and reads keys: its key works)
        self.why = ""       # (its key's command's errors, a file)
        self.key_said = ""  # (why its key's command didn't start: its last line)
        self.opened = ""    # (how its key's page stands, in its header: "is open", "has closed", "didn't open")
        self.state = "waiting"
        self.note = ""                # (why it's waiting, stopped or not run)
        self.rc: int | None = None
        self.proc: subprocess.Popen | None = None
        self.started: float | None = None
        self.ended: float | None = None
        self.bars: dict[str, Progress] = {}
        self.last = ""
        self.buf = b""
        self.log = os.path.join(logs, f"{self.id}.log")
        self.out = open(self.log, "w", encoding="utf-8", errors="replace")  # noqa: SIM115 — (kept open while it runs)
        self.phase = 0                # 1: its "then" command
        self.reported = False         # (what it had to say beyond its block: said)
        self.telling: list[str] = []  # (failures said as they came, not yet put above the board)
        self.live_tell = False        # (the board on a terminal: those go above it at once)
        self.first = ""               # (the dependencies: the first line of the command that counts)
        self.looked = ""              # (the first line of its first step's)
        self.then_work: int | None = None   # (what its second step has to do, as its first said: 0, it isn't run)
        self.then_failed = False            # (its second step failed, and said so: then's fail_note)
        self.issues: list[dict] = []        # (what it found, as it said: {"level", "text", "fix"} — dev/progress.issue)
        self.said: str | None = None   # (what the tool said came of it, for the line under its bar: dev/progress.said)

    @property
    def done(self) -> bool:
        return self.state not in ("waiting", "running")

    @property
    def failing(self) -> bool:
        """Whether it has failed, or will: ended red, or still running with a failure on one of its bars (a test of the
        suite's failed). What waits on it, or stops with it, needn't wait for the rest."""
        return self.state == "fail" or (self.state == "running" and any(b.bad for b in self.bars.values()))

    def failed_note(self) -> str:
        """What a section it held up or stopped says of it: "Current Tests failed", or "Current Tests failing" (a test of
        it has failed, and it runs on)."""
        return f"{self.title} failed" if self.state == "fail" or not self.bars else f"{self.title} failing"

    @property
    def shown(self) -> str:
        """The state its block is drawn in: red when its key's command failed (the review page didn't open), however it
        went itself (its result stays what it was); while it runs, red from its first failure and orange from its first
        warning (a run of tests' bar: it needn't end for its colour to say so); once passed, orange when it printed
        warnings (its result a pass all the same)."""
        if self.key_said:
            return "fail"
        if self.state == "running" and self.failing:
            return "fail"
        if self.state in ("running", "pass") and self.warned:
            return "warn"
        return self.state

    @property
    def warned(self) -> int:
        """The warnings its run of tests printed (its bar's count): 0 for anything else."""
        return sum(b.warned for b in self.bars.values() if b.tests)

    @property
    def keyed(self) -> bool:
        """Whether its key does something now: while the board runs, however the section stands — still waiting or
        running (the board loading), or done — so its page opens at any time, not only once it has warned."""
        return bool(self.key) and self.live

    def press(self, tty: int):
        """Its key: its command, on the terminal (the board's stdin and stdout: the review page won't open without
        one) in a session of its own, so it stays open after the board, and Enter or Ctrl-C on the board don't reach it."""
        if self.helper is not None and self.helper.poll() is None:
            return
        env = {k: v for k, v in os.environ.items() if k != "PLEXCACHE_PROGRESS"}
        self.why = self.log[: -len(".log")] + ".key.log"   # (what it says if it can't start: its header says it)
        with open(self.why, "w", encoding="utf-8") as why:
            self.helper = subprocess.Popen(["bash", "-c", self.key["cmd"]], cwd=self.key.get("cwd") or self.cwd, env=env,
                                           stdin=tty, stdout=tty, stderr=why, start_new_session=True)
        self.opened, self.key_said = "is open", ""

    def helped(self):
        """Its key's command, once it has ended: said in its header."""
        if self.helper is None or self.helper.poll() is None:
            return
        rc, self.helper = self.helper.returncode, None
        said = ""
        try:
            with open(self.why, encoding="utf-8", errors="replace") as fh:
                said = next((plain(x) for x in reversed(fh.read().splitlines()) if plain(x)), "")
        except OSError:
            pass
        self.opened = "has closed" if rc == 0 else "didn't open"
        self.key_said = "" if rc == 0 else said or f"(exit {rc})"   # (why, in its last line)

    def start(self, cmd: str, cwd: str | None = None, more: dict | None = None):
        env = dict(os.environ, PLEXCACHE_PROGRESS="feed", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", **self.env, **(more or {}))
        self.proc = subprocess.Popen(["bash", "-c", cmd], cwd=cwd or self.cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        os.set_blocking(self.proc.stdout.fileno(), False)  # type: ignore[union-attr]
        self.state = "running"
        self.note = ""
        if self.started is None:
            self.started = time.time()

    def take(self, chunk: bytes):
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self.line(line.decode("utf-8", "replace"))
        # (a bar redrawn with \r and no newline yet: its latest state still shows)
        if self.buf and b"\x1e" not in self.buf:
            text = plain(self.buf.decode("utf-8", "replace"))
            if text:
                self.last = text

    def line(self, text: str):
        rec = record(text)
        if rec is not None and "said" in rec:   # (the line under its bar: what came of it, as the tool puts it)
            self.said = str(rec["said"])
            return
        if rec is not None and isinstance(rec.get("issue"), dict):   # (something it found: in the board's Issues at the end)
            self.issues.append({k: str(rec["issue"].get(k) or "") for k in ("level", "text", "fix")})
            return
        if rec is not None and isinstance(rec.get("tell"), str):   # (a failure as it comes: above the board now, on a terminal)
            if self.live_tell:
                self.telling.append(rec["tell"])
            else:   # (no terminal: with the rest of what it printed, once it's done)
                self.out.write(rec["tell"] + "\n")
            return
        if rec is not None and isinstance(rec.get("then"), int):   # (what its second step has to do: 0, nothing)
            self.then_work = rec["then"]
            return
        if rec is not None:
            name = str(rec.get("bar") or "main")
            bar = self.bars.pop(name, None) or Progress(0, tty=True, feed=False, bar=name)
            self.bars[name] = bar   # (the last one moved is the one shown)
            bar.apply(rec)
            return
        self.out.write(text + "\n")
        shown = plain(text)
        if shown:
            self.last = shown
            if self.upkeep and self.phase == (1 if self.then else 0) and not self.first:
                self.first = shown
            if self.upkeep and self.phase == 0 and not self.looked:
                self.looked = shown   # (the first step's first line: what counts when it's the only step)

    def finished(self, rc: int):
        if self.buf:
            self.line(self.buf.decode("utf-8", "replace"))
            self.buf = b""
        self.release()
        if self.state in ("stopped", "skipped"):
            self.rc = rc
            return
        if self.final_phase == 1:   # (its action done: its check again next, the end state its)
            self.final_phase, self.phase, self.state = 2, 2, "waiting"
            return
        if self.final_phase == 2:
            self.final_phase = 3
        if self.then and self.phase == 0 and self.then_work == 0:   # (nothing for the second step: it ends with the first)
            self.then, self.first = {}, self.first or self.looked
        if self.then and self.phase == 0:   # (the first command's end: the second waits its turn, its output the log)
            self.phase = 1
            self.state = "waiting"
            self.bars = {}   # (the second step's bars its own)
            self.rc = rc
            self.out.close()
            self.log = self.log[: -len(".log")] + ".then.log"
            self.out = open(self.log, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
            return
        if self.phase == 1 and not self.upkeep:   # (a second step after one with a result of its own: the first's stands —
            if rc != 0 and self.then.get("fail_note"):   # the looks still waiting, the sample not made: said, not failed)
                self.then_failed, rc = True, self.rc if self.rc is not None else rc
            elif rc == 0 and self.rc:
                rc = self.rc
        self.rc = rc
        self.ended = time.time()
        if self.info is not None:   # (a section that only tells: its header the same, red when its command failed)
            self.state, self.note = "info" if rc == 0 else "fail", str(self.info)
        elif self.upkeep:
            self.state = {"current": "pass", "behind": "warn", "cves": "warn"}.get(self.first, "skipped")
            self.note = {"current": "up to date", "behind": "new versions upstream",
                         "cves": "up to date · CVEs to act on"}.get(self.first, "not checked")   # (the box, above)
            self.last = ""
        elif rc == 0:
            self.state = "pass"
            if self.final_phase == 3 and self.finale.get("pass_note"):   # (after its last step: "all approved")
                self.note = str(self.finale["pass_note"])
            elif self.warned:   # (its tests passed, with warnings: said, in orange)
                self.note = f"passed · {counted(self.warned, 'warning')}"
        elif self.warn_rc is not None and rc == self.warn_rc:   # (its block says it all: the line it said, the note)
            self.state = "warn"
            self.note = self.warn_note   # (its key: in the header's keys, and its last line)
        elif rc in self.warn_more:
            self.state = "warn"
            self.note = self.warn_more[rc]
        elif self.not_run_rc is not None and rc == self.not_run_rc:
            self.state = "notrun"
        else:
            self.state = "fail"

    def release(self):
        """Its process is done with: the pipe it wrote to closed (left open, it warns when it's dropped), and forgotten."""
        if self.proc is not None and self.proc.stdout is not None:
            self.proc.stdout.close()
        self.proc = None

    def stop(self, note: str, state: str = "stopped"):
        """Ended before its end: stopped (Enter: orange), or skipped (another's failure made it pointless: grey)."""
        if self.proc is not None:
            self.state = state
            self.note = note
            stop_group(self.proc)
        elif self.state == "waiting":
            self.state = state
            self.note = note
        self.ended = self.ended or time.time()

    # ------------------------------------------------------------- drawing
    def lines(self, width: int, widths: tuple[int, int] | None = None) -> list[str]:
        """Its block: always ROWS lines, all in its colour (so nothing moves as it fills in) — the header (what it is, how
        it stands, its time), its bar (the one it moved last: the future tests' build, then their tests) and the last line
        it printed (once it's done, its result). Its bar's capsule drawn to ``widths`` (the board's widest: Board.widths), or
        its own."""
        took = ""
        if self.started is not None:
            secs = int((self.ended or time.time()) - self.started)
            took = f"{secs // 60}:{secs % 60:02d}"
        said = status(self.note or SAID[self.state])
        if self.doing and not self.done and not self.note:   # (what it's doing: its status while it runs)
            said = status(self.doing)
        if self.phase == 1 and self.state == "running" and self.then.get("doing"):   # (its second step's, likewise)
            said = status(self.then["doing"])
        if self.final_phase in (1, 2) and not self.done:   # (its last step's: the action, then its check again)
            said = status(self.finale.get("doing" if self.final_phase == 1 else "checking", "running"))
        if self.then_failed and self.done:
            said = status(self.then["fail_note"])
        between = self.state == "waiting" and self.phase == 1 and bool(self.then.get("after"))   # (the second waits for the checks)
        if between:
            said = status("waiting: checks")
        if self.opened:   # (its key's page, however the section stands: "running: the audits page is open")
            said += status(f": {self.key.get('opens', 'it')} {self.opened}")
        left = f"  {self.title:<{TITLE}}{said}"
        room = width - len(took) - 3
        left = left[:room] if len(left) > room else left
        head = f"{COLOUR[self.shown]}{left}{' ' * (room - len(left))} {took}  {OFF}"
        bar = ""
        shown = [b for b in self.bars.values() if (b.done or b.label) and not between]   # (one reset to nothing to do, or none at work: not shown)
        if shown:
            # (on its block's track, in its text colour; a failure count red while it runs, the block's own once done)
            # its workers on the right, under the time (the bar's label cut short first)
            label = shown[-1].label
            if self.final_phase == 1 and self.finale.get("label"):   # (what's under way, while it is: the page open, say)
                shown[-1].label = str(self.finale["label"])
            n = shown[-1].workers
            workers = f"{n} worker{'s' if n != 1 else ''}" if n else ""
            room = width - 1 - (len(workers) + 3 if workers else 0)
            bar = shown[-1].render(room, TRACK[self.shown], FILL[self.shown], "" if self.done else RED, CAPSULE[self.shown], widths, indent=2, timer=False).replace(DIM, "")
            shown[-1].label = label
            if workers:
                bar += " " * (width - 2 - len(workers) - len(ANSI.sub("", bar))) + workers
        # its last line: what the tool says of it; while it runs with a bar, nothing else (the bar says how it goes:
        # no stray line, a warning on stderr say — that's kept for the failure printout); once done, what it printed last
        # (stopped or skipped part way, likewise: what it printed last is where it was, not a result)
        last = self.said if self.said is not None else "" if self.upkeep or (self.bars and (not self.done or self.state in ("stopped", "skipped"))) else self.last
        if self.keyed and self.state == "warn" and self.key.get("line") and ": " in last:   # (warned, its key working: the key, not the command)
            last = last.rsplit(": ", 1)[0] + ": " + self.key["line"]
        last = self.key_said or last
        while len(last) > width - 6 and " · " in last:   # (a result line's facts: the last, the least, left off first)
            last = last.rsplit(" · ", 1)[0]
        text = f"    {last}" if len(last) <= width - 6 else f"    {last[: width - 7]}…"
        return [head, *(self.row(x, width) for x in (bar, text))]

    def row(self, text: str, width: int) -> str:
        """A line of the block: on its colour all the way across (a colour the text sets put back after it ends)."""
        body = BODY[self.shown]
        pad = width - len(ANSI.sub("", text))
        return body + text.replace(OFF, OFF + body) + " " * max(0, pad) + OFF


def stop_group(proc: subprocess.Popen):
    """SIGTERM to its process group (tests/run.py then says how far it got), SIGKILL 2 s later if it's still there."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        return
    end = time.time() + 2
    while proc.poll() is None and time.time() < end:
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


class Board:
    def __init__(self, plan: list[dict] | dict, logs: str, tty: bool):
        if isinstance(plan, list):
            plan = {"sections": plan}
        self.header = [str(x) for x in plan.get("header") or []]   # (the board's own block on top: what this run is)
        self.commit = str(plan.get("commit") or "")   # (the commit checked: after the header's first line)
        self.offered = [dict(k) for k in plan.get("keys") or []]   # (the keys the board says under it: Enter)
        self.started: float | None = None   # (its timer, on the header's right: from the start until the last section ends)
        self.ended: float | None = None
        self.sections = [Section(s, logs) for s in plan["sections"]]
        self.by_id = {s.id: s for s in self.sections}
        self.tty = tty
        self.drawn = 0           # lines on the terminal from the last frame
        self.queued: list[str] = []   # what's to go above the board at the next frame (above)
        self.widths = (0, 0)     # (the widest capsule fields any bar has had: every bar's drawn to them, so they line up)
        self.aborted = False
        self.sel = selectors.DefaultSelector()
        self.stdin_tty = tty and sys.stdin.isatty()
        self.pressed = False     # (a section's key has been pressed)
        for s in self.sections:
            s.live = self.stdin_tty
            s.live_tell = tty

    def ready(self, s: Section) -> bool | None:
        """Whether a waiting section may start: True, None (not yet), False (never: a section it waits on didn't pass)."""
        wait = s.then.get("after", []) if s.phase == 1 and s.then else s.after
        others = [self.by_id[i] for i in wait if i in self.by_id]
        if s.phase == 0 and any(o.failing for o in others):   # (one failed, or a test of it has: never, now)
            return False
        for other in others:
            if not other.done:
                return None
            if s.phase == 0 and other.state not in OK:
                return False
        return True

    def finale_due(self, s: Section) -> bool:
        """Whether a section's last step starts now: every other one done, none failed, it ended in a warning, and the
        board on a terminal (its action is one there: a page opened in the browser)."""
        others = [o for o in self.sections if o is not s]
        return (bool(s.finale) and s.final_phase == 0 and s.state == "warn" and self.stdin_tty and not self.aborted
                and all(o.done for o in others) and not any(o.state == "fail" for o in others))

    def step(self):
        for s in self.sections:
            if self.finale_due(s):
                s.kept, s.final_phase, s.state, s.ended = (s.note, s.said), 1, "running", None
                s.note = ""
                s.start(s.finale["cmd"], s.finale.get("cwd"))
                self.sel.register(s.proc.stdout, selectors.EVENT_READ, s)  # type: ignore[union-attr]
            elif s.final_phase == 2 and s.state == "waiting" and s.proc is None:   # (its check again: at once)
                s.state = "running"
                s.start(s.finale.get("check") or s.cmd, s.finale.get("cwd"), s.finale.get("env"))
                self.sel.register(s.proc.stdout, selectors.EVENT_READ, s)  # type: ignore[union-attr]
        for s in self.sections:
            if s.state == "waiting":
                go = self.ready(s)
                if go is False:
                    short = [self.by_id[i] for i in s.after if self.by_id[i].state not in OK]
                    short = [o for o in short if o.failing] or short  # (the one that failed, not the one stopped for it)
                    early = [o for o in short if not o.done]   # (still running, a test of it failed: it said so first)
                    note = ", ".join(o.failed_note() for o in early) if early else ", ".join(o.title for o in short) + " didn't pass"
                    s.state, s.note, s.ended = "skipped", "not run: " + note, time.time()
                    if not self.tty:
                        self.said(s)
                elif go:
                    if s.phase == 1 and s.then:   # (its second step, in its own folder if it says one)
                        s.start(s.then["cmd"], s.then.get("cwd"))
                    else:
                        s.start(s.cmd)
                    self.sel.register(s.proc.stdout, selectors.EVENT_READ, s)  # type: ignore[union-attr]
                    if not self.tty:
                        print(f"==> {s.title}: {s.then['cmd'] if s.phase == 1 and s.then else s.cmd}", flush=True)
                elif self.ready_note(s):
                    s.note = self.ready_note(s)
        for s in self.sections:
            if s.state in ("running", "waiting") and s.phase == 0:
                for i in s.stop_with:
                    other = self.by_id.get(i)
                    if other is not None and other.failing:   # (at its first failing test: not once it has ended)
                        self.close(s)
                        s.stop(f"stopped: {other.failed_note()}", "skipped")
                        break

    def ready_note(self, s: Section) -> str:
        wait = s.then.get("after", []) if s.phase == 1 and s.then else s.after
        pending = [self.by_id[i].title for i in wait if i in self.by_id and not self.by_id[i].done]
        return "waiting: " + ", ".join(pending) if pending else ""

    def close(self, s: Section):
        if s.proc is not None and s.proc.stdout is not None:
            try:
                self.sel.unregister(s.proc.stdout)
            except (KeyError, ValueError):
                pass

    def read(self, timeout: float):
        if self.stdin_tty:
            try:
                self.sel.register(sys.stdin, selectors.EVENT_READ, None)
            except KeyError:
                pass
        if not self.sel.get_map():
            time.sleep(timeout)
            return
        for key, _ in self.sel.select(timeout):
            s = key.data
            if s is None:      # a key: Enter, or a section's
                self.keys(os.read(sys.stdin.fileno(), 1024).decode("utf-8", "replace"))
                continue
            try:
                chunk = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                s.take(chunk)
                continue
            self.sel.unregister(key.fileobj)
            proc = s.proc
            rc = proc.wait() if proc is not None else 0
            if proc is not None:
                s.finished(rc)
                if not self.tty and s.done:
                    self.said(s)

    def said(self, s: Section):
        """Without a terminal: a line as each section ends."""
        said = status(s.then["fail_note"]) if s.then_failed else status(s.note or SAID[s.state])
        last = s.said if s.said is not None else s.last   # (what the tool says of it, as its block's last line has it)
        print(f"    {s.title}: {said}" + (f" ({last})" if last else ""), flush=True)

    def keys(self, typed: str):
        for ch in typed:
            if ch in "\r\n":
                self.enter()
                continue
            for s in self.sections:
                if s.keyed and s.key.get("key") == ch:
                    s.press(sys.stdin.fileno())
                    self.pressed = True

    def enter(self):
        for s in self.sections:   # (a last step under way: Enter skips it, the section as it was before it)
            if s.final_phase in (1, 2) and not s.done:
                self.close(s)
                if s.proc is not None:
                    stop_group(s.proc)
                    s.proc.wait(5)
                    s.release()
                s.final_phase, s.state, s.ended = 3, "warn", time.time()
                s.note, s.said = s.kept or (s.warn_note, s.said)
                return
        if self.aborted:
            return
        self.aborted = True
        for s in self.sections:
            if s.stoppable and not s.done:
                self.close(s)
                s.stop("stopped: you pressed Enter")
            elif not s.done and s.phase == 0 and any(self.by_id[i].stoppable for i in s.after if i in self.by_id):
                if s.proc is None:
                    s.state, s.note, s.ended = "stopped", "not run: you pressed Enter", time.time()

    def collect_stopped(self):
        """A stopped section's process, once it has gone."""
        for s in self.sections:
            if s.state in ("stopped", "skipped") and s.proc is not None and s.proc.poll() is not None:
                rc = s.proc.returncode
                try:
                    rest = os.read(s.proc.stdout.fileno(), 1 << 20) if s.proc.stdout is not None else b""
                except OSError:  # (BlockingIOError: nothing left to read)
                    rest = b""
                if rest:
                    s.take(rest)
                s.finished(rc)
                if not self.tty:
                    self.said(s)

    def frame(self):
        if not self.tty:
            return
        width = min(WIDTH, shutil.get_terminal_size((WIDTH, 40)).columns - 1)
        # what a section that ended has to say goes above the board, into the terminal's scrollback; the board stays the
        # last thing on the screen, where it was
        for s in self.sections:
            self.above(self.telling(s) + self.told(s))
        said, self.queued = "".join(self.queued), []
        lines: list[str] = []
        for b in (b for s in self.sections for b in s.bars.values()):   # (no space kept for what no bar has counted)
            self.widths = wider(self.widths, fields(b.done, b.total, b.bad, b.warned))
        header = list(self.header) or [""]
        keys = list(self.offered) + ([s.key for s in self.sections if s.keyed] if not self.pressed else [])
        last = [s for s in self.sections if s.final_phase in (1, 2) and not s.done]
        if last:   # (a last step under way: Enter skips it)
            keys = [{"key": "Enter", "does": last[0].finale.get("enter", "Skip")}]
        header.insert(1, ", ".join(f"[{k['key']}] {k.get('does', 'go on')}" for k in keys))
        took = ""
        if self.started is not None:
            secs = int((self.ended or time.time()) - self.started)
            took = f"{secs // 60}:{secs % 60:02d}"
        for i, text in enumerate(header):
            body = HEAD if i == 0 else HEAD.replace("[1;", "[")
            right, drawn = (took, took) if i == 0 else self.tally()[::-1] if i == 1 else ("", "")
            if i == 1 and len(text) + len(right) + 1 > width - 4:   # (the keys first: the tally gone when there's no room)
                right = drawn = ""
            room = width - 4 - (len(right) + 1 if right else 0)
            if i == 0 and self.commit:   # (the commit after it, cut first when there's no room)
                text = f"{text:<{TITLE}}" if len(text) < TITLE else text + "   "   # (in line with the blocks' statuses)
                rest = room - len(text)
                text = (text + (self.commit if len(self.commit) <= rest else self.commit[: rest - 1] + "…" if rest > 1 else "")).rstrip()
            text = text if len(text) <= room else text[: room - 1] + "…"
            lines.append(f"{body}  {text}{' ' * (width - 4 - len(text) - len(right))}{drawn}{body}  {OFF}")
        for s in self.sections:   # (a blank line between blocks, so each stands apart)
            lines += ([""] if lines else []) + s.lines(width, self.widths)
        out = f"\033[{self.drawn}F" if self.drawn else ""
        if said:
            out += "\033[J" + said.replace("\n", "\033[K\n") + "\n"
        out += "".join(f"\r\033[K{line}\n" for line in lines)
        out += "\033[J"
        sys.stdout.write(out)
        sys.stdout.flush()
        self.drawn = len(lines)

    def tally(self) -> tuple[str, str]:
        """How far the board is, on the header's right: its sections' counts in every bar's capsule — failed,
        warnings, passed / total (" 0 │ 1 │ 6 / 7 "), no space kept (the bars' widths aren't its) — on a deep shade of the
        header's teal. Drawn, and plain."""
        failed = sum(s.state == "fail" for s in self.sections)
        warned = sum(s.state == "warn" for s in self.sections)
        return capsule(sum(s.done for s in self.sections), len(self.sections), failed, warned, HEAD_CAPSULE)

    def run(self) -> None:
        if not self.tty and self.header:
            print("\n".join([self.header[0] + (f"   {self.commit}" if self.commit else ""), *self.header[1:]]), flush=True)
        if self.tty:   # (three blank lines first: what the board says above itself later comes under them)
            sys.stdout.write("\n\n\n\033[?25l")  # (the cursor hidden while the board moves)
        self.started = time.time()
        last = 0.0
        while True:
            self.step()
            if all(s.done for s in self.sections) and not any(self.finale_due(s) for s in self.sections):
                break
            self.read(FRAME)
            self.collect_stopped()
            for s in self.sections:
                s.helped()
            if time.time() - last >= FRAME:
                self.frame()
                last = time.time()
        self.ended = max((s.ended for s in self.sections if s.ended), default=None) or time.time()   # (the timer stops)
        for s in self.sections:   # (the board done: no key works now, and its block says the command to run instead)
            s.live = False
            if s.helper is None:
                s.opened = ""
        self.frame()
        if self.tty:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()

    def kill(self):
        for s in self.sections:
            if s.proc is not None:
                stop_group(s.proc)

    def above(self, text: str) -> None:
        """The one way anything is said beside the board: on a terminal, it goes above the board at the next frame, so
        the board stays the last thing on the screen; without one, it's written as it comes."""
        if not text:
            return
        if self.tty:
            self.queued.append(text)
        else:
            sys.stdout.write(text)
            sys.stdout.flush()

    def telling(self, s: Section) -> str:
        """The failures a section said as they came (tests/run.py's "tell" records), since the last frame: above the board."""
        text, s.telling = "".join(f"\n{t.rstrip()}\n" for t in s.telling), []
        return text

    def told(self, s: Section) -> str:
        """What there is to say of a section beyond its block, once it's done: what a failed one printed, as it came (the
        tests' failures in full); the dependencies' box when they aren't all current. Said once."""
        if s.reported or not s.done:
            return ""
        s.reported = True
        if s.state != "fail" and not (s.upkeep and s.state in ("warn", "skipped")) and not s.then_failed:   # (a warning of its own: its block says it)
            return ""
        s.out.flush()
        with open(s.log, encoding="utf-8", errors="replace") as fh:
            text = fh.read().rstrip()
        if s.upkeep:   # (its first line is the word tnsync reads; the rest, the box)
            text = "\n".join(line for line in text.split("\n")[1:] if record(line) is None).strip("\n")
        lines = text.split("\n")
        if lines and plain(lines[-1]) == s.last and s.said is None:   # (its last line is in its block already: not said twice)
            text = "\n".join(lines[:-1]).rstrip()
        if not text:
            return ""
        return f"\n{text}\n"   # (just what it printed: whose it is, its block says)

    def report(self):
        """Once the board is done: what there is still to say of each section, then the Issues (every section's errors,
        warnings and notices), all through above — on a terminal, above the board, drawn once more under them."""
        for s in self.sections:
            self.above(self.telling(s) + self.told(s))
            s.out.close()
        self.above(self.issues())
        self.frame()

    def found(self, s: Section) -> list[dict]:
        """A section's issues: what it said it found; or, a section that failed (or warned) and said nothing of it,
        its result as one — so every red or yellow block is in the Issues."""
        got = list(s.issues)
        last = s.said if s.said is not None else s.last
        if s.state == "fail" and not any(i["level"] == "error" for i in got):
            got.insert(0, {"level": "error", "text": last or "failed", "fix": ""})
        elif s.state == "warn" and not got:
            got.append({"level": "warning", "text": last or status(s.note or SAID[s.state]).lower(), "fix": ""})
        return got

    def issues(self) -> str:
        """The Issues, under the board: how many of each, then each section's, one a line, worst first, a fix on its own
        line (to paste as it is); nothing when there are none."""
        from dev.progress import LEVELS
        per = [(s, sorted(self.found(s), key=lambda i: LEVELS.index(i["level"]) if i["level"] in LEVELS else 9)) for s in self.sections]
        per = [(s, f) for s, f in per if f]
        if not per:
            return ""
        every = [i for _, f in per for i in f]
        head = " · ".join(f"{n} {lv}{'s' if n != 1 else ''}" for lv in LEVELS if (n := sum(i["level"] == lv for i in every)))
        out = [f"\n  Issues: {head}"]
        for s, f in per:
            out.append(f"    {s.title}")
            for i in f:
                out.append(f"      {i['level']:<8} {i['text']}")
                if i["fix"]:
                    out.append(f"      {'':<8} {i['fix']}")
        return "\n".join(out) + "\n"

    def results(self) -> str:
        out = [f"aborted={1 if self.aborted else 0}"]
        for s in self.sections:
            out += [f"{s.id}={s.state}", f"{s.id}_rc={'' if s.rc is None else s.rc}", f"{s.id}_log={s.log}",
                    f"{s.id}_last={s.last.replace(chr(10), ' ')}", f"{s.id}_first={s.first}"]
        return "\n".join(out) + "\n"


def main() -> int:
    args = sys.argv[1:]
    if len(args) != 2:
        print((__doc__ or "").split("\n\n")[1])
        return 2
    with open(args[0], encoding="utf-8") as fh:
        plan = json.load(fh)
    logs = os.path.join(os.path.dirname(os.path.abspath(args[1])), "logs")
    os.makedirs(logs, exist_ok=True)
    tty = sys.stdout.isatty()
    board = Board(plan, logs, tty)
    old = None
    if board.stdin_tty:
        import termios
        old = termios.tcgetattr(sys.stdin.fileno())
        quiet = termios.tcgetattr(sys.stdin.fileno())
        quiet[3] &= ~(termios.ECHO | termios.ICANON)   # (a key isn't echoed, and comes as it's pressed: no Enter after it)
        quiet[6][termios.VMIN], quiet[6][termios.VTIME] = 1, 0
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, quiet)

    def ended(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, ended)
    try:
        board.run()
    except KeyboardInterrupt:
        board.kill()
        if tty:
            sys.stdout.write("\033[?25h\n")
        if old is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, old)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)  # (ended as Ctrl-C ends a command, so tnsync stops too)
        return 130
    finally:
        if old is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, old)
    board.report()
    with open(args[1], "w", encoding="utf-8") as fh:
        fh.write(board.results())
    return 0


if __name__ == "__main__":
    sys.exit(main())

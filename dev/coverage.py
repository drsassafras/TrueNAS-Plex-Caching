#!/usr/bin/env python3
"""Test coverage, held by how settled the code is (tnsync's Coverage block): nothing to install (sys.monitoring).

    python3 dev/coverage.py              # run the suite (tests/run.py --coverage), then the table and the checks
    python3 dev/coverage.py --missing    # the table with the lines that never ran
    python3 dev/coverage.py --tnsync     # the checks, from the coverage the last tests/run.py --coverage saved
    python3 dev/coverage.py --release    # the release bar too (tnsync --release: its failure is red)

What counts: every line of plexcache/ that holds code — not a docstring, nor a ``def`` or ``class`` line (they run on
import, whatever the tests do), nor the ``if __name__ == "__main__":`` tail a test imports rather than runs. Nothing is
left out beyond that: a line no test can reach is a test to write, or the maintainer's say.

git says how long each line has stood (``git blame``), so nobody marks code fresh or settled:

  - fresh: not committed, or changed in the last FRESH_DAYS days — while it's moving, one test at its seam is enough:
    each function (or server route) with a fresh line has to be reached by a test; no line target;
  - settled: unchanged for FRESH_DAYS days or more — SETTLED % of its lines run by the tests, the lines that don't listed
    by file when it's under;
  - at release (--release): RELEASE % of every line, and each file at RELEASE_FILE % or more.

The suite's run saves what it reached (tests/run.py --coverage, every worker's, in ~/.cache/plex-cache/coverage.json), so
the checks don't run it again; the coverage of other code (a change since) isn't used. Through dev/checks.py, as every check says it: what's missing one line each (file:line -
warning: …), then the result line. Exit 0 every rule met, 1 something to add (tnsync's block orange; with --release, a
release bar not met: red), 2 no coverage saved for this code.
"""

import ast
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dev import checks  # noqa: E402 — the check tools' shared code
from dev.checks import Check, Finding, Outcome  # noqa: E402

PKG = os.path.join(ROOT, "plexcache")
SAVED = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "plex-cache", "coverage.json")
DAY = 24 * 3600
FRESH_DAYS = 14      # a line changed within this many days (or not committed) is fresh
SETTLED = 98.0       # % of the settled lines the tests have to run
RELEASE = 98.0       # % of every line, at release
RELEASE_FILE = 90.0  # % of each file's lines, at release
TOOL = sys.monitoring.COVERAGE_ID


# ---------------------------------------------------------------- what's code
def _body_start(node) -> int:
    return node.body[0].lineno if node.body else node.end_lineno + 1


def code_lines(path: str) -> set[int]:
    """Every line that holds code (from the compiled file), but docstrings, ``def``/``class`` lines (to the first line of
    the body: a signature over several lines, its decorators) and the run-as-a-script tail."""
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    lines: set[int] = set()
    todo = [compile(source, path, "exec")]
    while todo:
        code = todo.pop()
        lines.update(line for _, _, line in code.co_lines() if line)
        todo.extend(c for c in code.co_consts if hasattr(c, "co_lines"))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            lines -= set(range(first, _body_start(node)))
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            doc = node.body[0]
            if isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant) and isinstance(doc.value.value, str):
                lines -= set(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__" and node.col_offset == 0):
            lines -= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


@dataclass
class Unit:
    """A function, or a route of the server (an ``if path == "/…"`` branch): what a fresh line needs a test to reach."""
    kind: str        # function, route
    name: str
    line: int
    lines: set[int]  # its own code lines (not those of a function inside it)


def _route(test) -> str:
    """The path an ``if`` picks, when it's a route: ``path == "/x"``, ``path in ("/x", …)``, ``path.startswith("/x")``."""
    if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "path" and test.comparators:
        c = test.comparators[0]
        if isinstance(c, ast.Constant) and isinstance(c.value, str):
            return c.value
        if isinstance(c, (ast.Tuple, ast.List, ast.Set)) and c.elts and isinstance(c.elts[0], ast.Constant):
            return str(c.elts[0].value)
    if (isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute) and test.func.attr == "startswith"
            and isinstance(test.func.value, ast.Name) and test.func.value.id == "path" and test.args
            and isinstance(test.args[0], ast.Constant)):
        return f"{test.args[0].value}…"
    return ""


def units(path: str, code: set[int]) -> list[Unit]:
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out: list[Unit] = []

    def span(node) -> set[int]:
        return set(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    def visit(node, owner: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{owner}.{child.name}" if owner else child.name
                inner = set().union(*(span(n) for n in ast.walk(child) if n is not child
                                      and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))))
                out.append(Unit("function", name, child.lineno, (span(child) - inner) & code))
                visit(child, name)
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{owner}.{child.name}" if owner else child.name)
            else:
                if isinstance(child, ast.If) and (route := _route(child.test)):
                    out.append(Unit("route", route, child.lineno, set().union(*(span(n) for n in child.body)) & code))
                visit(child, owner)
    visit(tree, "")
    return out


# ---------------------------------------------------------------- what the tests ran
class Tracer:
    """Every line of plexcache/ that runs, in every thread (the app does much in background threads): each line seen once,
    then no longer watched (sys.monitoring's DISABLE), so the tests run at nearly their own speed."""

    def __init__(self, tool: int = TOOL):
        self.hit: dict[str, set[int]] = {}
        self.lock = threading.Lock()
        self.tool = tool

    def start(self) -> Tracer:
        mon = sys.monitoring
        pkg = PKG + os.sep

        def on_line(code, line):
            if code.co_filename.startswith(pkg):
                with self.lock:
                    self.hit.setdefault(code.co_filename, set()).add(line)
            return mon.DISABLE
        mon.use_tool_id(self.tool, "plexcache-coverage")
        mon.register_callback(self.tool, mon.events.LINE, on_line)
        mon.set_events(self.tool, mon.events.LINE)
        return self

    def stop(self) -> dict[str, list[int]]:
        mon = sys.monitoring
        mon.set_events(self.tool, 0)
        mon.register_callback(self.tool, mon.events.LINE, None)
        mon.free_tool_id(self.tool)
        return {os.path.relpath(f, ROOT): sorted(lines) for f, lines in self.hit.items()}


def merge(parts: list[dict[str, list[int]]]) -> dict[str, list[int]]:
    out: dict[str, set[int]] = {}
    for part in parts:
        for rel, lines in part.items():
            out.setdefault(rel, set()).update(lines)
    return {rel: sorted(lines) for rel, lines in out.items()}


def save(hit: dict[str, list[int]], tests: str) -> None:
    """What the suite reached (tests/run.py --coverage), for the checks: where it ran, when, and on what code."""
    os.makedirs(os.path.dirname(SAVED), exist_ok=True)
    with open(SAVED, "w", encoding="utf-8") as fh:
        json.dump({"at": time.time(), "code": code_signature(), "tests": tests, "hit": hit}, fh)


def code_signature() -> str:
    """The code measured, as a whole: every file of plexcache/, by what's in it (tnsync's copy of the same code, wherever
    it is, is the same code)."""
    import hashlib
    h = hashlib.sha256()
    for rel in package_files():
        with open(os.path.join(ROOT, rel), "rb") as fh:
            h.update(rel.encode() + b"\0" + hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


def load() -> tuple[dict | None, str]:
    """The last saved coverage of this code, and why there's none when there isn't."""
    try:
        with open(SAVED, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return None, "no coverage saved yet: python3 tests/run.py --coverage (tnsync's Current Tests, TN_TEST) saves it"
    if got.get("code") != code_signature():
        return None, "plexcache/ has changed since the coverage was saved: python3 tests/run.py --coverage again"
    return got, ""


def package_files() -> list[str]:
    out = []
    for dirpath, dirs, files in os.walk(PKG):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        out += [os.path.relpath(os.path.join(dirpath, f), ROOT) for f in sorted(files) if f.endswith(".py")]
    return out


# ---------------------------------------------------------------- fresh and settled: git blame
def blame(rel: str, where: tuple[str, str]) -> dict[int, float | None]:
    """Each line's last change: when it was committed, or None when it isn't."""
    repo, commit = where
    got = subprocess.run(["git", "-C", repo, "blame", "--line-porcelain", *([commit] if commit else []), "--", rel],
                         capture_output=True, text=True, timeout=120)
    if got.returncode != 0:
        raise OSError(got.stderr.strip().splitlines()[-1] if got.stderr.strip() else f"git blame {rel}: exit {got.returncode}")
    out: dict[int, float | None] = {}
    line, sha = 0, ""
    for row in got.stdout.splitlines():
        if row.startswith("\t"):
            continue
        parts = row.split(" ")
        if len(parts) >= 3 and len(parts[0]) == 40 and parts[1].isdigit() and parts[2].isdigit():
            sha, line = parts[0], int(parts[2])
            out[line] = None if set(sha) == {"0"} else out.get(line)
        elif parts[0] == "committer-time" and set(sha) != {"0"}:
            out[line] = float(parts[1])
    return out


@dataclass
class File:
    rel: str
    code: set[int]
    ran: set[int]
    fresh: set[int] = field(default_factory=set)
    units: list[Unit] = field(default_factory=list)

    @property
    def settled(self) -> set[int]:
        return self.code - self.fresh


def measure(hit: dict[str, list[int]], now: float | None = None) -> tuple[list[File], str]:
    """Every file of plexcache/, its code, what ran, and which lines are fresh; and what git couldn't say."""
    now = time.time() if now is None else now
    where = checks.repo_at()
    files = []
    for rel in package_files():
        path = os.path.join(ROOT, rel)
        code = code_lines(path)
        files.append(File(rel, code, set(hit.get(rel, ())) & code, units=units(path, code)))
    if where is None:
        return files, "no git history here: every line counted as settled"

    def one(f: File):
        when = blame(f.rel, where)
        f.fresh = {n for n in f.code if when.get(n, now) is None or now - (when.get(n) or now) < FRESH_DAYS * DAY}
    try:
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(one, files))
    except (OSError, subprocess.TimeoutExpired) as e:
        for f in files:
            f.fresh = set()
        return files, f"git blame didn't run ({e}): every line counted as settled"
    return files, ""


def _pct(n: int, of: int) -> float:
    return 100.0 * n / of if of else 100.0


def _ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for n in [*nums, None]:
        if start is None:
            start = prev = n
        elif n is not None and prev is not None and n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = n
    return ", ".join(out)


# ---------------------------------------------------------------- the rules
def fresh_check(files: list[File]) -> Outcome:
    found = []
    fresh_units = 0
    for f in files:
        for u in f.units:
            if u.lines & f.fresh:
                fresh_units += 1
                if not u.lines & f.ran:
                    what = f"no test reaches the fresh {u.kind} {u.name}" if u.kind == "function" else f"no test reaches the fresh route {u.name}"
                    found.append(Finding("Coverage", f.rel, u.line, 0, "warning", what, "fresh"))
    from dev.progress import counted
    facts = [f"{counted(len(found), 'fresh function')} no test reaches" if found else f"{counted(fresh_units, 'fresh function')} reached"]
    return Outcome("Fresh code", "", found, facts=facts)


def settled_check(files: list[File]) -> Outcome:
    total = sum(len(f.settled) for f in files)
    ran = sum(len(f.settled & f.ran) for f in files)
    pct = _pct(ran, total)
    found = []
    if pct < SETTLED:
        for f in sorted(files, key=lambda f: -len(f.settled - f.ran)):
            missing = sorted(f.settled - f.ran)
            if missing:
                found.append(Finding("Coverage", f.rel, missing[0], 0, "warning",
                                     f"{len(missing)} settled lines no test runs: {_ranges(missing)}", "settled"))
    return Outcome("Settled code", "", found, facts=[f"settled {pct:.1f}% (of {SETTLED:g}%)"])


def release_check(files: list[File]) -> Outcome:
    total = sum(len(f.code) for f in files)
    ran = sum(len(f.ran) for f in files)
    pct = _pct(ran, total)
    found = []
    if pct < RELEASE:
        found.append(Finding("Coverage", "plexcache", 0, 0, "error", f"{pct:.1f}% of every line run, under the release's {RELEASE:g}%", "release"))
    for f in files:
        mine = _pct(len(f.ran), len(f.code))
        if mine < RELEASE_FILE:
            found.append(Finding("Coverage", f.rel, 0, 0, "error",
                                 f"{mine:.1f}% of its lines run, under the release's {RELEASE_FILE:g}% for each file", "release"))
    return Outcome("Release bar", "", found, facts=[f"overall {pct:.1f}% (release: {RELEASE:g}%, each file {RELEASE_FILE:g}%)"])


def table(files: list[File], missing: bool) -> None:
    print(f"{'file':38} {'lines':>6} {'ran':>6} {'cover':>7} {'fresh':>6}")
    for f in sorted(files, key=lambda f: f.rel):
        print(f"{f.rel:38} {len(f.code):6} {len(f.ran):6} {_pct(len(f.ran), len(f.code)):6.1f}% {len(f.fresh):6}")
        if missing and f.code - f.ran:
            print("    not run:", _ranges(sorted(f.code - f.ran)))
    total, ran = sum(len(f.code) for f in files), sum(len(f.ran) for f in files)
    print(f"{'TOTAL':38} {total:6} {ran:6} {_pct(ran, total):6.1f}% {sum(len(f.fresh) for f in files):6}\n")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    release = "--release" in args or os.environ.get("TNSYNC_RELEASE") == "1"
    if "--tnsync" not in args and "--saved" not in args:
        run = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "run.py"), "--coverage"], cwd=ROOT)
        if run.returncode not in (0, 1):
            return 2
    saved, why = load()
    if saved is None:
        print(f"  {why}")
        from dev.progress import result
        print(result("coverage not measured"))
        return 1 if "--tnsync" in args else 2   # (on the board: a warning, orange, like a test to add)
    files, note = measure(saved["hit"])
    if "--tnsync" not in args:
        table(files, "--missing" in args)
    outcomes = [fresh_check(files), settled_check(files)]
    rules = [Check(o.tool, (lambda o=o: o)) for o in outcomes]
    if release:
        for o in outcomes:
            o.failed = False   # (at release, the bar is what holds it up)
        rules.append(Check("Release bar", lambda: release_check(files)))
    if note:
        print(f"  ({note})")
    return checks.run(rules, bar="coverage")


if __name__ == "__main__":
    sys.exit(main())

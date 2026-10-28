#!/usr/bin/env python3
"""Test coverage without installing anything (needs Python 3.12+ for sys.monitoring).

    python3 dev/coverage.py            # run the tests, print a per-file table
    python3 dev/coverage.py --missing  # also list the lines that never ran

Watches every thread (the app does a lot in background threads) and counts the lines
of plexcache/ that ran at least once, against every line that holds code. The figure is
close to what coverage.py reports, but not identical: docstrings and `def` lines count as
code here, which nudges the percentage up slightly.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "plexcache")
TOOL = 4  # sys.monitoring tool id reserved for "coverage" tools


def code_lines(path: str) -> set[int]:
    """Every line number that holds code, from the compiled file."""
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    lines: set[int] = set()
    todo = [compile(source, path, "exec")]
    while todo:
        code = todo.pop()
        lines.update(line for _, _, line in code.co_lines() if line)
        todo.extend(c for c in code.co_consts if hasattr(c, "co_lines"))
    # Ignore the "run as a script" tail of a module, which tests import rather than run.
    text = source.splitlines()
    for i, line in enumerate(text, 1):
        if line.startswith('if __name__ == "__main__":'):
            lines -= set(range(i, len(text) + 1))
    return lines


def main() -> int:
    if sys.version_info < (3, 12):
        print("Needs Python 3.12 or later (sys.monitoring).")
        return 2
    show_missing = "--missing" in sys.argv
    mon = sys.monitoring
    hit: dict[str, set[int]] = {}

    def on_line(code, line):
        f = code.co_filename
        if f.startswith(PKG):
            hit.setdefault(f, set()).add(line)
        return mon.DISABLE  # each line only needs to be seen once: much faster

    mon.use_tool_id(TOOL, "plexcache-coverage")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.set_events(TOOL, mon.events.LINE)
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_plexcache")
        result = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(suite)
    finally:
        mon.set_events(TOOL, 0)
        mon.free_tool_id(TOOL)

    rows, total_lines, total_hit = [], 0, 0
    for dirpath, _, files in os.walk(PKG):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            lines = code_lines(path)
            ran = hit.get(path, set()) & lines
            total_lines += len(lines)
            total_hit += len(ran)
            rows.append((os.path.relpath(path, ROOT), len(lines), len(ran), sorted(lines - ran)))
    print(f"{'file':38} {'lines':>6} {'ran':>6} {'cover':>7}")
    for rel, n, r, missing in sorted(rows):
        print(f"{rel:38} {n:6} {r:6} {100 * r / n if n else 100:6.1f}%")
        if show_missing and missing:
            print("    not run:", _ranges(missing))
    print(f"{'TOTAL':38} {total_lines:6} {total_hit:6} {100 * total_hit / total_lines:6.1f}%")
    if not result.wasSuccessful():
        print(f"\n{len(result.failures) + len(result.errors)} test(s) failed — run tests/run.py to see them.")
        return 1
    return 0


def _ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for n in nums + [None]:
        if start is None:
            start = prev = n
        elif n is not None and n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = n
    return ", ".join(out)


if __name__ == "__main__":
    sys.exit(main())

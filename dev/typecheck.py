#!/usr/bin/env python3
"""The type check, as GitHub runs it: pyright at build.yml's pin, --warnings (a warning fails it, as an error does).

    python3 dev/typecheck.py

What it finds is printed as pyright prints it (file:line:column - severity: message), then the result line every dev tool
says ("0 errors · 0 warnings"). On tnsync's board (PLEXCACHE_PROGRESS=feed: its Python Validation section) its bar is
the files checked, once pyright has said how many. The exit code is pyright's: 0 clean, 1 something found, else it
couldn't run.
"""

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dev.progress import Progress, counted, feeding, result, said  # noqa: E402 — the dev tools' bar and result line


def pin() -> str:
    """The pyright GitHub runs (build.yml's npx pyright@<version>)."""
    with open(os.path.join(ROOT, ".github", "workflows", "build.yml"), encoding="utf-8") as fh:
        got = re.search(r"pyright@([0-9.]+)", fh.read())
    if not got:
        raise SystemExit("no pyright@<version> in .github/workflows/build.yml")
    return got.group(1)


def main() -> int:
    version = pin()
    run = subprocess.run(["npx", "--yes", f"pyright@{version}", "--warnings", "--outputjson"], cwd=ROOT, capture_output=True, text=True)
    try:
        got = json.loads(run.stdout)
    except ValueError:   # (it didn't run: npx or node missing, no network for the download…)
        sys.stdout.write(run.stdout + run.stderr)
        return run.returncode or 2
    for d in got.get("generalDiagnostics") or []:
        start = (d.get("range") or {}).get("start") or {}
        rule = f" ({d['rule']})" if d.get("rule") else ""
        print(f"  {d.get('file', '')}:{start.get('line', 0) + 1}:{start.get('character', 0) + 1} - {d.get('severity', '')}: {d.get('message', '')}{rule}")
    total = got.get("summary") or {}
    files = int(total.get("filesAnalyzed") or 0)
    if feeding() and files:
        bar = Progress(files, unit="files checked", bar="types")
        bar.workers = 1   # (pyright's one process: no --threads given)
        bar.at(files, files, f"Python files · pyright {got.get('version') or version}")
    notes = int(total.get("informationCount") or 0)
    line = result(counted(int(total.get("errorCount") or 0), "error"), counted(int(total.get("warningCount") or 0), "warning"),
                  counted(notes, "information note") if notes else "")
    print(line)
    said(line)
    return run.returncode


if __name__ == "__main__":
    sys.exit(main())

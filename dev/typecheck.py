#!/usr/bin/env python3
"""Python Validation, as GitHub runs it: pyright's type check and Ruff's lint, each at its version in dev/tools.toml.

    python3 dev/typecheck.py          # both (at once); what each found; exit 1 if either found anything
    python3 dev/typecheck.py --fix    # Ruff's safe fixes made first (the import order, unused imports…), then both

pyright (pyrightconfig.json, --warnings: a warning fails it, as an error does) runs through npx. Ruff (ruff.toml: unused
imports and variables, likely bugs, what Python 3.15 says more simply, the import order, the security rules; kept at
zero, a rule wrong for a line said so there: # noqa: <code> — why) is installed the first time, at its version, into a
folder of its own in the cache (~/.cache/plex-cache/tools/ruff-<version>), by the Python that runs this: nothing goes into
that Python (Homebrew's takes no pip installs).

Both run through dev/checks.py, the code every check tool shares: what they find is said one way
(file:line:column - severity: message (Tool CODE)), then the result line every dev tool says ("0 errors · 0 warnings ·
0 Ruff findings"). On tnsync's board (PLEXCACHE_PROGRESS=feed: its Python Validation section) its bar is the files checked.
On GitHub, the findings are annotated on their lines too. Exit 0 clean, 1 something found, else one of them couldn't run.
"""

import argparse
import json
import os
import subprocess
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dev import checks  # noqa: E402 — the check tools' shared code
from dev.checks import Check, Finding, Outcome  # noqa: E402
from dev.progress import counted  # noqa: E402


def pyright_findings(got: dict) -> list[Finding]:
    out = []
    for d in got.get("generalDiagnostics") or []:
        start = (d.get("range") or {}).get("start") or {}
        out.append(Finding("pyright", d.get("file", ""), start.get("line", 0) + 1, start.get("character", 0) + 1,
                           d.get("severity", "error"), d.get("message", ""), d.get("rule", "")))
    return out


def pyright() -> Outcome:
    version = checks.version("pyright")
    cmd, why = checks.command("pyright")
    if not cmd:
        return Outcome("pyright", version, None, why)
    got, run = checks.json_out([*cmd, "--warnings", "--outputjson"])
    if not isinstance(got, dict):   # (it didn't run: npx or node missing, no network for the download…)
        return Outcome("pyright", version, None, f"pyright didn't run: {checks.last_line(run)}")
    total = got.get("summary") or {}
    notes = int(total.get("informationCount") or 0)
    return Outcome("pyright", str(got.get("version") or version), pyright_findings(got),
                   facts=[counted(int(total.get("errorCount") or 0), "error"), counted(int(total.get("warningCount") or 0), "warning"),
                          counted(notes, "information note") if notes else ""],
                   failed=run.returncode != 0)


def ruff_findings(found: list[dict]) -> list[Finding]:
    """Ruff's JSON as findings, in the order of the files (fixable: --fix makes it)."""
    out = []
    for f in sorted(found, key=lambda f: (f.get("filename", ""), f["location"]["row"], f["location"]["column"])):
        out.append(Finding("Ruff", f.get("filename", ""), f["location"]["row"], f["location"]["column"], "error",
                           str(f.get("message", "")), f.get("code") or "syntax",
                           (f.get("fix") or {}).get("applicability") == "safe"))
    return out


def ruff(fix: bool) -> Outcome:
    version = checks.version("ruff")
    cmd, why = checks.command("ruff")
    if not cmd:
        return Outcome("Ruff", version, None, why)
    if fix:
        subprocess.run([*cmd, "check", "--fix", "--quiet", "--exit-zero", "."], cwd=ROOT, timeout=checks.TIMEOUT)
    got, run = checks.json_out([*cmd, "check", "--output-format", "json", "--exit-zero", "."])
    if run.returncode != 0 or not isinstance(got, list):
        return Outcome("Ruff", version, None, f"Ruff didn't run: {checks.last_line(run)}")
    return Outcome("Ruff", version, ruff_findings(got))


def pyright_files() -> list[str]:
    """What pyright reads: the Python in pyrightconfig.json's include (its // comments left out)."""
    import re
    with open(os.path.join(ROOT, "pyrightconfig.json"), encoding="utf-8") as fh:
        config = json.loads(re.sub(r"^\s*//.*$", "", fh.read(), flags=re.M))
    return checks.project_files("*.py", under=tuple(config.get("include") or [""]), skip=tuple(config.get("exclude") or ()))


def ruff_files() -> list[str]:
    """What Ruff reads: the project's Python, but ruff.toml's extend-exclude (and what git ignores, as Ruff does)."""
    with open(os.path.join(ROOT, "ruff.toml"), "rb") as fh:
        config = tomllib.load(fh)
    return checks.project_files("*.py", skip=tuple(config.get("extend-exclude") or ()))


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--fix", action="store_true", help="make Ruff's safe fixes first (in the files), then check")
    args = ap.parse_args()
    # (pyright, the slow one, under way while Ruff is installed the first time and runs; Ruff's fixes land before
    # pyright reads the files only when asked for, so --fix runs them one after the other)
    if args.fix:
        fixed = ruff(True)
        return checks.run([Check("pyright", pyright, pyright_files), Check("Ruff", lambda: fixed, ruff_files)], bar="types")
    return checks.run([Check("pyright", pyright, pyright_files), Check("Ruff", lambda: ruff(False), ruff_files)], bar="types")


if __name__ == "__main__":
    sys.exit(main())

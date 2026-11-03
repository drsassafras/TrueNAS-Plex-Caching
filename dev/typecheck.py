#!/usr/bin/env python3
"""Python Validation, as GitHub runs it: pyright's type check and Ruff's lint, each at build.yml's pin.

    python3 dev/typecheck.py          # both (at once); what each found; exit 1 if either found anything
    python3 dev/typecheck.py --fix    # Ruff's safe fixes made first (the import order, unused imports…), then both

pyright (pyrightconfig.json, --warnings: a warning fails it, as an error does) runs through npx. Ruff (ruff.toml: unused
imports and variables, likely bugs, what Python 3.15 says more simply, the import order, the security rules; kept at
zero, a rule wrong for a line said so there: # noqa: <code> — why) is installed the first time, at its pin, into a folder
of its own in the cache (~/.cache/plex-cache/ruff-<version>), by the Python that runs this: nothing goes into that Python
(Homebrew's takes no pip installs).

What they find is printed as each says it (file:line:column - severity: message; file:line:column: CODE message), then
the result line every dev tool says ("0 errors · 0 warnings · 0 Ruff findings"). On tnsync's board (PLEXCACHE_PROGRESS=
feed: its Python Validation section) its bar is the files checked, once pyright has said how many. On GitHub, Ruff's
findings are annotated on their lines too. Exit 0 clean, 1 something found, else one of them couldn't run.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dev.progress import Progress, counted, feeding, result, said  # noqa: E402 — the dev tools' bar and result line

BUILD = os.path.join(ROOT, ".github", "workflows", "build.yml")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "plex-cache")
ON_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"


def _pin(pattern: str, what: str) -> str:
    with open(BUILD, encoding="utf-8") as fh:
        got = re.search(pattern, fh.read())
    if not got:
        raise SystemExit(f"no {what} in .github/workflows/build.yml")
    return got.group(1)


def pin() -> str:
    """The pyright GitHub runs (build.yml's pyright@<version>)."""
    return _pin(r"pyright@([0-9.]+)", "pyright@<version>")


def ruff_pin() -> str:
    """The Ruff GitHub runs (build.yml's ruff==<version>)."""
    return _pin(r"ruff==([0-9.]+)", "ruff==<version>")


def ruff_binary(version: str) -> tuple[str | None, str]:
    """Ruff at its pin, installed the first time (a venv of its own in the cache); else None and why."""
    venv = os.path.join(CACHE, f"ruff-{version}")
    exe = os.path.join(venv, "bin", "ruff")
    if os.path.exists(exe):
        return exe, ""
    tmp = f"{venv}.{os.getpid()}"
    try:
        os.makedirs(CACHE, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", tmp], check=True, capture_output=True, text=True, timeout=300)
        subprocess.run([os.path.join(tmp, "bin", "python"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                        f"ruff=={version}"], check=True, capture_output=True, text=True, timeout=300)
        try:
            os.rename(tmp, venv)   # (whole or not at all: a run stopped halfway leaves nothing to trust)
        except OSError:
            if not os.path.exists(exe):   # (another run got there first: its copy will do)
                raise
    except subprocess.CalledProcessError as e:
        lines = (e.stderr or e.stdout or "").strip().splitlines()
        return None, f"Ruff {version} couldn't be installed: {lines[-1] if lines else e}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"Ruff {version} couldn't be installed: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return exe, ""


def ruff_findings(exe: str, fix: bool) -> tuple[list[dict] | None, str]:
    """Ruff's findings over the project (ruff.toml); None and why when it couldn't say."""
    if fix:
        subprocess.run([exe, "check", "--fix", "--quiet", "--exit-zero", "."], cwd=ROOT, timeout=300)
    run = subprocess.run([exe, "check", "--output-format", "json", "--exit-zero", "."], cwd=ROOT, capture_output=True,
                         text=True, timeout=300)
    try:
        found = json.loads(run.stdout or "[]")
    except ValueError:
        found = None
    if run.returncode != 0 or not isinstance(found, list):
        lines = (run.stderr or run.stdout or "").strip().splitlines()
        return None, f"Ruff didn't run: {lines[-1] if lines else f'exit {run.returncode}'}"
    return found, ""


def ruff_lines(found: list[dict]) -> list[str]:
    """Each finding as a line, in the order of the files: "  dev/x.py:3:1: F401 [*] `os` imported but unused" ([*]:
    --fix makes it)."""
    out = []
    for f in sorted(found, key=lambda f: (f.get("filename", ""), f["location"]["row"], f["location"]["column"])):
        path = os.path.relpath(f.get("filename", ""), ROOT)
        fixable = " [*]" if (f.get("fix") or {}).get("applicability") == "safe" else ""
        out.append(f"  {path}:{f['location']['row']}:{f['location']['column']}: {f.get('code') or 'syntax'}{fixable} {f.get('message', '')}")
    return out


def annotate(found: list[dict]) -> None:
    """On GitHub: each finding an annotation on its line."""
    for f in found:
        path = os.path.relpath(f.get("filename", ""), ROOT)
        msg = str(f.get("message", "")).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error file={path},line={f['location']['row']},col={f['location']['column']},title=Ruff {f.get('code') or 'syntax'}::{msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--fix", action="store_true", help="make Ruff's safe fixes first (in the files), then check")
    args = ap.parse_args()
    version, ruff_version = pin(), ruff_pin()
    # pyright, the slow one, under way while Ruff is installed (the first time) and runs
    pyright = subprocess.Popen(["npx", "--yes", f"pyright@{version}", "--warnings", "--outputjson"], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    exe, why = ruff_binary(ruff_version)
    found, why = ruff_findings(exe, args.fix) if exe else (None, why)
    out, err = pyright.communicate()
    try:
        got = json.loads(out)
    except ValueError:   # (it didn't run: npx or node missing, no network for the download…)
        sys.stdout.write(out + err)
        return pyright.returncode or 2
    for d in got.get("generalDiagnostics") or []:
        start = (d.get("range") or {}).get("start") or {}
        rule = f" ({d['rule']})" if d.get("rule") else ""
        print(f"  {d.get('file', '')}:{start.get('line', 0) + 1}:{start.get('character', 0) + 1} - {d.get('severity', '')}: {d.get('message', '')}{rule}")
    if found:
        print("\n".join(ruff_lines(found)))
        if ON_GITHUB:
            annotate(found)
    elif found is None:
        print(f"  {why}")
    total = got.get("summary") or {}
    files = int(total.get("filesAnalyzed") or 0)
    if feeding() and files:
        bar = Progress(files, unit="files checked", bar="types")
        bar.workers = 1   # (pyright's one process: no --threads given)
        bar.at(files, files, f"Python files · pyright {got.get('version') or version} · Ruff {ruff_version}")
    notes = int(total.get("informationCount") or 0)
    line = result(counted(int(total.get("errorCount") or 0), "error"), counted(int(total.get("warningCount") or 0), "warning"),
                  counted(notes, "information note") if notes else "",
                  counted(len(found), "Ruff finding") if found is not None else "Ruff didn't run")
    print(line)
    said(line)
    if found is None:
        return 2
    return pyright.returncode or (1 if found else 0)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build Validation, as GitHub runs it: what builds and ships the app, checked — the shell script (ShellCheck on
dev/tnsync), the workflows (actionlint on .github/workflows, with ShellCheck on each step's script) and the Dockerfile
(hadolint, .hadolint.yaml), each at its version in dev/tools.toml.

    python3 dev/buildcheck.py

All three run through dev/checks.py, the code every check tool shares: each fetched at its version the first time (into
~/.cache/plex-cache/tools/), what they find said one way (file:line:column - severity: message (Tool CODE)), then the
result line every dev tool says ("0 ShellCheck findings · 0 actionlint findings · 0 hadolint findings"). On tnsync's board
(PLEXCACHE_PROGRESS=feed: its Build Validation section) its bar is the files checked. Every finding counts, ShellCheck's
and hadolint's notes and style too: they're kept at zero, fixed in the code; a rule wrong for a line says so there
(``# shellcheck disable=SC… — why``), and a rule is only turned off (.hadolint.yaml) with the maintainer's say. Exit 0
clean, 1 something found, else one of them couldn't run.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dev import checks  # noqa: E402 — the check tools' shared code
from dev.checks import Check, Finding, Outcome  # noqa: E402

SCRIPTS = [os.path.join("dev", "tnsync")]   # (the project's shell scripts: CheckToolTests holds this to every one there is)
WORKFLOWS = os.path.join(".github", "workflows")
DOCKERFILES = ["Dockerfile"]
LEVELS = {"error": "error", "warning": "warning", "info": "information", "style": "information"}


def shellcheck_findings(got: dict) -> list[Finding]:
    return [Finding("ShellCheck", c.get("file", ""), int(c.get("line") or 0), int(c.get("column") or 0),
                    LEVELS.get(c.get("level", ""), "warning"), str(c.get("message", "")), f"SC{c.get('code')}")
            for c in got.get("comments") or []]


def shellcheck() -> Outcome:
    version = checks.version("shellcheck")
    cmd, why = checks.command("shellcheck")
    if not cmd:
        return Outcome("ShellCheck", version, None, why)
    got, run = checks.json_out([*cmd, "--format=json1", "--external-sources", *SCRIPTS])
    if not isinstance(got, dict) or run.returncode not in (0, 1):
        return Outcome("ShellCheck", version, None, f"ShellCheck didn't run: {checks.last_line(run)}")
    return Outcome("ShellCheck", version, shellcheck_findings(got))


def actionlint_findings(got: list) -> list[Finding]:
    return [Finding("actionlint", f.get("filepath", ""), int(f.get("line") or 0), int(f.get("column") or 0), "error",
                    str(f.get("message", "")), str(f.get("kind", ""))) for f in got]


def actionlint() -> Outcome:
    version = checks.version("actionlint")
    cmd, why = checks.command("actionlint")
    if not cmd:
        return Outcome("actionlint", version, None, why)
    sc, why = checks.command("shellcheck")   # (each step's script checked as ShellCheck checks dev/tnsync)
    if not sc:
        return Outcome("actionlint", version, None, why)
    got, run = checks.json_out([*cmd, "-format", "{{json .}}", "-shellcheck", sc[0], "-pyflakes", ""])
    if not isinstance(got, list) or run.returncode not in (0, 1):
        return Outcome("actionlint", version, None, f"actionlint didn't run: {checks.last_line(run)}")
    return Outcome("actionlint", version, actionlint_findings(got))


def hadolint_findings(got: list) -> list[Finding]:
    return [Finding("hadolint", f.get("file", ""), int(f.get("line") or 0), int(f.get("column") or 0),
                    LEVELS.get(f.get("level", ""), "warning"), str(f.get("message", "")), str(f.get("code", ""))) for f in got]


def hadolint() -> Outcome:
    version = checks.version("hadolint")
    cmd, why = checks.command("hadolint")
    if not cmd:
        return Outcome("hadolint", version, None, why)
    got, run = checks.json_out([*cmd, "--config", ".hadolint.yaml", "--format", "json", "--no-color", *DOCKERFILES])
    if not isinstance(got, list) or run.returncode not in (0, 1):
        return Outcome("hadolint", version, None, f"hadolint didn't run: {checks.last_line(run)}")
    return Outcome("hadolint", version, hadolint_findings(got))


def main() -> int:
    return checks.run([Check("ShellCheck", shellcheck, lambda: list(SCRIPTS)),
                       Check("actionlint", actionlint, lambda: checks.project_files("*.yml", "*.yaml", under=(WORKFLOWS,))),
                       Check("hadolint", hadolint, lambda: list(DOCKERFILES))], bar="build")


if __name__ == "__main__":
    sys.exit(main())

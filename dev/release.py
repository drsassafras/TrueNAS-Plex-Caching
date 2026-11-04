#!/usr/bin/env python3
"""The release checks: what has to be true of dev before it's released (merged into main and tagged). tnsync runs them
in a block of their own with --release (TN_RELEASE in .tnsync), on the code it copies; never on a normal run, so a dev
run can't fail on what only a release needs.

    python3 dev/release.py           # each check, a line each; what to do for one that fails; exit 1 if any fail
    python3 dev/release.py --tnsync  # tnsync's Release block, every run: the same checks as an overall readiness —
                                     # "4 of 7 ready", what's still to go — grey between releases (exit 3), and with
                                     # --release (TNSYNC_RELEASE=1) the blocking check it always was

The checks:
* the version: plexcache/__init__.py's __version__ a new X.Y.0, above what's released (main's version and the newest
  release tag), and not tagged yet — dev's builds raise the last number (0.16.22), a release starts the next (0.17.0);
* the translations: every text translated, in every language (in step with the code: the suite's, as for a release);
* the MQTT settings: every setting (plexcache/config.py's DEFAULTS) either a control over MQTT or listed as kept off it,
  with why (plexcache/homeassistant.py: CONTROL_SETTINGS, PER_LIBRARY_CONTROLS, NOT_OVER_MQTT), and nothing listed that's gone;
* the MQTT docs: the README's topics and commands (MQTT – other tools) the ones the code has;
* the coverage: the release bar (dev/coverage.py: 98% of every line run, each file at 90%), from what the tests saved;
* the security audits: dev/audit.py's release rules, as a --release run holds them;
* the looks: every look the app wears approved into the style guide (dev/style.py), none waiting.

The released version is read from git: the repository tnsync runs in (TNSYNC_REPO, which tnsync sets, since the code it
tests may be a copy from git rather than a checkout), else this folder.
"""

import ast
import fnmatch
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:  # (run as a script: dev/ is on the path, not the project)
    sys.path.insert(0, ROOT)

VERSION_FILE = os.path.join(ROOT, "plexcache", "__init__.py")
RELEASED_ON = "origin/main"   # what's released: main, as GitHub has it
TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")   # a release tag (v0.16.0); a pre-release (v0.16.0-beta.1) isn't one


@dataclass
class Result:
    name: str
    ok: bool
    line: str                                         # what came of it, in a line
    todo: list[str] = field(default_factory=list)     # for one that failed: what's wrong, and what to do


def _git(*args: str) -> str | None:
    repo = os.environ.get("TNSYNC_REPO") or ROOT
    try:
        got = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return got.stdout if got.returncode == 0 else None


def version_of(text: str) -> str | None:
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return m.group(1) if m else None


def parse(version: str) -> tuple[int, int, int] | None:
    m = TAG.match(version.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def released() -> tuple[tuple[int, int, int] | None, str]:
    """The newest version released — main's, or a release tag's, whichever is newer — and where it was read."""
    found: list[tuple[tuple[int, int, int], str]] = []
    main = _git("show", f"{RELEASED_ON}:plexcache/__init__.py")
    v = parse(version_of(main) or "") if main else None
    if v:
        found.append((v, f"{RELEASED_ON.split('/')[-1]} has {'.'.join(map(str, v))}"))
    for tag in (_git("tag", "-l") or "").split():
        t = parse(tag)
        if t:
            found.append((t, f"tag {tag}"))
    if not found:
        return None, ""
    return max(found)


def check_version(text: str | None = None) -> Result:
    name = "Version"
    if text is None:
        try:
            with open(VERSION_FILE, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            return Result(name, False, f"can't read plexcache/__init__.py: {e.strerror}")
    have = version_of(text)
    if not have:
        return Result(name, False, "no __version__ in plexcache/__init__.py")
    v = parse(have)
    if not v:
        return Result(name, False, f"{have} isn't a release version (X.Y.Z)",
                      [f"Set __version__ in plexcache/__init__.py to the release, a new X.Y.0 (say {have.split('-')[0]})."])
    top, where = released()
    if top is None:
        return Result(name, False, f"{have}: can't tell what's released (no {RELEASED_ON}, no release tags)",
                      [f"git fetch origin, so {RELEASED_ON} is there to compare with."])
    nxt = f"{top[0]}.{top[1] + 1}.0"
    if v[2] != 0:
        return Result(name, False, f"{have} isn't a new X.Y.0 ({where})",
                      [f"A release starts a new minor (or major) version with .0: set __version__ in plexcache/__init__.py to {nxt}."])
    if v[:2] <= top[:2]:
        return Result(name, False, f"{have} isn't above what's released ({where})",
                      [f"Set __version__ in plexcache/__init__.py to {nxt} (or a new major, {top[0] + 1}.0.0)."])
    if (_git("tag", "-l", f"v{have}") or "").strip():
        return Result(name, False, f"{have} is tagged already", [f"v{have} exists: set __version__ to {nxt}."])
    return Result(name, True, f"{have}, a new release ({where})")


def check_translations() -> Result:
    """Every text translated in every language. (Only that: the catalogs in step with the code, and each translation
    matching its English, are the suite's TranslationTests, which tnsync --release runs as for a release.)"""
    from dev import i18n
    name = "Translations"
    keys = i18n.all_keys([])
    left = [(code, len(i18n.untranslated(keys, i18n.load(code)))) for code in i18n.TRANSLATED]
    left = [(code, n) for code, n in left if n]
    if not left:
        return Result(name, True, f"all {len(i18n.TRANSLATED)} languages complete: {len(keys)} texts, every one translated")
    todo = [f"{code}: {n} untranslated" for code, n in left]
    todo += ["For the release: python3 dev/i18n.py sync (every catalog in step), then translate what's null;",
             "python3 dev/i18n.py status says how far each language is."]
    return Result(name, False, f"{len(left)} of {len(i18n.TRANSLATED)} languages not complete "
                               f"(up to {max(n for _, n in left)} of {len(keys)} texts untranslated)", todo)


def check_mqtt_settings() -> Result:
    from plexcache import config
    from plexcache import homeassistant as ha
    name = "MQTT settings"
    settings = [f"{sec}.{key}" for sec, keys in config.DEFAULTS.items() for key in keys]
    controls = {f"{sec}.{key}" for sec, key in ha.CONTROL_SETTINGS.values()} | set(ha.PER_LIBRARY_CONTROLS)
    kept_off = list(ha.NOT_OVER_MQTT)
    unplaced = [p for p in settings if p not in controls and not any(fnmatch.fnmatchcase(p, k) for k in kept_off)]
    gone = sorted([k for k in kept_off if not any(fnmatch.fnmatchcase(p, k) for p in settings)]
                  + [c for c in controls if c not in settings])
    if not unplaced and not gone:
        over = sum(1 for p in settings if p in controls)
        return Result(name, True, f"each of the {len(settings)} settings placed: {over} over MQTT, the rest kept off it, each with why")
    todo = [f"{p}: not a control over MQTT, nor in NOT_OVER_MQTT" for p in unplaced]
    todo += [f"{k}: listed in plexcache/homeassistant.py, but there's no such setting" for k in gone]
    todo += ["Make each new setting a control (CONTROL_SETTINGS, and its entity) or add it to NOT_OVER_MQTT with why;",
             "take out what's no longer a setting."]
    said = [f"{len(unplaced)} settings not placed" if unplaced else "", f"{len(gone)} listed that are gone" if gone else ""]
    return Result(name, False, ", ".join(x for x in said if x), todo)


README = os.path.join(ROOT, "README.md")


def mqtt_commands(source: str) -> set[str]:
    """The commands homeassistant.py's _command takes: each name it compares with, and those it takes by their start and
    end, as the README writes them (name.startswith("library_") and name.endswith("_prefetch_start"):
    library_<key>_prefetch_start)."""
    names: set[str] = set()
    patterns: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "_command":
            for n in ast.walk(node):
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "name":
                    for c in n.comparators:
                        for v in (c.elts if isinstance(c, ast.Tuple) else [c]):
                            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                                names.add(v.value)
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("startswith", "endswith")
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == "name"):
                    for a in n.args:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            patterns.add(f"{n.func.attr}:{a.value}")
    starts = sorted(p.split(":", 1)[1] for p in patterns if p.startswith("startswith:"))
    ends = [p.split(":", 1)[1] for p in patterns if p.startswith("endswith:")]
    ends = [e for e in ends if not any(o != e and o.endswith(e) for o in ends)]   # ("_start" is "_prefetch_start"'s end)
    names |= {f"{s}<key>{e}" for s in starts for e in ends}
    return names


def check_mqtt_docs(readme: str | None = None, source: str | None = None) -> Result:
    name = "MQTT docs"
    try:
        if readme is None:
            with open(README, encoding="utf-8") as fh:
                readme = fh.read()
        if source is None:
            with open(os.path.join(ROOT, "plexcache", "homeassistant.py"), encoding="utf-8") as fh:
                source = fh.read()
    except OSError as e:
        return Result(name, False, f"can't read {e.filename}: {e.strerror}")
    topics_code = set(re.findall(r"\{base\}/([a-z_]+)", source))
    rows = re.findall(r"^\| `plexcache/([a-z_]+)[^`]*` \|(.*)$", readme, re.M)
    topics_doc = {t for t, _ in rows}
    commands = mqtt_commands(source)
    said_row = next((rest for t, rest in rows if t == "set"), "")
    words = set(re.findall(r"`([a-z_<>]+)`", said_row))
    todo = [f"topic plexcache/{t}: published, not in the README's table" for t in sorted(topics_code - topics_doc)]
    todo += [f"topic plexcache/{t}: in the README's table, not published" for t in sorted(topics_doc - topics_code)]
    todo += [f"command {c}: taken, not in the README's plexcache/set/<name> row" for c in sorted(commands - words)]
    todo += [f"command {w}: in the README's plexcache/set/<name> row, not taken" for w in sorted(w for w in words - commands if "_" in w)]
    if not rows:
        todo.insert(0, "no table of plexcache/… topics in the README (MQTT – other tools)")
    if not todo:
        return Result(name, True, f"the README has the {len(topics_code)} topics and {len(commands)} commands the code has")
    todo.append("README.md → MQTT – other tools: bring its table in step with plexcache/homeassistant.py.")
    return Result(name, False, f"the README's MQTT table differs from the code ({len(todo) - 1})", todo)


def check_coverage() -> Result:
    """The release's coverage bar (dev/coverage.py): 98% of every line run, each file at 90%, from what the tests saved."""
    from dev import coverage
    name = "Coverage"
    saved, why = coverage.load()
    if saved is None:
        return Result(name, False, "not measured", [why])
    files, _ = coverage.measure(saved["hit"])
    outcome = coverage.release_check(files)
    line = outcome.facts[0] if outcome.facts else ""
    if not outcome.findings:
        return Result(name, True, line)
    todo = [f.message if f.path == "plexcache" else f"{f.path}: {f.message}" for f in outcome.findings[:15]]
    todo.append("python3 dev/coverage.py --missing says which lines; add tests (or run where the optimizer's tools are).")
    return Result(name, False, line, todo)


def check_audits() -> Result:
    """The security audits' release rules (dev/audit.py --release), as a release run holds them."""
    from dev.progress import FEED
    name = "Security audits"
    try:
        got = subprocess.run([sys.executable, os.path.join(ROOT, "dev", "audit.py"), "--release"],
                             capture_output=True, text=True, timeout=300, cwd=ROOT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Result(name, False, f"couldn't be checked: {e}")
    lines = [ln.strip() for ln in got.stdout.splitlines() if ln.strip() and not ln.startswith(FEED)]
    line = lines[-1] if lines else f"exit {got.returncode}"
    if got.returncode == 0:
        return Result(name, True, line)
    return Result(name, False, line, lines[:-1][:15] + ["python3 dev/audit.py gui, and work through what the release rules still need."])


def check_looks() -> Result:
    """Every look the app wears approved into the style guide (dev/style.py): none waiting for the maintainer."""
    from dev import style
    name = "Looks"
    diff = style.pending()
    todo = [f"{state}: {look}" for state in diff for look in diff[state]]
    if not todo:
        return Result(name, True, "every look is in the style guide")
    return Result(name, False, f"{len(todo)} looks waiting for approval",
                  todo[:15] + ["python3 dev/style.py review, and approve what's right."])


CHECKS: list[Callable[[], Result]] = [check_version, check_translations, check_mqtt_settings, check_mqtt_docs,
                                      check_coverage, check_audits, check_looks]


def summary(results: list[Result]) -> str:
    """The block's line on tnsync's board: the ones that failed, else that all passed."""
    failed = [r for r in results if not r.ok]
    if not failed:
        return f"all {len(results)} passed: " + "; ".join(f"{r.name.lower()} {r.line}" for r in results)
    return "; ".join(f"{r.name}: {r.line}" for r in failed)


def main(argv: list[str] | None = None) -> int:
    from dev.progress import said
    args = sys.argv[1:] if argv is None else argv
    board = "--tnsync" in args   # (tnsync's Release block, every run: readiness between releases, the check on one)
    strict = "--release" in args or os.environ.get("TNSYNC_RELEASE") == "1"
    results: list[Result] = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as e:  # noqa: BLE001 — (a check that breaks is a check that failed, said as such)
            results.append(Result(check.__name__.removeprefix("check_").title(), False, f"couldn't be checked: {e}"))
    for r in results:
        print(f"{'✔' if r.ok else '✘'} {r.name}: {r.line}")
        for t in r.todo:
            print(f"    {t}")
    if board and not strict:   # (how ready dev is, in a line: nothing to block between releases)
        togo = ", ".join(r.name.lower() for r in results if not r.ok)
        ready = sum(1 for r in results if r.ok)
        line = f"{ready} of {len(results)} ready" + (f" · to go: {togo}" if togo else " · ready to release")
        print(line)
        said(line)
        return 0 if not togo else 3
    said(summary(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

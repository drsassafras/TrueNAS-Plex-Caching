#!/usr/bin/env python3
"""Security audits, line by line (tnsync's Security Audit block): which app code an audit has seen, what's stale or never
audited, and the release rules.

    python3 dev/audit.py                 # the checks (tnsync: TN_AUDIT=python3 dev/audit.py)
    python3 dev/audit.py --release       # the release rules too (tnsync --release: their failure is red)
    python3 dev/audit.py next            # what most needs an audit, grouped into batches by file
    python3 dev/audit.py start [PATH…]   # the standard prompt, filled in (no paths: a full audit; paths: a partial)
    python3 dev/audit.py record FILE     # a filled-in templates/audit.toml, checked, split and saved
    python3 dev/audit.py record FILE --author   # …an author's own review while building: counts on dev, never at release
    python3 dev/audit.py gui             # all of it on a page in your browser: a person's audit, start to end (dev/auditgui.py)
    python3 dev/audit.py run …           # an audit by an AI auditor installed here, when there is one (dev/auditrun.py)

Each audit has two halves with the same id (``YYYY-MM-DD-N``, never naming an auditor):

  - public, security/audits/<id>.toml here: the day, the dev commit audited, full or partial, and a partial's scope;
  - private, audits/<id>.toml in the private audits repo (dev/audit.toml's ``private``): the auditor (a person,
    ``human:name``, or an AI auditor's id), its thoroughness (1 the most thorough … 5), and what it found.

With the public half alone (GitHub, anyone's clone) this says what's covered, full or partial, and what's stale; with the
private repo checked out it also holds the release rules (its rules.toml) and lists the open findings.

A line counts as seen by an audit when it's in the audit's scope and hasn't changed since the audit's commit: git blame
gives the commit that last changed it, and that commit is the audit's or one before it. Or when its fingerprint is in the
audit's ``prints``: a short hash of the file's path, the line and the lines with something on them just above and below
it, which record takes from the code the audit read. That's how an audit of code not committed yet (your working tree)
counts once it is committed, however it's committed (one commit or many, squashed, rebased), and how an audit keeps its
lines where git no longer knows its commit. Change a line, or the line next to it, and its fingerprint changes: it needs
an audit again. Nobody marks lines by hand.

Through dev/checks.py, as every check says it. Exit 0 every rule met, 1 something to audit (tnsync's block orange; with
--release, a release rule not met: red), 2 the records couldn't be read.
"""

import ast
import datetime
import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dev import checks  # noqa: E402 — the check tools' shared code
from dev.checks import Check, Finding, Outcome  # noqa: E402
from dev.checks import repo_at as _repo  # noqa: E402 — where git knows these lines (this checkout, or tnsync's copy's)
from dev.progress import counted  # noqa: E402

CONFIG = os.path.join(ROOT, "dev", "audit.toml")
PUBLIC = os.path.join(ROOT, "security", "audits")
DAY = 24 * 3600
KINDS = ("full", "partial")
SEVERITIES = ("info", "low", "medium", "high", "critical")
STATUSES = ("open", "fixed", "accepted", "false-positive")
MAIN = ("affected", "not-affected", "fixed")
TYPES = ("vulnerability", "hardening")   # (exploitable, CVSS scored; or hardening, not exploitable: a priority, never a release block)
PRIORITIES = ("low", "medium", "high")   # (a hardening finding's)
CODE = ("there", "changed", "gone", "unknown")   # (a finding's lines in the code now: see code_state)
ID = re.compile(r"^\d{4}-\d{2}-\d{2}-\d+$")
FINDING_ID = re.compile(r"^F-\d{4}-\d{3,}$")
CVE = re.compile(r"^CVE-\d{4}-\d{4,}$")   # (a finding's CVE id, once one is assigned)
TEST = re.compile(r"^[A-Za-z_]\w*\.test\w*$")   # (a test that guards a finding: Class.test_name, in tests/)
REPORTS = ("fixed", "attempted", "not-real")   # (what whoever fixed a finding says of it)
PRINT = re.compile(r"^[0-9a-f]{12}$")   # (a line's fingerprint: see fingerprints)
TOP = 10   # files named on the board when lines wait for an audit

# ---------------------------------------------------------------- CVSS 3.1 (first.org/cvss/v3.1/specification-document)
CVSS_METRICS = {   # each base metric's values and weights, in the vector's order
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "PR": {"N": 0.85, "L": 0.62, "H": 0.27},   # (L 0.68 and H 0.5 when the scope changes)
    "UI": {"N": 0.85, "R": 0.62},
    "S": {"U": 0.0, "C": 0.0},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def _roundup(x: float) -> float:
    """The specification's round up to one decimal (floating point can't put 4.000001 above 4.0)."""
    n = round(x * 100000)
    return n / 100000.0 if n % 10000 == 0 else (n // 10000 + 1) / 10.0


def cvss(vector: str) -> tuple[float, str]:
    """A CVSS 3.1 base vector's score and its severity (``info`` for the specification's None, then low, medium, high,
    critical), or ValueError saying what's wrong with it."""
    parts = str(vector).strip().split("/")
    if not parts or parts[0] not in ("CVSS:3.1", "CVSS:3.0"):
        raise ValueError(f"{vector!r} isn't a CVSS 3.1 vector (CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)")
    got: dict[str, str] = {}
    for part in parts[1:]:
        key, _, value = part.partition(":")
        if key in got:
            raise ValueError(f"{vector!r} gives {key} twice")
        got[key] = value
    for key, values in CVSS_METRICS.items():
        if got.get(key) not in values:
            raise ValueError(f"{vector!r}: {key} isn't one of {', '.join(values)}")
    changed = got["S"] == "C"
    pr = {"N": 0.85, "L": 0.68, "H": 0.5}[got["PR"]] if changed else CVSS_METRICS["PR"][got["PR"]]
    iss = 1 - (1 - CVSS_METRICS["C"][got["C"]]) * (1 - CVSS_METRICS["I"][got["I"]]) * (1 - CVSS_METRICS["A"][got["A"]])
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    exploit = 8.22 * CVSS_METRICS["AV"][got["AV"]] * CVSS_METRICS["AC"][got["AC"]] * pr * CVSS_METRICS["UI"][got["UI"]]
    if impact <= 0:
        score = 0.0
    else:
        score = _roundup(min(1.08 * (impact + exploit), 10)) if changed else _roundup(min(impact + exploit, 10))
    sev = "info" if score == 0 else "low" if score < 4 else "medium" if score < 7 else "high" if score < 9 else "critical"
    return score, sev


def scored(f: dict) -> dict:
    """A finding with its severity from its CVSS vector, when it has one (and the vector as the specification writes it)."""
    if f.get("type") == "hardening":
        return f
    if f.get("cvss"):
        f["cvss"] = str(f["cvss"]).strip()
        f["severity"] = cvss(f["cvss"])[1]
    return f


def config() -> dict:
    with open(CONFIG, "rb") as fh:
        return tomllib.load(fh)


def private_root() -> str:
    """The private audits repo's checkout: PLEXCACHE_AUDITS, else dev/audit.toml's ``private`` from the project's folder
    (the one tnsync copied from, when this is its copy)."""
    if os.environ.get("PLEXCACHE_AUDITS"):
        return os.environ["PLEXCACHE_AUDITS"]
    base = os.environ.get("TNSYNC_REPO") or ROOT
    return os.path.normpath(os.path.join(base, config().get("private", "../TrueNAS-Plex-Caching-audits")))


# ---------------------------------------------------------------- the records
@dataclass
class Audit:
    id: str
    date: datetime.date
    commit: str
    kind: str
    scope: list[str] = field(default_factory=list)
    private: dict | None = None   # the private half, when it's here
    prints: frozenset[str] = frozenset()   # the fingerprints of the lines it read (none in records made before them)
    uncommitted: bool = False   # it read code that wasn't committed yet (its commit: the one that code was on)

    @property
    def auditor(self) -> str:
        return str((self.private or {}).get("auditor", ""))

    @property
    def thoroughness(self) -> int:
        return int((self.private or {}).get("thoroughness", 5))

    @property
    def findings(self) -> list[dict]:
        return list((self.private or {}).get("finding", []))

    @property
    def author(self) -> bool:
        """The code's own author reviewing it (while building it): it counts for the dev rule, never at release,
        however thorough, since the one who wrote it shares its blind spots."""
        return bool((self.private or {}).get("author", False))


def _date(v) -> datetime.date:
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    return datetime.date.fromisoformat(str(v))


def problems(rec: dict, half: str) -> list[str]:
    """What's wrong with one half of a record (``public``, ``private``, or ``both``: a filled-in template)."""
    out = []
    if not ID.match(str(rec.get("id", ""))):
        out.append(f"id {rec.get('id')!r} isn't YYYY-MM-DD-N")
    if half in ("public", "both"):
        for key in ("date", "commit", "kind"):
            if not rec.get(key):
                out.append(f"no {key}")
        if rec.get("kind") and rec["kind"] not in KINDS:
            out.append(f"kind {rec['kind']!r} isn't full or partial")
        if rec.get("kind") == "partial" and not rec.get("scope"):
            out.append("a partial audit needs its scope")
        if rec.get("kind") == "full" and rec.get("scope"):
            out.append("a full audit has no scope: it's all the app code")
        try:
            if rec.get("date"):
                _date(rec["date"])
        except ValueError:
            out.append(f"date {rec['date']!r} isn't YYYY-MM-DD")
        prints = rec.get("prints", [])
        if not isinstance(prints, list) or not all(isinstance(p, str) and PRINT.match(p) for p in prints):
            out.append("prints aren't a list of 12-character fingerprints")
    if half in ("private", "both"):
        if not rec.get("auditor"):
            out.append("no auditor")
        t = rec.get("thoroughness")
        if not isinstance(t, int) or isinstance(t, bool) or not 1 <= t <= 5:
            out.append(f"thoroughness {t!r} isn't 1 to 5")
        for f in rec.get("finding", []):
            name = f.get("id", "a finding")
            if not FINDING_ID.match(str(f.get("id", ""))):
                out.append(f"{name}: id isn't F-YYYY-NNN")
            for key in ("title", "lines", "detail"):
                if not f.get(key):
                    out.append(f"{name}: no {key}")
            if f.get("severity") not in SEVERITIES:
                out.append(f"{name}: severity {f.get('severity')!r} isn't one of {', '.join(SEVERITIES)}")
            kind = f.get("type", "vulnerability")
            if kind not in TYPES:
                out.append(f"{name}: type {kind!r} isn't one of {', '.join(TYPES)}")
            elif kind == "hardening":
                if f.get("cvss"):
                    out.append(f"{name}: a hardening finding has no CVSS vector (it isn't exploitable as it is)")
                if f.get("severity") not in PRIORITIES:
                    out.append(f"{name}: a hardening finding's priority (severity) is one of {', '.join(PRIORITIES)}")
            elif f.get("cvss"):
                try:
                    if cvss(str(f["cvss"]))[1] != f.get("severity"):
                        out.append(f"{name}: severity {f.get('severity')!r} isn't its CVSS vector's ({cvss(str(f['cvss']))[1]})")
                except ValueError as e:
                    out.append(f"{name}: {e}")
            if f.get("cve") and not CVE.match(str(f["cve"])):
                out.append(f"{name}: cve {f['cve']!r} isn't CVE-YYYY-NNNN")
            tests = f.get("tests", [])
            if not isinstance(tests, list) or not all(isinstance(t, str) and TEST.match(t) for t in tests):
                out.append(f"{name}: tests aren't a list of Class.test_name")
            if f.get("reported") and f["reported"] not in REPORTS:
                out.append(f"{name}: reported {f['reported']!r} isn't one of {', '.join(REPORTS)}")
            fp = f.get("prints", [])
            if not isinstance(fp, list) or not all(isinstance(p, str) and PRINT.match(p) for p in fp):
                out.append(f"{name}: prints aren't a list of 12-character fingerprints")
            if f.get("status") not in STATUSES:
                out.append(f"{name}: status {f.get('status')!r} isn't one of {', '.join(STATUSES)}")
            if f.get("main") not in MAIN:
                out.append(f"{name}: main {f.get('main')!r} isn't one of {', '.join(MAIN)}")
            for spot in f.get("lines", []):
                if not re.match(r"^[^:]+:\d+(-\d+)?$", str(spot)):
                    out.append(f"{name}: lines {spot!r} isn't path:start-end")
    return out


def _load_dir(folder: str) -> tuple[dict[str, dict], list[str]]:
    got, bad = {}, []
    for path in sorted(glob.glob(os.path.join(folder, "*.toml"))):
        try:
            with open(path, "rb") as fh:
                rec = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as e:
            bad.append(f"{path}: {e}")
            continue
        got[str(rec.get("id") or os.path.basename(path)[:-5])] = rec
    return got, bad


def load(private: str | None = None) -> tuple[list[Audit], bool, list[str]]:
    """Every audit (oldest first), whether the private half was here, and what couldn't be read."""
    public, bad = _load_dir(PUBLIC)
    root = private_root() if private is None else private
    have_private = bool(root) and os.path.isdir(os.path.join(root, "audits"))
    secret, more = _load_dir(os.path.join(root, "audits")) if have_private else ({}, [])
    bad += more
    audits = []
    for aid, rec in public.items():
        bad += [f"security/audits/{aid}.toml: {p}" for p in problems(rec, "public")]
        if problems(rec, "public"):
            continue
        priv = secret.get(aid)
        if priv is not None:
            bad += [f"private audits/{aid}.toml: {p}" for p in problems(priv, "private")]
        audits.append(Audit(aid, _date(rec["date"]), str(rec["commit"]), rec["kind"], [str(s) for s in rec.get("scope", [])], priv,
                            frozenset(rec.get("prints", [])), bool(rec.get("uncommitted", False))))
    bad += [f"private audits/{aid}.toml has no public half (security/audits/{aid}.toml)" for aid in secret if aid not in public]
    audits.sort(key=lambda a: (a.date, int(a.id.rsplit("-", 1)[1])))
    return audits, have_private, bad


def rules(private: str | None = None) -> dict:
    root = private_root() if private is None else private
    try:
        with open(os.path.join(root, "rules.toml"), "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# ---------------------------------------------------------------- who an audit counts for
def _version(v: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", v))


def accepts(entry: str, auditor: str, thoroughness: int) -> bool:
    """Whether a rule's ``accept`` entry takes this auditor at this thoroughness. An entry: ``any``, ``human``, a family
    (``family``), a family at a version or later within it (``family>=2.1``), or an exact auditor (``family-2.1``,
    ``human:sam``); then, optionally, ``thoroughness<=N``."""
    words = entry.split()
    if not words:
        return False
    who, rest = words[0], words[1:]
    for w in rest:
        m = re.match(r"^thoroughness<=(\d)$", w)
        if not m:
            return False   # (a word this doesn't know: never a pass by mistake)
        if thoroughness > int(m.group(1)):
            return False
    if who == "any":
        return True
    if who == "human":
        return auditor.startswith("human:")
    m = re.match(r"^([a-z]+)>=([\d.]+)$", who)
    if m:
        fam, _, ver = auditor.partition("-")
        return fam == m.group(1) and bool(ver) and _version(ver) >= _version(m.group(2))
    if "-" in who or ":" in who:
        return auditor == who
    return auditor.partition("-")[0] == who


# ---------------------------------------------------------------- the code, and what each audit saw of it
def code_files(base: str | None = None) -> list[str]:
    """The app code's files (dev/audit.toml's globs), in this project's folder or ``base`` (a copy of it at a commit)."""
    base = base or ROOT
    cfg = config()
    out: set[str] = set()
    for pattern in cfg.get("code", []):
        out.update(os.path.relpath(p, base) for p in glob.glob(os.path.join(base, pattern), recursive=True) if os.path.isfile(p))
    for pattern in cfg.get("exclude", []):
        out -= {os.path.relpath(p, base) for p in glob.glob(os.path.join(base, pattern), recursive=True)}
    return sorted(out)


def _text(rel: str, base: str | None = None) -> list[str]:
    with open(os.path.join(base or ROOT, rel), encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()


def _lines(rel: str, base: str | None = None) -> set[int]:
    """The lines that count: every one with something on it."""
    return {n for n, text in enumerate(_text(rel, base), 1) if text.strip()}


def fingerprints(rel: str, base: str | None = None) -> dict[int, str]:
    """Each line's fingerprint (the lines that count): a hash of the file's path, the line, and the lines with something
    on them just above and below it. Trailing spaces don't count; indenting does (it can change what code does). Moving a
    block keeps all but its edges; moving it to another file, or changing a line or its neighbour, doesn't. Public on
    purpose: it's a hash of code that is public."""
    return _prints_of(rel, _text(rel, base))


def _prints_of(rel: str, text: list[str]) -> dict[int, str]:
    rows = [(n, line.rstrip()) for n, line in enumerate(text, 1) if line.strip()]
    out = {}
    for i, (n, line) in enumerate(rows):
        above = rows[i - 1][1] if i else ""
        below = rows[i + 1][1] if i + 1 < len(rows) else ""
        out[n] = hashlib.sha256("\n".join((rel, above, line, below)).encode()).hexdigest()[:12]
    return out


def blame(rel: str, where: tuple[str, str]) -> dict[int, tuple[str | None, float | None]]:
    """Each line's last change: its commit and when it was committed, or (None, None) when it isn't committed."""
    repo, commit = where
    got = subprocess.run(["git", "-C", repo, "blame", "--line-porcelain", *([commit] if commit else []), "--", rel],
                         capture_output=True, text=True, timeout=120)
    if got.returncode != 0:
        return {}   # (not in git yet: every line uncommitted)
    out: dict[int, tuple[str | None, float | None]] = {}
    line, sha = 0, ""
    for row in got.stdout.splitlines():
        if row.startswith("\t"):
            continue
        parts = row.split(" ")
        if len(parts) >= 3 and len(parts[0]) == 40 and parts[1].isdigit() and parts[2].isdigit():
            sha, line = parts[0], int(parts[2])
            out[line] = (None, None) if set(sha) == {"0"} else (sha, out.get(line, (sha, None))[1])
        elif parts[0] == "committer-time" and set(sha) != {"0"}:
            out[line] = (sha, float(parts[1]))
    return out


def history(commit: str, repo: str) -> set[str] | None:
    """Every commit up to and including ``commit``, or None when git doesn't know it here."""
    got = subprocess.run(["git", "-C", repo, "rev-list", commit, "--"], capture_output=True, text=True, timeout=60)
    return set(got.stdout.split()) if got.returncode == 0 else None


def function_lines(rel: str, name: str, base: str | None = None) -> set[int] | None:
    """A Python function's lines (``name`` dotted through its classes: ``Server.login``), or None when there's no such."""
    if not rel.endswith(".py"):
        return None
    try:
        tree = ast.parse("\n".join(_text(rel, base)))
    except SyntaxError:
        return None
    want = name.split(".")

    def find(nodes, path):
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == path[0]:
                if len(path) == 1:
                    first = min([n.lineno, *(d.lineno for d in n.decorator_list)])
                    return set(range(first, (n.end_lineno or n.lineno) + 1))
                return find(n.body, path[1:])
        return None
    return find(tree.body, want)


def scope_lines(a: Audit, files: dict[str, set[int]], base: str | None = None) -> tuple[dict[str, set[int]], list[str]]:
    """The lines an audit's scope takes in, by file (a full audit: all of them), and the scope entries that name nothing."""
    if a.kind == "full":
        return dict(files), []
    out: dict[str, set[int]] = {}
    missing = []
    for entry in a.scope:
        rel, _, func = entry.partition(":")
        if rel not in files:
            missing.append(entry)
            continue
        if not func:
            out[rel] = set(files[rel])
            continue
        got = function_lines(rel, func, base)
        if got is None:
            missing.append(entry)
            continue
        out[rel] = out.get(rel, set()) | (got & files[rel])
    return out, missing


@dataclass
class Line:
    sha: str | None   # (None: not committed yet)
    when: float | None
    seen: list[Audit] = field(default_factory=list)
    fingerprint: str = ""
    pushed: bool | None = None   # (None: no upstream to tell)


@dataclass
class Map:
    """Every app code line, and the audits that saw each."""
    lines: dict[str, dict[int, Line]]
    notes: list[str] = field(default_factory=list)

    def all(self):
        for rel, lines in self.lines.items():
            for n, ln in lines.items():
                yield rel, n, ln


def measure(audits: list[Audit]) -> Map:
    where = _repo()
    files = {rel: _lines(rel) for rel in code_files()}
    blamed: dict[str, dict[int, tuple[str | None, float | None]]] = {}
    notes = []
    if where is None:
        notes.append("no git history here: no line can be placed against an audit")
    else:
        with ThreadPoolExecutor(8) as pool:
            for rel, got in zip(files, pool.map(lambda r: blame(r, where), files), strict=True):
                blamed[rel] = got
    m = Map({rel: {n: Line(*blamed.get(rel, {}).get(n, (None, None))) for n in sorted(ns)} for rel, ns in files.items()}, notes)
    for rel, lines in m.lines.items():
        for n, p in fingerprints(rel).items():
            if n in lines:
                lines[n].fingerprint = p
    pushed = history("@{upstream}", where[0]) if where is not None else None
    if pushed is not None:
        for *_, ln in m.all():
            ln.pushed = bool(ln.sha) and ln.sha in pushed
    for a in audits:
        before = history(a.commit, where[0]) if where is not None else None
        if before is None and not a.prints:
            if where is not None:
                notes.append(f"audit {a.id}: git doesn't know its commit {a.commit[:12]} here")
            continue
        if a.prints:
            for *_, ln in m.all():
                if ln.fingerprint in a.prints:
                    ln.seen.append(a)
        if before is None:
            continue   # (its fingerprints still place it)
        took, missing = scope_lines(a, files)
        notes += [f"audit {a.id}: its scope {e} names nothing in the app code now" for e in missing if not a.prints]
        for rel, ns in took.items():
            for n in ns:
                ln = m.lines[rel].get(n)
                if ln and ln.sha and ln.sha in before and a not in ln.seen:
                    ln.seen.append(a)
    return m


# ---------------------------------------------------------------- the rules
def _pct(n: int, of: int) -> float:
    return 100.0 * n / of if of else 100.0


def _by_file(spots: list[tuple[str, int]], what: str, severity: str) -> list[Finding]:
    by: dict[str, list[int]] = {}
    for rel, n in spots:
        by.setdefault(rel, []).append(n)
    order = sorted(by, key=lambda r: -len(by[r]))
    out = [Finding("Audit", rel, min(by[rel]), 0, severity, f"{counted(len(by[rel]), 'line')} {what}", "unaudited") for rel in order[:TOP]]
    if len(order) > TOP:
        rest = sum(len(by[r]) for r in order[TOP:])
        out.append(Finding("Audit", "app code", 0, 0, severity, f"{counted(rest, 'more line')} {what}, in {counted(len(order) - TOP, "more file")}", "unaudited"))
    return out


def coverage_check(m: Map, audits: list[Audit], now: float, sensitive: list[str]) -> Outcome:
    """The dev rule: what no audit has seen waits no longer than the grace window (its oldest line, or its share of the
    code), and sensitive code (the private rules.toml) doesn't wait at all."""
    dev = config().get("dev", {})
    grace_days, grace_share = float(dev.get("grace_days", 7)), float(dev.get("grace_share", 10))
    rows = list(m.all())
    total = len(rows)
    seen = sum(1 for *_, ln in rows if ln.seen)
    full = sum(1 for *_, ln in rows if any(a.kind == "full" for a in ln.seen))
    waiting = [(rel, n, ln) for rel, n, ln in rows if not ln.seen]
    oldest = min((ln.when for *_, ln in waiting if ln.when), default=None)
    late = oldest is not None and now - oldest > grace_days * DAY
    big = _pct(len(waiting), total) > grace_share
    touchy = [(rel, n) for rel, n, ln in waiting if ln.sha and _sensitive(rel, sensitive)]
    found = []
    if late or big:
        why = []
        if late and oldest is not None:
            why.append(f"the oldest has waited {int((now - oldest) // DAY)} days (grace {grace_days:g})")
        if big:
            why.append(f"they're {_pct(len(waiting), total):.1f}% of the app code (grace {grace_share:g}%)")
        found.append(Finding("Audit", "app code", 0, 0, "warning",
                             f"{counted(len(waiting), 'line')} no audit has seen: {", ".join(why)}", "unaudited"))
        found += _by_file([(rel, n) for rel, n, _ in waiting], "no audit has seen", "warning")
    elif touchy:
        found += _by_file(touchy, "of sensitive code no audit has seen", "warning")
    fulls = [a for a in audits if a.kind == "full"]
    facts = [f"audited {_pct(seen, total):.1f}%", f"full {_pct(full, total):.1f}%",
             f"last full {fulls[-1].date.isoformat()}" if fulls else "no full audit yet"]
    unpushed = sum(1 for *_, ln in waiting if not ln.sha or ln.pushed is False)
    if unpushed:
        facts.append(f"{counted(unpushed, 'line')} to audit before a push")   # (uncommitted, or committed and not pushed)
    return Outcome("Audit coverage", "", found, facts=facts)


def _sensitive(rel: str, paths: list[str]) -> bool:
    return any(rel == p or rel.startswith(p.rstrip("/") + "/") or fnmatch.fnmatch(rel, p) for p in paths)


def _spot(spot: str) -> tuple[str, int]:
    rel, _, nums = str(spot).rpartition(":")
    return rel, int(nums.split("-")[0]) if nums.split("-")[0].isdigit() else 0


def findings_check(audits: list[Audit]) -> Outcome:
    """The open findings (the private half), each on its line: a warning, as they're being worked on."""
    found, gone, cache = [], 0, {}
    for a in audits:
        for f in a.findings:
            if f.get("status") == "open":
                rel, n = _spot((f.get("lines") or ["?:0"])[0])
                code = code_state(f, a, cache)
                if code == "gone":   # (off the list: to confirm on the audits page, not to fix)
                    gone += 1
                    continue
                said = " (some of its code has changed)" if code == "changed" else ""
                found.append(Finding("Audit", rel, n, 0, "warning", f"{f.get('id')} {_rated(f)}: {f.get('title')}{said}", "finding"))
    facts = [counted(len(found), "open finding") if found else "no open findings"]
    if gone:
        facts.append(f"{gone} to confirm fixed (their code is gone)")
        found.append(Finding("Audit", "findings", 0, 0, "warning", f"{counted(gone, 'finding')} whose code is gone: confirm they're fixed, "
                             "or have them re-checked, on the audits page's Findings", "finding"))
    known = test_names()
    for a in audits:
        for f in a.findings:
            lost = [t for t in f.get("tests", []) if t not in known]
            if lost and f.get("status") != "false-positive":
                rel, n = _spot((f.get("lines") or ["?:0"])[0])
                found.append(Finding("Audit", rel, n, 0, "warning", f"{f.get('id')}'s guarding test{'s' if len(lost) > 1 else ''} "
                                     f"{', '.join(lost)} {'are' if len(lost) > 1 else 'is'} gone from tests/", "finding"))
            if f.get("status") == "fixed":
                at, _ = fix_where(f, a)
                said = {"working copy": "is fixed only in your working copy: commit and push the fix",
                        "committed": "is fixed in a commit that isn't pushed yet"}.get(at)
                if said:
                    rel, n = _spot((f.get("lines") or ["?:0"])[0])
                    found.append(Finding("Audit", rel, n, 0, "warning", f"{f.get('id')} {said}", "finding"))
    return Outcome("Findings", "", found, facts=facts)


def _rated(f: dict) -> str:
    """A finding's severity, with its CVSS score when it has one (a hardening finding: its priority)."""
    if f.get("type") == "hardening":
        return f"hardening, {f.get('severity')}"
    try:
        return f"{f.get('severity')} {cvss(str(f['cvss']))[0]:.1f}" if f.get("cvss") else str(f.get("severity"))
    except ValueError:
        return str(f.get("severity"))


def _rc(where: tuple[str, str] | None) -> tuple[str, float | None]:
    """The newest tag on this code, when it's a release candidate (vX.Y.Z-rc.N), and when it was made."""
    if where is None:
        return "", None
    repo, commit = where
    got = subprocess.run(["git", "-C", repo, "describe", "--tags", "--abbrev=0", "--match", "v*", commit or "HEAD"],
                         capture_output=True, text=True, timeout=30)
    tag = got.stdout.strip()
    if got.returncode != 0 or "-rc." not in tag:
        return "", None
    when = subprocess.run(["git", "-C", repo, "for-each-ref", "--format=%(creatordate:unix)", f"refs/tags/{tag}"],
                          capture_output=True, text=True, timeout=30)   # (an annotated tag: when it was made; else its commit's)
    return tag, float(when.stdout.strip()) if when.stdout.strip().isdigit() else None


def valid_accept(entry: str) -> bool:
    words = entry.split()
    if not words or not all(re.match(r"^thoroughness<=[1-5]$", w) for w in words[1:]):
        return False
    who = words[0]
    return who in ("any", "human") or bool(re.match(r"^([a-z]+(>=[\d.]+|-[\d.]+)?|human:\S+)$", who))


def config_check(have_private: bool, rule: dict, release: bool) -> Outcome:
    """Whether the auditor and thoroughness requirements are set (the private repo's rules.toml). Without the private
    repo (anyone's clone, GitHub) that's a notice, never a failure: the public records stand on their own, and what's
    required of who audits stays private. With it, rules that are missing or can't be read are a warning (at release:
    an error), so a requirement that isn't there doesn't go unnoticed."""
    if not have_private:
        return Outcome("Audit config", "", [Finding("Audit", "dev/audit.toml", 0, 0, "information",
                       f"no private audit config at {private_root()}: auditor and thoroughness requirements, findings and "
                       "the release rules aren't checked (the public records only)", "config")],
                       facts=["public records only"], failed=False)
    sev = "error" if release else "warning"
    found = []
    release_rule = rule.get("release", {})
    if not release_rule.get("accept"):
        found.append(Finding("Audit", "rules.toml", 0, 0, sev, "the private rules.toml has no [release] accept: no auditor is required", "config"))
    for entry in release_rule.get("accept", []):
        if not valid_accept(str(entry)):
            found.append(Finding("Audit", "rules.toml", 0, 0, sev, f"the private rules.toml's accept entry {entry!r} can't be read", "config"))
    return Outcome("Audit config", "", found, facts=["auditor requirements set"] if not found else [counted(len(found), "config problem")])


def release_check(m: Map, audits: list[Audit], have_private: bool, rule: dict, now: float, release: bool) -> Outcome:
    """The release rules (the private rules.toml's [release]): a full audit by an auditor it accepts, no older than
    max_age_days, and every line since seen by one it accepts; the RC soaked soak_days; no open finding at block_open or
    above; every finding's answer for main. Errors at release (red); warnings on dev once an RC is tagged."""
    sev = "error" if release else "warning"
    accept = list(rule.get("accept", []))
    max_age = float(rule.get("max_age_days", 180))
    today = datetime.date.fromtimestamp(now)
    fresh = lambda a: (today - a.date).days <= max_age   # noqa: E731
    ok = lambda a: not a.author and fresh(a) and any(accepts(e, a.auditor, a.thoroughness) for e in accept)   # noqa: E731
    found = []
    if not any(a.kind == "full" and ok(a) for a in audits):
        found.append(Finding("Audit", "app code", 0, 0, sev, f"no full audit the release rules accept in the last {max_age:g} days", "release"))
    short = [(rel, n) for rel, n, ln in m.all() if not any(ok(a) for a in ln.seen)]
    found += _by_file(short, "not seen by an audit the release rules accept", sev)
    tag, made = _rc(_repo())
    soak = float(rule.get("soak_days", 7))
    if release and not tag:
        found.append(Finding("Audit", "git tags", 0, 0, sev, "no release candidate tagged (vX.Y.Z-rc.N) on this code", "release"))
    elif tag and made is not None and now - made < soak * DAY:
        found.append(Finding("Audit", "git tags", 0, 0, sev, f"{tag} has soaked {max(0.0, now - made) / DAY:.1f} of {soak:g} days", "release"))
    block = rule.get("block_open", "high")
    for a in audits:
        for f in a.findings:
            rel, n = _spot((f.get("lines") or ["?:0"])[0])
            if (f.get("status") == "open" and f.get("type") != "hardening"
                    and SEVERITIES.index(f.get("severity", "info")) >= SEVERITIES.index(block)):
                found.append(Finding("Audit", rel, n, 0, sev, f"{f.get('id')} is open at {_rated(f)}", "release"))
            if f.get("main") not in MAIN:
                found.append(Finding("Audit", rel, n, 0, sev, f"{f.get('id')} has no answer for main", "release"))
    facts = ["release rules met"] if not found else [f"{counted(len(found), 'release rule')} not met"]
    return Outcome("Release rules", "", found, facts=facts)


# ---------------------------------------------------------------- next, start, record
def next_batches(m: Map, sensitive: list[str], now: float) -> list[tuple[str, int, float]]:
    """The files with lines no audit has seen: sensitive first, then the longest waiting, then the most lines."""
    by: dict[str, list[Line]] = {}
    for rel, _, ln in m.all():
        if not ln.seen:
            by.setdefault(rel, []).append(ln)
    rows = [(rel, len(ls), min((ln.when or now) for ln in ls)) for rel, ls in by.items()]
    return sorted(rows, key=lambda r: (not _sensitive(r[0], sensitive), r[2], -r[1]))


def _git(*args: str) -> str:
    got = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, timeout=30)
    return got.stdout.strip() if got.returncode == 0 else ""


def main_commit() -> str:
    return _git("rev-parse", "origin/main") or _git("rev-parse", "main")


def prompt(paths: list[str], private: str | None = None, commit: str = "", main: str = "") -> str:
    """The standard prompt (the private PROMPT.md, below its ---), filled in: a full audit, or a partial of ``paths``."""
    root = private_root() if private is None else private
    with open(os.path.join(root, "PROMPT.md"), encoding="utf-8") as fh:
        text = fh.read().split("\n---\n", 1)[-1].strip()
    commit = commit or _git("rev-parse", "HEAD") or "?"
    main = main or main_commit() or "?"
    kind = "partial" if paths else "full"
    text = text.replace("<commit>", commit).replace("<main_commit>", main).replace("<full | partial>", kind)
    text = text.replace("<all app code | the list below>", "the list below" if paths else "all app code")
    return text.replace("<scope list, for a partial audit>", "\n".join(f"- `{p}`" for p in paths))


# ---------------------------------------------------------------- what to audit: a target, as a scope
TARGETS = {
    "full": "All the app code (a full audit)",
    "uncommitted": "Uncommitted changes",
    "unpushed": "Unpushed and uncommitted changes",
    "since-audit": "Everything changed since the last audit",
    "branch": "This branch against main",
    "unaudited": "Everything no audit has seen",
    "chosen": "Files and functions I choose",
    "open": "Are the open findings fixed?",
    "open-changed": "The open findings, and everything changed since the last audit",
}


def changed_lines(ref: str) -> dict[str, set[int]]:
    """The app code's lines that differ, in this folder now, from ``ref`` (uncommitted changes included; a file git
    doesn't track yet: all of it)."""
    files = code_files()
    out: dict[str, set[int]] = {}
    got = subprocess.run(["git", "-C", ROOT, "diff", "-U0", "--no-color", "--no-ext-diff", ref, "--", *files],
                         capture_output=True, text=True, timeout=120)
    rel = ""
    for row in got.stdout.splitlines():
        if row.startswith("+++ "):
            rel = row[6:] if row.startswith("+++ b/") else ""
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", row)
        if m and rel in files:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            out.setdefault(rel, set()).update(range(start, start + count))
    new = subprocess.run(["git", "-C", ROOT, "ls-files", "--others", "--exclude-standard", "--", *files],
                         capture_output=True, text=True, timeout=60)
    for rel in new.stdout.split():
        out[rel] = _lines(rel)
    return {rel: ns & _lines(rel) for rel, ns in out.items() if ns & _lines(rel)}


def _defs(rel: str) -> list[tuple[int, int, str]]:
    """A Python file's functions and classes: their lines (decorators too) and dotted names."""
    try:
        tree = ast.parse("\n".join(_text(rel)))
    except SyntaxError:
        return []
    out = []

    def walk(nodes, prefix):
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + n.name
                first = min([n.lineno, *(d.lineno for d in n.decorator_list)])
                out.append((first, n.end_lineno or n.lineno, name))
                walk(n.body, name + ".")
    walk(tree.body, "")
    return out


def scope_of(lines: dict[str, set[int]]) -> list[str]:
    """The smallest scope that takes in these lines: in Python, the innermost function or class around each (a line
    outside any: the whole file); any other file, whole."""
    out: list[str] = []
    for rel in sorted(lines):
        if not rel.endswith(".py") or lines[rel] >= _lines(rel):   # (all of it: a new file, say)
            out.append(rel)
            continue
        defs = _defs(rel)
        names: set[str] = set()
        whole = False
        for n in lines[rel]:
            around = [d for d in defs if d[0] <= n <= d[1]]
            if not around:
                whole = True
                break
            names.add(min(around, key=lambda d: d[1] - d[0])[2])
        if whole:
            out.append(rel)
            continue
        # (a function inside another named too: the outer one takes it in)
        out += [f"{rel}:{x}" for x in sorted(names) if not any(x.startswith(y + ".") for y in names)]
    return out


def target_scope(target: str, paths: list[str], audits: list[Audit]) -> tuple[str, list[str], str]:
    """A target's kind and scope, or the reason there's nothing to audit (the third: empty when there is)."""
    if target == "full":
        return "full", [], ""
    if target == "chosen":
        files = {rel: _lines(rel) for rel in code_files()}
        wrong = [p for p in paths if p.partition(":")[0] not in files or
                 (p.partition(":")[2] and function_lines(p.partition(":")[0], p.partition(":")[2]) is None)]
        if wrong:
            return "partial", [], f"not in the app code: {', '.join(wrong)}"
        return ("partial", sorted(set(paths)), "") if paths else ("partial", [], "nothing chosen")
    if target in ("open", "open-changed"):
        if not any(f.get("status") == "open" for a in audits for f in a.findings):
            return "partial", [], "no open findings (they're in the private repo)"
        scope = open_scope(audits)
        if target == "open-changed" and audits:
            more = target_scope("since-audit", [], audits)
            scope = sorted(set(scope) | set(more[1]))
        return "partial", scope, ""
    if target == "unaudited":
        m = measure(audits)
        lines: dict[str, set[int]] = {}
        for rel, n, ln in m.all():
            if not ln.seen:
                lines.setdefault(rel, set()).add(n)
        return ("partial", scope_of(lines), "") if lines else ("partial", [], "every line has been seen by an audit")
    if target == "uncommitted":
        ref, what = "HEAD", "no uncommitted changes to the app code"
    elif target == "unpushed":
        ref, what = (_git("rev-parse", "@{upstream}"), "nothing unpushed or uncommitted in the app code")
        if not ref:
            return "partial", [], "this branch has no upstream to compare with"
    elif target == "since-audit":
        if not audits:
            return "partial", [], "there's no audit yet: start with a full one"
        ref, what = audits[-1].commit, f"nothing changed since the last audit ({audits[-1].id})"
        if not _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            return "partial", [], f"git doesn't know the last audit's commit {ref[:12]} here"
    elif target == "branch":
        main = main_commit()
        ref = _git("merge-base", "HEAD", main) if main else ""
        what = "nothing on this branch that main doesn't have"
        if not ref:
            return "partial", [], "no main here to compare with"
    else:
        return "partial", [], f"no such target {target!r}"
    lines = changed_lines(ref)
    return ("partial", scope_of(lines), "") if lines else ("partial", [], what)


def scope_size(kind: str, scope: list[str]) -> int:
    """How many lines a scope takes in, now."""
    files = {rel: _lines(rel) for rel in code_files()}
    took, _ = scope_lines(Audit("", datetime.date.today(), "", kind, scope), files)
    return sum(len(ns) for ns in took.values())


def next_ids(day: datetime.date, root: str) -> tuple[str, int]:
    """The next free audit id for a day, and the next finding number for its year (F-YYYY-NNN)."""
    n = 1
    while os.path.exists(os.path.join(PUBLIC, f"{day.isoformat()}-{n}.toml")) or \
            os.path.exists(os.path.join(root, "audits", f"{day.isoformat()}-{n}.toml")):
        n += 1
    top = 0
    secret, _ = _load_dir(os.path.join(root, "audits"))
    for rec in secret.values():
        for f in rec.get("finding", []):
            m = re.match(rf"^F-{day.year}-(\d+)$", str(f.get("id", "")))
            if m:
                top = max(top, int(m.group(1)))
    return f"{day.isoformat()}-{n}", top + 1


def _toml_str(v: str) -> str:
    if "\n" in v:
        return '"""\n' + v.replace("\\", "\\\\").replace('"""', '\\"""').strip("\n") + '\n"""'
    return json.dumps(v, ensure_ascii=False)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return _toml_str(str(v))


def _toml_list(k: str, v: list, per: int = 6) -> str:
    """A long list over several lines (prints: thousands of them), so a diff of it reads."""
    rows = [", ".join(_toml_value(x) for x in v[i:i + per]) for i in range(0, len(v), per)]
    return f"{k} = [\n" + "".join(f"    {r},\n" for r in rows) + "]"


def dump(rec: dict, keys: list[str], tables: str = "") -> str:
    out = [_toml_list(k, rec[k]) if isinstance(rec.get(k), list) and len(rec[k]) > 6 else f"{k} = {_toml_value(rec[k])}"
           for k in keys if k in rec and rec[k] not in ("", None, [], False)]
    for t in rec.get(tables, []) if tables else []:
        out += ["", f"[[{tables}]]"] + [f"{k} = {_toml_value(v)}" for k, v in t.items() if v not in ("", None, [])]
    return "\n".join(out) + "\n"


PUBLIC_KEYS = ["id", "date", "commit", "kind", "scope", "uncommitted", "prints"]
PRIVATE_KEYS = ["id", "auditor", "thoroughness", "author", "mode", "main_commit", "prompt", "run", "notes"]


def introduced(spot: str, commit: str, repo: str) -> str:
    """The newest commit among a finding's lines at the audited commit: where the bug came in."""
    rel, _, nums = spot.rpartition(":")
    a, _, b = nums.partition("-")
    got = subprocess.run(["git", "-C", repo, "blame", "--line-porcelain", "-L", f"{a},{b or a}", commit, "--", rel],
                         capture_output=True, text=True, timeout=60)
    best, best_t, sha = "", -1.0, ""
    for row in got.stdout.splitlines():
        parts = row.split(" ")
        if len(parts) >= 3 and len(parts[0]) == 40 and parts[1].isdigit():
            sha = parts[0]
        elif parts[0] == "committer-time" and float(parts[1]) > best_t:
            best, best_t = sha, float(parts[1])
    return best


def missed(rec: dict, f: dict, audits: list[Audit], repo: str) -> list[str]:
    """The earlier audits that had a finding's lines in scope after the bug came in, and didn't report it."""
    came = f.get("introduced_in", "")
    if not came:
        return []
    files = {rel: _lines(rel) for rel in code_files()}
    out = []
    for a in audits:
        if a.id == rec["id"] or (a.date, a.id) >= (_date(rec["date"]), rec["id"]):
            continue
        before = history(a.commit, repo)
        if not before or came not in before:
            continue
        took, _ = scope_lines(a, files)
        rels = {_spot(s)[0] for s in f.get("lines", [])}
        if any(r in took for r in rels):
            out.append(a.id)
    return out


def _rev(commit: str, repo: str) -> str:
    got = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], capture_output=True, text=True, timeout=30)
    return got.stdout.strip() if got.returncode == 0 else ""


def _spans(spot: str) -> tuple[str, set[int]]:
    rel, _, nums = str(spot).rpartition(":")
    a, _, b = nums.partition("-")
    if not a.isdigit():
        return rel, set()
    return rel, set(range(int(a), int(b or a) + 1)) if (b or a).isdigit() else {int(a)}


def spot_prints(spots: list[str], base: str | None = None, text=None) -> list[str]:
    """The fingerprints of a finding's lines (``path:start-end`` each), in ``base`` (or ``text(rel)``'s lines)."""
    out: set[str] = set()
    for spot in spots:
        rel, ns = _spans(spot)
        try:
            fp = _prints_of(rel, text(rel)) if text else fingerprints(rel, base)
        except (OSError, ValueError):
            continue
        out.update(fp[n] for n in ns if n in fp)
    return sorted(out)


def _at(commit: str, repo: str):
    def text(rel: str) -> list[str]:
        got = subprocess.run(["git", "-C", repo, "show", f"{commit}:{rel}"], capture_output=True, timeout=60)
        if got.returncode != 0:
            raise OSError(f"{rel} isn't in {commit[:12]}")
        return got.stdout.decode("utf-8", "replace").splitlines()
    return text


def _finding_prints(f: dict, a: Audit) -> set[str]:
    prints = set(f.get("prints") or [])
    if not prints and not a.uncommitted and (where := _repo()):
        prints = set(spot_prints(f.get("lines", []), text=_at(a.commit, where[0])))
    return prints


def _have(rel: str, cache: dict[str, dict[str, int]], text=None) -> dict[str, int]:
    """A file's lines by fingerprint: in this folder now, or ``text(rel)``'s."""
    if rel not in cache:
        try:
            got = _prints_of(rel, text(rel)) if text else fingerprints(rel)
        except (OSError, ValueError):
            got = {}
        cache[rel] = {p: n for n, p in got.items()}
    return cache[rel]


def code_state(f: dict, a: Audit, cache: dict | None = None, text=None) -> str:
    """Where a finding's lines are in the code now (this folder, uncommitted changes too; or ``text(rel)``'s, a commit's):
    ``there`` (every one as it was), ``changed`` (some), ``gone`` (none: changed or taken out), or ``unknown`` (no
    fingerprints to tell by: a record from before findings kept them, of code that wasn't committed). Worked out each
    time, never stored: undo the change and it's there again."""
    cache = {} if cache is None else cache
    prints = _finding_prints(f, a)
    if not prints:
        return "unknown"
    have: set[str] = set()
    for rel in {_spans(spot)[0] for spot in f.get("lines", [])}:
        have |= set(_have(rel, cache, text))
    kept = len(prints & have)
    return "there" if kept == len(prints) else "changed" if kept else "gone"


def locate(f: dict, a: Audit, cache: dict | None = None) -> list[str]:
    """Where a finding's lines are now (``path:start-end``, followed by their fingerprints: they may have moved); those
    that are gone, left out."""
    cache = {} if cache is None else cache
    prints = _finding_prints(f, a)
    out = []
    for rel in sorted({_spans(spot)[0] for spot in f.get("lines", [])}):
        have = _have(rel, cache)
        ns = sorted(have[p] for p in prints if p in have)
        order = {n: i for i, n in enumerate(sorted(have.values()))}   # (blank lines between two: still one span)
        start = prev = None
        for n in ns + [None]:
            if n is not None and prev is not None and order[n] == order[prev] + 1:
                prev = n
                continue
            if start is not None:
                out.append(f"{rel}:{start}" + (f"-{prev}" if prev != start else ""))
            start = prev = n
    return out


def fix_where(f: dict, a: Audit) -> tuple[str, str]:
    """Where a finding's fix is, from git: ``pushed`` (its code is changed on the branch's upstream), ``committed`` (in a
    commit not pushed yet), ``working copy`` (only in this folder, not committed), ``not fixed`` (its code is as it
    was everywhere), or ``unknown``; and the commit that changed it, when there is one."""
    where = _repo()
    if where is None or not _finding_prints(f, a):
        return "unknown", ""
    repo = where[0]
    files = sorted({_spans(spot)[0] for spot in f.get("lines", [])})
    for name, ref in (("pushed", "@{upstream}"), ("committed", "HEAD")):
        rev = _rev(ref, repo)
        if rev and code_state(f, a, {}, _at(rev, repo)) != "there":
            got = subprocess.run(["git", "-C", repo, "rev-list", "--reverse", f"{a.commit}..{rev}", "--", *files],
                                 capture_output=True, text=True, timeout=60)
            for sha in got.stdout.split() if got.returncode == 0 else []:
                if code_state(f, a, {}, _at(sha, repo)) != "there":
                    return name, sha
            return name, rev
    return ("working copy", "") if code_state(f, a) != "there" else ("not fixed", "")


def open_scope(audits: list[Audit]) -> list[str]:
    """What a re-check of the open findings reads: the functions around each one's lines now (a file whole, when its
    lines are gone)."""
    lines: dict[str, set[int]] = {}
    whole: set[str] = set()
    files = set(code_files())
    cache: dict = {}
    for a in audits:
        for f in a.findings:
            if f.get("status") != "open":
                continue
            spots = locate(f, a, cache)
            for spot in spots:
                rel, ns = _spans(spot)
                lines.setdefault(rel, set()).update(ns)
            if not spots:
                whole |= {rel for rel in (_spans(s)[0] for s in f.get("lines", [])) if rel in files}
    scope = scope_of({rel: ns for rel, ns in lines.items() if rel not in whole and rel in files})
    return sorted(set(scope) | whole)


def test_names(base: str | None = None) -> set[str]:
    """The project's tests, as a finding names them: Class.test_name (and a bare test_name, for one not in a class)."""
    out: set[str] = set()
    for path in sorted(glob.glob(os.path.join(base or ROOT, "tests", "**", "test*.py"), recursive=True)):
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError, ValueError):
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                out |= {f"{node.name}.{m.name}" for m in node.body
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test")}
    return out


def answers(text: str, key: str) -> list[dict]:
    """The ``[[key]]`` blocks in an answer's TOML (its last fenced block, or the whole of it)."""
    blocks = re.findall(r"```(?:toml)?\s*\n(.*?)```", text, re.S)
    try:
        got = tomllib.loads(blocks[-1] if blocks else text).get(key, [])
    except tomllib.TOMLDecodeError:
        return []
    return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []


def guards(tests: object, known: set[str]) -> tuple[list[str], list[str]]:
    """Tests named as guarding a finding: those the project has, and those it doesn't."""
    names = [str(t).strip() for t in (tests if isinstance(tests, list) else str(tests or "").replace(",", " ").split())]
    names = [t.split("::")[-1].removeprefix("tests.test_plexcache.") for t in names if t]
    return [t for t in names if TEST.match(t) and t in known], [t for t in names if not (TEST.match(t) and t in known)]


REPORT = """When you're done, end your answer with a report of each finding, in TOML:

```toml
[[fix]]
id = "F-YYYY-NNN"
status = "fixed | attempted | not-real"
tests = ["TestClass.test_name"]   # the tests that would fail if it came back, added or changed for it
detail = \"\"\"
What you changed, and why it fixes it (or why it isn't real).
\"\"\"
```

The report is pasted into the project's security records, where each finding keeps the tests that guard it."""


def brief(items: list[tuple[Audit, dict]]) -> str:
    """Findings, written up for whoever fixes them (a person, or an AI assistant): what's wrong, where it is now, how
    bad, and what to do."""
    out = ["# Security findings to fix", "",
           "Fix each finding below in this repository. Keep each fix to what the finding needs, and don't change unrelated "
           "code. Keep the tests passing, and add a test that would have caught it where you can. If a finding turns out not "
           "to be real, say why instead of changing the code.", "", REPORT, ""]
    cache: dict = {}
    for a, f in items:
        if f.get("type") == "hardening":
            how = f"hardening (not exploitable as it is), priority {f.get('severity')}"
        elif f.get("cvss"):
            try:
                score = f"{cvss(str(f['cvss']))[0]:.1f} "
            except ValueError:
                score = ""
            how = f"vulnerability, CVSS 3.1 {score}({f.get('severity')}), `{f['cvss']}`"
        else:
            how = f"vulnerability, {f.get('severity')}"
        now = locate(f, a, cache)
        was = ", ".join(f.get("lines", []))
        where = (", ".join(now) + (f" (it was at {was} when it was found)" if now != list(f.get("lines", [])) else "")
                 if now else f"its lines have changed since it was found at {was}: look for it there")
        out += [f"## {f.get('id')}: {f.get('title')}", "", f"- Kind: {how}" + (f", {f['cve']}" if f.get("cve") else ""), f"- Where it is now: {where}",
                f"- Found by audit {a.id}, at commit {a.commit[:12]}" + (" (with uncommitted changes)" if a.uncommitted else ""),
                f"- On main: {f.get('main', 'affected')}"]
        if f.get("tests"):
            out.append(f"- Tests that guard it: {', '.join(f['tests'])}")
        out += ["", str(f.get("detail", "")).strip(), ""]
    return "\n".join(out).rstrip() + "\n"


def read_prints(rec: dict, where: tuple[str, str] | None, snapshot: str = "") -> tuple[list[str], bool]:
    """The fingerprints of the lines a record's audit read, and whether any of them weren't committed. Its commit is the
    one you're on (HEAD): the code in this folder now, uncommitted changes included, as the auditor read it. Another
    commit: the code at that commit, from git. Each finding's own lines' fingerprints are put in it too (its ``prints``:
    how it's known later whether its code is still there)."""
    a = Audit(str(rec.get("id")), _date(rec["date"]), str(rec["commit"]), str(rec["kind"]), [str(x) for x in rec.get("scope", [])])
    repo = where[0] if where else ROOT
    head = _rev("HEAD", repo) if where and not where[1] else (where[1] if where else "")
    base, tmp = ROOT, None
    if snapshot:   # (the copy the auditor read: dev/auditrun.py's)
        files = {rel: _lines(rel, snapshot) for rel in code_files(snapshot)}
        took, _ = scope_lines(a, files, snapshot)
        prints = {p for rel, ns in took.items() for n, p in fingerprints(rel, snapshot).items() if n in ns}
        for f in rec.get("finding", []):
            f["prints"] = spot_prints(f.get("lines", []), snapshot)
        return sorted(prints), bool(rec.get("uncommitted"))
    if where and _rev(a.commit, repo) != head:
        tmp = tempfile.mkdtemp(prefix="audit.")
        archive = subprocess.run(["git", "-C", repo, "archive", a.commit], capture_output=True, timeout=300, check=True)
        subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True, timeout=300)
        base = tmp
    try:
        files = {rel: _lines(rel, base) for rel in code_files(base)}
        took, _ = scope_lines(a, files, base)
        prints: set[str] = set()
        for rel, ns in took.items():
            fp = fingerprints(rel, base)
            prints.update(fp[n] for n in ns if n in fp)
        for f in rec.get("finding", []):
            f["prints"] = spot_prints(f.get("lines", []), base)
        dirty = False
        if tmp is None and where and took:
            got = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--", *sorted(took)], capture_output=True, text=True, timeout=60)
            dirty = bool(got.stdout.strip())
        return sorted(prints), dirty
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def record(path: str, private: str | None = None, force: bool = False, author: bool = False) -> tuple[int, list[str]]:
    """A filled-in template: checked, its findings' introduced_in and missed_by filled in, then its public half to
    security/audits/ and its private half to the private repo's audits/ (save)."""
    try:
        with open(path, "rb") as fh:
            rec = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return 2, [f"{path}: {e}"]
    if author:
        rec["author"] = True
    return save(rec, private, force)


def save(rec: dict, private: str | None = None, force: bool = False, snapshot: str = "",
         prints: list[str] | None = None) -> tuple[int, list[str]]:
    """An audit's record, checked and written as its two halves (record's, and dev/auditrun.py's once an audit is done:
    ``snapshot`` the copy of the code an AI auditor read, for its fingerprints; ``prints`` a person's, taken when they
    started)."""
    root = private_root() if private is None else private
    wrong = []
    for f in rec.get("finding", []):
        try:
            scored(f)
        except ValueError as e:
            wrong.append(f"{f.get('id', 'a finding')}: {e}")
    wrong += problems(rec, "both")
    if not os.path.isdir(os.path.join(root, "audits")):
        wrong.append(f"no private audits repo at {root}")
    where = _repo()
    if where and rec.get("commit") and history(str(rec["commit"]), where[0]) is None:
        wrong.append(f"git doesn't know the commit {rec['commit']}")
    pub = os.path.join(PUBLIC, f"{rec.get('id')}.toml")
    sec = os.path.join(root, "audits", f"{rec.get('id')}.toml")
    if not force and (os.path.exists(pub) or os.path.exists(sec)):
        wrong.append(f"{rec.get('id')} is already recorded (--force to replace it)")
    if wrong:
        return 1, wrong
    audits, _, _ = load(root)
    took, dirty = read_prints(rec, where, snapshot)   # (each finding's prints too)
    if prints is None:
        rec["prints"], rec["uncommitted"] = took, dirty
    else:
        rec["prints"] = sorted(set(prints))
    for f in rec.get("finding", []):
        if where:
            f["introduced_in"] = introduced(str(f["lines"][0]), str(rec["commit"]), where[0])
            f["missed_by"] = missed(rec, f, audits, where[0])
    os.makedirs(PUBLIC, exist_ok=True)
    with open(pub, "w", encoding="utf-8") as fh:
        fh.write("# The public half of a security audit (dev/audit.py record). Who did it, and what it found, are private.\n")
        fh.write(dump(rec, PUBLIC_KEYS))
    with open(sec, "w", encoding="utf-8") as fh:
        fh.write(dump(rec, PRIVATE_KEYS, "finding"))
    return 0, [f"wrote {os.path.relpath(pub, ROOT)}", f"wrote {sec}"]


# ---------------------------------------------------------------- the commands
def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    now = time.time()
    if args[:1] == ["record"]:
        if len(args) < 2:
            print("usage: dev/audit.py record FILE [--force] [--author]")
            return 2
        rc, said = record(args[1], force="--force" in args, author="--author" in args)
        for line in said:
            print(f"  {line}")
        return rc
    if args[:1] in (["run"], ["gui"]):
        from dev import auditgui, auditrun  # (the runner, and the page: only for these)
        return auditrun.main(args[1:]) if args[0] == "run" else auditgui.main(args[1:])
    if args[:1] == ["start"]:
        try:
            print(prompt([a for a in args[1:] if not a.startswith("-")]))
        except OSError as e:
            print(f"  the standard prompt is in the private audits repo: {e}")
            return 2
        return 0
    audits, have_private, bad = load()
    rule = rules() if have_private else {}
    sensitive = list(rule.get("sensitive", {}).get("paths", []))
    if args[:1] == ["next"]:
        m = measure(audits)
        rows = next_batches(m, sensitive, now)
        if not rows:
            print("  every line of the app code has been seen by an audit")
            return 0
        for rel, n, when in rows:
            mark = " (sensitive)" if _sensitive(rel, sensitive) else ""
            print(f"  {rel}{mark}: {counted(n, 'line')}, waiting since {datetime.date.fromtimestamp(when).isoformat()}")
        print(f"\n  to start a partial audit of the first batch: python3 dev/audit.py start {' '.join(r for r, *_ in rows[:5])}")
        return 0
    release = "--release" in args or os.environ.get("TNSYNC_RELEASE") == "1"
    for line in bad:
        print(f"  {line}")
    m = measure(audits)
    for note in m.notes:
        print(f"  ({note})")
    outcomes = [coverage_check(m, audits, now, sensitive), config_check(have_private, rule, release)]
    if have_private:
        outcomes.append(findings_check(audits))
        tag, _ = _rc(_repo())
        if release or tag:
            outcomes.append(release_check(m, audits, have_private, rule.get("release", {}), now, release))
    if release:
        for o in outcomes:
            if o.tool not in ("Release rules", "Audit config"):
                o.failed = False   # (at release, the release rules are what hold it up)
    rc = checks.run([Check(o.tool, (lambda o=o: o)) for o in outcomes], bar="audit")
    return 2 if bad and rc == 0 else rc


if __name__ == "__main__":
    sys.exit(main())

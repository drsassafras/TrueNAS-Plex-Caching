#!/usr/bin/env python3
"""Security audits, line by line (tnsync's Security Audit block): which app code an audit has seen, what's stale or never
audited, and the release rules.

    python3 dev/audit.py                 # the checks (tnsync: TN_AUDIT=python3 dev/audit.py)
    python3 dev/audit.py --release       # the release rules too (tnsync --release: their failure is red)
    python3 dev/audit.py next            # what most needs an audit, grouped into batches by file
    python3 dev/audit.py start [PATH…]   # the standard prompt, filled in (no paths: a full audit; paths: a partial)
    python3 dev/audit.py record FILE     # a filled-in templates/audit.toml, checked, split and saved

Each audit has two halves with the same id (``YYYY-MM-DD-N``, never naming an auditor):

  - public, security/audits/<id>.toml here: the day, the dev commit audited, full or partial, and a partial's scope;
  - private, audits/<id>.toml in the private audits repo (dev/audit.toml's ``private``): the auditor (``fable-5.1``,
    ``opus-5.5``, ``human:name``), its thoroughness (1 the most thorough … 5), and what it found.

With the public half alone (GitHub, anyone's clone) this says what's covered, full or partial, and what's stale; with the
private repo checked out it also holds the release rules (its rules.toml) and lists the open findings.

A line counts as seen by an audit when it's in the audit's scope and hasn't changed since the audit's commit: git blame
gives the commit that last changed it, and that commit is the audit's or one before it. Nobody marks lines by hand.

Through dev/checks.py, as every check says it. Exit 0 every rule met, 1 something to audit (tnsync's block orange; with
--release, a release rule not met: red), 2 the records couldn't be read.
"""

import ast
import datetime
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys
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
ID = re.compile(r"^\d{4}-\d{2}-\d{2}-\d+$")
FINDING_ID = re.compile(r"^F-\d{4}-\d{3,}$")
TOP = 10   # files named on the board when lines wait for an audit


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

    @property
    def auditor(self) -> str:
        return str((self.private or {}).get("auditor", ""))

    @property
    def thoroughness(self) -> int:
        return int((self.private or {}).get("thoroughness", 5))

    @property
    def findings(self) -> list[dict]:
        return list((self.private or {}).get("finding", []))


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
        audits.append(Audit(aid, _date(rec["date"]), str(rec["commit"]), rec["kind"], [str(s) for s in rec.get("scope", [])], priv))
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
    (``opus``), a family at a version or later within it (``opus>=5.5``), or an exact auditor (``opus-5.5``,
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
def code_files() -> list[str]:
    cfg = config()
    out: set[str] = set()
    for pattern in cfg.get("code", []):
        out.update(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, pattern), recursive=True) if os.path.isfile(p))
    for pattern in cfg.get("exclude", []):
        out -= {os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, pattern), recursive=True)}
    return sorted(out)


def _lines(rel: str) -> set[int]:
    """The lines that count: every one with something on it."""
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
        return {n for n, text in enumerate(fh, 1) if text.strip()}


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


def function_lines(rel: str, name: str) -> set[int] | None:
    """A Python function's lines (``name`` dotted through its classes: ``Server.login``), or None when there's no such."""
    if not rel.endswith(".py"):
        return None
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
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


def scope_lines(a: Audit, files: dict[str, set[int]]) -> tuple[dict[str, set[int]], list[str]]:
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
        got = function_lines(rel, func)
        if got is None:
            missing.append(entry)
            continue
        out[rel] = out.get(rel, set()) | (got & files[rel])
    return out, missing


@dataclass
class Line:
    sha: str | None
    when: float | None
    seen: list[Audit] = field(default_factory=list)


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
    if where is None:
        return m
    for a in audits:
        before = history(a.commit, where[0])
        if before is None:
            notes.append(f"audit {a.id}: git doesn't know its commit {a.commit[:12]} here")
            continue
        took, missing = scope_lines(a, files)
        notes += [f"audit {a.id}: its scope {e} names nothing in the app code now" for e in missing]
        for rel, ns in took.items():
            for n in ns:
                ln = m.lines[rel].get(n)
                if ln and ln.sha and ln.sha in before:
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
    return Outcome("Audit coverage", "", found, facts=facts)


def _sensitive(rel: str, paths: list[str]) -> bool:
    return any(rel == p or rel.startswith(p.rstrip("/") + "/") or fnmatch.fnmatch(rel, p) for p in paths)


def _spot(spot: str) -> tuple[str, int]:
    rel, _, nums = str(spot).rpartition(":")
    return rel, int(nums.split("-")[0]) if nums.split("-")[0].isdigit() else 0


def findings_check(audits: list[Audit]) -> Outcome:
    """The open findings (the private half), each on its line: a warning, as they're being worked on."""
    found = []
    for a in audits:
        for f in a.findings:
            if f.get("status") == "open":
                rel, n = _spot((f.get("lines") or ["?:0"])[0])
                found.append(Finding("Audit", rel, n, 0, "warning", f"{f.get('id')} {f.get('severity')}: {f.get('title')}", "finding"))
    return Outcome("Findings", "", found, facts=[f"{counted(len(found), 'open finding')}" if found else "no open findings"])


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
    ok = lambda a: fresh(a) and any(accepts(e, a.auditor, a.thoroughness) for e in accept)   # noqa: E731
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
        found.append(Finding("Audit", "git tags", 0, 0, sev, f"{tag} has soaked {(now - made) / DAY:.1f} of {soak:g} days", "release"))
    block = rule.get("block_open", "high")
    for a in audits:
        for f in a.findings:
            rel, n = _spot((f.get("lines") or ["?:0"])[0])
            if f.get("status") == "open" and SEVERITIES.index(f.get("severity", "info")) >= SEVERITIES.index(block):
                found.append(Finding("Audit", rel, n, 0, sev, f"{f.get('id')} is open at {f.get('severity')}", "release"))
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


def prompt(paths: list[str], private: str | None = None) -> str:
    root = private_root() if private is None else private
    with open(os.path.join(root, "PROMPT.md"), encoding="utf-8") as fh:
        text = fh.read().split("\n---\n", 1)[-1].strip()
    commit = _git("rev-parse", "HEAD") or "?"
    main = _git("rev-parse", "origin/main") or _git("rev-parse", "main") or "?"
    kind = "partial" if paths else "full"
    text = text.replace("<commit>", commit).replace("<main_commit>", main).replace("<full | partial>", kind)
    text = text.replace("<all app code | the list below>", "the list below" if paths else "all app code")
    return text.replace("<scope list, for a partial audit>", "\n".join(f"- `{p}`" for p in paths))


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


def dump(rec: dict, keys: list[str], tables: str = "") -> str:
    out = [f"{k} = {_toml_value(rec[k])}" for k in keys if k in rec and rec[k] not in ("", None, [])]
    for t in rec.get(tables, []) if tables else []:
        out += ["", f"[[{tables}]]"] + [f"{k} = {_toml_value(v)}" for k, v in t.items() if v not in ("", None, [])]
    return "\n".join(out) + "\n"


PUBLIC_KEYS = ["id", "date", "commit", "kind", "scope"]
PRIVATE_KEYS = ["id", "auditor", "thoroughness", "mode", "main_commit", "prompt", "notes"]


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


def record(path: str, private: str | None = None, force: bool = False) -> tuple[int, list[str]]:
    """A filled-in template: checked, its findings' introduced_in and missed_by filled in, then its public half to
    security/audits/ and its private half to the private repo's audits/."""
    root = private_root() if private is None else private
    try:
        with open(path, "rb") as fh:
            rec = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return 2, [f"{path}: {e}"]
    wrong = problems(rec, "both")
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
            print("usage: dev/audit.py record FILE [--force]")
            return 2
        rc, said = record(args[1], force="--force" in args)
        for line in said:
            print(f"  {line}")
        return rc
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

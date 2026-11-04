#!/usr/bin/env python3
"""Security audits, from start to end: what the audits page's Start audit (dev/auditgui.py), and dev/audit.py's ``run``,
do. An audit is done by a person (the default), or by an AI auditor installed on this computer (a plugin: dev/auditor_*.py,
each offered only when it's found). Each one is kept in the private audits repo (runs/<id>/), however it went:

    run.json     what it was asked (kind, target, scope, auditor, thoroughness), where it got to, and what came of it;
                 a person's findings as they're written
    prints.json  the fingerprints of the code as it was when the audit started: what it reads
    prompt.md    an AI auditor's: the standard prompt as it was sent (the private PROMPT.md, filled in)
    output.jsonl an AI auditor's: what it said as it went
    result.md    an AI auditor's: its answer, as it gave it

A person's audit is open until they finish it: they read the scope, write what they find, and finish; it's recorded with the
fingerprints of the code as it was when they started (and says so if it has changed since). "Mark as audited" records a
review already done, in one go. An AI auditor works in a copy of the code as it is now (uncommitted changes too), with
main's beside it, and can only read; its answer is checked and recorded. Either way, the record is dev/audit.py's (both
halves; who and how thoroughly in the private one). Nothing is committed: the new files wait in both repos for you.

    python3 dev/audit.py run --target uncommitted --auditor <id> --thoroughness 3   # an AI auditor's (the page lists them)
"""

import datetime
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import asdict, dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dev import audit  # noqa: E402
from dev.progress import counted  # noqa: E402

# how closely a person read it: their thoroughness, 1 the most thorough
PEOPLE = {1: "every line traced", 2: "line by line", 3: "read closely", 4: "read through", 5: "skimmed"}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._'-]{0,39}$")
ENDED = ("done", "failed", "cancelled", "interrupted")


def runs_root(private: str | None = None) -> str:
    return os.path.join(audit.private_root() if private is None else private, "runs")


def person(name: str) -> str:
    """A person as an auditor: ``human:<name>`` (lower case, a dash for each space)."""
    name = name.strip()
    if not NAME.match(name):
        raise ValueError("a name: letters, digits, spaces and . _ ' -, up to 40")
    return "human:" + re.sub(r"\s+", "-", name.lower())


# ---------------------------------------------------------------- AI auditors: plugins
class Auditor:
    """An AI auditor that can be installed here (a plugin). ``found`` says whether it is (and why not); ``auditors`` the
    auditor ids it runs (``family-version``); ``efforts`` its thoroughness, 1 the most thorough; ``run`` does one, in
    ``code`` (a copy of the code as it is now), ``main`` beside it, logging to ``folder``, and gives its answer."""

    id = ""
    label = ""
    efforts: dict[int, str] = {}

    def found(self, private: str | None = None) -> str:
        return "not installed"

    def auditors(self, private: str | None = None) -> list[str]:
        return []

    def owns(self, auditor: str, private: str | None = None) -> bool:
        return auditor in self.auditors(private)

    def run(self, r: Run, code: str, main: str, prompt: str, folder: str, started, pushed: str = "") -> tuple[int, str, bool]:
        raise NotImplementedError

    def events(self, folder: str, start: int) -> tuple[list[dict], int]:
        return [], start

    def settings(self, private: str | None = None) -> dict:
        """Its own settings: dev/audit.toml's [auditors.<id>], and the private rules.toml's over them."""
        return {**audit.config().get("auditors", {}).get(self.id, {}), **audit.rules(private).get("auditors", {}).get(self.id, {})}


_PLUGINS: list[Auditor] = []


def plugins() -> list[Auditor]:
    """Every AI auditor this knows how to run, found or not (each plugin its own file, dev/auditor_*.py)."""
    if not _PLUGINS:
        import importlib
        for path in sorted(glob.glob(os.path.join(ROOT, "dev", "auditor_*.py"))):
            mod = importlib.import_module("dev." + os.path.basename(path)[:-3])
            _PLUGINS.extend(cls() for cls in getattr(mod, "PLUGINS", []))
    return _PLUGINS


def plugin_for(auditor: str, private: str | None = None) -> Auditor | None:
    return next((p for p in plugins() if p.owns(auditor, private)), None)


@dataclass
class Run:
    id: str
    target: str
    kind: str
    scope: list[str]
    auditor: str
    thoroughness: int
    plugin: str = ""   # (an AI auditor's plugin; "" a person)
    commit: str = ""
    main_commit: str = ""
    uncommitted: bool = False
    lines: int = 0
    state: str = "preparing"   # a person's: open, then done or cancelled; an AI auditor's: preparing, running, then done,
    started: float = 0.0       # failed, cancelled or interrupted
    ended: float = 0.0
    pid: int = 0
    audit_id: str = ""
    findings: int = 0
    error: str = ""
    author: bool = False
    draft: list[dict] = field(default_factory=list)   # (a person's findings, as they're written)
    recheck: list[str] = field(default_factory=list)   # (the open findings it re-checks: their ids)
    rechecked: list[str] = field(default_factory=list)   # (what came of each)
    said: list[str] = field(default_factory=list)   # (record's lines: what it wrote)

    def folder(self, private: str | None = None) -> str:
        return os.path.join(runs_root(private), self.id)

    def write(self, private: str | None = None) -> None:
        os.makedirs(self.folder(private), exist_ok=True)
        tmp = os.path.join(self.folder(private), "run.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, os.path.join(self.folder(private), "run.json"))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def load_run(rid: str, private: str | None = None) -> Run | None:
    try:
        with open(os.path.join(runs_root(private), os.path.basename(rid), "run.json"), encoding="utf-8") as fh:
            got = json.load(fh)
        r = Run(**{k: v for k, v in got.items() if k in Run.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None
    if r.plugin and r.state not in ENDED and not (r.pid and _alive(r.pid)) and (r.state != "preparing" or time.time() - r.started > 600):
        r.state, r.error = "interrupted", r.error or "nothing was running it any more"
    return r


def load_runs(private: str | None = None) -> list[Run]:
    """Every run, newest first. An AI auditor's that says it's running with nothing running it (the page was closed, the
    computer slept) is interrupted; a person's stays open until they finish it."""
    root = runs_root(private)
    got = [load_run(rid, private) for rid in sorted(os.listdir(root), reverse=True)] if os.path.isdir(root) else []
    return [r for r in got if r is not None]


def events(rid: str, private: str | None = None, start: int = 0) -> tuple[list[dict], int]:
    """What a run's AI auditor has done so far, from ``start`` (a byte offset)."""
    r = load_run(rid, private)
    p = next((x for x in plugins() if r and x.id == r.plugin), None)
    return p.events(r.folder(private), start) if p and r else ([], start)


# ---------------------------------------------------------------- findings
def answer(text: str) -> tuple[list[dict], str]:
    """The findings in an AI auditor's answer (PROMPT.md's TOML, in a ```toml block or bare), or what's wrong with it."""
    blocks = re.findall(r"```(?:toml)?\s*\n(.*?)```", text, re.S)
    body = blocks[-1] if blocks else text
    try:
        got = tomllib.loads(body)
    except tomllib.TOMLDecodeError as e:
        if not body.strip() or (re.search(r"\bno findings\b", text, re.I) and "[[finding]]" not in text):
            return [], ""
        return [], f"its answer isn't the TOML the prompt asks for: {e}"
    found = got.get("finding", [])
    if not isinstance(found, list):
        return [], "its answer's finding isn't a list"
    return [f for f in found if isinstance(f, dict)], ""


def rechecks(text: str) -> list[dict]:
    """An AI auditor's answers on the open findings it re-checked (``[[recheck]]`` blocks)."""
    return audit.answers(text, "recheck")


def recheck_prompt(private: str | None, ids: list[str], pushed: str, main: str) -> str:
    """What an auditor re-checking the open findings is asked, beside the standard prompt."""
    audits = audit.load(private)[0]
    items = [(a, f) for a in audits for f in a.findings if f.get("id") in ids]
    out = ["## Re-check the open findings", "",
           "Besides the audit above, check whether each finding below is still there: in your working folder (the code as "
           "it is now, uncommitted changes too), " + (f"in {pushed} (the code as pushed to this branch), " if pushed else "")
           + f"and in {main} (main). Their lines may have moved: look for the problem, not the line numbers. A finding "
           "whose problem is gone in one place but not another: say so for each. Where a finding names the tests that guard "
           "it, read them in tests/ and say whether they'd fail if the problem came back.", "",
           "Answer each with a `[[recheck]]` block, in the same TOML as any new `[[finding]]`:", "", "```toml", "[[recheck]]",
           'id = "F-YYYY-NNN"', 'working = "fixed | present"',
           'pushed = "fixed | present"' if pushed else 'pushed = "none"', 'main = "affected | not-affected | fixed"',
           'cvss = "CVSS:3.1/..."   # only for a vulnerability with no CVSS vector yet: score it',
           'tests = ["TestClass.test_name"]   # the tests that would fail if it came back: of those it names, the ones that would, and any other you found',
           'detail = """', "What you looked at, and what you saw.", '"""', "```", ""]
    out.append(audit.brief(items).split("\n", 2)[-1].lstrip() if items else "")
    return "\n".join(out).rstrip()


def tidy(found: list[dict], year: int, first: int, scope: list[str]) -> list[dict]:
    """Findings as records keep them: each its id, open unless said otherwise; what was given that a record can't hold
    kept in its detail, never dropped (a severity or answer for main it doesn't know: the most careful reading)."""
    out = []
    for i, f in enumerate(found):
        notes = []
        sev = str(f.get("severity", "")).strip().lower()
        vector = str(f.get("cvss", "") or "").strip()
        kind = str(f.get("type", "") or "").strip().lower().replace(" ", "-").replace("_", "-")
        kind = "hardening" if kind in ("hardening", "best-practice", "practice") else "vulnerability"
        if kind == "hardening":
            if vector:
                notes.append(f"(the CVSS vector given, for a hardening finding: {vector!r})")
                vector = ""
            if sev not in audit.PRIORITIES:
                notes.append(f"(the priority given: {f.get('severity')!r})")
                sev = "medium"
        if vector:
            try:
                sev = audit.cvss(vector)[1]   # (its CVSS vector says the severity)
            except ValueError as e:
                notes.append(f"(the CVSS vector given: {vector!r}, {e})")
                vector = ""
        if sev not in audit.SEVERITIES:
            notes.append(f"(the severity given: {f.get('severity')!r})")
            sev = "medium"
        main = str(f.get("main", "")).strip().lower()
        if main not in audit.MAIN:
            notes.append(f"(the answer for main given: {f.get('main')!r})")
            main = "affected"
        lines = [str(x).strip() for x in (f.get("lines") or []) if re.match(r"^[^:\s]+:\d+(-\d+)?$", str(x).strip())]
        if len(lines) != len(f.get("lines") or []):
            notes.append(f"(the lines given: {f.get('lines')!r})")
        if not lines:
            lines = [f"{(scope[0].partition(':')[0] if scope else 'app')}:0"]
        status = str(f.get("status", "open")).strip().lower()
        detail = str(f.get("detail", "")).strip() or "(no detail given)"
        out.append({"id": f"F-{year}-{first + i:03d}", "title": str(f.get("title", "")).strip() or "(untitled)",
                    "type": kind, "severity": sev, **({"cvss": vector} if vector else {}), "lines": lines, "detail": "\n".join([detail, *notes]) + "\n",
                    "status": status if status in audit.STATUSES else "open", "main": main, "main_by": "audit"})
    return out


# ---------------------------------------------------------------- the code
def _git(*args: str) -> str:
    got = subprocess.run(["git", "-C", audit.ROOT, *args], capture_output=True, text=True, timeout=60)
    return got.stdout.strip() if got.returncode == 0 else ""


def snapshot(into: str) -> None:
    """The project as it is now, uncommitted changes too (what git ignores left out, and the audit records)."""
    got = subprocess.run(["git", "-C", audit.ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, timeout=120, check=True)
    for rel in got.stdout.decode().split("\0"):
        src = os.path.join(audit.ROOT, rel)
        if not rel or rel.startswith("security/audits/") or not os.path.isfile(src) or os.path.islink(src):
            continue
        os.makedirs(os.path.join(into, os.path.dirname(rel)), exist_ok=True)
        shutil.copy2(src, os.path.join(into, rel))


def extract(commit: str, into: str) -> None:
    """The project as it is at ``commit`` (git archive), its files and folders only: a link it has (one committed to
    ~/.ssh, say) is left out, as snapshot leaves links out, so nothing the auditor reads leads outside its copy."""
    import io
    import tarfile
    got = subprocess.run(["git", "-C", audit.ROOT, "archive", commit], capture_output=True, timeout=300, check=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(got.stdout)) as tar:
            tar.extractall(into, members=[m for m in tar if m.isfile() or m.isdir()], filter="data")
    except tarfile.TarError as e:
        raise OSError(f"git archive {commit} couldn't be unpacked: {e}") from e


def _dirty(kind: str, scope: list[str]) -> bool:
    files = audit.code_files() if kind == "full" else sorted({s.partition(":")[0] for s in scope})
    got = subprocess.run(["git", "-C", audit.ROOT, "status", "--porcelain", "--", *files], capture_output=True, text=True, timeout=60)
    return bool(got.stdout.strip())


class Runner:
    """The audits going now (the page's, or the command's one), so they can be watched, finished and cancelled."""

    def __init__(self, private: str | None = None):
        self.private = private
        self.live: dict[str, subprocess.Popen] = {}
        self.cancelled: set[str] = set()
        self.lock = threading.Lock()

    def root(self) -> str:
        return audit.private_root() if self.private is None else self.private

    def plan(self, target: str, paths: list[str]) -> tuple[str, list[str], int, str]:
        """A target's kind, scope and size now, or why there's nothing to audit."""
        audits = audit.load(self.private)[0]
        kind, scope, why = audit.target_scope(target, paths, audits)
        return kind, scope, (audit.scope_size(kind, scope) if not why else 0), why

    def _new(self, target: str, paths: list[str], auditor: str, thoroughness: int, plugin: str, author: bool) -> Run:
        if thoroughness not in PEOPLE:
            raise ValueError(f"thoroughness {thoroughness!r} isn't 1 to 5")
        if not os.path.isdir(os.path.join(self.root(), "audits")):
            raise ValueError("no private audits repo here: its runs and records go there")
        kind, scope, lines, why = self.plan(target, paths)
        if why:
            raise ValueError(why)
        rid = time.strftime("%Y%m%d-%H%M%S")
        while os.path.exists(os.path.join(runs_root(self.private), rid)):
            rid = rid[:15] + f"-{int(rid[16:] or 1) + 1}"
        audits = audit.load(self.private)[0]
        recheck = [str(f.get("id")) for a in audits for f in a.findings if f.get("status") == "open"] if target in ("open", "open-changed") else []
        return Run(rid, target, kind, scope, auditor, thoroughness, plugin=plugin, commit=_git("rev-parse", "HEAD"),
                   main_commit=audit.main_commit(), uncommitted=_dirty(kind, scope), lines=lines, started=time.time(), author=author,
                   recheck=recheck)

    def start(self, target: str, paths: list[str], auditor: str, thoroughness: int, author: bool = False,
              wait: bool = False) -> Run:
        """An audit, started: a person's (``human:<name>``), open until they finish it; an AI auditor's, run in the
        background (unless ``wait``), saying what went wrong in its own record."""
        if auditor.startswith("human:"):
            r = self._new(target, paths, auditor, thoroughness, "", author)
            r.state = "open"
            self._prints(r)
            r.write(self.private)
            return r
        p = plugin_for(auditor, self.private)
        if p is None:
            raise ValueError(f"no auditor {auditor!r} here: a person (human:name), or one an AI auditor installed here runs")
        why = p.found(self.private)   # (before a run is logged: said on the page, not as a failed run)
        if why:
            raise ValueError(why)
        r = self._new(target, paths, auditor, thoroughness, p.id, author)
        r.write(self.private)
        go = threading.Thread(target=self._run, args=(r, p), daemon=True)
        go.start()
        if wait:
            go.join()
        return r

    def mark(self, target: str, paths: list[str], auditor: str, thoroughness: int, findings: list[dict]) -> Run:
        """A person's review already done, recorded in one go."""
        r = self.start(target, paths, auditor, thoroughness)
        r.draft = list(findings)
        r.write(self.private)
        return self.finish(r.id)

    # ------------------------------------------------------------ a person's audit
    def _prints(self, r: Run) -> None:
        """The fingerprints of what the audit reads, as it is now (kept with the run, for when it's finished)."""
        rec = {"id": r.id, "date": datetime.date.today(), "commit": r.commit, "kind": r.kind, "scope": r.scope}
        prints, _ = audit.read_prints(rec, audit._repo())
        os.makedirs(r.folder(self.private), exist_ok=True)
        with open(os.path.join(r.folder(self.private), "prints.json"), "w", encoding="utf-8") as fh:
            json.dump(prints, fh)

    def _open(self, rid: str) -> Run:
        r = load_run(rid, self.private)
        if r is None or r.plugin or r.state != "open":
            raise ValueError(f"{rid} isn't a person's audit that's open")
        return r

    def write_finding(self, rid: str, finding: dict | None, remove: int | None = None) -> Run:
        """A finding written into a person's open audit (or, ``remove``: the one at that place taken out)."""
        with self.lock:
            r = self._open(rid)
            if remove is not None:
                if not 0 <= remove < len(r.draft):
                    raise ValueError("no such finding")
                r.draft.pop(remove)
            elif finding is not None:
                if not str(finding.get("title", "")).strip():
                    raise ValueError("a finding needs its title")
                r.draft.append({k: finding.get(k) for k in ("title", "type", "severity", "cvss", "lines", "detail", "main", "status") if k in finding})
                if r.draft[-1].get("cvss"):
                    try:
                        audit.scored(r.draft[-1])
                    except ValueError:
                        r.draft.pop()
                        raise
            r.write(self.private)
            return r

    def finish(self, rid: str) -> Run:
        """A person's audit, recorded: its findings, and the fingerprints of the code as it was when they started."""
        with self.lock:
            r = self._open(rid)
            try:
                with open(os.path.join(r.folder(self.private), "prints.json"), encoding="utf-8") as fh:
                    prints = list(json.load(fh))
            except (OSError, ValueError):
                prints = []
            rec = {"id": r.id, "date": datetime.date.today(), "commit": r.commit, "kind": r.kind, "scope": r.scope}
            now, _ = audit.read_prints(rec, audit._repo())
            if sorted(now) != sorted(prints):
                r.said.append("the code changed while it was audited: only the lines as they were when it started count as seen")
            self._record(r, r.draft, prints=prints)
            r.ended = time.time()
            r.write(self.private)
            return r

    def cancel(self, rid: str) -> bool:
        r = load_run(rid, self.private)
        if r is not None and not r.plugin and r.state == "open":
            with self.lock:
                r.state, r.error, r.ended = "cancelled", "cancelled", time.time()
                r.write(self.private)
            return True
        with self.lock:
            proc = self.live.get(rid)
            if proc is None:
                return False
            self.cancelled.add(rid)
        try:
            os.killpg(proc.pid, signal.SIGTERM)   # (its own session: the auditor and whatever it started)
        except OSError:
            proc.terminate()
        return True

    def _rechecked(self, r: Run, answers: list[dict], has_upstream: bool) -> None:
        """An AI auditor's re-check, written into each finding: one fixed in the working copy is closed (the commit that
        fixed it filled in once it's pushed, and until then the Findings tab and the block say it isn't pushed); otherwise
        still open, saying where it's fixed and where it isn't."""
        got = {str(x.get("id")): x for x in answers}
        known = audit.test_names()
        today = datetime.date.today().isoformat()
        for a in audit.load(self.private)[0]:
            if not any(f.get("id") in r.recheck for f in a.findings):
                continue
            path = os.path.join(self.root(), "audits", f"{a.id}.toml")
            with self.lock:
                with open(path, "rb") as fh:
                    rec = tomllib.load(fh)
                for f in rec.get("finding", []):
                    fid = str(f.get("id"))
                    if fid not in r.recheck or f.get("status") != "open":
                        continue
                    x = got.get(fid)
                    if x is None:
                        said = "not answered"
                    else:
                        working = str(x.get("working", "")).strip().lower()
                        pushed = str(x.get("pushed", "")).strip().lower()
                        main = str(x.get("main", "")).strip().lower()
                        if main in audit.MAIN:
                            f["main"], f["main_by"] = main, f"audit {r.audit_id}"
                        vector = str(x.get("cvss", "") or "").strip()
                        if vector and not f.get("cvss") and f.get("type") != "hardening":
                            try:
                                f["severity"], f["cvss"] = audit.cvss(vector)[1], vector   # (scored now, if it wasn't)
                            except ValueError:
                                pass
                        if working == "fixed":   # (closed: where it's fixed is said, and the commit filled in once it's pushed)
                            at, sha = audit.fix_where(f, a)
                            f["status"], f["closed_by"] = "fixed", f"audit {r.audit_id}"
                            if pushed == "fixed" and has_upstream:
                                f["fixed_in"] = sha or _git("rev-parse", "@{upstream}")
                                said = "fixed and pushed"
                            elif not has_upstream and at in ("committed", "pushed"):
                                f["fixed_in"] = sha or _git("rev-parse", "HEAD")
                                said = "fixed, committed (no upstream to push to)"
                            else:
                                f["fixed_in"] = sha if at == "committed" else ""
                                said = ("fixed and committed, not pushed yet" if at == "committed" else
                                        "fixed in the working copy, not pushed yet") + ": closed, push the fix"
                        elif pushed == "fixed":
                            said = "fixed as pushed, but present in the working copy"
                        else:
                            said = "still present"
                        guard, _ = audit.guards(x.get("tests", []), known)
                        if guard:
                            f["tests"] = sorted({*f.get("tests", []), *guard})
                            said += f" (guarded by {', '.join(guard)})"
                        detail = str(x.get("detail", "")).strip()
                        if detail:
                            said += f": {detail[:500]}"
                    f["checks"] = [*list(f.get("checks") or []), f"{today}, audit {r.audit_id}: {said}"]
                    r.rechecked.append(f"{fid}: {said.split(':')[0]}")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(audit.dump(rec, audit.PRIVATE_KEYS, "finding"))

    # ------------------------------------------------------------ an AI auditor's
    def _run(self, r: Run, p: Auditor) -> None:
        work = tempfile.mkdtemp(prefix="audit-run-")
        code, main = os.path.join(work, "code"), os.path.join(work, "main")
        folder = r.folder(self.private)
        try:
            os.makedirs(code)
            os.makedirs(main)
            snapshot(code)
            if r.main_commit:
                extract(r.main_commit, main)
            pushed = ""
            upstream = _git("rev-parse", "@{upstream}")
            if r.recheck and upstream:
                pushed = os.path.join(work, "pushed")
                os.makedirs(pushed)
                extract(upstream, pushed)
            text = audit.prompt(r.scope, self.private, r.commit, r.main_commit or "?")
            if r.recheck:
                text += "\n\n" + recheck_prompt(self.private, r.recheck, pushed, main)
            with open(os.path.join(folder, "prompt.md"), "w", encoding="utf-8") as fh:
                fh.write(text + "\n")

            def started(proc: subprocess.Popen) -> None:
                with self.lock:
                    self.live[r.id] = proc
                r.pid, r.state = proc.pid, "running"
                r.write(self.private)
            rc, result, is_error = p.run(r, code, main, text, folder, started, pushed)
            with self.lock:
                self.live.pop(r.id, None)
                cancelled = r.id in self.cancelled
            if cancelled:
                r.state, r.error = "cancelled", "cancelled"
                return
            with open(os.path.join(folder, "result.md"), "w", encoding="utf-8") as fh:
                fh.write(result + "\n")
            if rc != 0 or is_error:
                r.state, r.error = "failed", f"{p.label} ended with exit code {rc}" + (f": {result[:300]}" if result else "")
                return
            found, wrong = answer(result)
            if wrong:
                r.state, r.error = "failed", wrong + " (its answer is in result.md)"
                return
            self._record(r, found, snapshot=code)
            if r.state == "done" and r.recheck:
                self._rechecked(r, rechecks(result), bool(upstream))
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            r.state, r.error = "failed", str(e)
        finally:
            r.ended = time.time()
            r.pid = 0
            r.write(self.private)
            shutil.rmtree(work, ignore_errors=True)

    def _record(self, r: Run, found: list[dict], snapshot: str = "", prints: list[str] | None = None) -> None:
        day = datetime.date.today()
        aid, first = audit.next_ids(day, self.root())
        rec = {"id": aid, "date": day, "commit": r.commit, "kind": r.kind, "scope": r.scope, "uncommitted": r.uncommitted,
               "auditor": r.auditor, "thoroughness": r.thoroughness, "author": r.author, "main_commit": r.main_commit or "unknown",
               "run": r.id, "finding": tidy(found, day.year, first, r.scope)}
        if r.plugin:
            at = subprocess.run(["git", "-C", self.root(), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=30)
            rec["prompt"] = f"PROMPT.md@{at.stdout.strip() or 'uncommitted'}"
        rc, said = audit.save(rec, self.private, snapshot=snapshot, prints=prints)
        r.said += said
        if rc != 0:
            r.state, r.error = "failed", "; ".join(said)
            return
        r.state, r.audit_id, r.findings = "done", aid, len(rec["finding"])


# ---------------------------------------------------------------- the command
def main(args: list[str]) -> int:
    opts: dict[str, str] = {}
    paths: list[str] = []
    it = iter(args)
    for a in it:
        if a in ("--target", "--auditor", "--thoroughness"):
            opts[a[2:]] = next(it, "")
        elif a == "--author":
            opts["author"] = "1"
        else:
            paths.append(a)
    found = [(p, p.auditors()) for p in plugins() if not p.found()]
    if not opts.get("auditor") or not opts.get("thoroughness", "").isdigit():
        print("usage: dev/audit.py run --target TARGET --auditor ID --thoroughness 1-5 [--author] [PATH…]")
        print("  an AI auditor installed here: " + ("; ".join(f"{p.label} ({', '.join(ids)})" for p, ids in found) or "none found"))
        print("  targets: " + ", ".join(f"{k} ({v.lower()})" for k, v in audit.TARGETS.items()))
        print("  (a person's audit: the audits page, python3 dev/audit.py gui; or record)")
        return 2
    run = Runner()
    target = opts.get("target") or ("chosen" if paths else "uncommitted")
    try:
        if opts["auditor"].startswith("human:"):
            raise ValueError("a person's audit is the audits page's (python3 dev/audit.py gui), or record's")
        kind, scope, lines, why = run.plan(target, paths)
        if why:
            raise ValueError(why)
        print(f"  a {kind} audit of {counted(lines, 'line')}" + (f": {', '.join(scope[:6])}{' …' if len(scope) > 6 else ''}" if scope else ""))
        r = run.start(target, paths, opts["auditor"], int(opts["thoroughness"]), author="author" in opts, wait=True)
    except ValueError as e:
        print(f"  {e}")
        return 2
    except KeyboardInterrupt:
        return 130
    r = load_run(r.id) or r
    print(f"  run {r.id}: {r.state}" + (f", {r.error}" if r.error else ""))
    for line in r.said:
        print(f"  {line}")
    if r.state == "done":
        print(f"  {counted(r.findings, 'finding')} · nothing committed: the new files wait in both repos for you")
    return 0 if r.state == "done" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

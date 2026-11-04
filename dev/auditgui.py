#!/usr/bin/env python3
"""The audits page: security audits without the commands (python3 dev/audit.py gui, or a on tnsync's board).

    Overview     what the Security Audit block says, what waits to be audited before a push, the files waiting for you
                 to commit
    Start audit  full or partial, of what (uncommitted changes, unpushed, since the last audit, this branch against
                 main, everything no audit has seen, or what you choose), by whom, how closely: a person (you, by
                 default), started now or marked as already done; or an AI auditor, when one is installed here
                 (dev/auditrun.py). Who and how closely stay in the private repo
    Runs         every audit started here: a person's open one, its findings written and finished here; an AI
                 auditor's, how it went and what it's doing while it runs
    Findings     every finding: its status and its answer for main, changed here
    Explorer     the app code by how it's been audited, with toggles: full or partial, author audits, auditor,
                 thoroughness, age; and each file line by line
    Rules        the private rules.toml, checked before it's saved

A page on this computer only (127.0.0.1, a port of its own), for as long as it's open, and for the browser that opened the
address it printed only (its one-time code, swapped for a cookie: dev/devpage.py's Session): everything it reads or
changes needs that cookie and the token the page was given, and a page of another site's name pointed here is refused. It writes files (the
audit records, a finding's status, rules.toml) and never commits: what's new is on the Overview, for you to commit.
"""

import datetime
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dev import audit, auditfix, auditrun, devpage  # noqa: E402
from dev.checks import Outcome  # noqa: E402
from dev.progress import counted  # noqa: E402

PAGE = os.path.join(ROOT, "dev", "auditgui.html")
GONE = 30.0   # (seconds without a beat from any tab: the page was closed)


# ---------------------------------------------------------------- what the page shows
class Data:
    """What the page reads and changes: the records (public and private), git, the runner."""

    def __init__(self, private: str | None = None):
        self.private = private
        self.runner = auditrun.Runner(private)
        self.lock = threading.Lock()
        self.confirming: dict = {}   # (Confirm now's run: the fixes, how far it's got, what came of each)
        self._kept: dict[str, tuple] = {}   # (the Overview's and Findings' answers, until the code or the records change)

    def _sig(self) -> tuple:
        """What the Overview and Findings depend on, cheaply: the records' files, the commits, and the files changed
        since (each one's mtime, so an edit to a file already changed counts too)."""
        def stamp(folder: str) -> tuple:
            try:
                return tuple(sorted((n, os.stat(os.path.join(folder, n)).st_mtime_ns) for n in os.listdir(folder)))
            except OSError:
                return ()
        got = subprocess.run(["git", "-C", audit.ROOT, "status", "--porcelain", "-z", "--untracked-files=all"],
                             capture_output=True, text=True, timeout=60)
        dirty = []
        for row in got.stdout.split("\0"):
            rel = row[3:]
            try:
                dirty.append((rel, os.stat(os.path.join(audit.ROOT, rel)).st_mtime_ns))
            except OSError:
                dirty.append((rel, 0))
        heads = subprocess.run(["git", "-C", audit.ROOT, "rev-parse", "HEAD", "@{upstream}"], capture_output=True, text=True, timeout=60)
        return (stamp(os.path.join(self.root(), "audits")), stamp(audit.PUBLIC), stamp(self.root()), heads.stdout, tuple(dirty),
                int(time.time() // 600))

    def _keep(self, name: str, make):
        """``make()``'s answer, made again only once what it depends on has changed (switching tabs: at once)."""
        sig = self._sig()
        with self.lock:
            had = self._kept.get(name)
        if had and had[0] == sig:
            return had[1]
        got = make()
        with self.lock:
            self._kept[name] = (sig, got)
        return got

    def root(self) -> str:
        return audit.private_root() if self.private is None else self.private

    def _status(self, repo: str, *paths: str) -> list[str]:
        got = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=all", "--", *paths],
                             capture_output=True, text=True, timeout=60)
        return [row[3:] for row in got.stdout.splitlines()] if got.returncode == 0 else []

    def overview(self) -> dict:
        return self._keep("overview", self._overview)

    def _overview(self) -> dict:
        audits, have_private, bad = audit.load(self.private)
        rule = audit.rules(self.private) if have_private else {}
        sensitive = list(rule.get("sensitive", {}).get("paths", []))
        m = audit.measure(audits)
        now = time.time()
        outcomes: list[Outcome] = [audit.coverage_check(m, audits, now, sensitive), audit.config_check(have_private, rule, False)]
        if have_private:
            outcomes.append(audit.findings_check(audits))
            outcomes.append(audit.release_check(m, audits, have_private, rule.get("release", {}), now, release=False))
        rows = list(m.all())
        waiting = [(rel, ln) for rel, _, ln in rows if not ln.seen]
        return {
            "facts": [f for o in outcomes[:2] for f in o.said()],
            "checks": [{"tool": o.tool, "facts": o.said(), "bad": o.bad(),
                        "findings": [{"severity": f.severity, "path": f.path, "line": f.line, "message": f.message} for f in (o.findings or [])]}
                       for o in outcomes],
            "lines": len(rows),
            "seen": len(rows) - len(waiting),
            "full": sum(1 for *_, ln in rows if any(a.kind == "full" for a in ln.seen)),
            "push": sum(1 for _, ln in waiting if not ln.sha or ln.pushed is False),
            "audits": len(audits),
            "last": audits[-1].id if audits else "",
            "private": have_private,
            "open": sum(1 for a in audits for f in a.findings if f.get("status") == "open") if have_private else None,
            "scorecard": audit.scorecard(audits) if have_private else [],
            "private_root": self.root(),
            "problems": bad + m.notes,
            "commit": {"app": self._status(audit.ROOT, os.path.relpath(audit.PUBLIC, audit.ROOT)),
                       "private": self._status(self.root()) if have_private else []},
        }

    def options(self) -> dict:
        """What an audit can be: its targets, how closely a person can read, and the AI auditors installed here (only
        those found: one that isn't is never named)."""
        found = []
        for p in auditrun.plugins():
            try:
                if p.found(self.private):
                    continue
                found.append({"id": p.id, "label": p.label, "auditors": p.auditors(self.private), "efforts": p.efforts})
            except (OSError, ValueError):
                continue
        return {"targets": audit.TARGETS, "people": auditrun.PEOPLE, "plugins": found, "files": audit.code_files()}

    def _who(self, q: dict) -> str:
        """The auditor asked for: a person by name (``name``), or an AI auditor's id (``auditor``)."""
        return auditrun.person(str(q.get("name", ""))) if not q.get("auditor") else str(q["auditor"])

    def plan(self, q: dict) -> dict:
        kind, scope, lines, why = self.runner.plan(str(q.get("target", "")), [str(p) for p in q.get("paths", [])])
        text = ""
        if not why:
            try:
                text = audit.prompt(scope, self.private)
            except OSError as e:
                why = f"the standard prompt is in the private audits repo: {e}"
        return {"kind": kind, "scope": scope, "lines": lines, "why": why, "prompt": text}

    def start(self, q: dict) -> dict:
        r = self.runner.start(str(q.get("target", "")), [str(p) for p in q.get("paths", [])], self._who(q),
                              int(q.get("thoroughness", 0) or 0))
        return {"id": r.id}

    def mark(self, q: dict) -> dict:
        found = [f for f in q.get("findings", []) if isinstance(f, dict)]
        r = self.runner.mark(str(q.get("target", "")), [str(p) for p in q.get("paths", [])], self._who(q),
                             int(q.get("thoroughness", 0) or 0), found)
        return {"id": r.id, "state": r.state, "audit_id": r.audit_id, "error": r.error, "said": r.said}

    def draft(self, q: dict) -> dict:
        """A finding written into a person's open audit, or (``remove``) taken out of it."""
        f = q.get("finding")
        remove = q.get("remove")
        r = self.runner.write_finding(str(q.get("id", "")), f if isinstance(f, dict) else None,
                                      int(remove) if remove is not None else None)
        return {"draft": r.draft}

    def finish(self, q: dict) -> dict:
        r = self.runner.finish(str(q.get("id", "")))
        return {"id": r.id, "state": r.state, "audit_id": r.audit_id, "error": r.error, "said": r.said}

    def runs(self) -> list[dict]:
        out = []
        for r in auditrun.load_runs(self.private):
            d = {k: getattr(r, k) for k in ("id", "target", "kind", "scope", "auditor", "thoroughness", "lines", "state",
                                            "started", "ended", "audit_id", "findings", "error", "uncommitted", "commit",
                                            "plugin", "draft", "said")}
            d["target_name"] = audit.TARGETS.get(r.target, r.target)
            d["by"] = next((p.label for p in auditrun.plugins() if p.id == r.plugin), "") if r.plugin else ""
            d["how"] = (auditrun.PEOPLE.get(r.thoroughness, "") if not r.plugin else
                        next((p.efforts.get(r.thoroughness, "") for p in auditrun.plugins() if p.id == r.plugin), ""))
            out.append(d)
        return out

    def run(self, rid: str, start: int) -> dict:
        evs, at = auditrun.events(rid, self.private, start)
        prompt = ""
        if start == 0:
            try:
                with open(os.path.join(auditrun.runs_root(self.private), os.path.basename(rid), "prompt.md"), encoding="utf-8") as fh:
                    prompt = fh.read()
            except OSError:
                pass
        return {"events": evs, "at": at, "prompt": prompt}

    # ------------------------------------------------------------ findings
    def findings(self) -> list[dict]:
        return self._keep("findings", self._findings)

    def _findings(self) -> list[dict]:
        out, cache, known = [], {}, audit.test_names()
        led: dict[str, list[str]] = {}   # (the new cases each fix's test found)
        for a in audit.load(self.private)[0]:
            for f in a.findings:
                if f.get("via"):
                    led.setdefault(str(f["via"]), []).append(str(f.get("id")))
        audits = audit.load(self.private)[0]
        labels, by_id = audit.prompt_labels(audits), {a.id: a for a in audits}
        who = lambda x: {"id": x.id, "auditor": x.auditor, "thoroughness": x.thoroughness, "prompt": audit.prompt_label(x, labels)}  # noqa: E731
        for a in audits:
            for f in a.findings:
                try:
                    score = audit.cvss(str(f["cvss"]))[0] if f.get("cvss") else None
                except ValueError:
                    score = None
                out.append({**{k: v for k, v in f.items() if isinstance(v, (str, int, list)) and k != "prints"}, "audit": a.id,
                            "date": a.date.isoformat(), "auditor": a.auditor, "thoroughness": a.thoroughness, "author": a.author,
                            "score": score, "code": audit.code_state(f, a, cache), "now": audit.locate(f, a, cache),
                            "fix": audit.fix_where(f, a)[0] if f.get("status") == "fixed" else "",
                            "lost": [t for t in f.get("tests", []) if t not in known], "by_audit": audit.by_audit(f),
                            "waiting": auditfix.waiting(f), "led_to": led.get(str(f.get("id")), []),
                            "outside": f.get("status") == "fixed" and bool(f.get("reported")) and not f.get("closed_by"),
                            "prompt": audit.prompt_label(a, labels),
                            "missed": [who(by_id[m]) for m in f.get("missed_by", []) if m in by_id]})
        return sorted(out, key=lambda f: (f.get("status") != "open", -audit.SEVERITIES.index(f.get("severity", "info"))
                                          if f.get("severity") in audit.SEVERITIES else 0, f.get("id", "")))

    def confirm(self, q: dict) -> dict:
        """A person's word that a finding is fixed (Mark as fixed): where the fix is, and its commit, worked out from git."""
        aid, fid = os.path.basename(str(q.get("audit", ""))), str(q.get("id", ""))
        a = next((x for x in audit.load(self.private)[0] if x.id == aid), None)
        f = next((x for x in (a.findings if a else []) if x.get("id") == fid), None)
        if a is None or f is None:
            raise ValueError(f"no finding {fid} in audit {aid}")
        at, sha = audit.fix_where(f, a)
        got = {"audit": aid, "id": fid, "status": "fixed", "fixed_in": sha, "closed_by": "hand", "_check": f"marked fixed by hand ({at})"}
        self.finding(got)
        return {"where": at, "commit": sha}

    def fix_how(self, q: dict) -> dict:
        """What to tell whoever fixes them: one line, the command that has the rest."""
        return {"text": "Fix the security findings: run `python3 dev/audit.py fixes` and do as it says."}

    def stamp(self, q: dict) -> dict:
        """When the private records last changed (the Findings tab reloads when it does: a fixer's report shows at once)."""
        folder = os.path.join(self.root(), "audits")
        try:
            got = max((os.stat(os.path.join(folder, n)).st_mtime_ns for n in os.listdir(folder) if n.endswith(".toml")), default=0)
        except OSError:
            got = 0
        return {"stamp": str(got), "confirming": self.confirming}

    def confirm_fixes(self, q: dict) -> dict:
        """Confirm now: each fix reported and waiting, its tests run (in the background: the page shows how it goes);
        passed, it's closed; failed, it's open again, saying why."""
        with self.lock:
            if self.confirming.get("running"):
                raise ValueError("already confirming")
            ids = [f["id"] for a in audit.load(self.private)[0] for f in a.findings if auditfix.waiting(f)]
            if not ids:
                raise ValueError("no fixes are waiting to be confirmed")
            self.confirming = {"running": True, "ids": ids, "done": [], "started": time.time()}

        def work():
            try:
                for fid in ids:
                    self.confirming["now"] = fid
                    self.confirming["done"] += auditfix.confirm(self.root(), [fid])
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                self.confirming["error"] = str(e)
            finally:
                self.confirming.update(running=False, now="", ended=time.time())
        threading.Thread(target=work, daemon=True).start()
        return {"ids": ids}

    def refresh(self, q: dict) -> dict:
        """Re-check: a fixed finding whose fix has been committed since gets that commit; and each finding's "missed by"
        worked out again (the audits before it that read its lines), for records made before it was."""
        filled = 0
        audits = audit.load(self.private)[0]
        where = audit._repo()
        for a in audits if where else []:
            for f in a.findings:
                when = {"id": "~", "date": f.get("found")} if f.get("found") else {"id": a.id, "date": a.date}
                got = audit.missed(when, f, audits, where[0]) if where else []
                if sorted(got) != sorted(f.get("missed_by", [])):
                    auditfix.edit(self.root(), a.id, lambda rec, fid=f["id"], got=got: next(
                        x for x in rec["finding"] if x.get("id") == fid).update(missed_by=got))
        for a in audit.load(self.private)[0]:
            for f in a.findings:
                if f.get("status") == "fixed" and not f.get("fixed_in"):
                    at, sha = audit.fix_where(f, a)
                    if sha:
                        self.finding({"audit": a.id, "id": f["id"], "fixed_in": sha})
                        filled += 1
        return {"filled": filled}

    def cvss(self, q: dict) -> dict:
        """A CVSS 3.1 vector's score and severity (the page's picker)."""
        score, sev = audit.cvss(str(q.get("vector", "")))
        return {"score": score, "severity": sev}

    def finding(self, q: dict) -> dict:
        """A finding's status, answer for main, the fix's commit and a note, changed in its audit's private half."""
        aid, fid = os.path.basename(str(q.get("audit", ""))), str(q.get("id", ""))
        def change(rec):
            f = next((x for x in rec.get("finding", []) if x.get("id") == fid), None)
            if f is None:
                raise ValueError(f"no finding {fid} in audit {aid}")
            if "main" in q and str(q["main"]).strip() != f.get("main"):
                f["main_by"] = "hand"   # (changed here: no longer the audit's answer)
            for key in ("status", "main", "fixed_in", "main_fix", "note", "cvss", "type", "cve", "closed_by"):
                if key in q:
                    f[key] = str(q[key]).strip()
            if f.get("status") == "open":   # (open again: no longer closed, nor reported fixed)
                f.pop("closed_by", None)
                if q.get("status") == "open":
                    f.pop("reported", None)
            elif "status" in q and "closed_by" not in q:
                f["closed_by"] = "hand"
            if "tests" in q:   # (the tests that guard it: Class.test_name, a list or written with commas)
                got = q["tests"] if isinstance(q["tests"], list) else str(q["tests"]).replace(",", " ").split()
                f["tests"] = [str(t).strip() for t in got if str(t).strip()]
                if not f["tests"]:
                    f.pop("tests")
            if f.get("type") == "hardening":
                f.pop("cvss", None)
                if "priority" in q:
                    f["severity"] = str(q["priority"]).strip()
            for key in ("cvss", "cve", "closed_by"):
                if key in f and not f[key]:
                    f.pop(key)
            if f.get("cve"):
                f["cve"] = str(f["cve"]).upper()
            if q.get("_check"):
                auditfix.note(f, str(q["_check"]))
            audit.scored(f)
        auditfix.edit(self.root(), aid, change)
        return {"ok": True}

    # ------------------------------------------------------------ the explorer
    def _filter(self, q: dict):
        kinds = set(str(q.get("kinds", "full,partial")).split(","))
        author = q.get("author", "1") == "1"
        who = {x for x in str(q.get("auditors", "")).split(",") if x}
        most = int(q.get("thoroughness", 5) or 5)
        age = int(q.get("age", 0) or 0)
        today = datetime.date.today()

        def ok(a: audit.Audit) -> bool:
            return (a.kind in kinds and (author or not a.author) and (not who or a.auditor in who)
                    and (not a.private or a.thoroughness <= most) and (not age or (today - a.date).days <= age))
        return ok

    def explorer(self, q: dict) -> dict:
        audits = audit.load(self.private)[0]
        ok = self._filter(q)
        m = audit.measure(audits)
        files = []
        for rel, lines in m.lines.items():
            seen = [ln for ln in lines.values() if any(ok(a) for a in ln.seen)]
            files.append({"path": rel, "lines": len(lines), "seen": len(seen),
                          "full": sum(1 for ln in seen if any(ok(a) and a.kind == "full" for a in ln.seen)),
                          "push": sum(1 for ln in lines.values() if not any(ok(a) for a in ln.seen) and (not ln.sha or ln.pushed is False))})
        auditors = sorted({a.auditor for a in audits if a.auditor})
        return {"files": files, "auditors": auditors, "audits": len([a for a in audits if ok(a)])}

    def file(self, q: dict) -> dict:
        rel = str(q.get("path", ""))
        if rel not in audit.code_files():
            raise ValueError(f"{rel} isn't app code")
        audits = audit.load(self.private)[0]
        ok = self._filter(q)
        m = audit.measure(audits)
        text = audit._text(rel)
        rows = []
        for n, line in enumerate(text, 1):
            ln = m.lines[rel].get(n)
            rows.append({"n": n, "text": line, "counts": ln is not None,
                         "seen": [{"id": a.id, "kind": a.kind, "date": a.date.isoformat(), "auditor": a.auditor,
                                   "thoroughness": a.thoroughness if a.private else None, "author": a.author}
                                  for a in (ln.seen if ln else []) if ok(a)],
                         "uncommitted": bool(ln and not ln.sha)})
        return {"path": rel, "rows": rows}

    # ------------------------------------------------------------ the rules
    def rules(self) -> dict:
        path = os.path.join(self.root(), "rules.toml")
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
        dev = audit.config().get("dev", {})
        return {"text": text, "path": path, "problems": check_rules(text) if text else ["there's no rules.toml"], "dev": dev}

    def save_rules(self, q: dict) -> dict:
        text = str(q.get("text", ""))
        wrong = check_rules(text)
        if wrong:
            return {"ok": False, "problems": wrong}
        with self.lock, open(os.path.join(self.root(), "rules.toml"), "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        return {"ok": True, "problems": []}


def check_rules(text: str) -> list[str]:
    """What's wrong with a rules.toml, so a typo can't loosen the release rules unnoticed."""
    try:
        rule = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [f"it isn't TOML: {e}"]
    out = []
    rel = rule.get("release", {})
    if not isinstance(rel, dict):
        return ["[release] isn't a table"]
    accept = rel.get("accept", [])
    if not accept:
        out.append("[release] has no accept: no auditor would be required")
    out += [f"accept entry {e!r} can't be read" for e in accept if not audit.valid_accept(str(e))]
    for key in ("max_age_days", "soak_days"):
        v = rel.get(key, 1)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            out.append(f"[release] {key} isn't a number of days")
    if rel.get("block_open", "high") not in audit.SEVERITIES:
        out.append(f"[release] block_open isn't one of {', '.join(audit.SEVERITIES)}")
    paths = rule.get("sensitive", {}).get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        out.append("[sensitive] paths isn't a list of paths")
    return out


# ---------------------------------------------------------------- the server
def serve(data: Data, open_browser: bool = True, port: int = 0, ready=None) -> int:
    token = secrets.token_urlsafe(24)
    session = devpage.Session("audits")
    beats: dict[str, float] = {}
    lock = threading.Lock()
    ended = threading.Event()
    with open(PAGE, encoding="utf-8") as fh:
        page = fh.read().replace("/*THEME*/", devpage.THEME + devpage.BASE, 1)
    routes_get = {"/api/overview": lambda q: data.overview(), "/api/options": lambda q: data.options(),
                  "/api/runs": lambda q: data.runs(), "/api/run": lambda q: data.run(str(q.get("id", "")), int(q.get("at", 0) or 0)),
                  "/api/findings": lambda q: data.findings(), "/api/explorer": data.explorer, "/api/file": data.file,
                  "/api/rules": lambda q: data.rules(), "/api/stamp": data.stamp, "/api/fix-how": data.fix_how}
    routes_post = {"/api/plan": data.plan, "/api/start": data.start, "/api/mark": data.mark, "/api/draft": data.draft,
                   "/api/finish": data.finish, "/api/cancel": lambda q: {"ok": data.runner.cancel(str(q.get("id", "")))},
                   "/api/finding": data.finding, "/api/cvss": data.cvss, "/api/confirm": data.confirm,
                   "/api/refresh": data.refresh, "/api/confirm-fixes": data.confirm_fixes, "/api/rules": data.save_rules}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):   # noqa: A002  (quiet)
            pass

        def _host_ok(self) -> bool:   # (the page's browser: this computer's name, its cookie; dev/devpage.py)
            return session.ok(self, server.server_port)

        def _token_ok(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Audit-Token", ""), token)

        def _send(self, code: int, body: bytes, ctype: str, nonce: str = "") -> None:
            devpage.send(self, code, body, ctype, nonce)

        def _json(self, fn, q) -> None:
            try:
                got = fn(q)
            except (ValueError, OSError, tomllib.TOMLDecodeError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            self._send(200, json.dumps(got, default=str).encode(), "application/json")

        def do_GET(self):
            where, _, query = self.path.partition("?")
            if devpage.entry(self, session, server.server_port, where, query):
                return None
            if not self._host_ok():
                return self._send(403, devpage.REFUSED, "text/plain; charset=utf-8")
            if where == "/":
                nonce = secrets.token_urlsafe(18)   # (this response's: its own script and style run, nothing else)
                return self._send(200, devpage.nonced(page.replace("__TOKEN__", token, 1), nonce).encode(), "text/html; charset=utf-8", nonce)
            if where not in routes_get or not self._token_ok():
                return self._send(404 if where not in routes_get else 403, b"", "text/plain")
            self._json(routes_get[where], {k: v[0] for k, v in urllib.parse.parse_qs(query).items()})

        def do_POST(self):
            where, _, query = self.path.partition("?")
            if where in ("/alive", "/bye"):   # (a tab's beat or goodbye: sendBeacon has no headers, so the token's in the address)
                q = urllib.parse.parse_qs(query)
                tab, t = (q.get("tab") or [""])[0], (q.get("t") or [""])[0]
                if not self._host_ok() or not tab or not secrets.compare_digest(t, token):
                    return self._send(403, b"", "text/plain")
                with lock:
                    if where == "/alive":
                        beats[tab] = time.time()
                    else:
                        beats.pop(tab, None)
                return self._send(204, b"", "text/plain")
            if not self._host_ok() or not self._token_ok() or where not in routes_post:
                return self._send(403, b"", "text/plain")
            try:
                q = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 2_000_000)) or b"{}")
            except ValueError:
                return self._send(400, b"", "text/plain")
            self._json(routes_post[where], q if isinstance(q, dict) else {})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = session.url(server.server_port)
    opened = devpage.show(session, server.server_port, open_browser)   # (its code: in that file only, yours to read)
    print(f"The audits page: {session.address(server.server_port)}  (Ctrl-C to stop; it stops by itself once the page is closed "
          f"and no audit is running). If no browser opened it: open {opened}")

    def watch():
        seen = False
        while not ended.wait(2.0):
            with lock:
                open_ = [t for t, beat in beats.items() if time.time() - beat < GONE]
                seen = seen or bool(beats)
            if seen and not open_ and not data.runner.live:
                print("The audits page was closed.")
                break
        server.shutdown()
    threading.Thread(target=watch, daemon=True).start()
    if ready:
        ready(server, url, token)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ended.set()
        server.server_close()
        session.close()
    live = list(data.runner.live)
    for rid in live:
        data.runner.cancel(rid)
    if live:
        print(f"  {counted(len(live), 'audit')} still running, cancelled: {', '.join(live)}")
    made = data.overview()["commit"]
    if made["app"] or made["private"]:
        print(f"  to commit: {counted(len(made['app']), 'file')} here, {counted(len(made['private']), 'file')} in the private audits repo")
    return 0


def main(args: list[str]) -> int:
    port = next((int(a.split("=", 1)[1]) for a in args if a.startswith("--port=")), 0)
    return serve(Data(), open_browser="--no-browser" not in args, port=port)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

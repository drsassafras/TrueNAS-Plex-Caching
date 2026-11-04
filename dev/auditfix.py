#!/usr/bin/env python3
"""Fixing the findings: what whoever fixes them (a person, or an AI assistant) reads and reports, and the audits page's
Confirm (dev/audit.py fixes, fixed, found).

    dev/audit.py fixes                      the open findings, and how to fix and report them
    dev/audit.py fixed F-YYYY-NNN [--attempted | --not-real] [--test Class.test_name]… [--note TEXT]
                                            a fix done (or tried): its tests and what was done, kept with the finding
    dev/audit.py found --via F-YYYY-NNN --title TEXT --lines path:start-end… (--cvss VECTOR | --hardening --priority P)
                       [--test Class.test_name]… --detail TEXT
                                            another case a fix's test found: a new finding, found by that test

A fixer never closes a finding: what it reports waits on the audits page's Findings ("Fixed, to confirm") until Confirm
runs its tests (and an auditor re-checks it, when there is one) and closes it.
"""

import contextlib
import datetime
import fcntl
import os
import subprocess
import sys
import tomllib
from typing import Any

from dev import audit

HOW = """# Fixing the security findings

These are the project's open security findings. Fix them one at a time, and report each one as you go: the audits page
shows your progress. Keep each fix to what the finding needs, and don't change unrelated code. Keep the tests passing.
Add a test that would have caught it. Where it fits, make the test catch the same kind of problem anywhere in the code,
not just here. If that test finds another case, don't fix it. List it in the report as a new finding. (So the tests still
pass, leave that case out of the test for now, without saying why in the code, and say in the new finding what to put
back once it's fixed.) If a finding turns out not to be real, say why instead of changing the code.

Report each one with a command (it writes to the private security records; never edit them yourself):

    python3 dev/audit.py fixed F-YYYY-NNN --test TestClass.test_name --note "what you changed, and why it fixes it"
    python3 dev/audit.py fixed F-YYYY-NNN --attempted --note "what you tried, and what's left"
    python3 dev/audit.py fixed F-YYYY-NNN --not-real --note "why it isn't real"
    python3 dev/audit.py found --via F-YYYY-NNN --title "what's wrong, in a line" --lines path:start-end \\
        (--cvss CVSS:3.1/... | --hardening --priority low|medium|high) --test TestClass.test_name \\
        --detail "what is wrong, how it could be used, and what to put back in the test once it's fixed"

You can't close a finding, and don't try: the maintainer confirms each fix on the audits page, which runs its tests. When
you've been through them all, tell the maintainer what you fixed, and how many new cases your tests found (run
`python3 dev/audit.py fixes` again: it lists them), and ask whether to fix those too.
"""


@contextlib.contextmanager
def _locked(root: str):
    """The private records, one writer at a time (the audits page, and these commands, can both change them)."""
    fd = os.open(os.path.join(root, "audits"), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def edit(root: str, aid: str, change) -> Any:
    """One audit's private half, read, changed by ``change(rec)`` (what it returns is returned), checked and written."""
    path = os.path.join(root, "audits", f"{os.path.basename(aid)}.toml")
    with _locked(root):
        with open(path, "rb") as fh:
            rec = tomllib.load(fh)
        got = change(rec)
        wrong = audit.problems(rec, "private")
        if wrong:
            raise ValueError("; ".join(wrong))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(audit.dump(rec, audit.PRIVATE_KEYS, "finding"))
    return got


def where_is(root: str, fid: str) -> tuple[audit.Audit, dict]:
    """A finding, and the audit whose record keeps it."""
    for a in audit.load(root)[0]:
        for f in a.findings:
            if f.get("id") == fid:
                return a, f
    raise ValueError(f"no finding {fid}")


def note(f: dict, said: str) -> None:
    f["checks"] = [*list(f.get("checks") or []), f"{datetime.date.today().isoformat()}: {said}"]


def waiting(f: dict) -> bool:
    """Reported fixed by whoever fixed it, and not confirmed yet: the Findings tab's "Fixed, to confirm"."""
    return f.get("status") == "open" and f.get("reported") == "fixed"


def fixes_text(root: str) -> str:
    """What a fixer reads: how to fix and report them, then each open finding not yet reported fixed."""
    items = [(a, f) for a in audit.load(root)[0] for f in a.findings if f.get("status") == "open" and not waiting(f)]
    done = [(a, f) for a in audit.load(root)[0] for f in a.findings if waiting(f)]
    out = [HOW]
    if done:
        out.append(f"Reported fixed, waiting for the maintainer to confirm: {', '.join(str(f['id']) for _, f in done)}.\n")
    new = [f for _, f in items if not audit.by_audit(f)]
    if new:
        out.append(f"New cases a fix's test found: {', '.join(str(f['id']) for f in new)} (ask before fixing these).\n")
    if not items:
        out.append("No open findings to fix.\n")
        return "\n".join(out)
    out.append(audit.brief(items).split("\n", 2)[-1].lstrip())
    return "\n".join(out)


def fixed(root: str, fid: str, status: str = "fixed", tests: list[str] | None = None, said: str = "") -> dict:
    """A fix reported: its status left open (only Confirm closes it), its tests and what was done kept with it."""
    if status not in audit.REPORTS:
        raise ValueError(f"a fix is {', '.join(audit.REPORTS)}, not {status!r}")
    a, f = where_is(root, fid)
    if f.get("status") != "open":
        raise ValueError(f"{fid} is {f.get('status')}, not open")
    guard, lost = audit.guards(tests or [], audit.test_names())
    if lost:
        raise ValueError(f"not tests in tests/: {', '.join(lost)} (name each as TestClass.test_name)")

    def change(rec):
        g = next(x for x in rec["finding"] if x.get("id") == fid)
        if guard:
            g["tests"] = sorted({*g.get("tests", []), *guard})
        g["reported"] = status
        g["reported_on"] = datetime.date.today().isoformat()
        note(g, f"fix reported ({status})" + (f", guarded by {', '.join(guard)}" if guard else ", no test named")
             + (f": {said.strip()[:800]}" if said.strip() else ""))
        return {"id": fid, "reported": status, "tests": g.get("tests", [])}
    return edit(root, a.id, change)


def found(root: str, via: str, x: dict) -> str:
    """Another case a fix's test found: a new open finding, kept in the record of the finding whose fix it was, found by
    that test (never by that record's audit, nor counted for its auditor)."""
    from dev import auditrun  # (its tidy: findings as records keep them)
    a, _ = where_is(root, via)
    guard, lost = audit.guards(x.get("tests", []), audit.test_names())
    if lost:
        raise ValueError(f"not tests in tests/: {', '.join(lost)} (name each as TestClass.test_name)")
    day = datetime.date.today()

    def change(rec):
        _, first = audit.next_ids(day, root)
        f = auditrun.tidy([x], day.year, first, [])[0]
        f.update(source="fix-test", via=via, found=day.isoformat(), main_by="fix report",
                 prints=audit.spot_prints(f["lines"], audit.ROOT))
        where = audit._repo()
        if where:   # (what missed it: the audits that read these lines before a fix's test found them)
            head = audit._rev("HEAD", where[0])
            f["introduced_in"] = audit.introduced(str(f["lines"][0]), head, where[0]) if head else ""
            f["missed_by"] = audit.missed({"id": "~", "date": day}, f, audit.load(root)[0], where[0])
        if guard:
            f["tests"] = guard
        note(f, f"found by a test written for {via}'s fix" + (f" ({', '.join(guard)})" if guard else ""))
        audit.scored(f)
        rec.setdefault("finding", []).append(f)
        return f["id"]
    return edit(root, a.id, change)


def run_tests(names: list[str]) -> tuple[bool, str]:
    """The tests named (Class.test_name), run here: whether they all passed, and what they said."""
    ids = audit.test_ids()
    missing = [n for n in names if n not in ids]
    if missing:
        return False, f"not in tests/ any more: {', '.join(missing)}"
    got = subprocess.run([sys.executable, "-m", "unittest", *[ids[n] for n in names]], cwd=audit.ROOT, capture_output=True,
                         text=True, timeout=1800, stdin=subprocess.DEVNULL)
    tail = (got.stderr or got.stdout).strip().splitlines()[-12:]
    return got.returncode == 0, "\n".join(tail)


def confirm(root: str, fids: list[str], tests=run_tests) -> list[dict]:
    """Confirm: each fix's tests run; passed, the finding is closed (where its fix is, and its commit, from git); failed,
    it's open again (not reported fixed), saying why."""
    out = []
    for fid in fids:
        a, f = where_is(root, fid)
        if not waiting(f):
            out.append({"id": fid, "ok": False, "said": "not waiting to be confirmed"})
            continue
        names = list(f.get("tests", []))
        ok, said = tests(names) if names else (True, "")
        at, sha = audit.fix_where(f, a)

        def change(rec, fid=fid, ok=ok, said=said, at=at, sha=sha, names=names):
            g = next(x for x in rec["finding"] if x.get("id") == fid)
            if ok:
                g.update(status="fixed", closed_by="confirm")
                if sha:
                    g["fixed_in"] = sha
                note(g, "confirmed: " + (f"its tests passed ({', '.join(names)})" if names else "no test guards it") + f"; the fix is {at}")
            else:
                g.pop("reported", None)
                note(g, f"not confirmed, open again: its tests failed: {said[-600:]}")
            return {"id": fid, "ok": ok, "where": at, "said": said if not ok else ""}
        out.append(edit(root, a.id, change))
    return out


def _flag(args: list[str], name: str) -> list[str]:
    got, it = [], iter(args)
    for x in it:
        if x == name:
            got.append(next(it, ""))
        elif x.startswith(name + "="):
            got.append(x.split("=", 1)[1])
    return got


def main(args: list[str]) -> int:
    """dev/audit.py fixes / fixed / found."""
    root = audit.private_root()
    if not os.path.isdir(os.path.join(root, "audits")):
        print(f"  no private audits repo at {root}: the findings are kept there")
        return 2
    try:
        if args[0] == "fixes":
            print(fixes_text(root))
            return 0
        if args[0] == "fixed":
            ids = [x for x in args[1:2] if audit.FINDING_ID.match(x)]
            if not ids:
                print("usage: dev/audit.py fixed F-YYYY-NNN [--attempted | --not-real] [--test TestClass.test_name]… [--note TEXT]")
                return 2
            status = "attempted" if "--attempted" in args else "not-real" if "--not-real" in args else "fixed"
            got = fixed(root, ids[0], status, _flag(args, "--test"), " ".join(_flag(args, "--note")))
            print(f"  {got['id']}: reported {got['reported']}" + (f", guarded by {', '.join(got['tests'])}" if got["tests"] else "")
                  + ". The maintainer confirms it on the audits page.")
            return 0
        if args[0] == "found":
            via = (_flag(args, "--via") or [""])[0]
            x = {"title": " ".join(_flag(args, "--title")), "lines": _flag(args, "--lines"), "tests": _flag(args, "--test"),
                 "detail": "\n".join(_flag(args, "--detail")), "type": "hardening" if "--hardening" in args else "vulnerability"}
            if "--hardening" in args:
                x["severity"] = (_flag(args, "--priority") or ["medium"])[0]
            else:
                x["cvss"] = (_flag(args, "--cvss") or [""])[0]
            if not audit.FINDING_ID.match(via) or not x["title"] or not x["lines"] or not x["detail"]:
                print("usage: dev/audit.py found --via F-YYYY-NNN --title TEXT --lines path:start-end… "
                      "(--cvss VECTOR | --hardening --priority low|medium|high) [--test TestClass.test_name]… --detail TEXT")
                return 2
            if x["type"] == "vulnerability" and not x["cvss"]:
                print("  a vulnerability needs its CVSS 3.1 vector (--cvss), or it's --hardening with a --priority")
                return 2
            fid = found(root, via, x)
            print(f"  {fid}: a new finding, found by a test written for {via}'s fix. Ask the maintainer before fixing it.")
            return 0
    except ValueError as e:
        print(f"  {e}")
        return 2
    return 2

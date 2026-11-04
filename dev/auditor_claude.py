#!/usr/bin/env python3
"""An AI auditor plugin for dev/auditrun.py: Claude Code, when it's installed on this computer (offered only then).

It runs headless with your own sign-in, as the model the auditor names (``opus-5.5`` is claude-opus-5-5) at the effort its
thoroughness is (1 max … 5 low), in a copy of the code as it is now with main's beside it, and can only read: Read, Grep and
Glob, in those two folders and nowhere else (``--restricted``: no shell, no edits, no web; its Read allowed only under
those folders, by path, and refused in this project's own folder, the private repo and, when the copies aren't under it,
your home folder).

Its settings are ``[auditors.claude-code]``, dev/audit.toml's and the private rules.toml's over them: ``models`` (the
auditors it offers), and, from the private rules.toml alone, ``command`` (where it is, when it isn't found by itself:
``claude`` or an absolute path; or PLEXCACHE_AUDIT_CLAUDE). A command is never taken from the committed dev/audit.toml:
a commit could make Start audit run anything.
"""

import glob
import json
import os
import re
import shutil
import subprocess

from dev import audit
from dev.auditrun import Auditor

FAMILIES = ("fable", "opus", "sonnet", "haiku")
AUDITOR = re.compile(r"^(%s)-\d+(\.\d+)*$" % "|".join(FAMILIES))
MODELS = ["fable-5.1", "opus-5.5", "sonnet-5.5", "haiku-5.5"]   # (offered unless its settings say otherwise)
TOOLS = "Read,Grep,Glob"
NOTE = ("You are running headless and can only read files. The code to audit is in your working folder, as it is now "
        "(uncommitted changes included). The code on main is in {main}: read it there to answer each finding's main. "
        "Answer with the TOML only, as the prompt says.")
PUSHED = " The code as pushed to this branch is in {pushed}."
# (where its installers put it: a page started from tnsync's board may not have it on its PATH)
PLACES = ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
          "~/.npm-global/bin/claude", "~/.bun/bin/claude")
# (the Claude desktop app's own copy, one folder per version: when it's the only one on this computer)
BUNDLED = ("~/Library/Application Support/Claude/claude-code/*/claude",
           "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude",
           "~/Library/Application Support/Claude/claude-code/*/bin/claude",
           "/Applications/Claude.app/Contents/Resources/*/claude", "/Applications/Claude.app/Contents/Resources/claude",
           "/Applications/Claude.app/Contents/Helpers/claude")


def _bundled() -> str:
    """The newest of the Claude desktop app's own copies, if it has one."""
    got = [p for pattern in BUNDLED for p in glob.glob(os.path.expanduser(pattern))
           if os.path.isfile(p) and os.access(p, os.X_OK)]
    key = lambda p: [int(n) for n in re.findall(r"\d+", p.split("claude-code/")[-1].split("/")[0])] or [0]  # noqa: E731
    return max(got, key=key) if got else ""


def model(auditor: str) -> str:
    """The model an auditor names: ``opus-5.5`` is claude-opus-5-5."""
    fam, _, ver = auditor.partition("-")
    return f"claude-{fam}-{ver.replace('.', '-')}"


def _from_shell(name: str) -> str:
    """Where your own shell finds a command (its PATH as a terminal has it), when the page's own PATH doesn't."""
    if os.sep in name or not re.match(r"^[\w.-]+$", name):
        return ""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        got = subprocess.run([shell, "-lic", f"command -v {name}"], capture_output=True, text=True, timeout=15,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    path = next((x.strip() for x in reversed(got.stdout.splitlines()) if x.strip().startswith("/")), "")
    return path if path and os.access(path, os.X_OK) else ""


def _rule(path: str) -> str:
    """A Read rule for everything under a folder, by its absolute path (``//`` is from the filesystem's root)."""
    return f"Read(/{path.rstrip(os.sep)}/**)"


def reads(folders: list[str], refused: list[str]) -> tuple[list[str], list[str]]:
    """Its Read rules: allowed under ``folders`` only (each as given and as its real path), never a bare Read, which
    would allow any file; refused under ``refused`` (this project's folder, the private repo) and, when no folder is
    under it, the home folder. Deny rules win over allow rules, and Grep and Glob go by Read's."""
    def both(p: str) -> list[str]:
        p = os.path.abspath(p)
        return list(dict.fromkeys([p, os.path.realpath(p)]))
    for p in folders:
        if any(ch in p for ch in "(),\n") or any(ch.isspace() for ch in p):
            raise ValueError(f"the auditor's folder {p!r} can't be named in a Read rule")
    allowed = [_rule(x) for p in folders for x in both(p)]
    denied = [_rule(x) for p in refused for x in both(p)
              if not any(f == x or f.startswith(x.rstrip(os.sep) + os.sep) for q in folders for f in both(q))]
    home = os.path.realpath(os.path.expanduser("~"))
    if not any(f == home or f.startswith(home + os.sep) for q in folders for f in both(q)):
        denied.append("Read(~/**)")
    return list(dict.fromkeys(allowed)), list(dict.fromkeys(denied))


class ClaudeCode(Auditor):
    id = "claude-code"
    label = "Claude Code"
    efforts = {1: "max", 2: "xhigh", 3: "high", 4: "medium", 5: "low"}

    def __init__(self) -> None:
        self._found: dict[str, list[str]] = {}

    def given(self, private: str | None = None) -> tuple[str, str]:
        """The command it's told to run, and what's wrong with it: PLEXCACHE_AUDIT_CLAUDE (your own environment), else the
        private rules.toml's (``claude`` or an absolute path), never dev/audit.toml's (the repo's: a commit could set it)."""
        if os.environ.get("PLEXCACHE_AUDIT_CLAUDE"):
            return os.environ["PLEXCACHE_AUDIT_CLAUDE"], ""
        given = str(audit.rules(private).get("auditors", {}).get(self.id, {}).get("command", "claude")).strip() or "claude"
        first = os.path.expanduser(given.split()[0])
        if first != "claude" and not os.path.isabs(first):
            return "", (f"its command in the private rules.toml, {given!r}, isn't claude or an absolute path: set "
                        "[auditors.claude-code] command to where it is (`which claude` in a terminal says it)")
        return given, ""

    def command(self, private: str | None = None) -> list[str]:
        """Its command, found on the PATH, where its installers put it, or your shell's PATH ([] when it isn't)."""
        given, wrong = self.given(private)
        if wrong:
            return []
        if given in self._found:
            return self._found[given]
        words = given.split()
        first = os.path.expanduser(words[0]) if words else "claude"
        found = shutil.which(first) or next((os.path.expanduser(p) for p in PLACES if os.path.basename(first) == "claude"
                                             and os.access(os.path.expanduser(p), os.X_OK)), "") or _from_shell(first) \
            or (_bundled() if os.path.basename(first) == "claude" else "")
        self._found[given] = [found, *words[1:]] if found else []
        return self._found[given]

    def found(self, private: str | None = None) -> str:
        if self.command(private):
            return ""
        wrong = self.given(private)[1]
        if wrong:
            return wrong
        return ("not found on this computer (nor the Claude app's own copy): if it's installed, set its full path as [auditors.claude-code] command in the "
                "private rules.toml, or PLEXCACHE_AUDIT_CLAUDE (`which claude` in a terminal says it)")

    def auditors(self, private: str | None = None) -> list[str]:
        got = self.settings(private).get("models", MODELS)
        return [str(m) for m in got if AUDITOR.match(str(m))] if isinstance(got, list) else list(MODELS)

    def owns(self, auditor: str, private: str | None = None) -> bool:
        return bool(AUDITOR.match(auditor))

    def run(self, r, code: str, main: str, prompt: str, folder: str, started, pushed: str = "") -> tuple[int, str, bool]:
        cmd = self.command()
        if not cmd:
            raise ValueError(self.found())
        allowed, denied = reads([code, main, *([pushed] if pushed else [])], [audit.ROOT, audit.private_root()])
        cmd = [*cmd, "-p", "--model", model(r.auditor), "--effort", self.efforts[r.thoroughness],
               "--tools", TOOLS, "--allowedTools", *allowed, "--disallowedTools", *denied, "--permission-mode", "dontAsk", "--restricted",
               "--strict-mcp-config", "--no-session-persistence", "--output-format", "stream-json", "--verbose",
               "--add-dir", main, *(["--add-dir", pushed] if pushed else []),
               "--append-system-prompt", NOTE.format(main=main) + (PUSHED.format(pushed=pushed) if pushed else "")]
        with open(os.path.join(folder, "output.jsonl"), "wb") as out:
            proc = subprocess.Popen(cmd, cwd=code, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            started(proc)
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode())
            proc.stdin.close()
            rc = proc.wait()
        result, is_error = self._result(folder)
        return rc, result, is_error

    def _result(self, folder: str) -> tuple[str, bool]:
        """Its answer: the stream's result, or failing that, the last thing it said."""
        last = ""
        try:
            with open(os.path.join(folder, "output.jsonl"), encoding="utf-8", errors="replace") as fh:
                for row in fh:
                    try:
                        ev = json.loads(row)
                    except ValueError:
                        last = last or row.strip()
                        continue
                    if ev.get("type") == "result":
                        return str(ev.get("result") or ""), bool(ev.get("is_error"))
                    if ev.get("type") == "assistant":
                        for part in (ev.get("message") or {}).get("content") or []:
                            if part.get("type") == "text":
                                last = part.get("text", "")
        except OSError:
            pass
        return last, True

    def events(self, folder: str, start: int) -> tuple[list[dict], int]:
        """What it has done so far, from ``start`` (a byte offset): what it said, and each file it read or searched."""
        try:
            with open(os.path.join(folder, "output.jsonl"), "rb") as fh:
                fh.seek(start)
                data = fh.read()
        except OSError:
            return [], start
        out = []
        end = data.rfind(b"\n") + 1   # (whole lines only: the rest next time)
        for row in data[:end].decode("utf-8", "replace").splitlines():
            try:
                ev = json.loads(row)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for part in (ev.get("message") or {}).get("content") or []:
                    if part.get("type") == "text" and part.get("text", "").strip():
                        out.append({"kind": "text", "text": part["text"].strip()})
                    elif part.get("type") == "tool_use":
                        got = part.get("input") or {}
                        what = got.get("file_path") or got.get("pattern") or got.get("path") or ""
                        out.append({"kind": "tool", "tool": part.get("name", ""), "text": str(what)})
            elif ev.get("type") == "result":
                out.append({"kind": "result", "text": "finished" if not ev.get("is_error") else "ended with an error"})
        return out, start + end


PLUGINS = [ClaudeCode]   # (what dev/auditrun.py finds here)

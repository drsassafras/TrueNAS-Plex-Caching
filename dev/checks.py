"""What every check tool shares (dev/typecheck.py's Python Validation, dev/buildcheck.py's Build Validation): each tool
fetched at its version in dev/tools.toml, what it finds said one way, the board's bar and the result line.

    from dev import checks
    exe, why = checks.command("ruff")          # the command at its pin (fetched the first time), or None and why
    sys.exit(checks.run([Check("Ruff", ruff), Check("pyright", pyright)], bar="types"))

A finding is said as ``  path:line:col - severity: message (Tool CODE)``, `` [*]`` after it when the tool's own --fix
makes it; on GitHub it's annotated on its line too (an error, a warning or a notice). Then the result line every dev
tool says (dev/progress.py: "0 errors · 0 warnings · 0 Ruff findings"): each tool's own facts, in the order the checks
are given. On tnsync's board (PLEXCACHE_PROGRESS=feed) the bar is the files checked (what each check says it reads:
``files``, from project_files; a file counted once every check that reads it is done), each check named as it ends.

The exit code: 0 clean, 1 something found, 2 a tool couldn't run (not installed, no network for its download…).
"""

import errno
import hashlib
import io
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dev.progress import Progress, counted, feeding, result, said  # noqa: E402 — the dev tools' bar and result line

TOOLS = os.path.join(ROOT, "dev", "tools.toml")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "plex-cache", "tools")
ON_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"
TIMEOUT = 300   # a download or an install


def tools() -> dict[str, dict]:
    """dev/tools.toml: each check tool by its key."""
    with open(TOOLS, "rb") as fh:
        return tomllib.load(fh)


def version(key: str) -> str:
    return str(tools()[key]["version"])


def platform() -> str:
    """This computer as dev/tools.toml's ``assets`` and ``sha256`` name it: darwin-arm64, linux-x86_64, linux-arm64…"""
    machine = _platform.machine().lower()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    return f"{sys.platform}-{machine}"


def _whole(tmp: str, final: str, exe: str) -> None:
    """``tmp`` made ``final``, whole or not at all (a run stopped halfway leaves nothing to trust)."""
    try:
        os.rename(tmp, final)
    except OSError:
        if not os.path.exists(exe):   # (another run got there first: its copy will do)
            raise


def _scratch(final: str) -> str:
    """A folder of this fetch's own beside ``final`` (on its disk, so _whole's rename is whole): not the process's id alone,
    which two threads of one run share (actionlint fetching ShellCheck as ShellCheck's own check does)."""
    os.makedirs(CACHE, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"{os.path.basename(final)}.", dir=CACHE)


_FETCHING: dict[str, threading.Lock] = {}   # one lock per tool: a run's checks wait on its one fetch, then use it
_FETCHING_LOCK = threading.Lock()


def _one_fetch(key: str) -> threading.Lock:
    with _FETCHING_LOCK:
        return _FETCHING.setdefault(key, threading.Lock())


_BAR: list[Progress] = []   # the bar run() draws, while it runs: a tool fetched the first time says so on it


def _fetching(tool: dict) -> None:
    """A tool fetched (or installed) the first time, its run waiting on that: said on the bar, so a slow first run says why."""
    for bar in _BAR:
        bar.show(f"fetching {tool['name']} {tool['version']} (the first run only)", ticking=True)


def _pip(key: str, tool: dict) -> tuple[list[str] | None, str]:
    venv = os.path.join(CACHE, f"{key}-{tool['version']}")
    exe = os.path.join(venv, "bin", tool.get("binary") or tool["package"])
    if os.path.exists(exe):
        return [exe], ""
    tmp = ""
    _fetching(tool)
    try:
        tmp = _scratch(venv)
        subprocess.run([sys.executable, "-m", "venv", tmp], check=True, capture_output=True, text=True, timeout=TIMEOUT)
        subprocess.run([os.path.join(tmp, "bin", "python"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                        f"{tool['package']}=={tool['version']}"], check=True, capture_output=True, text=True, timeout=TIMEOUT)
        _whole(tmp, venv, exe)
    except subprocess.CalledProcessError as e:
        lines = (e.stderr or e.stdout or "").strip().splitlines()
        return None, f"{tool['name']} {tool['version']} couldn't be installed: {lines[-1] if lines else e}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"{tool['name']} {tool['version']} couldn't be installed: {e}"
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return [exe], ""


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:   # (dev/tools.toml's own release URLs)
        return r.read()


def unpack(data: bytes, inside: str) -> bytes:
    """The file ``inside`` an archive (.tar.xz, .tar.gz, .zip), or the download itself when ``inside`` is empty."""
    if not inside:
        return data
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.read(inside)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        got = t.extractfile(inside)
        if got is None:
            raise OSError(f"no {inside} in the archive")
        return got.read()


def asset_url(tool: dict, plat: str, version: str | None = None) -> str:
    v = version or str(tool["version"])
    return tool["url"].format(version=v, asset=tool["assets"][plat])


def _release(key: str, tool: dict) -> tuple[list[str] | None, str]:
    plat = platform()
    if plat not in tool.get("assets", {}):
        return None, f"{tool['name']} has no release for {plat} in dev/tools.toml"
    folder = os.path.join(CACHE, f"{key}-{tool['version']}-{plat}")
    inside = str(tool.get("binary", "")).format(version=tool["version"])
    exe = os.path.join(folder, os.path.basename(inside) or key)
    if os.path.exists(exe):
        return [exe], ""
    tmp = ""
    _fetching(tool)
    try:
        data = fetch(asset_url(tool, plat))
        got = hashlib.sha256(data).hexdigest()
        want = tool.get("sha256", {}).get(plat, "")
        if got != want:
            return None, f"{tool['name']} {tool['version']}'s download for {plat} isn't the one dev/tools.toml names (sha256 {got[:12]}…)"
        tmp = _scratch(folder)
        path = os.path.join(tmp, os.path.basename(exe))
        with open(path, "wb") as fh:
            fh.write(unpack(data, inside))
        os.chmod(path, 0o700)   # (a command, its owner's: the cache is theirs)
        _whole(tmp, folder, exe)
    except (OSError, KeyError, tarfile.TarError, zipfile.BadZipFile) as e:
        return None, f"{tool['name']} {tool['version']} couldn't be fetched: {e}"
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return [exe], ""


def command(key: str) -> tuple[list[str] | None, str]:
    """The command that runs tool ``key`` at its version (fetched or installed the first time), or None and why not."""
    tool = tools()[key]
    if tool["get"] == "npx":
        if not shutil.which("npx"):
            return None, f"{tool['name']} needs npx (Node), which isn't installed"
        return ["npx", "--yes", f"{tool['package']}@{tool['version']}"], ""
    with _one_fetch(key):   # (two checks wanting one tool: the second waits for the first's fetch, then finds it there)
        if tool["get"] == "pip":
            return _pip(key, tool)
        return _release(key, tool)


def call(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """subprocess.run for a check tool's command. A tool just fetched can be 'Text file busy' (ETXTBSY) for a moment: a
    subprocess another check's thread started while the download was being written holds the file open for writing until
    it execs its own program, and Linux won't run a file that's open for writing. That's waited out, not said as a failure."""
    for wait in (0.05, 0.1, 0.2, 0.4, 0.8, 1.6):
        try:
            return subprocess.run(cmd, **kw)
        except OSError as e:
            if e.errno != errno.ETXTBSY:
                raise
            time.sleep(wait)
    return subprocess.run(cmd, **kw)


# ---------------------------------------------------------------- what they find, said one way
SEVERITIES = ("error", "warning", "information")


@dataclass
class Finding:
    tool: str          # who found it, as the result line names it ("Ruff")
    path: str          # the file, from the project's root
    line: int
    col: int
    severity: str      # error, warning or information
    message: str
    code: str = ""     # the tool's own name for the rule (F401, SC2034, reportArgumentType)
    fixable: bool = False   # the tool's --fix makes it


def rel(path: str) -> str:
    """A path from the project's root (as the tools are run there), however the tool said it."""
    r = os.path.relpath(path, ROOT) if os.path.isabs(path) else path
    return r if not r.startswith("..") else path


def say(f: Finding) -> str:
    code = f" ({f.tool} {f.code})" if f.code else f" ({f.tool})"
    return f"  {rel(f.path)}:{f.line}:{f.col} - {f.severity}: {f.message}{code}{' [*]' if f.fixable else ''}"


def annotate(f: Finding) -> str:
    """The line GitHub reads as an annotation on the finding's line."""
    kind = {"error": "error", "warning": "warning"}.get(f.severity, "notice")
    msg = f.message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    title = f"{f.tool} {f.code}".strip()
    return f"::{kind} file={rel(f.path)},line={f.line},col={f.col},title={title}::{msg}"


@dataclass
class Outcome:
    tool: str
    version: str
    findings: list[Finding] | None = None   # None: it couldn't say (``why``)
    why: str = ""
    facts: list[str] = field(default_factory=list)   # its part of the result line; empty: "N <tool> findings"
    failed: bool | None = None   # whether what it found fails the check; None: anything found does

    def said(self) -> list[str]:
        if self.findings is None:
            return [f"{self.tool} didn't run"]
        return self.facts or [counted(len(self.findings), f"{self.tool} finding")]

    def bad(self) -> bool:
        if self.findings is None:
            return False
        return bool(self.findings) if self.failed is None else self.failed


@dataclass
class Check:
    name: str
    run: Callable[[], Outcome]
    files: Callable[[], list[str]] | None = None   # what it reads (project_files): the bar counts them; None: the checks


def project_files(*patterns: str, under: tuple[str, ...] = ("",), skip: tuple[str, ...] = ()) -> list[str]:
    """The project's files (from its root) whose names match one of ``patterns`` (fnmatch: "*.py", "Dockerfile"), in the
    folders ``under`` ("" the whole project), none in ``skip``: what git keeps or would (not what it ignores), or, with
    no git, every file but .git, node_modules and __pycache__."""
    import fnmatch
    try:
        got = subprocess.run(["git", "-C", ROOT, "ls-files", "-z", "-co", "--exclude-standard"], capture_output=True, timeout=30)
        listed = [p for p in got.stdout.decode("utf-8", "replace").split("\0") if p] if got.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        listed = None
    if listed is None:
        listed = []
        for dirpath, dirs, names in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
            listed += [os.path.relpath(os.path.join(dirpath, n), ROOT).replace(os.sep, "/") for n in names]

    def within(path: str, folders: tuple[str, ...]) -> bool:
        return any(not f or path == f or path.startswith(f.rstrip("/") + "/") for f in folders)
    return sorted(p for p in listed if within(p, under) and not within(p, skip) and os.path.isfile(os.path.join(ROOT, p))
                  and any(fnmatch.fnmatch(os.path.basename(p), pat) for pat in patterns))


def run(checks: list[Check], bar: str = "main", stream=None) -> int:
    """Every check at once; what each found, in their order, then the result line. The exit code (above)."""
    stream = stream or sys.stdout
    # what the bar counts: the files the checks read (each once), when every check says which; else the checks
    read = [c.files() if c.files else None for c in checks] if feeding() else []
    readers: dict[str, set[int]] = {}
    if read and all(r is not None for r in read):
        for i, r in enumerate(read):
            for f in r or []:
                readers.setdefault(f, set()).add(i)
    progress = None
    if feeding():
        progress = Progress(len(readers), unit="files checked", bar=bar, stream=stream) if readers else \
            Progress(len(checks), unit="checks done", bar=bar, stream=stream)
        progress.workers = len(checks)
        progress.show(", ".join(c.name for c in checks), ticking=True)
        _BAR.append(progress)
    outcomes: list[Outcome | None] = [None] * len(checks)
    lock = threading.Lock()

    def one(i: int, c: Check):
        try:
            got = c.run()
        except Exception as e:   # noqa: BLE001 — one tool falling over is said as that, the others still report
            got = Outcome(c.name, "", None, f"{c.name} fell over: {e!r}")
        with lock:
            outcomes[i] = got
            bad = sum(1 for o in outcomes if o and (o.bad() or o.findings is None))
            name = f"{c.name} {got.version}".strip()
            if progress and readers:   # (a file is checked once every check that reads it is done)
                done = sum(1 for who in readers.values() if all(outcomes[j] is not None for j in who))
                progress.bad = bad
                progress.at(done, len(readers), name)
            elif progress:
                progress.draw(name, bad=bad)

    threads = [threading.Thread(target=one, args=(i, c), name=c.name) for i, c in enumerate(checks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    done = [o for o in outcomes if o is not None]
    for o in done:
        if o.findings is None:
            stream.write(f"  {o.why}\n")
            continue
        for f in o.findings:
            stream.write(say(f) + "\n")
            if ON_GITHUB:
                stream.write(annotate(f) + "\n")
    if progress:
        _BAR.remove(progress)
        progress.finished = " · ".join(f"{o.tool} {o.version}".strip() for o in done)
        progress.clear()
    line = result(*(fact for o in done for fact in o.said()))
    stream.write(line + "\n")
    stream.flush()
    said(line, stream)
    if any(o.findings is None for o in done):
        return 2
    return 1 if any(o.bad() for o in done) else 0


def repo_at() -> tuple[str, str] | None:
    """Where git knows the project's lines, for a check that asks git about them (dev/audit.py's blame): this folder when
    it's a checkout (its working tree, so uncommitted lines count as not committed yet), else the repository tnsync copied
    it from, at the commit it copied (TNSYNC_REPO, TNSYNC_COMMIT); None: no history here."""
    try:
        got = subprocess.run(["git", "-C", ROOT, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        if got.returncode == 0 and os.path.realpath(got.stdout.strip()) == os.path.realpath(ROOT):
            return ROOT, ""
    except (OSError, subprocess.TimeoutExpired):
        return None
    repo, commit = os.environ.get("TNSYNC_REPO", ""), os.environ.get("TNSYNC_COMMIT", "")
    return (repo, commit) if repo and commit else None


def json_out(cmd: list[str], **kw) -> tuple[object | None, subprocess.CompletedProcess]:
    """A tool's JSON output (its findings), or None when it didn't give any."""
    got = call(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT, **kw)
    try:
        return json.loads(got.stdout), got
    except ValueError:
        return None, got


def last_line(got: subprocess.CompletedProcess) -> str:
    lines = (got.stderr or got.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {got.returncode}"

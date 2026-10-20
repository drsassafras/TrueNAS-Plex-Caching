"""The external programs the optimizer uses: MKVToolNix (mkvmerge, mkvextract) to
remux and extract, ffmpeg/ffprobe to check the result plays the same; oxipng and jpegtran to
compress cover art losslessly (optional: without them images are kept as they are).

They're found on PATH, or in the folders listed in PLEXCACHE_TOOLS_PATH (e.g. the macOS app
bundle, /Applications/MKVToolNix.app/Contents/MacOS, when testing outside the container).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading

NAMES = ("mkvmerge", "mkvextract", "ffmpeg", "ffprobe")
OPTIONAL = ("oxipng", "jpegtran")


_PROGRESS_LINE = re.compile(r"^[a-z_0-9]+=")  # ffmpeg -progress: "out_time_us=…", "progress=end"


class ToolError(RuntimeError):
    pass


class VerifyError(ToolError):
    """A new file failed a check (its streams, attachments, layout or tags not what they should be):
    it's thrown away and the original kept."""


def find(name: str) -> str | None:
    extra = [p for p in os.environ.get("PLEXCACHE_TOOLS_PATH", "").split(os.pathsep) if p]
    for folder in extra:
        cand = os.path.join(folder, name)
        if os.access(cand, os.X_OK):
            return cand
    return shutil.which(name)


def missing() -> list[str]:
    return [n for n in NAMES if not find(n)]


def version(name: str) -> str | None:
    exe = find(name)
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "-version" if name.startswith(("ff", "jpeg")) else "--version"],
                           capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        out = p.stdout or p.stderr  # jpegtran prints it on stderr
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.splitlines() or [""])[0].strip() or None


def run(name: str, *args: str, timeout: float | None = None, binary: bool = False, lines=None):
    """Run a tool; its stdout (text unless ``binary``). Raises ToolError with its message on failure.
    mkvmerge and mkvpropedit exit with 1 for warnings, which still means success. ``lines(text)``:
    each line of the tool's progress as it comes (mkvmerge's on stdout, ffmpeg's on stderr)."""
    exe = find(name)
    if not exe:
        raise ToolError(f"{name} isn't installed")
    if lines is None:
        try:
            p = subprocess.run([exe, *args], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ToolError(f"{name} took too long") from None
        code, out, err = p.returncode, p.stdout, p.stderr
    else:
        code, out, err = _watched([exe, *args], name.startswith("mkv"), lines, timeout)
        if code is None:
            raise ToolError(f"{name} took too long")
    ok = code == 0 or (code == 1 and name.startswith("mkv"))
    if not ok:
        msg = [m.replace("#GUI#error ", "") for m in (err or out).decode("utf-8", "replace").strip().splitlines()
               if not (m.startswith("#GUI#progress") or (lines and _PROGRESS_LINE.match(m)))]  # the error, not progress
        raise ToolError(f"{name} failed: {msg[-1] if msg else code}")
    return out if binary else out.decode("utf-8", "replace")


def _watched(cmd: list[str], on_stdout: bool, lines, timeout: float | None) -> tuple:
    """Run ``cmd``, passing each line of its progress stream to ``lines`` as it comes; (exit code or
    None when it took too long, stdout, stderr). The other stream is read alongside (no deadlock).
    The time limit counts from the start, so a tool that hangs without a word is stopped too."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    watched, other = (p.stdout, p.stderr) if on_stdout else (p.stderr, p.stdout)
    got: dict[str, bytes] = {}
    reader = threading.Thread(target=lambda: got.update(other=other.read()), daemon=True)
    reader.start()
    expired = threading.Event()

    def expire():
        expired.set()
        try:
            p.kill()  # (its output then ends, and so does the reading below)
        except OSError:
            pass
    timer = threading.Timer(timeout, expire) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    seen = []
    for raw in iter(watched.readline, b""):
        seen.append(raw)
        try:
            lines(raw.decode("utf-8", "replace").strip())
        except Exception:  # noqa: BLE001 — progress is only shown: never let it stop the tool
            pass
    p.wait()
    if timer:
        timer.cancel()
    code = None if expired.is_set() else p.returncode
    reader.join()
    p.stdout.close()
    p.stderr.close()
    mine = b"".join(seen)
    return (code, mine, got.get("other", b"")) if on_stdout else (code, got.get("other", b""), mine)

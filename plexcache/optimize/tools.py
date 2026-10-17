"""The external programs the optimizer uses: MKVToolNix (mkvmerge, mkvpropedit, mkvextract) to
remux and edit, ffmpeg/ffprobe to check the result plays the same; oxipng and jpegtran to
compress cover art losslessly (optional: without them images are kept as they are).

They're found on PATH, or in the folders listed in PLEXCACHE_TOOLS_PATH (e.g. the macOS app
bundle, /Applications/MKVToolNix.app/Contents/MacOS, when testing outside the container).
"""

from __future__ import annotations

import os
import shutil
import subprocess

NAMES = ("mkvmerge", "mkvpropedit", "mkvextract", "ffmpeg", "ffprobe")
OPTIONAL = ("oxipng", "jpegtran")


class ToolError(RuntimeError):
    pass


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


def run(name: str, *args: str, timeout: float | None = None, binary: bool = False):
    """Run a tool; its stdout (text unless ``binary``). Raises ToolError with its message on failure.
    mkvmerge and mkvpropedit exit with 1 for warnings, which still means success."""
    exe = find(name)
    if not exe:
        raise ToolError(f"{name} isn't installed")
    try:
        p = subprocess.run([exe, *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"{name} took too long") from None
    ok = p.returncode == 0 or (p.returncode == 1 and name.startswith("mkv"))
    if not ok:
        err = (p.stderr or p.stdout).decode("utf-8", "replace").strip().splitlines()
        raise ToolError(f"{name} failed: {err[-1] if err else p.returncode}")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")

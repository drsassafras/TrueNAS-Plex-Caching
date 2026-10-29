#!/usr/bin/env python3
"""What this installation carries: the versions of everything the app depends on, in one list — the OS, Python and its
zstd, MKVToolNix, ffmpeg, oxipng, jpegtran. The build workflow prints it for each image (its log says what shipped);
inside an image, or on a machine with the tools, by hand:

    python3 dev/versions.py            # one line per dependency
    python3 dev/versions.py --json     # as JSON (for a diff between two builds)
"""

import json
import platform
import re
import shutil
import subprocess
import sys


def _run(*cmd: str) -> str:
    try:
        got = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (got.stdout or got.stderr or "").strip().splitlines()[0] if (got.stdout or got.stderr) else ""


def versions() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            m = re.search(r'^PRETTY_NAME="?([^"\n]+)', fh.read(), re.M)
        out["os"] = m.group(1) if m else platform.platform()
    except OSError:
        out["os"] = platform.platform()
    out["python"] = platform.python_version()
    try:
        from compression import zstd
        out["zstd"] = zstd.zstd_version
    except ImportError:
        out["zstd"] = "missing"
    for name, cmd, pat in (("mkvmerge", ("mkvmerge", "--version"), r"v([\d.]+)"), ("ffmpeg", ("ffmpeg", "-version"), r"version (\S+)"),
                           ("oxipng", ("oxipng", "--version"), r"([\d.]+)"), ("jpegtran", ("jpegtran", "-version"), r"version ([\d.]+)")):
        line = _run(*cmd) if shutil.which(cmd[0]) else ""
        m = re.search(pat, line) if line else None
        out[name] = m.group(1) if m else ("missing" if not line else line)
    node = _run("node", "--version") if shutil.which("node") else ""
    out["node"] = node.lstrip("v") if node else "missing (the page tests skip)"
    return out


def main() -> int:
    got = versions()
    if "--json" in sys.argv:
        print(json.dumps(got, indent=2))
    else:
        for k, v in got.items():
            print(f"{k:10} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

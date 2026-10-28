"""The MKV optimizer (the app's second container). See optimizer.py."""

import re

# The optimizer's own version, written into every file it optimizes (the PLEXCACHE_OPTIMIZER tag).
# Change it only when what the optimizer writes changes. A new minor version (0.1 → 0.2) means
# files done before are done again; a new patch version (0.1.0 → 0.1.1) doesn't. A file tagged by
# a newer optimizer is never touched by an older one — two systems sharing a library (Syncthing)
# can't rewrite a file back and forth.
VERSION = "0.1.2"


def parse(version) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", str(version or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None


def compare(file_version) -> str:
    """How a file's tag compares with this optimizer, by what decides the output (major.minor):
    "none" (no tag, or one we can't read), "older" (to be done again), "same", or "newer" (done
    by a newer optimizer: leave it alone)."""
    theirs, ours = parse(file_version), parse(VERSION)
    if not theirs:
        return "none"
    if theirs[:2] < ours[:2]:
        return "older"
    return "newer" if theirs[:2] > ours[:2] else "same"

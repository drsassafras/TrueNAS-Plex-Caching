"""Plex Cache: keeps Plex media warm in ZFS ARC/L2ARC on TrueNAS."""

from __future__ import annotations

import os

__version__ = "0.16.12"
REPO_URL = "https://github.com/drsassafras/TrueNAS-Plex-Caching"
# Which build this is: "stable" (the main branch, released and tagged) or "dev" (the dev branch, the
# same version number until the next release). Set when the image is built (Dockerfile ARG); the
# commit it was built from too, so a dev build knows whether dev has moved on.
CHANNEL = (os.environ.get("PLEXCACHE_CHANNEL") or "stable").strip().lower()
COMMIT = (os.environ.get("PLEXCACHE_COMMIT") or "").strip()


def version_label(channel: str | None = None) -> str:
    """The version as shown: "0.16.0", or "0.16.0-dev" for a dev build."""
    return __version__ + ("-dev" if (channel or CHANNEL) == "dev" else "")

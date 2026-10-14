"""Is there a newer version? Asks GitHub every few hours (can be switched off in Settings).

Reads the version number from plexcache/__init__.py on the main branch, which is what
the published :latest image is built from. Nothing is sent but an ordinary download.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.request

from . import REPO_URL
from .i18n import M

log = logging.getLogger("plexcache")
SOURCE = REPO_URL.replace("https://github.com/", "https://raw.githubusercontent.com/") + "/main/plexcache/__init__.py"
EVERY = 6 * 3600
RETRY = 3600


def parse_version(text: str) -> str | None:
    m = re.search(r'^__version__\s*=\s*["\']([0-9][0-9A-Za-z.+-]*)["\']', text, re.M)
    return m.group(1) if m else None


def newer(a: str, b: str) -> bool:
    """Is version a newer than b? (0.14.0 > 0.13.2; anything unparseable isn't newer)."""
    def key(v):
        return tuple(int(x) for x in re.findall(r"\d+", v.split("+")[0])[:4])
    try:
        return key(a) > key(b)
    except ValueError:
        return False


class UpdateChecker:
    def __init__(self, app, source: str | None = None):
        self.app = app
        self.source = source or os.environ.get("PLEXCACHE_UPDATE_SOURCE") or SOURCE
        self.latest: str | None = None  # newest version seen, if newer than this one; else this one
        self.checked: float | None = None
        self.error: str | None = None
        self.wake = threading.Event()
        self.first_delay = 30  # let startup finish first

    def start(self):
        threading.Thread(target=self._loop, name="updates", daemon=True).start()

    def _loop(self):
        self.app.stopping.wait(self.first_delay)
        while not self.app.stopping.is_set():
            wait = EVERY
            if self.app.settings.section("general").get("update_check", True):
                wait = EVERY if self.check() else RETRY
            self.wake.wait(wait)
            self.wake.clear()

    def check(self) -> bool:
        try:
            req = urllib.request.Request(self.source, headers={"User-Agent": f"plex-cache/{self.app.version}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read(64 * 1024).decode("utf-8", "replace")
            found = parse_version(text)
            if not found:
                raise ValueError("no version number in the file")
        except (OSError, ValueError) as e:
            self.error = str(getattr(e, "reason", None) or e)
            log.info("update check failed: %s", self.error)
            return False
        before = self.latest
        self.latest = found if newer(found, self.app.version) else self.app.version
        self.checked, self.error = time.time(), None
        if self.latest != self.app.version and before != self.latest:
            self.app.activity.event("update", M("Plex Cache {latest} is available (this is {current})", latest=self.latest, current=self.app.version))
        if getattr(self.app, "ha", None):
            self.app.ha.wake.set()
        return True

    def status(self) -> dict:
        return {"latest": self.latest, "available": bool(self.latest and self.latest != self.app.version),
                "checked": self.checked, "error": self.error, "repo": REPO_URL}

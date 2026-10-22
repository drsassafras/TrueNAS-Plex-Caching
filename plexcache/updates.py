"""Is there a newer version? Asks GitHub every few hours (can be switched off in System), and when
you press Check Now — and a few seconds after the app starts.

Follows the channel this build is from (plexcache.CHANNEL): a stable build reads the version number
from plexcache/__init__.py on the main branch, which is what the published :latest image is built
from; a dev build reads it from the dev branch and, the number being the same between releases,
also asks which commit dev is at, so a newer dev image is noticed. Nothing is sent but ordinary
downloads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request

from . import CHANNEL, COMMIT, REPO_URL, __version__
from .i18n import M

log = logging.getLogger("plexcache")
RAW = REPO_URL.replace("https://github.com/", "https://raw.githubusercontent.com/")
API = REPO_URL.replace("https://github.com/", "https://api.github.com/repos/")
BRANCH = {"stable": "main", "dev": "dev"}
SOURCE = f"{RAW}/main/plexcache/__init__.py"
EVERY = 6 * 3600
RETRY = 3600


def source_for(channel: str) -> str:
    return f"{RAW}/{BRANCH.get(channel, 'main')}/plexcache/__init__.py"


def commit_source_for(channel: str) -> str | None:
    """Where the branch's newest commit is read from — dev builds only (a release has its number)."""
    return f"{API}/commits/{BRANCH['dev']}" if channel == "dev" else None


def parse_version(text: str) -> str | None:
    m = re.search(r'^__version__\s*=\s*["\']([0-9][0-9A-Za-z.+-]*)["\']', text, re.M)
    return m.group(1) if m else None


def newer(a: str, b: str) -> bool:
    """Is version a newer than b? (0.14.0 > 0.13.2; a pre-release comes before its release:
    0.16.0-beta.1 < 0.16.0-beta.2 < 0.16.0; anything unparseable isn't newer)."""
    def key(v):
        core, _, pre = v.split("+")[0].partition("-")
        nums = [int(x) for x in re.findall(r"\d+", core)[:4]]
        nums += [0] * (4 - len(nums))
        # a release sorts after every pre-release of it
        return (tuple(nums), (1,) if not pre else (0, *[int(x) for x in re.findall(r"\d+", pre)]))
    try:
        return key(a) > key(b)
    except ValueError:
        return False


class UpdateChecker:
    def __init__(self, app, source: str | None = None, channel: str | None = None, commit_source: str | None = None, commit: str | None = None):
        self.app = app
        self.channel = channel or CHANNEL
        self.source = source or os.environ.get("PLEXCACHE_UPDATE_SOURCE") or source_for(self.channel)
        # the commit this build is from, and where the branch's newest is read: only when neither the
        # source nor the commit source was given by hand (tests), and for dev builds
        self.commit = COMMIT if commit is None else commit
        self.commit_source = commit_source if commit_source is not None else (commit_source_for(self.channel) if source is None and not os.environ.get("PLEXCACHE_UPDATE_SOURCE") else None)
        self.latest: str | None = None  # newest version seen, if newer than this one; else this one
        self.latest_commit: str | None = None  # the branch's newest commit (dev builds)
        self.checked: float | None = None
        self.error: str | None = None
        self.checking = False
        self.updating: float | None = None  # when Update Now was asked (TrueNAS pulls and restarts: this process ends)
        self.wake = threading.Event()
        self.first_delay = 5  # a check soon after every start, once startup has settled

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

    def check_async(self) -> bool:
        """Check Now: a check in the background (one at a time)."""
        if self.checking:
            return False
        self.checking = True

        def work():
            try:
                self.check()
            finally:
                self.checking = False
        threading.Thread(target=work, name="updates-now", daemon=True).start()
        return True

    def _fetch(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": f"plex-cache/{self.app.version}", "Accept": "application/vnd.github+json, text/plain;q=0.9, */*;q=0.8"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read(256 * 1024)

    def check(self) -> bool:
        try:
            found = parse_version(self._fetch(self.source).decode("utf-8", "replace"))
            if not found:
                raise ValueError("no version number in the file")
            head = None
            if self.commit_source:
                data = json.loads(self._fetch(self.commit_source).decode("utf-8", "replace"))
                head = str((data or {}).get("sha") or "")[:40] or None
        except (OSError, ValueError) as e:
            self.error = str(getattr(e, "reason", None) or e)
            log.info("update check failed: %s", self.error)
            return False
        before = (self.latest, self.latest_commit)
        mine = __version__
        self.latest = found if newer(found, mine) else mine
        self.latest_commit = head
        self.checked, self.error = time.time(), None
        if self.available() and before != (self.latest, self.latest_commit):
            if self.latest != mine:
                self.app.activity.event("update", M("Plex Cache {latest} is available (this is {current})", latest=self.latest, current=self.app.version))
            else:
                self.app.activity.event("update", M("A newer dev build is available: dev is at {head} (this is {mine})", head=head[:7], mine=(self.commit or "?")[:7]))
        if getattr(self.app, "ha", None):
            self.app.ha.wake.set()
        return True

    def available(self) -> bool:
        """A newer version — or, on the dev channel, a newer commit of dev than this build's."""
        if self.latest and self.latest != __version__:
            return True
        return bool(self.latest_commit and self.commit and not self.latest_commit.startswith(self.commit) and not self.commit.startswith(self.latest_commit))

    def build_url(self) -> str:
        """This build's page on GitHub: a stable build's release, a dev build's commit — else the repository."""
        if self.channel == "dev":
            return f"{REPO_URL}/commit/{self.commit}" if self.commit else f"{REPO_URL}/tree/dev"
        return f"{REPO_URL}/releases/tag/v{__version__}"

    def status(self) -> dict:
        return {"latest": self.latest, "available": self.available(), "checked": self.checked, "error": self.error, "repo": REPO_URL, "url": self.build_url(),
                "channel": self.channel, "branch": BRANCH.get(self.channel, "main"), "commit": (self.commit or "")[:7], "latest_commit": (self.latest_commit or "")[:7],
                "checking": self.checking, "updating": self.updating, "version": self.app.version}

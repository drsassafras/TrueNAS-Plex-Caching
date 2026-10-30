"""Files set aside by the tools (the library's .plexcache-trash), and when they go for good.

A file set aside — a duplicate version, an old path after a move — is purged only once Plex here no
longer lists it (a scan), and, with a mirror, once its Plex no longer lists it
either (the synced folder carries the move there as a deletion), then after the hold
(general.backup_days: the one length every backup is kept). A file that never meets that —
the mirror keeps listing it, say — is held, and said so: Health lists it, and the tool's page offers
Purge Now, Put Back and Keep (held, no longer followed). Kept in trash.json.
"""

import os
import threading
import time

from .i18n import M
from .plex import PlexError

STORE = "trash.json"
CHECK_EVERY = 300
STUCK_AFTER = 14 * 86400  # held this long without meeting the criteria: said on Health


class Trash:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.items: list[dict] = [e for e in (app._load_json(STORE) or {}).get("items") or [] if isinstance(e, dict)]
        self.busy = False

    def save(self) -> None:
        with self.lock:
            data = {"items": [dict(e) for e in self.items]}
        self.app._save_json(STORE, data)

    def add(self, library: str, path: str, plex_path: str, trashed: str, why: str, title: str = "") -> dict:
        e = {"library": str(library), "path": path, "plex_path": plex_path, "trashed": trashed, "why": why, "title": title or os.path.basename(path),
             "at": time.time(), "state": "waiting", "checked": 0.0, "here": False, "there": None}
        with self.lock:
            self.items = [x for x in self.items if x["path"] != path] + [e]
        self.save()
        return e

    def hold_days(self) -> int:
        from . import safety
        return safety.life(self.app) // 86400

    def _root(self, library: str, path: str) -> str | None:
        lib = self.app.library_info(library) or {}
        for loc in sorted((loc.rstrip("/") for loc in lib.get("locations") or []), key=len, reverse=True):
            if self.app.map_path(path).startswith(self.app.map_path(loc) + "/"):
                return self.app.map_path(loc)
        return None

    def tick(self) -> None:
        with self.lock:
            if self.busy:
                return
            self.busy = True
        try:
            changed = False
            for e in [x for x in self.items if x["state"] == "waiting"]:
                changed |= self._check(e)
            if changed:
                self.save()
        finally:
            with self.lock:
                self.busy = False

    def _check(self, e: dict) -> bool:
        now = time.time()
        if now - e.get("checked", 0) < CHECK_EVERY:
            return False
        e["checked"] = now
        # here: Plex no longer lists the path
        try:
            listed = any(f.path == e["plex_path"] for f in list(self.app.files))
            e["here"] = not listed
        except Exception:  # noqa: BLE001
            pass
        # there: the mirror's Plex no longer lists it (or there's no mirror for this library)
        other = getattr(self.app, "mirrors", None)
        there = None
        try:
            lib_there = other.mirror_for(str(e["library"])) if other is not None else None
            if other is not None and lib_there is not None:
                idx = other.index(lib_there, fresh=True)
                cands = other.paths_there(e["library"], e["plex_path"], lib_there)
                there = not any(c in idx for c in cands)
        except PlexError as err:
            e["note"] = str(err)
            return True
        e["there"] = there
        e.pop("note", None)
        if e["here"] and there is not False:
            e.setdefault("clear_at", now)
            if now - e["clear_at"] >= self.hold_days() * 86400:
                self._purge(e)
        elif now - e["at"] > STUCK_AFTER:
            e["stuck"] = True
        return True

    def _purge(self, e: dict) -> bool:
        """The file deleted for good through the optimizer service; False when it refused (said on the row, tried again later)."""
        root = self._root(e["library"], e["path"])
        try:
            self.app.optimizing.client.file_action("purge", e["trashed"], root=root or "")
        except Exception as err:  # noqa: BLE001 — said; tried again later
            e["error"] = str(getattr(err, "reason", None) or err)
            return False
        e.update(state="purged", purged=time.time())
        e.pop("error", None)
        self.app.activity.event("tools", M("deleted for good: {file} ({why})", file=os.path.basename(e["path"]), why=e["why"]))
        return True

    def act(self, path: str, action: str) -> None:
        """"purge": now; "restore": the file back where it was; "keep": held, no longer followed."""
        with self.lock:
            e = next((x for x in self.items if x["path"] == path), None)
        if e is None:
            raise ValueError("no such file")
        root = self._root(e["library"], e["path"])
        if action == "purge":
            if not self._purge(e):
                raise ValueError(M("Not deleted: {why}", why=e.get("error") or ""))
        elif action == "restore":
            self.app.optimizing.client.file_action("restore", e["trashed"], root=root or "")
            e.update(state="restored")
            self.app.activity.event("tools", M("put back: {file}", file=os.path.basename(e["path"])))
        elif action == "keep":
            e.update(state="kept")
        else:
            raise ValueError("unknown action")
        self.save()

    def stuck(self) -> list[dict]:
        with self.lock:
            return [dict(e) for e in self.items if e["state"] == "waiting" and e.get("stuck")]

    def snapshot(self) -> dict:
        with self.lock:
            return {"items": sorted((dict(e) for e in self.items if e["state"] in ("waiting",)), key=lambda e: -e["at"]), "hold_days": self.hold_days()}

"""Converted files on their way into Plex: the original is deleted only once Plex has the MKV.

Converting (optimize/convert.py) leaves the original where it was, with ``<name>.mkv`` next to it
(or ``<name> - MKV.mkv``… where that name was taken).
Then, the way that keeps Plex's metadata (edits, watch history, collections): Plex is asked to scan
the folder, and picks the MKV up as a second version of the same item. Once it has — checked here
by asking Plex for the item's files, and on a second Plex server with the same files too (plex2.py)
— the original is deleted and the folder scanned again. Plex keeps the item, now with just the MKV.
The run that converted it took a backup first (tools/convert.py, a ToolJob): while it lasts, Revert
on the run puts the original back and removes the MKV.

If Plex makes the MKV an item of its own instead, nothing is deleted: the page says to merge them in
Plex (which brings the check back on track) or to keep both. Kept in conversions.json.

States: "waiting" (Plex doesn't have the MKV yet), "seen" (it's a version of the same item, and the
original's deletion failed: tried again), "separate" (another item), "deleted", "missing" (the MKV
is gone: the original stays).
"""

from __future__ import annotations

import logging
import os
import threading
import time

from .i18n import M
from .plex import PlexError

log = logging.getLogger(__name__)

STORE = "conversions.json"
CHECK_EVERY = 120  # seconds between asking Plex about a converted file
RESCAN_EVERY = 15 * 60  # asking Plex to scan the folder again, while it doesn't have the MKV
RETRY_DELETE = 3600  # after a deletion that failed
KEEP_DONE = 30 * 86400  # how long a deleted original stays listed


class Conversions:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        saved = app._load_json(STORE) or {}
        self.items: list[dict] = [e for e in saved.get("items") or [] if isinstance(e, dict) and e.get("original")]
        self.busy = False

    def save(self) -> None:
        with self.lock:
            data = {"items": [dict(e) for e in self.items]}
        self.app._save_json(STORE, data)

    def get(self, path: str) -> dict | None:
        """The conversion a file is part of (as its original or its MKV), or None."""
        with self.lock:
            return next((dict(e) for e in self.items if path in (e["original"], e["new"])), None)

    def add(self, library: str, plex_original: str, original: str, new: str, rating_key: str = "", title: str = "") -> None:
        """A file just converted: its path as Plex has it and as this app sees it, and its MKV's."""
        plex_new = os.path.join(os.path.dirname(plex_original), os.path.basename(new))  # (its name: <name>.mkv, or the next free one)
        e = {"original": original, "new": new, "plex_original": plex_original, "plex_new": plex_new, "library": str(library),
             "rating_key": rating_key or "", "title": title or os.path.basename(plex_original),
             "converted": time.time(), "state": "waiting", "checked": 0.0, "scanned": 0.0}
        with self.lock:
            self.items = [x for x in self.items if x["original"] != original] + [e]
        self._scan(e)
        self.save()

    def forget(self, original: str) -> None:
        """A conversion no longer followed (reverted): its entry dropped."""
        with self.lock:
            self.items = [x for x in self.items if x["original"] != original]
        self.save()

    def _scan(self, e: dict) -> None:
        """Ask Plex to scan the file's folder (its libraries may be scanned only by hand)."""
        e["scanned"] = time.time()
        try:
            self.app.plex(timeout=30).scan(e["library"], os.path.dirname(e["plex_original"]))
        except PlexError as err:
            e["note"] = str(err)

    def tick(self) -> None:
        """Check the converted files waiting on Plex, and delete the originals that are due."""
        with self.lock:
            if self.busy:
                return
            self.busy = True
        try:
            changed = False
            for e in [x for x in self.items]:
                changed |= self._check(e)
            with self.lock:
                before = len(self.items)
                self.items = [e for e in self.items if not (e["state"] == "deleted" and time.time() - e.get("deleted", 0) > KEEP_DONE)]
                changed |= len(self.items) != before
            if changed:
                self.save()
        finally:
            with self.lock:
                self.busy = False

    def _where(self, e: dict) -> str:
        """Where Plex has the MKV: "same" (a version of the original's item), "other" (an item of its
        own), or "" (not yet)."""
        files = self.app.plex(timeout=30).item_files(e["rating_key"]) if e.get("rating_key") else None
        if files and e["plex_new"] in files:
            return "same"
        # as the last library scan has them: merged in Plex since (into the MKV's item, say), the
        # original and the MKV share an item again
        known = {f.path: getattr(f, "rating_key", "") for f in list(self.app.files) if f.path in (e["plex_original"], e["plex_new"])}
        new_key = known.get(e["plex_new"])
        if new_key and known.get(e["plex_original"]) == new_key:
            e["rating_key"] = new_key
            return "same"
        if new_key and new_key != e.get("rating_key"):
            return "other"
        return ""

    def _check(self, e: dict) -> bool:
        now, state = time.time(), e["state"]
        if state in ("deleted",):
            return False
        # whether this server has a mirror is a choice to make (the Sync page): until it's made, the
        # original stays — a mirror not watched could lose its item
        sync = getattr(self.app, "sync", None)
        if sync is not None and not sync.decided():
            if e.get("note") != str(M("Waiting: whether there’s a mirrored server isn’t chosen yet (Sync page).")):
                e["note"] = str(M("Waiting: whether there’s a mirrored server isn’t chosen yet (Sync page)."))
                return True
            return False
        if not os.path.exists(e["new"]):
            if state != "missing":
                e["state"] = "missing"
                return True
            return False
        if state in ("waiting", "separate", "missing") and now - e.get("checked", 0) >= CHECK_EVERY:
            e["checked"] = now
            try:
                where = self._where(e)
            except PlexError as err:
                e["note"] = str(err)
                return True
            # the second server, if there's one for this library (plex2.py)
            other = getattr(self.app, "other", None)
            try:
                there = other.where(e, now) if other is not None else None
            except PlexError as err:
                there = ""
                e["note"] = M("{server}: {why}", server=other.settings().get("name") or M("the other server"), why=str(err))
            else:
                e.pop("note", None)
            e["servers"] = {"here": where or "waiting", **({"there": there or "waiting"} if there is not None else {})}
            if where == "other" or there == "other":
                e["state"] = "separate"
            elif where == "same" and there in (None, "same", "absent"):
                e.update(state="seen", seen=now)
                self.app.activity.event("optimizer", M("Plex has the MKV of {title}: the original goes", title=e["title"]))
                self._delete(e)
            else:
                e["state"] = "waiting"
                if where != "same" and now - e.get("scanned", 0) >= RESCAN_EVERY:
                    self._scan(e)
            return True
        if state == "seen" and now - e.get("failed", 0) >= RETRY_DELETE:
            self._delete(e)
            return True
        return False

    def _delete(self, e: dict) -> bool:
        if e["original"] in self.app.monitor.playing_paths():
            return False  # playing: next time
        try:
            self.app.optimizing.client.delete_original(e["original"], e["new"])
        except Exception as err:  # noqa: BLE001 — refused (the MKV isn't there…), or the optimizer is down: said, tried again later
            reason = getattr(err, "reason", None) or str(err)
            try:
                import json
                reason = json.loads(err.read() or b"{}").get("error") or reason  # (an HTTP error's own reason)
            except Exception:  # noqa: BLE001
                pass
            e.update(failed=time.time(), error=str(reason))
            return False
        e.pop("error", None)
        e.update(state="deleted", deleted=time.time())
        self._scan(e)  # so Plex lets go of the original's part
        self.app.activity.event("optimizer", M("deleted the original of {title} ({file}): Plex has its MKV",
                                               title=e["title"], file=os.path.basename(e["original"])))
        return True

    def act(self, original: str, action: str) -> None:
        """"delete": the original now (only once Plex has the MKV on the same item); "keep": keep both
        files, and stop following it; "check": ask Plex again now."""
        with self.lock:
            e = next((x for x in self.items if x["original"] == original), None)
        if e is None:
            raise ValueError("no such converted file")
        if action == "keep":
            with self.lock:
                self.items = [x for x in self.items if x is not e]
        elif action == "delete":
            if e["state"] != "seen":
                raise ValueError(M("Not until Plex has the MKV as a version of the same item."))
            if not self._delete(e):
                raise ValueError(M("Not deleted: {why}", why=e.get("error") or M("it’s playing")))
        elif action == "check":
            e["checked"] = 0.0
            self._check(e)
        else:
            raise ValueError("unknown action")
        self.save()

    def snapshot(self) -> dict:
        with self.lock:
            items = [dict(e) for e in self.items]
        return {"items": sorted(items, key=lambda e: -e.get("converted", 0))}

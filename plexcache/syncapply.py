"""Library Sync, applying: the differences you tick on the Library page's Sync view made true on the
other server — one way, from the source (settings "sync.source") to the other, through its Plex API.
Only with the sync mode "apply", and only after a review (sync.py): never by itself.

What's applied, per item: edited fields (set and locked, as the source has them), edited tag fields
(genres, directors…), the match, posters and backgrounds chosen by hand (the source's image uploaded),
collections, labels, the edition — and, where the source's one item is several there, those merged
into one. Nothing is deleted: a file only on the other server stays, as do items; what the source's
several items are as one item there is left for you (a split needs matching again).

Before the first change, the records it will change there are backed up (plexbackup.py, per the Backup preference),
and each change is logged (sync-log.json) with what the item had before, and can be undone from the page:
the fields set back (and unlocked, if they weren't locked), the match put back, the poster chosen
before chosen again, collections and labels as they were. A merge is undone from the backup: split apart, and each
record given its own metadata back.
"""

from __future__ import annotations

import threading
import time
import uuid

from .i18n import M
from .plex import ART_FIELDS, EDIT_FIELDS, PlexError

STORE = "sync-log.json"
KEEP = 500
TYPE_NO = {"movie": 1, "show": 2, "season": 3, "episode": 4, "artist": 8, "album": 9, "track": 10}
TAG_LOCKS = ("genre", "writer", "director", "producer", "country")  # tag fields an edit locks (collection and label have kinds of their own)


class Applier:
    def __init__(self, app, sync):
        self.app, self.sync = app, sync
        self.lock = threading.Lock()
        self.entries: list[dict] = [e for e in (app._load_json(STORE) or {}).get("entries") or [] if isinstance(e, dict)]
        self.running = False
        self.progress: dict | None = None
        self.error: str | None = None

    def save(self) -> None:
        with self.lock:
            data = {"entries": [dict(e) for e in self.entries[-KEEP:]]}
        self.app._save_json(STORE, data)

    def allowed(self) -> str | None:
        """Why applying can't run now, or None."""
        s = self.sync.settings()
        if s.get("mode") != "apply":
            return M("Applying is off: set Sync to “Compare, and apply what I tick” on the Sync page.")
        if self.sync.decided() != "plex2":
            return M("No mirrored server is watched.")
        if self.sync.review_needed():
            return M("The sync settings changed since they were reviewed: look the differences over and press I’ve Reviewed These first.")
        from . import peers
        return peers.sync_conflict(self.app)

    # ------------------------------------------------------------ the two servers
    def _ends(self, key: str):
        """(src client, dst client, dst section key, dst server name, dst is the mirror) for a library."""
        lib, there = self.sync._pairs()[str(key)]
        src_is_mirror = self.sync.settings().get("source") == "mirror"
        here = self.app.plex(timeout=120)
        self.app.other.run(lambda c: c.identity(), timeout=20)
        mirror = self.app.other.client(timeout=120)
        name_here = getattr(self.app, "plex_name", "") or str(M("This server"))
        name_there = self.app.settings.section("plex2").get("name") or str(M("the mirror"))
        if src_is_mirror:
            return mirror, here, str(key), name_here, False
        return here, mirror, there.key, name_there, True

    # ------------------------------------------------------------ applying
    def apply_async(self, key: str, ids: list[str]) -> bool:
        why = self.allowed()
        if why:
            raise ValueError(why)
        rows = [r for r in ((self.sync.report or {}).get("libraries", {}).get(str(key)) or {}).get("items") or [] if r.get("id") in set(ids)]
        with self.lock:
            if self.running or not rows:
                return False
            self.running, self.error = True, None
            self.progress = {"library": str(key), "done": 0, "total": len(rows), "row": None}
        threading.Thread(target=self._work, args=(str(key), rows), name="sync-apply", daemon=True).start()
        return True

    def _work(self, key: str, rows: list[dict]) -> None:
        from . import plexbackup
        try:
            src_client, dst_client, dst_section, dst_name, dst_is_mirror = self._ends(key)
            dst_keys = [str(k) for r in rows for k in [(r.get("dst") or {}).get("key")] + [it.get("key") for it in r.get("dst_items") or []] if k]
            with plexbackup.session(self.app, M("Library Sync on {server}", server=dst_name), "mirror" if dst_is_mirror else "here", dst_section, client=dst_client, keys=dst_keys) as pb:
                self._rows(key, rows, src_client, dst_client, dst_section, dst_name, dst_is_mirror, pb)
        except Exception as e:  # noqa: BLE001 — said on the page
            self.error = str(e)
        finally:
            with self.lock:
                self.running, self.progress = False, None
            self.app.run_checks_async()

    def _rows(self, key: str, rows: list[dict], src_client, dst_client, dst_section: str, dst_name: str, dst_is_mirror: bool, pb: dict | None) -> None:
        for i, row in enumerate(rows):
            with self.lock:
                self.progress.update(done=i, row=row.get("title"))
            entry = {"id": uuid.uuid4().hex[:12], "at": time.time(), "library": key, "row": row["id"], "title": row.get("title"), "dst_server": dst_name,
                     "dst_mirror": dst_is_mirror, "dst_key": (row.get("dst") or {}).get("key"), "section": dst_section, "type": (row.get("dst") or row.get("src") or {}).get("type", ""),
                     "done": [], "before": {}, "after": {}, "notes": [], "result": "done", "undone": False, "backup": (pb or {}).get("id")}
            try:
                self._apply_row(row, entry, src_client, dst_client, dst_section)
            except (PlexError, ValueError) as e:
                entry.update(result="failed", reason=str(e))
            if not entry["done"] and entry["result"] == "done":
                entry["result"] = "skipped"
            with self.lock:
                self.entries.append(entry)
                if entry["result"] == "done" and entry["done"]:  # (applied: off the report until the next compare)
                    lib = (self.sync.report or {}).get("libraries", {}).get(key) or {}
                    lib["items"] = [r for r in lib.get("items") or [] if r["id"] != row["id"]]
                    for k in entry["done"]:
                        lib.setdefault("counts", {})[k] = max(0, lib["counts"].get(k, 0) - 1)
                    self.sync.report["totals"] = {k: sum(v["counts"].get(k, 0) for v in self.sync.report["libraries"].values()) for k in self.sync.report["totals"]}
            self.save()
            self.sync.save()
            self.app.activity.event("sync", M("{title}: {what} applied on {server}", title=row.get("title"), what=", ".join(entry["done"]) or entry["result"], server=dst_name))

    def _apply_row(self, row: dict, e: dict, src_client, dst_client, section: str) -> None:
        src, dst = row.get("src"), row.get("dst")
        shape = row.get("shape")
        if shape in ("missing", "extra"):
            e["notes"].append(M("Nothing to apply: a file not listed needs a scan there; items only there stay."))
            return
        if shape == "merge":
            e["notes"].append(M("Several items here are one there: splitting it needs matching the parts again — done by hand in Plex."))
            return
        if shape == "regroup":
            e["notes"].append(M("The files are grouped another way on each side: left alone — look at both sides."))
            return
        type_no = TYPE_NO.get((dst or src or {}).get("type", ""), 1)
        if shape == "split":  # one item here, several there: merged into the one holding the source's first file
            items = row.get("dst_items") or []
            first = (src.get("files") or [None])[0]
            keep = next((it for it in items if first in (it.get("files") or [])), items[0])
            others = [it["key"] for it in items if it["key"] != keep["key"]]
            dst_client.merge(keep["key"], others)
            e["dst_key"] = keep["key"]
            e["before"]["merged"] = others
            e["done"].append("grouping")
            e["notes"].append(M("Merged {n} items into {title}: undone from the backup, split apart with their own metadata back", n=len(others), title=keep["label"])
                              if e.get("backup") else M("Merged {n} items into {title}: can’t be undone here (split them in Plex)", n=len(others), title=keep["label"]))
            dst = keep
        if not (src and dst):
            return
        kinds = row.get("kinds") or []
        if "edits" in kinds:
            values, before_fields = {}, {"fields": dict(dst.get("values") or dst.get("fields") or {}), "locked": list(dst.get("locked") or []), "tags": dict(dst.get("tags") or {})}
            for f in EDIT_FIELDS:
                if f in (src.get("fields") or {}):
                    values[f"{f}.value"], values[f"{f}.locked"] = src["fields"][f], 1
            if values:
                dst_client.edit(section, type_no, dst["key"], values)
            for f in TAG_LOCKS:
                if f in (src.get("locked") or []):
                    dst_client.set_tags(section, type_no, dst["key"], f, src.get("tags", {}).get(f) or [], remove=[t for t in dst.get("tags", {}).get(f) or [] if t not in (src.get("tags", {}).get(f) or [])], locked=True)
            if values or any(f in (src.get("locked") or []) for f in TAG_LOCKS):
                e["before"]["edits"] = before_fields
                e["after"]["edits"] = {"fields": dict(src.get("fields") or {}), "locked": list(src.get("locked") or []), "tags": dict(src.get("tags") or {})}
                e["done"].append("edits")
        if "match" in kinds:
            e["before"]["match"] = {"guid": dst.get("guid"), "matched": dst.get("matched")}
            if src.get("matched") and src.get("guid"):
                dst_client.match(dst["key"], src["guid"], src.get("label") or "")
            else:
                dst_client.unmatch(dst["key"])
            e["after"]["match"] = {"guid": src.get("guid"), "matched": src.get("matched")}
            e["done"].append("match")
        if "art" in kinds and src.get("art"):
            before = {}
            for field, kind_list, kind_one, path_key in (("thumb", "posters", "poster", "thumb"), ("art", "arts", "art", "art_path")):
                if field not in src["art"] or not src.get(path_key):
                    continue
                try:
                    chosen = next((im["id"] for im in dst_client.images(dst["key"], kind_list) if im["selected"]), "")
                except PlexError:
                    chosen = ""
                data = src_client.image(src[path_key])
                dst_client.upload_image(dst["key"], data, kind_list)
                before[field] = chosen
            if before:
                e["before"]["art"], e["after"]["art"] = before, list(src["art"])
                e["done"].append("art")
        for kind, field in (("collections", "collection"), ("labels", "label")):
            if kind in kinds:
                want, have = list(src.get(kind) or []), list(dst.get(kind) or [])
                dst_client.set_tags(section, type_no, dst["key"], field, [t for t in want if t not in have], remove=[t for t in have if t not in want])
                e["before"][kind], e["after"][kind] = have, want
                e["done"].append(kind)
        if "editions" in kinds:
            dst_client.edit(section, type_no, dst["key"], {"editionTitle.value": src.get("edition") or "", "editionTitle.locked": 1 if src.get("edition") else 0})
            e["before"]["editions"], e["after"]["editions"] = dst.get("edition") or "", src.get("edition") or ""
            e["done"].append("editions")

    # ------------------------------------------------------------ undoing
    def undo(self, entry_id: str) -> dict:
        """A logged change put back as it was (what can be)."""
        with self.lock:
            e = next((x for x in self.entries if x["id"] == entry_id), None)
        if e is None:
            raise ValueError(M("No such change."))
        if e.get("undone"):
            raise ValueError(M("Already undone."))
        if not e.get("done"):
            raise ValueError(M("Nothing was applied."))
        from . import plexbackup
        left = []
        if "grouping" in e["done"] and e.get("backup") and plexbackup.find(self.app, e["backup"]):  # (a merge: its records put back from the backup, split apart)
            got = plexbackup.revert(self.app, e["backup"], keys=[e["dst_key"]])
            with self.lock:
                e.update(undone=True, undone_at=time.time(), left=[str(x) for x in got["left"]])
            self.save()
            self.app.activity.event("sync", M("{title}: undone on {server}", title=e.get("title"), server=e.get("dst_server")))
            return dict(e)
        client = self.app.other.client(timeout=120) if e.get("dst_mirror") else self.app.plex(timeout=120)
        section, type_no, key, b = e["section"], TYPE_NO.get(e.get("type", ""), 1), e["dst_key"], e.get("before") or {}
        for kind in e["done"]:
            if kind == "edits" and "edits" in b:
                values = {}
                for f in EDIT_FIELDS:
                    if f in (e["after"]["edits"].get("fields") or {}):
                        values[f"{f}.value"], values[f"{f}.locked"] = b["edits"]["fields"].get(f, ""), 1 if f in b["edits"]["locked"] else 0
                if values:
                    client.edit(section, type_no, key, values)
                for f in TAG_LOCKS:
                    if f in (e["after"]["edits"].get("locked") or []):
                        was, now = b["edits"]["tags"].get(f) or [], e["after"]["edits"]["tags"].get(f) or []
                        client.set_tags(section, type_no, key, f, was, remove=[t for t in now if t not in was], locked=f in b["edits"]["locked"])
            elif kind == "match" and "match" in b:
                if b["match"].get("matched") and b["match"].get("guid"):
                    client.match(key, b["match"]["guid"], e.get("title") or "")
                else:
                    client.unmatch(key)
            elif kind == "art" and "art" in b:
                for field, kind_one in (("thumb", "poster"), ("art", "art")):
                    if b["art"].get(field):
                        client.set_image(key, b["art"][field], kind_one)
                    elif field in b["art"]:
                        left.append(M("the {what} chosen before couldn’t be told: choose it in Plex", what=M("poster") if field == "thumb" else M("background")))
            elif kind in ("collections", "labels") and kind in b:
                field = "collection" if kind == "collections" else "label"
                was, now = b[kind], e["after"][kind]
                client.set_tags(section, type_no, key, field, [t for t in was if t not in now], remove=[t for t in now if t not in was])
            elif kind == "editions" and "editions" in b:
                client.edit(section, type_no, key, {"editionTitle.value": b["editions"], "editionTitle.locked": 1 if b["editions"] else 0})
            elif kind == "grouping":
                left.append(M("the merge: split the item in Plex"))
        with self.lock:
            e.update(undone=True, undone_at=time.time(), left=[str(x) for x in left])
        self.save()
        self.app.activity.event("sync", M("{title}: undone on {server}", title=e.get("title"), server=e.get("dst_server")))
        return dict(e)

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "progress": dict(self.progress) if self.progress else None, "error": self.error, "allowed": self.allowed(),
                    "log": [dict(x) for x in reversed(self.entries[-50:])]}

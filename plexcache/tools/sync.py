"""Sync Libraries: a library on this server and on its mirror (the Mirror page) made the same — one way, from the side
whose edits win to the other, through the other's Plex API: the folders of files it doesn't list yet scanned there,
versions grouped as one item where the winning side has them as one, the match, edited fields (set and locked as the
winning side has them; a field edited only on the other side unlocked and given the winning side's value), posters
and backgrounds chosen by hand, collections and labels, the edition. Watched state and ratings aren't: Plex's own sync
carries them (the Mirror page).

What it reads is the data layer's (libdata.py: the mirror's side — both servers' items, each one's own page where the two
differ, and the other site's files through the Plex Cache there), compared by sync.compare_library: items matched across
the two by their files' place in the library folder. So a row is only what this can bring in line. What it can't — several
items on the winning side that are one on the other (a split needs matching each part again), files shared out another way
on both sides (regroup()), a file that isn't on disk on the other side — is Mirror Checks' (mirrorhealth.py), with the
rest of what the folder sync or a scan missed.

Whose edits win is an option like the others, per library: changed, the rows are worked out again the other way round
(``"finds"``: the same data, nothing read). Nothing happens until you press Apply; the records it will change on the
other side are backed up first (plexbackup.py), and Revert puts them back from that backup.
"""

import os

from ..i18n import M
from ..peerlink import rel_files, rel_of
from ..sync import _Side, _norm, compare_library
from . import MAX_ROWS, Tool

KINDS = ("movie", "tv", "audio")
# what the comparison finds (sync.KINDS), by the option it's about; "extra" (items only on the other side) is none of this
# tool's: nothing is deleted
ISSUE = {"missing": "scan", "grouping": "grouping", "match": "matches", "edits": "edits", "art": "art", "collections": "collections", "labels": "collections", "editions": "editions"}
NAMES = {"missing": M("files not listed"), "grouping": M("versions grouped"), "match": M("the match"), "edits": M("edited fields"), "art": M("posters chosen by hand"),
         "collections": M("collections"), "labels": M("labels"), "editions": M("the edition")}


class SyncLibraries(Tool):
    name = "sync"
    title = M("Sync Libraries")
    page = "/sync"
    applies = KINDS
    reads = ("records", "files", "details", "mirror")  # (libdata.ALL: the mirror's side too)
    options = [
        {"id": "source", "name": M("Whose edits win"), "kinds": KINDS, "default": "this", "finds": True,
         "choices": [("this", M("This server")), ("mirror", M("The mirror"))],
         "help": M("The side the other is brought in line with: its matches, versions, edits, posters and collections. This server: the mirror is changed to match it. The mirror: this server is.")},
        {"id": "scan", "name": M("Files the other side doesn’t list"), "kinds": KINDS, "default": True,
         "help": M("A file on the winning side that Plex on the other side doesn’t list, though it’s on disk there (or can’t be told: no Plex Cache at that site): its folder scanned there. A file that isn’t on disk there is Mirror Checks’.")},
        {"id": "grouping", "name": M("Versions grouped differently"), "kinds": KINDS, "default": True,
         "help": M("Files that are one item’s versions on the winning side and several items on the other: merged into one item there (Plex’s own Merge). The other way round — one item there that would need splitting — is Mirror Checks’.")},
        {"id": "matches", "name": M("Matches"), "kinds": KINDS, "default": True,
         "help": M("An item matched to another entry on the other side, or unmatched there: matched as on the winning side.")},
        {"id": "edits", "name": M("Edited fields"), "kinds": KINDS, "default": True,
         "help": M("Fields you edited (Plex locks them) — title, sort title, summary, dates, genres, directors…: set and locked as on the winning side. A field edited only on the other side is unlocked and given the winning side’s value.")},
        {"id": "art", "name": M("Posters chosen by hand"), "kinds": KINDS, "default": True,
         "help": M("A poster or background chosen by hand on the winning side: the same image uploaded and chosen on the other.")},
        {"id": "collections", "name": M("Collections and labels"), "kinds": KINDS, "default": True,
         "help": M("An item’s collections and labels made the same as on the winning side.")},
        {"id": "editions", "name": M("Editions"), "kinds": ("movie",), "default": True,
         "help": M("A film’s edition (Director’s Cut, Extended…) set as on the winning side: Plex doesn’t carry these between servers.")},
    ]
    flag_help = M("The item differs between this server and its mirror in a way Sync Libraries brings in line: a file the other side doesn’t list, versions grouped differently, the match, edited fields, a poster chosen by hand, collections and labels, or the edition.")
    history = [("server", M("Changed on")), ("what", M("Brought in line"))]
    # an item whose files Mirror Checks finds aren't the same at the two sites (only on one, a different size, not on disk
    # here) is red here until that's put right or ignored there: what Plex lists of it would be brought in line over files
    # that differ; so is one whose record here holds a file that looks like another film's (Plex Checks' Dissimilar file
    # versions: its files pair it with the item there); and the whole library held while .plexcache doesn't reach the
    # other site (the guard against both sides syncing over each other reads the other side's marker, which lives there)
    blocking_sources = ("mirror",)
    blocking_checks = ("versions_named_differently",)
    held_by_folder_sync = True

    def evaluated_after(self) -> tuple[str, ...]:
        return ("mirrorchecks", "merge")

    def not_applicable(self, library: str) -> M | None:
        """No mirror watched, or the library not paired with one there: read so by the data layer (libdata's mirror side)."""
        d = self.app.libdata.data(str(library))
        if d.mirror_at and d.mirror is None and not d.errors.get("mirror"):
            return M("This library isn’t paired with one on a mirrored server: set the mirror up on the [Mirror page](/mirrors).")
        return None

    # ------------------------------------------------------------ the scan
    def names(self) -> dict:
        """How the page names the two sides: {"this", "mirror"}."""
        return {"this": getattr(self.app, "plex_name", "") or M("This server"), "mirror": self.app.settings.section("plex2").get("name") or M("the mirror")}

    def scan(self, data) -> dict:
        from .. import libhealth
        key = data.key
        lib = self.app.library_info(key) or {}
        source = self.enabled(key).get("source") or "this"
        m = data.mirror
        out = {"library": key, "rows": [], "total": 0, "records": None, "matched": 0, "source": source, "names": self.names(), "regroup": [],
               "mirror": (m or {}).get("title") or "", "disk": None}
        if not m:  # (no mirror watched, the library not paired with one there, not read yet, or the mirror away)
            out["why"] = "error" if data.errors.get("mirror") else "reading" if not data.mirror_at else "none"
            out["error"] = data.errors.get("mirror") or ""
            return out
        here = [dict(it, **((m.get("pages_here") or {}).get(it["key"]) or {})) for it in m.get("here") or []]
        there = [dict(it, **((m.get("pages_there") or {}).get(it["key"]) or {})) for it in m.get("there") or []]
        locs = [r[0] for r in data.roots] or [loc.rstrip("/") for loc in lib.get("locations") or []]
        a, b = _Side(here, locs), _Side(there, m.get("locations") or [])
        src, dst = (a, b) if source == "this" else (b, a)
        got = compare_library(src, dst, {}, libhealth.enabled(self.app.settings.get()), limit=None)
        # the other side's disk, by each file's place in the library folder: where a scan there can find a file (None: can't
        # be told — no Plex Cache there, or it isn't talking)
        if source == "this":
            disk = None if m.get("disk") is None else {_norm(r[0]) for r in m["disk"]}
            on_disk_there = {}
        else:
            disk = {_norm(r[0]) for r in rel_files(data)}
            on_disk_there = {}  # place -> Plex's path for it here (a file here Plex doesn't list: its own folder)
            for f in list(data.unlisted) + list(data.files):
                rel = rel_of(data, f.get("local") or "")
                if rel:
                    on_disk_there[_norm(rel)] = f.get("path") or ""
        dst_locs = list(m.get("locations") or []) if source == "this" else locs
        out["disk"] = disk is not None
        rows = []
        for r in got["items"]:
            shape = r["shape"]
            if shape == "extra":
                continue  # (only on the other side: nothing is copied, nothing deleted — Mirror Checks list it)
            here_side = r["src_items"] if source == "this" else r["dst_items"]
            if shape in ("merge", "regroup"):  # (not this tool's to do: Mirror Checks', as regroup())
                out["regroup"].append({"shape": shape, "title": r["title"], "keys": [x["key"] for x in here_side],
                                       "here": [x["label"] for x in (r["src_items"] if source == "this" else r["dst_items"])],
                                       "there": [x["label"] for x in (r["dst_items"] if source == "this" else r["src_items"])]})
                continue
            # the winning side's files the other side doesn't list: scanned for there — those on disk there (or that can't be
            # told); one known not to be there is Mirror Checks' (a scan can't find it)
            unlisted = sorted({src.shown[n] for x in r["src_items"] for n in src.rels(x["key"]) if n not in dst.by_rel}, key=str.lower)
            scan, unknown = [], []
            for rel in unlisted:
                if disk is None:
                    unknown.append(rel)
                elif _norm(rel) not in disk:
                    continue
                scan.append(rel)
            changes = [c for c in r["changes"] if not (c["kind"] == "grouping" and shape == "pair") and c["kind"] != "missing"]  # (more files there: they stay)
            if scan:
                changes.insert(0, {"kind": "missing", "from": ", ".join(os.path.basename(x) for x in scan[:3]) + (" …" if len(scan) > 3 else ""),
                                   "to": str(M("not listed: its folder scanned there"))})
            kinds = []
            for c in changes:
                if c["kind"] not in kinds:
                    kinds.append(c["kind"])
            issues = sorted({ISSUE[k] for k in kinds if k in ISSUE})
            if not issues:
                continue
            folders = []
            for rel in scan:
                path = on_disk_there.get(_norm(rel))
                for f in ([os.path.dirname(path)] if path else [os.path.dirname(loc.rstrip("/") + "/" + rel) for loc in dst_locs]):
                    if f not in folders:
                        folders.append(f)
            rows.append({"id": source + ":" + r["id"], "library": key, "title": r["title"], "shape": shape, "source": source,
                         "rks": [x["key"] for x in here_side], "issues": issues, "kinds": kinds, "changes": changes,
                         "src": r["src"], "dst": r["dst"], "src_items": r["src_items"], "dst_items": r["dst_items"], "warn": r["warn"], "tags": r["tags"],
                         "scan": folders, "scan_files": scan, "unknown": unknown, "poster": (here_side[0] if here_side else {}).get("thumb") or ""})
        out.update(rows=rows[:MAX_ROWS], total=len(rows), records=got["total"], matched=got["matched"])
        return out

    def regroup(self, data) -> list[dict]:
        """What this can't bring in line by grouping (Mirror Checks': mirror_regroup): the winning side's several items that
        are one on the other side, and files shared out another way on both — [{"shape", "title", "keys" (here), "here",
        "there" (names)}]. From the analysis worked out (nothing read)."""
        if not data.ready or not data.mirror or not self.works_on(data.key):
            return []
        return self.analysis(data).get("regroup") or []

    def flag_lines(self, rows):
        names = self.names()
        return [M("{what}, brought in line on {server}", what=", ".join(str(NAMES.get(k, k)) for k in r.get("kinds") or []),
                  server=names["mirror"] if r.get("source") != "mirror" else names["this"]) for r in rows]

    # ------------------------------------------------------------ applying
    def plex_keys(self, rows: list[dict]) -> list[str]:
        """The other side's records the rows change (a scan alone changes none)."""
        return [str(x["key"]) for r in rows if any(k != "missing" for k in r.get("kinds") or []) for x in r.get("dst_items") or []]

    def plex_server(self, library: str) -> str:
        return "here" if self.enabled(library).get("source") == "mirror" else "mirror"

    def apply_row(self, row: dict, log) -> dict:
        from .. import syncapply
        key = str(row.get("library") or "")
        on = self.enabled(key)
        if row.get("source") != on.get("source"):
            return {"result": "failed", "reason": M("Whose edits win was changed since this was found: scan again.")}
        kinds = [k for k in row.get("kinds") or [] if on.get(ISSUE.get(k)) is True]
        if not kinds:
            return {"result": "failed", "reason": M("Nothing to do: what it was about is switched off.")}
        e = syncapply.ends(self.app, key, on["source"])
        got = syncapply.apply(row, kinds, e, log)
        if not got["done"]:
            return {"result": "failed", "reason": M("Nothing could be done: scan again."), **got}
        self.app.activity.event("tools", M("{title}: {what} brought in line on {server}", title=row.get("title") or "", what=", ".join(str(NAMES.get(k, k)) for k in got["done"]), server=e["name"]))
        return {"result": "done", **got}

    def history_row(self, row: dict, outcome: dict) -> dict:
        names = self.names()
        return {"server": names["this"] if (outcome.get("server") or ("here" if row.get("source") == "mirror" else "mirror")) == "here" else names["mirror"],
                "what": ", ".join(str(NAMES.get(k, k)) for k in outcome.get("done") or row.get("kinds") or [])}

    def revert_row(self, row: dict, outcome: dict, backup: dict, log) -> None:
        """The row's records put back from the backup the run made first (plexbackup.py): a merge split apart, each record
        given its own metadata back. Without one, what it had before (kept with the result) put back — all but a merge."""
        from .. import plexbackup, syncapply
        done = outcome.get("done") or []
        if not [k for k in done if k != "missing"]:
            log(M("Nothing to put back for {title}: the scan only had Plex look at the folder.", title=row.get("title") or row["id"]))
            return
        if (backup.get("plex") or {}).get("id"):
            keys = [str(k) for k in [outcome.get("dst_key")] + list(outcome.get("merged") or []) if k]
            got = plexbackup.revert(self.app, backup["plex"]["id"], keys=keys)
            if got["failed"] and not got["restored"]:
                raise ValueError(M("{title} not put back: {why}", title=row.get("title") or row["id"], why="; ".join(str(x) for x in got["left"])))
            log(M("{title} put back from the backup", title=row.get("title") or row["id"]) if "grouping" not in done
                else M("{title} split into items of their own again, each with its own metadata back from the backup", title=row.get("title") or row["id"]))
            for why in got["left"]:
                log(why)
            return
        syncapply.undo(self.app, outcome, row.get("title") or "", log)

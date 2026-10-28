"""Sync Libraries, applying (tools/sync.py): one row's differences made true on the side whose edits don't win, through
its Plex API — one way, from the other — and undone again where no backup of the records was made.

What's applied, per item: the folders of files it doesn't list yet scanned there; where the winning side's one item is
several there, those merged into one; the match; edited fields (set and locked as the winning side has them — and a
field edited only there unlocked and given the winning side's value); edited tag fields (genres, directors…); posters
and backgrounds chosen by hand (the winning side's image uploaded); collections and labels; the edition. Nothing is
deleted: a file only on the other side stays, as do items. What the winning side's several items are as one item there
(a split needs matching each part again), or files shared out another way on both sides, is Mirror Checks'.

Before the first change, the records it will change are backed up (plexbackup.py, per the Backup preference: the tool's
process opens it), and Revert puts them back from it. What each row had before is kept with its result too, so a run made
without that backup ("A backup I make myself") is undone the old way: the fields set back (and unlocked, if they
weren't locked), the match put back, the poster chosen before chosen again, collections and labels as they were.
"""

from __future__ import annotations

from .i18n import M
from .plex import EDIT_FIELDS, PlexError

TYPE_NO = {"movie": 1, "show": 2, "season": 3, "episode": 4, "artist": 8, "album": 9, "track": 10}
TAG_LOCKS = ("genre", "writer", "director", "producer", "country")  # tag fields an edit locks (collection and label have kinds of their own)


def ends(app, key: str, source: str) -> dict:
    """The two servers of a library, as a row is applied: {"src", "dst" (clients), "section" (the library on the side
    changed), "name" (that side's), "server" ("here" | "mirror": the side changed)}."""
    there = app.other.pairs().get(str(key))
    if there is None:
        raise ValueError(M("This library isn’t paired with one on the mirror any more ([Mirror page](/mirrors))."))
    here = app.plex(timeout=120)
    app.other.run(lambda c: c.identity(), timeout=20)  # (reached — at another of its addresses if need be)
    mirror = app.other.client(timeout=120)
    if source == "mirror":
        return {"src": mirror, "dst": here, "section": str(key), "name": getattr(app, "plex_name", "") or M("This server"), "server": "here"}
    return {"src": here, "dst": mirror, "section": there.key, "name": app.settings.section("plex2").get("name") or M("the mirror"), "server": "mirror"}


def client_for(app, server: str):
    return app.other.client(timeout=120) if server == "mirror" else app.plex(timeout=120)


def apply(row: dict, kinds: list[str], e: dict, log) -> dict:
    """One row made true on the side changed (``e``: ends()), for the kinds of difference named. What was done, and what
    the item had before: {"done": [kinds], "dst_key", "merged": [keys], "scanned": [folders], "before", "after", "section",
    "type", "server"}."""
    src_client, dst_client, section = e["src"], e["dst"], e["section"]
    src, dst = row.get("src"), row.get("dst")
    out = {"done": [], "dst_key": (dst or {}).get("key"), "merged": [], "scanned": [], "before": {}, "after": {}, "section": section,
           "type": (dst or src or {}).get("type", ""), "server": e["server"]}
    if "missing" in kinds and row.get("scan"):
        for folder in row["scan"]:
            dst_client.scan(section, folder)
            out["scanned"].append(folder)
        out["done"].append("missing")
        log(M("{server}’s Plex asked to scan {folder}", server=e["name"], folder=row["scan"][0]) if len(row["scan"]) == 1
            else M("{server}’s Plex asked to scan {n} folders", server=e["name"], n=len(row["scan"])))
    type_no = TYPE_NO.get(out["type"], 1)
    if row.get("shape") == "split":  # one item on the winning side, several here: merged into the one holding its first file
        if "grouping" not in kinds:
            return out
        items = row.get("dst_items") or []
        first = ((src or {}).get("files") or [None])[0]
        keep = next((it for it in items if first in (it.get("files") or [])), items[0])
        others = [it["key"] for it in items if it["key"] != keep["key"]]
        dst_client.merge(keep["key"], others)
        out["dst_key"], out["merged"] = keep["key"], others
        out["done"].append("grouping")
        log(M("{n} items merged into {title} on {server}", one="{n} item merged into {title} on {server}", n=len(others), title=keep.get("label") or keep["key"], server=e["name"]))
        return out  # (what else differs shows once it's one item: scanned again after the run)
    if not (src and dst):
        return out
    key = dst["key"]
    if "edits" in kinds:
        sf, df = src.get("fields") or {}, dst.get("fields") or {}
        sl, dl = set(src.get("locked") or []), set(dst.get("locked") or [])
        values = {}
        for f in EDIT_FIELDS:
            if f in sf and (f not in df or str(sf[f]) != str(df[f])):
                values[f"{f}.value"], values[f"{f}.locked"] = sf[f], 1
            elif f in dl and f not in sl:  # (edited only here: unlocked, with the winning side's own value)
                v = (src.get("values") or {}).get(f, "")
                if v or f != "year":
                    values[f"{f}.value"] = v
                values[f"{f}.locked"] = 0
        if values:
            dst_client.edit(section, type_no, key, values)
        tags_done = []
        for f in TAG_LOCKS:
            want, have = list((src.get("tags") or {}).get(f) or []), list((dst.get("tags") or {}).get(f) or [])
            if f in sl:
                dst_client.set_tags(section, type_no, key, f, want, remove=[t for t in have if t not in want], locked=True)
                tags_done.append(f)
            elif f in dl:
                dst_client.set_tags(section, type_no, key, f, [t for t in want if t not in have], remove=[t for t in have if t not in want], locked=False)
                tags_done.append(f)
        if values or tags_done:
            out["before"]["edits"] = {"fields": dict(dst.get("values") or {}), "locked": sorted(dl), "tags": dict(dst.get("tags") or {})}
            changed = sorted({k.rsplit(".", 1)[0] for k in values})
            out["after"]["edits"] = {"fields": {f: values.get(f + ".value", "") for f in changed}, "locked": sorted(sl), "tags": dict(src.get("tags") or {}), "tag_fields": tags_done}
            out["done"].append("edits")
            log(M("Edited fields set as on the other side: {fields}", fields=", ".join(changed + tags_done)))
    if "match" in kinds:
        out["before"]["match"] = {"guid": dst.get("guid"), "matched": dst.get("matched")}
        if src.get("matched") and src.get("guid"):
            dst_client.match(key, src["guid"], src.get("label") or "")
            log(M("Matched to {guid}", guid=src["guid"]))
        else:
            dst_client.unmatch(key)
            log(M("Unmatched, as on the other side"))
        out["after"]["match"] = {"guid": src.get("guid"), "matched": src.get("matched")}
        out["done"].append("match")
    if "art" in kinds and src.get("art"):
        before = {}
        for field, kind_list, path_key in (("thumb", "posters", "thumb"), ("art", "arts", "art_path")):
            if field not in src["art"] or not src.get(path_key):
                continue
            try:
                chosen = next((im["id"] for im in dst_client.images(key, kind_list) if im["selected"]), "")
            except PlexError:
                chosen = ""
            dst_client.upload_image(key, src_client.image(src[path_key]), kind_list)
            before[field] = chosen
        if before:
            out["before"]["art"], out["after"]["art"] = before, list(src["art"])
            out["done"].append("art")
            log(M("The poster chosen by hand uploaded and chosen") if "thumb" in before and "art" not in before
                else M("The background chosen by hand uploaded and chosen") if "thumb" not in before else M("The poster and background chosen by hand uploaded and chosen"))
    for kind, field in (("collections", "collection"), ("labels", "label")):
        if kind in kinds:
            want, have = list(src.get(kind) or []), list(dst.get(kind) or [])
            dst_client.set_tags(section, type_no, key, field, [t for t in want if t not in have], remove=[t for t in have if t not in want])
            out["before"][kind], out["after"][kind] = have, want
            out["done"].append(kind)
            log(M("Collections set: {names}", names=", ".join(want) or "—") if kind == "collections" else M("Labels set: {names}", names=", ".join(want) or "—"))
    if "editions" in kinds:
        dst_client.edit(section, type_no, key, {"editionTitle.value": src.get("edition") or "", "editionTitle.locked": 1 if src.get("edition") else 0})
        out["before"]["editions"], out["after"]["editions"] = dst.get("edition") or "", src.get("edition") or ""
        out["done"].append("editions")
        log(M("Edition set: {edition}", edition=src.get("edition") or "—"))
    return out


def undo(app, outcome: dict, title: str, log) -> None:
    """A row put back as it was from what it had before (apply()'s outcome) — what can be, without a backup of the records:
    a merge can't be (split the item in Plex), nor a scan (it only made Plex look)."""
    client = client_for(app, outcome.get("server") or "mirror")
    section, type_no, key, b, after = outcome.get("section") or "", TYPE_NO.get(outcome.get("type") or "", 1), outcome.get("dst_key"), outcome.get("before") or {}, outcome.get("after") or {}
    for kind in outcome.get("done") or []:
        if kind == "edits" and "edits" in b:
            values = {}
            for f in EDIT_FIELDS:
                if f in (after["edits"].get("fields") or {}):
                    was = b["edits"]["fields"].get(f, "")
                    if was or f != "year":
                        values[f"{f}.value"] = was
                    values[f"{f}.locked"] = 1 if f in b["edits"]["locked"] else 0
            if values:
                client.edit(section, type_no, key, values)
            for f in after["edits"].get("tag_fields") or []:
                was, now = b["edits"]["tags"].get(f) or [], after["edits"]["tags"].get(f) or []
                client.set_tags(section, type_no, key, f, was, remove=[t for t in now if t not in was], locked=f in b["edits"]["locked"])
            log(M("Edited fields put back"))
        elif kind == "match" and "match" in b:
            if b["match"].get("matched") and b["match"].get("guid"):
                client.match(key, b["match"]["guid"], title or "")
            else:
                client.unmatch(key)
            log(M("The match put back"))
        elif kind == "art" and "art" in b:
            for field, kind_one in (("thumb", "poster"), ("art", "art")):
                if b["art"].get(field):
                    client.set_image(key, b["art"][field], kind_one)
                elif field in b["art"]:
                    log(M("The {what} chosen before couldn’t be told: choose it in Plex", what=M("poster") if field == "thumb" else M("background")))
        elif kind in ("collections", "labels") and kind in b:
            field = "collection" if kind == "collections" else "label"
            was, now = b[kind], after[kind]
            client.set_tags(section, type_no, key, field, [t for t in was if t not in now], remove=[t for t in now if t not in was])
            log(M("Collections put back") if kind == "collections" else M("Labels put back"))
        elif kind == "editions" and "editions" in b:
            client.edit(section, type_no, key, {"editionTitle.value": b["editions"], "editionTitle.locked": 1 if b["editions"] else 0})
            log(M("The edition put back"))
        elif kind == "grouping":
            log(M("The merge can’t be undone without the backup: split the item in Plex"))
        elif kind == "missing":
            log(M("Nothing to put back for the scan: it only had Plex look at the folder"))

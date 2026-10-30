"""Duplicate Versions: one Plex record holding the same file twice (the same running time and size —
a copy, or a second path to one file), the extra copies set aside. Which is kept: the copy in a folder
of its own, then the newest, then the first Plex lists. And the copies Plex doesn't list at all: a
second path to a listed file (a hard link), or an identical copy of one (the same size and bytes at
both ends) — Plex's scanner skips a file it already has under another path, so these never show in
Plex, only on disk. The extras go to the library's trash
(.plexcache-trash, on the same dataset) and Plex is asked to scan; they're deleted for good by
trash.py once Plex here — and the mirror's Plex, whose synced folder carries the change —
no longer list them, and the hold is over. Until then they can be put back.
"""

import os

from ..i18n import M
from ..libdata import same_bytes
from ..libhealth import _same_file
from ..plex import PlexError
from . import MAX_ROWS, Tool
from ..paging import sort_key


def _brief(x: dict, keep: dict | None = None) -> dict:
    return {"path": x["path"], "local": x["local"], "name": os.path.basename(x["path"]), "size": x["size"], "sub": x["sub"], "mtime": x["mtime"], "ino": x["ino"] or 0,
            "linked": bool(keep and keep["ino"] and keep["ino"] == x["ino"]),
            "folder": os.path.relpath(os.path.dirname(x["local"]), x["root"]) if x["root"] else os.path.dirname(x["path"])}


def _held(idx: dict) -> dict[str, set]:
    """A server's records and the files each holds, from its files ({path: rating key})."""
    held: dict[str, set] = {}
    for p, rk in idx.items():
        held.setdefault(rk, set()).add(p)
    return held


def _ending(idx: dict, held: dict, extras: list[dict], where) -> set[str]:
    """The paths (keys of ``idx``) of ``extras`` — found at ``where(path)`` — whose record holds no other file: deleting
    them would leave the record empty, and Plex would remove it, its metadata and watched state with it."""
    gone = {c for x in extras for c in where(x["path"]) if c in idx}
    return {p for p in gone if held[idx[p]] <= gone}


def _copies(row: dict) -> list[dict]:
    """A row's copies: the one kept, then the ones set aside."""
    return [row["keep"]] + list(row["extras"])


class DuplicateVersions(Tool):
    name = "versions"
    title = M("Duplicate Versions")
    page = "/versions"
    options = [{"id": "versions", "name": M("The same file twice on one record"), "help": M("One Plex record holds the same file as two of its versions — the same size and running time, the same bytes at both ends."), "kinds": ("movie", "tv", "audio"), "default": True},
               {"id": "unlisted", "name": M("A copy on disk Plex doesn’t list"), "help": M("A media file in the library folder that Plex doesn’t list, holding the same bytes as a file it does."), "kinds": ("movie", "tv", "audio"), "default": True}]
    scan_version = 4  # (each copy says, for each copy kept instead — the page's arrow — whether deleting it would destroy a record)
    keeps = M("the extra copies themselves: set aside in the library’s .plexcache-trash, kept for the hold (and put back from the page)")
    flag_help = M("One Plex item holding the same file twice — a copy that slipped in, a second path to one file — or a copy on disk that Plex doesn’t list: it takes space and is cached and optimized twice. Duplicate Versions sets the extra copies aside, and deletes them once every Plex has let go of them.")

    def flag_lines(self, rows):
        return [M("{n} extra copies of {name}", one="{n} extra copy of {name}", n=len(r.get("extras") or []), name=(r.get("keep") or {}).get("name") or r.get("title") or "") for r in rows]

    def scan(self, data) -> dict:
        library = data.key
        lib = self.app.library_info(library)
        if not lib:
            raise ValueError(M("No such library."))
        files = [f for f in data.files if not f.get("missing")]
        by_item: dict[str, list] = {}
        for f in files:
            if f.get("rk"):
                by_item.setdefault(f["rk"], []).append(f)
        rows = []
        for i, (rk, group) in enumerate(by_item.items()):
            if i % 50 == 0:
                self.step(i, len(by_item))
            if len(group) < 2:
                continue
            infos = []
            for f in group:
                local, root = f["local"], f.get("root") or ""
                infos.append({"path": f["path"], "local": local, "size": f["size"], "duration": f.get("duration") or 0, "ino": f["ino"], "dev": f["dev"],
                              "mtime": f["mtime"], "root": root, "sub": bool(root) and "/" in local[len(root) + 1:]})
            # clusters of the same file (union-find over the pairs)
            parent = list(range(len(infos)))

            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i
            for i in range(len(infos)):
                for j in range(i + 1, len(infos)):
                    if _same_file(infos[i], infos[j]):
                        parent[find(i)] = find(j)
            clusters: dict[int, list] = {}
            for i, x in enumerate(infos):
                clusters.setdefault(find(i), []).append(x)
            for n, cluster in enumerate(c for c in clusters.values() if len(c) > 1):
                cluster.sort(key=lambda x: (not x["sub"], -x["mtime"]))  # (stable: the first listed wins a tie)
                keep, extras = cluster[0], cluster[1:]
                rows.append({"id": f"{rk}:{n}", "library": str(library), "rk": rk, "title": group[0].get("title") or os.path.basename(keep["path"]),
                             "poster": group[0].get("poster") or "", "issues": ["versions"], "keep": _brief(keep), "extras": [_brief(x, keep) for x in extras],
                             "frees": sum(x["size"] for x in extras if not (keep["ino"] and x["ino"] == keep["ino"]))})
        rows += self._unlisted(data)
        rows.sort(key=lambda r: sort_key(r["title"]))
        here = {f["path"]: f["rk"] for f in files if f.get("rk")}
        held = _held(here)
        for r in rows:  # (a record here whose only files are the ones deleted: deleting them would destroy it) — for each copy kept
            r["ending"] = {k["path"]: {"here": sorted(_ending(here, held, [x for x in _copies(r) if x is not k], lambda p: [p])), "there": []} for k in _copies(r)}
            for x in r["extras"]:
                x["ends_record"] = x["path"] in r["ending"][r["keep"]["path"]]["here"]
        there = self.mirror(library)
        if there is not None:  # each extra copy on the mirror: listed there too (then its synced folder carries the deletion there)
            try:
                idx = self.app.mirrors.index(there)
                held_there = _held(idx)
                for r in rows:
                    self._there(library, there, idx, held_there, r)
            except PlexError as e:
                for r in rows:
                    r["mirror_error"] = str(e)
        for r in rows:  # (red: a record here destroyed. A record destroyed on the mirror is said, grey, and doesn't stop it)
            r["blocked"] = any(x.get("ends_record") for x in r["extras"])
        return {"library": str(library), "rows": rows[:MAX_ROWS], "total": len(rows), "records": len(data.records), "items": sum(1 for g in by_item.values() if len(g) > 1), "frees": sum(r["frees"] for r in rows),
                "mirror": {"name": self.mirror_name(library)} if there is not None else None, "hold_days": self.app.trash.hold_days()}

    def _there(self, library: str, there, idx: dict, held: dict, row: dict) -> None:
        """Each extra copy of ``row`` on the mirror (``idx``: its files, {path: rating key}): listed there too, and
        whether it's the only file of its record there — the synced folder carries the deletion, so that record would go."""
        where = lambda p: self.app.mirrors.paths_there(library, p, there)  # noqa: E731
        for x in _copies(row):
            x["there"] = any(c in idx for c in where(x["path"]))
        for k in _copies(row):  # (for each copy kept: the arrow on the page can keep another)
            drop = [x for x in _copies(row) if x is not k]
            ending = _ending(idx, held, drop, where)
            row.setdefault("ending", {}).setdefault(k["path"], {"here": []})["there"] = sorted(x["path"] for x in drop if any(c in ending for c in where(x["path"])))
        gone = set(row["ending"][row["keep"]["path"]]["there"])
        for x in row["extras"]:
            x["ends_record_there"] = x["path"] in gone

    def choose(self, row: dict, choice: dict) -> dict:
        """Another copy kept (the arrow on the page: ``keep_key``, its path): the others are the ones set aside, each with
        what deleting it would destroy when that copy is kept, as the scan found it."""
        row = super().choose(row, choice)
        copies, want = _copies(row), str(row.get("keep_key") or "")
        keep = next((x for x in copies if x["path"] == want), None)
        if keep is None or keep["path"] == row["keep"]["path"]:
            return row
        ending = (row.get("ending") or {}).get(keep["path"]) or {"here": [], "there": []}
        extras = []
        for x in copies:
            if x is keep:
                continue
            linked = bool(keep.get("ino") and keep.get("ino") == x.get("ino"))
            extras.append(dict(x, linked=linked, ends_record=x["path"] in ending["here"], ends_record_there=x["path"] in ending.get("there", [])))
        keep = {k: v for k, v in keep.items() if k not in ("ends_record", "ends_record_there", "linked")}
        return dict(row, keep=keep, extras=extras, blocked=any(x["ends_record"] for x in extras),
                    frees=sum(x["size"] for x in extras if not x["linked"]))

    def _unlisted(self, data) -> list[dict]:
        """Media files in the library's folders that Plex doesn't list (data.unlisted), and are a listed file over
        again: a second path to it (the same inode), or an identical copy (the same size, the same bytes at
        both ends). Each a row: the listed file kept, the unlisted one set aside."""
        by_ino: dict[tuple, dict] = {}
        by_size: dict[int, list] = {}
        for f in data.files:
            if f.get("missing"):
                continue
            root = f.get("root") or ""
            info = {"path": f["path"], "local": f["local"], "size": f["size"], "ino": f["ino"], "dev": f["dev"], "mtime": f["mtime"], "root": root,
                    "sub": bool(root) and "/" in f["local"][len(root) + 1:], "rk": f.get("rk") or "", "title": f.get("title") or "", "poster": f.get("poster") or ""}
            by_ino[(f["dev"], f["ino"])] = info
            by_size.setdefault(f["size"], []).append(info)
        rows = []
        for u in data.unlisted:
            keep = by_ino.get((u["dev"], u["ino"]))
            linked = keep is not None
            if keep is None:
                for cand in by_size.get(u["size"]) or []:
                    if same_bytes(u["local"], cand["local"], u["size"]):
                        keep = cand
                        break
            if keep is None:
                continue
            local_root = u["root"]
            x = {"path": u["path"], "local": u["local"], "size": u["size"], "ino": u["ino"], "mtime": u["mtime"], "root": local_root, "sub": "/" in u["local"][len(local_root) + 1:]}
            rows.append({"id": "disk:" + u["path"], "library": data.key, "rk": keep["rk"], "title": keep["title"] or os.path.basename(keep["path"]), "poster": keep["poster"],
                         "issues": ["unlisted"], "keep": _brief(keep), "extras": [dict(_brief(x, keep), unlisted=True)], "unlisted": True, "frees": 0 if linked else u["size"]})
        return rows

    history = [("set_aside", M("Set aside")), ("frees", M("Freed")), ("kept", M("Kept"))]

    def history_row(self, row: dict, outcome: dict) -> dict:
        return {"set_aside": int(outcome.get("set_aside") or len(row.get("extras") or [])), "frees": int(outcome.get("frees") or 0),
                "kept": (row.get("keep") or {}).get("name") or ""}

    def revert_row(self, row: dict, outcome: dict, backup: dict, log) -> None:
        """The copies set aside put back (the trash keeps them for the hold), and Plex asked to scan."""
        for x in row.get("extras") or []:
            self.app.trash.act(x["local"], "restore")
            log(M("{file} put back", file=x["name"]))
        try:
            plex = self.app.plex()
            for folder in sorted({os.path.dirname(x["path"]) for x in row.get("extras") or []}):
                plex.scan(row["library"], folder)
        except Exception as e:  # noqa: BLE001
            log(M("Plex wasn’t asked to scan: {why}", why=str(e)))

    def apply_row(self, row: dict, log) -> dict:
        library, folders = row["library"], []
        there = self.mirror(library)
        if there is not None:  # (the mirror as it is now: a record there left with no file is refused, as the page does)
            idx = self.app.mirrors.index(there)
            self._there(library, there, idx, _held(idx), row)
        if any(x.get("ends_record") for x in row["extras"]):  # (one on the mirror isn't refused: the page says it, grey)
            raise ValueError(M("Deleting it would destroy a Plex record: merge the records with Merge Duplicates first."))
        for x in row["extras"]:
            root = self.root_of(library, x["path"])
            if not root:
                raise ValueError(M("{file} isn’t in one of the library’s folders", file=x["name"]))
            got = self.file_action("set_aside", x["local"], root=root)
            self.app.trash.add(library, x["local"], x["path"], got["trashed"], why=M("a duplicate version of {title}", title=row["title"]), title=row["title"])
            log(M("{file} set aside in the library’s trash", file=x["name"]))
            if os.path.dirname(x["path"]) not in folders:
                folders.append(os.path.dirname(x["path"]))
        plex = self.app.plex()
        for folder in folders:
            plex.scan(library, folder)
        try:
            plex.empty_trash(library)
        except Exception as e:  # noqa: BLE001 — Plex does it by itself after the scan, unless that's off
            log(M("Plex’s trash wasn’t emptied: {why}", why=str(e)))
        log(M("Plex asked to scan {n} folders", one="Plex asked to scan the folder", n=len(folders)))
        self.app.activity.event("tools", M("{n} duplicate versions of {title} set aside", one="a duplicate version of {title} set aside", n=len(row["extras"]), title=row["title"]))
        return {"result": "done", "set_aside": len(row["extras"]), "frees": row.get("frees", 0)}

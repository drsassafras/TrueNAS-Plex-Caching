"""Duplicate Versions: one Plex record holding the same file twice (the same running time and size —
a copy, or a second path to one file), the extra copies set aside. Which is kept: the copy in a folder
of its own, then the newest, then the first Plex lists. And the copies Plex doesn't list at all: a
second path to a listed file (a hard link), or an identical copy of one (the same size and bytes at
both ends) — Plex's scanner skips a file it already has under another path, so these never show in
Plex, only on disk. The extras go to the library's trash
(.plexcache-trash, on the same dataset) and Plex is asked to scan; they're deleted for good by
trash.py once Plex here — and the mirrored server's Plex, whose synced folder carries the change —
no longer list them, and the hold is over. Until then they can be put back.
"""

from __future__ import annotations

import os

from ..i18n import M
from ..libhealth import _same_file
from ..plex import PlexError
from . import MAX_ROWS, Tool

MEDIA = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".flv", ".wmv", ".ogm", ".ogv", ".webm",
         ".mp3", ".flac", ".m4a", ".m4b", ".ogg", ".oga", ".opus", ".wav", ".aac", ".wma")
SAMPLE = 64 * 1024  # bytes read at each end of two files of one size, to tell an identical copy


def _ends(path: str, size: int) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(SAMPLE)
            if size > SAMPLE:
                fh.seek(max(SAMPLE, size - SAMPLE))
                return head + fh.read(SAMPLE)
            return head
    except OSError:
        return None


def _brief(x: dict, keep: dict | None = None) -> dict:
    return {"path": x["path"], "local": x["local"], "name": os.path.basename(x["path"]), "size": x["size"], "sub": x["sub"], "mtime": x["mtime"],
            "linked": bool(keep and keep["ino"] and keep["ino"] == x["ino"]),
            "folder": os.path.relpath(os.path.dirname(x["local"]), x["root"]) if x["root"] else os.path.dirname(x["path"])}


class DuplicateVersions(Tool):
    name = "versions"
    scan_version = 2  # (each extra copy says whether the mirror lists it)

    def scan(self, library: str, options: dict | None = None) -> dict:
        lib = self.app.library_info(library)
        if not lib:
            raise ValueError(M("No such library."))
        files = [f for f in list(self.app.files) if str(f.library_key) == str(library)]
        by_item: dict[str, list] = {}
        for f in files:
            if getattr(f, "rating_key", ""):
                by_item.setdefault(f.rating_key, []).append(f)
        rows = []
        for rk, group in by_item.items():
            if len(group) < 2:
                continue
            infos = []
            for f in group:
                local = self.app.map_path(f.path)
                try:
                    st = os.lstat(local)
                except OSError:
                    continue
                root = self.root_of(library, f.path)
                infos.append({"path": f.path, "local": local, "size": st.st_size, "duration": getattr(f, "duration_ms", 0), "ino": st.st_ino,
                              "mtime": st.st_mtime, "root": root, "sub": bool(root) and "/" in local[len(root) + 1:]})
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
                rows.append({"id": f"{rk}:{n}", "library": str(library), "rk": rk, "title": getattr(group[0], "title", "") or os.path.basename(keep["path"]),
                             "poster": getattr(group[0], "poster", ""), "keep": _brief(keep), "extras": [_brief(x, keep) for x in extras],
                             "frees": sum(x["size"] for x in extras if not (keep["ino"] and x["ino"] == keep["ino"]))})
        rows += self._unlisted(library, lib, files)
        rows.sort(key=lambda r: str(r["title"]).lower())
        there = self.mirror(library)
        if there is not None:  # each extra copy on the mirrored server: listed there too (then its synced folder carries the deletion there)
            try:
                idx = self.app.other.index(there)
                for r in rows:
                    for x in r["extras"]:
                        x["there"] = any(c in idx for c in self.app.other.paths_there(library, x["path"], there))
            except PlexError as e:
                for r in rows:
                    r["mirror_error"] = str(e)
        return {"library": str(library), "rows": rows[:MAX_ROWS], "total": len(rows), "items": sum(1 for g in by_item.values() if len(g) > 1), "frees": sum(r["frees"] for r in rows),
                "mirror": {"name": self.mirror_name()} if there is not None else None, "hold_days": self.app.trash.hold_days()}

    def _unlisted(self, library: str, lib: dict, files: list) -> list[dict]:
        """Media files in the library's folders that Plex doesn't list, and are a listed file over again:
        a second path to it (the same inode), or an identical copy (the same size, the same bytes at
        both ends). Each a row: the listed file kept, the unlisted one set aside."""
        listed: dict[str, dict] = {}
        by_ino: dict[tuple, dict] = {}
        by_size: dict[int, list] = {}
        for f in files:
            local = self.app.map_path(f.path)
            try:
                st = os.lstat(local)
            except OSError:
                continue
            root = self.root_of(library, f.path)
            info = {"path": f.path, "local": local, "size": st.st_size, "ino": st.st_ino, "dev": st.st_dev, "mtime": st.st_mtime, "root": root,
                    "sub": bool(root) and "/" in local[len(root) + 1:], "rk": getattr(f, "rating_key", ""), "title": getattr(f, "title", ""), "poster": getattr(f, "poster", "")}
            listed[local] = info
            by_ino[(st.st_dev, st.st_ino)] = info
            by_size.setdefault(st.st_size, []).append(info)
        rows = []
        for loc in lib.get("locations") or []:
            loc = loc.rstrip("/")
            local_root = self.app.map_path(loc)
            for dirpath, dirs, names in os.walk(local_root):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for n in names:
                    if n.startswith(".") or not n.lower().endswith(MEDIA):
                        continue
                    local = os.path.join(dirpath, n)
                    if local in listed:
                        continue
                    try:
                        st = os.lstat(local)
                    except OSError:
                        continue
                    if not os.path.isfile(local) or os.path.islink(local):
                        continue
                    keep = by_ino.get((st.st_dev, st.st_ino))
                    linked = keep is not None
                    if keep is None:
                        mine = None
                        for cand in by_size.get(st.st_size) or []:
                            mine = mine if mine is not None else _ends(local, st.st_size)
                            if mine is not None and mine == _ends(cand["local"], st.st_size):
                                keep = cand
                                break
                    if keep is None:
                        continue
                    plex_path = loc + local[len(local_root):]
                    x = {"path": plex_path, "local": local, "size": st.st_size, "ino": st.st_ino, "mtime": st.st_mtime, "root": local_root, "sub": "/" in local[len(local_root) + 1:]}
                    rows.append({"id": "disk:" + plex_path, "library": str(library), "rk": keep["rk"], "title": keep["title"] or os.path.basename(keep["path"]), "poster": keep["poster"],
                                 "keep": _brief(keep), "extras": [dict(_brief(x, keep), unlisted=True)], "unlisted": True, "frees": 0 if linked else st.st_size})
        return rows

    def apply_row(self, row: dict, log) -> dict:
        library, folders = row["library"], []
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

"""Unlink Files: a media file that's a link — a hard link (one file under two paths) or a symlink —
untangled. Why: Syncthing can't carry a hard link, so the other site gets two whole copies that
drift apart once either changes; a symlink is carried as a link, and dangles there when its target
isn't in the synced folder; and Plex lists only one of two paths to the same bytes, so the other
looks like a file that's there while Plex ignores it.

Two ways, the scan's option ``remove``:

* **A file of its own** (the default, and the only way on a synced library): the bytes copied under
  the same path, the dates kept, so Plex and the folder sync see the same file as before. Nothing
  disappears anywhere — a path removed here would be removed at the other site too, where it isn't
  a link but the only copy. Then Merge Duplicates or Duplicate Versions tidy the copies, the same
  way on each side.
* **The extra paths removed** (``remove``: a library with no mirrored server only): the paths to
  the file that Plex doesn't list are removed, so the file is where Plex has it and nowhere else
  (optimize/files.py unlink, which only removes a path while another path still holds the file).
  A link whose every path Plex lists (two records) is made a file of its own instead, for Merge
  Duplicates. Refused outright when the library has a mirror.

The copy is made by the optimizer service (optimize/files.py make_real).
"""

from __future__ import annotations

import os

from ..i18n import M
from ..libdata import link_of
from . import MAX_ROWS, Tool
from ..paging import sort_key


class UnlinkFiles(Tool):
    name = "unlink"
    title = M("Unlink Files")
    page = "/unlink"
    flag_help = M("A media file that’s a link: a hard link (one file under two paths — Plex lists one and ignores the other, and a folder sync turns it into two whole copies that drift apart) or a symlink. Unlink Files makes each a file of its own, or on a library that isn’t synced removes the extra paths.")

    def flag_lines(self, rows):
        return [M("{name}: a symlink to {target}", name=r.get("name") or "", target=r.get("target") or "") if r.get("kind") == "symlink"
                else M("{name}: a hard link, {n} paths to one file", name=r.get("name") or "", n=r.get("links") or 2) for r in rows]

    def scan(self, data, options: dict | None = None) -> dict:
        library = data.key
        lib = self.app.library_info(library)
        if not lib:
            raise ValueError(M("No such library."))
        remove = bool((options or {}).get("remove"))
        mirror = self.mirrored(library)
        if remove and mirror is not None:
            raise ValueError(M("Not while the files may be mirrored ({name}): a path removed here is removed there too by the folder sync — and there it isn’t a link but the only copy. Make each link a file of its own instead, then tidy the copies with Merge Duplicates on each side; or say there’s no mirror on the Sync page.", name=mirror["name"]))
        rows, links = [], 0
        files = [f for f in data.files if not f.get("missing")]
        listed = data.listed()
        siblings = data.by_inode()
        for i, f in enumerate(files):
            if i % 50 == 0:
                self.step(i, len(files))
            kind, n, target = link_of(f)
            if not kind:
                continue
            links += 1
            local = f["local"]
            # the other paths to the same file in the library folders, and whether Plex lists each: the unlisted
            # ones are what "remove" takes away (none when every path is a record of its own)
            others = [{"path": p, "listed": p in listed} for p in siblings.get((f["dev"], f["ino"]), []) if p != local] if kind == "hardlink" else []
            rows.append({"id": "link:" + f["path"], "library": str(library), "rk": f.get("rk") or "", "title": f.get("title") or os.path.basename(f["path"]), "poster": f.get("poster") or "",
                         "path": f["path"], "local": local, "name": os.path.basename(f["path"]), "kind": kind, "links": n, "target": target, "size": f["size"],
                         "others": others, "remove": remove, "removable": [o["path"] for o in others if not o["listed"]]})
        rows.sort(key=lambda r: sort_key(r["title"]))
        return {"library": str(library), "rows": rows[:MAX_ROWS], "total": len(rows), "links": links, "remove": remove, "mirror": mirror}

    history = [("what", M("What was done")), ("written", M("Written")), ("paths", M("Paths"))]

    def history_row(self, row: dict, outcome: dict) -> dict:
        removed = int(outcome.get("removed") or 0)
        return {"what": M("{n} paths removed", one="{n} path removed", n=removed) if removed else M("Made a file of its own (was a {kind})", kind=row.get("kind") or "link"),
                "written": int(outcome.get("bytes") or 0), "paths": int(row.get("links") or 1)}

    def changed_paths(self, row: dict, outcome: dict) -> list[str]:
        return [] if outcome.get("removed") else [row["local"]]  # (a link made a file of its own: its path; paths removed are re-linked instead)

    def revert_row(self, row: dict, outcome: dict, backup: dict, log) -> None:
        if outcome.get("removed"):  # the extra paths removed: made again as links to the file, where Plex still has it
            for p in row.get("removable") or []:
                self.file_action("link", row["local"], to=p)
                log(M("{path} made a path to the file again", path=os.path.basename(p)))
            return
        # a hard link made a file of its own: the other path still holds the original, so the copy goes and the path is
        # linked to it again — no snapshot needed; a symlink, or a link whose other paths are gone, comes back from the snapshot
        others = [o["path"] for o in row.get("others") or [] if o.get("path")]
        if row.get("kind") == "hardlink" and others:
            keep = others[0]
            self.file_action("unlink", row["local"], to=keep)  # (only while keep still holds the file)
            self.file_action("link", keep, to=row["local"])
            log(M("{file} is a link to {other} again; the copy is gone", file=row.get("name") or os.path.basename(row["local"]), other=os.path.basename(keep)))
            return
        super().revert_row(row, outcome, backup, log)  # (the copy swapped out for the snapshot's file: a link again, as it was)

    def needed_bytes(self, rows: list[dict]) -> int:
        return sum(int(r.get("size") or 0) for r in rows if not (r.get("remove") and r.get("removable")))  # (a link made a copy: a whole copy to write)

    def apply_async(self, ids: list[str], choices: dict | None = None) -> bool:
        sc = self.scan_result or {}
        if sc.get("remove") and self.mirrored(str(sc.get("library") or "")) is not None:  # (a mirror appeared since the scan)
            raise ValueError(M("Not on a synced library: scan again, and make each link a file of its own instead."))
        return super().apply_async(ids, choices)

    def apply_row(self, row: dict, log) -> dict:
        if row.get("remove") and row.get("removable"):
            gone = []
            for p in row["removable"]:
                self.file_action("unlink", p, to=row["local"])  # (only while row["local"] still holds the file)
                gone.append(os.path.basename(p))
                log(M("{path} removed: the file stays as {file}, where Plex has it", path=os.path.basename(p), file=row["name"]))
            self.app.activity.event("tools", M("{n} extra paths to {file} removed: {paths}", n=len(gone), file=row["name"], paths=", ".join(gone)))
            return {"result": "done", "removed": len(gone)}
        got = self.file_action("make_real", row["local"])
        log(M("{file} is a file of its own now ({size} written)", file=row["name"], size=got.get("bytes", row["size"])))
        if row.get("remove"):
            log(M("Every path to it is listed in Plex, so none was removed: merge the records with Merge Duplicates."))
        self.app.activity.event("tools", M("{file} made a file of its own (was a {kind})", file=row["name"], kind=row["kind"]))
        return {"result": "done", "bytes": got.get("bytes", 0)}

"""Unlink Files: a media file that's a link — a hard link (one file under two paths) or a symlink —
replaced by a file of its own, the same bytes. Why: Syncthing can't carry a hard link, so the other
site gets two whole copies that drift apart once either changes; a symlink is carried as a link, and
dangles there when its target isn't in the synced folder; and Plex lists only one of two paths to
the same bytes. Once every path is a file of its own, the folders sync alike everywhere, and
Merge Duplicates or Duplicate Versions can then tidy the copies safely.

The copy is made by the optimizer service (optimize/files.py make_real), under the same path, with
the file's own dates kept, so Plex and the folder sync see the same file as before.
"""

from __future__ import annotations

import os

from ..i18n import M
from . import MAX_ROWS, Tool
from ..paging import sort_key


def link_kind(local: str) -> tuple[str, int, str]:
    """("symlink" | "hardlink" | "", links, target) for a path."""
    try:
        if os.path.islink(local):
            return "symlink", 1, os.path.realpath(local)
        st = os.lstat(local)
        return ("hardlink", st.st_nlink, "") if st.st_nlink > 1 else ("", 1, "")
    except OSError:
        return "", 0, ""


class UnlinkFiles(Tool):
    name = "unlink"

    def scan(self, library: str, options: dict | None = None) -> dict:
        lib = self.app.library_info(library)
        if not lib:
            raise ValueError(M("No such library."))
        rows, links = [], 0
        files = [f for f in list(self.app.files) if str(f.library_key) == str(library)]
        for i, f in enumerate(files):
            if i % 50 == 0:
                self.step(i, len(files))
            local = self.app.map_path(f.path)
            kind, n, target = link_kind(local)
            if not kind:
                continue
            links += 1
            try:
                size = os.stat(local).st_size
            except OSError:
                size = getattr(f, "size", 0)
            rows.append({"id": "link:" + f.path, "library": str(library), "title": getattr(f, "title", "") or os.path.basename(f.path), "poster": getattr(f, "poster", ""),
                         "path": f.path, "local": local, "name": os.path.basename(f.path), "kind": kind, "links": n, "target": target, "size": size})
        rows.sort(key=lambda r: sort_key(r["title"]))
        return {"library": str(library), "rows": rows[:MAX_ROWS], "total": len(rows), "links": links, "mirror": {"name": self.mirror_name()} if self.mirror(library) is not None else None}

    def needed_bytes(self, rows: list[dict]) -> int:
        return sum(int(r.get("size") or 0) for r in rows)  # (each link becomes a whole copy)

    def apply_row(self, row: dict, log) -> dict:
        got = self.file_action("make_real", row["local"])
        log(M("{file} is a file of its own now ({size} written)", file=row["name"], size=got.get("bytes", row["size"])))
        self.app.activity.event("tools", M("{file} made a file of its own (was a {kind})", file=row["name"], kind=row["kind"]))
        return {"result": "done", "bytes": got.get("bytes", 0)}

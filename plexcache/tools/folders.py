"""Organize Folders: a film's files moved into a folder of their own, named as Plex recommends —
``Title (Year)``, with ``{edition-…}`` for an edition and, only when two films share a title and
year, ``{tmdb-…}`` to tell them apart. Loose files (in the library folder itself) by default; with
Clean Up, folders holding one film whose name doesn't carry its title and year are renamed too (a
folder the user named, with the title and year in it, is left as it is).

Plex never loses anything: each file is given its new path first (a hard link: the same file, no
copying), Plex asked to scan the new folder, and — once it lists the new path — the record it made for
it merged into the original (keeping its edits, poster, watched state), then the old path removed and
Plex asked to scan again. Sidecar files (subtitles, artwork) move along. With a mirrored server, the
synced folder carries the change there, and the same merge is done there once its Plex has the new
path (tick: followed until it has).
"""

from __future__ import annotations

import os
import re
import time

from ..i18n import M
from ..libhealth import file_title_year
from ..plex import PlexError
from . import MAX_ROWS, Tool

WAIT, POLL = 600, 10  # how long Plex gets to list a moved file, checked every POLL seconds
MIRROR_SCAN_EVERY, MIRROR_GIVE_UP = 15 * 60, 3 * 86400
SIDECAR = (".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".nfo", ".jpg", ".jpeg", ".png", ".tbn", ".xml", ".txt")
_BAD = re.compile(r'[\\/:*?"<>|]+')


def clean(s: str) -> str:
    """A name fit for a folder: the characters file systems refuse replaced, spaces tidied."""
    s = _BAD.sub(" ", s or "").replace("…", "").strip(" .")
    return re.sub(r"\s+", " ", s)


def folder_name(rec: dict) -> str:
    title = clean(rec.get("title") or "")
    name = f"{title} ({rec['year']})" if rec.get("year") else title
    if rec.get("edition"):
        name += " {edition-" + clean(rec["edition"]).replace("{", "").replace("}", "") + "}"
    return name


def id_tag(rec: dict) -> str:
    for g in rec.get("guids") or []:
        site, _, ident = g.partition("://")
        if site in ("tmdb", "imdb", "tvdb") and ident:
            return "{" + site + "-" + ident + "}"
    return ""


def _named_for(folder: str, rec: dict) -> bool:
    """Whether a folder's name carries the record's title and year (named by hand, and uniquely)."""
    ft, fy = file_title_year(folder)
    rt, _ = file_title_year(f"{rec.get('title') or ''} ({rec.get('year') or ''})")
    return bool(ft) and ft == rt and (not rec.get("year") or fy == str(rec["year"]))


class OrganizeFolders(Tool):
    name = "folders"

    # ------------------------------------------------------------ the plan
    def scan(self, library: str, options: dict | None = None) -> dict:
        cleanup = bool((options or {}).get("cleanup"))
        lib = self.app.library_info(library)
        if not lib or lib.get("type") != "movie":
            raise ValueError(M("Only movie libraries are organized: TV and music have folder layouts of their own."))
        recs = self.app.plex(timeout=120).library_items(lib["key"], 1)
        roots = sorted((loc.rstrip("/") for loc in lib.get("locations") or []), key=len, reverse=True)
        named: dict[str, list] = {}
        for r in recs:
            named.setdefault(folder_name(r).lower(), []).append(r)
        target = {}
        for rs in named.values():
            for r in rs:
                tag = id_tag(r) if len(rs) > 1 else ""
                target[r["key"]] = folder_name(r) + (" " + tag if tag else "")
        by_key = {r["key"]: r for r in recs}
        folder_recs: dict[tuple, set] = {}
        rows = []
        for r in recs:
            for path, _size in r.get("files") or []:
                root = next((x for x in roots if path.startswith(x + "/")), None)
                if not root:
                    continue
                rel = path[len(root) + 1:]
                if "/" in rel:
                    folder_recs.setdefault((root, rel.split("/")[0]), set()).add(r["key"])
                    continue
                to = f"{root}/{target[r['key']]}"
                moves = [{"from": path, "to": f"{to}/{os.path.basename(path)}", "media": True}]
                moves += [{"from": f"{root}/{n}", "to": f"{to}/{n}", "media": False} for n in self._sidecars(root, os.path.basename(path))]
                rows.append({"id": "loose:" + path, "library": str(library), "kind": "loose", "title": r.get("label") or r.get("title"), "rk": r["key"],
                             "ptitle": r.get("title") or "", "guid": r.get("guid") or "", "thumb": r.get("thumb"), "root": root,
                             "folder": target[r["key"]], "moves": moves})
        if cleanup:
            for (root, top), keys in folder_recs.items():
                if len(keys) != 1:
                    continue  # (several films in one folder: not a film's own folder)
                r = by_key[next(iter(keys))]
                want = target[r["key"]]
                if top == want or _named_for(top, r):
                    continue
                old = f"{root}/{top}"
                names = self._listing(old)
                moves = [{"from": f"{old}/{n}", "to": f"{root}/{want}/{n}", "media": any(f"{old}/{n}" == p for p, _ in r["files"])} for n in names]
                if not any(m["media"] for m in moves):
                    continue
                rows.append({"id": "folder:" + old, "library": str(library), "kind": "rename", "title": r.get("label") or r.get("title"), "rk": r["key"],
                             "ptitle": r.get("title") or "", "guid": r.get("guid") or "", "thumb": r.get("thumb"), "root": root,
                             "from_folder": top, "folder": want, "moves": moves})
        rows.sort(key=lambda x: (x["kind"] != "loose", str(x["title"]).lower()))
        there = self.mirror(library)
        return {"library": str(library), "rows": rows[:MAX_ROWS], "total": len(rows), "records": len(recs), "cleanup": cleanup,
                "folders": len(folder_recs), "mirror": {"name": self.mirror_name()} if there is not None else None}

    def _sidecars(self, root: str, media_name: str) -> list[str]:
        """Files next to a loose film that belong to it (its subtitles, artwork): the same stem."""
        stem = os.path.splitext(media_name)[0]
        try:
            names = os.listdir(self.app.map_path(root))
        except OSError:
            return []
        return sorted(n for n in names if n != media_name and n.startswith(stem) and n.lower().endswith(SIDECAR))

    def _listing(self, folder: str) -> list[str]:
        """A film's folder's files, with their paths within it (a subfolder's too)."""
        local = self.app.map_path(folder)
        out = []
        for dirpath, dirs, files in os.walk(local):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in files:
                if not n.startswith("."):
                    out.append(os.path.relpath(os.path.join(dirpath, n), local))
        return sorted(out)

    # ------------------------------------------------------------ doing it
    def apply_row(self, row: dict, log) -> dict:
        library, rk, moves = row["library"], row["rk"], row["moves"]
        plex = self.app.plex()
        for mv in moves:
            self.file_action("link", self.app.map_path(mv["from"]), to=self.app.map_path(mv["to"]))
        log(M("{n} files given their new path in {folder} (the same files: nothing copied)", one="{n} file given its new path in {folder} (the same file: nothing copied)",
              n=len(moves), folder=row["folder"]))
        new_folder = f"{row['root']}/{row['folder']}"
        plex.scan(library, new_folder)
        media = {mv["to"] for mv in moves if mv["media"]}
        holders = self._wait_for(plex, library, row["ptitle"], media)
        if not holders:
            for mv in moves:  # undone: the new paths removed again, the old ones untouched
                try:
                    self.file_action("unlink", self.app.map_path(mv["to"]), to=self.app.map_path(mv["from"]))
                except ValueError:
                    pass
            raise ValueError(M("Plex didn’t list the new folder within {n} minutes: the new paths were removed again and nothing changed.", n=WAIT // 60))
        log(M("Plex lists the new path"))
        new_keys = sorted({k for k in holders.values() if k != rk})
        if new_keys:
            plex.merge(rk, new_keys)
            log(M("Plex made a new record for it: merged into the original (its edits, poster and watched state kept)"))
        for mv in moves:
            self.file_action("unlink", self.app.map_path(mv["from"]), to=self.app.map_path(mv["to"]))
        old_folders = sorted({os.path.dirname(mv["from"]) for mv in moves})
        for folder in old_folders:
            plex.scan(library, folder)
        try:
            plex.empty_trash(library)
        except PlexError:
            pass
        log(M("The old paths removed, and Plex asked to scan again"))
        self.app.activity.event("tools", M("{title} moved into {folder}", title=row["title"], folder=row["folder"]))
        there = self.mirror(library)
        if there is not None:
            with self.lock:
                self.extra.setdefault("pending", []).append({"library": library, "title": row["title"], "folder": row["folder"], "at": time.time(), "scanned": 0.0,
                                                            "old": [mv["from"] for mv in moves if mv["media"]], "new": sorted(media)})
            log(M("{name}: followed until its Plex has the new path (its synced folder brings the files)", name=self.mirror_name()))
        return {"result": "done", "moved": len(moves), "merged": bool(new_keys)}

    def _wait_for(self, plex, library: str, title: str, paths: set) -> dict:
        """Until Plex lists every one of ``paths``: {path: the record holding it}."""
        deadline = time.time() + WAIT
        while True:
            try:
                recs = plex.items_titled(library, 1, title)
            except PlexError:
                recs = []
            got = {p: r["key"] for r in recs for p, _ in r.get("files") or [] if p in paths}
            if len(got) == len(paths):
                return got
            if time.time() >= deadline:
                return {}
            time.sleep(POLL)

    # ------------------------------------------------------------ the mirror, followed
    def waiting(self) -> bool:
        return bool(self.extra.get("pending"))

    def tick(self) -> None:
        other, now, changed = self.app.other, time.time(), False
        for e in list(self.extra.get("pending") or []):
            there = self.mirror(e["library"])
            if there is None:
                continue
            try:
                idx = other.index(there, fresh=True)
                new = [c for p in e["new"] for c in other.paths_there(e["library"], p, there) if c in idx]
                old = [c for p in e["old"] for c in other.paths_there(e["library"], p, there) if c in idx]
                if new:
                    keep = {idx[c] for c in old}
                    extra = {idx[c] for c in new} - keep
                    if keep and extra:
                        k = sorted(keep)[0]
                        other.run(lambda c: c.merge(k, sorted(extra)))
                        self.app.activity.event("tools", M("{title}: its new record on {name} merged into the old one", title=e["title"], name=self.mirror_name()))
                    if not old:
                        self._done(e, M("{name} has it at its new path", name=self.mirror_name()) if keep or not extra else
                                   M("{name} has it at its new path, as a new record (the old one had gone first)", name=self.mirror_name()))
                        changed = True
                        continue
                if now - e["scanned"] >= MIRROR_SCAN_EVERY:
                    e["scanned"] = now
                    other.run(lambda c: c.scan(there.key, next(iter(other.paths_there(e["library"], e["new"][0], there)), "").rsplit("/", 1)[0]))
                    changed = True
                if now - e["at"] > MIRROR_GIVE_UP and not e.get("stuck"):
                    e["stuck"] = True
                    changed = True
            except PlexError as err:
                e["note"] = str(err)
                changed = True
        if changed:
            self.save()

    def _done(self, e: dict, how: str) -> None:
        with self.lock:
            self.extra["pending"] = [x for x in self.extra.get("pending") or [] if x is not e]
            self.results.append({"result": "done", "id": "mirror:" + e["folder"], "title": e["title"], "at": time.time(), "steps": [how], "mirror": True})

    def stuck(self) -> list[dict]:
        return [dict(e) for e in self.extra.get("pending") or [] if e.get("stuck")]

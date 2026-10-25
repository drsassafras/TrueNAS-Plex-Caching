"""Merge Duplicates: the same film (or show) on several Plex records, made versions of one record
with Plex's own Merge, which keeps the record's metadata — its edits, poster, collections, watched
state — while the others' are lost. Why: a copy added before the other was matched, a second file in
another folder, a re-added film — each ends up as a record of its own, with its own poster and
history, and twice in every list.

Two records are the same film when they hold the very same file (a link or a copy in another folder:
the same size and running time), whatever they're matched to — or when they share a match, or,
unmatched, when their files are named for the same title and year — and their running times agree (LEN_PCT / LEN_SECS: a little leeway for a
different frame rate or cut of the credits; two films that merely share a title and year differ by
more) — and they're the same edition (a Director's Cut and the theatrical cut are two records on
purpose), unless their files are the very same file. The record kept is the one with the most edited
fields, then the most files, then the oldest. Only this server's records: a merge changes nothing on
a mirrored server (no file is touched), so its own duplicates are its own matter — or Library Sync's, which
merges them there when this server is its source (sync.py: one item here, several there).
"""

from __future__ import annotations

import os
import re

from ..i18n import M
from ..libhealth import file_title_year
from ..sync import _compare_pair, side
from . import MAX_ROWS, Tool
from ..libdata import link_of
from ..paging import sort_key

TOP_TYPE = {"movie": 1, "show": 2}  # the records that get duplicated: films, and shows
LEN_PCT, LEN_SECS = 4, 30  # running times may differ this much (whichever is more) and still be one film
WHAT = {"grouping": False, "matches": True, "edits": True, "art": True, "collections": True, "editions": True}


def group_key(rec: dict) -> str | None:
    """What makes two records the same film: their match; unmatched, the title and year their files
    are named for (or Plex's)."""
    if rec.get("matched") and rec.get("guid"):
        return "guid:" + rec["guid"]
    files = rec.get("files") or []
    if files:
        f = files[0][0] if isinstance(files[0], (list, tuple)) else files[0]
        title, year = file_title_year(f)
    else:
        title, year = (rec.get("title") or "").strip().lower(), ""
    year = year or str(rec.get("year") or "")
    return f"name:{title}|{year}" if title else None


def _files(rec: dict) -> list[tuple]:
    """A record's files as (size, running time) — what tells the very same file."""
    sizes = [f[1] if isinstance(f, (list, tuple)) else 0 for f in rec.get("files") or []]
    durs = list(rec.get("durations") or [])
    return [(s, durs[i] if i < len(durs) else 0) for i, s in enumerate(sizes)]


def same_file(a: tuple, b: tuple) -> bool:
    """The very same bytes: the same size, and the running time Plex read from it (to the half second)."""
    return bool(a[0] and a[0] == b[0] and abs(a[1] - b[1]) <= 500)


def plain_title(rec: dict) -> str:
    """A record's title as Plex has it, lower-cased and without punctuation."""
    return re.sub(r"[\W_]+", " ", str(rec.get("title") or "")).strip().lower()


def duplicates(a: dict, b: dict) -> bool:
    """Whether two records of one group are one film: running times that agree, the same edition, and
    the same title — two records matched to one entry yet titled apart were told apart on purpose: a
    work released in parts, Henry IV Part 1 and Part 2, each its own record — unless they hold the
    very same file."""
    identical = any(same_file(x, y) for x in _files(a) for y in _files(b))
    if identical:
        return True
    if plain_title(a) and plain_title(b) and plain_title(a) != plain_title(b):
        return False
    da, db = [d for d in a.get("durations") or [] if d], [d for d in b.get("durations") or [] if d]
    if da and db:
        la, lb = max(da), max(db)
        if abs(la - lb) > max(LEN_SECS * 1000, max(la, lb) * LEN_PCT / 100):
            return False
    return (a.get("edition") or "") == (b.get("edition") or "")


def keep_rank(rec: dict) -> tuple:
    key = rec.get("key") or "0"
    return (len(rec.get("locked") or []), len(rec.get("files") or []), -int(key) if str(key).isdigit() else 0)


def clusters(recs: list[dict]) -> list[tuple[str, list[dict]]]:
    """A library's records gathered into the films that are on more than one record: [(why, records)],
    why "file" (they hold the very same file), "matched" (the same match) or "named" (unmatched, named
    for the same title and year) — union-find over the pairs that are duplicates()."""
    parent = list(range(len(recs)))
    why: dict[int, str] = {}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j, how):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
        if how == "file" or rj not in why:
            why[rj] = how
    # the same file on two records: by size (then the running time), whatever they're matched to
    by_size: dict[int, list] = {}
    for i, r in enumerate(recs):
        for size, _ in _files(r):
            if size:
                by_size.setdefault(size, []).append(i)
    for same in by_size.values():
        for a in range(len(same)):
            for b in range(a + 1, len(same)):
                i, j = same[a], same[b]
                if i != j and any(same_file(x, y) for x in _files(recs[i]) for y in _files(recs[j])):
                    union(i, j, "file")
    # the same match, or the same name: with running times that agree, and the same edition
    groups: dict[str, list] = {}
    for i, r in enumerate(recs):
        k = group_key(r)
        if k:
            groups.setdefault(k, []).append(i)
    for k, members in groups.items():
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                i, j = members[a], members[b]
                if duplicates(recs[i], recs[j]):
                    union(i, j, "matched" if k.startswith("guid:") else "named")
    out: dict[int, list] = {}
    for i, r in enumerate(recs):
        out.setdefault(find(i), []).append(r)
    return [(why.get(root, "named"), c) for root, c in out.items() if len(c) > 1]


class MergeDuplicates(Tool):
    name = "merge"
    scan_version = 3  # (rows hold whole records now, as the Sync view shows them; the scan says the mirror, for the box before Apply)

    title = M("Merge Duplicates")
    page = "/merge"
    applies = ("movie", "tv")
    options = [{"id": "file", "name": M("The same file on two records"), "help": M("Two records hold the very same file — a link, or a copy in another folder with the same size and running time — whatever each is matched to."), "kinds": ("movie", "tv"), "default": True},
               {"id": "matched", "name": M("Two records with the same match"), "help": M("Two records Plex matched to the same film or show, with running times that agree and the same edition."), "kinds": ("movie", "tv"), "default": True},
               {"id": "named", "name": M("Unmatched records named alike"), "help": M("Two unmatched records whose files are named for the same title and year, with running times that agree."), "kinds": ("movie", "tv"), "default": True}]
    flag_help = M("The same film or show on several Plex records (the same match, or files named for the same title and year): twice in every list, two posters, watched on one but not the other. Merge Duplicates makes them one record with versions, keeping its edits, poster and watched state.")

    def flag_lines(self, rows):
        return [M("{n} records of {title}: {why}", n=len(r.get("others") or []) + 1, title=r.get("title") or "", why={"identical": M("the very same file"), "matched": M("the same match"), "named": M("named for the same title and year")}.get(r.get("why"), r.get("why") or "")) for r in rows]

    def scan(self, data) -> dict:
        library = data.key
        lib = self.app.library_info(library)
        if not lib or lib.get("type") not in TOP_TYPE:
            raise ValueError(M("Only movie and TV libraries have records to merge."))
        recs = data.records
        rows = []
        found = clusters(recs)
        for i, (why, cluster) in enumerate(found):
            if i % 20 == 0:
                self.step(i, len(found))
            cluster.sort(key=keep_rank, reverse=True)
            keep, others = cluster[0], cluster[1:]
            kinds: list[str] = []
            changes: list[dict] = []
            for o in others:  # what the folded records have that the kept one hasn't: lost in the merge
                _compare_pair(o, keep, WHAT, lambda kind, _a, frm, to, _b=None: (kinds.append(kind) if kind not in kinds else None, changes.append({"kind": kind, "from": str(frm), "to": str(to)})))
            linked = []  # files of these records that are links: merging them is unsafe until each is a file of its own
            for rec in cluster:
                for path, _ in rec.get("files") or []:
                    k, n, _t = link_of(data.file(self.app.map_path(path)))
                    if k:
                        linked.append({"name": os.path.basename(path), "kind": k, "links": n})
            rows.append({"id": "rec:" + keep["key"], "library": str(library), "title": keep.get("label") or keep.get("title"), "rks": [r["key"] for r in cluster],
                         "why": why, "issues": [why], "keep": side(keep), "others": [side(o) for o in others], "kinds": kinds, "changes": changes, "linked": linked})
        rows.sort(key=lambda r: sort_key(r["title"]))
        return {"library": str(library), "rows": rows[:MAX_ROWS], "total": len(rows), "records": len(recs),
                "mirror": self.mirrored(library)}  # (what the page says before Apply: how a mirror's records follow, if they can)

    history = [("merged", M("Records merged")), ("kept", M("Record kept")), ("why", M("Why"))]

    def history_row(self, row: dict, outcome: dict) -> dict:
        chosen = str(row.get("keep_key") or (row.get("keep") or {}).get("key") or "")
        kept = next((r for r in [row.get("keep")] + list(row.get("others") or []) if r and str(r.get("key")) == chosen), row.get("keep") or {})
        return {"merged": int(outcome.get("merged") or 0) + 1, "kept": kept.get("label") or kept.get("title") or chosen,
                "why": {"identical": M("the very same file"), "matched": M("the same match"), "named": M("named for the same title and year")}.get(row.get("why"), row.get("why") or "")}

    def plex_keys(self, rows: list[dict]) -> list[str]:
        return [str(k) for r in rows for k in r.get("rks") or []]

    def revert_row(self, row: dict, outcome: dict, backup: dict, log) -> None:
        """The kept record split apart again (Plex's Split Apart): records of their own — and, from the backup of the records
        the run made first (plexbackup.py), each given its own metadata back. With no such backup, matched afresh: the
        folded ones' own edits are gone with the merge."""
        from .. import plexbackup
        chosen = str(row.get("keep_key") or row["keep"]["key"])
        if (backup.get("plex") or {}).get("id"):
            got = plexbackup.revert(self.app, backup["plex"]["id"], keys=[chosen])
            if got["failed"] and not got["restored"]:
                raise ValueError(M("{title} not put back: {why}", title=row.get("title") or chosen, why="; ".join(got["left"])))
            log(M("{title} split into records of its own again, each with its own metadata back from the backup", title=row.get("title") or chosen))
            for why in got["left"]:
                log(why)
            return
        self.app.plex().split(chosen)
        log(M("{title} split into records of its own again (the folded records’ edits aren’t back: Plex matches them afresh)", title=row.get("title") or chosen))

    def apply_row(self, row: dict, log) -> dict:
        records = [row["keep"]] + list(row.get("others") or [])
        chosen = str(row.get("keep_key") or row["keep"]["key"])  # (the arrow on the page: another record kept instead)
        keep = next((r for r in records if str(r["key"]) == chosen), row["keep"])
        others = [r["key"] for r in records if r is not keep]
        self.app.plex().merge(keep["key"], others)
        log(M("{n} records merged into {title}", one="{n} record merged into {title}", n=len(others), title=keep["label"]))
        self.app.activity.event("tools", M("merged {n} records into {title}", n=len(others) + 1, title=keep["label"]))
        return {"result": "done", "merged": len(others)}

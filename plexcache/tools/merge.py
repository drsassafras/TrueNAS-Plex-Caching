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
a mirrored server (no file is touched), so its own duplicates are its own matter.
"""

from __future__ import annotations

from ..i18n import M
from ..libhealth import file_title_year
from ..sync import _compare_pair, side
from . import MAX_ROWS, Tool

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


def duplicates(a: dict, b: dict) -> bool:
    """Whether two records of one group are one film: running times that agree, and the same edition
    (unless they hold the very same file)."""
    identical = any(same_file(x, y) for x in _files(a) for y in _files(b))
    if identical:
        return True
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
    scan_version = 2  # (rows hold whole records now, as the Sync view shows them)

    def scan(self, library: str, options: dict | None = None) -> dict:
        lib = self.app.library_info(library)
        if not lib or lib.get("type") not in TOP_TYPE:
            raise ValueError(M("Only movie and TV libraries have records to merge."))
        leaf = TOP_TYPE[lib["type"]]
        recs = self.app.plex(timeout=120).library_items(lib["key"], leaf)
        rows = []
        for why, cluster in clusters(recs):
            cluster.sort(key=keep_rank, reverse=True)
            keep, others = cluster[0], cluster[1:]
            kinds: list[str] = []
            changes: list[dict] = []
            for o in others:  # what the folded records have that the kept one hasn't: lost in the merge
                _compare_pair(o, keep, WHAT, lambda kind, _a, frm, to, _b=None: (kinds.append(kind) if kind not in kinds else None, changes.append({"kind": kind, "from": str(frm), "to": str(to)})))
            rows.append({"id": "rec:" + keep["key"], "library": str(library), "title": keep.get("label") or keep.get("title"),
                         "why": why, "keep": side(keep), "others": [side(o) for o in others], "kinds": kinds, "changes": changes})
        rows.sort(key=lambda r: str(r["title"]).lower())
        return {"library": str(library), "rows": rows[:MAX_ROWS], "total": len(rows), "records": len(recs)}

    def apply_row(self, row: dict, log) -> dict:
        keep, others = row["keep"], [o["key"] for o in row.get("others") or []]
        self.app.plex().merge(keep["key"], others)
        log(M("{n} records merged into {title}", one="{n} record merged into {title}", n=len(others), title=keep["label"]))
        self.app.activity.event("tools", M("merged {n} records into {title}", n=len(others) + 1, title=keep["label"]))
        return {"result": "done", "merged": len(others)}

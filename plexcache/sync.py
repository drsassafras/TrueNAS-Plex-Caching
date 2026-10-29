"""The mirror: another Plex server with the same files — at another site, kept in step by syncing the folders — and
how its libraries are compared with this server's.

* Mirrors: whether a library here is mirrored is a choice per library (plex2.py: not decided, none, or a library of
  the server watched, the Mirror page's Mirrored Libraries). Until it's made, Health asks, and what would act on it
  (deleting converted originals) waits. discover() looks for mirrors among the account's servers: for each of its
  libraries, a sample of its files looked up here by path, scored against each library here of the kind — so the
  page can say, per library here, which library where looks like its mirror (by_library, suspected).
* Another Plex Cache: a system sharing the library folders marks itself there (optimize/claims.py): seen by peers(),
  so a Plex Cache at the other site is known without any setup.
* compare_library(): a library on two servers — read by the data layer (libdata.py: the mirror's side) — items matched
  across them by their files' place in the library folder (the same on both: the folders are synced), else by online
  entry; shows, seasons, artists and albums through their episodes and tracks — and the differences listed, source →
  the other: files the other doesn't list (it needs a scan), versions grouped differently, another online match,
  edited fields, posters chosen by hand, collections, labels, editions. Watched state and ratings aren't compared:
  Plex's own sync carries them. Sync Libraries (tools/sync.py) brings the other in line with what it finds; what it
  can't is Mirror Checks' (mirrorhealth.py).
"""

import os
import threading
import time
import unicodedata

from .i18n import M
from .plex import EDIT_FIELDS, PlexClient, PlexError, reachable, servers
from .optimize.claims import PEER_DAYS
from .paging import sort_key

STORE = "sync-report.json"  # (the servers found: named from when a compare's report was kept in it too)
MAX_ITEMS = 1000  # differences listed per library by default (the counts are whole)
SAMPLE = 300  # of a candidate mirror's files looked up here, per library
MIRROR_RATE = 0.9  # the share of the sample found here for a library to look like a mirror
KINDS = ("missing", "extra", "grouping", "match", "edits", "art", "collections", "labels", "editions")
LEAVES = {"movie": (1,), "show": (2, 3, 4), "artist": (8, 9, 10)}
NAMES = {"missing": M("Not listed there"), "extra": M("Only on the other server"), "grouping": M("Versions grouped differently"), "match": M("Matched to another entry"),
         "edits": M("Edited fields"), "art": M("Posters chosen by hand"), "collections": M("Collections"), "labels": M("Labels"),
         "editions": M("Editions")}


def _norm(rel: str) -> str:
    """A file's place in the folder, as compared: one Unicode form (macOS and Syncthing can write
    accents decomposed, Linux keeps them composed) and one case — the same file either way."""
    return unicodedata.normalize("NFC", rel).casefold()


class _Side:
    """One server's library, read: its items by key, and its leaf items by their file's place in the folder."""

    def __init__(self, items: list[dict], locations: list[str]):
        self.items = {it["key"]: it for it in items}
        self.by_rel: dict[str, str] = {}  # relative path (_norm) -> item key
        self.by_name: dict[str, set[str]] = {}  # file name (_norm) -> the item keys with a file by that name
        self.shown: dict[str, str] = {}  # relative path (_norm) -> as it's written
        self.children: dict[str, list[str]] = {}
        locs = sorted((loc.rstrip("/") for loc in locations), key=len, reverse=True)
        for it in items:
            for path, _ in it["files"]:
                for loc in locs:
                    if path.startswith(loc + "/"):
                        rel = path[len(loc) + 1:]
                        self.by_rel[_norm(rel)] = it["key"]
                        self.shown[_norm(rel)] = rel
                        break
                self.by_name.setdefault(_norm(os.path.basename(path)), set()).add(it["key"])
            for up in (it["parent"], it["grandparent"]):
                if up:
                    self.children.setdefault(up, []).append(it["key"])

    def rels(self, key: str) -> set[str]:
        """A leaf item's files, as their place in the folder."""
        return {rel for rel, k in self.by_rel.items() if k == key}


def side(it: dict) -> dict:
    """One server's item, as the page shows it beside the other's."""
    return {"key": it["key"], "label": it["label"], "year": it.get("year", ""), "matched": it["matched"], "guid": it["guid"], "type": it.get("type", ""), "art_path": it.get("art_path", ""),
            "summary": it.get("summary", False), "thumb": it.get("thumb", ""), "fields": dict(it["fields"]), "values": dict(it.get("values") or {}), "locked": list(it.get("locked") or []), "art": list(it["art_locked"]),
            "files": [os.path.basename(f) for f, _ in it["files"]], "paths": [f for f, _ in it["files"]], "durations": list(it.get("durations") or []),
            "summary_text": it.get("summary_text", ""), "tags": dict(it.get("tags") or {}),
            "collections": list(it["collections"]), "labels": list(it["labels"]), "edition": it.get("edition", "")}


def _empty(it: dict) -> bool:
    """An item with next to no metadata: unmatched, and no description (Plex found nothing for it)."""
    return not it["matched"] and not it.get("summary")


def compare_library(src: _Side, dst: _Side, what: dict, checks_on: dict | None = None, limit: int | None = MAX_ITEMS) -> dict:
    """The differences between a library on the source and on the other server: {"counts": {kind: n},
    "items": [rows], "matched": items found on both, "total"}. Items sharing files across the two
    servers (a version's file on one item here, another there) form one group, and each group is a row:
    {"title", "shape": "pair" | "split" | "merge" | "regroup" | "missing" | "extra", "kinds", "changes":
    [{"kind", "from", "to"}], "src_items": [sides], "dst_items": [sides], "src", "dst" (the first of each),
    "warn", "tags": {"src": [[checks]…], "dst": [[checks]…]} — the Plex checks (libhealth) on each record,
    marking data that looks bad before anything would be copied}. "split": one item on the source is
    several there (applying merges them); "merge": several here are one there (applying splits it);
    "regroup": several on both sides, tangled — shown as the records on each side, with no copy worked out. ``limit``: the
    rows listed (None: every one)."""
    from .libhealth import plex_checks
    on = checks_on or {}
    counts = dict.fromkeys(KINDS, 0)
    rows: list[dict] = []
    pairs: dict[str, str] = {}  # source key -> other key (1:1 groups, and parents)

    def tags(items):
        return [plex_checks({"files": [r for r in sd_rels], "durations": it.get("durations") or [], "locked": it.get("locked") if it.get("locked") is not None else None,
                             "fields": it.get("fields"), "tags": it.get("tags")}, on) for it, sd_rels in items]

    def row(shape, src_items, dst_items, src_side, dst_side):
        r = {"id": ("s" + src_items[0]["key"]) if src_items else ("d" + dst_items[0]["key"]),  # (stable across compares: what Apply names)
             "title": (src_items or dst_items)[0]["label"], "shape": shape, "kinds": [], "changes": [],
             "src_items": [side(it) for it in src_items], "dst_items": [side(it) for it in dst_items],
             "src": side(src_items[0]) if src_items else None, "dst": side(dst_items[0]) if dst_items else None,
             "warn": bool(src_items and dst_items and _empty(src_items[0]) and not _empty(dst_items[0])),
             "tags": {"src": tags([(it, src_side.rels(it["key"])) for it in src_items]), "dst": tags([(it, dst_side.rels(it["key"])) for it in dst_items])}}
        return r

    def add(r, kind, frm, to):
        counts[kind] += 1
        if kind not in r["kinds"]:
            r["kinds"].append(kind)
        r["changes"].append({"kind": kind, "from": str(frm), "to": str(to)})
    # the groups: items on either server joined by the files they share (their place in the folder)
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for rel, k in src.by_rel.items():
        union(("s", k), ("r", rel))
    for rel, k in dst.by_rel.items():
        union(("d", k), ("r", rel))
    for k, it in src.items.items():
        if it["files"]:
            find(("s", k))
    for k, it in dst.items.items():
        if it["files"]:
            find(("d", k))
    groups: dict = {}
    for node in list(parent):
        g = groups.setdefault(find(node), {"s": [], "d": []})
        if node[0] in ("s", "d"):
            g[node[0]].append(node[1])
    # an item not there by its files (named otherwise there) pairs by its online entry, when that's one
    # item there that is likewise only on that side
    solo_d = {dst.items[g["d"][0]]["guid"]: gk for gk, g in groups.items() if g["d"] and not g["s"] and len(g["d"]) == 1 and dst.items[g["d"][0]]["matched"]}
    for gk, g in list(groups.items()):
        if g["s"] and not g["d"] and len(g["s"]) == 1:
            it = src.items[g["s"][0]]
            other = solo_d.get(it["guid"]) if it["matched"] else None
            if other and other != gk and other in groups:
                g["d"] = groups.pop(other)["d"]
                solo_d.pop(it["guid"], None)
    for g in groups.values():
        S = sorted((src.items[k] for k in g["s"]), key=lambda it: sort_key(it["label"]))
        D = sorted((dst.items[k] for k in g["d"]), key=lambda it: sort_key(it["label"]))
        if S and not D:
            for it in S:
                r = row("missing", [it], [], src, dst)
                if what.get("grouping", True):
                    add(r, "missing", ", ".join(sorted(os.path.basename(x) for x in src.rels(it["key"]))[:3]), M("not listed: the folder needs a scan there"))
                    rows.append(r)
            continue
        if D and not S:
            if what.get("grouping", True):
                r = row("extra", [], D, src, dst)
                add(r, "extra", M("nothing"), M("{n} items only on the other server: nothing is copied; they stay", n=len(D)))
                rows.append(r)
            continue
        if len(S) == 1 and len(D) == 1:
            it, o = S[0], D[0]
            pairs[it["key"]] = o["key"]
            r = row("pair", [it], [o], src, dst)
            if what.get("grouping", True):
                rels, orels = src.rels(it["key"]), dst.rels(o["key"])
                if rels - orels:
                    add(r, "missing", ", ".join(sorted(os.path.basename(x) for x in rels - orels)[:3]), M("not listed: the folder needs a scan there"))
                if orels - rels:
                    add(r, "grouping", M("{n} files", n=len(rels)), M("{n} files: {more} more grouped with it — they stay", n=len(orels), more=len(orels - rels)))
            _compare_pair(it, o, what, lambda kind, _it, frm, to, _o=None: add(r, kind, frm, to))
            if r["kinds"]:
                rows.append(r)
            continue
        if not what.get("grouping", True):
            continue
        shape = "split" if len(S) == 1 else "merge" if len(D) == 1 else "regroup"
        r = row(shape, S, D, src, dst)
        if shape == "split":
            add(r, "grouping", M("one item, {n} files", n=len(src.rels(S[0]["key"]))), M("{n} items: applying would merge them into one", n=len(D)))
        elif shape == "merge":
            add(r, "grouping", M("{n} items", n=len(S)), M("one item, {n} files: applying would split it into {m}", n=len(dst.rels(D[0]["key"])), m=len(S)))
        else:
            add(r, "grouping", M("{n} items", n=len(S)), M("{n} items, sharing files in another way: too tangled for a copy to be worked out — look at both sides", n=len(D)))
        rows.append(r)
    # parents (shows, seasons, artists, albums): the item there that most of their matched children belong to
    for key, it in src.items.items():
        if it["files"]:
            continue
        votes: dict[str, int] = {}
        for k in src.children.get(key) or []:
            o = dst.items.get(pairs.get(k, ""))
            if o:
                up = o["parent"] if it["type"] in ("season", "album") else o["grandparent"] or o["parent"]
                if up:
                    votes[up] = votes.get(up, 0) + 1
        if not votes:
            continue
        other = dst.items.get(max(votes, key=votes.__getitem__))
        if other:
            pairs[key] = other["key"]
            r = row("pair", [it], [other], src, dst)
            _compare_pair(it, other, what, lambda kind, _it, frm, to, _o=None: add(r, kind, frm, to))
            if r["kinds"]:
                rows.append(r)
    rows.sort(key=lambda r: sort_key(r["title"]))
    return {"counts": counts, "items": rows[:limit] if limit else rows, "matched": len(pairs), "total": len(src.items)}


def _compare_pair(a: dict, b: dict, what: dict, add) -> None:
    unmatched = M("unmatched")
    # (both unmatched: each server's own local:// id, never the same — not a difference)
    if what.get("matches", True) and a["guid"] and b["guid"] and a["guid"] != b["guid"] and (a["matched"] or b["matched"]):
        add("match", a, a["guid"] if a["matched"] else unmatched, b["guid"] if b["matched"] else unmatched, b)
    if what.get("edits", True):
        for f in EDIT_FIELDS:
            if f in a["fields"] and (f not in b["fields"] or a["fields"][f] != b["fields"][f]):
                add("edits", a, f"{f}: {a['fields'][f]}", f"{f}: {b['fields'].get(f, '')}" + ("" if f in b["fields"] else " " + str(M("(not edited)"))), b)
        for f in a.get("locked") or []:  # edited tag fields (genres, writers…): the edit itself, no values to compare
            if f not in EDIT_FIELDS and f not in (b.get("locked") or []):
                add("edits", a, f"{f}: " + str(M("edited")), f"{f}: " + str(M("not edited")), b)
        # the other server's edits the source doesn't have: applying the source's would undo them
        lost = [f for f in (b.get("locked") or []) if f not in (a.get("locked") or [])]
        if lost:
            add("edits", a, M("not edited: {fields}", fields=", ".join(lost)), M("edited: {fields} — these edits would be lost", fields=", ".join(lost)), b)
    if what.get("art", True) and a["art_locked"] and a["art_locked"] != b["art_locked"]:
        add("art", a, M("chosen by hand: {which}", which=", ".join(a["art_locked"])), M("chosen by hand: {which}", which=", ".join(b["art_locked"])) if b["art_locked"] else M("Plex’s own"), b)
    if what.get("collections", True) and a["collections"] != b["collections"]:
        add("collections", a, ", ".join(a["collections"]) or "—", ", ".join(b["collections"]) or "—", b)
    if what.get("collections", True) and a["labels"] != b["labels"]:
        add("labels", a, ", ".join(a["labels"]) or "—", ", ".join(b["labels"]) or "—", b)
    if what.get("editions", True) and a["files"] and (a["edition"] or "") != (b["edition"] or ""):
        add("editions", a, a["edition"] or "—", b["edition"] or "—", b)


DETAIL_KEYS = ("fields", "locked", "art_locked", "summary", "summary_text", "tags", "thumb", "edition", "collections", "labels")


class Sync:
    """The mirror choice (settings "sync": mirrors), the servers that may be one (discover), and the other Plex Caches seen
    in the library folders (peers)."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        saved = app._load_json(STORE) or {}
        self.candidates: list[dict] = saved.get("candidates") or []
        self.discovered_at: float = saved.get("discovered_at") or 0.0
        self.running = False  # looking for mirrors
        self.error: str | None = None

    def save(self) -> None:
        with self.lock:
            data = {"candidates": self.candidates, "discovered_at": self.discovered_at}
        self.app._save_json(STORE, data)

    def settings(self) -> dict:
        return self.app.settings.section("sync")

    # ------------------------------------------------------------ mirrors
    def by_library(self) -> dict[str, dict]:
        """What discovery found for each library here: the library found that looks most like its mirror — {our key: {"id",
        "name", "url", "their_key", "their_title", "found", "of", "rate"}} — from every server found (the one watched too)."""
        out: dict[str, dict] = {}
        for c in self.candidates:
            for lib in c.get("libraries") or []:
                for key, found in (lib.get("matches") or {}).items():
                    of = int(lib.get("of") or 0)
                    rate = found / of if of else 0.0
                    got = {"id": c.get("id") or "", "name": c.get("name") or "", "url": c.get("url") or "", "their_key": lib.get("key") or "",
                           "their_title": lib.get("title") or "", "found": found, "of": of, "rate": rate}
                    if key not in out or rate > out[key]["rate"]:
                        out[key] = got
        return out

    def suspected(self, key: str) -> dict | None:
        """A library found that looks like the mirror of library ``key`` (nearly all of a sample of its files here), while
        the library's state isn't chosen — what Health, every tool and converting a folder to a child dataset go by. None
        once a state is chosen, or nothing looks like one."""
        if self.app.other.state_of(str(key)) != "unset":
            return None
        got = self.by_library().get(str(key))
        return got if got and got["rate"] >= MIRROR_RATE else None

    def peers(self) -> list[str]:
        """Other Plex Caches seen in the library folders (their markers, within PEER_DAYS)."""
        mine = self.app.optimizing.instance()
        found, now = set(), time.time()
        for lib in getattr(self.app, "libraries", []) or []:
            for loc in lib.get("locations") or []:
                sdir = os.path.join(self.app.map_path(loc), ".plexcache", "systems")
                try:
                    names = os.listdir(sdir)
                except OSError:
                    continue
                for n in names:
                    if n.endswith(".json") and n[:-5] != mine:
                        try:
                            if now - os.stat(os.path.join(sdir, n)).st_mtime < PEER_DAYS * 86400:
                                found.add(n[:-5])
                        except OSError:
                            pass
        return sorted(found)

    def discover(self) -> list[dict]:
        """The account's other servers, each with how many of a sample of its files are here, by library — scored
        against each library here of the kind: [{"id", "name", "url", "libraries": [{"key", "title", "kind", "found" (the best
        library here), "of", "matches": {our key: found}}], "rate"}]."""
        token = self.app.settings.section("plex2").get("account_token") or self.app.settings.section("plex").get("token")
        if not token:
            raise ValueError(M("Sign in with Plex first (System → Plex)."))
        mine_id = self.app.other._main_id()
        mine = {self.app.map_path(f.path) for f in list(self.app.files)}
        libs = {str(lib["key"]): lib for lib in getattr(self.app, "libraries", []) or []}
        out = []
        for sv in servers(self.app.settings.section("general")["client_id"], token):
            if sv["id"] == mine_id:
                continue
            url = reachable(sv)
            cand = {"id": sv["id"], "name": sv["name"], "url": url or "", "libraries": [], "rate": None}
            if url:
                try:
                    client = PlexClient(url, sv["token"], timeout=60)
                    found = of = 0
                    for there in client.libraries():
                        if not there.kind:
                            continue
                        ours = [lib for lib in libs.values() if lib.get("kind") == there.kind]
                        files = client.library_files(there, limit=SAMPLE)
                        matches = {str(lib["key"]): 0 for lib in ours}
                        for f in files:
                            rel = next((f.path[len(loc.rstrip("/")) + 1:] for loc in there.locations if f.path.startswith(loc.rstrip("/") + "/")), None)
                            if rel is None:
                                continue
                            for lib in ours:
                                if any(self.app.map_path(loc.rstrip("/") + "/" + rel) in mine for loc in lib.get("locations") or []):
                                    matches[str(lib["key"])] += 1
                        hit = max(matches.values()) if matches else 0
                        cand["libraries"].append({"key": there.key, "title": there.title, "kind": there.kind, "found": hit, "of": len(files), "matches": matches})
                        found, of = found + hit, of + len(files)
                    cand["rate"] = found / of if of else 0.0
                except PlexError as e:
                    cand["error"] = str(e)
            out.append(cand)
        with self.lock:
            self.candidates, self.discovered_at = out, time.time()
        self.save()
        self.app.run_checks_async()
        return out

    def discover_async(self) -> None:
        with self.lock:
            if self.running:
                return
            self.running = True

        def work():
            try:
                self.discover()
            except Exception as e:  # noqa: BLE001 — said on the page
                self.error = str(e)
            finally:
                with self.lock:
                    self.running = False
        threading.Thread(target=work, name="sync-discover", daemon=True).start()

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "error": self.error, "candidates": list(self.candidates), "discovered_at": self.discovered_at,
                    "peers": self.peers(), "libraries": self.by_library()}

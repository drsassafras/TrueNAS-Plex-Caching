"""Library Sync: this server and a mirror — another Plex server with the same files — compared, so
their libraries can be kept in step. This is the read-only half: nothing on either server is changed.

* Mirrors (settings "sync"): whether this server has a mirrored server is a choice to make — not
  chosen yet, none, or the server watched on the Sync page (plex2.py). Until it's made, Health asks,
  and what would act on it (deleting converted originals) waits. discover() looks for mirrors among
  the account's servers: for each, a sample of its files looked up here by path.
* Another Plex Cache: a system sharing the library folders marks itself there (optimize/claims.py):
  seen by peers(), so a Plex Cache at the other site is known without any setup.
* compare(): each paired library read from both servers (plex.library_items), items matched across
  them — by their files' place in the library folder (the same on both: the folders are synced),
  else by online entry; shows, seasons, artists and albums through their episodes and tracks — and
  the differences listed, source → mirror: files one server doesn't list (it needs a scan), versions
  grouped differently, another online match, edited fields, posters chosen by hand, collections,
  labels, editions. Watched state and ratings aren't compared: Plex's own sync carries them.
* Review before anything acts (settings "approved"): the sync settings as last reviewed; changed
  since, applying (a later part) stays paused until the differences are looked at again and approved.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unicodedata

from .i18n import M
from .plex import EDIT_FIELDS, LEAF_TYPE, PlexClient, PlexError, reachable, servers
from .optimize.claims import PEER_DAYS
from . import libhealth

STORE = "sync-report.json"
REPORT_VERSION = 3  # what a report holds; one made by an older version is dropped, so it's never shown as current
MAX_ITEMS = 1000  # differences listed per library, paged on the Library page (the counts are whole)
SAMPLE = 300  # of a candidate mirror's files looked up here, per library
KINDS = ("missing", "extra", "grouping", "match", "edits", "art", "collections", "labels", "editions")
LEAVES = {"movie": (1,), "show": (2, 3, 4), "artist": (8, 9, 10)}
NAMES = {"missing": M("Not listed there"), "extra": M("Only on the other server"), "grouping": M("Versions grouped differently"), "match": M("Matched to another entry"),
         "edits": M("Edited fields"), "art": M("Posters chosen by hand"), "collections": M("Collections"), "labels": M("Labels"),
         "editions": M("Editions")}


def signature(settings: dict, plex2: dict) -> str:
    """The sync settings that decide what a sync would do, as one string: what's reviewed."""
    s = settings
    return json.dumps({"mirror": plex2.get("machine_id") or plex2.get("url") or "", "pairs": plex2.get("libraries") or {},
                       "source": s.get("source"), "what": s.get("what"), "watched": s.get("watched"), "users": s.get("users"),
                       "libraries": s.get("libraries") or {}}, sort_keys=True)


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


def compare_library(src: _Side, dst: _Side, what: dict, checks_on: dict | None = None) -> dict:
    """The differences between a library on the source and on the other server: {"counts": {kind: n},
    "items": [rows], "matched": items found on both, "total"}. Items sharing files across the two
    servers (a version's file on one item here, another there) form one group, and each group is a row:
    {"title", "shape": "pair" | "split" | "merge" | "regroup" | "missing" | "extra", "kinds", "changes":
    [{"kind", "from", "to"}], "src_items": [sides], "dst_items": [sides], "src", "dst" (the first of each),
    "warn", "tags": {"src": [[checks]…], "dst": [[checks]…]} — the Plex checks (libhealth) on each record,
    marking data that looks bad before anything would be copied}. "split": one item on the source is
    several there (applying merges them); "merge": several here are one there (applying splits it);
    "regroup": several on both sides, tangled — shown as the records on each side, with no copy worked out."""
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
        S = sorted((src.items[k] for k in g["s"]), key=lambda it: it["label"].lower())
        D = sorted((dst.items[k] for k in g["d"]), key=lambda it: it["label"].lower())
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
        other = dst.items.get(max(votes, key=votes.get))
        if other:
            pairs[key] = other["key"]
            r = row("pair", [it], [other], src, dst)
            _compare_pair(it, other, what, lambda kind, _it, frm, to, _o=None: add(r, kind, frm, to))
            if r["kinds"]:
                rows.append(r)
    rows.sort(key=lambda r: r["title"].lower())
    return {"counts": counts, "items": rows[:MAX_ITEMS], "matched": len(pairs), "total": len(src.items)}


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


class _Paused(Exception):
    """The library's compare was paused (another was opened): dropped, picked up from the start later."""


class Sync:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        saved = app._load_json(STORE) or {}
        self.report: dict | None = saved.get("report") if (saved.get("report") or {}).get("version") == REPORT_VERSION else None
        from .syncapply import Applier
        self.applier = Applier(app, self)  # the differences you tick made true on the other server (syncapply.py)
        self.candidates: list[dict] = saved.get("candidates") or []
        self.discovered_at: float = saved.get("discovered_at") or 0.0
        self.running = False  # the comparing thread is going
        self.error: str | None = None
        self.libs: dict[str, dict] = {}  # key -> {"queued", "running", "paused", "step", "pct", "started", "finished", "error"}
        self.current: str | None = None

    def save(self) -> None:
        with self.lock:
            data = {"report": self.report, "candidates": self.candidates, "discovered_at": self.discovered_at}
        self.app._save_json(STORE, data)

    def settings(self) -> dict:
        return self.app.settings.section("sync")

    # ------------------------------------------------------------ mirrors
    def decided(self) -> str:
        """"none", "plex2" (watched, and set up), or "" (not chosen yet — or chosen but not set up)."""
        m = self.settings().get("mirrors")
        if m == "plex2" and not self.app.other.configured():
            return ""
        return m if m in ("none", "plex2") else ""

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
        """The account's other servers, each with how many of a sample of its files are here, by
        library: [{"id", "name", "url", "libraries": [{"title", "kind", "found", "of"}], "rate"}]."""
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
                        hit = 0
                        for f in files:
                            rel = next((f.path[len(loc.rstrip("/")) + 1:] for loc in there.locations if f.path.startswith(loc.rstrip("/") + "/")), None)
                            if rel is None:
                                continue
                            if any(self.app.map_path(loc.rstrip("/") + "/" + rel) in mine for lib in ours for loc in lib.get("locations") or []):
                                hit += 1
                        cand["libraries"].append({"title": there.title, "kind": there.kind, "found": hit, "of": len(files)})
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

    # ------------------------------------------------------------ comparing
    # One library at a time, as inspecting: opening a library's Sync view starts it (pausing the one
    # running, which picks up when it's opened again), and its result shows as soon as it's done.
    def _pairs(self) -> dict:
        s = self.settings()
        libs = {str(lib["key"]): lib for lib in getattr(self.app, "libraries", []) or []}
        return {key: (libs[key], there) for key, there in self.app.other.pairs().items()
                if there is not None and key in libs and (s.get("libraries") or {}).get(key) is not False}

    def start(self, key: str | None = None, force: bool = False) -> dict:
        """Compare one library (``key``), or every paired one, pausing any other; ``force``: again
        even if its result is current. The snapshot."""
        if self.decided() != "plex2":
            raise ValueError(M("No mirror to compare with: choose one on the Sync page."))
        pairs = self._pairs()
        keys = [str(key)] if key else list(pairs)
        with self.lock:
            for k in keys:
                if k not in pairs:
                    continue
                st = self.libs.get(k)
                done = (self.report or {}).get("libraries", {}).get(k)
                if st and st.get("running"):
                    continue
                if done and not force and not (st and st.get("paused")):
                    continue  # (compared already: its result stands until Compare Now)
                self.libs[k] = {"queued": True, "running": False, "paused": False, "step": None, "pct": 0, "started": None, "finished": None, "error": None}
            for k, st in self.libs.items():
                if k not in keys and st.get("running"):
                    st["paused"], st["queued"] = True, True  # (one at a time: it picks up when started again)
            self.current = next((k for k in keys if self.libs.get(k, {}).get("queued")), None)
            if self.current and not self.running:
                self.running = True
                threading.Thread(target=self._work, name="sync-compare", daemon=True).start()
        return self.snapshot()

    def pause(self, key: str) -> None:
        with self.lock:
            st = self.libs.get(str(key))
            if st and (st.get("running") or st.get("queued")):
                st["paused"] = True
            if self.current == str(key):
                self.current = None

    def _work(self) -> None:
        while True:
            with self.lock:
                key = self.current or next((k for k, st in self.libs.items() if st.get("queued") and not st.get("paused")), None)
                if not key:
                    self.running, self.current = False, None
                    return
                self.current = key
                st = self.libs[key]
                st.update(running=True, queued=False, paused=False, started=time.time(), error=None)
            try:
                got = self._compare_one(key)
            except _Paused:
                with self.lock:
                    st.update(running=False, queued=True, paused=True, step=None)
                    if self.current == key:
                        self.current = None
                continue
            except Exception as e:  # noqa: BLE001 — said on the page
                with self.lock:
                    st.update(running=False, finished=time.time(), error=str(e), step=None)
                    self.error = str(e)
                    if self.current == key:
                        self.current = None
                continue
            with self.lock:
                rep = self.report if (self.report or {}).get("version") == REPORT_VERSION else None
                if rep is None:
                    rep = self.report = {"version": REPORT_VERSION, "libraries": {}, "totals": {}, "names": {}}
                rep["libraries"][key] = got
                rep["at"] = time.time()
                rep["source"] = got["source"]
                rep["names"] = got["names"]
                rep["totals"] = {k: sum(v["counts"].get(k, 0) for v in rep["libraries"].values()) for k in KINDS}
                st.update(running=False, finished=time.time(), step=None, pct=100)
                self.error = None
                if self.current == key:
                    self.current = None
            self.save()
            self.app.run_checks_async()

    def _step(self, key: str, what: str, pct: int) -> None:
        with self.lock:
            st = self.libs.get(key) or {}
            if st.get("paused"):
                raise _Paused()
            st.update(step=what, pct=pct)

    def _compare_one(self, key: str) -> dict:
        """One paired library compared (read-only): its part of the report."""
        s = self.settings()
        lib, lib_there = self._pairs()[key]
        other = self.app.other
        here = self.app.plex(timeout=120)
        other.run(lambda c: c.identity(), timeout=20)  # (reached — at another of its addresses if need be)
        there = other.client(timeout=120)
        src_is_mirror = s.get("source") == "mirror"
        names = {"this": getattr(self.app, "plex_name", "") or str(M("This server")), "mirror": self.app.settings.section("plex2").get("name") or str(M("the mirror"))}
        kind = lib.get("kind")
        leaves = LEAVES.get(lib.get("type") or {"movie": "movie", "tv": "show", "audio": "artist"}.get(kind, ""), ())
        self._step(key, "here", 0)
        a_items = [it for t in leaves for it in here.library_items(key, t)]
        self._step(key, "there", 30)
        b_items = [it for t in leaves for it in there.library_items(lib_there.key, t)]
        self._step(key, "compare", 60)
        a, b = _Side(a_items, lib.get("locations") or []), _Side(b_items, lib_there.locations)
        src, dst = (b, a) if src_is_mirror else (a, b)
        on = libhealth.enabled(self.app.settings.get())
        got = compare_library(src, dst, s.get("what") or {}, on)
        # the items listed: each one's own page read from both servers (the listings leave out which
        # fields are edited, and the summary), then compared again with that
        src_client, dst_client = (there, here) if src_is_mirror else (here, there)
        for i, row in enumerate(got["items"]):
            self._step(key, "details", 70 + int(30 * i / max(1, len(got["items"]))))
            for sd, client, key_ in [(src, src_client, it["key"]) for it in row["src_items"]] + [(dst, dst_client, it["key"]) for it in row["dst_items"]]:
                if not key_:
                    continue
                try:
                    d = client.item_detail(key_)
                except PlexError:
                    d = None
                if d:
                    sd.items[key_].update({k: d[k] for k in DETAIL_KEYS if k in d})
        got = compare_library(src, dst, s.get("what") or {}, on)
        got.update(title=lib.get("title"), there=lib_there.title, at=time.time(), source="mirror" if src_is_mirror else "this", names=names)
        return got

    def compare(self) -> dict:
        """Every paired library compared now, in this thread (tests, and the scheduled run): the report."""
        if self.decided() != "plex2":
            raise ValueError(M("No mirror to compare with: choose one on the Sync page."))
        for key in self._pairs():
            got = self._compare_one(key)
            with self.lock:
                rep = self.report if (self.report or {}).get("version") == REPORT_VERSION else None
                if rep is None:
                    rep = self.report = {"version": REPORT_VERSION, "libraries": {}, "totals": {}, "names": {}}
                rep["libraries"][key] = got
                rep.update(at=time.time(), source=got["source"], names=got["names"])
                rep["totals"] = {k: sum(v["counts"].get(k, 0) for v in rep["libraries"].values()) for k in KINDS}
                self.libs[key] = {"queued": False, "running": False, "paused": False, "step": None, "pct": 100, "started": None, "finished": time.time(), "error": None}
        with self.lock:
            self.error = None
        self.save()
        self.app.run_checks_async()
        return self.report

    def compare_async(self) -> bool:
        """Every paired library, again (Compare Now)."""
        self.start(None, force=True)
        return True

    # ------------------------------------------------------------ review
    def current_signature(self) -> str:
        return signature(self.settings(), self.app.settings.section("plex2"))

    def review_needed(self) -> bool:
        """The sync settings changed since they were last reviewed and approved."""
        return self.settings().get("approved", "") != self.current_signature()

    def approve(self) -> None:
        """The differences as they are now have been looked at: the settings count as reviewed."""
        self.app.settings.update({"sync": {"approved": self.current_signature()}})
        self.app.run_checks_async()

    def due(self, now: float | None = None) -> bool:
        s = self.settings()
        if s.get("mode") == "off" or self.decided() != "plex2" or self.running:
            return False
        if any(st.get("queued") or st.get("paused") for st in self.libs.values()):
            return False
        last = (self.report or {}).get("at") or 0
        return (now or time.time()) - last >= int(s.get("compare_hours") or 6) * 3600

    def snapshot(self) -> dict:
        with self.lock:
            rep = self.report
            summary = {"at": rep.get("at"), "source": rep.get("source"), "totals": rep.get("totals"), "names": rep.get("names"),
                       "libraries": {k: {"title": v.get("title"), "there": v.get("there"), "counts": v.get("counts"), "matched": v.get("matched"), "total": v.get("total")}
                                     for k, v in (rep.get("libraries") or {}).items()}} if rep else None
            cur = self.libs.get(self.current or "") or {}
            return {"decided": self.decided(), "chosen": self.settings().get("mirrors"), "running": self.running, "error": self.error,
                    "libs": {k: dict(st) for k, st in self.libs.items()}, "current": self.current,
                    "progress": {"library": next((v.get("title") for k, v in (rep or {}).get("libraries", {}).items() if k == self.current), None) or self.current,
                                 "step": cur.get("step"), "pct": cur.get("pct", 0), "started": cur.get("started")} if self.running and self.current else None,
                    "report": summary, "candidates": list(self.candidates), "discovered_at": self.discovered_at, "apply": self.applier.snapshot(),
                    "peers": self.peers(), "review_needed": self.review_needed(), "names": {k: str(v) for k, v in NAMES.items()}}

    def differences(self, key: str) -> dict | None:
        with self.lock:
            got = dict((self.report or {}).get("libraries", {}).get(str(key)) or {})
            got["job"] = dict(self.libs.get(str(key)) or {})
            return got if got.get("counts") else {"job": got["job"]}

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

from .i18n import M
from .plex import EDIT_FIELDS, LEAF_TYPE, PlexClient, PlexError, reachable, servers
from .optimize.claims import PEER_DAYS

STORE = "sync-report.json"
MAX_ITEMS = 200  # differences listed per library (the counts are whole)
SAMPLE = 300  # of a candidate mirror's files looked up here, per library
KINDS = ("missing", "grouping", "match", "edits", "art", "collections", "labels", "editions")
LEAVES = {"movie": (1,), "show": (2, 3, 4), "artist": (8, 9, 10)}
NAMES = {"missing": M("Not listed there"), "grouping": M("Versions grouped differently"), "match": M("Matched to another entry"),
         "edits": M("Edited fields"), "art": M("Posters chosen by hand"), "collections": M("Collections"), "labels": M("Labels"),
         "editions": M("Editions")}


def signature(settings: dict, plex2: dict) -> str:
    """The sync settings that decide what a sync would do, as one string: what's reviewed."""
    s = settings
    return json.dumps({"mirror": plex2.get("machine_id") or plex2.get("url") or "", "pairs": plex2.get("libraries") or {},
                       "source": s.get("source"), "what": s.get("what"), "watched": s.get("watched"), "users": s.get("users"),
                       "libraries": s.get("libraries") or {}}, sort_keys=True)


class _Side:
    """One server's library, read: its items by key, and its leaf items by their file's place in the folder."""

    def __init__(self, items: list[dict], locations: list[str]):
        self.items = {it["key"]: it for it in items}
        self.by_rel: dict[str, str] = {}  # relative path -> item key
        self.children: dict[str, list[str]] = {}
        locs = sorted((loc.rstrip("/") for loc in locations), key=len, reverse=True)
        for it in items:
            for path, _ in it["files"]:
                for loc in locs:
                    if path.startswith(loc + "/"):
                        self.by_rel[path[len(loc) + 1:]] = it["key"]
                        break
            for up in (it["parent"], it["grandparent"]):
                if up:
                    self.children.setdefault(up, []).append(it["key"])

    def rels(self, key: str) -> set[str]:
        """A leaf item's files, as their place in the folder."""
        return {rel for rel, k in self.by_rel.items() if k == key}


def compare_library(src: _Side, dst: _Side, what: dict) -> dict:
    """The differences between a library on the source and on the mirror: {"counts": {kind: n},
    "items": [{"kind", "title", "detail"}], "matched": items found on both, "total"}."""
    counts = dict.fromkeys(KINDS, 0)
    out: list[dict] = []
    pairs: dict[str, str] = {}  # source key -> mirror key

    def add(kind, title, detail):
        counts[kind] += 1
        if len(out) < MAX_ITEMS:
            out.append({"kind": kind, "title": title, "detail": detail})
    # leaves first, by their files' place; then by online entry
    for key, it in src.items.items():
        if not it["files"]:
            continue
        rels = src.rels(key)
        there = {dst.by_rel[r] for r in rels if r in dst.by_rel}
        missing = [r for r in rels if r not in dst.by_rel]
        if missing and what.get("grouping", True):
            add("missing", it["label"], M("{n} of its files aren’t listed on the other server: {files}", one="Its file isn’t listed on the other server: {files}",
                                           n=len(missing), files=", ".join(os.path.basename(r) for r in missing[:3])))
        if not there:
            if it["guid"] and it["matched"]:
                cands = [k for k, o in dst.items.items() if o["guid"] == it["guid"] and o["files"]]
                if len(cands) == 1:
                    there = {cands[0]}
        if not there:
            continue
        if len(there) > 1 and what.get("grouping", True):
            add("grouping", it["label"], M("One item on the source, {n} items on the other server.", n=len(there)))
            continue
        other = next(iter(there))
        pairs[key] = other
        o = dst.items[other]
        if what.get("grouping", True) and not missing and src.rels(key) != dst.rels(other) and dst.rels(other) - src.rels(key):
            add("grouping", it["label"], M("The other server’s item has {n} more files grouped with it.", n=len(dst.rels(other) - src.rels(key))))
        _compare_pair(it, o, what, add)
    # parents (shows, seasons, artists, albums): the item there that most of their matched children belong to
    for key, it in src.items.items():
        if it["files"]:
            continue
        kids = src.children.get(key) or []
        votes: dict[str, int] = {}
        for k in kids:
            other = pairs.get(k)
            if other:
                o = dst.items[other]
                up = o["parent"] if it["type"] in ("season", "album") else o["grandparent"] or o["parent"]
                if up:
                    votes[up] = votes.get(up, 0) + 1
        if not votes:
            continue
        other = max(votes, key=votes.get)
        if other in dst.items:
            pairs[key] = other
            _compare_pair(it, dst.items[other], what, add)
    return {"counts": counts, "items": out, "matched": len(pairs), "total": len(src.items)}


def _compare_pair(a: dict, b: dict, what: dict, add) -> None:
    label = a["label"]
    if what.get("matches", True) and a["guid"] and b["guid"] and a["guid"] != b["guid"]:
        add("match", label, M("Matched to {here} on the source, {there} on the other server.", here=a["guid"], there=b["guid"] if b["matched"] else M("nothing (unmatched)")))
    if what.get("edits", True):
        diff = [f for f in EDIT_FIELDS if f in a["fields"] and (f not in b["fields"] or a["fields"][f] != b["fields"][f])]
        if diff:
            add("edits", label, M("Edited on the source, not the same on the other server: {fields}.", fields=", ".join(diff)))
    if what.get("art", True) and a["art_locked"] and a["art_locked"] != b["art_locked"]:
        add("art", label, M("Chosen by hand on the source ({which}), not on the other server.", which=", ".join(a["art_locked"])))
    if what.get("collections", True) and a["collections"] != b["collections"]:
        add("collections", label, M("Source: {here}. Other server: {there}.", here=", ".join(a["collections"]) or "—", there=", ".join(b["collections"]) or "—"))
    if what.get("collections", True) and a["labels"] != b["labels"]:
        add("labels", label, M("Source: {here}. Other server: {there}.", here=", ".join(a["labels"]) or "—", there=", ".join(b["labels"]) or "—"))
    if what.get("editions", True) and a["files"] and (a["edition"] or "") != (b["edition"] or ""):
        add("editions", label, M("Edition on the source: {here}. Other server: {there}.", here=a["edition"] or "—", there=b["edition"] or "—"))


class Sync:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        saved = app._load_json(STORE) or {}
        self.report: dict | None = saved.get("report")
        self.candidates: list[dict] = saved.get("candidates") or []
        self.discovered_at: float = saved.get("discovered_at") or 0.0
        self.running = False
        self.error: str | None = None

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
    def compare(self) -> dict:
        """Every paired library compared (read-only): the report, kept."""
        s = self.settings()
        if self.decided() != "plex2":
            raise ValueError(M("No mirror to compare with: choose one on the Sync page."))
        other = self.app.other
        here = self.app.plex(timeout=120)
        other.run(lambda c: c.identity(), timeout=20)  # (reached — at another of its addresses if need be)
        there = other.client(timeout=120)
        src_is_mirror = s.get("source") == "mirror"
        libs = {str(lib["key"]): lib for lib in getattr(self.app, "libraries", []) or []}
        report = {"at": time.time(), "source": "mirror" if src_is_mirror else "this", "libraries": {}, "totals": dict.fromkeys(KINDS, 0)}
        for key, lib_there in other.pairs().items():
            lib = libs.get(key)
            if lib_there is None or not lib or (s.get("libraries") or {}).get(key) is False:
                continue
            kind = lib.get("kind")
            leaves = LEAVES.get(lib.get("type") or {"movie": "movie", "tv": "show", "audio": "artist"}.get(kind, ""), ())
            a_items = [it for t in leaves for it in here.library_items(key, t)]
            b_items = [it for t in leaves for it in there.library_items(lib_there.key, t)]
            a, b = _Side(a_items, lib.get("locations") or []), _Side(b_items, lib_there.locations)
            src, dst = (b, a) if src_is_mirror else (a, b)
            got = compare_library(src, dst, s.get("what") or {})
            got.update(title=lib.get("title"), there=lib_there.title)
            report["libraries"][key] = got
            for k, n in got["counts"].items():
                report["totals"][k] += n
        with self.lock:
            self.report, self.error = report, None
        self.save()
        self.app.run_checks_async()
        return report

    def compare_async(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True

        def work():
            try:
                self.compare()
            except Exception as e:  # noqa: BLE001 — said on the page
                self.error = str(e)
            finally:
                with self.lock:
                    self.running = False
        threading.Thread(target=work, name="sync-compare", daemon=True).start()
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
        last = (self.report or {}).get("at") or 0
        return (now or time.time()) - last >= int(s.get("compare_hours") or 6) * 3600

    def snapshot(self) -> dict:
        with self.lock:
            rep = self.report
            summary = {"at": rep.get("at"), "source": rep.get("source"), "totals": rep.get("totals"),
                       "libraries": {k: {"title": v.get("title"), "there": v.get("there"), "counts": v.get("counts"), "matched": v.get("matched"), "total": v.get("total")}
                                     for k, v in (rep.get("libraries") or {}).items()}} if rep else None
            return {"decided": self.decided(), "chosen": self.settings().get("mirrors"), "running": self.running, "error": self.error,
                    "report": summary, "candidates": list(self.candidates), "discovered_at": self.discovered_at,
                    "peers": self.peers(), "review_needed": self.review_needed(), "names": {k: str(v) for k, v in NAMES.items()}}

    def differences(self, key: str) -> dict | None:
        with self.lock:
            return dict((self.report or {}).get("libraries", {}).get(str(key)) or {}) or None

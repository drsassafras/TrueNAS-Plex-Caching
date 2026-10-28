"""Mirror Checks (Tools → Mirror Checks): whether the two sites of a mirror hold the same files, and whether each
side's Plex sees them. A category of Library Health's checks ("mirror", libhealth.py), on the check page every category
has (checksPage): each finding a chip on its item, ignored, counted and switched off as every check's. It only reads;
its one action is asking a Plex to scan a folder.

For each library paired with one on the mirror (the Mirror page), what's compared, by each file's place in the library
folder (the same at both sites: the folders are synced; one Unicode form and one case, as Sync Libraries compares):

* **Here** — the files on disk against what Plex here lists (the data layer has both): on disk but not in Plex
  (Scan Folder asks Plex here to look), in Plex but not on disk.
* **The mirror's Plex** — what it lists against what Plex here lists (plex2.index): a file it doesn't list (Scan
  Folder asks it to look: its sign-in is the Mirror page's), or one only it lists.
* **The other site's disk** — with the other Plex Cache paired and talking (peerlink.py): its files against ours —
  only here, only there, a different size. A file changed in the last hour is left out: it may still be syncing.
* **The folder sync itself** — whether the markers and claims in .plexcache reach the other side (peerlink.folder_sync).
  While they don't, the two could optimize the same file: optimizing, the file tools and Sync Libraries are held in that
  library (hold()) until it's fixed, or the finding is ignored, or its check switched off.
* **What Sync Libraries can't do** — it brings the two Plexes in line (tools/sync.py); what that covers isn't listed
  here (sync_handles: a file the other side's Plex doesn't list, from the side whose edits win, is its to scan for), and
  what it can't is (the items grouped another way: several on the winning side that are one on the other, or files shared
  out differently on both — from its analysis, as it stands).

The comparing is slow (the other Plex, the other site's files), so it's done in the background by the evaluator
(evaluate.py: a library's turn comes before Sync Libraries' and the other tools', the page open first) — when the
library's data changes, every few hours, and on Scan Again — and kept (mirrorhealth.json); Library Health reads what was
found (findings(), notes()). What it finds on an item's files stops Sync Libraries' row on it (blocks()). What a
scan couldn't check is said at the top of the page (unchecked()): a red chip for each reason — the other site's files
not compared while the link to a Plex Cache known to be there (paired, or found) isn't working, the mirror's Plex not
read — and those checks' counts red. With no Plex Cache known at the mirror's site, the disks aren't compared and
nothing is red: nothing known to be there went unread.
"""

import logging
import os
import re
import threading
import time

from .i18n import M, from_json, jsonable, size_text
from .peerlink import rel_files, rel_of

log = logging.getLogger("plexcache")

CHECK_EVERY = 6 * 3600
RECENT = 3600            # a file changed this recently may still be syncing: not compared across the sites
MAX_PER_KIND = 1000      # findings kept per kind and library (the counts are whole: a note says when more were found)
RECHECK_AFTER = 90       # seconds after Scan Folder before the library is checked again
# the findings on an item's files that stop Sync Libraries' row on it (Tool.blocking_sources): the files aren't the same at
# the two sites, so bringing what Plex lists of them in line would be over files that differ
BLOCKS = ("only_here", "only_there", "size_differs", "missing_here")
BLOCK_IDS = tuple("mirror_" + k for k in BLOCKS)  # (their Library Health ids: Tool.block_ids)

# the kinds of finding, each a check of Library Health's ("mirror_<kind>": its name and help are there). What Sync Libraries
# brings in line isn't one (sync_handles); what it can't of the items' grouping is (LIVE: from its analysis, as it stands)
KINDS = ("sync_files", "only_here", "only_there", "size_differs", "not_listed_there", "only_listed_there", "unlisted_here", "missing_here")
LIVE = ("regroup",)  # worked out as Library Health asks (regroup), not by a scan
ACTION = {"not_listed_there": "there", "unlisted_here": "here"}  # the kinds Scan Folder applies to, and on which Plex
SYNC_ITEM = ".plexcache"  # the item the folder sync's finding is on (a library's, not a file's)
PEER_CARD = "/system#peer"  # System → Mirror Plex Cache: where the link is seen to
FORMAT = 2  # what a scan records ("v"): a scan saved before (no record of the checks it couldn't run) is done again at once
# what a scan before FORMAT 2 said, as a line under its findings, when it couldn't compare the other site's files
OLD_DISK_NOTES = ("The other site’s disk isn’t compared", "The other Plex Cache is still reading", "The other site’s files couldn’t be read")


def _norm(rel: str) -> str:
    from .sync import _norm as norm
    return norm(rel)


def _rel_there(path: str, locations: list[str]) -> str:
    for loc in sorted((loc.rstrip("/") for loc in locations or []), key=len, reverse=True):
        if path.startswith(loc + "/"):
            return path[len(loc) + 1:]
    return ""


class MirrorHealth:
    BLOCK_IDS = BLOCK_IDS
    TITLE = M("Mirror Checks")

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        saved = app._load_json("mirrorhealth.json") or {}
        self.results: dict[str, dict] = _back(saved.get("results") or {})      # library -> the last check (its notes translatable)
        self.scanned: dict[str, float] = saved.get("scanned") or {}              # "library/item/check" -> when Scan Folder was pressed
        self.running: str | None = None
        self.checked_at = float(saved.get("checked_at") or 0)
        self.wanted: set[str] = set()  # libraries to compare again (Scan Again, after Scan Folder): not current until they are

    def save(self) -> None:
        with self.lock:
            data = {"results": jsonable(self.results), "scanned": {k: v for k, v in self.scanned.items() if time.time() - v < 7 * 86400},
                    "checked_at": self.checked_at}
        self.app._save_json("mirrorhealth.json", data)

    # ------------------------------------------------------------ what's compared
    def libraries(self) -> list[str]:
        """The libraries paired with one on the mirror (none while that isn't known yet: see waiting())."""
        return self.paired()[0]

    def waiting(self):
        """None once what's paired with the mirror is known (a mirror watched or not); else ("pending", None) while the
        mirror's libraries are being read — the first time — or ("failed", why) when they couldn't be. Never "no mirror"
        for a mirror that's watched but not reached yet."""
        return self.paired()[1]

    def paired(self) -> tuple[list[str], tuple | None]:
        """(libraries paired, waiting) — kept a minute once known (the state asks every few seconds). The mirror's libraries
        are read in a thread of their own once the evaluator runs (the state never waits on the mirror); before (the tests),
        here."""
        here = tuple(str(lib["key"]) for lib in getattr(self.app, "libraries", []) or [])
        sig = (here, self.app.settings.rev)  # (the libraries here, the pairing chosen: what's paired follows them at once)
        cached = getattr(self, "_libs", None)
        if cached and cached[2] == sig and time.time() - cached[0] < 60:
            return cached[1], None
        other = getattr(self.app, "other", None)
        out, waiting = [], None
        watched = other is not None and other.any_mirror()
        if watched and not here:  # (this Plex's libraries not read yet: what's paired with them isn't known)
            waiting = ("pending", None)
        elif watched:
            from .plex2 import LIBS_TTL
            with other.lock:
                theirs, err, at = other.libs, other.error, other.libs_at
            if theirs is None or time.time() - at >= LIBS_TTL:
                theirs = self._read_mirror(theirs)
                with other.lock:
                    err = other.error
            if theirs is None:
                waiting = ("failed", err) if err else ("pending", None)
            else:
                try:
                    out = [k for k, t in other.pairs(list(theirs)).items() if t is not None]
                except Exception as e:  # noqa: BLE001
                    out, waiting = [], ("failed", str(e))
        if waiting is None:
            self._libs = (time.time(), out, sig)
        return out, waiting

    def _read_mirror(self, theirs):
        """The mirror's libraries read (again): in a thread of its own once the evaluator runs — what's known kept meanwhile,
        the evaluator told when they're in — else here. What's known now."""
        other = self.app.other
        ev = getattr(self.app, "evaluator", None)
        if ev is None or not ev.started:
            other.libraries()
            with other.lock:
                return other.libs
        with self.lock:
            if getattr(self, "reading", False):
                return theirs
            self.reading = True

        def run():
            try:
                other.libraries()
            except Exception:  # noqa: BLE001 — said by its error (waiting)
                pass
            finally:
                with self.lock:
                    self.reading = False
                self._libs = self._snap = None
                ev.kick()
        threading.Thread(target=run, name="mirror-libraries", daemon=True).start()
        return theirs

    def check(self, key: str) -> dict:
        """One library compared now (see the module's docstring): {"at", "rows", "counts", "notes", "error", "disk",
        "mirror"}. Each row a finding on an item of Library Health's: Plex's record here where it lists the file
        (item: its rating key), else the file here (its path), else its place in the folder (there:<place>)."""
        key = str(key)
        link = self.app.peerlink
        notes: list = []
        unchecked: list[dict] = []  # the checks that couldn't run, a group for each reason: [{"checks", "label", "why", "link"}]
        rows: dict[str, list[dict]] = {k: [] for k in KINDS}
        counts = dict.fromkeys(KINDS, 0)
        d = self.app.libdata.data(key)  # (as it is: the evaluator compares once it's in — asked for directly, read first if it isn't)
        if not d.ready:
            d = self.app.libdata.gather(key, ("records", "files"), wait=True)
        # each file here by its place: the item it's on (Plex's record, else the file itself), its name, its path
        here_item: dict[str, dict] = {}
        for f in list(d.unlisted) + list(d.files):
            rel = rel_of(d, f.get("local") or "")
            if rel:
                rk = f.get("rk")
                here_item[_norm(rel)] = {"item": str(rk) if rk else f.get("path") or rel, "label": f.get("title") or rel,  # (no record: its place in the folder)
                                         "poster": f.get("poster") or "", "path": f.get("path") or ""}

        def add(kind, rel, **extra):
            counts[kind] += 1
            if len(rows[kind]) < MAX_PER_KIND:
                at = here_item.get(_norm(rel)) or {"item": "there:" + rel, "label": rel, "poster": "", "path": ""}
                rows[kind].append(dict(at, kind=kind, rel=rel, file=os.path.basename(rel) or rel, **extra))
        now = time.time()
        # here: the disk against Plex
        for f in d.unlisted:
            rel = rel_of(d, f.get("local") or "")
            if rel:
                add("unlisted_here", rel, size=f.get("size"), folder=os.path.dirname(f.get("path") or ""))
        for f in d.files:
            if f.get("missing"):
                rel = rel_of(d, f.get("local") or "")
                if rel:
                    add("missing_here", rel)
        here_disk = {_norm(r): (r, s, m) for r, s, m in rel_files(d)}
        here_plex = {}
        for f in d.files:
            rel = rel_of(d, f.get("local") or "")
            if rel:
                here_plex[_norm(rel)] = f
        # the other site's disk, through the other Plex Cache
        there = self.app.other.pairs().get(key)
        if there is None:
            raise ValueError(M("This library isn’t paired with one on the mirror ([Mirror page](/mirrors))."))
        there_disk, disk_why = None, None
        if link.talking():
            try:
                got = link.files(there.key)
                if got.get("ready"):
                    there_disk = {_norm(r): (r, int(s or 0), float(m or 0)) for r, s, m in got.get("files") or []}
                else:
                    disk_why = (M("The other Plex Cache is still reading this library’s files: they’re compared at the next scan."), None)
            except Exception as e:  # noqa: BLE001
                disk_why = (M("The other site’s files couldn’t be read: {why}", why=str(e)), PEER_CARD)
        elif link.known():
            bad = "; ".join(str(v["text"]) for v in link.lights().values() if v["status"] in ("warn", "fail"))
            disk_why = (M("The link to the Plex Cache at the other site isn’t working: {lights}.", lights=bad) if bad
                        else M("The link to the Plex Cache at the other site isn’t working."), PEER_CARD)
        elif not link.wanted():  # (none there, or none found yet: nothing that should have been compared wasn't — not red)
            notes.append(M("The other site’s files aren’t compared: System → Mirror Plex Cache is set to say there’s no Plex Cache there."))
        if disk_why:  # (the checks that need the other site's files, and the folder sync's, which needs the link)
            unchecked.append({"checks": ["mirror_only_here", "mirror_only_there", "mirror_size_differs"] + ([] if link.talking() else ["mirror_sync_files"]),
                              "label": M("Other site’s files not compared"), "why": disk_why[0], "link": disk_why[1]})
        if there_disk is not None:
            for n, (rel, size, mtime) in here_disk.items():
                got = there_disk.get(n)
                if got is None:
                    if now - mtime > RECENT:
                        add("only_here", rel, size=size)
                elif got[1] != size and now - max(mtime, got[2]) > RECENT:
                    add("size_differs", rel, size=size, size_there=got[1])
            for n, (rel, size, mtime) in there_disk.items():
                if n not in here_disk and now - mtime > RECENT:
                    add("only_there", rel, size=size)
        # the mirror's Plex against Plex here
        try:
            idx = self.app.other.index(there, fresh=True)
        except Exception as e:  # noqa: BLE001
            idx = None
            unchecked.append({"checks": ["mirror_not_listed_there", "mirror_only_listed_there", "mirror_unlisted_here"], "label": M("Mirror’s Plex not read"),
                              "why": M("The mirror’s Plex couldn’t be read: {why}", why=str(e)), "link": "/mirrors"})
        if idx is not None:
            there_plex = {}
            for path in idx:
                rel = _rel_there(path, there.locations)
                if rel:
                    there_plex[_norm(rel)] = path
            for n, f in here_plex.items():
                if f.get("missing") or n in there_plex:
                    continue
                if there_disk is not None and n not in there_disk:
                    continue  # (not on disk there: said above, a scan there can't help)
                rel = rel_of(d, f.get("local") or "")
                add("not_listed_there", rel, folders=[os.path.dirname(loc.rstrip("/") + "/" + rel) for loc in there.locations])
            for n, path in there_plex.items():
                if n not in here_plex and n not in here_disk:
                    add("only_listed_there", _rel_there(path, there.locations))
            for r in rows["unlisted_here"]:  # (a file Plex here doesn't list that the mirror's does: Sync Libraries' to scan for, where the mirror's edits win)
                r["listed_there"] = _norm(r["rel"]) in there_plex
        # the folder sync of .plexcache
        fs = (link.folder_sync() or {}).get(key) if link.talking() else None
        if fs and fs["status"] in ("warn", "fail"):
            counts["sync_files"] += 1
            behind = fs.get("out") or fs.get("in")
            rows["sync_files"].append({"item": SYNC_ITEM, "label": SYNC_ITEM, "poster": "", "path": "", "kind": "sync_files", "rel": SYNC_ITEM, "file": "",
                                       "status": fs["status"], "behind": int(behind) if behind else None})
        more = sum(counts[k] - len(rows[k]) for k in KINDS)
        if more > 0:
            notes.append(M("{n} more differences were found than are shown: the first {max} of each kind are.", one="{n} more difference was found than is shown: the first {max} of each kind are.",
                           n=more, max=MAX_PER_KIND))
        return {"v": FORMAT, "at": now, "rows": [r for k in KINDS for r in rows[k]], "counts": counts, "notes": notes, "unchecked": unchecked, "error": None,
                "disk": there_disk is not None, "mirror": there.title if there is not None else ""}

    def check_async(self, key: str | None = None) -> bool:
        """The libraries compared again (one, or every paired one: Scan Again, after Scan Folder) — the library's records and
        files read again first — by the evaluator, in its turn; before it runs (the tests), in a thread here."""
        keys = [str(key)] if key else self.libraries()
        ev = getattr(self.app, "evaluator", None)
        if ev is not None and ev.started:
            if not keys:
                return False
            with self.lock:
                self.wanted.update(keys)
            for k in keys:
                self.app.libdata.refresh(k, ("records", "files"))
            self._snap = None
            ev.kick()
            return True
        with self.lock:
            if self.running or not keys:
                return False
            self.running = keys[0]

        def work():
            try:
                for k in keys:
                    with self.lock:
                        self.running = k
                    try:
                        got = self.check(k)
                    except Exception as e:  # noqa: BLE001 — said on the page
                        log.debug("mirror health check failed", exc_info=True)
                        got = dict(self.results.get(k) or {}, error=e.args[0] if e.args and isinstance(e.args[0], M) else str(e), at=time.time(), v=FORMAT)
                    with self.lock:
                        self.results[k] = got
            finally:
                with self.lock:
                    self.running = None
                    self.checked_at = time.time()
                self._snap = None
                self.save()
        threading.Thread(target=work, name="mirror-health", daemon=True).start()
        return True

    def due(self) -> bool:
        """Whether a library's comparing is due (due_for)."""
        return any(self.due_for(k) for k in self.libraries())

    def due_for(self, key: str) -> bool:
        """A library compared again every few hours — and at once while its last scan is from before FORMAT, which didn't
        record the checks it couldn't run (its red would be missing)."""
        with self.lock:
            res = self.results.get(str(key)) or {}
        if not res.get("at"):
            return True
        return res.get("v") != FORMAT or time.time() - float(res["at"]) > CHECK_EVERY

    # ------------------------------------------------------------ the evaluator's (evaluate.MirrorUnit)
    def not_applicable(self, key: str):
        """Why a library isn't compared (an M: its count "na"): no mirror watched, it isn't paired with one there, or the
        other side is switching its folder to a child dataset (compared again once that's done)."""
        libs, waiting = self.paired()
        if str(key) in libs:
            why = self.converting(key)
            was = getattr(self, "_converting", set())
            if why:
                self._converting = was | {str(key)}
                return M("The other site is converting this library’s folder to a child dataset: compared again once it’s done.")
            if str(key) in was:  # (done there: compared again now, not on the next round)
                self._converting = was - {str(key)}
                with self.lock:
                    self.wanted.add(str(key))
        if str(key) in libs or waiting:  # (not known yet: its count says so — waiting())
            return None
        return M("No mirror is watched: mark a library as mirrored on the [Mirror page](/mirrors).") if not libs else \
            M("This library isn’t paired with one on the mirror: pair it on the [Mirror page](/mirrors) to compare it.")

    def current(self, key: str) -> bool:
        """Compared over the library's data as it is now (records and files here: the stamp), not asked for again, and not
        being compared now (its count pending meanwhile: the page's one bar)."""
        key = str(key)
        with self.lock:
            res = self.results.get(key) or {}
            wanted = key in self.wanted or self.running == key
        return not wanted and res.get("v") == FORMAT and res.get("stamp") is not None and res["stamp"] == self.app.libdata.data(key).stamp

    def scanning(self, key: str) -> bool:
        """Whether the library is being compared now, or is to be (Scan Again, or what it read changed): what was found is
        being worked out again — its findings shown meanwhile, their buttons not to be pressed (libhealth.scanning)."""
        key = str(key)
        if key not in self.libraries():
            return False
        with self.lock:
            if self.running == key or key in self.wanted:
                return True
            had = bool((self.results.get(key) or {}).get("at"))
        return had and not self.current(key)

    def evaluate(self, key: str) -> None:
        """The library compared now, over its data as it is (the evaluator's thread): kept with the stamp it's of. A compare
        that goes wrong is said on the page (the last check's error) and raised: its count says it couldn't be worked out."""
        key = str(key)
        stamp = self.app.libdata.data(key).stamp
        with self.lock:
            self.running = key
            self.wanted.discard(key)
        self._snap = None
        try:
            try:
                got = dict(self.check(key), stamp=stamp)
            except Exception as e:
                log.debug("mirror checks failed", exc_info=True)
                with self.lock:
                    self.results[key] = dict(self.results.get(key) or {}, error=e.args[0] if e.args and isinstance(e.args[0], M) else str(e), at=time.time(), v=FORMAT, stamp=None)
                raise
            with self.lock:
                self.results[key] = got
        finally:
            with self.lock:
                self.running = None
                self.checked_at = time.time()
            self._snap = None
            self.save()
        # its findings laid over Library Health's answer as it's kept (quick): the counts follow at once, never turning
        if hasattr(self.app, "library_health"):
            try:
                self.app.library_health(key)
            except Exception:  # noqa: BLE001 — the evaluator works it out in its turn
                log.debug("library health after mirror checks failed", exc_info=True)

    def blocks(self, key: str) -> dict[str, list[dict]]:
        """What stops Sync Libraries' row on an item (BLOCKS: its files aren't the same at the two sites), as the last compare
        found it: {item: [{"key", "id", "title", "text"}]} — red there until it's put right, or ignored here."""
        with self.lock:
            rows = list((self.results.get(str(key)) or {}).get("rows") or [])
        out: dict[str, list[dict]] = {}
        for r in rows:
            if r.get("kind") in BLOCKS and r.get("item"):
                out.setdefault(str(r["item"]), []).append({"key": str(r["item"]), "id": "mirror_" + r["kind"], "title": r.get("label") or "", "text": _said(r)})
        return out

    # ------------------------------------------------------------ what Sync Libraries does instead
    def sync_on(self, key: str) -> dict:
        """Sync Libraries' options on the library: whose edits win ("source": "this" | "mirror"), and whether it scans for
        the files the other side doesn't list ("scan"). {}: no such tool."""
        tool = (getattr(self.app, "tools", None) or {}).get("sync")
        return tool.enabled(str(key)) if tool is not None else {}

    def source(self, key: str) -> str | None:
        """Whose edits win in Sync Libraries on the library ("this" | "mirror"; None: no such tool)."""
        return self.sync_on(key).get("source")

    @staticmethod
    def sync_handles(r: dict, on: dict) -> bool:
        """Whether a finding is Sync Libraries' to bring in line, not Mirror Checks' (``on``: its options on the library,
        sync_on): a file the mirror's Plex doesn't list, where this server's edits win (it scans for it there); one Plex here
        doesn't list that the mirror's does, where the mirror's win (it scans for it here). Neither while its "Files the
        other side doesn't list" is off: nothing else would say them."""
        if not on.get("scan"):
            return False
        if on.get("source") == "this":
            return r.get("kind") == "not_listed_there"
        if on.get("source") == "mirror":
            return r.get("kind") == "unlisted_here" and bool(r.get("listed_there"))
        return False

    def regroup(self, key: str) -> list[dict]:
        """The items Sync Libraries can't group as the side whose edits win has them (its analysis, as it stands): findings
        of their own (mirror_regroup), on each item here."""
        tool = (getattr(self.app, "tools", None) or {}).get("sync")
        d = self.app.libdata.data(str(key))
        if tool is None or not d.ready:
            return []
        try:
            groups = tool.regroup(d)
        except Exception:  # noqa: BLE001 — the tool's scan refusing this library: none
            return []
        names = tool.names()
        src_name, dst_name = (names["this"], names["mirror"]) if self.source(key) != "mirror" else (names["mirror"], names["this"])
        here_first = self.source(key) != "mirror"
        out = []
        for g in groups:
            a, b = (g["here"], g["there"]) if here_first else (g["there"], g["here"])  # (the winning side's first)
            text = (M("{n} items on {src} are one item on {dst}: Sync Libraries can’t split it. Split it in Plex on {dst}, or merge the items on {src}.", n=len(a), src=src_name, dst=dst_name)
                    if g["shape"] == "merge" else
                    M("{a} items on {src} and {b} on {dst} share their files out differently: Sync Libraries can’t work out which is which. Split or merge them in Plex until each side’s items hold the same files.", a=len(a), b=len(b), src=src_name, dst=dst_name))
            lines = [M("Here: {items}", items=", ".join(g["here"])), M("On the mirror: {items}", items=", ".join(g["there"]))]
            for k in g["keys"]:
                out.append({"item": str(k), "label": g["title"], "poster": "", "result": {"id": "mirror_regroup", "category": "mirror", "status": "warn", "text": text, "file": "", "lines": lines}})
        return out

    # ------------------------------------------------------------ what Library Health shows (libhealth.SOURCES)
    def findings(self, key: str) -> list[dict]:
        """The last check's findings, for Library Health: [{"item", "label", "poster", "result"}], each result one of its
        chips (category "mirror", id "mirror_<kind>"), with Scan Folder on the kinds it applies to — but those Sync Libraries
        brings in line (sync_handles) — and the items it can't group (regroup)."""
        key = str(key)
        with self.lock:
            rows = list((self.results.get(key) or {}).get("rows") or [])
            scanned = dict(self.scanned)
        on = self.sync_on(key)
        out = []
        for r in rows:
            kind = r.get("kind")
            if kind not in KINDS or not r.get("item"):
                continue  # (a check from before findings were Library Health's: replaced at the next one)
            if self.sync_handles(r, on):
                continue
            res = {"id": "mirror_" + kind, "category": "mirror", "status": "warn", "text": _said(r), "file": r.get("file") or ""}
            lines = []
            if r.get("rel") and kind != "sync_files" and r.get("label") != r["rel"]:  # (an item of its own is named for its place)
                lines.append(M("In the library folder: {place}", place=r["rel"]))
            if r.get("size") is not None and kind != "size_differs":
                lines.append(size_text(r["size"]))
            res["lines"] = lines
            if kind in ACTION:
                res["action"] = {"label": M("Scan Folder"), "done_at": scanned.get(key + "/" + r["item"] + "/" + res["id"]),
                                 "tip": M("Asks the mirror’s Plex to look at this file’s folder.") if ACTION[kind] == "there" else M("Asks Plex here to look at this file’s folder.")}
            out.append({"item": r["item"], "label": r.get("label") or r["item"], "poster": r.get("poster") or "", "result": res})
        return out + self.regroup(key)

    def notes(self, key: str) -> list[dict]:
        """What the page says above the findings, each {"status": "warn" | "info", "text"}: the link not healthy, the library
        held, not paired, not checked yet, what the last check couldn't compare."""
        key = str(key)
        snap = self.snapshot()
        if snap["waiting"]:
            return [self.waiting_note(snap["waiting"])]
        if key not in snap["libraries"]:
            return [{"status": "info", "text": M("No mirror is watched: mark a library as mirrored on the [Mirror page](/mirrors).") if not snap["libraries"]
                     else M("This library isn’t paired with one on the mirror: pair it on the [Mirror page](/mirrors) to compare it.")}]
        out = []
        with self.lock:
            res = dict(self.results.get(key) or {})
        conn = snap["connection"]
        if conn["status"] == "warn" and not any(u.get("link") == PEER_CARD for u in self.unchecked(key)):
            out.append({"status": "warn", "text": conn["text"]})  # (said by the red chip on top instead, when the last scan was left short by it)
        held = self.hold(key)
        if held:
            out.append({"status": "warn", "text": M("Optimizing, the file tools and Sync Libraries are held in this library. {why}", why=held)})
        if res.get("error"):
            out.append({"status": "warn", "text": M("The last check failed: {why}", why=res["error"])})
        if not res.get("at"):
            out.append({"status": "info", "text": M("Not compared yet: it’s done in the background, this library’s turn is coming.")})
        else:
            if res.get("mirror"):
                out.append({"status": "info", "text": M("Compared with {library} on the mirror.", library=res["mirror"])})
            out.extend({"status": "info", "text": n} for n in res.get("notes") or [] if not _old_disk_note(n))  # (an old scan's: the red chip says it)
        return out

    def unchecked(self, key: str) -> list[dict]:
        """The checks the last scan of a library couldn't run, a group for each reason — [{"checks": [ids], "label", "why",
        "link"}]: the page's red chip on top, and those checks' counts in red."""
        key = str(key)
        snap = self.snapshot()
        if key not in snap["libraries"]:
            return []
        with self.lock:
            res = dict(self.results.get(key) or {})
        if res.get("v") == FORMAT or not res.get("at"):  # (the other site's files red only while its Plex Cache is known: said to be there since, or found)
            return [u for u in res.get("unchecked") or [] if u.get("link") != PEER_CARD or snap["known"]]
        if not snap["known"]:
            return []
        # a scan from before FORMAT (done again shortly): what it couldn't compare, worked out from what it said
        said = next((n for n in res.get("notes") or [] if _old_disk_note(n)), None)
        why = M("{why}", why=re.sub(r"\s*\(\[[^\]]*\]\([^)]*\)\)", "", str(said))) if said is not None else None  # (its link left out: the chip is it)
        if res.get("disk") is not False and said is None:
            return []
        link = getattr(self.app, "peerlink", None)
        return [{"checks": ["mirror_only_here", "mirror_only_there", "mirror_size_differs"] + ([] if link is not None and link.talking() else ["mirror_sync_files"]),
                 "label": M("Other site’s files not compared"), "why": why or M("The last scan didn’t compare the other site’s files."), "link": PEER_CARD}]

    def stamp(self, key: str) -> tuple:
        """What Library Health's answer takes from here, for its key (App._health_sig): the last check, the link's health,
        the hold, Scan Folder pressed, Sync Libraries' options (which findings are its), the state of the library's
        data (what it can't group) — for a library not compared, only whether a mirror is watched (what it's told), so
        starting a scan, or the link changing, leaves the other libraries' answers as they are."""
        snap = self.snapshot()
        if str(key) not in snap["libraries"]:
            return (False, bool(snap["libraries"]), snap["waiting"])
        return ((snap["at"] or {}).get(str(key)), snap["connection"]["status"], snap["known"], str(key) in snap["held"],
                sum(1 for k in self.scanned if k.startswith(str(key) + "/")), tuple(sorted(self.sync_on(key).items())), self.app.libdata.data(str(key)).stamp)

    def act(self, key: str, item: str, check: str) -> M:
        """Scan Folder on an item's finding: the Plex that doesn't list its files asked to look at their folders. What was
        asked."""
        key, kind = str(key), check[len("mirror_"):] if check.startswith("mirror_") else ""
        with self.lock:
            rows = [r for r in (self.results.get(key) or {}).get("rows") or [] if r.get("item") == str(item) and r.get("kind") == kind]
        if not rows or kind not in ACTION:
            raise ValueError(M("That finding can’t be scanned for: scan again."))
        if ACTION[kind] == "here":
            folders = sorted({r["folder"] for r in rows if r.get("folder")})
            for folder in folders:
                self.app.plex(timeout=20).scan(key, folder)
            said = M("Plex here asked to scan {folder}", folder=folders[0] if folders else "")
        else:
            there = self.app.other.pairs().get(key)
            if there is None:
                raise ValueError(M("This library isn’t paired with one on the mirror any more."))
            folders = sorted({f for r in rows for f in r.get("folders") or []})
            for folder in folders:
                self.app.other.run(lambda c, f=folder: c.scan(there.key, f), timeout=20)
            said = M("The mirror’s Plex asked to scan {folder}", folder=folders[0] if folders else "")
        with self.lock:
            self.scanned[key + "/" + str(item) + "/" + check] = time.time()
        self.save()
        self.app.activity.event("sync", said)
        again = threading.Timer(RECHECK_AFTER, lambda: self.check_async(key))
        again.daemon = True
        again.start()
        return said

    # ------------------------------------------------------------ the hold, the link, the state
    def converting(self, key: str) -> M | None:
        """Why this library waits while the Plex Cache at the other site converts its folder to a child dataset: past its
        copy (Switch, Verify), its folder about to be set aside, or set aside, and its folder sync paused — anything changed
        here meanwhile would reach it the wrong way round. Told by the link, or the marker (peerlink.facts). None otherwise."""
        link = getattr(self.app, "peerlink", None)
        if link is None or not link.mirror_id():
            return None
        from .migrating import HOLD_STEPS, step_name
        f = link.facts()
        conv = (((f or {}).get("libraries") or {}).get(str(key)) or {}).get("converting")
        if not conv or conv.get("step") not in HOLD_STEPS:
            return None
        return M("{name} is converting this library’s folder to a child dataset ({step}): optimizing, the file tools and Sync Libraries wait here "
                 "until it’s done, so nothing reaches its folder while it’s switched.", name=f.get("name") or M("The other site"), step=step_name(conv.get("step")))

    def hold(self, key: str) -> M | None:
        """Why optimizing, the file tools and Sync Libraries wait in this library: the other site switching its folder to a
        child dataset (converting), or the folder sync not carrying .plexcache to the other side (live, from the link),
        unless that finding is ignored (Library Health's Ignore, on the .plexcache item) or its check switched off. None
        otherwise."""
        why = self.converting(key)
        if why:
            return why
        link = getattr(self.app, "peerlink", None)
        if link is None or not link.talking():
            return None
        fs = link.folder_sync().get(str(key))
        if not fs or fs["status"] != "fail":
            return None
        from . import libhealth
        if not libhealth.enabled(self.app.settings.get()).get("mirror_sync_files", True):
            return None
        if "mirror_sync_files" in ((libhealth.ignored(self.app).get(str(key)) or {}).get(SYNC_ITEM) or []):
            return None
        return M("The .plexcache folder doesn’t reach the other site, so its Plex Cache wouldn’t see this side’s claims or markers: the two could optimize the same file, or sync their Plexes over each other. "
                 "Fix the folder sync (Syncthing: don’t ignore .plexcache), or ignore the finding on Mirror Checks.")

    @staticmethod
    def waiting_note(waiting) -> dict:
        """What the page says while what's paired with the mirror isn't known (waiting())."""
        if waiting[0] == "failed":
            return {"status": "warn", "text": M("The mirror’s Plex couldn’t be reached, so what’s paired with it isn’t known yet: {why}", why=waiting[1])}
        return {"status": "info", "text": M("Reading the mirror’s libraries…")}

    def connection(self) -> dict:
        """The link's health, as the page says it: {"status": "ok" | "warn" | "off", "text": M, "lights": [names not ok]}."""
        link = self.app.peerlink
        waiting = self.waiting()
        if waiting:
            n = self.waiting_note(waiting)
            return {"status": "warn" if n["status"] == "warn" else "off", "text": n["text"], "lights": []}
        if not self.libraries():
            return {"status": "off", "text": M("No mirror is watched: mark a library as mirrored on the [Mirror page](/mirrors)."), "lights": []}
        if not link.wanted():
            return {"status": "ok", "text": M("No Plex Cache at the mirror’s site: the disks aren’t compared."), "lights": []}
        bad = [v["text"] for v in link.lights().values() if v["status"] in ("warn", "fail")]
        if bad:
            return {"status": "warn", "text": M("The link to the other Plex Cache needs attention ([System → Mirror Plex Cache](/system#peer)): {lights}. Mirror Checks rely on it, so some checks are left out.",
                                                 lights="; ".join(str(b) for b in bad)), "lights": bad}
        return {"status": "ok", "text": M("Talking to the other Plex Cache."), "lights": []}

    def snapshot(self) -> dict:
        """For the state and Library Health (small): what's running, when each library was last checked, the libraries
        compared, the link's health, the libraries held — worked out at most every few seconds."""
        cached = getattr(self, "_snap", None)
        from . import libhealth
        quiet = libhealth.ignored(self.app) or {}
        key = (self.running, self.checked_at, self.app.settings.rev, tuple(str(lib["key"]) for lib in getattr(self.app, "libraries", []) or []), tuple(sorted((k, tuple((v or {}).get(SYNC_ITEM) or ())) for k, v in quiet.items())))
        if cached and cached[0] == key and time.time() - cached[1] < 15:
            return cached[2]
        libs, waiting = self.paired()
        with self.lock:
            at = {k: (self.results.get(k) or {}).get("at") for k in libs}
        link = getattr(self.app, "peerlink", None)
        got = {"running": self.running, "checked_at": self.checked_at, "at": at, "libraries": libs, "connection": self.connection(),
               "held": [k for k in libs if self.hold(k)], "known": bool(link is not None and link.known()),
               "waiting": waiting}
        self._snap = (key, time.time(), got)
        return got


def _old_disk_note(n) -> bool:
    return str(getattr(n, "tmpl", n) or "").startswith(OLD_DISK_NOTES)


def _back(v):
    """What was saved read back: each message (jsonable's {"_t": …}) an M again, so it's said in the page's language."""
    if isinstance(v, dict):
        return from_json(v) if isinstance(v.get("_t"), str) else {k: _back(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_back(x) for x in v]
    return v


def _said(r: dict) -> M:
    """A finding's text: what was found, of the file (or the folder sync)."""
    kind = r["kind"]
    if kind == "sync_files":
        if r.get("behind"):
            return M("What this side writes in .plexcache reaches the other site {minutes} minutes late.", minutes=r["behind"] // 60)
        return M("What this side writes in .plexcache never reached the other site.")
    if kind == "size_differs":
        return M("{size} here, {size_there} at the other site.", size=size_text(r.get("size") or 0), size_there=size_text(r.get("size_there") or 0))
    return {
        "only_here": M("On disk here, not at the other site."),
        "only_there": M("On disk at the other site, not here."),
        "not_listed_there": M("Plex here lists it; the mirror’s Plex doesn’t."),
        "only_listed_there": M("The mirror’s Plex lists it; Plex here doesn’t, and it isn’t on disk here."),
        "unlisted_here": M("On disk here, and Plex here doesn’t list it."),
        "missing_here": M("Plex here lists it, and it isn’t on disk."),
    }[kind]

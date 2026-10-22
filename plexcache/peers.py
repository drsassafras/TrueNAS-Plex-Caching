"""Two Plex Caches on servers that share library folders (synced both ways): what each must know
about the other so their settings don't fight.

Each system marks itself in every library folder it shares — .plexcache/systems/<instance>.json,
written by the optimizer service (the one part that may write there) and carried to the other
site by the folder sync. The marker holds the settings that matter to the other side: how Library
Sync is set (does it apply, and which server is its source), the optimizer choices that change a
file's content (cover art, tags, MKV layout, converting), and the file tools lately run there.

From the other systems' markers this module tells:
* **Sync direction** — both sides applying with themselves as the source (or both with the other)
  would send edits back and forth: applying is refused here until one side is the source.
* **Optimizer choices** — different cover-art, tag or layout choices on the two sides would
  rewrite each other's files for ever: optimizing that library is refused here while they differ.
* **File tools** — a tool the other side ran on a folder lately (Duplicate Versions, Organize
  Folders): this side's run of one waits, since both would move the same files.
Health lists all of it. Without the optimizer service nothing is published, and that's said too.
"""

from __future__ import annotations

import json
import os
import time

from .i18n import M
from .optimize.claims import PEER_DAYS

PUBLISH_EVERY = 30 * 60
TOOL_GAP = 3600  # a file tool run on the other side within this: ours waits
FILE_TOOLS = ("versions", "folders")
CONTENT = ("mkv", "images", "tags", "convert")  # the optimizer choices that change a file's bytes


def _folders(app) -> list[tuple[str, str, str]]:
    """(library key, Plex path, local path) for every library folder."""
    out = []
    for lib in getattr(app, "libraries", []) or []:
        for loc in lib.get("locations") or []:
            loc = loc.rstrip("/")
            out.append((str(lib["key"]), loc, app.map_path(loc)))
    return out


def info(app, key: str) -> dict:
    """What this system tells the other systems sharing a library folder (its marker's body)."""
    s = app.settings.get()
    sy, p2 = s.get("sync") or {}, s.get("plex2") or {}
    opts = app.optimizing.options(key)
    tools = (getattr(app, "tool_runs", None) or {}).get(str(key)) or {}
    return {"name": getattr(app, "plex_name", "") or "", "version": app.version, "at": time.time(),
            "sync": {"mode": sy.get("mode"), "source": sy.get("source"), "mirror": p2.get("machine_id") or "", "mirrors": sy.get("mirrors")},
            "optimizer": {k: opts.get(k) for k in CONTENT},
            "tools": {name: at for name, at in tools.items() if time.time() - at < 7 * 86400}}


def publish(app) -> int:
    """Our marker written into every library folder, through the optimizer service. How many."""
    client = getattr(getattr(app, "optimizing", None), "client", None)
    if client is None or not getattr(client, "configured", False):
        return 0
    n = 0
    for key, _plex, local in _folders(app):
        try:
            client.mark(local, app.optimizing.instance(), info(app, key))
            n += 1
        except Exception:  # noqa: BLE001 — a folder not writable, the service away: said by Health elsewhere
            continue
    return n


def read(app) -> dict:
    """The other systems' markers: {instance: {"marker": {...}, "libraries": [library keys sharing a folder with it]}}."""
    mine = app.optimizing.instance() if getattr(app, "optimizing", None) else ""
    out: dict = {}
    now = time.time()
    for key, _plex, local in _folders(app):
        sdir = os.path.join(local, ".plexcache", "systems")
        try:
            names = os.listdir(sdir)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".json") or n[:-5] == mine:
                continue
            p = os.path.join(sdir, n)
            try:
                if now - os.stat(p).st_mtime > PEER_DAYS * 86400:
                    continue
                with open(p, encoding="utf-8") as fh:
                    marker = json.load(fh)
            except (OSError, ValueError):
                continue
            e = out.setdefault(n[:-5], {"marker": {}, "libraries": [], "by_library": {}})
            e["marker"] = marker if marker.get("at", 0) >= e["marker"].get("at", 0) else e["marker"]
            e["by_library"][key] = marker
            if key not in e["libraries"]:
                e["libraries"].append(key)
    return out


def sync_conflict(app, found: dict | None = None) -> M | None:
    """Why applying Library Sync here would fight the other side, or None."""
    s = app.settings.section("sync")
    if s.get("mode") != "apply":
        return None
    for inst, e in (found if found is not None else read(app)).items():
        other = (e["marker"].get("sync") or {})
        if other.get("mode") != "apply":
            continue
        name = e["marker"].get("name") or inst
        if other.get("source") == s.get("source"):  # both "this", or both "mirror": edits would go round and round
            return M("{name} applies Library Sync too, with the same side as its source: the two would send edits back and forth. One side must be the source — set this side’s source to {fix}, or turn applying off there.",
                     name=name, fix=M("the mirror") if s.get("source") == "this" else M("this server"))
    return None


def optimizer_conflict(app, key: str, found: dict | None = None) -> M | None:
    """Why optimizing library ``key`` here would fight the other side, or None."""
    mine = app.optimizing.options(key)
    for inst, e in (found if found is not None else read(app)).items():
        marker = e.get("by_library", {}).get(str(key))
        if not marker:
            continue
        theirs = marker.get("optimizer") or {}
        differ = [k for k in CONTENT if k in theirs and theirs[k] != mine.get(k)]
        if differ:
            return M("{name} optimizes this library with other choices ({which}): each side would rewrite the other’s files, over and over. Make the choices the same on both sides first.",
                     name=marker.get("name") or inst, which=", ".join(differ))
    return None


def tool_conflict(app, key: str, tool: str, found: dict | None = None) -> M | None:
    """Why a file tool shouldn't run on library ``key`` here now, or None."""
    if tool not in FILE_TOOLS:
        return None
    for inst, e in (found if found is not None else read(app)).items():
        marker = e.get("by_library", {}).get(str(key))
        if not marker:
            continue
        for name, at in (marker.get("tools") or {}).items():
            if name in FILE_TOOLS and time.time() - float(at or 0) < TOOL_GAP:
                return M("{name} ran {tool} on this library {when} minutes ago: wait until its changes have synced here (an hour), so the two don’t move the same files.",
                         name=marker.get("name") or inst, tool=name, when=int((time.time() - float(at)) / 60))
    return None


def summary(app) -> dict:
    """For Health and the state: the other systems, and what fights."""
    found = read(app)
    out = {"systems": [{"instance": i, "name": e["marker"].get("name") or i, "version": e["marker"].get("version"), "libraries": e["libraries"],
                        "sync": e["marker"].get("sync"), "at": e["marker"].get("at")} for i, e in found.items()],
           "publishing": bool(getattr(getattr(app, "optimizing", None), "client", None) and app.optimizing.client.configured),
           "sync_conflict": sync_conflict(app, found), "optimizer_conflicts": {}}
    for lib in getattr(app, "libraries", []) or []:
        why = optimizer_conflict(app, str(lib["key"]), found)
        if why:
            out["optimizer_conflicts"][str(lib["key"])] = why
    return out

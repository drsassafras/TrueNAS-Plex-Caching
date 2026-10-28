"""Two Plex Caches on servers that share library folders (synced both ways): what each must know
about the other so their settings don't fight.

Each system marks itself in every library folder it shares — .plexcache/systems/<instance>.json,
written by the optimizer service (the one part that may write there) and carried to the other
site by the folder sync. The marker holds the settings that matter to the other side: whose edits
win in Sync Libraries (tools/sync.py), per library, the optimizer choices that change a file's content
(cover art, tags, MKV layout, converting), and the tools lately run there.

From the other systems' markers this module tells:
* **Sync direction** — both sides running Sync Libraries on a library with themselves as the source (or
  both with the other) would send edits back and forth: its Apply is refused here until one side is the
  source.
* **Optimizer choices** — different cover-art, tag or layout choices on the two sides would
  rewrite each other's files for ever: optimizing that library is refused here while they differ.
* **File tools** — a tool the other side ran on a folder lately (Duplicate Versions, Organize
  Folders): this side's run of one waits, since both would move the same files.
Health lists all of it. Without the optimizer service nothing is published, and that's said too.
"""

import json
import os
import time

from .i18n import M
from .optimize.claims import PEER_DAYS

PUBLISH_EVERY = 30 * 60
TOOL_GAP = 3600  # a file tool run on the other side within this: ours waits
FILE_TOOLS = ("versions", "folders", "unlink", "convert")
TOLD_TOOLS = FILE_TOOLS + ("sync",)  # the tools whose runs the marker tells (Sync Libraries': which way it went, for sync_conflict)
SYNC_DAYS = 7  # Sync Libraries run on the other side within this: running it here the other way round fights it
CONTENT = ("mkv", "images", "tags")  # the optimizer choices that change a file's bytes


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
    link = getattr(app, "peerlink", None)
    return {"name": getattr(app, "plex_name", "") or "", "version": app.version, "at": time.time(),
            "sync": {"source": _source(app, key), "mirror": p2.get("machine_id") or "", "mirrors": app.other.state_of(str(key))},  # (the library's state here: unset, none or mirror)
            "optimizer": {k: opts.get(k) for k in CONTENT},
            "tools": {name: at for name, at in tools.items() if time.time() - at < 7 * 86400},
            **({"peer": link.advert(), "plex": link.own_plex_id()} if link is not None else {}),  # (how the other Plex Cache finds and pairs with this one: peerlink.py)
            # what it runs on, and a folder being converted to a child dataset (migrating.py): for the other side even unpaired
            **_conversion_facts(app, key)}


def _conversion_facts(app, key: str) -> dict:
    from .peerlink import own_facts, own_library
    try:
        return dict(own_facts(app), **own_library(app, str(key)))
    except Exception:  # noqa: BLE001 — not worked out: not said
        return {}


def publish(app) -> int:
    """Our marker written into every library folder, through the optimizer service. How many."""
    client = getattr(getattr(app, "optimizing", None), "client", None)
    if client is None or not getattr(client, "configured", False):
        return 0
    n = 0
    published = getattr(app, "published_markers", None)
    first = getattr(app, "first_published", None)
    for key, _plex, local in _folders(app):
        try:
            body = info(app, key)
            client.mark(local, app.optimizing.instance(), body)
            n += 1
        except Exception:  # noqa: BLE001 — a folder not writable, the service away: said by Health elsewhere
            continue
        if isinstance(published, dict):  # (when, so the other side can say whether it arrived: peerlink.folder_sync)
            published[key] = body["at"]
        if isinstance(first, dict) and key not in first:
            first[key] = body["at"]
            app._save_json("published.json", first)
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


def _source(app, key: str) -> str | None:
    """Whose edits win in Sync Libraries on a library here: "this" or "mirror" (None: no such tool)."""
    tool = (getattr(app, "tools", None) or {}).get("sync")
    return tool.enabled(str(key)).get("source") if tool is not None else None


def sync_conflict(app, key: str, found: dict | None = None) -> M | None:
    """Why running Sync Libraries on library ``key`` here would fight the other side, or None: the other side ran it on the
    library lately (SYNC_DAYS) with the same side as its source — both "this", or both "mirror" — so each run would undo the
    other's edits."""
    mine = _source(app, key)
    if mine is None:
        return None
    for inst, e in (found if found is not None else read(app)).items():
        marker = e.get("by_library", {}).get(str(key))
        if not marker:
            continue
        other = marker.get("sync") or {}
        ran = (marker.get("tools") or {}).get("sync")
        if not (ran and time.time() - float(ran) < SYNC_DAYS * 86400) and other.get("mode") != "apply":  # (an older Plex Cache: applying on a timer)
            continue
        if other.get("source") == mine:
            return M("{name} syncs this library too, with the same side’s edits winning: the two would send edits back and forth. One side’s edits must win on both — set Whose edits win to {fix} here, or stop syncing it there.",
                     name=marker.get("name") or inst, fix=M("The mirror") if mine == "this" else M("This server"))
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
    """Why a tool shouldn't run on library ``key`` here now, or None: a file tool the other side ran there lately, or Sync
    Libraries the other way round (sync_conflict)."""
    if tool == "sync":
        return sync_conflict(app, key, found)
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
                        "sync": e["marker"].get("sync"), "tools": e["marker"].get("tools") or {}, "at": e["marker"].get("at")} for i, e in found.items()],
           "publishing": bool(getattr(getattr(app, "optimizing", None), "client", None) and app.optimizing.client.configured),
           "sync_conflicts": {}, "optimizer_conflicts": {}}
    for lib in getattr(app, "libraries", []) or []:
        why = optimizer_conflict(app, str(lib["key"]), found)
        if why:
            out["optimizer_conflicts"][str(lib["key"])] = why
        why = sync_conflict(app, str(lib["key"]), found)
        if why:
            out["sync_conflicts"][str(lib["key"])] = why
    return out

"""Before a tool changes files: is there room for what it writes, and is there a recent snapshot of
the dataset to fall back on? Two lights per library, both green before Apply opens.

* **Space**: the free space on each of the library's folders (statvfs), against what the action
  would write (each tool says, from its scan), with a margin kept clear (MARGIN).
* **Backup**: what the process does first, per the Backup preference (System → Preferences): a
  snapshot through the API (pool.snapshot.create on TrueNAS 25.04 and later, zfs.snapshot.create
  before: both are tried), kept general.backup_days and named for the action; the copies the action
  keeps of its own, where it does; or none, where you back up yourself. The light says the plan;
  a snapshot that couldn't be taken aborts the process, and is kept as an error until the next try.
"""

from __future__ import annotations

import os
import time

from .i18n import M

FRESH = 30 * 60          # a snapshot (or a backup you made) this recent counts
MARGIN = 1 << 30         # free space kept clear beyond what's written (1 GiB)
LIFE = 30 * 86400        # how long a backup — a snapshot the app takes, files set aside — is kept (the default of general.backup_days), then deleted by the app itself
# The snapshot methods, by TrueNAS version: pool.snapshot.* from 25.04, zfs.snapshot.* before. The first that TrueNAS
# knows is used, and remembered.
SNAPSHOT_APIS = ("pool.snapshot", "zfs.snapshot")
SNAPS = "snapshots.json"  # the snapshots the app took: {"name", "dataset", "at", "library", "tool"}


def _roots(app, key: str) -> list[dict]:
    """The library's folders as this app has them: [{"plex", "local", "dataset"}]."""
    out = []
    lib = app.library_info(key) or {}
    dsets = app.library_datasets(key)
    for loc in lib.get("locations") or []:
        local = app.map_path(loc.rstrip("/"))
        ds = next((d for d in dsets if local.startswith(str(d.get("mountpoint") or "/\\0").rstrip("/"))), None)
        out.append({"plex": loc, "local": local, "dataset": (ds or {}).get("dataset") or "", "mountpoint": (ds or {}).get("mountpoint") or ""})
    return out


def space(app, key: str, needed: int) -> dict:
    """{"status": "ok" | "warn", "free", "needed", "text"}: the least free space across the library's folders."""
    free = None
    for r in _roots(app, key):
        try:
            st = os.statvfs(r["local"])
            f = st.f_bavail * st.f_frsize
            free = f if free is None else min(free, f)
        except OSError:
            continue
    if free is None:
        return {"status": "warn", "free": None, "needed": needed, "text": M("The library’s folders can’t be read here: free space unknown.")}
    ok = free >= needed + MARGIN
    return {"status": "ok" if ok else "warn", "free": free, "needed": needed,
            "text": M("{free} free; this writes about {needed}.", free=_gb(free), needed=_gb(needed)) if ok
            else M("Only {free} free, and this writes about {needed} (a {margin} margin is kept): make room first.", free=_gb(free), needed=_gb(needed), margin=_gb(MARGIN))}


def life(app) -> int:
    """How long a backup is kept, in seconds (general.backup_days): a snapshot the app takes, files set aside or copied."""
    try:
        return int(app.settings.section("general").get("backup_days") or LIFE // 86400) * 86400
    except (TypeError, ValueError):
        return LIFE


def backup_kind(app) -> str:
    """The Backup preference: "snapshot", "manual" or "copies"."""
    kind = str(app.settings.section("general").get("backup") or "snapshot")
    return kind if kind in ("snapshot", "manual", "copies") else "snapshot"


def snap_call(app, tn, what: str, params: list):
    """``<api>.<what>`` on TrueNAS, with the API this TrueNAS has (SNAPSHOT_APIS, the first that exists)."""
    known = getattr(app, "_snapshot_api", None)
    if not known:  # (what the TrueNAS release offers, as read when it was connected: the right name first time)
        offered = (app.zfs or {}).get("truenas_methods") if getattr(app, "zfs", None) else None
        known = next((api for api in SNAPSHOT_APIS if f"{api}.{what}" in (offered or ())), None)
    apis = (known,) if known else SNAPSHOT_APIS
    last = None
    for api in apis:
        try:
            out = tn.call(f"{api}.{what}", params)
        except Exception as e:  # noqa: BLE001
            last = e
            if api != apis[-1] and "exist" in str(e).lower():  # (this TrueNAS hasn't got it: the other name)
                continue
            raise
        app._snapshot_api = api
        return out
    raise last  # type: ignore[misc]


def _gb(n: int | None) -> str:
    n = n or 0
    return f"{n / 1e9:.1f} GB" if n >= 1e8 else f"{n / 1e6:.0f} MB"


def newest_snapshot(app, key: str) -> tuple[float | None, str]:
    """(when, name) of the newest snapshot of the library's datasets: from .zfs/snapshot under the
    folders mounted here, and from TrueNAS's API when it's connected."""
    best: tuple[float | None, str] = (None, "")
    for r in _roots(app, key):
        for base in {r["local"], r["mountpoint"]}:
            if not base:
                continue
            sdir = os.path.join(base, ".zfs", "snapshot")
            try:
                names = os.listdir(sdir)
            except OSError:
                continue
            for n in names:
                try:
                    at = os.lstat(os.path.join(sdir, n)).st_mtime
                except OSError:
                    continue
                if best[0] is None or at > best[0]:
                    best = (at, n)
    t = app.settings.section("truenas")
    if t.get("url") and t.get("api_key"):
        names = sorted({r["dataset"] for r in _roots(app, key) if r["dataset"]})
        if names:
            try:
                with app.truenas() as tn:
                    for ds in names:
                        for snap in snap_call(app, tn, "query", [[["dataset", "=", ds]], {"select": ["name", "properties"]}]) or []:
                            raw = ((snap.get("properties") or {}).get("creation") or {})
                            at = raw.get("rawvalue") if isinstance(raw, dict) else raw
                            try:
                                at = float(at)
                            except (TypeError, ValueError):
                                continue
                            if best[0] is None or at > best[0]:
                                best = (at, str(snap.get("name") or ""))
            except Exception:  # noqa: BLE001 — not allowed, or an older TrueNAS: the folder's word stands
                pass
    return best


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in str(text or "")).strip("-").lower()
    while "--" in out:
        out = out.replace("--", "-")
    return out[:24] or "library"


def taken(app) -> list[dict]:
    if getattr(app, "_snapshots", None) is None:
        app._snapshots = [s for s in (app._load_json(SNAPS) or []) if isinstance(s, dict)]
    return app._snapshots


def take_snapshot(app, key: str, tool: str = "") -> list[str]:
    """A snapshot of each of the library's datasets, through TrueNAS, named for what it's for —
    plexcache-<tool>-<library>-<time> — and noted, so the app deletes it after life(). The names made.
    What went wrong is kept (errors()) and logged, until a snapshot is taken or the error dismissed."""
    try:
        made = _take(app, key, tool)
    except ValueError as e:
        errors(app)[str(key)] = {"at": time.time(), "text": e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))}
        app.activity.event("tools", M("snapshot not taken ({library}): {why}", library=(app.library_info(key) or {}).get("title") or key, why=e.args[0] if e.args else str(e)), level="warn")
        raise
    errors(app).pop(str(key), None)
    return made


def _take(app, key: str, tool: str) -> list[str]:
    t = app.settings.section("truenas")
    if not (t.get("url") and t.get("api_key")):
        raise ValueError(M("Snapshots are taken through the TrueNAS API: connect it in [System → TrueNAS](/system#truenas), with a key allowed to make snapshots — or make a backup yourself and choose that in [System → Preferences](/system#prefs)."))
    names = sorted({r["dataset"] for r in _roots(app, key) if r["dataset"]})
    if not names:
        raise ValueError(M("The library’s dataset isn’t known: run the Health checks with TrueNAS connected first."))
    made = []
    title = (app.library_info(key) or {}).get("title") or key
    stamp = "plexcache-" + (_slug(tool) + "-" if tool else "") + _slug(title) + time.strftime("-%Y%m%d-%H%M")
    try:
        with app.truenas() as tn:
            for ds in names:
                snap_call(app, tn, "create", [{"dataset": ds, "name": stamp}])
                made.append(f"{ds}@{stamp}")
    except Exception as e:  # noqa: BLE001
        raise ValueError(M("TrueNAS didn’t take the snapshot: {why}", why=str(e))) from None
    now = time.time()
    taken(app).extend({"name": n, "dataset": n.split("@")[0], "at": now, "library": str(key), "tool": tool} for n in made)
    app._save_json(SNAPS, taken(app))
    app.activity.event("tools", M("snapshot taken before changing files: {names} (kept {days} days)", names=", ".join(made), days=life(app) // 86400))
    return made


def errors(app) -> dict:
    """The last snapshot that couldn't be taken, per library: {"at", "text"} — shown until the next one is, or it's dismissed."""
    if getattr(app, "_snapshot_errors", None) is None:
        app._snapshot_errors = {}
    return app._snapshot_errors


def prune_snapshots(app) -> int:
    """The snapshots the app took, deleted once life() is up (TrueNAS keeps a snapshot for ever otherwise). How many went."""
    old = [s for s in taken(app) if time.time() - float(s.get("at") or 0) > life(app)]
    if not old:
        return 0
    t = app.settings.section("truenas")
    if not (t.get("url") and t.get("api_key")):
        return 0
    gone = []
    try:
        with app.truenas() as tn:
            for s in old:
                try:
                    snap_call(app, tn, "delete", [s["name"]])
                except Exception as e:  # noqa: BLE001 — gone already, or refused: tried again later
                    app.activity.event("tools", M("snapshot {name} not deleted: {why}", name=s["name"], why=str(e)), level="warn")
                    continue
                gone.append(s["name"])
    except Exception:  # noqa: BLE001
        return 0
    app._snapshots = [s for s in taken(app) if s["name"] not in gone]
    app._save_json(SNAPS, app._snapshots)
    if gone:
        app.activity.event("tools", M("{n} snapshots the app took, {days} days old, deleted: {names}", n=len(gone), days=life(app) // 86400, names=", ".join(gone)))
    return len(gone)


def rollback(app, names: list[str]) -> list[str]:
    """The datasets rolled back to the snapshots named, through TrueNAS: everything written to them since is gone — the
    whole dataset as it was, not only the files a run changed. The snapshots rolled back."""
    t = app.settings.section("truenas")
    if not (t.get("url") and t.get("api_key")):
        raise ValueError(M("Rolling back is done through the TrueNAS API: connect it in [System → TrueNAS](/system#truenas)."))
    done = []
    try:
        with app.truenas() as tn:
            for n in names:
                snap_call(app, tn, "rollback", [n, {"force": True}])
                done.append(n)
    except Exception as e:  # noqa: BLE001
        raise ValueError(M("TrueNAS didn’t roll back {name}: {why}", name=names[len(done)] if len(done) < len(names) else "", why=str(e))) from None
    app.activity.event("tools", M("rolled back to {names}: the datasets as they were then", names=", ".join(done)), level="warn")
    return done


def snapshot(app, key: str, keeps: bool = False) -> dict:
    """{"status": "ok" | "warn" | "off", "at", "name", "text"}: the newest snapshot of the library's dataset within FRESH
    (what's there already, before the process takes its own)."""
    at, name = newest_snapshot(app, key)
    now = time.time()
    if at and now - at < FRESH:
        return {"status": "ok", "at": at, "name": name, "text": M("Snapshot {name}, {m} minutes ago.", name=name, m=int((now - at) / 60))}
    return {"status": "warn", "at": at, "name": name,
            "text": M("The newest snapshot is {name}, {h} hours old.", name=name, h=int((now - at) / 3600)) if at else M("No snapshot of the library’s dataset is in reach.")}


def plan(app, key: str, keeps: bool = False, tool: str = "") -> dict:
    """What the process does for a backup before it changes files, per the Backup preference (System → Preferences):
    {"kind": "snapshot" | "copies" | "manual" | "none", "status": "ok" | "off" | "warn", "text", "label"} — ``label``: what the
    Apply button says. A snapshot is the process's first step; the copies an action keeps are its own; "manual" says you
    back up yourself, so Apply goes ahead without one; "none" blocks: no backup is possible as things are."""
    how = backup_kind(app)
    can = bool(app.settings.section("truenas").get("api_key")) and any(r["dataset"] for r in _roots(app, key))
    days = life(app) // 86400
    title = (app.library_info(key) or {}).get("title") or key
    name = "plexcache-" + (_slug(tool) + "-" if tool else "") + _slug(title) + "-<time>"
    if how == "copies" and keeps:
        return {"kind": "copies", "status": "off", "label": "backup", "days": days,
                "text": M("[The copies this keeps are the backup](/system#prefs): put back from its page or from the [history](/processes) of the job.")}
    if how == "manual":
        return {"kind": "manual", "status": "off", "label": "none", "days": days,
                "text": M("[You back up yourself](/system#prefs), as chosen: Apply goes ahead without one.")}
    if can:  # (a snapshot: the preference, or the fallback where the action keeps no copies of its own)
        return {"kind": "snapshot", "status": "ok", "label": "backup", "days": days, "name": name,
                "text": M("[A snapshot is taken first](/system#prefs), then the files change. It can be reverted through TrueNAS, or from the [history](/processes) of the job.")
                if how == "snapshot" else M("This action keeps no copies of its own, so [a snapshot is taken first](/system#prefs), then the files change. It can be reverted through TrueNAS, or from the [history](/processes) of the job.")}
    return {"kind": "none", "status": "warn", "label": "none", "days": days,
            "text": M("No backup is possible as things are: a snapshot needs TrueNAS connected in [System → TrueNAS](/system#truenas) with a key allowed to take one — or choose a backup you make yourself in [System → Preferences](/system#prefs).")}


def readiness(app, key: str, needed: int | str = 0, keeps: bool = False, retained: int = 0, tool: str = "") -> dict:
    """The lights before a file-changing action, and whether it may go ahead: room for what it writes — and, under a
    snapshot, for what the snapshot keeps of the files it rewrites or deletes (``retained``) until it expires — and the
    backup plan (plan()). ``needed``: bytes, or "largest" — the library's largest file (what a rewrite needs room for)."""
    if needed == "largest":
        needed = max((getattr(f, "size", 0) or 0 for f in list(app.files) if str(f.library_key) == str(key)), default=0)
    bp = plan(app, key, keeps, tool)
    total = int(needed or 0) + (int(retained or 0) if bp["kind"] == "snapshot" else 0)
    sp = space(app, key, total)
    if bp["kind"] == "snapshot" and retained and sp["status"] == "ok":
        sp["text"] = M("{free} free; this writes about {needed}, and the snapshot keeps the {retained} of files it changes until it expires.",
                       free=_gb(sp["free"]), needed=_gb(int(needed or 0)), retained=_gb(int(retained)))
    return {"library": str(key), "space": sp, "backup": bp, "snapshot": snapshot(app, key, keeps), "ready": sp["status"] == "ok" and bp["status"] != "warn",
            "can_snapshot": bool(app.settings.section("truenas").get("api_key")) and any(r["dataset"] for r in _roots(app, key)),
            "kind": backup_kind(app), "keeps": bool(keeps), "error": errors(app).get(str(key)), "fresh_minutes": FRESH // 60, "life_days": life(app) // 86400}


def why_not(ready: dict):
    """One sentence for a refusal: the light that isn't green."""
    return ready["space"]["text"] if ready["space"]["status"] != "ok" else ready["backup"]["text"]


def snapshot_path(app, key: str, local: str, snapshot_name: str) -> str | None:
    """Where a file sits inside a snapshot of its dataset: <mountpoint>/.zfs/snapshot/<name>/<its path within the dataset>."""
    for r in _roots(app, key):
        mp = r.get("mountpoint") or ""
        ds = r.get("dataset") or ""
        if mp and ds and (local == mp or local.startswith(mp.rstrip("/") + "/")) and snapshot_name.split("@")[0] == ds:
            return os.path.join(mp, ".zfs", "snapshot", snapshot_name.split("@", 1)[1], local[len(mp.rstrip("/")) + 1:])
    return None

"""Health checks and the recommendations that steer the Caching page."""

import os
import time

from . import zfs
from .i18n import Fixed, M, join
from .optimizing import HEALTHCHECK_YAML
from .zfslog import grade as compression_grade
from .warmup import recommend_recordsize

GB = 1e9
GiB = 1024 ** 3


def _fmt_bytes(n: float | None) -> M:
    if n is None:
        return M("unknown")
    for unit, size in ((M("{n} TB"), 1e12), (M("{n} GB"), 1e9), (M("{n} MB"), 1e6)):
        if n >= size:
            return M(unit.tmpl, n=Fixed(n / size, 1 if n < 10 * size else 0))
    return M("{n} B", n=round(n))


def item(status: str, title: str, detail: str = "", fix: str = "", fix_cmd: str = "", help: str = "") -> dict:
    """One Health result. ``help``: a longer explanation, behind an ⓘ next to the title."""
    out = {"status": status, "title": title, "detail": detail, "fix": fix, "fix_cmd": fix_cmd}
    if help:
        out["help"] = help
    return out


STATUS_MAP = {"ok": "pass", "warn": "warn", "fail": "fail"}


def check_plex(app) -> dict:
    c = app.connections or {}
    items = []
    for key in ("plex_server", "plex_token"):
        r = c.get(key)
        if r and r["status"] in STATUS_MAP:
            items.append(item(STATUS_MAP[r["status"]], r["text"], r["detail"],
                              "" if r["status"] == "ok" else M("Open [System → Plex](/system#plex).")))
    if c.get("plex_token", {}).get("status") == "ok":
        if app.scan_error:
            items.append(item("fail", M("Couldn’t list libraries"), app.scan_error))
        elif app.libraries:
            n = sum(1 for lib in app.libraries if lib["kind"] and not lib["excluded"])
            items.append(item("pass", M("Libraries found"), M("{n} libraries, {files} media files", one="{n} library, {files} media files",
                                                             n=n, files=len(app.files))))
        mon = app.monitor
        if mon.last_error:
            items.append(item("warn", M("Now-playing feed"), mon.last_error))
        elif mon.last_poll:
            poll = app.settings.section("general")["poll_seconds"]
            items.append(item("pass", M("Now-playing feed"),
                              M("Checking what’s playing every {n} s", n=poll)))
    return {"id": "plex", "title": "Plex", "hint": M("Where the app finds your libraries and sees what’s playing."),
            "items": items}


# A ticked library is one you asked to cache, so a folder that can't be read is an error.
LOC_STATUS = {"ok": "pass", "missing": "fail", "empty": "fail", "unreadable": "fail"}


def check_media(app) -> dict:
    by_lib: dict[str, dict] = {}
    for loc in app.locations:
        if loc.get("excluded"):
            continue
        row = by_lib.setdefault(loc["key"], {"library": loc["library"], "key": loc["key"], "folders": [],
                                             "status": "pass"})
        st = LOC_STATUS[loc["status"]]
        row["folders"].append({"plex_path": loc["plex_path"], "local_path": loc["local_path"], "status": st,
                               "detail": loc["detail"], "found_at": loc.get("found_at", [])})
        rank = {"pass": 0, "info": 0, "warn": 1, "fail": 2}
        if rank[st] > rank[row["status"]]:
            row["status"] = st
    rows = []
    for lib in app.libraries:
        if not lib["kind"]:
            continue
        if lib["excluded"]:
            rows.append({"library": lib["title"], "key": lib["key"], "count": 0, "status": "info",
                         "folders": [], "detail": M("Not cached — unticked in [System → Media Mappings](/system#mappings)"),
                         "badge": M("Disabled")})
            continue
        row = by_lib.get(lib["key"], {"library": lib["title"], "key": lib["key"], "folders": [], "status": "pass"})
        row["count"] = lib["count"]
        bad = [f for f in row["folders"] if f["status"] != "pass"]
        if bad:
            found = [f for f in bad if f["found_at"]]
            row["detail"] = bad[0]["detail"]
            if found:
                row["fix"] = M("Found “{library}” files at {path} — choose that folder in [System → Media Mappings](/system#mappings).",
                               library=lib["title"], path=found[0]["found_at"][0])
            else:
                row["fix"] = M("Fix its folder in [System → Media Mappings](/system#mappings), or untick it there to stop caching it.")
        else:
            row["detail"] = M("Files readable")
        rows.append(row)
    items = []
    # Record size is a column of the table: its info box says what to do about a warning, so it
    # isn't repeated as an item below it. (Compression is per dataset: Media Datasets.)
    for row in rows:
        row["record"] = library_record(app, row["key"])
    if not rows:
        items.append(item("info", M("No libraries checked yet"), M("Connect Plex first.")))
    return {"id": "media", "title": M("Libraries"),
            "hint": M("For each library folder, a real file is opened through this app’s mounts."),
            "items": items, "table": rows}


def _libs(d: dict):
    return join(d["libraries"])


def cell(status: str, text: str, badge: str | None = None) -> dict:
    """One check in the datasets table. ``badge`` replaces the usual Pass/Warn/Info label."""
    out = {"status": status, "text": text}
    if badge:
        out["badge"] = badge
    return out


def _dataset_row(app, d: dict, pools: dict, tn_ok: bool) -> dict:
    """One row of the Media Datasets table. Problems carry their fix in ``fixes``."""
    s = app.settings.get()
    any_on = s["nowplaying"]["enabled"] or app.prefetch_active() or app.warmup_active()
    l2_wanted = not s["general"]["no_l2arc"]  # caching is planned around an L2ARC unless switched off
    name = d["dataset"] or d["mountpoint"]
    row = {"dataset": name, "mountpoint": d["mountpoint"], "pool": d["pool"], "libraries": d["libraries"],
           "off": d["off"], "keys": d["keys"], "in_use": None, "cache": None, "recordsize": None, "file_rs": None, "compression": None, "l2arc": None,
           "fixes": []}
    fixes = row["fixes"]

    # --- what uses it
    if d["libraries"]:
        text = (M("{list} · not cached: {off}", list=d["libraries"], off=d["off"]) if d["off"]
                else M("{list}", list=d["libraries"]))
        row["in_use"] = cell("pass", text)
    elif d["off"]:
        # Nothing is cached from it, so its cache settings and L2ARC don't matter here.
        # Its record size still does: that affects how well Plex itself reads the files.
        row["in_use"] = cell("info", M("{list} · caching off", list=d["off"]))
    else:
        row["in_use"] = cell("warn", M("No Plex library"))
        fixes.append(item("warn", M("{name} isn’t used by any Plex library", name=name),
                          M("It’s mounted in this app at {path}, but none of Plex’s library folders are in it, "
                            "so nothing there is ever cached.", path=d["mountpoint"]),
                          M("If a library should be using it, check where that library’s folder is in [System → Media "
                            "Mappings](/system#mappings). Otherwise remove the mount from this app’s volumes (Apps → plex-cache → Edit).")))
        return row
    libs = _libs(d) if d["libraries"] else M("{list}", list=d["off"])
    cached = bool(d["libraries"])

    if not d["dataset"]:
        if not cached:
            return row
        row["cache"] = cell("warn", M("Not ZFS ({fstype})", fstype=d["fstype"]))
        fixes.append(item("warn", M("{libraries}: not on ZFS here", libraries=libs),
                          M("{path} is a {fstype} mount, so ZFS caching can’t be checked or used for {libraries}.",
                            path=d["mountpoint"], fstype=d["fstype"], libraries=libs),
                          M("Mount the dataset directly as a host path (not via SMB/NFS).")))
        return row
    ds = d["dataset"]
    props = d.get("props")
    pool = pools.get(d["pool"])

    # The record size: from TrueNAS, or failing that from the dataset itself (never guessed)
    rs = (props or {}).get("recordsize") if tn_ok else None
    rs = rs or (zfs.recordsize(d.get("mountpoint")) if d.get("fstype") == "zfs" else None)
    # --- primarycache / secondarycache
    if not tn_ok or props is None:
        row["cache"] = cell("info", M("Not checked")) if cached else None
    elif not cached:
        pass  # only the record size below
    else:
        prim, sec = props.get("primarycache"), props.get("secondarycache")
        one = len(d["libraries"]) == 1
        if prim is None or sec is None:
            missing = join([n for n, v in (("primarycache", prim), ("secondarycache", sec)) if v is None])
            said = "; ".join(d.get("props_debug") or [])
            row["cache"] = cell("info", M("Couldn’t read"))
            fixes.append(item("info", M("Cache settings for {dataset}: couldn’t read", dataset=ds),
                              M("TrueNAS didn’t return {what} for {dataset} ({libraries}), so this wasn’t checked. (TrueNAS answered — {said})",
                                what=missing, dataset=ds, libraries=libs, said=said) if said else
                              M("TrueNAS didn’t return {what} for {dataset} ({libraries}), so this wasn’t checked.",
                                what=missing, dataset=ds, libraries=libs),
                              M("You can check by hand in the TrueNAS shell:"),
                              f"sudo zfs get primarycache,secondarycache,recordsize {ds}"))
        elif prim != "all":
            # ZFS fills L2ARC only from data that passed through RAM (ARC). With
            # primarycache=metadata file data never enters RAM, so nothing the app
            # reads is cached anywhere — whatever secondarycache says.
            st = "fail" if any_on else "warn"
            row["cache"] = cell(st, f"primarycache={prim}")
            fixes.append(item(st, M("Cache settings for {dataset}", dataset=ds),
                              (M("primarycache={prim}, secondarycache={sec}. With primarycache set to {prim}, file data "
                                 "never enters RAM — and ZFS only fills the L2ARC from RAM, so nothing read from {libraries} gets "
                                 "cached. The app skips it until primarycache is all.", prim=prim, sec=sec, libraries=libs) if one else
                               M("primarycache={prim}, secondarycache={sec}. With primarycache set to {prim}, file data "
                                 "never enters RAM — and ZFS only fills the L2ARC from RAM, so nothing read from {libraries} gets "
                                 "cached. The app skips them until primarycache is all.", prim=prim, sec=sec, libraries=libs)),
                              M("Run in the TrueNAS shell (ZFS still frees RAM for other things when it needs to):"),
                              f"sudo zfs set primarycache=all {ds}"))
        # secondarycache only matters with an SSD cache (L2ARC) on the pool: without one there's
        # nothing for it to allow into, so any value is fine.
        elif sec != "all" and l2_wanted and (pool is None or pool["l2_online"]):
            row["cache"] = cell("warn", f"secondarycache={sec}")
            fixes.append(item("warn", M("Cache settings for {dataset}", dataset=ds),
                              M("primarycache=all, secondarycache={sec}. Caching {libraries} on the SSD (L2ARC) needs file data "
                                "to be allowed into it.", sec=sec, libraries=libs),
                              M("Run in the TrueNAS shell:"), f"sudo zfs set secondarycache=all {ds}"))
        else:
            row["cache"] = cell("pass", f"primarycache=all, secondarycache={sec}")

    # --- record size, two checks, for every library cached or not (it's about how well the files
    # read at all). What to do about a warning is in each column's info box.
    # 1. The setting: does it suit every library on the dataset (each one's recommended size or
    #    second best), and can one size suit them all?
    keys = (d.get("keys") or []) + (d.get("off_keys") or [])
    if rs:
        titles = {str(lib["key"]): lib.get("title") or str(lib["key"]) for lib in getattr(app, "libraries", []) or []}
        unsuited = [(titles.get(str(k), str(k)), r) for k in keys for r in [library_record(app, k, rs)] if r["status"] == "warn"]
        lines = [M("Doesn’t suit {library} ({best} or {alt})", library=t, best=_fmt_rs(r["best"]), alt=_fmt_rs(r["alt"]))
                 for t, r in unsuited]
        shared = _shared_record_check(app, ds, keys)
        if shared:
            # one record size per dataset, and these libraries need different ones
            row["recordsize"] = cell("warn", _fmt_rs(rs), badge=M("Split dataset"))
            row["recordsize"]["split"] = True  # the page links to the Optimizer page's folder-to-dataset steps
            row["recordsize"]["lines"] = [M("[Move {libraries} to datasets of their own](/dataset); {keep} suits the rest.",
                                            one="[Move {libraries} to a dataset of its own](/dataset); {keep} suits the rest.",
                                            n=len(shared["move"]), libraries=join(shared["move"]), keep=_fmt_rs(shared["keep"]))]
        else:
            row["recordsize"] = cell("warn" if lines else "pass", _fmt_rs(rs))
            row["recordsize"]["lines"] = lines
    else:
        row["recordsize"] = cell("info", M("Not checked"))
    # 2. The files: is every one written in blocks of that size? (A new record size only applies to
    #    files written after it was set.)
    st = _blocks_check(ds, rs, d.get("blocks"), libs)
    note = _files_note(d.get("blocks"), rs) if rs else ""
    row["file_rs"] = (cell("info", M("Not checked")) if st == "info" or not note else
                      cell("pass", note, badge=M("All files match")) if st == "pass" else
                      cell("warn", note, badge=M("None match") if _none_match(d.get("blocks"), rs) else M("Some don’t match")))

    # --- compression: always the same advice (ZSTD-3), whatever is on the dataset
    comp = (props or {}).get("compression") if tn_ok else None
    row["compression"] = compression_cell(comp)

    # --- L2ARC on this dataset's pool
    if not cached:
        return row
    if pool is None:
        row["l2arc"] = cell("info", M("Not checked"))
    elif pool["l2_online"]:
        row["l2arc"] = cell("pass", M("{n} devices, {size} on {pool}", one="{n} device, {size} on {pool}",
                                      n=pool["l2_online"], size=_fmt_bytes(pool["l2_total"]), pool=pool["name"]))
    elif pool["cache_devices"]:
        row["l2arc"] = cell("warn", M("Devices not ONLINE"))
        fixes.append(item("warn", M("L2ARC on pool {pool}", pool=pool["name"]),
                          M("Cache devices are attached to {pool} ({libraries}) but none are ONLINE.", pool=pool["name"], libraries=libs),
                          M("Check the pool in TrueNAS → Storage.")))
    else:
        # With no L2ARC anywhere the warning (and fix) is under "Caching mode", once for the whole NAS;
        # this cell only warns when other pools have one and this pool's libraries miss out.
        row["l2arc"] = cell("warn" if l2_wanted and app.l2arc_status() == "present" else "info", "", badge=M("None"))
    return row


def check_datasets(app) -> dict:
    z = app.zfs or {}
    s = app.settings.get()
    t = s["truenas"]
    tn_set = bool(t["url"] and t["api_key"])
    tn_ok = tn_set and not z.get("truenas_error")
    pools = z.get("pools") or {}
    datasets = z.get("datasets") or []
    rows = [_dataset_row(app, d, pools, tn_ok) for d in datasets]
    items = []
    if not any(d["keys"] for d in datasets):
        why = (M("Can’t check — no library folder is readable yet, so there’s nothing to check.")
               if app.libraries else M("Can’t check — Plex isn’t connected yet."))
        items.append(item("info", M("Media Datasets: not checked yet"), why,
                          M("Fix the problems under [Libraries](/health#media) first ([System → Media Mappings](/system#mappings)).") if app.libraries else M("Open [System → Plex](/system#plex).")))
    elif not tn_ok:
        items.append(item("info", M("Dataset settings: not checked"),
                          M("Cache settings, record size and L2ARC devices need the TrueNAS API (see TrueNAS & ZFS).") if tn_set
                          else M("Cache settings, record size and L2ARC devices need the TrueNAS API."),
                          M("Check [System → TrueNAS](/system#truenas).")))
    return {"id": "datasets", "title": M("Media Datasets"),
            "hint": M("Each dataset your libraries are on, checked on its own."),
            "datasets": rows, "items": items}


def check_zfs(app) -> dict:
    z = app.zfs or {}
    s = app.settings.get()
    items: list[dict] = []
    t = s["truenas"]
    pf = z.get("platform") or {}
    if not (t["url"] and t["api_key"]):
        if pf.get("truenas") is False:  # (another Linux host: there's no TrueNAS API to connect)
            items.append(item("info", M("Not running on TrueNAS"),
                              M("{label}. Datasets are read from ZFS directly; the checks that need the TrueNAS API — cache devices, pool layout, scrubs — are left out.", label=pf.get("label") or "")))
        elif pf.get("truenas"):  # (on TrueNAS for certain: the key is what's missing)
            items.append(item("warn", M("TrueNAS API key needed"),
                              M("This is TrueNAS, but no API key is set. With one, Plex Cache checks each dataset’s settings and cache devices, sizes the L2ARC, pauses for scrubs and resilvers — and can update itself from the page when a new build is out."),
                              M("Add a read-only key with app management allowed in [System → TrueNAS](/system#truenas).")))
        else:
            items.append(item("info", M("TrueNAS API not connected"),
                              M("Add an API key to check each dataset’s settings and cache devices, and to size the L2ARC."),
                              M("Open [System → TrueNAS](/system#truenas).")))
    elif z.get("truenas_error"):
        items.append(item("fail", M("TrueNAS API"), z["truenas_error"], M("Check the address and API key in [System → TrueNAS](/system#truenas).")))
    elif z.get("truenas_missing"):  # (a TrueNAS release without a call the app makes: said before the feature is tried)
        gone = z["truenas_missing"]
        items.append(item("warn", M("TrueNAS API"),
                          M("Connected to {version}, but this release hasn’t got {n} of the calls the app makes: {what}. The features that need them won’t work until the app is updated for it.",
                            version=z["truenas_version"] or "", n=len(gone), what="; ".join(f"{what} ({', '.join(names)})" for what, names in gone.items())),
                          M("Look for a newer Plex Cache in [System → Updates](/system#updates) — or report the TrueNAS version and the calls above on GitHub.")))
    elif z.get("truenas_version"):
        items.append(item("pass", M("TrueNAS API"), M("Connected to {version} (read-only). Every call the app makes is there.", version=z["truenas_version"])
                          if z.get("truenas_methods") is not None else M("Connected to {version} (read-only).", version=z["truenas_version"])))

    arc = z.get("arc")
    if arc:
        items.append(item("pass", M("ARC (RAM cache, whole NAS)"),
                          M("{used} in use of {max} max. Whether caching is helping playback is on the Metrics page.",
                            used=_fmt_bytes(arc["arc_size"]), max=_fmt_bytes(arc["arc_max"]))))
    else:
        items.append(item("warn", M("Can’t read ZFS statistics"),
                          M("/proc/spl/kstat/zfs/arcstats isn’t readable here, so cache hit and L2ARC growth figures are unavailable.")))

    items.extend(_ram_check(app))
    if app.l2arc_status() == "present":
        items.extend(_l2arc_tuning_checks(app, z.get("params") or {}))
    return {"id": "zfs", "title": M("TrueNAS & ZFS"),
            "hint": M("NAS-wide settings, read from ZFS statistics and the TrueNAS API. Nothing is changed without you."),
            "items": items}


TIGHT_RAM = 2 * GiB  # below this, Now Playing Cache can't hold much of a film


def _ram_check(app) -> list[dict]:
    """Is there RAM to preload into once the system reserve is set aside?"""
    s = app.settings.get()
    b = app.ram_budget()
    if not b["cache_max"]:
        return [item("info", M("RAM for Now Playing Cache and Prefetch"), M("Couldn’t read the ZFS RAM cache size, so Now Playing Cache and "
                                                        "Prefetch limits can’t be worked out."))]
    on = s["nowplaying"]["enabled"] or app.prefetch_active()
    base = (M("{available} of RAM is available to the ZFS cache (what it holds now plus free RAM; its maximum is {max})",
              available=_fmt_bytes(b["available"]), max=_fmt_bytes(b["cache_max"])) if b["source"] == "arc"
            else M("ZFS statistics aren’t readable, so half of RAM ({size}) is assumed", size=_fmt_bytes(b["cache_max"])))
    reserve = M("{size} of it left for the rest of the NAS", size=_fmt_bytes(b["reserve"]))
    if b["usable"] <= 0:
        return [item("fail" if on else "warn", M("Not enough RAM for Now Playing Cache and Prefetch"),
                     M("{base}, and {reserve} leaves nothing for Now Playing Cache or Prefetch.", base=base, reserve=reserve),
                     M("Lower “Reserve ZFS cache” in [Caching → Cache Settings](/caching#arc), or add RAM."))]
    limits = M("Now Playing Cache may use up to {nowplaying} and Prefetch {nextepisode}.",
               nowplaying=_fmt_bytes(b["nowplaying"]), nextepisode=_fmt_bytes(b["nextepisode"]))
    if b["nowplaying"] < TIGHT_RAM:
        return [item("warn", M("RAM is tight for Now Playing Cache and Prefetch"),
                     M("{base}; with {reserve}, {limits} That’s only a few minutes of a 4K film, so skipping far "
                       "ahead will still read from disk.", base=base, reserve=reserve, limits=limits),
                     M("Add RAM, an L2ARC (SSD cache), or lower “Reserve ZFS cache” in [Caching → Cache Settings](/caching#arc)."))]
    return [item("pass", M("RAM for Now Playing Cache and Prefetch"), M("{base}; with {reserve}, {limits}", base=base, reserve=reserve, limits=limits))]


PERSIST_HINT = M("To keep it after a reboot, add it (without sudo — these run as root) in TrueNAS under System → Advanced Settings → "
                 "Init/Shutdown Scripts (Type: Command, When: Post Init).")


def _files_note(blocks: dict | None, rs: int):
    """The file record size cell: "21,604 files in 1M", or "1,203 of 21,604 files in 128K" (the
    most common other size). Files that fit in one block don't count."""
    if not blocks or not blocks.get("checked"):
        return ""
    multi = blocks["checked"] - blocks.get("single", 0)
    if not multi:
        return M("{n} files, each smaller than a record", n=blocks["checked"])
    other = {int(bs): n for bs, n in (blocks.get("sizes") or {}).items() if int(bs) != rs and n}
    if not other:
        return M("{n} files in {size}", n=multi, size=_fmt_rs(rs))
    common = max(other, key=other.get)
    return M("{old} of {n} files in {size}", old=sum(other.values()), n=multi, size=_fmt_rs(common))


def _none_match(blocks: dict | None, rs: int) -> bool:
    """Whether every file bigger than a record is written in another block size."""
    multi = blocks["checked"] - blocks.get("single", 0)
    return multi > 0 and sum(n for bs, n in (blocks.get("sizes") or {}).items() if int(bs) != rs) >= multi


def _fmt_rs(n: int) -> str:
    return f"{n // (1024 * 1024)}M" if n >= 1024 * 1024 else f"{n // 1024}K"


MiB = 1024 * 1024


def compression_cell(comp: str | None) -> dict | None:
    """The dataset's compression: ZSTD 3 to 19 pass (3 recommended), anything else is a warning
    with what to use instead. The badge is the setting itself."""
    g = compression_grade(comp)
    if g is None:
        return cell("info", M("Not checked"))
    if g == "good":
        level = int(comp.split("-")[1]) if "-" in comp else 3
        return cell("pass", M("Recommended") if level == 3 else
                    M("Marginally smaller files than ZSTD-3, more CPU when writing"), badge=comp)
    if g == "low":
        return cell("warn", M("Use ZSTD-3: compresses more, and no slower in practice"), badge=comp)
    return cell("warn", M("Use ZSTD-3"), badge=comp)


def _blocks_check(ds: str, recordsize: int | None, blocks: dict | None, libs: str = "") -> str:
    """That every file on the dataset is written with its record size: a new record size only
    applies to files written after it was set, and files that fit in one block don't count
    (they're one block of their own size whatever the setting). "warn" when some aren't (the
    cell says how many, _files_note), "pass" when they all are, "info" when not known."""
    if not recordsize:
        return "info"
    if blocks and blocks.get("checked"):
        if any(int(bs) != recordsize and n for bs, n in (blocks.get("sizes") or {}).items()):
            return "warn"
    return "pass"


def library_record(app, key: str, rs: int | None = None) -> dict:
    """Whether a library's dataset record size suits its media: "ideal" when it's the size
    recommended from the library's average bitrate, "pass" when it's the next best (either is
    fine, so a library on the line between two doesn't flip), "warn" otherwise; "info" when the
    record size or the library's files aren't known yet."""
    key = str(key)
    info = app.library_records().get(key) or {}
    rs = rs or info.get("rs")
    lib = next((x for x in app.stats.libraries if str(x["key"]) == key), None)
    out = {"rs": rs, "dataset": info.get("dataset"), "best": None, "alt": None, "status": "info"}
    if not lib or not lib.get("count"):
        return out
    rec = recommend_recordsize(lib["avg_kbps"], lib.get("avg_size") or 0)
    out.update(best=rec["best"], alt=rec["alt"], kbps=lib["avg_kbps"])
    if rs:
        out["status"] = "ideal" if rs == rec["best"] else "pass" if rs == rec["alt"] else "warn"
    return out


def _shared_record_check(app, ds: str, keys: list) -> dict | None:
    """That one record size can suit every library on a dataset — the recommended size or the
    second best of each — whatever the dataset is set to now. A record size is per dataset,
    so libraries that want different ones need datasets of their own."""
    titles = {str(lib["key"]): lib.get("title") or str(lib["key"]) for lib in getattr(app, "libraries", []) or []}
    recs = []
    for key in keys:
        rec = library_record(app, key)
        if rec["best"]:
            recs.append((titles.get(str(key), str(key)), rec))
    if len(recs) < 2:
        return None
    fits = {size: [t for t, rec in recs if size in (rec["best"], rec["alt"])]
            for size in {x for _, rec in recs for x in (rec["best"], rec["alt"])}}
    if any(len(v) == len(recs) for v in fits.values()):
        return None
    # The size that suits the most libraries stays; the others are the ones to move.
    keep = max(fits, key=lambda size: (len(fits[size]), size))
    move = [t for t, rec in recs if keep not in (rec["best"], rec["alt"])]
    needs = [M("{library}: {best} or {alt}", library=t, best=_fmt_rs(rec["best"]), alt=_fmt_rs(rec["alt"])) for t, rec in recs]
    return {"needs": needs, "move": move, "keep": keep}


def _l2arc_tuning_checks(app, params: dict) -> list[dict]:
    """ZFS module settings that decide whether warm-up reads actually land in L2ARC."""
    if not any(v is not None for v in params.values()):
        return [item("info", M("L2ARC settings"), M("Couldn’t read ZFS module settings from this app, so they weren’t checked."))]
    items = []
    ok_bits = []
    sysp = "/sys/module/zfs/parameters"

    mfu = params.get("l2arc_mfuonly")
    if mfu not in (None, "0"):
        items.append(item("warn", M("L2ARC only keeps frequently used data"),
                          M("l2arc_mfuonly={value}: only data read more than once is copied to the L2ARC. Warm-up reads "
                            "each file once, so it would never reach the SSD.", value=mfu),
                          M("Run in the TrueNAS shell. {persist}", persist=PERSIST_HINT), f"echo 0 | sudo tee {sysp}/l2arc_mfuonly"))
    elif mfu == "0":
        ok_bits.append(M("all data can be cached (l2arc_mfuonly=0)"))

    rebuild = params.get("l2arc_rebuild_enabled")
    if rebuild == "0":
        items.append(item("warn", M("The L2ARC is emptied on every reboot"),
                          M("l2arc_rebuild_enabled=0, so the SSD cache starts empty after each restart and warm-up "
                            "has to begin again."),
                          M("Run in the TrueNAS shell. {persist}", persist=PERSIST_HINT), f"echo 1 | sudo tee {sysp}/l2arc_rebuild_enabled"))
    elif rebuild == "1":
        ok_bits.append(M("kept across reboots (l2arc_rebuild_enabled=1)"))

    wm = params.get("l2arc_write_max")
    if wm and wm.isdigit():
        fill = int(wm) / 1048576
        disk = app.fastest_cached_disk()
        w = app.settings.section("warmup")
        reading = disk * w["pacing_percent"] / 100
        from .warmup import L2ARC_RATE_SHARE
        rate = int(wm) * L2ARC_RATE_SHARE if w["pacing_auto"] else None
        if rate:
            # Automatic pacing matches it; say what that means for a full warm-up
            need = app.lead_in_plan(w)["need_max"] if app.stats.scanned_at else 0
            hours = need / rate / 3600 if need else 0
            if app.warmup_active() and fill < 64 and hours >= 2:
                items.append(item("info", M("Warm-up is held to {rate} MB/s by the L2ARC", rate=round(rate / 1e6)),
                                  M("ZFS copies at most {fill} MiB/s to the SSD (l2arc_write_max), and automatic L2ARC pacing reads just "
                                    "under that, so nothing is read only to be lost. Warming up every library’s lead-ins from scratch "
                                    "takes about {hours} hours at that speed (repeat runs are quicker: what’s already on the SSD isn’t held back).",
                                    fill=round(fill), hours=round(hours)),
                                  M("To go faster, raise the limit to 128 MiB/s in the TrueNAS shell; pacing follows it. {persist}",
                                    persist=PERSIST_HINT),
                                  f"echo 134217728 | sudo tee {sysp}/l2arc_write_max"))
            else:
                ok_bits.append(M("fills at up to {fill} MiB/s with L2ARC pacing just under that", fill=round(fill)))
        elif app.warmup_active() and reading > fill * 1.1:
            items.append(item("warn", M("The L2ARC fills slower than warm-up reads"),
                              M("ZFS copies at most {fill} MiB/s to the SSD (l2arc_write_max), but warm-up reads "
                                "about {reading} MB/s ({pct}% of {disk} MB/s). "
                                "Data can leave RAM before it reaches the SSD, so some reads are wasted.",
                                fill=round(fill), reading=round(reading), pct=w["pacing_percent"], disk=disk),
                              M("Either turn on automatic L2ARC pacing or lower it to about {pct}% ([SSD Cache → L2ARC Cache Settings](/ssd#l2arc-settings)), "
                                "or raise the limit to 128 MiB/s in the TrueNAS shell. {persist}",
                                pct=max(1, int(fill / disk * 100)), persist=PERSIST_HINT),
                              f"echo 134217728 | sudo tee {sysp}/l2arc_write_max"))
        else:
            ok_bits.append(M("fills at up to {fill} MiB/s", fill=round(fill)))

    npf = params.get("l2arc_noprefetch")
    if npf == "1":
        ok_bits.append(M("only what’s actually read is copied (l2arc_noprefetch=1)"))
    elif npf is not None:
        ok_bits.append(M("ZFS’s own read-ahead is copied too (l2arc_noprefetch={value})", value=npf))
    if ok_bits:
        items.append(item("pass", M("L2ARC settings"), M("Suited to warm-up: {list}.", list=ok_bits)))
    return items


def check_cache_mode(app) -> dict:
    """L2ARC or RAM only, whether settings get what they asked for, and the missing-L2ARC warning."""
    s = app.settings.get()
    items: list[dict] = []
    status, no_l2 = app.l2arc_status(), s["general"]["no_l2arc"]
    if no_l2:
        items.append(item("info", M("RAM-only caching"),
                          M("You chose not to use an L2ARC (SSD Cache page), so settings use their RAM-only defaults "
                            "and SSD-only choices aren’t used.")))
    elif status == "present":
        items.append(item("pass", M("L2ARC caching"), M("The media pool has an L2ARC, so settings use their L2ARC defaults.")))
    elif status == "absent":
        items.append(item("warn", M("No L2ARC on the media pool"),
                          M("Caching is planned around an SSD cache (L2ARC). Without one, settings use their RAM-only "
                            "defaults, SSD-only choices fall back, and warm-up can’t run."),
                          M("Add an SSD to the pool as a Cache (L2ARC) vdev in TrueNAS → Storage — or, to run on RAM only, "
                            "tick “I won’t use an L2ARC” on the SSD Cache page.")))
    else:
        items.append(item("info", M("L2ARC not checked yet"),
                          M("Couldn’t tell whether the media pool has an L2ARC, so settings use their RAM-only defaults for now."),
                          M("Connect TrueNAS in [System → TrueNAS](/system#truenas) to check.")))
    fell = app.fallbacks.recent()
    fallback_help = M("Some choices only work with an SSD cache (L2ARC), because they read more than RAM can hold: every warm-up "
                      "schedule ([Caching → Warm-up](/caching#leadin)), and Prefetch’s larger steps — Whole season, Next season, All seasons, Every "
                      "show, and Rest of the season near the end. They’re marked with the SSD icon.<br><br>"
                      "When the media pool has no L2ARC (or “I won’t use an L2ARC” is ticked on the SSD Cache page), the app uses "
                      "that setting’s RAM-only default instead — so nothing breaks, but you get less than you chose — and lists it "
                      "here for 24 hours.<br><br><strong>To fix a warning</strong>: add an L2ARC to the media pool, or pick choices "
                      "without the SSD icon.")
    if fell:
        items.append(item("warn", M("Choices that need an SSD cache"),
                          M("These need an L2ARC, which the media pool doesn’t have, so their RAM-only default was used instead: {list}.",
                            list=[M("{setting} ({chosen} → {used})", setting=f["label"], chosen=f["chosen"], used=f["used"])
                                  for f in fell]),
                          M("Add an L2ARC, or pick a choice without the SSD icon."), help=fallback_help))
    else:
        items.append(item("pass", M("Choices that need an SSD cache"),
                          M("Every warm-up schedule and Prefetch choice is running as you set it: none needed an L2ARC that "
                            "wasn’t there in the last 24 hours."), help=fallback_help))
    items.append(_access_check(app))
    items.append(_optimizer_check(app))
    items.extend(_conflict_items(app))
    items.extend(_migration_items(app))
    items.extend(_sync_items(app))
    items.extend(_tool_items(app))
    items.extend(_link_items(app))
    items.extend(_job_failures(app))
    return {"id": "mode", "title": M("Plex Cache"),
            "hint": M("How caching runs here — with an SSD cache (L2ARC) or RAM only — who can use the app, and the optimizer service."), "items": items}


OPTIMIZER_HELP = M("The optimizer is the part of Plex Cache that rewrites your media files so they suit caching: in their "
                   "dataset’s record size and compression and, for MKV files, with the attachments, chapters, index and tags after the "
                   "video. The Optimizer page has the choices for each library and a card for each file to optimize, with <strong>Optimize Selected</strong>.<br><br>"
                   "It’s optional, and runs as a second service in this app — the only part of Plex Cache that can write to your "
                   "media. It does nothing on its own: it rewrites one file at a time when you press Optimize Now, checks each "
                   "new file before swapping it in, and skips anything playing or just added. It has no web page and no port of "
                   "its own; only this app talks to it, with a token both services share.")


def _job_failures(app) -> list[dict]:
    """The last file job's failures (the Optimizer page lists them all, with why): a failed file is
    left exactly as it was — the new one only replaces it once every check has passed."""
    jobs = getattr(app, "jobs", None)
    last = jobs.snapshot() if jobs else None
    if not last or last.get("running") or not last.get("errors"):
        return []
    errs = last["errors"]
    return [dict(item("warn", M("{n} files weren’t optimized", one="{n} file wasn’t optimized", n=len(errs)),
                 M("For example: {files}.", files=[f"{os.path.basename(e.get('file') or '')}: {e.get('reason')}" for e in errs[:3]]),
                 M("They’re as they were: a file is only replaced once every check has passed."),
                 help=M("Each file the optimizer writes is checked before it replaces the original: every video, audio and subtitle "
                        "stream must be the same as the original’s (ffmpeg), the attachments byte for byte, the layout and tags as "
                        "written. When a check fails — or a tool does, or the file is in use — the new file is thrown away and the "
                        "original kept. Optimize Now tries the failed files again.")),
                 link={"job": last.get("id"), "text": M("See them all, and why, on the Processes page")})]


def _sync_items(app) -> list[dict]:
    """The mirror (sync.py, the Mirror page): the mirror choice still to make, a server found that may be a mirror,
    another Plex Cache seen in the folders. (What Sync Libraries would bring in line is its count, as every tool's.)"""
    sync = getattr(app, "sync", None)
    if sync is None:
        return []
    out = []
    other = app.other
    titles = {str(lib["key"]): lib.get("title") or str(lib["key"]) for lib in getattr(app, "libraries", []) or []}
    undecided = other.undecided()
    if undecided:
        out.append(dict(item("warn", M("Is {libraries} mirrored?", one="Is {libraries} mirrored?", libraries=[titles[k] for k in undecided], n=len(undecided)),
                             M("Whether another Plex server has the same files (another site, kept in step by syncing the folders) hasn’t been chosen for "
                               "{n} libraries. Until it is, their converted files’ originals aren’t deleted: a mirror that isn’t watched could lose its item.",
                               one="Whether another Plex server has the same files (another site, kept in step by syncing the folders) hasn’t been chosen for "
                               "this library. Until it is, its converted files’ originals aren’t deleted: a mirror that isn’t watched could lose its item.", n=len(undecided)),
                             M("On the [Mirror page](/mirrors), mark each library as mirrored, or as having no mirror."))))
    if any(other.state_of(k) == "mirror" for k in titles) and not other.configured():
        out.append(dict(item("warn", M("The mirrored Plex server isn’t set up"), M("A library is marked as mirrored, but no server is watched yet."),
                             M("On the [Mirror page](/mirrors), choose the server."))))
    seen = [(titles[k], c) for k in undecided for c in [sync.suspected(k)] if c]
    if seen:
        out.append(dict(item("info", M("A library that looks mirrored: {names}", names=[f"{t} ({c['name']}: {c['their_title']})" for t, c in seen]),
                             M("Its files are here too, at the same places (a sample of them)."),
                             M("Mark it as mirrored on the [Mirror page](/mirrors), or say it has no mirror."))))
    out.extend(_peer_items(app))
    out.extend(_mirror_items(app))
    return out


def _tool_items(app) -> list[dict]:
    """The tools' leftovers that wait on you: a set-aside file that never met the conditions for its
    deletion (the mirror keeps listing it, say), a moved film the mirror's Plex hasn't picked up."""
    trash = getattr(app, "trash", None)
    out = []
    stuck = trash.stuck() if trash else []
    if stuck:
        out.append(dict(item("warn", M("{n} set-aside files can’t be deleted yet", one="{n} set-aside file can’t be deleted yet", n=len(stuck)),
                             M("Plex here, or the mirrored server’s, still lists them after {days} days: {files}.", days=14, files=[os.path.basename(e["path"]) for e in stuck[:3]]),
                             M("On the Duplicate Versions page: delete it now, put it back, or keep it set aside.")),
                        link={"href": "/versions", "text": M("Open Duplicate Versions")}))
    folders = (getattr(app, "tools", None) or {}).get("folders")
    late = folders.stuck() if folders else []
    if late:
        out.append(dict(item("warn", M("{n} moved films the mirrored server hasn’t picked up", one="{n} moved film the mirrored server hasn’t picked up", n=len(late)),
                             M("Its Plex doesn’t list the new folder after {days} days: {titles}. Is the folder synced there?", days=3, titles=[e["title"] for e in late[:3]])),
                        link={"href": "/folders", "text": M("Open Organize Folders")}))
    return out


def _link_items(app) -> list[dict]:
    """Hard links among a library's files (from the data layer): a warning per library, with why they're
    trouble and where to start untangling them."""
    from .libdata import link_of
    counts: dict[str, int] = {}
    libdata = getattr(app, "libdata", None)
    for key, d in list(getattr(libdata, "libs", {}).items()):
        if not d.ready:
            continue
        n = sum(1 for f in d.files if link_of(f)[0] == "hardlink")
        if n:
            counts[str(key)] = n
    names = {str(lib["key"]): lib.get("title") or lib["key"] for lib in getattr(app, "libraries", []) or []}
    return [dict(item("warn", M("{n} hard-linked files in {library}", one="{n} hard-linked file in {library}", n=n, library=names.get(key, key)),
                      M("A hard link is one file under two paths. Plex lists one and ignores the other, so the folder looks to hold a file Plex hasn’t got; "
                        "a folder sync can’t carry the link, so the other site gets two whole copies that drift apart once either changes, and show up as sync differences. "
                        "Untangle them with the tools, starting with [Unlink Files](/unlink): each link made a file of its own (or, on a library that isn’t synced, its extra paths removed), "
                        "then [Merge Duplicates](/merge) or [Duplicate Versions](/versions) to tidy the copies."),
                      M("Open [Unlink Files](/unlink) and scan the library.")))
            for key, n in sorted(counts.items(), key=lambda kv: names.get(kv[0], kv[0]))]


def _peer_items(app) -> list[dict]:
    """Another Plex Cache sharing library folders (peers.py): who, with which settings, and what
    would fight — syncing both ways, optimizer choices that differ, a file tool just run there."""
    from . import peers
    summary = getattr(app, "peers_summary", None)
    if summary is None:
        try:
            summary = app.peers_summary = peers.summary(app)
        except Exception:  # noqa: BLE001
            return []
    out = []
    names = {str(lib["key"]): lib.get("title") or lib["key"] for lib in getattr(app, "libraries", []) or []}
    for sy in summary.get("systems") or []:
        s = sy.get("sync") or {}
        ran = (sy.get("tools") or {}).get("sync") or s.get("mode") == "apply"  # (an older Plex Cache: applying on a timer)
        how = (M("hasn’t run Sync Libraries this week") if not ran else M("runs Sync Libraries with its own edits winning") if s.get("source") == "this"
               else M("runs Sync Libraries with its mirror’s edits winning"))
        out.append(dict(item("info", M("Another Plex Cache shares these folders: {name}", name=sy.get("name") or sy.get("instance")),
                             M("Version {version}; shares {libs}; it {how}. Each side optimizes its own files, and claims keep the two off the same file.",
                               version=sy.get("version") or "?", libs=[names.get(k, k) for k in sy.get("libraries") or []], how=how),
                             "" if summary.get("publishing") else M("This side can’t tell the other about its own settings until the optimizer service is set up (it writes the marker).")),
                        link={"href": "/mirrors", "text": M("Open the Mirror page")}))
    for key, why in (summary.get("sync_conflicts") or {}).items():
        out.append(dict(item("fail", M("Sync Libraries would go back and forth: {library}", library=names.get(key, key)), why,
                             M("Its Apply is refused for this library until it’s settled.")), link={"href": "/sync", "text": M("Open Sync Libraries")}))
    for key, why in (summary.get("optimizer_conflicts") or {}).items():
        out.append(dict(item("fail", M("Optimizer choices differ between the two sides: {library}", library=names.get(key, key)), why,
                             M("Optimize Now is refused for this library until they match.")), link={"href": "/optimizer", "text": M("Open the Optimizer page")}))
    return out


def _mirror_items(app) -> list[dict]:
    """The Plex Cache at the mirror's site (peerlink.py) and Mirror Checks (mirrorhealth.py): the link not healthy, a
    library held because .plexcache doesn't reach the other site, findings waiting."""
    link, health = getattr(app, "peerlink", None), getattr(app, "mirror_health", None)
    if link is None or health is None:
        return []
    out = []
    state = link.health()
    if state in ("warn", "fail"):
        bad = [v for v in link.lights().values() if v["status"] in ("warn", "fail")]
        out.append(dict(item(state, M("The link to the Plex Cache at the mirror’s site needs attention"),
                             M("{lights}. Mirror Checks rely on it: some of its checks are left out meanwhile.", lights=[b["text"] for b in bad]),
                             M("See [System → Mirror Plex Cache](/system#peer).")), link={"href": "/system#peer", "text": M("Open System")}))
    names = {str(lib["key"]): lib.get("title") or lib["key"] for lib in getattr(app, "libraries", []) or []}
    converting = [k for k in health.libraries() if health.converting(k)]
    if converting:
        out.append(dict(item("info", M("The other site is converting a folder to a child dataset: {libraries}", libraries=[names.get(k, k) for k in converting]),
                             health.converting(converting[0]),
                             M("Mirror Checks compare these libraries again once it’s done.")), link={"href": "/mirrorchecks", "text": M("Open Mirror Checks")}))
    held = [k for k in health.libraries() if k not in converting and health.hold(k)]
    if held:
        out.append(dict(item("fail", M("Optimizing is held: {libraries}", libraries=[names.get(k, k) for k in held]),
                             M("The .plexcache folder doesn’t reach the other site, so its Plex Cache wouldn’t see this side’s claims: the two could optimize the same file."),
                             M("Fix the folder sync (Syncthing: don’t ignore .plexcache), or ignore the finding on Mirror Checks.")),
                        link={"href": "/mirrorchecks", "text": M("Open Mirror Checks")}))
    n = sum((c or {}).get("mirror") or 0 for k, c in (getattr(app, "health_counts", None) or {}).items() if k in health.libraries())
    if n:
        out.append(dict(item("info", M("{n} items differ between the two sites of the mirror", one="{n} item differs between the two sites of the mirror", n=n),
                             M("Files on one side only, or one Plex not listing them, as Mirror Checks last compared them (ignored ones left out).")),
                        link={"href": "/mirrorchecks", "text": M("Open Mirror Checks")}))
    return out


def _migration_items(app) -> list[dict]:
    """A folder being converted to a child dataset that waits on you: said here, not only on the
    Optimizer page."""
    mig = getattr(app, "migration", None)
    st = (mig.state or {}) if mig else {}
    if st.get("step") not in ("create", "switch", "verify") or st.get("copying") or st.get("queued"):
        return []
    failing = [c for c in mig.checks if not c.get("ok")]
    todo = next((c for c in failing if c.get("level") == "error"), None) or \
        next((c for c in failing if c.get("level") != "warn"), None) or \
        next(iter(failing), None)  # (something wrong first, then a step to do; a warning when nothing else is left)
    if todo:
        what, more = todo["title"], todo.get("detail") or ""
    elif st["step"] == "switch":
        what, more = M("rename the dataset to the folder’s name"), ""
    else:
        return []
    level = "fail" if todo and todo.get("level") == "error" else "warn"  # (something wrong that stops it: red)
    return [dict(item(level, M("Action needed to finish converting {title} to a child dataset", title=st.get("title") or st.get("folder")),
                      M("Next: {what}. {more}", what=what, more=more),
                      help=M("Turning a library folder into a dataset of its own goes step by step, and some steps are yours — in "
                             "TrueNAS, or a button on the Optimizer page. Until then nothing moves on; the folder and Plex are as they were.")),
                 link={"href": "/dataset", "text": M("Carry on, on its Tools page")})]


def _conflict_items(app) -> list[dict]:
    """Syncthing conflict copies next to library files: two systems wrote a file at once. The
    optimizer deletes its own (Optimize Now); the rest are listed here for a person to look at."""
    op = getattr(app, "optimizing", None)
    if op is None:
        return []
    found = [c for lib in (op.snapshot().get("libraries") or {}).values() for c in lib.get("conflicts") or []]
    if not found:
        return []
    return [item("warn", M("{n} Syncthing conflict copies", one="{n} Syncthing conflict copy", n=len(found)),
                 M("Next to your media: {files}.", files=[os.path.basename(c["copy"]) for c in found[:3]]),
                 M("Optimize Now on the Optimizer page deletes the ones the optimizer made that are the same as the file. "
                   "Others are kept for you to compare and delete."),
                 help=M("When two systems sharing a library (Syncthing, both ways) change the same file at once, Syncthing "
                        "keeps both, the second as name.sync-conflict-<date>-<device>.mkv. The optimizer avoids it — it "
                        "claims a file before changing it, and writes the very same bytes for the same file on every "
                        "system — but if it happens, it deletes the copy only when both are optimized by it and every "
                        "stream is the same."))]


def optimizer_code_mount() -> str | None:
    """Where this app's code is on the host, when it's mounted over the image's (a test copy, like
    dev/compose.dev.yaml): the optimizer needs the same mount to run the same code. None normally."""
    here = os.path.dirname(os.path.abspath(__file__))
    ml = zfs.mounts()
    if not any(m["mountpoint"] == here for m in ml):
        return None
    return zfs.host_path(here, ml)


def optimizer_yaml(app) -> str:
    """The optimizer's service, to paste into the app's YAML. Its volumes are filled in from this
    app's own mounts: the settings folder (read-only: the optimizer reads the app's token there),
    the media folders (writable: what it may rewrite) and, in a test copy, the code."""
    ml = zfs.mounts()
    config = zfs.host_path(getattr(app, "config_dir", "/config"), ml) or "/mnt/tank/apps/plex-cache"
    mounts = sorted({d.get("mountpoint") for d in (app.zfs or {}).get("datasets") or []
                     if d.get("mountpoint") and (d.get("keys") or d.get("off_keys"))})
    # a dataset mounted inside another one comes with it (bind mounts include what's below them)
    mounts = [mp for mp in mounts if not any(mp != o and mp.startswith(o.rstrip("/") + "/") for o in mounts)] or ["/data"]
    # long form, with propagation: a dataset made inside a media folder shows up without a restart
    media = "\n".join(f"      - type: bind\n        source: {zfs.host_path(mp, ml) or '/mnt/tank/plexfiles'}\n"
                      f"        target: {mp}\n        bind:\n          propagation: rslave" for mp in mounts)
    code = optimizer_code_mount()
    code_line = f"\n      - {code}:/app/plexcache:ro" if code else ""
    return ("  plex-cache-optimizer:\n"
            "    image: ghcr.io/drsassafras/plex-cache:latest\n"
            "    restart: unless-stopped\n"
            "    command: [\"python\", \"-m\", \"plexcache.optimize\", \"serve\"]\n"
            "    user: \"568:568\"\n"
            "    volumes:\n"
            f"      - {config}:/config:ro\n"
            f"{media}{code_line}\n"
            "    security_opt:\n"
            "      - no-new-privileges:true\n"
            "    cap_drop:\n"
            "      - ALL\n"
            "    read_only: true\n"
            "    tmpfs:\n"
            "      - /tmp:size=64m\n"
            f"{HEALTHCHECK_YAML}")


def _access_check(app) -> dict:
    """Who can use the web UI: a sign-in, and devices on the local network only (System → Access).
    No sign-in is a note when only the local network gets in, and a warning when anything that can
    reach it does."""
    auth = getattr(app, "auth", None)
    lan = app.settings.get()["general"].get("lan_only", True)
    if auth is not None and auth.enabled:
        return item("pass", M("Sign-in required"),
                    M("Devices on your local network can use it after signing in.") if lan
                    else M("Any device that can reach it can use it after signing in."))
    return item("info" if lan else "warn", M("No sign-in"),
                M("Any device on your local network can change settings and run the optimizer.") if lan
                else M("Any device that can reach it can change settings and run the optimizer."),
                M("To require a sign-in, set PLEXCACHE_USERNAME and PLEXCACHE_PASSWORD in the app’s YAML (Apps → plex-cache → Edit)."))


def _optimizer_check(app) -> dict:
    """The optimizer, in one line: ready, or what's left (the Optimizer page has the steps)."""
    op = getattr(app, "optimizing", None)
    if op is None:
        return item("info", M("Optimizer"), M("Not available."))
    setup = op.diagnose()
    link = {"href": "/optimizer", "text": M("Open the Optimizer page")}
    if setup["ready"]:
        roots = next((x["title"] for x in setup["steps"] if x["id"] == "media"), "")
        return dict(item("pass", M("Optimizer ready"), roots, help=OPTIMIZER_HELP))
    first = next((x for x in setup["steps"] if not x["ok"]), None) or {"id": "yaml", "title": ""}
    if first["id"] == "yaml":
        return dict(item("info", M("Optimizer: not set up"),
                         M("Optional. The Optimizer page shows how to add it."), help=OPTIMIZER_HELP), link=link)
    return dict(item("warn", M("Optimizer: not working yet"), first["title"], help=OPTIMIZER_HELP), link=link)


def check_effectiveness(app) -> dict | None:
    runs = [r for r in app.activity.snapshot()["runs"] if r.get("result") == "ok"]
    if len(runs) < 2:
        return None
    last = runs[0]
    hr = last.get("hit_rate")
    growth = last.get("l2_growth")
    items = []
    if hr is not None and hr < 0.2:
        found = (M("The last run found only {pct}% of files already cached. Data read on the previous run should have been there.",
                   pct=round(hr * 100)) if growth is None else
                 M("The last run found only {pct}% of files already cached and the L2ARC grew {size}. Data read on the "
                   "previous run should have been there.", pct=round(hr * 100), size=_fmt_bytes(max(0, growth))))
        items.append(item("warn", M("Warm-up reads aren’t staying cached"), found,
                          M("Check Media Datasets above (secondarycache=all), and that the L2ARC is large enough "
                            "that other data isn’t pushing the warm-up out between runs.")))
    elif hr is not None:
        items.append(item("pass", M("Warm-up is landing in cache"), M("{pct}% of files were already cached on the last run.", pct=round(hr * 100))))
    if not items:
        return None
    return {"id": "effect", "title": M("Is It Working?"), "hint": M("Compares consecutive warm-up runs."), "items": items}


def recommendations(app) -> list[dict]:
    z = app.zfs or {}
    pools = z.get("pools") or {}
    zds = [d for d in z.get("datasets") or [] if d["dataset"]]
    prim = {d["dataset"]: (d.get("props") or {}).get("primarycache") for d in zds}
    good = [d for d in zds if prim[d["dataset"]] == "all"]
    bad = [d for d in zds if prim[d["dataset"]] not in (None, "all")]
    unknown = [d for d in zds if prim[d["dataset"]] is None]
    l2 = app.l2arc_status()
    ram = app.ram_budget()
    no_ram = ram["usable"] is not None and ram["usable"] <= 0
    no_l2_msg = M("Unavailable until an L2ARC (SSD cache) is added to the media pool.")

    skip = ""
    if bad:
        skip = M(" Skips {list}.", list=[M("{libraries} ({dataset} has primarycache={value})", libraries=_libs(d),
                                             dataset=d["dataset"], value=prim[d["dataset"]]) for d in bad])

    if bad and not good and not unknown:
        why = (M("Won’t work while the media dataset has primarycache={value} — set it to all (see TrueNAS & ZFS).",
                 value=prim[bad[0]["dataset"]]) if len(bad) == 1
               else M("Won’t work while none of the media datasets has primarycache=all — set it to all (see TrueNAS & ZFS)."))
        return [{"feature": "warmup", "verdict": "avoid", "reason": no_l2_msg if l2 == "absent" else why},
                {"feature": "nextepisode", "verdict": "avoid", "reason": why},
                {"feature": "nowplaying", "verdict": "avoid", "reason": why}]

    recs = []
    if l2 == "present":
        no_l2 = [d for d in zds if d["pool"] in pools and not pools[d["pool"]]["l2_online"] and d not in bad]
        total = app.l2_total()
        reason = (M("You have {size} of L2ARC for the lead-in of every title.", size=_fmt_bytes(total)) if total
                  else M("An L2ARC is in use; add a TrueNAS API key to size it."))
        if no_l2:
            skip_l2 = M(" Skips {list}.", list=[M("{libraries} (no L2ARC on pool {pool})", libraries=_libs(d), pool=d["pool"])
                                                for d in no_l2])
        else:
            skip_l2 = ""
        recs.append({"feature": "warmup", "verdict": "use", "reason": M("{reason}{skip}{skip_l2}", reason=reason, skip=skip, skip_l2=skip_l2)})
    elif l2 == "absent":
        recs.append({"feature": "warmup", "verdict": "avoid", "reason": no_l2_msg})
    else:
        recs.append({"feature": "warmup", "verdict": "maybe",
                     "reason": M("Needs an L2ARC on the media pool; connect TrueNAS in System to check.")})
    if no_ram:
        why = M("No ZFS cache left after the reserve — lower “Reserve ZFS cache” in [Caching → Cache Settings](/caching#arc).")
        recs.append({"feature": "nextepisode", "verdict": "avoid", "reason": why})
        recs.append({"feature": "nowplaying", "verdict": "avoid", "reason": why})
        return recs
    recs.append({"feature": "nextepisode", "verdict": "use",
                 "reason": M("Reads a little ahead per show; cheap on any system.{skip}", skip=skip)})
    if good:
        recs.append({"feature": "nowplaying", "verdict": "use",
                     "reason": (M("Your media datasets keep file data in RAM.{skip}", skip=skip) if len(good) > 1
                                else M("Your media dataset keeps file data in RAM.{skip}", skip=skip))})
    else:
        recs.append({"feature": "nowplaying", "verdict": "maybe",
                     "reason": M("Needs primarycache=all on the media datasets; couldn’t check it yet (see TrueNAS & ZFS).{skip}", skip=skip)})
    return recs


def run_all(app) -> dict:
    groups = [check_plex(app), check_media(app), check_datasets(app), check_cache_mode(app), check_zfs(app)]
    eff = check_effectiveness(app)
    if eff:
        groups.append(eff)
    counts = {"pass": 0, "warn": 0, "fail": 0, "info": 0}
    for g in groups:
        for it in g["items"]:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
        for row in g.get("table", []):
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            for key in ("record",):  # the Libraries columns' warnings count too
                if (row.get(key) or {}).get("status") == "warn":
                    counts["warn"] += 1
        for row in g.get("datasets", []):
            for key in ("in_use", "cache", "recordsize", "file_rs", "compression", "l2arc"):
                if row[key]:
                    counts[row[key]["status"]] = counts.get(row[key]["status"], 0) + 1
    # (the counts are in the tiles right under it: the summary doesn't repeat them)
    if counts["fail"]:
        summary = {"status": "fail", "text": M("Something needs fixing — see the red and orange items below.") if counts["warn"]
                   else M("Something needs fixing — see the red items below.")}
    elif counts["warn"]:
        summary = {"status": "warn", "text": M("Working — the orange items below could be better.")}
    else:
        summary = {"status": "ok", "text": M("Everything is OK.")}
    return {"groups": groups, "counts": counts, "summary": summary, "recommendations": recommendations(app),
            "at": time.time()}

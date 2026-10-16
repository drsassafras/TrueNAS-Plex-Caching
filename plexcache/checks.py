"""Health checks and the recommendations that steer the Caching page."""

from __future__ import annotations

import os
import time

from .i18n import Fixed, M, join
from .plex import PlexError

GB = 1e9
GiB = 1024 ** 3


def _fmt_bytes(n: float | None) -> M:
    if n is None:
        return M("unknown")
    for unit, size in ((M("{n} TB"), 1e12), (M("{n} GB"), 1e9), (M("{n} MB"), 1e6)):
        if n >= size:
            return M(unit.tmpl, n=Fixed(n / size, 1 if n < 10 * size else 0))
    return M("{n} B", n=round(n))


def item(status: str, title: str, detail: str = "", fix: str = "", fix_cmd: str = "") -> dict:
    return {"status": status, "title": title, "detail": detail, "fix": fix, "fix_cmd": fix_cmd}


STATUS_MAP = {"ok": "pass", "warn": "warn", "fail": "fail"}


def check_plex(app) -> dict:
    c = app.connections or {}
    items = []
    for key in ("plex_server", "plex_token"):
        r = c.get(key)
        if r and r["status"] in STATUS_MAP:
            items.append(item(STATUS_MAP[r["status"]], r["text"], r["detail"],
                              "" if r["status"] == "ok" else M("Open System → Plex.")))
    if c.get("plex_token", {}).get("status") == "ok":
        if app.scan_error:
            items.append(item("fail", M("Couldn't list libraries"), app.scan_error))
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
                              M("Checking what's playing every {n} s", n=poll)))
    return {"id": "plex", "title": "Plex", "hint": M("Where the app finds your libraries and sees what is playing."),
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
                         "folders": [], "detail": M("Not cached — unticked in System → Media mappings"),
                         "badge": M("Disabled")})
            continue
        row = by_lib.get(lib["key"], {"library": lib["title"], "key": lib["key"], "folders": [], "status": "pass"})
        row["count"] = lib["count"]
        bad = [f for f in row["folders"] if f["status"] != "pass"]
        if bad:
            found = [f for f in bad if f["found_at"]]
            row["detail"] = bad[0]["detail"]
            if found:
                row["fix"] = M("Found “{library}” files at {path} — choose that folder in System → Media mappings.",
                               library=lib["title"], path=found[0]["found_at"][0])
            else:
                row["fix"] = M("Fix its folder in System → Media mappings, or untick it there to stop caching it.")
        else:
            row["detail"] = M("Files readable")
        rows.append(row)
    items = []
    if not rows:
        items.append(item("info", M("No libraries checked yet"), M("Connect Plex first.")))
    return {"id": "media", "title": M("Media access"),
            "hint": M("For each library folder, a real file is opened through this app's mounts."),
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
    """One row of the Media datasets table. Problems carry their fix in ``fixes``."""
    s = app.settings.get()
    any_on = s["nowplaying"]["enabled"] or app.prefetch_active() or app.warmup_active()
    l2_wanted = not s["general"]["no_l2arc"]  # caching is planned around an L2ARC unless switched off
    name = d["dataset"] or d["mountpoint"]
    row = {"dataset": name, "mountpoint": d["mountpoint"], "pool": d["pool"], "libraries": d["libraries"],
           "off": d["off"], "keys": d["keys"], "in_use": None, "cache": None, "recordsize": None, "l2arc": None,
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
        fixes.append(item("warn", M("{name} isn't used by any Plex library", name=name),
                          M("It's mounted in this app at {path}, but none of Plex's library folders are in it, "
                            "so nothing there is ever cached.", path=d["mountpoint"]),
                          M("If a library should be using it, check where that library's folder is in System → Media "
                            "mappings. Otherwise remove the mount from this app (Apps → this app → Edit → Storage).")))
        return row
    libs = _libs(d) if d["libraries"] else M("{list}", list=d["off"])
    cached = bool(d["libraries"])

    if not d["dataset"]:
        if not cached:
            return row
        row["cache"] = cell("warn", M("Not ZFS ({fstype})", fstype=d["fstype"]))
        fixes.append(item("warn", M("{libraries}: not on ZFS here", libraries=libs),
                          M("{path} is a {fstype} mount, so ZFS caching can't be checked or used for {libraries}.",
                            path=d["mountpoint"], fstype=d["fstype"], libraries=libs),
                          M("Mount the dataset directly as a host path (not via SMB/NFS).")))
        return row
    ds = d["dataset"]
    props = d.get("props")
    pool = pools.get(d["pool"])

    need = required_recordsize(d.get("kinds"))
    # A finished "Check every file" beats the sample taken on each health check.
    scan = (getattr(app, "block_scans", {}) or {}).get(ds) or {}
    if scan.get("complete") and scan.get("checked"):
        d = dict(d, blocks=dict(scan, full=True))
    # --- primarycache / secondarycache
    if not tn_ok or props is None:
        row["cache"] = cell("info", M("Not checked")) if cached else None
        # Without the API the setting is unknown, but the files themselves still show how they were written.
        note = _blocks_note(d.get("blocks"), need)
        if note and not note.endswith("match"):
            row["recordsize"] = cell("info", M("Setting not checked{note}", note=note))
    elif not cached:
        pass  # only the record size below
    else:
        prim, sec = props.get("primarycache"), props.get("secondarycache")
        one = len(d["libraries"]) == 1
        if prim is None or sec is None:
            missing = join([n for n, v in (("primarycache", prim), ("secondarycache", sec)) if v is None])
            said = "; ".join(d.get("props_debug") or [])
            row["cache"] = cell("info", M("Couldn't read"))
            fixes.append(item("info", M("Cache settings for {dataset}: couldn't read", dataset=ds),
                              M("TrueNAS didn't return {what} for {dataset} ({libraries}), so this wasn't checked. (TrueNAS answered — {said})",
                                what=missing, dataset=ds, libraries=libs, said=said) if said else
                              M("TrueNAS didn't return {what} for {dataset} ({libraries}), so this wasn't checked.",
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
                                 "never enters RAM — and ZFS only fills L2ARC from RAM, so nothing read from {libraries} gets "
                                 "cached. The app skips it until this is all.", prim=prim, sec=sec, libraries=libs) if one else
                               M("primarycache={prim}, secondarycache={sec}. With primarycache set to {prim}, file data "
                                 "never enters RAM — and ZFS only fills L2ARC from RAM, so nothing read from {libraries} gets "
                                 "cached. The app skips them until this is all.", prim=prim, sec=sec, libraries=libs)),
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

    # --- recordsize: checked for every library, cached or not (it's about how well the files read at all)
    rs = (props or {}).get("recordsize") if tn_ok else None
    if rs:
        st, its = _recordsize_check(app, ds, rs, pool, libs, d.get("kinds"), d.get("blocks"))
        row["recordsize"] = cell(st, M("{size}{note}", size=_fmt_rs(rs), note=_blocks_note(d.get("blocks"), need)))
        fixes.extend(its)

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
        why = (M("Can't check — no library folder is readable yet, so there's nothing to check.")
               if app.libraries else M("Can't check — Plex isn't connected yet."))
        items.append(item("info", M("Media datasets: not checked yet"), why,
                          M("Fix Media access first (System → Media mappings).") if app.libraries else M("Open System → Plex.")))
    elif not tn_ok:
        items.append(item("info", M("Dataset settings: not checked"),
                          M("Cache settings, record size and L2ARC devices need the TrueNAS API (see TrueNAS & ZFS).") if tn_set
                          else M("Cache settings, record size and L2ARC devices need the TrueNAS API."),
                          M("Check System → TrueNAS.")))
    return {"id": "datasets", "title": M("Media datasets"),
            "hint": M("Each dataset your libraries are on, checked on its own."),
            "datasets": rows, "items": items}


def check_zfs(app) -> dict:
    z = app.zfs or {}
    s = app.settings.get()
    items: list[dict] = []
    t = s["truenas"]
    if not (t["url"] and t["api_key"]):
        items.append(item("info", M("TrueNAS API not connected"),
                          M("Add an API key to check each dataset's settings and cache devices, and to size the L2ARC."),
                          M("Open System → TrueNAS.")))
    elif z.get("truenas_error"):
        items.append(item("fail", M("TrueNAS API"), z["truenas_error"], M("Check the address and API key in System.")))
    elif z.get("truenas_version"):
        items.append(item("pass", M("TrueNAS API"), M("Connected to {version} (read-only).", version=z["truenas_version"])))

    arc = z.get("arc")
    if arc:
        items.append(item("pass", M("ARC (RAM cache, whole NAS)"),
                          M("{used} in use of {max} max. Whether caching is helping playback is on the Metrics page.",
                            used=_fmt_bytes(arc["arc_size"]), max=_fmt_bytes(arc["arc_max"]))))
    else:
        items.append(item("warn", M("Can't read ZFS statistics"),
                          M("/proc/spl/kstat/zfs/arcstats isn't readable here, so cache hit and L2ARC growth figures are unavailable.")))

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
        return [item("info", M("RAM for Now Playing Cache and Prefetch"), M("Couldn't read the ZFS RAM cache size, so Now Playing Cache and "
                                                        "Prefetch limits can't be worked out."))]
    on = s["nowplaying"]["enabled"] or app.prefetch_active()
    base = (M("{available} of RAM is available to the ZFS cache (what it holds now plus free RAM; its maximum is {max})",
              available=_fmt_bytes(b["available"]), max=_fmt_bytes(b["cache_max"])) if b["source"] == "arc"
            else M("ZFS statistics aren't readable, so half of RAM ({size}) is assumed", size=_fmt_bytes(b["cache_max"])))
    reserve = M("{size} of it left for the rest of the NAS", size=_fmt_bytes(b["reserve"]))
    if b["usable"] <= 0:
        return [item("fail" if on else "warn", M("Not enough RAM for Now Playing Cache and Prefetch"),
                     M("{base}, and {reserve} leaves nothing for Now Playing Cache or Prefetch.", base=base, reserve=reserve),
                     M("Lower “Reserve ZFS Cache” in Caching → Cache Settings, or add RAM."))]
    limits = M("Now Playing Cache may use up to {nowplaying} and Prefetch {nextepisode}.",
               nowplaying=_fmt_bytes(b["nowplaying"]), nextepisode=_fmt_bytes(b["nextepisode"]))
    if b["nowplaying"] < TIGHT_RAM:
        return [item("warn", M("RAM is tight for Now Playing Cache and Prefetch"),
                     M("{base}; with {reserve}, {limits} That's only a few minutes of a 4K film, so skipping far "
                       "ahead will still read from disk.", base=base, reserve=reserve, limits=limits),
                     M("Add RAM, an L2ARC (SSD cache), or lower “Reserve ZFS Cache” in Caching → Cache Settings."))]
    return [item("pass", M("RAM for Now Playing Cache and Prefetch"), M("{base}; with {reserve}, {limits}", base=base, reserve=reserve, limits=limits))]


L2ARC_HEADER_BYTES = 96  # RAM used per block held in L2ARC (roughly; 70–100 B depending on OpenZFS version)
PERSIST_HINT = M("To keep it after a reboot, add it (without sudo — these run as root) in TrueNAS under System → Advanced Settings → "
                 "Init/Shutdown Scripts (Type: Command, When: Post Init).")


def _blocks_note(blocks: dict | None, need: int):
    """How the files compare with what the dataset needs, saying whether that's a sample:
    " · 300 sampled files match", " · ~62% of a sample at 128K", " · all 21,604 files match",
    " · 1,203 of 21,604 files at 128K"."""
    if not blocks or not blocks.get("checked"):
        return ""
    multi = blocks["checked"] - blocks.get("single", 0)
    old = {int(bs): n for bs, n in (blocks.get("sizes") or {}).items() if int(bs) < need}
    if not multi:
        return ""
    full = blocks.get("full")
    if not old:
        return (M(" · all {n} files match", n=blocks["checked"]) if full
                else M(" · {n} sampled files match", n=blocks["checked"]))
    common = max(old, key=old.get)
    if full:
        return M(" · {old} of {n} files at {size}", old=sum(old.values()), n=blocks["checked"], size=_fmt_rs(common))
    return M(" · ~{pct}% of a sample at {size}", pct=round(sum(old.values()) / multi * 100), size=_fmt_rs(common))


def _fmt_rs(n: int) -> str:
    return f"{n // (1024 * 1024)}M" if n >= 1024 * 1024 else f"{n // 1024}K"


MiB = 1024 * 1024
OLD_BLOCKS_WARN = 0.10  # share of (multi-block) files written with smaller blocks worth a warning


def required_recordsize(kinds) -> int:
    """Movies and TV need 1M; a dataset holding only audio is fine with 512K or 1M."""
    kinds = set(kinds or ())
    return 512 * 1024 if kinds and kinds <= {"audio"} else MiB


def rewrite_cmd(ds: str) -> str:
    # {APP} becomes this app's address in the browser, so the command fetches the matching script.
    return f"curl -fsSO {{APP}}/tools/rewrite_blocks.py && sudo python3 rewrite_blocks.py {ds}"


def _recordsize_check(app, ds: str, recordsize: int | None, pool: dict | None,
                      libs: str = "", kinds=None, blocks: dict | None = None) -> tuple[str, list[dict]]:
    """Media files are big and read in order, so large records suit them: fewer blocks
    means less work per read and much less RAM spent indexing what's in L2ARC.
    Video needs 1M; audio-only datasets may use 512K (tracks are small and warm-up caches
    whole blocks, so 512K wastes less SSD on the first seconds of each song).

    Also compares the files themselves with the setting: a new recordsize only applies to
    files written after it was set.

    Returns the status for the datasets table and any problem to show below it."""
    if not recordsize:
        return "info", []
    need = required_recordsize(kinds)
    audio_only = need < MiB
    on = M(" ({libraries})", libraries=libs) if libs else ""
    # Files written with smaller blocks than this dataset needs, among those big enough to show it.
    old, multi, old_bs = 0, 0, {}
    if blocks and blocks.get("checked"):
        multi = blocks["checked"] - blocks.get("single", 0)
        for bs, n in (blocks.get("sizes") or {}).items():
            if int(bs) < need:
                old += n
                old_bs[int(bs)] = n
    share = old / multi if multi else 0
    common = max(old_bs, key=old_bs.get) if old_bs else None
    if not old:
        old_text = ""
    elif blocks.get("full"):
        ex = (blocks.get("examples") or {}).get(str(common)) or []
        old_text = M("Of all {n} files, {old} ({pct}%) were written with {size} blocks — "
                     "before the record size was changed — and keep them until they're copied again.",
                     n=blocks["checked"], old=old, pct=round(share * 100), size=_fmt_rs(common))
        if ex:
            old_text = M("{text} For example: {files}.", text=old_text, files=ex[:3])
    else:
        old_text = M("In a random sample of {n} files, about {pct}% were written with "
                     "{size} blocks — before the record size was changed — and keep them until they're "
                     "copied again. “Check every file” in the table counts them all.",
                     n=blocks["checked"], pct=round(share * 100), size=_fmt_rs(common))

    if recordsize < need:
        l2 = (pool or {}).get("l2_total") or 0
        ram = ""
        if l2:
            now = l2 / recordsize * L2ARC_HEADER_BYTES
            at_1m = l2 / MiB * L2ARC_HEADER_BYTES
            ram = M(" With the pool's {l2} L2ARC full of this dataset, ZFS would need about "
                    "{ram} of RAM to index it (about {ram_1m} at 1M).",
                    l2=_fmt_bytes(l2), ram=_fmt_bytes(now), ram_1m=_fmt_bytes(at_1m))
        why = (M("Audio-only datasets work well with 512K or 1M records.") if audio_only else
               M("Movies and TV need 1M: the files are large and read start to finish, so 1M records read faster "
                 "and make L2ARC far cheaper to index in RAM."))
        target = "512K" if audio_only and recordsize < 512 * 1024 else "1M"
        return "warn", [item("warn", M("Record size for {dataset}", dataset=ds),
                             M("recordsize={size}{on}. {why}{ram}", size=_fmt_rs(recordsize), on=on, why=why, ram=ram),
                             M("1. Set it in the TrueNAS shell (only files written afterwards use it):"),
                             f"sudo zfs set recordsize={target} {ds}"),
                        item("info", M("Then rewrite the existing files on {dataset}", dataset=ds),
                             M("Files already there keep their old block size until they're copied again. This script "
                               "copies them one at a time, gently, and swaps each one in place; it only looks until you add --apply."),
                             M("2. Run in the TrueNAS shell:"), rewrite_cmd(ds))]
    if old and share >= OLD_BLOCKS_WARN:
        return "warn", [item("warn", M("Older files on {dataset} use smaller blocks", dataset=ds),
                             M("recordsize={size}{on} is right, but the files don't all match. {details}",
                               size=_fmt_rs(recordsize), on=on, details=old_text),
                             M("This script copies them one at a time, gently, and swaps each one in place; "
                               "it only looks until you add --apply. Run in the TrueNAS shell:"), rewrite_cmd(ds))]
    if old:
        return "info", [item("info", M("A few older files on {dataset} use smaller blocks", dataset=ds), old_text,
                             M("Not worth fixing on its own. If you want to, run in the TrueNAS shell:"), rewrite_cmd(ds))]
    return "pass", []


def _l2arc_tuning_checks(app, params: dict) -> list[dict]:
    """ZFS module settings that decide whether warm-up reads actually land in L2ARC."""
    if not any(v is not None for v in params.values()):
        return [item("info", M("L2ARC settings"), M("Couldn't read ZFS module settings from this app, so they weren't checked."))]
    items = []
    ok_bits = []
    sysp = "/sys/module/zfs/parameters"

    mfu = params.get("l2arc_mfuonly")
    if mfu not in (None, "0"):
        items.append(item("warn", M("L2ARC only keeps frequently used data"),
                          M("l2arc_mfuonly={value}: only data read more than once is copied to L2ARC. Warm-up reads "
                            "each file once, so it would never reach the SSD.", value=mfu),
                          M("Run in the TrueNAS shell. {persist}", persist=PERSIST_HINT), f"echo 0 | sudo tee {sysp}/l2arc_mfuonly"))
    elif mfu == "0":
        ok_bits.append(M("all data can be cached (l2arc_mfuonly=0)"))

    rebuild = params.get("l2arc_rebuild_enabled")
    if rebuild == "0":
        items.append(item("warn", M("L2ARC is emptied on every reboot"),
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
        from .warmup import L2ARC_RATE_SHARE, plan
        rate = int(wm) * L2ARC_RATE_SHARE if w["pacing_auto"] else None
        if rate:
            # Automatic pacing matches it; say what that means for a full warm-up
            need = plan(app.stats, w, app.l2_total(), app.index_avg())["need_max"] if app.stats.scanned_at else 0
            hours = need / rate / 3600 if need else 0
            if app.warmup_active() and fill < 64 and hours >= 2:
                items.append(item("info", M("Warm-up is held to {rate} MB/s by the L2ARC", rate=round(rate / 1e6)),
                                  M("ZFS copies at most {fill} MiB/s to the SSD (l2arc_write_max), and automatic L2ARC pacing reads just "
                                    "under that, so nothing is read only to be lost. Warming up every library's lead-ins from scratch "
                                    "takes about {hours} hours at that speed (repeat runs are quicker: what's already on the SSD isn't held back).",
                                    fill=round(fill), hours=round(hours)),
                                  M("To go faster, raise the limit to 128 MiB/s in the TrueNAS shell; pacing follows it. {persist}",
                                    persist=PERSIST_HINT),
                                  f"echo 134217728 | sudo tee {sysp}/l2arc_write_max"))
            else:
                ok_bits.append(M("fills at up to {fill} MiB/s, and L2ARC pacing reads just under that", fill=round(fill)))
        elif app.warmup_active() and reading > fill * 1.1:
            items.append(item("warn", M("L2ARC fills slower than warm-up reads"),
                              M("ZFS copies at most {fill} MiB/s to the SSD (l2arc_write_max), but warm-up reads "
                                "about {reading} MB/s ({pct}% of {disk} MB/s). "
                                "Data can leave RAM before it reaches the SSD, so some reads are wasted.",
                                fill=round(fill), reading=round(reading), pct=w["pacing_percent"], disk=disk),
                              M("Either turn on automatic L2ARC pacing or lower it to about {pct}% (SSD Cache → L2ARC Cache Settings), "
                                "or raise the limit to 128 MiB/s in the TrueNAS shell. {persist}",
                                pct=max(1, int(fill / disk * 100)), persist=PERSIST_HINT),
                              f"echo 134217728 | sudo tee {sysp}/l2arc_write_max"))
        else:
            ok_bits.append(M("fills at up to {fill} MiB/s", fill=round(fill)))

    npf = params.get("l2arc_noprefetch")
    if npf is not None:
        ok_bits.append(M("l2arc_noprefetch={value}", value=npf))
    if ok_bits:
        items.append(item("pass", M("L2ARC settings"), M("L2ARC: {list}.", list=ok_bits)))
    return items


def check_cache_mode(app) -> dict:
    """L2ARC or RAM only, whether settings get what they asked for, and the missing-L2ARC warning."""
    s = app.settings.get()
    items: list[dict] = []
    status, no_l2 = app.l2arc_status(), s["general"]["no_l2arc"]
    if no_l2:
        items.append(item("info", M("RAM-only caching"),
                          M("You chose not to use an L2ARC (SSD Cache page), so settings use their RAM-only defaults "
                            "and SSD-only choices aren't used.")))
    elif status == "present":
        items.append(item("pass", M("L2ARC caching"), M("The media pool has an L2ARC, so settings use their L2ARC defaults.")))
    elif status == "absent":
        items.append(item("warn", M("No L2ARC on the media pool"),
                          M("Caching is planned around an SSD cache (L2ARC). Without one, settings use their RAM-only "
                            "defaults, SSD-only choices fall back, and warm-up can't run."),
                          M("Add an SSD to the pool as a Cache (L2ARC) vdev in TrueNAS → Storage — or, to run on RAM only, "
                            "tick “I won’t use an L2ARC” on the SSD Cache page.")))
    else:
        items.append(item("info", M("L2ARC not checked yet"),
                          M("Couldn't tell whether the media pool has an L2ARC, so settings use their RAM-only defaults for now."),
                          M("Connect TrueNAS in System to check.")))
    fell = app.fallbacks.recent()
    if fell:
        items.append(item("warn", M("Settings not used as chosen"),
                          M("These need an L2ARC, so their RAM-only default was used instead: {list}.",
                            list=[M("{setting} ({chosen} → {used})", setting=f["label"], chosen=f["chosen"], used=f["used"])
                                  for f in fell]),
                          M("Add an L2ARC, or pick a choice without the SSD icon.")))
    else:
        items.append(item("pass", M("Settings used as chosen"), M("No setting has had to fall back in the last 24 hours.")))
    return {"id": "mode", "title": M("Caching mode"),
            "hint": M("Whether caching runs with an SSD cache (L2ARC) or RAM only, and what that changes."), "items": items}


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
                 M("The last run found only {pct}% of files already cached and L2ARC grew {size}. Data read on the "
                   "previous run should have been there.", pct=round(hr * 100), size=_fmt_bytes(max(0, growth))))
        items.append(item("warn", M("Warm-up reads aren't staying cached"), found,
                          M("Check Media datasets above (secondarycache=all), and that L2ARC is large enough "
                            "that other data isn't pushing the warm-up out between runs.")))
    elif hr is not None:
        items.append(item("pass", M("Warm-up is landing in cache"), M("{pct}% of files were already cached on the last run.", pct=round(hr * 100))))
    if not items:
        return None
    return {"id": "effect", "title": M("Is it working?"), "hint": M("Compares consecutive warm-up runs."), "items": items}


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
                  else M("L2ARC is in use; add the TrueNAS API key to size it."))
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
        why = M("No ZFS cache left after the reserve — lower “Reserve ZFS Cache” in Caching → Cache Settings.")
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
                     "reason": M("Needs primarycache=all on the media datasets; couldn't check it yet (see TrueNAS & ZFS).{skip}", skip=skip)})
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
        for row in g.get("datasets", []):
            for key in ("in_use", "cache", "recordsize", "l2arc"):
                if row[key]:
                    counts[row[key]["status"]] = counts.get(row[key]["status"], 0) + 1
    if counts["fail"]:
        problems = M("{n} problems to fix", one="{n} problem to fix", n=counts["fail"])
        if counts["warn"]:
            problems = M("{problems} and {n} warnings", one="{problems} and {n} warning", problems=problems, n=counts["warn"])
        summary = {"status": "fail", "text": M("{problems} — see the red and orange items below.", problems=problems)}
    elif counts["warn"]:
        summary = {"status": "warn", "text": M("Working, with {n} warnings worth a look below.",
                                               one="Working, with {n} warning worth a look below.", n=counts["warn"])}
    else:
        summary = {"status": "ok", "text": M("Everything is OK.")}
    return {"groups": groups, "counts": counts, "summary": summary, "recommendations": recommendations(app),
            "at": time.time()}

"""Health checks and the recommendations that steer the Features page."""

from __future__ import annotations

import os
import time

from .plex import PlexError

GB = 1e9
GiB = 1024 ** 3


def _fmt_bytes(n: float | None) -> str:
    if n is None:
        return "unknown"
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if n >= size:
            return f"{n / size:.1f} {unit}" if n < 10 * size else f"{n / size:.0f} {unit}"
    return f"{n:.0f} B"


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
                              "" if r["status"] == "ok" else "Open Settings → Plex."))
    if c.get("plex_token", {}).get("status") == "ok":
        if app.scan_error:
            items.append(item("fail", "Couldn't list libraries", app.scan_error))
        elif app.libraries:
            n = sum(1 for lib in app.libraries if lib["kind"] and not lib["excluded"])
            items.append(item("pass", "Libraries found", f"{n} libraries, {len(app.files):,} media files"))
        mon = app.monitor
        if mon.last_error:
            items.append(item("warn", "Now-playing feed", mon.last_error))
        elif mon.last_poll:
            poll = app.settings.section("general")["poll_seconds"]
            items.append(item("pass", "Now-playing feed",
                              f"Checking what's playing every {poll} s"))
    return {"id": "plex", "title": "Plex", "hint": "Where the app finds your libraries and sees what is playing.",
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
                         "folders": [], "detail": "Not cached — unticked in Settings → Media mappings",
                         "badge": "Disabled"})
            continue
        row = by_lib.get(lib["key"], {"library": lib["title"], "key": lib["key"], "folders": [], "status": "pass"})
        row["count"] = lib["count"]
        bad = [f for f in row["folders"] if f["status"] != "pass"]
        if bad:
            found = [f for f in bad if f["found_at"]]
            row["detail"] = bad[0]["detail"]
            if found:
                row["fix"] = (f"Found “{lib['title']}” files at {found[0]['found_at'][0]} — choose that folder in "
                              "Settings → Media mappings.")
            else:
                row["fix"] = "Fix its folder in Settings → Media mappings, or untick it there to stop caching it."
        else:
            row["detail"] = "Files readable"
        rows.append(row)
    items = []
    if not rows:
        items.append(item("info", "No libraries checked yet", "Connect Plex first."))
    return {"id": "media", "title": "Media access",
            "hint": "For each library folder, a real file is opened through this app's mounts.",
            "items": items, "table": rows}


def _libs(d: dict) -> str:
    names = d["libraries"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def cell(status: str, text: str, badge: str | None = None) -> dict:
    """One check in the datasets table. ``badge`` replaces the usual Pass/Warn/Info label."""
    out = {"status": status, "text": text}
    if badge:
        out["badge"] = badge
    return out


def _dataset_row(app, d: dict, pools: dict, tn_ok: bool) -> dict:
    """One row of the Media datasets table. Problems carry their fix in ``fixes``."""
    s = app.settings.get()
    warm_on = s["warmup"]["enabled"]
    any_on = warm_on or s["nowplaying"]["enabled"] or s["nextepisode"]["enabled"]
    name = d["dataset"] or d["mountpoint"]
    row = {"dataset": name, "mountpoint": d["mountpoint"], "pool": d["pool"], "libraries": d["libraries"],
           "off": d["off"], "keys": d["keys"], "in_use": None, "cache": None, "recordsize": None, "l2arc": None,
           "fixes": []}
    fixes = row["fixes"]

    # --- what uses it
    if d["libraries"]:
        text = ", ".join(d["libraries"]) + (" · not cached: " + ", ".join(d["off"]) if d["off"] else "")
        row["in_use"] = cell("pass", text)
    elif d["off"]:
        # Nothing is cached from it, so its cache settings and L2ARC don't matter here.
        # Its record size still does: that affects how well Plex itself reads the files.
        row["in_use"] = cell("info", ", ".join(d["off"]) + " · caching off")
    else:
        row["in_use"] = cell("warn", "No Plex library")
        fixes.append(item("warn", f"{name} isn't used by any Plex library",
                          f"It's mounted in this app at {d['mountpoint']}, but none of Plex's library folders are in it, "
                          "so nothing there is ever cached.",
                          "If a library should be using it, check where that library's folder is in Settings → Media "
                          "mappings. Otherwise remove the mount from this app (Apps → this app → Edit → Storage)."))
        return row
    libs = _libs(d) if d["libraries"] else ", ".join(d["off"])
    cached = bool(d["libraries"])

    if not d["dataset"]:
        if not cached:
            return row
        row["cache"] = cell("warn", f"Not ZFS ({d['fstype']})")
        fixes.append(item("warn", f"{libs}: not on ZFS here",
                          f"{d['mountpoint']} is a {d['fstype']} mount, so ZFS caching can't be checked or used for {libs}.",
                          "Mount the dataset directly as a host path (not via SMB/NFS)."))
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
        row["cache"] = cell("info", "Not checked") if cached else None
        # Without the API the setting is unknown, but the files themselves still show how they were written.
        note = _blocks_note(d.get("blocks"), need)
        if note and not note.endswith("match"):
            row["recordsize"] = cell("info", "Setting not checked" + note)
    elif not cached:
        pass  # only the record size below
    else:
        prim, sec = props.get("primarycache"), props.get("secondarycache")
        one = len(d["libraries"]) == 1
        if prim is None or sec is None:
            missing = " and ".join(n for n, v in (("primarycache", prim), ("secondarycache", sec)) if v is None)
            said = "; ".join(d.get("props_debug") or [])
            row["cache"] = cell("info", "Couldn't read")
            fixes.append(item("info", f"Cache settings for {ds}: couldn't read",
                              f"TrueNAS didn't return {missing} for {ds} ({libs}), so this wasn't checked."
                              + (f" (TrueNAS answered — {said})" if said else ""),
                              "You can check by hand in the TrueNAS shell:",
                              f"sudo zfs get primarycache,secondarycache,recordsize {ds}"))
        elif prim != "all":
            # ZFS fills L2ARC only from data that passed through RAM (ARC). With
            # primarycache=metadata file data never enters RAM, so nothing the app
            # reads is cached anywhere — whatever secondarycache says.
            st = "fail" if any_on else "warn"
            row["cache"] = cell(st, f"primarycache={prim}")
            fixes.append(item(st, f"Cache settings for {ds}",
                              f"primarycache={prim}, secondarycache={sec}. With primarycache set to {prim}, file data "
                              f"never enters RAM — and ZFS only fills L2ARC from RAM, so nothing read from {libs} gets "
                              f"cached. The app skips {'it' if one else 'them'} until this is all.",
                              "Run in the TrueNAS shell (ZFS still frees RAM for other things when it needs to):",
                              f"sudo zfs set primarycache=all {ds}"))
        # secondarycache only matters with an SSD cache (L2ARC) on the pool: without one there's
        # nothing for it to allow into, so any value is fine.
        elif sec != "all" and warm_on and (pool is None or pool["l2_online"]):
            row["cache"] = cell("warn", f"secondarycache={sec}")
            fixes.append(item("warn", f"Cache settings for {ds}",
                              f"primarycache=all, secondarycache={sec}. Library warm-up of {libs} needs file data "
                              "to be allowed into L2ARC.",
                              "Run in the TrueNAS shell:", f"sudo zfs set secondarycache=all {ds}"))
        else:
            row["cache"] = cell("pass", f"primarycache=all, secondarycache={sec}")

    # --- recordsize: checked for every library, cached or not (it's about how well the files read at all)
    rs = (props or {}).get("recordsize") if tn_ok else None
    if rs:
        st, its = _recordsize_check(app, ds, rs, pool, libs, d.get("kinds"), d.get("blocks"))
        row["recordsize"] = cell(st, _fmt_rs(rs) + _blocks_note(d.get("blocks"), need))
        fixes.extend(its)

    # --- L2ARC on this dataset's pool
    if not cached:
        return row
    if pool is None:
        row["l2arc"] = cell("info", "Not checked")
    elif pool["l2_online"]:
        row["l2arc"] = cell("pass", f"{pool['l2_online']} device{'s' if pool['l2_online'] != 1 else ''}, "
                                    f"{_fmt_bytes(pool['l2_total'])} on {pool['name']}")
    elif pool["cache_devices"]:
        row["l2arc"] = cell("warn", "Devices not ONLINE")
        fixes.append(item("warn", f"L2ARC on pool {pool['name']}",
                          f"Cache devices are attached to {pool['name']} ({libs}) but none are ONLINE.",
                          "Check the pool in TrueNAS → Storage."))
    else:
        row["l2arc"] = cell("warn" if warm_on else "info", "", badge="None")
        if warm_on:
            fixes.append(item("warn", f"No L2ARC on pool {pool['name']}",
                              f"{libs} {'is' if len(d['libraries']) == 1 else 'are'} on pool {pool['name']}, which has "
                              "no cache devices, so Library warm-up skips them. Now-playing and Next-episode still work.",
                              "Add an SSD to the pool as a Cache (L2ARC) vdev in TrueNAS → Storage."))
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
        why = ("no library folder is readable yet, so there's nothing to check."
               if app.libraries else "Plex isn't connected yet.")
        items.append(item("info", "Media datasets: not checked yet", "Can't check — " + why,
                          "Fix Media access first (Settings → Media mappings)." if app.libraries else "Open Settings → Plex."))
    elif not tn_ok:
        items.append(item("info", "Dataset settings: not checked",
                          "Cache settings, record size and L2ARC devices need the TrueNAS API"
                          + (" (see TrueNAS & ZFS)." if tn_set else "."),
                          "Check Settings → TrueNAS."))
    return {"id": "datasets", "title": "Media datasets",
            "hint": "Each dataset your libraries are on, checked on its own. Record size is checked for every library, "
                    "since it affects how well Plex reads the files whether or not they're cached. For libraries unticked "
                    "in Settings → Media mappings, that's all that's checked.",
            "datasets": rows, "items": items}


def check_zfs(app) -> dict:
    z = app.zfs or {}
    s = app.settings.get()
    items: list[dict] = []
    t = s["truenas"]
    if not (t["url"] and t["api_key"]):
        items.append(item("info", "TrueNAS API not connected",
                          "Add an API key to check each dataset's settings and cache devices, and to size Library warm-up.",
                          "Open Settings → TrueNAS."))
    elif z.get("truenas_error"):
        items.append(item("fail", "TrueNAS API", z["truenas_error"], "Check the address and API key in Settings."))
    elif z.get("truenas_version"):
        items.append(item("pass", "TrueNAS API", f"Connected to {z['truenas_version']} (read-only)."))

    arc = z.get("arc")
    if arc:
        items.append(item("pass", "ARC (RAM cache, whole NAS)",
                          f"{_fmt_bytes(arc['arc_size'])} in use of {_fmt_bytes(arc['arc_max'])} max. "
                          "Whether preloads are helping playback is on the Activity page."))
    else:
        items.append(item("warn", "Can't read ZFS statistics",
                          "/proc/spl/kstat/zfs/arcstats isn't readable here, so cache hit and L2ARC growth figures are unavailable."))

    items.extend(_ram_check(app))
    if app.l2arc_status() == "present":
        items.extend(_l2arc_tuning_checks(app, z.get("params") or {}))
    return {"id": "zfs", "title": "TrueNAS & ZFS",
            "hint": "NAS-wide settings, read from ZFS statistics and the TrueNAS API. Nothing is changed without you.",
            "items": items}


TIGHT_RAM = 2 * GiB  # below this, Now-playing can't hold much of a film


def _ram_check(app) -> list[dict]:
    """Is there RAM to preload into once the system reserve is set aside?"""
    s = app.settings.get()
    b = app.ram_budget()
    if not b["cache_max"]:
        return [item("info", "RAM for preloading", "Couldn't read the ZFS RAM cache size, so Now-playing and "
                     "Next-episode limits can't be worked out.")]
    on = s["nowplaying"]["enabled"] or s["nextepisode"]["enabled"]
    base = (f"{_fmt_bytes(b['available'])} of RAM is available to the ZFS cache (what it holds now plus free RAM; "
            f"its maximum is {_fmt_bytes(b['cache_max'])})" if b["source"] == "arc"
            else f"ZFS statistics aren't readable, so half of RAM ({_fmt_bytes(b['cache_max'])}) is assumed")
    reserve = f"{_fmt_bytes(b['reserve'])} of it left for the rest of the NAS"
    if b["usable"] <= 0:
        return [item("fail" if on else "warn", "Not enough RAM for preloading",
                     f"{base}, and {reserve} leaves nothing for Now-playing or Next-episode.",
                     "Lower “ZFS cache to leave for the rest of the NAS” in Settings → Behaviour, or add RAM.")]
    limits = (f"Now-playing may use up to {_fmt_bytes(b['nowplaying'])} and Next-episode "
              f"{_fmt_bytes(b['nextepisode'])}.")
    if b["nowplaying"] < TIGHT_RAM:
        return [item("warn", "RAM is tight for preloading",
                     f"{base}; with {reserve}, {limits} That's only a few minutes of a 4K film, so skipping far "
                     "ahead will still read from disk.",
                     "Add RAM, an L2ARC (SSD cache), or lower “ZFS cache to leave for the rest of the NAS” in Settings → Behaviour.")]
    return [item("pass", "RAM for preloading", f"{base}; with {reserve}, {limits}")]


L2ARC_HEADER_BYTES = 96  # RAM used per block held in L2ARC (roughly; 70–100 B depending on OpenZFS version)
PERSIST_HINT = ("To keep it after a reboot, add it (without sudo — these run as root) in TrueNAS under System → Advanced Settings → "
                "Init/Shutdown Scripts (Type: Command, When: Post Init).")


def _blocks_note(blocks: dict | None, need: int) -> str:
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
        return f" · all {blocks['checked']:,} files match" if full else f" · {blocks['checked']:,} sampled files match"
    common = max(old, key=old.get)
    if full:
        return f" · {sum(old.values()):,} of {blocks['checked']:,} files at {_fmt_rs(common)}"
    return f" · ~{round(sum(old.values()) / multi * 100)}% of a sample at {_fmt_rs(common)}"


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
    on = f" ({libs})" if libs else ""
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
        old_text = (f"Of all {blocks['checked']:,} files, {old:,} ({round(share * 100)}%) were written with {_fmt_rs(common)} blocks — "
                    "before the record size was changed — and keep them until they're copied again."
                    + (f" For example: {', '.join(ex[:3])}." if ex else ""))
    else:
        old_text = (f"In a random sample of {blocks['checked']:,} files, about {round(share * 100)}% were written with "
                    f"{_fmt_rs(common)} blocks — before the record size was changed — and keep them until they're "
                    "copied again. “Check every file” in the table counts them all.")

    if recordsize < need:
        l2 = (pool or {}).get("l2_total") or 0
        ram = ""
        if l2:
            now = l2 / recordsize * L2ARC_HEADER_BYTES
            at_1m = l2 / MiB * L2ARC_HEADER_BYTES
            ram = (f" With the pool's {_fmt_bytes(l2)} L2ARC full of this dataset, ZFS would need about "
                   f"{_fmt_bytes(now)} of RAM to index it (about {_fmt_bytes(at_1m)} at 1M).")
        why = ("Audio-only datasets work well with 512K or 1M records." if audio_only else
               "Movies and TV need 1M: the files are large and read start to finish, so 1M records read faster "
               "and make L2ARC far cheaper to index in RAM.")
        target = "512K" if audio_only and recordsize < 512 * 1024 else "1M"
        return "warn", [item("warn", f"Record size for {ds}",
                             f"recordsize={_fmt_rs(recordsize)}{on}. {why}{ram}",
                             f"1. Set it in the TrueNAS shell (only files written afterwards use it):",
                             f"sudo zfs set recordsize={target} {ds}"),
                        item("info", f"Then rewrite the existing files on {ds}",
                             "Files already there keep their old block size until they're copied again. This script "
                             "copies them one at a time, gently, and swaps each one in place; it only looks until you add --apply.",
                             "2. Run in the TrueNAS shell:", rewrite_cmd(ds))]
    if old and share >= OLD_BLOCKS_WARN:
        return "warn", [item("warn", f"Older files on {ds} use smaller blocks",
                             f"recordsize={_fmt_rs(recordsize)}{on} is right, but the files don't all match. {old_text}",
                             "This script copies them one at a time, gently, and swaps each one in place; "
                             "it only looks until you add --apply. Run in the TrueNAS shell:", rewrite_cmd(ds))]
    if old:
        return "info", [item("info", f"A few older files on {ds} use smaller blocks", old_text,
                             "Not worth fixing on its own. If you want to, run in the TrueNAS shell:", rewrite_cmd(ds))]
    return "pass", []


def _l2arc_tuning_checks(app, params: dict) -> list[dict]:
    """ZFS module settings that decide whether warm-up reads actually land in L2ARC."""
    if not any(v is not None for v in params.values()):
        return [item("info", "L2ARC settings", "Couldn't read ZFS module settings from this app, so they weren't checked.")]
    items = []
    ok_bits = []
    sysp = "/sys/module/zfs/parameters"

    mfu = params.get("l2arc_mfuonly")
    if mfu not in (None, "0"):
        items.append(item("warn", "L2ARC only keeps frequently used data",
                          f"l2arc_mfuonly={mfu}: only data read more than once is copied to L2ARC. Warm-up reads "
                          "each file once, so it would never reach the SSD.",
                          "Run in the TrueNAS shell. " + PERSIST_HINT, f"echo 0 | sudo tee {sysp}/l2arc_mfuonly"))
    elif mfu == "0":
        ok_bits.append("all data can be cached (l2arc_mfuonly=0)")

    rebuild = params.get("l2arc_rebuild_enabled")
    if rebuild == "0":
        items.append(item("warn", "L2ARC is emptied on every reboot",
                          "l2arc_rebuild_enabled=0, so the SSD cache starts empty after each restart and warm-up "
                          "has to begin again.",
                          "Run in the TrueNAS shell. " + PERSIST_HINT, f"echo 1 | sudo tee {sysp}/l2arc_rebuild_enabled"))
    elif rebuild == "1":
        ok_bits.append("kept across reboots (l2arc_rebuild_enabled=1)")

    wm = params.get("l2arc_write_max")
    if wm and wm.isdigit():
        fill = int(wm) / 1048576
        disk = app.fastest_cached_disk()
        w = app.settings.section("warmup")
        reading = disk * w["pacing_percent"] / 100
        if w["enabled"] and reading > fill * 1.1:
            items.append(item("warn", "L2ARC fills slower than warm-up reads",
                              f"ZFS copies at most {fill:.0f} MiB/s to the SSD (l2arc_write_max), but warm-up reads "
                              f"about {reading:.0f} MB/s ({w['pacing_percent']}% of {disk} MB/s). "
                              "Data can leave RAM before it reaches the SSD, so some reads are wasted.",
                              f"Either lower Background pacing in Features to about {max(1, int(fill / disk * 100))}%, "
                              "or raise the limit to 128 MiB/s in the TrueNAS shell. " + PERSIST_HINT,
                              f"echo 134217728 | sudo tee {sysp}/l2arc_write_max"))
        else:
            ok_bits.append(f"fills at up to {fill:.0f} MiB/s")

    npf = params.get("l2arc_noprefetch")
    if npf is not None:
        ok_bits.append(f"l2arc_noprefetch={npf}")
    if ok_bits:
        text = "; ".join(ok_bits)
        items.append(item("pass", "L2ARC settings", text[0].upper() + text[1:] + "."))
    return items


def check_effectiveness(app) -> dict | None:
    runs = [r for r in app.activity.snapshot()["runs"] if r.get("result") == "ok"]
    if len(runs) < 2:
        return None
    last = runs[0]
    hr = last.get("hit_rate")
    growth = last.get("l2_growth")
    items = []
    if hr is not None and hr < 0.2:
        g = "" if growth is None else f" and L2ARC grew {_fmt_bytes(max(0, growth))}"
        items.append(item("warn", "Warm-up reads aren't staying cached",
                          f"The last run found only {hr * 100:.0f}% of files already cached{g}. "
                          "Data read on the previous run should have been there.",
                          "Check Media datasets above (secondarycache=all), and that L2ARC is large enough "
                          "that other data isn't pushing the warm-up out between runs."))
    elif hr is not None:
        items.append(item("pass", "Warm-up is landing in cache", f"{hr * 100:.0f}% of files were already cached on the last run."))
    if not items:
        return None
    return {"id": "effect", "title": "Is it working?", "hint": "Compares consecutive warm-up runs.", "items": items}


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
    no_l2_msg = "Unavailable until an L2ARC (SSD cache) is added to the media pool."

    skip = ""
    if bad:
        skip = " Skips " + "; ".join(f"{_libs(d)} ({d['dataset']} has primarycache={prim[d['dataset']]})" for d in bad) + "."

    if bad and not good and not unknown:
        what = (f"the media dataset has primarycache={prim[bad[0]['dataset']]}" if len(bad) == 1
                else "none of the media datasets has primarycache=all")
        why = f"Won’t work while {what} — set it to all (see TrueNAS & ZFS)."
        return [{"feature": "warmup", "verdict": "avoid", "reason": no_l2_msg if l2 == "absent" else why},
                {"feature": "nextepisode", "verdict": "avoid", "reason": why},
                {"feature": "nowplaying", "verdict": "avoid", "reason": why}]

    recs = []
    if l2 == "present":
        no_l2 = [d for d in zds if d["pool"] in pools and not pools[d["pool"]]["l2_online"] and d not in bad]
        total = app.l2_total()
        reason = (f"You have {_fmt_bytes(total)} of L2ARC for the start of every title." if total
                  else "L2ARC is in use; add the TrueNAS API key to size it.")
        if no_l2:
            skip_l2 = " Skips " + "; ".join(f"{_libs(d)} (no L2ARC on pool {d['pool']})" for d in no_l2) + "."
        else:
            skip_l2 = ""
        recs.append({"feature": "warmup", "verdict": "use", "reason": reason + skip + skip_l2})
    elif l2 == "absent":
        recs.append({"feature": "warmup", "verdict": "avoid", "reason": no_l2_msg})
    else:
        recs.append({"feature": "warmup", "verdict": "maybe",
                     "reason": "Needs an L2ARC on the media pool; connect TrueNAS in Settings to check."})
    if no_ram:
        why = "No ZFS cache left after the reserve — lower “ZFS cache to leave for the rest of the NAS” in Settings."
        recs.append({"feature": "nextepisode", "verdict": "avoid", "reason": why})
        recs.append({"feature": "nowplaying", "verdict": "avoid", "reason": why})
        return recs
    recs.append({"feature": "nextepisode", "verdict": "use",
                 "reason": "Reads a little ahead per show; cheap on any system." + skip})
    if good:
        recs.append({"feature": "nowplaying", "verdict": "use",
                     "reason": ("Your media datasets keep file data in RAM." if len(good) > 1
                                else "Your media dataset keeps file data in RAM.") + skip})
    else:
        recs.append({"feature": "nowplaying", "verdict": "maybe",
                     "reason": "Needs primarycache=all on the media datasets; couldn't check it yet (see TrueNAS & ZFS)." + skip})
    return recs


def run_all(app) -> dict:
    groups = [check_plex(app), check_media(app), check_datasets(app), check_zfs(app)]
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
        summary = {"status": "fail", "text": f"{counts['fail']} problem{'s' if counts['fail'] != 1 else ''} to fix"
                   + (f" and {counts['warn']} warning{'s' if counts['warn'] != 1 else ''}" if counts["warn"] else "")
                   + " — see the red and orange items below."}
    elif counts["warn"]:
        summary = {"status": "warn", "text": f"Working, with {counts['warn']} warning{'s' if counts['warn'] != 1 else ''} "
                                             "worth a look below."}
    else:
        summary = {"status": "ok", "text": "Everything is OK."}
    return {"groups": groups, "counts": counts, "summary": summary, "recommendations": recommendations(app),
            "at": time.time()}

"""Health checks and the recommendations that steer the Features page."""

from __future__ import annotations

import os
import time

from .plex import PlexError

GB = 1e9


def _fmt_bytes(n: float | None) -> str:
    if n is None:
        return "unknown"
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if n >= size:
            return f"{n / size:.1f} {unit}" if n < 10 * size else f"{n / size:.0f} {unit}"
    return f"{n:.0f} B"


def item(status: str, title: str, detail: str = "", fix: str = "", fix_cmd: str = "") -> dict:
    return {"status": status, "title": title, "detail": detail, "fix": fix, "fix_cmd": fix_cmd}


def _readable_sample(paths: list[str]) -> tuple[bool, str]:
    for p in paths:
        try:
            with open(p, "rb") as fh:
                fh.read(1)
            return True, p
        except OSError as e:
            err = f"{p}: {e.strerror}"
    return False, err if paths else "no files to test"


def check_plex(app) -> dict:
    s = app.settings.section("plex")
    items = []
    if not s["url"] or not s["token"]:
        items.append(item("fail", "Plex isn't set up yet", "Add your Plex server address and sign in.",
                          "Open Settings → Plex server."))
        return {"id": "plex", "title": "Plex", "hint": "Where the app finds your libraries and sees what is playing.", "items": items}
    try:
        info = app.plex(timeout=10).ping()
        items.append(item("pass", "Server reachable", f"{s['url']} answered in {info['ms']} ms"))
        items.append(item("pass", "Token accepted", f"Server “{info['name']}”, Plex {info['version']}"))
    except PlexError as e:
        bad_token = "token" in str(e).lower()
        items.append(item("fail", "Token rejected" if bad_token else "Server not reachable", str(e),
                          "Sign in with Plex again in Settings." if bad_token else
                          "Check the address in Settings. Use your NAS's IP, not 127.0.0.1 — inside this app that means the app itself."))
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
        items.append(item("pass", "Now-playing feed", f"Checking active sessions every {poll} s — no Tautulli or Plex Pass needed"))
    return {"id": "plex", "title": "Plex", "hint": "Where the app finds your libraries and sees what is playing.", "items": items}


def check_media(app) -> dict:
    rows = []
    for lib in app.libraries:
        if not lib["kind"]:
            continue
        locs = lib["locations"]
        mapped = [app.map_path(p) for p in locs]
        row = {"library": lib["title"], "key": lib["key"], "plex_paths": locs, "local_paths": mapped,
               "count": lib["count"], "kind": lib["kind"], "excluded": lib["excluded"]}
        if lib["excluded"]:
            row.update(status="info", detail="Excluded in Settings")
        else:
            missing = [m for m in mapped if not os.path.isdir(m)]
            if missing:
                row.update(status="warn", detail=f"{missing[0]} doesn't exist in this app",
                           fix=f"Plex reads “{lib['title']}” from {locs[mapped.index(missing[0])]}, which isn't mounted here. "
                               f"Edit the app in TrueNAS and add a host path mounted at {missing[0]} (read-only), "
                               f"or exclude the library in Settings.")
            else:
                ok, detail = _readable_sample([app.map_path(p) for p in lib.get("sample", [])])
                if ok or not lib.get("sample"):
                    row.update(status="pass", detail="Files readable")
                else:
                    row.update(status="fail", detail=detail,
                               fix="The folder is mounted but files can't be read. Give the app's user (568, “apps”) "
                                   "read access to the dataset, or check that the mount matches the one Plex uses.")
        rows.append(row)
    items = []
    if not rows:
        items.append(item("info", "No libraries checked yet", "Connect Plex first."))
    return {"id": "media", "title": "Media access",
            "hint": "For each library, a real file is opened through this app's mounts. Mount your media at the same path Plex uses.",
            "items": items, "table": rows}


def check_zfs(app) -> dict:
    z = app.zfs or {}
    s = app.settings.get()
    items = []
    mount = z.get("mount")
    pool = z.get("pool_info")
    if mount and mount.get("dataset"):
        layout = f" ({pool['data_disks']} disks, {pool['layout']})" if pool else ""
        items.append(item("pass", "Media dataset", f"{mount['dataset']} on pool {mount['pool']}{layout}"))
    elif mount:
        items.append(item("warn", "Media isn't on ZFS here",
                          f"{z.get('media_path')} is on a {mount['fstype']} mount, so ZFS caching can't be checked.",
                          "Mount the dataset directly as a host path (not via SMB/NFS)."))
    elif z.get("media_path") is None and app.libraries:
        items.append(item("warn", "Media dataset not found", "None of the library folders are mounted in this app yet."))

    arc = z.get("arc")
    if arc:
        ratio = f" · {arc['arc_hit_ratio'] * 100:.0f}% hit rate since boot" if arc["arc_hit_ratio"] is not None else ""
        items.append(item("pass", "ARC (RAM cache)", f"{_fmt_bytes(arc['arc_size'])} in use of {_fmt_bytes(arc['arc_max'])} max{ratio}"))
    else:
        items.append(item("warn", "Can't read ZFS statistics",
                          "/proc/spl/kstat/zfs/arcstats isn't readable here, so cache hit and L2ARC growth figures are unavailable."))

    tn_set = bool(s["truenas"]["url"] and s["truenas"]["api_key"])
    if not tn_set:
        items.append(item("info", "TrueNAS API not connected",
                          "Add an API key to check cache devices and dataset settings, and to size Library warm-up.",
                          "Open Settings → TrueNAS."))
    elif z.get("truenas_error"):
        items.append(item("fail", "TrueNAS API", z["truenas_error"], "Check the address and API key in Settings."))

    params = z.get("params") or {}
    write_max = params.get("l2arc_write_max")
    write_note = f" · L2ARC fill rate limit {int(write_max) / 1048576:.0f} MiB/s" if write_max and write_max.isdigit() else ""
    if pool:
        if pool["l2_online"]:
            filled = f" · {_fmt_bytes(arc['l2_asize'])} filled" if arc else ""
            items.append(item("pass", "L2ARC cache devices",
                              f"{pool['l2_online']} online, {_fmt_bytes(pool['l2_total'])} total{filled}{write_note}"))
        elif pool["cache_devices"]:
            items.append(item("warn", "L2ARC cache devices", "Cache devices are attached but none are ONLINE."))
        else:
            items.append(item("warn" if s["warmup"]["enabled"] else "info", "No L2ARC cache devices",
                              f"Pool {pool['name']} has no cache devices. Library warm-up needs at least one SSD added as cache."))
    elif arc and arc["l2_present"]:
        items.append(item("info", "L2ARC in use", f"{_fmt_bytes(arc['l2_asize'])} cached on SSD{write_note}. "
                          "Device size needs the TrueNAS API."))

    props = z.get("dataset_props")
    if props and mount:
        ds = mount["dataset"]
        playback_on = s["nowplaying"]["enabled"] or s["nextepisode"]["enabled"]
        prim, sec = props["primarycache"], props["secondarycache"]
        detail = f"primarycache={prim}, secondarycache={sec}."
        if sec != "all" and s["warmup"]["enabled"]:
            items.append(item("warn", "Dataset cache settings", detail + " Library warm-up needs file data in L2ARC.",
                              "Run in the TrueNAS shell:", f"zfs set secondarycache=all {ds}"))
        elif prim != "all" and playback_on:
            items.append(item("warn", "Dataset cache settings",
                              detail + " File data won't stay in RAM, so Now-playing preload and Next-episode prefetch "
                              "only help if L2ARC keeps the reads.",
                              "If you use the playback features, consider caching data in RAM. Run in the TrueNAS shell:",
                              f"zfs set primarycache=all {ds}"))
        else:
            items.append(item("pass", "Dataset cache settings", detail))
    return {"id": "zfs", "title": "TrueNAS & ZFS",
            "hint": "Read from ZFS statistics and the TrueNAS API. Nothing is changed without you.", "items": items}


def check_disk(app) -> dict:
    g = app.settings.section("general")
    items = []
    if g["hdd_measured_at"]:
        items.append(item("pass", f"Measured {g['hdd_mb_per_sec']} MB/s uncached",
                          f"Median of a few files per library · measured {g['hdd_measured_at'].replace('T', ' ')}"))
    else:
        items.append(item("warn", f"Using a guess of {g['hdd_mb_per_sec']} MB/s",
                          "Benchmark your disks so cache hits can be told apart from disk reads.", "Press “Benchmark now”."))
    return {"id": "disk", "title": "Disk speed",
            "hint": "Used to tell cache hits from disk reads.", "items": items, "action": "benchmark"}


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
                          "Check Dataset cache settings above (secondarycache=all), and that L2ARC is large enough "
                          "that other data isn't pushing the warm-up out between runs."))
    elif hr is not None:
        items.append(item("pass", "Warm-up is landing in cache", f"{hr * 100:.0f}% of files were already cached on the last run."))
    if not items:
        return None
    return {"id": "effect", "title": "Is it working?", "hint": "Compares consecutive warm-up runs.", "items": items}


def recommendations(app) -> list[dict]:
    z = app.zfs or {}
    pool = z.get("pool_info")
    arc = z.get("arc")
    props = z.get("dataset_props")
    recs = []
    if pool is not None:
        if pool["l2_online"]:
            recs.append({"feature": "warmup", "verdict": "use",
                         "reason": f"You have {_fmt_bytes(pool['l2_total'])} of L2ARC for the start of every title."})
        else:
            recs.append({"feature": "warmup", "verdict": "avoid", "reason": "Needs an SSD cache (L2ARC) on the media pool."})
    elif arc and arc["l2_present"]:
        recs.append({"feature": "warmup", "verdict": "use", "reason": "L2ARC is in use; add the TrueNAS API key to size it."})
    else:
        recs.append({"feature": "warmup", "verdict": "maybe", "reason": "Only useful if the media pool has an L2ARC; couldn't confirm."})
    recs.append({"feature": "nextepisode", "verdict": "use", "reason": "Reads a little ahead per show; cheap on any system."})
    if props and props["primarycache"] == "all":
        recs.append({"feature": "nowplaying", "verdict": "use", "reason": "Your dataset keeps file data in RAM."})
    elif props:
        recs.append({"feature": "nowplaying", "verdict": "maybe",
                     "reason": f"Works best with primarycache=all (yours is {props['primarycache']}). Otherwise keep the amount small."})
    else:
        recs.append({"feature": "nowplaying", "verdict": "maybe", "reason": "Helps if file data can stay in RAM; keep the amount capped."})
    return recs


def run_all(app) -> dict:
    groups = [check_plex(app), check_media(app), check_zfs(app), check_disk(app)]
    eff = check_effectiveness(app)
    if eff:
        groups.append(eff)
    counts = {"pass": 0, "warn": 0, "fail": 0, "info": 0}
    for g in groups:
        for it in g["items"]:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
        for row in g.get("table", []):
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"groups": groups, "counts": counts, "recommendations": recommendations(app), "at": time.time()}

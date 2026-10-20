"""MQTT: status for Home Assistant, Node-RED, openHAB and anything else that speaks MQTT.

Off until switched on in System. Everything is in one retained JSON message
(<base>/state); commands arrive on <base>/set/<name>; <base>/availability says
online/offline. With discovery on, Home Assistant's MQTT discovery messages make the
"Plex Cache" device and its entities appear on their own.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time

from . import REPO_URL
from . import prefetch
from .config import SettingsError
from .i18n import M, msg, render
from .mqttclient import MQTTClient, MQTTError, topic_matches

log = logging.getLogger("plexcache")
GB = 1e9

STEPS = ["broker", "tls", "login", "publish"]
RETRY_FIRST, RETRY_MAX = 5, 60  # seconds between attempts: 5, 10, 20, 40, then every minute
PERIOD_NAME = {"today": M("today"), "week": M("7 days"), "month": M("30 days")}
WARMUP_STATES = ["off", "idle", "preparing", "running", "paused"]
HEALTH_STATES = ["ok", "warning", "problem", "checking"]
POLL = {str(n): M("{n} seconds", one="{n} second", n=n) for n in (3, 5, 10, 30)}


def prefetch_choices(kind: str, field: str) -> dict:
    """value -> name of a library's Prefetch choices (Default first), as Home Assistant lists them."""
    levels = prefetch.levels_for(kind) if field == "start" else prefetch.END_LEVELS
    return {"default": M("Default"), **{v: prefetch.NAMES[kind][v] for v in levels}}
WARMUP_RESULT = {"ok": M("ok"), "stopped": M("stopped"), "errors": M("errors")}


def _choice(choices: dict, value: str, lang: str = "en") -> str:
    """A choice by its label ("10 seconds", as Home Assistant sends it, in the MQTT language
    or in English) or its value ("10")."""
    if value in choices:
        return value
    v = value.strip().lower()
    return next(k for k, label in choices.items() if v in (str(label).lower(), render(label, lang).lower()))


def localize(obj, lang: str):
    """``obj`` with every message rendered in ``lang`` (MQTT has no browser to do it)."""
    if isinstance(obj, M):
        return render(obj, lang)
    if isinstance(obj, dict):
        return {k: localize(v, lang) for k, v in obj.items()}
    if isinstance(obj, list):
        return [localize(v, lang) for v in obj]
    return obj


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "x"


def _gb(n) -> float | None:
    return round(n / GB, 2) if n is not None else None


def _pct(a, b) -> float | None:
    return round(100 * a / b, 1) if b else None


def _iso(ts) -> str | None:
    if not ts:
        return None
    if isinstance(ts, str):  # "2026-10-14T03:00" in local time
        try:
            ts = dt.datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------- state
# The web pages' summaries point at lists further down the page ("see below"). Home Assistant
# shows them on their own, so these say what the lists would have said.
def _verdict(rep: dict):
    verdict = rep.get("verdict") or {}
    if verdict.get("status") != "warn":
        return verdict.get("text")
    titles = [a["title"] for a in rep.get("attention") or [] if a.get("status") == "warn"]
    joined = titles[0] if titles else None
    for t in titles[1:]:
        joined = M("{a}; {b}", a=joined, b=t)
    return M("{period}: {list}", period=rep.get("span") or "", list=joined) if titles else verdict.get("text")


def _health_text(health: dict):
    summary, counts = health.get("summary") or {}, health.get("counts") or {}
    if summary.get("status") not in ("fail", "warn"):
        return summary.get("text")
    fails, warns = counts.get("fail", 0), counts.get("warn", 0)
    if fails and warns:
        what = M("{problems} and {n} warnings", one="{problems} and {n} warning",
                 problems=M("{n} problems to fix", one="{n} problem to fix", n=fails), n=warns)
        return M("{what} — open Health in Plex Cache for the details.", what=what)
    if fails:
        return M("{n} problems to fix — open Health in Plex Cache for the details.",
                 one="{n} problem to fix — open Health in Plex Cache for the details.", n=fails)
    return M("{n} warnings — open Health in Plex Cache for the details.",
             one="{n} warning — open Health in Plex Cache for the details.", n=warns)


def update_info(app, lang: str = "en") -> dict:
    """For Home Assistant's update entity: the installed version and the newest one."""
    latest = (getattr(app, "updates", None) and app.updates.latest) or app.version
    info = {"installed_version": app.version, "latest_version": latest, "title": "Plex Cache",
            "release_url": REPO_URL}
    if latest != app.version:
        info["release_summary"] = render(M("A newer version is on GitHub. Update the app in TrueNAS so it pulls the latest image."), lang)
    return info


def build_state(app, period: str = "week", lang: str = "en") -> dict:
    """Everything the entities read, as one JSON object, with its text in ``lang``."""
    s = app.settings.get()
    st = app.state()
    health = st.get("health") or {}
    counts = health.get("counts") or {}
    summary = (health.get("summary") or {}).get("status")
    conns = st.get("connections") or {}
    ram = st.get("ram") or {}
    used = ram.get("in_use") or {}
    arc = (st.get("zfs") or {}).get("arc") or {}
    wu = st.get("warmup") or {}
    prog = wu.get("progress") or {}
    runs = (st.get("activity") or {}).get("runs") or []
    last = runs[0] if runs else {}
    rep = (st.get("reports") or {}).get(period) or {}
    tiles = rep.get("tiles") or {}
    fully = tiles.get("fully") or {}
    route = ((st.get("survival") or {}).get("routes") or {}).get("ssd" if app.cache_mode() == "l2arc" else "ram") or {}
    # How much of every library was cached within 1 h, 24 h, 7 d, 30 d, 6 months, at the last hour recorded (%)
    hours = app.coverage.series({}).get("all") or []
    latest = next((h for h in reversed(hours) if h), None)
    cover = [x / 10 for x in latest[0]] if latest else [None] * 5
    totals = dict(app.activity.totals)
    if not any(v != "none" for v in app.warmup_schedules().values()):
        wstate = "off"  # no library has a warm-up
    elif not wu.get("running"):
        wstate = "idle"
    elif prog.get("paused"):
        wstate = "paused"
    elif not prog.get("total"):
        wstate = "preparing"
    else:
        wstate = "running"
    sessions = (st.get("playback") or {}).get("sessions") or []
    datasets = {}
    for g in health.get("groups") or []:
        for r in g.get("datasets") or []:
            rs, cache = r.get("recordsize") or {}, r.get("cache") or {}
            datasets[_slug(r["dataset"])] = {
                "name": r["dataset"], "record_size": rs.get("text"), "record_size_status": rs.get("status"),
                "cache_settings": cache.get("text"), "cache_status": cache.get("status"),
            }
    l2 = (st.get("zfs") or {}).get("l2_total")
    out = {
        "version": st["version"],
        # Health
        "health": "checking" if st.get("checking") and not summary else
                  {"ok": "ok", "warn": "warning", "fail": "problem"}.get(summary, "checking"),
        "health_text": _health_text(health),
        "health_passed": counts.get("pass", 0),
        "health_warnings": counts.get("warn", 0),
        "health_failures": counts.get("fail", 0),
        "health_checked": _iso(health.get("at")),
        "problem": "ON" if summary == "fail" else "OFF",
        "plex_connected": "ON" if (conns.get("plex_token") or {}).get("status") == "ok" else "OFF",
        "truenas_connected": "ON" if (conns.get("truenas_key") or {}).get("status") == "ok" else "OFF",
        # Playing now
        "streams": len(sessions),
        "streaming": "ON" if sessions else "OFF",
        "preloading": app.monitor.preloading() or M("Idle"),
        "preloading_active": "ON" if app.monitor.preloading() else "OFF",
        # RAM
        "nowplaying_ram_used": _gb(used.get("nowplaying", 0)),
        "nowplaying_ram_limit": _gb(ram.get("nowplaying")),
        "nextepisode_ram_used": _gb(used.get("nextepisode", 0)),
        "nextepisode_ram_limit": _gb(ram.get("nextepisode")),
        "zfs_cache_available": _gb(ram.get("available")),
        "ram_reserve": _gb(ram.get("reserve")),
        "arc_size": _gb(arc.get("arc_size")),
        "arc_hit_ratio": round(arc["arc_hit_ratio"] * 100, 1) if arc.get("arc_hit_ratio") is not None else None,
        # L2ARC
        "l2arc_size": _gb(l2),
        "l2arc_cached": _gb(arc.get("l2_size")),
        # Warm-up
        "warmup_state": wstate,
        "warmup_running": "ON" if wu.get("running") else "OFF",
        "warmup_progress": _pct(prog.get("done", 0), prog.get("total")) if wu.get("running") else None,
        "warmup_files_done": prog.get("done") if wu.get("running") else None,
        "warmup_files_total": prog.get("total") if wu.get("running") else None,
        "warmup_read": _gb(prog.get("bytes")) if wu.get("running") else None,
        "warmup_pause_reason": prog.get("paused") or ("" if wu.get("running") else None),
        "warmup_next_run": _iso(wu.get("next_run")),
        "warmup_last_run": _iso(last.get("started")),
        "warmup_last_result": WARMUP_RESULT.get(last.get("result"), last.get("result")),
        "warmup_last_cached": round(last["hit_rate"] * 100, 1) if last.get("hit_rate") is not None else None,
        "warmup_last_l2arc_growth": _gb(last.get("l2_growth")),
        "warmup_last_files": last.get("files"),
        # The Metrics page's report, for the chosen period
        "report_period": PERIOD_NAME.get(period, period),
        "report_verdict": _verdict(rep),
        "report_streams": rep.get("streams"),
        "skips": (tiles.get("skips") or {}).get("total"),
        "skips_cached": (tiles.get("skips") or {}).get("cached"),
        # (expected: each skip counted by how likely it came from cache, as the Metrics page shows it)
        "skips_cached_pct": _pct((tiles.get("skips") or {}).get("expected", 0), (tiles.get("skips") or {}).get("total")),
        "lead_in_served_pct": (tiles.get("lead") or {}).get("pct"),
        "fully_cached_age": round(fully["age"] / 60) if fully.get("n") else None,
        "suggested_cooldown": round(route["suggest"] / 60) if route.get("suggest") else None,
        "cooldown_in_use": round(app.cooldown_seconds(False) / 60),
        "library_cached_24h": cover[1], "library_cached_7d": cover[2], "library_cached_30d": cover[3],
        "still_cached_pct": (tiles.get("retention") or {}).get("pct"),
        "preloaded": _gb((tiles.get("preloaded") or {}).get("bytes")),
        "preloaded_already_cached_pct": (tiles.get("preloaded") or {}).get("cached_pct"),
        "disk_busy_avg": (rep.get("disk") or {}).get("avg"),
        "disk_busy_peak": (rep.get("disk") or {}).get("peak"),
        "attention": len(rep.get("attention") or []),
        # Lifetime totals (only go up)
        "total_streams": totals["streams"],
        "total_skips": totals["skips"],
        "total_skips_cached": totals["skips_cached"],
        "total_preloaded": _gb(totals["preloaded_bytes"]),
        # Background jobs
        "checking": "ON" if st.get("checking") else "OFF",
        "benchmarking": "ON" if (st.get("benchmark") or {}).get("running") else "OFF",
        # Settings (for the controls)
        "nowplaying_enabled": "ON" if s["nowplaying"]["enabled"] else "OFF",
        "nowplaying_ram_percent": s["nowplaying"]["ram_percent"],
        "nextepisode_ram_percent": s["nextepisode"]["ram_percent"],
        "warmup_pacing_percent": s["warmup"]["pacing_percent"],
        "warmup_pacing_auto": "ON" if s["warmup"]["pacing_auto"] else "OFF",
        "ram_reserve_gb": s["general"]["ram_reserve_gb"],
        "poll_seconds": POLL.get(str(s["general"]["poll_seconds"]),
                                 M("{n} seconds", one="{n} second", n=s["general"]["poll_seconds"])),
        "datasets": datasets,
        "libraries": {},
        "update": update_info(app, lang),
    }
    out["available"] = control_availability(app, s, wu)
    for lib in st["library"]["libraries"]:
        kind = prefetch.library_kind(lib)
        if kind and not lib.get("excluded"):
            mine = prefetch.library_settings(s["nextepisode"], lib["key"])
            out["libraries"][lib["key"]] = {"name": lib["title"], "kind": kind, "items": lib.get("count"),
                                            "prefetch_start": prefetch_choices(kind, "start")[mine["start"]],
                                            "prefetch_end": prefetch_choices(kind, "end")[mine["end"]]}
    out = localize(out, lang)
    for k in ("health_text", "report_verdict", "preloading", "warmup_pause_reason", "warmup_last_result"):
        if isinstance(out[k], str) and len(out[k]) > 250:  # Home Assistant keeps at most 255 characters of a state
            out[k] = out[k][:249] + "…"
    return out


# ---------------------------------------------------------------- discovery
# Shown in Home Assistant when you open an entity (its "description" attribute), from one
# retained message on <base>/info. Plain text, a sentence or two: the web UI's ⓘ, shortened.
DESCRIPTIONS = {
    # Plex Cache
    "health": M("Overall result of the health checks: ok, warning (features still run), problem (something needs fixing) or checking."),
    "health_text": M("A one-line summary of the health checks. The Health page in Plex Cache lists every check with its fix."),
    "problem": M("On when a health check failed: something needs fixing before every feature can work."),
    "streams": M("How many Plex streams are playing right now."),
    "streaming": M("On while anything is playing on Plex."),
    "preloading": M("What Now Playing Cache or Prefetch is reading right now, or Idle."),
    "preloading_active": M("On while Now Playing Cache or Prefetch is reading from the disks."),
    "health_passed": M("How many health checks passed."),
    "health_warnings": M("How many health checks gave a warning. Features still run; the Health page says what could be better."),
    "health_failures": M("How many health checks failed. The Health page says how to fix each one."),
    "health_checked": M("When the health checks last ran. They run at startup, every 6 hours and whenever settings change."),
    "plex_connected": M("On when Plex Cache can reach your Plex server with a working sign-in."),
    "truenas_connected": M("On when Plex Cache can reach the TrueNAS API with its (read-only) API key."),
    "checking": M("On while the health checks are running."),
    "benchmarking": M("On while the disk benchmark is measuring each dataset’s uncached read speed."),
    "version": M("The version of Plex Cache that’s running."),
    "update": M("Whether a newer Plex Cache is on GitHub (checked every 6 hours, if switched on in System). Update the app in TrueNAS."),
    # Activity
    "report_verdict": M("The Metrics page’s verdict for the chosen period (System → MQTT): what could be better, or that all went well."),
    "report_streams": M("Streams played in the report period."),
    "skips": M("Times someone jumped more than 30 seconds within a title, in the report period."),
    "skips_cached": M("Skips that landed on a part Now Playing Cache had already loaded, so they played from cache."),
    "warmup_pacing_auto": M("L2ARC pacing: on, warm-up and Prefetch’s L2ARC-only reads go just under how fast ZFS copies to the SSD "
                            "(l2arc_write_max). Off, they use the share of disk time in L2ARC pacing."),
    "skips_cached_pct": M("Share of skips expected to have played from cache: each counted by how likely that part was still "
                          "cached, from how long ago it was cached and how long data lasts in its library. Higher is better."),
    "lead_in_served_pct": M("Share of plays that started from a lead-in (or whole file) already cached — by warm-up, Prefetch "
                            "or an earlier play — each counted by how likely it was still there. Higher is better."),
    "fully_cached_age": M("Everything read again up to this age was still cached, in the report period: the oldest re-read "
                          "before the first one that had to come from the disks."),
    "suggested_cooldown": M("The fully cached age over the last 7 days, every library: reading cached data again sooner "
                            "would be for nothing. Automatic cooldowns use it, per library."),
    "cooldown_in_use": M("The lead-in cooldown in use now: Automatic’s, or the one set in the web UI."),
    "library_cached_24h": M("Share of the library’s files this app cached within the last 24 hours (in RAM or on the SSD), "
                            "every library together."),
    "library_cached_7d": M("Share of the library’s files this app cached within the last 7 days (in RAM or on the SSD), "
                           "every library together."),
    "library_cached_30d": M("Share of the library’s files this app cached within the last 30 days (in RAM or on the SSD), "
                            "every library together."),
    "still_cached_pct": M("About 30 minutes after a read, how much of it was still in RAM. Low means other work pushes it out too soon."),
    "preloaded": M("How much data Now Playing Cache and Prefetch read into RAM for viewers in the report period (not counting reads on their way to the SSD)."),
    "preloaded_already_cached_pct": M("Share of what they read that was already cached (it read much faster than the disks can)."),
    "disk_busy_avg": M("How busy the media pool’s disks were, on average, while people watched."),
    "disk_busy_peak": M("The busiest the media pool’s disks got while people watched. Near 100% leaves little room for skips that miss the cache."),
    "attention": M("How many items the Metrics page’s Needs Attention list has for the report period."),
    "total_streams": M("Streams played since Plex Cache started counting. Only goes up, for long-term statistics."),
    "total_skips": M("Skips since Plex Cache started counting. Only goes up."),
    "total_skips_cached": M("Skips that played from cache, since Plex Cache started counting. Only goes up."),
    "total_preloaded": M("Data read by Now Playing Cache and Prefetch, since Plex Cache started counting. Only goes up."),
    # Warm-up
    "warmup_state": M("Warm-up (fills the Lead-in Cache on a schedule): off, idle (waiting for its next run), preparing, running or paused."),
    "warmup_running": M("On while a warm-up run is going (including while it’s paused)."),
    "warmup_progress": M("How far through its files the current warm-up run is."),
    "warmup_pause_reason": M("Why warm-up is paused (a player buffering, Now Playing Cache or Prefetch reading, a scrub or busy disks — SSD Cache → L2ARC Cache Settings). It carries on afterwards."),
    "warmup_next_run": M("When the next scheduled warm-up starts."),
    "warmup_last_run": M("When the last warm-up started."),
    "warmup_last_result": M("How the last warm-up ended."),
    "warmup_last_cached": M("Share of files that were already cached when the last warm-up read them. Climbs as the SSD cache fills."),
    "warmup_last_l2arc_growth": M("How much the SSD cache (L2ARC) grew during the last warm-up. ZFS fills it gradually, so this can lag a little."),
    "warmup_files_done": M("Files the current warm-up run has finished."),
    "warmup_files_total": M("Files the current warm-up run will read."),
    "warmup_read": M("Data the current warm-up run has read so far."),
    "warmup_last_files": M("Files the last warm-up run read."),
    # RAM & ZFS
    "nowplaying_ram_used": M("RAM Now Playing Cache is using for the streams playing now."),
    "nextepisode_ram_used": M("RAM Prefetch is using for the streams playing now."),
    "arc_size": M("How much RAM the ZFS cache (ARC) holds right now, for everything on the NAS."),
    "l2arc_cached": M("How much data the SSD cache (L2ARC) holds right now."),
    "nowplaying_ram_limit": M("The most RAM Now Playing Cache may use, shared by all streams: its share of the ZFS cache that’s available, after the reserve."),
    "nextepisode_ram_limit": M("The most RAM Prefetch may use, shared by all streams."),
    "zfs_cache_available": M("How much RAM the ZFS cache can use right now (up to its maximum). The two RAM limits are shares of this, after the reserve."),
    "l2arc_size": M("Total size of the SSD cache (L2ARC) devices on the media pools."),
    "arc_hit_ratio": M("Share of all reads on the NAS answered from RAM since it started (ZFS’s own count, not just Plex)."),
    # Controls
    "nowplaying_enabled": M("Now Playing Cache: once a stream is playing, read the file into RAM ahead of the viewer, so skips play from cache."),
    "run_warmup": M("Start a warm-up now, at full speed (it ignores the busy-disks pause and the cooldown)."),
    "stop_warmup": M("Stop the current warm-up. The next scheduled run starts as usual."),
    "recheck": M("Run all the health checks again."),
    "benchmark": M("Measure each dataset’s uncached read speed. Used to tell cache hits from disk reads, and to time Now Playing Cache."),
    "nowplaying_ram_percent": M("Now Playing Cache’s RAM limit, as a share of the ZFS cache available after the reserve. Shared by all streams."),
    "nextepisode_ram_percent": M("Prefetch’s RAM limit, as a share of the ZFS cache available after the reserve. The two limits together can’t pass 100%."),
    "warmup_pacing_percent": M("Share of disk time L2ARC-only caching (warm-up, and Prefetch’s L2ARC-only steps) may use, when automatic L2ARC pacing is off in the web UI. Lower is gentler on other work; “Run warm-up now” ignores it."),
    "ram_reserve_gb": M("ZFS cache kept for the rest of the NAS (TrueNAS, other apps, ZFS itself). The RAM limits are shares of what’s left."),
    "poll_seconds": M("How often Plex Cache asks Plex what’s playing. 5 seconds suits most people; 3 reacts sooner to skips and short tracks."),
}


def describe(oid: str):
    """The description for an entity, including the per-dataset and per-library ones."""
    if oid in DESCRIPTIONS:
        return DESCRIPTIONS[oid]
    if oid.startswith("dataset_") and oid.endswith("_record_size"):
        return M("This dataset’s ZFS record size, and whether its files match it. The size that suits a library depends on its bitrate: Health → Libraries shows the one recommended for each.")
    if oid.startswith("dataset_") and oid.endswith("_cache"):
        return M("This dataset’s cache settings. primarycache=all is needed for any caching; secondarycache=all lets data go on to "
                 "the SSD cache, which warm-up needs.")
    if oid.startswith("library_") and oid.endswith("_prefetch_start"):
        return M("What Prefetch puts in the Lead-in Cache once Now Playing Cache finishes: from the next episode, track or chapter "
                 "up to the whole library, each step including the ones before it. Default follows the caching mode.")
    if oid.startswith("library_") and oid.endswith("_prefetch_end"):
        return M("What Prefetch puts in the Whole File Cache near the end (80% or 90%, set in the web UI): nothing, the next "
                 "episode, track or chapter, the rest of the season, album or book, or all of it. Default follows the caching mode.")
    return ""


def control_availability(app, s: dict, wu: dict) -> dict:
    """For each control in AVAILABLE_WHEN: does it apply right now? A control that doesn't shows as
    unavailable in Home Assistant (greyed out, can't be changed) and its commands are refused."""
    mode = app.cache_mode()  # "l2arc": an L2ARC in use; "ram": RAM only
    nowplaying_ok = app.feature_available("nowplaying")[0]
    # Prefetch puts something in RAM: whole files near the end, and without an L2ARC its lead-ins too
    prefetch_ram = False
    for lib in getattr(app, "libraries", []) or []:
        kind = prefetch.library_kind(lib)
        if not kind or lib.get("excluded"):
            continue
        mine = prefetch.library_settings(s["nextepisode"], lib["key"])
        end = prefetch.resolve(mode, lib["key"], "end", mine["end"], kind)
        start = prefetch.resolve(mode, lib["key"], "start", mine["start"], kind)
        if end != "none" or (mode == "ram" and start not in ("off", "none")):
            prefetch_ram = True
            break
    l2arc = mode == "l2arc"
    manual = not s["warmup"]["pacing_auto"] or not app.l2arc_rate()
    out = {
        "nowplaying_enabled": nowplaying_ok,
        "nowplaying_ram_percent": nowplaying_ok and s["nowplaying"]["enabled"],
        "nextepisode_ram_percent": prefetch_ram,
        "warmup_pacing_auto": l2arc,
        "warmup_pacing_percent": l2arc and manual,
        "run_warmup": l2arc and not wu.get("running"),
        "stop_warmup": bool(wu.get("running")),
    }
    assert set(out) == set(AVAILABLE_WHEN)
    return out


# Controls that only apply in some conditions, and when — as the web UI shows them (dimmed, hidden
# or marked "not in use"). Every other control applies always (ALWAYS_AVAILABLE); a test makes sure
# each control is in one or the other, so a new one can't be left out.
AVAILABLE_WHEN = {
    "nowplaying_enabled": M("only when Now Playing Cache can run on this NAS"),
    "nowplaying_ram_percent": M("only while Now Playing Cache is on"),
    "nextepisode_ram_percent": M("only while Prefetch keeps something in RAM: whole files, or lead-ins without an L2ARC"),
    "warmup_pacing_auto": M("only with an L2ARC"),
    "warmup_pacing_percent": M("only with an L2ARC, while automatic pacing is off"),
    "run_warmup": M("only with an L2ARC, while warm-up isn’t running"),
    "stop_warmup": M("only while warm-up is running"),
}
ALWAYS_AVAILABLE = {"ram_reserve_gb", "poll_seconds", "recheck", "benchmark"}  # and each library's Prefetch choices


# Each control, and the setting it changes — the same settings the web UI offers, shown under
# the same conditions (tests/test_plexcache.py checks they stay in sync). Per-library Prefetch
# selects are added for each library ("library_<key>_prefetch_start" / "_end").
CONTROL_SETTINGS = {
    "nowplaying_enabled": ("nowplaying", "enabled"),
    "nowplaying_ram_percent": ("nowplaying", "ram_percent"),
    "nextepisode_ram_percent": ("nextepisode", "ram_percent"),
    "ram_reserve_gb": ("general", "ram_reserve_gb"),
    "poll_seconds": ("general", "poll_seconds"),
    "warmup_pacing_auto": ("warmup", "pacing_auto"),
    "warmup_pacing_percent": ("warmup", "pacing_percent"),  # only while automatic pacing is off, as on the page
}

# Entities dropped in an update, removed from Home Assistant if they're still there.
RETIRED = [("sensor", "arc_max"), ("sensor", "l2arc_needed"), ("select", "start_at"), ("switch", "warmup_enabled"),
           ("switch", "nextepisode_enabled"),
           # "Next episodes ready" was taken off the Metrics page
           ("sensor", "next_total"), ("sensor", "next_ready"), ("sensor", "next_ready_pct"), ("sensor", "next_lead_min"),
           ("sensor", "total_next"), ("sensor", "total_next_ready")]

# Entities are spread over a main device and three linked ones, so Home Assistant shows them
# under clear headings (each with its own page and dashboard card).
DEVICES = {
    "main": "Plex Cache",
    "activity": M("Plex Cache Metrics"),
    "warmup": M("Plex Cache Warm-up"),
    "memory": M("Plex Cache RAM & ZFS"),
}


def entities(state: dict, allow_control: bool, lang: str = "en") -> list[tuple[str, str, dict]]:
    """[(component, object_id, config), ...] — config without the shared parts. Each config
    has "_device": the key in DEVICES it belongs to (removed before publishing)."""
    E = []

    def sensor(dev, oid, name, key, unit=None, dc=None, sc=None, icon=None, cat=None, **extra):
        c = {"_device": dev, "name": name, "value_template": "{{ value_json.%s }}" % key}
        if unit:
            c["unit_of_measurement"] = unit
        if dc:
            c["device_class"] = dc
        if sc:
            c["state_class"] = sc
        if icon:
            c["icon"] = icon
        if cat:
            c["entity_category"] = cat
        c.update(extra)
        E.append(("sensor", oid, c))

    def binary(dev, oid, name, key, dc=None, icon=None, cat=None):
        c = {"_device": dev, "name": name, "value_template": "{{ value_json.%s }}" % key,
             "payload_on": "ON", "payload_off": "OFF"}
        if dc:
            c["device_class"] = dc
        if icon:
            c["icon"] = icon
        if cat:
            c["entity_category"] = cat
        E.append(("binary_sensor", oid, c))

    D = "diagnostic"
    period = state.get("report_period") or render(PERIOD_NAME["week"], lang)

    # ---- Plex Cache: is it healthy, what's playing, what's being preloaded
    sensor("main", "health", M("Health"), "health", dc="enum", options=HEALTH_STATES, icon="mdi:heart-pulse")
    sensor("main", "health_text", M("Health summary"), "health_text", icon="mdi:text-box-outline")
    binary("main", "problem", M("Problem"), "problem", dc="problem")
    sensor("main", "streams", M("Streams playing"), "streams", icon="mdi:play-network-outline", sc="measurement")
    binary("main", "streaming", M("Streaming"), "streaming", dc="running")
    sensor("main", "preloading", M("Caching ahead"), "preloading", icon="mdi:tray-arrow-down")
    binary("main", "preloading_active", M("Reading ahead"), "preloading_active", dc="running")
    # diagnostics
    sensor("main", "health_passed", M("Checks passed"), "health_passed", icon="mdi:check-circle-outline", sc="measurement", cat=D)
    sensor("main", "health_warnings", M("Health warnings"), "health_warnings", icon="mdi:alert-outline", sc="measurement", cat=D)
    sensor("main", "health_failures", M("Health failures"), "health_failures", icon="mdi:alert-circle-outline", sc="measurement", cat=D)
    sensor("main", "health_checked", M("Last health check"), "health_checked", dc="timestamp", cat=D)
    binary("main", "plex_connected", M("Plex connected"), "plex_connected", dc="connectivity", cat=D)
    binary("main", "truenas_connected", M("TrueNAS connected"), "truenas_connected", dc="connectivity", cat=D)
    binary("main", "checking", M("Health check running"), "checking", dc="running", cat=D)
    binary("main", "benchmarking", M("Disk benchmark running"), "benchmarking", dc="running", cat=D)
    sensor("main", "version", M("Version"), "version", icon="mdi:information-outline", cat=D)
    for slug, d in (state.get("datasets") or {}).items():
        sensor("main", f"dataset_{slug}_record_size", M("{name} record size", name=d["name"]), f"datasets.{slug}.record_size",
               icon="mdi:database-cog-outline", cat=D)
        sensor("main", f"dataset_{slug}_cache", M("{name} cache settings", name=d["name"]), f"datasets.{slug}.cache_settings",
               icon="mdi:database-cog-outline", cat=D)
    # The installed version, and the newest one on GitHub (when checking is on)
    # (Its own topic, <base>/update, holds just the JSON Home Assistant's update entity reads.)
    E.append(("update", "update", {"_device": "main", "name": M("Update"), "device_class": "firmware",
                                   "title": "Plex Cache", "release_url": REPO_URL, "entity_category": "diagnostic"}))

    # ---- Metrics: the Metrics page's report, plus totals that only go up
    sensor("activity", "report_verdict", M("Report ({period})", period=period), "report_verdict", icon="mdi:chart-box-outline")
    sensor("activity", "report_streams", M("Streams ({period})", period=period), "report_streams", sc="measurement", icon="mdi:play-box-multiple-outline")
    sensor("activity", "skips", M("Skips ({period})", period=period), "skips", sc="measurement", icon="mdi:skip-forward-outline")
    sensor("activity", "skips_cached", M("Skips from cache ({period})", period=period), "skips_cached", sc="measurement", icon="mdi:skip-forward")
    sensor("activity", "skips_cached_pct", M("Skips served from cache ({period})", period=period), "skips_cached_pct", unit="%", sc="measurement", icon="mdi:percent")
    sensor("activity", "lead_in_served_pct", M("Plays started from cache ({period})", period=period), "lead_in_served_pct", unit="%", sc="measurement", icon="mdi:play-circle-outline")
    sensor("activity", "fully_cached_age", M("Fully cached age ({period})", period=period), "fully_cached_age", unit="min", dc="duration", sc="measurement", icon="mdi:timer-check-outline")
    sensor("activity", "suggested_cooldown", M("Suggested cooldown"), "suggested_cooldown", unit="min", dc="duration", sc="measurement", icon="mdi:timer-cog-outline")
    sensor("activity", "cooldown_in_use", M("Cooldown in use"), "cooldown_in_use", unit="min", dc="duration", sc="measurement", icon="mdi:timer-outline", cat=D)
    for oid, name in (("library_cached_24h", M("Library cached within 24 hours")), ("library_cached_7d", M("Library cached within 7 days")),
                      ("library_cached_30d", M("Library cached within 30 days"))):
        sensor("activity", oid, name, oid, unit="%", sc="measurement", icon="mdi:bookshelf")
    sensor("activity", "still_cached_pct", M("Still cached 30 min later ({period})", period=period), "still_cached_pct", unit="%", sc="measurement", icon="mdi:timer-sand")
    sensor("activity", "preloaded", M("Data cached ahead ({period})", period=period), "preloaded", unit="GB", dc="data_size", sc="measurement")
    sensor("activity", "preloaded_already_cached_pct", M("Already in cache ({period})", period=period), "preloaded_already_cached_pct", unit="%", sc="measurement")
    sensor("activity", "disk_busy_avg", M("Disks busy during streams ({period})", period=period), "disk_busy_avg", unit="%", sc="measurement", icon="mdi:harddisk")
    sensor("activity", "disk_busy_peak", M("Disks busy peak ({period})", period=period), "disk_busy_peak", unit="%", sc="measurement", icon="mdi:harddisk-plus")
    sensor("activity", "attention", M("Needs attention ({period})", period=period), "attention", sc="measurement", icon="mdi:alert-decagram-outline")
    for oid, name in (("total_streams", M("Total streams")), ("total_skips", M("Total skips")),
                      ("total_skips_cached", M("Total skips from cache"))):
        sensor("activity", oid, name, oid, sc="total_increasing", icon="mdi:counter")
    sensor("activity", "total_preloaded", M("Total data cached ahead"), "total_preloaded", unit="GB", dc="data_size", sc="total_increasing")

    # ---- Warm-up
    sensor("warmup", "warmup_state", M("Warm-up"), "warmup_state", dc="enum", options=WARMUP_STATES, icon="mdi:fire")
    binary("warmup", "warmup_running", M("Warm-up running"), "warmup_running", dc="running")
    sensor("warmup", "warmup_progress", M("Warm-up progress"), "warmup_progress", unit="%", sc="measurement", icon="mdi:progress-clock")
    sensor("warmup", "warmup_pause_reason", M("Warm-up paused for"), "warmup_pause_reason", icon="mdi:pause-circle-outline")
    sensor("warmup", "warmup_next_run", M("Warm-up next run"), "warmup_next_run", dc="timestamp")
    sensor("warmup", "warmup_last_run", M("Warm-up last run"), "warmup_last_run", dc="timestamp")
    sensor("warmup", "warmup_last_result", M("Warm-up last result"), "warmup_last_result", icon="mdi:clipboard-check-outline")
    sensor("warmup", "warmup_last_cached", M("Warm-up already cached"), "warmup_last_cached", unit="%", icon="mdi:cached")
    sensor("warmup", "warmup_last_l2arc_growth", M("Warm-up L2ARC growth"), "warmup_last_l2arc_growth", unit="GB", dc="data_size")
    sensor("warmup", "warmup_files_done", M("Warm-up files done"), "warmup_files_done", icon="mdi:file-check-outline", cat=D)
    sensor("warmup", "warmup_files_total", M("Warm-up files total"), "warmup_files_total", icon="mdi:file-multiple-outline", cat=D)
    sensor("warmup", "warmup_read", M("Warm-up read"), "warmup_read", unit="GB", dc="data_size", icon="mdi:harddisk", cat=D)
    sensor("warmup", "warmup_last_files", M("Warm-up last run files"), "warmup_last_files", icon="mdi:file-multiple-outline", cat=D)

    # ---- RAM & ZFS: how much each feature uses, and the caches themselves
    for oid, name, cat in (("nowplaying_ram_used", M("Now Playing Cache RAM in use"), None),
                           ("nextepisode_ram_used", M("Prefetch RAM in use"), None),
                           ("arc_size", M("ARC size"), None),
                           ("l2arc_cached", M("L2ARC filled"), None),
                           ("nowplaying_ram_limit", M("Now Playing Cache RAM limit"), D),
                           ("nextepisode_ram_limit", M("Prefetch RAM limit"), D),
                           ("zfs_cache_available", M("ZFS cache available"), D),
                           ("l2arc_size", M("L2ARC size"), D)):
        sensor("memory", oid, name, oid, unit="GB", dc="data_size", sc="measurement", icon="mdi:memory", cat=cat)
    sensor("memory", "arc_hit_ratio", M("ARC hit ratio"), "arc_hit_ratio", unit="%", sc="measurement", icon="mdi:bullseye-arrow")

    # ---- Controls (on the main device, so they're all in one place)
    if allow_control:
        for feat, name in (("nowplaying", M("Now Playing Cache")),):
            E.append(("switch", f"{feat}_enabled", {"_device": "main", "name": name,
                                                     "value_template": "{{ value_json.%s_enabled }}" % feat,
                                                     "state_on": "ON", "state_off": "OFF", "payload_on": "ON", "payload_off": "OFF",
                                                     "icon": "mdi:cached"}))
        E.append(("switch", "warmup_pacing_auto", {"_device": "main", "name": M("L2ARC pacing: automatic"),
                                                   "value_template": "{{ value_json.warmup_pacing_auto }}",
                                                   "state_on": "ON", "state_off": "OFF", "payload_on": "ON", "payload_off": "OFF",
                                                   "entity_category": "config", "icon": "mdi:speedometer-medium"}))
        for oid, name, icon in (("run_warmup", M("Run warm-up now"), "mdi:play"), ("stop_warmup", M("Stop warm-up"), "mdi:stop"),
                                ("recheck", M("Re-run health checks"), "mdi:refresh"), ("benchmark", M("Benchmark disks"), "mdi:speedometer")):
            E.append(("button", oid, {"_device": "main", "name": name, "payload_press": "PRESS", "icon": icon}))
        # Boxes, not sliders: Home Assistant's device page shows a slider without its value or unit.
        numbers = [("nowplaying_ram_percent", M("Now Playing Cache RAM"), 5, 90, 5, "%"),
                   ("nextepisode_ram_percent", M("Prefetch RAM"), 5, 90, 5, "%"),
                   ("ram_reserve_gb", M("Reserve ZFS cache"), 0, 4096, 1, "GB")]
        numbers.insert(2, ("warmup_pacing_percent", M("L2ARC pacing"), 5, 100, 5, "%"))  # (unavailable while automatic)
        for oid, name, lo, hi, step, unit in numbers:
            E.append(("number", oid, {"_device": "main", "name": name, "value_template": "{{ value_json.%s }}" % oid,
                                      "min": lo, "max": hi, "step": step, "unit_of_measurement": unit,
                                      "mode": "box", "entity_category": "config", "icon": "mdi:tune-variant"}))
        E.append(("select", "poll_seconds", {"_device": "main", "name": M("Check what’s playing every"),
                                             "value_template": "{{ value_json.poll_seconds }}",
                                             "options": list(POLL.values()), "entity_category": "config"}))
        for key, lib in (state.get("libraries") or {}).items():
            for field, name, icon in (("start", M("{name} Lead-in Cache prefetch", name=lib["name"]), "mdi:playlist-play"),
                                      ("end", M("{name} Whole File Cache prefetch", name=lib["name"]), "mdi:playlist-check")):
                E.append(("select", f"library_{key}_prefetch_{field}", {
                    "_device": "main", "name": name, "options": list(prefetch_choices(lib["kind"], field).values()),
                    "value_template": "{{ value_json.libraries['%s'].prefetch_%s }}" % (key, field),
                    "entity_category": "config", "icon": icon}))
    return [(component, oid, localize(conf, lang)) for component, oid, conf in E]


def mqtt_client_id(node: str) -> str:
    """The MQTT client ID: letters and digits only, 1 to 23 of them — what every broker must accept
    [MQTT-3.1.3-5]. (``node``, with its underscore, stays the name Home Assistant knows the entities by.)"""
    return ("".join(ch for ch in node if ch.isascii() and ch.isalnum()) or "plexcache")[:23]


# ------------------------------------------------------------------- bridge
class HomeAssistantBridge:
    """Keeps an MQTT connection while it's enabled, publishing status and taking commands."""

    def __init__(self, app):
        self.app = app
        self.stop = threading.Event()
        self.wake = threading.Event()  # set to publish straight away (e.g. after a command)
        self.retry_now = threading.Event()  # "Reconnect now" in System
        self.status = {"state": "off", "error": None, "since": None, "entities": 0, "last_publish": None,
                       "next_retry": None, "ha_seen": None, "steps": self._steps("off")}
        self._client: MQTTClient | None = None
        self._published: set[str] = set()

    @staticmethod
    def _steps(state: str) -> dict:
        return {k: {"status": state, "text": ""} for k in STEPS}

    def _step(self, name: str, status: str, text="") -> None:
        self.status["steps"][name] = {"status": status, "text": text}

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="mqtt", daemon=True)
        self.thread.start()

    def close(self, timeout: float = 8) -> None:
        """Stop, telling Home Assistant we're going (it would notice anyway, from the last will)."""
        self.stop.set()
        self.retry_now.set()
        t = getattr(self, "thread", None)
        if t:
            t.join(timeout)

    def retry(self) -> None:
        """Try to connect again now, instead of waiting for the next attempt."""
        self.retry_now.set()

    @staticmethod
    def _sig(cfg) -> str:
        return json.dumps(cfg, sort_keys=True)

    def _sleep(self, seconds: float, cfg) -> None:
        """Wait before the next attempt, but go straight away for "Reconnect now",
        a change to the MQTT settings, or shutting down."""
        sig = self._sig(cfg)
        end = time.monotonic() + seconds
        while not self.stop.is_set() and time.monotonic() < end:
            if self.retry_now.wait(min(1.0, max(0.01, end - time.monotonic()))):
                break
            if self._sig(self.app.settings.section("mqtt")) != sig:
                break
        self.retry_now.clear()
        self.status["next_retry"] = None

    # --------------------------------------------------------------- loop
    def _loop(self):
        backoff = RETRY_FIRST
        last_error = None
        while not self.stop.is_set():
            cfg = self.app.settings.section("mqtt")
            if not cfg["enabled"] or not cfg["host"]:
                self._disconnect()
                if not cfg["enabled"]:
                    self.status.update(state="off", error=None, steps=self._steps("off"), next_retry=None)
                else:
                    steps = self._steps("off")
                    steps["broker"] = {"status": "fail", "text": M("No broker address set.")}
                    self.status.update(steps=steps)  # the lights first, then the state that refers to them
                    self.status.update(state="error", error=M("No broker address set."))
                last_error, backoff = None, RETRY_FIRST
                self._sleep(3600, cfg)
                continue
            try:
                self._run(cfg)
                backoff, last_error = RETRY_FIRST, None
            except (MQTTError, OSError) as e:
                self._disconnect()
                step = getattr(e, "step", "session")
                why = msg(e)
                # The lights first, then the state that refers to them (the page may read in between)
                if step in STEPS:
                    self._step(step, "fail", why)
                    for later in STEPS[STEPS.index(step) + 1:]:
                        self._step(later, "off", "")
                else:  # dropped after being connected
                    self._step("publish", "fail", why)
                self.status.update(state="error", error=why, since=time.time())
                if why != last_error:  # don't fill the log with the same failure every retry
                    self.app.activity.event("mqtt", M("MQTT: {why}", why=why), "warn")
                    last_error = why
                self.status["next_retry"] = time.time() + backoff
                self._sleep(backoff, cfg)
                backoff = min(RETRY_MAX, backoff * 2)
        self._disconnect()

    def _topics(self, cfg):
        base = cfg["base_topic"]
        return base, f"{base}/availability", f"{base}/state", f"{base}/set"

    def _run(self, cfg):
        base, avail, state_topic, set_topic = self._topics(cfg)
        node = "plexcache_" + (self.app.settings.section("general")["client_id"] or "x").replace("-", "")[:12]
        c = MQTTClient(cfg["host"], cfg["port"], client_id=mqtt_client_id(node), username=cfg["username"], password=cfg["password"],
                       tls=cfg["tls"], verify_tls=cfg["verify_tls"], will=(avail, "offline", True))
        self.status.update(state="connecting", error=None, steps=self._steps("testing"))
        if not cfg["tls"]:
            self._step("tls", "off", M("Not used — the connection isn’t encrypted."))
        c.open()
        self._step("broker", "ok", f"{cfg['host']}:{cfg['port']}")
        if cfg["tls"]:
            c.start_tls()
            self._step("tls", "ok", M("Encrypted") if cfg["verify_tls"] else M("Encrypted (certificate not checked)"))
        c.login()
        self._step("login", "ok", M("Signed in as {user}", user=cfg["username"]) if cfg["username"] else M("Connected without a login"))
        self._client = c
        self.status.update(state="connected", error=None, since=time.time())
        self.app.activity.event("mqtt", M("MQTT: connected to {host}:{port}", host=cfg["host"], port=str(cfg["port"])))
        c.publish(avail, "online", retain=True)
        if cfg["allow_control"]:
            c.subscribe([f"{set_topic}/#"])
        if cfg["discovery"]:
            # Home Assistant re-reads discovery when it restarts; it announces that here.
            c.subscribe([f"{cfg['discovery_prefix']}/status"])
        else:
            self.status["entities"] = 0
        snapshot = json.dumps(cfg, sort_keys=True)
        next_pub, discovery_sig = 0.0, None
        while not self.stop.is_set():
            now_cfg = self.app.settings.section("mqtt")
            if json.dumps(now_cfg, sort_keys=True) != snapshot:
                # Settings changed: drop the entities, reconnect with the new ones.
                self._clear_discovery(c, cfg)
                self._disconnect()
                return
            if time.monotonic() >= next_pub or self.wake.is_set():
                self.wake.clear()
                state = build_state(self.app, cfg["report_period"], cfg.get("language", "en"))
                sig = json.dumps([sorted(state["datasets"]), sorted(state["libraries"]), state["report_period"], self._web_url()])
                if cfg["discovery"] and sig != discovery_sig:
                    self._publish_discovery(c, cfg, node, state)
                    discovery_sig = sig
                c.publish(state_topic, json.dumps(state, default=str), retain=True)
                c.publish(f"{base}/update", json.dumps(state["update"]), retain=True)
                self.status["last_publish"] = time.time()
                self._step("publish", "ok", M("{topic} · {n} Home Assistant entities", topic=state_topic, n=self.status["entities"])
                           if cfg["discovery"] else state_topic)
                next_pub = time.monotonic() + cfg["interval"]
            if self.retry_now.is_set():  # "Reconnect now" while connected: start afresh
                self.retry_now.clear()
                self._disconnect()
                return
            for topic, payload in c.poll(1.0):
                if topic == f"{cfg['discovery_prefix']}/status" and payload == b"online":
                    self.status["ha_seen"] = time.time()
                    discovery_sig = None  # Home Assistant restarted: announce everything again
                    self.wake.set()
                elif cfg["allow_control"] and topic_matches(f"{set_topic}/#", topic):
                    self._command(topic[len(set_topic) + 1:], payload.decode("utf-8", "replace").strip())
                    self.wake.set()
            c.ping_if_due()

    def _disconnect(self):
        c, self._client = self._client, None
        if c:
            try:
                c.publish(c.will[0], "offline", retain=True)
            except (MQTTError, OSError):
                pass
            c.close()

    # ---------------------------------------------------------- discovery
    def _devices(self, node: str, lang: str = "en") -> dict:
        main = {"identifiers": [node], "name": DEVICES["main"], "manufacturer": "Plex Cache",
                "model": "Plex Cache for TrueNAS", "sw_version": self.app.version}
        url = self._web_url()
        if url:
            main["configuration_url"] = url  # Home Assistant's "Visit" link
        out = {"main": main}
        for key, name in DEVICES.items():
            if key != "main":
                out[key] = {"identifiers": [f"{node}_{key}"], "name": render(name, lang), "manufacturer": "Plex Cache",
                            "model": "Plex Cache for TrueNAS", "sw_version": self.app.version, "via_device": node}
                if url:
                    out[key]["configuration_url"] = url
        return out

    def _publish_discovery(self, c, cfg, node, state):
        base, avail, state_topic, set_topic = self._topics(cfg)
        lang = cfg.get("language", "en")
        devices = self._devices(node, lang)
        topics = set()
        ents = entities(state, cfg["allow_control"], lang)
        # Which software sent these: Home Assistant shows it, and asks every discovery message for it
        origin = {"name": "Plex Cache", "sw_version": self.app.version, "support_url": REPO_URL}
        # Every entity's description, in one retained message (sent with discovery, not with each update)
        c.publish(f"{base}/info", json.dumps({oid: render(describe(oid), lang) for _, oid, _ in ents}), retain=True)
        for component, oid, conf in ents:
            if self.stop.is_set() or self.retry_now.is_set():
                return  # shutting down or reconnecting: the rest is sent next time
            conf = dict(conf)
            conf = dict(conf, unique_id=f"{node}_{oid}", device=devices[conf.pop("_device")], origin=origin)
            if oid in AVAILABLE_WHEN:
                # available while Plex Cache is online AND the control applies now (from the state message)
                conf["availability"] = [{"topic": avail},
                                        {"topic": state_topic, "value_template": "{{ 'online' if value_json.available.%s else 'offline' }}" % oid}]
                conf["availability_mode"] = "all"
            else:
                conf["availability_topic"] = avail
            # The entity ID Home Assistant suggests (object_id no longer sets it: default_entity_id does)
            conf["default_entity_id"] = f"{component}.plex_cache_{oid}"
            if component == "update":
                conf["state_topic"] = f"{base}/update"
            elif component != "button":
                conf["state_topic"] = state_topic
            if component in ("switch", "button", "number", "select"):
                conf["command_topic"] = f"{set_topic}/{oid}"
            # Its description, shown under Attributes when the entity is opened in Home Assistant
            conf["json_attributes_topic"] = f"{base}/info"
            conf["json_attributes_template"] = "{{ {'description': value_json['%s']} | tojson }}" % oid
            topic = f"{cfg['discovery_prefix']}/{component}/{node}/{oid}/config"
            c.publish(topic, json.dumps(conf), retain=True)
            topics.add(topic)
        # Entities that no longer exist (a removed library, or one dropped in an update — remembered
        # across restarts, since the broker keeps them until told otherwise)
        retired = {f"{cfg['discovery_prefix']}/{comp}/{node}/{oid}/config" for comp, oid in RETIRED}
        for gone in (self._published | self._load_published() | retired) - topics:
            c.publish(gone, "", retain=True)
        self._published = topics
        self._save_published(topics)
        self.status["entities"] = len(ents)

    def _load_published(self) -> set:
        return set((self.app._load_json("mqtt-entities.json") or {}).get("topics") or [])

    def _save_published(self, topics: set) -> None:
        self.app._save_json("mqtt-entities.json", {"topics": sorted(topics)})

    def _web_url(self) -> str | None:
        """The web UI's address: PLEXCACHE_URL if set (e.g. behind a reverse proxy), otherwise
        the address and port a browser last used to open it."""
        return os.environ.get("PLEXCACHE_URL", "").strip() or self.app.web_url

    def _clear_discovery(self, c, cfg):
        for t in self._published | self._load_published():
            try:
                c.publish(t, "", retain=True)
            except (MQTTError, OSError):
                break
        self._published = set()
        self._save_published(set())

    # ------------------------------------------------------------ commands
    def _command(self, name: str, value: str):
        app = self.app
        lang = app.settings.section("mqtt").get("language", "en")
        try:
            if name in AVAILABLE_WHEN:
                state = build_state(app, "week", lang)
                if not state["available"].get(name):
                    raise SettingsError(M("it applies {when}", when=AVAILABLE_WHEN[name]))
            if name == "warmup_pacing_auto":
                if value.upper() not in ("ON", "OFF", "TRUE", "FALSE", "1", "0"):
                    raise SettingsError(M("send ON or OFF"))
                app.settings.update({"warmup": {"pacing_auto": value.upper() in ("ON", "TRUE", "1")}})
            elif name == "nowplaying_enabled":
                if value.upper() not in ("ON", "OFF", "TRUE", "FALSE", "1", "0"):
                    raise SettingsError(M("send ON or OFF"))
                feat, on = name[:-8], value.upper() in ("ON", "TRUE", "1")
                if on:
                    ok, why = app.feature_available(feat)
                    if not ok:
                        raise SettingsError(why)
                app.settings.update({feat: {"enabled": on}})
            elif name == "run_warmup":
                app.start_warmup(manual=True)
            elif name == "stop_warmup":
                app.stop_warmup()
            elif name == "recheck":
                app.run_checks_async(rescan=True)
            elif name == "benchmark":
                app.run_benchmark()
            elif name in ("nowplaying_ram_percent", "nextepisode_ram_percent", "warmup_pacing_percent", "ram_reserve_gb"):
                sec = {"nowplaying_ram_percent": "nowplaying", "nextepisode_ram_percent": "nextepisode",
                       "warmup_pacing_percent": "warmup", "ram_reserve_gb": "general"}[name]
                key = name.replace("nowplaying_", "").replace("nextepisode_", "").replace("warmup_", "")
                app.settings.update({sec: {key: float(value)}})
            elif name == "poll_seconds":
                app.settings.update({"general": {"poll_seconds": int(_choice(POLL, value, lang))}})
            elif name.startswith("library_") and (name.endswith("_prefetch_start") or name.endswith("_prefetch_end")):
                field = "start" if name.endswith("_start") else "end"
                key = name[len("library_"):-len("_prefetch_" + field)]
                kind = prefetch.library_kind(app.library_info(key))
                if kind is None:
                    raise SettingsError(M("Plex has no such library"))
                libs = dict(app.settings.section("nextepisode").get("libraries") or {})
                libs[key] = dict(libs.get(key) or {}, **{field: _choice(prefetch_choices(kind, field), value, lang)})
                app.check_prefetch_libraries(libs)  # the same check as the web UI's
                app.settings.update({"nextepisode": {"libraries": libs}})
            else:
                raise SettingsError(M("unknown command {name}", name=name))
            app.activity.event("mqtt", M("MQTT command: {name} → {value}", name=name, value=value))
        except (SettingsError, ValueError, StopIteration) as e:
            why = msg(e) if str(e) else M("not a valid choice")
            app.activity.event("mqtt", M("MQTT command: couldn’t do {name} ({value}): {why}", name=name, value=value, why=why), "warn")

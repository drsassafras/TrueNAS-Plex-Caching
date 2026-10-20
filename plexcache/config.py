"""Settings storage.

Settings live in one JSON file in the config folder (``/config`` in the
container), so they survive restarts and app updates no matter how the
process is started.  Secrets are never sent back to the browser; the UI gets
a placeholder instead and sending the placeholder back keeps the stored value.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import urllib.parse
import threading
import uuid

from .i18n import CODES, M
from .optimize.convert import DEFAULT_TYPES as CONVERT_DEFAULTS

SECRET_MASK = "__unchanged__"
SECRET_PATHS = (("plex", "token"), ("truenas", "api_key"), ("mqtt", "password"))

DEFAULTS: dict = {
    "plex": {"url": "", "token": ""},
    # A second Plex server with the same files (another site, synced): watched so a converted file's
    # original goes only once both have its MKV. "account_token": from signing in (lists the
    # account's servers); "token": the chosen server's own; "libraries": this server's library key ->
    # the other's ("" for none; a library not set is paired with the one of the same name and kind).
    "plex2": {"url": "", "token": "", "account_token": "", "name": "", "machine_id": "", "libraries": {}},
    "truenas": {"url": "", "api_key": "", "verify_tls": False},
    "general": {
        "poll_seconds": 5,
        "update_check": True,  # ask GitHub every few hours whether there's a newer version
        "hdd_mb_per_sec": 150,
        "hdd_measured_at": None,
        # Measured uncached read speed per dataset (or mount, when it isn't ZFS):
        # {"tank/media": {"mb_s": 210, "measured_at": "2026-10-13T12:47:29"}}.
        # hdd_mb_per_sec is used for anything not measured yet.
        "disk_speeds": {},
        # RAM kept out of reach of Now Playing Cache and Prefetch, for TrueNAS,
        # other apps and ZFS itself. Their limits are shares of what's left.
        "ram_reserve_gb": 4,
        # Cache log (cachelog.py): hours before a lead-in or a whole file is cached again. RAM-only
        # mode uses the RAM pair (Caching page); with an L2ARC, everything read is taken to be on
        # the SSD and the L2ARC pair applies (SSD Cache page).
        "ram_cooldown_lead_in_minutes": 120,
        "ram_cooldown_whole_minutes": 180,
        "l2arc_cooldown_lead_in_minutes": 1440,
        "l2arc_cooldown_whole_minutes": 360,
        # "I won't use an L2ARC" (SSD Cache page): RAM-only mode, and no Health warning about a missing L2ARC.
        "no_l2arc": False,
        # Only answer devices on the local network (and VPNs into it): nothing from the internet, signed
        # in or not (server.py, local_address()). Behind a reverse proxy, the proxy's address is what counts.
        "lan_only": True,
        "debug": False,  # unused: logging detail comes from PLEXCACHE_DEBUG (kept so older exports still import)
        "excluded_libraries": [],
        # Optional rewrites when Plex sees media at a different path than
        # this app does: [{"plex": "/data", "local": "/media"}]
        "path_map": [],
        "client_id": "",
    },
    "warmup": {
        "time": "03:00",   # when timed warm-ups start
        "weekday": 6,      # weekly: 0 = Monday (twice weekly adds 3 days later); twice monthly is the 1st and 15th
        # L2ARC pacing: automatic reads from the disks just under how fast ZFS copies to the SSD
        # (l2arc_write_max), so nothing is read only to leave RAM before reaching it; otherwise
        # pacing_percent, a share of disk time.
        "pacing_auto": True,
        "pacing_percent": 10,
        # Per library (Plex library key -> choices; anything not set is its default):
        # {"schedule": "default" | "none" | "boot" | "twice_monthly" | "weekly" | "twice_weekly" | "daily",
        #  "seconds": n} — every schedule but "none" also runs at boot
        # "seconds" is each lead-in's length (warm-up, and Prefetch's lead-ins); without it the
        # library's kind decides (warmup.KIND_SECONDS: 60 s for movies, 30 for TV, 10 for audio).
        "libraries": {},
        "reserve_gb": 100,
        "padding_video": 5,
        "padding_audio": 15,
        # "pacing_percent" above and these are the L2ARC Cache Settings (SSD Cache page): they
        # apply to all caching that needs an L2ARC — warm-up, and Prefetch's L2ARC-only steps.
        # When to pause (it carries on from where it was afterwards).
        "pause_buffering": True,   # a player reports buffering (and for a few minutes after)
        "pause_preload": True,     # Now Playing Cache or Prefetch's other steps are reading
        "pause_scrub": True,       # a scrub or resilver is running on a media pool
        "pause_disk_busy": 50,     # pool disks this % busy with other work (0 = off)
    },
    "nowplaying": {
        "enabled": False,
        # Reads the whole file if it fits in the limit, otherwise as much as fits,
        # from the play position forward.
        "ram_percent": 60,  # share of the usable RAM cache (all streams together)
        "delay_sec": 0,  # 0 = automatic: after Plex's start-up buffer
        "buffer_direct_sec": 10,  # playback Plex buffers before we start (direct play)
        "buffer_transcode_sec": 60,  # ... and when transcoding
        "min_delay_sec": 5,
        "max_streams": 2,
        "pacing_percent": 50,
    },
    "nextepisode": {  # Prefetch (see prefetch.py); no switch: each library's choices say what it does
        "ram_percent": 20,  # share of the usable RAM cache
        "pacing_percent": 50,
        # Per library (Plex library key -> choices); anything not set is "default" (see prefetch.py):
        # {"start": after Now Playing Cache, the start of files; "end": near the end, whole files;
        #  "end_at": "80" | "90"}
        "libraries": {},
    },
    # The optimizer (the second container): rewrites files so they're laid out for caching
    # (attachments, index and tags after the video), with the dataset's record size and compression.
    # Per library (Plex library key -> choices; see optimizing.py): {"sync": true — write files again
    # so they take the dataset's record size and compression, "mkv": false — lay MKV files out for
    # caching, "images": "unmatched" | "remove" | "compress" | "plex" — what an optimized MKV's cover
    # art becomes; "unmatched": Plex's poster only for items Plex hasn't matched, "tags": "keep" |
    # "excessive" | "strip", "convert": false — convert other containers to MKV}.
    # "convert_types": which containers are converted (optimize/convert.py TYPES), in every library
    # that converts; "convert_hold_days": how long an original is kept once Plex has its MKV.
    "optimizer": {
        "libraries": {},
        "convert_types": dict(CONVERT_DEFAULTS),
        "convert_hold_days": 7,
    },
    # MQTT (off until switched on): status on <base_topic>/state, commands on <base_topic>/set/<name>.
    # With "discovery", Home Assistant (and openHAB, Domoticz...) create the entities themselves.
    "mqtt": {
        "enabled": False,
        "host": "",
        "port": 1883,
        "username": "",
        "password": "",
        "tls": False,
        "verify_tls": False,
        "discovery": True,  # Home Assistant MQTT discovery messages
        "discovery_prefix": "homeassistant",
        "base_topic": "plexcache",
        "interval": 30,  # seconds between status updates
        "report_period": "week",  # what the "last N days" sensors cover: today | week | month
        "allow_control": True,  # accept commands (switches, buttons, numbers and selects in Home Assistant)
        "language": "en",  # names and text sent to Home Assistant (it doesn't say which language it uses)
    },
}

WARMUP_SCHEDULES = ("default", "none", "boot", "twice_monthly", "weekly", "twice_weekly", "daily")
SECONDS_LIMITS = {"movie": (0, 180), "tv": (0, 180), "audio": (0, 60)}  # per library, by its kind; 0: one record
# Before 0.15 warm-up had one schedule for every library; what it becomes.
OLD_SCHEDULE = {"daily": "daily", "weekly": "weekly", "hours": "daily", "manual": "none"}
OLD_LIBRARY_SCHEDULE = {"monthly": "twice_monthly"}  # 0.15 betas
PREFETCH_FIELDS = {"start": ("default", "off", "next", "rest", "whole", "next_group", "all_groups", "library"),
                   "end": ("default", "none", "next", "rest"),
                   "end_at": ("80", "90")}
# Before 0.15 each library had one choice ("library_modes"); what each becomes.
OLD_MODES = {"off": {"start": "off", "end": "none"}, "rest": {"end": "rest"}, "album": {"end": "rest"},
             "artist": {"start": "all_groups"}}

# What each setting accepts. Every change — from the web UI, the API or MQTT — goes
# through _coerce(), so these are the only rules. Numbers are whole numbers in the same
# ranges the web UI offers; dropdowns accept only their own choices.
LIMITS = {
    ("general", "hdd_mb_per_sec"): (10, 5000),
    ("general", "ram_reserve_gb"): (0, 4096),
    ("warmup", "weekday"): (0, 6),
    ("warmup", "pacing_percent"): (5, 100),
    ("warmup", "reserve_gb"): (0, 500),
    ("warmup", "padding_video"): (0, 100),
    ("warmup", "padding_audio"): (0, 100),
    ("nowplaying", "ram_percent"): (5, 90),
    ("nowplaying", "buffer_direct_sec"): (0, 600),
    ("nowplaying", "buffer_transcode_sec"): (0, 600),
    ("nowplaying", "min_delay_sec"): (0, 600),
    ("nowplaying", "max_streams"): (1, 20),
    ("nowplaying", "pacing_percent"): (5, 100),
    ("nextepisode", "ram_percent"): (5, 90),
    ("nextepisode", "pacing_percent"): (5, 100),
    ("mqtt", "port"): (1, 65535),
    ("mqtt", "interval"): (5, 600),
    ("optimizer", "convert_hold_days"): (0, 90),
}
AUTO = -1  # a cooldown worked out from what's been measured (cachelog.CacheLog.auto_cooldown)
COOLDOWN_RAM = (AUTO, 0, 10, 30, 60, 120, 180, 240, 360, 720)                    # minutes
COOLDOWN_L2ARC = (AUTO, 0, 10, 60, 180, 360, 720, 1440, 2880, 4320, 10080)
CHOICES = {
    ("general", "poll_seconds"): (3, 5, 10, 30),
    # 0 and 10 minutes: next to no cooldown, to collect re-reads for "How Long Cached Data Lasts"
    ("general", "ram_cooldown_lead_in_minutes"): COOLDOWN_RAM,
    ("general", "ram_cooldown_whole_minutes"): COOLDOWN_RAM,
    ("general", "l2arc_cooldown_lead_in_minutes"): COOLDOWN_L2ARC,
    ("general", "l2arc_cooldown_whole_minutes"): COOLDOWN_L2ARC,
    ("warmup", "pause_disk_busy"): (0, 40, 50, 60),
    ("nowplaying", "delay_sec"): (0, 15, 30, 60),
    ("mqtt", "report_period"): ("today", "week", "month"),
    ("mqtt", "language"): tuple(CODES),
}
def public_defaults() -> dict:
    """Every setting's default that's a plain value (no lists or maps), but secrets: for the
    page's ↺ "back to default" buttons."""
    return {sec: {k: v for k, v in vals.items() if isinstance(v, (str, int, float)) and not SECRET_KEY(k)}
            for sec, vals in DEFAULTS.items() if isinstance(vals, dict)}


def SECRET_KEY(key: str) -> bool:  # noqa: N802 (a predicate used like a constant)
    return any(w in key for w in ("token", "password", "api_key", "secret"))


MAX_ITEMS = 500  # entries in a list/map setting (libraries, mappings, datasets)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?")
_LIB_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HOST = re.compile(r"[A-Za-z0-9._:\[\]-]{1,253}")


class SettingsError(ValueError):
    pass


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


PLEX_DEFAULT_PORT = 32400


def normalize_url(section: str, value: str) -> str:
    """Fill in what people usually leave out.

    Plex: ``192.168.1.10`` -> ``http://192.168.1.10:32400`` (port added for http only).
    TrueNAS: ``192.168.1.10`` -> ``https://192.168.1.10`` (API keys need https).
    """
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = ("https://" if section == "truenas" else "http://") + value
    if section in ("plex", "plex2") and value.lower().startswith("http://"):
        # (an https:// address is usually a reverse proxy on 443, so leave it alone)
        scheme, rest = value.split("://", 1)
        hostport = rest.split("/", 1)[0]
        has_port = re.search(r":\d+$", hostport) and not hostport.endswith("]")
        if not has_port:
            value = f"{scheme}://{hostport}:{PLEX_DEFAULT_PORT}" + (rest[len(hostport):] if "/" in rest else "")
    return value


def _text(section: str, key: str, value, max_len: int = 255, spaces: bool = True) -> str:
    """A string: no control characters, a sensible length, optionally no spaces."""
    if not isinstance(value, str):
        raise SettingsError(M("{setting} must be text", setting=f"{section}.{key}"))
    if len(value) > max_len or _CONTROL.search(value) or (not spaces and any(c.isspace() for c in value)):
        raise SettingsError(M("{setting} isn’t valid", setting=f"{section}.{key}"))
    return value


def _path(section: str, key: str, value) -> str:
    """A folder path: absolute, no '..' parts."""
    value = _text(section, key, value, 1024).rstrip("/") or "/"
    if not value.startswith("/") or ".." in value.split("/"):
        raise SettingsError(M("{setting}: folder paths must start with / (and can’t use ..)", setting=f"{section}.{key}"))
    return value


def _url(section: str, value) -> str:
    value = normalize_url(section, _text(section, "url", value, 255, spaces=False))
    if not value:
        return ""
    u = urllib.parse.urlsplit(value)
    if u.scheme not in ("http", "https") or not u.hostname or "@" in u.netloc or u.query or u.fragment:
        raise SettingsError(M("{setting} must be an address like 192.168.1.10 or http://nas.local:32400", setting=f"{section}.url"))
    try:
        u.port  # raises on a bad port
    except ValueError:
        raise SettingsError(M("{setting} has a bad port", setting=f"{section}.url")) from None
    return value


def _coerce(section: str, key: str, value, current):
    """Check an incoming value strictly against what the setting accepts, and return it."""
    where = f"{section}.{key}"
    if (section, key) in CHOICES:
        choices = CHOICES[(section, key)]
        if isinstance(choices[0], int):
            # Numbers may arrive as 10 or "10" (a form field); nothing else
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).removeprefix("-").isdigit():
                raise SettingsError(M("{setting} must be one of {choices}", setting=where, choices=", ".join(map(str, choices))))
            value = int(value)
        if value not in choices:
            raise SettingsError(M("{setting} must be one of {choices}", setting=where, choices=", ".join(map(str, choices))))
        return value
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise SettingsError(M("{setting} must be true or false", setting=where))
        return value
    if isinstance(current, int):
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or value != int(value)):
            raise SettingsError(M("{setting} must be a whole number", setting=where))
        lo, hi = LIMITS[(section, key)]
        if not lo <= value <= hi:
            raise SettingsError(M("{setting} must be between {min} and {max}", setting=where, min=str(lo), max=str(hi)))
        return int(value)
    if key == "time":
        if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise SettingsError(M("time must look like 03:00"))
        return value
    if key == "url":
        return _url(section, value)
    if (section, key) in (("plex", "token"), ("truenas", "api_key"), ("plex2", "token"), ("plex2", "account_token")):
        return _text(section, key, value, 255, spaces=False)
    if (section, key) in (("plex2", "name"), ("plex2", "machine_id")):
        return _text(section, key, value, 200)
    if (section, key) == ("plex2", "libraries"):
        if not isinstance(value, dict) or len(value) > MAX_ITEMS or any(
                not _LIB_KEY.fullmatch(str(k)) or not (v == "" or (isinstance(v, str) and _LIB_KEY.fullmatch(v))) for k, v in value.items()):
            raise SettingsError(M("plex2.libraries must pair this server’s library keys with the other server’s (or \"\")"))
        return {str(k): v for k, v in value.items()}
    if (section, key) == ("mqtt", "host"):
        value = _text(section, key, value, 253).strip()
        if value and not _HOST.fullmatch(value):
            raise SettingsError(M("mqtt.host must be an IP address or host name"))
        return value
    if (section, key) == ("mqtt", "username"):
        return _text(section, key, value, 128)
    if (section, key) == ("mqtt", "password"):
        return _text(section, key, value, 256)
    if key == "hdd_measured_at":
        if value is not None and not (isinstance(value, str) and _ISO_TIME.fullmatch(value)):
            raise SettingsError(M("general.hdd_measured_at must be a date and time"))
        return value
    if key == "excluded_libraries":
        if not isinstance(value, list) or len(value) > MAX_ITEMS:
            raise SettingsError(M("excluded_libraries must be a list"))
        return [_text(section, key, x, 200) for x in value]
    if key in ("base_topic", "discovery_prefix"):
        value = _text(section, key, value, 100).strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*", value):
            raise SettingsError(M("{setting} may only use letters, numbers, - _ and /", setting=key))
        return value
    if (section, key) == ("warmup", "libraries"):
        if not isinstance(value, dict) or len(value) > MAX_ITEMS:
            raise SettingsError(M("warmup.libraries must be an object"))
        out = {}
        for lib, choice in value.items():
            if not (_LIB_KEY.fullmatch(str(lib)) or lib == "*"):
                raise SettingsError(M("unknown library: {key}", key=str(lib)))
            if choice is None:
                continue
            if not isinstance(choice, dict) or any(k not in ("schedule", "seconds") for k in choice):
                raise SettingsError(M("each library’s warm-up choices must be an object"))
            if "schedule" in choice and choice["schedule"] not in WARMUP_SCHEDULES:
                raise SettingsError(M("unknown warm-up schedule: {mode}", mode=str(choice["schedule"])))
            secs = choice.get("seconds")
            if secs is not None and (isinstance(secs, bool) or not isinstance(secs, (int, float)) or not math.isfinite(secs)
                                     or secs != int(secs) or not 0 <= secs <= 180):
                raise SettingsError(M("{setting} must be between {min} and {max}", setting="seconds", min="0", max="180"))
            row = {k: v for k, v in choice.items() if not (k == "schedule" and v == "default") and v is not None}
            if "seconds" in row:
                row["seconds"] = int(row["seconds"])
            if row:
                out[str(lib)] = row
        return out
    if (section, key) == ("optimizer", "libraries"):
        if not isinstance(value, dict) or len(value) > MAX_ITEMS:
            raise SettingsError(M("optimizer.libraries must be an object"))
        out = {}
        for lib, choice in value.items():
            if not _LIB_KEY.fullmatch(str(lib)):
                raise SettingsError(M("unknown library: {key}", key=str(lib)))
            if choice is None:
                continue
            if isinstance(choice, dict) and choice.get("images") == "keep":
                choice = dict(choice, images="compress")  # the name before the three choices
            if (not isinstance(choice, dict) or any(k not in ("sync", "mkv", "images", "tags", "convert") for k in choice)
                    or choice.get("images", "unmatched") not in ("unmatched", "compress", "remove", "plex")
                    or choice.get("tags", "keep") not in ("keep", "excessive", "strip")
                    or any(not isinstance(choice[k], bool) for k in ("sync", "mkv", "convert") if k in choice)):
                raise SettingsError(M("each library’s optimizer choices must be {\"sync\": true/false, \"mkv\": true/false, "
                                      "\"images\": \"unmatched\", \"remove\", \"compress\" or \"plex\", \"tags\": \"keep\", \"excessive\" or \"strip\", "
                                      "\"convert\": true/false}"))
            if choice:
                out[str(lib)] = dict(choice)
        return out
    if (section, key) == ("optimizer", "convert_types"):
        if not isinstance(value, dict) or any(k not in CONVERT_DEFAULTS or not isinstance(v, bool) for k, v in value.items()):
            raise SettingsError(M("optimizer.convert_types must set each type to true or false; the types are {types}",
                                  types=", ".join(CONVERT_DEFAULTS)))
        return dict(CONVERT_DEFAULTS, **value)
    if key == "libraries":
        if not isinstance(value, dict) or len(value) > MAX_ITEMS:
            raise SettingsError(M("nextepisode.libraries must be an object"))
        out = {}
        for lib, choice in value.items():
            if not (_LIB_KEY.fullmatch(str(lib)) or lib == "*"):
                raise SettingsError(M("unknown library: {key}", key=str(lib)))
            if choice is None:
                continue  # null puts a library back to its defaults
            if not isinstance(choice, dict) or any(k not in PREFETCH_FIELDS for k in choice):
                raise SettingsError(M("each library’s prefetch choices must be an object"))
            for k, v in choice.items():
                if v not in PREFETCH_FIELDS[k]:
                    raise SettingsError(M("unknown prefetch choice: {mode}", mode=str(v)))
            out[str(lib)] = {k: v for k, v in choice.items() if not (k != "end_at" and v == "default")}
        return out
    if key == "disk_speeds":
        if not isinstance(value, dict) or len(value) > MAX_ITEMS:
            raise SettingsError(M("disk_speeds must be an object"))
        out = {}
        for name, row in value.items():
            _text(section, key, name, 255)
            if row is None:
                continue  # null removes a dataset's measurement
            if not isinstance(row, dict):
                raise SettingsError(M("disk speed must be an object"))
            mb, at = row.get("mb_s"), row.get("measured_at")
            if isinstance(mb, bool) or not isinstance(mb, (int, float)) or not (math.isfinite(mb) and 10 <= mb <= 20000):
                raise SettingsError(M("disk speed must be between 10 and 20000 MB/s"))
            if at is not None and not (isinstance(at, str) and _ISO_TIME.fullmatch(at)):
                raise SettingsError(M("disk speed measured_at must be a date and time"))
            out[name] = {"mb_s": int(mb), "measured_at": at}
        return out
    if key == "path_map":
        if not isinstance(value, list) or len(value) > MAX_ITEMS:
            raise SettingsError(M("path_map must be a list"))
        rows = []
        for row in value:
            if not isinstance(row, dict):
                raise SettingsError(M("each media mapping must be an object"))
            plex, local = row.get("plex") or "", row.get("local") or ""
            if plex and local:  # a row without both is a mapping being removed
                # Plex's side is only matched as text (Plex may even run on Windows); ours must be a real path here
                rows.append({"plex": _text(section, key, plex, 1024).rstrip("/") or "/", "local": _path(section, key, local)})
        return rows
    raise SettingsError(M("{setting} can’t be changed", setting=where))  # anything not covered above


class Settings:
    """Thread-safe settings store backed by ``<config_dir>/settings.json``."""

    def __init__(self, config_dir: str):
        self.dir = config_dir
        self.path = os.path.join(config_dir, "settings.json")
        self._lock = threading.RLock()
        # bumped by every change: what's worked out from the settings (Library Health's checks and
        # estimates, kept a little while) is worked out again, never kept past a change
        self.rev = 0
        os.makedirs(config_dir, exist_ok=True)
        self._data = self._load()
        changed = False
        # 0.9.0 had per-library checkboxes for Prefetch; an unticked one becomes "Don't preload".
        ne = self._data.get("nextepisode", {})
        if ne.get("excluded_libraries"):
            modes = ne.setdefault("library_modes", {})
            for k in ne["excluded_libraries"]:
                modes.setdefault(str(k), "off")
        # Before 0.15: one warm-up schedule for every library ("*" is shared out once Plex's libraries are known).
        w = self._data.get("warmup", {})
        if "schedule" in w and w.get("schedule") in OLD_SCHEDULE and not (w.get("libraries") or {}).get("*"):
            if w.get("enabled") or w["schedule"] == "manual":
                w.setdefault("libraries", {})["*"] = {"schedule": OLD_SCHEDULE[w["schedule"]]}
            changed = True
        # 0.15 betas: near-the-end "off" became the choice "Nothing"; warm-up had an on/off switch
        # besides each library's schedule (off: every library without its own schedule gets none).
        for row in (ne.get("libraries") or {}).values():
            if isinstance(row, dict) and row.get("end_at") == "off":
                del row["end_at"]
                row["end"] = "none"
                changed = True
            # ... and the whole season in full (including what was already watched) became the rest of it
            if isinstance(row, dict) and row.get("end") == "whole":
                row["end"] = "rest"
                changed = True
        if w.get("enabled") is False and not (w.get("libraries") or {}):
            w.setdefault("libraries", {})["*"] = {"schedule": "none"}
            changed = True
        # 0.15 betas: cooldowns were in hours; now minutes (a value no longer offered becomes the nearest one)
        gen = self._data.get("general", {})
        for key in [k for k in gen if k.endswith("_cooldown_lead_in_hours") or k.endswith("_cooldown_whole_hours")]:
            mins = gen.pop(key) * 60
            new = key[:-len("hours")] + "minutes"
            offered = COOLDOWN_L2ARC if new.startswith("l2arc") else COOLDOWN_RAM
            gen[new] = min(offered, key=lambda m: abs(m - mins)) if isinstance(mins, (int, float)) else DEFAULTS["general"][new]
            changed = True
        # 0.15 betas: a monthly warm-up became twice monthly
        for row in (w.get("libraries") or {}).values():
            if isinstance(row, dict) and row.get("schedule") in OLD_LIBRARY_SCHEDULE:
                row["schedule"] = OLD_LIBRARY_SCHEDULE[row["schedule"]]
                changed = True
        # Prefetch had an on/off switch until 0.15: off becomes "Don't prefetch" and "Nothing" for
        # every library without its own choices ("*" is shared out once Plex's libraries are known).
        if ne.get("enabled") is False:
            libs = ne.setdefault("libraries", {})
            if "*" not in libs:
                libs["*"] = {"start": "off", "end": "none"}
            changed = True
        # Before 0.15: one preload choice per library, and one start time for all of them.
        if ne.get("library_modes"):
            libs = ne.setdefault("libraries", {})
            for k, mode in ne["library_modes"].items():
                if mode in OLD_MODES and str(k) not in libs:
                    libs[str(k)] = dict(OLD_MODES[mode])
            changed = True
        for sec, values in DEFAULTS.items():
            for key in [k for k in self._data.get(sec, {}) if k not in values]:
                del self._data[sec][key]  # settings retired in a later version
                changed = True
        # Retired choices from earlier versions fall back to the default.
        for (sec, key), allowed in CHOICES.items():
            if self._data[sec][key] not in allowed:
                self._data[sec][key] = DEFAULTS[sec][key]
                changed = True
        if not self._data["general"]["client_id"]:
            self._data["general"]["client_id"] = str(uuid.uuid4())
            changed = True
        # First-run seeding from environment variables (handy for compose files).
        for env, (sec, key) in {
            "PLEX_URL": ("plex", "url"),
            "PLEX_TOKEN": ("plex", "token"),
            "TRUENAS_URL": ("truenas", "url"),
            "TRUENAS_API_KEY": ("truenas", "api_key"),
        }.items():
            if os.environ.get(env) and not self._data[sec][key]:
                self._data[sec][key] = _coerce(sec, key, os.environ[env], "")
                changed = True
        if changed or not os.path.exists(self.path):
            self._save()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return _merge(DEFAULTS, json.load(fh))
        except FileNotFoundError:
            return copy.deepcopy(DEFAULTS)
        except (OSError, ValueError):
            # Keep the broken file for the user and start from defaults.
            try:
                os.replace(self.path, self.path + ".broken")
            except OSError:
                pass
            return copy.deepcopy(DEFAULTS)

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def section(self, name: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._data[name])

    def public(self) -> dict:
        """Settings safe to send to the browser (secrets masked)."""
        data = self.get()
        for sec, key in SECRET_PATHS:
            data[sec][key + "_set"] = bool(data[sec][key])
            data[sec][key] = SECRET_MASK if data[sec][key] else ""
        return data

    def update(self, patch: dict) -> dict:
        """Apply a partial update like ``{"warmup": {"pacing_percent": 20}}``."""
        if not isinstance(patch, dict):
            raise SettingsError(M("settings update must be an object"))
        with self._lock:
            new = copy.deepcopy(self._data)
            for sec, values in patch.items():
                if sec not in DEFAULTS or not isinstance(values, dict):
                    raise SettingsError(M("unknown settings section: {section}", section=str(sec)))
                for key, value in values.items():
                    if key.endswith("_set") or key == "client_id":
                        continue
                    if key not in DEFAULTS[sec]:
                        raise SettingsError(M("unknown setting: {setting}", setting=f"{sec}.{key}"))
                    if (sec, key) in SECRET_PATHS and value == SECRET_MASK:
                        continue
                    new[sec][key] = _coerce(sec, key, value, DEFAULTS[sec][key])
            if new["nowplaying"]["ram_percent"] + new["nextepisode"]["ram_percent"] > 100:
                raise SettingsError(M("Now Playing Cache and Prefetch limits together can’t be more than 100%"))
            if new != self._data:
                self.rev += 1
            self._data = new
            self._save()
            return copy.deepcopy(self._data)

    def export(self) -> dict:
        data = self.get()
        for sec, key in SECRET_PATHS:
            data[sec][key] = ""
        data["general"]["client_id"] = ""
        return data

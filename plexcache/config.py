"""Settings storage.

Settings live in one JSON file in the config folder (``/config`` in the
container), so they survive restarts and app updates no matter how the
process is started.  Secrets are never sent back to the browser; the UI gets
a placeholder instead and sending the placeholder back keeps the stored value.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import uuid

SECRET_MASK = "__unchanged__"
SECRET_PATHS = (("plex", "token"), ("truenas", "api_key"), ("mqtt", "password"))

DEFAULTS: dict = {
    "plex": {"url": "", "token": ""},
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
        # RAM kept out of reach of Now-playing and Next-episode, for TrueNAS,
        # other apps and ZFS itself. Their limits are shares of what's left.
        "ram_reserve_gb": 4,
        "debug": False,  # unused: logging detail comes from PLEXCACHE_DEBUG (kept so older exports still import)
        "excluded_libraries": [],
        # Optional rewrites when Plex sees media at a different path than
        # this app does: [{"plex": "/data", "local": "/media"}]
        "path_map": [],
        "client_id": "",
    },
    "warmup": {
        "enabled": False,
        "schedule": "daily",  # daily | weekly | hours | manual
        "time": "03:00",
        "weekday": 6,  # 0 = Monday
        "every_hours": 12,
        "pacing_percent": 10,
        "movie_sec": 60,
        "tv_sec": 30,
        "audio_sec": 10,
        "movie_min_sec": 3,
        "tv_min_sec": 3,
        "audio_min_sec": 1,
        "reserve_gb": 100,
        "padding_video": 5,
        "padding_audio": 15,
        # When to pause (it carries on from where it was afterwards).
        "pause_buffering": True,   # a player reports buffering (and for a few minutes after)
        "pause_preload": True,     # Now-playing or Next-episode is reading
        "pause_scrub": True,       # a scrub or resilver is running on a media pool
        "pause_disk_busy": 50,     # pool disks this % busy with other work (0 = off)
        "finish_by_enabled": False,
        "finish_by": "07:00",      # pause at this time until the next scheduled start
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
    "nextepisode": {
        "enabled": True,
        # default | after_nowplaying | "80" | "90" (percent through the episode)
        "start_at": "default",
        "ram_percent": 20,  # share of the usable RAM cache
        "pacing_percent": 50,
        # Per library (Plex library key -> mode); libraries not listed use their type's default.
        # TV: "off" | "next". Music and audiobooks: "off" | "10" | "20" | "60" (minutes ahead),
        # "rest" (rest of the album/book), "album" (the whole album/book), "artist" (everything
        # by the artist/author). All within the RAM limit above.
        "library_modes": {},
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
    },
}

TV_MODES = ("off", "next")
AUDIO_MODES = ("off", "10", "20", "60", "rest", "album", "artist")
DEFAULT_MODE = {"tv": "next", "audio": "20"}

# Basic validation: (min, max) for numeric fields.
LIMITS = {
    ("general", "poll_seconds"): (2, 120),
    ("general", "hdd_mb_per_sec"): (10, 5000),
    ("general", "ram_reserve_gb"): (0, 4096),
    ("warmup", "weekday"): (0, 6),
    ("warmup", "every_hours"): (1, 168),
    ("warmup", "pacing_percent"): (1, 100),
    ("warmup", "movie_sec"): (1, 600),
    ("warmup", "tv_sec"): (1, 600),
    ("warmup", "audio_sec"): (1, 600),
    ("warmup", "movie_min_sec"): (1, 600),
    ("warmup", "tv_min_sec"): (1, 600),
    ("warmup", "audio_min_sec"): (1, 600),
    ("warmup", "reserve_gb"): (0, 100000),
    ("warmup", "padding_video"): (0, 100),
    ("warmup", "padding_audio"): (0, 100),
    ("warmup", "pause_disk_busy"): (0, 100),
    ("nowplaying", "ram_percent"): (1, 90),
    ("nowplaying", "delay_sec"): (0, 600),
    ("nowplaying", "buffer_direct_sec"): (0, 600),
    ("nowplaying", "buffer_transcode_sec"): (0, 600),
    ("nowplaying", "min_delay_sec"): (0, 600),
    ("nowplaying", "max_streams"): (1, 20),
    ("nowplaying", "pacing_percent"): (1, 100),
    ("nextepisode", "ram_percent"): (1, 90),
    ("nextepisode", "pacing_percent"): (1, 100),
    ("mqtt", "port"): (1, 65535),
    ("mqtt", "interval"): (5, 600),
}
CHOICES = {
    ("warmup", "schedule"): ("daily", "weekly", "hours", "manual"),
    ("nextepisode", "start_at"): ("default", "after_nowplaying", "80", "90"),
    ("mqtt", "report_period"): ("today", "week", "month"),
}


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
    if section == "plex" and value.lower().startswith("http://"):
        # (an https:// address is usually a reverse proxy on 443, so leave it alone)
        scheme, rest = value.split("://", 1)
        hostport = rest.split("/", 1)[0]
        has_port = re.search(r":\d+$", hostport) and not hostport.endswith("]")
        if not has_port:
            value = f"{scheme}://{hostport}:{PLEX_DEFAULT_PORT}" + (rest[len(hostport):] if "/" in rest else "")
    return value


def _coerce(section: str, key: str, value, current):
    """Coerce an incoming value to the type of the current one and validate it."""
    if key == "pause_disk_busy":
        try:
            num = int(float(value))
        except (TypeError, ValueError):
            num = -1
        if num not in (0, 40, 50, 60):
            raise SettingsError("warmup.pause_disk_busy must be 0, 40, 50 or 60")
        return num
    if (section, key) in CHOICES:
        value = str(value)
        if value not in CHOICES[(section, key)]:
            raise SettingsError(f"{section}.{key} must be one of {CHOICES[(section, key)]}")
        return value
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{section}.{key} must be a number") from None
        lo, hi = LIMITS.get((section, key), (None, None))
        if lo is not None and not (lo <= num <= hi):
            raise SettingsError(f"{section}.{key} must be between {lo} and {hi}")
        return int(num) if isinstance(current, int) and num == int(num) else num
    if key in ("time", "finish_by"):
        value = str(value)
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise SettingsError("time must look like 03:00")
        return value
    if key == "url":
        return normalize_url(section, str(value))
    if key == "excluded_libraries":
        if not isinstance(value, list):
            raise SettingsError("excluded_libraries must be a list")
        return [str(x) for x in value]
    if key in ("base_topic", "discovery_prefix"):
        value = str(value).strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*", value):
            raise SettingsError(f"{key} may only use letters, numbers, - _ and /")
        return value
    if key == "library_modes":
        if not isinstance(value, dict):
            raise SettingsError("library_modes must be an object")
        out = {}
        for lib, mode in value.items():
            if mode not in AUDIO_MODES and mode not in TV_MODES:
                raise SettingsError(f"unknown preload choice: {mode}")
            out[str(lib)] = str(mode)
        return out
    if key == "disk_speeds":
        if not isinstance(value, dict):
            raise SettingsError("disk_speeds must be an object")
        out = {}
        for name, row in value.items():
            if row is None:
                continue  # null removes a dataset's measurement
            try:
                mb = float(row.get("mb_s"))
            except (TypeError, ValueError, AttributeError):
                raise SettingsError("disk speed must be a number") from None
            if not 10 <= mb <= 20000:
                raise SettingsError("disk speed must be between 10 and 20000 MB/s")
            at = row.get("measured_at")
            out[str(name)] = {"mb_s": int(mb), "measured_at": str(at) if at else None}
        return out
    if key == "path_map":
        if not isinstance(value, list):
            raise SettingsError("path_map must be a list")
        rows = []
        for row in value:
            plex, local = str(row.get("plex", "")).rstrip("/"), str(row.get("local", "")).rstrip("/")
            if plex and local:
                rows.append({"plex": plex, "local": local})
        return rows
    if current is None or isinstance(current, str):
        return None if value is None else str(value)
    return value


class Settings:
    """Thread-safe settings store backed by ``<config_dir>/settings.json``."""

    def __init__(self, config_dir: str):
        self.dir = config_dir
        self.path = os.path.join(config_dir, "settings.json")
        self._lock = threading.RLock()
        os.makedirs(config_dir, exist_ok=True)
        self._data = self._load()
        changed = False
        # 0.9.0 had per-library checkboxes for Next-episode; an unticked one becomes "Don't preload".
        ne = self._data.get("nextepisode", {})
        if ne.get("excluded_libraries"):
            modes = ne.setdefault("library_modes", {})
            for k in ne["excluded_libraries"]:
                modes.setdefault(str(k), "off")
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
        """Apply a partial update like ``{"warmup": {"tv_sec": 20}}``."""
        if not isinstance(patch, dict):
            raise SettingsError("settings update must be an object")
        with self._lock:
            new = copy.deepcopy(self._data)
            for sec, values in patch.items():
                if sec not in DEFAULTS or not isinstance(values, dict):
                    raise SettingsError(f"unknown settings section: {sec}")
                for key, value in values.items():
                    if key.endswith("_set") or key == "client_id":
                        continue
                    if key not in DEFAULTS[sec]:
                        raise SettingsError(f"unknown setting: {sec}.{key}")
                    if (sec, key) in SECRET_PATHS and value == SECRET_MASK:
                        continue
                    new[sec][key] = _coerce(sec, key, value, DEFAULTS[sec][key])
            w = new["warmup"]
            for kind in ("movie", "tv", "audio"):
                if w[f"{kind}_min_sec"] > w[f"{kind}_sec"]:
                    w[f"{kind}_min_sec"] = w[f"{kind}_sec"]
            if new["nowplaying"]["ram_percent"] + new["nextepisode"]["ram_percent"] > 100:
                raise SettingsError("Now-playing and Next-episode limits together can't be more than 100%")
            self._data = new
            self._save()
            return copy.deepcopy(self._data)

    def export(self) -> dict:
        data = self.get()
        for sec, key in SECRET_PATHS:
            data[sec][key] = ""
        data["general"]["client_id"] = ""
        return data

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
SECRET_PATHS = (("plex", "token"), ("truenas", "api_key"))

DEFAULTS: dict = {
    "plex": {"url": "", "token": ""},
    "truenas": {"url": "", "api_key": "", "verify_tls": False},
    "general": {
        "poll_seconds": 5,
        "hdd_mb_per_sec": 150,
        "hdd_measured_at": None,
        "pause_when_streams": 3,
        "debug": False,
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
    },
    "nowplaying": {
        "enabled": False,
        "mode": "minutes",  # minutes | whole | size
        "minutes": 10,
        "size_gb": 10,
        "delay_sec": 0,  # 0 = automatic
        "max_streams": 2,
        "min_kbps": 0,
        "pacing_percent": 50,
    },
    "nextepisode": {
        "enabled": True,
        "start_at": "50",  # "0", "50", "75" (percent) or "credits"
        "max_gb": 2,
        "pacing_percent": 50,
    },
}

# Basic validation: (min, max) for numeric fields.
LIMITS = {
    ("general", "poll_seconds"): (2, 120),
    ("general", "hdd_mb_per_sec"): (10, 5000),
    ("general", "pause_when_streams"): (0, 50),
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
    ("nowplaying", "minutes"): (1, 600),
    ("nowplaying", "size_gb"): (1, 1000),
    ("nowplaying", "delay_sec"): (0, 600),
    ("nowplaying", "max_streams"): (1, 20),
    ("nowplaying", "min_kbps"): (0, 200000),
    ("nowplaying", "pacing_percent"): (1, 100),
    ("nextepisode", "max_gb"): (0, 200),
    ("nextepisode", "pacing_percent"): (1, 100),
}
CHOICES = {
    ("warmup", "schedule"): ("daily", "weekly", "hours", "manual"),
    ("nowplaying", "mode"): ("minutes", "whole", "size"),
    ("nextepisode", "start_at"): ("0", "50", "75", "credits"),
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


def _coerce(section: str, key: str, value, current):
    """Coerce an incoming value to the type of the current one and validate it."""
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
    if key == "time":
        value = str(value)
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise SettingsError("time must look like 03:00")
        return value
    if key in ("url",):
        value = str(value).strip().rstrip("/")
        if value and not re.match(r"^https?://", value):
            value = "http://" + value
        return value
    if key == "excluded_libraries":
        if not isinstance(value, list):
            raise SettingsError("excluded_libraries must be a list")
        return [str(x) for x in value]
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
            self._data = new
            self._save()
            return copy.deepcopy(self._data)

    def export(self) -> dict:
        data = self.get()
        for sec, key in SECRET_PATHS:
            data[sec][key] = ""
        data["general"]["client_id"] = ""
        return data


# --------------------------------------------------------------------------
# Import from the old bash scripts' plex-l2arc.conf
# --------------------------------------------------------------------------

_LEGACY_MAP = {
    "PRELOAD_PACING_PERCENT": ("warmup", "pacing_percent"),
    "RESERVED_L2ARC_GB": ("warmup", "reserve_gb"),
    "MOVIE_PRELOAD_MIN_SEC": ("warmup", "movie_min_sec"),
    "MOVIE_PRELOAD_MAX_SEC": ("warmup", "movie_sec"),
    "TV_PRELOAD_MIN_SEC": ("warmup", "tv_min_sec"),
    "TV_PRELOAD_MAX_SEC": ("warmup", "tv_sec"),
    "AUDIO_PRELOAD_MIN_SEC": ("warmup", "audio_min_sec"),
    "AUDIO_PRELOAD_MAX_SEC": ("warmup", "audio_sec"),
    "PADDING_VIDEO": ("warmup", "padding_video"),
    "PADDING_AUDIO": ("warmup", "padding_audio"),
    "HDD_MB_PER_SEC": ("general", "hdd_mb_per_sec"),
    "PLEX_TOKEN": ("plex", "token"),
}


def parse_legacy_conf(text: str) -> tuple[dict, list[str]]:
    """Turn a plex-l2arc.conf into a settings patch.

    The file is parsed as plain ``KEY="value"`` lines; it is never executed.
    Returns ``(patch, notes)``.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"""^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(?:"([^"]*)"|'([^']*)'|([^\s#]*))""", line)
        if m:
            values[m.group(1)] = next(g for g in m.groups()[1:] if g is not None)
    patch: dict = {}
    notes: list[str] = []
    for name, (sec, key) in _LEGACY_MAP.items():
        if name in values and values[name] != "":
            patch.setdefault(sec, {})[key] = values[name]
    host = values.get("PLEX_HOST")
    if host:
        proto = values.get("PLEX_PROTOCOL", "http") or "http"
        port = values.get("PLEX_PORT", "32400") or "32400"
        patch.setdefault("plex", {})["url"] = f"{proto}://{host}:{port}"
        if host in ("127.0.0.1", "localhost"):
            notes.append(
                "Plex address was 127.0.0.1. Inside this app that means the app itself — "
                "change it to your NAS's IP address."
            )
    if values.get("PLEX_PATH_PREFIX") or values.get("TRUENAS_PATH_PREFIX"):
        notes.append(
            "Path prefixes are not needed any more: mount your media in this app at the same "
            "path Plex uses (Health checks it). Use Settings → Path rewrites only if you can't."
        )
    if "PRELOAD_PACING_PERCENT_CONSOLE" in values:
        notes.append("Console pacing is now the “Run now” button, which runs at full speed.")
    if not patch:
        notes.append("No recognised settings were found in that file.")
    return patch, notes

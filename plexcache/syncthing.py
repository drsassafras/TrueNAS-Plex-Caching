"""Syncthing, the folder sync most mirrors run (System → Syncthing): read and asked through its REST API on this server,
with the API key its settings show. What it's for:

* **Mirror Checks** (mirrorhealth.py, through findings()): for each library mirrored, whether Syncthing runs and answers,
  a folder of it covers the library folder, that folder isn't paused, watches for changes, sends the smallest files first
  (the markers and claims in .plexcache, small, then reach the other site before the big files they're about), doesn't
  ignore .plexcache, and the device at the other site is connected. Each is a check of Library Health's
  ("mirror_syncthing_<what>", libhealth.CHECKS), with the fix as its action where the API can make it (act(): resume the
  folder, watch for changes, smallest first).
* **Convert Folder to Child Dataset** (migrating.py): the folder paused before the switch and resumed after, and the ignore
  patterns that keep the new dataset and the old folder from reaching the other site added — all through the API, where
  before you did them in Syncthing and said so.
* **The System card**: its lights (found, the key, the folders over the mirrored libraries, the devices they're shared
  with), and this device's name and ID with Syncthing's own picture of it (the identicon its UI draws for every device,
  the same cells: app.js stIdenticon), so the two can be matched by eye.

Nothing here reads the media: only Syncthing's settings and what it reports. Asked every couple of minutes (tick_async)
and on Test Again; between, the last answer is kept (status). Which Syncthing folder is a library's: the one whose path
is the library folder or a folder above it, in any of the forms the folder is known by — as Plex, this app and the host
see it, since Syncthing runs in a container of its own with mounts of its own — else the one named for a folder on the
way (covering())."""

import base64
import hashlib
import ipaddress as _ipaddress
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from . import zfs
from .i18n import M


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""

log = logging.getLogger("plexcache")

DEFAULT_PORT = 8384
REFRESH = 120          # seconds between askings while set up
ITEM = "syncthing"     # the item its findings are on (a library's, not a file's): "syncthing:<folder id>" per folder
MARK = ".plexcache"    # the folder the markers and claims travel in: never to be ignored
# its checks, each "mirror_syncthing_<what>" in libhealth.CHECKS ("mirror"), and findings() what each finds
CHECKS = ("down", "no_folder", "paused", "not_watched", "order", "ignores", "disconnected", "transit", "other_device")
SYNC_PORT = 22000       # the port devices talk on: its certificate is the device's identity (cert_device_id), read with no key
TRANSIT_MAX = 500       # files on their way listed per folder and direction
ALPHA32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
HEALTH = "/rest/noauth/health"  # answers {"status": "OK"} without a key: how a Syncthing is found (discover)
DISCOVER_EVERY = 3600           # seconds between looks for one while no address is set
HOT_HOLD, CALM_HOLD = 30, 60     # the throttle: seconds the disks must be busy before Syncthing is held back, and calm before it's let go
NET_GRACE = 60                   # the give-way: seconds after the last remote stream before the internet devices are let go
TAILNET = _ipaddress.ip_network("100.64.0.0/10")  # Tailscale's range (CGNAT): a tailnet device is reached over the internet, whatever its address looks like
THROTTLE_FILE = "syncthing-throttle.json"  # what was held back and the limits it had, so a restart lets go of it too
# Every call the app makes, by what it's for (as truenas.METHODS): checked against this Syncthing when it's connected
# (API compatibility light), each tried as the app would make it; a 404 or 405 means this release hasn't got it.
CALLS = {"the system's status": ("GET", "/rest/system/status"), "the version": ("GET", "/rest/system/version"),
         "the configuration": ("GET", "/rest/config"), "the connections": ("GET", "/rest/system/connections"),
         "a folder's state": ("GET", "/rest/db/status"), "a folder's ignore patterns": ("GET", "/rest/db/ignores"), "a folder's top-level entries": ("GET", "/rest/db/browse"),
         "what's still to come": ("GET", "/rest/db/need"), "what a device still needs": ("GET", "/rest/db/remoteneed"),
         "changing a folder's settings": ("PATCH", "/rest/config/folders/{id}"), "changing a device's settings": ("PATCH", "/rest/config/devices/{id}"),
         "asking for a scan": ("POST", "/rest/db/scan")}
ORDERS = {"random": M("random"), "alphabetic": M("alphabetical"), "smallestFirst": M("smallest first"), "largestFirst": M("largest first"),
          "oldestFirst": M("oldest first"), "newestFirst": M("newest first")}
# the findings' fixes the API can make: check -> (the folder's setting, its value, what was asked)
FIXES = {"mirror_syncthing_paused": ("paused", False, M("Syncthing asked to resume the folder")),
         "mirror_syncthing_not_watched": ("fsWatcherEnabled", True, M("Syncthing asked to watch the folder for changes")),
         "mirror_syncthing_order": ("order", "smallestFirst", M("Syncthing asked to send the smallest files first"))}


class SyncthingError(Exception):
    """``code``: "auth" (the key refused), "reach" (nothing answered), "missing" (404 or 405: the release hasn't got the call),
    "state" (a 404 about the folder itself — paused, or not in Syncthing's database yet — not about the call), "answer"
    (something else)."""

    def __init__(self, why: M, code: str = "answer"):
        super().__init__(why)
        self.why, self.code = why, code


class SyncthingClient:
    """Syncthing's REST API (https://docs.syncthing.net/dev/rest.html): the few calls the app makes."""

    def __init__(self, url: str, api_key: str, timeout: float = 10, tls: ssl.SSLContext | None = None):
        """``tls``: the context the connection is verified with (tlspin, through Syncthing.client: the pin, or full
        verification); without one, the certificate isn't checked — tests and keyless probes only."""
        self.url, self.key, self.timeout = url.rstrip("/"), api_key, timeout
        self.after_tls: Callable[[bool], object] | None = None  # called with whether the connection was made (app.tls_ok / tls_refused)
        if tls is not None:
            self.ctx = tls
        else:
            self.ctx = ssl.create_default_context()  # (its own certificate is self-signed: not checked)
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _req(self, method: str, path: str, params: dict | None = None, body=None):
        host = (urllib.parse.urlsplit(self.url).hostname or "").lower()
        if self.key and self.url.lower().startswith("http://") and not (host == "localhost" or host == "::1" or host.startswith("127.")):
            # the key signs every request: over the network it goes only on an encrypted connection
            raise SyncthingError(M("Syncthing’s API key isn’t sent over plain http across the network: use an https:// address (Syncthing answers on one), or a localhost address."), "reach")
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path + q, data=data, method=method,
                                     headers={"X-API-Key": self.key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if self.after_tls is not None:
                self.after_tls(True)  # (an HTTP answer: the connection, and its certificate, went through)
            try:
                body = e.read()[:200].decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                body = ""
            e.close()  # (it holds the connection open)
            if e.code in (401, 403):
                raise SyncthingError(M("Syncthing didn’t accept the API key."), "auth") from None
            if e.code == 404 and "folder" in body.lower():  # ("no such folder", "folder is paused": the folder's state, not a call it lacks)
                raise SyncthingError(M("Syncthing on {what}: {why}", what=path, why=body), "state") from None
            raise SyncthingError(M("Syncthing answered {code} to {what}.", code=str(e.code), what=path), "missing" if e.code in (404, 405) else "answer") from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if self.after_tls is not None:
                self.after_tls(False)
            raise SyncthingError(M("Nothing answered at {url}: {why}.", url=self.url, why=str(getattr(e, "reason", None) or e)), "reach") from None
        if self.after_tls is not None:
            self.after_tls(True)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise SyncthingError(M("What answered at {url} isn’t Syncthing (its answer to {what} isn’t JSON).", url=self.url, what=path)) from None

    def status(self) -> dict:
        return self._req("GET", "/rest/system/status")

    def healthy(self) -> bool:
        """Whether a Syncthing answers at the address (its health endpoint needs no key)."""
        try:
            return (self._req("GET", HEALTH) or {}).get("status") == "OK"
        except SyncthingError:
            return False

    def version(self) -> dict:
        return self._req("GET", "/rest/system/version")

    def config(self) -> dict:
        return self._req("GET", "/rest/config")

    def connections(self) -> dict:
        return self._req("GET", "/rest/system/connections")

    def folder_status(self, folder: str) -> dict:
        return self._req("GET", "/rest/db/status", {"folder": folder})

    def ignores(self, folder: str) -> list[str]:
        return list(self._req("GET", "/rest/db/ignores", {"folder": folder}).get("ignore") or [])

    def browse(self, folder: str) -> list[str]:
        """The names at the top level of a folder, as Syncthing lists them (/rest/db/browse, one level: a list of entries in
        current releases, a tree of names in old ones)."""
        got = self._req("GET", "/rest/db/browse", {"folder": folder, "levels": 0})
        if isinstance(got, dict):
            return [str(k) for k in got]
        return [str(e.get("name")) for e in got or [] if isinstance(e, dict) and e.get("name")]

    def need(self, folder: str) -> list[str]:
        """The files this device still has to fetch into a folder (on their way here), the first TRANSIT_MAX."""
        got = self._req("GET", "/rest/db/need", {"folder": folder, "page": 1, "perpage": TRANSIT_MAX}) or {}
        return [str(f.get("name")) for k in ("progress", "queued", "rest") for f in got.get(k) or [] if isinstance(f, dict) and f.get("name")][:TRANSIT_MAX]

    def remote_need(self, folder: str, device: str) -> list[str]:
        """The files a device still has to fetch from this one (on their way out), the first TRANSIT_MAX."""
        got = self._req("GET", "/rest/db/remoteneed", {"folder": folder, "device": device, "page": 1, "perpage": TRANSIT_MAX}) or {}
        return [str(f.get("name")) for f in got.get("files") or [] if isinstance(f, dict) and f.get("name")][:TRANSIT_MAX]

    def set_ignores(self, folder: str, lines: list[str]) -> None:
        self._req("POST", "/rest/db/ignores", {"folder": folder}, {"ignore": list(lines)})

    def patch_folder(self, folder: str, fields: dict) -> None:
        self._req("PATCH", "/rest/config/folders/" + urllib.parse.quote(folder, safe=""), None, fields)

    def patch_device(self, device: str, fields: dict) -> None:
        self._req("PATCH", "/rest/config/devices/" + urllib.parse.quote(device, safe=""), None, fields)

    def scan(self, folder: str, sub: str = "") -> None:
        self._req("POST", "/rest/db/scan", {"folder": folder, **({"sub": sub} if sub else {})})


def luhn32(s: str) -> str:
    """Syncthing's check character for a base32 string (its luhn package: mod 32)."""
    factor, total = 1, 0
    for c in s:
        addend = factor * ALPHA32.index(c)
        factor = 1 if factor == 2 else 2
        total += addend // 32 + addend % 32
    return ALPHA32[(32 - total % 32) % 32]


def device_id_from_cert(der: bytes) -> str:
    """A Syncthing device's ID from its certificate, as Syncthing writes it: the SHA-256 of the certificate in base32, a check
    character after each 13, in groups of 7 — GAEALE5-IUWJBCI-…"""
    raw = base64.b32encode(hashlib.sha256(der).digest()).decode().rstrip("=")
    full = "".join(raw[i:i + 13] + luhn32(raw[i:i + 13]) for i in range(0, 52, 13))
    return "-".join(full[i:i + 7] for i in range(0, 56, 7))


def cert_device_id(host: str, port: int = SYNC_PORT, timeout: float = 4) -> str | None:
    """The device ID of the Syncthing at host:port, from the certificate it presents to anyone who connects on its sync port
    (no key needed: devices recognise each other by it). None when nothing answers there."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            der = t.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError, ValueError):
        return None
    return device_id_from_cert(der) if der else None


def ignored_by(lines, rel: str) -> bool:
    """Whether Syncthing's ignore patterns ``lines`` (a folder's .stignore, or what its API gives) leave out ``rel`` (a path
    within the folder), as Syncthing reads them: the first pattern that matches decides; "/a" anchored at the root, "a" at
    any level; *, ? and ** wildcards; (?d) and (?i) prefixes; "!" lets a path back in; a folder left out leaves out what's
    in it. #include lines aren't followed (their patterns don't count here)."""
    rel = rel.strip("/")
    for line in lines or []:
        pat = str(line).strip()
        if not pat or pat.startswith("//") or pat.startswith("#"):
            continue
        neg = pat.startswith("!")
        pat = pat[1:] if neg else pat
        fold = False
        while pat[:4] in ("(?d)", "(?i)"):
            fold = fold or pat[:4] == "(?i)"
            pat = pat[4:]
        pat = pat.rstrip("/")
        if not pat:
            continue
        body = re.escape(pat.lstrip("/")).replace(r"\*\*", "\0").replace(r"\*", "[^/]*").replace(r"\?", "[^/]").replace("\0", ".*")
        rx = ("^" if pat.startswith("/") else "^(?:.*/)?") + body + "(?:/.*)?$"
        if re.match(rx, rel, re.I if fold else 0):
            return not neg
    return False


def mark_in(rel: str) -> str:
    """Where a library's .plexcache is within the Syncthing folder over it (``rel``: the library folder's place in it)."""
    return (rel.strip("/") + "/" if rel and rel.strip("/") else "") + MARK


def covering(folders: list[dict], paths: list, names: bool = True) -> tuple[dict | None, str]:
    """The Syncthing folder over a library folder — the one whose path is the folder, or a folder above it, in any of the
    forms the folder is known by (``paths``: as this app, the host and Plex see it; Syncthing's path is its own
    container's), the nearest when several — or, with no path shared, the one named for a folder on the way (its last name
    within a path: the same folder mounted elsewhere). (the folder, where the library folder is within it: "" when it is
    the folder itself.)"""
    cands = [os.path.normpath(str(p)) for p in paths if p]
    best: tuple | None = None
    for f in folders:
        for root in {os.path.normpath(str(x)) for x in (f.get("path"), f.get("host_path")) if x}:  # (its path in Syncthing's container, and on the host)
            if root in ("", ".", "/"):
                continue
            for p in cands:
                if p == root or p.startswith(root.rstrip("/") + "/"):
                    rel = "" if p == root else os.path.relpath(p, root)
                    if best is None or len(root) > len(best[0]):
                        best = (root, f, rel)
    if best or not names:
        return (best[1], best[2]) if best else (None, "")
    for f in folders:
        known = folder_names(f)
        if not known:
            continue
        for p in cands:
            parts = p.strip("/").split("/")
            norm = [_plain(x) for x in parts]
            hits = [i for i, x in enumerate(norm) if x in known]
            if hits:
                i = hits[-1]
                rel = "/".join(parts[i + 1:])
                if best is None or len(rel) < len(best[2]):
                    best = (parts[i], f, rel)
    return (best[1], best[2]) if best else (None, "")


def _plain(s: str) -> str:
    """A name with only its letters and digits, lower case: "Plex Files", "plexfiles" and "plex-files" are one."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def folder_names(f: dict) -> set[str]:
    """The names a Syncthing folder goes by, plain (_plain): its path's last part, that part without "syncthing" (a mount
    named for the sync: /plexfilesSyncthing), and its label."""
    base = os.path.basename(os.path.normpath(str(f.get("path") or "")))
    out = {_plain(base), _plain(re.sub(r"syncthing", "", base, flags=re.I)), _plain(f.get("label") or "")}
    return {n for n in out if n}


def st_cells(device_id: str) -> list[tuple[int, int]]:
    """The cells Syncthing's UI fills in its picture of a device (gui/default/syncthing/core/identiconDirective.js): every
    non-word character dropped, then a 5×5 grid whose left half is read off the characters — the cell at (row, col) filled
    when the character at row + col × 5 has an even code (none there: NaN, filled) — mirrored to the right. app.js
    stIdenticon draws the same; dev/syncthing_api.py holds both to Syncthing's source."""
    value = re.sub(r"[\W_]", "", str(device_id))
    out = []
    for row in range(5):
        for col in (2, 1, 0):
            i = row + col * 5
            if i >= len(value) or ord(value[i]) % 2 == 0:
                out.append((row, col))
                if col != 2:
                    out.append((row, 4 - col))
    return out


class Syncthing:
    """Syncthing as this app sees it (app.syncthing): asked every couple of minutes while set up, the answer kept."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.status: dict = {}
        self.busy = False
        self.asked_at = 0.0
        self.acted: dict[str, float] = {}  # "<library>/<item>/<check>" -> when its fix was asked
        self._stamp = ""
        self.found: dict = {}  # the last look for one: {"at", "url" (found, or None), "tried": [urls]}
        load = getattr(app, "_load_json", None)
        self.throttled: dict | None = (load(THROTTLE_FILE) if load else None) or None  # {"since", "kbps", "why", "devices": {id: {the limits before}}}
        self.reading: dict = {}  # the throttle's last look: {"at", "playing", "busy"}
        self._hot_since: float | None = None
        self._calm_since: float | None = None
        self._disk_on = False
        self._net_last: float | None = None
        self._cert_id: tuple[float, str | None] = (0.0, None)  # the device ID read from the sync port's certificate, hourly
        self._mounts: tuple[float, dict] = (0.0, {})  # the apps' volume mounts from TrueNAS (host path, path in the container), hourly

    # ------------------------------------------------------------ settings, the client
    def settings(self) -> dict:
        return self.app.settings.get().get("syncthing") or {}

    def wanted(self) -> bool:
        return bool(self.settings().get("url"))

    def client(self, timeout: float = 10) -> SyncthingClient:
        s = self.settings()
        make = getattr(self.app, "tls_context", None)  # (tests' fakes have no TLS machinery)
        c = SyncthingClient(s.get("url") or "", s.get("api_key") or "", timeout, tls=make("syncthing", s) if make else None)
        if make is not None:
            c.after_tls = lambda ok, x=c.ctx: (self.app.tls_ok("syncthing", x) if ok else self.app.tls_refused("syncthing", x))
        return c

    def talking(self) -> bool:
        return bool(self.status.get("ok"))

    # ------------------------------------------------------------ asking
    def tick_async(self, force: bool = False) -> None:
        if not self.wanted():
            if self.status:
                with self.lock:
                    self.status, self._stamp = {}, ""
            return
        if self.busy or (not force and time.time() - self.asked_at < REFRESH):
            return
        self.busy = True
        threading.Thread(target=self._refresh_safe, name="syncthing", daemon=True).start()

    def _refresh_safe(self) -> None:
        try:
            self.refresh()
        except Exception:
            log.exception("syncthing")
        finally:
            self.busy = False

    def _library_paths(self) -> dict[str, list[tuple[str, list[str]]]]:
        """Per mirrored library, each of its folders in the forms it's known by: (as this app sees it, [as Plex sees it, as
        this app, as the host])."""
        mh = getattr(self.app, "mirror_health", None)
        keys = set(mh.libraries()) if mh is not None else set()
        path_map = (self.app.settings.get().get("general") or {}).get("path_map") or []
        out: dict[str, list] = {}
        for lib in getattr(self.app, "libraries", []) or []:
            if str(lib.get("key")) not in keys:
                continue
            for loc in lib.get("locations") or []:
                local = loc
                for m in path_map:
                    if loc == m["plex"] or loc.startswith(m["plex"].rstrip("/") + "/"):
                        local = m["local"].rstrip("/") + loc[len(m["plex"].rstrip("/")):]
                        break
                host = None
                try:
                    host = zfs.host_path(local)
                except Exception:  # noqa: BLE001
                    host = None
                out.setdefault(str(lib["key"]), []).append((local, [loc, local, host, self.host_of(local, "mine")]))
        return out

    def refresh(self) -> dict:
        """Syncthing asked how it is: its version and this device, every folder (its settings, the devices it's shared
        with and whether each is connected), which folder covers each mirrored library's folders, and those folders' state
        and ignore patterns. Kept as status."""
        s = self.settings()
        if not s.get("url"):
            url = self.discover()
            if not url:
                with self.lock:
                    self.status, self._stamp = {}, ""
                return {}
            self.app.settings.update({"syncthing": {"url": url}})  # (found by itself: filled in, the key is yours to paste)
            self.app.activity.event("settings", M("Syncthing found at {url}: its address filled in", url=url))
            s = self.settings()
        got: dict = {"at": time.time(), "ok": False, "error": None, "code": None, "url": s["url"], "version": "", "device": {}, "folders": [], "libraries": {},
                     "api": {}}  # (api: each call of CALLS -> True known, False missing, None not tried)
        if not s.get("api_key"):  # (no key yet: whether one answers at the address, nothing more asked)
            got.update(code="nokey", health=self.client(timeout=5).healthy())
            with self.lock:
                self.status, self.asked_at, self._stamp = got, time.time(), ""
            return got
        try:
            c = self.client()
            api = got["api"]

            def tried(name, fn):
                try:
                    out = fn()
                    api[name] = True
                    return out
                except SyncthingError as e:
                    if e.code == "missing":
                        api[name] = False
                        return None
                    raise
            v = tried("the version", c.version) or {}
            st = tried("the system's status", c.status) or {}
            cfg = tried("the configuration", c.config) or {}
            conns = ((tried("the connections", c.connections) or {}).get("connections") or {})
            my = str(st.get("myID") or "")
            devs = {str(d.get("deviceID")): d for d in cfg.get("devices") or []}
            got.update(ok=True, version=str(v.get("version") or ""), device={"id": my, "name": str((devs.get(my) or {}).get("name") or "")})
            folders = []
            for f in cfg.get("folders") or []:
                fid = str(f.get("id") or "")
                shared = [str(d.get("deviceID")) for d in f.get("devices") or [] if str(d.get("deviceID")) != my]
                folders.append({"id": fid, "label": str(f.get("label") or fid), "path": str(f.get("path") or ""), "paused": bool(f.get("paused")),
                                "order": str(f.get("order") or "random"), "watched": bool(f.get("fsWatcherEnabled")), "rescan": f.get("rescanIntervalS"),
                                "devices": [{"id": d, "name": str((devs.get(d) or {}).get("name") or d[:7]), "connected": bool((conns.get(d) or {}).get("connected")),
                                             "address": str((conns.get(d) or {}).get("address") or ""), "type": str((conns.get(d) or {}).get("type") or ""),
                                             "addresses": [str(a) for a in (devs.get(d) or {}).get("addresses") or []],
                                             "max_recv": int((devs.get(d) or {}).get("maxRecvKbps") or 0), "max_send": int((devs.get(d) or {}).get("maxSendKbps") or 0)} for d in shared],
                                "state": None, "need_files": None, "need_bytes": None, "ignores": None, "libraries": []})
            for row in folders:
                row["host_path"] = self.host_of(row["path"], "syncthing")  # (where its path is on the host, from TrueNAS's mounts of the Syncthing app)
            got["folders"] = folders
            opts = cfg.get("options") or {}
            got["options"] = {k: bool(opts.get(k, True)) for k in ("globalAnnounceEnabled", "localAnnounceEnabled", "relaysEnabled", "natEnabled")}
            byid = {f["id"]: f for f in folders}
            listings: dict[str, list[str] | None] = {}  # (a folder's top level, asked once: for a library folder no path form matches)

            def listed(fid: str) -> list[str]:
                if byid[fid]["paused"]:  # (a paused folder's database is stopped: Syncthing says "no such folder")
                    return []
                if fid not in listings:
                    try:
                        listings[fid] = tried("a folder's top-level entries", lambda: c.browse(fid))
                    except SyncthingError:
                        listings[fid] = None
                return listings[fid] or []
            chosen = (s.get("folders") or {})  # (a folder chosen by hand for a library: over whatever is found)
            for key, places in self._library_paths().items():
                for local, cands in places:
                    # in order: a path the two share (Syncthing's, or the host's through the mounts); the folder whose own top-level
                    # listing names the library folder (a mount under another name, as /plexfilesSyncthing: what's after Plex's mount
                    # point is the folder); last, a folder named or labelled for it; and a folder chosen by hand over all of these
                    f, rel = covering(folders, cands, names=False)
                    how = "path"
                    name = os.path.basename(os.path.normpath(local))
                    if f is None:
                        f = next((row for row in folders if name and name in listed(row["id"])), None)
                        rel, how = name, "listing"
                    if f is None:
                        f, rel = covering(folders, cands)
                        how = "name"
                    pick = byid.get((chosen.get(key) or {}).get("folder") or "")
                    if pick is not None:
                        f, rel, how = pick, (rel if f is pick and rel else name), "chosen"
                    if f is not None:
                        got["libraries"].setdefault(key, []).append({"folder": f["id"], "rel": rel, "local": local, "how": how})
                        if key not in byid[f["id"]]["libraries"]:
                            byid[f["id"]]["libraries"].append(key)
            for row in folders:  # (each folder's state and ignore patterns: the migration's folder needn't be a mirrored library's)
                try:
                    ds = tried("a folder's state", lambda row=row: c.folder_status(row["id"])) or {}
                    row.update(state=ds.get("state"), need_files=ds.get("needFiles"), need_bytes=ds.get("needBytes"))
                except SyncthingError as e:
                    row.update(state="error", error=e.why)
                try:
                    row["ignores"] = tried("a folder's ignore patterns", lambda row=row: c.ignores(row["id"]))
                except SyncthingError:
                    row["ignores"] = None
                row["need"], row["remote_need"] = [], {}
                if row["libraries"] and not row["paused"]:  # (what's on its way, in and out, for the folders over our libraries; a paused folder's database is stopped: nothing to ask)
                    try:
                        row["need"] = tried("what's still to come", lambda row=row: c.need(row["id"])) or []
                    except SyncthingError:
                        pass
                    for d in row["devices"]:
                        if d["connected"]:
                            try:
                                row["remote_need"][d["id"]] = tried("what a device still needs", lambda row=row, d=d: c.remote_need(row["id"], d["id"])) or []
                            except SyncthingError:
                                pass
            # the calls that change something, tried with nothing to change (an empty change answers 200 where the call exists);
            # a scan isn't tried, since it would start one: known when the folder calls are
            if folders:
                tried("changing a folder's settings", lambda: c.patch_folder(folders[0]["id"], {}))
            if my:
                tried("changing a device's settings", lambda: c.patch_device(my, {}))
            api["asking for a scan"] = api.get("a folder's state")
            for name in CALLS:
                api.setdefault(name, None)
        except SyncthingError as e:
            got.update(error=e.why, code=e.code)
        with self.lock:
            self.status = got
            self.asked_at = time.time()
            self._stamp = hashlib.sha256(json.dumps(self._findings_input(got), sort_keys=True, default=str).encode()).hexdigest()[:16]
        return got

    @staticmethod
    def _findings_input(got: dict) -> dict:
        """What the findings are worked out from (the stamp): a change here is a new Library Health answer."""
        return {"ok": got.get("ok"), "error": str(got.get("error") or ""), "libraries": got.get("libraries"), "api": got.get("api"), "device": got.get("device"),
                "folders": [{k: f.get(k) for k in ("id", "label", "paused", "order", "watched", "rescan", "devices", "ignores", "need", "remote_need")} for f in got.get("folders") or []]}

    def stamp(self, key: str) -> tuple:
        """For App._health_sig, through mirrorhealth.stamp: the findings' inputs, and the fixes asked for this library."""
        return (self.wanted(), self._stamp, tuple(sorted(v for k, v in self.acted.items() if k.startswith(str(key) + "/"))))

    # ------------------------------------------------------------ the folders, for the migration
    def folder_over(self, local_folder: str) -> tuple[dict | None, str]:
        """The folder in Syncthing over a library folder here (from the last answer), and where the folder is within it."""
        if not self.talking():
            return None, ""
        cands: list = [local_folder]
        for m in (self.app.settings.get().get("general") or {}).get("path_map") or []:
            if local_folder == m["local"] or local_folder.startswith(m["local"].rstrip("/") + "/"):
                cands.append(m["plex"].rstrip("/") + local_folder[len(m["local"].rstrip("/")):])
        try:
            cands.append(zfs.host_path(local_folder))
        except Exception:  # noqa: BLE001
            pass
        return covering(self.status.get("folders") or [], cands)

    def pause(self, folder: str, paused: bool) -> None:
        self.client().patch_folder(folder, {"paused": paused})
        self.app.activity.event("sync", M("Syncthing asked to pause the folder {folder}", folder=folder) if paused else M("Syncthing asked to resume the folder {folder}", folder=folder))
        self.refresh()

    def add_ignores(self, folder: str, lines: list[str]) -> list[str]:
        """The lines not among the folder's ignore patterns added to them (the API's; Syncthing writes its .stignore). What
        was added."""
        c = self.client()
        have = c.ignores(folder)
        new = [ln for ln in lines if ln not in have]
        if new:
            c.set_ignores(folder, have + new)
            self.app.activity.event("sync", M("Syncthing’s ignore patterns for {folder} given {lines}", folder=folder, lines=", ".join(new)))
            self.refresh()
        return new

    # ------------------------------------------------------------ what Mirror Checks show (mirrorhealth.findings, notes)
    def findings(self, key: str) -> list[dict]:
        """The library's findings, Library Health's way ([{"item", "label", "poster", "result"}]): none while Syncthing
        isn't set up; not answering; no folder over the library; per folder over it, paused, not watched, not smallest
        first, .plexcache ignored, none of its devices connected."""
        if not self.wanted():
            return []
        key, st, out = str(key), dict(self.status), []

        def finding(item: str, label, cid: str, text, lines=None, action=None):
            res: dict = {"id": cid, "category": "mirror", "status": "warn", "text": text, "file": "", "lines": lines or []}
            if action:
                res["action"] = {"label": action[0], "tip": action[1], "done_at": self.acted.get(key + "/" + item + "/" + cid)}
            out.append({"item": item, "label": label, "poster": "", "result": res})

        if not st or not st.get("ok"):
            finding(ITEM, M("Syncthing"), "mirror_syncthing_down", M("Syncthing isn’t answering: {why}", why=st.get("error") or M("not asked yet.")))
            return out
        covers = (st.get("libraries") or {}).get(key) or []
        if not covers:
            finding(ITEM, M("Syncthing"), "mirror_syncthing_no_folder",
                    M("No folder in Syncthing covers this library’s folder, so nothing carries it to the other site. Its folders: {folders}.",
                      folders=", ".join(f"{f['label']} ({f['path']})" for f in st.get("folders") or []) or M("none")))
            return out
        seen = set()
        for e in covers:
            f = next((r for r in st.get("folders") or [] if r["id"] == e["folder"]), None)
            if f is None or f["id"] in seen:
                continue
            seen.add(f["id"])
            item, label, mark = ITEM + ":" + f["id"], M("Syncthing folder “{label}”", label=f["label"]), mark_in(e["rel"])
            if f["paused"]:
                finding(item, label, "mirror_syncthing_paused", M("Paused in Syncthing: nothing moves between the sites until it’s resumed."),
                        action=(M("Resume Folder"), M("Resumes the folder in Syncthing on this server.")))
            if not f["watched"]:
                finding(item, label, "mirror_syncthing_not_watched",
                        M("Syncthing isn’t watching this folder for changes: a new file waits for its next scan (every {mins} minutes).", mins=str(round((f.get("rescan") or 3600) / 60))),
                        action=(M("Watch for Changes"), M("Turns Watch for Changes on for the folder in Syncthing.")))
            if f["order"] != "smallestFirst":
                finding(item, label, "mirror_syncthing_order",
                        M("Syncthing sends this folder’s files in {order} order: the small marker and claim files in .plexcache arrive after the big files they’re about, and the two sites can optimize the same file.", order=ORDERS.get(f["order"], f["order"])),
                        action=(M("Smallest First"), M("Sets the folder’s pull order to Smallest First in Syncthing.")))
            if f.get("ignores") and ignored_by(f["ignores"], mark):
                finding(item, label, "mirror_syncthing_ignores",
                        M("Syncthing’s ignore patterns for this folder leave out this library’s .plexcache, so the markers and claims don’t reach the other site. Remove the pattern in Syncthing (Edit → Ignore Patterns):"),
                        lines=[ln for ln in f["ignores"] if ignored_by([ln], mark)])
            if f["devices"] and not any(d["connected"] for d in f["devices"]):
                finding(item, label, "mirror_syncthing_disconnected",
                        M("None of the devices this folder is shared with is connected ({names}): nothing reaches the other site.", names=", ".join(d["name"] for d in f["devices"])))
            other = self.other_identity(key)
            if other and other.get("source") == "syncthing" and f["devices"] and other["id"] not in {d["id"] for d in f["devices"]}:
                finding(item, label, "mirror_syncthing_other_device",
                        M("The Dionysus at the mirror’s site says its Syncthing is {short} ({name}), but this folder isn’t shared with that device: the two Dionysus instances talk, their Syncthings don’t.",
                          short=other.get("short") or "", name=other.get("name") or ""))
        # the files on their way, in or out, each on its Plex item (or its own): read as "not yet the same at both sites"
        for item, label, poster, lines in self.transit_items(key):
            res = {"id": "mirror_syncthing_transit", "category": "mirror", "status": "warn", "text": lines[0], "file": "", "lines": lines[1:]}
            out.append({"item": item, "label": label, "poster": poster, "result": res})
        return out

    def transit(self, key: str) -> dict:
        """The library's files on their way through Syncthing: {"in": {path in the library: the folder's label}, "out": {path: [device
        names]}}, paths relative to the library folder."""
        st, out = dict(self.status), {"in": {}, "out": {}}
        for e in (st.get("libraries") or {}).get(str(key)) or []:
            f = next((r for r in st.get("folders") or [] if r["id"] == e["folder"]), None)
            if f is None:
                continue
            prefix = (e["rel"].strip("/") + "/") if e["rel"] and e["rel"].strip("/") else ""
            within = lambda n, prefix=prefix: n[len(prefix):] if n.startswith(prefix) else None  # noqa: E731
            for n in f.get("need") or []:
                p = within(n)
                if p:
                    out["in"][p] = f["label"]
            names = {d["id"]: d["name"] for d in f.get("devices") or []}
            for did, ns in (f.get("remote_need") or {}).items():
                for n in ns:
                    p = within(n)
                    if p:
                        out["out"].setdefault(p, []).append(names.get(did, did[:7]))
        return out

    def transit_items(self, key: str) -> list[tuple]:
        """The files on their way as Library Health's items: [(item, label, poster, [lines])] — a Plex record's where the data layer
        knows the file, else an item of its own named for the file."""
        tr = self.transit(key)
        if not tr["in"] and not tr["out"]:
            return []
        known: dict[str, tuple] = {}
        try:
            d = self.app.libdata.data(str(key))
            for f in d.files:
                root, local = str(f.get("root") or ""), str(f.get("local") or "")
                if root and local.startswith(root.rstrip("/") + "/"):
                    known[local[len(root.rstrip("/")) + 1:]] = (str(f.get("rk") or ""), f.get("title") or "", f.get("poster") or "")
        except Exception:  # noqa: BLE001 — without the data layer, every file is an item of its own
            pass
        by_item: dict[str, dict] = {}
        for p, label in tr["in"].items():
            rk, title, poster = known.get(p) or ("", "", "")
            it = by_item.setdefault(rk or "transit:" + p, {"label": title or os.path.basename(p), "poster": poster, "lines": []})
            it["lines"].append(M("On its way here through Syncthing ({folder}): {file}", folder=label, file=os.path.basename(p)))
        for p, devs in tr["out"].items():
            rk, title, poster = known.get(p) or ("", "", "")
            it = by_item.setdefault(rk or "transit:" + p, {"label": title or os.path.basename(p), "poster": poster, "lines": []})
            it["lines"].append(M("Still being sent to {devices} through Syncthing: {file}", devices=", ".join(sorted(set(devs))), file=os.path.basename(p)))
        return [(item, v["label"], v["poster"], [M("Not yet the same at both sites: Syncthing is still carrying it. The tools leave it until it has arrived.")] + v["lines"]) for item, v in by_item.items()]

    def blocks(self, key: str) -> dict[str, list[dict]]:
        """What stops Sync Libraries' row on an item (mirrorhealth.blocks adds these): its file still on its way through Syncthing."""
        return {item: [{"key": item, "id": "mirror_syncthing_transit", "title": str(label), "text": lines[0]}]
                for item, label, _poster, lines in self.transit_items(key) if not item.startswith("transit:")}

    # ------------------------------------------------------------ identity: Syncthing's device ID as this server's
    def identity(self) -> dict | None:
        """This server as Syncthing names it, when Syncthing is here: {"id" (the device ID), "short" (its first group), "source"
        ("syncthing": from its API; "certificate": read off its sync port, no key needed), "name" (the device's name)}. None
        without one (peerlink.own_identity falls back to this app's own key)."""
        dev = self.status.get("device") or {}
        if dev.get("id"):
            return {"id": dev["id"], "short": dev["id"].split("-")[0], "source": "syncthing", "name": dev.get("name") or ""}
        cid = self.cert_identity()
        if cid:
            return {"id": cid, "short": cid.split("-")[0], "source": "certificate", "name": ""}
        return None

    def cert_identity(self) -> str | None:
        """The device ID from the certificate on the sync port (SYNC_PORT, or the port TrueNAS publishes for the app), at the
        address set or the hosts a Syncthing is looked for on; kept an hour."""
        at, got = self._cert_id
        if time.time() - at < 3600:
            return got
        hosts: list[str] = []
        for u in [self.settings().get("url") or ""] + self.candidates():
            h = _host_of(u)
            if h and h not in hosts:
                hosts.append(h)
        port = self._published_port(SYNC_PORT) or SYNC_PORT
        got = next((cid for h in hosts for cid in [cert_device_id(h, port)] if cid), None)
        with self.lock:
            self._cert_id = (time.time(), got)
        return got

    def _published_port(self, inside: int) -> int | None:
        """The host port TrueNAS publishes for the Syncthing app's port ``inside`` (app.query's used_ports), if it can be asked."""
        s = self.app.settings.get()
        if not (s.get("truenas") or {}).get("api_key"):
            return None
        try:
            with self.app.truenas() as tn:
                apps = tn.apps()
            for a in apps:
                if "syncthing" not in str(a.get("name") or "").lower():
                    continue
                for up in (a.get("active_workloads") or {}).get("used_ports") or []:
                    if int(up.get("container_port") or 0) == inside:
                        return next((int(h["host_port"]) for h in up.get("host_ports") or [] if h.get("host_port")), None)
        except Exception:  # noqa: BLE001
            return None
        return None

    def other_identity(self, key: str) -> dict | None:
        """The Dionysus at the mirror's site, as it names itself over the link (its hello and status carry its identity)."""
        links = getattr(self.app, "peerlinks", None)
        link = links.for_library(key) if links is not None else None
        return link.other_identity() if link is not None else None

    def device_hosts(self, did: str) -> list[str]:
        """Where the mirror behind a device may be reached: the hosts of the link whose Dionysus names this device as its
        Syncthing, else of any link at whose site the device is connected from — for an address to give the device."""
        links = getattr(self.app, "peerlinks", None)
        out: list[str] = []
        for lk in (links.used() if links is not None else []):
            hosts = [h for h in [_host_of(lk.url_setting()), _host_of((lk.found or {}).get("url") or "")] if h]
            try:
                hosts += [h for h in lk._mirror_hosts() if h]
            except Exception:  # noqa: BLE001
                pass
            other = lk.other_identity() or {}
            dev = next((d for f in self.status.get("folders") or [] for d in f.get("devices") or [] if d["id"] == did), None)
            here = (dev or {}).get("address", "").rsplit(":", 1)[0].strip("[]")
            if other.get("id") == did or (here and here in hosts):
                for h in hosts:
                    if h not in out:
                        out.append(h)
        return out

    def reach_light(self) -> dict:
        """How each mirror device is reached: direct (ok), through a relay (orange: an address to give it, when one is known),
        not findable (orange: dynamic address with global discovery off). {"status", "text", "detail", "fix": {device: host}}."""
        st = dict(self.status)
        devices = {d["id"]: d for f in st.get("folders") or [] if f.get("libraries") for d in f.get("devices") or []}
        if not st.get("ok") or not devices:
            return {"status": "off", "text": M("Reach"), "detail": M("How each mirror device is reached, once the folders over the libraries are known.")}
        opts = st.get("options") or {}
        said, fix, bad = [], {}, False
        for did, d in devices.items():
            kind = str(d.get("type") or "")
            hosts = self.device_hosts(did)
            if d.get("connected") and "relay" in kind:
                bad = True
                said.append(M("{name} is reached through a relay (slow, and someone else’s): give it an address of its own.", name=d["name"]))
                if hosts:
                    fix[did] = hosts[0]
            elif d.get("connected"):
                said.append(M("{name}: direct, {kind}.", name=d["name"], kind=kind or "tcp"))
            elif d.get("addresses") in ([], ["dynamic"]) and not opts.get("globalAnnounceEnabled", True):
                bad = True
                said.append(M("{name} has no address of its own and global discovery is off, so it can’t be found.", name=d["name"]))
                if hosts:
                    fix[did] = hosts[0]
            else:
                said.append(M("{name}: not connected now.", name=d["name"]))
        notes = []
        if not opts.get("localAnnounceEnabled", True):
            notes.append(M("Local discovery is off: devices on your LAN are found by their addresses only."))
        if not opts.get("relaysEnabled", True):
            notes.append(M("Relays are off: no fallback when a direct connection fails."))
        detail = " ".join(str(x) for x in said + notes)
        if fix:
            detail += " " + str(M("Add Its Address sets tcp://{host}:{port} on the device, keeping what it has; Syncthing then tries that first.", host=next(iter(fix.values())), port=str(SYNC_PORT)))
        return {"status": "warn" if bad else "ok", "text": M("{n} mirror devices reached through a relay or not findable", one="{n} mirror device reached through a relay or not findable", n=sum(1 for d in devices.values() if ("relay" in str(d.get("type") or "") and d.get("connected")) or (not d.get("connected") and d.get("addresses") in ([], ["dynamic"]) and not opts.get("globalAnnounceEnabled", True)))) if bad
                else M("Every mirror device is reached directly") if all(d.get("connected") for d in devices.values()) else M("Mirror devices: direct where connected"),
                "detail": detail, "fix": fix}

    def add_device_address(self, did: str) -> str:
        """The mirror's host, as the link knows it, given to the device as tcp://host:22000 (what it has kept): Syncthing tries it first."""
        hosts = self.device_hosts(did)
        if not hosts:
            raise ValueError(M("No address is known for that device: pair the Dionysus at its site first, or set its address in Syncthing."))
        dev = next((d for f in self.status.get("folders") or [] for d in f.get("devices") or [] if d["id"] == did), None)
        have = list((dev or {}).get("addresses") or ["dynamic"])
        addr = f"tcp://{hosts[0]}:{SYNC_PORT}"
        if addr not in have:
            try:
                self.client().patch_device(did, {"addresses": [addr] + have})
            except SyncthingError as e:
                raise ValueError(e.why) from None
            self.app.activity.event("sync", M("Syncthing device {name} given the address {addr}", name=(dev or {}).get("name") or did[:7], addr=addr))
            self.refresh()
        return addr

    def device_at(self, hosts) -> dict | None:
        """The device our Syncthing is connected to at one of ``hosts`` (its connection's address): {"id", "short", "source":
        "syncthing-device", "name"} — how the mirror's Syncthing is known when its Dionysus doesn't say (an older build)."""
        want = {h for h in hosts if h}
        if not want:
            return None
        for f in self.status.get("folders") or []:
            for d in f.get("devices") or []:
                host = d.get("address", "").rsplit(":", 1)[0].strip("[]")
                if d.get("connected") and host in want:
                    return {"id": d["id"], "short": d["id"].split("-")[0], "source": "syncthing-device", "name": d.get("name") or ""}
        return None

    # ------------------------------------------------------------ where paths are on the host (TrueNAS's mounts of the apps)
    def mounts(self) -> dict:
        """The volume mounts TrueNAS gives the Syncthing app and this one, from their YAML (app.query with the config):
        {"syncthing": [(host path, path in its container)], "mine": [...]}, kept an hour. Empty without TrueNAS."""
        at, got = self._mounts
        if time.time() - at < 3600:
            return got
        got = {"syncthing": [], "mine": []}
        s = self.app.settings.get()
        if (s.get("truenas") or {}).get("api_key"):
            try:
                me = zfs.own_container_id() or ""
                with self.app.truenas() as tn:
                    apps = tn.apps(config=True)
                for a in apps:
                    conts = (a.get("active_workloads") or {}).get("container_details") or []
                    mine = bool(me) and any(str(c.get("id") or "").startswith(me[:12]) for c in conts)
                    which = "mine" if mine else "syncthing" if "syncthing" in str(a.get("name") or "").lower() else None
                    if not which:
                        continue
                    for svc in ((a.get("config") or {}).get("services") or {}).values():
                        for v in (svc or {}).get("volumes") or []:
                            if isinstance(v, dict) and v.get("source") and v.get("target"):
                                got[which].append((str(v["source"]), str(v["target"])))
                            elif isinstance(v, str) and ":" in v:
                                src, dst = v.split(":")[:2]
                                got[which].append((src, dst))
            except Exception:  # noqa: BLE001 — no TrueNAS, no permission: the paths are compared as they are
                pass
        with self.lock:
            self._mounts = (time.time(), got)
        return got

    def host_of(self, path: str, which: str) -> str | None:
        """Where a path inside an app's container (``which``: "syncthing" or "mine") is on the host, by its mounts; None when no
        mount holds it."""
        best = None
        for src, dst in self.mounts().get(which) or []:
            d = dst.rstrip("/") or "/"
            if path == d or path.startswith(d + "/"):
                if best is None or len(d) > len(best[1]):
                    best = (src, d)
        if best is None:
            return None
        return best[0].rstrip("/") + path[len(best[1]):] if best[1] != "/" else best[0].rstrip("/") + path

    def rescan(self, folder: str, sub: str = "") -> None:
        """Syncthing asked to scan a folder (or a path within it) now, rather than at its next interval."""
        self.client().scan(folder, sub)
        self.app.activity.event("sync", M("Syncthing asked to scan {what}", what=(folder + "/" + sub.strip("/")) if sub else folder))

    def notes(self, key: str) -> list[dict]:
        """What the Mirror Checks page says above the findings for this library: Syncthing not set up (its checks need
        it), or files still on their way here."""
        if not self.wanted():
            return [{"status": "info", "text": M("Syncthing isn’t connected: whether it runs, covers this library and sends the small files first is checked once it is ([System → Syncthing](/system#syncthing)).")}]
        st, out = dict(self.status), []
        for e in (st.get("libraries") or {}).get(str(key)) or []:
            f = next((r for r in st.get("folders") or [] if r["id"] == e["folder"]), None)
            if f and f.get("need_files"):
                from .i18n import size_text
                out.append({"status": "info", "text": M("Syncthing: {n} files still to come here into “{label}” ({size}).", one="Syncthing: {n} file still to come here into “{label}” ({size}).",
                                                        n=f["need_files"], label=f["label"], size=size_text(f.get("need_bytes") or 0))})
        return out

    def act(self, key: str, item: str, check: str) -> M:
        """A finding's fix, through the API (FIXES): the folder resumed, watched, or sending the smallest files first."""
        fid = item.split(":", 1)[1] if item.startswith(ITEM + ":") else ""
        if check not in FIXES or not fid:
            raise ValueError(M("That finding has no fix from here: see to it in Syncthing."))
        name, value, said = FIXES[check]
        try:
            self.client().patch_folder(fid, {name: value})
        except SyncthingError as e:
            raise ValueError(e.why) from None
        with self.lock:
            self.acted[str(key) + "/" + item + "/" + check] = time.time()
        self.app.activity.event("sync", said)
        self.refresh()
        return said

    # ------------------------------------------------------------ finding one, the calls it knows
    def candidates(self) -> list[str]:
        """Where a Syncthing may answer on this network, without being told: the web port TrueNAS publishes for an app named
        for it (app.query), and port 8384 on the TrueNAS and Plex hosts."""
        s = self.app.settings.get()
        out: list[str] = []
        tn_host = _host_of((s.get("truenas") or {}).get("url") or "")
        if tn_host and (s.get("truenas") or {}).get("api_key"):
            try:
                with self.app.truenas() as tn:
                    apps = tn.apps()
                for a in apps:
                    if "syncthing" not in str(a.get("name") or "").lower():
                        continue
                    wl = a.get("active_workloads") or {}
                    ports = [int(h["host_port"]) for up in wl.get("used_ports") or [] if int(up.get("container_port") or 0) == DEFAULT_PORT
                             for h in up.get("host_ports") or [] if h.get("host_port")]
                    for p in ports:
                        out.append(f"http://{tn_host}:{p}")
            except Exception:  # noqa: BLE001 — no TrueNAS, no permission: the other ways
                pass
        for host in (tn_host, _host_of((s.get("plex") or {}).get("url") or "")):
            if host and host not in ("127.0.0.1", "localhost", "::1"):  # (inside this app, that's the app itself)
                out.append(f"http://{host}:{DEFAULT_PORT}")
        seen: list[str] = []
        for u in out:
            if u not in seen:
                seen.append(u)
        return seen

    def discover(self, force: bool = False) -> str | None:
        """A Syncthing found on this network: the first candidate whose health endpoint answers. Looked at most hourly;
        the look kept (found) for the Found light."""
        if not force and self.found.get("at") and time.time() - self.found["at"] < DISCOVER_EVERY:
            return self.found.get("url")
        tried = self.candidates()
        url = next((u for u in tried if SyncthingClient(u, "", timeout=3).healthy()), None)
        with self.lock:
            self.found = {"at": time.time(), "url": url, "tried": tried}
        return url

    @staticmethod
    def api_compatibility(api: dict, version: str) -> dict:
        """The API compatibility light (integrations.api_light, the words every service uses): every call the app makes (CALLS)
        against what this Syncthing answered."""
        from .integrations import api_light
        return api_light([f"{w} ({CALLS[w][0]} {CALLS[w][1]})" for w, ok in api.items() if ok is False], [w for w in CALLS if api.get(w) is None], len(CALLS), version, "paths")

    # ------------------------------------------------------------ the throttle: the disks, and the internet
    def throttle_tick(self, force_release: bool = False) -> None:
        """Every half minute (App.run), Syncthing's devices held to a rate for two reasons, each with its own setting, the
        lower rate winning where both apply (Syncthing's per-device limits, their own kept to put back):

        * **the disks** ("throttle"): while someone watches and the media pool's disks have been busier than the setting for
          HOT_HOLD seconds, every device the mirrored folders are shared with, both ways; let go once nobody watches or the
          disks have been calmer than the lower setting for CALM_HOLD seconds;
        * **the internet** ("giveway"): while a stream plays to a player Plex calls remote (it leaves over your connection),
          the devices reached over the internet (not on this NAS's own networks), sending — and receiving too with
          "giveway_both" — held to your upload less the streams' bandwidth and a fifth of headroom (a fixed floor when the
          upload isn't known); let go NET_GRACE seconds after the last remote stream ends, so a pause doesn't flap it.

        Pausing a folder would also stop the small .plexcache files, and a rate limit is Syncthing's own, applied at once,
        so that's the lever. What's held is kept on disk (THROTTLE_FILE), so a restart lets go of it too."""
        s = self.settings()
        now = time.time()
        mon = getattr(self.app, "monitor", None)
        sessions = list((getattr(mon, "sessions", None) or {}).values())
        playing = len(sessions)
        busy = getattr(mon, "disk_busy", None) if playing else None
        remote = [x for x in sessions if x.get("local") is False]
        remote_kbps = sum(int(x.get("kbps") or 0) for x in remote)
        self.reading = {"at": now, "playing": playing, "busy": busy, "remote": len(remote), "remote_kbps": remote_kbps}
        if force_release or not self.wanted() or not (s.get("throttle") or s.get("giveway")):
            self._hot_since = self._calm_since = self._net_last = None
            self._apply({}, [])
            return
        # the disks: latched after HOT_HOLD over the mark, released after CALM_HOLD under the lower one or when nobody watches
        if s.get("throttle"):
            if playing and busy is not None and busy >= s["throttle_busy"]:
                self._hot_since, self._calm_since = self._hot_since or now, None
            elif not playing or busy is None or busy < s["throttle_calm"]:
                self._calm_since, self._hot_since = self._calm_since or now, None
            if not self._disk_on and self._hot_since and now - self._hot_since >= HOT_HOLD:
                self._disk_on = True
            elif self._disk_on and (not playing or (self._calm_since and now - self._calm_since >= CALM_HOLD)):
                self._disk_on = False
        else:
            self._disk_on = False
        # the internet: on at once with a remote stream, off NET_GRACE after the last
        if s.get("giveway") and remote:
            self._net_last = now
        net_on = bool(s.get("giveway")) and self._net_last is not None and now - self._net_last < NET_GRACE
        target: dict[str, dict] = {}
        reasons: list = []
        if self._disk_on and self.talking():
            for did, d in self._devices().items():
                target[did] = {"send": int(s["throttle_kbps"]), "recv": int(s["throttle_kbps"]), "name": d["name"]}
            reasons.append(M("{n} streams playing and the disks {pct}% busy", one="{n} stream playing and the disks {pct}% busy", n=playing, pct=round(busy or 0)))
        if net_on and self.talking():
            rate = self.net_rate(s, remote_kbps)
            for did, d in self._devices().items():
                if self.is_lan(d.get("address") or ""):
                    continue
                t = target.setdefault(did, {"send": None, "recv": None, "name": d["name"]})
                t["send"] = min(rate, t["send"]) if t["send"] else rate
                if s.get("giveway_both"):
                    t["recv"] = min(rate, t["recv"]) if t["recv"] else rate
            if remote:
                reasons.append(M("{n} remote streams at {mbps} Mbit/s", one="{n} remote stream at {mbps} Mbit/s", n=len(remote), mbps=f"{remote_kbps / 1000:.1f}")
                               + ((" " + M("of your {up} Mbit/s up", up=str(s["giveway_up_mbps"]))) if s.get("giveway_up_mbps") else ""))
            else:
                reasons.append(M("a remote stream ended a moment ago"))
        self._apply(target, reasons)

    def net_rate(self, s: dict, remote_kbps: int) -> int:
        """What Syncthing may send over the internet while remote streams play (KB/s): the upload less the streams and a fifth
        of headroom, never under the floor; the floor alone when the upload isn't known."""
        floor = int(s.get("giveway_floor_kbps") or 500)
        up = int(s.get("giveway_up_mbps") or 0)
        if not up:
            return floor
        return max(floor, int(up * 125 - remote_kbps / 8 * 1.2))

    def own_networks(self) -> list:
        """This NAS's own networks (/24 around each address it's reached at, loopback and the tailnet's range left out): a device
        connected from one of these is on the LAN."""
        import ipaddress
        out = []
        links = getattr(self.app, "peerlinks", None)
        for h in (links.own_addresses() if links is not None else []):
            try:
                ip = ipaddress.ip_address(h)
            except ValueError:
                continue
            if ip.version != 4 or ip.is_loopback or ip in TAILNET:
                continue
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
            if net not in out:
                out.append(net)
        return out

    def is_lan(self, address: str) -> bool:
        """Whether a device's connection address is on one of this NAS's own networks (else it's reached over the internet,
        a VPN or the tailnet: what remote streams share)."""
        import ipaddress
        host = address.rsplit(":", 1)[0].strip("[]") if address else ""
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(ip in net for net in self.own_networks())

    def _devices(self) -> dict[str, dict]:
        """The devices the folders over mirrored libraries are shared with, by id."""
        return {d["id"]: d for f in self.status.get("folders") or [] if f.get("libraries") for d in f.get("devices") or []}

    def _apply(self, target: dict[str, dict], reasons: list) -> None:
        """The devices held as ``target`` says ({id: {"send", "recv" (None: its own), "name"}}) and no other: those newly held
        or at another rate set through the API (their own limits kept the first time), those no longer held put back."""
        held = dict(self.throttled or {})
        devices = dict(held.get("devices") or {})
        changed = False
        for did, want in target.items():
            prev = devices.get(did)
            d = self._devices().get(did) or {}
            own = prev["before"] if prev else {"maxRecvKbps": d.get("max_recv") or 0, "maxSendKbps": d.get("max_send") or 0}
            send = want["send"] if want["send"] else own["maxSendKbps"]
            recv = want["recv"] if want["recv"] else own["maxRecvKbps"]
            if prev and prev.get("send") == send and prev.get("recv") == recv:
                continue
            try:
                self.client().patch_device(did, {"maxRecvKbps": recv, "maxSendKbps": send})
                devices[did] = {"name": want["name"], "before": own, "send": send, "recv": recv}
                changed = True
            except SyncthingError as e:
                log.warning("syncthing: couldn't hold %s back: %s", want["name"], e.why)
        for did in list(devices):
            if did in target:
                continue
            prev = devices[did]
            try:
                self.client().patch_device(did, {"maxRecvKbps": prev["before"].get("maxRecvKbps") or 0, "maxSendKbps": prev["before"].get("maxSendKbps") or 0})
                del devices[did]
                changed = True
            except SyncthingError as e:  # (kept, so the next tick tries again)
                log.warning("syncthing: couldn't let %s go: %s", prev.get("name"), e.why)
        if not changed:
            return
        if devices:
            rate = min(v["send"] for v in devices.values())
            why = M("{reasons}", reasons="; ".join(str(r) for r in reasons)) if reasons else M("let go of some")
            with self.lock:
                self.throttled = {"since": held.get("since") or time.time(), "kbps": rate, "why": why, "reasons": [str(r) for r in reasons], "devices": devices}
            self.app.activity.event("sync", M("Syncthing held to {kbps} KB/s with {names}: {why}", kbps=str(rate), names=", ".join(v["name"] for v in devices.values()), why=why))
        else:
            with self.lock:
                self.throttled = None
            self.app.activity.event("sync", M("Syncthing let go ({names} back to their own limits): {why}", names=", ".join(v.get("name") or k for k, v in (held.get("devices") or {}).items()),
                                             why=reasons[0] if reasons else (M("nobody is watching") if not self.reading.get("playing") else M("the disks are calm and no remote stream plays"))))
        self._save_throttle()

    def _save_throttle(self) -> None:
        save = getattr(self.app, "_save_json", None)
        if save:
            save(THROTTLE_FILE, self.throttled or {})

    def throttle_light(self) -> dict:
        s, r = self.settings(), self.reading
        if not s.get("throttle") and not s.get("giveway"):
            return {"status": "off", "text": M("Not held back for the disks or the internet"), "detail": M("Tick a box below to hold Syncthing to a rate while something plays and the media pool’s disks are busy, or while a stream plays to a remote player.")}
        if self.throttled:
            return {"status": "warn", "text": M("Syncthing held to {kbps} KB/s", kbps=str(self.throttled.get("kbps"))),
                    "detail": M("Since {when}: {why}. Let go once the reason passes — the disks under {calm}% busy for a minute, the last remote stream over for a minute.",
                                when=time.strftime("%H:%M", time.localtime(self.throttled.get("since") or 0)), why=self.throttled.get("why") or "", calm=str(s["throttle_calm"]))}
        what = []
        if s.get("throttle"):
            what.append(M("held to {kbps} KB/s once something plays and the disks pass {busy}% busy", kbps=str(s["throttle_kbps"]), busy=str(s["throttle_busy"])))
        if s.get("giveway"):
            what.append(M("the devices over the internet held to {rate} KB/s while a remote stream plays", rate=str(self.net_rate(s, int(r.get("remote_kbps") or 0)))))
        detail = M("{what}.", what="; ".join(str(w) for w in what))
        if not r.get("playing"):
            return {"status": "ok", "text": M("Watching: nobody is playing"), "detail": detail}
        return {"status": "ok", "text": M("Watching: {n} playing ({remote} remote), disks {pct}% busy", n=r["playing"], remote=r.get("remote") or 0, pct=round(r["busy"] or 0)), "detail": detail}

    # ------------------------------------------------------------ the System card
    def lights(self) -> dict:
        """The card's six lights, TrueNAS's card's way: {"found", "server", "key", "api", "folders", "devices"}: {"status", "text",
        "detail"} — where one was looked for and found (found), whether the address answers (server), the key (key), every call
        the app makes known to this release (api), a folder over each mirrored library (folders), their devices (devices)."""
        st = dict(self.status)
        off = lambda text, detail="": {"status": "off", "text": text, "detail": detail}  # noqa: E731
        rest = {"key": off(M("API key")), "api": off(M("API compatibility")), "folders": off(M("Folders")), "devices": off(M("Devices")), "reach": off(M("Reach")), "throttle": off(M("Throttle"))}
        if not self.wanted():
            looked = self.found.get("at")
            return dict({"found": {"status": "warn" if looked else "off", "text": M("Syncthing not found on this network") if looked else M("Syncthing"),
                                   "detail": M("Looked at TrueNAS’s apps and on port 8384 of the TrueNAS and Plex hosts ({tried}). Enter its address below when it runs elsewhere.", tried=", ".join(self.found.get("tried") or []) or M("nowhere yet"))
                                   if looked else M("Looked for by itself: among TrueNAS’s apps, and on port 8384 of the TrueNAS and Plex hosts.")},
                         "server": {"status": "warn", "text": M("Syncthing not set up (optional)"),
                                    "detail": M("Without it, Mirror Checks can’t see that the folder sync runs and is set up well, and Convert Folder to Child Dataset asks you to pause it and add its ignore patterns yourself.")}}, **rest)
        found = {"status": "ok", "text": M("Syncthing found at {url} by itself", url=self.found["url"]), "detail": M("Its address was filled in from the look; the API key is yours to paste.")} \
            if self.found.get("url") == self.settings().get("url") else {"status": "ok", "text": M("Syncthing’s address set by hand"), "detail": self.settings().get("url") or ""}
        if not st:
            return dict({"found": found, "server": off(M("Syncthing"), M("Not asked yet"))}, **rest)
        if not st.get("ok"):
            if st.get("code") == "nokey":
                return dict(rest, found=found,
                            server={"status": "ok", "text": M("Syncthing answers"), "detail": st["url"]} if st.get("health") else {"status": "fail", "text": M("Syncthing not answering"), "detail": M("Nothing answered at {url}.", url=st["url"])},
                            key={"status": "warn", "text": M("Syncthing API key needed"), "detail": M("Paste its key below: in Syncthing, Actions → Settings → General → API Key. It only reads until you press a button here.")},
                            folders=off(M("Folders"), M("Read once the key is in.")))
            if st.get("code") == "auth":
                return dict(rest, found=found, server={"status": "ok", "text": M("Syncthing answers"), "detail": st["url"]},
                            key={"status": "fail", "text": M("Syncthing API key not accepted"), "detail": st.get("error") or ""},
                            folders=off(M("Folders"), M("Read once the key is accepted.")))
            return dict(rest, found=found, server={"status": "fail", "text": M("Syncthing not answering"), "detail": st.get("error") or ""},
                        key=off(M("API key: waiting for Syncthing")))
        dev = st.get("device") or {}
        out = {"found": found,
               "server": {"status": "ok", "text": M("Syncthing answers"), "detail": M("{version} at {url}, this device “{name}”", version=st.get("version") or "", url=st["url"], name=dev.get("name") or "")},
               "key": {"status": "ok", "text": M("Syncthing API key accepted"), "detail": ""},
               "api": self.api_compatibility(st.get("api") or {}, st.get("version") or "")}
        mh = getattr(self.app, "mirror_health", None)
        keys = list(mh.libraries()) if mh is not None else []
        names = {str(lib["key"]): lib.get("title") or lib["key"] for lib in getattr(self.app, "libraries", []) or []}
        folders = [f for f in st.get("folders") or [] if f["libraries"]]
        out["throttle"] = self.throttle_light()
        out["reach"] = self.reach_light()
        if not keys:
            out["folders"] = off(M("No library is mirrored"), M("Which folder carries each library is looked at once one is ([Mirror page](/mirrors))."))
            out["devices"] = off(M("Devices"))
            return out
        missing = [names.get(k, k) for k in keys if k not in (st.get("libraries") or {})]
        said = "; ".join(f"{', '.join(names.get(k, k) for k in f['libraries'])}: “{f['label']}” ({f['path']})" for f in folders)
        if missing:
            out["folders"] = {"status": "warn", "text": M("{n} mirrored libraries without a Syncthing folder", one="{n} mirrored library without a Syncthing folder", n=len(missing)),
                              "detail": M("Not covered by any folder in Syncthing: {names}. {covered}", names=", ".join(missing), covered=said)}
        else:
            out["folders"] = {"status": "ok", "text": M("Every mirrored library has a Syncthing folder"), "detail": said}
        devs = {d["id"]: d for f in folders for d in f["devices"]}
        if not devs:
            out["devices"] = off(M("Devices"), M("The folders over the mirrored libraries are shared with no other device."))
        else:
            down = [d["name"] for d in devs.values() if not d["connected"]]
            up = [d["name"] for d in devs.values() if d["connected"]]
            out["devices"] = {"status": "warn" if down else "ok",
                              "text": M("{n} devices not connected", one="{n} device not connected", n=len(down)) if down else M("Connected to {names}", names=", ".join(up)),
                              "detail": M("Not connected: {down}. Connected: {up}.", down=", ".join(down), up=", ".join(up) or M("none")) if down else ""}
        return out

    def snapshot(self) -> dict:
        """For the state (System → Syncthing, the Mirror page, the Mirror Checks page): the last answer without the ignore patterns
        and the file lists (their counts instead)."""
        st = dict(self.status)
        return {"wanted": self.wanted(), "busy": self.busy, "at": st.get("at"), "ok": bool(st.get("ok")), "error": st.get("error"), "version": st.get("version"),
                "device": st.get("device") or {}, "identity": self.identity(), "lights": self.lights(), "found": dict(self.found), "api": st.get("api") or {}, "options": st.get("options") or {},
                "throttle": {"on": bool(self.settings().get("throttle")), "active": dict(self.throttled) if self.throttled else None, "reading": dict(self.reading)},
                "folders": [{k: v for k, v in f.items() if k not in ("ignores", "need", "remote_need")} | {"need_n": len(f.get("need") or []), "out_n": sum(len(v) for v in (f.get("remote_need") or {}).values()),
                             "ignores_plexcache": bool(f.get("ignores") and any(ignored_by(f["ignores"], mark_in(e["rel"]))
                                                                                                                                 for es in (st.get("libraries") or {}).values() for e in es if e["folder"] == f["id"]))}
                            for f in st.get("folders") or []],
                "libraries": st.get("libraries") or {}}

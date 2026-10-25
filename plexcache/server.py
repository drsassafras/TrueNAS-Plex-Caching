"""HTTP server: the web UI plus a small JSON API (standard library only)."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import filehistory, plexbackup
from .auth import SESSION_DAYS, Auth
from .config import SettingsError
from .i18n import M, jsonable, msg
from .plex import PlexError, check_pin, create_pin

log = logging.getLogger("plexcache")
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
PAGES = {"/", "/health", "/caching", "/ssd", "/library", "/tools", "/optimizer", "/convert", "/dataset", "/merge", "/versions", "/folders", "/unlink", "/filechecks", "/plexchecks", "/processes", "/sync", "/activity", "/system", "/features", "/settings"}  # the last two: links from before 0.15
MAX_BODY = 1024 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def _build_id() -> str:
    """A short fingerprint of the web UI's files. It changes whenever they do (after tnsync or an
    update), so pages ask for the new files by name and open tabs know to reload themselves."""
    h = hashlib.sha1()
    for root, dirs, files in os.walk(WEB_DIR):
        dirs.sort()
        for name in sorted(files):
            try:
                with open(os.path.join(root, name), "rb") as fh:
                    h.update(name.encode() + b"\0" + fh.read())
            except OSError:
                pass
    return h.hexdigest()[:12]


BUILD = _build_id()
# In the HTML pages, /static/x.js and /static/x.css get "?v=<build>" so a browser can't keep using
# the files from before.
_ASSET = re.compile(r'(/static/[A-Za-z0-9_.-]+\.(?:js|css))"')


# Reachable without signing in: the sign-in page's own files and the container health check.
OPEN_PATHS = {"/healthz", "/login"}


# The local network: private and link-local addresses, this machine, and the shared range VPNs such as
# Tailscale use (100.64.0.0/10 is never routed on the internet). Docker's own networks are private too.
LOCAL_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10",
    "::1/128", "fc00::/7", "fe80::/10")]


def local_address(address: str) -> bool:
    """Is ``address`` (a client's IP) on the local network?"""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in n for n in LOCAL_NETS)


def host_allowed(host_header: str | None) -> bool:
    """Without a sign-in, answer only to this app's IP address or a local name. A public name could be
    DNS rebinding: someone else's web page pointing its own name at the NAS, so a browser on the
    network sends the page's requests here. (With a sign-in, such requests carry no session.)"""
    from .app import _is_local_name
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        h = h[1:h.find("]")] if "]" in h else ""
    elif h.count(":") == 1:
        h = h.split(":")[0]
    if not h:
        return True  # (HTTP/1.0 sends none: not a browser)
    proxied = urllib.parse.urlsplit(os.environ.get("PLEXCACHE_URL", "")).hostname
    return h in ("localhost", proxied) or _is_local_name(h)


def _no_constants(name):
    raise ValueError(f"{name} isn't allowed")


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_handler(app):
    auth = getattr(app, "auth", None) or Auth()
    refused, refused_lock = {}, threading.Lock()  # address -> when a refusal from it was last logged

    class Handler(BaseHTTPRequestHandler):
        server_version = "PlexCache"
        sys_version = ""
        timeout = 30  # drop connections that go quiet, so idle sockets can't pile up

        def log_message(self, fmt, *args):  # quiet access log unless debugging
            log.debug("%s - %s", self.address_string(), fmt % args)

        # ------------------------------------------------------------ utils
        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            # Only this app's own files may run or load; nothing may frame the page (clickjacking).
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Cross-Origin-Opener-Policy", "same-origin-allow-popups")  # keeps the plex.tv sign-in window
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, status: int = 200):
            # Messages (M) go out as {"_t": template, "_p": values}, for each browser to translate.
            self._send(status, json.dumps(jsonable(data), default=str).encode(), "application/json", {"Cache-Control": "no-store"})

        def _error(self, message, status: int = 400):
            self._json({"error": message}, status)

        def _file(self, path: str):
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._error("not found", 404)
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype == "text/html":
                body = _ASSET.sub(lambda m: f'{m.group(1)}?v={BUILD}"', body.decode("utf-8")).encode("utf-8")
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            # Always checked with the server; unchanged files are answered with a short 304.
            etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
            if etag in (self.headers.get("If-None-Match") or ""):
                return self._send(304, b"", ctype, {"Cache-Control": "no-cache", "ETag": etag})
            self._send(200, body, ctype, {"Cache-Control": "no-cache", "ETag": etag})

        def _body(self) -> dict:
            # Only JSON is accepted, which also stops cross-site form posts.
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                raise SettingsError("expected application/json")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise SettingsError("bad Content-Length") from None
            if n < 0 or n > MAX_BODY:
                raise SettingsError("request too large" if n > 0 else "bad Content-Length")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}", parse_constant=_no_constants)  # NaN / Infinity aren't JSON
            except ValueError:
                raise SettingsError("invalid JSON") from None
            if not isinstance(data, dict):
                raise SettingsError("expected a JSON object")
            return data

        # ----------------------------------------------------------- routes
        def do_HEAD(self):
            self.do_GET()

        def _allowed(self, path: str) -> bool:
            """Refuse what this app shouldn't answer at all: devices outside the local network (when
            System → Local network only is on), and, without a sign-in, names that aren't local."""
            if path == "/healthz":
                return True
            if app.settings.section("general").get("lan_only") and not local_address(self.client_address[0]):
                self._refuse(path, M("outside the local network"),
                             M("Plex Cache only answers devices on your local network. To use it from elsewhere, turn off "
                               "“Local network only” in System, from a device on your network."))
                return False
            if not auth.enabled and not host_allowed(self.headers.get("Host")):
                self._refuse(path, M("not a local name"),
                             M("Without a sign-in, Plex Cache only answers to its IP address or a local name. Open it by "
                               "its IP address, set PLEXCACHE_URL to the address you use, or set PLEXCACHE_USERNAME and "
                               "PLEXCACHE_PASSWORD in the app’s YAML."))
                return False
            return True

        def _refuse(self, path: str, reason, why):
            # noted in the activity log once an hour per address (so scanners from the internet can't flood it)
            now, address = time.time(), self.client_address[0]
            with refused_lock:
                logged = now - refused.get(address, 0) < 3600
                refused[address] = refused.get(address, 0) if logged else now
            if not logged:
                app.activity.event("settings", M("refused a request from {address}: {why}", address=address, why=reason), "warn")
            if path in PAGES or path == "/login":
                page = "<!doctype html><meta charset=utf-8><title>Plex Cache</title><p style=\"font:16px system-ui;margin:3em auto;max-width:36em\">"
                return self._send(403, (page + _html(str(why)) + "</p>").encode(), "text/html; charset=utf-8")
            return self._error(why, 403)

        def _signed_in(self, path: str) -> bool:
            """Let the request through, or answer it with the sign-in page / a 401."""
            if not auth.enabled or path in OPEN_PATHS or path.startswith("/static/") or auth.request_ok(self.headers):
                return True
            if path in PAGES:
                self._file(os.path.join(WEB_DIR, "login.html"))
            else:
                self._error("sign in first", 401)
            return False

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if not self._allowed(path) or not self._signed_in(path):
                return
            if path in PAGES or path.startswith("/api/"):  # not the open paths (/healthz, /login...)
                app.note_host(self.headers.get("Host"))
            if path in PAGES:
                return self._file(os.path.join(WEB_DIR, "index.html"))
            if path == "/login":
                return self._file(os.path.join(WEB_DIR, "login.html" if auth.enabled else "index.html"))
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if not re.fullmatch(r"(i18n/)?[a-zA-Z0-9_-][a-zA-Z0-9_.-]*", name):
                    return self._error("not found", 404)
                return self._file(os.path.join(WEB_DIR, name))
            if path == "/healthz":
                return self._json({"ok": True, "version": app.version})
            if path == "/api/state":
                return self._json(dict(app.state(), build=BUILD))  # (build: see app.js load())
            if path == "/api/tools/readiness":  # before files change: room for it, and a recent snapshot (safety.py)
                from .safety import readiness
                rq = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                key, need = (rq.get("key") or [""])[0], (rq.get("needed") or ["0"])[0]
                if not app.library_info(key):
                    return self._error("not found", 404)
                if need != "largest":
                    try:
                        need = max(0, int(need))
                    except ValueError:
                        need = 0
                try:
                    retained = max(0, int((rq.get("retained") or ["0"])[0]))
                except ValueError:
                    retained = 0
                tool = (rq.get("tool") or [""])[0]
                if tool == "optimizer":  # (what a snapshot keeps of a library's rewrite: the files still to do)
                    retained = retained or int(((app.optimizing.summary or {}).get(key) or {}).get("todo_bytes") or 0)
                return self._json(jsonable(readiness(app, key, need, keeps=(rq.get("keeps") or [""])[0] == "1", retained=retained, tool=tool)))
            if path == "/api/optimizer/files":  # the Optimizer page: a page of one library's files to optimize, each a card
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                arg = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731
                if not app.library_info(arg("key")):
                    return self._error("not found", 404)
                try:
                    off, lim = int(arg("offset") or 0), max(1, min(200, int(arg("limit") or 25)))
                except ValueError:
                    return self._error("bad offset", 400)
                return self._json(jsonable(app.optimizing.files_todo(arg("key"), off, lim)))
            if path.startswith("/api/library"):
                # The Library Health page: a library's files with their layouts, or one file in full
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                arg = lambda k, d="": (q.get(k) or [d])[0]
                if path == "/api/library":
                    try:
                        off, lim = max(0, int(arg("offset", "0"))), min(1000, max(1, int(arg("limit", "100"))))
                    except ValueError:
                        return self._error("bad request", 400)
                    if not app.library_info(arg("key")):
                        return self._error("not found", 404)
                    return self._json(jsonable(app.library_view(arg("key"), off, lim, arg("q"))))
                if path == "/api/library/blocks":
                    try:
                        numbers = [int(x) for x in arg("n").split(",") if x]
                        got = app.block_estimates(arg("path"), numbers)
                    except (ValueError, OSError) as e:
                        return self._error(str(e), 400)
                    return self._json({"blocks": got}) if got is not None else self._error("not found", 404)
                if path == "/api/library/health":  # Library Health: one library's items through the content checks
                    app.libdata.fast_track(arg("key"))  # (the library opened: its data gathered first)
                    if not app.library_info(arg("key")):
                        return self._error("not found", 404)
                    return self._json(jsonable(app.library_health(arg("key"))))  # (worked out in the background already, as a rule)
                if path == "/api/library/differences":  # Library Sync: one library's differences, as last compared
                    got = app.sync.differences(arg("key"))
                    return self._json(jsonable(got)) if got else self._error("not found", 404)
                if path == "/api/library/file":
                    try:
                        got = app.file_view(arg("path"))
                    except OSError as e:
                        return self._error(str(e), 404)
                    return self._json(jsonable(got)) if got else self._error("not found", 404)
                return self._error("not found", 404)
            if path == "/api/match":
                # a Plex match (plex://…), looked up on Plex's metadata service: what it is, and its poster
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                try:
                    got = app.match_info((qs.get("guid") or [""])[0], image=bool((qs.get("image") or [""])[0]))
                except Exception:  # noqa: BLE001 — plex.tv unreachable, or no such entry
                    return self._error("not found", 404)
                if got is None:
                    return self._error("not found", 404)
                if isinstance(got, bytes):
                    return self._send(200, got, "image/jpeg", {"Cache-Control": "private, max-age=86400"})
                return self._json(got)
            if path == "/api/poster":
                # a small poster beside a file or what plays (from Plex, through this app: the browser has no Plex token)
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                p = (qs.get("path") or [""])[0]
                if not re.fullmatch(r"/library/metadata/\d+/(thumb|art)/\d+", p):
                    return self._error("not found", 404)
                try:
                    data = app.poster_thumb(p, "mirror" if (qs.get("server") or [""])[0] == "mirror" else "")
                except Exception:  # noqa: BLE001 — Plex unreachable, or no such poster: no thumbnail
                    return self._error("not found", 404)
                return self._send(200, data, "image/jpeg", {"Cache-Control": "private, max-age=86400"})
            if path.startswith("/api/processes"):
                # The Processes page: the job running now, every job in the log, a job's files, and the export
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                arg = lambda k, d="": (q.get(k) or [d])[0]
                if path == "/api/processes":
                    return self._json({"job": app.jobs.snapshot(), "queue": app.jobs.queued(), "jobs": app.jobs.log.jobs(),
                                       "plex_backups": plexbackup.listing(app)})  # (the backups of Plex's records: each with its Revert)
                if path == "/api/processes/files":
                    try:
                        off, lim = max(0, int(arg("offset", "0"))), min(1000, max(1, int(arg("limit", "200"))))
                    except ValueError:
                        return self._error("bad request", 400)
                    d = app.jobs.log.files(arg("job"), off, lim, sort=arg("sort") or "file", fill=lambda rows: app.optimizing.describe(rows, arg("job")))
                    for f in d["files"]:  # each written file's chart: what caching keeps of it, just before and after
                        f["views"] = filehistory.file_views(f.pop("mix_before", None), f.pop("mix_after", None))
                    tool = app.tools.get(str(d.get("tool") or "")) if d.get("kind") == "tool" else None
                    d["columns"] = [{"id": cid, "name": name} for cid, name in tool.history] if tool else []  # (a tool's run: its own columns)
                    return self._json(jsonable(d))
                if path == "/api/processes/export":
                    fmt = "json" if arg("format") == "json" else "csv"
                    body, ctype = app.jobs.log.export(fmt)
                    return self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="plex-cache-jobs.{fmt}"',
                                                         "Cache-Control": "no-store"})
                return self._error("not found", 404)
            if path == "/api/coverage":
                # How much of each library was cached, hour by hour (asked for when the card is shown)
                return self._json(app.coverage.series({lib["key"]: lib.get("title") for lib in app.libraries}))
            if path == "/api/cachelog/export":
                # The whole cache log (what was cached when, and every count), to look into problems
                body = json.dumps(jsonable(app.diagnostics()), indent=1, default=str).encode()
                return self._send(200, body, "application/json",
                                  {"Content-Disposition": 'attachment; filename="plex-cache-cachelog.json"', "Cache-Control": "no-store"})
            if path == "/api/export":
                body = json.dumps(app.settings.export(), indent=2).encode()
                return self._send(200, body, "application/json",
                                  {"Content-Disposition": 'attachment; filename="plex-cache-settings.json"'})
            return self._error("not found", 404)

        def _same_origin(self) -> bool:
            """A browser says which page a request came from (Origin). Changes are only
            taken from this app's own pages; scripts and tools send no Origin at all."""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            # Behind a reverse proxy the browser's address is the proxy's: accept that too.
            ours = {(self.headers.get("Host") or "").lower(), (self.headers.get("X-Forwarded-Host") or "").lower(),
                    urllib.parse.urlsplit(os.environ.get("PLEXCACHE_URL", "")).netloc.lower()}
            return urllib.parse.urlsplit(origin).netloc.lower() in ours - {""}

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not self._allowed(path):
                return
            if not self._same_origin():
                return self._error("requests from other sites aren't accepted", 403)
            try:
                body = self._body()
            except SettingsError as e:
                return self._error(msg(e), 415 if "json" in str(e) else 400)
            if path == "/login":
                if not auth.enabled:
                    return self._json({"ok": True})
                user, pw = body.get("username", ""), body.get("password", "")
                if not isinstance(user, str) or not isinstance(pw, str) or len(user) > 256 or len(pw) > 1024:
                    return self._error(M("username and password must be text"), 400)
                user = user.strip()  # (a space from autofill or a paste shouldn't refuse the sign-in)
                address = self.client_address[0]
                if auth.locked_out(address):
                    return self._error(M("Too many wrong passwords from this device. Try again in 15 minutes."), 429)
                ok = auth.check_password(user, pw)
                auth.note_attempt(address, ok)
                if ok:
                    if auth.default_password:
                        app.activity.event("settings", M("sign-in refused: the default password hasn’t been changed"), "warn")
                        return self._error(M("The default password can’t be used. Set your own PLEXCACHE_PASSWORD (and "
                                             "username) in the app’s YAML in TrueNAS — Apps → plex-cache → Edit — then sign "
                                             "in with it."), 403)
                    app.activity.event("settings", M("signed in to the web UI"))
                    https = (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"
                    return self._send(200, b'{"ok": true}', "application/json",
                                      {"Set-Cookie": auth.cookie(auth.new_session(), SESSION_DAYS * 86400, secure=https)})
                app.activity.event("settings", M("failed sign-in from {address}", address=self.address_string()), "warn")
                return self._error(M("wrong username or password"), 401)
            if path == "/logout":
                auth.revoke(auth.cookie_value(self.headers))
                return self._send(200, b'{"ok": true}', "application/json", {"Set-Cookie": auth.cookie("", 0)})
            if not self._signed_in(path):
                return
            app.note_host(self.headers.get("Host"))
            try:
                if path == "/api/settings":
                    libs = (body.get("nextepisode") or {}).get("libraries") if isinstance(body.get("nextepisode"), dict) else None
                    if libs is not None:
                        app.check_prefetch_libraries(libs)
                    for feature in ("warmup", "nowplaying"):
                        sec = body.get(feature)
                        if isinstance(sec, dict) and sec.get("enabled") is True:
                            ok, why = app.feature_available(feature)
                            if not ok:
                                raise SettingsError(why)
                    app.settings.update(body)
                    if getattr(app, "ha", None):
                        app.ha.wake.set()  # Home Assistant shows the change straight away
                    if (body.get("general") or {}).get("update_check") is True and getattr(app, "updates", None):
                        app.updates.wake.set()  # check now, rather than in a few hours
                    # Re-run the checks this setting affects, so the page shows whether it worked.
                    if any(k in body for k in ("plex", "truenas")):
                        app.test_connections_async(then_checks=True, rescan="plex" in body)
                    else:
                        app.run_checks_async(rescan="excluded_libraries" in body.get("general", {}))
                    return self._json({"settings": app.settings.public()})
                if path == "/api/test":
                    app.test_connections_async(then_checks=True, rescan=True)
                    return self._json({"started": True})
                if path == "/api/checks":
                    rescan = body.get("rescan", True)
                    if not isinstance(rescan, bool):
                        raise SettingsError(M("rescan must be true or false"))
                    app.run_checks_async(rescan=rescan)
                    return self._json({"started": True})
                if path == "/api/mqtt/reconnect":
                    app.ha.retry()
                    return self._json({"started": True})
                if path == "/api/benchmark":
                    app.run_benchmark()
                    return self._json({"started": True})
                if path == "/api/warmup/run":
                    return self._json({"started": app.start_warmup(manual=True)})
                if path == "/api/cachelog/reset":
                    app.cachelog.reset_stats()
                    app.cachelog.save(force=True)
                    app.activity.event("settings", M("“How Long Cached Data Lasts” started again"))
                    return self._json({"reset": True})
                if path == "/api/optimize/library":
                    key = body.get("library")
                    if not isinstance(key, str) or not app.library_info(key):
                        raise SettingsError(M("unknown library: {key}", key=str(key)))
                    mode = body.get("mode", "optimize")
                    if mode not in ("optimize", "covers"):
                        raise SettingsError(M("mode must be optimize or covers"))
                    from . import peers, safety
                    why = peers.optimizer_conflict(app, key)
                    if why:
                        raise SettingsError(why)  # (another Plex Cache would undo it: said on Health too)
                    if mode == "optimize":  # files are rewritten: room for the largest (and what a snapshot keeps of the rewritten), and a backup that can be made
                        sums = (app.optimizing.summary or {}).get(key) or {}
                        ready = safety.readiness(app, key, "largest", retained=int(sums.get("todo_bytes") or 0), tool="optimizer")
                        if not ready["ready"]:
                            raise SettingsError(M("Not yet: {why}", why=safety.why_not(ready)))
                    files = body.get("files")  # (the files ticked on the page, their Plex paths; none: every file that needs it)
                    if files is not None and (mode != "optimize" or not isinstance(files, list) or not all(isinstance(f, str) for f in files) or not files):
                        raise SettingsError(M("files must be a list of the files to optimize"))
                    got = app.optimizing.queue(key, mode, files)  # now, or after the jobs before it
                    return self._json({"started": got == "started", "queued": got == "queued"})
                if path.startswith("/api/sync/"):
                    # Library Sync (sync.py): compare now, look for mirrors, approve the settings as reviewed
                    try:
                        if path == "/api/sync/compare":
                            return self._json({"started": app.sync.compare_async()})
                        if path == "/api/sync/start":  # one library (opened on the Library page): {"library", "pause"?}
                            key = str(body.get("library") or "")
                            if body.get("pause"):
                                app.sync.pause(key)
                                return self._json(jsonable(app.sync.snapshot()))
                            return self._json(jsonable(app.sync.start(key, force=bool(body.get("force")))))
                        if path == "/api/sync/discover":
                            app.sync.discover_async()
                            return self._json({"started": True})
                        if path == "/api/sync/approve":
                            app.sync.approve()
                            return self._json(jsonable(app.sync.snapshot()))
                        if path == "/api/sync/apply":  # the differences ticked, made true on the other server: {"library", "ids"}
                            started = app.sync.applier.apply_async(str(body.get("library") or ""), [str(i) for i in body.get("ids") or []])
                            return self._json(dict(jsonable(app.sync.snapshot()), started=started))
                        if path == "/api/sync/undo":  # a logged change put back: {"id"}
                            app.sync.applier.undo(str(body.get("id") or ""))
                            return self._json(jsonable(app.sync.snapshot()))
                    except ValueError as e:
                        raise SettingsError(e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))) from None
                    raise SettingsError(M("unknown action"))
                if path == "/api/updates/check":  # Check Now: GitHub asked for the newest version (and, on dev, commit)
                    app.updates.check_async()
                    return self._json(app.updates.status())
                if path == "/api/updates/apply":  # Update Now: the newer image pulled and the app redeployed by TrueNAS
                    try:
                        return self._json(app.update_now())
                    except ValueError as e:
                        raise SettingsError(e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))) from None
                if path == "/api/tools/snapshot":  # a snapshot of the library's datasets, "I've made a backup", or the last error dismissed: {"library", "action": "take" | "ack" | "dismiss"}
                    from . import safety
                    key = str(body.get("library") or "")
                    if not app.library_info(key):
                        raise SettingsError(M("unknown library: {key}", key=key))
                    try:
                        if body.get("action") == "dismiss":
                            safety.errors(app).pop(key, None)
                        else:
                            safety.take_snapshot(app, key, str(body.get("tool") or ""))
                    except ValueError as e:
                        raise SettingsError(e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))) from None
                    return self._json(jsonable(safety.readiness(app, key)))
                if path.startswith("/api/tools/"):
                    # a tool (tools/): {"action": "scan", "library", "options"} or {"action": "apply", "ids": [...]}
                    tool = app.tools.get(path[len("/api/tools/"):])
                    if tool is None:
                        raise SettingsError(M("unknown tool"))
                    action = body.get("action")
                    if action == "scan":
                        started = tool.scan_async(str(body.get("library") or ""), body.get("options") if isinstance(body.get("options"), dict) else {},
                                                  refresh=bool(body.get("refresh", True)))  # (Scan Again gathers the data again; the page's own first scan takes it as it is)
                    elif action == "apply":
                        choices = body.get("choices") if isinstance(body.get("choices"), dict) else {}
                        try:
                            started = tool.apply_async([str(i) for i in body.get("ids") or []], {str(k): v for k, v in choices.items() if isinstance(v, dict)})
                        except ValueError as e:
                            raise SettingsError(e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))) from None
                    else:
                        raise SettingsError(M("unknown action"))
                    return self._json(dict(jsonable(tool.snapshot()), started=started))
                if path == "/api/trash":
                    # a file a tool set aside (trash.py): {"path", "action": "purge" | "restore" | "keep"}
                    try:
                        app.trash.act(str(body.get("path") or ""), str(body.get("action") or ""))
                    except ValueError as e:
                        raise SettingsError(e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))) from None
                    return self._json(jsonable(app.trash.snapshot()))
                if path == "/api/conversions":
                    # A converted file's original (converting.py): {"original", "action": "delete" | "keep" | "check"}
                    try:
                        app.conversions.act(str(body.get("original") or ""), str(body.get("action") or ""))
                    except ValueError as e:
                        why = e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))
                        raise SettingsError(why) from None
                    return self._json(jsonable(app.conversions.snapshot()))
                if path == "/api/migration":
                    # Turning a library folder into a dataset (migrating.py): one action at a time
                    mig, action = app.migration, body.get("action")
                    try:
                        if action == "plan":
                            return self._json(jsonable(mig.plan(str(body.get("library") or ""), str(body.get("folder") or ""))))
                        if action == "check":
                            return self._json(jsonable(mig.refresh()))
                        if action in ("copy", "catchup"):
                            if action == "copy":  # the folder copied into the new dataset: a recent snapshot first (room is among its own checks)
                                from . import safety
                                ready = safety.readiness(app, str((mig.state or {}).get("library") or ""), 0, keeps=True, tool="dataset")
                                if not ready["ready"]:
                                    raise SettingsError(M("Not yet: {why}", why=safety.why_not(ready)))
                            return self._json(jsonable(mig.start_copy(mirror=action == "catchup")))
                        if action in ("pause", "resume"):
                            app.jobs.pause(action == "pause")
                            return self._json(jsonable(mig.refresh()))
                        if action == "stop":
                            app.jobs.cancel()
                            return self._json(jsonable(mig.refresh()))
                        if action == "pass_then_move":
                            return self._json(jsonable(mig.pass_then_move()))
                        if action == "rename_old":
                            return self._json(jsonable(mig.rename_old()))
                        if action == "undo_rename":
                            return self._json(jsonable(mig.undo_rename()))
                        if action == "delete_old":
                            return self._json(jsonable(mig.delete_old()))
                        if action == "restart":
                            return self._json(jsonable(mig.restart()))
                        if action == "cancel":
                            mig.cancel()
                            return self._json({"cancelled": True})
                    except ValueError as e:
                        why = e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))  # (translated, when it can be)
                        raise SettingsError(why) from None
                    raise SettingsError(M("unknown action"))
                if path == "/api/library/health/ignore":
                    # an item's Library Health issues ignored (greyed out, kept for good) or brought back:
                    # {"library", "item", "action": "ignore" | "restore", "ids": [check ids]}
                    from .libhealth import set_ignored
                    key = str(body.get("library") or "")
                    if not app.library_info(key) or not body.get("item"):
                        raise SettingsError(M("unknown library: {key}", key=key))
                    if body.get("action") == "ignore":
                        set_ignored(app, key, str(body["item"]), [str(i) for i in body.get("ids") or []])
                    elif body.get("action") == "restore":
                        set_ignored(app, key, str(body["item"]), None)
                    else:
                        raise SettingsError(M("unknown action"))
                    return self._json({"ok": True})
                if path == "/api/library/survey":
                    # read the layout of a library's files (Library Health): {"library", "force"?, "pause"?}.
                    # One library at a time: starting one pauses the other, which carries on when started again.
                    key = body.get("library")
                    if not isinstance(key, str) or not app.library_info(key):
                        raise SettingsError(M("unknown library: {key}", key=str(key)))
                    if body.get("pause"):
                        app.inspector.pause(key)
                        return self._json(app.inspector.snapshot())
                    # "force": every file again; otherwise only files new or changed since they were inspected
                    return self._json(app.inspector.start(key, force=bool(body.get("force"))))
                if path == "/api/optimizer/survey":  # the Optimizer page's Scan Again: every library's files looked at again now
                    app.optimizing.survey_soon()
                    return self._json({"started": True})
                if path == "/api/optimizer/check":
                    # The optimizer's setup steps, checked again now (the Optimizer page's "Check again")
                    return self._json(jsonable(app.optimizing.diagnose()))
                if path == "/api/job":
                    # The file job running now, whatever kind: {"action": "pause" | "resume" | "cancel"}
                    a = body.get("action")
                    if a == "dismiss":
                        app.jobs.dismiss()
                        return self._json({"ok": True})
                    if a == "retry":  # a failed file (from the job log), tried again after the file in hand
                        got = app.optimizing.retry(str(body.get("job") or ""), str(body.get("file") or ""))
                        return self._json({"ok": got is not None})
                    if a == "remove":  # queued work, taken off the queue
                        return self._json({"ok": app.jobs.remove(str(body.get("id") or ""))})
                    if a == "plex_revert":  # Plex's records put back from a backup of them (plexbackup.py): {"id"}
                        try:
                            return self._json(plexbackup.revert(app, str(body.get("id") or "")))
                        except ValueError as e:
                            raise SettingsError(e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))) from None
                    if a in ("revert", "rollback"):  # a tool's run undone: its changed files put back from the backup, or the whole snapshot rolled back
                        from .tools import revert_job, rollback_job
                        try:
                            return self._json((revert_job if a == "revert" else rollback_job)(app, str(body.get("job") or "")))
                        except ValueError as e:
                            raise SettingsError(e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))) from None
                    ok = app.jobs.pause(a == "pause") if a in ("pause", "resume") else app.jobs.cancel() if a == "cancel" else None
                    if ok is None:
                        raise SettingsError(M("unknown action"))
                    return self._json({"ok": ok})
                if path == "/api/warmup/stop":
                    app.stop_warmup()
                    return self._json({"stopping": True})
                # Has the Plex sign-in been approved yet? (A POST: it saves the token when it has.)
                # /api/plex2/pin/<id>: the second Plex server's sign-in (its account's token, to list its servers)
                m = re.fullmatch(r"/api/plex(2?)/pin/(\d{1,12})", path)
                if m:
                    try:
                        token = check_pin(app.settings.section("general")["client_id"], int(m.group(2)))
                    except PlexError as e:
                        return self._error(msg(e), 502)
                    if token and m.group(1):
                        app.settings.update({"plex2": {"account_token": token}})
                        app.activity.event("settings", M("signed in with Plex for the second server"))
                    elif token:
                        app.settings.update({"plex": {"token": token}})
                        app.activity.event("settings", M("signed in with Plex"))
                        app.test_connections_async(then_checks=True, rescan=True)
                    return self._json({"done": bool(token)})
                if path in ("/api/plex2/servers", "/api/plex2/use", "/api/plex2/forget", "/api/plex2/refresh"):
                    # The second Plex server (plex2.py): the account's servers; watch one; stop; read it again
                    try:
                        if path == "/api/plex2/servers":
                            return self._json({"servers": app.other.list_servers()})
                        if path == "/api/plex2/use":
                            return self._json(jsonable(app.other.use(str(body.get("id") or ""))))
                        if path == "/api/plex2/forget":
                            app.other.forget()
                        else:
                            app.other.refresh()
                        return self._json(jsonable(app.other.snapshot()))
                    except ValueError as e:
                        raise SettingsError(e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))) from None
                if path in ("/api/plex/pin", "/api/plex2/pin"):
                    try:
                        return self._json(create_pin(app.settings.section("general")["client_id"]))
                    except PlexError as e:
                        return self._error(msg(e), 502)
            except SettingsError as e:
                return self._error(msg(e), 400)
            return self._error("not found", 404)

    return Handler


def serve(app, host: str, port: int):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    log.info("Plex Cache %s listening on http://%s:%d", app.version, host, port)
    return httpd

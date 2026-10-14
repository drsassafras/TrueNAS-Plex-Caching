"""HTTP server: the web UI plus a small JSON API (standard library only)."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .auth import SESSION_DAYS, Auth
from .config import SettingsError
from .plex import PlexError, check_pin, create_pin

log = logging.getLogger("plexcache")
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
PAGES = {"/", "/health", "/features", "/activity", "/settings"}
MAX_BODY = 1024 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


# Reachable without signing in: the sign-in page's own files, the container health
# check, and the record-size script (fetched with curl from the TrueNAS shell).
OPEN_PATHS = {"/healthz", "/login", "/tools/rewrite_blocks.py"}


def _no_constants(name):
    raise ValueError(f"{name} isn't allowed")


def make_handler(app):
    auth = getattr(app, "auth", None) or Auth()

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
            self._send(status, json.dumps(data, default=str).encode(), "application/json", {"Cache-Control": "no-store"})

        def _error(self, message: str, status: int = 400):
            self._json({"error": message}, status)

        def _file(self, path: str):
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._error("not found", 404)
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self._send(200, body, ctype, {"Cache-Control": "no-cache"})

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
            if not self._signed_in(path):
                return
            if path in PAGES or path.startswith("/api/"):  # not the open paths (/healthz, /login...)
                app.note_host(self.headers.get("Host"))
            if path in PAGES:
                return self._file(os.path.join(WEB_DIR, "index.html"))
            if path == "/login":
                return self._file(os.path.join(WEB_DIR, "login.html" if auth.enabled else "index.html"))
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) or name.startswith("."):
                    return self._error("not found", 404)
                return self._file(os.path.join(WEB_DIR, name))
            if path == "/tools/rewrite_blocks.py":
                # The re-copy script Health points to, served so it always matches this version.
                return self._file(os.path.join(os.path.dirname(WEB_DIR), "tools", "rewrite_blocks.py"))
            if path == "/healthz":
                return self._json({"ok": True, "version": app.version})
            if path == "/api/state":
                return self._json(app.state())
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
            if not self._same_origin():
                return self._error("requests from other sites aren't accepted", 403)
            try:
                body = self._body()
            except SettingsError as e:
                return self._error(str(e), 415 if "json" in str(e) else 400)
            if path == "/login":
                if not auth.enabled:
                    return self._json({"ok": True})
                user, pw = body.get("username", ""), body.get("password", "")
                if not isinstance(user, str) or not isinstance(pw, str) or len(user) > 256 or len(pw) > 1024:
                    return self._error("username and password must be text", 400)
                if auth.check_password(user, pw):
                    if auth.default_password:
                        app.activity.event("settings", "sign-in refused: the default password hasn't been changed", "warn")
                        return self._error("The default password can't be used. Set your own PLEXCACHE_PASSWORD (and "
                                           "username) in the app's YAML in TrueNAS — Apps → Plex Cache → Edit — then sign "
                                           "in with it.", 403)
                    app.activity.event("settings", "signed in to the web UI")
                    return self._send(200, b'{"ok": true}', "application/json",
                                      {"Set-Cookie": auth.cookie(auth.new_session(), SESSION_DAYS * 86400)})
                app.activity.event("settings", f"failed sign-in from {self.address_string()}", "warn")
                return self._error("wrong username or password", 401)
            if path == "/logout":
                auth.revoke(auth.cookie_value(self.headers))
                return self._send(200, b'{"ok": true}', "application/json", {"Set-Cookie": auth.cookie("", 0)})
            if not self._signed_in(path):
                return
            app.note_host(self.headers.get("Host"))
            try:
                if path == "/api/settings":
                    modes = (body.get("nextepisode") or {}).get("library_modes") if isinstance(body.get("nextepisode"), dict) else None
                    if modes is not None:
                        app.check_library_modes(modes)
                    for feature in ("warmup", "nowplaying", "nextepisode"):
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
                        raise SettingsError("rescan must be true or false")
                    app.run_checks_async(rescan=rescan)
                    return self._json({"started": True})
                if path == "/api/blockscan":
                    dataset = body.get("dataset")
                    if not isinstance(dataset, str) or len(dataset) > 255:
                        raise SettingsError("dataset must be a dataset name")
                    return self._json({"started": app.run_block_scan(dataset)})
                if path == "/api/mqtt/reconnect":
                    app.ha.retry()
                    return self._json({"started": True})
                if path == "/api/benchmark":
                    app.run_benchmark()
                    return self._json({"started": True})
                if path == "/api/warmup/run":
                    return self._json({"started": app.start_warmup(manual=True)})
                if path == "/api/warmup/stop":
                    app.stop_warmup()
                    return self._json({"stopping": True})
                # Has the Plex sign-in been approved yet? (A POST: it saves the token when it has.)
                m = re.fullmatch(r"/api/plex/pin/(\d{1,12})", path)
                if m:
                    try:
                        token = check_pin(app.settings.section("general")["client_id"], int(m.group(1)))
                    except PlexError as e:
                        return self._error(str(e), 502)
                    if token:
                        app.settings.update({"plex": {"token": token}})
                        app.activity.event("settings", "signed in with Plex")
                        app.test_connections_async(then_checks=True, rescan=True)
                    return self._json({"done": bool(token)})
                if path == "/api/plex/pin":
                    try:
                        return self._json(create_pin(app.settings.section("general")["client_id"]))
                    except PlexError as e:
                        return self._error(str(e), 502)
            except SettingsError as e:
                return self._error(str(e), 400)
            return self._error("not found", 404)

    return Handler


def serve(app, host: str, port: int):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    log.info("Plex Cache %s listening on http://%s:%d", app.version, host, port)
    return httpd

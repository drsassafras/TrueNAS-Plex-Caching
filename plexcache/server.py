"""HTTP server: the web UI plus a small JSON API (standard library only)."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import SettingsError, parse_legacy_conf
from .plex import PlexError, check_pin, create_pin
from .truenas import TrueNASError

log = logging.getLogger("plexcache")
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
PAGES = {"/", "/health", "/features", "/activity", "/settings"}
MAX_BODY = 1024 * 1024


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PlexCache"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet access log unless debugging
            log.debug("%s - %s", self.address_string(), fmt % args)

        # ------------------------------------------------------------ utils
        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
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
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise SettingsError("request too large")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise SettingsError("invalid JSON") from None
            if not isinstance(data, dict):
                raise SettingsError("expected a JSON object")
            return data

        # ----------------------------------------------------------- routes
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in PAGES:
                return self._file(os.path.join(WEB_DIR, "index.html"))
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) or name.startswith("."):
                    return self._error("not found", 404)
                return self._file(os.path.join(WEB_DIR, name))
            if path == "/healthz":
                return self._json({"ok": True, "version": app.version})
            if path == "/api/state":
                return self._json(app.state())
            if path == "/api/export":
                body = json.dumps(app.settings.export(), indent=2).encode()
                return self._send(200, body, "application/json",
                                  {"Content-Disposition": 'attachment; filename="plex-cache-settings.json"'})
            m = re.fullmatch(r"/api/plex/pin/(\d+)", path)
            if m:
                try:
                    token = check_pin(app.settings.section("general")["client_id"], int(m.group(1)))
                except PlexError as e:
                    return self._error(str(e), 502)
                if token:
                    app.settings.update({"plex": {"token": token}})
                    app.activity.event("settings", "signed in with Plex")
                    app.run_checks_async(rescan=True)
                return self._json({"done": bool(token)})
            return self._error("not found", 404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                body = self._body()
            except SettingsError as e:
                return self._error(str(e), 415 if "json" in str(e) else 400)
            try:
                if path == "/api/settings":
                    app.settings.update(body)
                    rescan = any(k in body for k in ("plex", "truenas")) or any(
                        k in body.get("general", {}) for k in ("excluded_libraries", "path_map"))
                    app.run_checks_async(rescan=rescan)
                    return self._json({"settings": app.settings.public()})
                if path == "/api/checks":
                    app.run_checks_async(rescan=bool(body.get("rescan", True)))
                    return self._json({"started": True})
                if path == "/api/benchmark":
                    app.run_benchmark()
                    return self._json({"started": True})
                if path == "/api/warmup/run":
                    return self._json({"started": app.start_warmup(manual=True)})
                if path == "/api/warmup/stop":
                    app.stop_warmup()
                    return self._json({"stopping": True})
                if path == "/api/test/plex":
                    try:
                        info = app.plex(timeout=10).ping()
                        return self._json({"ok": True, "message": f"Connected to “{info['name']}” in {info['ms']} ms"})
                    except PlexError as e:
                        return self._json({"ok": False, "message": str(e)})
                if path == "/api/test/truenas":
                    try:
                        with app.truenas() as tn:
                            v = tn.version()
                        app.run_checks_async()
                        return self._json({"ok": True, "message": f"Connected{(' to ' + v) if v else ''}"})
                    except TrueNASError as e:
                        return self._json({"ok": False, "message": str(e)})
                if path == "/api/plex/pin":
                    try:
                        return self._json(create_pin(app.settings.section("general")["client_id"]))
                    except PlexError as e:
                        return self._error(str(e), 502)
                if path == "/api/import":
                    patch, notes = parse_legacy_conf(str(body.get("text", "")))
                    if patch:
                        app.settings.update(patch)
                        app.activity.event("settings", "imported plex-l2arc.conf")
                        app.run_checks_async(rescan=True)
                    return self._json({"imported": sorted(f"{s}.{k}" for s, v in patch.items() for k in v), "notes": notes,
                                       "settings": app.settings.public()})
            except SettingsError as e:
                return self._error(str(e), 400)
            return self._error("not found", 404)

    return Handler


def serve(app, host: str, port: int):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    log.info("Plex Cache %s listening on http://%s:%d", app.version, host, port)
    return httpd

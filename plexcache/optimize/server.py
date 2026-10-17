"""The optimizer service: the second container of the app, the only one that may write media.

It does one thing — optimize the file it's given — and nothing on its own: the main app
decides what to optimize and when (it knows what's playing and what was just added), sends
one file at a time, and reads the result back.

    GET  /healthz        ok, and what the app needs to tell why it can't use the optimizer:
                         its version, whether it has a token (never the token) and how many
                         media folders it may write
    GET  /api/status     tools and versions, the file being optimized, recent results
    POST /api/check      {"path", "images"}: is the file optimized, and if not why
    POST /api/rewrite    {"path"}: write any file again, byte for byte, so it takes its dataset's
                         current record size and compression (one at a time, like optimize)
    POST /api/optimize   {"path", "images", "cover", "force"}: start optimizing it (one at a time).
                         "images": "compress" (default), "remove", "plex" or "keep" (see
                         optimizer.py); "cover": Plex's poster for "plex", base64; "force"
                         rewrites it even when its layout is already right, so it picks up the
                         dataset's current record size and compression, or the cover art choice

Every /api call needs the header ``X-Plexcache-Token`` with the token the app made: read from
the app's config folder, mounted here read-only (/config/optimizer-token), or set as
PLEXCACHE_OPTIMIZER_TOKEN. Only files under the folders mounted into this container read-write
can be touched (or under PLEXCACHE_MEDIA_ROOTS, when set): the YAML's volumes decide what it may
write. It listens on port 8643 (PLEXCACHE_OPTIMIZER_PORT).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION
from . import images as imgs
from . import mkv, tools
from .optimizer import IMAGE_MODES, Skipped, optimize_file, rewrite_file

RECENT = 200  # results kept for the app to read back
MAX_BODY = 48 * 1024 * 1024  # room for a large poster, base64


class Service:
    def __init__(self, roots: list[str], token: str | None = None, token_file: str | None = None):
        self.roots = [os.path.realpath(r) for r in roots]
        self._token = token or ""
        self.token_file = token_file
        self._read: tuple = (None, "")  # (the file's mtime, its token)
        self.lock = threading.Lock()
        self.current: dict | None = None
        self.results: list[dict] = []
        self.versions = {n: tools.version(n) for n in tools.NAMES + tools.OPTIONAL}

    @property
    def token(self) -> str:
        """The token to accept: set when started, or the app's token file — read again when it
        changes, so the optimizer can start before the app has made it."""
        if self._token or not self.token_file:
            return self._token
        try:
            mtime = os.stat(self.token_file).st_mtime_ns
            if mtime != self._read[0]:
                with open(self.token_file, encoding="utf-8") as fh:
                    self._read = (mtime, fh.read().strip())
        except OSError:
            self._read = (None, "")
        return self._read[1] if len(self._read[1]) >= 16 else ""

    def token_problem(self) -> str | None:
        """Why there's no token, for /healthz: "missing" (no file), "unreadable", "empty"."""
        if self.token:
            return None
        if not self.token_file or not os.path.exists(self.token_file):
            return "missing"
        if not os.access(self.token_file, os.R_OK):
            return "unreadable"
        return "empty"

    def allowed(self, path: str) -> str | None:
        real = os.path.realpath(path)
        return real if any(real == r or real.startswith(r + os.sep) for r in self.roots) else None

    def status(self) -> dict:
        with self.lock:
            return {"version": VERSION, "tools": self.versions, "missing": [n for n in tools.NAMES if not self.versions[n]],
                    "missing_optional": [n for n in tools.OPTIONAL if not self.versions[n]],
                    "roots": self.roots, "current": dict(self.current) if self.current else None,
                    "results": list(self.results[-RECENT:]), "uid": os.getuid(), "gid": os.getgid(),
                    "images": imgs.available()}

    def start(self, path: str, images: str = "compress", force: bool = False, cover: bytes | None = None,
              action: str = "optimize") -> dict:
        with self.lock:
            if self.current:
                return {"started": False, "error": "busy", "current": self.current["path"]}
            self.current = {"path": path, "action": action, "images": images, "started": time.time(), "step": "starting"}
        threading.Thread(target=self._work, args=(path, images, force, cover, action), daemon=True).start()
        return {"started": True}

    def _work(self, path: str, images: str, force: bool, cover: bytes | None, action: str = "optimize") -> None:
        def step(text):
            with self.lock:
                if self.current:
                    self.current["step"] = text
        res = {"path": path, "action": action, "images": images, "at": time.time()}
        try:
            if action == "rewrite":
                res.update(rewrite_file(path, log=step))
            else:
                res.update(optimize_file(path, images, cover=cover, force=force, log=step))
        except Skipped as e:
            res.update(result="skipped", reason=str(e))
        except (tools.ToolError, OSError, mkv.NotMatroska) as e:
            res.update(result="failed", reason=str(e))
        except Exception as e:  # never leave the service stuck "busy"
            res.update(result="failed", reason=f"unexpected error: {e}")
        res["finished"] = time.time()
        with self.lock:
            self.results.append(res)
            del self.results[:-RECENT]
            self.current = None


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "plexcache-optimizer"

        def log_message(self, *a):  # quiet: the app keeps the history
            pass

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            got = self.headers.get("X-Plexcache-Token", "")
            token = svc.token
            if token and hmac.compare_digest(got.encode(), token.encode()):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"ok": True, "service": "optimizer", "version": VERSION, "token": bool(svc.token),
                                        "token_problem": svc.token_problem(), "token_file": svc.token_file,
                                        "roots": len(svc.roots), "uid": os.getuid(), "gid": os.getgid()})
            if self.path == "/api/status":
                if self._authed():
                    self._send(200, svc.status())
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._send(413, {"error": "too large"})
                body = json.loads(self.rfile.read(n) or b"{}")
                path = svc.allowed(str(body.get("path") or ""))
                images = body.get("images") or ("compress" if body.get("keep_images", True) else "remove")
                force = bool(body.get("force", False))
                cover = base64.b64decode(body["cover"], validate=True) if body.get("cover") else None
                if images not in IMAGE_MODES:
                    raise ValueError(images)
            except (ValueError, TypeError, binascii.Error):
                return self._send(400, {"error": "bad request"})
            if not path:
                return self._send(403, {"error": "path is outside the media folders"})
            if self.path == "/api/check":
                return self._send(200, mkv.check(path, images != "remove"))
            if self.path == "/api/optimize":
                return self._send(200, svc.start(path, images, force, cover))
            if self.path == "/api/rewrite":
                return self._send(200, svc.start(path, action="rewrite"))
            self._send(404, {"error": "not found"})

    return Handler


TOKEN_FILE = "/config/optimizer-token"  # made by the app in its config folder


def serve() -> int:
    from .. import zfs
    token = os.environ.get("PLEXCACHE_OPTIMIZER_TOKEN", "")
    token_file = os.environ.get("PLEXCACHE_OPTIMIZER_TOKEN_FILE", TOKEN_FILE)
    if token and len(token) < 16:
        print("PLEXCACHE_OPTIMIZER_TOKEN is too short (at least 16 characters)", flush=True)
        return 2
    env_roots = [r for r in os.environ.get("PLEXCACHE_MEDIA_ROOTS", "").split(os.pathsep) if r]
    # By default, what the YAML mounts read-write: that's what the optimizer may write
    roots = env_roots or zfs.writable_mounts(exclude=("/config", os.path.dirname(token_file)))
    port = int(os.environ.get("PLEXCACHE_OPTIMIZER_PORT") or "8643")
    svc = Service(roots, token, None if token else token_file)
    if svc.status()["missing"]:
        print("missing tools: " + ", ".join(svc.status()["missing"]), flush=True)
    if not roots:
        print("no media folders mounted read-write: nothing can be optimized", flush=True)
    if not svc.token:
        print(f"waiting for the app's token ({token_file}): mount the app's config folder here, read-only", flush=True)
    print(f"optimizer listening on {port}, media under {', '.join(svc.roots) or 'nothing'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(svc)).serve_forever()
    return 0

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
    POST /api/claim      {"dir", "key", "instance", "release"}: claim a file before optimizing it,
                         for systems sharing a library (Syncthing): one small file per system in
                         <dir>/.plexcache/claims/, so two systems never write the same file. The
                         answer lists every claim on that file and the other systems seen there
    POST /api/conflict   {"copy", "original"}: a Syncthing conflict copy next to a file: deleted
                         if it's one of ours — both tagged by the optimizer, every stream the same
    POST /api/folder     the tools' file actions (optimize/files.py): "set_aside" / "restore" / "purge" {"path",
                         "root"}, "link" / "unlink" {"path", "to"}; and
                         turning a library folder into a dataset (optimize/migrate.py), by "action":
                         "probe" {"path"}; "copy" {"path", "to", "mirror"} (a job: progress in
                         /api/status); "pause" / "resume" / "stop"; "compare" {"path", "to"};
                         "rename" {"path", "name"}; "delete" {"path"} — the last two only for the
                         wizard's own .<name>.plexcache-old folders
    POST /api/optimize   {"path", "images", "cover", "force", "tags"}: start optimizing it (one at a time).
    POST /api/convert    {"path", "images", "cover", "tags", "name"}: convert it to <name> MKV-<EXT>.mkv (or "name", the
                         free one the app showed), optimized (convert.py).
                         "tags": "keep" (default), "excessive" or "strip" (optimizer.strips_tags).
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

import base64
import binascii
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION
from . import claims, migrate
from . import images as imgs
from . import mkv, tools
from .optimizer import IMAGE_MODES, Skipped, optimize_file, resolve_conflict, rewrite_file

RECENT = 200  # results kept for the app to read back
MAX_BODY = 48 * 1024 * 1024  # room for a large poster, base64


class Service:
    def __init__(self, roots: list[str], token: str | None = None, token_file: str | None = None):
        self.roots = [os.path.realpath(r) for r in roots]
        self.since = time.time()  # when it started: what it mounted is what was there then (no rslave)
        self._token = token or ""
        self.token_file = token_file
        self._read: tuple = (None, "")  # (the file's mtime, its token)
        self.lock = threading.Lock()
        self.current: dict | None = None
        self.results: list[dict] = []
        self.versions = {n: tools.version(n) for n in tools.NAMES + tools.OPTIONAL}
        self.copy_ctl = migrate.Paused()

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
            return {"version": VERSION, "tools": self.versions, "paused": not self.copy_ctl.go.is_set(),
                    # whether a dataset made while this runs shows up here without a restart (rslave)
                    "propagation": {r: migrate.propagates(r) for r in self.roots}, "missing": [n for n in tools.NAMES if not self.versions[n]],
                    "missing_optional": [n for n in tools.OPTIONAL if not self.versions[n]],
                    "roots": self.roots, "since": self.since, "current": dict(self.current) if self.current else None,
                    "results": list(self.results[-RECENT:]), "uid": os.getuid(), "gid": os.getgid(),
                    "images": imgs.available()}

    def start(self, path: str, images: str = "compress", force: bool = False, cover: bytes | None = None,
              action: str = "optimize", tags: str = "keep", name: str = "", original: str = "") -> dict:
        with self.lock:
            if self.current:
                return {"started": False, "error": "busy", "current": self.current["path"]}
            self.current = {"path": path, "action": action, "images": images, "started": time.time(), "step": "starting"}
        self.tags, self.name = tags, name  # (for the file in hand: one at a time)
        threading.Thread(target=self._work, args=(path, images, force, cover, action, original), daemon=True).start()
        return {"started": True}

    def start_copy(self, src: str, dst: str, mirror: bool) -> dict:
        """Copy a library folder into its new dataset, in the background (one job at a time)."""
        with self.lock:
            if self.current:
                return {"started": False, "error": "busy", "current": self.current["path"]}
            self.current = {"path": src, "action": "copy", "to": dst, "started": time.time(), "step": "copying"}
        self.copy_ctl = migrate.Paused()

        def report(**counts):
            with self.lock:
                if self.current:
                    self.current.update(counts)

        def work():
            res = {"path": src, "action": "copy", "to": dst, "at": time.time()}
            try:
                res.update(migrate.copy_tree(src, dst, self.copy_ctl, report, mirror))
            except InterruptedError:
                res.update(result="stopped")
            except Exception as e:  # never leave the service stuck "busy"
                res.update(result="failed", reason=str(e))
            res["finished"] = time.time()
            with self.lock:
                self.results.append(res)
                del self.results[:-RECENT]
                self.current = None
        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    def _work(self, path: str, images: str, force: bool, cover: bytes | None, action: str = "optimize", original: str = "") -> None:
        def step(text):
            with self.lock:
                if self.current:
                    self.current["step"] = text

        def progress(pct):  # how far through the file (%), for the app's second bar
            with self.lock:
                if self.current:
                    self.current["pct"] = pct
        res = {"path": path, "action": action, "images": images, "at": time.time()}
        try:
            if action == "rewrite":
                res.update(rewrite_file(path, log=step, progress=progress))
            elif action == "conflict":
                res.update(resolve_conflict(path, original, log=step))
            elif action == "convert":
                from .convert import convert_file
                res.update(convert_file(path, images, cover=cover, tags_mode=getattr(self, "tags", "keep"), log=step, progress=progress,
                                        name=getattr(self, "name", "")))
            else:
                res.update(optimize_file(path, images, cover=cover, force=force, log=step, progress=progress,
                                         tags_mode=getattr(self, "tags", "keep")))
        except Skipped as e:
            res.update(result="skipped", reason=str(e))
        except tools.VerifyError as e:
            res.update(result="failed", reason=str(e), verified="failed")  # the new file failed a check: thrown away
        except (tools.ToolError, OSError, mkv.NotMatroska) as e:
            res.update(result="failed", reason=str(e))
        except Exception as e:  # never leave the service stuck "busy"
            res.update(result="failed", reason=f"unexpected error: {e}")
        res["finished"] = time.time()
        with self.lock:
            self.results.append(res)
            del self.results[:-RECENT]
            self.current = None


def folder_action(svc: Service, body: dict) -> tuple[int, dict]:
    """/api/folder: the steps of turning a library folder into a dataset (optimize/migrate.py)."""
    action = body.get("action")
    if action in ("pause", "resume", "stop"):
        {"pause": svc.copy_ctl.go.clear, "resume": svc.copy_ctl.go.set,
         "stop": lambda: (svc.copy_ctl.stop.set(), svc.copy_ctl.go.set())}[action]()
        return 200, {"ok": True}
    path = svc.allowed(str(body.get("path") or ""))
    to = svc.allowed(str(body.get("to") or "")) if body.get("to") else None
    root = svc.allowed(str(body.get("root") or "")) if body.get("root") else None
    if not path or (body.get("to") and not to) or (body.get("root") and not root):
        return 403, {"error": "path is outside the media folders"}
    try:
        if action == "mark":  # this system's marker in a library folder, with what it tells the other systems (peers.py)
            from . import claims
            info = body.get("info") if isinstance(body.get("info"), dict) else {}
            if len(json.dumps(info)) > 8192:
                return 400, {"error": "too much to publish"}
            return 200, {"marked": claims.mark(path, str(body.get("instance") or ""), info)}
        if action == "make_real":  # a link made a file of its own (files.py)
            from . import files
            return 200, files.make_real(path)
        if action in ("set_aside", "restore", "purge", "link", "unlink", "copy_from"):  # the tools' file actions (files.py)
            from . import files
            if action in ("set_aside", "restore", "purge"):
                if not root:
                    return 400, {"error": "root is needed"}
                return 200, {"set_aside": lambda: files.set_aside(root, path), "restore": lambda: files.put_back(root, path),
                             "purge": lambda: files.purge(root, path)}[action]()
            if not to:
                return 400, {"error": "to is needed"}
            return 200, {"link": lambda: files.link(path, to), "unlink": lambda: files.unlink(path, to),
                         "copy_from": lambda: files.copy_from(path, to)}[action]()
        if action == "probe":
            return 200, migrate.probe(path)
        if action in ("copy", "compare"):
            if not to:
                return 400, {"error": "to is needed"}
            return 200, svc.start_copy(path, to, bool(body.get("mirror"))) if action == "copy" else migrate.compare(path, to)
        if action == "rename":
            return 200, {"path": migrate.rename_folder(path, str(body.get("name") or ""))}
        if action == "delete":
            migrate.delete_old(path)
            return 200, {"deleted": True}
        if action == "delete_original":  # a converted file's original, once Plex has its MKV (path: the original, to: the MKV)
            from .convert import delete_original
            return 200, delete_original(path, to or "")
        if action == "remove_converted":  # a converted file's MKV, on a revert (path: the MKV, to: the original, which stays)
            from .convert import remove_converted
            return 200, remove_converted(path, to or "")
        if action == "cleanup":  # a work folder left by an optimizer stopped mid-way (only those)
            from .optimizer import WORK
            if not WORK.match(os.path.basename(path.rstrip("/"))) or not os.path.isdir(path) or os.path.islink(path):
                return 400, {"error": "not an optimizer work folder"}
            import shutil
            shutil.rmtree(path)
            return 200, {"deleted": True}
    except (OSError, ValueError) as e:
        return 400, {"error": str(e)}
    return 404, {"error": "not found"}


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "plexcache-optimizer"

        def log_message(self, format, *args):  # quiet: the app keeps the history
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
                if self.path == "/api/folder":
                    return self._send(*folder_action(svc, body))
                if self.path == "/api/claim":
                    folder = svc.allowed(str(body.get("dir") or ""))
                    if not folder:
                        return self._send(403, {"error": "folder is outside the media folders"})
                    return self._send(200, claims.claim(folder, str(body.get("key") or ""), str(body.get("instance") or ""),
                                                        bool(body.get("release"))))
                path = svc.allowed(str(body.get("path") or ""))
                images = body.get("images") or ("compress" if body.get("keep_images", True) else "remove")
                force = bool(body.get("force", False))
                tags = body.get("tags") or "keep"
                if tags not in ("keep", "excessive", "strip"):
                    raise ValueError(tags)
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
                return self._send(200, svc.start(path, images, force, cover, tags=tags))
            if self.path == "/api/convert":
                name = os.path.basename(str(body.get("name") or ""))  # (the MKV's name, as the app showed it)
                return self._send(200, svc.start(path, images, False, cover, action="convert", tags=tags, name=name))
            if self.path == "/api/rewrite":
                return self._send(200, svc.start(path, action="rewrite"))
            if self.path == "/api/conflict":
                original = svc.allowed(str(body.get("original") or ""))
                if not original:
                    return self._send(403, {"error": "path is outside the media folders"})
                return self._send(200, svc.start(path, action="conflict", original=original))
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

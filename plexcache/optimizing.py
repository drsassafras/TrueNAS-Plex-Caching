"""The app's side of the optimizer: each library's choices, the record of what the optimizer
did, which files still need it, and a library at a time sent to it.

The app only reads media; the optimizer service (the second container, plexcache/optimize)
writes it. Per library (Optimizer page, settings optimizer.libraries):

* **TrueNAS compression & record size** (on unless switched off) — any file not yet in its
  dataset's record size, or not known to be written with the dataset's current compression
  (only files the optimizer wrote are known), is written again byte for byte
  (optimizer.rewrite_file), so it takes the dataset's current settings;
* **Optimize MKV files** (off unless switched on) — MKV files are remuxed into the layout that
  suits caching (optimizer.optimize_file), with the library's **cover art** choice: "remove"
  (the default), "compress" (keep it, losslessly compressed) or "plex" (Plex's poster, losslessly
  compressed), or "unmatched" (the default: Plex's poster for items Plex hasn't matched — for
  those, the file is the most reliable place for it — and none for the rest). An optimized file
  is also written in the dataset's current settings.

**The record** (optimized.json in the config folder) has, for every file the optimizer finished:
when, what it did last ("optimized", "already" when an MKV was laid out right and left as it
was, "rewritten"), the sha256 of the file it left, the optimizer's version, and the file's size,
mtime and inode then; what the file was written with (compression, record size) when the
optimizer wrote it; and for MKV files laid out by it, the version, cover art choice and Plex
poster ("layout"). A record counts only while the file is still that same file (size, mtime and
inode unchanged), so telling which files are done reads none of them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

from . import zfs, zfslog
from .i18n import M, join as join_names, msg
from .jobs import Job
from .optimize import VERSION, compare as version_of, convert, mkv
from .optimize.optimizer import strips_tags
from .optimize.optimizer import COVER_PREFIX, WORK, conflict_original
from .plex import PlexError
from .projection import family

IMAGE_MODES = ("unmatched", "remove", "compress", "plex")
DEFAULTS = {"sync": True, "mkv": False, "images": "unmatched", "tags": "keep", "convert": False}  # each library's choices unless set
TAG_MODES = ("keep", "excessive", "strip")
SYNC_PROBLEMS = ("compression", "blocks")  # fixed by writing the file again
RECORD = "optimized.json"
ZFS_RECORD = "zfs-record.json"
# The optimizer's own health check, for its service in the YAML (images older than the optimizer
# check the web app's port instead, so Docker never sees it healthy)
HEALTHCHECK_YAML = ('    healthcheck:\n'
                    '      test: ["CMD", "python", "-m", "plexcache.healthcheck"]')
POSTER_SIZES = "poster-sizes.json"  # Plex's posters' sizes, by their paths (Library Health's estimates)
RECENT_SECONDS = 600  # files written this recently may still be being imported (Sonarr, Radarr…)
STATUS_TTL = 15


DEFAULT_URL = "http://plex-cache-optimizer:8643"  # the second service in the app's YAML
TOKEN_FILE = "optimizer-token"  # in the config folder, which the optimizer mounts read-only
# How a missing service shows up: its name doesn't resolve (no such service in the YAML), or nothing listens
NOT_THERE = ("name or service not known", "nodename nor servname", "name resolution", "no address associated",
             "name does not resolve", "connection refused", "not set up")


def not_set_up(error) -> bool:
    """Whether an error reaching the optimizer means it isn't there at all (rather than broken)."""
    return any(x in str(error).lower() for x in NOT_THERE)


class OptimizerClient:
    """Talks to the optimizer service: at PLEXCACHE_OPTIMIZER_URL (the second service's name in
    the YAML by default), with the token the app made (PLEXCACHE_OPTIMIZER_TOKEN overrides it)."""

    def __init__(self, url: str | None = None, token: str | None = None):
        self.url = (url if url is not None else os.environ.get("PLEXCACHE_OPTIMIZER_URL") or DEFAULT_URL).rstrip("/")
        self.token = os.environ.get("PLEXCACHE_OPTIMIZER_TOKEN") or token or ""
        self._status: tuple[float, dict] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def ready(self) -> bool:
        """It answered the last time it was asked (within a few seconds)."""
        return self.configured and not self.status().get("error")

    def _call(self, path: str, body: dict | None = None, timeout: float = 5) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Plexcache-Token": self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def status(self, fresh: bool = False) -> dict:
        """The service's status, or {"error": why}. Kept for a few seconds."""
        if not self.configured:
            return {"error": "not set up"}
        if not fresh and self._status and time.time() - self._status[0] < STATUS_TTL:
            return self._status[1]
        try:
            st = self._call("/api/status")
        except (OSError, ValueError, urllib.error.URLError) as e:
            st = {"error": str(getattr(e, "reason", e))}
        self._status = (time.time(), st)
        return st

    def healthz(self) -> dict:
        """The optimizer's /healthz (no token needed), or {"error": why}."""
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url + "/healthz"), timeout=5) as r:
                return json.loads(r.read() or b"{}")
        except (OSError, ValueError, urllib.error.URLError) as e:
            return {"error": str(getattr(e, "reason", e))}

    def claim(self, folder: str, key: str, instance: str, release: bool = False) -> dict:
        return self._call("/api/claim", {"dir": folder, "key": key, "instance": instance, "release": release})

    def conflict(self, copy: str, original: str) -> dict:
        return self._call("/api/conflict", {"path": copy, "original": original})

    def rewrite(self, path: str) -> dict:
        return self._call("/api/rewrite", {"path": path}, timeout=60)

    def convert(self, path: str, images: str, cover: bytes | None = None, tags: str = "keep") -> dict:
        body = {"path": path, "images": images, "tags": tags}
        if cover:
            body["cover"] = base64.b64encode(cover).decode()
        return self._call("/api/convert", body, timeout=60)

    def delete_original(self, path: str, new: str) -> dict:
        return self._call("/api/folder", {"action": "delete_original", "path": path, "to": new}, timeout=60)

    def file_action(self, action: str, path: str, to: str = "", root: str = "") -> dict:
        """The tools' file actions (optimize/files.py): "set_aside" / "restore" / "purge" (``root``: the
        library folder, whose trash it is), "link" (``to``: the new path), "unlink" (``to``: the path kept)."""
        return self._call("/api/folder", {"action": action, "path": path, "to": to, "root": root}, timeout=120)

    def optimize(self, path: str, images: str, force: bool, cover: bytes | None = None, tags: str = "keep") -> dict:
        body = {"path": path, "images": images, "force": force, "tags": tags}
        if cover:
            body["cover"] = base64.b64encode(cover).decode()
        return self._call("/api/optimize", body, timeout=60)


class Optimizing:
    """The record of what the optimizer did, per-library summaries, and the "optimize a library" job."""

    IDLE_LIMIT = 30  # seconds the optimizer may show nothing running (and no result) before it counts as restarted

    def __init__(self, app, client: OptimizerClient | None = None):
        self.app = app
        self.client = client or OptimizerClient(token=self.token())
        self.lock = threading.Lock()
        saved = app._load_json(RECORD) or {}
        # path -> {size, mtime_ns, ino, ours (our tags), problems (layout), images, and the last result}:
        # what each file's tags said, kept with its identity so it's read once (a cache: rebuilt if lost)
        self.record: dict[str, dict] = (saved.get("files") or {}) if saved.get("version") == 2 else {}
        self.zfs = zfslog.ZfsLog(lambda: app._load_json(ZFS_RECORD), lambda d: app._save_json(ZFS_RECORD, d))
        # a saved optimize job (a restart in the middle of one) is started again by the shared jobs
        app.jobs.register("optimize", lambda args, saved: OptimizeJob(self, args.get("library"), args.get("mode") or "optimize", saved))
        self.retries: list[dict] = []  # failed files to try again, after the file in hand (not queued: retry())
        self._retrying = False  # the worker that does them when no optimizing job runs
        self.summary: dict[str, dict] = {}
        self.surveying = False
        self.setup: dict = {"ready": False, "steps": [], "at": 0}  # diagnose(): what's left to set up
        self._diagnosing = False

    def token(self) -> str:
        """The token the optimizer accepts, made once and kept in the config folder: the optimizer
        reads it there (it mounts the folder read-only), so nobody has to copy it."""
        path = os.path.join(self.app.config_dir, TOKEN_FILE)
        try:
            with open(path, encoding="utf-8") as fh:
                token = fh.read().strip()
            if len(token) >= 32:
                return token
        except OSError:
            pass
        token = secrets.token_hex(24)
        try:
            # readable by the apps group too (568): the optimizer may run as another user in it
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
        except OSError:
            pass  # a new one next time, then
        return token

    # ------------------------------------------------------------ setting it up
    def _containers(self) -> dict | None:
        """This app's containers, as TrueNAS sees them: {"app", "me", "optimizer"} with each
        container's service name and state, or None when it can't be asked (no TrueNAS API key,
        or a key without permission to see apps)."""
        me = zfs.own_container_id()
        if not me:
            return None
        try:
            with self.app.truenas() as tn:
                apps = tn.apps(config=True)
        except Exception:  # noqa: BLE001 — not connected, or not allowed: the checks go on without it
            return None
        for a in apps:
            conts = (a.get("active_workloads") or {}).get("container_details") or []
            mine = next((c for c in conts if str(c.get("id") or "").startswith(me[:12])), None)
            if not mine:
                continue
            others = [c for c in conts if c is not mine]
            opt = next((c for c in others if "optimiz" in str(c.get("service_name") or "")), None)
            if opt:
                # its volumes and health check from the YAML, so only missing lines are suggested
                # (read here, never shown: the YAML also holds the web UI's password)
                svc = ((a.get("config") or {}).get("services") or {}).get(opt.get("service_name")) or {}
                vols = [str(v.get("target") if isinstance(v, dict) else str(v).split(":")[1] if ":" in str(v) else v)
                        for v in svc.get("volumes") or []]
                opt = dict(opt, targets=vols, healthcheck=bool(svc.get("healthcheck")), has_config="config" in a)
            return {"app": a.get("name"), "me": mine.get("service_name"), "optimizer": opt}
        return None

    def diagnose(self) -> dict:
        """What has to be true before the optimizer works, checked in order: in the YAML,
        running, answering, holding the app's token, accepting it, with its tools, with media to
        write. {"ready", "steps": [{"id", "ok", "title", "detail", "fix", "cmd"}], "at"} — the
        steps that pass, then the first that doesn't (the ones after it can't be checked yet)."""
        from . import checks
        steps = []

        def step(sid, ok, title, detail="", fix="", cmd=""):
            steps.append({"id": sid, "ok": ok, "title": title, "detail": detail, "fix": fix, "cmd": cmd})
            return ok

        appname = "plex-cache"
        code = checks.optimizer_code_mount()  # this app's code mounted over its image (a test copy)
        tn = self._containers()
        if tn:
            appname = tn["app"] or appname
            opt = tn["optimizer"]
            if not opt:
                step("yaml", False, M("Add the optimizer to the app’s YAML"),
                     M("{app} has no optimizer service.", app=appname),
                     M("In TrueNAS, Apps → {app} → Edit. Paste the lines below under services: (next to {service}), "
                       "and Save. The volumes are filled in from {service}'s own: its settings folder, read-only (the "
                       "optimizer finds the app’s token there), and your media, writable — the optimizer can only change "
                       "what’s mounted, so leave out any folder it shouldn’t touch.", app=appname, service=tn["me"] or appname),
                     checks.optimizer_yaml(self.app))
                return self._done(steps)
            step("yaml", True, M("In the app’s YAML"), M("Service {service}.", service=opt.get("service_name")))
            state = str(opt.get("state") or "").lower()
            # TrueNAS shows a running container's health instead while it has one: "starting" until its
            # health check first passes, "unhealthy" when it keeps failing — the program is running either way
            health = state if state in ("starting", "unhealthy") else None
            if state not in ("running", "healthy", "starting", "unhealthy"):
                needs_code = code and opt.get("has_config") and "/app/plexcache" not in opt.get("targets", [])
                fix = (M("This app runs code mounted over its image (a test copy?). The optimizer needs the same code: "
                         "add this line to its volumes, in Apps → {app} → Edit:", app=appname) if needs_code else
                       M("Its logs say why: in TrueNAS, Apps → {app} → the optimizer’s container → View Logs. Most often "
                         "the image is older than the optimizer: update it to the latest.", app=appname))
                step("running", False, M("The optimizer isn’t running"),
                     M("TrueNAS says it’s {state}: it stopped right after starting.", state=state or "?"), fix,
                     f"      - {code}:/app/plexcache:ro" if needs_code else "")
                return self._done(steps)
            step("running", True, M("Running"))
            if not os.environ.get("PLEXCACHE_OPTIMIZER_URL") and opt.get("service_name"):
                self.client.url = f"http://{opt['service_name']}:8643"  # renamed in the YAML: follow it
        else:
            health = None
        hz = self.client.healthz()
        if hz.get("error"):
            if not tn and not_set_up(hz["error"]):
                step("yaml", False, M("Add the optimizer to the app’s YAML"),
                     M("Nothing answers at {url}. (With a TrueNAS API key that can see apps, this page can tell whether it’s "
                       "in the YAML and running.)", url=self.client.url),
                     M("In TrueNAS, Apps → plex-cache → Edit. Paste the lines below under services: (next to plex-cache), "
                       "and Save. If it’s already there, check it’s running: Apps → plex-cache, and its logs."),
                     checks.optimizer_yaml(self.app))
            else:
                step("answers", False, M("The optimizer doesn’t answer"),
                     M("It’s running, but the app couldn’t reach it at {url}: {why}", url=self.client.url, why=hz["error"]),
                     M("It listens on port 8643 inside the app. If you set PLEXCACHE_OPTIMIZER_URL or "
                       "PLEXCACHE_OPTIMIZER_PORT, check they match."))
            return self._done(steps)
        if hz.get("service") != "optimizer":
            step("answers", False, M("Something else answers at {url}", url=self.client.url),
                 M("It isn’t the optimizer: it may run an image older than the optimizer."),
                 M("In TrueNAS, Apps → {app}: update its image to the latest.", app=appname))
            return self._done(steps)
        step("answers", True, M("Answering"), M("Optimizer version {version}.", version=hz.get("version")))
        if health == "starting" and tn and tn["optimizer"].get("healthcheck"):
            step("health", False, M("Still starting"),
                 M("It answers, and its health check hasn’t run yet. TrueNAS shows the app as deploying until it does."),
                 M("Check again in a minute."))
            return self._done(steps)
        if health and tn and tn["optimizer"].get("healthcheck"):
            step("health", False, M("Its health check doesn’t pass"),
                 M("The optimizer answers, but TrueNAS says it’s {state}.", state=health),
                 M("Check its health check in the YAML is the one below, and its logs (Apps → {app} → the optimizer’s "
                   "container → View Logs).", app=appname), HEALTHCHECK_YAML)
            return self._done(steps)
        if health:
            # It answers, but Docker's health check doesn't pass: TrueNAS then keeps the app "Deploying"
            step("health", False, M("Its health check doesn’t pass"),
                 M("The optimizer answers, but TrueNAS says it’s {state}: its image’s built-in health check looks at the "
                   "web app’s port, not the optimizer’s — the image is older than the optimizer. TrueNAS keeps the app "
                   "deploying until it passes.", state=health),
                 M("In Apps → {app} → Edit, add these lines to the optimizer service (next to its volumes), or update the "
                   "image to the latest:", app=appname),
                 HEALTHCHECK_YAML)
            return self._done(steps)
        if not hz.get("token"):
            problem = hz.get("token_problem")
            why = {"missing": M("There’s no {file} in it: the app’s settings folder isn’t mounted into it at /config.",
                                file=hz.get("token_file") or "/config/optimizer-token"),
                   "unreadable": M("It can’t read {file}: it runs as user {uid}, which can’t read files in the app’s "
                                   "settings folder.", file=hz.get("token_file") or "", uid=hz.get("uid")),
                   }.get(problem, M("The token file is empty."))
            fix = (M("In Apps → {app} → Edit, run the optimizer as user 568 (apps), or add it to that group "
                     "(group_add: [\"568\"]).", app=appname) if problem == "unreadable" else
                   M("In Apps → {app} → Edit, add the app’s settings folder to the optimizer’s volumes, read-only:", app=appname))
            config = zfs.host_path(self.app.config_dir) or "/mnt/tank/apps/plex-cache"
            step("token", False, M("The optimizer doesn’t have the app’s token"), why, fix,
                 "" if problem == "unreadable" else f"      - {config}:/config:ro")
            return self._done(steps)
        st = self.client.status(fresh=True)
        if st.get("error"):
            wrong = "401" in st["error"] or "nauthorized" in st["error"]
            step("token", False, M("The optimizer has a different token") if wrong else M("The optimizer doesn’t answer"),
                 M("It reads a token, but not the one this app uses: PLEXCACHE_OPTIMIZER_TOKEN is set in one service only, "
                   "or its settings folder isn’t this app’s.") if wrong else str(st["error"]),
                 M("In Apps → {app} → Edit: mount the same settings folder in both services, and remove "
                   "PLEXCACHE_OPTIMIZER_TOKEN (or set it the same in both).", app=appname) if wrong else "")
            return self._done(steps)
        step("token", True, M("Has the app’s token"))
        if st.get("missing"):
            step("tools", False, M("The optimizer is missing tools"),
                 M("It can’t find {tools}: its image is older than the optimizer.", tools=join_names(st["missing"])),
                 # in a test copy the code is mounted over the image, but programs like these come only with the image
                 M("This app’s code is mounted over its image (a test copy), but MKVToolNix and ffmpeg come with the image "
                   "itself, not the code: it needs an image built from this code. Then, in TrueNAS, Apps → {app}: update "
                   "its image.", app=appname) if code else
                 M("In TrueNAS, Apps → {app}: update its image to the latest.", app=appname))
            return self._done(steps)
        step("tools", True, M("Has its tools"))
        roots = st.get("roots") or []
        if not roots:
            step("media", False, M("The optimizer has no media to work on"),
                 M("No media folder is mounted into it writable, so it can’t change any file."),
                 M("In Apps → {app} → Edit, add your media folders to the optimizer’s volumes, the same as {app}'s but "
                   "without :ro.", app=appname))
            return self._done(steps)
        step("media", True, M("Can write {roots}", roots=join_names(roots)))
        return self._done(steps)

    def _done(self, steps: list) -> dict:
        with self.lock:
            self.setup = {"ready": bool(steps) and all(x["ok"] for x in steps) and steps[-1]["id"] == "media",
                          "steps": steps, "at": time.time()}
            return dict(self.setup)

    def diagnose_soon(self, max_age: float = 60) -> None:
        """Diagnose again in the background when the last one is older than ``max_age`` and the
        optimizer isn't ready (the Optimizer page shows the result)."""
        with self.lock:
            if self._diagnosing or self.setup["ready"] or time.time() - self.setup["at"] < max_age:
                return
            self._diagnosing = True

        def work():
            try:
                self.diagnose()
            except Exception:  # noqa: BLE001 — shown as "not checked" until the next try
                pass
            finally:
                with self.lock:
                    self._diagnosing = False
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ per file
    def options(self, library_key: str) -> dict:
        """The library's choices: {"sync", "mkv", "images", "tags"} (DEFAULTS for what isn't set;
        an audio library's cover art is always kept)."""
        lib = (self.app.settings.section("optimizer")["libraries"] or {}).get(str(library_key)) or {}
        out = dict(DEFAULTS, **{k: v for k, v in lib.items() if k in DEFAULTS})
        if out["images"] not in IMAGE_MODES:
            out["images"] = DEFAULTS["images"]
        if out["tags"] not in TAG_MODES:
            out["tags"] = DEFAULTS["tags"]
        out["convert"] = out["convert"] is True
        info = getattr(self.app, "library_info", lambda k: None)(library_key) or {}
        if info.get("kind") == "audio":
            # music and audiobooks: the file is the place for cover art (players, other libraries and
            # iTunes read it there; Plex's own lookups for music are thin) — always kept, compressed
            out["images"] = "compress"
            out["convert"] = False  # (converting is for video)
        return out

    def convert_types(self) -> dict:
        """Which kinds of container are converted to MKV, in the libraries that convert."""
        return dict(convert.DEFAULT_TYPES, **(self.app.settings.section("optimizer").get("convert_types") or {}))

    def converts(self, path: str, opts: dict) -> str | None:
        """The file's container type (convert.TYPES) if its library converts that kind, else None."""
        kind = convert.type_of(path)
        return kind if kind and opts.get("convert") and self.convert_types().get(kind) else None

    def convert_skipped(self, path: str, st: os.stat_result) -> str | None:
        """Why the optimizer last left this file unconverted (what MKV wouldn't keep), while the file is
        unchanged since; None otherwise."""
        with self.lock:
            e = self.record.get(path) or {}
        skip = e.get("convert_skip") or {}
        if (skip.get("size"), skip.get("mtime_ns"), skip.get("ino")) == (st.st_size, st.st_mtime_ns, st.st_ino):
            return skip.get("reason")
        return None

    @staticmethod
    def cover_for(images: str, poster: str, matched: bool | None) -> str:
        """What a file's cover art should be under a library's choice: "remove", "compress" or
        "plex". "unmatched": Plex's poster for an item Plex hasn't matched (the file is the most
        reliable place to keep it), none for a matched one; when Plex didn't say, or has no
        poster, what's there is kept, compressed."""
        if images != "unmatched":
            return images
        if matched is True:
            return "remove"
        return "plex" if matched is False and poster else "compress"

    @classmethod
    def images_mode(cls, choice: str, poster: str, matched: bool | None, ours: dict) -> str:
        """What the optimizer is asked to do with a file's cover art when it first optimizes it.
        Cover art is set then, once: a later change of choice doesn't touch optimized files
        (Sync Cover Art does that, on request). A Plex poster already in the file (our
        PLEXCACHE_COVER tag) is kept, unless the choice is to strip every image."""
        want = cls.cover_for(choice, poster, matched)
        has_plex = bool((ours or {}).get("PLEXCACHE_COVER"))
        if want == "plex":
            return "keep" if has_plex else ("plex" if poster else "compress")
        if want == "remove":
            return "keep" if has_plex and choice != "remove" else "remove"
        return "keep" if has_plex else "compress"

    def _mkv_info(self, path: str, st: os.stat_result) -> dict:
        """What the file's layout and our tags say ({"problems", "ours", "images"}), read once per
        file (a few small reads) and then kept with its identity: after that, a stat() is enough."""
        ident = (st.st_size, st.st_mtime_ns, st.st_ino)
        with self.lock:
            cached = self.record.get(path)
        if cached and (cached.get("size"), cached.get("mtime_ns"), cached.get("ino")) == ident and "foreign_tags" in cached:
            return cached
        c = mkv.check(path, True)
        info = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino, "ours": c.get("ours") or {},
                "problems": [p for p in c.get("problems") or [] if p != "images"], "images": c.get("images") or 0,
                "tags_size": c.get("tags_size", 0), "foreign_tags": c.get("foreign_tags", False)}
        with self.lock:
            self.record[path] = dict(cached or {}, **info)
        return info

    def file_status(self, path: str, library_key: str, rec: dict, poster: str = "", matched: bool | None = None) -> dict:
        """{"applies", "mkv", "problems", "compression"} for one file under its library's choices.

        Optimize MKV files: "new" (not tagged by the optimizer), "version" (by an older
        optimizer: 0.1 → 0.2), "tags" (tags the library's tag choice strips), "layout" (tagged, but no longer laid out right — remuxed since by
        something else). A file tagged by a newer optimizer is left alone. TrueNAS compression &
        record size: "compression" (the ZFS record says Unknown or NotOptimal), "blocks" (not in
        blocks of the dataset's record size)."""
        opts = self.options(library_key)
        is_mkv = path.lower().endswith(mkv.MKV_EXT)
        kind = None if is_mkv else self.converts(path, opts)
        if not (opts["sync"] or (opts["mkv"] and is_mkv) or kind) or conflict_original(path):
            return {"applies": False, "mkv": is_mkv}
        try:
            st = os.stat(path)
        except OSError:
            return {"applies": False, "missing": True, "mkv": is_mkv}
        if kind:
            # converted already (waiting for Plex, or by another system sharing the files): nothing to do
            # here — it goes once Plex has its MKV
            conv = self.app.conversions.get(path) if hasattr(self.app, "conversions") else None
            if conv or os.path.exists(convert.target_of(path)):
                return {"applies": True, "mkv": False, "problems": [], "converted": (conv or {}).get("state") or "there"}
            why = self.convert_skipped(path, st)
            if not why:
                return {"applies": True, "mkv": False, "problems": ["convert"], "convert": kind}
            if not opts["sync"]:
                return {"applies": True, "mkv": False, "problems": [], "convert_skipped": why}
        problems, comp = [], None
        if opts["mkv"] and is_mkv:
            info = self._mkv_info(path, st)
            v = version_of((info.get("ours") or {}).get("PLEXCACHE_OPTIMIZER"))
            if v == "none":
                problems.append("new")
            elif v == "older":
                problems.append("version")
            elif v == "same" and info.get("problems"):
                problems.append("layout")
            elif v == "same" and strips_tags(opts["tags"], info):
                problems.append("tags")  # tags the library's choice strips
        if opts["sync"]:
            comp = self.zfs.status(path, st.st_ino, rec.get("dataset"))
            if comp in zfslog.NEEDS_WORK:
                problems.append("compression")
            rs = rec.get("rs")
            if rs and st.st_size > st.st_blksize and st.st_blksize != rs:
                problems.append("blocks")
        return {"applies": True, "mkv": is_mkv, "problems": problems, "compression": comp}

    def zfs_config(self, library_key: str, rec: dict) -> dict:
        """Whether the library's dataset is set up for the best result, before files are written
        with it: {"status": "ideal" | "pass" | "warn" | "info", "issues": [what to change]}. Ideal:
        the recommended record size and ZSTD-3; Pass: the second-best record size, or ZSTD 4–19;
        Warn: either is off (writing files now would mean writing them again after the change)."""
        from . import checks
        r = checks.library_record(self.app, library_key)
        comp = rec.get("compression")
        g = zfslog.grade(comp)
        comp_status = (None if g is None else "ideal" if g == zfslog.GOOD and comp in ("zstd", "zstd-3")
                       else "pass" if g == zfslog.GOOD else "warn")
        issues = []
        if r["status"] == "warn":
            issues.append(M("Record size {size}: {best} suits it best", size=checks._fmt_rs(r["rs"]), best=checks._fmt_rs(r["best"])))
        if comp_status == "warn":
            issues.append(M("Compression {value}: use ZSTD-3", value=comp))
        statuses = [r["status"] if r["status"] != "info" else None, comp_status]
        if "warn" in statuses:
            status = "warn"
        elif None in statuses:
            status = "info"  # not known yet (no TrueNAS API, or the library's files not read yet)
        else:
            status = "ideal" if statuses == ["ideal", "ideal"] else "pass"
        return {"status": status, "issues": issues, "rs": r.get("rs"), "best": r.get("best"), "compression": comp}

    def survey_soon(self) -> None:
        """survey() in the background: the first time, every MKV file's tags are read (a few small
        reads each), which can take minutes on a large library."""
        threading.Thread(target=self.survey, daemon=True).start()

    def survey(self) -> None:
        """How far each library is, under its choices; the ZFS record brought up to date; Syncthing
        conflict copies found."""
        with self.lock:
            if self.surveying:
                return
            self.surveying = True
        try:
            records = self.app.library_records()
            self.zfs.observe_datasets({r["dataset"]: {"rs": r.get("rs"), "compression": r.get("compression")}
                                       for r in records.values() if r.get("dataset")})
            out: dict[str, dict] = {}
            seen, dirs = set(), {}
            # Every library that isn't unticked in Media Mappings: optimizing doesn't depend on caching
            off = {str(lib["key"]) for lib in getattr(self.app, "libraries", []) or [] if lib.get("excluded")}
            for f in list(self.app.files):
                key = str(f.library_key)
                if key in off:
                    continue
                rec = records.get(key) or {}
                path = self.app.map_path(f.path)
                seen.add(path)
                dirs.setdefault(key, set()).add(os.path.dirname(path))
                s = self.file_status(path, key, rec, getattr(f, "poster", ""), getattr(f, "matched", None))
                lib = out.get(key)
                if lib is None:
                    lib = out[key] = {"files": 0, "mkv": 0, "mkv_done": 0, "sync_todo": 0, "todo": 0,
                                      "compression_setting": rec.get("compression"), "dataset": rec.get("dataset"),
                                      "why": {}, "compression": {}, "conflicts": [], "zfs": self.zfs_config(key, rec)}
                if s.get("missing") or conflict_original(path):
                    continue
                lib["files"] += 1
                lib["mkv"] += bool(s.get("mkv"))
                probs = s.get("problems") or []
                if s.get("mkv") and self.options(key)["mkv"] and not any(p not in SYNC_PROBLEMS for p in probs):
                    lib["mkv_done"] += 1
                lib["sync_todo"] += any(p in SYNC_PROBLEMS for p in probs)
                lib["todo"] += bool(probs)
                for p in probs:
                    lib["why"][p] = lib["why"].get(p, 0) + 1
                if s.get("compression"):
                    lib["compression"][s["compression"]] = lib["compression"].get(s["compression"], 0) + 1
            # Syncthing conflict copies next to the library's files (two systems wrote a file at once)
            # …and work folders left by an optimizer stopped mid-way (a restart): swept up when a job starts
            for key, ds in dirs.items():
                found, left = [], []
                for d in ds:
                    try:
                        names = os.listdir(d)
                    except OSError:
                        continue
                    for n in names:
                        if WORK.match(n):
                            left.append(os.path.join(d, n))
                            continue
                        orig = conflict_original(os.path.join(d, n))
                        if orig and os.path.exists(orig):
                            found.append({"copy": os.path.join(d, n), "original": orig})
                if key in out:
                    out[key]["conflicts"] = sorted(found, key=lambda c: c["copy"])
                    out[key]["leftovers"] = sorted(left)
            with self.lock:
                for path in [p for p in self.record if p not in seen]:
                    del self.record[path]  # gone from the libraries (it's a cache: read again if it comes back)
                self.summary = out
            self.zfs.collect()
            self.zfs.save()
            self.save()
        finally:
            with self.lock:
                self.surveying = False

    def note(self, path: str, key: str, r: dict, rec: dict) -> None:
        """A file the optimizer finished: its tags and identity in the record (so it isn't read
        again), and in the ZFS record the compression it was written with."""
        ident = r.get("file") or {}
        if not ident.get("ino"):
            return
        result = r.get("result")
        with self.lock:
            entry = dict(self.record.get(path) or {})
            entry.update({k: ident.get(k) for k in ("size", "mtime_ns", "ino")}, at=r.get("finished") or time.time(),
                         result=result, sha256=r.get("sha256") or entry.get("sha256"))
            if result in ("optimized", "already"):
                entry.update(ours=r.get("tags") or {}, problems=[], images=r.get("images_after", entry.get("images", 0)))
                if r.get("tags_stripped"):
                    entry.update(foreign_tags=False, tags_size=0)  # (only ours left: as good as nothing)
            self.record[path] = entry
        if result in ("optimized", "rewritten"):
            self.zfs.written(path, ident["ino"], rec.get("dataset"), rec.get("compression"))
        self.zfs.save()
        self.save()

    def save(self) -> None:
        with self.lock:
            data = {"version": 2, "files": dict(self.record)}
        self.app._save_json(RECORD, data)

    # ------------------------------------------------------------ the job
    def start_library(self, library_key: str, mode: str = "optimize", resumed: dict | None = None) -> bool:
        """Do every file of a library that needs it under its choices, one at a time, in the
        background, as a file job (jobs.py: progress, pause, resume after a restart, one at a time).
        ``mode`` "covers": Sync Cover Art — bring optimized files' cover art in line with the
        library's choice and Plex's posters now. False when another job runs (queue() waits for it)."""
        if not self.client.ready():
            return False
        return self.app.jobs.start(OptimizeJob(self, str(library_key), mode, resumed))

    def queue(self, library_key: str, mode: str = "optimize") -> str | None:
        """Start the job now, or queue it after the ones before it: "started", "queued", or None
        (not set up, or the same work is queued or running already)."""
        if not self.client.ready():
            return None
        title = (self.app.library_info(str(library_key)) or {}).get("title")
        item = self.app.jobs.enqueue("optimize", {"library": str(library_key), "mode": mode},
                                     library=str(library_key), mode=mode, title=title)
        if item is None:
            return None
        return "started" if all(q["id"] != item["id"] for q in self.app.jobs.queued()) else "queued"

    def set_paused(self, paused: bool) -> bool:
        return self.app.jobs.pause(paused)

    def resume_job(self, wait: float = 600) -> None:
        """After a restart: the file job that was running, started again where it was."""
        self.app.jobs.resume_saved(ready=self.client.ready, wait=wait)

    def library_root(self, plex_path: str) -> str | None:
        """The library folder (as this app sees it) a file is in: where its claims go."""
        best = None
        for lib in getattr(self.app, "libraries", []) or []:
            for loc in lib.get("locations") or []:
                loc = loc.rstrip("/") or "/"
                if plex_path.startswith(loc + "/") and (best is None or len(loc) > len(best)):
                    best = loc
        return self.app.map_path(best) if best else None

    def _run(self, job: "OptimizeJob") -> None:
        key, mode = job.state["library"], job.state["mode"]
        app = self.app
        name = (app.library_info(key) or {}).get("title") or key
        self._posters = {}  # Plex's posters as they are now: fetched again for every run
        try:
            rec = app.library_records().get(key) or {}
            if rec.get("dataset") and rec["dataset"] not in self.zfs.datasets:  # not seen by a check yet
                self.zfs.observe_datasets({rec["dataset"]: {"rs": rec.get("rs"), "compression": rec.get("compression")}})
            # Work folders left by an optimizer stopped mid-way (a restart): nobody's now
            for w in list((self.summary.get(key) or {}).get("leftovers") or []):
                try:
                    self.client._call("/api/folder", {"action": "cleanup", "path": w})
                except (OSError, ValueError, urllib.error.URLError):
                    pass
            # Our own Syncthing conflict copies first: deleted when they're the same as the file
            for c in list((self.summary.get(key) or {}).get("conflicts") or []):
                if job.stopped:
                    break
                r = self._send(c["copy"], lambda c=c: self.client.conflict(c["copy"], c["original"]), os.path.getsize(c["copy"]))
                if r.get("result") == "deleted":
                    job.update(conflicts=job.state["conflicts"] + 1)
                    app.activity.event("optimizer", M("deleted a Syncthing conflict copy: {file} ({why})",
                                                      file=os.path.basename(c["copy"]), why=r.get("reason") or ""))
            todo = []
            # carried on after a restart: files that failed earlier in this job count as done (they're
            # listed with why), not tried again — the next job tries them
            failed = {e.get("file") for e in job.state.get("errors") or []}
            mine = [f for f in list(app.files) if str(f.library_key) == key]
            job.update(files=len(mine))
            for f in mine:
                if job.stopped:
                    break
                self.run_retries(job)  # a failed file to try again: before the next
                job.update(checked=job.state["checked"] + 1)
                path = app.map_path(f.path)
                poster, matched = getattr(f, "poster", ""), getattr(f, "matched", None)
                if mode == "covers":
                    item = self._cover_todo(path, key, poster, matched)
                    if item:
                        todo.append((path, f.path, dict(item, size=f.size), poster))
                    continue
                if path in failed or f.path in failed:
                    continue
                s = self.file_status(path, key, rec, poster, matched)
                if s.get("applies") and s.get("problems"):
                    todo.append((path, f.path, {"problems": s["problems"], "matched": matched, "size": f.size}, poster))
            # resumed: what was done before counts, so the progress carries on where it was
            job.update(total=job.state["done"] + len(todo), phase="working",
                       todo_bytes=job.state["done_bytes"] + sum(int(it.get("size") or 0) for _, _, it, _ in todo))
            app.activity.event("optimizer", M("optimizing {n} files in {library}", n=len(todo), library=name))
            job.log(type="plan", files=[p for p, _, _, _ in todo])  # its list shows the ones not tried yet too
            claims = Claims(self, [(p, plex) for p, plex, item, _ in todo if self._needs_claim(item)])
            for i, (path, plex_path, item, poster) in enumerate(todo):
                if not job.wait_while_paused():
                    break
                self.run_retries(job)  # a failed file to try again: after the file in hand, before the next
                job.update(current=os.path.basename(path), current_file=path, step=None, file_pct=None, poster=poster or None, current_title=app.title_of(path) or None)
                t0 = time.time()
                before = dict(self._before(path), layout=self._layout_before(path, key), comp=self.app.compression_of(path))
                if self._needs_claim(item):
                    ok, why = claims.take(path)
                    if not ok:
                        result = {"result": "skipped", "reason": why}
                    else:
                        result = self._one(path, key, item, rec, poster)
                        claims.release(path)
                else:
                    result = self._one(path, key, item, rec, poster)
                self._log_file(job.log, job.state["id"], path, rec, item, result, time.time() - t0, before, poster, key)
                if result["result"] == "cancelled":
                    break  # not tried: listed as cancelled, counted as not done
                size = int(item.get("size") or 0)
                kind = {"optimized": "optimized", "already": "optimized", "converted": "optimized", "rewritten": "rewritten",
                        "skipped": "skipped"}.get(result["result"], "failed")
                job.file_done(size, kind, time.time() - t0 if result["result"] in ("optimized", "rewritten", "converted") else None,
                              (path, str(result.get("reason") or "failed")) if kind == "failed" else None)
                if result["result"] in ("optimized", "rewritten", "already", "converted"):
                    with self.lock:
                        lib = self.summary.get(key)
                        if lib and lib.get("todo"):
                            lib["todo"] -= 1  # the Status column follows as files are done
            claims.release_all()
            if not job.stopped:
                self.run_retries(job)
            j = dict(job.state)
            app.activity.event("optimizer", M("{library}: {optimized} optimized, {rewritten} written again, {skipped} skipped, {failed} failed",
                                              library=name, optimized=j["optimized"], rewritten=j["rewritten"],
                                              skipped=j["skipped"], failed=j["failed"]),
                               "warn" if j["failed"] else "info")
        finally:
            history = getattr(app, "history", None)
            if history is not None:
                history.save()  # (saved every half minute or so as files are done: the rest now)
            self.survey()  # before the job ends, so the counts shown then are already the new ones

    # ------------------------------------------------------------ retries
    def retry(self, job_id: str, path: str) -> str | None:
        """Try a file that failed in a job again, after the file in hand (an optimizing job's, or right
        away when none runs) — not queued. Its result replaces the failure in that job's log.
        "next", or None (not a failed file of an optimizing job, or waiting already)."""
        entry = next((e for e in reversed(self.app.jobs.log.files(job_id, 0, 10 ** 6)["files"]) if e.get("file") == path), None)
        if not entry or entry.get("result") != "failed" or entry.get("kind") != "optimize":
            return None
        with self.lock:
            if any(r["file"] == path for r in self.retries):
                return None
            self.retries.append({"job": job_id, "file": path, "library": str(entry.get("library") or ""),
                                 "mode": entry.get("mode") or "optimize", "title": entry.get("title"), "added": time.time()})
            start = not self._retrying
            self._retrying = True
        if start:
            threading.Thread(target=self._retry_worker, name="retries", daemon=True).start()
        return "next"

    def retrying(self) -> list[dict]:
        """The files waiting to be tried again (and the one being tried: "active")."""
        with self.lock:
            return [dict(r) for r in self.retries]

    def _retry_worker(self) -> None:
        """Retries when no optimizing job runs (one does them between its own files): once a folder
        copy is done, and the optimizer is ready."""
        try:
            while True:
                with self.lock:
                    if not any(not r.get("active") for r in self.retries):
                        self._retrying = False
                        return
                job = self.app.jobs.job
                if (job and job.state.get("running")) or not self.client.ready():
                    time.sleep(RETRY_POLL)  # an optimizing job takes them between files; a copy, after
                    continue
                self.run_retries(None)
        except Exception:  # noqa: BLE001 — never leave the flag set
            with self.lock:
                self._retrying = False
            raise

    def run_retries(self, job: "OptimizeJob | None") -> None:
        """Try the waiting retries now, one by one."""
        while True:
            with self.lock:
                it = next((r for r in self.retries if not r.get("active")), None)
                if it is None:
                    return
                it["active"] = True
            try:
                self._retry_one(it, job)
            finally:
                with self.lock:
                    self.retries = [r for r in self.retries if r is not it]

    def _retry_one(self, it: dict, job: "OptimizeJob | None") -> None:
        app, path, key, mode = self.app, it["file"], it["library"], it["mode"]
        f = next((x for x in list(app.files) if app.map_path(x.path) == path), None)
        rec = app.library_records().get(key) or {}
        poster, matched = (getattr(f, "poster", ""), getattr(f, "matched", None)) if f else ("", None)
        if job:
            job.update(current=os.path.basename(path), current_file=path, step=None, file_pct=None, poster=poster or None, current_title=app.title_of(path) or None)
        t0, before = time.time(), dict(self._before(path), layout=self._layout_before(path, key), comp=self.app.compression_of(path))
        item = None
        if f is None or not os.path.exists(path):
            result = {"result": "failed", "reason": "no longer in the library"}
        else:
            if mode == "covers":
                item = self._cover_todo(path, key, poster, matched)
            else:
                s = self.file_status(path, key, rec, poster, matched)
                item = {"problems": s["problems"], "matched": matched, "size": f.size} if s.get("applies") and s.get("problems") else None
            if item is None:
                result = {"result": "already"}  # nothing to do any more
            elif self._needs_claim(item):
                claims = Claims(self, [(path, f.path)])
                ok, why = claims.take(path)
                result = self._one(path, key, item, rec, poster) if ok else {"result": "skipped", "reason": why}
                claims.release_all()
            else:
                result = self._one(path, key, item, rec, poster)
        # logged as the failed job's: its result replaces the failure there
        log = lambda **e: app.jobs.log.add(dict(e, job=it["job"], kind="optimize", mode=mode, library=key,
                                                title=it.get("title"), retried=True))
        self._log_file(log, it["job"], path, rec, item or {}, result, time.time() - t0, before, poster, key)
        app.jobs.retried(it["job"], path, result)
        if result.get("result") in ("optimized", "rewritten", "already"):
            with self.lock:
                lib = self.summary.get(key)
                if lib and lib.get("todo"):
                    lib["todo"] -= 1

    def describe(self, rows: list[dict], job_id: str) -> None:
        """The Processes list's rows, filled in: each file's title and poster (logged, or from the last
        scan), and for the files not tried yet how they are now — waiting (its job runs) or not done."""
        snap = self.app.jobs.snapshot() or {}
        live = snap.get("id") == job_id and (snap.get("running") or snap.get("waiting"))
        for f in rows:
            path = f.get("file") or ""
            f["poster"] = f.get("poster") or self.app.poster_of(path)
            f["label"] = self.app.title_of(path) or f.get("label")  # as Plex names it now (with its edition)
            if f.get("result") == "pending":
                b = self._before(path)
                f.update(result="waiting" if live else "not done", size_before=b.get("size"), rs_before=b.get("rs"),
                         compression_before=b.get("compression") or "unknown", disk_before=b.get("disk"),
                         version_before=b.get("version"), images_before=b.get("images"))

    def _before(self, path: str) -> dict:
        """What the log notes about a file before the optimizer writes it."""
        try:
            st = os.stat(path)
        except OSError:
            return {}
        z = self.zfs.files.get(path) or {}
        with self.lock:
            known = self.record.get(path) or {}
        same = (known.get("size"), known.get("mtime_ns"), known.get("ino")) == (st.st_size, st.st_mtime_ns, st.st_ino)
        return {"size": st.st_size, "rs": st.st_blksize if st.st_size > st.st_blksize else None,
                "compression": z.get("compression") if z.get("ino") == st.st_ino else None,
                "disk": st.st_blocks * 512,  # its space on the pool, after compression
                "version": (known.get("ours") or {}).get("PLEXCACHE_OPTIMIZER") if same else None,
                "images": known.get("images") if same else None}

    def _layout_before(self, path: str, key: str) -> dict | None:
        """The file's layout before the optimizer writes it (read now if it changed since it was
        inspected), the file as it is then put in its history (filehistory.py)."""
        maps = getattr(self.app, "file_maps", None)
        if maps is None or getattr(self.app, "history", None) is None:
            return None
        try:
            m = maps.read(path)
            self.app.observe_history(path, key, m)
            return m
        except OSError:
            return None

    def _history(self, path: str, key: str, written: str, before: dict | None, comp_before: str | None = None,
                 comp_after: str | None = None) -> tuple[dict | None, dict | None]:
        """A file the optimizer has just written, in its history: its layout before and after (read now),
        as kept there (filehistory.snapshot) — for its line in the job log."""
        from .filehistory import snapshot
        maps, history = getattr(self.app, "file_maps", None), getattr(self.app, "history", None)
        if maps is None or history is None:
            return None, None
        try:
            st = os.stat(written)
            after = maps.read(written)
            ours = self._mkv_info(written, st).get("ours") or {} if after.get("format") == "mkv" else {}
        except Exception:  # noqa: BLE001 — not readable now: its history waits for the next inspection
            return snapshot(before, comp_before), None
        history.processed(path, key, before, after, [st.st_size, st.st_mtime_ns, st.st_ino], ours,
                          new_path=written if written != path else None, comp_before=comp_before, comp_after=comp_after)
        history.save_soon()
        return snapshot(before, comp_before), snapshot(after, comp_after)

    def _log_file(self, log, job_id: str, path: str, rec: dict, item: dict, r: dict, seconds: float, before: dict,
                  poster: str = "", key: str = "") -> None:
        """One file in the job log: how long, its size, the verification, its record size and
        compression before and after, whether it was laid out again, and its cover art."""
        res = r.get("result")
        wrote = res in ("optimized", "rewritten", "converted")
        written = r.get("new") if res == "converted" else path  # (a converted file: what's said after is its MKV's)
        try:
            st = os.stat(written)
            after = {"size": st.st_size, "rs": st.st_blksize if st.st_size > st.st_blksize else None}
        except OSError:
            after = {}
        cover, b, a = None, None, None
        if res in ("optimized", "converted"):
            b, a = r.get("images_before") or 0, r.get("images_after") or 0
            cover = ("Plex poster added" if r.get("cover") and not b else "replaced by Plex's poster" if r.get("cover")
                     else f"{b - a} removed" if a < b else "compressed" if r.get("images_saved") else "kept" if a else None)
        mix_before, mix_after = self._history(path, str(key), written, before.get("layout"), before.get("comp"),
                                              family(rec.get("compression"))) if wrote else (None, None)
        log(type="file", file=path, result=res, seconds=round(seconds, 1), reason=r.get("reason"), mix_before=mix_before, mix_after=mix_after,
                size_before=before.get("size"), size_after=after.get("size") if wrote else before.get("size"),
                verified=r.get("verified") or ("not needed" if res == "already" else None),
                rs_before=before.get("rs"), rs_after=after.get("rs") if wrote else before.get("rs"),
                compression_before=before.get("compression") or "unknown",
                compression_after=rec.get("compression") if wrote else before.get("compression") or "unknown",
                layout=res in ("optimized", "converted"), cover=cover, poster=poster or None, label=self.app.title_of(path) or None,
                version=r.get("version") if res in ("optimized", "converted") else None, version_before=before.get("version"),
                images_before=b, images_after=a, images_saved=r.get("images_saved") or None, new=r.get("new"),
                poster_added=bool(r.get("cover") and not b) if res in ("optimized", "converted") else None, disk_before=before.get("disk"))
        if wrote:
            # its space on the pool once ZFS has written it out (compressed): known a little later
            def disk_after():
                got = settled_blocks(written)
                if got is not None:
                    self.app.jobs.log.add({"type": "disk", "job": job_id, "file": path, "disk_after": got})
            threading.Thread(target=disk_after, name="disk-after", daemon=True).start()

    @staticmethod
    def _needs_claim(item: dict) -> bool:
        """Only remuxes are claimed: a byte-for-byte rewrite (record size, compression) is about
        this NAS only, keeps the modification time, and Syncthing doesn't even see it."""
        return item.get("cover") is not None or any(p not in SYNC_PROBLEMS for p in item.get("problems") or [])

    def _cover_todo(self, path: str, key: str, poster: str, matched: bool | None) -> dict | None:
        """Sync Cover Art: what an optimized MKV's cover art should change to now, or None."""
        opts = self.options(key)
        if not (opts["mkv"] and path.lower().endswith(mkv.MKV_EXT)) or conflict_original(path):
            return None
        try:
            info = self._mkv_info(path, os.stat(path))
        except OSError:
            return None
        ours = info.get("ours") or {}
        if version_of(ours.get("PLEXCACHE_OPTIMIZER")) not in ("same", "older"):
            return None  # not optimized yet (Optimize Now does it), or by a newer optimizer
        want = self.cover_for(opts["images"], poster, matched)
        if want == "plex" and poster:
            try:
                data = self._poster(poster)
            except PlexError:
                return None
            if ours.get("PLEXCACHE_COVER") == COVER_PREFIX + hashlib.sha256(data).hexdigest():
                return None  # Plex's poster, as it is now
            return {"cover": "plex", "problems": []}
        if want == "remove" and opts["images"] == "remove" and info.get("images"):
            return {"cover": "remove", "problems": []}
        return None

    def _stop_event(self) -> threading.Event:
        """The running job's cancel (a never-set one when none runs)."""
        job = self.app.jobs.job
        return job.stop_event if job and job.state.get("running") else threading.Event()

    def poster_size(self, poster: str, fetch: bool = False) -> int | None:
        """The size of Plex's poster as Plex has it (before the optimizer compresses it), kept by its
        path — which changes when the poster does. ``fetch``: downloaded now if it isn't known."""
        sizes = self._poster_sizes()
        if not poster or poster in sizes or not fetch:
            return sizes.get(poster)
        try:
            n = len(self._poster(poster))
        except PlexError:
            return None
        with self.lock:
            sizes[poster] = n
            while len(sizes) > 50000:
                sizes.pop(next(iter(sizes)))
        self.app._save_json(POSTER_SIZES, sizes)
        return n

    def _poster_sizes(self) -> dict:
        with self.lock:
            if not hasattr(self, "_psizes"):
                got = self.app._load_json(POSTER_SIZES)
                self._psizes = got if isinstance(got, dict) else {}
            return self._psizes

    def poster_sizes_soon(self, posters: list) -> None:
        """Learn the sizes of these posters in the background, one at a time (Library Health's estimates
        for a library whose cover art becomes Plex's poster)."""
        todo = [p for p in dict.fromkeys(posters) if p and p not in self._poster_sizes()]
        with self.lock:
            if not todo or getattr(self, "_psizing", False):
                return
            self._psizing = True

        def work():
            try:
                for p in todo:
                    self.poster_size(p, fetch=True)
            finally:
                with self.lock:
                    self._psizing = False
        threading.Thread(target=work, name="poster-sizes", daemon=True).start()

    def _poster(self, path: str) -> bytes:
        """Plex's poster, kept for the run: a season's episodes share one."""
        cache = self.__dict__.setdefault("_posters", {})
        if path not in cache:
            if len(cache) > 8:
                cache.clear()
            cache[path] = self.app.plex(timeout=60).image(path)
        return cache[path]

    def _one(self, path: str, key: str, item: dict, rec: dict, poster: str = "") -> dict:
        """Send one file to the optimizer — laid out (MKV), or written again — and wait for its result."""
        if path in self.app.monitor.playing_paths():
            return {"result": "skipped", "reason": "playing"}
        try:
            st = os.stat(path)
        except OSError as e:
            return {"result": "failed", "reason": str(e)}
        if time.time() - st.st_mtime < RECENT_SECONDS:
            return {"result": "skipped", "reason": "just written"}
        opts = self.options(key)
        problems = item.get("problems") or []
        try:
            if "convert" in problems:  # converted to MKV (and optimized), next to the original
                images = self.images_mode(opts["images"], poster, item.get("matched"), {})
                cover = None
                if images == "plex":
                    try:
                        cover = self._poster(poster)
                    except PlexError as e:
                        return {"result": "failed", "reason": msg(e)}
                send = lambda: self.client.convert(path, images, cover, opts["tags"])
            elif item.get("cover"):  # Sync Cover Art: written again with the new cover art
                cover = self._poster(poster) if item["cover"] == "plex" else None
                send = lambda: self.client.optimize(path, item["cover"], True, cover, opts["tags"])
            elif any(p not in SYNC_PROBLEMS for p in problems):
                info = self._mkv_info(path, st)
                images = self.images_mode(opts["images"], poster, item.get("matched"), info.get("ours"))
                cover = None
                if images == "plex":
                    try:
                        cover = self._poster(poster)
                    except PlexError as e:
                        return {"result": "failed", "reason": msg(e)}
                send = lambda: self.client.optimize(path, images, False, cover, opts["tags"])
            else:
                send = lambda: self.client.rewrite(path)
        except PlexError as e:
            return {"result": "failed", "reason": msg(e)}
        r = self._send(path, send, st.st_size)
        if r.get("result") in ("optimized", "already", "rewritten"):
            self.note(path, key, r, rec)
        elif r.get("result") == "converted" and r.get("new"):
            self.note(r["new"], key, dict(r, result="optimized"), rec)  # (the MKV: optimized, written with the dataset's settings)
            f = next((x for x in list(self.app.files) if self.app.map_path(x.path) == path), None)
            if f is not None:
                self.app.conversions.add(f, path, r["new"])
        elif "convert" in problems and r.get("result") == "skipped":
            # what MKV wouldn't keep: not tried again while the file stays as it is
            with self.lock:
                self.record[path] = dict(self.record.get(path) or {}, convert_skip={
                    "reason": r.get("reason"), "size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino})
            self.save()
        return r

    def _send(self, path: str, start, size: int) -> dict:
        """Start one thing on the optimizer and wait for its result (polling its status). A cancel
        while it waits its turn: "cancelled" (not tried). Once it's started, its result is waited
        for even then — the optimizer finishes the file in hand whatever happens here."""
        try:
            started = start()
            # still finishing a file from before this app restarted: wait for it, then send this one
            waited = time.time()
            while not started.get("started") and started.get("error") == "busy" and time.time() - waited < 3600:
                if self._stop_event().wait(5):
                    return {"result": "cancelled", "reason": "cancelled before it started"}
                started = start()
        except (OSError, ValueError, urllib.error.URLError) as e:
            return {"result": "failed", "reason": str(getattr(e, "reason", e))}
        if not started.get("started"):
            return {"result": "failed", "reason": started.get("error") or "not started"}
        sent = time.time()
        idle_since = None
        deadline = sent + 600 + size / (20 * 1024 * 1024)  # 20 MB/s at the slowest, plus 10 minutes
        stop = self._stop_event()
        while time.time() < deadline:
            stop.wait(2) if not stop.is_set() else time.sleep(2)
            status = self.client.status(fresh=True)
            cur = status.get("current") or {}
            job = self.app.jobs.job
            if job and job.state.get("running"):
                mine = cur.get("path") == path
                job.update(step=cur.get("step") if mine else None, file_pct=cur.get("pct") if mine else None)
                if mine:
                    job.first_estimate(size, time.time() - sent, cur.get("pct"))
            for r in reversed(status.get("results") or []):
                if r.get("path") == path and r.get("finished", 0) >= sent - 1:
                    return r
            # nothing running there and no result: the optimizer restarted mid-way (the file is
            # untouched: a new file only ever replaces it whole)
            if not status.get("error") and not status.get("current"):
                idle_since = idle_since or time.time()
                if time.time() - idle_since > self.IDLE_LIMIT:
                    return {"result": "failed", "reason": "the optimizer restarted while working on it"}
            else:
                idle_since = None
        return {"result": "failed", "reason": "timed out"}

    def instance(self) -> str:
        """This system's ID in claims, made once and kept in the config folder."""
        path = os.path.join(self.app.config_dir, "instance-id")
        try:
            with open(path, encoding="utf-8") as fh:
                got = fh.read().strip()
            if 8 <= len(got) <= 64:
                return got
        except OSError:
            pass
        got = secrets.token_hex(8)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(got)
        except OSError:
            pass
        return got

    def snapshot(self) -> dict:
        ready = self.client.ready()  # may ask the service: not while holding the lock
        job = self.app.jobs.snapshot()
        with self.lock:
            return {"configured": ready, "setup": dict(self.setup), "libraries": dict(self.summary), "version": VERSION,
                    "recorded": len(self.record), "zfs_started": self.zfs.started,
                    "job": job if job and job.get("kind") == "optimize" else None, "surveying": self.surveying}


RETRY_POLL = 3  # seconds: how often waiting retries look for their turn
# A written file's space on the pool is known once ZFS has committed it (compressing it as it does,
# every few seconds — later on a busy pool): watched until two looks in a row agree.
DISK_SETTLE = 10  # seconds before the first look
DISK_EVERY = 5  # seconds between looks
DISK_GIVE_UP = 600  # seconds: then the last look counts


def settled_blocks(path: str, sleep=time.sleep, clock=time.monotonic) -> int | None:
    """The file's space on the pool (bytes) once it stops changing; None if it's gone."""
    sleep(DISK_SETTLE)
    until, last = clock() + DISK_GIVE_UP, None
    while True:
        try:
            now = os.stat(path).st_blocks * 512
        except OSError:
            return None
        if now == last or clock() >= until:
            return now
        last = now
        sleep(DISK_EVERY)


class OptimizeJob(Job):
    """Optimizing a library (or, mode "covers", syncing its cover art) as a file job."""

    kind = "optimize"

    def __init__(self, op: "Optimizing", library: str, mode: str = "optimize", resumed: dict | None = None):
        self.op = op
        title = (op.app.library_info(str(library)) or {}).get("title") or (resumed or {}).get("title")
        super().__init__(op.app.jobs, resumed, library=str(library), mode=mode, title=title)

    def args(self) -> dict:
        return {"library": self.state["library"], "mode": self.state["mode"]}

    def dataset(self) -> str | None:
        return (self.app.library_records().get(self.state["library"]) or {}).get("dataset")

    def run(self) -> None:
        self.op._run(self)


class Claims:
    """A job's claims on the files it remuxes, for systems sharing a library: each file is
    claimed a few files ahead, so by the time its turn comes another system's claim would have
    synced; the earliest claim wins. With no other system ever seen in the folder, nothing waits."""

    AHEAD = 10   # files claimed ahead of the one being done
    WAIT = 120   # seconds a claim must be old before it's acted on, when other systems share the folder

    def __init__(self, op: Optimizing, items: list):
        self.op, self.items = op, items  # [(path, plex path)]
        self.at: dict[str, float] = {}
        self.peers: dict[str, bool] = {}
        self.me = op.instance()

    def _key(self, plex_path: str, root_plex: str) -> str:
        rel = os.path.relpath(plex_path, root_plex)
        return hashlib.sha1(rel.encode()).hexdigest()[:24]

    def _call(self, path: str, release: bool = False) -> dict | None:
        plex = dict(self.items).get(path)
        root = self.op.library_root(plex) if plex else None
        if not root:
            return None
        root_plex = next((loc.rstrip("/") for lib in getattr(self.op.app, "libraries", []) or [] for loc in lib.get("locations") or []
                          if plex.startswith(loc.rstrip("/") + "/")), None)
        try:
            return self.op.client.claim(root, self._key(plex, root_plex), self.me, release)
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def _ahead(self, path: str) -> None:
        paths = [p for p, _ in self.items]
        i = paths.index(path) if path in paths else 0
        for p in paths[i:i + self.AHEAD]:
            if p not in self.at:
                r = self._call(p)
                self.at[p] = time.time()
                self.peers[p] = bool(r and r.get("peers"))

    def take(self, path: str) -> tuple[bool, str]:
        """Whether this system may remux the file now (waiting for its claim to settle if other
        systems share the folder), and if not, why."""
        self._ahead(path)
        if not self.peers.get(path):
            return True, ""
        left = self.WAIT - (time.time() - self.at[path])
        if left > 0 and self.op._stop_event().wait(left):
            return False, "stopped"
        r = self._call(path)
        first = ((r or {}).get("claims") or [{}])[0]
        if first.get("instance") not in (None, self.me):
            self.release(path)
            return False, "another system is optimizing it"
        return True, ""

    def release(self, path: str) -> None:
        if path in self.at:
            self._call(path, release=True)
            del self.at[path]

    def release_all(self) -> None:
        for path in list(self.at):
            self.release(path)

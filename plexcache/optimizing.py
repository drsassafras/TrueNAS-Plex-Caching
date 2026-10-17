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
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

from . import zfs
from .i18n import M, join as join_names, msg
from .optimize import VERSION, mkv
from .plex import PlexError

ZSTD3 = ("zstd", "zstd-3")  # compression=zstd is level 3
IMAGE_MODES = ("unmatched", "remove", "compress", "plex")
DEFAULTS = {"sync": True, "mkv": False, "images": "unmatched"}  # each library's choices unless set
SYNC_PROBLEMS = ("compression", "blocks")  # fixed by writing the file again
RECORD = "optimized.json"
# The optimizer's own health check, for its service in the YAML (images older than the optimizer
# check the web app's port instead, so Docker never sees it healthy)
HEALTHCHECK_YAML = ('    healthcheck:\n'
                    '      test: ["CMD", "python", "-m", "plexcache.healthcheck"]')
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

    def rewrite(self, path: str) -> dict:
        return self._call("/api/rewrite", {"path": path}, timeout=60)

    def optimize(self, path: str, images: str, force: bool, cover: bytes | None = None) -> dict:
        body = {"path": path, "images": images, "force": force}
        if cover:
            body["cover"] = base64.b64encode(cover).decode()
        return self._call("/api/optimize", body, timeout=60)


class Optimizing:
    """The record of what the optimizer did, per-library summaries, and the "optimize a library" job."""

    def __init__(self, app, client: OptimizerClient | None = None):
        self.app = app
        self.client = client or OptimizerClient(token=self.token())
        self.lock = threading.Lock()
        saved = app._load_json(RECORD) or {}
        self.record: dict[str, dict] = saved.get("files") or {}  # path -> what the optimizer did
        self.summary: dict[str, dict] = {}
        self.job: dict | None = None
        self._stop = threading.Event()
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
                step("yaml", False, M("Add the optimizer to the app's YAML"),
                     M("{app} has no optimizer service.", app=appname),
                     M("In TrueNAS, Apps → {app} → Edit. Paste the lines below under services: (next to {service}), "
                       "and Save. The volumes are filled in from {service}'s own: its settings folder, read-only (the "
                       "optimizer finds the app's token there), and your media, writable — the optimizer can only change "
                       "what's mounted, so leave out any folder it shouldn't touch.", app=appname, service=tn["me"] or appname),
                     checks.optimizer_yaml(self.app))
                return self._done(steps)
            step("yaml", True, M("In the app's YAML"), M("Service {service}.", service=opt.get("service_name")))
            state = str(opt.get("state") or "").lower()
            # TrueNAS shows a running container's health instead while it has one: "starting" until its
            # health check first passes, "unhealthy" when it keeps failing — the program is running either way
            health = state if state in ("starting", "unhealthy") else None
            if state not in ("running", "healthy", "starting", "unhealthy"):
                needs_code = code and opt.get("has_config") and "/app/plexcache" not in opt.get("targets", [])
                fix = (M("This app runs code mounted over its image (a test copy?). The optimizer needs the same code: "
                         "add this line to its volumes, in Apps → {app} → Edit:", app=appname) if needs_code else
                       M("Its logs say why: in TrueNAS, Apps → {app} → the optimizer's container → View Logs. Most often "
                         "the image is older than the optimizer: update it to the latest.", app=appname))
                step("running", False, M("The optimizer isn't running"),
                     M("TrueNAS says it's {state}: it stopped right after starting.", state=state or "?"), fix,
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
                step("yaml", False, M("Add the optimizer to the app's YAML"),
                     M("Nothing answers at {url}. (With a TrueNAS API key that can see apps, this page can tell whether it's "
                       "in the YAML and running.)", url=self.client.url),
                     M("In TrueNAS, Apps → plex-cache → Edit. Paste the lines below under services: (next to plex-cache), "
                       "and Save. If it's already there, check it's running: Apps → plex-cache, and its logs."),
                     checks.optimizer_yaml(self.app))
            else:
                step("answers", False, M("The optimizer doesn't answer"),
                     M("It's running, but the app couldn't reach it at {url}: {why}", url=self.client.url, why=hz["error"]),
                     M("It listens on port 8643 inside the app. If you set PLEXCACHE_OPTIMIZER_URL or "
                       "PLEXCACHE_OPTIMIZER_PORT, check they match."))
            return self._done(steps)
        if hz.get("service") != "optimizer":
            step("answers", False, M("Something else answers at {url}", url=self.client.url),
                 M("It isn't the optimizer: it may run an image older than the optimizer."),
                 M("In TrueNAS, Apps → {app}: update its image to the latest.", app=appname))
            return self._done(steps)
        step("answers", True, M("Answering"), M("Optimizer version {version}.", version=hz.get("version")))
        if health == "starting" and tn and tn["optimizer"].get("healthcheck"):
            step("health", False, M("Still starting"),
                 M("It answers, and its health check hasn't run yet. TrueNAS shows the app as deploying until it does."),
                 M("Check again in a minute."))
            return self._done(steps)
        if health and tn and tn["optimizer"].get("healthcheck"):
            step("health", False, M("Its health check doesn't pass"),
                 M("The optimizer answers, but TrueNAS says it's {state}.", state=health),
                 M("Check its health check in the YAML is the one below, and its logs (Apps → {app} → the optimizer's "
                   "container → View Logs).", app=appname), HEALTHCHECK_YAML)
            return self._done(steps)
        if health:
            # It answers, but Docker's health check doesn't pass: TrueNAS then keeps the app "Deploying"
            step("health", False, M("Its health check doesn't pass"),
                 M("The optimizer answers, but TrueNAS says it's {state}: its image's built-in health check looks at the "
                   "web app's port, not the optimizer's — the image is older than the optimizer. TrueNAS keeps the app "
                   "deploying until it passes.", state=health),
                 M("In Apps → {app} → Edit, add these lines to the optimizer service (next to its volumes), or update the "
                   "image to the latest:", app=appname),
                 HEALTHCHECK_YAML)
            return self._done(steps)
        if not hz.get("token"):
            problem = hz.get("token_problem")
            why = {"missing": M("There's no {file} in it: the app's settings folder isn't mounted into it at /config.",
                                file=hz.get("token_file") or "/config/optimizer-token"),
                   "unreadable": M("It can't read {file}: it runs as user {uid}, which can't read files in the app's "
                                   "settings folder.", file=hz.get("token_file") or "", uid=hz.get("uid")),
                   }.get(problem, M("The token file is empty."))
            fix = (M("In Apps → {app} → Edit, run the optimizer as user 568 (apps), or add it to that group "
                     "(group_add: [\"568\"]).", app=appname) if problem == "unreadable" else
                   M("In Apps → {app} → Edit, add the app's settings folder to the optimizer's volumes, read-only:", app=appname))
            config = zfs.host_path(self.app.config_dir) or "/mnt/tank/apps/plex-cache"
            step("token", False, M("The optimizer doesn't have the app's token"), why, fix,
                 "" if problem == "unreadable" else f"      - {config}:/config:ro")
            return self._done(steps)
        st = self.client.status(fresh=True)
        if st.get("error"):
            wrong = "401" in st["error"] or "nauthorized" in st["error"]
            step("token", False, M("The optimizer has a different token") if wrong else M("The optimizer doesn't answer"),
                 M("It reads a token, but not the one this app uses: PLEXCACHE_OPTIMIZER_TOKEN is set in one service only, "
                   "or its settings folder isn't this app's.") if wrong else str(st["error"]),
                 M("In Apps → {app} → Edit: mount the same settings folder in both services, and remove "
                   "PLEXCACHE_OPTIMIZER_TOKEN (or set it the same in both).", app=appname) if wrong else "")
            return self._done(steps)
        step("token", True, M("Has the app's token"))
        if st.get("missing"):
            step("tools", False, M("The optimizer is missing tools"),
                 M("It can't find {tools}: its image is older than the optimizer.", tools=join_names(st["missing"])),
                 # in a test copy the code is mounted over the image, but programs like these come only with the image
                 M("This app's code is mounted over its image (a test copy), but MKVToolNix and ffmpeg come with the image "
                   "itself, not the code: it needs an image built from this code. Then, in TrueNAS, Apps → {app}: update "
                   "its image.", app=appname) if code else
                 M("In TrueNAS, Apps → {app}: update its image to the latest.", app=appname))
            return self._done(steps)
        step("tools", True, M("Has its tools"))
        roots = st.get("roots") or []
        if not roots:
            step("media", False, M("The optimizer has no media to work on"),
                 M("No media folder is mounted into it writable, so it can't change any file."),
                 M("In Apps → {app} → Edit, add your media folders to the optimizer's volumes, the same as {app}'s but "
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
        """The library's choices: {"sync", "mkv", "images"} (DEFAULTS for what isn't set)."""
        lib = (self.app.settings.section("optimizer")["libraries"] or {}).get(str(library_key)) or {}
        out = dict(DEFAULTS, **{k: v for k, v in lib.items() if k in DEFAULTS})
        if out["images"] not in IMAGE_MODES:
            out["images"] = DEFAULTS["images"]
        return out

    @staticmethod
    def cover_for(images: str, poster: str, matched: bool | None) -> str:
        """What a file's cover art becomes under a library's choice: "remove", "compress" or
        "plex". "unmatched": Plex's poster for an item Plex hasn't matched (the file is the most
        reliable place to keep it), none for a matched one; when Plex didn't say, or has no
        poster, what's there is kept, compressed."""
        if images != "unmatched":
            return images
        if matched is True:
            return "remove"
        return "plex" if matched is False and poster else "compress"

    def file_status(self, path: str, library_key: str, rec: dict, poster: str = "", matched: bool | None = None) -> dict:
        """{"applies", "mkv", "problems": [...]} for one file under its library's choices, from the
        record and one stat() — the file itself isn't read. ``rec``: its dataset
        (App.library_records); ``poster``: Plex's poster for it (MediaFile.poster).

        With Optimize MKV files, for MKV files: "new" (never laid out by the optimizer),
        "changed" (replaced since), "version" (by an older optimizer), "images" (for another
        cover art choice), "cover" (Plex's poster changed). With TrueNAS compression &
        record size, for any file: "compression" (not known to be written with the dataset's
        current compression — that can't be read from a file, so only a file the optimizer
        wrote, with the compression the dataset had then, counts), "blocks" (not in blocks of
        the dataset's record size)."""
        opts = self.options(library_key)
        is_mkv = path.lower().endswith(mkv.MKV_EXT)
        if not (opts["sync"] or (opts["mkv"] and is_mkv)):
            return {"applies": False, "mkv": is_mkv}
        try:
            st = os.stat(path)
        except OSError:
            return {"applies": False, "missing": True, "mkv": is_mkv}
        with self.lock:
            done = self.record.get(path)
        same = bool(done) and (done.get("size"), done.get("mtime_ns"), done.get("ino")) == (st.st_size, st.st_mtime_ns, st.st_ino)
        problems = []
        if opts["mkv"] and is_mkv:
            lay = (done or {}).get("layout") if same else None
            if not done or (same and not lay):
                problems.append("new")
            elif not same:
                problems.append("changed")
            else:
                if (lay.get("version") or 0) < VERSION:
                    problems.append("version")
                cover = self.cover_for(opts["images"], poster, matched)
                if cover == "plex" and poster:
                    if lay.get("cover") != poster:
                        problems.append("cover")
                elif lay.get("images") != cover and not (cover == "compress" and lay.get("images") == "plex"):
                    problems.append("images")
        if opts["sync"]:
            if not (same and done.get("written") and (not rec.get("compression") or done.get("compression") == rec.get("compression"))):
                problems.append("compression")
            rs = rec.get("rs")
            if rs and st.st_size > st.st_blksize and st.st_blksize != rs:
                problems.append("blocks")
        return {"applies": True, "mkv": is_mkv, "problems": problems}

    def survey(self) -> None:
        """How far each library is, under its choices: one stat() per file, nothing read."""
        with self.lock:
            if self.surveying:
                return
            self.surveying = True
        try:
            records = self.app.library_records()
            out: dict[str, dict] = {}
            # Every library that isn't unticked in Media mappings: optimizing doesn't depend on caching
            off = {str(lib["key"]) for lib in getattr(self.app, "libraries", []) or [] if lib.get("excluded")}
            for f in list(self.app.files):
                key = str(f.library_key)
                if key in off:
                    continue
                rec = records.get(key) or {}
                s = self.file_status(self.app.map_path(f.path), key, rec, getattr(f, "poster", ""), getattr(f, "matched", None))
                lib = out.setdefault(key, {"files": 0, "mkv": 0, "mkv_done": 0, "sync_todo": 0, "todo": 0,
                                           "compression_setting": rec.get("compression"), "dataset": rec.get("dataset"),
                                           "why": {}})
                if s.get("missing"):
                    continue  # can't be read here (Health's Libraries says why)
                lib["files"] += 1
                lib["mkv"] += bool(s.get("mkv"))
                probs = s.get("problems") or []
                if s.get("mkv") and self.options(key)["mkv"] and not any(p not in SYNC_PROBLEMS for p in probs):
                    lib["mkv_done"] += 1
                lib["sync_todo"] += any(p in SYNC_PROBLEMS for p in probs)
                lib["todo"] += bool(probs)
                for p in probs:
                    lib["why"][p] = lib["why"].get(p, 0) + 1
            with self.lock:
                self.summary = out
        finally:
            with self.lock:
                self.surveying = False

    def note(self, path: str, key: str, r: dict, rec: dict, images: str, poster: str, before: dict | None) -> None:
        """Put a finished file in the record. ``before``: its record entry when the job started,
        if that was still the same file — what the optimizer didn't redo is carried over."""
        ident = r.get("file") or {}
        if not ident.get("ino"):
            return
        result = r.get("result")
        wrote = result in ("optimized", "rewritten")
        entry = {"at": r.get("finished") or time.time(), "result": result, "sha256": r.get("sha256"),
                 "version": r.get("version"), "library": key, **{k: ident.get(k) for k in ("size", "mtime_ns", "ino")},
                 # what it was written with — only known when the optimizer wrote it
                 "written": wrote or bool((before or {}).get("written")),
                 "compression": rec.get("compression") if wrote else (before or {}).get("compression"),
                 "rs": rec.get("rs") if wrote else (before or {}).get("rs")}
        if result in ("optimized", "already"):
            # laid out for caching, for this cover art choice (none when an image couldn't be compressed)
            entry["layout"] = {"version": r.get("version"), "images": None if r.get("images_left") else images,
                               "cover": poster if r.get("cover") else None}
        else:
            entry["layout"] = (before or {}).get("layout")  # written again byte for byte: the layout is as it was
        with self.lock:
            self.record[path] = entry
        self.save()

    def save(self) -> None:
        with self.lock:
            data = {"version": 1, "files": dict(self.record)}
        self.app._save_json(RECORD, data)

    # ------------------------------------------------------------ the job
    def start_library(self, library_key: str) -> bool:
        """Do every file of a library that needs it under its choices, one at a time, in the background."""
        if not self.client.ready():
            return False
        with self.lock:
            if self.job and self.job.get("running"):
                return False
            self.job = {"library": str(library_key), "running": True, "started": time.time(), "done": 0, "total": 0,
                        "optimized": 0, "rewritten": 0, "skipped": 0, "failed": 0, "current": None, "step": None, "errors": []}
        self._stop.clear()
        threading.Thread(target=self._run, args=(str(library_key),), daemon=True).start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self, key: str) -> None:
        app = self.app
        name = (app.library_info(key) or {}).get("title") or key
        try:
            rec = app.library_records().get(key) or {}
            todo = []
            for f in list(app.files):
                if str(f.library_key) != key:
                    continue
                path = app.map_path(f.path)
                poster, matched = getattr(f, "poster", ""), getattr(f, "matched", None)
                s = self.file_status(path, key, rec, poster, matched)
                if s.get("applies") and s.get("problems"):
                    todo.append((path, s["problems"], poster, matched))
            with self.lock:
                self.job["total"] = len(todo)
            app.activity.event("optimizer", M("optimizing {n} files in {library}", n=len(todo), library=name))
            for path, problems, poster, matched in todo:
                if self._stop.is_set():
                    break
                with self.lock:
                    self.job.update(current=os.path.basename(path), step=None)
                result = self._one(path, key, problems, rec, poster, matched)
                with self.lock:
                    self.job["done"] += 1
                    kind = {"optimized": "optimized", "already": "optimized", "rewritten": "rewritten",
                            "skipped": "skipped"}.get(result["result"], "failed")
                    self.job[kind] += 1
                    if kind == "failed" and len(self.job["errors"]) < 20:
                        self.job["errors"].append(f"{os.path.basename(path)}: {result.get('reason')}")
            with self.lock:
                j = dict(self.job)
            app.activity.event("optimizer", M("{library}: {optimized} optimized, {rewritten} written again, {skipped} skipped, {failed} failed",
                                              library=name, optimized=j["optimized"], rewritten=j["rewritten"],
                                              skipped=j["skipped"], failed=j["failed"]),
                               "warn" if j["failed"] else "info")
        except Exception as e:  # never leave the job "running"
            app.activity.event("optimizer", M("unexpected error: {why}", why=str(e)), "warn")
        finally:
            try:
                self.survey()  # before "finished", so the counts shown then are already the new ones
            finally:
                with self.lock:
                    if self.job:
                        self.job.update(running=False, current=None, step=None, finished=time.time())

    def _poster(self, path: str) -> bytes:
        """Plex's poster, kept for the run: a season's episodes share one."""
        cache = self.__dict__.setdefault("_posters", {})
        if path not in cache:
            if len(cache) > 8:
                cache.clear()
            cache[path] = self.app.plex(timeout=60).image(path)
        return cache[path]

    def _one(self, path: str, key: str, problems: list, rec: dict, poster: str = "", matched: bool | None = None) -> dict:
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
        with self.lock:
            done = self.record.get(path)
        before = done if done and (done.get("size"), done.get("mtime_ns"), done.get("ino")) == (st.st_size, st.st_mtime_ns, st.st_ino) else None
        layout = any(p not in SYNC_PROBLEMS for p in problems)
        try:
            images = self.cover_for(opts["images"], poster, matched)
            if layout:
                cover = None
                if images == "plex" and poster:
                    try:
                        cover = self._poster(poster)
                    except PlexError as e:
                        return {"result": "failed", "reason": msg(e)}
                # written again even if laid out right, when the dataset's settings call for it
                force = any(p in SYNC_PROBLEMS + ("version",) for p in problems)
                started = self.client.optimize(path, images, force, cover)
            else:
                cover = None
                started = self.client.rewrite(path)
        except (OSError, ValueError, urllib.error.URLError) as e:
            return {"result": "failed", "reason": str(getattr(e, "reason", e))}
        if not started.get("started"):
            return {"result": "failed", "reason": started.get("error") or "not started"}
        sent = time.time()
        deadline = sent + 600 + st.st_size / (20 * 1024 * 1024)  # 20 MB/s at the slowest, plus 10 minutes
        while time.time() < deadline and not self._stop.wait(2):
            status = self.client.status(fresh=True)
            cur = status.get("current") or {}
            with self.lock:
                if self.job:
                    self.job["step"] = cur.get("step") if cur.get("path") == path else None
            for r in reversed(status.get("results") or []):
                if r.get("path") == path and r.get("finished", 0) >= sent - 1:
                    if r.get("result") in ("optimized", "already", "rewritten"):
                        self.note(path, key, r, rec, images, poster if cover else "", before)
                    return r
            if self._stop.is_set():
                break
        return {"result": "failed", "reason": "stopped" if self._stop.is_set() else "timed out"}

    def snapshot(self) -> dict:
        ready = self.client.ready()  # may ask the service: not while holding the lock
        with self.lock:
            return {"configured": ready, "setup": dict(self.setup), "libraries": dict(self.summary), "version": VERSION,
                    "recorded": len(self.record), "job": dict(self.job) if self.job else None,
                    "surveying": self.surveying}

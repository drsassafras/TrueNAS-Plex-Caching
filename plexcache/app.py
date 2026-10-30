"""Application core: shared state and background workers."""

import datetime as dt
import hashlib
import ipaddress
import contextlib
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import cachemode, checks, config, mapping, prefetch, report, version_label, zfs
from .activity import Activity
from .cachelog import CacheLog
from .auth import Auth
from .homeassistant import HomeAssistantBridge
from .i18n import Fixed, M, join, msg, size_text
from .updates import UpdateChecker
from .config import PLEX_DEFAULT_PORT, Settings
from .coverage import Coverage
from .evaluate import Evaluator
from .filehistory import FileHistory
from .filemap import FileMaps, Inspector
from .pauses import L2arcPauses
from .jobs import Jobs
from .converting import Conversions
from .plex2 import Mirrors
from .peerlink import PeerLinks
from .mirrorhealth import MirrorHealth
from .sync import Sync
from .trash import Trash
from .tools.merge import MergeDuplicates
from .tools.versions import DuplicateVersions
from .tools.folders import OrganizeFolders
from .tools.unlink import UnlinkFiles
from .tools.convert import ConvertToMkv
from .tools.sync import SyncLibraries
from .libhealth import defaults as libhealth_defaults
from .libhealth import scans as libhealth_scans
from .languages import language_name
from .migrating import Migration
from .optimizing import DEFAULTS as OPT_DEFAULTS, Optimizing
from .playback import PlaybackMonitor
from .plex import PlexClient, PlexError
from .reader import MiB, benchmark_file
from . import platform as platform_mod
from . import peers, plexbackup, safety
from .scheduler import TIMED, due, next_run
from .truenas import METHODS as TRUENAS_METHODS, TrueNASClient, TrueNASError, describe_pool, missing_methods, probe as probe_truenas
from . import libdata
from .warmup import KIND_SECONDS, KINDS, L2ARC_RATE_SHARE, NEXT_DAY, next_day_check, LibraryStats, WarmupRun, plan, resolve_schedule

GiB = 1024 ** 3
log = logging.getLogger("plexcache")
TRUENAS_PROBE_PORT = 443  # tests point this at a fake server


# the parts of a Library Health answer's sig (App._health_sig) each check page's count is of: the data's stamp, the checks'
# settings, the items ignored — and File Checks the inspector's reads, Mirror Checks the comparing's, the tools' chips their
# options. A change to the others leaves the category's count as it is (App.health_current)
HEALTH_COUNTS = "health-counts.json"  # the last health_counts, kept over a restart
HEALTH_PARTS = {"file": (0, 1, 2, 5), "plex": (0, 1, 2), "mirror": (0, 1, 2, 4), "tools": (0, 1, 2, 3)}


def _no_sources(sig: tuple) -> tuple:
    """A Library Health sig but what the sources found (its part 4): what the answer without their findings is of."""
    return sig[:4] + sig[5:]


def _rev(sig: tuple) -> str:
    """A short name for what a Library Health answer was worked out from (App._health_sig), for the page to compare."""
    return hashlib.sha1(repr(sig).encode()).hexdigest()[:12]


def _split_host(host_header: str | None) -> tuple[str | None, int | None]:
    """'192.168.1.10:8643' -> ('192.168.1.10', 8643); (None, None) for loopback and junk."""
    if not host_header:
        return None, None
    h, port = host_header.strip(), None
    if h.startswith("["):
        end = h.find("]")
        h, rest = (h[1:end], h[end + 1:]) if end > 0 else ("", "")
        if rest.startswith(":"):
            port = rest[1:]
    elif h.count(":") == 1:
        h, port = h.split(":")
    if port is not None:
        if not port.isdigit() or not 0 < int(port) < 65536:
            return None, None
        port = int(port)
    if (not h or len(h) > 253 or h.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
            or not all(c.isalnum() or c in ".-:" for c in h)):
        return None, None
    return h, port


def _host_only(host_header: str | None) -> str | None:
    return _split_host(host_header)[0]


LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".localhost")


def _is_local_name(host: str) -> bool:
    """An IP address, or a name that can only mean something on the home network. A public
    domain name could be a DNS-rebinding trick, pointing at the NAS from someone else's page."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    h = host.lower().rstrip(".")
    return "." not in h or h.endswith(LOCAL_SUFFIXES)


KIND_LABEL = {"movie": M("movies"), "tv": M("TV episodes"), "audio": M("audio files")}  # (the warm-up event's)


def index_summary(result: dict) -> M:
    """e.g. "metadata at the end of files — movies: 3 of 3 had some, 0.2 GB (largest 2.0 MB)": what warm-up
    read there, apart from the lead-ins (a file a lead-in covers whole doesn't count)."""
    def size(n):
        if n >= 1e8:
            return M("{n} GB", n=Fixed(n / 1e9, 1))
        return M("{n} MB", n=Fixed(n / 1e6, 1)) if n >= 1e6 else M("{n} KB", n=Fixed(n / 1e3, 0))
    bits, mp4 = [], 0
    for kind, st in result["index"].items():
        files = (result.get("by_kind") or {}).get(kind, {}).get("files", 0)
        if not files:
            continue
        mp4 += st["mp4_at_end"]
        if st["files"]:
            bits.append(M("{kind}: {n} of {files} had some, {size} (largest {largest})", kind=KIND_LABEL[kind],
                          n=st["files"], files=files, size=size(st["bytes"]), largest=size(st["largest"])))
        else:
            bits.append(M("{kind}: none outside the lead-ins", kind=KIND_LABEL[kind]))
    joined = bits[0]
    for b in bits[1:]:
        joined = M("{a}; {b}", a=joined, b=b)
    text = M("metadata at the end of files — {list}", list=joined)
    if mp4:
        text = M("{text}. {n} MP4 files keep their index at the end (no “fast start”)",
                 one="{text}. {n} MP4 file keeps its index at the end (no “fast start”)", text=text, n=mp4)
    return text


def file_block(m: dict | None, size: int, record: int | None) -> int | None:
    """The size of the blocks a file is stored in: as read from it, or the dataset's record size —
    except a file no larger than that, which ZFS keeps in one block of its own size (in 4K sectors)."""
    if m and m.get("blksize"):
        return m["blksize"]
    if record and 0 < size <= record:
        return -(-size // 4096) * 4096
    return record



@contextlib.contextmanager
def _closing_errors():
    """An HTTP error's response closed before it's raised on: left open, it warns when it's dropped."""
    try:
        yield
    except urllib.error.HTTPError as e:
        e.close()
        raise


class App:
    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self.settings = Settings(config_dir)
        # Optional web UI login, from PLEXCACHE_USERNAME / PLEXCACHE_PASSWORD in the app's YAML.
        self.auth = Auth.from_env(config_dir)
        self.activity = Activity(config_dir)
        self.lock = threading.RLock()
        self.version = version_label()  # (0.16.0, or 0.16.0-dev for a build of the dev branch)
        # Library scan
        self.files: list = []
        self._posters: tuple | None = None  # (files, {path: (poster, title)}) for poster_of() and title_of()
        self._thumbs: dict[str, bytes] = {}  # poster thumbnails, oldest first
        self.libraries: list = []
        self.libraries_read = False  # (Plex's libraries read once: until then no count says a page doesn't apply)
        self.stats = LibraryStats()
        self.scan_error: str | None = None
        self.scanning = False
        # ZFS / TrueNAS
        self.zfs: dict = {}
        # Health
        self.health: dict | None = None
        self.checking = False
        self.locations: list = []
        self.mapping_choices: list = []
        # Connection tests shown on the Settings page
        self.connections: dict = {}
        self.testing = False
        self.host: str | None = None  # how the browser reaches us (usually the NAS's IP)
        # The web UI's address as the browser sees it, port included (the app can't know which
        # host port TrueNAS maps to its 8642). Kept, so links work before anyone opens the page.
        self.web_url: str | None = (self._load_json("web.json") or {}).get("url")
        self._autodetected: set = set()
        # Warm-up
        self.warmup_run: WarmupRun | None = None
        self.warmup_stop = threading.Event()
        self.last_warmup: dt.datetime | None = None
        runs = self.activity.snapshot()["runs"]
        if runs:
            self.last_warmup = dt.datetime.fromtimestamp(runs[0]["started"])
        # Benchmark
        self.benchmarking = False
        self.benchmark: dict | None = None
        self.benchmark_progress: dict | None = None  # {"dataset", "done", "total", "finished": [...]} while it runs
        # What's been cached, when, and whether it was still there when read again (cachelog.py)
        self.cachelog = CacheLog(os.path.join(config_dir, "cachelog.json"))
        # How much of each library was cached, hour by hour (coverage.py)
        self.coverage = Coverage(os.path.join(config_dir, "coverage.json"))
        self.cachelog.boot_time = zfs.boot_time() or 0.0
        # Playback
        self.monitor = PlaybackMonitor(self)
        self.jobs = Jobs(self)  # the one file job at a time: optimizing, syncing cover art, copying a folder
        self.optimizing = Optimizing(self)
        self.migration = Migration(self)
        self.conversions = Conversions(self)  # converted files waiting for Plex before their originals go
        self.mirrors = Mirrors(self)  # the mirrors: other Plex servers with the same files, chosen per library (plex2.py)
        self.sync = Sync(self)  # the mirror: whether there's one, and the servers that may be (sync.py; Sync Libraries is a tool)
        self.trash = Trash(self)  # files the tools set aside, purged once Plex (here and mirrored) no longer lists them
        self.tool_runs: dict = self._load_json("tool-runs.json") or {}  # {library: {tool: when}} — told to the other Plex Cache (peers.py)
        self.peers_summary: dict | None = None
        self.published_markers: dict[str, float] = {}  # library -> when our marker was last written there (peers.publish)
        self.published_bodies: dict[str, dict] = {}  # library -> the marker as written, for the link to carry (peers.carry)
        self.first_published: dict[str, float] = self._load_json("published.json") or {}  # library -> when first: for "never arrived"
        self.peerlinks = PeerLinks(self)  # the Plex Cache at each mirror's site: found, paired, asked (peerlink.py)
        self.mirror_health = MirrorHealth(self)  # Tools → Mirror Checks: the two sites' files and Plexes compared (mirrorhealth.py)
        self.libdata = libdata.Gatherer(self)  # the library data layer: records and files, gathered once, read by every check and tool
        self.tools = {t.name: t for t in (MergeDuplicates(self), DuplicateVersions(self), OrganizeFolders(self), UnlinkFiles(self), ConvertToMkv(self), SyncLibraries(self))}  # the Tools pages
        from .tools import ToolJob
        self.jobs.register("tool", lambda args, saved: ToolJob(self, args, saved))  # a tool's Apply: the backup, then the rows (Processes)
        self.health_counts: dict[str, dict] = {}  # library -> {"file": n, "plex": n, "ignored": n, "items": n, "stamp"}: Library Health's counts for the cards
        # (the last ones kept over a restart, of no state — sig None: not current — the numbers a page shows beside its wheel,
        # as last known, while they're worked out again: evaluate.Evaluator.count)
        self._load_health_counts()
        self._health: dict[str, tuple] = {}  # library -> (what it was worked out from: _health_sig, Library Health's answer)
        self._health_base: dict[str, tuple] = {}  # library -> (_health_sig but the sources' stamps, the answer without their findings)
        self._health_lock = threading.Lock()
        # what's where in each media file (the Library Health page), and the job that reads it
        self.file_maps = FileMaps(self.config_dir)
        self.inspector = Inspector(self)  # Library Health: each library's files read, one library at a time
        # every count a page shows, worked out before it's shown, in one queue — the page open first (evaluate.py)
        self.evaluator = Evaluator(self)
        self.libdata.listeners.append(self.evaluator.data_changed)  # (a gathering done: what reads it worked out again)
        # each file's history with the optimizer, kept for good: the Metrics page's original against now
        self.history = FileHistory(self.config_dir, log=lambda e: self.jobs.log.add(e))
        for p, e in list(self.file_maps.files.items()):  # (optimized before it was kept: what was kept then)
            self.history.seed(p, "", e.get("before"), e.get("map"), e.get("id") or [])
        self.history.save()
        # Home Assistant over MQTT (does nothing until it's switched on in System)
        self.ha = HomeAssistantBridge(self)
        self.updates = UpdateChecker(self)
        # L2ARC-only choices that had to fall back to their RAM-only default (Health lists them)
        self.fallbacks = cachemode.Fallbacks()
        self._slots: dict = {}
        self._slots_lock = threading.Lock()
        self.stopping = threading.Event()

    # --------------------------------------------------------------- helpers
    def map_path(self, path: str) -> str:
        """Where a path Plex reports can be found here (longest mapping wins)."""
        best = None
        for row in self.settings.section("general")["path_map"]:
            plex = row["plex"]
            if (path == plex or path.startswith(plex.rstrip("/") + "/")) and (best is None or len(plex) > len(best["plex"])):
                best = row
        if best is None:
            return path
        return best["local"] + path[len(best["plex"]):]

    def plex(self, timeout: float = 30) -> PlexClient:
        p = self.settings.section("plex")
        client = PlexClient(p["url"], p["token"], timeout=timeout)
        client.before_change = lambda c, op, keys: plexbackup.guard(self, c, "here", op, keys)  # (each change to its records backed up first)
        return client

    def truenas(self) -> TrueNASClient:
        t = self.settings.section("truenas")
        return TrueNASClient(t["url"], t["api_key"], verify_tls=t["verify_tls"])

    def job_slots(self, feature: str, n: int) -> threading.BoundedSemaphore:
        with self._slots_lock:
            key = (feature, n)
            if key not in self._slots:
                self._slots[key] = threading.BoundedSemaphore(n)
            return self._slots[key]

    def l2_total(self) -> int | None:
        """L2ARC size across the pools that hold cached libraries."""
        pools = [p for p in (self.zfs or {}).get("pools", {}).values() if p]
        return sum(p["l2_total"] for p in pools) if pools else None

    def start(self):
        self.monitor.start()
        self.ha.start()
        self.updates.start()
        threading.Thread(target=self._scheduler_loop, name="scheduler", daemon=True).start()
        threading.Thread(target=self._startup, name="startup", daemon=True).start()

    def _startup(self):
        self.scan_library()
        self.run_checks()
        self.optimizing.resume_job()   # a file job that was running before a restart, started again where it was
        # Every schedule but "none" warms up at boot (at boot only is the default with an L2ARC)
        boot = {k for k, v in self.warmup_schedules().items() if v != "none"}
        if boot:
            self.start_warmup(manual=False, libs=boot, why=M("at boot"))

    def _refresh_migration(self):
        try:
            self.migration.refresh()
        except Exception:  # noqa: BLE001 — checked again next time
            log.debug("migration re-check failed", exc_info=True)

    def _scheduler_loop(self):
        last_check = dt.datetime.now()
        last_health = last_tools = time.time()
        last_publish = 0.0
        last_prune = time.time()
        while not self.stopping.wait(30):
            now = dt.datetime.now()
            w = self.settings.section("warmup")
            if self.warmup_run is None:
                libs = set()
                for sched in TIMED:
                    if due(w, now, self.last_warmup, last_check, sched):
                        libs |= self.warmup_libraries(sched)
                if libs:
                    self.start_warmup(manual=False, libs=libs)
            last_check = now
            self.check_next_day()
            # a folder being converted to a child dataset: its step's conditions checked again by themselves
            mst = (self.migration.state or {}).get("step")
            if mst in ("create", "switch", "verify"):
                threading.Thread(target=self._refresh_migration, daemon=True).start()
            # the mirror: looked for weekly among the account's servers, while it isn't chosen
            if self.sync.settings().get("discover") and time.time() - self.sync.discovered_at > 7 * 86400 and self.mirrors.undecided():
                self.sync.discover_async()
            # converted files: Plex asked whether it has the MKVs, originals deleted once it has (and the hold is over)
            if any(e["state"] != "deleted" for e in self.conversions.items):
                threading.Thread(target=self.conversions.tick, name="conversions", daemon=True).start()
            # files the tools set aside: purged once Plex no longer lists them (and the mirror's), after the hold
            if any(e["state"] == "waiting" for e in self.trash.items):
                threading.Thread(target=self.trash.tick, name="trash", daemon=True).start()
            if time.time() - last_prune > 3600:  # the snapshots the app took, deleted after their two weeks (safety.py)
                last_prune = time.time()
                threading.Thread(target=lambda: safety.prune_snapshots(self), name="snapshots", daemon=True).start()
                plexbackup.prune(self)  # (and the Plex record backups, after the same days)
            if time.time() - last_publish > peers.PUBLISH_EVERY and self.libraries:  # our marker in the shared folders (peers.py)
                last_publish = time.time()
                threading.Thread(target=self.publish_peers, name="peers", daemon=True).start()
            self.peerlinks.tick_async()  # the Plex Cache at each mirror's site: found, paired, asked how its side is (every few minutes)
            self.evaluator.kick()  # (what's due again: Mirror Checks every few hours)
            if self.libdata.started:  # the library data layer: a changed library (Plex's stamp, a folder's mtime) gathered again
                threading.Thread(target=self.libdata.tick, name="libdata-tick", daemon=True).start()
            if time.time() - last_tools > 300:  # the tools' follow-ups on the mirror (Organize Folders)
                last_tools = time.time()
                for tool in self.tools.values():
                    if tool.waiting():
                        threading.Thread(target=tool.tick, name=f"tool-{tool.name}", daemon=True).start()
            self.cachelog.prune()
            self.cachelog.save()
            self.record_coverage()
            if time.time() - last_health > 6 * 3600:
                last_health = time.time()
                self.run_checks_async()

    # --------------------------------------------------------------- library
    def refresh_library_files(self, key: str) -> None:
        """One library's files listed from Plex again — the data layer asks when it reads the library's records again
        (Plex's change stamp moved, or Scan Again): that library's part of app.files replaced, the stats and the count with
        it, so what Plex has now is what every tool and check sees."""
        from .plex import Library
        info = self.library_info(key) or {}
        if not info.get("kind") or info.get("excluded"):
            return
        lib = Library(str(info["key"]), info.get("title") or "", info.get("type") or "", info.get("kind"), list(info.get("locations") or []), str(info.get("changed") or ""))
        lf = self.plex(timeout=120).library_files(lib)
        with self.lock:
            self.files = [f for f in self.files if str(f.library_key) != str(key)] + lf
            for i in self.libraries:
                if str(i["key"]) == str(key):
                    i["count"] = len(lf)
            self.stats = LibraryStats.from_files(self.files, {i["key"]: i["title"] for i in self.libraries})

    def scan_library(self) -> None:
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        try:
            client = self.plex(timeout=120)
            libs = client.libraries()
            excluded = set(self.settings.section("general")["excluded_libraries"])
            files, lib_info = [], []
            for lib in libs:
                info = {"key": lib.key, "title": lib.title, "type": lib.type, "kind": lib.kind, "changed": lib.changed,
                        "locations": lib.locations, "excluded": lib.key in excluded or lib.title in excluded, "count": 0}
                if lib.kind and not info["excluded"]:
                    lf = client.library_files(lib)
                    info["count"] = len(lf)
                    # A few files per library folder, to test where they can be read.
                    info["samples"] = {}
                    for loc in lib.locations:
                        loc = loc.rstrip("/") or "/"
                        mine = [f.path for f in lf if f.path.startswith(loc + "/")]
                        info["samples"][loc] = random.sample(mine, min(3, len(mine)))
                    files.extend(lf)
                lib_info.append(info)
            with self.lock:
                self.files, self.libraries = files, lib_info
                self.libraries_read = True
                self.stats = LibraryStats.from_files(files, {i["key"]: i["title"] for i in lib_info})
                self.scan_error = None
            self.libdata.invalidate_files()  # (Plex's listing read again: the data layer's files follow)
            self._share_out_old_choices()
        except PlexError as e:
            with self.lock:
                self.scan_error = msg(e)
        finally:
            with self.lock:
                self.scanning = False

    # ---------------------------------------------------------- connections
    def test_connections(self) -> dict:
        """Test Plex and TrueNAS step by step, for the lights on the Settings page."""
        s = self.settings.get()
        res: dict = {"at": time.time()}
        # Plex: is a server there (no token needed), then is the token accepted?
        p = s["plex"]
        if not p["url"]:
            res["plex_server"] = {"status": "warn", "text": M("Plex server address needed"),
                                  "detail": M("Enter your Plex server’s address, e.g. your NAS’s IP. "
                                              "Port {port} is used if you leave it off.", port=str(PLEX_DEFAULT_PORT))}
        else:
            try:
                ident = PlexClient(p["url"], "", timeout=8).identity()
                res["plex_server"] = {"status": "ok", "text": M("Plex server found"),
                                      "detail": M("Plex Media Server {version} at {url} ({ms} ms)",
                                                  version=ident["version"], url=p["url"], ms=ident["ms"])}
            except PlexError as e:
                if "127.0.0.1" in p["url"] or "localhost" in p["url"]:
                    detail = M("{why}. Use your NAS’s IP, not 127.0.0.1 — inside this app that means the app itself.", why=msg(e))
                else:
                    detail = M("{why}.", why=msg(e))
                res["plex_server"] = {"status": "fail", "text": M("No Plex server found"), "detail": detail}
        if res["plex_server"]["status"] != "ok":
            res["plex_token"] = {"status": "off", "text": M("Plex sign-in: waiting for the server"),
                                 "detail": M("Sign-in is checked once the server is found.")}
        elif not p["token"]:
            # Without a token Plex shares nothing, so no feature can work: a failure, not a warning.
            res["plex_token"] = {"status": "fail", "text": M("Plex sign-in needed"),
                                 "detail": M("Nothing works until you sign in: Plex only shares your libraries and "
                                             "what’s playing with apps that have a token for your account.")}
        else:
            try:
                info = self.plex(timeout=10).ping()
                self.plex_name = info["name"]  # (how Plex names this server: beside the mirror's, on the Mirror page)
                n = len(self.plex(timeout=10).libraries())
                res["plex_token"] = {"status": "ok", "text": M("Signed in to Plex"),
                                     "detail": M("Token accepted by “{name}” · {n} libraries visible",
                                                 one="Token accepted by “{name}” · {n} library visible", name=info["name"], n=n)}
            except PlexError as e:
                bad = "token" in str(e).lower()
                res["plex_token"] = {"status": "fail", "text": M("Plex token rejected") if bad else M("Couldn’t check Plex sign-in"),
                                     "detail": (M("Plex no longer accepts the saved token. Sign in again.")
                                                if bad else msg(e))}
        # TrueNAS: optional. Is the API there, then is the key accepted?
        t = s["truenas"]
        if not t["url"]:
            res["truenas_server"] = {"status": "warn", "text": M("TrueNAS not set up (optional)"),
                                     "detail": M("Without it, cache devices and dataset settings can’t be checked, "
                                                 "and the L2ARC can’t be sized.")}
            res["truenas_key"] = {"status": "off", "text": M("TrueNAS API key"), "detail": ""}
            res["truenas_api"] = {"status": "off", "text": M("API compatibility"), "detail": ""}
        elif not probe_truenas(t["url"]):
            res["truenas_server"] = {"status": "fail", "text": M("TrueNAS not found"),
                                     "detail": M("No TrueNAS API answered at {url}.", url=t["url"])}
            res["truenas_key"] = {"status": "off", "text": M("API key: waiting for TrueNAS"), "detail": ""}
            res["truenas_api"] = {"status": "off", "text": M("API compatibility"), "detail": ""}
        else:
            res["truenas_server"] = {"status": "ok", "text": M("TrueNAS found"), "detail": t["url"]}
            if not t["api_key"]:
                res["truenas_key"] = {"status": "warn", "text": M("TrueNAS API key needed"),
                                      "detail": M("Add a key so the app can read pool and dataset settings. "
                                                  "It only reads; nothing is changed.")}
                res["truenas_api"] = {"status": "off", "text": M("API compatibility"), "detail": ""}
            else:
                try:
                    with self.truenas() as tn:
                        v = tn.version()
                        known = tn.known_methods()
                    res["truenas_key"] = {"status": "ok", "text": M("TrueNAS API key accepted"), "detail": v or M("Connected")}
                    res["truenas_api"] = self._api_compatibility(known, v)
                except TrueNASError as e:
                    res["truenas_key"] = {"status": "fail", "text": M("TrueNAS API key not accepted"), "detail": msg(e)}
                    res["truenas_api"] = {"status": "off", "text": M("API compatibility"), "detail": ""}
        problems = [k for k in ("plex_server", "plex_token") if res[k]["status"] != "ok"]
        optional = [k for k in ("truenas_server", "truenas_key", "truenas_api") if res[k]["status"] in ("warn", "fail")]
        if problems:
            res["summary"] = {"status": "fail" if any(res[k]["status"] == "fail" for k in problems) else "warn",
                              "text": M("Plex needs attention — the app can’t do anything until Plex is connected.")}
        elif optional:
            res["summary"] = {"status": "warn", "text": M("Plex is connected. TrueNAS is optional but recommended.")}
        else:
            res["summary"] = {"status": "ok", "text": M("Everything is connected.")}
        with self.lock:
            self.connections = res
        return res

    @staticmethod
    def _api_compatibility(known, version: str) -> dict:
        """The API compatibility light (System → TrueNAS): every call the app makes (truenas.METHODS) against
        what this TrueNAS release offers (core.get_methods). Green when each has a name there; orange naming
        what hasn't — to be reported, so the app can learn the new name."""
        total = len(TRUENAS_METHODS)
        if known is None:
            return {"status": "warn", "text": M("API compatibility: not checked"),
                    "detail": M("This TrueNAS wouldn’t list its API methods (core.get_methods), so the app’s {n} calls couldn’t be checked against it. Please report the TrueNAS version ({version}) on GitHub.", n=total, version=version or "?")}
        gone = missing_methods(known)
        if gone:
            return {"status": "warn", "text": M("API compatibility: {n} of {total} calls missing", n=len(gone), total=total),
                    "detail": M("{version} hasn’t got: {what}. The features that need them won’t work until the app learns the new names — please report this TrueNAS version and these calls on GitHub.",
                                version=version or "?", what="; ".join(f"{what} ({', '.join(names)})" for what, names in gone.items()))}
        return {"status": "ok", "text": M("API compatibility: all {total} calls known", total=total),
                "detail": M("Every call the app makes has a name {version} offers.", version=version or "")}

    def test_connections_async(self, then_checks: bool = True, rescan: bool = False):
        with self.lock:
            self.testing = True

        def work():
            try:
                self.test_connections()
            finally:
                with self.lock:
                    self.testing = False
            if then_checks:
                if rescan:
                    self.scan_library()
                self.run_checks()
            self.mirrors.refresh_async()  # (the mirrors' libraries, and how their paths line up)

        threading.Thread(target=work, daemon=True).start()

    def note_host(self, host_header: str | None):
        """Remember how the browser reached us (for links, e.g. Home Assistant's "Visit"),
        and fill in empty Plex/TrueNAS addresses from it.

        Only called for signed-in requests. The browser usually talks to us via the NAS's
        own IP, which is also where Plex and TrueNAS live on a typical install.
        """
        host, port = _split_host(host_header)
        if not host or not _is_local_name(host):
            return
        address = f"[{host}]" if ":" in host else host
        web_url = f"http://{address}" + (f":{port}" if port and port != 80 else "")
        if web_url != self.web_url:
            self.web_url = web_url
            self._save_json("web.json", {"url": web_url})
            if getattr(self, "ha", None):
                self.ha.wake.set()
        if host == self.host:
            return
        self.host = host
        if host in self._autodetected:
            return
        self._autodetected.add(host)
        threading.Thread(target=self._autodetect, args=(host,), daemon=True).start()

    def _autodetect(self, host: str):
        try:
            self._autodetect_inner(host)
        except Exception:  # a background helper: log it, never let it die silently
            log.exception("auto-detecting Plex/TrueNAS at %s failed", host)

    def _autodetect_inner(self, host: str):
        s = self.settings.get()
        changed = []
        h = f"[{host}]" if ":" in host else host
        if not s["plex"]["url"]:
            url = f"http://{h}:{PLEX_DEFAULT_PORT}"
            try:
                PlexClient(url, "", timeout=4).identity()
                self.settings.update({"plex": {"url": url}})
                changed.append(M("Plex at {url}", url=url))
            except PlexError:
                pass
        if not s["truenas"]["url"]:
            url = f"https://{h}" + ("" if TRUENAS_PROBE_PORT == 443 else f":{TRUENAS_PROBE_PORT}")
            if probe_truenas(url, timeout=4):
                self.settings.update({"truenas": {"url": url}})
                changed.append(M("TrueNAS at {url}", url=url))
        if changed:
            self.activity.event("settings", M("found {list}", list=join(changed)))
        self.test_connections_async(then_checks=bool(changed), rescan=bool(changed))

    def _media(self, path: str) -> tuple:
        files = self.files
        if not self._posters or self._posters[0] is not files:
            self._posters = (files, {self.map_path(f.path): (getattr(f, "poster", ""), getattr(f, "title", "")) for f in files})
        return self._posters[1].get(path) or ("", "")

    def poster_of(self, path: str) -> str:
        """The Plex poster of a file (as this app sees its path), from the last scan, or ""."""
        return self._media(path)[0]

    def title_of(self, path: str) -> str:
        """How Plex names a file's item ("Title (year)", "Show · S01E05"), from the last scan, or ""."""
        return self._media(path)[1]

    def poster_thumb(self, path: str, server: str = "") -> bytes:
        """A small poster for lists (the Processes page), kept in memory: the last few hundred.
        ``server``: a mirror's machine id for one of its (Sync Libraries' rows), else this server's."""
        key = (server or "") + path
        with self.lock:
            hit = self._thumbs.pop(key, None)
            if hit is not None:
                self._thumbs[key] = hit
                return hit
        data = (self.mirrors.client(server, timeout=15) if server else self.plex(timeout=15)).thumb(path)
        with self.lock:
            self._thumbs[key] = data
            while len(self._thumbs) > 400:
                self._thumbs.pop(next(iter(self._thumbs)))
        return data

    def match_info(self, guid: str, image: bool = False):
        """What a Plex match (plex://movie/…) is, from Plex's own metadata service, with this server's
        token: {"title", "year", "type", "summary", "url"} — ``url`` its public page (watch.plex.tv) when
        Plex names one — or, ``image``, its poster's bytes. Kept in memory. None: not a plex:// match."""
        import re
        import urllib.parse
        import urllib.request
        m = re.fullmatch(r"plex://(movie|show|season|episode|artist|album|track)/([0-9a-f]{12,40})", guid or "")
        if not m:
            return None
        cache = self.__dict__.setdefault("_matches", {})
        got = cache.get(guid)
        if got is None:
            req = urllib.request.Request("https://metadata.provider.plex.tv/library/metadata/" + m.group(2) + "?includeGuids=1")
            req.add_header("Accept", "application/json")
            req.add_header("X-Plex-Token", self.settings.section("plex")["token"])
            with _closing_errors(), urllib.request.urlopen(req, timeout=15) as resp:
                md = ((json.loads(resp.read() or b"{}").get("MediaContainer") or {}).get("Metadata") or [{}])[0]
            kind = md.get("type") or m.group(1)
            slug = md.get("slug")
            got = {"title": md.get("title", ""), "year": md.get("year"), "type": kind, "summary": md.get("summary", ""),
                   # (its public page as Plex names it; else built from its slug)
                   "url": md.get("publicPagesURL") or (f"https://watch.plex.tv/{kind}/{urllib.parse.quote(slug)}" if slug and kind in ("movie", "show") else None)
                   or self._public_page(md),
                   "thumb": md.get("thumb") or ""}
            cache[guid] = got
            while len(cache) > 500:
                cache.pop(next(iter(cache)))
        if not image:
            return {k: v for k, v in got.items() if k != "thumb"}
        if not got["thumb"].startswith("https://"):
            raise OSError("no poster")
        req = urllib.request.Request(got["thumb"])
        with _closing_errors(), urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read(4 * 1024 * 1024)

    @staticmethod
    def _public_page(md: dict) -> str:
        """A page anyone can open for a Plex entry without its public page: IMDb, TMDB or TheTVDB (by
        the ids Plex lists for it), else a search for its title on watch.plex.tv."""
        import urllib.parse
        ids = {}
        for g in md.get("Guid") or []:
            k, _, v = str(g.get("id", "")).partition("://")
            ids.setdefault(k, v)
        if ids.get("imdb"):
            return f"https://www.imdb.com/title/{ids['imdb']}/"
        if ids.get("tmdb"):
            return f"https://www.themoviedb.org/{'tv' if md.get('type') == 'show' else 'movie'}/{ids['tmdb']}"
        if ids.get("tvdb"):
            return f"https://thetvdb.com/dereferrer/series/{ids['tvdb']}"
        q = " ".join(str(x) for x in (md.get("title"), md.get("year")) if x)
        return "https://watch.plex.tv/search?q=" + urllib.parse.quote(q) if q else ""

    def library_info(self, key: str) -> dict | None:
        """A library from the last scan ({key, title, kind, ...}), or None."""
        return next((lib for lib in self.libraries if lib["key"] == str(key)), None)

    def _health_sig(self, key: str) -> tuple:
        """What a library's Library Health answer is worked out from: its data's state, the checks' settings, the items
        ignored, the tools' options for it (what their chips say), what the sources found (Mirror Checks' last compare),
        the files the inspector has read there — the answer kept until one of them changes (HEALTH_PARTS: which of them
        each check page's count is of)."""
        from . import libhealth

        def work():
            return (self.libdata.data(key).stamp, json.dumps(self.settings.get().get("library_health") or {}, sort_keys=True, default=str),
                    json.dumps(libhealth.ignored(self).get(str(key)) or {}, sort_keys=True, default=str),
                    json.dumps({name: t.enabled(key) for name, t in (getattr(self, "tools", None) or {}).items()}, sort_keys=True, default=str),
                    libhealth.stamps(self, key), self.inspector.rev(key))
        ev = getattr(self, "evaluator", None)
        return ev.memo("health_sig", str(key), work) if ev is not None else work()

    def health_current(self, key: str, cat: str | None = None) -> bool:
        """Whether a library's Library Health counts are of its state now — all of them, or one category's (``cat``:
        HEALTH_PARTS), whose count a change to the rest leaves as it is."""
        got = self.health_counts.get(str(key))
        if not got or not got.get("sig"):
            return False
        sig = self._health_sig(str(key))
        if cat is None:
            return got["sig"] == sig
        return all(got["sig"][i] == sig[i] for i in HEALTH_PARTS.get(cat, range(len(sig))))

    def library_health(self, key: str) -> dict:
        """A library's items through Library Health's checks (libhealth.library_health), worked out once for what it's
        worked out from (_health_sig) and kept: worked out by the evaluator as the data changes (evaluate.py), so opening a
        check page — or a library on it — shows it at once. When only the sources' findings changed (a Mirror Checks
        compare), they're laid over the rest as kept (libhealth.with_sources): quick."""
        from . import libhealth
        key = str(key)
        sig = self._health_sig(key)
        with self._health_lock:
            got, base = self._health.get(key), self._health_base.get(key)
        if got and got[0] == sig:
            return got[1]
        if not base or base[0] != _no_sources(sig):
            base = libhealth.base_health(self, key)
            sig = self._health_sig(key) if sig[0] != self.libdata.data(key).stamp else sig  # (gathered just now for it: the new state's)
            base = (_no_sources(sig), base)
            with self._health_lock:
                self._health_base[key] = base
        out = libhealth.with_sources(self, key, base[1])
        out["rev"] = _rev(sig)  # (the page holds on to an answer until the state's count says a newer one is out)
        with self._health_lock:
            self._health[key] = (sig, out)
        self._store_health_counts(key, sig, out)
        return out

    def forget_health(self, key: str) -> None:
        """A library's Library Health answer dropped, to be worked out whole again (its stale period up: evaluate.py)."""
        with self._health_lock:
            self._health.pop(str(key), None)
            self._health_base.pop(str(key), None)

    def health_kept(self, key: str) -> dict | None:
        """A library's Library Health answer as last worked out, whatever it was worked out from (None: none yet)."""
        with self._health_lock:
            got = self._health.get(str(key))
        return got[1] if got else None

    def _load_health_counts(self) -> None:
        """The last health_counts, as saved (of no state now: not current until worked out again)."""
        for k, v in (self._load_json(HEALTH_COUNTS) or {}).items():
            if isinstance(v, dict):
                self.health_counts[str(k)] = dict(v, sig=None)

    def _store_health_counts(self, key: str, sig: tuple, got: dict) -> None:
        """The counts the check pages' tabs, the Tools page's cards and the menu show: per category, the items a check
        switched on finds something on (each once), ignored items apart."""
        on = {c["id"]: c.get("on") for cs in got["checks"].values() for c in cs}
        of = {c["id"]: c.get("of") for cs in got["checks"].values() for c in cs}
        live_check = {i: bool(v) and (not of.get(i) or bool(on.get(of[i]))) for i, v in on.items()}
        cat_of = {c["id"]: cat for cat, cs in got["checks"].items() for c in cs}
        counts: dict[str, int] = dict.fromkeys(got["checks"], 0)  # (every category: file, plex, mirror, tools)
        ignored = 0
        for it in got["items"]:
            live = {r["id"] for r in it["results"] if not r.get("ignored") and live_check.get(r["id"], True)}
            for cat in set(cat_of.get(i, "") for i in live):
                if cat in counts:
                    counts[cat] += 1  # (an item once per category, however many of its checks found something)
            ignored += any(r.get("ignored") for r in it["results"])
        # the categories whose last scan couldn't run a check that's switched on (Mirror Checks without the other site's
        # files): their counts shown red, from the check's own row down to the tab, the Tools card and the menu
        unchecked = sorted({cat for cat, us in (got.get("unchecked") or {}).items() for u in us for i in u.get("checks") or [] if live_check.get(i, True)})
        self.health_counts[key] = dict(counts, ignored=ignored, items=got["stats"]["items"], flagged=got["stats"]["flagged"], unchecked=unchecked,
                                       unread=int(got.get("unread") or 0), outdated=int(got.get("outdated") or 0), files=int(got.get("files") or 0),
                                       stamp=sig[0], rev=_rev(sig), sig=sig)
        try:
            self._save_json(HEALTH_COUNTS, {k: {x: y for x, y in v.items() if x != "sig"} for k, v in list(self.health_counts.items())})
        except Exception:  # noqa: BLE001 — (kept for the next start only)
            log.debug("health counts not saved", exc_info=True)

    def _health_counts_now(self) -> dict:
        """health_counts for the state — what's under the check pages' counts (the numbers shown are the state's "counts",
        evaluate.py): each library's, marked stale when what it was worked out from has changed since (the evaluator works
        it out again, in its turn)."""
        out = {}
        for lib in self.libraries:
            key = str(lib["key"])
            got = self.health_counts.get(key)
            if got:
                out[key] = dict({k: v for k, v in got.items() if k != "sig"}, stale=not self.health_current(key))
        return out

    def check_prefetch_libraries(self, libs) -> None:
        """Prefetch choices must be for a library Plex has, of a kind Prefetch covers, and offered
        for that kind (music has no "next album"). Raises SettingsError."""
        from . import prefetch
        if not isinstance(libs, dict):
            raise config.SettingsError(M("nextepisode.libraries must be an object"))
        for key, choice in libs.items():
            if key == "*":
                continue  # every library (from before Prefetch's switch was retired)
            lib = self.library_info(key)
            if self.libraries and lib is None:
                raise config.SettingsError(M("Plex has no library {key}", key=key))
            kind = prefetch.library_kind(lib)
            if lib is not None and kind is None:
                raise config.SettingsError(M("Prefetch doesn’t cover that library"))
            start = choice.get("start") if isinstance(choice, dict) else None
            if kind and start not in (None, "default") and start not in prefetch.levels_for(kind):
                raise config.SettingsError(M("“{mode}” isn’t a prefetch choice for that library", mode=str(start)))

    def library_datasets(self, key: str) -> list[dict]:
        """The datasets holding a library's folders (from the last check)."""
        return [d for d in (self.zfs or {}).get("datasets", []) if key in d["keys"]]

    def library_cacheable(self, key: str, feature: str | None = None) -> bool:
        """Ticked in Media Mappings, its folders can be read here, and the
        dataset under it can actually cache (primarycache=all; for warm-up
        also an L2ARC on its pool).

        Unknowns (not scanned or checked yet) are allowed, so nothing is
        silently skipped before the first check has run."""
        if not key:
            return True
        lib = next((lib for lib in self.libraries if lib["key"] == key), None)
        if lib is None:
            return True
        if lib["excluded"]:
            return False
        rows = [r for r in self.locations if r["key"] == key]
        if rows and not all(r["status"] == "ok" for r in rows):
            return False
        pools = (self.zfs or {}).get("pools", {})
        for d in self.library_datasets(key):
            prim = (d.get("props") or {}).get("primarycache")
            if prim is not None and prim != "all":
                return False
            pool = pools.get(d.get("pool"))
            if feature == "warmup" and pool is not None and not pool["l2_online"]:
                return False
        return True

    def library_l2arc(self, key: str) -> bool | None:
        """Whether a library's data can reach an SSD cache: an L2ARC online on the pool of any
        dataset holding it. The cache is per pool, so libraries can differ. None: not known yet."""
        pools = (self.zfs or {}).get("pools", {})
        seen = [pools.get(d.get("pool")) for d in self.library_datasets(key)]
        seen = [p for p in seen if p is not None]
        return any(p["l2_online"] for p in seen) if seen else None

    def feature_available(self, feature: str) -> tuple[bool, str]:
        """(True, "") or (False, why) — the same verdict the Caching page shows."""
        for r in checks.recommendations(self):
            if r["feature"] == feature and r["verdict"] == "avoid":
                return False, r["reason"]
        return True, ""

    def l2arc_status(self) -> str:
        """'present' if any pool holding cached libraries has L2ARC, else 'absent' or 'unknown'."""
        z = self.zfs or {}
        pools = [p for p in z.get("pools", {}).values() if p]
        if pools:
            return "present" if any(p["l2_online"] for p in pools) else "absent"
        arc = z.get("arc")
        if arc is not None:
            return "present" if arc["l2_present"] else "absent"
        return "unknown"

    def cache_mode(self) -> str:
        """'l2arc' or 'ram' (see cachemode)."""
        return cachemode.mode(self.settings.get(), self.l2arc_status())

    # ------------------------------------------------------------------- RAM
    def ram_budget(self) -> dict:
        """How much RAM Now Playing Cache and Prefetch may fill.

        The base is the most the ZFS RAM cache (ARC) can hold: reading more
        than that just pushes out what was read first. The system reserve
        comes off the top, and each feature gets its share of the rest. All
        streams of a feature share its limit."""
        g = self.settings.section("general")
        arc = zfs.arc_summary(zfs.arcstats())
        source = "arc"
        cache_max = (arc or {}).get("arc_max") or 0
        if arc and cache_max:
            # What the ZFS cache can actually use right now: what it holds plus free
            # RAM, never more than its maximum. RAM used by apps and services isn't
            # available, however high the maximum is set.
            free = arc.get("memory_free")
            if free is None:
                free = zfs.meminfo().get("MemFree", 0)
            available = min(cache_max, arc["arc_size"] + free)
        else:
            # No ZFS stats: fall back to half of RAM, roughly what ZFS on Linux uses by default.
            cache_max = available = zfs.meminfo().get("MemTotal", 0) // 2
            source = "ram"
        reserve = int(g["ram_reserve_gb"] * 1e9)  # GB as shown in the UI
        usable = max(0, available - reserve)
        np_pct = self.settings.section("nowplaying")["ram_percent"]
        ne_pct = self.settings.section("nextepisode")["ram_percent"]
        return {
            "cache_max": cache_max or None, "available": available or None,
            "arc_size": (arc or {}).get("arc_size"), "source": source if cache_max else None, "reserve": reserve,
            "usable": usable if cache_max else None,
            "nowplaying": int(usable * np_pct / 100), "nextepisode": int(usable * ne_pct / 100),
            "in_use": self.monitor.ram_in_use() if hasattr(self, "monitor") else {"nowplaying": 0, "nextepisode": 0},
        }

    # ------------------------------------------------------------------- zfs
    def media_datasets(self) -> list[dict]:
        """Every dataset (or other mount) holding media in this app.

        Each entry lists the libraries being cached from it (``keys`` and
        ``libraries``), libraries on it whose caching is switched off (``off``),
        and ``unused`` when it's mounted here but no Plex library is on it.
        """
        groups: dict[str, dict] = {}

        def group(m: dict) -> dict:
            gid = m.get("dataset") or m["mountpoint"]
            return groups.setdefault(gid, {"dataset": m.get("dataset"), "pool": m.get("pool"),
                                           "mountpoint": m["mountpoint"], "fstype": m["fstype"],
                                           "libraries": [], "keys": [], "off": [], "folders": [],
                                           "off_keys": [], "off_folders": [],
                                           "props": None, "unused": False})

        def attach(g: dict, row: dict, folder: str) -> None:
            if row.get("excluded"):
                if row["library"] not in g["off"]:
                    g["off"].append(row["library"])
                    g["off_keys"].append(row["key"])
                g["off_folders"].append(folder)
                return
            if row["key"] not in g["keys"]:
                g["keys"].append(row["key"])
                g["libraries"].append(row["library"])
            g["folders"].append(folder)

        mlist = zfs.mounts()
        ok_rows = [r for r in self.locations if r["status"] == "ok"]
        for row in ok_rows:
            m = zfs.mount_for(row["local_path"], mlist)
            if not m:
                continue
            g = group(m)
            row["dataset"] = g["dataset"] or g["mountpoint"]
            attach(g, row, row["local_path"])
        # The mount that actually holds each library folder (the deepest one), so a parent
        # dataset with a library on a child dataset mounted inside it doesn't count as used.
        owners = {(zfs.mount_for(r["local_path"], mlist) or {}).get("mountpoint") for r in self.locations}
        for mp in mapping.media_mounts(mlist):
            if mp in owners:
                continue
            m = zfs.mount_for(mp, mlist)
            if not m or m["mountpoint"] != mp:
                continue
            # A dataset mounted inside a library folder: Plex scans into it, so its files are
            # part of that library and its cache settings matter too.
            inside = [r for r in ok_rows if mp.startswith(r["local_path"].rstrip("/") + "/")]
            if inside:
                for row in inside:
                    attach(group(m), row, mp)
                continue
            # Mounted here, but no Plex library uses it: usually a leftover or a typo.
            group(m)["unused"] = True
        return sorted(groups.values(), key=lambda g: (g["dataset"] is None, g["dataset"] or g["mountpoint"]))

    # Uncached disk speed: measured per dataset by the benchmark, else the general guess.
    def disk_speed(self, dataset: str | None) -> int:
        g = self.settings.section("general")
        row = g["disk_speeds"].get(dataset or "")
        return row["mb_s"] if row else g["hdd_mb_per_sec"]

    def disk_speed_for_library(self, key: str) -> int:
        ds = self.library_datasets(key)
        if not ds:
            return self.settings.section("general")["hdd_mb_per_sec"]
        return min(self.disk_speed(d["dataset"] or d["mountpoint"]) for d in ds)

    def fastest_cached_disk(self) -> int:
        """The quickest uncached speed among datasets being cached (what warm-up reads at most)."""
        ds = [d for d in (self.zfs or {}).get("datasets", []) if d["keys"]]
        if not ds:
            return self.settings.section("general")["hdd_mb_per_sec"]
        return max(self.disk_speed(d["dataset"] or d["mountpoint"]) for d in ds)

    def probe_zfs(self) -> dict:
        info: dict = {"arc": zfs.arc_summary(zfs.arcstats())}
        datasets = self.media_datasets()
        info["datasets"] = datasets
        info["pools"] = {}
        info["params"] = {p: zfs.module_param(p) for p in
                          ("l2arc_noprefetch", "l2arc_write_max", "l2arc_rebuild_enabled", "l2arc_mfuonly")}
        t = self.settings.section("truenas")
        if t["url"] and t["api_key"]:
            try:
                with self.truenas() as tn:
                    info["truenas_version"] = tn.version()
                    info["platform"] = platform_mod.detect(info["truenas_version"])
                    # every call the app makes, against what this TrueNAS release offers (truenas.METHODS)
                    known = tn.known_methods()
                    ours = {n for names in TRUENAS_METHODS.values() for n in names}
                    info["truenas_methods"] = sorted(known & ours) if known is not None else None
                    info["truenas_missing"] = missing_methods(known) if known is not None else {}
                    for name in sorted({d["pool"] for d in datasets if d["pool"] and d["keys"]}):
                        pool = tn.pool(name)
                        if pool:
                            missing = [c.get("disk") for c in (pool.get("topology") or {}).get("cache") or []
                                       if not (c.get("stats") or {}).get("size") and c.get("disk")]
                            sizes = tn.disk_sizes(missing) if missing else {}
                            info["pools"][name] = describe_pool(pool, sizes)
                    for d in datasets:
                        if d["dataset"] and (d["keys"] or d["off"]):  # any dataset a Plex library is on
                            d["props"] = tn.dataset_cache_props(d["dataset"])
                            d["props_debug"] = list(getattr(tn, "last_props_raw", []) or [])
                info["truenas_error"] = None
            except TrueNASError as e:
                info["truenas_error"] = msg(e)
        self._survey_blocks(datasets)
        with self.lock:
            self.zfs = info
        if "platform" not in info:  # (no TrueNAS API: told by the host's version file, the environment, or not at all)
            info["platform"] = platform_mod.detect(None)
        return info

    def _survey_blocks(self, datasets: list[dict]) -> None:
        """The block size every file on each dataset was written with (one stat() per file, nothing
        read — a few tenths of a second for 20,000 files), so Health can tell when files predate a
        record size change."""
        kind_of = {lib["key"]: lib["kind"] for lib in self.libraries}
        for d in datasets:
            if not (d["keys"] or d["off"]) or d.get("fstype") != "zfs":
                continue
            d["blocks"] = dict(zfs.scan_block_sizes(d["mountpoint"], stop=lambda: self.stopping.is_set()), full=True)
            d["kinds"] = sorted({kind_of[k] for k in d["keys"] + d["off_keys"] if kind_of.get(k)})

    # ---------------------------------------------------------------- checks
    def run_checks(self) -> dict:
        with self.lock:
            if self.checking:
                return self.health or {}
            self.checking = True
        try:
            self.test_connections()
            locations = mapping.location_report(self)
            with self.lock:
                self.locations = locations
                self.mapping_choices = mapping.choices(locations)
            self.probe_zfs()  # needs the locations: it checks the dataset under each library
            self.optimizing.survey_soon()  # the Optimizer page's counts, in the background (reads new files' tags)
            if not self.libdata.started:
                self.libdata.start()  # every library's records and files gathered in the background, the ones opened lately first
            result = checks.run_all(self)
            with self.lock:
                self.health = result
            return result
        finally:
            with self.lock:
                self.checking = False

    def publish_peers(self) -> None:
        peers.publish(self)
        self.peers_summary = peers.summary(self)

    def note_tool_run(self, library: str, tool: str) -> None:
        """A tool ran on a library here: told to the other Plex Cache in the folder's marker."""
        self.tool_runs.setdefault(str(library), {})[tool] = time.time()
        self._save_json("tool-runs.json", self.tool_runs)
        threading.Thread(target=self.publish_peers, name="peers", daemon=True).start()

    def update_now(self) -> dict:
        """The newer image pulled and the app redeployed, through the TrueNAS API (Update Now)."""
        if not self.updates.available():
            raise ValueError(M("No update is available."))
        name = None
        try:
            name = (self.optimizing._containers() or {}).get("app")
        except Exception:  # noqa: BLE001
            name = None
        if not name:
            raise ValueError(M("Plex Cache can’t update itself without the TrueNAS API ([System → TrueNAS](/system#truenas), with a key allowed to manage apps). Update it in TrueNAS: Apps → plex-cache → Update."))
        try:
            with self.truenas() as tn:
                try:
                    tn.call("app.pull_images", [name, {"redeploy": True}])
                except TrueNASError:
                    tn.call("app.redeploy", [name])  # (an older TrueNAS: a redeploy pulls the tag anew)
        except TrueNASError as e:
            raise ValueError(M("TrueNAS didn’t update {app}: {why}", app=name, why=str(e))) from None
        self.updates.updating = time.time()
        self.activity.event("update", M("Update asked of TrueNAS: {app} is pulling the newer image and restarting", app=name))
        return {"updating": True, "app": name}

    def run_checks_async(self, rescan: bool = False):
        def work():
            if rescan:
                self.scan_library()
            self.run_checks()
        threading.Thread(target=work, daemon=True).start()

    # --------------------------------------------------------------- warm-up
    def warmup_schedules(self) -> dict:
        """Each library's warm-up schedule right now (Default resolved; none without an L2ARC)."""
        w = self.settings.section("warmup")
        out = {}
        for lib in self.libraries:
            if not lib.get("kind") or lib.get("excluded"):
                continue
            mine = (w.get("libraries") or {}).get(lib["key"]) or {}
            out[lib["key"]] = resolve_schedule(self, lib["key"], mine.get("schedule", "default"), lib["title"])
        return out

    def warmup_active(self) -> bool:
        """Does any library have a warm-up schedule right now?"""
        return any(v != "none" for v in self.warmup_schedules().values())

    def warmup_libraries(self, schedule: str) -> set:
        """Libraries whose warm-up schedule is ``schedule``."""
        return {k for k, v in self.warmup_schedules().items() if v == schedule}

    def next_warmup(self, now: dt.datetime | None = None) -> dt.datetime | None:
        """When the next timed warm-up starts, for any library."""
        w = self.settings.section("warmup")
        now = now or dt.datetime.now()
        used = set(self.warmup_schedules().values())
        times = [next_run(w, now, None, sched) for sched in TIMED if sched in used]
        return min((t for t in times if t), default=None)

    def _share_out_old_choices(self) -> None:
        """Choices kept for every library ("*"): warm-up's one schedule before 0.15, and
        Prefetch's switch turned off. Once Plex's libraries are known, each library without
        its own choices gets them."""
        for sec in ("warmup", "nextepisode"):
            libs = dict(self.settings.section(sec).get("libraries") or {})
            shared = libs.pop("*", None)
            if shared is None:
                continue
            for lib in self.libraries:
                if lib.get("kind") and (sec == "warmup" or prefetch.library_kind(lib)):
                    libs[lib["key"]] = dict(shared, **(libs.get(lib["key"]) or {}))
            self.settings.update({sec: {"libraries": libs}})

    # ------------------------------------------------------------- cache log
    def l2arc_fill_rate(self) -> int | None:
        """How fast ZFS copies data to the L2ARC, in bytes a second (l2arc_write_max), if known."""
        wm = ((self.zfs or {}).get("params") or {}).get("l2arc_write_max")
        return int(wm) if wm and str(wm).isdigit() and int(wm) > 0 else None

    def l2arc_rate(self) -> float | None:
        """The speed L2ARC-only reads from the disks are held to, in bytes a second: just under
        the fill rate with automatic pacing (None: pace by share of disk time instead)."""
        fill = self.l2arc_fill_rate()
        if not self.settings.section("warmup")["pacing_auto"] or not fill:
            return None
        return fill * L2ARC_RATE_SHARE

    def cache_route(self, ram: bool) -> str:
        """The cache log's route for a read: "ram" if it's meant to stay in RAM, else "ssd"."""
        return "ram" if ram else "ssd"

    def cooldown_seconds(self, whole: bool, library: str = "") -> float:
        """Seconds before a lead-in (or a whole file) is cached again: the L2ARC setting when
        there's an L2ARC (what's read is taken to be on the SSD), else the RAM one. "Automatic"
        works it out per library from what's been measured (cachelog), with the setting's
        default until there's enough."""
        g = self.settings.section("general")
        l2 = self.cache_mode() == cachemode.L2ARC
        key = ("l2arc_" if l2 else "ram_") + ("cooldown_whole_minutes" if whole else "cooldown_lead_in_minutes")
        if g[key] == config.AUTO:
            return self.cachelog.auto_cooldown("ssd" if l2 else "ram", library, config.DEFAULTS["general"][key] * 60)
        return g[key] * 60

    def cooldowns(self) -> tuple[float, float]:
        """Hours before a lead-in, and a whole file, are cached again (for every library)."""
        return self.cooldown_seconds(False) / 3600, self.cooldown_seconds(True) / 3600

    def recently_cached(self, path: str, whole: bool) -> bool:
        return self.cachelog.recent(path, whole, self.cooldown_seconds(whole, self.cachelog.library_of(path)) / 3600)

    def record_coverage(self, force: bool = False) -> None:
        """Once an hour (after the first library scan): how much of each library was cached."""
        if not self.files or not (force or self.coverage.due()):
            return
        files = [(self.map_path(f.path), f.size, f.library_key) for f in self.files]
        self.coverage.record(files, self.cachelog.latest_writes())

    def diagnostics(self) -> dict:
        """For "Export log": the cache log, plus what's needed to see why something isn't cached —
        the streams Plex reported last, recent events, cooldowns and each library's state.
        No secrets (tokens, keys, passwords) are included."""
        g = self.settings.section("general")
        mon = getattr(self, "monitor", None)
        return dict(self.cachelog.export(),
                    version=self.version, cache_mode=self.cache_mode(),
                    cooldowns={k: v for k, v in g.items() if "cooldown" in k},
                    libraries=[{"key": lib["key"], "title": lib.get("title"), "kind": lib.get("kind"), "excluded": lib.get("excluded"),
                                "cacheable": self.library_cacheable(lib["key"]), "l2arc": self.library_l2arc(lib["key"])}
                               for lib in self.libraries],
                    path_map=g.get("path_map"),
                    plex_sessions=getattr(mon, "last_sessions", None),
                    playing=mon.snapshot() if mon else None,
                    events=self.activity.snapshot()["events"])

    def _refresh_auto(self) -> None:
        """Let Automatic decide again if it's due, so the page shows what it would use now."""
        for route, key in (("ram", "ram_cooldown_lead_in_minutes"), ("ssd", "l2arc_cooldown_lead_in_minutes")):
            self.cachelog.auto_cooldown(route, "", config.DEFAULTS["general"][key] * 60)

    def cached_chance(self, path: str, whole: bool) -> float:
        """How likely ``path``'s lead-in (or whole file) was still cached just now: from how long
        ago it was cached and how long cached data lasts in its library (cachelog). Without
        enough measured yet at that age: 1 within the last hour (or its cooldown, if longer), else 0.
        (Not "within the cooldown" alone: with no cooldown, nothing would ever count as cached.)"""
        t, route, lib = self.cachelog.written(path, whole)
        if not t:
            return 0.0
        age = time.time() - t
        p = self.cachelog.still_cached(route, lib, age)
        if p is None:
            return 1.0 if age < max(3600, self.cooldown_seconds(whole, lib)) else 0.0
        return p

    def prefetch_active(self) -> bool:
        """Does any library prefetch anything? (Prefetch has no switch: each library's choices say.)"""
        cfg = self.settings.section("nextepisode")
        libs = [lib for lib in self.libraries if prefetch.library_kind(lib) and not lib.get("excluded")]
        if not libs:
            return True  # not scanned yet: the defaults prefetch
        for lib in libs:
            mine = prefetch.library_settings(cfg, lib["key"])
            if mine["start"] != "off" or mine["end"] != "none":
                return True
        return False

    def check_next_day(self) -> None:
        """A day after each warm-up, check whether a sample of its lead-ins is still on the SSD,
        which says how long the L2ARC really keeps them here (the cooldown and the warm-up
        schedule can be set by it). Waits while a warm-up is running, so it measures the SSD."""
        if self.warmup_run is not None or getattr(self, "_next_day_busy", False):
            return
        due = self.activity.runs_due(time.time(), NEXT_DAY)
        if not due:
            return
        self._next_day_busy = True
        run = due[0]

        def work():
            try:
                r = next_day_check(run["sample"])
                self.activity.update_run(run["started"], sample=None, next_day=r["pct"], next_day_checked=r["checked"])
                if r["pct"] is not None:
                    when = dt.datetime.fromtimestamp(run["started"]).strftime("%Y-%m-%d %H:%M")
                    self.activity.event("warm-up", M("a day after the warm-up of {when}: {pct}% of a sample of its lead-ins still on the SSD",
                                                     when=when, pct=r["pct"]))
            except Exception:  # never let it stop the scheduler
                log.exception("next-day check failed")
                self.activity.update_run(run["started"], sample=None)
            finally:
                self._next_day_busy = False
        threading.Thread(target=work, name="next-day", daemon=True).start()

    def start_warmup(self, manual: bool, libs: set | None = None, why=None) -> bool:
        """Warm up ``libs`` (library keys). Run now (``libs`` None) warms every library that
        has a warm-up schedule, or all of them if none has."""
        with self.lock:
            if self.warmup_run is not None:
                return False
            self.warmup_stop = threading.Event()
            self.warmup_run = WarmupRun([], {k: 0 for k in KINDS}, 100, 150, self.warmup_stop)  # placeholder while scanning
            self.warmup_run.result = "scanning"
        threading.Thread(target=self._warmup_worker, args=(manual, libs, why), name="warmup", daemon=True).start()
        return True

    def stop_warmup(self):
        self.warmup_stop.set()

    def _warmup_worker(self, manual: bool, libs: set | None = None, why=None):
        started = dt.datetime.now()
        try:
            self.activity.event("warm-up", M("started (run now, full speed)") if manual
                                else M("started ({why})", why=why) if why else M("started"))
            self.scan_library()
            if self.scan_error:
                self.activity.event("warm-up", M("stopped: couldn’t read libraries: {why}", why=self.scan_error), "warn")
                return
            self.probe_zfs()
            s = self.settings.get()
            w, g = s["warmup"], s["general"]
            if libs is None:
                libs = {k for k, v in self.warmup_schedules().items() if v != "none"} or None
            records = self.library_records()
            p = self.lead_in_plan(w, libs, records)
            if p["status"] == "none":
                self.activity.event("warm-up", M("skipped: no L2ARC on the media pool"), "warn")
                return
            if p["status"] in ("reduced", "insufficient"):
                secs = [M("{kind} {n} s", kind=r["name"], n=r["sec"]) for r in p["libraries"]]
                self.activity.event("warm-up", M("L2ARC too small for your settings, using {list}", list=secs), "warn")
            files = [f.__class__(self.map_path(f.path), f.kbps, f.size, f.kind, f.library, f.library_key)
                     for f in self.files if self.library_cacheable(f.library_key, "warmup")
                     and (libs is None or f.library_key in libs)]
            pauses = L2arcPauses(self, manual)
            holder: dict = {}

            def pause():
                why = pauses.reason()
                run = holder.get("run")
                if run is not None and why != run.paused:
                    self.activity.event("warm-up", M("paused: {why}", why=why) if why else M("carrying on"))
                    run.paused = why
                return why is not None

            speeds = {f.library_key: self.disk_speed_for_library(f.library_key) for f in files}
            # Lead-ins cached within their cooldown are skipped — but only those cached since the last
            # warm-up started, so a timed warm-up re-checks its own (rewriting what the L2ARC has overwritten since) ("Run now" reads everything)
            since = self.last_warmup.timestamp() if self.last_warmup else 0
            skip = None if manual else (lambda f: self.cachelog.last(f.path, False) > since
                                        and self.recently_cached(f.path, whole=False))
            run = WarmupRun(files, p["seconds"], 100 if manual else w["pacing_percent"],
                            lambda f: speeds.get(f.library_key) or g["hdd_mb_per_sec"],
                            self.warmup_stop, pause, zfs.arcstats, skip=skip,
                            note=lambda f, hit: self.cachelog.note(f.path, whole=False, route="ssd", hit=hit,
                                                                   library=f.library_key),
                            rate=None if manual else self.l2arc_rate(),
                            record=lambda f: (records.get(f.library_key) or {}).get("rs"))
            run.pacer.before_wait = pauses.mark_idle
            holder["run"] = run
            with self.lock:
                self.warmup_run = run
            self.activity.event("warm-up", M("reading the lead-ins of {n} files at {pct}% pacing", n=len(files), pct=run.pacer.percent))
            result = run.run()
            result["manual"] = manual
            self.activity.add_run(result)
            hr = result["hit_rate"]
            growth = result["l2_growth"]
            outcome = {"ok": M("ok"), "stopped": M("stopped"), "errors": M("errors")}.get(result["result"], result["result"])
            bits = [M("{outcome}: {n} files, {size} read", outcome=outcome, n=result["files"], size=size_text(result["bytes"]))]
            if result.get("skipped"):
                bits.append(M("{n} skipped (cached since the last warm-up, within the cooldown)", n=result["skipped"]))
            if hr is not None:
                bits.append(M("{pct}% already cached", pct=round(hr * 100)))
            if growth is not None:
                bits.append(M("L2ARC {sign}{size}", sign="+" if growth >= 0 else "−", size=size_text(abs(growth))))
            if result["errors"]:
                bits.append(M("{n} unreadable", n=result["errors"]))
            self.activity.event("warm-up", M("{list}", list=bits), "warn" if result["errors"] else "info")
            if result.get("index"):
                self.activity.event("warm-up", index_summary(result))
        except Exception as e:  # keep the app alive
            log.exception("warm-up failed")
            self.activity.event("warm-up", M("failed: {why}", why=msg(e)), "warn")
        finally:
            self.last_warmup = started
            with self.lock:
                self.warmup_run = None

    def reports(self) -> dict:
        """The Activity report for each period. A year of history takes a moment to add up,
        so it's reused for a few seconds unless something new was measured."""
        key = (len(self.activity.metrics), len(self.activity.daily))
        cached = getattr(self, "_reports", None)
        if cached and cached[0] == key and time.time() - cached[1] < 15:
            return cached[2]
        out = report.build_all(self)
        self._reports = (key, time.time(), out)
        return out

    def index_avg(self) -> dict | None:
        """Average bytes per file at the ends of files (index etc.), per kind, from the last run that measured it."""
        for run in reversed(self.activity.runs):
            idx = run.get("index")
            if idx:
                kinds = run.get("by_kind") or {}
                return {k: v["bytes"] / kinds[k]["files"] for k, v in idx.items()
                        if kinds.get(k, {}).get("files")}
        return None

    def index_layout(self) -> dict | None:
        """Per kind, from the last run that measured it: the share of files keeping their index
        (and the like) at the end, and the average bytes there for those files."""
        for run in reversed(self.activity.runs):
            idx = run.get("index")
            if idx:
                kinds = run.get("by_kind") or {}
                out = {k: {"share": v["files"] / kinds[k]["files"], "avg": v["bytes"] / v["files"] if v["files"] else 0}
                       for k, v in idx.items() if kinds.get(k, {}).get("files")}
                for k, v in idx.items():  # metadata in front of the video (runs from before this didn't measure it)
                    if k in out and v.get("front_files"):
                        out[k]["front"] = v["front"] / v["front_files"]
                return out
        return None

    def lead_in_plan(self, w: dict | None = None, libs: set | None = None, records: dict | None = None) -> dict:
        """The Lead-in Cache's plan (warmup.plan), sized at each library's dataset record size."""
        return plan(self.stats, w or self.settings.get()["warmup"], self.l2_total(), self.index_avg(), libs,
                    records=self.library_records() if records is None else records, index_layout=self.index_layout())

    # ---------------------------------------------------------------- Library Health
    def _health_files(self, key: str) -> list:
        from .paging import sort_key
        return sorted((f for f in list(self.files) if str(f.library_key) == str(key)),
                      key=lambda f: (sort_key(getattr(f, "title", "") or os.path.basename(f.path)), f.path))

    def library_view(self, key: str, offset: int = 0, limit: int = 100, q: str = "") -> dict:
        """A library's files for the Library Health page, by title: each with its layout (as last read;
        None until the library is surveyed) and what caching takes from it."""
        from . import filechecks
        from .filemap import block_map, cache_plan
        from .warmup import library_seconds
        files = self._health_files(key)
        lrec = checks.library_record(self, key)
        if q:
            ql = q.lower()
            files = [f for f in files if ql in (getattr(f, "title", "") or "").lower() or ql in os.path.basename(f.path).lower()]
        paths = [self.map_path(f.path) for f in files]
        w = self.settings.get()["warmup"]
        record = self.record_for(key)
        rows = []
        for f, path in list(zip(files, paths))[offset:offset + limit]:
            try:
                st = os.stat(path)
                m = self.file_maps.get(path, st)
            except OSError:
                st, m = None, None
            kind = getattr(f, "kind", "movie")
            secs = library_seconds(w, key, kind if kind in ("movie", "tv", "audio") else "movie")
            rec = file_block(m, f.size, record)  # the blocks the file is really stored in
            plan = cache_plan(m, f.size, f.kbps, secs, rec, kind)
            fc = filechecks.for_file(self, f, path, m, key, lrec)
            rows.append({"file": path, "label": getattr(f, "title", "") or os.path.basename(path), "poster": getattr(f, "poster", ""),
                         "checks": fc, "health": filechecks.worst(fc),
                         "size": f.size, "kbps": f.kbps, "kind": kind, "missing": st is None, "read": bool(m),
                         "saved": f.size - m["disk"] if m and m.get("disk") is not None else None,
                         "plan": plan, "blocks": block_map(m, f.size, plan, rec)})
        from .paging import page_ranges
        return {"library": key, "total": len(files), "surveyed": self.file_maps.current(paths), "record": record,
                "offset": offset, "files": rows, "summary": self._health_summary(key, lrec),
                "pages": page_ranges([getattr(f, "title", "") or os.path.basename(f.path) for f in files], limit)}

    def _health_summary(self, key: str, lrec: dict) -> dict:
        """Every inspected file of the library through its checks (filechecks.summarize), kept a little
        while: the page asks every few seconds."""
        from . import filechecks, projection
        memo = self.__dict__.setdefault("_summaries", {})
        files = self._health_files(key)
        sig = (len(files), len(self.file_maps.files), self.settings.rev)  # (a change of settings: worked out again)
        got = memo.get(key)
        if got and got[0] == sig and time.time() - got[1] < 30:
            return got[2]
        from .filemap import MIX_KEYS
        results = []
        mix = dict.fromkeys(MIX_KEYS, 0)
        mix["files"] = 0
        posters: list = []  # Plex's posters whose sizes aren't known yet
        # the charts' rows: every file now, and once optimized (est.) — the files still to optimize as
        # the optimizer would leave them, the rest as they are (the Metrics page has the optimized ones,
        # first seen against now)
        sv = dict.fromkeys(("all", "all_after"))
        # the files the optimizer would rewrite: what caching keeps of them now, and would after (projection.py)
        proj = {"files": 0, "now": dict.fromkeys(MIX_KEYS + ("blocks",), 0), "after": dict.fromkeys(MIX_KEYS + ("blocks",), 0),
                "removed": {}, "removed_disk": 0}
        for f in files:
            path = self.map_path(f.path)
            try:
                m = self.file_maps.get(path, os.stat(path))
            except OSError:
                continue
            ctx = filechecks.context(self, path, m, key, lrec)
            results.append(filechecks.check_file(self, f, path, m, lrec, *ctx))
            if not m or not m.get("mix"):
                continue
            got = projection.existing(m["mix"], self.compression_of(path))  # (as it's stored now: its compression)
            pr = self._projection(f, m, lrec, ctx, missing=posters)
            t = projection.tail_video(m)
            kinds = {k: v[0] for k, v in (got.get("kinds") or {}).items()}
            if pr:  # (now: what optimizing would take out shown as such)
                sv["all"] = projection.add_view(sv["all"], projection.view(got, kinds, t, pr["removed"], pr["removed_disk"]))
                sv["all_after"] = projection.add_view(sv["all_after"], projection.view(pr["mix"], pr["kinds"], pr["mix"]["video_tail"],
                                                                                      pr["removed"], pr["removed_disk"], gone=True))
            else:
                now_row = projection.view(got, kinds, t)
                sv["all"] = projection.add_view(sv["all"], now_row)
                sv["all_after"] = projection.add_view(sv["all_after"], dict(now_row, gone=True))
            if pr:
                proj["files"] += 1
                proj["rewrite"] = proj.get("rewrite", 0) + bool(pr.get("rewrite"))
                for k in MIX_KEYS + ("blocks",):
                    proj["now"][k] += got.get(k, 0)
                    proj["after"][k] += pr["mix"].get(k, 0)
                for k, n in pr["removed"].items():
                    proj["removed"][k] = proj["removed"].get(k, 0) + n
                proj["removed_disk"] += pr["removed_disk"]
            mix["files"] += 1
            for k in MIX_KEYS:
                mix[k] += got.get(k, 0)  # (a mix kept before a key was added — round_disk — hasn't it yet)
        if posters:
            self.optimizing.poster_sizes_soon(posters)  # (learnt in the background: the estimate then has them)
        out = filechecks.summarize(results)
        out["mix"] = mix if mix["files"] else None
        out["projected"] = proj if proj["files"] else None
        # once optimized: only while some file would gain from it (a library optimized already shows it as it is)
        out["views"] = {"all": sv["all"], "after": sv["all_after"] if proj["files"] else None,
                        "rewrite": bool(proj["files"]) and proj.get("rewrite", 0) == proj["files"]}  # (only written again, every one)
        memo[key] = (sig, time.time(), out)
        return out

    def _projection(self, f, m: dict | None, lrec: dict, ctx: tuple, fetch: bool = False, missing: list | None = None) -> dict | None:
        """What optimizing would make of the file (projection.project), if the optimizer would write
        it: an MKV laid out again (its library set to optimize them, not optimized by this version
        yet), or any file written again as it is, to take its dataset's compression and record size
        (TrueNAS compression & record size on, and the file not in them: "rewrite" in the result).
        Either way it's written with the dataset's compression."""
        from . import projection, zfslog
        from .optimize import compare as version_of
        from .optimize.optimizer import strips_tags
        opts, info, zstate = ctx
        if not m:
            return None
        comp = projection.family(lrec.get("compression"))
        ours = (info or {}).get("ours") or {}
        remux = (m.get("format") == "mkv" and opts.get("mkv") and info is not None and not (
            version_of(ours.get("PLEXCACHE_OPTIMIZER")) == "same" and not info.get("problems") and not strips_tags(opts["tags"], info)))
        if not remux:
            rs = lrec.get("rs")
            blocks = bool(rs) and (m.get("size") or 0) > (m.get("blksize") or 0) and m.get("blksize") and m["blksize"] != rs
            if not opts.get("sync") or not (zstate in zfslog.NEEDS_WORK or blocks):
                return None  # done already: nothing to gain
            record = rs or (m.get("mix") or {}).get("record")
            if not record:
                return None  # (no record size known: nothing to project)
            return projection.project(m, record, "keep", False, comp=comp, rewrite=True,
                                      comp_now=self.compression_of(self.map_path(f.path)))
        poster = getattr(f, "poster", "")
        images = self.optimizing.images_mode(opts["images"], poster, getattr(f, "matched", None), ours)
        record = (lrec.get("rs") if opts.get("sync") else None) or (m.get("mix") or {}).get("record")
        if not record:
            return None  # (no record size known: nothing to project)
        cover = None
        if images == "plex":  # (Plex's poster in place of its cover art: its size, as Plex has it)
            cover = self.optimizing.poster_size(poster, fetch=fetch)
            if cover is None and missing is not None:
                missing.append(poster)
        return projection.project(m, record, images, strips_tags(opts["tags"], info or {}), cover, comp=comp,
                                  comp_now=self.compression_of(self.map_path(f.path)))

    def file_view(self, path: str) -> dict | None:
        """One file in full for the Library Health page: its layout (read now if it changed), tracks,
        attachments, and what caching takes from it. None if it isn't a library file."""
        from . import filemap
        from .filemap import block_map, cache_plan
        from .warmup import library_seconds
        f = self._library_file(path)
        if f is None:
            return None
        from . import filechecks
        m = self.file_maps.read(path)
        self.file_maps.save()
        key, kind = str(f.library_key), getattr(f, "kind", "movie")
        record = self.record_for(key)
        lrec = checks.library_record(self, key)
        ctx = filechecks.context(self, path, m, key, lrec)
        from . import projection
        pr = self._projection(f, m, lrec, ctx, fetch=True)
        rec = file_block(m, f.size, record)  # the blocks the file is really stored in
        secs = library_seconds(self.settings.get()["warmup"], key, kind if kind in ("movie", "tv", "audio") else "movie")
        plan = cache_plan(m, f.size, f.kbps, secs, rec, kind)
        return {"file": path, "label": getattr(f, "title", "") or os.path.basename(path), "poster": getattr(f, "poster", ""),
                "library": key, "size": f.size, "kbps": f.kbps, "kind": kind,
                "tracks": [dict(t, codec_name=filemap.codec_name(t.get("codec")), language_name=language_name(t.get("language"))) for t in m.get("tracks") or []],
                "format": m.get("format"), "disk": m.get("disk"), "saved": f.size - m["disk"] if m.get("disk") is not None else None,
                "video_start": m.get("video_start"), "video_end": m.get("video_end"), "record": rec, "dataset_record": record,
                "plan": plan, "blocks": block_map(m, f.size, plan, rec), "error": m.get("error"),
                "checks": filechecks.check_file(self, f, path, m, lrec, *ctx),
                "attachments": m.get("attachments") or [], "optimized": self._optimized_version(path),
                "mix": m.get("mix"), "projected": pr, "views": projection.views(m, pr, self._original(path, key, m), self.compression_of(path)) if m.get("mix") else None}

    def compression_of(self, path: str) -> str:
        """The compression family a file is stored with (projection.family): as the ZFS record knows
        it (written by the optimizer, or assumed from its dataset), or "lz4" — TrueNAS's default — when
        that isn't known."""
        from .projection import family
        z = getattr(getattr(self, "optimizing", None), "zfs", None)
        e = (getattr(z, "files", None) or {}).get(path) or {}
        try:
            same = e.get("ino") == os.stat(path).st_ino
        except OSError:
            same = False
        return family(e.get("compression") if same else None)

    def _original(self, path: str, key: str, m: dict) -> dict | None:
        """The file when it was first seen, before the optimizer touched it (filehistory.py), as a chart
        row; None if it hasn't changed since, or isn't known."""
        from .filehistory import row
        self.observe_history(path, key, m)
        return row(self.history.original(path))

    def history_metrics(self) -> list[dict]:
        """For the Metrics page, per library: its optimized files' average per file, first seen and now,
        and how many files were optimized elsewhere before this app saw them (left out). Kept until the
        history changes (the page asks every few seconds)."""
        libs = [(str(lib["key"]), lib.get("title") or "") for lib in list(self.libraries)]
        sig = (self.history.rev, tuple(libs))
        memo = getattr(self, "_history_memo", None)
        if memo and memo[0] == sig:
            return memo[1]
        out = [dict(self.history.library(k), key=k, title=t) for k, t in libs]
        out = [x for x in out if x["files"] or x["unknown"]]
        for x in out:
            x.pop("total", None)
        self._history_memo = (sig, out)
        return out

    def observe_history(self, path: str, key: str, m: dict | None = None, st: os.stat_result | None = None) -> str | None:
        """The file as it is now, in its history with the optimizer (filehistory.observe): its layout
        (``m``, or as last inspected if it's still the same file) and our tags in it."""
        try:
            st = st or os.stat(path)
        except OSError:
            return None
        if m is None:
            m = self.file_maps.get(path, st, full=True)
        if m is None:
            return None  # not inspected as it is now: next time
        ours = {}
        if m.get("format") == "mkv":
            try:
                ours = self.optimizing._mkv_info(path, st).get("ours") or {}
            except Exception:  # noqa: BLE001 — unreadable tags: taken as none
                ours = {}
        return self.history.observe(path, str(key), m, [st.st_size, st.st_mtime_ns, st.st_ino], ours, comp=self.compression_of(path))

    def history_pass(self, key: str, everything: set | None = None) -> None:
        """A library's files, each as it is now, in their history (after an inspection); ``everything``:
        every library's files (the ones not among them are marked gone)."""
        for f in [x for x in list(self.files) if str(x.library_key) == str(key)]:
            self.observe_history(self.map_path(f.path), key)
        if everything:
            self.history.missing(everything)
        self.history.save()

    def _optimized_version(self, path: str) -> str | None:
        """The optimizer version that wrote this MKV (its tag), or None."""
        if not path.lower().endswith((".mkv", ".mk3d", ".mka", ".mks")):
            return None
        try:
            return (self.optimizing._mkv_info(path, os.stat(path)).get("ours") or {}).get("PLEXCACHE_OPTIMIZER")
        except Exception:  # noqa: BLE001 — unreadable tags: shown as not optimized
            return None

    def _library_file(self, path: str):
        return next((x for x in list(self.files) if self.map_path(x.path) == path), None)

    def block_estimates(self, path: str, numbers: list[int]) -> list[dict] | None:
        """How ZFS might store some of a file's blocks (filemap.estimate_blocks), or None if it isn't a library file."""
        from .filemap import estimate_blocks
        f = self._library_file(path)
        if f is None:
            return None
        st = os.stat(path)
        rec = (st.st_blksize if st.st_size > st.st_blksize else None) or self.record_for(str(f.library_key)) or 1024 * 1024
        return estimate_blocks(path, numbers, rec, st.st_size)

    def record_for(self, library_key: str) -> int | None:
        """The record size of a library's dataset, as checked (None if it couldn't be)."""
        return (self.library_records().get(str(library_key)) or {}).get("rs")

    def library_records(self) -> dict:
        """Library key -> {"rs": its dataset's record size, "blocks": the block sizes a sample of
        the dataset's files were written with, "dataset"}. The record size comes from TrueNAS,
        or failing that from the dataset itself: ZFS reports it as the filesystem's block size.
        A dataset that can't be checked either way has none — it's never guessed."""
        out = {}
        with self.lock:
            datasets = list((self.zfs or {}).get("datasets") or [])
        for d in datasets:
            rs = (d.get("props") or {}).get("recordsize")
            if not rs and d.get("fstype") == "zfs":
                rs = zfs.recordsize(d.get("mountpoint"))
            for key in (d.get("keys") or []) + (d.get("off_keys") or []):
                out[str(key)] = {"rs": rs or None, "blocks": d.get("blocks"), "dataset": d.get("dataset") or d.get("mountpoint"),
                                 "compression": (d.get("props") or {}).get("compression")}
        return out

    # ------------------------------------------------------------- benchmark
    def _load_json(self, name: str):
        try:
            with open(os.path.join(self.config_dir, name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _save_json(self, name: str, data) -> None:
        path = os.path.join(self.config_dir, name)
        tmp = f"{path}.{threading.get_ident()}.tmp"  # each thread its own: two saves at once can't mix
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("couldn't save %s: %s", name, e)

    def run_benchmark(self):
        """Time reads of a few media files on each dataset to learn its uncached speed.

        The result tells a cache hit (much faster) from a disk read, so warm-up
        can report what was already cached, and paces background reads."""
        with self.lock:
            if self.benchmarking:
                return
            self.benchmarking = True

        def work():
            rows, results = [], {}
            try:
                if not self.zfs.get("datasets"):
                    self.run_checks()
                now = dt.datetime.now().isoformat(timespec="seconds")
                todo = [d for d in (self.zfs or {}).get("datasets", []) if d["keys"]]
                finished: list[str] = []
                # One dataset at a time (reading two at once would slow both and skew the result),
                # and each one's speed is saved as soon as it's measured, so the page fills in row by row.
                for i, d in enumerate(todo):
                    name = d["dataset"] or d["mountpoint"]
                    self.benchmark_progress = {"dataset": name, "done": i, "total": len(todo), "finished": list(finished)}
                    mine = [f for f in self.files if f.library_key in d["keys"]]
                    pool = [f for f in mine if f.size >= 64 * MiB] or [f for f in mine if f.size >= MiB]
                    got = []
                    for f in random.sample(pool, min(5, len(pool))):
                        path = self.map_path(f.path)
                        res = benchmark_file(path, f.size)
                        if not res.error and res.bytes:
                            got.append(round(res.mb_per_sec))
                            rows.append({"dataset": name, "kind": f.kind, "file": os.path.basename(path),
                                         "mb_s": round(res.mb_per_sec), "mb": round(res.bytes / MiB)})
                    if got:
                        # The median, so one file that happened to be cached doesn't skew it.
                        median = sorted(got)[len(got) // 2]
                        speeds = dict(self.settings.section("general")["disk_speeds"])
                        speeds[name] = {"mb_s": min(20000, max(10, median)), "measured_at": now}
                        self.settings.update({"general": {"disk_speeds": speeds}})
                        results[name] = median
                    finished.append(name)
                if results:
                    self.settings.update({"general": {"hdd_measured_at": now}})
                    self.activity.event("benchmark", M("measured {list}", list=[M("{dataset} {n} MB/s", dataset=k, n=v)
                                                                                  for k, v in results.items()]))
                    self.benchmark = {"rows": rows, "results": results, "at": time.time()}
                else:
                    self.benchmark = {"rows": [], "results": {}, "at": time.time(),
                                      "error": M("No readable media files to benchmark.")}
            finally:
                with self.lock:
                    self.benchmarking = False
                    self.benchmark_progress = None
            self.run_checks()

        self.benchmark_thread = threading.Thread(target=work, daemon=True)
        self.benchmark_thread.start()

    # ---------------------------------------------------------------- state
    def _optimizer_state(self) -> dict:
        snap = self.optimizing.snapshot()
        if not snap["setup"].get("ready"):  # (answering or not: until every step passes — never once it has)
            self.optimizing.diagnose_soon()  # what's left to set up, checked again now and then
        return snap

    def state(self) -> dict:
        s = self.settings.get()
        now = dt.datetime.now()
        nxt = self.next_warmup(now)
        run = self.warmup_run
        with self.lock:
            zinfo = dict(self.zfs)
            stats = self.stats.to_dict()
            libraries = list(self.libraries)
        out = {
            "version": self.version,
            "settings": self.settings.public(),
            "auth": {"enabled": self.auth.enabled, "user": self.auth.username if self.auth.enabled else ""},
            # Defaults of every setting (but secrets), for the page's ↺ "back to default" buttons
            "defaults": config.public_defaults(),
            "health": self.health,
            "checking": self.checking,
            # the first checks haven't finished: ZFS, TrueNAS and the platform aren't known yet — the page says
            # Checking… rather than a verdict (tests/render.js renders every page in that state to make sure)
            "pending": not self.zfs,
            "connections": self.connections,
            "testing": self.testing,
            "host": self.host,
            "web_url": self.web_url,
            "locations": self.locations,
            "mapping_choices": self.mapping_choices,
            "l2arc": self.l2arc_status(),
            "cache": cachemode.describe(self.cache_mode(), self.l2arc_status(), bool(s["general"]["no_l2arc"]), self.fallbacks),
            "ram": self.ram_budget(),
            "cachelog": self.cachelog.counts(*self.cooldowns()),
            # how long cached data lasts here (RAM, SSD), and per library
            "survival": self._refresh_auto() or self.cachelog.survival({lib["key"]: lib.get("title") or "" for lib in self.libraries},
                                               {lib["key"]: self.library_cacheable(lib["key"]) for lib in self.libraries},
                                               {lib["key"]: self.library_l2arc(lib["key"]) for lib in self.libraries}),
            # each library's optimized files: what caching kept of them first seen, and now (filehistory.py)
            "optimized_history": self.history_metrics(),
            "kind_seconds": KIND_SECONDS,  # each library's default lead-in length, by kind
            "l2arc_fill": self.l2arc_fill_rate(),  # bytes a second ZFS copies to the L2ARC (l2arc_write_max)
            "l2arc_rate": self.l2arc_rate(),  # what automatic L2ARC pacing holds reads to
            "prefetch_active": self.prefetch_active(),
            "prefetch_kind_defaults": prefetch.KIND_DEFAULTS,  # kinds whose Default differs (music: no whole files)
            # Prefetch's L2ARC-only queue: waiting, reading now, and why it's paused (if it is)
            "l2arc_queue": self.monitor.background_queue(),
            "library": {"stats": stats, "error": self.scan_error, "scanning": self.scanning,
                        # tv / music / books for the libraries Prefetch covers
                        "libraries": [dict(lib, prefetch_kind=prefetch.library_kind(lib)) for lib in libraries]},
            "platform": zinfo.get("platform") or platform_mod.detect(None),  # TrueNAS, or another Linux host (platform.py)
            "zfs": {
                "arc": zinfo.get("arc"),
                "pools": zinfo.get("pools", {}),
                "datasets": [{k: v for k, v in d.items() if k != "props_debug"} for d in zinfo.get("datasets", [])],
                "l2_total": self.l2_total(),
            },
            # Sizing assumes every library's lead-ins are cached (libraries without a warm-up need less)
            "plan": self.lead_in_plan(s["warmup"]),
            "index_avg": self.index_avg(),
            "warmup": {
                "running": run is not None,
                "progress": run.progress() if run else None,
                "phase": run.result if run else None,
                "next_run": nxt.isoformat(timespec="minutes") if nxt else None,
                "last_run": self.last_warmup.isoformat(timespec="minutes") if self.last_warmup else None,
                "schedules": self.warmup_schedules(),  # library key -> its schedule now (Default resolved)
            },
            "benchmark": {"running": self.benchmarking, "last": self.benchmark, "progress": self.benchmark_progress},
            # The optimizer: per library how many files are optimized, the job, and the service itself
            "optimizer": dict(self._optimizer_state(), service=self.optimizing.client.status()),
            "migration": dict(self.migration.snapshot(), candidates=self.migration.candidates(), mirror=self.migration.mirror_view()),
            "conversions": self.conversions.snapshot(),
            "plex2": self.mirrors.snapshot(),
            "sync": self.sync.snapshot(),
            "tools": {name: t.snapshot() for name, t in self.tools.items()},  # the Tools pages: each one's last scan, run, results
            "plex_backup": plexbackup.plan(self),  # what's done before Plex's records change: the backup of them, or none (plexbackup.py)
            # every page of the Tools menu's count, per library and in all — done (a number), pending, na or failed: the only
            # numbers the pages show for them, each evaluated before it's shown (evaluate.py; app.js countChip)
            "counts": self.evaluator.counts(),
            "evaluating": self.evaluator.snapshot(),  # what's being worked out now, and the page in focus
            "tool_counts": {name: t.counts() for name, t in self.tools.items()},  # what's under each tool's count (its red: tools_red)
            "health_counts": self._health_counts_now(),  # Library Health's findings per library and category, ignored apart (under the check pages' counts)
            "libdata": self.libdata.snapshot(),  # the data layer: what's gathered, what's being read
            "trash": self.trash.snapshot(),  # files the tools set aside, waiting to be purged
            "job": self.jobs.snapshot(),
            "inspect": self.inspector.snapshot(),  # the file job running now (or the last one): one card for every kind
            "queue": self.jobs.queued(),  # file work waiting its turn
            "queue_hold": self.jobs.hold_reason(),
            "settings_rev": self.settings.rev,
            # each library's optimizer choices and the converted kinds, by default: for their ↺ buttons
            "optimizer_defaults": {"library": dict(OPT_DEFAULTS)},
            "library_health_defaults": {"checks": libhealth_defaults(), "subtitle_language": "en", "audio_quality": "poor"},
            "retries": self.optimizing.retrying(),  # failed files to try again, after the file in hand
            "playback": {"sessions": self.monitor.snapshot(), "error": self.monitor.last_error, "last_poll": self.monitor.last_poll},
            "activity": self.activity.snapshot(),
            "reports": self.reports(),
            "mqtt": dict(self.ha.status),
            "updates": self.updates.status(),
            "peers": self.peers_summary,  # the other Plex Caches sharing library folders, and what fights (peers.py)
            "peer": self.peerlinks.snapshot(),  # the Plex Cache at each mirror's site: System → Mirror Plex Cache (peerlink.py)
            "health_scans": libhealth_scans(self),  # the checks that scan by themselves (Mirror Checks): what's running, when each library was checked
        }
        out["tools_red"] = self._tools_red(out)  # each tool page's red, per library: its card on the Tools page, its tabs, the menu
        return out

    def _tools_red(self, st: dict) -> dict:
        """Each tool page's red, per library — {page: [libraries]}: something red on the page that stops part of the tool
        there — a row a red chip stops (Tool.mark_stops: tool_counts' "blocked"), the optimizer not set up while it has
        files to do (the red box on every card) — or checks a check page couldn't run (health_counts' "unchecked"). The
        page's library tab, its card on the Tools page and the menu show it red (app.js toolBad), the same for every tool:
        a new tool's red goes here, and the tests hold every page of the Tools menu to it."""
        from . import libhealth
        out = {name: sorted(k for k, n in (c.get("blocked") or {}).items() if n) for name, c in st["tool_counts"].items()}
        op = st["optimizer"]
        out["optimizer"] = [] if op.get("configured") else sorted(k for k, sm in (op.get("libraries") or {}).items() if (sm or {}).get("todo"))
        # (each step says what it waits for; red only for something wrong that stops it — a conversion of the library left
        # partway at the other site, at the switch: migrating.py's checks of level "error")
        mig = st.get("migration") or {}
        out["dataset"] = [str(mig["state"]["library"])] if mig.get("state") and any(not c["ok"] and c.get("level") == "error" for c in mig.get("checks") or []) else []
        for cat, page in libhealth.PAGES.items():
            out[page] = sorted(k for k, h in st["health_counts"].items() if cat in (h.get("unchecked") or []))
        return out



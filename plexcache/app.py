"""Application core: shared state and background workers."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import os
import random
import threading
import time

from . import __version__, cachemode, checks, config, mapping, prefetch, report, zfs
from .activity import Activity
from .cachelog import CacheLog
from .auth import Auth
from .homeassistant import HomeAssistantBridge
from .i18n import Fixed, M, join, msg, size_text
from .updates import UpdateChecker
from .config import PLEX_DEFAULT_PORT, Settings
from .coverage import Coverage
from .filemap import FileMaps, SurveyJob
from .pauses import L2arcPauses
from .jobs import Jobs
from .migrating import Migration
from .optimizing import Optimizing
from .playback import PlaybackMonitor
from .plex import PlexClient, PlexError
from .reader import MiB, benchmark_file
from .scheduler import TIMED, due, next_run
from .truenas import TrueNASClient, TrueNASError, describe_pool, probe as probe_truenas
from .warmup import KIND_SECONDS, KINDS, L2ARC_RATE_SHARE, NEXT_DAY, next_day_check, LibraryStats, WarmupRun, plan, resolve_schedule

GiB = 1024 ** 3
log = logging.getLogger("plexcache")
TRUENAS_PROBE_PORT = 443  # tests point this at a fake server


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


KIND_LABEL = {"movie": M("movies"), "tv": M("TV episodes"), "audio": M("audio files")}


def index_summary(result: dict) -> M:
    """e.g. "end-of-file indexes — movies: 3 of 3 had one at the end, 0.2 GB (largest 2.0 MB)"."""
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
            bits.append(M("{kind}: {n} of {files} had one at the end, {size} (largest {largest})", kind=KIND_LABEL[kind],
                          n=st["files"], files=files, size=size(st["bytes"]), largest=size(st["largest"])))
        else:
            bits.append(M("{kind}: none at the end", kind=KIND_LABEL[kind]))
    joined = bits[0]
    for b in bits[1:]:
        joined = M("{a}; {b}", a=joined, b=b)
    text = M("end-of-file indexes — {list}", list=joined)
    if mp4:
        text = M("{text}. {n} MP4 files keep their index at the end (no “fast start”)",
                 one="{text}. {n} MP4 file keeps its index at the end (no “fast start”)", text=text, n=mp4)
    return text


class App:
    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self.settings = Settings(config_dir)
        # Optional web UI login, from PLEXCACHE_USERNAME / PLEXCACHE_PASSWORD in the app's YAML.
        self.auth = Auth.from_env(config_dir)
        self.activity = Activity(config_dir)
        self.lock = threading.RLock()
        self.version = __version__
        # Library scan
        self.files: list = []
        self._posters: tuple | None = None  # (files, {path: (poster, title)}) for poster_of() and title_of()
        self._thumbs: dict[str, bytes] = {}  # poster thumbnails, oldest first
        self.libraries: list = []
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
        # what's where in each media file (the Library Health page), and the job that reads it
        self.file_maps = FileMaps(self.config_dir)
        self.jobs.register("survey", lambda args, saved: SurveyJob(self, args.get("library"), saved, args.get("force")))  # turning a library folder into a dataset (Optimizer page)  # the optimizer service (second container): which files need it
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
        return PlexClient(p["url"], p["token"], timeout=timeout)

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

    def _scheduler_loop(self):
        last_check = dt.datetime.now()
        last_health = time.time()
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
            self.cachelog.prune()
            self.cachelog.save()
            self.record_coverage()
            if time.time() - last_health > 6 * 3600:
                last_health = time.time()
                self.run_checks_async()

    # --------------------------------------------------------------- library
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
                info = {"key": lib.key, "title": lib.title, "type": lib.type, "kind": lib.kind,
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
                self.stats = LibraryStats.from_files(files, {i["key"]: i["title"] for i in lib_info})
                self.scan_error = None
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
                                  "detail": M("Enter your Plex server's address, e.g. your NAS's IP. "
                                              "Port {port} is used if you leave it off.", port=str(PLEX_DEFAULT_PORT))}
        else:
            try:
                ident = PlexClient(p["url"], "", timeout=8).identity()
                res["plex_server"] = {"status": "ok", "text": M("Plex server found"),
                                      "detail": M("Plex Media Server {version} at {url} ({ms} ms)",
                                                  version=ident["version"], url=p["url"], ms=ident["ms"])}
            except PlexError as e:
                if "127.0.0.1" in p["url"] or "localhost" in p["url"]:
                    detail = M("{why}. Use your NAS's IP, not 127.0.0.1 — inside this app that means the app itself.", why=msg(e))
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
                                             "what's playing with apps that have a token for your account.")}
        else:
            try:
                info = self.plex(timeout=10).ping()
                n = len(self.plex(timeout=10).libraries())
                res["plex_token"] = {"status": "ok", "text": M("Signed in to Plex"),
                                     "detail": M("Token accepted by “{name}” · {n} libraries visible",
                                                 one="Token accepted by “{name}” · {n} library visible", name=info["name"], n=n)}
            except PlexError as e:
                bad = "token" in str(e).lower()
                res["plex_token"] = {"status": "fail", "text": M("Plex token rejected") if bad else M("Couldn't check Plex sign-in"),
                                     "detail": (M("Plex no longer accepts the saved token. Sign in again.")
                                                if bad else msg(e))}
        # TrueNAS: optional. Is the API there, then is the key accepted?
        t = s["truenas"]
        if not t["url"]:
            res["truenas_server"] = {"status": "warn", "text": M("TrueNAS not set up (optional)"),
                                     "detail": M("Without it, cache devices and dataset settings can't be checked, "
                                                 "and the L2ARC can't be sized.")}
            res["truenas_key"] = {"status": "off", "text": M("TrueNAS API key"), "detail": ""}
        elif not probe_truenas(t["url"]):
            res["truenas_server"] = {"status": "fail", "text": M("TrueNAS not found"),
                                     "detail": M("No TrueNAS API answered at {url}.", url=t["url"])}
            res["truenas_key"] = {"status": "off", "text": M("API key: waiting for TrueNAS"), "detail": ""}
        else:
            res["truenas_server"] = {"status": "ok", "text": M("TrueNAS found"), "detail": t["url"]}
            if not t["api_key"]:
                res["truenas_key"] = {"status": "warn", "text": M("TrueNAS API key needed"),
                                      "detail": M("Add a key so the app can read pool and dataset settings. "
                                                  "It only reads; nothing is changed.")}
            else:
                try:
                    with self.truenas() as tn:
                        v = tn.version()
                    res["truenas_key"] = {"status": "ok", "text": M("TrueNAS API key accepted"), "detail": v or M("Connected")}
                except TrueNASError as e:
                    res["truenas_key"] = {"status": "fail", "text": M("TrueNAS API key not accepted"), "detail": msg(e)}
        problems = [k for k in ("plex_server", "plex_token") if res[k]["status"] != "ok"]
        optional = [k for k in ("truenas_server", "truenas_key") if res[k]["status"] in ("warn", "fail")]
        if problems:
            res["summary"] = {"status": "fail" if any(res[k]["status"] == "fail" for k in problems) else "warn",
                              "text": M("Plex needs attention — the app can't do anything until Plex is connected.")}
        elif optional:
            res["summary"] = {"status": "warn", "text": M("Plex is connected. TrueNAS is optional but recommended.")}
        else:
            res["summary"] = {"status": "ok", "text": M("Everything is connected.")}
        with self.lock:
            self.connections = res
        return res

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

    def poster_thumb(self, path: str) -> bytes:
        """A small poster for lists (the Processes page), kept in memory: the last few hundred."""
        with self.lock:
            hit = self._thumbs.pop(path, None)
            if hit is not None:
                self._thumbs[path] = hit
                return hit
        data = self.plex(timeout=15).thumb(path)
        with self.lock:
            self._thumbs[path] = data
            while len(self._thumbs) > 400:
                self._thumbs.pop(next(iter(self._thumbs)))
        return data

    def library_info(self, key: str) -> dict | None:
        """A library from the last scan ({key, title, kind, ...}), or None."""
        return next((lib for lib in self.libraries if lib["key"] == str(key)), None)

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
                raise config.SettingsError(M("Prefetch doesn't cover that library"))
            start = choice.get("start") if isinstance(choice, dict) else None
            if kind and start not in (None, "default") and start not in prefetch.levels_for(kind):
                raise config.SettingsError(M("“{mode}” isn't a prefetch choice for that library", mode=str(start)))

    def library_datasets(self, key: str) -> list[dict]:
        """The datasets holding a library's folders (from the last check)."""
        return [d for d in (self.zfs or {}).get("datasets", []) if key in d["keys"]]

    def library_cacheable(self, key: str, feature: str | None = None) -> bool:
        """Ticked in Media mappings, its folders can be read here, and the
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

    def resolve_setting(self, sid: str, value: str) -> str:
        """The value a mode-dependent setting really uses right now (Default, or a fallback)."""
        return cachemode.resolve(sid, value, self.cache_mode(), self.fallbacks)

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
        if cache_max:
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
            result = checks.run_all(self)
            with self.lock:
                self.health = result
            return result
        finally:
            with self.lock:
                self.checking = False

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
                self.activity.event("warm-up", M("stopped: couldn't read libraries: {why}", why=self.scan_error), "warn")
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
                            lambda f: speeds.get(f.library_key, g["hdd_mb_per_sec"]),
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
        return sorted((f for f in list(self.files) if str(f.library_key) == str(key)),
                      key=lambda f: ((getattr(f, "title", "") or os.path.basename(f.path)).lower(), f.path))

    def library_view(self, key: str, offset: int = 0, limit: int = 100, q: str = "") -> dict:
        """A library's files for the Library Health page, by title: each with its layout (as last read;
        None until the library is surveyed) and what caching takes from it."""
        from .filemap import block_map, cache_plan
        from .warmup import library_seconds
        files = self._health_files(key)
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
            rec = (m or {}).get("blksize") or record  # the blocks the file is really stored in
            plan = cache_plan(m, f.size, f.kbps, secs, rec, kind)
            rows.append({"file": path, "label": getattr(f, "title", "") or os.path.basename(path), "poster": getattr(f, "poster", ""),
                         "size": f.size, "kbps": f.kbps, "kind": kind, "missing": st is None, "read": bool(m),
                         "saved": max(0, f.size - m["disk"]) if m and m.get("disk") is not None else None,
                         "plan": plan, "blocks": block_map(m, f.size, plan, rec)})
        return {"library": key, "total": len(files), "surveyed": self.file_maps.current(paths), "record": record,
                "offset": offset, "files": rows}

    def file_view(self, path: str) -> dict | None:
        """One file in full for the Library Health page: its layout (read now if it changed), tracks,
        attachments, and what caching takes from it. None if it isn't a library file."""
        from .filemap import block_map, cache_plan
        from .warmup import library_seconds
        f = self._library_file(path)
        if f is None:
            return None
        m = self.file_maps.read(path)
        self.file_maps.save()
        key, kind = str(f.library_key), getattr(f, "kind", "movie")
        record = self.record_for(key)
        rec = m.get("blksize") or record  # the blocks the file is really stored in
        secs = library_seconds(self.settings.get()["warmup"], key, kind if kind in ("movie", "tv", "audio") else "movie")
        plan = cache_plan(m, f.size, f.kbps, secs, rec, kind)
        return {"file": path, "label": getattr(f, "title", "") or os.path.basename(path), "poster": getattr(f, "poster", ""),
                "library": key, "size": f.size, "kbps": f.kbps, "kind": kind, "tracks": m.get("tracks") or [],
                "format": m.get("format"), "disk": m.get("disk"), "saved": max(0, f.size - m["disk"]) if m.get("disk") is not None else None,
                "video_start": m.get("video_start"), "video_end": m.get("video_end"), "record": rec, "dataset_record": record,
                "plan": plan, "blocks": block_map(m, f.size, plan, rec), "error": m.get("error")}

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
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(path + ".tmp", path)
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
        if not snap["configured"]:
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
        return {
            "version": self.version,
            "settings": self.settings.public(),
            "auth": {"enabled": self.auth.enabled, "user": self.auth.username if self.auth.enabled else ""},
            # Defaults of every setting (but secrets), for the page's ↺ "back to default" buttons
            "defaults": config.public_defaults(),
            "health": self.health,
            "checking": self.checking,
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
            "migration": dict(self.migration.snapshot(), candidates=self.migration.candidates()),
            "job": self.jobs.snapshot(),  # the file job running now (or the last one): one card for every kind
            "queue": self.jobs.queued(),  # file work waiting its turn
            "retries": self.optimizing.retrying(),  # failed files to try again, after the file in hand
            "playback": {"sessions": self.monitor.snapshot(), "error": self.monitor.last_error, "last_poll": self.monitor.last_poll},
            "activity": self.activity.snapshot(),
            "reports": self.reports(),
            "mqtt": dict(self.ha.status),
            "updates": self.updates.status(),
        }



"""Application core: shared state and background workers."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import threading
import time

from . import __version__, checks, config, mapping, report, zfs
from .activity import Activity
from .config import PLEX_DEFAULT_PORT, Settings
from .pauses import WarmupPauses
from .playback import PlaybackMonitor
from .plex import PlexClient, PlexError
from .reader import MiB, benchmark_file
from .scheduler import due, next_run
from .truenas import TrueNASClient, TrueNASError, describe_pool, probe as probe_truenas
from .warmup import KINDS, LibraryStats, WarmupRun, plan

GiB = 1024 ** 3
log = logging.getLogger("plexcache")
TRUENAS_PROBE_PORT = 443  # tests point this at a fake server


def _host_only(host_header: str | None) -> str | None:
    """'192.168.1.10:8642' -> '192.168.1.10'; ignores loopback and junk."""
    if not host_header:
        return None
    h = host_header.strip()
    if h.startswith("["):
        h = h[1:h.find("]")] if "]" in h else ""
    elif h.count(":") == 1:
        h = h.split(":")[0]
    if not h or h in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or not all(c.isalnum() or c in ".-:" for c in h):
        return None
    return h


KIND_LABEL = {"movie": "movies", "tv": "TV episodes", "audio": "audio files"}


def index_summary(result: dict) -> str:
    """e.g. "indexes cached: 6,809 TV episodes 0.2 GB (largest 1.1 MB); 412 MP4s keep theirs at the end"."""
    def size(n):
        if n >= 1e8:
            return f"{n / 1e9:.1f} GB"
        return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"
    bits, mp4 = [], 0
    for kind, st in result["index"].items():
        files = (result.get("by_kind") or {}).get(kind, {}).get("files", 0)
        if not files:
            continue
        mp4 += st["mp4_at_end"]
        if st["files"]:
            bits.append(f"{KIND_LABEL[kind]}: {st['files']:,} of {files:,} had one at the end, "
                        f"{size(st['bytes'])} (largest {size(st['largest'])})")
        else:
            bits.append(f"{KIND_LABEL[kind]}: none at the end")
    text = "end-of-file indexes — " + "; ".join(bits)
    if mp4:
        text += f". {mp4:,} MP4 file{'s' if mp4 != 1 else ''} keep their index at the end (no “fast start”)"
    return text


class App:
    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self.settings = Settings(config_dir)
        self.activity = Activity(config_dir)
        self.lock = threading.RLock()
        self.version = __version__
        # Library scan
        self.files: list = []
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
        # "Check every file": dataset -> {"running", "checked", "single", "sizes", "examples", "at", ...}
        self.block_scans: dict[str, dict] = self._load_json("blockscans.json") or {}
        for v in self.block_scans.values():
            v["running"] = False  # a scan cut short by a restart
        # Playback
        self.monitor = PlaybackMonitor(self)
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
        threading.Thread(target=self._scheduler_loop, name="scheduler", daemon=True).start()
        threading.Thread(target=self._startup, name="startup", daemon=True).start()

    def _startup(self):
        self.scan_library()
        self.run_checks()

    def _scheduler_loop(self):
        last_check = dt.datetime.now()
        last_health = time.time()
        while not self.stopping.wait(30):
            now = dt.datetime.now()
            w = self.settings.section("warmup")
            if w["enabled"] and self.warmup_run is None and due(w, now, self.last_warmup, last_check):
                self.start_warmup(manual=False)
            last_check = now
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
        except PlexError as e:
            with self.lock:
                self.scan_error = str(e)
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
            res["plex_server"] = {"status": "warn", "text": "Plex server address needed",
                                  "detail": "Enter your Plex server's address, e.g. your NAS's IP. "
                                            f"Port {PLEX_DEFAULT_PORT} is used if you leave it off."}
        else:
            try:
                ident = PlexClient(p["url"], "", timeout=8).identity()
                res["plex_server"] = {"status": "ok", "text": "Plex server found",
                                      "detail": f"Plex Media Server {ident['version']} at {p['url']} ({ident['ms']} ms)"}
            except PlexError as e:
                hint = " Use your NAS's IP, not 127.0.0.1 — inside this app that means the app itself." \
                    if "127.0.0.1" in p["url"] or "localhost" in p["url"] else ""
                res["plex_server"] = {"status": "fail", "text": "No Plex server found", "detail": str(e) + "." + hint}
        if res["plex_server"]["status"] != "ok":
            res["plex_token"] = {"status": "off", "text": "Plex sign-in: waiting for the server",
                                 "detail": "Sign-in is checked once the server is found."}
        elif not p["token"]:
            # Without a token Plex shares nothing, so no feature can work: a failure, not a warning.
            res["plex_token"] = {"status": "fail", "text": "Plex sign-in needed",
                                 "detail": "Nothing works until you sign in: Plex only shares your libraries and "
                                           "what's playing with apps that have a token for your account."}
        else:
            try:
                info = self.plex(timeout=10).ping()
                n = len(self.plex(timeout=10).libraries())
                res["plex_token"] = {"status": "ok", "text": "Signed in to Plex",
                                     "detail": f"Token accepted by “{info['name']}” · {n} libraries visible"}
            except PlexError as e:
                bad = "token" in str(e).lower()
                res["plex_token"] = {"status": "fail", "text": "Plex token rejected" if bad else "Couldn't check Plex sign-in",
                                     "detail": ("Plex no longer accepts the saved token. Sign in again."
                                                if bad else str(e))}
        # TrueNAS: optional. Is the API there, then is the key accepted?
        t = s["truenas"]
        if not t["url"]:
            res["truenas_server"] = {"status": "warn", "text": "TrueNAS not set up (optional)",
                                     "detail": "Without it, cache devices and dataset settings can't be checked, "
                                               "and Library warm-up can't be sized."}
            res["truenas_key"] = {"status": "off", "text": "TrueNAS API key", "detail": ""}
        elif not probe_truenas(t["url"]):
            res["truenas_server"] = {"status": "fail", "text": "TrueNAS not found",
                                     "detail": f"No TrueNAS API answered at {t['url']}."}
            res["truenas_key"] = {"status": "off", "text": "API key: waiting for TrueNAS", "detail": ""}
        else:
            res["truenas_server"] = {"status": "ok", "text": "TrueNAS found", "detail": t["url"]}
            if not t["api_key"]:
                res["truenas_key"] = {"status": "warn", "text": "TrueNAS API key needed",
                                      "detail": "Add a key so the app can read pool and dataset settings. "
                                                "It only reads; nothing is changed."}
            else:
                try:
                    with self.truenas() as tn:
                        v = tn.version()
                    res["truenas_key"] = {"status": "ok", "text": "TrueNAS API key accepted", "detail": v or "Connected"}
                except TrueNASError as e:
                    res["truenas_key"] = {"status": "fail", "text": "TrueNAS API key not accepted", "detail": str(e)}
        problems = [k for k in ("plex_server", "plex_token") if res[k]["status"] != "ok"]
        optional = [k for k in ("truenas_server", "truenas_key") if res[k]["status"] in ("warn", "fail")]
        if problems:
            res["summary"] = {"status": "fail" if any(res[k]["status"] == "fail" for k in problems) else "warn",
                              "text": "Plex needs attention — the app can't do anything until Plex is connected."}
        elif optional:
            res["summary"] = {"status": "warn", "text": "Plex is connected. TrueNAS is optional but recommended."}
        else:
            res["summary"] = {"status": "ok", "text": "Everything is connected."}
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
        """Remember how the browser reached us and fill in empty addresses from it.

        The browser usually talks to us via the NAS's own IP, which is also
        where Plex and TrueNAS live on a typical install.
        """
        host = _host_only(host_header)
        if not host or host == self.host:
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
                changed.append(f"Plex at {url}")
            except PlexError:
                pass
        if not s["truenas"]["url"]:
            url = f"https://{h}" + ("" if TRUENAS_PROBE_PORT == 443 else f":{TRUENAS_PROBE_PORT}")
            if probe_truenas(url, timeout=4):
                self.settings.update({"truenas": {"url": url}})
                changed.append(f"TrueNAS at {url}")
        if changed:
            self.activity.event("settings", "found " + " and ".join(changed))
        self.test_connections_async(then_checks=bool(changed), rescan=bool(changed))

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

    def feature_available(self, feature: str) -> tuple[bool, str]:
        """(True, "") or (False, why) — the same verdict the Features page shows."""
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

    # ------------------------------------------------------------------- RAM
    def ram_budget(self) -> dict:
        """How much RAM Now-playing and Next-episode may fill.

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
                info["truenas_error"] = str(e)
        self._survey_blocks(datasets)
        with self.lock:
            self.zfs = info
        return info

    BLOCK_SAMPLE = 300  # files per dataset; a sample is enough to see how much was written before a change

    def _survey_blocks(self, datasets: list[dict]) -> None:
        """Sample each cached dataset's files for the block size they were written with,
        so Health can tell when files predate a recordsize change."""
        mlist = zfs.mounts()
        kind_of = {lib["key"]: lib["kind"] for lib in self.libraries}
        for d in datasets:
            if not (d["keys"] or d["off"]) or d.get("fstype") != "zfs":
                continue
            mine = [f for f in self.files if f.library_key in d["keys"]]
            candidates = [self.map_path(f.path) for f in random.sample(mine, min(len(mine), self.BLOCK_SAMPLE * 3))]
            # Unticked libraries aren't listed from Plex, so sample their folders directly.
            candidates += _sample_files(d.get("off_folders") or [], self.BLOCK_SAMPLE * 3)
            paths = []
            for path in candidates:
                m = zfs.mount_for(path, mlist)
                if m and m["mountpoint"] == d["mountpoint"]:  # not a file on a child dataset
                    paths.append(path)
                if len(paths) >= self.BLOCK_SAMPLE:
                    break
            d["blocks"] = zfs.block_sizes(paths)
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
    def start_warmup(self, manual: bool) -> bool:
        with self.lock:
            if self.warmup_run is not None:
                return False
            self.warmup_stop = threading.Event()
            self.warmup_run = WarmupRun([], {k: 0 for k in KINDS}, 100, 150, self.warmup_stop)  # placeholder while scanning
            self.warmup_run.result = "scanning"
        threading.Thread(target=self._warmup_worker, args=(manual,), name="warmup", daemon=True).start()
        return True

    def stop_warmup(self):
        self.warmup_stop.set()

    def _warmup_worker(self, manual: bool):
        started = dt.datetime.now()
        try:
            self.activity.event("warm-up", "started" + (" (run now, full speed)" if manual else ""))
            self.scan_library()
            if self.scan_error:
                self.activity.event("warm-up", f"stopped: couldn't read libraries: {self.scan_error}", "warn")
                return
            self.probe_zfs()
            s = self.settings.get()
            w, g = s["warmup"], s["general"]
            p = plan(self.stats, w, self.l2_total(), self.index_avg())
            if p["status"] == "none":
                self.activity.event("warm-up", "skipped: no L2ARC on the media pool", "warn")
                return
            if p["status"] in ("reduced", "insufficient"):
                secs = ", ".join(f"{k} {v:g}s" for k, v in p["seconds"].items())
                self.activity.event("warm-up", f"L2ARC too small for your settings, using {secs}", "warn")
            files = [f.__class__(self.map_path(f.path), f.kbps, f.size, f.kind, f.library, f.library_key)
                     for f in self.files if self.library_cacheable(f.library_key, "warmup")]
            pauses = WarmupPauses(self, manual)
            holder: dict = {}

            def pause():
                why = pauses.reason()
                run = holder.get("run")
                if run is not None and why != run.paused:
                    self.activity.event("warm-up", f"paused: {why}" if why else "carrying on")
                    run.paused = why
                return why is not None

            speeds = {f.library_key: self.disk_speed_for_library(f.library_key) for f in files}
            run = WarmupRun(files, p["seconds"], 100 if manual else w["pacing_percent"],
                            lambda f: speeds.get(f.library_key, g["hdd_mb_per_sec"]),
                            self.warmup_stop, pause, zfs.arcstats)
            run.pacer.before_wait = pauses.mark_idle
            holder["run"] = run
            with self.lock:
                self.warmup_run = run
            self.activity.event("warm-up", f"reading the start of {len(files):,} files at {run.pacer.percent:g}% pacing")
            result = run.run()
            result["manual"] = manual
            self.activity.add_run(result)
            hr = result["hit_rate"]
            growth = result["l2_growth"]
            msg = f"{result['result']}: {result['files']:,} files, {result['bytes'] / 1e9:.1f} GB read"
            if hr is not None:
                msg += f", {hr * 100:.0f}% already cached"
            if growth is not None:
                msg += f", L2ARC {'+' if growth >= 0 else ''}{growth / 1e9:.1f} GB"
            if result["errors"]:
                msg += f", {result['errors']} unreadable"
            self.activity.event("warm-up", msg, "warn" if result["errors"] else "info")
            if result.get("index"):
                self.activity.event("warm-up", index_summary(result))
        except Exception as e:  # keep the app alive
            log.exception("warm-up failed")
            self.activity.event("warm-up", f"failed: {e}", "warn")
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

    def run_block_scan(self, dataset: str) -> bool:
        """Check the block size of every file on a dataset (Health shows a sample otherwise)."""
        d = next((d for d in (self.zfs or {}).get("datasets", []) if d.get("dataset") == dataset), None)
        if d is None:
            return False
        with self.lock:
            if (self.block_scans.get(dataset) or {}).get("running"):
                return False
            self.block_scans[dataset] = {"running": True, "checked": 0, "started": time.time()}

        def work():
            def progress(n):
                self.block_scans[dataset]["checked"] = n
            res = zfs.scan_block_sizes(d["mountpoint"], progress, lambda: self.stopping.is_set())
            res.update(running=False, at=time.time(), started=self.block_scans[dataset]["started"])
            with self.lock:
                self.block_scans[dataset] = res
                self._save_json("blockscans.json", self.block_scans)
            self.activity.event("record size", f"{dataset}: checked {res['checked']:,} files")
            self.run_checks()

        threading.Thread(target=work, name="block-scan", daemon=True).start()
        return True

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
                    self.activity.event("benchmark", "measured " + ", ".join(f"{k} {v} MB/s" for k, v in results.items()))
                    self.benchmark = {"rows": rows, "results": results, "at": time.time()}
                else:
                    self.benchmark = {"rows": [], "results": {}, "at": time.time(),
                                      "error": "No readable media files to benchmark."}
            finally:
                with self.lock:
                    self.benchmarking = False
                    self.benchmark_progress = None
            self.run_checks()

        self.benchmark_thread = threading.Thread(target=work, daemon=True)
        self.benchmark_thread.start()

    # ---------------------------------------------------------------- state
    def state(self) -> dict:
        s = self.settings.get()
        now = dt.datetime.now()
        nxt = next_run(s["warmup"], now, self.last_warmup) if s["warmup"]["enabled"] else None
        run = self.warmup_run
        with self.lock:
            zinfo = dict(self.zfs)
            stats = self.stats.to_dict()
            libraries = list(self.libraries)
        return {
            "version": self.version,
            "settings": self.settings.public(),
            # Defaults of the tunable settings, for the page's "back to default" buttons.
            "defaults": {sec: {k: v for k, v in config.DEFAULTS[sec].items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
                         for sec in ("general", "warmup", "nowplaying", "nextepisode")},
            "health": self.health,
            "checking": self.checking,
            "connections": self.connections,
            "testing": self.testing,
            "host": self.host,
            "locations": self.locations,
            "mapping_choices": self.mapping_choices,
            "l2arc": self.l2arc_status(),
            "ram": self.ram_budget(),
            "library": {"stats": stats, "libraries": libraries, "error": self.scan_error, "scanning": self.scanning},
            "zfs": {
                "arc": zinfo.get("arc"),
                "pools": zinfo.get("pools", {}),
                "datasets": [{k: v for k, v in d.items() if k != "props_debug"} for d in zinfo.get("datasets", [])],
                "l2_total": self.l2_total(),
            },
            "plan": plan(self.stats, s["warmup"], self.l2_total(), self.index_avg()),
            "index_avg": self.index_avg(),
            "warmup": {
                "running": run is not None,
                "progress": run.progress() if run else None,
                "phase": run.result if run else None,
                "next_run": nxt.isoformat(timespec="minutes") if nxt else None,
                "last_run": self.last_warmup.isoformat(timespec="minutes") if self.last_warmup else None,
            },
            "benchmark": {"running": self.benchmarking, "last": self.benchmark, "progress": self.benchmark_progress},
            "block_scans": {k: {x: v.get(x) for x in ("running", "checked", "at", "complete", "error")}
                            for k, v in self.block_scans.items()},
            "playback": {"sessions": self.monitor.snapshot(), "error": self.monitor.last_error, "last_poll": self.monitor.last_poll},
            "activity": self.activity.snapshot(),
            "reports": self.reports(),
        }


def _sample_files(folders: list[str], limit: int, max_seen: int = 5000) -> list[str]:
    """Up to ``limit`` random files from these folders (looking at no more than ``max_seen``)."""
    seen: list[str] = []
    for root in folders:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [n for n in dirnames if not n.startswith((".", "@"))]
            seen.extend(os.path.join(dirpath, n) for n in filenames if not n.startswith("."))
            if len(seen) >= max_seen:
                break
        if len(seen) >= max_seen:
            break
    return random.sample(seen, min(limit, len(seen)))

"""Application core: shared state and background workers."""

from __future__ import annotations

import datetime as dt
import logging
import os
import random
import threading
import time

from . import __version__, checks, zfs
from .activity import Activity
from .config import Settings
from .playback import PlaybackMonitor
from .plex import PlexClient, PlexError
from .reader import MiB, benchmark_file
from .scheduler import due, next_run
from .truenas import TrueNASClient, TrueNASError, describe_pool
from .warmup import KINDS, LibraryStats, WarmupRun, plan

log = logging.getLogger("plexcache")


class App:
    def __init__(self, config_dir: str):
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
        # Playback
        self.monitor = PlaybackMonitor(self)
        self._slots: dict = {}
        self._slots_lock = threading.Lock()
        self.stopping = threading.Event()

    # --------------------------------------------------------------- helpers
    def map_path(self, path: str) -> str:
        for row in self.settings.section("general")["path_map"]:
            plex = row["plex"]
            if path == plex or path.startswith(plex + "/"):
                return row["local"] + path[len(plex):]
        return path

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
        pool = (self.zfs or {}).get("pool_info")
        return pool["l2_total"] if pool else None

    # ------------------------------------------------------------ background
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
                    info["sample"] = [f.path for f in random.sample(lf, min(3, len(lf)))]
                    files.extend(lf)
                lib_info.append(info)
            with self.lock:
                self.files, self.libraries = files, lib_info
                self.stats = LibraryStats.from_files(files)
                self.scan_error = None
        except PlexError as e:
            with self.lock:
                self.scan_error = str(e)
        finally:
            with self.lock:
                self.scanning = False

    # ------------------------------------------------------------------- zfs
    def probe_zfs(self) -> dict:
        info: dict = {"arc": zfs.arc_summary(zfs.arcstats())}
        # Find the dataset holding the media from a library location.
        media_path = None
        for lib in self.libraries:
            if lib["kind"] and not lib["excluded"]:
                for loc in lib["locations"]:
                    local = self.map_path(loc)
                    if os.path.exists(local):
                        media_path = local
                        break
            if media_path:
                break
        mount = zfs.mount_for(media_path) if media_path else None
        info["media_path"] = media_path
        info["mount"] = mount
        info["params"] = {p: zfs.module_param(p) for p in ("l2arc_noprefetch", "l2arc_write_max", "l2arc_rebuild_enabled")}
        t = self.settings.section("truenas")
        if t["url"] and t["api_key"]:
            try:
                with self.truenas() as tn:
                    info["truenas_version"] = tn.version()
                    if mount and mount.get("pool"):
                        pool = tn.pool(mount["pool"])
                        if pool:
                            missing = [c.get("disk") for c in (pool.get("topology") or {}).get("cache") or []
                                       if not (c.get("stats") or {}).get("size") and c.get("disk")]
                            sizes = tn.disk_sizes(missing) if missing else {}
                            info["pool_info"] = describe_pool(pool, sizes)
                        info["dataset_props"] = tn.dataset_cache_props(mount["dataset"])
                info["truenas_error"] = None
            except TrueNASError as e:
                info["truenas_error"] = str(e)
        with self.lock:
            self.zfs = info
        return info

    # ---------------------------------------------------------------- checks
    def run_checks(self) -> dict:
        with self.lock:
            if self.checking:
                return self.health or {}
            self.checking = True
        try:
            self.probe_zfs()
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
            p = plan(self.stats, w, self.l2_total())
            if p["status"] == "none":
                self.activity.event("warm-up", "skipped: no L2ARC on the media pool", "warn")
                return
            if p["status"] in ("reduced", "insufficient"):
                secs = ", ".join(f"{k} {v:g}s" for k, v in p["seconds"].items())
                self.activity.event("warm-up", f"L2ARC too small for your settings, using {secs}", "warn")
            files = [f.__class__(self.map_path(f.path), f.kbps, f.size, f.kind, f.library) for f in self.files]
            limit = g["pause_when_streams"]

            def pause():
                return bool(limit) and self.monitor.active_streams() >= limit

            run = WarmupRun(files, p["seconds"], 100 if manual else w["pacing_percent"], g["hdd_mb_per_sec"],
                            self.warmup_stop, pause, zfs.arcstats)
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
        except Exception as e:  # keep the app alive
            log.exception("warm-up failed")
            self.activity.event("warm-up", f"failed: {e}", "warn")
        finally:
            self.last_warmup = started
            with self.lock:
                self.warmup_run = None

    # ------------------------------------------------------------- benchmark
    def run_benchmark(self):
        with self.lock:
            if self.benchmarking:
                return
            self.benchmarking = True

        def work():
            rows = []
            try:
                for kind in KINDS:
                    pool = [f for f in self.files if f.kind == kind and f.size >= 64 * MiB] or \
                           [f for f in self.files if f.kind == kind and f.size >= MiB]
                    for f in random.sample(pool, min(3, len(pool))):
                        path = self.map_path(f.path)
                        res = benchmark_file(path, f.size)
                        if not res.error and res.bytes:
                            rows.append({"kind": kind, "file": os.path.basename(path), "mb_s": round(res.mb_per_sec),
                                         "mb": round(res.bytes / MiB)})
                if rows:
                    # Use the median so one cached file doesn't skew the result.
                    speeds = sorted(r["mb_s"] for r in rows)
                    median = speeds[len(speeds) // 2]
                    self.settings.update({"general": {"hdd_mb_per_sec": median,
                                                      "hdd_measured_at": dt.datetime.now().isoformat(timespec="seconds")}})
                    self.activity.event("benchmark", f"measured {median} MB/s over {len(rows)} files")
                    self.benchmark = {"rows": rows, "median": median, "at": time.time()}
                else:
                    self.benchmark = {"rows": [], "median": None, "at": time.time(),
                                      "error": "No readable media files to benchmark."}
            finally:
                with self.lock:
                    self.benchmarking = False
            self.run_checks()

        threading.Thread(target=work, daemon=True).start()

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
            "health": self.health,
            "checking": self.checking,
            "library": {"stats": stats, "libraries": libraries, "error": self.scan_error, "scanning": self.scanning},
            "zfs": {
                "arc": zinfo.get("arc"),
                "pool": zinfo.get("pool_info"),
                "dataset": (zinfo.get("mount") or {}).get("dataset"),
                "dataset_props": zinfo.get("dataset_props"),
                "l2_total": self.l2_total(),
            },
            "plan": plan(self.stats, s["warmup"], self.l2_total()),
            "warmup": {
                "running": run is not None,
                "progress": run.progress() if run else None,
                "phase": run.result if run else None,
                "next_run": nxt.isoformat(timespec="minutes") if nxt else None,
                "last_run": self.last_warmup.isoformat(timespec="minutes") if self.last_warmup else None,
            },
            "benchmark": {"running": self.benchmarking, "last": self.benchmark},
            "playback": {"sessions": self.monitor.snapshot(), "error": self.monitor.last_error, "last_poll": self.monitor.last_poll},
            "activity": self.activity.snapshot(),
        }

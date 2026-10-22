import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import logging
from unittest import mock
import os
import re
import shutil
import struct
import sys
import tempfile
import errno
import socket
import threading
import time
import types
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

TMP = tempfile.mkdtemp(prefix="plexcache-test-")
# Never ask GitHub for the latest version while testing (an app started by a test would, after 30 s).
os.environ["PLEXCACHE_UPDATE_SOURCE"] = "http://127.0.0.1:9/never"
# No optimizer service: nothing listens there, so the app sees it as not set up.
os.environ["PLEXCACHE_OPTIMIZER_URL"] = "http://127.0.0.1:9"
MEDIA = os.path.join(TMP, "media")
import fakes  # noqa: E402

CAT = fakes.make_media(MEDIA)
# TV sits on its own child dataset, like many real setups.
# A spare dataset is mounted too, with no library in it.
SPARE = os.path.join(TMP, "spare")
os.makedirs(SPARE)
os.environ["PLEXCACHE_PROC"] = fakes.make_proc(TMP, MEDIA, extra=((os.path.join(MEDIA, "TV"), "tank/plexfiles/tv"),
                                                                  (SPARE, "tank/spare")))
os.environ["PLEXCACHE_SYS"] = fakes.make_sys(TMP, {"l2arc_noprefetch": 1, "l2arc_write_max": 33554432,
                                                    "l2arc_rebuild_enabled": 1, "l2arc_mfuonly": 0})

from plexcache import config, scheduler, zfs  # noqa: E402


def en(v) -> str:
    """A message from the API ({"_t": ...}) as English text."""
    return str(from_json(v))

# Test files sit on an ordinary filesystem, whose 4K blocks would look like files written
# before a recordsize change. Report every sampled file as fitting in one block instead
# (no information); BlockSizeTests checks the real function and what Health makes of it.
REAL_BLOCK_SIZES = zfs.block_sizes
zfs.block_sizes = lambda paths: (lambda n: {"checked": n, "single": n, "sizes": {}})(len(list(paths)))
from plexcache.app import App  # noqa: E402
from plexcache.i18n import from_json  # noqa: E402
from plexcache.plex import PlexClient, PlexError  # noqa: E402
from plexcache.reader import MiB, ReadResult, is_cache_hit, read_range  # noqa: E402
from plexcache.truenas import TrueNASClient, TrueNASError, describe_pool, ws_url  # noqa: E402
from plexcache.warmup import LibraryStats, WarmupRun, plan, preload_bytes  # noqa: E402

PLEX = fakes.FakePlex(CAT)
TRUENAS = fakes.FakeTrueNAS(workdir=TMP)
# The parent dataset (Movies, Audiobooks) is left at primarycache=metadata and 128K;
# the TV child dataset is set up properly. Health has to tell them apart.
TV_DS = "tank/plexfiles/tv"
TRUENAS.per_dataset[TV_DS] = {"primarycache": "all", "secondarycache": "all", "recordsize": 1048576}


def dataset_fixes(group: dict) -> dict:
    """Problems listed under the Media datasets table, by title."""
    return {f["title"]: f for r in group["datasets"] for f in r["fixes"]}


def zfs_with(saved, props=None, pools=None, datasets=None):
    """A copy of app.zfs with some dataset properties or pool fields changed."""
    import copy
    z = copy.deepcopy(saved)
    if datasets is not None:
        z["datasets"] = datasets
    for d in z["datasets"]:
        if props and d["dataset"] in props:
            d["props"] = props[d["dataset"]] if props[d["dataset"]] is None else dict(d["props"] or {}, **props[d["dataset"]])
    for name, change in (pools or {}).items():
        z["pools"][name].update(change)
    return z


# An exception in a background thread (web server, monitor, auto-detect...) only gets printed by
# Python; it doesn't fail the test that caused it. Collect them and fail the run at the end.
THREAD_ERRORS: list = []


def _thread_error(args):
    import traceback
    THREAD_ERRORS.append("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


threading.excepthook = _thread_error
# The app logs what it does (warm-up started, playback errors...). Keep that out of the test output;
# tests that care use assertLogs.
logging.getLogger("plexcache").addHandler(logging.NullHandler())


def tearDownModule():
    PLEX.close()
    TRUENAS.close()
    shutil.rmtree(TMP, ignore_errors=True)
    if THREAD_ERRORS:
        raise AssertionError(f"{len(THREAD_ERRORS)} error(s) in background threads:\n\n" + "\n".join(THREAD_ERRORS))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)

    def test_defaults_and_persistence(self):
        s = config.Settings(self.dir)
        s.update({"warmup": {"reserve_gb": 20}})
        again = config.Settings(self.dir)
        self.assertEqual(again.section("warmup")["reserve_gb"], 20)
        self.assertNotIn("enabled", again.section("warmup"))            # warm-up is set per library now
        self.assertEqual(oct(os.stat(again.path).st_mode & 0o777), "0o600")

    def test_secrets_are_masked_and_kept(self):
        s = config.Settings(self.dir)
        s.update({"plex": {"token": "secret"}})
        pub = s.public()
        self.assertEqual(pub["plex"]["token"], config.SECRET_MASK)
        self.assertTrue(pub["plex"]["token_set"])
        s.update({"plex": {"token": config.SECRET_MASK, "url": "192.168.1.10:32400"}})
        self.assertEqual(s.section("plex")["token"], "secret")
        self.assertEqual(s.section("plex")["url"], "http://192.168.1.10:32400")
        self.assertEqual(s.export()["plex"]["token"], "")

    def test_validation(self):
        s = config.Settings(self.dir)
        with self.assertRaises(config.SettingsError):
            s.update({"warmup": {"pacing_percent": 0}})
        with self.assertRaises(config.SettingsError):
            s.update({"warmup": {"nope": 1}})
        with self.assertRaises(config.SettingsError):
            s.update({"nextepisode": {"libraries": {"2": {"start": "forever"}}}})
        # The per-kind minimum seconds are retired: the floor is now the first record of each file
        with self.assertRaises(config.SettingsError):
            s.update({"warmup": {"movie_min_sec": 100}})

    def test_broken_file_is_set_aside(self):
        with open(os.path.join(self.dir, "settings.json"), "w") as fh:
            fh.write("{not json")
        s = config.Settings(self.dir)
        self.assertEqual(s.section("warmup")["reserve_gb"], 100)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "settings.json.broken")))

    def test_url_defaults(self):
        s = config.Settings(self.dir)
        s.update({"plex": {"url": "192.168.50.222"}, "truenas": {"url": "192.168.50.222"}})
        self.assertEqual(s.section("plex")["url"], "http://192.168.50.222:32400")
        self.assertEqual(s.section("truenas")["url"], "https://192.168.50.222")
        s.update({"plex": {"url": "192.168.50.222:32401"}})
        self.assertEqual(s.section("plex")["url"], "http://192.168.50.222:32401")
        s.update({"plex": {"url": "https://plex.example.com"}})  # reverse proxy: no port added
        self.assertEqual(s.section("plex")["url"], "https://plex.example.com")


class PlexTests(unittest.TestCase):
    def setUp(self):
        self.c = PlexClient(PLEX.url, "good-token")

    def test_bad_token(self):
        with self.assertRaisesRegex(PlexError, "token"):
            PlexClient(PLEX.url, "bad").libraries()

    def test_unreachable(self):
        with self.assertRaisesRegex(PlexError, "Can’t reach"):
            PlexClient("http://127.0.0.1:1", "x", timeout=2).ping()

    def test_libraries_and_files(self):
        libs = {lib.key: lib for lib in self.c.libraries()}
        self.assertEqual(libs["1"].kind, "movie")
        self.assertIsNone(libs["5"].kind)
        movies = self.c.library_files(libs["1"])
        self.assertEqual(len(movies), 4)
        # entities decoded (the script only handled &amp;)
        self.assertIn("Movie & Friends 0", movies[0].path)
        self.assertTrue(os.path.exists(movies[0].path))
        eps = self.c.library_files(libs["2"])
        self.assertEqual(len(eps), 6)
        self.assertIn("Show's Name", eps[0].path)
        audio = self.c.library_files(libs["3"])
        # no bitrate from Plex -> size*8/duration
        self.assertEqual(audio[0].kbps, int(2 * MiB * 8 / 120000))

    def test_paging(self):
        import plexcache.plex as plexmod
        old = plexmod.PAGE_SIZE
        plexmod.PAGE_SIZE = 2
        try:
            lib = [lib for lib in self.c.libraries() if lib.key == "2"][0]
            self.assertEqual(len(self.c.library_files(lib)), 6)
        finally:
            plexmod.PAGE_SIZE = old

    def test_identity_needs_no_token(self):
        info = PlexClient(PLEX.url, "").identity()
        self.assertEqual(info["version"], "1.41.0")
        with self.assertRaises(PlexError):
            PlexClient(TRUENAS.url.replace("https", "http"), "", timeout=2).identity()

    def test_next_episode_crosses_seasons(self):
        last_s1 = [e for e in CAT["episodes"] if e["season"] == 1][-1]
        nxt = self.c.next_episode(last_s1["key"], "500")
        self.assertEqual((nxt["season"], nxt["episode"]), (2, 1))
        self.assertTrue(nxt["files"][0][0].endswith("S02E01.mkv"))
        self.assertIsNone(self.c.next_episode(CAT["episodes"][-1]["key"], "500"))

    def test_sessions(self):
        ep = CAT["episodes"][0]
        PLEX.sessions = [{"id": "s1", "item": ep, "offset": 600000, "duration": 1200000, "credits": 1100000}]
        try:
            s = self.c.sessions()[0]
        finally:
            PLEX.sessions = []
        self.assertEqual(s.type, "episode")
        self.assertEqual(s.label, "Show's Name · S01E01")
        self.assertAlmostEqual(s.progress, 0.5)
        self.assertEqual(s.credits_start_ms, 1100000)
        self.assertEqual(s.files[0][0], ep["path"])


class TrueNASTests(unittest.TestCase):
    def test_ws_url_requires_https(self):
        self.assertEqual(ws_url("https://nas.local"), "wss://nas.local/api/current")
        self.assertEqual(ws_url("nas.local:444"), "wss://nas.local:444/api/current")
        with self.assertRaises(TrueNASError):
            ws_url("http://nas.local")

    def test_queries(self):
        with TrueNASClient(TRUENAS.url, "good-key") as tn:
            self.assertEqual(tn.version(), "TrueNAS-25.10.0")
            pool = tn.pool("tank")
            sizes = tn.disk_sizes(["nvme1n1"])
            props = tn.dataset_cache_props("tank/plexfiles")
        info = describe_pool(pool, sizes)
        self.assertEqual(info["l2_total"], 960 * 10 ** 9)
        self.assertEqual(info["l2_online"], 2)
        self.assertEqual(info["data_disks"], 4)
        self.assertEqual(props, {"primarycache": "metadata", "secondarycache": "all", "recordsize": 131072, "compression": "lz4"})

    def test_probe(self):
        from plexcache.truenas import probe
        self.assertTrue(probe(TRUENAS.url))
        self.assertFalse(probe("https://127.0.0.1:1", timeout=2))
        self.assertFalse(probe(PLEX.url.replace("http://", "https://"), timeout=2))

    def test_cache_props_shapes(self):
        from plexcache.truenas import extract_props
        names = ("primarycache", "secondarycache", "recordsize")
        # zfs.dataset.query style, pool.dataset.query style, plain strings, and missing
        self.assertEqual(extract_props({"properties": {"primarycache": {"value": "all", "rawvalue": "all"}}}, names),
                         {"primarycache": "all"})
        self.assertEqual(extract_props({"primarycache": {"value": "METADATA", "rawvalue": "metadata"}}, names),
                         {"primarycache": "metadata"})
        self.assertEqual(extract_props({"secondarycache": "all"}, names), {"secondarycache": "all"})
        self.assertEqual(extract_props({"primarycache": {"value": "", "rawvalue": None}}, names), {})

    def test_bad_key(self):
        with self.assertRaisesRegex(TrueNASError, "rejected"):
            TrueNASClient(TRUENAS.url, "wrong")

    def test_verify_tls_rejects_self_signed(self):
        with self.assertRaisesRegex(TrueNASError, "TLS"):
            TrueNASClient(TRUENAS.url, "good-key", verify_tls=True)


class ZfsTests(unittest.TestCase):
    def test_arcstats(self):
        a = zfs.arc_summary(zfs.arcstats())
        self.assertAlmostEqual(a["arc_hit_ratio"], 0.94)
        self.assertTrue(a["l2_present"])

    def test_mount_for(self):
        m = zfs.mount_for(os.path.join(MEDIA, "Movies", "x.mkv"))
        self.assertEqual(m["dataset"], "tank/plexfiles")
        self.assertEqual(m["pool"], "tank")
        self.assertEqual(zfs.mount_for("/etc/passwd")["fstype"], "overlay")

    def test_escaped_mountpoint(self):
        mounts = [{"root": "/", "mountpoint": "/media/My Films", "fstype": "zfs", "source": "tank/films"}]
        self.assertEqual(zfs.mount_for("/media/My Films/a.mkv", mounts)["dataset"], "tank/films")
        self.assertEqual(zfs._unescape("/media/My\\040Films"), "/media/My Films")


class ReaderAndPlanTests(unittest.TestCase):
    def test_preload_bytes_units(self):
        # 8000 kbps for 10 s = 10,000,000 bytes -> rounds up to 10 MiB
        self.assertEqual(preload_bytes(8000, 10), 10 * MiB)
        self.assertEqual(preload_bytes(8000, 10, size=3 * MiB), 3 * MiB)
        self.assertEqual(preload_bytes(0, 10), MiB)
        # in whole records of the dataset when it's known; 0 seconds is one record
        self.assertEqual(preload_bytes(8000, 10, record=4 * MiB), 12 * MiB)
        self.assertEqual(preload_bytes(8000, 0, record=4 * MiB), 4 * MiB)
        self.assertEqual(preload_bytes(8000, 0), MiB)
        from plexcache.warmup import align_ranges
        self.assertEqual(align_ranges([(5 * MiB + 10, 100), (6 * MiB - 1, 2)], MiB, 100 * MiB), [(5 * MiB, 2 * MiB)])
        self.assertEqual(align_ranges([(99 * MiB + 5, 10)], 4 * MiB, 99 * MiB + 15), [(96 * MiB, 3 * MiB + 15)])
        self.assertEqual(align_ranges([(5, 10)], None, 100), [(5, 10)])

    def test_failed_reads_are_not_hits(self):
        res = read_range("/nonexistent/file.mkv", 0, MiB)
        self.assertIsNotNone(res.error)
        self.assertFalse(is_cache_hit(res, 150))
        self.assertTrue(is_cache_hit(ReadResult(100 * MiB, 0.1), 150))
        self.assertFalse(is_cache_hit(ReadResult(100 * MiB, 1.0), 150))

    def test_read_range(self):
        path = CAT["movies"][0]["path"]
        res = read_range(path, 5 * MiB, 3 * MiB)
        self.assertEqual(res.bytes, 3 * MiB)
        res = read_range(path, 39 * MiB, 10 * MiB)
        self.assertEqual(res.bytes, MiB)  # stops at end of file

    def _stats(self):
        st = LibraryStats()
        st.count = {"movie": 1240, "tv": 14600, "audio": 9800}
        st.avg_kbps = {"movie": 18000, "tv": 6500, "audio": 128}
        st.libraries = [{"key": "1", "name": "Movies", "kind": "movie", "count": 1240, "avg_kbps": 18000},
                        {"key": "2", "name": "TV", "kind": "tv", "count": 14600, "avg_kbps": 6500},
                        {"key": "3", "name": "Music", "kind": "audio", "count": 9800, "avg_kbps": 128}]
        return st

    def test_stats_per_library(self):
        from types import SimpleNamespace as F
        files = [F(kind="audio", kbps=64, library_key="5"), F(kind="audio", kbps=96, library_key="5"),
                 F(kind="audio", kbps=900, library_key="9"), F(kind="movie", kbps=20000, library_key="1")]
        st = LibraryStats.from_files(files, {"5": "Audiobooks", "9": "Dr Music"})
        self.assertEqual((st.count["audio"], st.avg_kbps["audio"]), (3, 353))
        self.assertEqual([(l["name"], l["kind"], l["count"], l["avg_kbps"]) for l in st.libraries],
                         [("Movies", "movie", 1, 20000), ("Audiobooks", "audio", 2, 80), ("Dr Music", "audio", 1, 900)])
        self.assertEqual(st.to_dict()["libraries"][2]["name"], "Dr Music")

    def test_plan_fits_and_reduces(self):
        w = config.DEFAULTS["warmup"]
        from plexcache.warmup import DEFAULT_INDEX
        p = plan(self._stats(), w, 960 * 10 ** 9)
        self.assertEqual(p["status"], "fits")
        # record sizes not checked: sized from the bitrate alone, plus each file's index
        self.assertAlmostEqual(p["need_by_kind"]["movie"] / 1e9, (18000 * 125 * 60 * 1240 * 1.05 + 1240 * DEFAULT_INDEX["movie"]) / 1e9)
        p = plan(self._stats(), w, 400 * 10 ** 9)
        self.assertEqual(p["status"], "reduced")
        self.assertLess(p["seconds"]["1"], 60)                     # seconds are per library now
        self.assertGreaterEqual(p["seconds"]["1"], 0)
        planned = sum(p["planned_by_kind"].values())
        self.assertLessEqual(planned, 400 * 10 ** 9 - 100 * 10 ** 9 + 1)
        self.assertEqual(plan(self._stats(), w, 0)["status"], "none")
        self.assertEqual(plan(self._stats(), w, None)["status"], "unknown")
        self.assertEqual(plan(self._stats(), w, 101 * 10 ** 9)["status"], "insufficient")
        # Each library's own seconds, and only the libraries warm-up covers
        w2 = dict(w, libraries={"1": {"seconds": 30}})
        p = plan(self._stats(), w2, 960 * 10 ** 9)
        self.assertAlmostEqual(p["need_by_kind"]["movie"] / 1e9, (18000 * 125 * 30 * 1240 * 1.05 + 1240 * DEFAULT_INDEX["movie"]) / 1e9)
        self.assertEqual([r["set_sec"] for r in p["libraries"]], [30, 30, 10])
        self.assertEqual([r["key"] for r in plan(self._stats(), w2, None, keys={"2"})["libraries"]], ["2"])

    def test_warmup_run_counts_errors_separately(self):
        import threading
        from plexcache.plex import MediaFile
        files = [MediaFile(m["path"], m["kbps"], m["size"], "movie") for m in CAT["movies"]]
        files.append(MediaFile("/missing/file.mkv", 8000, 10 * MiB, "movie"))
        run = WarmupRun(files, {"movie": 2, "tv": 2, "audio": 2}, 100, 150, threading.Event(), arcstats=zfs.arcstats)
        r = run.run()
        self.assertEqual(r["files"], 5)
        self.assertEqual(r["errors"], 1)
        self.assertEqual(r["by_kind"]["movie"]["hits"] + r["by_kind"]["movie"]["reads"], 4)
        self.assertEqual(r["result"], "ok")
        self.assertEqual(r["l2_growth"], 0)

    def test_warmup_run_outcomes(self):
        """A run stopped before it starts, one where most files can't be read ("errors"), reads that
        came from the disks, a sample of at most 30 files for the next-day check — and a plan that
        only just fits, and a schedule worked out from the caching mode alone."""
        import threading
        from plexcache import warmup
        from plexcache.plex import MediaFile
        d = tempfile.mkdtemp(dir=TMP)
        files = []
        for i in range(warmup.SAMPLE_FILES + 5):
            p = os.path.join(d, f"f{i}.mkv")
            with open(p, "wb") as fh:
                fh.write(os.urandom(4096))
            files.append(MediaFile(p, 8000, 4096, "movie"))
        stop = threading.Event()
        stop.set()
        run = WarmupRun(files, {"movie": 2}, 100, 150, stop)
        self.assertEqual(run.run()["result"], "stopped")
        self.assertEqual(run.progress()["done"], 0)
        r = WarmupRun(files, {"movie": 2}, 100, 10 ** 9, threading.Event()).run()      # disks "faster" than any read: none was a hit
        self.assertEqual((r["result"], r["by_kind"]["movie"]["hits"], len(r["sample"])), ("ok", 0, warmup.SAMPLE_FILES))
        missing = [MediaFile(f"/missing/{i}.mkv", 8000, 10 * MiB, "movie") for i in range(3)]
        self.assertEqual(WarmupRun(missing + files[:1], {"movie": 2}, 100, 150, threading.Event()).run()["result"], "errors")
        w = dict(config.DEFAULTS["warmup"], libraries={})
        need = plan(self._stats(), w, None)["need_max"]
        self.assertEqual(plan(self._stats(), w, int(need / 0.95) + w["reserve_gb"] * warmup.GB)["status"], "tight")
        self.assertEqual(warmup.resolve_schedule("ram", "1", "default"), "none")

    def test_next_day_check(self):
        import threading
        from plexcache.plex import MediaFile
        from plexcache.warmup import next_day_check
        files = [MediaFile(m["path"], m["kbps"], m["size"], "movie") for m in CAT["movies"]]
        r = WarmupRun(files, {"movie": 2}, 100, 150, threading.Event()).run()
        self.assertEqual(sorted(r["sample"]), sorted(f.path for f in files))     # a few files it read, kept for tomorrow
        check = next_day_check(r["sample"] + ["/missing/file.mkv"])
        self.assertEqual(check["checked"], 4)                                      # unreadable files don't count
        self.assertTrue(0 <= check["pct"] <= 100)
        # The app checks each run a day after it finished, then forgets the sample
        app = _new_app()
        r.update(started=time.time() - 90000, finished=time.time() - 87000)
        app.activity.add_run(r)
        self.assertNotIn("sample", app.activity.snapshot()["runs"][0])            # the page doesn't get the paths
        app.check_next_day()
        self.assertTrue(wait_for(lambda: app.activity.snapshot()["runs"][0].get("next_day") is not None, 10))
        self.assertEqual(app.activity.runs_due(time.time(), 0), [])
        self.assertTrue(any("still on the SSD" in e["message"] for e in app.activity.snapshot()["events"]))

    def test_warmup_skips_lead_ins_in_their_cooldown(self):
        import threading
        from plexcache.cachelog import CacheLog
        from plexcache.plex import MediaFile
        files = [MediaFile(m["path"], m["kbps"], m["size"], "movie") for m in CAT["movies"]]
        log = CacheLog()
        log.note(files[0].path, whole=True)   # Prefetch read this one whole: its lead-in is cached too
        run = WarmupRun(files, {"movie": 2}, 100, 150, threading.Event(),
                        skip=lambda f: log.recent(f.path, False, 12), note=lambda f, hit: log.note(f.path, False, route="ssd", hit=hit))
        r = run.run()
        self.assertEqual((r["files"], r["skipped"]), (4, 1))
        self.assertTrue(all(log.recent(f.path, False, 12) for f in files))   # each one is logged now
        self.assertEqual(WarmupRun(files, {"movie": 2}, 100, 150, threading.Event(),
                                   skip=lambda f: log.recent(f.path, False, 12)).run()["skipped"], 4)


class MappingTests(unittest.TestCase):
    """Library folders Plex reports vs. where this app can find them."""

    def setUp(self):
        from plexcache import mapping
        # Test folders live in the system temp folder (/tmp on Linux, /var/folders on a Mac),
        # which the app normally ignores as a system location.
        self._saved = mapping.SYSTEM_MOUNTS
        mapping.SYSTEM_MOUNTS = tuple(m for m in mapping.SYSTEM_MOUNTS
                                      if m == "/" or not (TMP == m or TMP.startswith(m + "/")))
        self.addCleanup(setattr, mapping, "SYSTEM_MOUNTS", self._saved)
        self.root = tempfile.mkdtemp(dir=TMP)
        # Files Plex sees at /plexsees/Movies/... are mounted here at <root>/films
        films = os.path.join(self.root, "films", "Film (2020)")
        os.makedirs(films)
        open(os.path.join(films, "Film.mkv"), "w").close()
        os.makedirs(os.path.join(self.root, "emptydata"))
        self.mounts = [{"root": "/", "mountpoint": self.root, "fstype": "zfs", "source": "tank/m"},
                       {"root": "/", "mountpoint": "/proc", "fstype": "proc", "source": "proc"}]

    def _app(self, path_map):
        from plexcache.app import App
        app = App(tempfile.mkdtemp(dir=TMP))
        app.settings.update({"general": {"path_map": path_map}})
        app.libraries = [
            {"key": "1", "title": "Movies", "kind": "movie", "excluded": False, "count": 1,
             "locations": ["/plexsees/Movies"], "samples": {"/plexsees/Movies": ["/plexsees/Movies/Film (2020)/Film.mkv"]}},
            {"key": "2", "title": "Music", "kind": "audio", "excluded": False, "count": 1,
             "locations": ["/data/Music"], "samples": {"/data/Music": ["/data/Music/a.flac"]}},
        ]
        return app

    def test_missing_folder_suggests_where_files_are(self):
        from plexcache import mapping
        app = self._app([{"plex": "/data", "local": os.path.join(self.root, "emptydata")}])
        rows = {r["library"]: r for r in mapping.location_report(app, self.mounts)}
        self.assertEqual(rows["Movies"]["status"], "missing")
        self.assertEqual(rows["Movies"]["found_at"], [os.path.join(self.root, "films")])
        self.assertEqual(rows["Music"]["status"], "missing")  # /data is mapped to an empty folder without Music
        self.assertEqual(rows["Music"]["found_at"], [])
        choices = mapping.choices(list(rows.values()), self.mounts)
        self.assertIn(os.path.join(self.root, "films"), choices)
        self.assertNotIn("/proc", choices)

    def test_choosing_the_folder_fixes_it(self):
        from plexcache import mapping
        app = self._app([{"plex": "/plexsees/Movies", "local": os.path.join(self.root, "films")}])
        rows = {r["library"]: r for r in mapping.location_report(app, self.mounts)}
        self.assertEqual(rows["Movies"]["status"], "ok")
        self.assertTrue(rows["Movies"]["mapped"])

    def test_empty_folder(self):
        from plexcache import mapping
        app = self._app([{"plex": "/data/Music", "local": os.path.join(self.root, "emptydata")}])
        rows = {r["library"]: r for r in mapping.location_report(app, self.mounts)}
        self.assertEqual(rows["Music"]["status"], "empty")

    def test_longest_mapping_wins(self):
        app = self._app([{"plex": "/data", "local": "/a"}, {"plex": "/data/Movies", "local": "/b"}])
        self.assertEqual(app.map_path("/data/Movies/x.mkv"), "/b/x.mkv")
        self.assertEqual(app.map_path("/data/TV/x.mkv"), "/a/TV/x.mkv")
        self.assertEqual(app.map_path("/database/x"), "/database/x")

    def test_host_header_parsing(self):
        from plexcache.app import _host_only
        self.assertEqual(_host_only("192.168.50.222:8643"), "192.168.50.222")
        self.assertEqual(_host_only("nas.local"), "nas.local")
        self.assertEqual(_host_only("[fe80::1]:8642"), "fe80::1")
        self.assertIsNone(_host_only("localhost:8642"))
        self.assertIsNone(_host_only("127.0.0.1"))
        self.assertIsNone(_host_only("evil<script>"))


class PrefetchPlanTests(unittest.TestCase):
    """The per-library ladders: each step includes the ones before it, in order."""

    @staticmethod
    def show():
        eps = [{"rating_key": "sp", "season": 0, "episode": 1, "files": [("/s0", 1, 1)]}]
        for se in (1, 2, 3):
            for e in (1, 2, 3):
                eps.append({"rating_key": f"{se}{e}", "season": se, "episode": e, "files": [(f"/{se}{e}", 1, 1)]})
        return eps

    def keys(self, items):
        return [i["rating_key"] for i in items]

    def test_tv_ladder(self):
        from plexcache import prefetch as pf
        steps = pf.tv_steps(self.show(), "12")                     # S01E02
        self.assertEqual(self.keys(pf.plan(steps, "next")), ["13"])
        self.assertEqual(self.keys(pf.plan(steps, "rest")), ["13"])
        self.assertEqual(self.keys(pf.plan(steps, "whole")), ["13", "11"])           # the rest first, then the earlier one
        self.assertEqual(self.keys(pf.plan(steps, "next_group")), ["13", "11", "21", "22", "23"])
        self.assertEqual(self.keys(pf.plan(steps, "all_groups")), ["13", "11", "21", "22", "23", "31", "32", "33"])  # no specials
        lib = [{"rating_key": "", "files": [("/other/show.mkv", 1, 1)]}]
        self.assertEqual(self.keys(pf.plan(steps, "library", lib))[-1], "")
        self.assertEqual(pf.plan(steps, "off"), [])

    def test_season_of_what_plays_next(self):
        from plexcache import prefetch as pf
        # At the last episode of season 1, the next one is S02E01, so "the season" is season 2
        steps = pf.tv_steps(self.show(), "13")
        self.assertEqual(self.keys(pf.plan(steps, "next")), ["21"])
        self.assertEqual(self.keys(pf.plan(steps, "rest")), ["21", "22", "23"])
        self.assertEqual(self.keys(pf.plan(steps, "whole")), ["21", "22", "23"])
        self.assertEqual(self.keys(pf.plan(steps, "next_group")), ["21", "22", "23", "31", "32", "33"])
        # The last episode of the show: nothing comes next
        self.assertEqual(pf.plan(pf.tv_steps(self.show(), "33"), "rest"), [])
        self.assertIsNone(pf.tv_steps(self.show(), "missing"))

    def test_audio_ladders(self):
        from plexcache import prefetch as pf

        def album(name, n):
            return [{"rating_key": f"{name}{i}", "files": [(f"/{name}{i}", 1)], "duration_ms": 1000} for i in range(1, n + 1)]
        a, b, c = album("a", 3), album("b", 2), album("c", 2)
        steps = pf.audio_steps(a, "a2", b, [a, b, c], "books")
        self.assertEqual(self.keys(pf.plan(steps, "next")), ["a3"])
        self.assertEqual(self.keys(pf.plan(steps, "whole")), ["a3", "a1"])
        self.assertEqual(self.keys(pf.plan(steps, "next_group")), ["a3", "a1", "b1", "b2"])
        self.assertEqual(self.keys(pf.plan(steps, "all_groups")), ["a3", "a1", "b1", "b2", "c1", "c2"])
        # The last chapter: the next book is the one in focus
        steps = pf.audio_steps(a, "a3", b, [a, b, c], "books")
        self.assertEqual(self.keys(pf.plan(steps, "rest")), ["b1", "b2"])
        self.assertEqual(self.keys(pf.plan(steps, "next_group")), ["b1", "b2", "c1", "c2"])
        # Music has no next album
        steps = pf.audio_steps(a, "a3", None, [a, b], "music")
        self.assertEqual(self.keys(pf.plan(steps, "rest")), [])
        self.assertEqual(self.keys(pf.plan(steps, "whole")), ["a1", "a2"])
        self.assertNotIn("next_group", [n for n, _ in steps])

    def test_defaults_follow_the_mode_and_fall_back(self):
        from plexcache import cachemode as cm, prefetch as pf
        self.assertEqual(pf.resolve(cm.L2ARC, "2", "start", "default", "tv"), "library")
        self.assertEqual(pf.resolve(cm.RAM, "2", "start", "default", "tv"), "rest")
        self.assertEqual(pf.resolve(cm.L2ARC, "2", "end", "default", "tv"), "rest")
        self.assertEqual(pf.resolve(cm.RAM, "2", "end", "default", "tv"), "next")
        self.assertEqual(pf.resolve(cm.RAM, "2", "start", "all_groups", "tv"), "rest")    # needs an L2ARC
        # RAM keeps one-off reads for hours: only what's needed soon is worth reading into it
        self.assertEqual(pf.resolve(cm.RAM, "2", "start", "next_group", "tv"), "rest")
        self.assertEqual(pf.resolve(cm.RAM, "2", "start", "whole", "tv"), "rest")
        self.assertNotIn("whole", pf.END_LEVELS)       # episodes already watched, in full: never worth it
        # Music on RAM only: no whole files by default; with an L2ARC, as for the rest
        self.assertEqual(pf.resolve(cm.L2ARC, "4", "end", "default", "music"), "rest")
        self.assertEqual(pf.resolve(cm.RAM, "4", "end", "default", "music"), "none")
        self.assertEqual(pf.resolve(cm.RAM, "4", "end", "default", "tv"), "next")
        self.assertEqual(pf.resolve(cm.L2ARC, "4", "end", "next", "music"), "next")       # still a choice
        self.assertEqual(pf.resolve(cm.L2ARC, "3", "end", "default", "books"), "rest")
        self.assertEqual(pf.resolve(cm.L2ARC, "4", "start", "next_group", "music"), "whole")  # not offered for music

    def test_whole_file_choices(self):
        from plexcache import cachemode as cm, prefetch as pf
        self.assertEqual(pf.resolve(cm.RAM, "2", "end", "rest", "tv"), "next")       # needs an L2ARC now
        self.assertEqual(pf.resolve(cm.L2ARC, "2", "end", "rest", "tv"), "rest")
        self.assertEqual(pf.resolve(cm.RAM, "2", "end", "none", "tv"), "none")
        self.assertEqual(pf.plan(pf.tv_steps(self.show(), "12"), "none"), [])
        # From the 0.15 betas: "off" near the end becomes Nothing; warm-up switched off becomes no warm-up
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"nextepisode": {"libraries": {"2": {"end_at": "off", "start": "next"}}}, "warmup": {"enabled": False}}, fh)
        st = config.Settings(d)
        self.assertEqual(st.section("nextepisode")["libraries"]["2"], {"start": "next", "end": "none"})
        self.assertEqual(st.section("warmup")["libraries"], {"*": {"schedule": "none"}})
        self.assertNotIn("enabled", st.section("warmup"))
        # Prefetch's switch, turned off, becomes Don't prefetch and Nothing for every library without choices of its own
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"nextepisode": {"enabled": False, "libraries": {"2": {"start": "next"}}}}, fh)
        st = config.Settings(d)
        self.assertNotIn("enabled", st.section("nextepisode"))
        self.assertEqual(st.section("nextepisode")["libraries"]["*"], {"start": "off", "end": "none"})
        self.assertEqual(pf.library_settings(st.section("nextepisode"), "7")["start"], "off")  # until it's shared out
        self.assertEqual(pf.library_settings(st.section("nextepisode"), "2")["start"], "next")

    def test_l2arc_only_steps_are_background(self):
        from plexcache import prefetch as pf
        steps = pf.tv_steps(self.show(), "12")
        tagged = pf.plan_steps(steps, "library", [{"rating_key": "x", "files": [("/x", 1, 1)]}])
        bg = {it["rating_key"]: pf.background("start", st) for st, it in tagged}
        self.assertFalse(bg["13"])                          # the next episode reads straight away, into RAM
        self.assertTrue(bg["11"] and bg["21"])              # what's needed later goes to the SSD in the background:
        self.assertTrue(bg["31"] and bg["x"])               # earlier episodes, the next season, all seasons, the library
        ends = [(it["rating_key"], pf.background("end", st)) for st, it in pf.plan_steps(pf.tv_steps(self.show(), "11"), "rest")]
        self.assertEqual(ends, [("12", False), ("13", True)])   # in full: the next one into RAM, the rest to the SSD

    def test_report_splits_reads_and_next_episodes(self):
        from plexcache import report
        s = report.summarize([{"what": "read", "cached": 10, "disk": 30},
                              {"what": "read", "cached": 5, "disk": 95, "route": "ssd"},
                              {"what": "next", "ready": True, "full": False, "label": "a"},
                              {"what": "next", "ready": True, "label": "b"},          # older history: ready = in full
                              {"what": "next", "ready": False, "full": False, "label": "c"}])
        self.assertEqual((s["read_cached"], s["read_disk"], s["read_ssd"]), (10, 30, 100))
        self.assertEqual((s["next"], s["next_ready"], s["next_full"]), (3, 2, 1))

    def test_cache_log(self):
        from plexcache.cachelog import CacheLog
        log, now = CacheLog(), 1_000_000.0
        log.note("/a", whole=True, at=now)
        log.note("/b", whole=False, at=now - 2 * 3600)
        self.assertTrue(log.recent("/a", whole=False, cooldown_hours=1, now=now + 60))   # a whole file is a lead-in too
        self.assertTrue(log.recent("/a", whole=True, cooldown_hours=1, now=now + 60))
        self.assertFalse(log.recent("/b", whole=True, cooldown_hours=6, now=now))         # a lead-in isn't a whole file
        self.assertTrue(log.recent("/b", whole=False, cooldown_hours=6, now=now))
        self.assertFalse(log.recent("/b", whole=False, cooldown_hours=1, now=now))        # past its cooldown
        self.assertEqual(log.counts(6, 1, now=now + 60), {"lead_ins": 2, "whole_files": 1})
        # Writes are kept for 6 months, well past any cooldown (they're what re-reads are measured against)
        log.prune(now=now + 60)
        self.assertEqual(log.counts(6, 6, now=now + 60), {"lead_ins": 2, "whole_files": 1})
        log.prune(now=now + 31 * 86400)
        self.assertEqual(log.counts(10 ** 6, 10 ** 6, now=now + 31 * 86400)["lead_ins"], 2)       # still there after a month
        log.prune(now=now + 181 * 86400)
        self.assertEqual(log.counts(10 ** 6, 10 ** 6, now=now + 181 * 86400), {"lead_ins": 0, "whole_files": 0})

    def test_how_long_cached_data_lasts(self):
        from plexcache import cachelog as cl
        d = tempfile.mkdtemp(dir=TMP)
        log, t = cl.CacheLog(os.path.join(d, "cachelog.json")), 1_000_000.0
        log.boot_time = t - 100
        log.note("/a", False, at=t, route="ram", hit=False)                  # first time: from the disks
        log.note("/a", False, at=t + 20 * 60, route="ram", hit=True)         # 20 min later: still in RAM
        log.note("/a", False, at=t + 40 * 60, route="ram", hit=True)         # counted too: 20 min since the last read (a hit touches it)
        log.note("/b", True, at=t, route="ram", hit=False)
        log.note("/b", True, at=t + 5 * 3600, route="ram", hit=False)        # 5 h later: gone
        log.note("/c", False, at=t - 1000, route="ram", hit=False)           # written before the last boot:
        log.note("/c", False, at=t + 600, route="ram", hit=False)            # not counted (a reboot empties RAM)
        log.note("/s", False, at=t, route="ssd", hit=False)
        log.note("/s", False, at=t + 3600, route="ssd", hit=True)            # SSD: every re-read counts
        log.note("/s", False, at=t + 2 * 3600, route="ssd", hit=True)
        sv = log.survival()
        ram, ssd = sv["routes"]["ram"], sv["routes"]["ssd"]
        self.assertEqual(ram["first"], 3)
        kept = {r["upto"]: (r["kept"], r["gone"]) for r in ram["rows"] if r["kept"] + r["gone"]}
        self.assertEqual(kept, {30 * 60: (2, 0), 6 * 3600: (0, 1)})
        self.assertEqual(sum(r["kept"] for r in ssd["rows"]), 2)
        # A whole file holds its lead-in: a lead-in read is measured against the later write
        log.note("/w", True, at=t, route="ram", hit=False)
        self.assertEqual(log.last("/w", False), t)
        # Kept across restarts of the app
        sv = log.survival()
        log.save()
        again = cl.CacheLog(os.path.join(d, "cachelog.json"))
        self.assertEqual(again.survival()["routes"], sv["routes"])
        self.assertEqual(again.last("/w", True), t)
        again.reset_stats()
        self.assertEqual(again.survival()["routes"]["ram"]["first"], 0)
        self.assertEqual(again.last("/w", True), t)                         # the history stays
        # Suggested cooldown: the longest age still cached ≥98% of the time, with ≥3 reads at each age
        rows = [{"upto": 600, "kept": 20, "gone": 0}, {"upto": 1800, "kept": 2, "gone": 0},     # (too few: skipped)
                {"upto": 3600, "kept": 50, "gone": 1}, {"upto": 7200, "kept": 19, "gone": 1}, {"upto": 14400, "kept": 30, "gone": 0}]
        self.assertEqual(cl.suggest(rows), 3600)

    def test_cached_data_lasts_per_library(self):
        from plexcache import cachelog as cl
        d = tempfile.mkdtemp(dir=TMP)
        log, t = cl.CacheLog(os.path.join(d, "cachelog.json")), 1_000_000.0
        log.boot_time = t - 100
        log.note("/m1", False, at=t, route="ram", hit=False, library="1")         # first time, a miss
        log.note("/m1", False, at=t + 20 * 60, route="ram", hit=True, library="1")  # 20 min later: hit
        log.note("/m2", False, at=t, route="ram", hit=True, library="1")          # first time, already cached
        log.note("/m2", False, at=t + 5 * 3600, route="ram", hit=False, library="1")  # 5 h later: gone
        log.note("/t1", False, at=t, route="ssd", hit=False, library="2")
        log.note("/t1", False, at=t + 3 * 86400, route="ssd", hit=False, library="2")  # 3 days later: gone
        sv = log.survival({"1": "Movies", "2": "TV Shows"})
        self.assertEqual(len(sv["fine"]), len(cl.FINE))
        ram = {lib["name"]: lib for lib in sv["routes"]["ram"]["libs"]}
        self.assertEqual(list(ram), ["Movies", "TV Shows"])                          # TV Shows: nothing in RAM, still listed
        self.assertEqual(ram["TV Shows"]["first"], [0, 0])
        self.assertEqual(ram["Movies"]["first"], [1, 1])                           # [hit, miss]
        self.assertEqual(sum(h for h, _ in ram["Movies"]["fine"]), 1)
        self.assertEqual(ram["Movies"]["fine"][cl._bucket(cl.FINE, 20 * 60)], [1, 0])
        self.assertEqual(sum(r["kept"] for r in ram["Movies"]["rows"]), 1)
        tv = sv["routes"]["ssd"]["libs"][1]
        self.assertEqual((tv["key"], tv["first"]), ("2", [0, 1]))
        self.assertEqual(tv["fine"][cl._bucket(cl.FINE, 3 * 86400)], [0, 1])
        # Every library is listed, even with nothing measured yet, and says whether it's cached at all
        sv2 = log.survival({"1": "Movies", "2": "TV Shows", "3": "Music"}, {"3": False})
        ram2 = {lib["name"]: lib for lib in sv2["routes"]["ram"]["libs"]}
        self.assertEqual(list(ram2), ["Movies", "TV Shows", "Music"])                 # in Plex's order
        self.assertEqual((ram2["Music"]["cached"], ram2["Music"]["first"], ram2["TV Shows"]["cached"]), (False, [0, 0], True))
        # Kept across restarts; a log from before libraries were noted still loads
        log.save()
        again = cl.CacheLog(os.path.join(d, "cachelog.json"))
        self.assertEqual(again.survival({"1": "Movies", "2": "TV Shows"})["routes"], sv["routes"])
        with open(os.path.join(d, "old.json"), "w", encoding="utf-8") as fh:
            json.dump({"stats": {"ram": {"first": 3, "ages": [[1, 0]] + [[0, 0]] * (len(cl.AGES) - 1)}}}, fh)
        old = cl.CacheLog(os.path.join(d, "old.json")).survival()["routes"]["ram"]
        self.assertEqual((old["first"], old["libs"], old["rows"][0]["kept"]), (3, [], 1))
        # A log from when the history was 30 days (one age bucket fewer, other finer ages) still loads
        with open(os.path.join(d, "v30.json"), "w", encoding="utf-8") as fh:
            json.dump({"stats": {"ram": {"first": 2, "ages": [[1, 0]] * (len(cl.AGES) - 1),
                                         "libs": {"1": {"first": [1, 1], "ages": [[0, 1]] * (len(cl.AGES) - 1), "fine": [[5, 5]] * 64}}}}}, fh)
        v30 = cl.CacheLog(os.path.join(d, "v30.json")).survival({"1": "Movies"})["routes"]["ram"]
        self.assertEqual((v30["rows"][-2]["kept"], v30["rows"][-1]["kept"]), (1, 0))
        self.assertEqual((v30["libs"][0]["first"], sum(h for h, _ in v30["libs"][0]["fine"])), ([1, 1], 0))
        # Whether each library's pool has an SSD cache (the cache is per dataset, so they can differ)
        l2 = log.survival({"1": "Movies", "2": "TV Shows"}, None, {"1": False, "2": True})["routes"]["ssd"]["libs"]
        self.assertEqual([lib["l2arc"] for lib in l2], [False, True])
        self.assertEqual(cl.HISTORY, 180 * 86400)
        self.assertIsNone(cl.suggest([{"upto": 600, "kept": 2, "gone": 0}]))      # fewer than 3 reads: no suggestion
        # Cooldowns from before 0.15 were in hours; now minutes
        d2 = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d2, "settings.json"), "w") as fh:
            json.dump({"general": {"ram_cooldown_lead_in_hours": 1, "l2arc_cooldown_whole_hours": 5}}, fh)
        g = config.Settings(d2).section("general")
        self.assertEqual((g["ram_cooldown_lead_in_minutes"], g["l2arc_cooldown_whole_minutes"]), (60, 360))
        self.assertNotIn("ram_cooldown_lead_in_hours", g)

    def test_automatic_cooldown_follows_the_nas(self):
        from plexcache import cachelog as cl
        log, t, H = cl.CacheLog(), 1_000_000.0, 3600
        log.boot_time = 0
        fallback = 2 * H

        def reads(start, n, age, hit, lib="1"):
            for i in range(n):
                p = f"/{lib}/{start}/{age}/{i}"
                log.note(p, False, at=start, route="ram", hit=False, library=lib)
                log.note(p, False, at=start + age, route="ram", hit=hit, library=lib)
        self.assertEqual(log.auto_cooldown("ram", "1", fallback, now=t), fallback)       # nothing yet: the default
        reads(t, 10, 2.5 * H, True)                                                       # still cached at 2.5 h ...
        reads(t, 10, 5 * H, False)                                                        # ... gone by 5 h
        self.assertEqual(log.fully_cached("ram", "1"), {"age": 2.5 * H, "bound": False, "n": 20, "miss": 5 * H})
        self.assertEqual(log.auto_cooldown("ram", "1", fallback, now=t + 6 * H), 2.5 * H)
        self.assertEqual(log.auto_cooldown("ram", "2", fallback, now=t + 6 * H), 2.5 * H)  # a library without its own: every library's
        # It goes up when data lasts longer (older reads have left the week's window)...
        later = t + 30 * 86400
        reads(later, 10, 10 * H, True)
        self.assertEqual(log.auto_cooldown("ram", "1", fallback, now=later + 11 * H), 10 * H)
        self.assertTrue(log.fully_cached("ram", "1", later)["bound"])                    # no misses: at least 10 h
        # ...and back down when it doesn't: a miss at the youngest age measured gives the floor
        reads(later + 60 * 86400, 10, 10 * H, False)
        self.assertEqual(log.auto_cooldown("ram", "1", fallback, now=later + 60 * 86400 + 11 * H), cl.AUTO_FLOOR)
        # Hour by hour (and day by day), for every library and each one apart: [age, no misses, re-reads, youngest miss]
        end = later + 60 * 86400 + 11 * H
        hours = log.fully_cached_series("ram", ["1", "2"], "hour", 24, now=end)
        self.assertEqual(len(hours["starts"]), 24)
        self.assertEqual([p for p in hours["libs"]["1"] if p], [[0, 0, 10, 10 * H]])     # the misses, in their hour
        self.assertEqual(hours["libs"]["2"], [None] * 24)
        days = log.fully_cached_series("ram", ["1"], "day", 3, now=end)
        self.assertEqual(sum(1 for p in days["all"] if p), 1)
        # How likely data of a given age is still cached: the library's own share, else every library's
        self.assertAlmostEqual(log.still_cached("ram", "1", 2.5 * H), 1.0)
        self.assertEqual(log.still_cached("ram", "1", 5 * H), 0.0)
        self.assertIsNone(log.still_cached("ssd", "1", H))
        # Saved and loaded with the rest
        d = tempfile.mkdtemp(dir=TMP)
        log.path = os.path.join(d, "c.json")
        log.save(force=True)
        again = cl.CacheLog(log.path)
        self.assertEqual(again.reads, log.reads)                                           # each re-read is kept
        self.assertEqual(again.auto_cooldown("ram", "1", fallback, now=later + 60 * 86400 + 11 * H), cl.AUTO_FLOOR)
        self.assertEqual(again.library_of("/1/" + str(t) + "/" + str(2.5 * H) + "/0"), "1")
        self.assertIn("reads", again.export())
        again.reset_stats()
        self.assertEqual(again.reads, [])

    def test_library_cached_over_time(self):
        from plexcache import coverage as cv
        d = tempfile.mkdtemp(dir=TMP)
        c, now, H = cv.Coverage(os.path.join(d, "coverage.json")), 1_000_000.0 * 3600, 3600
        files = [("/a", 100, "1"), ("/b", 300, "1"), ("/c", 50, "2"), ("/d", 50, "2")]
        self.assertTrue(c.due(now))
        c.record(files, {"/a": now - 600, "/b": now - 3 * 86400, "/c": now - 200 * 86400}, now=now)
        self.assertFalse(c.due(now + 60))                                               # once an hour
        self.assertTrue(c.due(now + H))
        s = c.series({"1": "Movies", "2": "TV"})
        # Movies: /a within the hour (and everything after), /b within 7 days; by files and by bytes (tenths of %)
        self.assertEqual(s["libs"]["1"][0], [[500, 500, 1000, 1000, 1000], [250, 250, 1000, 1000, 1000]])
        self.assertEqual(s["libs"]["2"][0], [[0, 0, 0, 0, 0], [0, 0, 0, 0, 0]])           # /c was more than 6 months ago
        self.assertEqual(s["all"][0][0], [250, 250, 500, 500, 500])
        # Only the hourly result is kept, for 6 months, across restarts
        again = cv.Coverage(c.path)
        self.assertEqual(len(again.hours), 1)
        again.record(files, {}, now=now + 181 * 86400)
        self.assertEqual(len(again.hours), 1)

    def test_every_setting_has_a_back_to_default_button(self):
        # Every setting the pages offer has a ↺ that puts it back to its default, unless it has no
        # meaningful default (where your servers are, secrets, which libraries, mappings, speeds,
        # and the big feature switches). Checked in the page's code, so a new field can't miss it.
        import re
        js = open(os.path.join(ROOT, "plexcache", "web", "app.js"), encoding="utf-8").read()
        no_default = {"plex.url", "truenas.url", "mqtt.host", "mqtt.username", "mqtt.enabled"}

        def body(name):  # a field builder's source
            m = re.search(r"\n  function " + name + r"\(.*?\n  }\n", js, re.S)
            self.assertIsNotNone(m, name)
            return m.group(0)
        for builder in ("range", "ramSlider", "select", "number", "checkbox", "textInput"):
            self.assertIn("revertBtn(sec, key", body(builder), builder)
        self.assertIn("NO_REVERT[sec + \".\" + key]", body("textInput"))
        self.assertIn("data-pick-revert", body("pickRevert"))                        # choice lists: back to Default...
        # ...beside their name, for every choice list on the pages
        for m in re.finditer(r'pickHtml\(("[a-z]+:" \+ [^,]+), ', js):
            self.assertIn("pickRevert(" + m.group(1) + ",", js, f"the choice list {m.group(1)} has no ↺")
        builders = {n: body(n) for n in ("range", "ramSlider", "select", "number", "checkbox", "textInput")}
        # Hand-built fields (outside the builders) carry their own ↺
        for m in re.finditer(r'data-(?:set|check)="([a-z_]+\.[a-z_]+)"', js):
            key = m.group(1)
            if key in no_default or any(m.group(0) in b for b in builders.values()):
                continue
            line = js[js.rfind("\n", 0, m.start()) + 1: js.find("\n", m.end())]
            nxt = "\n".join(js[m.end():].split("\n")[:5])                           # (the field, as built over a few lines)
            sec, k = key.split(".")
            self.assertTrue(f'revertBtn("{sec}", "{k}"' in line + nxt, f"{key} has no ↺ back-to-default button")
        # Controls with their own save handler (a library's optimizer choices, the kinds converted, a
        # library's warm-up seconds, the second server's pairs…) carry their own ↺ too: every handler
        # keyed on el.dataset.<name> has a data-<name>-revert button. Not: a what-if slider (est), where
        # your folders are (map) and measured disk speeds (speed) — they have no default.
        handled = set(re.findall(r"el\.dataset\.([a-z0-9]+) [!=]== undefined", js))
        self.assertTrue({"optlib", "convtype", "libsec", "p2lib"} <= handled, handled)       # (the search finds them)
        for name in sorted(handled - {"est", "map", "speed"}):
            self.assertIn(f"data-{name}-revert", js, f"the {name} controls have no ↺ back-to-default button")
        # ...and everything the builders are called with has a default the page is sent
        defaults = config.public_defaults()
        calls = re.findall(r'\b(?:range|ramSlider|select|number|checkbox|textInput)\("([a-z]+)", "([a-z_]+)"', js)
        self.assertGreater(len(calls), 20)                                            # (the search finds them)
        for sec, k in calls:
            if f"{sec}.{k}" in no_default:
                continue
            self.assertIn(k, defaults.get(sec, {}), f"{sec}.{k} has no default for its ↺ button")
        self.assertNotIn("password", str(defaults))                                    # (secrets never are)

    def test_library_kinds(self):
        from plexcache import prefetch as pf
        self.assertEqual(pf.library_kind({"kind": "tv", "title": "TV"}), "tv")
        self.assertEqual(pf.library_kind({"kind": "audio", "title": "Audiobooks"}), "books")
        self.assertEqual(pf.library_kind({"kind": "audio", "title": "Music"}), "music")
        self.assertEqual(pf.library_kind({"kind": "movie", "title": "Movies"}), "movies")
        self.assertIsNone(pf.library_kind({"kind": None, "title": "Photos"}))
        self.assertEqual(pf.levels_for("movies"), ("off", "next", "rest", "whole", "library"))
        self.assertNotIn("next_group", pf.levels_for("music"))

    def test_old_settings_are_migrated(self):
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"nextepisode": {"start_at": "after_nowplaying",
                                       "library_modes": {"2": "next", "3": "album", "4": "off", "5": "artist", "6": "rest"}}}, fh)
        s = config.Settings(d)
        ne = s.section("nextepisode")
        self.assertNotIn("start_at", ne)
        self.assertNotIn("library_modes", ne)
        self.assertEqual(ne["libraries"], {"3": {"end": "rest"}, "4": {"start": "off", "end": "none"},
                                           "5": {"start": "all_groups"}, "6": {"end": "rest"}})
        s.update({"nextepisode": {"libraries": {"2": {"start": "default", "end": "rest", "end_at": "80"}}}})
        self.assertEqual(s.section("nextepisode")["libraries"]["2"], {"end": "rest", "end_at": "80"})
        for bad in ({"2": {"end": "library"}}, {"2": {"end_at": "50"}}, {"2": {"when": "now"}}, {"../2": {}}, ["off"]):
            with self.assertRaises(config.SettingsError):
                s.update({"nextepisode": {"libraries": bad}})


class SchedulerTests(unittest.TestCase):
    def test_daily_and_weekly(self):
        w = dict(config.DEFAULTS["warmup"], schedule="daily", time="03:00")
        now = dt.datetime(2026, 10, 12, 18, 0)
        self.assertEqual(scheduler.next_run(w, now, None), dt.datetime(2026, 10, 13, 3, 0))
        self.assertTrue(scheduler.due(w, dt.datetime(2026, 10, 13, 3, 0, 20), None, dt.datetime(2026, 10, 13, 2, 59, 50)))
        self.assertFalse(scheduler.due(w, dt.datetime(2026, 10, 13, 3, 1), None, dt.datetime(2026, 10, 13, 3, 0, 30)))
        w = dict(w, schedule="weekly", weekday=6)
        self.assertEqual(scheduler.next_run(w, now, None), dt.datetime(2026, 10, 18, 3, 0))
        # Twice weekly: the weekday (Sunday) and three days later (Wednesday)
        self.assertEqual(scheduler.next_run(w, now, None, "twice_weekly"), dt.datetime(2026, 10, 14, 3, 0))
        self.assertEqual(scheduler.next_run(w, dt.datetime(2026, 10, 14, 4, 0), None, "twice_weekly"), dt.datetime(2026, 10, 18, 3, 0))
        # Twice monthly: the 1st and 15th, at the time
        self.assertEqual(scheduler.next_run(w, now, None, "twice_monthly"), dt.datetime(2026, 10, 15, 3, 0))
        self.assertEqual(scheduler.next_run(w, dt.datetime(2026, 12, 15, 5, 0), None, "twice_monthly"), dt.datetime(2027, 1, 1, 3, 0))
        self.assertTrue(scheduler.due(w, dt.datetime(2026, 11, 1, 3, 0, 10), None, dt.datetime(2026, 11, 1, 2, 59, 50), "twice_monthly"))
        for untimed in ("none", "boot"):
            self.assertIsNone(scheduler.next_run(w, now, None, untimed))


class PreloadPlanTests(unittest.TestCase):
    """Start timing, read order and RAM limits for Now-playing / Next-episode."""

    CFG = {"delay_sec": 0, "buffer_direct_sec": 10, "buffer_transcode_sec": 60, "min_delay_sec": 5}

    def test_start_delay(self):
        from plexcache.playback import start_delay
        # 60 s of an 80 Mbps transcode = 600 MB; at 100 MB/s that's 6 s
        self.assertEqual(start_delay(self.CFG, 80000, True, 100), 6.0)
        # 10 s of 10 Mbps direct play reads in no time, so the minimum applies
        self.assertEqual(start_delay(self.CFG, 10000, False, 235), 5.0)
        # no bitrate from Plex: assume a 25 Mbps 4K stream
        self.assertEqual(start_delay(dict(self.CFG, min_delay_sec=0), 0, True, 10), 18.8)
        # a fixed delay wins
        self.assertEqual(start_delay(dict(self.CFG, delay_sec=30), 80000, True, 100), 30.0)

    def test_read_order(self):
        from plexcache.playback import plan_ranges
        # whole file fits: from the play position to the end, then the beginning
        self.assertEqual(plan_ranges(100, 60, 100, 1000, wrap=True), [(60, 40), (0, 60)])
        # limit too small for all of it: forward first
        self.assertEqual(plan_ranges(100, 60, 100, 50, wrap=True), [(60, 40), (0, 10)])
        self.assertEqual(plan_ranges(100, 60, 100, 30, wrap=True), [(60, 30)])
        # without wrapping (Next-episode) it only reads forward
        self.assertEqual(plan_ranges(100, 60, 20, 1000, wrap=False), [(60, 20)])
        self.assertEqual(plan_ranges(100, 60, 100, 0, wrap=True), [])

    def test_skip_restarts(self):
        from plexcache.playback import SKIP_AHEAD, Job, needs_restart
        GB = 1024 ** 3
        j = Job("nowplaying", "x", "/f", [(10 * GB, 5 * GB)], 40 * GB, state="reading", read=GB)
        self.assertFalse(needs_restart(j, 11 * GB))                    # just ahead of the reader
        self.assertTrue(needs_restart(j, 11 * GB + SKIP_AHEAD + GB))   # far ahead: start over there
        self.assertTrue(needs_restart(j, 30 * GB))                     # outside what it would read
        j.state = "done"
        self.assertFalse(needs_restart(j, 14 * GB))
        self.assertTrue(needs_restart(j, 2 * GB))

    def test_ram_limits(self):
        app = App(tempfile.mkdtemp(dir=TMP))
        b = app.ram_budget()
        GiB = 1024 ** 3
        self.assertEqual(b["cache_max"], 56 * GiB)                     # c_max from the fake arcstats
        usable = 56 * GiB - 4 * 10 ** 9
        self.assertEqual(b["usable"], usable)                          # minus the 4 GB reserve
        self.assertEqual(b["nowplaying"], int(usable * 0.60))
        self.assertEqual(b["nextepisode"], int(usable * 0.20))
        with self.assertRaises(config.SettingsError):
            app.settings.update({"nowplaying": {"ram_percent": 90}, "nextepisode": {"ram_percent": 20}})
        # Apps using RAM shrink what the ZFS cache can use, whatever its maximum says
        real = zfs.arcstats
        zfs.arcstats = lambda: dict(real(), size=19 * GiB, memory_free_bytes=3 * GiB, c_max=32 * GiB)
        try:
            b = app.ram_budget()
        finally:
            zfs.arcstats = real
        self.assertEqual(b["available"], 22 * GiB)
        self.assertEqual(b["usable"], 22 * GiB - 4 * 10 ** 9)
        from plexcache import checks
        app.settings.update({"general": {"ram_reserve_gb": 61}, "nowplaying": {"enabled": True}})
        self.assertEqual(app.ram_budget()["usable"], 0)
        z = {i["title"]: i for i in checks._ram_check(app)}
        self.assertEqual(z["Not enough RAM for Now Playing Cache and Prefetch"]["status"], "fail")
        recs = {r["feature"]: r["verdict"] for r in checks.recommendations(app)}
        self.assertEqual(recs["nowplaying"], "avoid")
        app.settings.update({"general": {"ram_reserve_gb": 57}})       # leaves about 3 GB: under 2 GB for Now-playing
        self.assertEqual(checks._ram_check(app)[0]["title"], "RAM is tight for Now Playing Cache and Prefetch")

    def test_retired_settings_are_dropped(self):
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"nowplaying": {"mode": "minutes", "minutes": 10, "size_gb": 10, "min_kbps": 5000}, "nextepisode": {"max_gb": 2}}, fh)
        s = config.Settings(d)
        self.assertNotIn("mode", s.section("nowplaying"))
        self.assertNotIn("minutes", s.section("nowplaying"))
        self.assertNotIn("min_kbps", s.section("nowplaying"))
        self.assertNotIn("size_gb", s.section("nowplaying"))
        self.assertNotIn("max_gb", s.section("nextepisode"))


class LayoutTests(unittest.TestCase):
    """Finding the index and other parts a player reads at the end of a file."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)

    def test_mkv(self):
        from plexcache.layout import tail_ranges
        size = 40 * MiB
        for kw in ({}, {"second_seekhead": True}, {"tags_size": 5000}, {"second_seekhead": True, "tags_size": 5000}):
            p = os.path.join(self.dir, "a.mkv")
            m = fakes.make_mkv(p, size, cues_size=60_000, **kw)
            ranges, info = tail_ranges(p, size, 4 * MiB)
            self.assertEqual(info["format"], "mkv", kw)
            self.assertEqual(info["parts"]["index"], m["cues_len"], kw)
            covered = lambda off, n: any(o <= off and off + n <= o + l for o, l in ranges)
            self.assertTrue(covered(m["cues_at"], m["cues_len"]), kw)
            if kw.get("tags_size"):
                self.assertTrue(covered(m["tags_at"], m["tags_len"]), kw)
            self.assertLess(info["bytes"], 100_000, kw)
        # an index inside what's read from the start anyway isn't read twice
        self.assertEqual(tail_ranges(p, size, size - 10)[0], [])
        # what the always-cached blocks hold: every byte of them is video, metadata or unused room;
        # the end block (metadata, compressible) is stored compressed
        from plexcache import filemap
        x = filemap.cache_mix(p, filemap.describe(p), MiB)
        self.assertEqual(x["video"] + x["meta"] + x["unused"], x["blocks"] * MiB)
        self.assertLessEqual(x["unused_disk"], x["unused"])
        self.assertLessEqual(x["meta_disk"], x["meta"])
        self.assertEqual(x["video_disk"], x["video"])
        self.assertIsNone(filemap.cache_mix(p, {"size": size}, MiB))      # no layout: nothing to say

    def test_metadata_and_least_start(self):
        """What's cached from each file: the metadata at its end without the cover art (for a file
        the optimizer laid out, one stretch from the first non-image attachment to the end), and
        at its start whole records through at least 60% of a record of video."""
        from plexcache import layout
        # the least of the start: a 1.2 MB poster in front, 1 MB records: 2 (the second holds 0.8 MB of video)
        M1 = 1000 * 1000
        self.assertEqual(layout.min_head(int(1.2 * M1), M1) // M1, 2)
        self.assertEqual(layout.min_head(int(1.5 * M1), M1) // M1, 3)            # only 0.5 MB of video in the second
        self.assertEqual(layout.min_head(int(0.3 * M1), M1) // M1, 1)
        self.assertEqual(layout.min_head(None, None), MiB)                        # unknown: a MiB's 60%, in whole MiB
        self.assertEqual(layout.min_head(0, M1, size=500), 500)                   # never past the end
        # an optimized file: images, then fonts, the index and tags at the end — cached from the first font on
        p = os.path.join(self.dir, "opt.mkv")
        poster = ("cover.jpg", "image/jpeg", os.urandom(200_000))
        build_mkv(p, tail=("Attachments", "Cues", "Tags"), tail_atts=[poster, FONT], pgs_algo=0, cluster=os.urandom(300_000))
        size = os.path.getsize(p)
        with mock.patch.object(layout, "CACHE_IMAGES", False):                    # cover art left out
            ranges, info = layout.tail_ranges(p, size, 0)
        data = open(p, "rb").read()
        font_at = data.index(FONT[2])                                          # (its data; its entry starts just before)
        cover_at = data.index(poster[2])
        tail = [(o, n) for o, n in ranges if o >= info["video_start"]]
        self.assertEqual(len(tail), 1)
        self.assertTrue(cover_at + len(poster[2]) <= tail[0][0] <= font_at)      # past the last image…
        self.assertEqual(tail[0][0] + tail[0][1], size)                          # …to the end
        self.assertGreater(info["parts"]["images_left_out"], len(poster[2]))
        self.assertEqual(data[info["video_start"]:info["video_start"] + 4], bytes.fromhex("1F43B675"))   # a Cluster
        # attachments in front of the video: inside the start; the poster's there, so the start grows past it
        p2 = os.path.join(self.dir, "front.mkv")
        build_mkv(p2, front_atts=[("poster.jpg", "image/jpeg", os.urandom(300_000)), FONT])
        info2 = layout.tail_ranges(p2, os.path.getsize(p2), 0)[1]
        self.assertGreater(info2["video_start"], 300_000)
        self.assertEqual(layout.min_head(info2["video_start"], 128 * 1024), 3 * 128 * 1024)   # ~305K in front + 60% of 128K
        # cover art counted as metadata (the default: players read it when they open a file): all the attachments
        whole = layout.tail_ranges(p, size, 0)[0]
        self.assertLessEqual(whole[-1][0], cover_at)

    def test_file_map(self):
        """The Library Health page's picture of a file: every part with its place and size, the tracks,
        the attachments (images apart), kept until the file changes; and what caching takes from it."""
        from plexcache import filemap
        p = os.path.join(self.dir, "f.mkv")
        poster = ("cover.jpg", "image/jpeg", os.urandom(200_000))
        build_mkv(p, tail=("Attachments", "Cues", "Tags"), tail_atts=[poster, FONT], pgs_algo=0, cluster=os.urandom(300_000))
        m = filemap.describe(p)
        size = os.path.getsize(p)
        self.assertEqual(m["format"], "mkv")
        names = [x["part"] for x in m["parts"]]
        self.assertEqual(names[-4:], ["video", "attachments", "index", "tags"])
        self.assertEqual(sum(x["size"] for x in m["parts"]), size)                  # every byte accounted for
        self.assertEqual([(a["name"], a["image"]) for a in m["attachments"]], [("cover.jpg", True), ("font.ttf", False)])
        self.assertEqual([(t["type"], t["codec"], t["compression"]) for t in m["tracks"]],
                         [("video", "V_MPEG4/ISO/AVC", None), ("subtitles", "S_HDMV/PGS", "zlib")])
        self.assertEqual(m["video_start"], next(x["pos"] for x in m["parts"] if x["part"] == "video"))
        # what caching takes: the start through 60% of a record of video, and the metadata at the end, in whole records
        plan = filemap.cache_plan(m, size, 8000, 0, 64 * 1024, "movie")
        self.assertFalse(plan["estimated"])
        self.assertEqual(plan["start"], 64 * 1024)                                  # one 64K record: 60% of it video
        self.assertEqual(plan["end"][-1][0] + plan["end"][-1][1], size)
        guess = filemap.cache_plan(None, 10 ** 9, 8000, 10, 1024 * 1024, "movie")
        self.assertTrue(guess["estimated"])
        self.assertEqual(guess["start"], 10 * 8000 * 125 // MiB * MiB + MiB)        # the lead-in, in whole records
        # the blocks always cached, each with what's in it — the last one's unused rest (zeros) too
        rec = 64 * 1024
        bm = filemap.block_map(m, size, plan, rec)
        self.assertEqual([b["n"] for b in bm["start"]], [1])
        self.assertEqual(sum(p["bytes"] for p in bm["start"][0]["pieces"]), rec)
        self.assertIn("video", [p["kind"] for p in bm["start"][0]["pieces"]])
        last = bm["end"][-1]
        self.assertEqual(last["pieces"][-1]["kind"], "empty")
        self.assertEqual(last["data"] + last["pieces"][-1]["bytes"], rec)
        kinds = {p["kind"] for b in bm["end"] for p in b["pieces"]}
        self.assertTrue({"image", "font", "index", "tags"} <= kinds)
        self.assertEqual(bm["total_blocks"], -(-size // rec))
        self.assertIsNone(filemap.block_map(None, size, plan, rec))
        # a file whose layout isn't read (a format the inspector doesn't open) is still one block or more: shown as one piece
        small = filemap.block_map({"format": "stream", "size": 7 * 1024 * 1024, "parts": [], "tracks": []}, 7 * 1024 * 1024, plan, rec)
        self.assertEqual((len(small["start"]), small["total_blocks"], small["streams"]), (1, 7 * 1024 * 1024 // rec + (1 if (7 * 1024 * 1024) % rec else 0), ["audio"]))
        self.assertEqual([p["kind"] for p in small["start"][0]["pieces"]][:1], ["other"])
        tiny = filemap.block_map({"format": "stream", "size": 10000, "parts": [], "tracks": []}, 10000, {"floor": rec, "start": 40 * rec}, rec)
        self.assertEqual((len(tiny["start"]), tiny["lead_blocks"], tiny["total_blocks"]), (1, 0, 1))  # (a lead-in longer than the file: no blocks it hasn't got)
        # how ZFS might store them: the last block's zeros compress away; random data (video) doesn't
        est = filemap.estimate_blocks(p, [1, last["n"], 999999], rec, size)
        self.assertEqual([e["n"] for e in est], [1, last["n"]])                     # (past the end: none)
        self.assertEqual(est[0]["stored"], rec)                                     # mostly random: kept as it is
        self.assertLess(est[1]["stored"], rec)
        # an MP4: its boxes; something else: its streams, whole
        q = os.path.join(self.dir, "f.mp4")
        fakes.make_mp4(q, 40 * MiB, moov_size=300_000)
        self.assertEqual([x["part"] for x in filemap.describe(q)["parts"]], ["header", "video", "index"])
        r = os.path.join(self.dir, "f.avi")
        with open(r, "wb") as fh:
            fh.write(b"RIFF" + b"\0" * 100)
        self.assertEqual((filemap.describe(r)["format"], filemap.describe(r)["parts"]), ("stream", [{"part": "video", "pos": 0, "size": 104}]))
        # kept with the file's identity: read again only once it changes
        maps = filemap.FileMaps(self.dir)
        first = maps.read(p)
        with mock.patch.object(filemap, "describe", mock.Mock(side_effect=AssertionError("read again"))):
            self.assertIs(maps.read(p), first)
        self.assertIsNot(maps.read(p, force=True), first)                           # inspected again on request
        self.assertEqual(maps.current([p, q, os.path.join(self.dir, "gone.mkv")]), 1)   # known and unchanged
        with open(p, "ab") as fh:
            fh.write(b"x")
        self.assertIsNone(maps.get(p, os.stat(p)))                                   # changed: not current
        maps.read(p)
        maps.save()
        again = filemap.FileMaps(self.dir)
        self.assertEqual(again.known([p, q]), 1)
        again.forget_missing({q})
        self.assertEqual(again.known([p]), 0)

    def test_codec_names_follow_mkvtoolnix(self):
        """Tracks are named the way MKVToolNix names them (dev/codecs.py makes the list from its source);
        an ID it doesn't know stays as it is. The list's patterns all work in Python."""
        import importlib.util
        from plexcache import codec_names, filemap
        for name, pattern in codec_names.CODECS:
            re.compile(pattern)
        self.assertEqual([filemap.codec_name(x) for x in ("V_MPEGH/ISO/HEVC", "v_mpeg4/iso/avc", "A_AAC", "S_TEXT/UTF8", "S_VOBSUB", "S_HDMV/PGS", "X_NEW")],
                         ["HEVC/H.265/MPEG-H", "AVC/H.264/MPEG-4p10", "AAC", "SubRip/SRT", "VobSub", "HDMV PGS", ""])
        spec = importlib.util.spec_from_file_location("codecs_tool", os.path.join(ROOT, "dev", "codecs.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        cpp = ('s_codecs.emplace_back("AAC", type_e::A_AAC, track_audio, "mp4a|aac.|A_AAC.*");\n'
               's_codecs.emplace_back("Odd", type_e::V_X, track_video, "", fourcc_c{1u});\n'
               's_codecs.emplace_back("HDMV PGS", type_e::S_HDMV_PGS, track_subtitle, MKV_S_HDMV_PGS);\n'
               's_codecs.emplace_back("Dots", type_e::V_D, track_video, "a\\\\.b");\n')
        header = 'constexpr auto MKV_S_HDMV_PGS = "S_HDMV/PGS";'
        self.assertEqual(tool.parse(cpp, header), [("AAC", "mp4a|aac.|A_AAC.*"), ("HDMV PGS", "S_HDMV/PGS"), ("Dots", "a\\.b")])
        # the image build: without mkvmerge, or offline, the committed names stay as they are
        before = open(tool.OUT, encoding="utf-8").read()
        said = io.StringIO()  # (what the build log says: kept here, not in the test run's output)
        with contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            with mock.patch.object(tool.subprocess if hasattr(tool, "subprocess") else __import__("subprocess"), "run",
                                   mock.Mock(side_effect=FileNotFoundError("mkvmerge"))):
                self.assertEqual(tool.main(["codecs.py", "--installed"]), 0)
            with mock.patch.object(tool.urllib.request, "urlopen", mock.Mock(side_effect=OSError("offline"))):
                self.assertEqual(tool.main(["codecs.py", "102.0"]), 0)
        self.assertIn("mkvmerge isn't installed", said.getvalue())
        self.assertIn("offline", said.getvalue())
        self.assertEqual(open(tool.OUT, encoding="utf-8").read(), before)

    def test_cover_art_compression_is_told_apart(self):
        """A JPEG still using the standard Huffman tables hasn't been compressed losslessly; one with its
        own (or progressive) has; anything else isn't known. Read while inspecting, from the picture's
        first bytes; maps read before this was looked at are read again."""
        from plexcache import filemap
        seg = lambda m, body: b"\xff" + bytes([m]) + (len(body) + 2).to_bytes(2, "big") + body
        table = lambda counts: b"\x00" + bytes(counts) + bytes(sum(counts))
        std = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
        mine = [0, 2, 2, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
        jpeg = lambda counts, sof=0xC0: b"\xff\xd8" + seg(0xE0, b"JFIF\x00") + seg(sof, b"\x08") + seg(0xC4, table(counts)) + seg(0xDA, b"\x00")
        self.assertEqual([filemap.jpeg_tables(jpeg(std)), filemap.jpeg_tables(jpeg(mine)), filemap.jpeg_tables(jpeg(std, 0xC2)),
                          filemap.jpeg_tables(b"\x89PNG\r\n"), filemap.jpeg_tables(b"\xff\xd8\x00")],
                         ["standard", "optimized", "progressive", None, None])
        p = os.path.join(self.dir, "cover.mkv")
        build_mkv(p, front_atts=[("cover.jpg", "image/jpeg", jpeg(std) + os.urandom(2000)), FONT])
        (cover,) = [a for a in filemap.describe(p)["attachments"] if a["image"]]
        self.assertEqual(cover["jpeg_tables"], "standard")
        maps = filemap.FileMaps(self.dir)
        maps.read(p)
        maps.files[p]["v"] = filemap.LAYOUT_VERSION - 1                   # read by a much older version
        self.assertIsNone(maps.get(p, os.stat(p)))
        maps.files[p]["v"] = filemap.MAP_VERSION - 1                      # only the mix missing: shown, but read again
        self.assertIsNotNone(maps.get(p, os.stat(p)))
        self.assertIsNone(maps.get(p, os.stat(p), full=True))

    def test_file_map_audio(self):
        """Library Health's reading of audio files: an MP3's ID3v2 tag (its frames, cover art and padding)
        and ID3v1 tag, and a FLAC's metadata blocks — in front of the audio, which is named as such;
        and a file it can't read into parts is still its streams, in blocks (one, if it's small)."""
        from plexcache import filemap
        d = tempfile.mkdtemp(dir=TMP)
        jpeg = b"\xff\xd8\xff\xe0" + os.urandom(3000)
        frame = lambda fid, body: fid + len(body).to_bytes(4, "big") + b"\0\0" + body
        frames = frame(b"TIT2", b"\0Title") + frame(b"APIC", b"\0image/jpeg\0\x03\0" + jpeg) + b"\0" * 500
        size = len(frames)
        id3 = b"ID3\x03\0\0" + bytes([(size >> 21) & 127, (size >> 14) & 127, (size >> 7) & 127, size & 127]) + frames
        mp3 = os.path.join(d, "a.mp3")
        with open(mp3, "wb") as fh:
            fh.write(id3 + os.urandom(3 * MiB) + b"TAG" + b"\0" * 125)
        m = filemap.describe(mp3)
        self.assertEqual(m["format"], "mp3")
        self.assertEqual([p["part"] for p in m["parts"]], ["id3", "tags", "attachments", "padding", "video", "tags"])
        self.assertEqual((m["video_start"], m["tracks"][0]["type"]), (len(id3), "audio"))
        self.assertEqual([(a["mime"], a["image"]) for a in m["attachments"]], [("image/jpeg", True)])
        self.assertEqual(m["meta"], [[len(id3) + 3 * MiB, 128]])                 # the ID3v1 tag: metadata at the end
        self.assertEqual({p.get("stream") for p in filemap.pieces(m) if p["kind"] == "video"}, {"audio"})
        block = lambda kind, body, last=False: bytes([(0x80 if last else 0) | kind]) + len(body).to_bytes(3, "big") + body
        pic = (3).to_bytes(4, "big") + (10).to_bytes(4, "big") + b"image/jpeg" + (0).to_bytes(4, "big") + b"\0" * 16 + len(jpeg).to_bytes(4, "big") + jpeg
        flac = os.path.join(d, "b.flac")
        with open(flac, "wb") as fh:
            fh.write(b"fLaC" + block(0, b"\0" * 34) + block(4, b"\0" * 200) + block(6, pic) + block(1, b"\0" * 4000, True) + os.urandom(2 * MiB))
        m = filemap.describe(flac)
        self.assertEqual((m["format"], [p["part"] for p in m["parts"]]), ("flac", ["flac", "info", "tags", "attachments", "padding", "video"]))
        self.assertEqual(m["attachments"][0]["mime"], "image/jpeg")
        self.assertIsNotNone(filemap.block_map(m, m["size"], {"floor": MiB, "start": 0}, MiB))
        other = os.path.join(d, "c.wav")
        with open(other, "wb") as fh:
            fh.write(os.urandom(300_000))
        m = filemap.describe(other)
        self.assertEqual((m["format"], m["tracks"][0]["type"]), ("stream", "audio"))
        from plexcache.app import file_block
        self.assertEqual(file_block(dict(m, blksize=None), 300_000, 512 * 1024), 303_104)  # smaller than a record: one block of its own size
        self.assertEqual(len(filemap.block_map(m, 300_000, {"floor": 0, "start": 0}, 303_104)["start"]), 1)

    def test_projection(self):
        """What optimizing would make of a file, from its inspection alone: cover art in front (the
        video pushed out of the first block) taken out when the library strips it, tags too when it
        strips them, the rest at the end — less metadata cached, more video in the first block."""
        from plexcache import filemap, projection
        p = os.path.join(self.dir, "proj.mkv")
        big = ("cover.jpg", "image/jpeg", b"\xff\xd8" + os.urandom(40_000))
        build_mkv(p, front_atts=[big, FONT], cluster=os.urandom(400_000))
        m = filemap.describe(p)
        rec = 16 * 1024
        m["mix"] = filemap.cache_mix(p, m, rec)
        self.assertGreater(m["video_start"], rec)                               # the cover art pushes the video out of the first block
        keep = projection.project(m, rec, "compress", False)
        gone = projection.project(m, rec, "remove", True)
        self.assertEqual(keep["removed"], {})
        self.assertEqual(set(gone["removed"]), {"image", "tags"})
        self.assertGreater(gone["removed"]["image"], 40_000)
        self.assertEqual(keep["first_video_now"], 0)
        self.assertGreater(keep["first_video"], rec // 2)                       # only the header in front now
        self.assertLess(gone["mix"]["meta_disk"], keep["mix"]["meta_disk"])
        self.assertLessEqual(keep["mix"]["meta"], m["mix"]["meta"])
        # the charts' rows: the video split into the start's (the lead-in's) and the end's (cached with the
        # metadata there); the total kept never counts what's taken out — now, or once optimized
        v = projection.views(m, gone)
        now, after = v["now"], v["after"]
        # now, what's to go is read and cached like the rest (players read cover art when they open a
        # file): in the total; once optimized, it's gone — the total is that much less
        for row, mix, out in ((now, m["mix"], True), (after, gone["mix"], False)):
            st, ca = row["stream"], row["cache"]
            self.assertEqual(st["lead"] + st["tail"], mix["video"])
            self.assertEqual(st["total"], st["lead"] + st["tail"] + st["meta"] + (st["out"] if out else 0))
            self.assertEqual(ca["total"], ca["lead"] + ca["tail"] + ca["meta"] + ca["round"] + (ca["out"] if out else 0))
        self.assertEqual(now["stream"]["total"], m["mix"]["video"] + m["mix"]["meta"])             # every byte of the cached blocks a play reads
        self.assertEqual(now["cache"]["total"], m["mix"]["video_disk"] + m["mix"]["meta_disk"] + m["mix"]["unused_disk"] + m["mix"]["round_disk"])
        self.assertEqual(now["stream"]["out"], sum(gone["removed"].values()))
        self.assertEqual(now["stream"]["meta"], m["mix"]["meta"] - sum(gone["removed"].values()))   # (what's to go: apart)
        self.assertEqual((now["gone"], after["gone"]), (False, True))
        self.assertNotIn("image", after["kinds"])
        # meaningful: what's streamed less the dead weight — the video at the end, tags, cover art
        for row in (now, after):
            mf = row["meaningful"]
            self.assertEqual((mf["lead"], mf["tail"], mf["out"]), (row["stream"]["lead"], 0, 0))
            self.assertEqual(mf["meta"], sum(row["useful_kinds"].values()))
            self.assertFalse({"tags", "image"} & set(row["useful_kinds"]))
            self.assertLessEqual(mf["total"], row["stream"]["total"])
        self.assertIn("image", now["left_out"])                                               # (now: the cover art's there, and left out)
        self.assertEqual(set(now["out_kinds"]), {"image", "tags"})
        for row in (now, after):                                                              # at least 60% of a block of video at the start
            self.assertGreaterEqual(row["stream"]["lead"], 0.6 * rec)
        self.assertLess(after["blocks"], now["blocks"])                                       # (here: fewer blocks for the file's start)
        self.assertEqual(projection.add_view(projection.add_view(None, now), now)["stream"]["total"], 2 * now["stream"]["total"])
        self.assertIsNone(projection.project(dict(m, format="mp4"), rec, "compress", False))   # only MKV files
        self.assertIsNone(projection.project(dict(m, mix={"video": 0}), rec, "compress", False))  # measured by an older version

    def test_file_map_damaged_files(self):
        """Library Health's reading of damaged files: an EBML header of unknown size, garbage instead of
        the SeekHead's targets, a part of unknown size, a second SeekHead, room after the last attachment,
        and other elements among the tracks — what can be read is shown, the rest is said, never an error."""
        from plexcache import filemap
        d = tempfile.mkdtemp(dir=TMP)
        E, UNKNOWN = fakes.ebml, b"\x01" + b"\xff" * 7

        def write(name, data, size=None):
            path = os.path.join(d, name)
            with open(path, "wb") as fh:
                fh.write(data)
                if size:
                    fh.truncate(size)
            return path
        self.assertEqual(filemap.describe(write("u.mkv", bytes.fromhex("1A45DFA3") + UNKNOWN + b"\x00" * 64))["format"], "other")
        head = E("1A45DFA3", E("4282", b"matroska"))
        seg_hdr = bytes.fromhex("18538067") + UNKNOWN
        seg = len(head) + len(seg_hdr)
        seek = lambda eid, at: E("4DBB", E("53AB", bytes.fromhex(eid)) + E("53AC", (at - seg).to_bytes(8, "big")))
        cluster = E("1F43B675", b"\x00" * 64)
        # a second SeekHead at the end, listing Tags of unknown size
        far = 1 * MiB
        body = E("114D9B74", seek("114D9B74", far)) + E("1549A966", b"") + cluster
        tags = bytes.fromhex("1254C367") + UNKNOWN + b"\x00" * 32
        p = write("s.mkv", head + seg_hdr + body, size=far + 4096)
        sh2 = E("114D9B74", seek("1254C367", far + 54))
        with open(p, "r+b") as fh:
            fh.seek(far)
            fh.write(sh2 + tags)
        m = filemap.describe(p)
        self.assertEqual([x["part"] for x in m["parts"]][-2:], ["seekhead", "tags"])
        self.assertEqual(m["parts"][-1]["size"], os.path.getsize(p) - (far + len(sh2)))       # (to the end: its size is unknown)
        # garbage where the SeekHead says something is: the video runs to the end
        p = write("g.mkv", head + seg_hdr + E("114D9B74", seek("1C53BB6B", 900)) + E("1549A966", b"") + cluster, size=4096)
        m = filemap.describe(p)
        self.assertEqual(m["parts"][-1]["part"], "video")
        # attachments with room after the last one, and an attachment of unknown size
        att = E("1941A469", E("61A7", E("466E", b"cover.jpg") + E("4660", b"image/jpeg") + E("465C", b"\xff" * 100)) + E("EC", b"\x00" * 10))
        p = write("a.mkv", head + seg_hdr + E("1549A966", b"") + att + cluster)
        kinds = [x["kind"] for x in filemap.pieces(filemap.describe(p))]
        self.assertEqual(kinds[kinds.index("image") + 1], "attachments")
        p = write("b.mkv", head + seg_hdr + E("1549A966", b"") + E("1941A469", bytes.fromhex("61A7") + UNKNOWN) + cluster)
        self.assertEqual(filemap.describe(p)["attachments"], [])
        fd = os.open(p, os.O_RDONLY)
        try:
            self.assertEqual(filemap._attachments(fd, 0, os.path.getsize(p)), [])      # not an Attachments element there
        finally:
            os.close(fd)
        self.assertEqual(filemap._tracks(E("EC", b"")), [])
        # a file that can't be parsed at all says why
        with mock.patch.object(filemap, "_mkv", mock.Mock(side_effect=ValueError("bad element"))):
            self.assertEqual(filemap.describe(p)["error"], "bad element")

    def test_file_map_details(self):
        """Track details (language, name, picture size, each kind of compression), attachments in front of
        the video, unusual MP4 boxes, and files that can't be read — never an error."""
        from plexcache import filemap
        u = lambda n, k=1: n.to_bytes(k, "big")
        entry = lambda *kids: _el("AE", b"".join(kids))
        tracks = (entry(_el("D7", u(1)), _el("83", u(1)), _el("86", b"V_MPEGH/ISO/HEVC"), _el("E0", _el("B0", u(1920, 2)) + _el("BA", u(1080, 2))))
                  + entry(_el("D7", u(2)), _el("83", u(2)), _el("86", b"A_AC3"), _el("22B59C", b"eng"), _el("536E", "Commentary".encode()))
                  + entry(_el("D7", u(3)), _el("86", b"S_VOBSUB"), _el("6D80", _el("6240", _el("5034", _el("4254", u(3))))))
                  + entry(_el("D7", u(4)), _el("86", b"S_TEXT/ASS"), _el("6D80", _el("6240", _el("5034", _el("4254", u(9))))))
                  + entry(_el("D7", u(5)), _el("86", b"S_TEXT/UTF8"), _el("22B59D", b"fr-CA"), _el("22B59C", b"fre")))
        got = filemap._tracks(tracks)
        # no Language written means English (Matroska's default); LanguageBCP47 wins over Language
        self.assertEqual([(t["type"], t["language"], t["name"], t["compression"]) for t in got],
                         [("video", "eng", "", None), ("audio", "eng", "Commentary", None), ("subtitles", "eng", "", "header stripping"),
                          ("subtitles", "eng", "", "other"), ("subtitles", "fr-CA", "", None)])
        self.assertEqual((got[0]["width"], got[0]["height"]), (1920, 1080))
        # attachments kept in front of the video
        p = os.path.join(self.dir, "front.mkv")
        build_mkv(p, front_atts=[("poster.jpg", "image/jpeg", os.urandom(50_000)), FONT])
        m = filemap.describe(p)
        self.assertEqual([a["image"] for a in m["attachments"]], [True, False])
        self.assertLess(next(x["pos"] for x in m["parts"] if x["part"] == "attachments"), m["video_start"])
        # MP4 boxes of 64-bit size and to-the-end size; a box too small to be one stops the walk
        q = os.path.join(self.dir, "odd.mp4")
        with open(q, "wb") as fh:
            fh.write(struct.pack(">I4s", 16, b"ftyp") + b"isom" * 2)
            fh.write(struct.pack(">I4sQ", 1, b"free", 24) + b"\0" * 8)
            fh.write(struct.pack(">I4s", 0, b"mdat") + b"\0" * 100)
        self.assertEqual([x["part"] for x in filemap.describe(q)["parts"]], ["header", "padding", "video"])
        with open(q, "wb") as fh:
            fh.write(struct.pack(">I4s", 16, b"ftyp") + b"isom" * 2 + struct.pack(">I4s", 4, b"junk") + b"\0" * 20)
        self.assertEqual([x["part"] for x in filemap.describe(q)["parts"]], ["header"])
        # a streamed MKV (a recording): the segment and its video of no recorded size — the video runs to the end
        live = os.path.join(self.dir, "live.mkv")
        unknown = bytes.fromhex("01FFFFFFFFFFFFFF")
        with open(live, "wb") as fh:
            fh.write(_el("1A45DFA3", _el("4282", b"matroska")) + bytes.fromhex("18538067") + unknown +
                     _el("1549A966", _el("2AD7B1", u(1000000, 3))) + bytes.fromhex("1F43B675") + unknown + os.urandom(5000))
        lm = filemap.describe(live)
        self.assertEqual(lm["parts"][-1]["part"], "video")
        self.assertEqual(lm["video_end"], os.path.getsize(live))
        self.assertEqual(list(filemap._children(b"\x1a\x45\xdf\xa3\x01\xff\xff\xff\xff\xff\xff\xff", 0, 12)), [])   # no size: stops
        self.assertEqual(list(filemap._children(b"\x00\x00", 0, 2)), [])                                            # not EBML: stops
        # can't be opened: the error, not an exception; a broken store starts empty; an unwritable one tries again
        with mock.patch.object(filemap.os, "open", mock.Mock(side_effect=PermissionError("no"))):
            self.assertIn("error", filemap.describe(p))
        bad = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(bad, filemap.STORE), "w") as fh:
            fh.write("{not json")
        maps = filemap.FileMaps(bad)
        self.assertEqual(maps.files, {})
        maps.read(p)
        with mock.patch("builtins.open", mock.Mock(side_effect=OSError("read-only"))):
            maps.save()
        self.assertTrue(maps.dirty)
        maps.save()
        self.assertFalse(maps.dirty)
        maps.save()                                                                  # nothing new: nothing written

    def test_mp4(self):
        from plexcache.layout import tail_ranges
        size = 40 * MiB
        p = os.path.join(self.dir, "a.mp4")
        m = fakes.make_mp4(p, size, moov_size=300_000)
        ranges, info = tail_ranges(p, size, 4 * MiB)
        self.assertEqual((info["index_at"], ranges), ("end", [(m["moov_at"], 300_000)]))
        self.assertLess(info["video_start"], m["moov_at"])                         # the video, before the index
        fakes.make_mp4(p, size, moov_size=300_000, faststart=True)
        ranges, info = tail_ranges(p, size, 4 * MiB)
        self.assertEqual((info["index_at"], ranges), ("start", []))
        self.assertGreater(info["video_start"], 300_000)                          # after the index in front

    def test_never_raises(self):
        from plexcache.layout import tail_ranges
        for name, data in (("x.mkv", bytes.fromhex("1A45DFA3") + b"\xff" * 20), ("y.mp4", b"\x00\x00\x00\x01ftyp"),
                           ("z.avi", b"RIFF" + b"\x00" * 100), ("e.mkv", b"")):
            p = os.path.join(self.dir, name)
            with open(p, "wb") as fh:
                fh.write(data)
                fh.truncate(9 * MiB)
            self.assertEqual(tail_ranges(p, 9 * MiB, 4 * MiB)[0], [], name)
        self.assertEqual(tail_ranges(os.path.join(self.dir, "missing.mkv"), 9 * MiB, 4 * MiB)[0], [])

    def test_with_index(self):
        from plexcache.playback import with_index
        size = 40 * MiB
        p = os.path.join(self.dir, "b.mkv")
        m = fakes.make_mkv(p, size, cues_size=60_000)
        # reading only the start: the index is added first, and the total stays in budget
        r = with_index(p, size, [(0, 10 * MiB)], 10 * MiB)
        self.assertEqual(r[0], (m["cues_at"], m["cues_len"]))
        self.assertEqual(sum(n for _, n in r), 10 * MiB)
        # the whole file fits: the index still comes first, and nothing is read twice
        r = with_index(p, size, [(0, size)], size)
        self.assertEqual(r[0], (m["cues_at"], m["cues_len"]))
        self.assertEqual(r[1], (0, m["cues_at"]))
        self.assertEqual(sum(n for _, n in r), size)
        # reading from the play position then wrapping round: index first, the rest in order
        r = with_index(p, size, [(30 * MiB, 10 * MiB), (0, 30 * MiB)], size)
        self.assertEqual(r, [(m["cues_at"], m["cues_len"]), (30 * MiB, m["cues_at"] - 30 * MiB), (0, 30 * MiB)])

    def test_read_plan_helpers(self):
        """What's been read so far (across the ranges in order), cutting reads out of the middle of a
        range, an empty file, a budget that drops whole ranges, and jobs that can't be restarted."""
        from plexcache import playback
        size = 40 * MiB
        p = os.path.join(self.dir, "c.mkv")
        m = fakes.make_mkv(p, size, cues_size=60_000)
        job = playback.Job("nowplaying", "x", p, [(0, 10), (100, 10)], state="reading", read=15)
        self.assertEqual([job.loaded(x) for x in (5, 102, 106)], [True, True, False])
        self.assertEqual(playback._without([(0, 100)], [(40, 10)]), [(0, 40), (50, 50)])
        self.assertEqual(playback.with_index(p, 0, [(0, 10)], 10), [(0, 10)])          # (no size: nothing to add)
        r = playback.with_index(p, size, [(0, 10 * MiB), (20 * MiB, 1 * MiB)], m["cues_len"] + 5 * MiB)
        self.assertEqual(r, [(m["cues_at"], m["cues_len"]), (0, 5 * MiB)])                     # the last range dropped
        self.assertFalse(playback.needs_restart(playback.Job("nowplaying", "x", p, [(0, 10)], state="failed"), 500))

    def test_recommend_recordsize(self):
        from plexcache.warmup import recommend_recordsize as rec
        K, M = 1024, 1024 * 1024
        pick = lambda kbps, size=10 ** 9: (lambda r: (r["best"], r["alt"]))(rec(kbps, size))
        self.assertEqual(pick(70000), (4 * M, 2 * M))     # 4K remux
        self.assertEqual(pick(120000), (4 * M, 8 * M))    # past the middle of the top band
        self.assertEqual(pick(30000), (2 * M, 4 * M))     # 1080p remux / 4K encode
        self.assertEqual(pick(10000), (1 * M, 2 * M))     # 1080p encode
        self.assertEqual(pick(3000), (1 * M, 512 * K))    # SD / 720p, FLAC
        self.assertEqual(pick(256), (512 * K, 1 * M))     # lossy music
        self.assertEqual(pick(64), (512 * K, 128 * K))    # audiobooks
        self.assertEqual(pick(256, 80_000), (128 * K, 512 * K))  # short clips: ZFS's own 128K
        # On either side of the line between two bands, the same two sizes: no flapping
        for edge in (1000, 15000, 40000):
            self.assertEqual(set(pick(edge - 1)), set(pick(edge + 1)))

    def test_dataset_must_suit_all_its_libraries(self):
        from types import SimpleNamespace
        from plexcache import checks
        libs = [("1", "Movies", 70000, 8e10), ("2", "Concerts", 30000, 3e10), ("3", "Music", 256, 8e6)]
        app = SimpleNamespace(
            libraries=[{"key": k, "title": t} for k, t, _, _ in libs],
            stats=LibraryStats(libraries=[{"key": k, "name": t, "kind": "movie", "count": 5, "avg_kbps": b, "avg_size": z}
                                          for k, t, b, z in libs]),
            library_records=lambda: {})
        # Movies (4M or 2M) and Concerts (2M or 4M) can share; whatever the dataset is set to now
        self.assertIsNone(checks._shared_record_check(app, "tank/films", ["1", "2"]))
        # Music (512K or 1M) can't share with them: move it out, the others keep 2M or 4M
        r = checks._shared_record_check(app, "tank/media", ["1", "2", "3"])
        self.assertEqual((r["move"], r["keep"]), (["Music"], 4 * 1024 * 1024))
        self.assertIn("Music: 512K or 1M", [str(x) for x in r["needs"]])
        self.assertIsNone(checks._shared_record_check(app, "tank/m", ["3"]))  # one library: nothing to share

    def test_cached_bytes(self):
        from plexcache.warmup import cached_bytes
        M = 1024 * 1024
        # A file no bigger than a record is one block of its own size
        self.assertEqual(cached_bytes(10 * M, 0, 0, 4 * M, 3 * M, False), 3 * M)
        # A lead-in shorter than a record still costs one record
        self.assertEqual(cached_bytes(300_000, 0, 0, M, 50 * M, False), M)
        # Longer ones round up by half a record on average; an index at the end adds the whole records it takes
        self.assertEqual(cached_bytes(100 * M, 0, 0, 4 * M, 10 ** 10, False), 102 * M)
        self.assertEqual(cached_bytes(100 * M, M, 1.0, 4 * M, 10 ** 10, False), 106 * M)
        self.assertEqual(cached_bytes(100 * M, 5 * M, 1.0, 4 * M, 10 ** 10, False), 110 * M)
        # 0 seconds: one record, plus the index's records if it's at the end
        self.assertEqual(cached_bytes(0, M, 1.0, 4 * M, 10 ** 10, False), 8 * M)
        self.assertEqual(cached_bytes(0, M, 1.0, 4 * M, 10 ** 10, True), 4 * M)
        # At the front, the index is simply part of the lead-in
        self.assertEqual(cached_bytes(100 * M, M, 1.0, 4 * M, 10 ** 10, True), 103 * M)
        # Only the share of files with an index at the end pays for it
        self.assertEqual(cached_bytes(100 * M, M, 0.5, 4 * M, 10 ** 10, False), 104 * M)
        # Never more than the file
        self.assertEqual(cached_bytes(100 * M, M, 1.0, 4 * M, 101 * M, False), 101 * M)
        # your minimum: one record plus the index's whole records
        stats = LibraryStats(libraries=[{"key": "1", "name": "M", "kind": "movie", "count": 10, "avg_kbps": 30000, "avg_size": 10 ** 10}])
        p = plan(stats, dict(config.DEFAULTS["warmup"]), None, records={"1": 4 * M}, index_layout={"movie": {"share": 1.0, "avg": 300_000}})
        self.assertEqual(p["libraries"][0]["your_min"], 10 * (4 * M + 4 * M))

    def test_plan_record_sizes(self):
        K, M = 1024, 1024 * 1024
        stats = LibraryStats(libraries=[
            {"key": "1", "name": "Movies", "kind": "movie", "count": 100, "avg_kbps": 70000, "avg_size": 80 * 10 ** 9},
            {"key": "3", "name": "Music", "kind": "audio", "count": 1000, "avg_kbps": 256, "avg_size": 8 * 10 ** 6},
            {"key": "4", "name": "Clips", "kind": "audio", "count": 10, "avg_kbps": 256, "avg_size": 80_000}])
        w = dict(config.DEFAULTS["warmup"])
        # Movies: dataset set to 1M, but a quarter of the files were written with 128K blocks
        records = {"1": {"rs": M, "blocks": {"sizes": {str(M): 75, str(128 * K): 25}}, "dataset": "tank/movies"},
                   "3": 512 * K}
        p = plan(stats, w, 10 ** 12, records=records, index_layout={"movie": {"share": 1.0, "avg": 2 * M}})
        movies, music, clips = p["libraries"]
        self.assertEqual((movies["rs"], movies["rs_best"], movies["rs_alt"], movies["rs_ok"]), (M, 4 * M, 2 * M, False))
        self.assertEqual(movies["dataset"], "tank/movies")
        self.assertTrue(music["rs_ok"])
        # Target minimum: optimized files at the recommended record size — one record at the start (60% of it
        # video), and the records the metadata at the end takes; your minimum: the same at yours
        self.assertEqual(movies["target_min"], 100 * (4 * M + 4 * M))
        self.assertEqual(movies["your_min"], 100 * (M + 2 * M))
        self.assertEqual(music["your_min"], 1000 * 512 * K)  # audio: no index at the end
        self.assertEqual(clips["target_min"], 10 * 80_000)   # never more than the file
        # Target: recommended records, the metadata at the end; yours: the block sizes the files really have
        lead = 70000 * 125 * (100 + w["padding_video"]) / 100 * movies["set_sec"]
        self.assertAlmostEqual(movies["target"], 100 * (lead + 2 * M + 4 * M))
        # the start never less than what's in front of the video and 60% of a record of video
        big_front = plan(stats, w, 10 ** 12, records=records, index_layout={"movie": {"share": 1.0, "avg": 2 * M, "front": 1.2 * M}})
        self.assertAlmostEqual(big_front["libraries"][0]["your_min"], 100 * (1.2 * M + 0.6 * M + M / 2 + 2 * M))   # (half a record: rounding up, on average)
        self.assertAlmostEqual(movies["your"], 100 * (0.25 * (lead + 64 * K + 2 * M) + 0.75 * (lead + M / 2 + 2 * M)))
        # A dataset that couldn't be checked isn't guessed at
        self.assertIsNone(clips["rs"])
        self.assertIsNone(clips["your_min"])
        self.assertIsNone(clips["your"])
        self.assertFalse(clips["rs_ok"])
        # Warm-up's sizes are these real ones; its floor: whole records through what's in front of the video
        # (64K until measured) and 60% of a record of video — past one 128K record — plus the metadata at the end
        self.assertAlmostEqual(movies["max"], movies["your"])
        self.assertAlmostEqual(movies["min"], 100 * (0.25 * (64 * K + 0.6 * 128 * K + 64 * K + 2 * M) + 0.75 * (M + 2 * M)))
        # Too small: every library's seconds are cut by the same share, as little as fits
        avail = p["need_max"] * 0.6
        q = plan(stats, w, avail + w["reserve_gb"] * 10 ** 9, records=records, index_layout={"movie": {"share": 1.0, "avg": 2 * M}})
        self.assertEqual(q["status"], "reduced")
        self.assertLessEqual(sum(q["planned_by_kind"].values()), avail)
        self.assertGreater(sum(q["planned_by_kind"].values()), avail * 0.9)
        self.assertLess(q["seconds"]["1"], movies["set_sec"])
        # 0 seconds is one record
        z = plan(stats, dict(w, libraries={"1": {"seconds": 0}}), None, records=records,
                 index_layout={"movie": {"share": 1.0, "avg": 2 * M}})["libraries"][0]
        self.assertEqual(z["set_sec"], 0)
        self.assertAlmostEqual(z["max"], z["min"])

    def test_plan_counts_indexes(self):
        from plexcache.warmup import DEFAULT_INDEX
        stats = LibraryStats(count={"movie": 1000, "tv": 0, "audio": 0}, avg_kbps={"movie": 20000, "tv": 0, "audio": 0},
                             libraries=[{"key": "1", "name": "Movies", "kind": "movie", "count": 1000, "avg_kbps": 20000}])
        w = dict(config.DEFAULTS["warmup"])
        with_idx = plan(stats, w, None)
        self.assertEqual(with_idx["need_max"], with_idx["need_by_kind"]["movie"])
        self.assertEqual(with_idx["index_by_kind"]["movie"], 1000 * DEFAULT_INDEX["movie"])  # always counted
        measured = plan(stats, w, None, {"movie": 100_000})
        self.assertEqual(measured["index_by_kind"]["movie"], 100_000_000)
        self.assertTrue(measured["index_measured"])


class WarmupPauseTests(unittest.TestCase):
    """When L2ARC-only caching steps aside."""

    def test_busy_percent(self):
        from plexcache.pauses import busy_percent
        before = {"sda": 1000, "sdb": 0, "loop0": 0, "sda1": 0}
        after = {"sda": 1600, "sdb": 200, "loop0": 5000, "sda1": 900}
        self.assertEqual(busy_percent(before, after, 1000, None), 60)       # whole disks only
        self.assertEqual(busy_percent(before, after, 1000, ["sdb"]), 20)    # just the pool's disks
        self.assertIsNone(busy_percent(before, after, 100, None))           # too short to judge

    def _app(self):
        app = App(tempfile.mkdtemp(dir=TMP))
        app.settings.update({"truenas": {"url": TRUENAS.url, "api_key": "good-key"}})
        app.zfs = {"datasets": [{"dataset": "tank/plexfiles", "pool": "tank", "keys": ["1"]}],
                   "pools": {"tank": {"disks": ["sdq"]}}}
        return app

    def test_reasons(self):
        from plexcache.pauses import L2arcPauses
        app = self._app()
        p = L2arcPauses(app, manual=False)
        self.assertIsNone(p.reason())
        app.monitor.last_buffering = (time.time(), "Dune on Living room TV")
        self.assertIn("buffering (Dune on Living room TV)", p.reason())
        app.settings.update({"warmup": {"pause_buffering": False}})
        self.assertIsNone(p.reason())
        app.monitor.preloading = lambda foreground_only=False: "Now-playing is loading Dune"
        self.assertEqual(p.reason(), "Now-playing is loading Dune")
        app.settings.update({"warmup": {"pause_preload": False}})
        self.assertIsNone(p.reason())
        with self.assertRaises(config.SettingsError):
            app.settings.update({"warmup": {"pause_disk_busy": 45}})

    def test_prefetch_l2arc_only_reads_pause_too(self):
        from plexcache.playback import Job
        app = self._app()
        mon = app.monitor
        self.assertIsNone(mon._background_pause())
        mon.last_buffering = (time.time(), "Dune on Living room TV")
        self.assertIn("buffering (Dune on Living room TV)", mon._background_pause())
        msgs = [e["message"] for e in app.activity.snapshot()["events"]]
        self.assertTrue(any(m.startswith("L2ARC-only reads paused: a player was buffering") for m in msgs))
        # A background read doesn't make others wait; a foreground one does
        bg = Job("nextepisode", "Show · S01E05", "/x", [(0, 1)], 1, background=True, state="reading")
        mon._chains["p"] = [bg]
        self.assertIsNone(mon.preloading(foreground_only=True))
        self.assertIn("Prefetch is loading", mon.preloading())
        bg.background = False
        self.assertIn("Prefetch is loading", mon.preloading(foreground_only=True))

    def test_scrub(self):
        from plexcache.pauses import L2arcPauses
        app = self._app()
        TRUENAS.pool_data["scan"] = {"function": "SCRUB", "state": "SCANNING"}
        try:
            p = L2arcPauses(app, manual=False)
            self.assertEqual(p._scrubbing(), "a scrub is running on tank", f"TrueNAS error: {p.scrub_error}")
        finally:
            TRUENAS.pool_data.pop("scan")
        self.assertIsNone(L2arcPauses(app, manual=False).reason())

    def test_disk_busy(self):
        from plexcache.pauses import L2arcPauses
        stats = os.path.join(os.environ["PLEXCACHE_PROC"], "diskstats")
        line = "   8      0 {name} 1 0 0 0 0 0 0 0 0 {ms} 0\n"
        app = self._app()
        p = L2arcPauses(app, manual=False)
        try:
            with open(stats, "w") as fh:
                fh.write(line.format(name="sdq", ms=1000))
            p.mark_idle()
            time.sleep(1.0)
            with open(stats, "w") as fh:
                fh.write(line.format(name="sdq", ms=1800))   # busy ~80% of the last second
            self.assertIn("% busy with other work", p.reason())
            # manual "Run now" goes at full speed and ignores it
            m = L2arcPauses(app, manual=True)
            m.mark_idle()
            time.sleep(0.6)
            self.assertIsNone(m.reason())
        finally:
            os.remove(stats)


class AppTests(unittest.TestCase):
    """End to end: the app against the fake Plex/TrueNAS and the HTTP API."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(dir=TMP)
        cls.app = App(cls.dir)
        cls.app.settings.update({
            "plex": {"url": PLEX.url, "token": "good-token"},
            "truenas": {"url": TRUENAS.url, "api_key": "good-key"},
            "nowplaying": {"enabled": True, "delay_sec": 0, "min_delay_sec": 0},
        })
        force_settings(cls.app, {"general": {"poll_seconds": 2}})
        from plexcache.server import serve
        cls.httpd = serve(cls.app, "127.0.0.1", 0)
        import threading
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.app.scan_library()
        cls.app.run_checks()
        cls.app.monitor.start()

    @classmethod
    def tearDownClass(cls):
        cls.app.monitor.stop.set()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.read(), r.headers

    def post(self, path, body, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_job_and_process_endpoints(self):
        """The Processes page's reads, the job and folder-move actions, and the poster thumbnails."""
        def get(path):
            try:
                with urllib.request.urlopen(self.base + path) as r:
                    return r.status, r.read(), r.headers
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, e.read(), e.headers
        app = self.app
        app.jobs.log.add({"type": "plan", "job": "e1", "kind": "optimize", "files": ["/m/a.mkv"]})
        app.jobs.log.add({"type": "file", "job": "e1", "kind": "optimize", "file": "/m/b.mkv", "result": "failed", "reason": "x"})
        code, body, _ = get("/api/processes")
        self.assertEqual(code, 200)
        self.assertIn("e1", [j["job"] for j in json.loads(body)["jobs"]])
        code, body, _ = get("/api/processes/files?job=e1&sort=result")
        rows = json.loads(body)["files"]
        self.assertEqual([(r["file"], r["result"]) for r in rows], [("/m/b.mkv", "failed"), ("/m/a.mkv", "not done")])
        self.assertEqual(get("/api/processes/files?job=e1&offset=x")[0], 400)
        rows = json.loads(get("/api/processes/files?job=e1")[1])["files"]                 # by title (here, the file's name)
        self.assertEqual([r["file"] for r in rows], ["/m/a.mkv", "/m/b.mkv"])
        code, body, headers = get("/api/processes/export?format=json")
        self.assertEqual((code, headers["Content-Type"]), (200, "application/json"))
        self.assertIn("text/csv", get("/api/processes/export")[2]["Content-Type"])
        self.assertEqual(get("/api/processes/nope")[0], 404)
        # posters: only Plex's own paths, through the app
        self.assertEqual(get("/api/poster?path=/etc/passwd")[0], 404)
        with mock.patch.object(app, "poster_thumb", lambda p, server="": b"jpeg"):
            code, body, headers = get("/api/poster?path=/library/metadata/5/thumb/17")
        self.assertEqual((code, body, headers["Content-Type"]), (200, b"jpeg", "image/jpeg"))
        with mock.patch.object(app, "poster_thumb", mock.Mock(side_effect=OSError("no"))):
            self.assertEqual(get("/api/poster?path=/library/metadata/5/thumb/17")[0], 404)
        # the job actions (no job running: nothing to do, but no errors)
        for action, want in (("dismiss", {"ok": True}), ("retry", {"ok": False}), ("remove", {"ok": False}),
                             ("pause", {"ok": False}), ("resume", {"ok": False}), ("cancel", {"ok": False})):
            with self.subTest(action=action):
                self.assertEqual(self.post("/api/job", {"action": action, "job": "e1", "file": "/m/zz.mkv"}), (200, want))
        self.assertEqual(self.post("/api/job", {"action": "nope"})[0], 400)
        # optimizing a library: known libraries and modes only; not set up, so neither started nor queued
        self.assertEqual(self.post("/api/optimize/library", {"library": "999"})[0], 400)
        key = app.libraries[0]["key"]
        self.assertEqual(self.post("/api/optimize/library", {"library": key, "mode": "x"})[0], 400)
        # optimizing follows the same formula as the file tools: without a recent snapshot or a backup, it does not start
        self.assertEqual(self.post("/api/optimize/library", {"library": key})[0], 400)
        from plexcache import safety
        safety.acknowledge_backup(app, key)
        self.assertEqual(self.post("/api/optimize/library", {"library": key}), (200, {"started": False, "queued": False}))
        self.assertEqual(self.post("/api/optimizer/check", {})[0], 200)
        # the folder move: with none in progress, each step says why it can't (never a server error)
        for action in ("plan", "copy", "catchup", "rename_old", "undo_rename", "delete_old", "nope"):
            with self.subTest(action=action):
                self.assertEqual(self.post("/api/migration", {"action": action})[0], 400)
        for action in ("check", "pause", "resume", "stop", "cancel"):
            with self.subTest(action=action):
                self.assertEqual(self.post("/api/migration", {"action": action})[0], 200)

    def test_local_network_only_and_local_names(self):
        """System → Local network only: devices outside the network are refused (pages, the API, sign-in),
        but not the container's health check. Without a sign-in, only local names are answered."""
        from plexcache import server
        cases = [("192.168.50.59", True), ("10.1.2.3", True), ("172.17.0.1", True), ("100.100.1.1", True), ("127.0.0.1", True),
                 ("::1", True), ("::ffff:192.168.1.5", True), ("fe80::1%eth0", True), ("fd00::5", True),
                 ("8.8.8.8", False), ("2001:4860::8888", False), ("172.32.0.1", False), ("junk", False)]
        for address, local in cases:
            self.assertEqual(server.local_address(address), local, address)
        names = [("192.168.1.10:8642", True), ("truenas", True), ("nas.local:8642", True), ("[fe80::1]:8642", True),
                 ("localhost:8642", True), (None, True), ("plexcache.example.com", False), ("evil.com:80", False)]
        for host, ok in names:
            self.assertEqual(server.host_allowed(host), ok, host)
        with mock.patch.dict(os.environ, {"PLEXCACHE_URL": "https://plexcache.example.com"}):
            self.assertTrue(server.host_allowed("plexcache.example.com"))              # the address it's set up at

        def get(path, headers=None):
            req = urllib.request.Request(self.base + path, headers=headers or {})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, e.read()
        app = self.app
        self.assertTrue(app.settings.section("general")["lan_only"])                  # on by default
        events = lambda: [e for e in app.activity.snapshot()["events"] if "refused" in json.dumps(e, default=str)]
        before = len(events())
        with mock.patch.object(server, "local_address", lambda a: False):              # as if from the internet
            code, body = get("/api/state")
            self.assertEqual(code, 403)
            self.assertIn("local network", json.loads(body)["error"]["_t"])
            code, body = get("/health")
            self.assertEqual(code, 403)
            self.assertIn(b"Local network only", body)
            self.assertEqual(self.post("/api/settings", {"general": {"lan_only": False}})[0], 403)   # can't be turned off from outside
            self.assertEqual(get("/healthz")[0], 200)                                  # the health check still answers
            self.assertEqual(len(events()), before + 1)                                # logged once, not per request
            app.settings.update({"general": {"lan_only": False}})
            self.assertEqual(get("/api/state")[0], 200)
            app.settings.update({"general": {"lan_only": True}})
        # without a sign-in, a public name is refused (DNS rebinding); the IP address and local names are fine
        self.assertFalse(app.auth.enabled)
        self.assertEqual(get("/api/state", {"Host": "evil.example.com"})[0], 403)
        self.assertEqual(get("/api/state", {"Host": "truenas.local:8642"})[0], 200)
        # Health says who can use it
        from plexcache import checks
        access = lambda: (lambda a: (a["status"], en(a["title"]), en(a["detail"])))(checks._access_check(app))
        self.assertEqual(access(), ("info", "No sign-in", "Any device on your local network can change settings and run the optimizer."))
        app.settings.update({"general": {"lan_only": False}})                     # open to anything: a warning
        self.assertEqual(access(), ("warn", "No sign-in", "Any device that can reach it can change settings and run the optimizer."))
        with mock.patch.object(app, "auth", mock.Mock(enabled=True)):
            self.assertEqual(access(), ("pass", "Sign-in required", "Any device that can reach it can use it after signing in."))
            app.settings.update({"general": {"lan_only": True}})
            self.assertEqual(access()[2], "Devices on your local network can use it after signing in.")

    def test_poster_thumbnails_kept(self):
        """Small posters from Plex, kept in memory: asked of Plex once each, the oldest dropped past 400."""
        from types import SimpleNamespace
        app = _new_app()
        asked = []
        app.plex = lambda timeout=None: SimpleNamespace(thumb=lambda p: asked.append(p) or p.encode())
        self.assertEqual(app.poster_thumb("/library/metadata/1/thumb/1"), b"/library/metadata/1/thumb/1")
        app.poster_thumb("/library/metadata/1/thumb/1")
        self.assertEqual(asked, ["/library/metadata/1/thumb/1"])
        for i in range(2, 403):
            app.poster_thumb(f"/library/metadata/{i}/thumb/1")
        self.assertEqual(len(app._thumbs), 400)
        self.assertNotIn("/library/metadata/2/thumb/1", app._thumbs)                # the oldest gone

    def test_inspections_one_library_at_a_time(self):
        """Reading a library's files (Library Health): a file that's gone meanwhile is skipped; starting
        another library pauses the first, which carries on where it was when started again; a paused
        one stays paused; only files new or changed are read unless every one is asked for."""
        from types import SimpleNamespace
        from plexcache import filemap
        app = _new_app()
        d = tempfile.mkdtemp(dir=TMP)
        files = []
        for lib in ("1", "2"):
            for i in range(3):
                p = os.path.join(d, f"{lib}-{i}.mkv")
                build_mkv(p)
                files.append(SimpleNamespace(path=p, library_key=lib, size=1, kbps=1, kind="movie"))
        files.append(SimpleNamespace(path=os.path.join(d, "gone.mkv"), library_key="1", size=1, kbps=1, kind="movie"))
        app.files = files
        app.map_path = lambda p: p
        app.library_info = lambda key: {"title": "Movies " + key}
        ins = filemap.Inspector(app)
        gate = threading.Event()
        real = app.file_maps.read

        def slow(path, force=False):
            gate.wait(10)
            return real(path, force)
        with mock.patch.object(app.file_maps, "read", slow):
            ins.start("1")
            self.assertTrue(ins.snapshot()["libs"]["1"]["running"])
            ins.start("2")                                                    # the first is paused
            snap = ins.snapshot()["libs"]
            self.assertEqual((snap["1"]["paused"], snap["2"]["running"]), (True, True))
            gate.set()
            self.assertTrue(wait_for(lambda: ins.snapshot()["libs"]["2"]["finished"]))
            self.assertFalse(ins.snapshot()["libs"]["1"]["finished"])         # still paused, where it was
            ins.start("1")
            self.assertTrue(wait_for(lambda: ins.snapshot()["libs"]["1"]["finished"]))
        self.assertEqual(app.file_maps.known([f.path for f in files]), 6)    # (the missing one skipped)
        self.assertEqual(ins.start("1")["libs"]["1"]["total"], 3)             # nothing new: not started again (a missing file is nothing to read)
        ins.pause("1")
        self.assertEqual(ins.start("1", force=True)["libs"]["1"]["total"], 4)
        self.assertTrue(wait_for(lambda: ins.snapshot()["libs"]["1"]["finished"]))
        self.assertTrue(any("inspected 3 files in Movies 1" in en(e["message"]) for e in app.activity.snapshot()["events"]))

    def test_every_check_has_a_place_on_the_page(self):
        """Library Health shows each check beside what it's about (a part's line, or a block's heading):
        a new check with no place there would never be seen, so each one must have one."""
        from plexcache import filechecks
        js = open(os.path.join(ROOT, "plexcache", "web", "app.js"), encoding="utf-8").read()
        start = js.index("  var CHECK_PARTS = ")
        placed = js[start:js.index("\n  }\n", js.index("  function blockChecks(", start))]
        for cid in filechecks.NAMES:
            self.assertIn(f'"{cid}"', placed, f"the check {cid} isn't placed on the Library Health page")

    def test_file_caching_checks(self):
        """Each check says Ideal, Pass or Warning, and the file takes the worst; the library's summary
        counts files by their worst and checks by their warnings."""
        from types import SimpleNamespace
        from plexcache import filechecks as fc
        M_ = 1024 * 1024
        rec = {"best": M_, "alt": 2 * M_, "rs": M_}
        good = {"format": "mkv", "size": 50 * M_, "blksize": M_, "video_start": 6000,
                "parts": [{"part": "index", "pos": 49 * M_, "size": 9000}, {"part": "chapters", "pos": 49 * M_ + 9000, "size": 500},
                          {"part": "tags", "pos": 49 * M_ + 9500, "size": 3000}],
                "attachments": [{"image": True, "jpeg_tables": "standard"}]}
        f = SimpleNamespace(size=50 * M_, matched=True)
        opts = {"mkv": True, "sync": True, "images": "unmatched"}
        ours = {"ours": {"PLEXCACHE_OPTIMIZER": "0.1.2"}}
        by = lambda checks: {c["id"]: c["status"] for c in checks}
        got = by(fc.check_file(None, f, "/x.mkv", good, rec, opts, ours, "optimal"))
        self.assertEqual(got, {"blocks": "ideal", "compression": "ideal", "front": "ideal", "index": "ideal",
                               "chapters": "ideal", "tags": "ideal", "cover": "ideal", "cover_setting": "warn"})   # cover art that isn't Plex's
        bad = dict(good, blksize=128 * 1024, video_start=3 * M_, parts=[{"part": "index", "pos": 100, "size": 9000},
                                                                        {"part": "tags", "pos": 49 * M_, "size": 300 * 1024}])
        got = by(fc.check_file(None, f, "/x.mkv", bad, rec, opts, None, "unknown"))
        self.assertEqual(got, {"blocks": "warn", "compression": "warn", "front": "warn", "index": "warn", "tags": "warn",
                               "cover": "warn", "cover_setting": "warn"})
        self.assertEqual(by(fc.check_file(None, f, "/x.mkv", dict(good, blksize=2 * M_, parts=[]), rec, {}, None, "assumed")),
                         {"blocks": "pass", "compression": "pass", "front": "ideal", "index": "warn", "cover": "warn", "cover_setting": "warn"})
        self.assertEqual(fc.check_file(None, f, "/x.mkv", None, rec, opts, None, None), [])   # not inspected: no checks
        plex = {"ours": {"PLEXCACHE_OPTIMIZER": "0.1.2", "PLEXCACHE_COVER": "plex sha256:abc"}}
        self.assertEqual(by(fc.check_file(None, f, "/x.mkv", good, rec, opts, plex, "optimal"))["cover_setting"], "pass")   # Plex's poster
        self.assertEqual(by(fc.check_file(None, f, "/x.mkv", dict(good, attachments=[]), rec, opts, ours, "optimal"))["cover_setting"], "ideal")
        results = [fc.check_file(None, f, "/x.mkv", good, rec, opts, ours, "optimal"), fc.check_file(None, f, "/x.mkv", bad, rec, opts, None, "unknown"), []]
        sm = fc.summarize(results)
        self.assertEqual((sm["checked"], sm["ideal"], sm["pass"], sm["warn"]), (2, 0, 0, 2))
        warned = {w["id"]: w["n"] for w in sm["warnings"]}
        self.assertNotIn("cover_setting", warned)                          # the library's own choice: not a warning to count
        self.assertNotIn("cover", warned)                                  # a share instead
        self.assertEqual((warned["index"], sm["cover"]), (1, {"files": 2, "compressed": 1}))
        self.assertEqual(en(next(w["text"] for w in sm["warnings"] if w["id"] == "index")), "1 file with no index, or the index in front of the video")
        self.assertEqual(fc.counts(results[1]), {"ideal": 0, "pass": 0, "warn": 7})
        # music: cover art in the file is ideal (none passes), and a big cover in front is the format's way: a pass
        song = {"format": "mp3", "size": 9 * M_, "blksize": M_, "video_start": 2 * M_, "tracks": [{"type": "audio"}],
                "parts": [], "attachments": [{"image": True, "jpeg_tables": "standard"}]}
        got = by(fc.check_file(None, f, "/x.mp3", song, rec, opts, None, "optimal"))
        self.assertEqual((got["cover_setting"], got["front"], got["cover"]), ("ideal", "pass", "pass"))
        self.assertEqual(by(fc.check_file(None, f, "/x.mp3", dict(song, attachments=[]), rec, opts, None, "optimal"))["cover_setting"], "pass")

    def test_library_health_endpoints(self):
        """The Library Health page: a library's files (estimated until read), the Survey job that reads
        them, and one file in full."""
        def get(path):
            try:
                with urllib.request.urlopen(self.base + path) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, None
        app = self.app
        key = next(l["key"] for l in app.libraries if l.get("kind") == "movie")
        self.assertEqual(get("/api/library?key=nope")[0], 404)
        self.assertEqual(get("/api/library?key=" + key + "&offset=x")[0], 400)
        code, d = get("/api/library?key=" + key)
        self.assertEqual(code, 200)
        self.assertEqual(d["total"], len([f for f in app.files if str(f.library_key) == key]))
        self.assertEqual(d["files"], sorted(d["files"], key=lambda f: (f["label"].lower(), f["file"])))   # by title
        self.assertTrue(all(f["plan"]["estimated"] and f["blocks"] is None for f in d["files"] if not f["read"]))
        self.assertEqual(self.post("/api/library/survey", {"library": "nope"})[0], 400)
        self.assertEqual(self.post("/api/library/survey", {"library": key})[0], 200)
        self.assertTrue(wait_for(lambda: not (app.inspector.snapshot()["libs"].get(key) or {}).get("running")))
        self.assertEqual(self.post("/api/library/survey", {"library": key, "pause": True})[0], 200)
        code, d = get("/api/library?key=" + key)
        self.assertEqual(d["surveyed"], d["total"])
        one = d["files"][0]
        self.assertTrue(one["read"])
        code, full = get("/api/library/file?path=" + urllib.parse.quote(one["file"]))
        self.assertEqual((code, full["file"], full["format"]), (200, one["file"], "mkv"))
        self.assertIsNotNone(full["blocks"])
        code, est = get("/api/library/blocks?path=" + urllib.parse.quote(one["file"]) + "&n=1")
        self.assertEqual((code, [b["n"] for b in est["blocks"]]), (200, [1]))
        self.assertEqual(get("/api/library/blocks?path=/etc/passwd&n=1")[0], 404)
        self.assertEqual(get("/api/library/blocks?path=" + urllib.parse.quote(one["file"]) + "&n=x")[0], 400)
        self.assertEqual(get("/api/library/file?path=/etc/passwd")[0], 404)         # library files only
        self.assertEqual(get("/api/library/nope")[0], 404)
        self.assertNotIn("survey", [j["kind"] for j in app.jobs.log.jobs()])         # not a job: nothing changes on disk
        self.assertIn(one["health"], ("ideal", "pass", "warn"))                        # each file's caching checks, worst wins
        self.assertTrue(full["checks"] and d["summary"]["checked"] >= 1)
        self.assertEqual(len(get("/api/library?key=" + key + "&q=zzzz-none")[1]["files"]), 0)

    def test_health_results(self):
        h = self.app.health
        groups = {g["id"]: g for g in h["groups"]}
        media = {r["library"]: r for r in groups["media"]["table"]}
        self.assertEqual(media["Movies"]["status"], "pass")
        # Music is ticked (wanted) but its folder isn't mounted: an error, fixed in Settings
        self.assertEqual(media["Music"]["status"], "fail")
        self.assertIn("/music", media["Music"]["detail"])
        self.assertIn("System → Media Mappings", media["Music"]["fix"])
        # Record size, compression and optimized are columns with their own info boxes: no items repeat them
        self.assertEqual([i["title"] for i in groups["media"]["items"]], [])
        self.assertEqual(media["Movies"]["record"]["status"], "warn")       # tank/plexfiles is 128K
        self.assertNotIn("compression", media["Movies"])                     # per dataset: Media datasets
        dsrow = {r["dataset"]: r for r in groups["datasets"]["datasets"]}["tank/plexfiles"]
        # the badge is what the dataset uses; under it, what to use instead
        self.assertEqual((dsrow["compression"]["status"], dsrow["compression"]["badge"], en(dsrow["compression"]["text"])),
                         ("warn", "lz4", "Use ZSTD-3"))
        # Movies, TV Shows and Audiobooks share it but need different record sizes: split it, saying how
        rs = dsrow["recordsize"]
        if rs["status"] == "warn" and en(rs.get("badge", "")) == "Split dataset":
            self.assertEqual(len(rs["lines"]), 1)
            self.assertIn("to a dataset of its own;", en(rs["lines"][0]))
        self.assertNotIn("optimized", media["Movies"])                        # the Optimizer page has its own
        # one row per dataset, naming the libraries on it
        rows = {r["dataset"]: r for r in groups["datasets"]["datasets"]}
        self.assertEqual(set(rows), {"tank/plexfiles", TV_DS})
        self.assertEqual(rows["tank/plexfiles"]["libraries"], ["Movies", "Audiobooks"])
        self.assertEqual(rows["tank/plexfiles"]["in_use"], {"status": "pass", "text": "Movies, Audiobooks"})
        self.assertEqual(rows[TV_DS]["libraries"], ["TV Shows"])
        self.assertEqual(rows["tank/plexfiles"]["cache"]["status"], "fail")
        self.assertEqual(rows["tank/plexfiles"]["recordsize"]["status"], "warn")
        self.assertEqual(rows[TV_DS]["cache"]["status"], "pass")
        self.assertEqual(rows[TV_DS]["recordsize"], {"status": "pass", "text": "1M", "lines": []})
        self.assertEqual(rows[TV_DS]["l2arc"]["status"], "pass")
        self.assertIn("960 GB", rows[TV_DS]["l2arc"]["text"])
        self.assertNotIn("L2ARC cache devices", [i["title"] for i in groups["zfs"]["items"]])
        self.assertNotIn("disk", groups)                      # disk speed lives in Settings now
        zitems = dataset_fixes(groups["datasets"])
        # primarycache=metadata keeps data out of RAM, so nothing can reach L2ARC: a failure, not a tip
        bad = zitems["Cache settings for tank/plexfiles"]
        self.assertEqual(bad["status"], "fail")
        self.assertIn("Movies and Audiobooks", bad["detail"])
        self.assertEqual("sudo zfs set primarycache=all tank/plexfiles", bad["fix_cmd"])
        self.assertNotIn(f"Cache settings for {TV_DS}", zitems)
        # the good dataset keeps the features usable; the bad one's libraries are named as skipped
        recs = {r["feature"]: r for r in h["recommendations"]}
        self.assertEqual({k: r["verdict"] for k, r in recs.items()}, {"warmup": "use", "nextepisode": "use", "nowplaying": "use"})
        self.assertIn("Skips Movies and Audiobooks (tank/plexfiles has primarycache=metadata)", recs["nowplaying"]["reason"])
        self.assertFalse(self.app.library_cacheable("1"))
        self.assertTrue(self.app.library_cacheable("2"))
        self.assertEqual(self.app.stats.count["tv"], 6)

    def test_connection_lights(self):
        c = self.app.connections
        for k in ("plex_server", "plex_token", "truenas_server", "truenas_key"):
            self.assertEqual(c[k]["status"], "ok", (k, c[k]))
        self.assertEqual(c["summary"]["status"], "ok")
        self.assertIn("libraries visible", c["plex_token"]["detail"])
        groups = {g["id"]: g for g in self.app.health["groups"]}
        titles = [i["title"] for i in groups["plex"]["items"]]
        self.assertIn("Plex server found", titles)
        self.assertIn("Signed in to Plex", titles)

    def test_connection_lights_without_token(self):
        from plexcache.app import App
        app = App(tempfile.mkdtemp(dir=TMP))
        app.settings.update({"plex": {"url": PLEX.url}})
        c = app.test_connections()
        self.assertEqual(c["plex_server"]["status"], "ok")
        self.assertEqual(c["plex_token"]["status"], "fail")  # nothing works without it
        self.assertEqual(c["truenas_server"]["status"], "warn")
        self.assertEqual(c["summary"]["status"], "fail")
        app.settings.update({"plex": {"token": "wrong"}})
        self.assertEqual(app.test_connections()["plex_token"]["text"], "Plex token rejected")
        app.settings.update({"plex": {"url": "http://127.0.0.1:1"}})
        c = app.test_connections()
        self.assertEqual(c["plex_server"]["status"], "fail")
        self.assertIn("127.0.0.1", c["plex_server"]["detail"])
        self.assertEqual(c["plex_token"]["status"], "off")

    def test_autodetect_fills_empty_addresses(self):
        import plexcache.app as appmod
        old = appmod.PLEX_DEFAULT_PORT, appmod.TRUENAS_PROBE_PORT
        appmod.PLEX_DEFAULT_PORT = int(PLEX.url.rsplit(":", 1)[1])
        appmod.TRUENAS_PROBE_PORT = int(TRUENAS.url.rsplit(":", 1)[1])
        try:
            app = appmod.App(tempfile.mkdtemp(dir=TMP))
            app._autodetect("127.0.0.1")
        finally:
            appmod.PLEX_DEFAULT_PORT, appmod.TRUENAS_PROBE_PORT = old
        self.assertEqual(app.settings.section("plex")["url"], PLEX.url)
        self.assertEqual(app.settings.section("truenas")["url"], TRUENAS.url)
        # never overwrites an address you typed
        app.settings.update({"plex": {"url": "http://10.0.0.5"}})
        app._autodetect("127.0.0.1")
        self.assertEqual(app.settings.section("plex")["url"], "http://10.0.0.5:32400")

    def test_cache_settings_ok_and_not_checked(self):
        from plexcache import checks
        saved = self.app.zfs
        try:
            self.app.zfs = zfs_with(saved, {"tank/plexfiles": {"primarycache": "all"}})
            g = checks.check_datasets(self.app)
            self.assertTrue(all(r["cache"]["status"] == "pass" for r in g["datasets"]))
            recs = {r["feature"]: r for r in checks.recommendations(self.app)}
            self.assertEqual({k: r["verdict"] for k, r in recs.items()}, {"warmup": "use", "nextepisode": "use", "nowplaying": "use"})
            self.assertNotIn("Skips", recs["nowplaying"]["reason"])
            self.assertTrue(self.app.library_cacheable("1"))
            # no readable media → a row still appears, saying why nothing was checked
            self.app.zfs = zfs_with(saved, datasets=[])
            z = {i["title"]: i for i in checks.check_datasets(self.app)["items"]}
            row = z["Media Datasets: not checked yet"]
            self.assertEqual(row["status"], "info")
            self.assertIn("no library folder is readable", row["detail"])
        finally:
            self.app.zfs = saved

    def test_unread_cache_settings_are_not_called_wrong(self):
        from plexcache import checks
        saved = self.app.zfs
        try:
            none = {"primarycache": None, "secondarycache": None, "recordsize": None}
            self.app.zfs = zfs_with(saved, {"tank/plexfiles": none, TV_DS: none})
            g = checks.check_datasets(self.app)
            z = dataset_fixes(g)
            row = z[f"Cache settings for {TV_DS}: couldn’t read"]
            self.assertEqual(row["status"], "info")
            self.assertIn(f"sudo zfs get primarycache,secondarycache,recordsize {TV_DS}", row["fix_cmd"])
            self.assertEqual({r["cache"]["text"] for r in g["datasets"]}, {"Couldn’t read"})
            recs = {r["feature"]: r["verdict"] for r in checks.recommendations(self.app)}
            self.assertNotEqual(recs["nowplaying"], "avoid")
            self.assertNotEqual(recs["nextepisode"], "avoid")
            self.assertTrue(self.app.library_cacheable("1"))      # unknown isn't treated as wrong
        finally:
            self.app.zfs = saved

    def test_recordsize_and_l2arc_settings(self):
        from plexcache import checks
        groups = {g["id"]: g for g in checks.run_all(self.app)["groups"]}  # fresh, not left over from another test
        z = dict(dataset_fixes(groups["datasets"]), **{i["title"]: i for i in groups["zfs"]["items"] + groups["media"]["items"]})
        media = {r["library"]: r for r in groups["media"]["table"]}
        rec = media["Movies"]["record"]                           # parent dataset is 128K: Movies wants 2M
        self.assertEqual((rec["status"], rec["rs"], rec["best"]), ("warn", 128 * 1024, 2 * 1024 * 1024))
        # 182 MB/s default guess? pacing 10% of hdd speed vs 32 MiB/s fill: fine
        self.assertEqual(z["L2ARC settings"]["status"], "pass")
        self.assertIn("32 MiB/s", z["L2ARC settings"]["detail"])

    def test_l2arc_setting_problems(self):
        from plexcache import checks
        params = {"l2arc_noprefetch": "1", "l2arc_write_max": str(8 * 1048576),
                  "l2arc_rebuild_enabled": "0", "l2arc_mfuonly": "1"}
        # Automatic L2ARC pacing reads just under the fill rate, so there's nothing to warn about
        auto = {i["title"]: i for i in checks._l2arc_tuning_checks(self.app, params)}
        self.assertNotIn("The L2ARC fills slower than warm-up reads", auto)
        self.app.settings.update({"warmup": {"pacing_auto": False, "pacing_percent": 50}, "general": {"hdd_mb_per_sec": 150}})
        try:
            items = {i["title"]: i for i in checks._l2arc_tuning_checks(self.app, params)}
        finally:
            self.app.settings.update({"warmup": {"pacing_auto": True, "pacing_percent": 10}})
        self.assertIn("L2ARC only keeps frequently used data", items)
        self.assertEqual("echo 0 | sudo tee /sys/module/zfs/parameters/l2arc_mfuonly", items["L2ARC only keeps frequently used data"]["fix_cmd"])
        self.assertIn("The L2ARC is emptied on every reboot", items)
        slow = items["The L2ARC fills slower than warm-up reads"]
        self.assertIn("8 MiB/s", slow["detail"])
        self.assertIn("75 MB/s", slow["detail"])
        self.assertIn("about 5%", slow["fix"])
        self.assertIn("automatic L2ARC pacing", slow["fix"])
        self.assertIn("Init/Shutdown Scripts", slow["fix"])
        # A slow fill with a long warm-up ahead: said, with how long; pacing that keeps up; read-ahead copied too
        from plexcache.warmup import L2ARC_RATE_SHARE
        with mock.patch.object(self.app, "warmup_active", lambda: True), \
                mock.patch.object(self.app, "lead_in_plan", lambda w: {"need_max": 8 * 1048576 * L2ARC_RATE_SHARE * 3 * 3600}):
            held = {en(i["title"]): i for i in checks._l2arc_tuning_checks(self.app, dict(params, l2arc_noprefetch="0"))}
        self.assertIn("takes about 3 hours at that speed", en(held["Warm-up is held to 8 MB/s by the L2ARC"]["detail"]))
        self.assertIn("ZFS’s own read-ahead is copied too (l2arc_noprefetch=0)", en(held["L2ARC settings"]["detail"]))
        self.app.settings.update({"warmup": {"pacing_auto": False, "pacing_percent": 5}})
        try:
            ok = {en(i["title"]): i for i in checks._l2arc_tuning_checks(self.app, {"l2arc_write_max": str(64 * 1048576)})}
        finally:
            self.app.settings.update({"warmup": {"pacing_auto": True, "pacing_percent": 10}})
        self.assertEqual(en(ok["L2ARC settings"]["detail"]), "Suited to warm-up: fills at up to 64 MiB/s.")
        # Each library's record size against the one recommended for its media, and the next best
        K, M = 1024, 1024 * 1024
        real_stats, real_records = self.app.stats, self.app.library_records
        self.app.stats = LibraryStats(libraries=[{"key": "1", "name": "Movies", "kind": "movie", "count": 10,
                                                  "avg_kbps": 30000, "avg_size": 30 * 10 ** 9}])
        try:
            for rs, status in ((2 * M, "ideal"), (4 * M, "pass"), (M, "warn"), (None, "info")):
                self.app.library_records = lambda rs=rs: {"1": {"rs": rs, "dataset": "tank/m"}}
                rec = checks.library_record(self.app, "1")
                self.assertEqual((rec["best"], rec["alt"], rec["status"]), (2 * M, 4 * M, status))
        finally:
            self.app.stats, self.app.library_records = real_stats, real_records

    def test_unavailable_features_cannot_be_enabled(self):
        # With every media dataset at primarycache=metadata nothing can be cached.
        saved = self.app.zfs
        self.app.zfs = zfs_with(saved, {TV_DS: {"primarycache": "metadata"}})
        try:
            for feature in ("nowplaying",):
                self.app.settings.update({feature: {"enabled": False}})
                status, r = self.post("/api/settings", {feature: {"enabled": True}})
                self.assertEqual(status, 400, feature)
                self.assertIn("primarycache", en(r["error"]))
                # switching off is always allowed
                status, _ = self.post("/api/settings", {feature: {"enabled": False}})
                self.assertEqual(status, 200)
        finally:
            self.app.zfs = saved
            self.app.settings.update({"nowplaying": {"enabled": True}})

    def test_unticked_library_is_listed_but_not_cached(self):
        from plexcache import mapping
        self.app.settings.update({"general": {"excluded_libraries": ["1"]}})
        try:
            self.app.scan_library()
            rows = mapping.location_report(self.app)
            movies = [r for r in rows if r["key"] == "1"]
            self.assertTrue(movies and movies[0]["excluded"])        # still shown, so it can be ticked again
            self.assertFalse(self.app.library_cacheable("1"))
            self.assertTrue(self.app.library_cacheable("2"))
            self.assertFalse(any(f.library_key == "1" for f in self.app.files))
            # its dataset isn't checked for it any more, and Health says so
            for _ in range(100):  # let any re-check started by an earlier settings save finish
                if not self.app.checking:
                    break
                time.sleep(0.1)
            self.app.run_checks()
            groups = {g["id"]: g for g in self.app.health["groups"]}
            rows = {r["dataset"]: r for r in groups["datasets"]["datasets"]}
            self.assertEqual(rows["tank/plexfiles"]["libraries"], ["Audiobooks"])
            self.assertEqual(rows["tank/plexfiles"]["in_use"]["text"], "Audiobooks · not cached: Movies")
            media = {r["library"]: r for r in groups["media"]["table"]}
            self.assertEqual((media["Movies"]["status"], media["Movies"]["badge"]), ("info", "Disabled"))
        finally:
            self.app.settings.update({"general": {"excluded_libraries": []}})
            self.app.scan_library()
            self.app.run_checks()
        # a ticked library whose folder can't be found isn't cached either
        self.assertFalse(self.app.library_cacheable("4"))           # Music: /music isn't mounted

    def test_warmup_cannot_be_enabled_without_l2arc(self):
        saved = self.app.zfs
        self.app.zfs = zfs_with(saved, pools={"tank": {"l2_online": 0, "l2_total": 0, "cache_devices": 0}})
        try:
            # Without an L2ARC no library warms up, and one set to warm up says it fell back
            self.app.settings.update({"warmup": {"libraries": {"2": {"schedule": "weekly"}}}})
            self.assertEqual(set(self.app.warmup_schedules().values()), {"none"})
            self.assertFalse(self.app.warmup_active())
            self.assertIn("warmup.2.schedule", [f["id"] for f in self.app.fallbacks.recent()])
            self.assertEqual(self.app.state()["l2arc"], "absent")
            from plexcache import checks
            rec = {r["feature"]: r for r in checks.recommendations(self.app)}["warmup"]
            self.assertEqual(rec["verdict"], "avoid")
            self.assertIn("Unavailable", rec["reason"])
            rows = {r["dataset"]: r for r in checks.check_datasets(self.app)["datasets"]}
            # No L2ARC anywhere: the table just says None; the warning is under Caching mode, once
            self.assertEqual(rows[TV_DS]["l2arc"], {"status": "info", "text": "", "badge": "None"})
            mode = {en(i["title"]): i for i in checks.check_cache_mode(self.app)["items"]}
            self.assertEqual(mode["No L2ARC on the media pool"]["status"], "warn")
            self.assertIn("I won’t use an L2ARC", en(mode["No L2ARC on the media pool"]["fix"]))
            self.assertEqual(self.app.cache_mode(), "ram")
            # Saying you won't use one turns the warning into a note
            self.app.settings.update({"general": {"no_l2arc": True}})
            mode = {en(i["title"]): i for i in checks.check_cache_mode(self.app)["items"]}
            self.assertNotIn("No L2ARC on the media pool", mode)
            self.assertEqual(mode["RAM-only caching"]["status"], "info")
            self.app.settings.update({"general": {"no_l2arc": False}})
            self.assertFalse(self.app.library_cacheable("2", "warmup"))
            self.assertTrue(self.app.library_cacheable("2", "nowplaying"))
        finally:
            self.app.zfs = saved
            self.app.settings.update({"warmup": {"libraries": {}}})
            self.app.fallbacks.items.clear()
        # with an L2ARC, every library warms up at boot by default
        self.assertTrue(self.app.warmup_active())
        self.assertEqual(self.app.warmup_schedules()["2"], "boot")

    def test_nested_dataset_mounts(self):
        """Which dataset really holds a library when datasets are mounted inside each other."""
        from plexcache import mapping
        root = tempfile.mkdtemp(dir=TMP)
        for sub in ("music", "music/Mine", "data", "data/TV", "data/TV/Extra"):
            os.makedirs(os.path.join(root, sub))
        j = lambda *p: os.path.join(root, *p)  # noqa: E731
        fake = [{"root": "/", "mountpoint": "/", "fstype": "overlay", "source": "overlay"},
                # a parent dataset mounted at /music, and the library's own dataset mounted inside it
                {"root": "/", "mountpoint": j("music"), "fstype": "zfs", "source": "tank/Parent"},
                {"root": "/", "mountpoint": j("music", "Mine"), "fstype": "zfs", "source": "tank/Parent/Music"},
                # a dataset mounted inside a library folder: Plex scans into it
                {"root": "/", "mountpoint": j("data"), "fstype": "zfs", "source": "tank/plex"},
                {"root": "/", "mountpoint": j("data", "TV", "Extra"), "fstype": "zfs", "source": "tank/plex/extra"}]
        app = App(tempfile.mkdtemp(dir=TMP))
        app.locations = [{"key": "9", "library": "Music", "status": "ok", "local_path": j("music", "Mine"), "excluded": False},
                         {"key": "2", "library": "TV Shows", "status": "ok", "local_path": j("data", "TV"), "excluded": False}]
        saved_mounts, saved_sys = zfs.mounts, mapping.SYSTEM_MOUNTS
        zfs.mounts = lambda: fake
        mapping.SYSTEM_MOUNTS = tuple(m for m in saved_sys if not (root == m or root.startswith(m + "/")))
        try:
            rows = {d["dataset"]: d for d in app.media_datasets()}
        finally:
            zfs.mounts, mapping.SYSTEM_MOUNTS = saved_mounts, saved_sys
        self.assertEqual(rows["tank/Parent/Music"]["libraries"], ["Music"])
        self.assertTrue(rows["tank/Parent"]["unused"])      # holds no library itself: flagged
        self.assertEqual(rows["tank/plex"]["libraries"], ["TV Shows"])
        self.assertEqual(rows["tank/plex/extra"]["libraries"], ["TV Shows"])  # inside the TV folder: part of TV
        self.assertFalse(rows["tank/plex/extra"]["unused"])

    def test_unused_dataset_is_flagged(self):
        from plexcache import mapping
        saved = mapping.SYSTEM_MOUNTS
        # test files live under the temp folder, which normally counts as part of the container
        mapping.SYSTEM_MOUNTS = tuple(m for m in saved if not (TMP == m or TMP.startswith(m + "/")))
        try:
            self.app.run_checks()
        finally:
            mapping.SYSTEM_MOUNTS = saved
        groups = {g["id"]: g for g in self.app.health["groups"]}
        rows = {r["dataset"]: r for r in groups["datasets"]["datasets"]}
        self.assertEqual(rows["tank/spare"]["in_use"], {"status": "warn", "text": "No Plex library"})
        self.assertIsNone(rows["tank/spare"]["cache"])                 # nothing cached from it, so not checked
        self.assertIn("tank/spare isn’t used by any Plex library", dataset_fixes(groups["datasets"]))

    def test_benchmark_goes_one_dataset_at_a_time(self):
        import plexcache.app as appmod
        seen = []
        real = appmod.benchmark_file

        def spy(path, size):
            p = self.app.benchmark_progress
            saved = set(self.app.settings.section("general")["disk_speeds"])
            seen.append((p["dataset"], p["done"], p["total"], tuple(p["finished"]), saved))
            return real(path, size)
        appmod.benchmark_file = spy
        old = self.app.settings.section("general")["disk_speeds"]
        try:
            self.app.settings.update({"general": {"disk_speeds": {}}})
            self.app.run_benchmark()
            self.app.benchmark_thread.join(30)
        finally:
            appmod.benchmark_file = real
            self.app.settings.update({"general": {"disk_speeds": old}})
        order = [d for d, *_ in seen]
        first, second = order[0], [d for d in order if d != order[0]][0]
        self.assertEqual(order, sorted(order, key=lambda d: d != first))  # all of one, then all of the other
        later = [x for x in seen if x[0] == second]
        self.assertEqual(later[0][1:4], (1, 2, (first,)))
        self.assertIn(first, later[0][4])  # the first dataset's speed was saved before the second began

    def test_benchmark_is_per_dataset(self):
        saved = self.app.settings.section("general")
        try:
            self.app.run_benchmark()
            # wait for all of it, including the health check it runs afterwards
            self.app.benchmark_thread.join(30)
            self.assertIsNone(self.app.state()["benchmark"]["progress"])  # cleared once it's done
            speeds = self.app.settings.section("general")["disk_speeds"]
            self.assertEqual(set(speeds), {"tank/plexfiles", TV_DS})
            self.assertTrue(all(v["mb_s"] >= 10 and v["measured_at"] for v in speeds.values()))
            self.assertEqual({r["dataset"] for r in self.app.benchmark["rows"]}, {"tank/plexfiles", TV_DS})
            # each library's files are judged against their own dataset's speed
            self.app.settings.update({"general": {"disk_speeds": {TV_DS: {"mb_s": 900}, "tank/plexfiles": {"mb_s": 120}}}})
            self.assertEqual(self.app.disk_speed_for_library("2"), 900)
            self.assertEqual(self.app.disk_speed_for_library("1"), 120)
            self.assertEqual(self.app.fastest_cached_disk(), 900)
            # a speed typed by hand is kept; out-of-range values are refused
            status, _ = self.post("/api/settings", {"general": {"disk_speeds": {TV_DS: {"mb_s": 5}}}})
            self.assertEqual(status, 400)
        finally:
            self.app.settings.update({"general": {"disk_speeds": saved["disk_speeds"], "hdd_mb_per_sec": saved["hdd_mb_per_sec"],
                                                  "hdd_measured_at": saved["hdd_measured_at"]}})
            self.app.run_checks()  # so later tests don't see health worked out with the speeds above

    def test_pages_and_static(self):
        for p in ("/", "/health", "/caching", "/ssd", "/activity", "/system", "/features", "/settings"):
            status, body, _ = self.get(p)
            self.assertEqual(status, 200)
            self.assertIn(b"/static/app.js", body)
        status, _, headers = self.get("/static/app.js")
        self.assertIn("javascript", headers["Content-Type"])
        # After an update (or tnsync) browsers mustn't keep the old interface: the page names its
        # files with the build, files carry an ETag (unchanged ones get a 304), and the state says
        # which build is running so open tabs reload themselves
        from plexcache.server import BUILD
        _, body, _ = self.get("/activity")
        self.assertIn(f'/static/app.js?v={BUILD}"'.encode(), body)
        self.assertIn(f'/static/app.css?v={BUILD}"'.encode(), body)
        req = urllib.request.Request(self.base + "/static/app.js", headers={"If-None-Match": headers["ETag"]})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 304)
        cm.exception.close()
        self.assertEqual(json.loads(self.get("/api/state")[1])["build"], BUILD)
        for bad in ("/static/../config.py", "/static/.hidden", "/nope"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(bad)
            cm.exception.close()

    def test_state_hides_secrets(self):
        _, body, _ = self.get("/api/state")
        self.assertNotIn(b"good-token", body)
        self.assertNotIn(b"good-key", body)
        st = json.loads(body)
        self.assertEqual(st["zfs"]["l2_total"], 960 * 10 ** 9)
        self.assertEqual(st["plan"]["status"], "fits")

    def test_settings_api(self):
        status, r = self.post("/api/settings", {"warmup": {"reserve_gb": 25}})
        self.assertEqual(status, 200)
        self.assertEqual(r["settings"]["warmup"]["reserve_gb"], 25)
        self.addCleanup(self.app.settings.update, {"warmup": {"reserve_gb": 100}})
        status, r = self.post("/api/settings", {"warmup": {"reserve_gb": -1}})
        self.assertEqual(status, 400)
        status, r = self.post("/api/settings", {"warmup": {"reserve_gb": 20}}, ctype="text/plain")
        self.assertEqual(status, 415)

    def test_warmup_run_via_api(self):
        # 3 s of each episode is far less than the file, so the index at the end is a separate read
        tv = next(l["key"] for l in self.app.libraries if l["kind"] == "tv")
        self.app.settings.update({"warmup": {"libraries": {tv: {"seconds": 3}}}})
        self.addCleanup(self.app.settings.update, {"warmup": {"libraries": {}}})
        status, r = self.post("/api/warmup/run", {})
        self.assertTrue(r["started"])
        for _ in range(100):
            if self.app.warmup_run is None:
                break
            time.sleep(0.1)
        run = self.app.activity.snapshot()["runs"][0]
        self.assertEqual(run["result"], "ok")
        # Movies and Audiobooks sit on the primarycache=metadata dataset, so only TV is read
        self.assertEqual(run["files"], 6)
        # each episode's index at the end was found and cached too
        self.assertEqual(run["index"]["tv"]["files"], 6)
        self.assertEqual(run["index"]["tv"]["largest"], 20_012)
        msgs = [e["message"] for e in self.app.activity.snapshot()["events"]]
        self.assertTrue(any("TV episodes: 6 of 6 had some" in m for m in msgs), msgs[:5])
        self.assertAlmostEqual(self.app.index_avg()["tv"], 20_012)
        self.assertEqual(self.app.index_layout()["tv"], {"share": 1.0, "avg": 20_012, "front": 96})   # front: before the video
        self.assertEqual(run["errors"], 0)
        self.assertTrue(run["manual"])

    def test_playback_features(self):
        import plexcache.playback as pb
        ep = CAT["episodes"][2]  # S01E03 -> next is S02E01
        PLEX.sessions = [{"id": "s9", "item": ep, "offset": 700000, "duration": 1200000}]
        try:
            for _ in range(60):
                snap = self.app.monitor.snapshot()
                jobs = [j for s in snap for j in s["jobs"]]
                if len(jobs) >= 2 and all(j["state"] == "done" for j in jobs):
                    break
                time.sleep(0.2)
            feats = {}
            for j in jobs:
                feats.setdefault(j["feature"], j)                   # the first of each: the next episode comes first
            self.assertEqual(feats["nextepisode"]["target"], "Show's Name · S02E01")
            self.assertEqual(feats["nextepisode"]["state"], "done")
            self.assertEqual(feats["nowplaying"]["state"], "done")
        finally:
            PLEX.sessions = []
        for _ in range(30):
            if not self.app.monitor.snapshot():
                break
            time.sleep(0.2)
        self.assertEqual(self.app.monitor.snapshot(), [])
        msgs = [e["message"] for e in self.app.activity.snapshot()["events"]]
        self.assertTrue(any("started: Show's Name · S01E03" in m for m in msgs))



def force_settings(app, patch):
    """Set values the web UI can't (a 1 s delay, a 2 s poll, an exact RAM reserve), to make
    tests quick and precise. Bypasses validation on purpose: tests only."""
    with app.settings._lock:
        for sec, values in patch.items():
            app.settings._data[sec].update(values)


def wait_for(cond, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def _new_app(**settings):
    app = App(tempfile.mkdtemp(dir=TMP))
    base = {"plex": {"url": PLEX.url, "token": "good-token"}, "truenas": {"url": TRUENAS.url, "api_key": "good-key"}}
    for sec, vals in settings.items():
        base.setdefault(sec, {}).update(vals)
    app.settings.update(base)
    return app


class PlaybackMonitorTests(unittest.TestCase):
    """The playback monitor driven poll by poll against the fake Plex."""

    @classmethod
    def setUpClass(cls):
        cls.app = _new_app(nowplaying={"enabled": True, "delay_sec": 0, "min_delay_sec": 0},
                           nextepisode={"libraries": {"*": {"start": "off", "end": "none"}}})
        cls.app.scan_library()
        cls.app.run_checks()

    def setUp(self):
        import plexcache.playback as pb
        self.pb = pb
        self.saved = (pb.MIN_JOB, pb.RESTART_EVERY)
        mon = self.app.monitor
        mon.sessions.clear()
        mon._seen.clear()
        self.app.cachelog.__init__()
        for j in mon._bg_queue:  # L2ARC-only reads left over from another test (they'd otherwise carry on)
            j.cancel = True
            if j.stop_event is not None:
                j.stop_event.set()
        mon._bg_queue = []
        mon.last_buffering = None
        self.app.settings.update({"general": {"ram_reserve_gb": 4}, "nowplaying": {"delay_sec": 0},
                                  "nextepisode": {"libraries": {"*": {"start": "off", "end": "none"}}}})

    def tearDown(self):
        self.pb.MIN_JOB, self.pb.RESTART_EVERY = self.saved
        PLEX.sessions = []
        self.poll()

    def poll(self):
        self.app.monitor.poll(self.app.settings.get())

    def jobs(self, feature="nowplaying"):
        return [j for s in self.app.monitor.snapshot() for j in s["jobs"] if j["feature"] == feature]

    def events(self):
        return [e["message"] for e in self.app.activity.snapshot()["events"]]

    def session(self, ep, offset, state="playing", sid="m1"):
        PLEX.sessions = [{"id": sid, "item": ep, "offset": offset, "duration": 1200000, "state": state}]

    def test_streams_that_cant_be_read(self):
        """Plex not naming the file, or a stream in a state other than playing, paused or buffering: said
        once in Recent events, and nothing is read. An episode or track Plex gives no show or album
        for has nothing after it to prefetch."""
        mon = self.app.monitor
        settings = self.app.settings.get()
        for key, s in (("odd1", types.SimpleNamespace(files=[], state="playing", label="Odd Stream", player="TV")),
                       ("odd2", types.SimpleNamespace(files=[("/x.mkv", 100)], state="stopped", label="Stopped Stream", player=""))):
            mon._seen[key] = {}
            mon._maybe_nowplaying(key, s, settings)
        ev = [en(e) for e in self.events()]
        self.assertTrue(any("Odd Stream on TV: not cached — Plex didn’t say which file is playing" in e for e in ev), ev[:3])
        self.assertTrue(any("Stopped Stream on unknown player: not cached — Plex reports it as “stopped”, not playing" in e for e in ev), ev[:3])
        lonely = types.SimpleNamespace(type="episode", grandparent_rating_key="", parent_rating_key="", rating_key="1", library_key="2")
        self.assertIsNone(mon._steps(None, lonely, "tv"))
        self.assertIsNone(mon._steps(None, types.SimpleNamespace(**dict(vars(lonely), type="track")), "music"))

    def test_music_plays_are_read_logged_and_measured(self):
        app, mon = self.app, self.app.monitor
        app.library_cacheable = lambda *a, **k: True
        try:
            app.settings.update({"general": {"no_l2arc": True, "ram_cooldown_lead_in_minutes": 0, "ram_cooldown_whole_minutes": 0}})
            track = CAT["tracks"][0]
            path = app.map_path(track["path"])

            def play(sid):
                PLEX.sessions = [{"id": sid, "item": track, "offset": 10000, "duration": 120000}]
                self.poll()
                self.assertTrue(wait_for(lambda: self.jobs() and all(j["state"] == "done" for j in self.jobs()), 10))
                PLEX.sessions = []
                self.poll()
            play("t1")
            self.assertTrue(any("whole file" in en(e) for e in self.events()))            # the read is in Recent events
            self.assertTrue(app.cachelog.last(path, whole=True))
            play("t2")                                                                    # no cooldown: read again, and measured
            ram = app.cachelog.survival()["routes"]["ram"]
            self.assertEqual(sum(r["kept"] + r["gone"] for r in ram["rows"]), 1)
            # A stream that can't be cached says why, once (here: its file isn't where Plex says)
            gone = dict(track, path=track["path"] + ".missing", key="9999")
            PLEX.sessions = [{"id": "t9", "item": gone, "offset": 10000, "duration": 120000}]
            self.poll()
            self.poll()
            self.assertEqual(sum("isn’t found here" in en(e) for e in self.events()), 1)
            self.assertEqual(app.monitor.last_sessions["sessions"][0]["rating_key"], "9999")   # (for the exported log)
            self.assertIn("plex_sessions", app.diagnostics())
            # Within the whole-file cooldown: not read again, and Recent events says why
            app.settings.update({"general": {"ram_cooldown_whole_minutes": 180}})
            PLEX.sessions = [{"id": "t3", "item": track, "offset": 10000, "duration": 120000}]
            self.poll()
            self.assertTrue(any("within its cooldown" in en(e) for e in self.events()))
        finally:
            del app.library_cacheable
            app.settings.update({"general": {"no_l2arc": False, "ram_cooldown_lead_in_minutes": 120, "ram_cooldown_whole_minutes": 180}})

    def test_prefetch_waits_without_saying_nothing_plays_next(self):
        # Seen first near the end (e.g. after the app restarted): whole files resolve to "Nothing"
        # while lead-ins wait for Now Playing Cache. That's nothing due yet, not nothing next.
        import plexcache.prefetch as pf
        app = self.app
        app.library_cacheable = lambda *a, **k: True
        real = pf.resolve
        pf.resolve = lambda app_, key, field, value, kind, name="": "none" if field == "end" else real(app_, key, field, value, kind, name)
        try:
            app.settings.update({"general": {"no_l2arc": True}, "nextepisode": {"libraries": {"3": {"start": "rest", "end": "next"}}}})
            before = len(app.activity.events)                                             # (only this test's events)
            PLEX.sessions = [{"id": "z1", "item": CAT["tracks"][0], "offset": 115000, "duration": 120000}]
            self.poll()
            mine = [e["message"] for e in list(app.activity.events)[before:]]
            self.assertFalse(any("nothing plays after" in en(m) for m in mine))
            self.assertTrue(wait_for(lambda: self.jobs() and all(j["state"] == "done" for j in self.jobs()), 10))
            self.poll()                                                                   # Now Playing done: lead-ins next
            self.assertTrue(wait_for(lambda: self.jobs("nextepisode"), 10))
        finally:
            pf.resolve = real
            del app.library_cacheable
            app.settings.update({"general": {"no_l2arc": False}, "nextepisode": {"libraries": {"*": {"start": "off", "end": "none"}}}})

    def test_prefetch_for_audiobooks(self):
        app, mon = self.app, self.app.monitor
        app.library_cacheable = lambda *a, **k: True       # the audiobook dataset is left at primarycache=metadata in tests

        def jobs(sid):  # this player's reads: its own queue, then its share of the L2ARC-only one
            return mon._chains.get(sid, []) + [j for j in mon._bg_queue if j.player == sid]

        def queued(sid, whole=None):
            return [j.target for j in jobs(sid) if not j.cancel and (whole is None or j.slice != whole)]

        def done(sid):
            return wait_for(lambda: jobs(sid) and all(j.state == "done" for j in jobs(sid)), 10)

        def play(n, sid, offset=10000):
            PLEX.sessions = [{"id": sid, "item": CAT["tracks"][n], "offset": offset, "duration": 120000}]
            self.poll()
        try:
            app.settings.update({"nowplaying": {"enabled": False}, "nextepisode": {"libraries": {}}})
            self.assertEqual(app.cache_mode(), "l2arc")
            # Default with an L2ARC: the start of everything in the library, nearest first —
            # the rest of this book, then the next book (the chapter playing is left to Now-playing)
            play(0, "m1")
            self.assertTrue(done("m1"))
            self.assertEqual(queued("m1"), ["Author · Chapter 2", "Author · Chapter 3", "Author · Book 2, chapter 1", "Author · Book 2, chapter 2"])
            self.assertTrue(all(j.slice and not j.ram for j in jobs("m1")))            # starts head for the SSD
            self.assertEqual(jobs("m1")[0].length % (128 * 1024), 0)                   # 10 s, in whole 128K records of the dataset
            self.assertLessEqual(jobs("m1")[0].length, 1024 * 1024)
            # Near the end (90%): the rest of the book in full — the next chapter into RAM (within the
            # limit), the one after it to the SSD in the background (not counted against the limit)
            play(0, "m1", offset=110000)
            self.assertTrue(done("m1"))
            self.assertEqual(queued("m1", whole=True), ["Author · Chapter 2", "Author · Chapter 3"])
            full = [j for j in jobs("m1") if not j.slice]
            self.assertEqual([(j.background, j.ram) for j in full], [(False, True), (True, False)])
            # Starts read in the last few hours aren't read again
            play(1, "m2")
            self.assertEqual(queued("m2"), ["Author · Chapter 1"])  # only what hasn't been read yet
            # RAM only: Default is the rest of the book, and a choice that needs an L2ARC falls back to it (and Health says so)
            self.app.cachelog.__init__()
            app.settings.update({"general": {"no_l2arc": True}, "nextepisode": {"libraries": {"3": {"start": "all_groups"}}}})
            play(0, "m3")
            self.assertTrue(done("m3"))
            self.assertEqual(queued("m3"), ["Author · Chapter 2", "Author · Chapter 3"])
            self.assertTrue(all(j.ram for j in jobs("m3")))                            # in RAM, so within the limit
            (fb,) = app.fallbacks.recent()
            self.assertEqual((en(fb["chosen"]), en(fb["used"])), ("All the author’s books", "Rest of the book"))
            # Within the RAM limit, in order: here only the first fits
            self.app.cachelog.__init__()
            real = app.ram_budget
            app.ram_budget = lambda: dict(real(), nextepisode=400 * 1024)  # one 256K lead-in (two 128K records), not two
            try:
                play(0, "m4")
                self.assertTrue(done("m4"))
            finally:
                app.ram_budget = real
            self.assertEqual(queued("m4"), ["Author · Chapter 2"])
            # Don't prefetch, and nothing near the end: nothing
            app.settings.update({"nextepisode": {"libraries": {"3": {"start": "off", "end": "none"}}}})
            play(0, "m5", offset=115000)
            self.assertEqual(queued("m5"), [])
            # a player that stops: what it had queued is dropped
            PLEX.sessions = []
            self.poll()
            self.assertEqual(mon._chains, {})
        finally:
            del app.library_cacheable
            app.fallbacks.items.clear()
            app.settings.update({"general": {"no_l2arc": False},
                                 "nowplaying": {"enabled": True}, "nextepisode": {"libraries": {"*": {"start": "off", "end": "none"}}}})

    def test_l2arc_only_queue_newest_first_and_outlives_its_player(self):
        from types import SimpleNamespace
        app, mon = self.app, self.app.monitor
        real = mon._run_background
        mon._run_background = lambda: None                   # keep everything waiting, to look at the order
        app.cachelog.__init__()

        def item(name):
            return {"rating_key": "", "title": name, "files": [(f"/lib/{name}.mkv", 50 * MiB, 8000)]}

        def ask(pid, names):
            s = SimpleNamespace(session_key=pid, files=[], library_key="2", label=pid, type="episode", grandparent_title="")
            mon._queue(f"{pid}:x", s, app.settings.get(), "tv", [], [(True, item(n)) for n in names])
        try:
            ask("a", ["one", "two", "three"])
            ask("b", ["four", "two"])                         # newest first; "two" moves up with it
            self.assertEqual([j.target for j in mon._bg_queue if j.state == "waiting"], ["four", "two", "one", "three"])
            PLEX.sessions = []
            self.poll()                                       # both players stop: their reads stay queued
            self.assertEqual([j.target for j in mon._bg_queue if j.state == "waiting"], ["four", "two", "one", "three"])
        finally:
            mon._run_background = real
            for j in mon._bg_queue:
                j.state, j.cancel = "cancelled", True
            mon._bg_queue.clear()

    def test_prefetch_for_movies_in_a_collection(self):
        app, mon = self.app, self.app.monitor
        app.library_cacheable = lambda *a, **k: True       # the movie dataset is left at primarycache=metadata in tests

        def jobs(sid):
            return mon._chains.get(sid, []) + [j for j in mon._bg_queue if j.player == sid]

        def queued(sid):
            return [j.target for j in jobs(sid) if not j.cancel]
        try:
            app.settings.update({"nowplaying": {"enabled": False}, "nextepisode": {"libraries": {"1": {"end": "none"}}},
                                 # the library step is L2ARC-only: let it read at once here
                                 "warmup": {"pacing_percent": 100, "pause_disk_busy": 0, "pause_buffering": False}})
            # Part 1 of a trilogy, default with an L2ARC: the rest of the collection first, then the library
            PLEX.sessions = [{"id": "v1", "item": CAT["movies"][0], "offset": 60000, "duration": 6000000}]
            self.poll()
            self.assertEqual(queued("v1"), ["Trilogy · Part 2", "Trilogy · Part 3", "Movie 3.mkv"])
            self.assertTrue(wait_for(lambda: all(j.state == "done" for j in jobs("v1")), 10),
                            [(j.target, j.state, str(j.paused)) for j in jobs("v1")])
            # Just the collection: the middle movie gets the next one, then the one before
            self.app.cachelog.__init__()
            app.settings.update({"nextepisode": {"libraries": {"1": {"start": "whole", "end": "none"}}}})
            PLEX.sessions = [{"id": "v2", "item": CAT["movies"][1], "offset": 60000, "duration": 6000000}]
            self.poll()
            self.assertEqual(queued("v2"), ["Trilogy · Part 3", "Trilogy · Part 1"])
            # A movie in no collection: nothing until the library step
            self.app.cachelog.__init__()
            PLEX.sessions = [{"id": "v3", "item": CAT["movies"][3], "offset": 60000, "duration": 6000000}]
            self.poll()
            self.assertEqual(queued("v3"), [])
            self.assertTrue(any("nothing plays after" in m for m in self.events()))
        finally:
            del app.library_cacheable
            PLEX.sessions = []
            self.poll()
            app.settings.update({"nowplaying": {"enabled": True}, "nextepisode": {"libraries": {"*": {"start": "off", "end": "none"}}},
                                 "warmup": {"pacing_percent": 10, "pause_disk_busy": 50, "pause_buffering": True}})

    def test_buffering_is_noticed_and_preload_waits_for_playing(self):
        self.session(CAT["episodes"][0], 60000, state="buffering")
        self.poll()
        self.assertEqual(self.app.monitor.active_streams(), 1)
        self.assertIn("Show's Name · S01E01", self.app.monitor.last_buffering[1])
        self.assertEqual(self.jobs(), [])                       # Plex isn't playing yet
        self.session(CAT["episodes"][0], 60000)
        self.poll()
        self.assertTrue(wait_for(lambda: self.jobs() and self.jobs()[0]["state"] == "done"))
        self.assertTrue(self.jobs()[0]["whole"])

    def test_preloading_reports_what_it_reads(self):
        force_settings(self.app, {"nowplaying": {"delay_sec": 3}})
        self.session(CAT["episodes"][1], 60000)
        self.poll()
        job = self.app.monitor.sessions["m1:" + CAT["episodes"][1]["key"]]["jobs"][0]
        job.state = "reading"                                   # as if mid-read
        self.assertIn("Now Playing Cache is loading", self.app.monitor.preloading())
        job.state = "waiting"
        self.assertIsNone(self.app.monitor.preloading())

    def test_skip_ahead_restarts_from_the_new_position(self):
        self.pb.MIN_JOB, self.pb.RESTART_EVERY = MiB, 0
        b = self.app.ram_budget()
        # leave Now-playing about 5 MB, less than the 12 MB episode
        force_settings(self.app, {"general": {"ram_reserve_gb": (b["available"] - 8.5e6) / 1e9}})
        ep = CAT["episodes"][2]
        self.session(ep, 120000)                                # 10% in
        self.poll()
        self.assertTrue(wait_for(lambda: self.jobs() and self.jobs()[0]["state"] == "done"))
        first = self.jobs()[0]
        self.assertFalse(first["whole"])
        self.session(ep, 1080000)                               # jump to 90%
        self.poll()
        self.assertTrue(any("viewer skipped ahead" in m for m in self.events()))
        self.assertTrue(wait_for(lambda: len(self.jobs()) == 2 and self.jobs()[1]["state"] == "done"))

    def test_no_room_is_reported_once(self):
        self.pb.MIN_JOB = MiB
        b = self.app.ram_budget()
        force_settings(self.app, {"general": {"ram_reserve_gb": b["available"] / 1e9}})
        self.session(CAT["episodes"][3], 60000)
        self.poll()
        self.poll()
        self.assertEqual(sum("no room left" in m for m in self.events()[:5]), 1)
        self.assertEqual(self.jobs(), [])

    def test_unreadable_file_fails_cleanly(self):
        ghost = {"path": os.path.join(MEDIA, "TV", "gone.mkv"), "size": 12 * MiB, "kbps": 6500,
                 "season": 9, "episode": 9, "key": "9999"}
        self.session(ghost, 60000)
        self.poll()
        self.assertEqual(self.jobs(), [])                       # nothing to read: it isn't there
        self.assertTrue(any("isn’t found here" in en(m) for m in self.events()))

    def test_stream_ending_cancels_waiting_preload(self):
        force_settings(self.app, {"nowplaying": {"delay_sec": 1}})
        self.session(CAT["episodes"][4], 60000)
        self.poll()
        job = list(self.app.monitor.sessions.values())[0]["jobs"][0]
        self.assertEqual(job.state, "waiting")
        PLEX.sessions = []
        self.poll()
        self.assertEqual(job.state, "cancelled")
        time.sleep(1.3)                                         # the job thread notices too
        self.assertEqual(job.state, "cancelled")

    def test_no_next_episode(self):
        self.app.settings.update({"nextepisode": {"libraries": {"2": {"start": "next", "end": "none"}}},
                                  "nowplaying": {"enabled": False}})
        try:
            self.session(CAT["episodes"][-1], 60000)
            self.poll()
            self.assertTrue(any("nothing plays after" in m for m in self.events()))
        finally:
            self.app.settings.update({"nowplaying": {"enabled": True}, "nextepisode": {"libraries": {}}})

    def test_monitor_survives_errors(self):
        mon = self.app.monitor
        force_settings(self.app, {"general": {"poll_seconds": 2}})
        real = mon.poll
        try:
            for boom, expect in ((PlexError("Plex is down"), "can’t read Plex sessions"), (ValueError("bad"), "unexpected error")):
                def failing(_settings, e=boom):
                    raise e
                mon.poll = failing
                mon.stop.clear()
                t = threading.Thread(target=mon._loop, daemon=True)
                t.start()
                self.assertTrue(wait_for(lambda: mon.last_error is not None))
                mon.stop.set()
                t.join(5)
                self.assertTrue(any(expect in m for m in self.events()), expect)
                mon.last_error = None
        finally:
            mon.poll = real
            mon.stop.clear()

    def metrics(self, what):
        return [m for m in self.app.activity.metrics if m["what"] == what]

    def test_skips_are_measured(self):
        ep = CAT["episodes"][0]
        self.session(ep, 60000)
        self.poll()
        self.assertTrue(wait_for(lambda: self.jobs() and self.jobs()[0]["state"] == "done"))
        before = len(self.metrics("skip"))
        self.session(ep, 600000)                                # 9 minutes on in a moment: a skip
        self.poll()
        skip = self.metrics("skip")[before]
        self.assertTrue(skip["cached"])                         # the whole episode was loaded
        self.assertGreater(skip["seconds"], 500)
        self.session(ep, 603000)                                # normal playback: not a skip
        self.poll()
        self.assertEqual(len(self.metrics("skip")), before + 1)

    def test_skip_reasons(self):
        ep = CAT["episodes"][1]
        self.app.settings.update({"nowplaying": {"enabled": False}})
        try:
            self.session(ep, 60000)
            self.poll()
            self.session(ep, 900000)
            self.poll()
            self.assertEqual(self.metrics("skip")[-1]["reason"], "Now-playing preload is off")
        finally:
            self.app.settings.update({"nowplaying": {"enabled": True}})
        from plexcache.playback import Job
        mon = self.app.monitor
        key = list(mon.sessions)[0]
        s = type("S", (), {"files": [(ep["path"], ep["size"])], "progress": 0.9, "label": "x", "library_key": "2"})()
        for job, reason in ((Job("nowplaying", "x", "/f", [(0, 100)], 12 * MiB, state="failed"), "the preload failed"),
                            (Job("nowplaying", "x", "/f", [(0, 12 * MiB)], 12 * MiB, state="reading"), "the preload hadn't reached that part yet"),
                            (Job("nowplaying", "x", "/f", [(0, MiB)], 12 * MiB, state="done", limited=True), "only part of the file fits in the RAM limit"),
                            (Job("nowplaying", "x", "/f", [(0, MiB)], 12 * MiB, state="done"), "it was outside what had been loaded")):
            mon.sessions[key]["jobs"] = [job]
            mon._record_skip(key, s, self.app.settings.get(), 120)
            self.assertEqual(self.metrics("skip")[-1]["reason"], reason)

    def test_next_episode_readiness_and_stream_end(self):
        import plexcache.playback as pb
        # RAM only: the lead-in is read into RAM, so it gets the still-cached check (with an L2ARC
        # it's on its way to the SSD, and isn't checked for still being in RAM)
        self.app.settings.update({"nextepisode": {"libraries": {"2": {"start": "next"}}},
                                  "nowplaying": {"enabled": False}, "general": {"no_l2arc": True}})
        stats = os.path.join(os.environ["PLEXCACHE_PROC"], "diskstats")
        saved_after = pb.RETENTION_AFTER
        pb.RETENTION_AFTER = 0
        try:
            with open(stats, "w") as fh:
                fh.write("   8 0 sda 1 0 0 0 0 0 0 0 0 1000 0\n")
            self.session(CAT["episodes"][2], 60000, sid="n1")    # S01E03 -> next is S02E01
            self.poll()
            self.assertTrue(wait_for(lambda: self.jobs("nextepisode") and self.jobs("nextepisode")[0]["state"] == "done"))
            time.sleep(0.6)                                     # long enough to judge how busy the disks were
            with open(stats, "w") as fh:
                fh.write("   8 0 sda 1 0 0 0 0 0 0 0 0 1400 0\n")
            self.poll()                                         # disk sample, and the still-cached check
            self.assertTrue(wait_for(lambda: self.metrics("retention")))
            self.assertEqual(self.metrics("retention")[-1]["feature"], "nextepisode")
            self.assertTrue(self.metrics("read"))
            # the viewer moves on to the episode that was got ready
            self.session(CAT["episodes"][3], 1000, sid="n2")
            self.poll()
            nxt = self.metrics("next")[-1]
            self.assertTrue(nxt["ready"])                       # its lead-in was cached: it started instantly...
            self.assertFalse(nxt["full"])                       # ...but only the lead-in
            self.assertEqual(nxt["label"], "Show's Name · S02E01")
            end = self.metrics("streamend")[-1]
            self.assertIsNotNone(end["busy_peak"])
        finally:
            pb.RETENTION_AFTER = saved_after
            os.remove(stats)
            self.app.settings.update({"nowplaying": {"enabled": True}, "general": {"no_l2arc": False}})

    def test_next_episode_ready_with_no_cooldown(self):
        # "Was it cached?" comes from the cache log, not the cooldown: with no cooldown, a next
        # episode Prefetch got ready seconds earlier is still ready (it used to count as not)
        self.app.settings.update({"nextepisode": {"libraries": {"2": {"start": "next"}}}, "nowplaying": {"enabled": False},
                                  "general": {"no_l2arc": True, "ram_cooldown_lead_in_minutes": 0, "ram_cooldown_whole_minutes": 0}})
        try:
            self.session(CAT["episodes"][2], 60000, sid="c1")
            self.poll()
            self.assertTrue(wait_for(lambda: self.jobs("nextepisode") and self.jobs("nextepisode")[0]["state"] == "done"))
            self.session(CAT["episodes"][3], 1000, sid="c2")
            self.poll()
            nxt = self.metrics("next")[-1]
            self.assertTrue(nxt["ready"])
            self.assertEqual(nxt["p"], 1.0)
        finally:
            self.app.settings.update({"nowplaying": {"enabled": True},
                                      "general": {"no_l2arc": False, "ram_cooldown_lead_in_minutes": 120, "ram_cooldown_whole_minutes": 180}})

    def test_cached_some_other_way_still_counts(self):
        from types import SimpleNamespace
        app, mon = self.app, self.app.monitor
        path = CAT["episodes"][4]["path"]
        size = CAT["episodes"][4]["size"]
        s = SimpleNamespace(files=[(path, size)], library_key="2", type="episode", kbps=100, label="S02E02",  # lead-in: 1 MiB of 12
                            progress=0.5, rating_key="r42", session_key="z", player="TV", transcoding=False, local=True)
        try:
            app.cachelog.__init__()
            mon._record_skip("z:r42", s, app.settings.get(), 120)
            self.assertFalse(self.metrics("skip")[-1]["cached"])           # nothing read: from disk
            app.cachelog.note(path, whole=True)                               # the Whole File Cache had it
            mon._record_skip("z:r42", s, app.settings.get(), 120)
            self.assertTrue(self.metrics("skip")[-1]["cached"])
            app.cachelog.__init__()
            app.cachelog.note(path, whole=False)                              # only its lead-in:
            mon._record_skip("z:r42", s, app.settings.get(), -120)
            self.assertFalse(self.metrics("skip")[-1]["cached"])           # mid-file isn't covered...
            mon._record_skip("z:r42", SimpleNamespace(**dict(vars(s), progress=0.0001)), app.settings.get(), -600)
            self.assertTrue(self.metrics("skip")[-1]["cached"])            # ...a jump back into the lead-in is
            # Prefetch picked it as next, but the cooldown meant nothing was read: it still counts, fully cached
            app.cachelog.note(path, whole=True)
            mon._expected["r42"] = time.time()
            mon._stream_started(s)
            nxt = self.metrics("next")[-1]
            self.assertEqual((nxt["ready"], nxt["full"], nxt["state"]), (True, True, "logged"))
            before = len(self.metrics("next"))
            mon._stream_started(s)                                            # not picked as next this time: not counted
            self.assertEqual(len(self.metrics("next")), before)
        finally:
            app.cachelog.__init__()

    def test_near_the_end_timing(self):
        from plexcache.playback import Job
        self.assertEqual(Job("nowplaying", "x", "/f", [], 0).offset, 0)
        mon = self.app.monitor
        self.app.settings.update({"nowplaying": {"enabled": False},
                                  "nextepisode": {"libraries": {"2": {"start": "off", "end_at": "80", "end": "next"}}}})
        try:
            self.session(CAT["episodes"][0], int(1200000 * 0.79), sid="t1")    # S01E01 at 79%: not yet
            self.poll()
            self.assertEqual(mon._chains.get("t1", []), [])
            self.session(CAT["episodes"][0], int(1200000 * 0.85), sid="t1")    # at 85%: the next episode, in full
            self.poll()
            self.assertEqual([(j.target, j.slice) for j in mon._chains["t1"]], [("Show's Name · S01E02", False)])
            self.assertTrue(wait_for(lambda: mon._chains["t1"][0].state == "done", 10))
            self.assertEqual(mon._chains["t1"][0].length, CAT["episodes"][1]["size"])
        finally:
            self.app.settings.update({"nowplaying": {"enabled": True}, "nextepisode": {"libraries": {"*": {"start": "off", "end": "none"}}}})


class ServerTests(unittest.TestCase):
    """HTTP routes not covered by AppTests: export, sign-in with Plex, bad requests."""

    @classmethod
    def setUpClass(cls):
        from plexcache.server import serve
        cls.app = _new_app()
        cls.httpd = serve(cls.app, "127.0.0.1", 0)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.plextv = fakes.FakePlexTV()
        import plexcache.plex as plexmod
        cls.saved_tv = plexmod.PLEX_TV
        plexmod.PLEX_TV = cls.plextv.url

    @classmethod
    def tearDownClass(cls):
        import plexcache.plex as plexmod
        plexmod.PLEX_TV = cls.saved_tv
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.plextv.httpd.shutdown()
        cls.plextv.httpd.server_close()

    def req(self, method, path, body=None, ctype="application/json", raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None or method == "POST":
            r.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), e.headers

    def test_healthz_head_and_missing(self):
        status, body, _ = self.req("GET", "/healthz")
        self.assertEqual(json.loads(body)["version"], self.app.version)
        status, body, _ = self.req("HEAD", "/")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(self.req("GET", "/static/nope.js")[0], 404)
        self.assertEqual(self.req("POST", "/api/nope", {})[0], 404)

    def test_export_has_no_secrets(self):
        self.app.settings.update({"plex": {"token": "secret-token"}})
        status, body, headers = self.req("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        data = json.loads(body)
        self.assertEqual((data["plex"]["token"], data["truenas"]["api_key"], data["general"]["client_id"]), ("", "", ""))
        self.assertNotIn(b"secret-token", body)

    def test_sign_in_with_plex(self):
        self.app.settings.update({"plex": {"token": ""}})
        status, body, _ = self.req("POST", "/api/plex/pin", {})
        pin = json.loads(body)
        self.assertEqual(pin["id"], 4242)
        self.assertIn("code=ABCD1234", pin["auth_url"])
        self.plextv.approved = False
        self.assertEqual(json.loads(self.req("POST", "/api/plex/pin/4242", {})[1]), {"done": False})
        self.plextv.approved = True
        self.assertEqual(json.loads(self.req("POST", "/api/plex/pin/4242", {})[1]), {"done": True})
        self.assertEqual(self.app.settings.section("plex")["token"], "good-token")
        self.plextv.fail = True
        try:
            self.assertEqual(self.req("POST", "/api/plex/pin", {})[0], 502)
            self.assertEqual(self.req("POST", "/api/plex/pin/4242", {})[0], 502)
        finally:
            self.plextv.fail = False

    def test_bad_requests(self):
        self.assertEqual(self.req("POST", "/api/test", raw=b"{nope")[0], 400)
        self.assertEqual(self.req("POST", "/api/test", raw=b"{}", ctype="text/plain")[0], 415)
        self.assertEqual(self.req("POST", "/api/test", raw=b"[1, 2]")[0], 400)
        # Too big: refused from the Content-Length alone, before any of the body is read
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        conn.putrequest("POST", "/api/test")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(2 * 1024 * 1024))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_action_routes(self):
        for path in ("/api/test", "/api/checks", "/api/benchmark"):
            status, body, _ = self.req("POST", path, {"rescan": False})
            self.assertEqual((status, json.loads(body)), (200, {"started": True}), path)
        self.assertEqual(json.loads(self.req("POST", "/api/warmup/stop", {})[1]), {"stopping": True})
        status, _, _ = self.req("POST", "/api/settings", {"truenas": {"verify_tls": False}})
        self.assertEqual(status, 200)
        self.app.benchmark_thread.join(30)
        wait_for(lambda: not self.app.checking and not self.app.testing)


class ReportTests(unittest.TestCase):
    """The Activity report: verdict, numbers and what needs attention."""

    def app_with(self, rows):
        app = App(tempfile.mkdtemp(dir=TMP))
        for what, fields in rows:
            app.activity.record(what, **fields)
        return app

    def test_nothing_yet(self):
        from plexcache import report
        r = report.build(App(tempfile.mkdtemp(dir=TMP)))
        self.assertEqual(r["verdict"]["status"], "info")
        self.assertEqual(r["attention"], [])

    def test_a_good_week(self):
        from plexcache import report
        rows = [("stream", {"label": "A"}), ("stream", {"label": "B"}),
                ("skip", {"label": "A", "cached": True, "reason": None, "seconds": 90}),
                ("next", {"label": "B", "ready": True, "lead": 600, "pct": 100, "state": "done"}),
                ("retention", {"label": "A", "pct": 100, "minutes": 30, "feature": "nowplaying"}),
                ("read", {"feature": "nowplaying", "label": "A", "cached": 10 ** 9, "disk": 3 * 10 ** 9}),
                ("streamend", {"label": "A", "seconds": 60, "busy_avg": 12, "busy_peak": 40})]
        r = report.build(self.app_with(rows))
        self.assertEqual(r["verdict"]["status"], "ok")
        self.assertEqual("Last 7 days, 2 streams: nothing to fix.", r["verdict"]["text"])
        t = r["tiles"]
        self.assertEqual((t["skips"]["cached"], t["next"]["lead_min"], t["retention"]["pct"], t["preloaded"]["cached_pct"]),
                         (1, 10, 100, 25))
        self.assertEqual((r["disk"]["avg"], r["disk"]["peak"]), (12, 40))

    def test_what_needs_attention(self):
        from plexcache import report
        rows = [("stream", {"label": "A"}),
                ("skip", {"label": "A", "cached": False, "reason": "only part of the file fits in the RAM limit", "seconds": 90}),
                ("skip", {"label": "A", "cached": False, "reason": "the preload hadn't started yet", "seconds": 90}),
                ("skip", {"label": "A", "cached": False, "reason": "the preload hadn't reached that part yet", "seconds": 90}),
                ("skip", {"label": "A", "cached": False, "reason": "Now-playing preload is off", "seconds": 90}),
                ("next", {"label": "B", "ready": False, "lead": None, "pct": 40, "state": "reading"}),
                ("retention", {"label": "A", "pct": 40, "minutes": 30, "feature": "nowplaying"}),
                ("limit", {"label": "A", "feature": "nowplaying", "pct": 45}),
                ("limit", {"label": "A", "feature": "nowplaying", "pct": 0}),
                ("streamend", {"label": "A", "seconds": 60, "busy_avg": 50, "busy_peak": 95})]
        r = report.build(self.app_with(rows))
        self.assertEqual(r["verdict"]["status"], "warn")
        titles = [a["title"] for a in r["attention"]]
        for want in ("1 skip went to disk: RAM limit full", "1 skip before Now Playing Cache started",
                     "1 skip ahead of Now Playing Cache", "1 skip without Now Playing Cache",
                     "Cached data isn’t staying in RAM (40% still cached)"):
            self.assertIn(want, titles)
        self.assertIn("Disks reached 95% busy while people watched", titles)
        self.assertEqual((r["ram"]["limited"], r["ram"]["no_room"]), (1, 1))

    def test_warnings_busy_disks_and_quiet_weeks(self):
        from plexcache import report
        rows = [("stream", {"label": "A"}), ("streamend", {"label": "A", "seconds": 60, "busy_avg": 50, "busy_peak": 90})]
        r = report.build(self.app_with(rows))
        self.assertNotIn("next episode", str(r["attention"]))                         # (no longer reported)
        self.assertIn("Disks reached 90% busy while people watched", [a["title"] for a in r["attention"]])
        # streams with nothing to report: still a green verdict
        r = report.build(self.app_with([("stream", {"label": "A"})]))
        self.assertEqual(r["verdict"], {"status": "ok", "text": "Last 7 days, 1 stream: nothing to fix."})

    def test_periods_and_chart_buckets(self):
        import datetime as dt
        from plexcache import report
        now = dt.datetime(2026, 3, 15, 13, 30).timestamp()
        app = App(tempfile.mkdtemp(dir=TMP))
        app.activity.metrics = [
            {"what": "stream", "t": now - 3600, "label": "A"},                       # today, 12:00
            {"what": "skip", "t": now - 60, "cached": True, "label": "A"},            # today, 13:00
            {"what": "stream", "t": now - 3 * 86400, "label": "B"},                  # this week
            {"what": "skip", "t": now - 20 * 86400, "cached": False, "reason": "Now-playing preload is off"},
        ]
        # older than a month: already folded into a day summary
        app.activity.daily = {"2025-06-01": report.summarize([{"what": "stream", "t": 0},
                                                               {"what": "retention", "t": 0, "pct": 50}])}
        allr = report.build_all(app, now=now)
        self.assertEqual(list(allr), ["today", "week", "month", "year"])
        self.assertEqual([len(allr[p]["series"]) for p in allr], [14, 7, 30, 12])
        self.assertEqual([allr[p]["streams"] for p in allr], [1, 2, 2, 3])
        self.assertEqual(allr["today"]["span"], "Today")
        self.assertTrue(allr["today"]["verdict"]["text"].startswith("Today, 1 stream"))
        self.assertEqual((allr["today"]["series"][12]["streams"], allr["today"]["series"][13]["skips_cached"]), (1, 1))
        self.assertEqual(allr["today"]["series"][13]["label"], "13:00")
        self.assertEqual({k: v for k, v in allr["month"]["tiles"]["skips"].items() if k != "by_lib"}, {"cached": 1, "total": 2, "expected": 1.0})
        self.assertEqual(allr["year"]["series"][0]["label"], "Apr")
        self.assertEqual(allr["year"]["series"][2]["retention"], 50)  # June's summary
        # Skips per library (each counted by how likely it came from cache), in the total and over time
        app.libraries = [{"key": "1", "title": "Movies"}, {"key": "2", "title": "TV Shows"}]
        app.activity.metrics += [{"what": "skip", "t": now - 30, "cached": True, "label": "C", "p": 0.5, "lib": "2"},
                                 {"what": "skip", "t": now - 20, "cached": True, "label": "C", "p": 1.0, "lib": "2"}]
        today = report.build_all(app, now=now)["today"]
        self.assertEqual(today["tiles"]["skips"]["by_lib"], [{"key": "1", "name": "Movies", "total": 0, "expected": 0},
                                                             {"key": "2", "name": "TV Shows", "total": 2, "expected": 1.5}])
        self.assertEqual(today["series"][13]["skips_by"], {"2": [2, 1.5]})
        merged = report.add(report.summarize(app.activity.metrics[-2:]), report.summarize(app.activity.metrics[-1:]))
        self.assertEqual(merged["skips_by"], {"2": [3, 2.5]})                    # day summaries add up per library
        self.assertEqual(allr["year"]["tiles"]["retention"]["pct"], 50)
        self.assertEqual(allr["week"]["series"][-1]["label"], "Sun")

    def test_old_measurements_roll_up_into_days(self):
        from plexcache import activity, report
        d = tempfile.mkdtemp(dir=TMP)
        a = activity.Activity(d)
        old = time.time() - 40 * 86400
        a.metrics = [{"what": "stream", "t": old}, {"what": "skip", "t": old + 1, "cached": True},
                     {"what": "stream", "t": time.time() - 10}]
        a.daily = {"2001-01-01": report.summarize([])}  # past a year: dropped
        a.record("stream", label="now")
        self.assertEqual(len(a.metrics), 2)
        day = time.strftime("%Y-%m-%d", time.localtime(old))
        self.assertEqual(list(a.daily), [day])
        self.assertEqual((a.daily[day]["streams"], a.daily[day]["skips_cached"]), (1, 1))
        # it survives a restart, and adds to an existing day rather than replacing it
        b = activity.Activity(d)
        self.assertEqual(b.daily[day]["streams"], 1)
        b.metrics.append({"what": "stream", "t": old + 2})
        b.record("stream", label="again")
        self.assertEqual(b.daily[day]["streams"], 2)
        raw, daily = b.history()
        self.assertEqual((len(raw), list(daily)), (3, [day]))

    def test_summaries_add_up(self):
        from plexcache import report
        a = report.summarize([{"what": "streamend", "busy_avg": 10, "busy_peak": 30},
                              {"what": "next", "ready": False, "label": "X", "pct": 20}])
        b = report.summarize([{"what": "streamend", "busy_avg": 20, "busy_peak": 80},
                              {"what": "skip", "cached": False}])
        s = report.add(a, b)
        self.assertEqual((s["busy_n"], s["busy_peak"], s["miss"], s["late"]), (2, 80, {"unknown": 1}, [{"label": "X", "pct": 20}]))
        self.assertEqual(report.add(report.empty(), report.empty())["busy_peak"], None)

    def test_reports_in_state_are_cached(self):
        app = App(tempfile.mkdtemp(dir=TMP))
        first = app.reports()
        self.assertEqual(set(first), {"today", "week", "month", "year"})
        self.assertIs(app.reports(), first)
        app.activity.record("stream", label="A")
        self.assertIsNot(app.reports(), first)
        self.assertEqual(app.reports()["week"]["streams"], 1)


class BlockSizeTests(unittest.TestCase):
    """Do the files on a dataset match its recordsize?"""

    def test_block_sizes_of_files(self):
        d = tempfile.mkdtemp(dir=TMP)
        small, big = os.path.join(d, "small"), os.path.join(d, "big")
        with open(small, "wb") as fh:
            fh.write(b"x" * 100)
        with open(big, "wb") as fh:
            fh.write(b"x" * (3 * 1024 * 1024))
        r = REAL_BLOCK_SIZES([small, big, os.path.join(d, "gone")])
        bs = os.stat(big).st_blksize
        self.assertEqual((r["checked"], r["single"], r["sizes"]), (2, 1, {str(bs): 1}))

    def test_health_compares_files_with_the_setting(self):
        from plexcache import checks
        app = App(tempfile.mkdtemp(dir=TMP))
        old = {"checked": 300, "single": 20, "sizes": {str(128 * 1024): 180, str(1024 * 1024): 100}}
        self.assertEqual(checks._blocks_check("tank/tv", 1024 * 1024, old, "TV"), "warn")  # the cell says how many
        self.assertEqual(checks._files_note(old, 1024 * 1024), "180 of 280 files in 128K")
        # every file is checked against the setting: a few stragglers warn too, and so do larger blocks
        few = {"checked": 300, "single": 0, "sizes": {str(128 * 1024): 3, str(1024 * 1024): 297}}
        self.assertEqual(checks._blocks_check("tank/tv", 1024 * 1024, few), "warn")
        music = {"checked": 300, "single": 50, "sizes": {str(1024 * 1024): 250}}
        self.assertEqual(checks._blocks_check("tank/music", 512 * 1024, music), "warn")
        self.assertEqual(checks._blocks_check("tank/music", 1024 * 1024, music), "pass")
        self.assertEqual(checks._files_note(music, 1024 * 1024), "250 files in 1M")
        self.assertEqual(checks._files_note(None, 1024 * 1024), "")
        self.assertEqual(checks._files_note({"checked": 5, "single": 5, "sizes": {}}, 1024 * 1024), "5 files, each smaller than a record")
        # the page gets the defaults it needs for its "back to default" buttons
        d = app.state()["defaults"]
        self.assertEqual((d["general"]["poll_seconds"], d["general"]["ram_reserve_gb"]), (5, 4))
        self.assertEqual(app.state()["kind_seconds"], {"movie": 60, "tv": 30, "audio": 10})  # each library's default lead-in
        self.assertNotIn("cache_index", config.DEFAULTS["warmup"])  # file indexes are always cached now

    def test_check_every_file(self):
        from plexcache import checks
        d = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(d, "Show", ".hidden"))
        for name, n in (("Show/a.mkv", 3 * 1024 * 1024), ("Show/b.mkv", 3 * 1024 * 1024), ("small.nfo", 10), ("Show/.hidden/x", 9999)):
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(b"x" * n)
        seen = []
        r = zfs.scan_block_sizes(d, progress=seen.append)
        bs = str(os.stat(os.path.join(d, "Show/a.mkv")).st_blksize)
        self.assertEqual((r["checked"], r["single"], r["sizes"], r["complete"]), (3, 1, {bs: 2}, True))
        self.assertEqual(sorted(r["examples"][bs]), ["Show/a.mkv", "Show/b.mkv"])
        self.assertFalse(zfs.scan_block_sizes(d, stop=lambda: True)["complete"])
        self.assertIn("error", zfs.scan_block_sizes(os.path.join(d, "gone")))
        full = {"checked": 1000, "single": 0, "sizes": {"131072": 600, "1048576": 400}, "full": True}
        self.assertEqual(checks._files_note(full, 1024 * 1024), "600 of 1,000 files in 128K")
        self.assertEqual(checks._files_note(dict(full, sizes={"1048576": 1000}), 1024 * 1024), "1,000 files in 1M")
        self.assertEqual(checks._blocks_check("tank/tv", 1024 * 1024, full), "warn")
        # through the app: every health check looks at every file, in its own column
        app = _new_app()
        app.scan_library()
        app.run_checks()
        rows = {r["dataset"]: r for g in app.health["groups"] if g["id"] == "datasets" for r in g["datasets"]}
        tv = next(d for d in app.zfs["datasets"] if d["dataset"] == TV_DS)
        self.assertTrue(tv["blocks"]["full"] and tv["blocks"]["complete"])
        self.assertEqual(tv["blocks"]["checked"], len([n for _, _, fs in os.walk(tv["mountpoint"]) for n in fs if not n.startswith(".")]))
        self.assertIn("files", rows[TV_DS]["file_rs"]["text"])
        self.assertNotIn("block_scans", app.state())

    def test_rewrite_script_rewrites_safely(self):
        import subprocess
        import urllib.request
        srv_app = _new_app()
        from plexcache.server import serve
        httpd = serve(srv_app, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:  # no longer served by the app (it runs as root: fetched over HTTPS from the release instead)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/tools/rewrite_blocks.py", timeout=5).read()
        finally:
            httpd.shutdown()
            httpd.server_close()
        with open(os.path.join(ROOT, "plexcache", "tools", "rewrite_blocks.py"), "rb") as fh:
            body = fh.read()
        self.assertIn(b"def rewrite(", body)
        # Run it against a folder, with a stand-in `zfs` command that reports recordsize=1M.
        d = tempfile.mkdtemp(dir=TMP)
        media, bin_dir = os.path.join(d, "media"), os.path.join(d, "bin")
        os.makedirs(os.path.join(media, "Show"))
        os.makedirs(bin_dir)
        script = os.path.join(d, "rewrite_blocks.py")
        with open(script, "wb") as fh:
            fh.write(body)
        with open(os.path.join(bin_dir, "zfs"), "w") as fh:
            fh.write("#!/bin/sh\ncase \"$*\" in *recordsize*) echo 1048576;; *mountpoint*) echo " + media + ";; *) ;; esac\n")
        os.chmod(os.path.join(bin_dir, "zfs"), 0o755)
        f = os.path.join(media, "Show", "ep1.mkv")
        data = os.urandom(300 * 1024)
        with open(f, "wb") as fh:
            fh.write(data)
        os.utime(f, (1_600_000_000, 1_600_000_000))
        os.chmod(f, 0o640)
        env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"])
        # The downloaded copy runs on its own (as it will in the TrueNAS shell)...
        look = subprocess.run([sys.executable, script, "tank/media"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(look.returncode, 0, look.stderr)
        self.assertIn("1 to rewrite", look.stdout)
        self.assertIn("Nothing was changed", look.stdout)
        ino = os.stat(f).st_ino
        # ...and the rest runs in this process, so test coverage can see it
        run = self.run_rewrite(bin_dir, ["tank/media", "--apply", "--yes", "--mb-per-sec", "0", "--min-free-gb", "0"])
        self.assertIn("Rewrote 1 file(s)", run)
        st = os.stat(f)
        with open(f, "rb") as fh:
            self.assertEqual(fh.read(), data)                       # same content
        self.assertNotEqual(st.st_ino, ino)                         # a new file…
        self.assertEqual((st.st_mtime, st.st_mode & 0o777), (1_600_000_000, 0o640))  # …with the same details
        self.assertEqual(os.listdir(os.path.join(media, "Show")), ["ep1.mkv"])        # no temporary file left


    @staticmethod
    def load_rewrite():
        import importlib.util
        spec = importlib.util.spec_from_file_location("rewrite_blocks", os.path.join(ROOT, "plexcache", "tools", "rewrite_blocks.py"))
        rb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rb)
        return rb

    def run_rewrite(self, bin_dir, args, answer=None):
        """Run rewrite_blocks.main() here, with a stand-in `zfs` on the PATH. Returns what it printed."""
        import contextlib
        import io
        from unittest import mock
        rb = self.load_rewrite()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ["PATH"]}), \
                mock.patch.object(sys, "argv", ["rewrite_blocks.py"] + args), \
                mock.patch("builtins.input", lambda *a: answer or "n"), contextlib.redirect_stdout(out):
            try:
                code = rb.main()
            except SystemExit as e:
                code = e.code
        return f"[exit {code}]\n" + out.getvalue()

    def test_rewrite_script_with_nothing_to_do(self):
        """Files that fit in one block are fine as they are, and a temporary copy left by an interrupted
        run isn't counted: nothing to rewrite, and it stops there."""
        d = tempfile.mkdtemp(dir=TMP)
        media, bin_dir = os.path.join(d, "media"), os.path.join(d, "bin")
        os.makedirs(media)
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "zfs"), "w") as fh:
            fh.write("#!/bin/sh\ncase \"$*\" in *recordsize*) echo 1048576;; *mountpoint*) echo " + media + ";; *) ;; esac\n")
        os.chmod(os.path.join(bin_dir, "zfs"), 0o755)
        for name, size in (("small.mkv", 1000), (".big.mkv.rewrite-tmp", 300 * 1024)):
            with open(os.path.join(media, name), "wb") as fh:
                fh.write(os.urandom(size))
        out = self.run_rewrite(bin_dir, ["tank/media"])
        self.assertIn("[exit 0]", out)
        self.assertIn("1 files: 1 already fine, 0 to rewrite", out)

    def test_rewrite_script_options_and_skips(self):
        rb = self.load_rewrite()
        self.assertEqual(rb.human(1.5e12), "1.5 TB")
        self.assertEqual(rb.human(12), "12 B")
        self.assertEqual((rb.bs_label(1048576), rb.bs_label(131072)), ("1M", "128K"))
        d = tempfile.mkdtemp(dir=TMP)
        media, bin_dir = os.path.join(d, "media"), os.path.join(d, "bin")
        os.makedirs(os.path.join(media, "A"))
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "zfs"), "w") as fh:
            fh.write("#!/bin/sh\ncase \"$*\" in *recordsize*) echo 1048576;; *mountpoint*) echo " + media +
                     ";; *snapshot*) echo tank/media@daily;; *) ;; esac\n")
        os.chmod(os.path.join(bin_dir, "zfs"), 0o755)
        for name in ("one.mkv", "two.mkv", "linked.mkv"):
            with open(os.path.join(media, "A", name), "wb") as fh:
                fh.write(os.urandom(200 * 1024))
        os.link(os.path.join(media, "A", "linked.mkv"), os.path.join(media, "A", "linked-too.mkv"))
        os.symlink(os.path.join(media, "A", "one.mkv"), os.path.join(media, "A", "shortcut.mkv"))  # links are ignored
        # Asking for a bigger block than the dataset has is refused
        self.assertIn("set it first", self.run_rewrite(bin_dir, ["tank/media", "--block", "4M"]))
        # Looks only; mentions snapshots and how long it'll take
        look = self.run_rewrite(bin_dir, ["tank/media", "--block", "1M", "--mb-per-sec", "50"])
        self.assertIn("snapshot", look)
        self.assertIn("hours", look)
        # Saying no at the prompt changes nothing
        self.assertNotIn("Rewrote", self.run_rewrite(bin_dir, ["tank/media", "--apply"], answer="n"))
        # Not enough free space: stops before starting
        self.assertIn("less than", self.run_rewrite(bin_dir, ["tank/media", "--apply", "--yes", "--min-free-gb", "1e9"]))
        # Hard-linked files are left alone; the rest are rewritten
        run = self.run_rewrite(bin_dir, ["tank/media", "--apply", "--yes", "--mb-per-sec", "0",
                                         "--min-free-gb", "0", "--no-verify"])
        self.assertIn("skipped (hard-linked)", run)
        self.assertIn("Rewrote 2 file(s)", run)
        # Not a ZFS dataset
        self.assertIn("isn't on a ZFS dataset", self.run_rewrite(bin_dir, ["/proc/self"]))
        # The pacer slows to the rate asked for
        p = rb.Pacer(1)  # 1 MB/s
        t = time.monotonic()
        p.add(200_000)
        self.assertGreater(time.monotonic() - t, 0.15)

    def test_rewrite_script_edges(self):
        """Finding a folder's dataset from the mount table, files other programs have open (left for later),
        stopping with Ctrl-C, and each reason a file is skipped."""
        import contextlib
        import io
        from types import SimpleNamespace
        rb = self.load_rewrite()
        d = tempfile.mkdtemp(dir=TMP)
        media = os.path.join(d, "media")
        os.makedirs(os.path.join(media, "A"))
        info = os.path.join(d, "mountinfo")
        with open(info, "w") as fh:
            fh.write(f"36 25 0:30 / {os.path.realpath(d)} rw - zfs tank rw\n"
                     f"37 25 0:31 / {os.path.realpath(media)} rw - zfs tank/media rw\n12 1 0:5 / /proc rw - proc proc rw\n")
        with mock.patch.object(rb, "MOUNTINFO", info):
            self.assertEqual(rb.resolve(os.path.join(media, "A")), (os.path.realpath(os.path.join(media, "A")), "tank/media"))
        with mock.patch.object(rb, "MOUNTINFO", os.path.join(d, "none")), self.assertRaises(SystemExit):
            rb.resolve(media)                                                       # no mount table: not ZFS
        with mock.patch.object(rb, "zfs", lambda *a: "none"), self.assertRaises(SystemExit):
            rb.resolve("tank/media")                                                # not mounted
        # open files, from /proc: every process's descriptors
        fds = {"/proc": ["1", "2", "self"], "/proc/1/fd": ["0", "1", "2"]}

        def listdir(p):
            if p in fds:
                return fds[p]
            raise PermissionError(p)

        def stat(p):
            if p.endswith("/2"):
                raise FileNotFoundError(p)
            return SimpleNamespace(st_dev=1, st_ino=int(p[-1]) + 100)
        with mock.patch.object(rb.os.path, "isdir", lambda p: True), mock.patch.object(rb.os, "listdir", listdir), \
                mock.patch.object(rb.os, "stat", stat):
            self.assertEqual(rb.open_files(), {(1, 100), (1, 101)})
        # the run: one file in use, one that fails, then Ctrl-C before the rest
        bin_dir = os.path.join(d, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "zfs"), "w") as fh:
            fh.write("#!/bin/sh\ncase \"$*\" in *recordsize*) echo 1048576;; *mountpoint*) echo " + media + ";; *) ;; esac\n")
        os.chmod(os.path.join(bin_dir, "zfs"), 0o755)
        for name in ("a.mkv", "b.mkv", "c.mkv", "d.mkv"):
            with open(os.path.join(media, "A", name), "wb") as fh:
                fh.write(os.urandom(200 * 1024))
        busy = os.stat(os.path.join(media, "A", "a.mkv"))
        handlers = {}
        calls = {"n": 0}

        def rewrite(path, *a):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk error")
            handlers["int"]()                                                      # Ctrl-C while the second is copied
            return ""
        with mock.patch.object(rb, "open_files", lambda: {(busy.st_dev, busy.st_ino)}), mock.patch.object(rb, "rewrite", rewrite), \
                mock.patch.object(rb.signal, "signal", lambda sig, h: handlers.update(int=h)):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ["PATH"]}), \
                    mock.patch.object(sys, "argv", ["rewrite_blocks.py", "tank/media", "--apply", "--yes", "--mb-per-sec", "0", "--min-free-gb", "0"]), \
                    contextlib.redirect_stdout(out):
                rb.main()
        text = out.getvalue()
        self.assertIn("skipped (in use", text)
        self.assertIn("skipped (disk error)", text)
        self.assertIn("Stopped. Run again to carry on.", text)
        # each reason a file is skipped
        f = os.path.join(media, "A", "b.mkv")
        st, dir_st = os.lstat(f), os.lstat(os.path.dirname(f))
        self.assertIn("can't open its folder", rb.rewrite(os.path.join(d, "nope", "x.mkv"), st, dir_st, rb.Pacer(0), True))
        self.assertEqual(rb.rewrite(f, st, os.lstat(media), rb.Pacer(0), True), "its folder changed since the scan")
        leftover = os.path.join(media, "A", ".b.mkv.rewrite-tmp")
        open(leftover, "w").close()
        self.assertIn("leftover temporary file", rb.rewrite(f, st, dir_st, rb.Pacer(0), True))
        os.remove(leftover)
        with mock.patch.object(rb, "same_content", lambda *a: False):
            self.assertEqual(rb.rewrite(f, st, dir_st, rb.Pacer(0), True), "the copy didn't match the original")
        self.assertFalse(os.path.exists(leftover))                                  # cleaned up
        with open(f, "ab") as fh:
            fh.write(b"more")
        self.assertEqual(rb.rewrite(f, st, dir_st, rb.Pacer(0), True), "it changed since the scan")

    def test_rewrite_refuses_swapped_files_and_folders(self):
        """The script runs as root: a file or folder swapped for a link after the scan is skipped."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("rewrite_blocks", os.path.join(ROOT, "plexcache", "tools", "rewrite_blocks.py"))
        rb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rb)
        d = tempfile.mkdtemp(dir=TMP)
        media, private = os.path.join(d, "media"), os.path.join(d, "private")
        os.makedirs(media)
        os.makedirs(private)
        secret = os.path.join(private, "ep.mkv")
        with open(secret, "wb") as fh:
            fh.write(b"secret" * 1000)
        f = os.path.join(media, "ep.mkv")
        with open(f, "wb") as fh:
            fh.write(b"x" * 6000)
        st, dir_st = os.lstat(f), os.lstat(media)
        pacer = rb.Pacer(0)
        # The file becomes a link to something else
        os.remove(f)
        os.symlink(secret, f)
        why = rb.rewrite(f, st, dir_st, pacer, True)
        self.assertIn("can't open it", why)
        self.assertEqual(sorted(os.listdir(media)), ["ep.mkv"])
        self.assertTrue(os.path.islink(f))
        # The folder becomes a link to another folder holding a file of the same name
        os.remove(f)
        moved = media + "-old"
        os.rename(media, moved)
        os.symlink(private, media)
        why = rb.rewrite(f, st, dir_st, pacer, True)
        self.assertTrue("folder" in why, why)
        self.assertEqual(os.listdir(private), ["ep.mkv"])
        with open(secret, "rb") as fh:
            self.assertEqual(fh.read(), b"secret" * 1000)
        # A different file of the same name, same folder: also skipped
        os.remove(media)
        os.rename(moved, media)
        with open(f, "wb") as fh:
            fh.write(b"y" * 6000)
        self.assertIn("changed since the scan", rb.rewrite(f, st, dir_st, pacer, True))


class StartupTests(unittest.TestCase):
    """An unwritable config folder: explain and wait, rather than crash and restart forever."""

    def test_unwritable_config_waits_with_an_explanation(self):
        import http.client
        from plexcache import __main__ as entry
        d = tempfile.mkdtemp(dir=TMP)
        blocker = os.path.join(d, "config")
        open(blocker, "w").close()  # a file where the folder should be: can't be written into
        self.assertIn("Can't save settings in " + blocker, entry.config_problem(blocker))
        self.assertIsNone(entry.config_problem(os.path.join(d, "ok")))
        stop = threading.Event()
        old = entry.RETRY_SECONDS
        entry.RETRY_SECONDS = 0.2
        result = []
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        t = threading.Thread(target=lambda: result.append(entry.wait_for_config(blocker, "127.0.0.1", port, stop)))
        try:
            t.start()
            self.assertTrue(wait_for(lambda: _can_connect(port), 5))
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            page = resp.read().decode()
            conn.close()
            self.assertEqual(resp.status, 503)
            self.assertIn("give user", page)
            os.remove(blocker)  # fixed: it carries on by itself
            t.join(5)
            self.assertEqual(result, [True])
        finally:
            entry.RETRY_SECONDS = old
            stop.set()
            t.join(5)
        # stopped before it was fixed
        blocker2 = os.path.join(d, "config2")
        open(blocker2, "w").close()
        stop = threading.Event()
        stop.set()
        self.assertFalse(entry.wait_for_config(blocker2, "127.0.0.1", 0, stop))


def _can_connect(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


class WarmupScheduleTests(unittest.TestCase):
    """Each library's warm-up schedule: Default follows the caching mode; the old shared schedule carries over."""

    def test_schedules_per_library(self):
        app = _new_app()
        app.scan_library()
        app.run_checks()
        self.assertEqual(app.cache_mode(), "l2arc")
        kinds = {l["key"] for l in app.libraries if l.get("kind")}
        self.assertEqual(app.warmup_schedules(), {k: "boot" for k in kinds})          # Default with an L2ARC: at boot
        app.settings.update({"warmup": {"libraries": {"2": {"schedule": "weekly", "seconds": 45}, "3": {"schedule": "none"}}}})
        sch = app.warmup_schedules()
        self.assertEqual((sch["2"], sch["3"]), ("weekly", "none"))
        self.assertEqual(app.warmup_libraries("weekly"), {"2"})
        self.assertIsNotNone(app.next_warmup())
        st = app.state()
        self.assertEqual(st["warmup"]["schedules"]["2"], "weekly")
        self.assertIn("3", [r["key"] for r in st["plan"]["libraries"]])              # sizing covers every library's lead-ins
        self.assertEqual(next(r for r in st["plan"]["libraries"] if r["key"] == "2")["set_sec"], 45)
        # RAM only: no warm-ups, and the weekly one is noted as a fallback
        app.settings.update({"general": {"no_l2arc": True}})
        self.assertEqual(set(app.warmup_schedules().values()), {"none"})
        self.assertIn("warmup.2.schedule", [f["id"] for f in app.fallbacks.recent()])
        self.assertIsNone(app.next_warmup())
        app.settings.update({"general": {"no_l2arc": False}})
        for bad in ({"2": {"schedule": "hourly"}}, {"2": {"seconds": -1}}, {"2": {"seconds": 181}}, {"2": {"seconds": 2.5}}, {"2": {"when": 1}}):
            with self.assertRaises(config.SettingsError):
                app.settings.update({"warmup": {"libraries": bad}})

    def test_old_shared_schedule_carries_over(self):
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"warmup": {"enabled": True, "schedule": "weekly", "every_hours": 6}}, fh)
        st = config.Settings(d)
        w = st.section("warmup")
        self.assertNotIn("schedule", w)
        self.assertEqual(w["libraries"], {"*": {"schedule": "weekly"}})
        app = _new_app()
        st.update({"plex": app.settings.section("plex")})
        app.settings = st
        app.scan_library()                                                           # once the libraries are known...
        libs = st.section("warmup")["libraries"]
        self.assertNotIn("*", libs)
        self.assertEqual({k: v["schedule"] for k, v in libs.items()}, {l["key"]: "weekly" for l in app.libraries if l.get("kind")})


class AppEdgeTests(unittest.TestCase):
    def test_small_app_paths(self):
        """Less common paths through the app: no Host header, Prefetch choices that aren't an object
        or are the old shared ones, unknown libraries counting as cacheable, nothing to measure yet,
        no library prefetching, one warm-up at a time, and settings files that can't be written."""
        from plexcache import app as appmod
        self.assertEqual(appmod._split_host(None), (None, None))
        app = _new_app()
        with self.assertRaises(config.SettingsError):
            app.check_prefetch_libraries("everything")
        app.check_prefetch_libraries({"*": {"start": "off"}})                   # the old shared choice: fine
        self.assertTrue(app.library_cacheable(""))
        self.assertTrue(app.library_cacheable("no-such-library"))
        app.record_coverage(force=True)                                          # no files yet: nothing to record
        app.scan_library()
        app.settings.update({"nextepisode": {"libraries": {lib["key"]: {"start": "off", "end": "none"} for lib in app.libraries
                                                           if lib["kind"]}}})
        self.assertFalse(app.prefetch_active())
        app.settings.update({"general": {"ram_cooldown_lead_in_minutes": config.AUTO}})
        self.assertGreater(app.cooldown_seconds(False), 0)
        app.warmup_run = object()                                                # one warm-up at a time
        self.assertFalse(app.start_warmup(manual=True))
        app.check_next_day()                                                     # (waits for the warm-up)
        app.warmup_run = None
        os.makedirs(os.path.join(app.config_dir, "blocked.json"))
        with self.assertLogs("plexcache", level="WARNING"):
            app._save_json("blocked.json", {"a": 1})

    """Start-up, scheduling, connection lights and fallbacks."""

    def test_start_and_scheduler(self):
        import plexcache.app as appmod
        app = _new_app(warmup={"libraries": {"2": {"schedule": "daily"}}})
        app.scan_library()
        app.run_checks()                      # finds the L2ARC: without one, a daily warm-up can't run
        started = []
        app.start_warmup = lambda manual, libs=None, why=None: started.append(manual) or self.assertEqual(libs, {"2"})
        waits = iter([False, True])
        app.stopping.wait = lambda t: next(waits)
        real_due, real_time = appmod.due, appmod.time
        appmod.due = lambda *a: True
        try:
            app._scheduler_loop()
        finally:
            appmod.due = real_due
        self.assertEqual(started, [False])
        # A full start: monitor, scheduler and first checks run in the background
        app2 = _new_app()
        app2.start()
        try:
            self.assertTrue(wait_for(lambda: app2.health is not None, 20))
        finally:
            app2.stopping.set()
            app2.monitor.stop.set()

    def test_connection_lights(self):
        app = App(tempfile.mkdtemp(dir=TMP))
        c = app.test_connections()
        self.assertEqual(c["plex_server"]["text"], "Plex server address needed")
        app.settings.update({"plex": {"url": PLEX.url, "token": "good-token"}})
        c = app.test_connections()
        self.assertEqual(c["summary"]["status"], "warn")
        self.assertIn("TrueNAS is optional", c["summary"]["text"])
        app.settings.update({"truenas": {"url": "https://127.0.0.1:1", "api_key": "x"}})
        self.assertEqual(app.test_connections()["truenas_server"]["text"], "TrueNAS not found")
        app.settings.update({"truenas": {"url": TRUENAS.url, "api_key": "wrong"}})
        self.assertEqual(app.test_connections()["truenas_key"]["text"], "TrueNAS API key not accepted")
        app.probe_zfs()
        self.assertIsNotNone(app.zfs["truenas_error"])

    def test_scan_error_and_warmup_stops(self):
        app = _new_app(plex={"token": "wrong"})
        app.scan_library()
        self.assertTrue(app.scan_error)
        app._warmup_worker(manual=True)
        msgs = [e["message"] for e in app.activity.snapshot()["events"]]
        self.assertTrue(any("couldn’t read libraries" in m for m in msgs))

    def test_warmup_skipped_without_l2arc_and_seconds_reduced(self):
        app = _new_app()
        app.scan_library()
        app.run_checks()
        app.probe_zfs = lambda: None
        app.zfs["pools"]["tank"] = dict(app.zfs["pools"]["tank"], l2_online=0, l2_total=0)
        app._warmup_worker(manual=True)
        msgs = [e["message"] for e in app.activity.snapshot()["events"]]
        self.assertTrue(any("skipped: no L2ARC" in m for m in msgs))
        # an L2ARC too small for the settings: seconds are cut down, and it says so
        import plexcache.app as appmod
        app.zfs["pools"]["tank"] = dict(app.zfs["pools"]["tank"], l2_online=1, l2_total=10 ** 9)
        real_plan = appmod.plan
        appmod.plan = lambda *a, **kw: dict(real_plan(*a, **kw), status="reduced")
        try:
            app._warmup_worker(manual=True)
        finally:
            appmod.plan = real_plan
        msgs = [e["message"] for e in app.activity.snapshot()["events"]]
        self.assertTrue(any("L2ARC too small" in m for m in msgs))

    def test_warmup_pause_is_logged_and_failures_dont_crash(self):
        import plexcache.app as appmod
        app = _new_app()
        app.scan_library()
        app.run_checks()
        app.probe_zfs = lambda: None
        calls = iter(["a player was buffering (x)", None])

        class P:
            def __init__(self, *a, **k):
                pass

            def reason(self):
                return next(calls, None)

            def mark_idle(self):
                pass
        real = appmod.L2arcPauses
        appmod.L2arcPauses = P
        try:
            app._warmup_worker(manual=True)
        finally:
            appmod.L2arcPauses = real
        msgs = [e["message"] for e in app.activity.snapshot()["events"]]
        self.assertTrue(any(m == "paused: a player was buffering (x)" for m in msgs))
        self.assertTrue(any(m == "carrying on" for m in msgs))
        app.scan_library = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        with self.assertLogs("plexcache", level="ERROR"):        # logged with its traceback, not raised
            app._warmup_worker(manual=True)
        msgs = [e["message"] for e in app.activity.snapshot()["events"]]
        self.assertTrue(any("failed: boom" in m for m in msgs))

    def test_benchmark_with_nothing_to_read(self):
        app = _new_app()
        app.run_benchmark()
        app.benchmark_thread.join(30)
        self.assertIn("No readable media files", app.benchmark["error"])

    def test_fallbacks(self):
        app = App(tempfile.mkdtemp(dir=TMP))
        # no datasets known: the general disk speed is used
        self.assertEqual(app.disk_speed_for_library("1"), app.settings.section("general")["hdd_mb_per_sec"])
        self.assertEqual(app.fastest_cached_disk(), app.settings.section("general")["hdd_mb_per_sec"])
        # no pools from TrueNAS: L2ARC is judged from the ARC statistics
        app.zfs = {"arc": {"l2_present": True}}
        self.assertEqual(app.l2arc_status(), "present")
        app.zfs = {}
        self.assertEqual(app.l2arc_status(), "unknown")
        # ZFS statistics without free memory, then none at all: /proc/meminfo is used
        meminfo = os.path.join(os.environ["PLEXCACHE_PROC"], "meminfo")
        with open(meminfo, "w") as fh:
            fh.write("MemTotal:       32000000 kB\nMemFree:         4000000 kB\n")
        real = zfs.arcstats
        try:
            zfs.arcstats = lambda: {k: v for k, v in real().items() if k != "memory_free_bytes"}
            b = app.ram_budget()
            self.assertEqual(b["available"], min(56 * 2 ** 30, 48 * 2 ** 30 + 4_000_000 * 1024))
            zfs.arcstats = lambda: None
            b = app.ram_budget()
            self.assertEqual((b["source"], b["cache_max"]), ("ram", 32_000_000 * 1024 // 2))
        finally:
            zfs.arcstats = real
            os.remove(meminfo)
        # the browser's address only triggers auto-detection once
        seen = []
        app._autodetect = seen.append
        app.note_host("192.0.2.1:8642")
        app.note_host("192.0.2.1:8642")
        self.assertEqual(app.host, "192.0.2.1")
        self.assertTrue(wait_for(lambda: seen == ["192.0.2.1"]))

    def test_index_summary(self):
        from plexcache.app import index_summary
        text = index_summary({"index": {"movie": {"files": 3, "bytes": 2e8, "largest": 2e6, "mp4_at_end": 1},
                                        "tv": {"files": 0, "bytes": 0, "largest": 0, "mp4_at_end": 0},
                                        "audio": {"files": 0, "bytes": 0, "largest": 0, "mp4_at_end": 0}},
                              "by_kind": {"movie": {"files": 3}, "tv": {"files": 5}, "audio": {"files": 0}}})
        self.assertIn("movies: 3 of 3 had some, 0.2 GB (largest 2.0 MB)", text)
        self.assertIn("TV episodes: none outside the lead-ins", text)
        self.assertIn("1 MP4 file keeps its index at the end", text)

    def test_main_entry_point(self):
        import plexcache.__main__ as main_mod
        served = {}

        class FakeHTTPD:
            def serve_forever(self):
                served["ran"] = True
                raise KeyboardInterrupt

            def shutdown(self):
                pass
        real_serve, real_start = main_mod.serve, main_mod.App.start
        main_mod.serve = lambda app, host, port: (served.update(app=app, port=port), FakeHTTPD())[1]
        main_mod.App.start = lambda self: None
        os.environ["PLEXCACHE_CONFIG"] = tempfile.mkdtemp(dir=TMP)
        os.environ["PLEXCACHE_PORT"] = "9999"
        import logging
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        try:
            # main() sets up logging to the screen; catch what it says instead of printing it mid-progress-bar
            with self.assertLogs("plexcache", "WARNING") as logs:
                self.assertEqual(main_mod.main(), 0)
            self.assertTrue(any("login is off" in m for m in logs.output))
        finally:
            root.handlers[:] = saved_handlers                       # main() sets up logging; keep test output quiet
            root.setLevel(saved_level)
            main_mod.serve, main_mod.App.start = real_serve, real_start
            del os.environ["PLEXCACHE_CONFIG"], os.environ["PLEXCACHE_PORT"]
        self.assertEqual((served["port"], served["ran"]), (9999, True))

    def test_main_entry_point_other_starts(self):
        """No usable config folder (it waits, then stops cleanly), the example password still set (said
        in the log), and a stop signal shutting everything down."""
        import logging
        import signal
        import plexcache.__main__ as main_mod
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        handlers = {}
        stopped = []

        class FakeHTTPD:
            def serve_forever(self):
                handlers[signal.SIGTERM]()                          # TrueNAS stops the app
                self.done.wait(10)

            def shutdown(self):
                stopped.append(True)
                self.done.set()
        FakeHTTPD.done = threading.Event()
        env = {"PLEXCACHE_CONFIG": tempfile.mkdtemp(dir=TMP), "PLEXCACHE_PORT": "9998",
               "PLEXCACHE_USERNAME": "admin", "PLEXCACHE_PASSWORD": "change-me"}
        try:
            with mock.patch.dict(os.environ, env), mock.patch.object(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h)):
                with mock.patch.object(main_mod, "wait_for_config", lambda *a: False):
                    self.assertEqual(main_mod.main(), 0)
                with mock.patch.object(main_mod, "serve", lambda app, host, port: FakeHTTPD()), \
                        mock.patch.object(main_mod.App, "start", lambda self: None), \
                        self.assertLogs("plexcache", "WARNING") as logs:
                    self.assertEqual(main_mod.main(), 0)
            self.assertTrue(any("still the default" in m for m in logs.output))
            self.assertEqual(stopped, [True])
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)



class _RawWS:
    """A one-shot plain-text WebSocket server that sends scripted frames after the handshake."""

    def __init__(self, frames=(), handshake=None, raw_reply=None):
        import base64, hashlib, socket
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.url = f"ws://127.0.0.1:{self.sock.getsockname()[1]}/api/current"
        self.received = []

        def serve():
            conn, _ = self.sock.accept()
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(4096)
            if raw_reply is not None:
                conn.sendall(raw_reply)
            else:
                key = [ln.split(":", 1)[1].strip() for ln in buf.decode().split("\r\n") if ln.lower().startswith("sec-websocket-key")][0]
                accept = handshake or base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
                for f in frames:
                    conn.sendall(f)
                try:
                    self.received.append(conn.recv(4096))
                except OSError:
                    pass
            time.sleep(0.2)
            conn.close()
            self.sock.close()
        threading.Thread(target=serve, daemon=True).start()


class WebSocketTests(unittest.TestCase):
    def test_frames(self):
        from plexcache.wsclient import WebSocket, encode_frame
        big = "x" * 70000                                         # needs the 64-bit length form
        frames = [encode_frame(0x9, b"hi", mask=False),            # ping: answered with a pong
                  encode_frame(0xA, b"", mask=False),              # unsolicited pong: ignored
                  encode_frame(0x1, b"masked!", mask=True),        # masked text
                  encode_frame(0x1, big.encode(), mask=False),
                  encode_frame(0x8, b"", mask=False)]              # close
        srv = _RawWS(frames)
        ws = WebSocket(srv.url, timeout=5)
        self.assertEqual(ws.recv_text(), "masked!")
        self.assertEqual(ws.recv_text(), big)
        with self.assertRaises(Exception) as cm:
            ws.recv_text()
        self.assertIn("closed", str(cm.exception))
        ws.close()
        ws.close()                                                 # twice is fine

    def test_handshake_problems(self):
        from plexcache.wsclient import WebSocket, WebSocketError
        with self.assertRaises(WebSocketError):
            WebSocket("http://example.invalid/")
        for kwargs, words in (({"raw_reply": b"HTTP/1.1 403 Forbidden\r\n\r\n"}, "refused"),
                              ({"handshake": "wrong"}, "handshake"),
                              ({"raw_reply": b"HTTP/1.1 101 OK\r\n"}, "handshake"),
                              ({"raw_reply": b"x" * 70000}, "handshake")):
            srv = _RawWS(**kwargs)
            with self.assertRaises(WebSocketError, msg=words) as cm:
                WebSocket(srv.url, timeout=5)
            self.assertIn(words, str(cm.exception))


class TrueNASEdgeTests(unittest.TestCase):
    def test_probe_survives_a_silent_tls_port(self):
        """Something accepts the connection but never answers TLS: that's "not TrueNAS", not a crash."""
        import socket
        from plexcache.truenas import probe
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            self.assertFalse(probe(f"https://127.0.0.1:{srv.getsockname()[1]}", timeout=1))
        finally:
            srv.close()
        # and auto-detect, which runs it in the background, logs any surprise instead of dying
        app = App(tempfile.mkdtemp(dir=TMP))
        app._autodetect_inner = lambda host: 1 / 0
        with self.assertLogs("plexcache", level="ERROR"):
            app._autodetect("192.0.2.1")

    def test_connection_problems(self):
        """A connection that breaks mid-way (also during the login), a dataset TrueNAS doesn't list, and
        an API key that can't see the apps' YAML: an error or a fallback each time, never a crash."""
        from plexcache import truenas

        class Broken:
            def __init__(self, *a, **kw):
                self.closed = False

            def send_text(self, text):
                raise truenas.WebSocketError("connection reset")

            def recv_text(self):
                return ""

            def close(self):
                self.closed = True
        with mock.patch.object(truenas, "WebSocket", Broken):
            self.assertFalse(truenas.probe("https://nas.local"))
            with self.assertRaises(TrueNASError) as cm:
                TrueNASClient("https://nas.local", "key")
            self.assertIn("connection problem: connection reset", str(cm.exception))
        with TrueNASClient(TRUENAS.url, "good-key") as tn:
            calls = []

            def call(method, params=None):
                calls.append(method)
                if method == "app.query" and params:
                    raise TrueNASError("not allowed")
                return [] if method.endswith("dataset.query") else [{"name": "plex-cache"}]
            with mock.patch.object(tn, "call", call):
                self.assertEqual(tn.dataset_cache_props("tank/gone"), None)
                self.assertIn("no dataset tank/gone", " ".join(tn.last_props_raw))
                self.assertEqual(tn.apps(config=True), [{"name": "plex-cache"}])     # without the YAML, then
            self.assertEqual(calls[-2:], ["app.query", "app.query"])

    def test_errors_and_helpers(self):
        from plexcache.truenas import parse_size, probe
        with TrueNASClient(TRUENAS.url, "good-key") as tn:
            with self.assertRaises(TrueNASError) as cm:
                tn.call("test.error")
            self.assertIn("no such thing", str(cm.exception))
            with self.assertRaises(TrueNASError) as cm:
                tn.call("test.garbage")
            self.assertIn("isn’t JSON", str(cm.exception))
            self.assertEqual(tn.disk_sizes([]), {})
            self.assertIsNone(tn.pool("nope") if False else None)
        with self.assertRaises(TrueNASError):
            TrueNASClient("", "")
        with self.assertRaises(TrueNASError):
            TrueNASClient(TRUENAS.url, "wrong-key")
        self.assertFalse(probe("https://127.0.0.1:1", timeout=1))
        self.assertEqual([parse_size(v) for v in ("128K", "1M", "2G", "4096", "1.5MB", None, "lots")],
                         [131072, 1048576, 2 * 1024 ** 3, 4096, 1572864, None, None])
        one = describe_pool({"name": "p", "topology": {"data": [{"type": "DISK", "disk": "sdz"}]}})
        self.assertEqual((one["disks"], one["data_disks"]), (["sdz"], 1))


class SmallModuleTests(unittest.TestCase):
    """Fallbacks and error branches in the helper modules."""

    def test_scheduler(self):
        from plexcache.scheduler import due, next_run
        now = dt.datetime(2026, 10, 13, 12, 0)
        weekly = {"schedule": "weekly", "weekday": now.weekday(), "time": "09:00"}
        self.assertEqual(next_run(weekly, now, None), dt.datetime(2026, 10, 20, 9, 0))   # today's 9:00 has passed
        self.assertFalse(due({"time": "09:00", "weekday": 0}, now, None, now, "none"))

    def test_config_validation(self):
        d = tempfile.mkdtemp(dir=TMP)
        s = config.Settings(d)
        bad = [{"warmup": {"time": "25:00"}}, {"warmup": {"pacing_percent": "lots"}}, {"general": {"excluded_libraries": "1"}},
               {"general": {"disk_speeds": []}}, {"general": {"disk_speeds": {"a": {"mb_s": "x"}}}},
               {"general": {"path_map": "x"}}, {"nope": {}}, {"warmup": {"nope": 1}}, {"warmup": {"pause_disk_busy": "x"}}, "x"]
        for patch in bad:
            with self.assertRaises(config.SettingsError, msg=patch):
                s.update(patch)
        s.update({"general": {"disk_speeds": {"tank/a": {"mb_s": 200}, "tank/b": None}, "debug": True},
                  "plex": {"token_set": True}, "warmup": {"time": "06:30"}})
        self.assertEqual(s.section("general")["disk_speeds"], {"tank/a": {"mb_s": 200, "measured_at": None}})
        self.assertTrue(s.section("general")["debug"])
        self.assertEqual(config.normalize_url("plex", "  "), "")
        os.environ["PLEX_URL"] = "10.0.0.9"
        try:
            s2 = config.Settings(tempfile.mkdtemp(dir=TMP))
        finally:
            del os.environ["PLEX_URL"]
        self.assertEqual(s2.section("plex")["url"], "http://10.0.0.9:32400")

    def test_reader_errors_and_pacing(self):
        from plexcache.reader import Pacer, Stopped, benchmark_file, read_head, read_range
        missing = os.path.join(TMP, "missing.bin")
        self.assertTrue(read_range(missing, 0, 10).error)
        self.assertTrue(benchmark_file(missing, 10 * MiB).error)
        path = CAT["movies"][0]["path"]
        self.assertEqual(read_head(path, MiB).bytes, MiB)
        stop = threading.Event()
        idle = []
        p = Pacer(50, stop)
        p.before_wait = lambda: idle.append(1)
        res = read_range(path, 0, 40 * MiB, p)                   # paced: waits between chunk groups
        self.assertEqual(res.bytes, 40 * MiB)
        self.assertTrue(idle)
        stop.set()
        with self.assertRaises(Stopped):
            p.check()
        waits = iter([True])
        held = Pacer(50, threading.Event(), pause=lambda: next(waits, False))
        held.stop.wait = lambda t: True                          # stopped while paused
        with self.assertRaises(Stopped):
            held.check()
        # A share of disk time: 10% busy means waiting 9 s after a 1 s read
        self.assertAlmostEqual(Pacer(10).wait_for(1.0, MiB), 9.0)
        # A speed (L2ARC pacing): 100 MB from the disks at 10 MB/s takes 10 s in all
        fill = Pacer(10, rate=10e6)
        fill.disk_mb_s = 150
        self.assertAlmostEqual(fill.wait_for(2.0, 100 * 10 ** 6), 8.0)
        self.assertEqual(fill.wait_for(0.01, 100 * MiB), 0.0)        # 10 GiB/s: came from cache, nothing to copy
        self.assertEqual(fill.wait_for(20.0, 100 * 10 ** 6), 0.0)    # already slower than the limit

    def test_zfs_unreadable(self):
        real = zfs.PROC, zfs.SYS
        zfs.PROC = zfs.SYS = os.path.join(TMP, "no-such-dir")
        try:
            self.assertIsNone(zfs.arcstats())
            self.assertEqual((zfs.meminfo(), zfs.disk_busy_ms(), zfs.mounts()), ({}, {}, []))
            self.assertIsNone(zfs.module_param("l2arc_write_max"))
        finally:
            zfs.PROC, zfs.SYS = real
        self.assertIsNone(zfs.arc_summary(None))

    def test_activity_history_survives_restart_and_bad_disk(self):
        from plexcache.activity import Activity
        d = tempfile.mkdtemp(dir=TMP)
        a = Activity(d)
        a.event("test", "hello")
        a.add_run({"started": 1, "result": "ok"})
        b = Activity(d)
        self.assertEqual(b.runs[0]["started"], 1)
        self.assertEqual(b.snapshot()["events"][0]["message"], "hello")
        b.dir = os.path.join(d, "missing", "deeper")              # can't save: logged, not fatal
        b.path = os.path.join(b.dir, "activity.json")
        with self.assertLogs("plexcache", level="WARNING"):
            b.add_run({"started": 2})

    def test_mapping_edges(self):
        from plexcache import mapping
        d = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(d, "a", "b"))
        old = mapping.MAX_CANDIDATES
        mapping.MAX_CANDIDATES = 2
        try:
            self.assertEqual(len(mapping.candidate_folders([d])), 2)
        finally:
            mapping.MAX_CANDIDATES = old
        self.assertEqual(mapping.candidate_folders([os.path.join(d, "gone")]), [os.path.join(d, "gone")])
        self.assertFalse(mapping._readable(os.path.join(d, "nope")))

    def test_layout_odd_files(self):
        from plexcache import layout
        d = tempfile.mkdtemp(dir=TMP)
        # an EBML header with no Segment after it
        p = os.path.join(d, "noseg.mkv")
        with open(p, "wb") as fh:
            fh.write(fakes.ebml("1A45DFA3", b"") + fakes.ebml("1F43B675", b"") + b"\x00" * 64)
            fh.truncate(9 * MiB)
        self.assertEqual(layout.tail_ranges(p, 9 * MiB, 4 * MiB)[1].get("note"), "no segment")
        # an MP4 whose last box runs to the end of the file (size 0) and has no moov
        p = os.path.join(d, "open.mp4")
        with open(p, "wb") as fh:
            fh.write(struct.pack(">I4s", 0, b"mdat"))
            fh.truncate(9 * MiB)
        self.assertEqual(layout.tail_ranges(p, 9 * MiB, 4 * MiB)[0], [])
        with self.assertRaises(ValueError):
            layout._vint(b"", 0, True)
        with self.assertRaises(ValueError):
            layout._vint(b"\x00\x00", 0, True)

    def test_zfs_readers_take_odd_input(self):
        """The /proc readers skip what they can't parse, and the dataset scan skips links and files
        that vanish mid-scan, reporting progress every 500 files."""
        from plexcache import zfs
        d = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(d, "spl/kstat/zfs"))
        os.makedirs(os.path.join(d, "self"))
        for name, text in (("spl/kstat/zfs/arcstats", "h1\nh2\nsize 4 1024\nc_max 4 lots\n"), ("stat", "cpu 1 2 3\nbtime 1700000000\n"),
                           ("self/mountinfo", "too short - x\n36 35 0:1 / /data rw - zfs tank/data rw\n")):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text)
        with mock.patch.object(zfs, "PROC", d):
            self.assertEqual(zfs.arcstats(), {"size": 1024})
            self.assertEqual(zfs.boot_time(), 1700000000.0)
            self.assertEqual([(m["mountpoint"], m["source"]) for m in zfs.mounts()], [("/data", "tank/data")])
        with mock.patch.object(zfs, "PROC", os.path.join(d, "nothing")):
            self.assertIsNone(zfs.own_container_id())
        self.assertIsNone(zfs.recordsize(None))
        root = tempfile.mkdtemp(dir=TMP)
        for i in range(501):
            open(os.path.join(root, f"f{i}"), "w").close()
        os.symlink(os.path.join(root, "f0"), os.path.join(root, "link"))
        os.makedirs(os.path.join(root, "gone-dir"))
        open(os.path.join(root, "gone-file"), "w").close()
        real = os.lstat

        def lstat(path, *a, **kw):
            if os.path.basename(path).startswith("gone-"):
                raise FileNotFoundError(path)
            return real(path, *a, **kw)
        seen = []
        with mock.patch.object(zfs.os, "lstat", lstat):
            got = zfs.scan_block_sizes(root, progress=seen.append)
        self.assertEqual((got["checked"], seen, got["complete"]), (501, [500], True))

    def test_layout_damaged_files(self):
        """Damaged or unusual files never raise and never send caching off reading nonsense: parts of
        unknown size, garbage where a part should be, a SeekHead pointing at the wrong thing, MP4s
        without their video or with 64-bit sizes — and no more than 64 MiB is ever taken from the end."""
        from plexcache import layout
        d = tempfile.mkdtemp(dir=TMP)
        E, UNKNOWN = fakes.ebml, b"\x01" + b"\xff" * 7
        hdr = E("1A45DFA3", E("4282", b"matroska"))
        seg_hdr = bytes.fromhex("18538067") + UNKNOWN
        seg = len(hdr) + len(seg_hdr)
        seek = lambda eid, at: E("4DBB", E("53AB", bytes.fromhex(eid)) + E("53AC", (at - seg).to_bytes(8, "big")))

        def mkv(name, body, size=None, at=()):
            path = os.path.join(d, name)
            with open(path, "wb") as fh:
                fh.write(hdr + seg_hdr + body)
                for off, data in at:
                    fh.seek(off)
                    fh.write(data)
                if size:
                    fh.truncate(size)
            return path, os.path.getsize(path)
        info = E("1549A966", b"\x00" * 20)
        # no cluster before the end of the file; a part of unknown size; garbage where the next part should be
        for body in (info, info + bytes.fromhex("1654AE6B") + UNKNOWN + b"\x00" * 64, info + b"\x00" * 8):
            path, size = mkv("v.mkv", body)
            ranges, got = layout.tail_ranges(path, size, 0)
            self.assertEqual((ranges, got["video_start"]), ([], None))
        # more parts in front than any real file has: given up on
        path, size = mkv("many.mkv", E("EC", b"") * (layout.MAX_LEVEL1 + 1) + E("1F43B675", b"\x00" * 64))
        self.assertIsNone(layout.tail_ranges(path, size, 0)[1]["video_start"])
        self.assertEqual(layout.tail_ranges(path, size, size), ([], {"format": "small"}))  # all of it read from the start anyway
        # garbage right after the segment starts: not parsed, nothing taken
        path, size = mkv("g.mkv", b"\x00" * 64)
        self.assertEqual(layout.tail_ranges(path, size, 0)[1]["format"], "unparsed")
        # a SeekHead whose entries are cut short (unknown sizes) lists nothing
        for bad in (bytes.fromhex("4DBB") + UNKNOWN, E("4DBB", bytes.fromhex("53AB") + UNKNOWN)):
            self.assertEqual(layout._seekhead_entries(bad, 0, len(bad)), [])
        # a SeekHead pointing at garbage (a second SeekHead, the Cues) or at another part (Tags): all skipped
        far = 2 * MiB
        heads = E("114D9B74", seek("114D9B74", far) + seek("1C53BB6B", far + 100) + seek("1254C367", far + 200))
        path, size = mkv("s.mkv", heads + info + E("1F43B675", b"\x00" * 64), size=far + 4096,
                         at=((far, b"\x00" * 16), (far + 100, b"\x00" * 16), (far + 200, E("EC", b"\x00" * 8))))
        ranges, got = layout.tail_ranges(path, size, 0)
        self.assertEqual((ranges, got["parts"]), ([], {}))
        # more metadata at the end than is ever cached: the first 64 MiB of it
        cues_at, tags_at = 10 * MiB, 100 * MiB
        cues = bytes.fromhex("1C53BB6B") + b"\x01" + (65 * MiB).to_bytes(7, "big")
        heads = E("114D9B74", seek("1C53BB6B", cues_at) + seek("1254C367", tags_at))
        path, size = mkv("big.mkv", heads + info + E("1F43B675", b"\x00" * 64), size=tags_at + 4096,
                         at=((cues_at, cues), (tags_at, E("1254C367", b"\x00" * 100))))
        ranges, got = layout.tail_ranges(path, size, 0)
        self.assertEqual((ranges, got["bytes"]), ([(cues_at, layout.MAX_TAIL)], layout.MAX_TAIL))

        def mp4(name, boxes, size=None):
            path = os.path.join(d, name)
            with open(path, "wb") as fh:
                fh.write(b"".join(boxes))
                if size:
                    fh.truncate(size)
            return path, os.path.getsize(path)
        box = lambda kind, body=b"": struct.pack(">I4s", 8 + len(body), kind) + body
        ftyp, moov = box(b"ftyp", b"isom" + b"\x00" * 4), box(b"moov", b"\x00" * 8)
        # a 64-bit box header cut off by the end of the file
        path, size = mp4("cut.mp4", [ftyp, struct.pack(">I4s", 1, b"mdat")])
        self.assertEqual(layout.tail_ranges(path, size, 0)[1]["note"], "no moov box found")
        # the index at the front, then no video: past the end, a box too small to be real, too many boxes
        for boxes in ([ftyp, moov, struct.pack(">I4sQ", 1, b"free", 4000)], [ftyp, moov, struct.pack(">I4s", 4, b"free")],
                      [ftyp, moov] + [box(b"free")] * (layout.MAX_BOXES + 1)):
            path, size = mp4("front.mp4", boxes, size=4000)
            ranges, got = layout.tail_ranges(path, size, 1000)
            self.assertEqual((ranges, got["index_at"], got["video_start"]), ([], "start", None))


    def test_mkv_damaged_files(self):
        """The optimizer's own MKV reading refuses what it can't rewrite safely, with a reason: cut-off or
        malformed elements, no Segment, parts of unknown size, no video, a Cluster after the index, a
        second set of attachments — and skips what doesn't concern it (other elements in a SeekHead,
        a Tags or Tracks element, or a track's encodings)."""
        from plexcache.optimize import mkv
        d = tempfile.mkdtemp(dir=TMP)
        bad = mkv.NotMatroska
        for b, marker in ((b"", True), (b"\x00", True), (b"\x40", True)):
            with self.assertRaises(bad):
                mkv._vint(b, 0, marker)
        self.assertEqual(mkv._vint(b"\xff", 0, False), (-1, 1))                 # an unknown size
        with self.assertRaises(bad):
            list(mkv._children(b"\xec\xff"))                                   # ... inside a master element
        self.assertEqual(mkv._void(5), b"\xec\x83\x00\x00\x00")              # a short Void: one-byte size
        with self.assertRaises(bad):
            mkv._void(1)
        E = _el
        head = E("1A45DFA3", E("4282", b"matroska"))
        seg = lambda body: bytes.fromhex("18538067") + (len(body) | (1 << 56)).to_bytes(8, "big") + body

        def layout_of(raw):
            path = os.path.join(d, "x.mkv")
            with open(path, "wb") as fh:
                fh.write(raw)
            return mkv.read_layout(path)
        for raw, why in ((head + E("1549A966", b""), "no Segment"),
                         (head + seg(bytes.fromhex("1549A966") + b"\xff" + b"\x00" * 20), "unknown-size"),
                         (head + seg(E("1549A966", b"\x00" * 4)), "no clusters")):
            with self.assertRaises(bad) as cm:
                layout_of(raw)
            self.assertIn(why, str(cm.exception))
        # a SeekHead with a checksum in it, and Tags/Tracks with other elements among theirs
        sh = E("114D9B74", E("BF", b"\x00" * 4) + E("4DBB", E("53AB", bytes.fromhex("1549A966")) + E("53AC", (0).to_bytes(8, "big"))))
        got = layout_of(head + seg(sh + E("1549A966", b"") + E("1F43B675", b"\x00" * 16)))
        self.assertEqual(got["seek"]["Info"], len(head) + 12)                 # (the checksum skipped)
        self.assertEqual(mkv._our_tags(E("EC", b"") + E("7373", E("63C0", b"") + E("67C8", E("45A3", b"PLEXCACHE_OPTIMIZER") + E("4487", b"0.1.0")))),
                         {"PLEXCACHE_OPTIMIZER": "0.1.0"})
        tracks = mkv._tracks(E("EC", b"") + E("AE", E("D7", b"\x02") + E("86", b"S_HDMV/PGS") + E("6D80", E("EC", b"") + E("6240", E("5034", E("4254", b"\x00"))))))
        self.assertEqual([(t["number"], t["codec"], t["compression"]) for t in tracks], [(2, "S_HDMV/PGS", 0)])  # zlib
        # rewriting the end: nothing there to sort; a Cluster after the index; attachments added twice
        p = os.path.join(d, "plain.mkv")
        build_mkv(p, tail=())
        self.assertEqual(mkv.reorder_tail(p), [])
        build_mkv(p)
        with open(p, "ab") as fh:
            fh.write(E("1F43B675", b"\x00" * 16))
        with self.assertRaises(bad) as cm:
            mkv.reorder_tail(p)
        self.assertIn("Cluster after the index", str(cm.exception))
        build_mkv(p, tail=("Cues", "Tags", "Attachments"), tail_atts=[("font.ttf", "font/ttf", b"\x00" * 100)])
        with self.assertRaises(bad) as cm:
            font = os.path.join(d, "a.ttf")
            with open(font, "wb") as fh:
                fh.write(b"\x00" * 10)
            mkv.reorder_tail(p, attachments=mkv.attachments_element([{"name": "a.ttf", "mime": "font/ttf", "path": font, "uid": 1}]))
        self.assertIn("attachments already", str(cm.exception))

class PageStructureTests(unittest.TestCase):
    def test_info_buttons_line_up(self):
        """Every ⓘ in a row of checks is the row's first thing, and no style indents such a row: a
        sub-option indents its box and name (the label), so the ⓘ of every row sit in one column."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js = open(os.path.join(root, "plexcache", "web", "app.js"), encoding="utf-8").read()
        css = open(os.path.join(root, "plexcache", "web", "app.css"), encoding="utf-8").read()
        rows = list(re.finditer(r"<div class=\"(chk|convtype)(?: [^\"]*)?\"[^>]*>", js))
        self.assertGreater(len(rows), 5)
        for m in rows:
            seg = js[m.end():m.end() + 400]
            label, inf = seg.find("<label"), re.search(r"\b(info|infoOrGap)\(", seg)
            self.assertIsNotNone(inf, f"a row of checks without an ⓘ (or its gap): {seg[:80]!r}")
            self.assertTrue(label < 0 or inf.start() < label, f"a row of checks puts its ⓘ after the label: {seg[:100]!r}")
        for m in re.finditer(r"([^{}]*)\{([^}]*)\}", css):
            sel, body = m.group(1).strip(), m.group(2)
            if re.search(r"\.(chk|lhchk|lhsub)\b", sel) and not re.search(r"\b(label|lhlang|info|badge|select)\b", sel.split(",")[-1]):
                self.assertNotRegex(body, r"(margin|padding)-left\s*:\s*[1-9]", f"{sel} indents a whole row (its ⓘ would leave the column)")

    def test_paged_lists_use_the_pager(self):
        """Every list shown a page at a time uses the one pager(): spans of what each page holds (its
        first and last entry cut to what tells it from its neighbours), the open page plain, Beginning
        and End only when pages lie beyond the spans shown. No list draws Next/Previous of its own."""
        from plexcache.paging import page_ranges
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js = open(os.path.join(root, "plexcache", "web", "app.js"), encoding="utf-8").read()
        body = js[js.index("  function pager("):]
        body = body[:body.index("\n  function ", 10)]
        self.assertNotRegex(js.replace(body, ""), r'H\("(Next|Previous)"\)', "a list pages with Next/Previous of its own: use pager()")
        for m in re.finditer(r'data-offset="', js.replace(body, "")):
            self.fail(f"a list draws its own page links: {js[m.start() - 80:m.start() + 40]!r}")
        self.assertGreaterEqual(js.count("pager("), 5)  # (the Library files, Health items, Sync differences, a job's files)
        # the page's own span helper (for lists it holds whole) agrees with the server's
        fn = self._js_fn("pageRanges")
        got = self._node(fn + "\nconsole.log(JSON.stringify(pageRanges(" + json.dumps(["Alpha", "Alps", "Amber", "Beta", "Altered Carbon · S01E01", "Altered Carbon · S01E02"]) + ", 2)));")
        self.assertEqual(got, page_ranges(["Alpha", "Alps", "Amber", "Beta", "Altered Carbon · S01E01", "Altered Carbon · S01E02"], 2))
        # three letters at most: episodes of one show across pages don't make a span of the whole title
        shows = [f"Altered Carbon · S01E{i:02d}" for i in range(1, 11)] + ["Andromeda · S01E01"]
        self.assertEqual([(p["from"], p["to"]) for p in page_ranges(shows, 5)], [("A", "Alt"), ("Alt", "Al"), ("An", "A")])
        # the spans: shortest prefixes that tell the pages apart
        labels = [f"{c}{i:02d}" for c in "ABCD" for i in range(5)]  # A00…A04, B00…
        self.assertEqual(page_ranges(labels, 5), [{"offset": 0, "from": "A", "to": "A"}, {"offset": 5, "from": "B", "to": "B"},
                                                  {"offset": 10, "from": "C", "to": "C"}, {"offset": 15, "from": "D", "to": "D"}])
        got = page_ranges(["Alpha", "Alps", "Amber", "Beta"], 2)
        self.assertEqual(got, [{"offset": 0, "from": "A", "to": "Al"}, {"offset": 2, "from": "Am", "to": "B"}])  # ("Al" already tells Alps from Amber)
        self.assertEqual(page_ranges(["x"], 10), [{"offset": 0, "from": "x", "to": "x"}])
        self.assertEqual(page_ranges([], 10), [])
        self.assertEqual(page_ranges(["", "b"], 1)[0]["from"], "…")
        # a title's opening quote, apostrophe or bracket doesn't sort it, nor open a span: 'Twas the Night… is a T
        from plexcache.paging import plain, sort_key
        titles = ["'Twas the Night Before Christmas (1974)", "10,000 BC (2008)", "Alpha", "(500) Days of Summer", "“Quoted”", "zed"]
        self.assertEqual(sorted(titles, key=sort_key), ["10,000 BC (2008)", "(500) Days of Summer", "Alpha", "“Quoted”", "'Twas the Night Before Christmas (1974)", "zed"])
        self.assertEqual([(p["from"], p["to"]) for p in page_ranges(sorted(titles, key=sort_key), 2)], [("1", "5"), ("A", "Q"), ("T", "z")])
        plainjs = self._js_fn("plainTitle")
        for t in titles:
            self.assertEqual(self._node(plainjs + "\nconsole.log(JSON.stringify(plainTitle(" + json.dumps(t) + ")));"), plain(t))
        got = self._node(fn + "\nconsole.log(JSON.stringify(pageRanges(" + json.dumps(sorted(titles, key=sort_key)) + ", 2)));")
        self.assertEqual(got, page_ranges(sorted(titles, key=sort_key), 2))
        # every list sorts so (the server's lists through sort_key; the page's pages come from the server)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for mod in ("app.py", "libhealth.py", "joblog.py", "sync.py", "tools/merge.py", "tools/versions.py", "tools/folders.py", "tools/unlink.py"):
            src = open(os.path.join(root, "plexcache", mod), encoding="utf-8").read()
            self.assertIn("sort_key(", src, f"{mod} sorts titles without sort_key")
            self.assertNotRegex(src, r'\["(title|label)"\]\.lower\(\)\)', f"{mod} sorts titles by .lower() alone")
        # the pager: one look for every page, the open one framed (no size of its own)
        css = open(os.path.join(root, "plexcache", "web", "app.css"), encoding="utf-8").read()
        cur = re.search(r"\.pager \.pg\.cur\{([^}]*)\}", css).group(1)
        self.assertNotRegex(cur, r"padding|font-weight|font-size|line-height|min-height", "the open page's chip is sized differently from the others")
        self.assertRegex(css, r"\.pager button\.link\.pg\{[^}]*min-height", "the page links keep .link's own height: taller than the open page's chip")
        self.assertIn("border-color:var(--ink)", cur)

    def test_internal_links_reach_their_place(self):
        """Wherever a text names a place in the app, it's a link — and every internal link points at a page
        that exists and, with a fragment, at an element on it with that id, so it scrolls there. Links in
        the page's own text (href="/page#id"), in the server's texts ([text](/page#id), rendered by links()),
        and in the menu."""
        from plexcache import server
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        read = lambda *p: open(os.path.join(root, *p), encoding="utf-8").read()
        js, html = read("plexcache", "web", "app.js"), read("plexcache", "web", "index.html")
        py = "".join(read("plexcache", n) for n in os.listdir(os.path.join(root, "plexcache")) if n.endswith(".py"))
        py += "".join(read("plexcache", "tools", n) for n in os.listdir(os.path.join(root, "plexcache", "tools")) if n.endswith(".py"))
        found = set(re.findall(r'href=\\?"(/[a-z0-9-]*(?:#[a-z0-9-]+)?)\\?"', js)) | set(re.findall(r'href="(/[a-z0-9-]*(?:#[a-z0-9-]+)?)"', html))
        found |= set(re.findall(r"\]\((/[a-z0-9-]*(?:#[a-z0-9-]+)?)\)", py))
        # also the links a check builds as a dict: {"href": "/sync", ...}
        found |= set(re.findall(r'"href": "(/[a-z0-9-]*(?:#[a-z0-9-]+)?)"', py))
        self.assertGreater(len(found), 15)
        ids = set(re.findall(r'\bid="([a-z0-9-]+)"', js)) | set(re.findall(r'\bid="([a-z0-9-]+)"', html)) | set(re.findall(r'"id": "([a-z0-9-]+)"', py))
        for link in sorted(found):
            page, _, frag = link.partition("#")
            self.assertIn(page, server.PAGES, f"{link}: no such page")
            if frag:
                self.assertIn(frag, ids, f"{link}: nothing on the page has id={frag!r}")
        # no text a page shows names a place without linking it ("System → Plex" and the like): the Health and Metrics
        # items' texts (checks.py, report.py — rendered through links()) and the page's own H() texts. TrueNAS's own
        # places ("in TrueNAS under System → …") aren't ours to link.
        shown = "".join(re.findall(r'H\("((?:[^"\\]|\\.)*)"', js)) + "\n" + "".join(re.findall(r'M\("((?:[^"\\]|\\.)*)"', read("plexcache", "checks.py") + read("plexcache", "report.py")))
        for m in re.finditer(r"(System|Caching|SSD Cache|Sync|Health|Tools) → [A-Z][A-Za-z ]+", shown):
            before = shown[max(0, m.start() - 120):m.start()]
            self.assertTrue("data-nav>" in before[-60:] or before.endswith("[") or before.endswith("TrueNAS under "),
                            f"“{m.group(0)}” names a place without a link")

    def test_every_page_starts_with_a_description(self):
        """Every page opens with its title and a short description under it (top()'s second argument):
        what the page is for, in a sentence or two, before anything else."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "plexcache", "web", "app.js"), encoding="utf-8").read()
        pages = re.findall(r"\n  function (page[A-Z]\w*)\(\) \{(.*?)(?=\n  function |\n  var |\Z)", src, re.S)
        self.assertGreater(len(pages), 10)
        for name, body in pages:
            m = re.search(r"\btop\(([^,]+),\s*(.+)", body)
            self.assertIsNotNone(m, f"{name} doesn't call top(title, description)")
            desc = m.group(2).strip()
            self.assertFalse(desc.startswith('""') or desc.startswith("''"), f"{name} opens without a description")
            self.assertTrue(desc.startswith(("H(", "T(", "esc(", "(", "LIB.", "libs", "S.", "ST.", "op.", "sy.")) or '"' in desc, f"{name}: {desc[:60]}")

    def _js_fn(self, name):
        """A pure function marked "// @testable: <name>" in app.js, as its source."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "plexcache", "web", "app.js"), encoding="utf-8").read()
        m = re.search(r"// @testable: " + re.escape(name) + r"\b.*?\n(  function .*?)\n  // @end", src, re.S)
        self.assertIsNotNone(m, name)
        return m.group(1)

    def _node(self, code):
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node isn't installed")
        got = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(got.returncode, 0, got.stderr)
        return json.loads(got.stdout)

    def test_library_and_tool_pages_scan_by_themselves(self):
        """The Library page starts the view's job by itself — inspecting the files for Caching and
        Health, comparing with the mirror for Sync — pausing the other; once the view's job is done the
        other carries on, so it's ready when that view is opened; a job paused by hand stays paused.
        A tool's page scans the library opened once by itself. (libAutoPlan and toolAutoPlan, run in node.)"""
        fn = self._js_fn("libAutoPlan") + "\n" + self._js_fn("toolAutoPlan")
        cases = [
            # landing on Caching with files to inspect: inspected
            ["caching", {"inspect": {"needed": True, "known": True}, "sync": {"on": True, "needed": True, "known": True}}, ["inspect:start"]],
            # the files to inspect not known yet (the library's data still loading): nothing
            ["caching", {"inspect": {"needed": False, "known": False}, "sync": {"on": True, "needed": True, "known": True}}, []],
            # Health (the view the page opens on) runs no job of its own — and pauses one left running
            ["health", {"inspect": {"needed": True, "known": True}, "sync": {"on": True, "needed": True, "known": True}}, []],
            ["health", {"inspect": {"running": True, "needed": True, "known": True}, "sync": {"on": True, "needed": True, "known": True}}, ["inspect:pause"]],
            # switching to Sync while inspecting: the compare started, the inspection paused
            ["sync", {"inspect": {"running": True, "needed": True, "known": True}, "sync": {"on": True, "needed": True, "known": True}}, ["sync:start", "inspect:pause"]],
            # back on Caching while the compare runs: the inspection resumed, the compare paused
            ["caching", {"inspect": {"paused": True, "needed": True, "known": True}, "sync": {"on": True, "running": True, "needed": True, "known": True}}, ["inspect:start", "sync:pause"]],
            # the view's job running: left alone
            ["caching", {"inspect": {"running": True, "needed": True, "known": True}, "sync": {"on": True, "needed": True, "known": True}}, []],
            # the view's job done: the other isn't started off its own view
            ["caching", {"inspect": {"needed": False, "known": True}, "sync": {"on": True, "needed": True, "known": True}}, []],
            ["sync", {"inspect": {"paused": True, "needed": True, "known": True}, "sync": {"on": True, "needed": False, "known": True}}, []],
            # everything done: nothing
            ["sync", {"inspect": {"needed": False, "known": True}, "sync": {"on": True, "needed": False, "known": True}}, []],
            # no mirror: nothing to compare; an inspection left running is paused all the same
            ["sync", {"inspect": {"running": True, "needed": True, "known": True}, "sync": {"on": False, "needed": True, "known": True}}, ["inspect:pause"]],
            # paused by hand: stays paused
            ["caching", {"inspect": {"paused": True, "needed": True, "known": True}, "sync": {"on": True, "needed": True, "known": True}, "hold": {"inspect": True}}, []],
            # a compare waiting its turn (another library's first) isn't started again
            ["sync", {"inspect": {"needed": False, "known": True}, "sync": {"on": True, "queued": True, "needed": True, "known": True}}, []],
        ]
        tools = [
            ["1", {"running": False, "scan": None}, {}, "scan"],
            ["1", {"running": False, "scan": {"library": "1"}}, {}, None],       # scanned already: Scan Again is yours
            ["1", {"running": False, "scan": {"library": "2"}}, {}, "scan"],     # another library's scan shown: this one's
            ["1", {"running": True, "scan": None}, {}, None],
            ["1", {"running": False, "scan": None}, {"1": True}, None],          # tried once (it failed, say): not again by itself
            ["", {"running": False, "scan": None}, {}, None],
        ]
        got = self._node(fn + "\nconsole.log(JSON.stringify({lib: " + json.dumps([c[:2] for c in cases]) + ".map(function (c) { return libAutoPlan(c[0], c[1]); }), tools: " +
                         json.dumps([t[:3] for t in tools]) + ".map(function (t) { return toolAutoPlan(t[0], t[1], t[2]); })}));")
        for c, out in zip(cases, got["lib"]):
            self.assertEqual(out, c[2], c)
        for t, out in zip(tools, got["tools"]):
            self.assertEqual(out, t[3], t)

    def test_no_info_button_inside_another(self):
        """An ⓘ's text opens in a pop-up, so an ⓘ inside that text can't be reached. Nothing passed to
        info(), to a card heading's explanation (heading's second argument: it goes behind an ⓘ when
        long) or to a field's longer help ({more: ...}) may contain an info() of its own."""
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location("i18n_tool", os.path.join(root, "dev", "i18n.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        with open(os.environ.get("PLEXCACHE_CHECK_JS") or os.path.join(root, "plexcache", "web", "app.js"), encoding="utf-8") as fh:
            src = fh.read()
        toks = tool.js_tokens(src)
        line = lambda k: src.count("\n", 0, toks[k][2]) + 1

        def args(k):
            """The arguments of the call whose "(" is toks[k]: a list of token lists."""
            out, cur, depth = [], [], 0
            for t in toks[k + 1:]:
                if t[0] == "p" and t[1] in "([{":
                    depth += 1
                elif t[0] == "p" and t[1] in ")]}":
                    if depth == 0:
                        return out + [cur]
                    depth -= 1
                elif t[0] == "p" and t[1] == "," and depth == 0:
                    out.append(cur)
                    cur = []
                    continue
                cur.append(t)
            return out + [cur]

        def has_info(part):
            return any(t == ("id", "info") and i + 1 < len(part) and part[i + 1][1] == "(" for i, (t) in enumerate((x[0], x[1]) for x in part)) \
                or any(x[0] == "str" and 'class="info"' in x[1] for x in part)

        def value(k):
            """The tokens of an object's value after toks[k] (a ":"), up to its "," or "}"."""
            out, depth = [], 0
            for t in toks[k + 1:]:
                if t[0] == "p" and t[1] in "([{":
                    depth += 1
                elif t[0] == "p" and t[1] in ")]}":
                    if depth == 0:
                        return out
                    depth -= 1
                elif t[0] == "p" and t[1] == "," and depth == 0:
                    return out
                out.append(t)
            return out
        bad = []
        for k, (kind, v, _) in enumerate(toks[:-1]):
            if kind != "id" or (k and toks[k - 1][1] in (".", "function")):
                continue
            if v in ("info", "heading") and toks[k + 1][1] == "(":
                a = args(k + 1)
                help_arg = a[0] if v == "info" else (a[1] if len(a) > 1 else [])
                if has_info(help_arg):
                    bad.append(f"app.js:{line(k)}: {v}() with an ⓘ inside its explanation")
            if v == "more" and toks[k + 1][1] == ":" and has_info(value(k + 1)):
                bad.append(f"app.js:{line(k)}: more: with an ⓘ inside it")
        self.assertEqual(bad, [])
        self.assertTrue(has_info(tool.js_tokens('x + info(H("y"))')))       # (the check itself sees one)


class JobEdgeTests(unittest.TestCase):
    def test_first_estimate_and_retries(self):
        """A first estimate from how far through the first file a job is (not before 2%, and not once a
        file is done); a retry that was cancelled leaves the failure standing; one that worked is
        counted in the running job."""
        from plexcache import jobs
        app = _new_app()
        j = jobs.Job(app.jobs, None, library="1")
        j.state.update(todo_bytes=1000, done_bytes=0)
        j.first_estimate(1000, 10, 1)                     # too early to tell
        self.assertIsNone(j.state["eta"])
        j.first_estimate(1000, 10, 10)                    # 10% of the first file in 10 s: about 90 s to go
        self.assertEqual(j.state["eta"], 90)
        j.state["work_seconds"] = 5                       # a file is done: real figures from then on
        j.first_estimate(1000, 10, 50)
        self.assertEqual(j.state["eta"], 90)
        app.jobs.job = j
        j.state.update(id="j1", errors=[{"file": "/a.mkv", "reason": "failed"}], failed=1)
        app.jobs.retried("j1", "/a.mkv", {"result": "cancelled"})
        self.assertEqual(j.state["failed"], 1)
        with mock.patch.object(j, "save") as save:
            app.jobs.retried("j1", "/a.mkv", {"result": "optimized"})
        self.assertEqual((j.state["failed"], j.state["errors"]), (0, []))
        save.assert_called_once()


class LogFileEdgeTests(unittest.TestCase):
    def test_coverage_and_cache_logs_on_odd_ground(self):
        """The hourly coverage record and the cache log: nowhere to save, a folder that can't be
        written, the same hour recorded twice, a library with no files, and stored data that's
        malformed — skipped or kept for the next try, never an error."""
        from plexcache import cachelog
        from plexcache.coverage import Coverage
        d = tempfile.mkdtemp(dir=TMP)
        Coverage(None).save()                                                  # nowhere to keep it: fine
        cov = Coverage(os.path.join(d, "no-such-folder", "coverage.json"))
        now = 1_700_000_000.0
        cov.record([("/a.mkv", 100, "1")], {"/a.mkv": now - 60}, now)
        cov.record([("/a.mkv", 100, "1")], {}, now + 1)                        # the same hour again: replaced
        self.assertEqual(len(cov.hours), 1)
        cov.save()                                                             # can't be written: kept for later
        cov.hours.append({"t": now + 3600, "libs": {"2": [0] * 12}})
        series = cov.series({"1": "Films", "2": "Empty"})
        self.assertIsNone(series["libs"]["2"][-1])                              # (an hour with no files in it)
        self.assertIsNone(cachelog._load_lib("not a dict", True))
        self.assertIsNone(cachelog._load_lib({"first": [1, 2, 3], "ages": []}, True))
        log = cachelog.CacheLog(os.path.join(d, "no-such-folder", "cachelog.json"))
        log.note("/a.mkv", whole=False)
        log.save(force=True)
        self.assertTrue(log._dirty)                                            # tried again next time
        with mock.patch.object(cachelog, "MAX_READS", 2):
            for i in range(4):
                log.note("/b.mkv", whole=False, route="ram", hit=True, at=now + i * 4000)
        self.assertLessEqual(len(log.reads), 2)


class MoreEdgeTests(unittest.TestCase):
    def test_library_view_and_chances(self):
        """Library Health lists a file that's gone without failing; a re-check can rescan first; how
        likely something is still cached comes from what's been measured once there's enough."""
        app = _new_app()
        app.scan_library()
        f = next(f for f in app.files if os.path.exists(app.map_path(f.path)))
        path = app.map_path(f.path)
        os.rename(path, path + ".away")
        try:
            view = app.library_view(str(f.library_key), 0, 500, "")
        finally:
            os.rename(path + ".away", path)
        self.assertTrue(any(r for r in view["files"]))
        done = threading.Event()
        with mock.patch.object(app, "run_checks", done.set), mock.patch.object(app, "scan_library") as scan:
            app.run_checks_async(rescan=True)
            self.assertTrue(done.wait(10))
        scan.assert_called_once()
        with mock.patch.object(app.cachelog, "written", lambda p, whole: (time.time() - 600, "ram", "1")), \
                mock.patch.object(app.cachelog, "still_cached", lambda route, lib, age: 0.4):
            self.assertEqual(app.cached_chance("/x.mkv", False), 0.4)

    def test_connections_that_wont_close(self):
        """A socket that errors on closing is let go all the same."""
        from plexcache.mqttclient import MQTTClient

        class Stuck:
            def close(self):
                raise OSError("bad file descriptor")

            def sendall(self, data):
                raise OSError("broken pipe")

            def shutdown(self, how):
                raise OSError("not connected")

            def settimeout(self, t):
                raise OSError("bad file descriptor")
        c = MQTTClient("127.0.0.1", 1883)
        c.sock = Stuck()
        c._drop()
        self.assertIsNone(c.sock)
        c.sock = Stuck()
        c.close()
        self.assertIsNone(c.sock)

    def test_unreadable_tags_are_none_of_ours(self):
        """Tags that can't be parsed count as none of the optimizer's: the file is optimized again."""
        from plexcache.optimize import mkv
        p = os.path.join(tempfile.mkdtemp(dir=TMP), "t.mkv")
        build_mkv(p, ours={"PLEXCACHE_OPTIMIZER": "0.1.2"})
        tags_at = mkv.read_layout(p)["seek"]["Tags"]
        with open(p, "r+b") as fh:
            fh.seek(tags_at + 12)                                       # the first Tag inside: of unknown size
            fh.write(bytes.fromhex("7373") + b"\xff")
        self.assertEqual(mkv.read_layout(p)["ours"], {})


class SmallEdgeTests(unittest.TestCase):
    """Small helpers' less common paths: a job log that can't be written, tallies of what files
    gained, sign-in that can't be kept, Basic auth that isn't base64, and media folders that
    hold something other than the library's files."""

    def test_job_log_edges(self):
        from plexcache import joblog
        t = {}
        joblog.tally_file(t, {"result": "optimized", "rs_before": 131072, "rs_after": 1048576, "poster_added": True, "images_saved": 1234})
        joblog.tally_file(t, {"result": "skipped", "rs_after": 1})                       # not written: not counted
        self.assertEqual(t, {"written": 1, "rs_fixed": 1, "posters_added": 1, "posters_saved": 1234})
        self.assertEqual(joblog._cell({"a": 1}), '{"a": 1}')
        d = tempfile.mkdtemp(dir=TMP)
        log = joblog.JobLog(d)
        os.makedirs(log.path)                                        # (a folder where the file should be: can't be written)
        log.add({"type": "job", "job": "j1", "event": "started"})    # never an error: the job carries on
        log2 = joblog.JobLog(tempfile.mkdtemp(dir=TMP))
        for e in ({"type": "file", "file": "/x"}, {"type": "job", "job": "j2", "event": "started", "kind": "optimize"},
                  {"type": "file", "job": "j2", "file": "/a.mkv", "result": "cancelled"},
                  {"type": "file", "job": "j2", "file": "/b.mkv", "result": "optimized"}):
            log2.add(e)
        (j,) = log2.jobs()
        self.assertEqual((j["job"], j["files"]), ("j2", 1))                # the cancelled one wasn't tried

    def test_sign_in_edges(self):
        from plexcache.auth import Auth
        d = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(d, "session.key"))                 # can't be written: sessions last until a restart
        a = Auth("admin", "s3cret-long", d)
        self.assertTrue(a.session_ok(a.new_session()))
        a._revoked_path = None
        a._save_revoked()                                            # nowhere to keep them: fine
        self.assertFalse(a.request_ok({"Authorization": "Basic %%%not-base64"}))

    def test_media_folder_report(self):
        from plexcache import mapping
        root = tempfile.mkdtemp(dir=TMP)
        other = os.path.join(root, "other")
        os.makedirs(other)
        with open(os.path.join(other, "notes.txt"), "w") as fh:
            fh.write("x")
        os.makedirs(os.path.join(root, "nothing-yet"))
        app = types.SimpleNamespace(map_path=lambda p: p, libraries=[
            {"title": "Films", "key": "1", "kind": "movie", "excluded": False, "locations": [other], "samples": {other: [other + "/Film.mkv"]}},
            {"title": "New", "key": "2", "kind": "movie", "excluded": False, "locations": [os.path.join(root, "nothing-yet")], "samples": {}}])
        rows = {r["library"]: r for r in mapping.location_report(app, mount_list=[])}
        self.assertEqual(rows["Films"]["status"], "unreadable")                  # files there, but not the library's
        self.assertEqual((rows["New"]["status"], en(rows["New"]["detail"])), ("ok", "Mounted (no files in this folder yet)"))
        self.assertFalse(mapping._is_empty_dir(os.path.join(root, "missing")))


class PlexEdgeTests(unittest.TestCase):
    def test_odd_answers(self):
        """Episodes without numbers are named by title; libraries of a kind that isn't cached have no
        files; a limit stops paging; posters come back as Plex has them; plex.tv down is an error."""
        import xml.etree.ElementTree as ET
        from plexcache import plex
        ep = ET.Element("Video", {"type": "episode", "title": "Pilot", "grandparentTitle": "Harbour Street"})
        self.assertEqual(plex.title_of(ep), "Harbour Street · Pilot")
        self.assertEqual(plex.title_of(ET.Element("Video", {"type": "episode", "title": "Pilot"})), "Pilot")
        c = PlexClient(PLEX.url, "good-token")
        with mock.patch.object(c, "_request", lambda *a, **kw: b"\x89PNG poster"):
            self.assertEqual(c.image("/library/metadata/1/thumb/1"), b"\x89PNG poster")
        self.assertEqual(c.library_files(plex.Library("9", "Photos", "photo", None)), [])
        tv = next(lib for lib in c.libraries() if lib.kind == "tv")
        self.assertEqual(len(c.library_files(tv, limit=2)), 2)
        with mock.patch.object(plex.urllib.request, "urlopen", mock.Mock(side_effect=urllib.error.URLError("down"))):
            with self.assertRaises(PlexError) as cm:
                plex._plextv("POST", "/api/v2/pins", "client-1")
        self.assertIn("Can’t reach plex.tv", str(cm.exception))

    def test_errors(self):
        with self.assertRaises(PlexError):
            PlexClient("", "x")
        c = PlexClient(PLEX.url, "good-token")
        with self.assertRaises(PlexError) as cm:
            c.get("/no/such/thing")
        self.assertIn("HTTP 404", str(cm.exception))
        with self.assertRaises(PlexError):
            PlexClient("http://127.0.0.1:1", "x", timeout=1).identity()



def _el(id_hex: str, payload: bytes) -> bytes:
    return fakes.ebml(id_hex, payload)


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def build_mkv(path, tail=("Cues", "Tags"), front_atts=(), tail_atts=(), pgs_algo="none", void=0, cluster=b"", ours=None, chapters=False):
    """A small, real-structured MKV for the optimizer: header, SeekHead (+ Void), Info, Tracks
    (video + PGS), optional Attachments before the cluster, one Cluster, then ``tail`` in order
    (names from Cues, Tags, Attachments). ``pgs_algo``: "none" (no ContentEncodings) or a number.
    ``ours``: our global tags ({"PLEXCACHE_OPTIMIZER": "0.1.0"}), next to the file's own title tag.
    ``chapters``: a Chapters element in front of the cluster, as mkvmerge writes it."""
    cluster = cluster or os.urandom(20000)
    att = lambda files: _el("1941A469", b"".join(
        _el("61A7", _el("466E", n.encode()) + _el("4660", m.encode()) + _el("46AE", (1000 + i).to_bytes(8, "big")) + _el("465C", d))
        for i, (n, m, d) in enumerate(files)))
    enc = b"" if pgs_algo == "none" else _el("6D80", _el("6240", _el("5034", _el("4254", bytes([pgs_algo])))))
    tracks = _el("1654AE6B", _el("AE", _el("D7", b"\x01") + _el("86", b"V_MPEG4/ISO/AVC")) +
                 _el("AE", _el("D7", b"\x02") + _el("86", b"S_HDMV/PGS") + enc))
    info = _el("1549A966", _el("2AD7B1", (1000000).to_bytes(3, "big")))
    simple = lambda n, v: _el("67C8", _el("45A3", n.encode()) + _el("4487", v.encode()))
    tags = _el("1254C367", _el("7373", _el("63C0", b"") + simple("TITLE", "A film")) +
               (_el("7373", _el("63C0", b"") + b"".join(simple(k, v) for k, v in ours.items())) if ours else b"") +
               _el("7373", _el("63C0", _el("63C5", (77).to_bytes(8, "big"))) + simple("PLEXCACHE_OPTIMIZER", "9.9.9")))
    parts = {"Cues": _el("1C53BB6B", b"\x00" * 300), "Tags": tags,
             "Attachments": att(tail_atts) if tail_atts else b""}
    ids = {"Info": "1549A966", "Tracks": "1654AE6B", "Attachments": "1941A469", "Cues": "1C53BB6B", "Tags": "1254C367", "Chapters": "1043A770"}
    chap = _el("1043A770", _el("45B9", _el("B6", _el("73C4", (5).to_bytes(8, "big")) + _el("91", (0).to_bytes(1, "big")) +
                                         _el("80", _el("85", b"Opening")))))
    header = _el("1A45DFA3", _el("4282", b"matroska"))

    def seekhead(pos):
        return _el("114D9B74", b"".join(_el("4DBB", _el("53AB", bytes.fromhex(ids[n])) + _el("53AC", pos.get(n, 0).to_bytes(8, "big")))
                                         for n in ("Info", "Tracks", "Chapters", "Attachments", "Cues", "Tags") if n in pos))
    names = ["Info", "Tracks", "Cues", "Tags"] + (["Attachments"] if front_atts or tail_atts else []) + (["Chapters"] if chapters else [])
    sh_len = len(seekhead({n: 0 for n in names}))
    gap = _el("EC", b"\x00" * (200 + void))
    front = [("Info", info), ("Tracks", tracks)] + ([("Chapters", chap)] if chapters else []) + ([("Attachments", att(front_atts))] if front_atts else [])
    pos, p = {}, sh_len + len(gap)
    for n, raw in front:
        pos[n] = p
        p += len(raw)
    clus = _el("1F43B675", cluster)
    p += len(clus)
    tail_parts = [(n, parts[n]) for n in tail if parts[n]]
    for n, raw in tail_parts:
        pos[n] = p
        p += len(raw)
    body = seekhead(pos) + gap + b"".join(r for _, r in front) + clus + b"".join(r for _, r in tail_parts)
    with open(path, "wb") as fh:
        fh.write(header + bytes.fromhex("18538067") + (len(body) | (1 << 56)).to_bytes(8, "big") + body)
    return {"cluster": clus}


FONT = ("font.ttf", "font/ttf", b"glyph data " * 400)
COVER = ("cover.jpg", "image/jpeg", os.urandom(3000))


class FakeOptimizer:
    """Stands in for the optimizer service: "optimizes" a file by writing it again, tagged as the
    real one tags it, and reports what the real one does (the new file's identity, its tags, its
    sha256, the optimizer version). Claims are kept in a dict, as one shared folder would."""
    configured = True

    def __init__(self, result="optimized"):
        self.sent, self.done, self.result = [], None, result
        self.convert_result = "converted"  # or "skipped": what MKV wouldn't keep
        self.claims = {}   # key -> {instance: at}
        self.peers = []

    def ready(self):
        return True

    def rewrite(self, path):
        return self._done(path, "rewritten", {"path": path, "action": "rewrite"})

    def optimize(self, path, images, force, cover=None, tags="keep"):
        return self._done(path, self.result, {"path": path, "action": "optimize", "images": images, "force": force, "cover": cover, "tags": tags})

    def convert(self, path, images, cover=None, tags="keep"):
        """Converts as the real one does: <name>.mkv next to the original, optimized and tagged."""
        from plexcache.optimize import VERSION
        self.sent.append({"path": path, "action": "convert", "images": images, "tags": tags})
        new = os.path.splitext(path)[0] + ".mkv"
        if self.convert_result == "converted":
            build_mkv(new, ours={"PLEXCACHE_OPTIMIZER": VERSION})
            st = os.stat(new)
            self.done = {"path": path, "result": "converted", "new": new, "finished": time.time() + 1, "verified": "passed",
                         "tags": {"PLEXCACHE_OPTIMIZER": VERSION}, "version": VERSION, "after": st.st_size,
                         "file": {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino}}
        else:
            self.done = {"path": path, "result": "skipped", "reason": "its chapters wouldn't be kept", "finished": time.time() + 1}
        return {"started": True}

    def delete_original(self, path, new):
        self.sent.append({"action": "delete_original", "path": path, "new": new})
        os.remove(path)
        return {"deleted": True}

    def mark(self, folder, instance, info):
        from plexcache.optimize import claims
        return {"marked": claims.mark(folder, instance, info)}

    def file_action(self, action, path, to="", root=""):
        from plexcache.optimize import files
        self.sent.append({"action": action, "path": path, "to": to, "root": root})
        return {"set_aside": lambda: files.set_aside(root, path), "restore": lambda: files.put_back(root, path), "purge": lambda: files.purge(root, path),
                "link": lambda: files.link(path, to), "unlink": lambda: files.unlink(path, to), "make_real": lambda: files.make_real(path)}[action]()

    def claim(self, folder, key, instance, release=False):
        mine = self.claims.setdefault(key, {})
        if release:
            mine.pop(instance, None)
        else:
            mine.setdefault(instance, time.time())
        self.sent.append({"action": "claim", "key": key, "release": release})
        return {"claims": sorted(({"instance": i, "at": t} for i, t in mine.items()), key=lambda c: (c["at"], c["instance"])),
                "peers": list(self.peers)}

    def conflict(self, copy, original):
        os.remove(copy)
        return self._done(copy, "deleted", {"path": copy, "action": "conflict"}, rebuild=False)

    def _done(self, path, result, sent, rebuild=True):
        from plexcache.optimize import VERSION
        from plexcache.optimize.optimizer import COVER_PREFIX
        self.sent.append(sent)
        cover = sent.get("cover")
        tags = {"PLEXCACHE_OPTIMIZER": VERSION}
        if cover:
            tags["PLEXCACHE_COVER"] = COVER_PREFIX + hashlib.sha256(cover).hexdigest()
        if result in ("optimized", "rewritten") and rebuild:
            if result == "rewritten":
                with open(path, "rb") as fh:
                    data = fh.read()
                with open(path + ".new", "wb") as fh:
                    fh.write(data)                                  # byte for byte
            else:
                atts = [] if sent.get("images") == "remove" else [COVER] if cover or sent.get("images") in ("compress", "keep") else []
                build_mkv(path + ".new", pgs_algo=0, tail_atts=atts + [FONT], tail=("Attachments", "Cues", "Tags"), ours=tags)
            os.replace(path + ".new", path)                         # a new file, as the optimizer writes
            os.utime(path, (time.time() - 3600, time.time() - 3600))  # with the original's timestamps
        st = os.stat(path) if os.path.exists(path) else None
        digest = _sha256(path) if st else None
        self.done = {"path": path, "result": result, "finished": time.time() + 1, "images_left": 0, "tags": tags,
                     "reason": "the streams in the new file differ from the original" if result == "failed" else None,
                     "verified": "passed" if result in ("optimized", "rewritten") else "failed" if result == "failed" else None,
                     "images_before": 1 if sent.get("images") in ("remove", "plex") else 0,
                     "cover": bool(cover), "sha256": digest, "version": VERSION, "images_after": 0 if sent.get("images") == "remove" else 1,
                     "file": {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino} if st else {}}
        return {"started": True}

    def healthz(self):
        return {"ok": True, "service": "optimizer", "token": True, "token_problem": None, "roots": 1, "uid": 0, "gid": 0}

    def status(self, fresh=False):
        return {"current": None, "results": [self.done] if self.done else []}


def run_job_mode(op, mode, key="1"):
    assert op.start_library(key, mode)
    for _ in range(200):
        if not op.snapshot()["job"]["running"]:
            return op.snapshot()
        time.sleep(0.05)
    raise AssertionError("the job didn't finish")


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def run_job(op, key="1"):
    assert op.start_library(key)
    for _ in range(200):
        if not op.snapshot()["job"]["running"]:
            return op.snapshot()
        time.sleep(0.05)
    raise AssertionError("the job didn't finish")


class OptimizerTests(unittest.TestCase):
    """plexcache/optimize: the layout check, the tail reorder, and the service."""

    def path(self, name):
        d = tempfile.mkdtemp(dir=TMP)
        return os.path.join(d, name)

    def test_copy_meta_on_an_acl_dataset(self):
        """TrueNAS datasets with ACLs (aclmode=restricted) refuse chmod on a file with a full ACL:
        the ACL is copied first, and sets the permissions."""
        from plexcache.optimize import migrate, optimizer as opt
        src, dst = self.path("a"), self.path("b")
        for p, mode in ((src, 0o770), (dst, 0o644)):
            open(p, "wb").close()
            os.chmod(p, mode)
        st = os.stat(src)
        acl = {src: {opt.NFS4_ACL: b"acl"}, dst: {}}

        def refuse(*a, **kw):
            raise PermissionError(1, "Operation not permitted")
        with mock.patch.object(os, "listxattr", lambda p, **kw: list(acl[p]), create=True), \
                mock.patch.object(os, "getxattr", lambda p, n, **kw: acl[p][n], create=True), \
                mock.patch.object(os, "setxattr", lambda p, n, v, **kw: acl[p].__setitem__(n, v), create=True), \
                mock.patch.object(os, "chmod", refuse):
            opt._copy_meta(st, src, dst, False)                                    # no error: the ACL is the original's
            self.assertEqual(acl[dst], {opt.NFS4_ACL: b"acl"})
            self.assertTrue(migrate._copy_meta(st, dst))
            acl[src], acl[dst] = {}, {}                                             # no ACL: chmod must work
            with self.assertRaises(PermissionError):
                opt._copy_meta(st, src, dst, False)

    def test_projection_matches_the_optimizer(self):
        """Library Health's estimate of an optimized file (projection.py) against what the optimizer
        really writes, for each choice that changes the layout: cover art kept or taken out, tags kept or
        stripped, in two record sizes. If the optimizer changes how it lays files out, this fails until
        the estimate follows it. (A remux repacks the video a little: a few KB of it may move between
        blocks, so video and what's around it are allowed that much.)"""
        from plexcache import filemap, projection
        from plexcache.optimize import tools
        from plexcache.optimize.optimizer import optimize_file
        bundle = "/Applications/MKVToolNix.app/Contents/MacOS"
        if os.path.isdir(bundle) and not os.environ.get("PLEXCACHE_TOOLS_PATH"):
            os.environ["PLEXCACHE_TOOLS_PATH"] = bundle
        if tools.missing():
            self.skipTest("MKVToolNix and ffmpeg aren't installed")
        d = tempfile.mkdtemp(dir=TMP)
        v, cover, ch, src = (os.path.join(d, n) for n in ("v.mkv", "cover.jpg", "ch.txt", "src.mkv"))
        tools.run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24", "-f", "lavfi", "-i", "sine",
                  "-t", "12", "-c:v", "mpeg4", "-q:v", "2", "-c:a", "aac", "-metadata", "COMMENT=keep me", v)
        tools.run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1000x1500", "-frames:v", "1", "-q:v", "1", cover)
        with open(ch, "w") as fh:
            fh.write("".join(f"CHAPTER{i:02d}=00:00:{i:02d}.000\nCHAPTER{i:02d}NAME=Chapter {i}\n" for i in range(1, 12)))
        # as mkvmerge writes it: cover art and chapters in front of the video
        tools.run("mkvmerge", "-q", "-o", src, "--chapters", ch, "--attachment-mime-type", "image/jpeg", "--attach-file", cover, v)
        # Plex's poster, larger than the file's own cover art: what it is in the estimate is its real size
        poster = os.path.join(d, "poster.jpg")
        tools.run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1400x2100", "-frames:v", "1", "-q:v", "1", poster)
        with open(poster, "rb") as fh:
            plex = fh.read()
        for images in ("compress", "remove", "plex"):
            for strip in (False, True):
                for rec in (64 * 1024, 256 * 1024):
                    kw = {"images": images, "strip": strip, "record": rec}
                    f = os.path.join(d, f"{images}-{strip}-{rec}.mkv")
                    shutil.copy(src, f)
                    m = filemap.describe(f)
                    m["mix"] = filemap.cache_mix(f, m, rec)
                    want = projection.project(m, rec, images, strip, len(plex) if images == "plex" else None)
                    optimize_file(f, images, cover=plex if images == "plex" else None, tags_mode="strip" if strip else "keep")
                    got = filemap.describe(f)
                    got["mix"] = filemap.cache_mix(f, got, rec)
                    pm, gm = want["mix"], got["mix"]
                    # (cover art — the file's own, or Plex's poster — is compressed losslessly where a tool for it is
                    # installed: a few % smaller than the estimate, which can't know the tool's gain, so the
                    # estimate may be a block over, never under)
                    # (a remux also repacks the video a little — another mkvmerge version, another few KB — so the
                    # tail may cross a block boundary either way: one block of leeway)
                    self.assertIn(pm["blocks"], (gm["blocks"] - 1, gm["blocks"], gm["blocks"] + 1), kw)
                    self.assertAlmostEqual(want["first_video"], max(0, rec - got["video_start"]), delta=256, msg=kw)
                    slack = rec  # (the tail crossing a block boundary — see above — moves a block of video in or out of the count)
                    self.assertAlmostEqual(pm["meta"], gm["meta"], delta=256 + gm["meta"] * 0.03 + slack, msg=kw)
                    self.assertAlmostEqual(pm["video"], gm["video"], delta=8192 + slack, msg=kw)
                    self.assertAlmostEqual(pm["video_tail"], projection.tail_video(got), delta=8192 + slack, msg=kw)   # the end's video, as written
                    self.assertAlmostEqual(pm["meta_disk"], gm["meta_disk"], delta=4096 + gm["meta_disk"] * 0.05, msg=kw)
                    if images != "plex":
                        gone = m["size"] - got["size"]                  # what the file lost, against what was said to go
                        self.assertAlmostEqual(sum(want["removed"].values()), max(0, gone), delta=8192, msg=kw)

    def test_converting_to_mkv(self):
        """Converting other containers to MKV: off until a library asks for it, and then only the kinds
        chosen. A converted file's MKV is put next to it and Plex asked to scan the folder; the original
        goes only once Plex has the MKV as a version of the same item (and the hold is over) — never
        while Plex has it as an item of its own. A file that can't be converted without losing something
        isn't tried again, and Library Health says why."""
        from plexcache import filechecks
        app, op, client, d, state = self._app(names=("old.avi", "home.mp4", "film.mkv"))
        avi, mp4 = os.path.join(d, "old.avi"), os.path.join(d, "home.mp4")
        for f in app.files:
            f.rating_key, f.title = "r-" + os.path.basename(f.path), os.path.basename(f.path)
        scans, items = [], {}
        plex = types.SimpleNamespace(scan=lambda key, folder: scans.append(folder), item_files=lambda rk: items.get(rk))
        app.plex = lambda timeout=30: plex
        self.assertNotIn("convert", op.file_status(avi, "1", {}).get("problems") or [])          # off by default
        app.settings.update({"optimizer": {"libraries": {"1": {"convert": True, "sync": False}}}, "sync": {"mirrors": "none"}})  # (no mirror: originals needn't wait)
        self.assertEqual(op.file_status(avi, "1", {})["problems"], ["convert"])
        self.assertFalse(op.file_status(mp4, "1", {})["applies"])                                  # MP4 isn't converted by default
        warn = lambda path: next(c for c in filechecks.check_file(app, None, path, {"format": "stream", "size": 10, "tracks": [{"type": "video"}]},
                                                                     {}, op.options("1"), None, None) if c["id"] == "container")
        self.assertIn("converts it", en(warn(avi)["text"]))
        self.assertIn("this kind of file is off", en(warn(mp4)["text"]))
        run_job(op)
        self.assertEqual([x["path"] for x in client.sent if x.get("action") == "convert"], [avi])
        (e,) = app.conversions.snapshot()["items"]
        self.assertEqual((e["state"], e["new"], scans), ("waiting", os.path.join(d, "old.mkv"), [d]))
        self.assertEqual(op.file_status(avi, "1", {})["problems"], [])                             # converted: waiting for Plex
        self.assertEqual(warn(avi)["status"], "pass")
        with self.assertRaises(ValueError):
            app.conversions.act(avi, "delete")                                                     # not before Plex has it
        # Plex adds it as a second version of the same item: the original waits out the hold
        items["r-old.avi"] = [avi, os.path.join(d, "old.mkv")]
        app.conversions.act(avi, "check")
        self.assertEqual(app.conversions.get(avi)["state"], "seen")
        app.conversions.tick()
        self.assertTrue(os.path.exists(avi))                                                       # 7 days by default
        app.settings.update({"optimizer": {"convert_hold_days": 0}})
        app.conversions.tick()
        self.assertEqual((app.conversions.get(avi)["state"], os.path.exists(avi)), ("deleted", False))
        self.assertEqual(len(scans), 2)                                                            # scanned again after
        # MP4 chosen too: Plex makes its MKV an item of its own — nothing is deleted
        app.settings.update({"optimizer": {"convert_types": {"mp4": True}, "convert_hold_days": 0}})
        app.files.append(types.SimpleNamespace(path=os.path.join(d, "home.mkv"), library_key="1", kind="movie", size=1, kbps=1,
                                               library="Movies", rating_key="r-new", title="home"))
        run_job(op)
        items["r-home.mp4"] = [mp4]
        app.conversions.act(mp4, "check")
        self.assertEqual(app.conversions.get(mp4)["state"], "separate")
        app.conversions.tick()
        self.assertTrue(os.path.exists(mp4))
        app.conversions.act(mp4, "keep")                                                           # both files stay
        self.assertIsNone(app.conversions.get(mp4))
        # a file that can't be converted without losing something: not tried again, and said why
        clip = os.path.join(d, "clip.flv")
        with open(clip, "wb") as fh:
            fh.write(b"x" * 100)
        os.utime(clip, (time.time() - 3600, time.time() - 3600))
        app.files.append(types.SimpleNamespace(path=clip, library_key="1", kind="movie", size=1, kbps=1, library="Movies", rating_key="r-c", title="clip"))
        client.convert_result = "skipped"
        run_job(op)
        self.assertEqual(op.file_status(clip, "1", {})["problems"], [])
        self.assertIn("chapters wouldn't be kept", en(warn(clip)["text"]))
        self.assertEqual(len([x for x in client.sent if x.get("action") == "convert" and x["path"] == clip]), 1)
        run_job(op)
        self.assertEqual(len([x for x in client.sent if x.get("action") == "convert" and x["path"] == clip]), 1)
        # music isn't converted, whatever's set
        app.libraries.append({"key": "2", "title": "Music", "type": "artist", "kind": "audio", "locations": []})
        app.settings.update({"optimizer": {"libraries": {"2": {"convert": True}}}})
        self.assertFalse(op.options("2")["convert"])
        with self.assertRaises(config.SettingsError):
            app.settings.update({"optimizer": {"convert_types": {"wmv": True}}})
        with self.assertRaises(config.SettingsError):
            app.settings.update({"optimizer": {"convert_hold_days": 400}})

    def test_second_plex_server(self):
        """A second Plex server with the same files: its libraries paired with these by name and kind (or
        as chosen), a file's path there worked out from its place in its library folder, and converting
        waits for it too — an original goes once both have the MKV as a version of the same item, or the
        other doesn't have the file at all (when the paths line up: never on a guess)."""
        from plexcache import plex as plexmod
        from plexcache.plex import Library
        app, op, client, d, state = self._app(names=("old.avi", "b.avi"))
        avi = os.path.join(d, "old.avi")
        for f in app.files:
            f.rating_key, f.title = "r-" + os.path.basename(f.path), os.path.basename(f.path)
        app.settings.update({"optimizer": {"libraries": {"1": {"convert": True, "sync": False}}, "convert_hold_days": 0}})
        here = {"r-old.avi": None}
        app.plex = lambda timeout=30: types.SimpleNamespace(scan=lambda k, f: None, item_files=lambda rk: here.get(rk))
        there_dir = "/srv/other/Movies"
        theirs = {there_dir + "/old.avi": "t1", there_dir + "/b.avi": "t2"}
        items, scans = {"t1": [there_dir + "/old.avi"]}, []

        class Other:
            def libraries(self):
                return [Library("7", "Movies", "movie", "movie", [there_dir]), Library("8", "Music", "artist", "audio", ["/srv/other/Music"])]

            def library_files(self, lib):
                return [types.SimpleNamespace(path=p, rating_key=rk) for p, rk in theirs.items()]

            def item_files(self, rk):
                return items.get(rk)

            def scan(self, key, folder):
                scans.append(folder)

            def identity(self):
                return {"machine_id": "abc"}
        app.other.client = lambda timeout=30: Other()
        app.settings.update({"plex2": {"url": "http://10.9.0.2:32400", "token": "tok", "name": "Cabin"}, "sync": {"mirrors": "plex2"}})
        self.assertEqual({k: (v.key if v else None) for k, v in app.other.pairs().items()}, {"1": "7"})     # by name and kind
        self.assertEqual(app.other.paths_there("1", avi, app.other.pairs()["1"]), [there_dir + "/old.avi"])
        app.other.refresh()
        self.assertEqual(app.other.snapshot()["rates"], {"1": 1.0})
        run_job(op)
        here["r-old.avi"] = [avi, os.path.join(d, "old.mkv")]                                    # this server has it…
        app.conversions.act(avi, "check")
        e = app.conversions.get(avi)
        self.assertEqual((e["state"], e["servers"]), ("waiting", {"here": "same", "there": "waiting"}))  # …the other doesn't yet
        self.assertEqual(scans, [there_dir])                                                    # its folder scanned there
        items["t1"] = [there_dir + "/old.avi", there_dir + "/old.mkv"]
        app.conversions.act(avi, "check")
        self.assertEqual(app.conversions.get(avi)["state"], "seen")
        app.conversions.tick()
        self.assertFalse(os.path.exists(avi))
        # the other server doesn't have a file at all: nothing there to lose (its paths line up)
        b = os.path.join(d, "b.avi")
        del theirs[there_dir + "/b.avi"]
        app.other.indexes.clear()
        e = {"library": "1", "plex_original": b}
        self.assertEqual(app.other.where(e, time.time()), "absent")
        # …but when they don't line up, it's never taken for absent
        app.other.rates["1"] = 0.4
        self.assertEqual(app.other.where({"library": "1", "plex_original": b}, time.time()), "unpaired")
        # unpaired by choice: not watched there
        app.settings.update({"plex2": {"libraries": {"1": ""}}})
        self.assertIsNone(app.other.where({"library": "1", "plex_original": b}, time.time()))
        # plex.tv's list of servers, and the first address that answers as that server
        got = [{"provides": "server", "clientIdentifier": "abc", "name": "Cabin", "owned": True, "accessToken": "srv",
                "connections": [{"uri": "https://relay", "relay": True, "local": False}, {"uri": "http://192.168.9.2:32400", "relay": False, "local": True},
                                {"uri": "https://93.184.216.34:32400", "relay": False, "local": False}]},
               {"provides": "player", "clientIdentifier": "tv"}]
        with mock.patch.object(plexmod, "_plextv", lambda *a, **kw: got):
            (sv,) = plexmod.servers("cid", "acct")
        self.assertEqual((sv["id"], sv["token"], len(sv["connections"])), ("abc", "srv", 3))
        tried = []

        def ident(self):
            tried.append(self.url)
            if "93.184.216.34" in self.url:
                return {"machine_id": "abc"}
            raise plexmod.PlexError("no")
        with mock.patch.object(plexmod.PlexClient, "identity", ident):
            self.assertEqual(plexmod.reachable(sv), "https://93.184.216.34:32400")
        self.assertEqual(tried, ["http://192.168.9.2:32400", "https://93.184.216.34:32400"])      # the local address first, then the public one; the relay last
        # the order addresses are tried in: local IPs, then names, then other IPs — https before http,
        # IPv4 before IPv6 — the relay last: the ones that last, first
        conns = [{"uri": "https://relay.plex", "relay": True}, {"uri": "http://93.184.216.34:32400", "relay": False}, {"uri": "https://93.184.216.34:32400", "relay": False},
                 {"uri": "https://104-195-227-86.abc.plex.direct:32400", "relay": False}, {"uri": "http://10.0.0.5:32400", "relay": False},
                 {"uri": "https://10.0.0.5:32400", "relay": False}, {"uri": "https://[fd00::5]:32400", "relay": False}, {"uri": "https://plex.example.org", "relay": False}]
        self.assertEqual([c["uri"] for c in plexmod.rank_connections(conns)],
                         ["https://10.0.0.5:32400", "https://[fd00::5]:32400", "http://10.0.0.5:32400", "https://104-195-227-86.abc.plex.direct:32400",
                          "https://plex.example.org", "https://93.184.216.34:32400", "http://93.184.216.34:32400", "https://relay.plex"])
        # the saved address stops answering (a public address changed): the server is reached at another
        app.settings.update({"plex2": {"url": "https://93.184.216.34:32400", "token": "srv", "machine_id": "abc", "account_token": "acct"}})
        calls = {"n": 0}

        def flaky(self):
            calls["n"] += 1
            if "93.184.216.34" in self.url:
                raise plexmod.PlexError("gone")
            return {"machine_id": "abc"}
        got[0]["connections"] = [{"uri": "https://93.184.216.34:32400", "relay": False, "local": False}, {"uri": "https://104-195-227-86.abc.plex.direct:32400", "relay": False, "local": False}]
        app.other._reconnected = 0
        del app.other.client                                                                     # (the real client again, at the saved address)
        with mock.patch.object(plexmod, "_plextv", lambda *a, **kw: got), mock.patch.object(plexmod.PlexClient, "identity", flaky):
            self.assertEqual(app.other.run(lambda c: c.identity()), {"machine_id": "abc"})
        self.assertEqual(app.settings.section("plex2")["url"], "https://104-195-227-86.abc.plex.direct:32400")
        self.assertFalse(app.other.reconnect())                                                 # not again within RECONNECT_EVERY
        # the account's servers are listed with this server's own sign-in: no second sign-in needed
        from plexcache import plex2 as plex2mod
        seen = []
        app.settings.update({"plex": {"token": "this-servers-token"}, "plex2": {"account_token": ""}})
        with mock.patch.object(plex2mod, "servers", lambda cid, tok: seen.append(tok) or []), mock.patch.object(app.other, "_main_id", lambda: ""):
            self.assertEqual(app.other.list_servers(), [])
        self.assertEqual(seen, ["this-servers-token"])
        self.assertTrue(app.other.snapshot()["signed_in"])
        app.other.forget()
        self.assertFalse(app.other.configured())

    def test_library_sync_compare(self):
        """Library Sync, read-only: items matched across the two servers by their files' place in the
        library folder (shows through their episodes), the differences listed source → mirror — a file
        not listed there, versions grouped differently, another match, edited fields, posters chosen by
        hand, collections, labels, editions — and the direction follows the "source" setting. The mirror
        choice gates converting; the settings must be reviewed again once changed."""
        from plexcache import checks, sync
        from plexcache.plex import Library
        app, op, client, d, state = self._app(names=("film.mkv",))
        app.libraries = [{"key": "1", "title": "Movies", "type": "movie", "kind": "movie", "locations": ["/data/Movies"]},
                         {"key": "2", "title": "TV", "type": "show", "kind": "tv", "locations": ["/data/TV"]}]
        app.map_path = lambda p: p
        app.files = [types.SimpleNamespace(path="/data/Movies/A/a.mkv", library_key="1"), types.SimpleNamespace(path="/data/TV/S/e1.mkv", library_key="2")]
        item = lambda key, title, files=(), guid="plex://x", fields=None, parent="", grandparent="", edition="", colls=(), labels=(), art=(), typ="movie", summary=True: dict(
            key=key, parent=parent, grandparent=grandparent, type=typ, title=title, label=title, index=0, guid=guid, matched=not guid.startswith("local://"),
            files=[(f, 1) for f in files], edition=edition, fields=fields or {}, collections=sorted(colls), labels=sorted(labels), art_locked=sorted(art),
            summary=summary and not guid.startswith("local://"), thumb="/library/metadata/%s/thumb/1" % key, year="2001")
        here = {("1", 1): [item("10", "A", ["/data/Movies/A/a.mkv", "/data/Movies/A/a2.mkv"], fields={"title": "A!"}, colls=["Best"], art=["thumb"], edition="Extended"),
                           item("11", "B", ["/data/Movies/B/b.mkv"], guid="plex://b"),
                           item("12", "C", ["/data/Movies/C/c.mkv"], guid="local://12")],
                ("2", 2): [item("20", "Show", typ="show", colls=["Kids"])], ("2", 3): [item("21", "S1", typ="season", parent="20")],
                ("2", 4): [item("22", "Show · S01E01", ["/data/TV/S/e1.mkv"], typ="episode", parent="21", grandparent="20")]}
        there = {("7", 1): [item("70", "A", ["/srv/Movies/A/a.mkv"], fields={"title": "A"}, colls=[], art=[], edition=""),
                            item("71", "B", ["/srv/Movies/B/b.mkv"], guid="plex://other"),
                            item("72", "C", ["/srv/Movies/C/c.mkv"], guid="plex://c")],
                 ("8", 2): [item("80", "Show", typ="show", colls=["Kids"], labels=["Seen"])], ("8", 3): [item("81", "S1", typ="season", parent="80")],
                 ("8", 4): [item("82", "Show · S01E01", ["/srv/TV/S/e1.mkv"], typ="episode", parent="81", grandparent="80")]}
        mk = lambda data: types.SimpleNamespace(library_items=lambda key, leaf: list(data.get((str(key), leaf), [])),
                                                libraries=lambda: [Library("7", "Movies", "movie", "movie", ["/srv/Movies"]), Library("8", "TV", "show", "tv", ["/srv/TV"])],
                                                item_files=lambda rk: None, scan=lambda k, f: None, library_files=lambda lib, limit=None: [],
                                                identity=lambda: {"machine_id": "abc"},
                                                item_detail=lambda key: next((it for its in data.values() for it in its if it["key"] == str(key)), None))
        app.plex = lambda timeout=30: mk(here)
        app.other.client = lambda timeout=30: mk(there)
        app.run_checks_async = lambda **kw: None
        # nothing chosen yet: Health asks, comparing refuses, converted originals wait
        (h,) = [x for x in checks._sync_items(app) if "mirrored Plex server" in en(x["title"])]
        self.assertEqual(h["status"], "warn")
        with self.assertRaises(ValueError):
            app.sync.compare()
        app.conversions.items = [{"original": "/x.avi", "new": "/x.mkv", "state": "waiting", "library": "1", "plex_original": "/x.avi", "plex_new": "/x.mkv", "title": "x", "rating_key": "r", "checked": 0, "scanned": 0, "converted": 0}]
        app.conversions.tick()
        self.assertIn("isn’t chosen yet", app.conversions.items[0]["note"])
        app.settings.update({"plex2": {"url": "http://10.9.0.2:32400", "token": "tok", "name": "Cabin", "machine_id": "abc"}, "sync": {"mirrors": "plex2"}})
        self.assertEqual(app.sync.decided(), "plex2")
        rep = app.sync.compare()
        m = rep["libraries"]["1"]
        self.assertEqual((m["matched"], m["total"]), (3, 3))
        rows = {it["title"]: it for it in m["items"]}
        self.assertEqual(sorted(rows["A"]["kinds"]), ["art", "collections", "editions", "edits", "missing"])   # a2.mkv isn't listed there, and the rest
        self.assertEqual((rows["B"]["kinds"], rows["C"]["kinds"]), (["match"], ["match"]))      # another entry; unmatched here, matched there
        self.assertEqual(next(c for c in rows["A"]["changes"] if c["kind"] == "edits"), {"kind": "edits", "from": "title: A!", "to": "title: A"})
        self.assertEqual((rows["A"]["src"]["label"], rows["A"]["dst"]["key"], rows["A"]["src"]["art"]), ("A", "70", ["thumb"]))
        # C is unmatched here with no description, matched there: copying it over would replace metadata with none
        self.assertEqual((rows["C"]["warn"], rows["A"]["warn"], rows["B"]["warn"]), (True, False, False))
        self.assertEqual(en(next(c for c in rows["C"]["changes"] if c["kind"] == "match")["from"]), "unmatched")
        self.assertEqual(rep["names"]["mirror"], "Cabin")
        # edits on the other server the source doesn't have (tag fields too): listed as lost
        here[("1", 1)][2]["locked"] = []
        there[("7", 1)][2]["locked"] = ["title", "genre", "writer"]
        there[("7", 1)][2]["fields"] = {"title": "Carmen"}
        rep_l = app.sync.compare()
        c_row = next(it for it in rep_l["libraries"]["1"]["items"] if it["title"] == "C")
        lost = next(c for c in c_row["changes"] if "would be lost" in en(c["to"]))
        self.assertEqual(en(lost["to"]), "edited: title, genre, writer — these edits would be lost")
        self.assertEqual(c_row["dst"]["locked"], ["title", "genre", "writer"])
        here[("1", 1)][2]["locked"] = there[("7", 1)][2]["locked"] = []
        there[("7", 1)][2]["fields"] = {}
        self.assertEqual(c_row["src"]["files"], ["c.mkv"])                                      # (the file's name, where the match is in question)
        # an edit the listing doesn't show, only the item's own page: found all the same (the listed
        # items' pages are read from both servers)
        detail_there = dict(there[("7", 1)][1], locked=["studio"], fields={})
        mk_there = mk(there)
        mk_there.item_detail = lambda key: detail_there if str(key) == "71" else next((it for its in there.values() for it in its if it["key"] == str(key)), None)
        app.other.client = lambda timeout=30: mk_there
        rep_d = app.sync.compare()
        b_row = next(it for it in rep_d["libraries"]["1"]["items"] if it["title"] == "B")
        self.assertIn("studio", en(next(c["to"] for c in b_row["changes"] if c["kind"] == "edits")))
        self.assertEqual(b_row["dst"]["locked"], ["studio"])
        app.other.client = lambda timeout=30: mk(there)
        # a field edited the same on both: not a difference
        here[("1", 1)][1]["locked"] = there[("7", 1)][1]["locked"] = ["title", "genre"]
        here[("1", 1)][1]["fields"] = there[("7", 1)][1]["fields"] = {"title": "B"}
        rep_s = app.sync.compare()
        b_row = next(it for it in rep_s["libraries"]["1"]["items"] if it["title"] == "B")
        self.assertEqual([c["kind"] for c in b_row["changes"]], ["match"])
        here[("1", 1)][1]["locked"] = there[("7", 1)][1]["locked"] = []
        here[("1", 1)][1]["fields"] = there[("7", 1)][1]["fields"] = {}
        app.sync.compare()                                                                      # (the report as before)
        t = rep["libraries"]["2"]
        self.assertEqual((t["matched"], {k for it in t["items"] for k in it["kinds"]}), (3, {"labels"}))    # the show matched through its episode; its labels differ
        self.assertEqual(rep["totals"]["labels"], 1)
        self.assertEqual(app.sync.snapshot()["report"]["libraries"]["1"]["counts"]["edits"], 1)
        self.assertEqual(app.sync.differences("2")["title"], "TV")
        # the mirror as the source: the same pairs, the other way round
        app.settings.update({"sync": {"source": "mirror"}})
        rep2 = app.sync.compare()
        self.assertEqual(rep2["source"], "mirror")
        rows2 = {it["title"]: it for it in rep2["libraries"]["1"]["items"]}
        self.assertIn("grouping", rows2["A"]["kinds"])                                          # seen from the mirror: this server's A has a file more
        self.assertNotIn("missing", rows2["A"]["kinds"])
        self.assertFalse(rows2["C"]["warn"])                                                    # the matched side as the source: fine
        self.assertEqual(rep2["libraries"]["2"]["counts"]["labels"], 1)
        # what's compared can be narrowed
        app.settings.update({"sync": {"what": {"edits": False, "collections": False, "editions": False, "art": False}}})
        rep3 = app.sync.compare()
        self.assertEqual({k for k, n in rep3["totals"].items() if n}, {"grouping", "match"})         # (still from the mirror's side)
        with self.assertRaises(config.SettingsError):
            app.settings.update({"sync": {"what": {"posters": True}}})
        # review: the settings changed since they were approved → said; approved → quiet, until the next change
        self.assertTrue(app.sync.review_needed())
        app.sync.approve()
        self.assertFalse(app.sync.review_needed())
        app.settings.update({"sync": {"source": "this"}})
        self.assertTrue(app.sync.review_needed())
        self.assertTrue(any("changed since" in en(x["title"]) for x in checks._sync_items(app)))
        self.assertTrue(any("differences between" in en(x["title"]) for x in checks._sync_items(app)))
        # one library at a time, as inspecting: starting one pauses the other (which picks up when
        # started again), and each library's result shows as soon as it's done
        gate = threading.Event()
        slow = mk(here)
        slow_items = slow.library_items
        slow.library_items = lambda key, leaf: (gate.wait(5), slow_items(key, leaf))[1]
        app.plex = lambda timeout=30: slow
        app.sync.report = None
        app.sync.start("1")
        self.assertTrue(wait_for(lambda: app.sync.snapshot()["libs"]["1"]["running"]))
        app.sync.start("2")                                                                     # (1 paused, 2 next)
        gate.set()
        self.assertTrue(wait_for(lambda: app.sync.snapshot()["libs"]["2"].get("finished")))
        self.assertIn("2", app.sync.report["libraries"])
        self.assertTrue(app.sync.snapshot()["libs"]["1"]["paused"])
        app.sync.start("1")
        self.assertTrue(wait_for(lambda: app.sync.snapshot()["libs"]["1"].get("finished")))
        self.assertEqual(set(app.sync.report["libraries"]), {"1", "2"})
        self.assertEqual(app.sync.differences("1")["job"]["pct"], 100)
        self.assertIsNone(app.sync.snapshot()["progress"])
        app.plex = lambda timeout=30: mk(here)
        # due every compare_hours, unless off (once the worker has let go: it clears "running" after the result is in)
        self.assertTrue(wait_for(lambda: not app.sync.running))
        self.assertFalse(app.sync.due())
        self.assertTrue(app.sync.due(now=time.time() + 7 * 3600))
        app.settings.update({"sync": {"mode": "off"}})
        self.assertFalse(app.sync.due(now=time.time() + 7 * 3600))
        # another Plex Cache, seen by its marker in a library folder
        sdir = os.path.join(d, ".plexcache", "systems")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "otherbox1.json"), "w") as fh:
            fh.write("{}")
        app.libraries[0]["locations"] = [d]
        self.assertEqual(app.sync.peers(), ["otherbox1"])
        # discovery: a sample of each account server's files looked up here by path
        from plexcache import plex as plexmod
        found = [{"id": "zzz", "name": "Cabin", "owned": True, "token": "t", "connections": [{"uri": "http://c", "local": False, "relay": False}]}]
        sample = [types.SimpleNamespace(path="/srv/Movies/A/a.mkv"), types.SimpleNamespace(path="/srv/Movies/Z/z.mkv")]
        app.libraries[0]["locations"] = ["/data/Movies"]
        with mock.patch.object(sync, "servers", lambda cid, tok: found), mock.patch.object(sync, "reachable", lambda sv: "http://c"), \
                mock.patch.object(sync, "PlexClient", lambda url, tok, timeout=30: types.SimpleNamespace(
                    libraries=lambda: [Library("7", "Movies", "movie", "movie", ["/srv/Movies"])], library_files=lambda lib, limit=None: sample)), \
                mock.patch.object(app.other, "_main_id", lambda: "me"):
            (c,) = app.sync.discover()
        self.assertEqual((c["name"], c["rate"], c["libraries"][0]["found"], c["libraries"][0]["of"]), ("Cabin", 0.5, 1, 2))
        self.assertEqual(app.sync.snapshot()["candidates"][0]["id"], "zzz")
        # a report made by an older version isn't shown as current: compared again
        app.sync.report["version"] = 1
        app.sync.save()
        self.assertIsNone(sync.Sync(app).report)
        self.assertEqual(sync.Sync(app).candidates[0]["id"], "zzz")

    def test_library_sync_grouping_shapes(self):
        """Versions grouped differently on the two servers, in every shape: one item here split into
        several there, several here merged into one there, a tangle of several on both sides, an item's
        files partly missing there, one not there at all, one only there, and a file renamed on one side
        (paired by its online entry). Each group is one row with every record of both sides, so nothing
        is reported twice or lost — and the Plex checks tag the records whose versions look wrongly added."""
        from plexcache import libhealth, sync
        it = lambda key, files, guid="plex://x", locked=None: dict(key=key, parent="", grandparent="", type="movie", title=key, label=key, index=0, guid=guid,
                                                                     matched=not guid.startswith("local://"), files=[("/a/" + f, 1) for f in files], edition="",
                                                                     fields={}, locked=locked, collections=[], labels=[], art_locked=[], summary=True, tags={})
        here = [it("S1", ["Big (2000)/Big (2000) 1080p.mkv", "Big (2000)/Big (2000) 4K.mkv", "Big (2000)/Big (2000) 720p.mkv", "Big (2000)/Big (2000) a.mkv",
                         "Big (2000)/Big (2000) b.mkv", "Big (2000)/Big (2000) c.mkv", "Big (2000)/Big (2000) d.mkv"], "plex://big"),  # 7 files → 3 items there
                it("M1", ["Two (2001)/Two (2001).mkv"], "plex://two"), it("M2", ["Two (2001)/Two (2001) x.mkv"], "plex://two2"), it("M3", ["Two (2001)/Two (2001) y.mkv"], "plex://two3"),  # 3 → 1
                it("T1", ["Tangle/p.mkv", "Tangle/q.mkv"], "plex://t1"), it("T2", ["Tangle/r.mkv", "Tangle/s.mkv"], "plex://t2"),  # 2 ↔ 2, crossed
                it("P1", ["Part (2002)/Part (2002).mkv", "Part (2002)/Part (2002) extra.mkv"], "plex://part"),  # one file not there
                it("G1", ["Gone (2003)/Gone (2003).mkv"], "plex://gone"),  # not there at all
                it("R1", ["Renamed (2004)/Renamed (2004) old.mkv"], "plex://ren"),  # named otherwise there: paired by its entry
                it("W1", ["Wrong (2005)/Wrong (2005).mkv", "Wrong (2005)/Other Film (1999).mkv"], "plex://wrong")]  # a version from another film
        there = [it("D1", ["Big (2000)/Big (2000) 1080p.mkv", "Big (2000)/Big (2000) 4K.mkv"], "plex://big"), it("D2", ["Big (2000)/Big (2000) 720p.mkv", "Big (2000)/Big (2000) a.mkv", "Big (2000)/Big (2000) b.mkv"], "plex://big"),
                 it("D3", ["Big (2000)/Big (2000) c.mkv", "Big (2000)/Big (2000) d.mkv"], "plex://big"),
                 it("DM", ["Two (2001)/Two (2001).mkv", "Two (2001)/Two (2001) x.mkv", "Two (2001)/Two (2001) y.mkv"], "plex://two"),
                 it("DT1", ["Tangle/p.mkv", "Tangle/r.mkv"], "plex://t1"), it("DT2", ["Tangle/q.mkv", "Tangle/s.mkv"], "plex://t2"),
                 it("DP", ["Part (2002)/Part (2002).mkv"], "plex://part"),
                 it("DR", ["Renamed (2004)/Renamed (2004) new.mkv"], "plex://ren"),
                 it("DX", ["Only There (2006)/Only There (2006).mkv"], "plex://only"),
                 it("DW", ["Wrong (2005)/Wrong (2005).mkv", "Wrong (2005)/Other Film (1999).mkv"], "plex://wrong2")]  # (matched otherwise: a row; identical pairs have none)
        src, dst = sync._Side(here, ["/a"]), sync._Side(there, ["/a"])
        got = sync.compare_library(src, dst, {}, libhealth.enabled({}))
        rows = {r["title"]: r for r in got["items"]}
        self.assertEqual(rows["S1"]["shape"], "split")
        self.assertEqual(([x["key"] for x in rows["S1"]["src_items"]], sorted(x["key"] for x in rows["S1"]["dst_items"])), (["S1"], ["D1", "D2", "D3"]))
        self.assertIn("merge them into one", en(rows["S1"]["changes"][0]["to"]))
        self.assertEqual((rows["M1"]["shape"], sorted(x["key"] for x in rows["M1"]["src_items"]), [x["key"] for x in rows["M1"]["dst_items"]]), ("merge", ["M1", "M2", "M3"], ["DM"]))
        self.assertIn("split it into 3", en(rows["M1"]["changes"][0]["to"]))
        self.assertNotIn("M2", rows)                                                            # (one row for the group, not one per item)
        self.assertEqual((rows["T1"]["shape"], len(rows["T1"]["src_items"]), len(rows["T1"]["dst_items"])), ("regroup", 2, 2))
        self.assertIn("too tangled", en(rows["T1"]["changes"][0]["to"]))
        self.assertEqual((rows["P1"]["shape"], rows["P1"]["kinds"]), ("pair", ["missing"]))
        self.assertIn("extra.mkv", rows["P1"]["changes"][0]["from"])
        self.assertEqual((rows["G1"]["shape"], rows["G1"]["dst"]), ("missing", None))
        self.assertEqual((rows["R1"]["shape"], rows["R1"]["dst"]["key"], rows["R1"]["kinds"]), ("pair", "DR", ["missing", "grouping"]))   # paired by its entry; its file isn’t there under that name, and the item there has one more
        self.assertEqual((rows["DX"]["shape"], rows["DX"]["src"], rows["DX"]["kinds"]), ("extra", None, ["extra"]))
        self.assertEqual(got["counts"]["grouping"], 4)                                           # split, merge, tangle, and the renamed file’s item having one more
        self.assertEqual(got["counts"]["extra"], 1)
        # the Plex check on the records: W1/DW's versions are named for two films — tagged on both sides
        self.assertEqual([t["id"] for t in rows["W1"]["tags"]["src"][0]], ["versions_named_differently"])
        self.assertEqual([t["id"] for t in rows["W1"]["tags"]["dst"][0]], ["versions_named_differently"])
        self.assertEqual(rows["S1"]["tags"]["src"][0], [])                                      # (7 versions of the one film: fine)
        self.assertEqual(len(rows["S1"]["tags"]["dst"]), 3)
        # the checks turned off: no tags
        got_off = sync.compare_library(src, dst, {}, {c["id"]: False for cat in libhealth.CHECKS.values() for c in cat})
        self.assertEqual({t["id"] for r in got_off["items"] for side in ("src", "dst") for ts in r["tags"][side] for t in ts}, set())

    def test_library_health_file_checks(self):
        """The file checks: no audio; a spoken film with no subtitles (not one whose audio is zxx); subtitles
        on a zxx film unless forced. Each can be turned off."""
        from plexcache import libhealth
        on = libhealth.enabled({})
        v = {"type": "video", "codec": "V_X"}
        au = lambda lang: {"type": "audio", "codec": "A_X", "language": lang}
        sub = lambda lang, forced=False: {"type": "subtitles", "codec": "S_X", "language": lang, "forced": forced}
        ids = lambda m: [r["id"] for r in libhealth.file_checks(m, on)]
        self.assertEqual(ids({"tracks": [v]}), ["no_audio"])
        self.assertEqual(ids({"tracks": [v, au("eng")]}), ["no_subtitles"])
        self.assertEqual(ids({"tracks": [v, au("eng"), sub("eng")]}), [])
        self.assertEqual(ids({"tracks": [v, au("zxx")]}), [])                                  # no speech: no subtitles wanted
        self.assertEqual(ids({"tracks": [v, au("zxx"), sub("eng", forced=True)]}), [])          # forced: written content, belongs
        self.assertEqual(ids({"tracks": [v, au("zxx"), sub("eng")]}), ["subtitles_on_zxx"])
        self.assertEqual(ids({"tracks": [v, au("zxx"), au("fra")]}), ["no_subtitles"])          # one spoken track: speech
        self.assertEqual(ids({"tracks": [au("eng")]}), [])                                      # music: not a video file
        self.assertEqual(ids(None), [])
        off = dict(on, no_subtitles=False)
        self.assertEqual([r["id"] for r in libhealth.file_checks({"tracks": [v, au("eng")]}, off)], [])
        # off unless turned on, each narrowing "No subtitles": one in the chosen language (any code for it), one in a
        # text format (SRT, ASS) — both: that language's must be the text one. One finding either way.
        self.assertFalse(on["subtitles_language"] or on["subtitles_text"])
        srt = lambda lang: dict(sub(lang), codec="S_TEXT/UTF8")
        pgs = lambda lang: dict(sub(lang), codec="S_HDMV/PGS")
        res = lambda m, lang="en", **opts: libhealth.file_checks(m, dict(on, **opts), lang)
        ids2 = lambda m, lang="en", **opts: [r["id"] for r in res(m, lang, **opts)]
        self.assertEqual(ids2({"tracks": [v, au("eng"), srt("eng")]}, subtitles_language=True, subtitles_text=True), [])
        self.assertEqual(ids2({"tracks": [v, au("eng"), srt("en-US")]}, subtitles_language=True), [])          # ("en", "eng", "en-US": one language)
        self.assertEqual(ids2({"tracks": [v, au("eng"), srt("fra")]}, subtitles_language=True), ["no_subtitles"])
        self.assertIn("no English subtitle track (has: French (fra))", en(res({"tracks": [v, au("eng"), srt("fra")]}, subtitles_language=True)[0]["text"]))
        self.assertEqual(ids2({"tracks": [v, au("eng"), srt("fra")]}, "fr", subtitles_language=True), [])
        self.assertEqual(ids2({"tracks": [v, au("eng"), pgs("eng")]}, subtitles_text=True), ["no_subtitles"])
        self.assertIn("no SRT/ASS subtitle track", en(res({"tracks": [v, au("eng"), pgs("eng")]}, subtitles_text=True)[0]["text"]))
        self.assertEqual(ids2({"tracks": [v, au("eng"), pgs("eng"), srt("fra")]}, subtitles_text=True), [])        # (any language's text one will do)
        self.assertEqual(ids2({"tracks": [v, au("eng"), pgs("eng"), srt("fra")]}, subtitles_text=True, subtitles_language=True), ["no_subtitles"])  # (English must be the text one)
        self.assertEqual(ids2({"tracks": [v, au("eng")]}, subtitles_language=True, subtitles_text=True), ["no_subtitles"])
        self.assertEqual(ids2({"tracks": [v, au("eng"), pgs("fra")]}, subtitles_language=True, no_subtitles=False), [])  # (the options narrow the check: off with it)
        self.assertEqual(ids2({"tracks": [v, au("zxx"), pgs("eng")]}, subtitles_language=True, subtitles_text=True), ["subtitles_on_zxx"])  # (no speech)
        self.assertTrue(libhealth.same_language("eng", "en") and not libhealth.same_language("eng", "fra") and not libhealth.same_language("", "en"))
        # each check says which libraries it applies to; an audio library gets only its own
        self.assertTrue(all(c.get("applies") for cat in libhealth.CHECKS.values() for c in cat))
        audio_on = libhealth.for_kind(on, "audio")
        self.assertFalse(audio_on["no_subtitles"] or audio_on["no_audio"] or audio_on["versions_named_differently"])
        self.assertTrue(audio_on["duplicate_items"] and audio_on["audio_low_bitrate"] and audio_on["audio_uncompressed"])
        self.assertFalse(libhealth.for_kind(on, "movie")["audio_low_bitrate"])                      # (a video's bitrate is its picture)
        self.assertTrue(libhealth.for_kind(on, "movie")["audio_uncompressed"])                      # (its audio tracks are checked, though)
        # the audio checks: a lossy track below its codec's floor (per channel), uncompressed PCM
        ac = lambda m, path, kbps, ch=2, level="fair": [r["id"] for r in libhealth.audio_checks(m, audio_on, path, kbps, ch, level)]
        mp3 = {"tracks": [{"type": "audio", "codec": "A_MPEG/L3"}]}
        self.assertEqual(ac(mp3, "/m/a.mp3", 128), ["audio_low_bitrate"])
        self.assertEqual(ac(mp3, "/m/a.mp3", 256), [])
        self.assertEqual(ac(mp3, "/m/a.mp3", 128, 1), [])                                          # (mono: half the floor)
        self.assertEqual(ac({"tracks": []}, "/m/a.m4a", 96), ["audio_low_bitrate"])                 # (codec from the extension)
        self.assertEqual(ac({"tracks": []}, "/m/a.opus", 96), [])
        # the quality level scales every codec's floor alike; "poor" (as it ships) lets a 128 kbps MP3 through
        self.assertEqual((libhealth.audio_floor("mp3", "poor"), libhealth.audio_floor("mp3", "fair"), libhealth.audio_floor("mp3", "good"), libhealth.audio_floor("mp3", "best")), (128, 192, 256, 320))
        self.assertEqual((libhealth.audio_floor("aac", "poor"), libhealth.audio_floor("opus", "poor"), libhealth.audio_floor("flac", "best")), (88, 64, None))
        self.assertEqual(ac(mp3, "/m/a.mp3", 128, 2, "poor"), [])
        self.assertEqual(ac(mp3, "/m/a.mp3", 96, 2, "poor"), ["audio_low_bitrate"])
        # outdated codecs: off unless turned on; MP3 is one whatever its bitrate, Opus isn't
        self.assertFalse(audio_on["audio_outdated"])
        dated = dict(audio_on, audio_outdated=True)
        self.assertEqual([r["id"] for r in libhealth.audio_checks(mp3, dated, "/m/a.mp3", 320)], ["audio_outdated"])
        self.assertEqual([r["id"] for r in libhealth.audio_checks({"tracks": []}, dated, "/m/a.opus", 320)], [])
        self.assertIn("link", next(c for c in libhealth.CHECKS["file"] if c["id"] == "audio_outdated"))
        self.assertIn("pre-echo", en(libhealth.audio_checks(mp3, dated, "/m/a.mp3", 320)[0]["text"]))      # (each codec its own reasons)
        self.assertIn("Microsoft", en(libhealth.audio_checks({"tracks": []}, dated, "/m/a.wma", 320)[0]["text"]))
        # a video's audio tracks: outdated and uncompressed codecs flagged by track (AC-3 is normal in a film); no bitrate check
        film = {"tracks": [{"type": "video", "codec": "V_X"}, {"type": "audio", "codec": "A_AC3"}, {"type": "audio", "codec": "A_MPEG/L3"}, {"type": "audio", "codec": "A_PCM/INT/LIT"}]}
        video_on = dict(libhealth.for_kind(on, "movie"), audio_outdated=True)
        self.assertEqual([r["id"] for r in libhealth.audio_checks(film, video_on, "/m/f.mkv", 15000, kind="movie")], ["audio_outdated", "audio_uncompressed"])
        self.assertIn("inside the MKV", en(libhealth.audio_checks(film, video_on, "/m/f.mkv", 15000, kind="movie")[1]["text"]))
        self.assertEqual([r["id"] for r in libhealth.audio_checks({"tracks": [{"type": "video"}, {"type": "audio", "codec": "A_AC3"}]}, video_on, "/m/f.mkv", 15000, kind="movie")], [])
        self.assertEqual(ac({"tracks": [{"type": "audio", "codec": "A_FLAC"}]}, "/m/a.flac", 40), [])  # (lossless: never low)
        self.assertEqual(ac({"tracks": []}, "/m/a.wav", 1411), ["audio_uncompressed"])
        self.assertEqual(ac({"tracks": [{"type": "audio", "codec": "A_PCM/INT/LIT"}]}, "/m/a.mka", 1411), ["audio_uncompressed"])
        self.assertIn("Apple Lossless", en(libhealth.audio_checks({"tracks": []}, audio_on, "/m/a.wav", 1411)[0]["text"]))
        self.assertEqual(libhealth.audio_codec({"codec": "A_DTS/LOSSLESS"}, ""), "dts-hd")

    def test_platform_detection(self):
        """Where the app runs: TrueNAS for certain from the API or the host's version file (or said by
        PLEXCACHE_PLATFORM); otherwise only whether ZFS is there, TrueNAS "not detected". Health's TrueNAS
        item follows: on another Linux host there's no API to ask for."""
        from plexcache import checks, platform
        self.assertEqual((platform.detect("25.04.1")["name"], platform.detect("25.04.1")["truenas"], platform.detect("25.04.1")["how"]), ("truenas", True, "api"))
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "version"), "w") as fh:
            fh.write("24.10.2\n")
        with mock.patch.object(platform, "HOST_FILES", (os.path.join(d, "version"),)), mock.patch.dict(os.environ, {"PLEXCACHE_PLATFORM": ""}):
            got = platform.detect(None)
            self.assertEqual((got["truenas"], got["how"], got["version"]), (True, "host", "24.10.2"))
        with mock.patch.object(platform, "HOST_FILES", ()), mock.patch.dict(os.environ, {"PLEXCACHE_PLATFORM": ""}), \
                mock.patch.object(platform, "kernel_release", lambda: "6.8.12-amd64"):
            got = platform.detect(None)                                                          # (the test's fake /proc has ZFS)
            self.assertEqual((got["name"], got["truenas"]), ("linux-zfs", None))
            self.assertIn("not detected", en(got["label"]))
        # TrueNAS's own kernel, seen from inside the container: TrueNAS for certain, no key or mount needed
        with mock.patch.object(platform, "HOST_FILES", ()), mock.patch.dict(os.environ, {"PLEXCACHE_PLATFORM": ""}), \
                mock.patch.object(platform, "kernel_release", lambda: "6.18.52-debug+truenas"):
            got = platform.detect(None)
            self.assertEqual((got["name"], got["truenas"], got["how"], en(got["label"])), ("truenas", True, "kernel", "TrueNAS"))
        with mock.patch.dict(os.environ, {"PLEXCACHE_PLATFORM": "linux"}):
            self.assertIs(platform.detect(None)["truenas"], False)
        app = _new_app(truenas={"url": "", "api_key": ""})  # (no API connected)
        tn = lambda: next(i for i in checks.check_zfs(app)["items"] if "TrueNAS" in en(i["title"]))
        app.zfs = {"platform": {"truenas": False, "label": "Linux with ZFS"}}
        self.assertIn("Not running on TrueNAS", en(tn()["title"]))
        app.zfs = {"platform": {"truenas": None, "label": "x"}}
        self.assertIn("TrueNAS API not connected", en(tn()["title"]))
        self.assertIn("[System → TrueNAS](/system#truenas)", en(tn()["fix"]))
        app.zfs = {"platform": {"truenas": True, "label": "TrueNAS"}}                            # (TrueNAS for certain: the key is asked for)
        self.assertEqual((en(tn()["title"]), tn()["status"]), ("TrueNAS API key needed", "warn"))
        self.assertIn("update itself", en(tn()["detail"]))

    def test_library_view_with_an_older_mix(self):
        """A file inspected before a mix key existed (round_disk came later) still adds up on the
        Library page: the summary takes a missing key as zero instead of failing the whole view."""
        app, op, client, d, state = self._app(names=("film.mkv",))
        f = app.files[0]
        f.rating_key, f.title = "r1", "Film"
        old = {"format": "matroska", "size": 100, "tracks": [{"type": "video", "codec": "V_X"}],
               "mix": {"video": 60, "meta": 30, "unused": 10, "video_disk": 60, "meta_disk": 30, "unused_disk": 10, "blocks": 1}}  # (no round_disk)
        app.file_maps.get = lambda path, st=None, full=False: old
        view = app.library_view("1")
        self.assertEqual((view["summary"]["mix"]["files"], view["summary"]["mix"]["round_disk"]), (1, 0))

    def test_two_plex_caches_sharing_folders(self):
        """Two Plex Caches on servers sharing library folders (peers.py): each publishes its settings in
        the folder's marker (through the optimizer service), and the other refuses what would fight —
        Library Sync applying from both sides, optimizer choices that differ, a file tool just run
        there — saying so on Health. A claim's hourly refresh keeps the marker's content."""
        from plexcache import checks, peers
        from plexcache.optimize import claims
        app, op, client, d, state = self._app(names=("film.mkv",))
        app.plex_name = "Here"
        app.settings.update({"sync": {"mode": "apply", "source": "this"}})
        self.assertEqual(peers.publish(app), 1)
        mine = json.load(open(os.path.join(d, ".plexcache", "systems", app.optimizing.instance() + ".json")))
        self.assertEqual((mine["name"], mine["sync"]["mode"], mine["sync"]["source"], mine["optimizer"]["images"]), ("Here", "apply", "this", op.options("1")["images"]))
        self.assertEqual(peers.read(app), {})                                                       # (only our own marker)
        # the other side's marker arrives (the folder sync): it applies with itself as the source too
        other = {"name": "There", "version": "0.16.0", "sync": {"mode": "apply", "source": "this"}, "optimizer": dict(mine["optimizer"]), "tools": {}}
        claims.mark(d, "system-b", other)
        found = peers.read(app)
        self.assertEqual((list(found), found["system-b"]["libraries"]), (["system-b"], ["1"]))
        self.assertIn("back and forth", en(peers.sync_conflict(app)))
        app.settings.update({"sync": {"source": "mirror"}})                                          # one side the source: fine
        self.assertIsNone(peers.sync_conflict(app))
        claims.mark(d, "system-b", dict(other, sync={"mode": "apply", "source": "mirror"}))         # both pulling: fights too
        self.assertIn("back and forth", en(peers.sync_conflict(app)))
        claims.mark(d, "system-b", dict(other, sync={"mode": "compare", "source": "this"}))
        self.assertIsNone(peers.sync_conflict(app))
        # optimizer choices that change the bytes must agree
        self.assertIsNone(peers.optimizer_conflict(app, "1"))
        claims.mark(d, "system-b", dict(other, optimizer=dict(mine["optimizer"], images="remove" if mine["optimizer"]["images"] != "remove" else "compress")))
        self.assertIn("other choices (images)", en(peers.optimizer_conflict(app, "1")))
        summary = peers.summary(app)
        self.assertEqual((summary["systems"][0]["name"], list(summary["optimizer_conflicts"]), summary["publishing"]), ("There", ["1"], True))
        titles = [en(i["title"]) for i in checks._peer_items(app)]
        self.assertTrue(any("shares these folders: There" in t for t in titles) and any("Optimizer choices differ" in t for t in titles), titles)
        # a file tool just run there: ours waits; Merge (no files) doesn't
        claims.mark(d, "system-b", dict(other, tools={"versions": time.time() - 600}))
        self.assertIn("ran versions", en(peers.tool_conflict(app, "1", "folders")))
        self.assertIsNone(peers.tool_conflict(app, "1", "merge"))
        with self.assertRaises(ValueError):
            app.tools["versions"].scan_result = {"library": "1", "rows": [{"id": "x", "title": "x"}]}
            app.tools["versions"].apply_async(["x"])
        claims.mark(d, "system-b", dict(other, tools={"versions": time.time() - 2 * peers.TOOL_GAP}))
        self.assertIsNone(peers.tool_conflict(app, "1", "folders"))
        # a claim's hourly refresh of the marker keeps what was published in it
        claims.claim(d, "abcdefgh", "system-b", now=time.time() + 7200)
        self.assertEqual(json.load(open(os.path.join(d, ".plexcache", "systems", "system-b.json")))["name"], "There")
        # a tool run here is told to the other side
        app.note_tool_run("1", "folders")
        self.assertTrue(wait_for(lambda: "folders" in (json.load(open(os.path.join(d, ".plexcache", "systems", app.optimizing.instance() + ".json"))).get("tools") or {})))

    def test_update_now_through_truenas(self):
        """Update Now: with an update available and the TrueNAS API connected, TrueNAS is asked to pull
        the newer image and redeploy the app; without an update, or an app name, it's refused."""
        app = _new_app()
        with self.assertRaises(ValueError):
            app.update_now()                                                                       # (nothing newer)
        app.updates.latest = "9.0.0"
        with mock.patch.object(app.optimizing, "_containers", lambda: None), self.assertRaises(ValueError):
            app.update_now()                                                                       # (no app name without the API)
        with mock.patch.object(app.optimizing, "_containers", lambda: {"app": "plex-cache"}):
            got = app.update_now()
        self.assertEqual(got, {"updating": True, "app": "plex-cache"})
        self.assertTrue(app.updates.status()["updating"])                                        # (said as updating until the restart)
        self.assertIn(("app.pull_images", ["plex-cache", {"redeploy": True}]), TRUENAS.apps_called)
        self.assertTrue(any("pulling the newer image" in en(e["message"]) for e in app.activity.snapshot()["events"]))

    def test_library_health_shows_a_library_its_own_checks(self):
        """An audio library's Health view lists only the checks that apply to audio, and runs only those."""
        from plexcache import libhealth
        app, op, client, d, state = self._app(names=("song.mp3",))
        app.libraries = [{"key": "1", "title": "Music", "type": "artist", "kind": "audio", "locations": [d]}]
        f = app.files[0]
        f.rating_key, f.title, f.kind, f.kbps = "r1", "Song", "audio", 96
        app.file_maps.get = lambda path, st=None, full=False: {"tracks": [{"type": "audio", "codec": "A_MPEG/L3"}]}
        got = libhealth.library_health(app, "1")
        ids = [c["id"] for cat in got["checks"].values() for c in cat]
        self.assertNotIn("no_subtitles", ids)
        self.assertIn("audio_low_bitrate", ids)
        # every check that applies ran — the outdated-codec one too, though it ships off: the page shows what's switched on
        self.assertEqual((got["kind"], got["counts"], got["audio_quality"]), ("audio", {"audio_low_bitrate": 1, "audio_outdated": 1}, "poor"))
        # switched off, the finding is still in the answer (the page hides it, no new scan needed); the level moves the floor
        app.settings.update({"library_health": {"checks": {"audio_low_bitrate": False}}})
        got = libhealth.library_health(app, "1")
        self.assertEqual((got["counts"]["audio_low_bitrate"], next(c for c in got["checks"]["file"] if c["id"] == "audio_low_bitrate")["on"]), (1, False))
        app.settings.update({"library_health": {"audio_quality": "best"}})
        f.kbps = 300
        self.assertEqual(libhealth.library_health(app, "1")["counts"]["audio_low_bitrate"], 1)      # (320 is the bar at "best")
        f.kbps = 320
        self.assertNotIn("audio_low_bitrate", libhealth.library_health(app, "1")["counts"])
        with self.assertRaises(config.SettingsError):
            app.settings.update({"library_health": {"audio_quality": "loud"}})

    def test_library_health_issues_ignored(self):
        """An item's issues ignored: greyed out, not counted, the item at the bottom — kept across
        re-checks and restarts; a new kind of issue on it still warns; brought back on request."""
        from plexcache import libhealth
        app, op, client, d, state = self._app(names=("film.mkv",))
        f = app.files[0]
        f.rating_key, f.title = "r1", "Film"
        app.file_maps.files[f.path] = {"map": {"format": "matroska", "size": 100, "tracks": [{"type": "video", "codec": "V_X"}]}, "stat": {}}
        maps = lambda path, st=None: {"tracks": [{"type": "video", "codec": "V_X"}]}
        app.file_maps.get = maps
        got = libhealth.library_health(app, "1")
        self.assertEqual(([r["id"] for r in got["items"][0]["results"]], got["counts"]), (["no_audio"], {"no_audio": 1}))
        libhealth.set_ignored(app, "1", "r1", ["no_audio"])
        got = libhealth.library_health(app, "1")
        self.assertEqual((got["items"][0]["results"][0]["ignored"], got["items"][0]["live"], got["counts"]), (True, 0, {}))
        app._lh_ignored = None                                                                   # (a restart: read from disk)
        self.assertTrue(libhealth.library_health(app, "1")["items"][0]["results"][0]["ignored"])
        # a new kind of issue on the item: a warning still
        app.file_maps.get = lambda path, st=None: {"tracks": [{"type": "video", "codec": "V_X"}, {"type": "audio", "codec": "A_X", "language": "eng"}, {"type": "subtitles", "codec": "S_X", "language": "eng"}]}
        app.settings.update({"library_health": {"checks": {"subtitles_text": True}}})
        got = libhealth.library_health(app, "1")
        self.assertEqual([(r["id"], r["ignored"]) for r in got["items"][0]["results"]], [("no_subtitles", False)])
        libhealth.set_ignored(app, "1", "r1", ["no_subtitles"])
        self.assertEqual(libhealth.ignored(app)["1"]["r1"], ["no_audio", "no_subtitles"])           # (both kept)
        libhealth.set_ignored(app, "1", "r1", None)
        self.assertEqual(libhealth.ignored(app), {})
        self.assertFalse(libhealth.library_health(app, "1")["items"][0]["results"][0]["ignored"])

    def test_same_file_means_the_same_bytes(self):
        """Two files alike in size and running time aren't the same file unless their bytes say so: over
        thousands of episodes, two of one size and length do turn up (two anime episodes, say)."""
        from plexcache import libhealth
        d = tempfile.mkdtemp(dir=TMP)
        a, b, c = (os.path.join(d, n) for n in ("a.mkv", "b.mkv", "c.mkv"))
        for p, tail in ((a, b"AAAA"), (b, b"AAAA"), (c, b"CCCC")):
            with open(p, "wb") as fh:
                fh.write(b"x" * 200000 + tail)
        f = lambda p, ino: {"path": p, "local": p, "size": 200004, "duration": 1440000, "ino": ino, "dev": 1}
        self.assertTrue(libhealth._same_file(f(a, 1), f(b, 2)))                                   # the same bytes: one file copied
        self.assertFalse(libhealth._same_file(f(a, 1), f(c, 3)))                                  # the same size and length, other bytes
        self.assertTrue(libhealth._same_file(f(a, 7), f(c, 7)))                                   # one inode: a link, whatever we'd read
        self.assertFalse(libhealth._same_file(dict(f(a, 1), size=200005), f(b, 2)))                # a byte apart: not the same
        self.assertFalse(libhealth._same_file(dict(f(a, 1), duration=1441000), f(b, 2)))           # a second apart: not the same
        self.assertTrue(libhealth._same_file(dict(f(a, 1), local=""), dict(f(c, 3), local="")))    # (nothing to read: size and length must do)

    def test_library_health_duplicate_checks(self):
        """The duplicate checks: the same file (running time and size) twice on one item — a link to one
        file, or two copies — and the same file under two items. Another bitrate isn't a duplicate."""
        from plexcache import libhealth
        on = libhealth.enabled({})
        f = lambda path, dur, size, ino=0: {"path": path, "duration": dur, "size": size, "ino": ino}
        got = libhealth.duplicate_checks({"A": [f("/m/A/a.mkv", 7200000, 4e9, 1), f("/m/a.mkv", 7200000, 4e9, 1)],
                                          "B": [f("/m/b 1080p.mkv", 5400000, 3e9), f("/m/b 4K.mkv", 5400000, 9e9)],
                                          "C": [f("/m/c.mkv", 6000000, 2e9, 5)], "D": [f("/m/sub/c.mkv", 6000400, 2e9, 6)]}, on)
        self.assertEqual([r["id"] for r in got["A"]], ["duplicate_versions"])
        self.assertIn("a link to one file", en(got["A"][0]["text"]))
        self.assertNotIn("B", got)                                                              # two bitrates: not duplicates
        self.assertEqual(([r["id"] for r in got["C"]], [r["id"] for r in got["D"]]), (["duplicate_items"], ["duplicate_items"]))
        self.assertIn("two copies", en(got["C"][0]["text"]))
        off = dict(on, duplicate_items=False)
        self.assertNotIn("C", libhealth.duplicate_checks({"C": [f("/m/c.mkv", 6000000, 2e9)], "D": [f("/m/d.mkv", 6000000, 2e9)]}, off))

    def test_library_health_plex_checks(self):
        """The Plex checks: versions named for different titles or years; a locked field with nothing in it
        (read from an item's own page). Each can be turned off."""
        from plexcache import libhealth
        on = libhealth.enabled({})
        self.assertEqual(libhealth.file_title_year("/m/Alcina - Vienna State Opera (2010) Handel Blu-ray 1080i.mkv"), ("alcina vienna state opera", "2010"))
        self.assertEqual(libhealth.file_title_year("/m/The.Thing.1982.1080p.mkv"), ("the thing", "1982"))
        self.assertEqual(libhealth.file_title_year("/m/Home video.mkv"), ("home video", ""))
        ids = lambda item: [r["id"] for r in libhealth.plex_checks(item, on)]
        self.assertEqual(ids({"files": ["/m/Big (2000) 1080p.mkv", "/m/Big (2000) 4K.mkv"]}), [])
        self.assertEqual(ids({"files": ["/m/Big (2000).mkv", "/m/Big (2001).mkv"]}), ["versions_named_differently"])
        self.assertEqual(ids({"files": ["/m/Big (2000).mkv", "/m/Small (2000).mkv"]}), ["versions_named_differently"])
        self.assertEqual(ids({"files": ["/m/Big (2000) a.mkv", "/m/Big (2000) b.mkv"], "durations": [7200000, 7260000]}), [])          # a minute apart: cuts of one film
        self.assertEqual(ids({"files": ["/m/Big (2000) a.mkv", "/m/Big (2000) b.mkv"], "durations": [7200000, 5400000]}), ["versions_differ_in_length"])
        self.assertIn("1:30, 2:00", en(libhealth.plex_checks({"files": ["/m/Big (2000) a.mkv", "/m/Big (2000) b.mkv"], "durations": [7200000, 5400000]}, on)[0]["text"]))   # shortest first
        self.assertEqual(ids({"files": ["/m/Big (2000) a.mkv", "/m/Big (2000) b.mkv"], "durations": [7200000, 0]}), [])                # (a length not known: not judged)
        self.assertEqual(ids({"files": ["/m/Big (2000).mkv"], "locked": ["title", "genre"], "fields": {"title": ""}, "tags": {}}), ["locked_empty"])
        self.assertEqual(ids({"files": ["/m/Big (2000).mkv"], "locked": ["title", "genre"], "fields": {"title": "Big"}, "tags": {"genre": ["Drama"]}}), [])
        self.assertEqual(ids({"files": ["/m/Big (2000).mkv"], "locked": None}), [])              # (its page not read: not judged)
        off = dict(on, versions_named_differently=False)
        self.assertEqual([r["id"] for r in libhealth.plex_checks({"files": ["/m/Big (2000).mkv", "/m/Big (2001).mkv"]}, off)], [])

    def test_every_library_health_check_has_a_preference(self):
        """Each check — file or Plex — has a switch in the settings, is on by default, and can be turned
        off; an unknown id is refused."""
        from plexcache import libhealth
        settings = config.Settings(tempfile.mkdtemp(dir=TMP))
        on = libhealth.enabled(settings.get())
        for cat, checks in libhealth.CHECKS.items():
            self.assertIn(cat, ("file", "plex"))
            for c in checks:
                self.assertEqual(on[c["id"]], c.get("default", True), c["id"])
                settings.update({"library_health": {"checks": {c["id"]: not c.get("default", True)}}})
                self.assertEqual(libhealth.enabled(settings.get())[c["id"]], not c.get("default", True), f"{cat}: {c['id']} can't be switched")
                self.assertTrue(c.get("name") and c.get("help"), c["id"])
        self.assertEqual(libhealth.defaults()["subtitles_language"], False)
        with self.assertRaises(config.SettingsError):
            settings.update({"library_health": {"checks": {"made_up": True}}})
        settings.update({"library_health": {"subtitle_language": "FR"}})
        self.assertEqual(settings.section("library_health")["subtitle_language"], "fr")
        with self.assertRaises(config.SettingsError):
            settings.update({"library_health": {"subtitle_language": "english"}})

    def test_convert_with_the_real_tools(self):
        """The optimizer's converting, with MKVToolNix and ffmpeg: an AVI becomes an optimized MKV next to
        it (checked against it), the original untouched; an MP4 with an iTunes show and episode is left
        as it is, saying so; never over an MKV that's there; and an original is deleted only with its MKV."""
        from plexcache.optimize import convert, mkv, tools
        from plexcache.optimize.optimizer import Skipped
        bundle = "/Applications/MKVToolNix.app/Contents/MacOS"
        if os.path.isdir(bundle) and not os.environ.get("PLEXCACHE_TOOLS_PATH"):
            os.environ["PLEXCACHE_TOOLS_PATH"] = bundle
        if tools.missing():
            self.skipTest("MKVToolNix and ffmpeg aren't installed")
        d = tempfile.mkdtemp(dir=TMP)
        src = ["-f", "lavfi", "-t", "4", "-i", "testsrc=size=320x240:rate=25", "-f", "lavfi", "-t", "4", "-i", "sine"]
        avi, mp4 = os.path.join(d, "a.avi"), os.path.join(d, "b.mp4")
        tools.run("ffmpeg", "-v", "error", *src, "-c:v", "mpeg4", "-c:a", "mp2", avi)
        tools.run("ffmpeg", "-v", "error", *src, "-c:v", "mpeg4", "-c:a", "aac", "-metadata", "show=Show", "-metadata", "episode_id=E1", mp4)
        self.assertEqual(convert.inspect(avi)["reasons"], [])
        os.utime(avi, (1_600_000_000, 1_600_000_000))
        before = _sha256(avi)
        r = convert.convert_file(avi)
        self.assertEqual((r["result"], r["new"], r["verified"]), ("converted", os.path.join(d, "a.mkv"), "passed"))
        self.assertTrue(mkv.check(r["new"])["optimized"])
        self.assertEqual(int(os.stat(r["new"]).st_mtime), 1_600_000_000)                         # the original's timestamps
        self.assertEqual(_sha256(avi), before)                                                    # the original untouched
        with self.assertRaises(Skipped) as cm:
            convert.convert_file(avi)                                                             # its MKV is there
        self.assertIn("same name", str(cm.exception))
        with self.assertRaises(Skipped) as cm:
            convert.convert_file(mp4)
        self.assertIn("episode_id, show", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(d, "b.mkv")))
        self.assertFalse([n for n in os.listdir(d) if n.startswith(".plexcache")])               # nothing left behind
        with self.assertRaises(ValueError):
            convert.delete_original(mp4, os.path.join(d, "b.mkv"))                                # no MKV
        with self.assertRaises(ValueError):
            convert.delete_original(avi, mp4)                                                     # not its MKV
        convert.delete_original(avi, r["new"])
        self.assertFalse(os.path.exists(avi))

    def test_estimates_follow_the_settings(self):
        """Library Health keeps only what was read from each file; the checks and the estimate of an
        optimized file are worked out from the settings as they are now. Changing a library's cover art
        choice changes the estimate straight away — the file isn't inspected again, and the library's
        summary (kept a little while) isn't kept past the change."""
        from plexcache import filemap
        app, op, client, d, state = self._app(names=("film.mkv",))
        path = os.path.join(d, "film.mkv")
        big = ("cover.jpg", "image/jpeg", b"\xff\xd8" + os.urandom(300_000))
        build_mkv(path, front_atts=[big, FONT], cluster=os.urandom(3 * 1024 * 1024))
        os.utime(path, (time.time() - 3600, time.time() - 3600))
        f = app.files[0]
        f.poster, f.matched, f.title, f.rating_key, f.size = "/library/metadata/5/thumb/1", True, "Film", "5", os.path.getsize(path)
        app._library_file = lambda p: f if p == path else None
        app.record_for = lambda key: 256 * 1024
        app.file_maps.read(path, force=True)
        with open(filemap.__file__, encoding="utf-8") as fh:
            self.assertNotIn("settings", fh.read())                                                  # what's kept of a file is read from it only
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "images": "compress"}}}})
        before = app.file_view(path)["projected"]
        self.assertEqual(before["removed"], {})
        summary = app.library_view("1")["summary"]["projected"]
        self.assertEqual(summary["removed"], {})
        views = app.library_view("1")["summary"]["views"]                                          # one chart: now, and once optimized
        self.assertEqual((sorted(views), views["after"]["gone"], views["all"]["gone"]), (["after", "all", "rewrite"], True, False))
        rev = app.settings.rev
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "images": "remove"}}}})
        self.assertEqual(app.settings.rev, rev + 1)
        after = app.file_view(path)["projected"]
        self.assertGreater(after["removed"]["image"], 300_000)                                     # straight away, not inspected again
        self.assertLess(after["mix"]["meta"], before["mix"]["meta"])
        self.assertGreater(app.library_view("1")["summary"]["projected"]["removed"]["image"], 300_000)
        # Plex's poster: its real size once it's known (fetched for one file; learnt in the background for a library)
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "images": "plex"}}}})
        with mock.patch.object(op, "_poster", lambda p: b"p" * 50_000):
            cover = app.file_view(path)["projected"]["cover"]
        self.assertEqual((cover["before"] > 300_000, cover["after"], cover["known"]), (True, 50_000, True))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "images": "plex", "tags": "keep"}}}})
        self.assertEqual(op.poster_size("/library/metadata/5/thumb/1"), 50_000)                    # kept by its path

    def test_tag_choice(self):
        """Which files' tags go under each tag choice: never with "keep"; with "excessive" only over
        256 KB; with "strip" whenever there are any besides ours. The choice is checked where it's set."""
        from plexcache.optimize import optimizer as opt
        big, small, ours_only = ({"foreign_tags": True, "tags_size": 300 * 1024}, {"foreign_tags": True, "tags_size": 2000},
                                 {"foreign_tags": False, "tags_size": 300})
        self.assertEqual([[opt.strips_tags(m, c) for c in (big, small, ours_only)] for m in opt.TAG_MODES],
                         [[False, False, False], [True, False, False], [True, True, False]])
        from plexcache.config import SettingsError, Settings
        settings = Settings(tempfile.mkdtemp(dir=TMP))
        settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "tags": "strip"}}}})
        self.assertEqual(settings.get()["optimizer"]["libraries"]["1"]["tags"], "strip")
        with self.assertRaises(SettingsError):
            settings.update({"optimizer": {"libraries": {"1": {"tags": "sometimes"}}}})

    def test_optimizer_guards(self):
        """The optimizer's own checks, without its tools: a big file's seed takes its end into account; tags
        that can't be read start afresh; no room for a copy, or a file that changed meanwhile, is skipped;
        and a conflict copy that differs from the file is kept."""
        from plexcache.optimize import optimizer as opt
        from plexcache.optimize.tools import ToolError
        d = tempfile.mkdtemp(dir=TMP)
        big = os.path.join(d, "big.mkv")
        with open(big, "wb") as fh:
            fh.write(b"\x00" * (3 * MiB))
        seed = opt.fingerprint_seed(big)
        with open(big, "r+b") as fh:
            fh.seek(3 * MiB - 10)
            fh.write(b"different")
        self.assertNotEqual(opt.fingerprint_seed(big), seed)                     # (the last MiB counts)
        with mock.patch.object(opt, "run", mock.Mock(side_effect=ToolError("mkvextract failed: bad file"))):
            path = opt._global_tags(big, d, {"PLEXCACHE_OPTIMIZER": "0.1.2"})
        with open(path, encoding="utf-8") as fh:
            self.assertIn("PLEXCACHE_OPTIMIZER", fh.read())
        full = types.SimpleNamespace(f_bavail=1, f_frsize=4096)
        with mock.patch.object(opt.os, "statvfs", lambda folder: full):
            with self.assertRaises(opt.Skipped) as cm:
                opt._prepare(big)
        self.assertIn("not enough free space", str(cm.exception))
        st = os.stat(big)
        out = os.path.join(d, "new.mkv")
        with open(out, "wb") as fh:
            fh.write(b"new")
        with open(big, "ab") as fh:                                               # changed meanwhile
            fh.write(b"more")
        with mock.patch.object(opt, "_copy_meta", lambda *a: None):
            with self.assertRaises(opt.Skipped) as cm:
                opt._swap(st, big, out, d, False)
        self.assertIn("changed while it was being written again", str(cm.exception))
        original = os.path.join(d, "Film.mkv")
        copy = os.path.join(d, "Film.sync-conflict-20261018-120000-ABCDEFG.mkv")
        for p in (original, copy):
            build_mkv(p, ours={"PLEXCACHE_OPTIMIZER": "0.1.2"})
        with mock.patch.object(opt, "stream_hashes", lambda p, *a: p):
            self.assertEqual(opt.resolve_conflict(copy, original), {"result": "kept", "reason": "the streams differ"})
        atts = iter([json.dumps({"attachments": [{"file_name": "a.ttf", "size": 1}]}), json.dumps({"attachments": []})])
        with mock.patch.object(opt, "stream_hashes", lambda p, *a: "same"), mock.patch.object(opt, "run", lambda *a, **kw: next(atts)):
            self.assertEqual(opt.resolve_conflict(copy, original), {"result": "kept", "reason": "the attachments differ"})

    def test_tools_that_hang_or_cant_run(self):
        """A tool that hangs is stopped at its time limit — with or without progress being watched,
        and even when it says nothing — and one that can't run has no version: never a hang."""
        from plexcache.optimize import tools
        bin_dir = tempfile.mkdtemp(dir=TMP)
        for name in ("mkvmerge", "ffmpeg"):
            with open(os.path.join(bin_dir, name), "w") as fh:
                fh.write("#!/bin/sh\nexec /bin/sleep 30\n")
            os.chmod(os.path.join(bin_dir, name), 0o755)
        with open(os.path.join(bin_dir, "ffprobe"), "wb") as fh:
            fh.write(b"\x00\x01 not a program")
        os.chmod(os.path.join(bin_dir, "ffprobe"), 0o755)
        with mock.patch.dict(os.environ, {"PLEXCACHE_TOOLS_PATH": bin_dir, "PATH": bin_dir}):
            for kw in ({}, {"lines": lambda line: None}):
                t0 = time.time()
                with self.assertRaises(tools.ToolError) as e:
                    tools.run("mkvmerge", timeout=0.5, **kw)
                self.assertEqual(str(e.exception), "mkvmerge took too long")
                self.assertLess(time.time() - t0, 10)
            self.assertIsNone(tools.version("ffprobe"))
            with self.assertRaises(tools.ToolError) as e:
                tools.run("mkvextract")
            self.assertEqual(str(e.exception), "mkvextract isn't installed")

    def test_claims_tidy_up(self):
        """Releasing a claim that's gone is fine; other keys' claims, unreadable ones and stale ones
        (a system that stopped mid-way) aren't counted, and the stale ones are removed."""
        from plexcache.optimize import claims
        folder = tempfile.mkdtemp(dir=TMP)
        now = time.time()
        self.assertEqual(claims.claim(folder, "key1", "mine", release=True, now=now)["claims"], [])
        cdir = os.path.join(folder, ".plexcache", "claims")
        os.makedirs(cdir, exist_ok=True)
        for name, text in (("otherkey.your.json", '{"instance": "your", "at": %f}' % now), ("key1.brok.json", "{not json"),
                           ("key1.olds.json", '{"instance": "olds", "at": %f}' % (now - claims.STALE - 60))):
            with open(os.path.join(cdir, name), "w") as fh:
                fh.write(text)
        got = claims.claim(folder, "key1", "mine", now=now)
        self.assertEqual([c["instance"] for c in got["claims"]], ["mine"])
        self.assertFalse(os.path.exists(os.path.join(cdir, "key1.olds.json")))

    def test_image_compression_keeps_the_original(self):
        """Lossless compression keeps the original whenever it can't be sure: the tool failed or wrote
        nothing, or the pixels couldn't be compared."""
        from plexcache.optimize import images
        d = tempfile.mkdtemp(dir=TMP)
        src, out = os.path.join(d, "cover.jpg"), os.path.join(d, "small.jpg")
        with open(src, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0" + b"\x00" * 4000)

        def writes(data):
            def run(*args, **kw):
                if data is not None:
                    with open(out, "wb") as fh:
                        fh.write(data)
            return run
        with mock.patch.object(images, "find", lambda name: "/bin/" + name):
            with mock.patch.object(images, "run", mock.Mock(side_effect=images.ToolError("jpegtran failed: bad"))):
                self.assertEqual(images.compress(src, out)["why"], "jpegtran failed: bad")
            with mock.patch.object(images, "run", writes(None)):
                self.assertEqual(images.compress(src, out)["why"], "jpegtran wrote nothing")
            with mock.patch.object(images, "run", writes(b"\xff\xd8" + b"\x00" * 100)), \
                    mock.patch.object(images, "pixels", mock.Mock(side_effect=images.ToolError("ffmpeg failed"))):
                got = images.compress(src, out)
            self.assertEqual((got["path"], got["why"]), (src, "couldn't be checked: ffmpeg failed"))
            self.assertFalse(os.path.exists(out))

    def test_file_progress(self):
        """How far through a file: read from mkvmerge's and ffmpeg's own progress lines as they come,
        weighted across the stages, and for a rewrite from the bytes copied and read back."""
        from plexcache.optimize import optimizer as opt
        from plexcache.optimize import tools
        bin_dir = tempfile.mkdtemp(dir=TMP)
        scripts = {"mkvmerge": "echo '#GUI#progress 0%'; echo '#GUI#progress 50%'; echo '#GUI#progress 100%'; echo done",
                   "ffmpeg": "echo out_time_us=5000000 >&2; echo out_time_us=10000000 >&2; echo 0,v,SHA256=abc",
                   "mkvextract": "echo '#GUI#progress 10%'; echo '#GUI#error the disk is full'; exit 2"}
        for name, body in scripts.items():
            with open(os.path.join(bin_dir, name), "w") as fh:
                fh.write("#!/bin/sh\n" + body + "\n")
            os.chmod(os.path.join(bin_dir, name), 0o755)
        seen = []
        with mock.patch.dict(os.environ, {"PLEXCACHE_TOOLS_PATH": bin_dir}):
            self.assertIn("done", tools.run("mkvmerge", lines=seen.append))
            self.assertEqual([x for x in seen if x.startswith("#GUI#progress")], ["#GUI#progress 0%", "#GUI#progress 50%", "#GUI#progress 100%"])
            with self.assertRaises(tools.ToolError) as e:
                tools.run("mkvextract", lines=lambda line: 1 / 0)                   # (a broken watcher doesn't stop it)
            self.assertEqual(str(e.exception), "mkvextract failed: the disk is full")
            fractions = []
            self.assertEqual(opt.stream_hashes("x.mkv", fractions.append, 20 * 10 ** 9), "0,v,SHA256=abc\n")
            self.assertEqual(fractions, [0.25, 0.5])
            self.assertEqual(opt.stream_hashes("x.mkv"), "0,v,SHA256=abc\n")      # without: no progress asked for
            with open(os.path.join(bin_dir, "ffprobe"), "w") as fh:
                fh.write("#!/bin/sh\necho 'x.mkv: Invalid data found' >&2; echo progress=end >&2; exit 1\n")
            os.chmod(os.path.join(bin_dir, "ffprobe"), 0o755)
            with self.assertRaises(tools.ToolError) as e:
                tools.run("ffprobe", lines=lambda line: None)
            self.assertEqual(str(e.exception), "ffprobe failed: x.mkv: Invalid data found")   # the error, not the progress
        # the stages, weighted: remuxing counts twice each check
        pct = []
        remux, new, old = opt._stages(pct.append, 2, 1, 1)
        remux(0.5)
        new(1)
        old(0.5)
        old(7)                                                                     # (never past the end)
        self.assertEqual(pct, [25.0, 75.0, 87.5, 100.0])
        # a rewrite: the bytes copied, then read back
        p = self.path("a.bin")
        with open(p, "wb") as fh:
            fh.write(os.urandom(9 * 1024 * 1024))
        pct = []
        self.assertEqual(opt.rewrite_file(p, progress=pct.append)["result"], "rewritten")
        self.assertEqual((pct[0] < 50, pct[-1]), (True, 100.0))
        self.assertEqual(pct, sorted(pct))

    def test_links_are_never_optimized(self):
        """A link swapped in for a media file is refused: the optimizer works only on the file itself."""
        from plexcache.optimize import optimizer as opt
        real = self.path("real.mkv")
        build_mkv(real)
        link = os.path.join(os.path.dirname(real), "link.mkv")
        os.symlink(real, link)
        for run in (lambda: opt.rewrite_file(link), lambda: opt.optimize_file(link)):
            with self.assertRaises(opt.Skipped):
                run()
        self.assertTrue(os.path.islink(link))

    def test_chapters_move_to_the_end(self):
        """Chapters (and attachments) in front of the video move to the end, after the attachments: the
        start keeps only what a player needs first, and metadata together compresses."""
        from plexcache.optimize import mkv
        p = self.path("ch.mkv")
        build_mkv(p, chapters=True, front_atts=[FONT], pgs_algo=0)
        self.assertEqual(set(mkv.check(p)["problems"]), {"attachments_front", "chapters_front"})
        self.assertEqual(mkv.reorder_tail(p), ["Attachments", "Chapters", "Cues", "Tags"])
        c = mkv.check(p)
        self.assertEqual((c["problems"], c["optimized"]), ([], True))
        lay = mkv.read_layout(p)
        self.assertEqual([x[0] for x in lay["front"] if x[0] != "Void"], ["SeekHead", "Info", "Tracks"])
        self.assertGreater(lay["seek"]["Chapters"], lay["first_cluster"])

    def test_copy_meta_owners_and_attributes(self):
        """An owner that can't be kept skips the file (unless allowed); attributes the system won't
        copy are passed over, real errors aren't; and the checks on conflict copies that aren't ours."""
        from plexcache.optimize import optimizer as opt
        src, dst = self.path("a"), self.path("b")
        for p in (src, dst):
            open(p, "wb").close()
        st = os.stat(src)
        from types import SimpleNamespace
        other = SimpleNamespace(st_uid=st.st_uid + 1, st_gid=st.st_gid, st_mode=st.st_mode, st_atime_ns=st.st_atime_ns,
                                st_mtime_ns=st.st_mtime_ns)                        # owned by someone else
        with mock.patch.object(os, "chown", mock.Mock(side_effect=PermissionError)):
            with self.assertRaises(opt.Skipped):
                opt._copy_meta(other, src, dst, False)
            opt._copy_meta(other, src, dst, True)                                    # allowed: carries on
        refuse = lambda code: mock.Mock(side_effect=OSError(code, "no"))
        with mock.patch.object(os, "listxattr", lambda p: ["user.a"], create=True), \
                mock.patch.object(os, "getxattr", lambda p, n: b"v", create=True):
            with mock.patch.object(os, "setxattr", refuse(errno.EPERM), create=True):
                opt._copy_meta(st, src, dst, False)                                  # not allowed: passed over
            with mock.patch.object(os, "setxattr", refuse(errno.EIO), create=True), self.assertRaises(OSError):
                opt._copy_meta(st, src, dst, False)
        with mock.patch.object(os, "listxattr", refuse(errno.ENOTSUP), create=True):
            opt._copy_meta(st, src, dst, False)
        with mock.patch.object(os, "listxattr", refuse(errno.EIO), create=True), self.assertRaises(OSError):
            opt._copy_meta(st, src, dst, False)
        # reading a file that's gone, in the background
        with self.assertRaises(OSError):
            opt._Hasher(self.path("missing")).result()
        # conflict copies: only one of that very file, both Matroska, both optimized by us
        d = os.path.dirname(src)
        original = os.path.join(d, "film.mkv")
        copy = os.path.join(d, "film.sync-conflict-20261017-101500-ABCDEF1.mkv")
        self.assertEqual(opt.resolve_conflict(copy, original)["reason"], "not a conflict copy of that file")
        notes, notes_copy = os.path.join(d, "notes.txt"), os.path.join(d, "notes.sync-conflict-20261017-101500-ABCDEF1.txt")
        for p in (notes, notes_copy):
            with open(p, "w") as fh:
                fh.write("not mkv")
        self.assertEqual(opt.resolve_conflict(notes_copy, notes)["reason"], "not Matroska")
        for p in (original, copy):
            build_mkv(p, pgs_algo=0)
        self.assertEqual(opt.resolve_conflict(copy, original)["reason"], "not both optimized by Plex Cache")

    def test_check(self):
        from plexcache.optimize import mkv
        cases = [
            ({"pgs_algo": 0}, True, []),
            ({"pgs_algo": "none"}, True, ["subs_uncompressed"]),
            ({"pgs_algo": 0, "front_atts": [FONT]}, True, ["attachments_front"]),
            ({"pgs_algo": 0, "tail": ("Cues", "Tags", "Attachments"), "tail_atts": [COVER, FONT]}, True, ["tail_order"]),
            ({"pgs_algo": 0, "tail": ("Attachments", "Cues", "Tags"), "tail_atts": [COVER, FONT]}, True, []),
            ({"pgs_algo": 0, "tail": ("Attachments", "Cues", "Tags"), "tail_atts": [FONT, COVER]}, True, ["images_order"]),
            ({"pgs_algo": 0, "tail": ("Attachments", "Cues", "Tags"), "tail_atts": [COVER, FONT]}, False, ["images"]),
            ({"pgs_algo": 0, "void": 2 * 1024 * 1024}, True, ["void"]),
        ]
        for kw, keep, problems in cases:
            p = self.path("a.mkv")
            build_mkv(p, **kw)
            c = mkv.check(p, keep)
            self.assertEqual((c["applies"], c["problems"], c["optimized"]), (True, problems, not problems), kw)
        self.assertEqual(mkv.check(self.path("a.mp4")), {"applies": False})
        bad = self.path("bad.mkv")
        with open(bad, "wb") as fh:
            fh.write(b"not a matroska file at all")
        self.assertEqual(mkv.check(bad)["problems"], ["unreadable"])

    def test_reorder_tail(self):
        from plexcache.optimize import mkv
        p = self.path("t.mkv")
        built = build_mkv(p, pgs_algo=0, tail=("Cues", "Tags", "Attachments"), tail_atts=[COVER, FONT])
        read = lambda: _read_bytes(p)
        before = read()
        self.assertEqual(mkv.reorder_tail(p), ["Attachments", "Cues", "Tags"])
        after = read()
        self.assertEqual(len(after), len(before))
        self.assertIn(built["cluster"], after)                      # the video isn't touched
        self.assertEqual(after.index(built["cluster"]), before.index(built["cluster"]))
        self.assertTrue(mkv.check(p)["optimized"])
        lay = mkv.read_layout(p)                                    # the SeekHead points at each one
        for name, eid in (("Attachments", "1941A469"), ("Cues", "1C53BB6B"), ("Tags", "1254C367")):
            self.assertEqual(after[lay["seek"][name]:lay["seek"][name] + 4], bytes.fromhex(eid), name)
        self.assertEqual([a["name"] for a in lay["attachments"]], ["cover.jpg", "font.ttf"])
        self.assertEqual(mkv.reorder_tail(p), ["Attachments", "Cues", "Tags"])  # running it again changes nothing
        self.assertEqual(read(), after)

    def test_attachment_order(self):
        from plexcache.optimize.optimizer import plan_attachments
        d = tempfile.mkdtemp(dir=TMP)
        files = {}
        for i, data in enumerate((b"text " * 2000, os.urandom(10000), os.urandom(5000))):
            files[i] = os.path.join(d, str(i))
            with open(files[i], "wb") as fh:
                fh.write(data)
        atts = [{"id": 0, "image": False, "name": "notes.txt"}, {"id": 1, "image": False, "name": "font.otf"},
                {"id": 2, "image": True, "name": "cover.jpg"}]
        # images first (next to the video), then least to most compressible
        self.assertEqual([a["id"] for a in plan_attachments(atts, files, True)], [2, 1, 0])
        self.assertEqual([a["id"] for a in plan_attachments(atts, files, False)], [1, 0])

    def test_optimize_with_the_real_tools(self):
        from plexcache.optimize import VERSION, mkv, tools
        from plexcache.optimize import optimizer as opt
        from plexcache.optimize.optimizer import COVER_PREFIX, Skipped, optimize_file, stream_hashes
        bundle = "/Applications/MKVToolNix.app/Contents/MacOS"
        if os.path.isdir(bundle) and not os.environ.get("PLEXCACHE_TOOLS_PATH"):
            os.environ["PLEXCACHE_TOOLS_PATH"] = bundle
        if tools.missing():
            self.skipTest("MKVToolNix and ffmpeg aren't installed")
        d = tempfile.mkdtemp(dir=TMP)
        src = os.path.join(d, "src.mkv")
        tools.run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24", "-t", "3",
                  "-c:v", "mpeg4", "-metadata", "COMMENT=keep me", os.path.join(d, "v.mkv"))
        for name, data in (("font.ttf", FONT[2]), ("cover.jpg", COVER[2])):
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(data)
        tools.run("mkvmerge", "-q", "-o", src, "--attachment-mime-type", "image/jpeg", "--attach-file", os.path.join(d, "cover.jpg"),
                  "--attachment-mime-type", "font/ttf", "--attach-file", os.path.join(d, "font.ttf"), os.path.join(d, "v.mkv"))
        self.assertEqual(mkv.check(src)["problems"], ["attachments_front"])
        uids = lambda p: [(a["file_name"], a["properties"]["uid"]) for a in json.loads(tools.run("mkvmerge", "-J", p))["attachments"]]
        hashes = stream_hashes(src)
        os.utime(src, (1_600_000_000, 1_600_000_000))
        keep = os.path.join(d, "keep.mkv")
        shutil.copy2(src, keep)
        r = optimize_file(keep)
        self.assertEqual((r["result"], r["tags"]), ("optimized", {"PLEXCACHE_OPTIMIZER": VERSION}))
        c = mkv.check(keep)
        self.assertTrue(c["optimized"])
        self.assertEqual(c["ours"], {"PLEXCACHE_OPTIMIZER": VERSION})          # tagged, in the file itself
        self.assertEqual(stream_hashes(keep), hashes)
        self.assertEqual(int(os.stat(keep).st_mtime), 1_600_000_000)          # timestamps kept
        self.assertEqual([a["name"] for a in mkv.read_layout(keep)["attachments"]], ["cover.jpg", "font.ttf"])
        self.assertEqual(sorted(uids(keep)), sorted(uids(src)))               # each attachment keeps its ID
        extracted = tools.run("mkvextract", keep, "tags", "-")
        self.assertIn("keep me", extracted)                                   # the file's own tags kept…
        self.assertEqual(extracted.count("PLEXCACHE_OPTIMIZER"), 1)           # …and ours there once
        with open(keep, "rb") as fh:
            self.assertEqual(r["sha256"], hashlib.sha256(fh.read()).hexdigest())  # the file it left
        # deterministic: the same file, optimized on another system, is the same bytes
        twin = os.path.join(d, "twin.mkv")
        shutil.copy2(src, twin)
        optimize_file(twin)
        with open(keep, "rb") as a, open(twin, "rb") as b:
            self.assertEqual(a.read(), b.read())
        # tagged by this version and laid out right: left as it is (nothing read but its layout)
        r = optimize_file(keep, "keep")
        self.assertEqual((r["result"], r["version"], r["file"]["ino"]), ("already", VERSION, os.stat(keep).st_ino))
        self.assertEqual(optimize_file(keep, force=True)["result"], "optimized")  # written again when asked
        extracted = tools.run("mkvextract", keep, "tags", "-")
        self.assertEqual(extracted.count("PLEXCACHE_OPTIMIZER"), 1)           # still once
        # the tag choice: the file's own tags are small, so "excessive" leaves them; "strip" takes them
        # (and the tracks' statistics) out, ours written back; then there's nothing more to strip
        self.assertTrue(mkv.check(keep)["foreign_tags"])
        self.assertFalse(opt.strips_tags("excessive", mkv.check(keep)))
        self.assertEqual(optimize_file(keep, tags_mode="excessive")["result"], "already")
        self.assertEqual(optimize_file(keep, tags_mode="strip")["result"], "optimized")
        extracted = tools.run("mkvextract", keep, "tags", "-")
        self.assertNotIn("keep me", extracted)
        self.assertEqual(extracted.count("PLEXCACHE_OPTIMIZER"), 1)
        self.assertFalse(mkv.check(keep)["foreign_tags"])
        self.assertEqual(optimize_file(keep, tags_mode="strip")["result"], "already")
        self.assertEqual(stream_hashes(keep), hashes)
        with self.assertRaises(ValueError):
            optimize_file(keep, tags_mode="some")
        # small attachments: mkvpropedit would put them before the video, so they're written by us
        self.assertLess(mkv.read_layout(keep)["front"][-1][1], mkv.read_layout(keep)["first_cluster"])
        self.assertNotIn("Attachments", [n for n, _, _ in mkv.read_layout(keep)["front"]])
        drop = os.path.join(d, "drop.mkv")
        shutil.copy2(src, drop)
        r = optimize_file(drop, "remove")
        self.assertEqual((r["images_before"], r["images_after"]), (1, 0))
        self.assertEqual([a["name"] for a in mkv.read_layout(drop)["attachments"]], ["font.ttf"])
        # Plex's poster: in place of the cover art, as cover.png, losslessly compressed, and tagged with its hash
        poster = os.path.join(d, "poster.png")
        tools.run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=200x300", "-frames:v", "1",
                  "-compression_level", "0", poster)
        plex = os.path.join(d, "plex.mkv")
        shutil.copy2(src, plex)
        with open(poster, "rb") as fh:
            data = fh.read()
        r = optimize_file(plex, "plex", cover=data)
        self.assertTrue(r["cover"])
        self.assertEqual(mkv.check(plex)["ours"]["PLEXCACHE_COVER"], COVER_PREFIX + hashlib.sha256(data).hexdigest())
        atts = mkv.read_layout(plex)["attachments"]
        self.assertEqual([(a["name"], a["mime"]) for a in atts], [("cover.png", "image/png"), ("font.ttf", "font/ttf")])
        self.assertEqual(stream_hashes(plex), hashes)
        if tools.find("oxipng"):
            self.assertGreater(r["images_saved"], 0)
            self.assertEqual(r["images_left"], 0)
        else:
            self.assertEqual(r["images_left"], 1)  # kept as it was, and said so
        # kept with the images (keep, compress); gone with them (remove)
        self.assertIn("PLEXCACHE_COVER", optimize_file(plex, "compress", force=True)["tags"])
        self.assertNotIn("PLEXCACHE_COVER", optimize_file(plex, "remove", force=True)["tags"])
        # a newer optimizer's file is never touched by an older one
        with mock.patch.object(opt, "VERSION", "0.9.0"):
            optimize_file(plex, "compress", force=True)
        with self.assertRaises(Skipped):
            optimize_file(plex, "compress", force=True)
        with self.assertRaises(ValueError):
            optimize_file(plex, "sometimes")
        self.assertFalse([n for n in os.listdir(d) if n.startswith(".plexcache-optimize")])  # cleaned up
        with self.assertRaises(Skipped):
            optimize_file(os.path.join(d, "v.mp4"))
        # a Syncthing conflict copy we made (two systems at once): deleted; another one kept
        copy = os.path.join(d, "keep.sync-conflict-20261017-101500-ABCDEF1.mkv")
        shutil.copy2(keep, copy)
        self.assertEqual(opt.conflict_original(copy), keep)
        self.assertEqual(opt.resolve_conflict(copy, keep)["result"], "deleted")
        self.assertFalse(os.path.exists(copy))
        shutil.copy2(src, copy)                                                # not optimized by us
        self.assertEqual(opt.resolve_conflict(copy, keep), {"result": "kept", "reason": "not both optimized by Plex Cache"})
        self.assertTrue(os.path.exists(copy))

    def test_rewrite_file(self):
        from plexcache.optimize import VERSION
        from plexcache.optimize.optimizer import Skipped, rewrite_file
        d = tempfile.mkdtemp(dir=TMP)
        p = os.path.join(d, "song.flac")
        data = os.urandom(3 * 1024 * 1024 + 17)
        with open(p, "wb") as fh:
            fh.write(data)
        os.chmod(p, 0o640)
        os.utime(p, (1_600_000_000, 1_600_000_000))
        ino = os.stat(p).st_ino
        steps = []
        r = rewrite_file(p, log=steps.append)
        self.assertEqual((r["result"], r["version"], r["sha256"]), ("rewritten", VERSION, hashlib.sha256(data).hexdigest()))
        with open(p, "rb") as fh:
            self.assertEqual(fh.read(), data)                                    # byte for byte
        st = os.stat(p)
        self.assertNotEqual(st.st_ino, ino)                                     # a new file, written now
        self.assertEqual((r["file"]["ino"], int(st.st_mtime), st.st_mode & 0o777), (st.st_ino, 1_600_000_000, 0o640))
        self.assertEqual(steps, ["copying", "checking the copy"])
        self.assertFalse([n for n in os.listdir(d) if n.startswith(".plexcache-optimize")])  # cleaned up
        os.link(p, p + ".link")
        with self.assertRaises(Skipped):
            rewrite_file(p)                                                     # a rename would split the link

    def test_command_line(self):
        """python -m plexcache.optimize: check files, optimize them (each result said), or run the service."""
        import contextlib
        import io
        from plexcache.optimize import __main__ as cli
        from plexcache.optimize.optimizer import Skipped
        from plexcache.optimize.tools import ToolError
        out = io.StringIO()
        p = self.path("a.mkv")
        build_mkv(p, pgs_algo=0)
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--check", p, __file__]), 0)
        self.assertIn("a.mkv: ", out.getvalue())
        self.assertIn("not Matroska", out.getvalue())
        with mock.patch.object(cli, "missing", lambda: ["mkvmerge"]), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main([p]), 2)
        self.assertIn("missing tools: mkvmerge", err.getvalue())
        done = {"result": "optimized", "before": 2000, "after": 1500, "images_saved": 10, "seconds": 1.5}
        results = iter([done, Skipped("playing"), ToolError("mkvmerge failed"), {"result": "already"}])

        def fake(f, images, **kw):
            kw["log"]("remuxing")
            r = next(results)
            if isinstance(r, Exception):
                raise r
            return r
        cover = self.path("poster.png")
        with open(cover, "wb") as fh:
            fh.write(b"png")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "missing", lambda: []), mock.patch.object(cli, "optimize_file", fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--images", "plex", "--cover", cover, "a", "b", "c", "d"]), 1)   # one failed
        self.assertIn("a: optimized (2,000 → 1,500 bytes", out.getvalue())
        self.assertIn("b: skipped — playing", out.getvalue())
        self.assertIn("c: FAILED — mkvmerge failed", err.getvalue())
        self.assertIn("d: already", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--images", "plex", p])                                      # needs --cover
        with mock.patch("plexcache.optimize.server.serve", lambda: 0):
            self.assertEqual(cli.main(["serve"]), 0)

    def test_healthcheck(self):
        """The image's health check: either service answering is healthy."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from plexcache import healthcheck

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        closed = str(free.getsockname()[1])
        free.close()
        with mock.patch.dict(os.environ, {"PLEXCACHE_PORT": closed, "PLEXCACHE_OPTIMIZER_PORT": str(httpd.server_address[1])}):
            self.assertEqual(healthcheck.main(), 0)
        with mock.patch.dict(os.environ, {"PLEXCACHE_PORT": closed, "PLEXCACHE_OPTIMIZER_PORT": closed}):
            self.assertEqual(healthcheck.main(), 1)

    def test_service_folders_and_failures(self):
        """The optimizer service's folder steps, its answers to bad requests, how each failure is
        reported (never stuck busy), and how it starts."""
        from http.server import ThreadingHTTPServer
        from plexcache.optimize import optimizer as opt
        from plexcache.optimize import server as osrv
        from plexcache.optimize import tools
        root = tempfile.mkdtemp(dir=TMP)
        src = os.path.join(root, "Movies")
        os.makedirs(os.path.join(src, "Film (2000)"))
        with open(os.path.join(src, "Film (2000)", "film.mkv"), "wb") as fh:
            fh.write(b"x" * 5000)
        os.makedirs(src + "-new")                                                  # the new dataset, made in TrueNAS
        svc = osrv.Service([root], "a-long-test-token-123456")
        act = lambda **body: osrv.folder_action(svc, body)
        self.assertEqual(act(action="probe", path="/etc")[0], 403)
        self.assertEqual(act(action="copy", path=src, to="/etc")[0], 403)
        self.assertEqual(act(action="probe", path=src)[0], 200)
        # the copy, in the background: busy meanwhile; paused, resumed, then done
        real, held = osrv.migrate.copy_tree, threading.Event()

        def slow(*a, **kw):
            held.wait(5)
            return real(*a, **kw)
        with mock.patch.object(osrv.migrate, "copy_tree", slow):
            self.assertEqual(act(action="copy", path=src, to=src + "-new")[1], {"started": True})
            self.assertEqual(svc.start(src)["error"], "busy")
            self.assertEqual(svc.start_copy(src, src + "-x", False)["error"], "busy")
            self.assertEqual(act(action="pause"), (200, {"ok": True}))
            self.assertTrue(svc.status()["paused"])
            self.assertEqual(act(action="resume"), (200, {"ok": True}))
            held.set()
            for _ in range(100):
                if not svc.current:
                    break
                time.sleep(0.05)
        self.assertEqual(svc.results[-1]["result"], "copied", svc.results[-1])
        self.assertTrue(act(action="compare", path=src, to=src + "-new")[1]["same"])
        # stopped mid-way, and a copy that fails, are reported — and the service is free again
        with mock.patch.object(osrv.migrate, "copy_tree", mock.Mock(side_effect=InterruptedError)):
            svc.start_copy(src, src + "-new", True)
            for _ in range(100):
                if not svc.current:
                    break
                time.sleep(0.02)
        self.assertEqual(svc.results[-1]["result"], "stopped")
        with mock.patch.object(osrv.migrate, "copy_tree", mock.Mock(side_effect=RuntimeError("disk full"))):
            svc.start_copy(src, src + "-new", True)
            for _ in range(100):
                if not svc.current:
                    break
                time.sleep(0.02)
        self.assertEqual((svc.results[-1]["result"], svc.results[-1]["reason"]), ("failed", "disk full"))
        self.assertEqual(act(action="stop"), (200, {"ok": True}))
        # renaming, deleting, cleaning up a work folder (only those)
        self.assertEqual(act(action="rename", path=src, name="Other")[0], 400)  # only the wizard's own names
        moved = act(action="rename", path=src + "-new", name=".Movies-new.plexcache-old")[1]["path"]
        self.assertTrue(os.path.isdir(moved))
        self.assertEqual(act(action="delete", path=src)[0], 400)
        self.assertEqual(act(action="delete", path=moved), (200, {"deleted": True}))
        self.assertEqual(act(action="cleanup", path=src)[0], 400)
        work = os.path.join(src, ".plexcache-optimize-1-0123456789")
        os.makedirs(work)
        self.assertEqual(act(action="cleanup", path=work), (200, {"deleted": True}))
        self.assertEqual(act(action="nope", path=src)[0], 404)
        # each kind of failure, reported as a result
        p = os.path.join(src, "Film (2000)", "film.mkv")
        for exc, want in ((opt.Skipped("in use"), ("skipped", "in use")), (tools.VerifyError("differs"), ("failed", "differs")),
                          (OSError("gone"), ("failed", "gone")), (RuntimeError("boom"), ("failed", "unexpected error: boom"))):
            with self.subTest(exc=exc), mock.patch.object(osrv, "optimize_file", mock.Mock(side_effect=exc)):
                svc._work(p, "compress", False, None)
                self.assertEqual((svc.results[-1]["result"], svc.results[-1]["reason"]), want)
        self.assertEqual(svc.results[-3]["verified"], "failed")                    # (the check that failed)
        with mock.patch.object(osrv, "resolve_conflict", mock.Mock(return_value={"result": "deleted"})):
            svc._work(p, "compress", False, p, action="conflict")
        self.assertEqual(svc.results[-1]["result"], "deleted")
        # the token: why there's none
        self.assertIsNone(svc.token_problem())
        self.assertEqual(osrv.Service([root], token_file=os.path.join(root, "nope")).token_problem(), "missing")
        empty = os.path.join(root, "token")
        open(empty, "w").close()
        self.assertEqual(osrv.Service([root], token_file=empty).token_problem(), "empty")
        # over HTTP: unknown paths, bad and oversized requests, conflicts
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), osrv.make_handler(svc))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def call(path, body=None, headers=None):
            req = urllib.request.Request(base + path, data=body, headers=dict({"X-Plexcache-Token": "a-long-test-token-123456"}, **(headers or {})))
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read())
        self.assertEqual(call("/nope")[0], 404)
        self.assertEqual(call("/api/nope", json.dumps({"path": p}).encode())[0], 404)
        self.assertEqual(call("/api/optimize", b"not json")[0], 400)
        self.assertEqual(call("/api/optimize", json.dumps({"path": p, "images": "weird"}).encode())[0], 400)
        self.assertEqual(call("/api/optimize", b"{}", {"Content-Length": str(osrv.MAX_BODY + 1)})[0], 413)
        self.assertEqual(call("/api/folder", json.dumps({"action": "probe", "path": src}).encode())[0], 200)
        self.assertEqual(call("/api/conflict", json.dumps({"path": p, "original": "/etc/hosts"}).encode())[0], 403)
        with mock.patch.object(svc, "start", lambda *a, **kw: {"started": True}):
            self.assertEqual(call("/api/conflict", json.dumps({"path": p, "original": p}).encode()), (200, {"started": True}))
        # starting: a token that's too short; otherwise it serves (here: not for real)
        with mock.patch.dict(os.environ, {"PLEXCACHE_OPTIMIZER_TOKEN": "short"}), mock.patch("builtins.print"):
            self.assertEqual(osrv.serve(), 2)
        served = []
        fake = mock.Mock(side_effect=lambda addr, handler: mock.Mock(serve_forever=lambda: served.append(addr)))
        with mock.patch.dict(os.environ, {"PLEXCACHE_OPTIMIZER_TOKEN": "", "PLEXCACHE_MEDIA_ROOTS": "",
                                          "PLEXCACHE_OPTIMIZER_TOKEN_FILE": os.path.join(root, "nope"), "PLEXCACHE_OPTIMIZER_PORT": "9999"}), \
                mock.patch.object(osrv, "ThreadingHTTPServer", fake), \
                mock.patch("plexcache.zfs.writable_mounts", lambda exclude=(): []), \
                mock.patch("builtins.print"):
            self.assertEqual(osrv.serve(), 0)
        self.assertEqual(served, [("0.0.0.0", 9999)])

    def test_service(self):
        from http.server import ThreadingHTTPServer
        from plexcache.optimize import server as osrv
        d = tempfile.mkdtemp(dir=TMP)
        good = os.path.join(d, "a.mkv")
        from plexcache.optimize import VERSION
        build_mkv(good, pgs_algo=0, ours={"PLEXCACHE_OPTIMIZER": VERSION})   # optimized already
        svc = osrv.Service([d], "a-long-test-token-123456")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), osrv.make_handler(svc))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def call(path, body=None, token="a-long-test-token-123456"):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"X-Plexcache-Token": token})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        code, hz = call("/healthz", token="")                                   # no token needed, and none shown
        self.assertEqual((code, hz["ok"], hz["service"], hz["token"], hz["roots"]), (200, True, "optimizer", True, 1))
        self.assertNotIn("a-long-test-token-123456", json.dumps(hz))
        self.assertEqual(call("/api/status", token="wrong")[0], 401)
        self.assertEqual(call("/api/check", {"path": "/etc/passwd"})[0], 403)
        self.assertEqual(call("/api/check", {"path": d + "/../a.mkv"})[0], 403)           # no escaping the media folders
        self.assertEqual(call("/api/check", {"path": good})[1]["optimized"], True)
        code, r = call("/api/optimize", {"path": good})
        self.assertEqual((code, r["started"]), (200, True))
        for _ in range(50):
            st = call("/api/status")[1]
            if st["results"]:
                break
            time.sleep(0.1)
        self.assertEqual(st["results"][-1]["result"], "already")
        self.assertIsNone(st["current"])
        # any file can be written again (record size, compression), still only under the media folders
        other = os.path.join(d, "notes.txt")
        with open(other, "w") as fh:
            fh.write("hello")
        self.assertEqual(call("/api/rewrite", {"path": "/etc/hosts"})[0], 403)
        self.assertEqual(call("/api/rewrite", {"path": other})[1], {"started": True})
        for _ in range(50):
            st = call("/api/status")[1]
            if st["results"][-1]["path"] == other:
                break
            time.sleep(0.1)
        self.assertEqual((st["results"][-1]["result"], st["results"][-1]["action"]), ("rewritten", "rewrite"))
        # claims: one file per system in the library folder; the earliest wins; others are seen
        code, r = call("/api/claim", {"dir": d, "key": "abcd1234", "instance": "system-a"})
        self.assertEqual((code, [c["instance"] for c in r["claims"]], r["peers"]), (200, ["system-a"], []))
        r = call("/api/claim", {"dir": d, "key": "abcd1234", "instance": "system-b"})[1]
        self.assertEqual(([c["instance"] for c in r["claims"]], r["peers"]), (["system-a", "system-b"], ["system-a"]))
        self.assertTrue(os.path.exists(os.path.join(d, ".plexcache", "claims", "abcd1234.system-a.json")))
        r = call("/api/claim", {"dir": d, "key": "abcd1234", "instance": "system-a", "release": True})[1]
        self.assertEqual([c["instance"] for c in r["claims"]], ["system-b"])
        self.assertEqual(call("/api/claim", {"dir": "/etc", "key": "abcd1234", "instance": "system-a"})[0], 403)
        self.assertEqual(call("/api/claim", {"dir": d, "key": "../x", "instance": "system-a"})[0], 400)

    def _app(self, names=("ok.mkv", "todo.mkv", "a.mp4"), comp="zstd"):
        from types import SimpleNamespace
        from plexcache.optimizing import Optimizing
        app = _new_app()
        app.scan_library()
        d = tempfile.mkdtemp(dir=TMP)
        files = []
        for n in names:
            p = os.path.join(d, n)
            if n.endswith(".mkv"):
                build_mkv(p, pgs_algo=0 if n != "todo.mkv" else "none")
            else:
                with open(p, "wb") as fh:
                    fh.write(b"x" * 100)
            os.utime(p, (time.time() - 3600, time.time() - 3600))
            files.append(SimpleNamespace(path=p, library_key="1", kind="movie", size=1, kbps=1, library="Movies"))
        app.files = files
        state = {"comp": comp}
        app.library_records = lambda: {"1": {"rs": None, "dataset": "tank/movies", "compression": state["comp"]}}
        app.map_path = lambda p: p
        app.libraries = [{"key": "1", "title": "Movies", "type": "movie", "kind": "movie", "locations": [d]}]
        client = FakeOptimizer()
        app.optimizing = Optimizing(app, client)
        return app, app.optimizing, client, d, state

    def test_app_survey_checks_and_job(self):
        from plexcache import checks
        from plexcache.optimize import VERSION
        app, op, client, d, state = self._app()
        lib = lambda: op.snapshot()["libraries"]["1"]
        # The defaults: TrueNAS compression & record size on, MKV off, Plex's cover art for unmatched items only
        self.assertEqual(op.options("1"), {"sync": True, "mkv": False, "images": "unmatched", "tags": "keep", "convert": False})
        app.libraries.append({"key": "2", "title": "Music", "type": "artist", "kind": "audio", "locations": []})
        app.settings.update({"optimizer": {"libraries": {"2": {"mkv": True, "images": "remove"}}}})
        self.assertEqual(op.options("2")["images"], "compress")             # music keeps its cover art, whatever's set
        app.libraries.pop()
        op.survey()
        # The record has just begun: no file's compression is known yet — every kind of file
        self.assertEqual((lib()["files"], lib()["mkv"], lib()["sync_todo"], lib()["todo"]), (3, 2, 3, 3))
        self.assertEqual(lib()["compression"], {"unknown": 3})
        job = run_job(op)["job"]
        self.assertEqual((job["total"], job["rewritten"], job["optimized"], job["failed"]), (3, 3, 0, 0))
        self.assertEqual({x["action"] for x in client.sent}, {"rewrite"})  # MKV off: written again, not remuxed, not claimed
        self.assertEqual((lib()["todo"], lib()["compression"]), (0, {"optimal": 3}))
        # the record, per file: the hash of the file left, the optimizer's version, its identity
        entry = op.record[os.path.join(d, "a.mp4")]
        with open(os.path.join(d, "a.mp4"), "rb") as fh:
            self.assertEqual(entry["sha256"], hashlib.sha256(fh.read()).hexdigest())
        self.assertEqual((entry["result"], entry["ino"]), ("rewritten", os.stat(os.path.join(d, "a.mp4")).st_ino))
        # a new file at a later check, the dataset unchanged: assumed written with its settings
        from types import SimpleNamespace
        new = os.path.join(d, "new.mkv")
        build_mkv(new, pgs_algo=0)
        os.utime(new, (time.time() - 3600, time.time() - 3600))
        app.files.append(SimpleNamespace(path=new, library_key="1", kind="movie", size=1, kbps=1, library="Movies"))
        op.survey()
        self.assertEqual((lib()["todo"], lib()["compression"]), (0, {"optimal": 3, "assumed": 1}))
        # the dataset changes to LZ4: what was written with ZSTD-3 is better already, so it stays as it is
        state["comp"] = "lz4"
        op.survey()
        self.assertEqual((lib()["todo"], lib()["compression"]), (0, {"optimal": 3, "assumed": 1}))
        # Optimize MKV files: the three MKVs aren't tagged by the optimizer yet (the MP4 stays as it is)
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        op.survey()
        self.assertEqual((lib()["mkv_done"], lib()["why"]), (0, {"new": 3}))
        client.sent.clear()
        run_job(op)
        sent = [x for x in client.sent if x["action"] == "optimize"]
        self.assertEqual([(x["images"], x["force"], x["tags"]) for x in sent], [("compress", False, "keep")] * 3)
        self.assertEqual(len([x for x in client.sent if x["action"] == "claim" and not x["release"]]), 3)  # remuxes are claimed…
        self.assertTrue(all(not v for v in client.claims.values()))                # …and released
        self.assertEqual((lib()["mkv_done"], lib()["todo"]), (3, 0))
        self.assertEqual(op.record[new]["ours"], {"PLEXCACHE_OPTIMIZER": VERSION})
        # kept across restarts (its tag isn't read again: the record says what it is)
        from plexcache.optimizing import Optimizing
        with mock.patch("plexcache.optimize.mkv.check", side_effect=AssertionError("not read again")):
            again = Optimizing(app, client)
            again.survey()
        self.assertEqual(again.snapshot()["libraries"]["1"]["mkv_done"], 3)
        # a newer optimizer *minor* version: done again; a newer patch version: not
        with mock.patch("plexcache.optimize.VERSION", "0.1.7"):
            op.survey()
            self.assertEqual(lib()["todo"], 0)
        with mock.patch("plexcache.optimize.VERSION", "0.2.0"):
            op.survey()
            self.assertEqual(lib()["why"], {"version": 3})
        # a file tagged by a newer optimizer (another system): left alone
        build_mkv(new, pgs_algo=0, ours={"PLEXCACHE_OPTIMIZER": "0.9.0"})
        os.utime(new, (time.time() - 3600, time.time() - 3600))
        op.survey()
        self.assertEqual(lib()["todo"], 0)
        # tagged, but remuxed since by something else: laid out wrong again
        build_mkv(new, tail=("Tags", "Cues"), front_atts=[FONT], ours={"PLEXCACHE_OPTIMIZER": VERSION})
        op.survey()
        self.assertEqual(lib()["why"], {"layout": 1})

    def test_zfs_settings_per_library(self):
        """Ideal, Pass or Warning for each library's dataset, before files are written with it."""
        from plexcache import checks
        app, op, client, d, state = self._app(names=("a.mkv",))
        M1 = 1024 * 1024
        cases = [("ideal", "zstd", "ideal", []), ("ideal", "zstd-12", "pass", []), ("pass", "zstd-3", "pass", []),
                 ("ideal", "lz4", "warn", ["Compression lz4: use ZSTD-3"]),
                 ("warn", "zstd", "warn", ["Record size 128K: 1M suits it best"]),
                 ("info", "zstd", "info", []), ("ideal", None, "info", [])]
        for rs_status, comp, want, issues in cases:
            with self.subTest(rs=rs_status, compression=comp), \
                    mock.patch.object(checks, "library_record", lambda app, key, rs=None, s=rs_status:
                                      {"status": s, "rs": 128 * 1024, "best": M1, "alt": 2 * M1}):
                z = op.zfs_config("1", {"compression": comp})
                self.assertEqual((z["status"], [en(x) for x in z["issues"]]), (want, issues))
        op.survey()
        self.assertIn(op.snapshot()["libraries"]["1"]["zfs"]["status"], ("ideal", "pass", "warn", "info"))

    def test_job_resumes_and_one_activity_at_a_time(self):
        from plexcache import jobs
        app, op, client, d, state = self._app(names=("a.mkv", "b.mkv"))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        # a job that was running when the app stopped: started again, counted as resumed
        app._save_json(jobs.JOB_FILE, {"kind": "optimize", "args": {"library": "1", "mode": "optimize"}, "resumes": 1})
        op.resume_job(wait=0)
        for _ in range(200):
            j = app.jobs.snapshot()
            if j and not j["running"] and not j.get("waiting"):
                break
            time.sleep(0.05)
        self.assertEqual((j["resumed"], j["resumes"], j["optimized"], j["phase"], j["checked"]), (True, 2, 2, "working", 2))
        self.assertEqual(app._load_json(jobs.JOB_FILE), {})                        # done: nothing to resume
        # one file job at a time: a second one doesn't start while one runs (it's queued instead: test_queue)
        class Long(jobs.Job):
            kind = "test"

            def run(self):
                self.stop_event.wait(5)
        long = Long(app.jobs)
        self.assertTrue(app.jobs.start(long))
        self.assertFalse(op.start_library("1"))
        app.jobs.cancel()
        for _ in range(100):
            if not app.jobs.busy():
                break
            time.sleep(0.05)
        self.assertTrue(app.jobs.snapshot()["cancelled"])

    def test_retry_a_failed_file(self):
        """Retry: after the file in hand (not queued), and its result replaces the failure in its job."""
        from plexcache import jobs, optimizing
        app, op, client, d, state = self._app(names=("a.mkv", "b.mkv"))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        client.result = "failed"
        first = run_job(op)["job"]
        self.assertEqual(first["failed"], 2)
        a, b = sorted(e["file"] for e in first["errors"])
        client.result = "optimized"
        self.assertIsNone(op.retry(first["id"], "/not/a/failed/file.mkv"))
        # a folder copy running: the retry waits for it (not in the queue)
        gate = threading.Event()

        class Long(jobs.Job):
            kind = "copy"

            def run(self):
                gate.wait(5)
        with mock.patch.object(optimizing, "RETRY_POLL", 0.05):
            self.assertTrue(app.jobs.start(Long(app.jobs)))
            self.assertEqual(op.retry(first["id"], a), "next")
            self.assertIsNone(op.retry(first["id"], a))                            # once
            time.sleep(0.3)
            self.assertEqual(([r["file"] for r in op.retrying()], app.jobs.queued()), ([a], []))
            gate.set()
            for _ in range(200):
                if not op.retrying():
                    break
                time.sleep(0.05)
        self.assertEqual(op.retrying(), [])
        # its result replaces the failure: in the job's log, counts and failures
        (old,) = [j for j in app.jobs.log.jobs() if j["job"] == first["id"]]
        self.assertEqual((old["files"], old["failed"], old["counts"]["optimized"], old["counts"]["failed"]), (2, 1, 1, 1))
        rows = app.jobs.log.files(first["id"])["files"]
        self.assertEqual([(r["file"], r["result"]) for r in rows], [(a, "optimized"), (b, "failed")])   # in its place
        last = app.jobs.snapshot()
        self.assertEqual(last["kind"], "copy")                                    # (the copy ended after)
        # with an optimizing job running, it takes the retry between its own files
        with mock.patch.object(op, "_retry_worker", lambda: None):
            op._retrying = False
            self.assertEqual(op.retry(first["id"], b), "next")
            run_job(op)
        self.assertEqual(op.retrying(), [])
        (old,) = [j for j in app.jobs.log.jobs() if j["job"] == first["id"]]
        self.assertEqual((old["failed"], old["counts"]["optimized"]), (0, 2))

    def test_client_and_one_file_edges(self):
        """The app's side of the optimizer: each call over HTTP, answers kept a few seconds, and why a
        file isn't sent (playing, gone, just written, no poster) or doesn't get done (refused, stopped)."""
        from http.server import ThreadingHTTPServer
        from plexcache import optimizing
        from plexcache.optimize import server as osrv
        from plexcache.plex import PlexError
        root = tempfile.mkdtemp(dir=TMP)
        svc = osrv.Service([root], "a-long-test-token-123456")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), osrv.make_handler(svc))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        c = optimizing.OptimizerClient(f"http://127.0.0.1:{httpd.server_address[1]}", "a-long-test-token-123456")
        self.assertEqual(c.healthz()["service"], "optimizer")
        self.assertEqual(c.status()["version"], c.status()["version"])             # (the second from what's kept)
        self.assertEqual([x["instance"] for x in c.claim(root, "abcd1234", "system-me")["claims"]], ["system-me"])
        p = os.path.join(root, "a.txt")
        with open(p, "w") as fh:
            fh.write("x")
        with mock.patch.object(svc, "start", lambda *a, **kw: {"started": True, "kw": sorted(kw)}):
            self.assertTrue(c.rewrite(p)["started"])
            self.assertEqual(c.optimize(p, "plex", False, cover=b"png")["kw"], ["tags"])
            self.assertTrue(c.conflict(p, p)["started"])
        gone = optimizing.OptimizerClient("http://127.0.0.1:9", "a-long-test-token-123456")
        self.assertIn("error", gone.healthz())
        self.assertIn("error", gone.status(fresh=True))
        self.assertEqual(optimizing.OptimizerClient("", "").status(), {"error": "not set up"})
        # one file: why it isn't sent, or isn't done
        app, op, client, d, state = self._app(names=("a.mkv",))
        f = os.path.join(d, "a.mkv")
        with mock.patch.object(app.monitor, "playing_paths", lambda: {f}):
            self.assertEqual(op._one(f, "1", {}, {}), {"result": "skipped", "reason": "playing"})
        self.assertEqual(op._one(os.path.join(d, "gone.mkv"), "1", {}, {})["result"], "failed")
        os.utime(f, None)                                                          # written a moment ago
        self.assertEqual(op._one(f, "1", {}, {})["reason"], "just written")
        os.utime(f, (time.time() - 3600, time.time() - 3600))
        with mock.patch.object(op, "_poster", mock.Mock(side_effect=PlexError("no poster"))):
            self.assertEqual(op._one(f, "1", {"cover": "plex"}, {}, "/library/metadata/1/thumb/1"), {"result": "failed", "reason": "no poster"})
        # sending: refused, an error, or stopped while it waits for a busy optimizer
        self.assertEqual(op._send(f, lambda: {"started": False, "error": "nope"}, 1), {"result": "failed", "reason": "nope"})
        self.assertEqual(op._send(f, mock.Mock(side_effect=OSError("refused")), 1), {"result": "failed", "reason": "refused"})
        stopped = threading.Event()
        stopped.set()
        with mock.patch.object(op, "_stop_event", lambda: stopped):
            self.assertEqual(op._send(f, lambda: {"started": False, "error": "busy"}, 1), {"result": "cancelled", "reason": "cancelled before it started"})
        # the system's ID in claims: made once, kept
        self.assertEqual(op.instance(), op.instance())

    def test_space_on_disk_once_settled(self):
        """A written file's space on the pool, read once ZFS has committed it: two looks in a row agree."""
        from types import SimpleNamespace
        from plexcache import optimizing
        looks = iter([900, 700, 400, 400, 999])                                   # shrinking as ZFS compresses it
        naps = []
        with mock.patch.object(optimizing.os, "stat", lambda p: SimpleNamespace(st_blocks=next(looks))):
            self.assertEqual(optimizing.settled_blocks("f", sleep=naps.append), 400 * 512)
        self.assertEqual(naps, [optimizing.DISK_SETTLE] + [optimizing.DISK_EVERY] * 3)
        # it never settles: the last look after the time limit
        clock = iter(range(0, 10 ** 6, 400))
        with mock.patch.object(optimizing.os, "stat", lambda p: SimpleNamespace(st_blocks=next(clock))):
            self.assertIsNotNone(optimizing.settled_blocks("f", sleep=lambda s: None, clock=lambda: next(clock)))
        with mock.patch.object(optimizing.os, "stat", mock.Mock(side_effect=FileNotFoundError)):
            self.assertIsNone(optimizing.settled_blocks("f", sleep=lambda s: None))     # gone meanwhile

    def test_job_engine_edges(self):
        """A job that breaks still ends; resilvers are asked about once a minute; a retry's result lands in
        the last job; a job that can't carry on after a restart is forgotten (and the queue goes on)."""
        from plexcache import jobs
        app, op, client, d, state = self._app(names=("a.mkv",))

        class Broken(jobs.Job):
            kind = "broken"
        b = Broken(app.jobs)
        self.assertTrue(app.jobs.start(b))
        for _ in range(100):
            if not app.jobs.busy():
                break
            time.sleep(0.02)
        self.assertEqual((app.jobs.snapshot()["kind"], app.jobs.snapshot()["running"]), ("broken", False))
        self.assertIn("unexpected error", json.dumps(app.activity.snapshot()["events"], default=str))
        self.assertIsNone(b.dataset())
        self.assertEqual(b.args(), {})
        b.file_done(10, "failed", error="just a reason")
        self.assertEqual(b.state["errors"][-1]["reason"], "just a reason")
        # a retry's result, in the last job's counts and failures
        app.jobs.last = {"id": "L1", "failed": 2, "optimized": 0, "errors": [{"file": "/a"}, {"file": "/b"}]}
        app.jobs.retried("L1", "/a", {"result": "optimized"})
        app.jobs.retried("L1", "/b", {"result": "failed", "reason": "again"})
        app.jobs.retried("L1", "/zzz", {"result": "optimized"})                  # not one of its failures
        self.assertEqual((app.jobs.last["failed"], app.jobs.last["optimized"], [e["reason"] for e in app.jobs.last["errors"]]), (1, 1, ["again"]))
        self.assertEqual(app._load_json(jobs.LAST_FILE)["failed"], 1)
        # resilvers: asked of TrueNAS, at most once a minute; can't ask: not held
        asked = []

        class TN:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def pool(self, name):
                asked.append(name)
                return {"scan": {"state": "SCANNING", "function": "RESILVER"}}
        app.truenas = lambda: TN()
        self.assertIsNone(app.jobs.resilvering(None))
        self.assertIn("tank is resilvering", str(app.jobs.resilvering("tank/media")))
        app.jobs.resilvering("tank/other")
        self.assertEqual(asked, ["tank"])
        app.truenas = mock.Mock(side_effect=OSError("no"))
        self.assertIsNone(app.jobs.resilvering("pool2/x"))
        # queued work removed by what it is; the same work isn't queued twice
        app.jobs.register("test", lambda args, saved: None)
        with mock.patch.object(app.jobs, "start_next", lambda: False):
            self.assertIsNotNone(app.jobs.enqueue("test", {"x": 1}))
            self.assertIsNone(app.jobs.enqueue("test", {"x": 1}))
        self.assertTrue(app.jobs.remove(kind="test", args={"x": 1}))
        # after a restart: a saved job that can't be made again is forgotten
        app._save_json(jobs.JOB_FILE, {"kind": "test", "args": {}})
        app.jobs.resume_saved(wait=0)
        for _ in range(100):
            if not app.jobs.waiting:
                break
            time.sleep(0.02)
        time.sleep(0.05)
        self.assertEqual(app._load_json(jobs.JOB_FILE), {})

    def test_waiting_to_carry_on(self):
        """After a restart, until the optimizer is ready: the job is shown waiting, and nothing else starts first."""
        from plexcache import jobs
        app, op, client, d, state = self._app(names=("a.mkv",))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        app._save_json(jobs.JOB_FILE, {"kind": "optimize", "args": {"library": "1", "mode": "optimize"}, "done": 3, "total": 9,
                                       "id": "w1", "title": "Movies", "library": "1", "mode": "optimize"})
        ready = threading.Event()
        nap = mock.patch.object(jobs.time, "sleep", lambda s: ready.wait(0.05))  # its wait for the optimizer, short
        nap.start()
        self.addCleanup(nap.stop)
        app.jobs.resume_saved(ready=ready.is_set, wait=30)
        j = app.jobs.snapshot()
        self.assertEqual((j["waiting"], j["running"], j["id"], j["done"], j["title"]), (True, False, "w1", 3, "Movies"))
        self.assertTrue(app.jobs.busy())
        self.assertEqual(op.queue("1", "covers"), "queued")                        # waits behind it
        self.assertIsNone(op.queue("1"))                                           # the same work as the waiting job
        ready.set()
        for _ in range(200):
            j = app.jobs.snapshot()
            if j and j.get("mode") == "covers" and not j.get("running") and not j.get("waiting"):
                break
            threading.Event().wait(0.05)  # (time.sleep is patched)
        self.assertEqual(j["mode"], "covers", j)                                   # the queued one ran after it
        self.assertFalse(app.jobs.busy())

    def test_queue(self):
        """Work asked for while a job runs waits its turn, kept across a restart; it can be removed."""
        from plexcache import jobs
        app, op, client, d, state = self._app(names=("a.mkv",))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        gate = threading.Event()

        class Long(jobs.Job):
            kind = "test"

            def run(self):
                gate.wait(5)
        app.jobs.register("test", lambda args, saved: Long(app.jobs))
        self.assertTrue(app.jobs.start(Long(app.jobs)))
        self.assertEqual(op.queue("1"), "queued")
        self.assertIsNone(op.queue("1"))                                           # the same work once
        self.assertEqual(op.queue("1", "covers"), "queued")
        q = app.jobs.queued()
        self.assertEqual([(x["kind"], x["args"]["mode"]) for x in q], [("optimize", "optimize"), ("optimize", "covers")])
        self.assertEqual(app._load_json(jobs.QUEUE_FILE)["items"], q)              # kept across a restart
        self.assertTrue(app.jobs.remove(q[1]["id"]))                               # taken off the queue
        self.assertFalse(app.jobs.remove(q[1]["id"]))
        self.assertEqual(len(app.jobs.queued()), 1)
        # after a restart: the queue is read back, and runs once nothing else does
        again = jobs.Jobs(app)
        self.assertEqual([x["id"] for x in again.queued()], [q[0]["id"]])
        # when the running job ends, the next starts by itself
        gate.set()
        for _ in range(200):
            j = app.jobs.snapshot()
            if j and j.get("kind") == "optimize" and not j["running"]:
                break
            time.sleep(0.05)
        self.assertEqual((j["kind"], j["library"], j["optimized"]), ("optimize", "1", 1), j)
        self.assertEqual((app.jobs.queued(), app._load_json(jobs.QUEUE_FILE)["items"]), ([], []))
        # nothing running: it starts straight away
        self.assertEqual(op.queue("1", "covers"), "started")
        for _ in range(200):
            if not app.jobs.busy():
                break
            time.sleep(0.05)

    def test_resume_where_it_was_and_pause(self):
        from plexcache import jobs
        from plexcache.optimizing import OptimizeJob
        app, op, client, d, state = self._app(names=("a.mkv", "b.mkv", "c.mkv"))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        op.survey()
        # saved as it was when the app stopped: 2 done (a and b, say), paused by hand
        app._save_json(jobs.JOB_FILE, {"kind": "optimize", "args": {"library": "1", "mode": "optimize"}, "done": 2, "optimized": 2,
                                       "done_bytes": 200, "work_seconds": 10.0, "work_bytes": 200, "paused": True})
        with mock.patch.object(OptimizeJob, "POLL", 0.02):
            op.resume_job(wait=0)
            for _ in range(100):
                j = app.jobs.snapshot()
                if j and j.get("paused_by") == "user":
                    break
                time.sleep(0.05)
            # it carries on from 2, not 0; nothing is sent while it's paused
            self.assertEqual((j["done"], j["total"], j["paused_by"], j["resumes"]), (2, 5, "user", 1))
            self.assertEqual([x for x in client.sent if x["action"] == "optimize"], [])
            # a resilver: paused for it, and resuming by hand doesn't lift it
            with mock.patch.object(app.jobs, "resilvering", lambda ds: "tank is resilvering"):
                op.set_paused(False)
                time.sleep(0.3)
                j = app.jobs.snapshot()
                self.assertEqual((j["paused_by"], j["paused_why"]), ("resilver", "tank is resilvering"))
                self.assertEqual([x for x in client.sent if x["action"] == "optimize"], [])
            for _ in range(400):
                if not app.jobs.busy():
                    break
                time.sleep(0.05)
        j = app.jobs.snapshot()
        self.assertEqual((j["done"], j["total"], j["optimized"]), (5, 5, 5))
        self.assertIsNotNone(j["eta"])                                             # the estimate, from the real rate
        self.assertEqual(app._load_json(jobs.JOB_FILE), {})

    def test_leftover_work_folders(self):
        """An optimizer stopped mid-way (a restart) leaves its work folder: a retry clears it, a job sweeps the rest."""
        from plexcache.optimize import optimizer as opt
        d = tempfile.mkdtemp(dir=TMP)
        p = os.path.join(d, "film.mkv")
        build_mkv(p)
        tag = hashlib.sha1(p.encode()).hexdigest()[:10]
        os.makedirs(os.path.join(d, f".plexcache-optimize-1-{tag}", "x"))         # the same name a container gets again
        st, folder, work = opt._prepare(p)
        self.assertEqual(sorted(n for n in os.listdir(d) if n.startswith(".plexcache-optimize")), [os.path.basename(work)])
        shutil.rmtree(work)
        # the survey finds others (for other files); a job sweeps them through the optimizer
        app, op, client, d2, state = self._app(names=("a.mkv",))
        left = os.path.join(d2, ".plexcache-optimize-1-0123456789")
        os.makedirs(left)
        op.survey()
        self.assertEqual(op.snapshot()["libraries"]["1"]["leftovers"], [left])
        calls = []
        client._call = lambda path, body, timeout=5: calls.append(body) or {"deleted": True}
        run_job(op)
        self.assertEqual(calls, [{"action": "cleanup", "path": left}])

    def test_failures_are_listed_and_kept(self):
        """A file that fails a check is left as it was, and listed — after the job, across restarts, and on Health."""
        from plexcache import checks, jobs
        app, op, client, d, state = self._app(names=("a.mkv", "b.mkv"))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        client.result = "failed"
        before = open(os.path.join(d, "a.mkv"), "rb").read()
        job = run_job(op)["job"]
        self.assertEqual((job["failed"], len(job["errors"])), (2, 2))
        self.assertEqual(job["errors"][0]["reason"], "the streams in the new file differ from the original")
        self.assertTrue(job["errors"][0]["file"].endswith(".mkv"))
        self.assertEqual(open(os.path.join(d, "a.mkv"), "rb").read(), before)        # untouched
        # kept after the job and across a restart, until dismissed
        self.assertEqual(len(jobs.Jobs(app).snapshot()["errors"]), 2)
        self.assertEqual([en(i["title"]) for i in checks._job_failures(app)], ["2 files weren’t optimized"])
        app.jobs.dismiss()
        self.assertIsNone(app.jobs.snapshot())
        self.assertEqual(checks._job_failures(app), [])

    def test_compression_estimates(self):
        """What caching keeps of a file is estimated with the compression it's stored with: its dataset's
        (ZSTD here) once the optimizer wrote it, LZ4 — TrueNAS's default — when that isn't known. A file
        laid out right already, but not written with its dataset's compression, gets an Optimized row too:
        written again as it is."""
        from plexcache import filemap, projection
        self.assertEqual([projection.family(c) for c in ("zstd", "zstd-3", "gzip-9", "lz4", "LZ4", None, "off", "zle")],
                         ["zstd", "zstd", "zstd", "lz4", "lz4", "lz4", "off", "lz4"])
        self.assertLessEqual(filemap.lz4_size(b"\0" * 100_000), 500)                                # zeros: next to nothing
        rnd = os.urandom(50_000)
        self.assertGreaterEqual(filemap.lz4_size(rnd), len(rnd))                                    # noise: no smaller
        text = (" ".join(str(i) for i in range(20_000))).encode()
        import zlib
        self.assertLess(len(zlib.compress(text, 6)), filemap.lz4_size(text))                        # between zlib and as it is
        self.assertLess(filemap.lz4_size(text), len(text))
        p = self.path("tags.mkv")
        build_mkv(p, cluster=os.urandom(3 * 1024 * 1024), tail=("Cues", "Tags"), ours={"PLEXCACHE_OPTIMIZER": "9", "NOTES": text.decode()[:200_000]})
        m = filemap.describe(p)
        m["mix"] = mix = filemap.cache_mix(p, m, 1024 * 1024)  # (as in 1M records: this folder's are 4K, too small to show compression)
        self.assertIn("lz4", mix)
        self.assertEqual(len(mix["kinds"]["tags"]), 3)                                             # as it is, ZSTD, LZ4
        lz4, off = projection.existing(mix, "lz4"), projection.existing(mix, "off")
        stored = lambda x: x["meta_disk"] + x["unused_disk"]
        self.assertLessEqual(stored(mix), stored(lz4))
        self.assertLessEqual(stored(lz4), stored(off))
        pr = projection.project(m, mix["record"], "keep", False, comp="zstd", rewrite=True, comp_now="lz4")
        self.assertTrue(pr["rewrite"])
        self.assertEqual((pr["mix"]["video"], pr["removed"]), (mix["video"], {}))                  # the same file, written again
        self.assertLess(pr["mix"]["meta_disk"], lz4["meta_disk"])                                  # ZSTD keeps less than LZ4 did
        v = projection.views(m, pr, None, "lz4")
        self.assertTrue(v["rewrite"])
        self.assertEqual(v["now"]["stream"]["total"], v["after"]["stream"]["total"])               # streamed: unchanged
        self.assertLess(v["after"]["cache"]["total"], v["now"]["cache"]["total"])

    def test_file_history(self):
        """Each file's history with the optimizer, kept for good: its original (when first seen) against
        now for the Metrics page, and in the job log each written file's mix just before and after. A
        file brought back still tagged keeps its history (Syncthing); laid out differently, it's logged
        as found optimized elsewhere; untagged, it's a new download and starts again; one first seen
        tagged has no known original and is left out (counted)."""
        import shutil
        from types import SimpleNamespace
        from plexcache import filehistory, optimizing
        from plexcache.optimize import VERSION
        app, op, client, d, state = self._app(names=("a.mkv", "b.mp4"))
        a, b = os.path.join(d, "a.mkv"), os.path.join(d, "b.mp4")
        build_mkv(a, front_atts=[("cover.jpg", "image/jpeg", b"\xff\xd8" + os.urandom(5000)), FONT])
        os.utime(a, (time.time() - 3600, time.time() - 3600))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "images": "remove"}}}})
        everything = {a, b}
        app.history_pass("1", everything)
        self.assertEqual(app.history.files, {})                                     # not inspected yet: nothing to go by
        for p in (a, b):
            app.file_maps.read(p)
        app.history_pass("1", everything)
        first = app.history.files[a]["original"]
        self.assertEqual((app.history.files[a]["origin"], app.history.files[a]["runs"], app.history_metrics()), ("seen", 0, []))
        with mock.patch.object(optimizing, "DISK_SETTLE", 0), mock.patch.object(optimizing, "DISK_EVERY", 0):
            run_job(op)
        # the job log: each written file's always-cached blocks just before and after, for its chart
        (j,) = app.jobs.log.jobs()
        row = next(f for f in app.jobs.log.files(j["job"])["files"] if f["file"] == a)
        self.assertEqual(row["mix_before"], first)
        self.assertGreater(row["mix_before"]["mix"]["meta"], row["mix_after"]["mix"]["meta"])   # the cover art's gone
        views = filehistory.file_views(row["mix_before"], row["mix_after"])
        self.assertGreater(views["before"]["stream"]["total"], views["after"]["stream"]["total"])
        e = app.history.files[a]
        self.assertEqual((e["runs"], e["original"], e["now"]), (1, first, row["mix_after"]))
        (lib,) = app.history_metrics()
        self.assertEqual((lib["key"], lib["files"], lib["unknown"]), ("1", 2, 0))
        self.assertGreater(lib["original"]["stream"]["total"], lib["now"]["stream"]["total"])
        self.assertIs(app.history_metrics(), app.history_metrics())                  # kept until the history changes
        # Library: once optimized (est.) only while something's still to optimize
        self.assertIsNone(app._health_summary("1", {})["views"]["after"])
        # brought back by Syncthing as it was (a new inode, the same file): its history carries on
        shutil.copy2(a, a + ".sync")
        os.replace(a + ".sync", a)
        app.file_maps.read(a)
        self.assertEqual(app.observe_history(a, "1"), "synced")
        self.assertEqual((app.history.files[a]["original"], app.history.files[a]["runs"]), (first, 1))
        # optimized differently on another NAS: found, logged with its before and after, the original kept
        build_mkv(a + ".new", tail=("Attachments", "Cues", "Tags"), ours={"PLEXCACHE_OPTIMIZER": VERSION, "PLEXCACHE_COVER": "plex:abc"},
                  tail_atts=[("cover.jpg", "image/jpeg", b"\xff\xd8" + os.urandom(9000))])
        os.replace(a + ".new", a)
        app.file_maps.read(a)
        self.assertEqual(app.observe_history(a, "1"), "discovered")
        found = next(x for x in app.jobs.log.jobs() if x["kind"] == "discovered")
        self.assertEqual((found["status"], found["files"], found["library"]), ("finished", 1, "1"))
        frow = app.jobs.log.files(found["job"])["files"][0]
        self.assertEqual((frow["result"], frow["mix_before"]["mix"], frow["version"]), ("discovered", row["mix_after"]["mix"], VERSION))
        self.assertIsNotNone(frow["disk_after"])
        self.assertEqual((app.history.files[a]["original"], app.history.files[a]["runs"]), (first, 2))
        # a new download in its place (not tagged): its history starts again — no longer an optimized file
        build_mkv(a + ".new")
        os.replace(a + ".new", a)
        app.file_maps.read(a)
        self.assertEqual(app.observe_history(a, "1"), "replaced")
        self.assertEqual((app.history.files[a]["runs"], app.history_metrics()[0]["files"]), (0, 1))
        # a file first seen already optimized (somewhere else): its origin isn't known, so it's left out, and counted
        c = os.path.join(d, "c.mkv")
        build_mkv(c, ours={"PLEXCACHE_OPTIMIZER": VERSION})
        app.files.append(SimpleNamespace(path=c, library_key="1", kind="movie", size=1, kbps=1, library="Movies"))
        app.file_maps.read(c)
        app.history_pass("1", everything | {c})
        self.assertEqual((app.history.files[c]["origin"], app.history_metrics()[0]["unknown"]), ("unknown", 1))
        # gone from the libraries: kept, but left out until it comes back
        app.history_pass("1", {a, c})
        self.assertTrue(app.history.files[b]["gone"])
        self.assertEqual(app.history_metrics()[0]["files"], 0)
        app.history_pass("1", {a, b, c})
        self.assertEqual(app.history_metrics()[0]["files"], 1)
        # saved, and read back after a restart
        app.history.save()
        again = filehistory.FileHistory(app.config_dir)
        self.assertEqual(again.files, json.loads(json.dumps(app.history.files)))
        # a file optimized before the history was kept: its mix then (kept with its layout) is its original
        maps = app.file_maps
        maps.files[c]["before"] = first["mix"]
        maps.dirty = True
        maps.save()
        os.remove(again.path)
        fresh = filehistory.FileHistory(app.config_dir)
        for p, entry in maps.files.items():
            fresh.seed(p, "", entry.get("before"), entry.get("map"), entry.get("id"))
        self.assertEqual((fresh.files[c]["original"]["mix"], fresh.files[c]["runs"], list(fresh.files)), (first["mix"], 1, [c]))
        self.assertEqual(fresh.observe(c, "1", maps.files[c]["map"], maps.files[c]["id"], {"PLEXCACHE_OPTIMIZER": VERSION}), None)
        self.assertEqual((fresh.files[c]["lib"], fresh.library("1")["files"]), ("1", 1))

    def test_job_log(self):
        """What each job did, file by file, kept for good, for the Processes page and its export."""
        from plexcache import joblog, jobs
        app, op, client, d, state = self._app(names=("a.mkv", "b.mp4"))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "images": "remove"}}}})
        from plexcache import optimizing
        with mock.patch.object(optimizing, "DISK_SETTLE", 0), mock.patch.object(optimizing, "DISK_EVERY", 0):
            run_job(op)
            for _ in range(200):                                                   # the space on disk, noted a little later
                if sum(e.get("type") == "disk" for e in app.jobs.log.entries()) == 2:
                    break
                time.sleep(0.05)
        log = app.jobs.log
        (j,) = log.jobs()
        # the running tally: what was written and put right, posters removed
        t = j["tally"]
        self.assertEqual((t["written"], t["compression_set"], t["posters_removed"]), (2, 2, 1), t)
        self.assertIn("disk_saved", t)
        self.assertEqual((j["kind"], j["status"], j["files"], [e["event"] for e in j["events"]]), ("optimize", "finished", 2, ["started", "finished"]))
        files = {os.path.basename(f["file"]): f for f in log.files(j["job"])["files"]}
        a, b = files["a.mkv"], files["b.mp4"]
        self.assertEqual((a["result"], a["verified"], a["layout"], a["cover"]), ("optimized", "passed", True, "1 removed"))
        self.assertEqual((a["images_before"], a["images_after"], a["poster_added"], a["version_before"]), (1, 0, False, None))
        self.assertIsNotNone(a["version"])
        self.assertIsNotNone(a["disk_after"])                                       # merged from its later "disk" entry
        self.assertNotIn("disk", [e.get("event") for e in j["events"]])
        self.assertEqual((b["result"], b["layout"]), ("rewritten", False))
        self.assertEqual((a["compression_before"], a["compression_after"]), ("unknown", "zstd"))
        self.assertIsNotNone(a["size_before"])
        self.assertIsNotNone(a["seconds"])
        # a job carried on after a restart: its resume point noted
        app._save_json(jobs.JOB_FILE, {"kind": "optimize", "args": {"library": "1", "mode": "optimize"}, "done": 7, "total": 9, "id": "abc123"})
        op.resume_job(wait=0)
        for _ in range(200):
            j = app.jobs.snapshot()
            if j and j.get("id") == "abc123" and not j["running"] and not j.get("waiting"):
                break
            time.sleep(0.05)
        log.add({"type": "file", "job": "abc123", "file": "/x/earlier.mkv", "result": "optimized"})   # (done before the restart)
        resumed = next(x for x in log.jobs() if x["job"] == "abc123")
        log.add({"type": "job", "job": "nothing", "event": "started"})
        log.add({"type": "job", "job": "nothing", "event": "cancelled"})
        self.assertNotIn("nothing", [x["job"] for x in log.jobs()])                 # ended without trying a file: not listed
        self.assertEqual(resumed["events"][0], {"event": "resumed", "at": resumed["events"][0]["at"], "done": 7, "total": 9})
        self.assertEqual(resumed["title"], "Movies")                                # the library's name, not its key
        # every file the job set out to do: the ones tried in the order done, then the rest (not tried yet)
        log.add({"type": "plan", "job": "s1", "files": ["/x/c.mkv", "/x/d.mkv", "/x/e.mkv", "/x/f.mkv"]})
        log.add({"type": "file", "job": "s1", "file": "/x/c.mkv", "result": "optimized", "size_before": 1000, "size_after": 990, "seconds": 5})
        log.add({"type": "file", "job": "s1", "file": "/x/d.mkv", "result": "failed", "reason": "no", "size_before": 4000, "seconds": 9})
        log.add({"type": "file", "job": "s1", "file": "/x/e.mkv", "result": "optimized", "size_before": 3000, "size_after": 2970, "seconds": 1})
        got = log.files("s1")
        self.assertEqual([(f["file"][3:], f["result"]) for f in got["files"]],
                         [("c.mkv", "optimized"), ("d.mkv", "failed"), ("e.mkv", "optimized"), ("f.mkv", "pending")])
        self.assertEqual((got["total"], got["failed"]), (4, 1))
        self.assertEqual(next(j for j in log.jobs() if j["job"] == "s1")["planned"], 4)
        # sorted by a column: Result puts the failed at the top; Size the most smaller first, then the biggest
        order = lambda key: [f["file"][3:4] for f in log.files("s1", sort=key)["files"]]
        self.assertEqual(order("result"), ["d", "c", "e", "f"])
        self.assertEqual(order("size"), ["e", "c", "d", "f"])                       # both 1% smaller: the bigger first
        self.assertEqual(order("time"), ["d", "c", "e", "f"])
        self.assertEqual(order("file"), ["c", "d", "e", "f"])
        self.assertEqual(order("nonsense"), ["c", "d", "e", "f"])
        log.add({"type": "file", "job": "s1", "file": "/x/f.mkv", "result": "optimized", "size_before": 10, "size_after": 9, "rs_before": 131072,
                 "disk_before": 100, "layout": True, "version_before": "0.0.9", "images_before": 2, "images_after": 0, "verified": "passed"})
        log.add({"type": "disk", "job": "s1", "file": "/x/f.mkv", "disk_after": 50})
        self.assertEqual(order("verified")[0], "f")                                 # (the failed check would be first)
        self.assertEqual(order("rs")[0], "f")
        self.assertEqual(order("compression")[0], "f")                              # the most saved on disk
        self.assertEqual(order("optimized")[0], "f")
        self.assertEqual(order("cover")[0], "f")
        filled = log.files("s1", fill=lambda rows: [r.update(label="X") for r in rows])
        self.assertEqual({f["label"] for f in filled["files"]}, {"X"})
        # the export: every entry, as CSV or JSON
        body, ctype = log.export("csv")
        self.assertEqual(ctype, "text/csv")
        self.assertIn("a.mkv", body.decode())
        self.assertEqual(body.decode().splitlines()[0].split(",")[:4], ["type", "event", "time", "job"])
        self.assertNotIn("\ndisk,", body.decode())                                  # in the file's own row
        self.assertTrue(json.loads(log.export("json")[0]))
        # a name that looks like a formula stays text in a spreadsheet
        self.assertEqual((joblog._cell("=HYPERLINK(1)"), joblog._cell("-x"), joblog._cell(-5), joblog._cell("a")), ("'=HYPERLINK(1)", "'-x", "-5", "a"))
        # kept for good: nothing's dropped, however old
        old = joblog.JobLog(tempfile.mkdtemp(dir=TMP))
        old.add({"type": "job", "job": "x", "event": "started", "at": time.time() - 3 * 365 * 86400})
        old.add({"type": "job", "job": "y", "event": "started"})
        self.assertFalse(hasattr(old, "prune"))
        self.assertEqual([e["job"] for e in old.entries()], ["x", "y"])
        # a line cut short (a crash mid-write) is skipped
        with open(old.path, "a") as fh:
            fh.write('{"type": "fil')
        self.assertEqual(len(old.entries()), 2)

    def test_optimizer_busy_or_restarted(self):
        from plexcache import optimizing
        app, op, client, d, state = self._app(names=("a.mkv",))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        calls = {"n": 0}
        real = client.optimize

        def busy_first(*a, **kw):                    # still finishing a file from before a restart
            calls["n"] += 1
            return {"started": False, "error": "busy"} if calls["n"] == 1 else real(*a, **kw)
        client.optimize = busy_first

        class Quick(threading.Event):  # this job's waits only (not every thread's): no need to sleep in a test
            def wait(self, timeout=None):
                return self.is_set()
        with mock.patch.object(op, "_stop_event", lambda: Quick()):
            job = run_job(op)["job"]
        self.assertEqual((calls["n"], job["optimized"]), (2, 1))
        # the optimizer restarts mid-file: no result ever comes — counted as failed, the job carries on
        build_mkv(os.path.join(d, "a.mkv"), pgs_algo=0)
        os.utime(os.path.join(d, "a.mkv"), (time.time() - 3600, time.time() - 3600))
        op.survey()
        client.optimize = lambda *a, **kw: {"started": True}
        client.done = None
        with mock.patch.object(optimizing.Optimizing, "IDLE_LIMIT", 0.1):
            job = run_job(op)["job"]
        self.assertEqual((job["total"], job["failed"]), (1, 1), job)
        self.assertIn("restarted", job["errors"][0]["reason"])

    def test_claims_between_systems(self):
        """Another system sharing the folder has claimed a file first: it's left to that system."""
        from plexcache import optimizing
        app, op, client, d, state = self._app(names=("a.mkv", "b.mkv"))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        op.survey()
        client.peers = ["system-b"]
        a_key = hashlib.sha1(b"a.mkv").hexdigest()[:24]
        client.claims[a_key] = {"system-b": time.time() - 100}                  # its claim came first
        with mock.patch.object(optimizing.Claims, "WAIT", 0.2):
            job = run_job(op)["job"]
        self.assertEqual((job["optimized"], job["skipped"]), (1, 1))
        self.assertEqual([x["path"] for x in client.sent if x["action"] == "optimize"], [os.path.join(d, "b.mkv")])
        self.assertEqual(list(client.claims[a_key]), ["system-b"])                 # ours released, theirs left

    def test_conflict_copies(self):
        """Syncthing conflict copies we made are found and deleted; files aren't optimized twice."""
        from types import SimpleNamespace
        app, op, client, d, state = self._app(names=("film.mkv",))
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        copy = os.path.join(d, "film.sync-conflict-20261017-101500-ABCDEF1.mkv")
        build_mkv(copy, pgs_algo=0)
        app.files.append(SimpleNamespace(path=copy, library_key="1", kind="movie", size=1, kbps=1, library="Movies"))
        op.survey()
        self.assertEqual(op.snapshot()["libraries"]["1"]["conflicts"], [{"copy": copy, "original": os.path.join(d, "film.mkv")}])
        self.assertEqual(op.snapshot()["libraries"]["1"]["files"], 1)            # a conflict copy isn't a file to optimize
        job = run_job(op)["job"]
        self.assertEqual((job["conflicts"], job["optimized"]), (1, 1))
        self.assertFalse(os.path.exists(copy))
        self.assertEqual(op.snapshot()["libraries"]["1"]["conflicts"], [])

    def test_zfs_record(self):
        from plexcache import zfslog
        saved = {}
        log = zfslog.ZfsLog(lambda: None, lambda d: saved.update(d))
        t0 = 1_000_000.0
        log.observe_datasets({"tank/m": {"rs": 1048576, "compression": "lz4"}}, now=t0)
        # the record begins: what's there can't be known
        self.assertEqual(log.status("/m/a", 11, "tank/m", now=t0), "unknown")
        # the next check, the dataset as it was: new files were written with its settings
        log.observe_datasets({"tank/m": {"rs": 1048576, "compression": "lz4"}}, now=t0 + 3600)
        self.assertEqual(log.status("/m/b", 12, "tank/m", now=t0 + 3600), "assumed")
        self.assertEqual(log.status("/m/a", 11, "tank/m", now=t0 + 3600), "unknown")    # still not known
        log.written("/m/a", 13, "tank/m", "lz4", now=t0 + 3700)                          # the optimizer wrote it
        self.assertEqual(log.status("/m/a", 13, "tank/m"), "optimal")
        # the dataset changes: written with LZ4 is now not optimal; new files right after are unknown
        log.observe_datasets({"tank/m": {"rs": 1048576, "compression": "zstd"}}, now=t0 + 7200)
        self.assertEqual([log.status(p, i, "tank/m") for p, i in (("/m/a", 13), ("/m/b", 12))], ["not_optimal", "not_optimal"])
        self.assertEqual(log.status("/m/c", 14, "tank/m", now=t0 + 7200), "unknown")
        self.assertEqual(log.datasets["tank/m"]["history"][0]["compression"], "lz4")   # the earlier settings, with their dates
        # a file replaced (another inode): as if new
        log.observe_datasets({"tank/m": {"rs": 1048576, "compression": "zstd"}}, now=t0 + 10800)
        self.assertEqual(log.status("/m/b", 99, "tank/m", now=t0 + 10800), "assumed")
        # the dataset's compression can't be read: nothing to say
        self.assertIsNone(log.status("/x/a", 1, "tank/other"))
        # ZSTD 3 to 19 are all good: a change between them rewrites nothing (the level written is still kept)
        log.observe_datasets({"tank/z": {"rs": 1048576, "compression": "zstd-19"}}, now=t0)
        log.written("/z/a", 21, "tank/z", "zstd-19")
        log.observe_datasets({"tank/z": {"rs": 1048576, "compression": "zstd"}}, now=t0 + 3600)
        self.assertEqual((log.status("/z/a", 21, "tank/z"), log.files["/z/a"]["compression"]), ("optimal", "zstd-19"))
        log.written("/z/b", 22, "tank/z", "lz4")                                         # but LZ4 is worth rewriting
        self.assertEqual(log.status("/z/b", 22, "tank/z"), "not_optimal")
        from plexcache.zfslog import grade
        self.assertEqual([grade(v) for v in ("zstd", "zstd-3", "zstd-19", "zstd-1", "zstd-2", "zstd-fast-5", "lz4", "on", "off", "gzip-9", None)],
                         ["good", "good", "good", "low", "low", "weak", "weak", "weak", "other", "other", None])
        # kept six months after they're last seen
        log.save()
        self.assertEqual(saved["started"], log.started)
        self.assertEqual(log.collect(now=t0 + 10800 + 60), 0)                              # seen lately: kept
        self.assertEqual(log.collect(now=time.time() + zfslog.KEEP + 60), 8)              # 6 files, 2 datasets
        self.assertEqual((log.files, log.datasets), ({}, {}))


class FixtureTests(unittest.TestCase):
    """The optimizer on small files with as many real-world features as practical (tests/fixtures,
    made by dev/make_fixtures.py), compared with a snapshot of what optimizing them gave. A failure
    here after updating MKVToolNix or ffmpeg means they now place or change something differently:
    look at what changed, and if it's fine, run python3 dev/make_fixtures.py --snapshot."""

    def setUp(self):
        from plexcache.optimize import tools
        bundle = "/Applications/MKVToolNix.app/Contents/MacOS"
        if os.path.isdir(bundle) and not os.environ.get("PLEXCACHE_TOOLS_PATH"):
            os.environ["PLEXCACHE_TOOLS_PATH"] = bundle
        if tools.missing():
            self.skipTest("MKVToolNix and ffmpeg aren't installed (CI runs these inside the image)")
        import importlib.util
        spec = importlib.util.spec_from_file_location("make_fixtures", os.path.join(ROOT, "dev", "make_fixtures.py"))
        self.fx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fx)

    def test_fixtures(self):
        from plexcache.optimize import mkv
        from plexcache.optimize.optimizer import optimize_file, stream_hashes
        with open(os.path.join(ROOT, "tests", "fixtures", "expected.json"), encoding="utf-8") as fh:
            expected = json.load(fh)
        d = tempfile.mkdtemp(dir=TMP)
        for name in ("feature-rich.mkv", "anime.mkv", "plain.mkv"):
            src = os.path.join(ROOT, "tests", "fixtures", name)
            before = self.fx.summary(src)
            hashes = stream_hashes(src)
            for images in ("compress", "remove"):
                with self.subTest(file=name, images=images):
                    p = os.path.join(d, f"{images}-{name}")
                    shutil.copy(src, p)
                    optimize_file(p, images)
                    got = self.fx.summary(p)
                    self.assertEqual(got, expected[f"{name} {images}"],
                                     "what optimizing gives has changed: look at it, then run python3 dev/make_fixtures.py --snapshot")
                    # nothing of the original lost: the same streams, and every track as it was
                    # (but image subtitles, which get zlib)
                    self.assertEqual(stream_hashes(p), hashes)
                    zlibbed = lambda ts: [dict(t, content_encoding_algorithms="0") if t["codec_id"] in mkv.IMAGE_SUBS else t for t in ts]
                    self.assertEqual(got["tracks"], zlibbed(before["tracks"]))
                    self.assertEqual((got["chapters"], got["title"]), (before["chapters"], before["title"]))
                    self.assertTrue(set(before["tags"]) <= set(got["tags"]))      # the file's own tags kept
                    # optimized: left as it is next time; the same bytes when done again elsewhere
                    self.assertEqual(optimize_file(p, images)["result"], "already")
                    twin = os.path.join(d, f"twin-{images}-{name}")
                    shutil.copy(src, twin)
                    optimize_file(twin, images)
                    with open(p, "rb") as a, open(twin, "rb") as b:
                        self.assertEqual(a.read(), b.read())


class MigrateTests(unittest.TestCase):
    """Turning a library folder into a dataset: the copy (optimize/migrate.py) and the steps (migrating.py)."""

    def _tree(self, root):
        os.makedirs(os.path.join(root, "Book One", "disc 1"))
        files = {"Book One/disc 1/01.m4b": os.urandom(300_000), "Book One/cover.jpg": b"jpeg" * 100,
                 "Book Two.m4b": os.urandom(150_000), ".hidden": b"x"}
        for rel, data in files.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "wb") as fh:
                fh.write(data)
        os.symlink("Book Two.m4b", os.path.join(root, "latest.m4b"))
        os.utime(os.path.join(root, "Book Two.m4b"), (1_600_000_000, 1_600_000_000))
        return files

    def test_copy_odd_cases(self):
        """A half-written file from a stopped copy isn't compared; a file that vanishes mid-scan is
        skipped; a folder that can't be listed is neither empty nor not; no ACL support means no ACL."""
        from plexcache.optimize import migrate
        d = tempfile.mkdtemp(dir=TMP)
        src, dst = os.path.join(d, "Books"), os.path.join(d, "Books-new")
        self._tree(src)
        os.mkdir(dst)
        migrate.copy_tree(src, dst, migrate.Paused())
        with open(os.path.join(dst, "Book Three.m4b" + migrate.PART), "wb") as fh:
            fh.write(b"half")
        self.assertTrue(migrate.compare(src, dst)["same"])
        real = os.lstat

        def lstat(path, *a, **kw):
            if path.endswith("Book Two.m4b"):
                raise FileNotFoundError(path)
            return real(path, *a, **kw)
        with mock.patch.object(migrate.os, "lstat", lstat):
            self.assertTrue(migrate.compare(src, dst)["same"])                  # gone from both sides as far as it knows
            got = migrate.copy_tree(src, dst, migrate.Paused())               # deleted meanwhile: skipped, not a failure
            self.assertEqual((got["result"], got["files_total"], got["files_done"]), ("copied", 3, 3))
        os.remove(os.path.join(dst, "Book Two.m4b"))
        with mock.patch.object(migrate, "_copy_file", mock.Mock(side_effect=FileNotFoundError("gone"))), \
                mock.patch.object(migrate.os.path, "lexists", lambda p: not p.endswith("Book Two.m4b")):
            self.assertEqual(migrate.copy_tree(src, dst, migrate.Paused())["copied"], 0)
        with mock.patch.object(migrate.os, "listdir", mock.Mock(side_effect=PermissionError("no access"))):
            self.assertIsNone(migrate.probe(dst)["empty"])
        self.assertFalse(migrate._has_acl(os.path.join(d, "no-such-file")))

    def test_copy(self):
        from plexcache.optimize import migrate
        d = tempfile.mkdtemp(dir=TMP)
        src, dst = os.path.join(d, "Books"), os.path.join(d, "Books-new")
        files = self._tree(src)
        os.mkdir(dst)
        seen = []
        r = migrate.copy_tree(src, dst, migrate.Paused(), lambda **c: seen.append(dict(c)))
        self.assertEqual((r["result"], r["files_total"], r["copied"], r["skipped"]), ("copied", 4, 4, 0))
        self.assertEqual(seen[-1]["bytes_done"], sum(len(v) for v in files.values()))
        self.assertTrue(migrate.compare(src, dst)["same"])
        self.assertEqual(os.readlink(os.path.join(dst, "latest.m4b")), "Book Two.m4b")          # links kept as links
        self.assertEqual(int(os.stat(os.path.join(dst, "Book Two.m4b")).st_mtime), 1_600_000_000)  # times kept
        # carried on (or run again): what's there already is skipped
        self.assertEqual(migrate.copy_tree(src, dst, migrate.Paused())["skipped"], 4)
        # stopped part-way: nothing half-written under a real name
        ctl = migrate.Paused()
        ctl.stop.set()
        os.remove(os.path.join(dst, "Book Two.m4b"))
        with self.assertRaises(InterruptedError):
            migrate.copy_tree(src, dst, ctl)
        self.assertFalse([n for _, _, fs in os.walk(dst) for n in fs if n.endswith(migrate.PART)])
        # the last pass: changes picked up, and what's gone from the folder removed from the copy
        with open(os.path.join(src, "Book Three.m4b"), "wb") as fh:
            fh.write(b"new")
        os.remove(os.path.join(src, "Book One", "cover.jpg"))
        before = migrate.compare(src, dst)
        self.assertEqual((before["same"], before["missing"]), (False, ["Book Three.m4b", "Book Two.m4b"]))
        self.assertEqual(before["extra"], ["Book One/cover.jpg"])
        migrate.copy_tree(src, dst, migrate.Paused(), mirror=True)
        self.assertTrue(migrate.compare(src, dst)["same"])
        # probe
        pr = migrate.probe(dst)
        self.assertEqual((pr["exists"], pr["is_dir"], pr["writable"], pr["is_mount"], pr["empty"]), (True, True, True, False, False))
        self.assertGreater(pr["free"], 0)
        # only the wizard's two renames, and only its own old folder deleted
        old = migrate.rename_folder(src, ".Books.plexcache-old")
        self.assertEqual(os.path.basename(old), ".Books.plexcache-old")
        for bad in (".Other.plexcache-old", "Elsewhere", "../x"):
            with self.assertRaises((ValueError, FileExistsError)):
                migrate.rename_folder(dst, bad)
        self.assertEqual(migrate.rename_folder(old, "Books"), src)               # undo
        with self.assertRaises(ValueError):
            migrate.delete_old(src)                                              # not an old folder
        migrate.delete_old(migrate.rename_folder(src, ".Books.plexcache-old"))
        self.assertFalse(os.path.exists(os.path.join(d, ".Books.plexcache-old")))
        # propagation: a mount that follows the host's (rslave) shows "master:" in its optional fields
        self.assertTrue(migrate.propagates("/data/x", [{"mountpoint": "/data", "fstype": "zfs", "source": "t", "root": "/",
                                                        "options": ["rw"], "optional": ["master:125"]}]))
        self.assertFalse(migrate.propagates("/data/x", [{"mountpoint": "/data", "fstype": "zfs", "source": "t", "root": "/",
                                                         "options": ["rw"], "optional": []}]))

    def test_copy_edges(self):
        """Stopping a copy (also while paused), links and odd files, the last pass's clean-up, and the
        wizard's guards on renaming and probing."""
        from plexcache.optimize import migrate
        d = tempfile.mkdtemp(dir=TMP)
        src, dst = os.path.join(d, "Shows"), os.path.join(d, "Shows-new")
        self._tree(src)
        os.makedirs(dst)
        os.symlink("Book Two.m4b", os.path.join(src, "link.m4b"))
        os.mkfifo(os.path.join(src, "pipe"))                                         # not media: left out
        # stopped before it starts, and stopped while paused
        ctl = migrate.Paused()
        ctl.stop.set()
        with self.assertRaises(InterruptedError):
            migrate.copy_tree(src, dst, ctl)
        ctl = migrate.Paused()
        ctl.go.clear()
        threading.Timer(0.1, ctl.stop.set).start()
        with mock.patch.object(ctl.go, "wait", lambda t=None: time.sleep(0.05) or ctl.go.is_set()):
            self.assertFalse(ctl.wait())
        # a stop inside a file: its part file is removed
        ctl = migrate.Paused()
        calls = {"n": 0}
        real_wait = ctl.wait

        def stop_soon():
            calls["n"] += 1
            if calls["n"] == 2:
                ctl.stop.set()
            return real_wait()
        ctl.wait = stop_soon
        with self.assertRaises(InterruptedError):
            migrate.copy_tree(src, dst, ctl)
        self.assertFalse([n for root, _, names in os.walk(dst) for n in names if n.endswith(migrate.PART)])
        # the whole copy; then the last pass removes what's gone from the source (and part files)
        r = migrate.copy_tree(src, dst, migrate.Paused())
        self.assertEqual(r["result"], "copied")
        self.assertTrue(os.path.islink(os.path.join(dst, "link.m4b")))
        self.assertFalse(os.path.exists(os.path.join(dst, "pipe")))
        os.makedirs(os.path.join(dst, "gone"))
        with open(os.path.join(dst, "gone", "x"), "w") as fh:
            fh.write("x")
        with open(os.path.join(dst, "y" + migrate.PART), "w") as fh:
            fh.write("y")
        r = migrate.copy_tree(src, dst, migrate.Paused(), mirror=True)
        self.assertEqual(r["removed"], 3)
        self.assertTrue(migrate.compare(src, dst)["same"])
        with self.assertRaises(OSError):
            migrate.copy_tree(src, os.path.join(d, "nope"), migrate.Paused())
        # a copy that reads back different is thrown away
        with mock.patch.object(migrate.hashlib, "sha256", side_effect=[hashlib.sha256(b"a"), hashlib.sha256(b"b")]):
            st = os.stat(os.path.join(src, "Book Two.m4b"))
            with self.assertRaises(OSError):
                migrate._copy_file(os.path.join(src, "Book Two.m4b"), os.path.join(d, "copy"), st, migrate.Paused(), lambda n: None)
        self.assertFalse(os.path.exists(os.path.join(d, "copy" + migrate.PART)))
        # owners that can't be kept are counted, not fatal
        with mock.patch.object(migrate.os, "chown", mock.Mock(side_effect=PermissionError)):
            self.assertTrue(migrate._copy_meta(os.stat(src), dst))
        # extended attributes: other errors are real
        with mock.patch.object(migrate.os, "listxattr", mock.Mock(side_effect=OSError(errno.EIO, "io")), create=True):
            with self.assertRaises(OSError):
                migrate._copy_xattrs(src, dst)
        with mock.patch.object(migrate.os, "listxattr", lambda p, **kw: ["user.x"], create=True), \
                mock.patch.object(migrate.os, "getxattr", lambda *a, **kw: b"v", create=True), \
                mock.patch.object(migrate.os, "setxattr", mock.Mock(side_effect=OSError(errno.EIO, "io")), create=True):
            with self.assertRaises(OSError):
                migrate._copy_xattrs(src, dst)
        # renames: only the wizard's, never over something, never a missing folder
        old = os.path.join(d, ".Shows.plexcache-old")
        os.makedirs(old)
        with self.assertRaises(FileExistsError):
            migrate.rename_folder(src, ".Shows.plexcache-old")
        os.rmdir(old)
        with self.assertRaises(FileNotFoundError):
            migrate.rename_folder(os.path.join(d, "Missing"), ".Missing.plexcache-old")
        # probing: a path that isn't there, a file
        self.assertEqual(migrate.probe(os.path.join(d, "nope")), {"path": os.path.join(d, "nope"), "exists": False})
        f = os.path.join(src, "Book Two.m4b")
        self.assertIsNone(migrate.probe(f)["empty"])
        with mock.patch.object(migrate.os, "statvfs", mock.Mock(side_effect=OSError)):
            self.assertIsNone(migrate.probe(src)["bsize"])
        with mock.patch("plexcache.zfs.mount_for", lambda path, ml=None: None):
            self.assertIsNone(migrate.propagates(src))

    def test_copy_job(self):
        """The copy as a file job: picked up still running after a restart, paused while something plays,
        a failure listed with the job; a copy that can't start; and the wizard's errors from the optimizer."""
        from plexcache import jobs, migrating
        app = _new_app()
        app.run_checks_async = lambda **kw: None  # (Health is told when what's next changes: not this test's)
        statuses, calls = [], []
        folder = "/data/Movies"

        class Client:
            configured = True

            def ready(self):
                return True

            def status(self, fresh=False):
                return statuses.pop(0) if len(statuses) > 1 else statuses[0]

            def _call(self, path, body, timeout=5):
                calls.append(body["action"])
                return start_answer if body["action"] == "copy" else {"ok": True}
        start_answer = {"started": True}
        app.optimizing.client = Client()
        mig = migrating.Migration(app)
        fresh = lambda: {"folder": folder, "temp_folder": folder + "-new", "child": "tank/Movies", "mirror": False, "library": "1",
                         "title": "Movies", "dataset": "tank", "step": "copy", "copying": True}

        class Fast(threading.Event):  # the job's waits between looks at the optimizer: short in a test
            def wait(self, timeout=None):
                return super().wait(0.01)
        real_init = migrating.CopyJob.__init__

        def init(job, *a, **kw):
            real_init(job, *a, **kw)
            job.stop_event = Fast()
        playing = iter([{"/data/Movies/x.mkv"}, {"/data/Movies/x.mkv"}])
        app.monitor.playing_paths = lambda: next(playing, set())
        app.truenas = mock.Mock(side_effect=OSError("not connected"))
        copying = {"action": "copy", "path": folder}
        statuses[:] = [{"current": dict(copying, bytes_done=0)}, {"current": dict(copying, bytes_done=100, files_total=2, files_done=1)},
                       {"current": dict(copying, bytes_done=300, files_total=2, files_done=1)},
                       {"current": None, "results": [{"action": "copy", "path": folder, "finished": time.time() + 5, "result": "failed",
                                                      "reason": "disk full", "bytes_total": 500, "bytes_done": 300}]}]
        with mock.patch.object(migrating.CopyJob, "__init__", init):
            mig.state = fresh()
            job = migrating.CopyJob(mig)
            self.assertTrue(app.jobs.start(job))
            for _ in range(300):
                if not app.jobs.busy():
                    break
                time.sleep(0.02)
        self.assertNotIn("copy", calls)                                             # still running: followed, not started again
        self.assertEqual(calls[:2], ["pause", "resume"])                            # while something played
        self.assertEqual((mig.state["copy"]["result"], mig.state["copying"]), ("failed", False))
        self.assertEqual(app.jobs.snapshot()["errors"][0]["reason"], "disk full")
        (logged,) = [e for e in app.jobs.log.entries() if e.get("type") == "file" and e.get("job") == job.state["id"]]
        self.assertEqual((logged["result"], logged["verified"]), ("failed", "failed"))
        # a copy the optimizer won't start
        start_answer = {"started": False, "error": "no space"}
        statuses[:] = [{"current": None, "results": []}]
        with mock.patch.object(migrating.CopyJob, "__init__", init):
            mig.state = fresh()
            app.jobs.start(migrating.CopyJob(mig))
            for _ in range(300):
                if not app.jobs.busy():
                    break
                time.sleep(0.02)
        self.assertEqual(mig.state["copy"], {"result": "failed", "reason": "no space"})
        # queued, then taken off the queue (or cancelled): back to where it was
        with mock.patch.object(app.jobs, "start_next", lambda: False):
            mig.state = dict(fresh(), step="create", copying=False)
            mig.state.update(queued=True, copying=True)
            app.jobs.enqueue("copy", {"folder": folder, "mirror": False})
            self.assertTrue(app.jobs.remove(kind="copy", args={"folder": folder, "mirror": False}))
            self.assertEqual((mig.state["queued"], mig.state["copying"], mig.state["copy"]["result"]), (False, False, "cancelled"))
            mig.state.update(queued=True)
            app.jobs.enqueue("copy", {"folder": folder, "mirror": False})
            mig.cancel()
            self.assertEqual((mig.state, app.jobs.queued()), (None, []))
        # the optimizer's errors, as the wizard shows them
        err = lambda body: urllib.error.HTTPError("http://x", 400, "Bad", {}, io.BytesIO(body))
        for raised, want in ((err(b'{"error": "not allowed"}'), "not allowed"), (err(b"not json"), "HTTP Error 400: Bad"),
                             (urllib.error.URLError("refused"), "refused")):
            with mock.patch.object(Client, "_call", mock.Mock(side_effect=raised)):
                self.assertEqual(mig._folder("probe", path=folder), {"error": want})
        self.assertEqual(mig._props("tank/x"), "no-api")                          # TrueNAS not reachable
        app.plex = mock.Mock(side_effect=OSError("down"))
        self.assertIsNone(mig._plex_count("1"))
        # each step's guards, and the optimizer refusing a rename
        mig.state = None
        mig.copy_finished({"result": "copied"})                                    # nothing being moved: ignored
        mig.state = dict(fresh(), copying=True)
        self.assertEqual(mig.refresh()["state"]["copying"], False)                 # no job behind it: not copying
        with mock.patch.object(app.jobs, "busy", lambda: True), self.assertRaises(ValueError):
            mig.rename_old()
        mig.state = dict(fresh(), step="switch", copying=False)
        with self.assertRaises(ValueError):
            mig.undo_rename()                                                      # nothing to undo
        mig.state["compare"] = {"same": True}
        with mock.patch.object(Client, "_call", lambda self, path, body, timeout=5: {"error": "not allowed"}):
            with self.assertRaises(ValueError):
                mig.rename_old()
            mig.state["old_path"] = folder + "-old"
            with self.assertRaises(ValueError):
                mig.undo_rename()
        with self.assertRaises(ValueError):
            mig.start_copy()                                                       # renamed already
        with self.assertRaises(ValueError):
            mig.cancel()                                                           # undo the rename first

    def test_steps(self):
        """The wizard: nothing is copied until the dataset's record size and compression are right."""
        from types import SimpleNamespace
        from plexcache import checks, migrating
        from plexcache.optimize import migrate
        d = tempfile.mkdtemp(dir=TMP)
        folder = os.path.join(d, "Books")
        self._tree(folder)
        app = _new_app()
        app.libraries = [{"key": "9", "title": "Books", "kind": "audio", "type": "artist", "locations": [folder]}]
        app.files = [SimpleNamespace(path=os.path.join(folder, "Book Two.m4b"), library_key="9", size=150_000, kind="audio")]
        app.map_path = lambda p: p
        mounted = {d}
        ml = [{"mountpoint": d, "fstype": "zfs", "source": "tank/media", "root": "/", "options": ["rw"], "optional": ["master:1"]}]
        props, count = {}, {"n": 12}
        K = 1024

        class TN:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def dataset_cache_props(self, name):
                return props.get(name)

            def call(self, method, params=None):
                calls.append((method, params))
        calls, opt = [], {"prop": True, "since": 0}
        app.truenas = lambda: TN()
        app.plex = lambda timeout=None: SimpleNamespace(library_count=lambda key: count["n"])
        app.run_checks_async = lambda **kw: None  # (Health is told when what's next changes: not this test's)

        class Client:
            configured = True
            results = []

            def ready(self):
                return True

            def status(self, fresh=False):
                return {"current": None, "results": self.results, "paused": False, "propagation": {d: opt["prop"]}, "since": opt["since"]}

            def _call(self, path, body, timeout=5):
                a = body["action"]
                if a == "probe":
                    pr = migrate.probe(body["path"])
                    pr["is_mount"] = body["path"] in mounted
                    return pr
                if a == "copy":
                    r = migrate.copy_tree(body["path"], body["to"], migrate.Paused(), mirror=body["mirror"])
                    self.results.append(dict(r, action="copy", path=body["path"], finished=time.time() + 1))
                    return {"started": True}
                if a == "compare":
                    return migrate.compare(body["path"], body["to"])
                if a == "rename":
                    return {"path": migrate.rename_folder(body["path"], body["name"])}
                if a == "delete":
                    migrate.delete_old(body["path"])
                    return {"deleted": True}
                return {"ok": True}
        app.optimizing.client = Client()
        record = lambda app, key, rs=None: {"status": "warn", "rs": 1024 * K, "best": 512 * K, "alt": 128 * K}
        with mock.patch.object(zfs, "mounts", lambda: ml), mock.patch.object(checks, "library_record", record):
            mig = migrating.Migration(app)
            (c,) = mig.candidates()
            self.assertEqual((c["child"], c["temp"], c["rs"], c["suggested"]), ("tank/media/Books", "tank/media/Books-new", 512 * K, True))
            snap = mig.plan("9", folder)
            self.assertEqual(snap["state"]["step"], "create")
            failing = lambda: [x["id"] for x in mig.refresh()["checks"] if not x["ok"]]
            self.assertEqual(failing(), ["dataset"])                                   # not created yet
            app.migration = mig
            (h,) = checks._migration_items(app)                                         # Health says it waits on you
            self.assertEqual((h["status"], en(h["title"])), ("warn", "Action needed to finish converting Books to a child dataset"))
            self.assertIn("The new dataset tank/media/Books-new is there", en(h["detail"]))
            with self.assertRaises(ValueError):
                mig.start_copy()
            # created with the wrong record size, then the wrong compression: still not copied
            props["tank/media/Books-new"] = {"recordsize": 1024 * K, "compression": "lz4"}
            os.mkdir(folder + "-new")
            mounted.add(folder + "-new")
            self.assertEqual(failing(), ["recordsize", "compression"])
            props["tank/media/Books-new"] = {"recordsize": 512 * K, "compression": "lz4"}
            self.assertEqual(failing(), ["compression"])
            with self.assertRaises(ValueError):
                mig.start_copy()
            props["tank/media/Books-new"]["compression"] = "zstd"
            self.assertEqual(failing(), [])
            # an optimizer without propagation, started before the dataset: it can't see it until it's
            # restarted — which is done from here, through TrueNAS — and isn't asked for again meanwhile
            opt["prop"] = False
            mounted.discard(folder + "-new")
            (c,) = [x for x in mig.refresh()["checks"] if not x["ok"]]
            self.assertEqual((c["id"], c["action"]), ("sees", "restart"))
            self.assertIn("propagation: rslave", c["cmd"])
            with mock.patch.object(app.optimizing, "_containers", lambda: {"app": "plex-cache"}):
                mig.restart()
            self.assertEqual(calls, [("app.redeploy", ["plex-cache"])])
            c = next(x for x in mig.refresh()["checks"] if x["id"] == "sees")
            self.assertEqual(c["action"], "")
            self.assertIn("Restarting Plex Cache", en(c["detail"]))
            opt["since"] = time.time() + 1                                             # back, and it sees it
            mounted.add(folder + "-new")
            self.assertEqual(failing(), [])
            mounted.discard(folder + "-new")                                           # started after it, yet not seen
            self.assertIn("has to be inside the folder", en(next(x for x in mig.refresh()["checks"] if x["id"] == "sees")["detail"]))
            mounted.add(folder + "-new")
            with mock.patch.object(app.optimizing, "_containers", lambda: None):
                with self.assertRaises(ValueError):
                    mig.restart()                                                      # no TrueNAS API: said, not done
            # the copy: a file job, like optimizing
            def wait_job():
                for _ in range(200):
                    if not app.jobs.busy():
                        return app.jobs.snapshot()
                    time.sleep(0.05)
            def settled(cond):
                for _ in range(300):
                    if cond() and not app.jobs.busy() and not mig.state.get("copying"):
                        return
                    time.sleep(0.05)
                raise AssertionError("the copy didn't settle")
            mig.start_copy()
            # the copy, then the last pass by itself: both compared, and when
            settled(lambda: mig.state.get("compare"))
            st = mig.refresh()["state"]
            self.assertEqual((st["step"], st["compare"]["same"], st["last_pass"]["copied"]), ("switch", True, 0))
            self.assertLess(time.time() - st["compare_at"], 60)
            self.assertEqual(st["plex_before"], 12)
            # a file added meanwhile: the pass again copies it, compares both, then moves the old folder by itself
            with open(os.path.join(folder, "Late.m4b"), "wb") as fh:
                fh.write(b"late")
            mig.pass_then_move()
            settled(lambda: mig.state.get("old_path"))
            st = mig.state
            self.assertEqual((st["compare"]["same"], st["last_pass"]["copied"]), (True, 1))
            self.assertTrue(os.path.exists(os.path.join(folder + "-new", "Late.m4b")))
            self.assertTrue(os.path.isdir(os.path.join(d, ".Books.plexcache-old")))
            # set aside: other file jobs wait until the dataset is renamed (the copies don't)
            app.jobs.holds = [mig.hold]
            self.assertIn("Books", en(app.jobs.hold_reason("optimize")))
            self.assertIsNone(app.jobs.hold_reason("copy"))
            mig.undo_rename()                                                          # put back, then again
            self.assertTrue(os.path.isdir(folder))
            self.assertIsNone(app.jobs.hold_reason("optimize"))
            mig.state["compare_at"] = time.time() - 3600                               # compared a while ago: not without a last pass
            with self.assertRaises(ValueError):
                mig.rename_old()
            mig.state["compare_at"] = time.time()
            mig.rename_old()
            # you rename the dataset in TrueNAS: it's mounted at the folder's path now
            os.rename(folder + "-new", folder)
            mounted.add(folder)
            props["tank/media/Books"] = props.pop("tank/media/Books-new")
            self.assertEqual(mig.refresh()["state"]["step"], "verify")
            self.assertIsNone(app.jobs.hold_reason("optimize"))                        # renamed: the hold is lifted
            count["n"] = 11                                                            # Plex hasn't found them all yet
            self.assertEqual(failing(), ["plex"])
            with self.assertRaises(ValueError):
                mig.delete_old()
            count["n"] = 12
            self.assertEqual(failing(), [])
            self.assertEqual(mig.delete_old()["state"]["step"], "done")
            self.assertFalse(os.path.exists(os.path.join(d, ".Books.plexcache-old")))
            self.assertTrue(migrate.compare(folder, folder)["same"] and os.path.exists(os.path.join(folder, "Late.m4b")))
            # kept across restarts
            self.assertEqual(migrating.Migration(app).snapshot()["state"]["step"], "done")


class OptimizerSetupTests(unittest.TestCase):
    """Setting the optimizer up: as little to edit as possible, and Health → Plex Cache says how."""

    def test_optimizer_check(self):
        from plexcache import checks
        from plexcache.optimizing import DEFAULT_URL, OptimizerClient
        app = _new_app()
        app.scan_library()
        app.run_checks()
        # The app makes the token itself and keeps it in its config folder, for the optimizer to read
        token = app.optimizing.token()
        self.assertEqual(len(token), 48)
        self.assertEqual(app.optimizing.token(), token)                      # the same each time
        path = os.path.join(app.config_dir, "optimizer-token")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o640")         # the apps group can read it
        self.assertEqual(app.optimizing.client.token, token)
        with mock.patch.dict(os.environ, {"PLEXCACHE_OPTIMIZER_URL": ""}):
            self.assertEqual(OptimizerClient(token="x" * 20).url, DEFAULT_URL)  # the second service in the YAML
        # Nothing answers, and TrueNAS can't be asked: add it to the YAML — no token, no environment
        setup = app.optimizing.diagnose()
        self.assertFalse(setup["ready"])
        (first,) = setup["steps"]
        self.assertEqual((first["id"], first["ok"]), ("yaml", False))
        yaml = first["cmd"]
        self.assertIn("  plex-cache-optimizer:", yaml)
        self.assertNotIn("TOKEN", yaml)
        self.assertNotIn("environment", yaml)
        self.assertNotIn("container_name", yaml)                              # a test copy next to the real app can't clash
        self.assertIn('test: ["CMD", "python", "-m", "plexcache.healthcheck"]', yaml)  # its own port, whatever the image says
        self.assertIn(":/config:ro", yaml)                                      # the settings folder, read-only
        # the host path from the app's own mount: the dataset's folder under /mnt, plus where in it the mount starts
        self.assertIn(f"source: /mnt/tank/plexfiles/plexfiles\n        target: {MEDIA}\n        bind:\n          propagation: rslave", yaml)
        self.assertNotIn(f"target: {MEDIA}/TV\n", yaml)                         # TV comes with the folder it's in
        # Health only says so, and links to the Optimizer page (where the steps are)
        it = checks._optimizer_check(app)
        self.assertEqual((it["status"], en(it["title"]), it["link"]["href"]), ("info", "Optimizer: not set up", "/optimizer"))
        self.assertNotIn("fix_cmd", {k: v for k, v in it.items() if v})
        self.assertFalse(app.optimizing.snapshot()["configured"])

    def test_setup_steps(self):
        """Each step is checked in order; the first that fails says what to do."""
        from plexcache import checks
        from plexcache.optimizing import Optimizing
        app = _new_app()
        op = Optimizing(app)
        ids = lambda: [(x["id"], x["ok"]) for x in op.diagnose()["steps"]]
        me = {"id": "abc123" + "0" * 58, "service_name": "plex-cache-dev", "state": "running"}
        containers = {"app": "plex-cache-dev", "me": "plex-cache-dev", "optimizer": None}
        hz = {"error": "[Errno 8] nodename nor servname provided"}
        st = {"error": "not set up"}
        with mock.patch.dict(os.environ, {"PLEXCACHE_OPTIMIZER_URL": ""}), \
                mock.patch.object(op, "_containers", lambda: containers), \
                mock.patch.object(op.client, "healthz", lambda: hz), \
                mock.patch.object(op.client, "status", lambda fresh=False: st), \
                mock.patch.object(checks, "optimizer_code_mount", lambda: "/mnt/ssd/AppConfigs/plex-cache-dev/src/plexcache"):
            # 1. TrueNAS sees the app, without an optimizer service: paste it (with the test copy's code mount)
            self.assertEqual(ids(), [("yaml", False)])
            step = op.setup["steps"][0]
            self.assertIn("plex-cache-dev has no optimizer service", en(step["detail"]))
            self.assertIn("- /mnt/ssd/AppConfigs/plex-cache-dev/src/plexcache:/app/plexcache:ro", step["cmd"])
            # 2. there, but stopped right after starting: the test copy's code isn't mounted into it
            containers["optimizer"] = {"service_name": "plex-cache-optimizer", "state": "exited", "targets": ["/config", "/data"],
                                       "has_config": True}
            self.assertEqual(ids(), [("yaml", True), ("running", False)])
            step = op.setup["steps"][1]
            self.assertIn("exited", en(step["detail"]))
            self.assertEqual(step["cmd"], "      - /mnt/ssd/AppConfigs/plex-cache-dev/src/plexcache:/app/plexcache:ro")
            with mock.patch.object(checks, "optimizer_code_mount", lambda: None):
                self.assertIn("older than the optimizer", en(op.diagnose()["steps"][1]["fix"]))
            # …the code is mounted already: not suggested again — its logs say why
            containers["optimizer"]["targets"].append("/app/plexcache")
            step = op.diagnose()["steps"][1]
            self.assertEqual(step["cmd"], "")
            self.assertIn("View Logs", en(step["fix"]))
            # running, answering, but the image's built-in health check looks at the wrong port: TrueNAS
            # shows "starting" (then "unhealthy") and keeps the app deploying — not a mount problem
            containers["optimizer"] = {"service_name": "plex-cache-optimizer", "state": "starting", "healthcheck": False}
            hz = {"service": "optimizer", "version": 1, "token": True}
            self.assertEqual(ids(), [("yaml", True), ("running", True), ("answers", True), ("health", False)])
            self.assertIn("plexcache.healthcheck", op.setup["steps"][-1]["cmd"])
            containers["optimizer"]["healthcheck"] = True                         # its own check, not run yet
            self.assertEqual(en(op.diagnose()["steps"][-1]["title"]), "Still starting")
            hz = {"error": "timed out"}
            # 3. running, but not answering
            containers["optimizer"] = {"service_name": "optimizer", "state": "running"}
            hz = {"error": "timed out"}
            self.assertEqual(ids(), [("yaml", True), ("running", True), ("answers", False)])
            self.assertEqual(op.client.url, "http://optimizer:8643")         # a renamed service is followed
            # 4. answering, without the token: the settings folder isn't mounted
            hz = {"service": "optimizer", "version": 1, "token": False, "token_problem": "missing", "token_file": "/config/optimizer-token"}
            self.assertEqual(ids()[-1], ("token", False))
            self.assertIn(":/config:ro", op.setup["steps"][-1]["cmd"])
            hz = dict(hz, token_problem="unreadable", uid=1000)
            self.assertIn("group_add", en(op.diagnose()["steps"][-1]["fix"]))
            # 5. a token, but not this app's
            hz = dict(hz, token=True, token_problem=None)
            st = {"error": "HTTP Error 401: Unauthorized"}
            self.assertEqual(en(op.diagnose()["steps"][-1]["title"]), "The optimizer has a different token")
            # 6. tools, 7. media
            st = {"missing": ["mkvmerge"], "roots": []}
            self.assertEqual(ids()[-1], ("tools", False))
            st = {"missing": [], "roots": []}
            self.assertEqual(ids()[-1], ("media", False))
            st = {"missing": [], "roots": ["/data"]}
            self.assertEqual(ids(), [(x, True) for x in ("yaml", "running", "answers", "token", "tools", "media")])
            self.assertTrue(op.setup["ready"])
            # without TrueNAS it can't tell "not in the YAML" from "stopped": it says both
            containers = None
            hz = {"error": "Name does not resolve"}
            self.assertEqual(ids(), [("yaml", False)])
            self.assertIn("If it’s already there", en(op.setup["steps"][0]["fix"]))
        # Health: ready, or what's left, in one line
        app.optimizing = op
        with mock.patch.object(op, "diagnose", lambda: {"ready": False, "steps": [
                {"id": "yaml", "ok": True, "title": "In the app’s YAML"}, {"id": "running", "ok": False, "title": "The optimizer isn’t running"}]}):
            it = checks._optimizer_check(app)
        self.assertEqual((it["status"], en(it["title"]), en(it["detail"])), ("warn", "Optimizer: not working yet", "The optimizer isn’t running"))

    def test_setup_edge_steps(self):
        """Something else answering on the optimizer's port, a health check that keeps failing, tools
        missing from an old image; TrueNAS that can't be asked, or doesn't list this container; a token
        that can't be kept; and a background check that fails — each said plainly, never an error."""
        from plexcache import checks, zfs
        from plexcache.optimizing import Optimizing
        app = _new_app()
        op = Optimizing(app)
        last = lambda: op.diagnose()["steps"][-1]
        containers = {"app": "plex-cache", "me": "plex-cache", "optimizer": {"service_name": "plex-cache-optimizer", "state": "running"}}
        hz = {"service": "web", "version": 1}
        st = {"missing": ["mkvmerge"], "roots": []}
        with mock.patch.dict(os.environ, {"PLEXCACHE_OPTIMIZER_URL": ""}), \
                mock.patch.object(op, "_containers", lambda: containers), \
                mock.patch.object(op.client, "healthz", lambda: hz), \
                mock.patch.object(op.client, "status", lambda fresh=False: st), \
                mock.patch.object(checks, "optimizer_code_mount", lambda: None):
            self.assertEqual(en(last()["title"]), "Something else answers at " + op.client.url)
            hz = {"service": "optimizer", "version": 1, "token": True}
            containers["optimizer"] = {"service_name": "plex-cache-optimizer", "state": "unhealthy", "healthcheck": True}
            self.assertEqual(en(last()["title"]), "Its health check doesn’t pass")
            containers["optimizer"]["state"] = "running"
            self.assertEqual((last()["id"], en(last()["fix"])), ("tools", "In TrueNAS, Apps → plex-cache: update its image to the latest."))
        # TrueNAS can't be asked, or this container isn't among its apps' containers
        with mock.patch.object(zfs, "own_container_id", lambda: "f" * 64):
            with mock.patch.object(app, "truenas", mock.Mock(side_effect=TrueNASError("no key"))):
                self.assertIsNone(op._containers())
            tn = mock.MagicMock()
            tn.__enter__.return_value.apps.return_value = [{"name": "other", "active_workloads": {"container_details": [{"id": "0" * 64}]}}]
            with mock.patch.object(app, "truenas", lambda: tn):
                self.assertIsNone(op._containers())
        # a token that can't be written is made again next time
        from plexcache.optimizing import TOKEN_FILE
        os.remove(os.path.join(app.config_dir, TOKEN_FILE))
        with mock.patch.object(os, "open", mock.Mock(side_effect=PermissionError("read-only"))):
            self.assertEqual(len(Optimizing(app).client.token), 48)
        # a check in the background that fails just leaves it as it was
        op.setup = {"ready": False, "steps": [], "at": 0}
        with mock.patch.object(op, "diagnose", mock.Mock(side_effect=RuntimeError("boom"))):
            op.diagnose_soon()
            self.assertTrue(wait_for(lambda: not op._diagnosing))
        # an unknown cover art choice (from a newer version, say) falls back to the default
        app.settings._data["optimizer"]["libraries"] = {"1": {"images": "sparkle"}}
        self.assertEqual(op.options("1")["images"], "unmatched")

    def test_file_checks_that_cant_go_ahead(self):
        """A file that's gone, an optimizer that isn't ready, cover art that can't be checked (not an
        MKV, unreadable, not optimized yet, Plex's poster unavailable), and an ID that can't be kept."""
        from plexcache.optimize import VERSION
        from plexcache.optimizing import Optimizing
        app = _new_app()
        op = Optimizing(app)
        app.settings.update({"optimizer": {"libraries": {"1": {"sync": True, "mkv": True, "images": "plex"}}}})
        self.assertEqual(op.file_status("/no/such/file.mkv", "1", {}), {"applies": False, "missing": True, "mkv": True})
        with mock.patch.object(op.client, "ready", lambda: False):
            self.assertFalse(op.start_library("1"))
        d = tempfile.mkdtemp(dir=TMP)
        p = os.path.join(d, "film.mkv")
        build_mkv(p)
        self.assertIsNone(op._cover_todo(os.path.join(d, "film.mp4"), "1", "/library/metadata/1/thumb/1", True))   # not an MKV
        self.assertIsNone(op._cover_todo(os.path.join(d, "gone.mkv"), "1", "/library/metadata/1/thumb/1", True))   # can't be read
        self.assertIsNone(op._cover_todo(p, "1", "/library/metadata/1/thumb/1", True))                          # not optimized yet
        with mock.patch.object(op, "_mkv_info", lambda path, st: {"ours": {"PLEXCACHE_OPTIMIZER": VERSION}}), \
                mock.patch.object(op, "_poster", mock.Mock(side_effect=PlexError("Plex is down"))):
            self.assertIsNone(op._cover_todo(p, "1", "/library/metadata/1/thumb/1", True))
        # a season's episodes share a poster: kept for the run, but only a few at a time
        got = []
        app.plex = lambda timeout=None: types.SimpleNamespace(image=lambda path: got.append(path) or path.encode())
        for i in range(12):
            op._poster(f"/library/metadata/{i}/thumb/1")
        self.assertLessEqual(len(op._posters), 9)
        # this system's ID for claims, when the config folder can't keep it: a new one each time
        os.makedirs(os.path.join(app.config_dir, "instance-id"))
        self.assertEqual(len(op.instance()), 16)

    def test_own_container_and_its_app(self):
        from plexcache.optimizing import Optimizing
        cid = "c3ce07a94a53" + "b" * 52
        d = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(d, "self"))
        with open(os.path.join(d, "self/mountinfo"), "w") as fh:
            fh.write(f"500 22 0:60 /var/lib/docker/containers/{cid}/hostname /etc/hostname rw - zfs boot-pool/x rw\n")
        with mock.patch.object(zfs, "PROC", d):
            self.assertEqual(zfs.own_container_id(), cid)
            app = _new_app()
            op = Optimizing(app)

            class TN:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

                def apps(self, config=False):
                    yaml = {"services": {"plex-cache-optimizer": {
                        "volumes": ["/mnt/ssd/x/config:/config", "/mnt/ssd/x/src/plexcache:/app/plexcache",
                                    {"type": "bind", "source": "/mnt/tank2/plexfiles", "target": "/data"}],
                        "environment": {"PLEXCACHE_PASSWORD": "secret"}}}}
                    return [{"name": "plex", "active_workloads": {"container_details": [{"id": "f" * 64, "service_name": "plex"}]}},
                            dict({"name": "plex-cache-dev", "active_workloads": {"container_details": [
                                {"id": cid, "service_name": "plex-cache-dev", "state": "running"},
                                {"id": "e" * 64, "service_name": "plex-cache-optimizer", "state": "exited"}]}},
                                **({"config": yaml} if config else {}))]
            app.truenas = lambda: TN()
            got = op._containers()
        self.assertEqual((got["app"], got["me"], got["optimizer"]["state"]), ("plex-cache-dev", "plex-cache-dev", "exited"))
        # the optimizer's volumes and health check from the YAML — nothing else is kept from it
        self.assertEqual((got["optimizer"]["targets"], got["optimizer"]["healthcheck"]), (["/config", "/app/plexcache", "/data"], False))
        self.assertNotIn("secret", json.dumps(got))

    def test_service_reads_the_apps_token_and_its_own_mounts(self):
        from plexcache.optimize import server as osrv
        d = tempfile.mkdtemp(dir=TMP)
        token_file = os.path.join(d, "optimizer-token")
        svc = osrv.Service([d], token_file=token_file)
        self.assertEqual(svc.token, "")                                         # not made yet: refuses everything
        with open(token_file, "w") as fh:
            fh.write("a-token-made-by-the-app-1234\n")
        self.assertEqual(svc.token, "a-token-made-by-the-app-1234")             # picked up without a restart
        self.assertEqual(osrv.Service([d], "set-in-the-yaml-123456", token_file).token, "set-in-the-yaml-123456")
        # By default it may write what the YAML mounts writable: not its own filesystems, not /config
        # (media paths as a container has them: under /tmp would be the container's own scratch space)
        ml = [{"root": "/", "mountpoint": "/", "fstype": "overlay", "source": "overlay", "options": ["rw"]},
              {"root": "/", "mountpoint": "/data", "fstype": "zfs", "source": "tank/plexfiles", "options": ["rw", "noatime"]},
              {"root": "/", "mountpoint": "/config", "fstype": "zfs", "source": "tank/apps", "options": ["ro"]},
              {"root": "/", "mountpoint": "/tmp", "fstype": "tmpfs", "source": "tmpfs", "options": ["rw"]},
              {"root": "/h", "mountpoint": "/etc/hosts", "fstype": "zfs", "source": "boot", "options": ["rw"]},
              {"root": "/", "mountpoint": "/proc", "fstype": "proc", "source": "proc", "options": ["rw"]}]
        isdir = lambda p: p != "/etc/hosts"                                    # /etc/hosts is a file
        with mock.patch("os.path.isdir", isdir):
            self.assertEqual(zfs.writable_mounts(ml), ["/data"])
            ml[1]["options"] = ["ro"]
            self.assertEqual(zfs.writable_mounts(ml), [])                       # read-only media: nothing to write
        # where a container folder is on the host, for the YAML the Optimizer page shows
        self.assertEqual(zfs.host_path("/data/Movies", [dict(ml[1], root="/media")]), "/mnt/tank/plexfiles/media/Movies")
        self.assertIsNone(zfs.host_path("/tmp", ml))


class CoverArtTests(unittest.TestCase):
    """Lossless cover art compression, and the three cover art choices."""

    def tools(self):
        from plexcache.optimize import tools
        bundle = "/Applications/MKVToolNix.app/Contents/MacOS"
        if os.path.isdir(bundle) and not os.environ.get("PLEXCACHE_TOOLS_PATH"):
            os.environ["PLEXCACHE_TOOLS_PATH"] = bundle
        if not tools.find("ffmpeg"):
            self.skipTest("ffmpeg isn't installed")
        return tools

    def test_compress_images(self):
        from plexcache.optimize import images
        tools = self.tools()
        d = tempfile.mkdtemp(dir=TMP)
        for name, args in (("a.png", ["-compression_level", "0"]), ("a.jpg", ["-huffman", "default", "-q:v", "3"])):
            tools.run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=300x450", "-frames:v", "1", *args,
                      os.path.join(d, name))
        with open(os.path.join(d, "a.webp"), "wb") as fh:
            fh.write(b"RIFF\0\0\0\0WEBPVP8 ")
        self.assertEqual([images.kind(os.path.join(d, n)) for n in ("a.png", "a.jpg", "a.webp")], ["png", "jpeg", None])
        self.assertEqual(images.compress(os.path.join(d, "a.webp"), os.path.join(d, "o"))["why"], "not PNG or JPEG")
        for name, tool in (("a.png", "oxipng"), ("a.jpg", "jpegtran")):
            src, out = os.path.join(d, name), os.path.join(d, "min-" + name)
            r = images.compress(src, out)
            if not tools.find(tool):
                self.assertEqual((r["path"], r["why"]), (src, f"{tool} isn't installed"))
                continue
            self.assertEqual(r["path"], out, r)
            self.assertLess(r["after"], r["before"])
            self.assertEqual(images.pixels(out), images.pixels(src))      # lossless
            again = images.compress(out, out + ".2")                     # nothing more to gain
            self.assertEqual(again["path"], out)
        # an animated PNG is left alone (oxipng would keep only its first frame)
        apng = os.path.join(d, "anim.png")
        with open(apng, "wb") as fh:
            fh.write(images.PNG_MAGIC + b"\0\0\0\x08acTL" + b"\0" * 12 + b"IDAT")
        self.assertEqual(images.compress(apng, apng + ".o")["why"], "animated PNG")

    def test_settings(self):
        from plexcache.config import Settings, SettingsError
        st = Settings(tempfile.mkdtemp(dir=TMP))
        st.update({"optimizer": {"libraries": {"1": {"images": "keep"}, "2": {"images": "plex", "mkv": True}, "3": {"sync": False},
                                               "4": {"images": "unmatched"}}}})
        self.assertEqual(st.section("optimizer")["libraries"],
                         {"1": {"images": "compress"}, "2": {"images": "plex", "mkv": True}, "3": {"sync": False},
                          "4": {"images": "unmatched"}})
        for bad in ({"images": "sometimes"}, {"mkv": "yes"}, {"other": 1}):
            with self.assertRaises(SettingsError):
                st.update({"optimizer": {"libraries": {"1": bad}}})

    def test_plex_client_answers(self):
        """The Plex client: a library's count, posters (whole or small), and each way an answer can be wrong."""
        from plexcache.plex import PlexClient, PlexError
        c = PlexClient(PLEX.url, "good-token", timeout=5)
        with mock.patch.object(c, "_request", lambda *a, **kw: b'<MediaContainer size="0" totalSize="7"/>'):
            self.assertEqual(c.library_count("1"), 7)
        for bad in (lambda: c.image("/etc/passwd"), lambda: c.thumb("/etc/passwd")):
            with self.assertRaises(PlexError):
                bad()
        with mock.patch.object(c, "_request", lambda *a, **kw: b"x" * 100):
            self.assertEqual(len(c.thumb("/library/metadata/1/thumb/2")), 100)
            with self.assertRaises(PlexError):
                c.image("/library/metadata/1/thumb/2", limit=10)                   # too large
            with self.assertRaises(PlexError):
                c.get("/library/sections")                                         # not XML
            with self.assertRaises(PlexError):
                c.identity()                                                       # not Plex
        with mock.patch.object(c, "_request", lambda *a, **kw: b"<MediaContainer/>"), self.assertRaises(PlexError):
            c.identity()                                                           # XML, but not Plex
        with self.assertRaises(PlexError):
            c.get("/no/such/path")                                                 # Plex says 404
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        with self.assertRaises(PlexError):
            PlexClient(f"http://127.0.0.1:{port}", "t", timeout=2).get("/")         # nothing there
        with mock.patch("urllib.request.urlopen", mock.Mock(side_effect=TimeoutError("slow"))), self.assertRaises(PlexError):
            c.get("/")

    def test_title_of(self):
        """How Plex names an item, for lists: "Title (year)", "Show · S01E05", "Album · Track"."""
        import xml.etree.ElementTree as ET
        from plexcache.plex import title_of
        cases = [('<Video type="movie" title="Heat" year="1995"/>', "Heat (1995)"),
                 ('<Video type="movie" title="Heat" year="1995" editionTitle="Director\'s Cut"/>', "Heat (1995) · Director's Cut"),
                 ('<Video type="movie" title="Heat"/>', "Heat"),
                 ('<Video type="episode" title="Pilot" grandparentTitle="Show" parentIndex="1" index="5"/>', "Show · S01E05"),
                 ('<Track type="track" title="Chapter 1" parentTitle="Book"/>', "Book · Chapter 1")]
        for xml, want in cases:
            self.assertEqual(title_of(ET.fromstring(xml)), want)

    def test_posters_from_plex(self):
        import xml.etree.ElementTree as ET
        from plexcache.plex import poster_of
        self.assertEqual(poster_of(ET.fromstring('<Video type="movie" thumb="/library/metadata/5/thumb/17"/>')),
                         "/library/metadata/5/thumb/17")
        self.assertEqual(poster_of(ET.fromstring('<Video type="episode" thumb="/e" parentThumb="/s" grandparentThumb="/g"/>')), "/s")
        self.assertEqual(poster_of(ET.fromstring('<Video type="episode" thumb="/e" grandparentThumb="/g"/>')), "/g")
        # matched to an online entry, or Plex's own "local" item
        from plexcache.plex import matched_of
        self.assertTrue(matched_of(ET.fromstring('<Video guid="plex://movie/5d776b59ad5437001f79c6f8"/>')))
        self.assertTrue(matched_of(ET.fromstring('<Video guid="com.plexapp.agents.imdb://tt0048280?lang=en"/>')))
        self.assertFalse(matched_of(ET.fromstring('<Video guid="local://12345"/>')))
        self.assertFalse(matched_of(ET.fromstring('<Video guid="com.plexapp.agents.none://abc?lang=xn"/>')))
        self.assertIsNone(matched_of(ET.fromstring('<Video/>')))
        # what cover art becomes under "unmatched"
        from plexcache.optimizing import Optimizing
        self.assertEqual([Optimizing.cover_for("unmatched", p, m) for p, m in (("/p", False), ("", False), ("/p", True), ("/p", None))],
                         ["plex", "compress", "remove", "compress"])
        self.assertEqual(Optimizing.cover_for("remove", "/p", False), "remove")

    def test_cover_art_is_set_once_then_synced_on_request(self):
        from types import SimpleNamespace
        from plexcache.optimize.optimizer import COVER_PREFIX
        from plexcache.optimizing import Optimizing
        app = _new_app()
        d = tempfile.mkdtemp(dir=TMP)
        path = os.path.join(d, "film.mkv")
        build_mkv(path, pgs_algo=0, tail_atts=[COVER, FONT], tail=("Attachments", "Cues", "Tags"))
        os.utime(path, (time.time() - 3600, time.time() - 3600))
        f = SimpleNamespace(path=path, library_key="1", kind="movie", size=1, kbps=1, library="Movies",
                            poster="/library/metadata/5/thumb/100", matched=False)
        app.files = [f]
        app.library_records = lambda: {"1": {"rs": None, "dataset": "tank/movies", "compression": "lz4"}}
        app.map_path = lambda p: p
        app.libraries = [{"key": "1", "title": "Movies", "locations": [d]}]
        poster = {"data": b"\x89PNG poster one"}
        app.plex = lambda timeout=None: SimpleNamespace(image=lambda p: poster["data"])
        client = FakeOptimizer()
        op = app.optimizing = Optimizing(app, client)
        lib = lambda: op.snapshot()["libraries"]["1"]
        optimized = lambda: [x for x in client.sent if x["action"] == "optimize"]
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False}}}})
        # The default (unmatched only): Plex can't match it, so its poster goes in, set once
        run_job(op)
        self.assertEqual((optimized()[-1]["images"], optimized()[-1]["cover"]), ("plex", b"\x89PNG poster one"))
        self.assertEqual(op.record[path]["ours"]["PLEXCACHE_COVER"], COVER_PREFIX + hashlib.sha256(b"\x89PNG poster one").hexdigest())
        self.assertEqual(lib()["todo"], 0)
        # a new poster in Plex, or a new choice: optimized files aren't touched by Optimize Now…
        poster["data"] = b"\x89PNG poster two"
        f.matched = True
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False, "images": "remove"}}}})
        op.survey()
        self.assertEqual(lib()["todo"], 0)
        # …only by Sync Cover Art, on request: here, strip (the choice now)
        n = len(optimized())
        run_job_mode(op, "covers")
        self.assertEqual([(x["images"], x["force"]) for x in optimized()[n:]], [("remove", True)])
        # Compress and embed Plex cover art, poster changed: Sync Cover Art puts the new one in
        app.settings.update({"optimizer": {"libraries": {"1": {"mkv": True, "sync": False, "images": "plex"}}}})
        n = len(optimized())
        run_job_mode(op, "covers")
        self.assertEqual([(x["images"], x["cover"]) for x in optimized()[n:]], [("plex", b"\x89PNG poster two")])
        # the same poster as in the file: nothing to do
        n = len(optimized())
        run_job_mode(op, "covers")
        self.assertEqual(optimized()[n:], [])
        # what a file's cover art becomes when it's first optimized
        im = Optimizing.images_mode
        cover = {"PLEXCACHE_COVER": "x"}
        self.assertEqual([im("unmatched", "/p", False, {}), im("unmatched", "/p", False, cover), im("unmatched", "/p", True, {}),
                          im("unmatched", "/p", True, cover), im("remove", "/p", None, cover), im("compress", "", None, {}),
                          im("compress", "", None, cover), im("plex", "", None, {})],
                         ["plex", "keep", "remove", "keep", "remove", "compress", "keep", "compress"])


class HealthBranchTests(unittest.TestCase):
    """The less common Health results, one situation at a time."""

    def setUp(self):
        from plexcache import checks
        self.checks = checks
        self.app = _new_app()
        self.app.scan_library()
        self.app.run_checks()

    def ds(self, **kw):
        d = {"dataset": "tank/x", "pool": "tank", "mountpoint": "/x", "fstype": "zfs", "libraries": ["TV Shows"],
             "keys": ["2"], "off": [], "folders": ["/x"], "props": {"primarycache": "all", "secondarycache": "all",
                                                                   "recordsize": 1048576}, "unused": False}
        d.update(kw)
        return d

    def row(self, d, pools=None, tn_ok=True):
        pools = {"tank": {"name": "tank", "l2_online": 1, "l2_total": 10 ** 11, "cache_devices": [{}]}} if pools is None else pools
        return self.checks._dataset_row(self.app, d, pools, tn_ok)

    def test_dataset_rows_less_common(self):
        """A non-ZFS folder holding only unticked libraries says nothing more; cache settings TrueNAS
        didn't return are "couldn't read", with the command to look by hand."""
        r = self.row(self.ds(dataset=None, pool=None, fstype="nfs", libraries=[], keys=[], off=["Music"]))
        self.assertIsNone(r["cache"])
        r = self.row(self.ds(props={"primarycache": None, "secondarycache": "all", "recordsize": 1048576}))
        self.assertEqual(en(r["cache"]["text"]), "Couldn’t read")
        (fix,) = r["fixes"]
        self.assertEqual(en(fix["detail"]), "TrueNAS didn’t return primarycache for tank/x (TV Shows), so this wasn’t checked.")
        self.assertIn("sudo zfs get primarycache,secondarycache,recordsize tank/x", fix["fix_cmd"])

    def test_dataset_rows(self):
        r = self.row(self.ds(libraries=[], keys=[], off=["Music"]))
        self.assertEqual((r["in_use"]["text"], r["cache"]), ("Music · caching off", None))
        # caching off, but the record size is still checked: it matters to Plex either way
        r = self.row(self.ds(libraries=[], keys=[], off=["Films"], kinds=["movie"],
                             props={"primarycache": "metadata", "secondarycache": "all", "recordsize": 131072},
                             blocks={"checked": 10, "single": 0, "sizes": {"1048576": 10}}))
        # the setting itself is a separate check from whether the files use it
        self.assertEqual((r["cache"], r["l2arc"], r["file_rs"]["status"], r["file_rs"]["badge"]), (None, None, "warn", "None match"))
        self.assertEqual(r["fixes"], [])                                  # what to do is in the column's info box
        self.assertEqual(r["file_rs"]["text"], "10 of 10 files in 1M")
        r = self.row(self.ds(libraries=[], keys=[], off=["Films"], kinds=["movie"],
                             props={"primarycache": "metadata", "secondarycache": "all", "recordsize": 131072},
                             blocks={"checked": 10, "single": 0, "sizes": {"1048576": 4, "131072": 6}}))
        self.assertEqual((r["file_rs"]["badge"], r["file_rs"]["text"]), ("Some don’t match", "4 of 10 files in 1M"))
        # files that all match: nothing to fix here (whether 128K suits the library is under Libraries)
        r = self.row(self.ds(libraries=[], keys=[], off=["Films"], kinds=["movie"],
                             props={"primarycache": "metadata", "secondarycache": "all", "recordsize": 131072},
                             blocks={"checked": 10, "single": 0, "sizes": {"131072": 10}}))
        self.assertEqual((r["file_rs"]["status"], r["file_rs"]["badge"], r["fixes"]), ("pass", "All files match", []))
        # no TrueNAS API and not readable from the dataset either: not checked, never guessed
        r = self.row(self.ds(kinds=["tv"], blocks={"checked": 10, "single": 0, "sizes": {"131072": 8, "1048576": 2}}), tn_ok=False)
        self.assertEqual(r["recordsize"], {"status": "info", "text": "Not checked"})
        # libraries that need different record sizes on one dataset: split it, saying how
        with mock.patch.object(self.checks, "_shared_record_check",
                               return_value={"needs": [], "move": ["Audiobooks"], "keep": 1024 * 1024}):
            r = self.row(self.ds())
        self.assertEqual((r["recordsize"]["status"], en(r["recordsize"]["badge"]), r["recordsize"]["text"]), ("warn", "Split dataset", "1M"))
        self.assertEqual([en(x) for x in r["recordsize"]["lines"]], ["Move Audiobooks to a dataset of its own; 1M suits the rest."])
        # compression: the badge says what's used; ZSTD-3 is all that passes
        self.assertEqual((r["compression"]["status"], r["compression"]["text"]), ("info", "Not checked"))
        r = self.row(self.ds(props={"primarycache": "all", "secondarycache": "all", "recordsize": 1048576, "compression": "lz4"}))
        self.assertEqual((r["compression"]["status"], r["compression"]["badge"], en(r["compression"]["text"])), ("warn", "lz4", "Use ZSTD-3"))
        r = self.row(self.ds(props={"primarycache": "all", "secondarycache": "all", "recordsize": 1048576, "compression": "zstd"}))
        self.assertEqual((r["compression"]["status"], r["compression"]["badge"], en(r["compression"]["text"])), ("pass", "zstd", "Recommended"))
        # ZSTD 3–19 pass (the user's call); 1–2, LZ4, ZSTD-FAST and anything else warn
        from plexcache.checks import compression_cell
        self.assertEqual([compression_cell(v)["status"] for v in ("zstd-19", "zstd-6", "zstd-2", "zstd-fast-1", "lz4", "gzip", "off")],
                         ["pass", "pass", "warn", "warn", "warn", "warn", "warn"])
        self.assertEqual(en(compression_cell("zstd-19")["text"]), "Marginally smaller files than ZSTD-3, more CPU when writing")
        r = self.row(self.ds(dataset=None, fstype="nfs"))
        self.assertEqual(r["cache"]["text"], "Not ZFS (nfs)")
        self.assertIn("not on ZFS here", r["fixes"][0]["title"])
        r = self.row(self.ds(), tn_ok=False)
        self.assertEqual(r["cache"]["text"], "Not checked")
        r = self.row(self.ds(props={"primarycache": "all", "secondarycache": "metadata", "recordsize": None}))
        self.assertEqual(r["cache"]["text"], "secondarycache=metadata")
        self.assertEqual(r["fixes"][0]["fix_cmd"], "sudo zfs set secondarycache=all tank/x")
        # no L2ARC on the pool: secondarycache doesn't matter, so no warning about it
        r = self.row(self.ds(props={"primarycache": "all", "secondarycache": "metadata", "recordsize": 1048576}),
                     pools={"tank": {"name": "tank", "l2_online": 0, "l2_total": 0, "cache_devices": []}})
        self.assertEqual((r["cache"]["status"], r["cache"]["text"]), ("pass", "primarycache=all, secondarycache=metadata"))
        self.assertFalse([f for f in r["fixes"] if "secondarycache" in f.get("fix_cmd", "")])
        r = self.row(self.ds(), pools={})
        self.assertEqual(r["l2arc"]["text"], "Not checked")
        r = self.row(self.ds(), pools={"tank": {"name": "tank", "l2_online": 0, "l2_total": 0, "cache_devices": [{}]}})
        self.assertEqual(r["l2arc"]["text"], "Devices not ONLINE")

    def test_truenas_problems(self):
        z = self.app.zfs
        self.app.zfs = dict(z, truenas_error="connection refused", arc=None)
        zitems = {i["title"]: i for i in self.checks.check_zfs(self.app)["items"]}
        self.assertEqual(zitems["TrueNAS API"]["status"], "fail")
        self.assertIn("Can’t read ZFS statistics", zitems)
        ditems = {i["title"] for i in self.checks.check_datasets(self.app)["items"]}
        self.assertIn("Dataset settings: not checked", ditems)
        self.app.settings.update({"truenas": {"api_key": ""}})
        zitems = {i["title"] for i in self.checks.check_zfs(self.app)["items"]}
        # (on TrueNAS for certain — this app's probe said so through the fake API — the key is asked for; elsewhere it's an offer)
        self.assertTrue({"TrueNAS API key needed", "TrueNAS API not connected"} & zitems, zitems)
        self.app.zfs = z

    def test_plex_problems(self):
        self.app.scan_error = "Plex said no"
        self.app.monitor.last_error = "timed out"
        items = {i["title"]: i for i in self.checks.check_plex(self.app)["items"]}
        self.assertEqual(items["Couldn’t list libraries"]["status"], "fail")
        self.assertEqual(items["Now-playing feed"]["status"], "warn")
        self.app.monitor.last_error = None

    def test_ram_and_tuning_fallbacks(self):
        real = self.app.ram_budget
        self.app.ram_budget = lambda: {"cache_max": None}
        self.assertEqual(self.checks._ram_check(self.app)[0]["title"], "RAM for Now Playing Cache and Prefetch")
        self.app.ram_budget = lambda: dict(real(), source="ram")
        self.assertIn("half of RAM", self.checks._ram_check(self.app)[0]["detail"])
        self.app.ram_budget = real
        self.assertEqual(self.checks._blocks_check("t", None, None), "info")
        self.assertEqual(self.checks._l2arc_tuning_checks(self.app, {"l2arc_mfuonly": None})[0]["title"], "L2ARC settings")
        self.assertEqual((self.checks._fmt_bytes(None), self.checks._fmt_bytes(512)), ("unknown", "512 B"))

    def test_is_it_working(self):
        base = {"result": "ok", "started": 1, "l2_growth": 5e9}
        self.app.activity.runs = [dict(base), dict(base, hit_rate=0.05)]
        g = self.checks.check_effectiveness(self.app)
        self.assertEqual(g["items"][0]["title"], "Warm-up reads aren’t staying cached")
        self.app.activity.runs = [dict(base), dict(base, hit_rate=0.8)]
        self.assertEqual(self.checks.check_effectiveness(self.app)["items"][0]["status"], "pass")
        self.app.activity.runs = [dict(base), dict(base, hit_rate=None)]
        self.assertIsNone(self.checks.check_effectiveness(self.app))

    def test_warmup_skips_pools_without_l2arc(self):
        z = self.app.zfs
        tv = next(d for d in z["datasets"] if d["dataset"] == TV_DS)
        self.app.zfs = dict(z, datasets=[dict(tv, pool="slow")],
                            pools=dict(z["pools"], slow={"name": "slow", "l2_online": 0, "l2_total": 0, "cache_devices": []}))
        rec = {r["feature"]: r for r in self.checks.recommendations(self.app)}["warmup"]
        self.assertIn("no L2ARC on pool slow", rec["reason"])
        self.app.zfs = z

    def test_optimizer_lines(self):
        """Health's optimizer lines: ready (with what it can write), Syncthing conflict copies found next
        to the media, and the test copy's code mount found from the container's mounts."""
        c = self.checks
        from plexcache import zfs
        op = types.SimpleNamespace(
            diagnose=lambda: {"ready": True, "steps": [{"id": "yaml", "ok": True, "title": "In the app’s YAML"},
                                                       {"id": "media", "ok": True, "title": "Can write /data"}]},
            snapshot=lambda: {"libraries": {"1": {"conflicts": [{"copy": "/data/Film.sync-conflict-20261018-ABC.mkv"}]}}})
        app = types.SimpleNamespace(optimizing=op)
        ready = c._optimizer_check(app)
        self.assertEqual((ready["status"], en(ready["title"]), ready["detail"]), ("pass", "Optimizer ready", "Can write /data"))
        (conflict,) = c._conflict_items(app)
        self.assertEqual((en(conflict["title"]), en(conflict["detail"])),
                         ("1 Syncthing conflict copy", "Next to your media: Film.sync-conflict-20261018-ABC.mkv."))
        here = os.path.dirname(os.path.abspath(c.__file__))
        mounts = [{"mountpoint": here, "root": "/AppConfigs/plex-cache-dev/src/plexcache", "source": "ssd/apps", "fstype": "zfs",
                   "options": [], "optional": []}]
        with mock.patch.object(zfs, "mounts", lambda: mounts), mock.patch.object(zfs, "host_path", lambda p, ml: "/mnt/ssd/AppConfigs/plex-cache-dev/src/plexcache"):
            self.assertEqual(c.optimizer_code_mount(), "/mnt/ssd/AppConfigs/plex-cache-dev/src/plexcache")

    def test_summaries(self):
        c = self.checks
        names = ("check_plex", "check_media", "check_datasets", "check_cache_mode", "check_zfs", "check_effectiveness")
        saved = {n: getattr(c, n) for n in names}
        group = lambda *st: {"id": "g", "items": [c.item(x, "t") for x in st]}
        try:
            for n in names:
                setattr(c, n, lambda app: group("pass"))
            c.check_effectiveness = lambda app: group("warn")       # the "Is it working?" group is added when present
            res = c.run_all(self.app)
            self.assertEqual(len(res["groups"]), 6)
            self.assertEqual(res["summary"]["text"], "Working — the orange items below could be better.")
            self.assertEqual(res["counts"]["warn"], 1)                       # (the numbers are in the tiles)
            c.check_zfs = lambda app: group("fail", "warn")
            self.assertEqual(c.run_all(self.app)["summary"]["text"], "Something needs fixing — see the red and orange items below.")
            c.check_effectiveness = lambda app: group("pass")
            c.check_zfs = lambda app: group("fail")
            self.assertEqual(c.run_all(self.app)["summary"]["text"], "Something needs fixing — see the red items below.")
            c.check_effectiveness = lambda app: None
            c.check_zfs = lambda app: group("pass")
            self.assertEqual(c.run_all(self.app)["summary"]["text"], "Everything is OK.")
        finally:
            for n, f in saved.items():
                setattr(c, n, f)


class LoginTests(unittest.TestCase):
    """The optional web UI login (PLEXCACHE_USERNAME / PLEXCACHE_PASSWORD)."""

    @classmethod
    def setUpClass(cls):
        import plexcache.auth as authmod
        from plexcache.server import serve
        cls.authmod = authmod
        cls.saved_delay, authmod.FAIL_DELAY = authmod.FAIL_DELAY, 0
        cls.app = _new_app()
        cls.app.auth = authmod.Auth("admin", "hunter2", cls.app.config_dir)
        cls.httpd = serve(cls.app, "127.0.0.1", 0)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.authmod.FAIL_DELAY = cls.saved_delay
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, method, path, body=None, headers=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method)
        if body is not None:
            r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), e.headers

    def sign_in(self, user="admin", pw="hunter2"):
        status, _, headers = self.req("POST", "/login", {"username": user, "password": pw})
        cookie = (headers.get("Set-Cookie") or "").split(";")[0]
        return status, cookie

    def test_pages_show_sign_in_and_api_refuses(self):
        status, body, _ = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="login"', body)
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("POST", "/api/settings", {"warmup": {"pacing_percent": 50}})[0], 401)
        # Open without signing in: health check for TrueNAS, static files, the sign-in page itself
        self.assertEqual(self.req("GET", "/healthz")[0], 200)
        self.assertEqual(self.req("GET", "/static/app.css")[0], 200)
        self.assertIn(b'id="login"', self.req("GET", "/login")[1])

    def test_cookie_session(self):
        status, cookie = self.sign_in()
        self.assertEqual(status, 200)
        self.assertTrue(cookie.startswith("pc_session="))
        status, body, _ = self.req("GET", "/api/state", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["auth"], {"enabled": True, "user": "admin"})
        self.assertIn(b"app.js", self.req("GET", "/", headers={"Cookie": cookie})[1])
        # Cookie flags
        _, _, headers = self.req("POST", "/login", {"username": "admin", "password": "hunter2"})
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, headers["Set-Cookie"])
        # A tampered or expired cookie doesn't work
        exp, sid, sig = cookie.split("=", 1)[1].split(".")
        self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": f"pc_session={int(exp) + 1}.{sid}.{sig}"})[0], 401)
        self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": "pc_session=garbage"})[0], 401)
        t = int(time.time()) - 5
        self.assertFalse(self.app.auth.session_ok(f"{t}.{sid}.{self.app.auth._sign(t, sid)}"))

    def test_wrong_password_and_logout(self):
        status, cookie = self.sign_in(pw="nope")
        self.assertEqual((status, cookie), (401, ""))
        self.assertEqual(self.sign_in(user="root")[0], 401)
        events = [e["message"] for e in self.app.activity.snapshot()["events"]]
        self.assertTrue(any("failed sign-in" in m for m in events))
        status, _, headers = self.req("POST", "/logout", {})
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_sign_out_revokes_the_session(self):
        _, cookie = self.sign_in()
        self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": cookie})[0], 200)
        _, other = self.sign_in()  # another browser
        self.req("POST", "/logout", {}, headers={"Cookie": cookie})
        # A copy of the cookie no longer works, even after a restart; other sessions carry on
        self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": cookie})[0], 401)
        restarted = self.authmod.Auth("admin", "hunter2", self.app.config_dir)
        self.assertFalse(restarted.session_ok(cookie.split("=", 1)[1]))
        self.assertTrue(restarted.session_ok(other.split("=", 1)[1]))
        self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": other})[0], 200)

    def test_failed_attempts_queue_up(self):
        self.authmod.FAIL_DELAY = 0.3
        try:
            start = time.time()
            threads = [threading.Thread(target=self.sign_in, kwargs={"pw": "nope"}) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertGreaterEqual(time.time() - start, 1.1)  # 4 x 0.3 s, not 0.3 s in parallel
        finally:
            self.authmod.FAIL_DELAY = 0

    def test_lockout_after_wrong_passwords(self):
        """An address that keeps getting the password wrong is refused for a while, even with the right one."""
        auth = self.app.auth
        auth._fails.clear()
        for _ in range(self.authmod.LOCKOUT_FAILS):
            self.assertEqual(self.sign_in(pw="nope")[0], 401)
        status, body, _ = self.req("POST", "/login", {"username": "admin", "password": "hunter2"})
        self.assertEqual(status, 429)
        self.assertIn("15 minutes", json.loads(body)["error"]["_t"])
        # later: a fresh start; and a right password clears the count
        n, at = auth._fails["127.0.0.1"]
        auth._fails["127.0.0.1"] = (n, at - self.authmod.LOCKOUT_SECONDS - 1)
        self.assertEqual(self.sign_in()[0], 200)
        self.assertNotIn("127.0.0.1", auth._fails)
        auth.note_attempt("10.0.0.9", False)
        auth.note_attempt("10.0.0.9", True)
        self.assertNotIn("10.0.0.9", auth._fails)
        with mock.patch.object(auth, "_fails", {f"10.{i // 250}.{i % 250}.1": (1, float(i)) for i in range(10001)}):
            auth.note_attempt("10.200.0.1", False)                                     # many addresses: the oldest forgotten
            self.assertLess(len(auth._fails), 6000)

    def test_secure_cookie_over_https(self):
        _, _, headers = self.req("POST", "/login", {"username": "admin", "password": "hunter2"})
        self.assertNotIn("Secure", headers["Set-Cookie"])
        _, _, headers = self.req("POST", "/login", {"username": "admin", "password": "hunter2"}, headers={"X-Forwarded-Proto": "https"})
        self.assertIn("; Secure", headers["Set-Cookie"])

    def test_security_headers_and_host(self):
        _, body, headers = self.req("GET", "/")
        csp = headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn(b"<script>", body)  # the sign-in page has no inline script (the policy would block it)
        self.assertEqual(self.req("GET", "/static/login.js")[0], 200)
        # Before signing in, the Host header is ignored (it could steer auto-detection)
        self.app.web_url = None
        self.req("GET", "/api/state", headers={"Host": "10.9.8.7:9999"})
        self.req("GET", "/healthz", headers={"Host": "10.9.8.7:9999"})
        self.assertIsNone(self.app.web_url)
        _, cookie = self.sign_in()
        self.req("GET", "/api/state", headers={"Cookie": cookie, "Host": "10.9.8.7:9999"})
        self.assertEqual(self.app.web_url, "http://10.9.8.7:9999")
        # Bad request sizes
        r = urllib.request.Request(self.base + "/login", data=b"{}", method="POST",
                                   headers={"Content-Type": "application/json", "Content-Length": "-1"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()

    def test_basic_auth_for_scripts(self):
        import base64
        good = "Basic " + base64.b64encode(b"admin:hunter2").decode()
        bad = "Basic " + base64.b64encode(b"admin:wrong").decode()
        self.assertEqual(self.req("GET", "/api/state", headers={"Authorization": good})[0], 200)
        self.assertEqual(self.req("GET", "/api/state", headers={"Authorization": bad})[0], 401)
        self.assertEqual(self.req("GET", "/api/state", headers={"Authorization": "Basic !!"})[0], 401)

    def test_default_password_is_refused(self):
        import base64
        from plexcache.server import serve
        app = _new_app()
        app.auth = self.authmod.Auth("plex-cache", "change-me", app.config_dir)
        self.assertTrue(app.auth.default_password)
        httpd = serve(app, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            def post(user, pw, headers=None):
                r = urllib.request.Request(base + "/login", data=json.dumps({"username": user, "password": pw}).encode(),
                                           method="POST", headers={"Content-Type": "application/json", **(headers or {})})
                try:
                    with urllib.request.urlopen(r) as resp:
                        return resp.status, json.loads(resp.read()), resp.headers
                except urllib.error.HTTPError as e:
                    with e:
                        return e.code, json.loads(e.read()), e.headers
            # The right username and the example password: refused, saying what to change
            status, body, headers = post("plex-cache", "change-me")
            self.assertEqual(status, 403)
            self.assertIn("default password", en(body["error"]))
            self.assertIn("PLEXCACHE_PASSWORD", en(body["error"]))
            self.assertIsNone(headers.get("Set-Cookie"))
            self.assertTrue(any("hasn’t been changed" in e["message"] for e in app.activity.snapshot()["events"]))
            # A wrong guess still just says it doesn't match (nothing given away)
            self.assertEqual(post("plex-cache", "nope")[0], 401)
            # Basic auth and a forged-but-valid session don't get in either
            basic = "Basic " + base64.b64encode(b"plex-cache:change-me").decode()
            r = urllib.request.Request(base + "/api/state", headers={"Authorization": basic})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(r)
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()
            self.assertFalse(app.auth.session_ok(app.auth.new_session()))
        finally:
            httpd.shutdown()
            httpd.server_close()
        # A different password works as usual
        self.assertFalse(self.authmod.Auth("plex-cache", "my-own-secret", app.config_dir).default_password)

    def test_login_page_help(self):
        _, body, _ = self.req("GET", "/")
        for text in (b'id="help-btn"', b"PLEXCACHE_USERNAME", b"PLEXCACHE_PASSWORD", b"anyone on your network", b"change-me"):
            self.assertIn(text, body)

    def test_damaged_files(self):
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "session.key"), "wb") as fh:
            fh.write(b"short")  # too short to be a key: replaced
        with open(os.path.join(d, "signed-out.json"), "w") as fh:
            fh.write("not json")
        a = self.authmod.Auth("u", "p", d)
        with open(os.path.join(d, "session.key"), "rb") as fh:
            self.assertGreaterEqual(len(fh.read()), 32)
        token = a.new_session()
        self.assertTrue(a.session_ok(token))
        a.revoke("garbage")  # ignored
        # Can't save the sign-out list (read-only folder): still signed out until a restart
        a._revoked_path = os.path.join(d, "missing-folder", "signed-out.json")
        a.revoke(token)
        self.assertFalse(a.session_ok(token))
        # No config folder at all: still works, with a key that lasts until a restart
        b = self.authmod.Auth("u", "p", None)
        self.assertTrue(b.session_ok(b.new_session()))
        self.assertFalse(b.session_ok("1.2"))
        self.assertFalse(b.session_ok("x.y.z"))

    def test_key_kept_and_password_change_signs_out(self):
        a = self.authmod.Auth("admin", "hunter2", self.app.config_dir)
        token = self.app.auth.new_session()
        self.assertTrue(a.session_ok(token))  # same key from the config folder: survives a restart
        self.assertEqual(oct(os.stat(os.path.join(self.app.config_dir, "session.key")).st_mode & 0o777), "0o600")
        b = self.authmod.Auth("admin", "changed", self.app.config_dir)
        self.assertFalse(b.session_ok(token))
        self.assertFalse(self.authmod.Auth("admin", "").enabled)
        self.assertTrue(self.authmod.Auth().request_ok({}))
        old = dict(os.environ)
        try:
            os.environ.update(PLEXCACHE_USERNAME="u", PLEXCACHE_PASSWORD="p")
            self.assertTrue(self.authmod.Auth.from_env(self.app.config_dir).enabled)
        finally:
            os.environ.clear()
            os.environ.update(old)


class ValidationTests(unittest.TestCase):
    """Every setting accepts only what the web UI can send: its choices, whole numbers in its
    ranges, and well-formed text. MQTT commands go through the same checks."""

    def setUp(self):
        self.s = config.Settings(tempfile.mkdtemp(dir=TMP))

    def bad(self, patch):
        with self.assertRaises(config.SettingsError, msg=patch):
            self.s.update(patch)

    def test_library_choices_are_checked(self):
        """Per-library choices must be objects keyed by real library keys; null puts a library back
        to its defaults; and nothing outside the rules can be set at all."""
        for sec in ("warmup", "optimizer", "nextepisode"):
            self.bad({sec: {"libraries": "everything"}})
            self.bad({sec: {"libraries": {"../etc": {}}}})
        self.s.update({"warmup": {"libraries": {"1": {"schedule": "weekly"}}}, "optimizer": {"libraries": {"1": {"mkv": True}}},
                       "nextepisode": {"libraries": {"1": {"start": "next"}}}})
        self.s.update({"warmup": {"libraries": {"1": None}}, "optimizer": {"libraries": {"1": None}},
                       "nextepisode": {"libraries": {"1": None}}})
        for sec in ("warmup", "optimizer", "nextepisode"):
            self.assertNotIn("1", self.s.section(sec)["libraries"])
        self.s.update({"plex": {"url": ""}})                                  # an empty address just clears it
        self.assertEqual(self.s.section("plex")["url"], "")
        with self.assertRaises(config.SettingsError):
            config._coerce("general", "no_such_setting", 1, None)

    def test_old_settings_are_brought_up_to_date(self):
        """Settings from earlier versions load as today's: unticked libraries, the whole season near the
        end, a monthly warm-up, retired choices; a settings file that can't be moved aside is left."""
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"nextepisode": {"excluded_libraries": [3], "libraries": {"2": {"end": "whole"}}},
                       "warmup": {"libraries": {"2": {"schedule": "monthly"}}},
                       "general": {"poll_seconds": 7}}, fh)
        s = config.Settings(d)
        ne = s.section("nextepisode")["libraries"]
        self.assertEqual((ne["3"], ne["2"]["end"]), ({"start": "off", "end": "none"}, "rest"))
        self.assertEqual(s.section("warmup")["libraries"]["2"]["schedule"], "twice_monthly")
        self.assertEqual(s.section("general")["poll_seconds"], config.DEFAULTS["general"]["poll_seconds"])
        with open(os.path.join(d, "settings.json"), "w") as fh:
            fh.write("{broken")
        with mock.patch.object(config.os, "replace", mock.Mock(side_effect=OSError("read-only"))):
            self.assertEqual(s._load(), config.DEFAULTS)
        self.assertTrue(os.path.exists(os.path.join(d, "settings.json")))                # (left where it was)

    def test_every_setting_has_a_rule(self):
        for sec, values in config.DEFAULTS.items():
            for key, default in values.items():
                if key == "client_id" or (sec, key) in config.CHOICES or isinstance(default, bool):
                    continue
                if isinstance(default, int):
                    self.assertIn((sec, key), config.LIMITS, f"{sec}.{key} has no range")

    def test_ranges_and_choices(self):
        for (sec, key), (lo, hi) in config.LIMITS.items():
            if key == "ram_percent":  # the two RAM limits share 100%: make room for this one
                other = "nextepisode" if sec == "nowplaying" else "nowplaying"
                self.s.update({other: {"ram_percent": 5}})
            for ok in (lo, hi):
                self.s.update({sec: {key: ok}})
                self.assertEqual(self.s.section(sec)[key], ok)
            for wrong in (lo - 1, hi + 1, lo + 0.5, str(lo), True, None, float("nan"), float("inf"), [lo]):
                self.bad({sec: {key: wrong}})
        for (sec, key), choices in config.CHOICES.items():
            for ok in choices:
                self.s.update({sec: {key: ok}})
            wrong = [999, "nope", None, True, 1.5] + (["997"] if isinstance(choices[0], int) else [])
            for w in wrong:
                self.bad({sec: {key: w}})
        self.s.update({"general": {"poll_seconds": "10"}})  # a form field's "10" is fine for a numeric choice
        self.assertEqual(self.s.section("general")["poll_seconds"], 10)

    def test_types(self):
        for sec, key in (("warmup", "enabled"), ("mqtt", "tls"), ("general", "update_check")):
            for wrong in ("yes", "true", 1, 0, None, "ON"):
                self.bad({sec: {key: wrong}})
        for wrong in ("3:00", "24:00", "03:60", 300, None, "03:00 "):
            self.bad({"warmup": {"time": wrong}})
        self.bad({"general": {"client_id_x": 1}})
        self.bad({"general": {"hdd_measured_at": "yesterday"}})
        self.s.update({"general": {"hdd_measured_at": "2026-10-14T10:00:00"}})

    def test_text_fields(self):
        for url in ("javascript:alert(1)", "http://user:pw@nas", "http://nas/x?y=1", "ftp://nas", "http://nas:99999",
                    "nas name", "http://", 42, "http://nas\n"):
            self.bad({"plex": {"url": url}})
        self.s.update({"plex": {"url": "nas.local"}, "truenas": {"url": "192.168.1.10"}})
        self.assertEqual(self.s.section("plex")["url"], "http://nas.local:32400")
        self.assertEqual(self.s.section("truenas")["url"], "https://192.168.1.10")
        self.s.update({"plex": {"url": "https://plex.example.com/proxy"}})  # a reverse proxy with a path is fine
        for token in ("has space", "x" * 300, "tab\there", 123):
            self.bad({"plex": {"token": token}})
        for host in ("bad host", "evil;rm", "x" * 300, "a/b"):
            self.bad({"mqtt": {"host": host}})
        for ok in ("192.168.1.20", "homeassistant.local", "fe80::1", "[fe80::1]", ""):
            self.s.update({"mqtt": {"host": ok}})
        self.s.update({"mqtt": {"password": "pass with spaces & symbols!"}})
        self.bad({"mqtt": {"password": "line\nbreak"}})
        self.bad({"mqtt": {"username": "x" * 200}})
        for topic in ("a/#", "a/+/b", "", "x" * 120, "spaces here"):
            self.bad({"mqtt": {"base_topic": topic}})

    def test_lists_and_maps(self):
        self.bad({"general": {"path_map": [{"plex": "/data", "local": "relative/path"}]}})
        self.bad({"general": {"path_map": [{"plex": "/data", "local": "/media/../etc"}]}})
        self.bad({"general": {"path_map": ["not a row"]}})
        self.bad({"general": {"path_map": [{"plex": "/x", "local": "/y"}] * (config.MAX_ITEMS + 1)}})
        self.s.update({"general": {"path_map": [{"plex": "D:\\Movies", "local": "/media/movies/"}, {"plex": "/tv", "local": ""}]}})
        self.assertEqual(self.s.section("general")["path_map"], [{"plex": "D:\\Movies", "local": "/media/movies"}])
        self.bad({"nextepisode": {"libraries": {"1": {"start": "forever"}}}})
        self.bad({"nextepisode": {"libraries": {"../1": {"start": "off"}}}})
        self.bad({"nextepisode": {"libraries": ["off"]}})
        self.bad({"general": {"excluded_libraries": [1, 2]}})
        self.bad({"general": {"disk_speeds": {"tank/a": {"mb_s": float("nan")}}}})
        self.bad({"general": {"disk_speeds": {"tank/a": {"mb_s": 200, "measured_at": "soon"}}}})
        self.bad({"general": {"disk_speeds": {"tank/a": 200}}})

    def test_one_bad_value_changes_nothing(self):
        before = self.s.get()
        self.bad({"warmup": {"pacing_percent": 20, "reserve_gb": 9999}})
        self.assertEqual(self.s.get(), before)

    def test_http_api(self):
        from plexcache.server import serve
        app = _new_app()
        app.scan_library()
        httpd = serve(app, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def post(path, raw, headers=None):
            r = urllib.request.Request(base + path, data=raw if isinstance(raw, bytes) else json.dumps(raw).encode(),
                                       method="POST", headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(r) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read())
        try:
            self.assertEqual(post("/api/settings", b'{"warmup": {"reserve_gb": NaN}}')[0], 400)
            self.assertEqual(post("/api/settings", b'{"warmup": {"reserve_gb": Infinity}}')[0], 400)
            self.assertEqual(post("/api/settings", {"warmup": {"reserve_gb": "20"}})[0], 400)
            self.assertEqual(post("/api/settings", {"warmup": {"reserve_gb": 20}})[0], 200)
            self.assertEqual(post("/api/checks", {"rescan": "yes"})[0], 400)
            self.assertEqual(post("/login", {"username": ["a"], "password": "b"})[0], 200)  # login off: always fine
            # A prefetch choice must be for a library Prefetch covers, from the web UI as from MQTT
            tv = next(l for l in app.libraries if l["kind"] == "tv")
            photos = next(l for l in app.libraries if l["title"] == "Photos")
            music = next(l for l in app.libraries if l["title"] == "Music")
            status, body = post("/api/settings", {"nextepisode": {"libraries": {photos["key"]: {"start": "next"}}}})
            self.assertEqual(status, 400)
            self.assertIn("doesn’t cover", en(body["error"]))
            status, body = post("/api/settings", {"nextepisode": {"libraries": {music["key"]: {"start": "next_group"}}}})
            self.assertIn("isn’t a prefetch choice", en(body["error"]))
            self.assertEqual(post("/api/settings", {"nextepisode": {"libraries": {"9999": {"start": "off"}}}})[0], 400)
            self.assertEqual(post("/api/settings", {"nextepisode": {"libraries": {tv["key"]: {"start": "off"}}}})[0], 200)
            self.assertEqual(post("/api/settings", {"nextepisode": {"libraries": {}}})[0], 200)
            # Requests from another site's page are refused; this app's own pages and scripts are fine
            port = httpd.server_address[1]
            status, body = post("/api/settings", {"warmup": {"reserve_gb": 25}}, {"Origin": "http://evil.example.com"})
            self.assertEqual(status, 403)
            self.assertEqual(app.settings.section("warmup")["reserve_gb"], 20)
            self.assertEqual(post("/api/settings", {"warmup": {"reserve_gb": 25}}, {"Origin": f"http://127.0.0.1:{port}"})[0], 200)
            self.assertEqual(post("/api/settings", {"warmup": {"reserve_gb": 25}}, {"Origin": "https://plex.example.com",
                                                                                   "X-Forwarded-Host": "plex.example.com"})[0], 200)
            # Checking the Plex sign-in saves the token, so it's a POST (a link or image can't trigger it)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/api/plex/pin/1")
            self.assertEqual(cm.exception.code, 404)
            cm.exception.close()
            # a body length that isn't a number
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.putrequest("POST", "/api/settings")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "lots")
            conn.endheaders()
            resp = conn.getresponse()
            self.assertEqual((resp.status, json.loads(resp.read())["error"]), (400, "bad Content-Length"))
            conn.close()

            def get(path):
                try:
                    with urllib.request.urlopen(base + path) as r:
                        return r.status, r.read()
                except urllib.error.HTTPError as e:
                    with e:
                        return e.code, e.read()
            # the cache log: exported in full for a look into problems, and started again
            status, raw = get("/api/cachelog/export")
            self.assertEqual(status, 200)
            self.assertIsInstance(json.loads(raw), dict)
            self.assertEqual(get("/api/coverage")[0], 200)
            self.assertEqual(post("/api/cachelog/reset", {})[0], 200)
            self.assertIn("started again", en(app.activity.snapshot()["events"][0]["message"]))
            # a library file that can't be read now
            with mock.patch.object(app, "file_view", mock.Mock(side_effect=PermissionError("no access"))):
                self.assertEqual(get("/api/library/file?path=/x.mkv")[0], 404)
        finally:
            httpd.shutdown()
            httpd.server_close()


class ActivityTotalsTests(unittest.TestCase):
    def test_totals_count_up_and_survive_restart(self):
        from plexcache.activity import Activity
        d = tempfile.mkdtemp(dir=TMP)
        a = Activity(d)
        a.record("stream", label="x")
        a.record("skip", label="x", cached=True)
        a.record("skip", label="x", cached=False)
        a.record("next", label="x", ready=True)
        a.record("read", label="x", cached=100, disk=50)
        want = {"streams": 1, "skips": 2, "skips_cached": 1, "next": 1, "next_ready": 1, "preloaded_bytes": 150}
        self.assertEqual(a.totals, want)
        self.assertEqual(Activity(d).totals, want)
        # A history file from an older version (no totals) starts from its measurements
        path = os.path.join(d, "history.json")
        with open(path) as fh:
            data = json.load(fh)
        del data["totals"]
        with open(path, "w") as fh:
            json.dump(data, fh)
        self.assertEqual(Activity(d).totals, want)


class ErrorPathTests(unittest.TestCase):
    """What happens when reads fail or TrueNAS answers badly: reported, never crashing or miscounted."""

    def test_reader_errors(self):
        from unittest import mock
        from plexcache import reader
        d = tempfile.mkdtemp(dir=TMP)
        # Reading a folder fails: the error is reported, nothing counted
        r = reader.read_range(d, 0, 1024)
        self.assertTrue(r.error)
        f = os.path.join(d, "x.bin")
        with open(f, "wb") as fh:
            fh.write(os.urandom(3 * reader.MiB))
        # Each group's time is reported; read-ahead hints are optional
        groups = []
        with mock.patch.object(reader.os, "posix_fadvise", mock.Mock(side_effect=OSError("nope")), create=True):
            r = reader.read_range(f, 0, 3 * reader.MiB, on_group=lambda b, t: groups.append(b))
        self.assertEqual((r.bytes, r.error), (3 * reader.MiB, None))
        self.assertEqual(sum(groups), 3 * reader.MiB)
        # The benchmark falls back to an ordinary read when direct reads fail
        with mock.patch.object(reader.os, "preadv", mock.Mock(side_effect=OSError(22, "Invalid argument")), create=True):
            r = reader.benchmark_file(f, 3 * reader.MiB, window=reader.MiB)
        self.assertTrue(r.error)
        # Stopping during the gap between reads
        stop = threading.Event()
        stop.set()
        p = reader.Pacer(50, stop=stop)
        p.before_wait = lambda: None
        with self.assertRaises(reader.Stopped):
            p.after(0.01)

    def test_truenas_errors(self):
        from plexcache.truenas import TrueNASClient, TrueNASError, probe
        c = TrueNASClient.__new__(TrueNASClient)

        def broken(method, params=None):
            raise TrueNASError(f"{method} failed")
        c.call = broken
        self.assertEqual(c.version(), "")
        self.assertIsNone(c.dataset_cache_props("tank/x"))
        self.assertEqual(len(c.last_props_raw), 3)  # every way of asking was tried and noted
        self.assertFalse(probe("https://127.0.0.1:9", timeout=1))  # nothing there
        # A real connection whose key is refused
        with self.assertRaises(TrueNASError):
            TrueNASClient(TRUENAS.url, "wrong-key")


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        test = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                test.agent = self.headers.get("User-Agent")
                body = (test.runs_json if "/actions/runs" in self.path else test.body).encode()  # (the branch's newest finished build is asked too)
                test.paths = getattr(test, "paths", []) + [self.path]
                self.send_response(test.code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.body, self.code = '"""x"""\n\n__version__ = "9.0.0"\n', 200
        self.runs_json = json.dumps({"workflow_runs": [{"head_sha": "0000000"}]})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/plexcache/__init__.py"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_versions(self):
        from plexcache.updates import newer, parse_version
        self.assertTrue(newer("0.14.0", "0.13.9"))
        self.assertTrue(newer("1.0", "0.99.9"))
        self.assertFalse(newer("0.13.0", "0.13.0"))
        # a pre-release comes before its release (so a beta is told when the release is out)
        self.assertTrue(newer("0.16.0", "0.16.0-beta.1"))
        self.assertFalse(newer("0.16.0-beta.1", "0.16.0"))
        self.assertTrue(newer("0.16.0-beta.2", "0.16.0-beta.1"))
        self.assertTrue(newer("0.16.0-beta.1", "0.15.0"))
        self.assertFalse(newer("0.12.9", "0.13.0"))
        self.assertEqual(parse_version("__version__ = '1.2.3'\n"), "1.2.3")
        self.assertIsNone(parse_version("nothing here"))
        self.assertIn("raw.githubusercontent.com/drsassafras/TrueNAS-Plex-Caching/main/plexcache/__init__.py",
                      __import__("plexcache.updates").updates.SOURCE)
        # the channel: a dev build reads the dev branch's version and its newest commit; the label says -dev
        from plexcache import updates, version_label
        self.assertTrue(updates.source_for("dev").endswith("/dev/plexcache/__init__.py"))
        self.assertIn("/actions/runs?branch=dev&status=success", updates.runs_source_for("dev"))
        self.assertIn("branch=main", updates.runs_source_for("stable"))
        self.assertTrue(updates.source_for("dev", "abc123").endswith("/abc123/plexcache/__init__.py"))
        self.assertEqual((version_label("stable"), version_label("dev")), (updates.__version__, updates.__version__ + "-dev"))
        app = _new_app()
        self.assertTrue(updates.UpdateChecker(app, self.url, channel="stable").build_url().endswith("/releases/tag/v" + updates.__version__))
        self.assertTrue(updates.UpdateChecker(app, self.url, channel="dev", commit="abc1234def").build_url().endswith("/commit/abc1234def"))
        self.assertTrue(updates.UpdateChecker(app, self.url, channel="dev", commit="").build_url().endswith("/tree/dev"))
        self.assertIn("url", updates.UpdateChecker(app, self.url).status())

    def test_check(self):
        from plexcache.homeassistant import update_info
        from plexcache.updates import UpdateChecker
        app = _new_app()
        u = app.updates = UpdateChecker(app, self.url)
        self.assertTrue(u.check())
        self.assertEqual(u.status()["latest"], "9.0.0")
        self.assertTrue(u.status()["available"])
        self.assertIn("plex-cache/", self.agent)
        self.assertTrue(any("9.0.0 is available" in e["message"] for e in app.activity.snapshot()["events"]))
        info = update_info(app)
        self.assertEqual((info["installed_version"], info["latest_version"]), (app.version, "9.0.0"))
        self.assertIn("release_summary", info)
        self.assertEqual(app.state()["updates"]["latest"], "9.0.0")
        # Same version as running: up to date
        self.body = f'__version__ = "{app.version}"\n'
        self.assertTrue(u.check())
        self.assertFalse(u.status()["available"])
        self.assertNotIn("release_summary", update_info(app))
        # Errors are kept, not raised
        self.code, self.body = 404, "nope"
        self.assertFalse(u.check())
        self.assertTrue(u.status()["error"])
        self.code, self.body = 200, "no version"
        self.assertFalse(u.check())
        # a dev build: the same version number, but dev's newest finished build is another commit — an update
        from plexcache import updates
        self.body = f'__version__ = "{updates.__version__}"\n'
        self.runs_json = json.dumps({"workflow_runs": [{"head_sha": "abcdef0123456789"}]})
        d = app.updates = UpdateChecker(app, self.url, channel="dev", runs_source=self.url + "/actions/runs?x", commit="1234567890ab")
        self.assertTrue(d.check())
        self.assertEqual((d.status()["available"], d.status()["channel"], d.status()["latest_commit"], d.status()["commit"]), (True, "dev", "abcdef0", "1234567"))
        self.assertTrue(any("newer dev build" in en(e["message"]) for e in app.activity.snapshot()["events"]))
        d = UpdateChecker(app, self.url, channel="dev", runs_source=self.url + "/actions/runs?x", commit="abcdef0123456789")
        self.assertTrue(d.check())
        self.assertFalse(d.status()["available"])                                                 # (this build is dev's newest built one)
        # a push still building (no finished run newer than ours): nothing offered, whatever dev's head is
        self.runs_json = json.dumps({"workflow_runs": []})
        d = UpdateChecker(app, self.url, channel="dev", runs_source=self.url + "/actions/runs?x", commit="abcdef0123456789")
        self.assertTrue(d.check())
        self.assertFalse(d.status()["available"])
        self.assertTrue(d.check_async())                                                           # (Check Now)
        self.assertTrue(wait_for(lambda: not d.checking))

    def test_background_loop(self):
        from plexcache.updates import UpdateChecker
        app = _new_app()
        u = app.updates = UpdateChecker(app, self.url)
        u.first_delay = 0
        u.start()
        self.assertTrue(wait_for(lambda: u.latest == "9.0.0"))
        # Switched off: no more checks
        app.settings.update({"general": {"update_check": False}})
        self.body = '__version__ = "10.0.0"\n'
        u.wake.set()
        time.sleep(0.5)
        self.assertEqual(u.latest, "9.0.0")
        app.stopping.set()
        u.wake.set()

    def test_setting_and_wake(self):
        from plexcache.server import serve
        app = _new_app()
        from plexcache.updates import UpdateChecker
        app.updates = UpdateChecker(app, self.url)
        self.assertTrue(app.settings.section("general")["update_check"])
        httpd = serve(app, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            r = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/settings",
                                       data=json.dumps({"general": {"update_check": True}}).encode(), method="POST",
                                       headers={"Content-Type": "application/json"})
            urllib.request.urlopen(r).close()
            self.assertTrue(app.updates.wake.is_set())
        finally:
            httpd.shutdown()
            httpd.server_close()


class MQTTTests(unittest.TestCase):
    """Home Assistant over MQTT, against a small fake broker."""

    @classmethod
    def setUpClass(cls):
        cls.broker = fakes.FakeMQTTBroker()
        cls.app = _new_app(nowplaying={"enabled": True})
        cls.app.scan_library()
        cls.app.run_checks()

    @classmethod
    def tearDownClass(cls):
        cls.broker.close()

    def setUp(self):
        # Start every test from the defaults, so one failure can't knock over the next.
        self.app.settings.update({"mqtt": dict(config.DEFAULTS["mqtt"])})
        self.broker.reset()

    def tearDown(self):
        self.app.ha.close()
        t = getattr(self.app.ha, "thread", None)
        self.assertFalse(t and t.is_alive(), "the MQTT thread didn't stop")
        self.app.ha = type(self.app.ha)(self.app)

    def enable(self, **extra):
        cfg = {"enabled": True, "host": "127.0.0.1", "port": self.broker.port, "username": "ha", "password": "secret"}
        cfg.update(extra)
        self.app.settings.update({"mqtt": cfg})
        self.app.ha.start()

    def state(self):
        raw = self.broker.state("plexcache/state")
        return json.loads(raw) if raw else None

    def test_texts_stand_on_their_own(self):
        """The web pages' summaries say "see below"; Home Assistant has nothing below them."""
        from plexcache.homeassistant import _health_text, _verdict
        rep = {"span": "Last 7 days", "verdict": {"status": "warn", "text": "Last 7 days: 2 things could be better — see below."},
               "attention": [{"status": "warn", "title": "Preloads aren’t staying in RAM (22% still cached)"},
                             {"status": "info", "title": "Not a problem"},
                             {"status": "warn", "title": "1 next episode not ready in time"}]}
        self.assertEqual(_verdict(rep), "Last 7 days: Preloads aren’t staying in RAM (22% still cached); 1 next episode not ready in time")
        self.assertEqual(_verdict({"verdict": {"status": "ok", "text": "All good."}}), "All good.")
        self.assertEqual(_health_text({"summary": {"status": "fail"}, "counts": {"fail": 1, "warn": 2}}),
                         "1 problem to fix and 2 warnings — open Health in Plex Cache for the details.")
        self.assertEqual(_health_text({"summary": {"status": "ok", "text": "Everything is OK."}}), "Everything is OK.")
        for text in (_verdict(rep), _health_text({"summary": {"status": "warn"}, "counts": {"warn": 1}})):
            self.assertNotIn("below", text)

    def test_helpers(self):
        from plexcache.mqttclient import _packet, topic_matches
        self.assertTrue(topic_matches("a/#", "a/b/c"))
        self.assertTrue(topic_matches("a/+/c", "a/b/c"))
        self.assertFalse(topic_matches("a/+", "a/b/c"))
        self.assertFalse(topic_matches("a/b", "a"))
        self.assertEqual(_packet(0x30, b"x" * 200)[:3], bytes([0x30, 0xC8, 0x01]))  # two-byte length
        # A huge message from the broker is refused before it's read into memory
        from plexcache.mqttclient import MQTTClient, MQTTError
        a, b = socket.socketpair()
        try:
            c = MQTTClient("x")
            c.sock = a
            b.sendall(bytes([0x30, 0xFF, 0xFF, 0xFF, 0x7F]))  # claims 268 MB
            with self.assertRaises(MQTTError):
                c.poll(1)
        finally:
            a.close()
            b.close()
        # Same for the TrueNAS connection
        from plexcache import wsclient
        a, b = socket.socketpair()
        try:
            ws = wsclient.WebSocket.__new__(wsclient.WebSocket)
            ws.sock, ws._buf = a, b""
            b.sendall(bytes([0x81, 127]) + struct.pack("!Q", 2 ** 40))
            with self.assertRaises(wsclient.WebSocketError):
                ws.recv_text()
        finally:
            a.close()
            b.close()

    def test_off_by_default(self):
        self.assertFalse(config.DEFAULTS["mqtt"]["enabled"])
        self.app.ha.start()
        time.sleep(0.3)
        self.assertEqual(self.app.ha.status["state"], "off")
        self.assertEqual(self.app.state()["mqtt"]["state"], "off")
        self.assertEqual(self.broker.connects, [])

    def test_settings_validation_and_secret(self):
        for bad in ({"port": 0}, {"interval": 1}, {"report_period": "year"}, {"base_topic": "a/#"}, {"discovery_prefix": ""}):
            with self.assertRaises(config.SettingsError, msg=bad):
                self.app.settings.update({"mqtt": bad})
        for bad in ({"backup": "none"}, {"page_size": 10}, {"page_size": 5000}, {"snapshot_days": 0}, {"snapshot_days": "14"}):
            with self.assertRaises(config.SettingsError, msg=bad):
                self.app.settings.update({"general": bad})
        self.app.settings.update({"general": {"backup": "manual", "page_size": 50, "snapshot_days": 30}})
        g = self.app.settings.section("general")
        self.assertEqual((g["backup"], g["page_size"], g["snapshot_days"]), ("manual", 50, 30))
        self.app.settings.update({"general": {"backup": "snapshot", "page_size": 200, "snapshot_days": 14}})
        self.app.settings.update({"mqtt": {"password": "pw"}})
        pub = self.app.settings.public()["mqtt"]
        self.assertTrue(pub["password_set"])
        self.assertNotEqual(pub["password"], "pw")
        self.assertEqual(self.app.settings.export()["mqtt"]["password"], "")

    def test_visit_link_uses_the_real_address(self):
        from plexcache import app as appmod
        self.assertTrue(appmod._is_local_name("192.168.1.10"))
        self.assertTrue(appmod._is_local_name("truenas.local"))
        self.assertTrue(appmod._is_local_name("nas"))
        self.assertFalse(appmod._is_local_name("rebind.attacker.example.com"))
        self.assertEqual(appmod._split_host("[fe80::1]:8643"), ("fe80::1", 8643))
        self.assertEqual(appmod._split_host("10.0.0.2:99999"), (None, None))
        saved = self.app.web_url
        try:
            self.app.note_host("rebind.attacker.example.com:8642")  # ignored
            self.assertEqual(self.app.web_url, saved)
            self.app._autodetected.add("192.168.50.222")  # don't go probing
            self.app.note_host("192.168.50.222:8643")
            self.assertEqual(self.app.web_url, "http://192.168.50.222:8643")
            self.assertEqual(appmod.App(self.app.config_dir).web_url, "http://192.168.50.222:8643")  # kept
            self.enable()
            self.assertTrue(wait_for(lambda: self.broker.state(next(
                (t for t in self.broker.retained if t.endswith("/health/config")), "x")) is not None))
            cfg = json.loads(next(v for t, v in self.broker.retained.items() if t.endswith("/health/config")))
            self.assertEqual(cfg["device"]["configuration_url"], "http://192.168.50.222:8643")
        finally:
            self.app.web_url = saved
            self.app.host = None

    def test_publishes_discovery_and_state(self):
        self.enable()
        self.assertTrue(wait_for(lambda: self.state() is not None), self.app.ha.status)
        st = self.state()
        self.assertEqual(self.broker.state("plexcache/availability"), b"online")
        self.assertEqual(st["version"], self.app.version)
        self.assertIn(st["health"], ("ok", "warning", "problem", "checking"))
        self.assertEqual(st["plex_connected"], "ON")
        self.assertEqual(st["nowplaying_enabled"], "ON")
        self.assertEqual(st["streams"], 0)
        self.assertIn("total_streams", st)
        disc = {t: json.loads(v) for t, v in self.broker.retained.items() if t.startswith("homeassistant/")}
        self.assertGreater(len(disc), 60)
        self.assertEqual(self.app.ha.status["state"], "connected")
        self.assertEqual(self.app.ha.status["entities"], len(disc))
        health = next(v for t, v in disc.items() if t.endswith("/health/config"))
        self.assertEqual(health["state_topic"], "plexcache/state")
        self.assertEqual(health["availability_topic"], "plexcache/availability")
        self.assertEqual(health["device"]["name"], "Plex Cache")
        components = {t.split("/")[1] for t in disc}
        self.assertEqual(components, {"sensor", "binary_sensor", "switch", "button", "number", "select", "update"})
        # Grouped under a main device and three linked ones
        devices = {}
        for t, v in disc.items():
            devices.setdefault(v["device"]["name"], set()).add(t.split("/")[3])
        self.assertEqual(set(devices), {"Plex Cache", "Plex Cache Metrics", "Plex Cache Warm-up", "Plex Cache RAM & ZFS"})
        self.assertIn("skips", devices["Plex Cache Metrics"])
        self.assertIn("warmup_progress", devices["Plex Cache Warm-up"])
        self.assertIn("arc_size", devices["Plex Cache RAM & ZFS"])
        self.assertIn("run_warmup", devices["Plex Cache"])  # controls stay together
        sub = next(v for t, v in disc.items() if v["device"]["name"] == "Plex Cache Metrics")
        self.assertEqual(sub["device"]["via_device"], health["device"]["identifiers"][0])
        self.assertFalse(any("_device" in v for v in disc.values()))
        self.assertEqual(next(v for t, v in disc.items() if "/health_passed/" in t)["entity_category"], "diagnostic")
        # Every entity has a description, read from one retained message as its "description" attribute
        self.assertTrue(wait_for(lambda: self.broker.state("plexcache/info") is not None))
        info = json.loads(self.broker.state("plexcache/info"))
        for t, v in disc.items():
            oid = t.split("/")[3]
            self.assertEqual(v["json_attributes_topic"], "plexcache/info", t)
            self.assertEqual(v["json_attributes_template"], "{{ {'description': value_json['%s']} | tojson }}" % oid)
            self.assertTrue(info.get(oid), f"{oid} has no description")
            self.assertNotIn("<", info[oid])  # plain text: Home Assistant shows attributes as-is
        self.assertLess(len(self.broker.state("plexcache/info")), 20000)
        # Number controls show their value and unit (a slider wouldn't, on the device page)
        numbers = [v for t, v in disc.items() if t.startswith("homeassistant/number/")]
        self.assertEqual(len(numbers), 4)                                               # (pacing's share: unavailable while automatic)
        self.assertTrue(any("/warmup_pacing_auto/" in t for t in disc))
        for n in numbers:
            self.assertEqual(n["mode"], "box")
            self.assertIn(n["unit_of_measurement"], ("%", "GB"))
        # Dropped: a recommendation shown as if it were a minimum, and a figure nobody needs
        self.assertFalse(any("/arc_max/" in t or "/l2arc_needed/" in t for t in disc))
        self.assertNotIn("l2arc_needed", st)
        # The update entity
        self.assertEqual(st["update"]["installed_version"], self.app.version)
        upd = next(v for t, v in disc.items() if t.startswith("homeassistant/update/"))
        self.assertEqual(upd["release_url"], "https://github.com/drsassafras/TrueNAS-Plex-Caching")
        # Home Assistant reads the versions straight from their own topic (no template)
        self.assertEqual(upd["state_topic"], "plexcache/update")
        self.assertNotIn("value_template", upd)
        self.assertTrue(wait_for(lambda: self.broker.state("plexcache/update") is not None))
        versions = json.loads(self.broker.state("plexcache/update"))
        self.assertEqual((versions["installed_version"], versions["latest_version"]), (self.app.version, self.app.version))
        # Per library, the two Prefetch choices in the library's own words, Default first
        libs = {v["name"]: v for t, v in disc.items() if "/library_" in t}
        tv = libs["TV Shows Lead-in Cache prefetch"]
        self.assertEqual(tv["options"], ["Default", "Don’t prefetch", "Next episode", "Rest of the season", "Whole season",
                                         "Next season", "All seasons of the show", "Every show in the library"])
        self.assertEqual(libs["TV Shows Whole File Cache prefetch"]["options"], ["Default", "Nothing", "Next episode", "Rest of the season"])
        self.assertEqual(len(libs["Music Lead-in Cache prefetch"]["options"]), 7)          # no "next album"
        self.assertIn("Next book", libs["Audiobooks Lead-in Cache prefetch"]["options"])
        self.assertEqual(libs["Movies Lead-in Cache prefetch"]["options"],
                         ["Default", "Don’t prefetch", "Next movie in the collection", "Rest of the collection", "Whole collection",
                          "Every movie in the library"])
        # Every template reads a key that exists in the state
        import re as _re
        for t, v in disc.items():
            m = _re.match(r"\{\{ value_json\.([a-z0-9_]+)", v.get("value_template", ""))
            if m:
                self.assertIn(m.group(1), st, t)

    def test_commands(self):
        # (put back whatever happens, so a failure here doesn't spill into the tests after it)
        self.addCleanup(self.app.settings.update, {"nowplaying": {"enabled": True, "ram_percent": 20},
                                                   "general": {"poll_seconds": 5, "ram_reserve_gb": 4},
                                                   "warmup": {"pacing_percent": 10, "pacing_auto": True}})
        self.enable()
        self.assertTrue(wait_for(lambda: self.state() is not None))
        self.assertTrue(wait_for(lambda: any("plexcache/set/#" in c[1] for c in self.broker.clients)))
        self.broker.inject("plexcache/set/nowplaying_ram_percent", "35")
        self.assertTrue(wait_for(lambda: self.app.settings.section("nowplaying")["ram_percent"] == 35))
        self.broker.inject("plexcache/set/nowplaying_enabled", "OFF")
        self.assertTrue(wait_for(lambda: not self.app.settings.section("nowplaying")["enabled"]))
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("nowplaying_enabled") == "OFF"))
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("available", {}).get("nowplaying_ram_percent") is False))
        # its RAM limit doesn't apply while it's off: unavailable in Home Assistant, and a command for it refused
        self.broker.inject("plexcache/set/nowplaying_ram_percent", "40")
        self.assertTrue(wait_for(lambda: any("only while Now Playing Cache is on" in e["message"] for e in self.app.activity.snapshot()["events"])))
        self.assertEqual(self.app.settings.section("nowplaying")["ram_percent"], 35)
        self.broker.inject("plexcache/set/poll_seconds", "10 seconds")
        self.assertTrue(wait_for(lambda: self.app.settings.section("general")["poll_seconds"] == 10))
        tv = next(l for l in self.app.libraries if l["kind"] == "tv")
        libs = lambda: self.app.settings.section("nextepisode")["libraries"]
        self.broker.inject(f"plexcache/set/library_{tv['key']}_prefetch_start", "Don’t prefetch")
        self.assertTrue(wait_for(lambda: libs().get(tv["key"], {}).get("start") == "off"))
        self.broker.inject(f"plexcache/set/library_{tv['key']}_prefetch_end", "Rest of the season")
        self.assertTrue(wait_for(lambda: libs().get(tv["key"], {}).get("end") == "rest"))
        self.assertEqual(libs()[tv["key"]], {"start": "off", "end": "rest"})
        # Not a choice for a TV library: refused, nothing changes
        self.broker.inject(f"plexcache/set/library_{tv['key']}_prefetch_start", "Next book")
        self.broker.inject("plexcache/set/poll_seconds", "2 seconds")
        self.broker.inject("plexcache/set/nope", "1")
        self.assertTrue(wait_for(lambda: sum("couldn’t do" in e["message"] for e in self.app.activity.snapshot()["events"]) >= 3))
        self.assertEqual(libs()[tv["key"]], {"start": "off", "end": "rest"})
        # Buttons, numbers and the remaining selects
        calls = []
        from plexcache import homeassistant as ha
        everything = mock.patch.object(ha, "control_availability", lambda app, s, wu: {k: True for k in ha.AVAILABLE_WHEN})
        everything.start()                                                              # (whatever the NAS: L2ARC, warm-up running)
        self.addCleanup(everything.stop)
        saved = {n: getattr(self.app, n) for n in ("start_warmup", "stop_warmup", "run_checks_async", "run_benchmark")}
        self.app.start_warmup = lambda manual: calls.append("run")
        self.app.stop_warmup = lambda: calls.append("stop")
        self.app.run_checks_async = lambda rescan=False: calls.append("recheck")
        self.app.run_benchmark = lambda: calls.append("benchmark")
        try:
            for b in ("run_warmup", "stop_warmup", "recheck", "benchmark"):
                self.broker.inject(f"plexcache/set/{b}", "PRESS")
            self.assertTrue(wait_for(lambda: len(calls) == 4))
            self.assertEqual(sorted(calls), ["benchmark", "recheck", "run", "stop"])
        finally:
            for n, f in saved.items():
                setattr(self.app, n, f)
        # Out of range, not a whole number, or not a number: refused by the same rules as the web UI
        for name, value in (("nowplaying_ram_percent", "95"), ("warmup_pacing_percent", "2"), ("ram_reserve_gb", "6.5"),
                            ("ram_reserve_gb", "nan"), ("poll_seconds", "7")):
            n = sum("couldn’t do" in e["message"] for e in self.app.activity.snapshot()["events"])
            self.broker.inject(f"plexcache/set/{name}", value)
            self.assertTrue(wait_for(lambda: sum("couldn’t do" in e["message"] for e in self.app.activity.snapshot()["events"]) > n),
                            (name, value))
        self.broker.inject("plexcache/set/warmup_pacing_percent", "25")
        self.broker.inject("plexcache/set/ram_reserve_gb", "6")
        self.assertTrue(wait_for(lambda: self.app.settings.section("warmup")["pacing_percent"] == 25
                                 and self.app.settings.section("general")["ram_reserve_gb"] == 6))
        # Switching on a feature that can't work here is refused, with the reason
        saved_fa = self.app.feature_available
        self.app.feature_available = lambda feat: (False, "no L2ARC on this pool")
        try:
            self.app.settings.update({"nowplaying": {"enabled": False}})
            self.broker.inject("plexcache/set/nowplaying_enabled", "ON")
            self.assertTrue(wait_for(lambda: any("no L2ARC" in e["message"] for e in self.app.activity.snapshot()["events"])))
            self.assertFalse(self.app.settings.section("nowplaying")["enabled"])
        finally:
            self.app.feature_available = saved_fa
        # automatic L2ARC pacing takes ON or OFF; a Prefetch choice needs a library Plex has
        self.broker.inject("plexcache/set/warmup_pacing_auto", "OFF")
        self.assertTrue(wait_for(lambda: self.app.settings.section("warmup")["pacing_auto"] is False))
        for name, value in (("warmup_pacing_auto", "maybe"), ("library_9999_prefetch_start", "off")):
            n = sum("couldn’t do" in e["message"] for e in self.app.activity.snapshot()["events"])
            self.broker.inject(f"plexcache/set/{name}", value)
            self.assertTrue(wait_for(lambda: sum("couldn’t do" in e["message"] for e in self.app.activity.snapshot()["events"]) > n),
                            (name, value))
        self.app.settings.update({"nowplaying": {"enabled": True, "ram_percent": 20}, "general": {"poll_seconds": 5, "ram_reserve_gb": 4},
                                  "warmup": {"pacing_percent": 10, "pacing_auto": True}})
        everything.stop()

    def test_state_details(self):
        """Warm-up's state as Home Assistant shows it, texts cut to what it keeps, times in ISO, and
        the health summary when only problems are left."""
        from plexcache import homeassistant as ha
        app = _new_app()
        real = app.state
        on = mock.patch.object(app, "warmup_schedules", lambda: {"1": "daily"})
        on.start()
        self.addCleanup(on.stop)
        for wu, want in (({"running": True, "progress": {"paused": "a player is buffering"}}, "paused"),
                         ({"running": True, "progress": {}}, "preparing"), ({"running": True, "progress": {"total": 10}}, "running")):
            with mock.patch.object(app, "state", lambda wu=wu: dict(real(), warmup=wu)):
                self.assertEqual(ha.build_state(app)["warmup_state"], want)
        on.stop()
        with mock.patch.object(app, "warmup_schedules", lambda: {"1": "none"}):
            self.assertEqual(ha.build_state(app)["warmup_state"], "off")
        long = {"summary": {"status": "ok", "text": "x" * 300}, "counts": {}}
        with mock.patch.object(app, "state", lambda: dict(real(), health=long)):
            self.assertEqual(len(ha.build_state(app)["health_text"]), 250)
        self.assertEqual(en(ha._health_text({"summary": {"status": "fail"}, "counts": {"fail": 2}})),
                         "2 problems to fix — open Health in Plex Cache for the details.")
        self.assertIsNone(ha._iso("not a time"))
        self.assertTrue(ha._iso("2026-10-14T03:00").startswith("2026-10-14T03:00"))
        self.assertEqual(ha.describe("no_such_entity"), "")

    def test_watch_only(self):
        self.enable(allow_control=False)
        self.assertTrue(wait_for(lambda: self.state() is not None))
        comps = {t.split("/")[1] for t in self.broker.retained if t.startswith("homeassistant/")}
        self.assertEqual(comps, {"sensor", "binary_sensor", "update"})
        self.broker.inject("plexcache/set/nowplaying_enabled", "OFF")
        time.sleep(0.5)
        self.assertTrue(self.app.settings.section("nowplaying")["enabled"])

    def steps(self):
        return {k: v["status"] for k, v in self.app.ha.status["steps"].items()}

    def test_old_entities_are_removed_even_after_a_restart(self):
        node = None
        # Entities from an older version are still retained on the broker
        self.enable()
        self.assertTrue(wait_for(lambda: self.state() is not None))
        health = next(t for t in self.broker.retained if t.endswith("/health/config"))
        node = health.split("/")[2]
        self.app.ha.close()
        stale = [f"homeassistant/sensor/{node}/arc_max/config", f"homeassistant/sensor/{node}/l2arc_needed/config",
                 f"homeassistant/select/{node}/library_999_preload/config"]
        for t in stale:
            self.broker.inject(t, "{}")
            with self.broker.lock:
                self.broker.retained[t] = b"{}"
        # A library that existed last time it ran (remembered in the config folder)
        self.app._save_json("mqtt-entities.json", {"topics": [stale[2]]})
        self.app.ha = type(self.app.ha)(self.app)
        self.app.ha.start()
        self.assertTrue(wait_for(lambda: not any(t in self.broker.retained for t in stale)), [t for t in stale if t in self.broker.retained])
        saved = self.app._load_json("mqtt-entities.json")["topics"]
        self.assertIn(health, saved)

    def test_discovery_follows_home_assistant_rules(self):
        """Every discovery message follows Home Assistant's MQTT discovery rules
        (https://www.home-assistant.io/integrations/mqtt/)."""
        import re
        from plexcache import homeassistant as ha
        self.enable()
        self.assertTrue(wait_for(lambda: self.app.ha.status.get("entities") and self.state() is not None, 15))
        state = self.state()
        configs = {t: v for t, v in self.broker.retained.items() if t.endswith("/config") and v}
        self.assertGreater(len(configs), 50)
        components = {"sensor", "binary_sensor", "switch", "button", "number", "select", "update"}
        data_units = {"bit", "kbit", "Mbit", "Gbit", "B", "kB", "MB", "GB", "TB", "PB", "KiB", "MiB", "GiB", "TiB", "PiB"}
        seen_ids = set()
        for topic, raw in configs.items():
            with self.subTest(topic):
                # <prefix>/<component>/<node_id>/<object_id>/config, ids from [a-zA-Z0-9_-]
                m = re.fullmatch(r"homeassistant/([a-z_]+)/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/config", topic)
                self.assertIsNotNone(m)
                comp = m.group(1)
                self.assertIn(comp, components)
                conf = json.loads(raw)                                                    # a JSON dictionary
                self.assertIsInstance(conf, dict)
                # unique_id (unique), and a device with identifiers and a name
                self.assertTrue(conf.get("unique_id"))
                self.assertNotIn(conf["unique_id"], seen_ids)
                seen_ids.add(conf["unique_id"])
                self.assertTrue(conf["device"].get("identifiers") and conf["device"].get("name"))
                # origin: which software sent it (its name is required)
                self.assertEqual(conf["origin"]["name"], "Plex Cache")
                # default_entity_id (object_id no longer sets the entity ID)
                self.assertNotIn("object_id", conf)
                self.assertRegex(conf["default_entity_id"], r"^" + comp + r"\.[a-z0-9_]+$")
                # availability: a topic or a list, not both; the payloads as published. A control that applies only
                # sometimes: Plex Cache online AND its condition, read from the state message
                self.assertFalse("availability_topic" in conf and "availability" in conf)
                oid = m.group(3)
                if oid in ha.AVAILABLE_WHEN:
                    self.assertEqual(conf["availability_mode"], "all")
                    first, cond = conf["availability"]
                    self.assertIn(self.broker.state(first["topic"]), ("online", b"online"))
                    self.assertEqual(cond["topic"], "plexcache/state")
                    self.assertIn("value_json.available.%s " % oid, cond["value_template"])
                else:
                    self.assertIn(self.broker.state(conf["availability_topic"]), ("online", b"online"))
                self.assertNotIn("payload_available", conf)                              # (the default, "online")
                # entity categories: config or diagnostic, and sensors are never config
                if "entity_category" in conf:
                    self.assertIn(conf["entity_category"], ("config", "diagnostic"))
                    if comp in ("sensor", "binary_sensor"):
                        self.assertEqual(conf["entity_category"], "diagnostic")
                # sensors: device class, state class and unit fit together
                dc, sc, unit = conf.get("device_class"), conf.get("state_class"), conf.get("unit_of_measurement")
                if comp == "sensor":
                    self.assertIn(sc, (None, "measurement", "total", "total_increasing"))
                    if dc == "enum":
                        self.assertTrue(conf.get("options"))
                        self.assertIsNone(unit)
                        self.assertIsNone(sc)
                    if dc == "timestamp":
                        self.assertIsNone(unit)
                        self.assertIsNone(sc)
                    if dc == "duration":
                        self.assertIn(unit, ("d", "h", "min", "s", "ms"))
                    if dc == "data_size":
                        self.assertIn(unit, data_units)
                # what can be commanded has a command topic; a button has no state
                if comp in ("switch", "button", "number", "select"):
                    self.assertTrue(conf.get("command_topic"))
                if comp == "button":
                    self.assertNotIn("state_topic", conf)
                elif comp in ("switch", "number", "select", "sensor", "binary_sensor", "update"):
                    self.assertTrue(conf.get("state_topic"))
                if comp == "number":
                    self.assertLess(conf["min"], conf["max"])
                    self.assertGreater(conf["step"], 0)
                if comp == "select":
                    self.assertTrue(conf["options"])
                    self.assertEqual(len(conf["options"]), len(set(conf["options"])))
                # the value it reads exists in the state it reads
                vt = conf.get("value_template") or ""
                for path in re.findall(r"value_json((?:\.[a-zA-Z0-9_]+|\['[^']+'\])+)", vt):
                    node = state
                    for part in re.findall(r"\.([a-zA-Z0-9_]+)|\['([^']+)'\]", path):
                        key = part[0] or part[1]
                        self.assertIn(key, node, f"{vt} reads a value the state doesn't have")
                        node = node[key]
                if conf.get("json_attributes_topic"):
                    self.assertIsNotNone(self.broker.state(conf["json_attributes_topic"]))
        # It listens for Home Assistant's birth message, to announce everything again
        self.assertTrue(any("homeassistant/status" in patterns for _, patterns, _ in self.broker.clients))
        # ...and its last will marks it offline
        self.assertTrue(any(will and will[0] == "plexcache/availability" and will[1] in ("offline", b"offline")
                            for _, _, will in self.broker.clients))

    def test_controls_match_the_web_ui(self):
        """Every setting Home Assistant can change is one the web UI offers, under the same conditions."""
        import re
        from plexcache import homeassistant as ha
        js = open(os.path.join(ROOT, "plexcache", "web", "app.js"), encoding="utf-8").read()

        def offered(sec, key):  # the page has a field (or switch) for it
            return (re.search(r'\b(?:range|ramSlider|select|number|checkbox|textInput)\("%s", "%s"' % (sec, key), js) is not None
                    or f'data-set="{sec}.{key}"' in js or f'data-check="{sec}.{key}"' in js
                    or (key == "enabled" and f'toggle("{sec}"' in js))
        state = {"libraries": {"2": {"name": "TV Shows", "kind": "tv"}}}
        controls = [(c, oid) for c, oid, _ in ha.entities(state, allow_control=True) if c in ("switch", "number", "select")]
        self.assertGreater(len(controls), 5)
        for comp, oid in controls:
            with self.subTest(oid):
                if oid.startswith("library_"):
                    self.assertIn('pickHtml("lib:" + l.key + ".%s"' % oid.rsplit("_", 1)[1], js)    # a library's Prefetch choice
                    continue
                self.assertIn(oid, ha.CONTROL_SETTINGS, "a control that isn't mapped to a setting")
                sec, key = ha.CONTROL_SETTINGS[oid]
                self.assertIn(key, config.DEFAULTS[sec])
                self.assertTrue(offered(sec, key), f"{sec}.{key} can be changed from Home Assistant but not in the web UI")
        # ...and nothing mapped that isn't published
        self.assertEqual(set(ha.CONTROL_SETTINGS) - {oid for _, oid in controls}, set())
        # Same conditions: the share-of-disk-time slider only shows while automatic pacing is off (its control: unavailable)
        self.assertIn("var manual = !w.pacing_auto || !rate;", js)
        # Watch-only: no controls at all
        self.assertFalse([c for c, _, _ in ha.entities(state, allow_control=False) if c in ("switch", "number", "select", "button")])

    def test_every_control_says_when_it_applies(self):
        """Every Home Assistant control either applies always or says when (AVAILABLE_WHEN); one that
        doesn't apply shows as unavailable. A new control has to be put in one or the other."""
        from types import SimpleNamespace
        from plexcache import homeassistant as ha
        state = {"libraries": {"2": {"name": "TV Shows", "kind": "tv"}}}
        controls = {oid for c, oid, _ in ha.entities(state, allow_control=True) if c in ("switch", "number", "select", "button")}
        for oid in controls:
            with self.subTest(oid):
                self.assertTrue(oid in ha.AVAILABLE_WHEN or oid in ha.ALWAYS_AVAILABLE or oid.startswith("library_"),
                                f"{oid}: add it to AVAILABLE_WHEN (and control_availability) or to ALWAYS_AVAILABLE")
        self.assertEqual(set(ha.AVAILABLE_WHEN) & ha.ALWAYS_AVAILABLE, set())
        self.assertEqual((set(ha.AVAILABLE_WHEN) | ha.ALWAYS_AVAILABLE) - controls, set())   # nothing listed that isn't a control
        # each condition, as the web UI shows it
        def fake(mode="l2arc", can_run=True, rate=1e8, libs=()):
            return SimpleNamespace(cache_mode=lambda: mode, feature_available=lambda f: (can_run, ""), l2arc_rate=lambda: rate,
                                   libraries=[{"key": k, "title": k, "type": "show", "kind": "tv"} for k in libs])
        s = copy.deepcopy(config.DEFAULTS)
        s["nowplaying"]["enabled"] = True
        avail = ha.control_availability(fake(), s, {})
        self.assertTrue(avail["nowplaying_enabled"] and avail["nowplaying_ram_percent"] and avail["warmup_pacing_auto"])
        self.assertFalse(avail["warmup_pacing_percent"])                                # automatic pacing, and it works
        self.assertTrue(avail["run_warmup"])
        self.assertFalse(avail["stop_warmup"])
        self.assertFalse(avail["nextepisode_ram_percent"])                              # no library prefetches anything
        self.assertTrue(ha.control_availability(fake(rate=None), s, {})["warmup_pacing_percent"])   # automatic can't work
        s["warmup"]["pacing_auto"] = False
        self.assertTrue(ha.control_availability(fake(), s, {})["warmup_pacing_percent"])
        running = ha.control_availability(fake(), s, {"running": True})
        self.assertEqual((running["run_warmup"], running["stop_warmup"]), (False, True))
        ram = ha.control_availability(fake(mode="ram"), s, {})
        self.assertFalse(ram["warmup_pacing_auto"] or ram["warmup_pacing_percent"] or ram["run_warmup"])   # no L2ARC
        s["nowplaying"]["enabled"] = False
        self.assertFalse(ha.control_availability(fake(), s, {})["nowplaying_ram_percent"])
        off = ha.control_availability(fake(can_run=False), s, {})
        self.assertFalse(off["nowplaying_enabled"] or off["nowplaying_ram_percent"])
        # Prefetch's RAM limit: while something's kept in RAM — whole files, or lead-ins without an L2ARC
        s["nextepisode"]["libraries"] = {"2": {"start": "next", "end": "none"}}
        self.assertFalse(ha.control_availability(fake(libs=["2"]), s, {})["nextepisode_ram_percent"])   # lead-ins on the SSD
        self.assertTrue(ha.control_availability(fake(mode="ram", libs=["2"]), s, {})["nextepisode_ram_percent"])
        s["nextepisode"]["libraries"] = {"2": {"start": "off", "end": "next"}}
        self.assertTrue(ha.control_availability(fake(libs=["2"]), s, {})["nextepisode_ram_percent"])

    def test_without_discovery(self):
        """For Node-RED and the like: just the state, availability and command topics."""
        self.enable()
        self.assertTrue(wait_for(lambda: any(t.startswith("homeassistant/") for t in self.broker.retained)))
        # Turning discovery off removes the Home Assistant entities
        self.app.settings.update({"mqtt": {"discovery": False}})
        self.assertTrue(wait_for(lambda: not any(t.startswith("homeassistant/") for t in self.broker.retained)
                                 and self.app.ha.status["state"] == "connected"))
        self.assertTrue(wait_for(lambda: self.app.ha.status["steps"]["publish"] == {"status": "ok", "text": "plexcache/state"}),
                        self.app.ha.status["steps"]["publish"])
        self.assertIsNotNone(self.state())
        # Plain values work as commands, as well as Home Assistant's labels
        self.assertTrue(wait_for(lambda: any("plexcache/set/#" in c[1] for c in self.broker.clients)))
        self.broker.inject("plexcache/set/poll_seconds", "10")
        self.assertTrue(wait_for(lambda: self.app.settings.section("general")["poll_seconds"] == 10))
        audio = next(l for l in self.app.libraries if l["kind"] == "audio")
        self.broker.inject(f"plexcache/set/library_{audio['key']}_prefetch_end", "rest")
        self.assertTrue(wait_for(lambda: (self.app.settings.section("nextepisode")["libraries"].get(audio["key"]) or {}).get("end") == "rest"))
        self.broker.inject("plexcache/set/nowplaying_enabled", "maybe")
        self.assertTrue(wait_for(lambda: any("send ON or OFF" in e["message"] for e in self.app.activity.snapshot()["events"])))
        self.app.settings.update({"general": {"poll_seconds": 5}, "nextepisode": {"libraries": {}}})

    def test_wrong_password_then_retry(self):
        self.enable(password="wrong")
        self.assertTrue(wait_for(lambda: self.app.ha.status["state"] == "error"))
        self.assertIn("not authorized", self.app.ha.status["error"])
        self.assertEqual(self.steps(), {"broker": "ok", "tls": "off", "login": "fail", "publish": "off"})
        self.assertIsNotNone(self.app.ha.status["next_retry"])
        # Fixed on the broker's side: "Reconnect now" doesn't wait for the next attempt
        self.broker.password = "wrong"
        try:
            self.app.ha.retry()
            self.assertTrue(wait_for(lambda: self.app.ha.status["state"] == "connected", timeout=3))
            self.assertTrue(wait_for(lambda: self.steps()["publish"] == "ok", timeout=3))
            self.assertEqual(self.steps(), {"broker": "ok", "tls": "off", "login": "ok", "publish": "ok"})
            self.assertIn("Signed in as ha", self.app.ha.status["steps"]["login"]["text"])
        finally:
            self.broker.password = "secret"

    def test_settings_change_retries_at_once(self):
        # TLS ticked for a broker that doesn't use it: fails at the TLS step, with a hint
        self.enable(tls=True)
        self.assertTrue(wait_for(lambda: self.steps()["tls"] == "fail"))
        self.assertEqual(self.steps()["broker"], "ok")
        self.assertIn("doesn’t use TLS", self.app.ha.status["error"])
        # Unticking it is picked up straight away, not after the retry delay
        start = time.time()
        self.app.settings.update({"mqtt": {"tls": False}})
        self.assertTrue(wait_for(lambda: self.app.ha.status["state"] == "connected", timeout=4), self.app.ha.status)
        self.assertLess(time.time() - start, 4)
        self.assertIsNone(self.app.ha.status["error"])

    def test_broker_not_found(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens there now
        self.enable(port=port)
        self.assertTrue(wait_for(lambda: self.app.ha.status["state"] == "error"))
        self.assertEqual(self.steps(), {"broker": "fail", "tls": "off", "login": "off", "publish": "off"})
        self.assertIn("nothing is listening", self.app.ha.status["error"])
        # The same failure isn't logged on every retry
        self.app.ha.retry()
        time.sleep(1.5)
        logged = [e for e in self.app.activity.snapshot()["events"] if "nothing is listening" in e["message"]]
        self.assertEqual(len(logged), 1)

    def test_client_details(self):
        from unittest import mock
        from plexcache import mqttclient
        from plexcache.mqttclient import MQTTClient, MQTTError
        real = socket.create_connection

        def fake(open_ports, timeout_on=()):
            def connect(addr, timeout=None):
                if addr[1] in timeout_on:
                    raise socket.timeout()
                if addr[1] in open_ports:
                    return real(("127.0.0.1", self.broker.port), timeout=timeout)
                raise ConnectionRefusedError()
            return connect
        # 8883 closed but 1883 open: the broker runs without TLS
        with mock.patch.object(mqttclient.socket, "create_connection", fake({1883})):
            with self.assertRaises(MQTTError) as cm:
                MQTTClient("nas", 8883, tls=True).open()
        self.assertIn("running without TLS", str(cm.exception))
        # Some other port answers
        with mock.patch.object(mqttclient.socket, "create_connection", fake({8883})):
            with self.assertRaises(MQTTError) as cm:
                MQTTClient("nas", 1234).open()
        self.assertIn("Port 8883 answers", str(cm.exception))
        # No answer at all
        with mock.patch.object(mqttclient.socket, "create_connection", fake(set(), timeout_on={1234, 1883, 8883})):
            with self.assertRaises(MQTTError) as cm:
                MQTTClient("nas", 1234).open()
        self.assertIn("no answer", str(cm.exception))
        with mock.patch.object(mqttclient.socket, "create_connection", mock.Mock(side_effect=OSError(113, "No route to host"))):
            with self.assertRaises(MQTTError) as cm:
                MQTTClient("nas", 1883).open()
        self.assertIn("No route to host", str(cm.exception))
        # Not connected
        c = MQTTClient("x")
        with self.assertRaises(MQTTError):
            c.publish("a", "b")
        with self.assertRaises(MQTTError):
            c.poll(0.1)
        c.close()  # closing twice is fine
        # A QoS 1 message is acknowledged; keep-alive pings go out when due; a bad length is refused
        a, b = socket.socketpair()
        try:
            c = MQTTClient("x", keepalive=0)
            c.sock = a
            body = struct.pack("!H", 3) + b"a/b" + b"\x00\x07" + b"hi"
            b.sendall(bytes([0x32, len(body)]) + body)
            self.assertEqual(c.poll(0.5), [("a/b", b"hi")])
            self.assertEqual(b.recv(4), bytes([0x40, 2, 0, 7]))  # PUBACK for message 7
            c.ping_if_due()
            self.assertEqual(b.recv(2), bytes([0xC0, 0]))
            b.sendall(bytes([0x30, 0xFF, 0xFF, 0xFF, 0xFF, 0x01]))
            with self.assertRaises(MQTTError):
                c.poll(0.5)
            b.close()
            c._buf = b""
            with self.assertRaises(MQTTError):  # the broker hung up
                c.poll(0.5)
        finally:
            a.close()

    def test_over_tls(self):
        tls_broker = fakes.FakeMQTTBroker(tls_dir=tempfile.mkdtemp(dir=TMP))
        try:
            self.app.settings.update({"mqtt": {"enabled": True, "host": "127.0.0.1", "port": tls_broker.port, "username": "ha",
                                               "password": "secret", "tls": True, "verify_tls": False}})
            self.app.ha.start()
            self.assertTrue(wait_for(lambda: self.app.ha.status["steps"]["publish"]["status"] == "ok"), self.app.ha.status)
            self.assertIn("certificate not checked", self.app.ha.status["steps"]["tls"]["text"])
            self.assertTrue(wait_for(lambda: tls_broker.state("plexcache/state") is not None))
            # "Reconnect" while connected starts a fresh connection
            n = len(tls_broker.connects)
            self.app.ha.retry()
            self.assertTrue(wait_for(lambda: len(tls_broker.connects) > n))
        finally:
            self.app.ha.close()
            tls_broker.close()

    def test_tls_mismatch_messages(self):
        """The fake TrueNAS speaks TLS, so it stands in for a TLS-only broker port."""
        from plexcache.mqttclient import MQTTClient, MQTTError
        port = int(TRUENAS.url.rsplit(":", 1)[1])

        def fail(**kw):
            c = MQTTClient("127.0.0.1", port, timeout=3, **kw)
            with self.assertRaises(MQTTError) as cm:
                c.connect()
            return cm.exception
        e = fail(tls=False)  # TLS unticked on a TLS port
        self.assertEqual(e.step, "login")
        self.assertIn("TLS", str(e))
        e = fail(tls=True, verify_tls=True)  # self-signed certificate
        self.assertEqual(e.step, "tls")
        self.assertIn("certificate wasn’t accepted", str(e))
        e = fail(tls=True, verify_tls=False)  # TLS works, but it isn't MQTT
        self.assertEqual(e.step, "login")
        e = MQTTClient("no-such-host.invalid", 1883, timeout=3)
        with self.assertRaises(MQTTError) as cm:
            e.connect()
        self.assertEqual(cm.exception.step, "broker")
        self.assertIn("can’t find", str(cm.exception))

    def test_not_a_broker(self):
        """Something that answers, but not as a broker would, a TLS port that never
        answers, and a message too big for MQTT: each a clear error, never a hang."""
        import socket
        from plexcache import mqttclient
        from plexcache.mqttclient import MQTTClient, MQTTError
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]

        def answer():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                if answer.http:
                    conn.recv(1024)
                    conn.sendall(b"\x40\x02\x00\x01")  # a well-formed packet, but not the login's answer
                answer.open.append(conn)  # (a silent one is kept open: no answer at all)
        answer.http, answer.open = True, []
        threading.Thread(target=answer, daemon=True).start()
        try:
            with self.assertRaises(MQTTError) as cm:
                MQTTClient("127.0.0.1", port, timeout=3).connect()
            self.assertEqual(cm.exception.step, "login")
            self.assertIn("wasn’t from an MQTT broker — check the port", str(cm.exception))
            answer.http = False
            with self.assertRaises(MQTTError) as cm:
                MQTTClient("127.0.0.1", port, timeout=1, tls=True).connect()
            self.assertEqual(cm.exception.step, "tls")
            self.assertIn("no TLS answer on port", str(cm.exception))
        finally:
            srv.close()
            for c in answer.open:
                c.close()
        with mock.patch.object(mqttclient, "MAX_LENGTH", 10):
            with self.assertRaises(ValueError):
                mqttclient._packet(0x30, b"x" * 11)

    def test_reconnect_endpoint(self):
        from plexcache.server import serve
        httpd = serve(self.app, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            self.app.ha.retry_now.clear()
            r = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/mqtt/reconnect", data=b"{}",
                                       method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(r) as resp:
                self.assertEqual(json.loads(resp.read()), {"started": True})
            self.assertTrue(self.app.ha.retry_now.is_set())
        finally:
            httpd.shutdown()
            httpd.server_close()
            self.app.ha.retry_now.clear()

    def test_no_broker_address(self):
        self.app.settings.update({"mqtt": {"enabled": True, "host": ""}})
        self.app.ha.start()
        self.assertTrue(wait_for(lambda: self.app.ha.status["state"] == "error"))
        self.assertIn("No broker", self.app.ha.status["error"])
        self.assertEqual(self.app.ha.status["steps"]["broker"]["status"], "fail")
        self.app.settings.update({"mqtt": {"enabled": False}})
        self.assertTrue(wait_for(lambda: self.app.ha.status["state"] == "off", timeout=3))

    def test_offline_and_reconnect(self):
        self.enable()
        self.assertTrue(wait_for(lambda: self.state() is not None))
        # Settings change: reconnects under the new topic
        self.app.settings.update({"mqtt": {"base_topic": "nas/plexcache"}})
        self.assertTrue(wait_for(lambda: self.broker.state("nas/plexcache/state") is not None))
        self.assertEqual(self.broker.state("plexcache/availability"), b"offline")
        self.assertEqual(self.broker.state("nas/plexcache/availability"), b"online")
        # Home Assistant restarting makes us announce everything again
        n = len(self.broker.published)
        self.broker.inject("homeassistant/status", "online")
        self.assertTrue(wait_for(lambda: sum(t.endswith("/config") for t, _, _ in self.broker.published[n:]) > 60))
        # The connection drops: the broker publishes our last will, and we come back
        n = len(self.broker.published)
        self.broker.drop_clients()
        self.assertIn(("nas/plexcache/availability", b"offline", True), self.broker.published[n:])
        self.assertTrue(wait_for(lambda: self.broker.state("nas/plexcache/availability") == b"online", timeout=15))
        # Shutting down says goodbye
        self.app.ha.close()
        self.assertTrue(wait_for(lambda: self.broker.state("nas/plexcache/availability") == b"offline", timeout=10))
        self.app.settings.update({"mqtt": {"base_topic": "plexcache"}})


class CacheModeTests(unittest.TestCase):
    """L2ARC or RAM only: Default follows the mode; L2ARC-only choices fall back, and Health says so."""

    def test_mode(self):
        from plexcache import cachemode as cm
        s = {"general": {"no_l2arc": False}}
        self.assertEqual(cm.mode(s, "present"), cm.L2ARC)
        self.assertEqual(cm.mode(s, "absent"), cm.RAM)
        self.assertEqual(cm.mode(s, "unknown"), cm.RAM)    # unconfirmed counts as absent
        s["general"]["no_l2arc"] = True
        self.assertEqual(cm.mode(s, "present"), cm.RAM)    # "I won't use an L2ARC"

    def test_default_follows_the_mode_and_fallbacks_are_recorded(self):
        from plexcache import cachemode as cm
        from plexcache.i18n import M
        dyn = cm.Dynamic(l2arc="library", ram="season", l2arc_only=("library",), label=M("Prefetch"),
                         names={"library": "Whole library", "season": "Whole season"})
        fb = cm.Fallbacks()
        self.assertEqual(cm.resolve("x", "default", cm.L2ARC, fb, dyn), "library")
        self.assertEqual(cm.resolve("x", "default", cm.RAM, fb, dyn), "season")
        self.assertEqual(fb.recent(), [])
        self.assertEqual(cm.resolve("x", "next", cm.RAM, fb, dyn), "next")        # not L2ARC-only: used as is
        self.assertEqual(cm.resolve("x", "library", cm.L2ARC, fb, dyn), "library")
        self.assertEqual(fb.recent(), [])
        self.assertEqual(cm.resolve("x", "library", cm.RAM, fb, dyn), "season")   # falls back...
        (f,) = fb.recent()
        self.assertEqual((f["chosen"], f["used"]), ("Whole library", "Whole season"))  # ...and it's noted
        self.assertEqual(fb.recent(now=f["at"] + cm.FALLBACK_KEEP + 1), [])        # forgotten after a day
        cm.resolve("x", "library", cm.RAM, fb, dyn)
        cm.resolve("x", "library", cm.L2ARC, fb, dyn)                              # honoured again: cleared
        self.assertEqual(fb.recent(), [])

    def test_health_lists_fallbacks(self):
        from plexcache import cachemode as cm, checks
        from plexcache.i18n import M

        class FakeApp:
            def __init__(self, no_l2, status):
                self.s = {"general": {"no_l2arc": no_l2}}
                self.status = status
                self.fallbacks = cm.Fallbacks()
                self.settings = self

            def get(self):
                return self.s

            def l2arc_status(self):
                return self.status

        app = FakeApp(False, "present")
        titles = lambda: {en(i["title"]): i["status"] for i in checks.check_cache_mode(app)["items"]}
        self.assertEqual(titles(), {"L2ARC caching": "pass", "Choices that need an SSD cache": "pass", "No sign-in": "info",
                                    "Optimizer": "info"})
        self.assertEqual(en(checks.check_cache_mode(app)["title"]), "Plex Cache")
        app.fallbacks.note("x", M("Prefetch"), "Whole library", "Whole season")
        group = checks.check_cache_mode(app)
        fell = [i for i in group["items"] if en(i["title"]) == "Choices that need an SSD cache"][0]
        self.assertEqual(fell["status"], "warn")
        self.assertIn("warm-up schedule", en(fell["help"]))                # what it's about, behind an ⓘ
        self.assertIn("Prefetch (Whole library → Whole season)", en(fell["detail"]))
        app.status = "unknown"
        self.assertEqual(titles()["L2ARC not checked yet"], "info")

    def test_describe(self):
        from plexcache import cachemode as cm, prefetch  # noqa: F401  (registers the prefetch settings)
        d = cm.describe(cm.RAM, "absent", False, cm.Fallbacks())
        self.assertEqual(d["defaults"]["prefetch.start"], "rest")
        self.assertEqual(d["settings"]["prefetch.end"], {"l2arc": "rest", "ram": "next", "l2arc_only": ["rest"]})
        self.assertEqual(d["mode"], "ram")


class MQTTStandardsTests(unittest.TestCase):
    """The MQTT client follows the client rules of the OASIS MQTT 3.1.1 standard
    (https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html): the [MQTT-x.x.x-x] numbers."""

    def pair(self, **kw):
        from plexcache.mqttclient import MQTTClient
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        c = MQTTClient("broker", **kw)
        c.sock = a
        b.settimeout(2)
        return c, b

    @staticmethod
    def read(sock):
        """One packet from the other end: (first byte, body)."""
        head = sock.recv(1)
        mult, length = 1, 0
        while True:
            byte = sock.recv(1)[0]
            length += (byte & 0x7F) * mult
            mult *= 128
            if not byte & 0x80:
                break
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return head[0], body

    @staticmethod
    def fields(body, i, n):
        out = []
        for _ in range(n):
            size = struct.unpack("!H", body[i:i + 2])[0]
            out.append(body[i + 2:i + 2 + size].decode())
            i += 2 + size
        return out

    def test_connect(self):
        # CONNECT first [MQTT-3.1.0-1]: "MQTT", level 4, the client ID first in the payload [MQTT-3.1.3-3],
        # then will, username, password in that order [MQTT-3.1.3-1]
        c, b = self.pair(client_id="plexcache1a2b", username="ha", password="pw", will=("pc/availability", "offline", True), keepalive=60)
        b.sendall(bytes([0x20, 2, 0, 0]))
        c.login()
        kind, body = self.read(b)
        self.assertEqual(kind, 0x10)
        self.assertEqual(body[:7], b"\x00\x04MQTT\x04")
        flags = body[7]
        self.assertEqual(flags, 0x80 | 0x40 | 0x20 | 0x04 | 0x02)                      # user, password, will retain, will, clean
        self.assertEqual(flags & 0x18, 0)                                                # will QoS 0 [MQTT-3.1.2-14]
        self.assertEqual(struct.unpack("!H", body[8:10])[0], 60)
        self.assertEqual(self.fields(body, 10, 5), ["plexcache1a2b", "pc/availability", "offline", "ha", "pw"])
        # No will: its QoS and retain bits are 0 [MQTT-3.1.2-13, -15]; a password without a username isn't sent [MQTT-3.1.2-22]
        c, b = self.pair(client_id="x", password="pw")
        b.sendall(bytes([0x20, 2, 0, 0]))
        c.login()
        kind, body = self.read(b)
        self.assertEqual(body[7], 0x02)
        self.assertEqual(self.fields(body, 10, 1), ["x"])

    def test_client_id(self):
        # Letters and digits, 1 to 23: what every broker must accept [MQTT-3.1.3-5]
        from plexcache.homeassistant import mqtt_client_id
        import re
        for node in ("plexcache_1a2b3c4d5e6f", "plexcache_x", "plexcache_" + "f" * 40, "", "plexcache_ü-ß"):
            self.assertRegex(mqtt_client_id(node), r"^[0-9A-Za-z]{1,23}$", node)
        self.assertEqual(mqtt_client_id("plexcache_1a2b3c4d5e6f"), "plexcache1a2b3c4d5e6f")

    def test_topic_names_and_filters(self):
        from plexcache.mqttclient import check_filter, check_topic
        for good in ("plexcache/state", "homeassistant/sensor/plexcache_x/health/config", "a", "/"):
            self.assertEqual(check_topic(good), good)
        # Not empty [MQTT-4.7.3-1], no wildcards [MQTT-3.3.2-2], no U+0000 [MQTT-4.7.3-2], at most 65535 bytes [MQTT-4.7.3-3]
        for bad in ("", "a/#", "a/+/b", "a+", "a\x00b", "é" * 40000):
            with self.assertRaises(ValueError, msg=repr(bad[:20])):
                check_topic(bad)
        for good in ("#", "a/#", "+", "+/b", "a/+/c", "homeassistant/status"):
            self.assertEqual(check_filter(good), good)
        # "#" a whole level, at the end [MQTT-4.7.1-2]; "+" a whole level [MQTT-4.7.1-3]
        for bad in ("", "a#", "a/#/b", "a/b#", "a+", "a/+b/c"):
            with self.assertRaises(ValueError, msg=bad):
                check_filter(bad)

    def test_publish_and_subscribe(self):
        c, b = self.pair()
        # QoS 0: no packet identifier [MQTT-2.3.1-5], DUP 0 [MQTT-3.3.1-2]
        c.publish("pc/state", b"{}", retain=True)
        kind, body = self.read(b)
        self.assertEqual(kind, 0x31)
        self.assertEqual(body, b"\x00\x08pc/state{}")
        with self.assertRaises(ValueError):
            c.publish("pc/#", b"x")
        # SUBSCRIBE: flags 0010 [MQTT-3.8.1-1], a non-zero identifier [MQTT-2.3.1-1], at least one filter [MQTT-3.8.3-3]
        c._pid = 65535
        c.subscribe(["pc/set/#", "homeassistant/status"])
        kind, body = self.read(b)
        self.assertEqual(kind, 0x82)
        self.assertEqual(struct.unpack("!H", body[:2])[0], 1)                          # wraps to 1, never 0
        self.assertEqual(body[2:], b"\x00\x08pc/set/#\x00\x00\x14homeassistant/status\x00")
        with self.assertRaises(ValueError):
            c.subscribe([])
        with self.assertRaises(ValueError):
            c.subscribe(["pc/#/x"])

    def test_answers_as_the_qos_says(self):
        from plexcache.mqttclient import MQTTError, _packet
        c, b = self.pair()
        # QoS 1: PUBACK; QoS 2: PUBREC, then PUBCOMP for the PUBREL — each with the same identifier [MQTT-3.3.4-1, MQTT-2.3.1-6]
        b.sendall(_packet(0x32, b"\x00\x01t\x00\x07one") + _packet(0x34, b"\x00\x01t\x00\x08two") + _packet(0x62, b"\x00\x08"))
        self.assertEqual(c.poll(0.5), [("t", b"one"), ("t", b"two")])
        self.assertEqual(self.read(b), (0x40, b"\x00\x07"))
        self.assertEqual(self.read(b), (0x50, b"\x00\x08"))
        self.assertEqual(self.read(b), (0x70, b"\x00\x08"))
        # Both QoS bits set [MQTT-3.3.1-4], ill-formed UTF-8 [MQTT-1.5.3-1] or U+0000 [MQTT-1.5.3-2]: the connection is closed
        for bad in (_packet(0x36, b"\x00\x01t\x00\x01x"), _packet(0x30, b"\x00\x02\xff\xfex"), _packet(0x30, b"\x00\x02a\x00x")):
            c, b = self.pair()
            b.sendall(bad)
            with self.assertRaises(MQTTError):
                c.poll(0.5)
            self.assertIsNone(c.sock)

    def test_keep_alive_and_disconnect(self):
        # A PINGREQ well within the keep alive when nothing else was sent [MQTT-3.1.2-23]
        c, b = self.pair(keepalive=60)
        c._last_send = time.monotonic() - 29
        c.ping_if_due()
        b.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            b.recv(2)
        c._last_send = time.monotonic() - 31
        c.ping_if_due()
        b.settimeout(2)
        self.assertEqual(self.read(b), (0xC0, b""))
        # DISCONNECT, then nothing more [MQTT-3.14.4-1, -2]
        c.close()
        self.assertEqual(self.read(b), (0xE0, b""))
        self.assertEqual(b.recv(10), b"")
        self.assertIsNone(c.sock)

    def test_strings_and_lengths(self):
        from plexcache.mqttclient import _packet, _str
        # UTF-8 strings: no U+0000 [MQTT-1.5.3-2], at most 65535 bytes
        with self.assertRaises(ValueError):
            _str("a\x00")
        with self.assertRaises(ValueError):
            _str("x" * 65536)
        self.assertEqual(_str("é"), b"\x00\x02\xc3\xa9")
        # Remaining Length: 1 to 4 bytes, 7 bits each
        for n, size in ((0, 1), (127, 1), (128, 2), (16383, 2), (16384, 3), (2097151, 3), (2097152, 4)):
            self.assertEqual(len(_packet(0x30, b"x" * n)) - 1 - n, size, n)


class TranslationTests(unittest.TestCase):
    """The catalogs match the texts in the code; untranslated texts fall back to English."""

    @classmethod

    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("i18n_tool", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dev", "i18n.py"))
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)
        problems = []
        cls.keys = cls.tool.all_keys(problems)
        cls.problems = problems

    def test_house_style(self):
        """The texts' style (README.md → Writing the texts): typographic apostrophes everywhere,
        and Title Case for what names a place or an action — page and card titles, and buttons."""
        self.assertEqual([k for k in self.keys if re.search(r"[A-Za-z]'[A-Za-z]", k)], [])  # ’, not '
        small = {"a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet", "as", "at", "by", "in", "of", "on", "to",
                 "up", "via", "from", "with", "into", "per", "vs"}
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "plexcache", "web", "app.js"), encoding="utf-8").read()
        lit = r'"((?:[^"\\]|\\.)+)"'
        names = set(re.findall(r"<h2[^>]*>' \+ H\(" + lit, src)) | set(re.findall(r"heading\(esc\(T\(" + lit, src))
        names |= set(re.findall(r'\bbtn\("[a-z_]+", H\(' + lit, src))
        for m in re.finditer(r'class=\\?"btn[^"\\]*\\?"', src):  # each button's own texts (not its title or label)
            ends = [e for e in (src.find("</button>", m.end()), src.find("</a>", m.end())) if 0 <= e - m.end() < 600]
            seg = re.sub(r'(title|aria-label)="\' \+ [HT]\(' + lit + r"\)", "", src[m.end():min(ends)] if ends else "")
            names |= set(re.findall(r"\b[HT]\(" + lit, seg))
        checks_src = open(os.path.join(root, "plexcache", "checks.py"), encoding="utf-8").read()
        names |= set(re.findall(r'"title": M\(' + lit, checks_src))  # the Health page's cards
        self.assertGreater(len(names), 40)
        for name in names:
            words = name.split()
            for i, w in enumerate(words):
                if not w[0].isalpha() or w[0].isupper() or (0 < i < len(words) - 1 and w in small):
                    continue
                self.fail(f"not Title Case: {name!r} ({w!r})")

    def test_texts_extract_cleanly(self):
        # Notes about helpers called on variables are expected (their texts are marked with N());
        # anything else means a text the tool couldn't read
        self.assertEqual([p for p in self.problems if "of a variable" not in p and "without literal" not in p], [])
        self.assertGreater(len(self.keys), 500)

    # Catalogs list every text the app uses (null until translated: shown in English) and
    # nothing it no longer uses. After changing any wording, run `python3 dev/i18n.py sync`.
    # (PLEXCACHE_I18N_LOOSE=1 relaxes this while wording is still being worked on.)
    STRICT = not os.environ.get("PLEXCACHE_I18N_LOOSE")

    def test_catalogs_consistent(self):
        for code in self.tool.TRANSLATED + list(self.tool.VARIANTS):
            with self.subTest(code):
                problems = self.tool.check_catalog(code, self.keys, self.tool.load(code))
                if not self.STRICT:
                    problems = [p for p in problems if not p.startswith("no longer used")]
                self.assertEqual(problems, [])

    @unittest.skipUnless(STRICT, "PLEXCACHE_I18N_LOOSE is set")
    def test_catalogs_list_every_text(self):
        # After `dev/i18n.py sync` every text is in each catalog, null until translated
        for code in self.tool.TRANSLATED:
            with self.subTest(code):
                missing = [k for k in self.keys if k not in self.tool.load(code)]
                self.assertEqual(missing[:5], [], "run: python3 dev/i18n.py sync")

    def test_untranslated_falls_back_to_english(self):
        from unittest import mock
        from plexcache import i18n
        i18n._catalogs["xx-test"] = {"Idle": None, "{n} files": {"one": None, "other": None}}
        try:
            with mock.patch.object(i18n, "CODES", list(i18n.CODES) + ["xx-test"]):
                self.assertEqual(i18n.render(i18n.M("Idle"), "xx-test"), "Idle")
                self.assertEqual(i18n.render(i18n.M("{n} files", one="{n} file", n=1), "xx-test"), "1 file")
                self.assertEqual(i18n.render(i18n.M("{n} files", one="{n} file", n=3), "xx-test"), "3 files")
        finally:
            i18n._catalogs.pop("xx-test", None)

    def test_helpers(self):
        """Plurals in each language's own way, sizes and durations in the unit that suits them,
        numbers with each language's separators, lists, and messages that keep their template."""
        import copy
        import pickle
        from plexcache import i18n
        from plexcache.i18n import M, Fixed
        plurals = [("ja", 1, "other"), ("fr", 0, "one"), ("fr", 2, "other"), ("ru", 1, "one"), ("ru", 3, "few"), ("ru", 11, "many"),
                   ("ru", 1.5, "other"), ("uk", 22, "few"), ("pl", 1, "one"), ("pl", 23, "few"), ("pl", 25, "many"), ("pl", 2.5, "other"),
                   ("en", 1, "one"), ("en", 1.0, "one"), ("en", 2, "other"), ("en", "x", "other"), ("en", True, "other")]
        for lang, n, want in plurals:
            with self.subTest(lang=lang, n=n):
                self.assertEqual(i18n.plural_category(lang, n), want)
        sizes = [(512, "512 B"), (1500, "1.5 KB"), (41e6, "41 MB"), (1.6e9, "1.6 GB"), (2e12, "2.0 TB")]
        for n, want in sizes:
            self.assertEqual(str(i18n.size_text(n)), want)
        durations = [(0.18, "180 ms"), (2.4, "2.4 s"), (38, "38 s"), (240, "4 min"), (250, "4 min 10 s"), (3600, "1 h"), (3900, "1 h 5 min")]
        for sec, want in durations:
            self.assertEqual(str(i18n.duration_text(sec)), want)
        numbers = [(Fixed(2.0, 1), "de", "2,0"), (1234.5, "en", "1,234.5"), (1234, "es", "1234"), (12345, "es", "12.345"), (0.25, "en", "0.25")]
        for v, lang, want in numbers:
            self.assertEqual(i18n.number(v, lang), want)
        self.assertEqual(i18n.value([1, 2], "ja"), "1、2")
        self.assertEqual((i18n.value(None, "en"), i18n.value(True, "en"), i18n.value(object, "en")[:6]), ("None", "True", "<class"))
        self.assertEqual(i18n.fill("{a} {b}", {"a": 1}, "en"), "1 {b}")                # unknown placeholders stay
        self.assertEqual([str(i18n.join(x)) for x in ([], ["a"], ["a", "b", "c"])], ["", "a", "a, b and c"])
        self.assertEqual(str(i18n.join(["a", "b"], "or")), "a or b")
        # messages: their template kept through a copy or pickle, and as JSON
        m = M("{n} files", one="{n} file", n=1)
        for again in (copy.deepcopy(m), pickle.loads(pickle.dumps(m))):
            self.assertEqual((str(again), again.tmpl, again.one), ("1 file", "{n} files", "{n} file"))
        self.assertEqual(m.json(), {"_t": "{n} files", "_p": {"n": 1}, "_one": "{n} file"})
        self.assertEqual(pickle.loads(pickle.dumps(Fixed(2.25, 2))).places, 2)
        self.assertEqual((i18n.msg(ValueError(m)), i18n.msg(ValueError("a", "b"))), (m, "('a', 'b')"))
        # rendering: a plural translated (or not yet), a plain string; unknown languages are English
        with mock.patch.object(i18n, "lookup", lambda key, lang: {"one": "un fichier", "other": "{n} fichiers"} if key == "{n} files" else "traduit"):
            self.assertEqual(i18n.render(M("{n} files", one="{n} file", n=3), "fr"), "3 fichiers")
            self.assertEqual(i18n.render(M("{n} files", one="{n} file", n=1), "fr"), "un fichier")
            self.assertEqual(i18n.render(M("{n} things", one="{n} thing", n=1), "fr"), "traduit")
        with mock.patch.object(i18n, "lookup", lambda key, lang: None):
            self.assertEqual(i18n.render(M("{n} things", one="{n} thing", n=1), "fr"), "1 thing")
        self.assertEqual((i18n.render(None), i18n.render(5), i18n.render(M("Hi"), "xx")), ("", "5", "Hi"))
        self.assertEqual((i18n.chain("en"), i18n.chain("xx")), ([], []))
        with mock.patch.object(i18n, "I18N_DIR", "/nonexistent"), mock.patch.dict(i18n._catalogs, {}, clear=True):
            self.assertEqual(i18n._load("fr"), {})                                  # a missing catalog: English


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ToolsTests(unittest.TestCase):
    """The Tools pages' tools (tools/): Merge Duplicates, Duplicate Versions, Organize Folders — and
    the trash the file ones set aside into (trash.py)."""

    def _app(self, mirror=False):
        """An app on a Plex of its own (its records get changed), with the fake optimizer for the file
        actions — and, asked, a second Plex with the same films as the mirrored server."""
        from plexcache.optimizing import Optimizing
        d = tempfile.mkdtemp(dir=TMP)
        cat = fakes.make_media(d, movies=4, seasons=1, episodes=1, tracks=1)
        for i, m in enumerate(cat["movies"]):
            m.update(title=f"Film {i}", year=2010 + i, guid=f"plex://movie/{i}", guids=[f"tmdb://{100 + i}"], duration=5400000 + i * 1000)
        plex = fakes.FakePlex(cat)
        self.addCleanup(plex.close)
        app = _new_app(plex={"url": plex.url, "token": "good-token"}, sync={"mirrors": "none"}, optimizer={"convert_hold_days": 0})
        client = FakeOptimizer()
        app.optimizing = Optimizing(app, client)
        other = None
        if mirror:
            cat2 = {"movies": [dict(m) for m in cat["movies"]], "episodes": [], "tracks": [], "tracks_b": [], "root": d}
            other = fakes.FakePlex(cat2, token="other-token")
            self.addCleanup(other.close)
            app.settings.update({"plex2": {"url": other.url, "token": "other-token", "name": "Starlit", "machine_id": "xyz"}, "sync": {"mirrors": "plex2"}})
        app.scan_library()
        from plexcache import safety
        safety.acknowledge_backup(app, "1")  # (the file tools wait for a recent snapshot or a backup you say you made: said here)
        return app, plex, client, cat, other

    def _run(self, tool, started):
        self.assertTrue(started)
        for _ in range(400):
            if not tool.running:
                return tool.snapshot()
            time.sleep(0.05)
        raise AssertionError("the tool didn't finish")

    def test_merge_duplicates(self):
        """The same film on two records (the same match; or unmatched, files named for the same title
        and year): the record with the most edited fields is kept, the others merged into it with Plex's
        own Merge — and the same records on the mirrored server too. No file is touched."""
        from plexcache.tools.merge import group_key
        app, plex, client, cat, other = self._app(mirror=True)
        tool = app.tools["merge"]
        # a second record of Film 1, with its own file; Film 1's first record has edits (kept)
        copy = os.path.join(cat["root"], "Movies", "Film 1 (2011) [copy].mkv")
        fakes.make_mkv(copy, 1024 * 1024)
        for c in (plex.cat, other.cat):
            c["movies"].append({"path": copy, "size": 1, "kbps": 1, "key": "190", "title": "Film 1", "year": 2011, "guid": "plex://movie/1", "duration": 5401000})
        plex.cat["movies"][1]["locked"] = ["title"]
        # unmatched records are the same film when their files are named for the same title and year
        self.assertEqual(group_key({"matched": False, "files": [("/m/The Film (1999) 1080p.mkv", 1)]}), group_key({"matched": False, "files": [("/m/the.film.1999.mkv", 1)]}))
        self.assertNotEqual(group_key({"matched": True, "guid": "a", "files": []}), group_key({"matched": True, "guid": "b", "files": []}))
        snap = self._run(tool, tool.scan_async("1"))
        (row,) = snap["scan"]["rows"]
        self.assertEqual((row["why"], row["keep"]["key"], [o["key"] for o in row["others"]], row["keep"]["locked"]), ("matched", "101", ["190"], ["title"]))
        self.assertEqual(row["id"], "rec:101")
        self.assertEqual((row["kinds"], snap["scan"]["records"]), (["edits"], 5))                     # (the kept record's edit: said as what the other lacks)
        self.assertFalse(tool.apply_async(["nothing"]))
        path1 = cat["movies"][1]["path"]
        snap = self._run(tool, tool.apply_async([row["id"]], {row["id"]: {"keep_key": "190"}}))   # (the arrow: the other record kept instead)
        self.assertEqual((plex.merged, getattr(other, "merged", [])), ([("190", "101")], []))     # (the mirror's records are its own: untouched)
        (res,) = snap["results"]
        self.assertEqual((res["result"], res["merged"]), ("done", 1))
        self.assertEqual(snap["scan"]["rows"], [])                                                    # done: off the list
        self.assertTrue(os.path.exists(copy))
        self.assertEqual(client.sent, [])
        self.assertEqual(sorted(m["key"] for m in plex.cat["movies"]), ["100", "102", "103", "190"])
        self.assertEqual(next(m for m in plex.cat["movies"] if m["key"] == "190")["more"], [path1])  # a version of the kept record now
        # another edition isn't a duplicate (unless it's the very same file); nor is a film of another length
        from plexcache.tools import merge as mg
        rec = lambda key, **kw: dict({"key": key, "files": [("/m/%s.mkv" % key, 100 + int(key))], "durations": [5400000], "edition": "", "locked": []}, **kw)
        self.assertTrue(mg.duplicates(rec("1"), rec("2")))
        self.assertFalse(mg.duplicates(rec("1"), rec("2", edition="Director’s Cut")))
        self.assertTrue(mg.duplicates(rec("1"), rec("2", edition="Director’s Cut", files=[("/m/1.mkv", 101)])))  # (the same file: one film)
        self.assertFalse(mg.duplicates(rec("1"), rec("2", durations=[5400000 + 5 * 60000], files=[("/m/2.mkv", 7)])))
        self.assertTrue(mg.duplicates(rec("1"), rec("2", durations=[5400000 + 20000], files=[("/m/2.mkv", 7)])))     # (a few seconds: fine)
        self.assertEqual([w for w, _ in mg.clusters([rec("1", guid="g", matched=True), rec("2", guid="g", matched=True, durations=[9000000]), rec("3", guid="g", matched=True)])], ["matched"])
        # a work in parts, each part a record, all matched to one entry but titled apart on purpose — kept apart
        parts = [rec("1", guid="g", matched=True, title="Henry IV, Part 1", files=[("/m/Henry IV Part 1 (2012).mkv", 101)]),
                 rec("2", guid="g", matched=True, title="Henry IV Part 2", files=[("/m/Henry IV Part 2 (2012).mkv", 102)])]
        self.assertFalse(mg.duplicates(*parts))
        self.assertEqual(mg.clusters(parts), [])
        # …unless a part is there twice as the very same file: those two are duplicates, the other part still apart
        parts.append(rec("3", guid="g", matched=True, title="Shakespeare’s Globe: Henry IV, Part 2", files=[("/m/Henry IV Part 2 (2012) copy.mkv", 102)]))
        self.assertEqual([(w, [r["key"] for r in c]) for w, c in mg.clusters(parts)], [("file", ["2", "3"])])
        # the very same file (a link, a copy) on two records matched to different entries — or one unmatched: one film
        same = [rec("1", guid="g", matched=True), rec("2", guid="local://2", matched=False, files=[("/other/1.mkv", 101)])]
        self.assertEqual([(w, [r["key"] for r in c]) for w, c in mg.clusters(same)], [("file", ["1", "2"])])
        # only movie and TV libraries
        snap = self._run(tool, tool.scan_async("3"))
        self.assertIn("Only movie and TV", snap["error"])
        # a scan saved by an older version (rows of another shape) is dropped when the app starts
        from plexcache.tools.merge import MergeDuplicates
        app._save_json("tools-merge.json", {"scan": {"library": "1", "rows": [{"id": "x", "keep": {"key": "1"}}]}, "results": []})
        self.assertIsNone(MergeDuplicates(app).scan_result)
        self.assertEqual(MergeDuplicates(app).scan_version, 2)

    def test_duplicate_versions_and_the_trash(self):
        """One record holding the same file twice (the same running time and size): the copy in a folder
        of its own is kept, the other set aside in the library's .plexcache-trash through the optimizer,
        and Plex asked to scan. It's deleted for good once Plex no longer lists it and the hold is over —
        or put back, or kept set aside. Health says when one is held too long."""
        from plexcache import checks, trash as trash_mod
        app, plex, client, cat, _ = self._app()
        tool, tr = app.tools["versions"], app.trash
        m = cat["movies"][0]
        loose = os.path.join(cat["root"], "Movies", "Film 0 (2010).mkv")
        shutil.copy(m["path"], loose)
        m["more"] = [loose]
        os.utime(loose, (time.time(), time.time()))  # (newer: still not kept — the one in its own folder is)
        app.scan_library()
        snap = self._run(tool, tool.scan_async("1"))
        (row,) = snap["scan"]["rows"]
        self.assertEqual((row["keep"]["path"], [x["path"] for x in row["extras"]], row["extras"][0]["linked"], row["keep"]["sub"]), (m["path"], [loose], False, True))
        self.assertEqual((snap["scan"]["items"], snap["scan"]["frees"], snap["scan"]["mirror"]), (1, os.path.getsize(loose), None))  # (one item with several files)
        snap = self._run(tool, tool.apply_async([row["id"]]))
        (res,) = snap["results"]
        self.assertEqual((res["result"], res["set_aside"]), ("done", 1))
        trashed = os.path.join(cat["root"], "Movies", ".plexcache-trash", "Film 0 (2010).mkv")
        self.assertTrue(os.path.exists(trashed) and not os.path.exists(loose))
        self.assertTrue(os.path.exists(os.path.join(cat["root"], "Movies", ".plexcache-trash", ".plexignore")))
        self.assertEqual(client.sent[-1]["action"], "set_aside")
        self.assertEqual(plex.scanned, [os.path.join(cat["root"], "Movies")])
        self.assertIn("PUT /library/sections/1/emptyTrash", plex.requests)
        (e,) = tr.snapshot()["items"]
        self.assertEqual((e["state"], e["trashed"], e["here"]), ("waiting", trashed, False))
        # Plex still lists it: not purged, even with no hold
        tr.tick()
        self.assertEqual((tr.items[0]["state"], os.path.exists(trashed)), ("waiting", True))
        # held too long: Health says so
        tr.items[0]["at"] -= trash_mod.STUCK_AFTER + 1
        tr.items[0]["checked"] = 0
        tr.tick()
        self.assertTrue(tr.items[0]["stuck"])
        self.assertIn("can’t be deleted yet", en(next(i for i in checks._tool_items(app) if "set-aside" in en(i["title"]))["title"]))
        # Plex let go of it (a scan), the hold is 0: purged
        m.pop("more")
        app.scan_library()
        tr.items[0]["checked"] = 0
        tr.tick()
        self.assertEqual((tr.items[0]["state"], os.path.exists(trashed), os.listdir(os.path.dirname(trashed))), ("purged", False, [".plexignore"]))  # (the trash folder stays, empty)
        self.assertEqual(tr.snapshot()["items"], [])
        # a second path Plex doesn't list (a hard link in the library folder; Plex skips a file it already has),
        # and an identical copy: each a row, the listed file kept
        link = os.path.join(cat["root"], "Movies", "Film 2 (2012) link.mkv")
        os.link(cat["movies"][2]["path"], link)
        twin = os.path.join(cat["root"], "Movies", "twin.mkv")
        with open(cat["movies"][3]["path"], "r+b") as fh:  # (the sample films are alike byte for byte: this one made its own)
            fh.seek(-16, os.SEEK_END)
            fh.write(b"FILM3-ITS-OWN-ND")
        shutil.copy(cat["movies"][3]["path"], twin)
        snap = self._run(tool, tool.scan_async("1"))
        by = {r["id"]: r for r in snap["scan"]["rows"]}
        self.assertEqual(sorted(by), ["disk:" + link, "disk:" + twin])
        self.assertEqual((by["disk:" + link]["keep"]["path"], by["disk:" + link]["extras"][0]["linked"], by["disk:" + link]["extras"][0]["unlisted"], by["disk:" + link]["frees"]),
                         (cat["movies"][2]["path"], True, True, 0))
        self.assertEqual((by["disk:" + twin]["keep"]["path"], by["disk:" + twin]["extras"][0]["linked"], by["disk:" + twin]["frees"]), (cat["movies"][3]["path"], False, os.path.getsize(twin)))
        snap = self._run(tool, tool.apply_async(["disk:" + link]))
        self.assertEqual(snap["results"][0]["result"], "done")
        self.assertFalse(os.path.exists(link))
        self.assertTrue(os.path.exists(cat["movies"][2]["path"]))
        os.remove(twin)
        # put back instead
        shutil.copy(m["path"], loose)
        m["more"] = [loose]
        app.scan_library()
        self._run(tool, tool.scan_async("1"))
        self._run(tool, tool.apply_async([row["id"]]))
        self.assertFalse(os.path.exists(loose))
        tr.act(loose, "restore")
        self.assertTrue(os.path.exists(loose))
        self.assertEqual(tr.items[-1]["state"], "restored")
        with self.assertRaises(ValueError):
            tr.act("/nowhere", "keep")

    def test_organize_folders(self):
        """A loose film (in the library folder itself) moved into "Title (Year)" — with its subtitles;
        {edition-…} for an edition; {tmdb-…} only when two films share a title and year — by linking the
        new path first, waiting for Plex to list it, merging the record Plex makes into the original,
        and only then removing the old path. Clean Up renames a film's folder whose name doesn't carry
        the title and year; one that does is left as it is."""
        from plexcache.tools import folders as fo
        app, plex, client, cat, _ = self._app()
        tool = app.tools["folders"]
        self.assertEqual(fo.folder_name({"title": "Alien: Covenant", "year": 2017}), "Alien Covenant (2017)")
        self.assertEqual(fo.folder_name({"title": "Blade Runner", "year": 1982, "edition": "Final Cut"}), "Blade Runner (1982) {edition-Final Cut}")
        self.assertEqual(fo.id_tag({"guids": ["imdb://tt1", "tmdb://55"]}), "{imdb-tt1}")
        self.assertTrue(fo._named_for("Film 2 (2012) Director's Cut", {"title": "Film 2", "year": 2012}))
        self.assertFalse(fo._named_for("Movie & Friends 2 (2012)", {"title": "Film 2", "year": 2012}))
        root = os.path.join(cat["root"], "Movies")
        m = cat["movies"][0]
        loose = os.path.join(root, "Film 0 (2010) 1080p.mkv")
        os.rename(m["path"], loose)
        shutil.rmtree(os.path.dirname(m["path"]))
        m["path"] = loose
        with open(os.path.join(root, "Film 0 (2010) 1080p.en.srt"), "w") as fh:
            fh.write("1\n")
        # Film 3 shares Film 2's title and year: both get their id in the folder name
        cat["movies"][3].update(title="Film 2", year=2012)
        app.scan_library()
        snap = self._run(tool, tool.scan_async("1"))
        self.assertEqual([r["kind"] for r in snap["scan"]["rows"]], ["loose"])                      # (no Clean Up: folders left)
        snap = self._run(tool, tool.scan_async("1", {"cleanup": True}))
        rows = {r["id"]: r for r in snap["scan"]["rows"]}
        self.assertEqual(sorted(r["kind"] for r in rows.values()), ["loose", "rename", "rename", "rename"])
        self.assertEqual(sorted(r["folder"] for r in rows.values() if r["kind"] == "rename"), ["Film 1 (2011)", "Film 2 (2012) {tmdb-102}", "Film 2 (2012) {tmdb-103}"])
        row = rows["loose:" + loose]
        self.assertEqual(row["folder"], "Film 0 (2010)")
        self.assertEqual([(mv["to"], mv["media"]) for mv in row["moves"]],
                         [(os.path.join(root, "Film 0 (2010)", "Film 0 (2010) 1080p.mkv"), True), (os.path.join(root, "Film 0 (2010)", "Film 0 (2010) 1080p.en.srt"), False)])
        new = row["moves"][0]["to"]
        # Plex, asked to scan the new folder, lists the new path as a record of its own
        def on_scan(folder):
            if folder == os.path.dirname(new) and os.path.exists(new) and not any(x["path"] == new for x in cat["movies"]):
                cat["movies"].append({"path": new, "size": m["size"], "kbps": 1, "key": "999", "title": "Film 0", "year": 2010, "guid": m["guid"]})
        plex.on_scan = on_scan
        fo.WAIT, fo.POLL = 5, 0.1
        snap = self._run(tool, tool.apply_async([row["id"]]))
        (res,) = snap["results"]
        self.assertEqual((res["result"], res["moved"], res["merged"]), ("done", 2, True), res)
        self.assertTrue(os.path.exists(new) and os.path.exists(row["moves"][1]["to"]))
        self.assertFalse(os.path.exists(loose) or os.path.exists(os.path.join(root, "Film 0 (2010) 1080p.en.srt")))
        self.assertEqual(plex.merged, [("100", "999")])                                               # the new record folded into the original
        self.assertEqual([x["action"] for x in client.sent], ["link", "link", "unlink", "unlink"])
        self.assertEqual(plex.scanned, [os.path.dirname(new), root])
        self.assertEqual(snap["extra"], {})                                                           # (no mirror: nothing followed)
        # a folder renamed: everything in it moves, and the old folder goes
        row = rows["folder:" + os.path.join(root, "Movie & Friends 1 (2011)")]
        old_folder = os.path.join(root, "Movie & Friends 1 (2011)")
        new1 = row["moves"][0]["to"]
        plex.on_scan = lambda folder: cat["movies"].append({"path": new1, "size": 1, "kbps": 1, "key": "998", "title": "Film 1", "year": 2011}) if folder == os.path.dirname(new1) and not any(x["path"] == new1 for x in cat["movies"]) else None
        snap = self._run(tool, tool.apply_async([row["id"]]))
        self.assertEqual(snap["results"][0]["result"], "done", snap["results"][0])
        self.assertTrue(os.path.exists(new1) and not os.path.exists(old_folder))
        # Plex never lists the new path: undone, the old path untouched
        row = rows["folder:" + os.path.join(root, "Movie & Friends 2 (2012)")]
        plex.on_scan = None
        fo.WAIT = 0.3
        snap = self._run(tool, tool.apply_async([row["id"]]))
        self.assertEqual(snap["results"][0]["result"], "failed")
        self.assertIn("didn’t list", en(snap["results"][0]["reason"]))
        self.assertTrue(os.path.exists(row["moves"][0]["from"]) and not os.path.exists(row["moves"][0]["to"]))
        self.assertEqual(len(snap["scan"]["rows"]), 2)                                                # still to do

    def test_organize_folders_follows_the_mirror(self):
        """With a mirrored server, a moved film is followed there: the synced folder brings the files, and
        once its Plex lists the new path under a record of its own, that record is merged into the old
        one there. Not picked up for days: Health says so."""
        from plexcache import checks
        from plexcache.tools import folders as fo
        app, plex, client, cat, other = self._app(mirror=True)
        tool = app.tools["folders"]
        root = os.path.join(cat["root"], "Movies")
        m = cat["movies"][0]
        loose = os.path.join(root, "Film 0 (2010).mkv")
        os.rename(m["path"], loose)
        shutil.rmtree(os.path.dirname(m["path"]))
        m["path"] = loose
        other.cat["movies"][0]["path"] = loose
        app.scan_library()
        snap = self._run(tool, tool.scan_async("1"))
        (row,) = snap["scan"]["rows"]
        self.assertEqual(snap["scan"]["mirror"], {"name": "Starlit"})
        new = row["moves"][0]["to"]
        plex.on_scan = lambda folder: cat["movies"].append({"path": new, "size": 1, "kbps": 1, "key": "999", "title": "Film 0"}) if folder == os.path.dirname(new) and not any(x["path"] == new for x in cat["movies"]) else None
        fo.WAIT, fo.POLL = 5, 0.1
        snap = self._run(tool, tool.apply_async([row["id"]]))
        self.assertEqual(snap["results"][0]["result"], "done", snap["results"][0])
        (p,) = snap["extra"]["pending"]
        self.assertEqual((p["old"], p["new"], p["title"]), ([loose], [new], "Film 0 (2010)"))
        self.assertTrue(tool.waiting())
        # not there yet: its folder scanned there, and waited for
        tool.tick()
        self.assertEqual(other.scanned, [os.path.dirname(new)])
        self.assertEqual(len(tool.extra["pending"]), 1)
        # the mirror's Plex lists the new path as a record of its own, the old one still there: merged
        other.cat["movies"].append({"path": new, "size": 1, "kbps": 1, "key": "777", "title": "Film 0"})
        tool.tick()
        self.assertEqual(other.merged, [("100", "777")])
        self.assertEqual(len(tool.extra["pending"]), 1)                                               # (the old path still listed there)
        other.cat["movies"][0]["path"] = new
        other.cat["movies"][0].pop("more", None)
        tool.tick()
        self.assertEqual((tool.extra["pending"], tool.results[-1]["mirror"], tool.waiting()), ([], True, False))
        # never picked up: said on Health
        tool.extra["pending"] = [dict(p, at=time.time() - fo.MIRROR_GIVE_UP - 1, scanned=time.time())]
        other.cat["movies"][0]["path"] = loose
        tool.tick()
        self.assertTrue(tool.stuck())
        self.assertIn("hasn’t picked up", en(next(i for i in checks._tool_items(app) if "moved" in en(i["title"]))["title"]))

    def test_unlink_files_and_the_link_check(self):
        """Linked media files — hard links and symlinks — are flagged by Library Health and listed by the
        Unlink Files tool, which makes each a file of its own under the same path (the bytes copied, the
        dates kept); Merge Duplicates marks a row whose files are links."""
        from plexcache import libhealth
        app, plex, client, cat, _ = self._app()
        root = os.path.join(cat["root"], "Movies")
        m1, m2 = cat["movies"][1], cat["movies"][2]
        hard = os.path.join(root, "Film 1 (2011) hard.mkv")
        os.link(m1["path"], hard)
        m1["more"] = [hard]
        soft = os.path.join(root, "Film 2 (2012) soft.mkv")
        os.symlink(m2["path"], soft)
        m2["more"] = [soft]
        app.scan_library()
        # Health: each link said, by kind
        got = libhealth.library_health(app, "1")
        flagged = {r["file"]: r["id"] for it in got["items"] for r in it["results"] if r["id"] == "linked_file"}
        self.assertEqual(set(flagged), {"Movie 1.mkv", "Film 1 (2011) hard.mkv", "Film 2 (2012) soft.mkv"})  # (the original of a hard link is one too)
        # the tool lists them, and makes each a file of its own
        tool = app.tools["unlink"]
        snap = self._run(tool, tool.scan_async("1"))
        kinds = {r["name"]: r["kind"] for r in snap["scan"]["rows"]}
        self.assertEqual(kinds, {"Movie 1.mkv": "hardlink", "Film 1 (2011) hard.mkv": "hardlink", "Film 2 (2012) soft.mkv": "symlink"})
        before = os.stat(hard)
        snap = self._run(tool, tool.apply_async(["link:" + hard, "link:" + soft]))
        self.assertEqual([r["result"] for r in snap["results"]], ["done", "done"])
        after = os.stat(hard)
        self.assertEqual((after.st_nlink, after.st_size, int(after.st_mtime)), (1, before.st_size, int(before.st_mtime)))
        self.assertEqual(os.stat(m1["path"]).st_nlink, 1)                                         # (the original is on its own too now)
        self.assertFalse(os.path.islink(soft))
        self.assertEqual(os.path.getsize(soft), os.path.getsize(m2["path"]))
        self.assertEqual([x["action"] for x in client.sent], ["make_real", "make_real"])
        self.assertEqual(self._run(tool, tool.scan_async("1"))["scan"]["rows"], [])                 # (Movie 1.mkv is on its own too once its twin was copied: none left)
        # Merge Duplicates: a row whose files are links carries the warning
        from plexcache.optimize import files
        self.assertEqual(files.make_real(hard)["already"], True)
        link2 = os.path.join(root, "Film 3 (2013) twin.mkv")
        os.link(cat["movies"][3]["path"], link2)
        cat["movies"].append({"path": link2, "size": cat["movies"][3]["size"], "kbps": 1, "key": "193", "title": "Film 3", "year": 2013, "guid": "plex://movie/3", "duration": cat["movies"][3]["duration"]})
        mg = app.tools["merge"]
        snap = self._run(mg, mg.scan_async("1"))
        row = next(r for r in snap["scan"]["rows"] if r["keep"]["key"] in ("103", "193"))
        self.assertEqual(sorted(l["name"] for l in row["linked"]), ["Film 3 (2013) twin.mkv", "Movie 3.mkv"])

    def test_file_tools_wait_for_room_and_a_snapshot(self):
        """Before a tool changes files: room for what it writes, and a snapshot of the library's dataset
        within the last half hour (or a backup you say you made). Apply is refused until both are so;
        Take a Snapshot makes one through TrueNAS; the acknowledgement counts for the same half hour."""
        from plexcache import safety
        app, plex, client, cat, _ = self._app()
        root = os.path.join(cat["root"], "Movies")
        app.zfs = {"datasets": [{"dataset": "tank/media", "mountpoint": cat["root"], "keys": ["1"]}]}
        tool = app.tools["unlink"]
        link = os.path.join(root, "Film 1 (2011) link.mkv")
        os.link(cat["movies"][1]["path"], link)
        app.scan_library()
        snap = self._run(tool, tool.scan_async("1"))
        ids = [r["id"] for r in snap["scan"]["rows"]]
        self.assertEqual(tool.needed_bytes(snap["scan"]["rows"]), os.path.getsize(link))         # (Plex lists one path of the pair: one copy to write)
        app._backup_acks = {}                                                                    # (no backup said, no snapshot anywhere)
        app._save_json(safety.ACKS, {})
        # no snapshot anywhere: not ready, Apply refused
        r = safety.readiness(app, "1", 1000)
        self.assertEqual((r["space"]["status"], r["snapshot"]["status"], r["ready"], r["can_snapshot"]), ("ok", "warn", False, True))
        with self.assertRaises(ValueError) as cm:
            tool.apply_async(ids)
        self.assertIn("No snapshot", en(cm.exception.args[0]))
        # not enough room: said, and refused
        with mock.patch.object(safety.os, "statvfs", lambda p: types.SimpleNamespace(f_bavail=10, f_frsize=4096)):
            r = safety.readiness(app, "1", 1000)
            self.assertEqual((r["space"]["status"], r["ready"]), ("warn", False))
            self.assertIn("make room", en(r["space"]["text"]))
        # a snapshot taken through TrueNAS: green, named for what it's for, and deleted by the app after its two weeks
        made = safety.take_snapshot(app, "1", "unlink")
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].startswith("tank/media@plexcache-unlink-movies-"), made[0])
        self.assertEqual([s["tool"] for s in safety.taken(app)], ["unlink"])
        self.assertEqual(safety.prune_snapshots(app), 0)                                           # (fresh: kept)
        app._snapshots[0]["at"] -= safety.LIFE + 1
        self.assertEqual(safety.prune_snapshots(app), 1)
        self.assertEqual((safety.taken(app), TRUENAS.snapshots.get("tank/media")), ([], []))
        safety.take_snapshot(app, "1", "unlink")
        r = safety.readiness(app, "1", 1000)
        self.assertEqual((r["snapshot"]["status"], r["ready"]), ("ok", True))
        self.assertIn("minutes ago", en(r["snapshot"]["text"]))
        # an old snapshot alone doesn't do; "I've made a manual backup" does, for half an hour (grey)
        TRUENAS.snapshots = {"tank/media": [("old", time.time() - 2 * safety.FRESH)]}
        self.assertEqual(safety.readiness(app, "1")["snapshot"]["status"], "warn")
        safety.acknowledge_backup(app, "1")
        r = safety.readiness(app, "1")
        self.assertEqual((r["snapshot"]["status"], r["snapshot"]["ack"], r["snapshot"]["how"], r["ready"]), ("off", True, "manual", True))
        safety.acknowledge_backup(app, "1", "trash")                                             # (the action's own set-aside copies will do)
        self.assertIn("copies set aside", en(safety.readiness(app, "1")["snapshot"]["text"]))
        self.assertEqual(safety.readiness(app, "1", "largest")["space"]["needed"], max(f.size for f in app.files if f.library_key == "1"))
        app._backup_acks = None                                                                  # (a restart: kept on disk)
        self.assertTrue(safety.readiness(app, "1")["ready"])
        # both TrueNAS snapshot APIs: pool.snapshot.* (25.04 and later) is tried first, and zfs.snapshot.* (before)
        # when TrueNAS says the first doesn't exist; the one that worked is remembered
        self.assertIn("pool.snapshot.create", TRUENAS.snapshot_calls)
        self.assertNotIn("zfs.snapshot.create", TRUENAS.snapshot_calls)
        TRUENAS.snapshot_api, TRUENAS.snapshot_calls, app._snapshot_api = "zfs.snapshot", [], None
        safety.take_snapshot(app, "1", "unlink")
        self.assertEqual(TRUENAS.snapshot_calls, ["pool.snapshot.create", "zfs.snapshot.create"])
        self.assertEqual(app._snapshot_api, "zfs.snapshot")
        safety.take_snapshot(app, "1", "unlink")
        self.assertEqual(TRUENAS.snapshot_calls[-1], "zfs.snapshot.create")                      # (remembered: not tried again)
        TRUENAS.snapshot_api, app._snapshot_api = "pool.snapshot", None
        # a snapshot TrueNAS refuses: said with the lights until the next one is taken (or dismissed), and logged
        TRUENAS.snapshot_refused = "not allowed for this key"
        before = len(app.activity.events)
        with self.assertRaises(ValueError):
            safety.take_snapshot(app, "1", "unlink")
        r = safety.readiness(app, "1")
        self.assertIn("not allowed for this key", en(r["error"]["text"]))
        logged = [e for e in list(app.activity.events)[before:]]
        self.assertEqual([(e["kind"], e["level"]) for e in logged], [("tools", "warn")])
        self.assertIn("snapshot not taken", en(logged[0]["message"]))
        TRUENAS.snapshot_refused = ""
        safety.take_snapshot(app, "1", "unlink")
        self.assertIsNone(safety.readiness(app, "1")["error"])
        self.assertIn("snapshot taken", en(list(app.activity.events)[-1]["message"]))
        # the Backup preference (System → Preferences): "copies" — the action's own set-aside copies do, where it keeps some
        TRUENAS.snapshots = {"tank/media": []}
        app._backup_acks = {}
        app.settings.update({"general": {"backup": "copies", "snapshot_days": 1}})
        r = safety.readiness(app, "1", 0, keeps=True)
        self.assertEqual((r["snapshot"]["status"], r["snapshot"]["how"], r["ready"], r["backup"], r["life_days"]), ("off", "trash", True, "copies", 1))
        self.assertIn("Preferences", en(r["snapshot"]["text"]))
        self.assertFalse(safety.readiness(app, "1", 0, keeps=False)["ready"])                   # (an action that keeps none: a snapshot or a backup)
        app.settings.update({"general": {"backup": "snapshot", "snapshot_days": 14}})
        self.assertFalse(safety.readiness(app, "1", 0, keeps=True)["ready"])
        app._backup_acks = None
        # the dataset's own .zfs/snapshot folder tells too, with no API
        app.settings.update({"truenas": {"api_key": ""}})
        app._backup_acks = {}
        self.assertEqual(safety.readiness(app, "1")["snapshot"]["status"], "warn")
        sdir = os.path.join(cat["root"], ".zfs", "snapshot", "auto-now")
        os.makedirs(sdir)
        r = safety.readiness(app, "1")
        self.assertEqual((r["snapshot"]["status"], r["snapshot"]["name"], r["can_snapshot"]), ("ok", "auto-now", False))
        with self.assertRaises(ValueError):
            safety.take_snapshot(app, "1")                                                       # (no API: make one yourself)
        snap = self._run(tool, tool.apply_async(ids))                                              # (ready now: it goes)
        self.assertEqual([x["result"] for x in snap["results"]], ["done"])
        # the page: every tool that changes files passes the gate (its Apply goes through toolApply, which holds the lights)
        js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plexcache", "web", "app.js"), encoding="utf-8").read()
        from plexcache import peers
        for name in peers.FILE_TOOLS:
            self.assertIn(f'toolApply("{name}"', js, f"{name}'s page doesn't apply through toolApply (the lights)")
            self.assertRegex(js, r"var FILE_TOOLS = .*\b" + name + r": function", f"{name} isn't among the page's FILE_TOOLS: its Apply wouldn't wait for the lights")
        self.assertIn("if (!gate.ready) u.confirm = false;", js)
        self.assertRegex(js, r'readinessPanel\(key, "largest", "optimizer"')                       # (Optimize Now)
        self.assertRegex(js, r'readinessPanel\(String\(st\.library\), 0, "dataset"')             # (Start Copying, on the dataset page)
        self.assertEqual(app.tools["versions"].keeps != "", True)
        # the lights say which backup stands in, link to the preference, and hold the Take a Snapshot button and the kept error
        panel = js[js.index("  function readinessPanel("):]
        panel = panel[:panel.index("\n  function ", 10)]
        self.assertIn('href=\\"/system#prefs\\" data-nav', panel)
        self.assertIn('data-act="tool-backup" data-how="snapshot"', panel)
        self.assertIn('data-act="tool-backup" data-how="manual"', panel)
        self.assertIn('data-how="dismiss"', panel)
        self.assertNotIn("tool-fallback", js)                                                      # (no choice on the page itself any more)
        self.assertIn('id="prefs"', js)
        for key in ("page_size", "backup", "snapshot_days"):
            self.assertIn(f'"general", "{key}"', js)

    def test_tool_api_and_state(self):
        """The Tools API: a scan and an apply started through it, each tool's state in the app's state,
        and a set-aside file acted on."""
        app, plex, client, cat, _ = self._app()
        from plexcache.server import serve
        srv = serve(app, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        got = post("/api/tools/merge", {"action": "scan", "library": "1"})
        self.assertTrue(got["started"])
        self._run(app.tools["merge"], True)
        st = json.loads(urllib.request.urlopen(base + "/api/state", timeout=10).read())
        self.assertEqual(sorted(st["tools"]), ["folders", "merge", "unlink", "versions"])
        self.assertEqual((st["tools"]["merge"]["scan"]["library"], st["trash"]["items"], st["trash"]["hold_days"]), ("1", [], 0))
        with self.assertRaises(urllib.error.HTTPError):
            post("/api/tools/nothing", {"action": "scan", "library": "1"})
        with self.assertRaises(urllib.error.HTTPError):
            post("/api/trash", {"path": "/nowhere", "action": "keep"})
        self.assertFalse(post("/api/tools/merge", {"action": "apply", "ids": []})["started"])

    def test_library_sync_apply_and_undo(self):
        """Library Sync, applying: the differences ticked made true on the other server — edited fields
        (set and locked as the source has them), the match, a poster chosen by hand (uploaded),
        collections, several items there merged into one — only with the mode "apply" and after a
        review; each change logged with what the item had before, and undone from the log."""
        from plexcache import sync as sync_mod
        app, plex, client, cat, other = self._app(mirror=True)
        sy = app.sync
        # this server (the source): Film 1's title edited and in a collection; Film 2 matched to another entry;
        # Film 3's poster chosen by hand; Film 0 one record here, two on the mirror
        cat["movies"][1].update(title="Film One", locked=["title"], collections=["Best"])
        other.cat["movies"][2]["guid"] = "plex://movie/other"
        cat["movies"][3]["locked"] = ["thumb"]
        copy = os.path.join(cat["root"], "Movies", "Film 0 (2010) [copy].mkv")
        fakes.make_mkv(copy, 1024 * 1024)
        cat["movies"][0]["more"] = [copy]
        other.cat["movies"].append({"path": copy, "size": 1, "kbps": 1, "key": "190", "title": "Film 0", "year": 2010, "guid": "plex://movie/0", "duration": 5400000})
        app.settings.update({"sync": {"libraries": {"2": False, "3": False, "4": False, "5": False}}})  # (the films only)
        app.scan_library()
        sy.compare()
        rows = {r["title"]: r for r in sy.report["libraries"]["1"]["items"]}
        self.assertEqual(sorted((t, r["shape"], r["kinds"]) for t, r in rows.items()),
                         [("Film 0 (2010)", "split", ["grouping"]), ("Film 2 (2012)", "pair", ["match"]), ("Film 3 (2013)", "pair", ["art"]), ("Film One (2011)", "pair", ["edits", "collections"])])
        # not without the mode, nor before a review
        with self.assertRaises(ValueError):
            sy.applier.apply_async("1", [rows["Film One (2011)"]["id"]])
        app.settings.update({"sync": {"mode": "apply"}})
        self.assertIn("reviewed", en(sy.applier.allowed()))
        sy.approve()
        self.assertIsNone(sy.applier.allowed())
        self.assertFalse(sy.applier.apply_async("1", ["nothing"]))
        self.assertTrue(sy.applier.apply_async("1", [r["id"] for r in rows.values()]))
        for _ in range(400):
            if not sy.applier.running:
                break
            time.sleep(0.05)
        snap = sy.snapshot()["apply"]
        self.assertFalse(snap["running"])
        log = {e["title"]: e for e in snap["log"]}
        self.assertEqual({t: (e["result"], e["done"]) for t, e in log.items()},
                         {"Film 0 (2010)": ("done", ["grouping"]), "Film 2 (2012)": ("done", ["match"]), "Film 3 (2013)": ("done", ["art"]), "Film One (2011)": ("done", ["edits", "collections"])})
        puts = [(p, q) for p, q in other.puts]
        self.assertIn(("/library/sections/1/all", {"type": "1", "id": "101", "title.value": "Film One", "title.locked": "1"}), puts)
        self.assertIn(("/library/sections/1/all", {"type": "1", "id": "101", "collection[0].tag.tag": "Best"}), puts)
        self.assertIn(("/library/metadata/102/match", {"guid": "plex://movie/2", "name": "Film 2 (2012)"}), puts)
        self.assertEqual(other.posts, [("/library/metadata/103/posters", b"IMG")])
        self.assertEqual(other.merged, [("100", "190")])
        self.assertEqual((other.cat["movies"][1]["title"], other.cat["movies"][1]["locked"], other.cat["movies"][2]["guid"]), ("Film One", ["title"], "plex://movie/2"))
        self.assertEqual(sy.report["libraries"]["1"]["items"], [])                                  # applied: off the report
        self.assertEqual(sum(sy.report["totals"].values()), 0)
        self.assertEqual(log["Film 3 (2013)"]["before"]["art"], {"thumb": "upload://posters/old"})
        self.assertIn("can’t be undone", en(log["Film 0 (2010)"]["notes"][0]))
        # compared again: in step (but Film 0's merge, which the fake's listing shows as the other's own record now)
        sy.compare()
        self.assertEqual([r["title"] for r in sy.report["libraries"]["1"]["items"]], [])
        # undone: the title back, unlocked; the collection taken off; the match back; the poster chosen before chosen again
        other.puts = []
        for e in log.values():
            sy.applier.undo(e["id"])
        puts = [(p, q) for p, q in other.puts]
        self.assertIn(("/library/sections/1/all", {"type": "1", "id": "101", "title.value": "Film 1", "title.locked": "0"}), puts)
        self.assertIn(("/library/sections/1/all", {"type": "1", "id": "101", "collection[].tag.tag-": "Best"}), puts)
        self.assertIn(("/library/metadata/102/match", {"guid": "plex://movie/other", "name": "Film 2 (2012)"}), puts)
        self.assertIn(("/library/metadata/103/poster", {"url": "upload://posters/old"}), puts)
        self.assertEqual((other.cat["movies"][1]["title"], other.cat["movies"][1]["locked"]), ("Film 1", []))
        undone = {e["title"]: e for e in sy.snapshot()["apply"]["log"]}
        self.assertTrue(all(e["undone"] for e in undone.values()))
        self.assertIn("split the item", en(undone["Film 0 (2010)"]["left"][0]))
        with self.assertRaises(ValueError):
            sy.applier.undo(log["Film One (2011)"]["id"])                                          # (already undone)
        sy.compare()  # (the poster chosen again counts as chosen by hand there too: no art difference any more)
        self.assertEqual(sorted(r["title"] for r in sy.report["libraries"]["1"]["items"]), ["Film 2 (2012)", "Film One (2011)"])

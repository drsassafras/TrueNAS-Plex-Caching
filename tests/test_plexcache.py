import datetime as dt
import json
import logging
import os
import shutil
import struct
import sys
import tempfile
import socket
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

TMP = tempfile.mkdtemp(prefix="plexcache-test-")
# Never ask GitHub for the latest version while testing (an app started by a test would, after 30 s).
os.environ["PLEXCACHE_UPDATE_SOURCE"] = "http://127.0.0.1:9/never"
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
        # A minimum above a library's lead-in length uses the lead-in length (warmup.plan)
        s.update({"warmup": {"movie_min_sec": 100}})
        self.assertEqual(s.section("warmup")["movie_min_sec"], 100)

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
        with self.assertRaisesRegex(PlexError, "Can't reach"):
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
        self.assertEqual(props, {"primarycache": "metadata", "secondarycache": "all", "recordsize": 131072})

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
        p = plan(self._stats(), w, 960 * 10 ** 9)
        self.assertEqual(p["status"], "fits")
        self.assertAlmostEqual(p["need_by_kind"]["movie"] / 1e9, 18000 * 125 * 60 * 1240 * 1.05 / 1e9)
        p = plan(self._stats(), w, 400 * 10 ** 9)
        self.assertEqual(p["status"], "reduced")
        self.assertLess(p["seconds"]["1"], 60)                     # seconds are per library now
        self.assertGreaterEqual(p["seconds"]["1"], 3)
        planned = sum(p["planned_by_kind"].values())
        self.assertLessEqual(planned, 400 * 10 ** 9 - 100 * 10 ** 9 + 1)
        self.assertEqual(plan(self._stats(), w, 0)["status"], "none")
        self.assertEqual(plan(self._stats(), w, None)["status"], "unknown")
        self.assertEqual(plan(self._stats(), w, 101 * 10 ** 9)["status"], "insufficient")
        # Each library's own seconds, and only the libraries warm-up covers
        w2 = dict(w, libraries={"1": {"seconds": 30}})
        p = plan(self._stats(), w2, 960 * 10 ** 9)
        self.assertAlmostEqual(p["need_by_kind"]["movie"] / 1e9, 18000 * 125 * 30 * 1240 * 1.05 / 1e9)
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

    def test_mp4(self):
        from plexcache.layout import tail_ranges
        size = 40 * MiB
        p = os.path.join(self.dir, "a.mp4")
        m = fakes.make_mp4(p, size, moov_size=300_000)
        ranges, info = tail_ranges(p, size, 4 * MiB)
        self.assertEqual((info["index_at"], ranges), ("end", [(m["moov_at"], 300_000)]))
        fakes.make_mp4(p, size, moov_size=300_000, faststart=True)
        ranges, info = tail_ranges(p, size, 4 * MiB)
        self.assertEqual((info["index_at"], ranges), ("start", []))

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

    def test_plan_counts_indexes(self):
        from plexcache.warmup import DEFAULT_INDEX
        stats = LibraryStats(count={"movie": 1000, "tv": 0, "audio": 0}, avg_kbps={"movie": 20000, "tv": 0, "audio": 0},
                             libraries=[{"key": "1", "name": "Movies", "kind": "movie", "count": 1000, "avg_kbps": 20000}])
        w = dict(config.DEFAULTS["warmup"])
        with_idx = plan(stats, w, None)
        self.assertEqual(with_idx["need_max"], with_idx["need_by_kind"]["movie"] + 1000 * DEFAULT_INDEX["movie"])  # always counted
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

    def test_health_results(self):
        h = self.app.health
        groups = {g["id"]: g for g in h["groups"]}
        media = {r["library"]: r for r in groups["media"]["table"]}
        self.assertEqual(media["Movies"]["status"], "pass")
        # Music is ticked (wanted) but its folder isn't mounted: an error, fixed in Settings
        self.assertEqual(media["Music"]["status"], "fail")
        self.assertIn("/music", media["Music"]["detail"])
        self.assertIn("System → Media mappings", media["Music"]["fix"])
        self.assertEqual(groups["media"]["items"], [])
        # one row per dataset, naming the libraries on it
        rows = {r["dataset"]: r for r in groups["datasets"]["datasets"]}
        self.assertEqual(set(rows), {"tank/plexfiles", TV_DS})
        self.assertEqual(rows["tank/plexfiles"]["libraries"], ["Movies", "Audiobooks"])
        self.assertEqual(rows["tank/plexfiles"]["in_use"], {"status": "pass", "text": "Movies, Audiobooks"})
        self.assertEqual(rows[TV_DS]["libraries"], ["TV Shows"])
        self.assertEqual(rows["tank/plexfiles"]["cache"]["status"], "fail")
        self.assertEqual(rows["tank/plexfiles"]["recordsize"]["status"], "warn")
        self.assertEqual(rows[TV_DS]["cache"]["status"], "pass")
        self.assertEqual(rows[TV_DS]["recordsize"], {"status": "pass", "text": "1M"})
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
            row = z["Media datasets: not checked yet"]
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
            row = z[f"Cache settings for {TV_DS}: couldn't read"]
            self.assertEqual(row["status"], "info")
            self.assertIn(f"sudo zfs get primarycache,secondarycache,recordsize {TV_DS}", row["fix_cmd"])
            self.assertEqual({r["cache"]["text"] for r in g["datasets"]}, {"Couldn't read"})
            recs = {r["feature"]: r["verdict"] for r in checks.recommendations(self.app)}
            self.assertNotEqual(recs["nowplaying"], "avoid")
            self.assertNotEqual(recs["nextepisode"], "avoid")
            self.assertTrue(self.app.library_cacheable("1"))      # unknown isn't treated as wrong
        finally:
            self.app.zfs = saved

    def test_recordsize_and_l2arc_settings(self):
        from plexcache import checks
        groups = {g["id"]: g for g in checks.run_all(self.app)["groups"]}  # fresh, not left over from another test
        z = dict(dataset_fixes(groups["datasets"]), **{i["title"]: i for i in groups["zfs"]["items"]})
        rs = z["Record size for tank/plexfiles"]
        self.assertEqual(rs["status"], "warn")                       # parent dataset is 128K, TV is 1M
        self.assertIn("recordsize=128K (Movies and Audiobooks)", rs["detail"])
        self.assertEqual("sudo zfs set recordsize=1M tank/plexfiles", rs["fix_cmd"])
        self.assertIn("RAM", rs["detail"])
        self.assertNotIn(f"Record size for {TV_DS}", z)
        # 182 MB/s default guess? pacing 10% of hdd speed vs 32 MiB/s fill: fine
        self.assertEqual(z["L2ARC settings"]["status"], "pass")
        self.assertIn("32 MiB/s", z["L2ARC settings"]["detail"])

    def test_l2arc_setting_problems(self):
        from plexcache import checks
        params = {"l2arc_noprefetch": "1", "l2arc_write_max": str(8 * 1048576),
                  "l2arc_rebuild_enabled": "0", "l2arc_mfuonly": "1"}
        # Automatic L2ARC pacing reads just under the fill rate, so there's nothing to warn about
        auto = {i["title"]: i for i in checks._l2arc_tuning_checks(self.app, params)}
        self.assertNotIn("L2ARC fills slower than warm-up reads", auto)
        self.app.settings.update({"warmup": {"pacing_auto": False, "pacing_percent": 50}, "general": {"hdd_mb_per_sec": 150}})
        try:
            items = {i["title"]: i for i in checks._l2arc_tuning_checks(self.app, params)}
        finally:
            self.app.settings.update({"warmup": {"pacing_auto": True, "pacing_percent": 10}})
        self.assertIn("L2ARC only keeps frequently used data", items)
        self.assertEqual("echo 0 | sudo tee /sys/module/zfs/parameters/l2arc_mfuonly", items["L2ARC only keeps frequently used data"]["fix_cmd"])
        self.assertIn("L2ARC is emptied on every reboot", items)
        slow = items["L2ARC fills slower than warm-up reads"]
        self.assertIn("8 MiB/s", slow["detail"])
        self.assertIn("75 MB/s", slow["detail"])
        self.assertIn("about 5%", slow["fix"])
        self.assertIn("automatic L2ARC pacing", slow["fix"])
        self.assertIn("Init/Shutdown Scripts", slow["fix"])
        self.assertEqual(checks._recordsize_check(self.app, "tank/m", 1048576, {"l2_total": 960 * 10 ** 9}), ("pass", []))
        # Movies and TV need 1M; audio-only datasets may use 512K or 1M
        self.assertEqual(checks._recordsize_check(self.app, "tank/m", 512 * 1024, None, kinds=["audio"]), ("pass", []))
        self.assertEqual(checks._recordsize_check(self.app, "tank/m", 1024 * 1024, None, kinds=["audio"]), ("pass", []))
        st, its = checks._recordsize_check(self.app, "tank/m", 512 * 1024, None, kinds=["movie", "audio"])
        self.assertEqual((st, its[0]["fix_cmd"]), ("warn", "sudo zfs set recordsize=1M tank/m"))
        self.assertIn("rewrite_blocks.py tank/m", its[1]["fix_cmd"])  # then the files already there
        st, its = checks._recordsize_check(self.app, "tank/m", 256 * 1024, None, kinds=["audio"])
        self.assertEqual((st, its[0]["fix_cmd"]), ("warn", "sudo zfs set recordsize=512K tank/m"))

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
        self.assertIn("tank/spare isn't used by any Plex library", dataset_fixes(groups["datasets"]))

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
        self.app.settings.update({"warmup": {"libraries": {tv: {"seconds": 3}}, "tv_min_sec": 3}})
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
        self.assertTrue(any("TV episodes: 6 of 6 had one at the end" in m for m in msgs), msgs[:5])
        self.assertAlmostEqual(self.app.index_avg()["tv"], 20_012)
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
            self.assertEqual(sum("isn't found here" in en(e) for e in self.events()), 1)
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
            self.assertEqual(jobs("m1")[0].length, 1024 * 1024)                        # 10 s, rounded up to a whole MiB
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
            self.assertEqual((en(fb["chosen"]), en(fb["used"])), ("All the author's books", "Rest of the book"))
            # Within the RAM limit, in order: here only the first fits
            self.app.cachelog.__init__()
            real = app.ram_budget
            app.ram_budget = lambda: dict(real(), nextepisode=1024 * 1024)
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
        self.assertTrue(any("isn't found here" in en(m) for m in self.events()))

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
            for boom, expect in ((PlexError("Plex is down"), "can't read Plex sessions"), (ValueError("bad"), "unexpected error")):
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
        self.assertEqual("Last 7 days, 2 streams: 1 of 1 skip came from cache.",
                         r["verdict"]["text"])
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
                     "Cached data isn't staying in RAM (40% still cached)"):
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
        self.assertEqual(r["verdict"], {"status": "ok", "text": "Last 7 days, 1 stream, and caching had nothing to fix."})

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

    def test_sample_files_from_folders(self):
        from plexcache.app import _sample_files
        d = tempfile.mkdtemp(dir=TMP)
        for sub in ("A", "B", ".hidden"):
            os.makedirs(os.path.join(d, sub))
            for i in range(4):
                open(os.path.join(d, sub, f"f{i}.flac"), "w").close()
        got = _sample_files([d], 5)
        self.assertEqual(len(got), 5)
        self.assertFalse(any("/.hidden/" in p for p in got))
        self.assertEqual(len(_sample_files([d], 100, max_seen=3)), 4)  # stops looking after a folder passes the cap

    def test_health_compares_files_with_the_setting(self):
        from plexcache import checks
        app = App(tempfile.mkdtemp(dir=TMP))
        old = {"checked": 300, "single": 20, "sizes": {str(128 * 1024): 180, str(1024 * 1024): 100}}
        st, its = checks._recordsize_check(app, "tank/tv", 1024 * 1024, None, "TV", ["tv"], old)
        self.assertEqual((st, its[0]["title"]), ("warn", "Older files on tank/tv use smaller blocks"))
        self.assertIn("In a random sample of 300 files, about 64% were written with 128K blocks", its[0]["detail"])
        self.assertEqual(its[0]["fix_cmd"], "curl -fsSO {APP}/tools/rewrite_blocks.py && sudo python3 rewrite_blocks.py tank/tv")
        self.assertEqual(checks._blocks_note(old, 1024 * 1024), " · ~64% of a sample at 128K")
        # a few stragglers: a note, not a warning
        few = {"checked": 300, "single": 0, "sizes": {str(128 * 1024): 3, str(1024 * 1024): 297}}
        self.assertEqual(checks._recordsize_check(app, "tank/tv", 1024 * 1024, None, "", ["tv"], few)[0], "info")
        # audio written at 512K is fine on an audio-only dataset, even if it's now 1M
        music = {"checked": 300, "single": 50, "sizes": {str(512 * 1024): 250}}
        self.assertEqual(checks._recordsize_check(app, "tank/music", 1024 * 1024, None, "", ["audio"], music), ("pass", []))
        self.assertEqual(checks._blocks_note(music, 512 * 1024), " · 300 sampled files match")
        self.assertEqual(checks._blocks_note(None, 1024 * 1024), "")
        self.assertEqual(checks._blocks_note({"checked": 5, "single": 5, "sizes": {}}, 1024 * 1024), "")  # nothing to tell
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
        # Health words a full count differently from a sample, and names a few of the files
        full = {"checked": 1000, "single": 0, "sizes": {"131072": 600, "1048576": 400}, "full": True,
                "examples": {"131072": ["TV/a.mkv", "TV/b.mkv"]}}
        self.assertEqual(checks._blocks_note(full, 1024 * 1024), " · 600 of 1,000 files at 128K")
        self.assertEqual(checks._blocks_note(dict(full, sizes={"1048576": 1000}), 1024 * 1024), " · all 1,000 files match")
        st, its = checks._recordsize_check(App(tempfile.mkdtemp(dir=TMP)), "tank/tv", 1024 * 1024, None, "", ["tv"], full)
        self.assertIn("Of all 1,000 files, 600 (60%) were written with 128K blocks", its[0]["detail"])
        self.assertIn("For example: TV/a.mkv, TV/b.mkv.", its[0]["detail"])
        # through the app: started from the API, kept across a restart, and used by Health
        app = _new_app()
        app.scan_library()
        app.run_checks()
        self.assertFalse(app.run_block_scan("no/such"))
        self.assertTrue(app.run_block_scan(TV_DS))
        self.assertTrue(wait_for(lambda: app.block_scans[TV_DS].get("complete"), 20))
        self.assertTrue(wait_for(lambda: not app.checking, 20))
        self.assertGreater(app.state()["block_scans"][TV_DS]["checked"], 0)
        again = App(app.config_dir)
        self.assertTrue(again.block_scans[TV_DS]["complete"])
        rows = {r["dataset"]: r for g in app.health["groups"] if g["id"] == "datasets" for r in g["datasets"]}
        self.assertIn("files", rows[TV_DS]["recordsize"]["text"])

    def test_rewrite_script_is_served_and_rewrites_safely(self):
        import subprocess
        import urllib.request
        srv_app = _new_app()
        from plexcache.server import serve
        httpd = serve(srv_app, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            body = urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/tools/rewrite_blocks.py", timeout=5).read()
        finally:
            httpd.shutdown()
            httpd.server_close()
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
        for bad in ({"2": {"schedule": "hourly"}}, {"2": {"seconds": 0}}, {"2": {"seconds": 2.5}}, {"2": {"when": 1}}):
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
        self.assertTrue(any("couldn't read libraries" in m for m in msgs))

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
        appmod.plan = lambda *a: dict(real_plan(*a), status="reduced")
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
        self.assertIn("movies: 3 of 3 had one at the end, 0.2 GB (largest 2.0 MB)", text)
        self.assertIn("TV episodes: none at the end", text)
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

    def test_errors_and_helpers(self):
        from plexcache.truenas import parse_size, probe
        with TrueNASClient(TRUENAS.url, "good-key") as tn:
            with self.assertRaises(TrueNASError) as cm:
                tn.call("test.error")
            self.assertIn("no such thing", str(cm.exception))
            with self.assertRaises(TrueNASError) as cm:
                tn.call("test.garbage")
            self.assertIn("isn't JSON", str(cm.exception))
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


class PlexEdgeTests(unittest.TestCase):
    def test_errors(self):
        with self.assertRaises(PlexError):
            PlexClient("", "x")
        c = PlexClient(PLEX.url, "good-token")
        with self.assertRaises(PlexError) as cm:
            c.get("/no/such/thing")
        self.assertIn("HTTP 404", str(cm.exception))
        with self.assertRaises(PlexError):
            PlexClient("http://127.0.0.1:1", "x", timeout=1).identity()



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

    def test_dataset_rows(self):
        r = self.row(self.ds(libraries=[], keys=[], off=["Music"]))
        self.assertEqual((r["in_use"]["text"], r["cache"]), ("Music · caching off", None))
        # caching off, but the record size is still checked: it matters to Plex either way
        r = self.row(self.ds(libraries=[], keys=[], off=["Films"], kinds=["movie"],
                             props={"primarycache": "metadata", "secondarycache": "all", "recordsize": 131072},
                             blocks={"checked": 10, "single": 0, "sizes": {"131072": 10}}))
        self.assertEqual((r["cache"], r["l2arc"], r["recordsize"]["status"]), (None, None, "warn"))
        self.assertEqual([f["title"] for f in r["fixes"]], ["Record size for tank/x", "Then rewrite the existing files on tank/x"])
        self.assertIn("(Films)", r["fixes"][0]["detail"])
        # no TrueNAS API: the setting is unknown, but the files still tell how they were written
        r = self.row(self.ds(kinds=["tv"], blocks={"checked": 10, "single": 0, "sizes": {"131072": 8, "1048576": 2}}), tn_ok=False)
        self.assertEqual(r["recordsize"], {"status": "info", "text": "Setting not checked · ~80% of a sample at 128K"})
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
        self.assertIn("Can't read ZFS statistics", zitems)
        ditems = {i["title"] for i in self.checks.check_datasets(self.app)["items"]}
        self.assertIn("Dataset settings: not checked", ditems)
        self.app.settings.update({"truenas": {"api_key": ""}})
        zitems = {i["title"] for i in self.checks.check_zfs(self.app)["items"]}
        self.assertIn("TrueNAS API not connected", zitems)
        self.app.zfs = z

    def test_plex_problems(self):
        self.app.scan_error = "Plex said no"
        self.app.monitor.last_error = "timed out"
        items = {i["title"]: i for i in self.checks.check_plex(self.app)["items"]}
        self.assertEqual(items["Couldn't list libraries"]["status"], "fail")
        self.assertEqual(items["Now-playing feed"]["status"], "warn")
        self.app.monitor.last_error = None

    def test_ram_and_tuning_fallbacks(self):
        real = self.app.ram_budget
        self.app.ram_budget = lambda: {"cache_max": None}
        self.assertEqual(self.checks._ram_check(self.app)[0]["title"], "RAM for Now Playing Cache and Prefetch")
        self.app.ram_budget = lambda: dict(real(), source="ram")
        self.assertIn("half of RAM", self.checks._ram_check(self.app)[0]["detail"])
        self.app.ram_budget = real
        self.assertEqual(self.checks._recordsize_check(self.app, "t", None, None), ("info", []))
        self.assertEqual(self.checks._l2arc_tuning_checks(self.app, {"l2arc_mfuonly": None})[0]["title"], "L2ARC settings")
        self.assertEqual((self.checks._fmt_bytes(None), self.checks._fmt_bytes(512)), ("unknown", "512 B"))

    def test_is_it_working(self):
        base = {"result": "ok", "started": 1, "l2_growth": 5e9}
        self.app.activity.runs = [dict(base), dict(base, hit_rate=0.05)]
        g = self.checks.check_effectiveness(self.app)
        self.assertEqual(g["items"][0]["title"], "Warm-up reads aren't staying cached")
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

    def test_summaries(self):
        c = self.checks
        names = ("check_plex", "check_media", "check_datasets", "check_zfs", "check_effectiveness")
        saved = {n: getattr(c, n) for n in names}
        group = lambda *st: {"id": "g", "items": [c.item(x, "t") for x in st]}
        try:
            for n in names:
                setattr(c, n, lambda app: group("pass"))
            c.check_effectiveness = lambda app: group("warn")       # the "Is it working?" group is added when present
            res = c.run_all(self.app)
            self.assertEqual(len(res["groups"]), 6)
            self.assertEqual(res["summary"]["text"], "Working, with 1 warning worth a look below.")
            c.check_zfs = lambda app: group("fail", "warn")
            self.assertEqual(c.run_all(self.app)["summary"]["text"],
                             "1 problem to fix and 2 warnings — see the red and orange items below.")
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
            self.assertTrue(any("hasn't been changed" in e["message"] for e in app.activity.snapshot()["events"]))
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
            self.assertEqual(post("/api/blockscan", {"dataset": ["x"]})[0], 400)
            self.assertEqual(post("/login", {"username": ["a"], "password": "b"})[0], 200)  # login off: always fine
            # A prefetch choice must be for a library Prefetch covers, from the web UI as from MQTT
            tv = next(l for l in app.libraries if l["kind"] == "tv")
            photos = next(l for l in app.libraries if l["title"] == "Photos")
            music = next(l for l in app.libraries if l["title"] == "Music")
            status, body = post("/api/settings", {"nextepisode": {"libraries": {photos["key"]: {"start": "next"}}}})
            self.assertEqual(status, 400)
            self.assertIn("doesn't cover", en(body["error"]))
            status, body = post("/api/settings", {"nextepisode": {"libraries": {music["key"]: {"start": "next_group"}}}})
            self.assertIn("isn't a prefetch choice", en(body["error"]))
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
                body = test.body.encode()
                self.send_response(test.code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.body, self.code = '"""x"""\n\n__version__ = "9.0.0"\n', 200
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
        self.assertFalse(newer("0.12.9", "0.13.0"))
        self.assertEqual(parse_version("__version__ = '1.2.3'\n"), "1.2.3")
        self.assertIsNone(parse_version("nothing here"))
        self.assertIn("raw.githubusercontent.com/drsassafras/TrueNAS-Plex-Caching/main/plexcache/__init__.py",
                      __import__("plexcache.updates").updates.SOURCE)

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
               "attention": [{"status": "warn", "title": "Preloads aren't staying in RAM (22% still cached)"},
                             {"status": "info", "title": "Not a problem"},
                             {"status": "warn", "title": "1 next episode not ready in time"}]}
        self.assertEqual(_verdict(rep), "Last 7 days: Preloads aren't staying in RAM (22% still cached); 1 next episode not ready in time")
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
        manual = self.state()["warmup_pacing_manual"]                                   # (pacing's share only while automatic is off)
        self.assertEqual(len(numbers), 4 if manual else 3)
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
        self.assertEqual(tv["options"], ["Default", "Don't prefetch", "Next episode", "Rest of the season", "Whole season",
                                         "Next season", "All seasons of the show", "Every show in the library"])
        self.assertEqual(libs["TV Shows Whole File Cache prefetch"]["options"], ["Default", "Nothing", "Next episode", "Rest of the season"])
        self.assertEqual(len(libs["Music Lead-in Cache prefetch"]["options"]), 7)          # no "next album"
        self.assertIn("Next book", libs["Audiobooks Lead-in Cache prefetch"]["options"])
        self.assertEqual(libs["Movies Lead-in Cache prefetch"]["options"],
                         ["Default", "Don't prefetch", "Next movie in the collection", "Rest of the collection", "Whole collection",
                          "Every movie in the library"])
        # Every template reads a key that exists in the state
        import re as _re
        for t, v in disc.items():
            m = _re.match(r"\{\{ value_json\.([a-z0-9_]+)", v.get("value_template", ""))
            if m:
                self.assertIn(m.group(1), st, t)

    def test_commands(self):
        self.enable()
        self.assertTrue(wait_for(lambda: self.state() is not None))
        self.assertTrue(wait_for(lambda: any("plexcache/set/#" in c[1] for c in self.broker.clients)))
        self.broker.inject("plexcache/set/nowplaying_enabled", "OFF")
        self.assertTrue(wait_for(lambda: not self.app.settings.section("nowplaying")["enabled"]))
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("nowplaying_enabled") == "OFF"))
        self.broker.inject("plexcache/set/nowplaying_ram_percent", "35")
        self.assertTrue(wait_for(lambda: self.app.settings.section("nowplaying")["ram_percent"] == 35))
        self.broker.inject("plexcache/set/poll_seconds", "10 seconds")
        self.assertTrue(wait_for(lambda: self.app.settings.section("general")["poll_seconds"] == 10))
        tv = next(l for l in self.app.libraries if l["kind"] == "tv")
        libs = lambda: self.app.settings.section("nextepisode")["libraries"]
        self.broker.inject(f"plexcache/set/library_{tv['key']}_prefetch_start", "Don't prefetch")
        self.assertTrue(wait_for(lambda: libs().get(tv["key"], {}).get("start") == "off"))
        self.broker.inject(f"plexcache/set/library_{tv['key']}_prefetch_end", "Rest of the season")
        self.assertTrue(wait_for(lambda: libs().get(tv["key"], {}).get("end") == "rest"))
        self.assertEqual(libs()[tv["key"]], {"start": "off", "end": "rest"})
        # Not a choice for a TV library: refused, nothing changes
        self.broker.inject(f"plexcache/set/library_{tv['key']}_prefetch_start", "Next book")
        self.broker.inject("plexcache/set/poll_seconds", "2 seconds")
        self.broker.inject("plexcache/set/nope", "1")
        self.assertTrue(wait_for(lambda: sum("couldn't do" in e["message"] for e in self.app.activity.snapshot()["events"]) >= 3))
        self.assertEqual(libs()[tv["key"]], {"start": "off", "end": "rest"})
        # Buttons, numbers and the remaining selects
        calls = []
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
            n = sum("couldn't do" in e["message"] for e in self.app.activity.snapshot()["events"])
            self.broker.inject(f"plexcache/set/{name}", value)
            self.assertTrue(wait_for(lambda: sum("couldn't do" in e["message"] for e in self.app.activity.snapshot()["events"]) > n),
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
        self.app.settings.update({"nowplaying": {"enabled": True, "ram_percent": 20}, "general": {"poll_seconds": 5, "ram_reserve_gb": 4},
                                  "warmup": {"pacing_percent": 10}})

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
                # availability: a topic or a list, not both; the payloads as published
                self.assertFalse("availability_topic" in conf and "availability" in conf)
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
        state = {"libraries": {"2": {"name": "TV Shows", "kind": "tv"}}, "warmup_pacing_manual": True}
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
        # Same conditions: the share-of-disk-time slider only shows while automatic pacing is off, and so does its control
        self.assertIn("var manual = !w.pacing_auto || !rate;", js)
        auto = [oid for _, oid, _ in ha.entities(dict(state, warmup_pacing_manual=False), allow_control=True)]
        self.assertNotIn("warmup_pacing_percent", auto)
        self.assertIn("warmup_pacing_auto", auto)
        # Watch-only: no controls at all
        self.assertFalse([c for c, _, _ in ha.entities(state, allow_control=False) if c in ("switch", "number", "select", "button")])

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
        self.assertIn("doesn't use TLS", self.app.ha.status["error"])
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
        self.assertIn("certificate wasn't accepted", str(e))
        e = fail(tls=True, verify_tls=False)  # TLS works, but it isn't MQTT
        self.assertEqual(e.step, "login")
        e = MQTTClient("no-such-host.invalid", 1883, timeout=3)
        with self.assertRaises(MQTTError) as cm:
            e.connect()
        self.assertEqual(cm.exception.step, "broker")
        self.assertIn("can't find", str(cm.exception))

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
        self.assertEqual(titles(), {"L2ARC caching": "pass", "Settings used as chosen": "pass"})
        app.fallbacks.note("x", M("Prefetch"), "Whole library", "Whole season")
        group = checks.check_cache_mode(app)
        fell = [i for i in group["items"] if en(i["title"]) == "Settings not used as chosen"][0]
        self.assertEqual(fell["status"], "warn")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

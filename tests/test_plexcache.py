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

# Test files sit on an ordinary filesystem, whose 4K blocks would look like files written
# before a recordsize change. Report every sampled file as fitting in one block instead
# (no information); BlockSizeTests checks the real function and what Health makes of it.
REAL_BLOCK_SIZES = zfs.block_sizes
zfs.block_sizes = lambda paths: (lambda n: {"checked": n, "single": n, "sizes": {}})(len(list(paths)))
from plexcache.app import App  # noqa: E402
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
        s.update({"warmup": {"tv_sec": "20", "enabled": True}})
        again = config.Settings(self.dir)
        self.assertEqual(again.section("warmup")["tv_sec"], 20)
        self.assertTrue(again.section("warmup")["enabled"])
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
            s.update({"nextepisode": {"start_at": "99"}})
        s.update({"warmup": {"movie_sec": 5, "movie_min_sec": 10}})
        self.assertEqual(s.section("warmup")["movie_min_sec"], 5)

    def test_broken_file_is_set_aside(self):
        with open(os.path.join(self.dir, "settings.json"), "w") as fh:
            fh.write("{not json")
        s = config.Settings(self.dir)
        self.assertEqual(s.section("warmup")["tv_sec"], 30)
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
        self.assertLess(p["seconds"]["movie"], 60)
        self.assertGreaterEqual(p["seconds"]["movie"], 3)
        planned = sum(p["planned_by_kind"].values())
        self.assertLessEqual(planned, 400 * 10 ** 9 - 100 * 10 ** 9 + 1)
        self.assertEqual(plan(self._stats(), w, 0)["status"], "none")
        self.assertEqual(plan(self._stats(), w, None)["status"], "unknown")
        self.assertEqual(plan(self._stats(), w, 101 * 10 ** 9)["status"], "insufficient")

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


class NextEpisodeTimingTests(unittest.TestCase):
    def _s(self, progress, credits=None):
        from types import SimpleNamespace
        return SimpleNamespace(progress=progress, view_offset_ms=int(progress * 1_200_000),
                               credits_start_ms=credits)

    def _job(self, state):
        from plexcache.playback import Job
        j = Job("nowplaying", "x", "/x", 0, 1)
        j.state = state
        return j

    def test_default_depends_on_l2arc(self):
        from plexcache.playback import next_episode_due as due
        reading, done = [self._job("reading")], [self._job("done")]
        # With an L2ARC: after Now-playing preload finishes
        self.assertFalse(due("default", self._s(0.1), reading, True, "present"))
        self.assertTrue(due("default", self._s(0.1), done, True, "present"))
        self.assertTrue(due("default", self._s(0.1), [], True, "present"))     # now-playing skipped this stream
        self.assertTrue(due("default", self._s(0.0), [], False, "present"))    # now-playing off: right away
        # Without one (or unknown): at 90%
        for l2 in ("absent", "unknown"):
            self.assertFalse(due("default", self._s(0.85), done, True, l2))
            self.assertTrue(due("default", self._s(0.9), done, True, l2))

    def test_explicit_choices(self):
        from plexcache.playback import next_episode_due as due
        self.assertFalse(due("after_nowplaying", self._s(0.5), [self._job("waiting")], True, "absent"))
        self.assertTrue(due("after_nowplaying", self._s(0.0), [], False, "absent"))
        self.assertFalse(due("80", self._s(0.79), [], False))
        self.assertTrue(due("80", self._s(0.8), [], False))
        self.assertTrue(due("90", self._s(0.9), [], False))

    def test_old_settings_are_migrated(self):
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"nextepisode": {"start_at": "credits"}}, fh)
        s = config.Settings(d)
        self.assertEqual(s.section("nextepisode")["start_at"], "default")
        for v in ("default", "after_nowplaying", "80", "90"):
            s.update({"nextepisode": {"start_at": v}})
        with self.assertRaises(config.SettingsError):
            s.update({"nextepisode": {"start_at": "50"}})


class SchedulerTests(unittest.TestCase):
    def test_daily_and_weekly(self):
        w = dict(config.DEFAULTS["warmup"], schedule="daily", time="03:00")
        now = dt.datetime(2026, 10, 12, 18, 0)
        self.assertEqual(scheduler.next_run(w, now, None), dt.datetime(2026, 10, 13, 3, 0))
        self.assertTrue(scheduler.due(w, dt.datetime(2026, 10, 13, 3, 0, 20), None, dt.datetime(2026, 10, 13, 2, 59, 50)))
        self.assertFalse(scheduler.due(w, dt.datetime(2026, 10, 13, 3, 1), None, dt.datetime(2026, 10, 13, 3, 0, 30)))
        w = dict(w, schedule="weekly", weekday=6)
        self.assertEqual(scheduler.next_run(w, now, None), dt.datetime(2026, 10, 18, 3, 0))
        w = dict(w, schedule="hours", every_hours=12)
        last = dt.datetime(2026, 10, 12, 10, 0)
        self.assertEqual(scheduler.next_run(w, now, last), dt.datetime(2026, 10, 12, 22, 0))
        self.assertIsNone(scheduler.next_run(dict(w, schedule="manual"), now, None))


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
        self.assertEqual(z["Not enough RAM for preloading"]["status"], "fail")
        recs = {r["feature"]: r["verdict"] for r in checks.recommendations(app)}
        self.assertEqual(recs["nowplaying"], "avoid")
        app.settings.update({"general": {"ram_reserve_gb": 57}})       # leaves about 3 GB: under 2 GB for Now-playing
        self.assertEqual(checks._ram_check(app)[0]["title"], "RAM is tight for preloading")

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
        stats = LibraryStats(count={"movie": 1000, "tv": 0, "audio": 0}, avg_kbps={"movie": 20000, "tv": 0, "audio": 0})
        w = dict(config.DEFAULTS["warmup"])
        with_idx = plan(stats, w, None)
        self.assertEqual(with_idx["need_max"], with_idx["need_by_kind"]["movie"] + 1000 * DEFAULT_INDEX["movie"])  # always counted
        measured = plan(stats, w, None, {"movie": 100_000})
        self.assertEqual(measured["index_by_kind"]["movie"], 100_000_000)
        self.assertTrue(measured["index_measured"])


class WarmupPauseTests(unittest.TestCase):
    """When Library warm-up steps aside."""

    def test_quiet_hours(self):
        from plexcache.pauses import in_quiet_hours
        at = lambda h, m=0: dt.datetime(2026, 10, 13, h, m)
        # finish by 07:00, next start 03:00: paused all day, reading 03:00-07:00
        self.assertTrue(in_quiet_hours(at(8), "07:00", "03:00"))
        self.assertTrue(in_quiet_hours(at(2, 59), "07:00", "03:00"))
        self.assertFalse(in_quiet_hours(at(5), "07:00", "03:00"))
        # window that doesn't cross midnight
        self.assertTrue(in_quiet_hours(at(12), "06:00", "23:00"))
        self.assertFalse(in_quiet_hours(at(23, 30), "06:00", "23:00"))
        self.assertFalse(in_quiet_hours(at(12), "03:00", "03:00"))

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
        from plexcache.pauses import WarmupPauses
        app = self._app()
        p = WarmupPauses(app, manual=False)
        self.assertIsNone(p.reason())
        app.monitor.last_buffering = (time.time(), "Dune on Living room TV")
        self.assertIn("buffering (Dune on Living room TV)", p.reason())
        app.settings.update({"warmup": {"pause_buffering": False}})
        self.assertIsNone(p.reason())
        app.monitor.preloading = lambda: "Now-playing is loading Dune"
        self.assertEqual(p.reason(), "Now-playing is loading Dune")
        app.settings.update({"warmup": {"pause_preload": False}})
        self.assertIsNone(p.reason())
        with self.assertRaises(config.SettingsError):
            app.settings.update({"warmup": {"pause_disk_busy": 45}})

    def test_scrub(self):
        from plexcache.pauses import WarmupPauses
        app = self._app()
        TRUENAS.pool_data["scan"] = {"function": "SCRUB", "state": "SCANNING"}
        try:
            p = WarmupPauses(app, manual=False)
            self.assertEqual(p._scrubbing(), "a scrub is running on tank", f"TrueNAS error: {p.scrub_error}")
        finally:
            TRUENAS.pool_data.pop("scan")
        self.assertIsNone(WarmupPauses(app, manual=False).reason())

    def test_disk_busy(self):
        from plexcache.pauses import WarmupPauses
        stats = os.path.join(os.environ["PLEXCACHE_PROC"], "diskstats")
        line = "   8      0 {name} 1 0 0 0 0 0 0 0 0 {ms} 0\n"
        app = self._app()
        p = WarmupPauses(app, manual=False)
        try:
            with open(stats, "w") as fh:
                fh.write(line.format(name="sdq", ms=1000))
            p.mark_idle()
            time.sleep(1.0)
            with open(stats, "w") as fh:
                fh.write(line.format(name="sdq", ms=1800))   # busy ~80% of the last second
            self.assertIn("% busy with other work", p.reason())
            # manual "Run now" goes at full speed and ignores it
            m = WarmupPauses(app, manual=True)
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
            "warmup": {"enabled": True},
            "nowplaying": {"enabled": True, "delay_sec": 0, "min_delay_sec": 0},
            "general": {"poll_seconds": 2},
        })
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
        self.assertIn("Settings → Media mappings", media["Music"]["fix"])
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
        self.app.settings.update({"warmup": {"pacing_percent": 50}, "general": {"hdd_mb_per_sec": 150}})
        try:
            items = {i["title"]: i for i in checks._l2arc_tuning_checks(self.app, params)}
        finally:
            self.app.settings.update({"warmup": {"pacing_percent": 10}})
        self.assertIn("L2ARC only keeps frequently used data", items)
        self.assertEqual("echo 0 | sudo tee /sys/module/zfs/parameters/l2arc_mfuonly", items["L2ARC only keeps frequently used data"]["fix_cmd"])
        self.assertIn("L2ARC is emptied on every reboot", items)
        slow = items["L2ARC fills slower than warm-up reads"]
        self.assertIn("8 MiB/s", slow["detail"])
        self.assertIn("75 MB/s", slow["detail"])
        self.assertIn("about 5%", slow["fix"])
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
            for feature in ("nowplaying", "nextepisode"):
                self.app.settings.update({feature: {"enabled": False}})
                status, r = self.post("/api/settings", {feature: {"enabled": True}})
                self.assertEqual(status, 400, feature)
                self.assertIn("primarycache", r["error"])
                # switching off is always allowed
                status, _ = self.post("/api/settings", {feature: {"enabled": False}})
                self.assertEqual(status, 200)
        finally:
            self.app.zfs = saved
            self.app.settings.update({"nowplaying": {"enabled": True}, "nextepisode": {"enabled": True}})

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
            self.app.settings.update({"warmup": {"enabled": False}})
            status, r = self.post("/api/settings", {"warmup": {"enabled": True}})
            self.assertEqual(status, 400)
            self.assertIn("L2ARC", r["error"])
            self.assertFalse(self.app.settings.section("warmup")["enabled"])
            self.assertEqual(self.app.state()["l2arc"], "absent")
            from plexcache import checks
            rec = {r["feature"]: r for r in checks.recommendations(self.app)}["warmup"]
            self.assertEqual(rec["verdict"], "avoid")
            self.assertIn("Unavailable", rec["reason"])
            rows = {r["dataset"]: r for r in checks.check_datasets(self.app)["datasets"]}
            self.assertEqual(rows[TV_DS]["l2arc"], {"status": "info", "text": "", "badge": "None"})  # warm-up is off here
            self.app.settings.update({"warmup": {"enabled": True}})
            z = dataset_fixes(checks.check_datasets(self.app))
            self.assertIn("TV Shows", z["No L2ARC on pool tank"]["detail"])
            self.assertFalse(self.app.library_cacheable("2", "warmup"))
            self.assertTrue(self.app.library_cacheable("2", "nowplaying"))
        finally:
            self.app.zfs = saved
            self.app.settings.update({"warmup": {"enabled": True}})
        # with an L2ARC and primarycache=all it can be switched on
        self.app.settings.update({"warmup": {"enabled": False}})
        status, r = self.post("/api/settings", {"warmup": {"enabled": True}})
        self.assertEqual(status, 200)

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
        for p in ("/", "/health", "/features", "/activity", "/settings"):
            status, body, _ = self.get(p)
            self.assertEqual(status, 200)
            self.assertIn(b"/static/app.js", body)
        status, _, headers = self.get("/static/app.js")
        self.assertIn("javascript", headers["Content-Type"])
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
        status, r = self.post("/api/settings", {"warmup": {"tv_sec": 25}})
        self.assertEqual(status, 200)
        self.assertEqual(r["settings"]["warmup"]["tv_sec"], 25)
        status, r = self.post("/api/settings", {"warmup": {"tv_sec": -1}})
        self.assertEqual(status, 400)
        status, r = self.post("/api/settings", {"warmup": {"tv_sec": 20}}, ctype="text/plain")
        self.assertEqual(status, 415)

    def test_warmup_run_via_api(self):
        # 3 s of each episode is far less than the file, so the index at the end is a separate read
        self.app.settings.update({"warmup": {"tv_sec": 3, "tv_min_sec": 3}})
        self.addCleanup(self.app.settings.update, {"warmup": {"tv_sec": 30}})
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
            feats = {j["feature"]: j for j in jobs}
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
                           nextepisode={"enabled": False})
        cls.app.scan_library()
        cls.app.run_checks()

    def setUp(self):
        import plexcache.playback as pb
        self.pb = pb
        self.saved = (pb.MIN_JOB, pb.RESTART_EVERY)
        mon = self.app.monitor
        mon.sessions.clear()
        mon._seen.clear()
        mon._recent.clear()
        mon.last_buffering = None
        self.app.settings.update({"general": {"ram_reserve_gb": 4}, "nowplaying": {"delay_sec": 0},
                                  "nextepisode": {"enabled": False}})

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

    def test_next_tracks_for_music_and_audiobooks(self):
        app, mon = self.app, self.app.monitor
        app.library_cacheable = lambda *a, **k: True       # the audiobook dataset is left at primarycache=metadata in tests

        def targets(sid):
            return [j.target for j in mon._chains.get(sid, []) if not j.cancel]

        def play(n, sid, mode):
            app.settings.update({"nextepisode": {"library_modes": {"3": mode}}})
            self.session(CAT["tracks"][n], 10000, sid=sid)
            self.poll()
            self.assertTrue(wait_for(lambda: all(j.state == "done" for j in mon._chains.get(sid, [])), 10))
        try:
            app.settings.update({"nowplaying": {"enabled": False}, "nextepisode": {"enabled": True}})
            # Default for audiobooks: 20 minutes ahead — here, the rest of the book (two 2-minute chapters)
            app.settings.update({"nextepisode": {"library_modes": {}}})
            self.session(CAT["tracks"][0], 10000)
            self.poll()
            self.assertTrue(wait_for(lambda: len(mon._chains.get("m1", [])) == 2 and all(j.state == "done" for j in mon._chains["m1"]), 10))
            self.assertEqual(targets("m1"), ["Author · Chapter 2", "Author · Chapter 3"])
            self.assertTrue(all(j["whole"] for j in self.jobs("nextepisode")))
            # Whole book, from the last chapter: the earlier chapter is read too (the others are already done)
            mon._recent.pop(CAT["tracks"][0]["path"], None)
            play(2, "m2", "album")
            self.assertEqual(targets("m2"), ["Author · Chapter 1"])
            # Everything by the author: the rest of this book, then the start of the other book, then all of it
            mon._recent.clear()
            play(0, "m3", "artist")
            self.assertEqual(targets("m3"), ["Author · Chapter 2", "Author · Chapter 3", "Author · Book 2, chapter 1 (first seconds)",
                                             "Author · Book 2, chapter 1", "Author · Book 2, chapter 2"])
            self.assertEqual(mon._chains["m3"][2].length, 1024 * 1024)  # ~10 s, rounded up to a whole MiB
            # Within the RAM limit, in order: here only the first chapter fits
            mon._recent.clear()
            real = app.ram_budget
            app.ram_budget = lambda: dict(real(), nextepisode=3 * 1024 * 1024)
            try:
                play(0, "m4", "artist")
            finally:
                app.ram_budget = real
            self.assertEqual(targets("m4"), ["Author · Chapter 2"])
            # Don't preload: nothing
            play(0, "m5", "off")
            self.assertEqual(targets("m5"), [])
            # a player that stops: what it had queued is dropped
            PLEX.sessions = []
            self.poll()
            self.assertEqual(mon._chains, {})
        finally:
            del app.library_cacheable
            app.settings.update({"nowplaying": {"enabled": True}, "nextepisode": {"library_modes": {}}})

    def test_preload_choices_are_checked_and_old_checkboxes_carry_over(self):
        import json
        from plexcache.config import Settings, SettingsError
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"nextepisode": {"excluded_libraries": ["3"], "audio_ahead_min": 60}}, fh)
        st = Settings(d)
        self.assertEqual(st.section("nextepisode")["library_modes"], {"3": "off"})
        self.assertNotIn("excluded_libraries", st.section("nextepisode"))
        with self.assertRaises(SettingsError):
            st.update({"nextepisode": {"library_modes": {"3": "forever"}}})
        with self.assertRaises(SettingsError):
            st.update({"nextepisode": {"library_modes": ["off"]}})
        from plexcache.playback import slice_bytes
        self.assertEqual((slice_bytes(0, 1000), slice_bytes(5 * 1024 * 1024, 0)), (0, 1024 * 1024))

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
        self.app.settings.update({"nowplaying": {"delay_sec": 3}})
        self.session(CAT["episodes"][1], 60000)
        self.poll()
        job = self.app.monitor.sessions["m1:" + CAT["episodes"][1]["key"]]["jobs"][0]
        job.state = "reading"                                   # as if mid-read
        self.assertIn("Now-playing is loading", self.app.monitor.preloading())
        job.state = "waiting"
        self.assertIsNone(self.app.monitor.preloading())

    def test_skip_ahead_restarts_from_the_new_position(self):
        self.pb.MIN_JOB, self.pb.RESTART_EVERY = MiB, 0
        b = self.app.ram_budget()
        # leave Now-playing about 5 MB, less than the 12 MB episode
        self.app.settings.update({"general": {"ram_reserve_gb": (b["available"] - 8.5e6) / 1e9}})
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
        self.app.settings.update({"general": {"ram_reserve_gb": b["available"] / 1e9}})
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
        self.assertTrue(wait_for(lambda: self.jobs() and self.jobs()[0]["state"] == "failed"))
        self.assertTrue(wait_for(lambda: any("couldn't read" in m for m in self.events())))

    def test_stream_ending_cancels_waiting_preload(self):
        self.app.settings.update({"nowplaying": {"delay_sec": 1}})
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
        self.app.settings.update({"nextepisode": {"enabled": True, "start_at": "after_nowplaying"},
                                  "nowplaying": {"enabled": False}})
        try:
            self.session(CAT["episodes"][-1], 60000)
            self.poll()
            self.assertTrue(any("no next episode after" in m for m in self.events()))
        finally:
            self.app.settings.update({"nowplaying": {"enabled": True}})

    def test_monitor_survives_errors(self):
        mon = self.app.monitor
        self.app.settings.update({"general": {"poll_seconds": 2}})
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
        self.app.settings.update({"nextepisode": {"enabled": True, "start_at": "after_nowplaying"},
                                  "nowplaying": {"enabled": False}})
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
            self.assertTrue(nxt["ready"])
            self.assertEqual(nxt["label"], "Show's Name · S02E01")
            end = self.metrics("streamend")[-1]
            self.assertIsNotNone(end["busy_peak"])
        finally:
            pb.RETENTION_AFTER = saved_after
            os.remove(stats)
            self.app.settings.update({"nowplaying": {"enabled": True}})

    def test_start_rules(self):
        from plexcache.playback import Job, next_episode_due, resolve_start
        s = type("S", (), {"progress": 0.85})()
        self.assertTrue(next_episode_due("80", s, [], False))
        self.assertFalse(next_episode_due("90", s, [], False))
        self.assertTrue(next_episode_due("after_nowplaying", s, [], False))
        busy = Job("nowplaying", "x", "/f", [(0, 10)], 10, state="reading")
        self.assertFalse(next_episode_due("after_nowplaying", s, [busy], True))
        self.assertEqual(resolve_start("default", "present"), "after_nowplaying")
        self.assertEqual(resolve_start("default", "absent"), "90")
        self.assertEqual(Job("nowplaying", "x", "/f", [], 0).offset, 0)


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
        self.assertEqual(json.loads(self.req("GET", "/api/plex/pin/4242")[1]), {"done": False})
        self.plextv.approved = True
        self.assertEqual(json.loads(self.req("GET", "/api/plex/pin/4242")[1]), {"done": True})
        self.assertEqual(self.app.settings.section("plex")["token"], "good-token")
        self.plextv.fail = True
        try:
            self.assertEqual(self.req("POST", "/api/plex/pin", {})[0], 502)
            self.assertEqual(self.req("GET", "/api/plex/pin/4242")[0], 502)
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
        self.assertEqual("Last 7 days, 2 streams: 1 of 1 skips and 1 of 1 next episodes came from cache.",
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
        for want in ("1 skip went to disk: RAM limit full", "1 skip before the preload started",
                     "1 skip ahead of the preload", "1 skip without Now-playing", "1 next episode not ready in time",
                     "Preloads aren't staying in RAM (40% still cached)"):
            self.assertIn(want, titles)
        self.assertIn("Disks reached 95% busy while people watched", titles)
        self.assertEqual((r["ram"]["limited"], r["ram"]["no_room"]), (1, 1))

    def test_warnings_busy_disks_and_quiet_weeks(self):
        from plexcache import report
        rows = [("stream", {"label": "A"}), ("streamend", {"label": "A", "seconds": 60, "busy_avg": 50, "busy_peak": 90}),
                ("next", {"label": "B", "ready": False, "lead": None, "pct": 40, "state": "reading"})]
        r = report.build(self.app_with(rows))
        self.assertEqual(r["verdict"]["status"], "warn")
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
        self.assertEqual(allr["month"]["tiles"]["skips"], {"cached": 1, "total": 2})
        self.assertEqual(allr["year"]["series"][0]["label"], "Apr")
        self.assertEqual(allr["year"]["series"][2]["retention"], 50)  # June's summary
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
        self.assertEqual((s["busy_n"], s["busy_peak"], s["miss"], s["late"]), (2, 80, {"unknown": 1}, ["X (20% loaded)"]))
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
        self.assertEqual((d["general"]["poll_seconds"], d["general"]["ram_reserve_gb"], d["warmup"]["movie_sec"]), (5, 4, 60))
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
        look = subprocess.run([sys.executable, script, "tank/media"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(look.returncode, 0, look.stderr)
        self.assertIn("1 to rewrite", look.stdout)
        self.assertIn("Nothing was changed", look.stdout)
        ino = os.stat(f).st_ino
        run = subprocess.run([sys.executable, script, "tank/media", "--apply", "--yes", "--mb-per-sec", "0", "--min-free-gb", "0"],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Rewrote 1 file(s)", run.stdout)
        st = os.stat(f)
        with open(f, "rb") as fh:
            self.assertEqual(fh.read(), data)                       # same content
        self.assertNotEqual(st.st_ino, ino)                         # a new file…
        self.assertEqual((st.st_mtime, st.st_mode & 0o777), (1_600_000_000, 0o640))  # …with the same details
        self.assertEqual(os.listdir(os.path.join(media, "Show")), ["ep1.mkv"])        # no temporary file left


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


class AppEdgeTests(unittest.TestCase):
    """Start-up, scheduling, connection lights and fallbacks."""

    def test_start_and_scheduler(self):
        import plexcache.app as appmod
        app = _new_app(warmup={"enabled": True})
        started = []
        app.start_warmup = lambda manual: started.append(manual)
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
            def __init__(self, *a):
                pass

            def reason(self):
                return next(calls, None)

            def mark_idle(self):
                pass
        real = appmod.WarmupPauses
        appmod.WarmupPauses = P
        try:
            app._warmup_worker(manual=True)
        finally:
            appmod.WarmupPauses = real
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
        self.assertIn("1 MP4 file keep their index at the end", text)

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
            self.assertEqual(main_mod.main(), 0)
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
        self.assertEqual(next_run({"schedule": "hours", "every_hours": 6}, now, None), now)
        weekly = {"schedule": "weekly", "weekday": now.weekday(), "time": "09:00"}
        self.assertEqual(next_run(weekly, now, None), dt.datetime(2026, 10, 20, 9, 0))   # today's 9:00 has passed
        self.assertFalse(due({"schedule": "manual"}, now, None, now))
        self.assertTrue(due({"schedule": "hours", "every_hours": 6}, now, None, now))
        self.assertFalse(due({"schedule": "hours", "every_hours": 6}, now, now - dt.timedelta(hours=1), now))

    def test_config_validation(self):
        d = tempfile.mkdtemp(dir=TMP)
        s = config.Settings(d)
        bad = [{"warmup": {"time": "25:00"}}, {"warmup": {"pacing_percent": "lots"}}, {"general": {"excluded_libraries": "1"}},
               {"general": {"disk_speeds": []}}, {"general": {"disk_speeds": {"a": {"mb_s": "x"}}}},
               {"general": {"path_map": "x"}}, {"nope": {}}, {"warmup": {"nope": 1}}, {"warmup": {"pause_disk_busy": "x"}}, "x"]
        for patch in bad:
            with self.assertRaises(config.SettingsError, msg=patch):
                s.update(patch)
        s.update({"general": {"disk_speeds": {"tank/a": {"mb_s": 200}, "tank/b": None}, "debug": "yes"},
                  "plex": {"token_set": True}, "warmup": {"finish_by": "06:30"}})
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
        self.app = _new_app(warmup={"enabled": True})
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
        self.assertEqual(self.checks._ram_check(self.app)[0]["title"], "RAM for preloading")
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
            self.assertEqual(len(res["groups"]), 5)
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

if __name__ == "__main__":
    unittest.main(verbosity=2)

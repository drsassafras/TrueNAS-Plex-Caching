import datetime as dt
import json
import os
import shutil
import sys
import tempfile
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
os.environ["PLEXCACHE_PROC"] = fakes.make_proc(TMP, MEDIA)
os.environ["PLEXCACHE_SYS"] = os.path.join(TMP, "sys")

from plexcache import config, scheduler, zfs  # noqa: E402
from plexcache.app import App  # noqa: E402
from plexcache.plex import PlexClient, PlexError  # noqa: E402
from plexcache.reader import MiB, ReadResult, is_cache_hit, read_range  # noqa: E402
from plexcache.truenas import TrueNASClient, TrueNASError, describe_pool, ws_url  # noqa: E402
from plexcache.warmup import LibraryStats, WarmupRun, plan, preload_bytes  # noqa: E402

PLEX = fakes.FakePlex(CAT)
TRUENAS = fakes.FakeTrueNAS(workdir=TMP)


def tearDownModule():
    PLEX.close()
    TRUENAS.close()
    shutil.rmtree(TMP, ignore_errors=True)


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

    def test_legacy_import(self):
        text = '''PLEX_PATH_PREFIX="/data"
TRUENAS_PATH_PREFIX="/mnt/tank/plexfiles"
PLEX_HOST="127.0.0.1"
PLEX_PORT="32400"
PLEX_PROTOCOL="http"
PRELOAD_PACING_PERCENT="15"
MOVIE_PRELOAD_MAX_SEC="45"
TV_PRELOAD_MAX_SEC='20'
HDD_MB_PER_SEC=180
EVIL="$(rm -rf /)"
'''
        patch, notes = config.parse_legacy_conf(text)
        self.assertEqual(patch["warmup"]["pacing_percent"], "15")
        self.assertEqual(patch["warmup"]["movie_sec"], "45")
        self.assertEqual(patch["warmup"]["tv_sec"], "20")
        self.assertEqual(patch["general"]["hdd_mb_per_sec"], "180")
        self.assertEqual(patch["plex"]["url"], "http://127.0.0.1:32400")
        self.assertTrue(any("127.0.0.1" in n for n in notes))
        s = config.Settings(self.dir)
        s.update(patch)
        self.assertEqual(s.section("warmup")["movie_sec"], 45)


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
        self.assertEqual(props, {"primarycache": "metadata", "secondarycache": "all"})

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
            "nowplaying": {"enabled": True, "delay_sec": 0, "mode": "size", "size_gb": 1},
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

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.read(), r.headers

    def post(self, path, body, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health_results(self):
        h = self.app.health
        groups = {g["id"]: g for g in h["groups"]}
        media = {r["library"]: r for r in groups["media"]["table"]}
        self.assertEqual(media["Movies"]["status"], "pass")
        self.assertEqual(media["Music"]["status"], "warn")
        self.assertIn("/music", media["Music"]["fix"])
        zitems = {i["title"]: i for i in groups["zfs"]["items"]}
        self.assertEqual(zitems["Media dataset"]["status"], "pass")
        self.assertIn("960", zitems["L2ARC cache devices"]["detail"])
        self.assertEqual(zitems["Dataset cache settings"]["status"], "warn")  # metadata + playback features on
        self.assertIn("zfs set primarycache=all tank/plexfiles", zitems["Dataset cache settings"]["fix_cmd"])
        recs = {r["feature"]: r["verdict"] for r in h["recommendations"]}
        self.assertEqual(recs, {"warmup": "use", "nextepisode": "use", "nowplaying": "maybe"})
        self.assertEqual(self.app.stats.count["tv"], 6)

    def test_pages_and_static(self):
        for p in ("/", "/health", "/features", "/activity", "/settings"):
            status, body, _ = self.get(p)
            self.assertEqual(status, 200)
            self.assertIn(b"/static/app.js", body)
        status, _, headers = self.get("/static/app.js")
        self.assertIn("javascript", headers["Content-Type"])
        for bad in ("/static/../config.py", "/static/.hidden", "/nope"):
            with self.assertRaises(urllib.error.HTTPError):
                self.get(bad)

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

    def test_import_api(self):
        status, r = self.post("/api/import", {"text": 'RESERVED_L2ARC_GB="50"\n'})
        self.assertEqual(status, 200)
        self.assertEqual(r["imported"], ["warmup.reserve_gb"])
        self.assertEqual(self.app.settings.section("warmup")["reserve_gb"], 50)

    def test_warmup_run_via_api(self):
        status, r = self.post("/api/warmup/run", {})
        self.assertTrue(r["started"])
        for _ in range(100):
            if self.app.warmup_run is None:
                break
            time.sleep(0.1)
        run = self.app.activity.snapshot()["runs"][0]
        self.assertEqual(run["result"], "ok")
        self.assertEqual(run["files"], 4 + 6 + 3)
        self.assertEqual(run["errors"], 0)
        self.assertTrue(run["manual"])

    def test_playback_features(self):
        import plexcache.playback as pb
        pb.AUTO_DELAY_LOCAL = 1
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

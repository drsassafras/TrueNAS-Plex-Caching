"""Run the app against fake Plex/TrueNAS servers for UI work.

    python3 tests/demo.py [port]

Then open http://127.0.0.1:8642/.
"""

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fakes  # noqa: E402

tmp = tempfile.mkdtemp(prefix="plexcache-demo-")
# DEMO_ROOT puts the sample media at a realistic path (e.g. /mnt/tank/plexfiles) for screenshots.
media = os.environ.get("DEMO_ROOT") or os.path.join(tmp, "media")
cat = fakes.make_media(media, movies=12, seasons=3, episodes=8, tracks=10)
# Invented titles, so screenshots show a believable library without anyone's real one.
MOVIES = ["The Lighthouse Keeper", "Northern Lines", "Glass Harbour", "Paper Moons", "The Last Ferry", "Copper Valley",
          "Midnight Orchard", "Saltwater Kings", "A Quiet Signal", "The Cartographer", "Ember and Ash", "Long Way North"]
SHOW, AUTHOR = "Harbour Street", "E. M. Hollis"
EPISODES = ["Low Tide", "The Night Shift", "Breakwater", "Fog Bells", "Signal Fires", "Undertow", "Mooring", "Slack Water"]
for m, title in zip(cat["movies"], MOVIES):
    m["title"] = title
    m["year"] = 2010 + cat["movies"].index(m)
    m["duration"] = 5400000 + 60000 * cat["movies"].index(m)
    m["guid"] = f"plex://movie/demo{cat['movies'].index(m)}"
    m["guids"] = [f"tmdb://{4000 + cat['movies'].index(m)}"]
# the Tools pages: "Glass Harbour" on a second record too (the same match); "Paper Moons" with a copy of its
# file beside it on one record; two films loose in the library folder itself
import shutil as _sh
_m2, _m3 = cat["movies"][2], cat["movies"][3]
_dup = os.path.join(media, "Movies", "Glass Harbour (2012) [copy].mkv")
_sh.copy(_m2["path"], _dup)
cat["movies"].append({"path": _dup, "size": _m2["size"], "kbps": _m2["kbps"], "key": "190", "title": _m2["title"], "year": _m2["year"], "guid": _m2["guid"], "guids": _m2["guids"]})
cat["movies"][2]["locked"] = ["title", "summary"]
# a film in another container, for Convert to MKV
_avi = os.path.join(media, "Movies", "Tin Lanterns (1998)", "Tin Lanterns (1998).avi")
os.makedirs(os.path.dirname(_avi), exist_ok=True)
with open(_avi, "wb") as _fh:
    _fh.write(b"RIFF" + b"\0" * (3 * 1024 * 1024))
os.utime(_avi, (time.time() - 86400, time.time() - 86400))
cat["movies"].append({"path": _avi, "size": 3 * 1024 * 1024, "kbps": 1200, "key": "191", "title": "Tin Lanterns", "year": 1998, "guid": "plex://movie/191", "guids": ["tmdb://1998"], "duration": 5000000})
_copy = os.path.join(os.path.dirname(_m3["path"]), "Paper Moons (2013) - copy.mkv")
_sh.copy(_m3["path"], _copy)
_m3["more"] = [_copy]
for _i in (10, 11):
    _m = cat["movies"][_i]
    _loose = os.path.join(media, "Movies", os.path.basename(_m["path"]).replace("Movie", _m["title"]))
    os.rename(_m["path"], _loose)
    with open(os.path.splitext(_loose)[0] + ".en.srt", "w") as _fh:
        _fh.write("1\n")
    _sh.rmtree(os.path.dirname(_m["path"]))
    _m["path"] = _loose
for ep in cat["episodes"]:
    ep["title"] = EPISODES[(ep["episode"] - 1) % len(EPISODES)]
# TV is on its own child dataset (set up well); the rest share the parent (left at metadata/128K).
os.environ.setdefault("PLEXCACHE_OPTIMIZER_URL", "http://127.0.0.1:9")  # no optimizer service in the demo
os.environ["PLEXCACHE_PROC"] = fakes.make_proc(tmp, media, extra=((os.path.join(media, "TV"), "tank/plexfiles/tv"),))
os.environ["PLEXCACHE_SYS"] = fakes.make_sys(tmp, {"l2arc_noprefetch": 1, "l2arc_write_max": 8388608,
                                                   "l2arc_rebuild_enabled": 1, "l2arc_mfuonly": 0})

from plexcache import mapping, zfs  # noqa: E402
from plexcache.app import App  # noqa: E402
from plexcache.server import serve  # noqa: E402

# The demo files sit on an ordinary filesystem (4K blocks). Pretend the TV dataset was moved to
# 1M with most of its episodes still written at 128K, and everything else matches.
def _demo_blocks(paths):
    paths = list(paths)
    tv = [p for p in paths if "/TV/" in p]
    if tv:
        old = round(len(tv) * 0.6)
        return {"checked": len(tv), "single": 0, "sizes": {str(131072): old, str(1048576): len(tv) - old}}
    return {"checked": len(paths), "single": len(paths), "sizes": {}}


zfs.block_sizes = _demo_blocks
mapping.SYSTEM_MOUNTS = tuple(m for m in mapping.SYSTEM_MOUNTS if m != "/tmp")  # demo files live in /tmp
# Plex reports everything under /data, like a typical Plex app; this app has it elsewhere.
plex = fakes.FakePlex(cat, report_root="/data")
plex.show_name, plex.author = SHOW, AUTHOR
# DEMO_SHOWCASE=1: a system that's mostly set up well (for screenshots), with one thing left to fix.
showcase = os.environ.get("DEMO_SHOWCASE") == "1"
if not showcase:
    # Movies has two folders in Plex, like many real setups ("Movies" and "3D Movies").
    os.makedirs(os.path.join(media, "3D Movies"), exist_ok=True)
    plex.libraries[0] = ("1", "Movies", "movie", os.path.join(media, "3D Movies") + "|" + os.path.join(media, "Movies"))
no_l2 = os.environ.get("DEMO_NO_L2ARC") == "1"  # show what happens on a pool without an SSD cache
tn = fakes.FakeTrueNAS(workdir=tmp, pool={"name": "tank", "status": "ONLINE",
                                          "topology": {"data": [{"type": "RAIDZ1", "children": [{}, {}, {}, {}]}], "cache": []}}
                       if no_l2 else None)
tn.per_dataset["tank/plexfiles/tv"] = {"primarycache": "all", "secondarycache": "all", "recordsize": 1048576}
if showcase:
    tn.per_dataset["tank/plexfiles"] = {"primarycache": "all", "secondarycache": "all", "recordsize": 1048576}
plex.sessions = [
    {"id": "a", "item": cat["episodes"][4], "offset": 690000, "duration": 1680000, "player": "Living room TV"},
    {"id": "b", "item": cat["movies"][3], "offset": 900000, "duration": 7200000, "player": "Phone", "local": False},
]
app = App(os.path.join(tmp, "config"))
app.settings.update({
    "plex": {"url": plex.url, "token": "good-token"},
    "truenas": {"url": tn.url, "api_key": "good-key"},
    "general": {"hdd_mb_per_sec": 182, "hdd_measured_at": "2026-10-07T21:14:00",
                # TV and Audiobooks already mapped; Movies left for the demo to pick.
                "path_map": [{"plex": "/data/TV", "local": os.path.join(media, "TV")},
                             {"plex": "/data/Audiobooks", "local": os.path.join(media, "Audiobooks")}]
                + ([{"plex": "/data/Movies", "local": os.path.join(media, "Movies")}] if showcase else [])},
})
if showcase:
    app.settings.update({"nowplaying": {"enabled": True, "delay_sec": 15},
                         "general": {"excluded_libraries": ["4"]}})  # no music in the sample library
now = time.time()
for i, (hit, growth, files) in enumerate([(0.02, 11e9, 25598), (0.64, 171e9, 25611), (0.71, 148e9, 25640)]):
    start = now - (2 - i) * 86400 - 15 * 3600
    app.activity.add_run({"started": start, "finished": start + 9700 - i * 400, "files": files, "total": files,
                          "bytes": 505e9, "hit_rate": hit, "errors": 0, "l2_growth": growth, "result": "ok",
                          "manual": i == 0, "by_kind": {}, "seconds": {}})
# How long cached data lasts: re-reads by age, RAM falling off after a few hours, the SSD lasting days.
_ram = [[40, 0], [55, 1], [48, 2], [30, 4], [18, 6], [12, 14], [4, 15], [1, 9], [0, 3], [0, 0], [0, 0], [0, 0], [0, 0]]
_ssd = [[0, 0], [12, 0], [20, 0], [34, 0], [41, 1], [60, 1], [88, 2], [120, 3], [96, 5], [70, 9], [31, 12], [6, 9], [0, 2]]
app.cachelog.stats = {"ram": {"first": 412, "ages": _ram, "libs": {}}, "ssd": {"first": 25611, "ages": _ssd, "libs": {}}}
# The same per library, from simulated reads: each library's data lasts a little differently.
import math as _m
import random as _rl
_rl.seed(7)
_t0, _boot = time.time() - 9 * 86400, app.cachelog.boot_time
app.cachelog.boot_time = 0
for _key, _n, _new, _ram_h, _ssd_d in (("1", 160, 0.4, 1.5, 3), ("2", 320, 0.15, 4, 6), ("3", 90, 0.1, 8, 10)):
    for _i in range(_n):
        for _route, _life, _spread in (("ram", _ram_h * 3600, 600), ("ssd", _ssd_d * 86400, 1800)):
            _p, _at = f"/demo/{_route}/{_key}/{_i}", _t0 + _rl.uniform(0, 3600)
            app.cachelog.note(_p, False, at=_at, route=_route, hit=_rl.random() < 0.05, library=_key)
            if _rl.random() < _new:
                continue  # cached once, never read again
            _age = _m.exp(_rl.uniform(_m.log(_spread), _m.log(20 * 86400)))
            app.cachelog.note(_p, False, at=_at + _age, route=_route, hit=_rl.random() < _m.exp(-_age / _life), library=_key)
app.cachelog.boot_time = _boot
# Library cached over time: two weeks of hours, with a warm-up every Sunday at 03:00
_files = [(f"/demo/{_k}/{_i}", 10_000_000, _k) for _k, _n in (("1", 300), ("2", 900), ("3", 200)) for _i in range(_n)]
_last = {}
_h0 = time.time() - time.time() % 3600 - 14 * 86400
for _h in range(14 * 24):
    _t = _h0 + _h * 3600
    _tm = time.localtime(_t)
    if 18 <= _tm.tm_hour <= 23:
        for _ in range(8):
            _last[_files[int(_rl.random() ** 2.5 * len(_files))][0]] = _t
    if _tm.tm_wday == 6 and _tm.tm_hour == 3:
        for _p, _s, _k in _files:
            if _k == "2" or _rl.random() < 0.3:
                _last[_p] = _t
    app.coverage.record(_files, _last, now=_t + 60)
app.cachelog.since = time.time() - 9 * 86400
# A week of what the playback monitor measures, for the Activity report.
import random as _r
_r.seed(4)
problems = os.environ.get("DEMO_PROBLEMS") == "1"  # show the "Needs attention" side
for i in range(37):
    app.activity.record("stream", label=MOVIES[i % 12], player="Living room TV", transcoding=False, local=True, media="movie")
    app.activity.record("streamend", label=MOVIES[i % 12], seconds=3600, busy_avg=_r.uniform(8, 30),
                        busy_peak=_r.uniform(30, 92 if problems else 60))
for i in range(43):
    miss = i < (5 if problems else 2)
    app.activity.record("skip", label=MOVIES[i % 12], seconds=120, cached=not miss,
                        reason=("only part of the file fits in the RAM limit" if problems and i < 3 else "the preload hadn't reached that part yet") if miss else None)
for i in range(12):
    late = problems and i < 2
    app.activity.record("next", label=f"{SHOW} · S01E{i + 2:02d}", ready=not late, lead=None if late else _r.randint(300, 900),
                        pct=40 if late else 100, state="reading" if late else "done")
for i in range(9):
    app.activity.record("retention", label=MOVIES[i % 12], feature="nowplaying", pct=_r.choice([88, 100, 100, 75]) if not problems else _r.choice([40, 60, 75]), minutes=30)
    app.activity.record("read", feature="nowplaying", label=MOVIES[i % 12], cached=_r.randint(0, 2) * 10 ** 9, disk=_r.randint(5, 30) * 10 ** 9)
if problems:
    app.activity.record("limit", label=MOVIES[0], feature="nowplaying", pct=45)
# Spread those over the last week, and add a quieter year before it, so every period has a chart.
_now = time.time()
for m in app.activity.metrics:
    m["t"] = _now - _r.uniform(0, 7 * 86400) * _r.random()
for day in range(7, 365):
    for _ in range(_r.choice([0, 1, 1, 2, 3])):
        t = _now - day * 86400 - _r.uniform(0, 86400)
        app.activity.metrics.append({"what": "stream", "t": t, "label": _r.choice(MOVIES)})
        app.activity.metrics.append({"what": "streamend", "t": t, "label": _r.choice(MOVIES), "busy_avg": _r.uniform(8, 30), "busy_peak": _r.uniform(30, 60)})
        for _ in range(_r.randint(0, 3)):
            app.activity.metrics.append({"what": "skip", "t": t, "cached": _r.random() > 0.08, "reason": "the preload hadn't reached that part yet"})
        if _r.random() < 0.4:
            app.activity.metrics.append({"what": "next", "t": t, "label": f"{SHOW} · S0{_r.randint(1, 3)}E0{_r.randint(1, 8)}", "ready": _r.random() > 0.05, "lead": 600, "pct": _r.choice([40, 70])})
        app.activity.metrics.append({"what": "retention", "t": t, "pct": _r.choice([80, 90, 100])})
        app.activity.metrics.append({"what": "read", "t": t, "cached": 10 ** 9, "disk": _r.randint(5, 30) * 10 ** 9})
app.activity.metrics.sort(key=lambda m: m["t"])
app.activity.record("stream", label="Just now", player="Phone", transcoding=False, local=True, media="movie")
app.start()
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8642
httpd = serve(app, "127.0.0.1", port)
print(f"demo on http://127.0.0.1:{port}", flush=True)
httpd.serve_forever()

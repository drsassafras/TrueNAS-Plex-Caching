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
media = os.path.join(tmp, "media")
cat = fakes.make_media(media, movies=12, seasons=3, episodes=8, tracks=10)
os.environ["PLEXCACHE_PROC"] = fakes.make_proc(tmp, media)

from plexcache.app import App  # noqa: E402
from plexcache.server import serve  # noqa: E402

plex = fakes.FakePlex(cat)
tn = fakes.FakeTrueNAS(workdir=tmp)
plex.sessions = [
    {"id": "a", "item": cat["episodes"][4], "offset": 690000, "duration": 1680000, "player": "Living room TV"},
    {"id": "b", "item": cat["movies"][3], "offset": 900000, "duration": 7200000, "player": "Phone", "local": False},
]
app = App(os.path.join(tmp, "config"))
app.settings.update({
    "plex": {"url": plex.url, "token": "good-token"},
    "truenas": {"url": tn.url, "api_key": "good-key"},
    "warmup": {"enabled": True},
    "general": {"hdd_mb_per_sec": 182, "hdd_measured_at": "2026-10-07T21:14:00"},
})
now = time.time()
for i, (hit, growth, files) in enumerate([(0.02, 11e9, 25598), (0.64, 171e9, 25611), (0.71, 148e9, 25640)]):
    start = now - (2 - i) * 86400 - 15 * 3600
    app.activity.add_run({"started": start, "finished": start + 9700 - i * 400, "files": files, "total": files,
                          "bytes": 505e9, "hit_rate": hit, "errors": 0, "l2_growth": growth, "result": "ok",
                          "manual": i == 0, "by_kind": {}, "seconds": {}})
app.start()
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8642
httpd = serve(app, "127.0.0.1", port)
print(f"demo on http://127.0.0.1:{port}", flush=True)
httpd.serve_forever()

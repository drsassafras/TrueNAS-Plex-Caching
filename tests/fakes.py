"""Fake Plex and TrueNAS servers plus a fake media tree for tests and demos."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import struct
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import quoteattr

MiB = 1024 * 1024


def make_media(root: str, movies: int = 4, seasons: int = 2, episodes: int = 3, tracks: int = 3) -> dict:
    """Create small sparse media files. Returns a catalogue for FakePlex."""
    cat = {"movies": [], "episodes": [], "tracks": [], "root": root}

    def mk(path, size):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.truncate(size)

    for i in range(movies):
        p = os.path.join(root, "Movies", f"Movie & Friends {i} (20{10 + i})", f"Movie {i}.mkv")
        mk(p, 40 * MiB)
        cat["movies"].append({"path": p, "size": 40 * MiB, "kbps": 18000 + i * 1000, "key": str(100 + i)})
    rk = 1000
    for s in range(1, seasons + 1):
        for e in range(1, episodes + 1):
            p = os.path.join(root, "TV", "Show's Name", f"Season {s:02d}", f"Show S{s:02d}E{e:02d}.mkv")
            mk(p, 12 * MiB)
            cat["episodes"].append({"path": p, "size": 12 * MiB, "kbps": 6500, "season": s, "episode": e, "key": str(rk)})
            rk += 1
    for t in range(tracks):
        p = os.path.join(root, "Audiobooks", "Author", "Book", f"{t:02d}.m4b")
        mk(p, 2 * MiB)
        # Audiobooks often have no bitrate in Plex; the client falls back to size/duration.
        cat["tracks"].append({"path": p, "size": 2 * MiB, "kbps": 0, "duration": 120000, "key": str(5000 + t)})
    return cat


class FakePlex:
    def __init__(self, cat: dict, token: str = "good-token", music_path: str = "/music"):
        self.cat = cat
        self.token = token
        self.sessions: list[dict] = []
        self.requests: list[str] = []
        root = cat["root"]
        self.libraries = [
            ("1", "Movies", "movie", os.path.join(root, "Movies")),
            ("2", "TV Shows", "show", os.path.join(root, "TV")),
            ("3", "Audiobooks", "artist", os.path.join(root, "Audiobooks")),
            ("4", "Music", "artist", music_path),
            ("5", "Photos", "photo", os.path.join(root, "Photos")),
        ]
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                if self.headers.get("X-Plex-Token") != fake.token:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = fake.route(self.path, self.headers)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()

    # ------------------------------------------------------------------
    @staticmethod
    def _part(item, extra=""):
        size = item["size"]
        media_br = f' bitrate="{item["kbps"]}"' if item["kbps"] else ""
        dur = item.get("duration", 0)
        return (f'<Media id="1"{media_br} duration="{dur}"><Part id="1" file={quoteattr(item["path"])} size="{size}"'
                f' duration="{dur}"/></Media>{extra}')

    def _episode_xml(self, ep, extra_attrs=""):
        return (f'<Video type="episode" ratingKey="{ep["key"]}" grandparentRatingKey="500" grandparentTitle="Show&apos;s Name" '
                f'parentIndex="{ep["season"]}" index="{ep["episode"]}" title="Episode {ep["episode"]}" {extra_attrs}>'
                f'{self._part(ep)}</Video>')

    def route(self, path, headers):
        from urllib.parse import parse_qs, urlparse

        u = urlparse(path)
        q = parse_qs(u.query)
        if u.path == "/":
            return '<MediaContainer friendlyName="Test Server" version="1.41.0" machineIdentifier="abc"/>'
        if u.path == "/library/sections":
            dirs = "".join(
                f'<Directory key="{k}" title="{t}" type="{ty}"><Location id="{k}" path={quoteattr(loc)}/></Directory>'
                for k, t, ty, loc in self.libraries
            )
            return f"<MediaContainer>{dirs}</MediaContainer>"
        if u.path.startswith("/library/sections/") and u.path.endswith("/all"):
            key = u.path.split("/")[3]
            typ = q.get("type", [""])[0]
            if key == "1" and typ == "1":
                items = [f'<Video type="movie" ratingKey="{m["key"]}" title="Movie">{self._part(m)}</Video>' for m in self.cat["movies"]]
            elif key == "2" and typ == "4":
                items = [self._episode_xml(e) for e in self.cat["episodes"]]
            elif key == "3" and typ == "10":
                items = [f'<Track type="track" ratingKey="{t["key"]}" title="Chapter">{self._part(t)}</Track>' for t in self.cat["tracks"]]
            elif key == "4" and typ == "10":
                items = []
            else:
                return None
            start = int(headers.get("X-Plex-Container-Start", 0))
            size = int(headers.get("X-Plex-Container-Size", 10 ** 9))
            page = items[start:start + size]
            return f'<MediaContainer size="{len(page)}" totalSize="{len(items)}" offset="{start}">{"".join(page)}</MediaContainer>'
        if u.path == "/library/metadata/500/allLeaves":
            return f'<MediaContainer>{"".join(self._episode_xml(e) for e in self.cat["episodes"])}</MediaContainer>'
        if u.path == "/status/sessions":
            out = []
            for s in self.sessions:
                item = s["item"]
                player = (f'<Player title="{s.get("player", "Living room TV")}" state="playing" local="{1 if s.get("local", True) else 0}"/>'
                          f'<Session id="{s["id"]}"/>')
                markers = ""
                if s.get("credits"):
                    markers = f'<Marker type="credits" startTimeOffset="{s["credits"]}" endTimeOffset="{s["credits"] + 60000}"/>'
                attrs = f'viewOffset="{s["offset"]}" duration="{s["duration"]}" sessionKey="{s["id"]}"'
                if "season" in item:
                    out.append(self._episode_xml(item, attrs).replace("</Video>", player + markers + "</Video>"))
                else:
                    out.append(f'<Video type="movie" ratingKey="{item["key"]}" title="A Movie" {attrs}>{self._part(item)}{player}</Video>')
            return f'<MediaContainer size="{len(out)}">{"".join(out)}</MediaContainer>'
        return None


# ---------------------------------------------------------------------------
# Fake TrueNAS JSON-RPC over TLS WebSocket
# ---------------------------------------------------------------------------

def self_signed_cert(directory: str) -> tuple[str, str]:
    cert, key = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
         "-days", "1", "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    return cert, key


class FakeTrueNAS:
    def __init__(self, api_key: str = "good-key", pool: dict | None = None, dataset_props=("metadata", "all"),
                 workdir: str | None = None):
        self.api_key = api_key
        self.calls: list[str] = []
        self.pool_data = pool or {
            "name": "tank", "status": "ONLINE",
            "topology": {
                "data": [{"type": "RAIDZ1", "children": [{}, {}, {}, {}]}],
                "cache": [
                    {"type": "DISK", "disk": "nvme0n1", "status": "ONLINE", "stats": {"size": 480 * 10 ** 9}},
                    {"type": "DISK", "disk": "nvme1n1", "status": "ONLINE", "stats": {}},
                ],
            },
        }
        self.props = dataset_props
        workdir = workdir or tempfile.mkdtemp()
        cert, key = self_signed_cert(workdir)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        import socket
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.url = f"https://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, raw):
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head = buf.decode("latin-1")
            if "GET /api/current " not in head:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
                return
            key = [ln.split(":", 1)[1].strip() for ln in head.split("\r\n") if ln.lower().startswith("sec-websocket-key")][0]
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
            authed = False
            while True:
                msg = self._recv(conn)
                if msg is None:
                    return
                req = json.loads(msg)
                self.calls.append(req["method"])
                # an unrelated notification first, to test the client skips it
                self._send(conn, json.dumps({"jsonrpc": "2.0", "method": "collection_update", "params": {}}))
                if req["method"] == "auth.login_with_api_key":
                    authed = req["params"][0] == self.api_key
                    result = authed
                elif not authed:
                    self._send(conn, json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": 13, "message": "Not authenticated"}}))
                    continue
                elif req["method"] == "system.version":
                    result = "TrueNAS-25.10.0"
                elif req["method"] == "pool.query":
                    result = [self.pool_data]
                elif req["method"] == "pool.dataset.query":
                    ds = req["params"][0][0][2]
                    result = [{"id": ds, "primarycache": {"value": self.props[0].upper()}, "secondarycache": {"value": self.props[1].upper()}}]
                elif req["method"] == "disk.query":
                    result = [{"name": "nvme1n1", "size": 480 * 10 ** 9}]
                else:
                    result = None
                # A big reply exercises the 16-bit length path.
                self._send(conn, json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result, "pad": "x" * 300}))
        except (OSError, ssl.SSLError, ValueError, IndexError):
            return

    @staticmethod
    def _recv(conn):
        def rd(n):
            out = b""
            while len(out) < n:
                c = conn.recv(n - len(out))
                if not c:
                    raise OSError("closed")
                out += c
            return out
        try:
            b1, b2 = rd(2)
        except OSError:
            return None
        if b1 & 0x0F == 0x8:
            return None
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", rd(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", rd(8))[0]
        mask = rd(4)
        data = rd(n)
        return bytes(b ^ mask[i % 4] for i, b in enumerate(data)).decode()

    @staticmethod
    def _send(conn, text):
        data = text.encode()
        n = len(data)
        head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack("!H", n) if n < 65536 else bytes([127]) + struct.pack("!Q", n))
        conn.sendall(head + data)

    def close(self):
        self.sock.close()


def make_proc(root: str, mount_point: str, dataset: str = "tank/plexfiles", l2_asize: int = 311 * 10 ** 9) -> str:
    """A fake /proc with arcstats and a mountinfo entry for the media dir."""
    proc = os.path.join(root, "proc")
    os.makedirs(os.path.join(proc, "spl/kstat/zfs"), exist_ok=True)
    os.makedirs(os.path.join(proc, "self"), exist_ok=True)
    stats = {"hits": 940000, "misses": 60000, "size": 48 * 2 ** 30, "c_max": 56 * 2 ** 30,
             "l2_hits": 5000, "l2_misses": 2000, "l2_size": l2_asize + 10 ** 9, "l2_asize": l2_asize}
    with open(os.path.join(proc, "spl/kstat/zfs/arcstats"), "w") as fh:
        fh.write("13 1 0x01 147 39984 1 1\nname                            type data\n")
        for k, v in stats.items():
            fh.write(f"{k:<32}4    {v}\n")
    with open(os.path.join(proc, "self/mountinfo"), "w") as fh:
        fh.write("22 1 0:21 / / rw,relatime - overlay overlay rw\n")
        esc = mount_point.replace(" ", "\\040")
        fh.write(f"300 22 0:55 /plexfiles {esc} ro,relatime - zfs {dataset} ro,xattr\n")
    return proc

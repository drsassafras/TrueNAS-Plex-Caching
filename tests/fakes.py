"""Fake Plex and TrueNAS servers plus a fake media tree for tests and demos."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import struct
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import quoteattr

MiB = 1024 * 1024


def _ebml_size(n: int) -> bytes:
    return b"\x01" + n.to_bytes(7, "big")  # 8-byte form, fine for any size


def ebml(id_hex: str, payload: bytes) -> bytes:
    return bytes.fromhex(id_hex) + _ebml_size(len(payload)) + payload


def make_mkv(path: str, size: int, cues_size: int = 4000, second_seekhead: bool = False, tags_size: int = 0):
    """A sparse file laid out like a real MKV: header and SeekHead at the start,
    clusters in the middle, Cues (and optionally Tags) at the end."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    header = ebml("1A45DFA3", ebml("4282", b"matroska"))
    seg_start = len(header) + 4 + 8
    cues = ebml("1C53BB6B", b"\x00" * cues_size)
    tags = ebml("1254C367", b"\x00" * tags_size) if tags_size else b""
    tags_at = size - len(cues) - len(tags)
    cues_at = size - len(cues)
    seek = lambda eid, at: ebml("4DBB", ebml("53AB", bytes.fromhex(eid)) + ebml("53AC", (at - seg_start).to_bytes(8, "big")))
    tail_head = b""
    if second_seekhead:
        # The first SeekHead points to a second one just before the end, which lists the Cues.
        tail_head = ebml("114D9B74", seek("1C53BB6B", cues_at))
        tail_head_at = tags_at - len(tail_head)
        first = ebml("114D9B74", seek("114D9B74", tail_head_at) + (seek("1254C367", tags_at) if tags else b""))
    else:
        first = ebml("114D9B74", seek("1C53BB6B", cues_at) + (seek("1254C367", tags_at) if tags else b""))
    body_start = seg_start + len(first)
    end_of_clusters = tags_at - len(tail_head)
    cluster = bytes.fromhex("1F43B675") + _ebml_size(end_of_clusters - body_start - 12)
    with open(path, "wb") as fh:
        fh.write(header + bytes.fromhex("18538067") + _ebml_size(size - seg_start) + first + cluster)
        fh.truncate(size)
        if tail_head:
            fh.seek(tags_at - len(tail_head))
            fh.write(tail_head)
        fh.seek(tags_at)
        fh.write(tags + cues)
    return {"cues_at": cues_at, "cues_len": len(cues), "tags_at": tags_at, "tags_len": len(tags)}


def make_mp4(path: str, size: int, moov_size: int = 300_000, faststart: bool = False):
    """A sparse MP4: ftyp, then moov before or after the media data."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ftyp = struct.pack(">I4s", 24, b"ftyp") + b"isom" + b"\x00\x00\x02\x00" + b"isomiso2"
    moov = struct.pack(">I4s", moov_size, b"moov")
    mdat_size = size - len(ftyp) - moov_size
    mdat = struct.pack(">I4s", mdat_size, b"mdat")
    with open(path, "wb") as fh:
        fh.truncate(size)
        fh.seek(0)
        if faststart:
            fh.write(ftyp + moov)
            fh.seek(len(ftyp) + moov_size)
            fh.write(mdat)
        else:
            fh.write(ftyp + mdat)
            fh.seek(len(ftyp) + mdat_size)
            fh.write(moov)
    return {"moov_at": size - moov_size if not faststart else len(ftyp), "moov_len": moov_size}


def make_media(root: str, movies: int = 4, seasons: int = 2, episodes: int = 3, tracks: int = 3) -> dict:
    """Create small sparse media files. Returns a catalogue for FakePlex."""
    cat = {"movies": [], "episodes": [], "tracks": [], "tracks_b": [], "root": root}

    def mk(path, size):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.truncate(size)

    for i in range(movies):
        p = os.path.join(root, "Movies", f"Movie & Friends {i} (20{10 + i})", f"Movie {i}.mkv")
        make_mkv(p, 40 * MiB, cues_size=60_000)
        cat["movies"].append({"path": p, "size": 40 * MiB, "kbps": 18000 + i * 1000, "key": str(100 + i)})
    rk = 1000
    for s in range(1, seasons + 1):
        for e in range(1, episodes + 1):
            p = os.path.join(root, "TV", "Show's Name", f"Season {s:02d}", f"Show S{s:02d}E{e:02d}.mkv")
            make_mkv(p, 12 * MiB, cues_size=20_000)
            cat["episodes"].append({"path": p, "size": 12 * MiB, "kbps": 6500, "season": s, "episode": e, "key": str(rk)})
            rk += 1
    for t in range(tracks):
        p = os.path.join(root, "Audiobooks", "Author", "Book", f"{t:02d}.m4b")
        mk(p, 2 * MiB)
        # Audiobooks often have no bitrate in Plex; the client falls back to size/duration.
        cat["tracks"].append({"path": p, "size": 2 * MiB, "kbps": 0, "duration": 120000, "key": str(5000 + t)})
    # A second book by the same author (not in the library listing; only reached through the author).
    for t in range(2):
        p = os.path.join(root, "Audiobooks", "Author", "Book 2", f"{t:02d}.m4b")
        mk(p, 2 * MiB)
        cat["tracks_b"].append({"path": p, "size": 2 * MiB, "kbps": 0, "duration": 120000, "key": str(5100 + t)})
    return cat


class FakePlex:
    def __init__(self, cat: dict, token: str = "good-token", music_path: str = "/music", report_root: str | None = None):
        self.cat = cat
        self.token = token
        # Pretend Plex sees the media under a different folder (like /data inside its own app).
        self.report_root = report_root
        # Names shown in the UI (the demo swaps in nicer ones for screenshots).
        self.show_name = "Show's Name"
        self.author = "Author"
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
                if self.path.split("?")[0] == "/identity":  # Plex answers this without a token
                    data = b'<MediaContainer size="0" machineIdentifier="abc" version="1.41.0"/>'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.headers.get("X-Plex-Token") != fake.token:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = fake.route(self.path, self.headers)
                if body is not None and fake.report_root:
                    body = body.replace(fake.cat["root"], fake.report_root)
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

            def do_PUT(self):
                fake.requests.append("PUT " + self.path)
                if self.headers.get("X-Plex-Token") != fake.token:
                    self.send_response(401)
                    self.end_headers()
                    return
                ok = fake.put(self.path)
                self.send_response(200 if ok else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.requests.append("POST " + self.path)
                fake.posts = getattr(fake, "posts", []) + [(self.path, body)]
                parts = self.path.split("?")[0].split("/")
                if len(parts) == 5 and parts[4] in ("posters", "arts"):  # an image uploaded: chosen by hand now
                    m = next((m for m in fake.cat["movies"] if m["key"] == parts[3]), None)
                    if m is not None:
                        m["locked"] = sorted(set(m.get("locked", [])) | {"thumb" if parts[4] == "posters" else "art"})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------------
    @staticmethod
    def _part(item, extra=""):
        size = item["size"]
        media_br = f' bitrate="{item["kbps"]}"' if item["kbps"] else ""
        dur = item.get("duration", 0)
        out = (f'<Media id="1"{media_br} duration="{dur}"><Part id="1" file={quoteattr(item["path"])} size="{size}"'
               f' duration="{dur}"/></Media>')
        for i, p in enumerate(item.get("more", [])):  # more versions (paths) on the same record
            out += f'<Media id="{i + 2}"{media_br} duration="{dur}"><Part id="{i + 2}" file={quoteattr(p)} size="{size}" duration="{dur}"/></Media>'
        return out + extra

    def put(self, path) -> bool:
        """Plex's PUTs: a merge (the others' files become versions of the one kept), empty trash; an
        edit (fields and locks taking effect on the record), a match, a poster chosen."""
        from urllib.parse import parse_qs, urlparse
        u = urlparse(path)
        q = parse_qs(u.query)
        parts = u.path.split("/")
        self.puts = getattr(self, "puts", []) + [(u.path, {k: v if len(v) > 1 else v[0] for k, v in q.items()})]
        if u.path.startswith("/library/sections/") and u.path.endswith("/all") and q.get("id"):  # an edit
            m = next((m for m in self.cat["movies"] if m["key"] == q["id"][0]), None)
            if m is None:
                return False
            for k, v in q.items():
                if k.endswith(".value"):
                    m[k[:-6]] = v[0] if k[:-6] != "year" else int(v[0])
                elif k.endswith(".locked") and "[" not in k:
                    f, locked = k[:-7], set(m.get("locked", []))
                    (locked.add if v[0] == "1" else locked.discard)(f)
                    m["locked"] = sorted(locked)
                elif k.endswith("].tag.tag") or k.endswith("].tag.tag-"):  # a tag field: "collection[0].tag.tag" added, "collection[].tag.tag-" taken off
                    field, plural = k.split("[")[0], {"collection": "collections", "label": "labels"}
                    lst = m.setdefault(plural.get(field, field), [])
                    for t in v[0].split(","):
                        if k.endswith("-"):
                            [lst.remove(t) for _ in range(lst.count(t))]
                        elif t not in lst:
                            lst.append(t)
                    lst.sort()
            return True
        if len(parts) == 5 and parts[1:3] == ["library", "metadata"] and parts[4] in ("match", "unmatch"):
            m = next((m for m in self.cat["movies"] if m["key"] == parts[3]), None)
            if m is None:
                return False
            m["guid"] = q["guid"][0] if parts[4] == "match" else f"local://{m['key']}"
            return True
        if len(parts) == 5 and parts[1:3] == ["library", "metadata"] and parts[4] in ("poster", "art"):
            return True
        if len(parts) == 5 and parts[1:3] == ["library", "metadata"] and parts[4] == "merge":
            keep = next((m for m in self.cat["movies"] if m["key"] == parts[3]), None)
            if keep is None:
                return False
            for k in q.get("ids", [""])[0].split(","):
                other = next((m for m in self.cat["movies"] if m["key"] == k and m is not keep), None)
                if other is not None:
                    keep.setdefault("more", []).extend([other["path"]] + other.get("more", []))
                    self.cat["movies"].remove(other)
            self.merged = getattr(self, "merged", []) + [(parts[3], q.get("ids", [""])[0])]
            return True
        return len(parts) == 5 and parts[1:3] == ["library", "sections"] and parts[4] == "emptyTrash"

    def _episode_xml(self, ep, extra_attrs=""):
        return (f'<Video type="episode" ratingKey="{ep["key"]}" grandparentRatingKey="500" grandparentTitle={quoteattr(self.show_name)} '
                f'parentIndex="{ep["season"]}" index="{ep["episode"]}" title={quoteattr(ep.get("title") or "Episode " + str(ep["episode"]))} {extra_attrs}>'
                f'{self._part(ep)}</Video>')

    def route(self, path, headers):
        from urllib.parse import parse_qs, urlparse

        u = urlparse(path)
        q = parse_qs(u.query)
        if u.path == "/":
            return '<MediaContainer friendlyName="Test Server" version="1.41.0" machineIdentifier="abc"/>'
        if u.path == "/library/sections":
            dirs = "".join(
                f'<Directory key="{k}" title="{t}" type="{ty}">'
                + "".join(f'<Location id="{k}{i}" path={quoteattr(p)}/>' for i, p in enumerate(loc.split("|")))
                + "</Directory>"
                for k, t, ty, loc in self.libraries
            )
            return f"<MediaContainer>{dirs}</MediaContainer>"
        if u.path.startswith("/library/sections/") and u.path.endswith("/refresh"):  # a folder scanned: noted
            self.scanned = getattr(self, "scanned", []) + [q.get("path", [""])[0]]
            if getattr(self, "on_scan", None):  # (a test's Plex "finding" files there)
                self.on_scan(q.get("path", [""])[0])
            return "<MediaContainer/>"
        if u.path.startswith("/library/sections/") and u.path.endswith("/all"):
            key = u.path.split("/")[3]
            typ = q.get("type", [""])[0]
            if key == "1" and typ == "1":
                movies = [m for m in self.cat["movies"] if not q.get("title") or m.get("title", "Movie") == q["title"][0]]
                items = [f'<Video type="movie" ratingKey="{m["key"]}" title={quoteattr(m.get("title", "Movie"))}'
                         + (f' year="{m["year"]}"' if m.get("year") else "") + (f' guid={quoteattr(m["guid"])}' if m.get("guid") else "")
                         + (f' editionTitle={quoteattr(m["edition"])}' if m.get("edition") else "")
                         + (f' thumb="/library/metadata/{m["key"]}/thumb/1"' if m.get("guid") else "") + ">"
                         + "".join(f'<Guid id={quoteattr(g)}/>' for g in m.get("guids", []))
                         + "".join(f'<Field locked="1" name="{n}"/>' for n in m.get("locked", []))
                         + "".join(f'<Collection tag={quoteattr(c)}/>' for c in m.get("collections", [])) + "".join(f'<Label tag={quoteattr(c)}/>' for c in m.get("labels", []))
                         + f"{self._part(m)}</Video>" for m in movies]
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
        if u.path in ("/library/metadata/600/children", "/library/metadata/601/children"):  # a book's tracks, in order
            book, name = (self.cat["tracks"], "Chapter") if "600" in u.path else (self.cat["tracks_b"], "Book 2, chapter")
            return "<MediaContainer>" + "".join(
                f'<Track type="track" ratingKey="{t["key"]}" index="{i + 1}" title="{name} {i + 1}" '
                f'duration="{t["duration"]}">{self._part(t)}</Track>' for i, t in enumerate(book)) + "</MediaContainer>"
        if u.path == "/library/metadata/700/children":  # the author's books
            return '<MediaContainer><Directory type="album" ratingKey="600"/><Directory type="album" ratingKey="601"/></MediaContainer>'
        # Movies 0-2 are a collection ("Trilogy"); the last movie is in none.
        if u.path == "/library/sections/1/collections":
            return '<MediaContainer><Directory ratingKey="900" title="Trilogy" type="collection"/></MediaContainer>'
        if u.path == "/library/collections/900/children":
            return "<MediaContainer>" + "".join(
                f'<Video type="movie" ratingKey="{m["key"]}" title="Part {i + 1}">{self._part(m)}</Video>'
                for i, m in enumerate(self.cat["movies"][:3])) + "</MediaContainer>"
        if u.path.startswith("/library/metadata/") and u.path.endswith("/thumb/1"):  # a poster's bytes
            return "IMG"
        if u.path.startswith("/library/metadata/") and u.path.endswith(("/posters", "/arts")):  # an item's images, the chosen one first
            return '<MediaContainer><Photo ratingKey="upload://posters/old" selected="1" thumb="/library/metadata/x/thumb/1"/><Photo ratingKey="metadata://posters/other" thumb="/t2"/></MediaContainer>'
        movie = next((m for m in self.cat["movies"] if u.path == f'/library/metadata/{m["key"]}'), None)
        if movie is not None and movie.get("guid"):  # (a test's own records: their page as the listing gives it)
            from urllib.parse import quote
            return self.route("/library/sections/1/all?type=1&title=" + quote(movie.get("title", "Movie")), headers)
        if movie is not None:
            tag = '<Collection id="77" tag="Trilogy"/>' if self.cat["movies"].index(movie) < 3 else ""
            return f'<MediaContainer><Video type="movie" ratingKey="{movie["key"]}" title="Movie">{self._part(movie)}{tag}</Video></MediaContainer>'
        if u.path == "/library/metadata/500/allLeaves":
            return f'<MediaContainer>{"".join(self._episode_xml(e) for e in self.cat["episodes"])}</MediaContainer>'
        if u.path == "/status/sessions":
            out = []
            for s in self.sessions:
                item = s["item"]
                player = (f'<Player title="{s.get("player", "Living room TV")}" state="{s.get("state", "playing")}" local="{1 if s.get("local", True) else 0}"/>'
                          f'<Session id="{s["id"]}"/>')
                markers = ""
                if s.get("credits"):
                    markers = f'<Marker type="credits" startTimeOffset="{s["credits"]}" endTimeOffset="{s["credits"] + 60000}"/>'
                section = "2" if "season" in item else "1"
                attrs = (f'viewOffset="{s["offset"]}" duration="{s["duration"]}" sessionKey="{s["id"]}" '
                         f'librarySectionID="{section}"')
                if "season" in item:
                    out.append(self._episode_xml(item, attrs).replace("</Video>", player + markers + "</Video>"))
                elif item["path"].endswith(".m4b"):
                    attrs = attrs.replace('librarySectionID="1"', 'librarySectionID="3"')
                    out.append(f'<Track type="track" ratingKey="{item["key"]}" parentRatingKey="600" grandparentRatingKey="700" grandparentTitle={quoteattr(self.author)} '
                               f'title="Chapter" {attrs}>{self._part(item)}{player}</Track>')
                else:
                    out.append(f'<Video type="movie" ratingKey="{item["key"]}" title={quoteattr(item.get("title", "A Movie"))} {attrs}>{self._part(item)}{player}</Video>')
            return f'<MediaContainer size="{len(out)}">{"".join(out)}</MediaContainer>'
        return None


class FakePlexTV:
    """plex.tv's PIN sign-in: POST /api/v2/pins gives an id and code; GET /api/v2/pins/<id>
    gives the token once the user has approved (``approved``)."""

    def __init__(self):
        self.approved = False
        self.fail = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, data):
                if fake.fail:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self._reply({"id": 4242, "code": "ABCD1234"})

            def do_GET(self):
                self._reply({"id": 4242, "authToken": "good-token" if fake.approved else None})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------
# Fake TrueNAS JSON-RPC over TLS WebSocket
# ---------------------------------------------------------------------------

def self_signed_cert(directory: str) -> tuple[str, str]:
    """A certificate for the fakes' TLS: the one in tests/fixtures (self-signed, for "localhost", only
    ever used by these fakes), so no openssl is needed — the app's image has none."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    return os.path.join(here, "localhost.crt"), os.path.join(here, "localhost.key")


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
        self.per_dataset: dict[str, dict] = {}  # dataset -> {"primarycache":..., "recordsize": bytes, ...}
        self.recordsize, self.recordsize_bytes = "128K", 131072
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
        conn = raw
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
                elif req["method"] == "test.error":
                    self._send(conn, json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                                 "error": {"code": 22, "message": "EINVAL", "data": {"reason": "no such thing"}}}))
                    continue
                elif req["method"] == "test.garbage":
                    self._send(conn, "this is not json")
                    continue
                elif req["method"] == "system.version":
                    result = "TrueNAS-25.10.0"
                elif req["method"] == "pool.query":
                    result = [self.pool_data]
                elif req["method"] == "pool.dataset.query":
                    ds = req["params"][0][0][2]
                    # Like a real system: common fields only, no primarycache/secondarycache.
                    result = [{"id": ds, "name": ds, "compression": {"value": "LZ4", "rawvalue": "lz4"},
                               "recordsize": {"value": self.recordsize, "rawvalue": str(self.recordsize_bytes)}}]
                elif req["method"] == "zfs.dataset.query":
                    ds = req["params"][0][0][2]
                    wanted = ((req["params"][1] if len(req["params"]) > 1 else {}).get("extra") or {}).get("properties") or []
                    o = self.per_dataset.get(ds, {})
                    pc, sc = o.get("primarycache", self.props[0]), o.get("secondarycache", self.props[1])
                    rsb = o.get("recordsize", self.recordsize_bytes)
                    rsv = f"{rsb // 1048576}M" if rsb >= 1048576 else f"{rsb // 1024}K"
                    allp = {"primarycache": {"value": pc, "rawvalue": pc, "source": "LOCAL"},
                            "secondarycache": {"value": sc, "rawvalue": sc, "source": "INHERITED"},
                            "recordsize": {"value": rsv, "rawvalue": str(rsb), "source": "LOCAL"}}
                    result = [{"id": ds, "name": ds, "properties": {k: v for k, v in allp.items() if k in wanted}}]
                elif req["method"] == "disk.query":
                    result = [{"name": "nvme1n1", "size": 480 * 10 ** 9}]
                else:
                    result = None
                # A big reply exercises the 16-bit length path.
                self._send(conn, json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result, "pad": "x" * 300}))
        except (OSError, ssl.SSLError, ValueError, IndexError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

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


def make_proc(root: str, mount_point: str, dataset: str = "tank/plexfiles", l2_asize: int = 311 * 10 ** 9,
              extra: tuple = ()) -> str:
    """A fake /proc with arcstats and a mountinfo entry for the media dir.

    ``extra`` adds more (mount point, dataset) pairs, e.g. a child dataset."""
    proc = os.path.join(root, "proc")
    os.makedirs(os.path.join(proc, "spl/kstat/zfs"), exist_ok=True)
    os.makedirs(os.path.join(proc, "self"), exist_ok=True)
    stats = {"hits": 940000, "misses": 60000, "size": 48 * 2 ** 30, "c_max": 56 * 2 ** 30,
             "memory_free_bytes": 8 * 2 ** 30, "l2_hits": 5000, "l2_misses": 2000, "l2_size": l2_asize + 10 ** 9, "l2_asize": l2_asize}
    with open(os.path.join(proc, "spl/kstat/zfs/arcstats"), "w") as fh:
        fh.write("13 1 0x01 147 39984 1 1\nname                            type data\n")
        for k, v in stats.items():
            fh.write(f"{k:<32}4    {v}\n")
    with open(os.path.join(proc, "self/mountinfo"), "w") as fh:
        fh.write("22 1 0:21 / / rw,relatime - overlay overlay rw\n")
        esc = mount_point.replace(" ", "\\040")
        fh.write(f"300 22 0:55 /plexfiles {esc} ro,relatime - zfs {dataset} ro,xattr\n")
        for i, (mp, ds) in enumerate(extra):
            esc = mp.replace(" ", "\\040")
            fh.write(f"{301 + i} 22 0:{56 + i} / {esc} ro,relatime - zfs {ds} ro,xattr\n")
    return proc


def make_sys(root: str, params: dict) -> str:
    """A fake /sys with ZFS module parameters."""
    sysdir = os.path.join(root, "sys")
    pdir = os.path.join(sysdir, "module/zfs/parameters")
    os.makedirs(pdir, exist_ok=True)
    for k, v in params.items():
        with open(os.path.join(pdir, k), "w") as fh:
            fh.write(f"{v}\n")
    return sysdir


# ------------------------------------------------------------------ MQTT broker
class FakeMQTTBroker:
    """A tiny MQTT 3.1.1 broker: one login, retained messages, subscriptions, last wills."""

    def __init__(self, username="ha", password="secret", tls_dir=None):
        import socket as _s
        self.username, self.password = username, password
        self.tls = None
        if tls_dir:  # TLS on, with a self-signed certificate
            cert, key = self_signed_cert(tls_dir)
            self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.tls.load_cert_chain(cert, key)
        self.retained: dict = {}
        self.published: list = []  # (topic, payload, retain) in arrival order
        self.connects: list = []  # (client_id, username, accepted)
        self.clients: list = []  # [sock, [patterns], will]
        self.active = 0  # connections still being served (including ones winding down)
        self.lock = threading.Lock()
        self.srv = _s.socket()
        self.srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.closed = True
        self.srv.close()
        with self.lock:
            for c in self.clients:
                try:
                    c[0].close()
                except OSError:
                    pass

    def drop_clients(self):
        """Cut every connection without a goodbye (as if the network failed)."""
        with self.lock:
            clients, self.clients = self.clients, []
        for sock, _, will in clients:
            try:
                sock.shutdown(2)
                sock.close()
            except OSError:
                pass
            if will:
                self._route(*will)

    def reset(self, timeout=10.0):
        """Cut every connection, wait until they've all finished, then forget everything."""
        import time as _t
        self.drop_clients()
        end = _t.time() + timeout
        while self.active and _t.time() < end:
            _t.sleep(0.02)
        with self.lock:
            self.retained.clear()
            self.published.clear()
            self.connects.clear()

    def inject(self, topic, payload):
        """Publish as if another client (Home Assistant) sent it."""
        self._route(topic, payload.encode() if isinstance(payload, str) else payload, False)

    def state(self, topic):
        with self.lock:
            v = self.retained.get(topic)
        return v

    @staticmethod
    def _str(data, i):
        n = struct.unpack("!H", data[i:i + 2])[0]
        return data[i + 2:i + 2 + n], i + 2 + n

    @staticmethod
    def _packet(kind, body):
        n, length = len(body), b""
        while True:
            byte, n = n % 128, n // 128
            length += bytes([byte | (0x80 if n else 0)])
            if not n:
                break
        return bytes([kind]) + length + body

    def _accept(self):
        while not self.closed:
            try:
                sock, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _recv(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def _read(self, sock):
        kind = self._recv(sock, 1)[0]
        mult, length = 1, 0
        while True:
            b = self._recv(sock, 1)[0]
            length += (b & 0x7F) * mult
            mult *= 128
            if not b & 0x80:
                break
        return kind, self._recv(sock, length) if length else b""

    def _route(self, topic, payload, retain):
        from plexcache.mqttclient import topic_matches
        with self.lock:
            self.published.append((topic, payload, retain))
            if retain:
                if payload:
                    self.retained[topic] = payload
                else:
                    self.retained.pop(topic, None)
            targets = [c[0] for c in self.clients if any(topic_matches(p, topic) for p in c[1])]
        body = struct.pack("!H", len(topic.encode())) + topic.encode() + payload
        for s in targets:
            try:
                s.sendall(self._packet(0x30, body))
            except OSError:
                pass

    def _serve(self, sock):
        from plexcache.mqttclient import topic_matches
        entry = None
        with self.lock:
            self.active += 1
        try:
            if self.tls:
                sock = self.tls.wrap_socket(sock, server_side=True)
            kind, data = self._read(sock)
            if kind != 0x10:
                return
            _, i = self._str(data, 0)
            flags = data[i + 1]
            i += 4
            cid, i = self._str(data, i)
            will = None
            if flags & 0x04:
                wt, i = self._str(data, i)
                wm, i = self._str(data, i)
                will = (wt.decode(), wm, bool(flags & 0x20))
            user = pw = b""
            if flags & 0x80:
                user, i = self._str(data, i)
            if flags & 0x40:
                pw, i = self._str(data, i)
            ok = user.decode() == self.username and pw.decode() == self.password
            self.connects.append((cid.decode(), user.decode(), ok))
            sock.sendall(self._packet(0x20, bytes([0, 0 if ok else 5])))
            if not ok:
                return
            entry = [sock, [], will]
            with self.lock:
                self.clients.append(entry)
            while True:
                kind, data = self._read(sock)
                t = kind & 0xF0
                if t == 0x30:
                    n = struct.unpack("!H", data[:2])[0]
                    self._route(data[2:2 + n].decode(), data[2 + n:], bool(kind & 1))
                elif t == 0x80:
                    pid, j, topics = data[:2], 2, []
                    while j < len(data):
                        tp, j = self._str(data, j)
                        j += 1
                        topics.append(tp.decode())
                    entry[1].extend(topics)
                    sock.sendall(self._packet(0x90, pid + bytes([0] * len(topics))))
                    with self.lock:
                        matches = [(tp, v) for tp, v in self.retained.items()
                                   for p in topics if topic_matches(p, tp)]
                    for tp, v in matches:
                        sock.sendall(self._packet(0x31, struct.pack("!H", len(tp.encode())) + tp.encode() + v))
                elif t == 0xC0:
                    sock.sendall(self._packet(0xD0, b""))
                elif t == 0xE0:
                    entry[2] = None  # a clean goodbye: no last will
                    return
        except (ConnectionError, OSError, IndexError, struct.error, ssl.SSLError):
            pass
        finally:
            with self.lock:
                gone = entry in self.clients
                if gone:
                    self.clients.remove(entry)
            if entry and gone and entry[2]:
                self._route(*entry[2])
            try:
                sock.close()
            except OSError:
                pass
            with self.lock:
                self.active -= 1

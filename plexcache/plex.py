"""Minimal Plex Media Server client (standard library only)."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import __version__

PRODUCT = "Plex Cache"
PAGE_SIZE = 500

# Plex library type -> our media kind
KIND_BY_LIBRARY_TYPE = {"movie": "movie", "show": "tv", "artist": "audio"}
# Plex search type number used to list leaf items of each library type
LEAF_TYPE = {"movie": 1, "show": 4, "artist": 10}


class PlexError(Exception):
    pass


@dataclass
class MediaFile:
    path: str
    kbps: int
    size: int
    kind: str  # movie | tv | audio
    library: str = ""
    library_key: str = ""


@dataclass
class Library:
    key: str
    title: str
    type: str
    kind: str | None
    locations: list[str] = field(default_factory=list)


@dataclass
class Session:
    session_key: str
    rating_key: str
    type: str  # movie | episode | track | ...
    title: str
    grandparent_title: str
    grandparent_rating_key: str
    season: int | None
    episode: int | None
    view_offset_ms: int
    duration_ms: int
    kbps: int
    files: list[tuple[str, int]]  # (path, size)
    player: str
    state: str
    local: bool
    transcoding: bool
    credits_start_ms: int | None
    library_key: str = ""

    @property
    def progress(self) -> float:
        return (self.view_offset_ms / self.duration_ms) if self.duration_ms else 0.0

    @property
    def label(self) -> str:
        if self.type == "episode" and self.season is not None and self.episode is not None:
            return f"{self.grandparent_title} · S{self.season:02d}E{self.episode:02d}"
        if self.grandparent_title:
            return f"{self.grandparent_title} · {self.title}"
        return self.title


def _int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def media_kbps(media: ET.Element, part: ET.Element) -> int:
    """Bitrate in kbps; falls back to size/duration when Plex has none."""
    kbps = _int(media.get("bitrate"))
    if kbps <= 0:
        size = _int(part.get("size"))
        duration_ms = _int(part.get("duration")) or _int(media.get("duration"))
        if size > 0 and duration_ms > 0:
            kbps = int(size * 8 / duration_ms)  # bytes*8 / ms == kbit/s
    return kbps


class PlexClient:
    def __init__(self, url: str, token: str, timeout: float = 30):
        if not url:
            raise PlexError("Plex server address is not set")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Plex's https uses plex.direct certificates that won't match a LAN IP.
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------------ http
    def _request(self, path: str, params: dict | None = None, headers: dict | None = None,
                 auth: bool = True) -> bytes:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.url}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/xml")
        req.add_header("X-Plex-Product", PRODUCT)
        req.add_header("X-Plex-Version", __version__)
        if self.token and auth:
            req.add_header("X-Plex-Token", self.token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            e.close()  # it holds the connection open
            if e.code == 401:
                raise PlexError("Plex rejected the token (401 Unauthorized)") from None
            raise PlexError(f"Plex returned HTTP {e.code} for {path}") from None
        except urllib.error.URLError as e:
            raise PlexError(f"Can't reach Plex at {self.url}: {e.reason}") from None
        except (TimeoutError, OSError) as e:
            raise PlexError(f"Can't reach Plex at {self.url}: {e}") from None

    def get(self, path: str, params: dict | None = None, headers: dict | None = None) -> ET.Element:
        body = self._request(path, params, headers)
        try:
            return ET.fromstring(body)
        except ET.ParseError:
            raise PlexError(f"Plex returned something that isn't XML for {path}") from None

    # --------------------------------------------------------------- server
    def identity(self) -> dict:
        """Is a Plex server answering here? Works without a token."""
        start = time.monotonic()
        body = self._request("/identity", auth=False)
        ms = int((time.monotonic() - start) * 1000)
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise PlexError(f"Something answered at {self.url}, but it isn't Plex") from None
        if not root.get("machineIdentifier"):
            raise PlexError(f"Something answered at {self.url}, but it isn't Plex")
        return {"version": root.get("version", ""), "machine_id": root.get("machineIdentifier", ""), "ms": ms}

    def ping(self) -> dict:
        start = time.monotonic()
        root = self.get("/")
        ms = int((time.monotonic() - start) * 1000)
        return {
            "name": root.get("friendlyName", ""),
            "version": root.get("version", ""),
            "machine_id": root.get("machineIdentifier", ""),
            "ms": ms,
        }

    def libraries(self) -> list[Library]:
        root = self.get("/library/sections")
        libs = []
        for d in root.findall("Directory"):
            libs.append(
                Library(
                    key=d.get("key", ""),
                    title=d.get("title", ""),
                    type=d.get("type", ""),
                    kind=KIND_BY_LIBRARY_TYPE.get(d.get("type", "")),
                    locations=[loc.get("path", "") for loc in d.findall("Location") if loc.get("path")],
                )
            )
        return libs

    def library_files(self, lib: Library, limit: int | None = None) -> list[MediaFile]:
        """Every playable file in a library with its bitrate (paged)."""
        if lib.kind is None:
            return []
        leaf = LEAF_TYPE[lib.type]
        out: list[MediaFile] = []
        start = 0
        while True:
            size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(out))
            if size <= 0:
                break
            root = self.get(
                f"/library/sections/{lib.key}/all",
                {"type": leaf, "includeGuids": 0},
                {"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(size)},
            )
            items = list(root)
            for item in items:
                for media in item.findall("Media"):
                    for part in media.findall("Part"):
                        path = part.get("file")
                        if path:
                            out.append(MediaFile(path, media_kbps(media, part), _int(part.get("size")), lib.kind, lib.title, lib.key))
            total = _int(root.get("totalSize"), -1)
            start += len(items)
            if not items or len(items) < size or (total >= 0 and start >= total):
                break
        return out[:limit] if limit else out

    # -------------------------------------------------------------- playback
    def sessions(self) -> list[Session]:
        root = self.get("/status/sessions", {"includeMarkers": 1})
        out = []
        for item in root:
            if item.tag not in ("Video", "Track"):
                continue
            files: list[tuple[str, int]] = []
            kbps = 0
            for media in item.findall("Media"):
                # When transcoding Plex lists the original media and a virtual
                # transcode one without a file; keep the ones with files.
                for part in media.findall("Part"):
                    if part.get("file"):
                        files.append((part.get("file"), _int(part.get("size"))))
                        kbps = kbps or media_kbps(media, part)
            player = item.find("Player")
            sess = item.find("Session")
            credits = None
            for marker in item.findall("Marker"):
                if marker.get("type") == "credits":
                    start = _int(marker.get("startTimeOffset"), -1)
                    if start >= 0 and (credits is None or start < credits):
                        credits = start
            out.append(
                Session(
                    session_key=(sess.get("id") if sess is not None else "") or item.get("sessionKey", ""),
                    rating_key=item.get("ratingKey", ""),
                    type=item.get("type", ""),
                    title=item.get("title", ""),
                    grandparent_title=item.get("grandparentTitle", ""),
                    grandparent_rating_key=item.get("grandparentRatingKey", ""),
                    season=_int(item.get("parentIndex"), None) if item.get("parentIndex") else None,
                    episode=_int(item.get("index"), None) if item.get("index") else None,
                    view_offset_ms=_int(item.get("viewOffset")),
                    duration_ms=_int(item.get("duration")),
                    kbps=kbps,
                    files=files,
                    player=(player.get("title") or player.get("product") or "") if player is not None else "",
                    state=player.get("state", "") if player is not None else "",
                    local=(player.get("local") == "1") if player is not None else True,
                    transcoding=item.find("TranscodeSession") is not None,
                    credits_start_ms=credits,
                    library_key=item.get("librarySectionID", ""),
                )
            )
        return out

    def next_episode(self, rating_key: str, show_rating_key: str) -> dict | None:
        """The episode after ``rating_key`` in Plex's own order (crosses seasons)."""
        root = self.get(f"/library/metadata/{show_rating_key}/allLeaves")
        episodes = [v for v in root.findall("Video")]
        # allLeaves is ordered by season then episode; specials (season 0)
        # come first and are skipped unless we are watching specials.
        for i, ep in enumerate(episodes):
            if ep.get("ratingKey") == rating_key:
                current_season = _int(ep.get("parentIndex"), -1)
                for nxt in episodes[i + 1:]:
                    if _int(nxt.get("parentIndex"), -1) == 0 and current_season != 0:
                        continue
                    files = []
                    for media in nxt.findall("Media"):
                        for part in media.findall("Part"):
                            if part.get("file"):
                                files.append((part.get("file"), _int(part.get("size")), media_kbps(media, part)))
                    return {
                        "rating_key": nxt.get("ratingKey", ""),
                        "season": _int(nxt.get("parentIndex"), 0),
                        "episode": _int(nxt.get("index"), 0),
                        "title": nxt.get("title", ""),
                        "files": files,
                    }
                return None
        return None


# ---------------------------------------------------------------------------
# "Sign in with Plex" (PIN flow against plex.tv)
# ---------------------------------------------------------------------------

PLEX_TV = "https://plex.tv"


def _plextv(method: str, path: str, client_id: str, data: bytes | None = None) -> dict:
    req = urllib.request.Request(PLEX_TV + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("X-Plex-Product", PRODUCT)
    req.add_header("X-Plex-Version", __version__)
    req.add_header("X-Plex-Client-Identifier", client_id)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        e.close()
        raise PlexError(f"plex.tv returned HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise PlexError(f"Can't reach plex.tv: {e}") from None


def create_pin(client_id: str) -> dict:
    pin = _plextv("POST", "/api/v2/pins?strong=true", client_id, data=b"")
    params = urllib.parse.urlencode(
        {"clientID": client_id, "code": pin["code"], "context[device][product]": PRODUCT}
    )
    return {"id": pin["id"], "auth_url": f"https://app.plex.tv/auth#?{params}"}


def check_pin(client_id: str, pin_id: int) -> str | None:
    pin = _plextv("GET", f"/api/v2/pins/{int(pin_id)}", client_id)
    return pin.get("authToken") or None

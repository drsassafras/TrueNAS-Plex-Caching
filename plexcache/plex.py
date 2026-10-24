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
from .i18n import M

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
    # Plex's poster for it (a path on the server, with a stamp that changes when the poster
    # does): a movie's own, an episode's season poster (the show's when the season has none)
    poster: str = ""
    # Whether Plex matched it to an online entry (False: unmatched, its own "local://" item;
    # None: Plex didn't say)
    matched: bool | None = None
    # How Plex names it: "Title (year)", "Show · S01E05", "Album · Track"
    title: str = ""
    # The Plex item it belongs to (a movie, an episode): a second version of it shares this
    rating_key: str = ""
    duration_ms: int = 0  # the file's running time, as Plex has it


@dataclass
class Library:
    key: str
    title: str
    type: str
    kind: str | None
    locations: list[str] = field(default_factory=list)
    changed: str = ""  # Plex's stamp of the last change to the library's content (contentChangedAt, else updatedAt): the data layer reads again when it moves


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
    parent_rating_key: str = ""  # the season, or for music and audiobooks the album
    poster: str = ""  # Plex's poster (the show's or album's for an episode or track)

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


def title_of(item: ET.Element) -> str:
    """How Plex names an item: a movie "Title (year)" (and its edition: "Title (year) · Extended"),
    an episode "Show · S01E05", a track "Album · Track"."""
    title = item.get("title") or ""
    if item.get("type") == "episode":
        s, e = item.get("parentIndex"), item.get("index")
        show = item.get("grandparentTitle") or ""
        if s and e and s.isdigit() and e.isdigit():
            return f"{show} · S{int(s):02d}E{int(e):02d}"
        return f"{show} · {title}" if show else title
    if item.get("type") == "track":
        album = item.get("parentTitle") or ""
        return f"{album} · {title}" if album else title
    year = item.get("year")
    out = f"{title} ({year})" if year and title else title
    edition = item.get("editionTitle")  # Plex's editions: "Director's Cut", "Extended"…
    return f"{out} · {edition}" if edition else out


def matched_of(item: ET.Element) -> bool | None:
    """Whether Plex matched an item to an online entry: unmatched items get a "local://" guid
    (or, with the old agents, the "none" agent's)."""
    guid = item.get("guid") or ""
    if not guid:
        return None
    return not (guid.startswith("local://") or "agents.none" in guid)


def poster_of(item: ET.Element) -> str:
    if item.get("type") == "episode":
        return item.get("parentThumb") or item.get("grandparentThumb") or ""
    if item.get("type") == "track":
        return item.get("parentThumb") or ""
    return item.get("thumb") or ""


# The fields Plex locks when you edit them, and compares across servers (Library Sync): each is the
# item's attribute of the same name
EDIT_FIELDS = ("title", "titleSort", "originalTitle", "summary", "tagline", "year", "studio", "contentRating", "originallyAvailableAt")
ART_FIELDS = ("thumb", "art")
TAG_FIELDS = {"genre": "Genre", "writer": "Writer", "director": "Director", "producer": "Producer", "country": "Country", "collection": "Collection", "label": "Label"}


def item_summary(item: ET.Element) -> dict:
    """What Library Sync compares of an item (PlexClient.library_items)."""
    locked = {f.get("name") for f in item.findall("Field") if f.get("locked") in ("1", "true")}
    return {"key": item.get("ratingKey", ""), "parent": item.get("parentRatingKey", ""), "grandparent": item.get("grandparentRatingKey", ""),
            "type": item.get("type", ""), "title": item.get("title", ""), "label": title_of(item), "index": _int(item.get("index"), 0),
            "guid": item.get("guid", ""), "matched": matched_of(item), "year": item.get("year", ""),
            "guids": [g.get("id", "") for g in item.findall("Guid") if g.get("id")],  # its ids at the sites (tmdb://…, imdb://…)
            "summary": bool((item.get("summary") or "").strip()), "summary_text": (item.get("summary") or "").strip()[:400], "thumb": item.get("thumb", ""),
            # the tag fields' values (genres, writers…): what an edit of them holds
            "tags": {f: [t.get("tag", "") for t in item.findall(tag) if t.get("tag")] for f, tag in TAG_FIELDS.items() if item.findall(tag)},
            "files": [(p.get("file"), _int(p.get("size"))) for m in item.findall("Media") for p in m.findall("Part") if p.get("file")],
            "durations": [_int(p.get("duration") or m.get("duration")) for m in item.findall("Media") for p in m.findall("Part") if p.get("file")],
            "edition": item.get("editionTitle", ""), "art_path": item.get("art", ""),
            "fields": {f: item.get(f, "") for f in EDIT_FIELDS if f in locked},
            "values": {f: item.get(f, "") for f in EDIT_FIELDS if item.get(f)},  # every field's value, edited or not (what applying puts back)
            "locked": sorted(n for n in locked if n and n not in ART_FIELDS),  # every edited field: the tag ones too (genre, writer…)
            "collections": sorted(c.get("tag", "") for c in item.findall("Collection") if c.get("tag")),
            "labels": sorted(c.get("tag", "") for c in item.findall("Label") if c.get("tag")),
            "art_locked": sorted(f for f in ART_FIELDS if f in locked),
            "updated_at": _int(item.get("updatedAt")),
            # the streams of each file, as Plex probed them (an item's own page lists them; the listings don't): what the
            # file itself doesn't say — a track's bitrate, and Atmos or DTS:X in its name
            "streams": [stream_summary(st, p.get("file") or "") for m in item.findall("Media") for p in m.findall("Part") for st in p.findall("Stream")]}


STREAM_TYPES = {"1": "video", "2": "audio", "3": "subtitles", "4": "lyrics"}


def stream_summary(st: ET.Element, file: str) -> dict:
    """One stream of a file as Plex has it: {"file", "type", "index", "codec", "channels", "bitrate" (kbps), "sample_rate",
    "bit_depth", "language", "forced", "default", "title", "display", "layout", "profile"}."""
    return {"file": file, "type": STREAM_TYPES.get(st.get("streamType") or "", "other"), "index": _int(st.get("index"), -1),
            "codec": (st.get("codec") or "").lower(), "channels": _int(st.get("channels")), "bitrate": _int(st.get("bitrate")),
            "sample_rate": _int(st.get("samplingRate")), "bit_depth": _int(st.get("bitDepth")),
            "language": st.get("languageTag") or st.get("languageCode") or st.get("language") or "",
            "forced": st.get("forced") in ("1", "true"), "default": st.get("default") in ("1", "true"),
            "title": st.get("title") or "", "display": st.get("extendedDisplayTitle") or st.get("displayTitle") or "",
            "layout": st.get("audioChannelLayout") or "", "profile": st.get("profile") or ""}


class PlexClient:
    def __init__(self, url: str, token: str, timeout: float = 30):
        if not url:
            raise PlexError(M("Plex server address is not set"))
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Plex's https uses plex.direct certificates that won't match a LAN IP.
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------------ http
    def _request(self, path: str, params: dict | None = None, headers: dict | None = None,
                 auth: bool = True, method: str = "GET", data: bytes | None = None) -> bytes:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.url}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, method=method, data=data)
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
                raise PlexError(M("Plex rejected the token (401 Unauthorized)")) from None
            raise PlexError(M("Plex returned HTTP {code} for {path}", code=str(e.code), path=path)) from None
        except urllib.error.URLError as e:
            raise PlexError(M("Can’t reach Plex at {url}: {why}", url=self.url, why=str(e.reason))) from None
        except (TimeoutError, OSError) as e:
            raise PlexError(M("Can’t reach Plex at {url}: {why}", url=self.url, why=str(e))) from None

    def scan(self, section_key: str, folder: str) -> None:
        """Ask Plex to scan one folder of a library now (as its own "Scan Library Files", just
        there): for libraries scanned by hand, or before Plex notices a change by itself."""
        self._request(f"/library/sections/{section_key}/refresh", {"path": folder})

    def split(self, rating_key: str) -> None:
        """A record's versions split into records of their own again (Plex's Split Apart): what undoes a merge — the
        folded records' own metadata is gone, though; they're matched afresh."""
        self._request(f"/library/metadata/{rating_key}/split", method="PUT")

    def merge(self, keep: str, others: list[str]) -> None:
        """Several items made one (Plex's own "Merge"): ``others`` become versions of ``keep``, whose
        metadata (edits, poster, collections, watched state) is the one kept."""
        if others:
            self._request(f"/library/metadata/{keep}/merge", {"ids": ",".join(str(k) for k in others)}, method="PUT")

    # ---------------------------------------------------------- edits (Library Sync, applying)
    def edit(self, section_key: str, type_no: int, rating_key: str, values: dict) -> None:
        """An item's fields set as Plex's own editor does: ``values`` {"title.value": "…", "title.locked": 1,
        "genre[0].tag.tag": "Drama", "genre[].tag.tag-": "Old", "genre.locked": 1}."""
        self._request(f"/library/sections/{section_key}/all", dict({"type": type_no, "id": rating_key}, **values), method="PUT")

    def set_tags(self, section_key: str, type_no: int, rating_key: str, field: str, tags: list[str], remove: list[str] = (), locked: bool | None = None) -> None:
        """A tag field (genre, director, collection, label…) given ``tags`` (added) and ``remove`` taken off."""
        values: dict = {f"{field}[{i}].tag.tag": t for i, t in enumerate(tags)}
        for t in remove:
            values[f"{field}[].tag.tag-"] = t if f"{field}[].tag.tag-" not in values else values[f"{field}[].tag.tag-"] + "," + t
        if locked is not None:
            values[f"{field}.locked"] = 1 if locked else 0
        if values:
            self.edit(section_key, type_no, rating_key, values)

    def match(self, rating_key: str, guid: str, name: str = "") -> None:
        """An item matched to an online entry (its guid, as Plex gives it: plex://movie/…)."""
        self._request(f"/library/metadata/{rating_key}/match", {"guid": guid, "name": name}, method="PUT")

    def unmatch(self, rating_key: str) -> None:
        self._request(f"/library/metadata/{rating_key}/unmatch", method="PUT")

    def images(self, rating_key: str, kind: str = "posters") -> list[dict]:
        """The posters (or backgrounds: "arts") Plex has for an item: [{"id", "selected", "thumb"}]; "id" is
        what set_image() takes."""
        root = self.get(f"/library/metadata/{rating_key}/{kind}")
        return [{"id": p.get("ratingKey", ""), "selected": p.get("selected") in ("1", "true"), "thumb": p.get("thumb", "")} for p in root]

    def set_image(self, rating_key: str, image_id: str, kind: str = "poster") -> None:
        """One of an item's images (images()) chosen as its poster (or background: "art")."""
        self._request(f"/library/metadata/{rating_key}/{kind}", {"url": image_id}, method="PUT")

    def upload_image(self, rating_key: str, data: bytes, kind: str = "posters") -> None:
        """An image added to an item as its poster (or background: "arts"), chosen at once."""
        self._request(f"/library/metadata/{rating_key}/{kind}", method="POST", data=data, headers={"Content-Type": "image/jpeg"})

    def empty_trash(self, section_key: str) -> None:
        """Items whose files are gone dropped from a library (Plex's "Empty Trash"; done by itself
        after a scan unless that's turned off)."""
        self._request(f"/library/sections/{section_key}/emptyTrash", method="PUT")

    def items_titled(self, section_key: str, leaf: int, title: str) -> list[dict]:
        """The items of one type named ``title`` (as item_summary gives them): a few, so quick to poll
        while Plex picks up a moved file."""
        root = self.get(f"/library/sections/{section_key}/all", {"type": leaf, "title": title, "includeGuids": 1, "includeFields": 1})
        return [item_summary(it) for it in root]

    def item_files(self, rating_key: str) -> list[str] | None:
        """Every file of a Plex item (its versions' parts), or None if Plex has no such item."""
        try:
            root = self.get(f"/library/metadata/{rating_key}")
        except PlexError as e:
            if "404" in str(e):
                return None
            raise
        return [p.get("file") for item in root for m in item.findall("Media") for p in m.findall("Part") if p.get("file")]

    def library_count(self, section_key: str) -> int:
        """How many items a library has (its movies, shows, artists…), as Plex counts them."""
        root = self.get(f"/library/sections/{section_key}/all",
                        headers={"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "0"})
        return _int(root.get("totalSize") or root.get("size"))

    def image(self, path: str, limit: int = 32 * 1024 * 1024) -> bytes:
        """An image Plex keeps (a poster's path from MediaFile.poster), as Plex has it — not
        resized or re-encoded by Plex's photo transcoder."""
        if not path.startswith("/library/"):
            raise PlexError(M("Plex returned HTTP {code} for {path}", code="404", path=path))
        data = self._request(path, headers={"Accept": "image/*"})
        if len(data) > limit:
            raise PlexError(M("Plex’s poster is too large ({size})", size=f"{len(data) // (1024 * 1024)} MB"))
        return data

    def thumb(self, path: str, width: int = 120, height: int = 180) -> bytes:
        """A small copy of an image Plex keeps (a poster), made by Plex's photo transcoder: for lists."""
        if not path.startswith("/library/"):
            raise PlexError(M("Plex returned HTTP {code} for {path}", code="404", path=path))
        return self._request("/photo/:/transcode", {"url": path, "width": width, "height": height, "minSize": 1, "upscale": 0},
                             headers={"Accept": "image/*"})

    def get(self, path: str, params: dict | None = None, headers: dict | None = None) -> ET.Element:
        body = self._request(path, params, headers)
        try:
            return ET.fromstring(body)
        except ET.ParseError:
            raise PlexError(M("Plex returned something that isn’t XML for {path}", path=path)) from None

    # --------------------------------------------------------------- server
    def identity(self) -> dict:
        """Is a Plex server answering here? Works without a token."""
        start = time.monotonic()
        body = self._request("/identity", auth=False)
        ms = int((time.monotonic() - start) * 1000)
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise PlexError(M("Something answered at {url}, but it isn’t Plex", url=self.url)) from None
        if not root.get("machineIdentifier"):
            raise PlexError(M("Something answered at {url}, but it isn’t Plex", url=self.url))
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
                    changed=d.get("contentChangedAt") or d.get("updatedAt") or "",
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
                            out.append(MediaFile(path, media_kbps(media, part), _int(part.get("size")), lib.kind, lib.title, lib.key,
                                                 poster_of(item), matched_of(item), title_of(item), item.get("ratingKey", ""),
                                                 _int(part.get("duration") or media.get("duration"))))
            total = _int(root.get("totalSize"), -1)
            start += len(items)
            if not items or len(items) < size or (total >= 0 and start >= total):
                break
        return out[:limit] if limit else out

    def library_items(self, section_key: str, leaf: int, limit: int | None = None) -> list[dict]:
        """A library's items of one type (LEAF_TYPE's numbers: 1 movies, 2 shows, 3 seasons, 4 episodes,
        8 artists, 9 albums, 10 tracks), paged, with what Library Sync compares: {"key", "parent",
        "grandparent", "type", "title", "label" (as Plex names it), "guid", "matched", "files":
        [(path, size)], "edition", "fields": {name: value} for the fields you edited (Plex locks them),
        "collections", "labels", "art_locked": the poster/background chosen by hand}."""
        out: list[dict] = []
        start = 0
        while True:
            size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(out))
            if size <= 0:
                break
            root = self.get(f"/library/sections/{section_key}/all", {"type": leaf, "includeGuids": 1, "includeFields": 1},
                            {"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(size)})
            items = list(root)
            for item in items:
                out.append(item_summary(item))
            total = _int(root.get("totalSize"), -1)
            start += len(items)
            if not items or len(items) < size or (total >= 0 and start >= total):
                break
        return out[:limit] if limit else out

    def item_detail(self, rating_key: str) -> dict | None:
        """One item's own page (/library/metadata/<key>): what the listings leave out — which fields are
        locked (edited), the summary — as item_summary() gives it; None if Plex has no such item."""
        try:
            root = self.get(f"/library/metadata/{rating_key}", {"includeGuids": 1})
        except PlexError as e:
            if "404" in str(e):
                return None
            raise
        items = list(root)
        return item_summary(items[0]) if items else None

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
                    parent_rating_key=item.get("parentRatingKey", ""),
                    poster=poster_of(item),
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


    def show_episodes(self, show_rating_key: str) -> list[dict]:
        """Every episode of a show in Plex's order (season, then episode), specials included."""
        root = self.get(f"/library/metadata/{show_rating_key}/allLeaves")
        out = []
        for ep in root.findall("Video"):
            files = [(part.get("file"), _int(part.get("size")), media_kbps(media, part))
                     for media in ep.findall("Media") for part in media.findall("Part") if part.get("file")]
            out.append({"rating_key": ep.get("ratingKey", ""), "season": _int(ep.get("parentIndex"), 0),
                        "episode": _int(ep.get("index"), 0), "title": ep.get("title", ""), "files": files})
        return out

    def movie_collections(self, rating_key: str, section_key: str) -> list[list[dict]]:
        """The Plex collections a movie is in, each as its movies in the collection's order."""
        root = self.get(f"/library/metadata/{rating_key}")
        video = root.find("Video")
        tags = [c.get("tag", "") for c in (video.findall("Collection") if video is not None else []) if c.get("tag")]
        if not tags or not section_key:
            return []
        cols = {d.get("title", ""): d.get("ratingKey", "") for d in self.get(f"/library/sections/{section_key}/collections").findall("Directory")}
        out = []
        for tag in tags:
            rk = cols.get(tag)
            if not rk:
                continue
            movies = []
            for v in self.get(f"/library/collections/{rk}/children").findall("Video"):
                files = [(part.get("file"), _int(part.get("size")), media_kbps(media, part))
                         for media in v.findall("Media") for part in media.findall("Part") if part.get("file")]
                movies.append({"rating_key": v.get("ratingKey", ""), "title": v.get("title", ""), "collection": tag, "files": files})
            out.append(movies)
        return out

    def album_tracks(self, album_rating_key: str) -> list[dict]:
        """An album's (or audiobook's) tracks in Plex's order."""
        root = self.get(f"/library/metadata/{album_rating_key}/children")
        out = []
        for tr in root.findall("Track"):
            files = [(part.get("file"), _int(part.get("size")))
                     for media in tr.findall("Media") for part in media.findall("Part") if part.get("file")]
            out.append({"rating_key": tr.get("ratingKey", ""), "title": tr.get("title", ""),
                        "duration_ms": _int(tr.get("duration")), "files": files})
        return out

    def artist_albums(self, artist_rating_key: str) -> list[str]:
        """Rating keys of an artist's (or author's) albums, in Plex's order."""
        root = self.get(f"/library/metadata/{artist_rating_key}/children")
        return [d.get("ratingKey", "") for d in root.findall("Directory") if d.get("ratingKey")]


# ---------------------------------------------------------------------------
# "Sign in with Plex" (PIN flow against plex.tv)
# ---------------------------------------------------------------------------

PLEX_TV = "https://plex.tv"


def _plextv(method: str, path: str, client_id: str, data: bytes | None = None, token: str = ""):
    req = urllib.request.Request(PLEX_TV + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("X-Plex-Token", token)
    req.add_header("X-Plex-Product", PRODUCT)
    req.add_header("X-Plex-Version", __version__)
    req.add_header("X-Plex-Client-Identifier", client_id)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        e.close()
        raise PlexError(M("plex.tv returned HTTP {code}", code=str(e.code))) from None
    except (urllib.error.URLError, OSError) as e:
        raise PlexError(M("Can’t reach plex.tv: {why}", why=str(e))) from None


def create_pin(client_id: str) -> dict:
    pin = _plextv("POST", "/api/v2/pins?strong=true", client_id, data=b"")
    params = urllib.parse.urlencode(
        {"clientID": client_id, "code": pin["code"], "context[device][product]": PRODUCT}
    )
    return {"id": pin["id"], "auth_url": f"https://app.plex.tv/auth#?{params}"}


def check_pin(client_id: str, pin_id: int) -> str | None:
    pin = _plextv("GET", f"/api/v2/pins/{int(pin_id)}", client_id)
    return pin.get("authToken") or None


def servers(client_id: str, token: str) -> list[dict]:
    """The Plex servers a plex.tv account can use (its own and ones shared with it): [{"id", "name",
    "owned", "token" (the server's own token for this account), "connections": [{"uri", "local",
    "relay"}]}], from plex.tv's list of the account's resources."""
    got = _plextv("GET", "/api/v2/resources?includeHttps=1&includeRelay=1", client_id, token=token) or []
    out = []
    for r in got if isinstance(got, list) else []:
        if "server" not in str(r.get("provides") or ""):
            continue
        out.append({"id": r.get("clientIdentifier") or "", "name": r.get("name") or "", "owned": bool(r.get("owned")),
                    "token": r.get("accessToken") or token,
                    "connections": [{"uri": c.get("uri") or "", "local": bool(c.get("local")), "relay": bool(c.get("relay")),
                                     "address": c.get("address") or "", "protocol": c.get("protocol") or "", "ipv6": bool(c.get("IPv6"))}
                                    for c in r.get("connections") or [] if c.get("uri")]})
    return out


def rank_connections(connections: list[dict]) -> list[dict]:
    """A server's addresses in the order to try them: the ones that last — addresses on a local network
    (private IPs: this NAS's, or over a VPN) first, then names (plex.direct's and your own: they follow
    the server when its public address changes), then other IPs (a public address that may change),
    Plex's relay last (slow, and limited); within each, https before http, IPv4 before IPv6."""
    import ipaddress
    import urllib.parse as up

    def key(c):
        host = up.urlsplit(c["uri"]).hostname or c.get("address") or ""
        try:
            ip = ipaddress.ip_address(host)
            kind = 0 if ip.is_private or ip.is_loopback or ip.is_link_local else 2
            v6 = ip.version == 6
        except ValueError:
            kind, v6 = 1, False  # a name
        return (bool(c.get("relay")), kind, not c["uri"].lower().startswith("https://"), v6)
    return sorted(connections, key=key)


def reachable(server: dict, timeout: float = 6) -> str | None:
    """The first of a server's addresses, in rank_connections' order, that answers as that server."""
    order = rank_connections(server.get("connections") or [])
    for c in order:
        try:
            got = PlexClient(c["uri"], server.get("token") or "", timeout=timeout).identity()
        except PlexError:
            continue
        if got.get("machine_id") == server.get("id"):
            return c["uri"]
    return None

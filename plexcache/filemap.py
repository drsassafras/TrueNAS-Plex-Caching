"""What's where in a media file, for the Library Health page: every part of it — the headers,
attachments, padding and anything else in front of the video, the video itself, and the index,
tags, chapters and attachments after it — with each part's place and size, the tracks (codec,
language, compression) and each attachment (and whether it's an image).

``describe()`` reads only the parts' headers (a few small reads, wherever the file is), never the
video. ``FileMaps`` keeps what was found with the file's size, modification time and inode, so a
file is read again only when it changes; the Survey job (``SurveyJob``) fills it for a library.
What caching takes from each file comes from the same reading of its layout as caching itself
(layout.tail_ranges, layout.min_head), so the page shows what really happens.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time

from . import layout
from .jobs import Job
from .layout import _element, _pread
from .i18n import M

STORE = "file-maps.json"
MAX_PARTS = 512  # top-level parts to read from either end before giving up
MAX_TRACKS = 128
NAMES = {  # MKV top-level parts, as the page names them
    0x1A45DFA3: "header", 0x114D9B74: "seekhead", 0x1549A966: "info", 0x1654AE6B: "tracks",
    0x1043A770: "chapters", 0x1941A469: "attachments", 0x1254C367: "tags", 0x1C53BB6B: "index",
    0xEC: "padding", 0xBF: "checksum", 0x1F43B675: "video",
}
TRACK_TYPES = {1: "video", 2: "audio", 3: "complex", 0x10: "logo", 0x11: "subtitles", 0x12: "buttons", 0x20: "control"}
COMPRESSION = {0: "zlib", 1: "bzlib", 2: "lzo", 3: "header stripping"}


def _children(buf: bytes, start: int, end: int):
    """(id, data start, data end) of each element in ``buf[start:end]``."""
    p = start
    while p < end:
        try:
            eid, size, hl = _element(buf, p)
        except ValueError:
            return
        if size is None:
            return
        yield eid, p + hl, p + hl + size
        p += hl + size


def _uint(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def _tracks(buf: bytes) -> list[dict]:
    """The tracks from a Tracks element's body: number, type, codec, language, name, size in pixels,
    and the compression its blocks are stored with (ContentEncodings)."""
    out = []
    for eid, a, b in _children(buf, 0, len(buf)):
        if eid != 0xAE or len(out) >= MAX_TRACKS:
            continue
        t = {"number": None, "type": None, "codec": "", "language": "", "name": "", "compression": None}
        for cid, ca, cb in _children(buf, a, b):
            v = buf[ca:cb]
            if cid == 0xD7:
                t["number"] = _uint(v)
            elif cid == 0x83:
                t["type"] = TRACK_TYPES.get(_uint(v), "other")
            elif cid == 0x86:
                t["codec"] = v.decode("ascii", "replace")
            elif cid in (0x22B59C, 0x22B59D):  # Language, LanguageIETF (the second wins)
                t["language"] = v.decode("ascii", "replace")
            elif cid == 0x536E:
                t["name"] = v.decode("utf-8", "replace")
            elif cid == 0xE0:  # Video: its size
                for vid, va, vb in _children(buf, ca, cb):
                    if vid == 0xB0:
                        t["width"] = _uint(buf[va:vb])
                    elif vid == 0xBA:
                        t["height"] = _uint(buf[va:vb])
            elif cid == 0x6D80:  # ContentEncodings → ContentEncoding → ContentCompression → algorithm
                for _, ea, eb in _children(buf, ca, cb):
                    for kid, ka, kb in _children(buf, ea, eb):
                        if kid == 0x5034:
                            algo = 0
                            for zid, za, zb in _children(buf, ka, kb):
                                if zid == 0x4254:
                                    algo = _uint(buf[za:zb])
                            t["compression"] = COMPRESSION.get(algo, "other")
        if not t["type"]:  # not recorded: from the codec ("V_…", "A_…", "S_…")
            t["type"] = {"V": "video", "A": "audio", "S": "subtitles"}.get(t["codec"][:1], "other")
        out.append(t)
    return out


def _attachments(fd: int, at: int, size: int) -> list[dict]:
    """Each attachment in the Attachments element at ``at``: its name, type, place and size, and
    whether it's an image (only the start of each is read: its name and type come first)."""
    eid, esize, hl = _element(_pread(fd, at, 16), 0)
    if eid != layout.ATTACHMENTS or esize is None:
        return []
    out, p, end = [], at + hl, min(at + hl + esize, size)
    while p < end and len(out) < layout.MAX_ATTACHMENTS:
        cid, csize, chl = _element(_pread(fd, p, 16), 0)
        if csize is None:
            break
        if cid == layout.ATTACHED_FILE:
            head = _pread(fd, p + chl, min(csize, 4096))
            name = mime = ""
            for fid, fa, fb in _children(head, 0, len(head)):
                if fid == layout.FILE_DATA:
                    break
                if fid == layout.FILE_NAME:
                    name = head[fa:fb].decode("utf-8", "replace")
                elif fid == layout.FILE_MIME:
                    mime = head[fa:fb].decode("utf-8", "replace")
            out.append({"name": name, "mime": mime, "pos": p, "size": chl + csize, "image": layout._is_image(name, mime)})
        p += chl + csize
    return out


def _walk(fd: int, start: int, size: int, stop_at_video: bool) -> list[dict]:
    """Top-level parts from ``start``, one header at a time: [{"part", "pos", "size"}]. Stops at the
    video (``stop_at_video``), at a part of unknown size, or at the end of the file."""
    out, p = [], start
    while p < size and len(out) < MAX_PARTS:
        try:
            eid, esize, hl = _element(_pread(fd, p, 16), 0)
        except ValueError:
            break
        name = NAMES.get(eid, "other")
        if eid == layout.CLUSTER and stop_at_video:
            break
        if esize is None:
            out.append({"part": name, "pos": p, "size": size - p})
            break
        out.append({"part": name, "pos": p, "size": min(hl + esize, size - p)})
        p += hl + esize
    return out


def _mkv(fd: int, size: int) -> dict:
    buf = _pread(fd, 0, min(size, layout.HEAD_READ))
    eid, esize, hl = _element(buf, 0)
    if eid != layout.EBML or esize is None:
        return {"format": "other"}
    parts = [{"part": "header", "pos": 0, "size": hl + esize}]
    pos = hl + esize
    _, _, shl = _element(buf, pos)
    seg = pos + shl
    parts.append({"part": "segment", "pos": pos, "size": shl})  # where the Segment (everything else) starts
    front = _walk(fd, seg, size, True)
    parts += front
    video_start = (front[-1]["pos"] + front[-1]["size"]) if front else seg
    tracks, attachments = [], []
    for p in front:
        if p["part"] == "tracks":
            data = _pread(fd, p["pos"], min(p["size"], 1024 * 1024))
            _, _, thl = _element(data, 0)
            tracks = _tracks(data[thl:])
        elif p["part"] == "attachments":
            attachments += _attachments(fd, p["pos"], size)
    # after the video: where the index, tags and the like start (from the file's own table of
    # contents), then every part from there to the end
    tail_from = None
    try:
        for sid, spos in _seek_targets(fd, buf, seg, size):
            at = seg + spos
            if at > video_start and (tail_from is None or at < tail_from):
                tail_from = at
    except (ValueError, struct.error):
        tail_from = None
    tail = _walk(fd, tail_from, size, False) if tail_from else []
    for p in tail:
        if p["part"] == "attachments":
            attachments += _attachments(fd, p["pos"], size)
    video_end = tail_from or size
    parts.append({"part": "video", "pos": video_start, "size": video_end - video_start})
    parts += tail
    if tail and tail[-1]["pos"] + tail[-1]["size"] < size:  # anything after the last part
        end = tail[-1]["pos"] + tail[-1]["size"]
        parts.append({"part": "other", "pos": end, "size": size - end})
    return {"format": "mkv", "parts": parts, "tracks": tracks, "attachments": attachments,
            "video_start": video_start, "video_end": video_end}


def _seek_targets(fd: int, buf: bytes, seg: int, size: int) -> list[tuple[int, int]]:
    """(id, position in the segment) of everything the file's SeekHeads list (a second one too)."""
    out, seen = [], set()
    p = seg
    heads = []
    while p < len(buf) - 12:
        eid, esize, ehl = _element(buf, p)
        if eid == layout.CLUSTER or esize is None:
            break
        if eid == layout.SEEKHEAD and p + ehl + esize <= len(buf):
            heads.append(buf[p + ehl:p + ehl + esize])
        p += ehl + esize
    while heads:
        body = heads.pop(0)
        for sid, spos in layout._seekhead_entries(body, 0, len(body)):
            if sid == layout.SEEKHEAD and spos not in seen and seg + spos < size:
                seen.add(spos)
                extra = _pread(fd, seg + spos, 64 * 1024)
                hid, hsize, hhl = _element(extra, 0)
                if hid == layout.SEEKHEAD and hsize is not None and hhl + hsize <= len(extra):
                    heads.append(extra[hhl:hhl + hsize])
            out.append((sid, spos))
    return out


def _mp4(fd: int, size: int) -> dict:
    """An MP4's top-level boxes: the index (moov), the video data (mdat), and the rest."""
    parts, p = [], 0
    names = {b"moov": "index", b"mdat": "video", b"ftyp": "header", b"free": "padding", b"skip": "padding", b"wide": "padding"}
    video_start = video_end = None
    while p + 8 <= size and len(parts) < MAX_PARTS:
        hdr = _pread(fd, p, 16)
        box, kind = struct.unpack(">I4s", hdr[:8])
        if box == 1:
            box = struct.unpack(">Q", hdr[8:16])[0]
        elif box == 0:
            box = size - p
        if box < 8:
            break
        name = names.get(kind, "other")
        parts.append({"part": name, "pos": p, "size": min(box, size - p)})
        if kind == b"mdat" and video_start is None:
            video_start, video_end = p, p + box
        p += box
    return {"format": "mp4", "parts": parts, "tracks": [], "attachments": [],
            "video_start": video_start, "video_end": video_end}


def describe(path: str) -> dict:
    """What's where in the file at ``path`` (see the module's notes), with what caching takes from
    it: ``meta`` (the metadata ranges, layout.tail_ranges) and ``video_start``."""
    st = os.stat(path)
    size = st.st_size
    out = {"size": size, "disk": st.st_blocks * 512, "blksize": st.st_blksize if size > st.st_blksize else None,
           "format": "other", "parts": [], "tracks": [], "attachments": []}
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        out["error"] = str(e)
        return out
    try:
        magic = _pread(fd, 0, 12)
        if magic[:4] == b"\x1a\x45\xdf\xa3":
            out.update(_mkv(fd, size))
        elif magic[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"):
            out.update(_mp4(fd, size))
    except (OSError, ValueError, struct.error) as e:
        out["error"] = str(e)
    finally:
        os.close(fd)
    meta, info = layout.tail_ranges(path, size, 0)
    out["meta"] = [list(r) for r in meta]
    out["video_start"] = out.get("video_start") if out.get("video_start") is not None else info.get("video_start")
    return out


def cache_plan(m: dict | None, size: int, kbps: int, seconds: float, record: int | None, kind: str) -> dict:
    """What caching takes from a file: its start (the lead-in, never less than the metadata in front of
    the video and 60% of a record of video) and its metadata at the end, in whole records — as
    warm-up reads them. Without the file's layout, estimated (``estimated``)."""
    from .warmup import DEFAULT_FRONT, DEFAULT_INDEX, align_ranges, preload_bytes
    lead = preload_bytes(kbps or 1, seconds, size, record)
    if m and m.get("format") in ("mkv", "mp4"):
        vs = m.get("video_start")
        floor = layout.min_head(vs, record, size)
        start = max(lead, floor)
        end = [(o, n) for o, n in (tuple(r) for r in m.get("meta") or []) if vs is None or o >= vs]
        estimated = False
    else:
        floor = layout.min_head(DEFAULT_FRONT.get(kind, 0), record, size)
        start = max(lead, floor)
        tail = DEFAULT_INDEX.get(kind, 0)
        end = [(max(0, size - tail), tail)] if tail and size > tail else []
        estimated = True
    end = [(o, n) for o, n in align_ranges(end, record, size) if o + n > start]
    end = [(max(o, start), o + n - max(o, start)) for o, n in end]
    return {"start": min(start, size) if size else start, "lead": lead, "floor": floor, "end": [list(r) for r in end],
            "bytes": min(start, size) + sum(n for _, n in end), "record": record, "estimated": estimated}


def pieces(m: dict) -> list[dict]:
    """The file's parts in order, attachments split into each one: [{"kind", "pos", "size", "name"?}].
    Kinds: the parts' (header, seekhead, info, tracks, chapters, padding, video, index, tags,
    checksum, other) and, for attachments, image, font or attachment."""
    out = []
    for p in m.get("parts") or []:
        atts = [a for a in m.get("attachments") or [] if p["part"] == "attachments" and p["pos"] <= a["pos"] < p["pos"] + p["size"]]
        if not atts:
            out.append({"kind": p["part"], "pos": p["pos"], "size": p["size"]})
            continue
        at = p["pos"]
        for a in atts:
            if a["pos"] > at:
                out.append({"kind": "attachments", "pos": at, "size": a["pos"] - at})
            kind = "image" if a["image"] else "font" if any(x in (a["mime"] + a["name"]).lower() for x in ("font", ".ttf", ".otf")) else "attachment"
            out.append({"kind": kind, "pos": a["pos"], "size": a["size"], "name": a["name"]})
            at = a["pos"] + a["size"]
        if at < p["pos"] + p["size"]:
            out.append({"kind": "attachments", "pos": at, "size": p["pos"] + p["size"] - at})
    return out


def block_map(m: dict | None, size: int, plan: dict, record: int | None) -> dict | None:
    """The blocks caching always keeps of a file — at its start, through the metadata in front of the
    video and 60% of a block of video; at its end, every block holding metadata — each with what's in
    it (bytes per part, and past the end of the file the block's unused, zero-filled rest). The lead-in
    adds more blocks of video after the first ones (``lead_blocks``)."""
    if not m or not m.get("parts") or not record or not size:
        return None
    parts = pieces(m)

    def block(n: int) -> dict:
        a, b = n * record, (n + 1) * record
        got = []
        for p in parts:
            lo, hi = max(a, p["pos"]), min(b, p["pos"] + p["size"])
            if hi > lo:
                got.append(dict({"kind": p["kind"], "bytes": hi - lo}, **({"name": p["name"]} if p.get("name") else {})))
        data = max(0, min(b, size) - a)
        if data < record:
            got.append({"kind": "empty", "bytes": record - data})  # past the end of the file: zeros
        return {"n": n + 1, "from": a, "data": data, "pieces": got}

    first = max(1, -(-plan["floor"] // record))
    start = [block(n) for n in range(min(first, -(-size // record)))]
    vs = m.get("video_start") or 0
    end_ranges = [(o, n) for o, n in (tuple(r) for r in m.get("meta") or []) if o >= vs and o + n > first * record]
    end = []
    if end_ranges:
        lo = max(first, min(o for o, _ in end_ranges) // record)
        hi = (max(o + n for o, n in end_ranges) - 1) // record
        end = [block(n) for n in range(lo, hi + 1)]
    lead = max(0, -(-plan["start"] // record) - len(start))
    return {"record": record, "start": start, "end": end, "lead_blocks": lead,
            "total_blocks": -(-size // record), "video": max(0, (m.get("video_end") or size) - vs)}


def estimate_blocks(path: str, numbers: list[int], record: int, size: int) -> list[dict]:
    """For each block (1-based), how ZFS might store it: its bytes compressed (zlib standing in for
    ZFS's ZSTD — similar on metadata), kept only if that saves at least 1/8 of the block (ZFS's own
    rule, so video stays as it is), in whole 4K sectors. A file's last block is padded with zeros to a
    whole block, as ZFS does, and the zeros compress away."""
    import zlib
    out = []
    with open(path, "rb") as fh:
        for n in numbers[:32]:
            a = (n - 1) * record
            if a >= size or n < 1:
                continue
            fh.seek(a)
            data = fh.read(record)
            raw = data + b"\0" * (record - len(data))
            packed = len(zlib.compress(raw, 6))
            stored = record if packed > record - record // 8 else -(-packed // 4096) * 4096
            out.append({"n": n, "data": len(data), "stored": stored, "compressed": stored < record})
    return out


class FileMaps:
    """Each file's layout as last read, with its identity: read again only once the file changes."""

    def __init__(self, config_dir: str):
        self.path = os.path.join(config_dir, STORE)
        self.lock = threading.Lock()
        self.dirty = False
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.files: dict[str, dict] = json.load(fh).get("files") or {}
        except (OSError, ValueError):
            self.files = {}

    @staticmethod
    def _ident(st: os.stat_result) -> list:
        return [st.st_size, st.st_mtime_ns, st.st_ino]

    def get(self, path: str, st: os.stat_result | None = None) -> dict | None:
        """The file's layout if it's known and the file hasn't changed since (``st``: its stat)."""
        with self.lock:
            e = self.files.get(path)
        if not e:
            return None
        if st is not None and e.get("id") != self._ident(st):
            return None
        return e.get("map")

    def read(self, path: str, force: bool = False) -> dict:
        """The file's layout: known, or read now (and kept). ``force``: read again even if unchanged."""
        st = os.stat(path)
        got = None if force else self.get(path, st)
        if got is None:
            got = describe(path)
            got["read"] = time.time()
            with self.lock:
                self.files[path] = {"id": self._ident(st), "map": got}
                self.dirty = True
        return got

    def known(self, paths) -> int:
        with self.lock:
            return sum(1 for p in paths if p in self.files)

    def current(self, paths) -> int:
        """How many of ``paths`` are known as they are now (unchanged since they were inspected)."""
        n = 0
        for p in paths:
            try:
                n += self.get(p, os.stat(p)) is not None
            except OSError:
                continue
        return n

    def forget_missing(self, keep: set) -> None:
        """Drop files no longer in any library."""
        with self.lock:
            gone = [p for p in self.files if p not in keep]
            for p in gone:
                del self.files[p]
            self.dirty = self.dirty or bool(gone)

    def save(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            data = json.dumps({"files": self.files}, separators=(",", ":"))
            self.dirty = False
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, self.path)
        except OSError:
            self.dirty = True  # the config folder isn't writable: Health says so


class SurveyJob(Job):
    """Read the layout of every file in a library (the Library Health page), as a file job: only
    files that changed since they were last read. Reads a few small parts of each file, never the
    video. Not kept in the job log (nothing changes on disk)."""

    kind = "survey"
    logged = False

    def __init__(self, app, library: str, resumed: dict | None = None, force: bool = False):
        title = (app.library_info(str(library)) or {}).get("title") or (resumed or {}).get("title")
        super().__init__(app.jobs, resumed, library=str(library), mode="survey", title=title, force=bool(force))

    def args(self) -> dict:
        return {"library": self.state["library"], "force": self.state["force"]}

    def run(self) -> None:
        app, key = self.app, self.state["library"]
        maps = app.file_maps
        mine = [(app.map_path(f.path), f.size) for f in list(app.files) if str(f.library_key) == key]
        self.update(total=len(mine), todo_bytes=len(mine), phase="working")
        for i, (path, _size) in enumerate(mine):
            if not self.wait_while_paused():
                break
            self.update(current=os.path.basename(path), current_file=path, current_title=app.title_of(path) or None)
            t0 = time.time()
            try:
                maps.read(path, force=self.state["force"])  # (again: every file, changed or not)
                result = "copied"  # (read: counted for the estimate, not as optimized)
            except OSError:
                result = "skipped"
            self.file_done(1, result, time.time() - t0)
            if i % 200 == 0:
                maps.save()
        maps.save()
        app.activity.event("optimizer", M("inspected {n} files in {library}", n=len(mine), library=self.state.get("title") or key))

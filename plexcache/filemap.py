"""What's where in a media file, for the Library Health page: every part of it — the headers,
attachments, padding and anything else in front of the video, the video itself, and the index,
tags, chapters and attachments after it — with each part's place and size, the tracks (codec,
language, compression) and each attachment (and whether it's an image).

``describe()`` reads only the parts' headers (a few small reads, wherever the file is), never the
video. ``FileMaps`` keeps what was found with the file's size, modification time and inode, so a
file is read again only when it changes; the Inspector fills it for a library.
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
from .layout import _element, _pread
from .i18n import M

STORE = "file-maps.json"
MAP_VERSION = 7  # what's read from each file; a map from an older version is read again (2: JPEG tables, 3: mix, 4: audio files, 5: LZ4, 6: ZFS rounding apart, 7: track flags)
LAYOUT_VERSION = 2  # the oldest map still shown while it waits to be read again (it lacks only the mix)
MAX_PARTS = 512  # top-level parts to read from either end before giving up
MAX_TRACKS = 128
NAMES = {  # MKV top-level parts, as the page names them
    0x1A45DFA3: "header", 0x114D9B74: "seekhead", 0x1549A966: "info", 0x1654AE6B: "tracks",
    0x1043A770: "chapters", 0x1941A469: "attachments", 0x1254C367: "tags", 0x1C53BB6B: "index",
    0xEC: "padding", 0xBF: "checksum", 0x1F43B675: "video",
}
TRACK_TYPES = {1: "video", 2: "audio", 3: "complex", 0x10: "logo", 0x11: "subtitles", 0x12: "buttons", 0x20: "control"}
COMPRESSION = {0: "zlib", 1: "bzlib", 2: "lzo", 3: "header stripping"}


def codec_name(codec_id: str) -> str:
    """A codec ID's name as MKVToolNix shows it ("HEVC/H.265/MPEG-H"), or "" when it doesn't know it."""
    import re
    from .codec_names import CODECS
    for name, pattern in CODECS:
        if re.fullmatch(f"(?:{pattern})", codec_id or "", re.I):
            return name
    return ""


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
        t = {"number": None, "type": None, "codec": "", "language": "", "name": "", "compression": None, "forced": False, "default": True}
        bcp47 = ""
        for cid, ca, cb in _children(buf, a, b):
            v = buf[ca:cb]
            if cid == 0xD7:
                t["number"] = _uint(v)
            elif cid == 0x83:
                t["type"] = TRACK_TYPES.get(_uint(v), "other")
            elif cid == 0x86:
                t["codec"] = v.decode("ascii", "replace")
            elif cid == 0x22B59C:  # Language
                t["language"] = v.decode("ascii", "replace")
            elif cid == 0x22B59D:  # LanguageBCP47: wins over Language, wherever it is
                bcp47 = v.decode("ascii", "replace")
            elif cid == 0x536E:
                t["name"] = v.decode("utf-8", "replace")
            elif cid == 0x55AA:  # FlagForced: a subtitle track for the written content of an otherwise spoken-language film
                t["forced"] = bool(_uint(v))
            elif cid == 0x88:  # FlagDefault
                t["default"] = bool(_uint(v))
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
        t["language"] = bcp47 or t["language"] or "eng"  # (Matroska's default when neither is written)
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
            data_at = None
            for fid, fa, fb in _children(head, 0, len(head)):
                if fid == layout.FILE_DATA:
                    data_at = p + chl + fa
                    break
                if fid == layout.FILE_NAME:
                    name = head[fa:fb].decode("utf-8", "replace")
                elif fid == layout.FILE_MIME:
                    mime = head[fa:fb].decode("utf-8", "replace")
            a = {"name": name, "mime": mime, "pos": p, "size": chl + csize, "image": layout._is_image(name, mime)}
            if a["image"] and data_at is not None:
                a["jpeg_tables"] = jpeg_tables(_pread(fd, data_at, 64 * 1024))
            out.append(a)
        p += chl + csize
    return out


# The Huffman tables JPEG's standard suggests (ITU T.81, Annex K): the code counts per length of each.
# A JPEG that uses them was saved without optimizing them; jpegtran -optimize (and most tools that
# squeeze JPEGs) replace them with tables made for the picture — smaller, with every pixel the same.
STANDARD_HUFFMAN = {
    bytes([0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]), bytes([0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]),
    bytes([0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D]), bytes([0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77])}


def jpeg_tables(data: bytes) -> str | None:
    """A JPEG's Huffman tables, from its first bytes: "standard" (not optimized: lossless compression
    would make it smaller), "optimized", "progressive" (its own tables too), or None (not a JPEG, or
    not read far enough)."""
    if data[:2] != b"\xff\xd8":
        return None
    p, counts = 2, []
    while p + 4 <= len(data):
        if data[p] != 0xFF:
            return None
        marker, n = data[p + 1], int.from_bytes(data[p + 2:p + 4], "big")
        if marker == 0xC2:
            return "progressive"
        if marker == 0xC4:  # one or more tables: class/id byte, 16 counts, then the symbols
            q, end = p + 4, p + 2 + n
            while q + 17 <= min(end, len(data)):
                counts.append(bytes(data[q + 1:q + 17]))
                q += 17 + sum(data[q + 1:q + 17])
        if marker == 0xDA:  # the picture itself starts: every table has been seen
            return "standard" if counts and all(c in STANDARD_HUFFMAN for c in counts) else "optimized" if counts else None
        p += 2 + n
    return None


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


AUDIO_EXT = (".mp3", ".flac", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".wav", ".aif", ".aiff", ".wma", ".ape", ".wv", ".dsf", ".mka")


def _picture(fd: int, pos: int, size: int, data_at: int, name: str, mime: str) -> dict:
    """Cover art in an audio file's tags, as an attachment (its frame or block, whole)."""
    a = {"name": name, "mime": mime, "pos": pos, "size": size, "image": True}
    a["jpeg_tables"] = jpeg_tables(_pread(fd, data_at, 64 * 1024)) if data_at < pos + size else None
    return a


def _id3(fd: int, size: int) -> tuple[list[dict], list[dict], int]:
    """An ID3v2 tag at the start: (parts, pictures, where the audio starts). Its frames are tags,
    its pictures (APIC) attachments, the zeros after them padding."""
    head = _pread(fd, 0, 10)
    if head[:3] != b"ID3" or size < 10:
        return [], [], 0
    ver, flags = head[3], head[5]
    syncsafe = lambda b: (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]
    total = min(10 + syncsafe(head[6:10]) + (10 if flags & 0x10 else 0), size)
    parts, pics = [{"part": "id3", "pos": 0, "size": 10}], []
    if ver not in (3, 4):  # (ID3v2.2: read as one block of tags)
        parts.append({"part": "tags", "pos": 10, "size": total - 10})
        return parts, pics, total
    p, run = 10, None
    while p + 10 <= total:
        h = _pread(fd, p, 10)
        if h[0] == 0:
            break  # padding from here
        fid = h[:4]
        n = syncsafe(h[4:8]) if ver == 4 else int.from_bytes(h[4:8], "big")
        if n <= 0 or p + 10 + n > total:
            break
        if fid == b"APIC":
            body = _pread(fd, p + 10, min(n, 512))
            mime = body[1:body.find(b"\0", 1)].decode("latin-1", "replace") if b"\0" in body[1:] else ""
            at = next((i for i in (body.find(b"\xff\xd8"), body.find(b"\x89PNG")) if i >= 0), len(body))
            pics.append(_picture(fd, p, 10 + n, p + 10 + at, "APIC", mime))
            parts.append({"part": "attachments", "pos": p, "size": 10 + n})
            run = None
        elif run:
            run["size"] += 10 + n
        else:
            run = {"part": "tags", "pos": p, "size": 10 + n}
            parts.append(run)
        p += 10 + n
    if p < total:
        parts.append({"part": "padding", "pos": p, "size": total - p})
    return parts, pics, total


FLAC_BLOCKS = {0: "info", 1: "padding", 2: "other", 3: "index", 4: "tags", 5: "chapters", 6: "attachments"}


def _flac(fd: int, size: int, at: int) -> tuple[list[dict], list[dict], int] | None:
    """A FLAC's metadata blocks (from ``at``, after any ID3v2 tag): stream info, seek table (its
    index), Vorbis comments (tags), cue sheet (chapters), pictures and padding. None if not FLAC."""
    if _pread(fd, at, 4) != b"fLaC":
        return None
    parts, pics, p = [{"part": "flac", "pos": at, "size": 4}], [], at + 4
    while p + 4 <= size:
        h = _pread(fd, p, 4)
        kind, n = h[0] & 0x7F, int.from_bytes(h[1:4], "big")
        part = FLAC_BLOCKS.get(kind, "other")
        if part == "attachments":
            body = _pread(fd, p + 4, min(n, 1024))
            ml = int.from_bytes(body[4:8], "big")
            mime = body[8:8 + ml].decode("latin-1", "replace")
            dl = int.from_bytes(body[8 + ml:12 + ml], "big")
            pics.append(_picture(fd, p, 4 + n, p + 4 + 12 + ml + dl + 20, "PICTURE", mime))
        parts.append({"part": part, "pos": p, "size": min(4 + n, size - p)})
        p += 4 + n
        if h[0] & 0x80:
            break
    return parts, pics, min(p, size)


def _audio(fd: int, size: int, path: str) -> dict:
    """An MP3 or FLAC (or another audio file with an ID3v2 tag in front): its tags and cover art in
    front of the audio, and an ID3v1 tag at its end. Anything else: the whole file as its streams."""
    parts, pics, start = _id3(fd, size)
    flac = _flac(fd, size, start)
    fmt = "flac" if flac else "mp3" if parts or path.lower().endswith(".mp3") else "stream"
    if flac:
        parts, pics, start = parts + flac[0], pics + flac[1], flac[2]
    end = size - 128 if size - 128 > start and _pread(fd, size - 128, 3) == b"TAG" else size
    parts.append({"part": "video", "pos": start, "size": end - start})  # (the streams: audio here)
    if end < size:
        parts.append({"part": "tags", "pos": end, "size": size - end})
    codec = {"flac": "A_FLAC", "mp3": "A_MPEG/L3"}.get(fmt, "")
    audio = fmt != "stream" or path.lower().endswith(AUDIO_EXT)
    return {"format": fmt, "parts": parts, "attachments": pics, "video_start": start, "video_end": end,
            "tracks": [{"type": "audio" if audio else "video", "codec": codec, "number": 1}]}


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
            if path.lower().endswith(AUDIO_EXT):
                out["tracks"] = [{"type": "audio", "codec": "", "number": 1}]  # (an M4A/M4B: its tracks aren't read)
        elif size:
            out.update(_audio(fd, size, path))
    except (OSError, ValueError, struct.error) as e:
        out["error"] = str(e)
    finally:
        os.close(fd)
    if out["format"] in ("mp3", "flac", "stream"):
        # (warm-up doesn't know these: the metadata at the end is what's after the audio)
        end = out.get("video_end") or size
        out["meta"] = [[p["pos"], p["size"]] for p in out["parts"] if p["pos"] >= end]
    else:
        meta, info = layout.tail_ranges(path, size, 0)
        out["meta"] = [list(r) for r in meta]
        out["video_start"] = out.get("video_start") if out.get("video_start") is not None else info.get("video_start")
    try:
        out["mix"] = cache_mix(path, out, out["blksize"] or -(-max(size, 1) // 4096) * 4096)
    except OSError:
        out["mix"] = None
    return out


MIX_KEYS = ("video", "meta", "unused", "video_disk", "meta_disk", "unused_disk", "round_disk")
HEAD_KINDS = ("header", "segment", "seekhead", "info", "tracks", "checksum", "id3", "flac")  # what a player reads first


def lz4_size(data: bytes) -> int:
    """About how many bytes LZ4 (ZFS's lz4, TrueNAS's default compression) makes of ``data``: its
    block format, greedy matching of 4 bytes or more within 64 KB, skipping ahead faster the longer
    nothing matches (as LZ4 does) — so data that doesn't compress (cover art) is passed over quickly."""
    n = len(data)
    if n < 13:
        return n + 1
    table: dict[bytes, int] = {}
    out, anchor, i, misses, limit = 0, 0, 0, 0, n - 5
    while i < limit:
        key = data[i:i + 4]
        cand = table.get(key)
        table[key] = i
        if cand is None or i - cand > 65535:
            misses += 1
            i += 1 + (misses >> 6)
            continue
        misses = 0
        j = i + 4
        while j < n - 5 and data[j] == data[cand + j - i]:
            j += 1
        lits, mlen = i - anchor, j - i - 4
        out += 1 + lits + ((lits - 15) // 255 + 1 if lits >= 15 else 0) + 2 + ((mlen - 15) // 255 + 1 if mlen >= 15 else 0)
        anchor = i = j
    lits = n - anchor
    return out + 1 + lits + ((lits - 15) // 255 + 1 if lits >= 15 else 0)


def store(mix: dict, record: int, video: int, meta: int, packed: int, unused: int) -> None:
    """Add one block to ``mix`` as ZFS stores it: compressed only if that saves 1/8 of it (ZFS's own
    rule), in whole 4K sectors — then its unused room (zeros, padding) is gone and its metadata takes
    what's left after the video (which doesn't compress); otherwise everything as it is.
    ``packed``: the metadata's bytes compressed."""
    mix["blocks"] = mix.get("blocks", 0) + 1
    mix["video"] += video
    mix["meta"] += meta
    mix["unused"] += unused
    mix["video_disk"] += video
    if record - (video + packed) >= record // 8:
        # (the last sector's unused rest: ZFS rounding, kept apart from the metadata it follows)
        mix["meta_disk"] += packed
        mix["round_disk"] = mix.get("round_disk", 0) + -(-(video + packed) // 4096) * 4096 - video - packed
    else:
        mix["meta_disk"] += meta
        mix["unused_disk"] += unused


def cache_mix(path: str, m: dict, record: int) -> dict | None:
    """What the blocks caching always keeps of a file (block_map: its start and its end, not the
    lead-in) hold: video (the streams), metadata, and unused room (padding, and the zeros past the end
    of the file) — as they are, and as ZFS stores them (``*_disk``, store()). Only the metadata is
    read: video doesn't compress, and zeros compress away. zlib stands in for ZFS's ZSTD (similar on
    metadata). ``kinds``: each kind of metadata's bytes, as they are, compressed, and compressed with
    LZ4 — what the optimizer's result is estimated from (projection.py). ``lz4``: the blocks as
    stored with LZ4 instead ({"meta_disk", "unused_disk"}: TrueNAS's default, for a file not known
    to be written with the dataset's compression)."""
    import zlib
    size = m.get("size") or 0
    bm = block_map(m, size, {"floor": layout.min_head(m.get("video_start"), record, size), "start": 0}, record)
    if not bm:
        return None
    parts = pieces(m)
    out = dict.fromkeys(MIX_KEYS, 0)
    out.update(blocks=0, record=record)
    lz4 = dict.fromkeys(MIX_KEYS, 0)
    kinds: dict[str, list[int]] = {}
    with open(path, "rb") as fh:
        for b in bm["start"] + bm["end"]:
            a = b["from"]
            e = a + record
            video = unused = 0
            data = b""
            for p in parts:
                lo, hi = max(a, p["pos"]), min(e, p["pos"] + p["size"])
                if hi <= lo:
                    continue
                if p["kind"] == "video":
                    video += hi - lo
                elif p["kind"] == "padding":
                    unused += hi - lo
                else:
                    fh.seek(lo)
                    got = fh.read(hi - lo)
                    data += got
                    k = kinds.setdefault(p["kind"], [0, 0, 0])
                    k[0] += len(got)
                    k[1] += len(zlib.compress(got, 6))
                    k[2] += lz4_size(got)
            unused += record - b["data"]
            store(out, record, video, record - video - unused, len(zlib.compress(data, 6)) if data else 0, unused)
            store(lz4, record, video, record - video - unused, lz4_size(data) if data else 0, unused)
    out["kinds"] = kinds
    out["lz4"] = {"meta_disk": lz4["meta_disk"], "unused_disk": lz4["unused_disk"], "round_disk": lz4["round_disk"]}
    return out


def cache_plan(m: dict | None, size: int, kbps: int, seconds: float, record: int | None, kind: str) -> dict:
    """What caching takes from a file: its start (the lead-in, never less than the metadata in front of
    the video and 60% of a record of video) and its metadata at the end, in whole records — as
    warm-up reads them. Without the file's layout, estimated (``estimated``)."""
    from .warmup import DEFAULT_FRONT, DEFAULT_INDEX, align_ranges, preload_bytes
    lead = preload_bytes(kbps or 1, seconds, size, record)
    if m and m.get("format") in ("mkv", "mp4", "mp3", "flac", "stream"):
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
    owner = None  # the part before: padding is the room left after it (an edit grew into it, or can)
    kinds = {t.get("type") for t in m.get("tracks") or []}
    audio = {"stream": "audio"} if kinds == {"audio"} else {}  # (an audio file's streams: named so)
    for p in m.get("parts") or []:
        atts = [a for a in m.get("attachments") or [] if p["part"] == "attachments" and p["pos"] <= a["pos"] < p["pos"] + p["size"]]
        if not atts:
            out.append(dict({"kind": p["part"], "pos": p["pos"], "size": p["size"]},
                            **({"of": owner} if p["part"] == "padding" and owner else {}), **(audio if p["part"] == "video" else {})))
            if p["part"] not in ("padding", "segment"):
                owner = p["part"]
            continue
        owner = "attachments"
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
                got.append(dict({"kind": p["kind"], "bytes": hi - lo}, **({"name": p["name"]} if p.get("name") else {}),
                                **({"of": p["of"]} if p.get("of") else {}), **({"stream": p["stream"]} if p.get("stream") else {})))
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
    kinds = {t.get("type") for t in m.get("tracks") or []}
    streams = [k for k in ("video", "audio", "subtitles") if k in kinds] or ["video"]  # (MP4: not read, so just "video")
    return {"record": record, "start": start, "end": end, "lead_blocks": lead, "streams": streams,
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
            # rounding: the rest of its last 4K sector, which ZFS stores but holds nothing
            out.append({"n": n, "data": len(data), "stored": stored, "compressed": stored < record,
                        "rounding": stored - packed if stored < record else 0})
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

    def get(self, path: str, st: os.stat_result | None = None, full: bool = False) -> dict | None:
        """The file's layout if it's known and the file hasn't changed since (``st``: its stat).
        ``full``: only one read with everything read now (MAP_VERSION), not just its layout."""
        with self.lock:
            e = self.files.get(path)
        if not e:
            return None
        if st is not None and e.get("id") != self._ident(st):
            return None
        if (e.get("v") or 0) < (MAP_VERSION if full else LAYOUT_VERSION):
            return None  # read with less than is read now: again
        return e.get("map")

    def read(self, path: str, force: bool = False) -> dict:
        """The file's layout: known, or read now (and kept). ``force``: read again even if unchanged."""
        st = os.stat(path)
        got = None if force else self.get(path, st, full=True)
        if got is None:
            got = describe(path)
            got["read"] = time.time()
            with self.lock:
                before = (self.files.get(path) or {}).get("before")
                self.files[path] = dict({"id": self._ident(st), "map": got, "v": MAP_VERSION}, **({"before": before} if before else {}))
                self.dirty = True
        return got

    def before(self, path: str) -> dict | None:
        """The file's mix before it was first optimized, as versions before file-history.json kept it
        with its layout (what that history starts from: filehistory.FileHistory.seed), or None."""
        with self.lock:
            return (self.files.get(path) or {}).get("before")

    def known(self, paths) -> int:
        with self.lock:
            return sum(1 for p in paths if p in self.files)

    def current(self, paths) -> int:
        """How many of ``paths`` are known as they are now (unchanged since they were inspected)."""
        n = 0
        for p in paths:
            try:
                n += self.get(p, os.stat(p), full=True) is not None  # (read by an older version: read again)
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
        tmp = f"{self.path}.{threading.get_ident()}.tmp"  # (the page and a survey can save at once)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, self.path)
        except OSError:
            self.dirty = True  # the config folder isn't writable: Health says so


class Inspector:
    """Reads the layout of a library's files for the Library Health page — only files new or changed
    since they were last read, unless asked for every one. One library at a time: starting one pauses
    the one running, and each picks up where it was paused. Not a file job: it only reads a few small
    parts of each file, so it never waits for (or holds up) optimizing."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.libs: dict[str, dict] = {}  # key -> {"todo": [paths], "done", "total", "read", "paused", "finished"}
        self.current: str | None = None
        self.working = False  # the reading thread is (still) going

    def start(self, key: str, force: bool = False) -> dict:
        """Inspect a library (resuming it if it was paused), pausing any other. ``force``: every file
        again. A library with nothing left to read isn't started (unless ``force``)."""
        key = str(key)
        app, maps = self.app, self.app.file_maps
        with self.lock:
            st = self.libs.get(key)
            if force or not st or not st["todo"]:
                paths = [app.map_path(f.path) for f in list(app.files) if str(f.library_key) == key]
                todo = paths if force else [p for p in paths if not self._current(maps, p)]
                if not todo and st:
                    self._history_soon(key, paths)
                    return self.snapshot_locked()  # nothing new since: the last inspection stands
                st = self.libs[key] = {"todo": todo, "done": 0, "total": len(todo), "read": 0, "paused": False,
                                       "finished": None if todo else time.time(), "force": bool(force)}
            if not st["todo"]:
                return self.snapshot_locked()
            st["paused"] = False
            for k, other in self.libs.items():
                if k != key and other["todo"]:
                    other["paused"] = True  # (one at a time: the other picks up where it was)
            self.current = key
            if not self.working:
                self.working = True
                threading.Thread(target=self._work, name="inspect", daemon=True).start()
            return self.snapshot_locked()

    def pause(self, key: str) -> None:
        with self.lock:
            st = self.libs.get(str(key))
            if st and st["todo"]:
                st["paused"] = True
            if self.current == str(key):
                self.current = None

    @staticmethod
    def _current(maps, path) -> bool:
        try:
            return maps.get(path, os.stat(path), full=True) is not None
        except OSError:
            return True  # missing: nothing to read

    def _work(self) -> None:
        maps, since_save = self.app.file_maps, 0
        while True:
            finished = None
            with self.lock:  # (what to do next, and whether to stop, decided together with start())
                st = self.libs.get(self.current) if self.current else None
                if not st or st["paused"]:
                    self.working = False
                    break
                if not st["todo"]:
                    st["finished"] = time.time()
                    finished, self.current, self.working = (self.current, st["read"]), None, False
                    path = None
                else:
                    path = st["todo"].pop(0)
            if finished:
                self._finished(*finished)
                break
            try:
                maps.read(path, force=st["force"])
                with self.lock:
                    st["read"] += 1
            except OSError:
                pass
            with self.lock:
                st["done"] += 1
            since_save += 1
            if since_save >= 200:
                maps.save()
                since_save = 0
        maps.save()

    def _history_soon(self, key: str, paths: list) -> None:
        """Files inspected before their history was kept (filehistory.py): put in it, in the background."""
        history = getattr(self.app, "history", None)
        if history is None or getattr(self, "_history_busy", False) or history.known(paths) >= len(paths):
            return
        self._history_busy = True

        def work():
            try:
                self.app.history_pass(key)
            finally:
                self._history_busy = False
        threading.Thread(target=work, name="history", daemon=True).start()

    def _finished(self, key: str, read: int) -> None:
        app, maps = self.app, self.app.file_maps
        everything = {app.map_path(f.path) for f in list(app.files)}
        if everything:  # (not when the library list is empty for now: that's Plex unreachable, not every file gone)
            maps.forget_missing(everything)  # files no longer in any library: their entries go
        maps.save()
        if getattr(app, "history", None) is not None:
            app.history_pass(key, everything)  # each file as it is now, in its history with the optimizer
        title = (app.library_info(key) or {}).get("title") or key
        # (the files actually read: not the missing ones)
        app.activity.event("library health", M("inspected {n} files in {library}", one="inspected {n} file in {library}",
                                                n=read, library=title))

    def snapshot_locked(self) -> dict:
        return {"current": self.current, "libs": {k: {"done": v["done"], "total": v["total"], "paused": v["paused"],
                                                      "running": k == self.current and not v["paused"] and bool(v["todo"]),
                                                      "finished": v["finished"]} for k, v in self.libs.items()}}

    def snapshot(self) -> dict:
        with self.lock:
            return self.snapshot_locked()

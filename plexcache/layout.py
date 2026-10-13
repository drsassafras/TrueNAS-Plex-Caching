"""Where a media file keeps the parts a player needs before it can start.

A player opening a file reads the header at the start, then — for many files —
jumps to the end for the index that lets it seek ("Cues" in MKV, ``moov`` in
an MP4 written without "fast start"), and for anything else the header points
to there (MKV tags, chapters, attachments such as subtitle fonts).

``tail_ranges()`` finds those parts by reading the file's own layout, so the
exact bytes can be cached, rather than guessing at "the last few MB".
"""

from __future__ import annotations

import os
import struct

HEAD_READ = 256 * 1024  # enough to hold an MKV header and SeekHead
MAX_TAIL = 64 * 1024 * 1024  # never cache more than this from the end of one file
MAX_BOXES = 64  # top-level MP4 boxes to walk before giving up

# MKV element IDs (with their length-marker bits, as they appear in the file)
EBML = 0x1A45DFA3
SEGMENT = 0x18538067
SEEKHEAD = 0x114D9B74
SEEK = 0x4DBB
SEEK_ID = 0x53AB
SEEK_POS = 0x53AC
CLUSTER = 0x1F43B675
WANTED = {  # level-1 elements a player may read when it opens the file
    0x1C53BB6B: "index",        # Cues
    0x1254C367: "tags",         # Tags
    0x1043A770: "chapters",     # Chapters
    0x1941A469: "attachments",  # Attachments (e.g. subtitle fonts)
    0x1549A966: "info",         # Info
    0x1654AE6B: "tracks",       # Tracks
}

MP4_EXT = (".mp4", ".m4v", ".mov", ".m4a", ".m4b", ".3gp")


def _pread(fd: int, offset: int, n: int) -> bytes:
    return os.pread(fd, n, offset)


def _vint(buf: bytes, pos: int, keep_marker: bool) -> tuple[int | None, int]:
    """An EBML variable-length integer at ``pos``: (value, length). value None = "unknown"."""
    if pos >= len(buf):
        raise ValueError("truncated")
    first = buf[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(buf):
        raise ValueError("bad vint")
    value = first if keep_marker else first & (mask - 1)
    all_ones = (first & (mask - 1)) == mask - 1
    for b in buf[pos + 1:pos + length]:
        value = (value << 8) | b
        all_ones = all_ones and b == 0xFF
    if not keep_marker and all_ones:
        return None, length
    return value, length


def _element(buf: bytes, pos: int) -> tuple[int, int | None, int]:
    """(id, size, header length) of the element at ``pos``."""
    eid, n1 = _vint(buf, pos, keep_marker=True)
    size, n2 = _vint(buf, pos + n1, keep_marker=False)
    return eid, size, n1 + n2


def _uint(data: bytes) -> int:
    return int.from_bytes(data, "big") if data else 0


def _seekhead_entries(buf: bytes, start: int, end: int) -> list[tuple[int, int]]:
    """[(element id, position relative to the segment), ...] from a SeekHead body."""
    out = []
    pos = start
    while pos < end:
        eid, size, hl = _element(buf, pos)
        if size is None:
            break
        body = pos + hl
        if eid == SEEK:
            sid, spos, p = None, None, body
            while p < body + size:
                cid, csize, chl = _element(buf, p)
                if csize is None:
                    break
                data = buf[p + chl:p + chl + csize]
                if cid == SEEK_ID:
                    sid = _uint(data)
                elif cid == SEEK_POS:
                    spos = _uint(data)
                p += chl + csize
            if sid is not None and spos is not None:
                out.append((sid, spos))
        pos = body + size
    return out


def _mkv(fd: int, size: int, head_bytes: int) -> tuple[list[tuple[int, int]], dict]:
    buf = _pread(fd, 0, min(size, HEAD_READ))
    eid, esize, hl = _element(buf, 0)
    if eid != EBML or esize is None:
        return [], {"format": "unknown"}
    pos = hl + esize
    eid, _seg_size, hl = _element(buf, pos)
    if eid != SEGMENT:
        return [], {"format": "mkv", "note": "no segment"}
    seg = pos + hl  # SeekHead positions are relative to here
    targets: dict[int, int] = {}
    extra_heads: list[tuple[int, int]] = []  # SeekHeads near the end: players read them too
    seen_heads = set()
    heads = []
    # Level-1 elements before the first Cluster: a SeekHead is normally the first one.
    p = seg
    while p < len(buf) - 12:
        eid, esize, ehl = _element(buf, p)
        if eid == CLUSTER or esize is None:
            break
        if eid == SEEKHEAD and p + ehl + esize <= len(buf):
            heads.append((p + ehl, p + ehl + esize))
        p += ehl + esize
    while heads:
        start, end = heads.pop(0)
        for sid, spos in _seekhead_entries(buf, start, end):
            if sid == SEEKHEAD and spos not in seen_heads:
                # A second SeekHead (often at the end) lists more elements.
                seen_heads.add(spos)
                at = seg + spos
                if at < size:
                    extra = _pread(fd, at, 64 * 1024)
                    try:
                        hid, hsize, hhl = _element(extra, 0)
                    except ValueError:
                        continue
                    if hid == SEEKHEAD and hsize is not None and hhl + hsize <= len(extra):
                        extra_heads.append((at, hhl + hsize))
                        for sid2, spos2 in _seekhead_entries(extra, hhl, hhl + hsize):
                            if sid2 in WANTED:
                                targets.setdefault(sid2, spos2)
            elif sid in WANTED:
                targets.setdefault(sid, spos)
    ranges, parts = [], {}
    for at, length in extra_heads:
        if head_bytes <= at < size:
            ranges.append((at, min(length, size - at)))
            parts["seekhead"] = parts.get("seekhead", 0) + length
    for sid, spos in targets.items():
        at = seg + spos
        if at < head_bytes or at >= size:
            continue  # already inside the part read from the start, or bogus
        hdr = _pread(fd, at, 16)
        try:
            hid, hsize, hhl = _element(hdr, 0)
        except ValueError:
            continue
        if hid != sid or hsize is None:
            continue
        length = min(hhl + hsize, size - at)
        ranges.append((at, length))
        parts[WANTED[sid]] = parts.get(WANTED[sid], 0) + length
    return ranges, {"format": "mkv", "parts": parts}


def _mp4(fd: int, size: int, head_bytes: int) -> tuple[list[tuple[int, int]], dict]:
    pos = 0
    for _ in range(MAX_BOXES):
        if pos + 8 > size:
            break
        hdr = _pread(fd, pos, 16)
        if len(hdr) < 8:
            break
        box_size, kind = struct.unpack(">I4s", hdr[:8])
        if box_size == 1:
            if len(hdr) < 16:
                break
            box_size = struct.unpack(">Q", hdr[8:16])[0]
        elif box_size == 0:
            box_size = size - pos
        if box_size < 8:
            break
        if kind == b"moov":
            if pos + box_size <= head_bytes:
                return [], {"format": "mp4", "index_at": "start", "parts": {}}
            start = max(pos, head_bytes)
            length = min(pos + box_size, size) - start
            return [(start, length)], {"format": "mp4", "index_at": "end", "parts": {"index": length}}
        pos += box_size
    return [], {"format": "mp4", "note": "no moov box found"}


def tail_ranges(path: str, size: int, head_bytes: int) -> tuple[list[tuple[int, int]], dict]:
    """Parts of the file a player reads on opening that lie beyond the first ``head_bytes``.

    Returns ([(offset, length), ...], info). Never raises for a bad or unknown
    file: it just returns no ranges. The total is capped at MAX_TAIL."""
    if size <= head_bytes:
        return [], {"format": "small"}
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        return [], {"format": "unreadable", "note": str(e)}
    try:
        magic = _pread(fd, 0, 12)
        if magic[:4] == b"\x1a\x45\xdf\xa3":
            ranges, info = _mkv(fd, size, head_bytes)
        elif magic[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip") or path.lower().endswith(MP4_EXT):
            ranges, info = _mp4(fd, size, head_bytes)
        else:
            ranges, info = [], {"format": "other"}
    except (OSError, ValueError, struct.error) as e:
        ranges, info = [], {"format": "unparsed", "note": str(e)}
    finally:
        os.close(fd)
    # Merge neighbours and cap the total.
    ranges.sort()
    merged: list[list[int]] = []
    for off, n in ranges:
        if merged and off <= merged[-1][0] + merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], off + n - merged[-1][0])
        else:
            merged.append([off, n])
    out, total = [], 0
    for off, n in merged:
        n = min(n, MAX_TAIL - total)
        if n <= 0:
            break
        out.append((off, n))
        total += n
    info["bytes"] = total
    return out, info

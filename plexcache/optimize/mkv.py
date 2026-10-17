"""Read (and for the optimizer, reorder) the top-level layout of a Matroska file.

Only the parts that matter for the optimized layout are read, with seeks rather than a pass
through the file: the elements before the first Cluster, the SeekHead's pointers to the rest,
the track list (for subtitle compression) and the attachment list (names and types, not data).

The optimized layout is::

    EBML header · SeekHead · Info · Tracks · Chapters      (small; players read them on opening)
    Clusters                                                (video, audio, subtitles — untouched)
    Attachments: least compressible first (images, if kept) → most compressible last
    Cues (the index) · Tags

so a file's lead-in is all video, everything compressible sits together at the end with the
zero padding of the last ZFS record, and image subtitles keep zlib (mkvmerge's default).
"""

from __future__ import annotations

import os

EBML, SEGMENT = 0x1A45DFA3, 0x18538067
SEEKHEAD, SEEK, SEEK_ID, SEEK_POS = 0x114D9B74, 0x4DBB, 0x53AB, 0x53AC
INFO, TRACKS, CHAPTERS, ATTACHMENTS, TAGS, CUES, CLUSTER = (
    0x1549A966, 0x1654AE6B, 0x1043A770, 0x1941A469, 0x1254C367, 0x1C53BB6B, 0x1F43B675)
VOID, CRC32 = 0xEC, 0xBF
TRACK_ENTRY, TRACK_NUMBER, CODEC_ID = 0xAE, 0xD7, 0x86
CONTENT_ENCODINGS, CONTENT_ENCODING, CONTENT_COMPRESSION, CONTENT_COMP_ALGO = 0x6D80, 0x6240, 0x5034, 0x4254
ATTACHED_FILE, FILE_NAME, FILE_MIME, FILE_DATA, FILE_UID, FILE_DESCRIPTION = 0x61A7, 0x466E, 0x4660, 0x465C, 0x46AE, 0x467E
NAMES = {SEEKHEAD: "SeekHead", INFO: "Info", TRACKS: "Tracks", CHAPTERS: "Chapters", ATTACHMENTS: "Attachments",
         TAGS: "Tags", CUES: "Cues", CLUSTER: "Cluster", VOID: "Void"}

IMAGE_SUBS = ("S_HDMV/PGS", "S_VOBSUB")  # image subtitles: mkvmerge zlib-compresses these by default
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")
MKV_EXT = (".mkv", ".mk3d", ".mka", ".mks")
BIG_VOID = 1024 * 1024  # empty space before the video worth rewriting away (left by in-place edits)


class NotMatroska(ValueError):
    pass


def _vint(b: bytes, p: int, marker: bool) -> tuple[int, int]:
    if p >= len(b):
        raise NotMatroska("truncated")
    first = b[p]
    n, mask = 1, 0x80
    while n <= 8 and not first & mask:
        mask >>= 1
        n += 1
    if n > 8 or p + n > len(b):
        raise NotMatroska("bad element")
    v = first if marker else first & (mask - 1)
    for x in b[p + 1:p + n]:
        v = (v << 8) | x
    if not marker and v == (1 << (7 * n)) - 1:
        return -1, n  # unknown size
    return v, n


def _children(b: bytes, start: int = 0, end: int | None = None):
    """(id, data start, data size) of the elements in ``b[start:end]``."""
    end = len(b) if end is None else end
    p = start
    while p < end:
        eid, n1 = _vint(b, p, True)
        size, n2 = _vint(b, p + n1, False)
        if size < 0:
            raise NotMatroska("unknown-size element inside a master element")
        yield eid, p + n1 + n2, size
        p += n1 + n2 + size


def _uint(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def is_image(name: str, mime: str) -> bool:
    return (mime or "").lower().startswith("image/") or (name or "").lower().endswith(IMAGE_EXT)


def read_layout(path: str) -> dict:
    """The file's top-level layout: {"segment_data": offset, "segment_size": n (-1 unknown),
    "front": [(name, offset, size)] up to the first Cluster, "first_cluster": offset,
    "seek": {name: offset}, "tracks": [...], "attachments": [...]}. Offsets are absolute."""
    with open(path, "rb") as fh:
        head = fh.read(256 * 1024)
        eid, n1 = _vint(head, 0, True)
        if eid != EBML:
            raise NotMatroska("not an EBML file")
        size, n2 = _vint(head, n1, False)
        p = n1 + n2 + size
        eid, n1 = _vint(head, p, True)
        if eid != SEGMENT:
            raise NotMatroska("no Segment")
        seg_size, n2 = _vint(head, p + n1, False)
        data = p + n1 + n2
        out = {"segment_data": data, "segment_size": seg_size, "segment_size_at": p + n1, "segment_size_len": n2,
               "front": [], "first_cluster": None, "seek": {}, "tracks": [], "attachments": [], "file_size": os.fstat(fh.fileno()).st_size}
        # Elements before the first Cluster, by seeking from one to the next
        p = data
        while p < out["file_size"]:
            fh.seek(p)
            hdr = fh.read(16)
            eid, n1 = _vint(hdr, 0, True)
            size, n2 = _vint(hdr, n1, False)
            if eid == CLUSTER:
                out["first_cluster"] = p
                break
            if size < 0:
                raise NotMatroska("unknown-size element before the clusters")
            name = NAMES.get(eid, hex(eid))
            out["front"].append((name, p, n1 + n2 + size))
            if eid == SEEKHEAD:
                fh.seek(p + n1 + n2)
                body = fh.read(size)
                for cid, cs, csz in _children(body):
                    if cid != SEEK:
                        continue
                    sid = spos = None
                    for did, ds, dsz in _children(body, cs, cs + csz):
                        if did == SEEK_ID:
                            sid = _uint(body[ds:ds + dsz])
                        elif did == SEEK_POS:
                            spos = _uint(body[ds:ds + dsz])
                    if sid in NAMES and spos is not None and sid != CLUSTER:
                        out["seek"].setdefault(NAMES[sid], data + spos)
            elif eid == TRACKS:
                fh.seek(p + n1 + n2)
                out["tracks"] = _tracks(fh.read(size))
            elif eid == ATTACHMENTS:
                out["attachments"] = _attachments(fh, p + n1 + n2, size)
            p += n1 + n2 + size
        if out["first_cluster"] is None:
            raise NotMatroska("no clusters")
        # Attachments the SeekHead points to after the clusters
        at = out["seek"].get("Attachments")
        if at and at > out["first_cluster"] and not out["attachments"]:
            fh.seek(at)
            hdr = fh.read(16)
            eid, n1 = _vint(hdr, 0, True)
            size, n2 = _vint(hdr, n1, False)
            if eid == ATTACHMENTS:
                out["attachments"] = _attachments(fh, at + n1 + n2, size)
        for name, off, _ in out["front"]:
            out["seek"].setdefault(name, off)
    return out


def _tracks(body: bytes) -> list[dict]:
    tracks = []
    for eid, s, size in _children(body):
        if eid != TRACK_ENTRY:
            continue
        t = {"number": None, "codec": "", "compression": None}
        for cid, cs, csz in _children(body, s, s + size):
            if cid == TRACK_NUMBER:
                t["number"] = _uint(body[cs:cs + csz])
            elif cid == CODEC_ID:
                t["codec"] = body[cs:cs + csz].decode("ascii", "replace").rstrip("\0")
            elif cid == CONTENT_ENCODINGS:
                for eid2, es, esz in _children(body, cs, cs + csz):
                    if eid2 != CONTENT_ENCODING:
                        continue
                    for fid, fs, fsz in _children(body, es, es + esz):
                        if fid == CONTENT_COMPRESSION:
                            algo = 0  # the spec's default: zlib
                            for gid, gs, gsz in _children(body, fs, fs + fsz):
                                if gid == CONTENT_COMP_ALGO:
                                    algo = _uint(body[gs:gs + gsz])
                            t["compression"] = algo
        tracks.append(t)
    return tracks


def _attachments(fh, start: int, size: int) -> list[dict]:
    """Each attachment's name, type, size and position, without reading its data."""
    files, p, end = [], start, start + size
    while p < end:
        fh.seek(p)
        hdr = fh.read(16)
        eid, n1 = _vint(hdr, 0, True)
        fsize, n2 = _vint(hdr, n1, False)
        if eid == ATTACHED_FILE:
            a = {"name": "", "mime": "", "description": "", "uid": None, "size": 0, "offset": p}
            q, qend = p + n1 + n2, p + n1 + n2 + fsize
            while q < qend:
                fh.seek(q)
                h2 = fh.read(16)
                cid, k1 = _vint(h2, 0, True)
                csz, k2 = _vint(h2, k1, False)
                if cid == FILE_DATA:
                    a["size"] = csz
                elif cid in (FILE_NAME, FILE_MIME, FILE_DESCRIPTION, FILE_UID) and csz < 4096:
                    fh.seek(q + k1 + k2)
                    v = fh.read(csz)
                    if cid == FILE_UID:
                        a["uid"] = _uint(v)
                    else:
                        a[{FILE_NAME: "name", FILE_MIME: "mime", FILE_DESCRIPTION: "description"}[cid]] = v.decode("utf-8", "replace")
                q += k1 + k2 + csz
            a["image"] = is_image(a["name"], a["mime"])
            files.append(a)
        p += n1 + n2 + fsize
    return files


def check(path: str, keep_images: bool = True) -> dict:
    """Whether a file has the optimized layout, and if not, why.

    {"applies": False} for files that aren't Matroska. Otherwise {"applies": True,
    "optimized": bool, "problems": [codes], "attachments": n, "images": n, "image_subs": n}.
    Problem codes: "attachments_front" (attachments before the video, in every lead-in),
    "tail_order" (the index or tags before the attachments), "images" (images kept where the
    library drops them), "images_order" (kept images not next to the video), "subs_uncompressed"
    (image subtitles without zlib), "void" (more than 1 MiB of empty space before the video).
    """
    if not path.lower().endswith(MKV_EXT):
        return {"applies": False}
    try:
        lay = read_layout(path)
    except (OSError, NotMatroska) as e:
        return {"applies": True, "optimized": False, "problems": ["unreadable"], "error": str(e)}
    problems = []
    first = lay["first_cluster"]
    atts = lay["attachments"]
    at = lay["seek"].get("Attachments")
    if atts and (at is None or at < first):
        problems.append("attachments_front")
    if atts and at is not None and at > first:
        if any(lay["seek"].get(x) is not None and lay["seek"][x] > first and lay["seek"][x] < at for x in ("Cues", "Tags")):
            problems.append("tail_order")
    images = [a for a in atts if a["image"]]
    if images and not keep_images:
        problems.append("images")
    elif images:
        seen_other = False
        for a in atts:
            if not a["image"]:
                seen_other = True
            elif seen_other:
                problems.append("images_order")
                break
    subs = [t for t in lay["tracks"] if t["codec"] in IMAGE_SUBS]
    if any(t["compression"] != 0 for t in subs):
        problems.append("subs_uncompressed")
    if sum(size for name, _, size in lay["front"] if name == "Void") > BIG_VOID:
        problems.append("void")
    return {"applies": True, "optimized": not problems, "problems": problems, "attachments": len(atts),
            "images": len(images), "image_subs": len(subs)}


# ---------------------------------------------------------------- writing

def _enc_id(eid: int) -> bytes:
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big")


def _enc_size(n: int, width: int | None = None) -> bytes:
    width = width or next(w for w in range(1, 9) if n < (1 << (7 * w)) - 1)
    return ((1 << (7 * width)) | n).to_bytes(width, "big")


def _element(eid: int, body: bytes) -> bytes:
    return _enc_id(eid) + _enc_size(len(body)) + body


def reorder_tail(path: str, order=("Attachments", "Cues", "Tags")) -> list[str]:
    """Rewrite, in place, the elements after the last Cluster in ``order`` (others after them),
    and the SeekHead to match. Nothing else moves: the file's size, its clusters and the cluster
    positions in the Cues stay as they are. Returns the new order of the tail."""
    lay = read_layout(path)
    data = lay["segment_data"]
    wanted = [x for x in ("Attachments", "Cues", "Tags", "Chapters") if lay["seek"].get(x, 0) > lay["first_cluster"]]
    if not wanted:
        return []
    tail_start = min(lay["seek"][x] for x in wanted)
    with open(path, "r+b") as fh:
        fh.seek(tail_start)
        tail = fh.read()
        els, q = [], 0
        while q < len(tail):
            eid, n1 = _vint(tail, q, True)
            size, n2 = _vint(tail, q + n1, False)
            if eid == CLUSTER or size < 0:
                raise NotMatroska("a Cluster after the index: not the layout mkvmerge writes")
            els.append((NAMES.get(eid, hex(eid)), tail[q:q + n1 + n2 + size]))
            q += n1 + n2 + size
        rank = {name: i for i, name in enumerate(order)}
        els.sort(key=lambda e: rank.get(e[0], len(order)))
        new_pos, p = {}, tail_start
        for name, raw in els:
            new_pos.setdefault(name, p)
            p += len(raw)
        # The SeekHead, rebuilt with 8-byte positions in its old room (plus a Void after it, if any)
        sh = next((x for x in lay["front"] if x[0] == "SeekHead"), None)
        if sh is None:
            raise NotMatroska("no SeekHead")
        _, sh_off, room = sh
        i = lay["front"].index(sh)
        if i + 1 < len(lay["front"]) and lay["front"][i + 1][0] == "Void":
            room += lay["front"][i + 1][2]
        fh.seek(sh_off)
        old = fh.read(sh[2])
        eid, n1 = _vint(old, 0, True)
        size, n2 = _vint(old, n1, False)
        seeks = []
        for cid, cs, csz in _children(old, n1 + n2, n1 + n2 + size):
            if cid == CRC32:
                continue  # the SeekHead is rewritten, so its old checksum would be wrong
            if cid != SEEK:
                continue
            sid = spos = None
            for did, ds, dsz in _children(old, cs, cs + csz):
                if did == SEEK_ID:
                    sid = _uint(old[ds:ds + dsz])
                elif did == SEEK_POS:
                    spos = _uint(old[ds:ds + dsz])
            name = NAMES.get(sid)
            if name in new_pos:
                spos = new_pos[name] - data
            seeks.append((sid, spos))
        body = b"".join(_element(SEEK, _element(SEEK_ID, _enc_id(sid)) + _element(SEEK_POS, pos.to_bytes(8, "big")))
                        for sid, pos in seeks)
        new_sh = _element(SEEKHEAD, body)
        left = room - len(new_sh)
        if left < 0 or 0 < left < 2:
            raise NotMatroska("no room to rewrite the SeekHead")
        void = b"" if not left else (_enc_id(VOID) + _enc_size(left - 9, 8) + b"\0" * (left - 9) if left >= 9
                                     else _enc_id(VOID) + _enc_size(left - 2, 1) + b"\0" * (left - 2))
        fh.seek(sh_off)
        fh.write(new_sh + void)
        fh.seek(tail_start)
        fh.write(b"".join(raw for _, raw in els))
        fh.flush()
        os.fsync(fh.fileno())
    return [name for name, _ in els]

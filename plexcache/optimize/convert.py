"""Convert a video file to MKV (Matroska) with mkvmerge — only when nothing in it would be lost.

1. Inspected first (inspect()): what MKVToolNix reads of the file (``mkvmerge -J``) against what
   ffprobe sees in it. Anything MKV wouldn't keep — a track MKVToolNix can't carry (Teletext, MP4
   closed-caption tracks), chapters, cover art, details like an iTunes show and episode, DRM —
   and the file is left as it is, with the reason.
2. Converted (``mkvmerge``) into a hidden work folder next to it, then checked: every video and
   audio track there with the same codec and the same number of packets, every subtitle track
   there, and closed captions in the video still in it.
3. Optimized like any MKV (optimizer.optimize_file: the layout, cover art, tags, our tag).
4. Given the original's owner, permissions and timestamps, and put next to it as
   ``<name> MKV-<EXT>.mkv`` (``Film (2001) MKV-AVI.mkv``: what it was converted from), or where
   that's taken, ``<name> MKV-<EXT> 2.mkv``… (targets_of): never over an existing file. The
   original stays: the app deletes it once Plex has the MKV (converting.py), through
   delete_original() here.
"""

import json
import os
import shutil
import time

from . import VERSION, mkv
from .optimizer import Skipped, _GUI_PROGRESS, _copy_meta, _identity, _prepare, optimize_file
from .tools import VerifyError, run

# The containers that can be converted, by the file's extension. (WebM is Matroska already;
# WMV/ASF isn't read by MKVToolNix.)
TYPES = {
    "mp4": (".mp4", ".m4v"),
    "mov": (".mov", ".qt"),
    "avi": (".avi", ".divx"),
    "ts": (".ts", ".m2ts", ".mts", ".m2t", ".tp", ".trp"),
    "ps": (".vob", ".mpg", ".mpeg", ".mod", ".tod"),
    "flv": (".flv",),
    "ogm": (".ogm", ".ogv"),
    "rm": (".rm", ".rmvb"),
    "3gp": (".3gp", ".3g2"),
}
DEFAULT_TYPES = {"mp4": False, "mov": False, "avi": True, "ts": True, "ps": True, "flv": True, "ogm": True, "rm": True, "3gp": True}
# Details in a file that are only about how it was made: nothing is lost without them
TECHNICAL = {"major_brand", "minor_version", "compatible_brands", "encoder", "creation_time", "software", "handler_name",
             "vendor_id", "encoded_by", "encoding_tool", "duration", "timecode"}
DRM = {"drmi", "drms", "drmc", "encv", "enca"}  # an encrypted (store-bought) MP4's track types


def type_of(path: str) -> str | None:
    """The kind of container (a key of TYPES) by the file's extension, or None."""
    ext = os.path.splitext(path)[1].lower()
    return next((t for t, exts in TYPES.items() if ext in exts), None)


def target_of(path: str) -> str:
    """A file's MKV's name: ``<name> MKV-<EXT>.mkv`` (what it was converted from in its name)."""
    return targets_of(path)[0]


MAX_TARGETS = 50  # names tried for a file's MKV before giving up


def targets_of(path: str) -> list[str]:
    """The names a file's MKV can have, in the order they're tried: ``<name> MKV-<EXT>.mkv`` (the
    original's extension: ``Film (2001) MKV-AVI.mkv``), then ``<name> MKV-<EXT> 2.mkv``…. Plex still
    sees the same title, and lists it as another version of the item."""
    stem, ext = os.path.splitext(path)
    base = f"{stem} MKV-{ext.lstrip('.').upper()}"
    return [base + ".mkv"] + [f"{base} {n}.mkv" for n in range(2, MAX_TARGETS)]


def earlier_targets(path: str) -> list[str]:
    """The names MKVs were given before: ``<name>.mkv``, ``<name> - MKV.mkv``, ``<name> - MKV 2.mkv``…"""
    stem = os.path.splitext(path)[0]
    return [stem + ".mkv", stem + " - MKV.mkv"] + [f"{stem} - MKV {n}.mkv" for n in range(2, MAX_TARGETS)]


def is_target_of(path: str, new: str) -> bool:
    """Whether ``new`` is one of the names ``path``'s MKV can have (or had, converted before)."""
    return new in targets_of(path) or new in earlier_targets(path)


def _in_order(path: str, name: str = "") -> list[str]:
    """targets_of, with ``name`` (one of them, by its file name: the one the scan showed) first."""
    names = targets_of(path)
    want = os.path.join(os.path.dirname(path), os.path.basename(name)) if name else ""
    return [want] + [t for t in names if t != want] if want in names else names


def free_target(path: str, taken=()) -> str | None:
    """The first of a file's MKV names (targets_of) that no file has and isn't in ``taken`` (names
    other files' MKVs will have: a scan's), or None when every one is."""
    return next((t for t in targets_of(path) if t not in taken and not os.path.lexists(t)), None)


def _probe(path: str, count: bool = False) -> dict:
    args = ["-v", "error", "-show_streams", "-show_format", "-show_chapters", "-of", "json"]
    return json.loads(run("ffprobe", *(["-count_packets"] if count else []), *args, path) or "{}")


def _kind(s: dict) -> str | None:
    """A stream's kind as MKVToolNix names them, or None for one that isn't a track."""
    if (s.get("disposition") or {}).get("attached_pic"):
        return None  # cover art (MP4's covr): counted apart
    return {"video": "video", "audio": "audio", "subtitle": "subtitles"}.get(s.get("codec_type"))


def inspect(path: str) -> dict:
    """Whether the file can be converted without losing anything: {"ok", "type", "reasons": [what
    MKV wouldn't keep], "captions": closed captions in its video}."""
    reasons = []
    j = json.loads(run("mkvmerge", "-J", path) or "{}")
    c = j.get("container") or {}
    if not (c.get("recognized") and c.get("supported")):
        return {"ok": False, "type": type_of(path), "reasons": ["MKVToolNix can't read this container"], "captions": False}
    p = _probe(path)
    streams = p.get("streams") or []
    tracks: dict[str, int] = {}
    for t in j.get("tracks") or []:
        tracks[t.get("type")] = tracks.get(t.get("type"), 0) + 1
    seen: dict[str, list[str]] = {}
    for s in streams:
        if str(s.get("codec_tag_string") or "").lower() in DRM or (s.get("codec_name") == "none" and s.get("codec_type") in ("video", "audio")):
            return {"ok": False, "type": type_of(path), "reasons": ["protected by DRM (bought from a store): it can't be converted"], "captions": False}
        k = _kind(s)
        if k:
            seen.setdefault(k, []).append(s.get("codec_name") or s.get("codec_tag_string") or "?")
    for k, codecs in seen.items():
        if len(codecs) > tracks.get(k, 0):
            n = len(codecs) - tracks.get(k, 0)
            reasons.append(f"{n} of its {k} tracks ({', '.join(sorted(set(codecs)))}) wouldn't be kept")
    chapters = bool(j.get("chapters"))
    for s in streams:
        if s.get("codec_type") in ("data", "attachment") and not (s.get("disposition") or {}).get("attached_pic"):
            tag = str(s.get("codec_tag_string") or s.get("codec_name") or "data")
            if tag == "tmcd" or (tag == "text" and chapters):
                continue  # a timecode track; MP4 chapters (as chapters in the MKV)
            reasons.append(f"a data track ({tag}) wouldn't be kept")
    if p.get("chapters") and not chapters:
        reasons.append("its chapters wouldn't be kept")
    pics = sum(1 for s in streams if (s.get("disposition") or {}).get("attached_pic"))
    if pics > len(j.get("attachments") or []):
        reasons.append("its cover art wouldn't be kept")
    tags = {k.lower() for k in ((p.get("format") or {}).get("tags") or {})} - TECHNICAL
    if (c.get("properties") or {}).get("title"):
        tags.discard("title")  # (kept: the MKV's title)
    if tags:
        reasons.append(f"details in the file ({', '.join(sorted(tags))}) wouldn't be kept")
    captions = any(s.get("closed_captions") for s in streams if s.get("codec_type") == "video")
    return {"ok": not reasons, "type": type_of(path), "reasons": reasons, "captions": captions}


def verify(path: str, out: str, captions: bool) -> None:
    """The MKV holds what the original did: each video and audio track with the same codec and the
    same number of packets, as many subtitle tracks (MP4 text subtitles become SRT), and closed
    captions in the video if there were. Raises VerifyError."""
    def tracks(x):
        return [s for s in _probe(x, count=True).get("streams") or [] if _kind(s)]
    a, b = tracks(path), tracks(out)
    for kind in ("video", "audio"):
        sa = sorted((s.get("codec_name"), int(s.get("nb_read_packets") or 0)) for s in a if _kind(s) == kind)
        sb = sorted((s.get("codec_name"), int(s.get("nb_read_packets") or 0)) for s in b if _kind(s) == kind)
        if [c for c, _ in sa] != [c for c, _ in sb]:
            raise VerifyError(f"the MKV's {kind} tracks differ from the original's")
        for (_, na), (_, nb) in zip(sa, sb):
            if abs(na - nb) > max(2, na // 1000):
                raise VerifyError(f"a {kind} track lost packets ({na} → {nb})")
    if sum(1 for s in a if _kind(s) == "subtitles") != sum(1 for s in b if _kind(s) == "subtitles"):
        raise VerifyError("the MKV's subtitle tracks differ from the original's")
    if captions and not any(s.get("closed_captions") for s in b if _kind(s) == "video"):
        raise VerifyError("the closed captions in the video weren't kept")


def convert_file(path: str, images: str = "compress", cover: bytes | None = None, tags_mode: str = "keep",
                 log=lambda text: None, progress=lambda pct: None, name: str = "") -> dict:
    """Convert ``path`` to ``<name> MKV-<EXT>.mkv`` next to it (or the next free name: targets_of),
    optimized; ``name``: the one of those names to give it (the one the app showed), while it's free.
    The original stays. Returns {"result": "converted", "new", ...}; raises Skipped (with what MKV wouldn't keep), VerifyError,
    ToolError or OSError (nothing is left behind then)."""
    started = time.time()
    kind = type_of(path)
    if not kind:
        raise Skipped("not a kind of file that's converted")
    if free_target(path) is None:
        raise Skipped("every name for its MKV is taken by a file there already")
    log("inspecting")
    ins = inspect(path)
    if not ins["ok"]:
        raise Skipped("; ".join(ins["reasons"]))
    st, folder, work = _prepare(path)
    try:
        out = os.path.join(work, "converted.mkv")
        log("converting to MKV")

        def line(text):
            m = _GUI_PROGRESS.match(text)
            if m:
                progress(int(m.group(1)) / 3)
        run("mkvmerge", "--gui-mode", "-o", out, path, lines=line)
        log("checking the MKV against the original")
        verify(path, out, ins["captions"])
        progress(40)
        r = optimize_file(out, images, cover=cover, tags_mode=tags_mode, log=log,
                          progress=lambda pct: progress(40 + pct * 0.6))
        now = os.stat(path)
        if (now.st_size, now.st_mtime_ns, now.st_ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
            raise Skipped("the file changed while it was being converted")
        _copy_meta(st, path, out, False)
        for target in _in_order(path, name):  # the first free name (never over a file that appeared meanwhile: link refuses)
            try:
                os.link(out, target)
                break
            except FileExistsError:
                continue
        else:
            raise Skipped("every name for its MKV is taken by a file there already")
        os.unlink(out)
        dfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        ident = _identity(target)
        return {"result": "converted", "path": path, "new": target, "type": kind, "before": st.st_size, "after": ident["size"],
                "file": ident, "version": VERSION, "tags": r.get("tags"), "verified": "passed", "sha256": r.get("sha256"),
                "images": r.get("images"), "images_before": r.get("images_before"), "images_after": r.get("images_after"),
                "images_saved": r.get("images_saved"), "cover": r.get("cover"), "cover_hash": r.get("cover_hash"),
                "tags_stripped": r.get("tags_stripped"), "seconds": round(time.time() - started, 1)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def delete_original(path: str, new: str) -> dict:
    """Delete a converted file's original — only with its MKV there, a Matroska file of some size,
    and the original a plain file (never a folder or a link)."""
    if os.path.abspath(path) == os.path.abspath(new) or not is_target_of(path, new):
        raise ValueError("not a converted file's original")
    if not os.path.isfile(new) or os.path.getsize(new) == 0 or not mkv.check(new, True).get("applies"):
        raise ValueError("its MKV isn't there")
    st = os.lstat(path)
    if not os.path.isfile(path) or os.path.islink(path) or st.st_nlink > 1:
        raise ValueError("not a plain file")
    os.remove(path)
    return {"deleted": True}


def remove_converted(new: str, original: str) -> dict:
    """Remove a converted file's MKV (a revert) — only while its original is there, a plain file, and
    ``new`` is the MKV written for it."""
    if os.path.abspath(new) == os.path.abspath(original) or not is_target_of(original, new):
        raise ValueError("not the file's MKV")
    if not os.path.isfile(original) or os.path.islink(original):
        raise ValueError("the original isn't there")
    if not os.path.isfile(new) or os.path.islink(new):
        raise ValueError("the MKV isn't there")
    os.remove(new)
    return {"removed": True}

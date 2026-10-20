"""Optimize one MKV: remux it with MKVToolNix, put its attachments after the video in the
best order, check that every track still holds exactly the same data, and swap it in.

Steps, all in a hidden folder next to the file (so the swap is an atomic rename on the same
dataset, and the new file is written with the dataset's current record size and compression):

1. ``mkvmerge --no-attachments --deterministic <seed>``: a fresh file without attachments, the
   same bytes on any system for the same file (the seed comes from its content). Tracks are
   copied, with mkvmerge's default compression (zlib on PGS and VobSub image subtitles,
   nothing else); the index is rebuilt; chapters, titles and tags are kept, and our global
   tags (PLEXCACHE_OPTIMIZER, PLEXCACHE_COVER) are replaced, never added twice.
2. Cover art (``images``): "remove" drops every image attachment; "compress" keeps them,
   losslessly compressed (optimize/images); "plex" puts Plex's poster (``cover``, losslessly
   compressed) in their place as cover.jpg/cover.png, the name Matroska players look for;
   "keep" leaves them as they are.
3. The attachments, written by us after the video (mkv.attachments_element) — images first,
   then the rest from least to most compressible — with their original names, types,
   descriptions and IDs; then the index and tags after them (mkv.reorder_tail).
4. Checked: the layout and our tags; ffmpeg's hash of every video, audio and subtitle stream is
   the same as the original's; every attachment is byte for byte what was meant to go in
   (compressed images were checked pixel for pixel before).
5. Owner, permissions, timestamps and extended attributes copied, then an atomic rename.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import zlib

from . import VERSION
from . import compare as version_of
from . import images as imgs
from . import mkv
from .tools import ToolError, VerifyError, run

SAMPLE = 4 * 1024 * 1024  # bytes of each attachment test-compressed to sort them
IMAGE_MODES = ("compress", "remove", "plex", "keep")


class Skipped(Exception):
    """Not an error: the file was left as it is (the reason is the message)."""


def _compressibility(path: str) -> float:
    """Compressed share of the attachment's first 4 MiB: 1.0 = doesn't compress at all."""
    with open(path, "rb") as fh:
        data = fh.read(SAMPLE)
    return len(zlib.compress(data, 1)) / len(data) if data else 1.0


def _sha256(path: str, read=lambda n: None) -> str:
    """The file's SHA-256. ``read(n)``: called with each chunk's size (for progress)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(chunk)
            read(len(chunk))
    return h.hexdigest()


class _Hasher(threading.Thread):
    """sha256 of a file in the background — run alongside ffmpeg reading the same file, so
    the two share one pass over the disk."""

    def __init__(self, path: str):
        super().__init__(daemon=True)
        self.path, self.digest, self.error = path, None, None
        self.start()

    def run(self):
        try:
            self.digest = _sha256(self.path)
        except OSError as e:
            self.error = e

    def result(self) -> str:
        self.join()
        if self.error:
            raise self.error
        return self.digest


def _identity(path: str) -> dict:
    """What tells this exact file apart later without reading it: size, mtime and inode."""
    st = os.stat(path)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino}


# mkvmerge's own changes to streams, off: every packet is copied as it is (the check below insists on it).
# It would otherwise put the HEVC parameter sets in front of every key frame that lacks them, trim spaces
# at the start and end of text subtitles' lines, and rewrite bytes in text subtitles that aren't valid
# UTF-8 — copying every track as it is (force_passthrough_packetizer) covers all of these.
AS_THEY_ARE = "dont_normalize_parameter_sets,keep_whitespaces_in_text_subtitles,force_passthrough_packetizer"
# Copied as they are, image subtitles keep the compression they had: zlib on these, as mkvmerge does
IMAGE_SUBS = ("S_HDMV/PGS", "S_VOBSUB")


def stream_hashes(path: str, done=None, duration_ns: int = 0) -> str:
    """ffmpeg's hash of every video, audio and subtitle stream's packets (attachments and cover
    pictures left out). Compression inside the file doesn't change it: packets are compared as
    the player gets them. ``done(fraction)``: how far through the file it is (needs its duration)."""
    watch = None
    if done and duration_ns:
        def watch(line):
            if line.startswith("out_time_us="):
                done(min(1.0, int(line.split("=", 1)[1]) * 1000 / duration_ns))
    return run("ffmpeg", "-v", "error", *(("-nostats", "-progress", "pipe:2") if watch else ()), "-i", path,
               "-map", "0:V?", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-", lines=watch)


def _stages(progress, *weights: float):
    """Progress through a file done in stages: one function per stage taking that stage's own
    fraction (0–1), together reporting the whole file's percent to ``progress``."""
    total, start, out = sum(weights), 0.0, []
    for w in weights:
        out.append(lambda f, a=start, w=w: progress(round(100 * (a + w * max(0.0, min(1.0, f))) / total, 1)))
        start += w
    return out


_GUI_PROGRESS = re.compile(r"#GUI#progress (\d+)%")


def plan_attachments(atts: list[dict], files: dict, keep_images: bool) -> list[dict]:
    """The attachments to put back, in order: images first (none unless kept), then the rest
    from least to most compressible — so what compresses sits at the very end of the file."""
    keep = [a for a in atts if keep_images or not a["image"]]
    return sorted(keep, key=lambda a: (not a["image"], -_compressibility(files[a["id"]])))


FINAL = ("already as small as it gets", "not PNG or JPEG", "animated PNG")  # nothing more to do for these


def _images(atts: list[dict], files: dict, mode: str, cover: bytes | None, work: str, log) -> tuple[list, int, int]:
    """Apply the cover-art choice to the extracted attachments: (the attachments, bytes saved,
    images that could have been compressed but weren't — a tool missing or failing)."""
    if mode == "remove":
        return [a for a in atts if not a["image"]], 0, 0
    if mode == "plex" and cover:
        src = os.path.join(work, "plex-poster")
        with open(src, "wb") as fh:
            fh.write(cover)
        k = imgs.kind(src)
        if not k:
            raise Skipped("Plex's poster isn't a PNG or JPEG")
        atts = [a for a in atts if not a["image"]] + [
            {"id": "cover", "name": "cover" + imgs.EXT[k], "mime": imgs.MIME[k], "description": "", "uid": None,
             "size": len(cover), "image": True}]
        files["cover"] = src
    if mode == "keep":
        return atts, 0, 0
    saved = left = 0
    for a in atts:
        if not a["image"]:
            continue
        r = imgs.compress(files[a["id"]], files[a["id"]] + ".min")
        if r["path"] != files[a["id"]]:
            files[a["id"]] = r["path"]
            a["size"] = r["after"]
            saved += r["before"] - r["after"]
        elif r.get("why") not in FINAL:
            left += 1
            log(f"{a['name']} kept as it was: {r.get('why')}")
    if saved:
        log(f"cover art {saved:,} bytes smaller")
    return atts, saved, left


def fingerprint_seed(path: str) -> int:
    """mkvmerge's --deterministic seed for a file, from its content (its size, first and last MiB):
    the same file gives the same seed on every system, so two systems optimizing it write the very
    same bytes — and Syncthing sees no conflict."""
    size = os.path.getsize(path)
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            fh.seek(size - 1024 * 1024)
            h.update(fh.read(1024 * 1024))
    return int.from_bytes(h.digest()[:4], "big") % 2_000_000_000 + 1


TAG_MODES = ("keep", "excessive", "strip")
EXCESSIVE_TAGS = 256 * 1024  # tags larger than this are "excessive" (Library Health warns too)


def strips_tags(mode: str, check: dict) -> bool:
    """Whether a file's tags (all but ours) go, under the library's tag choice: "keep" never,
    "strip" whenever it has any, "excessive" when they're larger than EXCESSIVE_TAGS."""
    if not check.get("foreign_tags"):
        return False
    return mode == "strip" or (mode == "excessive" and (check.get("tags_size") or 0) > EXCESSIVE_TAGS)


def _global_tags(path: str, work: str, ours: dict, strip: bool = False) -> str:
    """The file's global tags without our old ones, with ``ours`` added, as an XML file for
    mkvmerge --global-tags (with --no-global-tags, so ours are never there twice). Track,
    chapter and edition tags aren't in it: mkvmerge copies those itself."""
    import xml.etree.ElementTree as ET
    src = os.path.join(work, "tags-in.xml")
    root = None
    try:
        if strip:
            raise OSError("stripped")  # (only ours, below)
        run("mkvextract", path, "tags", src)
        if os.path.getsize(src):
            root = ET.parse(src).getroot()
    except (ToolError, OSError, ET.ParseError):
        root = None
    if root is None:
        root = ET.Element("Tags")
    for tag in list(root):
        targets = tag.find("Targets")
        if targets is not None and any(c.tag.endswith("UID") for c in targets):
            root.remove(tag)  # a track's, chapter's or edition's
            continue
        for simple in tag.findall("Simple"):
            if (simple.findtext("Name") or "").startswith(mkv.OUR_TAG):
                tag.remove(simple)
        if not tag.findall("Simple"):
            root.remove(tag)
    ours_tag = ET.SubElement(root, "Tag")
    ET.SubElement(ours_tag, "Targets")
    for name, value in ours.items():
        simple = ET.SubElement(ours_tag, "Simple")
        ET.SubElement(simple, "Name").text = name
        ET.SubElement(simple, "String").text = value
    out = os.path.join(work, "tags.xml")
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    return out


COVER_PREFIX = "plex sha256:"  # PLEXCACHE_COVER: the poster as downloaded from Plex, hashed before it was compressed


def optimize_file(path: str, images: str = "compress", cover: bytes | None = None, verify: bool = True,
                  allow_owner_change: bool = False, force: bool = False, log=lambda text: None,
                  progress=lambda pct: None, tags_mode: str = "keep") -> dict:
    """Optimize ``path`` in place. Returns {"result": "optimized" | "already", ...}; raises
    Skipped for files left alone on purpose, ToolError/OSError when something failed (the
    original is untouched then).

    ``images``: "remove" (strip every image), "compress" (keep them, losslessly compressed),
    "plex" (``cover``, Plex's poster, in their place, compressed; without one, as "keep"), or
    "keep" (as they are). ``tags_mode``: "keep" the file's tags, strip them when "excessive", or
    "strip" them (strips_tags()) — the optimizer's own are always written. ``force``: write it again even if it's laid out right and tagged by this
    optimizer version (for the dataset's record size and compression, or new cover art).

    The output is deterministic (mkvmerge --deterministic, seeded from the file's content): the same
    file and choices give the same bytes on any system with the same tools. It's tagged
    PLEXCACHE_OPTIMIZER=<version>, and PLEXCACHE_COVER when its cover art is Plex's poster."""
    if images not in IMAGE_MODES:
        raise ValueError(f"images must be one of {', '.join(IMAGE_MODES)}")
    if images == "plex" and not cover:
        images = "keep"
    keep_images = images != "remove"
    started = time.time()
    before = mkv.check(path, keep_images)
    if not before.get("applies"):
        raise Skipped("not a Matroska file")
    tagged = (before.get("ours") or {}).get("PLEXCACHE_OPTIMIZER")
    if version_of(tagged) == "newer":
        raise Skipped(f"optimized by a newer optimizer ({tagged})")  # never downgrade
    if tags_mode not in TAG_MODES:
        raise ValueError(f"tags_mode must be one of {', '.join(TAG_MODES)}")
    strip = strips_tags(tags_mode, before)
    if before.get("optimized") and version_of(tagged) == "same" and not force and not strip:
        return {"result": "already", "path": path, "version": tagged, "file": _identity(path),
                "tags": before.get("ours") or {}}
    st, folder, work = _prepare(path)
    try:
        info = json.loads(run("mkvmerge", "-J", path))
        atts = []
        for a in info.get("attachments") or []:
            atts.append({"id": a["id"], "name": a.get("file_name") or f"attachment{a['id']}",
                         "mime": a.get("content_type") or "application/octet-stream",
                         "description": a.get("description") or "",
                         "uid": (a.get("properties") or {}).get("uid"), "size": a.get("size"),
                         "image": mkv.is_image(a.get("file_name") or "", a.get("content_type") or "")})
        files = {a["id"]: os.path.join(work, f"att{a['id']}") for a in atts}
        if atts:
            log(f"extracting {len(atts)} attachments")
            run("mkvextract", path, "attachments", *[f"{a['id']}:{files[a['id']]}" for a in atts])
        # Our tags: this optimizer's version, and Plex's poster if that's the cover art now
        tags = {"PLEXCACHE_OPTIMIZER": VERSION}
        old_cover = (before.get("ours") or {}).get("PLEXCACHE_COVER")
        cover_hash = None
        if images == "plex":
            cover_hash = hashlib.sha256(cover).hexdigest()
            tags["PLEXCACHE_COVER"] = COVER_PREFIX + cover_hash
        elif images in ("keep", "compress") and old_cover:
            tags["PLEXCACHE_COVER"] = old_cover  # still the same Plex poster
        out = os.path.join(work, "out.mkv")
        # how far through the file: remuxing, then reading the new file and the original to compare them
        remuxed, checked_new, checked_old = _stages(progress, 2, 1, 1) if verify else (_stages(progress, 1) + [None, None])
        duration = int(((info.get("container") or {}).get("properties") or {}).get("duration") or 0)
        log("remuxing")
        remuxed(0)

        def remux_line(line):
            m = _GUI_PROGRESS.match(line)
            if m:
                remuxed(int(m.group(1)) / 100)
        zlib = [a for t in info.get("tracks") or [] if (t.get("properties") or {}).get("codec_id") in IMAGE_SUBS
                for a in ("--compression", f"{t['id']}:zlib")]
        run("mkvmerge", "--gui-mode", "--deterministic", str(fingerprint_seed(path)), "--engage", AS_THEY_ARE, *zlib, "-o", out, "--no-attachments",
            "--no-global-tags", "--global-tags", _global_tags(path, work, tags, strip), *(["--no-track-tags", "--disable-track-statistics-tags"] if strip else []),
            path, lines=remux_line)
        remuxed(1)
        had = sum(a["image"] for a in atts)
        atts, saved, left = _images(atts, files, images, cover, work, log)
        order = plan_attachments(atts, files, keep_images)
        if order:
            log(f"adding {len(order)} attachments after the video")
        # each with a fixed ID (its own; Plex's poster's from its hash), so the output is the same everywhere
        element = mkv.attachments_element([
            {"name": a["name"], "mime": a["mime"], "description": a["description"], "path": files[a["id"]],
             "uid": a["uid"] or (int(cover_hash[:15], 16) + 1 if a["id"] == "cover" and cover_hash else
                                 int(hashlib.sha256(a["name"].encode()).hexdigest()[:15], 16) + 1)}
            for a in order]) if order else None
        mkv.reorder_tail(out, attachments=element)
        after = mkv.check(out, keep_images)
        if not after.get("optimized"):
            raise VerifyError(f"the new file isn't laid out as expected: {', '.join(after.get('problems', []))}")
        if (after.get("ours") or {}) != tags:
            raise VerifyError("the new file's tags aren't what was written")
        if verify:
            log("checking every stream is unchanged")
            hasher = _Hasher(out)
            if stream_hashes(out, checked_new, duration) != stream_hashes(path, checked_old, duration):
                raise VerifyError("the streams in the new file differ from the original")
            digest = hasher.result()
            got = json.loads(run("mkvmerge", "-J", out)).get("attachments") or []
            if [(g.get("file_name"), g.get("size")) for g in got] != [(a["name"], a["size"]) for a in order]:
                raise VerifyError("the attachments in the new file differ from the original")
            if got:
                check_dir = os.path.join(work, "check")
                os.makedirs(check_dir)
                run("mkvextract", out, "attachments", *[f"{g['id']}:{os.path.join(check_dir, str(g['id']))}" for g in got])
                for g, a in zip(got, order):
                    if _sha256(os.path.join(check_dir, str(g["id"]))) != _sha256(files[a["id"]]):
                        raise VerifyError(f"attachment {a['name']} changed")
        else:
            digest = _sha256(out)
        ident = _swap(st, path, out, folder, allow_owner_change)
        return {"result": "optimized", "path": path, "before": st.st_size, "after": ident["size"],
                "sha256": digest, "version": VERSION, "file": ident, "tags": tags, "verified": "passed" if verify else "skipped",
                "fixed": before.get("problems", []), "images": images, "images_before": had,
                "images_after": sum(a["image"] for a in order), "images_saved": saved, "images_left": left,
                "cover": cover_hash is not None, "cover_hash": cover_hash, "tags_stripped": strip,
                "seconds": round(time.time() - started, 1)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


WORK = re.compile(r"^\.plexcache-optimize-\d+-[0-9a-f]{10}$")  # a work folder next to a file being optimized
CONFLICT = re.compile(r"^(?P<base>.+)\.sync-conflict-\d{8}-\d{6}-[A-Z0-9]{7}(?P<ext>\.[^./]+)$")


def conflict_original(copy: str) -> str | None:
    """The file a Syncthing conflict copy is of ("film.sync-conflict-20261017-101500-ABCDEF1.mkv" →
    "film.mkv"), or None if it isn't one."""
    m = CONFLICT.match(os.path.basename(copy))
    return os.path.join(os.path.dirname(copy), m.group("base") + m.group("ext")) if m else None


def resolve_conflict(copy: str, original: str, log=lambda text: None) -> dict:
    """A Syncthing conflict copy that two systems made by optimizing the same file at once: delete
    it when it's ours — both it and the file carry the optimizer's tag, and every stream and
    attachment is the same. Anything else is kept (the result says why)."""
    if conflict_original(copy) != original or not os.path.exists(original):
        return {"result": "kept", "reason": "not a conflict copy of that file"}
    a, b = mkv.check(copy), mkv.check(original)
    if not (a.get("applies") and b.get("applies")):
        return {"result": "kept", "reason": "not Matroska"}
    va, vb = (a.get("ours") or {}).get("PLEXCACHE_OPTIMIZER"), (b.get("ours") or {}).get("PLEXCACHE_OPTIMIZER")
    if not (va and vb):
        return {"result": "kept", "reason": "not both optimized by Plex Cache"}
    log("comparing the streams")
    if stream_hashes(copy) != stream_hashes(original):
        return {"result": "kept", "reason": "the streams differ"}
    atts = lambda p: [(x.get("file_name"), x.get("size")) for x in json.loads(run("mkvmerge", "-J", p)).get("attachments") or []]
    if atts(copy) != atts(original):
        return {"result": "kept", "reason": "the attachments differ"}
    os.remove(copy)
    return {"result": "deleted", "reason": f"the same as {os.path.basename(original)}"}


def _prepare(path: str) -> tuple[os.stat_result, str, str]:
    """Checks before writing a new copy of ``path``, and the hidden work folder next to it (so the
    swap is a rename on the same dataset). (stat, folder, work folder); raises Skipped."""
    # The file itself, never a link to it: swapped in after the service checked the path, a link would make
    # the optimizer copy some other file (outside the media folders) into a media folder.
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise Skipped("a link or something else, not a plain file")
    if st.st_nlink > 1:
        raise Skipped("hard-linked (a rename would split the link)")
    folder = os.path.dirname(os.path.abspath(path))
    vfs = os.statvfs(folder)
    if vfs.f_bavail * vfs.f_frsize < st.st_size * 1.1 + 1024 ** 3:
        raise Skipped("not enough free space on the dataset for a copy")
    tag = hashlib.sha1(path.encode()).hexdigest()[:10]
    # A work folder left by an optimizer stopped mid-way (a restart) for this same file: one job runs
    # at a time, so it's nobody's now — and in a container the process ID is the same every time
    for name in os.listdir(folder):
        if WORK.match(name) and name.endswith("-" + tag):
            shutil.rmtree(os.path.join(folder, name), ignore_errors=True)
    work = os.path.join(folder, f".plexcache-optimize-{os.getpid()}-{tag}")
    os.makedirs(work, exist_ok=False)
    return st, folder, work


def _swap(st: os.stat_result, path: str, out: str, folder: str, allow_owner_change: bool) -> dict:
    """The original's owner, permissions and timestamps on the copy, then the copy renamed over
    the original — unless the original changed meanwhile. The new file's identity."""
    _copy_meta(st, path, out, allow_owner_change)
    now = os.stat(path)
    if (now.st_size, now.st_mtime_ns, now.st_ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
        raise Skipped("the file changed while it was being written again")
    os.replace(out, path)
    ident = _identity(path)
    dfd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return ident


def rewrite_file(path: str, allow_owner_change: bool = False, log=lambda text: None, progress=lambda pct: None) -> dict:
    """Write ``path`` again, byte for byte, so it takes its dataset's current record size and
    compression (ZFS applies both only when a file is written). Any kind of file. The copy is
    read back and must hash the same as the original before it's swapped in. Returns
    {"result": "rewritten", "sha256", "version", "file", ...}; raises Skipped, or OSError when
    something failed (the original is untouched then)."""
    started = time.time()
    st, folder, work = _prepare(path)
    try:
        out = os.path.join(work, "copy")
        log("copying")
        copied, checked = _stages(progress, 1, 1)  # how far through: copying, then reading the copy back
        size, n = max(1, st.st_size), {"copy": 0, "check": 0}

        def moved(stage, part):
            def add(k):
                n[stage] += k
                part(n[stage] / size)
            return add
        src_hash = hashlib.sha256()
        to_copy = moved("copy", copied)
        with open(path, "rb") as fi, open(out, "wb") as fo:
            for chunk in iter(lambda: fi.read(4 * 1024 * 1024), b""):
                src_hash.update(chunk)
                fo.write(chunk)
                to_copy(len(chunk))
            fo.flush()
            os.fsync(fo.fileno())
        log("checking the copy")
        digest = _sha256(out, moved("check", checked))
        if digest != src_hash.hexdigest():
            raise VerifyError("the copy differs from the original")
        ident = _swap(st, path, out, folder, allow_owner_change)
        return {"result": "rewritten", "path": path, "before": st.st_size, "after": ident["size"], "verified": "passed",
                "sha256": digest, "version": VERSION, "file": ident, "seconds": round(time.time() - started, 1)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


NFS4_ACL = "system.nfs4_acl_xdr"  # a TrueNAS (ZFS, acltype=nfsv4) file's ACL, as an extended attribute


def _copy_meta(st: os.stat_result, src: str, dst: str, allow_owner_change: bool) -> None:
    """The original's owner, extended attributes (incl. its NFSv4 ACL), permissions and timestamps,
    on the copy. The ACL comes first: on a TrueNAS dataset with ACLs (aclmode=restricted) chmod is
    refused for a file with a full ACL — the ACL sets its permissions."""
    try:
        os.chown(dst, st.st_uid, st.st_gid)
    except PermissionError:
        mine = os.stat(dst)
        if (mine.st_uid, mine.st_gid) != (st.st_uid, st.st_gid) and not allow_owner_change:
            raise Skipped(f"can't keep the file's owner ({st.st_uid}:{st.st_gid}) without permission to change owners") from None
    acl = False  # the original's ACL is on the copy
    if hasattr(os, "listxattr"):
        try:
            for name in os.listxattr(src):
                try:
                    os.setxattr(dst, name, os.getxattr(src, name))
                    acl = acl or name == NFS4_ACL
                except OSError as e:
                    if e.errno not in (errno.EPERM, errno.ENOTSUP, errno.EACCES):
                        raise
        except OSError as e:
            if e.errno not in (errno.ENOTSUP, errno.EPERM, errno.EACCES):
                raise
    if os.stat(dst).st_mode & 0o7777 != st.st_mode & 0o7777:
        try:
            os.chmod(dst, st.st_mode & 0o7777)
        except PermissionError:
            if not acl:
                raise
            # the ACL is the original's: its permissions are those (the mode bits only mirror it)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))

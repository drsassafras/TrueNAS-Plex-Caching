"""Optimize one MKV: remux it with MKVToolNix, put its attachments after the video in the
best order, check that every track still holds exactly the same data, and swap it in.

Steps, all in a hidden folder next to the file (so the swap is an atomic rename on the same
dataset, and the new file is written with the dataset's current record size and compression):

1. ``mkvmerge --no-attachments``: a fresh file without attachments. Tracks are copied, with
   mkvmerge's default compression (zlib on PGS and VobSub image subtitles, nothing else);
   the index is rebuilt; chapters, tags and titles are kept.
2. Cover art, as the library chooses (``images``): "compress" (the default) keeps every image
   attachment, losslessly compressed (optimize/images); "remove" drops them; "plex" puts Plex's
   poster (``cover``, losslessly compressed) in their place as cover.jpg/cover.png, the name
   Matroska players look for. "keep" leaves them as they are.
3. ``mkvpropedit --add-attachment``: the attachments, appended after the video — images first,
   then the rest from least to most compressible — with their original names, types,
   descriptions and UIDs.
4. The index and tags moved after the attachments (mkv.reorder_tail).
5. Checked: the layout; ffmpeg's hash of every video, audio and subtitle stream is the same as
   the original's; every attachment is byte for byte what was meant to go in (compressed images
   were checked pixel for pixel before).
6. Owner, permissions, timestamps and extended attributes copied, then an atomic rename.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import threading
import time
import zlib

from . import VERSION
from . import images as imgs
from . import mkv
from .tools import ToolError, run

SAMPLE = 4 * 1024 * 1024  # bytes of each attachment test-compressed to sort them
IMAGE_MODES = ("compress", "remove", "plex", "keep")


class Skipped(Exception):
    """Not an error: the file was left as it is (the reason is the message)."""


def _compressibility(path: str) -> float:
    """Compressed share of the attachment's first 4 MiB: 1.0 = doesn't compress at all."""
    with open(path, "rb") as fh:
        data = fh.read(SAMPLE)
    return len(zlib.compress(data, 1)) / len(data) if data else 1.0


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(chunk)
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


def stream_hashes(path: str) -> str:
    """ffmpeg's hash of every video, audio and subtitle stream's packets (attachments and cover
    pictures left out). Compression inside the file doesn't change it: packets are compared as
    the player gets them."""
    return run("ffmpeg", "-v", "error", "-i", path, "-map", "0:V?", "-map", "0:a?", "-map", "0:s?",
               "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-")


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


def optimize_file(path: str, images: str = "compress", cover: bytes | None = None, verify: bool = True,
                  allow_owner_change: bool = False, force: bool = False, log=lambda text: None) -> dict:
    """Optimize ``path`` in place. Returns {"result": "optimized" | "already", ...}; raises
    Skipped for files left alone on purpose, ToolError/OSError when something failed (the
    original is untouched then). ``images``: one of IMAGE_MODES; ``cover``: Plex's poster for
    "plex" (without one, "plex" compresses the images the file has). ``force``: rewrite it even
    if its layout is already right — for the dataset's current record size and compression, or
    to compress its cover art or put Plex's poster in."""
    if images not in IMAGE_MODES:
        raise ValueError(f"images must be one of {', '.join(IMAGE_MODES)}")
    keep_images = images != "remove"
    started = time.time()
    before = mkv.check(path, keep_images)
    if not before.get("applies"):
        raise Skipped("not a Matroska file")
    # Laid out right already: left as it is, unless its cover art is to be compressed or replaced
    # (whether images were compressed can't be read from the file)
    images_to_do = (images in ("compress", "plex") and before.get("images")) or (images == "plex" and cover)
    if before.get("optimized") and not force and not images_to_do:
        ident = _identity(path)
        digest = _sha256(path)
        if _identity(path) != ident:
            raise Skipped("the file changed while it was being hashed")
        return {"result": "already", "path": path, "sha256": digest, "version": VERSION, "file": ident}
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
        out = os.path.join(work, "out.mkv")
        log("remuxing")
        run("mkvmerge", "-q", "-o", out, "--no-attachments", path)
        had = sum(a["image"] for a in atts)
        atts, saved, left = _images(atts, files, images, cover, work, log)
        order = plan_attachments(atts, files, keep_images)
        if order:
            args = []
            for a in order:
                args += ["--attachment-name", a["name"], "--attachment-mime-type", a["mime"]]
                if a["description"]:
                    args += ["--attachment-description", a["description"]]
                if a["uid"]:
                    args += ["--attachment-uid", str(a["uid"])]
                args += ["--add-attachment", files[a["id"]]]
            log(f"adding {len(order)} attachments after the video")
            run("mkvpropedit", "-q", out, *args)
        mkv.reorder_tail(out)
        after = mkv.check(out, keep_images)
        if not after.get("optimized"):
            raise ToolError(f"the new file isn't laid out as expected: {', '.join(after.get('problems', []))}")
        if verify:
            log("checking every stream is unchanged")
            hasher = _Hasher(out)
            if stream_hashes(out) != stream_hashes(path):
                raise ToolError("the streams in the new file differ from the original")
            digest = hasher.result()
            got = json.loads(run("mkvmerge", "-J", out)).get("attachments") or []
            if [(g.get("file_name"), g.get("size")) for g in got] != [(a["name"], a["size"]) for a in order]:
                raise ToolError("the attachments in the new file differ from the original")
            if got:
                check_dir = os.path.join(work, "check")
                os.makedirs(check_dir)
                run("mkvextract", out, "attachments", *[f"{g['id']}:{os.path.join(check_dir, str(g['id']))}" for g in got])
                for g, a in zip(got, order):
                    if _sha256(os.path.join(check_dir, str(g["id"]))) != _sha256(files[a["id"]]):
                        raise ToolError(f"attachment {a['name']} changed")
        else:
            digest = _sha256(out)
        ident = _swap(st, path, out, folder, allow_owner_change)
        return {"result": "optimized", "path": path, "before": st.st_size, "after": ident["size"],
                "sha256": digest, "version": VERSION, "file": ident,
                "fixed": before.get("problems", []), "images": images, "images_before": had,
                "images_after": sum(a["image"] for a in order), "images_saved": saved, "images_left": left,
                "cover": bool(images == "plex" and cover),
                "seconds": round(time.time() - started, 1)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _prepare(path: str) -> tuple[os.stat_result, str, str]:
    """Checks before writing a new copy of ``path``, and the hidden work folder next to it (so the
    swap is a rename on the same dataset). (stat, folder, work folder); raises Skipped."""
    st = os.stat(path)
    if st.st_nlink > 1:
        raise Skipped("hard-linked (a rename would split the link)")
    folder = os.path.dirname(os.path.abspath(path))
    vfs = os.statvfs(folder)
    if vfs.f_bavail * vfs.f_frsize < st.st_size * 1.1 + 1024 ** 3:
        raise Skipped("not enough free space on the dataset for a copy")
    work = os.path.join(folder, f".plexcache-optimize-{os.getpid()}-{hashlib.sha1(path.encode()).hexdigest()[:10]}")
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


def rewrite_file(path: str, allow_owner_change: bool = False, log=lambda text: None) -> dict:
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
        src_hash = hashlib.sha256()
        with open(path, "rb") as fi, open(out, "wb") as fo:
            for chunk in iter(lambda: fi.read(4 * 1024 * 1024), b""):
                src_hash.update(chunk)
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        log("checking the copy")
        digest = _sha256(out)
        if digest != src_hash.hexdigest():
            raise OSError("the copy differs from the original")
        ident = _swap(st, path, out, folder, allow_owner_change)
        return {"result": "rewritten", "path": path, "before": st.st_size, "after": ident["size"],
                "sha256": digest, "version": VERSION, "file": ident, "seconds": round(time.time() - started, 1)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _copy_meta(st: os.stat_result, src: str, dst: str, allow_owner_change: bool) -> None:
    """The original's owner, permissions, extended attributes (ACLs) and timestamps, on the copy."""
    try:
        os.chown(dst, st.st_uid, st.st_gid)
    except PermissionError:
        mine = os.stat(dst)
        if (mine.st_uid, mine.st_gid) != (st.st_uid, st.st_gid) and not allow_owner_change:
            raise Skipped(f"can't keep the file's owner ({st.st_uid}:{st.st_gid}) without permission to change owners") from None
    os.chmod(dst, st.st_mode & 0o7777)
    if hasattr(os, "listxattr"):
        try:
            for name in os.listxattr(src):
                try:
                    os.setxattr(dst, name, os.getxattr(src, name))
                except OSError as e:
                    if e.errno not in (errno.EPERM, errno.ENOTSUP, errno.EACCES):
                        raise
        except OSError as e:
            if e.errno not in (errno.ENOTSUP, errno.EPERM, errno.EACCES):
                raise
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))

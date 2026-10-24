"""Caching health checks for one file (the Library Health page): each says Ideal, Pass or Warning,
with why, and the file takes the worst of them. They look only at what's already known — the file's
layout as last inspected (filemap.py), the optimizer's mark and the ZFS record (optimizing.py) — so
they cost no reads of the file.

* **Block size** — written in the record size that suits its library (Health → Libraries).
* **Compression** — written with the dataset's compression, as far as the ZFS record knows.
* **Start of the file** — what's in front of the video fits the first block with the video's start.
* **Index**, **Chapters** — after the video. In front of it nothing is compressed (the video around it isn't);
  at the end it is, and there's no drawback.
* **Tags** — small: they're cached with every file.
* **Cover art compressed** — losslessly compressed (by the optimizer, or a JPEG's own tables).
* **Container** — MKV (a video file in another container warns, saying whether the optimizer converts it).
* **Cover art** — none is ideal (nothing extra cached), Plex's poster passes, anything else warns;
  in audio files the other way round: cover art in the file is ideal, none passes.

Each is something the optimizer puts right: together they say whether a file needs it.
"""

from __future__ import annotations

import os

from .i18n import M

ORDER = {"ideal": 0, "pass": 1, "warn": 2}
TAGS_IDEAL, TAGS_PASS = 16 * 1024, 256 * 1024
NAMES = {"blocks": M("Block size"), "compression": M("Compression"), "front": M("Start of the file"),
         "index": M("Index"), "chapters": M("Chapters"), "tags": M("Tags"), "cover": M("Cover art compressed"),
         "cover_setting": M("Cover art"), "container": M("Container")}
# the library summary's line for each check that warned: what it says of the files, with how many
WARNED = {
    "blocks": lambda n: M("{n} files written in a block size that doesn’t suit the library", one="{n} file written in a block size that doesn’t suit the library", n=n),
    "compression": lambda n: M("{n} files whose compression isn’t known to be the dataset’s", one="{n} file whose compression isn’t known to be the dataset’s", n=n),
    "front": lambda n: M("{n} files with more in front of the video than the first block holds well", one="{n} file with more in front of the video than the first block holds well", n=n),
    "index": lambda n: M("{n} files with no index, or the index in front of the video", one="{n} file with no index, or the index in front of the video", n=n),
    "chapters": lambda n: M("{n} files with chapters in front of the video", one="{n} file with chapters in front of the video", n=n),
    "tags": lambda n: M("{n} files with over 256 KB of tags", one="{n} file with over 256 KB of tags", n=n),
    "container": lambda n: M("{n} video files that aren’t MKV", one="{n} video file that isn’t MKV", n=n),
}


def _size(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} KB"


def _rs(n: int) -> str:
    return f"{n // (1024 * 1024)}M" if n >= 1024 * 1024 else f"{n // 1024}K"


def check_file(app, f, path: str, m: dict | None, rec: dict, opts: dict, info: dict | None, zfs_state: str | None) -> list[dict]:
    """[{"id", "status", "text"}] for one file. ``m``: its layout (None: not inspected yet — no checks);
    ``rec``: checks.library_record() for its library; ``opts``: the library's optimizer choices;
    ``info``: what the optimizer knows of an MKV ({"ours": tags}), or None; ``zfs_state``: the ZFS
    record's word on its compression (zfslog), or None."""
    if not m:
        return []
    out = []

    def add(cid, status, text):
        out.append({"id": cid, "name": NAMES[cid], "status": status, "text": text})
    size = m.get("size") or getattr(f, "size", 0) or 0
    bs = m.get("blksize")
    if bs and rec.get("best"):
        if bs == rec["best"]:
            add("blocks", "ideal", M("Written in {size} blocks, the size that suits this library.", size=_rs(bs)))
        elif bs == rec.get("alt"):
            add("blocks", "pass", M("Written in {size} blocks, the second best for this library ({best} is best).", size=_rs(bs), best=_rs(rec["best"])))
        else:
            add("blocks", "warn", M("Written in {size} blocks; {best} suits this library. Optimize Now writes it again in the dataset’s record size.",
                                    size=_rs(bs), best=_rs(rec["best"])))
    if zfs_state:
        add("compression", {"optimal": "ideal", "assumed": "pass"}.get(zfs_state, "warn"),
            {"optimal": M("Written with the dataset’s compression, by the optimizer."),
             "assumed": M("Written after the dataset’s compression was set, so it has it."),
             "unknown": M("Not known: it was there before Plex Cache began keeping track. Optimize Now writes it with the dataset’s compression."),
             "not_optimal": M("Written with another compression than the dataset’s. Optimize Now writes it again.")}.get(zfs_state, ""))
    mkv = m.get("format") == "mkv"
    ours = (info or {}).get("ours") or {}
    vs = m.get("video_start")
    record = bs or rec.get("rs") or 1024 * 1024
    audio = {t.get("type") for t in m.get("tracks") or []} == {"audio"}
    if vs is not None and size > record:
        movable = [p for p in m.get("parts") or [] if p["part"] in ("chapters", "index", "tags") and p["pos"] < vs] if mkv else []
        if vs <= 0.4 * record and movable:
            # it fits, but what could sit at the end (where it compresses) takes room the video's start could have
            add("front", "pass", M("{size} in front of the video: the first block holds it with the video’s start, "
                                   "but it has metadata that belongs at the end. Optimize Now moves it there.", size=_size(vs)))
        elif vs <= 0.4 * record:
            add("front", "ideal", M("{size} in front of the audio: the first block holds it with the audio’s start.", size=_size(vs)) if audio
                else M("{size} in front of the video: the first block holds it with the video’s start.", size=_size(vs)))
        elif mkv:
            add("front", "warn", M("{size} in front of the video (attachments, usually): the start of every play takes more blocks. Optimize Now moves it to the end.",
                                   size=_size(vs)))
        elif audio:
            # MP3 and FLAC keep their tags and cover art in front, where players look for them
            add("front", "pass", M("{size} in front of the audio (cover art, usually): the format keeps it there, where players look for it. "
                                   "The start of every play takes more blocks; a smaller cover takes fewer.", size=_size(vs)))
        else:
            add("front", "warn", M("{size} in front of the video (the index, usually): the start of every play takes more blocks. "
                                   "The optimizer only rewrites MKV files.", size=_size(vs)))
    parts = m.get("parts") or []
    if mkv and vs is not None:
        index = [p for p in parts if p["part"] == "index"]
        if not index:
            add("index", "warn", M("No index: players can’t seek quickly. Remuxing it with MKVToolNix adds one."))
        elif all(p["pos"] >= vs for p in index):
            add("index", "ideal", M("After the video, with the rest of the metadata, where it compresses."))
        else:
            add("index", "warn", M("In front of the video, where it isn’t compressed. Optimize Now moves it to the end, where it is."))
        chapters = [p for p in parts if p["part"] == "chapters"]
        if chapters:
            add("chapters", "ideal" if all(p["pos"] >= vs for p in chapters) else "warn",
                M("After the video, with the rest of the metadata.") if all(p["pos"] >= vs for p in chapters)
                else M("In front of the video, where they aren’t compressed. Optimize Now moves them to the end, where they are."))
    tags = sum(p["size"] for p in parts if p["part"] == "tags")
    if tags:
        add("tags", "ideal" if tags <= TAGS_IDEAL else "pass" if tags <= TAGS_PASS else "warn",
            M("{size} of tags.", size=_size(tags)) if tags <= TAGS_PASS
            else M("{size} of tags: they’re cached with every file. Tools that write statistics tags can make them this large.", size=_size(tags)))
    images = [a for a in m.get("attachments") or [] if a.get("image")]
    optimized = bool(ours.get("PLEXCACHE_OPTIMIZER"))
    if images:
        worst = "ideal"
        for a in images:
            st = "ideal" if optimized or a.get("jpeg_tables") in ("optimized", "progressive") else "warn" if a.get("jpeg_tables") == "standard" else "pass"
            worst = max(worst, st, key=ORDER.get)
        if worst == "warn" and not mkv:
            # (the optimizer doesn't rewrite this kind of file: a pass, with what would make it smaller)
            add("cover", "pass", M("Not compressed losslessly: a JPEG with the standard tables. The optimizer only rewrites MKV files; "
                                   "jpegtran -optimize would make it smaller, every pixel the same."))
        else:
            add("cover", worst, M("Compressed losslessly by Plex Cache (where it helped).") if optimized
                else M("Not compressed losslessly: a JPEG with the standard tables. Optimize Now compresses it, every pixel the same.") if worst == "warn"
                else M("Already compressed losslessly (the JPEG’s own tables).") if worst == "ideal"
                else M("Not compressed by Plex Cache; whether it could be smaller isn’t known (PNG)."))
    if not audio and not mkv:  # (a video file in another container)
        _container(app, add, path, str(getattr(f, "library_key", "") or ""))
    if audio:
        # music and audiobooks: the file is the place for cover art (players and other libraries read it there)
        add("cover_setting", "ideal" if images else "pass",
            M("Cover art in the file, where music players and other libraries find it.") if images
            else M("No cover art in the file: players outside Plex show none."))
    elif mkv:
        # Ideal: no cover art at all (nothing extra cached); Pass: Plex's poster, put in by the optimizer;
        # anything else (art from elsewhere) is worth a look
        plex_cover = str(ours.get("PLEXCACHE_COVER") or "").startswith("plex ")
        mode = opts.get("images")
        if not images:
            add("cover_setting", "ideal", M("No cover art in the file."))
        elif plex_cover:
            add("cover_setting", "pass", M("Plex’s poster, put in by the optimizer."))
        else:
            add("cover_setting", "warn",
                M("Cover art that isn’t Plex’s poster. The library’s choice is to strip it: the optimizer takes it out.") if mode == "remove"
                else M("Cover art that isn’t Plex’s poster. The optimizer puts in Plex’s, or takes it out, as the library’s choice says.") if mode in ("plex", "unmatched")
                else M("Cover art that isn’t Plex’s poster. The library’s choice is to keep it (compressed)."))
    return out


def _container(app, add, path: str, library: str) -> None:
    """A video file that isn't MKV: what Convert to MKV does about it (tools/convert.py, converting.py) in its library."""
    from .optimize import convert
    name = (os.path.splitext(path)[1].lstrip(".") or "?").upper()
    kind = convert.type_of(path)
    op = getattr(app, "optimizing", None)
    tool = (getattr(app, "tools", None) or {}).get("convert")
    conv = app.conversions.get(path) if app is not None and hasattr(app, "conversions") else None
    if conv or os.path.exists(convert.target_of(path)):
        add("container", "pass", M("Converted: its MKV is next to it, and this file goes once Plex has the MKV."))
        return
    if not kind:
        add("container", "warn", M("Not MKV ({kind}): MKVToolNix can’t read this kind of file, so it isn’t converted.", kind=name))
        return
    try:
        why = op.convert_skipped(path, os.stat(path)) if op else None
    except OSError:
        why = None
    if why:
        add("container", "warn", M("Not MKV ({kind}), and not converted: {why}.", kind=name, why=why))
    elif tool is not None and not tool.enabled(library or None).get(kind):
        add("container", "warn", M("Not MKV ({kind}). Converting this kind of file is off for this library: Tools → Convert to MKV.", kind=name))
    else:
        add("container", "warn", M("Not MKV ({kind}): Convert to MKV converts it, if nothing in it would be lost.", kind=name))


def worst(checks: list[dict]) -> str | None:
    return max((c["status"] for c in checks), key=ORDER.get) if checks else None


def counts(checks: list[dict]) -> dict:
    """How many of a file's checks are Ideal, Pass and Warning."""
    out = dict.fromkeys(ORDER, 0)
    for c in checks:
        out[c["status"]] += 1
    return out


def summarize(results: list[list[dict]]) -> dict:
    """A library's files: how many are Ideal, Pass or Warning; for each check that warned, how many
    files it warned of, said in words (``warnings``: [{"id", "n", "text"}], most first); and of the
    files with cover art, how many have it compressed losslessly (``cover``: {"files", "compressed"}).
    The cover art setting isn't listed: it follows the library's own choice."""
    out = {"ideal": 0, "pass": 0, "warn": 0, "checked": 0, "warnings": [], "cover": {"files": 0, "compressed": 0}}
    warned = {}
    for checks in results:
        w = worst(checks)
        if not w:
            continue
        out["checked"] += 1
        out[w] += 1
        for c in checks:
            if c["id"] == "cover":
                out["cover"]["files"] += 1
                out["cover"]["compressed"] += c["status"] == "ideal"
            if c["status"] == "warn" and c["id"] in WARNED:
                warned[c["id"]] = warned.get(c["id"], 0) + 1
    out["warnings"] = [{"id": k, "n": n, "text": WARNED[k](n)} for k, n in sorted(warned.items(), key=lambda kv: -kv[1])]
    return out


def context(app, path: str, m: dict | None, key: str, rec: dict) -> tuple[dict, dict | None, str | None]:
    """What the checks (and projection.py) need of a file besides its layout, all kept already (no
    reads of the file): (the library's optimizer choices, what the optimizer knows of the MKV, the
    ZFS record's word on its compression)."""
    op = getattr(app, "optimizing", None)
    if op is None or not m:
        return {}, None, None
    opts = op.options(key)
    info = zstate = None
    try:
        st = os.stat(path)
        if m.get("format") == "mkv" and opts.get("mkv"):
            info = op._mkv_info(path, st)
        zstate = op.zfs.status(path, st.st_ino, rec.get("dataset"))
    except (OSError, ValueError):
        pass
    except Exception:  # noqa: BLE001 — a file whose tags can't be read: checked without them
        info = None
    return opts, info, zstate


def for_file(app, f, path: str, m: dict | None, key: str, rec: dict) -> list[dict]:
    """check_file() with what it needs looked up (context())."""
    return check_file(app, f, path, m, rec, *context(app, path, m, key, rec))

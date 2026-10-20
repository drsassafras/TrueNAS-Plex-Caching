"""What optimizing would make of a file, estimated from its inspection (filemap.cache_mix): the
metadata it takes out (cover art and tags, as the library chooses), the rest moved to the end where
it compresses, and the file written in the dataset's record size — and so what caching would keep
of it then, against now. Nothing is read: each kind of metadata's size, as it is and compressed,
was measured when the file was inspected.

Only MKV files the optimizer would rewrite are projected. A Plex poster the optimizer would put in
isn't counted (its size isn't known until it's fetched): a file's own cover art stands in for it.
"""

from __future__ import annotations

from . import layout
from .filemap import MIX_KEYS, store


def _ratio(kinds: dict, kind: str) -> float:
    """How well a kind of metadata compressed when the file was inspected (1: not measured)."""
    raw, packed = (kinds.get(kind) or [0, 0])[:2]
    return packed / raw if raw else 1.0


def removed(m: dict, images: str, strip_tags: bool) -> dict:
    """The metadata the optimizer takes out of the file: {"image": bytes, "tags": bytes}."""
    out = {}
    if images == "remove":
        out["image"] = sum(a["size"] for a in m.get("attachments") or [] if a.get("image"))
    if strip_tags:
        out["tags"] = max(0, sum(p["size"] for p in m.get("parts") or [] if p["part"] == "tags") - OURS_TAGS)
    return {k: v for k, v in out.items() if v}


OURS_TAGS = 64  # about what our own tag takes, once the file's are stripped


def layout_after(m: dict, images: str, strip_tags: bool, cover_size: int | None = None) -> tuple[list[tuple[str, int]], list[tuple[str, int]]] | None:
    """The file's metadata as the optimizer leaves it: (in front of the video, after it), each as
    [(kind, bytes)]. In front, everything stays where it is but the attachments: the room the chapters
    took becomes padding (the optimizer edits the file's front in place). After the video: the
    attachments (images first, less what's taken out), the chapters, the index and the tags."""
    parts, vs = m.get("parts") or [], m.get("video_start") or 0
    front, tail = [], []
    for p in parts:
        if p["pos"] >= vs or p["part"] in ("video", "segment"):
            if p["part"] == "segment" and p["pos"] < vs:
                front.append(("segment", p["size"]))
            continue
        if p["part"] == "attachments":
            continue  # (moved: below)
        front.append(("padding" if p["part"] == "chapters" else p["part"], p["size"]))
    atts = m.get("attachments") or []
    images_kept = sum(a["size"] for a in atts if a.get("image")) if images != "remove" else 0
    if images == "plex" and cover_size:
        images_kept = cover_size  # Plex's poster in place of the file's cover art
    others = sum(a["size"] for a in atts if not a.get("image"))
    if images_kept:
        tail.append(("image", images_kept))
    if others:
        tail.append(("font", others))
    for kind in ("chapters", "index", "tags", "other"):
        n = sum(p["size"] for p in parts if p["part"] == kind)
        if kind == "tags" and strip_tags:
            n = min(n, OURS_TAGS)
        if n:
            tail.append((kind, n))
    return front, tail


def project(m: dict, record: int, images: str, strip_tags: bool, cover_size: int | None = None) -> dict | None:
    """The file as the optimizer would write it, in blocks of ``record``: {"mix" (as cache_mix
    gives it), "removed", "first_video" (video in its first block), "first_video_now"}. ``images``:
    what's done with its cover art (Optimizing.images_mode: "remove" takes it out); ``strip_tags``:
    whether its tags go; ``cover_size``: Plex's poster's size, when the cover art becomes it (unknown:
    the file's own cover art stands in). None if the file wasn't measured (inspected by an older version)."""
    mix = m.get("mix")
    if not mix or "kinds" not in mix or m.get("format") != "mkv" or not record:
        return None
    kinds = mix["kinds"]
    front, tail = layout_after(m, images, strip_tags, cover_size)
    video = max(0, (m.get("video_end") or m.get("size") or 0) - (m.get("video_start") or 0))
    # where each byte goes: [(start, end, "video" | "padding" | a kind of metadata)]
    spans, at = [], 0
    for kind, n in front:
        spans.append((at, at + n, kind))
        at += n
    head = at
    spans.append((at, at + video, "video"))
    at += video
    for kind, n in tail:
        spans.append((at, at + n, kind))
        at += n
    size = at
    out = dict.fromkeys(MIX_KEYS, 0)
    out.update(blocks=0, record=record)
    first = max(1, -(-layout.min_head(head, record, size) // record))
    last = -(-size // record)
    tail_from = (head + video) // record
    for n in sorted(set(range(min(first, last))) | set(range(max(first, tail_from), last))):
        a, b = n * record, (n + 1) * record
        v = meta = packed = unused = 0
        end_block = n >= first
        for lo, hi, kind in spans:
            got = max(0, min(b, hi) - max(a, lo))
            if not got:
                continue
            if kind == "video":
                v += got
                if end_block:
                    out["video_tail"] = out.get("video_tail", 0) + got
            elif kind == "padding":
                unused += got
            else:
                meta += got
                packed += int(got * _ratio(kinds, kind))
        unused += record - max(0, min(b, size) - a)
        store(out, record, v, meta, packed, unused)
    rs_now = mix.get("record") or record
    gone = removed(m, images, strip_tags)
    # what's taken out, as it's stored now: each kind as well as it compressed when inspected (a JPEG
    # hardly at all, tags well)
    gone_disk = int(sum(n * _ratio(kinds, k) for k, n in gone.items()))
    out.setdefault("video_tail", 0)
    kept = {}
    for kind, n in front + tail:
        if kind != "padding":
            kept[kind] = kept.get(kind, 0) + n
    return {"mix": out, "kinds": kept, "removed": gone, "removed_disk": gone_disk, "images": images, "strip_tags": strip_tags,
            "has_images": any(a.get("image") for a in m.get("attachments") or []),
            "cover": {"before": sum(a["size"] for a in m.get("attachments") or [] if a.get("image")),
                      "after": dict(tail).get("image", 0), "known": images != "plex" or bool(cover_size)},
            "tags": sum(p["size"] for p in m.get("parts") or [] if p["part"] == "tags"), "first_video": max(0, min(record, size) - head),
            "first_video_now": max(0, min(rs_now, m.get("size") or 0) - (m.get("video_start") or 0))}


# ---------------------------------------------------------------- the page's charts

def tail_video(m: dict) -> int | None:
    """The video in a file's end blocks (sharing them with the metadata there): cached with it, but no
    use to the lead-in. From its layout as inspected (cache_mix's blocks); None without one."""
    from .filemap import block_map, pieces
    mix = m.get("mix") or {}
    record, size = mix.get("record"), m.get("size") or 0
    if not record or not m.get("parts"):
        return None
    bm = block_map(m, size, {"floor": layout.min_head(m.get("video_start"), record, size), "start": 0}, record)
    if not bm or not bm["end"]:
        return 0
    return sum(p["bytes"] for b in bm["end"] for p in b["pieces"] if p["kind"] == "video")


# The metadata a play needs: the file's header and track list, its table of contents and index, its
# chapters, and the fonts its subtitles use. Not: tags, cover art and other attachments.
MEANINGFUL = ("header", "segment", "seekhead", "info", "tracks", "checksum", "chapters", "index", "attachments", "font", "id3", "flac")


def view(mix: dict, kinds: dict, tail: int | None, out: dict | None = None, out_disk: int = 0, gone: bool = False) -> dict:
    """One row of Library Health's charts — what the always-cached blocks hold, two ways:
    "stream" (what a play reads: the file's own bytes, not the unused room after its end) and "cache"
    (as ZFS stores them: compressed; unused room counts as metadata, taking space where its block isn't
    compressed). Each: "lead" (video in the first blocks: what a play starts with), "tail" (video in the
    end blocks, cached with the metadata there: no use to the lead-in), "meta" (the metadata kept), "out"
    (what optimizing takes out: still there now — read and cached like the rest: players read cover art
    when they open a file, wherever it is — and gone once optimized, ``gone``), and "total" (everything
    there in that row: now, what's to be taken out included; once optimized, not). ``kinds``: the metadata kept, by kind ({kind: bytes}); ``out``: what's taken
    out, by kind; ``tail``: None when not known (all the video counts as the start's)."""
    out = {k: n for k, n in (out or {}).items() if n}
    out_raw = sum(out.values())
    video, tail = mix.get("video", 0), min(tail or 0, mix.get("video", 0))
    still = 0 if gone else out_raw  # (now, what's to go is part of what's cached)
    stream = {"lead": video - tail, "tail": tail, "meta": max(0, mix.get("meta", 0) - still), "out": out_raw}
    cache = {"lead": video - tail, "tail": tail,
             "meta": max(0, mix.get("meta_disk", 0) + mix.get("unused_disk", 0) - (0 if gone else out_disk)), "out": out_disk}
    for v in (stream, cache):
        v["total"] = v["lead"] + v["tail"] + v["meta"] + (0 if gone else v["out"])
    kept = {k: n for k, n in (kinds or {}).items() if n}
    if not gone:  # (now: what's to go, out of the metadata it's part of)
        for k, n in out.items():
            if k in kept:
                kept[k] = max(0, kept[k] - n)
    # meaningful: what's streamed less the dead weight — the video at the end, and metadata a play has no
    # use for (tags, cover art, other attachments); without each kind known, all the metadata counts
    useful = {k: n for k, n in kept.items() if k in MEANINGFUL and n}
    left = {k: n for k, n in kept.items() if k not in MEANINGFUL and n}
    if not gone:
        for k, n in out.items():
            left[k] = left.get(k, 0) + n
    if tail:
        left["unused_video"] = tail
    meta = sum(useful.values()) if kept else stream["meta"]
    meaningful = {"lead": stream["lead"], "tail": 0, "meta": meta, "out": 0, "total": stream["lead"] + meta}
    # as stored: the metadata it keeps compressed as well as the rest of the metadata kept (an estimate)
    cmeta = int(cache["meta"] * meta / stream["meta"]) if stream["meta"] else 0
    stream["useful_meta"], stream["useful"] = meta, stream["lead"] + meta
    cache["useful_meta"], cache["useful"] = cmeta, cache["lead"] + cmeta
    return {"stream": stream, "meaningful": meaningful, "useful_kinds": useful, "left_out": left,
            "cache": cache, "kinds": {k: n for k, n in kept.items() if n}, "out_kinds": out,
            "unused_disk": mix.get("unused_disk", 0), "blocks": mix.get("blocks", 0), "split": tail is not None, "gone": gone}


def add_view(a: dict | None, b: dict) -> dict:
    """Two rows' numbers added up (a library's files)."""
    if a is None:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in b.items()}
    for k, v in b.items():
        if isinstance(v, dict):
            for kk, n in v.items():
                a[k][kk] = a[k].get(kk, 0) + n
        elif isinstance(v, bool):
            a[k] = a[k] and v if k == "split" else v
        else:
            a[k] = a.get(k, 0) + v
    return a


def views(m: dict, pr: dict | None, before: dict | None = None) -> dict:
    """A file's rows: "now", and "after" (once optimized: ``pr``, project()'s) or "before" (what it was
    before the optimizer wrote it, as kept then)."""
    mix = m.get("mix") or {}
    out = (pr or {}).get("removed") or {}
    now = view(mix, (mix.get("kinds") and {k: v[0] for k, v in mix["kinds"].items()}) or {}, tail_video(m), out, (pr or {}).get("removed_disk", 0))
    got = {"now": now}
    if pr:
        got["after"] = view(pr["mix"], pr.get("kinds") or {}, pr["mix"].get("video_tail", 0), out, pr.get("removed_disk", 0), gone=True)
    elif before:
        got["before"] = view(before, {k: v[0] for k, v in (before.get("kinds") or {}).items()}, None)
    return got

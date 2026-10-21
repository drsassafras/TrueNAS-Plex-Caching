"""Library Health: checks on the content itself, apart from caching — two kinds.

* **File** checks look at a file's tracks, as inspected (filemap): no audio; a spoken-language film
  with no subtitles; subtitles on a film with no linguistic content (its audio marked "zxx") that
  aren't forced ones — forced subtitles carry written content, and belong.
* **Plex** checks look at how Plex has an item: versions (several files on one item) whose files
  are named for different titles or years — a version added to the wrong item, most likely; a field
  you locked that holds nothing.

Each check can be turned off (settings "library_health.checks"). The Plex checks also run on both
servers' items in a Library Sync comparison, marking the items whose data looks bad before anything
would be copied.
"""

from __future__ import annotations

import os
import re

from .i18n import M
from .languages import language_label
from .plex import EDIT_FIELDS, TAG_FIELDS

LEEWAY_PCT, LEEWAY_SECS = 3, 120  # how far versions' running times may differ before they're flagged
CHECKS = {
    "file": [
        {"id": "no_audio", "name": M("No audio"), "help": M("A video file with no audio track at all.")},
        {"id": "no_subtitles", "name": M("No subtitles"), "help": M("A film with spoken audio and no subtitle track. Files whose audio is marked as having no linguistic content (zxx) aren’t flagged.")},
        {"id": "subtitles_on_zxx", "name": M("Subtitles on a film without speech"),
         "help": M("A file whose audio is marked zxx (no linguistic content) with subtitles that aren’t forced ones. Forced subtitles carry written content and belong; others are probably for another film.")},
        {"id": "duplicate_versions", "name": M("The same file twice on one item"),
         "help": M("Two versions on one Plex item that are the same file — the same running time and size, so not another bitrate of the film. A hard link or symlink counts as a second path to one file, and is said so.")},
        {"id": "duplicate_items", "name": M("The same file on different items"),
         "help": M("The same file (running time and size) under two Plex items: a copy or link that slipped in — a file in a subfolder and one in the root, say.")},
    ],
    "plex": [
        {"id": "versions_named_differently", "name": M("Versions named for different titles"),
         "help": M("Several files on one Plex item (versions) whose file names carry different titles or years: a version added to the wrong item, most likely.")},
        {"id": "versions_differ_in_length", "name": M("Versions of different lengths"),
         "help": M("Several files on one Plex item (versions) whose running times differ by more than a little — {leeway}% or {secs} seconds, whichever is more: two cuts of the same film can differ a little; two films do by far more.", leeway=LEEWAY_PCT, secs=LEEWAY_SECS)},
        {"id": "locked_empty", "name": M("Locked field with nothing in it"),
         "help": M("A field you edited (Plex locks it) that holds nothing — so Plex’s agents can’t fill it in either. Read from an item’s own page: items a Library Sync comparison has read, or opened here.")},
    ],
}
NO_SPEECH = ("zxx", "und")  # audio marked as having no linguistic content (and unknown: not flagged either way)
_TITLE = re.compile(r"^(?P<title>.+?)[ ._\-(\[]+(?P<year>(?:19|20)\d\d)(?![0-9p])")


def enabled(settings: dict) -> dict:
    """Each check's switch: on unless turned off."""
    chosen = (settings.get("library_health") or {}).get("checks") or {}
    return {c["id"]: chosen.get(c["id"], True) for cat in CHECKS.values() for c in cat}


def file_title_year(path: str) -> tuple[str, str]:
    """What a file is named for: ("the title", "2010"), from "The Title (2010) 1080p….mkv". Without a
    year, the whole name (less its extension), lower-cased and without punctuation."""
    base = os.path.splitext(os.path.basename(path))[0]
    m = _TITLE.match(base)
    title = m.group("title") if m else base
    title = re.sub(r"[\W_]+", " ", title).strip().lower()
    return title, (m.group("year") if m else "")


def file_checks(m: dict | None, on: dict) -> list[dict]:
    """The file checks on a file's layout as inspected (its tracks). [] without one, or for a file
    whose tracks aren't read (MP4: none listed)."""
    tracks = (m or {}).get("tracks") or []
    if not tracks or not any(t.get("type") == "video" for t in tracks):
        return []
    out = []
    audio = [t for t in tracks if t.get("type") == "audio"]
    subs = [t for t in tracks if t.get("type") == "subtitles"]
    langs = {str(t.get("language") or "").lower().split("-")[0] for t in audio}
    speech = bool(audio) and any(lang not in NO_SPEECH for lang in langs)
    if on.get("no_audio") and not audio:
        out.append({"id": "no_audio", "category": "file", "status": "warn", "text": M("No audio track.")})
    if on.get("no_subtitles") and speech and not subs:
        out.append({"id": "no_subtitles", "category": "file", "status": "warn",
                    "text": M("Spoken audio ({langs}) and no subtitle track.", langs=", ".join(sorted(language_label(l) for l in langs if l)))})
    if on.get("subtitles_on_zxx") and audio and not speech:
        odd = [t for t in subs if not t.get("forced")]
        if odd:
            out.append({"id": "subtitles_on_zxx", "category": "file", "status": "warn",
                        "text": M("The audio has no linguistic content (zxx), yet {n} subtitle tracks aren’t forced ones: {langs}.",
                                  one="The audio has no linguistic content (zxx), yet a subtitle track isn’t a forced one: {langs}.",
                                  n=len(odd), langs=", ".join(sorted({language_label(t.get("language") or "?") for t in odd})))})
    return out


def _mins(ms: int) -> str:
    h, m = divmod(round(ms / 60000), 60)
    return f"{h}:{m:02d}" if h else f"{m} min"


def plex_checks(item: dict, on: dict) -> list[dict]:
    """The Plex checks on one item: {"files": [paths], "locked": [field names], "fields": {name: value},
    "tags": {name: [values]}} (a Library Sync side, or an item from the library listing)."""
    out = []
    files = item.get("files") or []
    if on.get("versions_named_differently") and len(files) > 1:
        named = {file_title_year(f if isinstance(f, str) else f[0]) for f in files}
        titles, years = {t for t, _ in named}, {y for _, y in named if y}
        if len(titles) > 1 or len(years) > 1:
            out.append({"id": "versions_named_differently", "category": "plex", "status": "warn",
                        "text": M("{n} versions, named for different titles or years: {names}.", n=len(files),
                                  names="; ".join(sorted(f"{t} ({y})" if y else t for t, y in named)))})
    durations = [d for d in item.get("durations") or [] if d]
    if on.get("versions_differ_in_length") and len(files) > 1 and len(durations) > 1:
        lo, hi = min(durations), max(durations)
        if hi - lo > max(LEEWAY_SECS * 1000, hi * LEEWAY_PCT / 100):
            out.append({"id": "versions_differ_in_length", "category": "plex", "status": "warn",
                        "text": M("{n} versions of different lengths: {lengths}.", n=len(durations), lengths=", ".join(_mins(d) for d in sorted(durations)))})
    if on.get("locked_empty") and item.get("locked") is not None:
        empty = [f for f in item.get("locked") or [] if (f in EDIT_FIELDS and not str((item.get("fields") or {}).get(f, "")).strip())
                 or (f in TAG_FIELDS and not (item.get("tags") or {}).get(f))]
        if empty:
            out.append({"id": "locked_empty", "category": "plex", "status": "warn",
                        "text": M("Locked, with nothing in it: {fields}.", fields=", ".join(empty))})
    return out


def _same_file(a: dict, b: dict) -> bool:
    """Two files that are one film at one bitrate — the same running time (2 s) and size (1 MB)."""
    return bool(a.get("duration") and b.get("duration") and abs(a["duration"] - b["duration"]) <= 2000 and abs(a.get("size", 0) - b.get("size", 0)) <= 1024 * 1024)


def duplicate_checks(groups: dict, on: dict) -> dict:
    """The duplicate checks over a library: ``groups`` {item key: [{"path", "size", "duration", "ino"}]} →
    {item key: [results]}. Within an item: files that are the same file (linked, when they share an
    inode); across items: the same file under two."""
    out: dict[str, list] = {}
    if on.get("duplicate_versions"):
        for k, files in groups.items():
            for i, a in enumerate(files):
                for b in files[i + 1:]:
                    if _same_file(a, b):
                        linked = a.get("ino") and a.get("ino") == b.get("ino")
                        out.setdefault(k, []).append({"id": "duplicate_versions", "category": "file", "status": "warn",
                                                      "text": M("{a} and {b} are the same file ({how}).", a=os.path.basename(a["path"]), b=os.path.basename(b["path"]),
                                                                how=M("a link to one file") if linked else M("two copies"))})
    if on.get("duplicate_items"):
        seen: list[tuple] = []  # (key, file)
        for k, files in groups.items():
            for a in files:
                for ok, b in seen:
                    if ok != k and _same_file(a, b):
                        for me, mine, theirs in ((k, a, b), (ok, b, a)):
                            out.setdefault(me, []).append({"id": "duplicate_items", "category": "file", "status": "warn",
                                                           "text": M("{mine} is the same file as {theirs}, on another item ({how}).", mine=os.path.basename(mine["path"]),
                                                                     theirs=os.path.basename(theirs["path"]), how=M("a link to one file") if a.get("ino") and a.get("ino") == b.get("ino") else M("two copies"))})
                seen.append((k, a))
    return out


def library_health(app, key: str) -> dict:
    """A library's items through the checks: {"checks": CHECKS with each one's switch, "counts":
    {id: n}, "items": [{"label", "file", "results": [...]}], "checked", "unread" (files not inspected
    yet)}. The Plex "locked field" check needs an item's own page: run for the items a Library Sync
    comparison has read."""
    on = enabled(app.settings.get())
    files = [f for f in list(app.files) if str(f.library_key) == str(key)]
    counts: dict[str, int] = {}
    items: list[dict] = []
    unread = 0
    by_item: dict[str, list] = {}
    for f in files:
        by_item.setdefault(getattr(f, "rating_key", "") or f.path, []).append(f)
    sync_sides = {}
    rep = (getattr(app, "sync", None) and app.sync.report) or {}
    for row in ((rep.get("libraries") or {}).get(str(key)) or {}).get("items") or []:
        for sd in [row.get("src")] + (row.get("src_items") or []) + [row.get("dst")] + (row.get("dst_items") or []):
            if sd and sd.get("key") and sd.get("locked") is not None:
                sync_sides[sd["key"]] = sd
    dup_groups = {}
    for rk, group in by_item.items():
        rows = []
        for f in group:
            try:
                ino = os.lstat(app.map_path(f.path)).st_ino
            except OSError:
                ino = 0
            rows.append({"path": f.path, "size": getattr(f, "size", 0), "duration": getattr(f, "duration_ms", 0), "ino": ino})
        dup_groups[rk] = rows
    dups = duplicate_checks(dup_groups, on)
    for rk, group in by_item.items():
        results = list(dups.get(rk) or [])
        for f in group:
            path = app.map_path(f.path)
            try:
                m = app.file_maps.get(path, os.stat(path))
            except OSError:
                m = None
            if m is None:
                unread += 1
            for r in file_checks(m, on):
                results.append(dict(r, file=os.path.basename(path)))
        item = {"files": [f.path for f in group], "durations": [getattr(f, "duration_ms", 0) for f in group], "locked": None}
        sd = sync_sides.get(rk)
        if sd:
            item.update(locked=sd.get("locked") or [], fields=sd.get("fields") or {}, tags=sd.get("tags") or {})
        results += plex_checks(item, on)
        if results:
            for r in results:
                counts[r["id"]] = counts.get(r["id"], 0) + 1
            items.append({"label": getattr(group[0], "title", "") or os.path.basename(group[0].path), "poster": getattr(group[0], "poster", ""),
                          "files": [os.path.basename(f.path) for f in group], "results": results})
    items.sort(key=lambda it: it["label"].lower())
    return {"checks": {cat: [dict(c, on=on[c["id"]]) for c in cs] for cat, cs in CHECKS.items()}, "counts": counts,
            "items": items[:500], "total": len(items), "checked": len(by_item), "unread": unread, "pages_read": len(sync_sides)}

"""Prefetch: caching what's likely to play next, per library.

Terms: a file's *lead-in* is its first seconds plus its index — everything a player needs to
start it instantly. The *Lead-in Cache* holds lead-ins (Warm-up fills it on a schedule,
Prefetch for what plays next); the *Whole File Cache* holds whole files; the *Now Playing
Cache* is the file being played, read ahead of the viewer (playback.py).

Each library has two ladders. Each step includes everything before it, read in that order,
so the next episode always comes first:

* Lead-in Cache, once Now Playing Cache is done (lead-in length is set per library):
      off · next · rest · whole · next_group · all_groups · library
* Whole File Cache, near the end (at 80% or 90%):
      none · next · rest

What's needed soon is read straight away, into RAM; what's needed later goes to the SSD.
RAM keeps one-off reads for hours at most, so only the next episode (in full) and the rest
of the season's lead-ins are worth reading into it. Everything further ahead needs an L2ARC
(the grey SSD icon) and is background work: read at the L2ARC pacing, pausing like warm-up
does (SSD Cache → L2ARC Cache Settings), and not counted against the RAM limit — it's only
passing through on its way to the SSD. The next episode is never held up by it.

Left out because they never pay off: whole files of what was already watched (the earlier
episodes of a season, in full) — they'd only push lead-ins off the SSD.

The "group" is the season, album or book of what plays next: at the last episode of
season 1 the next episode is S02E01, so "rest of the season" means season 2.
Music has no "next album" step: albums aren't played in a set order.
"""

from __future__ import annotations

from . import cachemode
from .i18n import M

START_LEVELS = ("off", "next", "rest", "whole", "next_group", "all_groups", "library")
END_LEVELS = ("none", "next", "rest")
END_AT = ("80", "90")
DEFAULT_END_AT = "90"
FIELDS = {"start": START_LEVELS, "end": END_LEVELS, "end_at": END_AT}

# What "Default" means in each caching mode, and the choices that need an L2ARC.
START_DYN = cachemode.Dynamic(l2arc="library", ram="rest", l2arc_only=("whole", "next_group", "all_groups", "library"))
END_DYN = cachemode.Dynamic(l2arc="rest", ram="next", l2arc_only=("rest",))
cachemode.DYNAMIC["prefetch.start"] = START_DYN
cachemode.DYNAMIC["prefetch.end"] = END_DYN
# Kinds whose Default differs in a mode: on RAM only, music gets no whole files ahead — RAM is
# scarce, and tracks are short (Now Playing Cache reads them whole). With an L2ARC it's as for the rest.
KIND_DEFAULTS = {"music": {"end": {"ram": "none"}}}

NAMES = {
    "tv": {"off": M("Don’t prefetch"), "none": M("Nothing"), "next": M("Next episode"), "rest": M("Rest of the season"), "whole": M("Whole season"),
           "next_group": M("Next season"), "all_groups": M("All seasons of the show"), "library": M("Every show in the library")},
    "music": {"off": M("Don’t prefetch"), "none": M("Nothing"), "next": M("Next track"), "rest": M("Rest of the album"), "whole": M("Whole album"),
              "all_groups": M("All the artist’s albums"), "library": M("All music in the library")},
    "movies": {"off": M("Don’t prefetch"), "none": M("Nothing"), "next": M("Next movie in the collection"), "rest": M("Rest of the collection"),
               "whole": M("Whole collection"), "library": M("Every movie in the library")},
    "books": {"off": M("Don’t prefetch"), "none": M("Nothing"), "next": M("Next chapter"), "rest": M("Rest of the book"), "whole": M("Whole book"),
              "next_group": M("Next book"), "all_groups": M("All the author’s books"), "library": M("Every book in the library")},
}


def library_kind(lib: dict | None) -> str | None:
    """tv, music or books (audiobooks), or None for libraries Prefetch doesn't cover.
    Plex lists audiobooks as music; a library with "book" in its name is taken as audiobooks."""
    if not lib:
        return None
    if lib.get("kind") == "tv":
        return "tv"
    if lib.get("kind") == "movie":
        return "movies"
    if lib.get("kind") == "audio":
        return "books" if "book" in (lib.get("title") or "").lower() else "music"
    return None


def levels_for(kind: str) -> tuple:
    """Start levels a library offers (music has no next album; movies only have collections)."""
    return tuple(v for v in START_LEVELS if v in NAMES.get(kind, NAMES["tv"]))  # ("none" is for whole files)


def library_settings(cfg: dict, key: str) -> dict:
    """This library's choices, with "default" for anything not set. ("*" holds choices for every
    library, from before Prefetch's switch was retired, until it's shared out.)"""
    libs = cfg.get("libraries") or {}
    mine = libs.get(str(key)) or libs.get("*") or {}
    return {"start": mine.get("start", "default"), "end": mine.get("end", "default"),
            "end_at": mine.get("end_at", DEFAULT_END_AT)}


def resolve(app_or_mode, key: str, field: str, value: str, kind: str, lib_name: str = "") -> str:
    """The level to use now: Default per the caching mode, and L2ARC-only choices falling
    back to the RAM default (noted for Health) when there's no L2ARC."""
    dyn = START_DYN if field == "start" else END_DYN
    label = M("{library}: {setting}", library=lib_name or key,
              setting=M("Lead-in Cache prefetch") if field == "start" else M("Whole File Cache prefetch"))
    names = NAMES.get(kind, NAMES["tv"])
    own = KIND_DEFAULTS.get(kind, {}).get(field) or {}
    one = cachemode.Dynamic(own.get("l2arc", dyn.l2arc), own.get("ram", dyn.ram), dyn.l2arc_only, label, names)
    if isinstance(app_or_mode, str):
        level = cachemode.resolve(f"prefetch.{key}.{field}", value, app_or_mode, None, one)
    else:
        level = cachemode.resolve(f"prefetch.{key}.{field}", value, app_or_mode.cache_mode(), app_or_mode.fallbacks, one)
    if level not in NAMES.get(kind, NAMES["tv"]):
        level = "whole"  # not offered for this kind (music has no next album; movies no seasons)
    return level


def warm_kind(kind: str) -> str:
    """The warm-up kind (movie, tv, audio) of a Prefetch kind, for its seconds."""
    return {"movies": "movie", "tv": "tv"}.get(kind, "audio")


# ------------------------------------------------------------------ planning
def _ladder(steps: list[tuple[str, list]], level: str) -> list[tuple[str, dict]]:
    """(step, item) for every step up to and including ``level``, in order, each item once
    (under the first step that has it)."""
    out, seen = [], set()
    for name, items in steps:
        for it in items:
            k = it.get("rating_key") or (it["files"][0][0] if it.get("files") else id(it))
            if k not in seen:
                seen.add(k)
                out.append((name, it))
        if name == level:
            break
    return out


def tv_steps(episodes: list[dict], current_rk: str) -> list[tuple[str, list]] | None:
    """The ladder's steps for an episode: ``episodes`` is the show in Plex's order
    ({rating_key, season, episode, files, ...}). None if the episode isn't there."""
    i = next((n for n, e in enumerate(episodes) if e["rating_key"] == current_rk), None)
    if i is None:
        return None
    cur = episodes[i]
    if cur["season"] != 0:  # specials only when watching specials
        episodes = [e for e in episodes if e["season"] != 0 or e is cur]
        i = episodes.index(cur)
    after = episodes[i + 1:]
    nxt = after[:1]
    group = nxt[0]["season"] if nxt else cur["season"]  # the season of what plays next
    in_group = [e for e in episodes if e["season"] == group and e is not cur]
    rest = [e for e in after if e["season"] == group]
    seasons = sorted({e["season"] for e in episodes})
    later = [s for s in seasons if s > group]
    next_group = [e for e in episodes if later and e["season"] == later[0]]
    everything = [e for e in after] + [e for e in episodes[:i]]
    return [("next", nxt), ("rest", rest), ("whole", rest + in_group), ("next_group", next_group),
            ("all_groups", everything)]


def audio_steps(album: list[dict], current_rk: str, next_album: list[dict] | None,
                later_albums: list[list[dict]], kind: str) -> list[tuple[str, list]] | None:
    """The ladder's steps for a track or chapter. ``next_album`` is the book after this one
    (audiobooks only); ``later_albums`` every album or book by the artist or author."""
    i = next((n for n, t in enumerate(album) if t["rating_key"] == current_rk), None)
    if i is None:
        return None
    after = album[i + 1:]
    if not after and kind == "books" and next_album:
        # The last chapter: what plays next is the next book, so that's the book in focus.
        nxt, rest, whole = next_album[:1], next_album, next_album
        follow = []
        for a in later_albums:
            if a and a[0]["rating_key"] == next_album[0]["rating_key"]:
                j = later_albums.index(a)
                follow = later_albums[j + 1] if j + 1 < len(later_albums) else []
                break
    else:
        nxt, rest, whole = after[:1], after, after + album[:i]
        follow = next_album or []
    steps = [("next", nxt), ("rest", rest), ("whole", whole)]
    if kind == "books":
        steps.append(("next_group", follow))
    steps.append(("all_groups", [t for a in later_albums for t in a if t["rating_key"] != current_rk]))
    return steps


def movie_steps(collections: list[list[dict]], current_rk: str) -> list[tuple[str, list]]:
    """The ladder's steps for a movie: the movies around it in each of its Plex collections
    (in the collection's order). A movie in no collection has only the library step."""
    nxt, rest, before = [], [], []
    for col in collections:
        i = next((n for n, m in enumerate(col) if m["rating_key"] == current_rk), None)
        if i is None:
            continue
        nxt += col[i + 1:i + 2]
        rest += col[i + 1:]
        before += col[:i]
    return [("next", nxt), ("rest", rest), ("whole", rest + before)]


def plan_steps(steps: list[tuple[str, list]], level: str, library_items: list | None = None) -> list[tuple[str, dict]]:
    """(step, item) to read for ``level``, in order. The library step adds ``library_items``."""
    if level in ("off", "none") or steps is None:
        return []
    steps = list(steps)
    if level == "library":
        steps.append(("library", library_items or []))
    return _ladder(steps, level)


def plan(steps: list[tuple[str, list]], level: str, library_items: list | None = None) -> list:
    """Items to read for ``level``, in order."""
    return [it for _step, it in plan_steps(steps, level, library_items)]


def background(field: str, step: str) -> bool:
    """Is an item from this step L2ARC-only (paced and paused like warm-up)?"""
    return step in (START_DYN if field == "start" else END_DYN).l2arc_only

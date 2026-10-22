"""Library Health: checks on the content itself, apart from caching — two kinds.

* **File** checks look at a file's tracks, as inspected (filemap): no audio; a spoken-language film
  with no subtitles; subtitles on a film with no linguistic content (its audio marked "zxx") that
  aren't forced ones — forced subtitles carry written content, and belong.
* **Plex** checks look at how Plex has an item: versions (several files on one item) whose files
  are named for different titles or years — a version added to the wrong item, most likely; a field
  you locked that holds nothing.

* **Audio** libraries get checks of their own: a lossy track at a bitrate too low for its codec to
  sound right (LOW_KBPS: kbps per channel, by codec), and uncompressed audio (PCM, WAV, AIFF) that
  FLAC would hold losslessly in half the space.

Each check says which libraries it applies to ("applies": movie, tv, audio) — the page shows a
library only its own, and only those run — and can be turned off (settings "library_health.checks").
The Plex checks also run on both servers' items in a Library Sync comparison, marking the items whose
data looks bad before anything would be copied.
"""

from __future__ import annotations

import os
import re

from .i18n import M
from .languages import language_label, language_name
from .plex import EDIT_FIELDS, TAG_FIELDS

LEEWAY_PCT, LEEWAY_SECS = 3, 120  # how far versions' running times may differ before they're flagged
VIDEO, AUDIO, ALL = ("movie", "tv"), ("audio",), ("movie", "tv", "audio")
# a lossy codec's "fair" bitrate, in kbps per channel (stereo: twice) — where it stops falling audibly
# short: MP3 needs the most, Opus the least; a lossless codec never does. The quality level chosen
# scales every codec's floor alike (AUDIO_LEVELS), so the bar moves but the codecs stay comparable.
LOW_KBPS = {"mp3": 96, "aac": 64, "vorbis": 64, "opus": 48, "wma": 80, "ac3": 64, "eac3": 48, "dts": 128, "mp2": 96}
AUDIO_LEVELS = {"poor": 2 / 3, "fair": 1.0, "good": 4 / 3, "best": 5 / 3}  # (×LOW_KBPS; "poor" ships: only clearly poor files)
DEFAULT_AUDIO_LEVEL = "poor"
LOSSLESS = ("flac", "alac", "pcm", "truehd", "dts-hd", "mlp", "ape", "wavpack", "tta", "dsd")
UNCOMPRESSED = ("pcm",)  # (WAV and AIFF hold PCM)
# codecs that fall short of today's at any bitrate — the listening tests collected by Hydrogenaudio put
# AAC, Opus and Vorbis well ahead of these at equal bitrates, and MP3 can't go above 320 kbps at all
OUTDATED = {
    "mp3": M("MP3 — MPEG-1 Layer III, from 1993, the codec that made music files small enough to share. Its psychoacoustic model is a generation behind: sharp sounds (a drum hit, a plucked string) get a smear of noise just before them (pre-echo), most encoders cut everything above 16 kHz to save bits, and the format can’t go above 320 kbps however good the source. In the listening tests Hydrogenaudio has collected, AAC and Opus are preferred to MP3 at every bitrate; at the same quality they need about a third less space."),
    "mp2": M("MPEG-1 Layer II, from 1991: the codec of digital radio and DVD audio, built to be cheap to decode on the hardware of its day rather than to sound good per bit. It needs 256 kbps or more to be clean; AAC does the same at half that."),
    "wma": M("Windows Media Audio, from 1999: Microsoft’s answer to MP3. It never caught up with AAC in listening tests, few players outside Windows read it well, and Microsoft itself has moved on. Keeping it means keeping a format with a shrinking future."),
    "ac3": M("Dolby Digital AC-3, from 1991: a cinema delivery codec, designed for six channels of film sound on a disc, not for music. For a stereo file it spends bits poorly — below 192 kbps a pair it falls audibly short — and AAC or Opus give the same quality in half the space."),
    "ra": M("RealAudio: the streaming codec of the dial-up years, made to squeeze through a modem, poor by any standard now, and read by almost nothing current.")}
VIDEO_AUDIO_OK = ("ac3",)  # (a film's AC-3 track is the normal thing: not outdated there)
OUTDATED_LINK = "https://wiki.hydrogenaud.io/index.php?title=Lossy_codec_comparison"
CHECKS = {
    "file": [
        {"id": "no_audio", "name": M("No audio"), "applies": VIDEO, "help": M("A video file with no audio track at all.")},
        {"id": "no_subtitles", "name": M("No subtitles"), "applies": VIDEO, "help": M("A film with spoken audio and no subtitle track — any language, any format. Files whose audio is marked as having no linguistic content (zxx) aren’t flagged. The two options under it narrow what counts: a subtitle in one language, or one in a text format.")},
        {"id": "subtitles_language", "name": M("Must include the chosen language"), "default": False, "of": "no_subtitles", "applies": VIDEO,
         "help": M("Narrows No subtitles: a film needs a subtitle track in the language chosen beside this (any format, unless SRT/ASS is required too). Off unless you turn it on.")},
        {"id": "subtitles_text", "name": M("Must include SRT or ASS"), "default": False, "of": "no_subtitles", "applies": VIDEO,
         "help": M("Narrows No subtitles: a film needs a subtitle track in a text format — SRT, ASS or SSA — not only bitmap ones (PGS, VobSub). With a language chosen above as well, that language’s subtitle must be the text one. Text subtitles can be searched, styled and resized by every player, and are tiny. Off unless you turn it on.")},
        {"id": "subtitles_on_zxx", "name": M("Subtitles on a film without speech"), "applies": VIDEO,
         "help": M("A file whose audio is marked zxx (no linguistic content) yet holds subtitles that aren’t forced ones. One of three things is wrong: the audio’s language is tagged wrongly (it does have speech); the subtitles don’t belong to this file, or are empty; or they translate text shown on screen and should be marked forced. Forced subtitles carry written content and belong.")},
        {"id": "audio_low_bitrate", "name": M("Poor Audio Bitrate"), "applies": AUDIO,
         "help": M("A lossy track encoded below what its codec needs for the quality level chosen beside this, per channel (stereo: twice). Each codec has its own floor — MP3 needs the most, Opus the least — and the level scales them all alike: “Only clearly poor” (as it ships) flags MP3 under 128 kbps stereo, AAC or Vorbis under 85, Opus under 64; “Fair” MP3 192, AAC 128, Opus 96; “Good” and “Transparent” raise the bar further. Lossless codecs — FLAC, ALAC, PCM — are never flagged. The bitrate is the file’s, as Plex reads it; the codec from the file itself or its extension.")},
        {"id": "audio_outdated", "name": M("Outdated Codec"), "default": False, "applies": ALL, "link": OUTDATED_LINK,
         "help": M("An audio track in a codec that today’s codecs beat at every bitrate — MP3, MPEG Layer II, WMA, RealAudio, and in music libraries AC-3 (a film’s AC-3 track is the normal thing and isn’t flagged). Each finding says what the codec is and where it falls short. In the public listening tests collected by Hydrogenaudio, AAC, Opus and Vorbis come out clearly ahead of MP3 at equal bitrates. Off unless you turn it on: a well-made 320 kbps MP3 is fine for most ears, and the files are what they are unless you still have the source.")},
        {"id": "audio_uncompressed", "name": M("Uncompressed audio"), "applies": ALL,
         "help": M("PCM audio — WAV, AIFF, a raw PCM track, the LPCM track of a Blu-ray rip: the sound as it is, not compressed at all. A lossless codec holds the very same sound, bit for bit, in about half the space, and every player and Plex read them: FLAC (.flac, and inside an MKV), Apple Lossless (.m4a, ALAC) or WavPack (.wv). Re-encoding to one loses nothing. Lossy tracks (MP3, AAC, AC-3) aren’t flagged: a lossless copy of them would only be bigger.")},
        {"id": "duplicate_versions", "name": M("The same file twice on one item"), "applies": ALL,
         "help": M("Two versions on one Plex item that are the same file: the same size and running time, and the same bytes at both ends — not another bitrate of the film. A hard link or symlink counts as a second path to one file, and is said so.")},
        {"id": "duplicate_items", "name": M("The same file on different items"), "applies": ALL,
         "help": M("The same file under two Plex items — the same size and running time, and the same bytes at both ends: a copy or link that slipped in, a file in a subfolder and one in the root, say.")},
    ],
    "plex": [
        {"id": "versions_named_differently", "name": M("Versions named for different titles"), "applies": VIDEO,
         "help": M("Several files on one Plex item (versions) whose file names carry different titles or years: a version added to the wrong item, most likely.")},
        {"id": "versions_differ_in_length", "name": M("Different length versions"), "applies": ALL,
         "help": M("Several files on one Plex item (versions) whose running times differ by more than a little — {leeway}% or {secs} seconds, whichever is more: two cuts of the same film can differ a little; two films do by far more.", leeway=LEEWAY_PCT, secs=LEEWAY_SECS)},
        {"id": "locked_empty", "name": M("Empty field locked"), "applies": ALL,
         "help": M("A field you edited (Plex locks it) that holds nothing — so Plex’s agents can’t fill it in either. Read from an item’s own page: items a Library Sync comparison has read, or opened here.")},
    ],
}
NO_SPEECH = ("zxx", "und")  # audio marked as having no linguistic content (and unknown: not flagged either way)
TEXT_SUBS = ("s_text/utf8", "s_text/ascii", "s_text/ass", "s_text/ssa", "subrip", "srt", "ass", "ssa")  # subtitle codecs that are text
DEFAULT_LANGUAGE = "en"


def same_language(a: str | None, b: str | None) -> bool:
    """Whether two language codes name one language: "en", "eng" and "en-US" are all English."""
    x, y = str(a or "").strip().lower().split("-")[0], str(b or "").strip().lower().split("-")[0]
    if not x or not y:
        return False
    return x == y or (language_name(x) != "" and language_name(x) == language_name(y))
_TITLE = re.compile(r"^(?P<title>.+?)[ ._\-(\[]+(?P<year>(?:19|20)\d\d)(?![0-9p])")


def enabled(settings: dict) -> dict:
    """Each check's switch: on unless turned off."""
    chosen = (settings.get("library_health") or {}).get("checks") or {}
    return {c["id"]: chosen.get(c["id"], c.get("default", True)) for cat in CHECKS.values() for c in cat}


def defaults() -> dict:
    """Each check's switch as shipped (the page's ↺ buttons)."""
    return {c["id"]: c.get("default", True) for cat in CHECKS.values() for c in cat}


def for_kind(on: dict, kind: str | None) -> dict:
    """The switches with the checks that don't apply to a library of this kind turned off."""
    return {c["id"]: bool(on.get(c["id"])) and (not kind or kind in c.get("applies", ALL)) for cat in CHECKS.values() for c in cat}


def audio_codec(track: dict | None, path: str) -> str:
    """A short name for an audio track's codec — "mp3", "aac", "flac", "pcm"… — from the track as the
    file says it (A_MPEG/L3, A_FLAC, A_PCM/INT/LIT), else from the file's extension."""
    c = str((track or {}).get("codec") or "").upper()
    for key, name in (("MPEG/L3", "mp3"), ("MPEG/L2", "mp2"), ("AAC", "aac"), ("FLAC", "flac"), ("ALAC", "alac"), ("PCM", "pcm"), ("VORBIS", "vorbis"), ("OPUS", "opus"),
                      ("EAC3", "eac3"), ("AC3", "ac3"), ("TRUEHD", "truehd"), ("DTS/LOSSLESS", "dts-hd"), ("DTS", "dts"), ("MLP", "mlp"), ("WAVPACK", "wavpack"), ("TTA", "tta"), ("WMA", "wma")):
        if key in c:
            return name
    ext = os.path.splitext(path)[1].lower()
    return {".mp3": "mp3", ".m4a": "aac", ".m4b": "aac", ".aac": "aac", ".flac": "flac", ".ogg": "vorbis", ".oga": "vorbis", ".opus": "opus", ".wav": "pcm", ".aif": "pcm", ".aiff": "pcm",
            ".wma": "wma", ".ape": "ape", ".wv": "wavpack", ".dsf": "dsd", ".ac3": "ac3", ".dts": "dts"}.get(ext, "")


def audio_floor(codec: str, level: str = DEFAULT_AUDIO_LEVEL, channels: int = 2) -> int | None:
    """The bitrate (kbps) a lossy codec needs at a quality level for this many channels, to the nearest 8; None for a lossless one."""
    if codec not in LOW_KBPS:
        return None
    return int(round(LOW_KBPS[codec] * AUDIO_LEVELS.get(level, 1.0) * max(1, channels) / 8) * 8)


def audio_checks(m: dict | None, on: dict, path: str, kbps: int | None, channels: int = 2, level: str = DEFAULT_AUDIO_LEVEL, kind: str = "audio") -> list[dict]:
    """The checks on a file's audio: an audio library's file (its codec and bitrate — the file's, as Plex
    reads it), or a video's audio tracks (their codecs: a video's bitrate is mostly its picture, so
    the bitrate check is for audio libraries alone)."""
    tracks = (m or {}).get("tracks") or []
    audio = [t for t in tracks if t.get("type") == "audio"]
    out = []
    if kind == "audio":
        codec = audio_codec(audio[0] if audio else None, path)
        floor = audio_floor(codec, level, channels)
        if on.get("audio_low_bitrate") and floor and kbps and kbps < floor:
            out.append({"id": "audio_low_bitrate", "category": "file", "status": "warn",
                        "text": M("{codec} at {kbps} kbps: below the {floor} kbps this codec needs for {ch} channels at the chosen level.", codec=codec.upper(), kbps=kbps, floor=floor, ch=max(1, channels))})
        codecs = [codec] if codec else []
    else:
        codecs = sorted({audio_codec(t, "") for t in audio if audio_codec(t, "")})
    for codec in codecs:
        if on.get("audio_outdated") and codec in OUTDATED and not (kind != "audio" and codec in VIDEO_AUDIO_OK):
            out.append({"id": "audio_outdated", "category": "file", "status": "warn", "text": OUTDATED[codec]})
        if on.get("audio_uncompressed") and codec in UNCOMPRESSED:
            out.append({"id": "audio_uncompressed", "category": "file", "status": "warn",
                        "text": M("Uncompressed {codec}: FLAC (.flac, or inside the MKV) or Apple Lossless (.m4a) would hold the same sound, bit for bit, in about half the space.", codec=codec.upper())})
    return out


def file_title_year(path: str) -> tuple[str, str]:
    """What a file is named for: ("the title", "2010"), from "The Title (2010) 1080p….mkv". Without a
    year, the whole name (less its extension), lower-cased and without punctuation."""
    base = os.path.splitext(os.path.basename(path))[0]
    m = _TITLE.match(base)
    title = m.group("title") if m else base
    title = re.sub(r"[\W_]+", " ", title).strip().lower()
    return title, (m.group("year") if m else "")


def file_checks(m: dict | None, on: dict, lang: str = DEFAULT_LANGUAGE) -> list[dict]:
    """The file checks on a file's layout as inspected (its tracks). [] without one, or for a file
    whose tracks aren't read (MP4: none listed). ``lang``: the subtitle language wanted."""
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
    if on.get("no_subtitles") and speech:
        # any subtitle — narrowed, when asked, to one in the chosen language, in a text format, or both
        want_lang, want_text = bool(on.get("subtitles_language") and lang), bool(on.get("subtitles_text"))
        fit = [t for t in subs if (not want_lang or same_language(t.get("language"), lang)) and (not want_text or str(t.get("codec") or "").lower() in TEXT_SUBS)]
        if not fit:
            spoken = ", ".join(sorted(language_label(l) for l in langs if l))
            have = ", ".join(sorted({language_label(t.get("language") or "?") + (" " + str(t.get("codec") or "") if want_text else "") for t in subs}))
            what = (M("{lang} SRT/ASS", lang=language_name(lang) or lang) if want_lang and want_text else M("{lang}", lang=language_name(lang) or lang) if want_lang
                    else M("SRT/ASS") if want_text else "")
            out.append({"id": "no_subtitles", "category": "file", "status": "warn",
                        "text": M("Spoken audio ({langs}) and no subtitle track.", langs=spoken) if not subs
                        else M("Spoken audio ({langs}) and no {what} subtitle track (has: {have}).", langs=spoken, what=what, have=have)})
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


SAMPLE = 64 * 1024  # bytes read at each end of two files of one size, to tell the same bytes


def _ends(path: str, size: int) -> bytes | None:
    """The first and last SAMPLE bytes of a file (what two copies of one file share and two files of one size don't)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(SAMPLE)
            if size > SAMPLE:
                fh.seek(max(SAMPLE, size - SAMPLE))
                return head + fh.read(SAMPLE)
            return head
    except OSError:
        return None


def _same_file(a: dict, b: dict) -> bool:
    """Two entries that are one file: the same inode, or the very same size and running time (to the
    half second) and — when both can be read ("local" paths) — the same bytes at both ends. Over
    thousands of files two merely alike in size and length do turn up; the bytes settle it."""
    if a.get("ino") and a.get("ino") == b.get("ino") and a.get("dev", 0) == b.get("dev", 0):
        return True
    if not (a.get("duration") and b.get("duration")) or abs(a["duration"] - b["duration"]) > 500 or a.get("size", 0) != b.get("size", 0) or not a.get("size"):
        return False
    if a.get("local") and b.get("local"):
        x = _ends(a["local"], a["size"])
        return x is not None and x == _ends(b["local"], b["size"])
    return True


def duplicate_checks(groups: dict, on: dict) -> dict:
    """The duplicate checks over a library: ``groups`` {item key: [{"path", "size", "duration", "ino",
    "local"}]} → {item key: [results]}. Within an item: files that are the same file (linked, when they
    share an inode); across items: the same file under two. With "local" paths the bytes at both ends
    are compared, so two files merely alike in size and length aren't called the same."""
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


IGNORED = "health-ignored.json"  # {library key: {item key: [check ids ignored]}}


def ignored(app) -> dict:
    if getattr(app, "_lh_ignored", None) is None:
        app._lh_ignored = {k: v for k, v in (app._load_json(IGNORED) or {}).items() if isinstance(v, dict)}
    return app._lh_ignored


def set_ignored(app, key: str, item: str, ids: list | None) -> None:
    """An item's issues ignored (``ids``: the check ids to grey out, added to any already ignored) or
    brought back (None). Kept for good: it survives re-checks and restarts; a new kind of issue on the
    item still shows as a warning."""
    store = ignored(app)
    lib = store.setdefault(str(key), {})
    if ids is None:
        lib.pop(str(item), None)
    else:
        lib[str(item)] = sorted(set(lib.get(str(item)) or []) | set(ids))
    if not lib:
        store.pop(str(key), None)
    app._save_json(IGNORED, store)


def library_health(app, key: str) -> dict:
    """A library's items through the checks: {"checks": CHECKS with each one's switch, "counts":
    {id: n}, "items": [{"label", "file", "results": [...]}], "checked", "unread" (files not inspected
    yet)}. The Plex "locked field" check needs an item's own page: run for the items a Library Sync
    comparison has read."""
    lib = app.library_info(key) or {}
    kind = lib.get("kind")
    # every check that applies to this kind of library runs, switched on or not: the page keeps the findings
    # and shows those of the checks switched on, so a switch changes nothing but the view
    on = for_kind({c["id"]: True for cat in CHECKS.values() for c in cat}, kind)
    on_all = enabled(app.settings.get())  # (each switch as set, for the page)
    lh = app.settings.get().get("library_health") or {}
    lang = lh.get("subtitle_language") or DEFAULT_LANGUAGE
    level = lh.get("audio_quality") or DEFAULT_AUDIO_LEVEL
    languages: set = set()
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
            local = app.map_path(f.path)
            try:
                st = os.lstat(local)
                ino, dev, size = st.st_ino, st.st_dev, st.st_size
            except OSError:
                ino, dev, size = 0, 0, getattr(f, "size", 0)
            rows.append({"path": f.path, "local": local if ino else "", "size": size, "duration": getattr(f, "duration_ms", 0), "ino": ino, "dev": dev})
        dup_groups[rk] = rows
    dups = duplicate_checks(dup_groups, on)
    quiet = ignored(app).get(str(key)) or {}
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
            for t in (m or {}).get("tracks") or []:
                if t.get("type") in ("audio", "subtitles") and t.get("language"):
                    languages.add(str(t["language"]).lower().split("-")[0])
            for r in file_checks(m, on, lang):
                results.append(dict(r, file=os.path.basename(path)))
            for r in audio_checks(m, on, path, getattr(f, "kbps", 0) or 0, level=level, kind=kind or "movie"):
                results.append(dict(r, file=os.path.basename(path)))
        item = {"files": [f.path for f in group], "durations": [getattr(f, "duration_ms", 0) for f in group], "locked": None}
        sd = sync_sides.get(rk)
        if sd:
            item.update(locked=sd.get("locked") or [], fields=sd.get("fields") or {}, tags=sd.get("tags") or {})
        results += plex_checks(item, on)
        if results:
            muted = set(quiet.get(rk) or [])
            for r in results:
                r["ignored"] = r["id"] in muted  # (greyed out, not counted: ignored on this item)
                if not r["ignored"]:
                    counts[r["id"]] = counts.get(r["id"], 0) + 1
            items.append({"key": rk, "label": getattr(group[0], "title", "") or os.path.basename(group[0].path), "poster": getattr(group[0], "poster", ""),
                          "files": [os.path.basename(f.path) for f in group], "results": results, "live": sum(1 for r in results if not r["ignored"])})
    items.sort(key=lambda it: (it["live"] == 0, it["label"].lower()))  # (items with nothing live left at the bottom)
    langs = sorted(languages | {lang, DEFAULT_LANGUAGE}, key=lambda c: (language_name(c) or c).lower())
    return {"checks": {cat: [dict(c, on=on_all[c["id"]]) for c in cs if not kind or kind in c.get("applies", ALL)] for cat, cs in CHECKS.items()}, "counts": counts, "kind": kind,
            "items": items[:2000], "total": len(items), "checked": len(by_item), "unread": unread, "pages_read": len(sync_sides),
            "language": lang, "languages": [{"code": c, "name": language_name(c) or c} for c in langs if c not in NO_SPEECH],
            "audio_quality": level, "audio_levels": list(AUDIO_LEVELS)}

#!/usr/bin/env python3
"""Make the optimizer's test files (tests/fixtures/*.mkv) and the snapshot of what optimizing
them gives (tests/fixtures/expected.json).

    python3 dev/make_fixtures.py             # the files and the snapshot
    python3 dev/make_fixtures.py --snapshot  # only the snapshot, from the files there now

The files are small (a few seconds, tiny pictures) but have as many of the things real files have
as practical: several video, audio and subtitle formats, image subtitles (PGS and VobSub, one of
them without compression), styled subtitles with fonts, chapters, global and track tags, IETF
languages, track names and flags, HDR colour information, cover art (JPEG and PNG) and other
attachments, some small enough to sit before the video.

The snapshot is what the tests compare the optimized files with (tests/test_plexcache.py,
FixtureTests): the layout, the tracks and their properties, the attachments and our tags — not
the tools' version strings. When an update of MKVToolNix or ffmpeg changes any of it (they place
the index or attachments differently, or start optimizing something themselves), the tests fail:
look at what changed, and if it's fine, run this with --snapshot. Needs ffmpeg and MKVToolNix
(PLEXCACHE_TOOLS_PATH works here too).
"""

import json
import os
import random
import shutil
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "tests", "fixtures")

from plexcache.optimize import mkv, tools  # noqa: E402


# ------------------------------------------------------------------ PGS (Blu-ray subtitles)
def _seg(kind, pts, data):
    return b"PG" + struct.pack(">IIBH", pts, pts, kind, len(data)) + data


def _rle(width, height, pixels):
    out = bytearray()
    for y in range(height):
        row = pixels[y * width:(y + 1) * width]
        x = 0
        while x < width:
            c, n = row[x], 1
            while x + n < width and row[x + n] == c and n < 16383:
                n += 1
            if c == 0:
                out += bytes([0, n]) if n < 64 else bytes([0, 0x40 | (n >> 8), n & 0xFF])
            elif n <= 3:
                out += bytes([c]) * n
            else:
                out += bytes([0, 0x80 | n, c]) if n < 64 else bytes([0, 0xC0 | (n >> 8), n & 0xFF, c])
            x += n
        out += b"\0\0"
    return bytes(out)


def pgs(path, events=4):
    """A .sup with ``events`` subtitles: text-like bitmaps, shown and cleared."""
    rng = random.Random(1)
    W, H, w, h = 160, 90, 120, 16
    out = bytearray()
    for i in range(1, events + 1):
        start, end = int(i * 0.6 * 90000), int((i * 0.6 + 0.4) * 90000)
        px = [0] * (w * h)
        for x in range(4, w - 4):
            for y in range(3, h - 3):
                if rng.random() < 0.35:
                    px[y * w + x] = 1 if y in (3, h - 4) else 2
        data = _rle(w, h, px)
        x0, y0 = (W - w) // 2, H - h - 6
        pcs = struct.pack(">HHBHBBBB", W, H, 0x10, i * 2, 0x80, 0, 0, 1) + struct.pack(">HBBHH", 0, 0, 0, x0, y0)
        wds = struct.pack(">BBHHHH", 1, 0, x0, y0, w, h)
        pds = bytes([0, 0]) + bytes([0, 16, 128, 128, 0, 1, 16, 128, 128, 255, 2, 235, 128, 128, 255])
        ods = struct.pack(">HBB", 0, 0, 0xC0) + (len(data) + 4).to_bytes(3, "big") + struct.pack(">HH", w, h) + data
        out += _seg(0x16, start, pcs) + _seg(0x17, start, wds) + _seg(0x14, start, pds) + _seg(0x15, start, ods) + _seg(0x80, start, b"")
        clear = struct.pack(">HHBHBBBB", W, H, 0x10, i * 2 + 1, 0x00, 0, 0, 0)
        out += _seg(0x16, end, clear) + _seg(0x17, end, wds) + _seg(0x80, end, b"")
    with open(path, "wb") as fh:
        fh.write(out)


# ------------------------------------------------------------------ the files
def ff(*args):
    tools.run("ffmpeg", "-v", "error", "-y", *args)


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


SRT = "1\n00:00:00,500 --> 00:00:01,500\nHello\n\n2\n00:00:02,000 --> 00:00:02,800\nAgain\n"
ASS = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 160\nPlayResY: 90\n\n[V4+ Styles]\n"
       "Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Italic, Alignment, MarginV\n"
       "Style: Default,Fixture Sans,12,&H00FFFFFF,0,0,2,6\n\n[Events]\n"
       "Format: Layer, Start, End, Style, Text\n"
       "Dialogue: 0,0:00:00.50,0:00:01.50,Default,{\\i1}Styled{\\i0} line\n"
       "Dialogue: 0,0:00:02.00,0:00:02.80,Default,Second line\n")
CHAPTERS = ('<?xml version="1.0"?>\n<Chapters><EditionEntry>'
            '<ChapterAtom><ChapterTimeStart>00:00:00.000</ChapterTimeStart><ChapterDisplay><ChapterString>Opening</ChapterString>'
            '<ChapterLanguage>eng</ChapterLanguage></ChapterDisplay></ChapterAtom>'
            '<ChapterAtom><ChapterTimeStart>00:00:01.500</ChapterTimeStart><ChapterDisplay><ChapterString>Middle</ChapterString>'
            '<ChapterLanguage>eng</ChapterLanguage></ChapterDisplay></ChapterAtom></EditionEntry></Chapters>\n')
GLOBAL_TAGS = ('<?xml version="1.0"?>\n<Tags><Tag><Targets><TargetTypeValue>50</TargetTypeValue></Targets>'
               '<Simple><Name>TITLE</Name><String>Fixture Film</String></Simple>'
               '<Simple><Name>DATE_RELEASED</Name><String>2026</String></Simple>'
               '<Simple><Name>COMMENT</Name><String>made by dev/make_fixtures.py</String></Simple></Tag></Tags>\n')
TRACK_TAGS = ('<?xml version="1.0"?>\n<Tags><Tag><Targets/><Simple><Name>FIXTURE_TRACK_NOTE</Name>'
              '<String>fixture</String></Simple></Tag></Tags>\n')


def build(work):
    """The fixtures, into tests/fixtures. Returns their names."""
    j = lambda n: os.path.join(work, n)
    os.makedirs(OUT, exist_ok=True)
    src = "testsrc=size=160x90:rate=24"
    ff("-f", "lavfi", "-i", src, "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", j("h264.mkv"))
    ff("-f", "lavfi", "-i", src, "-t", "3", "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none",
       "-pix_fmt", "yuv420p10le", j("hevc.mkv"))
    sine = lambda freq, ch: ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration=3,aformat=channel_layouts={ch}"]
    ff(*sine(440, "stereo"), "-c:a", "aac", "-b:a", "48k", j("aac.mka"))
    ff(*sine(220, "5.1"), "-c:a", "ac3", "-b:a", "96k", j("ac3.mka"))
    ff(*sine(330, "stereo"), "-c:a", "libopus", "-b:a", "24k", j("opus.mka"))
    ff(*sine(550, "mono"), "-c:a", "flac", j("flac.mka"))
    write(j("subs.srt"), SRT)
    write(j("subs.ass"), ASS)
    write(j("chapters.xml"), CHAPTERS)
    write(j("global.xml"), GLOBAL_TAGS)
    write(j("track.xml"), TRACK_TAGS)
    pgs(j("subs.sup"))
    ff("-i", j("subs.sup"), "-c:s", "dvdsub", j("vobsub.mkv"))  # VobSub from the same bitmaps
    ff("-f", "lavfi", "-i", "testsrc2=size=60x90", "-frames:v", "1", "-q:v", "4", j("cover.jpg"))
    ff("-f", "lavfi", "-i", "testsrc2=size=90x60", "-frames:v", "1", "-compression_level", "0", j("cover_land.png"))
    with open(j("Fixture Sans.ttf"), "wb") as fh:
        fh.write(b"\x00\x01\x00\x00" + b"fixture glyph outlines " * 120)  # stands in for a font
    with open(j("Small Font.otf"), "wb") as fh:
        fh.write(b"OTTO" + b"tiny " * 40)  # small enough to sit before the video
    write(j("notes.nfo"), "Fixture notes\n" * 30)
    att = lambda name, mime, desc=None: (["--attachment-description", desc] if desc else []) + [
        "--attachment-mime-type", mime, "--attach-file", j(name)]

    # 1. Feature-rich: H.264 with HDR colour, four audio formats, SRT/ASS/PGS/VobSub, chapters, tags, cover art
    tools.run("mkvmerge", "-q", "-o", os.path.join(OUT, "feature-rich.mkv"),
              "--title", "Fixture Film", "--chapters", j("chapters.xml"), "--global-tags", j("global.xml"),
              "--colour-matrix-coefficients", "0:9", "--colour-transfer-characteristics", "0:16", "--colour-primaries", "0:9",
              "--max-content-light", "0:1000", "--max-frame-light", "0:400", "--track-name", "0:Main video",
              "--tags", "0:" + j("track.xml"), j("h264.mkv"),
              "--language", "0:en", "--default-track-flag", "0:yes", j("aac.mka"),
              "--language", "0:fr-CA", "--track-name", "0:Français 5.1", "--default-track-flag", "0:no", j("ac3.mka"),
              "--language", "0:en", "--track-name", "0:Commentary", "--commentary-flag", "0:yes", j("opus.mka"),
              "--language", "0:de", j("flac.mka"),
              "--language", "0:en", "--forced-display-flag", "0:yes", "--track-name", "0:Signs", j("subs.srt"),
              "--language", "0:en", j("subs.ass"),
              "--language", "0:en", "--compression", "0:none", j("subs.sup"),        # PGS without zlib: the optimizer adds it
              "--language", "0:es", j("vobsub.mkv"),
              *att("cover.jpg", "image/jpeg", "Poster"), *att("Fixture Sans.ttf", "font/ttf"),
              *att("notes.nfo", "text/plain"), *att("cover_land.png", "image/png"))
    # 2. Anime-like: HEVC 10-bit, styled subtitles, fonts — one small enough to sit before the video
    tools.run("mkvmerge", "-q", "-o", os.path.join(OUT, "anime.mkv"), "--chapters", j("chapters.xml"),
              j("hevc.mkv"), "--language", "0:ja", j("opus.mka"), "--language", "0:en", j("subs.ass"),
              *att("Small Font.otf", "font/otf"), *att("Fixture Sans.ttf", "font/ttf"))
    # 3. Plain: video and audio only — no attachments, no chapters, no tags of its own
    tools.run("mkvmerge", "-q", "-o", os.path.join(OUT, "plain.mkv"), "--disable-track-statistics-tags",
              j("h264.mkv"), j("aac.mka"))
    return ["feature-rich.mkv", "anime.mkv", "plain.mkv"]


# ------------------------------------------------------------------ the snapshot
VOLATILE_TAGS = ("_STATISTICS_", "ENCODER", "DURATION", "BPS", "NUMBER_OF_")  # tools' versions and statistics


def summary(path: str) -> dict:
    """What the tests compare: the layout, the tracks, attachments, chapters and tags — nothing
    that names a tool's version."""
    info = json.loads(tools.run("mkvmerge", "-J", path))
    lay = mkv.read_layout(path)
    first = lay["first_cluster"]
    tail = [name for name, at in sorted(lay["seek"].items(), key=lambda kv: kv[1]) if at > first]
    keep = ("codec_id", "language", "language_ietf", "track_name", "default_track", "forced_track", "flag_commentary",
            "content_encoding_algorithms", "color_matrix_coefficients", "color_transfer_characteristics",
            "color_primaries", "max_content_light", "max_frame_light", "audio_channels", "pixel_dimensions")
    tracks = [{"type": t["type"], **{k: t["properties"].get(k) for k in keep if t["properties"].get(k) is not None}}
              for t in info.get("tracks") or []]
    tags = json.loads(tools.run("mkvmerge", "-J", path)).get("global_tags") or []
    names = sorted({n for n in tools.run("mkvextract", path, "tags", "-").split("<Name>")[1:]
                    for n in [n.split("<")[0]] if not n.startswith(VOLATILE_TAGS)}) if tags else []
    return {
        "front": [name for name, _, _ in lay["front"] if name != "Void"],
        "tail": tail,
        "tracks": tracks,
        "attachments": [[a.get("file_name"), a.get("content_type"), a.get("description") or ""] for a in info.get("attachments") or []],
        "chapters": sum(c.get("num_entries", 0) for c in info.get("chapters") or []),
        "title": (info.get("container") or {}).get("properties", {}).get("title"),
        "tags": names,
        "ours": lay.get("ours") or {},
    }


def snapshot(names):
    from plexcache.optimize.optimizer import optimize_file
    out = {}
    with tempfile.TemporaryDirectory() as d:
        for n in names:
            for images in ("compress", "remove"):
                p = os.path.join(d, f"{images}-{n}")
                shutil.copy(os.path.join(OUT, n), p)
                optimize_file(p, images)
                out[f"{n} {images}"] = summary(p)
    with open(os.path.join(OUT, "expected.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    print(f"snapshot of {len(out)} optimized files in tests/fixtures/expected.json")


if __name__ == "__main__":
    names = ["feature-rich.mkv", "anime.mkv", "plain.mkv"]
    if "--snapshot" not in sys.argv:
        with tempfile.TemporaryDirectory() as work:
            names = build(work)
        for n in names:
            print(f"tests/fixtures/{n}: {os.path.getsize(os.path.join(OUT, n)):,} bytes")
    snapshot(names)

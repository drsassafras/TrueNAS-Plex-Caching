"""Lossless compression of image attachments (cover art) and Plex posters.

* PNG: ``oxipng -o 4 --strip safe`` — tries PNG's filters and zlib settings and reduces the color
  type and bit depth where no pixel changes; "safe" removes only chunks that don't change how the
  image looks (text, timestamps), and keeps color profiles, gamma and transparency.
* JPEG: ``jpegtran -copy all -optimize`` — rewrites the Huffman tables for this image. The DCT
  coefficients (the picture) aren't touched, and every marker (EXIF orientation, color profile)
  is kept. It stays baseline: progressive JPEG would save a little more, but some TVs and DLNA
  players don't show it.

Each result is checked: decoded by ffmpeg, its pixels must be exactly the original's. Anything
that isn't smaller, can't be checked, or is another format (WebP, GIF, animated PNG) is kept as
it was.
"""

import os

from .tools import ToolError, find, run

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
EXT = {"png": ".png", "jpeg": ".jpg"}
MIME = {"png": "image/png", "jpeg": "image/jpeg"}


def kind(path: str) -> str | None:
    """"png", "jpeg", or None, from the file's first bytes (names and MIME types can be wrong)."""
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(PNG_MAGIC):
        return "png"
    if head.startswith(JPEG_MAGIC):
        return "jpeg"
    return None


def _animated_png(path: str) -> bool:
    """An APNG has an acTL chunk before its first IDAT; oxipng would keep only the first frame."""
    with open(path, "rb") as fh:
        data = fh.read(1024 * 1024)
    at = data.find(b"IDAT")
    return b"acTL" in (data[:at] if at >= 0 else data)


def pixels(path: str) -> str:
    """ffmpeg's hash of the decoded picture, in one wide pixel format, so a PNG whose color type
    or bit depth oxipng reduced still compares equal when no pixel changed."""
    return run("ffmpeg", "-v", "error", "-i", path, "-frames:v", "1", "-pix_fmt", "rgba64le",
               "-f", "hash", "-hash", "sha256", "-")


def available() -> dict:
    return {"png": bool(find("oxipng")), "jpeg": bool(find("jpegtran"))}


def compress(path: str, out: str) -> dict:
    """Write a losslessly smaller copy of ``path`` to ``out``. {"path": the file to use (``out``,
    or ``path`` when nothing was gained), "kind", "before", "after", "why" when kept}."""
    k = kind(path)
    before = os.path.getsize(path)
    res = {"path": path, "kind": k, "before": before, "after": before}
    if k is None:
        return dict(res, why="not PNG or JPEG")
    if k == "png" and _animated_png(path):
        return dict(res, why="animated PNG")
    tool = "oxipng" if k == "png" else "jpegtran"
    if not find(tool):
        return dict(res, why=f"{tool} isn't installed")
    try:
        if k == "png":
            run("oxipng", "-q", "-o", "4", "--strip", "safe", "--out", out, path)
        else:
            run("jpegtran", "-copy", "all", "-optimize", "-outfile", out, path)
    except ToolError as e:
        return dict(res, why=str(e))
    if not os.path.exists(out):
        return dict(res, why=f"{tool} wrote nothing")
    after = os.path.getsize(out)
    if after >= before:
        os.unlink(out)
        return dict(res, why="already as small as it gets")
    try:
        same = pixels(out) == pixels(path)
    except ToolError as e:
        os.unlink(out)
        return dict(res, why=f"couldn't be checked: {e}")
    if not same:
        os.unlink(out)
        return dict(res, why="the pixels changed")
    return dict(res, path=out, after=after)

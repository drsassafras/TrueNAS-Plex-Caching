"""Optimize MKV files from the command line (``python -m plexcache.optimize FILE...``), or run
the optimizer service (``python -m plexcache.optimize serve``)."""

import argparse
import sys

from . import mkv
from .optimizer import Skipped, optimize_file
from .tools import ToolError, missing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m plexcache.optimize", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("files", nargs="+", help="MKV files, or 'serve' to run the optimizer service")
    ap.add_argument("--check", action="store_true", help="only say whether each file is optimized")
    ap.add_argument("--images", choices=("compress", "remove", "plex", "keep"), default="compress",
                    help="cover art: compress losslessly (default), remove, replace with --cover (plex), or keep as is")
    ap.add_argument("--cover", help="the poster to embed with --images plex (PNG or JPEG)")
    ap.add_argument("--drop-images", action="store_true", help="the same as --images remove")
    ap.add_argument("--force", action="store_true", help="rewrite even files already laid out right (new record size/compression)")
    ap.add_argument("--no-verify", action="store_true", help="skip comparing the streams before swapping")
    ap.add_argument("--allow-owner-change", action="store_true", help="swap even if the owner can't be kept")
    a = ap.parse_args(argv)
    if a.files == ["serve"]:
        from .server import serve
        return serve()
    images = "remove" if a.drop_images else a.images
    keep = images != "remove"
    cover = None
    if images == "plex":
        if not a.cover:
            ap.error("--images plex needs --cover FILE")
        with open(a.cover, "rb") as fh:
            cover = fh.read()
    if a.check:
        for f in a.files:
            c = mkv.check(f, keep)
            print(f"{f}: " + ("not Matroska" if not c.get("applies") else "optimized" if c["optimized"]
                              else "needs: " + ", ".join(c["problems"])))
        return 0
    if missing():
        print("missing tools: " + ", ".join(missing()), file=sys.stderr)
        return 2
    failed = 0
    for f in a.files:
        try:
            r = optimize_file(f, images, cover=cover, verify=not a.no_verify, allow_owner_change=a.allow_owner_change, force=a.force,
                              log=lambda t, f=f: print(f"{f}: {t}"))
            print(f"{f}: {r['result']}" + (f" ({r['before']:,} → {r['after']:,} bytes, cover art {r['images_saved']:,} bytes smaller, "
                                           f"{r['seconds']} s)" if r["result"] == "optimized" else ""))
        except Skipped as e:
            print(f"{f}: skipped — {e}")
        except (ToolError, OSError, mkv.NotMatroska) as e:
            failed += 1
            print(f"{f}: FAILED — {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

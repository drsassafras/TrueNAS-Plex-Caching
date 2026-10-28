#!/usr/bin/env python3
"""Codec names as MKVToolNix shows them ("HEVC/H.265/MPEG-H" for V_MPEGH/ISO/HEVC), for the Library
Health page: read from MKVToolNix's own list (src/common/codec.cpp, with the codec IDs some
entries take from src/common/codec.h) and written to plexcache/codec_names.py.

The image build runs it with --installed: the names of the MKVToolNix release that's in the image,
so they change with it, build by build. Offline, the build keeps the committed file. By hand:

    python3 dev/codecs.py --installed       # the release `mkvmerge --version` says (the image build)
    python3 dev/codecs.py 102.0             # a release
    python3 dev/codecs.py                   # MKVToolNix's main branch
    python3 dev/codecs.py path/to/codec.cpp path/to/codec.h

It says what changed; the tests check every pattern still works in Python.
"""

import os
import re
import sys
import urllib.request

REPO = "https://codeberg.org/mbunkus/mkvtoolnix/raw/"
SOURCE, HEADER = "src/common/codec.cpp", "src/common/codec.h"
CONST = re.compile(r'constexpr\s+auto\s+(MKV_\w+)\s*=\s*"((?:[^"\\]|\\.)*)"')
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plexcache", "codec_names.py")
# s_codecs.emplace_back("Name", type_e::…, track_…, "pattern" or MKV_CONSTANT[, …])
ENTRY = re.compile(r'emplace_back\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*type_e::\w+\s*,\s*track_\w+\s*,\s*(?:"((?:[^"\\]|\\.)*)"|(MKV_\w+))')


def unescape(c: str) -> str:
    return re.sub(r"\\(.)", r"\1", c)  # a C string's \\ is one \ in the pattern


def parse(text: str, header: str = "") -> list[tuple[str, str]]:
    """[(name, pattern)] in MKVToolNix's order (it uses the first that matches). An entry naming a
    constant (MKV_S_HDMV_PGS) matches that codec ID exactly; entries without either (matched by
    number only) are left out."""
    consts = {k: unescape(v) for k, v in CONST.findall(header)}
    out = []
    for n, p, const in ENTRY.findall(text):
        pattern = unescape(p) if p else re.escape(consts[const]) if const in consts else ""
        if pattern:
            out.append((unescape(n), pattern))
    return out


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args and args[0] == "--installed":
        import subprocess
        try:
            out = subprocess.run(["mkvmerge", "--version"], capture_output=True, text=True, timeout=30).stdout
        except OSError:
            out = ""
        m = re.match(r"mkvmerge v([0-9.]+)", out)
        if not m:
            print("mkvmerge isn't installed: codec names left as they are.")
            return 0
        args = [m.group(1)]
    files = len(args) == 2 and os.path.exists(args[0])
    ref = "branch/main/" if not args else f"tag/release-{args[0]}/"

    def read(path):
        if files:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        with urllib.request.urlopen(REPO + ref + path, timeout=30) as r:
            return r.read().decode("utf-8")
    try:
        text, header = (read(args[0]), read(args[1])) if files else (read(SOURCE), read(HEADER))
    except OSError as e:  # offline (or no such release): the committed names stay
        print(f"Couldn't read MKVToolNix's codec list ({e}): codec names left as they are.")
        return 0
    where = args[0] if files else REPO + ref + SOURCE
    entries = parse(text, header)
    if len(entries) < 20:
        print(f"Only {len(entries)} codecs found in {where}: its layout may have changed. Nothing written.")
        return 1
    for name, pattern in entries:
        re.compile(f"(?:{pattern})", re.I)  # (fails loudly if MKVToolNix uses something Python can't read)
    old = {}
    if os.path.exists(OUT):
        ns: dict = {}
        exec(open(OUT, encoding="utf-8").read(), ns)
        old = dict(ns.get("CODECS", []))
    new = dict(entries)
    for name in sorted(set(new) - set(old)):
        print("added:  ", name)
    for name in sorted(set(old) - set(new)):
        print("removed:", name)
    for name in sorted(n for n in set(new) & set(old) if new[n] != old[n]):
        print("changed:", name)
    lines = ['"""Codec names as MKVToolNix shows them, by codec ID. Made by dev/codecs.py from', f"{where}", '— run it again to bring them up to date. Each pattern matches a whole codec ID (or FourCC),',
             'ignoring case; the first that matches wins."""', "", "CODECS = ["]
    lines += [f"    ({name!r}, {pattern!r})," for name, pattern in entries]
    lines += ["]", ""]
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"{len(entries)} codecs written to {os.path.relpath(OUT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

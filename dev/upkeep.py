"""What the app depends on, kept current: each dependency's version as the project pins or ships it, against the newest
release upstream — looked up once a day, by the suite when it runs by hand (``test_dependencies_are_current``; GitHub
doesn't run it) and by this script.

    python3 dev/upkeep.py            # the report; a pin a minor or patch release behind is moved to it
    python3 dev/upkeep.py --fresh    # asks upstream again rather than using the day's answers
    python3 dev/upkeep.py --report   # looks, changes nothing

What it does with each: a dependency behind by a patch or minor release is moved to it where the project holds the pin
(``bump``: the file edited, said in the report); one behind by a major release — or one with no build upstream yet — is a prompt: the test fails naming it until it's moved or, meant to stay, held (HOLD below). The GitHub
Actions are Dependabot's (.github/dependabot.yml): not looked at here.

The dependencies: the Python the image runs (PYTHON_TAG in the Dockerfile; a new minor is a prompt, once released: moving
is a line there and one in build.yml, after dev/future.py is clean on it), the Debian that tag's image is built on (a new
Debian is a prompt), the tools the image installs — MKVToolNix from its own repository, ffmpeg built from its release,
oxipng and libjpeg-turbo from theirs, each an ARG in the Dockerfile moved here once upstream has the release (every
MKVToolNix release is taken, since each carries fixes) — and pyright (its pin in build.yml; patch and minor releases taken).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(ROOT, "Dockerfile")
BUILD = os.path.join(ROOT, ".github", "workflows", "build.yml")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "plex-cache", "upkeep.json")
FUTURE = os.path.join(os.path.dirname(CACHE), "future.json")  # what dev/future.py last chose to run on (its day's choices)
A_DAY = 24 * 3600
TIMEOUT = 20

# A dependency meant to stay where it is, by choice: its name -> the newest version the choice covers (it prompts again
# once upstream passes that). Empty: nothing is held.
HOLD: dict[str, str] = {}


class Offline(Exception):
    """Upstream couldn't be reached (and there's no answer from the last day to go by)."""


def _get(url: str, accept: str = "*/*") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "plex-cache-upkeep", "Accept": accept})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _json(url: str):
    return json.loads(_get(url, "application/json"))


def parse(version: str) -> tuple[int, ...]:
    """A version's numbers: "v3.2.1", "99.0-r1", "3.15-slim-trixie" → (3, 2, 1), (99, 0), (3, 15)."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", version.strip())
    if not m:
        raise ValueError(f"not a version: {version!r}")
    return tuple(int(x) for x in m.group(1).split("."))


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _pin(path: str, pattern: str) -> str:
    m = re.search(pattern, _read(path))
    if not m:
        raise ValueError(f"no {pattern!r} in {os.path.relpath(path, ROOT)}")
    return m.group(1)


def _repin(path: str, pattern: str, new: str) -> str:
    """The pin ``pattern`` (its group 1) in ``path`` set to ``new``; what changed."""
    text = _read(path)
    m = re.search(pattern, text)
    if not m:
        raise ValueError(f"no {pattern!r} in {os.path.relpath(path, ROOT)}")
    _write(path, text[:m.start(1)] + new + text[m.end(1):])
    return f"{os.path.relpath(path, ROOT)}: {m.group(1)} → {new}"


# ---------------------------------------------------------------- upstream
def python_cycles() -> list[dict]:
    """Python's release cycles, newest first: [{"cycle": "3.15", "latest": "3.15.2", ...}] (released ones only)."""
    return _json("https://endoflife.date/api/python.json")


def image_python() -> str:
    """The Python the image is built on: the major.minor of PYTHON_TAG."""
    return ".".join(str(x) for x in parse(_pin(DOCKERFILE, r"ARG PYTHON_TAG=(\S+)"))[:2])


DEBIAN = {"bullseye": "11", "bookworm": "12", "trixie": "13", "forky": "14", "duke": "15"}  # (codenames as numbers, to compare)


def debian_of_tag() -> str:
    """The Debian the image is built on, from PYTHON_TAG (3.15-slim-trixie → trixie)."""
    m = re.search(r"-(\w+)$", _pin(DOCKERFILE, r"ARG PYTHON_TAG=(\S+)"))
    if not m or m.group(1) not in DEBIAN:
        raise ValueError("PYTHON_TAG doesn't end in a Debian codename")
    return m.group(1)


def debian_newest() -> str:
    """The Debian docker-library builds the image's Python on (the first variant it lists, its newest Debian)."""
    versions = _json("https://raw.githubusercontent.com/docker-library/python/master/versions.json")
    first = next(iter((versions.get(image_python()) or {}).get("variants") or []), "")
    if first not in DEBIAN:
        raise ValueError(f"docker-library's first variant for Python {image_python()} isn't a Debian: {first!r}")
    return first


def _debian_number(name: str) -> str:
    return DEBIAN[name]


def _arg(name: str) -> Callable[[], str]:
    """A pin in the Dockerfile: ARG <name>=<version>."""
    return lambda: _pin(DOCKERFILE, rf"ARG {name}=(\S+)")


def _exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "plex-cache-upkeep"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT):
            return True
    except urllib.error.HTTPError as e:
        e.close()
        return False


def _bump_arg(name: str, built: Callable[[str], str]) -> Callable[[str], str]:
    """Moves the Dockerfile's ARG <name> to a version — once upstream has a build of it at ``built(version)``, the URL
    the Dockerfile fetches (the release may be announced before its build is up)."""
    def bump(new: str) -> str:
        if not _exists(built(new)):
            raise ValueError(f"{new} has no build yet at {built(new)}")
        return _repin(DOCKERFILE, rf"ARG {name}=(\S+)", new)
    return bump


def _mkvtoolnix_built(version: str) -> str:
    return f"https://mkvtoolnix.download/debian/dists/{debian_of_tag()}/main/binary-amd64/Packages"


def _mkvtoolnix_bump(new: str) -> str:
    packages = _get(_mkvtoolnix_built(new))
    if not re.search(rf"^Version: {re.escape(new)}-", packages, re.M):
        raise ValueError(f"{new} isn't in mkvtoolnix.download's {debian_of_tag()} repository yet")
    return _repin(DOCKERFILE, r"ARG MKVTOOLNIX=(\S+)", new)


def github_release(repo: str) -> Callable[[], str]:
    return lambda: _json(f"https://api.github.com/repos/{repo}/releases/latest")["tag_name"].lstrip("v")


def mkvtoolnix_latest() -> str:
    m = re.search(r"^# Version (\d+(?:\.\d+)+)", _get("https://mkvtoolnix.download/doc/NEWS.md"), re.M)
    if not m:
        raise ValueError("no version in MKVToolNix's NEWS")
    return m.group(1)


def ffmpeg_latest() -> str:
    found = {parse(v) for v in re.findall(r"ffmpeg-(\d+(?:\.\d+)+)\.tar\.xz", _get("https://ffmpeg.org/releases/"))}
    if not found:
        raise ValueError("no releases listed at ffmpeg.org")
    return ".".join(str(x) for x in max(found))


def npm_latest(package: str) -> Callable[[], str]:
    return lambda: _json(f"https://registry.npmjs.org/{package}/latest")["version"]


# ---------------------------------------------------------------- the dependencies
@dataclass
class Dep:
    name: str
    have: Callable[[], str]  # the version the project pins
    newest: Callable[[], str]  # upstream's newest release
    where: str  # where the project's version is held
    major: int = 1  # how many leading numbers make the major version (Python: two, 3.16 is a prompt; MKVToolNix: none, every release taken)
    bump: Callable[[str], str] | None = None  # moves the pin to a version; what changed. None: a prompt whatever the release
    cant: str = ""  # what moving it takes, for the prompt


def _bump_pyright(new: str) -> str:
    return _repin(BUILD, r"pyright@(\d+(?:\.\d+)+)", new)


DEPS = [
    Dep("Python", image_python, lambda: python_cycles()[0]["cycle"], "PYTHON_TAG in the Dockerfile (and python-version in build.yml)",
        major=2, cant="A new Python: dev/future.py clean on it first, then PYTHON_TAG in the Dockerfile and python-version in build.yml."),
    Dep("Debian", lambda: _debian_number(debian_of_tag()), lambda: _debian_number(debian_newest()), "the python image's tag",
        cant="A new Debian: dev/future.py --python 3.15 --tag 3.15-slim-<codename> clean on it first (MKVToolNix's repository has to build "
             "for it too), then PYTHON_TAG in the Dockerfile."),
    Dep("MKVToolNix", _arg("MKVTOOLNIX"), mkvtoolnix_latest, "ARG MKVTOOLNIX in the Dockerfile", major=0, bump=_mkvtoolnix_bump,
        cant="ARG MKVTOOLNIX in the Dockerfile, once mkvtoolnix.download has the release for the image's Debian."),
    Dep("ffmpeg", _arg("FFMPEG"), ffmpeg_latest, "ARG FFMPEG in the Dockerfile",
        bump=_bump_arg("FFMPEG", lambda v: f"https://ffmpeg.org/releases/ffmpeg-{v}.tar.xz"),
        cant="ARG FFMPEG in the Dockerfile; the optimizer tests inside the image then say whether the swap checks still pass."),
    Dep("oxipng", _arg("OXIPNG"), github_release("oxipng/oxipng"), "ARG OXIPNG in the Dockerfile",
        bump=_bump_arg("OXIPNG", lambda v: f"https://github.com/oxipng/oxipng/releases/download/v{v}/oxipng-{v}-x86_64-unknown-linux-gnu.tar.gz"),
        cant="ARG OXIPNG in the Dockerfile; CoverArtTests inside the image say whether it still compresses as expected."),
    Dep("libjpeg-turbo", _arg("LIBJPEG_TURBO"), github_release("libjpeg-turbo/libjpeg-turbo"), "ARG LIBJPEG_TURBO in the Dockerfile",
        bump=_bump_arg("LIBJPEG_TURBO", lambda v: f"https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/{v}/libjpeg-turbo-{v}.tar.gz"),
        cant="ARG LIBJPEG_TURBO in the Dockerfile; CoverArtTests inside the image say whether jpegtran still does."),
    Dep("pyright", lambda: _pin(BUILD, r"pyright@(\d+(?:\.\d+)+)"), npm_latest("pyright"), "its pin in build.yml", bump=_bump_pyright),
]


# ---------------------------------------------------------------- the check
@dataclass
class Report:
    lines: list[str] = field(default_factory=list)  # one per dependency, as it stands
    bumped: list[str] = field(default_factory=list)  # pins moved, what changed
    prompts: list[str] = field(default_factory=list)  # what's left to you
    stale: bool = False  # upstream not reached: the last day's answers used


def _load_cache() -> dict:
    try:
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(found: dict) -> None:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump({"at": time.time(), "found": found}, fh)


def lookup(fresh: bool = False) -> tuple[dict, bool]:
    """Every dependency's (have, newest), from upstream — or, within a day, from the last look. (found, stale)."""
    cache = _load_cache()
    if not fresh and cache.get("found") and time.time() - cache.get("at", 0) < A_DAY:
        return cache["found"], False
    found, failed = {}, []
    for d in DEPS:
        try:
            found[d.name] = [d.have(), d.newest()]
        except (OSError, ValueError, KeyError, TypeError) as e:
            failed.append(f"{d.name}: {getattr(e, 'reason', e)}")
    if failed:
        if cache.get("found"):
            return cache["found"], True  # (the last look, however old: better than nothing)
        raise Offline("; ".join(failed))
    _save_cache(found)
    return found, False


def check(apply: bool = True, fresh: bool = False) -> Report:
    """The report: each dependency as it stands; with ``apply``, pins a minor or patch behind moved."""
    found, stale = lookup(fresh)
    out = Report(stale=stale)
    for d in DEPS:
        have, newest = found[d.name]
        h, n = parse(have), parse(newest)
        if n <= h:
            out.lines.append(f"{d.name} {have}: current")
            continue
        held = HOLD.get(d.name)
        if held and parse(held) >= n:
            out.lines.append(f"{d.name} {have}: {newest} is out, held by choice ({d.where})")
            continue
        level = "major" if n[:d.major] != h[:d.major] else "minor"
        if level == "minor" and d.bump:
            if apply:
                out.lines.append(f"{d.name} {have} → {newest}: moved")
                out.bumped.append(f"{d.name}: " + d.bump(newest))
            else:
                out.lines.append(f"{d.name} {have} → {newest}: to be moved")
                out.prompts.append(f"{d.name} {have} → {newest}: moved by the check in the checkout (python3 dev/upkeep.py)")
            continue
        why = f"a {level} release behind ({d.where})" if d.bump else f"behind ({d.where})"
        out.lines.append(f"{d.name} {have}: {newest} is out — {why}")
        out.prompts.append(f"{d.name} {have} → {newest}: {d.cant}")
    return out


def future_track() -> str | None:
    """What dev/future.py last ran on — each dependency's version and channel (alpha, nightly, snapshot, stable…) — from its
    day's choices; None when it hasn't run here."""
    try:
        with open(FUTURE, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return None
    picks = [p for p in got.get("picks") or [] if isinstance(p, dict) and p.get("name")]
    return ", ".join(f"{p['name']} {p.get('version', '?')} ({p.get('channel', '?')})" for p in picks) or None


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--fresh", action="store_true", help="ask upstream again rather than using the day's answers")
    ap.add_argument("--report", action="store_true", help="look, but move no pin")
    args = ap.parse_args()
    try:
        r = check(apply=not args.report, fresh=args.fresh)
    except Offline as e:
        print(f"Upstream not reached: {e}")
        return 2
    if r.stale:
        print("(upstream not reached now: the last look's answers)")
    print("\n".join(r.lines))
    if track := future_track():
        print(f"\nThe future run (dev/future.py) last chose: {track}")
    if r.bumped:
        print("\nMoved:\n  " + "\n  ".join(r.bumped))
    if r.prompts:
        print("\nFor you:\n  " + "\n  ".join(r.prompts))
    return 1 if r.prompts else 0


if __name__ == "__main__":
    sys.exit(main())

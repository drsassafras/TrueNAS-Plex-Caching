"""What the app depends on, kept current: each dependency's version as the project pins or ships it, against the newest
release upstream — looked up once a day by this script (tnsync runs it at the end of every run). The suite's
``test_dependencies_are_current`` (by hand; GitHub doesn't run it) reads only the last look's answers: never upstream or npm.

    python3 dev/upkeep.py            # the report; a pin a minor or patch release behind is moved to it
    python3 dev/upkeep.py --fresh    # asks upstream again rather than using the day's answers
    python3 dev/upkeep.py --report   # looks, changes nothing
    python3 dev/upkeep.py --tnsync   # the same, as the box tnsync shows at the end of a run (TN_UPKEEP in .tnsync)

Pins are moved only in a checkout of the dev branch (MOVES_ON), or of main (MINORS_ON): dev carries the newest software,
majors and all; main is kept at the newest minor and patch release of what it has, and never takes a major (those come
with dev, when it's released): there a major release isn't looked at, nor reported. Anywhere else — another branch, a
copy that isn't a checkout (tnsync's, the image's) — it reports and moves nothing.

What it does with each: a dependency behind by a patch or minor release is moved to it where the project holds the pin
(``bump``: the file edited, said in the report); one behind by a major release — or one with no build upstream yet — is a prompt: the test fails naming it until it's moved or, meant to stay, held (HOLD below). The GitHub
Actions are Dependabot's (.github/dependabot.yml): not looked at here.

The dependencies: the Python the image runs (PYTHON_TAG in the Dockerfile; a new minor is a prompt, once released: moving
is a line there and one in build.yml, after dev/future.py is clean on it), the Debian that tag's image is built on (a new
Debian is a prompt), the tools the image installs — MKVToolNix from its own repository, ffmpeg built from its release,
oxipng and libjpeg-turbo from theirs, each an ARG in the Dockerfile moved here once upstream has the release (every
MKVToolNix release is taken, since each carries fixes) — pyright (its pin in build.yml; patch and minor releases taken), and
what builds the web app's CSS, scripts and pages: the Node of the image build's web stage (NODE_TAG in the Dockerfile, an LTS
major; a new LTS is a prompt) and the packages of dev/web.js — Browserslist, caniuse-lite and MDN's browser data, Autoprefixer,
PostCSS, cssnano, Stylelint and its checks, ESLint and its, html-validate, Terser, html-minifier-terser: every one
dev/package.json pins — moved with npm, so dev/package-lock.json moves too; a major is a prompt.
And Home Assistant's MQTT discovery rules: each release (PyPI's newest) has every key the discovery messages use
(DISCOVERY_KEYS in plexcache/homeassistant.py) looked for in its sources — still taken, not deprecated or removed — and
HA_CHECKED is moved to it; a key it no longer takes is a prompt naming it.
"""

import argparse
import ast
import http.client
import json
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(ROOT, "Dockerfile")
BUILD = os.path.join(ROOT, ".github", "workflows", "build.yml")
PACKAGE = os.path.join(ROOT, "dev", "package.json")  # what checks and builds the web app (dev/web.js): Autoprefixer and the rest
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "plex-cache", "upkeep.json")
FUTURE = os.path.join(os.path.dirname(CACHE), "future.json")  # what dev/future.py last chose to run on (its day's choices)
A_DAY = 24 * 3600
TIMEOUT = 20   # an answer waited on (each read)
CONNECT = 5    # a connection to one of a host's addresses
DEADLINE = 45  # one dependency's whole look (its requests, every address tried): past it, not reached this time

# A dependency meant to stay where it is, by choice: its name -> the newest version the choice covers (it prompts again
# once upstream passes that). Empty: nothing is held.
HOLD: dict[str, str] = {}

# The branch pins are moved on (in a checkout of it), and the one only minor and patch releases count on (moved there too;
# a major is left to dev). Anywhere else upkeep only reports.
MOVES_ON = "dev"
MINORS_ON = "main"
WIDTH = 66  # tnsync's box: the lines --tnsync prints fit it


class Offline(Exception):
    """Upstream couldn't be reached (and there's no answer from the last day to go by)."""


_asking = threading.local()   # the dependency this thread is looking up (lookup)
WHERE: dict[str, str] = {}    # dependency -> the host it's waiting on now, for the bar


def _connect(address, timeout=None, source_address=None, **kw):
    """socket.create_connection, but IPv4 addresses first and CONNECT seconds for each: a network whose IPv6 goes nowhere
    (or a dead address of the many a registry lists) is passed over in seconds, not TIMEOUT for every address in turn.
    The answer is then waited on for TIMEOUT."""
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda i: i[0] != socket.AF_INET)
    err: OSError | None = None
    for family, kind, proto, _, addr in infos:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(CONNECT)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
            sock.settimeout(timeout)
            return sock
        except OSError as e:
            err = e
            sock.close()
    raise err or OSError(f"no address for {host}")


class _HTTPS(http.client.HTTPSConnection):
    def connect(self):
        self._create_connection = _connect  # (http.client's hook: the proxy's address too, when there is one)
        super().connect()


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        self.tls = ssl.create_default_context()  # (as urllib's own: the system's certificates, the host name checked)
        super().__init__(context=self.tls)

    def https_open(self, req):
        return self.do_open(_HTTPS, req, context=self.tls)


_OPENER = urllib.request.build_opener(_HTTPSHandler())


def _open(req):
    name = getattr(_asking, "name", None)
    if name:
        WHERE[name] = urllib.parse.urlsplit(req.full_url).hostname or ""
    return _OPENER.open(req, timeout=TIMEOUT)


def _get(url: str, accept: str = "*/*") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "plex-cache-upkeep", "Accept": accept})
    with _open(req) as r:
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
        with _open(req):
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
    package: str = ""  # a package of dev/package.json: moved with the others in one npm install (check)


def _bump_pyright(new: str) -> str:
    return _repin(BUILD, r"pyright@(\d+(?:\.\d+)+)", new)


def node_lts() -> str:
    """The newest LTS release line of Node ("26"), from nodejs.org's index."""
    for r in _json("https://nodejs.org/dist/index.json"):
        if r.get("lts"):
            return r["version"].lstrip("v").split(".")[0]
    raise ValueError("no LTS release listed at nodejs.org")


WEB_SECTIONS = ("devDependencies", "dependencies")  # (npm ci installs both; each package stays in its own when moved)


def web_packages() -> dict[str, str]:
    """Every package dev/package.json pins, either section: name → version."""
    got = json.loads(_read(PACKAGE))
    return {name: version for section in WEB_SECTIONS for name, version in (got.get(section) or {}).items()}


def _package_pin(package: str) -> Callable[[], str]:
    """A pin in dev/package.json."""
    return lambda: web_packages()[package]


NPM_TIMEOUT = 300


def npm_install(pins: dict[str, str]) -> None:
    """Moves pins in dev/package.json with npm, every one in one install (npm resolves them together, once), so
    dev/package-lock.json moves with them (and dev/node_modules). ValueError (npm's own words) or TimeoutExpired."""
    got = subprocess.run(["npm", "install", "--prefix", os.path.dirname(PACKAGE), "--save-exact", "--no-audit", "--no-fund",
                          *(f"{p}@{v}" for p, v in pins.items())], capture_output=True, text=True, timeout=NPM_TIMEOUT)
    if got.returncode:
        said = [ln.removeprefix("npm error ").strip() for ln in (got.stderr or got.stdout).splitlines()]
        said = [ln for ln in said if ln and not ln.startswith(("A complete log", "/", "In most cases"))]
        raise ValueError(f"npm couldn't install {', '.join(f'{p}@{v}' for p, v in pins.items())}: " + " ".join(said)[:300])


def _bump_package(package: str) -> Callable[[str], str]:
    """Moves a pin in dev/package.json with npm (npm_install); check moves several in one install instead."""
    def bump(new: str) -> str:
        old = _package_pin(package)()
        npm_install({package: new})
        return f"dev/package.json (and its lock): {package} {old} → {new}"
    return bump


HA = os.path.join(ROOT, "plexcache", "homeassistant.py")   # what the MQTT discovery messages use (DISCOVERY_KEYS), checked as of HA_CHECKED
HA_SOURCE = "https://raw.githubusercontent.com/home-assistant/core/{tag}/homeassistant/{path}"
HA_FILES = ("components/mqtt/abbreviations.py", "components/mqtt/const.py", "const.py", "components/mqtt/schemas.py",
            "components/mqtt/entity.py", "components/mqtt/config.py", "components/mqtt/sensor.py",
            "components/mqtt/binary_sensor.py", "components/mqtt/switch.py", "components/mqtt/button.py",
            "components/mqtt/number.py", "components/mqtt/select.py", "components/mqtt/update.py")


def ha_latest() -> str:
    """Home Assistant's newest release (PyPI's, which is the stable one)."""
    return _json("https://pypi.org/pypi/homeassistant/json")["info"]["version"]


def discovery_keys() -> dict[str, set[str]]:
    """DISCOVERY_KEYS from plexcache/homeassistant.py, read (not imported: upkeep runs without the app's imports)."""
    for node in ast.walk(ast.parse(_read(HA))):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DISCOVERY_KEYS" for t in node.targets):
            return {k: set(v) for k, v in ast.literal_eval(node.value).items()}
    raise ValueError("no DISCOVERY_KEYS in plexcache/homeassistant.py")


def ha_rules(sources: dict[str, str]) -> tuple[set[str], set[str]]:
    """From Home Assistant's MQTT sources (path -> text): every discovery key it takes (its abbreviations' full names and
    every CONF_ name the MQTT component defines), and those deprecated or removed (cv.deprecated / cv.removed)."""
    conf: dict[str, str] = {}
    taken: set[str] = set()
    for path, text in sources.items():
        tree = ast.parse(text)
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.startswith("CONF_"):
                        conf[t.id] = node.value.value
                        if "mqtt" in path:
                            taken.add(node.value.value)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict) and path.endswith("abbreviations.py"):
                taken |= {v.value for v in node.value.values if isinstance(v, ast.Constant) and isinstance(v.value, str)}
    gone: set[str] = set()
    for text in sources.values():
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("deprecated", "removed")
                    and node.args):
                a = node.args[0]
                key = conf.get(a.id) if isinstance(a, ast.Name) else a.value if isinstance(a, ast.Constant) else None
                if isinstance(key, str):
                    gone.add(key)
    return taken - gone, gone


def ha_check(version: str) -> list[str]:
    """The keys of DISCOVERY_KEYS Home Assistant ``version`` doesn't take, or has deprecated: what's wrong, [] when none."""
    sources: dict[str, str] = {}
    for path in HA_FILES:
        try:
            sources[path] = _get(HA_SOURCE.format(tag=version, path=path))
        except urllib.error.HTTPError as e:   # (a file the release doesn't have: its keys are elsewhere; the abbreviations it must)
            if e.code != 404 or path == HA_FILES[0]:
                raise
    taken, gone = ha_rules(sources)
    out = []
    for where, keys in discovery_keys().items():
        for k in sorted(keys):
            if k in gone:
                out.append(f"{k} ({where}): deprecated or removed in Home Assistant {version}")
            elif k not in taken:
                out.append(f"{k} ({where}): not a discovery key Home Assistant {version} knows")
    return out


def _bump_ha(new: str) -> str:
    wrong = ha_check(new)
    if wrong:
        raise ValueError("the discovery messages need changing first: " + "; ".join(wrong))
    return _repin(HA, r'HA_CHECKED = "([^"]+)"', new)


def _web_dep(package: str) -> "Dep":
    return Dep(package, _package_pin(package), npm_latest(package), "its pin in dev/package.json", bump=_bump_package(package), package=package,
               cant=f"{package} in dev/package.json (npm install --prefix dev --save-exact {package}@<version>); "
                    "WebBuildTests then say whether the CSS and scripts still pass and build.")


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
    Dep("Node", lambda: _pin(DOCKERFILE, r"ARG NODE_TAG=(\d+)"), node_lts, "NODE_TAG in the Dockerfile (the web stage)",
        cant="A new Node LTS: NODE_TAG in the Dockerfile; the image build then says whether the CSS and scripts still build."),
    # Home Assistant's MQTT discovery rules: each release, every key the discovery messages use is looked for in its
    # sources (still taken, not deprecated); then HA_CHECKED moves. One that isn't is a prompt naming it.
    Dep("Home Assistant (MQTT discovery)", lambda: _pin(HA, r'HA_CHECKED = "([^"]+)"'), ha_latest,
        "HA_CHECKED in plexcache/homeassistant.py", major=0, bump=_bump_ha,
        cant="Change the discovery messages (plexcache/homeassistant.py: entities and _publish_discovery, and DISCOVERY_KEYS) "
             "for that release's MQTT rules (home-assistant.io/integrations/mqtt), then HA_CHECKED."),
    *(_web_dep(p) for p in web_packages()),  # (every one it pins, both sections)
]


# ---------------------------------------------------------------- the check
@dataclass
class Report:
    lines: list[str] = field(default_factory=list)  # one per dependency, as it stands
    bumped: list[str] = field(default_factory=list)  # pins moved, what changed
    moved: list[str] = field(default_factory=list)  # the same, short: name, from → to
    prompts: list[str] = field(default_factory=list)  # what's left to you
    stale: bool = False  # upstream not reached: the last day's answers used
    news: list[tuple[str, str, str, str, str]] = field(default_factory=list)  # what's left to you: name, have, newest, level, what to do
    moves_on: str = MOVES_ON  # the branch its minor releases move on (where it ran, when that's main)


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


def lookup(fresh: bool = False, saved: bool = False, progress=None) -> tuple[dict, bool]:
    """Every dependency's (have, newest), from upstream — or, within a day, from the last look. (found, stale).
    ``saved``: the last look's answers only, however old (stale past a day), upstream never asked — Offline when there's
    none for every dependency yet (the suite's test: tnsync's box, or this script, does the looking)."""
    cache = _load_cache()
    whole = bool(cache.get("found")) and {d.name for d in DEPS} <= set(cache["found"])  # (a dependency added since: asked again)
    if saved:
        if not whole:
            raise Offline("no saved answers for every dependency yet. To look them up: python3 dev/upkeep.py")
        return cache["found"], time.time() - cache.get("at", 0) >= A_DAY
    if not fresh and whole and time.time() - cache.get("at", 0) < A_DAY:
        if progress is not None:   # (its bar all the same: every one known, from the day's answers, by this process alone)
            progress.workers = 1
            progress.finished = "dependencies · the day's answers from upstream"
            progress.at(len(DEPS), len(DEPS), progress.finished)
        return cache["found"], False
    found, failed = {}, []
    if progress is not None:
        progress.reset(len(DEPS), "looked up")
        progress.workers = len(DEPS)   # (a thread for each, below: all asked at once)
        progress.finished = "dependencies · checked against upstream"

    # every one asked at once, each in a thread of its own: the look takes about as long as the slowest answer, and one
    # that hasn't answered by DEADLINE is left (not reached this time: its last answer is used, when there is one)
    answers: queue.Queue = queue.Queue()

    def look(d):
        _asking.name = d.name
        try:
            answers.put((d, [d.have(), d.newest()], None))
        except (OSError, ValueError, KeyError, TypeError) as e:
            answers.put((d, None, f"{d.name}: {getattr(e, 'reason', e)}"))
    WHERE.clear()
    for d in DEPS:
        threading.Thread(target=look, args=(d,), name=f"upkeep {d.name}", daemon=True).start()
    pending, started = {d.name: d for d in DEPS}, time.time()
    while pending:
        if progress is not None:
            waiting = [n for n in pending]
            at = sorted({WHERE[n] for n in waiting if WHERE.get(n)})
            progress.show(f"waiting on {len(waiting)}: {', '.join(waiting)}" + (f" ({', '.join(at)})" if at and len(at) <= 2 else ""), ticking=True)
        try:
            d, got, why = answers.get(timeout=max(0.1, started + DEADLINE - time.time()))
        except queue.Empty:  # past the deadline: the rest are left to their threads (daemons: they end with the process)
            for name in list(pending):
                failed.append(f"{name}: no answer in {DEADLINE} s" + (f" from {WHERE[name]}" if WHERE.get(name) else ""))
                if progress is not None:
                    progress.draw(f"{name}: not reached")
                pending.pop(name)
            break
        pending.pop(d.name, None)
        if got is not None:
            found[d.name] = got
        else:
            failed.append(why)
        if progress is not None:
            progress.draw(f"{d.name}: " + (f"{got[1]} upstream" if got is not None else "not reached"))
    failed.sort(key=lambda f: [d.name for d in DEPS].index(f.split(":")[0]))  # (in DEPS' order, however they came back)
    if failed:
        old = cache.get("found") or {}
        missing = [f for f in failed if f.split(":")[0] not in old]
        if not missing:  # each one not reached now from the last look, however old: better than nothing (and not saved as new)
            return {d.name: found.get(d.name) or old[d.name] for d in DEPS}, True
        raise Offline("; ".join(failed))
    _save_cache(found)
    return found, False


def branch() -> str | None:
    """The branch checked out where this file is; None when it isn't a git checkout (tnsync's copy, the image)."""
    try:
        got = subprocess.run(["git", "-C", ROOT, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = got.stdout.strip()
    return name if got.returncode == 0 and name and name != "HEAD" else None


def may_move() -> bool:
    """Whether pins may be moved here: only in a checkout of MOVES_ON or MINORS_ON."""
    return branch() in (MOVES_ON, MINORS_ON)


def _why(e: BaseException) -> str:
    return f"took longer than {e.timeout:.0f} s" if isinstance(e, subprocess.TimeoutExpired) else str(getattr(e, "reason", e))


def check(apply: bool = True, fresh: bool = False, saved: bool = False, progress=None, where: str | None = MOVES_ON) -> Report:
    """The report: each dependency as it stands; with ``apply``, pins a minor or patch behind moved. ``saved``: from the
    last look's answers only (see lookup). ``progress`` (dev/progress.py's bar): each dependency as it's looked up, then
    each pin as it's moved (npm's installs can take a while), so the look is seen to be going. ``where``: the branch it's
    for — on MINORS_ON (main) a major release is no one's to do there (it comes with dev): a line, not a prompt."""
    found, stale = lookup(fresh, saved, progress)
    majors = where != MINORS_ON
    out = Report(stale=stale, moves_on=MINORS_ON if not majors else MOVES_ON)
    plan = []  # (dep, have, newest, what): what is current, held, move, report or behind
    for d in DEPS:
        have, newest = found[d.name]
        h, n = parse(have), parse(newest)
        held = HOLD.get(d.name)
        if n <= h:
            plan.append((d, have, newest, "current", ""))
        elif held and parse(held) >= n:
            plan.append((d, have, newest, "held", ""))
        else:
            level = "major" if n[:d.major] != h[:d.major] else "minor"
            if level == "major" and not majors:
                plan.append((d, have, newest, "for dev", level))
            else:
                plan.append((d, have, newest, ("move" if apply else "report") if level == "minor" and d.bump else "behind", level))
    moving = [p for p in plan if p[3] == "move"]
    npm = {d.package: newest for d, _, newest, _, _ in moving if d.package}
    steps = len([p for p in moving if not p[0].package]) + (1 if npm else 0)
    if progress is not None and steps:
        progress.reset(steps, "moved")
        progress.workers = 1   # (one at a time)
        progress.finished = f"pins · moved on {where or 'this branch'}"
    failed: dict[str, str] = {}  # dependency -> why it wasn't moved
    if npm:  # the packages of dev/package.json: one npm install for all of them (npm's slow part is once, not each time)
        olds = {d.package: have for d, have, _, _, _ in moving if d.package}
        if progress is not None:
            progress.show(f"npm install {' '.join(f'{p}@{v}' for p, v in npm.items())}", ticking=True)
        try:
            npm_install(npm)
            for p, v in npm.items():
                out.bumped.append(f"{p}: dev/package.json (and its lock): {p} {olds[p]} → {v}")
                out.moved.append(f"{p} {olds[p]} → {v}")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            why = _why(e)
            if len(npm) > 1 and not isinstance(e, subprocess.TimeoutExpired):  # (one of them holds the others up: each alone, to say which)
                for p, v in npm.items():
                    if progress is not None:
                        progress.show(f"npm install {p}@{v} (alone: the install of them all failed)", ticking=True)
                    try:
                        npm_install({p: v})
                        out.bumped.append(f"{p}: dev/package.json (and its lock): {p} {olds[p]} → {v}")
                        out.moved.append(f"{p} {olds[p]} → {v}")
                    except (OSError, ValueError, subprocess.TimeoutExpired) as e1:
                        failed[p] = _why(e1)
            else:
                failed.update(dict.fromkeys(npm, why))
        if progress is not None:
            from dev.progress import counted  # (a count as every dev tool says it; the bar put the project on the path)
            progress.draw(f"{counted(len(npm) - len(failed), 'package', of=len(npm))} moved with npm")
    for d, have, newest, what, level in plan:
        if what == "current":
            out.lines.append(f"{d.name} {have}: current")
        elif what == "held":
            out.lines.append(f"{d.name} {have}: {newest} is out, held by choice ({d.where})")
        elif what == "for dev":
            out.lines.append(f"{d.name} {have}: {newest} is out — a major release, for {MOVES_ON} (never moved on {MINORS_ON})")
        elif what == "move":
            assert d.bump is not None
            if not d.package:
                if progress is not None:
                    progress.show(f"moving {d.name} {have} → {newest}", ticking=True)
                try:
                    out.bumped.append(f"{d.name}: " + d.bump(newest))
                    out.moved.append(f"{d.name} {have} → {newest}")
                except (OSError, ValueError, subprocess.TimeoutExpired) as e:
                    failed[d.name] = _why(e)
                if progress is not None:
                    progress.draw(f"{d.name} {newest}")
            if d.name in failed:  # (not moved now: said, and tried again next time)
                why = failed[d.name]
                out.lines.append(f"{d.name} {have} → {newest}: couldn't be moved — {why}")
                todo = f"Not moved by itself this time ({why}); tried again at the next look. " + (d.cant or f"By hand: {d.where}.")
                out.prompts.append(f"{d.name} {have} → {newest}: {todo}")
                out.news.append((d.name, have, newest, "new", todo))
            else:
                out.lines.append(f"{d.name} {have} → {newest}: moved")
        elif what == "report":
            out.lines.append(f"{d.name} {have} → {newest}: to be moved")
            on = out.moves_on
            out.prompts.append(f"{d.name} {have} → {newest}: moved on {on} by itself (tnsync does it, with {on} checked out). To move it now, on {on}: python3 dev/upkeep.py")
            out.news.append((d.name, have, newest, "minor", f"Moves on {on} by itself: at the next tnsync (or python3 dev/upkeep.py) with {on} checked out."))
        else:
            why = f"a {level} release behind ({d.where})" if d.bump else f"behind ({d.where})"
            out.lines.append(f"{d.name} {have}: {newest} is out — {why}")
            out.prompts.append(f"{d.name} {have} → {newest}: {d.cant}")
            out.news.append((d.name, have, newest, level if d.bump else "new", d.cant))
    if progress is not None:
        progress.clear()
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
    tools = [f"{p['name']} {p.get('version', '?')} ({p.get('channel', '?')})" for p in picks if p.get("kind") != "npm"]
    npm = [p for p in picks if p.get("kind") == "npm"]
    if npm:  # (the web's packages in one: those on a pre-release named)
        ahead = [f"{p['name']} {p.get('version', '?')} ({p.get('channel', '?')})" for p in npm if p.get("channel") not in ("stable", "pin")]
        tools.append(f"{len(npm)} Node packages at their newest" + (f", of them {', '.join(ahead)}" if ahead else ""))
    return ", ".join(tools) or None


def _wrap(text: str, indent: str = "") -> list[str]:
    import textwrap
    return textwrap.wrap(text, WIDTH, initial_indent=indent, subsequent_indent=indent, break_on_hyphens=False, break_long_words=False) or [indent]


def box(r: Report | None, where: str | None, error: str = "") -> list[str]:
    """What tnsync shows: a first line it reads — current (a green box), behind (yellow), unknown (grey) — then the box's
    lines, each fitting it. Behind: what moved here (to test and commit), then what's left to you, each with what to do."""
    if r is None:
        return ["unknown", "DEPENDENCIES NOT CHECKED  -  UPSTREAM NOT REACHED", "", *_wrap(error or "no answer"),
                "Nothing is known from this run; tnsync looks again next time."]
    old = ["", "(Upstream not reached now: these are the last look's answers.)"] if r.stale else []
    if not r.bumped and not r.news:
        return ["current", "DEPENDENCIES  -  ALL UP TO DATE", "",
                *_wrap(f"Each of the {len(DEPS)} dependencies dev/upkeep.py keeps (Python, Debian, the image's tools, "
                       "pyright, Node, the web's packages and Home Assistant's MQTT rules) is at upstream's newest release."), *old]
    majors = sum(1 for n in r.news if n[3] != "minor")
    title = "NEW VERSIONS UPSTREAM  -  " + ", ".join(x for x in (f"{len(r.bumped)} MOVED" if r.bumped else "",
                                                                 f"{majors} FOR YOU" if majors else "",
                                                                 f"{len(r.news) - majors} TO MOVE ON {r.moves_on.upper()}" if len(r.news) > majors else "") if x)
    out = ["behind", title]
    if r.bumped:
        out += ["", f"Moved here ({where}): run the tests, then commit them."]
        for b in r.bumped:
            out += _wrap(b, "  ")
    for name, have, newest, level, todo in r.news:
        out += ["", f"{name} {have} → {newest}" + {"major": "  (a major release)", "minor": "", "new": ""}[level]]
        out += _wrap(todo if level == "minor" else f"To do: {todo}", "  ")
    return out + old


def summary(r: Report | None, error: str = "") -> str:
    """The box in a line, for the board's Dependency Versions block (under its bar): what moved and what's new, each from → to."""
    from dev.progress import result, tally  # (the result line every dev tool says)
    if r is None:
        return result("upstream not reached", error or "no answer")
    if not r.bumped and not r.news:
        return result(tally(len(DEPS), "dependency", "up to date", "dependencies"))
    said = [f"moved {m}" for m in r.moved] + [f"{n} {have} → {newest}" + (" (major)" if level == "major" else "")
                                              for n, have, newest, level, _ in r.news]
    return result(*said)


def _progress(tnsync: bool):
    """Where the bar goes while it looks (dev/progress.py): on tnsync's board, its Dependency Versions section (PLEXCACHE_PROGRESS=
    feed: to stdout, where the board takes it from the box's lines); else the terminal — under tnsync without its board,
    which keeps this script's output for its box, straight to it (/dev/tty); else stderr, when that's one. None: nowhere to
    show it (CI, a log)."""
    if ROOT not in sys.path:  # (run as a script: dev/ is on the path, not the project)
        sys.path.insert(0, ROOT)
    from dev.progress import Progress, feeding
    if feeding():
        return Progress(len(DEPS), stream=sys.stdout, unit="looked up", bar="upkeep")
    if tnsync:
        try:
            stream = open("/dev/tty", "w", encoding="utf-8")  # noqa: SIM115 — (kept open for the run)
        except OSError:
            return None
    elif sys.stderr.isatty():
        stream = sys.stderr
    else:
        return None
    return Progress(len(DEPS), tty=True, stream=stream, unit="looked up")


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--fresh", action="store_true", help="ask upstream again rather than using the day's answers")
    ap.add_argument("--report", action="store_true", help="look, but move no pin")
    ap.add_argument("--tnsync", action="store_true", help="the result as tnsync's box: a first line it reads, then the box (moves pins on dev, as without it)")
    args = ap.parse_args()
    where = branch()
    # (tnsync's board looks while the tests run, TNSYNC_UPKEEP=look, and moves pins only once they're done: =move)
    move = not args.report and where in (MOVES_ON, MINORS_ON) and os.environ.get("TNSYNC_UPKEEP") != "look"
    bar = _progress(args.tnsync)
    from dev.progress import said  # (on the board: the box in a line, under its bar; _progress put the project on the path)
    try:
        r = check(apply=move, fresh=args.fresh, progress=bar, where=where)
    except Offline as e:
        if bar is not None:
            bar.clear()
        if args.tnsync:
            print("\n".join(box(None, where, str(e))))
            if os.environ.get("TNSYNC_UPKEEP") == "look":   # (nothing to move: the board's second step isn't run)
                from dev.progress import then
                then(0)
            said(summary(None, str(e)))
            return 0
        print(f"Upstream not reached: {e}")
        return 2
    if args.tnsync:
        print("\n".join(box(r, where)))
        if os.environ.get("TNSYNC_UPKEEP") == "look":   # (the board's first step: what its second, once the checks pass, has to move)
            from dev.progress import result, tally, then
            moves = len([n for n in r.news if n[3] == "minor"]) if where in (MOVES_ON, MINORS_ON) and not args.report else 0
            then(moves)
            if moves:
                said(result(tally(len(DEPS), "dependency", "looked up", "dependencies", of=len(DEPS)), f"{moves} to move once the checks pass"))
                return 0
        said(summary(r))
        return 0
    if not args.report and not move:
        print(f"(on {where or 'a copy that is not a checkout'}: reported only; versions are moved on {MOVES_ON}, and minor ones on {MINORS_ON})")
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

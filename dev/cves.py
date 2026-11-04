"""Known vulnerabilities (CVEs) in what the app runs and is built with: a part of the dependency check (dev/upkeep.py,
tnsync's Dependencies block), looked up once a day with the versions.

It says only what needs doing. A CVE someone else is already fixing stays quiet; one is shown when there's something for
us to do — always as a warning (the block orange), never a failure:

  - fixed in a patch or minor release upkeep moves by itself: nothing asked; the move's line says what it fixes;
  - fixed in a Debian package or the base image's own Python packages: nothing, unless the published image still lacks
    the fix DEBIAN_GRACE days after it was first seen (the weekly rebuild takes it; shown only when it didn't);
  - fixed only in a major release, or in a version held by choice (upkeep's HOLD): shown — moving is yours;
  - fixed, but in a package pulled in by another (npm's) that still asks for the old one: quiet for STALE days, then
    shown, with the choices (an npm override, a newer parent, replacing it);
  - fixed in upstream's git only, on a date (NVD's range for a git snapshot, with no release named): quiet when the
    release we have was made after that date (its tag, TAGS), so has it; shown when it's older or can't be dated;
  - no fix: quiet while upstream is at it; shown once there's been none for STALE days, or upstream has made no release
    in DORMANT days — "no fix coming": replace it, patch it ourselves, or move to a maintained fork.
    A Debian package's CVE with no fix is Debian's call (it maintains them, and says which it will fix): counted, not shown.

One fixed here already, that only waits for the image's next build — pip's packages once the Dockerfile takes pip out,
or a package of an image built from an older Dockerfile on another OS (latest, until the next release) — is said apart:
"fixed in dev, waiting on the release", not counted and not orange.

What's shown can be decided (DECISIONS, the maintainer's only): the choice, when, and why. A decided CVE goes quiet; it's
looked at again REVIEW days on, and as soon as a fix comes out for one decided with none (replace, patch, fork); a "not
reachable" one stays decided with a fix (a fix in a package pulled in by another, which still asks for the old one).

Where the CVEs come from: OSV (osv.dev) for the npm packages of dev/package-lock.json and for oxipng (a Rust crate); NVD
(nvd.nist.gov, by each one's CPE) for the ffmpeg, libjpeg-turbo and MKVToolNix the image is built with;
Trivy (dev/tools.toml, through dev/checks.py) for the image of the copy being checked (source(): tnsync's — the folder for
tnsync local, an export of main for tnsync main… — else this checkout), built here from it (build_local): its Debian
packages, and the Python packages the base image carries. Docker not running, or the build failing: the image that copy's
branch publishes instead (images(): dev on dev, latest on main), said in a note. The pins and the npm lock are the copy's too. NVD answers 5 requests in 30 s without a key; NVD_API_KEY in the environment raises it.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "plex-cache", "cves.json")
SEEN = os.path.join(os.path.dirname(CACHE), "cves-seen.json")   # each image fix as first seen missing: how long it's been
DAY = 24 * 3600
STALE = 90      # days a CVE may go without a fix (or a fix without reaching us) before it's ours to act on
DORMANT = 365   # days without an upstream release: no fix is coming
DEBIAN_GRACE = 8   # days after a Debian fix before an image without it is shown (the weekly rebuild takes it)
REVIEW = 182    # days a decision stands before the CVE is shown again

# A CVE decided: its ID → (the choice: "replace", "patch", "fork" or "not reachable", the date, why). The maintainer's only.
DECISIONS: dict[str, tuple[str, str, str]] = {}

# The binaries the image is built with at a version of ours, by their CPE (vendor, product) for NVD: upkeep's dependency of
# that name. (Not Python or Node: the image takes their newest patch release with each weekly rebuild, python:3.15-slim and
# node:26-slim, so a fix in one reaches it by itself; their tag is a minor or major only, too loose for NVD to match on.)
CPES = {"ffmpeg": ("ffmpeg", "ffmpeg"), "libjpeg-turbo": ("libjpeg-turbo", "libjpeg-turbo"), "MKVToolNix": ("bunkus", "mkvtoolnix")}
REPOS = {"oxipng": "oxipng/oxipng", "libjpeg-turbo": "libjpeg-turbo/libjpeg-turbo"}   # (where their releases are: when the last came out)
CRATES = {"oxipng": "oxipng"}   # (upkeep's dependency → its crate on crates.io, for OSV)
# Where an image tool's releases are tagged (GitHub repository, the tag's name): when the release we have was made, for a
# fix NVD gives only as a date in upstream's git (DATED).
TAGS = {"ffmpeg": ("FFmpeg/FFmpeg", "n{version}"), "libjpeg-turbo": ("libjpeg-turbo/libjpeg-turbo", "{version}")}
DATED = re.compile(r"\d{4}-\d{2}-\d{2}$")   # a fix given as a date: NVD's versionEndExcluding for a git snapshot (git-master)


@dataclass
class Vuln:
    id: str               # the CVE's ID (or the advisory's, where it has no CVE)
    package: str          # what has it
    version: str          # the version we have
    kind: str             # npm (a package of dev/package.json), npm-via (one another pulls in), pin (an image tool), debian, base (the base image's Python packages)
    fixed: str = ""       # the first version without it; "" none known
    published: float = 0  # when it was made known
    severity: str = ""
    summary: str = ""
    dep: str = ""         # upkeep's dependency it belongs to (npm, pin); for npm-via, the packages that pull it in
    image: str = ""       # the image it's in (debian, base)
    built: float = 0      # when that image was built
    seen: float = 0       # (debian, base) when this fix was first seen missing from the image (look's SEEN); 0 not known
    distro: str = ""      # (debian) the image's OS as Trivy names it: "debian", or another for one built from an older Dockerfile


@dataclass
class Judged:
    act: list[tuple[str, str]] = field(default_factory=list)   # (what, what to do): shown, orange
    shown: int = 0        # the CVEs in act (an image's package's are one line)
    handled: dict[str, list[str]] = field(default_factory=dict)  # upkeep's dependency → the CVEs its move fixes
    quiet: int = 0        # CVEs left to someone already at it
    debian: int = 0       # Debian's with no fix: its call
    decided: int = 0
    notes: list[str] = field(default_factory=list)   # what couldn't be looked at this time
    waiting: list[tuple[str, str]] = field(default_factory=list)   # (what, why): fixed here, waiting on the image's next build
    waited: int = 0       # the CVEs in waiting: said, not counted


def _open(url: str, body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "plex-cache-upkeep", "Accept": "application/json",
                                                           **({"Content-Type": "application/json"} if body else {}), **(headers or {})})
    from dev import upkeep  # (its connections: every address tried, the proxy's settings)
    return upkeep._open(req)


def get_json(url: str, body: dict | None = None, headers: dict | None = None):
    with _open(url, json.dumps(body).encode() if body is not None else None, headers) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _when(stamp: str) -> float:
    """An ISO time (2026-01-02T03:04:05.678Z, 2026-01-02) as seconds since the epoch; 0 when there's none."""
    if not stamp:
        return 0
    import datetime as dt
    try:
        got = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return (got if got.tzinfo else got.replace(tzinfo=dt.UTC)).timestamp()


def parse(version: str) -> tuple[int, ...]:
    from dev import upkeep
    return upkeep.parse(version) if re.match(r"v?\d", version.strip()) else ()


# ---------------------------------------------------------------- npm: OSV over the lock
def lock_packages(lock: dict, direct: set[str]) -> list[tuple[str, str, str]]:
    """Each package the lock installs: (name, version, the packages of dev/package.json that pull it in; "" when it's one)."""
    pkgs = lock.get("packages") or {}
    needs: dict[str, set[str]] = {}   # name → who needs it
    for path, meta in pkgs.items():
        if not path:
            continue
        owner = path.rsplit("node_modules/", 1)[-1]
        for dep in {**(meta.get("dependencies") or {}), **(meta.get("optionalDependencies") or {})}:
            needs.setdefault(dep, set()).add(owner)
    out = []
    for path, meta in pkgs.items():
        if not path or "version" not in meta:
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        if name in direct:
            out.append((name, meta["version"], ""))
            continue
        tops, seen, todo = set(), {name}, [name]
        while todo:
            for parent in needs.get(todo.pop(), ()):
                if parent in direct:
                    tops.add(parent)
                elif parent not in seen:
                    seen.add(parent)
                    todo.append(parent)
        out.append((name, meta["version"], ", ".join(sorted(tops)) or "?"))
    return out


def _cve_id(v: dict) -> str:
    return next((a for a in v.get("aliases") or [] if a.startswith("CVE-")), v.get("id", "?"))


def _osv_fixed(v: dict, ecosystem: str, name: str) -> str:
    for a in v.get("affected") or []:
        p = a.get("package") or {}
        if p.get("ecosystem") == ecosystem and p.get("name") == name:
            for r in a.get("ranges") or []:
                for e in r.get("events") or []:
                    if e.get("fixed"):
                        return str(e["fixed"])
    return ""


def _osv_severity(v: dict) -> str:
    return str((v.get("database_specific") or {}).get("severity") or "").upper()


def osv(queries: list[tuple[str, str, str]]) -> dict[int, list[dict]]:
    """OSV's advisories for each (ecosystem, name, version): by the query's place in the list."""
    if not queries:
        return {}
    got = get_json("https://api.osv.dev/v1/querybatch",
                   {"queries": [{"package": {"ecosystem": e, "name": n}, "version": v} for e, n, v in queries]})
    out: dict[int, list[dict]] = {}
    full: dict[str, dict] = {}
    for i, res in enumerate(got.get("results") or []):
        for brief in res.get("vulns") or []:
            vid = brief.get("id", "")
            if vid not in full:
                full[vid] = get_json(f"https://api.osv.dev/v1/vulns/{vid}")
            out.setdefault(i, []).append(full[vid])
    return out


def npm_vulns(lock_path: str, package_json: str) -> list[Vuln]:
    if not os.path.exists(lock_path):   # (a copy from before the web's npm packages: none to look at)
        return []
    with open(lock_path, encoding="utf-8") as fh:
        lock = json.load(fh)
    with open(package_json, encoding="utf-8") as fh:
        pkg = json.load(fh)
    direct = {n for s in ("devDependencies", "dependencies") for n in (pkg.get(s) or {})}
    pkgs = lock_packages(lock, direct)
    seen, out = set(), []
    for i, found in osv([("npm", n, v) for n, v, _ in pkgs]).items():
        name, version, via = pkgs[i]
        for v in found:
            key = (_cve_id(v), name, version)
            if key in seen:
                continue
            seen.add(key)
            out.append(Vuln(_cve_id(v), name, version, "npm" if not via else "npm-via", _osv_fixed(v, "npm", name),
                            _when(v.get("published", "")), _osv_severity(v), str(v.get("summary") or "")[:120], via or name))
    return out


def crate_vulns(pins: dict[str, str]) -> list[Vuln]:
    wanted = [(dep, CRATES[dep], pins[dep]) for dep in CRATES if dep in pins]
    out = []
    for i, found in osv([("crates.io", crate, v) for _, crate, v in wanted]).items():
        dep, crate, version = wanted[i]
        for v in found:
            out.append(Vuln(_cve_id(v), dep, version, "pin", _osv_fixed(v, "crates.io", crate), _when(v.get("published", "")),
                            _osv_severity(v), str(v.get("summary") or "")[:120], dep))
    return out


# ---------------------------------------------------------------- the image's tools: NVD by CPE
def _nvd_fixed(cve: dict, vendor: str, product: str) -> str:
    for conf in cve.get("configurations") or []:
        for node in conf.get("nodes") or []:
            for m in node.get("cpeMatch") or []:
                if m.get("vulnerable") and f":{vendor}:{product}:" in m.get("criteria", "") and m.get("versionEndExcluding"):
                    return str(m["versionEndExcluding"])
    return ""


def _nvd_severity(cve: dict) -> str:
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        for m in (cve.get("metrics") or {}).get(key) or []:
            sev = (m.get("cvssData") or {}).get("baseSeverity") or m.get("baseSeverity")
            if sev:
                return str(sev).upper()
    return ""


def tagged_at(dep: str, version: str) -> float:
    """When ``dep``'s release ``version`` was made: its tag's commit (TAGS). 0 when it can't be told."""
    if dep not in TAGS:
        return 0
    repo, tag = TAGS[dep]
    try:
        got = get_json(f"https://api.github.com/repos/{repo}/commits/{tag.format(version=version)}")
    except (OSError, ValueError):
        return 0
    return _when(((got.get("commit") or {}).get("committer") or {}).get("date", ""))


def nvd_vulns(pins: dict[str, str], pause: float | None = None) -> list[Vuln]:
    """NVD's CVEs for each image tool at its version (pins: upkeep's dependency → the version the image has). A CVE fixed
    in upstream's git on a date (DATED: NVD names no release) is left out when our release was made a day or more after
    it: NVD can't compare a date with a release number, so it matches every release, those with the fix too."""
    key = os.environ.get("NVD_API_KEY", "")
    pause = (0.7 if key else 6.5) if pause is None else pause   # (NVD: 5 requests in 30 s without a key, 50 with one)
    out = []
    made: dict[str, float] = {}   # (each tool's release: when it was made, looked up once, only when needed)
    for n, (dep, (vendor, product)) in enumerate((d, c) for d, c in CPES.items() if d in pins):
        if n and pause:
            time.sleep(pause)
        version = pins[dep]
        got = get_json(f"https://services.nvd.nist.gov/rest/json/cves/2.0?virtualMatchString=cpe:2.3:a:{vendor}:{product}:{version}",
                       headers={"apiKey": key} if key else None)
        for item in got.get("vulnerabilities") or []:
            cve = item.get("cve") or {}
            if cve.get("vulnStatus") == "Rejected":
                continue
            text = next((d.get("value", "") for d in cve.get("descriptions") or [] if d.get("lang") == "en"), "")
            fixed = _nvd_fixed(cve, vendor, product)
            if DATED.match(fixed):
                if dep not in made:
                    made[dep] = tagged_at(dep, version)
                if made[dep] and made[dep] >= _when(fixed) + DAY:   # (made after the fix: it has it)
                    continue
            out.append(Vuln(cve.get("id", "?"), dep, version, "pin", fixed, _when(cve.get("published", "")),
                            _nvd_severity(cve), text[:120], dep))
    return out


# ---------------------------------------------------------------- the published images: Trivy
BRANCH_IMAGES = {"main": "latest", "dev": "dev"}   # (the branch → the image build.yml publishes from it)


def images(where: str | None = None) -> list[str]:
    """The image the branch checked out here is published as (build.yml): latest on main, dev on dev — each branch's look
    is of its own image only — at the owner the Dockerfile's source label names. Both on another branch or a copy that
    isn't a checkout (``where`` None: the branch looked up here)."""
    with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
        m = re.search(r'org\.opencontainers\.image\.source="https://github\.com/([^/"]+)/', fh.read())
    owner = m.group(1).lower() if m else ""
    if where is None:
        where = source_branch()
    tags = [BRANCH_IMAGES[where]] if where in BRANCH_IMAGES else list(BRANCH_IMAGES.values())
    return [f"ghcr.io/{owner}/plex-cache:{tag}" for tag in tags] if owner else []


def source() -> str:
    """The copy whose CVEs are looked at: the one tnsync is checking (TNSYNC_COPY: the folder for tnsync local, an export of
    the branch or tag for tnsync main, dev, release, beta), else this checkout."""
    return os.environ.get("TNSYNC_COPY") or ROOT


def source_branch() -> str:
    """The branch that copy is of: what tnsync was asked for (TNSYNC_MODE: main or release are main, dev or beta are dev),
    else (tnsync local, or no tnsync) the branch checked out here; "" when it's neither."""
    mode = os.environ.get("TNSYNC_MODE", "")
    if mode in ("main", "master", "release"):
        return "main"
    if mode in ("dev", "beta"):
        return "dev"
    from dev import upkeep
    return upkeep.branch() or ""


LOCAL_IMAGE = "plex-cache:cves"   # (the copy's image, built for its look: docker's, never pushed)
LOCAL_LABEL = "this copy's image"


BUILD_LOG = os.path.join(os.path.dirname(CACHE), "cves-build.log")   # (the copy's image's last build, as docker said it)


def build_local(src: str, status: Callable[[str], None] | None = None) -> tuple[str, str]:
    """The copy's image built here (LOCAL_IMAGE), as dev/future.py builds: BuildKit when Docker has it, the base image pulled
    (its newest patches, as the weekly rebuild takes them); its build cache makes it seconds when only the app changed (the
    first build compiles ffmpeg: minutes). ``status``: told each build step as it starts (its stage and command, as
    future.py's bar reads them), so a long build is seen going. (its image ID, "") built; ("", why not) otherwise."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from dev import future
    buildkit = future.has_buildkit()
    cmd = ["docker", "buildx", "build", "--load", "--progress=plain"] if buildkit else ["docker", "build"]
    tell = status or (lambda text: None)
    tell(f"building {LOCAL_LABEL}")
    try:
        os.makedirs(os.path.dirname(BUILD_LOG), exist_ok=True)
        with open(BUILD_LOG, "w", encoding="utf-8") as log:
            proc = subprocess.Popen([*cmd, "--pull", "-t", LOCAL_IMAGE, src], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", env=dict(os.environ, DOCKER_BUILDKIT="1" if buildkit else "0"))
            assert proc.stdout is not None
            lines: list[str] = []
            for raw in proc.stdout:
                log.write(raw)
                s = raw.rstrip()
                if s:
                    lines = [*lines[-39:], s]
                if m := future.STEP_BUILDKIT.match(s):
                    tell(f"building {LOCAL_LABEL} · {m.group(2)}: {m.group(3)[:50]}")
                elif m := future.STEP_LEGACY.match(s):
                    tell(f"building {LOCAL_LABEL} · step {m.group(1)}/{m.group(2)}: {m.group(3)[:50]}")
            rc = proc.wait(timeout=60)
        if rc != 0:
            if future.fetch_failed(lines):
                return "", f"a download for it failed; {BUILD_LOG}"
            return "", (lines[-1].strip()[:160] if lines else f"docker build: exit {rc}") + f"; {BUILD_LOG}"
        iid = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", LOCAL_IMAGE], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return "", "no docker here"
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", f"docker didn't answer: {e}"
    return (iid.stdout.strip(), "") if iid.returncode == 0 and iid.stdout.strip() else ("", "docker built it, but has no such image")


def copy_os() -> str:
    """The OS the copy's Dockerfile builds on, as Trivy names it: "alpine" for an Alpine base, else "debian"."""
    try:
        with open(os.path.join(source(), "Dockerfile"), encoding="utf-8") as fh:
            return "alpine" if re.search(r"(?m)^(FROM|ARG)\b.*alpine", fh.read()) else "debian"
    except OSError:
        return "debian"


def trivy_vulns(got: dict, image: str) -> list[Vuln]:
    built = _when(((got.get("Metadata") or {}).get("ImageConfig") or {}).get("created", ""))
    distro = str(((got.get("Metadata") or {}).get("OS") or {}).get("Family") or "").lower()
    out = []
    for res in got.get("Results") or []:
        kind = "debian" if res.get("Class") == "os-pkgs" else "base"
        for v in res.get("Vulnerabilities") or []:
            if v.get("Status") in ("not_affected", "end_of_life"):
                continue
            out.append(Vuln(v.get("VulnerabilityID", "?"), v.get("PkgName", "?"), v.get("InstalledVersion", ""), kind,
                            v.get("FixedVersion", "").split(",")[0].strip(), _when(v.get("PublishedDate", "")),
                            str(v.get("Severity", "")).upper(), str(v.get("Title") or "")[:120], image=image, built=built,
                            distro=distro if kind == "debian" else ""))
    return out


def scan(image: str, local: bool = False) -> list[Vuln]:
    """Trivy's look at an image: published (from its registry, not pulled), or one docker has here (``local``)."""
    sys.path.insert(0, ROOT)
    from dev import checks
    cmd, why = checks.command("trivy")
    if not cmd:
        raise OSError(why)
    got = checks.call([*cmd, "image", "--quiet", "--scanners", "vuln", "--format", "json", "--image-src", "docker" if local else "remote",
                          "--cache-dir", os.path.join(os.path.dirname(CACHE), "trivy"), image],
                         capture_output=True, text=True, timeout=900)
    if got.returncode != 0:
        lines = (got.stderr or got.stdout).strip().splitlines()
        tail = next((ln for ln in reversed(lines) if ln.strip()), f"exit {got.returncode}")
        if "DENIED" in tail or "UNAUTHORIZED" in tail or "denied" in tail:
            tail = "the registry wants a sign-in (docker login ghcr.io)"
        raise OSError(f"{image}: {tail[:160]}")
    return trivy_vulns(json.loads(got.stdout), image)


# ---------------------------------------------------------------- when upstream last made a release
def released_at(vulns: list[Vuln]) -> dict[str, float]:
    """When each package with a CVE that has no fix last had a release upstream (npm's registry, GitHub's releases): what
    tells "no fix yet" from "no fix coming". A package not found here is left out (the STALE rule still holds for it)."""
    out: dict[str, float] = {}
    for name in sorted({v.package for v in vulns if not v.fixed and v.kind in ("npm", "npm-via", "pin")}):
        try:
            if name in REPOS:
                got = get_json(f"https://api.github.com/repos/{REPOS[name]}/releases/latest")
                out[name] = _when(got.get("published_at", ""))
            elif name not in CPES:   # (an npm package)
                got = get_json(f"https://registry.npmjs.org/{name}")
                out[name] = _when((got.get("time") or {}).get((got.get("dist-tags") or {}).get("latest", ""), ""))
        except (OSError, ValueError, KeyError):
            continue
    return out


def mark_seen(vulns: list[Vuln], now: float | None = None) -> None:
    """Each image fix (debian, base) given when it was first seen missing (SEEN, kept from look to look; a fix no longer
    missing is dropped). With no record yet — the first look, or GitHub's, where nothing is kept — when the CVE came out."""
    now = time.time() if now is None else now
    try:
        with open(SEEN, encoding="utf-8") as fh:
            known: dict[str, float] | None = json.load(fh)
    except (OSError, ValueError):
        known = None
    kept: dict[str, float] = {}
    for v in vulns:
        if v.kind in ("debian", "base") and v.fixed:
            key = f"{v.id}|{v.package}|{v.fixed}"
            v.seen = kept[key] = (known or {}).get(key) or (now if known is not None else (v.published or now))
    try:
        os.makedirs(os.path.dirname(SEEN), exist_ok=True)
        with open(SEEN, "w", encoding="utf-8") as fh:
            json.dump(kept, fh)
    except OSError:
        pass


# ---------------------------------------------------------------- the look, once a day
def code_sig() -> str:
    """This file as it asks for the CVEs and reads the answers: part of the day's answers' key, so a change to the code
    asks again the same day, not a day later. DECISIONS isn't part of it (judged each time, never asked about)."""
    try:
        with open(os.path.abspath(__file__), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    text = re.sub(r"^DECISIONS:[^\n]*?\{\}$|^DECISIONS:.*?^\}$", "", text, count=1, flags=re.M | re.S)
    return hashlib.sha256(text.encode()).hexdigest()


PARTS = ("the npm packages (OSV)", "oxipng (OSV)", "the image's tools (NVD)")   # what look() asks, then each image (Trivy)


def parts(scan_images: list[str] | None = None) -> int:
    """How many steps look() takes (its ``step`` called once each): the parts asked, then the images Trivy looks at (with
    none named: the copy's image built, then looked at)."""
    return len(PARTS) + (len(scan_images) if scan_images is not None else 2)


def look(pins: dict[str, str], lock_path: str, package_json: str, scan_images: list[str] | None = None, local: bool = False,
         fresh: bool = False, saved: bool = False, step: Callable[[str], None] | None = None,
         status: Callable[[str], None] | None = None) -> tuple[list[Vuln], dict[str, float], list[str]]:
    """Every CVE known in what we run and build with — from the day's answers, when there are some for these versions — when
    each package with one unfixed last had a release, and what couldn't be looked at. ``scan_images``: the images Trivy
    looks at (``local``: ones docker has here); None: the copy's own image, built here (build_local), else the one its
    branch publishes (images(), from its registry, said in a note). ``saved``: the
    last look's answers only, however old; nothing asked. ``step``: told as each of its parts() is done (dev/upkeep.py's
    bar), all at once when the day's answers are used; ``status``: what it's doing meanwhile (the image's build step,
    Trivy at work), the count unmoved."""
    told = step or (lambda what: None)
    doing = status or (lambda what: None)
    steps = len(scan_images) if scan_images is not None else 2
    said: list[str] = []   # (what the look says of itself: not a part missing)
    built = ""
    if scan_images is None and not saved:
        built, why = build_local(source(), doing)
        told(f"{LOCAL_LABEL} built" if built else f"{LOCAL_LABEL} not built")
        if built:
            scan_images, local = [LOCAL_IMAGE], True
        else:
            scan_images, local = images(), False
            said.append(f"{LOCAL_LABEL} not built ({why}): the published {', '.join(scan_images) or 'image'} looked at instead")
    scan_images = scan_images or []
    try:
        with open(lock_path, "rb") as fh:
            lock_sig = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        lock_sig = ""   # (a copy with no npm lock)
    sig = json.dumps({"pins": pins, "lock": lock_sig, "images": scan_images, "local": local, "built": built, "code": code_sig()},
                     sort_keys=True)
    try:
        with open(CACHE, encoding="utf-8") as fh:
            saved_look = json.load(fh)
    except (OSError, ValueError):
        saved_look = {}
    if saved or (not fresh and saved_look.get("sig") == sig and time.time() - saved_look.get("at", 0) < DAY):
        for what in PARTS:
            told(f"CVEs in {what}: the day's answers")
        for _ in range(steps - (1 if built or said else 0)):
            told("CVEs in the image: the day's answers")
        if not saved_look.get("vulns") and saved_look.get("sig") is None:
            return [], {}, ["CVEs not looked up yet (python3 dev/upkeep.py does it)"]
        return ([Vuln(**v) for v in saved_look.get("vulns", [])], dict(saved_look.get("released", {})),
                list(saved_look.get("notes", [])))
    vulns: list[Vuln] = []
    notes: list[str] = []
    for what, fn in zip(PARTS, (lambda: npm_vulns(lock_path, package_json), lambda: crate_vulns(pins), lambda: nvd_vulns(pins)),
                        strict=True):
        doing(f"looking up CVEs in {what}")
        try:
            vulns += fn()
        except (OSError, ValueError, KeyError) as e:
            notes.append(f"{what} not checked: {getattr(e, 'reason', e)}")
        told(f"CVEs in {what}")
    for image in scan_images:
        doing(f"Trivy looking at {LOCAL_LABEL if built else image}")
        try:
            found = scan(image, local)
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            notes.append(f"image not checked: {e}")
            found = []
        for v in found if built else []:
            v.image = LOCAL_LABEL
        vulns += found
        if not built or image == scan_images[-1]:
            told(f"CVEs in {LOCAL_LABEL if built else image} (Trivy)")
    for _ in range(steps - (1 if built or said else 0) - (1 if built else len(scan_images))):
        told("CVEs in the image")   # (the bar's steps all told, however many images were looked at)
    released = released_at(vulns)
    mark_seen(vulns)
    if not notes:   # (a look with a part missing is asked again next time, not kept for the day)
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), "sig": sig, "vulns": [asdict(v) for v in vulns], "released": released,
                       "notes": said}, fh)
    return vulns, released, said + notes


# ---------------------------------------------------------------- what needs doing
def _days(seconds: float) -> int:
    return int(seconds // DAY)


def drops_pip() -> bool:
    """Whether the Dockerfile takes pip out of the image (its final stage): then the base image's Python packages (pip's
    own copies) leave with the image's next build."""
    try:
        with open(os.path.join(source(), "Dockerfile"), encoding="utf-8") as fh:
            return bool(re.search(r"pip uninstall\b.*\bpip\b", fh.read()))
    except OSError:
        return False


def judge(vulns: list[Vuln], plan: dict[str, tuple[str, str, str]], released: dict[str, float] | None = None,
          now: float | None = None) -> Judged:
    """What of ``vulns`` needs doing. ``plan``: upkeep's dependencies as it found them — name → (have, newest, what: current,
    move, report, behind, for dev or held); ``released``: when each package's newest release came out, where known (released_at)."""
    now = time.time() if now is None else now
    released = released or {}
    out = Judged()
    seen: set[tuple[str, str]] = set()
    image_fixes: dict[tuple[str, str, str, str], list[Vuln]] = {}   # (kind, package, version, image) → its CVEs: one line each
    for v in sorted(vulns, key=lambda v: (v.package, v.id)):
        if (v.id, v.package) in seen:   # (the same in both images, or OSV and NVD both)
            continue
        seen.add((v.id, v.package))
        dated = v.kind == "pin" and bool(DATED.match(v.fixed))   # (fixed in upstream's git on a date, no release named)
        fixed = parse(v.fixed) if v.fixed and not dated else ()
        decision = DECISIONS.get(v.id)
        if decision:
            since = _when(decision[1])
            if now - since < REVIEW * DAY and (decision[0] == "not reachable" or not fixed):   # (a fix since a no-fix choice: as any fix)
                out.decided += 1
                continue
        where = f"{v.package} {v.version}"
        if v.kind in ("debian", "base"):
            if not v.fixed:
                out.debian += 1 if v.kind == "debian" else 0
                out.quiet += 0 if v.kind == "debian" else 1
                continue
            since = v.seen or v.published
            if now - since < DEBIAN_GRACE * DAY:   # (a fix this new: the weekly rebuild takes it)
                out.quiet += 1
                continue
            image_fixes.setdefault((v.kind, v.package, v.version, v.image), []).append(v)
            continue
        if dated:   # (nvd_vulns left it in: our release is older than the fix, or couldn't be dated)
            out.act.append((f"{v.id} in {where}", f"Fixed in {v.package}'s git on {v.fixed} (NVD names no release), and "
                                                  f"{v.version} is older or couldn't be dated: check whether it has the fix "
                                                  "(a newer release, if not); or record the choice in DECISIONS (dev/cves.py)."))
            continue
        dep = v.dep
        have, newest, state = plan.get(dep, ("", "", ""))
        if fixed and newest and parse(newest) >= fixed and v.kind != "npm-via":
            if state in ("move", "report"):
                out.handled.setdefault(dep, []).append(v.id)
            elif state == "held":
                out.act.append((f"{v.id} in {where}", f"Fixed in {v.fixed}, but {dep} is held at {have} by choice (upkeep's HOLD): "
                                                      "weigh the hold against it."))
            else:   # (behind or for dev: a major release, or one upkeep can't move)
                out.act.append((f"{v.id} in {where}", f"Fixed in {v.fixed}: {dep} {newest} is a major release, yours to move "
                                                      "(it fixes this)."))
            continue
        age = _days(now - v.published) if v.published else 0
        if v.kind == "npm-via" and fixed:
            if age < STALE:
                out.quiet += 1
                continue
            out.act.append((f"{v.id} in {where} (pulled in by {dep})",
                            f"Fixed in {v.fixed} {age} days ago, but {dep} still asks for {v.version}: an npm override "
                            f"(\"overrides\" in dev/package.json), a newer {dep.split(', ')[0]}, or replacing it."))
            continue
        quiet_for = _days(now - released[v.package]) if released.get(v.package) else 0
        if age < STALE and quiet_for < DORMANT:
            out.quiet += 1
            continue
        why = f"no fix after {age} days" if age >= STALE else f"no fix, and no release of {v.package} in {quiet_for} days"
        out.act.append((f"{v.id} in {where}" + (f" (pulled in by {dep})" if v.kind == "npm-via" else ""),
                        f"No fix coming ({why}): replace it, patch it ourselves, or move to a maintained fork; then record "
                        "the choice in DECISIONS (dev/cves.py)."))
    out.shown = len(out.act)
    no_pip = drops_pip()
    own_os = copy_os()
    for (kind, package, version, image), found in image_fixes.items():
        ids = ", ".join(v.id for v in found)
        fixed = max((v.fixed for v in found), key=lambda f: parse(f) or (0,))
        days = _days(now - min(v.seen or v.published for v in found))
        built = found[0].built
        distro = found[0].distro or "debian"
        if kind == "debian" and distro != own_os and image != LOCAL_LABEL:   # (a published image, from an older Dockerfile on another OS)
            out.waiting.append((f"{ids} in {package} {version} ({distro.title()}, in {image})",
                                f"Fixed in {fixed}. A {distro.title()} image, built from an older Dockerfile than this "
                                f"copy's ({own_os.title()}): its next build from this one (for latest, the next release) replaces it."))
            out.waited += len(found)
            continue
        out.shown += 0 if kind == "base" and no_pip else len(found)
        if kind == "debian":
            todo = (f"Fixed in {fixed}, but the image still has {version} {days} days on: "
                    + (f"it was last built {_days(now - built)} days ago, so rebuild it (build.yml, workflow_dispatch); "
                       if built and now - built >= DEBIAN_GRACE * DAY else "the base image hasn't taken it; ")
                    + (f"an apk upgrade {package}" if distro == "alpine" else f"an apt-get install --only-upgrade {package}")
                    + " in the Dockerfile's final stage takes it now.")
            out.act.append((f"{ids} in {package} {version} ({distro.title()}, in {image})", todo))
        elif no_pip:
            out.waiting.append((f"{ids} in {package} {version} (the base image's Python packages, in {image})",
                                f"Fixed in {fixed}. pip's own copy: the Dockerfile now takes pip out, so the image's next "
                                "build (for latest, the next release) drops it."))
            out.waited += len(found)
        else:
            out.act.append((f"{ids} in {package} {version} (the base image's Python packages, in {image})",
                            f"Fixed in {fixed}, but the base image still ships {version} {days} days on (pip's own copy, when "
                            "the image installs nothing with pip): remove pip from the Dockerfile's final stage, or upgrade "
                            "it there; or record the choice in DECISIONS (dev/cves.py)."))
    return out

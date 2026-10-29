#!/usr/bin/env python3
"""Does the TrueNAS that's coming still have every call Plex Cache makes?

TrueNAS renames API methods between releases (zfs.snapshot.* became pool.snapshot.* in 25.04,
zfs.dataset.* went in 25.10). The app keeps each call's names over the versions in
plexcache/truenas.py METHODS, and checks them against the running TrueNAS when it connects.
This script looks ahead: it reads the middleware's source on GitHub (the branch that becomes the
next release) and says, for every call, which of its names is still declared there — and fails
when a call has none left, so the rename shows up weeks before the release does. The weekly
workflow (.github/workflows/truenas-api.yml) runs it; by hand:

    python3 dev/truenas_api.py                 # clones the middleware's master branch (a sparse, shallow clone: a few MB)
    python3 dev/truenas_api.py --ref stable/goldeye
    python3 dev/truenas_api.py --checkout /path/to/middleware

How a method is found: a service class in middlewared/ (plugins/*.py and main.py) — its namespace
from ``namespace = '…'`` in its Config, else its class name without "Service", lowercased — and in
it ``def name`` or ``async def name`` (``do_create``/``do_update``/``do_delete`` stand for
create/update/delete, as the middleware maps them; CRUD services have ``query`` by inheritance).
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from plexcache.truenas import METHODS  # noqa: E402

REPO = "https://github.com/truenas/middleware.git"
PACKAGE = "src/middlewared/middlewared"
_CLASS = re.compile(r"^class (\w+)\(([^)]*)\):", re.M)
_TOP = re.compile(r"^(?:class|def|async def) ", re.M)  # (where a class's body ends: the next thing at the top level)
_NAMESPACE = re.compile(r"^\s+namespace\s*=\s*['\"]([\w.]+)['\"]", re.M)
_DEF = re.compile(r"^\s+(?:async\s+)?def (\w+)\(", re.M)
_DO = {"do_create": "create", "do_update": "update", "do_delete": "delete"}


def services(root: str) -> dict[str, set[str]]:
    """Every service namespace declared under ``root`` (the middlewared package) and its methods."""
    out: dict[str, set[str]] = {}
    for base, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            try:
                with open(os.path.join(base, fn), encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
            except OSError:
                continue
            heads = list(_CLASS.finditer(src))
            for m in heads:
                name, bases = m.group(1), m.group(2)
                if "Service" not in bases and "Service" not in name:
                    continue
                nxt = _TOP.search(src, m.end())
                body = src[m.end():nxt.start() if nxt else len(src)]
                ns = _NAMESPACE.search(body)
                namespace = ns.group(1) if ns else re.sub(r"Service.*$", "", name).lower()
                methods = {str(_DO.get(d, d)) for d in _DEF.findall(body)}
                if "CRUDService" in bases or "ConfigService" in bases:
                    methods |= {"query"} if "CRUDService" in bases else {"config", "update"}
                out.setdefault(namespace, set()).update(methods)
    return out


def check(root: str) -> tuple[dict[str, list[str]], dict[str, tuple[str, ...]]]:
    """({what: the names declared}, {what: names} for every call with none left)."""
    have = services(root)
    found = {what: [n for n in names if n.rsplit(".", 1)[-1] in have.get(n.rsplit(".", 1)[0], ())] for what, names in METHODS.items()}
    return found, {what: METHODS[what] for what, names in found.items() if not names}


def clone(ref: str) -> str:
    """A sparse, shallow clone of the middleware at ``ref``: the middlewared package only."""
    d = tempfile.mkdtemp(prefix="truenas-middleware-")
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", "--branch", ref, REPO, d], check=True)
    subprocess.run(["git", "-C", d, "sparse-checkout", "set", PACKAGE], check=True)
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--ref", default="master", help="the middleware branch or tag to read (default: master, the next release)")
    ap.add_argument("--checkout", help="an existing checkout of truenas/middleware, instead of cloning")
    args = ap.parse_args()
    root = args.checkout or clone(args.ref)
    pkg = os.path.join(root, PACKAGE)
    if not os.path.isdir(pkg):
        print(f"not a middleware checkout: {root} ({PACKAGE} missing)", file=sys.stderr)
        return 2
    found, missing = check(pkg)
    label = args.checkout or f"{REPO} @ {args.ref}"
    print(f"Plex Cache's TrueNAS calls against {label}:")
    for what, names in METHODS.items():
        ok = found[what]
        gone = [n for n in names if n not in ok]
        mark = "ok " if ok else "GONE"
        print(f"  {mark}  {what}: " + ", ".join(ok) + (("  (no longer: " + ", ".join(gone) + ")") if gone and ok else ""))
    if missing:
        print(f"\n{len(missing)} call(s) with no name left — TrueNAS renamed or removed them; add the new name to plexcache/truenas.py METHODS "
              "(and the code that calls it):")
        for what, names in missing.items():
            print(f"  {what}: tried " + ", ".join(names))
        return 1
    print("\nEvery call has a name the next TrueNAS declares.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

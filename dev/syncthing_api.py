#!/usr/bin/env python3
"""Does the Syncthing that's coming still have every REST call Dionysus makes?

Syncthing's REST API moves between releases (paths are added, a few retired). The app keeps the calls it makes in
plexcache/syncthing.py CALLS, and tries each against the running Syncthing when it connects (the API compatibility
light). This script looks ahead: it reads Syncthing's source on GitHub (its main branch, which becomes the next release,
or a release tag) and says, for every call, whether its path is still declared there — and fails when one isn't, so a
change shows up before the release reaches anyone's NAS. The weekly workflow (.github/workflows/truenas-api.yml) runs
it; by hand:

    python3 dev/syncthing_api.py                 # clones syncthing's main branch (a sparse, shallow clone: a few MB)
    python3 dev/syncthing_api.py --ref v2.0.0
    python3 dev/syncthing_api.py --checkout /path/to/syncthing

How a path is found: every "/rest/…" string literal in lib/api (api.go registers the handlers; confighandler.go the
/rest/config ones). A call's "{id}" stands for Syncthing's ":id" (or a path the handler takes the id after), so a call
is declared when its path is there as written, with ":id", or without its last {id} part.

The picture too: Syncthing's identicon (gui/default/syncthing/core/identiconDirective.js) is drawn here for every
identity (syncthing.st_cells, app.js stIdenticon). Its source is run in node for a few IDs and the cells compared, so a
change in how Syncthing draws it shows up here before the pictures stop matching.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from plexcache.syncthing import CALLS  # noqa: E402

REPO = "https://github.com/syncthing/syncthing.git"
PACKAGE = "lib/api"
IDENTICON = "gui/default/syncthing/core/identiconDirective.js"
SAMPLE_IDS = ("GAEALE5-IUWJBCI-C7KNKOW-UXWTUAW-TXK7N5T-HNSRXRJ-LBHZHF3-XSEEAAA", "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH",
              "225HLGU-OSGTLOK-XWXPS6G-A4D5HST-DWMKTEU-VVDU2CQ-C5BO66C-PU2UUAF", "7VAC7BE-YA7V4EL-AYF6DKX-A2FV33U-4HUP4VQ-ASOWVFZ-2ONHXSJ-YWLB7Q5")
# a stand-in for the browser around the directive: angular's module and directive, a document whose rects are collected
NODE_HARNESS = """
const fs = require("fs"); const src = fs.readFileSync(process.argv[2], "utf-8"); const ids = JSON.parse(process.argv[3]);
let factory = null;
const angular = { module() { return { directive(name, arr) { factory = arr[arr.length - 1]; return this; } }; } };
const document = { createElementNS(ns, tag) { return { tag, attrs: {}, children: [], setAttribute(k, v) { this.attrs[k] = v; }, appendChild(c) { this.children.push(c); } }; } };
const $window = { parseInt };
new Function("angular", "document", src)(angular, document);
const directive = factory($window);
const out = {};
for (const id of ids) {
  let svg = null;
  directive.link({ value: id }, { append(el) { svg = el; } }, {});
  out[id] = svg.children.map(r => [Math.round(parseFloat(r.attrs.y) / 20), Math.round(parseFloat(r.attrs.x) / 20)]);
}
process.stdout.write(JSON.stringify(out));
"""
LITERAL = re.compile(r'"(/rest/[A-Za-z0-9_/.:{}-]*)"')


def paths(root: str) -> set[str]:
    """Every /rest/… literal in the package's Go sources."""
    out: set[str] = set()
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if not f.endswith(".go") or f.endswith("_test.go"):
                continue
            try:
                with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                    out.update(LITERAL.findall(fh.read()))
            except OSError:
                continue
    return out


def declared(path: str, have: set[str]) -> bool:
    """Whether a call's path is declared: as written, with ":id" for "{id}", or (an id taken after it) without that part."""
    if path in have or path.replace("{id}", ":id") in have:
        return True
    return path.endswith("/{id}") and path[: -len("/{id}")] in have


def check(root: str) -> tuple[dict[str, bool], list[str]]:
    """Per call, whether its path is declared — and the calls whose path isn't."""
    have = paths(root)
    found = {what: declared(path, have) for what, (_method, path) in CALLS.items()}
    return found, [what for what, ok in found.items() if not ok]


def identicon_cells(source_path: str, ids=SAMPLE_IDS) -> dict[str, set[tuple[int, int]]]:
    """The cells Syncthing's own directive fills for each ID, by running its source in node (None where node is missing)."""
    import json
    import shutil
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is needed to run Syncthing's identicon source")
    harness = os.path.join(tempfile.mkdtemp(prefix="identicon-"), "run.js")
    with open(harness, "w", encoding="utf-8") as fh:
        fh.write(NODE_HARNESS)
    got = subprocess.run([node, harness, source_path, json.dumps(list(ids))], capture_output=True, text=True, check=True)
    return {k: {tuple(c) for c in v} for k, v in json.loads(got.stdout).items()}


def check_identicon(root: str) -> list[str]:
    """The IDs whose picture Syncthing's source draws differently from ours (syncthing.st_cells): none, or the mismatch."""
    from plexcache.syncthing import st_cells
    path = os.path.join(root, IDENTICON)
    if not os.path.exists(path):
        return ["(no identicon source at %s)" % IDENTICON]
    theirs = identicon_cells(path)
    return [i for i in SAMPLE_IDS if set(st_cells(i)) != theirs.get(i, set())]


def clone(ref: str) -> str:
    """A sparse, shallow clone of Syncthing at ``ref``: the API package and the identicon directive."""
    d = tempfile.mkdtemp(prefix="syncthing-")
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", "--branch", ref, REPO, d], check=True)
    subprocess.run(["git", "-C", d, "sparse-checkout", "set", PACKAGE, os.path.dirname(IDENTICON)], check=True)
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--ref", default="main", help="the Syncthing branch or tag to read (default: main, the next release)")
    ap.add_argument("--checkout", help="an existing checkout of syncthing/syncthing, instead of cloning")
    args = ap.parse_args()
    root = args.checkout or clone(args.ref)
    pkg = os.path.join(root, PACKAGE)
    if not os.path.isdir(pkg):
        print(f"not a Syncthing checkout: {root} ({PACKAGE} missing)", file=sys.stderr)
        return 2
    found, missing = check(pkg)
    label = args.checkout or f"{REPO} @ {args.ref}"
    print(f"Dionysus's Syncthing calls against {label}:")
    for what, (method, path) in CALLS.items():
        print(f"  {'ok ' if found[what] else 'GONE'}  {what}: {method} {path}")
    bad = 0
    if missing:
        print(f"\n{len(missing)} call(s) whose path isn't declared — Syncthing moved or removed them; change plexcache/syncthing.py CALLS and the client:")
        for what in missing:
            print(f"  {what}: {CALLS[what][0]} {CALLS[what][1]}")
        bad = 1
    try:
        differ = check_identicon(root)
    except Exception as e:  # noqa: BLE001
        print(f"\nThe identicon couldn't be compared: {e}")
        differ = []
    if differ:
        print(f"\nSyncthing draws the identicon differently now for {len(differ)} of {len(SAMPLE_IDS)} sample IDs: change syncthing.st_cells and app.js stIdenticon to its rule ({IDENTICON}).")
        bad = 1
    elif not missing:
        print("\nEvery call's path is declared by the next Syncthing, and its identicon is drawn as ours is.")
    return bad


if __name__ == "__main__":
    sys.exit(main())

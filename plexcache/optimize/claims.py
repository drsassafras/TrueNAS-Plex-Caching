"""Claims: so two systems sharing a library (Syncthing, two ways) don't optimize the same file.

Before optimizing a file, a system claims it: a small file of its own in the library folder,
.plexcache/claims/<key>.<instance>.json (the key comes from the file's path in the library, the
same on every system). Each system only ever writes its own claim files, so the claims themselves
never conflict. The earliest claim on a file wins; the other systems leave it alone. Every claim
also marks its system as seen in that folder (.plexcache/systems/<instance>.json): a system that
sees no other one there doesn't wait for claims to arrive.

Claims are released when the file is done, and any left behind (a system stopped mid-way) are
ignored after STALE and tidied away.
"""

from __future__ import annotations

import json
import os
import re
import time

STALE = 6 * 3600         # a claim older than this is ignored (and removed)
PEER_DAYS = 30           # another system counts as sharing the folder if seen this recently
_ID = re.compile(r"[A-Za-z0-9_-]{4,64}")


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def claim(folder: str, key: str, instance: str, release: bool = False, now: float | None = None) -> dict:
    """Claim (or release) ``key`` in a library folder for ``instance``. {"claims": [{"instance",
    "at"}] — every live claim on the key, earliest first —, "peers": [other instances seen]}."""
    if not _ID.fullmatch(key) or not _ID.fullmatch(instance):
        raise ValueError("bad key or instance")
    now = now or time.time()
    base = os.path.join(folder, ".plexcache")
    mine = os.path.join(base, "claims", f"{key}.{instance}.json")
    if release:
        try:
            os.remove(mine)
        except FileNotFoundError:
            pass
    elif not os.path.exists(mine):
        _write(mine, {"instance": instance, "at": now})
    seen = os.path.join(base, "systems", f"{instance}.json")
    try:
        fresh = now - os.stat(seen).st_mtime < 3600
    except OSError:
        fresh = False
    if not fresh:  # at most once an hour: it's synced to the other systems too
        _write(seen, {"instance": instance, "at": now})
    claims = []
    cdir = os.path.join(base, "claims")
    for name in os.listdir(cdir) if os.path.isdir(cdir) else []:
        if not (name.startswith(key + ".") and name.endswith(".json")):
            continue
        p = os.path.join(cdir, name)
        try:
            with open(p, encoding="utf-8") as fh:
                c = json.load(fh)
        except (OSError, ValueError):
            continue
        if now - float(c.get("at") or 0) > STALE:
            try:
                os.remove(p)  # left behind by a system that stopped mid-way
            except OSError:
                pass
            continue
        claims.append({"instance": str(c.get("instance")), "at": float(c.get("at") or 0)})
    claims.sort(key=lambda c: (c["at"], c["instance"]))
    peers = []
    sdir = os.path.join(base, "systems")
    for name in os.listdir(sdir) if os.path.isdir(sdir) else []:
        other = name[:-5] if name.endswith(".json") else None
        if other and other != instance:
            try:
                if now - os.stat(os.path.join(sdir, name)).st_mtime < PEER_DAYS * 86400:
                    peers.append(other)
            except OSError:
                pass
    return {"claims": claims, "peers": sorted(peers)}

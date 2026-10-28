"""Where each Plex library folder can be found inside this app.

Plex reports library folders as it sees them inside its own container
(e.g. ``/data/Movies``). This app may see the same files somewhere else, or
not at all. For each library folder we work out:

* where we look for it here (after any mapping the user chose),
* whether real files from that library can be read there, and
* if not, which folders mounted in this app *do* contain those files, so the
  user can just pick the right one from a list.
"""

import os

from . import zfs
from .i18n import M

# Mounts that are part of the container itself, never media.
SYSTEM_MOUNTS = ("/", "/config", "/app", "/proc", "/sys", "/dev", "/etc", "/run", "/tmp", "/var", "/usr", "/lib",
                 "/bin", "/sbin", "/root", "/home")
SKIP_FSTYPES = {"proc", "sysfs", "tmpfs", "devpts", "mqueue", "cgroup", "cgroup2", "devtmpfs", "overlay", "shm",
                "securityfs", "debugfs", "tracefs", "pstore", "bpf", "fusectl", "configfs", "nsfs"}
MAX_CANDIDATES = 400
CANDIDATE_DEPTH = 2


def media_mounts(mount_list: list[dict] | None = None) -> list[str]:
    """Folders mounted into this app that could hold media."""
    out = []
    for m in mount_list if mount_list is not None else zfs.mounts():
        mp = m["mountpoint"]
        if m["fstype"] in SKIP_FSTYPES:
            continue
        if any(mp == s or mp.startswith(s + "/") for s in SYSTEM_MOUNTS if s != "/") or mp == "/":
            # /etc/hosts, /etc/resolv.conf etc. are bind mounts too; skip them.
            continue
        if os.path.isdir(mp):
            out.append(mp)
    return sorted(set(out))


def candidate_folders(mounts: list[str], depth: int = CANDIDATE_DEPTH) -> list[str]:
    """Each media mount plus the folders up to ``depth`` levels inside it."""
    out: list[str] = []
    for root in mounts:
        level = [root]
        out.append(root)
        for _ in range(depth):
            nxt = []
            for d in level:
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "@")):
                                nxt.append(e.path)
                except OSError:
                    continue
            nxt.sort()
            out.extend(nxt)
            level = nxt
            if len(out) >= MAX_CANDIDATES:
                return out[:MAX_CANDIDATES]
    return out


def _readable(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False


def _is_empty_dir(path: str) -> bool:
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return False


def location_report(app, mount_list: list[dict] | None = None) -> list[dict]:
    """One entry per library folder Plex uses (excluded libraries skipped)."""
    candidates: list[str] | None = None
    rows = []
    for lib in app.libraries:
        if not lib["kind"]:
            continue
        samples_by_loc = lib.get("samples", {})
        for loc in lib["locations"]:
            loc = loc.rstrip("/") or "/"
            local = app.map_path(loc)
            samples = samples_by_loc.get(loc, [])
            rels = [p[len(loc):].lstrip("/") for p in samples]
            row = {"library": lib["title"], "key": lib["key"], "plex_path": loc, "local_path": local,
                   "mapped": local != loc, "samples": len(samples), "excluded": lib["excluded"]}
            if lib["excluded"]:
                # Not scanned, so only say whether the folder is there.
                there = os.path.isdir(local)
                row.update(status="ok" if there else "missing",
                           detail=M("Not cached (unticked)") if there else M("{path} isn’t mounted in this app", path=local))
                rows.append(row)
                continue
            if not os.path.isdir(local):
                row.update(status="missing", detail=M("{path} isn’t mounted in this app", path=local))
            elif rels and any(_readable(os.path.join(local, r)) for r in rels):
                row.update(status="ok", detail=M("Files readable"))
            elif rels and _is_empty_dir(local):
                row.update(status="empty", detail=M("{path} is mounted but empty", path=local))
            elif rels:
                row.update(status="unreadable",
                           detail=M("{path} exists but this library’s files aren’t in it, or can’t be read", path=local))
            else:
                row.update(status="ok", detail=M("Mounted (no files in this folder yet)"))
            if row["status"] != "ok":
                if candidates is None:
                    candidates = candidate_folders(media_mounts(mount_list))
                found = [c for c in candidates if c != local and rels
                         and any(os.path.isfile(os.path.join(c, r)) for r in rels)]
                row["found_at"] = found[:5]
            rows.append(row)
    return rows


def choices(rows: list[dict], mount_list: list[dict] | None = None) -> list[str]:
    """Folders to offer in the mapping dropdowns."""
    if not rows:
        return []
    return candidate_folders(media_mounts(mount_list))

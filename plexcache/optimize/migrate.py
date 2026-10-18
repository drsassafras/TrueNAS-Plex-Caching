"""Turning a library folder into a child dataset: the optimizer's part (the app drives it).

The folder is copied into a new, empty dataset mounted next to it (``<name>-new``), file by file,
each one checked (its SHA-256 read back after writing). The copy can be paused, stopped and
started again: a file already there with the same size and modification time is skipped, and a
file is only given its name once it's complete, so a copy cut short leaves nothing half-written
under a real name. A last pass with ``mirror`` also removes what was deleted from the folder
meanwhile. Then the old folder is renamed out of the way (``.<name>.plexcache-old``), the dataset
takes its name, and once everything's checked, the old folder is deleted.

Only those two renames and that one deletion are possible: the names are checked, so this can't
rename or delete anything else.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import threading

OLD = re.compile(r"^\.(?P<name>.+)\.plexcache-old$")  # where the old folder goes until it's deleted
PART = ".plexcache-part"  # a file being copied, until it's complete
CHUNK = 4 * 1024 * 1024


class Paused:
    """Pause/stop controls for a running copy (set from the service's endpoints)."""

    def __init__(self):
        self.go = threading.Event()
        self.go.set()
        self.stop = threading.Event()

    def wait(self) -> bool:
        """Block while paused; False when stopped."""
        while not self.go.wait(1):
            if self.stop.is_set():
                return False
        return not self.stop.is_set()


def _copy_meta(st: os.stat_result, dst: str, follow: bool = True) -> bool:
    """Owner, permissions, extended attributes and times on the copy. False when the owner couldn't
    be kept (the optimizer can't change owners)."""
    kept = True
    try:
        os.chown(dst, st.st_uid, st.st_gid, follow_symlinks=follow)
    except PermissionError:
        kept = (os.lstat(dst).st_uid, os.lstat(dst).st_gid) == (st.st_uid, st.st_gid)
    if follow and stat.S_IMODE(os.stat(dst).st_mode) != stat.S_IMODE(st.st_mode):
        try:
            os.chmod(dst, stat.S_IMODE(st.st_mode))
        except PermissionError:
            # an ACL dataset (aclmode=restricted) refuses chmod on a file with a full ACL: the ACL,
            # copied with the extended attributes first, sets its permissions
            if not _has_acl(dst):
                raise
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=follow)
    return kept


def _has_acl(path: str) -> bool:
    try:
        return "system.nfs4_acl_xdr" in os.listxattr(path)
    except (OSError, AttributeError):
        return False


def _copy_xattrs(src: str, dst: str) -> None:
    if not hasattr(os, "listxattr"):
        return
    try:
        for name in os.listxattr(src, follow_symlinks=False):
            try:
                os.setxattr(dst, name, os.getxattr(src, name, follow_symlinks=False), follow_symlinks=False)
            except OSError as e:
                if e.errno not in (errno.EPERM, errno.ENOTSUP, errno.EACCES):
                    raise
    except OSError as e:
        if e.errno not in (errno.ENOTSUP, errno.EPERM, errno.EACCES):
            raise


def _copy_file(src: str, dst: str, st: os.stat_result, ctl: Paused, progress) -> None:
    """One file, checked: written to <dst>.plexcache-part, read back and compared, then renamed."""
    part = dst + PART
    h = hashlib.sha256()
    with open(src, "rb") as fi, open(part, "wb") as fo:
        while True:
            if not ctl.wait():
                fo.close()
                os.remove(part)
                raise InterruptedError("stopped")
            chunk = fi.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            fo.write(chunk)
            progress(len(chunk))
        fo.flush()
        os.fsync(fo.fileno())
    check = hashlib.sha256()
    with open(part, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            check.update(chunk)
    if check.hexdigest() != h.hexdigest():
        os.remove(part)
        raise OSError(f"the copy of {os.path.basename(src)} differs from the original")
    _copy_xattrs(src, part)
    _copy_meta(st, part)
    os.replace(part, dst)


def copy_tree(src: str, dst: str, ctl: Paused, report=lambda **kw: None, mirror: bool = False) -> dict:
    """Copy the folder ``src`` into the (empty, or partly copied) folder ``dst``: every folder, file
    and symbolic link, with owners, permissions, times and extended attributes. Files already in
    ``dst`` with the same size and modification time are left alone (a copy picked up again).
    ``mirror``: also remove from ``dst`` what isn't in ``src`` any more (the last pass before the
    switch). ``report(**counts)`` is called as it goes."""
    if not os.path.isdir(src) or not os.path.isdir(dst):
        raise OSError("both folders must exist")
    total_files = total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(src):
        for n in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, n))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total_files += 1
                total_bytes += st.st_size
    c = {"files_total": total_files, "bytes_total": total_bytes, "files_done": 0, "bytes_done": 0,
         "copied": 0, "skipped": 0, "removed": 0, "owners_not_kept": 0, "current": None}
    report(**c)

    def moved(n):
        c["bytes_done"] += n
        report(**c)

    dirs = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        rel = os.path.relpath(dirpath, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        dirs.append((dirpath, target))
        for n in sorted(filenames):
            if not ctl.wait():
                raise InterruptedError("stopped")
            sp, dp = os.path.join(dirpath, n), os.path.join(target, n)
            st = os.lstat(sp)
            if stat.S_ISLNK(st.st_mode):
                if not os.path.lexists(dp):
                    os.symlink(os.readlink(sp), dp)
                    _copy_meta(st, dp, follow=False)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue  # sockets, devices: not media
            c["current"] = os.path.join(rel, n) if rel != "." else n
            try:
                ds = os.stat(dp)
                same = (ds.st_size, ds.st_mtime_ns) == (st.st_size, st.st_mtime_ns)
            except FileNotFoundError:
                same = False
            if same:
                c["skipped"] += 1
                c["bytes_done"] += st.st_size
            else:
                _copy_file(sp, dp, st, ctl, moved)
                c["copied"] += 1
                if (os.stat(dp).st_uid, os.stat(dp).st_gid) != (st.st_uid, st.st_gid):
                    c["owners_not_kept"] += 1
            c["files_done"] += 1
            report(**c)
    if mirror:
        for dirpath, dirnames, filenames in os.walk(dst, topdown=False):
            rel = os.path.relpath(dirpath, dst)
            source = src if rel == "." else os.path.join(src, rel)
            for n in filenames:
                if not os.path.lexists(os.path.join(source, n)) or n.endswith(PART):
                    os.remove(os.path.join(dirpath, n))
                    c["removed"] += 1
            if rel != "." and not os.path.isdir(source):
                shutil.rmtree(dirpath, ignore_errors=True)
                c["removed"] += 1
    for sp, dp in reversed(dirs):  # folders last, so their times aren't changed by what's copied into them
        st = os.lstat(sp)
        _copy_xattrs(sp, dp)
        if not _copy_meta(st, dp):
            c["owners_not_kept"] += 1
    c["current"] = None
    report(**c)
    return dict(c, result="copied")


def compare(src: str, dst: str) -> dict:
    """Both folders, file by file (names, sizes, modification times): {"same", "files", "bytes",
    "missing": [...], "different": [...], "extra": [...]} (up to 20 of each)."""
    def listing(root):
        out = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for n in filenames:
                if n.endswith(PART):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
        return out
    a, b = listing(src), listing(dst)
    missing = sorted(set(a) - set(b))
    extra = sorted(set(b) - set(a))
    different = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    return {"same": not (missing or extra or different), "files": len(a), "bytes": sum(s for s, _ in a.values()),
            "missing": missing[:20], "different": different[:20], "extra": extra[:20]}


def probe(path: str) -> dict:
    """What the wizard needs to know about a folder: there, a folder, writable, its own mount (a
    dataset), its filesystem's block size (a dataset's record size), owner and mode."""
    out = {"path": path, "exists": os.path.exists(path)}
    if not out["exists"]:
        return out
    st = os.stat(path)
    out.update(is_dir=os.path.isdir(path), writable=os.access(path, os.W_OK), is_mount=os.path.ismount(path),
               uid=st.st_uid, gid=st.st_gid, mode=stat.S_IMODE(st.st_mode), parent_writable=os.access(os.path.dirname(path), os.W_OK))
    try:
        vfs = os.statvfs(path)
        out["bsize"] = vfs.f_bsize if vfs.f_bsize and vfs.f_bsize & (vfs.f_bsize - 1) == 0 and vfs.f_bsize >= 4096 else None
        out["free"] = vfs.f_bavail * vfs.f_frsize
    except OSError:
        out["bsize"] = out["free"] = None
    try:
        out["empty"] = not any(n for n in os.listdir(path) if not n.endswith(PART)) if out["is_dir"] else None
    except OSError:
        out["empty"] = None
    return out


def rename_folder(path: str, new_name: str) -> str:
    """The wizard's two renames, and only those: a folder to ``.<its name>.plexcache-old`` (out of
    the way), or back (undo). Returns the new path."""
    parent, name = os.path.split(path.rstrip("/"))
    m_old, m_new = OLD.match(name), OLD.match(new_name)
    if not ((m_new and m_new.group("name") == name) or (m_old and m_old.group("name") == new_name)):
        raise ValueError("only a folder's own .<name>.plexcache-old can be used")
    target = os.path.join(parent, new_name)
    if os.path.lexists(target):
        raise FileExistsError(f"{new_name} is there already")
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    os.rename(path, target)
    return target


def delete_old(path: str) -> None:
    """Delete an old folder the wizard moved out of the way — only one named .<name>.plexcache-old."""
    if not OLD.match(os.path.basename(path.rstrip("/"))) or not os.path.isdir(path) or os.path.islink(path):
        raise ValueError("only a .<name>.plexcache-old folder can be deleted")
    shutil.rmtree(path)


def propagates(path: str, mount_list: list[dict] | None = None) -> bool | None:
    """Whether new mounts under ``path`` on the host show up here without a restart (its volume
    uses propagation: rslave). None when it can't be told."""
    from .. import zfs
    m = zfs.mount_for(path, mount_list)
    if not m:
        return None
    return any(f.startswith("master:") for f in m.get("optional") or [])

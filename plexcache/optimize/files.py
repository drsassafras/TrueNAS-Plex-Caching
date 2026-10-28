"""The optimizer's file actions for the tools (Duplicate Versions, Organize Folders): a file set
aside in the library's trash, put back, or purged; a second path to a file (a hard link) made, and a
path removed. Each stays inside the media folders the service may write (server.py checks), and never
follows a link out of them.

Trash: ``<library folder>/.plexcache-trash/<the file's path within the folder>`` — the same dataset,
so setting aside is a rename; a synced folder carries the move to the other site as a deletion there.
"""

import os
import shutil
import stat

TRASH = ".plexcache-trash"


def _plain(path: str) -> os.stat_result:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("not a plain file")
    return st


def trash_path(root: str, path: str) -> str:
    """Where a file of library folder ``root`` goes when set aside."""
    rel = os.path.relpath(path, root)
    if rel.startswith(".."):
        raise ValueError("not inside the library folder")
    return os.path.join(root, TRASH, rel)


def set_aside(root: str, path: str) -> dict:
    """Move a file into the library's trash (a rename on the same dataset). {"trashed": its new path}."""
    _plain(path)
    dst = trash_path(root, path)
    if os.path.lexists(dst):
        raise ValueError("already set aside")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    ignore = os.path.join(root, TRASH, ".plexignore")  # (Plex skips dot-folders; said outright too)
    if not os.path.exists(ignore):
        with open(ignore, "w") as fh:
            fh.write("*\n")
    os.rename(path, dst)
    return {"trashed": dst}


def put_back(root: str, trashed: str) -> dict:
    """A set-aside file back where it was. {"path": where}."""
    _plain(trashed)
    base = os.path.join(root, TRASH)
    rel = os.path.relpath(trashed, base)
    if rel.startswith(".."):
        raise ValueError("not in the trash")
    dst = os.path.join(root, rel)
    if os.path.lexists(dst):
        raise ValueError("a file is there now")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(trashed, dst)
    _prune(base, os.path.dirname(trashed))
    return {"path": dst}


def purge(root: str, trashed: str) -> dict:
    """A set-aside file deleted for good."""
    _plain(trashed)
    base = os.path.join(root, TRASH)
    if os.path.relpath(trashed, base).startswith(".."):
        raise ValueError("not in the trash")
    os.remove(trashed)
    _prune(base, os.path.dirname(trashed))
    return {"purged": True}


def _prune(base: str, folder: str) -> None:
    """Empty folders left in the trash removed, up to the trash itself."""
    while folder.startswith(base) and folder != base:
        try:
            os.rmdir(folder)
        except OSError:
            return
        folder = os.path.dirname(folder)


def copy_keeping_dates(src: str, dst: str) -> None:
    """The bytes copied, then the mode and dates: what copy2 does, but a refusal to copy ownership, flags or
    extended attributes (ACLs, in a container without the right to set them) isn't a failure — Plex and the
    folder sync go by the bytes and the dates."""
    shutil.copyfile(src, dst)
    try:
        shutil.copystat(src, dst)
    except PermissionError:
        st = os.stat(src)
        try:
            os.chmod(dst, stat.S_IMODE(st.st_mode))
        except OSError:
            pass
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


def make_real(path: str) -> dict:
    """A link — a hard link (two paths to one file) or a symlink — replaced by a file of its own at the
    same path: the bytes copied beside it, then swapped in, the dates kept. {"bytes": copied}."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        src = os.path.realpath(path)
        if not os.path.isfile(src):
            raise ValueError("the link's target isn't a file")
    elif stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        src = path
    elif stat.S_ISREG(st.st_mode):
        return {"bytes": 0, "already": True}  # (a file of its own already)
    else:
        raise ValueError("not a file or a link")
    tmp = os.path.join(os.path.dirname(path), ".plexcache-copy-" + os.path.basename(path))
    try:
        copy_keeping_dates(src, tmp)  # (the bytes and the dates: Plex and the folder sync see the same file)
        os.replace(tmp, path)   # (the old path's inode drops a link; a symlink is replaced by the file)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return {"bytes": os.stat(path).st_size}


def link(path: str, new: str) -> dict:
    """A second path to the file: a hard link (instant, no space; the same dataset). {"linked": new}."""
    _plain(path)
    if os.path.lexists(new):
        raise ValueError("a file is there already")
    os.makedirs(os.path.dirname(new), exist_ok=True)
    try:
        os.link(path, new)
    except OSError as e:
        if e.errno in (18, 95):  # EXDEV, EOPNOTSUPP: another dataset, or no hard links — copied instead
            shutil.copy2(path, new)
        else:
            raise
    return {"linked": new}


def unlink(path: str, keep: str | None = None) -> dict:
    """A path removed. With ``keep``, only when that path is another link to the same file (the move
    is complete), so the file itself is never lost."""
    st = _plain(path)
    if keep is not None:
        try:
            other = os.lstat(keep)
        except OSError:
            raise ValueError("the other path isn't there") from None
        if (other.st_ino, other.st_dev) != (st.st_ino, st.st_dev) and other.st_size != st.st_size:
            raise ValueError("the other path isn't the same file")
    os.remove(path)
    folder = os.path.dirname(path)
    try:
        if not os.listdir(folder):
            os.rmdir(folder)  # (a folder left empty by the move)
    except OSError:
        pass
    return {"removed": True}


def copy_from(path: str, src: str) -> dict:
    """A file put back from a backup's copy (a snapshot's .zfs/snapshot path, a set-aside copy): ``src`` copied beside
    ``path`` and swapped in, dates kept. {"bytes"}."""
    if not os.path.isfile(src):
        raise ValueError("the backup's copy isn't there")
    if os.path.lexists(path):
        _plain(path)
    tmp = os.path.join(os.path.dirname(path), ".plexcache-copy-" + os.path.basename(path))
    try:
        copy_keeping_dates(src, tmp)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return {"bytes": os.path.getsize(path)}

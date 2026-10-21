"""The optimizer's file actions for the tools (Duplicate Versions, Organize Folders): a file set
aside in the library's trash, put back, or purged; a second path to a file (a hard link) made, and a
path removed. Each stays inside the media folders the service may write (server.py checks), and never
follows a link out of them.

Trash: ``<library folder>/.plexcache-trash/<the file's path within the folder>`` — the same dataset,
so setting aside is a rename; a synced folder carries the move to the other site as a deletion there.
"""

from __future__ import annotations

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

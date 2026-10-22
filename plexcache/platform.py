"""Where Plex Cache is running: TrueNAS, or another Linux host — with ZFS or without. Nothing is
assumed: the TrueNAS API answering says TrueNAS for certain, with its version; so does the kernel —
TrueNAS builds its own, named "…+truenas", and a container runs on the host's kernel, so that's
visible from inside with no key and no mount; so does the host's /etc/version when the app's YAML
mounts it (read-only, at /host/etc/version), or PLEXCACHE_PLATFORM=truenas|linux set by hand.
Otherwise all that's known is whether ZFS is there (/proc/spl), and TrueNAS is "not detected":
the pages say so, and what needs the TrueNAS API is left out rather than asked for.
"""

from __future__ import annotations

import os
import platform as _platform
import re

from . import zfs
from .i18n import M

HOST_FILES = ("/host/etc/version", "/host/version")  # TrueNAS's /etc/version, when mounted into the app


def detect(truenas_version: str | None = None) -> dict:
    """{"name": "truenas" | "linux-zfs" | "linux", "truenas": True | False | None (not known), "how": what
    told, "version": TrueNAS's, "zfs": bool, "label": M}."""
    has_zfs = zfs.arcstats() is not None
    if truenas_version:
        return _out("truenas", True, "api", truenas_version, has_zfs)
    env = (os.environ.get("PLEXCACHE_PLATFORM") or "").strip().lower()
    if env == "truenas":
        return _out("truenas", True, "env", "", has_zfs)
    if env == "linux":
        return _out("linux-zfs" if has_zfs else "linux", False, "env", "", has_zfs)
    version = ""
    for path in HOST_FILES:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read(200).strip()
        except OSError:
            continue
        if re.match(r"\d+\.\d+", text):  # TrueNAS writes its version there (25.04.1); other distributions don't have the file
            version = text.split()[0]
            break
    kernel = kernel_release()
    if "truenas" in kernel.lower():  # TrueNAS's own kernel: seen from inside any of its apps
        return _out("truenas", True, "host" if version else "kernel", version, has_zfs)
    if version:
        return _out("truenas", True, "host", version, has_zfs)
    return _out("linux-zfs" if has_zfs else "linux", None if has_zfs else False, "guess", "", has_zfs)


def kernel_release() -> str:
    """The running kernel's release string ("6.12.15-production+truenas" on TrueNAS)."""
    try:
        return _platform.release() or ""
    except Exception:  # noqa: BLE001
        return ""


def _out(name: str, truenas, how: str, version: str, has_zfs: bool) -> dict:
    if name == "truenas":
        version = re.sub(r"^TrueNAS[- ]?(SCALE[- ]?)?", "", version, flags=re.I)  # (the API says "TrueNAS-25.10.0": once is enough)
        label = M("TrueNAS {version}", version=version) if version else M("TrueNAS")
    elif name == "linux-zfs":
        label = M("Linux with ZFS — TrueNAS not detected") if truenas is None else M("Linux with ZFS")
    else:
        label = M("Linux without ZFS")
    return {"name": name, "truenas": truenas, "how": how, "version": version, "zfs": has_zfs, "label": label}

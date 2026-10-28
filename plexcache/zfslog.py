"""The ZFS record: what this NAS's datasets are set to, and the compression each file is written
with. It's about this system only (a file synced elsewhere is written again there), so it lives
in Plex Cache's config folder (zfs-record.json), not with the media.

A file's compression can't be read back from it. So the record keeps, per dataset, its record size
and compression and since when they've been seen (``since``), with the earlier settings and their
dates; and per file, the compression it's known to be written with:

* **Optimal** — the optimizer wrote it, with the dataset's current compression;
* **Assumed** — it first showed up at a check when the dataset's settings were the same as at the
  check before: it was written with them;
* **Unknown** — it was there when the record began, or showed up right after the settings changed
  (it could have been written either side of the change);
* **NotOptimal** — it's known to be written with another compression than the dataset's now.

Optimal and Assumed need nothing; Unknown and NotOptimal are written again (Optimizer page). A
file written with ZSTD 3 to 19 is never NotOptimal, whatever level the dataset uses now: every one
of those levels is fine (grade()), so a change between them never rewrites a library. The exact
compression each file was written with is kept either way, for later decisions. The record size
isn't kept: it's read from each file whenever it's needed. Datasets and files not seen for six
months are dropped.
"""

import re
import threading
import time

KEEP = 182 * 24 * 3600  # missing datasets and files are kept this long
OPTIMAL, ASSUMED, UNKNOWN, NOT_OPTIMAL = "optimal", "assumed", "unknown", "not_optimal"
NEEDS_WORK = (UNKNOWN, NOT_OPTIMAL)
GOOD, LOW, WEAK, OTHER = "good", "low", "weak", "other"


def grade(value) -> str | None:
    """How a ZFS compression setting suits media:

    * "good" — ZSTD 3 to 19 (plain "zstd" is 3, the one recommended). From level 3, ZFS gives up
      early on what won't compress (most of a video), so higher levels only cost CPU on what does —
      for marginally smaller files — and reading is no slower at any level;
    * "low" — ZSTD 1 and 2: slightly less compression, and no faster in practice (no early abort);
    * "weak" — LZ4 ("on" is LZ4) and ZSTD-FAST: fast, but compress the metadata, index and padding less;
    * "other" — off, gzip (slower to read), zle, lzjb.
    None when it isn't known."""
    if not value:
        return None
    v = str(value).strip().lower()
    if v in ("lz4", "on") or v.startswith("zstd-fast"):
        return WEAK
    m = re.fullmatch(r"zstd(?:-(\d+))?", v)
    if m:
        return GOOD if 3 <= int(m.group(1) or 3) <= 19 else LOW
    return OTHER


class ZfsLog:
    def __init__(self, load, save):
        """``load()`` → the saved record or None; ``save(data)`` stores it."""
        self._save = save
        self.lock = threading.Lock()
        data = load() or {}
        self.started = data.get("started") or time.time()  # when this record began
        self.datasets: dict[str, dict] = data.get("datasets") or {}
        self.files: dict[str, dict] = data.get("files") or {}
        self.dirty = not data

    # ---------------------------------------------------------- datasets
    def observe_datasets(self, current: dict, now: float | None = None) -> None:
        """At each check: ``current`` is {dataset: {"rs", "compression"}}. New settings start a new
        period (``since``); the old ones go into the dataset's history."""
        now = now or time.time()
        with self.lock:
            for name, cur in current.items():
                rs, comp = cur.get("rs"), cur.get("compression")
                if comp is None and rs is None:
                    continue  # not readable this time: keep what's known
                ds = self.datasets.get(name)
                if ds is None:
                    self.datasets[name] = {"rs": rs, "compression": comp, "since": now, "checked": now,
                                           "prev_checked": None, "seen": now, "history": []}
                else:
                    if (ds.get("rs"), ds.get("compression")) != (rs, comp):
                        ds["history"].append({"rs": ds.get("rs"), "compression": ds.get("compression"),
                                              "since": ds.get("since"), "until": now})
                        ds.update(rs=rs, compression=comp, since=now)
                    ds["prev_checked"], ds["checked"], ds["seen"] = ds.get("checked"), now, now
                self.dirty = True

    def settled(self, dataset: str) -> bool:
        """Were the dataset's settings the same at the check before this one? Then files that
        showed up since were written with them."""
        ds = self.datasets.get(dataset) or {}
        prev = ds.get("prev_checked")
        return bool(prev and ds.get("since") is not None and ds["since"] <= prev)

    # ---------------------------------------------------------- files
    def status(self, path: str, ino: int, dataset: str | None, now: float | None = None) -> str | None:
        """The file's compression status (see the module's notes), recording it as seen. None when
        the dataset's compression isn't known (then nothing can be said either way)."""
        now = now or time.time()
        ds = self.datasets.get(dataset or "")
        current = (ds or {}).get("compression")
        with self.lock:
            entry = self.files.get(path)
            if not entry or entry.get("ino") != ino:
                # new here (or replaced): written with the current settings if they'd settled already
                assumed = current is not None and self.settled(dataset)
                entry = {"ino": ino, "dataset": dataset, "compression": current if assumed else None,
                         "basis": "assumed" if assumed else None}
                self.files[path] = entry
                self.dirty = True
            elif entry.get("dataset") != dataset:
                entry.update(dataset=dataset, compression=None, basis=None)  # moved to another dataset
                self.dirty = True
            entry["seen"] = now
        if current is None:
            return None
        if entry.get("compression") is None:
            return UNKNOWN
        # written with something else than the dataset uses now: worth writing again only if what
        # it's written with isn't good already (ZSTD 3–19 never moves to another level of it)
        if entry["compression"] != current and grade(entry["compression"]) != GOOD:
            return NOT_OPTIMAL
        return OPTIMAL if entry.get("basis") == "written" else ASSUMED

    def written(self, path: str, ino: int, dataset: str | None, compression: str | None, now: float | None = None) -> None:
        """The optimizer wrote the file (at ``ino``), while its dataset used ``compression``."""
        with self.lock:
            self.files[path] = {"ino": ino, "dataset": dataset, "compression": compression,
                                "basis": "written" if compression else None, "seen": now or time.time(),
                                "at": now or time.time()}
            self.dirty = True

    # ---------------------------------------------------------- upkeep
    def collect(self, now: float | None = None) -> int:
        """Drop datasets and files not seen for six months. How many were dropped."""
        now = now or time.time()
        with self.lock:
            old = [p for p, e in self.files.items() if now - (e.get("seen") or 0) > KEEP]
            for p in old:
                del self.files[p]
            gone = [d for d, e in self.datasets.items() if now - (e.get("seen") or 0) > KEEP]
            for d in gone:
                del self.datasets[d]
            if old or gone:
                self.dirty = True
            return len(old) + len(gone)

    def save(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            data = {"version": 1, "started": self.started, "datasets": dict(self.datasets), "files": dict(self.files)}
            self.dirty = False
        self._save(data)

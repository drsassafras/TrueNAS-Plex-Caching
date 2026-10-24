"""The library data layer: one local copy per library of what the Library Health checks and the Tools
read, gathered once and kept fresh, so every check is a function over data that's already here.

Three kinds of data, each with its own way of learning it changed:

* **Records** — the library's top-level Plex records (films, shows; plex.library_items: key, title,
  guid, files with sizes and running times, edited fields, collections…). Plex's own change stamp on
  the library (``contentChangedAt``) says when to read them again.
* **Files** — every file Plex lists, as it is on disk (size, mtime, inode, link count, symlink), the
  media files in the library's folders that Plex *doesn't* list, the other paths of hard-linked files,
  and the folder tree (each folder's names). A walk that reads only the folders' mtimes — they change
  whenever something is added, removed or renamed in them — says when to read the files again.
* **Details** — each file's item page in Plex (plex.item_detail): the streams as Plex probed them — a
  track's bitrate, channels, bit depth, and Atmos or DTS:X in its name — and the fields you locked. Read
  after the records and files, one request per item, and again when the library's stamp moves.
* **Layouts** — the inside of each file (filemap), read by the Inspector and cached by the file's stat:
  stale the moment the file's stat changes. Not held here; FileMaps is the cache.

The Gatherer keeps the copies: one library at a time, the one just opened first (fast_track), paused
between items for another; tick() (the scheduler, every few minutes) looks for changes and gathers
what changed; start() gathers every library after the first checks. Each library's copy is saved
(libdata-<key>.json), so a restart starts with yesterday's data and only refreshes.

The rules the tests enforce (test_tools_read_through_the_data_layer, test_request_budget): a tool's
or a check's scan reads from a LibraryData and nothing else — no Plex call, no stat, no walk of its
own — so one gathering serves every check, and a change reaches every one of them.
"""

from __future__ import annotations

import json
import os
import threading
import time

from .i18n import M

VERSION = 1  # (raised when the data's shape changes: older copies on disk are gathered again)
TOP_TYPE = {"movie": 1, "show": 2}  # the records the tools work on: films and shows (music has no duplicate records to merge)
MEDIA = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".flv", ".wmv", ".ogm", ".ogv", ".webm",
         ".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".oga", ".opus", ".wav", ".aif", ".aiff", ".wma", ".ape", ".wv", ".dsf", ".ac3", ".dts")
RECORDS_TTL = 6 * 3600   # records read again this often even without a change stamp (an older Plex without one)
FILES_TTL = 6 * 3600     # the files, likewise, besides what the folders' mtimes say
TICK_EVERY = 300         # how often tick() looks for changes
PACE = 0.02              # between item pages (details): a big library's read spread out, Plex left room to breathe
ALL = ("records", "files", "details")


def link_of(row: dict | None) -> tuple[str, int, str]:
    """("symlink" | "hardlink" | "", links, target) for a file as the data layer has it."""
    if not row or row.get("missing"):
        return "", 0, ""
    if row.get("symlink"):
        return "symlink", 1, row.get("target") or ""
    return ("hardlink", int(row.get("nlink") or 1), "") if int(row.get("nlink") or 1) > 1 else ("", 1, "")


SAMPLE = 64 * 1024  # bytes read at each end of two files of one size, to tell an identical copy


def ends(local: str, size: int) -> bytes | None:
    """The first and last SAMPLE bytes of a file: what two copies of one file share and two files of one size don't."""
    try:
        with open(local, "rb") as fh:
            head = fh.read(SAMPLE)
            if size > SAMPLE:
                fh.seek(max(SAMPLE, size - SAMPLE))
                return head + fh.read(SAMPLE)
            return head
    except OSError:
        return None


def same_bytes(a: str, b: str, size: int) -> bool:
    """Whether two files of one size are the same bytes, as far as their ends tell."""
    x = ends(a, size)
    return x is not None and x == ends(b, size)


class LibraryData:
    """One library's copy. ``files``: [{"path" (Plex's), "local", "rk", "title", "poster", "kbps", "duration",
    "kind", "size", "mtime", "ino", "dev", "nlink", "symlink", "target", "root", "missing"}]; ``unlisted``:
    media files on disk Plex doesn't list [{"local", "path", "size", "mtime", "ino", "dev", "nlink", "root"}];
    ``tree``: {folder: [names]}; ``dirs``: {folder: mtime}; ``records``: the Plex records."""

    def __init__(self, key: str):
        self.key = str(key)
        self.records: list[dict] = []
        self.records_at = 0.0
        self.plex_changed = ""   # Plex's change stamp on the library when the records were read
        self.files: list[dict] = []
        self.unlisted: list[dict] = []
        self.tree: dict[str, list[str]] = {}
        self.dirs: dict[str, float] = {}
        self.roots: list[tuple[str, str]] = []  # (Plex's folder, local folder)
        self.files_at = 0.0
        self.details: dict[str, dict] = {}  # rating key -> its item page: {"streams", "locked", "fields", "tags", "updated_at"}
        self.details_at = 0.0
        self.stamp = 0             # raised at every change: what the tools' cached analyses are keyed by
        self.errors: dict[str, str] = {}

    # ------------------------------------------------------------ what's known
    @property
    def ready(self) -> bool:
        return bool(self.records_at and self.files_at)

    def streams(self, rk: str, local: str = "") -> list[dict]:
        """A file's audio and subtitle streams as Plex probed them (its item's page), in order; [] when not read."""
        d = self.details.get(str(rk)) or {}
        out = d.get("streams") or []
        if local and any(st.get("file") for st in out):
            mine = [f["path"] for f in self.files if f["local"] == local]
            if mine:
                out = [st for st in out if st.get("file") == mine[0]] or out
        return out

    def file(self, local: str) -> dict | None:
        """A listed or unlisted file as it was on disk, by local path."""
        for f in self.files:
            if f["local"] == local:
                return None if f.get("missing") else f
        for f in self.unlisted:
            if f["local"] == local:
                return f
        return None

    @staticmethod
    def stat_result(row: dict):
        """A file's stat as FileMaps keys a layout by (size, mtime_ns, inode), from the data layer's row."""
        import types
        return types.SimpleNamespace(st_size=row.get("size") or 0, st_mtime_ns=row.get("mtime_ns") or 0, st_ino=row.get("ino") or 0, st_mtime=row.get("mtime") or 0)

    def by_inode(self) -> dict[tuple, list[str]]:
        """Every hard-linked file's paths, by (device, inode): the listed ones and the unlisted."""
        out: dict[tuple, list[str]] = {}
        for f in self.files + self.unlisted:
            if f.get("nlink", 1) > 1 and not f.get("symlink") and not f.get("missing"):
                out.setdefault((f["dev"], f["ino"]), []).append(f["local"])
        return out

    def listed(self) -> set[str]:
        return {f["local"] for f in self.files if not f.get("missing")}

    def names_in(self, local_dir: str) -> list[str]:
        """The names in a folder (files and folders), as walked."""
        return list(self.tree.get(local_dir.rstrip("/") or "/", []))

    def listing(self, local_dir: str) -> list[str]:
        """A folder's files with their paths within it, subfolders included (dot files left out)."""
        base = local_dir.rstrip("/") or "/"
        out = []
        for d, names in self.tree.items():
            if d == base or d.startswith(base + "/"):
                for n in names:
                    if n.startswith(".") or (os.path.join(d, n) in self.tree):
                        continue
                    out.append(os.path.relpath(os.path.join(d, n), base))
        return sorted(out)

    def root_of(self, path: str) -> tuple[str, str] | None:
        """(Plex's folder, local folder) a Plex path is under."""
        for loc, local in sorted(self.roots, key=lambda r: len(r[0]), reverse=True):
            if path.startswith(loc + "/"):
                return loc, local
        return None

    # ------------------------------------------------------------ on disk
    def to_json(self) -> dict:
        return {"v": VERSION, "key": self.key, "records": self.records, "records_at": self.records_at, "plex_changed": self.plex_changed,
                "files": self.files, "unlisted": self.unlisted, "tree": self.tree, "dirs": self.dirs, "roots": self.roots,
                "files_at": self.files_at, "details": self.details, "details_at": self.details_at, "stamp": self.stamp}

    @classmethod
    def from_json(cls, d: dict) -> "LibraryData | None":
        if not isinstance(d, dict) or d.get("v") != VERSION or not d.get("key"):
            return None
        x = cls(d["key"])
        x.records, x.records_at, x.plex_changed = d.get("records") or [], float(d.get("records_at") or 0), str(d.get("plex_changed") or "")
        x.files, x.unlisted, x.tree, x.dirs = d.get("files") or [], d.get("unlisted") or [], d.get("tree") or {}, d.get("dirs") or {}
        x.roots = [tuple(r) for r in d.get("roots") or []]
        x.files_at, x.stamp = float(d.get("files_at") or 0), int(d.get("stamp") or 0)
        x.details, x.details_at = d.get("details") or {}, float(d.get("details_at") or 0)
        return x


class Gatherer:
    """Keeps every library's LibraryData: gathers in the background, one library at a time, the one asked for
    first; looks for changes now and then; tells the page what it's doing (snapshot)."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.libs: dict[str, LibraryData] = {}
        self.queue: list[tuple[str, tuple[str, ...]]] = []  # (library, what) waiting, front first
        self.current: str | None = None
        self.progress: dict | None = None
        self.working = False
        self.started = False
        self.last_tick = 0.0
        self.viewed: list[str] = []  # libraries opened, latest first: the order to gather in
        self.listeners: list = []    # called with (key, what) after a gathering: the tools' caches, say
        self._inside = threading.local()  # (a gathering under way in this thread: a listener asking for the data gets it as it is, never a gathering within a gathering)

    # ------------------------------------------------------------ reading
    def data(self, key: str) -> LibraryData:
        """The library's copy as it is now (empty until gathered; loaded from disk the first time)."""
        key = str(key)
        with self.lock:
            d = self.libs.get(key)
            if d is None:
                d = LibraryData.from_json(self.app._load_json(f"libdata-{key}.json")) or LibraryData(key)
                self.libs[key] = d
            return d

    def fresh(self, key: str, what: tuple[str, ...] = ALL) -> bool:
        d = self.data(key)
        now = time.time()
        age = {"records": (d.records_at, RECORDS_TTL), "files": (d.files_at, FILES_TTL), "details": (d.details_at, RECORDS_TTL)}
        return all(age[w][0] and now - age[w][0] < age[w][1] for w in what)

    def gather(self, key: str, what: tuple[str, ...] = ALL, wait: bool = False, progress=None, refresh: bool = False) -> LibraryData:
        """The library's copy, gathered now in this thread (``wait``) when it isn't fresh — or whatever its age
        (``refresh``) — else queued. ``progress``: called with {"what", "done", "total"} as it goes (a tool's scan shows it)."""
        key = str(key)
        if getattr(self._inside, "key", None) is not None:  # (called from a listener of a gathering: what's there, without another)
            return self.data(key)
        if refresh:
            d = self.data(key)
            for w in what:
                self._stale(d, w)
        if wait:
            stale = tuple(w for w in what if not self.fresh(key, (w,)))
            if stale:
                self._gather(key, stale, progress)
            return self.data(key)
        self.fast_track(key, what)
        return self.data(key)

    def fast_track(self, key: str, what: tuple[str, ...] = ALL) -> None:
        """The library opened: gathered first (the one running pauses after its item), and remembered as the order."""
        key = str(key)
        with self.lock:
            self.viewed = [key] + [k for k in self.viewed if k != key]
            self.queue = [(key, what)] + [q for q in self.queue if q[0] != key]
            self._start_locked()

    def start(self) -> None:
        """Every library gathered, in the background, the ones opened lately first (after the first checks)."""
        with self.lock:
            self.started = True
            keys = [str(lib["key"]) for lib in getattr(self.app, "libraries", []) or [] if lib.get("kind") and not lib.get("excluded")]
            order = [k for k in self.viewed if k in keys] + [k for k in keys if k not in self.viewed]
            queued = {q[0] for q in self.queue}
            self.queue += [(k, ALL) for k in order if k not in queued]
            self._start_locked()

    def _start_locked(self) -> None:
        if not self.working and self.queue:
            self.working = True
            threading.Thread(target=self._work, name="libdata", daemon=True).start()

    def _work(self) -> None:
        while True:
            with self.lock:
                if not self.queue:
                    self.working, self.current = False, None
                    return
                key, what = self.queue.pop(0)
                self.current = key
            try:
                stale = tuple(w for w in what if not self.fresh(key, (w,)))
                if stale:
                    self._gather(key, stale)
            except Exception as e:  # noqa: BLE001 — said in the data's errors, the next library goes on
                self.data(key).errors["gather"] = str(e)
            finally:
                with self.lock:
                    self.current, self.progress = None, None

    def _gather(self, key: str, what: tuple[str, ...], progress=None) -> None:
        if getattr(self._inside, "key", None) is not None:
            return  # (never a gathering within a gathering)
        self._inside.key = key
        try:
            self._gather_inner(key, what, progress)
        finally:
            self._inside.key = None

    def _gather_inner(self, key: str, what: tuple[str, ...], progress=None) -> None:
        d = self.data(key)
        lib = self.app.library_info(key) or {}

        def step(w, done, total):
            p = {"library": key, "what": w, "done": done, "total": total}
            with self.lock:
                self.progress = p
            if progress:
                progress(p)
        if "records" in what:
            step("records", 0, 0)
            leaf = TOP_TYPE.get(lib.get("type") or "")
            try:
                recs = self.app.plex(timeout=120).library_items(key, leaf) if leaf else []
                d.records = recs
                d.errors.pop("records", None)
            except Exception as e:  # noqa: BLE001 — tried at this stamp: said, and not again until it moves (or the age is up)
                d.errors["records"] = str(e)
            d.records_at, d.plex_changed = time.time(), str(lib.get("changed") or "")
        if "files" in what:
            self._gather_files(d, lib, step)
        if "details" in what:
            self._gather_details(d, step)
        d.stamp += 1
        self.app._save_json(f"libdata-{key}.json", d.to_json())
        for fn in list(self.listeners):
            try:
                fn(key, what)
            except Exception:  # noqa: BLE001
                pass

    def _gather_details(self, d: LibraryData, step) -> None:
        """Each file's item page: one request per item, paced; an item whose page can't be read keeps what it had."""
        rks = sorted({f["rk"] for f in d.files if f.get("rk")})
        client = self.app.plex(timeout=60)
        for i, rk in enumerate(rks):
            if i % 10 == 0:
                step("details", i, len(rks))
            try:
                got = client.item_detail(rk)
            except Exception as e:  # noqa: BLE001 — Plex away: tried again next time
                d.errors["details"] = str(e)
                return
            if got is not None:
                d.details[rk] = {"streams": got.get("streams") or [], "locked": got.get("locked") or [], "fields": got.get("fields") or {},
                                 "tags": got.get("tags") or {}, "updated_at": got.get("updated_at") or 0}
            time.sleep(PACE)
        d.details = {k: v for k, v in d.details.items() if k in set(rks)}
        d.details_at = time.time()
        d.errors.pop("details", None)
        step("details", len(rks), len(rks))

    def _gather_files(self, d: LibraryData, lib: dict, step) -> None:
        app = self.app
        roots = [(loc.rstrip("/"), app.map_path(loc.rstrip("/"))) for loc in lib.get("locations") or []]
        files = []
        mine = [f for f in list(app.files) if str(f.library_key) == d.key]
        for i, f in enumerate(mine):
            if i % 100 == 0:
                step("files", i, len(mine))
            local = app.map_path(f.path)
            root = next((r for r in sorted(roots, key=lambda r: len(r[0]), reverse=True) if f.path.startswith(r[0] + "/")), None)
            row = {"path": f.path, "local": local, "rk": getattr(f, "rating_key", "") or "", "title": getattr(f, "title", "") or "", "poster": getattr(f, "poster", "") or "",
                   "kbps": getattr(f, "kbps", 0) or 0, "duration": getattr(f, "duration_ms", 0) or 0, "kind": getattr(f, "kind", "") or "", "root": root[1] if root else ""}
            try:
                st = os.lstat(local)
                sym = os.path.islink(local)
                row.update(size=st.st_size, mtime=st.st_mtime, mtime_ns=st.st_mtime_ns, ino=st.st_ino, dev=st.st_dev, nlink=st.st_nlink, symlink=sym,
                           target=os.path.realpath(local) if sym else "", missing=False)
                if sym:
                    try:
                        row["size"] = os.stat(local).st_size
                    except OSError:
                        pass
            except OSError:
                row.update(size=getattr(f, "size", 0) or 0, mtime=0, mtime_ns=0, ino=0, dev=0, nlink=1, symlink=False, target="", missing=True)
            files.append(row)
        listed = {f["local"] for f in files}
        tree, dirs, unlisted = {}, {}, []
        for loc, local_root in roots:
            for dirpath, subdirs, names in os.walk(local_root):
                subdirs[:] = [x for x in subdirs if not x.startswith(".")]  # (not the trash, nor .plexcache)
                tree[dirpath] = sorted(subdirs + names)
                try:
                    dirs[dirpath] = os.stat(dirpath).st_mtime
                except OSError:
                    dirs[dirpath] = 0
                for n in names:
                    if n.startswith(".") or not n.lower().endswith(MEDIA):
                        continue
                    local = os.path.join(dirpath, n)
                    if local in listed:
                        continue
                    try:
                        st = os.lstat(local)
                    except OSError:
                        continue
                    if not os.path.isfile(local) or os.path.islink(local):
                        continue
                    unlisted.append({"local": local, "path": loc + local[len(local_root):], "size": st.st_size, "mtime": st.st_mtime, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino, "dev": st.st_dev,
                                     "nlink": st.st_nlink, "symlink": False, "root": local_root})
        step("files", len(mine), len(mine))
        d.files, d.unlisted, d.tree, d.dirs, d.roots, d.files_at = files, unlisted, tree, dirs, roots, time.time()
        d.errors.pop("files", None)

    # ------------------------------------------------------------ changes
    def dirs_changed(self, key: str) -> bool:
        """Whether a folder of the library was changed since the files were gathered (its mtime moved, or a folder came or went)."""
        d = self.data(key)
        if not d.files_at:
            return False
        seen = {}
        for _loc, local_root in d.roots:
            for dirpath, subdirs, _names in os.walk(local_root):
                subdirs[:] = [x for x in subdirs if not x.startswith(".")]
                try:
                    seen[dirpath] = os.stat(dirpath).st_mtime
                except OSError:
                    seen[dirpath] = 0
        return seen != d.dirs

    def tick(self) -> list[str]:
        """Looks for changes: Plex's change stamp per library (its records), the folders' mtimes (its files); what
        changed is queued. The libraries changed."""
        if time.time() - self.last_tick < TICK_EVERY:
            return []
        self.last_tick = time.time()
        changed = []
        with self.lock:
            busy = {q[0] for q in self.queue} | ({self.current} if self.current else set())
        for lib in getattr(self.app, "libraries", []) or []:
            key = str(lib["key"])
            if not lib.get("kind") or lib.get("excluded") or key in busy:  # (being gathered, or waiting to be: left to it)
                continue
            d = self.data(key)
            now = time.time()
            what = []
            # the records: never read, Plex's change stamp moved, or old
            if not d.records_at or (lib.get("changed") or "") != d.plex_changed or now - d.records_at > RECORDS_TTL:
                what.append("records")
            # the files: never read or made stale (Plex's listing read again, a tool's work), a folder changed, old — or the records changed
            if not d.files_at or what or now - d.files_at > FILES_TTL or self.dirs_changed(key):
                what.append("files")
            # the item pages: never read, or the records changed (an item edited or re-probed moves the library's stamp)
            if not d.details_at or "records" in what:
                what.append("details")
            if what:
                changed.append(key)
                for w in what:  # (said stale, so the gathering reads it whatever its age)
                    self._stale(d, w)
                with self.lock:
                    if key not in {q[0] for q in self.queue}:
                        self.queue.append((key, tuple(what)))
                    self._start_locked()
        return changed

    def refresh(self, key: str, what: tuple[str, ...] = ALL) -> None:
        """Gathered again whatever its age (Scan Again)."""
        d = self.data(key)
        for w in what:
            self._stale(d, w)
        self.fast_track(key, what)

    @staticmethod
    def _stale(d: LibraryData, what: str) -> None:
        if what == "records":
            d.records_at = 0.0
        elif what == "details":
            d.details_at = 0.0
        else:
            d.files_at = 0.0

    def invalidate_files(self, key: str | None = None) -> None:
        """Files changed (a tool's work; Plex's listing read again: every library): stale, gathered again when next asked for."""
        with self.lock:
            keys = [str(key)] if key is not None else list(self.libs)
        for k in keys:
            self.data(k).files_at = 0.0

    # ------------------------------------------------------------ the page
    def snapshot(self) -> dict:
        with self.lock:
            return {"current": self.current, "progress": dict(self.progress) if self.progress else None, "queue": [q[0] for q in self.queue],
                    "libraries": {k: {"records_at": d.records_at, "files_at": d.files_at, "details_at": d.details_at, "ready": d.ready, "stamp": d.stamp, "errors": dict(d.errors),
                                      "files": len(d.files), "unlisted": len(d.unlisted), "records": len(d.records), "details": len(d.details)} for k, d in self.libs.items()}}

    def why_not_ready(self, key: str):
        """What a page says while a library's data isn't gathered yet (an M), or None."""
        d = self.data(key)
        if d.ready:
            return None
        with self.lock:
            running = self.current == str(key)
        return M("Reading the library’s records and files…") if running else M("The library’s records and files are read first: a moment.")

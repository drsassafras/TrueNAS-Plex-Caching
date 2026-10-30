"""The library data layer: one local copy per library of what the Library Health checks and the Tools
read, gathered once and kept fresh, so every check is a function over data that's already here.

Four kinds of data, each with its own way of learning it changed:

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
* **The mirror** — with the library paired with one on its mirror (the Mirror page): both servers' items of
  the kind the two compare by (films; shows, seasons and episodes; artists, albums and tracks), each one's own page
  where the two differ, and the other site's files on disk, through the Plex Cache there when it's talking. What Sync
  Libraries brings in line, and Mirror Checks. Read again when the pairing changes, the records here were read since,
  the library there changed (its own change stamp), or it's old; nothing at all without a mirror.
* **Layouts** — the inside of each file (filemap), read by the Inspector and cached by the file's stat:
  stale the moment the file's stat changes. Not held here; FileMaps is the cache.

The Gatherer keeps the copies: one library at a time, in the evaluator's order (evaluate.py: the library a page has
open first, then the ones opened lately); the one being read breaks off between its steps — and between item pages —
for a library a page opens (fast_track), and picks up where it was right after it. tick() (the scheduler, every few
minutes) looks for changes and gathers what changed; start() gathers every library after the first checks. Each
library's copy is saved (libdata-<key>.json), so a restart starts with yesterday's data and only refreshes.

A kind of data known to have changed (Plex's stamp moved, a folder changed, Scan Again) is said stale while it's read
again — every count that reads it waits for it (evaluate.data_waiting), never shown from what was there before — and
everything worked out over it is worked out again once it's in (the stamp moves). One read again only for its age stays
in meanwhile, and moves the stamp only when what's read differs from what was there: a re-read that finds nothing new
leaves every count worked out over it standing.

The rules the tests enforce (test_tools_read_through_the_data_layer, test_request_budget): a tool's
or a check's scan reads from a LibraryData and nothing else — no Plex call, no stat, no walk of its
own — so one gathering serves every check, and a change reaches every one of them.
"""

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
MIRROR_TTL = 6 * 3600    # the mirror's side, likewise, besides its library's change stamp
TICK_EVERY = 300         # how often tick() looks for changes
PACE = 0.02              # between item pages (details): a big library's read spread out, Plex left room to breathe
LOCAL = ("records", "files", "details")  # what's read of this server and its disk: what most tools read
ALL = LOCAL + ("mirror",)
# what's kept of an item of either server for comparing them (plex.item_summary's, less what the compare never reads)
MIRROR_KEYS = ("key", "parent", "grandparent", "type", "title", "label", "index", "guid", "matched", "year", "summary", "summary_text", "thumb", "tags",
               "files", "durations", "edition", "art_path", "fields", "values", "locked", "collections", "labels", "art_locked", "updated_at")


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
    ``tree``: {folder: [names]}; ``dirs``: {folder: mtime}; ``records``: the Plex records. ``mirror``: the mirrored
    server's side (None: no mirror, or the library isn't paired) — {"library", "title", "locations": its library there,
    "here", "there": each server's items (MIRROR_KEYS), "pages_here", "pages_there": {key: the item's own page} for the
    items the two differ on, "disk": the other site's files [[place in the folder, size, mtime]] or None, "disk_why":
    why not ("none": no Plex Cache known there; "link": the link to it not working; "reading": it hasn't read them yet;
    else what went wrong)}."""

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
        self.mirror: dict | None = None
        self.mirror_at = 0.0
        self.mirror_for = ""       # the pairing read: "<the mirror's machine id>/<its library>" ("" with none)
        self.mirror_changed = ""   # the library there's change stamp when it was read
        self.stamp = 0             # raised at every change: what the tools' cached analyses are keyed by
        self.errors: dict[str, str] = {}
        self.reading_details: set[str] = set()  # (the item pages read this round, when it was broken off for another library)

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
                "files_at": self.files_at, "details": self.details, "details_at": self.details_at, "stamp": self.stamp,
                "mirror": self.mirror, "mirror_at": self.mirror_at, "mirror_for": self.mirror_for, "mirror_changed": self.mirror_changed}

    @classmethod
    def from_json(cls, d: dict) -> LibraryData | None:
        if not isinstance(d, dict) or d.get("v") != VERSION or not d.get("key"):
            return None
        x = cls(d["key"])
        x.records, x.records_at, x.plex_changed = d.get("records") or [], float(d.get("records_at") or 0), str(d.get("plex_changed") or "")
        x.files, x.unlisted, x.tree, x.dirs = d.get("files") or [], d.get("unlisted") or [], d.get("tree") or {}, d.get("dirs") or {}
        x.roots = [tuple(r) for r in d.get("roots") or []]
        x.files_at, x.stamp = float(d.get("files_at") or 0), int(d.get("stamp") or 0)
        x.details, x.details_at = d.get("details") or {}, float(d.get("details_at") or 0)
        x.mirror = d.get("mirror") if isinstance(d.get("mirror"), dict) else None
        x.mirror_at, x.mirror_for, x.mirror_changed = float(d.get("mirror_at") or 0), str(d.get("mirror_for") or ""), str(d.get("mirror_changed") or "")
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
        self.listeners: list = []    # called with (key, what) after a gathering: the evaluator (evaluate.py)
        self.urgent: str | None = None  # the library a page has open (fast_track): what's being read breaks off for it
        self._reading: tuple[str, ...] = ()  # the kinds the library being read (current) is read for
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
        age = {"records": (d.records_at, RECORDS_TTL), "files": (d.files_at, FILES_TTL), "details": (d.details_at, RECORDS_TTL), "mirror": (d.mirror_at, MIRROR_TTL)}
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
        """The library opened: gathered first — the one being read breaks off between its steps for it, and picks up where
        it was after — and remembered as the order. What it was waiting to be read for already is read with it."""
        key = str(key)
        with self.lock:
            self.viewed = [key] + [k for k in self.viewed if k != key]
            had = next((q[1] for q in self.queue if q[0] == key), ())
            what = tuple(w for w in ALL if w in what or w in had)
            self.queue = [(key, what)] + [q for q in self.queue if q[0] != key]
            if self.current != key:
                self.urgent = key
            self._start_locked()

    def order(self, libs: list[str]) -> None:
        """The libraries waiting, put in this order (the evaluator's: the page's library, the ones opened lately, the rest)
        — the first left first while it's the one a page has open."""
        rank = {str(k): i for i, k in enumerate(libs)}
        with self.lock:
            front = self.queue[:1] if self.queue and self.queue[0][0] == self.urgent else []
            self.queue = front + sorted(self.queue[len(front):], key=lambda q: rank.get(q[0], len(rank)))

    def busy(self, key: str) -> set[str]:
        """The kinds of the library's data being read now, or waiting to be."""
        key = str(key)
        with self.lock:
            out = {w for k, what in self.queue if k == key for w in what}
            if self.current == key:
                out.update(self._reading)
            return out

    def start(self) -> None:
        """Every library gathered, in the background, the ones opened lately first (after the first checks)."""
        with self.lock:
            self.started = True
            keys = [str(lib["key"]) for lib in getattr(self.app, "libraries", []) or [] if lib.get("kind") and not lib.get("excluded")]
            order = [k for k in self.viewed if k in keys] + [k for k in keys if k not in self.viewed]
            queued = {q[0] for q in self.queue}
            self.queue += [(k, ALL) for k in order if k not in queued]
            self._start_locked()
        ev = getattr(self.app, "evaluator", None)
        if ev is not None:
            ev.start()  # (and everything that's worked out from the data, as it comes in)

    def _start_locked(self) -> None:
        if not self.working and self.queue:
            self.working = True
            threading.Thread(target=self._work, name="libdata", daemon=True).start()

    def _work(self) -> None:
        while True:
            key, what = "", ()
            with self.lock:
                idle = not self.queue
                if idle:
                    self.working, self.current = False, None
                else:
                    key, what = self.queue.pop(0)
            if idle:
                ev = getattr(self.app, "evaluator", None)
                if ev is not None:
                    ev.kick()  # (what waits for the data layer to be done — the optimizer's scan of every library — can go now)
                return
            with self.lock:
                self.current, self._reading = key, what
                if self.urgent == key:
                    self.urgent = None
            left: tuple[str, ...] = ()
            try:
                stale = tuple(w for w in what if not self.fresh(key, (w,)))
                if stale:
                    left = self._gather(key, stale, can_yield=True)
                self.data(key).errors.pop("gather", None)
            except Exception as e:  # noqa: BLE001 — said in the data's errors, the next library goes on
                self.data(key).errors["gather"] = str(e)
            finally:
                with self.lock:
                    self.current, self.progress, self._reading = None, None, ()
                    if left:  # (broken off for the library a page has open: picked up right after it)
                        had = next((q[1] for q in self.queue if q[0] == key), ())
                        self.queue = [q for q in self.queue if q[0] != key]
                        at = 1 if self.queue and self.queue[0][0] == self.urgent else 0
                        self.queue.insert(at, (key, tuple(w for w in ALL if w in left or w in had)))

    def _gather(self, key: str, what: tuple[str, ...], progress=None, can_yield: bool = False) -> tuple[str, ...]:
        """The kinds read, in order; what's left of them (``can_yield``: broken off for the library a page has open)."""
        if getattr(self._inside, "key", None) is not None:
            return ()  # (never a gathering within a gathering)
        self._inside.key = key
        try:
            return self._gather_inner(key, what, progress, can_yield)
        finally:
            self._inside.key = None

    def _yield_to_other(self, key: str) -> bool:
        """Whether the library being read should break off: another is waiting at the front, opened on a page."""
        with self.lock:
            return bool(self.urgent and self.urgent != key and self.queue and self.queue[0][0] == self.urgent)

    def _gather_inner(self, key: str, what: tuple[str, ...], progress=None, can_yield: bool = False) -> tuple[str, ...]:
        d = self.data(key)
        lib = self.app.library_info(key) or {}

        def step(w, done, total):
            p = {"library": key, "what": w, "done": done, "total": total}
            with self.lock:
                self.progress = p
            if progress:
                progress(p)
        yield_now = (lambda: self._yield_to_other(key)) if can_yield else None
        at = {"records": "records_at", "files": "files_at", "details": "details_at", "mirror": "mirror_at"}
        done: list[str] = []
        changed = False
        for w in [w for w in ALL if w in what]:
            if done and yield_now is not None and yield_now():
                break  # (between steps: the library a page has open is read first)
            first = not getattr(d, at[w])  # (never read, or known to have changed — said stale, Scan Again: worked out again whatever it finds)
            if w == "records":
                step("records", 0, 0)
                leaf = TOP_TYPE.get(lib.get("type") or "")
                old = d.records
                try:
                    recs = self.app.plex(timeout=120).library_items(key, leaf) if leaf else []
                    d.records = recs
                    d.errors.pop("records", None)
                    # and the library's files as Plex lists them now (app.files, the listing the files below come from): a
                    # file Plex added since reaches every tool's rows without waiting for the app's own next listing
                    if hasattr(self.app, "refresh_library_files"):
                        self.app.refresh_library_files(key)
                except Exception as e:  # noqa: BLE001 — tried at this stamp: said, and not again until it moves (or the age is up)
                    d.errors["records"] = str(e)
                d.records_at, d.plex_changed = time.time(), str(lib.get("changed") or "")
                changed = changed or first or d.records != old
            elif w == "files":
                old = (d.files, d.unlisted, d.tree)
                self._gather_files(d, lib, step)
                changed = changed or first or (d.files, d.unlisted, d.tree) != old
            elif w == "details":
                old = dict(d.details)
                whole = self._gather_details(d, step, yield_now)
                changed = changed or first or d.details != old
                if not whole:
                    break  # (the rest of the pages read after the other library)
            else:
                old = d.mirror
                self._gather_mirror(d, lib, step)
                changed = changed or first or d.mirror != old
            done.append(w)
        if done or changed:
            if changed:
                d.stamp += 1
            self.app._save_json(f"libdata-{key}.json", d.to_json())
            for fn in list(self.listeners):
                try:
                    fn(key, tuple(done))
                except Exception:  # noqa: BLE001
                    pass
        return tuple(w for w in what if w not in done)

    def _gather_details(self, d: LibraryData, step, yield_now=None) -> bool:
        """Each file's item page: one request per item, paced; an item whose page can't be read keeps what it had. False
        when broken off for the library a page has open (``yield_now``): the pages read so far kept, and not read again
        when it picks up."""
        rks = sorted({f["rk"] for f in d.files if f.get("rk")})
        client = self.app.plex(timeout=60)
        for i, rk in enumerate(rks):
            if i % 10 == 0:
                step("details", i, len(rks))
                if i and yield_now is not None and yield_now():
                    return False
            if rk in d.reading_details:
                continue
            try:
                got = client.item_detail(rk)
            except Exception as e:  # noqa: BLE001 — Plex away: tried again next time
                d.errors["details"] = str(e)
                d.reading_details = set()
                return True
            if got is not None:
                d.details[rk] = {"streams": got.get("streams") or [], "locked": got.get("locked") or [], "fields": got.get("fields") or {},
                                 "tags": got.get("tags") or {}, "updated_at": got.get("updated_at") or 0}
            d.reading_details.add(rk)
            time.sleep(PACE)
        d.details = {k: v for k, v in d.details.items() if k in set(rks)}
        d.details_at = time.time()
        d.reading_details = set()
        d.errors.pop("details", None)
        step("details", len(rks), len(rks))
        return True

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

    def mirror_of(self, key: str) -> tuple:
        """(the mirror's library paired with this one, "<the mirror's machine id>/<that library>"), or (None, "")
        with no mirror chosen, or the library not paired — or (None, None) while that isn't known: the mirror's libraries
        never read (it couldn't be reached), never taken for "not paired" (what was read of it kept)."""
        other = getattr(self.app, "mirrors", None)
        if other is None or other.state_of(str(key)) != "mirror":
            return None, ""
        sid = other.server_of(str(key))
        if not sid or not other.configured(sid):
            return None, ""
        if not other.known(sid):
            try:
                other.libraries(sid)
            except Exception:  # noqa: BLE001
                pass
            if not other.known(sid):
                return None, None
        try:
            there = other.mirror_for(str(key))
        except Exception:  # noqa: BLE001 — the mirror away: as unpaired, until it's back
            return None, ""
        if there is None:
            return None, ""
        return there, f"{sid}/{there.key}"

    def _gather_mirror(self, d: LibraryData, lib: dict, step) -> None:
        """The mirror's side (see the module's docstring): both servers' items, each one's own page where the two
        differ — read again only when the listing says it changed — and the other site's files. A mirror that can't be read
        keeps what was read of it (said in the errors), unless it was of another pairing."""
        from .sync import DETAIL_KEYS, LEAVES, _Side, compare_library
        there, pairing = self.mirror_of(d.key)
        if pairing is None:  # (what's paired isn't known: what was read of the mirror kept as it is)
            return
        if there is None:
            d.mirror, d.mirror_for, d.mirror_changed, d.mirror_at = None, "", "", time.time()
            d.errors.pop("mirror", None)
            return
        app = self.app
        typ = lib.get("type") or ""
        leaves = LEAVES.get(typ, ())
        old = d.mirror if d.mirror_for == pairing else None
        keep = lambda it: {k: it[k] for k in MIRROR_KEYS if k in it}  # noqa: E731
        try:
            step("mirror", 0, 0)
            here_client = app.plex(timeout=120)
            # (the records, read just now, are the films' and shows' items already)
            here = [keep(it) for t in leaves for it in (d.records if t == TOP_TYPE.get(typ) and d.records_at and "records" not in d.errors else here_client.library_items(d.key, t))]
            app.mirrors.run(there.server, lambda c: c.identity(), timeout=20)  # (reached — at another of its addresses if need be)
            there_client = app.mirrors.client(there.server, timeout=120)
            theirs = [keep(it) for t in leaves for it in there_client.library_items(there.key, t)]
            # the items on both servers the two differ on, either way round: each one's own page (which fields are edited,
            # the summary) — an item on one server only has nothing there to be compared with
            locs = [r[0] for r in d.roots] or [loc.rstrip("/") for loc in lib.get("locations") or []]
            a, b = _Side(here, locs), _Side(theirs, there.locations)
            want_here, want_there = set(), set()
            for got, mine, other_side in ((compare_library(a, b, {}, {}, None), want_here, want_there), (compare_library(b, a, {}, {}, None), want_there, want_here)):
                for row in got["items"]:
                    if row["src_items"] and row["dst_items"]:
                        mine.update(it["key"] for it in row["src_items"])
                        other_side.update(it["key"] for it in row["dst_items"])
            pages = {}
            for side, client, want, items in (("pages_here", here_client, want_here, {it["key"]: it for it in here}),
                                              ("pages_there", there_client, want_there, {it["key"]: it for it in theirs})):
                was = (old or {}).get(side) or {}
                pages[side] = {}
                for i, k in enumerate(sorted(want)):
                    if i % 10 == 0:
                        step("mirror", i, len(want))
                    seen = was.get(k)
                    if seen and seen.get("updated_at") and seen["updated_at"] == (items.get(k) or {}).get("updated_at"):
                        pages[side][k] = seen  # (unchanged since its page was read)
                        continue
                    got = client.item_detail(k)
                    if got is not None:
                        pages[side][k] = {x: got[x] for x in DETAIL_KEYS + ("updated_at",) if x in got}
                    time.sleep(PACE)
        except Exception as e:  # noqa: BLE001 — the mirror away: said, and what was read of it kept
            d.errors["mirror"] = str(e)
            d.mirror = old
            # (tried: not again until the library there changes, or the age is up — or Scan Again)
            d.mirror_for, d.mirror_changed, d.mirror_at = pairing, str(getattr(there, "changed", "") or ""), time.time()
            return
        # the other site's files, through the Plex Cache there
        disk, why = None, "none"
        links = getattr(app, "peerlinks", None)
        link = links.for_library(d.key) if links is not None else None
        if link is not None and link.talking():
            try:
                got = link.files(there.key)
                if got.get("ready"):
                    disk, why = [[str(r), int(s or 0), float(m or 0)] for r, s, m in got.get("files") or []], None
                else:
                    why = "reading"
            except Exception as e:  # noqa: BLE001
                why = str(e) or "link"
        elif link is not None and link.known():
            why = "link"
        d.mirror = {"library": there.key, "title": there.title, "locations": list(there.locations), "here": here, "there": theirs,
                    "pages_here": pages["pages_here"], "pages_there": pages["pages_there"], "disk": disk, "disk_why": why}
        d.mirror_for, d.mirror_changed, d.mirror_at = pairing, str(getattr(there, "changed", "") or ""), time.time()
        d.errors.pop("mirror", None)
        step("mirror", 1, 1)

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
            what, moved = [], []  # what's read again; what of it is known to have changed (said stale meanwhile)
            # the records: never read, Plex's change stamp moved, or old
            rec_moved = not d.records_at or (lib.get("changed") or "") != d.plex_changed
            if rec_moved or now - d.records_at > RECORDS_TTL:
                what.append("records")
            # the files: never read or made stale (Plex's listing read again, a tool's work), a folder changed, old — or the records changed
            files_moved = not d.files_at or rec_moved or self.dirs_changed(key)
            if files_moved or what or now - d.files_at > FILES_TTL:
                what.append("files")
            # the item pages: never read, the records changed (an item edited or re-probed moves the library's stamp), or old
            if not d.details_at or "records" in what or now - d.details_at > RECORDS_TTL:
                what.append("details")
            # the mirror's side: never read, the pairing changed, the records changed (or were read since), the library there
            # changed, or old — none of it while there's no pair, nor was
            there, pairing = self.mirror_of(key)
            mirror_moved = False
            if pairing is not None:  # (what's paired not known yet: the mirror's side left as it is, looked at again next time)
                there_moved = there is not None and str(getattr(there, "changed", "") or "") != d.mirror_changed
                mirror_moved = not d.mirror_at or ((pairing or d.mirror_for) and (pairing != d.mirror_for or rec_moved or there_moved))
                if mirror_moved or ((pairing or d.mirror_for) and ("records" in what or d.records_at > d.mirror_at or now - d.mirror_at > MIRROR_TTL)):
                    what.append("mirror")
            moved = [w for w, m in (("records", rec_moved), ("files", files_moved), ("details", not d.details_at or rec_moved), ("mirror", mirror_moved)) if m and w in what]
            if what:
                changed.append(key)
                for w in what:  # (read whatever its age; said stale meanwhile when it's known to have changed — read again only
                    if w in moved:  # for its age, it stays in while it's read)
                        self._stale(d, w)
                    else:
                        setattr(d, w + "_at", min(getattr(d, w + "_at"), now - 7 * 86400))
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
        elif what == "mirror":
            d.mirror_at = 0.0
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
                    "libraries": {k: {"records_at": d.records_at, "files_at": d.files_at, "details_at": d.details_at, "mirror_at": d.mirror_at, "ready": d.ready, "stamp": d.stamp, "errors": dict(d.errors),
                                      "files": len(d.files), "unlisted": len(d.unlisted), "records": len(d.records), "details": len(d.details)} for k, d in self.libs.items()}}

    def why_not_ready(self, key: str):
        """What a page says while a library's data isn't gathered yet (an M), or None."""
        d = self.data(key)
        if d.ready:
            return None
        with self.lock:
            running = self.current == str(key)
        return M("Reading the library’s records and files…") if running else M("The library’s records and files are read first: a moment.")

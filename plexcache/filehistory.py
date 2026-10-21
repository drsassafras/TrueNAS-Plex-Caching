"""Each file's history with the optimizer, kept for good (file-history.json in the config folder),
for the Metrics page: what caching kept of each file when it was first seen, against now.

A file's **original** is what the always-cached blocks held (filemap.cache_mix) the first time
this app saw it, before the optimizer touched it. Each time it's optimized again, only "now"
moves: the original stays, so repeat optimizing widens the gap rather than closing it.

What a file that changes (a new size, modification time or inode) becomes:

* **written by the optimizer here** (processed()): the same file, its "now" moved on;
* **still tagged by the optimizer** (Syncthing brought it from another NAS, say): the same file,
  its original kept. Laid out differently from before (another optimizer version, another cover
  art choice): it was optimized elsewhere, and the job log gets a "discovered" line for it, its
  before and after, as an optimizing job's file would;
* **not tagged** (Sonarr or Radarr replaced it with a new download): a new file, its original
  taken again.

A file first seen already tagged was optimized somewhere else, before this app saw it: its
original isn't known, and it's left out of the averages (counted as "unknown"). A file gone from
the libraries is left out too, but kept: it may come back (a sync that deletes, then copies).

Each entry: {"lib", "id" ([size, mtime_ns, ino]), "ours" (our tags in it), "origin" ("seen" or
"unknown"), "original" and "now" (snapshot()), "first" (when first seen), "runs" (times optimized,
here or elsewhere), "gone" (when it left the libraries, or None)}.
"""

from __future__ import annotations

import json
import os
import threading
import time

STORE = "file-history.json"
TAG = "PLEXCACHE_OPTIMIZER"  # the optimizer's version, in every file it laid out
MIX_FIELDS = ("video", "meta", "unused", "video_disk", "meta_disk", "unused_disk", "blocks", "record")
KEEP = MIX_FIELDS + ("kinds", "lz4", "round_disk")


def snapshot(m: dict | None, comp: str | None = None) -> dict | None:
    """What's kept of a file's layout (filemap.describe) for its history: the always-cached blocks'
    mix (with each kind of metadata's bytes) and the video in its end blocks — enough for a chart
    row (projection.view) — its size and space on the pool, and ``comp``: the compression it's stored
    with (projection.family; None: as the mix has it, ZSTD). None without a mix."""
    from .projection import tail_video
    mix = (m or {}).get("mix")
    if not mix:
        return None
    out = {"mix": {k: v for k, v in mix.items() if k in KEEP}, "tail": tail_video(m), "size": m.get("size"), "disk": m.get("disk")}
    if comp:
        out["comp"] = comp
    return out


def same_mix(a: dict | None, b: dict | None) -> bool:
    a, b = (a or {}).get("mix") or {}, (b or {}).get("mix") or {}
    return all(a.get(k) == b.get(k) for k in MIX_FIELDS)


def row(snap: dict | None) -> dict | None:
    """A snapshot as a chart row (projection.view)."""
    from .projection import existing, view
    if not snap:
        return None
    mix = existing(snap["mix"], snap.get("comp") or "zstd")
    return view(mix, {k: v[0] for k, v in (mix.get("kinds") or {}).items()}, snap.get("tail"))


def file_views(before: dict | None, after: dict | None) -> dict | None:
    """A job log line's chart: the file just before the optimizer wrote it, and just after."""
    b, a = row(before), row(after)
    return {"before": b, "after": a} if b and a else None


def scaled(v: dict | None, n: int) -> dict | None:
    """A chart row's numbers divided by ``n``: a library's total as the average per file."""
    if not v or n <= 1:
        return v
    out = {}
    for k, x in v.items():
        if isinstance(x, dict):
            out[k] = {kk: round(nn / n) if isinstance(nn, (int, float)) and not isinstance(nn, bool) else nn for kk, nn in x.items()}
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            out[k] = round(x / n)
        else:
            out[k] = x
    return out


class FileHistory:
    def __init__(self, config_dir: str, log=None):
        self.path = os.path.join(config_dir, STORE)
        self.lock = threading.Lock()
        self.dirty = False
        self.rev = 0  # changes as the history does (the Metrics page's sums are kept until then)
        self.log = log  # log(entry): the job log's add(), for the files found optimized elsewhere
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.files: dict[str, dict] = json.load(fh).get("files") or {}
        except (OSError, ValueError, AttributeError):
            self.files = {}

    # ------------------------------------------------------------ what's seen
    def seed(self, path: str, key: str, before: dict | None, m: dict | None, ident: list) -> None:
        """A file optimized before this history was kept: its mix before the optimizer first wrote it
        (kept with its layout then, filemap.FileMaps.before) as its original."""
        if not before:
            return
        with self.lock:
            if path in self.files:
                return
            # (its tags: None, taken as they are when it's next seen — its layout may be from before the optimizer wrote it)
            self.files[path] = {"lib": str(key), "id": list(ident), "ours": None, "origin": "seen", "first": None,
                                "original": {"mix": before, "tail": None}, "now": snapshot(m), "runs": 1, "gone": None}
            self._changed()

    def observe(self, path: str, key: str, m: dict | None, ident: list, ours: dict | None, now: float | None = None,
                comp: str | None = None) -> str | None:
        """The file as it is now (``m``: its layout, ``ident``: [size, mtime_ns, ino], ``ours``: our tags
        in it). What it turned out to be: "new", "unknown" (first seen already optimized), "replaced"
        (a new download), "synced" (the same file, brought back as it was), "discovered" (optimized
        elsewhere), or None (as it was)."""
        now = time.time() if now is None else now
        ours, ident, snap = dict(ours or {}), list(ident), snapshot(m, comp)
        with self.lock:
            e = self.files.get(path)
            if e is None:
                origin = "unknown" if ours.get(TAG) else "seen"
                self.files[path] = {"lib": str(key), "id": ident, "ours": ours, "origin": origin, "first": now,
                                    "original": snap if origin == "seen" else None, "now": snap, "runs": 0, "gone": None}
                self._changed()
                return "new" if origin == "seen" else "unknown"
            was_gone, moved = e.get("gone"), e.get("lib") != str(key)
            e["gone"], e["lib"] = None, str(key)
            if e.get("id") == ident:
                if e.get("ours") is None:  # seeded, and its layout is the file's as it is: its tags are known now
                    e["ours"], moved = ours, True
                if (snap and not e.get("now")) or was_gone or moved:
                    e["now"] = snap or e.get("now")
                    if e.get("origin") == "seen" and not e.get("original"):
                        e["original"] = snap
                    self._changed()
                return None
            before = e.get("now")
            if e.get("ours") is None:  # seeded: its layout then may be from before the optimizer wrote it — taken as it is now
                e.update(id=ident, ours=ours, now=snap or before)
                self._changed()
                return "synced"
            if not ours.get(TAG):  # a new file in its place (or our tags taken out): its history starts again
                e.update(id=ident, ours=ours, origin="seen", first=now, original=snap, now=snap, runs=0)
                self._changed()
                return "replaced"
            if ours == e["ours"] and same_mix(before, snap):
                e.update(id=ident, ours=ours, now=snap or before)
                self._changed()
                return "synced"
            prev_tags = dict(e.get("ours") or {})
            e.update(id=ident, ours=ours, now=snap or before, runs=(e.get("runs") or 0) + 1)
            self._changed()
        self._log_discovered(path, key, before, snap, prev_tags, ours, now)
        return "discovered"

    def processed(self, path: str, key: str, before: dict | None, after: dict | None, ident: list, ours: dict | None,
                  new_path: str | None = None, now: float | None = None, comp_before: str | None = None,
                  comp_after: str | None = None) -> None:
        """The optimizer here has just written the file (``before``, ``after``: its layout then and now;
        ``new_path``: the MKV a file was converted to, which carries its history on)."""
        now = time.time() if now is None else now
        with self.lock:
            e = self.files.pop(path, None) if new_path else self.files.get(path)
            if e is None:
                snap = snapshot(before, comp_before)
                e = {"lib": str(key), "ours": {}, "origin": "seen", "first": now, "original": snap, "runs": 0}
            e.update(id=list(ident), ours=dict(ours or {}), now=snapshot(after, comp_after) or e.get("now"), runs=(e.get("runs") or 0) + 1,
                     gone=None, lib=str(key))
            self.files[new_path or path] = e
            self._changed()

    def missing(self, keep: set, now: float | None = None) -> None:
        """Files no longer in any library: kept, but left out of the averages until they come back."""
        now = time.time() if now is None else now
        with self.lock:
            for p, e in self.files.items():
                if p not in keep and not e.get("gone"):
                    e["gone"] = now
                    self._changed()

    def _changed(self) -> None:
        self.dirty = True
        self.rev += 1

    def _log_discovered(self, path, key, before, after, tags_before, tags_after, now) -> None:
        """A file optimized elsewhere, in the job log: one "discovered" job a day per library."""
        if not self.log:
            return
        day = time.strftime("%Y%m%d", time.localtime(now))
        b, a = before or {}, after or {}
        self.log({"type": "file", "job": f"discovered-{key}-{day}", "kind": "discovered", "library": str(key), "at": now,
                  "file": path, "result": "discovered", "size_before": b.get("size"), "size_after": a.get("size"),
                  "disk_before": b.get("disk"),
                  "rs_before": (b.get("mix") or {}).get("record"), "rs_after": (a.get("mix") or {}).get("record"),
                  "version_before": tags_before.get(TAG), "version": tags_after.get(TAG), "layout": True,
                  "mix_before": before, "mix_after": after})
        if a.get("disk") is not None:  # (its space on the pool: a line of its own, as an optimized file's)
            self.log({"type": "disk", "job": f"discovered-{key}-{day}", "file": path, "disk_after": a["disk"], "at": now})

    # ------------------------------------------------------------ reading it
    def known(self, paths) -> int:
        with self.lock:
            return sum(1 for p in paths if p in self.files)

    def original(self, path: str) -> dict | None:
        """The file's original snapshot, if it's known and the file has changed since."""
        with self.lock:
            e = self.files.get(path) or {}
            if e.get("origin") != "seen" or not e.get("runs") or not e.get("original"):
                return None
            return e["original"]

    def library(self, key: str) -> dict:
        """A library's optimized files, for the Metrics page: how many, how many of unknown origin (left
        out), and the average per file of what caching kept of them first ("original") and now."""
        from .projection import add_view
        key = str(key)
        orig = cur = None
        n = unknown = 0
        with self.lock:
            entries = [dict(e) for e in self.files.values() if e.get("lib") == key and not e.get("gone")]
        for e in entries:
            if e.get("origin") != "seen":
                unknown += 1
                continue
            if not e.get("runs") or not e.get("original") or not e.get("now"):
                continue
            o, c = row(e["original"]), row(e["now"])
            if not o or not c:
                continue
            orig, cur, n = add_view(orig, o), add_view(cur, c), n + 1
        return {"files": n, "unknown": unknown, "original": scaled(orig, n), "now": scaled(cur, n),
                "total": {"original": orig, "now": cur} if n else None}

    def save_soon(self, every: float = 30) -> None:
        """Saved now unless it was saved less than ``every`` seconds ago (an optimizing job's files)."""
        if time.time() - getattr(self, "_saved", 0) >= every:
            self.save()

    def save(self) -> None:
        self._saved = time.time()
        with self.lock:
            if not self.dirty:
                return
            data = json.dumps({"version": 1, "files": self.files}, separators=(",", ":"))
            self.dirty = False
        tmp = f"{self.path}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, self.path)
        except OSError:
            self.dirty = True  # the config folder isn't writable: Health says so

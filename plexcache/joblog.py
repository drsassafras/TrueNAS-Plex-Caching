"""The job log (Processes page): what every file job did, kept for good.

One line of JSON per entry, appended as it happens (job-log.jsonl in the config folder), so a
restart loses nothing:

* a job's events — "started", "resumed" (after a restart: where it was, as "resume point"),
  "paused" / "unpaused" (and why), "finished" or "cancelled" (with its counts);
* each file a job finished — its name, how long it took, its size before and after, the
  verification outcome (passed, failed, skipped), its record size and compression before and
  after, whether its layout was optimized (and the optimizer's version, before and after), and
  what happened to its cover art;
* a written file's space on the pool, once ZFS has compressed it ("disk", a little later);
* the files a job set out to do ("plan", once it has checked which need it — again when it carries
  on after a restart), so its list shows the ones not tried yet too;
* a written file's always-cached blocks before and after ("mix_before", "mix_after":
  filehistory.snapshot), for its chart;
* the files found optimized elsewhere (Syncthing brought them from another NAS): "discovered", one
  job a day per library, each with its before and after (filehistory.py).

Each job's tally (jobs()) adds its files up as it goes: record sizes and compression put right,
space saved, posters removed, added and compressed.

Nothing is dropped: it's the history of every file optimized (the Metrics page's sums come from
filehistory.py). export() gives every entry as CSV or JSON.
"""

import csv
import io
import json
import os
import threading
import time
from .paging import sort_key

LOG = "job-log.jsonl"
FILE_FIELDS = ("at", "job", "kind", "library", "file", "label", "result", "seconds", "size_before", "size_after",
               "disk_before", "disk_after", "verified", "reason", "rs_before", "rs_after", "compression_before",
               "compression_after", "layout", "version_before", "version", "cover", "images_before", "images_after",
               "images_saved", "poster_added", "new")
WROTE = ("optimized", "rewritten", "converted")


def _pct(b, a):
    return (a - b) / b if a is not None and b else None


# The files list, sorted by a column (stable, so the order done within each)
RESULT_ORDER = {"failed": 0, "skipped": 1, "rewritten": 2, "converted": 3, "optimized": 3, "discovered": 3, "already": 4, "copied": 5}
SORTS = {
    # by title (each edition's files together, in file order), the default
    "file": lambda f: (sort_key(f.get("label") or os.path.basename(f.get("file") or "")), f.get("file") or ""),
    "result": lambda f: RESULT_ORDER.get(f.get("result"), 9),  # failed at the top, then the rest, untried last
    "time": lambda f: -(f.get("seconds") or 0),  # slowest first
    # most smaller first, then the biggest original
    "size": lambda f: (_pct(f.get("size_before"), f.get("size_after")) is None,
                       _pct(f.get("size_before"), f.get("size_after")) or 0, -(f.get("size_before") or 0)),
    "verified": lambda f: {"failed": 0, "passed": 1, "skipped": 2, "not needed": 3}.get(f.get("verified"), 9),
    "rs": lambda f: -(f.get("rs_before") or 0),
    # most saved on disk first
    "compression": lambda f: (_pct(f.get("disk_before"), f.get("disk_after")) is None, _pct(f.get("disk_before"), f.get("disk_after")) or 0),
    "optimized": lambda f: (not f.get("layout"), str(f.get("version_before") or "")),
    "cover": lambda f: -((f.get("images_before") or 0) - (f.get("images_after") or 0) + (1 if f.get("poster_added") else 0)),
}


def tally_file(t: dict, f: dict) -> None:
    """Add one file to a job's tally."""
    if f.get("result") not in WROTE:
        return
    t["written"] = t.get("written", 0) + 1
    if f.get("rs_after") and f.get("rs_before") != f.get("rs_after"):
        t["rs_fixed"] = t.get("rs_fixed", 0) + 1
    if f.get("compression_after") and f.get("compression_before") != f.get("compression_after"):
        t["compression_set"] = t.get("compression_set", 0) + 1
    if f.get("size_before") is not None and f.get("size_after") is not None:
        t["size_saved"] = t.get("size_saved", 0) + f["size_before"] - f["size_after"]
    if f.get("disk_before") is not None and f.get("disk_after") is not None:
        t["disk_saved"] = t.get("disk_saved", 0) + f["disk_before"] - f["disk_after"]
    b, a = f.get("images_before") or 0, f.get("images_after") or 0
    if a < b:
        t["posters_removed"] = t.get("posters_removed", 0) + b - a
    if f.get("poster_added"):
        t["posters_added"] = t.get("posters_added", 0) + 1
    if f.get("images_saved"):
        t["posters_saved"] = t.get("posters_saved", 0) + f["images_saved"]


class JobLog:
    def __init__(self, config_dir: str):
        self.path = os.path.join(config_dir, LOG)
        self.lock = threading.Lock()
        self._cache: tuple | None = None  # (mtime, size, entries)

    def add(self, entry: dict) -> None:
        entry = dict(entry, at=entry.get("at") or time.time())
        line = json.dumps(entry, separators=(",", ":"), default=str)
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass  # the config folder isn't writable: Health says so; the job carries on

    def entries(self) -> list[dict]:
        """Every entry, oldest first (read again only when the file changed)."""
        try:
            st = os.stat(self.path)
        except OSError:
            return []
        key = (st.st_mtime_ns, st.st_size)
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        out = []
        with self.lock, open(self.path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a line cut short (a crash mid-write)
        self._cache = (key, out)
        return out

    def jobs(self, limit: int = 200) -> list[dict]:
        """Each job, newest first: {"job", "kind", "mode", "library", "title", "started", "ended", "status",
        "events": [...], "counts", "files"}."""
        by: dict[str, dict] = {}
        files: dict[str, dict] = {}  # job -> {file: its latest entry (a retry's replaces the failure)}
        rows = self.entries()
        disk = self._disk(rows)
        for e in rows:
            jid = e.get("job")
            if not jid:
                continue
            j = by.setdefault(jid, {"job": jid, "kind": e.get("kind"), "mode": e.get("mode"), "library": e.get("library"), "tool": e.get("tool"),
                                    "title": e.get("title"), "started": e.get("at"), "ended": None, "status": "running",
                                    "events": [], "counts": {}, "files": 0, "failed": 0, "tally": {}})
            if e.get("type") in ("disk", "plan"):
                if e.get("type") == "plan":
                    j.setdefault("_planned", set()).update(e.get("files") or [])
                continue
            if e.get("type") == "file":
                if e.get("result") in ("reverted", "revert_failed"):
                    continue  # (the undoing of a tool's run: the reverted event says so; the file's own entry stands)
                files.setdefault(jid, {})[e.get("file")] = e
                if j["kind"] == "discovered":  # (found, not run: done as each file is found)
                    j.update(status="finished", ended=e.get("at"))
                continue
            j["events"].append({k: e.get(k) for k in ("event", "at", "done", "total", "why", "counts", "backup") if e.get(k) is not None})
            if e.get("event") == "backup_failed":
                j.update(status="failed", ended=e.get("at"))
            if e.get("event") in ("reverted", "rolled_back"):
                j["reverted"] = e.get("at")
                j["undone_how"] = e.get("event")
            if e.get("event") == "revert_failed":
                j["revert_failed"] = e.get("at")  # (nothing put back: the run still stands, and can be tried again)
            for k in ("mode", "library", "title", "tool"):
                j[k] = e.get(k) or j[k]  # the latest known (a title may be missing while Plex wasn't reachable)
            if e.get("event") in ("finished", "cancelled") and j["status"] != "failed":  # (a run that stopped at its backup stays "failed")
                j.update(status=e["event"], ended=e.get("at"), counts=e.get("counts") or {})
        for jid, mine in files.items():
            j = by[jid]
            counts = {}
            for e in mine.values():
                if e.get("result") == "cancelled":
                    continue  # never tried: not counted
                if e.get("result") in ("reverted", "revert_failed"):
                    continue  # (the undoing: said by the reverted event, not counted as files tried)
                j["files"] += 1
                j["failed"] += e.get("result") == "failed"
                tally_file(j["tally"], dict(e, disk_after=disk.get((jid, e.get("file")))))
                r = {"already": "optimized"}.get(e.get("result"), e.get("result"))
                counts[r] = counts.get(r, 0) + 1
            if j["kind"] == "optimize" and j["counts"]:
                j["counts"] = dict(j["counts"], **{k: counts.get(k, 0) for k in ("optimized", "rewritten", "skipped", "failed")})
        for j in by.values():  # every file it set out to do (tried or not)
            j["planned"] = len(j.pop("_planned", set()) | set(files.get(j["job"], {})))
        for j in by.values():  # a tool's run: whether Revert is on (its backup still there, nothing reverted yet)
            if j["kind"] == "tool":
                bk = next((ev.get("backup") for ev in j["events"] if ev.get("event") == "backup"), None)
                j["backup"] = bk
                j["revertable"] = bool(bk and j["status"] in ("finished", "cancelled") and not j.get("reverted") and j["files"] > j["failed"]
                                       and (not bk.get("until") or bk["until"] > time.time()))
        # a job that ended without trying a single file (cancelled straight away, or nothing to do): not shown — but a
        # tool's run that stopped at the backup is: its reason is what you need to see
        by = {k: j for k, j in by.items() if j["files"] or j["status"] in ("running", "failed")}
        return sorted(by.values(), key=lambda j: j["started"] or 0, reverse=True)[:limit]

    def files(self, job: str, offset: int = 0, limit: int = 200, sort: str = "", fill=None) -> dict:
        """A job's files, a page at a time: the ones tried, in the order done, then the ones it set out
        to do and hasn't tried (result "pending") — or sorted by a column (SORTS). ``fill(rows)``
        adds what the log doesn't hold (titles, posters, how untried files are now) before sorting."""
        every = self.entries()
        disk = self._disk(every)
        latest: dict[str, dict] = {}  # a retry's entry replaces the failure, in its place
        planned: dict[str, None] = {}
        for e in every:
            if e.get("job") != job:
                continue
            if e.get("type") == "file":
                if e.get("result") in ("reverted", "revert_failed") and e.get("file") in latest:  # (the undoing: its result and reason on the file's own entry)
                    latest[e["file"]] = dict(latest[e["file"]], result=e["result"], reason=e.get("reason"), reverted_at=e.get("at"))
                    continue
                latest[e.get("file")] = e  # (a dict keeps the first one's place)
            elif e.get("type") == "plan":
                planned.update(dict.fromkeys(e.get("files") or []))
        rows = [dict(e, disk_after=disk.get((job, e.get("file")))) for e in latest.values()]
        rows += [{"type": "file", "job": job, "file": p, "result": "pending"} for p in planned if p not in latest]
        failed = sum(e.get("result") == "failed" for e in rows)
        if fill:
            fill(rows)
        if sort in SORTS:
            rows = sorted(rows, key=SORTS[sort])  # (stable: the order done, within each)
        from .paging import page_ranges
        label = (lambda r: str(r.get("result") or "")) if sort == "result" else (lambda r: str(r.get("title") or os.path.basename(str(r.get("file") or ""))))
        first = next((e for e in every if e.get("job") == job), {})
        return {"total": len(rows), "failed": failed, "files": rows[offset:offset + limit], "pages": page_ranges([label(r) for r in rows], limit),
                "kind": first.get("kind"), "tool": first.get("tool")}

    @staticmethod
    def _disk(rows: list[dict]) -> dict:
        """(job, file) -> its space on the pool once written (logged a little after the file)."""
        return {(e.get("job"), e.get("file")): e.get("disk_after") for e in rows if e.get("type") == "disk"}

    def export(self, fmt: str = "csv") -> tuple[bytes, str]:
        """Every entry: JSON (everything), or CSV (the files, one row each, plus the jobs' events)."""
        rows = self.entries()
        if fmt == "json":
            return json.dumps(rows, indent=1, default=str).encode(), "application/json"
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(("type", "event", "time") + FILE_FIELDS[1:] + ("done", "total", "why"))
        disk = self._disk(rows)
        for e in rows:
            if e.get("type") in ("disk", "plan"):
                continue  # in its file's row; the plan is the jobs' (JSON has it)
            if e.get("type") == "file":
                e = dict(e, disk_after=disk.get((e.get("job"), e.get("file"))))
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("at") or 0))
            w.writerow([e.get("type"), e.get("event") or "", when] + [_cell(e.get(k)) for k in FILE_FIELDS[1:]] +
                       [_cell(e.get(k)) for k in ("done", "total", "why")])
        return buf.getvalue().encode(), "text/csv"


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        text = json.dumps(v, default=str)
    else:
        text = str(v)
    # A cell starting like a formula (a file named "=HYPERLINK(…)") would run as one in a spreadsheet:
    # a leading ' keeps it text (OWASP's CSV injection advice). Numbers aren't touched.
    if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not isinstance(v, (int, float)):
        text = "'" + text
    return text

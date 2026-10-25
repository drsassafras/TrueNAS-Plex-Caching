"""The tools (the Tools pages): each scans a library for what it could do, shows the plan, and does
the rows you tick — in the background, one row at a time, with the result of each kept. Nothing
happens until you press Apply. A tool knows about the mirrored server (plex2.py) where that matters:
what the same step does there, or what to wait for there.

A scan is a function over the library's data as the data layer has it (libdata.LibraryData: Plex's
records, the files on disk, the folder tree) — it asks Plex nothing and stats nothing itself, so one
gathering serves every tool, a scan of fresh data is instant, and a change the data layer sees
reaches the tool's rows and its count (analysis, counts) without a scan. The tests hold every tool
to it (test_tools_read_through_the_data_layer).

Options are per tool and per library (``options``, the ``tools`` settings section): each names the
library kinds it's for, and a library shows only those. A scan finds everything the tool could do
whatever the options say — each row names the issues it's about (``issues``, option ids) — and the
options decide which rows are live: counted (counts), flagged on Library Health (flags) and open to
Apply (apply_async). So a switch takes effect at once, over the rows already found, with no scan;
the page draws the rows of a switched-off issue grey, after the live ones. Each tool also says what it is to Library Health:
its title, the page it lives on, what its chip there means (flag_help), and which items its rows
concern (flags), where the view shows one chip per tool.

Files are only ever touched through the optimizer service (optimize/files.py): the one part of
Plex Cache that may write to the media. Plex is asked through its API, here and on the mirror.

A tool's state (the last scan, the results, what it's still waiting for) is kept in tools-<name>.json.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import urllib.error

from ..i18n import M
from ..jobs import Job

KEEP_RESULTS = 500
MAX_ROWS = 400


class Tool:
    name = ""
    title = M("Tool")                      # as the Tools page and Library Health name it
    page = ""                               # its page ("/merge")
    flag_help = M("")                       # what its chip on Library Health means
    applies = ("movie", "tv", "audio")      # the library kinds it works on
    # the options, per library: [{"id", "name": M, "help": M, "kinds": the library kinds it's for, "default"}] — a switch
    # (default True/False: whether rows about that issue are live) or, with "choices": [(value, M)], a way to do it
    options: list[dict] = []
    scan_version = 1  # (raised when a tool's rows change shape: an older scan on disk is dropped)

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        saved = app._load_json(f"tools-{self.name}.json") or {}
        self.scan_result: dict | None = saved.get("scan") if (saved.get("scan") or {}).get("v") == self.scan_version else None
        self.results: list[dict] = saved.get("results") or []
        self.extra: dict = saved.get("extra") or {}  # what a tool keeps besides (its follow-ups)
        self.running = False
        self.progress: dict | None = None
        self.error: str | None = None
        self.error_at: float = 0.0  # (when the last scan failed: the page tells it from an earlier one's)
        self._analysis: dict[str, tuple[int, dict]] = {}  # library -> (data stamp, the scan over that data)

    def save(self) -> None:
        with self.lock:
            data = {"scan": self.scan_result, "results": self.results[-KEEP_RESULTS:], "extra": self.extra}
        self.app._save_json(f"tools-{self.name}.json", data)

    # ------------------------------------------------------------ to be given by each tool
    def scan(self, data) -> dict:
        """What the tool could do in a library, from its data (libdata.LibraryData): {"library", "rows":
        [{"id", "title", "rk" | "rks", "issues": [option ids], ...}], ...}, whatever the options say. Pure: reads
        ``data``, asks nothing else."""
        raise NotImplementedError

    # ------------------------------------------------------------ the options, per library
    def options_for(self, kind: str | None) -> list[dict]:
        """The options a library of this kind has (all of them when the kind isn't known)."""
        return [o for o in self.options if kind is None or kind in o["kinds"]]

    def enabled(self, library: str | None) -> dict:
        """Each option's value for a library: its default, under what's saved for every library ("*") and for this one
        (settings tools.<name>). A switch is True or False; a choice is one of its values."""
        kind = (self.app.library_info(library) or {}).get("kind") if library else None
        saved = self.app.settings.section("tools").get(self.name) or {}
        out = {}
        for o in self.options_for(kind):
            v = o["default"]
            for layer in (saved.get("*") or {}, saved.get(str(library)) or {} if library else {}):
                if o["id"] in layer:
                    v = layer[o["id"]]
            if o.get("choices"):
                out[o["id"]] = v if v in [c[0] for c in o["choices"]] else o["default"]
            else:
                out[o["id"]] = v is True
        return out

    def live(self, library: str, rows: list[dict]) -> list[dict]:
        """The rows the library's options leave live: about an issue whose switch is on."""
        on = self.enabled(library)
        return [r for r in rows if any(on.get(i) is True for i in r.get("issues") or [])]

    history: list[tuple[str, object]] = []  # the columns a run's files table shows for this tool: (id, name); common to all: poster, title, when, result

    def history_row(self, row: dict, outcome: dict) -> dict:
        """What the history keeps of one row done: {column id: value (a string, number or M)}."""
        return {}

    def flag_lines(self, rows: list[dict]) -> list:
        """What the chip on Library Health says of an item's rows (one line each)."""
        return [r.get("title") or r.get("id") for r in rows]

    # ------------------------------------------------------------ the scan over the data layer
    def analysis(self, data) -> dict:
        """The scan over the library's data as it is now — worked out once per state of the data (its stamp)."""
        stamp = data.stamp
        with self.lock:
            got = self._analysis.get(data.key)
        if got and got[0] == stamp:
            return got[1]
        out = self.scan(data)
        out["rows"] = out.get("rows") or []
        with self.lock:
            self._analysis[data.key] = (stamp, out)
        return out

    def flags(self, data) -> dict[str, list[dict]]:
        """The items the tool's live rows concern (the rating keys), each with its rows: Library Health's chips."""
        out: dict[str, list[dict]] = {}
        for r in self.live(data.key, self.analysis(data).get("rows") or []):
            for rk in (r.get("rks") or ([r["rk"]] if r.get("rk") else [])):
                out.setdefault(str(rk), []).append(r)
        return out

    def works_on(self, library: str) -> bool:
        lib = self.app.library_info(library) or {}
        return bool(lib.get("kind")) and lib.get("kind") in self.applies

    def ignored_rows(self, key: str, rows: list[dict]) -> list[dict]:
        """The rows whose items are ignored for this tool on Library Health (every item a row concerns): not counted."""
        from .. import libhealth
        quiet = libhealth.ignored(self.app).get(str(key)) or {}
        tid = "tool_" + self.name
        out = []
        for r in rows:
            rks = [str(x) for x in (r.get("rks") or ([r["rk"]] if r.get("rk") else []))]
            if rks and all(tid in (quiet.get(rk) or []) for rk in rks):
                out.append(r)
        return out

    def counts(self) -> dict:
        """What the tool flags — its live rows, per the library's options — from the analyses worked out (never worked out
        here: the state is asked every few seconds), ignored items left out: {"total", "ignored", "libraries": {key: n},
        "pending": [libraries not analysed yet]}."""
        libs, pending, ignored = {}, [], 0
        for key, d in list(self.app.libdata.libs.items()):
            if not d.ready or not self.works_on(key):
                continue
            with self.lock:
                got = self._analysis.get(key)
            if got and got[0] == d.stamp:
                rows = self.live(key, got[1].get("rows") or [])
                quiet = len(self.ignored_rows(key, rows))
                libs[key] = len(rows) - quiet
                ignored += quiet
            else:
                pending.append(key)
        return {"total": sum(libs.values()), "ignored": ignored, "libraries": libs, "pending": pending}

    def data_changed(self, key: str, what) -> None:
        """A gathering done (libdata's listener): the analysis worked out, so the count and chips are fresh."""
        d = self.app.libdata.data(key)
        if d.ready and self.works_on(key):
            try:
                self.analysis(d)
            except Exception:  # noqa: BLE001 — said when the page scans
                pass

    def apply_row(self, row: dict, log) -> dict:
        """One row done. {"result": "done" | "failed", "reason"?, ...}; ``log(text)`` keeps a step."""
        raise NotImplementedError

    keeps = ""  # what the tool keeps of the files it changes, if anything (M): a fallback the page can offer instead of a snapshot

    def needed_bytes(self, rows: list[dict]) -> int:
        """What doing these rows writes to the library's folders (0: nothing new — a rename, a record change)."""
        return 0

    def tick(self) -> None:
        """Follow-ups checked now and then (the scheduler): what's waited for on the mirror."""

    def waiting(self) -> bool:
        """Whether tick() has anything to look at."""
        return False

    # ------------------------------------------------------------ running
    def scan_async(self, library: str, options: dict | None = None, refresh: bool = True) -> bool:
        """The scan, in the background: the library's data gathered again first (a scan you ask for reads afresh), or
        taken as it is when fresh (``refresh=False``: the page's own scan on opening, instant), then the rows worked
        out from it. (``options`` is taken for the API's sake and ignored: the options are the library's settings, and
        they never change what a scan finds.)"""
        with self.lock:
            if self.running:
                if self.progress and self.progress.get("what") == "scan":
                    # a scan under way already (the one that follows a run or a revert, say): this one runs after it, since
                    # the library may have changed since that one read it
                    self._again = (str(library), None, refresh)
                    return True
                return False
            self.running, self.error = True, None
            self.progress = {"what": "scan", "library": library}

        def progress(p):
            with self.lock:
                if self.progress:
                    self.progress.update(step=p.get("what"), done=p.get("done", 0), total=p.get("total", 0))

        def work():
            try:
                data = self.app.libdata.gather(library, wait=True, progress=progress, refresh=refresh)
                got = dict(self.analysis(data))
                got["at"] = time.time()
                got["v"] = self.scan_version
                got["rows"] = list(got.get("rows") or [])
                with self.lock:
                    self.scan_result = got
            except Exception as e:  # noqa: BLE001 — said on the page
                self.error, self.error_at = str(e), time.time()
            finally:
                self.save()
                with self.lock:
                    self.running, self.progress = False, None
                    again, self._again = getattr(self, "_again", None), None
                if again:
                    self.scan_async(*again)
        threading.Thread(target=work, name=f"tool-{self.name}-scan", daemon=True).start()
        return True

    def rows_for(self, ids: list[str], choices: dict | None = None) -> list[dict]:
        """The rows of the last scan named, each with your choices for it merged in."""
        with self.lock:
            want = set(ids)
            return [dict(r, **{k: v for k, v in ((choices or {}).get(r["id"]) or {}).items() if k != "blocked"}) for r in (self.scan_result or {}).get("rows") or [] if r["id"] in want]

    def retained_bytes(self, rows: list[dict]) -> int:
        """What a snapshot keeps of the files these rows rewrite or delete, until it expires (0: nothing goes)."""
        return 0

    def apply_async(self, ids: list[str], choices: dict | None = None) -> bool:
        """The rows named done, as a process (ToolJob, on the Processes page): the backup first, per the Backup
        preference, then each row, with your choices for it (``choices``: {row id: {...}}, merged into the row)."""
        from .. import peers, safety
        key = str((self.scan_result or {}).get("library") or "")
        why = peers.tool_conflict(self.app, key, self.name)
        if why:
            raise ValueError(why)
        with self.lock:
            if self.running or not self.scan_result:
                return False
        rows = [r for r in self.live(key, self.rows_for(ids, choices)) if not r.get("blocked")]  # (a row about a switched-off issue isn't done, nor one the page won't tick)
        if not rows:
            return False
        if self.name in peers.FILE_TOOLS:  # (files change: room for it, and a backup that can be made, first)
            ready = safety.readiness(self.app, key, self.needed_bytes(rows), keeps=bool(self.keeps), retained=self.retained_bytes(rows), tool=self.name)
            if not ready["ready"]:
                raise ValueError(M("Not yet: {why}", why=safety.why_not(ready)))
        item = self.app.jobs.enqueue("tool", {"tool": self.name, "library": key, "ids": [r["id"] for r in rows], "choices": choices or {}},
                                     library=key, title=(self.app.library_info(key) or {}).get("title") or key, tool=self.name)
        if item is None:
            return False
        with self.lock:
            self.running, self.error = True, None
            self.progress = {"what": "apply", "library": key, "done": 0, "total": len(rows), "row": None, "step": None, "queued": True}
        return True

    def run_rows(self, rows: list[dict], job) -> None:
        """Each row done, by the process: its result kept, its row taken off the scan, the job's log fed."""
        for i, row in enumerate(rows):
            if not job.wait_while_paused():
                break
            steps: list = []

            def log(text, _steps=steps):
                _steps.append(text)
                with self.lock:
                    if self.progress:
                        self.progress["step"] = text
                job.update(step=str(text))
            with self.lock:
                if self.progress:
                    self.progress.update(done=i, row=row.get("title"), step=None, queued=False)
            job.update(current=row.get("title"), done=i)
            try:
                res = self.apply_row(row, log)
            except Exception as e:  # noqa: BLE001 — one row's failure, listed; the rest go on
                res = {"result": "failed", "reason": e.args[0] if e.args and isinstance(e.args[0], M) else str(e)}
            with self.lock:
                self.results.append(dict(res, id=row["id"], title=row.get("title"), at=time.time(), steps=steps))
                if res.get("result") == "done" and self.scan_result:
                    self.scan_result["rows"] = [r for r in self.scan_result["rows"] if r["id"] != row["id"]]
            outcome = {k: v for k, v in res.items() if k not in ("result", "reason")}
            job.log(type="file", file=self.row_file(row), title=row.get("title"), poster=row.get("poster") or "", result="done" if res.get("result") == "done" else "failed",
                    reason=res.get("reason"), row=row, outcome=outcome, history=self.history_row(row, outcome))
            job.file_done(int(row.get("size") or 0), "optimized" if res.get("result") == "done" else "failed",
                          error=(row.get("title") or row["id"], str(res.get("reason") or "")) if res.get("result") != "done" else None)
            self.save()

    def row_file(self, row: dict) -> str:
        """The path a row is about, for the job log (a record tool: its title)."""
        return row.get("path") or row.get("local") or (row.get("keep") or {}).get("path") or (row.get("moves") or [{}])[0].get("from") or str(row.get("title") or row["id"])

    def plex_keys(self, rows: list[dict]) -> list[str]:
        """The Plex records doing these rows will change (a merge, an edit): backed up before the first row (plexbackup.py).
        A change to a record not named here is backed up all the same, as it's made."""
        return []

    def changed_paths(self, row: dict, outcome: dict) -> list[str]:
        """The local paths a done row rewrote or removed: what a Revert restores from the snapshot."""
        return []

    def revert_row(self, row: dict, outcome: dict, backup: dict, log) -> None:
        """A done row undone. The base: each changed path put back from the snapshot the process took (safety.snapshot_path);
        a tool with a better way (a path re-linked, a record split) has its own."""
        from .. import safety
        paths = self.changed_paths(row, outcome)
        if not paths:
            raise ValueError(M("Nothing to put back for {title}.", title=row.get("title") or row["id"]))
        if backup.get("kind") != "snapshot" or not backup.get("names"):
            raise ValueError(M("No snapshot was taken for this run: {title} can’t be put back.", title=row.get("title") or row["id"]))
        for local in paths:
            src = next((p for p in (safety.snapshot_path(self.app, str(row.get("library") or ""), local, n) for n in backup["names"]) if p), None)
            if not src:
                raise ValueError(M("{file} isn’t in the snapshot’s dataset.", file=os.path.basename(local)))
            self.file_action("copy_from", local, to=src)
            log(M("{file} put back from the snapshot", file=os.path.basename(local)))

    def finished_job(self, key: str) -> None:
        """The process ended (however): the tool is free again, the data layer reads the library again, Health runs."""
        from .. import peers
        with self.lock:
            self.running, self.progress = False, None
        self.save()
        if self.name in peers.FILE_TOOLS and hasattr(self.app, "note_tool_run"):
            self.app.note_tool_run(key, self.name)
        self.rescan(key)
        self.app.run_checks_async()

    def rescan(self, key: str) -> None:
        """After a run, or a revert: the data layer reads the library again and the scan runs over it, so the rows and
        counts say what's there now — no Scan Again needed."""
        self.app.libdata.refresh(key)
        self.scan_async(key, refresh=True)

    def snapshot(self) -> dict:
        libs = [str(lib["key"]) for lib in getattr(self.app, "libraries", None) or [] if self.works_on(str(lib["key"]))]
        with self.lock:
            return {"name": self.name, "title": self.title, "page": self.page, "running": self.running, "progress": dict(self.progress) if self.progress else None,
                    "error": self.error, "error_at": self.error_at,
                    "scan": self.scan_result, "results": list(reversed(self.results[-50:])), "extra": json.loads(json.dumps(self.extra)), "keeps": self.keeps or "",
                    # the options, for the page: each with the kinds it's for; and their values per library
                    "options": [{"id": o["id"], "name": o["name"], "help": o.get("help") or "", "kinds": list(o["kinds"]), "default": o["default"],
                                 "choices": [[v, n] for v, n in o["choices"]] if o.get("choices") else None} for o in self.options],
                    "enabled": {key: self.enabled(key) for key in libs}}

    # ------------------------------------------------------------ what the tools share
    def mirror(self, library: str):
        """The mirrored server's library paired with ``library``, or None (no mirror, or not paired)."""
        other = getattr(self.app, "other", None)
        if other is None or not other.configured() or self.app.sync.decided() != "plex2":
            return None
        return other.pairs().get(str(library))

    def mirrored(self, library: str) -> dict | None:
        """Whether the library's files are mirrored somewhere, as far as the app can tell: {"name", "how": "watched" |
        "suspected" | "peer"} — the mirrored server watched on the Sync page; a server of the account whose files are
        here too (a mirror until you say otherwise); another Plex Cache sharing the folders. None when you've said
        there's no mirror, or none is seen."""
        if self.mirror(library) is not None:
            return {"name": self.mirror_name(), "how": "watched"}
        sync = getattr(self.app, "sync", None)
        if sync is None or sync.settings().get("mirrors") == "none":
            return None
        mine = self.app.settings.section("plex2").get("machine_id")
        for c in getattr(sync, "candidates", None) or []:
            if (c.get("rate") or 0) >= 0.9 and c.get("id") != mine:
                return {"name": c.get("name") or c.get("id") or "", "how": "suspected"}
        for sy in ((getattr(self.app, "peers_summary", None) or {}).get("systems") or []):
            if str(library) in [str(k) for k in sy.get("libraries") or []]:
                return {"name": sy.get("name") or sy.get("instance") or "", "how": "peer"}
        return None

    def mirror_name(self) -> str:
        return self.app.other.settings().get("name") or M("the mirrored server")

    def file_action(self, action: str, path: str, to: str = "", root: str = "") -> dict:
        """A file action through the optimizer service, its refusal said in words."""
        client = self.app.optimizing.client
        if not client.configured:
            raise ValueError(M("The optimizer service isn’t set up: it’s the part of Plex Cache that may write to your media. See the Optimizer page."))
        try:
            return client.file_action(action, path, to=to, root=root)
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read() or b"{}").get("error") or str(e)
            except ValueError:
                why = str(e)
            raise ValueError(M("The optimizer service refused: {why}", why=why)) from None
        except (OSError, ValueError) as e:
            raise ValueError(M("The optimizer service couldn’t be reached: {why}", why=str(getattr(e, "reason", None) or e))) from None

    def root_of(self, library: str, plex_path: str) -> str | None:
        """The library folder (local path) a file is in."""
        lib = self.app.library_info(library) or {}
        for loc in sorted((loc.rstrip("/") for loc in lib.get("locations") or []), key=len, reverse=True):
            if plex_path.startswith(loc + "/"):
                return self.app.map_path(loc)
        return None

    def step(self, done: int, total: int) -> None:
        """How far a scan has got (the page's progress bar): ``done`` of ``total`` looked at."""
        with self.lock:
            if self.progress and self.progress.get("what") == "scan":
                self.progress.update(step="rows", done=int(done), total=int(total))


class ToolJob(Job):
    """A tool's Apply as a process (the Processes page): the backup first — a snapshot through TrueNAS, named for the
    tool and library, or none where the action keeps copies of its own or you back up yourself — then the rows ticked,
    one by one, each logged; a backup that can't be made aborts it before anything changes. Revert (revert_job) undoes
    the rows from the backup while it exists."""

    kind = "tool"

    def __init__(self, app, args: dict, saved: dict | None = None):
        super().__init__(app.jobs, resumed=saved, library=str(args.get("library") or ""), tool=str(args.get("tool") or ""),
                         title=(app.library_info(str(args.get("library") or "")) or {}).get("title") or str(args.get("library") or ""))
        self._args = dict(args)
        self.tool = app.tools.get(str(args.get("tool") or ""))

    def args(self) -> dict:
        return self._args

    def run(self) -> None:
        from .. import peers, plexbackup, safety
        app, tool, key = self.app, self.tool, str(self._args.get("library") or "")
        if tool is None:
            self.log(type="job", event="failed", why="no such tool")
            return
        rows = tool.rows_for(self._args.get("ids") or [], self._args.get("choices") or {})
        self.update(total=len(rows), todo_bytes=sum(int(r.get("size") or 0) for r in rows), phase="backup")
        self.log(type="plan", files=[tool.row_file(r) for r in rows])
        # a tool that changes files: the backup the preference says (a snapshot, its copies, yours); one that changes only
        # Plex's records: the backup of those records — and either way, any record a row changes is backed up first
        bp = safety.plan(app, key, keeps=bool(tool.keeps), tool=tool.name) if tool.name in peers.FILE_TOOLS else plexbackup.plan(app)
        backup = {"kind": bp["kind"], "until": time.time() + safety.life(app)}
        if bp["kind"] == "snapshot":
            self.update(step=str(M("Taking a snapshot")))
            try:
                backup["names"] = safety.take_snapshot(app, key, tool.name)
            except ValueError as e:  # the backup can't be made: nothing changes
                why = e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))
                self.update(errors=[{"file": "", "reason": str(why), "at": time.time()}], step=None)
                self.log(type="job", event="backup_failed", why=why)
                app.activity.event("tools", M("{tool} on {library} not run: the backup couldn’t be made — {why}", tool=tool.title, library=self.state.get("title"), why=why), level="warn")
                return
        elif bp["kind"] == "none":
            self.log(type="job", event="backup_failed", why=bp["text"])
            self.update(errors=[{"file": "", "reason": str(bp["text"]), "at": time.time()}])
            return
        records = contextlib.ExitStack()
        try:
            self.update(step=str(M("Backing up the Plex records")) if tool.plex_keys(rows) else None)
            pb = records.enter_context(plexbackup.session(app, M("{tool} on {library}", tool=tool.title, library=self.state.get("title") or key), "here", key, self.state.get("id") or "",
                                                          keys=tool.plex_keys(rows)))
        except ValueError as e:  # the records can't be read: nothing changes
            why = e.args[0] if e.args and isinstance(e.args[0], M) else M("{why}", why=str(e))
            self.update(errors=[{"file": "", "reason": str(why), "at": time.time()}], step=None)
            self.log(type="job", event="backup_failed", why=why)
            app.activity.event("tools", M("{tool} on {library} not run: the backup couldn’t be made — {why}", tool=tool.title, library=self.state.get("title"), why=why), level="warn")
            return
        with records:
            if pb is not None and pb["items"]:
                backup["plex"] = {"id": pb["id"], "records": len(pb["items"])}
            self.log(type="job", event="backup", backup=backup)
            self.update(phase="applying", backup=backup, step=None)
            tool.run_rows(rows, self)

    def finished(self) -> None:
        if self.tool is not None:
            self.tool.finished_job(str(self._args.get("library") or ""))


def revert_job(app, job_id: str) -> dict:
    """A tool's process undone from its backup, row by row (each tool's revert_row), while the backup exists. Logged
    on the same job; {"reverted": n, "failed": n}."""
    entries = [e for e in app.jobs.log.entries() if e.get("job") == job_id]
    if not entries or entries[0].get("kind") != "tool":
        raise ValueError(M("Not a tool’s run."))
    backup = next((e.get("backup") for e in entries if e.get("event") == "backup"), None)
    if any(e.get("event") in ("reverted", "rolled_back") for e in entries):
        raise ValueError(M("Undone already."))
    if not backup or (backup.get("until") and backup["until"] < time.time()):
        raise ValueError(M("The backup has expired: nothing to put back from."))
    tool = app.tools.get(str(entries[0].get("tool") or next((e.get("row", {}).get("tool") for e in entries if e.get("row")), "")) or "")
    if tool is None:
        # the tool's name travels in the job's args; the log keeps the kind: look it up by the rows' ids
        tool = next((t for t in app.tools.values() if any(r["id"] == e.get("row", {}).get("id") for e in entries if e.get("row") for r in t.results)), None)
    if tool is None:
        raise ValueError(M("The tool this run belongs to isn’t known."))
    done = [e for e in entries if e.get("type") == "file" and e.get("result") == "done" and e.get("row")]
    ok = failed = 0
    for e in done:
        steps: list = []
        try:
            tool.revert_row(e["row"], e.get("outcome") or {}, backup, steps.append)
            ok += 1
            app.jobs.log.add({"job": job_id, "kind": "tool", "type": "file", "file": e.get("file"), "title": e.get("title"), "result": "reverted", "steps": [str(x) for x in steps]})
        except Exception as ex:  # noqa: BLE001 — said per row; the rest go on
            failed += 1
            app.jobs.log.add({"job": job_id, "kind": "tool", "type": "file", "file": e.get("file"), "title": e.get("title"), "result": "revert_failed",
                              "reason": str(ex.args[0] if ex.args else ex)})
    app.jobs.log.add({"job": job_id, "kind": "tool", "type": "job", "event": "reverted" if ok else "revert_failed", "counts": {"reverted": ok, "failed": failed}})
    app.activity.event("tools", M("{tool} on {library} reverted: {ok} put back, {failed} not", tool=tool.title, library=entries[0].get("title") or "", ok=ok, failed=failed))
    key = str(entries[0].get("library") or "")
    tool.rescan(key)  # (the rows say what's there now: the links are back)
    app.run_checks_async()
    return {"reverted": ok, "failed": failed}


def rollback_job(app, job_id: str) -> dict:
    """A tool's run undone the other way: the datasets rolled back to the snapshot it took — everything written to
    them since is gone, not only what the run changed. {"rolled_back": [names]}."""
    from .. import safety
    entries = [e for e in app.jobs.log.entries() if e.get("job") == job_id]
    if not entries or entries[0].get("kind") != "tool":
        raise ValueError(M("Not a tool’s run."))
    backup = next((e.get("backup") for e in entries if e.get("event") == "backup"), None)
    if not backup or backup.get("kind") != "snapshot" or not backup.get("names"):
        raise ValueError(M("This run took no snapshot: nothing to roll back to."))
    if any(e.get("event") in ("reverted", "rolled_back") for e in entries):
        raise ValueError(M("Undone already."))
    names = safety.rollback(app, backup["names"])
    app.jobs.log.add({"job": job_id, "kind": "tool", "type": "job", "event": "rolled_back", "counts": {"snapshots": len(names)}})
    key = str(entries[0].get("library") or "")
    tool = app.tools.get(str(entries[0].get("tool") or ""))
    if tool is not None:
        tool.rescan(key)
    else:
        app.libdata.refresh(key)
    app.run_checks_async()
    return {"rolled_back": names}

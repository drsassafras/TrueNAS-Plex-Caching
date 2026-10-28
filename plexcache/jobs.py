"""File jobs: the long things that read and write media — optimizing a library, syncing its cover
art, copying a folder into its own dataset — run one at a time, and all the same way:

* progress — files and bytes done of how many, the file in hand, and an estimate of when it'll be
  done, from how fast files are really written on this NAS;
* **Pause** (after the file in hand, or, for a folder copy, within it) and **Cancel**; and by
  itself while the pool is resilvering (that pause can't be lifted by hand), or for whatever else
  a kind of job waits for (a folder copy: something playing);
* saved after every file (job.json), so a restart carries on where it was ("resumed");
* one at a time: work asked for while a job runs waits in the queue (job-queue.json, kept across
  restarts), and starts when the jobs before it are done; queued work can be removed.

A kind of job is a Job subclass: it says what it's called, how to run, how to start again from
what was saved, and what pausing means for it. The page shows every kind with one card, and Health
with one banner.
"""

from __future__ import annotations

import threading
import time
import uuid

from .i18n import M
from .joblog import JobLog

JOB_FILE = "job.json"
LAST_FILE = "job-last.json"
QUEUE_FILE = "job-queue.json"  # the last job that ended: its counts and every file that failed, until dismissed
MAX_ERRORS = 500
# what's saved after every file and carried over when a job is started again after a restart
CARRY = ("done", "total", "optimized", "rewritten", "skipped", "failed", "conflicts", "work_seconds", "work_bytes",
         "done_bytes", "todo_bytes", "resumes", "id")


class Job:
    """One file job. Subclasses set ``kind`` and implement run(); they may add saved arguments
    (args()), a pause reason of their own (extra_pause()) and what pausing does (apply_pause())."""

    kind = "job"
    POLL = 5  # seconds between looks while paused (a resilver is checked six times less often)

    def __init__(self, jobs: "Jobs", resumed: dict | None = None, **info):
        self.jobs, self.app = jobs, jobs.app
        self.stop_event = threading.Event()
        self.state = {"kind": self.kind, "running": True, "started": time.time(), "phase": "checking", "checked": 0,
                      "files": 0, "done": 0, "total": 0, "done_bytes": 0, "todo_bytes": 0, "work_seconds": 0.0,
                      "work_bytes": 0, "eta": None, "current": None, "step": None, "paused": False, "paused_by": None,
                      "paused_why": None, "resumed": bool(resumed), "errors": [], "optimized": 0, "rewritten": 0,
                      "skipped": 0, "failed": 0, "conflicts": 0, "resumes": 0, "id": uuid.uuid4().hex[:12], **info}
        if resumed:
            self.state.update({k: resumed[k] for k in CARRY if k in resumed}, paused=bool(resumed.get("paused")),
                              started=resumed.get("started") or self.state["started"], errors=list(resumed.get("errors") or []))
            self.state["resumes"] = int(resumed.get("resumes") or 0) + 1  # how many restarts it carried on after

    # ---------------------------------------------------------- for subclasses
    def run(self) -> None:
        raise NotImplementedError

    def args(self) -> dict:
        """What's needed to start this job again after a restart (with the progress, saved)."""
        return {}

    def dataset(self) -> str | None:
        """The dataset the job writes to: its pool's resilvers pause it."""
        return None

    def extra_pause(self) -> str | None:
        """Another reason to wait right now (None: none)."""
        return None

    def apply_pause(self, paused: bool) -> None:
        """What pausing does beyond not starting the next file (a folder copy pauses within a file)."""

    def finished(self) -> None:
        """Called once the job ends, however it ends."""

    def ended(self) -> None:
        """Called after finished(), once the end is recorded (the job's state and the job log say so)."""

    # ---------------------------------------------------------- helpers
    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def update(self, **kw) -> None:
        with self.jobs.lock:
            self.state.update(kw)

    logged = True  # kept in the job log (Processes page); a job that changes nothing on disk isn't

    def log(self, **entry) -> None:
        """An entry in the job log (Processes page), with this job's ID, kind and library."""
        if not self.logged:
            return
        s = self.state
        self.jobs.log.add(dict({"job": s["id"], "kind": self.kind, "mode": s.get("mode"), "library": s.get("library"),
                                "title": s.get("title"), **({"tool": s["tool"]} if s.get("tool") else {})}, **entry))

    def log_pause(self, by: str | None, why) -> None:
        """Log a pause starting or ending (once each)."""
        was = self.state.get("_logged_pause")
        if by != was:
            if by:
                self.log(type="job", event="paused", why=str(why) if why else by, done=self.state["done"], total=self.state["total"])
            elif was:
                self.log(type="job", event="unpaused", done=self.state["done"], total=self.state["total"])
            self.state["_logged_pause"] = by

    def pause_reason(self) -> tuple[str | None, str | None]:
        """(who's pausing: "resilver", "user", "playing"…, why) right now."""
        why = self.jobs.resilvering(self.dataset())
        if why:
            return "resilver", why
        if self.state.get("paused"):
            return "user", None
        extra = self.extra_pause()
        return ("waiting", extra) if extra else (None, None)

    def wait_while_paused(self) -> bool:
        """Before each file: wait while paused (by hand, a resilver, or the job's own reason).
        False when cancelled."""
        applied = False
        while not self.stopped:
            by, why = self.pause_reason()
            self.update(paused_by=by, paused_why=why)
            self.log_pause(by, why)
            if not by:
                if applied:
                    self.apply_pause(False)
                return True
            if not applied:
                self.apply_pause(True)
                applied = True
            self.stop_event.wait(self.POLL if by != "resilver" else self.POLL * 6)
        return False

    def file_done(self, size: int, result: str, seconds: float | None = None, error: str | None = None) -> None:
        """A file finished: the progress, the estimate (from files really written), and saved."""
        with self.jobs.lock:
            s = self.state
            s["done"] += 1
            s["done_bytes"] += size
            if result in s and isinstance(s[result], int):
                s[result] += 1
            if error and len(s["errors"]) < MAX_ERRORS:
                s["errors"].append({"file": error[0], "reason": error[1], "at": time.time()} if isinstance(error, tuple) else
                                   {"file": "", "reason": str(error), "at": time.time()})
            if seconds is not None and result in ("optimized", "rewritten", "copied"):
                s["work_seconds"] += seconds
                s["work_bytes"] += size
            self._estimate()
        self.save()

    def _estimate(self) -> None:
        s = self.state
        rate = s["work_bytes"] / s["work_seconds"] if s["work_seconds"] > 0 and s["work_bytes"] else None
        s["eta"] = round(max(0, s["todo_bytes"] - s["done_bytes"]) / rate) if rate else None

    def first_estimate(self, size: int, seconds: float, pct: float | None) -> None:
        """Before any file is done: an estimate from how far through the first one it is (``pct`` of
        ``size`` bytes in ``seconds``), so it isn't "working out how long" for a whole long file."""
        with self.jobs.lock:
            s = self.state
            if s["work_seconds"] > 0 or not pct or pct < 2 or seconds <= 0:
                return
            done = size * pct / 100
            s["eta"] = round(max(0, s["todo_bytes"] - s["done_bytes"] - done) / (done / seconds))

    def save(self) -> None:
        with self.jobs.lock:
            s = dict(self.state)
        if s.get("running"):
            self.app._save_json(JOB_FILE, {"kind": self.kind, "started": s["started"], "paused": s.get("paused"),
                                           "args": self.args(), "errors": s.get("errors") or [],
                                           # shown while it waits to carry on after a restart
                                           **{k: s.get(k) for k in ("library", "mode", "title", "folder", "child") if s.get(k)},
                                           **{k: s.get(k) for k in CARRY}})

    def _main(self) -> None:
        s = self.state
        # a job carried on after a restart notes where it picked up (its resume point)
        self.log(type="job", event="resumed" if s.get("resumed") else "started", done=s["done"], total=s["total"])
        try:
            self.run()
        except Exception as e:  # never leave the job "running"
            self.app.activity.event("optimizer", M("unexpected error: {why}", why=str(e)), "warn")
        finally:
            try:
                self.finished()
            finally:
                with self.jobs.lock:  # ended, nothing to pick up, and the last job in one step: never a moment between
                    self.app._save_json(JOB_FILE, {})  # done (or cancelled): nothing to pick up after a restart
                    self.update(running=False, current=None, current_file=None, step=None, file_pct=None, finished=time.time(),
                                cancelled=self.stopped, paused_by=None, paused_why=None)
                    if self.logged:  # (an inspection isn't a job to report: the last real one stays)
                        self.jobs.last = {k: v for k, v in self.state.items() if not k.startswith("_")}
                        self.app._save_json(LAST_FILE, self.jobs.last)  # its counts and failures stay until dismissed
                s = self.state
                self.log(type="job", event="cancelled" if self.stopped else "finished", done=s["done"], total=s["total"],
                         counts={k: s.get(k) for k in ("optimized", "rewritten", "skipped", "failed", "conflicts")})
                try:
                    self.ended()
                finally:
                    threading.Thread(target=self.jobs.start_next, daemon=True).start()  # the next in the queue


class Jobs:
    """The one file job running now (or the last one, finished), and starting them again after a restart."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.job: Job | None = None
        self.kinds: dict[str, callable] = {}  # kind -> make(args, saved) -> Job: queued work, or a job carried on after a restart
        self.waiting: dict | None = None  # after a restart: the job that carries on once the optimizer is ready
        self.removed: dict[str, callable] = {}  # kind -> removed(item)
        self.queue: list[dict] = (app._load_json(QUEUE_FILE) or {}).get("items") or []  # work waiting its turn
        # hold(kind) -> why queued work of that kind can't start now, or None (a folder mid-switch, say)
        self.holds: list = []
        self.last: dict | None = app._load_json(LAST_FILE) or None  # the last job that ended (until dismissed)
        if self.last and self.last.get("kind") == "survey":
            self.last = None  # kept by an earlier version: an inspection isn't one to report
        self.log = JobLog(app.config_dir)  # what every job did, file by file (Processes page), kept for good
        self._resilver: dict[str, tuple[float, str | None]] = {}

    def register(self, kind: str, make, removed=None) -> None:
        """``make(args, saved)``: a Job of this kind from its arguments (and, after a restart, what was
        saved). ``removed(item)``: called when queued work of this kind is taken off the queue."""
        self.kinds[kind] = make
        if removed:
            self.removed[kind] = removed

    # ---------------------------------------------------------- the queue
    def _save_queue(self) -> None:
        with self.lock:
            items = list(self.queue)
        self.app._save_json(QUEUE_FILE, {"items": items})

    def enqueue(self, kind: str, args: dict, **info) -> dict | None:
        """Queue work to start when the jobs before it are done (now, if none runs). None when the
        same work is queued or running already."""
        with self.lock:
            same = lambda k, a: k == kind and a == args
            if any(same(q["kind"], q["args"]) for q in self.queue):
                return None
            if self.job and self.job.state.get("running") and same(self.job.kind, self.job.args()):
                return None
            if self.waiting and same(self.waiting.get("kind"), self.waiting.get("args")):
                return None
            item = dict(info, id=uuid.uuid4().hex[:12], kind=kind, args=args, queued=time.time())
            self.queue.append(item)
        self._save_queue()
        self.start_next()
        return item

    def remove(self, item_id: str = "", kind: str = "", args: dict | None = None) -> bool:
        """Take queued work off the queue: by its id, or its kind and arguments."""
        with self.lock:
            gone = [q for q in self.queue if q["id"] == item_id or (kind and q["kind"] == kind and q["args"] == args)]
            self.queue = [q for q in self.queue if q not in gone]
        if gone:
            self._save_queue()
        for q in gone:
            if self.removed.get(q["kind"]):
                self.removed[q["kind"]](q)
        return bool(gone)

    def hold_reason(self, kind: str = "") -> str | None:
        """Why queued work (of ``kind``) waits beyond its turn, or None."""
        return next((r for r in (h(kind) for h in list(self.holds)) if r), None)

    def start_next(self) -> bool:
        """Start the first queued work, if no job runs (and drop what can't start any more)."""
        while True:
            with self.lock:
                if (self.job and self.job.state.get("running")) or self.waiting or not self.queue:
                    return False
                if self.hold_reason(self.queue[0]["kind"]):
                    return False  # (started once the hold is lifted)
                item = self.queue.pop(0)
            self._save_queue()
            make = self.kinds.get(item["kind"])
            job = make(item.get("args") or {}, None) if make else None
            if job and self.start(job):
                return True

    def busy(self) -> bool:
        """A job running now — or, after a restart, one waiting to carry on (nothing else starts first)."""
        with self.lock:
            return bool((self.job and self.job.state.get("running")) or self.waiting)

    def start(self, job: Job) -> bool:
        with self.lock:
            if self.job and self.job.state.get("running"):
                return False
            self.job = job
            self.waiting = None
        job.save()
        threading.Thread(target=job._main, name=f"job-{job.kind}", daemon=True).start()
        return True

    def pause(self, paused: bool) -> bool:
        """Pause or resume the job (a resilver's pause lasts as long as the resilver, whatever this says)."""
        with self.lock:
            job = self.job if self.job and self.job.state.get("running") else None
            if job:
                job.state["paused"] = bool(paused)
        if job:
            job.save()
        return job is not None

    def cancel(self) -> bool:
        with self.lock:
            job = self.job if self.job and self.job.state.get("running") else None
        if job:
            job.update(stopping=True)
            job.stop_event.set()
        return job is not None

    def retried(self, job_id: str, path: str, result: dict) -> None:
        """A file that failed in a job was tried again: its result replaces the failure in that job's
        counts and failures (the running job's, or the last one's)."""
        res = result.get("result")
        if res == "cancelled":
            return  # not tried after all: the failure stands
        kind = {"already": "optimized"}.get(res, res if res in ("optimized", "rewritten", "skipped") else "failed")
        with self.lock:
            running = self.job.state if self.job and self.job.state.get("running") and self.job.state.get("id") == job_id else None
            for st in (running, self.last if self.last and self.last.get("id") == job_id else None):
                if not st:
                    continue
                errs = st.get("errors") or []
                if not any(e.get("file") == path for e in errs):
                    continue
                st["errors"] = [e for e in errs if e.get("file") != path]
                st["failed"] = max(0, (st.get("failed") or 0) - 1)
                st[kind] = (st.get(kind) or 0) + 1
                if kind == "failed":
                    st["errors"].append({"file": path, "reason": str(result.get("reason") or "failed"), "at": time.time()})
        if running:
            self.job.save()
        elif self.last and self.last.get("id") == job_id:
            self.app._save_json(LAST_FILE, self.last)

    def queued(self) -> list[dict]:
        with self.lock:
            return [dict(q) for q in self.queue]

    def snapshot(self) -> dict | None:
        """The job running now, or the last one that ended (until dismissed)."""
        with self.lock:
            if self.job and self.job.state.get("running"):
                return {k: v for k, v in self.job.state.items() if not k.startswith("_")}
            if self.waiting:  # after a restart, before it carries on
                return dict(self.waiting, running=False, waiting=True)
            return dict(self.last) if self.last else None

    def dismiss(self) -> None:
        """Forget the last job that ended (its failures were looked at)."""
        with self.lock:
            self.last = None
            if self.job and not self.job.state.get("running"):
                self.job = None
        self.app._save_json(LAST_FILE, {})

    def resume_saved(self, ready=lambda: True, wait: float = 600) -> None:
        """After a restart: start the job that was running again, where it was, once ``ready()``."""
        saved = self.app._load_json(JOB_FILE) or {}
        make = self.kinds.get(saved.get("kind"))
        if not make and not self.queue:
            return

        if make:
            with self.lock:
                self.waiting = {k: v for k, v in saved.items() if not k.startswith("_")}

        def work():
            until = time.time() + wait
            while time.time() < until and not ready():
                time.sleep(10)
            job = make(saved.get("args") or {}, saved) if make else None
            with self.lock:
                self.waiting = None
            if not (job and self.start(job)):
                self.app._save_json(JOB_FILE, {})  # couldn't: don't try again on the next restart
                self.start_next()  # then what's queued
        threading.Thread(target=work, daemon=True).start()

    def resilvering(self, dataset: str | None) -> str | None:
        """Is the dataset's pool resilvering? (Asked of TrueNAS at most once a minute.) Writing files
        then would slow the resilver — the pool is rebuilding a disk — so file jobs wait."""
        pool = (dataset or "").split("/")[0]
        if not pool:
            return None
        hit = self._resilver.get(pool)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        why = None
        try:
            with self.app.truenas() as tn:
                scan = (tn.pool(pool) or {}).get("scan") or {}
                if scan.get("state") == "SCANNING" and scan.get("function") == "RESILVER":
                    why = M("{pool} is resilvering", pool=pool)
        except Exception:  # noqa: BLE001 — can't tell: don't hold the job for it
            why = None
        self._resilver[pool] = (time.time(), why)
        return why

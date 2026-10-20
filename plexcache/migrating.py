"""Turning a library folder into a child dataset, step by step (Optimizer page → "Move a folder
to its own dataset"). Health's "Split Dataset" advice leads here.

A dataset is its own filesystem, so a folder can't become one in place: its files are copied into
a new dataset, which then takes the folder's place — the same path, so Plex and every app see the
same files where they were, with their metadata. The copy is also what gives the files the new
dataset's record size and compression. Plex keeps running; it's restarted once, at the switch.

1. **Create** — you make the dataset (``<name>-new``, next to the folder) in TrueNAS, with the
   record size and compression shown. Before anything is copied, it's checked: there, with that
   record size and a ZSTD 3–19 compression (through the TrueNAS API), mounted and writable in the
   optimizer, with room for the copy.
2. **Copy** — the optimizer copies the folder into it, each file checked; pause, resume and stop
   from the page (it also pauses while something plays). Stopped or interrupted, it picks up
   where it was.
3. **Switch** — a last pass picks up what changed meanwhile and compares both, file by file; the
   folder is renamed out of the way (``.<name>.plexcache-old``); you rename the dataset to the
   folder's name in TrueNAS; you restart Plex.
4. **Verify** — the folder is now the dataset, with its settings and every file; Plex shows as many
   items as before. Then the old copy is deleted, on your say.

The state is kept in migration.json, so the page picks up at the same step after a restart. Only
the optimizer writes media; the TrueNAS API is only read.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.error

from . import zfs, zfslog
from .i18n import M
from .jobs import Job

FRESH_COMPARE = 120  # seconds a comparison counts as just made, for setting the old folder aside
RESTART_WAIT = 300  # seconds a restart asked for here is waited for before it's asked for again

STEPS = ("create", "copy", "switch", "verify", "done")
STATE = "migration.json"
MARGIN = 1.05  # free space needed: the folder's size plus 5%


class Migration:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.state: dict | None = app._load_json(STATE) or None
        self.checks: list[dict] = []
        self.checked_at: float | None = None  # when the current step's conditions were last looked at
        self._cand: tuple[float, list] | None = None
        app.jobs.holds.append(self.hold)
        # a folder copy cut short by a restart is started again by the shared jobs, where it was
        app.jobs.register("copy", lambda args, saved: CopyJob(self, resumed=saved)
                          if self.state and self.state.get("folder") == args.get("folder") and (self.state.get("copying") or self.state.get("queued")) else None,
                          removed=self._unqueued)

    def hold(self, kind: str) -> str | None:
        """While the old folder is set aside and the dataset not yet renamed to its name, the folder's
        path is empty: other file jobs wait (the copies themselves don't)."""
        st = self.state or {}
        if kind != "copy" and st.get("step") == "switch" and st.get("old_path"):
            return M("Waiting while {title} is switched to its new dataset: until the dataset is renamed.", title=st.get("title") or st.get("folder"))
        return None

    def _released(self) -> None:
        threading.Thread(target=self.app.jobs.start_next, daemon=True).start()

    def _unqueued(self, item: dict) -> None:
        """The copy was taken off the queue before its turn."""
        st = self.state
        if st and st.get("folder") == item["args"].get("folder") and st.get("queued"):
            st.update(copying=False, queued=False, copy={"result": "cancelled", "reason": M("taken off the queue")})
            self._save()

    def _moving(self) -> dict:
        """The folder being moved (a ValueError when there's none: the page was out of date)."""
        if not self.state:
            raise ValueError("no folder is being moved")
        return self.state

    def _not_while_optimizing(self) -> None:
        if self.app.jobs.busy():
            raise ValueError("another file job is running: wait for it, or cancel it")

    # ------------------------------------------------------------ helpers
    @property
    def client(self):
        return self.app.optimizing.client

    def _save(self) -> None:
        self.app._save_json(STATE, self.state or {})

    def _folder(self, action: str, **kw) -> dict:
        try:
            return self.client._call("/api/folder", dict(kw, action=action), timeout=600)
        except urllib.error.HTTPError as e:
            try:
                import json
                return {"error": json.loads(e.read() or b"{}").get("error") or str(e)}
            except ValueError:
                return {"error": str(e)}
        except (OSError, ValueError, urllib.error.URLError) as e:
            return {"error": str(getattr(e, "reason", e))}

    def _props(self, dataset: str) -> dict | None | str:
        """The dataset's settings from TrueNAS (None: no such dataset; "no-api": can't ask)."""
        try:
            with self.app.truenas() as tn:
                return tn.dataset_cache_props(dataset)
        except Exception:  # noqa: BLE001 — not connected
            return "no-api"

    def _plex_count(self, key: str) -> int | None:
        try:
            return self.app.plex(timeout=30).library_count(key)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ what can be moved
    def candidates(self) -> list[dict]:
        """Every library folder, with whether it can become a dataset of its own and what it would
        be: {"library", "title", "folder", "dataset", "child", "rs", "size", "suggested", "why"}."""
        if self._cand and time.time() - self._cand[0] < 60:
            return self._cand[1]
        from . import checks
        ml = zfs.mounts()
        out = []
        files = list(self.app.files)
        for lib in getattr(self.app, "libraries", []) or []:
            if lib.get("excluded") or not lib.get("kind"):
                continue
            rec = checks.library_record(self.app, lib["key"])
            for loc in lib.get("locations") or []:
                folder = self.app.map_path(loc.rstrip("/"))
                m = zfs.mount_for(folder, ml)
                host = zfs.host_path(folder, ml)
                c = {"library": str(lib["key"]), "title": lib.get("title"), "folder": folder, "plex_folder": loc,
                     "rs": rec.get("best"), "alt": rec.get("alt"), "size": sum(f.size for f in files if f.path.startswith(loc.rstrip("/") + "/"))}
                if not m or m["fstype"] != "zfs" or not host:
                    c["why"] = M("not on a ZFS dataset here")
                elif m["mountpoint"] == folder:
                    c["why"] = M("Dedicated dataset already")
                else:
                    ds_root = "/mnt/" + m["source"]
                    rel = host[len(ds_root) + 1:] if host.startswith(ds_root + "/") else ""
                    if not rel or "/" in rel:
                        c["why"] = M("only a folder directly inside its dataset can become one")
                    else:
                        c.update(dataset=m["source"], child=f"{m['source']}/{rel}", temp=f"{m['source']}/{rel}-new",
                                 host_folder=host, temp_folder=folder + "-new", temp_host=host + "-new")
                c["suggested"] = bool(rec.get("status") == "warn")
                # what to do: its dataset's record size is the one recommended for it, the second best, or neither
                c["advice"] = {"ideal": "ideal", "pass": "sufficient", "warn": "split"}.get(rec.get("status"))
                c["now_rs"] = rec.get("rs")
                out.append(c)
        self._cand = (time.time(), out)
        return out

    # ------------------------------------------------------------ the steps
    def plan(self, library: str, folder: str) -> dict:
        c = next((x for x in self.candidates() if x["library"] == str(library) and x["folder"] == folder), None)
        if not c or c.get("why") or not c.get("child"):
            raise ValueError(str((c or {}).get("why") or "not a folder that can become a dataset"))
        if self.state and self.state.get("step") not in (None, "done"):
            raise ValueError("another folder is being moved")
        with self.lock:
            self.state = dict(c, step="create", started=time.time(), log=[])
        self._save()
        return self.refresh()

    def cancel(self) -> None:
        """Stop and forget (before the switch: nothing has changed but the new dataset, which you delete)."""
        if self.state and self.state.get("step") in ("switch", "verify") and self.state.get("old_path"):
            raise ValueError("the folder has been renamed already: undo that first")
        if self.state and self.state.get("queued"):
            self.app.jobs.remove(kind="copy", args={"folder": self.state["folder"], "mirror": bool(self.state.get("mirror"))})
        self._folder("stop")
        with self.lock:
            self.state = None
        self._save()

    def refresh(self) -> dict:
        """Check the current step's conditions again. {"state", "checks"}."""
        st = self.state
        if not st:
            self.checks = []
            return self.snapshot()
        step = st.get("step")
        checks = []

        def check(cid, ok, title, detail="", cmd="", action=""):
            checks.append({"id": cid, "ok": ok, "title": title, "detail": detail, "cmd": cmd, "action": action})
            return ok

        if step == "create":
            self._check_create(st, check)
        elif step == "copy":
            self._check_copy(st)
        elif step == "switch":
            self._check_switch(st, check)
        elif step == "verify":
            self._check_verify(st, check)
        with self.lock:
            self.checks = checks
            self.checked_at = time.time()
        self._save()
        # Health says when a step waits on you: checked again whenever what's next changes
        waiting = (step, st.get("copying"), next((c["id"] for c in checks if not c["ok"]), None))
        if waiting != getattr(self, "_waiting", None):
            self._waiting = waiting
            run = getattr(self.app, "run_checks_async", None)
            if run:
                run()
        return self.snapshot()

    def _check_create(self, st: dict, check) -> None:
        ready = self.client.ready()
        if not check("optimizer", ready, M("The optimizer is running"),
                     "" if ready else M("Set it up first: see the top of this page.")):
            return
        status = self.client.status(fresh=True)
        f = self._folder("probe", path=st["folder"])
        check("folder", bool(f.get("exists") and f.get("parent_writable")), M("The optimizer can rename the folder"),
              "" if f.get("parent_writable") else M("It needs the media mounted writable (its volumes, in the app’s YAML)."))
        need = int(st.get("size") or 0) * MARGIN
        free = f.get("free")
        check("space", free is None or free >= need, M("Room for a second copy"),
              M("{free} free, {need} needed.", free=_gb(free), need=_gb(need)) if free is not None else "")
        props = self._props(st["temp"])
        if props == "no-api":
            check("api", False, M("The TrueNAS API is connected"),
                  M("It’s how the new dataset’s record size and compression are checked: System → TrueNAS."))
            return
        if not check("dataset", props is not None, M("The new dataset {name} is there", name=st["temp"]),
                     "" if props else M("Create it as below, then check again.")):
            return
        rs = props.get("recordsize")
        good = rs in (st.get("rs"), st.get("alt"))
        check("recordsize", good, M("Record size {size}", size=_rs(rs)),
              "" if good else M("It should be {best} (or {alt}): set it in TrueNAS before copying, or the files are written "
                                "with the wrong one.", best=_rs(st.get("rs")), alt=_rs(st.get("alt"))))
        g = zfslog.grade(props.get("compression"))
        check("compression", g == zfslog.GOOD, M("Compression {value}", value=props.get("compression")),
              "" if g == zfslog.GOOD else M("It should be ZSTD (levels 3 to 19): set it in TrueNAS before copying."))
        st["owner"] = {"uid": f.get("uid"), "gid": f.get("gid"), "mode": f.get("mode")}
        st.setdefault("seen", time.time())  # (when it was first there: an optimizer started before can't see it)
        t = self._folder("probe", path=st["temp_folder"])
        if not self._sees(st, check, status, bool(t.get("exists") and t.get("is_mount")), st["temp_folder"], st["seen"]):
            return
        check("writable", bool(t.get("writable")), M("The optimizer can write to it"),
              "" if t.get("writable") else M("Give it the folder’s owner: {uid}:{gid}.", uid=f.get("uid"), gid=f.get("gid")))

    def _sees(self, st: dict, check, status: dict, sees: bool, path: str, seen: float) -> bool:
        """Whether the optimizer sees a dataset TrueNAS has (at ``path``, there since ``seen``). Its
        media volume shows it new datasets only with propagation (rslave); otherwise it sees them once
        restarted — which it can be here (restart()). Said as it is: restarting now, needed (started
        before the dataset), or restarted since and still not seen (something else is wrong)."""
        if sees:
            check("sees", True, M("The optimizer sees the new dataset"))
            return True
        props = status.get("propagation") or {}
        root = next((r for r in props if path.startswith(r.rstrip("/") + "/")), None)
        since, asked = status.get("since"), st.get("restart_asked")
        snippet, action = "", ""
        if asked and time.time() - asked < RESTART_WAIT and not (since and since > asked):
            detail = M("Restarting Plex Cache… this is checked again once it’s back (a minute or so).")
        elif root and props.get(root):
            detail = M("Its media volume passes new datasets on: it should show up by itself in a few seconds.")
        elif since and since > seen:
            detail = M("The optimizer was started after the dataset appeared, yet doesn’t see it at {path}: the dataset has to be inside the folder mounted there.",
                       path=path)
        else:
            action = "restart"
            detail = M("The optimizer was started before the dataset appeared, so it needs a restart to see it. Restart Now restarts Plex Cache "
                       "(both its containers, a minute or so); anything running picks up where it was. To never need this again, make the "
                       "optimizer’s media volume read like this in Apps → {app} → Edit:", app=self._app_name() or "plex-cache")
            if root:
                snippet = (f"      - type: bind\n        source: {zfs.host_path(root, zfs.mounts()) or root}\n        target: {root}\n"
                           "        bind:\n          propagation: rslave")
        check("sees", False, M("The optimizer sees the new dataset"), detail, snippet, action)
        return False

    def _app_name(self) -> str | None:
        try:
            return (self.app.optimizing._containers() or {}).get("app")
        except Exception:  # noqa: BLE001 — not connected: unnamed
            return None

    def restart(self) -> dict:
        """Restart Plex Cache (both containers) through TrueNAS, so the optimizer sees a dataset made
        since it started. TrueNAS carries it out: this app goes down with it, and picks up after."""
        st = self.state
        if not st or st.get("step") not in ("create", "switch"):
            raise ValueError("nothing here needs a restart")
        name = self._app_name()
        if not name:
            raise ValueError(M("Plex Cache can’t restart itself without the TrueNAS API (System → TrueNAS, with a key allowed to manage apps). "
                               "Restart it in TrueNAS: Apps → plex-cache → Restart."))
        with self.lock:
            st["restart_asked"] = time.time()
        self._save()
        try:
            with self.app.truenas() as tn:
                tn.call("app.redeploy", [name])
        except Exception as e:  # noqa: BLE001 — not allowed, or an older TrueNAS
            with self.lock:
                st.pop("restart_asked", None)
            self._save()
            raise ValueError(M("TrueNAS didn’t restart {app}: {why}. Restart it there: Apps → {app} → Restart.", app=name, why=str(e))) from None
        return self.snapshot()

    def _check_copy(self, st: dict) -> None:
        """The copy runs as a file job (CopyJob, below), which updates this state when it ends; a
        copy marked running with no job behind it (stopped some other way) isn't running."""
        if st.get("copying") and not st.get("queued") and not self.app.jobs.busy():
            st["copying"] = False

    def copy_finished(self, result: dict) -> None:
        """The copy (CopyJob) ended: on to the switch, or, after the last pass, compare both copies."""
        st = self.state
        if not st:
            return
        first = result.get("result") == "copied" and not st.get("mirror")
        move = False
        with self.lock:
            st["copy"] = result
            if first:
                st["step"] = "switch"
            elif result.get("result") == "copied":
                # the last pass is done: compare both, file by file, straight away — and when it was asked for
                # before moving the old folder, move it now (if both are the same)
                st["compare"] = self._folder("compare", path=st["folder"], to=st["temp_folder"])
                st["compare_at"] = time.time()
                st["last_pass"] = {k: result.get(k) for k in ("files_total", "copied", "skipped", "removed")}
                move = bool(st.pop("move_after", False)) and bool(st["compare"].get("same"))
            else:
                st.pop("move_after", None)
            st["copying"] = False
        self._save()
        if first:
            # the copy is done: the last pass straight after (what changed meanwhile, then both compared),
            # so the result is there to see — it's run again just before the old folder is moved
            try:
                self.start_copy(mirror=True)
            except ValueError:
                pass
        if move:
            try:
                self.rename_old(from_pass=True)
            except ValueError as e:
                st["move_error"] = str(e)
                self._save()

    def _check_switch(self, st: dict, check) -> None:
        self._check_copy(st)
        if st.get("copying"):
            return
        cmp = st.get("compare")
        check("compare", bool(cmp and cmp.get("same")), M("Both copies the same, file by file"),
              M("{files} files, {size}.", files=cmp.get("files"), size=_gb(cmp.get("bytes"))) if cmp and cmp.get("same") else
              (M("Not compared yet: the last pass runs by itself once the copy is done.") if not cmp else
               M("Different: {list}", list=", ".join((cmp.get("missing") or []) + (cmp.get("different") or []) + (cmp.get("extra") or []))[:300])))
        check("renamed", bool(st.get("old_path")), M("The old folder set aside"),
              M("Now {path}.", path=os.path.basename(st["old_path"])) if st.get("old_path")
              else M("Waiting for you: Compare, Then Set the Old Folder Aside (step 3, below)."))
        if not st.get("old_path"):
            return
        props = self._props(st["child"])
        f = self._folder("probe", path=st["folder"])
        named = isinstance(props, dict) and bool(f.get("is_mount"))
        check("dataset", isinstance(props, dict), M("The dataset is now {name}, at the folder’s path", name=st["child"]),
              "" if isinstance(props, dict) else M("Rename it in TrueNAS, as below."))
        if isinstance(props, dict) and not named:
            st.setdefault("renamed", time.time())
            self._sees(st, check, self.client.status(fresh=True), False, st["folder"], st["renamed"])
        if named:
            st["step"] = "verify"
            self._released()  # the folder's path holds its files again: other file jobs carry on

    def _check_verify(self, st: dict, check) -> None:
        props = self._props(st["child"])
        f = self._folder("probe", path=st["folder"])
        ok = isinstance(props, dict) and f.get("is_mount")
        check("dataset", bool(ok), M("The folder is the dataset {name}", name=st["child"]))
        if isinstance(props, dict):
            check("settings", props.get("recordsize") in (st.get("rs"), st.get("alt")) and zfslog.grade(props.get("compression")) == zfslog.GOOD,
                  M("Record size {size}, compression {value}", size=_rs(props.get("recordsize")), value=props.get("compression")))
        if st.get("old_path"):
            cmp = self._folder("compare", path=st["old_path"], to=st["folder"])
            check("files", bool(cmp.get("same")), M("Every file there"),
                  M("{files} files, {size}.", files=cmp.get("files"), size=_gb(cmp.get("bytes"))) if cmp.get("same") else str(cmp.get("error") or ""))
        now_count = self._plex_count(st["library"])
        before = st.get("plex_before")
        check("plex", before is None or now_count == before,
              M("Plex shows {now} items in {library}, as before", now=now_count, library=st.get("title")) if now_count == before else
              M("Plex shows {now} items in {library}; before: {before}. Restart Plex, wait for it to scan, then check again.",
                now=now_count, library=st.get("title"), before=before))

    # ------------------------------------------------------------ actions
    def start_copy(self, mirror: bool = False) -> dict:
        st = self.state
        if not st or st.get("step") not in ("create", "switch"):
            raise ValueError("not ready to copy")
        if st["step"] == "create":
            self.refresh()
            if not all(c["ok"] for c in self.checks):
                raise ValueError("the checks above aren't all done")
        if st.get("old_path"):
            raise ValueError("the folder has been renamed already")
        with self.lock:
            st.update(step="copy" if st["step"] == "create" else st["step"], copying=True, mirror=mirror, copy_started=time.time())
            st.pop("compare", None)
            if mirror:
                st["plex_before"] = self._plex_count(st["library"])  # Plex's count before the switch
        # now, or queued after the file jobs before it
        st["queued"] = True
        self._save()
        if self.app.jobs.enqueue("copy", {"folder": st["folder"], "mirror": mirror}, library=st.get("library"),
                                 title=st.get("title"), folder=st["folder"], child=st.get("child")) is None:
            st.update(copying=False, queued=False)
            self._save()
            raise ValueError("it's queued or copying already")
        return self.snapshot()

    def pass_then_move(self) -> dict:
        """The last pass again, and the old folder moved out of the way straight after if both copies
        are then the same: what changed since the last check is copied first."""
        st = self._moving()
        if st.get("old_path"):
            raise ValueError("the folder has been moved already")
        out = self.start_copy(mirror=True)
        with self.lock:
            st["move_after"] = True
            st.pop("move_error", None)
        self._save()
        return out

    def rename_old(self, from_pass: bool = False) -> dict:
        """Set the old folder aside (renamed, in the same place). ``from_pass``: by the last pass as it
        ends (its own job, still finishing, is the file job running then). Otherwise only straight after
        a comparison: the move always follows a last pass (pass_then_move)."""
        if not from_pass:
            self._not_while_optimizing()
        st = self._moving()
        if not (st.get("compare") or {}).get("same"):
            raise ValueError("compare both copies first (the last pass)")
        if not from_pass and time.time() - (st.get("compare_at") or 0) > FRESH_COMPARE:
            raise ValueError("compare both copies again first: the last pass runs just before the move")
        st.pop("move_error", None)
        name = "." + os.path.basename(st["folder"]) + ".plexcache-old"
        r = self._folder("rename", path=st["folder"], name=name)
        if r.get("error"):
            raise ValueError(r["error"])
        st["old_path"] = r["path"]
        self._save()
        return self.refresh()

    def undo_rename(self) -> dict:
        st = self._moving()
        if not st.get("old_path") or st.get("step") != "switch":
            raise ValueError("nothing to undo")
        r = self._folder("rename", path=st["old_path"], name=os.path.basename(st["folder"]))
        if r.get("error"):
            raise ValueError(r["error"])
        st.pop("old_path", None)
        self._save()
        self._released()  # (other file jobs no longer wait)
        return self.refresh()

    def delete_old(self) -> dict:
        self._not_while_optimizing()
        st = self._moving()
        self.refresh()
        if st.get("step") != "verify" or not all(c["ok"] for c in self.checks):
            raise ValueError("verify first: every check must pass")
        r = self._folder("delete", path=st["old_path"])
        if r.get("error"):
            raise ValueError(r["error"])
        # the copied files were written with the new dataset's settings: Optimal in the ZFS record
        props = self._props(st["child"])
        if isinstance(props, dict):
            self.app.optimizing.zfs.observe_datasets({st["child"]: {"rs": props.get("recordsize"),
                                                                    "compression": props.get("compression")}})
        st.update(step="done", finished=time.time())
        st.pop("old_path", None)
        self._save()
        self.app.activity.event("optimizer", M("{folder} is now the dataset {name}", folder=st["folder"], name=st["child"]))
        return self.snapshot()

    def snapshot(self) -> dict:
        with self.lock:
            return {"state": dict(self.state) if self.state else None, "checks": list(self.checks), "checked_at": self.checked_at}


class CopyJob(Job):
    """Copying the folder into its new dataset (or the last pass) as a file job: the same progress,
    pause, resilver pause, estimate and resuming after a restart as optimizing. The optimizer does
    the copying; this follows it, pauses it — by hand, for a resilver, or while something plays —
    and, after a restart, picks it up (still running: followed; not: started again, skipping
    what's copied)."""

    kind = "copy"

    def __init__(self, mig: Migration, resumed: dict | None = None):
        self.mig = mig
        st = mig.state or {}
        super().__init__(mig.app.jobs, resumed, library=st.get("library"), title=st.get("title"), folder=st.get("folder"),
                         to=st.get("temp_folder"), child=st.get("child"), mirror=bool(st.get("mirror")))
        self._held = False

    def args(self) -> dict:
        return {"folder": self.state["folder"], "mirror": self.state["mirror"]}

    def dataset(self) -> str | None:
        return (self.mig.state or {}).get("dataset")

    def extra_pause(self) -> str | None:
        return M("something is playing") if self.app.monitor.playing_paths() else None

    def apply_pause(self, paused: bool) -> None:
        self.mig._folder("pause" if paused else "resume")  # the optimizer pauses within the file in hand
        self._held = paused

    def run(self) -> None:
        mig, s = self.mig, self.state
        client = mig.client
        if mig.state:
            mig.state.update(copying=True, queued=False)  # its turn (it may have waited in the queue)
            mig._save()
        status = client.status(fresh=True)
        cur = status.get("current") or {}
        sent = time.time()
        if not (cur.get("action") == "copy" and cur.get("path") == s["folder"]):  # not still running from before a restart
            while True:
                r = mig._folder("copy", path=s["folder"], to=s["to"], mirror=s["mirror"])
                if r.get("started"):
                    break
                if r.get("error") != "busy" or self.stop_event.wait(5):
                    mig.copy_finished({"result": "failed", "reason": r.get("error") or "not started"})
                    return
        last_t, last_b = time.time(), None
        while True:
            # paused by hand, for a resilver, or while something plays: the optimizer waits within the file
            by, why = self.pause_reason()
            self.update(paused_by=by, paused_why=why)
            self.log_pause(by, why)
            if by and not self._held:
                self.apply_pause(True)
            elif not by and self._held:
                self.apply_pause(False)
            if self.stopped:
                mig._folder("stop")
            status = client.status(fresh=True)
            cur = status.get("current") or {}
            now = time.time()
            if cur.get("action") == "copy" and cur.get("path") == s["folder"]:
                b = int(cur.get("bytes_done") or 0)
                with self.jobs.lock:
                    s.update(phase="working", total=cur.get("files_total") or 0, done=cur.get("files_done") or 0,
                             todo_bytes=cur.get("bytes_total") or 0, done_bytes=b, current=cur.get("current"), step=None)
                    if last_b is not None and not by and b > last_b:  # the rate, from time spent copying
                        s["work_seconds"] += now - last_t
                        s["work_bytes"] += b - last_b
                    self._estimate()
                last_t, last_b = now, b
                self.save()
            else:
                result = next((r for r in reversed(status.get("results") or []) if r.get("action") == "copy"
                               and r.get("path") == s["folder"] and r.get("finished", 0) >= sent - 1), None)
                if result:
                    with self.jobs.lock:  # (a last pass with nothing to copy can end before it's followed: its counts from here)
                        s.update(total=result.get("files_total") or s.get("total") or 0, done=result.get("files_done") or s.get("done") or 0,
                                 todo_bytes=result.get("bytes_total") or s.get("todo_bytes") or 0,
                                 done_bytes=result.get("bytes_done") or s.get("done_bytes") or 0,
                                 copied=result.get("copied") or 0, skipped=result.get("skipped") or 0)
                    # the copy, in the job log: what was copied and skipped (picked up again), and its outcome
                    self.log(type="file", file=s["folder"], result=result.get("result"), reason=result.get("reason"),
                             seconds=round(now - sent, 1), size_before=result.get("bytes_total"), size_after=result.get("bytes_done"),
                             verified="passed" if result.get("result") == "copied" else "failed" if result.get("result") == "failed" else None,
                             copied=result.get("copied"), skipped=result.get("skipped"), removed=result.get("removed"),
                             owners_not_kept=result.get("owners_not_kept"))
                    if result.get("result") not in ("copied",):
                        with self.jobs.lock:  # listed with the job, like a file the optimizer couldn't do
                            s["failed"] += result.get("result") == "failed"
                            s["errors"].append({"file": s["folder"], "reason": str(result.get("reason") or result.get("result")),
                                                "at": time.time()})
                    mig.copy_finished(result)
                    return
                if not status.get("error") and now - sent > 30:
                    mig.copy_finished({"result": "failed", "reason": "the optimizer restarted while copying"})
                    return
            self.stop_event.wait(2) if not self.stopped else time.sleep(1)

    def finished(self) -> None:
        if self._held:
            self.apply_pause(False)
        st = self.mig.state
        if st and st.get("copying") and not st.get("queued"):  # (not the last pass, queued by the copy as it ended)
            st["copying"] = False
            self.mig._save()


def _gb(n) -> str:
    return f"{(n or 0) / 1e9:,.1f} GB"


def _rs(n) -> str:
    if not n:
        return "?"
    return f"{n // (1024 * 1024)}M" if n >= 1024 * 1024 else f"{n // 1024}K"

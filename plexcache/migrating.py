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

The state is kept in migration.json, so the page picks up at the same step after a restart — and is
checked against the disk (reconcile): a conversion left partway (the new dataset there, the old folder
set aside) is found by what's on disk, even with migration.json gone or out of date, and picked up
where it is. Only the optimizer writes media; the TrueNAS API is only read.

**A mirrored folder.** A folder kept in step with another site (a folder sync, a mirror chosen on the
Mirror page) is converted so that nothing of it reaches the other site:

* **The folder sync** — Syncthing's root (its .stfolder) is the library folder itself (setting the
  folder aside stops Syncthing there, the copy carries .stfolder along), or one above it, whose
  .stignore must then leave out ``<name>-new`` and ``.<name>.plexcache-old``; it's paused here from
  the last pass until the dataset is in place, and resumed after. With a mirror chosen and no
  Syncthing found, you say nothing else syncs the folder.
* **The other Plex Cache** (peerlink.py, or its marker: peers.py) — it must run TrueNAS; it's
  converted too (both servers, one after the other: this one first) or not (this server only), as
  its room for its own copy allows; a conversion of the library left partway there is a warning
  until the switch here, and stops it from then on — completed or reverted there first; it holds
  its own file jobs on the library while this side switches (mirrorhealth.converting); and once
  switched, the dataset here holds the same files as the other site.

Each side only tells the other where it is (public(), folders()); neither asks the other to do
anything.
"""

import os
import re
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
KEEP_CLEAR = 1 << 30  # and 1 GB kept clear beyond it (safety.MARGIN): what the other side's room is told against
UNDER_WAY = ("create", "copy", "switch", "verify")  # a conversion started and not finished
HOLD_STEPS = ("switch", "verify")  # past the copy: the other side holds its file jobs on the library (mirrorhealth.converting)
SYNC_MARK, SYNC_IGNORE = ".stfolder", ".stignore"  # Syncthing's folder marker, and its ignore patterns, at its folder's root
RECENT = 3600  # a file changed this recently may still be syncing: not compared with the other site's
FILES_EVERY = 600  # seconds between comparisons with the other site's files (Verify), but on Check Again


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
            from .optimizing import error_body
            try:
                import json
                return {"error": json.loads(error_body(e) or b"{}").get("error") or str(e)}
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
                    c["is_dataset"] = True
                    if os.path.isdir(folder + "-new"):  # (a conversion's new dataset left beside the finished one)
                        c["leftover"] = folder + "-new"
                    old = _old_path(folder)
                    if os.path.isdir(old):  # the switch done, the old copy still there: Verify is next
                        c.update(partial={"step": "verify", "old_path": old}, dataset=m["source"].rsplit("/", 1)[0], child=m["source"],
                                 temp=m["source"] + "-new", host_folder=host, temp_folder=folder + "-new", temp_host=host + "-new")
                else:
                    ds_root = "/mnt/" + m["source"]
                    rel = host[len(ds_root) + 1:] if host.startswith(ds_root + "/") else ""
                    if not rel or "/" in rel:
                        c["why"] = M("only a folder directly inside its dataset can become one")
                    else:
                        c.update(dataset=m["source"], child=f"{m['source']}/{rel}", temp=f"{m['source']}/{rel}-new",
                                 host_folder=host, temp_folder=folder + "-new", temp_host=host + "-new")
                        # left partway (found on disk, whatever migration.json says): the old folder set aside, the dataset
                        # not yet renamed — Switch; or the new dataset made, the folder still there — Create (the copy skips
                        # what's copied already)
                        old = _old_path(folder)
                        if os.path.isdir(old):
                            c["partial"] = {"step": "switch", "old_path": old}
                        elif os.path.isdir(folder + "-new"):
                            c["partial"] = {"step": "create"}
                c["suggested"] = bool(rec.get("status") == "warn")
                # what to do: its dataset's record size is the one recommended for it, the second best, or neither
                c["advice"] = {"ideal": "ideal", "pass": "sufficient", "warn": "split"}.get(rec.get("status") or "")
                c["now_rs"] = rec.get("rs")
                out.append(c)
        self._cand = (time.time(), out)
        return out

    # ------------------------------------------------------------ the steps
    def plan(self, library: str, folder: str) -> dict:
        c = next((x for x in self.candidates() if x["library"] == str(library) and x["folder"] == folder), None)
        if not c or (c.get("why") and not c.get("partial")) or not c.get("child"):
            raise ValueError(str((c or {}).get("why") or "not a folder that can become a dataset"))
        if self.state and self.state.get("step") not in (None, "done"):
            raise ValueError("another folder is being moved")
        part = c.get("partial") or {}
        st: dict = dict(c, step=part.get("step") or "create", started=time.time(), log=[], scope=self._default_scope())
        st.pop("partial", None)
        if part.get("old_path"):  # picked up past the set-aside: the old folder is where it was put
            st["old_path"] = part["old_path"]
            st["picked_up"] = part["step"]
            if part["step"] == "switch":  # (compared again, the new dataset against the old folder: what Switch shows)
                st["compare"] = self._folder("compare", path=part["old_path"], to=st["temp_folder"])
                st["compare_at"] = time.time()
        elif part:
            st["picked_up"] = part["step"]
        with self.lock:
            self.state = st
        self._save()
        return self.refresh()

    def _default_scope(self) -> str:
        """Both servers where a mirror is watched (the files are the same, so the record size that suits them is too),
        else this one."""
        return "both" if self._mirror_watched((self.state or {}).get("library")) else "this"

    def set_scope(self, scope: str) -> dict:
        """Both servers, or this server only: chosen before the copy."""
        st = self._moving()
        if scope not in ("both", "this"):
            raise ValueError("scope must be both or this")
        if st.get("step") not in ("create", "copy"):
            raise ValueError("chosen before the switch")
        st["scope"] = scope
        self._save()
        return self.refresh()

    def confirm(self, what: str) -> dict:
        """What you say you've done where the app can't see it: the folder sync paused here before the set-aside
        (sync_paused), resumed after the switch (sync_resumed), or, with no Syncthing found, nothing else syncing the folder
        in a way that would carry the new dataset or the old folder away (sync_checked)."""
        st = self._moving()
        if what not in ("sync_paused", "sync_resumed", "sync_checked"):
            raise ValueError("nothing to confirm")
        if what == "sync_resumed" and st.get("step") != "verify":
            raise ValueError("resumed once the dataset is in place")
        st[what] = time.time()
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

    def refresh(self, force: bool = False) -> dict:
        """Check the current step's conditions again (``force``: Check Again — the other site's files compared afresh too).
        {"state", "checks"}."""
        st = self.state
        if not st:
            self.checks = []
            return self.snapshot()
        step = st.get("step")
        checks = []

        def check(cid, ok, title, detail="", cmd="", action="", level="", gate=""):
            # level: "" a step to do, "warn" said but stopping nothing, "error" something wrong that stops the step (red);
            # gate: what it stops besides the step's own button ("aside": setting the old folder aside)
            checks.append({"id": cid, "ok": ok, "title": title, "detail": detail, "cmd": cmd, "action": action, "level": level, "gate": gate})
            return ok

        self._reconcile(st)
        step = st.get("step")
        if step == "create":
            self._check_create(st, check)
        elif step == "copy":
            self._check_copy(st)
        elif step == "switch":
            self._check_switch(st, check)
        elif step == "verify":
            self._check_verify(st, check)
        if step in UNDER_WAY:
            self._check_mirror(st, check, force)  # (the mirror first: chosen, or not, decides what the folder sync is asked)
            self._check_sync(st, check)
        with self.lock:
            self.checks = checks
            self.checked_at = time.time()
        self._save()
        told = (step, bool(st.get("old_path")))
        if told != getattr(self, "_told", None):  # where it is, told to the other side (its marker) as it changes
            self._told = told
            pub = getattr(self.app, "publish_peers", None)
            if pub:
                threading.Thread(target=pub, name="peers", daemon=True).start()
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
                     "" if props else M("Create it as below, then check again.")) or not isinstance(props, dict):
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
            libdata = getattr(self.app, "libdata", None)
            if libdata is not None:  # (its files read again, as they are on the dataset: what's compared with the other site's)
                libdata.refresh(str(st.get("library") or ""), ("files",))

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

    # ------------------------------------------------------------ the disk, the folder sync and the other site
    def _reconcile(self, st: dict) -> None:
        """migration.json checked against the disk: what's there wins, and the page says what was put right
        (st["corrected"]). The old folder found set aside that the state doesn't know of: Switch, from there; the old folder
        the state says is set aside back at its name (put back by hand): set aside no more; gone, with the dataset in its
        place (deleted by hand): nothing left to compare it with."""
        folder = st.get("folder") or ""
        if not folder:
            return
        old, step, said = _old_path(folder), st.get("step"), None
        if step in ("create", "copy", "switch") and not st.get("old_path") and os.path.isdir(old) and not st.get("copying"):
            st.update(old_path=old, step="switch", copying=False, queued=False)
            st.setdefault("compare", self._folder("compare", path=old, to=st.get("temp_folder") or folder + "-new"))
            st.setdefault("compare_at", time.time())
            said = M("The old folder was found set aside as {name}: picked up from there.", name=os.path.basename(old))
        elif st.get("old_path") and not os.path.isdir(st["old_path"]):
            if step == "switch" and os.path.isdir(st.get("temp_folder") or ""):
                said = M("The old folder is back at its name: set it aside again when ready.")
            else:
                said = M("The old folder isn’t there any more: nothing is left to compare the dataset with.")
            st.pop("old_path", None)
        if said is not None:
            st["corrected"] = said
            self._released()

    @staticmethod
    def _collector(into: list):
        def check(cid, ok, title, detail="", cmd="", action="", level="", gate=""):
            into.append({"id": cid, "ok": ok, "title": title, "detail": detail, "cmd": cmd, "action": action, "level": level, "gate": gate})
            return ok
        return check

    def _check_gate(self) -> None:
        """What stops the set-aside looked at again by itself (the folder sync, the other site), the step's other checks as
        they were."""
        st, new = self.state or {}, []
        check = self._collector(new)
        self._check_sync(st, check)
        self._check_mirror(st, check)
        ids = {c["id"] for c in new}
        with self.lock:
            self.checks = [c for c in self.checks if c["id"] not in ids] + new

    def blocking(self, gate: str | None = None) -> list[dict]:
        """The checks not done that stop the step (a warning stops nothing) — or, with ``gate``, only those that stop it."""
        with self.lock:
            checks = list(self.checks)
        return [c for c in checks if not c["ok"] and c.get("level") != "warn" and (gate is None or c.get("gate") == gate)]

    def _check_sync(self, st: dict, check) -> None:
        """The folder sync over the folder: Syncthing's root (.stfolder) the folder itself, or one above whose .stignore
        leaves out the new dataset and the old folder; paused here before the set-aside, resumed after. With no Syncthing
        found and a mirror chosen, said by you. Nothing with neither."""
        folder, step = st.get("folder") or "", st.get("step")
        if not folder:
            return
        root, watched = sync_root(folder), self._mirror_watched(st.get("library")) or bool(self._mirror_seen(st.get("library")))
        if not root and not watched and not st.get("sync_paused"):  # (paused: said, even once the folder it was found in is set aside)
            return
        name = os.path.basename(folder)
        temp, old = os.path.basename(st.get("temp_folder") or folder + "-new"), os.path.basename(_old_path(folder))
        if step in ("create", "copy"):
            if root == folder:
                check("sync_root", True, M("Syncthing’s folder is the library folder itself"),
                      M("Setting the folder aside stops Syncthing there instead of sending deletes, and the copy carries its .stfolder into the new dataset."))
            elif root:
                rel = os.path.relpath(os.path.dirname(folder), root)
                lines = ["/" + (n if rel == "." else rel + "/" + n) for n in (temp, old)]
                missing = [ln for ln in lines if not ignored(root, ln[1:])]
                check("sync_ignore", not missing, M("Syncthing leaves out {temp} and {old}", temp=temp, old=old),
                      "" if not missing else M("Syncthing’s folder is {root}, above the library folder: it would send the new dataset to the other site "
                                               "as a second copy, and the set-aside as every file deleted. Add these lines to its ignore patterns on this server "
                                               "(Syncthing → the folder → Edit → Ignore Patterns, kept in {file}), then check again:",
                                               root=root, file=os.path.join(root, SYNC_IGNORE)), "\n".join(missing))
            else:
                done = bool(st.get("sync_checked"))
                check("sync_checked", done, M("Nothing else syncs the folder in a way that would carry the new dataset or the old folder away"),
                      "" if done else M("No Syncthing folder was found over {folder}, yet there’s a mirror. If something else keeps it in step with the "
                                        "other site (another sync tool, a script), have it leave out {temp} and {old}, and pause it during the switch.",
                                        folder=folder, temp=temp, old=old), action="" if done else "sync_checked")
        elif step == "switch":
            paused = bool(st.get("sync_paused"))
            check("sync_paused", paused or bool(st.get("old_path")), M("The folder sync paused here"),
                  "" if paused else M("Pause the folder in Syncthing on this server (Folders → {name} → Pause), or whatever else syncs it, before the old "
                                      "folder is set aside, and leave it paused until the dataset is in place. What the other site changes meanwhile waits "
                                      "there, and comes over once it’s resumed.", name=name),
                  action="" if paused or st.get("old_path") else "sync_paused", gate="aside")
        elif step == "verify" and (st.get("sync_paused") or root):
            done = bool(st.get("sync_resumed"))
            check("sync_resumed", done, M("The folder sync resumed here"),
                  "" if done else M("Resume the folder in Syncthing on this server: it finds the same files where they were, and sends nothing."),
                  action="" if done else "sync_resumed")

    def _mirror_seen(self, library) -> dict | None:
        """A mirror of the library's files seen though not chosen on the Mirror page — a server of the account with the same
        files, or another Plex Cache sharing the folders (tools.mirrored, as every tool goes by) — or None."""
        if not library:
            return None
        from .tools import mirrored
        try:
            seen = mirrored(self.app, str(library))
        except Exception:  # noqa: BLE001 — not known: none seen
            return None
        return seen if seen and seen.get("how") != "watched" else None

    def _seen_text(self, seen: dict):
        return M("{name} looks like a mirror of this library ({library} there), but the library isn’t marked as mirrored, so the conversion can’t "
                 "check it or work with it. Mark it as the mirror, or say the library has no mirror (here, or on the [Mirror page](/mirrors)).",
                 name=seen.get("name") or "?", library=seen.get("their_title") or "?") \
            if seen.get("how") == "suspected" else \
            M("Another Plex Cache ({name}) shares these folders, but the library isn’t marked as mirrored, so the conversion can’t check it or work "
              "with it. On the [Mirror page](/mirrors), choose its Plex server and mark the library as mirrored, or say it has no mirror.", name=seen.get("name") or "?")

    def _mirror_watched(self, library=None) -> bool:
        """Whether the library (or, with none named, any library) is marked as mirrored, by a server reached."""
        other = getattr(self.app, "mirrors", None)
        try:
            if other is None or not other.any_mirror():
                return False
            return (other.state_of(str(library)) == "mirror" and other.configured(other.server_of(str(library)))) if library else True
        except Exception:  # noqa: BLE001 — not set up: no mirror
            return False

    def _mirror(self, fresh: bool = False, library: str = "") -> dict | None:
        """What the Plex Cache at the site of the library's mirror says of itself (peerlink.facts): asked again first when
        ``fresh`` and the last answer is over a minute old. The library: the one named, else the conversion's."""
        links = getattr(self.app, "peerlinks", None)
        link = links.for_library(str(library or (self.state or {}).get("library") or "")) if links is not None else None
        if link is None:
            return None
        try:
            if fresh and link.talking() and time.time() - link.last_ok > 60:
                link.talk()
            return link.facts()
        except Exception:  # noqa: BLE001 — not reachable: not known
            return None

    def _mirror_name(self, f: dict | None, library: str = ""):
        other = getattr(self.app, "mirrors", None)
        sid = other.server_of(str(library or (self.state or {}).get("library") or "")) if other is not None else ""
        return (f or {}).get("name") or (other.name_of(sid) if other is not None and sid else "") or M("the other server")

    def _check_mirror(self, st: dict, check, force: bool = False) -> None:
        """With a mirror chosen: the other side runs TrueNAS; both servers converted (one after the other), with room there
        for its own copy, or this one only; no conversion of the library left partway there — a warning until the switch,
        an error from then on, until it's completed or reverted there; the other side holding its file jobs while this one
        switches; once switched, the same files here as there."""
        if not self._mirror_watched(st.get("library")):
            seen = self._mirror_seen(st.get("library"))
            if seen and st.get("step") == "create":
                check("mirror_chosen", False, M("The library’s mirror chosen"), self._seen_text(seen))
            return
        step, key = st.get("step"), str(st.get("library") or "")
        f = self._mirror(fresh=True, library=key)
        name = self._mirror_name(f, key)
        lib = ((f or {}).get("libraries") or {}).get(key) or {}
        there = their_folder(lib.get("folders"), os.path.basename(st.get("folder") or ""))
        conv = lib.get("converting")
        partly = bool(conv and conv.get("step") in UNDER_WAY)
        title = st.get("title") or os.path.basename(st.get("folder") or "")
        when = time.strftime("%d %b", time.localtime(conv["since"])) if conv and partly and conv.get("since") else "?"
        partial_text = M("{name} has a partly finished conversion of {library} ({step}, since {when}): complete it or revert it there before this "
                         "one can finish. To revert it there before its set-aside: Cancel, then delete {temp} in TrueNAS; after it: Put the Old "
                         "Folder Back, Cancel, then delete {temp}.", name=name, library=title, step=step_name(conv.get("step")) if conv and partly else "",
                         when=when, temp=os.path.basename(st.get("temp_folder") or ""))
        if step == "create":
            self._check_truenas(f, name, check)
            scope = st.get("scope") or "this"
            if scope == "both":
                done_there = (there or {}).get("state") == "dataset"
                check("scope", True, M("Both servers: {name} converts its copy once this one is done", name=name),
                      M("{name} has it as a child dataset already.", name=name) if done_there else
                      M("The files are the same on both, so the record size that suits them is too. Or convert this server only."), action="scope")
                room = (there or {}).get("room")
                if done_there:
                    pass
                elif not room:
                    check("mirror_room", False, M("Room on {name} for its own copy", name=name),
                          M("Not known yet: {name} hasn’t said (an older Plex Cache, or not reached). It checks again when its turn comes.", name=name), level="warn")
                else:
                    check("mirror_room", bool(room.get("ok")), M("Room on {name} for its own copy", name=name),
                          M("{free} free there; its copy needs {need}.", free=_gb(room.get("free")), need=_gb(room.get("needed"))) if room.get("ok") else
                          M("{free} free there, and its copy needs {need}: make room there, or convert this server only.", free=_gb(room.get("free")),
                            need=_gb(room.get("needed"))))
                other = (f or {}).get("migration")
                if other and other.get("step") in UNDER_WAY and not partly:
                    check("mirror_busy", False, M("{name} is converting {library} to a child dataset", name=name, library=other.get("title") or other.get("folder")),
                          M("One conversion at a time on each server: {name}’s turn here can’t start until that one is done.", name=name), level="warn")
            else:
                check("scope", True, M("This server only: {name} keeps its folder as it is", name=name),
                      M("Or convert both servers: the files are the same on both, so the record size that suits them is too."), action="scope")
            if there and there.get("state") == "dataset" and there.get("rs") and there["rs"] not in (st.get("rs"), st.get("alt")):
                check("mirror_rs", False, M("{name} has it as a child dataset with record size {size}", name=name, size=_rs(there["rs"])),
                      M("Use the same record size here, so both sides read alike."), level="warn")
        if step in ("create", "copy"):
            if partly:
                check("mirror_partial", False, M("A conversion of {library} left partway on {name}", library=title, name=name), partial_text, level="warn")
        elif step == "switch" and not st.get("old_path"):
            check("mirror_partial", not partly, M("No conversion of {library} left partway on {name}", library=title, name=name),
                  partial_text if partly else "", level="error" if partly else "", gate="aside")
            holding = lib.get("holding")
            if f and f.get("via") == "link" and holding is not None:
                check("mirror_holding", bool(holding), M("{name} holds its file jobs on {library}", name=name, library=title),
                      "" if holding else M("It looks every 5 minutes, and holds optimizing, the file tools and Sync Libraries there once it sees this side "
                                           "switching (since the copy finished). This is checked again by itself."), gate="aside")
            else:
                check("mirror_holding", False, M("{name} holds its file jobs on {library}", name=name, library=title),
                      M("Can’t be confirmed: the two Plex Caches aren’t talking (System → Mirror Plex Cache). Don’t run its file tools or the optimizer "
                        "on {library} there until this one is done.", library=title), level="warn")
        elif step == "verify":
            self._check_mirror_files(st, check, f, name, lib, force)

    def _check_truenas(self, f: dict | None, name, check) -> None:
        """The other side runs TrueNAS — said for certain (its API, its kernel, its version file), not guessed from ZFS."""
        links = self.app.peerlinks
        title = M("{name} runs TrueNAS", name=name)
        if f is None:
            check("mirror_truenas", False, title,
                  M("Can’t tell what it runs on: System → Mirror Plex Cache says there’s no Plex Cache at the mirror’s site.") if not links.wanted() else
                  M("Can’t tell what it runs on: pair the two Plex Caches (System → Mirror Plex Cache), or let the folder sync carry .plexcache, then check again."))
            return
        p = f.get("platform")
        if not isinstance(p, dict):
            check("mirror_truenas", False, title, M("{name} runs an older Plex Cache, which doesn’t say what it runs on: update it, then check again.", name=name))
        elif p.get("truenas") is True:
            check("mirror_truenas", True, M("{name} runs TrueNAS {version}", name=name, version=p.get("version") or ""), "")
        else:
            check("mirror_truenas", False, title,
                  M("It says: {what}. A child dataset needs TrueNAS on both servers.", what=p.get("label") or p.get("name") or "?") if p.get("truenas") is False else
                  M("TrueNAS isn’t detected there for certain ({what}): connect its TrueNAS API (System → TrueNAS there), then check again.", what=p.get("label") or "?"))

    def _check_mirror_files(self, st: dict, check, f: dict | None, name, lib: dict, force: bool) -> None:
        """Once switched: the files here, the same as the other site's (through the link), a file changed in the last hour
        left out — it may still be syncing. Compared at most every FILES_EVERY, or on Check Again."""
        title = M("The same files as on {name}", name=name)
        if not f or f.get("via") != "link" or not lib.get("key"):
            check("mirror_files", False, title, M("Not compared: the two Plex Caches aren’t talking (System → Mirror Plex Cache). Compare the folder on both "
                                                  "sides by hand, or pair them and check again."), level="warn")
            return
        cached = getattr(self, "_mf", None)
        if force or not cached or cached[0] != st["folder"] or cached[2].get("pending") or time.time() - cached[1] > FILES_EVERY:
            try:
                got = self._compare_with_mirror(st, lib)
            except Exception as e:  # noqa: BLE001 — the link went quiet: said, compared next time
                got = {"error": str(e)}
            cached = self._mf = (st["folder"], time.time(), got)
        got = cached[2]
        if got.get("pending"):
            check("mirror_files", False, title, M("Reading the files on both sides…"))
        elif got.get("error"):
            check("mirror_files", False, title, M("Not compared: {why}", why=got["error"]), level="warn")
        else:
            bad = got["missing"] + got["different"] + got["extra"]
            check("mirror_files", not bad, title,
                  M("{files} files, the same as there.", files=got["files"]) if not bad else
                  M("Different: {list}. Let the folder sync finish (resume it here), then check again.", list=", ".join(bad[:5])))

    def _compare_with_mirror(self, st: dict, lib: dict) -> dict:
        from .mirrorhealth import _norm
        key, folder = str(st["library"]), st["folder"].rstrip("/")
        d = self.app.libdata.data(key)
        if not d.ready:
            self.app.libdata.fast_track(key)
            return {"pending": True}
        link = self.app.peerlinks.for_library(key)
        if link is None:
            return {"error": str(M("the two Plex Caches aren’t talking"))}
        got = link.files(lib["key"])
        if not got.get("ready"):
            return {"pending": True}
        now = time.time()
        theirs = {_norm(r[0]): (int(r[1] or 0), float(r[2] or 0)) for r in got.get("files") or [] if r}
        ours = {}
        for row in list(d.files) + list(d.unlisted):
            local = row.get("local") or ""
            if not row.get("missing") and local.startswith(folder + "/"):
                ours[_norm(local[len(folder) + 1:])] = (int(row.get("size") or 0), float(row.get("mtime") or 0))
        recent = lambda v: now - v[1] < RECENT  # noqa: E731
        single = len((self.app.library_info(key) or {}).get("locations") or []) == 1 and len(lib.get("folders") or []) == 1
        return {"files": len(ours),
                "missing": sorted(r for r, v in ours.items() if r not in theirs and not recent(v))[:20],
                "different": sorted(r for r, v in ours.items() if r in theirs and v[0] != theirs[r][0] and not (recent(v) or recent(theirs[r])))[:20],
                "extra": sorted(r for r, v in theirs.items() if single and r not in ours and not recent(v))[:20]}

    # ------------------------------------------------------------ what the other side is told
    def public(self) -> dict | None:
        """The conversion under way here, for the other side (the link's status, our marker): None when there's none."""
        st = self.state
        if not st or st.get("step") not in UNDER_WAY:
            return None
        return {"library": str(st.get("library") or ""), "title": st.get("title") or "", "folder": os.path.basename(st.get("folder") or ""),
                "step": st["step"], "aside": bool(st.get("old_path")), "since": st.get("started"), "scope": st.get("scope") or "this"}

    def folders(self, key: str) -> list[dict]:
        """Each of a library's folders as it is here, for the other side: {"name", "state": "dataset" | "folder" | "converting"
        | "partial" | "other", "step", "rs", "room": {"free", "needed", "ok"}} — room for a second copy (its size and 5%,
        1 GB kept clear), where it's a folder that could become one."""
        out, st = [], self.state or {}
        for c in self.candidates():
            if c["library"] != str(key):
                continue
            row = {"name": os.path.basename(c["folder"]), "state": "dataset" if c.get("is_dataset") else "folder" if c.get("child") else "other",
                   "rs": c.get("now_rs")}
            if st.get("step") in UNDER_WAY and st.get("folder") == c["folder"]:
                row.update(state="converting", step=st["step"], aside=bool(st.get("old_path")), since=st.get("started"))
            elif c.get("partial"):
                row.update(state="partial", step=c["partial"]["step"], aside=bool(c["partial"].get("old_path")))
            if row["state"] in ("folder", "partial", "converting"):
                need = int(int(c.get("size") or 0) * MARGIN) + KEEP_CLEAR
                try:
                    vfs = os.statvfs(c["folder"] if os.path.isdir(c["folder"]) else os.path.dirname(c["folder"]))
                    free = vfs.f_bavail * vfs.f_frsize
                    row["room"] = {"free": free, "needed": need, "ok": free >= need}
                except OSError:
                    pass
            out.append(row)
        return out

    def converting_info(self, key: str) -> dict | None:
        """A conversion of one of the library's folders under way here, or left partway on disk: {"step", "aside", "folder",
        "since", "title"} — what the other side warns of, and holds its file jobs for."""
        for row in self.folders(key):
            if row["state"] in ("converting", "partial"):
                return {"step": row.get("step"), "aside": bool(row.get("aside")), "folder": row["name"], "since": row.get("since"),
                        "title": (self.app.library_info(key) or {}).get("title") or row["name"]}
        return None

    def mirror_view(self) -> dict:
        """For the page: whether a mirror is watched, and how its Plex Cache is heard from — its light ("off": no mirror,
        only this server; "ok": talking directly, over the link; "warn": only its marker, carried by the folder sync, or
        not heard from at all), what that means for the conversion; with a mirror, its name and page, and each candidate
        folder as it is there."""
        libs = [(self.state or {}).get("library")] if (self.state or {}).get("step") in UNDER_WAY else \
            list(dict.fromkeys(c["library"] for c in self.candidates()))
        if not any(self._mirror_watched(k) for k in libs):
            seen = next(((k, m) for k, m in ((k, self._mirror_seen(k)) for k in libs) if m), None)
            if seen:
                key, m = seen
                return {"watched": False, "status": "warn", "title": M("Mirror: {name}, not chosen yet", name=m.get("name") or "?"),
                        "detail": self._seen_text(m), "name": m.get("name") or "", "how": m.get("how"), "id": m.get("id") or "",
                        "library": str(key), "their_key": m.get("their_key") or ""}
            undecided = [k for k in libs if k and self.app.mirrors.state_of(str(k)) == "unset"]
            return {"watched": False, "status": "off", "title": M("No mirror"),
                    "detail": M("No library here is marked as mirrored ([Mirror page](/mirrors)), so the conversion is this server’s alone. "
                                "Anything else that syncs the folder is still looked at: Syncthing has to leave the new dataset and the "
                                "set-aside folder out.") if not undecided else
                    M("Whether this library is mirrored isn’t chosen yet ([Mirror page](/mirrors)): until it is, the conversion is this server’s alone. "
                      "Anything else that syncs the folder is still looked at: Syncthing has to leave the new dataset and the set-aside folder out."),
                    "library": str(undecided[0]) if undecided else ""}
        key = next((str(k) for k in libs if k and self._mirror_watched(k)), "")
        f = self._mirror(library=key)
        link = self.app.peerlinks.for_library(key)
        url = ((link.paired or {}).get("url") or "") if link is not None and link.paired else ""
        name, via = self._mirror_name(f, key), (f or {}).get("via")
        how = M("This server goes first, then {name} (or this one only: chosen before the copy). Before the copy, {name} must run "
                "TrueNAS, with room for its own copy, and nothing left partway there. While this one switches, the folder sync is "
                "paused here and {name} holds its file jobs on the library; after, the files here are compared with {name}’s.", name=name)
        if via == "link":
            status, title = "ok", M("Mirror: {name}, talking directly", name=name)
            detail = how
        elif via == "marker":
            status, title = "warn", M("Mirror: {name}, through its marker only", name=name)
            detail = M("The two Plex Caches aren’t talking directly ([System → Mirror Plex Cache](/system#peer)), so what {name} says comes "
                       "through the folder sync, as old as it makes it: that it holds its file jobs can’t be confirmed, and the files "
                       "aren’t compared after the switch. {how}", name=name, how=how)
        else:
            status, title = "warn", M("Mirror: {name}, not heard from", name=name)
            detail = M("Neither the link ([System → Mirror Plex Cache](/system#peer)) nor its marker in the shared folder says anything "
                       "of {name} yet, so it can’t be checked: pair the two, or let the folder sync carry .plexcache. {how}", name=name, how=how)
        out = {"watched": True, "status": status, "title": title, "detail": detail, "name": name, "via": via,
               "url": url.rstrip("/") + "/dataset" if url else "", "folders": {}}
        libs = (f or {}).get("libraries") or {}
        for c in self.candidates():
            lib = libs.get(c["library"])
            if lib is not None:
                out["folders"][c["folder"]] = their_folder(lib.get("folders"), os.path.basename(c["folder"]))
        return out

    # ------------------------------------------------------------ actions
    def start_copy(self, mirror: bool = False) -> dict:
        st = self.state
        if not st or st.get("step") not in ("create", "switch"):
            raise ValueError("not ready to copy")
        if st["step"] == "create":
            self.refresh()
            if self.blocking():
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
        self.refresh()
        stop = self.blocking("aside")
        if stop:
            raise ValueError(str(stop[0]["title"]))
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
        # the folder sync paused, the other side holding, no conversion of it left partway there: looked at again now
        # (it may have changed during the last pass)
        self._check_gate()
        stop = self.blocking("aside")
        if stop:
            raise ValueError(str(stop[0]["title"]))
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
        if st.get("step") != "verify" or self.blocking():
            raise ValueError("verify first: every check must pass")
        if st.get("old_path"):  # (gone already: deleted by hand)
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


def step_name(step):
    return {"create": M("Create"), "copy": M("Copy"), "switch": M("Switch"), "verify": M("Verify")}.get(step, step or "")


def _old_path(folder: str) -> str:
    """Where the old folder goes until it's deleted: .<name>.plexcache-old, beside it."""
    folder = folder.rstrip("/")
    return os.path.join(os.path.dirname(folder), "." + os.path.basename(folder) + ".plexcache-old")


def sync_root(folder: str) -> str | None:
    """The root of the Syncthing folder ``folder`` is in: the nearest of it and the folders above it that holds Syncthing's
    marker (.stfolder). None: no Syncthing folder over it."""
    p = folder.rstrip("/")
    while p and p != "/":
        if os.path.lexists(os.path.join(p, SYNC_MARK)):
            return p
        p = os.path.dirname(p)
    return None


def ignored(root: str, rel: str) -> bool:
    """Whether the ignore patterns of the Syncthing folder at ``root`` (its .stignore) leave out ``rel`` (a path within it),
    as Syncthing reads them: the first pattern that matches decides; "/a" anchored at the root, "a" at any level; *, ? and
    ** wildcards; (?d) and (?i) prefixes; "!" lets a path back in; a folder left out leaves out what's in it. #include
    lines aren't followed (their patterns don't count here)."""
    try:
        with open(os.path.join(root, SYNC_IGNORE), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    rel = rel.strip("/")
    for line in lines:
        pat = line.strip()
        if not pat or pat.startswith("//") or pat.startswith("#"):
            continue
        neg = pat.startswith("!")
        pat = pat[1:] if neg else pat
        fold = False
        while pat[:4] in ("(?d)", "(?i)"):
            fold = fold or pat[:4] == "(?i)"
            pat = pat[4:]
        pat = pat.rstrip("/")
        if not pat:
            continue
        body = re.escape(pat.lstrip("/")).replace(r"\*\*", "\0").replace(r"\*", "[^/]*").replace(r"\?", "[^/]").replace("\0", ".*")
        rx = ("^" if pat.startswith("/") else "^(?:.*/)?") + body + "(?:/.*)?$"
        if re.match(rx, rel, re.I if fold else 0):
            return not neg
    return False


def their_folder(rows, name: str) -> dict | None:
    """The other side's folder that is this one: the one of the same name, else the library's only folder there."""
    rows = [r for r in rows or [] if isinstance(r, dict)]
    return next((r for r in rows if r.get("name") == name), None) or (rows[0] if len(rows) == 1 else None)


def _gb(n) -> str:
    return f"{(n or 0) / 1e9:,.1f} GB"


def _rs(n) -> str:
    if not n:
        return "?"
    return f"{n // (1024 * 1024)}M" if n >= 1024 * 1024 else f"{n // 1024}K"

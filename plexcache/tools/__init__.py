"""The tools (the Tools pages): each scans a library for what it could do, shows the plan, and does
the rows you tick — in the background, one row at a time, with the result of each kept. Nothing
happens until you press Apply. A tool knows about the mirrored server (plex2.py) where that matters:
what the same step does there, or what to wait for there.

Files are only ever touched through the optimizer service (optimize/files.py): the one part of
Plex Cache that may write to the media. Plex is asked through its API, here and on the mirror.

A tool's state (the last scan, the results, what it's still waiting for) is kept in tools-<name>.json.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error

from ..i18n import M

KEEP_RESULTS = 500
MAX_ROWS = 400


class Tool:
    name = ""
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

    def save(self) -> None:
        with self.lock:
            data = {"scan": self.scan_result, "results": self.results[-KEEP_RESULTS:], "extra": self.extra}
        self.app._save_json(f"tools-{self.name}.json", data)

    # ------------------------------------------------------------ to be given by each tool
    def scan(self, library: str, options: dict | None = None) -> dict:
        """What the tool could do in a library: {"library", "rows": [{"id", "title", ...}], ...}."""
        raise NotImplementedError

    def apply_row(self, row: dict, log) -> dict:
        """One row done. {"result": "done" | "failed", "reason"?, ...}; ``log(text)`` keeps a step."""
        raise NotImplementedError

    def tick(self) -> None:
        """Follow-ups checked now and then (the scheduler): what's waited for on the mirror."""

    def waiting(self) -> bool:
        """Whether tick() has anything to look at."""
        return False

    # ------------------------------------------------------------ running
    def scan_async(self, library: str, options: dict | None = None) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.error = True, None
            self.progress = {"what": "scan", "library": library}

        def work():
            try:
                got = self.scan(library, options or {})
                got["at"] = time.time()
                got["v"] = self.scan_version
                got["rows"] = got.get("rows") or []
                with self.lock:
                    self.scan_result = got
            except Exception as e:  # noqa: BLE001 — said on the page
                self.error = str(e)
            finally:
                self.save()
                with self.lock:
                    self.running, self.progress = False, None
        threading.Thread(target=work, name=f"tool-{self.name}-scan", daemon=True).start()
        return True

    def apply_async(self, ids: list[str], choices: dict | None = None) -> bool:
        """The rows named done, each with your choices for it (``choices``: {row id: {...}}, merged into the row)."""
        from .. import peers
        why = peers.tool_conflict(self.app, str((self.scan_result or {}).get("library") or ""), self.name)
        if why:
            raise ValueError(why)
        with self.lock:
            if self.running or not self.scan_result:
                return False
            want = set(ids)
            rows = [dict(r, **((choices or {}).get(r["id"]) or {})) for r in self.scan_result.get("rows") or [] if r["id"] in want]
            if not rows:
                return False
            self.running, self.error = True, None
            self.progress = {"what": "apply", "library": self.scan_result.get("library"), "done": 0, "total": len(rows), "row": None, "step": None}

        def work():
            try:
                for i, row in enumerate(rows):
                    steps: list[str] = []

                    def log(text, _steps=steps):
                        _steps.append(text)
                        with self.lock:
                            self.progress["step"] = text
                    with self.lock:
                        self.progress.update(done=i, row=row.get("title"), step=None)
                    try:
                        res = self.apply_row(row, log)
                    except Exception as e:  # noqa: BLE001 — one row's failure, listed; the rest go on
                        res = {"result": "failed", "reason": e.args[0] if e.args and isinstance(e.args[0], M) else str(e)}
                    with self.lock:
                        self.results.append(dict(res, id=row["id"], title=row.get("title"), at=time.time(), steps=steps))
                        if res.get("result") == "done" and self.scan_result:
                            self.scan_result["rows"] = [r for r in self.scan_result["rows"] if r["id"] != row["id"]]
                    self.save()
            finally:
                self.save()
                with self.lock:
                    self.running, self.progress = False, None
                if self.name in peers.FILE_TOOLS and hasattr(self.app, "note_tool_run"):
                    self.app.note_tool_run(str(self.scan_result.get("library") if self.scan_result else ""), self.name)
                self.app.run_checks_async()
        threading.Thread(target=work, name=f"tool-{self.name}-apply", daemon=True).start()
        return True

    def snapshot(self) -> dict:
        with self.lock:
            return {"name": self.name, "running": self.running, "progress": dict(self.progress) if self.progress else None, "error": self.error,
                    "scan": self.scan_result, "results": list(reversed(self.results[-50:])), "extra": json.loads(json.dumps(self.extra))}

    # ------------------------------------------------------------ what the tools share
    def mirror(self, library: str):
        """The mirrored server's library paired with ``library``, or None (no mirror, or not paired)."""
        other = getattr(self.app, "other", None)
        if other is None or not other.configured() or self.app.sync.decided() != "plex2":
            return None
        return other.pairs().get(str(library))

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

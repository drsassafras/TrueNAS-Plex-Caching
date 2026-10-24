"""Convert to MKV: video files in other containers (AVI, MPEG-TS, VOB…) converted to MKV — only when
nothing in them would be lost — as a process like any tool's: the backup first, then each file.

Why MKV: it's the container the optimizer can lay out for caching (the index and metadata after
the video, so the start of a file is all content), and it keeps every stream, chapter and subtitle
the old containers hold. Which kinds convert is the tool's options, per library (one switch per
kind): the scan lists every file in another container the library has, whatever the switches say
— a kind switched off shows grey — and says of a file left as it is last time why (what MKV
wouldn't keep: the optimizer's record), so you can untick it.

Each file is converted by the optimizer service (optimize/convert.py): inspected first, the MKV
written next to the original and checked against it, then optimized. The original stays until
Plex has the MKV as a version of the same item — on the mirrored server too, where one is watched
(converting.py sees it in, and deletes the original then): Plex keeps the item's edits, watch
history and collections. Revert, while the run's backup lasts: the MKV removed and, where the
original has gone already, put back from the snapshot.
"""

from __future__ import annotations

import os

from ..i18n import M
from ..optimize import convert
from ..paging import sort_key
from . import MAX_ROWS, Tool

KIND_NAMES = {"avi": "AVI", "ts": "MPEG-TS", "ps": "MPEG-PS", "flv": "FLV", "ogm": "OGM", "rm": "RealMedia", "3gp": "3GP", "mp4": "MP4", "mov": "MOV"}
# the kinds as the options name them (the page's ⓘ for each is in app.js, CONVERT_TYPES)
OPTION_NAMES = {"avi": M("AVI"), "ts": M("MPEG-TS / M2TS"), "ps": M("MPEG-PS (VOB, MPG)"), "flv": M("FLV"), "ogm": M("OGM / OGV"), "rm": M("RealMedia (RM, RMVB)"),
                "3gp": M("3GP"), "mp4": M("MP4 / M4V"), "mov": M("MOV (QuickTime)")}


def kind_name(kind: str | None) -> str:
    return KIND_NAMES.get(kind or "", (kind or "?").upper())


class ConvertToMkv(Tool):
    name = "convert"
    title = M("Convert to MKV")
    page = "/convert"
    applies = ("movie", "tv")
    options = [{"id": k, "name": OPTION_NAMES[k], "help": M("Files of this kind are converted."), "kinds": ("movie", "tv"), "default": convert.DEFAULT_TYPES[k]}
               for k in convert.TYPES]
    flag_help = M("A video file in another container than MKV (AVI, MPEG-TS, VOB…). Convert to MKV writes an MKV next to it — only when nothing in the file would be lost — sees it into Plex as a version of the same item, then removes the original; the item keeps its edits and watch history.")

    def flag_lines(self, rows):
        return [M("{name}: left as it is — {why}", name=r.get("name") or "", why=r["skipped"]) if r.get("skipped")
                else M("{name}: {kind}, converts to MKV", name=r.get("name") or "", kind=kind_name(r.get("kind"))) for r in rows]

    def scan(self, data) -> dict:
        library = data.key
        lib = self.app.library_info(library)
        if not lib:
            raise ValueError(M("No such library."))
        op = self.app.optimizing
        have = {f["local"] for f in data.files + data.unlisted if not f.get("missing")}
        rows, waiting = [], 0
        files = [f for f in data.files if not f.get("missing")]
        for i, f in enumerate(files):
            if i % 50 == 0:
                self.step(i, len(files))
            kind = convert.type_of(f["path"])
            if not kind:
                continue
            if convert.target_of(f["local"]) in have:  # converted already: its MKV is next to it, on its way into Plex
                waiting += 1
                continue
            skipped = op.convert_skipped(f["local"], data.stat_result(f))
            rows.append({"id": "conv:" + f["path"], "library": str(library), "rk": f.get("rk") or "", "title": f.get("title") or os.path.basename(f["path"]),
                         "poster": f.get("poster") or "", "path": f["path"], "local": f["local"], "name": os.path.basename(f["path"]), "kind": kind,
                         "issues": [kind], "size": int(f.get("size") or 0), "skipped": skipped})
        rows.sort(key=lambda r: (bool(r["skipped"]), sort_key(r["title"])))
        return {"library": str(library), "rows": rows[:MAX_ROWS], "total": len(rows), "waiting": waiting,
                "skipped": sum(1 for r in rows if r["skipped"]), "mirror": self.mirrored(library)}

    history = [("what", M("What was done")), ("written", M("Written")), ("original", M("Original"))]

    def history_row(self, row: dict, outcome: dict) -> dict:
        return {"what": M("{kind} → MKV", kind=kind_name(row.get("kind"))), "written": int(outcome.get("bytes") or 0),
                "original": M("Stays until Plex has the MKV")}

    def needed_bytes(self, rows: list[dict]) -> int:
        return sum(int(r.get("size") or 0) for r in rows)  # (an MKV about the original's size, written beside it)

    def retained_bytes(self, rows: list[dict]) -> int:
        return sum(int(r.get("size") or 0) for r in rows)  # (the originals, once they go: the snapshot holds them)

    def apply_row(self, row: dict, log) -> dict:
        r = self.app.optimizing.convert_one(row["local"], row["library"], row.get("poster") or "", log)
        res = r.get("result")
        if res == "converted" and r.get("new"):
            new = r["new"]
            log(M("{file} written and checked against the original: every track with the same packets", file=os.path.basename(new)))
            self.app.conversions.add(row["library"], row["path"], row["local"], new, str(row.get("rk") or ""), row.get("title") or row["name"])
            log(M("Plex asked to scan the folder: the original goes once Plex has the MKV as a version of the same item"))
            self.app.activity.event("tools", M("{file} converted to MKV ({kind}); the original stays until Plex has the MKV", file=row["name"], kind=kind_name(row.get("kind"))))
            return {"result": "done", "new": new, "bytes": int(r.get("after") or 0)}
        if res == "skipped":
            return {"result": "failed", "reason": M("Left as it is: {why}", why=r.get("reason") or M("something in it wouldn’t be kept"))}
        if res == "cancelled":
            return {"result": "failed", "reason": M("Cancelled before it started")}
        return {"result": "failed", "reason": r.get("reason") or M("the optimizer service didn’t convert it")}

    def changed_paths(self, row: dict, outcome: dict) -> list[str]:
        return [row["local"]]  # (the original: put back from the snapshot where it has gone already)

    def revert_row(self, row: dict, outcome: dict, backup: dict, log) -> None:
        """The MKV removed; the original, where converting.py has deleted it already, put back from the snapshot first."""
        local, new = row["local"], outcome.get("new") or convert.target_of(row["local"])
        self.app.conversions.forget(local)
        if not os.path.exists(local):
            super().revert_row(row, outcome, backup, log)
        self.file_action("remove_converted", new, to=local)
        log(M("{file} removed: the original stays", file=os.path.basename(new)))
        try:
            self.app.plex(timeout=30).scan(row["library"], os.path.dirname(row["path"]))
        except Exception:  # noqa: BLE001 — Plex finds out at its next scan
            pass

    def snapshot(self) -> dict:
        out = super().snapshot()
        out["conversions"] = self.app.conversions.snapshot()["items"]  # (the files on their way into Plex, and the originals gone)
        return out

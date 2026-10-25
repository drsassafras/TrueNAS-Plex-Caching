"""Before Plex Cache changes Plex's own records — a merge, an edit, a match, a poster chosen, here or on the mirrored
server — a backup of each record it's about to change, as Plex has it then: its fields and which are locked, its tags
(genres, collections, labels…), its match, its edition, the poster and background chosen, and its files. Revert puts the
records back from it: a merge split apart again, and each record it folded given its own metadata back.

It's the same Backup preference as before files change (System → Preferences), and the same days: under "A snapshot" or
"The copies the action keeps" the app keeps this copy of the records itself (Plex's own database isn't on a dataset the
app knows, so there's no snapshot of it to take); under "A backup I make myself" none is made. Kept general.backup_days,
then deleted by the app.

Every change to Plex goes through PlexClient, which asks guard() first (``before_change``, set by App.plex() and
OtherServer.client()). A process opens a backup of its own (session(), with the records it will change taken first, so
one that can't be read stops it before anything changes); a change made outside one gets a backup of its own all the same.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid

from .i18n import M, jsonable
from .plex import EDIT_FIELDS, TAG_FIELDS, PlexError

STORE = "plex-backups.json"
KEEP = 300               # backups kept at most (the oldest go first, whatever their days)
LOOSE = 60               # a change outside a process within this many seconds of the last one joins its backup
FIND_WAIT, FIND_POLL = 30, 3  # after a split: how long Plex gets to list a record's files on a record of its own
TYPE_NO = {"movie": 1, "show": 2, "season": 3, "episode": 4, "artist": 8, "album": 9, "track": 10}
IMAGES = (("thumb", "posters", "poster"), ("art", "arts", "art"))  # (field, the list of them, what set_image takes)

_tl = threading.local()  # this thread's open backup per server ("here", "mirror"), and whether a revert is under way
_lock = threading.Lock()


def plan(app) -> dict:
    """What's done before Plex's records change, per the Backup preference: {"kind": "records" | "manual", "status": "ok" |
    "off", "label": "backup" | "none", "days", "text"} — ``label``: what the Apply button says."""
    from . import safety
    days = safety.life(app) // 86400
    if safety.backup_kind(app) == "manual":
        return {"kind": "manual", "status": "off", "label": "none", "days": days,
                "text": M("[You back up yourself](/system#prefs), as chosen: Plex’s records change without a backup.")}
    return {"kind": "records", "status": "ok", "label": "backup", "days": days,
            "text": M("[A backup of the Plex records is made first](/system#prefs): each record this changes, as Plex has it now, kept {days} days. Revert puts them back, from the [Processes page](/processes).", days=days)}


def backups(app) -> list[dict]:
    if getattr(app, "_plex_backups", None) is None:
        app._plex_backups = [b for b in ((app._load_json(STORE) or {}).get("backups") or []) if isinstance(b, dict)]
    return app._plex_backups


def save(app) -> None:
    with _lock:
        data = {"backups": [dict(b) for b in backups(app)[-KEEP:]]}
    app._save_json(STORE, data)


def find(app, backup_id: str) -> dict | None:
    return next((b for b in backups(app) if b.get("id") == backup_id), None)


def read(client, rating_key: str) -> dict | None:
    """One record as Plex has it now — what a revert puts back: {"key", "type", "section", "label", "guid", "matched",
    "values": every field EDIT_FIELDS names, "locked", "tags": every tag field, "edition", "images": the poster and
    background chosen, "files"}; None if Plex has no such record."""
    try:
        root = client.get(f"/library/metadata/{rating_key}", {"includeGuids": 1})
    except PlexError as e:
        if "404" in str(e):
            return None
        raise
    item = next(iter(root), None)
    if item is None:
        return None
    from .plex import item_summary
    s = item_summary(item)
    rec = {"key": str(rating_key), "type": s["type"], "section": item.get("librarySectionID", ""), "label": s["label"], "guid": s["guid"], "matched": s["matched"],
           "values": {f: item.get(f, "") for f in EDIT_FIELDS}, "locked": list(s["locked"]) + list(s["art_locked"]),
           "tags": {f: [t.get("tag", "") for t in item.findall(tag) if t.get("tag")] for f, tag in TAG_FIELDS.items()},
           "edition": s["edition"], "images": {}, "files": [p for p, _ in s["files"]]}
    if rec["type"] == "show":  # (a show's files are its episodes')
        try:
            leaves = client.get(f"/library/metadata/{rating_key}/allLeaves")
            rec["files"] = [p.get("file") for ep in leaves for m in ep.findall("Media") for p in m.findall("Part") if p.get("file")]
        except PlexError:
            pass
    for field, many, _one in IMAGES:
        try:
            rec["images"][field] = next((im["id"] for im in client.images(rating_key, many) if im["selected"]), "")
        except PlexError:
            rec["images"][field] = ""
    return rec


# ------------------------------------------------------------ taking one
def _new(app, what, server: str, library: str = "", job: str = "", loose: bool = False) -> dict:
    from . import safety
    now = time.time()
    name = (app.settings.section("plex2").get("name") or str(M("the mirror"))) if server == "mirror" else (getattr(app, "plex_name", "") or str(M("This server")))
    b = {"id": uuid.uuid4().hex[:12], "at": now, "until": now + safety.life(app), "what": jsonable(what), "server": server, "server_name": name,
         "library": str(library or ""), "job": job, "loose": loose, "items": {}, "ops": [], "reverted": False}
    with _lock:
        backups(app).append(b)
    return b


def _capture(app, b: dict, client, keys) -> None:
    """The records named, read into the backup — each only once, as it was before the first change to it."""
    for k in keys:
        k = str(k)
        if not k or k in b["items"]:
            continue
        rec = read(client, k)
        if rec is not None:
            b["items"][k] = rec
            if not b["library"] and rec.get("section"):
                b["library"] = str(rec["section"])


@contextlib.contextmanager
def session(app, what, server: str = "here", library: str = "", job: str = "", client=None, keys=()):
    """A process's backup, open while it changes Plex's records (this thread's, for ``server``): ``keys``, the records
    it will change, taken first — a record that can't be read raises here, before anything changes. Yields the backup,
    or None where you back up yourself (the preference)."""
    if plan(app)["kind"] != "records":
        yield None
        return
    b = _new(app, what, server, library, job)
    try:
        if keys:
            _capture(app, b, client or (app.other.client(timeout=120) if server == "mirror" else app.plex(timeout=120)), keys)
    except PlexError as e:
        with _lock:
            backups(app).remove(b)
        raise ValueError(M("The Plex records couldn’t be read for the backup: {why}", why=str(e))) from None
    save(app)
    opened = getattr(_tl, "open", None) or {}
    before = opened.get(server)
    _tl.open = dict(opened, **{server: b})
    try:
        yield b
    finally:
        _tl.open = dict(getattr(_tl, "open", None) or {}, **{server: before})
        if not b["items"]:  # (nothing changed after all: no backup to keep)
            with _lock:
                if b in backups(app):
                    backups(app).remove(b)
        save(app)


def guard(app, client, server: str, op: str, keys: list[str]) -> None:
    """Before a change to Plex's records (PlexClient's before_change): each record it touches backed up, in the backup the
    thread has open — or one of its own (a change outside a process) — unless you back up yourself, or this is a revert."""
    if getattr(_tl, "quiet", False) or not keys or plan(app)["kind"] != "records":
        return
    b = (getattr(_tl, "open", None) or {}).get(server)
    if b is None:
        now = time.time()
        with _lock:
            b = next((x for x in reversed(backups(app)) if x.get("loose") and x.get("server") == server and not x.get("reverted") and now - x["at"] < LOOSE), None)
        if b is None:
            b = _new(app, M("A change to Plex’s records"), server, loose=True)
    _capture(app, b, client, keys)
    if op == "merge":
        b["ops"].append({"op": "merge", "keep": str(keys[0]), "others": [str(k) for k in keys[1:]], "at": time.time()})
    if b.get("loose"):
        b["at"] = time.time()
    save(app)


# ------------------------------------------------------------ putting back
def put_back(client, section: str, rec: dict, key: str) -> list:
    """A record given back what the backup has of it (only what differs now): the match first (a match reads Plex's
    metadata afresh), then the fields and their locks, the edition, the tag fields, the poster and background. What was done."""
    cur = read(client, key)
    if cur is None:
        raise ValueError(M("Plex has no record {key} now.", key=key))
    type_no, done = TYPE_NO.get(rec.get("type") or cur.get("type") or "", 1), []
    if (rec.get("guid") or "") != (cur.get("guid") or ""):
        if rec.get("matched") and rec.get("guid"):
            client.match(key, rec["guid"], rec.get("label") or "")
        else:
            client.unmatch(key)
        done.append("match")
        cur = read(client, key) or cur
    was_locked, now_locked = set(rec.get("locked") or []), set(cur.get("locked") or [])
    values = {}
    for f in EDIT_FIELDS:
        v, lk = (rec.get("values") or {}).get(f, ""), f in was_locked
        if (v != (cur.get("values") or {}).get(f, "") or lk != (f in now_locked)) and not (f == "year" and not v):
            values[f"{f}.value"], values[f"{f}.locked"] = v, 1 if lk else 0
    if (rec.get("edition") or "") != (cur.get("edition") or ""):
        values["editionTitle.value"], values["editionTitle.locked"] = rec.get("edition") or "", 1 if rec.get("edition") else 0
    if values:
        client.edit(section, type_no, key, values)
        done.append("fields")
    for f in TAG_FIELDS:
        was, now = list((rec.get("tags") or {}).get(f) or []), list((cur.get("tags") or {}).get(f) or [])
        lock = (f in was_locked) if (f in was_locked) != (f in now_locked) else None
        if sorted(was) != sorted(now) or lock is not None:
            client.set_tags(section, type_no, key, f, [t for t in was if t not in now], remove=[t for t in now if t not in was], locked=lock)
            done.append(f)
    for field, _many, one in IMAGES:
        want = (rec.get("images") or {}).get(field) or ""
        if want and want != (cur.get("images") or {}).get(field):
            client.set_image(key, want, one)
            done.append(field)
    return done


def _holder(client, section: str, rec: dict) -> str | None:
    """The record holding a backed-up record's files now (after a split, Plex gives the parts records of their own)."""
    files = set(rec.get("files") or [])
    if not files or not section:
        return None
    deadline = time.time() + FIND_WAIT
    while True:
        leaf = TYPE_NO.get("episode" if rec.get("type") == "show" else rec.get("type") or "", 1)
        try:
            for it in client.library_items(section, leaf):
                if files & {p for p, _ in it.get("files") or []}:
                    return str(it.get("grandparent") if rec.get("type") == "show" else it.get("key"))
        except PlexError:
            pass
        if time.time() >= deadline:
            return None
        time.sleep(FIND_POLL)


def revert(app, backup_id: str, keys=None) -> dict:
    """The records put back from a backup while it lasts — all of them, or (``keys``) the ones a change to those records
    touched: each merge split apart first (Plex's Split Apart), then every record given its own metadata back, wherever its
    files are now. {"restored": n, "failed": n, "left": [why…]}."""
    b = find(app, backup_id)
    if b is None:
        raise ValueError(M("No such backup."))
    if b.get("reverted"):
        raise ValueError(M("Reverted already."))
    if b.get("until") and b["until"] < time.time():
        raise ValueError(M("The backup has expired: nothing to put back from."))
    want = {str(k) for k in keys} if keys else set(b["items"])
    merges = [op for op in b.get("ops") or [] if op.get("op") == "merge" and want & {op["keep"], *op["others"]}]
    for op in merges:
        want |= {op["keep"], *op["others"]}
    client = app.other.client(timeout=120) if b.get("server") == "mirror" else app.plex(timeout=120)
    ok = failed = 0
    left: list = []
    _tl.quiet = True
    try:
        for op in reversed(merges):
            try:
                client.split(op["keep"])
            except PlexError as e:
                left.append(M("{title} wasn’t split apart: {why}", title=(b["items"].get(op["keep"]) or {}).get("label") or op["keep"], why=str(e)))
        for k in sorted(want):
            rec = b["items"].get(k)
            if rec is None:
                continue
            section = str(rec.get("section") or b.get("library") or "")
            try:
                now = read(client, k)
                where = k if now is not None and (not merges or not rec.get("files") or set(rec["files"]) & set(now.get("files") or [])) else _holder(client, section, rec)
                if not where:
                    raise ValueError(M("{title}: no record holds its files now", title=rec.get("label") or k))
                put_back(client, section, rec, where)
                ok += 1
            except (PlexError, ValueError) as e:
                failed += 1
                left.append(e.args[0] if e.args and isinstance(e.args[0], M) else M("{title}: {why}", title=rec.get("label") or k, why=str(e)))
    finally:
        _tl.quiet = False
    if not keys or want >= set(b["items"]):
        b.update(reverted=True, reverted_at=time.time(), result={"restored": ok, "failed": failed}, left=[jsonable(x) for x in left])
    else:
        b.setdefault("reverted_keys", []).extend(sorted(want))
    save(app)
    app.activity.event("tools", M("Plex records put back from the backup on {server}: {ok} put back, {failed} not", server=b.get("server_name") or "", ok=ok, failed=failed),
                       level="warn" if failed else "info")
    return {"restored": ok, "failed": failed, "left": left}


# ------------------------------------------------------------ keeping them
def prune(app) -> int:
    """The backups whose days are up, deleted. How many went."""
    now = time.time()
    with _lock:
        old = [b for b in backups(app) if b.get("until") and b["until"] < now]
        if not old:
            return 0
        app._plex_backups = [b for b in backups(app) if b not in old]
    save(app)
    return len(old)


def listing(app, limit: int = 50) -> list[dict]:
    """The backups, newest first, for the Processes page: each without its records (how many, and their names)."""
    now = time.time()
    out = []
    for b in reversed(backups(app)[-limit:]):
        out.append({k: b.get(k) for k in ("id", "at", "until", "what", "server", "server_name", "library", "job", "reverted", "reverted_at", "result", "left")}
                   | {"records": len(b.get("items") or {}), "titles": [r.get("label") or k for k, r in list((b.get("items") or {}).items())[:6]],
                      "merges": len([op for op in b.get("ops") or [] if op.get("op") == "merge"]),
                      "revertable": not b.get("reverted") and bool(b.get("items")) and (not b.get("until") or b["until"] > now)})
    return out

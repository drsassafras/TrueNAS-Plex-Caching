"""A second Plex server with the same files — another site's, kept in step by syncing the folders.

Converting a file (converting.py) deletes its original only once Plex has the MKV as a version of the
same item; with a second server, on both. This is how the second one is reached and read:

* Chosen from the servers this server's Plex account can use (plex.tv's list) — or, signed in apart,
  another account's; its addresses are tried, direct ones
  first, and the first that answers as that server is kept, with that server's own token.
* Each library here has a state of its own (settings plex2.libraries, the Mirror page's Mirrored Libraries):
  not decided (the default: nothing is assumed, and what would act on a mirror waits), no mirror, or mirrored
  — by a library there, named by its key, or the one of the same name and kind when none is named. "*" stands
  for every library not listed. What every reader goes by (state_of, mirror_for, pairs).
* A file's path there: its place within its library folder here, under each of that library's
  folders there — the one Plex there has is it. Its files are read (paged, as here) and kept a few
  minutes, with how many of this library's files were found there: the paths only count as lining
  up when nearly all are (MATCHED). Otherwise nothing is deleted on its word.
"""

import os
import threading
import time

from . import plexbackup
from .i18n import M
from .plex import Library, PlexClient, PlexError, reachable, servers

RECONNECT_EVERY = 300  # seconds between tries at reaching the server by another of its addresses

INDEX_TTL = 300  # seconds the other server's file list is kept
LIBS_TTL = 600
RETRY_LIBS = 30  # seconds: its libraries read again this soon after they couldn't be (none known yet)
MATCHED = 0.9  # the share of a library's files found there for its paths to count as lining up


class OtherServer:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.libs: list[Library] | None = None
        self.libs_at = 0.0
        self.indexes: dict[str, tuple[float, dict]] = {}  # their library key -> (when, {path: rating key})
        self.rates: dict[str, float] = {}  # our library key -> share of its files found there
        self.error: str | None = None
        self.found: list[dict] = []  # the account's servers, as last listed (with their tokens: never shown)

    def settings(self) -> dict:
        return self.app.settings.section("plex2")

    def configured(self) -> bool:
        s = self.settings()
        return bool(s.get("url") and s.get("token"))

    # ------------------------------------------------------------ each library's state
    def entry(self, key: str) -> dict:
        """The library's setting: {"state": "unset" | "none" | "mirror", "key": its library there ("" : by name)}."""
        libs = self.settings().get("libraries") or {}
        e = libs.get(str(key)) if str(key) in libs else libs.get("*")
        if not isinstance(e, dict):
            return {"state": "unset", "key": ""}
        return {"state": e.get("state") if e.get("state") in ("unset", "none", "mirror") else "unset", "key": str(e.get("key") or "")}

    def state_of(self, key: str) -> str:
        """"unset" (not decided), "none" (no mirror), or "mirror"."""
        return self.entry(key)["state"]

    def any_mirror(self) -> bool:
        """Whether some library is mirrored (by the server watched): what the Plex Cache link and the mirror's checks need.
        (Every library not listed, "*", counts before this Plex's libraries are read.)"""
        if not self.configured():
            return False
        libs = self.settings().get("libraries") or {}
        return any(isinstance(e, dict) and e.get("state") == "mirror" for e in libs.values())

    def undecided(self) -> list[str]:
        """The libraries whose state isn't chosen yet."""
        return [str(lib["key"]) for lib in getattr(self.app, "libraries", []) or [] if lib.get("kind") and self.state_of(str(lib["key"])) == "unset"]

    def set_state(self, key: str, state: str, their_key: str = "") -> None:
        """A library's state chosen: "unset", "none", or "mirror" by ``their_key`` (""  : the library of the same name)."""
        if state not in ("unset", "none", "mirror"):
            raise ValueError(M("unknown state"))
        libs = dict(self.settings().get("libraries") or {})
        if state == "unset" and "*" not in libs:
            libs.pop(str(key), None)
        else:
            libs[str(key)] = {"state": state, "key": str(their_key or "") if state == "mirror" else ""}
        self.app.settings.update({"plex2": {"libraries": libs}})

    def states(self) -> dict:
        """Every library's state, for the page: {key: {"state", "key", "there": the library there (title) or ""}}."""
        theirs = {t.key: t for t in (self.libs or [])} if self.configured() else {}
        out = {}
        for lib in getattr(self.app, "libraries", []) or []:
            if not lib.get("kind"):
                continue
            key = str(lib["key"])
            e = self.entry(key)
            there = self.mirror_for(key, list(theirs.values())) if e["state"] == "mirror" and theirs else None
            out[key] = {"state": e["state"], "key": e["key"], "there": there.title if there else "", "there_key": there.key if there else ""}
        return out

    def mirror_for(self, key: str, theirs: list[Library] | None = None):
        """The library there that mirrors library ``key``, or None: not mirrored (not decided, or none), the server not
        watched, or the library named not there (nor one of the same name and kind)."""
        e = self.entry(key)
        if e["state"] != "mirror" or not self.configured():
            return None
        theirs = self.libraries() if theirs is None else theirs
        if e["key"]:
            return next((t for t in theirs if t.key == e["key"]), None)
        lib = next((x for x in getattr(self.app, "libraries", []) or [] if str(x["key"]) == str(key)), None) or {}
        return next((t for t in theirs if t.title.lower() == str(lib.get("title", "")).lower() and t.kind == lib.get("kind")), None)

    def client(self, timeout: float = 30) -> PlexClient:
        s = self.settings()
        client = PlexClient(s["url"], s["token"], timeout=timeout)
        client.before_change = lambda c, op, keys: plexbackup.guard(self.app, c, "mirror", op, keys)  # (each change to its records backed up first)
        return client

    def run(self, fn, timeout: float = 30):
        """``fn(client)`` — and, when the server doesn't answer at its address, once more at another of
        its addresses (reconnect: a public address changes; the one that lasts is preferred)."""
        try:
            return fn(self.client(timeout))
        except PlexError:
            if not self.reconnect():
                raise
            return fn(self.client(timeout))

    def reconnect(self) -> bool:
        """The server's addresses listed again from plex.tv and tried in rank order (plex.rank_connections);
        the first that answers is kept. True when one does. At most every RECONNECT_EVERY seconds."""
        s = self.settings()
        token = s.get("account_token") or self.app.settings.section("plex").get("token")
        now = time.time()
        if not s.get("machine_id") or not token or now - getattr(self, "_reconnected", 0) < RECONNECT_EVERY:
            return False
        self._reconnected = now
        try:
            found = next((x for x in servers(self.app.settings.section("general")["client_id"], token) if x["id"] == s["machine_id"]), None)
        except PlexError:
            return False
        if not found:
            return False
        url = reachable(found)
        if not url:
            return False
        if url != s.get("url") or found["token"] != s.get("token"):
            self.app.settings.update({"plex2": {"url": url, "token": found["token"]}})
            self.app.activity.event("sync", M("{name} is reached at {url} now", name=s.get("name") or found["name"], url=url))
        return True

    # ------------------------------------------------------------ choosing the server
    def list_servers(self) -> list[dict]:
        """The servers the signed-in account can use (not this one): [{"id", "name", "owned", "addresses"}]."""
        s = self.settings()
        # this server's own sign-in lists the account's other servers too: a sign-in of its own only
        # for a server another account can use
        token = s.get("account_token") or self.app.settings.section("plex").get("token")
        if not token:
            raise ValueError(M("Sign in with Plex first (System → Plex)."))
        try:
            got = servers(self.app.settings.section("general")["client_id"], token)
        except PlexError as e:
            raise ValueError(str(e)) from None
        mine = self._main_id()
        with self.lock:
            self.found = [x for x in got if x["id"] != mine]
            return [{"id": x["id"], "name": x["name"], "owned": x["owned"], "addresses": len(x["connections"])} for x in self.found]

    def _main_id(self) -> str:
        try:
            return self.app.plex(timeout=10).identity().get("machine_id", "")
        except PlexError:
            return ""

    def use(self, server_id: str) -> dict:
        """Watch this server (from list_servers()): the first of its addresses that answers is kept."""
        with self.lock:
            server = next((x for x in self.found if x["id"] == server_id), None)
        if server is None:
            raise ValueError(M("That server isn’t in the list: list them again."))
        url = reachable(server)
        if not url:
            raise ValueError(M("None of {name}’s addresses answer from here ({n} tried). If it’s at another site, it needs Remote Access on in its Plex settings, or a VPN between the sites.",
                               name=server["name"], n=len(server["connections"])))
        self.app.settings.update({"plex2": {"url": url, "token": server["token"], "name": server["name"], "machine_id": server["id"]}})
        self.refresh()
        return self.snapshot()

    def forget(self) -> None:
        self.app.settings.update({"plex2": {"url": "", "token": "", "account_token": "", "name": "", "machine_id": "", "libraries": {}}})
        with self.lock:
            self.libs, self.indexes, self.rates, self.error, self.found = None, {}, {}, None, []
            self.libs_at = 0.0

    # ------------------------------------------------------------ its libraries
    def libraries(self, fresh: bool = False) -> list[Library]:
        """Its libraries, kept LIBS_TTL. Not read yet, or never read for an error: ``libs`` stays None (not known — never
        taken for "it has none"), tried again after RETRY_LIBS."""
        if not self.configured():
            return []
        with self.lock:
            if self.libs is not None and not fresh and time.time() - self.libs_at < LIBS_TTL:
                return list(self.libs)
            if self.libs is None and not fresh and time.time() - self.libs_at < RETRY_LIBS:
                return []
        try:
            libs = [lib for lib in self.run(lambda c: c.libraries()) if lib.kind]
            err = None
        except PlexError as e:
            libs, err = self.libs, str(e)
        with self.lock:
            self.libs, self.libs_at, self.error = libs, time.time(), err
        return list(libs or [])

    def known(self) -> bool:
        """Whether its libraries have been read (pairs() says what's paired, not a guess)."""
        with self.lock:
            return self.libs is not None

    def pairs(self, theirs: list[Library] | None = None) -> dict[str, Library | None]:
        """Each library here -> the library there that mirrors it (mirror_for), or None."""
        theirs = self.libraries() if theirs is None else theirs
        return {str(lib["key"]): self.mirror_for(str(lib["key"]), theirs) for lib in getattr(self.app, "libraries", []) or []}

    def paths_there(self, key: str, plex_path: str, there: Library) -> list[str]:
        """Where a file of library ``key`` (its path as this Plex has it) may be on the other server."""
        lib = next((x for x in getattr(self.app, "libraries", []) or [] if str(x["key"]) == str(key)), None) or {}
        for loc in sorted(lib.get("locations") or [], key=len, reverse=True):
            loc = loc.rstrip("/")
            if plex_path.startswith(loc + "/"):
                rel = plex_path[len(loc) + 1:]
                return [t.rstrip("/") + "/" + rel for t in there.locations]
        return []

    def index(self, there: Library, fresh: bool = False) -> dict:
        """The other server's files of a library: {path: rating key}, kept a few minutes."""
        with self.lock:
            got = self.indexes.get(there.key)
        if got and not fresh and time.time() - got[0] < INDEX_TTL:
            return got[1]
        files = self.run(lambda c: c.library_files(there), timeout=120)
        idx = {f.path: f.rating_key for f in files}
        with self.lock:
            self.indexes[there.key] = (time.time(), idx)
        return idx

    def match_rate(self, key: str, there: Library, fresh: bool = False) -> float:
        """The share of library ``key``'s files found on the other server where their paths say."""
        idx = self.index(there, fresh)
        mine = [f.path for f in list(self.app.files) if str(f.library_key) == str(key)]
        if not mine:
            return 0.0
        found = sum(1 for p in mine if any(c in idx for c in self.paths_there(key, p, there)))
        rate = found / len(mine)
        with self.lock:
            self.rates[str(key)] = rate
        return rate

    def refresh(self) -> None:
        """Its libraries read again, and how well each pair's paths line up."""
        self.libraries(fresh=True)
        for key, there in self.pairs().items():
            if there is not None:
                try:
                    self.match_rate(key, there, fresh=True)
                except PlexError as e:
                    with self.lock:
                        self.error = str(e)

    def refresh_async(self) -> None:
        if self.configured():
            threading.Thread(target=self.refresh, name="plex2", daemon=True).start()

    def snapshot(self) -> dict:
        s = self.settings()
        with self.lock:
            libs = list(self.libs or [])
            rates, err = dict(self.rates), self.error
        pairs = {k: (v.key if v else "") for k, v in self.pairs(libs).items()} if libs else {}
        return {"configured": self.configured(), "own_signin": bool(s.get("account_token")),
                "signed_in": bool(s.get("account_token") or self.app.settings.section("plex").get("token")), "name": s.get("name") or "",
                "machine_id": s.get("machine_id") or "", "url": s.get("url") or "", "error": err, "pairs": pairs, "rates": rates, "matched": MATCHED,
                "states": self.states(), "any_mirror": self.any_mirror(), "undecided": self.undecided(),
                "libraries": [{"key": t.key, "title": t.title, "kind": t.kind} for t in libs]}

    # ------------------------------------------------------------ a converted file there
    def where(self, e: dict, now: float) -> str | None:
        """Where the other server has a converted file's MKV: "same" (a version of the original's item),
        "other" (an item of its own), "absent" (it doesn't have the original: nothing there to lose),
        "" (not yet), "unpaired" (paths don't line up: never taken as absent) — or None when there's
        no second server for this library. Its folder there is scanned while it hasn't."""
        there = self.mirror_for(str(e["library"]))
        if there is None:
            return None
        ox = e.setdefault("other", {})
        cands = self.paths_there(e["library"], e["plex_original"], there)
        if not cands:
            return "unpaired"
        if not ox.get("rating_key"):
            idx = self.index(there, fresh=now - ox.get("indexed", 0) > INDEX_TTL)
            ox["indexed"] = now
            orig = next((p for p in cands if p in idx), None)
            if orig:
                ox.update(path=orig, new=os.path.splitext(orig)[0] + ".mkv", rating_key=idx[orig])
            else:
                rate = self.rates.get(str(e["library"]))
                if rate is None:
                    rate = self.match_rate(e["library"], there)
                if rate < MATCHED:
                    return "unpaired"
                return "absent"  # (its paths line up, and it doesn't have this file: no item there to lose)
        files = self.run(lambda c: c.item_files(ox["rating_key"]))
        if files and ox["new"] in files:
            return "same"
        idx = self.index(there)
        if idx.get(ox["new"]) and idx[ox["new"]] != ox["rating_key"]:
            return "other"
        if files is None:
            ox.pop("rating_key", None)  # (merged into another item there, say: looked up again)
        if now - ox.get("scanned", 0) >= 15 * 60:
            ox["scanned"] = now
            self.run(lambda c: c.scan(there.key, os.path.dirname(ox["path"])))
        return ""

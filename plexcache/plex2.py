"""The mirrors: other Plex servers with the same files — another site's, kept in step by syncing the folders — chosen per
library, from the servers this server's Plex account can use (plex.tv's list; or, signed in apart, another account's).

A mirror matters wherever Plex Cache touches a file or a record the other site holds too: a converted file's original is
deleted only once both Plexes list the MKV (converting.py), a file set aside is purged only once both have let go of it
(trash.py), Sync Libraries carries edits between the two, Mirror Checks compares the sites, Organize Folders merges the
record there after a move, a folder's conversion to a child dataset is coordinated with the other side, and the Plex
Cache at that site is paired with directly (peerlink.py). This is how the mirrors are chosen, reached and read:

* Each library here has a state of its own (settings plex2.libraries, the Mirror page's Mirrored Libraries): not decided
  (the default: nothing is assumed, and what would act on a mirror waits), no mirror, or mirrored — by a library of one of
  the account's servers (its machine id, and the library's key there; "" for the one of the same name and kind). "*"
  stands for every library not listed. What every reader goes by (state_of, mirror_for, pairs). Any server for any
  library: two libraries may be mirrored by two servers.
* A server is reached when a library of it is chosen: its addresses are tried, direct ones first, and the first that
  answers as that server is kept with that server's own token (settings plex2.servers) — tried again at another address
  when it stops answering (reconnect). Its libraries are read when something asks (LIBS_TTL), never taken as "none"
  while they couldn't be.
* A file's path there: its place within its library folder here, under each of that library's folders there — the one
  Plex there has is it. The mirror library's files are read (paged, as here) and kept a few minutes, with how many of
  this library's files were found there: the paths only count as lining up when nearly all are (MATCHED). Otherwise
  nothing is deleted on its word.
"""

import threading
import time
from dataclasses import dataclass

from . import plexbackup
from .i18n import M
from .plex import Library, PlexClient, PlexError, direct_uri, is_relay, reachable, servers

RECONNECT_EVERY = 300  # seconds between tries at reaching a server by another of its addresses

INDEX_TTL = 300  # seconds a mirror library's file list is kept
LIBS_TTL = 600
RETRY_LIBS = 30  # seconds: a server's libraries read again this soon after they couldn't be (none known yet)
MATCHED = 0.9  # the share of a library's files found there for its paths to count as lining up


@dataclass
class Mirror(Library):
    """A mirror's library: a library of one of the account's servers, with the server it's on."""
    server: str = ""       # the server's machine id
    server_name: str = ""


class Mirrors:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.libs: dict[str, tuple[list[Library] | None, float, str | None]] = {}  # server id -> (its libraries, when read, the last error)
        self.indexes: dict[tuple[str, str], tuple[float, dict]] = {}  # (server, its library key) -> (when, {path: rating key})
        self.rates: dict[str, float] = {}  # our library key -> share of its files found on its mirror
        self.counts: dict[str, tuple[int, int]] = {}  # our library key -> (its files found on its mirror, its files)
        self.compare_errors: dict[str, str] = {}  # our library key -> why its files couldn't be compared with its mirror's last time
        self.found: list[dict] = []  # the account's servers, as last listed (with their tokens: never shown)
        self.reading: set[str] = set()
        self._reconnected: dict[str, float] = {}

    def settings(self) -> dict:
        return self.app.settings.section("plex2")

    def servers(self) -> dict:
        """The servers reached so far: {machine id: {"name", "url", "token", "relay" (reached through Plex's relay only)}}
        — a plex.direct name kept for a local address read as the address itself (settings from before it was)."""
        out = {}
        for k, v in (self.settings().get("servers") or {}).items():
            if isinstance(v, dict):
                v = dict(v)
                v["url"] = direct_uri(v.get("url") or "", lan_only=True)
                out[k] = v
        return out

    def name_of(self, server: str) -> str:
        """A server's name, else its address, else its id."""
        s = self.servers().get(server) or {}
        return s.get("name") or s.get("url") or next((x["name"] for x in self.found if x["id"] == server), None) or server

    def configured(self, server: str) -> bool:
        s = self.servers().get(server) or {}
        return bool(s.get("url") and s.get("token"))

    # ------------------------------------------------------------ each library's state
    def entry(self, key: str) -> dict:
        """The library's setting: {"state": "unset" | "none" | "mirror", "server": its mirror's id, "key": its library there ("": by name)}."""
        libs = self.settings().get("libraries") or {}
        e = libs.get(str(key)) if str(key) in libs else libs.get("*")
        if not isinstance(e, dict):
            return {"state": "unset", "server": "", "key": ""}
        state = e.get("state") if e.get("state") in ("unset", "none", "mirror") else "unset"
        return {"state": state, "server": str(e.get("server") or "") if state == "mirror" else "", "key": str(e.get("key") or "") if state == "mirror" else ""}

    def state_of(self, key: str) -> str:
        """"unset" (not decided), "none" (no mirror), or "mirror"."""
        return self.entry(key)["state"]

    def server_of(self, key: str) -> str:
        """The machine id of the library's mirror, or ""."""
        return self.entry(key)["server"]

    def used(self) -> list[str]:
        """The servers some library is mirrored by (the "*" entry counts), in the order chosen."""
        out: list[str] = []
        for e in (self.settings().get("libraries") or {}).values():
            if isinstance(e, dict) and e.get("state") == "mirror" and e.get("server") and e["server"] not in out:
                out.append(str(e["server"]))
        return out

    def any_mirror(self) -> bool:
        """Whether some library is mirrored, by a server reached: what the Plex Cache links and the mirror's checks need.
        (Every library not listed, "*", counts before this Plex's libraries are read.)"""
        return any(self.configured(s) for s in self.used())

    def undecided(self) -> list[str]:
        """The libraries whose state isn't chosen yet."""
        return [str(lib["key"]) for lib in getattr(self.app, "libraries", []) or [] if lib.get("kind") and self.state_of(str(lib["key"])) == "unset"]

    def set_state(self, key: str, state: str, server: str = "", their_key: str = "") -> None:
        """A library's state chosen: "unset", "none", or "mirror" by ``their_key`` of ``server`` (""  : the library of the same name)."""
        if state not in ("unset", "none", "mirror"):
            raise ValueError(M("unknown state"))
        libs = dict(self.settings().get("libraries") or {})
        if state == "unset" and "*" not in libs:
            libs.pop(str(key), None)
        else:
            libs[str(key)] = {"state": state, "server": str(server or "") if state == "mirror" else "", "key": str(their_key or "") if state == "mirror" else ""}
        self.app.settings.update({"plex2": {"libraries": libs}})

    def choose(self, key: str, state: str, server: str = "", their_key: str = "") -> M:
        """A library's mirror chosen on the Mirror page: not decided, no mirror, or a library (``their_key``) of one of the
        account's servers (``server``) — the server reached first when it isn't yet. What was chosen, for the log."""
        title = next((str(lib.get("title") or lib["key"]) for lib in getattr(self.app, "libraries", []) or [] if str(lib["key"]) == str(key)), str(key))
        if state != "mirror":
            self.set_state(key, state)
            return M("{library}: no mirror", library=title) if state == "none" else M("{library}: mirror not decided", library=title)
        if not server:
            raise ValueError(M("Choose a library of one of your account’s servers."))
        if not self.configured(server):
            self.reach(server)
        self.set_state(key, "mirror", server, their_key)
        self.libraries(server, fresh=True)
        there = self.mirror_for(key)
        if there is not None:
            self.compare(key, there)
        return M("{library}: mirrored by {name}", library=title, name=f"{self.name_of(server)}: {there.title}" if there else self.name_of(server))

    def states(self) -> dict:
        """Every library's state, for the page: {key: {"state", "server", "key", "there": the library there (title) or "", "there_key", "name": the server's}}."""
        out = {}
        for lib in getattr(self.app, "libraries", []) or []:
            if not lib.get("kind"):
                continue
            key = str(lib["key"])
            e = self.entry(key)
            there = self.mirror_for(key) if e["state"] == "mirror" and self.known(e["server"]) else None
            out[key] = {"state": e["state"], "server": e["server"], "key": e["key"], "there": there.title if there else "", "there_key": there.key if there else "",
                        "name": self.name_of(e["server"]) if e["server"] else ""}
        return out

    def mirror_for(self, key: str) -> Mirror | None:
        """The library that mirrors library ``key``, or None: not mirrored (not decided, or none), its server not reached,
        or the library named not there (nor one of the same name and kind)."""
        e = self.entry(key)
        if e["state"] != "mirror" or not e["server"] or not self.configured(e["server"]):
            return None
        theirs = self.libraries(e["server"])
        if e["key"]:
            t = next((t for t in theirs if t.key == e["key"]), None)
        else:
            lib = next((x for x in getattr(self.app, "libraries", []) or [] if str(x["key"]) == str(key)), None) or {}
            t = next((t for t in theirs if t.title.lower() == str(lib.get("title", "")).lower() and t.kind == lib.get("kind")), None)
        return Mirror(t.key, t.title, t.type, t.kind, list(t.locations), t.changed, e["server"], self.name_of(e["server"])) if t else None

    def pairs(self) -> dict[str, Mirror | None]:
        """Each library here -> the library that mirrors it (mirror_for), or None."""
        return {str(lib["key"]): self.mirror_for(str(lib["key"])) for lib in getattr(self.app, "libraries", []) or []}

    # ------------------------------------------------------------ reaching a server
    def client(self, server: str, timeout: float = 30) -> PlexClient:
        s = self.servers().get(server) or {}
        if not (s.get("url") and s.get("token")):
            raise PlexError(M("{name} isn’t reached yet", name=self.name_of(server)))
        client = PlexClient(s["url"], s["token"], timeout=timeout)
        client.before_change = lambda c, op, keys: plexbackup.guard(self.app, c, "mirror:" + server, op, keys)  # (each change to its records backed up first)
        return client

    def run(self, server: str, fn, timeout: float = 30):
        """``fn(client)`` on the server — and, when it doesn't answer at its address, once more at another of its
        addresses (reconnect: a public address changes; the one that lasts is preferred)."""
        try:
            return fn(self.client(server, timeout))
        except PlexError:
            if not self.reconnect(server):
                raise
            return fn(self.client(server, timeout))

    def _token(self) -> str:
        """The account's token: its own sign-in, else this server's."""
        return self.settings().get("account_token") or self.app.settings.section("plex").get("token") or ""

    def reconnect(self, server: str) -> bool:
        """The server's addresses listed again from plex.tv and tried in rank order (plex.rank_connections); the first that
        answers is kept. True when one does. At most every RECONNECT_EVERY seconds."""
        token, now = self._token(), time.time()
        if not token or now - self._reconnected.get(server, 0) < RECONNECT_EVERY:
            return False
        self._reconnected[server] = now
        try:
            found = next((x for x in servers(self.app.settings.section("general")["client_id"], token) if x["id"] == server), None)
        except PlexError:
            return False
        if not found:
            return False
        url = reachable(found)
        if not url:
            return False
        s = self.servers().get(server) or {}
        if url != s.get("url") or found["token"] != s.get("token") or is_relay(found, url) != bool(s.get("relay")):
            self.remember(found, url)
            self.app.activity.event("sync", M("{name} is reached at {url} now", name=s.get("name") or found["name"], url=url))
        return True

    def remember(self, server: dict, url: str) -> None:
        """A server reached (plex.tv's entry and the address that answered): kept in the settings — with whether that
        address is Plex's relay, for its light to say."""
        svs = self.servers()
        svs[server["id"]] = {"name": server.get("name") or "", "url": url, "token": server.get("token") or "", "relay": is_relay(server, url)}
        self.app.settings.update({"plex2": {"servers": svs}})

    def list_servers(self) -> list[dict]:
        """The servers the signed-in account can use (not this one): [{"id", "name", "owned", "addresses", "reached"}]."""
        # this server's own sign-in lists the account's other servers too: a sign-in of its own only for a server another account can use
        token = self._token()
        if not token:
            raise ValueError(M("Sign in with Plex first (System → Plex)."))
        try:
            got = servers(self.app.settings.section("general")["client_id"], token)
        except PlexError as e:
            raise ValueError(str(e)) from None
        mine = self._main_id()
        with self.lock:
            self.found = [x for x in got if x["id"] != mine]
        return [{"id": x["id"], "name": x["name"], "owned": x["owned"], "addresses": len(x["connections"]), "reached": self.configured(x["id"])} for x in self.found]

    def _main_id(self) -> str:
        try:
            return self.app.plex(timeout=10).identity().get("machine_id", "")
        except PlexError:
            return ""

    def reach(self, server: str) -> None:
        """A server of the account reached (listed first if it isn't in the list): the first of its addresses that answers
        is kept, with its token. ValueError when none does."""
        with self.lock:
            entry = next((x for x in self.found if x["id"] == server), None)
        if entry is None:
            self.list_servers()
            with self.lock:
                entry = next((x for x in self.found if x["id"] == server), None)
        if entry is None:
            raise ValueError(M("That server isn’t among your account’s: look again."))
        url = reachable(entry)
        if not url:
            raise ValueError(M("None of {name}’s addresses answer from here ({n} tried). If it’s at another site, it needs Remote Access on in its Plex settings, or a VPN between the sites — or enter an address this NAS can reach.",
                               name=entry["name"], n=len(entry["connections"])))
        self.remember(entry, url)

    def set_address(self, server: str, url: str) -> None:
        """An address for a server entered by hand (one plex.tv's list doesn't reach from here): kept when a Plex answers
        there as that server. Its token: the one plex.tv gave, else the account's."""
        with self.lock:
            entry = next((x for x in self.found if x["id"] == server), None)
        s = self.servers().get(server) or {}
        token = (entry or {}).get("token") or s.get("token") or self._token()
        try:
            got = PlexClient(url, token, timeout=10).identity()
        except PlexError as e:
            raise ValueError(M("No Plex answers at {url}: {why}", url=url, why=str(e))) from None
        if got.get("machine_id") != server:
            raise ValueError(M("The Plex at {url} is another server, not {name}.", url=url, name=self.name_of(server)))
        self.remember({"id": server, "name": (entry or {}).get("name") or s.get("name") or "", "token": token}, url)
        self.libraries(server, fresh=True)

    def forget(self, server: str = "") -> None:
        """A server forgotten (its libraries' mirrors not decided again), or, with none named, every server and the account's sign-in."""
        if server:
            svs = self.servers()
            svs.pop(server, None)
            libs = {k: v for k, v in (self.settings().get("libraries") or {}).items() if not (isinstance(v, dict) and v.get("server") == server)}
            self.app.settings.update({"plex2": {"servers": svs, "libraries": libs}})
        else:
            self.app.settings.update({"plex2": {"account_token": "", "servers": {}, "libraries": {}}})
        with self.lock:
            if server:
                self.libs.pop(server, None)
                self.indexes = {k: v for k, v in self.indexes.items() if k[0] != server}
            else:
                self.libs, self.indexes, self.rates, self.counts, self.compare_errors, self.found = {}, {}, {}, {}, {}, []

    # ------------------------------------------------------------ its libraries
    def libraries(self, server: str, fresh: bool = False) -> list[Library]:
        """A server's libraries, kept LIBS_TTL. Not read yet, or never read for an error: not known (never taken for "it
        has none"), tried again after RETRY_LIBS."""
        if not self.configured(server):
            return []
        with self.lock:
            libs, at, _err = self.libs.get(server, (None, 0.0, None))
            if libs is not None and not fresh and time.time() - at < LIBS_TTL:
                return list(libs)
            if libs is None and not fresh and time.time() - at < RETRY_LIBS:
                return []
            self.reading.add(server)
        try:
            got = [lib for lib in self.run(server, lambda c: c.libraries()) if lib.kind]
            err = None
        except PlexError as e:
            got, err = libs, str(e)
        with self.lock:
            self.libs[server] = (got, time.time(), err)
            self.reading.discard(server)
        return list(got or [])

    def known(self, server: str) -> bool:
        """Whether the server's libraries have been read (pairs() says what's paired, not a guess)."""
        with self.lock:
            return self.libs.get(server, (None, 0.0, None))[0] is not None

    def error(self, server: str) -> str | None:
        with self.lock:
            return self.libs.get(server, (None, 0.0, None))[2]

    def note_error(self, server: str, error: str) -> None:
        """A read of the server failed elsewhere (the look for mirrors, say): kept as its last error, its libraries as read,
        so its light says it answered, then stopped — one place for how a server is."""
        with self.lock:
            libs, at, _err = self.libs.get(server, (None, 0.0, None))
            self.libs[server] = (libs, at, error)

    def status(self) -> tuple[bool, str | None]:
        """Of the servers in use: (every one's libraries known, the first error among those not known)."""
        used = [s for s in self.used() if self.configured(s)]
        return all(self.known(s) for s in used), next((self.error(s) for s in used if not self.known(s) and self.error(s)), None)

    def read_all(self, fresh: bool = False) -> None:
        """Every server in use: its libraries read (again)."""
        for s in self.used():
            self.libraries(s, fresh=fresh)

    def paths_there(self, key: str, plex_path: str, there: Library) -> list[str]:
        """Where a file of library ``key`` (its path as this Plex has it) may be on its mirror."""
        lib = next((x for x in getattr(self.app, "libraries", []) or [] if str(x["key"]) == str(key)), None) or {}
        locations: list[str] = list(lib.get("locations") or [])
        for loc in sorted(locations, key=len, reverse=True):
            loc = loc.rstrip("/")
            if plex_path.startswith(loc + "/"):
                rel = plex_path[len(loc) + 1:]
                return [t.rstrip("/") + "/" + rel for t in there.locations]
        return []

    def index(self, there: Mirror, fresh: bool = False) -> dict:
        """A mirror library's files: {path: rating key}, kept a few minutes."""
        with self.lock:
            got = self.indexes.get((there.server, there.key))
        if got and not fresh and time.time() - got[0] < INDEX_TTL:
            return got[1]
        files = self.run(there.server, lambda c: c.library_files(there), timeout=120)
        idx = {f.path: f.rating_key for f in files}
        with self.lock:
            self.indexes[(there.server, there.key)] = (time.time(), idx)
        return idx

    def match_rate(self, key: str, there: Mirror, fresh: bool = False) -> float:
        """The share of library ``key``'s files found on its mirror where their paths say."""
        idx = self.index(there, fresh)
        mine = [f.path for f in list(self.app.files) if str(f.library_key) == str(key)]
        if not mine:
            return 0.0
        found = sum(1 for p in mine if any(c in idx for c in self.paths_there(key, p, there)))
        rate = found / len(mine)
        with self.lock:
            self.rates[str(key)] = rate
            self.counts[str(key)] = (found, len(mine))
        return rate

    def compare(self, key: str, there: Mirror) -> float | None:
        """A pair's files compared afresh (match_rate); None when its mirror's couldn't be read, the reason kept for the row
        (compare_errors) rather than the pair left looking as if it hadn't been compared yet."""
        try:
            rate = self.match_rate(key, there, fresh=True)
        except PlexError as e:
            with self.lock:
                self.compare_errors[str(key)] = str(e)
            return None
        with self.lock:
            self.compare_errors.pop(str(key), None)
        return rate

    def refresh(self) -> None:
        """Every mirror's libraries read again, and how well each pair's paths line up — each pair on its own, so one that
        can't be read doesn't stop the rest."""
        self.read_all(fresh=True)
        for key, there in self.pairs().items():
            if there is not None:
                self.compare(key, there)

    def refresh_async(self) -> None:
        if self.any_mirror():
            threading.Thread(target=self.refresh, name="plex2", daemon=True).start()

    def snapshot(self) -> dict:
        """For the state: the account's sign-in, each server in use (reached, its libraries, its error), every library's state,
        and how each pair's files line up (rates, with the counts behind them)."""
        s = self.settings()
        with self.lock:
            libs, rates, reading, errors = dict(self.libs), dict(self.rates), set(self.reading), dict(self.compare_errors)
            counts = {k: {"found": a, "of": b} for k, (a, b) in self.counts.items()}
        svs = []
        for sid in self.used():
            got, _at, err = libs.get(sid, (None, 0.0, None))
            svs.append({"id": sid, "name": self.name_of(sid), "url": (self.servers().get(sid) or {}).get("url") or "", "reached": self.configured(sid),
                        "relay": bool((self.servers().get(sid) or {}).get("relay")),
                        "known": got is not None, "reading": sid in reading, "error": err,
                        "libraries": [{"key": t.key, "title": t.title, "kind": t.kind} for t in got or []]})
        pairs = {k: {"server": v.server, "key": v.key, "title": v.title, "name": v.server_name} if v else None for k, v in self.pairs().items()}
        return {"own_signin": bool(s.get("account_token")), "signed_in": bool(self._token()), "servers": svs, "pairs": pairs, "rates": rates,
                "counts": counts, "compare_errors": errors, "matched": MATCHED, "states": self.states(), "any_mirror": self.any_mirror(), "undecided": self.undecided()}

    # ------------------------------------------------------------ a converted file there
    def where(self, e: dict, now: float) -> str | None:
        """Where the library's mirror has a converted file's MKV: "same" (a version of the original's item), "other" (an
        item of its own), "absent" (it doesn't have the original: nothing there to lose), "" (not yet), "unpaired" (paths
        don't line up: never taken as absent) — or None when the library has no mirror. Its folder there is scanned while
        it hasn't."""
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
                ox.update(path=orig, new=orig.rsplit(".", 1)[0] + ".mkv", rating_key=idx[orig])
            else:
                rate = self.rates.get(str(e["library"]))
                if rate is None:
                    rate = self.match_rate(e["library"], there)
                if rate < MATCHED:
                    return "unpaired"
                return "absent"  # (its paths line up, and it doesn't have this file: no item there to lose)
        files = self.run(there.server, lambda c: c.item_files(ox["rating_key"]))
        if files and ox["new"] in files:
            return "same"
        idx = self.index(there)
        if idx.get(ox["new"]) and idx[ox["new"]] != ox["rating_key"]:
            return "other"
        if files is None:
            ox.pop("rating_key", None)  # (merged into another item there, say: looked up again)
        if now - ox.get("scanned", 0) >= 15 * 60:
            ox["scanned"] = now
            folder = ox["path"].rsplit("/", 1)[0]
            self.run(there.server, lambda c: c.scan(there.key, folder))
        return ""

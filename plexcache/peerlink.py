"""The links between Plex Caches: the one here and the one at each mirror's site (the System page's
Mirror Plex Cache card, one block per mirrored server). Each finds the other, they pair, and from then
on they ask each other directly — what each side's folder sync carries, what files each has — instead
of only leaving notes in the shared folders (peers.py).

* **Finding it** — the other Plex Cache usually runs on the mirror's NAS: its Plex's addresses (the
  one that answers from here, and the LAN ones plex.tv lists) on the port this one uses and on
  8642–8649, and whatever addresses and port the other side advertises in its marker. Each is asked
  for ``/peer/v1/hello`` (open: public facts only); the one whose own Plex is that mirror is it. An
  address set by hand (setting peer.servers[<the mirror's id>].url) is tried first. The side that
  isn't found can say where it is: its Pair Now request carries its addresses and port.
* **Pairing** — this side has one key pair (Diffie-Hellman, RFC 3526's 2048-bit group: Python's own
  ``pow``, no library) kept in the config folder; the public half goes in its marker and its hello.
  Each pairing works out its own shared key from it and the other side's, which never travels. Pairing
  happens by itself when the other side's hello and its marker in our shared folder carry the same
  public key and its Plex is the mirror: someone who could only read the folder can't pose as it, and
  someone who could write there would also have to answer at the mirror NAS's address as the mirror's
  Plex. Without the marker (a folder sync that leaves out hidden folders), Pair Now pressed on both
  sides within PAIR_WINDOW pairs them. Unpair forgets that side; it isn't paired with again by itself
  until Pair Now.
* **Talking** — every request after pairing is signed with the shared key (HMAC-SHA256 over the
  method, path, time, a nonce and the body); a request older than MAX_AGE, or a nonce seen before, is
  refused. A peer only reads: what it may ask here is the status and the file lists. Nothing it asks
  changes a file or a setting.
* **Folder sync** — each side reports the markers it publishes and the ones it sees: ours not reaching
  the other side means claims don't either, so the two could optimize the same file (Mirror Checks
  holds optimizing there until it's fixed or ignored).

Everything here is in the state (PeerLinks.snapshot), per mirrored server, as five lights, worked out by
tick() every TICK_EVERY.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .i18n import M

log = logging.getLogger("plexcache")

API = 1
PORT = 8642               # Plex Cache's own port, as compose.yaml publishes it
PORTS = range(PORT, PORT + 8)  # the ports looked on besides the advertised ones and our own: one picked by hand is usually near the default
TICK_EVERY = 300          # seconds between looks at the other side (found, paired, talking)
FIND_EVERY = 3600         # while nothing is found: how often the addresses are tried again
PAIR_WINDOW = 600         # seconds after Pair Now during which the other side's Pair Now pairs the two
MAX_AGE = 300             # a signed request older (or newer) than this is refused: clocks must agree within it
SKEW_WARN = 120           # clocks further apart than this: a warning (claims and "ran 20 minutes ago" rely on time)
QUIET_AFTER = 900         # paired, and no answer for this long: not talking
LAG_WARN = 2 * 3600       # a marker taking longer than this to reach the other side: the sync is behind
NEVER_AFTER = 3 * 3600    # published this long ago and never seen there: the folder sync doesn't carry .plexcache
PROBE_TIMEOUT = 3

# RFC 3526, group 14: a 2048-bit safe prime, generator 2
P = int("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
        "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
        "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
        "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
        "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
        "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
G = 2


class PeerError(Exception):
    pass


def valid_public(text) -> int | None:
    """A public key as text (hex), as a number, when it's one: within 2 … P-2."""
    if not isinstance(text, str) or not 1 <= len(text) <= 520:
        return None
    try:
        y = int(text, 16)
    except ValueError:
        return None
    return y if 2 <= y <= P - 2 else None


def shared_key(private: int, other_public: str) -> bytes:
    """The key both sides work out from their own private half and the other's public one."""
    y = valid_public(other_public)
    if y is None:
        raise PeerError("bad public key")
    return hashlib.sha256(b"plexcache-peer-v1|" + pow(y, private, P).to_bytes(256, "big")).digest()


def fingerprint(a: str, b: str) -> str:
    """What both sides show of a pairing (the same on each): three groups of four characters."""
    h = hashlib.sha256("|".join(sorted((a.lower(), b.lower()))).encode()).hexdigest().upper()
    return "-".join(h[i:i + 4] for i in (0, 4, 8))


def signature(key: bytes, method: str, path: str, at: str, nonce: str, body: bytes) -> str:
    msg = "\n".join((method.upper(), path, at, nonce, hashlib.sha256(body or b"").hexdigest())).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def base_url(url: str) -> str:
    """An address as typed (192.168.1.20, nas.tailnet:9000, http://…) as http://host:port, no path."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    u = urllib.parse.urlsplit(url)
    if not u.hostname:
        return ""
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"{u.scheme}://{host}:{u.port or (PORT if u.scheme == 'http' else 443)}"


def _spans(ports: list[int]) -> str:
    """Ports as "8642–8649, 9001": each run of consecutive ports as a span."""
    out, run = [], []
    for p in sorted(set(ports)) + [None]:
        if run and (p is None or p != run[-1] + 1):
            out.append(str(run[0]) if len(run) == 1 else f"{run[0]}–{run[-1]}")
            run = []
        if p is not None:
            run.append(p)
    return ", ".join(out)


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _local_host(host: str) -> bool:
    """An address worth advertising and trying: a private or VPN address, or a name (not loopback)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return bool(host) and host not in ("localhost",)
    return not ip.is_loopback and not ip.is_unspecified


class PeerLinks:
    """This side of every link: the key pair, what this Plex Cache says of itself (its hello, its advert, its status), the
    requests that come in, and one PeerLink per mirrored server (link, for_library)."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        keys = app._load_json("peer-key.json") or {}
        self.private = int(keys["private"], 16) if isinstance(keys.get("private"), str) and valid_public(keys.get("public")) else 0
        if not self.private:
            self._new_keys()
        saved = app._load_json("peer.json") or {}
        paired, declined = saved.get("paired") or {}, saved.get("declined") or {}
        if "instance" in paired:  # (one pairing, from before the mirrors were several: the one mirrored server's)
            used = app.mirrors.used() if hasattr(app, "mirrors") else []
            paired = {used[0]: paired} if len(used) == 1 else {}
        if isinstance(declined, list):
            declined = {}
        self.links: dict[str, PeerLink] = {}
        for sid, p in paired.items():
            if isinstance(p, dict) and p.get("instance"):
                self.link(sid).paired = p
        for sid, d in declined.items():
            if isinstance(d, list):
                self.link(sid).declined = [str(x) for x in d]
        self.busy = False
        self.asked: dict[str, dict] = {}  # server id -> a Pair Now pressed at its site that didn't pair here: {"name", "instance", "why", "until"}
        self.nonces: dict[str, float] = {}
        self._tn_port: tuple[float, int | None] = (0.0, None)

    # ------------------------------------------------------------ keys and settings
    def _new_keys(self) -> None:
        self.private = secrets.randbits(256) | (1 << 255)
        # readable only by the app, as settings.json is: the private half is what every pairing's key is worked out from
        path = os.path.join(self.app.config_dir, "peer-key.json")
        tmp = f"{path}.{threading.get_ident()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"private": format(self.private, "x"), "public": self.public()}, fh)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("couldn't save peer-key.json: %s", e)

    def public(self) -> str:
        cached = getattr(self, "_public", None)
        if not cached or cached[0] != self.private:
            cached = self._public = (self.private, format(pow(G, self.private, P), "x"))
        return cached[1]

    def settings(self) -> dict:
        return self.app.settings.section("peer")

    def save(self) -> None:
        with self.lock:
            data = {"paired": {sid: lk.paired for sid, lk in self.links.items() if lk.paired},
                    "declined": {sid: lk.declined[-20:] for sid, lk in self.links.items() if lk.declined}}
        self.app._save_json("peer.json", data)

    def instance(self) -> str:
        return self.app.optimizing.instance()

    # ------------------------------------------------------------ the links
    def link(self, server: str) -> "PeerLink":
        """The link to the Plex Cache at the site of the mirrored server (its machine id), made as needed."""
        with self.lock:
            lk = self.links.get(server)
            if lk is None:
                lk = self.links[server] = PeerLink(self, server)
            return lk

    def used(self) -> list["PeerLink"]:
        """The links of the servers some library is mirrored by, in the order chosen."""
        return [self.link(sid) for sid in self.app.mirrors.used()]

    def for_library(self, key: str) -> "PeerLink | None":
        """The link to the Plex Cache at the site of the library's mirror, or None (no mirror, or none looked for there)."""
        sid = self.app.mirrors.server_of(str(key))
        if not sid:
            return None
        lk = self.link(sid)
        return lk if lk.wanted() else None

    def for_server(self, server: str) -> "PeerLink | None":
        return self.link(server) if server else None

    def wanted(self) -> bool:
        """Whether a Plex Cache is looked for at some mirror's site."""
        return any(lk.wanted() for lk in self.used())

    def talking(self) -> bool:
        return any(lk.talking() for lk in self.used())

    def known(self) -> bool:
        return any(lk.known() for lk in self.used())

    def health(self) -> str:
        """The links' worst light: "ok", "warn", "fail" or "off" (nothing looked for). What Mirror Checks warn on."""
        states = [lk.health() for lk in self.used() if lk.wanted()]
        order = {"off": 0, "ok": 0, "warn": 1, "fail": 2}
        if not states:
            return "off"
        return {0: "ok", 1: "warn", 2: "fail"}[max(order.get(s, 0) for s in states)]

    # ------------------------------------------------------------ what this side says of itself
    def own_plex_id(self) -> str:
        cached = getattr(self, "_own_plex", None)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        try:
            got = self.app.plex(timeout=8).identity().get("machine_id") or ""
        except Exception:  # noqa: BLE001 — Plex away: asked again next time
            return cached[1] if cached else ""
        self._own_plex = (time.time(), got)
        return got

    def own_port(self) -> int:
        """The port the other side reaches us on: as set; else the published port TrueNAS knows (as tick() last asked
        it: never asked here, which the state and the marker call); else the port the browser used; else the port we
        listen on."""
        set_port = int(self.settings().get("port") or 0)
        if set_port:
            return set_port
        tn = self._tn_port[1]
        if tn:
            return tn
        web = getattr(self.app, "web_url", None) or ""
        try:
            port = urllib.parse.urlsplit(web).port if web else None
        except ValueError:
            port = None
        if port:
            return port
        try:
            return int(os.environ.get("PLEXCACHE_PORT") or PORT)
        except ValueError:
            return PORT

    def _truenas_port(self) -> int | None:
        """The host port TrueNAS publishes this app's web port on (app.query's used_ports), kept an hour."""
        at, got = self._tn_port
        if time.time() - at < 3600:
            return got
        got = None
        try:
            from . import zfs
            me = zfs.own_container_id()
            if me and self.app.settings.section("truenas").get("api_key"):
                with self.app.truenas() as tn:
                    apps = tn.apps()
                inside = int(os.environ.get("PLEXCACHE_PORT") or PORT)
                for a in apps:
                    wl = a.get("active_workloads") or {}
                    if not any(str(c.get("id") or "").startswith(me[:12]) for c in wl.get("container_details") or []):
                        continue
                    for up in wl.get("used_ports") or []:
                        if int(up.get("container_port") or 0) == inside:
                            got = next((int(h.get("host_port")) for h in up.get("host_ports") or [] if h.get("host_port")), None)
        except Exception:  # noqa: BLE001 — no TrueNAS, no permission: the other ways
            got = None
        self._tn_port = (time.time(), got)
        return got

    def own_addresses(self) -> list[str]:
        """The addresses we may be reached on, to advertise: the one the browser uses, and Plex's here (usually the NAS)."""
        out = []
        for url in (getattr(self.app, "web_url", None) or "", self.app.settings.section("plex").get("url") or ""):
            h = _host_of(url)
            if h and _local_host(h) and h not in out:
                out.append(h)
        return out

    def advert(self) -> dict:
        """What our marker tells the other side about the link (peers.info)."""
        return {"api": API, "public": self.public(), "port": self.own_port(), "addresses": self.own_addresses()}

    def hello(self) -> dict:
        """The open answer: public facts only."""
        return {"app": "plexcache", "api": API, "instance": self.instance(), "name": getattr(self.app, "plex_name", "") or "",
                "version": self.app.version, "plex": self.own_plex_id(), "public": self.public(), "time": time.time()}

    def local_status(self) -> dict:
        """Our side, for the other: version and clock, and per library the marker we publish and the ones we see."""
        from . import peers
        found = {}
        try:
            found = peers.read(self.app)
        except Exception:  # noqa: BLE001
            pass
        published = dict(getattr(self.app, "published_markers", None) or {})
        first = dict(getattr(self.app, "first_published", None) or {})
        libs = {}
        for lib in getattr(self.app, "libraries", []) or []:
            key = str(lib["key"])
            seen = {}
            for inst, e in found.items():
                m = (e.get("by_library") or {}).get(key)
                if m:
                    seen[inst] = float(m.get("at") or 0)
            libs[key] = dict({"title": lib.get("title") or "", "published": published.get(key), "first": first.get(key), "seen": seen}, **own_library(self.app, key))
        return dict({"instance": self.instance(), "name": getattr(self.app, "plex_name", "") or "", "version": self.app.version, "api": API,
                     "time": time.time(), "publishing": bool(getattr(getattr(self.app, "optimizing", None), "client", None) and self.app.optimizing.client.configured),
                     "libraries": libs}, **own_facts(self.app))

    def local_files(self, key: str) -> dict:
        """A library's files on disk here (Plex's or not), by their place in the library folder: what Mirror Checks compare."""
        libdata = getattr(self.app, "libdata", None)
        if libdata is None or not self.app.library_info(key):
            return {"ready": False, "files": []}
        d = libdata.data(key)
        if not d.ready:
            libdata.fast_track(key)
            return {"ready": False, "files": []}
        return {"ready": True, "at": d.files_at, "files": rel_files(d)}

    # ------------------------------------------------------------ requests that come in
    def _markers(self) -> dict:
        """The other systems' markers in our shared folders (peers.read): {instance: marker}."""
        from . import peers
        try:
            return {i: e.get("marker") or {} for i, e in peers.read(self.app).items()}
        except Exception:  # noqa: BLE001
            return {}

    def verify(self, headers, method: str, path: str, body: bytes) -> "PeerLink":
        """A signed request from a paired side checked: its instance (which pairing), its time, its nonce, its signature.
        Raises PeerError when any is wrong. The link it came over."""
        inst = headers.get("X-Plexcache-Peer")
        link = next((lk for lk in list(self.links.values()) if lk.paired and lk.paired.get("instance") == inst), None)
        if link is None:
            raise PeerError("not paired with this Plex Cache")
        at, nonce, sig = headers.get("X-Plexcache-Time") or "", headers.get("X-Plexcache-Nonce") or "", headers.get("X-Plexcache-Signature") or ""
        try:
            age = abs(time.time() - int(at))
        except ValueError:
            raise PeerError("bad time") from None
        if age > MAX_AGE:
            raise PeerError("the clocks are too far apart (or the request is old)")
        if not hmac.compare_digest(sig, signature(link._key(), method, path, at, nonce, body)):
            raise PeerError("bad signature")
        now = time.time()
        with self.lock:
            self.nonces = {n: t for n, t in self.nonces.items() if now - t < 2 * MAX_AGE}
            if not nonce or nonce in self.nonces:
                raise PeerError("seen before")
            self.nonces[nonce] = now
        return link

    def handle(self, method: str, raw_path: str, headers, body: bytes, client_host: str) -> tuple[int, dict]:
        """A request on /peer/v1/… (``raw_path``: as it came, query and all — what was signed): (status, answer)."""
        path, _, qs = raw_path.partition("?")
        query = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
        if path == "/peer/v1/hello" and method == "GET":
            return 200, self.hello()
        if path == "/peer/v1/pair" and method == "POST":
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return 400, {"error": "invalid JSON"}
            data = data if isinstance(data, dict) else {}
            # the other side says which Plex it sits next to: the link to that mirror's site takes it
            sid = str(data.get("plex") or "")
            if not sid or sid not in self.app.mirrors.used():
                got = {"paired": False, "why": "this side has no library mirrored by that server, or no Plex Cache there"}
            else:
                got = self.link(sid).handle_pair(data, client_host)
            self._note_ask(sid, data, got)
            return 200, got
        try:
            link = self.verify(headers, method, raw_path, body)
        except PeerError as e:
            return 403, {"error": str(e)}
        if path == "/peer/v1/status" and method == "GET":
            return 200, self.local_status()
        if path == "/peer/v1/files" and method == "GET":
            return 200, self.local_files(str(query.get("library") or ""))
        if path == "/peer/v1/unpair" and method == "POST":
            with self.lock:
                old, link.paired, link.status = link.paired, None, None
                if old and old.get("instance") not in link.declined:
                    link.declined.append(old["instance"])
            self.save()
            if old:
                self.app.activity.event("sync", M("The Plex Cache on {name} unpaired from this one", name=old.get("name") or old.get("instance")))
            return 200, {"ok": True}
        return 404, {"error": "not found"}

    def _note_ask(self, sid: str, data: dict, got: dict) -> None:
        """Pair Now pressed on the other side (its request says so, with how long its window has left) that didn't pair here:
        kept, so every page here says it asks and where to press Pair Now — until its window closes, or the two pair. Only
        for a server of the account (the request isn't signed): the one mirrored here, or one listed to the sign-in."""
        if not sid:
            return
        known = sid in self.app.mirrors.used() or self.app.mirrors.configured(sid) or any(x.get("id") == sid for x in self.app.mirrors.found)
        with self.lock:
            if got.get("paired") or not data.get("manual") or not known:
                self.asked.pop(sid, None)
                return
            w = data.get("window")
            self.asked[sid] = {"name": str(data.get("name") or data.get("instance") or ""), "instance": str(data.get("instance") or ""),
                               "why": str(got.get("why") or ""), "until": time.time() + (w if isinstance(w, int) and 0 < w <= PAIR_WINDOW else PAIR_WINDOW)}

    def asks(self) -> dict:
        """The asks still open, for the page: {server id: {"name": the asker's, "server": the server's name, "why", "left": seconds,
        "wanted": whether a library here is mirrored by that server (else the Mirror page comes first)}}."""
        now = time.time()
        with self.lock:
            asked = {sid: dict(a) for sid, a in self.asked.items() if a["until"] > now}
        out = {}
        for sid, a in asked.items():
            used = sid in self.app.mirrors.used()
            if used and self.link(sid).paired:
                continue
            out[sid] = {"name": a["name"], "server": self.app.mirrors.name_of(sid), "why": a["why"], "left": int(a["until"] - now), "wanted": used and self.link(sid).wanted()}
        return out

    # ------------------------------------------------------------ the regular look
    def tick_async(self, force: bool = False) -> None:
        if self.busy:
            return
        links = [lk for lk in self.used() if force or time.time() - lk.ticked >= TICK_EVERY]
        if not links:
            return
        self.busy = True
        threading.Thread(target=self._tick_safe, args=(links, force), name="peerlink", daemon=True).start()

    def _tick_safe(self, links: list, force: bool = False) -> None:
        try:
            self._truenas_port()  # (the port TrueNAS publishes this app on, asked at most hourly: own_port)
            for lk in links:
                try:
                    lk.tick(force)
                except Exception:  # noqa: BLE001 — a background helper: logged, tried again next time
                    log.exception("peer link check failed (%s)", lk.server)
        finally:
            self.busy = False

    def snapshot(self) -> dict:
        """For the state (asked every few seconds): worked out at most every few seconds, or after a change here."""
        key = (tuple((lk.server, lk.ticked, bool(lk.paired), lk.window > time.time()) for lk in self.used()), self.busy, self.app.settings.rev,
               tuple(sorted((sid, a["until"]) for sid, a in self.asked.items())))
        cached = getattr(self, "_snap", None)
        if cached and cached[0] == key and time.time() - cached[1] < 15:
            return cached[2]
        got = self._snapshot()
        self._snap = (key, time.time(), got)
        return got

    def _snapshot(self) -> dict:
        return {"wanted": self.wanted(), "health": self.health(), "busy": self.busy, "public": self.public(),
                "own": {"port": self.own_port(), "addresses": self.own_addresses()}, "asked": self.asks(),
                "links": {lk.server: lk.snapshot() for lk in self.used()}}


class PeerLink:
    """The link to the Plex Cache at one mirrored server's site: found, paired, talking (one per server: PeerLinks.link)."""

    def __init__(self, links: PeerLinks, server: str):
        self.links, self.app, self.server = links, links.app, server
        self.lock = links.lock
        self.paired: dict | None = None     # {"instance", "name", "url", "public", "at", "how"}
        self.declined: list[str] = []  # instances unpaired by hand: not paired with by itself again
        self.window = 0.0          # until when Pair Now lets the other side pair with us
        self.found: dict | None = None  # the other side's hello, and where it answered: {"url", "hello", "at"}
        self.tried: list[str] = []  # the addresses tried last, for the light
        self.find_error = None      # why nothing was found (M), or why what answered isn't it
        self.pair_error = None      # why the last pairing didn't happen (M)
        self.last_ok = 0.0          # the last signed answer from the other side
        self.last_error = None      # the last signed request's failure (M or text)
        self.last_ms = 0
        self.status: dict | None = None  # the other side's last status (its version, clock, markers)
        self.skew = 0.0             # their clock minus ours, from the last status
        self.found_at = 0.0
        self.find_at = 0.0          # when find() last ran from tick()
        self.ticked = 0.0
        self._hosts: tuple[float, list[str]] = (0.0, [])  # the mirror Plex's addresses from plex.tv, kept an hour

    # ------------------------------------------------------------ what this link is to
    def name(self) -> str:
        return self.app.mirrors.name_of(self.server)

    def settings(self) -> dict:
        return self.links.settings()

    def url_setting(self) -> str:
        """The other Plex Cache's address set by hand for this mirror, or ""."""
        return str(((self.settings().get("servers") or {}).get(self.server) or {}).get("url") or "")

    def instance(self) -> str:
        return self.links.instance()

    def public(self) -> str:
        return self.links.public()

    def mirror_id(self) -> str:
        """The mirror's Plex (this link's server) while some library is mirrored by it and it's reached, or ""."""
        return self.server if self.server in self.app.mirrors.used() and self.app.mirrors.configured(self.server) else ""

    def wanted(self) -> bool:
        """Whether a Plex Cache is looked for at this mirror's site: the mirror in use, and not said to have none."""
        return bool(self.settings().get("enabled", True)) and bool(self.mirror_id())

    def known(self) -> bool:
        """Whether a Plex Cache is known to run at the mirror's site: one paired with, or found answering there — what a
        check that needs it is red for when it can't run (Mirror Checks), rather than merely not run."""
        return self.wanted() and bool(self.paired or self.found)

    def save(self) -> None:
        self.links.save()

    # ------------------------------------------------------------ finding the other side
    def _mirror_hosts(self) -> list[str]:
        """The mirror Plex's addresses: the one that answers from here first, then the ones plex.tv lists (local first)."""
        from .plex import direct_address
        sv = self.app.mirrors.servers().get(self.server) or {}
        # (its plex.direct name as the address it stands for; Plex's relay is no place for a Plex Cache)
        out = [direct_address(h) or h for h in [_host_of(sv.get("url") or "")] if h and not sv.get("relay")]
        at, listed = self._hosts
        if time.time() - at > 3600:
            listed = []
            token = self.app.mirrors._token()
            if token:
                try:
                    from .plex import rank_connections, servers
                    found = next((x for x in servers(self.app.settings.section("general")["client_id"], token) if x["id"] == self.server), None)
                    for c in rank_connections((found or {}).get("connections") or []):
                        if c.get("relay"):
                            continue
                        h = c.get("address") or _host_of(c.get("uri") or "")
                        if h and h not in listed:
                            listed.append(h)
                except Exception:  # noqa: BLE001 — plex.tv away: the address that answers is enough
                    listed = []
            self._hosts = (time.time(), listed)
        for h in listed:
            if h not in out:
                out.append(h)
        return out

    def _markers(self) -> dict:
        """The markers of the other systems whose Plex is this mirror: {instance: marker}."""
        return {i: m for i, m in self.links._markers().items() if m.get("plex") == self.server}

    def candidates(self) -> list[str]:
        """The addresses to try, in order: the one set by hand; the other side's own advert (its marker); the mirror Plex's
        hosts on the advertised ports, the port this one uses (two sides deployed alike share it) and 8642–8649."""
        out: list[str] = []

        from .plex import direct_uri

        def add(url):
            url = direct_uri(base_url(url))  # (a plex.direct name: the address it stands for, reached without the lookup)
            if url and url not in out:
                out.append(url)
        add(self.url_setting())
        if self.paired:
            add(self.paired.get("url") or "")
        ports = []
        for m in self._markers().values():
            ad = m.get("peer") or {}
            port = ad.get("port") if isinstance(ad.get("port"), int) else None
            if port:
                ports.append(port)
            for h in ad.get("addresses") or []:
                if isinstance(h, str):
                    add(f"{h}:{port or PORT}" if ":" not in h else f"[{h}]:{port or PORT}")
        for port in [self.links.own_port(), *PORTS]:
            if port not in ports:
                ports.append(port)
        for h in self._mirror_hosts():
            host = f"[{h}]" if ":" in h else h
            for port in ports:
                add(f"{host}:{port}")
        return out[:40]

    def tried_summary(self) -> str:
        """The addresses tried last, each host once with its ports (8642–8649 as a span): for the light. "" when none."""
        hosts: dict[str, list[int]] = {}
        for url in self.tried:
            u = urllib.parse.urlsplit(url)
            h = u.hostname or url
            hosts.setdefault(f"[{h}]" if ":" in h else h, []).append(u.port or (PORT if u.scheme == "http" else 443))
        return "; ".join(f"{h}: {_spans(ps)}" for h, ps in hosts.items())

    def _get(self, url: str, timeout: float = PROBE_TIMEOUT) -> dict:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                got = json.loads(resp.read(1024 * 1024) or b"{}")
        except urllib.error.HTTPError as e:  # (its response closed: an error left open warns when it's dropped)
            e.close()
            raise
        if not isinstance(got, dict):
            raise PeerError("not an object")
        return got

    def probe(self, url: str) -> dict | None:
        """The hello at an address, when a Plex Cache answers there (not us)."""
        try:
            got = self._get(url + "/peer/v1/hello")
        except Exception:  # noqa: BLE001 — nothing there, or not a Plex Cache
            return None
        if got.get("app") != "plexcache" or not valid_public(got.get("public")) or got.get("instance") == self.instance():
            return None
        return got

    def find(self) -> dict | None:
        """The other Plex Cache: the first address whose Plex Cache sits next to the mirror's Plex."""
        mirror = self.mirror_id()
        tried, wrong = [], None
        for url in self.candidates():
            tried.append(url)
            h = self.probe(url)
            if h is None:
                continue
            if h.get("plex") != mirror:
                wrong = M("A Plex Cache answers at {url}, but its Plex isn’t {name}.", url=url, name=self.name())
                continue
            with self.lock:
                self.found, self.found_at, self.tried, self.find_error = {"url": url, "hello": h, "at": time.time()}, time.time(), tried, None
            return self.found
        with self.lock:
            self.found, self.tried = None, tried
            self.find_error = wrong or (M("Nothing answered on the {n} addresses tried.", n=len(tried)) if tried
                                        else M("No address to try: {name} has none listed.", name=self.name()))
        return None

    # ------------------------------------------------------------ pairing
    def window_open(self) -> bool:
        return time.time() < self.window

    def pair_now(self) -> None:
        """Pair Now (System → Mirror Plex Cache): the other side may pair with us for a while, even without the marker,
        and an instance unpaired by hand is paired with again."""
        with self.lock:
            self.window = time.time() + PAIR_WINDOW
            self.declined = []
            self.pair_error = None
        self.save()
        self.links.tick_async(force=True)

    def unpair(self) -> None:
        """Forget the other side (and tell it): it isn't paired with again by itself until Pair Now."""
        old = self.paired
        if old:
            try:
                self.request("POST", "/peer/v1/unpair", {})
            except Exception:  # noqa: BLE001 — it's told when it next asks: refused, unknown
                pass
        with self.lock:
            if old and old.get("instance") not in self.declined:
                self.declined.append(old["instance"])
            self.paired, self.status, self.last_ok, self.last_error = None, None, 0.0, None
        self.save()
        if old:
            self.app.activity.event("sync", M("Unpaired from the Plex Cache on {name}", name=old.get("name") or old.get("instance")))
        threading.Thread(target=self.app.publish_peers, name="peers", daemon=True).start()

    def _marker_public(self, instance: str) -> str:
        return ((self._markers().get(instance) or {}).get("peer") or {}).get("public") or ""

    def may_pair(self, hello: dict, url: str) -> M | None:
        """Why we wouldn't pair with the Plex Cache whose hello this is, answering at ``url``, or None. By itself only when
        its marker in our folders carries the same key, and it answers at an address of the mirror's Plex (as plex.tv
        lists it to our sign-in) or the one set by hand: a hello's own word on which Plex it sits next to proves nothing."""
        if not self.wanted():
            return M("No library is mirrored by {name} here ([Mirror page](/mirrors)), or this side is set to have no Plex Cache at the mirrors’ sites.", name=self.name())
        if hello.get("plex") != self.mirror_id():
            return M("Its Plex isn’t {name}.", name=self.name())
        if self.window_open():
            return None
        if hello.get("instance") in self.declined:
            return M("It was unpaired here by hand: press “Pair Now” to pair again.")
        if not self.settings().get("auto_pair", True):
            return M("Pairing by itself is off: press “Pair Now” on both sides.")
        host = _host_of(url)
        if host not in self._mirror_hosts() and base_url(url) != base_url(self.url_setting()):
            return M("It answers at {url}, which isn’t an address of {name}: enter it below, or press “Pair Now” on both sides.", url=url, name=self.name())
        mark = self._marker_public(hello.get("instance") or "")
        if not mark:
            return M("Its marker hasn’t reached this side’s library folders (is .plexcache synced?): press “Pair Now” on both sides to pair without it.")
        if mark.lower() != str(hello.get("public") or "").lower():
            return M("The key in its marker doesn’t match the one it answers with: not paired.")
        return None

    def _keep_pairing(self, hello: dict, url: str, how: str) -> None:
        with self.lock:
            same = self.paired and self.paired.get("instance") == hello.get("instance") and self.paired.get("public") == hello.get("public")
            self.paired = {"instance": hello["instance"], "name": hello.get("name") or "", "url": url, "public": hello["public"],
                           "at": (self.paired or {}).get("at") if same else time.time(), "how": (self.paired or {}).get("how") if same else how}
            self.pair_error = None
        self.save()
        if not same:
            self.app.activity.event("sync", M("Paired with the Plex Cache on {name} ({url})", name=hello.get("name") or hello["instance"], url=url))

    def try_pair(self) -> bool:
        """Pairing with what find() found, when may_pair allows: it's asked to pair, and checks us the same way."""
        f = self.found
        if not f:
            return False
        why = self.may_pair(f["hello"], f["url"])
        if why:
            with self.lock:
                self.pair_error = why
            return False
        body = {"instance": self.instance(), "name": getattr(self.app, "plex_name", "") or "", "public": self.public(), "plex": self.links.own_plex_id(),
                "port": self.links.own_port(), "addresses": self.links.own_addresses(), "manual": self.window_open(),
                "window": max(0, int(self.window - time.time()))}  # (how long Pair Now here has left: the other side says so on its pages)
        try:
            got = self._post_open(f["url"] + "/peer/v1/pair", body)
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.pair_error = M("It couldn’t be asked to pair: {why}", why=str(getattr(e, "reason", None) or e))
            return False
        if not got.get("paired"):
            with self.lock:
                self.pair_error = M("It didn’t pair: {why}", why=got.get("why") or "?")
            return False
        if str(got.get("public") or "").lower() != str(f["hello"].get("public") or "").lower():
            with self.lock:
                self.pair_error = M("It answered with another key than its hello’s: not paired.")
            return False
        self._keep_pairing(f["hello"], f["url"], "manual" if self.window_open() else "auto")
        return True

    def _post_open(self, url: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT * 3) as resp:
                return json.loads(resp.read(1024 * 1024) or b"{}")
        except urllib.error.HTTPError as e:
            with e:  # (its response: read, then closed)
                answer = e.read()
            try:
                return json.loads(answer or b"{}")
            except ValueError:
                raise PeerError(f"HTTP {e.code}") from None

    def handle_pair(self, body: dict, client_host: str) -> dict:
        """The other side asks to pair (POST /peer/v1/pair): its hello fetched back from where it says it is — the address
        the request came from first — and judged as ours would be (may_pair)."""
        inst, pub = str(body.get("instance") or ""), str(body.get("public") or "")
        if not valid_public(pub) or not inst:
            return {"paired": False, "why": "bad request"}
        if not self.wanted():  # (nothing looked for here: nothing fetched back either)
            return {"paired": False, "why": "this side has no library mirrored by that server, or no Plex Cache there"}
        ports = [p for p in [body.get("port")] if isinstance(p, int) and 0 < p < 65536] + [PORT]
        hosts = [client_host] + [h for h in body.get("addresses") or [] if isinstance(h, str)]
        # where we'd look for it ourselves first (the address set by hand, the one found), then where it says it is
        urls = [u for u in (base_url(self.url_setting()), (self.found or {}).get("url") or "") if u]
        urls += [base_url(f"[{h}]:{port}" if ":" in h else f"{h}:{port}") for h in hosts[:8] for port in ports]
        seen = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            hello = self.probe(url)
            if not hello or hello.get("instance") != inst or str(hello.get("public") or "").lower() != pub.lower():
                continue
            why = self.may_pair(hello, url)
            if why:
                return {"paired": False, "why": str(why)}
            self._keep_pairing(hello, url, "manual" if self.window_open() else "auto")
            with self.lock:
                self.found, self.found_at, self.find_error = {"url": url, "hello": hello, "at": time.time()}, time.time(), None
            return {"paired": True, "public": self.public()}
        return {"paired": False, "why": "its hello couldn’t be fetched back from the addresses it gave"}

    # ------------------------------------------------------------ signed requests
    def _key(self) -> bytes:
        if not self.paired:
            raise PeerError("not paired")
        cached = getattr(self, "_shared", None)
        if not cached or cached[0] != (self.links.private, self.paired["public"]):
            cached = self._shared = ((self.links.private, self.paired["public"]), shared_key(self.links.private, self.paired["public"]))
        return cached[1]

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
        """A signed request to the paired side; its answer. Raises PeerError (or an OSError) when it fails."""
        if not self.paired:
            raise PeerError("not paired")
        data = json.dumps(body).encode() if body is not None else b""
        at, nonce = str(int(time.time())), secrets.token_hex(12)
        headers = {"Accept": "application/json", "X-Plexcache-Peer": self.instance(), "X-Plexcache-Time": at, "X-Plexcache-Nonce": nonce,
                   "X-Plexcache-Signature": signature(self._key(), method, path, at, nonce, data)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.paired["url"] + path, data=data if body is not None else None, method=method, headers=headers)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                got = json.loads(resp.read(64 * 1024 * 1024) or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                answer = e.read()
            try:
                why = json.loads(answer or b"{}").get("error")
            except ValueError:
                why = None
            raise PeerError(why or f"HTTP {e.code}") from None
        self.last_ms = int((time.time() - t0) * 1000)
        return got

    # ------------------------------------------------------------ what the other side tells us
    def facts(self) -> dict | None:
        """What the Plex Cache at the mirror's site says of itself, for converting a folder to a child dataset (migrating.py)
        and holding the file jobs while it converts one (mirrorhealth.converting): over the link while talking (fresh),
        else from its marker in our shared folders (as old as the folder sync makes it); None when neither is there.
        {"via": "link" | "marker", "at", "name", "platform" (None: an older Plex Cache that doesn't say), "truenas_api",
        "migration", "libraries": {our library key: {"key": its own, "folders", "converting", "holding"}}} — kept a few
        seconds (asked for every library, every few seconds)."""
        cached = getattr(self, "_facts", None)
        sig = (self.last_ok, self.ticked)
        if cached and cached[0] == sig and time.time() - cached[1] < 15:
            return cached[2]
        got = self._facts_link() or self._facts_marker()
        self._facts = (sig, time.time(), got)
        return got

    def _pairs(self) -> dict:
        """Our libraries mirrored by this link's server: {key: the library there}."""
        mirrors = getattr(self.app, "mirrors", None)
        try:
            pairs = mirrors.pairs() if mirrors is not None else {}
        except Exception:  # noqa: BLE001 — the mirror's Plex not read: its libraries aren't paired with ours
            pairs = {}
        return {k: v for k, v in pairs.items() if v is not None and v.server == self.server}

    def _facts_link(self) -> dict | None:
        st = self.status if self.talking() else None
        if not st:
            return None
        theirs = st.get("libraries") or {}
        libs = {}
        for key, there in self._pairs().items():
            t = theirs.get(str(there.key))
            if t is not None:
                libs[str(key)] = {"key": str(there.key), "folders": t.get("folders"), "converting": t.get("converting"), "holding": t.get("holding")}
        return {"via": "link", "at": self.last_ok, "name": st.get("name") or (self.paired or {}).get("name") or "", "version": st.get("version"),
                "platform": st.get("platform"), "truenas_api": st.get("truenas_api"), "migration": st.get("migration"), "libraries": libs}

    def _facts_marker(self) -> dict | None:
        """The other side's marker: the paired one's, else the one whose Plex is this mirror."""
        from . import peers
        try:
            found = peers.read(self.app)
        except Exception:  # noqa: BLE001
            return None
        inst = (self.paired or {}).get("instance")
        if inst not in found:
            inst = next((i for i, e in found.items() if (e.get("marker") or {}).get("plex") == self.server), None)
        e = found.get(inst) if inst else None
        if not e:
            return None
        m = e.get("marker") or {}
        libs = {key: {"key": "", "folders": b.get("folders"), "converting": b.get("converting"), "holding": b.get("holding")}
                for key, b in (e.get("by_library") or {}).items()}
        return {"via": "marker", "at": float(m.get("at") or 0), "name": m.get("name") or inst, "version": m.get("version"),
                "platform": m.get("platform"), "truenas_api": m.get("truenas_api"), "migration": m.get("migration"), "libraries": libs}

    def files(self, their_key: str) -> dict:
        """The other side's files of its library ``their_key``."""
        return self.request("GET", "/peer/v1/files?" + urllib.parse.urlencode({"library": their_key}), timeout=120)

    # ------------------------------------------------------------ the regular look
    def tick(self, force: bool = False) -> None:
        """Found, paired, talking: each step tried in turn. While nothing answers, the addresses are tried again only
        every FIND_EVERY (or when asked: Pair Now, Test Again)."""
        self.ticked = time.time()
        if not self.wanted():
            return
        if self.paired:
            if self.talk():
                return
            # not answering where it was: looked for again (a new address), the same instance and key kept
            f = self.find()
            if f and f["hello"].get("instance") == self.paired.get("instance") and f["hello"].get("public") == self.paired.get("public"):
                with self.lock:
                    self.paired["url"] = f["url"]
                self.save()
                self.talk()
            return
        if self.found is None and self.tried and time.time() - self.find_at < FIND_EVERY and not (force or self.window_open()):
            return
        self.find_at = time.time()
        if self.find() and self.try_pair():
            self.talk()

    def talk(self) -> bool:
        """The other side's status asked; True when it answered."""
        try:
            st = self.request("GET", "/peer/v1/status", timeout=20)
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.last_error = str(e)
            return False
        with self.lock:
            self.status, self.last_ok, self.last_error = st, time.time(), None
            self.skew = float(st.get("time") or time.time()) - time.time()
            if st.get("name") and self.paired and st["name"] != self.paired.get("name"):
                self.paired["name"] = st["name"]
        return True

    def talking(self) -> bool:
        return bool(self.paired) and time.time() - self.last_ok < QUIET_AFTER

    # ------------------------------------------------------------ folder sync, per library
    def folder_sync(self) -> dict:
        """Per library of ours mirrored by this server: whether our markers reach the other side and theirs reach us —
        {key: {"status": "ok" | "warn" | "fail" | "off", "out": seconds behind or None, "in": …, "text": M}}."""
        out: dict = {}
        st = self.status if self.talking() else None
        if not st:
            return out
        published = dict(getattr(self.app, "published_markers", None) or {})
        mine = self.instance()
        theirs = self.paired.get("instance") if self.paired else ""
        from . import peers
        try:
            here = peers.read(self.app)
        except Exception:  # noqa: BLE001
            here = {}
        now = time.time()
        for key, there in self._pairs().items():
            t = (st.get("libraries") or {}).get(str(there.key)) or {}
            row = {"status": "ok", "out": None, "in": None}
            # ours, there
            ours_at, seen_there = published.get(str(key)), (t.get("seen") or {}).get(mine)
            if ours_at:
                if seen_there is None:
                    if self._published_long_ago(str(key)):  # (never seen there, after hours of publishing)
                        row["status"] = "fail"
                elif seen_there < ours_at - LAG_WARN:
                    row["out"] = ours_at - seen_there
                    row["status"] = "warn"
                else:
                    row["out"] = max(0.0, ours_at - seen_there)
            # theirs, here
            their_at, their_first = t.get("published"), t.get("first")
            m = ((here.get(theirs) or {}).get("by_library") or {}).get(str(key))
            seen_here = float(m.get("at") or 0) if m else None
            if their_at:
                if seen_here is None:
                    if their_first and now - their_first > NEVER_AFTER:
                        row["status"] = "fail"
                elif seen_here < their_at - LAG_WARN and row["status"] == "ok":
                    row["status"] = "warn"
                    row["in"] = their_at - seen_here
                else:
                    row["in"] = max(0.0, their_at - seen_here)
            if not ours_at and not their_at:
                row["status"] = "off"
            out[str(key)] = row
        return out

    def _published_long_ago(self, key: str) -> bool:
        first = (getattr(self.app, "first_published", None) or {}).get(key)
        return bool(first) and time.time() - first > NEVER_AFTER

    # ------------------------------------------------------------ the lights
    def lights(self) -> dict:
        """The System card's five lights for this server: {"found", "paired", "talking", "folders", "version"}: {"status", "text", "detail"}."""
        off = lambda text, detail="": {"status": "off", "text": text, "detail": detail}  # noqa: E731
        name = self.name()
        if not self.mirror_id():
            why = M("No library is mirrored by {name}: choose one on the [Mirror page](/mirrors).", name=name)
            return {"found": off(M("No mirror"), why), "paired": off(M("Not paired")), "talking": off(M("Not talking")),
                    "folders": off(M("Folder sync")), "version": off(M("Version and clock"))}
        if not self.settings().get("enabled", True):
            why = M("Set to have no Plex Cache at the mirrors’ sites.")
            return {"found": off(M("No Plex Cache at {name}’s site", name=name), why), "paired": off(M("Not paired")), "talking": off(M("Not talking")),
                    "folders": off(M("Folder sync")), "version": off(M("Version and clock"))}
        out = {}
        f, p = self.found, self.paired
        if p and (self.talking() or not f):
            out["found"] = {"status": "ok", "text": M("Plex Cache found on {name}", name=p.get("name") or p.get("instance")), "detail": p.get("url") or ""}
        elif f:
            out["found"] = {"status": "ok", "text": M("Plex Cache found on {name}", name=f["hello"].get("name") or f["hello"].get("instance")),
                            "detail": M("{url} · version {version}", url=f["url"], version=f["hello"].get("version") or "?")}
        elif not self.ticked:
            out["found"] = {"status": "testing", "text": M("Looking for the Plex Cache at {name}’s site…", name=name), "detail": ""}
        else:
            out["found"] = {"status": "warn", "text": M("No Plex Cache found at {name}’s site", name=name),
                            "detail": M("{why} Tried: {tried}. Runs on another port, or at another address? Enter host:port below — or press Pair Now on that Plex Cache first: its request tells this side where it is.",
                                        why=self.find_error or "", tried=self.tried_summary() or "—")}
        if p:
            out["paired"] = {"status": "ok", "text": M("Paired with {name}", name=p.get("name") or p.get("instance")),
                             "detail": M("Fingerprint {fp} · {how} {when}", fp=fingerprint(self.public(), p.get("public") or ""),
                                         how=M("paired by itself") if p.get("how") == "auto" else M("paired with Pair Now"),
                                         when=time.strftime("%Y-%m-%d %H:%M", time.localtime(p.get("at") or 0)))}
        elif f:
            out["paired"] = {"status": "warn", "text": M("Plex Cache found, but not paired"), "detail": self.pair_error or M("Pairing is tried every few minutes.")}
        else:
            out["paired"] = off(M("Not paired"), M("Waiting for the other Plex Cache to be found."))
        if not p:
            out["talking"] = off(M("Not talking"), M("Waiting for the pairing."))
        elif self.talking():
            out["talking"] = {"status": "ok", "text": M("Talking"), "detail": M("Last answer {when} ({ms} ms)", when=time.strftime("%H:%M", time.localtime(self.last_ok)), ms=self.last_ms)}
        else:
            out["talking"] = {"status": "fail", "text": M("Not answering"),
                              "detail": M("{url}: {why}", url=p.get("url") or "", why=self.last_error or M("no answer yet"))}
        out["folders"] = self._folders_light()
        out["version"] = self._version_light()
        return out

    def _folders_light(self) -> dict:
        if not self.talking():
            return {"status": "off", "text": M("Folder sync"), "detail": M("Checked once the two are talking.")}
        publishing = bool(getattr(getattr(self.app, "optimizing", None), "client", None) and self.app.optimizing.client.configured)
        if not publishing or not (self.status or {}).get("publishing", True):  # (grey: without it there's nothing to optimize, nor claims to carry)
            return {"status": "off", "text": M("Folder sync can’t be checked"),
                    "detail": M("The markers are written by the optimizer service, which isn’t set up on both sides. Optimizing needs it too, and the claims it makes are what the folder sync must carry.")}
        rows = self.folder_sync()
        names = {str(lib["key"]): lib.get("title") or lib["key"] for lib in getattr(self.app, "libraries", []) or []}
        bad = {k: r for k, r in rows.items() if r["status"] in ("warn", "fail")}
        if not rows or all(r["status"] == "off" for r in rows.values()):
            return {"status": "off", "text": M("Folder sync"), "detail": M("No markers published yet: checked after the next one (every 30 minutes).")}
        if bad:
            worst = "fail" if any(r["status"] == "fail" for r in bad.values()) else "warn"
            return {"status": worst, "text": M("The .plexcache folder doesn’t sync") if worst == "fail" else M("The folder sync is behind"),
                    "detail": M("{libraries}: the markers and claims in .plexcache don’t reach the other side (in time). Optimizing there is held until it’s fixed or ignored on [Tools → Mirror Checks](/mirrorchecks).",
                                libraries=", ".join(str(names.get(k, k)) for k in bad))}
        lag = max([r["out"] or 0 for r in rows.values()] + [r["in"] or 0 for r in rows.values()])
        return {"status": "ok", "text": M("Folder sync carries .plexcache"), "detail": M("Both ways, up to {minutes} minutes behind", minutes=int(lag // 60))}

    def _version_light(self) -> dict:
        st = self.status if self.talking() else None
        if not st:
            return {"status": "off", "text": M("Version and clock"), "detail": M("Checked once the two are talking.")}
        problems = []
        if int(st.get("api") or 0) != API:
            problems.append(M("the link’s version differs ({theirs} there, {ours} here)", theirs=st.get("api"), ours=API))
        if str(st.get("version") or "") != str(self.app.version):
            problems.append(M("Plex Cache {theirs} there, {ours} here", theirs=st.get("version") or "?", ours=self.app.version))
        if abs(self.skew) > SKEW_WARN:
            problems.append(M("the clocks are {n} seconds apart", n=int(abs(self.skew))))
        if problems:
            return {"status": "warn", "text": M("Versions or clocks differ"), "detail": "; ".join(str(p) for p in problems)}
        return {"status": "ok", "text": M("Same version, clocks agree"), "detail": M("Plex Cache {version} on both sides, clocks within {n} seconds", version=self.app.version, n=max(1, int(abs(self.skew))))}

    def health(self) -> str:
        """The link's worst light: "ok", "warn", "fail" or "off" (nothing looked for). What Mirror Checks warn on."""
        if not self.wanted():
            return "off"
        order = {"ok": 0, "off": 0, "testing": 0, "warn": 1, "fail": 2}
        lights = self.lights()
        worst = max((order.get(v["status"], 0) for v in lights.values()), default=0)
        return {0: "ok", 1: "warn", 2: "fail"}[worst]

    def snapshot(self) -> dict:
        with self.lock:
            paired = dict(self.paired) if self.paired else None
        return {"server": self.server, "name": self.name(), "wanted": self.wanted(), "lights": self.lights(), "health": self.health(), "paired": paired,
                "fingerprint": fingerprint(self.public(), paired["public"]) if paired else "", "window": max(0, int(self.window - time.time())),
                "url": self.url_setting(), "detected": (self.found or {}).get("url") or ""}


def own_facts(app) -> dict:
    """What this side tells the other of itself (its status, its marker): what it runs on — TrueNAS for certain only by
    its API, its kernel or its version file (platform.py) — whether its TrueNAS API is connected, and the conversion to a
    child dataset under way here (migrating.py)."""
    from . import platform as platform_mod
    z = getattr(app, "zfs", None) or {}
    p = z.get("platform") or platform_mod.detect(None)
    mig = getattr(app, "migration", None)
    return {"platform": {k: p.get(k) for k in ("name", "truenas", "how", "version")} | {"label": str(p.get("label") or "")},
            "truenas_api": bool(z.get("truenas_version")) and not z.get("truenas_error"),
            "migration": mig.public() if mig is not None else None}


def own_library(app, key: str) -> dict:
    """Of a library here, for the other side: its folders (dataset or not, room for a copy: Migration.folders), a
    conversion of one under way or left partway, and whether this side holds its file jobs there while the other converts."""
    mig, health = getattr(app, "migration", None), getattr(app, "mirror_health", None)
    out = {}
    try:
        out["folders"] = mig.folders(key) if mig is not None else None
        out["converting"] = mig.converting_info(key) if mig is not None else None
    except Exception:  # noqa: BLE001 — not worked out: not said
        log.debug("folders of %s not told", key, exc_info=True)
    try:
        out["holding"] = bool(health.converting(key)) if health is not None else None
    except Exception:  # noqa: BLE001
        out["holding"] = None
    return out


def rel_files(d) -> list[list]:
    """A library's files on disk (libdata.LibraryData), Plex's and not, as [place in the library folder, size, mtime]."""
    out = []
    for row in list(d.files) + list(d.unlisted):
        if row.get("missing"):
            continue
        rel = rel_of(d, row.get("local") or "")
        if rel:
            out.append([rel, int(row.get("size") or 0), float(row.get("mtime") or 0)])
    return out


def rel_of(d, local: str) -> str:
    """A local path's place within the library folder it's in (the same at both sites: the folders are synced)."""
    for _plex, loc in sorted(d.roots or [], key=lambda r: len(r[1] or ""), reverse=True):
        loc = (loc or "").rstrip("/")
        if loc and local.startswith(loc + "/"):
            return local[len(loc) + 1:]
    return ""

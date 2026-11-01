"""Every service the app connects to, held to one standard: Plex, TrueNAS, Syncthing, and the app's own API — the link to
the Dionysus at a mirror's site (peerlink.py). Each has a card on the System page whose lights come in the same order
(STEPS), each a question answered in turn, the next waiting on the one before:

* **found** — where it is, worked out without being told (a look among TrueNAS's apps, a port on a known host, the
  mirror's own addresses), and the address filled in from that;
* **server** — something answers at the address, and it's the service it should be;
* **key** — the secret is accepted (an API key, Plex's token, the link's pairing);
* **api** — every call the app makes is known to the release that answered (its own method list where it publishes one,
  TrueNAS; each call tried where it doesn't, Syncthing and Plex), orange naming what isn't, to be reported;

then the lights that are the service's own (Syncthing's folders and devices, the link's folder sync). Each has an
**identity** too (identity()): a key the service itself holds — Plex's machine identifier, TrueNAS's host id (its web
certificate without a key), Syncthing's device ID, the link's server identity — drawn as Syncthing's identicon at the
card's top right, so the same server shows the same mark on every page and at both sites, whatever address reaches it;
never an address, which changes and multiplies. Public identifiers all, for recognition, not for trust. The settings are
alike too (config.py): an address normalised with the service's scheme and port, a secret masked and seeded from an
environment variable. The code behind each light stays with its service (app.test_connections, syncthing.py,
peerlink.py); what's here is the standard they're read through: the steps, which a service may not have (its
``exceptions``, each with the reason), its calls (``calls``: what the api light checks), and one snapshot shape for the
page (App.state "integrations"), which app.js draws with one function (integrationLights).
MQTT is not one of these by design: nothing to find, no API to verify — its card is its own (homeassistant.py).

A new service follows the standard or asks for an exception here, by name and with its reason — the tests hold every
one to it (IntegrationStandardTests)."""

from .i18n import M

STEPS = ("found", "server", "key", "api")
STATUSES = ("ok", "warn", "fail", "off", "testing")
LABELS = {"found": M("Found"), "server": M("Server"), "key": M("Sign-in"), "api": M("API compatibility")}


def api_light(gone: list, untried: list, total: int, version: str, kind: str = "names") -> dict:
    """The api light, the same words for every service: every call known; some missing (named, with the version, to be
    reported); or some not tried yet. ``gone``: the missing calls' labels, each with how it's called; ``kind``: what the app
    would have to learn — method "names" (TrueNAS) or "paths" (REST)."""
    unit = M("commands") if kind == "commands" else M("calls")
    if gone:
        return {"status": "warn", "text": M("API compatibility: {n} of {total} {unit} missing", n=len(gone), total=total, unit=unit),
                "detail": M("{version} hasn’t got: {what}. The features that need them won’t work until the app learns the new {kind} — please report this version and these calls on GitHub.",
                            version=version or "?", what="; ".join(gone), kind=M("names") if kind == "names" else M("commands") if kind == "commands" else M("paths"))}
    if untried:
        return {"status": "ok", "text": M("API compatibility: {n} of {total} {unit} known", n=total - len(untried), total=total, unit=unit),
                "detail": M("Not tried yet, since there’s nothing to try them on: {what}.", what="; ".join(untried))}
    return {"status": "ok", "text": M("API compatibility: all {total} {unit} known", total=total, unit=unit),
            "detail": M("Every {item} the app makes is answered by {version}.", item=M("command") if kind == "commands" else M("call"), version=version or "")}


class Integration:
    """One service held to the standard. Subclasses give ``name`` (the settings section and the card's id), ``title``,
    ``calls`` (what the api light checks), ``own`` (its lights after the standard ones, in order), ``exceptions`` (the
    standard steps it hasn't got, each with why), and standard_lights() from the service's own code."""

    name = ""
    title = ""
    calls: dict = {}
    own: tuple = ()
    exceptions: dict = {}

    def __init__(self, app):
        self.app = app

    def standard_lights(self) -> dict:
        """{step: {"status", "text", "detail"}} for every step not excepted, and every own light."""
        raise NotImplementedError

    def identity(self) -> dict | None:
        """{"id", "short", "source", "name"}, or None while it isn't known (not connected yet)."""
        return None

    def steps(self) -> list[str]:
        return [s for s in STEPS if s not in self.exceptions] + list(self.own)

    def lights(self) -> dict:
        got = self.standard_lights()
        out = {}
        for step in self.steps():
            r = got.get(step) or {"status": "off", "text": LABELS.get(step, M("{step}", step=step)), "detail": ""}
            out[step] = {"status": r.get("status") or "off", "text": r.get("text") or "", "detail": r.get("detail") or ""}
        return out

    def snapshot(self) -> dict:
        return {"title": self.title, "steps": self.steps(), "lights": self.lights(), "labels": {s: LABELS.get(s) or s for s in STEPS},
                "exceptions": dict(self.exceptions), "calls": len(self.calls), "identity": self.identity()}


def _worst(rows: list[dict], fallback: dict) -> dict:
    """The light that says most across several (the first fail, else the first warn, else the first)."""
    for want in ("fail", "warn", "testing"):
        for r in rows:
            if r and r.get("status") == want:
                return r
    return rows[0] if rows and rows[0] else fallback


class PlexIntegration(Integration):
    name, title = "plex", "Plex"
    own = ("lan", "urls")
    exceptions = {"found": "Plex is where your media is: its address is yours to give (or the sign-in's, which lists your servers), not looked for on the network."}

    @property
    def calls(self):  # type: ignore[override]
        from .plex import CALLS
        return CALLS

    def standard_lights(self) -> dict:
        c = getattr(self.app, "connections", None) or {}
        return {"server": c.get("plex_server"), "key": c.get("plex_token"), "api": c.get("plex_api"), "lan": c.get("plex_lan"), "urls": c.get("plex_urls")}

    def identity(self) -> dict | None:
        return (getattr(self.app, "connections", None) or {}).get("plex_identity")


class TrueNASIntegration(Integration):
    name, title = "truenas", "TrueNAS"
    exceptions = {"found": "TrueNAS is the host this app runs on: the Platform light (platform.py) says whether it is, and the address is offered from the page’s own host."}

    @property
    def calls(self):  # type: ignore[override]
        from .truenas import METHODS
        return METHODS

    def standard_lights(self) -> dict:
        c = getattr(self.app, "connections", None) or {}
        return {"server": c.get("truenas_server"), "key": c.get("truenas_key"), "api": c.get("truenas_api")}

    def identity(self) -> dict | None:
        return (getattr(self.app, "connections", None) or {}).get("truenas_identity")


class SyncthingIntegration(Integration):
    name, title = "syncthing", "Syncthing"
    own = ("folders", "devices", "reach", "throttle")

    @property
    def calls(self):  # type: ignore[override]
        from .syncthing import CALLS
        return CALLS

    def standard_lights(self) -> dict:
        return self.app.syncthing.lights()

    def identity(self) -> dict | None:
        return self.app.syncthing.identity()


class LinkIntegration(Integration):
    """The app's own API: the link between two Dionysus instances (peerlink.py), one per mirrored server. The standard
    lights say the worst of them; the card keeps a block of lights per server (its exception), since each is a link of
    its own with Pair Now and Unpair."""

    name, title = "peer", "Mirror Dionysus"
    own = ("talking", "folders", "version")
    exceptions = {"server": "One hello answers both: the Dionysus found at the mirror’s site is the one reached (found)."}
    # (its card: a block of lights per mirrored server, each drawn by the one function in the standard's order, from that link's lights)

    @property
    def calls(self):  # type: ignore[override]
        from .peerlink import COMMANDS
        return COMMANDS

    def identity(self) -> dict | None:
        return self.app.peerlinks.identity()

    def standard_lights(self) -> dict:
        links = [lk for lk in self.app.peerlinks.used() if lk.wanted()]
        rows = [lk.lights() for lk in links]
        off = lambda text, detail="": {"status": "off", "text": text, "detail": detail}  # noqa: E731
        none = off(M("No mirror"), M("The Dionysus at a mirror’s site is looked for once a library is mirrored ([Mirror page](/mirrors))."))
        if not self.app.peerlinks.wanted():
            none = off(M("Not looked for"), M("Mirror Dionysus is off: Mirror Checks compare the two Plexes only."))
        return {"found": _worst([r.get("found") for r in rows], none), "key": _worst([r.get("paired") for r in rows], off(M("Not paired"))),
                "api": _worst([r.get("api") for r in rows], off(M("API compatibility"))), "talking": _worst([r.get("talking") for r in rows], off(M("Not talking"))),
                "folders": _worst([r.get("folders") for r in rows], off(M("Folder sync"))), "version": _worst([r.get("version") for r in rows], off(M("Version and clock")))}


ALL = (PlexIntegration, TrueNASIntegration, SyncthingIntegration, LinkIntegration)


def snapshot(app) -> dict:
    """Every integration's standard view, for the state: {name: Integration.snapshot()}."""
    out = {}
    for cls in ALL:
        try:
            out[cls.name] = cls(app).snapshot()
        except Exception as e:  # noqa: BLE001 — one service's trouble never hides the others' lights
            out[cls.name] = {"title": cls.title, "steps": [], "lights": {}, "labels": {}, "exceptions": dict(cls.exceptions), "calls": 0, "identity": None, "error": str(e)}
    return out

"""What the dev tools' own pages share (dev/style.py's review page, dev/auditgui.py's audits page): their colours, light
and dark, so they look like one set of tools. Not the app's look: the app's pages are the style guide's. And how their
servers answer: to the browser that opened the address they printed only (Session), with the same headers (send)."""

LIGHT = ("--bg:#f4f4f1;--card:#fff;--ink:#1b1f1d;--mute:#5a625d;--line:#dcdcd6;--code:#f6f7f5;--before:#7f4205;"
         "--before-bg:#fbe8cf;--after:#155c37;--after-bg:#ddefe3;--gone:#8a1c14;--gone-bg:#f8d9d6")
DARK = ("--bg:#161917;--card:#1f2321;--ink:#e6e9e7;--mute:#a3aca6;--line:#343a37;--code:#272c29;--before:#f3c58e;"
        "--before-bg:#4a3213;--after:#9fdcb8;--after-bg:#173b28;--gone:#f2aaa3;--gone-bg:#4a1e1a;color-scheme:dark")
THEME = (":root{" + LIGHT + "}@media (prefers-color-scheme:dark){:root:not([data-theme=light]){" + DARK + "}}"
         ":root[data-theme=dark]{" + DARK + "}")
BASE = ("*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--ink);"
        "font:15px/1.5 system-ui,sans-serif}")


# ---------------------------------------------------------------- their servers (127.0.0.1, a port of their own)
def headers(nonce: str = "", img: str = "data:") -> list[tuple[str, str]]:
    """What every response of a dev tool's page sends: never framed (an Approve or Start button overlaid by another
    site's page), never sniffed, and a Content-Security-Policy that runs only the page's own script and styles (their
    nonce, this response's: no style="..." in its markup) and loads nothing from anywhere else."""
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    style = script
    return [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", f"default-src 'none'; script-src {script}; style-src {style}; connect-src 'self'; "
                                        f"img-src {img}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")]


def nonced(page: str, nonce: str) -> str:
    """A page with this response's nonce on each of its own <script> and <style> tags."""
    return page.replace("<script>", f'<script nonce="{nonce}">').replace("<style>", f'<style nonce="{nonce}">')


class Session:
    """Who a dev tool's page is for: the browser that opened it. Its address carries a one-time code; the first visit
    with it gets a cookie (HttpOnly, SameSite=Strict) and every request after needs the cookie. So the page, and the
    token in it, go to that browser only: never to another program on this computer that merely asks with the right
    Host. A second visit with the code is refused, unless it's from the browser that has the cookie.

    The page is on a name of its own (<name>-<random>.localhost, this computer): cookies don't keep to a port, so on
    127.0.0.1 its cookie would go to every other server there too (another user's, say), but they do keep to a name.
    A browser that can't find that name gets 127.0.0.1 instead (opener's page tries the name first). The code is never
    on a command line nor in what a tool prints: the browser is sent a file only you can read (opener) that takes it there."""

    def __init__(self, name: str):
        import secrets
        self.name = name
        self.code = secrets.token_urlsafe(18)
        self.secret = secrets.token_urlsafe(32)
        self.host = f"{name}-{secrets.token_hex(8)}.localhost"
        self.used = False
        self.at = ""        # (the Host the browser came in by, once it has: the page's from then on)
        self.folder = ""    # (opener's, until the code is spent or the page closes)

    def cookie(self, port: int) -> str:
        return f"{self.name}-{port}"   # (each page's its own name)

    def address(self, port: int, host: str = "") -> str:
        """The page's address, without its code: what a tool prints."""
        return f"http://{host or self.host}:{port}/"

    def url(self, port: int, host: str = "") -> str:
        return f"{self.address(port, host)}?code={self.code}"

    def opener(self, port: int) -> str:
        """The file the browser is sent to: in a folder only you can open, only you can read it, and it takes the
        browser to the page — on its own name if the browser finds it (a tiny picture fetched from there first), else
        on 127.0.0.1. Removed once the code is spent (close)."""
        import json
        import os
        import tempfile
        self.folder = tempfile.mkdtemp(prefix=f"{self.name}-page-")   # (0700)
        path = os.path.join(self.folder, "open.html")
        named, loopback = self.url(port), self.url(port, "127.0.0.1")
        page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Opening the page…</title></head><body>'
                '<p>Opening the page…</p><script>(function(){var done=false,go=function(u){if(!done){done=true;location.replace(u);}},'
                f'i=new Image();i.onload=function(){{go({json.dumps(named)});}};i.onerror=function(){{go({json.dumps(loopback)});}};'
                f'setTimeout(function(){{go({json.dumps(loopback)});}},4000);i.src={json.dumps(self.address(port) + "ping.png")}+"?"+Date.now();'
                '})();</script></body></html>')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(page)
        return path

    def close(self) -> None:
        import shutil
        if self.folder:
            shutil.rmtree(self.folder, ignore_errors=True)
            self.folder = ""

    def _hosts(self, port: int) -> tuple[str, ...]:
        return (self.at,) if self.at else (f"{self.host}:{port}", f"127.0.0.1:{port}")

    def ok(self, handler, port: int) -> bool:
        """Whether a request is from the page's browser: by the name it came in by (not another site's name pointed
        here), not sent by another site's page, and with the cookie."""
        import http.cookies
        import secrets
        h = handler.headers
        if not self.at or h.get("Host") != self.at or h.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return False
        try:
            jar = http.cookies.SimpleCookie(h.get("Cookie", ""))
        except http.cookies.CookieError:
            return False
        got = jar.get(self.cookie(port))
        return got is not None and secrets.compare_digest(got.value, self.secret)

    def enter(self, handler, port: int, query: str) -> str | None:
        """For a visit with the code: the Set-Cookie that lets this browser in (the code then spent), else None."""
        import secrets
        import urllib.parse
        code = (urllib.parse.parse_qs(query).get("code") or [""])[0]
        host = handler.headers.get("Host", "")
        if not code or host not in self._hosts(port) or not secrets.compare_digest(code, self.code):
            return None
        if self.used and not self.ok(handler, port):
            return None
        self.used, self.at = True, host
        self.close()   # (the code spent: its file goes)
        return f"{self.cookie(port)}={self.secret}; Path=/; HttpOnly; SameSite=Strict"


def show(session: Session, port: int, open_browser: bool = True) -> str:
    """The page opened in the browser by way of session.opener's file (the code never on a command line); the file's
    path, to say (to open by hand when no browser opened)."""
    import pathlib
    import webbrowser
    path = session.opener(port)
    if open_browser:
        webbrowser.open(pathlib.Path(path).as_uri())
    return path


def private(path: str):
    """A log a dev tool's page writes (what it says while it's open), for you alone to read: 0600, made or not."""
    import os
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", buffering=1)


REFUSED = (b"This page is for the browser that opened the address its command printed (that address works once). "
           b"To open it again, run the command again.\n")


def send(handler, code: int, body: bytes, ctype: str, nonce: str = "", img: str = "data:",
         more: list[tuple[str, str]] | None = None) -> None:
    """A response of a dev tool's page, with what every one sends (headers)."""
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    for k, v in headers(nonce, img) + list(more or []):
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


# (a 1×1 transparent PNG: what opener's page fetches to see whether the browser finds the page's own name)
PING = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                     "0000000b4944415478da636000020000050001e9fadcd80000000049454e44ae426082")


def entry(handler, session: Session, port: int, where: str, query: str) -> bool:
    """opener's picture, or a visit with the one-time code, answered (True): the cookie and on to the page, or refused;
    False for any other request, for the page's own checks (session.ok). On to the page by a page of its own, not a
    redirect: the visit comes from opener's file, another site to the browser, and a SameSite=Strict cookie isn't sent
    on the way on from there; from the page's own address it is."""
    import secrets
    import urllib.parse
    if where == "/ping.png":
        send(handler, 200, PING, "image/png")
        return True
    if where != "/" or "code" not in urllib.parse.parse_qs(query):
        return False
    cookie = session.enter(handler, port, query)
    if cookie is None and not session.ok(handler, port):
        send(handler, 403, REFUSED, "text/plain; charset=utf-8")
        return True
    nonce = secrets.token_urlsafe(18)
    on = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Opening the page…</title></head><body>'
          f'<p><a href="/">Open the page</a></p><script nonce="{nonce}">location.replace("/");</script></body></html>')
    send(handler, 200, on.encode(), "text/html; charset=utf-8", nonce, more=[("Set-Cookie", cookie)] if cookie else [])
    return True

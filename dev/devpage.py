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
def headers(nonce: str = "", img: str = "data:", inline_styles: bool = False) -> list[tuple[str, str]]:
    """What every response of a dev tool's page sends: never framed (an Approve or Start button overlaid by another
    site's page), never sniffed, and a Content-Security-Policy that runs only the page's own script (its nonce, this
    response's) and loads nothing from anywhere else. ``inline_styles``: a page that writes style="..." in its markup."""
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    style = "'unsafe-inline'" if inline_styles else (f"'nonce-{nonce}'" if nonce else "'none'")
    return [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", f"default-src 'none'; script-src {script}; style-src {style}; connect-src 'self'; "
                                        f"img-src {img}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")]


def nonced(page: str, nonce: str) -> str:
    """A page with this response's nonce on each of its own <script> and <style> tags."""
    return page.replace("<script>", f'<script nonce="{nonce}">').replace("<style>", f'<style nonce="{nonce}">')


class Session:
    """Who a dev tool's page is for: the browser that opened the address it printed. That address carries a one-time
    code; the first visit with it gets a cookie (HttpOnly, SameSite=Strict) and every request after needs the cookie. So
    the page, and the token in it, go to that browser only: never to another program on this computer that merely asks
    with the right Host. A second visit with the code is refused, unless it's from the browser that has the cookie."""

    def __init__(self, name: str):
        import secrets
        self.name = name
        self.code = secrets.token_urlsafe(18)
        self.secret = secrets.token_urlsafe(32)
        self.used = False

    def cookie(self, port: int) -> str:
        return f"{self.name}-{port}"   # (cookies don't keep to a port: each page's its own name)

    def url(self, port: int) -> str:
        return f"http://127.0.0.1:{port}/?code={self.code}"

    def ok(self, handler, port: int) -> bool:
        """Whether a request is from the page's browser: its Host this computer's (not another site's name pointed
        here), not sent by another site's page, and with the cookie."""
        import http.cookies
        import secrets
        h = handler.headers
        if h.get("Host") != f"127.0.0.1:{port}" or h.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
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
        if not code or handler.headers.get("Host") != f"127.0.0.1:{port}" or not secrets.compare_digest(code, self.code):
            return None
        if self.used and not self.ok(handler, port):
            return None
        self.used = True
        return f"{self.cookie(port)}={self.secret}; Path=/; HttpOnly; SameSite=Strict"


REFUSED = (b"This page is for the browser that opened the address its command printed (that address works once). "
           b"To open it again, run the command again.\n")


def send(handler, code: int, body: bytes, ctype: str, nonce: str = "", img: str = "data:", inline_styles: bool = False,
         more: list[tuple[str, str]] | None = None) -> None:
    """A response of a dev tool's page, with what every one sends (headers)."""
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    for k, v in headers(nonce, img, inline_styles) + list(more or []):
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def entry(handler, session: Session, port: int, query: str) -> bool:
    """A visit with the one-time code, answered (True): the cookie and on to the page, or refused; False for any other
    request, for the page's own checks (session.ok)."""
    import urllib.parse
    if "code" not in urllib.parse.parse_qs(query):
        return False
    cookie = session.enter(handler, port, query)
    if cookie is None and not session.ok(handler, port):
        send(handler, 403, REFUSED, "text/plain; charset=utf-8")
        return True
    send(handler, 303, b"", "text/plain", more=[("Location", "/"), *([("Set-Cookie", cookie)] if cookie else [])])
    return True

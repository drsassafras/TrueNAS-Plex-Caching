"""Login for the web UI and API.

On when PLEXCACHE_USERNAME and PLEXCACHE_PASSWORD are both set in the app's YAML (as
compose.yaml does); with either missing, the web UI is open to anyone on the network.
The example password from compose.yaml (DEFAULT_PASSWORD) is never accepted.

After signing in, the browser keeps a session cookie (HttpOnly, SameSite=Strict)
signed with a key kept in the config folder, so it survives restarts. Signing out
revokes that session on the server too (a copied cookie stops working). Changing the
username or password signs everyone out. Scripts can use HTTP Basic auth instead.

Wrong passwords are answered after a delay, one at a time: guessing in parallel
doesn't go any faster. An address that keeps getting it wrong (LOCKOUT_FAILS in a row) is
refused for a while (LOCKOUT_SECONDS) — signing in from elsewhere still works.
"""

import base64
import json
import hashlib
import hmac
import os
import secrets
import threading
import time

COOKIE = "pc_session"
# The example password in compose.yaml. It's public, so it's never accepted: the app can't be
# used until it's changed (otherwise anyone who read the README could sign in).
DEFAULT_PASSWORD = "change-me"
SESSION_DAYS = 30
FAIL_DELAY = 1.0  # seconds added to each failed attempt, so passwords can't be guessed quickly
LOCKOUT_FAILS, LOCKOUT_SECONDS = 10, 15 * 60  # wrong in a row from one address, then refused this long


class Auth:
    def __init__(self, username: str = "", password: str = "", config_dir: str | None = None):
        self.username = username or ""
        self._password = password or ""
        self.enabled = bool(self.username and self._password)
        self.default_password = self.enabled and self._password == DEFAULT_PASSWORD
        self._key = self._load_key(config_dir) if self.enabled else b""
        self._lock = threading.Lock()  # one failed attempt at a time
        self._revoked_path = os.path.join(config_dir, "signed-out.json") if config_dir else None
        self._revoked: dict[str, int] = self._load_revoked()
        self._fails: dict[str, tuple[int, float]] = {}  # address -> (wrong in a row, when last)

    @classmethod
    def from_env(cls, config_dir: str | None = None) -> Auth:
        return cls(os.environ.get("PLEXCACHE_USERNAME", ""), os.environ.get("PLEXCACHE_PASSWORD", ""), config_dir)

    @staticmethod
    def _load_key(config_dir: str | None) -> bytes:
        path = os.path.join(config_dir, "session.key") if config_dir else None
        if path:
            try:
                with open(path, "rb") as fh:
                    key = fh.read()
                if len(key) >= 32:
                    return key
            except OSError:
                pass
        key = secrets.token_bytes(32)
        if path:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(key)
            except OSError:
                pass  # sessions then last until the app restarts
        return key

    # ------------------------------------------------------------ checking
    def check_password(self, username: str, password: str) -> bool:
        ok = (hmac.compare_digest(str(username).encode(), self.username.encode())
              & hmac.compare_digest(str(password).encode(), self._password.encode()))
        if not ok:
            # Held while waiting, so parallel guesses queue up: at most one per FAIL_DELAY.
            with self._lock:
                time.sleep(FAIL_DELAY)
        return bool(ok)

    def locked_out(self, address: str) -> bool:
        """Has this address got the password wrong too often lately?"""
        n, at = self._fails.get(address, (0, 0.0))
        if n >= LOCKOUT_FAILS and time.time() - at < LOCKOUT_SECONDS:
            return True
        if n and time.time() - at >= LOCKOUT_SECONDS:
            self._fails.pop(address, None)  # long enough ago: a fresh start
        return False

    def note_attempt(self, address: str, ok: bool) -> None:
        """Count a sign-in attempt from ``address``: a right one clears the count."""
        if ok:
            self._fails.pop(address, None)
            return
        n, _ = self._fails.get(address, (0, 0.0))
        self._fails[address] = (n + 1, time.time())
        if len(self._fails) > 10000:  # (many addresses: forget the oldest)
            for k in sorted(self._fails, key=lambda k: self._fails[k][1])[:5000]:
                del self._fails[k]

    def _sign(self, expires: int, sid: str) -> str:
        # The password is part of what's signed, so changing it ends every session.
        msg = f"{expires}:{sid}:{self.username}:{hashlib.sha256(self._password.encode()).hexdigest()}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        expires = int(time.time()) + SESSION_DAYS * 86400
        sid = secrets.token_hex(8)
        return f"{expires}.{sid}.{self._sign(expires, sid)}"

    def _parse(self, token: str | None) -> tuple[int, str] | None:
        """(expires, session id) of a genuine, unexpired session token."""
        parts = (token or "").split(".")
        if len(parts) != 3 or not parts[0].isdigit():
            return None
        expires, sid, sig = int(parts[0]), parts[1], parts[2]
        if expires <= time.time() or not hmac.compare_digest(sig, self._sign(expires, sid)):
            return None
        return expires, sid

    def session_ok(self, token: str | None) -> bool:
        if self.default_password:
            return False
        p = self._parse(token)
        return p is not None and p[1] not in self._revoked

    def revoke(self, token: str | None) -> None:
        """Sign this session out for good (not just in this browser)."""
        p = self._parse(token)
        if not p:
            return
        now = time.time()
        with self._lock:
            self._revoked = {k: v for k, v in self._revoked.items() if v > now}  # expired ones can go
            self._revoked[p[1]] = p[0]
            self._save_revoked()

    def _load_revoked(self) -> dict:
        try:
            with open(self._revoked_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return {str(k): int(v) for k, v in data.items() if int(v) > time.time()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_revoked(self) -> None:
        if not self._revoked_path:
            return
        try:
            fd = os.open(self._revoked_path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._revoked, fh)
            os.replace(self._revoked_path + ".tmp", self._revoked_path)
        except OSError:
            pass  # still revoked until the app restarts

    def cookie_value(self, headers) -> str | None:
        for part in (headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return None

    def request_ok(self, headers) -> bool:
        """Is this request signed in (session cookie, or Basic auth for scripts)?"""
        if not self.enabled:
            return True
        if self.session_ok(self.cookie_value(headers)):
            return True
        authz = headers.get("Authorization") or ""
        if authz.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(authz[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return self.check_password(user, pw) and not self.default_password
        return False

    @staticmethod
    def cookie(value: str, max_age: int, secure: bool = False) -> str:
        """The session cookie; ``secure`` when the browser reached the app over HTTPS (a reverse proxy)."""
        return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")

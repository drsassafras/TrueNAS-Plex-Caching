"""A small MQTT 3.1.1 client (standard library only).

Just what the MQTT bridge (homeassistant.py) needs: connect with a username and password
(optionally over TLS), a "last will" so Home Assistant marks everything unavailable
if the app disappears, QoS 0 publish (retained or not), subscribe (QoS 1 messages are acknowledged), and keep-alive.
"""

from __future__ import annotations

import socket
import ssl
import struct
import time

from .i18n import M


MAX_PACKET = 1024 * 1024


class MQTTError(Exception):
    """``step`` says where it failed: "broker" (reaching it), "tls", "login" or "session" (after connecting)."""

    def __init__(self, message: str, step: str = "session"):
        super().__init__(message)
        self.step = step


CONNACK_ERRORS = {
    1: ("broker", M("the broker doesn't support MQTT 3.1.1")),
    2: ("login", M("the broker rejected the client ID")),
    3: ("broker", M("the broker is running but not accepting connections right now")),
    4: ("login", M("wrong username or password")),
    5: ("login", M("not authorized — check the username and password, and that this login may connect")),
}


def _str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _packet(kind: int, body: bytes) -> bytes:
    n, length = len(body), b""
    while True:
        byte, n = n % 128, n // 128
        length += bytes([byte | (0x80 if n else 0)])
        if not n:
            break
    return bytes([kind]) + length + body


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


class MQTTClient:
    def __init__(self, host: str, port: int = 1883, client_id: str = "plexcache", username: str = "", password: str = "",
                 tls: bool = False, verify_tls: bool = True, keepalive: int = 60, will: tuple | None = None,
                 timeout: float = 10):
        self.host, self.port, self.client_id = host, port, client_id
        self.username, self.password = username, password
        self.tls, self.verify_tls = tls, verify_tls
        self.keepalive, self.will, self.timeout = keepalive, will, timeout
        self.sock: socket.socket | None = None
        self._buf = b""
        self._last_send = 0.0
        self._pid = 0

    # ----------------------------------------------------------- connection
    def connect(self) -> None:
        self.open()
        if self.tls:
            self.start_tls()
        self.login()

    def open(self) -> None:
        """Step 1: reach the broker (a TCP connection)."""
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except socket.gaierror:
            raise MQTTError(M("can't find {host} — check the address", host=self.host), "broker") from None
        except socket.timeout:
            raise MQTTError(M("no answer from {host}:{port} — check the address, port and firewall{hint}",
                              host=self.host, port=str(self.port), hint=self._other_ports()), "broker") from None
        except ConnectionRefusedError:
            raise MQTTError(M("{host} is up, but nothing is listening on port {port}{hint}", host=self.host, port=str(self.port),
                              hint=self._other_ports()), "broker") from None
        except OSError as e:
            raise MQTTError(M("can't reach {host}:{port}: {why}", host=self.host, port=str(self.port), why=str(e.strerror or e)),
                            "broker") from None

    def _other_ports(self):
        """A hint from the usual MQTT ports that do answer on that host."""
        open_ports = []
        for port in (1883, 8883):
            if port == self.port:
                continue
            try:
                socket.create_connection((self.host, port), timeout=1.5).close()
                open_ports.append(port)
            except OSError:
                pass
        if 1883 in open_ports and self.port == 8883:
            return M(". Port 1883 is open, so the broker is running without TLS. Turn on its TLS listener "
                     "(Home Assistant's Mosquitto add-on only opens 8883 when its certificate files exist), "
                     "or use 1883 without TLS")
        if open_ports:
            return M(". Port {port} answers on that host — is that the broker's port?", port=str(open_ports[0]))
        return ""

    def start_tls(self) -> None:
        """Step 2 (only with TLS): the encrypted connection."""
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            self.sock = ctx.wrap_socket(self.sock, server_hostname=self.host)
        except ssl.SSLCertVerificationError as e:
            self._drop()
            raise MQTTError(M("the broker's certificate wasn't accepted ({why}) — untick “Verify”, "
                              "or give the broker a trusted certificate", why=str(e.verify_message)), "tls") from None
        except (ssl.SSLError, OSError) as e:
            self._drop()
            reason = getattr(e, "reason", None) or str(e)
            text = f"{reason} {e}".lower()
            if ("wrong_version_number" in text or "wrong version" in text or isinstance(e, (ssl.SSLEOFError, ConnectionResetError))
                    or "eof" in text or "reset" in text):
                # A plain-MQTT port either answers a TLS greeting with non-TLS bytes or just hangs up.
                reason = M("port {port} doesn't use TLS — untick TLS, or use the broker's TLS port (usually 8883)", port=str(self.port))
            elif isinstance(e, socket.timeout):
                reason = M("no TLS answer on port {port} — is TLS turned on in the broker?", port=str(self.port))
            raise MQTTError(M("TLS failed: {why}", why=reason), "tls") from None

    def login(self) -> None:
        """Step 3: the MQTT handshake, with the username and password."""
        flags = 0x02  # clean session
        payload = _str(self.client_id)
        if self.will:
            topic, message, retain = self.will
            flags |= 0x04 | (0x20 if retain else 0)
            payload += _str(topic) + _str(message)
        if self.username:
            flags |= 0x80
            payload += _str(self.username)
            if self.password:
                flags |= 0x40
                payload += _str(self.password)
        body = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive) + payload
        try:
            self._send(_packet(0x10, body))
            kind, data = self._read_packet(self.timeout)
        except socket.timeout:
            self.close()
            hint = M(" — does the broker expect TLS on this port?") if not self.tls else ""
            raise MQTTError(M("connected, but the broker didn't answer the MQTT login{hint}", hint=hint), "login") from None
        except MQTTError as e:
            self.close()
            hint = M(" — does the broker expect TLS on this port?") if not self.tls else ""
            raise MQTTError(M("the broker hung up instead of answering the login{hint}", hint=hint), "login") from None
        if kind != 0x20 or len(data) < 2:
            self.close()
            hint = (M(" — this port may expect TLS (tick “Use TLS”)") if not self.tls and kind == 0x15
                    else M(" — check the port"))
            raise MQTTError(M("the answer wasn't from an MQTT broker{hint}", hint=hint), "login")
        if data[1]:
            self.close()
            step, msg = CONNACK_ERRORS.get(data[1], ("login", M("the broker refused the connection ({code})", code=str(data[1]))))
            raise MQTTError(msg, step)

    def _drop(self) -> None:
        """Close without saying goodbye (for a connection that never got going)."""
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self._buf = b""

    def close(self) -> None:
        if self.sock:
            try:
                self._send(_packet(0xE0, b""))
            except (OSError, MQTTError):
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self._buf = b""

    # ------------------------------------------------------------ messages
    def publish(self, topic: str, payload, retain: bool = False) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        self._send(_packet(0x30 | (1 if retain else 0), _str(topic) + payload))

    def subscribe(self, topics: list[str]) -> None:
        self._pid = self._pid % 65535 + 1
        body = struct.pack("!H", self._pid) + b"".join(_str(t) + b"\x00" for t in topics)
        self._send(_packet(0x82, body))

    def ping_if_due(self) -> None:
        if time.monotonic() - self._last_send > self.keepalive / 2:
            self._send(_packet(0xC0, b""))

    def poll(self, timeout: float) -> list[tuple[str, bytes]]:
        """Messages that arrive within ``timeout`` seconds: [(topic, payload), ...]."""
        out = []
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return out
            try:
                kind, data = self._read_packet(left)
            except socket.timeout:
                return out
            if kind & 0xF0 == 0x30:  # PUBLISH
                qos = (kind >> 1) & 3
                n = struct.unpack("!H", data[:2])[0]
                topic, rest = data[2:2 + n].decode("utf-8", "replace"), data[2 + n:]
                if qos:
                    pid, rest = rest[:2], rest[2:]
                    self._send(_packet(0x40, pid))  # PUBACK
                out.append((topic, rest))
            # SUBACK, PINGRESP and anything else need no reply

    # ------------------------------------------------------------- wire
    def _send(self, data: bytes) -> None:
        if not self.sock:
            raise MQTTError(M("not connected"))
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise MQTTError(M("connection lost: {why}", why=str(e))) from None
        self._last_send = time.monotonic()

    def _fill(self, n: int, timeout: float) -> None:
        """Make sure at least ``n`` bytes are buffered (without using them up)."""
        deadline = time.monotonic() + timeout
        while len(self._buf) < n:
            self.sock.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise
            except OSError as e:
                raise MQTTError(M("connection lost: {why}", why=str(e))) from None
            if not chunk:
                raise MQTTError(M("the broker closed the connection"))
            self._buf += chunk

    def _read_packet(self, timeout: float) -> tuple[int, bytes]:
        if not self.sock:
            raise MQTTError(M("not connected"))
        # Only take a packet out of the buffer once all of it has arrived, so a
        # timeout part-way through never splits one.
        self._fill(2, timeout)
        mult, length, i = 1, 0, 1
        while True:
            self._fill(i + 1, timeout)
            byte = self._buf[i]
            length += (byte & 0x7F) * mult
            mult *= 128
            i += 1
            if not byte & 0x80:
                break
            if i > 4:
                raise MQTTError(M("bad packet from the broker"))
        if length > MAX_PACKET:  # only small commands come this way; refuse anything huge
            raise MQTTError(M("the broker sent a {n}-byte message; at most {max} is accepted", n=length, max=MAX_PACKET))
        self._fill(i + length, timeout)
        whole, self._buf = self._buf[:i + length], self._buf[i + length:]
        return whole[0], whole[i:]

"""A small MQTT 3.1.1 client (standard library only).

Just what the MQTT bridge (homeassistant.py) needs: connect with a username and password
(optionally over TLS), a "last will" so Home Assistant marks everything unavailable
if the app disappears, QoS 0 publish (retained or not), subscribe, and keep-alive.

It follows the client rules of the OASIS MQTT 3.1.1 standard (the [MQTT-x.x.x-x] numbers below);
tests/test_plexcache.py (MQTTStandardsTests) checks them.
"""

from __future__ import annotations

import socket
import ssl
import struct
import time

from .i18n import M


MAX_PACKET = 1024 * 1024
MAX_LENGTH = 268_435_455  # the most a Remaining Length can say (four bytes)


class MQTTError(Exception):
    """``step`` says where it failed: "broker" (reaching it), "tls", "login" or "session" (after connecting)."""

    def __init__(self, message: str, step: str = "session"):
        super().__init__(message)
        self.step = step


CONNACK_ERRORS = {
    1: ("broker", M("the broker doesn’t support MQTT 3.1.1")),
    2: ("login", M("the broker rejected the client ID")),
    3: ("broker", M("the broker is running but not accepting connections right now")),
    4: ("login", M("wrong username or password")),
    5: ("login", M("not authorized — check the username and password, and that this login may connect")),
}


def _str(s: str) -> bytes:
    """A UTF-8 string field: no U+0000 [MQTT-1.5.3-2], at most 65535 bytes [MQTT-1.5.3]."""
    if "\x00" in s:
        raise ValueError("MQTT strings can't contain U+0000")
    b = s.encode("utf-8")
    if len(b) > 65535:
        raise ValueError("MQTT strings are at most 65535 bytes")
    return struct.pack("!H", len(b)) + b


def check_topic(topic: str) -> str:
    """A topic name to publish to: at least one character [MQTT-4.7.3-1], no wildcards
    [MQTT-3.3.2-2, MQTT-4.7.1-1] (and _str's rules: no U+0000, at most 65535 bytes)."""
    if not topic:
        raise ValueError("an MQTT topic name can't be empty")
    if "#" in topic or "+" in topic:
        raise ValueError(f"an MQTT topic name can't contain wildcards: {topic!r}")
    _str(topic)
    return topic


def check_filter(pattern: str) -> str:
    """A topic filter to subscribe to: at least one character, "#" only as the whole last level
    [MQTT-4.7.1-2], "+" only as a whole level [MQTT-4.7.1-3]."""
    if not pattern:
        raise ValueError("an MQTT topic filter can't be empty")
    levels = pattern.split("/")
    for i, level in enumerate(levels):
        if "#" in level and (level != "#" or i != len(levels) - 1):
            raise ValueError(f"'#' must be a whole level, at the end: {pattern!r}")
        if "+" in level and level != "+":
            raise ValueError(f"'+' must be a whole level: {pattern!r}")
    _str(pattern)
    return pattern


def _packet(kind: int, body: bytes) -> bytes:
    if len(body) > MAX_LENGTH:
        raise ValueError("an MQTT packet is at most 268435455 bytes")
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
            raise MQTTError(M("can’t find {host} — check the address", host=self.host), "broker") from None
        except socket.timeout:
            raise MQTTError(M("no answer from {host}:{port} — check the address, port and firewall{hint}",
                              host=self.host, port=str(self.port), hint=self._other_ports()), "broker") from None
        except ConnectionRefusedError:
            raise MQTTError(M("{host} is up, but nothing is listening on port {port}{hint}", host=self.host, port=str(self.port),
                              hint=self._other_ports()), "broker") from None
        except OSError as e:
            raise MQTTError(M("can’t reach {host}:{port}: {why}", host=self.host, port=str(self.port), why=str(e.strerror or e)),
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
                     "(Home Assistant’s Mosquitto add-on only opens 8883 when its certificate files exist), "
                     "or use 1883 without TLS")
        if open_ports:
            return M(". Port {port} answers on that host — is that the broker’s port?", port=str(open_ports[0]))
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
            raise MQTTError(M("the broker’s certificate wasn’t accepted ({why}) — untick “Verify”, "
                              "or give the broker a trusted certificate", why=str(e.verify_message)), "tls") from None
        except (ssl.SSLError, OSError) as e:
            self._drop()
            reason = getattr(e, "reason", None) or str(e)
            text = f"{reason} {e}".lower()
            if ("wrong_version_number" in text or "wrong version" in text or isinstance(e, (ssl.SSLEOFError, ConnectionResetError))
                    or "eof" in text or "reset" in text):
                # A plain-MQTT port either answers a TLS greeting with non-TLS bytes or just hangs up.
                reason = M("port {port} doesn’t use TLS — untick TLS, or use the broker’s TLS port (usually 8883)", port=str(self.port))
            elif isinstance(e, socket.timeout):
                reason = M("no TLS answer on port {port} — is TLS turned on in the broker?", port=str(self.port))
            raise MQTTError(M("TLS failed: {why}", why=reason), "tls") from None

    def login(self) -> None:
        """Step 3: the MQTT handshake, with the username and password."""
        flags = 0x02  # clean session (so even an empty client ID would be allowed [MQTT-3.1.3-7])
        payload = _str(self.client_id)  # first in the payload [MQTT-3.1.3-3]; then will, username, password [MQTT-3.1.3-1]
        if self.will:  # (will QoS 0; without a will, its QoS and retain bits stay 0 [MQTT-3.1.2-13, -15])
            topic, message, retain = self.will
            flags |= 0x04 | (0x20 if retain else 0)
            payload += _str(check_topic(topic)) + _str(message)
        if self.username:
            flags |= 0x80
            payload += _str(self.username)
            if self.password:  # (a password only with a username [MQTT-3.1.2-22])
                flags |= 0x40
                payload += _str(self.password)
        body = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive) + payload
        try:
            self._send(_packet(0x10, body))
            kind, data = self._read_packet(self.timeout)
        except socket.timeout:
            self.close()
            hint = M(" — does the broker expect TLS on this port?") if not self.tls else ""
            raise MQTTError(M("connected, but the broker didn’t answer the MQTT login{hint}", hint=hint), "login") from None
        except MQTTError as e:
            self.close()
            hint = M(" — does the broker expect TLS on this port?") if not self.tls else ""
            raise MQTTError(M("the broker hung up instead of answering the login{hint}", hint=hint), "login") from None
        if kind != 0x20 or len(data) < 2:
            self.close()
            hint = (M(" — this port may expect TLS (tick “Use TLS”)") if not self.tls and kind == 0x15
                    else M(" — check the port"))
            raise MQTTError(M("the answer wasn’t from an MQTT broker{hint}", hint=hint), "login")
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
            # Close gently: say we're done sending, then read whatever the broker still sends until it
            # hangs up. Closing a socket with unread data makes the system reset the connection, and a
            # broker may then drop what we sent last — like the "offline" message.
            try:
                self.sock.shutdown(socket.SHUT_WR)
            except (OSError, ValueError):
                pass
            try:
                self.sock.settimeout(1.0)
                end = time.monotonic() + 2
                while time.monotonic() < end and self.sock.recv(4096):
                    pass
            except (OSError, ValueError):
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self._buf = b""

    # ------------------------------------------------------------ messages
    def publish(self, topic: str, payload, retain: bool = False) -> None:
        """QoS 0: no packet identifier [MQTT-2.3.1-5], DUP 0 [MQTT-3.3.1-2]."""
        if isinstance(payload, str):
            payload = payload.encode()
        self._send(_packet(0x30 | (1 if retain else 0), _str(check_topic(topic)) + payload))

    def subscribe(self, topics: list[str]) -> None:
        """At least one filter [MQTT-3.8.3-3], a non-zero packet identifier [MQTT-2.3.1-1],
        flags 0010 [MQTT-3.8.1-1], requested QoS 0."""
        if not topics:
            raise ValueError("SUBSCRIBE needs at least one topic filter")
        self._pid = self._pid % 65535 + 1
        body = struct.pack("!H", self._pid) + b"".join(_str(check_filter(t)) + b"\x00" for t in topics)
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
                if qos == 3:  # both QoS bits set: a protocol violation [MQTT-3.3.1-4]
                    self.close()
                    raise MQTTError(M("bad packet from the broker"))
                n = struct.unpack("!H", data[:2])[0]
                try:
                    topic = data[2:2 + n].decode("utf-8")  # ill-formed UTF-8: close the connection [MQTT-1.5.3-1]
                except UnicodeDecodeError:
                    topic = None
                if topic is None or "\x00" in topic:  # [MQTT-1.5.3-2]
                    self.close()
                    raise MQTTError(M("bad packet from the broker"))
                rest = data[2 + n:]
                if qos:
                    pid, rest = rest[:2], rest[2:]
                    # Answer as the QoS says [MQTT-3.3.4-1]: PUBACK for 1, PUBREC for 2 (then PUBCOMP)
                    self._send(_packet(0x40 if qos == 1 else 0x50, pid))
                out.append((topic, rest))
            elif kind & 0xF0 == 0x60:  # PUBREL (QoS 2): complete it, with the same identifier [MQTT-2.3.1-6]
                self._send(_packet(0x70, data[:2]))
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

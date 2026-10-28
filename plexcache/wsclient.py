"""A tiny synchronous WebSocket client (RFC 6455), standard library only.

Just enough for TrueNAS's JSON-RPC API: text frames, ping/pong, close.
"""

import base64
import hashlib
import os
import socket
import ssl
import struct
import urllib.parse

MAX_MESSAGE = 64 * 2**20  # TrueNAS's answers are well under this, even for big pools

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(Exception):
    pass


def encode_frame(opcode: int, payload: bytes, mask: bool = True) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack("!Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class WebSocket:
    def __init__(self, url: str, verify_tls: bool = True, timeout: float = 15):
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("ws", "wss"):
            raise WebSocketError("URL must start with ws:// or wss://")
        port = u.port or (443 if u.scheme == "wss" else 80)
        try:
            raw = socket.create_connection((u.hostname, port), timeout=timeout)
        except OSError as e:
            raise WebSocketError(f"can't connect to {u.hostname}:{port}: {e}") from None
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            if not verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            try:
                raw = ctx.wrap_socket(raw, server_hostname=u.hostname)
            except ssl.SSLError as e:
                raw.close()
                raise WebSocketError(f"TLS failed: {e.reason or e}") from None
            except OSError as e:  # e.g. timed out: something listens there but never answers TLS
                raw.close()
                raise WebSocketError(f"TLS failed: {e}") from None
        self.sock = raw
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        host = u.hostname + (f":{u.port}" if u.port else "")
        req = (
            f"GET {u.path or '/'}{'?' + u.query if u.query else ''} HTTP/1.1\r\n"
            f"Host: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            self.sock.sendall(req.encode())
            head = self._read_until(b"\r\n\r\n").decode("latin-1")
        except (OSError, WebSocketError) as e:
            self.close()
            raise WebSocketError(f"websocket handshake failed: {e}") from None
        status = head.split("\r\n", 1)[0]
        if " 101 " not in status + " ":
            self.close()
            raise WebSocketError(f"server refused websocket upgrade: {status}")
        expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if expect.lower() not in head.lower():
            self.close()
            raise WebSocketError("bad websocket handshake")

    def _recv(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            self._buf += chunk
            if len(self._buf) > 65536:
                raise WebSocketError("handshake too large")
        idx = self._buf.index(marker) + len(marker)
        out, self._buf = self._buf[:idx], self._buf[idx:]
        return out

    def send_text(self, text: str) -> None:
        self.sock.sendall(encode_frame(0x1, text.encode()))

    def recv_text(self) -> str:
        message = b""
        while True:
            b1, b2 = self._recv(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv(8))[0]
            if len(message) + n > MAX_MESSAGE:  # a broken or hostile server can't eat all the RAM
                raise WebSocketError(f"message from the server is too large (over {MAX_MESSAGE // 2**20} MB)")
            mask = self._recv(4) if b2 & 0x80 else None
            payload = self._recv(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping
                self.sock.sendall(encode_frame(0xA, payload))
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise WebSocketError("server closed the connection")
            message += payload
            if fin:
                return message.decode("utf-8")

    def close(self) -> None:
        try:
            self.sock.sendall(encode_frame(0x8, b""))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

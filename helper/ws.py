"""
A minimal RFC 6455 WebSocket client.

Hand-rolled on purpose. The plugin's whole install story is "no pip step"
(verified: stdlib + openssl only, see docs/AUTH.md), and no WebSocket client
ships in the standard library. We need exactly one thing -- send binary and
text frames to Otter's ASR endpoint and read its JSON acks -- so a few hundred
lines beats a dependency.

Scope, deliberately narrow:
  * client role only (so every frame we send is masked, per spec)
  * text + binary data frames, with continuation frames reassembled
  * ping answered with pong; pong and close handled
  * TLS via ssl.create_default_context()

Not implemented: permessage-deflate, subprotocol negotiation, fragmenting our
own sends. Otter needs none of them.
"""

from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import ssl
import struct
import time
import urllib.parse

# opcodes
CONT = 0x0
TEXT = 0x1
BINARY = 0x2
CLOSE = 0x8
PING = 0x9
PONG = 0xA

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(Exception):
    pass


class WebSocketClosed(WebSocketError):
    """The peer closed the connection."""

    def __init__(self, code: int = 1006, reason: str = ""):
        super().__init__(f"websocket closed ({code}) {reason}".strip())
        self.code = code
        self.reason = reason


class WebSocket:
    """A blocking WebSocket client connection."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""
        self._frag: list[bytes] = []
        self._frag_opcode: int | None = None
        self.closed = False

    # ------------------------------------------------------------- connect

    @classmethod
    def connect(cls, url: str, headers: dict[str, str] | None = None,
                timeout: float = 20.0) -> "WebSocket":
        parts = urllib.parse.urlsplit(url)
        secure = parts.scheme in ("wss", "https")
        if parts.scheme not in ("ws", "wss", "http", "https"):
            raise WebSocketError(f"not a websocket url: {url!r}")
        host = parts.hostname
        if not host:
            raise WebSocketError(f"no host in url: {url!r}")
        port = parts.port or (443 if secure else 80)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query

        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            if secure:
                ctx = ssl.create_default_context()
                raw = ctx.wrap_socket(raw, server_hostname=host)

            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = [
                f"GET {target} HTTP/1.1",
                f"Host: {host}" + ("" if port in (80, 443) else f":{port}"),
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            for name, value in (headers or {}).items():
                request.append(f"{name}: {value}")
            raw.sendall(("\r\n".join(request) + "\r\n\r\n").encode("utf8"))

            ws = cls(raw)
            ws._handshake(key)
            return ws
        except Exception:
            try:
                raw.close()
            except OSError:
                pass
            raise

    def _handshake(self, key: str) -> None:
        # Read headers only; anything after the blank line is frame data.
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest

        lines = head.decode("latin1").split("\r\n")
        if not lines or "101" not in lines[0]:
            raise WebSocketError(f"handshake rejected: {lines[0] if lines else '<empty>'}")

        got = ""
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                got = value.strip()
        want = base64.b64encode(
            hashlib.sha1((key + GUID).encode("ascii")).digest()
        ).decode("ascii")
        if got != want:
            raise WebSocketError("handshake accept key mismatch")

    # ---------------------------------------------------------------- send

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise WebSocketClosed()
        length = len(payload)
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode
        # Client frames are always masked.
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        try:
            self._sock.sendall(bytes(header) + masked)
        except OSError as e:
            self.closed = True
            raise WebSocketClosed(1006, str(e))

    def send_text(self, text: str) -> None:
        self._send_frame(TEXT, text.encode("utf8"))

    def send_binary(self, data: bytes) -> None:
        self._send_frame(BINARY, data)

    def ping(self, data: bytes = b"") -> None:
        self._send_frame(PING, data)

    # ---------------------------------------------------------------- recv

    # Frames are parsed out of a buffer rather than read field-by-field off
    # the socket. In a select() loop a read can return a partial frame at any
    # moment; a field-by-field reader would consume the header, hit the short
    # read, and lose its place in the stream. Parsing only when a whole frame
    # is buffered makes a partial read a no-op instead of corruption.
    @staticmethod
    def _parse_frame(buf: bytes):
        """(consumed, fin, opcode, payload) once a full frame is buffered."""
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < offset + 2:
                return None
            (length,) = struct.unpack("!H", buf[offset:offset + 2])
            offset += 2
        elif length == 127:
            if len(buf) < offset + 8:
                return None
            (length,) = struct.unpack("!Q", buf[offset:offset + 8])
            offset += 8
        mask = b""
        if masked:
            if len(buf) < offset + 4:
                return None
            mask = buf[offset:offset + 4]
            offset += 4
        if len(buf) < offset + length:
            return None
        payload = buf[offset:offset + length]
        if masked:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return offset + length, fin, opcode, payload

    def _fill(self, timeout: float | None) -> bool:
        """Wait up to `timeout` for bytes and append them. False on timeout."""
        # An SSL socket can hold already-decrypted bytes that select() will
        # never report, because they are in the SSL layer's buffer rather than
        # the kernel's. Always drain that first or we can block on a timeout
        # while a complete frame is sitting in userspace.
        pending = 0
        if isinstance(self._sock, ssl.SSLSocket):
            try:
                pending = self._sock.pending()
            except (OSError, ValueError):
                pending = 0
        if not pending:
            try:
                ready, _, _ = select.select([self._sock], [], [], timeout)
            except (OSError, ValueError) as e:
                self.closed = True
                raise WebSocketClosed(1006, str(e))
            if not ready:
                return False
        try:
            chunk = self._sock.recv(65536)
        except ssl.SSLWantReadError:
            return False
        except OSError as e:
            self.closed = True
            raise WebSocketClosed(1006, str(e))
        if not chunk:
            self.closed = True
            raise WebSocketClosed(1006, "eof")
        self._buf += chunk
        return True

    def _handle_control(self, opcode: int, payload: bytes) -> None:
        if opcode == PING:
            self._send_frame(PONG, payload)
        elif opcode == CLOSE:
            code, reason = 1005, ""
            if len(payload) >= 2:
                (code,) = struct.unpack("!H", payload[:2])
                reason = payload[2:].decode("utf8", "replace")
            self.closed = True
            try:
                self._send_frame(CLOSE, payload[:2])
            except Exception:
                pass
            raise WebSocketClosed(code, reason)

    def recv(self, timeout: float | None = None) -> tuple[int, bytes] | None:
        """Next data frame as (opcode, payload), or None if none arrived.

        Control frames are handled internally and never surface.
        Continuation frames are reassembled. Raises WebSocketClosed when the
        peer goes away.
        """
        deadline = None if timeout is None else (time.monotonic() + timeout)
        polled = False
        while True:
            # Drain everything already buffered before waiting on the socket.
            while True:
                parsed = self._parse_frame(self._buf)
                if parsed is None:
                    break
                consumed, fin, opcode, payload = parsed
                self._buf = self._buf[consumed:]
                if opcode in (PING, PONG, CLOSE):
                    self._handle_control(opcode, payload)
                    continue
                if opcode in (TEXT, BINARY):
                    self._frag_opcode = opcode
                    self._frag = [payload]
                elif opcode == CONT:
                    self._frag.append(payload)
                if fin and self._frag_opcode is not None:
                    op, data = self._frag_opcode, b"".join(self._frag)
                    self._frag_opcode, self._frag = None, []
                    return op, data

            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # `timeout=0` means "poll once", not "do nothing", and by
                    # the time we get here even a generous deadline can have
                    # just elapsed. Returning without ever calling _fill left
                    # the buffer empty forever, so no upload ack was ever
                    # read and resume-from-offset was silently dead.
                    if polled:
                        return None
                    remaining = 0.0
            polled = True
            if not self._fill(remaining):
                return None

    # --------------------------------------------------------------- close

    def close(self, code: int = 1000) -> None:
        if not self.closed:
            try:
                self._send_frame(CLOSE, struct.pack("!H", code))
            except Exception:
                pass
            self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def fileno(self) -> int:
        return self._sock.fileno()

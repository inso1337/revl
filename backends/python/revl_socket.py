"""The accepted-socket host engine behind `stdlib/socket.rvl` (issue #3357).

`stdlib/socket.rvl`'s externs reach this module through one-line `@py` bodies;
a user-origin body importing it is refused with a hint naming the stdlib module
(`_STDLIB_BACKEND_DOORS` in src/revl/compiler.py). It is standard library only:
the HTTP upgrade handshake and RFC 6455 text framing are written here, with no
extensions and no subprotocols, because the standard library has neither.

One `Listener` owns a listening socket and one selector thread that accepts,
reads and writes every line. The program's calls (`take`, `say`, `close`) only
touch shared state under one lock and wake the thread, so none of them blocks
on the network.

What the program sees is a list of plain dicts, one per knock, that
`stdlib/socket.rvl` maps onto `stdlib/wire.rvl`'s `Knock`:

    {"seq": n, "kind": "opened" | "said" | "shut", "why": <why_str> | "",
     "line": n, "who": <Origin> | "", "text": <message or reason>}

Close codes this module sends on its own: 1009 for a message over `most`, 1003
for a binary frame, 1002 for a protocol violation (an unmasked client frame, a
reserved bit or opcode, a bad fragment or control frame) and 1007 for a text
message that is not UTF-8. Each of those shuts the line `Refused`, apart from
1009, which is `TooLarge`. A close the program asks for carries the code
`stdlib/socket.rvl`'s `close_code` chose.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import selectors
import socket
import struct
import threading
import time

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEAD = 16384          # bytes of request head before it is refused 431
_DRAIN_S = 2.0             # how long a shut line is read (and discarded) for
_TICK_S = 0.25             # selector timeout, for drain deadlines and shut

CODE_CLOSED = 1000
CODE_GONE = 1001
CODE_PROTOCOL = 1002
CODE_BINARY = 1003
CODE_NOT_UTF8 = 1007
CODE_TOO_LARGE = 1009


def _http(status: str, extra: str = "") -> bytes:
    return (f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n"
            f"{extra}\r\n").encode("ascii")


def _frame(opcode: int, payload: bytes) -> bytes:
    """One unmasked server frame, FIN set."""
    n = len(payload)
    if n < 126:
        head = struct.pack(">BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack(">BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack(">BBQ", 0x80 | opcode, 127, n)
    return head + payload


def _close_payload(code: int, text: str) -> bytes:
    reason = text.encode("utf-8")[:123]
    # never cut a UTF-8 sequence in two: the peer would refuse the reason
    reason = reason.decode("utf-8", "ignore").encode("utf-8")
    return struct.pack(">H", code) + reason


def _unmask(mask: bytes, data: bytes) -> bytes:
    n = len(data)
    if n == 0:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


class _Line:
    """One accepted connection, from its request head to its shut."""

    __slots__ = ("sock", "num", "phase", "inbuf", "out", "frag", "frag_op",
                 "eof", "flush_then_shut", "drain_until")

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.num = 0             # line number, assigned at the upgrade
        self.phase = "head"      # head -> open -> shutting -> gone
        self.inbuf = bytearray()
        self.out = bytearray()
        self.frag = bytearray()
        self.frag_op = 0         # 0: no fragmented message in progress
        self.eof = False
        self.flush_then_shut = False
        self.drain_until = 0.0   # set once the last byte is written


class Listener:
    """A listening socket, its lines and the knocks waiting for `take`."""

    def __init__(self, door: dict):
        self.most = int(door["most"])
        self.held = int(door["held"])
        self.limit = int(door["limit"])
        self.origins = tuple(door["origins"])
        self.bare = bool(door["bare"])
        for name in ("most", "held", "limit"):
            if getattr(self, name) < 1:
                raise ValueError(f"socket door `{name}` must be at least 1, "
                                 f"got {getattr(self, name)}")
        # a bind failure raises here, inside the acquire, so the row's
        # activation fails with the OSError that names the address
        self._server = socket.create_server((door["address"], int(door["port"])),
                                            backlog=128)
        self._server.setblocking(False)
        self.port = self._server.getsockname()[1]
        self._lock = threading.Lock()
        self._knocks: collections.deque = collections.deque()
        self._seq = 0
        self._next = 0
        self._lines: dict[int, _Line] = {}      # open lines, by number
        self._conns: dict[socket.socket, _Line] = {}
        self._stopping = False
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._wake_r, selectors.EVENT_READ, None)
        self._accepting = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"revl-socket-{self.port}")
        self._thread.start()

    # -------------------------------------------------------- the program's side

    def take(self, most: int) -> list[dict]:
        with self._lock:
            out = []
            while self._knocks and len(out) < most:
                out.append(self._knocks.popleft())
        self._wake()
        return out

    def say(self, line: int, text: str) -> bool:
        with self._lock:
            conn = self._lines.get(line)
            if conn is None or conn.phase != "open" or len(conn.out) > self.limit:
                return False
            conn.out += _frame(0x1, text.encode("utf-8"))
        self._wake()
        return True

    def close(self, line: int, code: int, why: str, text: str) -> bool:
        with self._lock:
            conn = self._lines.get(line)
            if conn is None or conn.phase != "open":
                return False
            self._shut(conn, why, code, text)
        self._wake()
        return True

    def shut(self) -> None:
        with self._lock:
            self._stopping = True
        self._wake()
        self._thread.join(timeout=10)

    # ------------------------------------------------------------- shared state

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"x")
        except (BlockingIOError, OSError):
            pass  # a full wake pipe already wakes the thread

    def _knock(self, kind: str, why: str, conn: _Line, who: str, text: str) -> None:
        self._seq += 1
        self._knocks.append({"seq": self._seq, "kind": kind, "why": why,
                             "line": conn.num, "who": who, "text": text})

    def _full(self) -> bool:
        return len(self._knocks) >= self.held

    def _shut(self, conn: _Line, why: str, code: int, text: str) -> None:
        """End an open line from this side: close frame, then a `Shut` knock."""
        conn.out += _frame(0x8, _close_payload(code, text))
        conn.phase = "shutting"
        conn.flush_then_shut = True
        conn.inbuf.clear()
        self._lines.pop(conn.num, None)
        self._knock("shut", why, conn, "", text)

    def _refuse(self, conn: _Line, status: str, extra: str = "") -> None:
        conn.out += _http(status, extra)
        conn.phase = "shutting"
        conn.flush_then_shut = True
        conn.inbuf.clear()

    # --------------------------------------------------------- the thread's side

    def _run(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._stopping:
                        break
                    self._interest()
                for key, mask in self._sel.select(timeout=_TICK_S):
                    with self._lock:
                        self._event(key, mask)
                with self._lock:
                    self._resume()
                    self._expire()
        finally:
            with self._lock:
                self._teardown()

    def _interest(self) -> None:
        paused = self._full()
        want_accept = not paused
        if want_accept != self._accepting:
            if want_accept:
                self._sel.register(self._server, selectors.EVENT_READ, "accept")
            else:
                self._sel.unregister(self._server)
            self._accepting = want_accept
        for conn in list(self._conns.values()):
            events = 0
            reading = conn.phase == "shutting" or not paused
            if reading and not conn.eof:
                events |= selectors.EVENT_READ
            if conn.out:
                events |= selectors.EVENT_WRITE
            try:
                current = self._sel.get_key(conn.sock).events
            except KeyError:
                current = 0
            if events == current:
                continue
            if current and events:
                self._sel.modify(conn.sock, events, conn)
            elif events:
                self._sel.register(conn.sock, events, conn)
            else:
                self._sel.unregister(conn.sock)

    def _event(self, key, mask) -> None:
        if key.data is None:
            try:
                while self._wake_r.recv(4096):
                    pass
            except (BlockingIOError, OSError):
                pass
            return
        if key.data == "accept":
            self._accept()
            return
        conn = key.data
        if conn.sock not in self._conns:
            return
        if mask & selectors.EVENT_WRITE:
            self._write(conn)
        if mask & selectors.EVENT_READ and conn.sock in self._conns:
            self._read(conn)

    def _accept(self) -> None:
        while True:
            try:
                sock, _addr = self._server.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            sock.setblocking(False)
            self._conns[sock] = _Line(sock)

    def _write(self, conn: _Line) -> None:
        try:
            sent = conn.sock.send(conn.out)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(conn)
            return
        del conn.out[:sent]
        if not conn.out and conn.flush_then_shut and not conn.drain_until:
            try:
                conn.sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            conn.drain_until = time.monotonic() + _DRAIN_S

    def _read(self, conn: _Line) -> None:
        try:
            data = conn.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if conn.phase == "shutting":
            if not data:
                self._drop(conn)
            return
        if data:
            conn.inbuf += data
        else:
            conn.eof = True
        self._parse(conn)

    def _resume(self) -> None:
        """Parse what a pause left buffered, once `take` made room."""
        for conn in list(self._conns.values()):
            if conn.phase == "open" and (conn.inbuf or conn.eof) and not self._full():
                self._parse(conn)

    def _expire(self) -> None:
        now = time.monotonic()
        for conn in list(self._conns.values()):
            if conn.drain_until and now >= conn.drain_until:
                self._drop(conn)

    def _drop(self, conn: _Line) -> None:
        self._conns.pop(conn.sock, None)
        try:
            self._sel.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        try:
            conn.sock.close()
        except OSError:
            pass
        conn.phase = "gone"

    def _teardown(self) -> None:
        for conn in list(self._conns.values()):
            if conn.phase == "open":
                try:
                    conn.sock.send(bytes(conn.out)
                                   + _frame(0x8, _close_payload(CODE_GONE, "")))
                except OSError:
                    pass
            self._drop(conn)
        self._lines.clear()
        for sock in (self._server, self._wake_r, self._wake_w):
            try:
                sock.close()
            except OSError:
                pass
        self._sel.close()

    # ---------------------------------------------------------- the request head

    def _parse(self, conn: _Line) -> None:
        if conn.phase == "head":
            self._head(conn)
        if conn.phase == "open":
            self._frames(conn)
        if conn.phase == "open" and conn.eof and not conn.inbuf:
            # the peer went away without a close frame
            self._lines.pop(conn.num, None)
            self._knock("shut", "host_gone", conn, "", "")
            self._drop(conn)
        elif conn.phase == "head" and conn.eof:
            self._drop(conn)

    def _head(self, conn: _Line) -> None:
        end = conn.inbuf.find(b"\r\n\r\n")
        if end < 0:
            if len(conn.inbuf) > _MAX_HEAD:
                self._refuse(conn, "431 Request Header Fields Too Large")
            return
        head = bytes(conn.inbuf[:end]).decode("latin-1")
        del conn.inbuf[:end + 4]
        lines = head.split("\r\n")
        parts = lines[0].split(" ")
        headers: dict[str, list[str]] = {}
        for raw in lines[1:]:
            name, sep, value = raw.partition(":")
            if not sep:
                self._refuse(conn, "400 Bad Request")
                return
            headers.setdefault(name.strip().lower(), []).append(value.strip())

        def tokens(name: str) -> set[str]:
            return {t.strip().lower() for v in headers.get(name, [])
                    for t in v.split(",")}

        upgrade = (len(parts) == 3 and parts[0] == "GET"
                   and "websocket" in tokens("upgrade")
                   and "upgrade" in tokens("connection"))
        if not upgrade:
            self._refuse(conn, "404 Not Found")
            return
        if headers.get("sec-websocket-version") != ["13"]:
            self._refuse(conn, "426 Upgrade Required", "Sec-WebSocket-Version: 13\r\n")
            return
        keys = headers.get("sec-websocket-key", [])
        try:
            valid = len(keys) == 1 and len(base64.b64decode(keys[0], validate=True)) == 16
        except ValueError:
            valid = False
        if not valid:
            self._refuse(conn, "400 Bad Request")
            return
        origins = headers.get("origin", [])
        if len(origins) > 1 or (not origins and not self.bare) or (
                origins and origins[0] not in self.origins):
            self._refuse(conn, "403 Forbidden")
            return
        accept = base64.b64encode(hashlib.sha1(keys[0].encode("ascii") + _GUID)
                                  .digest()).decode("ascii")
        conn.out += (f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                     ).encode("ascii")
        self._next += 1
        conn.num = self._next
        conn.phase = "open"
        self._lines[conn.num] = conn
        self._knock("opened", "", conn, origins[0] if origins else "", "")

    # ------------------------------------------------------------------ frames

    def _frames(self, conn: _Line) -> None:
        buf = conn.inbuf
        while conn.phase == "open" and not self._full() and len(buf) >= 2:
            b0, b1 = buf[0], buf[1]
            fin, rsv, op = b0 & 0x80, b0 & 0x70, b0 & 0x0F
            n, off = b1 & 0x7F, 2
            if n == 126:
                if len(buf) < 4:
                    return
                n, off = struct.unpack(">H", buf[2:4])[0], 4
            elif n == 127:
                if len(buf) < 10:
                    return
                n, off = struct.unpack(">Q", buf[2:10])[0], 10
            if rsv or not b1 & 0x80:
                self._shut(conn, "refused", CODE_PROTOCOL, "protocol error")
                return
            if op >= 0x8:
                if not fin or n > 125 or op > 0xA:
                    self._shut(conn, "refused", CODE_PROTOCOL, "protocol error")
                    return
            elif op == 0x2 or (op == 0x0 and conn.frag_op == 0x2):
                self._shut(conn, "refused", CODE_BINARY, "binary frames are refused")
                return
            elif op not in (0x0, 0x1) or (op == 0x0) != bool(conn.frag_op):
                self._shut(conn, "refused", CODE_PROTOCOL, "protocol error")
                return
            elif len(conn.frag) + n > self.most:
                self._shut(conn, "too_large", CODE_TOO_LARGE,
                           f"message over {self.most} bytes")
                return
            if len(buf) < off + 4 + n:
                return
            payload = _unmask(bytes(buf[off:off + 4]), bytes(buf[off + 4:off + 4 + n]))
            del buf[:off + 4 + n]
            if op == 0x8:
                self._peer_close(conn, payload)
            elif op == 0x9:
                conn.out += _frame(0xA, payload)
            elif op == 0xA:
                pass
            else:
                conn.frag += payload
                conn.frag_op = 0x1
                if fin:
                    self._said(conn)

    def _said(self, conn: _Line) -> None:
        data = bytes(conn.frag)
        conn.frag.clear()
        conn.frag_op = 0
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._shut(conn, "refused", CODE_NOT_UTF8, "text is not UTF-8")
            return
        self._knock("said", "", conn, "", text)

    def _peer_close(self, conn: _Line, payload: bytes) -> None:
        code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else CODE_CLOSED
        reason = payload[2:].decode("utf-8", "replace")
        conn.out += _frame(0x8, payload[:2])
        conn.phase = "shutting"
        conn.flush_then_shut = True
        conn.inbuf.clear()
        self._lines.pop(conn.num, None)
        self._knock("shut", "host_gone" if code == CODE_GONE else "closed",
                    conn, "", reason)


# ---------------------------------------------------- the extern entry points

def listen(door: dict) -> Listener:
    return Listener(door)


def shut(listener: Listener) -> None:
    listener.shut()


def take(listener: Listener, most: int) -> list[dict]:
    return listener.take(int(most))


def say(listener: Listener, line: int, text: str) -> bool:
    return listener.say(int(line), text)


def close(listener: Listener, line: int, code: int, why: str, text: str) -> bool:
    return listener.close(int(line), int(code), why, text)

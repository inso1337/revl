"""The stdlib WIRE contract and its accepted-socket provider (issue #3357).

`stdlib/wire.rvl` is the contract a program that serves live clients is written
against: `Knock` (`seq`, `kind: Opened | Said | Shut(Why)`, `line`, `who`,
`text`) and the service `Wire` with `take`, `say`, `close`, each declaring one
fixed token, `wire.take`, `wire.say`, `wire.close`. `stdlib/socket.rvl`'s
`SocketWire` provides it on the py tier over `backends/python/revl_socket.py`,
which is standard library only (the RFC 6455 handshake and framing are written
there). What this suite pins:

  * the three events arrive in order on one line, `who` carrying the Origin;
  * every limit is the provider's config: the origin rule answers 403 before
    the upgrade, `most` shuts `TooLarge` with 1009, `held` stops reading and
    drops nothing, `limit` makes `say` skip a line that does not read;
  * `close(line, Refused, "banned")` reaches the client as 1008 and the text, a
    binary frame shuts `Refused` with 1003, a plain request is 404;
  * the listener is `acquire`: unloading releases the port, and a bind failure
    fails the activation by name;
  * `revl audit` names the five crossings, the three verbs with the contract's
    tokens;
  * a provider declaring its own token under `Wire` is refused (the seam a
    page-to-page provider rests on), and a user body importing the engine is
    refused with a hint naming `stdlib/socket.rvl`.

Every test fails on the base commit: `stdlib/wire.rvl` and `stdlib/socket.rvl`
do not exist there, so the module-level compile raises.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

STDLIB = ROOT / "stdlib"
SOCKET = STDLIB / "socket.rvl"
ORIGIN = "http://play.test"

IR = compile_files([str(SOCKET)])

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the provider runs in a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)


# ---------------------------------------------------------------------------
# a standard-library websocket client
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _read_head(sock: socket.socket) -> tuple[str, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head.decode("latin-1").split("\r\n")[0], rest


class Client:
    def __init__(self, port: int, origin: str | None = ORIGIN,
                 origins: tuple[str, ...] = ()):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        head = (f"GET /wire HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n")
        for each in ((origin,) if origin is not None else ()) + origins:
            head += f"Origin: {each}\r\n"
        self.sock.sendall((head + "\r\n").encode("ascii"))
        self.status, self.buf = _read_head(self.sock)

    def send(self, payload: bytes | str, opcode: int = 0x1) -> None:
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            head = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(head + mask + body)

    def _need(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed before a whole frame")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> tuple[int, bytes]:
        b0, b1 = self._need(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._need(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._need(8))[0]
        return b0 & 0x0F, self._need(n)

    def close_frame(self) -> tuple[int, str]:
        """Read until the server's close frame; its code and reason."""
        while True:
            opcode, payload = self.recv()
            if opcode == 0x8:
                return struct.unpack(">H", payload[:2])[0], payload[2:].decode()

    def bye(self, code: int = 1000) -> None:
        self.send(struct.pack(">H", code), opcode=0x8)

    def shut(self) -> None:
        self.sock.close()


def _request(port: int, raw: str) -> str:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(raw.encode("ascii"))
        return _read_head(sock)[0]


# ---------------------------------------------------------------------------
# the provider in a live session
# ---------------------------------------------------------------------------

class Wire:
    def __init__(self, **config):
        from revl.mcp.session import Session
        self.port = config.setdefault("port", _free_port())
        config.setdefault("origins", [ORIGIN])
        self.session = Session()
        self.loaded = self.session.load(copy.deepcopy(IR), {"SocketWire": config})

    def call(self, method: str, *args):
        return self.session.call("wire", method, list(args), raw=True)["result"]

    def take(self, most: int = 100) -> list[tuple]:
        return [_plain(k) for k in self.call("take", most)]

    def until(self, count: int, most: int = 100, seconds: float = 5.0) -> list[tuple]:
        got: list[tuple] = []
        deadline = time.monotonic() + seconds
        while len(got) < count and time.monotonic() < deadline:
            got += self.take(most)
            if len(got) < count:
                time.sleep(0.02)
        return got

    def why(self, name: str):
        """A `Why` value of the running program, built from its own module."""
        provided = self.session._driver._namespace()["wire"]
        return getattr(sys.modules[type(provided).__module__], name)()

    def unload(self) -> None:
        self.session.unload()


def _plain(knock) -> tuple:
    kind = type(knock["kind"]).__name__
    if kind == "Shut":
        kind = f"Shut({type(knock['kind'].value).__name__})"
    return knock["seq"], kind, knock["line"], knock["who"], knock["text"]


@pytest.fixture
def wire_factory():
    made: list[Wire] = []

    def make(**config) -> Wire:
        wire = Wire(**config)
        made.append(wire)
        return wire

    yield make
    for wire in made:
        if wire.session.loaded:
            wire.unload()


# ---------------------------------------------------------------------------
# the three events, in order
# ---------------------------------------------------------------------------

@needs_cordis
def test_opened_said_shut_arrive_in_order(wire_factory):
    wire = wire_factory()
    client = Client(wire.port)
    assert client.status == "HTTP/1.1 101 Switching Protocols"
    client.send("hi")
    client.bye()
    assert client.close_frame()[0] == 1000
    knocks = wire.until(3)
    assert [k[1] for k in knocks] == ["Opened", "Said", "Shut(Closed)"]
    assert len({k[2] for k in knocks}) == 1
    seqs = [k[0] for k in knocks]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    assert knocks[0][3] == ORIGIN       # `who` is the Origin on Opened
    assert knocks[1][3] == "" and knocks[1][4] == "hi"


@needs_cordis
def test_origin_not_listed_is_refused_before_upgrade(wire_factory):
    wire = wire_factory()
    assert Client(wire.port, origin="http://evil.test").status == "HTTP/1.1 403 Forbidden"
    assert Client(wire.port, origin=None).status == "HTTP/1.1 403 Forbidden"
    two = Client(wire.port, origins=(ORIGIN,))
    assert two.status == "HTTP/1.1 403 Forbidden"
    time.sleep(0.2)
    assert wire.take() == []            # a refused upgrade makes no knock

    bare = wire_factory(bare=True)
    client = Client(bare.port, origin=None)
    assert client.status == "HTTP/1.1 101 Switching Protocols"
    (opened,) = bare.until(1)
    assert opened[1] == "Opened" and opened[3] == ""


@needs_cordis
def test_message_over_most_closes_1009(wire_factory):
    wire = wire_factory(most=16)
    client = Client(wire.port)
    client.send("x" * 17)
    assert client.close_frame()[0] == 1009
    knocks = wire.until(2)
    assert [k[1] for k in knocks] == ["Opened", "Shut(TooLarge)"]


@needs_cordis
def test_held_stops_reading_and_drops_nothing(wire_factory):
    held = 4
    wire = wire_factory(held=held)
    client = Client(wire.port)
    for i in range(held + 5):
        client.send(f"m{i}")
    time.sleep(0.5)
    first = wire.take()
    assert len(first) == held           # Opened plus held - 1 messages
    rest = wire.until(held + 6 - len(first))
    said = [k[4] for k in first + rest if k[1] == "Said"]
    assert said == [f"m{i}" for i in range(held + 5)]


@needs_cordis
def test_say_skips_a_line_over_limit(wire_factory):
    wire = wire_factory(limit=1024)
    stalled = Client(wire.port)         # never reads
    reading = Client(wire.port)
    lines = [k[2] for k in wire.until(2)]
    big = "y" * 65536
    answers = []
    for _ in range(400):
        answers.append(wire.call("say", lines[0], big))
        if not answers[-1]:
            break
        time.sleep(0.005)
    assert answers[0] is True and answers[-1] is False
    assert wire.call("say", lines[1], "still here") is True
    assert reading.recv() == (0x1, b"still here")
    stalled.shut()


@needs_cordis
def test_close_shuts_with_code_and_reason(wire_factory):
    wire = wire_factory()
    client = Client(wire.port)
    (opened,) = wire.until(1)
    refused = wire.why("Refused")
    assert wire.call("close", opened[2], refused, "banned") is True
    assert client.close_frame() == (1008, "banned")
    (shut,) = wire.until(1)
    assert shut[1:] == ("Shut(Refused)", opened[2], "", "banned")
    assert wire.call("close", opened[2], refused, "again") is False
    assert wire.call("say", opened[2], "late") is False


@needs_cordis
def test_binary_frame_closes_1003(wire_factory):
    wire = wire_factory()
    client = Client(wire.port)
    client.send(b"\x00\x01", opcode=0x2)
    assert client.close_frame()[0] == 1003
    assert [k[1] for k in wire.until(2)] == ["Opened", "Shut(Refused)"]


@needs_cordis
def test_non_upgrade_request_is_404(wire_factory):
    wire = wire_factory()
    status = _request(wire.port, "GET /index.html HTTP/1.1\r\nHost: x\r\n\r\n")
    assert status == "HTTP/1.1 404 Not Found"
    time.sleep(0.2)
    assert wire.take() == []


@needs_cordis
def test_peer_gone_without_close_is_host_gone(wire_factory):
    wire = wire_factory()
    client = Client(wire.port)
    client.send("hi")
    client.shut()
    assert [k[1] for k in wire.until(3)] == ["Opened", "Said", "Shut(HostGone)"]


# ---------------------------------------------------------------------------
# the listener is an acquisition
# ---------------------------------------------------------------------------

@needs_cordis
def test_unload_releases_the_port(wire_factory):
    wire = wire_factory()
    client = Client(wire.port)
    wire.until(1)
    # the server closes its lines first, so their TIME_WAIT sits on its port:
    # rebind the way a server does (SO_REUSEADDR, as `create_server` sets),
    # and check that this bind is refused while the listener lives
    with pytest.raises(OSError):
        _rebind(wire.port)
    wire.unload()
    assert client.close_frame()[0] == 1001
    client.shut()
    _rebind(wire.port)


def _rebind(port: int) -> None:
    with socket.socket() as again:
        again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        again.bind(("127.0.0.1", port))
        again.listen()


@needs_cordis
def test_bind_failure_fails_the_activation_by_name(wire_factory):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        wire = wire_factory(port=taken.getsockname()[1])
        (row,) = wire.loaded["components"]
        assert row["name"] == "SocketWire"
        assert row["state"] == "FAILED"
        assert row["error"]["type"] == "OSError"
        assert "in use" in row["error"]["message"]


# ---------------------------------------------------------------------------
# the audit surface and the contract's tokens
# ---------------------------------------------------------------------------

GAME = '''\
use "stdlib/wire.rvl" { Wire, Knock, Kind, Why }

service Beat { emission fn tick() -> Int }

component Game requires wire: Wire provides beat: Beat {
  provide beat {
    fn tick() {
      let knocks = emit wire.take(512)
      var heard = 0
      for (k of knocks) {
        let ok = match k.kind {
          Said => emit wire.say(k.line, k.text),
          Opened => true,
          Shut(_) => false,
        }
        if (ok) { heard = heard + 1 }
        if (k.text == "bye") { let gone = emit wire.close(k.line, Refused, "banned") }
      }
      return heard
    }
  }
}
'''

CROSSINGS = {
    "socket_take": ("emission", ["wire.take"]),
    "socket_say": ("emission", ["wire.say"]),
    "socket_close": ("emission", ["wire.close"]),
    "socket_listen": ("acquire", []),
    "socket_shut": ("acquire", []),
}


def test_audit_names_the_crossings(tmp_path, capsys):
    game = tmp_path / "game.rvl"
    game.write_text(GAME, encoding="utf-8")
    assert main(["audit", str(game), str(SOCKET), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    externs = {row["name"]: row for row in document["boundary"]["SocketWire"]["externs"]}
    for name, (klass, caps) in CROSSINGS.items():
        assert externs[name]["class"] == klass, name
        assert externs[name].get("capabilities", []) == caps, name
        assert externs[name]["backends"] == ["py"], name

    assert main(["audit", str(game), str(SOCKET)]) == 0
    text = capsys.readouterr().out
    for line in ("socket_take [wire.take] (emission, py)",
                 "socket_say [wire.say] (emission, py)",
                 "socket_close [wire.close] (emission, py)",
                 "socket_listen (acquire, py)",
                 "socket_shut (acquire, py)"):
        assert line in text, line
    assert "capabilities: wire.close, wire.say, wire.take" in text


def test_provider_token_outside_wire_is_refused(tmp_path):
    source = tmp_path / "mine.rvl"
    source.write_text('''\
use "stdlib/wire.rvl" { Wire, Knock, Why }

type Handle = Opaque

extern emission[socket.take] fn my_take(most: Int) -> List[Knock]
  = @py { return [] }
extern emission[wire.say] fn my_say(line: Int, text: Str) -> Bool
  = @py { return False }
extern emission[wire.close] fn my_close(line: Int, text: Str) -> Bool
  = @py { return False }

component MyWire provides wire: Wire {
  provide wire {
    fn take(most) = emit my_take(most)
    fn say(line, text) = emit my_say(line, text)
    fn close(line, why, text) = emit my_close(line, text)
  }
}
''', encoding="utf-8")
    with pytest.raises(RevlError) as caught:
        compile_files([str(source)])
    message = str(caught.value)
    assert "`Wire.take` is declared `emission[wire.take]`" in message, message
    assert "emits through `socket.take`" in message, message
    assert caught.value.code == "G4"


def test_user_body_import_is_refused_naming_the_module():
    with pytest.raises(RevlError) as caught:
        compile_source('''\
extern emission fn grab(port: Int) -> Int
  = @py { from revl_socket import listen; return 0 }
''', "user_socket.rvl")
    assert "revl_socket" in str(caught.value)
    assert 'stdlib/socket.rvl' in (caught.value.hint or "")


def test_the_engine_is_a_trusted_stdlib_host_module():
    from revl.stdlib_hosts import STDLIB_HOST_MODULES
    assert "revl_socket" in STDLIB_HOST_MODULES
    assert (ROOT / "backends" / "python" / "revl_socket.py").is_file()


def test_close_code_table_matches_the_engine():
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import revl_socket
    table = {line.split("=>")[0].strip(): int(line.split("=>")[1].strip(" ,"))
             for line in SOCKET.read_text().split("pub fn close_code")[1]
             .split("}")[0].splitlines() if "=>" in line}
    assert table["Closed"] == revl_socket.CODE_CLOSED
    assert table["TooLarge"] == revl_socket.CODE_TOO_LARGE
    assert table["HostGone"] == revl_socket.CODE_GONE


# ---------------------------------------------------------------------------
# the engine's own bounds (review of the first slice)
# ---------------------------------------------------------------------------

def _engine():
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import revl_socket
    return revl_socket


def _listener(wire: Wire):
    """The live engine object behind `wire`, found by its port."""
    import gc
    engine = _engine()
    (found,) = [o for o in gc.get_objects()
                if isinstance(o, engine.Listener) and o.port == wire.port]
    return found


def _raw(port: int, payload: bytes, timeout: float = 5.0) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.sendall(payload)
    return sock


def _upgrade(extra: str = "", version: str = "13",
             key: str | None = None) -> bytes:
    key = key if key is not None else base64.b64encode(os.urandom(16)).decode()
    return (f"GET /wire HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: {version}\r\nOrigin: {ORIGIN}\r\n"
            f"{extra}\r\n").encode("ascii")


@needs_cordis
def test_partial_frame_then_eof_shuts_host_gone(wire_factory):
    wire = wire_factory()
    client = Client(wire.port)
    client.sock.sendall(b"\x81\x85\x00")   # a text frame header, cut short
    client.shut()
    assert [k[1] for k in wire.until(2)] == ["Opened", "Shut(HostGone)"]


@needs_cordis
def test_ping_flood_from_a_reader_that_never_reads_shuts_too_slow(wire_factory):
    wire = wire_factory(limit=1024)
    client = Client(wire.port)
    ping = bytes([0x89, 0x80 | 125]) + b"\x00" * 4 + b"p" * 125
    try:
        client.sock.sendall(ping * 40000)
    except OSError:
        pass                            # the server shut the line mid-flood
    knocks = wire.until(2, seconds=10)
    assert [k[1] for k in knocks] == ["Opened", "Shut(TooSlow)"]


@needs_cordis
def test_write_failure_on_an_open_line_shuts_host_gone(wire_factory):
    wire = wire_factory(held=1)         # Opened fills the queue: reading stops
    client = Client(wire.port)
    time.sleep(0.2)
    client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                           struct.pack("ii", 1, 0))
    client.shut()                       # a reset, not a close
    time.sleep(0.2)
    line = 1
    answers = []
    for _ in range(20):
        answers.append(wire.call("say", line, "x" * 1000))
        if not answers[-1]:
            break
        time.sleep(0.05)
    assert answers[-1] is False
    assert [k[1] for k in wire.until(2)] == ["Opened", "Shut(HostGone)"]


@needs_cordis
def test_a_shutting_line_whose_client_never_reads_is_dropped(wire_factory, monkeypatch):
    monkeypatch.setattr(_engine(), "_DRAIN_S", 0.3)
    wire = wire_factory(limit=1 << 30)
    client = Client(wire.port)
    (opened,) = wire.until(1)
    for _ in range(80):
        wire.call("say", opened[2], "z" * 65536)
    assert wire.call("close", opened[2], wire.why("Refused"), "bye") is True
    time.sleep(1.5)
    assert _listener(wire)._conns == {}
    client.shut()


@needs_cordis
def test_a_silent_request_head_is_dropped(wire_factory, monkeypatch):
    monkeypatch.setattr(_engine(), "_HEAD_S", 0.3)
    wire = wire_factory()
    sock = _raw(wire.port, b"GET / HT", timeout=3)
    assert sock.recv(10) == b""         # closed by the server, not timed out
    sock.close()


@needs_cordis
def test_lines_over_the_cap_wait_to_be_accepted(wire_factory, monkeypatch):
    monkeypatch.setattr(_engine(), "_MAX_LINES", 2)
    wire = wire_factory()
    first, second = Client(wire.port), Client(wire.port)
    third = _raw(wire.port, _upgrade(), timeout=0.5)
    with pytest.raises(socket.timeout):
        third.recv(100)
    first.bye()
    first.close_frame()
    first.shut()
    third.settimeout(5)
    assert _read_head(third)[0] == "HTTP/1.1 101 Switching Protocols"
    third.close()
    second.shut()


@needs_cordis
def test_message_length_alone_over_most_closes_1009(wire_factory):
    wire = wire_factory(most=16)
    client = Client(wire.port)
    client.sock.sendall(bytes([0x81, 0x80 | 127]) + struct.pack(">Q", 1 << 40))
    assert client.close_frame()[0] == 1009
    assert [k[1] for k in wire.until(2)] == ["Opened", "Shut(TooLarge)"]


@needs_cordis
def test_unmasked_frame_closes_1002(wire_factory):
    wire = wire_factory()
    client = Client(wire.port)
    client.sock.sendall(b"\x81\x02hi")
    assert client.close_frame()[0] == 1002
    assert [k[1] for k in wire.until(2)] == ["Opened", "Shut(Refused)"]


@needs_cordis
def test_text_that_is_not_utf8_closes_1007(wire_factory):
    wire = wire_factory()
    client = Client(wire.port)
    client.send(b"\xff\xfe")
    assert client.close_frame()[0] == 1007
    assert [k[1] for k in wire.until(2)] == ["Opened", "Shut(Refused)"]


@needs_cordis
def test_fragments_arrive_as_one_message(wire_factory):
    wire = wire_factory()
    client = Client(wire.port)

    def frame(b0: int, data: bytes) -> bytes:
        return bytes([b0, 0x80 | len(data)]) + b"\x00" * 4 + data

    client.sock.sendall(frame(0x01, b"hel") + frame(0x89, b"p") + frame(0x80, b"lo"))
    assert client.recv() == (0xA, b"p")   # a ping between fragments is answered
    knocks = wire.until(2)
    assert [(k[1], k[4]) for k in knocks] == [("Opened", ""), ("Said", "hello")]


@needs_cordis
def test_ping_is_answered_with_its_payload(wire_factory):
    wire = wire_factory()
    client = Client(wire.port)
    client.send(b"beat", opcode=0x9)
    assert client.recv() == (0xA, b"beat")


@needs_cordis
@pytest.mark.parametrize(("payload", "status"), [
    (b"GET / HTTP/1.1\r\n" + b"X-Pad: " + b"a" * 20000, "431"),
    (None, "426"),
    ("bad-key", "400"),
])
def test_malformed_upgrades_are_refused(wire_factory, payload, status):
    wire = wire_factory()
    if payload is None:
        payload = _upgrade(version="8")
    elif payload == "bad-key":
        payload = _upgrade(key="short")
    sock = _raw(wire.port, payload)
    assert _read_head(sock)[0].startswith(f"HTTP/1.1 {status} ")
    sock.close()
    time.sleep(0.1)
    assert wire.take() == []


@needs_cordis
@pytest.mark.parametrize("payload", [b"\x03", struct.pack(">H", 1005),
                                     struct.pack(">H", 1000) + b"\xff"])
def test_a_malformed_peer_close_is_refused_1002(wire_factory, payload):
    wire = wire_factory()
    client = Client(wire.port)
    client.send(payload, opcode=0x8)
    code = 1007 if payload.endswith(b"\xff") else 1002
    assert client.close_frame()[0] == code
    assert [k[1] for k in wire.until(2)] == ["Opened", "Shut(Refused)"]

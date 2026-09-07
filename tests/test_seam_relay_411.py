"""The conductor-owned seam relay (roadmap item 411 T3).

Plan-layer: the relay is a stdlib-only asyncio forwarder, so its byte-pumping
and table parsing are exercised here with plain loopback sockets and no
container runtime. The live cross-container forwarding is verified by the
`sandbox-container` CI job; here we prove the forwarder itself is a faithful,
blind byte relay and that the table shapes parse the way the conductor emits
them.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import seam_relay as _relay  # noqa: E402


def test_load_table_parses_both_listen_shapes():
    rows = _relay.load_table(
        '{"rows": ['
        '  {"id": "a", "listen": {"port": 15001}, "target": "prov:9443"},'
        '  {"id": "b", "listen": {"publish": 20002}, "target": "host.docker.internal:16000", "cap": 8}'
        ']}', bind_host="0.0.0.0")
    assert len(rows) == 2
    assert rows[0].id == "a" and rows[0].listen_port == 15001
    assert rows[0].target_host == "prov" and rows[0].target_port == 9443
    assert rows[0].listen_host == "0.0.0.0"
    assert rows[1].listen_port == 20002 and rows[1].cap == 8
    assert rows[1].target_host == "host.docker.internal" and rows[1].target_port == 16000


def test_a_bare_list_is_accepted_as_the_rows():
    rows = _relay.load_table('[{"listen": {"port": 1}, "target": "x:2"}]',
                             bind_host="127.0.0.1")
    assert len(rows) == 1 and rows[0].target_host == "x"


def test_a_malformed_target_is_rejected():
    with pytest.raises(ValueError):
        _relay.load_table('[{"listen": {"port": 1}, "target": "no-port"}]',
                          bind_host="127.0.0.1")


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_the_relay_forwards_bytes_both_ways_blindly():
    """A row forwarding to a local echo server: bytes written by the client come
    back through the relay unchanged. The relay never parses them — this is the
    property the design leans on to say it holds no key and sees only ciphertext."""

    async def scenario() -> bytes:
        # a trivial upstream: echo every chunk, uppercased, so we can tell the
        # round-trip went through it and not straight back.
        async def echo(reader, writer):
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                writer.write(chunk.upper())
                await writer.drain()
            writer.close()

        target_port = _free_port()
        upstream = await asyncio.start_server(echo, "127.0.0.1", target_port)

        listen_port = _free_port()
        relay = _relay.Relay([_relay.RelayRow(
            id="t", listen_host="127.0.0.1", listen_port=listen_port,
            target_host="127.0.0.1", target_port=target_port)])
        lines = await relay.start()
        assert lines and "127.0.0.1" in lines[0]

        reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
        writer.write(b"seam-payload")
        await writer.drain()
        got = await asyncio.wait_for(reader.readexactly(len("seam-payload")), 3)
        writer.close()
        await relay.close()
        upstream.close()
        await upstream.wait_closed()
        return got

    got = asyncio.run(scenario())
    assert got == b"SEAM-PAYLOAD"


def test_a_dead_target_drops_the_connection_without_killing_the_listener():
    """The target being down is not the relay dying: the connection is dropped
    (the consumer's own retry/deadline decides), the listener stays up."""

    async def scenario() -> bool:
        listen_port = _free_port()
        dead_port = _free_port()   # nothing listening here
        relay = _relay.Relay([_relay.RelayRow(
            id="t", listen_host="127.0.0.1", listen_port=listen_port,
            target_host="127.0.0.1", target_port=dead_port)])
        await relay.start()
        # first connect: target is down, so the relay closes our side promptly.
        reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
        assert await asyncio.wait_for(reader.read(1), 3) == b""   # EOF, not a hang
        writer.close()
        # the listener is still up: a second connect still succeeds at the TCP level.
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", listen_port)
        writer2.close()
        await relay.close()
        return True

    assert asyncio.run(scenario()) is True

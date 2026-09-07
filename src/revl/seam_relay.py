"""The conductor-owned seam relay (roadmap item 411 T3).

A sandboxed process runs on a seam-only per-process network (`--internal`,
`docs/design/411-seam-transport.md`). Nothing on that network can reach the
host or a sibling directly; the ONLY other thing on it is this relay, one
container per placement, attached to every per-process network and to the
default bridge. It is a BLIND BYTE FORWARDER: item 56's mutual TLS terminates
in the two seam processes, so the relay holds no certificate, no key and no CA
key, and it only ever sees ciphertext. It can drop or delay (an availability
property the per-operation seam deadline already bounds); it can only forward
where its table says; it cannot read, forge, or impersonate.

The relay is driven by a JSON table on argv (a path) or on stdin. Each row is
one direction of one seam:

    {
      "id": "work->consumer",           # human label, printed
      "listen": {"port": 15001}          # a listener on a per-process network
                 | {"publish": 15001},   # a host-published loopback port
      "target": "revl-sb-plc-prov:9443", # name:port, resolved LAZILY per conn
      "cap": 64                          # optional per-listener connection cap
    }

`listen.port` is a port the relay binds on its OWN address on ONE network (the
conductor decides which network by `docker network connect`ing the relay to it
and binding that interface), so a sandbox sees exactly the endpoints its own
seams use and no other process's. `listen.publish` is the same but on a port
the conductor published to the host's loopback (`-p 127.0.0.1:<port>:<port>`)
for a host-side consumer of a sandboxed provider.

The target is resolved at CONNECT time, not at start: Docker's embedded DNS on
a user-defined network answers a sibling container's name only once it is up,
and a lazy resolve means the relay can start before the process it forwards to.

This module imports nothing outside the standard library on purpose: it runs
inside the first-party runner image, which carries a `python3` but is not
guaranteed to carry revl's own dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class RelayRow:
    """One forwarding rule: a listener and the target it blindly forwards to."""

    id: str
    listen_host: str
    listen_port: int
    target_host: str
    target_port: int
    cap: int = 128

    @classmethod
    def from_spec(cls, spec: dict, *, bind_host: str) -> "RelayRow":
        """Build one row from its JSON shape. `bind_host` is the relay's own
        address on the network this row listens on (a `listen.port` row) or the
        loopback the conductor published to (`listen.publish`); the conductor
        picks it, the relay does not guess."""
        listen = spec["listen"]
        if "publish" in listen:
            port = int(listen["publish"])
        else:
            port = int(listen["port"])
        thost, _, tport = str(spec["target"]).rpartition(":")
        if not thost or not tport:
            raise ValueError(
                f"relay row {spec.get('id')!r}: target {spec.get('target')!r} is "
                f"not 'name:port'")
        return cls(id=str(spec.get("id") or f"{thost}:{tport}"),
                   listen_host=bind_host, listen_port=port,
                   target_host=thost, target_port=int(tport),
                   cap=int(spec.get("cap", 128)))


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction until EOF. Never inspects the bytes — they are the
    seam's TLS record stream, and the relay is not a party to that TLS."""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


class Relay:
    """The forwarder for one placement. One asyncio server per row; each
    accepted connection dials the row's target lazily and pumps both ways."""

    def __init__(self, rows: list[RelayRow]) -> None:
        self._rows = rows
        self._servers: list[asyncio.AbstractServer] = []
        self._active: dict[str, int] = {r.id: 0 for r in rows}

    def _handler(self, row: RelayRow):
        async def handle(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
            if self._active[row.id] >= row.cap:
                # a per-listener cap keeps a hostile body from exhausting the
                # relay's descriptors; the seam's own deadline covers the drop.
                writer.close()
                return
            self._active[row.id] += 1
            try:
                try:
                    t_reader, t_writer = await asyncio.open_connection(
                        row.target_host, row.target_port)
                except OSError:
                    # target not up yet, or gone: drop this connection, keep the
                    # listener. The consumer's own connect retry/deadline decides.
                    writer.close()
                    return
                await asyncio.gather(
                    _pump(reader, t_writer),
                    _pump(t_reader, writer))
            finally:
                self._active[row.id] -= 1

        return handle

    async def start(self) -> list[str]:
        """Bind every row's listener. Returns one description line per row for
        the boot summary; raises OSError if a listener cannot bind (a refusal,
        never a silent skip)."""
        lines: list[str] = []
        for row in self._rows:
            server = await asyncio.start_server(
                self._handler(row), host=row.listen_host, port=row.listen_port)
            self._servers.append(server)
            lines.append(f"{row.id}: {row.listen_host}:{row.listen_port} -> "
                         f"{row.target_host}:{row.target_port} (cap {row.cap})")
        return lines

    async def serve_forever(self) -> None:
        async with _all(self._servers):
            await asyncio.gather(*(s.serve_forever() for s in self._servers))

    async def close(self) -> None:
        for s in self._servers:
            s.close()
        for s in self._servers:
            try:
                await s.wait_closed()
            except Exception:  # pragma: no cover - best-effort teardown
                pass


class _all:
    """`async with` over several servers so `serve_forever` closes them all on
    exit, without depending on a particular asyncio version's ExitStack."""

    def __init__(self, servers) -> None:
        self._servers = servers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        for s in self._servers:
            s.close()


def load_table(text: str, *, bind_host: str) -> list[RelayRow]:
    """Parse a relay table JSON document into rows. The document is
    `{"rows": [...]}` or a bare list; each element is a `RelayRow` spec."""
    doc = json.loads(text)
    rows_spec = doc["rows"] if isinstance(doc, dict) else doc
    return [RelayRow.from_spec(r, bind_host=bind_host) for r in rows_spec]


async def _amain(rows: list[RelayRow]) -> None:
    relay = Relay(rows)
    lines = await relay.start()
    # the boot line the conductor scrapes to know the relay is listening, then
    # the table it derived, so the forwarding is auditable rather than assumed.
    print("UP", flush=True)
    for line in lines:
        print(f"ROW {line}", flush=True)
    await relay.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="revl.seam_relay")
    parser.add_argument("table", nargs="?",
                        help="path to the relay table JSON; omitted reads stdin")
    parser.add_argument("--bind-host", default="0.0.0.0",
                        help="the relay's own address on the network it listens on")
    args = parser.parse_args(argv)
    text = (open(args.table, encoding="utf-8").read() if args.table
            else sys.stdin.read())
    rows = load_table(text, bind_host=args.bind_host)
    if not rows:
        print("UP", flush=True)   # nothing to forward is still a live relay
        return 0
    try:
        asyncio.run(_amain(rows))
    except KeyboardInterrupt:  # pragma: no cover - teardown signal
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())

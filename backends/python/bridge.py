"""py<->py interop bridge (docs/interop-bridge.md §3): a service provided in
one process, consumed in another, over a Unix-domain socket.

Two halves, both transport-agnostic in shape (this cut uses AF_UNIX):

* **stub** (`serve`): the provider side. Given a running ``cordis.Context``
  and the keys to export, it listens and dispatches each incoming call to
  ``ctx.get(key).method(*args)``. Value results marshal straight back.
* **proxy** (`proxy_component`): the consumer side. A component that provides
  ``key`` with a forwarding object whose methods RPC to the stub. To the
  consumer it is an ordinary provider of ``key``; the seam is invisible.

The wire protocol is newline-delimited JSON. revl value types serialize
cleanly by construction (records are dicts, no object identity, no cycles), so
a call is ``{"key","method","args"}`` and a reply ``{"ok",...}``. Value types
cross by copy; resource types (``extern acquire`` returns) are never sent, and
``revl audit`` flags any service that would need to (address-space-bound).

Peer death is withdrawal: a dedicated monitor connection sees EOF when the
provider process goes away; the proxy disposes its own fiber, the provision is
withdrawn, and every dependent deactivates with ordered teardown (R2/R3): the
same reactive path demo/live.py shows for a local swap, now spanning processes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
import threading
import time


def _connect(path: str, attempts: int = 100, delay: float = 0.05) -> socket.socket:
    """Connect to a Unix socket, retrying while the provider comes up. Under
    placement the provider and consumer processes start concurrently, so the
    socket may not exist yet; retrying makes start order irrelevant."""
    last: OSError | None = None
    for _ in range(attempts):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            return sock
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last = exc
            sock.close()
            time.sleep(delay)
    raise last if last is not None else ConnectionError(path)


# ---------------------------------------------------------------------------
# provider side: export provided keys over a socket
# ---------------------------------------------------------------------------


async def _invoke(ctx, keyset, req: dict) -> dict:
    key, method, args = req.get("key"), req.get("method"), req.get("args") or []
    if key not in keyset:
        return {"ok": False, "error": f"key {key!r} is not exported by this process"}
    try:
        service = ctx.get(key)
        if service is None:
            return {"ok": False, "error": f"no provider for key {key!r} right now"}
        result = getattr(service, method)(*args)
        if inspect.isawaitable(result):
            result = await result
        return {"ok": True, "value": result}
    except Exception as exc:  # marshal the failure across the seam (leak 3)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def serve(ctx, keys, path: str):
    """Listen on `path` and answer calls to any of `keys` against `ctx`.
    Returns the asyncio server; the caller keeps it (and the process) alive."""
    keyset = set(keys)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:  # monitor connections never send: this is EOF
                    break
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                reply = await _invoke(ctx, keyset, req)
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    return await asyncio.start_unix_server(handle, path=path)


# ---------------------------------------------------------------------------
# consumer side: a synchronous RPC client + a proxy that forwards to it
# ---------------------------------------------------------------------------


class _Client:
    """One RPC connection plus one idle monitor connection. Provided methods
    are called synchronously in cordis-py, so the RPC round-trip is blocking;
    the monitor connection exists only to observe the provider's death."""

    def __init__(self, path: str) -> None:
        self.rpc = _connect(path)
        self._io = self.rpc.makefile("rwb")
        self.monitor = _connect(path)

    def call(self, key: str, method: str, args):
        self._io.write((json.dumps({"key": key, "method": method, "args": list(args)}) + "\n").encode())
        self._io.flush()
        line = self._io.readline()
        if not line:
            raise ConnectionError("bridge peer closed the connection")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "remote error"))
        return reply.get("value")

    def watch(self, on_lost) -> threading.Thread:
        """Call `on_lost()` once, from a daemon thread, when the provider dies
        (the monitor connection hits EOF)."""
        def run() -> None:
            try:
                while self.monitor.recv(64):
                    pass
            except OSError:
                pass
            on_lost()

        thread = threading.Thread(target=run, name="bridge-peer-watch", daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        for sock in (self.rpc, self.monitor):
            try:
                sock.close()
            except OSError:
                pass


class _Proxy:
    """Stands in for the remote provider of one key. Only the declared method
    names forward; everything else raises, so the runtime can introspect it
    safely."""

    def __init__(self, client: _Client, key: str, methods) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_methods", set(methods))

    def __getattr__(self, name: str):
        if name in object.__getattribute__(self, "_methods"):
            client = object.__getattribute__(self, "_client")
            key = object.__getattribute__(self, "_key")
            return lambda *args: client.call(key, name, args)
        raise AttributeError(name)


def proxy_component(key: str, methods, path: str) -> dict:
    """A cordis component that provides `key` via a proxy forwarding to the
    stub at `path`. Loads with no requirements of its own; its `_client` is
    exposed so the driver can `watch()` for peer death and dispose the fiber."""
    client = _Client(path)

    def apply(ctx, config=None):
        proxy = _Proxy(client, key, methods)

        def body():
            yield lambda: client.close()   # undo: tear the transport down
            yield ctx.provide(key)
            ctx.set(key, proxy)

        return ctx.effect(body, f"{key}-proxy/body")

    return {
        "name": f"{key.capitalize()}Proxy",
        "inject": [],
        "apply": apply,
        "_client": client,
    }

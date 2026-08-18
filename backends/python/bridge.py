"""py<->py interop bridge (docs/interop-bridge.md §3): a service provided in
one process, consumed in another, over a Unix-domain socket.

Two halves, both transport-agnostic in shape (this cut uses AF_UNIX):

* **stub** (`serve`): the provider side. Given a running ``cordis.Context``
  and the keys to export *with the method names each key exports*, it listens
  and dispatches each incoming call to ``ctx.get(key).method(*args)``. Both
  halves of the request are checked against the declaration — an unknown key
  and an unknown method are refused identically — so the seam is exactly the
  enumerable surface the service declares (G8), not "whatever attribute the
  provided object happens to have". Value results marshal straight back.
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
import dataclasses
import inspect
import json
import socket
import threading
import time
from collections.abc import Mapping


# ---------------------------------------------------------------------------
# canonical value codec (docs/interop-bridge.md "Canonical value encoding")
# ---------------------------------------------------------------------------


def _encode_value(value):
    """Encode a revl value for the wire. ADT / Result case instances become
    ``{"$kind": Case, "$value": payload}`` (``$value`` omitted for a nullary
    case); records (dicts), lists, scalars, and Opt (value | None) pass through
    unchanged. The ``$kind`` marker is what separates a tagged value from a
    record. Type-free: it introspects the native instance."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_encode_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode_value(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode_value(getattr(value, f.name)) for f in dataclasses.fields(value)}
    # a native ADT / Result case instance (an emitted case class)
    tagged = {"$kind": type(value).__name__}
    if hasattr(value, "value"):
        tagged["$value"] = _encode_value(value.value)
    return tagged


def _decode_value(value, module):
    """Inverse of `_encode_value`: rebuild native ADT / Result case instances
    from `{"$kind", "$value"}` using `module`'s case classes; records stay
    dicts. With no module a tagged value passes through as a plain dict (the
    caller does not cross ADTs)."""
    if isinstance(value, list):
        return [_decode_value(item, module) for item in value]
    if isinstance(value, dict):
        if "$kind" in value and module is not None:
            case = getattr(module, value["$kind"])
            return case(_decode_value(value["$value"], module)) if "$value" in value else case()
        return {key: _decode_value(item, module) for key, item in value.items()}
    return value


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


def _export_table(exports) -> dict:
    """Normalize `serve`'s `exports` argument to ``{key: allowed methods | None}``.

    Two accepted forms:

    * a mapping ``{key: [method, ...]}`` — the *declared* form. The list is the
      service's own operation list, read off the IR (`src/revl/placement.py`
      ships it in the process spec), so the stub admits exactly what the
      `service` declaration admits.
    * a bare iterable of keys — the legacy form (demo/bridge_pypy.py). With no
      declared list the allowlist is derived at dispatch time from the provided
      object's public methods: weaker, because it trusts the object rather than
      the interface, but still an allowlist (dunders, privates and
      non-callables are refused).
    """
    if isinstance(exports, Mapping):
        return {key: frozenset(methods or ()) for key, methods in exports.items()}
    return {key: None for key in exports}


def _public_methods(service) -> frozenset:
    """Fallback allowlist: the provided object's own public callables."""
    names = set()
    for name in dir(service):
        if name.startswith("_"):
            continue
        try:
            if callable(getattr(service, name)):
                names.add(name)
        except Exception:  # noqa: BLE001 — a property that raises is not a method
            continue
    return frozenset(names)


async def _invoke(ctx, exports: dict, req: dict) -> dict:
    key, method, args = req.get("key"), req.get("method"), req.get("args") or []
    if key not in exports:
        return {"ok": False, "error": f"key {key!r} is not exported by this process"}
    try:
        service = ctx.get(key)
        if service is None:
            return {"ok": False, "error": f"no provider for key {key!r} right now"}
        allowed = exports[key]
        if allowed is None:  # legacy key-only export: derive from the object
            allowed = _public_methods(service)
        if method not in allowed:
            listed = ", ".join(sorted(allowed)) or "(none)"
            return {"ok": False,
                    "error": f"method {method!r} is not exported for key {key!r} "
                             f"(exported: {listed})"}
        result = getattr(service, method)(*args)
        if inspect.isawaitable(result):
            result = await result
        return {"ok": True, "value": _encode_value(result)}
    except Exception as exc:  # marshal the failure across the seam (leak 3)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def serve(ctx, exports, path: str):
    """Listen on `path` and answer calls against `ctx` for the exported surface.

    `exports` is either ``{key: [method, ...]}`` (the declared allowlist, what
    placement passes) or a bare iterable of keys (legacy; see `_export_table`).
    A request naming a key or a method outside that surface is refused with an
    error reply — never dispatched. Returns the asyncio server; the caller
    keeps it (and the process) alive."""
    table = _export_table(exports)

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
                reply = await _invoke(ctx, table, req)
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

    def __init__(self, path: str, module=None) -> None:
        self.rpc = _connect(path)
        self._io = self.rpc.makefile("rwb")
        self.monitor = _connect(path)
        self._module = module  # emitted module: its case classes rebuild ADTs

    def call(self, key: str, method: str, args):
        self._io.write((json.dumps({"key": key, "method": method, "args": list(args)}) + "\n").encode())
        self._io.flush()
        line = self._io.readline()
        if not line:
            raise ConnectionError("bridge peer closed the connection")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "remote error"))
        return _decode_value(reply.get("value"), self._module)

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


def proxy_component(key: str, methods, path: str, module=None) -> dict:
    """A cordis component that provides `key` via a proxy forwarding to the
    stub at `path`. `module` (the emitted module) lets the proxy rebuild ADT /
    Result returns into native case instances. Its `_client` is exposed so the
    driver can `watch()` for peer death and dispose the fiber."""
    client = _Client(path, module)

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

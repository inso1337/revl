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

Peer *hang* is a deadline breach, not a death (docs/seam-deadlines.md). A
provider that is alive but wedged — slow, deadlocked, GC-stalled — answers
neither with a value nor with EOF; a naive blocking round-trip would wait on it
forever, so the consumer that called it wedges too. Every seam call therefore
carries a **deadline** (`_Client(deadline=...)` sets the per-operation default,
optionally per-method via ``deadlines={method: seconds}``; ``call(...,
deadline=...)`` overrides one call). When the round-trip outlives its deadline,
`call` raises `SeamDeadline` — its **own** fault kind, deliberately neither the
`ConnectionError` a peer death raises (so it does **not** withdraw the
provision) nor the `RuntimeError` a provider-side error marshals back. The
consumer's L-Raise then unwinds exactly as for any other seam failure (A8):
effects accumulated so far revert LIFO, the component lands FAILED, no residue.

Peer *replacement* is re-point, not withdrawal: `_Client.repoint(new_path)` is
a **planned cutover** (`revl swap <component> --to <backend>`, docs/swap.md). A
successor provider is booted on another socket; the client reconnects its RPC
and monitor connections to it atomically, under a lock so an in-flight call
drains against the old provider first (drain-v1). The monitor is keyed by a
*generation*: the old monitor connection's EOF, when the old provider is then
torn down, is recognised as the expected end of a superseded generation and
does **not** fire `on_lost` — so a swap is a re-point, not the withdrawal blip
an unplanned death produces. The distinction is the whole point: unplanned
death still withdraws; a planned cutover carries the provision to a successor.
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


class SeamDeadline(TimeoutError):
    """A seam call outlived its deadline: the provider is neither answering nor
    gone — it hung.

    A distinguishable fault, on purpose. It is **not** a `ConnectionError` (a
    peer death, which the monitor turns into a reactive withdrawal) and it is
    **not** a `RuntimeError` (a provider-side error marshalled back across the
    seam). A consumer — or a test — can therefore tell a hang apart from a death
    and from a remote failure by fault kind alone. It subclasses `TimeoutError`
    so generic timeout handling still recognises it, while staying disjoint from
    the two seam faults that already exist.

    Carries the seam it broke on: `key`, `method`, and the `deadline` (seconds)
    that was breached.
    """

    def __init__(self, key: str, method: str, deadline: float) -> None:
        self.key = key
        self.method = method
        self.deadline = deadline
        super().__init__(
            f"seam call {key}.{method} exceeded its {deadline:g}s deadline "
            f"(the provider is alive but did not answer in time)")


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
    the monitor connection exists only to observe the provider's death.

    A single lock serialises `call`, `repoint` and `close` against each other.
    Because `call` holds the lock across its blocking round-trip, a `repoint`
    requested mid-call waits for that in-flight call to drain against the old
    provider before the cutover — the client-granularity form of drain-v1.

    The monitor is keyed by a monotonic `_generation`. Each watcher thread is
    bound to the connection and generation it started with; on EOF it fires
    `on_lost` **only if** it is still the current generation. A `repoint`
    bumps the generation, so the old monitor's EOF (when the superseded
    provider is torn down) is recognised as an expected cutover, not a death.
    """

    def __init__(self, path: str, module=None, deadline=None, deadlines=None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self.rpc = _connect(path)
        self._io = self.rpc.makefile("rwb")
        self.monitor = _connect(path)
        self._module = module  # emitted module: its case classes rebuild ADTs
        self._generation = 0
        self._on_lost = None    # set by watch(); re-armed on each repoint
        # the per-operation deadline default (seconds), and an optional
        # per-method map that overrides it for named operations. `None` means
        # "no deadline" — an unbounded blocking round-trip, the legacy shape a
        # swap/repoint client keeps unless a caller opts in.
        self._deadline = deadline
        self._deadlines = dict(deadlines or {})

    def deadline_for(self, method: str, override=None):
        """Resolve the effective deadline (seconds) for one call: an explicit
        per-call `override` wins; else the per-method default; else the
        client-wide default; else `None` (unbounded)."""
        if override is not None:
            return override
        if method in self._deadlines:
            return self._deadlines[method]
        return self._deadline

    def call(self, key: str, method: str, args, deadline=None):
        seconds = self.deadline_for(method, deadline)
        with self._lock:
            io = self._io
            rpc = self.rpc
            io.write((json.dumps({"key": key, "method": method, "args": list(args)}) + "\n").encode())
            io.flush()
            # Bound the blocking read on the reply. A wedged provider sends
            # nothing, so the recv times out at `seconds`; we surface that as
            # the distinguishable SeamDeadline fault rather than blocking on it
            # forever. The socket's prior timeout is restored either way; the
            # whole round-trip is under the lock (as it already was for
            # drain-v1), so `rpc` is exactly the socket we time and read.
            prev = rpc.gettimeout()
            try:
                if seconds is not None:
                    rpc.settimeout(seconds)
                line = io.readline()
            except (TimeoutError, socket.timeout):
                # the seam hung: alive, not answering, not gone. Distinguishable
                # from a ConnectionError (death -> withdrawal) and a RuntimeError
                # (provider error) — its own fault kind.
                raise SeamDeadline(key, method, seconds) from None
            finally:
                try:
                    rpc.settimeout(prev)
                except OSError:
                    pass
            module = self._module
        if not line:
            raise ConnectionError("bridge peer closed the connection")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "remote error"))
        return _decode_value(reply.get("value"), module)

    def watch(self, on_lost) -> threading.Thread:
        """Call `on_lost()` once, from a daemon thread, when the *current*
        provider dies unplanned (its monitor connection hits EOF while still
        the live generation). A planned `repoint` supersedes the generation, so
        the old provider's later teardown does not trigger `on_lost`."""
        with self._lock:
            self._on_lost = on_lost
            return self._spawn_watch(self.monitor, self._generation)

    def _spawn_watch(self, monitor_sock, generation: int) -> threading.Thread:
        def run() -> None:
            try:
                while monitor_sock.recv(64):
                    pass
            except OSError:
                pass
            # EOF on this monitor connection. If a repoint has since carried us
            # to a successor, this is the expected end of a superseded
            # generation — stay quiet. Otherwise the current provider died
            # unplanned: withdraw (R2/R3).
            with self._lock:
                superseded = generation != self._generation
                on_lost = self._on_lost
            if superseded or on_lost is None:
                return
            on_lost()

        thread = threading.Thread(target=run, name="bridge-peer-watch", daemon=True)
        thread.start()
        return thread

    def repoint(self, new_path: str) -> None:
        """Planned cutover: reconnect this client's RPC and monitor to a
        successor provider serving at `new_path`, without firing `on_lost`.

        The successor is dialled *before* the lock is taken, so a failed
        connect raises here and leaves the live client entirely untouched (the
        swap can then be refused with nothing re-pointed). Under the lock — held
        by any in-flight `call`, which therefore drains against the old provider
        first — the new connections are swapped in and the generation is bumped;
        subsequent calls go to the successor. The old sockets are then shut so
        the superseded monitor thread wakes promptly and, seeing the bumped
        generation, exits without withdrawing."""
        new_rpc = _connect(new_path)
        new_io = new_rpc.makefile("rwb")
        new_monitor = _connect(new_path)
        with self._lock:
            old_io, old_rpc, old_monitor = self._io, self.rpc, self.monitor
            self.rpc, self._io, self.monitor = new_rpc, new_io, new_monitor
            self._path = new_path
            self._generation += 1
            generation = self._generation
            rearm = self._on_lost is not None
        try:
            old_io.close()
        except OSError:
            pass
        for sock in (old_rpc, old_monitor):
            try:
                sock.shutdown(socket.SHUT_RDWR)  # wake the superseded watcher
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if rearm:
            self._spawn_watch(new_monitor, generation)

    def close(self) -> None:
        with self._lock:
            socks = (self.rpc, self.monitor)
        for sock in socks:
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


def proxy_component(key: str, methods, path: str, module=None,
                    deadline=None, deadlines=None) -> dict:
    """A cordis component that provides `key` via a proxy forwarding to the
    stub at `path`. `module` (the emitted module) lets the proxy rebuild ADT /
    Result returns into native case instances. Its `_client` is exposed so the
    driver can `watch()` for peer death and dispose the fiber.

    `deadline` sets the per-operation deadline default (seconds) every forwarded
    call carries; `deadlines` (``{method: seconds}``) overrides it per named
    operation. A breach raises `SeamDeadline` in the calling fiber, which the
    runtime unwinds like any other seam failure (A8: revert LIFO, no residue).
    Placement (`src/revl/placement.py`) reads these off the seam spec."""
    client = _Client(path, module, deadline=deadline, deadlines=deadlines)

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

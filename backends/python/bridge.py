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
import ssl
import sys as _sys
import threading
import time
import types as _types
from collections.abc import Mapping

# Under ``from __future__ import annotations`` every annotation is a string, and
# defining a `@dataclass` makes `dataclasses` resolve those strings through
# ``sys.modules[cls.__module__]`` (the KW_ONLY sentinel scan, py3.14). A by-path
# loader (``spec_from_file_location`` + ``exec_module`` without registering the
# module) leaves that entry absent, so the scan hits ``None.__dict__``. The
# bridge is loaded exactly that way by several suites (it needs no cordis), so
# self-register a placeholder when nothing is registered yet — a no-op under a
# normal ``import`` (Python has already put the real module there).
if _sys.modules.get(__name__) is None:
    _sys.modules[__name__] = _types.ModuleType(__name__)


# The confidentiality choke point (item 256 Slice 3): `confidential.py`, the
# sibling module the runtime, the recorder and this bridge all read, so a value
# that must not leave is redacted in ONE place rather than at each printer. It
# ships next to this file and must travel with it.
#
# The fallback covers this module being loaded BY PATH with its own directory off
# `sys.path` (`spec_from_file_location(".../bridge.py")`, which several suites
# do). A bare import cannot survive that, and continuing without the module would
# mean continuing to leak, so this raises rather than degrades.
try:
    import confidential
except ModuleNotFoundError:  # pragma: no cover (path-loaded copy of this module)
    import importlib.util as _importlib_util
    import os as _os

    _confidential_spec = _importlib_util.spec_from_file_location(
        "confidential",
        _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                      "confidential.py"))
    confidential = _importlib_util.module_from_spec(_confidential_spec)
    _confidential_spec.loader.exec_module(confidential)
    _sys.modules.setdefault("confidential", confidential)


# ---------------------------------------------------------------------------
# transport endpoints: a local UDS (default) or a network TCP + mTLS seam
# (docs/network-placement.md)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TlsConfig:
    """The mutual-TLS material one process presents on a **network** seam.

    Both ends of a network seam present a certificate (mutual TLS): the
    provider's `serve` demands a client certificate (``CERT_REQUIRED``) and the
    consumer's client verifies the provider's certificate against the same CA —
    so a network seam is not "whoever can reach the port" but "the two processes
    that hold CA-signed certs". `identity` is *this* process's identity, reused
    from the operator model (item 55): the token the certificate is minted for,
    so a seam call is attributable to a named process, not just an address. A
    local UDS seam carries **no** `TlsConfig` — a 0700-dir Unix socket is bound
    to one host by construction and needs no cert (full back-compat).
    """

    certfile: str
    keyfile: str
    cafile: str
    identity: str
    server_hostname: str | None = None

    def server_context(self) -> ssl.SSLContext:
        """The provider side: present our cert, and **demand** the peer's (mTLS)."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.certfile, self.keyfile)
        ctx.load_verify_locations(self.cafile)
        ctx.verify_mode = ssl.CERT_REQUIRED  # mutual: a client with no cert is refused
        return ctx

    def client_context(self) -> ssl.SSLContext:
        """The consumer side: present our cert, and verify the provider's against
        the CA (hostname-checked)."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_cert_chain(self.certfile, self.keyfile)
        ctx.load_verify_locations(self.cafile)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True
        return ctx

    @classmethod
    def from_spec(cls, spec) -> "TlsConfig":
        """Build from the seam spec's ``tls`` mapping (what placement writes)."""
        return cls(
            certfile=spec["cert"], keyfile=spec["key"], cafile=spec["ca"],
            identity=spec["identity"], server_hostname=spec.get("server_hostname"))


@dataclasses.dataclass(frozen=True)
class Endpoint:
    """Where a seam is reachable, and how. Two shapes, one code path:

    * **local UDS** (default, fully back-compatible): `path` set, `host`/`port`
      unset, `tls` None. A bare socket-path string is exactly this.
    * **network TCP + mTLS**: `host` and `port` set, `tls` a `TlsConfig`. The
      deadline, reactive-withdrawal and canonical-encoding machinery all apply
      unchanged over TCP — the only differences are the address family and the
      TLS wrap around the stream.
    """

    path: str | None = None
    host: str | None = None
    port: int | None = None
    tls: TlsConfig | None = None

    @property
    def is_network(self) -> bool:
        return self.host is not None

    def describe(self) -> str:
        if self.is_network:
            who = f" as {self.tls.identity}" if self.tls else ""
            return f"tcp://{self.host}:{self.port}{who}"
        return self.path or "?"

    @classmethod
    def from_spec(cls, spec) -> "Endpoint":
        """Normalize any accepted seam-target form to an `Endpoint`:

        * an `Endpoint` -> itself;
        * a `str` -> a local UDS at that path (the legacy form);
        * a mapping with ``host`` -> a network TCP+mTLS endpoint;
        * a mapping with ``socket``/``path`` -> a local UDS.
        """
        if isinstance(spec, Endpoint):
            return spec
        if isinstance(spec, str):
            return cls(path=spec)
        if spec.get("host") is not None:
            tls = spec.get("tls")
            return cls(host=spec["host"], port=int(spec["port"]),
                       tls=TlsConfig.from_spec(tls) if tls is not None else None)
        return cls(path=spec.get("socket") or spec.get("path"))


def _as_endpoint(target) -> Endpoint:
    return Endpoint.from_spec(target)


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


class SeamMarshalError(TypeError):
    """A value reached the wire encoder that cannot cross a process seam: it is
    not a scalar, list, dict, declared value record, or emitted ADT/Result case
    — an opaque host object (a cordis `Value`, a live resource handle).

    The seam carries only value copies (docs/interop-bridge.md §3), so the
    encoder REFUSES such a value fail-closed rather than degrading it to a dead
    ``{"$kind": <typename>}`` tag that would arrive on the far side detached
    from its host-side identity. This is the runtime backstop to the plan-time
    resource-crossing refusal (`src/revl/placement.py`): a resource the name
    check missed still cannot cross silently, on the return path or the argument
    path. Subclasses `TypeError` (a marshaling failure is a type error) so the
    provider's dispatch loop marshals it back as an error reply and any generic
    `TypeError` handling still recognises it, while it stays distinctly
    identifiable by type.
    """

    def __init__(self, value) -> None:
        self.type_name = type(value).__name__
        super().__init__(
            f"cannot marshal a {self.type_name!r} across a process seam: it is "
            "not a scalar, list, dict, value record, or ADT/Result case. A seam "
            "carries only value copies; an opaque host object or a live resource "
            "handle does not cross (docs/interop-bridge.md §3). Refusing rather "
            "than shipping a dead tag.")


def _is_emitted_case(value) -> bool:
    """Is `value` an emitted ADT / Result case instance (as opposed to an opaque
    host object)?

    Recognised by the exact shape the Python emitter guarantees for cases
    (`backends/python/emit.py` `_emit_types` and the built-in Ok/Err): a plain,
    NON-dataclass, slots-only class whose only per-instance datum is an optional
    ``value`` payload. A record value is a dict or a `@dataclass` and is handled
    before this point; an opaque host object carries a per-instance ``__dict__``
    or slots other than ``value``, so it is refused instead of shipped as a dead
    ``$kind`` tag."""
    cls = type(value)
    # a case is always an instance of a NAMED emitted class, never bare `object`
    # (a slots-less, dict-less `object()` would otherwise read as a nullary case).
    if cls is object or dataclasses.is_dataclass(cls):
        return False
    # emitted cases are slots-only: no per-instance __dict__ anywhere in the MRO.
    if hasattr(value, "__dict__"):
        return False
    slots: set[str] = set()
    for klass in cls.__mro__:
        declared = getattr(klass, "__slots__", ())
        slots.update((declared,) if isinstance(declared, str) else declared)
    return slots <= {"value"}


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
    # Terminal: a native ADT / Result case instance (an emitted case class)
    # becomes a `{"$kind", "$value"?}` tag. ANY OTHER object — an opaque host
    # `Value`, a live resource handle — is REFUSED fail-closed rather than
    # degraded to a dead `$kind` tag it once was (Finding B). A seam ships only
    # value copies; this makes "the opaque host Value / a resource never crosses"
    # a runtime guarantee, backstopping the plan-time crossing refusal.
    if not _is_emitted_case(value):
        raise SeamMarshalError(value)
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


def _connect_tcp(endpoint: Endpoint, attempts: int = 100, delay: float = 0.05) -> socket.socket:
    """Connect to a network seam over TCP and wrap it in mutual TLS. The retry
    loop mirrors `_connect`: a *TCP* refusal (the provider process is still
    coming up) is retried, so start order stays irrelevant. A completed TCP
    connect whose **TLS handshake** then fails is a real fault — a bad cert, the
    wrong CA, a hostname mismatch — and is raised, never retried away."""
    if endpoint.tls is None:
        raise ValueError(
            f"network seam tcp://{endpoint.host}:{endpoint.port} has no TLS "
            "identity — a seam that crosses machines must present a per-process "
            "certificate (mTLS); refusing to dial it in the clear "
            "(docs/network-placement.md)")
    context = endpoint.tls.client_context()
    server_hostname = endpoint.tls.server_hostname or endpoint.host
    last: OSError | None = None
    for _ in range(attempts):
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.connect((endpoint.host, endpoint.port))
        except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
            last = exc
            raw.close()
            time.sleep(delay)
            continue
        try:  # TCP is up: the handshake runs now — its failures are terminal.
            return context.wrap_socket(raw, server_hostname=server_hostname)
        except (ssl.SSLError, OSError):
            raw.close()
            raise
    raise last if last is not None else ConnectionError(f"{endpoint.host}:{endpoint.port}")


def _connect_endpoint(endpoint: Endpoint, attempts: int = 100, delay: float = 0.05) -> socket.socket:
    """Dial an endpoint by its shape: TCP+mTLS for a network seam, UDS otherwise."""
    if endpoint.is_network:
        return _connect_tcp(endpoint, attempts, delay)
    return _connect(endpoint.path, attempts, delay)


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


def seam_failure(exc: BaseException, args) -> str:
    """The error text a provider-side failure is allowed to carry BACK ACROSS the
    seam (item 421 F5).

    The consumer is on the other side of a trust boundary. A forward crossing
    into a declared `Secret[T]` receiver authorises disclosure TO THE RECEIVER;
    it does not authorise the error channel to perform the REVERSE crossing the
    checker refuses statically. And the trigger needs no author interpolation at
    all: a plain `self.data[token]` lookup raises `KeyError: '<token>'`, so the
    provider hands the consumer back the very value it was called with.

    The exception TYPE and the message's shape survive, so a failure still
    says what went wrong and where, while every argument value it quotes
    becomes `confidential.REDACTED_ARG`. A remembered `Secret[T]` value is
    scrubbed too, so a credential threaded in from config (never an argument of
    this call) cannot ride out either."""
    return confidential.redact_call_text(f"{type(exc).__name__}: {exc}", args)


async def _invoke(ctx, exports: dict, req: dict, module=None) -> dict:
    key, method = req.get("key"), req.get("method")
    # Decode the arguments symmetrically with the client's `_encode_value`
    # (Finding B): a tagged ADT/Result arg is rebuilt into its native case
    # instance through `module`'s case classes; a plain value (scalar, list,
    # record dict) passes through unchanged, so a value-typed call is byte-
    # identical to the pre-encode wire. Without a module (the legacy serve form)
    # a tagged value stays a plain dict, exactly as before.
    args = _decode_value(req.get("args") or [], module)
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
    except Exception as exc:  # marshal the failure across the seam
        # ...with the caller's own argument values scrubbed out of it first
        # (item 421 F5): `_Client.call` re-raises this text as a RuntimeError on
        # the consumer, and `_process_runner` logs it, so this ONE funnel is
        # what those two sinks inherit.
        return {"ok": False, "error": seam_failure(exc, args)}


def peer_identity(writer) -> str | None:
    """The identity the TRANSPORT authenticated for this connection, or None.

    On a network seam that is the peer certificate's commonName — the item-55
    per-process identity the certificate was minted for, proven by the mTLS
    handshake rather than asserted in a request. On a local UDS seam there is no
    transport-level identity (a 0700-dir socket is bound to one host by
    construction), so this is None and a correlation guard authenticates the
    peer by its own per-process secret alone (item 118, docs/deploy.md).
    """
    ssl_object = writer.get_extra_info("ssl_object")
    if ssl_object is None:
        return None
    cert = ssl_object.getpeercert()
    if not cert:
        return None
    for field in cert.get("subject") or ():
        for name, value in field:
            if name == "commonName":
                return value
    return None


async def serve(ctx, exports, endpoint, module=None, correlation=None):
    """Listen on `endpoint` and answer calls against `ctx` for the exported
    surface.

    `endpoint` is a UDS path string (the legacy form) or an `Endpoint` — a
    local UDS, or a network TCP+mTLS seam. Over TCP the provider **demands** the
    consumer's certificate (mutual TLS), so an anonymous caller that reaches the
    port is refused at the handshake, before any request is read.

    `exports` is either ``{key: [method, ...]}`` (the declared allowlist, what
    placement passes) or a bare iterable of keys (legacy; see `_export_table`).
    A request naming a key or a method outside that surface is refused with an
    error reply — never dispatched.

    `module` is the emitted module (its case classes rebuild ADT/Result args a
    consumer encodes across the seam, symmetric with `proxy_component`'s
    `module` on the return path). Optional: with no module a tagged argument
    stays a plain dict, exactly the legacy behaviour.

    `correlation` (item 118, docs/deploy.md) is an optional guard —
    `revl.deploy.CorrelationGuard` — run on EVERY request before dispatch. With
    one set, a request must carry a `correlation` envelope that authenticates
    against the peer identity (its own per-process secret, and, on a network
    seam, the identity the mTLS handshake proved), and that is not a replay of
    an envelope already seen for that `(peer_identity, composition_id,
    generation, idempotency_key)` scope. A request that fails either check is
    answered with an error and NEVER dispatched. Absent (the default) nothing
    changes: the wire and the dispatch are byte-identical to the pre-118 seam.

    Returns the asyncio server; the caller keeps it (and the process) alive."""
    ep = _as_endpoint(endpoint)
    table = _export_table(exports)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Resolved once per connection: the transport-level identity does not
        # change mid-session, and re-reading the peer cert per request would
        # invite a check that drifts from the session it is supposed to bind.
        identity = peer_identity(writer) if correlation is not None else None
        try:
            while True:
                line = await reader.readline()
                if not line:  # monitor connections never send: this is EOF
                    break
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if correlation is not None:
                    ok, reason = correlation.admit(req.get("correlation"),
                                                   transport_identity=identity)
                    if not ok:
                        writer.write((json.dumps({
                            "ok": False,
                            "error": f"correlation refused: {reason}",
                            "correlation_refused": reason}) + "\n").encode())
                        await writer.drain()
                        continue
                reply = await _invoke(ctx, table, req, module)
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    if ep.is_network:
        if ep.tls is None or not ep.tls.identity:
            raise ValueError(
                f"cannot serve network seam tcp://{ep.host}:{ep.port} without a "
                "TLS identity — a network provider must present a per-process "
                "certificate (mTLS); refusing to listen in the clear "
                "(docs/network-placement.md)")
        return await asyncio.start_server(handle, host=ep.host, port=ep.port,
                                          ssl=ep.tls.server_context())
    return await asyncio.start_unix_server(handle, path=ep.path)


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

    def __init__(self, endpoint, module=None, deadline=None, deadlines=None,
                 correlation=None) -> None:
        self._lock = threading.RLock()
        # item 118 §1.4: the sealed correlation envelope this consumer stamps on
        # every crossing. A callable is invoked per call (so `effect_id` and the
        # idempotency key can vary); a mapping is sent verbatim. None keeps the
        # request line byte-identical to the pre-118 wire.
        self._correlation = correlation
        self._endpoint = _as_endpoint(endpoint)
        self.rpc = _connect_endpoint(self._endpoint)
        self._io = self.rpc.makefile("rwb")
        self.monitor = _connect_endpoint(self._endpoint)
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
        # Encode the arguments through the SAME fail-closed marshaller the return
        # path uses (Finding B): a scalar/list/dict/record/ADT arg encodes (byte-
        # identical for value-typed args, since `_encode_value` is the identity
        # on scalars/lists/dicts), and an opaque host object or a live resource
        # handle raises SeamMarshalError HERE, before it touches the socket,
        # instead of the raw `json.dumps` degrading it to a bare TypeError deep
        # in the encoder. Args and returns now both fail closed.
        encoded_args = _encode_value(list(args))
        request = {"key": key, "method": method, "args": encoded_args}
        if self._correlation is not None:
            envelope = self._correlation
            request["correlation"] = (envelope(key, method)
                                      if callable(envelope) else envelope)
        with self._lock:
            io = self._io
            rpc = self.rpc
            io.write((json.dumps(request) + "\n").encode())
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

    def repoint(self, new_target) -> None:
        """Planned cutover: reconnect this client's RPC and monitor to a
        successor provider serving at `new_target` (a UDS path string or an
        `Endpoint` — so a cutover may cross onto a network seam too), without
        firing `on_lost`.

        The successor is dialled *before* the lock is taken, so a failed
        connect raises here and leaves the live client entirely untouched (the
        swap can then be refused with nothing re-pointed). Under the lock — held
        by any in-flight `call`, which therefore drains against the old provider
        first — the new connections are swapped in and the generation is bumped;
        subsequent calls go to the successor. The old sockets are then shut so
        the superseded monitor thread wakes promptly and, seeing the bumped
        generation, exits without withdrawing."""
        new_endpoint = _as_endpoint(new_target)
        new_rpc = _connect_endpoint(new_endpoint)
        new_io = new_rpc.makefile("rwb")
        new_monitor = _connect_endpoint(new_endpoint)
        with self._lock:
            old_io, old_rpc, old_monitor = self._io, self.rpc, self.monitor
            self.rpc, self._io, self.monitor = new_rpc, new_io, new_monitor
            self._endpoint = new_endpoint
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
    safely.

    An `async fn` operation must forward as an *awaitable*, not as its resolved
    value. `_Client.call` is a blocking synchronous round-trip that returns the
    already-decoded value (e.g. a `str`), so a forwarding provide-method that
    does `return await cache.get(k)` — the shape an `async fn` consumer emits —
    would `await` a bare `str` and raise ``'str' object can't be awaited``
    (roadmap item 331). The proxy therefore wraps every method the service
    declares `async` in a coroutine that yields the round-trip's value, so the
    caller's `await` resolves it correctly. A *sync* (`fn`/`emission fn`)
    operation keeps returning its value directly — a fire-and-forget `emit
    db.execute(...)` across a seam must not hand back an un-awaited coroutine.
    `async_methods` is the subset of `methods` the IR marks `async`; placement
    reads it off the service declaration (`src/revl/placement.py`)."""

    def __init__(self, client: _Client, key: str, methods, async_methods=()) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_methods", set(methods))
        object.__setattr__(self, "_async_methods", set(async_methods))

    def __getattr__(self, name: str):
        if name in object.__getattribute__(self, "_methods"):
            client = object.__getattribute__(self, "_client")
            key = object.__getattribute__(self, "_key")
            if name in object.__getattribute__(self, "_async_methods"):
                async def _forward_async(*args):
                    # The round-trip is blocking (cordis-py's model); awaiting
                    # the coroutine performs it and resolves to the value, so a
                    # chained `await forward(await cross_seam_call())` works.
                    return client.call(key, name, args)
                return _forward_async
            return lambda *args: client.call(key, name, args)
        raise AttributeError(name)


def _require_network_contract(key: str, endpoint: Endpoint, deadline) -> None:
    """A network seam is malpractice without identity **and** a deadline: an
    anonymous cross-machine caller, or an unbounded round-trip against a wedged
    remote provider. Refuse to build either, with a diagnostic that names the
    missing half — before anything connects."""
    if endpoint.tls is None or not endpoint.tls.identity:
        raise ValueError(
            f"network seam {key!r} at tcp://{endpoint.host}:{endpoint.port} has "
            "no TLS identity — a seam that crosses machines must present a "
            "per-process identity (mTLS, item 55). Refusing to proxy it "
            "(docs/network-placement.md)")
    if deadline is None:
        raise ValueError(
            f"network seam {key!r} at tcp://{endpoint.host}:{endpoint.port} has "
            "no deadline — a network round-trip against a wedged provider would "
            "block the consumer forever. Refusing to proxy it without a seam "
            "deadline (item 54, docs/seam-deadlines.md)")


def proxy_component(key: str, methods, endpoint, module=None,
                    deadline=None, deadlines=None, async_methods=None,
                    correlation=None) -> dict:
    """A cordis component that provides `key` via a proxy forwarding to the
    stub at `endpoint` (a UDS path string, or an `Endpoint` — a local UDS or a
    network TCP+mTLS seam). `module` (the emitted module) lets the proxy rebuild
    ADT / Result returns into native case instances. Its `_client` is exposed so
    the driver can `watch()` for peer death and dispose the fiber.

    `async_methods` is the subset of `methods` the service declares `async fn`;
    those forward as awaitables so a chained `await` on the consuming side
    resolves the cross-seam value (item 331). `deadline` sets the per-operation
    deadline default (seconds) every forwarded call carries; `deadlines`
    (``{method: seconds}``) overrides it per named operation. A breach raises `SeamDeadline` in the calling fiber, which the
    runtime unwinds like any other seam failure (A8: revert LIFO, no residue).
    Placement (`src/revl/placement.py`) reads these off the seam spec. A
    **network** seam is refused here unless it carries both an identity and a
    deadline — a seam without either is malpractice across a machine boundary.

    `correlation` (item 118 §1.4) is the sealed effect-correlation envelope this
    consumer stamps on every crossing, or a callable `(key, method) -> envelope`
    when the identity varies per call. The provider authenticates it against the
    peer identity before dispatch (`serve`'s `correlation` guard)."""
    ep = _as_endpoint(endpoint)
    if ep.is_network:
        _require_network_contract(key, ep, deadline)
    client = _Client(ep, module, deadline=deadline, deadlines=deadlines,
                     correlation=correlation)

    def apply(ctx, config=None):
        proxy = _Proxy(client, key, methods, async_methods or ())

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

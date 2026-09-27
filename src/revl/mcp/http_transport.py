"""MCP over Streamable HTTP, one operator per request (issue #1463, slice 1).

`revl mcp serve --http` and `revl mcp proxy --http` put the same dispatchers the
stdio transports use (`server.handle`, `Proxy.handle`) behind one HTTP endpoint,
`POST /mcp`, speaking MCP revision 2026-07-28. That revision has no protocol
session: every request carries its own metadata, so every request is also
authenticated on its own. docs/mcp-http-transport.md is the user-facing account.

What this module adds, and nothing else:

  * **Who is calling.** Each request is bound to one operator of the operator
    profile (item 55), by a bearer secret whose SHA-256 is that operator's
    `key sha256:` line, or by the commonName of a client certificate under mutual
    TLS (item 56's rule). A request with no identity is answered 401 and never
    dispatched. There is no process operator to fall back to: HTTP mode refuses
    to start without a profile or with `--operator`.
  * **One identity at a time.** The live session is single-threaded, so requests
    are dispatched one at a time under a lock. Inside it the session's
    `operator` is the caller; outside it, a deny-all placeholder whose token no
    profile can declare. It is never None (None means "no profile, ungated") and
    never empty (an empty token is the lease book's default holder). So leases,
    tickets, proposers, approvals and the trace all name the real caller, through
    the surfaces that already read `session.operator`.
  * **A cast is the caller's own.** Over HTTP a caller casts only as itself:
    `asToken` naming anyone else is refused before dispatch, and a cast the
    session binds to its current operator is recorded `boundBy: "transport"`.
  * **E-Stop does not queue.** A caller blocked behind a long upstream call holds
    the lock. `revl_estop` therefore does not wait for it: it arms the runtime's
    E-Stop latch file (item 443), which every crossing seam reads, and the halt
    is completed on the session the next time the lock is taken. `runtime.estop`
    itself is not called from a second thread: measured, it is not thread-safe
    (`list(_LIVE_FRAMES)` raises when another thread adds a frame mid-walk).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler

from .http_guard import (DispatchLock, Exposure, ExposureError, Listener,
                         request_refusal)
from .live_profile import DEFAULT_SETTLE_MS, ProfileSource, ProfileUnavailable, is_estop

PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = (PROTOCOL_VERSION,)
ENDPOINT = "/mcp"
MAX_BODY = 4 * 1024 * 1024
NO_CALLER = "<no authenticated caller>"
BOUND_BY_TRANSPORT = "transport"
AUTH_MODES = ("bearer", "mtls")

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022
_NAMED_METHODS = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
# Methods this slice does not serve over HTTP: the long-lived notification stream
# (slice 2) and the legacy subscription and logging verbs it replaces.
_NOT_SERVED = frozenset({"subscriptions/listen", "resources/subscribe",
                         "resources/unsubscribe", "logging/setLevel"})
# The quorum verbs whose `asToken` could name someone other than the caller.
CAST_TOOLS = frozenset({"revl_approve", "revl_revoke", "revl_escalate",
                        "revl_override"})


class TransportError(RuntimeError):
    """The transport cannot start as configured."""


# ---------------------------------------------------------------- identity

def _now_ms() -> int:
    return int(time.time() * 1000)


def _header_values(headers, name: str) -> list[str]:
    getter = getattr(headers, "get_all", None)
    if getter is not None:
        return list(getter(name) or [])
    value = headers.get(name)
    return [] if value is None else [value]


def _common_name(cert) -> str | None:
    for rdn in (cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


class _StaticProfile:
    """A registry that never changes (a profile passed in memory)."""

    def __init__(self, registry) -> None:
        self.registry = registry
        self.error = None

    def current(self):
        return self.registry, None


class Authenticator:
    """Binds one request to one operator of the profile, or says why not."""

    def __init__(self, registry=None, mode: str = "bearer", *, source=None) -> None:
        if source is None:
            if registry is None:
                raise TransportError("HTTP mode needs --operator-profile: every "
                                     "request is bound to one of its operators, and "
                                     "there is no process operator to fall back to")
            source = _StaticProfile(registry)
        if mode not in AUTH_MODES:
            raise TransportError(f"--auth must be one of {', '.join(AUTH_MODES)}")
        self.source = source
        self.mode = mode
        if mode == "bearer" and not any(o.vote_key
                                        for o in source.registry.operators.values()):
            raise TransportError(
                "no operator in the profile declares `key sha256:<digest>`, so no "
                "caller could authenticate with a bearer secret. Declare one, or "
                "use --auth mtls")

    @property
    def registry(self):
        """The registry the last request was checked against (None while the
        profile file is broken)."""
        return self.source.registry

    def authenticate(self, headers, peer_cert, *, registry=None) -> tuple[object | None, str, int]:
        """`(operator, "", 200)`, or `(None, why, status)`: 401 for the caller's
        credential, 503 when the profile itself cannot be read. `registry`
        authenticates against a given registry instead of the live one (the
        E-Stop fallback)."""
        if registry is None:
            registry, broken = self.source.current()
            if registry is None:
                return None, broken or "no operator profile is loaded", 503
        authorizations = _header_values(headers, "Authorization")
        if self.mode == "mtls":
            if authorizations:
                return None, ("this server authenticates by client certificate; an "
                              "Authorization header as well would be a second "
                              "identity, so the request is refused"), 401
            token = _common_name(peer_cert)
            operator = registry.get(token) if token else None
            if operator is None:
                return None, "the client certificate names no operator of the profile", 401
        else:
            if len(authorizations) != 1:
                return None, "send exactly one `Authorization: Bearer <secret>` header", 401
            scheme, _, secret = authorizations[0].strip().partition(" ")
            secret = secret.strip()
            if scheme.lower() != "bearer" or not secret:
                return None, "the Authorization header must be `Bearer <secret>`", 401
            digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
            matches = [o for o in registry.operators.values()
                       if o.vote_key and hmac.compare_digest(digest, o.vote_key)]
            if len(matches) != 1:
                return None, ("the bearer secret matches no operator of the profile"
                              if not matches else
                              "the bearer secret matches more than one operator, so "
                              "it identifies none of them"), 401
            operator = matches[0]
        from .quorum import _lifetime_refusal  # noqa: PLC0415 - one lifetime rule

        lapsed = _lifetime_refusal(operator, _now_ms())
        if lapsed is not None:
            return None, lapsed.message, 401
        return operator, "", 200


class CallerBinding:
    """Serializes dispatch and binds the session to the caller for one request."""

    def __init__(self, server_module, registry_of) -> None:
        from .operator import Operator  # noqa: PLC0415

        self._server = server_module
        # the registry is read per request: the profile file can change
        self._registry_of = registry_of
        self.lock = DispatchLock()   # shared with http_face (issue #1488)
        self.sentinel = Operator(token=NO_CALLER)
        self._saved: list = []

    def _apply(self, session, operator) -> None:
        session.operator = operator
        session.operator_registry = self._registry_of()
        session.operator_bound_by = BOUND_BY_TRANSPORT

    def install(self) -> None:
        session = self._server.SESSION
        self._saved = [session, getattr(session, "operator", None),
                       getattr(session, "operator_registry", None),
                       hasattr(session, "operator_bound_by"),
                       getattr(session, "operator_bound_by", None)]
        with self.lock:
            self._apply(session, self.sentinel)

    def uninstall(self) -> None:
        if not self._saved:
            return
        original, operator, registry, had_bound_by, bound_by = self._saved
        with self.lock:
            for session in {id(s): s for s in (original, self._server.SESSION)}.values():
                session.operator = operator
                session.operator_registry = registry
                if had_bound_by:
                    session.operator_bound_by = bound_by
                elif hasattr(session, "operator_bound_by"):
                    del session.operator_bound_by
        self._saved = []

    @contextmanager
    def as_caller(self, operator, *, blocking: bool = True, before=None):
        """Hold the dispatch lock with the session bound to `operator`. With
        `blocking=False`, yields None instead of waiting when the lock is held."""
        if not self.lock.acquire(blocking):
            yield None
            return
        try:
            session = self._server.SESSION
            if before is not None:
                before(session)
            self._apply(session, operator)
            try:
                yield session
            finally:
                # a fork can replace the module's SESSION inside a request
                for s in {id(x): x for x in (session, self._server.SESSION)}.values():
                    self._apply(s, self.sentinel)
        finally:
            self.lock.release()


class _CallerView:
    """The session as `operator.decide` sees it for one caller, WITHOUT taking the
    dispatch lock: only `operator` differs, every other read goes to the session.
    Used for the one verb that must not queue behind a busy session."""

    def __init__(self, session, operator, registry) -> None:
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "operator_registry", registry)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_session"), name)


class HaltLatch:
    """The item-443 E-Stop latch this transport arms, so an E-Stop reaches the
    runtime without taking the dispatch lock."""

    def __init__(self) -> None:
        self.path: str | None = None
        self._runtime = None
        self._previous = None
        self._own_dir: str | None = None

    def arm(self) -> None:
        from .session import SessionError, _backend  # noqa: PLC0415

        try:
            runtime = _backend()[1]
        except SessionError:
            return  # no runtime installed: nothing can be loaded, nothing to halt
        self._runtime = runtime
        self._previous = getattr(runtime, "_ESTOP_LATCH", None)
        existing = runtime.estop_latch_path()
        if existing:
            self.path = existing  # an operator's own latch keeps working
            return
        self._own_dir = tempfile.mkdtemp(prefix="revl-mcp-http-")
        self.path = os.path.join(self._own_dir, "estop.latch")
        runtime.arm_estop_latch(self.path)

    def disarm(self) -> None:
        if self._runtime is not None and self._own_dir is not None:
            self._runtime.arm_estop_latch(self._previous)
        if self._own_dir is not None:
            shutil.rmtree(self._own_dir, ignore_errors=True)
        self._runtime = self._own_dir = self.path = None

    def record(self) -> dict | None:
        from ..estop import read_latch  # noqa: PLC0415

        return read_latch(self.path) if self.path else None

    def engage(self, reason: str, operator: str) -> tuple[dict, bool]:
        """Write the latch; `(record, already_halted)`. The first press wins."""
        record = {"halted": True, "verdict": "halted", "reason": reason,
                  "operator": operator, "at": time.time(), "resumable": False,
                  "reconcile": "revl recover --wal <file>"}
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self.record() or record, True
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        return record, False


# ---------------------------------------------------------------- dispatchers

def _modern_capabilities(capabilities: dict) -> dict:
    """The stdio capabilities, less what needs the notification stream this
    slice does not serve (`listChanged`, `subscribe`)."""
    out = {}
    for name, value in (capabilities or {}).items():
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in ("listChanged", "subscribe")}
        out[name] = value
    return out


class ServerDispatcher:
    """`revl mcp serve`: the compiler server's own `handle`."""

    def __init__(self, server_module) -> None:
        self.server = server_module

    def handle(self, message: dict):
        return self.server.handle(message)

    def describe(self) -> dict:
        return self.server.handle({"jsonrpc": "2.0", "id": 0,
                                   "method": "initialize", "params": {}})["result"]

    def advertised_tools(self) -> list:
        return list(self.server._ADVERTISED)


class ProxyDispatcher:
    """`revl mcp proxy`: the proxy's own `handle`."""

    def __init__(self, proxy) -> None:
        self.proxy = proxy
        self.server = proxy.server

    def handle(self, message: dict):
        return self.proxy.handle(message)

    def describe(self) -> dict:
        return self.proxy.handle({"jsonrpc": "2.0", "id": 0,
                                  "method": "initialize", "params": {}})["result"]

    def advertised_tools(self) -> list:
        return self.proxy._advertised()


# ---------------------------------------------------------------- the wire

def _decode_header_value(value: str) -> str | None:
    """A mirrored header value, with the `=?base64?...?=` sentinel decoded."""
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[len("=?base64?"):-2], validate=True) \
                .decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    if any(ord(c) < 0x20 and c != "\t" or ord(c) > 0x7E for c in value):
        return None
    return value


def _one_header(headers, name: str) -> str | None:
    values = _header_values(headers, name)
    return values[0] if len(values) == 1 else None


def _header_params(schema) -> list[tuple[str, tuple[str, ...]]]:
    """Every `x-mcp-header` annotation reachable through `properties` keys only,
    as `(header name, property path)`."""
    found: list = []

    def walk(node, path):
        props = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(props, dict):
            return
        for key, sub in props.items():
            if not isinstance(sub, dict):
                continue
            name = sub.get("x-mcp-header")
            if isinstance(name, str) and name:
                found.append((name, path + (key,)))
            walk(sub, path + (key,))

    walk(schema, ())
    return found


def _param_text(value) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    return None


def _param_headers_refusal(tool: dict | None, arguments, headers) -> str | None:
    if not tool or not isinstance(arguments, dict):
        return None
    for name, path in _header_params(tool.get("inputSchema")):
        value = arguments
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            continue
        header = _one_header(headers, f"Mcp-Param-{name}")
        if header is None:
            return f"missing or repeated Mcp-Param-{name} header for a value in the body"
        decoded = _decode_header_value(header)
        expected = _param_text(value)
        if decoded is None or expected is None:
            return f"Mcp-Param-{name} is not a valid header value"
        if isinstance(value, int) and not isinstance(value, bool):
            try:
                if float(decoded) != float(value):
                    return f"Mcp-Param-{name} does not match the body"
            except ValueError:
                return f"Mcp-Param-{name} does not match the body"
        elif decoded != expected:
            return f"Mcp-Param-{name} does not match the body"
    return None


def _rpc_error(request_id, code: int, message: str, data=None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    body = {"jsonrpc": "2.0", "error": error}
    if request_id is not None:
        body["id"] = request_id
    return body


def _status_for(error_code: int) -> int:
    if error_code == -32601:
        return 404
    if error_code in (-32700, -32600, -32602, HEADER_MISMATCH, UNSUPPORTED_VERSION, -32021):
        return 400
    if error_code == -32603:
        return 500
    return 200


def _reserved_meta_key(key: str) -> bool:
    prefix, sep, _ = key.partition("/")
    labels = prefix.split(".") if sep else []
    return len(labels) >= 2 and labels[1] in ("modelcontextprotocol", "mcp")


class HttpTransport:
    """One `POST /mcp` endpoint over a dispatcher, with per-request identity."""

    def __init__(self, dispatcher, *, registry=None, exposure: Exposure,
                 auth: str = "bearer", server_module=None,
                 profile_path: str | None = None,
                 profile_settle_ms: int = DEFAULT_SETTLE_MS) -> None:
        if server_module is None:
            from . import server as server_module  # noqa: PLC0415
        if auth == "mtls" and not exposure.tls_client_ca:
            raise TransportError("--auth mtls needs --tls-client-ca: the client "
                                 "certificate is the identity")
        self.dispatcher = dispatcher
        self.server = server_module
        self.exposure = exposure
        try:
            source = (ProfileSource(profile_path, settle_ms=profile_settle_ms)
                      if profile_path else None)
        except (ProfileUnavailable, ValueError) as error:
            raise TransportError(str(error)) from error
        self.authenticator = Authenticator(registry, auth, source=source)
        self.binding = CallerBinding(server_module, lambda: self.authenticator.registry)
        self.latch = HaltLatch()
        self.listener: Listener | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> tuple[str, int]:
        try:
            self.listener = Listener(self.exposure, _handler_class(self))
        except ExposureError as error:
            raise TransportError(str(error)) from error
        self.latch.arm()
        self.binding.install()
        self._thread = threading.Thread(target=self.listener.serve_forever,
                                        daemon=True, name="revl-mcp-http")
        self._thread.start()
        return self.exposure.host, self.exposure.port_in_use

    def stop(self) -> None:
        if self.listener is not None:
            self.listener.shutdown()
            self.listener.server_close()
            self.listener = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self.binding.uninstall()
        self.latch.disarm()

    def serve_forever(self, *, stderr=None) -> int:
        stderr = stderr or sys.stderr
        try:
            host, port = self.start()
        except TransportError as error:
            print(f"error: {error}", file=stderr)
            return 1
        scheme = "https" if self.exposure.tls else "http"
        print(f"revl mcp: MCP {PROTOCOL_VERSION} on {scheme}://{host}:{port}{ENDPOINT} "
              f"(auth: {self.authenticator.mode}; one request at a time)", file=stderr)
        if self.latch.path:
            # revl_estop is accepted even while the profile settles or is broken
            # (under the last adopted profile); the latch halts with no request
            # at all, the one path that needs no adopted profile
            print(f"revl mcp: out-of-band E-Stop: revl estop --latch {self.latch.path}",
                  file=stderr)
        try:
            while self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return 0

    # -- one request ---------------------------------------------------------

    def respond(self, request, method: str) -> tuple[int, dict | None, list]:
        """(status, JSON body or None, extra headers) for one HTTP request."""
        # every refusal before the body is read closes the connection: the unread
        # body must not be parsed as the next request
        close = ("Connection", "close")
        refusal = request_refusal(request.headers, self.exposure)
        if refusal is not None:
            return 403, _rpc_error(None, -32600, refusal), [close]
        if request.path.split("?", 1)[0] != ENDPOINT:
            return 404, _rpc_error(None, -32601, f"the MCP endpoint is {ENDPOINT}"), \
                [close]
        if method != "POST":
            return 405, _rpc_error(None, -32600,
                                   "only POST is served (MCP 2026-07-28 has no GET "
                                   "stream and no session to DELETE)"), \
                [("Allow", "POST"), close]
        peer = request.request.getpeercert() \
            if hasattr(request.request, "getpeercert") else None
        operator, why, status = self.authenticator.authenticate(request.headers, peer)
        if operator is None and status != 503:
            challenge = ('Bearer realm="revl", error="invalid_token"'
                         if self.authenticator.mode == "bearer" else 'Bearer realm="revl"')
            return 401, _rpc_error(None, -32600, why), \
                [("WWW-Authenticate", challenge), close]
        from .http_face import _frame_request  # noqa: PLC0415 - one framing reader

        length, framing = _frame_request(request.headers, MAX_BODY)
        if framing is not None:
            return framing.status, _rpc_error(None, -32600, framing.message), [close]
        body = request.rfile.read(length) if length else b""
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, _rpc_error(None, -32700, "parse error"), []
        if operator is None:
            # the profile is settling or broken: every request is refused except
            # an E-Stop from an operator authorized under the LAST ADOPTED
            # profile. E-Stop is never fenced, and it only stops things.
            operator = self._estop_fallback(message, request.headers, peer)
            if operator is None:
                return 503, _rpc_error(None, -32603, why), [close]
        return self.process(message, operator, request.headers)

    def _estop_fallback(self, message, headers, peer):
        last = getattr(self.authenticator.source, "last_adopted", None)
        if not is_estop(message) or last is None:
            return None
        operator, _why, _status = self.authenticator.authenticate(
            headers, peer, registry=last)
        return operator

    def process(self, message, operator, headers) -> tuple[int, dict | None, list]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" \
                or not isinstance(message.get("method"), str) \
                or "result" in message or "error" in message:
            return 400, _rpc_error(None, -32600, "the body must be one JSON-RPC "
                                                 "request or notification"), []
        method = message["method"]
        notification = "id" not in message
        request_id = message.get("id")
        if not notification and (request_id is None or isinstance(request_id, bool)
                                 or not isinstance(request_id, (str, int))):
            return 400, _rpc_error(None, -32600, "a request id must be a string or "
                                                 "an integer"), []
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return 400, _rpc_error(request_id, -32602, "params must be an object"), []
        if method == "initialize":
            requested = params.get("protocolVersion")
            return 400, _rpc_error(
                request_id, UNSUPPORTED_VERSION,
                f"this server speaks MCP {PROTOCOL_VERSION} only, which has no "
                f"`initialize`: send per-request `_meta` instead",
                {"supported": list(SUPPORTED_VERSIONS), "requested": requested}), []
        if notification:
            # MCP 2026-07-28 defines no client notification over HTTP; accept it
            # (202, as the transport requires) and act on nothing
            return 202, None, []
        meta = params.get("_meta")
        meta = meta if isinstance(meta, dict) else {}
        version = meta.get(META_VERSION)
        if not isinstance(version, str) or not isinstance(meta.get(META_CAPABILITIES), dict):
            return 400, _rpc_error(request_id, -32602,
                                   f"_meta must carry {META_VERSION} and "
                                   f"{META_CAPABILITIES}"), []
        header_version = _one_header(headers, "MCP-Protocol-Version")
        if header_version is None or header_version.strip() != version:
            return 400, _rpc_error(request_id, HEADER_MISMATCH,
                                   "MCP-Protocol-Version header missing or does not "
                                   "match _meta"), []
        if version not in SUPPORTED_VERSIONS:
            return 400, _rpc_error(request_id, UNSUPPORTED_VERSION,
                                   "Unsupported protocol version",
                                   {"supported": list(SUPPORTED_VERSIONS),
                                    "requested": version}), []
        header_method = _one_header(headers, "Mcp-Method")
        if header_method is None or header_method.strip() != method:
            return 400, _rpc_error(request_id, HEADER_MISMATCH,
                                   "Mcp-Method header missing or does not match "
                                   "the body"), []
        if method in _NAMED_METHODS:
            header_name = _one_header(headers, "Mcp-Name")
            decoded = _decode_header_value(header_name) if header_name is not None else None
            if decoded is None or decoded != params.get(_NAMED_METHODS[method]):
                return 400, _rpc_error(request_id, HEADER_MISMATCH,
                                       "Mcp-Name header missing or does not match "
                                       "the body"), []
        if method in _NOT_SERVED:
            return 404, _rpc_error(request_id, -32601,
                                   f"{method} is not served by this transport yet"), []
        if method == "server/discover":
            return 200, self._discover(request_id), []

        clean = dict(message)
        clean_params = dict(params)
        rest = {k: v for k, v in meta.items() if not _reserved_meta_key(k)}
        if rest:
            clean_params["_meta"] = rest
        else:
            clean_params.pop("_meta", None)
        clean["params"] = clean_params

        if method == "tools/call":
            name = clean_params.get("name")
            if name in CAST_TOOLS:
                refused = self._cast_refusal(clean_params.get("arguments"), operator)
                if refused is not None:
                    return 200, self._result(request_id, refused), []
            if name == "revl_estop":
                return 200, self._estop(clean, operator), []

        with self.binding.as_caller(operator, before=self._complete_halt) as session:
            del session
            if method == "tools/call":
                tools = {t.get("name"): t for t in self.dispatcher.advertised_tools()}
                bad = _param_headers_refusal(tools.get(clean_params.get("name")),
                                             clean_params.get("arguments"), headers)
                if bad is not None:
                    return 400, _rpc_error(request_id, HEADER_MISMATCH, bad), []
            response = self.dispatcher.handle(clean)
        if response is None:
            return 202, None, []
        if "error" in response:
            return _status_for(response["error"].get("code", 0)), response, []
        return 200, self._shape(response), []

    # -- the pieces ------------------------------------------------------------

    def _server_info(self) -> dict:
        return (self.dispatcher.describe().get("serverInfo")
                or {"name": "revl", "version": "2.0"})

    def _shape(self, response: dict) -> dict:
        result = response.get("result")
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("resultType", "complete")
            meta = dict(result.get("_meta") or {})
            meta.setdefault(META_SERVER_INFO, self._server_info())
            result["_meta"] = meta
            response = {**response, "result": result}
        return response

    def _result(self, request_id, payload: dict) -> dict:
        return self._shape({"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": not payload.get("ok", False),
            "structuredContent": payload}})

    def _discover(self, request_id) -> dict:
        described = self.dispatcher.describe()
        result = {"resultType": "complete",
                  "supportedVersions": list(SUPPORTED_VERSIONS),
                  "capabilities": _modern_capabilities(described.get("capabilities")),
                  "_meta": {META_SERVER_INFO: described.get("serverInfo")
                            or {"name": "revl", "version": "2.0"}}}
        if described.get("instructions"):
            result["instructions"] = described["instructions"]
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _cast_refusal(self, arguments, operator) -> dict | None:
        arguments = arguments if isinstance(arguments, dict) else {}
        named = arguments.get("asToken")
        if named in (None, "", operator.token):
            return None
        return self.server._session_error(
            f"over the HTTP transport a caller casts only as itself: this request "
            f"is authenticated as `{operator.token}` and names `{named}`. Each "
            f"operator casts on its own authenticated request (roadmap item 471, "
            f"issue #979)")

    def _complete_halt(self, session) -> None:
        """Finish a halt that was latched while the session was busy, on the
        thread that now owns the session, before anything else is dispatched."""
        record = self.latch.record()
        if record is None or not getattr(session, "loaded", False) \
                or getattr(session, "halted", False):
            return
        from .session import SessionError  # noqa: PLC0415

        try:
            session.estop(record.get("reason") or "operator halt",
                          record.get("operator") or "unknown")
        except SessionError:
            pass

    def _estop(self, message: dict, operator) -> dict:
        request_id = message.get("id")
        with self.binding.as_caller(operator, blocking=False,
                                    before=self._complete_halt) as session:
            if session is not None:
                return self._shape(self.dispatcher.handle(message))
        # the session is busy: decide, then latch, without waiting for it
        from . import operator as _operator  # noqa: PLC0415

        arguments = (message.get("params") or {}).get("arguments") or {}
        view = _CallerView(self.server.SESSION, operator, self.authenticator.registry)
        decision = _operator.decide(view, "revl_estop", arguments)
        if decision.gated and not decision.allowed:
            return self._result(request_id, self.server._refused_by_operator(decision))
        if self.latch.path is None:
            return self._result(request_id, self.server._session_error(
                "the session is busy and no E-Stop latch is armed, so the halt "
                "cannot be delivered without waiting"))
        reason = arguments.get("reason") or "operator halt"
        record, already = self.latch.engage(reason, operator.token)
        payload = {"ok": True, "halted": True, "latched": True,
                   "alreadyHalted": already, "reason": record.get("reason"),
                   "operator": record.get("operator"),
                   "note": ("the session was busy, so the halt was latched: every "
                            "crossing seam now refuses, the request in flight is "
                            "not interrupted, and the next request completes the "
                            "halt before anything else is dispatched (item 443). "
                            "Read the inventory with revl_estop_report")}
        return self._result(request_id, payload)


def _handler_class(transport: HttpTransport):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "revl-mcp"
        sys_version = ""

        def _serve(self, method: str) -> None:
            try:
                status, body, headers = transport.respond(self, method)
            except Exception as exc:  # noqa: BLE001 - never a traceback on the wire
                status, body, headers = 500, _rpc_error(
                    None, -32603, f"{type(exc).__name__}: {exc}"), []
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
                if name == "Connection" and value == "close":
                    self.close_connection = True
            if body is None:
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = json.dumps(body).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            self._serve("POST")

        def do_GET(self) -> None:  # noqa: N802
            self._serve("GET")

        def do_DELETE(self) -> None:  # noqa: N802
            self._serve("DELETE")

        def do_PUT(self) -> None:  # noqa: N802
            self._serve("PUT")

        def do_PATCH(self) -> None:  # noqa: N802
            self._serve("PATCH")

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve("HEAD")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._serve("OPTIONS")

        def log_message(self, *_args) -> None:
            pass

    return _Handler

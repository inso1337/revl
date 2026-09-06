"""Serve a composition's OWN operations over HTTP — item 424 gap (c), C1.

This is the SERVER face `revl export client` pairs with (D-424c.6). It is the
same fourth-quadrant projection `revl serve --mcp` makes of a booted
composition (`composed.py`), put on a different transport: each provided
operation becomes ``POST /<composition>/<key>/<op>`` and the request/response
bodies are the CANONICAL VALUE ENCODING the four bridges already speak
(docs/interop-bridge.md). "revl serve --http adds a transport to an existing
projection and decides nothing" — the operation set, the checked emission
hints, and the wire shape are all the compiler's, not this module's.

Why it round-trips to the placement bridge by construction. The bridge's
provider dispatch loop replies ``{"ok": True, "value": _encode_value(result)}``
(`backends/python/bridge.py`); this face replies with the identical shape,
using the identical encoder (mirrored below, `_encode_value`, so the src tree
carries no import-time coupling to the backend — the same reason
`export_client` keeps its own copy of a small backend helper). A value
therefore marshals the same bytes here as over the placement seam, and the
`export client` TS types — whose shape IS that encoding — read it without a
second marshalling spec.

What it deliberately is NOT (D-424c.8). The server sits on one side of a seam
and holds no gate over any callee it in turn reaches; it makes no safety claim
about them, and there is no verified-remote badge. It advertises the GATE
FRONTIER it was projected under (item 338) so a client can pin it: a refusal is
authoritative, an admission is a compile-time judgment scoped to that frontier,
never a runtime-confinement claim.

The wire layer (routing, decode, `_encode_value`, error mapping) is a pure
function of the request and runs with no runtime — `HttpComposedServer.dispatch`
is exercised directly and over a real loopback socket with a stub session.
Only standing a LIVE composition up (`serve_http`) imports the cordis-py
runtime, exactly as `serve_composition` does.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..gate import gate_version
from .approval import ApprovalRequired, two_step_payload
from .composed import ComposedServer
from .session import SessionError

# HTTP status codes this face speaks. A successful call is 200; a call the
# SESSION refuses (an unknown key, a runtime fault surfaced as a SessionError)
# is 400; a route with no operation is 404; a wrong method on an operation path
# is 405; a class-(c) crossing awaiting a human yes is 403 with the ticket in
# the body (fail-closed: nothing fired); a callee that raised is 500.
_OK = 200
_BAD_REQUEST = 400
_FORBIDDEN = 403
_NOT_FOUND = 404
_METHOD_NOT_ALLOWED = 405
_SERVER_ERROR = 500


def _encode_value(value):
    """Canonical wire encoding, byte-identical to
    `backends/python/bridge.py`'s `_encode_value`.

    Scalars, lists and dicts pass through with their items recursively encoded
    (`Opt[T]` is the bare value or ``None``); a `@dataclass` is a record; an
    emitted ADT / `Result` case (`_is_emitted_case`) becomes
    ``{"$kind": Case, "$value": payload}`` (``$value`` omitted for a nullary
    case). Any other object — an opaque host value, a live handle — is REFUSED
    fail-closed rather than shipped as a dead tag, the same guarantee the bridge
    keeps. Kept in lockstep with the backend copy: the two are one wire.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_encode_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode_value(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode_value(getattr(value, f.name))
                for f in dataclasses.fields(value)}
    if not _is_emitted_case(value):
        raise TypeError(
            f"cannot marshal a {type(value).__name__!r} across the HTTP seam: it "
            "is not a scalar, list, record, Opt or emitted case (the canonical "
            "encoding ships only value-semantic data; an opaque host value or a "
            "live handle crosses by handle, never by copy)")
    tagged = {"$kind": type(value).__name__}
    if hasattr(value, "value"):
        tagged["$value"] = _encode_value(value.value)
    return tagged


def _is_emitted_case(value) -> bool:
    """Is `value` an emitted ADT / `Result` case (vs an opaque host object)?

    Mirrors `backends/python/bridge.py`'s `_is_emitted_case` exactly: a case is
    a plain, NON-dataclass, slots-only class whose only per-instance datum is an
    optional ``value`` payload. A record (dict or `@dataclass`) is handled
    before this point; an opaque object with a per-instance ``__dict__`` or other
    slots is refused rather than shipped as a dead ``$kind`` tag.
    """
    cls = type(value)
    if cls is object or dataclasses.is_dataclass(cls):
        return False
    if hasattr(value, "__dict__"):
        return False
    slots: set[str] = set()
    for klass in cls.__mro__:
        declared = getattr(klass, "__slots__", ())
        slots.update((declared,) if isinstance(declared, str) else declared)
    return slots <= {"value"}


class HttpComposedServer:
    """A booted composition's provided operations, addressable over HTTP.

    Reuses `ComposedServer`'s projection (one source of truth for the route
    table and the compiler-derived hints) and dispatches each ``POST`` onto the
    same `Session.call`. `dispatch` is a pure `(method, path, body) -> (status,
    payload)` function so the whole wire layer is testable with no runtime.
    """

    def __init__(self, session, composition: str = "revl") -> None:
        self.composition = composition
        self._composed = ComposedServer(session, composition=composition)
        self.session = session
        # tool name is `<composition>.<key>.<op>`; index the same routes by the
        # HTTP path `/<composition>/<key>/<op>`, and keep the advertised hints
        # for the manifest so the fourth quadrant's compiler-derived
        # readOnly/emission classification rides this transport too.
        self._by_path: dict[str, tuple[str, str, list[str]]] = {}
        self._hints: dict[str, dict] = {}
        for tool in self._composed._advertised:
            key, method, params = self._composed._routes[tool["name"]]
            path = f"/{composition}/{key}/{method}"
            self._by_path[path] = (key, method, params)
            self._hints[path] = tool
        self.frontier = gate_version().get("frontier", "")

    # -- the manifest (GET /) ---------------------------------------------
    def _manifest(self) -> dict:
        operations = []
        for path, (key, method, params) in self._by_path.items():
            tool = self._hints.get(path) or {}
            annotations = tool.get("annotations") or {}
            provenance = tool.get("x-revl") or {}
            operations.append({
                "path": path,
                "key": key,
                "operation": method,
                "params": params,
                # compiler-derived, not author-asserted (the fourth-quadrant
                # guarantee, carried onto HTTP): read-only iff the checker
                # refused unreverted mutation.
                "readOnly": annotations.get("readOnlyHint") is True,
                "emission": provenance.get("classification") == "emission",
            })
        return {
            "composition": self.composition,
            # item 338: the covered surface this face was projected under. A
            # client pins it; a refusal is authoritative, an admission is scoped
            # to this frontier and is not a runtime-confinement claim.
            "frontier": self.frontier,
            "operations": operations,
            # D-424c.8: LOCAL contract only. This face is typed and bounded on
            # THIS side; it makes no safety claim about what any callee it
            # reaches ultimately runs, and there is no verified-remote badge. A
            # mutual guarantee between two revl peers is `revl contract
            # export`/`check`, not this.
            "note": ("LOCAL contract only: these operations are typed and bounded "
                     "on this side. The server makes no claim about what a callee "
                     "ultimately does; there is no verified-remote badge."),
        }

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, method: str, path: str,
                 body: bytes = b"") -> tuple[int, dict]:
        """Route one HTTP request. Returns `(status, json-able payload)`."""
        clean = path.split("?", 1)[0].rstrip("/") or "/"
        if clean == "/":
            if method != "GET":
                return _METHOD_NOT_ALLOWED, _err(
                    "the manifest is `GET /`", code="method")
            return _OK, self._manifest()

        route = self._by_path.get(clean)
        if route is None:
            return _NOT_FOUND, _err(
                f"no operation at `{clean}` — GET / for the served operations",
                code="route")
        if method != "POST":
            return _METHOD_NOT_ALLOWED, _err(
                f"`{clean}` is an operation — call it with POST", code="method")

        key, op, param_names = route
        args, problem = _decode_args(body, param_names)
        if problem is not None:
            return _BAD_REQUEST, _err(problem, code="request")

        try:
            result = self.session.call(key, op, args)
        except SessionError as error:
            return _BAD_REQUEST, {"ok": False, "diagnostics": [{
                "severity": "error", "code": "REVL", "category": "session",
                "message": str(error)}]}
        except ApprovalRequired as exc:
            # fail-closed: the crossing did not fire. Hand back the ticket so an
            # operator can mint the yes; this wire, like the MCP one, carries no
            # approve verb of its own.
            payload = two_step_payload(
                exc.ticket,
                how_to_approve="This HTTP face serves the composition's own "
                               "operations only — there is no approve verb on "
                               "this wire. Relay the ticket to the operator, who "
                               "mints the yes against the same session; the "
                               "identical re-issue then fires once.")
            return _FORBIDDEN, payload
        except Exception as exc:  # the callee raised — a result, not a crash
            return _SERVER_ERROR, {"ok": False, "raised": True, "diagnostics": [{
                "severity": "error", "code": "REVL", "category": "runtime",
                "message": f"{type(exc).__name__}: {exc}"}]}

        # the placement bridge's exact reply shape, same encoder: a value
        # marshals identical bytes here as over the placement seam.
        return _OK, {"ok": True, "value": _encode_value(result.get("result"))}


def _decode_args(body: bytes, param_names: list[str]) -> tuple[list, str | None]:
    """Request body -> the positional argument list, or a problem string.

    The body is the operation's arguments as canonical JSON: a bare JSON ARRAY
    (positional, the shape the generated client's `transport.call(method, args)`
    sends), or an object `{"args": [...]}`, or empty for a no-argument call.
    """
    if not body or not body.strip():
        return [], None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        return [], f"request body is not JSON ({exc})"
    if isinstance(parsed, dict) and "args" in parsed:
        parsed = parsed["args"]
    if not isinstance(parsed, list):
        return [], ("request body must be a JSON array of positional arguments "
                    "(or an object `{\"args\": [...]}`)")
    if len(parsed) > len(param_names):
        return [], (f"too many arguments: got {len(parsed)}, the operation takes "
                    f"{len(param_names)} ({', '.join(param_names) or 'none'})")
    return parsed, None


def _err(message: str, *, code: str) -> dict:
    return {"ok": False, "diagnostics": [{
        "severity": "error", "code": "REVL", "category": code,
        "message": message}]}


# ------------------------------------------------------------ HTTP plumbing

def _make_handler(server: HttpComposedServer):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload = server.dispatch(method, self.path, body)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._respond("POST")

        def log_message(self, *_args) -> None:  # keep the server quiet
            pass

    return _Handler


def build_http_server(server: HttpComposedServer, host: str,
                      port: int) -> ThreadingHTTPServer:
    """Bind a `ThreadingHTTPServer` for `server` (a port of 0 picks a free one).

    Split out from `serve_http` so a test can bind a stub-backed face to
    loopback and drive it over a real socket with no cordis runtime.
    """
    return ThreadingHTTPServer((host, port), _make_handler(server))


def serve_http(ir: dict, config: dict | None = None, *,
               composition: str = "revl", host: str = "127.0.0.1",
               port: int = 8080) -> int:
    """Boot `ir` into a live session and serve its operations over HTTP.

    Booting is admission, so this loads through the same `Session.load` the MCP
    face and `revl_load` run; the caller (the CLI entry) owns the config
    preflight, mirroring `revl run` and `serve_composition`.
    """
    from .session import Session  # noqa: PLC0415 — lazy: Session pulls cordis

    session = Session()
    session.load(ir, config or {}, origin=None)
    face = HttpComposedServer(session, composition=composition)
    httpd = build_http_server(face, host, port)
    bound_host, bound_port = httpd.server_address[:2]
    print(f"revl serve --http: {composition} on http://{bound_host}:{bound_port}",
          file=sys.stderr)
    print(f"  gate frontier: {face.frontier}", file=sys.stderr)
    print(f"  {len(face._by_path)} operation(s); GET / for the manifest. LOCAL "
          "contract only — no safety claim about any callee.", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0

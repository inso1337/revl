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
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..gate import gate_version
from .approval import ApprovalRequired, two_step_payload
from .composed import ComposedServer
from .session import SessionError

# HTTP status codes this face speaks. A successful call is 200; a call the
# SESSION refuses (an unknown key, a runtime fault surfaced as a SessionError)
# is 400; a route with no operation is 404; a wrong method on an operation path
# is 405; a class-(c) crossing awaiting a human yes is 403 with the ticket in
# the body (fail-closed: nothing fired); a callee that raised is 500; a body
# larger than this face accepts is 413 (`stdlib/framing.rvl`'s `status_for`).
_OK = 200
_NO_CONTENT = 204
_NOT_MODIFIED = 304
_BAD_REQUEST = 400
_FORBIDDEN = 403
_NOT_FOUND = 404
_METHOD_NOT_ALLOWED = 405
_PAYLOAD_TOO_LARGE = 413
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


# ------------------------------------------------------------ item 457: routes

_ROUTE_SCALARS = ("Str", "Int", "Bool")


@dataclasses.dataclass
class HttpReply:
    """A full HTTP reply the routed wire needs but the canonical fourth-quadrant
    JSON envelope cannot express: an arbitrary status, content type, extra
    headers, a raw body and the reason phrase for the status line.

    `status_text` is the handler's own reason phrase (`stdlib/http.rvl`'s
    `Response.status_text`, which that module documents as carried to the wire).
    Empty means the handler expressed no preference, and the status line falls
    back to this face's own table — the behaviour every reply that is not a
    routed `Response` keeps, so the head of a canonical/error reply is
    byte-identical to before. The phrase is the one part of the head the handler
    owns, so it is validated like a field value (`_response_refusal`) rather
    than trusted: it is written into the status line, and a CR or LF in it would
    end that line early."""
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: tuple = ()
    status_text: str = ""

    @classmethod
    def json(cls, status: int, payload) -> "HttpReply":
        return cls(status, json.dumps(payload).encode("utf-8"),
                   "application/json")


@dataclasses.dataclass
class _Route:
    """One routed operation, resolved from the IR `route` entry (item 457)."""
    method: str                 # upper-case HTTP verb
    template: str               # the `/notes/{id}` path template
    regex: "re.Pattern"         # template compiled to a full-path matcher
    key: str                    # the provided key on the composition
    op: str                     # the operation name
    param_order: list           # declared parameter names, in call order
    bind: dict                  # param name -> {kind, type, schema?, optional?}
    response: dict              # the return-rule classification
    auth: str | None            # "bearer" iff the op declares a Bearer param


def _template_regex(template: str) -> "re.Pattern":
    """Compile a `/notes/{id}` template to a full-path matcher whose named groups
    are the path parameters. A segment `{name}` matches one non-empty,
    slash-free segment; every other character is matched literally."""
    parts = []
    for token in re.split(r"(\{[^{}]*\})", template):
        if token.startswith("{") and token.endswith("}"):
            parts.append(f"(?P<{token[1:-1]}>[^/]+)")
        else:
            parts.append(re.escape(token))
    return re.compile("^" + "".join(parts) + "$")


def _coerce_scalar(raw: str, type_name: str):
    """Coerce a URL-string path/query value to the bound scalar's Python type, or
    raise `ValueError` naming why (a 400 the caller renders). JSON's own scalar
    reading, so `Int`/`Bool` match the body encoding."""
    if type_name == "Str":
        return raw
    if type_name == "Int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"expected an integer, got {raw!r}")
    if type_name == "Bool":
        if raw == "true":
            return True
        if raw == "false":
            return False
        raise ValueError(f"expected `true` or `false`, got {raw!r}")
    return raw


def _json_type_ok(value, json_type: str) -> bool:
    if json_type == "object":
        return isinstance(value, dict)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "null":
        return value is None
    return True


def _schema_error(value, schema, path: str = "$") -> str | None:
    """Validate ``value`` against the derived JSON-Schema subset `json_schema_for`
    emits (item 257 §3), returning the FIRST violation or ``None``. A runtime-free
    mirror of `backends/python/runtime.py`'s `_json_schema_error` (this module
    keeps its own copy of the small backend helper it needs, exactly as it does
    for `_encode_value`), so the wire layer validates with no runtime import."""
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return None if value == schema["const"] else \
            f"{path}: expected {schema['const']!r}"
    if "enum" in schema:
        return None if value in schema["enum"] else \
            f"{path}: {value!r} is not one of {schema['enum']!r}"
    if "oneOf" in schema:
        matches = [arm for arm in schema["oneOf"]
                   if _schema_error(value, arm, path) is None]
        if len(matches) == 1:
            return None
        if not matches:
            return f"{path}: value matches no arm of the union"
        return f"{path}: value is ambiguous, matching {len(matches)} union arms"
    if schema.get("nullable") and value is None:
        return None
    json_type = schema.get("type")
    if json_type is not None and not _json_type_ok(value, json_type):
        return f"{path}: expected {json_type}, got {type(value).__name__}"
    if json_type == "object" and isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                return f"{path}: missing required property {name!r}"
        extra = schema.get("additionalProperties", True)
        for pkey, item in value.items():
            if pkey in props:
                err = _schema_error(item, props[pkey], f"{path}.{pkey}")
                if err is not None:
                    return err
            elif extra is False:
                return f"{path}: unexpected property {pkey!r}"
    if json_type == "array" and isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                err = _schema_error(item, items, f"{path}[{i}]")
                if err is not None:
                    return err
    return None


def _header_values(headers, name: str) -> list:
    """Every value of `name`, in wire order.

    `Message.get_all` is the duplicate-visible reader, for the same reason
    `_frame_request` uses it: `.get()` answers with the FIRST occurrence and
    cannot see a doubled field at all. A plain mapping has no duplicates to
    report, but the same field can be spelled two ways in one, so both
    spellings are read -- a mapping carrying both IS the doubled case.
    """
    if headers is None:
        return []
    get_all = getattr(headers, "get_all", None)
    if get_all is not None:
        return list(get_all(name) or [])
    return [value for key in (name, name.lower())
            if (value := headers.get(key)) is not None]


def _bearer_token(headers) -> str | None:
    """The credential in an `Authorization: Bearer <token>` header, or None. The
    router binds it UNTOUCHED as an untrusted claim; it decides nothing.
    A request that presents MORE THAN ONE `Authorization` field presents no
    unambiguous credential, so no claim is made and the handler's `validate`
    denies -- the same answer a missing credential gets. Reading only the
    first of several would let this face and the `Request` escape hatch
    (which carries every header, per `stdlib/http.rvl`'s `Header` list)
    disagree about what one request presented, and would hand the first
    field the authority of the credential an intermediary validated.
    `stdlib/framing.rvl`'s `header_count` is the value-side reader for the
    same question.
    """
    values = _header_values(headers, "Authorization")
    if len(values) != 1:
        return None
    raw = values[0]
    if not raw:
        return None
    parts = raw.split(None, 1)
    if not parts:
        return None
    if parts[0].lower() == "bearer":
        # the scheme with no credentials is no credential, not a credential
        # whose value is the word "Bearer"
        return parts[1] if len(parts) == 2 else None
    return raw


class HttpComposedServer:
    """A booted composition's provided operations, addressable over HTTP.

    Reuses `ComposedServer`'s projection (one source of truth for the canonical
    route table and the compiler-derived hints) and dispatches each ``POST`` onto
    the same `Session.call`. A provided operation that HEADS a `route` clause
    (item 457) is ALSO reachable at its declared method+path: `dispatch_http`
    matches routed operations first and falls back to today's
    ``POST /<composition>/<key>/<op>`` for unrouted ones, so nothing changes for
    a service with no `route` clause. The wire layer is a pure function of the
    request (`dispatch`, `dispatch_http`) and is testable with no runtime.
    """

    def __init__(self, session, composition: str = "revl", decode=None) -> None:
        self.composition = composition
        self._composed = ComposedServer(session, composition=composition)
        self.session = session
        # `decode` rebuilds native ADT/Result case instances from the canonical
        # wire encoding, needed only to construct a typed `Request` (the escape
        # hatch) whose `method`/`body` are variants. Identity by default so the
        # common path — scalars, records, the bearer record — stays runtime-free;
        # `serve_http` wires the live module's decoder for the escape hatch.
        self._decode = decode or (lambda v: v)
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
        # item 457: the declared HTTP routes, resolved from the IR `route` entry.
        self._routes_457: list[_Route] = self._build_routes(session.ir or {})
        self.frontier = gate_version().get("frontier", "")

    # -- route table (item 457) -------------------------------------------
    def _build_routes(self, ir: dict) -> list["_Route"]:
        services = ir.get("services") or {}
        routes: list[_Route] = []
        for component in ir.get("components") or []:
            for key, service_name in (component.get("provides") or {}).items():
                service = services.get(service_name) or {}
                for op_name, op in (service.get("methods") or {}).items():
                    route = op.get("route")
                    if not route:
                        continue
                    routes.append(_Route(
                        method=route["method"].upper(),
                        template=route["path"],
                        regex=_template_regex(route["path"]),
                        key=key,
                        op=op_name,
                        param_order=[p["name"] for p in (op.get("params") or [])],
                        bind=route.get("bind") or {},
                        response=route.get("response") or {"kind": "plain",
                                                           "type": "Unit"},
                        auth=route.get("auth"),
                    ))
        return routes

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
            # item 457: the declared HTTP routes (method + template) served
            # ALONGSIDE the canonical operation paths. `auth` is a
            # documentation-only marker (a Bearer-bearing operation); the router
            # grants nothing — authorization is the handler's explicit step.
            "routes": [
                {"method": r.method, "path": r.template,
                 "key": r.key, "operation": r.op,
                 **({"auth": r.auth} if r.auth else {})}
                for r in self._routes_457
            ],
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
            # `raw=True`: this face's contract is the placement bridge's canonical
            # encoding (`_encode_value` below), so it must see the live runtime
            # value — an ADT / `Result` case as its native instance, not the
            # lossy `_plain` `repr` the MCP wire renders for agent inspection.
            result = self.session.call(key, op, args, raw=True)
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

    # -- routed dispatch (item 457) ---------------------------------------
    def dispatch_http(self, method: str, path: str, body: bytes = b"",
                      headers=None) -> HttpReply:
        """Route one HTTP request, honouring `route` clauses first (item 457) and
        falling back to the canonical fourth-quadrant dispatch otherwise.

        A routed match binds every parameter from the path/query/body/header per
        the compiler's bind table, VALIDATES each bound input against its derived
        schema BEFORE the handler runs (item 257; a failure is `400` naming the
        field and the handler is never invoked), then maps the handler's return
        per the return rules. A path that matches a template with the wrong method
        is `405`; a path no route and no canonical operation matches is `404`."""
        clean = path.split("?", 1)[0].rstrip("/") or "/"
        query = urllib.parse.parse_qs(path.split("?", 1)[1]) \
            if "?" in path else {}

        matched_path = False
        for route in self._routes_457:
            m = route.regex.match(clean)
            if m is None:
                continue
            matched_path = True
            if route.method != method:
                continue
            return self._serve_route(route, m, query, body, headers)
        if matched_path:
            allow = sorted({r.method for r in self._routes_457
                            if r.regex.match(clean)})
            return HttpReply.json(_METHOD_NOT_ALLOWED, _err(
                f"`{clean}` is routed for {', '.join(allow)}, not {method}",
                code="method"))

        # no route matched — the canonical fourth-quadrant path, wrapped as JSON.
        status, payload = self.dispatch(method, path, body)
        return HttpReply.json(status, payload)

    def _serve_route(self, route: "_Route", match, query: dict, body: bytes,
                     headers) -> HttpReply:
        # 1. bind + validate every parameter BEFORE the handler runs.
        bound: dict = {}
        _MISSING = object()
        parsed_body = _MISSING
        for pname in route.param_order:
            entry = route.bind.get(pname) or {}
            kind = entry.get("kind")
            if kind == "path":
                raw = match.groupdict().get(pname)
                try:
                    value = _coerce_scalar(raw, entry.get("type"))
                except ValueError as exc:
                    return HttpReply.json(_BAD_REQUEST, _err(
                        f"path parameter `{pname}`: {exc}", code="request"))
                err = _schema_error(value, entry.get("schema") or {})
                if err is not None:
                    return HttpReply.json(_BAD_REQUEST, _err(
                        f"path parameter `{pname}` {err}", code="request"))
                bound[pname] = value
            elif kind == "query":
                values = query.get(pname)
                if not values:
                    if entry.get("optional"):
                        bound[pname] = None
                        continue
                    return HttpReply.json(_BAD_REQUEST, _err(
                        f"missing required query parameter `{pname}`",
                        code="request"))
                inner = entry.get("type")
                if entry.get("optional"):
                    inner = _opt_inner(inner)
                try:
                    value = _coerce_scalar(values[0], inner)
                except ValueError as exc:
                    return HttpReply.json(_BAD_REQUEST, _err(
                        f"query parameter `{pname}`: {exc}", code="request"))
                err = _schema_error(value, entry.get("schema") or {})
                if err is not None:
                    return HttpReply.json(_BAD_REQUEST, _err(
                        f"query parameter `{pname}` {err}", code="request"))
                bound[pname] = value
            elif kind == "body":
                if parsed_body is _MISSING:
                    if not body or not body.strip():
                        return HttpReply.json(_BAD_REQUEST, _err(
                            f"missing request body for `{pname}`", code="request"))
                    try:
                        parsed_body = json.loads(body)
                    except json.JSONDecodeError as exc:
                        return HttpReply.json(_BAD_REQUEST, _err(
                            f"request body is not JSON ({exc})", code="request"))
                err = _schema_error(parsed_body, entry.get("schema") or {})
                if err is not None:
                    return HttpReply.json(_BAD_REQUEST, _err(
                        f"body `{pname}` {err}", code="request"))
                bound[pname] = self._decode(parsed_body)
            elif kind == "header":
                # the bearer credential, bound UNTOUCHED as an untrusted claim.
                bound[pname] = {"token": _bearer_token(headers)}
            elif kind == "request":
                bound[pname] = self._build_request(route, match, query, body,
                                                   headers)
            else:  # a parameter with no binding — should not happen post-check
                bound[pname] = None

        args = [bound.get(pname) for pname in route.param_order]

        # 2. run the handler.
        try:
            result = self.session.call(route.key, route.op, args, raw=True)
        except SessionError as error:
            return HttpReply.json(_BAD_REQUEST, _err(str(error), code="session"))
        except ApprovalRequired as exc:
            return HttpReply.json(_FORBIDDEN, two_step_payload(
                exc.ticket,
                how_to_approve="This routed HTTP face serves the composition's "
                               "own operations; there is no approve verb on this "
                               "wire. Relay the ticket to the operator."))
        except Exception as exc:  # the callee raised — a result, not a crash
            return HttpReply.json(_SERVER_ERROR, {
                "code": "internal_error",
                "message": f"{type(exc).__name__}: {exc}"})

        # 3. map the handler's return per the return rules (item 457).
        return self._encode_return(route.response, result.get("result"))

    def _build_request(self, route, match, query, body, headers) -> dict:
        """Construct the typed `Request` escape-hatch value (a record dict). The
        `method`/`body` variants are rebuilt through the live module's decoder so
        a handler that `match`es on `req.method` sees native cases."""
        verb = route.method.capitalize()
        raw_body = body.decode("utf-8", "replace") if body else ""
        body_val = ({"$kind": "Text", "$value": raw_body} if raw_body
                    else {"$kind": "Empty"})
        header_list = [{"name": k, "value": v}
                       for k, v in (headers.items() if headers else [])]
        return {
            "method": self._decode({"$kind": verb}),
            "path": match.string,
            "headers": header_list,
            "body": self._decode(body_val),
        }

    def _encode_return(self, response: dict, value) -> HttpReply:
        kind = response.get("kind")
        if kind == "response":
            return self._encode_response_value(value)
        if kind == "result":
            tag = type(value).__name__ if value is not None else None
            if tag == "Err":
                return self._encode_api_error(getattr(value, "value", None))
            inner = getattr(value, "value", None) if tag == "Ok" else value
            return self._encode_ok(response.get("ok"), inner)
        # plain T
        return self._encode_ok(response.get("type"), value)

    def _encode_ok(self, ok_type, value) -> HttpReply:
        if not ok_type or ok_type == "Unit" or value is None:
            return HttpReply(_NO_CONTENT, b"", "application/json")
        return HttpReply.json(_OK, _encode_value(value))

    def _encode_api_error(self, err_value) -> HttpReply:
        enc = _encode_value(err_value)
        if not isinstance(enc, dict):
            return HttpReply.json(_SERVER_ERROR, {
                "code": "internal_error",
                "message": "handler returned a malformed ApiError"})
        status = enc.get("status", _BAD_REQUEST)
        try:
            status = int(status)
        except (TypeError, ValueError):
            return HttpReply.json(_SERVER_ERROR, {
                "code": "internal_error",
                "message": "handler returned a malformed ApiError"})
        return HttpReply.json(status, {
            "code": enc.get("code", "error"),
            "message": enc.get("message", "")})

    def _encode_response_value(self, value) -> HttpReply:
        """A handler-owned `Response` (stdlib/http.rvl): the handler owns status,
        headers and body, sent as-is."""
        enc = _encode_value(value)
        if not isinstance(enc, dict):
            return HttpReply.json(_SERVER_ERROR, {
                "code": "internal_error",
                "message": "handler returned a malformed Response"})
        try:
            status = int(enc.get("status", _OK))
        except (TypeError, ValueError):
            return HttpReply.json(_SERVER_ERROR, {
                "code": "internal_error",
                "message": "handler returned a malformed Response"})
        extra_headers = tuple(
            (h.get("name"), h.get("value"))
            for h in (enc.get("headers") or [])
            if isinstance(h, dict) and h.get("name"))
        # `stdlib/http.rvl` documents `status_text` as the human-readable reason
        # phrase carried alongside `status`, and every constructor there
        # (`response`, `ok_text`, `ok_json`, `not_found`) populates it. The face
        # carried the status and dropped the phrase, so the wire phrase was
        # whatever `BaseHTTPRequestHandler`'s own table had for the code — the
        # handler's text was unreachable, and a code outside the table was
        # written with an empty phrase. It is carried here instead.
        status_text = enc.get("status_text") or ""
        body = enc.get("body") or {"$kind": "Empty"}
        kind = body.get("$kind") if isinstance(body, dict) else None
        payload = body.get("$value", "") if isinstance(body, dict) else ""
        if kind == "Empty":
            return HttpReply(status, b"", "application/json", extra_headers,
                             status_text)
        content_type = "application/json" if kind == "Json" else \
            "text/plain; charset=utf-8"
        return HttpReply(status, str(payload).encode("utf-8"),
                         content_type, extra_headers, status_text)


def _opt_inner(type_name: str | None) -> str | None:
    """`Opt[Int]` -> `Int`; otherwise the type unchanged."""
    if type_name and type_name.startswith("Opt[") and type_name.endswith("]"):
        return type_name[4:-1].strip()
    return type_name


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

# The largest request body this face accepts, in bytes. The ceiling is the
# CALLER's policy and not part of the wire framing (`stdlib/framing.rvl:252`),
# and this is the value that module's own usage example names —
# `body_length(req.headers, 1024 * 1024)`, `stdlib/framing.rvl:41` — so the host
# face and the primitive it mirrors agree on the number as well as on the rules.
_MAX_BODY = 1024 * 1024


@dataclasses.dataclass(frozen=True)
class _FramingRefusal:
    """A message this face refuses to frame — a request it will not read, or a
    reply it will not write — in `stdlib/framing.rvl`'s shape: the wire status
    (`status_for`), the stable machine token (`reason_of`) and the sentence to
    ship (`message_of`). The request-side three come from that module verbatim,
    so a caller reading this face's refusals reads the same words as a caller
    reading the primitive's."""

    status: int
    reason: str
    message: str


_UNSUPPORTED_TRANSFER_ENCODING = _FramingRefusal(
    _BAD_REQUEST, "unsupported_transfer_encoding",
    "the request declares a Transfer-Encoding, which this server does not "
    "decode; refuse it and close the connection rather than reading a body "
    "framed by Content-Length (RFC 9112 6.1)")

_DUPLICATE_CONTENT_LENGTH = _FramingRefusal(
    _BAD_REQUEST, "duplicate_content_length",
    "the request carries more than one Content-Length field (or a "
    "comma-separated list in one field), so where the body ends is ambiguous; "
    "refuse it and close the connection (RFC 9110 8.6, RFC 7230 3.3.3)")

_MALFORMED_CONTENT_LENGTH = _FramingRefusal(
    _BAD_REQUEST, "malformed_content_length",
    "Content-Length must be one plain non-negative decimal integer: no sign, "
    "no leading zeros beyond a single 0, no whitespace inside the value; refuse "
    "it and close the connection (RFC 9112 6.3)")

_OVER_CEILING = _FramingRefusal(
    _PAYLOAD_TOO_LARGE, "over_ceiling",
    "the declared body is larger than this endpoint accepts; refuse it with 413 "
    "before reading any of it")

# The response-side refusals. Both are the FACE's, not a handler's, and both are
# 500: the request was well-formed, so what is broken is the program that
# answered it.
_HANDLER_FRAMED_RESPONSE = _FramingRefusal(
    _SERVER_ERROR, "handler_framed_response",
    "the handler declared Content-Length, Transfer-Encoding or Connection on a "
    "response of its own; this face frames every reply it writes, and two "
    "framings on one message is how a reader downstream and this face come to "
    "disagree about where the reply ends (RFC 9110 6.1, RFC 9112 6.3), so the "
    "reply is refused rather than written with both")

_UNSAFE_RESPONSE_HEADER = _FramingRefusal(
    _SERVER_ERROR, "unsafe_response_header",
    "a handler-supplied response header name or value is not a valid HTTP "
    "field: a name must be a token and a value may contain neither CR, LF nor "
    "any other control character, because either ends the field early and lets "
    "the rest of the value write further fields, a body, or a whole second "
    "reply (RFC 9110 5.1, 5.5)")

_INVALID_STATUS = _FramingRefusal(
    _SERVER_ERROR, "invalid_status",
    "the handler chose a status that is not the three digits an HTTP status "
    "line has room for; this face writes it into the status line unchecked, and "
    "a reader is required to reject a status it cannot read as three digits, so "
    "the reply is refused rather than written unreadable (RFC 9110 15)")

_UNSAFE_STATUS_TEXT = _FramingRefusal(
    _SERVER_ERROR, "unsafe_status_text",
    "the handler's `status_text` is not a valid HTTP reason phrase: a phrase is "
    "HTAB, SP, VCHAR and obs-text and nothing else, because a CR or LF in it "
    "ends the status line early and lets the rest of the value write further "
    "fields, a body, or a whole second reply (RFC 9110 5.5, 15)")

# A status whose reply ends at the end of the header section, and so carries no
# body: 1xx (RFC 9110 15.2, an interim response), 204 (15.3.5) and 304 (15.4.5)
# all say "cannot contain content". RFC 9110 8.6 forbids `Content-Length` on 1xx
# and 204 outright, and permits it on 304 only when it equals the length the 200
# would have had — a length this face cannot know, since all it has is the body
# the handler returned. So none of the three declares a length here, and none of
# them has a body written after it: the set decides BOTH, because a reply that
# declares no content and then writes some is the same defect as one that
# declares a length it does not write.
_CONTENTLESS_STATUSES = frozenset(range(100, 200)) | {_NO_CONTENT, _NOT_MODIFIED}

# The three header fields a reply's framing lives in. A handler owns its reply's
# status, body and ordinary headers; it does NOT own where the body ends, because
# this face is what knows the body's length and what writes it. These are
# refused rather than overwritten: a program hand-framing its own reply is a
# defect to be told about, not silently repaired, and the refusal is what makes
# the defect visible instead of leaving one of the two framings on the wire.
_FACE_OWNED_RESPONSE_HEADERS = frozenset({
    "content-length", "transfer-encoding", "connection"})

# RFC 9110 5.1: a field name is a token — tchar is ALPHA / DIGIT / these six
# punctuation runs, and nothing else.
_TCHAR = frozenset(
    "!#$%&'*+-.^_`|~"
    "0123456789"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _is_token(value: str) -> bool:
    """Is this a legal HTTP field NAME (RFC 9110 5.1)?

    The name is what a receiver splits the field on, so a name carrying a colon
    or a space lets the handler-supplied name be read as a different field, and
    one carrying CR or LF ends the head outright.
    """
    return isinstance(value, str) and bool(value) and \
        all(ch in _TCHAR for ch in value)


def _is_field_value(value: str) -> bool:
    """Is this a legal HTTP field VALUE (RFC 9110 5.5)?

    HTAB, SP, VCHAR and obs-text, and nothing else. Excluding CR, LF and the
    other control characters is the entire point: any of them ends the field
    early, so everything after it is read as further header fields — or, after a
    blank line, as the message body (RFC 9112 2.2, 6.3).
    """
    if not isinstance(value, str):
        return False
    return all(ch == "\t" or " " <= ch <= "~" or "\x80" <= ch <= "\xff"
               for ch in value)


# RFC 9110 15: a status code is a three-digit integer. The face checks the
# SYNTAX and not the registry — a 6xx is undefined but is still three digits a
# reader can read, so it is the handler's to use; a 1000, a 99 or a 0 is not a
# status line, and `http.client` raises `BadStatusLine` on each of them.
_MIN_STATUS = 100
_MAX_STATUS = 999


def _response_refusal(reply: HttpReply) -> "_FramingRefusal | None":
    """The first reason this reply may not be written, or None if it may.

    The counterpart of `_frame_request`, one direction out. `http.server`'s
    `send_header` performs NO validation whatsoever — it is
    ``self._headers_buffer.append(("%s: %s\\r\\n" % (keyword, value))…)`` and
    nothing more; `_is_illegal_header_value` exists only in `http.client`, i.e.
    on the RECEIVING side. So every check a reply needs is this face's to make,
    and the handler-supplied header list is untrusted input: `route` handlers
    return `Response` values whose headers routinely carry a decoded path or
    query scalar, and a percent-decoded `%0d%0a` reaches the wire verbatim
    through `send_header`.

    `content-type` is the one handler header this face drops rather than
    refuses — the face always has a content type of its own (the same rule the
    request side applies to nothing, because a request has no such field), and
    dropping it is why a value carrying CR or LF in that field is inert.

    The status is checked first because the status line is written first. Both
    halves of that line are the handler's: the code (`Response.status`, checked
    for the three digits a status line has room for) and now the reason phrase
    (`Response.status_text`). The phrase is validated for the same reason a
    header value is — it goes on the wire verbatim, and a CR or LF in it ends the
    line early — and RFC 9110's reason-phrase grammar (HTAB, SP, VCHAR,
    obs-text) coincides with the field-value grammar, so `_is_field_value` is the
    check. An EMPTY phrase is legal rather than refused: it means the handler
    expressed no preference, and the status line falls back to this face's own
    table.
    """
    if not _MIN_STATUS <= reply.status <= _MAX_STATUS:
        return _INVALID_STATUS
    if not _is_field_value(reply.status_text):
        return _UNSAFE_STATUS_TEXT
    for name, value in reply.headers:
        if not name or (isinstance(name, str)
                        and name.lower() == "content-type"):
            continue
        if not _is_token(name):
            return _UNSAFE_RESPONSE_HEADER
        if name.lower() in _FACE_OWNED_RESPONSE_HEADERS:
            return _HANDLER_FRAMED_RESPONSE
        if not _is_field_value(value):
            return _UNSAFE_RESPONSE_HEADER
    return None


# The HTTP grammar's OWS is SP and HTAB, deliberately narrower than
# `str.strip()`'s wider set: a value padded with a form feed is malformed rather
# than trimmed (`stdlib/framing.rvl:340`, RFC 9110 5.6.3).
_OWS = " \t"


def _frame_request(headers, ceiling: int) -> "tuple[int | None, _FramingRefusal | None]":
    """How many body bytes belong to this request, or the refusal that says why
    it is not framed.

    The host-side port of `stdlib/framing.rvl`'s `body_length`
    (`stdlib/framing.rvl:262`): the same rules in the same order — transfer
    encoding, then a repeated length, then the value grammar, then the ceiling —
    earning the same cases, the same wire statuses and the same sentences. This
    is Python host code and cannot call a Revl primitive, so it mirrors the
    primitive rather than calling it; `docs/design/867-request-framing.md:151`
    names this reader as exactly that residue, and a reader that answered any of
    these four rules differently from the primitive is the drift that module
    exists to remove.

    `headers` is the `email.message.Message` `BaseHTTPRequestHandler` builds.
    `get_all` is its duplicate-visible reader — the analogue of the stdlib's
    `header_values`/`header_count` in `stdlib/framing.rvl` — because `.get()`
    answers with the FIRST occurrence only and cannot see a doubled field at all.
    `stdlib/http.rvl`'s `header_value` has the same first-match blind spot, but
    it now compares names through the same `header_name_eq` the framing readers
    use, so all of them agree case-insensitively (RFC 9110 5.1) about whether a
    name is present.
    """
    if headers.get_all("Transfer-Encoding"):
        return None, _UNSUPPORTED_TRANSFER_ENCODING
    lengths = headers.get_all("Content-Length") or []
    if len(lengths) > 1:
        return None, _DUPLICATE_CONTENT_LENGTH
    if not lengths:
        # RFC 9112 6.3 item 7: neither field means no body, which is 0, not a
        # refusal.
        return 0, None
    return _length_of(lengths[0], ceiling)


def _length_of(value: str, ceiling: int) -> "tuple[int | None, _FramingRefusal | None]":
    """One `Content-Length` value to its length, or the rule it broke."""
    v = value.strip(_OWS)
    if not v:
        return None, _MALFORMED_CONTENT_LENGTH
    if not _all_digits(v):
        # a comma in the value is a list of lengths in ONE field: the duplicate
        # defect spelled differently, and a comma is what a list is
        if "," in v:
            return None, _DUPLICATE_CONTENT_LENGTH
        return None, _MALFORMED_CONTENT_LENGTH
    if len(v) > 1 and v[0] == "0":
        # `0000000001` is legal ABNF and is still refused: accepting two
        # spellings of one length forces every reader to compare them as text
        return None, _MALFORMED_CONTENT_LENGTH
    # Bound BEFORE the multiply, exactly as `stdlib/framing.rvl`'s `length_of`
    # does (`stdlib/framing.rvl:299`): the digit loop stops as soon as the digits
    # read exceed the ceiling, so a 4300-digit value is refused on its first few
    # digits and is never handed to an integer conversion at all. Without the
    # second clause the loop could be walked one digit past the ceiling.
    acc = 0
    head = ceiling // 10
    for ch in v:
        d = ord(ch) - 48
        if acc > head or (acc == head and d > ceiling - acc * 10):
            return None, _OVER_CEILING
        acc = acc * 10 + d
    return acc, None


def _all_digits(value: str) -> bool:
    return all("0" <= ch <= "9" for ch in value)


def _make_handler(server: HttpComposedServer):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _write(self, reply: HttpReply, *, close: bool = False) -> None:
            refusal = _response_refusal(reply)
            if refusal is not None:
                # A reply the face will not write is answered with a reply it
                # wrote itself — the same structured refusal shape, and the same
                # code, as the request side uses. Substituting BEFORE anything
                # reaches `send_response` is what makes this safe: the handler's
                # headers are dropped whole, so there is no second visit here and
                # nothing of the refused reply is on the wire at all.
                reply = HttpReply.json(refusal.status, _err(
                    refusal.message, code=refusal.reason))
                close = True
            # `message=None` (not `""`) when the handler expressed no phrase, so
            # the table fallback in `send_response_only` still runs and every
            # reply that is not a routed `Response` keeps its old status line.
            self.send_response(reply.status, reply.status_text or None)
            self.send_header("Content-Type", reply.content_type)
            for name, value in reply.headers:
                if name and name.lower() != "content-type":
                    self.send_header(name, value)
            if close:
                # RFC 7230 3.3.3, and `docs/design/867-request-framing.md:159`:
                # a refused framing means the byte stream is no longer
                # trustworthy, so a pipelined request behind it must not be
                # parsed — say so, and close rather than keep reading.
                self.close_connection = True
                self.send_header("Connection", "close")
            if reply.status not in _CONTENTLESS_STATUSES:
                # The ONE place this face's reply framing is decided, and it is
                # decided from the body it is about to write rather than from
                # anything a handler said.
                self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            # RFC 9110 9.3.2: a HEAD response carries the header fields the GET
            # would have carried — the `Content-Length` above included — and no
            # body. RFC 9110 15.2/15.3.5/15.4.5: a 1xx, a 204 and a 304 end at
            # the end of the header section. In both cases writing the body
            # anyway hands the client bytes its own framing says are not there,
            # and on a connection that is being kept alive those bytes are read
            # as the head of the next reply — so the SAME set that withholds the
            # length withholds the body.
            if self.command != "HEAD" and reply.status not in _CONTENTLESS_STATUSES:
                self.wfile.write(reply.body)

        def _respond(self, method: str) -> None:
            length, refusal = _frame_request(self.headers, _MAX_BODY)
            if refusal is not None:
                # A clean, structured refusal in the module's own error shape,
                # never a traceback: which bytes belong to this body is the
                # sender's to declare and this face's to check, so a request that
                # declares it wrongly gets an answer (`_err`, the same
                # `{"severity", "code", "category", "message"}` envelope every
                # other refusal on this face uses) instead of a crash.
                self._write(HttpReply.json(refusal.status, _err(
                    refusal.message, code=refusal.reason)), close=True)
                return
            body = self.rfile.read(length) if length else b""
            # item 457: `dispatch_http` honours `route` clauses first (with the
            # request headers and query, for bearer/path/query binding) and falls
            # back to the canonical fourth-quadrant dispatch otherwise.
            reply = server.dispatch_http(method, self.path, body, self.headers)
            self._write(reply)

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._respond("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._respond("PUT")

        def do_PATCH(self) -> None:  # noqa: N802
            self._respond("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._respond("DELETE")

        def do_HEAD(self) -> None:  # noqa: N802
            self._respond("HEAD")

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
    # item 457: wire the live module's decoder so a routed handler binding a typed
    # `Request` (whose `method`/`body` are variants) sees native case instances.
    # Lazy import: the backend decoder is only reachable on the live path (Session
    # already pulled cordis above), keeping the pure wire layer decoupled.
    decode = None
    module = getattr(session, "_module", None)
    if module is not None:
        try:
            from .._paths import backends_root  # noqa: PLC0415
            backend_dir = backends_root() / "python"
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))
            import bridge  # noqa: PLC0415 — backend import after path setup
            decode = lambda v: bridge._decode_value(v, module)  # noqa: E731
        except Exception:  # noqa: BLE001 — the escape hatch degrades to identity
            decode = None
    face = HttpComposedServer(session, composition=composition, decode=decode)
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

"""revl IR -> an OpenAPI 3.1 document (roadmap item 457, artifact 4).

`revl export openapi FILES --service NAME` is the inverse of `revl import
openapi` (docs/import-openapi.md) over the subset both express: it renders one
path item per ROUTED operation (an operation that heads a `route` clause), the
compiler-resolved bind table as `parameters` (path/query) and `requestBody`, the
derived schemas under `components/schemas`, the `200`/`204` success and the
`ApiError` error responses, and the compiler's `x-revl-emission` hint.

What the document deliberately does NOT carry (docs/design/457-...md, §"the five
derived artifacts", 4): NO `securitySchemes` and NO `security` requirement, from
anything. There is nothing in the `route` clause to derive an authorization
requirement from — authorization is the handler's explicit step on a required
`Auth` service, not a fact the router, this document or the generated client can
stand in for. An `Auth`-bearing operation (a `Bearer` parameter) gets a
DOCUMENTATION-ONLY `x-revl-auth: bearer` marker whose text says the check happens
in the handler; the marker grants nothing.

Pure IR codegen, no runtime, no language change: the document is a projection of
the same `route` IR entry `revl serve --http` and `revl export client` read.
"""

from __future__ import annotations

import json

from .errors import RevlError

#: revl scalar surface type -> the OpenAPI (JSON Schema) scalar, the inverse of
#: the importer's `_SCALARS`, so a scalar round-trips through both tools.
_SCALARS = {
    "Str": {"type": "string"},
    "Int": {"type": "integer"},
    "Bool": {"type": "boolean"},
    "Float": {"type": "number"},
}

_REF_PREFIX = "#/components/schemas/"


def _split_generic(type_str: str) -> tuple[str, list[str]]:
    """`List[Note]` -> (`List`, [`Note`]); `Str` -> (`Str`, []). A small local
    copy of the shared splitter (no import-time coupling beyond the IR shape)."""
    if "[" not in type_str or not type_str.endswith("]"):
        return type_str, []
    head, _, rest = type_str.partition("[")
    inner, args, depth, start = rest[:-1], [], 0, 0
    for i, char in enumerate(inner):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "," and depth == 0:
            args.append(inner[start:i].strip())
            start = i + 1
    args.append(inner[start:].strip())
    return head, [a for a in args if a]


class _OpenApiExporter:
    def __init__(self, ir: dict) -> None:
        self.ir = ir
        self.services: dict = ir.get("services") or {}
        self.types: dict = ir.get("types") or {}
        #: named types referenced by any rendered schema, closed transitively and
        #: emitted under `components/schemas`.
        self._referenced: set[str] = set()

    # -- schema rendering (inverse of the importer's resolver) ------------
    def _schema_ref(self, type_str: str | None) -> dict:
        """The OpenAPI schema for a bound type: a scalar inline, a `List`/`Opt`
        structurally, a named record/variant as a `$ref` (registered for the
        components block)."""
        if not type_str or type_str == "Unit":
            return {}
        if type_str in _SCALARS:
            return dict(_SCALARS[type_str])
        head, args = _split_generic(type_str)
        if head == "List" and len(args) == 1:
            return {"type": "array", "items": self._schema_ref(args[0])}
        if head == "Opt" and len(args) == 1:
            inner = self._schema_ref(args[0])
            return {"oneOf": [inner, {"type": "null"}]}
        if head == "Map" and len(args) == 2:
            return {"type": "object",
                    "additionalProperties": self._schema_ref(args[1])}
        # a nominal type -> a component reference
        if type_str in self.types:
            self._referenced.add(type_str)
            return {"$ref": f"{_REF_PREFIX}{type_str}"}
        raise RevlError("<ir>", 0,
                        f"cannot render an OpenAPI schema for `{type_str}` "
                        "(no declared type)")

    def _component_schema(self, name: str) -> dict:
        spec = self.types[name]
        if spec.get("kind") == "record":
            props = {}
            required = []
            for fname, ftype in (spec.get("fields") or {}).items():
                props[fname] = self._schema_ref(ftype)
                # a bare `Opt[..]` field is optional; everything else is required
                head, _ = _split_generic(ftype)
                if head != "Opt":
                    required.append(fname)
            schema: dict = {"type": "object", "properties": props,
                            "additionalProperties": False}
            if required:
                schema["required"] = sorted(required)
            return schema
        if spec.get("kind") == "variant":
            # an ADT as the adjacently-tagged wire shape (the canonical encoding);
            # each case a `{$kind[, $value]}` object.
            arms = []
            for case in spec.get("cases") or []:
                props = {"$kind": {"const": case["name"]}}
                required = ["$kind"]
                if case.get("payload"):
                    props["$value"] = self._schema_ref(case["payload"])
                    required.append("$value")
                arms.append({"type": "object", "properties": props,
                             "required": required, "additionalProperties": False})
            return {"oneOf": arms}
        raise RevlError("<ir>", 0,
                        f"cannot render an OpenAPI schema for `{name}`")

    def _type_closure(self) -> list[str]:
        out: list[str] = []
        stack = list(self._referenced)
        seen: set[str] = set()
        while stack:
            name = stack.pop()
            if name in seen or name not in self.types:
                continue
            seen.add(name)
            out.append(name)
            spec = self.types[name]
            for ftype in (spec.get("fields") or {}).values():
                for ref in self._nominal_refs(ftype):
                    stack.append(ref)
            for case in spec.get("cases") or []:
                for ref in self._nominal_refs(case.get("payload")):
                    stack.append(ref)
        return out

    def _nominal_refs(self, type_str: str | None) -> list[str]:
        if not type_str:
            return []
        head, args = _split_generic(type_str)
        if head in _SCALARS or head in ("List", "Opt", "Result", "Map", "Unit"):
            refs: list[str] = []
            for arg in args:
                refs.extend(self._nominal_refs(arg))
            return refs
        return [head]

    # -- one operation -----------------------------------------------------
    def _operation(self, op_name: str, op: dict) -> tuple[str, str, dict]:
        route = op["route"]
        method = route["method"]
        path = route["path"]
        bind = route.get("bind") or {}
        response = route.get("response") or {"kind": "plain", "type": "Unit"}

        entry: dict = {
            "operationId": op_name,
            # item 457: the compiler's emission classification, so a re-import
            # reconstructs the same plain/emission split (the importer reads this
            # `x-revl-emission` hint, not just verb safety).
            "x-revl-emission": bool(op.get("emission")),
        }
        if route.get("auth"):
            # DOCUMENTATION-ONLY: the handler performs the authorization step; the
            # document derives no `security` requirement from it.
            entry["x-revl-auth"] = route["auth"]
            entry["x-revl-auth-note"] = (
                "authorization is checked in the handler via the required `Auth` "
                "service; this document grants nothing and carries no `security`.")

        parameters = []
        request_body = None
        # walk params in declared order so the document reads like the operation
        for param in op.get("params") or []:
            pname = param["name"]
            b = bind.get(pname) or {}
            kind = b.get("kind")
            if kind == "path":
                parameters.append({
                    "name": pname, "in": "path", "required": True,
                    "schema": self._schema_ref(b.get("type"))})
            elif kind == "query":
                inner = b.get("type")
                if b.get("optional"):
                    head, args = _split_generic(inner)
                    inner = args[0] if (head == "Opt" and args) else inner
                parameters.append({
                    "name": pname, "in": "query",
                    "required": not b.get("optional"),
                    "schema": self._schema_ref(inner)})
            elif kind == "body":
                request_body = {
                    "required": True,
                    "content": {"application/json": {
                        "schema": self._schema_ref(b.get("type"))}}}
            # `header` (bearer) and `request` bind from transport, not an
            # OpenAPI parameter; the bearer is surfaced only by `x-revl-auth`.
        if parameters:
            entry["parameters"] = parameters
        if request_body is not None:
            entry["requestBody"] = request_body

        entry["responses"] = self._responses(response)
        return method, path, entry

    def _responses(self, response: dict) -> dict:
        kind = response.get("kind")
        responses: dict = {}
        if kind == "response":
            responses["200"] = {"description": "handler-owned response"}
            responses["default"] = self._error_response()
            return responses
        ok_type = response.get("ok") if kind == "result" else response.get("type")
        if not ok_type or ok_type == "Unit":
            responses["204"] = {"description": "no content"}
        else:
            responses["200"] = {
                "description": "success",
                "content": {"application/json": {
                    "schema": self._schema_ref(ok_type)}}}
        if kind == "result":
            responses["default"] = self._error_response()
        return responses

    def _error_response(self) -> dict:
        # the `Err(ApiError)` wire shape: `{code, message}` (the numeric status is
        # the HTTP code itself). Registered as a component so it is named once.
        self._referenced.add("__ApiError")
        return {
            "description": "an error a client can act on (ApiError)",
            "content": {"application/json": {
                "schema": {"$ref": f"{_REF_PREFIX}ApiError"}}}}

    # -- the whole document ------------------------------------------------
    def emit(self, service_name: str) -> dict:
        service = self.services[service_name]
        methods = service.get("methods") or {}
        routed = [(n, op) for n, op in methods.items() if op.get("route")]
        if not routed:
            raise RevlError(
                "<ir>", 0,
                f"service `{service_name}` has no routed operations to export",
                hint="an OpenAPI document is one path item per operation that "
                     "heads a `route` clause; this service declares none")
        paths: dict = {}
        for op_name, op in routed:
            method, path, entry = self._operation(op_name, op)
            paths.setdefault(path, {})[method] = entry

        components_schemas: dict = {}
        # the wire-shape ApiError (code + message), referenced by every error
        # response; emitted only when some operation returns a `Result`.
        want_api_error = "__ApiError" in self._referenced
        self._referenced.discard("__ApiError")
        for name in self._type_closure():
            components_schemas[name] = self._component_schema(name)
        if want_api_error:
            components_schemas["ApiError"] = {
                "type": "object",
                "properties": {"code": {"type": "string"},
                               "message": {"type": "string"}},
                "required": ["code", "message"],
                "additionalProperties": False}

        doc: dict = {
            "openapi": "3.1.0",
            "info": {"title": service_name, "version": "1.0.0"},
            "paths": paths,
        }
        if components_schemas:
            doc["components"] = {"schemas": components_schemas}
        return doc


def export_openapi(ir: dict, *, service: str, indent: int | None = 2) -> str:
    """Render a compiled revl IR's routed service as an OpenAPI 3.1 JSON document.

    `service` names the service to export (its routed operations become the path
    items). No `--composition` form: an OpenAPI document is one API surface with
    one `info.title`, so it names a single service.
    """
    services = ir.get("services") or {}
    if service not in services:
        known = ", ".join(sorted(services)) or "(none)"
        raise RevlError("<ir>", 0, f"no service named `{service}` in this IR",
                        hint=f"known services: {known}")
    doc = _OpenApiExporter(ir).emit(service)
    return json.dumps(doc, indent=indent) + "\n"

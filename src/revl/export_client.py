"""revl IR -> a typed remote CLIENT (item 424 gap (c), slice C1).

`revl export client --lang ts` projects a compiled revl `service` into a typed
client for a NON-revl consumer, over the canonical value encoding the four
bridges already speak (docs/interop-bridge.md, "Canonical value encoding"). It
is pure IR codegen: the same projection `revl serve --mcp` and the placement
bridge already make of an operation, rendered here as a client face rather than
a server one. No language change, no runtime, no emission.

What this client IS, and what it is deliberately NOT
----------------------------------------------------
The design note (docs/design/424-dsh-language-gaps.md, D-424c.7/.8) is exact
about what a generated client may claim, and this module holds to it:

  * It carries the GATE FRONTIER (item 338's asymmetric contract, promoted to a
    first-class field): the covered surface of the gate that generated it. A
    refusal is authoritative; an admission is a compile-time judgment scoped to
    that frontier, not runtime confinement.
  * It makes NO safety claim about the callee. A client sits on the SENDING
    side and holds no gate over the remote (item 337's seam invariant: only the
    RECEIVER re-admits). So the generated header says the client is typed and
    bounded LOCALLY and says nothing about what the remote runs. There is no
    "verified remote" badge and no green checkmark on the peer.

The wire is the canonical encoding, so the generated TS TYPES ARE that encoding:
a record is a plain object, an `Opt[T]` is `T | null` (never tagged), and a user
ADT or `Result[T, E]` is the adjacently-tagged `{$kind, $value}` object the
bridges marshal to. A value therefore round-trips to the placement bridge by
construction — the type is the shape.

What the projection cannot express is REFUSED at generation, naming the method:
a resource type (anything an `extern acquire` returns) crosses by handle, not by
copy, so it has no client-side value (docs/interop-bridge.md §3); and a
`Map[K, V]` whose key is not `Str` cannot be a JSON object. Both are refused
here rather than emitted as something that would not round-trip.

The paired server face is `revl serve --http` (D-424c.6, `revl.mcp.http_face`):
it serves a booted composition's operations as `POST /<composition>/<key>/<op>`
over this same canonical encoding, so the generated `httpTransport` below drops
onto it directly. The remoteness LANGUAGE constructs (`remote` rows, the G4
admissibility check, per-realm peers) are C2, which waits on item 426 S1's row
table; this slice is codegen and transport only, no language change.

The second FACE: `--face webui`
-------------------------------
`--face webui --component NAME` projects a different typed boundary from the same
IR: the Cordis WebUI channel of one component (item 457 slice S4, design note
530 Decision B, docs/frontend-assets.md). The reactive state is the record type of
the `data` parameter the component publishes through `webui.add_entry`, and the
RPC method set is the services the component `provides` — the surface `revl audit`
reports as G1 — so the browser's `useRpc<T>()` type and the server's published
fields are one declaration. It emits no transport: Cordis WebUI owns the WebSocket
seam. See `_WebuiFaceExporter` below.
"""

from __future__ import annotations

import re

from .errors import RevlError
from .gate import gate_version

# ---------------------------------------------------------------- type mapping

#: revl scalar surface type -> TypeScript type. `Bytes` is a byte list on the
#: wire (the same shape `export wit` gives it, `list<u8>`); `Unit` is JSON null.
_SCALARS = {
    "Str": "string",
    "Bool": "boolean",
    "Int": "number",
    "Float": "number",
    "Bytes": "number[]",
    "Unit": "null",
}


def _split_generic(type_str: str) -> tuple[str, list[str]]:
    """`Result[Item, Str]` -> (`Result`, [`Item`, `Str`]); `Str` -> (`Str`, []).

    Kept local (a small copy of `export_wit`'s helper) so the client generator
    has no import-time coupling to the WIT exporter beyond the shared IR shape.
    """
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


class _ClientExporter:
    def __init__(self, ir: dict) -> None:
        self.ir = ir
        self.services: dict = ir.get("services") or {}
        self.types: dict = ir.get("types") or {}
        # a resource type is one an `extern acquire` returns — the same verdict
        # `export wit` and `placement.py` read: a handle that crosses by proxy,
        # never by value. It has no client-side representation.
        self.resources: set[str] = {
            ext["returns"]
            for ext in ir.get("externs") or []
            if ext.get("class") == "acquire" and ext.get("returns")
        }

    # -- wire-expressibility (the refusal predicate) ----------------------
    def _inexpressible_reason(self, type_str: str | None,
                              seen: frozenset = frozenset()) -> str | None:
        """Why a type cannot cross the client's wire, or `None` when it can.

        The canonical encoding expresses records, `List`, `Map[Str, V]`,
        `Opt`, user ADTs and `Result` (the last two adjacently tagged). It
        cannot express a RESOURCE (crosses by handle) or a `Map` with a
        non-`Str` key (JSON object keys are strings). Recursion is fine — the
        wire is JSON nesting and a TS type alias may be recursive — so a name
        already `seen` is accepted rather than re-descended."""
        if not type_str or type_str == "Unit":
            return None
        if type_str in _SCALARS:
            return None
        head, args = _split_generic(type_str)
        if head in ("List", "Opt") and args:
            return self._inexpressible_reason(args[0], seen)
        if head == "Result" and len(args) == 2:
            return (self._inexpressible_reason(args[0], seen)
                    or self._inexpressible_reason(args[1], seen))
        if head == "Map" and len(args) == 2:
            if args[0] != "Str":
                return (f"reaches `{type_str}`, whose key type `{args[0]}` is not "
                        "a JSON object key (the wire encodes a `Map` as an object; "
                        "use a `Str` key)")
            return self._inexpressible_reason(args[1], seen)
        # a nominal type
        if type_str in self.resources:
            return (f"reaches the resource type `{type_str}` (an `extern acquire` "
                    "handle), which crosses a seam by handle, not by value, so a "
                    "client has no value to marshal (docs/interop-bridge.md §3)")
        if type_str in seen:  # (mutually) recursive — the wire nests fine
            return None
        spec = self.types.get(type_str)
        if not spec:
            return f"reaches `{type_str}`, which has no declared type to marshal"
        inner = seen | {type_str}
        if spec.get("kind") == "record":
            for ftype in (spec.get("fields") or {}).values():
                reason = self._inexpressible_reason(ftype, inner)
                if reason is not None:
                    return reason
            return None
        if spec.get("kind") == "variant":
            for case in spec.get("cases") or []:
                if case.get("payload"):
                    reason = self._inexpressible_reason(case["payload"], inner)
                    if reason is not None:
                        return reason
            return None
        return f"reaches `{type_str}`, which has no client marshalling"

    # -- reverse type mapping (revl surface type -> TS) -------------------
    def ts_type(self, type_str: str | None) -> str:
        if not type_str or type_str == "Unit":
            return "null"
        if type_str in _SCALARS:
            return _SCALARS[type_str]
        head, args = _split_generic(type_str)
        if head == "List" and len(args) == 1:
            return f"Array<{self.ts_type(args[0])}>"
        if head == "Opt" and len(args) == 1:
            return f"{self.ts_type(args[0])} | null"
        if head == "Map" and len(args) == 2:
            # key already checked expressible (`Str`) before we render
            return f"Record<string, {self.ts_type(args[1])}>"
        if head == "Result" and len(args) == 2:
            ok, err = self.ts_type(args[0]), self.ts_type(args[1])
            return (f'{{ "$kind": "Ok", "$value": {ok} }} | '
                    f'{{ "$kind": "Err", "$value": {err} }}')
        if head in ("List", "Opt", "Result", "Map"):
            raise RevlError("<ir>", 0, f"malformed generic type `{type_str}`")
        # a nominal type -> its declared TS name
        self._referenced.add(head)
        return head

    # -- the referenced-type closure -------------------------------------
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

    def _type_closure(self, seeds: set[str]) -> set[str]:
        out: set[str] = set()
        stack = list(seeds)
        while stack:
            name = stack.pop()
            if name in out or name in self.resources or name not in self.types:
                continue
            out.add(name)
            spec = self.types[name]
            for ftype in (spec.get("fields") or {}).values():
                stack.extend(self._nominal_refs(ftype))
            for case in spec.get("cases") or []:
                stack.extend(self._nominal_refs(case.get("payload")))
        return out

    def _type_decl(self, name: str) -> str:
        spec = self.types[name]
        if spec.get("kind") == "record":
            fields = (spec.get("fields") or {})
            if not fields:
                return f"export interface {name} {{}}"
            lines = [f"export interface {name} {{"]
            for fname, ftype in fields.items():
                lines.append(f"  {fname}: {self.ts_type(ftype)};")
            lines.append("}")
            return "\n".join(lines)
        # a variant -> an adjacently-tagged discriminated union ($kind/$value),
        # exactly the canonical wire shape; a nullary case carries no $value.
        cases = spec.get("cases") or []
        arms = []
        for case in cases:
            if case.get("payload"):
                arms.append(f'{{ "$kind": "{case["name"]}", '
                            f'"$value": {self.ts_type(case["payload"])} }}')
            else:
                arms.append(f'{{ "$kind": "{case["name"]}" }}')
        body = "\n  | ".join(arms) if arms else "never"
        return f"export type {name} =\n  | {body};"

    # -- the client class per service ------------------------------------
    def _method(self, op_name: str, spec: dict) -> list[str]:
        route = spec.get("route")
        if route:
            return self._routed_method(op_name, spec, route)
        params = spec.get("params") or []
        sig = ", ".join(
            f"{p['name']}: {self.ts_type(p.get('type'))}" for p in params)
        arg_list = ", ".join(p["name"] for p in params)
        returns = spec.get("returns")
        ret_ts = self.ts_type(returns) if returns and returns != "Unit" else "null"
        lines = [f"  async {op_name}({sig}): Promise<{ret_ts}> {{"]
        lines.append(f'    const __r = await this.transport.call('
                     f'"{op_name}", [{arg_list}]);')
        lines.append(f"    return __r as {ret_ts};")
        lines.append("  }")
        return lines

    def _routed_return_ts(self, response: dict) -> str:
        """The TS return type for a routed operation's declared return
        (item 457). `Result[T, ApiError]` becomes the adjacently-tagged union the
        wire carries, with the `Err` value typed as the `{status, code, message}`
        shape the router renders."""
        kind = response.get("kind")
        if kind == "result":
            ok = self.ts_type(response.get("ok")) \
                if response.get("ok") and response.get("ok") != "Unit" else "null"
            return (f'{{ "$kind": "Ok", "$value": {ok} }} | '
                    '{ "$kind": "Err", "$value": '
                    '{ status: number, code: string, message: string } }')
        if kind == "response":
            return ("{ status: number, statusText: string; headers: Array<{ name: "
                    "string, value: string }>; body: string }")
        ok_type = response.get("type")
        return self.ts_type(ok_type) if ok_type and ok_type != "Unit" else "null"

    def _routed_method(self, op_name: str, spec: dict, route: dict) -> list[str]:
        """A REST method for a routed operation (item 457, artifact 5): it builds
        the path from its scalar arguments, puts the rest in the query or the JSON
        body per the bind table, sends a `Bearer` as the `Authorization` header
        when the operation declares one, and decodes the response into the same
        canonical-encoding types. No `transport.call` — this is the real route."""
        params = spec.get("params") or []
        bind = route.get("bind") or {}
        method = route["method"].upper()
        template = route["path"]
        response = route.get("response") or {"kind": "plain", "type": "Unit"}

        sig = ", ".join(
            f"{p['name']}: {self.ts_type(p.get('type'))}" for p in params)
        ret_ts = self._routed_return_ts(response)
        lines = [f"  async {op_name}({sig}): Promise<{ret_ts}> {{"]

        # 1. the path: substitute each `{name}` with its encoded scalar argument.
        path_js = "`" + re.sub(
            r"\{([^{}]+)\}",
            lambda m: "${encodeURIComponent(String(" + m.group(1) + "))}",
            template) + "`"
        lines.append(f"    let __path = {path_js};")

        # 2. the query string, from the query-bound arguments (Opt -> omit null).
        query = [p["name"] for p in params
                 if (bind.get(p["name"]) or {}).get("kind") == "query"]
        if query:
            lines.append("    const __q = new URLSearchParams();")
            for pname in query:
                lines.append(f"    if ({pname} !== null && {pname} !== undefined) "
                             f'__q.set("{pname}", String({pname}));')
            lines.append("    const __qs = __q.toString();")
            lines.append("    if (__qs) __path += `?${__qs}`;")

        # 3. headers: JSON for a body, and the bearer credential when declared.
        bearer = [p["name"] for p in params
                  if (bind.get(p["name"]) or {}).get("kind") == "header"]
        body = [p["name"] for p in params
                if (bind.get(p["name"]) or {}).get("kind") == "body"]
        lines.append("    const __headers: Record<string, string> = {};")
        if body:
            lines.append('    __headers["Content-Type"] = "application/json";')
        if bearer:
            b = bearer[0]
            lines.append(f"    if ({b} && {b}.token) "
                         f'__headers["Authorization"] = `Bearer ${{{b}.token}}`;')

        # 4. the fetch.
        init = [f'method: "{method}"', "headers: __headers"]
        if body:
            init.append(f"body: JSON.stringify({body[0]})")
        lines.append(f"    const __res = await fetch(`${{this.base}}${{__path}}`, "
                     f"{{ {', '.join(init)} }});")

        # 5. decode per the return rules.
        lines.extend(self._routed_decode(response, ret_ts))
        lines.append("  }")
        return lines

    def _routed_decode(self, response: dict, ret_ts: str) -> list[str]:
        kind = response.get("kind")
        if kind == "result":
            ok = self.ts_type(response.get("ok")) \
                if response.get("ok") and response.get("ok") != "Unit" else "null"
            return [
                "    if (__res.ok) {",
                ("      const __v = __res.status === 204 ? null : "
                 "await __res.json();"),
                f'      return {{ "$kind": "Ok", "$value": __v as {ok} }};',
                "    }",
                "    const __e = await __res.json().catch(() => ({}));",
                '    return { "$kind": "Err", "$value": { status: __res.status, '
                'code: String(__e.code ?? "error"), '
                'message: String(__e.message ?? "") } };',
            ]
        if kind == "response":
            return [
                "    const __body = await __res.text();",
                "    const __h: Array<{ name: string, value: string }> = [];",
                "    __res.headers.forEach((value, name) => __h.push({ name, value }));",
                "    return { status: __res.status, statusText: __res.statusText, "
                "headers: __h, body: __body };",
            ]
        # plain T. A `Unit` return (ret_ts == "null") is the 204 case; a non-Unit
        # T is a 200 body decoded as that type (no 204 line, which would be a
        # `null as T` strict error on a dead branch).
        if ret_ts == "null":
            return ["    return null;"]
        return [f"    return (await __res.json()) as {ret_ts};"]

    def _service_block(self, sname: str) -> list[str]:
        methods = (self.services.get(sname) or {}).get("methods") or {}
        routed = any(spec.get("route") for spec in methods.values())
        # refuse a method the projection cannot express, naming the method
        for op_name, spec in methods.items():
            for param in spec.get("params") or []:
                # a `Bearer`/`Request` parameter binds from transport on a routed
                # op, not a value the client marshals; skip the wire check for it.
                bkind = ((spec.get("route") or {}).get("bind") or {}).get(
                    param.get("name"), {}).get("kind")
                if bkind in ("header", "request"):
                    continue
                reason = self._inexpressible_reason(param.get("type"))
                if reason is not None:
                    raise RevlError(
                        "<ir>", 0,
                        f"cannot export a client for `{sname}.{op_name}`: "
                        f"parameter `{param.get('name')}` {reason}")
            # a routed op's `Result[T, ApiError]` is rendered structurally, so the
            # untagged-Result refusal does not apply to it.
            if not (spec.get("route") and (spec.get("route") or {}).get(
                    "response", {}).get("kind") == "result"):
                reason = self._inexpressible_reason(spec.get("returns"))
                if reason is not None:
                    raise RevlError(
                        "<ir>", 0,
                        f"cannot export a client for `{sname}.{op_name}`: "
                        f"its result {reason}")
        lines = [
            f"/** Typed client for revl service `{sname}`. LOCAL contract only: "
            "typed and",
            " *  bounded on THIS side; it makes no claim about what the remote "
            "runs. */",
            f"export class {sname}Client {{",
        ]
        if routed:
            # a routed service is a REST client: `base` is the server's origin
            # (e.g. `http://host:port`), and `transport` still serves any unrouted
            # operation over the canonical POST face.
            lines.append("  constructor(private readonly base: string, "
                         "private readonly transport: Transport = "
                         "{ call() { throw new Error(\"no transport configured "
                         "for an unrouted operation\"); } }) {}")
        else:
            lines.append("  constructor(private readonly transport: Transport) {}")
        lines.append("")
        first = True
        for op_name, spec in methods.items():
            if not first:
                lines.append("")
            first = False
            lines.extend(self._method(op_name, spec))
        lines.append("}")
        return lines

    # -- the whole file ---------------------------------------------------
    def emit(self, service_names: list[str]) -> str:
        self._referenced: set[str] = set()
        service_blocks: list[list[str]] = []
        for sname in service_names:
            service_blocks.append(self._service_block(sname))
        # types referenced by any exported signature, then their closure
        closure = self._type_closure(set(self._referenced))
        type_decls = [
            self._type_decl(name)
            for name in self.types
            if name in closure
        ]

        frontier = gate_version().get("frontier", "")
        parts = [self._header(service_names, frontier)]
        parts.append(
            "/** The gate frontier this client was generated under (item 338: a\n"
            " *  refusal is authoritative; an admission is a compile-time judgment\n"
            " *  scoped to THIS frontier, never a runtime-confinement claim). */\n"
            f'export const REVL_GATE_FRONTIER = "{frontier}";')
        parts.append(
            "/** How the client reaches the remote. The client marshals nothing\n"
            " *  itself: the canonical wire encoding IS the TS type shape below\n"
            " *  (docs/interop-bridge.md), so a value round-trips to any revl\n"
            " *  bridge by construction. Supply a transport for your endpoint. */\n"
            "export interface Transport {\n"
            "  call(method: string, args: unknown[]): Promise<unknown>;\n"
            "}")
        parts.append(
            "/** A Transport onto a `revl serve --http` face (D-424c.6). `base`\n"
            " *  is the service's route prefix, `http://host:port/<composition>/\n"
            " *  <key>`; each call POSTs the positional args as a JSON array to\n"
            " *  `<base>/<method>` and reads the `{ok, value}` reply. The value\n"
            " *  IS the canonical encoding, so it is already this client's types.\n"
            " *  This is transport only: it makes no claim about the remote. */\n"
            "export function httpTransport(base: string): Transport {\n"
            "  const root = base.replace(/\\/$/, \"\");\n"
            "  return {\n"
            "    async call(method: string, args: unknown[]): Promise<unknown> {\n"
            "      const res = await fetch(`${root}/${method}`, {\n"
            "        method: \"POST\",\n"
            "        headers: { \"Content-Type\": \"application/json\" },\n"
            "        body: JSON.stringify(args),\n"
            "      });\n"
            "      const reply = await res.json();\n"
            "      if (!reply || reply.ok !== true) {\n"
            "        throw new Error(\n"
            "          `revl remote call ${method} failed: ` +\n"
            "          JSON.stringify(reply?.diagnostics ?? reply));\n"
            "      }\n"
            "      return reply.value;\n"
            "    },\n"
            "  };\n"
            "}")
        if type_decls:
            parts.append("\n\n".join(type_decls))
        for block in service_blocks:
            parts.append("\n".join(block))
        return "\n\n".join(parts) + "\n"

    def _header(self, service_names: list[str], frontier: str) -> str:
        return "\n".join([
            "// Generated by `revl export client --lang ts` — a typed remote",
            "// client for a revl service (docs/interop-bridge.md, item 424 gap c).",
            "// Pure IR codegen over the canonical value encoding; no runtime.",
            f"// Services: {', '.join(service_names)}",
            "//",
            "// LOCAL CONTRACT ONLY. This client is typed and bounded on THIS side:",
            "// its wire shape, and its call surface. It sits on the SENDING side of",
            "// a seam and holds no gate over the remote, so it makes NO claim about",
            "// what the callee runs — there is no verified-remote badge (item 337's",
            "// seam invariant: only the receiver re-admits). A mutual guarantee",
            "// between two revl peers is `revl contract export`/`check`, not this.",
        ])


# -------------------------------------------- the webui face (457 S4 / 459)

#: the emission a component uses to contribute a frontend to the ambient WebUI
#: service, and the parameter on it that carries the reactive channel. Both names
#: are the Cordis WebUI shape design note 526 recorded (`addEntry(files, data)`),
#: so a revl `service WebUI` that declares them is projectable without a flag.
_WEBUI_EMISSION = "add_entry"
_WEBUI_DATA_PARAM = "data"


class _WebuiFaceExporter(_ClientExporter):
    """Project a component's Cordis WebUI channel into its TypeScript contract.

    This is the `--face webui` projection (design note 530 Decision B, item 457
    slice S4). Cordis WebUI's `addEntry(files, data)` publishes `data` as a
    reactive object the server mutates and the browser reads with `useRpc<T>()`,
    and any function on that object is an RPC method the browser may call
    (docs/design/526-webui-asset-alignment.md). Untyped on the raw JS API, the
    two halves are DECLARED in revl and read from one place each:

      * the reactive STATE is the record type of the `data` parameter on the
        component's `webui.add_entry` emission — the value the component actually
        publishes, so the published fields and the browser's type cannot drift;
      * the RPC METHOD SET is exactly the services the component `provides`
        (526's "an entry's exposed methods are exactly the component's declared
        provisions"), the enumerable boundary `revl audit` already reports as G1.

    Nothing is restated on the TS side: the emitted `<C>State`, `<C>Rpc` and
    `<C>Channel` are a projection of those declarations, the same way
    `revl export client --lang ts --service NAME` projects the REST client.
    """

    def _component(self, name: str) -> dict:
        for comp in self.ir.get("components") or []:
            if comp.get("name") == name:
                return comp
        known = ", ".join(
            sorted(c.get("name", "") for c in self.ir.get("components") or [])
        ) or "(none)"
        raise RevlError("<ir>", 0,
                        f"no component named `{name}` in this IR",
                        hint=f"known components: {known}")

    def _channel_state_type(self, comp: dict) -> tuple[str, str]:
        """`(webui service name, the `data` parameter's declared type)`.

        The webui requirement is found by its CONTRACT, not by its key name: the
        required service that declares `add_entry` with a `data` parameter. A
        component with no such requirement has no channel to project, and a
        component whose `add_entry` takes no `data` is the pre-projection surface
        the gap note named; both are refused here rather than emitted empty.
        """
        candidates = []
        for key, sname in (comp.get("requires") or {}).items():
            method = ((self.services.get(sname) or {}).get("methods")
                      or {}).get(_WEBUI_EMISSION)
            if method is not None:
                candidates.append((key, sname, method))
        if not candidates:
            raise RevlError(
                "<ir>", 0,
                f"component `{comp['name']}` has no webui channel to project",
                hint=f"a webui face needs a `requires` on a service declaring "
                     f"`emission fn {_WEBUI_EMISSION}(...)` — the ambient WebUI "
                     f"coeffect (docs/frontend-assets.md)")
        key, sname, method = candidates[0]
        for param in method.get("params") or []:
            if param.get("name") == _WEBUI_DATA_PARAM:
                state = param.get("type")
                spec = self.types.get(state or "")
                if not spec or spec.get("kind") != "record":
                    raise RevlError(
                        "<ir>", 0,
                        f"`{sname}.{_WEBUI_EMISSION}`'s `{_WEBUI_DATA_PARAM}` "
                        f"parameter is `{state}`, which is not a declared record "
                        f"type, so the reactive state has no fields to project",
                        hint="the reactive channel is a record: its fields are "
                             "the state the browser reads through `useRpc`")
                reason = self._inexpressible_reason(state)
                if reason is not None:
                    raise RevlError(
                        "<ir>", 0,
                        f"cannot project the webui channel of "
                        f"`{comp['name']}`: its reactive state {reason}")
                return sname, state
        raise RevlError(
            "<ir>", 0,
            f"`{sname}.{_WEBUI_EMISSION}` declares no `{_WEBUI_DATA_PARAM}` "
            f"parameter, so this component publishes no typed reactive channel",
            hint=f"add `{_WEBUI_DATA_PARAM}: <RecordType>` to "
                 f"`{_WEBUI_EMISSION}` and publish it from the `emit`")

    def _rpc_methods(self, comp: dict) -> list[tuple[str, str, dict]]:
        """`(provision key, service name, methods)` for every service the
        component provides — the RPC surface, in declaration order."""
        out = []
        for key, sname in (comp.get("provides") or {}).items():
            methods = (self.services.get(sname) or {}).get("methods") or {}
            out.append((key, sname, methods))
        if not out:
            raise RevlError(
                "<ir>", 0,
                f"component `{comp['name']}` provides nothing, so its webui "
                f"channel has no RPC surface to project",
                hint="the browser-callable methods are exactly the component's "
                     "declared provisions; declare one with `provides`")
        return out

    def _rpc_signature(self, cname: str, op_name: str, spec: dict) -> str:
        params = spec.get("params") or []
        for param in params:
            reason = self._inexpressible_reason(param.get("type"))
            if reason is not None:
                raise RevlError(
                    "<ir>", 0,
                    f"cannot project the webui channel of `{cname}`: RPC "
                    f"parameter `{param.get('name')}` of `{op_name}` {reason}")
        returns = spec.get("returns")
        reason = self._inexpressible_reason(returns)
        if reason is not None:
            raise RevlError(
                "<ir>", 0,
                f"cannot project the webui channel of `{cname}`: the result of "
                f"RPC `{op_name}` {reason}")
        sig = ", ".join(
            f"{p['name']}: {self.ts_type(p.get('type'))}" for p in params)
        ret = self.ts_type(returns) if returns and returns != "Unit" else "void"
        return f"  {op_name}({sig}): Promise<{ret}>;"

    def emit_face(self, cname: str) -> str:
        self._referenced: set[str] = set()
        comp = self._component(cname)
        webui_service, state_type = self._channel_state_type(comp)
        rpc = self._rpc_methods(comp)

        state_spec = self.types[state_type]
        state_lines = [
            "/** The reactive state the server publishes to the browser over "
            "Cordis WebUI's",
            f" *  `data` channel. Projected from `{state_type}`, the declared "
            f"type of the",
            f" *  `{_WEBUI_DATA_PARAM}` parameter `{cname}` publishes through",
            f" *  `{webui_service}.{_WEBUI_EMISSION}`, so the published fields "
            "and this type are ONE",
            " *  declaration. Read-only: the browser observes the synced state "
            "and changes",
            " *  it through the RPC surface. */",
            f"export interface {cname}State {{",
        ]
        for fname, ftype in (state_spec.get("fields") or {}).items():
            state_lines.append(f"  readonly {fname}: {self.ts_type(ftype)};")
        state_lines.append("}")

        provisions = ", ".join(f"`{key}: {sname}`" for key, sname, _ in rpc)
        rpc_lines = [
            "/** The RPC methods the browser may call on the server: exactly the "
            "provisions",
            f" *  `{cname}` declares, the enumerable boundary `revl audit` "
            "reports as G1",
            f" *  ({provisions}). Every call is asynchronous: it crosses the "
            "WebSocket",
            " *  seam Cordis WebUI opens for the entry. */",
            f"export interface {cname}Rpc {{",
        ]
        for _key, _sname, methods in rpc:
            for op_name, spec in methods.items():
                rpc_lines.append(self._rpc_signature(cname, op_name, spec))
        rpc_lines.append("}")

        closure = self._type_closure(set(self._referenced))
        type_decls = [self._type_decl(name) for name in self.types
                      if name in closure and name != state_type]

        parts = [self._face_header(cname, webui_service, state_type, rpc)]
        if type_decls:
            parts.append("\n\n".join(type_decls))
        parts.append("\n".join(state_lines))
        parts.append("\n".join(rpc_lines))
        parts.append(
            f"/** The whole typed channel `useRpc<{cname}Channel>()` reads in the\n"
            f" *  client extension: the synced reactive state and the callable "
            f"RPC\n *  surface, one declaration each. */\n"
            f"export type {cname}Channel = {cname}State & {cname}Rpc;")
        return "\n\n".join(parts) + "\n"

    def _face_header(self, cname: str, webui_service: str, state_type: str,
                     rpc: list[tuple[str, str, dict]]) -> str:
        provisions = ", ".join(f"{key}: {sname}" for key, sname, _ in rpc)
        return "\n".join([
            "// Generated by `revl export client --lang ts --face webui` — the "
            "typed",
            "// reactive-state / RPC channel of a revl component's Cordis WebUI "
            "entry",
            "// (docs/frontend-assets.md; design notes 526 and 530 Decision B, "
            "item 457 S4).",
            "//",
            f"// Component: {cname}",
            f"// Reactive state: {state_type} (the `{_WEBUI_DATA_PARAM}` "
            f"parameter of `{webui_service}.{_WEBUI_EMISSION}`)",
            f"// RPC surface:   {provisions} (the component's declared "
            f"provisions)",
            "//",
            "// DO NOT EDIT. Regenerate it from the declaration instead: the "
            "point of the",
            "// projection is that the server's published fields and the "
            "browser's `useRpc`",
            "// type are one declaration, so a hand edit here is a second, "
            "unchecked one.",
            "//",
            "// LOCAL CONTRACT ONLY, like every generated client: it is typed and "
            "bounded on",
            "// THIS side and makes no claim about what the peer runs (item 337's "
            "seam",
            "// invariant — only the receiver re-admits).",
        ])


# --------------------------------------------------------------- public API

_LANGS = ("ts",)

#: the projection faces. `rest` is the slice-C1 remote client (the canonical
#: encoding over a transport); `webui` is the Cordis WebUI reactive-state / RPC
#: channel of one component (item 457 slice S4, design note 530 Decision B).
_FACES = ("rest", "webui")


def export_client(ir: dict, *, lang: str = "ts", service: str | None = None,
                  composition: bool = False, face: str = "rest",
                  component: str | None = None) -> str:
    """Render a compiled revl IR as a typed client for a non-revl consumer.

    `face` picks WHICH typed boundary is projected:

    * `rest` (the default) is the remote client: exactly one of `service` (a
      single service by name) or `composition` (every service the composition
      provides) selects what to export.
    * `webui` is the browser channel of one `component`: the reactive state it
      publishes through its `webui.add_entry` emission and the RPC methods it
      provides, the surface `useRpc<T>()` reads (docs/frontend-assets.md).

    `lang` is the target language; `ts` is the slice-C1 target.
    """
    if lang not in _LANGS:
        raise RevlError("<ir>", 0,
                        f"unknown client language `{lang}` "
                        f"(supported: {', '.join(_LANGS)})")
    if face not in _FACES:
        raise RevlError("<ir>", 0,
                        f"unknown client face `{face}` "
                        f"(supported: {', '.join(_FACES)})")
    services = ir.get("services") or {}
    if face == "webui":
        if service is not None or composition:
            raise RevlError(
                "<ir>", 0,
                "the `webui` face projects a COMPONENT's browser channel, not a "
                "service: pass `--component NAME`",
                hint="the channel is one component's published state plus its "
                     "declared provisions, so a service name does not name it")
        if component is None:
            raise RevlError("<ir>", 0,
                            "select what to export: `--component NAME`")
        return _WebuiFaceExporter(ir).emit_face(component)
    if component is not None:
        raise RevlError(
            "<ir>", 0,
            "`--component` selects a `--face webui` channel; the default rest "
            "face exports a service or a composition")
    if service is not None and composition:
        raise RevlError("<ir>", 0,
                        "choose one of `--service` or `--composition`, not both")
    if service is not None:
        if service not in services:
            known = ", ".join(sorted(services)) or "(none)"
            raise RevlError("<ir>", 0, f"no service named `{service}` in this IR",
                            hint=f"known services: {known}")
        names = [service]
    elif composition:
        names = []
        for comp in ir.get("components") or []:
            for provided in (comp.get("provides") or {}).values():
                if provided in services and provided not in names:
                    names.append(provided)
        if not names:
            raise RevlError("<ir>", 0,
                            "this composition provides no services to export",
                            hint="a composition's client surface is what its "
                                 "components `provide`; nothing is provided here")
    else:
        raise RevlError("<ir>", 0,
                        "select what to export: `--service NAME` or `--composition`")
    return _ClientExporter(ir).emit(names)

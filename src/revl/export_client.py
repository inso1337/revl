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

NOTE (the paired server face, not built here): `revl serve --http` is C1's other
half (D-424c.6) and the remoteness LANGUAGE constructs (`remote` rows, the G4
admissibility check, per-realm peers) are C2, which waits on item 426 S1's row
table. This slice ships the client generator alone.
"""

from __future__ import annotations

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

    def _service_block(self, sname: str) -> list[str]:
        methods = (self.services.get(sname) or {}).get("methods") or {}
        # refuse a method the projection cannot express, naming the method
        for op_name, spec in methods.items():
            for param in spec.get("params") or []:
                reason = self._inexpressible_reason(param.get("type"))
                if reason is not None:
                    raise RevlError(
                        "<ir>", 0,
                        f"cannot export a client for `{sname}.{op_name}`: "
                        f"parameter `{param.get('name')}` {reason}")
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
            "  constructor(private readonly transport: Transport) {}",
            "",
        ]
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


# --------------------------------------------------------------- public API

_LANGS = ("ts",)


def export_client(ir: dict, *, lang: str = "ts", service: str | None = None,
                  composition: bool = False) -> str:
    """Render a compiled revl IR as a typed remote client.

    Exactly one of `service` (a single service by name) or `composition`
    (every service the composition provides) selects what to export. `lang`
    is the target language; `ts` is the slice-C1 target.
    """
    if lang not in _LANGS:
        raise RevlError("<ir>", 0,
                        f"unknown client language `{lang}` "
                        f"(supported: {', '.join(_LANGS)})")
    services = ir.get("services") or {}
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

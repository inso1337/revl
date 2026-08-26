"""revl IR -> WIT (WebAssembly Interface Types).

`revl export wit` is the reverse of `revl import wit` (docs/wit-bridge.md): it
runs the importer's type mapping *backwards*, over a compiled IR, to emit the
standard WIT interface a revl service or composition presents. It is pure
codegen — no runtime, no emission, no canonical-ABI binary (that is Slice 3,
the gated horizon: see the module NOTE below and docs/wit-bridge.md §5). This
is the interoperability *documentation* the Component Model asks for, produced
before any binary compatibility.

What WIT can carry, and what it cannot
--------------------------------------
WIT describes **shape**: names, parameters, results, records, variants, enums,
and resources. That is exactly what this module reverses out of the IR.

WIT describes **nothing about effects**. revl's `emission` classification,
its capability scopes (`emission[db]`), `async`, `commutative`, an
`extern acquire`'s `undo` inverse, an `emission`'s `compensate` — all of that
is *verified* by the revl compiler and travels with a revl component as
metadata **alongside** the shape. A standard WIT interface cannot express any
of it. So the honest thing this exporter does is:

  * emit `/// @revl:pure` above a plain operation — the one datum the importer
    round-trips back into a revl classification (an interface-author's claim);
  * emit the rest of the lifecycle facts as `/// @revl:*` **doc comments** — a
    standard WIT toolchain reads them as prose, a revl-aware one can recover
    them, and the WIT *type system* still says nothing. That is the precise
    sense in which a revl-authored component is the first standard component
    whose lifecycle and effects are verified: the verification rides alongside
    the WIT, it is not *in* the WIT.

NOTE (Slice 3 — the gated horizon, not built here): turning this WIT plus a
revl component into a canonical-ABI WASI Preview 2 **binary** is the next
slice. It is gated on the wasm tier's Str/records lowering restrictions (hard
`EmitError`s in `backends/wasm/emit.py`), so this module touches no emitter and
emits no binary — only the interface text.
"""

from __future__ import annotations

import re

from .errors import RevlError

# ---------------------------------------------------------------- type mapping

#: revl surface type -> WIT type. The reverse of import_wit's `_PRIMITIVES`.
#: `Int` collapsed every WIT integer width on the way in; on the way out it
#: becomes the widest signed spelling, `s64`, which the header notes.
_PRIMITIVES = {
    "Str": "string",
    "Bool": "bool",
    "Int": "s64",
    "Float": "f64",
    "Bytes": "list<u8>",
}


def _kebab_type(name: str) -> str:
    """PascalCase type/case name -> WIT kebab-case (`InStock` -> `in-stock`)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "-", spaced)
    return spaced.lower()


def _kebab_name(name: str) -> str:
    """snake_case field/op/param name -> WIT kebab-case."""
    return name.replace("_", "-")


def _split_generic(type_str: str) -> tuple[str, list[str]]:
    """`Result[Item, Str]` -> (`Result`, [`Item`, `Str`]); `Str` -> (`Str`, [])."""
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


class _Exporter:
    def __init__(self, ir: dict, package: str) -> None:
        self.ir = ir
        self.package = package
        self.services: dict = ir.get("services") or {}
        self.types: dict = ir.get("types") or {}
        # a resource type is one an `extern acquire` returns — the same verdict
        # distribute.py reads (a handle that crosses by proxy, not by copy)
        self.acquire: dict[str, dict] = {
            ext["returns"]: ext
            for ext in ir.get("externs") or []
            if ext.get("class") == "acquire" and ext.get("returns")
        }
        self.resources: set[str] = set(self.acquire)
        self.notes: list[str] = []
        self._referenced: set[str] = set()

    # -- reverse type mapping ---------------------------------------------
    def wit_type(self, type_str: str | None) -> str:
        if not type_str:
            raise RevlError("<ir>", 0, "cannot render an empty type to WIT")
        if type_str in _PRIMITIVES:
            return _PRIMITIVES[type_str]
        head, args = _split_generic(type_str)
        if head == "List" and len(args) == 1:
            return f"list<{self.wit_type(args[0])}>"
        if head == "Opt" and len(args) == 1:
            return f"option<{self.wit_type(args[0])}>"
        if head == "Result" and len(args) == 2:
            ok, err = args
            if ok == "Unit" and err == "Unit":
                return "result"
            ok_wit = "_" if ok == "Unit" else self.wit_type(ok)
            if err == "Unit":
                return f"result<{ok_wit}>"
            return f"result<{ok_wit}, {self.wit_type(err)}>"
        if head == "Map":
            raise RevlError(
                "<ir>", 0,
                f"WIT has no map type, so `{type_str}` cannot be exported",
                hint="WIT 0.2 has no `map`; model it as a `list` of a two-field "
                     "`record`, or expose the pairs through a resource")
        if head in ("List", "Opt", "Result"):
            raise RevlError("<ir>", 0,
                            f"malformed generic type `{type_str}` in the IR")
        # a nominal type: record / variant / enum / resource
        self._referenced.add(head)
        return _kebab_type(head)

    # -- resource / method classification ---------------------------------
    def _resource_methods(self, methods: dict) -> dict[str, list[tuple[str, dict]]]:
        """resource type -> [(method-name-without-prefix, op spec)]. A method is
        an op whose first parameter is `self` typed as a resource, whose name is
        the resource prefix + the method name — the shape the importer emits."""
        grouped: dict[str, list[tuple[str, dict]]] = {}
        for op_name, spec in methods.items():
            params = spec.get("params") or []
            if not params or params[0].get("name") != "self":
                continue
            handle = params[0].get("type")
            if handle not in self.resources:
                continue
            # the importer prefixes an op with the resource's *kebab* name run
            # through `_snake` (`Descriptor` -> `descriptor` -> `descriptor_`)
            prefix = f"{_snake(_kebab_type(handle))}_"
            if not op_name.startswith(prefix):
                continue
            grouped.setdefault(handle, []).append(
                (op_name[len(prefix):], spec))
        return grouped

    # -- emission (alongside metadata) ------------------------------------
    def _effect_docs(self, spec: dict, indent: str) -> list[str]:
        """The `/// @revl:*` lines carried ALONGSIDE the WIT shape. Only
        `@revl:pure` round-trips into a revl classification; the rest are
        advisory because WIT's type system cannot represent an effect."""
        lines: list[str] = []
        if spec.get("emission"):
            caps = spec.get("capabilities")
            scope = f" [{', '.join(caps)}]" if caps else ""
            lines.append(f"{indent}/// @revl:emission{scope}"
                         " (WIT carries no effect; verified alongside)")
        else:
            lines.append(f"{indent}/// @revl:pure")
        if spec.get("async"):
            lines.append(f"{indent}/// @revl:async")
        if spec.get("commutative"):
            lines.append(f"{indent}/// @revl:commutative")
        return lines

    def _func_decl(self, wit_name: str, spec: dict, indent: str,
                   drop_first: bool = False) -> list[str]:
        params = list(spec.get("params") or [])
        if drop_first:
            params = params[1:]
        sig = ", ".join(
            f"{_kebab_name(p['name'])}: {self.wit_type(p.get('type'))}"
            for p in params)
        returns = spec.get("returns")
        arrow = ""
        if returns and returns != "Unit":
            arrow = f" -> {self.wit_type(returns)}"
        lines = self._effect_docs(spec, indent)
        lines.append(f"{indent}{wit_name}: func({sig}){arrow};")
        return lines

    def _resource_block(self, handle: str,
                        methods: list[tuple[str, dict]]) -> list[str]:
        wit = _kebab_type(handle)
        lines = ["  /// A resource (a live handle). Its revl construction is an",
                 "  /// `extern acquire` whose `undo` is this destructor (G4).",
                 f"  resource {wit} {{"]
        ext = self.acquire.get(handle) or {}
        ctor_params = ext.get("params") or []
        ctor_sig = ", ".join(
            f"{_kebab_name(p['name'])}: {self.wit_type(p.get('type'))}"
            for p in ctor_params)
        lines.append(f"    constructor({ctor_sig});")
        for name, spec in methods:
            lines.extend(self._func_decl(_kebab_name(name), spec, "    ",
                                         drop_first=True))
        lines.append("  }")
        return lines

    # -- collect the nominal types actually referenced --------------------
    def _nominal_refs(self, type_str: str | None) -> list[str]:
        """The nominal type names a type string names, peeled out of any
        `List`/`Opt`/`Result`/`Map` generic wrapper. `Result[Item, Str]` ->
        `[Item]`; `Str` -> `[]`; `Item` -> `[Item]`. Pure — no side effects."""
        if not type_str:
            return []
        head, args = _split_generic(type_str)
        if head in _PRIMITIVES or head in ("List", "Opt", "Result", "Map",
                                           "Unit"):
            refs: list[str] = []
            for arg in args:
                refs.extend(self._nominal_refs(arg))
            return refs
        return [head]

    def _type_closure(self, seeds: set[str]) -> set[str]:
        """Every record/variant/enum reachable from `seeds` through record
        fields and variant payloads. Resources are excluded — they emit as
        `resource` blocks, not type declarations."""
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
        """The single-line WIT declaration for one nominal type."""
        spec = self.types[name]
        wit = _kebab_type(name)
        if spec.get("kind") == "record":
            fields = ", ".join(
                f"{_kebab_name(fname)}: {self.wit_type(ftype)}"
                for fname, ftype in (spec.get("fields") or {}).items())
            return f"record {wit} {{ {fields} }}"
        cases = spec.get("cases") or []
        if all(not c.get("payload") for c in cases):
            body = ", ".join(_kebab_type(c["name"]) for c in cases)
            return f"enum {wit} {{ {body} }}"
        body = ", ".join(
            _kebab_type(c["name"])
            + (f"({self.wit_type(c['payload'])})" if c.get("payload") else "")
            for c in cases)
        return f"variant {wit} {{ {body} }}"

    # -- the whole file ---------------------------------------------------
    def emit(self, service_names: list[str]) -> str:
        # assign each resource to the first service that owns its methods
        owner_of: dict[str, str] = {}
        # First pass: build each interface's function/resource body, capturing
        # the nominal types each service references directly in its signatures.
        built: list[tuple[str, str, list[str], set[str]]] = []
        for sname in service_names:
            # capture the nominal types THIS service references on its own —
            # a fresh set per service, so a type an earlier interface already
            # named still counts as referenced here (it needs a `use`).
            self._referenced = set()
            methods = (self.services.get(sname) or {}).get("methods") or {}
            grouped = self._resource_methods(methods)
            resource_lines: list[str] = []
            for handle, ms in grouped.items():
                if handle in owner_of:
                    continue
                owner_of[handle] = sname
                resource_lines.extend(self._resource_block(handle, ms))
            grouped_ops = {
                f"{_snake(_kebab_type(handle))}_{name}"
                for handle, ms in grouped.items() for name, _ in ms
            }
            free_lines: list[str] = []
            for op_name, spec in methods.items():
                if op_name in grouped_ops:
                    continue
                free_lines.extend(self._func_decl(_kebab_name(op_name), spec, "  "))
            iface = _kebab_type(sname)
            direct = set(self._referenced)
            built.append((sname, iface, resource_lines + free_lines, direct))

        # Referenced user-defined types must live INSIDE an interface (top-level
        # `record`/`variant`/`enum` is invalid WIT). Assign each type to the
        # first interface whose signatures reach it (directly or through another
        # type's fields/payloads); every later interface that also references it
        # brings it in with a `use`. Because ownership follows the first
        # reaching interface, a `use` only ever points at an earlier interface —
        # the cross-interface `use` graph is acyclic by construction.
        type_owner: dict[str, str] = {}
        for _sname, iface, _body, direct in built:
            for name in sorted(self._type_closure(direct)):
                type_owner.setdefault(name, iface)

        service_blocks: list[str] = []
        for _sname, iface, body, direct in built:
            refs = self._type_closure(direct)
            # types this interface references but another interface owns -> use
            foreign: dict[str, list[str]] = {}
            for name in refs:
                owner = type_owner.get(name)
                if owner and owner != iface:
                    foreign.setdefault(owner, []).append(name)
            use_lines = [
                f"  use {owner}.{{"
                + ", ".join(_kebab_type(n) for n in sorted(names)) + "};"
                for owner, names in sorted(foreign.items())
            ]
            # types this interface owns, declared in stable `self.types` order
            type_lines = [
                f"  {self._type_decl(name)}"
                for name in self.types
                if type_owner.get(name) == iface
            ]
            iface_body = use_lines + type_lines + body
            service_blocks.append(
                f"/// revl service `{_sname}` as a WIT interface.\n"
                f"interface {iface} {{\n" + "\n".join(iface_body) + "\n}")

        parts = [self._header(service_names)]
        parts.append(f"package {self.package};")
        parts.extend(service_blocks)
        return "\n\n".join(parts) + "\n"

    def _header(self, service_names: list[str]) -> str:
        lines = [
            "// Generated by `revl export wit` — the standard WIT shape of a revl",
            "// service/composition (docs/wit-bridge.md). Pure codegen from the IR:",
            "// the `revl import wit` type mapping, run backwards.",
            f"// Services exported: {', '.join(service_names)}",
            "//",
            "// WIT DESCRIBES SHAPE, NOT EFFECTS. revl's verified lifecycle —",
            "// `emission`, capability scopes, `async`, `commutative`, an `acquire`'s",
            "// `undo` inverse, an `emission`'s `compensate` — has no place in a WIT",
            "// type. It rides ALONGSIDE, in the `/// @revl:*` doc comments below: a",
            "// standard WIT toolchain reads them as prose; a revl-aware one recovers",
            "// them. Only `/// @revl:pure` round-trips back into a classification.",
            "//",
            "// Number widths are lossy the other way: revl `Int` became WIT `s64`",
            "// (import had collapsed every integer width to `Int`).",
            "//",
            "// Slice 3 (horizon, not built): a canonical-ABI WASI Preview 2 binary is",
            "// gated on the wasm tier's Str/records lowering — this is interface text",
            "// only, no emitter touched.",
        ]
        return "\n".join(lines)


# import_wit's `_snake`, needed to reconstruct an op's resource prefix. Kept
# local (a one-liner) rather than importing, so export has no import-time
# coupling to the importer's internals beyond the name convention it reverses.
def _snake(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name.replace("-", "_"))
    return ident


# --------------------------------------------------------------- public API

def export_wit(ir: dict, *, service: str | None = None,
               composition: bool = False, package: str = "revl:exported") -> str:
    """Render a compiled revl IR as a WIT interface document.

    Exactly one of `service` (a single service by name) or `composition`
    (every service the composition provides) selects what to export.
    """
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
                            hint="a composition's WIT surface is what its "
                                 "components `provide`; nothing is provided here")
    else:
        raise RevlError("<ir>", 0,
                        "select what to export: `--service NAME` or `--composition`")
    return _Exporter(ir, package).emit(names)

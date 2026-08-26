"""`revl scaffold` — a typed, holed composition skeleton from a small spec.

Scaffolding is the front half of the scaffold-then-fill loop (docs/holes.md §8):
write a component whose not-yet-known expressions are `hole`s, let the checker
render its verdict on the parts that *are* written, then fill one hole at a time
against its fill spec. This module is the generator — it turns a CLI spec into
skeleton `.rvl` source. It invents *structure* (the service contract, the
component wiring, the effect scaffolding), never *finished code*: every place a
real decision belongs becomes a `hole[T]` obligation the compiler tracks.

The generator is deliberately conservative about authority, which is the whole
point applied to codegen:

* it declares only the capabilities the spec explicitly names, and an emission
  bound only for a capability whose boundary the spec actually injects
  (`--requires`); it never widens to a boundary that was not asked for;
* a requested capability whose boundary is *not* injected is not silently
  wired — the operation that would need it stays a `hole`, so the missing
  authority is a compiler-tracked obligation rather than a granted permission;
* the skeleton compiles as a draft (holes check) but is never admissible while a
  hole remains (admission refuses holes, docs/holes.md §4).

`build_skeleton(spec)` renders the source; `scaffold_document(spec)` compiles it
and returns the source together with its obligations and each hole's fill spec
(the `revl_check` shape, reused verbatim), so `revl scaffold --json` hands an
agent the skeleton and its remaining work in one response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .compiler import compile_source
from .mcp.fillspec import enrich


class ScaffoldError(Exception):
    """A spec the generator will not honor — a malformed signature, or a
    request that could only be satisfied by inventing authority the spec did
    not grant. Refusing beats emitting a widened or dishonest skeleton."""


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SIG = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*->\s*(.+?)\s*$")


@dataclass
class Method:
    """One service operation. `emits` marks it as crossing the emission
    boundary; its bound is the spec's wired capabilities, decided in `Spec`."""
    name: str
    params: list[tuple[str, str]]
    returns: str
    emits: bool = False


@dataclass
class Spec:
    """A scaffold request, already parsed and validated."""
    service: str
    component: str
    provides: str
    requires: dict[str, str] = field(default_factory=dict)  # key -> Service
    capabilities: list[str] = field(default_factory=list)   # human labels
    methods: list[Method] = field(default_factory=list)
    config: list[tuple[str, str]] = field(default_factory=list)
    effect: bool = True
    resource_type: str = ""

    def wired_roots(self) -> list[str]:
        """Capability roots whose boundary the spec injects (`--requires`).

        A capability names the boundary a crossing goes through, and that
        boundary is a requirement key (docs/capabilities.md §2). The root of a
        dotted label like `filesystem.read` is `filesystem`; the `.read`
        refinement is below revl's granularity, so it survives as documentation,
        not as a distinct capability. A root that is not an injected key is
        *not* wired here — emitting a bound for it would grant authority the
        spec never handed the component."""
        roots: list[str] = []
        for cap in self.capabilities:
            root = cap.split(".", 1)[0]
            if root in self.requires and root not in roots:
                roots.append(root)
        return sorted(roots)

    def unwired(self) -> list[str]:
        """Requested capabilities with no injected boundary — recorded in the
        obligation prose so the gap is visible, never silently wired."""
        return [cap for cap in self.capabilities
                if cap.split(".", 1)[0] not in self.requires]


def _ident(value: str, what: str) -> str:
    if not _IDENT.match(value):
        raise ScaffoldError(f"{what} must be an identifier, found {value!r}")
    return value


def parse_requires(item: str) -> tuple[str, str]:
    """`key` or `key:Service` — the injected dependency's wiring name and its
    service type. A bare key defaults its service to the key capitalized."""
    key, _, service = item.partition(":")
    key = _ident(key.strip(), "a requires key")
    service = service.strip() or (key[:1].upper() + key[1:])
    return key, _ident(service, "a service name")


def parse_config(item: str) -> tuple[str, str]:
    """`name:Type` — one component config field."""
    name, sep, typ = item.partition(":")
    if not sep or not typ.strip():
        raise ScaffoldError(
            f"a config field is `name:Type`, found {item!r}")
    return _ident(name.strip(), "a config field name"), typ.strip()


def parse_method(sig: str, emits: bool) -> Method:
    """`name(p: T, q: U) -> R` — a return type is required: a provide body is
    `= hole[R]`, and the hole needs a type context to check (docs/holes.md §2)."""
    match = _SIG.match(sig)
    if not match:
        raise ScaffoldError(
            f"a method is `name(param: Type, ...) -> Return`, found {sig!r}"
            " — a return type is required so the hole in its body has a type")
    name, raw_params, returns = match.groups()
    params: list[tuple[str, str]] = []
    for part in (p for p in raw_params.split(",") if p.strip()):
        pname, sep, ptype = part.partition(":")
        if not sep or not ptype.strip():
            raise ScaffoldError(
                f"parameter {part.strip()!r} of {name!r} needs a `name: Type`")
        params.append((_ident(pname.strip(), "a parameter name"),
                       ptype.strip()))
    return Method(_ident(name, "a method name"), params, returns.strip(), emits)


def build_spec(*, service: str, provides: str | None = None,
               component: str | None = None,
               requires: list[str] | None = None,
               capabilities: list[str] | None = None,
               methods: list[str] | None = None,
               emits: list[str] | None = None,
               config: list[str] | None = None,
               effect: bool = True,
               resource_type: str | None = None) -> Spec:
    """Validate the raw CLI strings into a `Spec`, filling defaults.

    The one conservative refusal lives here: an emission method with no wired
    capability would have to be bound to nothing, which revl spells as bare
    `emission` — "any boundary". That is exactly the silent widening the
    generator exists to avoid, so it refuses instead."""
    service = _ident(service, "a service name")
    provides = _ident(provides or (service[:1].lower() + service[1:]),
                      "a provides key")
    component = _ident(component or f"{service}Provider", "a component name")

    req: dict[str, str] = {}
    for item in requires or []:
        key, svc = parse_requires(item)
        if key in req and req[key] != svc:
            raise ScaffoldError(
                f"requires key {key!r} is bound to two services")
        req[key] = svc

    spec = Spec(
        service=service, component=component, provides=provides,
        requires=req, capabilities=list(capabilities or []),
        config=[parse_config(c) for c in config or []],
        effect=effect,
        resource_type=_ident(resource_type or f"{service}Resource",
                              "a resource type"),
    )

    parsed = [parse_method(m, emits=False) for m in methods or []]
    parsed += [parse_method(m, emits=True) for m in emits or []]
    if not parsed:
        # A default entry point so the one-line spec yields a useful skeleton:
        # it emits when the spec wired a boundary to emit through, otherwise it
        # is pure.
        parsed = [Method("run", [("input", "Str")], "Str",
                         emits=bool(spec.wired_roots()))]
    spec.methods = parsed

    if any(m.emits for m in spec.methods) and not spec.wired_roots():
        raise ScaffoldError(
            "an emission method needs a capability whose boundary is injected: "
            "declare `--requires <key>` and `--capabilities <key>` together, or "
            "drop --emits so the method scaffolds pure — the generator will not "
            "bind an emission to `any boundary`")
    return spec


def _render_signature(method: Method, bound: list[str]) -> str:
    """A service method's declared line: `emission[a, b] fn m(p: T) -> R`."""
    params = ", ".join(f"{n}: {t}" for n, t in method.params)
    prefix = f"emission[{', '.join(bound)}] " if method.emits else ""
    return f"  {prefix}fn {method.name}({params}) -> {method.returns}"


def _provide_body(method: Method, unwired: list[str]) -> str:
    """A provide method: `fn m(p) = hole[R] "obligation"`. The message names the
    boundary a fill may cross, and flags any capability the spec asked for but
    did not inject — a gap the fill must not paper over by emitting."""
    names = ", ".join(n for n, _ in method.params)
    note = ""
    if method.emits:
        note = " (a fill here may emit through the declared boundary)"
    elif unwired:
        note = (f" — the spec named capability {', '.join(unwired)} but injected"
                " no boundary for it, so a fill here must stay pure; add"
                " `--requires`/`--capabilities` to grant it")
    message = f"produce {method.name}'s {method.returns} result{note}"
    return f"    fn {method.name}({names}) = hole[{method.returns}] \"{message}\""


def build_skeleton(spec: Spec) -> str:
    """Render the scaffold as `.rvl` source. Deterministic: the same spec always
    produces byte-identical output, so a re-scaffold is a clean diff."""
    wired = spec.wired_roots()
    unwired = spec.unwired()
    lines: list[str] = [
        "// Scaffold generated by `revl scaffold` (docs/scaffold.md).",
        "// Every `hole[T]` below is an open obligation: this file compiles as a",
        "// draft, but admission refuses it until each hole is filled (docs/holes.md).",
        "",
    ]

    # Stub port services for the injected dependencies. The scaffold does not
    # invent a dependency's contract, so each is an empty service the developer
    # fills with the methods the fills actually call.
    for svc in sorted(set(spec.requires.values())):
        if svc == spec.service:
            continue
        lines.append(f"// TODO: declare the operations {svc} must offer.")
        lines.append(f"service {svc} {{ }}")
        lines.append("")

    # The provided service contract.
    lines.append(f"service {spec.service} {{")
    for method in spec.methods:
        lines.append(_render_signature(method, wired))
    lines.append("}")
    lines.append("")

    # The component: wiring is real, bodies are obligations.
    header = f"component {spec.component}"
    for key in spec.requires:
        header += f" requires {key}: {spec.requires[key]}"
    header += f" provides {spec.provides}: {spec.service} {{"
    lines.append(header)
    if spec.config:
        fields = ", ".join(f"{n}: {t}" for n, t in spec.config)
        lines.append(f"  config {{ {fields} }}")

    if spec.effect:
        lines.append("")
        lines.append(f"  // The acquire/undo scaffolding is real; the resource"
                     " it yields is an obligation.")
        lines.append(f"  let resource = effect hole[{spec.resource_type}] "
                     f"\"acquire the resource {spec.component} manages; the undo"
                     " must fully release it (no residue)\"")
        lines.append("                 undo resource.release()")

    lines.append("")
    lines.append(f"  provide {spec.provides} {{")
    for method in spec.methods:
        lines.append(_provide_body(method, unwired))
    lines.append("  }")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def scaffold_document(spec: Spec, filename: str = "scaffold.rvl") -> dict:
    """Compile the skeleton and return it with its obligations and fill specs.

    The obligations carry the same `fillSpec` per hole that `revl_check` adds
    (`fillspec.enrich`), so an agent gets the skeleton and, for every hole, its
    expected type, capability upper bound, in-scope bindings and reachable
    services — the remaining work — in one response. `admissible` is the
    standing verdict: a scaffold with open holes never is (docs/holes.md §4)."""
    source = build_skeleton(spec)
    ir = compile_source(source, filename)
    holes = ir.get("holes") or []
    return {
        "ok": True,
        "source": source,
        "holeCount": len(holes),
        "admissible": not holes,
        "obligations": enrich(ir),
    }


__all__ = [
    "ScaffoldError", "Spec", "Method", "build_spec", "build_skeleton",
    "scaffold_document",
]

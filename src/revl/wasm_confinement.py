"""Generated wasm components are confined by default (issue #2729).

The wasm tier's confinement is physical: a component can reach only what its
import section declares, and the runtime can bound its work (fuel), its linear
memory, and its wall-clock time. Three pieces were only partial:

  * the emitter checked that the capability imports it *added* were in the
    component's ``requires`` — its own bookkeeping, read from the WAT it just
    produced. A component whose produced bytes carry any other import (an
    edited binary, a wrapper, a future host-import surface) was not checked;
  * ``run_component`` bounded only work (an optional ``fuel``) and wall-clock
    time (a fixed 120 s ``timeout``). Linear memory was unbounded, and every
    limit was left to the caller;
  * no policy clause made a generated component *require* confinement or state
    the ceilings it runs under.

This module is the enforcement half:

  * :func:`component_imports` reads the import section of the PRODUCED bytes —
    the embedded core modules of a component, recursively — and
    :func:`check_component_imports` refuses an undeclared capability import, a
    ``spawn:``/``instance:`` import naming a component outside the composition,
    or an import outside the expected namespaces;
  * :class:`ComponentLimits` is the fuel/memory/wall ceiling a generated
    component runs under, and :data:`DEFAULT_COMPONENT_LIMITS` is conservative
    so a caller that passes nothing still runs confined;
  * :func:`limits_from_policy` is the policy seam: a policy that declares
    ``wasm components confined`` but names no limits is refused with a named
    error rather than run unbounded, and an incomplete limit set is refused the
    same way.

The expected import namespaces are the wasm tier's own ABI surface
(``backends/wasm/emit.py``): capability channels (``coeffect:`` / ``route:``),
the spawn/config/dispose/instance seams (with their target checked against the
composition), the durable-WAL framing channel (``coeffect:revl:wal``, exempt
exactly as the emitter and ``least_authority`` exempt it), and the async job
pump. Anything else is not something a generated revl component may reach, so
it is refused rather than trusted. Component-level imports (the wrapper's own
``import-*`` canonical-lift plumbing) are decoded and checked too.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

from .errors import RevlError

# A conservative default: a generated component always runs with a ceiling,
# even when its caller names none. The wall-clock default matches the fixed
# timeout `run_component` used before limits existed, so no call that fitted
# before is refused now; the memory default is well above any emitted fixture.
DEFAULT_FUEL = 1_000_000_000
DEFAULT_MEMORY_BYTES = 64 * 1024 * 1024
DEFAULT_WALL_SECONDS = 120.0


class ConfinementError(RevlError):
    """A generated component was refused because it is not confined.

    Named, not a bare ``EmitError``: the caller can tell a confinement refusal
    (the import section is not what the component declared, or the policy names
    no limits) from a compile/emit failure, and the message names the policy or
    the offending import."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__("<component>", 0, message, hint=hint)


@dataclass(frozen=True)
class ComponentLimits:
    """The ceilings a generated component runs under, in wasmtime's terms.

    ``fuel`` bounds work (wasm instructions), ``memory_bytes`` bounds the
    linear memory a guest may grow to, and ``wall_seconds`` bounds elapsed
    time. All three are applied BEFORE execution; there is no unbounded mode.
    """

    fuel: int
    memory_bytes: int
    wall_seconds: float

    def __post_init__(self) -> None:
        for name, value in (("fuel", self.fuel),
                            ("memory_bytes", self.memory_bytes),
                            ("wall_seconds", self.wall_seconds)):
            if value is None or value <= 0:
                raise ValueError(
                    f"component limit `{name}` must be a positive number, "
                    f"got {value!r}")


DEFAULT_COMPONENT_LIMITS = ComponentLimits(
    fuel=DEFAULT_FUEL,
    memory_bytes=DEFAULT_MEMORY_BYTES,
    wall_seconds=DEFAULT_WALL_SECONDS,
)


# --------------------------------------------------------------------------- #
# the policy seam
# --------------------------------------------------------------------------- #

def limits_from_policy(policy) -> ComponentLimits:
    """The limits a generated component runs under for ``policy``.

    * no policy -> the default confinement (never unbounded);
    * a full limit set -> those limits;
    * ``wasm components confined`` with no limits, or only part of the set, ->
      a :class:`ConfinementError` naming what is missing. A policy that asks
      for confinement must state the ceilings; silently substituting a default
      would make the policy read like a promise it does not keep.
    """
    if policy is None:
        return DEFAULT_COMPONENT_LIMITS
    fuel = getattr(policy, "wasm_fuel", None)
    memory = getattr(policy, "wasm_memory_bytes", None)
    wall = getattr(policy, "wasm_wall_seconds", None)
    declared = {"fuel": fuel, "memory": memory, "wall": wall}
    present = {name: value for name, value in declared.items()
               if value is not None}
    if len(present) == len(declared):
        try:
            return ComponentLimits(fuel=fuel, memory_bytes=memory,
                                   wall_seconds=wall)
        except ValueError as exc:
            raise ConfinementError(
                f"the boundary policy's wasm component limits are not usable: "
                f"{exc}", hint=_LIMITS_HINT) from exc
    if present:
        missing = ", ".join(sorted(set(declared) - set(present)))
        raise ConfinementError(
            f"the boundary policy declares an incomplete set of wasm component "
            f"limits (missing {missing}); a limit set is fuel, memory and wall "
            f"together, and a partial set is refused rather than filled in",
            hint=_LIMITS_HINT)
    if getattr(policy, "wasm_confined", False):
        raise ConfinementError(
            "the boundary policy declares `wasm components confined` but names "
            "no limits; a generated component is confined by default, and a "
            "policy that asks for wasm confinement must also state the fuel, "
            "memory and wall ceilings. Refusing rather than running the "
            "component unbounded", hint=_LIMITS_HINT)
    return DEFAULT_COMPONENT_LIMITS


_LIMITS_HINT = (
    "declare the ceilings together: `wasm component limits fuel = 1000000000, "
    "memory = 64MiB, wall = 120s`, or drop `wasm components confined` to keep "
    "the default confinement (issue #2729)")


# --------------------------------------------------------------------------- #
# the byte-level import check
# --------------------------------------------------------------------------- #

_CORE_MAGIC = b"\0asm\x01\0\0\0"
_COMPONENT_MAGIC = b"\0asm\x0d\x00\x01\x00"

# Imports with no module-name suffix are the wasm tier's structural ABI seams.
_EXACT_MODULES = frozenset({"host", "config", "dispose", "revl:wal"})

# The durable-WAL framing channel is rendered under the capability prefix but
# is a first-party host binding, not a declared capability: `emit.py` and
# `least_authority._STRUCTURAL_COEFFECTS` both exempt it, so this check must too.
_STRUCTURAL_MODULES = frozenset({"coeffect:revl:wal"})

# Imports whose module name carries a target/key after a namespace prefix.
_PREFIXED_MODULES = ("coeffect:", "route:", "spawn:", "instance:")

# The namespaces that actually carry named authority.
_CAPABILITY_PREFIXES = ("coeffect:", "route:")


@dataclass(frozen=True)
class WasmImport:
    """One core-module import, read from produced bytes rather than the WAT."""

    module: str
    field: str

    def render(self) -> str:
        return f'"{self.module}" "{self.field}"'


def _uleb(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data):
            raise ConfinementError(
                "the wasm binary's import section is truncated")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 35:
            raise ConfinementError(
                "the wasm binary's import section has an over-long integer; "
                "refusing to guess at it")


def _name(data: bytes, pos: int) -> tuple[str, int]:
    length, pos = _uleb(data, pos)
    end = pos + length
    if end > len(data):
        raise ConfinementError("the wasm binary's import section is truncated")
    try:
        return data[pos:end].decode("utf-8"), end
    except UnicodeDecodeError as exc:
        raise ConfinementError(
            "the wasm binary's import section names an import in non-UTF-8 "
            f"bytes: {exc}") from exc


def _limits(data: bytes, pos: int) -> int:
    flags, pos = _uleb(data, pos)
    _minimum, pos = _uleb(data, pos)
    if flags & 0x01:
        _maximum, pos = _uleb(data, pos)
    return pos


def _skip_valtype(data: bytes, pos: int) -> int:
    """Advance past one value type.

    A typed reference (`0x63`/`0x64`, the GC function-reference encodings that
    wasmtime accepts) carries a heap type after the prefix, so a one-byte skip
    would misalign every later import. The heap type is a signed LEB; skipping
    it as an unsigned LEB consumes the same bytes."""
    if pos >= len(data):
        raise ConfinementError("the wasm binary's import section is truncated")
    if data[pos] in (0x63, 0x64):  # (ref null? <heaptype>)
        return _uleb(data, pos + 1)[1]
    return pos + 1


def _skip_import_desc(data: bytes, pos: int, kind: int) -> int:
    """Advance past one core import's type, so the next import's names line up."""
    if kind == 0x00:  # func: type index
        return _uleb(data, pos)[1]
    if kind == 0x01:  # table: reftype + limits
        return _limits(data, _skip_valtype(data, pos))
    if kind == 0x02:  # memory: limits
        return _limits(data, pos)
    if kind == 0x03:  # global: valtype + mutability
        return _skip_valtype(data, pos) + 1
    if kind == 0x04:  # tag: attribute + type index
        return _uleb(data, pos + 1)[1]
    raise ConfinementError(
        f"the wasm binary's import section uses unknown import kind 0x{kind:02x}; "
        "refusing to interpret an import shape this checker does not know")


def _component_level_import_names(payload: bytes) -> list[str]:
    """The names of a component import section's entries.

    The wrapper `wasm-tools component new` emits imports its own canonical lift
    plumbing (`import-func-<export>`, `import-type-<record>`, ...); those are
    not authority-bearing. They ARE still read (and checked by the caller)
    rather than skipped: an unreadable import is not a confined one. Each entry
    is a sort byte, a length-prefixed name, then a sort-specific descriptor; the
    shapes below are the ones this wrapper emits, and an unknown shape is
    refused rather than guessed at.
    """
    names: list[str] = []
    count, pos = _uleb(payload, 0)
    for _ in range(count):
        if pos >= len(payload):
            raise ConfinementError(
                "the produced component's import section is truncated")
        _sort = payload[pos]         # the wrapper's extern-name tag (0x00)
        pos += 1
        name, pos = _name(payload, pos)
        if pos >= len(payload):
            raise ConfinementError(
                "the produced component's import section is truncated")
        # the descriptor's own tag names the import sort: 0x01 func (a type
        # index), 0x03 type (an equality kind then a type index); the other
        # index-shaped sorts are accepted the same way.
        tag = payload[pos]
        pos += 1
        if tag == 0x03:
            if pos >= len(payload):
                raise ConfinementError(
                    f"the produced component import {name!r} has a truncated "
                    f"type descriptor")
            bound = payload[pos]
            pos += 1
            if bound == 0x00:      # eq: a type index follows
                _type_idx, pos = _uleb(payload, pos)
            elif bound == 0x01:    # sub resource: no index
                pass
            else:
                raise ConfinementError(
                    f"the produced component import {name!r} uses an unknown "
                    f"type-bound kind 0x{bound:02x}")
        elif tag in (0x00, 0x01, 0x02, 0x04):
            _type_idx, pos = _uleb(payload, pos)
        else:
            raise ConfinementError(
                f"the produced component import {name!r} uses extern descriptor "
                f"tag 0x{tag:02x}, which this checker does not understand; "
                f"refusing it rather than skipping an unreadable import")
        names.append(name)
    return names


def _core_imports(data: bytes) -> list[WasmImport]:
    if not data.startswith(_CORE_MAGIC):
        raise ConfinementError(
            "expected a wasm core module in the produced bytes")
    imports: list[WasmImport] = []
    pos = 8
    while pos < len(data):
        section_id = data[pos]
        size, start = _uleb(data, pos + 1)
        end = start + size
        if end > len(data):
            raise ConfinementError(
                "the wasm binary is truncated inside a section")
        if section_id == 2:  # import section
            count, cursor = _uleb(data, start)
            for _ in range(count):
                module, cursor = _name(data, cursor)
                field, cursor = _name(data, cursor)
                if cursor >= end:
                    raise ConfinementError(
                        "the wasm binary's import section is truncated")
                kind = data[cursor]
                cursor = _skip_import_desc(data, cursor + 1, kind)
                imports.append(WasmImport(module, field))
        pos = end
    return imports


def component_imports(data: bytes) -> tuple[WasmImport, ...]:
    """Every core-module import in a produced component, read from its bytes.

    A component's embedded core modules are the surface that carries authority
    (``coeffect:`` / ``route:`` channels and the structural seams); nested
    components are walked recursively. A component-level import has no such
    module/field split and is not a shape the emitted component uses, so its
    presence is refused rather than skipped: an unreadable import is not a
    confined one.
    """
    if data.startswith(_CORE_MAGIC):
        return tuple(_core_imports(data))
    if not data.startswith(_COMPONENT_MAGIC):
        raise ConfinementError(
            "the produced bytes are neither a wasm core module nor a component")
    imports: list[WasmImport] = []
    pos = 8
    while pos < len(data):
        section_id = data[pos]
        size, start = _uleb(data, pos + 1)
        end = start + size
        if end > len(data):
            raise ConfinementError(
                "the wasm component is truncated inside a section")
        payload = data[start:end]
        if section_id == 1:  # an embedded core module
            imports.extend(_core_imports(payload))
        elif section_id == 4:  # a nested component
            imports.extend(component_imports(payload))
        elif section_id == 10:  # a component-level import
            for name in _component_level_import_names(payload):
                if not name.startswith("import-"):
                    raise ConfinementError(
                        f"the produced component imports {name!r} at the "
                        f"COMPONENT level, which is not the core-module "
                        f"capability surface this check reads and is not a "
                        f"generated revl component's own lift plumbing; "
                        f"refusing it rather than skipping an unreadable import")
        pos = end
    return tuple(imports)


def _allowed_by(key: str, allow) -> bool:
    if key == "*":
        return "*" in allow
    return any(fnmatchcase(key, pattern) for pattern in allow)


def check_component_imports(data: bytes, *,
                            declared,
                            allowed=None,
                            component: str = "<component>",
                            declared_targets=()) -> tuple[WasmImport, ...]:
    """Refuse a produced component whose import section is not confined.

    ``declared`` is the capability surface the component declared (its G8
    reach). ``allowed`` is the boundary policy's allow-list for it, or ``None``
    when no policy rule constrains it. ``declared_targets`` is the composition's
    component names: a ``spawn:<target>`` or ``instance:<component>`` import
    reaches another component, so a target that is not part of the composition
    is refused. A capability import must name a declared key (a bare ``*``
    declaration covers any key) and, when an allow-list is present, a key inside
    it. An import outside the expected namespaces is refused unconditionally.
    """
    imports = component_imports(data)
    declared = frozenset(declared)
    targets = frozenset(declared_targets)
    for imported in imports:
        module = imported.module
        if module in _EXACT_MODULES or module in _STRUCTURAL_MODULES:
            continue
        if module.startswith(_CAPABILITY_PREFIXES):
            rest = module.split(":", 1)[1]
            key = rest.rsplit("/", 1)[-1]
            if "*" not in declared and key not in declared:
                raise ConfinementError(
                    f"the produced component `{component}` imports host "
                    f"capability {imported.render()}, whose key `{key}` it "
                    f"does not declare; the import section must be a "
                    f"subset of the component's declared capabilities "
                    f"(issue #2729, the byte-level import check)")
            if allowed is not None and not _allowed_by(key, allowed):
                raise ConfinementError(
                    f"the produced component `{component}` imports host "
                    f"capability {imported.render()}, whose key `{key}` the "
                    f"boundary policy does not allow; a confined component "
                    f"reaches no more than the policy grants")
            continue
        if module.startswith(("spawn:", "instance:")):
            target = module.split(":", 1)[1].rsplit("/", 1)[-1]
            if target not in targets:
                raise ConfinementError(
                    f"the produced component `{component}` imports "
                    f"{imported.render()}, which reaches component `{target}` "
                    f"that is not part of this composition; a cross-component "
                    f"import must name a declared component")
            continue
        raise ConfinementError(
            f"the produced component `{component}` imports {imported.render()}, "
            f"which is outside the expected namespaces "
            f"({', '.join(sorted(_EXACT_MODULES))}, "
            f"{', '.join(_PREFIXED_MODULES)}); a generated component may reach "
            f"no host surface other than its declared capability seams")
    return imports


def declared_capabilities(ir: dict, service: str | None = None) -> frozenset[str]:
    """The G8-reached capability tokens the emitted component declared.

    With ``service`` naming a provided service, this is THAT component's reach:
    a capability only another component declares must not make this component's
    import check vacuous. Without a matching provider (the pure-``fn`` shape,
    which has no component), it falls back to the union over the composition.
    """
    from .audit_diff import audit_report  # noqa: PLC0415 - keep import light
    from .policy import component_reach  # noqa: PLC0415

    audit = audit_report(ir)
    names = set((audit.get("boundary") or {}).keys())
    if service is not None:
        for component in ir.get("components") or []:
            if service in (component.get("provides") or {}).values():
                names = {component.get("name")}
                break
    out: set[str] = set()
    for name in names:
        out.update(reach.token for reach in component_reach(audit, name))
    return frozenset(out)


def component_targets(ir: dict) -> frozenset[str]:
    """The component names in the composition, for spawn/instance target checks."""
    return frozenset(
        component.get("name") for component in (ir.get("components") or [])
        if component.get("name"))

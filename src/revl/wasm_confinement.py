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
    an import outside the expected namespaces, or a structural seam import
    naming a field the wasm tier does not define;
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
``import-func-``/``import-type-`` canonical-lift plumbing) are decoded and
checked too: a name that merely starts with ``import-`` is not plumbing.

A capability import is compared by its FULL COMPOSED ADDRESS, not its leaf key.
The emitter renders ``coeffect:<realm>/<key>`` from the component's ``isolate``
placement (``emit.py:_import_module``), so ``coeffect:tenantb/kv`` and
``coeffect:kv`` are different authorities even though both end in ``kv``.
``check_component_imports`` therefore takes the component's ``isolate`` map and
admits only the addresses that map produces; an import in a realm the component
does not declare is refused.

A declared ``*`` is the unnameable boundary, not a wildcard that makes the key
check vacuous: it never proves a NAMED import is inside it, so a concrete
capability address must be backed by a concrete declaration. That mirrors the
emitter, whose ``requires`` is a key->service map and therefore has no ``*``
entry an import could resolve through.
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
# Each binds exactly one field (or, for `config`, only the fields the caller
# declares for the component): an unknown field is not "more of the same seam",
# it is an unread host surface and is refused.
_STRUCTURAL_FIELDS = {
    "host": frozenset({"job_run"}),
    "dispose": frozenset({"instance"}),
    "coeffect:revl:wal": frozenset({"record"}),
}
_EXACT_MODULES = frozenset({"host", "config", "dispose"})

# The durable-WAL framing channel is rendered under the capability prefix but
# is a first-party host binding, not a declared capability: `emit.py` and
# `least_authority._STRUCTURAL_COEFFECTS` both exempt it, so this check must too.
_STRUCTURAL_MODULES = frozenset({"coeffect:revl:wal"})

# Imports whose module name carries a target/key after a namespace prefix.
_PREFIXED_MODULES = ("coeffect:", "route:", "spawn:", "instance:")

# The namespaces that actually carry named authority.
_CAPABILITY_PREFIXES = ("coeffect:", "route:")

# The wrapper's component-level lift plumbing is exactly two name families, one
# per extern sort: `import-func-<name>` for a function and `import-type-<name>`
# for a type (`wasm-tools component new`). The name must match the SORT the
# import section declares: a type import called `import-func-t` is a spoof.
_COMPONENT_LEVEL_PREFIXES = {0x01: "import-func-", 0x03: "import-type-"}
_COMPONENT_LEVEL_SORTS = {0x01: "function", 0x03: "type"}


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


def _component_level_imports(payload: bytes) -> list[tuple[str, int]]:
    """The (name, sort) pairs of a component import section's entries.

    The wrapper `wasm-tools component new` emits imports its own canonical lift
    plumbing (`import-func-<export>`, `import-type-<record>`); those are not
    authority-bearing, but they are named by a small, closed convention and are
    READ rather than skipped. Each entry is an extern-name kind byte (only
    the standard 0x00 is readable here), a length-prefixed name, then a
    sort-specific descriptor; the shapes below are the ones this wrapper emits,
    and an unknown shape is refused rather than guessed at.

    Only the two sort families a wrapper can legitimately present are decoded:
    a function import (``0x01``) and a type import (``0x03``). A bare ``import-``
    prefix is not enough to be plumbing (`import-evil` is an import like any
    other), and the name must agree with the sort it declares, so
    `import-func-t` typed as a TYPE is refused as the spoof it is.
    """
    entries: list[tuple[str, int]] = []
    count, pos = _uleb(payload, 0)
    for _ in range(count):
        if pos >= len(payload):
            raise ConfinementError(
                "the produced component's import section is truncated")
        name_tag = payload[pos]      # 0x00 = a standard extern name
        pos += 1
        if name_tag != 0x00:
            raise ConfinementError(
                f"the produced component's import section uses extern-name "
                f"kind 0x{name_tag:02x}; only the standard 0x00 name is an "
                f"import this checker can read, so refusing it rather than "
                f"skipping an unreadable import")
        name, pos = _name(payload, pos)
        if pos >= len(payload):
            raise ConfinementError(
                "the produced component's import section is truncated")
        # the descriptor's own tag names the import sort: 0x01 func (a type
        # index), 0x03 type (an equality kind then a type index).
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
        elif tag == 0x01:
            _type_idx, pos = _uleb(payload, pos)
        else:
            raise ConfinementError(
                f"the produced component imports {name!r} at the COMPONENT "
                f"level with extern sort 0x{tag:02x}, which is not the "
                f"wrapper's lift plumbing (only a function or a type import "
                f"is); refusing it rather than skipping an unreadable import")
        entries.append((name, tag))
    return entries


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
    module/field split: the wrapper's own ``import-func-``/``import-type-``
    lift plumbing is admitted by name and sort, and anything else there is
    refused rather than skipped. An unreadable import is not a confined one.
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
            for name, sort in _component_level_imports(payload):
                expected = _COMPONENT_LEVEL_PREFIXES[sort]
                if not name.startswith(expected) or len(name) == len(expected):
                    raise ConfinementError(
                        f"the produced component imports {name!r} at the "
                        f"COMPONENT level as a {_COMPONENT_LEVEL_SORTS[sort]}, "
                        f"which is not the wrapper's `{expected}<name>` lift "
                        f"plumbing; a bare `import-` prefix is not plumbing, "
                        f"so refusing it rather than skipping an unreadable "
                        f"import")
        pos = end
    return tuple(imports)


def _allowed_by(key: str, allow) -> bool:
    if key == "*":
        return "*" in allow
    return any(fnmatchcase(key, pattern) for pattern in allow)


def _capability_key(module: str) -> str:
    """The leaf capability token of a ``coeffect:``/``route:`` address.

    The policy allow-list and the G8 reach are written in tokens, not addresses,
    so this is the ONLY place the realm half of an address is discarded — after
    the full-address comparison has already admitted the import. Losing the
    realm earlier is the bug this check exists to close.
    """
    _prefix, _, rest = module.partition(":")
    return rest.rsplit("/", 1)[-1]


def _declared_addresses(declared, isolate) -> frozenset[str]:
    """The composed capability addresses a component's declaration covers.

    The emitter composes ``coeffect:<realm>/<key>`` from the component's
    ``isolate`` placement and ``route:<key>`` for a routed require
    (``emit.py:_import_module``), so the expected set is derived the same way.
    A key with no placement is the shared realm: ``coeffect:<key>``.

    A ``*`` declaration is the unnameable boundary and contributes NO address:
    it cannot prove that a NAMED import is inside it, so it never makes the
    check vacuous. This matches the emitter, where ``requires`` is a key->service
    map and an import can only be created from a resolved key.
    """
    addresses: set[str] = set()
    placement = dict(isolate or {})
    for key in declared:
        if key == "*":
            continue
        realm = placement.get(key)
        addresses.add(f"coeffect:{realm}/{key}" if realm else f"coeffect:{key}")
        addresses.add(f"route:{key}")
    return frozenset(addresses)


def _structural_field(module: str, field: str, config_fields) -> bool:
    if module == "config":
        # The template's own `config { ... }` fields are the only ones the
        # runtime binds; a caller that does not know them (an ordinary
        # component has none) passes nothing, which refuses every config import
        # rather than trusting an unread field name.
        return field in config_fields
    return field in _STRUCTURAL_FIELDS[module]


def check_component_imports(data: bytes, *,
                            declared,
                            allowed=None,
                            config_fields=(),
                            isolate=None,
                            component: str = "<component>",
                            declared_targets=()) -> tuple[WasmImport, ...]:
    """Refuse a produced component whose import section is not confined.

    ``declared`` is the capability surface the component declared (its G8
    reach); ``isolate`` is that component's ``key -> realm`` placement, so a
    capability import is matched by its FULL composed address
    (``coeffect:<realm>/<key>`` / ``route:<key>``) rather than its leaf key.
    ``allowed`` is the boundary policy's allow-list for it, or ``None`` when no
    policy rule constrains it; it is matched against the leaf token, because
    that is the vocabulary policy rules are written in. ``config_fields`` is the
    set of config fields the component may read (empty for every component that
    is not a spawn template). ``declared_targets`` is the composition's
    component names: a ``spawn:<target>`` or ``instance:<component>`` import
    reaches another component, so a target that is not part of the composition
    is refused, and so is an unknown field on the seam.

    An import outside the expected namespaces is refused unconditionally.
    """
    imports = component_imports(data)
    declared = frozenset(declared)
    targets = frozenset(declared_targets)
    config_fields = frozenset(config_fields)
    addresses = _declared_addresses(declared, isolate)
    for imported in imports:
        module = imported.module
        if module in _EXACT_MODULES or module in _STRUCTURAL_MODULES:
            if not _structural_field(module, imported.field, config_fields):
                raise ConfinementError(
                    f"the produced component `{component}` imports "
                    f"{imported.render()} on the structural `{module}` channel, "
                    f"whose field the wasm tier does not define; that channel "
                    f"carries only its own ABI field(s), so an unknown field is "
                    f"an unread host surface (issue #2729, the byte-level "
                    f"import check)")
            continue
        if module.startswith(_CAPABILITY_PREFIXES):
            if module not in addresses:
                declared_note = (
                    "none" if not addresses else
                    ", ".join(sorted(addresses)))
                raise ConfinementError(
                    f"the produced component `{component}` imports host "
                    f"capability {imported.render()} at the composed address "
                    f"`{module}`, which this component does not declare; a "
                    f"capability is the full address, not its leaf key, so the "
                    f"realm is part of the authority. Declared addresses: "
                    f"{declared_note} (issue #2729, the byte-level import "
                    f"check)")
            key = _capability_key(module)
            if allowed is not None and not _allowed_by(key, allowed):
                raise ConfinementError(
                    f"the produced component `{component}` imports host "
                    f"capability {imported.render()}, whose key `{key}` the "
                    f"boundary policy does not allow; a confined component "
                    f"reaches no more than the policy grants")
            continue
        if module.startswith("spawn:"):
            target = module[len("spawn:"):]
            if ("/" in target or ":" in target or not target
                    or target not in targets):
                raise ConfinementError(
                    f"the produced component `{component}` imports "
                    f"{imported.render()}, which reaches component `{target}` "
                    f"that is not part of this composition; a cross-component "
                    f"import must name a declared component, not a sibling path")
            if imported.field != "new":
                raise ConfinementError(
                    f"the produced component `{component}` imports "
                    f"{imported.render()}, whose field is not the `new` spawn "
                    f"ABI; a spawn import carries only `new`")
            continue
        if module.startswith("instance:"):
            target = module[len("instance:"):]
            if ("/" in target or ":" in target or not target
                    or target not in targets):
                raise ConfinementError(
                    f"the produced component `{component}` imports "
                    f"{imported.render()}, which reaches component `{target}` "
                    f"that is not part of this composition; a cross-component "
                    f"import must name a declared component, not a sibling path")
            key, dot, op = imported.field.partition(".")
            if not dot or not key or not op or "." in op:
                raise ConfinementError(
                    f"the produced component `{component}` imports "
                    f"{imported.render()}, whose field is not the "
                    f"`<key>.<method>` instance-accessor ABI; only a provision "
                    f"read through a spawn handle is a defined instance "
                    f"field")
            continue
        raise ConfinementError(
            f"the produced component `{component}` imports {imported.render()}, "
            f"which is outside the expected namespaces "
            f"({', '.join(sorted(_EXACT_MODULES))}, "
            f"{', '.join(_PREFIXED_MODULES)}); a generated component may reach "
            f"no host surface other than its declared capability seams")
    return imports


def _component_entry(ir: dict, service: str | None) -> dict | None:
    """The composition entry that provides ``service``, if exactly one does.

    The canonical emitter lowers one provider component; ``service`` names the
    provided service, so its component is the subject of the import check. With
    no match (the pure-``fn`` shape, which has no component) there is no
    per-component placement to read and the caller falls back to the union.
    """
    if service is None:
        return None
    for component in ir.get("components") or []:
        if service in (component.get("provides") or {}).values():
            return component
    return None


def component_name(ir: dict, service: str | None = None) -> str | None:
    """The name of the component ``service`` provides, or ``None``."""
    component = _component_entry(ir, service)
    return component.get("name") if component is not None else None


def component_isolate(ir: dict, service: str | None = None) -> dict[str, str]:
    """The ``key -> realm`` placement of the component ``service`` provides.

    Without a matching provider, the union over the composition is returned: a
    composition that places a key in two realms still contributes both composed
    addresses, and a caller that knows only one component should pass its
    ``service`` rather than rely on this fallback.
    """
    component = _component_entry(ir, service)
    if component is not None:
        return dict(component.get("isolate") or {})
    out: dict[str, str] = {}
    for component in ir.get("components") or []:
        out.update(component.get("isolate") or {})
    return out


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
    component = _component_entry(ir, service)
    if component is not None:
        names = {component.get("name")}
    out: set[str] = set()
    for name in names:
        out.update(reach.token for reach in component_reach(audit, name))
    return frozenset(out)


def component_targets(ir: dict) -> frozenset[str]:
    """The component names in the composition, for spawn/instance target checks."""
    return frozenset(
        component.get("name") for component in (ir.get("components") or [])
        if component.get("name"))

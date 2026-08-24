"""revl backend-IR -> WAT emitter for the cordis-wasm substrate tier.

Target: the cordis-wasm runtime (~/Projects/cordis-wasm), where the paradigm
is enforced by the sandbox: a component's coeffect specification IS its Wasm
import section, its provision IS its `provide:<key>.<op>` exports, and
confinement is the instruction set (its DESIGN.md maps the calculus to the
substrate 1:1).

Lowering — the paper's §6.7 state machine, literally:

- each body step is one iteration of the exported `activate_step() -> i32`
  (1 = more, 0 = done); a mutable global `$__step` records progress;
- every `effect`/`emit-with-compensate` step's undo expression compiles into
  the exported `deactivate()`, guarded by `$__step >= <n>` and ordered
  newest-first — so partial rollback after a divert or trap reverts exactly
  the completed steps' inverses (paper §4.3.2), with no host bookkeeping;
- `provide` steps contribute `provide:<key>.<op>` exports; the runtime
  stages them at instantiation and publishes at L-Finish (its own R5);
- `req` calls compile to `coeffect:<key>` imports — the committed view is
  the linker binding itself, alive through this component's whole teardown.

Widths: `Int` is **i64** — 64-bit two's complement with trapping overflow, as
docs/arithmetic.md specifies for every tier. Linear-memory *addresses* stay
i32 (wasm32 addressing is 32-bit), as do Bool and the internal counters, so
this emitter deliberately uses both widths and `_wasm_ty` is the single place
that decides which. `Float` remains refused by name.

Tier restrictions (cordis-wasm status: core Wasm, sync base calculus).
Violations are EmitError, never silent degradation. The service boundary
(coeffect imports, `provide:<key>.<op>` exports) carries the SAME canonical-ABI
representation the v3 functions tier uses: an `Int` is an i64 value, a `Bool`
an i32 value, and every Str/List/record/variant/Opt/Result an i32 *pointer*
into the module's linear memory (`_boundary_wty`). A value whose wasm width
disagrees with its declared service type (a `List` from an `Int`-declared
op) stays refused — a real mismatch, not a silent narrowing. `@wasm`-bodied
externs lower to internal `(func $name …)`. Config blocks (no instantiation
channel), host builtins outside `await Job.run` (e.g. `Map.new` — the tier has
no Map representation), method-time effects, and `Float`/`Map` at the boundary
are still rejected with a precise reason.

`emit(ir) -> dict[name, wat]` — one WAT module per component, plus a
`functions` module for IR v3 type/function documents.
"""

from __future__ import annotations

import json
import re
from typing import Any

IR_VERSION = 1

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


#: Every non-byte value slot in linear memory — a record field, a list
#: element, a tagged cell's payload — is this many bytes wide. It used to be 4;
#: `Int` is 64-bit (docs/arithmetic.md), so a slot has to be able to hold an
#: i64. The width is *uniform* rather than per-field: list elements are
#: addressed as `base + 8 * index` by helpers that never see the element type,
#: and a record's field offset has to be computable without walking the
#: preceding fields' types. Pointers and Bools occupy a full slot too — they
#: are zero-extended in (`_slot_store`) and wrapped back out (`_slot_load`), so
#: a slot always holds the same 8 bytes whichever way it is read.
_SLOT = 8

# The total, value-returning division forms (docs/arithmetic.md): same
# rounding as the faulting operations, Err(reason) at a zero divisor.
_CHECKED_DIVS = ("checked_div_trunc", "checked_div_floor",
                 "checked_div_euclid", "checked_mod")
_DIV_ZERO_MSG = "revl: division by zero"


def _align4(value: int) -> int:
    return (value + 3) & ~3


def _align8(value: int) -> int:
    return (value + 7) & ~7


def _wasm_ty(ty: str | None) -> str:
    """The wasm value type that carries a revl type on this tier.

    `Int` is the only revl type here that is 64-bit. Everything else is a Bool
    (0/1) or a *linear-memory address*, and wasm32 addressing is 32-bit, so
    those stay i32. This one function is what stops "value" and "pointer" from
    being confused now that they are no longer the same wasm type.
    """
    return "i64" if ty == "Int" else "i32"


def _wat_string(value: str) -> str:
    """Escape a Python string for use as a WAT string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class EmitError(ValueError):
    """The IR document cannot be lowered to the wasm tier."""


# Dispatcher conformance (roadmap item 76a). This file carries THREE
# expression dispatchers — `_ComponentEmitter._lower` (component/method
# bodies; i32-native kinds plus everything the v3 value engine already models,
# delegated through `_DELEGATED`), `_V3Emitter._expr` (pure fn bodies) and
# `_V3Emitter._infer_type` (the fn-body type oracle) — and the sets below
# declare, as data, the IR expression kinds each one must render, plus the
# kinds each one deliberately refuses with a named tier-limit EmitError (never
# the "unknown/unsupported expression kind" fall-through).
# tests/test_expr_dispatcher_conformance.py checks them against the frontend
# schema (src/revl/lower.py: EXPR_KINDS / EXPR_KINDS_FN / EXPR_KINDS_COMPONENT).
#
# The tier's deliberate absences are listed here explicitly, each with a named
# refusal in the emitter: host builtins (`Map`/`Pool`), the Map VALUE type
# (`maplit` — no representation on this tier), functional record update
# (docs/records.md §6), arrow values (the module has no closures), and
# optional chaining (`?.` — unwrap with `match`/`??`). `req`/`config`/`host`/
# `instance-get` are component-only and position-conditional on this tier (a
# `req` is usable only as a call target, `config` only inside a spawn target
# template, `instance-get` only in call position); the component dispatcher
# handles those call/template forms and the value forms raise the named
# position refusal. `hole` is refused at the document level by the pre-emit
# walk.
EXPR_DISPATCHERS: dict[str, frozenset[str]] = {
    "component": frozenset({
        "adt", "bin", "builtin", "call", "config", "field", "fn",
        "format", "if", "index", "instance-get", "interp", "len", "list",
        "lit", "match", "name", "record", "req", "spawn", "un", "var",
    }),
    "fn": frozenset({
        "adt", "bin", "builtin", "call", "field", "if", "index", "interp",
        "len", "list", "lit", "match", "record", "un", "var",
    }),
    "fn-infer": frozenset({
        "adt", "bin", "builtin", "call", "field", "if", "index", "interp",
        "len", "list", "lit", "match", "record", "un", "var",
    }),
}
EXPR_REFUSED: dict[str, frozenset[str]] = {
    "component": frozenset({
        "arrow",         # no closures on this tier
        "host",          # host builtins: no host surface on this tier
        "maplit",        # the Map value type has no representation here
        "optcall",       # `?.` — unwrap with `match`/`??`
        "optfield",      # `?.` — unwrap with `match`/`??`
        "record_update", # docs/records.md §6 — lift into a helper fn
    }),
    "fn": frozenset({
        "arrow", "maplit", "optcall", "optfield", "record_update",
    }),
    "fn-infer": frozenset({
        "arrow", "maplit", "optcall", "optfield", "record_update",
    }),
}
# kinds refused at the document level on every position
EXPR_REFUSED_DOCUMENT: frozenset[str] = frozenset({"hole"})


def _is_fn_type(name: object) -> bool:
    """Is this surface type a function type, `(P, ...) -> R`?

    A function type is the one surface spelling that is not `Head[Args]`
    (docs/function-types.md), so it can be recognised without a full parse.
    """
    if not isinstance(name, str) or not name.strip().startswith("("):
        return False
    name = name.strip()
    depth = 0
    for i, ch in enumerate(name):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
            if depth == 0:
                return name[i + 1:].lstrip().startswith("->")
    return False



def _ident(name: Any, what: str) -> str:
    if not isinstance(name, str) or not IDENT_RE.match(name):
        raise EmitError(f"{what} {name!r} is not a usable identifier")
    return name


class _ComponentEmitter:
    """A component document -> one WAT module.

    Component/method bodies are i32 at the service boundary, but *inside* the
    module a method may use the same values a v3 `fn` can.  Rather than carry a
    second, poorer expression renderer, this class keeps a `_V3Emitter` as a
    value engine (`self.v3`) and delegates every kind that is not i32-native to
    it, splicing component-only nodes (`req` calls, `config`, `host`) back in
    as pre-lowered WAT.  Anything a delegated body needs — the linear-memory
    helpers, string data, and the top-level `fn`s it calls — is emitted into
    this module, so the component stays self-contained.
    """

    def __init__(self, component: dict, services: dict, ir_version: int = IR_VERSION,
                 types: dict | None = None, functions: list | None = None,
                 externs: list | None = None, is_template: bool = False,
                 spawn_targets: dict | None = None) -> None:
        self.ir = component
        self.services = services
        self.ir_version = ir_version
        self.name = _ident(component.get("name"), "component name")
        # A spawn target is a *template* (docs/design-v2-instances.md): a runtime
        # instance, never a static composition member. It alone may carry a
        # `config { }` block — the fields cross the instantiation-config channel
        # (the runtime binds each to a `config:<field>` import at spawn time),
        # which is exactly the gap "no config channel yet" used to reject.
        self.is_template = is_template
        # field -> declared type, for a template's own `config.<field>` reads.
        self.config_fields: dict[str, str] = {}
        for field in component.get("config") or []:
            self.config_fields[_ident(field.get("name"), f"{self.name}: config field")] = \
                field.get("type")
        if component.get("config") and not is_template:
            # A statically composed component with admission-time config still
            # has no channel on this tier — only a spawn target's config flows.
            raise EmitError(
                f"{self.name}: config blocks are not lowerable — the "
                f"cordis-wasm runtime has no instantiation-config channel yet "
                f"(a spawn *target* is the exception: its config crosses the "
                f"spawn boundary)"
            )
        # every component's declared config shape (field, type), in order — so a
        # `spawn <Target>` can pass its config positionally, in the target's
        # declared field order, matching the runtime's `register_template` order.
        self.spawn_targets: dict[str, list[tuple[str, str]]] = spawn_targets or {}
        # instance-parametric imports minted while lowering (rendered in _module)
        self.config_imports: dict[str, str] = {}   # field -> result wasm type
        self.spawn_imports: dict[str, list[str]] = {}  # template -> param wtys
        # (component, key, op) -> (param wasm types, result wasm type or None):
        # the instance-accessor ABI — reading a provision back through a spawn
        # handle. Each resolves `key` in THAT instance's realm (B3), the private
        # local realm the matching `spawn` isolated the key into.
        self.instance_imports: dict[tuple[str, str, str], tuple[list[str | None], str | None]] = {}
        self.uses_dispose = False
        self.requires = component.get("requires") or {}
        self.provides = component.get("provides") or {}
        self.isolate = component.get("isolate") or {}
        self.intercept = component.get("intercept") or {}
        for key in self.isolate:
            if key not in self.requires and key not in self.provides:
                raise EmitError(f"{self.name}: isolate key {key!r} is not declared")
            realm = self.isolate[key]
            if not isinstance(realm, str) or not realm:
                raise EmitError(f"{self.name}: isolate key {key!r} has a non-static realm {realm!r}")
        for key in self.intercept:
            if key not in self.requires:
                raise EmitError(f"{self.name}: intercept key {key!r} is not a requirement")
            try:
                json.dumps(self.intercept[key], sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise EmitError(f"{self.name}: intercept metadata for {key!r} is not JSON: {exc}")
        # (key, op) -> (param wasm types, result wasm type or None) — the
        # coeffect import's ABI, one width per declared param/return.
        self.imports: dict[tuple[str, str], tuple[list[str | None], str | None]] = {}
        self.globals: list[tuple[str, str]] = []   # (name, wasm type)
        self.uses_job = False
        # job name -> interned i32 id (see _job_id)
        self.job_names: dict[str, int] = {}
        # the value engine: the *same* renderer the v3 `fn` tier uses
        self.v3 = _V3Emitter(types or {}, functions or [], externs or [], [])
        self.externs = {ext.get("name"): ext for ext in (externs or [])}
        self.fn_by_name = {fn.get("name"): fn for fn in (functions or [])}
        self.needed_fns: list[str] = []   # top-level fns this component calls
        self.needed_externs: list[str] = []  # @wasm externs this component calls
        self.uses_v3 = False              # a delegated expression was lowered
        self.extra_locals: set[str] = set()  # v3 scratch locals for one method
        self.func_uses_v3 = False
        self.activation_locals: list[str] = []
        # a rich type (Str/List/record/variant/Opt/Result) crossed the service
        # boundary as a linear-memory pointer, so the module has to export a
        # memory even when its own body allocates nothing (an identity
        # `fn f(x: Str) -> Str = x` just forwards a pointer the host owns).
        self.boundary_uses_memory = False

    # -- v2 realms -----------------------------------------------------------

    def _scoped_key(self, key: str) -> str:
        realm = self.isolate.get(key)
        return f"{realm}/{key}" if realm else key

    def _import_module(self, key: str) -> str:
        return f"coeffect:{self._scoped_key(key)}"

    def _provide_prefix(self, key: str) -> str:
        return f"provide:{self._scoped_key(key)}"

    def _realm_sections(self) -> list[str]:
        """Document realm placement and intercept metadata.

        The substrate has no realm registry, but its coeffect table is keyed
        by the import/export namespace itself, so ``tenant_a/kv`` and
        ``tenant_b/kv`` are distinct providers to the runtime.  Intercept
        metadata is advisory: a host can read it from the custom section and
        enforce quotas/ACLs without touching provider or consumer.
        """
        lines: list[str] = []
        if self.isolate or self.intercept:
            lines.append("  ;; realms are advisory on this tier: isolate is the")
            lines.append("  ;; import/export namespace, intercept metadata is the")
            lines.append("  ;; revl:intercept custom section (host-enforced, if present).")
        if self.isolate:
            payload = _wat_string(json.dumps(self.isolate, sort_keys=True))
            lines.append(f'  (@custom "revl:isolate" "{payload}")')
        if self.intercept:
            payload = _wat_string(json.dumps(self.intercept, sort_keys=True))
            lines.append(f'  (@custom "revl:intercept" "{payload}")')
        return lines

    # -- service boundary widths ---------------------------------------------

    def _boundary_wty(self, ty: str | None, where: str) -> str | None:
        """The wasm ABI type a service param/return of declared type `ty` uses.

        This is the whole port: the v3 functions tier already lowers
        Str/List/record/variant/Opt/Result through a canonical-ABI linear-memory
        representation, so the service boundary carries the SAME representation —
        `Int` is an i64 value, `Bool` an i32 value, and every compound type an
        i32 *pointer* into the module's memory. Anything the v3 value model has
        no representation for (`Float`, `Map`, function types) is refused here by
        `_check_type` — a genuine boundary, not a silent narrowing. `None`/`Unit`
        means the slot is absent (a void operation, no param/result).
        """
        if _is_unit_type(ty):
            return None
        # `_check_type` is the v3 tier's single lowerability gate: it accepts
        # Int/Bool/Str/Bytes/List/record/variant/Opt/Result and refuses Float,
        # Map, and function types with a named reason.
        self.v3._check_type(ty, where)
        if not _is_scalar_type(ty):
            self.boundary_uses_memory = True
        return _wasm_ty(ty)

    # -- service lookup ------------------------------------------------------

    def _op_spec(self, key: str, op: str, where: str) -> tuple[list[str | None], str | None]:
        """Resolve a coeffect op, registering its import ABI.

        Returns the declared (param types, return type) — the revl types, so a
        call site can width-check each argument and carry the result's type. The
        wasm widths of those types are stored in ``self.imports`` for the import
        section to render.
        """
        service_name = self.requires.get(key)
        service = self.services.get(service_name)
        if service is None:
            raise EmitError(f"{where}: req {key!r} is not declared in requires")
        spec = (service.get("methods") or {}).get(op)
        if spec is None:
            raise EmitError(f"{where}: {key}.{op} is not a method of {service_name}")
        param_types = [param.get("type") for param in spec.get("params") or []]
        return_type = spec.get("returns")
        param_wtys = [
            self._boundary_wty(pty, f"{where}: {key}.{op} param {i}")
            for i, pty in enumerate(param_types)
        ]
        result_wty = self._boundary_wty(return_type, f"{where}: {key}.{op} return")
        self.imports[(key, op)] = (param_wtys, result_wty)
        return param_types, return_type

    # -- expressions ---------------------------------------------------------

    #: kinds the component path lowers itself, straight to i32 instructions.
    #: everything else that is a real expression goes to the v3 value engine.
    _DELEGATED = frozenset({
        "if", "fn", "list", "record", "field", "index", "builtin", "len",
        "adt", "match", "interp", "format", "arrow", "var",
    })

    def _expr(self, node: Any, scope: dict[str, str], where: str,
              types: dict[str, str | None] | None = None) -> tuple[str, bool]:
        """Returns (wat, has_result) — the i32 view used by activation steps."""
        value = self._lower(node, scope, types if types is not None else {}, where)
        return value.wat, not _is_unit_type(value.ty)

    def _lower(self, node: Any, scope: dict[str, str],
               types: dict[str, str | None], where: str) -> _E:
        """Lower one expression, carrying the value's revl type.

        i32-native kinds are emitted here; anything the v3 `fn` renderer
        already models is delegated to it (`_delegate`) so the two paths agree
        by construction instead of by duplication.
        """
        if not isinstance(node, dict) or "kind" not in node:
            raise EmitError(f"{where}: malformed expression {node!r}")
        kind = node["kind"]
        if kind == "lit":
            value = node.get("value")
            if isinstance(value, int) and not isinstance(value, bool):
                return _E(f"(i64.const {value})", "Int")
            # Bool/Str literals are in-module values: the engine owns them
            return self._delegate(node, scope, types, where)
        if kind == "name":
            name = _ident(node.get("id"), f"{where}: name")
            slot = scope.get(name)
            if slot is None:
                raise EmitError(f"{where}: unbound name {name!r}")
            return _E(slot, types.get(name, "Int"))
        if kind == "req":
            raise EmitError(f"{where}: a required service is only usable as a call target")
        if kind == "spawn":
            return self._lower_spawn(node, scope, types, where)
        if kind == "instance-get":
            # `s.<key>` in a value position (not `s.<key>.method(..)`) would be a
            # bare *service* value; a service is not a scalar this tier can carry
            # in a local — only the call form `s.<key>.method(..)` lowers, which
            # is intercepted at the enclosing `call` node.
            raise EmitError(
                f"{where}: a spawn handle's provision `{node.get('key')}` is a "
                f"service, not a value on this tier — call a method on it "
                f"(`s.{node.get('key')}.method(..)`), don't bind it")
        if kind == "call":
            if node.get("callee") is not None:
                # the frontend spells `Some(x)` (and other builtin
                # constructors) in the v3 dialect even inside a component
                # body — callee-shaped, not req-shaped
                return self._delegate(node, scope, types, where)
            target = node.get("target") or {}
            # `<handle>.dispose()` — the one method a spawn handle carries: the
            # acquisition's inverse (and the request-scoped reclaim when it
            # appears in a provide-method body). It lowers to the runtime's
            # `dispose` host op, taking the i32 handle the spawn returned.
            if node.get("method") == "dispose" and target.get("kind") == "name":
                hname = _ident(target.get("id"), f"{where}: dispose target")
                slot = scope.get(hname)
                if slot is None:
                    raise EmitError(f"{where}: unbound spawn handle {hname!r}")
                if node.get("args"):
                    raise EmitError(f"{where}: dispose() takes no arguments")
                self.uses_dispose = True
                # i32 result (a disposal status the runtime returns); the handle
                # itself is an i32 instance id.
                return _E(f"(call $dispose_instance {slot})", "Bool")
            if target.get("kind") == "instance-get":
                # `s.<key>.method(..)` — reading a provision back through a spawn
                # handle (docs/design-v2-instances.md "Instance accessor").
                return self._lower_instance_get_call(node, target, scope, types, where)
            if target.get("kind") != "req":
                raise EmitError(
                    f"{where}: scalar values have no methods — only calls on "
                    f"required services are lowerable on this tier"
                )
            key = _ident(target.get("name"), f"{where}: req")
            op = _ident(node.get("method"), f"{where}: method")
            param_types, return_type = self._op_spec(key, op, where)
            args = node.get("args") or []
            if len(args) != len(param_types):
                raise EmitError(f"{where}: {key}.{op} takes {len(param_types)} argument(s)")
            parts = []
            for arg, ptype in zip(args, param_types):
                value = self._lower(arg, scope, types, where)
                if _is_unit_type(value.ty):
                    raise EmitError(f"{where}: void expression used as an argument")
                # ABI-width agreement: a rich type crosses as a pointer when the
                # declared param is that rich type, but a value whose wasm width
                # disagrees with the declared param (a List where the op is
                # declared over Int) is a real mismatch and stays refused.
                if _wasm_ty(value.ty) != _wasm_ty(ptype):
                    raise EmitError(
                        f"{where}: a {value.ty!r} argument cannot cross this tier's "
                        f"scalar coeffect boundary — {key}.{op} is declared over "
                        f"{ptype!r}; keep compound values inside the module"
                    )
                parts.append(value.wat)
            call = f"(call $req_{key}_{op} {' '.join(parts)})" if parts else f"(call $req_{key}_{op})"
            return _E(call, return_type)
        if kind == "config":
            # A template reads its own `config { }` fields through the
            # instantiation-config channel: each field is an import the runtime
            # binds to the value the spawner passed. Only a spawn *target* has
            # config here, so a config read outside one is still refused.
            field = node.get("field")
            if not self.is_template or field not in self.config_fields:
                raise EmitError(f"{where}: config is not available on this tier")
            fty = self.config_fields[field]
            # The config channel passes a *value* (the runtime returns it from
            # the `config:<field>` import). A rich type would cross as a pointer
            # into memory the instance does not own — a genuine tier boundary,
            # the same one the scalar coeffect boundary draws.
            if not _is_scalar_type(fty):
                raise EmitError(
                    f"{where}: config field {field!r} is {fty!r} — only scalar "
                    f"config (Int/Bool) crosses the spawn boundary on this tier; "
                    f"a Str/record/List config value would cross as a pointer into "
                    f"memory the instance does not own (use a hosted backend)")
            self.config_imports[field] = _wasm_ty(fty)
            return _E(f"(call $config_{field})", fty)
        if kind == "host":
            raise EmitError(
                f"{where}: host builtin {node.get('fn')!r} is not available on "
                f"the cordis-wasm tier — express state through coeffects instead"
            )
        if kind in ("bin", "un"):
            # pure scalar arithmetic/comparison inside a component or method
            # body — this tier's native shape, and what a method that names an
            # intermediate is usually doing (tests/test_cross_tier.py).
            # Operators this path has no instruction for (`??`, Str `+`,
            # Str `==`) are the engine's: it knows the operand types.
            return self._scalar_operator(node, scope, types, where)
        if kind in self._DELEGATED:
            return self._delegate(node, scope, types, where)
        if kind == "record_update":
            raise EmitError(
                f"{where}: functional record update `{{r | f = e}}` is not "
                "emitted by the wasm backend yet (implemented tiers: python, "
                "typescript) — see docs/records.md §6; lift it into a helper fn instead")
        if kind == "maplit":
            raise EmitError(
                f"{where}: the Map value type is not lowerable on this tier yet — "
                "no representation here; use a hosted backend")
        if kind in ("optfield", "optcall"):
            raise EmitError(
                f"{where}: optional chaining (`?.`) is not yet lowerable on the "
                f"wasm tier ({kind!r}) — unwrap with `match` or `??` for now")
        raise EmitError(f"{where}: unknown expression kind {kind!r}")

    def _lower_spawn(self, node: Any, scope: dict[str, str],
                     types: dict[str, str | None], where: str) -> "_E":
        """Lower a `spawn <Target> with { .. }` acquisition (the frozen IR of
        docs/design-v2-instances.md) to the runtime's `spawn` host op.

        The target is a template — its module is registered with the runtime,
        never statically composed. Spawning plugs a fresh instance of it into a
        FRESH LOCAL realm (each provided key isolated per-instance, disjoint by
        construction) as its own nested teardown scope, and returns the i32
        handle the spawner names in `undo` / reaches its instance through. The
        config crosses positionally, in the target's declared field order, which
        is the order the runtime's `register_template` records — so the
        `config:<field>` imports on the instance bind to the right values.
        """
        target = node.get("component")
        if not isinstance(target, str) or not target.isidentifier():
            raise EmitError(f"{where}: bad spawn target {target!r}")
        if target not in self.spawn_targets:
            raise EmitError(f"{where}: spawn target {target!r} is not a component")
        fields = self.spawn_targets[target]           # [(name, type), ...] in order
        supplied = node.get("config") or {}
        unknown = set(supplied) - {f for f, _ in fields}
        if unknown:
            raise EmitError(f"{where}: spawn {target} has no config field {sorted(unknown)[0]!r}")
        args_wat: list[str] = []
        param_wtys: list[str] = []
        for fname, ftype in fields:
            if fname not in supplied:
                raise EmitError(f"{where}: spawn {target} is missing config field {fname!r}")
            # config crosses by value; a rich (pointer) type is a genuine tier
            # boundary, refused symmetrically with the template's own config read.
            if not _is_scalar_type(ftype):
                raise EmitError(
                    f"{where}: spawn {target}.{fname} is {ftype!r} — only scalar "
                    f"config (Int/Bool) crosses the spawn boundary on this tier")
            wty = _wasm_ty(ftype)
            value = self._lower(supplied[fname], scope, types, where)
            if _wasm_ty(value.ty) != wty:
                raise EmitError(
                    f"{where}: spawn {target}.{fname} expects {ftype!r} but got a "
                    f"{value.ty!r} value — config crosses this tier's scalar boundary")
            args_wat.append(value.wat)
            param_wtys.append(wty)
        self.spawn_imports[target] = param_wtys
        call = f"(call $spawn_{target} {' '.join(args_wat)})".replace("  ", " ")
        # the handle is a host-frontier instance id (i32); `Instance[T]` is
        # advisory and never Int, so _wasm_ty carries it as i32.
        return _E(call, f"Instance[{target}]")

    def _lower_instance_get_call(self, node: Any, get: dict, scope: dict[str, str],
                                 types: dict[str, str | None], where: str) -> "_E":
        """Lower `s.<key>.method(..)` — a provision read through a spawn handle
        (the frozen `instance-get` IR node, docs/design-v2-instances.md).

        `get` is the `instance-get` node: it carries the handle expr (`target`),
        the target `component`, the provided `key`, and the frozen `service`
        type the key yields. On this tier the handle is an i32 identifying the
        instance's B3 realm; the read lowers to a host import that resolves
        `key` in THAT instance's realm — the same realm-prefixed provider table
        `spawn` published into (`#<n>/<key>`), yielding that instance's method
        and no other's. Root/sibling hold no handle and share no realm, so they
        cannot name it — supervision-tree addressing (decision 1/2).
        """
        component = get.get("component")
        if not isinstance(component, str) or not component.isidentifier():
            raise EmitError(f"{where}: bad instance-get component {component!r}")
        key = get.get("key")
        if not isinstance(key, str) or not key.isidentifier():
            raise EmitError(f"{where}: bad instance-get key {key!r}")
        service_name = get.get("service")
        op = _ident(node.get("method"), f"{where}: method")
        # The service type is frozen inline on the node (the typing rule's
        # result), so this tier never re-derives it — it only reads the method's
        # declared ABI off that service.
        service = self.services.get(service_name)
        if service is None:
            raise EmitError(
                f"{where}: instance-get names service {service_name!r}, which is "
                f"not declared")
        spec = (service.get("methods") or {}).get(op)
        if spec is None:
            raise EmitError(
                f"{where}: {key}.{op} is not a method of {service_name} "
                f"(the provision {component}.{key} yields)")
        param_types = [param.get("type") for param in spec.get("params") or []]
        return_type = spec.get("returns")
        args = node.get("args") or []
        if len(args) != len(param_types):
            raise EmitError(
                f"{where}: {key}.{op} takes {len(param_types)} argument(s), "
                f"{len(args)} given")
        # The handle is an i32 instance id; lowering it yields that i32 slot.
        handle = self._lower(get.get("target"), scope, types, where)
        param_wtys: list[str | None] = []
        parts: list[str] = [handle.wat]
        for i, (arg, ptype) in enumerate(zip(args, param_types)):
            # The provision lives in a *different* instance's linear memory, so a
            # rich (pointer) param would cross as a pointer into memory the
            # spawner does not own — the same genuine boundary spawn/config draw.
            # Scalars (Int/Bool) cross by value and resolve cleanly.
            if not _is_scalar_type(ptype):
                raise EmitError(
                    f"{where}: {key}.{op} param {i} is {ptype!r} — only scalar "
                    f"(Int/Bool) values cross the instance-accessor boundary on "
                    f"this tier; a Str/record/List would cross as a pointer into "
                    f"the instance's own memory (use a hosted backend)")
            value = self._lower(arg, scope, types, where)
            wty = _wasm_ty(ptype)
            if _wasm_ty(value.ty) != wty:
                raise EmitError(
                    f"{where}: {key}.{op} param {i} expects {ptype!r} but got a "
                    f"{value.ty!r} value — it crosses this tier's scalar boundary")
            param_wtys.append(wty)
            parts.append(value.wat)
        if not _is_scalar_type(return_type) and not _is_unit_type(return_type):
            raise EmitError(
                f"{where}: {key}.{op} returns {return_type!r} — only scalar "
                f"(Int/Bool) provisions cross the instance-accessor boundary on "
                f"this tier; a rich return is a pointer into the instance's own "
                f"memory (use a hosted backend)")
        result_wty = _wasm_ty(return_type) if not _is_unit_type(return_type) else None
        self.instance_imports[(component, key, op)] = (param_wtys, result_wty)
        # $inst_<Component>_<key>_<op> — bound to the `instance:<Component>`
        # `<key>.<op>` host import; the runtime resolves the handle's realm.
        call = f"(call $inst_{component}_{key}_{op} {' '.join(parts)})".replace("  ", " ")
        return _E(call, return_type)

    def _scalar_operator(self, node: Any, scope: dict[str, str],
                         types: dict[str, str | None], where: str) -> _E:
        if node.get("kind") == "un":
            op = node.get("op")
            operand = self._lower(node.get("operand"), scope, types, where)
            if _is_unit_type(operand.ty):
                raise EmitError(f"{where}: unary `{op}` on a void expression")
            if not _is_scalar_type(operand.ty):
                return self._delegate(node, scope, types, where)
            if op == "-":
                # `0 - Int.MIN` overflows, so negation traps like any other
                # subtraction rather than being the one wrapping operator.
                return _E(f"(call $int_sub (i64.const 0) {operand.wat})", "Int")
            if op == "!":
                return _E(f"(i32.eqz {operand.wat})", "Bool")
            raise EmitError(f"{where}: unary `{op}` is not lowerable on this tier")

        op = node.get("op")
        left = self._lower(node.get("left"), scope, types, where)
        right = self._lower(node.get("right"), scope, types, where)
        if _is_unit_type(left.ty) or _is_unit_type(right.ty):
            raise EmitError(f"{where}: `{op}` on a void expression")
        _refuse_float_operands(node, where)
        operand_ty = left.ty if left.ty == right.ty else None
        instruction = _bin_instr(op, operand_ty) if _is_scalar_type(operand_ty) else None
        if instruction is None:
            # `??`, Str concatenation, Str equality, List `+` — the engine has
            # them and knows the operand types; it refuses with its own reason
            # if the combination is genuinely not lowerable.
            return self._delegate(node, scope, types, where)
        result_ty = "Bool" if op in _COMPARISON_OPS else "Int"
        return _E(f"({instruction} {left.wat} {right.wat})", result_ty)

    def _statement(self, node: Any, scope: dict[str, str], where: str,
                   types: dict[str, str | None] | None = None) -> str:
        """An expression evaluated for effect: drop an unused result."""
        wat, has_result = self._expr(node, scope, where, types)
        return f"(drop {wat})" if has_result else wat

    # -- delegation to the v3 value engine -----------------------------------

    def _delegate(self, node: Any, scope: dict[str, str],
                  types: dict[str, str | None], where: str) -> _E:
        v3_node = self._to_v3(node, scope, types, where)
        # match/arrow scratch locals have to be declared on the enclosing
        # function, and `_collect_*` stamps ids onto the nodes, so both run
        # before the engine renders the tree.
        binds: set[str] = set()
        scruts: set[str] = set()
        self.v3._collect_match_locals(v3_node, binds, scruts)
        arrows: set[str] = set()
        self.v3._collect_arrow_locals(v3_node, arrows)
        self.extra_locals.update(f"l_{name}" for name in binds)
        self.extra_locals.update(scruts)
        self.extra_locals.update(arrows)
        cdiv_locals: list[str] = []
        self.v3._collect_cdiv_locals(v3_node, cdiv_locals)
        self.extra_locals.update(cdiv_locals)
        self.uses_v3 = True
        self.func_uses_v3 = True
        return self.v3._expr(v3_node, _Scope(dict(scope), dict(types)), where)

    def _open_function(self) -> None:
        """Reset the engine's per-function state before lowering a body.

        `_V3Emitter` keeps arrow/match/loop counters and the scratch-local name
        on itself (they are per-function in the `fn` tier); a component method
        is a function too, so it gets the same fresh state.
        """
        self.v3._tmp = "__revl_tmp"
        self.v3._reset_tmp_pool()
        self.v3._arrows = {}
        self.v3._arrow_counter = 0
        self.v3._match_counter = 0
        self.v3._cdiv_counter = 0
        self.v3._loop_counter = 0
        self.v3._for_temps = []
        self.v3._local_types = {}
        self.extra_locals = set()
        self.func_uses_v3 = False

    # The cordis-wasm runtime documents job id 13 (and any negative id) as its
    # refusal hook for L-Raise tests. Interning would otherwise never produce
    # one, so the name `refuse` is reserved for it: the affordance stays
    # reachable from revl source and is named on both sides instead of being a
    # bare magic number in one of them.
    REFUSING_JOB = "refuse"
    REFUSING_JOB_ID = 13

    def _job_id(self, name: str) -> int:
        """A stable i32 id for a job name.

        Assigned in first-seen order per module, skipping the reserved
        refusal id so an ordinary job can never land on it by accident.
        """
        if name == self.REFUSING_JOB:
            return self.REFUSING_JOB_ID
        if name not in self.job_names:
            nxt = len(self.job_names) + 1
            while nxt == self.REFUSING_JOB_ID or nxt in self.job_names.values():
                nxt += 1
            self.job_names[name] = nxt
        return self.job_names[name]

    def _save_function_state(self) -> tuple:
        """`activate_step` is one function spanning every body segment, but a
        `provide` step in the middle of that body opens functions of its own.
        Save the activation function's rendering state across them."""
        return (self.extra_locals, self.func_uses_v3, self.v3._tmp,
                self.v3._arrows, self.v3._arrow_counter, self.v3._match_counter,
                self.v3._loop_counter, self.v3._for_temps, self.v3._local_types)

    def _restore_function_state(self, saved: tuple) -> None:
        (self.extra_locals, self.func_uses_v3, self.v3._tmp,
         self.v3._arrows, self.v3._arrow_counter, self.v3._match_counter,
         self.v3._loop_counter, self.v3._for_temps, self.v3._local_types) = saved

    def _to_v3(self, node: Any, scope: dict[str, str],
               types: dict[str, str | None], where: str) -> Any:
        """Rewrite a component-flavoured node into the v3 renderer's vocabulary.

        The two IR dialects differ in exactly three places: names are
        ``name``/``id`` here and ``var``/``name`` there, a call to a top-level
        function is ``fn`` here and ``call`` there, and templates are ``format``
        here and ``interp`` there.  Component-only nodes (`req` calls, `config`,
        `host`) have no v3 spelling, so they are lowered on this path and
        spliced in as an opaque pre-lowered ``__wat`` node.
        """
        if isinstance(node, list):
            return [self._to_v3(item, scope, types, where) for item in node]
        if not isinstance(node, dict):
            return node
        kind = node.get("kind")
        if kind == "name":
            return {"kind": "var", "name": node.get("id")}
        if kind == "fn":
            name = _ident(node.get("name"), f"{where}: function")
            if name not in self.fn_by_name and name in self.externs:
                ext = self.externs[name]
                if _extern_wasm_body(ext) is None:
                    available = ", ".join(sorted((ext.get("bodies") or {}))) or "none"
                    raise EmitError(
                        f"{where}: extern `{name}` has no @wasm body — not portable "
                        f"to this backend (available: {available})"
                    )
                # a @wasm-bodied extern lowers to an internal `(func $name …)`
                # emitted into this component's own module, so `call $name`
                # resolves and the component stays self-contained
                if name not in self.needed_externs:
                    self.needed_externs.append(name)
            return {
                "kind": "call",
                "callee": {"kind": "var", "name": name},
                "args": [self._to_v3(arg, scope, types, where)
                         for arg in node.get("args") or []],
            }
        if kind == "format":
            parts: list[list] = []
            args = node.get("args") or []
            for part_kind, part in _format_parts(node.get("template") or ""):
                if part_kind == "text":
                    parts.append(["text", part])
                    continue
                if part >= len(args):
                    raise EmitError(f"{where}: template placeholder ${part} has no argument")
                parts.append(["expr", self._to_v3(args[part], scope, types, where)])
            return {"kind": "interp", "parts": parts}
        callee = node.get("callee")
        if kind == "call" and isinstance(callee, dict) \
                and callee.get("kind") == "field" \
                and isinstance(callee.get("target"), dict) \
                and callee["target"].get("kind") == "instance-get":
            # `s.<key>.method(..)` in the v3 provide-method dialect: a field
            # access (`.method`) on an `instance-get` target, then called. The
            # accessor has no v3 spelling — lower it component-side (resolving
            # `key` in the handle's realm) and splice the pre-lowered call in.
            synth = {"target": callee["target"], "method": callee.get("name"),
                     "args": node.get("args") or []}
            value = self._lower_instance_get_call(synth, callee["target"],
                                                  scope, types, where)
            return {"kind": "__wat", "wat": value.wat, "ty": value.ty}
        if kind in ("config", "host", "req") or (kind == "call" and node.get("callee") is None):
            value = self._lower(node, scope, types, where)
            return {"kind": "__wat", "wat": value.wat, "ty": value.ty}
        return {key: self._to_v3(value, scope, types, where)
                for key, value in node.items()}

    # -- the top-level `fn`s this component needs in its own module ----------

    @staticmethod
    def _called_names(node: Any, acc: set[str]) -> set[str]:
        """Function names referenced anywhere in a tree, in either dialect."""
        if isinstance(node, dict):
            if node.get("kind") == "fn" and isinstance(node.get("name"), str):
                acc.add(node["name"])
            callee = node.get("callee")
            if node.get("kind") == "call" and isinstance(callee, dict) \
                    and callee.get("kind") == "var" and isinstance(callee.get("name"), str):
                acc.add(callee["name"])
            for value in node.values():
                _ComponentEmitter._called_names(value, acc)
        elif isinstance(node, list):
            for value in node:
                _ComponentEmitter._called_names(value, acc)
        return acc

    def _need_fn(self, name: str) -> None:
        if name in self.needed_fns or name not in self.fn_by_name:
            return
        self.needed_fns.append(name)   # before recursing: `fn fib` calls itself
        for inner in sorted(self._called_names(self.fn_by_name[name].get("body"), set())):
            self._need_fn(inner)

    def _plan(self) -> None:
        """Compute the call closure and pool the string literals it can reach.

        Literals must be pooled before anything renders, because `_str_ptr`
        resolves an offset at render time.
        """
        body = self.ir.get("body") or []
        for name in sorted(self._called_names(body, set())):
            self._need_fn(name)
        roots: list[Any] = [body]
        roots.extend(self.fn_by_name[name].get("body") for name in self.needed_fns)
        self.v3._collect_string_literals(roots)

    # -- component -----------------------------------------------------------

    def emit(self) -> str:
        where = self.name
        self._plan()
        self._open_function()
        scope: dict[str, str] = {}
        segments: list[str] = []          # activate_step bodies, in order
        inverses: list[tuple[int, str]] = []  # (segment index completed, wat)
        provide_funcs: list[str] = []

        for step in self.ir.get("body") or []:
            kind = step.get("step")
            if kind in ("let-effect", "effect"):
                seg = []
                if kind == "let-effect":
                    bind = _ident(step.get("bind"), f"{where}: bind")
                    glob = f"$g_{bind}"
                    value = self._lower(step["acquire"], scope, {}, where)
                    if _is_unit_type(value.ty):
                        raise EmitError(
                            f"{where}: `let {bind}` binds a void acquisition — "
                            f"use a plain `effect` step"
                        )
                    # the global carries the acquisition's *value*, so an Int
                    # acquisition needs an i64 global, not an i32 one
                    self.globals.append((glob, _wasm_ty(value.ty)))
                    seg.append(f"(global.set {glob} {value.wat})")
                    scope[bind] = f"(global.get {glob})"
                else:
                    seg.append(self._statement(step["acquire"], scope, where))
                index = len(segments) + 1
                inverses.append((index, self._statement(step["undo"], scope, where)))
                segments.append("\n      ".join(seg))
            elif kind == "emit":
                seg = [self._statement(step["expr"], scope, where)]
                index = len(segments) + 1
                if step.get("compensate") is not None:
                    inverses.append((index, self._statement(step["compensate"], scope, where)))
                segments.append("\n      ".join(seg))
            elif kind == "await":
                # A1 on the substrate: the segment launches an async host op;
                # the runtime awaits the fiber's pending futures before the
                # next boundary check, so the iteration lands (inertia) and a
                # divert during the wait skips every later step
                expr = step.get("expr") or {}
                if expr.get("kind") != "host" or expr.get("fn") != "Job.run" or len(expr.get("args") or []) != 1:
                    raise EmitError(
                        f"{where}: `await` on this tier supports only `Job.run(name)` "
                        f"(the runtime's async host op); other awaitables live on "
                        f"the hosted backends"
                    )
                # `Job.run` takes a name (typecheck.py's host contract), and the
                # runtime's host op takes an i32. A *literal* name is known at
                # compile time, so intern it: each distinct name gets a stable
                # id and the tier keeps the same contract as every other one
                # rather than a private i32 spelling of it. A computed name is
                # not knowable here, and says so.
                arg = expr["args"][0]
                if not (isinstance(arg, dict) and arg.get("kind") == "lit"
                        and isinstance(arg.get("value"), str)):
                    raise EmitError(
                        f"{where}: Job.run needs a literal name on the cordis-wasm "
                        f"tier — the host op is i32-only, so the name is interned "
                        f"at compile time and a computed one cannot be"
                    )
                job_id = self._job_id(arg["value"])
                self.uses_job = True
                segments.append(f"(call $host_job_run (i32.const {job_id}))")
            elif kind == "provide":
                saved = self._save_function_state()
                provide_funcs.extend(self._provide(step, scope, where))
                self._restore_function_state(saved)
            else:
                raise EmitError(f"{where}: unknown step {kind!r}")

        # scratch slots a delegated expression needed inside a body segment or
        # an inverse — they belong to activate_step/deactivate, not a method
        self.activation_locals = sorted(self.extra_locals)
        if self.func_uses_v3:
            self.activation_locals.append(self.v3._tmp)
            # deeper scratch pointers minted by nested allocations in a segment
            self.activation_locals.extend(sorted(self.v3._tmp_extra))
        return self._module(segments, inverses, provide_funcs)

    def _provide(self, step: dict, scope: dict[str, str], where: str) -> list[str]:
        key = _ident(step.get("name"), f"{where}: provide key")
        service_name = step.get("service")
        service = self.services.get(service_name)
        if service is None or self.provides.get(key) != service_name:
            raise EmitError(f"{where}: provide {key!r} does not match the component header")
        declared = service.get("methods") or {}
        funcs = []
        for method in step.get("methods") or []:
            mname = _ident(method.get("name"), f"{where}: method")
            spec = declared.get(mname)
            if spec is None:
                raise EmitError(f"{where}: {mname!r} is not a method of {service_name}")
            spec_params = spec.get("params") or []
            params = [_ident(p, f"{where}: param") for p in method.get("params") or []]
            if len(params) != len(spec_params):
                raise EmitError(f"{where}: method {mname!r} arity does not match the service")

            self._open_function()
            # the allocation scratch slot must not shadow a binding of the
            # method's own (the `fn` tier picks its temp the same way)
            self.v3._tmp = self.v3._fresh_tmp(
                set(params) | {mstep.get("name") for mstep in method.get("body") or []
                               if isinstance(mstep.get("name"), str)})
            mscope = dict(scope)
            mtypes: dict[str, str | None] = {name: "Int" for name in scope}
            decl = []
            for i, param in enumerate(params):
                # each service parameter crosses at the width of its declared
                # type: an `Int` is a 64-bit *value*, a Str/List/record/variant/
                # Opt/Result is an i32 *pointer* into this module's memory.
                ptype = spec_params[i].get("type")
                pwty = self._boundary_wty(ptype, f"{where}: {key}.{mname} param {param!r}")
                decl.append(f"(param $p_{param} {pwty})")
                mscope[param] = f"(local.get $p_{param})"
                mtypes[param] = ptype
            result_wty = self._boundary_wty(spec.get("returns"),
                                            f"{where}: {key}.{mname} return")
            has_result = result_wty is not None
            if has_result:
                decl.append(f"(result {result_wty})")

            body_lines = []
            mlocals: list[str] = []
            mwhere = f"{where}.{key}.{mname}"
            for mstep in method.get("body") or []:
                mkind = mstep.get("step")
                if mkind == "return":
                    if mstep.get("expr") is None:
                        # a void service operation: `{"step": "return",
                        # "expr": null}` is the natural body, and wasm has the
                        # instruction for it
                        if has_result:
                            raise EmitError(
                                f"{mwhere}: bare `return` in a method the service "
                                f"declares as returning {spec.get('returns')!r}"
                            )
                        body_lines.append("(return)")
                        continue
                    value = self._lower(mstep["expr"], mscope, mtypes, mwhere)
                    if has_result and _is_unit_type(value.ty):
                        raise EmitError(f"{mwhere}: void expression returned from a typed method")
                    # ABI-width agreement: a rich value crosses as a pointer when
                    # the operation is declared over that rich type, but a value
                    # whose wasm width disagrees with the declared return (a List
                    # from an Int-declared op) is a real mismatch and stays refused.
                    if has_result and _wasm_ty(value.ty) != result_wty:
                        raise EmitError(
                            f"{mwhere}: a {value.ty!r} value cannot cross this tier's "
                            f"scalar service boundary — the operation is declared "
                            f"{spec.get('returns')!r}; compound values stay inside the "
                            f"module (return them from a `fn`, or use a hosted backend)"
                        )
                    body_lines.append(
                        value.wat if has_result
                        else f"(drop {value.wat})" if not _is_unit_type(value.ty)
                        else value.wat)
                elif mkind == "emit":
                    body_lines.append(self._statement(mstep["expr"], mscope, mwhere, mtypes))
                    if mstep.get("compensate") is not None:
                        raise EmitError(
                            f"{mwhere}: method-time compensation is not lowerable — "
                            f"the wasm accumulator is the activation state machine"
                        )
                elif mkind in ("let", "assign"):
                    # a plain value binding: a wasm local, since a method body
                    # is a function and locals are exactly what it has
                    name = mstep.get("name")
                    if not isinstance(name, str) or not name.isidentifier():
                        raise EmitError(f"{mwhere}: bad binding name {name!r}")
                    value = self._lower(mstep["value"], mscope, mtypes, mwhere)
                    if _is_unit_type(value.ty):
                        raise EmitError(f"{mwhere}: `{name}` is bound to a void expression")
                    if mkind == "let":
                        if name in mlocals:
                            raise EmitError(f"{mwhere}: `{name}` is already bound")
                        mlocals.append(name)
                        mscope[name] = f"(local.get ${name})"
                        mtypes[name] = value.ty
                        self.v3._declare_local(name, value.ty, mwhere)
                    elif name not in mlocals:
                        raise EmitError(f"{mwhere}: `{name}` is not declared")
                    body_lines.append(f"(local.set ${name} {value.wat})")
                elif mkind in ("effect", "let-effect"):
                    raise EmitError(
                        f"{mwhere}: method-time effects are not lowerable — the "
                        f"wasm accumulator is fixed at activation (state machine); "
                        f"use a hosted backend for dynamic method-time acquisition"
                    )
                else:
                    raise EmitError(f"{mwhere}: unknown step {mkind!r}")

            header = f'(func (export "{self._provide_prefix(key)}.{mname}") {" ".join(decl)}'.rstrip()
            # wasm requires local declarations before the body — and each one
            # now has to be declared at the width of the value it holds, which
            # is only known once the body has been lowered (`_declare_local`)
            if mlocals:
                header += "".join(self.v3._local_decl(name) for name in mlocals)
            # scratch slots the value engine needs (match binds + scrutinee
            # pointers, inlined-arrow params, the allocation temporary)
            for extra in sorted(self.extra_locals):
                header += self.v3._local_decl(extra)
            if self.func_uses_v3:
                header += f" (local ${self.v3._tmp} i32)"
                # deeper scratch pointers from nested allocations in the body
                for extra in sorted(self.v3._tmp_extra):
                    header += f" (local ${extra} i32)"
            body = "\n    ".join(body_lines) if body_lines else "nop"
            funcs.append(f"  {header}\n    {body})")
        missing = set(declared) - {m.get("name") for m in step.get("methods") or []}
        if missing:
            raise EmitError(f"{where}: provision {key!r} is missing method {sorted(missing)[0]!r}")
        return funcs

    def _module(self, segments: list[str], inverses: list[tuple[int, str]], provide_funcs: list[str]) -> str:
        # the activation function's local widths come from the engine's
        # per-function record, and `_emit_function` below resets it — so render
        # the declarations before the `fn`s are emitted, not after
        activation_decls = "".join(self.v3._local_decl(name)
                                   for name in self.activation_locals)
        # the top-level `fn`s this component calls are emitted into its own
        # module, so `call $name` resolves and the component stays a single
        # self-contained artifact for the runtime to instantiate
        fn_defs = [self.v3._emit_function(self.fn_by_name[name]) for name in self.needed_fns]
        # @wasm-bodied externs this component calls, emitted as internal funcs
        extern_defs = [_emit_extern_func(self.externs[name], self.v3._check_type)
                       for name in self.needed_externs]
        fn_defs = extern_defs + fn_defs
        rendered = "\n".join(provide_funcs + fn_defs + segments
                             + [wat for _index, wat in inverses])
        # linear memory is pulled in only when something actually reaches for
        # it, so a scalar-only component emits no memory at all
        needs_memory = (self.boundary_uses_memory
                        or (self.uses_v3 and any(token in rendered for token in _MEMORY_TOKENS)))
        # the checked-arithmetic and named-division helpers are *not* memory:
        # `x + 1` traps on overflow through `$int_add` in a component that
        # never touches linear memory, so they get their own gate. (Before
        # this, `x.div_floor(2)` in a memory-free component emitted a call to
        # an `$int_div_floor` that was never defined.)
        needs_arith = any(token in rendered for token in _ARITH_TOKENS)

        lines = [f";; Generated by the revl cordis-wasm backend (ir_version {self.ir_version}) — do not edit.",
                 f";; component {self.name}",
                 "(module"]
        lines.extend(self._realm_sections())
        for (key, op), (param_wtys, result_wty) in sorted(self.imports.items()):
            # the coeffect ABI is one width per declared param/return: an `Int`
            # is an i64 *value*, a Str/List/record/variant/Opt/Result is an i32
            # *pointer* into this module's memory (the same canonical-ABI shape
            # the v3 functions tier uses). Truncating a value, or narrowing a
            # pointer, at the boundary would be the silent mismatch this guards.
            params = " ".join(f"(param {w})" for w in param_wtys)
            result = f" (result {result_wty})" if result_wty else ""
            sig = f" {params}" if params else ""
            lines.append(f'  (import "{self._import_module(key)}" "{op}" (func $req_{key}_{op}{sig}{result}))')
        if self.uses_job:
            # the job id is an interned compile-time *tag*, not an Int value:
            # it stays i32, which is what the runtime's host op declares
            lines.append('  (import "host" "job_run" (func $host_job_run (param i32)))')
        # instance-parametric spawn ABI (docs/design-v2-instances.md). Each is
        # inert unless the component actually spawns/reads-config, so a
        # non-spawning program's import section is byte-identical to before.
        for field, wty in sorted(self.config_imports.items()):
            lines.append(f'  (import "config" "{field}" (func $config_{field} (result {wty})))')
        for target, param_wtys in sorted(self.spawn_imports.items()):
            params = " ".join(f"(param {w})" for w in param_wtys)
            sig = f" {params}" if params else ""
            lines.append(f'  (import "spawn:{target}" "new" (func $spawn_{target}{sig} (result i32)))')
        if self.uses_dispose:
            lines.append('  (import "dispose" "instance" (func $dispose_instance (param i32) (result i32)))')
        # instance-accessor ABI (docs/design-v2-instances.md "Instance accessor").
        # `s.<key>.method(..)` reads a provision back through a spawn handle: the
        # first param is the i32 handle (which realm to resolve `key` in), then
        # the method's own scalar params. Inert unless the component reads a
        # provision, so a non-accessor module's import section is byte-identical.
        for (component, key, op), (param_wtys, result_wty) in sorted(self.instance_imports.items()):
            method_params = " ".join(f"(param {w})" for w in param_wtys)
            sig = f" {method_params}" if method_params else ""
            result = f" (result {result_wty})" if result_wty else ""
            lines.append(
                f'  (import "instance:{component}" "{key}.{op}" '
                f'(func $inst_{component}_{key}_{op} (param i32){sig}{result}))')
        if needs_memory:
            lines.append('  (memory (export "memory") 1)')
            for offset, data in self.v3.data_segments:
                lines.append(f'  (data (i32.const {offset}) "{_wat_bytes(data)}")')
            lines.append(f"  (global $__hp (mut i32) (i32.const {self.v3.heap_start}))")
        lines.append("  (global $__step (mut i32) (i32.const 0))")
        for glob, glob_ty in self.globals:
            zero = "i64.const 0" if glob_ty == "i64" else "i32.const 0"
            lines.append(f"  (global {glob} (mut {glob_ty}) ({zero}))")

        # activate_step: one iteration per body segment (paper §4.3.2)
        lines.append(f'  (func (export "activate_step") (result i32){activation_decls}')
        total = len(segments)
        for i, seg in enumerate(segments):
            more = 1 if i + 1 < total else 0
            lines.append(f"    (if (i32.eq (global.get $__step) (i32.const {i}))")
            lines.append("      (then")
            lines.append(f"      {seg}")
            lines.append(f"      (global.set $__step (i32.const {i + 1}))")
            lines.append(f"      (return (i32.const {more}))))")
        lines.append("    (i32.const 0))")

        # deactivate: the accumulator — completed steps' inverses, LIFO
        lines.append(f'  (func (export "deactivate"){activation_decls}')
        if inverses:
            for index, wat in reversed(inverses):
                lines.append(f"    (if (i32.ge_s (global.get $__step) (i32.const {index}))")
                lines.append("      (then")
                lines.append(f"      {wat}))")
        else:
            lines.append("    nop")
        lines.append("  )")

        lines.extend(provide_funcs)
        if needs_memory:
            lines.extend(self.v3._helper_funcs())
            # `$f64_to_str` is emitted only when a Float is actually rendered,
            # so a component that never interpolates a Float keeps a
            # byte-identical helper preamble (the v1 goldens are unchanged).
            if "$f64_to_str" in rendered:
                lines.append(self.v3._helper_f64_to_str())
            # The reader builtins are pulled in the same way — only when a
            # component actually reaches for one — so a component that never
            # splits/joins/searches keeps a byte-identical helper preamble.
            if "$str_index_of" in rendered:
                lines.append(self.v3._helper_str_index_of())
            if "$str_split" in rendered:
                lines.append(self.v3._helper_str_split())
            if "$str_join" in rendered:
                lines.append(self.v3._helper_str_join())
        elif needs_arith:
            lines.extend(self.v3._arith_helper_funcs())
        lines.extend(fn_defs)
        lines.append(")")
        return "\n".join(lines) + "\n"


#: WAT that only appears when a value lives in linear memory. Scanning the
#: rendered body for these is what decides whether a component module declares
#: a memory at all — a scalar-only component declares none.
_MEMORY_TOKENS = (
    "$alloc", "$str_", "$list_", "$int_to_str",
    "i32.load", "i32.store", "i64.load", "i64.store", "memory.copy",
)

#: The arithmetic helpers, which need no memory. All six travel together
#: because they call each other ($int_div_euclid -> $int_div_floor).
_ARITH_TOKENS = (
    "$int_add", "$int_sub", "$int_mul",
    "$int_div_floor", "$int_div_euclid", "$int_mod",
    "$int32_add", "$int32_sub", "$int32_mul", "$int32_narrow",
)


def _format_parts(template: str) -> list[tuple[str, Any]]:
    """Split a component `format` template into text/argument-index parts.

    The component dialect spells a template as ``"n=$0"`` plus an argument
    list; the v3 renderer wants ``interp`` parts. One function so the literal
    pooler and the node rewriter cannot disagree about the text segments.
    """
    parts: list[tuple[str, Any]] = []
    buffer: list[str] = []
    index = 0
    while index < len(template):
        char = template[index]
        if char == "$" and index + 1 < len(template) and template[index + 1].isdigit():
            end = index + 1
            while end < len(template) and template[end].isdigit():
                end += 1
            if buffer:
                parts.append(("text", "".join(buffer)))
                buffer = []
            parts.append(("arg", int(template[index + 1:end])))
            index = end
            continue
        buffer.append(char)
        index += 1
    if buffer:
        parts.append(("text", "".join(buffer)))
    return parts


#: Comparisons: same suffix on both widths, so the instruction is picked from
#: the *operand* type, not the result type (which is always Bool/i32).
_CMP_SUFFIX = {
    "==": "eq", "===": "eq", "!=": "ne", "!==": "ne",
    "<": "lt_s", ">": "gt_s", "<=": "le_s", ">=": "ge_s",
}
_BOOL_OPS = {"&&": "i32.and", "||": "i32.or"}
#: `Int` overflow *traps* (docs/arithmetic.md), and wasm has no checked
#: arithmetic, so `+`/`-`/`*` go through helpers that test for overflow and
#: execute `unreachable`. `/` and `%` cannot overflow into a wrong value — they
#: trap natively on a zero divisor, which is the fault every other tier gives.
_TRAPPING_INT_OPS = {"+": "call $int_add", "-": "call $int_sub", "*": "call $int_mul"}
#: Int32 `+ - *` trap at the i32 edge through their own checked helpers, the
#: same discipline as `Int` at half the width (docs/arithmetic.md).
_TRAPPING_INT32_OPS = {"+": "call $int32_add", "-": "call $int32_sub",
                       "*": "call $int32_mul"}
_RAW_INT_OPS = {"/": "i64.div_s", "%": "i64.rem_s"}
_COMPARISON_OPS = frozenset(set(_CMP_SUFFIX) | set(_BOOL_OPS))
_BINARY_OPS = frozenset(set(_CMP_SUFFIX) | set(_BOOL_OPS)
                        | set(_TRAPPING_INT_OPS) | set(_RAW_INT_OPS))


def _bin_instr(op: str, operand_ty: str | None) -> str | None:
    """The wasm instruction implementing `op` over `operand_ty` operands.

    Returns None when this tier has no single instruction for the combination
    (Str/List operands, `??`) — the caller then routes to the helper that does.
    """
    if op in _BOOL_OPS:
        return _BOOL_OPS[op] if operand_ty == "Bool" else None
    if op in _CMP_SUFFIX:
        if operand_ty == "Int":
            return f"i64.{_CMP_SUFFIX[op]}"
        if operand_ty in ("Bool", "Int32"):
            # Int32 comparisons are signed i32 (lt_s/…); Bool uses eq/ne only.
            return f"i32.{_CMP_SUFFIX[op]}"
        return None
    if operand_ty == "Int32":
        # Only `+ - *` reach here for Int32 (docs/arithmetic.md): `/` yields
        # Float (refused on this tier) and `%` is Int-only.
        return _TRAPPING_INT32_OPS.get(op)
    if operand_ty != "Int":
        return None
    return _TRAPPING_INT_OPS.get(op) or _RAW_INT_OPS.get(op)


def _refuse_float_operands(node: Any, where: str) -> None:
    """`Float` stays refused on this tier, by name (docs/arithmetic.md).

    The IR annotates `/ % + - *` with the *operand* type, which is the only
    thing that distinguishes `Int / Int` from `Float / Float` in the node. It
    is also what tells this tier that a site is Float-valued before it tries to
    infer a type for it, so the refusal names Float rather than surfacing as a
    confusing failure further down.
    """
    if isinstance(node, dict) and node.get("operands") == "Float":
        raise EmitError(
            f"{where}: type 'Float' is not lowerable — this tier supports "
            f"Int/Bool, and the operands of `{node.get('op')}` are Float")


def _extern_wasm_body(ext: dict) -> str | None:
    """The `@wasm` body text of an extern, or None if it has none.

    An extern's implementation on a backend is the body tagged for that backend
    (`bodies.rs` on rust, `bodies.wasm` here); a `@wasm` body is raw WAT for the
    function's body, referencing each parameter as `$p_<name>` — the same
    spelling a v3 `fn` gives its params — and leaving the result on the stack.
    """
    body = (ext.get("bodies") or {}).get("wasm")
    return body if isinstance(body, str) else None


def _emit_extern_func(ext: dict, check_type) -> str:
    """Render a `@wasm`-bodied extern as an internal `(func $name …)`.

    Widths follow the same rule as every other value on this tier (`_wasm_ty`):
    an `Int` param/return is an i64 value, a Str/List/record/variant/Opt/Result
    is an i32 pointer. `check_type` is the caller's lowerability gate so a
    Float/Map/function-typed extern is refused the same way an ordinary value of
    that type is, rather than emitting an unvalidated body.
    """
    name = _ident(ext.get("name"), "extern name")
    body = (_extern_wasm_body(ext) or "").strip()
    decl: list[str] = []
    for param in ext.get("params") or []:
        pname = _ident(param.get("name"), f"extern {name}: parameter")
        ptype = param.get("type")
        check_type(ptype, f"extern {name}: param {pname}")
        if not _is_unit_type(ptype):
            decl.append(f"(param $p_{pname} {_wasm_ty(ptype)})")
    rtype = ext.get("returns")
    check_type(rtype, f"extern {name}: return")
    if not _is_unit_type(rtype):
        decl.append(f"(result {_wasm_ty(rtype)})")
    header = f"(func ${name}"
    if decl:
        header += " " + " ".join(decl)
    return f"  {header}\n    {body or 'nop'})"


def _is_unit_type(ty: str | None) -> bool:
    return ty in (None, "Unit")


def _is_scalar_type(ty: str | None) -> bool:
    """A value that lives in a wasm register rather than in linear memory.

    (Was `_is_i32_type` while every value was an i32; an `Int` is an i64 now,
    so the question the callers actually ask — "does this cross a service
    boundary without a pointer?" — needed its own name.)
    """
    return ty in ("Int", "Bool")


def _is_list_type(ty: str | None) -> bool:
    return isinstance(ty, str) and ty.startswith("List[")


def _list_elem(ty: str) -> str:
    return ty[len("List[") : -1]


def _split_types(inner: str) -> list[str]:
    """Split top-level comma-separated type args, respecting `[]` nesting."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in inner:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return parts


class _E:
    def __init__(self, wat: str, ty: str | None) -> None:
        self.wat = wat
        self.ty = ty


class _Scope:
    def __init__(self, slots: dict[str, str], types: dict[str, str | None]) -> None:
        self.slots = slots
        self.types = types


def _f64_literal(value: float) -> str:
    """The WAT `f64.const` payload for a Float literal, bit-exact.

    Python's `float.hex()` is the IEEE-754 hexadecimal form WAT accepts
    verbatim (`0x1.b1ae4d6e2ef50p+69`), so the constant that reaches wasmtime
    is the same 64-bit pattern the source named — no decimal round-trip, no
    ambiguity. NaN/Infinity never arrive here (they are computed, not written).
    """
    return float.hex(value)


def _wat_bytes(data: bytes) -> str:
    parts: list[str] = []
    for byte in data:
        if byte == 0x22:
            parts.append("\\22")
        elif byte == 0x5C:
            parts.append("\\5c")
        elif 0x20 <= byte <= 0x7E:
            parts.append(chr(byte))
        else:
            parts.append(f"\\{byte:02x}")
    return "".join(parts)


def test_export_names(tests: list) -> list[tuple[str, str]]:
    """The deterministic wasm export for each `test` block, in document order.

    A test name is free-form source text ("add works"); an export name must be
    a wasm identifier, so it is slugified (`revl_test_add_works`) with a
    counter suffix on collision — deterministically, so the host-side test
    runner (`src/revl/test.py run_wasm`) can compute the same list from the IR
    alone and invoke each export by name.
    """
    used: set[str] = set()
    out: list[tuple[str, str]] = []
    for test in tests or []:
        base = re.sub(r"[^A-Za-z0-9_]", "_", str(test.get("name") or "test"))
        if not base or base[0].isdigit():
            base = f"test_{base}"
        name = f"revl_test_{base}"
        counter = 0
        while name in used:
            counter += 1
            name = f"revl_test_{base}_{counter}"
        used.add(name)
        out.append((test.get("name"), name))
    return out


class _V3Emitter:
    """IR v3 types + pure functions -> a standalone WAT module.

    `Int` is i64 (docs/arithmetic.md: 64-bit two's complement, overflow traps);
    Bool is i32, and every linear-memory *address* is i32 because wasm32
    addressing is. Str/List/record values use the canonical-ABI-shaped linear
    memory representation: a u32 length/count prefix followed by bytes (Str) or
    by one 8-byte slot per element/field. The emitted module exports its
    memory, so a host can read string/list/record results without an object
    model.
    """

    def __init__(self, types: dict, functions: list, externs: list, tests: list) -> None:
        self.types = types or {}
        self.all_types = dict(self.types)
        self.functions = functions or []
        self.externs = externs or []
        self.tests = tests or []
        self.fn_names = {fn.get("name") for fn in self.functions}
        self.fn_sigs = {
            fn.get("name"): {
                "params": [p.get("type") for p in (fn.get("params") or [])],
                "returns": fn.get("returns"),
            }
            for fn in self.functions
        }
        # a @wasm-bodied extern is a callable too: it lowers to an internal
        # `(func $name …)` (see `_emit_extern_func`), so its call site resolves
        # the same way a `fn` call does — through this signature table.
        self.extern_sigs = {
            ext.get("name"): {
                "params": [p.get("type") for p in (ext.get("params") or [])],
                "returns": ext.get("returns"),
            }
            for ext in self.externs
            if _extern_wasm_body(ext) is not None
        }
        self.literal_offsets: dict[str, int] = {}
        self.data_segments: list[tuple[int, bytes]] = []
        self.heap_start = 0
        self._anon = 0
        self._tmp = "__revl_tmp"
        # A single scratch pointer clobbers itself when one allocation nests
        # inside another (a record/list/variant used as a field, element, or
        # payload of another one). Hand out a *distinct* scratch per active
        # nesting depth instead: siblings reuse a name (safe — they don't
        # overlap in time), a nested allocation gets a deeper one. Depth 0
        # keeps the historical `__revl_tmp` name so non-nested output — and the
        # goldens — stay byte-identical.
        self._tmp_stack: list[str] = []
        self._tmp_extra: set[str] = set()
        # wasm local name (already prefixed: `l_x`, `ap_v_1`, `msc_2`, …) ->
        # wasm value type. Populated while a body is lowered, because that is
        # the first moment a binding's revl type is known; the `(local …)`
        # declarations are rendered from it afterwards. Everything not recorded
        # here is a pointer or an internal counter, hence i32.
        self._local_types: dict[str, str] = {}

    def _declare_local(self, name: str, ty: str | None, where: str) -> None:
        """Record the wasm width of a local. Idempotent; conflicts are fatal."""
        wasm = _wasm_ty(ty)
        previous = self._local_types.get(name)
        if previous is not None and previous != wasm:
            raise EmitError(
                f"{where}: `{name}` is bound to both a {previous} and a {wasm} "
                f"value on different paths — a wasm local has one type, so this "
                f"tier cannot hold that binding; split it into two names"
            )
        self._local_types[name] = wasm

    def _local_decl(self, name: str) -> str:
        return f" (local ${name} {self._local_types.get(name, 'i32')})"

    def _reset_tmp_pool(self) -> None:
        self._tmp_stack = []
        self._tmp_extra = set()

    def _acquire_tmp(self) -> str:
        depth = len(self._tmp_stack)
        name = self._tmp if depth == 0 else f"{self._tmp}_n{depth}"
        if depth != 0:
            self._tmp_extra.add(name)
        self._tmp_stack.append(name)
        return name

    def _release_tmp(self) -> None:
        self._tmp_stack.pop()

    # -- type layouts (documentation) ----------------------------------------

    def _type_comments(self) -> list[str]:
        if not self.types:
            return []
        lines = ["  ;; --- record/variant layouts (docs/syntax-2.0.md §2) ---"]
        for name, spec in self.types.items():
            name = _ident(name, "type name")
            if spec.get("kind") == "record":
                fields = " ".join(
                    f"{_ident(field, 'record field')}:{ftype}"
                    for field, ftype in (spec.get("fields") or {}).items()
                )
                lines.append(f"  ;; @record {name} {{ {fields} }}")
            else:
                lines.append(f"  ;; @variant {name} (tagged union layout)")
                for case in spec.get("cases") or []:
                    cname = _ident(case.get("name"), "case name")
                    payload = case.get("payload") or "unit"
                    lines.append(f"  ;;   case {cname}: {payload}")
        return lines

    def _unsupported_comments(self) -> list[str]:
        # only externs with no @wasm body are unsupported now; a @wasm-bodied
        # one is emitted as a real `(func $name …)` in `emit()`.
        bodyless = [ext for ext in self.externs if _extern_wasm_body(ext) is None]
        if not bodyless:
            return []
        names = ", ".join(_ident(ext.get("name"), "extern name") for ext in bodyless)
        return [f"  ;; unsupported on this tier: externs {names} (no @wasm body)"]

    def _extern_funcs(self) -> list[str]:
        """The @wasm-bodied externs, rendered as internal functions."""
        return [_emit_extern_func(ext, self._check_type)
                for ext in self.externs if _extern_wasm_body(ext) is not None]

    # -- type/layout helpers --------------------------------------------------

    def _check_type(self, ty: str | None, where: str) -> None:
        if ty in (None, "Unit", "Int", "Int32", "Bool", "Str", "Bytes"):
            return
        if _is_fn_type(ty):
            raise EmitError(
                f"{where}: a declared function type ({ty}) is not lowerable on "
                "this tier — the emitted module is wasm MVP with no closures "
                "and no function references, so a function value has no "
                "representation. Arrows called where they are bound still "
                "lower: they are inlined at the call site "
                "(`_inline_arrow`). See docs/function-types.md."
            )
        if _is_list_type(ty):
            self._check_type(_list_elem(ty), f"{where}: list element")
            return
        spec = self.all_types.get(ty)
        if spec is not None and spec.get("kind") == "record":
            # only function-typed fields are inspected here: the rest of the
            # record surface is checked where the field is actually read, and
            # widening this into a full field sweep is a separate change
            for fname, ftype in (spec.get("fields") or {}).items():
                if _is_fn_type(ftype):
                    self._check_type(ftype, f"{where}: field {fname!r} of {ty!r}")
            return
        # tagged unions (user variants, Opt, Result) lower to a two-slot
        # [u32 tag][pad][payload] cell — the payload is one 8-byte slot (an
        # Int/Bool value or a pointer). Check payloads are themselves lowerable.
        layout = self._tagged_layout(ty)
        if layout is not None:
            for _case, payload in layout:
                if payload is not None:
                    self._check_type(payload, f"{where}: payload of {ty!r}")
            return
        raise EmitError(
            f"{where}: type {ty!r} is not lowerable — this tier supports "
            f"Int/Bool/Str/Bytes/List/record/variant/Opt/Result values"
        )

    def _record_fields(self, ty: str | None) -> dict[str, str] | None:
        spec = self.all_types.get(ty or "")
        if spec is None or spec.get("kind") != "record":
            return None
        return spec.get("fields") or {}

    def _tagged_layout(self, ty: str | None) -> list[tuple[str, str | None]] | None:
        """Cases of a tagged union in tag order — (case_name, payload_type).
        User variants come from the type table; Opt/Result are built in. The
        tag is the case's index; the payload is one i32 slot (0 if None)."""
        if not ty:
            return None
        spec = self.all_types.get(ty)
        if spec is not None and spec.get("kind") == "variant":
            return [(c["name"], c.get("payload")) for c in spec.get("cases") or []]
        head = ty[: ty.index("[")] if "[" in ty else ty
        args = _split_types(ty[ty.index("[") + 1: ty.rindex("]")]) if "[" in ty else []
        if head == "Opt" and len(args) == 1:
            return [("None", None), ("Some", args[0])]
        if head == "Result" and len(args) == 2:
            return [("Ok", args[0]), ("Err", args[1])]
        return None

    def _tag_of(self, ty: str | None, case: str) -> int:
        layout = self._tagged_layout(ty) or []
        for index, (name, _payload) in enumerate(layout):
            if name == case:
                return index
        raise EmitError(f"case {case!r} is not a case of {ty!r}")

    def _anon_record(self, fields: list[tuple[str, str]]) -> str:
        self._anon += 1
        name = f"__revl_record{self._anon}"
        self.all_types[name] = {
            "params": [],
            "kind": "record",
            "fields": {field: ftype for field, ftype in fields},
        }
        return name

    def _collect_string_literals(self, roots: list | None = None) -> None:
        """Pool every string constant reachable from `roots` into data.

        `roots` defaults to this module's function bodies PLUS its `test`
        bodies — the tests are lowered later (as exported `revl_test_*`
        functions) but their string literals, template text segments and
        checked-division Err messages pool through the same `_str_ptr`, so
        they must be collected here or lowering a test raises "string
        literal … was not pooled". The component path passes its own set
        (its body plus only the functions it calls) so a component module
        carries just the data it can reach.
        """
        seen: dict[str, None] = {}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("kind") == "lit" and isinstance(node.get("value"), str):
                    seen.setdefault(node["value"], None)
                if node.get("method") in _CHECKED_DIVS:
                    # the total division forms carry their Err reason from
                    # the emitter, not from a literal in the source — pool it
                    # here so `_str_ptr` can name it at lowering time
                    seen.setdefault(_DIV_ZERO_MSG, None)
                if node.get("kind") == "interp":
                    # template text segments are string literals too
                    for part_kind, part in node.get("parts") or []:
                        if part_kind == "text":
                            seen.setdefault(part, None)
                if node.get("kind") == "format":
                    # the component dialect's template: same text, other shape
                    for part_kind, part in _format_parts(node.get("template") or ""):
                        if part_kind == "text":
                            seen.setdefault(part, None)
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        if roots is None:
            roots = ([fn.get("body") for fn in self.functions]
                     + [t.get("body") or [] for t in self.tests])
        for root in roots:
            walk(root)
        offset = 0
        for value in seen:
            raw = value.encode("utf-8")
            data = len(raw).to_bytes(4, "little") + raw
            self.literal_offsets[value] = offset
            self.data_segments.append((offset, data))
            offset = _align4(offset + len(data))
        # a string is a u32 length followed by bytes, so the pool itself only
        # needs 4-byte alignment; the heap above it hands out 8-byte slots, so
        # the first allocation has to start 8-aligned for `$alloc` to keep it
        self.heap_start = _align8(offset)

    def _str_ptr(self, value: str) -> str:
        offset = self.literal_offsets.get(value)
        if offset is None:
            raise EmitError(f"internal: string literal {value!r} was not pooled")
        return f"(i32.const {offset})"

    # -- the 8-byte value slot ------------------------------------------------
    #
    # Record fields, list elements and a tagged cell's payload all live in a
    # slot of `_SLOT` bytes, always read and written as an i64. An `Int` sits
    # there natively; a Bool or a pointer is zero-extended on the way in and
    # wrapped on the way out, so the same eight bytes are in the slot whichever
    # of the two views is taken. Doing it that way (rather than an `i32.store`
    # that leaves the high half untouched) means a slot whose element type the
    # emitter could not pin down — an `Any` variant payload, say — still reads
    # back the value that was written instead of half of it plus whatever the
    # allocator last left there.

    @staticmethod
    def _slot_load(address: str, ty: str | None) -> str:
        load = f"(i64.load {address})"
        return load if ty == "Int" else f"(i32.wrap_i64 {load})"

    @staticmethod
    def _slot_store(address: str, value: str, ty: str | None) -> str:
        if ty != "Int":
            value = f"(i64.extend_i32_u {value})"
        return f"(i64.store {address} {value})"


    # -- linear-memory runtime helpers ---------------------------------------

    def _helper_funcs(self) -> list[str]:
        return [
            self._helper_alloc(),
            self._helper_alloc_str(),
            self._helper_int_to_str(),
            self._helper_str_concat(),
            self._helper_str_eq(),
            self._helper_str_slice(),
            self._helper_str_char_at(),
            self._helper_str_char_code_at(),
            self._helper_str_cp_length(),
            self._helper_str_cp_offset(),
            self._helper_str_cp_slice(),
            self._helper_str_cp_char_at(),
            self._helper_str_cp_char_code_at(),
            self._helper_str_starts_with(),
            self._helper_str_ends_with(),
            self._helper_str_to_int(),
        ] + self._arith_helper_funcs() + [
            self._helper_list_push(),
            self._helper_list_concat(),
            self._helper_list_slice(),
        ]

    def _arith_helper_funcs(self) -> list[str]:
        """The helpers that need no linear memory — checked `+ - *` and the
        three named integer divisions. Kept separate so a component that does
        arithmetic and nothing else still gets them (`_ARITH_TOKENS`)."""
        return [
            self._helper_int_add(),
            self._helper_int_sub(),
            self._helper_int_mul(),
            self._helper_int_div_floor(),
            self._helper_int_div_euclid(),
            self._helper_int_mod(),
            self._helper_int32_add(),
            self._helper_int32_sub(),
            self._helper_int32_mul(),
            self._helper_int32_narrow(),
        ]

    def _helper_alloc(self) -> str:
        # `$n` is a byte count and the result is an address: both stay i32,
        # wasm32 addressing being 32-bit. The bump is rounded to 8 rather than
        # 4 so every allocation starts on an 8-byte slot boundary.
        return """  (func $alloc (param $n i32) (result i32)
    (local $p i32)
    (local.set $p (global.get $__hp))
    (global.set $__hp
      (i32.add
        (global.get $__hp)
        (i32.and
          (i32.add (local.get $n) (i32.const 7))
          (i32.const -8))))
    (local.get $p))"""

    def _helper_alloc_str(self) -> str:
        return """  (func $alloc_str (param $len i32) (result i32)
    (local $p i32)
    (local.set $p (call $alloc (i32.add (local.get $len) (i32.const 4))))
    (i32.store (local.get $p) (local.get $len))
    (local.get $p))"""

    def _helper_int_to_str(self) -> str:
        # The value is an i64 and the result is a string *address*, so this
        # helper straddles the split: `$n`/`$x`/`$d` are values, `$p`/`$i`/
        # `$len` are the address, the write cursor and a byte count.
        #
        # The digits are produced with unsigned division on the negated value,
        # which is what makes Int.MIN work: `0 - Int.MIN` wraps back to
        # Int.MIN, and Int.MIN read as unsigned is exactly its magnitude.
        return """  (func $int_to_str (param $n i64) (result i32)
    (local $neg i32)
    (local $x i64)
    (local $d i64)
    (local $len i32)
    (local $p i32)
    (local $i i32)
    (if (i64.eqz (local.get $n))
      (then
        (local.set $p (call $alloc_str (i32.const 1)))
        (i32.store8 (i32.add (local.get $p) (i32.const 4)) (i32.const 48))
        (return (local.get $p))))
    (local.set $neg (i64.lt_s (local.get $n) (i64.const 0)))
    (local.set $x (select
      (i64.sub (i64.const 0) (local.get $n))
      (local.get $n)
      (local.get $neg)))
    (local.set $len (i32.const 0))
    (local.set $d (local.get $x))
    (block (loop
      (br_if 1 (i64.eqz (local.get $d)))
      (local.set $len (i32.add (local.get $len) (i32.const 1)))
      (local.set $d (i64.div_u (local.get $d) (i64.const 10)))
      (br 0)))
    (local.set $p (call $alloc_str (i32.add (local.get $len) (local.get $neg))))
    (if (local.get $neg)
      (then (i32.store8 (i32.add (local.get $p) (i32.const 4)) (i32.const 45))))
    (local.set $i (i32.add (local.get $len) (local.get $neg)))
    (block (loop
      (br_if 1 (i64.eqz (local.get $x)))
      (local.set $i (i32.sub (local.get $i) (i32.const 1)))
      (i32.store8
        (i32.add (i32.add (local.get $p) (i32.const 4)) (local.get $i))
        (i32.wrap_i64
          (i64.add (i64.rem_u (local.get $x) (i64.const 10)) (i64.const 48))))
      (local.set $x (i64.div_u (local.get $x) (i64.const 10)))
      (br 0)))
    (local.get $p))"""

    def _helper_f64_to_str(self) -> str:
        # Canonical Float -> Str, the ECMAScript `Number::toString` form
        # (docs/strings.md), for the subset this hand-written tier renders
        # *exactly* — never approximately:
        #   * NaN            -> "NaN"
        #   * +/- Infinity   -> "Infinity" / "-Infinity"
        #   * integer-valued finite floats with |x| < 2^63  -> the decimal
        #     integer, with no trailing ".0" and with -0.0 rendered "0"
        #     (`$int_to_str` produces exactly the digits ES prescribes for any
        #     integer < 1e21, and every integer < 2^63 is one).
        # A non-integer float, or |x| >= 2^63 (whose ES form is exponent
        # notation, e.g. `1e21` -> "1e+21"), needs a shortest-round-trip
        # decimal conversion (Grisu/Ryu class) that is not implemented here;
        # rather than emit a string that would *diverge* from the other tiers,
        # this path traps. That trap is the honest fence — the still-open half
        # of docs/strings.md §"Remaining wasm WAT work".
        #
        # "Infinity" is written as two i32 stores of its little-endian ASCII:
        # "Infi" = 0x69666e49, "nity" = 0x7974696e; "-Infinity" prepends 0x2d.
        return """  (func $f64_to_str (param $x f64) (result i32)
    (local $p i32)
    (if (f64.ne (local.get $x) (local.get $x))
      (then
        (local.set $p (call $alloc_str (i32.const 3)))
        (i32.store8 (i32.add (local.get $p) (i32.const 4)) (i32.const 78))
        (i32.store8 (i32.add (local.get $p) (i32.const 5)) (i32.const 97))
        (i32.store8 (i32.add (local.get $p) (i32.const 6)) (i32.const 78))
        (return (local.get $p))))
    (if (f64.eq (f64.abs (local.get $x)) (f64.const inf))
      (then
        (if (f64.lt (local.get $x) (f64.const 0))
          (then
            (local.set $p (call $alloc_str (i32.const 9)))
            (i32.store8 (i32.add (local.get $p) (i32.const 4)) (i32.const 45))
            (i32.store (i32.add (local.get $p) (i32.const 5)) (i32.const 0x69666e49))
            (i32.store (i32.add (local.get $p) (i32.const 9)) (i32.const 0x7974696e)))
          (else
            (local.set $p (call $alloc_str (i32.const 8)))
            (i32.store (i32.add (local.get $p) (i32.const 4)) (i32.const 0x69666e49))
            (i32.store (i32.add (local.get $p) (i32.const 8)) (i32.const 0x7974696e))))
        (return (local.get $p))))
    (if (i32.and
          (f64.eq (local.get $x) (f64.trunc (local.get $x)))
          (f64.lt (f64.abs (local.get $x)) (f64.const 0x1p+63)))
      (then (return (call $int_to_str (i64.trunc_f64_s (local.get $x))))))
    (unreachable))"""

    def _helper_str_concat(self) -> str:
        return """  (func $str_concat (param $a i32) (param $b i32) (result i32)
    (local $la i32)
    (local $lb i32)
    (local $p i32)
    (local.set $la (i32.load (local.get $a)))
    (local.set $lb (i32.load (local.get $b)))
    (local.set $p (call $alloc_str (i32.add (local.get $la) (local.get $lb))))
    (memory.copy
      (i32.add (local.get $p) (i32.const 4))
      (i32.add (local.get $a) (i32.const 4))
      (local.get $la))
    (memory.copy
      (i32.add (i32.add (local.get $p) (i32.const 4)) (local.get $la))
      (i32.add (local.get $b) (i32.const 4))
      (local.get $lb))
    (local.get $p))"""

    def _helper_str_eq(self) -> str:
        return """  (func $str_eq (param $a i32) (param $b i32) (result i32)
    (local $n i32)
    (local $i i32)
    (local.set $n (i32.load (local.get $a)))
    (if (i32.ne (local.get $n) (i32.load (local.get $b)))
      (then (return (i32.const 0))))
    (local.set $i (i32.const 0))
    (block $done
      (loop $loop
        (br_if $done (i32.ge_u (local.get $i) (local.get $n)))
        (if (i32.ne
              (i32.load8_u (i32.add (i32.add (local.get $a) (i32.const 4)) (local.get $i)))
              (i32.load8_u (i32.add (i32.add (local.get $b) (i32.const 4)) (local.get $i))))
          (then (return (i32.const 0))))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $loop)))
    (i32.const 1))"""


    # -- checked arithmetic ---------------------------------------------------
    #
    # `Int` is 64-bit two's complement and overflow *traps* (docs/arithmetic.md
    # — python bound-checks, rust uses checked_*, java *Exact, go revlAdd/…).
    # wasm has no checked arithmetic at all, so the test is written out and the
    # fault is `unreachable`, which is a wasm trap. The one thing this tier
    # cannot carry is the message: a wasm trap has no payload, so `revl: Int
    # overflow` is not attached to it the way the hosted tiers attach it.

    def _helper_int_add(self) -> str:
        # Signed overflow iff both operands differ in sign from the result.
        return """  (func $int_add (param $a i64) (param $b i64) (result i64)
    (local $r i64)
    (local.set $r (i64.add (local.get $a) (local.get $b)))
    (if (i64.lt_s
          (i64.and (i64.xor (local.get $a) (local.get $r))
                   (i64.xor (local.get $b) (local.get $r)))
          (i64.const 0))
      (then unreachable))
    (local.get $r))"""

    def _helper_int_sub(self) -> str:
        # Signed overflow iff the operands differ in sign and the result's sign
        # is not the minuend's.
        return """  (func $int_sub (param $a i64) (param $b i64) (result i64)
    (local $r i64)
    (local.set $r (i64.sub (local.get $a) (local.get $b)))
    (if (i64.lt_s
          (i64.and (i64.xor (local.get $a) (local.get $b))
                   (i64.xor (local.get $a) (local.get $r)))
          (i64.const 0))
      (then unreachable))
    (local.get $r))"""

    def _helper_int_mul(self) -> str:
        # Zero never overflows; otherwise the wrapped product must divide back
        # to the other operand exactly. The one case that does not reach the
        # comparison is `Int.MIN * -1`, where the division itself traps — which
        # is the same answer, since that product overflows too.
        return """  (func $int_mul (param $a i64) (param $b i64) (result i64)
    (local $r i64)
    (local.set $r (i64.mul (local.get $a) (local.get $b)))
    (if (i64.ne (local.get $a) (i64.const 0))
      (then
        (if (i64.ne (i64.div_s (local.get $r) (local.get $a)) (local.get $b))
          (then unreachable))))
    (local.get $r))"""

    def _helper_int32_narrow(self) -> str:
        # The checked Int -> Int32 narrowing, and the range gate the i32
        # arithmetic helpers below reuse. An i64 outside [-2^31, 2^31-1] traps
        # (`unreachable`) exactly as an Int32 add/sub/mul overflow does; a
        # wasm trap carries no payload, so `revl: Int32 overflow` is not
        # attached to it, the same limit the i64 helpers have.
        return """  (func $int32_narrow (param $v i64) (result i32)
    (if (i32.or
          (i64.lt_s (local.get $v) (i64.const -2147483648))
          (i64.gt_s (local.get $v) (i64.const 2147483647)))
      (then unreachable))
    (i32.wrap_i64 (local.get $v)))"""

    def _helper_int32_add(self) -> str:
        # Two i32s cannot overflow an i64 sum, so compute wide and re-impose the
        # 32-bit bound — the same shape go/ts use (docs/arithmetic.md).
        return """  (func $int32_add (param $a i32) (param $b i32) (result i32)
    (call $int32_narrow
      (i64.add (i64.extend_i32_s (local.get $a))
               (i64.extend_i32_s (local.get $b)))))"""

    def _helper_int32_sub(self) -> str:
        return """  (func $int32_sub (param $a i32) (param $b i32) (result i32)
    (call $int32_narrow
      (i64.sub (i64.extend_i32_s (local.get $a))
               (i64.extend_i32_s (local.get $b)))))"""

    def _helper_int32_mul(self) -> str:
        return """  (func $int32_mul (param $a i32) (param $b i32) (result i32)
    (call $int32_narrow
      (i64.mul (i64.extend_i32_s (local.get $a))
               (i64.extend_i32_s (local.get $b)))))"""

    def _helper_int_div_floor(self) -> str:
        # wasm i64.div_s truncates; step the quotient down when the operands
        # have opposite signs and the division was inexact.
        return """  (func $int_div_floor (param $a i64) (param $b i64) (result i64)
    (local $q i64)
    (local.set $q (i64.div_s (local.get $a) (local.get $b)))
    (if (result i64)
      (i32.and
        (i64.ne (i64.rem_s (local.get $a) (local.get $b)) (i64.const 0))
        (i32.ne (i64.lt_s (local.get $a) (i64.const 0))
                (i64.lt_s (local.get $b) (i64.const 0))))
      (then (i64.sub (local.get $q) (i64.const 1)))
      (else (local.get $q))))"""

    def _helper_int_div_euclid(self) -> str:
        return """  (func $int_div_euclid (param $a i64) (param $b i64) (result i64)
    (if (result i64) (i64.gt_s (local.get $b) (i64.const 0))
      (then (call $int_div_floor (local.get $a) (local.get $b)))
      (else (i64.sub (i64.const 0)
              (call $int_div_floor (local.get $a)
                (i64.sub (i64.const 0) (local.get $b)))))))"""

    def _helper_int_mod(self) -> str:
        # Euclidean remainder: always in [0, |b|), for either sign of b.
        return """  (func $int_mod (param $a i64) (param $b i64) (result i64)
    (local $ab i64)
    (local $m i64)
    (local.set $ab
      (if (result i64) (i64.lt_s (local.get $b) (i64.const 0))
        (then (i64.sub (i64.const 0) (local.get $b)))
        (else (local.get $b))))
    (local.set $m (i64.rem_s (local.get $a) (local.get $ab)))
    (if (result i64) (i64.lt_s (local.get $m) (i64.const 0))
      (then (i64.add (local.get $m) (local.get $ab)))
      (else (local.get $m))))"""

    def _helper_str_slice(self) -> str:
        # `$s` is a string address (i32); `$start`/`$end` are `Int` *values*
        # (i64) and are narrowed once, here, into the byte offsets they index.
        return """  (func $str_slice (param $s i32) (param $start i64) (param $end i64) (result i32)
    (local $from i32)
    (local $len i32)
    (local $p i32)
    (local.set $from (i32.wrap_i64 (local.get $start)))
    (local.set $len (i32.sub (i32.wrap_i64 (local.get $end)) (local.get $from)))
    (local.set $p (call $alloc_str (local.get $len)))
    (memory.copy
      (i32.add (local.get $p) (i32.const 4))
      (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $from))
      (local.get $len))
    (local.get $p))"""

    def _helper_str_char_at(self) -> str:
        return """  (func $str_char_at (param $s i32) (param $idx i64) (result i32)
    (local $p i32)
    (local.set $p (call $alloc_str (i32.const 1)))
    (i32.store8
      (i32.add (local.get $p) (i32.const 4))
      (i32.load8_u
        (i32.add (i32.add (local.get $s) (i32.const 4))
                 (i32.wrap_i64 (local.get $idx)))))
    (local.get $p))"""

    def _helper_str_char_code_at(self) -> str:
        # in: an address and an Int index. out: an Int (a code point), so the
        # byte that comes back has to be widened rather than returned as an i32.
        # This is the *Bytes* form (one byte per index); Str routes through
        # $str_cp_char_code_at, which decodes UTF-8 (docs/strings.md).
        return """  (func $str_char_code_at (param $s i32) (param $idx i64) (result i64)
    (i64.extend_i32_u
      (i32.load8_u
        (i32.add (i32.add (local.get $s) (i32.const 4))
                 (i32.wrap_i64 (local.get $idx))))))"""

    # -- Str as code points (docs/strings.md) --------------------------------
    #
    # A `Str` is a sequence of Unicode scalar values. In memory it is UTF-8
    # (a u32 byte-length prefix, then the bytes), so `length`/`charAt`/
    # `charCodeAt`/`slice` count and index in *code points*, walking UTF-8
    # continuation bytes rather than raw byte offsets. `Bytes` keeps the byte
    # helpers above. A code point boundary is any byte whose top two bits are
    # not `10` (i.e. `(b & 0xC0) != 0x80`).

    def _helper_str_cp_length(self) -> str:
        return """  (func $str_cp_length (param $s i32) (result i32)
    (local $len i32) (local $i i32) (local $count i32) (local $b i32)
    (local.set $len (i32.load (local.get $s)))
    (block $done
      (loop $loop
        (br_if $done (i32.ge_u (local.get $i) (local.get $len)))
        (local.set $b (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $i))))
        (if (i32.ne (i32.and (local.get $b) (i32.const 0xC0)) (i32.const 0x80))
          (then (local.set $count (i32.add (local.get $count) (i32.const 1)))))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $loop)))
    (local.get $count))"""

    def _helper_str_cp_offset(self) -> str:
        # Byte offset of the `$cp`-th code point (or the byte length when `$cp`
        # is at/after the end), so a code-point index becomes a byte index.
        return """  (func $str_cp_offset (param $s i32) (param $cp i32) (result i32)
    (local $len i32) (local $i i32) (local $seen i32) (local $b i32)
    (local.set $len (i32.load (local.get $s)))
    (block $done
      (loop $loop
        (br_if $done (i32.ge_u (local.get $i) (local.get $len)))
        (br_if $done (i32.ge_s (local.get $seen) (local.get $cp)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (block $cont_done
          (loop $cont
            (br_if $cont_done (i32.ge_u (local.get $i) (local.get $len)))
            (local.set $b (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $i))))
            (br_if $cont_done (i32.ne (i32.and (local.get $b) (i32.const 0xC0)) (i32.const 0x80)))
            (local.set $i (i32.add (local.get $i) (i32.const 1)))
            (br $cont)))
        (local.set $seen (i32.add (local.get $seen) (i32.const 1)))
        (br $loop)))
    (local.get $i))"""

    def _helper_str_cp_slice(self) -> str:
        # `$start`/`$end` are code-point indices (Int values). JS slice
        # semantics: out-of-range bounds clamp into [0, cp_length], never trap.
        return """  (func $str_cp_slice (param $s i32) (param $start i64) (param $end i64) (result i32)
    (local $cplen i32) (local $a i32) (local $b i32) (local $from i32) (local $to i32) (local $len i32) (local $p i32)
    (local.set $cplen (call $str_cp_length (local.get $s)))
    (local.set $a (i32.wrap_i64 (local.get $start)))
    (local.set $b (i32.wrap_i64 (local.get $end)))
    (if (i32.lt_s (local.get $a) (i32.const 0)) (then (local.set $a (i32.const 0))))
    (if (i32.gt_s (local.get $a) (local.get $cplen)) (then (local.set $a (local.get $cplen))))
    (if (i32.lt_s (local.get $b) (local.get $a)) (then (local.set $b (local.get $a))))
    (if (i32.gt_s (local.get $b) (local.get $cplen)) (then (local.set $b (local.get $cplen))))
    (local.set $from (call $str_cp_offset (local.get $s) (local.get $a)))
    (local.set $to (call $str_cp_offset (local.get $s) (local.get $b)))
    (local.set $len (i32.sub (local.get $to) (local.get $from)))
    (local.set $p (call $alloc_str (local.get $len)))
    (memory.copy
      (i32.add (local.get $p) (i32.const 4))
      (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $from))
      (local.get $len))
    (local.get $p))"""

    def _helper_str_cp_char_at(self) -> str:
        # The whole scalar at code-point index `$idx`, as a new one-code-point
        # Str (its UTF-8 bytes copied out), not a single byte.
        return """  (func $str_cp_char_at (param $s i32) (param $idx i64) (result i32)
    (local $i i32) (local $from i32) (local $to i32) (local $len i32) (local $p i32)
    (local.set $i (i32.wrap_i64 (local.get $idx)))
    (local.set $from (call $str_cp_offset (local.get $s) (local.get $i)))
    (local.set $to (call $str_cp_offset (local.get $s) (i32.add (local.get $i) (i32.const 1))))
    (local.set $len (i32.sub (local.get $to) (local.get $from)))
    (local.set $p (call $alloc_str (local.get $len)))
    (memory.copy
      (i32.add (local.get $p) (i32.const 4))
      (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $from))
      (local.get $len))
    (local.get $p))"""

    def _helper_str_cp_char_code_at(self) -> str:
        # The Unicode scalar value at code-point index `$idx`: a UTF-8 decode
        # (1–4 bytes) rather than a raw byte read, so an astral char returns
        # e.g. 128512, not the lead byte.
        return """  (func $str_cp_char_code_at (param $s i32) (param $idx i64) (result i64)
    (local $off i32) (local $base i32) (local $b0 i32)
    (local.set $off (call $str_cp_offset (local.get $s) (i32.wrap_i64 (local.get $idx))))
    (local.set $base (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $off)))
    (local.set $b0 (i32.load8_u (local.get $base)))
    (i64.extend_i32_u
      (if (result i32) (i32.lt_u (local.get $b0) (i32.const 0x80))
        (then (local.get $b0))
        (else
          (if (result i32) (i32.lt_u (local.get $b0) (i32.const 0xE0))
            (then
              (i32.or
                (i32.shl (i32.and (local.get $b0) (i32.const 0x1F)) (i32.const 6))
                (i32.and (i32.load8_u (i32.add (local.get $base) (i32.const 1))) (i32.const 0x3F))))
            (else
              (if (result i32) (i32.lt_u (local.get $b0) (i32.const 0xF0))
                (then
                  (i32.or
                    (i32.or
                      (i32.shl (i32.and (local.get $b0) (i32.const 0x0F)) (i32.const 12))
                      (i32.shl (i32.and (i32.load8_u (i32.add (local.get $base) (i32.const 1))) (i32.const 0x3F)) (i32.const 6)))
                    (i32.and (i32.load8_u (i32.add (local.get $base) (i32.const 2))) (i32.const 0x3F))))
                (else
                  (i32.or
                    (i32.or
                      (i32.shl (i32.and (local.get $b0) (i32.const 0x07)) (i32.const 18))
                      (i32.shl (i32.and (i32.load8_u (i32.add (local.get $base) (i32.const 1))) (i32.const 0x3F)) (i32.const 12)))
                    (i32.or
                      (i32.shl (i32.and (i32.load8_u (i32.add (local.get $base) (i32.const 2))) (i32.const 0x3F)) (i32.const 6))
                      (i32.and (i32.load8_u (i32.add (local.get $base) (i32.const 3))) (i32.const 0x3F))))))))))))"""

    # -- lists: [u32 count][pad][slot0][slot1]… , one 8-byte slot per element --
    #
    # The count stays a u32 at offset 0 (the canonical-ABI shape a host reads);
    # the four bytes after it are padding, so the first element lands 8-aligned
    # and every element is at `8 + 8*i`. These helpers never learn the element
    # type — that is exactly why the slot width is uniform and why they move
    # elements as i64.

    def _helper_str_starts_with(self) -> str:
        # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith).
        # Both strings are canonical-ABI [u32 byte_len][bytes]; a code-point
        # prefix of a valid UTF-8 string is exactly a byte prefix, so a byte
        # comparison is the exact semantics. Empty prefix -> true.
        return """  (func $str_starts_with (param $s i32) (param $p i32) (result i32)
    (local $ls i32)
    (local $lp i32)
    (local $i i32)
    (local $ok i32)
    (local.set $ls (i32.load (local.get $s)))
    (local.set $lp (i32.load (local.get $p)))
    (if (i32.gt_u (local.get $lp) (local.get $ls))
      (then (return (i32.const 0))))
    (local.set $ok (i32.const 1))
    (block $done
      (loop $cmp
        (br_if $done (i32.ge_u (local.get $i) (local.get $lp)))
        (if (i32.ne
              (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $i)))
              (i32.load8_u (i32.add (i32.add (local.get $p) (i32.const 4)) (local.get $i))))
          (then (local.set $ok (i32.const 0)) (br $done)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $cmp)))
    (local.get $ok))"""

    def _helper_str_ends_with(self) -> str:
        return """  (func $str_ends_with (param $s i32) (param $p i32) (result i32)
    (local $ls i32)
    (local $lp i32)
    (local $i i32)
    (local $j i32)
    (local $ok i32)
    (local.set $ls (i32.load (local.get $s)))
    (local.set $lp (i32.load (local.get $p)))
    (if (i32.gt_u (local.get $lp) (local.get $ls))
      (then (return (i32.const 0))))
    (local.set $ok (i32.const 1))
    (local.set $i (i32.sub (local.get $ls) (local.get $lp)))
    (block $done
      (loop $cmp
        (br_if $done (i32.ge_u (local.get $i) (local.get $ls)))
        (local.set $j (i32.sub (local.get $i) (i32.sub (local.get $ls) (local.get $lp))))
        (if (i32.ne
              (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $i)))
              (i32.load8_u (i32.add (i32.add (local.get $p) (i32.const 4)) (local.get $j))))
          (then (local.set $ok (i32.const 0)) (br $done)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $cmp)))
    (local.get $ok))"""

    def _helper_str_to_int(self) -> str:
        # Str.to_int (FR-9, docs/stdlib-2.0.md §Str.to_int): total on the ASCII
        # digits with an optional leading `-`, Opt None (tag 0) otherwise. The
        # magnitude is accumulated as a wrapping i64 under unsigned guards:
        # each step rejects `n > (lim - d) / 10` (unsigned), where lim is
        # Int.MAX+1 for negatives (so `-9223372036854775808` — Int.MIN, whose
        # magnitude is 2^63 — is the ONE out-of-|MAX| magnitude that parses)
        # and Int.MAX for positives. The result is the tier's Opt[Int] tagged
        # cell: [u32 tag][pad][i64 payload], tag 1 = Some, 0 = None.
        return """  (func $str_to_int (param $s i32) (result i32)
    (local $len i32)
    (local $i i32)
    (local $neg i32)
    (local $b i32)
    (local $n i64)
    (local $d i64)
    (local $lim i64)
    (local $cell i32)
    (local.set $len (i32.load (local.get $s)))
    (local.set $i (i32.const 0))
    (local.set $neg (i32.const 0))
    (if (i32.and (i32.gt_u (local.get $len) (i32.const 0))
                 (i32.eq (i32.load8_u (i32.add (local.get $s) (i32.const 4))) (i32.const 45)))
      (then
        (local.set $neg (i32.const 1))
        (local.set $i (i32.const 1))))
    (local.set $n (i64.const 0))
    (local.set $lim (select
      (i64.const 0x8000000000000000)
      (i64.const 0x7fffffffffffffff)
      (local.get $neg)))
    (block $result
      (block $fail
        (if (i32.eq (local.get $i) (local.get $len))
          (then (br $fail)))
        (block $digits_done
          (loop $digits
            ;; normal exit: every byte consumed
            (br_if $digits_done (i32.ge_u (local.get $i) (local.get $len)))
            (local.set $b (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $i))))
            (if (i32.or (i32.lt_u (local.get $b) (i32.const 48))
                        (i32.gt_u (local.get $b) (i32.const 57)))
              (then (br $fail)))
            (local.set $d (i64.extend_i32_u (i32.sub (local.get $b) (i32.const 48))))
            (if (i64.gt_u (local.get $n)
                          (i64.div_u (i64.sub (local.get $lim) (local.get $d)) (i64.const 10)))
              (then (br $fail)))
            (local.set $n (i64.add (i64.mul (local.get $n) (i64.const 10)) (local.get $d)))
            (local.set $i (i32.add (local.get $i) (i32.const 1)))
            (br $digits)))
        ;; Some: tag 1, payload = -n (n may be 2^63, i.e. Int.MIN) or n
        (local.set $cell (call $alloc (i32.const 16)))
        (i32.store (local.get $cell) (i32.const 1))
        (if (local.get $neg)
          (then
            (if (i64.eq (local.get $n) (i64.const 0x8000000000000000))
              (then (i64.store (i32.add (local.get $cell) (i32.const 8)) (i64.const 0x8000000000000000)))
              (else (i64.store (i32.add (local.get $cell) (i32.const 8)) (i64.sub (i64.const 0) (local.get $n))))))
          (else (i64.store (i32.add (local.get $cell) (i32.const 8)) (local.get $n))))
        (br $result))
      ;; None: tag 0, zeroed payload
      (local.set $cell (call $alloc (i32.const 16)))
      (i32.store (local.get $cell) (i32.const 0))
      (i64.store (i32.add (local.get $cell) (i32.const 8)) (i64.const 0)))
    (local.get $cell))"""

    def _helper_list_push(self) -> str:
        return """  (func $list_push (param $list i32) (param $elem i64) (result i32)
    (local $n i32)
    (local $p i32)
    (local.set $n (i32.load (local.get $list)))
    (local.set $p
      (call $alloc
        (i32.add
          (i32.mul (i32.add (local.get $n) (i32.const 1)) (i32.const 8))
          (i32.const 8))))
    (i32.store (local.get $p) (i32.add (local.get $n) (i32.const 1)))
    (memory.copy
      (i32.add (local.get $p) (i32.const 8))
      (i32.add (local.get $list) (i32.const 8))
      (i32.mul (local.get $n) (i32.const 8)))
    (i64.store
      (i32.add
        (i32.add (local.get $p) (i32.const 8))
        (i32.mul (local.get $n) (i32.const 8)))
      (local.get $elem))
    (local.get $p))"""

    def _helper_list_concat(self) -> str:
        return """  (func $list_concat (param $a i32) (param $b i32) (result i32)
    (local $na i32)
    (local $nb i32)
    (local $p i32)
    (local.set $na (i32.load (local.get $a)))
    (local.set $nb (i32.load (local.get $b)))
    (local.set $p
      (call $alloc
        (i32.add
          (i32.mul (i32.add (local.get $na) (local.get $nb)) (i32.const 8))
          (i32.const 8))))
    (i32.store (local.get $p) (i32.add (local.get $na) (local.get $nb)))
    (memory.copy
      (i32.add (local.get $p) (i32.const 8))
      (i32.add (local.get $a) (i32.const 8))
      (i32.mul (local.get $na) (i32.const 8)))
    (memory.copy
      (i32.add
        (i32.add (local.get $p) (i32.const 8))
        (i32.mul (local.get $na) (i32.const 8)))
      (i32.add (local.get $b) (i32.const 8))
      (i32.mul (local.get $nb) (i32.const 8)))
    (local.get $p))"""

    def _helper_list_slice(self) -> str:
        # `$start`/`$end` are `Int` values; the element stride is bytes.
        return """  (func $list_slice (param $s i32) (param $start i64) (param $end i64) (result i32)
    (local $from i32)
    (local $len i32)
    (local $p i32)
    (local.set $from (i32.wrap_i64 (local.get $start)))
    (local.set $len (i32.sub (i32.wrap_i64 (local.get $end)) (local.get $from)))
    (local.set $p
      (call $alloc
        (i32.add (i32.mul (local.get $len) (i32.const 8)) (i32.const 8))))
    (i32.store (local.get $p) (local.get $len))
    (memory.copy
      (i32.add (local.get $p) (i32.const 8))
      (i32.add
        (i32.add (local.get $s) (i32.const 8))
        (i32.mul (local.get $from) (i32.const 8)))
      (i32.mul (local.get $len) (i32.const 8)))
    (local.get $p))"""

    # -- the reader builtins: split / join / indexOf (docs/stdlib-2.0.md) -----
    #
    # These three close the gap between "the Str boundary works" (concat-built
    # writers cross to wasm) and "the reader/parser artifacts cross too". Each
    # is expressed over the same linear-memory Str/List ABI as the helpers
    # above — a `Str` is `[u32 byte_len][utf8 bytes]`, a `List` is
    # `[u32 count][pad][i64 slot]…` with a `List[Str]` slot holding a str
    # address widened to i64. Their semantics mirror the reference tiers
    # (py/ts): index/count in *code points*, JS-shape split (trailing empties
    # kept, `""` → per-code-point pieces), and `List[Str].join(sep)`.

    def _helper_str_index_of(self) -> str:
        # Str.indexOf(needle): a byte substring scan; on a hit, the byte offset
        # is converted to the *code-point* index (the reference tiers return a
        # code-point index — py `str.find`, ts `Array.from(x.slice(0,at)).length`).
        # Empty needle → 0 (matches "".find("")/indexOf("")); absent → -1. The
        # result is an i32 that the call site sign-extends to the tier's Int.
        return """  (func $str_index_of (param $s i32) (param $needle i32) (result i32)
    (local $ls i32) (local $ln i32) (local $i i32) (local $j i32) (local $ok i32)
    (local $cp i32) (local $b i32)
    (local.set $ls (i32.load (local.get $s)))
    (local.set $ln (i32.load (local.get $needle)))
    (if (i32.eqz (local.get $ln)) (then (return (i32.const 0))))
    (local.set $i (i32.const 0))
    (block $notfound
      (loop $scan
        (br_if $notfound
          (i32.gt_u (i32.add (local.get $i) (local.get $ln)) (local.get $ls)))
        (local.set $ok (i32.const 1))
        (local.set $j (i32.const 0))
        (block $cmpdone
          (loop $cmp
            (br_if $cmpdone (i32.ge_u (local.get $j) (local.get $ln)))
            (if (i32.ne
                  (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4))
                                        (i32.add (local.get $i) (local.get $j))))
                  (i32.load8_u (i32.add (i32.add (local.get $needle) (i32.const 4)) (local.get $j))))
              (then (local.set $ok (i32.const 0)) (br $cmpdone)))
            (local.set $j (i32.add (local.get $j) (i32.const 1)))
            (br $cmp)))
        (if (local.get $ok)
          (then
            (local.set $cp (i32.const 0))
            (local.set $j (i32.const 0))
            (block $cpdone
              (loop $cploop
                (br_if $cpdone (i32.ge_u (local.get $j) (local.get $i)))
                (local.set $b (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $j))))
                (if (i32.ne (i32.and (local.get $b) (i32.const 0xC0)) (i32.const 0x80))
                  (then (local.set $cp (i32.add (local.get $cp) (i32.const 1)))))
                (local.set $j (i32.add (local.get $j) (i32.const 1)))
                (br $cploop)))
            (return (local.get $cp))))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $scan)))
    (i32.const -1))"""

    def _helper_str_split(self) -> str:
        # Str.split(sep) → List[Str]. JS-shape (docs/stdlib-2.0.md §split):
        # trailing empties are kept ("a,".split(",") → ["a",""]) and the
        # empty-string result is one empty piece (""→[""]). An empty separator
        # splits into per-code-point pieces (py `list(v)`), so ""→[] there.
        # Each piece is a freshly allocated Str; pieces are appended through
        # `$list_push`, so the result is a standard `[count][pad][slot]…` list
        # of str addresses.
        return """  (func $str_split (param $s i32) (param $sep i32) (result i32)
    (local $ls i32) (local $lsep i32) (local $list i32) (local $start i32)
    (local $i i32) (local $j i32) (local $ok i32) (local $seg i32) (local $seglen i32) (local $b i32)
    (local.set $ls (i32.load (local.get $s)))
    (local.set $lsep (i32.load (local.get $sep)))
    (local.set $list (call $alloc (i32.const 8)))
    (i32.store (local.get $list) (i32.const 0))
    (if (i32.eqz (local.get $lsep))
      (then
        (local.set $i (i32.const 0))
        (block $cpdone
          (loop $cploop
            (br_if $cpdone (i32.ge_u (local.get $i) (local.get $ls)))
            (local.set $j (i32.add (local.get $i) (i32.const 1)))
            (block $skipdone
              (loop $skip
                (br_if $skipdone (i32.ge_u (local.get $j) (local.get $ls)))
                (local.set $b (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $j))))
                (br_if $skipdone (i32.ne (i32.and (local.get $b) (i32.const 0xC0)) (i32.const 0x80)))
                (local.set $j (i32.add (local.get $j) (i32.const 1)))
                (br $skip)))
            (local.set $seglen (i32.sub (local.get $j) (local.get $i)))
            (local.set $seg (call $alloc_str (local.get $seglen)))
            (memory.copy (i32.add (local.get $seg) (i32.const 4))
                         (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $i))
                         (local.get $seglen))
            (local.set $list (call $list_push (local.get $list) (i64.extend_i32_u (local.get $seg))))
            (local.set $i (local.get $j))
            (br $cploop)))
        (return (local.get $list))))
    (local.set $start (i32.const 0))
    (local.set $i (i32.const 0))
    (block $done
      (loop $scan
        (br_if $done (i32.gt_u (i32.add (local.get $i) (local.get $lsep)) (local.get $ls)))
        (local.set $ok (i32.const 1))
        (local.set $j (i32.const 0))
        (block $cmpdone
          (loop $cmp
            (br_if $cmpdone (i32.ge_u (local.get $j) (local.get $lsep)))
            (if (i32.ne
                  (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4))
                                        (i32.add (local.get $i) (local.get $j))))
                  (i32.load8_u (i32.add (i32.add (local.get $sep) (i32.const 4)) (local.get $j))))
              (then (local.set $ok (i32.const 0)) (br $cmpdone)))
            (local.set $j (i32.add (local.get $j) (i32.const 1)))
            (br $cmp)))
        (if (local.get $ok)
          (then
            (local.set $seglen (i32.sub (local.get $i) (local.get $start)))
            (local.set $seg (call $alloc_str (local.get $seglen)))
            (memory.copy (i32.add (local.get $seg) (i32.const 4))
                         (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $start))
                         (local.get $seglen))
            (local.set $list (call $list_push (local.get $list) (i64.extend_i32_u (local.get $seg))))
            (local.set $i (i32.add (local.get $i) (local.get $lsep)))
            (local.set $start (local.get $i))
            (br $scan)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $scan)))
    (local.set $seglen (i32.sub (local.get $ls) (local.get $start)))
    (local.set $seg (call $alloc_str (local.get $seglen)))
    (memory.copy (i32.add (local.get $seg) (i32.const 4))
                 (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $start))
                 (local.get $seglen))
    (local.set $list (call $list_push (local.get $list) (i64.extend_i32_u (local.get $seg))))
    (local.get $list))"""

    def _helper_str_join(self) -> str:
        # List[Str].join(sep) → Str (receiver is the list, argument the
        # separator — the TS orientation the tier follows). Two passes: sum the
        # element byte lengths plus (n-1) separators, allocate the result once,
        # then copy each element with a separator before all but the first.
        # An empty list joins to "" (docs/stdlib-2.0.md §join).
        return """  (func $str_join (param $list i32) (param $sep i32) (result i32)
    (local $n i32) (local $lsep i32) (local $i i32) (local $total i32)
    (local $elem i32) (local $elen i32) (local $out i32) (local $cur i32)
    (local.set $n (i32.load (local.get $list)))
    (local.set $lsep (i32.load (local.get $sep)))
    (if (i32.eqz (local.get $n))
      (then (return (call $alloc_str (i32.const 0)))))
    (local.set $total (i32.mul (i32.sub (local.get $n) (i32.const 1)) (local.get $lsep)))
    (local.set $i (i32.const 0))
    (block $sumdone
      (loop $sum
        (br_if $sumdone (i32.ge_u (local.get $i) (local.get $n)))
        (local.set $elem (i32.wrap_i64
          (i64.load (i32.add (i32.add (local.get $list) (i32.const 8))
                             (i32.mul (local.get $i) (i32.const 8))))))
        (local.set $total (i32.add (local.get $total) (i32.load (local.get $elem))))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $sum)))
    (local.set $out (call $alloc_str (local.get $total)))
    (local.set $cur (i32.add (local.get $out) (i32.const 4)))
    (local.set $i (i32.const 0))
    (block $wdone
      (loop $write
        (br_if $wdone (i32.ge_u (local.get $i) (local.get $n)))
        (if (i32.gt_u (local.get $i) (i32.const 0))
          (then
            (memory.copy (local.get $cur) (i32.add (local.get $sep) (i32.const 4)) (local.get $lsep))
            (local.set $cur (i32.add (local.get $cur) (local.get $lsep)))))
        (local.set $elem (i32.wrap_i64
          (i64.load (i32.add (i32.add (local.get $list) (i32.const 8))
                             (i32.mul (local.get $i) (i32.const 8))))))
        (local.set $elen (i32.load (local.get $elem)))
        (memory.copy (local.get $cur) (i32.add (local.get $elem) (i32.const 4)) (local.get $elen))
        (local.set $cur (i32.add (local.get $cur) (local.get $elen)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $write)))
    (local.get $out))"""



    # -- type inference -------------------------------------------------------

    def _infer_type(self, node: Any, scope: _Scope, expected: str | None = None) -> str | None:
        if not isinstance(node, dict) or "kind" not in node:
            raise EmitError("malformed v3 expression")
        if node.get("widen") == "Int":
            return "Int"  # Int32 widened to Int (docs/arithmetic.md)
        kind = node["kind"]
        if kind == "lit":
            value = node.get("value")
            if isinstance(value, bool):
                return "Bool"
            if isinstance(value, int) and not isinstance(value, bool):
                return "Int"
            if isinstance(value, str):
                return "Str"
            if isinstance(value, float):
                return "Float"
            raise EmitError(f"literal {value!r} is not lowerable on this tier")
        if kind == "var":
            name = node.get("name")
            if name == "None":  # built-in Opt None
                if expected and self._tagged_layout(expected) is not None:
                    return expected
                return "Opt[Int]"
            _ident(name, "name")
            if name not in scope.types:
                raise EmitError(f"unbound name {name!r}")
            return scope.types[name]
        if kind == "__wat":
            # a component-path expression already lowered to WAT (a `req` call,
            # config/host access); it is opaque here but carries its type
            return node.get("ty")
        if kind == "bin":
            op = node.get("op")
            if op == "??":
                return self._nullish_type(node, scope)
            left = self._infer_type(node.get("left"), scope)
            right = self._infer_type(node.get("right"), scope)
            if op in ("==", "===", "!=", "!==", "<", ">", "<=", ">=", "&&", "||"):
                return "Bool"
            if op == "+" and left == "Str" and right == "Str":
                return "Str"
            if op == "+" and _is_list_type(left) and left == right:
                return left
            if node.get("operands") == "Float" and op in ("+", "-", "*", "/"):
                # Float arithmetic on this tier lowers to f64 ops only so the
                # result can be rendered (docs/strings.md); the value never
                # enters the storage ABI.
                return "Float"
            if node.get("operands") == "Int32":
                return "Int32"  # Int32 arithmetic stays Int32 (docs/arithmetic.md)
            return "Int"
        if kind == "un":
            op = node.get("op")
            if op == "!":
                return "Bool"
            if op == "-":
                return "Int32" if node.get("operands") == "Int32" else "Int"
            raise EmitError(f"unsupported unary operator {op!r}")
        if kind == "call":
            return self._call_type(node, scope)
        if kind == "builtin":
            return self._builtin_type(node, scope)
        if kind == "field":
            target_ty = self._infer_type(node.get("target"), scope)
            fields = self._record_fields(target_ty)
            if fields is None:
                raise EmitError(f"field access on non-record type {target_ty!r}")
            name = _ident(node.get("name"), "field name")
            if name not in fields:
                raise EmitError(f"record {target_ty!r} has no field {name!r}")
            return fields[name]
        if kind == "index":
            target_ty = self._infer_type(node.get("target"), scope)
            if target_ty == "Str":
                return "Str"
            if _is_list_type(target_ty):
                return _list_elem(target_ty)
            raise EmitError(f"indexing is only lowerable for Str and List, got {target_ty!r}")
        if kind == "len":
            return "Int"
        if kind == "if":
            then_ty = self._infer_type(node.get("then"), scope, expected)
            else_ty = self._infer_type(node.get("else"), scope, expected)
            if then_ty != else_ty:
                raise EmitError("if branches must have the same type on this tier")
            return then_ty
        if kind == "record":
            return self._record_type(node, scope, expected)
        if kind == "list":
            return self._list_type(node, scope, expected)
        if kind == "interp":
            return "Str"
        if kind == "adt":
            case = node.get("case")
            if expected and self._tagged_layout(expected) is not None \
                    and any(c == case for c, _ in self._tagged_layout(expected)):
                return expected
            return node.get("type")
        if kind == "match":
            # The arm body may reference the arm's payload binding, so infer it
            # in a scope that *knows* that binding's type — exactly as
            # `_match_expr.arm_scope` does when lowering. Without this, a match
            # with a payload bind used as an operand (`s + match x { J(v) => v,
            # … }`, common inside a loop) raised "unbound name" during type
            # inference even though it lowers fine.
            layout = self._tagged_layout(self._infer_type(node.get("scrutinee"), scope))
            for arm in node.get("arms") or []:
                arm_scope = scope
                bind = arm.get("bind")
                if bind:
                    payload_ty = arm.get("payload_type")
                    if payload_ty in (None, "Any") and layout is not None:
                        payload_ty = next(
                            (p for c, p in layout if c == arm.get("pattern")), None)
                    if payload_ty == "Any":
                        payload_ty = None
                    arm_scope = _Scope(dict(scope.slots), dict(scope.types))
                    arm_scope.types[bind] = payload_ty or "Int"
                return self._infer_type(arm.get("body"), arm_scope, expected)
            return expected
        if kind == "arrow":
            raise EmitError("arrow values are not lowerable on this tier — the wasm module has no closures")
        if kind == "maplit":
            raise EmitError(
                "the Map value type is not lowerable on this tier yet — "
                "no representation here; use a hosted backend")
        if kind in ("optfield", "optcall"):
            raise EmitError(
                f"optional chaining (`?.`) is not yet lowerable on the wasm tier "
                f"({kind!r}) — unwrap with `match` or `??` for now")
        if kind == "record_update":
            raise EmitError(
                "functional record update `{r | f = e}` is not emitted by the "
                "wasm backend yet (implemented tiers: python, typescript) — see "
                "docs/records.md §6; lift it into a helper fn instead")
        raise EmitError(f"unsupported v3 expression kind {kind!r}")

    def _arrow_callee(self, callee: dict):
        """The arrow node being called, if this callee is an arrow value:
        a local `let f = x => …` (inlined) or an inline `(x => …)(…)`."""
        if callee.get("kind") == "arrow":
            return callee
        if callee.get("kind") == "var":
            return self._arrows.get(callee.get("name"))
        return None

    def _call_type(self, node: dict, scope: _Scope) -> str | None:
        callee = node.get("callee") or {}
        arrow = self._arrow_callee(callee)
        if arrow is not None:
            inner = _Scope(dict(scope.slots), dict(scope.types))
            for p, a in zip(arrow.get("params") or [], node.get("args") or []):
                inner.types[p] = self._infer_type(a, scope)
            return self._infer_type(arrow.get("body"), inner)
        if callee.get("kind") != "var":
            raise EmitError("only direct function calls are lowerable on this tier")
        name = _ident(callee.get("name"), "callee")
        if name == "Some":  # built-in Opt Some(x)
            args = node.get("args") or []
            inner = self._infer_type(args[0], scope) if args else "Int"
            return f"Opt[{inner or 'Int'}]"
        sig = self.fn_sigs.get(name)
        if sig is None:
            raise EmitError(f"callee {name!r} is not a lowerable function")
        return sig["returns"]


    def _builtin_type(self, node: dict, scope: _Scope) -> str | None:
        method = node.get("method")
        target_ty = self._infer_type(node.get("target"), scope)
        if method == "length":
            return "Int"
        # Int/Int32 width conversions (docs/arithmetic.md). The Str form
        # (parse) is handled below.
        if method == "to_int" and target_ty == "Int32":
            return "Int"
        if method == "to_int32":
            return "Int32"
        if method == "push":
            if not _is_list_type(target_ty):
                raise EmitError("push is only lowerable on List values")
            return target_ty
        if method == "concat":
            if target_ty not in ("Str", "Bytes") and not _is_list_type(target_ty):
                raise EmitError("concat is only lowerable on Str/Bytes/List values")
            return target_ty
        if method == "slice":
            if target_ty not in ("Str", "Bytes") and not _is_list_type(target_ty):
                raise EmitError("slice is only lowerable on Str/Bytes/List values")
            return target_ty
        if method == "charAt":
            if target_ty not in ("Str", "Bytes"):
                raise EmitError("charAt is only lowerable on Str/Bytes values")
            return "Str"
        if method == "charCodeAt":
            if target_ty not in ("Str", "Bytes"):
                raise EmitError("charCodeAt is only lowerable on Str/Bytes values")
            return "Int"
        if method == "to_str":
            if target_ty != "Int":
                raise EmitError("to_str is only lowerable on Int values")
            return "Str"
        # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith).
        if method == "startsWith" or method == "endsWith":
            if target_ty not in ("Str", "Bytes"):
                raise EmitError(
                    f"{method} is only lowerable on Str/Bytes values")
            return "Bool"
        # Str.to_int (FR-9): the Int32 widen above returns Int; the parse form
        # answers the tier's Opt[Int] tagged cell.
        if method == "to_int" and target_ty == "Str":
            return "Opt[Int]"
        if method == "indexOf":
            # The reader's search probe. Lowered over the linear-memory Str ABI
            # (a byte scan, code-point index out); List.indexOf needs a
            # per-element comparison the harness never reaches, so it stays a
            # named refusal.
            if target_ty in ("Str", "Bytes"):
                return "Int"
            raise EmitError(
                "indexOf is not lowerable on this tier yet for List — the "
                "element comparison has no representation here; use a hosted backend")
        if method == "split":
            # Str.split(sep) → List[Str] (docs/stdlib-2.0.md §split).
            if target_ty not in ("Str", "Bytes"):
                raise EmitError("split is only lowerable on Str values")
            return "List[Str]"
        if method == "join":
            # List[Str].join(sep) → Str; the receiver must be a List[Str].
            if not _is_list_type(target_ty) or _list_elem(target_ty) != "Str":
                raise EmitError("join is only lowerable on List[Str] values")
            return "Str"
        if method in ("set", "lookup", "has", "size", "keys", "remove"):
            raise EmitError(
                f"`{method}` is not lowerable on this tier yet — the Map value "
                f"type has no representation here; use a hosted backend")
        raise EmitError(f"unsupported builtin method {method!r}")

    def _record_type(self, node: dict, scope: _Scope, expected: str | None) -> str:
        fields: list[tuple[str, str]] = []
        for raw_name, raw_value in node.get("fields") or []:
            name = _ident(raw_name, "record field")
            ftype = self._infer_type(raw_value, scope)
            if ftype is None or ftype == "Unit":
                raise EmitError(f"record field {name!r} has void type")
            fields.append((name, ftype))
        if expected is not None and self._record_fields(expected) is not None:
            return expected
        field_names = {name for name, _ in fields}
        for name, spec in self.all_types.items():
            if spec.get("kind") == "record" and set(spec.get("fields") or {}) == field_names:
                return name
        return self._anon_record(fields)

    def _list_type(self, node: dict, scope: _Scope, expected: str | None) -> str:
        if expected is not None and _is_list_type(expected):
            return expected
        # An annotated `let`/`var x: List[T] = []` threads the declared type onto
        # the literal as `expected` (roadmap 107) — the author's own annotation,
        # and the only type an empty positional literal can carry. Same pin the
        # empty-Map case uses; read it when no surface type flowed in.
        if expected is None:
            pinned = node.get("expected")
            if _is_list_type(pinned):
                return pinned
        items = node.get("items") or []
        if not items:
            raise EmitError("an untyped empty list literal needs an expected List type")
        elem_ty = self._infer_type(items[0], scope)
        if elem_ty is None or elem_ty == "Unit":
            raise EmitError("list elements cannot be void")
        for item in items[1:]:
            if self._infer_type(item, scope) != elem_ty:
                raise EmitError("all list elements must have the same type on this tier")
        return f"List[{elem_ty}]"

    # -- expression lowering --------------------------------------------------

    def _expr(self, node: Any, scope: _Scope, where: str, expected: str | None = None) -> _E:
        if not isinstance(node, dict) or "kind" not in node:
            raise EmitError(f"{where}: malformed v3 expression {node!r}")
        # An Int32 -> Int widening site (docs/arithmetic.md): Int32 is an i32
        # and Int is an i64, so the lossless widening is a sign-extend, emitted
        # where the frontend marked it. (`widen: "Float"` never reaches this
        # tier — Float is refused by name.)
        if node.get("widen") == "Int":
            inner = {k: v for k, v in node.items() if k != "widen"}
            operand = self._expr(inner, scope, where, "Int32")
            return _E(f"(i64.extend_i32_s {operand.wat})", "Int")
        kind = node["kind"]
        if kind == "__wat":
            return _E(node.get("wat"), node.get("ty"))
        if kind == "lit":
            value = node.get("value")
            if isinstance(value, bool):
                return _E("(i32.const 1)" if value else "(i32.const 0)", "Bool")
            if isinstance(value, int) and not isinstance(value, bool):
                # an Int literal is a 64-bit *value*; only offsets and counts
                # stay i32.const
                return _E(f"(i64.const {value})", "Int")
            if isinstance(value, str):
                return _E(self._str_ptr(value), "Str")
            if isinstance(value, float):
                # A Float value on this tier exists only to be rendered: it is
                # an f64 on the wasm stack, produced here and consumed by
                # `$f64_to_str` in interpolation. It is never stored in an
                # i32/8-byte slot, so no Float ever reaches the value ABI —
                # storing/returning a Float still refuses by name below.
                return _E(f"(f64.const {_f64_literal(value)})", "Float")
            raise EmitError(f"{where}: literal {value!r} is not lowerable on this tier")
        if kind == "var":
            name = node.get("name")
            if name == "None":  # built-in Opt None
                ty = expected if self._tagged_layout(expected) is not None else "Opt[Int]"
                return self._make_tagged(ty, "None", None, scope, where)
            _ident(name, f"{where}: name")
            if name not in scope.slots:
                raise EmitError(f"{where}: unbound name {name!r}")
            return _E(scope.slots[name], scope.types[name])
        if kind == "bin":
            return self._bin_expr(node, scope, where)
        if kind == "un":
            return self._un_expr(node, scope, where)
        if kind == "call":
            return self._call_expr(node, scope, where, expected)
        if kind == "builtin":
            return self._builtin_expr(node, scope, where, expected)
        if kind == "field":
            return self._field_expr(node, scope, where)
        if kind == "index":
            return self._index_expr(node, scope, where)
        if kind == "len":
            return self._len_expr(node, scope, where)
        if kind == "if":
            return self._if_expr(node, scope, where, expected)
        if kind == "record":
            return self._record_expr(node, scope, where, expected)
        if kind == "list":
            return self._list_expr(node, scope, where, expected)
        if kind == "interp":
            return self._interp_expr(node, scope, where)
        if kind == "adt":
            return self._adt_expr(node, scope, where, expected)
        if kind == "match":
            return self._match_expr(node, scope, where, expected)
        if kind == "arrow":
            raise EmitError(f"{where}: arrow values are not lowerable on this tier")
        if kind == "maplit":
            raise EmitError(
                f"{where}: the Map value type is not lowerable on this tier yet — "
                f"no representation here; use a hosted backend")
        if kind == "record_update":
            raise EmitError(
                f"{where}: functional record update `{{r | f = e}}` is not "
                "emitted by the wasm backend yet (implemented tiers: python, "
                "typescript) — see docs/records.md §6; lift it into a helper fn instead")
        if kind in ("optfield", "optcall"):
            raise EmitError(
                f"{where}: optional chaining (`?.`) is not yet lowerable on the "
                f"wasm tier ({kind!r}) — unwrap with `match` or `??` for now")
        raise EmitError(f"{where}: unsupported v3 expression kind {kind!r}")

    def _opt_payload(self, ty: str | None) -> str | None:
        """The `Some` payload type of an `Opt[T]`, or None if `ty` is not Opt."""
        layout = self._tagged_layout(ty)
        if layout is None or [case for case, _p in layout] != ["None", "Some"]:
            return None
        return next(payload for case, payload in layout if case == "Some")

    def _nullish_type(self, node: dict, scope: _Scope) -> str | None:
        payload = self._opt_payload(self._infer_type(node.get("left"), scope))
        if payload is None:
            raise EmitError("`??` needs an Opt value on its left")
        return payload

    def _nullish_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        """`a ?? b` on the tagged-cell model: read the tag, take the payload.

        `Opt` is `[u32 tag][pad][slot payload]` with `None` = 0 and `Some` = 1,
        so nullish coalescing is one load and a branch. It is lowerable exactly
        when the Opt value is *in* the module; an `Opt` returned by a required
        service never gets here — the scalar coeffect boundary rejects it first.
        """
        left_node, right_node = node.get("left"), node.get("right")
        left_ty = self._infer_type(left_node, scope)
        payload_ty = self._opt_payload(left_ty)
        if payload_ty is None:
            raise EmitError(
                f"{where}: `??` needs an Opt value on its left, got {left_ty!r}"
            )
        # Hold the Opt cell in a scratch that a nested allocation on either
        # side (or an outer allocation this `??` is a sub-expression of) cannot
        # clobber.
        tmp = self._acquire_tmp()
        try:
            left = self._expr(left_node, scope, where, left_ty)
            right = self._expr(right_node, scope, where, payload_ty)
            if right.ty != payload_ty:
                raise EmitError(
                    f"{where}: the `??` default is {right.ty!r} but the Opt carries "
                    f"{payload_ty!r}"
                )
            payload = self._slot_load(
                f"(i32.add (local.get ${tmp}) (i32.const {_SLOT}))", payload_ty)
            wat = (
                f"{left.wat}\n      (local.set ${tmp})\n"
                f"      (if (result {_wasm_ty(payload_ty)})\n"
                f"        (i32.eq (i32.load (local.get ${tmp})) (i32.const 1))\n"
                f"        (then {payload})\n"
                f"        (else {right.wat}))"
            )
        finally:
            self._release_tmp()
        return _E(wat, payload_ty)

    def _bin_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        op = node.get("op")
        if op == "??":
            return self._nullish_expr(node, scope, where)
        left_node = node.get("left")
        right_node = node.get("right")
        left_ty = self._infer_type(left_node, scope)
        right_ty = self._infer_type(right_node, scope)
        if op in ("==", "===", "!=", "!==") and left_ty == "Str" and right_ty == "Str":
            left = self._expr(left_node, scope, where, "Str")
            right = self._expr(right_node, scope, where, "Str")
            wat = f"{left.wat}\n      {right.wat}\n      (call $str_eq)"
            if op in ("!=", "!=="):
                wat += "\n      (i32.eqz)"
            return _E(wat, "Bool")
        if op == "+" and left_ty == "Str" and right_ty == "Str":
            left = self._expr(left_node, scope, where, "Str")
            right = self._expr(right_node, scope, where, "Str")
            return _E(f"{left.wat}\n      {right.wat}\n      (call $str_concat)", "Str")
        if op == "+" and _is_list_type(left_ty) and left_ty == right_ty:
            left = self._expr(left_node, scope, where, left_ty)
            right = self._expr(right_node, scope, where, right_ty)
            return _E(f"{left.wat}\n      {right.wat}\n      (call $list_concat)", left_ty)
        if op in ("<", ">", "<=", ">=") and not (
                left_ty == right_ty and left_ty in ("Int", "Int32")):
            raise EmitError(f"{where}: relational operator {op!r} is only lowerable for Int/Int32")
        if op in ("&&", "||") and (left_ty != "Bool" or right_ty != "Bool"):
            raise EmitError(f"{where}: logical operator {op!r} is only lowerable for Bool")
        if op in ("==", "===", "!=", "!==") and (left_ty not in ("Int", "Int32", "Bool") or right_ty not in ("Int", "Int32", "Bool")):
            raise EmitError(f"{where}: equality on this tier is lowerable for Int, Int32, Bool, and Str")
        if op in ("==", "===", "!=", "!==") and left_ty != right_ty:
            # Int (i64), Int32 (i32) and Bool (i32) are distinct wasm types, and
            # no comparison instruction spans two widths — say so rather than
            # emit a module that does not validate. (The checker already forbids
            # mixing Int32 with Int, so a mixed comparison here is Int/Bool.)
            raise EmitError(
                f"{where}: cannot compare {left_ty!r} with {right_ty!r} on this "
                f"tier — Int is 64-bit and Int32/Bool are 32-bit")
        if op in ("+", "-", "*", "/") and node.get("operands") == "Float":
            # Float arithmetic is lowerable *only* to feed the renderer: it
            # produces a bare f64 on the stack (IEEE-754 semantics, matching
            # every other tier's host), consumed by `$f64_to_str` in an
            # interpolation. No Float ever reaches an i32 slot, a local, a
            # return or the boundary — those positions still refuse by name
            # (docs/strings.md §"Remaining wasm WAT work").
            left = self._expr(left_node, scope, where, "Float")
            right = self._expr(right_node, scope, where, "Float")
            if left.ty != "Float" or right.ty != "Float":
                raise EmitError(
                    f"{where}: `{op}` marked Float but an operand is "
                    f"{left.ty!r}/{right.ty!r} on this tier")
            instr = {"+": "f64.add", "-": "f64.sub",
                     "*": "f64.mul", "/": "f64.div"}[op]
            return _E(f"{left.wat}\n      {right.wat}\n      ({instr})", "Float")
        if op in ("+", "-", "*", "/", "%") and not (
                left_ty == right_ty and left_ty in ("Int", "Int32")):
            _refuse_float_operands(node, where)
            raise EmitError(f"{where}: arithmetic operator {op!r} is only lowerable for Int/Int32")
        if op not in _BINARY_OPS:
            raise EmitError(f"{where}: unsupported binary operator {op!r}")
        _refuse_float_operands(node, where)
        # `expected` is the *operand* type, which is also what picks the
        # instruction: `i64.eq` and `i32.eq` are the same comparison at two
        # widths, and only the operands say which.
        if op in _BOOL_OPS or (op in _CMP_SUFFIX and left_ty == "Bool" and right_ty == "Bool"):
            operand_ty = "Bool"
        elif left_ty == "Int32" and right_ty == "Int32":
            operand_ty = "Int32"
        else:
            operand_ty = "Int"
        left = self._expr(left_node, scope, where, operand_ty)
        right = self._expr(right_node, scope, where, operand_ty)
        if _is_unit_type(left.ty) or _is_unit_type(right.ty):
            raise EmitError(f"{where}: void operand in binary expression")
        instruction = _bin_instr(op, operand_ty)
        if instruction is None:
            raise EmitError(
                f"{where}: {op!r} is not lowerable over {operand_ty} on this tier")
        result_ty = "Bool" if op in _COMPARISON_OPS else operand_ty
        return _E(f"{left.wat}\n      {right.wat}\n      ({instruction})", result_ty)

    def _un_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        op = node.get("op")
        operand_ty = "Bool" if op == "!" else self._infer_type(node.get("operand"), scope)
        operand = self._expr(node.get("operand"), scope, where, operand_ty)
        if _is_unit_type(operand.ty):
            raise EmitError(f"{where}: void operand in unary expression")
        if op == "!":
            return _E(f"{operand.wat}\n      (i32.eqz)", "Bool")
        if op == "-":
            # negation is a subtraction from zero, and `0 - MIN` overflows: it
            # goes through the checked helper like any other subtraction, at the
            # operand's width (docs/arithmetic.md).
            if operand.ty == "Int32":
                return _E(f"(i32.const 0)\n      {operand.wat}\n      (call $int32_sub)", "Int32")
            return _E(f"(i64.const 0)\n      {operand.wat}\n      (call $int_sub)", "Int")
        raise EmitError(f"{where}: unsupported unary operator {op!r}")

    def _inline_arrow(self, arrow: dict, args: list, scope: _Scope, where: str) -> _E:
        """Inline a local arrow call. Arrows can't escape this tier (no
        lowerable function type), so `let f = x => …; f(a)` is beta-reduced:
        bind args to per-arrow param locals, read mutable captures from their
        bind-time snapshot, then emit the body."""
        aid = arrow.get("_aid")
        params = arrow.get("params") or []
        if len(args) != len(params):
            raise EmitError(f"{where}: arrow expects {len(params)} argument(s), got {len(args)}")
        sets: list[str] = []
        inner = _Scope(dict(scope.slots), dict(scope.types))
        for p, a in zip(params, args):
            av = self._expr(a, scope, where)
            self._declare_local(f"ap_{p}_{aid}", av.ty, where)
            sets.append(f"{av.wat}\n      (local.set $ap_{p}_{aid})")
            inner.slots[p] = f"(local.get $ap_{p}_{aid})"
            inner.types[p] = av.ty
        for c in arrow.get("captures") or []:
            inner.slots[c] = f"(local.get $cap_{c}_{aid})"  # bind-time snapshot
        body = self._expr(arrow.get("body"), inner, where)
        wat = "\n      ".join(sets + [body.wat]) if sets else body.wat
        return _E(wat, body.ty)

    def _call_expr(self, node: dict, scope: _Scope, where: str, expected: str | None = None) -> _E:
        callee = node.get("callee") or {}
        arrow = self._arrow_callee(callee)
        if arrow is not None:
            return self._inline_arrow(arrow, node.get("args") or [], scope, where)
        if callee.get("kind") != "var":
            raise EmitError(f"{where}: only direct function calls are lowerable on this tier")
        name = _ident(callee.get("name"), f"{where}: callee")
        if name == "Some":  # built-in Opt Some(x)
            args = node.get("args") or []
            if len(args) != 1:
                raise EmitError(f"{where}: Some expects one argument")
            payload = args[0]
            ty = (expected if self._tagged_layout(expected) is not None
                  else f"Opt[{self._infer_type(payload, scope) or 'Int'}]")
            return self._make_tagged(ty, "Some", payload, scope, where)
        sig = self.fn_sigs.get(name) or self.extern_sigs.get(name)
        if sig is None:
            raise EmitError(f"{where}: callee {name!r} is not a lowerable function")
        args = node.get("args") or []
        if len(args) != len(sig["params"]):
            raise EmitError(f"{where}: {name} expects {len(sig['params'])} argument(s), got {len(args)}")
        parts: list[str] = []
        for arg, param_ty in zip(args, sig["params"]):
            value = self._expr(arg, scope, where, param_ty)
            if _is_unit_type(value.ty):
                raise EmitError(f"{where}: void expression used as an argument")
            parts.append(value.wat)
        parts.append(f"(call ${name})")
        return _E("\n      ".join(parts), sig["returns"])

    def _builtin_expr(self, node: dict, scope: _Scope, where: str,
                      expected: str | None = None) -> _E:
        method = node.get("method")
        target_node = node.get("target")
        target_ty = self._infer_type(target_node, scope)
        args = node.get("args") or []
        # The total forms (docs/arithmetic.md): same quotient as the faulting
        # operation, but a zero divisor yields Err(reason) instead of the
        # `i64.div_s` trap — `fail` is refused in a pure fn, so the error
        # travels as a value. Both operands are evaluated once into scratch
        # slots; each branch allocates its own tagged cell.
        if method in _CHECKED_DIVS:
            ty = expected if self._tagged_layout(expected) is not None else "Result[Int, Str]"
            if self._tagged_layout(ty) is None:
                raise EmitError(f"{where}: {ty!r} is not a tagged union")
            # the operands are Int *values* (i64), so they get their own i64
            # locals rather than the i32 scratch slots the tagged cells use;
            # the names were minted per-node by `_collect_cdiv_locals`
            tmp_a = f"cdiv_{node['_cdiv']}_a"
            tmp_b = f"cdiv_{node['_cdiv']}_b"
            self._declare_local(tmp_a, "Int", where)
            self._declare_local(tmp_b, "Int", where)
            dividend = self._expr(target_node, scope, where, "Int")
            divisor = self._expr(args[0], scope, where, "Int")
            read_a = f"(local.get ${tmp_a})"
            read_b = f"(local.get ${tmp_b})"
            quotient = {
                "checked_div_trunc": f"(i64.div_s {read_a} {read_b})",
                "checked_div_floor": f"(call $int_div_floor {read_a} {read_b})",
                "checked_div_euclid": f"(call $int_div_euclid {read_a} {read_b})",
                "checked_mod": f"(call $int_mod {read_a} {read_b})",
            }[method]
            ok_cell = self._make_tagged(
                ty, "Ok",
                {"kind": "__wat", "wat": quotient, "ty": "Int"},
                scope, where)
            err_cell = self._make_tagged(
                ty, "Err",
                {"kind": "lit", "value": _DIV_ZERO_MSG},
                scope, where)
            wat = (f"{dividend.wat}\n      (local.set ${tmp_a})\n"
                   f"      {divisor.wat}\n      (local.set ${tmp_b})\n"
                   f"      (if (result i32)\n"
                   f"        (i64.eqz {read_b})\n"
                   f"        (then {err_cell.wat})\n"
                   f"        (else {ok_cell.wat}))")
            return _E(wat, ty)
        if method == "length":
            target = self._expr(target_node, scope, where, target_ty)
            if target_ty not in ("Str", "Bytes") and not _is_list_type(target_ty):
                raise EmitError(f"{where}: length is only lowerable on Str/Bytes/List")
            # A `Str` counts code points (docs/strings.md), decoding UTF-8; a
            # `Bytes` or `List` counts its elements from the u32 prefix, which
            # is the byte/element count.
            if target_ty == "Str":
                return _E(f"(i64.extend_i32_u (call $str_cp_length {target.wat}))", "Int")
            return _E(f"(i64.extend_i32_u (i32.load {target.wat}))", "Int")
        # Int/Int32 width conversions (docs/arithmetic.md). Widening Int32 -> Int
        # is a sign-extend; narrowing Int -> Int32 traps out of the i32 range
        # through `$int32_narrow` before wrapping (the fault every tier gives).
        # The Str form (parse) is handled below.
        if method == "to_int" and target_ty == "Int32":
            target = self._expr(target_node, scope, where, "Int32")
            return _E(f"(i64.extend_i32_s {target.wat})", "Int")
        if method == "to_int32":
            target = self._expr(target_node, scope, where, "Int")
            return _E(f"(call $int32_narrow {target.wat})", "Int32")
        if method == "push":
            if not _is_list_type(target_ty):
                raise EmitError(f"{where}: push is only lowerable on List values")
            target = self._expr(target_node, scope, where, target_ty)
            elem_ty = _list_elem(target_ty)
            arg = self._expr(args[0], scope, where, elem_ty)
            # $list_push writes one 8-byte slot and never learns the element
            # type, so a non-Int element is widened into the slot here
            elem = arg.wat if elem_ty == "Int" else f"(i64.extend_i32_u {arg.wat})"
            return _E(f"{target.wat}\n      {elem}\n      (call $list_push)", target_ty)
        if method == "concat":
            if target_ty not in ("Str", "Bytes") and not _is_list_type(target_ty):
                raise EmitError(f"{where}: concat is only lowerable on Str/Bytes/List")
            target = self._expr(target_node, scope, where, target_ty)
            arg = self._expr(args[0], scope, where, target_ty)
            helper = "$str_concat" if target_ty in ("Str", "Bytes") else "$list_concat"
            return _E(f"{target.wat}\n      {arg.wat}\n      (call {helper})", target_ty)
        if method == "slice":
            if target_ty not in ("Str", "Bytes") and not _is_list_type(target_ty):
                raise EmitError(f"{where}: slice is only lowerable on Str/Bytes/List")
            target = self._expr(target_node, scope, where, target_ty)
            start = self._expr(args[0], scope, where, "Int")
            end = self._expr(args[1], scope, where, "Int")
            # Str slices on code-point boundaries; Bytes on byte offsets; List
            # on element offsets (docs/strings.md).
            if target_ty == "Str":
                helper = "$str_cp_slice"
            elif target_ty == "Bytes":
                helper = "$str_slice"
            else:
                helper = "$list_slice"
            return _E(f"{target.wat}\n      {start.wat}\n      {end.wat}\n      (call {helper})", target_ty)
        if method == "charAt":
            if target_ty not in ("Str", "Bytes"):
                raise EmitError(f"{where}: charAt is only lowerable on Str/Bytes")
            target = self._expr(target_node, scope, where, target_ty)
            arg = self._expr(args[0], scope, where, "Int")
            # Str returns the whole scalar at a code-point index (docs/strings.md);
            # Bytes returns the single byte at a byte index.
            helper = "$str_cp_char_at" if target_ty == "Str" else "$str_char_at"
            return _E(f"{target.wat}\n      {arg.wat}\n      (call {helper})", "Str")
        if method == "charCodeAt":
            if target_ty not in ("Str", "Bytes"):
                raise EmitError(f"{where}: charCodeAt is only lowerable on Str/Bytes")
            target = self._expr(target_node, scope, where, target_ty)
            arg = self._expr(args[0], scope, where, "Int")
            # Str decodes the UTF-8 scalar value at a code-point index; Bytes
            # reads the raw byte (docs/strings.md).
            helper = "$str_cp_char_code_at" if target_ty == "Str" else "$str_char_code_at"
            return _E(f"{target.wat}\n      {arg.wat}\n      (call {helper})", "Int")
        if method in ("div_trunc", "div_floor", "div_euclid", "mod"):
            # Integer division and modulo (docs/arithmetic.md). i64.div_s
            # already truncates; the other three go through helpers so every
            # tier computes the same thing rather than inheriting a host rule.
            target = self._expr(target_node, scope, where, "Int")
            arg = self._expr(args[0], scope, where, "Int")
            if method == "div_trunc":
                return _E(f"(i64.div_s {target.wat} {arg.wat})", "Int")
            call = {"div_floor": "$int_div_floor",
                    "div_euclid": "$int_div_euclid",
                    "mod": "$int_mod"}[method]
            return _E(f"(call {call} {target.wat} {arg.wat})", "Int")
        if method == "to_str":
            # The rendering builtin (docs/stdlib-2.0.md §Int.to_str). The runtime
            # helper already exists for templates; it renders Int.MIN exactly
            # by dividing the negated bit pattern as unsigned.
            if target_ty != "Int":
                raise EmitError(f"{where}: to_str is only lowerable on Int")
            target = self._expr(target_node, scope, where, "Int")
            return _E(f"(call $int_to_str {target.wat})", "Str")
        if method == "to_int" and target_ty == "Str":
            # Str.to_int (FR-9, docs/stdlib-2.0.md §Str.to_int): the runtime
            # helper parses ASCII digits (leading `-` allowed) and returns the
            # Opt[Int] tagged cell — None for empty/partial/`+` spellings and
            # for out-of-i64-range magnitudes (Int.MIN itself parses).
            target = self._expr(target_node, scope, where, "Str")
            return _E(f"(call $str_to_int {target.wat})", "Opt[Int]")
        if method == "startsWith" or method == "endsWith":
            # The prefix/suffix probes (FR-6): byte comparisons are exact for
            # valid UTF-8 prefixes, and both helpers return the tier's Bool
            # (an i32 0/1).
            if target_ty not in ("Str", "Bytes"):
                raise EmitError(f"{where}: {method} is only lowerable on Str/Bytes")
            target = self._expr(target_node, scope, where, target_ty)
            arg = self._expr(args[0], scope, where, "Str")
            helper = "$str_starts_with" if method == "startsWith" else "$str_ends_with"
            return _E(f"{target.wat}\n      {arg.wat}\n      (call {helper})", "Bool")
        if method == "indexOf":
            # Byte-scan the haystack for the needle; `$str_index_of` returns a
            # code-point index (or -1), matching py/ts. The i32 result is
            # sign-extended so -1 crosses to the tier's Int as -1.
            if target_ty not in ("Str", "Bytes"):
                raise EmitError(
                    f"{where}: indexOf is not lowerable on this tier yet for List — "
                    f"the element comparison has no representation here; use a hosted backend")
            target = self._expr(target_node, scope, where, target_ty)
            arg = self._expr(args[0], scope, where, "Str")
            return _E(f"{target.wat}\n      {arg.wat}\n      (call $str_index_of)\n      (i64.extend_i32_s)", "Int")
        if method == "split":
            # Str.split(sep) → List[Str]: a fresh list of freshly allocated
            # pieces over the bump heap (docs/stdlib-2.0.md §split).
            if target_ty not in ("Str", "Bytes"):
                raise EmitError(f"{where}: split is only lowerable on Str")
            target = self._expr(target_node, scope, where, target_ty)
            arg = self._expr(args[0], scope, where, "Str")
            return _E(f"{target.wat}\n      {arg.wat}\n      (call $str_split)", "List[Str]")
        if method == "join":
            # List[Str].join(sep) → Str: receiver is the list, argument the
            # separator (docs/stdlib-2.0.md §join).
            if not _is_list_type(target_ty) or _list_elem(target_ty) != "Str":
                raise EmitError(f"{where}: join is only lowerable on List[Str]")
            target = self._expr(target_node, scope, where, target_ty)
            arg = self._expr(args[0], scope, where, "Str")
            return _E(f"{target.wat}\n      {arg.wat}\n      (call $str_join)", "Str")
        # The Map value type (docs/stdlib-2.0.md §Map): refused with the
        # named tier error, like indexOf — the canonical-ABI model carries
        # only Int/Bool/String/List, and a persistent map needs a richer
        # value model than that. An honest refusal beats a miscompile.
        if method in ("set", "lookup", "has", "size", "keys", "remove"):
            raise EmitError(
                f"{where}: `{method}` is not lowerable on this tier yet — "
                f"the Map value type has no representation here; use a hosted backend")
        raise EmitError(f"{where}: unsupported builtin method {method!r}")

    def _field_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        target_ty = self._infer_type(node.get("target"), scope)
        fields = self._record_fields(target_ty)
        if fields is None:
            raise EmitError(f"{where}: field access on non-record type {target_ty!r}")
        name = _ident(node.get("name"), f"{where}: field")
        if name not in fields:
            raise EmitError(f"{where}: record {target_ty!r} has no field {name!r}")
        field_ty = fields[name]
        if _is_unit_type(field_ty):
            raise EmitError(f"{where}: cannot access void record field {name!r}")
        target = self._expr(node.get("target"), scope, where, target_ty)
        offset = _SLOT * list(fields).index(name)
        address = (f"(i32.add {target.wat} (i32.const {offset}))"
                   if offset else target.wat)
        return _E(self._slot_load(address, field_ty), field_ty)

    def _index_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        target_ty = self._infer_type(node.get("target"), scope)
        index = self._expr(node.get("index"), scope, where, "Int")
        if target_ty in ("Str", "Bytes"):
            target = self._expr(node.get("target"), scope, where, target_ty)
            return _E(f"{target.wat}\n      {index.wat}\n      (call $str_char_at)", "Str")
        if _is_list_type(target_ty):
            elem_ty = _list_elem(target_ty)
            if _is_unit_type(elem_ty):
                raise EmitError(f"{where}: list of void is not lowerable")
            target = self._expr(node.get("target"), scope, where, target_ty)
            # the index is an Int *value*; the address it lands on is i32, so
            # it is narrowed exactly once, here
            if index.wat.startswith("(i64.const "):
                value = int(index.wat[len("(i64.const ") : -1])
                address = f"(i32.add {target.wat} (i32.const {_SLOT + _SLOT * value}))"
            else:
                address = (
                    f"(i32.add {target.wat}\n"
                    f"        (i32.add (i32.const {_SLOT})"
                    f" (i32.mul (i32.wrap_i64 {index.wat}) (i32.const {_SLOT}))))"
                )
            return _E(self._slot_load(address, elem_ty), elem_ty)
        raise EmitError(f"{where}: indexing is only lowerable for Str and List, got {target_ty!r}")

    def _len_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        target_ty = self._infer_type(node.get("target"), scope)
        if target_ty not in ("Str", "Bytes") and not _is_list_type(target_ty):
            raise EmitError(f"{where}: length is only lowerable for Str/Bytes/List")
        target = self._expr(node.get("target"), scope, where, target_ty)
        # Mirror the `.length()` method path (`_builtin_expr`): a `Str` counts
        # code points by decoding UTF-8 (docs/strings.md), never the raw u32
        # byte-length prefix. `Bytes`/`List` count elements, which *is* the
        # prefix. The property form `x.length` reaches here as a `len` node, so
        # it must make the same distinction the method form does — otherwise a
        # multibyte Str literal folds to its byte count (item 104).
        if target_ty == "Str":
            return _E(f"(i64.extend_i32_u (call $str_cp_length {target.wat}))", "Int")
        return _E(f"(i64.extend_i32_u (i32.load {target.wat}))", "Int")

    def _if_expr(self, node: dict, scope: _Scope, where: str, expected: str | None) -> _E:
        cond = self._expr(node.get("cond"), scope, where, "Bool")
        then_ty = self._infer_type(node.get("then"), scope, expected)
        else_ty = self._infer_type(node.get("else"), scope, expected)
        if then_ty != else_ty:
            raise EmitError(f"{where}: if branches must have the same type on this tier")
        if _is_unit_type(then_ty):
            raise EmitError(f"{where}: void if-expression is not lowerable; use an if statement")
        then_wat = self._expr(node.get("then"), scope, where, then_ty).wat
        else_wat = self._expr(node.get("else"), scope, where, then_ty).wat
        wat = (
            f"{cond.wat}\n"
            f"      (if (result {_wasm_ty(then_ty)})\n"
            f"        (then {then_wat})\n"
            f"        (else {else_wat}))"
        )
        return _E(wat, then_ty)

    def _record_expr(self, node: dict, scope: _Scope, where: str, expected: str | None) -> _E:
        ty = self._record_type(node, scope, expected)
        fields = self._record_fields(ty) or {}
        raw_by_name: dict[str, Any] = {}
        for raw_name, raw_value in node.get("fields") or []:
            raw_by_name[_ident(raw_name, f"{where}: record field")] = raw_value
        # Reserve this record's base-pointer slot *before* lowering the field
        # values: a field that is itself an allocation must land on a deeper
        # scratch, or it would overwrite our base pointer mid-construction.
        tmp = self._acquire_tmp()
        try:
            field_values: list[tuple[str, _E]] = []
            for name, ftype in fields.items():
                raw_value = raw_by_name.get(name)
                if raw_value is None:
                    raise EmitError(f"{where}: record literal is missing field {name!r}")
                value = self._expr(raw_value, scope, where, ftype)
                if _is_unit_type(value.ty):
                    raise EmitError(f"{where}: record field {name!r} is void")
                field_values.append((name, value))
            lines = [f"(call $alloc (i32.const {_SLOT * len(field_values)}))",
                     f"(local.set ${tmp})"]
            for position, (name, value) in enumerate(field_values):
                offset = _SLOT * position
                address = (f"(i32.add (local.get ${tmp}) (i32.const {offset}))"
                           if offset else f"(local.get ${tmp})")
                lines.append(self._slot_store(address, value.wat, value.ty))
            lines.append(f"(local.get ${tmp})")
        finally:
            self._release_tmp()
        return _E("\n      ".join(lines), ty)

    # -- tagged unions (variants, Opt, Result): [u32 tag][pad][slot payload] --

    def _make_tagged(self, ty: str | None, case: str, payload_node: Any,
                     scope: _Scope, where: str) -> _E:
        layout = self._tagged_layout(ty)
        if layout is None:
            raise EmitError(f"{where}: {ty!r} is not a tagged union")
        tag = self._tag_of(ty, case)
        payload_ty = next((p for c, p in layout if c == case), None)
        # Acquire before lowering the payload: an allocated payload (a record,
        # list, or nested variant) must not reuse this cell's base pointer.
        tmp = self._acquire_tmp()
        try:
            # the tag is a discriminant, not an Int value: it stays a u32, and
            # the payload slot follows it 8-aligned like every other slot
            lines = [
                f"(call $alloc (i32.const {2 * _SLOT}))",
                f"(local.set ${tmp})",
                f"(i32.store (local.get ${tmp}) (i32.const {tag}))",
            ]
            address = f"(i32.add (local.get ${tmp}) (i32.const {_SLOT}))"
            if payload_node is not None:
                value = self._expr(payload_node, scope, where, payload_ty)
                lines.append(self._slot_store(address, value.wat, value.ty))
            else:
                lines.append(f"(i64.store {address} (i64.const 0))")
            lines.append(f"(local.get ${tmp})")
        finally:
            self._release_tmp()
        return _E("\n      ".join(lines), ty)

    def _adt_expr(self, node: dict, scope: _Scope, where: str, expected: str | None) -> _E:
        ty = self._infer_type(node, scope, expected)
        payload = (node.get("args") or [None])[0]
        return self._make_tagged(ty, node.get("case"), payload, scope, where)

    def _match_expr(self, node: dict, scope: _Scope, where: str, expected: str | None) -> _E:
        scrut = self._expr(node.get("scrutinee"), scope, where)
        layout = self._tagged_layout(scrut.ty)
        if layout is None:
            raise EmitError(f"{where}: match scrutinee {scrut.ty!r} is not a tagged union")
        mloc = node.get("_scrut")
        arms = node.get("arms") or []

        def arm_scope(arm: dict) -> _Scope:
            """The arm's body sees its payload binding — and nothing else sees it."""
            bind = arm.get("bind")
            if not bind:
                return scope
            bname = _ident(bind, f"{where}: match bind")
            payload_ty = arm.get("payload_type")
            if payload_ty in (None, "Any"):
                # the arm's own annotation is optional; the variant's layout
                # always knows what this case carries
                payload_ty = next(
                    (payload for case, payload in layout if case == arm.get("pattern")),
                    None)
            if payload_ty == "Any":
                # e.g. an inferred `Result[Any, Any]`: the layout knows no more
                # than the arm did
                payload_ty = None
            inner = _Scope(dict(scope.slots), dict(scope.types))
            inner.slots[bind] = f"(local.get $l_{bname})"
            # An unknown payload defaults to Int, so the binding is read at the
            # full slot width. That is the safe default now that widths differ:
            # a slot is always written as a whole i64 (`_slot_store`), so
            # reading an unknown one as an Int returns the value that was put
            # there — reading it as an i32 would silently keep half of it.
            inner.types[bind] = payload_ty or "Int"
            self._declare_local(f"l_{bname}", inner.types[bind], where)
            return inner

        # the result type has to be inferred *inside* the first arm's scope:
        # `match o { Found(v) => v, … }` has no meaning where `v` is unbound,
        # and there is no `expected` to fall back on in a component body
        result_ty = expected or self._infer_type(
            arms[0].get("body"), arm_scope(arms[0]), expected)

        def arm_body(arm: dict) -> str:
            inner = arm_scope(arm)
            body = self._expr(arm.get("body"), inner, where, result_ty).wat
            bind = arm.get("bind")
            if not bind:
                return body
            bname = _ident(bind, f"{where}: match bind")
            payload = self._slot_load(
                f"(i32.add (local.get ${mloc}) (i32.const {_SLOT}))",
                inner.types[bind])
            load = f"(local.set $l_{bname} {payload})"
            return f"{load}\n      {body}"

        wildcard = next((a for a in arms if a.get("pattern") == "_"), None)
        chain = arm_body(wildcard) if wildcard is not None else "(unreachable)"
        for arm in reversed([a for a in arms if a.get("pattern") != "_"]):
            tag = self._tag_of(scrut.ty, arm.get("pattern"))
            cond = f"(i32.eq (i32.load (local.get ${mloc})) (i32.const {tag}))"
            chain = (f"(if (result {_wasm_ty(result_ty)})\n        {cond}\n"
                     f"        (then {arm_body(arm)})\n        (else {chain}))")
        wat = f"{scrut.wat}\n      (local.set ${mloc})\n      {chain}"
        return _E(wat, result_ty)

    def _collect_match_locals(self, node: Any, binds: set, scruts: set) -> None:
        if isinstance(node, dict):
            if node.get("kind") == "match":
                self._match_counter += 1
                node["_scrut"] = f"msc_{self._match_counter}"
                scruts.add(node["_scrut"])
                for arm in node.get("arms") or []:
                    if arm.get("bind"):
                        binds.add(_ident(arm["bind"], "match bind"))
            for value in node.values():
                self._collect_match_locals(value, binds, scruts)
        elif isinstance(node, list):
            for value in node:
                self._collect_match_locals(value, binds, scruts)

    def _collect_cdiv_locals(self, node: Any, names: list) -> None:
        """Mint the i64 operand locals each total-division form needs, in the
        order the bodies are lowered — the same pre-pass `_collect_match_locals`
        does for match scratch, since both live in expression position."""
        if isinstance(node, dict):
            if node.get("method") in _CHECKED_DIVS and "_cdiv" not in node:
                self._cdiv_counter += 1
                node["_cdiv"] = self._cdiv_counter
                names.extend([f"cdiv_{self._cdiv_counter}_a",
                              f"cdiv_{self._cdiv_counter}_b"])
            for value in node.values():
                self._collect_cdiv_locals(value, names)
        elif isinstance(node, list):
            for value in node:
                self._collect_cdiv_locals(value, names)

    def _list_expr(self, node: dict, scope: _Scope, where: str, expected: str | None) -> _E:
        ty = self._list_type(node, scope, expected)
        elem_ty = _list_elem(ty)
        items = node.get("items") or []
        # A list of allocated elements (records, nested lists, …) needs a
        # deeper scratch per element so an element's construction cannot
        # overwrite this list's base pointer — acquire before lowering them.
        tmp = self._acquire_tmp()
        try:
            values = [self._expr(item, scope, where, elem_ty) for item in items]
            for value in values:
                if _is_unit_type(value.ty):
                    raise EmitError(f"{where}: list element is void")
            # [u32 count][pad][slot0]… — the count keeps its canonical-ABI
            # width, the elements are one 8-byte slot each
            lines = [
                f"(call $alloc (i32.const {_SLOT * (len(values) + 1)}))",
                f"(local.set ${tmp})",
                f"(i32.store (local.get ${tmp}) (i32.const {len(values)}))",
            ]
            for position, value in enumerate(values):
                offset = _SLOT + _SLOT * position
                address = f"(i32.add (local.get ${tmp}) (i32.const {offset}))"
                lines.append(self._slot_store(address, value.wat, value.ty))
            lines.append(f"(local.get ${tmp})")
        finally:
            self._release_tmp()
        return _E("\n      ".join(lines), ty)

    def _interp_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        parts = node.get("parts") or []
        rendered: list[str] = []
        for kind, value in parts:
            if kind == "text":
                rendered.append(self._str_ptr(str(value)))
            else:  # ["expr", ir_node]
                piece = self._expr(value, scope, where)
                if piece.ty == "Str":
                    rendered.append(piece.wat)
                elif piece.ty == "Int":
                    rendered.append(f"{piece.wat}\n      (call $int_to_str)")
                elif piece.ty == "Float":
                    # Canonical Float -> Str (ECMAScript Number::toString) for
                    # the subset this tier renders exactly: NaN, +/-Infinity,
                    # and every integer-valued finite float with |x| < 2^63.
                    # Non-integer floats and |x| >= 2^63 (e.g. 1e21, which the
                    # ES form spells in exponent notation) still trap inside
                    # `$f64_to_str` — the documented, narrowed fence
                    # (docs/strings.md §"Remaining wasm WAT work").
                    rendered.append(f"{piece.wat}\n      (call $f64_to_str)")
                else:
                    raise EmitError(
                        f"{where}: a `${{…}}` template interpolates Str, Int or Float on "
                        f"this tier, got {piece.ty!r}"
                    )
        if not rendered:
            return _E(self._str_ptr(""), "Str")
        wat = rendered[0]
        for piece in rendered[1:]:
            wat = f"{wat}\n      {piece}\n      (call $str_concat)"
        return _E(wat, "Str")

    # -- statements + function emission --------------------------------------

    def _collect_arrow_locals(self, node: Any, acc: set[str]) -> None:
        """Assign each arrow a stable id and reserve its per-call param locals
        and per-capture snapshot locals (arrows sit in expression position, so
        _collect_locals doesn't reach them)."""
        if isinstance(node, dict):
            if node.get("kind") == "arrow":
                self._arrow_counter += 1
                node["_aid"] = self._arrow_counter
                aid = node["_aid"]
                for p in node.get("params") or []:
                    acc.add(f"ap_{_ident(p, 'arrow param')}_{aid}")
                for c in node.get("captures") or []:
                    acc.add(f"cap_{_ident(c, 'arrow capture')}_{aid}")
            for value in node.values():
                self._collect_arrow_locals(value, acc)
        elif isinstance(node, list):
            for value in node:
                self._collect_arrow_locals(value, acc)

    def _collect_locals(self, stmts: list, acc: set[str]) -> None:
        for stmt in stmts or []:
            step = stmt.get("step")
            if step in ("let", "assign"):
                value = stmt.get("value")
                # `let f = <arrow>` binds no runtime value (inlined at calls)
                if step == "let" and isinstance(value, dict) and value.get("kind") == "arrow":
                    continue
                acc.add(_ident(stmt.get("name"), "local"))
            elif step == "if":
                self._collect_locals(stmt.get("then") or [], acc)
                self._collect_locals(stmt.get("else") or [], acc)
            elif step == "while":
                self._collect_locals(stmt.get("body") or [], acc)
            elif step == "for":
                acc.add(_ident(stmt.get("bind"), "loop bind"))
                self._loop_counter += 1
                stmt["_lid"] = self._loop_counter
                # ptr / count / index scratch locals for this loop
                self._for_temps += [f"for_ptr_{self._loop_counter}",
                                    f"for_cnt_{self._loop_counter}",
                                    f"for_idx_{self._loop_counter}"]
                self._collect_locals(stmt.get("body") or [], acc)

    def _emit_stmts(self, stmts: list, scope: _Scope, where: str, expected_return: str | None) -> list[str]:
        out: list[str] = []
        for stmt in stmts or []:
            step = stmt.get("step")
            if step in ("let", "assign"):
                name = _ident(stmt.get("name"), f"{where}: binding")
                value_node = stmt.get("value")
                # `let f = <arrow>`: not a runtime value on this tier — register
                # it for inlining and snapshot its mutable captures by value.
                if step == "let" and isinstance(value_node, dict) and value_node.get("kind") == "arrow":
                    self._arrows[name] = value_node
                    aid = value_node.get("_aid")
                    for c in value_node.get("captures") or []:
                        capture = _ident(c, "capture")
                        self._declare_local(f"cap_{c}_{aid}",
                                            scope.types.get(capture), where)
                        out.append(f"(local.get $l_{capture})")
                        out.append(f"(local.set $cap_{c}_{aid})")
                    continue
                if step == "let":
                    value_ty = self._infer_type(stmt.get("value"), scope)
                else:
                    if name not in scope.types:
                        raise EmitError(f"{where}: assignment to undeclared {name!r}")
                    value_ty = scope.types[name]
                if _is_unit_type(value_ty):
                    raise EmitError(f"{where}: cannot bind a void expression")
                value = self._expr(stmt.get("value"), scope, where, value_ty)
                self._declare_local(f"l_{name}", value_ty, where)
                out.append(value.wat)
                out.append(f"(local.set $l_{name})")
                scope.slots[name] = f"(local.get $l_{name})"
                scope.types[name] = value_ty
            elif step == "return":
                if stmt.get("expr") is None:
                    if not _is_unit_type(expected_return):
                        raise EmitError(f"{where}: bare return in a typed function")
                    out.append("return")
                else:
                    value = self._expr(stmt.get("expr"), scope, where, expected_return)
                    if not _is_unit_type(value.ty):
                        out.append(value.wat)
                    out.append("return")
            elif step == "if":
                cond = self._expr(stmt.get("cond"), scope, where, "Bool")
                then_scope = _Scope(dict(scope.slots), dict(scope.types))
                else_scope = _Scope(dict(scope.slots), dict(scope.types))
                then_lines = self._emit_stmts(stmt.get("then") or [], then_scope, where, expected_return)
                else_lines = self._emit_stmts(stmt.get("else") or [], else_scope, where, expected_return) if stmt.get("else") else []
                out.append(cond.wat)
                out.append("(if")
                out.append("  (then")
                out.extend("    " + line for line in then_lines)
                out.append("  )")
                if else_lines:
                    out.append("  (else")
                    out.extend("    " + line for line in else_lines)
                    out.append("  )")
                out.append(")")
            elif step == "expr":
                value = self._expr(stmt.get("expr"), scope, where)
                out.append(value.wat)
                if not _is_unit_type(value.ty):
                    out.append("(drop)")
            elif step == "assert":
                value = self._expr(stmt.get("expr"), scope, where, "Bool")
                out.append(value.wat)
                out.append("(i32.eqz)")
                out.append("(if (then unreachable))")
            elif step == "while":
                cond = self._expr(stmt.get("cond"), scope, where, "Bool")
                body_scope = _Scope(dict(scope.slots), dict(scope.types))
                body_lines = self._emit_stmts(stmt.get("body") or [], body_scope, where, expected_return)
                out.append("(block")
                out.append("  (loop")
                out.append("    " + cond.wat)
                out.append("    (i32.eqz)")
                out.append("    (br_if 1)")
                out.extend("    " + line for line in body_lines)
                out.append("    (br 0)")
                out.append("  )")
                out.append(")")
            elif step == "for":
                out.extend(self._emit_for(stmt, scope, where, expected_return))
            else:
                raise EmitError(f"{where}: unsupported v3 statement step {step!r}")
        return out

    def _diverges(self, stmts: list) -> bool:
        """Does this statement list return/trap on every path (never falling
        through to its end)? Only the *last* statement can carry the whole
        list, so it decides: a `return` diverges; an `if` diverges when it has
        an `else` and both arms diverge. Everything else may fall through.

        wasm's validator does no such flow analysis: an `if/else` with no result
        type is always a fallthrough point to it, even when both arms `return`.
        A non-unit function whose body ends in a diverging `if/else` therefore
        reaches its end with an unsatisfied result unless a trailing
        `unreachable` (stack-polymorphic) closes it — see `_emit_function`.
        """
        if not stmts:
            return False
        last = stmts[-1]
        step = last.get("step")
        if step == "return":
            return True
        if step == "if":
            else_branch = last.get("else")
            return bool(else_branch) and self._diverges(last.get("then") or []) \
                and self._diverges(else_branch)
        return False

    def _emit_for(self, stmt: dict, scope: _Scope, where: str, expected_return: str | None) -> list[str]:
        """`for (x of xs)` over a list `[u32 count][pad][slot0]…` in memory.

        The cursor locals stay i32: `$for_ptr` is an address and `$for_cnt` /
        `$for_idx` are the stored count and a slot index, none of which is an
        `Int` value the program can observe.
        """
        n = stmt.get("_lid")
        iter_ty = self._infer_type(stmt.get("iterable"), scope)
        if not _is_list_type(iter_ty):
            raise EmitError(f"{where}: `for … of` iterates a List, got {iter_ty!r}")
        elem_ty = _list_elem(iter_ty)
        bind = _ident(stmt.get("bind"), f"{where}: loop bind")
        self._declare_local(f"l_{bind}", elem_ty, where)
        it = self._expr(stmt.get("iterable"), scope, where, iter_ty)
        body_scope = _Scope(dict(scope.slots), dict(scope.types))
        body_scope.slots[stmt.get("bind")] = f"(local.get $l_{bind})"
        body_scope.types[stmt.get("bind")] = elem_ty
        body_lines = self._emit_stmts(stmt.get("body") or [], body_scope, where, expected_return)
        ptr, cnt, idx = f"$for_ptr_{n}", f"$for_cnt_{n}", f"$for_idx_{n}"
        element = self._slot_load(
            f"(i32.add (local.get {ptr}) "
            f"(i32.add (i32.const {_SLOT}) "
            f"(i32.mul (local.get {idx}) (i32.const {_SLOT}))))",
            elem_ty)
        out = [
            it.wat, f"(local.set {ptr})",
            f"(i32.load (local.get {ptr}))", f"(local.set {cnt})",
            "(i32.const 0)", f"(local.set {idx})",
            "(block",
            "  (loop",
            f"    (i32.ge_s (local.get {idx}) (local.get {cnt}))",
            "    (br_if 1)",
            f"    {element}",
            f"    (local.set $l_{bind})",
        ]
        out.extend("    " + line for line in body_lines)
        out.extend([
            f"    (local.set {idx} (i32.add (local.get {idx}) (i32.const 1)))",
            "    (br 0)",
            "  )",
            ")",
        ])
        return out

    def _fresh_tmp(self, taken: set[str]) -> str:
        base = "__revl_tmp"
        candidate = base
        counter = 1
        while candidate in taken:
            candidate = f"{base}_{counter}"
            counter += 1
        return candidate

    def _emit_function(self, fn: dict, *, test_mode: bool = False) -> str:
        """Lower one v3 function (or, with *test_mode*, one `test` block).

        A test lowers as an exported zero-arg function returning Bool (i32):
        reaching the end means every `assert` held, so the tail is
        `(i32.const 1)`; a failed `assert` traps (`unreachable`, the existing
        statement lowering) before that point — wasmtime surfaces the trap as
        a nonzero exit, which is the host-side runner's failure signal.
        """
        name = _ident(fn.get("name"), "function name")
        where = name
        scope = _Scope({}, {})
        # A local's *width* is only known once its binding has been lowered, so
        # the declarations are collected by name here and rendered after the
        # body (`_declare_local` / `_local_decl`). Params and the result come
        # from the signature and are known up front.
        signature: list[str] = []
        local_names_in_order: list[str] = []
        for param in fn.get("params") or []:
            pname = _ident(param.get("name"), f"{where}: parameter")
            ptype = param.get("type")
            self._check_type(ptype, f"{where}: parameter {pname}")
            if not _is_unit_type(ptype):
                signature.append(f"(param $p_{pname} {_wasm_ty(ptype)})")
            scope.slots[pname] = f"(local.get $p_{pname})"
            scope.types[pname] = ptype
        return_ty = fn.get("returns")
        self._check_type(return_ty, f"{where}: return")
        if not _is_unit_type(return_ty):
            signature.append(f"(result {_wasm_ty(return_ty)})")

        local_names: set[str] = set()
        self._loop_counter = 0
        self._for_temps: list[str] = []
        self._arrows: dict = {}
        self._arrow_counter = 0
        self._local_types = {}
        self._collect_locals(fn.get("body") or [], local_names)
        for lname in sorted(local_names):
            if lname not in scope.types:
                local_names_in_order.append(f"l_{lname}")
                scope.slots[lname] = f"(local.get $l_{lname})"
                scope.types[lname] = None
        # match binds + one scratch pointer per match (match is in expression
        # position, so _collect_locals doesn't reach it)
        self._match_counter = 0
        self._cdiv_counter = 0
        match_binds: set[str] = set()
        match_scruts: set[str] = set()
        self._collect_match_locals(fn.get("body") or [], match_binds, match_scruts)
        for bname in sorted(match_binds):
            if bname not in local_names:
                local_names_in_order.append(f"l_{bname}")
        cdiv_locals: list[str] = []
        self._collect_cdiv_locals(fn.get("body") or [], cdiv_locals)
        local_names_in_order.extend(cdiv_locals)
        for sname in sorted(match_scruts):
            local_names_in_order.append(sname)
        for tname in self._for_temps:
            local_names_in_order.append(tname)
        # arrow param + capture-snapshot locals (arrows are inlined at calls)
        arrow_locals: set[str] = set()
        self._collect_arrow_locals(fn.get("body") or [], arrow_locals)
        for aname in sorted(arrow_locals):
            local_names_in_order.append(aname)
        tmp = self._fresh_tmp(set(scope.types) | local_names | match_scruts
                              | set(self._for_temps) | arrow_locals)
        self._tmp = tmp
        local_names_in_order.append(tmp)
        self._reset_tmp_pool()

        body_lines = self._emit_stmts(fn.get("body") or [], scope, where, return_ty)
        # A non-unit body that returns on all paths through a trailing diverging
        # `if/else` (or nested such) leaves wasm's validator seeing a fallthrough
        # past the result-less `if`, with the function result unsatisfied ("type
        # mismatch: expected …, nothing on stack"). A bare trailing `return`
        # already puts wasm in its stack-polymorphic unreachable state, so it
        # needs nothing; only a diverging control structure does. Close it with
        # a trailing, stack-polymorphic `unreachable`.
        body_stmts = fn.get("body") or []
        if (not test_mode and body_lines and not _is_unit_type(return_ty)
                and body_stmts and body_stmts[-1].get("step") != "return"
                and self._diverges(body_stmts)):
            body_lines.append("unreachable")
        # deeper scratch pointers minted for nested allocations (see
        # `_acquire_tmp`); wasm requires every local declared in the header
        for extra in sorted(self._tmp_extra):
            local_names_in_order.append(extra)
        decls = signature + [self._local_decl(n).lstrip() for n in local_names_in_order]
        if test_mode:
            body = "\n    ".join(body_lines + ["(i32.const 1)"])
        elif body_lines:
            body = "\n    ".join(body_lines)
        elif not _is_unit_type(return_ty):
            body = "unreachable"
        else:
            body = "nop"
        # the `$name` identifier lets one v3 function call another
        # (`call $name`); without it intra-module calls fail to resolve
        header = f'(func ${name} (export "{name}") {" ".join(decls)}'.rstrip()
        return f"  {header}\n    {body})"

    def emit(self) -> str:
        self._collect_string_literals()
        lines = [
            ";; Generated by the revl cordis-wasm backend (ir_version 3) — do not edit.",
            ";; pure functions + documented type layouts; Int is i64 (values), addresses",
            ";; are i32; Str/List/record values use canonical-ABI-shaped linear memory",
            ";; (u32 length/count prefix, then one 8-byte slot per field/element)",
            "(module",
            '  (memory (export "memory") 1)',
        ]
        for offset, data in self.data_segments:
            lines.append(f'  (data (i32.const {offset}) "{_wat_bytes(data)}")')
        lines.append(f"  (global $__hp (mut i32) (i32.const {self.heap_start}))")
        # Bodies are rendered first so the helper preamble can be decided from
        # what they actually reference: `$f64_to_str` is pulled in only when a
        # Float is really rendered, keeping the `functions` golden and every
        # other Float-free module byte-identical. Rendering order (functions,
        # then externs, then tests) is unchanged, so the emitted text is too.
        fn_blocks = [self._emit_function(fn) for fn in self.functions]
        extern_blocks = list(self._extern_funcs())
        # Each `test` block lowers to an exported zero-arg function returning
        # Bool: 1 = every assert held; a failed assert traps before the tail
        # (`src/revl/test.py run_wasm` invokes these via wasmtime). Lifecycle
        # tests never reach here — `_refuse_lifecycle_tests` rejects them by
        # name at the top of `emit`.
        fn_names = {fn.get("name") for fn in self.functions}
        test_blocks: list[str] = []
        for (tname, export), test in zip(test_export_names(self.tests), self.tests):
            if export in fn_names:
                raise EmitError(
                    f"test {tname!r} would export wasm function {export!r}, "
                    f"which collides with a declared function of the same "
                    f"name — rename one of them"
                )
            test_blocks.append(self._emit_function(
                {"name": export, "params": [], "returns": "Bool",
                 "body": test.get("body") or []},
                test_mode=True))
        all_blocks = fn_blocks + extern_blocks + test_blocks
        uses_f64 = any("$f64_to_str" in block for block in all_blocks)
        # The reader builtins ($str_split/$str_join/$str_index_of) follow the
        # same demand-driven rule as $f64_to_str: pulled in only when a body
        # actually calls one, so every split/join/indexOf-free module stays
        # byte-identical to before.
        uses_index_of = any("$str_index_of" in block for block in all_blocks)
        uses_split = any("$str_split" in block for block in all_blocks)
        uses_join = any("$str_join" in block for block in all_blocks)

        # tests call the same helpers their functions do ($str_eq, $alloc_str,
        # …), so a document whose bodies are all `test` blocks still needs them
        if self.functions or self.tests:
            lines.extend(self._helper_funcs())
            if uses_f64:
                lines.append(self._helper_f64_to_str())
            if uses_index_of:
                lines.append(self._helper_str_index_of())
            if uses_split:
                lines.append(self._helper_str_split())
            if uses_join:
                lines.append(self._helper_str_join())
        lines.extend(self._type_comments())
        unsupported = self._unsupported_comments()
        if unsupported:
            lines.extend(unsupported)
        if self.functions:
            lines.append("")
        for block in fn_blocks:
            lines.append(block)
            lines.append("")
        for block in extern_blocks:
            lines.append(block)
            lines.append("")
        for block in test_blocks:
            lines.append(block)
            lines.append("")
        lines.append(")")
        return "\n".join(lines) + "\n"


def _emit_v1(ir: dict) -> dict[str, str]:
    """Lower a v1/v2 component document to WAT modules, one per component.

    v2 components carry ``isolate``/``intercept``; they are lowered to
    realm-qualified import/export namespaces plus documented custom sections
    (see ``_ComponentEmitter._realm_sections``).
    """
    version = ir.get("ir_version")
    if version not in (1, 2):
        raise EmitError(f"unsupported ir_version {version!r} (expected 1 or 2)")
    services = ir.get("services") or {}
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")
    out: dict[str, str] = {}
    for component in components:
        emitter = _ComponentEmitter(
            component, services, ir_version=version,
            types=ir.get("types"), functions=ir.get("functions"),
            externs=ir.get("externs"))
        if emitter.name in out:
            raise EmitError(f"duplicate component name {emitter.name!r}")
        out[emitter.name] = emitter.emit()
    return out

def _emit_v3(ir: dict) -> dict[str, str]:
    """Lower an IR v3 document.

    Components (when present) use the v1 component lowering; types and pure
    functions are emitted as a standalone `functions` module with documented
    record/variant layouts and exported wasm functions. Each `test` block is
    lowered to an exported zero-arg function returning Bool (`revl_test_*`,
    true = pass, failed asserts trap) so the host-side runner can invoke them;
    externs remain documented as unsupported in that module rather than
    rejected wholesale.
    """
    services = ir.get("services") or {}
    components = ir.get("components") or []
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    if not components and not types and not functions and not externs and not tests:
        raise EmitError("IR document has no components, types, functions, externs, or tests")

    # Every component's declared config shape, so a `spawn <Target>` resolves
    # the target's config field order; and the set of spawn targets (templates),
    # read off the manifest, so a template may carry a config block.
    spawn_targets = {
        _ident(c.get("name"), "component name"): [
            (_ident(f.get("name"), "config field"), f.get("type"))
            for f in c.get("config") or []
        ]
        for c in components
    }
    templates = set((ir.get("manifest") or {}).get("templates") or [])

    out: dict[str, str] = {}
    for component in components:
        emitter = _ComponentEmitter(component, services, ir_version=3,
                                    types=types, functions=functions,
                                    externs=externs,
                                    is_template=component.get("name") in templates,
                                    spawn_targets=spawn_targets)
        if emitter.name in out:
            raise EmitError(f"duplicate component name {emitter.name!r}")
        out[emitter.name] = emitter.emit()

    if types or functions or externs or tests:
        module_name = "functions"
        if module_name in out:
            raise EmitError(f"duplicate module name {module_name!r}")
        out[module_name] = _V3Emitter(types, functions, externs, tests).emit()
    return out


# ------------------------------------------------------------ typed holes

def _refuse_holes(ir: dict) -> None:
    """A typed hole is an unmet obligation, not code (docs/holes.md).

    Emitting one would put a placeholder into WebAssembly and make wat2wasm the
    thing that complains — in its own vocabulary, about a line revl wrote.
    revl already knows the draft is unfinished, so the refusal belongs
    here, before a single character is emitted.
    """
    found: list = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("kind") == "hole":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for section in ("components", "functions", "tests", "externs"):
        walk(ir.get(section))
    if not found:
        return
    where = ", ".join(
        f"{h.get('file') or '?'}:{h.get('line') or '?'} "
        f"(expects `{h.get('type')}`)" for h in found[:3])
    if len(found) > 3:
        where += f", and {len(found) - 3} more"
    raise EmitError(
        f"refusing to emit WebAssembly: this document still has {len(found)} typed "
        f"hole(s) — {where}. A hole type-checks so the surrounding draft can "
        f"be checked, but it has no implementation and there is nothing to "
        f"lower. Fill every hole, then emit (docs/holes.md)."
    )

# A `fault test` is executed by driving a real activation and inspecting the
# runtime's residue afterwards (docs/fault-tests.md).  The wasm tier
# has no such driver, so it is refused loudly instead of being dropped on the
# floor: a silently-missing fault test is a guarantee nobody is checking.
def _refuse_fault_tests(ir) -> None:
    fault_tests = (ir or {}).get("fault_tests") or []
    if not fault_tests:
        return
    names = ", ".join(repr(unit.get("name")) for unit in fault_tests)
    raise EmitError(
        f"fault tests do not lower to the wasm tier ({names}) — `fault test` runs "
        f"on the python reference tier only (docs/fault-tests.md). Compile "
        f"this document with --backend py, or move the fault tests to a "
        f"module that is not emitted for this tier."
    )

def _refuse_lifecycle_tests(tests: list) -> None:
    """`lifecycle test` blocks (syntax-2.0 §7.1) are reference-tier only.

    A lifecycle test is not a pure test unit: it loads components into a live
    context, calls through provision keys, unloads them, and asserts
    residue-freedom by reading the *host runtime's* introspection (R1/R4,
    docs/backend-ir.md). That driver exists only in the cordis-py emitter.
    Refuse by name — a construct that is silently dropped by one renderer and
    present in another is this project's recurring bug class.
    """
    for test in tests or []:
        if test.get("lifecycle"):
            raise EmitError(
                f"lifecycle test {test.get('name')!r} is not lowerable on the {'wasm'} tier: "
                "it drives a live composition (load/call/unload) and asserts R4 "
                "residue-freedom through the host runtime's introspection, which only the "
                "reference tier implements — run it with `revl test --backend py` "
                "(docs/syntax-2.0.md §7.1)"
            )


def emit(ir: dict) -> dict[str, str]:
    """Lower one IR document to WAT modules (v1 components, v3 types/fns)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    _refuse_holes(ir)

    _refuse_fault_tests(ir)

    _refuse_lifecycle_tests(ir.get("tests") or [])
    version = ir.get("ir_version")
    if version == 1 or version == 2:
        return _emit_v1(ir)
    if version == 3:
        return _emit_v3(ir)
    raise EmitError(f"unsupported ir_version {version!r} (expected 1, 2, or 3)")


if __name__ == "__main__":
    import json
    import pathlib
    import sys

    ir_path, out_dir = sys.argv[1], pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(ir_path, encoding="utf-8") as handle:
        modules = emit(json.load(handle))
    for name, wat in modules.items():
        (out_dir / f"{name}.wat").write_text(wat, encoding="utf-8")
        print(f"wrote {out_dir / (name + '.wat')}")

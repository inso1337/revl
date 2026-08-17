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

Tier restrictions (cordis-wasm status: core Wasm, sync base calculus).
Violations are EmitError, never silent degradation. The component tier is
i32-only (Int service params/returns, `await Job.run(Int)`); the v3
functions tier additionally lowers Str/List/record values through a
canonical-ABI-shaped linear-memory representation. Config blocks, host
builtins outside `await Job.run`, method-time effects, and variant values
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


def _align4(value: int) -> int:
    return (value + 3) & ~3


def _wat_string(value: str) -> str:
    """Escape a Python string for use as a WAT string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class EmitError(ValueError):
    """The IR document cannot be lowered to the wasm tier."""


def _ident(name: Any, what: str) -> str:
    if not isinstance(name, str) or not IDENT_RE.match(name):
        raise EmitError(f"{what} {name!r} is not a usable identifier")
    return name


def _i32_only(type_name: Any, where: str) -> None:
    if type_name not in ("Int", None):
        raise EmitError(
            f"{where}: type {type_name!r} is not lowerable — the cordis-wasm "
            f"tier is i32-only (Int in, Int out); keep string-shaped services "
            f"on the hosted backends"
        )


class _ComponentEmitter:
    def __init__(self, component: dict, services: dict, ir_version: int = IR_VERSION) -> None:
        self.ir = component
        self.services = services
        self.ir_version = ir_version
        self.name = _ident(component.get("name"), "component name")
        if component.get("config"):
            raise EmitError(
                f"{self.name}: config blocks are not lowerable — the "
                f"cordis-wasm runtime has no instantiation-config channel yet"
            )
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
        self.imports: dict[tuple[str, str], tuple[int, bool]] = {}  # (key, op) -> (arity, has_result)
        self.globals: list[str] = []
        self.uses_job = False

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

    # -- service lookup ------------------------------------------------------

    def _op_spec(self, key: str, op: str, where: str) -> tuple[int, bool]:
        service_name = self.requires.get(key)
        service = self.services.get(service_name)
        if service is None:
            raise EmitError(f"{where}: req {key!r} is not declared in requires")
        spec = (service.get("methods") or {}).get(op)
        if spec is None:
            raise EmitError(f"{where}: {key}.{op} is not a method of {service_name}")
        for param in spec.get("params") or []:
            _i32_only(param.get("type"), f"{where}: {key}.{op} param {param.get('name')!r}")
        _i32_only(spec.get("returns"), f"{where}: {key}.{op} return")
        arity = len(spec.get("params") or [])
        has_result = spec.get("returns") is not None
        self.imports[(key, op)] = (arity, has_result)
        return arity, has_result

    # -- expressions ---------------------------------------------------------

    def _expr(self, node: Any, scope: dict[str, str], where: str) -> tuple[str, bool]:
        """Returns (wat, has_result)."""
        if not isinstance(node, dict) or "kind" not in node:
            raise EmitError(f"{where}: malformed expression {node!r}")
        kind = node["kind"]
        if kind == "lit":
            value = node.get("value")
            if not isinstance(value, int) or isinstance(value, bool):
                raise EmitError(
                    f"{where}: literal {value!r} is not lowerable — i32-only tier"
                )
            return f"(i32.const {value})", True
        if kind == "name":
            name = _ident(node.get("id"), f"{where}: name")
            slot = scope.get(name)
            if slot is None:
                raise EmitError(f"{where}: unbound name {name!r}")
            return slot, True
        if kind == "req":
            raise EmitError(f"{where}: a required service is only usable as a call target")
        if kind == "call":
            target = node.get("target") or {}
            if target.get("kind") != "req":
                raise EmitError(
                    f"{where}: i32 values have no methods — only calls on "
                    f"required services are lowerable on this tier"
                )
            key = _ident(target.get("name"), f"{where}: req")
            op = _ident(node.get("method"), f"{where}: method")
            arity, has_result = self._op_spec(key, op, where)
            args = node.get("args") or []
            if len(args) != arity:
                raise EmitError(f"{where}: {key}.{op} takes {arity} argument(s)")
            parts = []
            for arg in args:
                wat, arg_result = self._expr(arg, scope, where)
                if not arg_result:
                    raise EmitError(f"{where}: void expression used as an argument")
                parts.append(wat)
            call = f"(call $req_{key}_{op} {' '.join(parts)})" if parts else f"(call $req_{key}_{op})"
            return call, has_result
        if kind == "config":
            raise EmitError(f"{where}: config is not available on this tier")
        if kind == "host":
            raise EmitError(
                f"{where}: host builtin {node.get('fn')!r} is not available on "
                f"the cordis-wasm tier — express state through coeffects instead"
            )
        if kind == "format":
            raise EmitError(f"{where}: strings are not lowerable — i32-only tier")
        raise EmitError(f"{where}: unknown expression kind {kind!r}")

    def _statement(self, node: Any, scope: dict[str, str], where: str) -> str:
        """An expression evaluated for effect: drop an unused result."""
        wat, has_result = self._expr(node, scope, where)
        return f"(drop {wat})" if has_result else wat

    # -- component -----------------------------------------------------------

    def emit(self) -> str:
        where = self.name
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
                    self.globals.append(glob)
                    wat, has_result = self._expr(step["acquire"], scope, where)
                    if not has_result:
                        raise EmitError(
                            f"{where}: `let {bind}` binds a void acquisition — "
                            f"use a plain `effect` step"
                        )
                    seg.append(f"(global.set {glob} {wat})")
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
                        f"{where}: `await` on this tier supports only `Job.run(Int)` "
                        f"(the runtime's async host op); other awaitables live on "
                        f"the hosted backends"
                    )
                arg_wat, has_result = self._expr(expr["args"][0], scope, where)
                if not has_result:
                    raise EmitError(f"{where}: Job.run needs an Int argument")
                self.uses_job = True
                segments.append(f"(call $host_job_run {arg_wat})")
            elif kind == "provide":
                provide_funcs.extend(self._provide(step, scope, where))
            else:
                raise EmitError(f"{where}: unknown step {kind!r}")

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
            for param in spec_params:
                _i32_only(param.get("type"), f"{where}: {key}.{mname} param")
            _i32_only(spec.get("returns"), f"{where}: {key}.{mname} return")
            params = [_ident(p, f"{where}: param") for p in method.get("params") or []]
            if len(params) != len(spec_params):
                raise EmitError(f"{where}: method {mname!r} arity does not match the service")

            mscope = dict(scope)
            decl = []
            for i, param in enumerate(params):
                decl.append(f"(param $p_{param} i32)")
                mscope[param] = f"(local.get $p_{param})"
            has_result = spec.get("returns") is not None
            if has_result:
                decl.append("(result i32)")

            body_lines = []
            mwhere = f"{where}.{key}.{mname}"
            for mstep in method.get("body") or []:
                mkind = mstep.get("step")
                if mkind == "return":
                    wat, expr_result = self._expr(mstep["expr"], mscope, mwhere)
                    if has_result and not expr_result:
                        raise EmitError(f"{mwhere}: void expression returned from a typed method")
                    body_lines.append(wat if has_result else f"(drop {wat})" if expr_result else wat)
                elif mkind == "emit":
                    body_lines.append(self._statement(mstep["expr"], mscope, mwhere))
                    if mstep.get("compensate") is not None:
                        raise EmitError(
                            f"{mwhere}: method-time compensation is not lowerable — "
                            f"the wasm accumulator is the activation state machine"
                        )
                elif mkind in ("effect", "let-effect"):
                    raise EmitError(
                        f"{mwhere}: method-time effects are not lowerable — the "
                        f"wasm accumulator is fixed at activation (state machine); "
                        f"use a hosted backend for dynamic method-time acquisition"
                    )
                else:
                    raise EmitError(f"{mwhere}: unknown step {mkind!r}")

            header = f'(func (export "{self._provide_prefix(key)}.{mname}") {" ".join(decl)}'.rstrip()
            body = "\n    ".join(body_lines) if body_lines else "nop"
            funcs.append(f"  {header}\n    {body})")
        missing = set(declared) - {m.get("name") for m in step.get("methods") or []}
        if missing:
            raise EmitError(f"{where}: provision {key!r} is missing method {sorted(missing)[0]!r}")
        return funcs

    def _module(self, segments: list[str], inverses: list[tuple[int, str]], provide_funcs: list[str]) -> str:
        lines = [f";; Generated by the revl cordis-wasm backend (ir_version {self.ir_version}) — do not edit.",
                 f";; component {self.name}",
                 "(module"]
        lines.extend(self._realm_sections())
        for (key, op), (arity, has_result) in sorted(self.imports.items()):
            params = " ".join(["(param i32)"] * arity)
            result = " (result i32)" if has_result else ""
            sig = f" {params}" if params else ""
            lines.append(f'  (import "{self._import_module(key)}" "{op}" (func $req_{key}_{op}{sig}{result}))')
        if self.uses_job:
            lines.append('  (import "host" "job_run" (func $host_job_run (param i32)))')
        lines.append("  (global $__step (mut i32) (i32.const 0))")
        for glob in self.globals:
            lines.append(f"  (global {glob} (mut i32) (i32.const 0))")

        # activate_step: one iteration per body segment (paper §4.3.2)
        lines.append('  (func (export "activate_step") (result i32)')
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
        lines.append('  (func (export "deactivate")')
        if inverses:
            for index, wat in reversed(inverses):
                lines.append(f"    (if (i32.ge_s (global.get $__step) (i32.const {index}))")
                lines.append("      (then")
                lines.append(f"      {wat}))")
        else:
            lines.append("    nop")
        lines.append("  )")

        lines.extend(provide_funcs)
        lines.append(")")
        return "\n".join(lines) + "\n"


_WASM_BIN_OPS = {
    "==": "i32.eq", "===": "i32.eq", "!=": "i32.ne", "!==": "i32.ne",
    "<": "i32.lt_s", ">": "i32.gt_s", "<=": "i32.le_s", ">=": "i32.ge_s",
    "+": "i32.add", "-": "i32.sub", "*": "i32.mul", "/": "i32.div_s",
    "%": "i32.rem_s", "&&": "i32.and", "||": "i32.or",
}


def _is_unit_type(ty: str | None) -> bool:
    return ty in (None, "Unit")


def _is_i32_type(ty: str | None) -> bool:
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


class _V3Emitter:
    """IR v3 types + pure functions -> a standalone WAT module.

    Int/Bool stay i32; Str/List/record values use the canonical-ABI-shaped
    linear memory representation (u32 length/count prefix followed by bytes or
    4-byte elements/fields). The emitted module exports its memory, so a host
    can read string/list/record results without an object model.
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
        self.literal_offsets: dict[str, int] = {}
        self.data_segments: list[tuple[int, bytes]] = []
        self.heap_start = 0
        self._anon = 0
        self._tmp = "__revl_tmp"

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
        lines = []
        if self.externs:
            names = ", ".join(_ident(ext.get("name"), "extern name") for ext in self.externs)
            lines.append(f"  ;; unsupported on this tier: externs {names} (no @wasm body)")
        if self.tests:
            names = ", ".join(repr(test.get("name")) for test in self.tests)
            lines.append(f"  ;; unsupported on this tier: tests {names} (test runner is host-side)")
        return lines

    # -- type/layout helpers --------------------------------------------------

    def _check_type(self, ty: str | None, where: str) -> None:
        if ty in (None, "Unit", "Int", "Bool", "Str", "Bytes"):
            return
        if _is_list_type(ty):
            self._check_type(_list_elem(ty), f"{where}: list element")
            return
        spec = self.all_types.get(ty)
        if spec is not None and spec.get("kind") == "record":
            return
        # tagged unions (user variants, Opt, Result) lower to a 2-word
        # [i32 tag][i32 payload] cell — the payload is always one i32 (an Int/
        # Bool value or a pointer). Check payload types are themselves lowerable.
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

    def _collect_string_literals(self) -> None:
        seen: dict[str, None] = {}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("kind") == "lit" and isinstance(node.get("value"), str):
                    seen.setdefault(node["value"], None)
                if node.get("kind") == "interp":
                    # template text segments are string literals too
                    for part_kind, part in node.get("parts") or []:
                        if part_kind == "text":
                            seen.setdefault(part, None)
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        for fn in self.functions:
            walk(fn.get("body"))
        offset = 0
        for value in seen:
            raw = value.encode("utf-8")
            data = len(raw).to_bytes(4, "little") + raw
            self.literal_offsets[value] = offset
            self.data_segments.append((offset, data))
            offset = _align4(offset + len(data))
        self.heap_start = offset

    def _str_ptr(self, value: str) -> str:
        offset = self.literal_offsets.get(value)
        if offset is None:
            raise EmitError(f"internal: string literal {value!r} was not pooled")
        return f"(i32.const {offset})"


    # -- linear-memory runtime helpers ---------------------------------------

    def _helper_funcs(self) -> list[str]:
        return [
            self._helper_alloc(),
            self._helper_alloc_str(),
            self._helper_str_concat(),
            self._helper_str_eq(),
            self._helper_str_slice(),
            self._helper_str_char_at(),
            self._helper_str_char_code_at(),
            self._helper_list_push(),
            self._helper_list_concat(),
            self._helper_list_slice(),
        ]

    def _helper_alloc(self) -> str:
        return """  (func $alloc (param $n i32) (result i32)
    (local $p i32)
    (local.set $p (global.get $__hp))
    (global.set $__hp
      (i32.add
        (global.get $__hp)
        (i32.and
          (i32.add (local.get $n) (i32.const 3))
          (i32.const -4))))
    (local.get $p))"""

    def _helper_alloc_str(self) -> str:
        return """  (func $alloc_str (param $len i32) (result i32)
    (local $p i32)
    (local.set $p (call $alloc (i32.add (local.get $len) (i32.const 4))))
    (i32.store (local.get $p) (local.get $len))
    (local.get $p))"""

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


    def _helper_str_slice(self) -> str:
        return """  (func $str_slice (param $s i32) (param $start i32) (param $end i32) (result i32)
    (local $len i32)
    (local $p i32)
    (local.set $len (i32.sub (local.get $end) (local.get $start)))
    (local.set $p (call $alloc_str (local.get $len)))
    (memory.copy
      (i32.add (local.get $p) (i32.const 4))
      (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $start))
      (local.get $len))
    (local.get $p))"""

    def _helper_str_char_at(self) -> str:
        return """  (func $str_char_at (param $s i32) (param $idx i32) (result i32)
    (local $p i32)
    (local.set $p (call $alloc_str (i32.const 1)))
    (i32.store8
      (i32.add (local.get $p) (i32.const 4))
      (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $idx))))
    (local.get $p))"""

    def _helper_str_char_code_at(self) -> str:
        return """  (func $str_char_code_at (param $s i32) (param $idx i32) (result i32)
    (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $idx))))"""

    def _helper_list_push(self) -> str:
        return """  (func $list_push (param $list i32) (param $elem i32) (result i32)
    (local $n i32)
    (local $p i32)
    (local.set $n (i32.load (local.get $list)))
    (local.set $p
      (call $alloc
        (i32.add
          (i32.mul (i32.add (local.get $n) (i32.const 1)) (i32.const 4))
          (i32.const 4))))
    (i32.store (local.get $p) (i32.add (local.get $n) (i32.const 1)))
    (memory.copy
      (i32.add (local.get $p) (i32.const 4))
      (i32.add (local.get $list) (i32.const 4))
      (i32.mul (local.get $n) (i32.const 4)))
    (i32.store
      (i32.add
        (i32.add (local.get $p) (i32.const 4))
        (i32.mul (local.get $n) (i32.const 4)))
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
          (i32.mul (i32.add (local.get $na) (local.get $nb)) (i32.const 4))
          (i32.const 4))))
    (i32.store (local.get $p) (i32.add (local.get $na) (local.get $nb)))
    (memory.copy
      (i32.add (local.get $p) (i32.const 4))
      (i32.add (local.get $a) (i32.const 4))
      (i32.mul (local.get $na) (i32.const 4)))
    (memory.copy
      (i32.add
        (i32.add (local.get $p) (i32.const 4))
        (i32.mul (local.get $na) (i32.const 4)))
      (i32.add (local.get $b) (i32.const 4))
      (i32.mul (local.get $nb) (i32.const 4)))
    (local.get $p))"""

    def _helper_list_slice(self) -> str:
        return """  (func $list_slice (param $s i32) (param $start i32) (param $end i32) (result i32)
    (local $len i32)
    (local $p i32)
    (local.set $len (i32.sub (local.get $end) (local.get $start)))
    (local.set $p
      (call $alloc
        (i32.add (i32.mul (local.get $len) (i32.const 4)) (i32.const 4))))
    (i32.store (local.get $p) (local.get $len))
    (memory.copy
      (i32.add (local.get $p) (i32.const 4))
      (i32.add
        (i32.add (local.get $s) (i32.const 4))
        (i32.mul (local.get $start) (i32.const 4)))
      (i32.mul (local.get $len) (i32.const 4)))
    (local.get $p))"""



    # -- type inference -------------------------------------------------------

    def _infer_type(self, node: Any, scope: _Scope, expected: str | None = None) -> str | None:
        if not isinstance(node, dict) or "kind" not in node:
            raise EmitError("malformed v3 expression")
        kind = node["kind"]
        if kind == "lit":
            value = node.get("value")
            if isinstance(value, bool):
                return "Bool"
            if isinstance(value, int) and not isinstance(value, bool):
                return "Int"
            if isinstance(value, str):
                return "Str"
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
        if kind == "bin":
            op = node.get("op")
            left = self._infer_type(node.get("left"), scope)
            right = self._infer_type(node.get("right"), scope)
            if op in ("==", "===", "!=", "!==", "<", ">", "<=", ">=", "&&", "||"):
                return "Bool"
            if op == "+" and left == "Str" and right == "Str":
                return "Str"
            if op == "+" and _is_list_type(left) and left == right:
                return left
            return "Int"
        if kind == "un":
            op = node.get("op")
            if op == "!":
                return "Bool"
            if op == "-":
                return "Int"
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
            for arm in node.get("arms") or []:
                return self._infer_type(arm.get("body"), scope, expected)
            return expected
        if kind == "arrow":
            raise EmitError("arrow values are not lowerable on this tier — the wasm module has no closures")
        raise EmitError(f"unsupported v3 expression kind {kind!r}")

    def _call_type(self, node: dict, scope: _Scope) -> str | None:
        callee = node.get("callee") or {}
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
        if method == "indexOf":
            raise EmitError("indexOf is not lowerable on this tier yet — use a hosted backend")
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
        kind = node["kind"]
        if kind == "lit":
            value = node.get("value")
            if isinstance(value, bool):
                return _E("(i32.const 1)" if value else "(i32.const 0)", "Bool")
            if isinstance(value, int) and not isinstance(value, bool):
                return _E(f"(i32.const {value})", "Int")
            if isinstance(value, str):
                return _E(self._str_ptr(value), "Str")
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
            return self._builtin_expr(node, scope, where)
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
        raise EmitError(f"{where}: unsupported v3 expression kind {kind!r}")

    def _bin_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        op = node.get("op")
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
        if op in ("<", ">", "<=", ">=") and (left_ty != "Int" or right_ty != "Int"):
            raise EmitError(f"{where}: relational operator {op!r} is only lowerable for Int")
        if op in ("&&", "||") and (left_ty != "Bool" or right_ty != "Bool"):
            raise EmitError(f"{where}: logical operator {op!r} is only lowerable for Bool")
        if op in ("==", "===", "!=", "!==") and (left_ty not in ("Int", "Bool") or right_ty not in ("Int", "Bool")):
            raise EmitError(f"{where}: equality on this tier is lowerable for Int, Bool, and Str")
        if op in ("+", "-", "*", "/", "%") and (left_ty != "Int" or right_ty != "Int"):
            raise EmitError(f"{where}: arithmetic operator {op!r} is only lowerable for Int")
        if op not in _WASM_BIN_OPS:
            raise EmitError(f"{where}: unsupported binary operator {op!r}")
        if op in ("==", "===", "!=", "!==", "<", ">", "<=", ">=", "&&", "||"):
            expected = "Bool"
        else:
            expected = "Int"
        left = self._expr(left_node, scope, where, expected)
        right = self._expr(right_node, scope, where, expected)
        if _is_unit_type(left.ty) or _is_unit_type(right.ty):
            raise EmitError(f"{where}: void operand in binary expression")
        result_ty = "Bool" if op in ("==", "===", "!=", "!==", "<", ">", "<=", ">=", "&&", "||") else "Int"
        return _E(f"{left.wat}\n      {right.wat}\n      ({_WASM_BIN_OPS[op]})", result_ty)

    def _un_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        op = node.get("op")
        operand = self._expr(node.get("operand"), scope, where, "Bool" if op == "!" else "Int")
        if _is_unit_type(operand.ty):
            raise EmitError(f"{where}: void operand in unary expression")
        if op == "!":
            return _E(f"{operand.wat}\n      (i32.eqz)", "Bool")
        if op == "-":
            return _E(f"(i32.const 0)\n      {operand.wat}\n      (i32.sub)", "Int")
        raise EmitError(f"{where}: unsupported unary operator {op!r}")

    def _call_expr(self, node: dict, scope: _Scope, where: str, expected: str | None = None) -> _E:
        callee = node.get("callee") or {}
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
        sig = self.fn_sigs.get(name)
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

    def _builtin_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        method = node.get("method")
        target_node = node.get("target")
        target_ty = self._infer_type(target_node, scope)
        args = node.get("args") or []
        if method == "length":
            target = self._expr(target_node, scope, where, target_ty)
            if target_ty not in ("Str", "Bytes") and not _is_list_type(target_ty):
                raise EmitError(f"{where}: length is only lowerable on Str/Bytes/List")
            return _E(f"(i32.load {target.wat})", "Int")
        if method == "push":
            if not _is_list_type(target_ty):
                raise EmitError(f"{where}: push is only lowerable on List values")
            target = self._expr(target_node, scope, where, target_ty)
            arg = self._expr(args[0], scope, where, _list_elem(target_ty))
            return _E(f"{target.wat}\n      {arg.wat}\n      (call $list_push)", target_ty)
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
            helper = "$str_slice" if target_ty in ("Str", "Bytes") else "$list_slice"
            return _E(f"{target.wat}\n      {start.wat}\n      {end.wat}\n      (call {helper})", target_ty)
        if method == "charAt":
            if target_ty not in ("Str", "Bytes"):
                raise EmitError(f"{where}: charAt is only lowerable on Str/Bytes")
            target = self._expr(target_node, scope, where, target_ty)
            arg = self._expr(args[0], scope, where, "Int")
            return _E(f"{target.wat}\n      {arg.wat}\n      (call $str_char_at)", "Str")
        if method == "charCodeAt":
            if target_ty not in ("Str", "Bytes"):
                raise EmitError(f"{where}: charCodeAt is only lowerable on Str/Bytes")
            target = self._expr(target_node, scope, where, target_ty)
            arg = self._expr(args[0], scope, where, "Int")
            return _E(f"{target.wat}\n      {arg.wat}\n      (call $str_char_code_at)", "Int")
        if method == "indexOf":
            raise EmitError(f"{where}: indexOf is not lowerable on this tier yet")
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
        offset = 4 * list(fields).index(name)
        if offset:
            wat = f"(i32.load (i32.add {target.wat} (i32.const {offset})))"
        else:
            wat = f"(i32.load {target.wat})"
        return _E(wat, field_ty)

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
            if index.wat.startswith("(i32.const "):
                value = int(index.wat[len("(i32.const ") : -1])
                offset = 4 + 4 * value
                wat = f"(i32.load (i32.add {target.wat} (i32.const {offset})))"
            else:
                wat = (
                    f"(i32.load (i32.add {target.wat}\n"
                    f"        (i32.add (i32.const 4) (i32.mul {index.wat} (i32.const 4)))))"
                )
            return _E(wat, elem_ty)
        raise EmitError(f"{where}: indexing is only lowerable for Str and List, got {target_ty!r}")

    def _len_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        target_ty = self._infer_type(node.get("target"), scope)
        if target_ty not in ("Str", "Bytes") and not _is_list_type(target_ty):
            raise EmitError(f"{where}: length is only lowerable for Str/Bytes/List")
        target = self._expr(node.get("target"), scope, where, target_ty)
        return _E(f"(i32.load {target.wat})", "Int")

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
            f"      (if (result i32)\n"
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
        field_values: list[tuple[str, _E]] = []
        for name, ftype in fields.items():
            raw_value = raw_by_name.get(name)
            if raw_value is None:
                raise EmitError(f"{where}: record literal is missing field {name!r}")
            value = self._expr(raw_value, scope, where, ftype)
            if _is_unit_type(value.ty):
                raise EmitError(f"{where}: record field {name!r} is void")
            field_values.append((name, value))
        lines = [f"(call $alloc (i32.const {4 * len(field_values)}))", f"(local.set ${self._tmp})"]
        for position, (name, value) in enumerate(field_values):
            offset = 4 * position
            if offset:
                lines.append(
                    f"(i32.store (i32.add (local.get ${self._tmp}) (i32.const {offset})) {value.wat})"
                )
            else:
                lines.append(f"(i32.store (local.get ${self._tmp}) {value.wat})")
        lines.append(f"(local.get ${self._tmp})")
        return _E("\n      ".join(lines), ty)

    # -- tagged unions (variants, Opt, Result): [i32 tag][i32 payload] --------

    def _make_tagged(self, ty: str | None, case: str, payload_node: Any,
                     scope: _Scope, where: str) -> _E:
        layout = self._tagged_layout(ty)
        if layout is None:
            raise EmitError(f"{where}: {ty!r} is not a tagged union")
        tag = self._tag_of(ty, case)
        payload_ty = next((p for c, p in layout if c == case), None)
        lines = [
            "(call $alloc (i32.const 8))",
            f"(local.set ${self._tmp})",
            f"(i32.store (local.get ${self._tmp}) (i32.const {tag}))",
        ]
        if payload_node is not None:
            value = self._expr(payload_node, scope, where, payload_ty)
            payload_wat = value.wat
        else:
            payload_wat = "(i32.const 0)"
        lines.append(
            f"(i32.store (i32.add (local.get ${self._tmp}) (i32.const 4)) {payload_wat})"
        )
        lines.append(f"(local.get ${self._tmp})")
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
        result_ty = expected or self._infer_type(arms[0].get("body"), scope, expected)

        def arm_body(arm: dict) -> str:
            bind = arm.get("bind")
            if not bind:
                return self._expr(arm.get("body"), scope, where, result_ty).wat
            bname = _ident(bind, f"{where}: match bind")
            scope.slots[bind] = f"(local.get $l_{bname})"
            # on the i32 tier an unknown/`Any` payload is an i32 — default to Int
            payload_ty = arm.get("payload_type")
            scope.types[bind] = "Int" if payload_ty in (None, "Any") else payload_ty
            body = self._expr(arm.get("body"), scope, where, result_ty).wat
            load = f"(local.set $l_{bname} (i32.load (i32.add (local.get ${mloc}) (i32.const 4))))"
            return f"{load}\n      {body}"

        wildcard = next((a for a in arms if a.get("pattern") == "_"), None)
        chain = arm_body(wildcard) if wildcard is not None else "(unreachable)"
        for arm in reversed([a for a in arms if a.get("pattern") != "_"]):
            tag = self._tag_of(scrut.ty, arm.get("pattern"))
            cond = f"(i32.eq (i32.load (local.get ${mloc})) (i32.const {tag}))"
            chain = (f"(if (result i32)\n        {cond}\n"
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

    def _list_expr(self, node: dict, scope: _Scope, where: str, expected: str | None) -> _E:
        ty = self._list_type(node, scope, expected)
        elem_ty = _list_elem(ty)
        items = node.get("items") or []
        values = [self._expr(item, scope, where, elem_ty) for item in items]
        for value in values:
            if _is_unit_type(value.ty):
                raise EmitError(f"{where}: list element is void")
        lines = [
            f"(call $alloc (i32.const {4 * (len(values) + 1)}))",
            f"(local.set ${self._tmp})",
            f"(i32.store (local.get ${self._tmp}) (i32.const {len(values)}))",
        ]
        for position, value in enumerate(values):
            offset = 4 + 4 * position
            lines.append(
                f"(i32.store (i32.add (local.get ${self._tmp}) (i32.const {offset})) {value.wat})"
            )
        lines.append(f"(local.get ${self._tmp})")
        return _E("\n      ".join(lines), ty)

    def _interp_expr(self, node: dict, scope: _Scope, where: str) -> _E:
        parts = node.get("parts") or []
        rendered: list[str] = []
        for kind, value in parts:
            if kind == "text":
                rendered.append(self._str_ptr(str(value)))
            else:  # ["expr", ir_node] — must be Str (i32 tier has no int->str)
                piece = self._expr(value, scope, where, "Str")
                if piece.ty != "Str":
                    raise EmitError(
                        f"{where}: a `${{…}}` template interpolates Str only on this "
                        f"tier, got {piece.ty!r} (no int→string)"
                    )
                rendered.append(piece.wat)
        if not rendered:
            return _E(self._str_ptr(""), "Str")
        wat = rendered[0]
        for piece in rendered[1:]:
            wat = f"{wat}\n      {piece}\n      (call $str_concat)"
        return _E(wat, "Str")

    # -- statements + function emission --------------------------------------

    def _collect_locals(self, stmts: list, acc: set[str]) -> None:
        for stmt in stmts or []:
            step = stmt.get("step")
            if step in ("let", "assign"):
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
                if step == "let":
                    value_ty = self._infer_type(stmt.get("value"), scope)
                else:
                    if name not in scope.types:
                        raise EmitError(f"{where}: assignment to undeclared {name!r}")
                    value_ty = scope.types[name]
                if _is_unit_type(value_ty):
                    raise EmitError(f"{where}: cannot bind a void expression")
                value = self._expr(stmt.get("value"), scope, where, value_ty)
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

    def _emit_for(self, stmt: dict, scope: _Scope, where: str, expected_return: str | None) -> list[str]:
        """`for (x of xs)` over a list `[i32 count][elem0]…` in linear memory."""
        n = stmt.get("_lid")
        iter_ty = self._infer_type(stmt.get("iterable"), scope)
        if not _is_list_type(iter_ty):
            raise EmitError(f"{where}: `for … of` iterates a List, got {iter_ty!r}")
        elem_ty = _list_elem(iter_ty)
        bind = _ident(stmt.get("bind"), f"{where}: loop bind")
        it = self._expr(stmt.get("iterable"), scope, where, iter_ty)
        body_scope = _Scope(dict(scope.slots), dict(scope.types))
        body_scope.slots[stmt.get("bind")] = f"(local.get $l_{bind})"
        body_scope.types[stmt.get("bind")] = elem_ty
        body_lines = self._emit_stmts(stmt.get("body") or [], body_scope, where, expected_return)
        ptr, cnt, idx = f"$for_ptr_{n}", f"$for_cnt_{n}", f"$for_idx_{n}"
        out = [
            it.wat, f"(local.set {ptr})",
            f"(i32.load (local.get {ptr}))", f"(local.set {cnt})",
            "(i32.const 0)", f"(local.set {idx})",
            "(block",
            "  (loop",
            f"    (i32.ge_s (local.get {idx}) (local.get {cnt}))",
            "    (br_if 1)",
            f"    (i32.load (i32.add (local.get {ptr}) "
            f"(i32.add (i32.const 4) (i32.mul (local.get {idx}) (i32.const 4)))))",
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

    def _emit_function(self, fn: dict) -> str:
        name = _ident(fn.get("name"), "function name")
        where = name
        scope = _Scope({}, {})
        decls: list[str] = []
        for param in fn.get("params") or []:
            pname = _ident(param.get("name"), f"{where}: parameter")
            ptype = param.get("type")
            self._check_type(ptype, f"{where}: parameter {pname}")
            if not _is_unit_type(ptype):
                decls.append(f"(param $p_{pname} i32)")
            scope.slots[pname] = f"(local.get $p_{pname})"
            scope.types[pname] = ptype
        return_ty = fn.get("returns")
        self._check_type(return_ty, f"{where}: return")
        if not _is_unit_type(return_ty):
            decls.append("(result i32)")

        local_names: set[str] = set()
        self._loop_counter = 0
        self._for_temps: list[str] = []
        self._collect_locals(fn.get("body") or [], local_names)
        for lname in sorted(local_names):
            if lname not in scope.types:
                decls.append(f"(local $l_{lname} i32)")
                scope.slots[lname] = f"(local.get $l_{lname})"
                scope.types[lname] = None
        # match binds + one scratch pointer per match (match is in expression
        # position, so _collect_locals doesn't reach it)
        self._match_counter = 0
        match_binds: set[str] = set()
        match_scruts: set[str] = set()
        self._collect_match_locals(fn.get("body") or [], match_binds, match_scruts)
        for bname in sorted(match_binds):
            if bname not in local_names:
                decls.append(f"(local $l_{bname} i32)")
        for sname in sorted(match_scruts):
            decls.append(f"(local ${sname} i32)")
        for tname in self._for_temps:
            decls.append(f"(local ${tname} i32)")
        tmp = self._fresh_tmp(set(scope.types) | local_names | match_scruts | set(self._for_temps))
        self._tmp = tmp
        decls.append(f"(local ${tmp} i32)")

        body_lines = self._emit_stmts(fn.get("body") or [], scope, where, return_ty)
        if body_lines:
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
            ";; pure functions + documented type layouts; Str/List/record values use",
            ";; canonical-ABI-shaped linear memory (u32 length/count prefix)",
            "(module",
            '  (memory (export "memory") 1)',
        ]
        for offset, data in self.data_segments:
            lines.append(f'  (data (i32.const {offset}) "{_wat_bytes(data)}")')
        lines.append(f"  (global $__hp (mut i32) (i32.const {self.heap_start}))")
        if self.functions:
            lines.extend(self._helper_funcs())
        lines.extend(self._type_comments())
        unsupported = self._unsupported_comments()
        if unsupported:
            lines.extend(unsupported)
        if self.functions:
            lines.append("")
        for fn in self.functions:
            lines.append(self._emit_function(fn))
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
        emitter = _ComponentEmitter(component, services, ir_version=version)
        if emitter.name in out:
            raise EmitError(f"duplicate component name {emitter.name!r}")
        out[emitter.name] = emitter.emit()
    return out

def _emit_v3(ir: dict) -> dict[str, str]:
    """Lower an IR v3 document.

    Components (when present) use the v1 component lowering; types and pure
    functions are emitted as a standalone `functions` module with documented
    record/variant layouts and exported wasm functions. Externs/tests are
    documented as unsupported in that module rather than rejected wholesale.
    """
    services = ir.get("services") or {}
    components = ir.get("components") or []
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    if not components and not types and not functions and not externs and not tests:
        raise EmitError("IR document has no components, types, functions, externs, or tests")

    out: dict[str, str] = {}
    for component in components:
        emitter = _ComponentEmitter(component, services, ir_version=3)
        if emitter.name in out:
            raise EmitError(f"duplicate component name {emitter.name!r}")
        out[emitter.name] = emitter.emit()

    if types or functions or externs or tests:
        module_name = "functions"
        if module_name in out:
            raise EmitError(f"duplicate module name {module_name!r}")
        out[module_name] = _V3Emitter(types, functions, externs, tests).emit()
    return out


def emit(ir: dict) -> dict[str, str]:
    """Lower one IR document to WAT modules (v1 components, v3 types/fns)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
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

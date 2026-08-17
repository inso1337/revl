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

Tier restrictions (cordis-wasm status: core Wasm, sync base calculus,
i32-only ops). Violations are EmitError, never silent degradation:
strings/format, config blocks, host builtins, `await` steps, and non-Int
service types are all rejected with the reason.

`emit(ir) -> dict[name, wat]` — one WAT module per component.
"""

from __future__ import annotations

import re
from typing import Any

IR_VERSION = 1

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        self.imports: dict[tuple[str, str], tuple[int, bool]] = {}  # (key, op) -> (arity, has_result)
        self.globals: list[str] = []
        self.uses_job = False

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

            header = f'(func (export "provide:{key}.{mname}") {" ".join(decl)}'.rstrip()
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
        for (key, op), (arity, has_result) in sorted(self.imports.items()):
            params = " ".join(["(param i32)"] * arity)
            result = " (result i32)" if has_result else ""
            sig = f" {params}" if params else ""
            lines.append(f'  (import "coeffect:{key}" "{op}" (func $req_{key}_{op}{sig}{result}))')
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


def _i32_v3_type(type_name: Any, where: str) -> bool:
    """Return True when a v3 type lowers to wasm i32.

    `Unit`/None are wasm's no-result signature; Int and Bool are i32 (the
    substrate tier's only value type). Everything else is rejected.
    """
    if type_name in (None, "Unit"):
        return False
    if type_name in ("Int", "Bool"):
        return True
    raise EmitError(
        f"{where}: type {type_name!r} is not lowerable — the cordis-wasm "
        f"tier is i32-only (Int/Bool/Unit)"
    )


class _V3Emitter:
    """IR v3 types + pure functions -> a standalone WAT module.

    Records/variants are documented as layout comments (the i32-only substrate
    has no GC structs yet); pure Int/Bool functions become exported wasm
    functions. Unsupported nodes are hard EmitErrors.
    """

    def __init__(self, types: dict, functions: list, externs: list, tests: list) -> None:
        self.types = types or {}
        self.functions = functions or []
        self.externs = externs or []
        self.tests = tests or []
        self.fn_names = {fn.get("name") for fn in self.functions}
        self.fn_has_result = {
            fn.get("name"): fn.get("returns") not in (None, "Unit")
            for fn in self.functions
        }

    # -- type layouts (documentation-only on the i32 substrate) -------------

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


    # -- expressions ---------------------------------------------------------

    def _expr(self, node: Any, scope: dict[str, str], where: str) -> tuple[str, bool]:
        if not isinstance(node, dict) or "kind" not in node:
            raise EmitError(f"{where}: malformed v3 expression {node!r}")
        kind = node["kind"]
        if kind == "lit":
            value = node.get("value")
            if isinstance(value, bool):
                return "(i32.const 1)" if value else "(i32.const 0)", True
            if isinstance(value, int) and not isinstance(value, bool):
                return f"(i32.const {value})", True
            raise EmitError(f"{where}: literal {value!r} is not lowerable — i32-only tier")
        if kind == "var":
            name = _ident(node.get("name"), f"{where}: name")
            slot = scope.get(name)
            if slot is None:
                raise EmitError(f"{where}: unbound name {name!r}")
            return slot, True
        if kind == "bin":
            op = _WASM_BIN_OPS.get(node.get("op"))
            if op is None:
                raise EmitError(f"{where}: unsupported binary operator {node.get('op')!r}")
            left, left_result = self._expr(node.get("left"), scope, where)
            right, right_result = self._expr(node.get("right"), scope, where)
            if not left_result or not right_result:
                raise EmitError(f"{where}: void operand in binary expression")
            return f"{left}\n      {right}\n      ({op})", True
        if kind == "un":
            operand, operand_result = self._expr(node.get("operand"), scope, where)
            if not operand_result:
                raise EmitError(f"{where}: void operand in unary expression")
            if node.get("op") == "!":
                return f"{operand}\n      (i32.eqz)", True
            if node.get("op") == "-":
                return f"(i32.const 0)\n      {operand}\n      (i32.sub)", True
            raise EmitError(f"{where}: unsupported unary operator {node.get('op')!r}")
        if kind == "call":
            return self._call(node, scope, where)
        if kind == "field":
            raise EmitError(f"{where}: field access is not lowerable — the i32-only tier has no structs")
        if kind == "index":
            raise EmitError(f"{where}: index access is not lowerable — the i32-only tier has no lists")
        if kind == "if":
            cond, cond_result = self._expr(node.get("cond"), scope, where)
            if not cond_result:
                raise EmitError(f"{where}: void condition")
            then_wat, then_result = self._expr(node.get("then"), scope, where)
            else_wat, else_result = self._expr(node.get("else"), scope, where)
            if then_result != else_result:
                raise EmitError(f"{where}: if branches must both produce i32 on this tier")
            return (
                f"{cond}\n"
                f"      (if (result i32)\n"
                f"        (then {then_wat})\n"
                f"        (else {else_wat}))",
                then_result,
            )
        if kind == "record":
            raise EmitError(f"{where}: record literals are not lowerable — the i32-only tier has no structs")
        if kind == "list":
            raise EmitError(f"{where}: list literals are not lowerable — the i32-only tier has no lists")
        if kind == "arrow":
            raise EmitError(f"{where}: arrow values are not lowerable — the i32-only tier has no closures")
        if kind == "match":
            raise EmitError(f"{where}: match is not lowerable — the i32-only tier has no tagged unions")
        if kind == "interp":
            raise EmitError(f"{where}: string interpolation is not lowerable — i32-only tier")
        raise EmitError(f"{where}: unsupported v3 expression kind {kind!r}")

    def _call(self, node: dict, scope: dict[str, str], where: str) -> tuple[str, bool]:
        callee = node.get("callee") or {}
        args = node.get("args") or []
        if callee.get("kind") != "var":
            raise EmitError(f"{where}: only direct function calls are lowerable on this tier")
        name = _ident(callee.get("name"), f"{where}: callee")
        if name in self.fn_names:
            parts = []
            for arg in args:
                wat, has_result = self._expr(arg, scope, where)
                if not has_result:
                    raise EmitError(f"{where}: void expression used as an argument")
                parts.append(wat)
            parts.append(f"(call ${name})")
            return "\n      ".join(parts), self.fn_has_result.get(name, False)
        if name in ("Some", "Ok", "Err") and len(args) == 1:
            return self._expr(args[0], scope, where)
        if name == "None" and not args:
            return "(i32.const 0)", True
        raise EmitError(f"{where}: callee {name!r} is not a lowerable function")


    # -- statements + function emission --------------------------------------

    def _collect_locals(self, stmts: list, acc: set[str]) -> None:
        for stmt in stmts or []:
            step = stmt.get("step")
            if step in ("let", "assign"):
                acc.add(_ident(stmt.get("name"), "local"))
            elif step == "if":
                self._collect_locals(stmt.get("then") or [], acc)
                self._collect_locals(stmt.get("else") or [], acc)

    def _emit_stmts(self, stmts: list, scope: dict[str, str], where: str) -> list[str]:
        out: list[str] = []
        for stmt in stmts or []:
            step = stmt.get("step")
            if step in ("let", "assign"):
                name = _ident(stmt.get("name"), f"{where}: binding")
                wat, has_result = self._expr(stmt.get("value"), scope, where)
                if not has_result:
                    raise EmitError(f"{where}: cannot bind a void expression")
                out.append(wat)
                out.append(f"(local.set $l_{name})")
                scope[name] = f"(local.get $l_{name})"
            elif step == "return":
                if stmt.get("expr") is not None:
                    wat, has_result = self._expr(stmt.get("expr"), scope, where)
                    if not has_result:
                        raise EmitError(f"{where}: cannot return a void expression")
                    out.append(wat)
                out.append("return")
            elif step == "if":
                cond, cond_result = self._expr(stmt.get("cond"), scope, where)
                if not cond_result:
                    raise EmitError(f"{where}: void condition")
                out.append(cond)
                then_lines = self._emit_stmts(stmt.get("then") or [], scope, where)
                else_lines = self._emit_stmts(stmt.get("else") or [], scope, where) if stmt.get("else") else []
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
                wat, has_result = self._expr(stmt.get("expr"), scope, where)
                out.append(wat)
                if has_result:
                    out.append("(drop)")
            elif step == "assert":
                wat, has_result = self._expr(stmt.get("expr"), scope, where)
                if not has_result:
                    raise EmitError(f"{where}: assert needs an i32 condition")
                out.append(wat)
                out.append("(i32.eqz)")
                out.append("(if (then unreachable))")
            else:
                raise EmitError(f"{where}: unsupported v3 statement step {step!r}")
        return out

    def _emit_function(self, fn: dict) -> str:
        name = _ident(fn.get("name"), "function name")
        where = name
        scope: dict[str, str] = {}
        decls: list[str] = []
        for param in fn.get("params") or []:
            pname = _ident(param.get("name"), f"{where}: parameter")
            _i32_v3_type(param.get("type"), f"{where}: parameter {pname}")
            decls.append(f"(param $p_{pname} i32)")
            scope[pname] = f"(local.get $p_{pname})"
        has_result = _i32_v3_type(fn.get("returns"), f"{where}: return")
        if has_result:
            decls.append("(result i32)")

        local_names: set[str] = set()
        self._collect_locals(fn.get("body") or [], local_names)
        for lname in sorted(local_names):
            if lname not in scope:
                decls.append(f"(local $l_{lname} i32)")
                scope[lname] = f"(local.get $l_{lname})"

        body_lines = self._emit_stmts(fn.get("body") or [], scope, where)
        if body_lines:
            body = "\n    ".join(body_lines)
        elif has_result:
            body = "unreachable"
        else:
            body = "nop"
        header = f'(func (export "{name}") {" ".join(decls)}'.rstrip()
        return f"  {header}\n    {body})"

    def emit(self) -> str:
        lines = [
            ";; Generated by the revl cordis-wasm backend (ir_version 3) — do not edit.",
            ";; pure functions + documented type layouts",
            "(module",
            "  (memory 1)",
        ]
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
    """Lower a v1 component document to WAT modules, one per component."""
    if ir.get("ir_version") == 2:
        raise EmitError(
            "ir_version 2 (realms/interception) is not lowerable on this tier yet — "
            "realm-qualified import namespaces are future work; see docs/design-v2-realms.md"
        )
    if ir.get("ir_version") != IR_VERSION:
        raise EmitError(f"unsupported ir_version {ir.get('ir_version')!r} (expected {IR_VERSION})")
    services = ir.get("services") or {}
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")
    out: dict[str, str] = {}
    for component in components:
        emitter = _ComponentEmitter(component, services)
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

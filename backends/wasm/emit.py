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
    def __init__(self, component: dict, services: dict) -> None:
        self.ir = component
        self.services = services
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
                raise EmitError(
                    f"{where}: `await` is not lowerable yet — the cordis-wasm "
                    f"prototype implements the synchronous base calculus (its "
                    f"README, Status); use a hosted backend or extend the runtime"
                )
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
        lines = [f";; Generated by the revl cordis-wasm backend (ir_version {IR_VERSION}) — do not edit.",
                 f";; component {self.name}",
                 "(module"]
        for (key, op), (arity, has_result) in sorted(self.imports.items()):
            params = " ".join(["(param i32)"] * arity)
            result = " (result i32)" if has_result else ""
            sig = f" {params}" if params else ""
            lines.append(f'  (import "coeffect:{key}" "{op}" (func $req_{key}_{op}{sig}{result}))')
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


def emit(ir: dict) -> dict[str, str]:
    """Lower one IR document to WAT modules, one per component."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
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

"""revl backend emitter — cordis v4 (TypeScript) target.

Pure-Python (stdlib only), per docs/backend-ir.md: `emit(ir: dict) -> str`
produces one idiomatic TypeScript module from an IR document.

Lowering summary (see REPORT.md for the reasoning):

- The whole component body is lowered into a SINGLE `ctx.effect(function* ...)`
  generator.  This is deliberate: cordis disposes *top-level* fiber effects
  concurrently (`Fiber._unload` uses `Promise.all`), but disposers collected by
  one `fiber.effect(generator)` are run strictly sequentially in LIFO order.
  One generator per body is what makes R1 (LIFO) and R3 (dependents fully
  deactivate before the provider's earlier effects are reverted) hold.
- `let-effect` / `effect` steps -> plain evaluation + `yield () => <undo>`.
- `provide` steps -> `yield ctx.provide(name, impl)`.  The withdrawal inverse
  is the runtime's own (R5); yielding the wrapper reparents it into the body
  effect at the correct LIFO position.
- `emit` steps -> plain calls (nothing accumulated).
- `req` expressions -> `ctx.<name>` (the fiber's committed view; stays
  readable during teardown).
- `effect` steps inside provide-method bodies -> `ctx.effect(() => ...)`,
  which joins the component fiber's accumulator (coeffect operations are
  effects).
- `format` expressions -> template literals.

CLI: `python3 emit.py <ir.json> [> out.ts]`.
"""

from __future__ import annotations

import json
import re
import sys

__all__ = ["emit", "EmitError"]

IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

# Names the emitted scaffolding uses; user bindings may not shadow them.
EMITTER_RESERVED = {"ctx", "config", "rawConfig", "host", "Context"}

# JS/TS reserved words (enough to reject anything the IR should never contain).
JS_RESERVED = {
    "await", "break", "case", "catch", "class", "const", "continue",
    "debugger", "default", "delete", "do", "else", "enum", "export",
    "extends", "false", "finally", "for", "function", "if", "import", "in",
    "instanceof", "let", "new", "null", "return", "static", "super",
    "switch", "this", "throw", "true", "try", "typeof", "var", "void",
    "while", "with", "yield",
}

TYPE_MAP = {"Str": "string", "Int": "number", "Bool": "boolean", "Float": "number"}


def _ts_type(name: object) -> str:
    """Surface type -> TS type (IR v1/A6). Unknown names map to `unknown`."""
    if not isinstance(name, str) or not name:
        return "unknown"
    if name in TYPE_MAP:
        return TYPE_MAP[name]
    generic = re.match(r"^(\w+)\[(.+)\]$", name)
    if generic:
        head, inner = generic.group(1), generic.group(2)
        if head == "List":
            return f"{_ts_type(inner)}[]"
        if head == "Opt":
            return f"{_ts_type(inner)} | undefined"
    return "unknown"


class EmitError(ValueError):
    """The IR document violates the backend contract."""


def _ident(name: object, role: str) -> str:
    if not isinstance(name, str) or not IDENT_RE.match(name):
        raise EmitError(f"invalid {role} identifier: {name!r}")
    if name in JS_RESERVED:
        raise EmitError(f"{role} identifier is a reserved word: {name!r}")
    if name in EMITTER_RESERVED:
        raise EmitError(
            f"{role} identifier collides with emitter scaffolding: {name!r}"
        )
    return name


def _string(value: str) -> str:
    # json.dumps produces a valid TS double-quoted string literal.
    return json.dumps(value)


def _literal(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return _string(value)
    raise EmitError(f"unsupported literal: {value!r}")


def _template_text(text: str) -> str:
    """Escape literal text for inclusion in a template literal."""
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


class _Scope:
    """Names visible to expressions at one point in a body."""

    def __init__(self, component: dict):
        self.component = component
        self.requires: dict = component.get("requires") or {}
        self.config_fields = {f["name"] for f in component.get("config") or []}
        self.locals: set[str] = set()

    def child(self) -> "_Scope":
        child = _Scope.__new__(_Scope)
        child.component = self.component
        child.requires = self.requires
        child.config_fields = self.config_fields
        child.locals = set(self.locals)
        return child

    def bind(self, name: str) -> str:
        name = _ident(name, "binding")
        if name in self.locals:
            raise EmitError(f"rebinding of {name!r} (bindings are single-assignment)")
        self.locals.add(name)
        return name


# Expression kinds that never need parentheses when used as a call target.
_ATOMIC_KINDS = {"name", "config", "req", "call", "host"}


def _expr(node: object, scope: _Scope) -> str:
    if not isinstance(node, dict) or "kind" not in node:
        raise EmitError(f"malformed expression: {node!r}")
    kind = node["kind"]

    if kind == "lit":
        return _literal(node.get("value"))

    if kind == "name":
        name = _ident(node.get("id"), "name")
        if name not in scope.locals:
            raise EmitError(
                f"reference to unbound name {name!r} in component "
                f"{scope.component.get('name')!r}"
            )
        return name

    if kind == "config":
        field = node.get("field")
        if field not in scope.config_fields:
            raise EmitError(
                f"reference to undeclared config field {field!r} in component "
                f"{scope.component.get('name')!r}"
            )
        return f"config.{_ident(field, 'config field')}"

    if kind == "req":
        name = node.get("name")
        if name not in scope.requires:
            raise EmitError(
                f"reference to undeclared requirement {name!r} in component "
                f"{scope.component.get('name')!r} (G1)"
            )
        # Committed-view access: resolved through the fiber's snapshot, so it
        # stays readable during this component's own teardown (R3).
        return f"ctx.{_ident(name, 'requirement')}"

    if kind == "call":
        target = node.get("target")
        method = _ident(node.get("method"), "method")
        target_ts = _expr(target, scope)
        if not (isinstance(target, dict) and target.get("kind") in _ATOMIC_KINDS):
            target_ts = f"({target_ts})"
        args = ", ".join(_expr(arg, scope) for arg in node.get("args") or [])
        return f"{target_ts}.{method}({args})"

    if kind == "host":
        fn = node.get("fn")
        if not isinstance(fn, str) or not all(IDENT_RE.match(p) for p in fn.split(".")):
            raise EmitError(f"invalid host builtin: {fn!r}")
        args = ", ".join(_expr(arg, scope) for arg in node.get("args") or [])
        return f"host.{fn}({args})"

    if kind == "format":
        template = node.get("template")
        if not isinstance(template, str):
            raise EmitError(f"format template must be a string: {template!r}")
        args = [_expr(arg, scope) for arg in node.get("args") or []]
        out = []
        pos = 0
        # v1/A4: split on placeholders and `$$` escapes first, then render
        for match in re.finditer(r"\$\$|\$(\d+)", template):
            out.append(_template_text(template[pos : match.start()]))
            if match.group(0) == "$$":
                out.append("$")
            else:
                index = int(match.group(1))
                if index >= len(args):
                    raise EmitError(
                        f"format placeholder ${index} out of range in {template!r}"
                    )
                out.append("${" + args[index] + "}")
            pos = match.end()
        out.append(_template_text(template[pos:]))
        return "`" + "".join(out) + "`"

    raise EmitError(f"unknown expression kind: {kind!r}")


def _method_body(steps: list, scope: _Scope, indent: str) -> list[str]:
    """Steps inside a provide-method body.

    These run while the component is ACTIVE; `effect` steps go through
    `ctx.effect` so their undos join the component fiber's accumulator.
    """
    lines: list[str] = []
    for step in steps:
        kind = step.get("step")
        if kind == "return":
            lines.append(f"{indent}return {_expr(step['expr'], scope)}")
        elif kind in ("effect", "let-effect"):
            bind = None
            if kind == "let-effect":
                bind = scope.bind(step["bind"])
                lines.append(f"{indent}let {bind}: any")
            acquire = _expr(step["acquire"], scope)
            undo = _expr(step["undo"], scope)
            lines.append(f"{indent}ctx.effect(() => {{")
            if bind is not None:
                lines.append(f"{indent}  {bind} = {acquire}")
            else:
                lines.append(f"{indent}  {acquire}")
            lines.append(f"{indent}  return () => {undo}")
            lines.append(f"{indent}}})")
        elif kind == "emit":
            if step.get("compensate") is not None:
                # v1/A5: the compensation joins the fiber's accumulator
                lines.append(f"{indent}ctx.effect(() => {{")
                lines.append(f"{indent}  {_expr(step['expr'], scope)}")
                lines.append(f"{indent}  return () => {_expr(step['compensate'], scope)}")
                lines.append(f"{indent}}})")
            else:
                lines.append(f"{indent}{_expr(step['expr'], scope)}")
        elif kind == "await":
            raise EmitError("await steps are not allowed inside method bodies (A1)")
        elif kind == "provide":
            raise EmitError("provide steps are not allowed inside method bodies")
        else:
            raise EmitError(f"unknown step in method body: {kind!r}")
    return lines


def _provide_impl(step: dict, scope: _Scope, services: dict, indent: str) -> list[str]:
    service_name = step.get("service")
    service = services.get(service_name)
    if service is None:
        raise EmitError(f"provide references unknown service {service_name!r}")
    declared = service.get("methods") or {}

    lines: list[str] = []
    for method in step.get("methods") or []:
        name = _ident(method.get("name"), "method")
        if name not in declared:
            raise EmitError(
                f"method {name!r} is not declared by service {service_name!r}"
            )
        params = [ _ident(p, "parameter") for p in method.get("params") or [] ]
        spec_params = declared[name].get("params") or []
        # v1/A6: method params are the surface names binding the body; the
        # *types* come from the service declaration (arity must agree)
        if len(params) != len(spec_params):
            raise EmitError(
                f"method {name!r} arity does not match service {service_name!r}"
            )
        body_scope = scope.child()
        for param in params:
            body_scope.locals.add(param)
        sig = ", ".join(
            f"{p}: {_ts_type(spec.get('type'))}"
            for p, spec in zip(params, spec_params)
        )
        lines.append(f"{indent}{name}({sig}) {{")
        lines.extend(_method_body(method.get("body") or [], body_scope, indent + "  "))
        lines.append(f"{indent}}},")
    return lines


def _component_body(component: dict, services: dict, indent: str) -> list[str]:
    """The activation body, lowered into one ctx.effect generator."""
    scope = _Scope(component)
    provides = component.get("provides") or {}
    lines: list[str] = []
    for step in component.get("body") or []:
        kind = step.get("step")
        if kind in ("let-effect", "effect"):
            acquire = _expr(step["acquire"], scope)
            if kind == "let-effect":
                bind = scope.bind(step["bind"])
                lines.append(f"{indent}const {bind} = {acquire}")
            else:
                lines.append(f"{indent}{acquire}")
            # `undo` may reference the binding; it types in teardown mode —
            # by construction it cannot register further effects.
            undo = _expr(step["undo"], scope)
            lines.append(f"{indent}yield () => {undo}")
        elif kind == "emit":
            lines.append(f"{indent}{_expr(step['expr'], scope)}")
            if step.get("compensate") is not None:
                # v1/A5: compensation accumulates LIFO like an inverse
                lines.append(f"{indent}yield () => {_expr(step['compensate'], scope)}")
        elif kind == "await":
            # v1/A1: the await lands (inertia), then the yield closes the
            # iteration so a divert during the await skips every later step
            lines.append(f"{indent}await {_expr(step['expr'], scope)}")
            lines.append(f"{indent}yield null  // iteration boundary (A1)")
        elif kind == "provide":
            name = step.get("name")
            if name not in provides:
                raise EmitError(
                    f"provide step {name!r} is not declared in the component "
                    f"header of {component.get('name')!r}"
                )
            if provides[name] != step.get("service"):
                raise EmitError(
                    f"provide step {name!r} service does not match the header"
                )
            # R5: the withdrawal inverse is the runtime's own (ctx.provide is
            # revertible); yielding the wrapper slots it into this body
            # effect's LIFO sequence.
            lines.append(f"{indent}yield ctx.provide({_string(name)}, {{")
            lines.extend(_provide_impl(step, scope, services, indent + "  "))
            lines.append(f"{indent}}} satisfies {_ident(step['service'], 'service')})")
        elif kind == "return":
            raise EmitError("return steps are only allowed inside method bodies")
        else:
            raise EmitError(f"unknown step: {kind!r}")
    return lines


def _config_interface(component: dict) -> list[str]:
    fields = component.get("config") or []
    if not fields:
        return []
    name = component["name"]
    lines = [f"export interface {name}Config {{"]
    for field in fields:
        fname = _ident(field.get("name"), "config field")
        ts_type = TYPE_MAP.get(field.get("type"), "any")
        if field.get("default") is not None:
            lines.append(f"  /** default: {json.dumps(field['default'])} */")
            lines.append(f"  {fname}?: {ts_type}")
        else:
            lines.append(f"  {fname}: {ts_type}")
    lines.append("}")
    return lines


def _component(component: dict, services: dict) -> list[str]:
    name = _ident(component.get("name"), "component")
    requires = component.get("requires") or {}
    provides = component.get("provides") or {}
    fields = component.get("config") or []

    for local, service in requires.items():
        _ident(local, "requirement")
        if service not in services:
            raise EmitError(f"requirement {local!r} names unknown service {service!r}")
    for key, service in provides.items():
        _ident(key, "provision key")
        if service not in services:
            raise EmitError(f"provision {key!r} names unknown service {service!r}")

    lines = _config_interface(component)
    lines.append(f"export const {name} = {{")
    lines.append(f"  name: {_string(name)},")
    inject = ", ".join(_string(k) for k in requires)
    lines.append(f"  inject: [{inject}],")
    if provides:
        keys = ", ".join(_string(k) for k in provides)
        lines.append(f"  provide: [{keys}],")

    if fields:
        lines.append(f"  apply(ctx: Context, rawConfig: {name}Config) {{")
        spec_parts = []
        for field in fields:
            fname = field["name"]
            if field.get("default") is not None:
                spec_parts.append(
                    f"{fname}: {{ default: {_literal(field['default'])} }}"
                )
            else:
                spec_parts.append(f"{fname}: {{ required: true }}")
        spec = ", ".join(spec_parts)
        lines.append(
            f"    const config = host.applyConfigDefaults({_string(name)}, "
            f"rawConfig, {{ {spec} }}) as Required<{name}Config>"
        )
    else:
        lines.append("  apply(ctx: Context) {")

    # One generator per body: cordis runs disposers of a single effect
    # strictly sequentially (LIFO); top-level fiber effects would be
    # disposed concurrently (see REPORT.md, finding 1). A body containing
    # an `await` step compiles to an async generator (v1/A1).
    is_async = any(
        step.get("step") == "await" for step in component.get("body") or []
    )
    generator = "async function*" if is_async else "function*"
    lines.append(f"    ctx.effect({generator} () {{")
    lines.extend(_component_body(component, services, "      "))
    lines.append(f"    }}, {_string(name + '.body')})")
    lines.append("  },")
    lines.append("}")
    return lines


def emit(ir: dict, *, runtime_import: str = "../runtime.ts") -> str:
    """Emit one TypeScript module for an IR document (docs/backend-ir.md)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be an object")
    if ir.get("ir_version") == 2:
        raise EmitError(
            "ir_version 2 (realms/interception) is not lowerable on this backend "
            "yet — cordis-py only for now; see docs/design-v2-realms.md"
        )
    if ir.get("ir_version") != 1:
        raise EmitError(f"unsupported ir_version: {ir.get('ir_version')!r}")

    services = ir.get("services") or {}
    components = ir.get("components") or []

    out: list[str] = [
        "// Generated by revl backends/typescript/emit.py — do not edit.",
        "// Target runtime: cordis v4 (https://github.com/cordiverse/cordis).",
        "import type { Context } from 'cordis'",
        f"import {{ host }} from '{runtime_import}'",
        "",
    ]

    # Service interfaces (coeffect interfaces, DESIGN.md §3.1).
    for sname, service in services.items():
        _ident(sname, "service")
        out.append(f"export interface {sname} {{")
        for mname, method in (service.get("methods") or {}).items():
            _ident(mname, "method")
            # v1/A6: typed signatures derived from the service declaration
            params = ", ".join(
                f"{_ident(p.get('name'), 'parameter')}: {_ts_type(p.get('type'))}"
                for p in method.get("params") or []
            )
            returns = _ts_type(method["returns"]) if method.get("returns") else "void"
            if method.get("emission"):
                out.append("  /** emission — crosses the system boundary (DESIGN.md §3.5) */")
            out.append(f"  {mname}({params}): {returns}")
        out.append("}")
        out.append("")

    # Typed committed-view access: ctx.<key> for every provision in the doc.
    provided: dict[str, str] = {}
    for component in components:
        for key, service in (component.get("provides") or {}).items():
            if key in provided and provided[key] != service:
                raise EmitError(
                    f"provision key {key!r} bound to two services (G2)"
                )
            provided[key] = service
    if provided:
        out.append("declare module 'cordis' {")
        out.append("  interface Context {")
        for key, service in provided.items():
            out.append(f"    {key}: {service}")
        out.append("  }")
        out.append("}")
        out.append("")

    seen = set()
    for component in components:
        if component.get("name") in seen:
            raise EmitError(f"duplicate component name: {component.get('name')!r}")
        seen.add(component.get("name"))
        out.extend(_component(component, services))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _main(argv: list[str]) -> int:
    args = argv[1:]
    runtime_import = "../runtime.ts"
    if args[:1] == ["--runtime"]:
        if len(args) < 2:
            print("--runtime requires a value", file=sys.stderr)
            return 2
        runtime_import = args[1]
        args = args[2:]
    if len(args) != 1:
        print("usage: python3 emit.py [--runtime <path>] <ir.json>", file=sys.stderr)
        return 2
    with open(args[0], "r", encoding="utf-8") as handle:
        ir = json.load(handle)
    sys.stdout.write(emit(ir, runtime_import=runtime_import))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

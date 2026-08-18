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

# Members cordis's Context already owns; a provision key colliding with one
# would shadow framework API (or be refused by the runtime). The host knows
# its own surface, so this rejection lives in the backend, not the frontend.
CONTEXT_MEMBERS = {
    "root", "fiber", "registry", "reflect", "events", "logger", "runtime",
    "effect", "extend", "isolate", "intercept", "inject", "plugin",
    "on", "once", "emit", "parallel", "serial", "bail", "waterfall",
    "get", "set", "provide", "accessor", "mixin", "baseUrl",
}


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


def _json(value: object) -> str:
    """Serialize an IR metadata object as a TS object/array literal.

    The IR guarantees that realm/intercept metadata is plain JSON (static
    literals per docs/design-v2-realms.md), so ``json.dumps`` emits a valid
    TypeScript literal expression.
    """
    return json.dumps(value)


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

# The 2.0 expression kinds live in `_v3_expr`; the component/method renderer
# below owns the component-specific ones (`req`, `config`, `name`, `host`,
# `format`). A component body mixes both — `let msg = prefix + name` is a
# pure `bin` beside a `req` call — so the two renderers fall back to each
# other. Without this a construct every other tier accepted was refused here
# (see tests/test_cross_tier.py).
_ACTIVE_V3: "list" = []


def _v3_context_for_components() -> object:
    return _ACTIVE_V3[0] if _ACTIVE_V3 else _TsV3Context({}, [], [])


# `call` exists in *both* dialects with different shapes — component form is
# `target`/`method`, the 2.0 form is `callee`/`args` — so the node kind alone
# does not say which renderer owns it. Everything else here is component-only.
_COMPONENT_ONLY_KINDS = {"req", "config", "name", "host", "format"}


def _is_component_node(node: dict) -> bool:
    kind = node.get("kind")
    return kind in _COMPONENT_ONLY_KINDS or (kind == "call" and "target" in node)


def _fn_call(node: dict, render_arg) -> str:
    """`{"kind": "fn", "name": ..., "args": [...]}` — a call to a top-level
    `fn` or `extern`.

    This is how the component lowering spells a call that a `fn` body spells
    as a `call` node with a `var` callee (src/revl/lower.py), so both
    renderers have to know it; `render_arg` supplies the dialect for the
    arguments.
    """
    name = _ident(node.get("name"), "function")
    if _ACTIVE_V3:
        ctx = _ACTIVE_V3[0]
        if name not in ctx.function_names and name not in ctx.extern_names:
            raise EmitError(
                f"call to unknown function {name!r} — no `fn` or `extern` of "
                f"that name is declared in this document"
            )
    args = ", ".join(render_arg(arg) for arg in node.get("args") or [])
    return f"{name}({args})"


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

    # Dispatch on *shape*, not kind: `call` is spelled `target`/`method` in the
    # component dialect and `callee`/`args` in the 2.0 one, and a v3-shaped call
    # does reach component positions — the `Some(..)` the frontend injects for
    # `T` -> `Opt[T]` is one. Keying on kind alone read `method` off a v3 call
    # and reported the resulting `None` as a bad identifier. The v3 form falls
    # through to the v3 renderer below, which owns it. (docs/conformance.md
    # names this as the one place the two-renderer split fails on real input.)
    if kind == "call" and "target" in node:
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

    if kind == "fn":
        return _fn_call(node, lambda arg: _expr(arg, scope))

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

    if kind == "adt":
        # tagged ADT value (Result / user variant): `{ kind: "Ok", value: x }`
        # or `{ kind: "Missing" }`. Opt is not tagged (value | undefined). Same
        # runtime shape the v3 fn-body emitter produces.
        case = _string(node["case"])
        args = node.get("args") or []
        if args:
            return f"{{ kind: {case}, value: {_expr(args[0], scope)} }}"
        return f"{{ kind: {case} }}"

    if kind == "record":
        fields = ", ".join(
            f"{_ident(k, 'record field')}: {_expr(v, scope)}"
            for k, v in node.get("fields") or []
        )
        return "{" + fields + "}"

    if kind == "list":
        return "[" + ", ".join(_expr(item, scope) for item in node.get("items") or []) + "]"

    # anything else is a pure 2.0 expression: the v3 renderer owns those
    # kinds, and falls back here for the component-specific ones
    ctx = _v3_context_for_components()
    ctx.component_scope = scope
    return _v3_expr(node, ctx)


def _method_body(steps: list, scope: _Scope, indent: str) -> list[str]:
    """Steps inside a provide-method body.

    These run while the component is ACTIVE; `effect` steps go through
    `ctx.effect` so their undos join the component fiber's accumulator.
    """
    lines: list[str] = []
    for step in steps:
        kind = step.get("step")
        if kind == "let":
            name = scope.bind(step["name"])
            keyword = "let" if step.get("mutable") else "const"
            lines.append(f"{indent}{keyword} {name} = {_expr(step['value'], scope)}")
        elif kind == "assign":
            lines.append(f"{indent}{_ident(step['name'], 'binding')} = "
                         f"{_expr(step['value'], scope)}")
        elif kind == "return":
            # a void service operation returns nothing at all
            if step.get("expr") is None:
                lines.append(f"{indent}return")
            else:
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
    lines: list[str] = []
    for step in component.get("body") or []:
        _component_step(step, component, services, scope, indent, lines)
    return lines


def _component_step(step: dict, component: dict, services: dict, scope: _Scope,
                    indent: str, lines: list[str]) -> None:
    """One step of the activation body, appended to `lines`.

    Recursive because `if` branches hold ordinary body steps.
    """
    provides = component.get("provides") or {}
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
        lines.append(f"{indent}yield () => {{}}  // iteration boundary (A1)")
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
        if name in CONTEXT_MEMBERS:
            raise EmitError(
                f"provision key {name!r} collides with a cordis Context "
                f"member — `ctx.{name}` already exists. Rename the key."
            )
        # R5: the withdrawal inverse is the runtime's own (ctx.provide is
        # revertible); yielding the wrapper slots it into this body
        # effect's LIFO sequence.
        lines.append(f"{indent}yield ctx.provide({_string(name)}, {{")
        lines.extend(_provide_impl(step, scope, services, indent + "  "))
        lines.append(f"{indent}}} satisfies {_ident(step['service'], 'service')})")
    elif kind == "if":
        # An activation guard (A8). Branches hold ordinary body steps, so a
        # `yield` inside one keeps its place in the accumulator's LIFO order —
        # being a generator body makes that work with no handling here.
        lines.append(f"{indent}if ({_expr(step['cond'], scope)}) {{")
        for nested in step.get("then") or []:
            _component_step(nested, component, services, scope, indent + "  ", lines)
        if step.get("else"):
            lines.append(f"{indent}}} else {{")
            for nested in step["else"]:
                _component_step(nested, component, services, scope, indent + "  ", lines)
        lines.append(f"{indent}}}")
    elif kind == "fail":
        # A8: refusing activation is a throw out of the body. Whatever the
        # accumulator already holds is reverted by the runtime, so a partly
        # activated component still leaves no residue (R4).
        lines.append(f"{indent}throw new Error({_expr(step['message'], scope)})")
    elif kind == "return":
        raise EmitError("return steps are only allowed inside method bodies")
    else:
        raise EmitError(f"unknown step: {kind!r}")


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
    # v2: realm placements and intercept metadata (docs/design-v2-realms.md)
    isolate = component.get("isolate") or {}
    intercept = component.get("intercept") or {}

    for local, service in requires.items():
        _ident(local, "requirement")
        if service not in services:
            raise EmitError(f"requirement {local!r} names unknown service {service!r}")
    for key, service in provides.items():
        _ident(key, "provision key")
        if service not in services:
            raise EmitError(f"provision {key!r} names unknown service {service!r}")
    for key in isolate:
        if key not in requires and key not in provides:
            raise EmitError(f"{name}: isolate key {key!r} is not declared")
    for key in intercept:
        if key not in requires:
            raise EmitError(f"{name}: intercept key {key!r} is not a requirement")

    lines = _config_interface(component)
    lines.append(f"export const {name} = {{")
    lines.append(f"  name: {_string(name)},")
    if intercept:
        # v2: dict-form inject — non-null values are copied into the fiber
        # context's intercept chain (the consumer-declared d(k)); null marks a
        # required-but-not-intercepted key.
        inject = {key: intercept.get(key) for key in requires}
        lines.append(f"  inject: {_json(inject)},")
    else:
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
    if isolate:
        # v2: realm placements, applied by runtime.plug() BEFORE ctx.plugin —
        # the fiber's context chain is fixed at plugin time.
        lines.append(f"  isolate: {_json(isolate)},")
    lines.append("}")
    return lines



# ---------------------------------------------------------------------------
# v2.0 (ir_version 3): types & pure functions (docs/syntax-2.0.md §2–§3)
# ---------------------------------------------------------------------------

_TS_V3_TYPE = {
    "Int": "number",
    "Float": "number",
    "Bool": "boolean",
    "Str": "string",
    "Bytes": "Uint8Array",
    "Unit": "void",
}

_TS_V3_BIN_OPS = {
    "==": "===", "===": "===", "!=": "!==", "!==": "!==",
    "<": "<", ">": ">", "<=": "<=", ">=": ">=",
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
    "&&": "&&", "||": "||",
}

_HOST_ROOTS = {"Pool", "Map", "Job"}
_BUILTIN_CONSTRUCTORS = {"Some", "None", "Ok", "Err"}

_V3_ATOMIC_KINDS = {"var", "field", "index", "call", "lit"}


def _split_v3_types(inner: str) -> list[str]:
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


def _ts_v3_type(type_name: object) -> str:
    """v3 surface type -> TS type (docs/syntax-2.0.md §2)."""
    if type_name is None:
        return "void"
    if not isinstance(type_name, str) or not type_name.strip():
        return "unknown"
    name = type_name.strip()
    if name in _TS_V3_TYPE:
        return _TS_V3_TYPE[name]
    if "[" in name:
        base = name[: name.index("[")]
        inner = name[name.index("[") + 1: name.rindex("]")]
        args = _split_v3_types(inner)
        if base == "Opt":
            return f"{_ts_v3_type(args[0])} | undefined"
        if base == "List":
            return f"{_ts_v3_type(args[0])}[]"
        if base == "Map":
            return f"Map<{_ts_v3_type(args[0])}, {_ts_v3_type(args[1])}>"
        if base == "Result":
            # tagged, matching the `adt`-node runtime shape `{ kind, value }`
            return (f'{{ kind: "Ok"; value: {_ts_v3_type(args[0])} }}'
                    f' | {{ kind: "Err"; value: {_ts_v3_type(args[1])} }}')
        return base + "<" + ", ".join("unknown" for _ in args) + ">"
    return _ident(name, "type name")


class _TsV3Context:
    """Names visible to lowered v3 expression emitters."""

    def __init__(self, types: dict, functions: list, externs: list) -> None:
        self.types = types or {}
        self.function_names = {fn.get("name") for fn in functions or []}
        self.extern_names = {ext.get("name") for ext in externs or []}
        self.case_names: set[str] = set()
        for spec in self.types.values():
            if spec.get("kind") == "variant":
                for case in spec.get("cases") or []:
                    self.case_names.add(case.get("name"))
        self._match_counter = 0

    def new_match_tmp(self) -> str:
        # `$` is not in revl's identifier alphabet, so this cannot collide.
        self._match_counter += 1
        return f"$revl_match_{self._match_counter}"


def _v3_var(node: dict, ctx: _TsV3Context) -> str:
    name = node.get("name")
    _ident(name, "name")
    if name in ctx.function_names or name in ctx.extern_names or name in ctx.case_names:
        return name
    if name in _HOST_ROOTS:
        return f"host.{name}"
    if name == "None":
        return "undefined"
    if name in ("Some", "Ok", "Err"):
        return "((value) => value)"
    return name


def _ts_builtin(method, target: str, args: list) -> str:
    """The stdlib surface (docs/stdlib-2.0.md) as idiomatic TS; `push` and
    `concat` are persistent (value semantics), matching the py backend."""
    if method == "length":
        return f"{target}.length"
    if method == "push":
        return f"[...{target}, {args[0]}]"
    if method == "slice":
        return f"{target}.slice({args[0]}, {args[1]})"
    if method == "charAt":
        return f"{target}.charAt({args[0]})"
    if method == "charCodeAt":
        return f"{target}.charCodeAt({args[0]})"
    if method == "concat":
        return f"{target}.concat({args[0]})"
    if method == "indexOf":
        return f"{target}.indexOf({args[0]})"
    if method == "split":
        return f"{target}.split({args[0]})"
    if method == "join":
        return f"{target}.join({args[0]})"
    if method == "repeat":
        return f"{target}.repeat({args[0]})"
    raise EmitError(f"unknown builtin method {method!r}")


def _v3_expr(node: object, ctx: _TsV3Context) -> str:
    if not isinstance(node, dict) or "kind" not in node:
        raise EmitError(f"malformed v3 expression: {node!r}")
    kind = node["kind"]

    # A component body mixes both dialects (`x ?? 0` is a pure `bin` whose
    # left operand is a `req` call), so hand component-shaped nodes back to
    # the component renderer *before* the 2.0 cases run. Kind alone is not
    # enough: a `call` here may be either dialect, distinguished by `target`.
    component_scope = getattr(ctx, "component_scope", None)
    if component_scope is not None and _is_component_node(node):
        return _expr(node, component_scope)

    if kind == "lit":
        return _literal(node.get("value"))

    if kind == "var":
        return _v3_var(node, ctx)

    if kind == "fn":
        return _fn_call(node, lambda arg: _v3_expr(arg, ctx))

    if kind == "bin":
        if node.get("op") == "??":
            return f"({_v3_expr(node['left'], ctx)} ?? {_v3_expr(node['right'], ctx)})"
        op = _TS_V3_BIN_OPS.get(node.get("op"))
        if op is None:
            raise EmitError(f"unsupported binary operator {node.get('op')!r}")
        return f"({_v3_expr(node['left'], ctx)} {op} {_v3_expr(node['right'], ctx)})"

    if kind == "un":
        operand = _v3_expr(node.get("operand"), ctx)
        if node.get("op") == "!":
            return f"(!{operand})"
        if node.get("op") == "-":
            return f"(-{operand})"
        raise EmitError(f"unsupported unary operator {node.get('op')!r}")

    if kind == "call":
        callee_node = node.get("callee")
        callee = _v3_expr(callee_node, ctx)
        if not (isinstance(callee_node, dict) and callee_node.get("kind") in _V3_ATOMIC_KINDS):
            callee = f"({callee})"
        args = ", ".join(_v3_expr(arg, ctx) for arg in node.get("args") or [])
        return f"{callee}({args})"

    if kind == "field":
        target_node = node.get("target")
        target = _v3_expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        return f"{target}.{_ident(node.get('name'), 'field')}"

    if kind == "index":
        target_node = node.get("target")
        target = _v3_expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        return f"{target}[{_v3_expr(node['index'], ctx)}]"

    if kind == "builtin":
        target = _v3_expr(node.get("target"), ctx)
        if not (isinstance(node.get("target"), dict) and node["target"].get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        args = [_v3_expr(a, ctx) for a in node.get("args") or []]
        return _ts_builtin(node.get("method"), target, args)

    if kind == "if":
        return (
            f"({_v3_expr(node['cond'], ctx)} ? "
            f"{_v3_expr(node['then'], ctx)} : {_v3_expr(node['else'], ctx)})"
        )

    if kind == "record":
        fields = ", ".join(
            f"{_ident(k, 'record field')}: {_v3_expr(v, ctx)}"
            for k, v in node.get("fields") or []
        )
        return "{" + fields + "}"

    if kind == "list":
        return "[" + ", ".join(_v3_expr(item, ctx) for item in node.get("items") or []) + "]"

    if kind == "arrow":
        # Arrow parameters are explicitly `any`, not implicitly: the checker
        # enumerates arrows in its unchecked remainder (typecheck.py header),
        # so the compiler has no type to emit here and a guess would be worse
        # than an admission. `strict` rejects only an *implicit* any, so
        # writing it is also what makes the emitted file compile.
        params = ", ".join(f"{_ident(p, 'arrow parameter')}: any"
                           for p in node.get("params") or [])
        return f"(({params}) => ({_v3_expr(node['body'], ctx)}))"

    if kind == "match":
        return _v3_match_expr(node, ctx)

    if kind == "interp":
        parts = node.get("parts") or []
        segs = ["`"]
        for part_kind, value in parts:
            if part_kind == "text":
                segs.append(_template_text(value))
            else:  # ["expr", ir_node] — a full expression
                segs.append("${" + _v3_expr(value, ctx) + "}")
        segs.append("`")
        return "".join(segs)

    if kind == "adt":
        # tagged ADT value (Result / user variant): `{ kind: "Ok", value: x }`
        # or `{ kind: "Missing" }`. Opt is not tagged (value | undefined).
        case = _string(node["case"])
        args = node.get("args") or []
        if args:
            return f"{{ kind: {case}, value: {_v3_expr(args[0], ctx)} }}"
        return f"{{ kind: {case} }}"

    if kind == "optfield":
        target_node = node.get("target")
        target = _v3_expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        return f"{target}?.{_ident(node.get('name'), 'optional field')}"

    if kind == "optcall":
        target_node = node.get("target")
        target = _v3_expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        args = ", ".join(_v3_expr(a, ctx) for a in node.get("args") or [])
        return f"{target}?.{_ident(node.get('method'), 'optional method')}({args})"

    raise EmitError(f"unsupported v3 expression kind {kind!r}")


def _v3_arm_body(arm: dict, ctx: _TsV3Context) -> str:
    """Render one match arm's body with that arm's payload binding in scope.

    In a `fn` body nothing tracks bindings, but in a component/method body the
    fallback renderer checks that every `name` resolves — and a match arm
    binds its payload for the arm body only. Without this, `match o { Found(v)
    => v }` inside a provide-method reports `v` as unbound.
    """
    bind = arm.get("bind")
    scope = getattr(ctx, "component_scope", None)
    if not bind or scope is None:
        return _v3_expr(arm.get("body"), ctx)
    arm_scope = scope.child()
    # `.add` rather than `.bind`: the emitted arm is an IIFE parameter, which
    # shadows in TS exactly as the pattern binding shadows in revl, so this is
    # not the rebinding that single-assignment forbids.
    arm_scope.locals.add(_ident(bind, "match bind"))
    ctx.component_scope = arm_scope
    try:
        return _v3_expr(arm.get("body"), ctx)
    finally:
        ctx.component_scope = scope


def _v3_match_expr(node: dict, ctx: _TsV3Context) -> str:
    tmp = ctx.new_match_tmp()
    scrutinee = _v3_expr(node.get("scrutinee"), ctx)
    arms = node.get("arms") or []

    # Opt is `value | undefined` (not tagged): Some/None discriminate on
    # undefined, and Some binds the scrutinee itself.
    if any(arm.get("pattern") in ("Some", "None") for arm in arms):
        lines = [f"(({tmp}) => {{"]
        wildcard = None
        for arm in arms:
            pattern = arm.get("pattern")
            body = _v3_arm_body(arm, ctx)
            if pattern == "_":
                wildcard = f"  return ({body})"
                continue
            if pattern == "None":
                lines.append(f"  if ({tmp} === undefined) return ({body})")
            else:  # Some
                bind = arm.get("bind")
                if bind:
                    b = _ident(bind, "match bind")
                    lines.append(f"  if ({tmp} !== undefined) return (({b}) => ({body}))({tmp})")
                else:
                    lines.append(f"  if ({tmp} !== undefined) return ({body})")
        lines.append(wildcard if wildcard is not None
                     else '  throw new TypeError("non-exhaustive match")')
        lines.append(f"}})({scrutinee})")
        return "\n".join(lines)

    lines = [f"(({tmp}) => {{", f"  switch ({tmp}.kind) {{"]
    wildcard = None
    for arm in node.get("arms") or []:
        pattern = arm.get("pattern")
        body = _v3_arm_body(arm, ctx)
        if pattern == "_":
            wildcard = f"      return ({body})"
            continue
        case = _ident(pattern, "case name")
        lines.append(f"    case {_string(case)}:")
        bind = arm.get("bind")
        if bind:
            bind = _ident(bind, "match bind")
            lines.append(f"      return (({bind}) => ({body}))({tmp}.value)")
        else:
            lines.append(f"      return ({body})")
    if wildcard is None:
        lines.append("    default:")
        lines.append('      throw new TypeError("non-exhaustive match")')
    else:
        lines.append("    default:")
        lines.append(wildcard)
    lines.append("  }")
    lines.append(f"}})({scrutinee})")
    return "\n".join(lines)



def _v3_stmt(node: dict, ctx: _TsV3Context, out: list[str], indent: int, *, test_mode: bool) -> None:
    step = node.get("step")
    if step in ("let", "assign"):
        name = _ident(node.get("name"), "binding")
        value = _v3_expr(node.get("value"), ctx)
        if step == "let":
            keyword = "let" if node.get("mutable") else "const"
            out.append(f"{'  ' * indent}{keyword} {name} = {value}")
        else:
            out.append(f"{'  ' * indent}{name} = {value}")
    elif step == "return":
        if node.get("expr") is None:
            out.append(f"{'  ' * indent}return")
        else:
            out.append(f"{'  ' * indent}return {_v3_expr(node['expr'], ctx)}")
    elif step == "if":
        out.append(f"{'  ' * indent}if ({_v3_expr(node['cond'], ctx)}) {{")
        for child in node.get("then") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        if node.get("else"):
            out.append(f"{'  ' * indent}}} else {{")
            for child in node["else"]:
                _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{'  ' * indent}}}")
    elif step == "while":
        out.append(f"{'  ' * indent}while ({_v3_expr(node['cond'], ctx)}) {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{'  ' * indent}}}")
    elif step == "for":
        bind = _ident(node.get("bind"), "loop binding")
        out.append(f"{'  ' * indent}for (const {bind} of {_v3_expr(node['iterable'], ctx)}) {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{'  ' * indent}}}")
    elif step == "let_pattern":
        value = _v3_expr(node.get("value"), ctx)
        names = [_ident(n, "binding") for n in node.get("names") or []]
        if node.get("pattern") == "record":
            out.append(f"{'  ' * indent}const {{ {', '.join(names)} }} = {value}")
        else:
            rest = node.get("rest")
            parts = ", ".join(names + ([f"...{_ident(rest, 'binding')}"] if rest else []))
            out.append(f"{'  ' * indent}const [{parts}] = {value}")
    elif step == "expr":
        out.append(f"{'  ' * indent}{_v3_expr(node['expr'], ctx)}")
    elif step == "assert":
        if test_mode:
            out.append(f"{'  ' * indent}expect({_v3_expr(node['expr'], ctx)}).toBeTruthy()")
        else:
            out.append(
                f"{'  ' * indent}if (!({_v3_expr(node['expr'], ctx)})) "
                f'throw new Error("assertion failed")'
            )
    else:
        raise EmitError(f"unsupported fn statement step {step!r}")


def _emit_ts_types(types: dict) -> list[str]:
    lines: list[str] = []
    for name, spec in types.items():
        name = _ident(name, "type name")
        if spec.get("kind") == "record":
            lines.append(f"export interface {name} {{")
            for field, ftype in (spec.get("fields") or {}).items():
                lines.append(f"  {_ident(field, 'record field')}: {_ts_v3_type(ftype)}")
            lines.append("}")
        else:
            cases = spec.get("cases") or []
            lines.append(f"export type {name} =")
            for index, case in enumerate(cases):
                cname = _ident(case.get("name"), "case name")
                if case.get("payload") is None:
                    member = f"{{ kind: {_string(cname)} }}"
                else:
                    member = f"{{ kind: {_string(cname)}; value: {_ts_v3_type(case['payload'])} }}"
                lines.append(("  | " if index else "  ") + member)
            lines.append("")
            for case in cases:
                cname = _ident(case.get("name"), "case name")
                payload = case.get("payload")
                if payload is None:
                    lines.append(f"export function {cname}(): {name} {{")
                    lines.append(f"  return {{ kind: {_string(cname)} }}")
                else:
                    lines.append(f"export function {cname}(value: {_ts_v3_type(payload)}): {name} {{")
                    lines.append(f"  return {{ kind: {_string(cname)}, value }}")
                lines.append("}")
                lines.append("")
        lines.append("")
    return lines


def _emit_ts_functions(functions: list, types: dict, externs: list) -> list[str]:
    ctx = _TsV3Context(types, functions, externs)
    lines: list[str] = []
    for fn in functions:
        name = _ident(fn.get("name"), "function name")
        params = ", ".join(
            f"{_ident(p.get('name'), 'parameter name')}: {_ts_v3_type(p.get('type'))}"
            for p in fn.get("params") or []
        )
        returns = _ts_v3_type(fn.get("returns"))
        lines.append(f"export function {name}({params}): {returns} {{")
        if not fn.get("body"):
            lines.append("  // (empty body)")
        else:
            for stmt in fn["body"]:
                _v3_stmt(stmt, ctx, lines, 2, test_mode=False)
        lines.append("}")
        lines.append("")
    return lines


def _emit_ts_externs(externs: list) -> list[str]:
    lines: list[str] = []
    for ext in externs:
        name = _ident(ext.get("name"), "extern name")
        params = ", ".join(
            f"{_ident(p.get('name'), 'extern parameter name')}: {_ts_v3_type(p.get('type'))}"
            for p in ext.get("params") or []
        )
        returns = _ts_v3_type(ext.get("returns"))
        bodies = ext.get("bodies") or {}
        if "ts" not in bodies:
            raise EmitError(
                f"extern `{name}` has no @ts body — not portable to this backend "
                f"(available: {', '.join(sorted(bodies)) or 'none'})"
            )
        lines.append(f"export function {name}({params}): {returns} {{")
        body = bodies["ts"].strip()
        if body:
            for line in body.splitlines() or [""]:
                lines.append("  " + line)
        else:
            lines.append("  // (empty @ts body)")
        lines.append("}")
        lines.append("")
    return lines


def _emit_ts_tests(tests: list, types: dict, functions: list, externs: list) -> list[str]:
    ctx = _TsV3Context(types, functions, externs)
    lines: list[str] = []
    for test in tests:
        lines.append(f"it({_string(test.get('name'))}, () => {{")
        if not test.get("body"):
            lines.append("  // (empty test body)")
        else:
            for stmt in test["body"]:
                _v3_stmt(stmt, ctx, lines, 2, test_mode=True)
        lines.append("})")
        lines.append("")
    return lines


def _context_augmentation(components: list) -> list[str]:
    """Typed committed-view access: `ctx.<key>` for every key the file touches.

    Provisions *and* requirements. A component reads a required service as
    `ctx.<key>` exactly as it reads one it provides, and cordis's `Context`
    has no such member until something declares it — so augmenting with
    provisions alone typechecks a component that happens to provide what it
    needs and rejects every real consumer. That is the TypeScript instance of
    the rust `requires` bug (docs/conformance.md); both were invisible to an
    emit-only sweep. Repeating a key across emitted files is safe: identical
    interface members merge.
    """
    keys: dict[str, str] = {}
    for component in components:
        for key, service in (component.get("provides") or {}).items():
            if key in keys and keys[key] != service:
                raise EmitError(f"provision key {key!r} bound to two services (G2)")
            keys[key] = service
    for component in components:
        for key, service in (component.get("requires") or {}).items():
            if key in keys and keys[key] != service:
                raise EmitError(
                    f"service key {key!r} is required as {service!r} but "
                    f"provided as {keys[key]!r}")
            keys[key] = service
    if not keys:
        return []
    return (["declare module 'cordis' {", "  interface Context {"]
            + [f"    {key}: {service}" for key, service in keys.items()]
            + ["  }", "}", ""])


def _emit_v1(ir: dict, *, runtime_import: str) -> str:
    """Emit a v1/v2 component module (docs/backend-ir.md)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be an object")
    if ir.get("ir_version") not in (1, 2):
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

    out.extend(_context_augmentation(components))

    seen = set()
    for component in components:
        if component.get("name") in seen:
            raise EmitError(f"duplicate component name: {component.get('name')!r}")
        seen.add(component.get("name"))
        out.extend(_component(component, services))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _emit_v3(ir: dict, *, runtime_import: str) -> str:
    """Emit an IR v3 module: types, pure functions, externs, tests, and any
    v1-shaped component bodies carried alongside them."""
    services = ir.get("services") or {}
    components = ir.get("components") or []
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    if not components and not types and not functions and not externs and not tests:
        raise EmitError("IR document has no components, types, functions, externs, or tests")

    out: list[str] = [
        "// Generated by revl backends/typescript/emit.py — do not edit.",
        "// Target runtime: cordis v4 (https://github.com/cordiverse/cordis).",
        "import type { Context } from 'cordis'",
        f"import {{ host }} from '{runtime_import}'",
    ]
    if tests:
        out.append("import { expect, it } from 'vitest'")
    out.append("")

    if types:
        out.extend(_emit_ts_types(types))
    if externs:
        out.extend(_emit_ts_externs(externs))
    if functions:
        out.extend(_emit_ts_functions(functions, types, externs))
    if tests:
        out.extend(_emit_ts_tests(tests, types, functions, externs))

    # Service interfaces (coeffect interfaces, DESIGN.md §3.1).
    for sname, service in services.items():
        _ident(sname, "service")
        out.append(f"export interface {sname} {{")
        for mname, method in (service.get("methods") or {}).items():
            _ident(mname, "method")
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

    out.extend(_context_augmentation(components))

    seen = set()
    for component in components:
        if component.get("name") in seen:
            raise EmitError(f"duplicate component name: {component.get('name')!r}")
        seen.add(component.get("name"))
        out.extend(_component(component, services))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# A `fault test` is executed by driving a real activation and inspecting the
# runtime's residue afterwards (docs/fault-tests.md).  The cordis (TS) tier
# has no such driver, so it is refused loudly instead of being dropped on the
# floor: a silently-missing fault test is a guarantee nobody is checking.
def _refuse_fault_tests(ir) -> None:
    fault_tests = (ir or {}).get("fault_tests") or []
    if not fault_tests:
        return
    names = ", ".join(repr(unit.get("name")) for unit in fault_tests)
    raise EmitError(
        f"fault tests do not lower to the cordis (TS) tier ({names}) — `fault test` runs "
        f"on the python reference tier only (docs/fault-tests.md). Compile "
        f"this document with --backend py, or move the fault tests to a "
        f"module that is not emitted for this tier."
    )


def emit(ir: dict, *, runtime_import: str = "../runtime.ts") -> str:
    """Emit one TypeScript module for an IR document (docs/backend-ir.md)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be an object")
    _refuse_fault_tests(ir)
    version = ir.get("ir_version")
    # publish a v3 context for the whole document so component/method bodies
    # can render pure 2.0 expressions (see _v3_context_for_components)
    _ACTIVE_V3.append(_TsV3Context(ir.get("types") or {},
                                   ir.get("functions") or [],
                                   ir.get("externs") or []))
    try:
        if version in (1, 2):
            return _emit_v1(ir, runtime_import=runtime_import)
        if version == 3:
            return _emit_v3(ir, runtime_import=runtime_import)
        raise EmitError(f"unsupported ir_version: {version!r}")
    finally:
        _ACTIVE_V3.pop()


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
        print("usage: python3 emit.py [--runtime <path>] <ir.json|->", file=sys.stderr)
        return 2
    # `-` reads the IR from stdin. Callers used to pass `/dev/stdin`, which
    # works on macOS and fails on a GitHub runner with `OSError: [Errno 6] No
    # such device or address` — the emitted-code tests were red in CI for that
    # reason alone.
    if args[0] == "-":
        ir = json.load(sys.stdin)
    else:
        with open(args[0], "r", encoding="utf-8") as handle:
            ir = json.load(handle)
    sys.stdout.write(emit(ir, runtime_import=runtime_import))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

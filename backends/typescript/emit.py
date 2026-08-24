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

# `Int` is 64-bit two's complement (docs/arithmetic.md) and a JS `number` is an
# IEEE double, exact only to 2^53 — it cannot represent `9223372036854775807`
# at all. `Int` is therefore `bigint`, which is arbitrary precision, so this
# tier *imposes* the 64-bit bound (`revlI64`) exactly as python does.
# `Float` is IEEE 754 binary64 and stays `number`: a different type, and the
# two never mix in JS (`1n + 1` throws), so every operation is rendered
# consistently typed from the IR's `operands` annotation.
# `Int32` is a JS `number`: a double holds every i32 exactly (2^53 >> 2^31),
# so the arithmetic is cheap and only the bound needs imposing. It is a
# *distinct* representation from `Int` (bigint) on purpose — bigint and number
# do not mix in JS, which is exactly the width-mixing the checker also forbids
# (docs/arithmetic.md).
TYPE_MAP = {"Str": "string", "Int": "bigint", "Int32": "number",
            "Bool": "boolean", "Float": "number"}

# Members cordis's Context already owns; a provision key colliding with one
# would shadow framework API (or be refused by the runtime). The host knows
# its own surface, so this rejection lives in the backend, not the frontend.
CONTEXT_MEMBERS = {
    "root", "fiber", "registry", "reflect", "events", "logger", "runtime",
    "effect", "extend", "isolate", "intercept", "inject", "plugin",
    "on", "once", "emit", "parallel", "serial", "bail", "waterfall",
    "get", "set", "provide", "accessor", "mixin", "baseUrl",
}


def _split_ts_types(inner: str) -> list[str]:
    """Split a type argument list on commas outside `[...]` / `(...)`."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(inner):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i].strip())
            start = i + 1
    parts.append(inner[start:].strip())
    return parts


def _split_fn_type(name: str) -> "tuple[list[str], str] | None":
    """`"(Int, Str) -> Bool"` -> `(["Int", "Str"], "Bool")`, else None.

    A function type (docs/function-types.md) is the one surface type not
    spelled `Head[Args]`, so it is recognised before the generic path.
    """
    if not name.startswith("("):
        return None
    depth = 0
    for i, ch in enumerate(name):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
            if depth == 0:
                rest = name[i + 1:].lstrip()
                if not rest.startswith("->"):
                    return None
                inner = name[1:i].strip()
                return (_split_ts_types(inner) if inner else []), rest[2:].strip()
    return None


def _ts_type(name: object) -> str:
    """Surface type -> TS type (IR v1/A6). Unknown names map to `unknown`."""
    if not isinstance(name, str) or not name:
        return "unknown"
    if name in TYPE_MAP:
        return TYPE_MAP[name]
    fn = _split_fn_type(name.strip())
    if fn is not None:
        # revl `(Int, Str) -> Bool` is TS `(a0: number, a1: string) => boolean`.
        # TS function types *require* parameter names and revl's do not carry
        # them, so positional placeholders are supplied here; they are not
        # observable at any call site.
        params, returns = fn
        rendered = ", ".join(f"a{i}: {_ts_type(p)}" for i, p in enumerate(params))
        return f"(({rendered}) => {_ts_type(returns)})"
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


# Dispatcher conformance (roadmap item 76a). This tier converged to ONE
# expression renderer (`_expr`) covering both IR dialects, so the table below
# has a single entry: every kind the frontend can produce in either position
# must render through it, or be deliberately refused with a named
# tier-limit EmitError — never the "unsupported expression kind"
# fall-through. tests/test_expr_dispatcher_conformance.py checks this table
# against src/revl/lower.py's EXPR_KINDS and against the renderer's source.
# `hole` is refused at the document level by the pre-emit walk.
EXPR_DISPATCHERS: dict[str, frozenset[str]] = {
    "renderer": frozenset({
        "adt", "arrow", "bin", "builtin", "call", "config", "field", "fn",
        "format", "host", "if", "index", "instance-get", "interp", "len",
        "list", "lit", "maplit", "match", "name", "optcall", "optfield",
        "record", "record_update", "req", "spawn", "un", "var",
    }),
}
EXPR_REFUSED: frozenset[str] = frozenset({"hole"})


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
    if isinstance(value, int):
        # An `Int` literal is a BigInt literal. The frontend lexes `1.5`/`1e10`
        # to a python float and `123` to a python int (lexer.py), so the two
        # literal types stay distinguishable all the way down and `123` never
        # renders as a `number` that silently loses precision past 2^53.
        return f"{value}n"
    if isinstance(value, float):
        # A `Float` literal stays IEEE 754 binary64. `json.dumps` keeps the
        # decimal point (`2.0`, not `2`), which is what stops a whole-valued
        # Float literal from being read back as an integer.
        return json.dumps(value)
    if isinstance(value, str):
        return _string(value)
    raise EmitError(f"unsupported literal: {value!r}")


def _float_literal(value: object) -> "str | None":
    """A numeric literal rendered as a JS `number`, or None if not numeric.

    `Int` widens to `Float` in revl (`1.5 + 2` type-checks), and the IR does
    not say *which* operand widened — only that the operation is a Float one.
    Rendering an int-valued literal in a Float position as `2` rather than `2n`
    is what keeps that common case free of a conversion nobody wrote.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return json.dumps(value)


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
# `call`/`field`/`index` targets are parenthesised unless atomic; the
# component dialect and the 2.0 dialect each have their own atomic set
# (`_V3_ATOMIC_KINDS` is defined further down). The one renderer checks
# whichever set fits the branch's shape.
_ATOMIC_KINDS = {"name", "config", "req", "call", "host", "instance-get"}

# The component (v1) dialect owns these kinds: `req`/`config`/`name` resolve
# against the component scope, and `host`/`format` are component-only
# spellings. `call` is deliberately NOT listed — it exists in *both* dialects
# with different shapes (component `target`/`method` vs 2.0 `callee`/`args`),
# so the single renderer dispatches it on SHAPE, never on kind.
_COMPONENT_ONLY_KINDS = {"req", "config", "name", "host", "format"}


def _fn_call(node: dict, ctx: "_Ctx") -> str:
    """`{"kind": "fn", "name": ..., "args": [...]}` — a call to a top-level
    `fn` or `extern`.

    This is how the component lowering spells a call that a 2.0 `fn` body
    spells as a `call` node with a `var` callee (src/revl/lower.py). One
    renderer now covers both dialects, so the kind is enough here; the args
    render through the same `_expr`.
    """
    name = _ident(node.get("name"), "function")
    if name not in ctx.function_names and name not in ctx.extern_names:
        raise EmitError(
            f"call to unknown function {name!r} — no `fn` or `extern` of "
            f"that name is declared in this document"
        )
    args = ", ".join(_expr(arg, ctx) for arg in node.get("args") or [])
    return f"{name}({args})"


def _is_float_expr(node: object) -> bool:
    """Is this expression *syntactically* certain to be a `number`?

    Only what the node itself proves: a Float literal, a `/` (true division
    always yields Float), or a Float-annotated arithmetic node. Everything else
    answers False, which costs a `Number(...)` that is the identity on a value
    that was already a `number`. The emitter has no type environment, so this
    is deliberately a proof, not an inference.
    """
    if not isinstance(node, dict):
        return False
    kind = node.get("kind")
    if kind == "lit":
        value = node.get("value")
        return isinstance(value, float) and not isinstance(value, bool)
    if kind == "bin":
        return node.get("op") == "/" or node.get("operands") == "Float"
    if kind == "un":
        return node.get("op") == "-" and _is_float_expr(node.get("operand"))
    return False


def _float_operand(node: object, ctx: "_Ctx") -> str:
    """Render *node* so the result is a JS `number`.

    Two things arrive in a Float position: an actual Float, and an `Int` that
    revl widened into it (`compatible("Float", "Int")` is true, so `1.5 + 2`
    type-checks). BigInt and number do not mix in JS — `1n + 1` is a TypeError
    — so the Int side has to convert, and the IR says the *operation* is a
    Float one without saying which side widened.

    A numeric literal is re-rendered as a `number` (`2`, not `2n`); anything
    already provably Float is left alone; everything else goes through
    `Number()`, which is exactly the identity on a `number`.
    """
    if isinstance(node, dict) and node.get("kind") == "lit":
        rendered = _float_literal(node.get("value"))
        if rendered is not None:
            return rendered
    rendered = _expr(node, ctx)
    return rendered if _is_float_expr(node) else f"Number({rendered})"


def _int_as_number(node: object, ctx: "_Ctx") -> str:
    """Render an `Int`-typed expression as the JS `number` an index/count API
    needs. `xs[i]`, `slice`, `charAt` and `repeat` all take a `number` on the
    host side while revl types the argument `Int`, so the conversion belongs
    here rather than in every caller."""
    if isinstance(node, dict) and node.get("kind") == "lit":
        value = node.get("value")
        if isinstance(value, int) and not isinstance(value, bool):
            return json.dumps(value)
    return f"Number({_expr(node, ctx)})"


def _expr(node: object, ctx: "_Ctx") -> str:
    """The single expression renderer for both of revl's IR dialects.

    revl mixes two expression dialects inside one component body: the v1
    "component" dialect (`req`, `config`, `host`, `format`, and a `call`
    shaped `target`/`method`) and the 2.0 dialect (`var`, `bin`, `un`,
    `field`, `index`, `builtin`, `if`, `arrow`, `match`, `interp`, ADTs,
    `??`, and a `call` shaped `callee`/`args`). These used to be two functions
    that fell through to each other; the fall-through read the wrong child on
    any kind that lived in both dialects. This renderer owns every kind, and
    for the kinds that exist in both it dispatches on the presence of
    distinguishing keys (SHAPE) — never on the kind alone.

    `ctx.component_scope` is the component `_Scope` while rendering a component
    or method body and None in a pure 2.0 fn/test body; the component-only
    kinds require it and refuse clearly when it is absent (a tier limit is an
    explicit refusal, not a silent fall-through).
    """
    if not isinstance(node, dict) or "kind" not in node:
        raise EmitError(f"malformed expression: {node!r}")
    # An implicit Int -> Float coercion site (docs/arithmetic.md): Int is a
    # bigint here and Float a number, so without the conversion `ident(3)`
    # did not just disagree across tiers — it computed the wrong answer
    # (`3n === 3` is false). The frontend marker makes the coercion emit-able.
    if node.get("widen") == "Float":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"Number({_expr(inner, ctx)})"
    # An Int32 -> Int widening site (docs/arithmetic.md): Int32 is a `number`
    # and Int a `bigint`, and the two never mix in JS, so the lossless widening
    # is spelled `BigInt(...)` exactly where the frontend marked it.
    if node.get("widen") == "Int":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"BigInt({_expr(inner, ctx)})"
    kind = node["kind"]
    scope = ctx.component_scope

    # ---- kinds shared by both dialects (rendered identically) ----
    if kind == "lit":
        return _literal(node.get("value"))

    if kind == "fn":
        return _fn_call(node, ctx)

    if kind == "record":
        fields = ", ".join(
            f"{_ident(k, 'record field')}: {_expr(v, ctx)}"
            for k, v in node.get("fields") or []
        )
        return "{" + fields + "}"

    if kind == "record_update":
        # functional record update (docs/records.md §2): spread the base,
        # then let the updated fields override it — a fresh object either way
        base = _expr(node.get("base"), ctx)
        overrides = ", ".join(
            f"{_ident(k, 'record field')}: {_expr(v, ctx)}"
            for k, v in node.get("updates") or []
        )
        return "{ ..." + base + (f", {overrides}" if overrides else "") + " }"

    if kind == "list":
        return "[" + ", ".join(_expr(item, ctx) for item in node.get("items") or []) + "]"

    if kind == "maplit":
        # `Map.empty()` (docs/stdlib-2.0.md §Map)
        return "new Map()"

    if kind == "adt":
        # tagged ADT value (Result / user variant): `{ kind: "Ok", value: x }`
        # or `{ kind: "Missing" }`. Opt is not tagged (value | undefined).
        case = _string(node["case"])
        args = node.get("args") or []
        if args:
            return f"{{ kind: {case}, value: {_expr(args[0], ctx)} }}"
        return f"{{ kind: {case} }}"

    if kind == "call":
        # THE dangerous kind: `call` exists in both dialects with different
        # shapes, so it is dispatched on SHAPE, never on kind. The component
        # form carries `target`/`method` (a method call on a service); the 2.0
        # form carries `callee`/`args` (a first-class call). A v3-shaped call
        # reaches component positions too (the `Some(..)` the frontend injects
        # for `T` -> `Opt[T]` is one). Keying on kind alone once read `method`
        # off a v3 call and reported the resulting `None` as a bad identifier
        # (docs/conformance.md names this as the split's real failure).
        if "target" in node:
            target = node.get("target")
            method = _ident(node.get("method"), "method")
            target_ts = _expr(target, ctx)
            if not (isinstance(target, dict) and target.get("kind") in _ATOMIC_KINDS):
                target_ts = f"({target_ts})"
            args = ", ".join(_expr(arg, ctx) for arg in node.get("args") or [])
            return f"{target_ts}.{method}({args})"
        callee_node = node.get("callee")
        callee = _expr(callee_node, ctx)
        if not (isinstance(callee_node, dict) and callee_node.get("kind") in _V3_ATOMIC_KINDS):
            callee = f"({callee})"
        args = ", ".join(_expr(arg, ctx) for arg in node.get("args") or [])
        return f"{callee}({args})"

    # ---- component (v1) dialect kinds — each needs a component scope ----
    if kind in _COMPONENT_ONLY_KINDS:
        if scope is None:
            raise EmitError(
                f"{kind!r} expression is only valid inside a component or "
                f"method body, but no component scope is in effect"
            )
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
            # Committed-view access: resolved through the fiber's snapshot, so
            # it stays readable during this component's own teardown (R3).
            return f"ctx.{_ident(name, 'requirement')}"
        if kind == "host":
            fn = node.get("fn")
            if not isinstance(fn, str) or not all(IDENT_RE.match(p) for p in fn.split(".")):
                raise EmitError(f"invalid host builtin: {fn!r}")
            args = ", ".join(_expr(arg, ctx) for arg in node.get("args") or [])
            return f"host.{fn}({args})"
        # kind == "format"
        template = node.get("template")
        if not isinstance(template, str):
            raise EmitError(f"format template must be a string: {template!r}")
        args = [_expr(arg, ctx) for arg in node.get("args") or []]
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

    # ---- 2.0 dialect kinds ----
    if kind == "var":
        return _v3_var(node, ctx)

    if kind == "bin":
        if node.get("op") == "??":
            return f"({_expr(node['left'], ctx)} ?? {_expr(node['right'], ctx)})"
        # revl has ONE equality and it is structural (syntax-2.0 §3.4). JS `===`
        # is identity for objects and arrays, so lowering to it made
        # `{a: 1} == {a: 1}` and `[1] == [1]` false on this tier and true on
        # python — a silent wrong answer, not a refusal. Java already did this
        # correctly via `Objects.equals`; `revlEq` is the same idea.
        if node.get("op") in _TS_EQUALITY_OPS:
            left = _expr(node["left"], ctx)
            right = _expr(node["right"], ctx)
            call = f"revlEq({left}, {right})"
            return call if node["op"] in ("==", "===") else f"(!{call})"
        op = _TS_V3_BIN_OPS.get(node.get("op"))
        if op is None:
            raise EmitError(f"unsupported binary operator {node.get('op')!r}")
        operands = node.get("operands")
        if op == "/":
            # `/` is TRUE division and yields Float even on two Ints
            # (docs/arithmetic.md), so both sides become `number` whatever they
            # were. That keeps it IEEE: a zero divisor gives Infinity/NaN — a
            # *value*, never a throw — where BigInt `/` would raise RangeError.
            # `Number()` on a bigint past 2^53 rounds; that is inherent to a
            # binary64 result and is the same rounding rust's `as f64` does.
            return (f"({_float_operand(node['left'], ctx)} / "
                    f"{_float_operand(node['right'], ctx)})")
        if operands == "Float":
            # A Float operation: every operand is a `number`, including an Int
            # that widened into it.
            return (f"({_float_operand(node['left'], ctx)} {op} "
                    f"{_float_operand(node['right'], ctx)})")
        left, right = _expr(node["left"], ctx), _expr(node["right"], ctx)
        if op in ("+", "-", "*") and operands == "Int":
            # Int is bounded 64-bit and overflow TRAPS (docs/arithmetic.md).
            # BigInt is arbitrary precision, so — exactly like python — this
            # tier has to *impose* the bound rather than detect it; without the
            # check a program that faults on rust/java/go would quietly grow a
            # 65-bit value here.
            return f"revlI64({left} {op} {right})"
        if op in ("+", "-", "*") and operands == "Int32":
            # Int32 is a `number`; the double result holds the exact i32 product
            # up to 2^53, so `revlI32` only has to re-impose the 32-bit bound
            # (docs/arithmetic.md).
            return f"revlI32({left} {op} {right})"
        # `%` on two bigints is already the TRUNCATED remainder (sign of the
        # dividend), which is what `%` means (§0), and it throws RangeError on
        # a zero divisor — the fault every tier gives for integer remainder at
        # zero, where the old `number` lowering returned NaN.
        return f"({left} {op} {right})"

    if kind == "un":
        operand = _expr(node.get("operand"), ctx)
        if node.get("op") == "!":
            return f"(!{operand})"
        if node.get("op") == "-":
            if node.get("operands") == "Int":
                # BigInt negation is unbounded; re-impose the i64 bound so
                # negating Int.MIN traps like every other tier (arithmetic.md)
                return f"revlI64(-{operand})"
            if node.get("operands") == "Int32":
                # `0 - Int32.MIN` overflows i32; re-impose the 32-bit bound.
                return f"revlI32(-{operand})"
            return f"(-{operand})"
        raise EmitError(f"unsupported unary operator {node.get('op')!r}")

    if kind == "field":
        target_node = node.get("target")
        target = _expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        return f"{target}.{_ident(node.get('name'), 'field')}"

    if kind == "index":
        target_node = node.get("target")
        target = _expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        # The index is an `Int` (bigint) and JS indexes with a `number`; TS
        # refuses a bigint index outright ("cannot be used as an index type").
        return f"{target}[{_int_as_number(node['index'], ctx)}]"

    if kind == "len":
        # `xs.length` in field position (lower.py emits `len` rather than
        # `field` when the receiver is a sized type). revl types it `Int`. For
        # a `Str` the count is in code points, not UTF-16 units, so it routes
        # through `revlLen` (which leaves a List/Bytes receiver on `.length`).
        target_node = node.get("target")
        target = _expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        return f"revlLen({target})"

    if kind == "builtin":
        target_node = node.get("target")
        target = _expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        arg_nodes = list(node.get("args") or [])
        args = [_expr(a, ctx) for a in arg_nodes]
        return _ts_builtin(node.get("method"), target, args, arg_nodes, ctx)

    if kind == "if":
        return (
            f"({_expr(node['cond'], ctx)} ? "
            f"{_expr(node['then'], ctx)} : {_expr(node['else'], ctx)})"
        )

    if kind == "arrow":
        # An arrow the checker typed carries its parameter types in the IR
        # (docs/function-types.md), and they are emitted verbatim — this is a
        # real TS signature, not an admission.
        #
        # An arrow with *no* expected type and no annotations is still on the
        # checker's unchecked frontier (typecheck.py header), and there the
        # parameter is written `any` deliberately: the compiler has no type,
        # and a guess would be worse than the admission. `strict` rejects only
        # an *implicit* any, so writing it is also what makes the file compile.
        names = node.get("params") or []
        declared = node.get("param_types") or []
        declared = list(declared) + [None] * (len(names) - len(declared))
        params = ", ".join(
            f"{_ident(p, 'arrow parameter')}: "
            f"{_ts_v3_type(t) if isinstance(t, str) and t not in ('Any', 'Never') else 'any'}"
            for p, t in zip(names, declared))
        return f"(({params}) => ({_expr(node['body'], ctx)}))"

    if kind == "match":
        return _v3_match_expr(node, ctx)

    if kind == "interp":
        parts = node.get("parts") or []
        segs = ["`"]
        for part_kind, value in parts:
            if part_kind == "text":
                segs.append(_template_text(value))
            else:  # ["expr", ir_node] — a full expression
                segs.append("${" + _expr(value, ctx) + "}")
        segs.append("`")
        return "".join(segs)

    if kind == "optfield":
        target_node = node.get("target")
        target = _expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        return f"{target}?.{_ident(node.get('name'), 'optional field')}"

    if kind == "optcall":
        target_node = node.get("target")
        target = _expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        method = node.get("method")
        arg_nodes = list(node.get("args") or [])
        int_args = _TS_INT_ARG_BUILTINS.get(method, ())
        args = ", ".join(
            _int_as_number(a, ctx) if index in int_args else _expr(a, ctx)
            for index, a in enumerate(arg_nodes))
        call = f"{target}?.{_ident(method, 'optional method')}({args})"
        if method in _TS_INT_RESULT_BUILTINS:
            # `?.` short-circuits to `undefined`, and `BigInt(undefined)`
            # throws — so the Int conversion cannot wrap the whole chain, only
            # the result when there is one. revl types this `Opt[Int]`.
            return ("((v: number | undefined) => v === undefined ? undefined : BigInt(v))"
                    f"({call})")
        return call

    if kind == "spawn":
        # instance-parametric components (docs/design-v2-instances.md): a spawn
        # is an acquisition that plugs the target component as a *child fiber* of
        # the spawner, each provided key isolated into its own fresh LOCAL realm
        # (an unlabelled `ctx.isolate`, a distinct identity per spawn — so two
        # instances never collide on a provision). `spawn(ctx, Worker, {config},
        # [realms])` returns a disposable handle; the step's `undo` disposes it,
        # tearing down that child fiber (its own nested teardown scope). The
        # target is a module-level plugin dict emitted like any component.
        target = node.get("component")
        if not isinstance(target, str) or not target.isidentifier():
            raise EmitError(f"bad spawn component {target!r}")
        cfg = "{" + ", ".join(
            f"{_string(k)}: {_expr(v, ctx)}"
            for k, v in (node.get("config") or {}).items()) + "}"
        realms = "[" + ", ".join(
            _string(r) for r in node.get("realms") or []) + "]"
        return f"spawn(ctx, {target}, {cfg}, {realms})"

    if kind == "instance-get":
        # instance-parametric components (docs/design-v2-instances.md): `s.<key>`
        # reads a provision back off a spawn handle. `target` is a
        # `SpawnHandle`, whose `.get(key)` resolves the key through the
        # instance's OWN private local realm — the realm the matching `spawn`
        # isolated it into (cordis' per-key `ctx.isolate`). Only the spawner
        # holding this handle reaches it: a sibling instance, isolated into a
        # different local realm, and the root cannot (supervision-tree
        # addressing). `service` is frozen inline on the node but needs no
        # emission here — cordis exposes the provision by key on the instance's
        # context, so the key alone resolves it (mirrors backends/python/emit.py
        # and the runtime `SpawnHandle.get`, backends/typescript/runtime.ts).
        target = _expr(node.get("target"), ctx)
        key = node.get("key")
        if not isinstance(key, str) or not key.isidentifier():
            raise EmitError(f"bad instance-get key {key!r}")
        return f"{target}.get({_string(key)})"

    raise EmitError(f"unsupported expression kind {kind!r}")


def _method_body(steps: list, ctx: "_Ctx", indent: str,
                 method_is_async: bool = False) -> list[str]:
    """Steps inside a provide-method body.

    These run while the component is ACTIVE; `effect` steps go through
    `ctx.effect` so their undos join the component fiber's accumulator. An
    `async` service operation (services 2.0 §5) lowers to an `async` method,
    whose body may `await` a host async value or an instance disposal (A1);
    a `sync` method rejects `await`, matching the reference tier.
    """
    scope = ctx.component_scope
    lines: list[str] = []
    for step in steps:
        kind = step.get("step")
        if kind == "let":
            name = scope.bind(step["name"])
            keyword = "let" if step.get("mutable") else "const"
            lines.append(f"{indent}{keyword} {name} = {_expr(step['value'], ctx)}")
        elif kind == "assign":
            lines.append(f"{indent}{_ident(step['name'], 'binding')} = "
                         f"{_expr(step['value'], ctx)}")
        elif kind == "return":
            # a void service operation returns nothing at all
            if step.get("expr") is None:
                lines.append(f"{indent}return")
            else:
                lines.append(f"{indent}return {_expr(step['expr'], ctx)}")
        elif kind in ("effect", "let-effect"):
            bind = None
            if kind == "let-effect":
                bind = scope.bind(step["bind"])
                lines.append(f"{indent}let {bind}: any")
            acquire = _expr(step["acquire"], ctx)
            undo = _expr(step["undo"], ctx)
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
                lines.append(f"{indent}  {_expr(step['expr'], ctx)}")
                lines.append(f"{indent}  return () => {_expr(step['compensate'], ctx)}")
                lines.append(f"{indent}}})")
            else:
                lines.append(f"{indent}{_expr(step['expr'], ctx)}")
        elif kind == "await":
            # A1: legal only in an `async` provide-method (services 2.0 §5); a
            # sync method has no in-flight window. The await lands, then control
            # returns to the caller — used here so a request can `await
            # handle.dispose()` and see an instance reclaimed synchronously.
            if not method_is_async:
                raise EmitError(
                    "await steps are not allowed inside sync provide-method bodies (A1)")
            lines.append(f"{indent}await {_expr(step['expr'], ctx)}")
        elif kind == "provide":
            raise EmitError("provide steps are not allowed inside method bodies")
        else:
            raise EmitError(f"unknown step in method body: {kind!r}")
    return lines


def _provide_impl(step: dict, ctx: "_Ctx", services: dict, indent: str) -> list[str]:
    scope = ctx.component_scope
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
        # services 2.0 §5: an async operation lowers to an async method, whose
        # body may await (A1). The flag lives on the service declaration, the
        # single source of truth for the operation's shape.
        method_is_async = bool(declared[name].get("async"))
        prefix = "async " if method_is_async else ""
        lines.append(f"{indent}{prefix}{name}({sig}) {{")
        lines.extend(_method_body(method.get("body") or [], ctx.with_scope(body_scope),
                                  indent + "  ", method_is_async))
        lines.append(f"{indent}}},")
    return lines


def _component_body(component: dict, services: dict, indent: str, doc_ctx: "_Ctx") -> list[str]:
    """The activation body, lowered into one ctx.effect generator."""
    ctx = doc_ctx.with_scope(_Scope(component))
    lines: list[str] = []
    for step in component.get("body") or []:
        _component_step(step, component, services, ctx, indent, lines)
    return lines


def _component_step(step: dict, component: dict, services: dict, ctx: "_Ctx",
                    indent: str, lines: list[str]) -> None:
    """One step of the activation body, appended to `lines`.

    Recursive because `if` branches hold ordinary body steps.
    """
    scope = ctx.component_scope
    provides = component.get("provides") or {}
    kind = step.get("step")
    if kind in ("let-effect", "effect"):
        acquire = _expr(step["acquire"], ctx)
        if kind == "let-effect":
            bind = scope.bind(step["bind"])
            lines.append(f"{indent}const {bind} = {acquire}")
        else:
            lines.append(f"{indent}{acquire}")
        # `undo` may reference the binding; it types in teardown mode —
        # by construction it cannot register further effects.
        undo = _expr(step["undo"], ctx)
        lines.append(f"{indent}yield () => {undo}")
    elif kind == "emit":
        lines.append(f"{indent}{_expr(step['expr'], ctx)}")
        if step.get("compensate") is not None:
            # v1/A5: compensation accumulates LIFO like an inverse
            lines.append(f"{indent}yield () => {_expr(step['compensate'], ctx)}")
    elif kind == "await":
        # v1/A1: the await lands (inertia), then the yield closes the
        # iteration so a divert during the await skips every later step
        lines.append(f"{indent}await {_expr(step['expr'], ctx)}")
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
        lines.extend(_provide_impl(step, ctx, services, indent + "  "))
        lines.append(f"{indent}}} satisfies {_ident(step['service'], 'service')})")
    elif kind == "if":
        # An activation guard (A8). Branches hold ordinary body steps, so a
        # `yield` inside one keeps its place in the accumulator's LIFO order —
        # being a generator body makes that work with no handling here.
        lines.append(f"{indent}if ({_expr(step['cond'], ctx)}) {{")
        for nested in step.get("then") or []:
            _component_step(nested, component, services, ctx, indent + "  ", lines)
        if step.get("else"):
            lines.append(f"{indent}}} else {{")
            for nested in step["else"]:
                _component_step(nested, component, services, ctx, indent + "  ", lines)
        lines.append(f"{indent}}}")
    elif kind == "fail":
        # A8: refusing activation is a throw out of the body. Whatever the
        # accumulator already holds is reverted by the runtime, so a partly
        # activated component still leaves no residue (R4).
        lines.append(f"{indent}throw new Error({_expr(step['message'], ctx)})")
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


def _component(component: dict, services: dict, doc_ctx: "_Ctx") -> list[str]:
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
    lines.extend(_component_body(component, services, "      ", doc_ctx))
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
    # Int is `bigint` — see TYPE_MAP for why. Float is a separate type.
    "Int": "bigint",
    "Int32": "number",  # a double holds every i32 exactly (docs/arithmetic.md)
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
        if ch in "[(":
            depth += 1
        elif ch in "])":
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
    fn = _split_fn_type(name)
    if fn is not None:
        # revl `(Int, Str) -> Bool` is TS `((a0: number, a1: string) => boolean)`.
        # TS function types *require* parameter names and revl's do not carry
        # them, so positional placeholders are supplied; they are not
        # observable anywhere (docs/function-types.md).
        params, returns = fn
        rendered = ", ".join(f"a{i}: {_ts_v3_type(p)}" for i, p in enumerate(params))
        return f"(({rendered}) => {_ts_v3_type(returns)})"
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


class _Ctx:
    """The single rendering context threaded through `_expr` for both dialects.

    It carries the document-level 2.0 type context — the type/function/extern
    names, the variant case names, and the shared match-temp counter — AND an
    optional `component_scope`: the component `_Scope` in effect while
    rendering a component or method body, or None in a pure 2.0 fn/test body.
    The component-dialect kinds (`req`/`config`/`name`/`host`/`format`) resolve
    against that scope; the 2.0 kinds ignore it.

    `with_scope` returns a *view* that shares the type context and the counter
    cell but rebinds `component_scope`, so entering a method or match-arm body
    is cheap and never desynchronises the match-temp numbering.
    """

    def __init__(self, types: dict, functions: list, externs: list,
                 component_scope=None, counter=None) -> None:
        self.types = types or {}
        self.function_names = {fn.get("name") for fn in functions or []}
        self.extern_names = {ext.get("name") for ext in externs or []}
        self.case_names: set[str] = set()
        for spec in self.types.values():
            if spec.get("kind") == "variant":
                for case in spec.get("cases") or []:
                    self.case_names.add(case.get("name"))
        # A one-cell list so every derived view shares the same counter.
        self._counter = counter if counter is not None else [0]
        self.component_scope = component_scope

    def new_match_tmp(self) -> str:
        # `$` is not in revl's identifier alphabet, so this cannot collide.
        self._counter[0] += 1
        return f"$revl_match_{self._counter[0]}"

    def with_scope(self, scope) -> "_Ctx":
        view = _Ctx.__new__(_Ctx)
        view.types = self.types
        view.function_names = self.function_names
        view.extern_names = self.extern_names
        view.case_names = self.case_names
        view._counter = self._counter
        view.component_scope = scope
        return view


def _v3_var(node: dict, ctx: "_Ctx") -> str:
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


def _ts_builtin(method, target: str, args: list, arg_nodes: list, ctx: "_Ctx") -> str:
    """The stdlib surface (docs/stdlib-2.0.md) as idiomatic TS; `push` and
    `concat` are persistent (value semantics), matching the py backend.

    This is the tier's widest Int boundary. revl types `length`, `indexOf` and
    `charCodeAt` as `Int` while the underlying JS APIs answer `number`, and
    types the index arguments of `slice`, `charAt` and `repeat` as `Int` while
    JS needs a `number`. Neither direction is implicit in JS, so both convert
    here — `BigInt(...)` on the way out, `Number(...)` on the way in.
    """
    # `Str` methods count and index in Unicode code points (docs/strings.md);
    # the JS UTF-16 APIs would count code units. `length`/`slice`/`indexOf` are
    # shared with List/Bytes, so they route through helpers that dispatch on
    # the receiver at runtime; `charAt`/`charCodeAt` are Str-only.
    if method == "length":
        return f"revlLen({target})"
    if method == "push":
        return f"[...{target}, {args[0]}]"
    if method == "slice":
        return f"revlSlice({target}, {args[0]}, {args[1]})"
    if method == "charAt":
        return f"revlCharAt({target}, {args[0]})"
    if method == "charCodeAt":
        return f"revlCharCodeAt({target}, {args[0]})"
    # Integer division and modulo (docs/arithmetic.md). JS `/` is true division
    # and `%` takes the dividend's sign, so every one of these is built rather
    # than inherited — through helpers, so the divisor is evaluated once and a
    # zero divisor throws instead of yielding Infinity/NaN. Returning Infinity
    # where the checker declared `Int` is the same class of unsoundness the
    # `===`-for-equality bug was.
    if method in _TS_INT_ARITH:
        return f"{_TS_INT_ARITH[method]}({target}, {args[0]})"
    if method in _TS_CHECKED_DIV:
        return f"{_TS_CHECKED_DIV[method]}({target}, {args[0]})"
    # Int/Int32 width conversions (docs/arithmetic.md). Int is a bigint and
    # Int32 a number: widening is `BigInt(...)`, narrowing goes through
    # `revlI32(Number(...))`, which re-imposes the 32-bit bound and traps.
    if method == "to_int":
        return f"BigInt({target})"
    if method == "to_int32":
        return f"revlI32(Number({target}))"
    if method == "concat":
        return f"{target}.concat({args[0]})"
    if method == "indexOf":
        return f"revlIndexOf({target}, {args[0]})"
    if method == "split":
        return f"{target}.split({args[0]})"
    if method == "join":
        return f"{target}.join({args[0]})"
    if method == "repeat":
        return f"{target}.repeat({_int_as_number(arg_nodes[0], ctx)})"
    # The Map value type (docs/stdlib-2.0.md §Map): the built-in JS Map,
    # copied on write. There is no expression-form copy, so `set` goes
    # through an immediately-applied closure: operands evaluate exactly
    # once, receiver never mutates.
    if method == "set":
        return (f"(() => {{ const c = new Map({target}); "
                f"c.set({args[0]}, {args[1]}); return c }})()")
    if method == "lookup":
        # Map.get answers undefined when absent: exactly the Opt None case.
        return f"{target}.get({args[0]})"
    if method == "has":
        return f"{target}.has({args[0]})"
    # The iteration/remove step (docs/stdlib-2.0.md §Map). JS `.sort()` is
    # UTF-16 code-unit order, which diverges from the canonical (code-point)
    # order only past U+FFFF — the inline comparator compares code points via
    # Array.from, so supplementary-plane keys sort canonically too.
    # `size` answers number; revl Int is a bigint here, so BigInt() on the
    # way out, exactly as length does. remove copies before deleting.
    if method == "size":
        return f"BigInt({target}.size)"
    if method == "keys":
        return (f"[...{target}.keys()].sort((a, b) => {{ "
                f"const A = Array.from(a), B = Array.from(b); "
                f"for (let i = 0; i < Math.min(A.length, B.length); i++) {{ "
                f"if (A[i] !== B[i]) return A[i] < B[i] ? -1 : 1 }} "
                f"return A.length - B.length }})")
    if method == "remove":
        return (f"(() => {{ const c = new Map({target}); "
                f"c.delete({args[0]}); return c }})()")
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str): Int is a
    # bigint on this tier, and BigInt.prototype.toString is exact decimal.
    if method == "to_str":
        return f"{target}.toString()"
    raise EmitError(f"unknown builtin method {method!r}")


def _v3_arm_body(arm: dict, ctx: "_Ctx") -> str:
    """Render one match arm's body with that arm's payload binding in scope.

    In a `fn` body nothing tracks bindings, but in a component/method body the
    fallback renderer checks that every `name` resolves — and a match arm
    binds its payload for the arm body only. Without this, `match o { Found(v)
    => v }` inside a provide-method reports `v` as unbound.
    """
    bind = arm.get("bind")
    scope = ctx.component_scope
    if not bind or scope is None:
        return _expr(arm.get("body"), ctx)
    arm_scope = scope.child()
    # `.add` rather than `.bind`: the emitted arm is an IIFE parameter, which
    # shadows in TS exactly as the pattern binding shadows in revl, so this is
    # not the rebinding that single-assignment forbids. `with_scope` shares the
    # match-temp counter, so a nested match inside the arm keeps unique temps.
    arm_scope.locals.add(_ident(bind, "match bind"))
    return _expr(arm.get("body"), ctx.with_scope(arm_scope))


def _v3_match_expr(node: dict, ctx: "_Ctx") -> str:
    tmp = ctx.new_match_tmp()
    scrutinee = _expr(node.get("scrutinee"), ctx)
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



def _v3_stmt(node: dict, ctx: _Ctx, out: list[str], indent: int, *, test_mode: bool) -> None:
    step = node.get("step")
    if step in ("let", "assign"):
        name = _ident(node.get("name"), "binding")
        value = _expr(node.get("value"), ctx)
        if step == "let":
            keyword = "let" if node.get("mutable") else "const"
            out.append(f"{'  ' * indent}{keyword} {name} = {value}")
        else:
            out.append(f"{'  ' * indent}{name} = {value}")
    elif step == "return":
        if node.get("expr") is None:
            out.append(f"{'  ' * indent}return")
        else:
            out.append(f"{'  ' * indent}return {_expr(node['expr'], ctx)}")
    elif step == "if":
        out.append(f"{'  ' * indent}if ({_expr(node['cond'], ctx)}) {{")
        for child in node.get("then") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        if node.get("else"):
            out.append(f"{'  ' * indent}}} else {{")
            for child in node["else"]:
                _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{'  ' * indent}}}")
    elif step == "while":
        out.append(f"{'  ' * indent}while ({_expr(node['cond'], ctx)}) {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{'  ' * indent}}}")
    elif step == "for":
        bind = _ident(node.get("bind"), "loop binding")
        out.append(f"{'  ' * indent}for (const {bind} of {_expr(node['iterable'], ctx)}) {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{'  ' * indent}}}")
    elif step == "let_pattern":
        value = _expr(node.get("value"), ctx)
        names = [_ident(n, "binding") for n in node.get("names") or []]
        if node.get("pattern") == "record":
            out.append(f"{'  ' * indent}const {{ {', '.join(names)} }} = {value}")
        else:
            rest = node.get("rest")
            parts = ", ".join(names + ([f"...{_ident(rest, 'binding')}"] if rest else []))
            out.append(f"{'  ' * indent}const [{parts}] = {value}")
    elif step == "expr":
        out.append(f"{'  ' * indent}{_expr(node['expr'], ctx)}")
    elif step == "assert":
        # `expect(x).toBeTruthy()` and a bare `throw` both discard the operand
        # values the emitter already has. An in-file `test` almost always
        # asserts a comparison, so lower equality to vitest's own matcher
        # (which prints a diff) and put both sides in the thrown message
        # otherwise. Mirrors _emit_assert in the python backend.
        expr = node["expr"]
        pad = "  " * indent
        equality = expr.get("kind") == "bin" and expr.get("op") in (
            "==", "===", "!=", "!==")
        if test_mode and equality:
            # Must go through `revlEq`, not vitest's `toStrictEqual`: revl's
            # `==` is IEEE on Float, so `NaN == NaN` is false, while
            # toStrictEqual uses Object.is and calls them equal. An assertion
            # has to use the language's equality or it is testing the wrong
            # thing. The rendered source and both values go in the message,
            # which is what the matcher's diff was buying us.
            left = _expr(expr["left"], ctx)
            right = _expr(expr["right"], ctx)
            want = "false" if expr["op"] in ("!=", "!==") else "true"
            shown = json.dumps(f"{left} {expr['op']} {right}")
            out.append(f"{pad}{{ const l = {left}, r = {right};")
            out.append(f"{pad}  expect(revlEq(l, r), {shown} + "
                       f'"\\n  left  = " + revlShow(l) + '
                       f'"\\n  right = " + revlShow(r)).toBe({want}) }}')
        elif test_mode:
            out.append(f"{pad}expect({_expr(expr, ctx)}).toBeTruthy()")
        elif expr.get("kind") == "bin" and expr.get("op") in (
                "==", "===", "!=", "!==", "<", ">", "<=", ">="):
            op = _TS_V3_BIN_OPS[expr["op"]]
            left = _expr(expr["left"], ctx)
            right = _expr(expr["right"], ctx)
            shown = json.dumps(f"{left} {op} {right}")
            out.append(f"{pad}{{ const l = {left}, r = {right};")
            out.append(f"{pad}  if (!(l {op} r)) throw new Error({shown} + "
                       f'"\\n  left  = " + revlShow(l) + '
                       f'"\\n  right = " + revlShow(r)) }}')
        else:
            out.append(
                f"{pad}if (!({_expr(expr, ctx)})) "
                f'throw new Error("assertion failed")'
            )
    else:
        raise EmitError(f"unsupported fn statement step {step!r}")


_TS_INT_ARITH = {
    "div_trunc": "revlDivTrunc",
    "div_floor": "revlDivFloor",
    "div_euclid": "revlDivEuclid",
    "mod": "revlMod",
}

# The total forms (docs/arithmetic.md): same quotient, Err(reason) at zero.
_TS_CHECKED_DIV = {
    "checked_div_trunc": "revlCheckedDivTrunc",
    "checked_div_floor": "revlCheckedDivFloor",
    "checked_div_euclid": "revlCheckedDivEuclid",
    "checked_mod": "revlCheckedMod",
}

# Builtin methods whose named argument positions revl types `Int` while the JS
# API takes a `number`, and builtins revl types `Int` while the JS API answers
# a `number`. `_ts_builtin` spells each conversion out per method; these tables
# are the same facts in the form the `?.` path needs.
_TS_INT_ARG_BUILTINS = {"slice": (0, 1), "charAt": (0,), "charCodeAt": (0,),
                        "repeat": (0,)}
_TS_INT_RESULT_BUILTINS = {"length", "indexOf", "charCodeAt"}

# Int is 64-bit two's complement and overflow traps (docs/arithmetic.md).
# BigInt is arbitrary precision, so this tier imposes the bound the way python
# does — the message is the one every bounded tier raises, so one guarantee
# does not read as six different bugs.
_REVL_I64_HELPER = """const REVL_I64_MIN = -(2n ** 63n)
const REVL_I64_MAX = 2n ** 63n - 1n
function revlI64(v: bigint): bigint {
  if (v < REVL_I64_MIN || v > REVL_I64_MAX) throw new RangeError('revl: Int overflow')
  return v
}"""

# Int32 is a `number`; the bound is imposed the same way, at 32 bits. The
# double holds every i32 sum/product exactly, so only the range check is needed
# (docs/arithmetic.md).
_REVL_I32_HELPER = """const REVL_I32_MIN = -(2 ** 31)
const REVL_I32_MAX = 2 ** 31 - 1
function revlI32(v: number): number {
  if (v < REVL_I32_MIN || v > REVL_I32_MAX) throw new RangeError('revl: Int32 overflow')
  return v
}"""

# The named integer operations (docs/arithmetic.md), on bigint. BigInt `/`
# already truncates toward zero, so `div_trunc` is native here; `%` on bigint
# already takes the dividend's sign, which is what the truncated remainder
# means. Only `div_floor`, `div_euclid` and `mod` are built.
#
# The divisor is guarded so a zero divisor throws rather than yielding a value:
# integer division has none at zero, and the checker has declared `Int`.
# `revlI64` re-imposes the bound because the one overflowing quotient,
# `-(2n ** 63n) / -1n`, is exactly the value that leaves the range.
_REVL_INT_ARITH_HELPER = """function revlNonZero(b: bigint): bigint {
  if (b === 0n) throw new Error('revl: division by zero')
  return b
}
function revlDivTrunc(a: bigint, b: bigint): bigint {
  return revlI64(a / revlNonZero(b))
}
function revlDivFloor(a: bigint, b: bigint): bigint {
  const d = revlNonZero(b)
  const q = a / d
  return a % d !== 0n && (a < 0n) !== (d < 0n) ? revlI64(q - 1n) : revlI64(q)
}
function revlDivEuclid(a: bigint, b: bigint): bigint {
  const d = revlNonZero(b)
  const q = a / d
  if (a % d >= 0n) return revlI64(q)
  return d > 0n ? revlI64(q - 1n) : revlI64(q + 1n)
}
function revlMod(a: bigint, b: bigint): bigint {
  const m = revlNonZero(b) < 0n ? -b : b
  return ((a % m) + m) % m
}
// The total forms (docs/arithmetic.md): the same quotient as the faulting
// helpers, but a zero divisor yields Err(reason) instead of throwing — `fail`
// is refused in a pure fn, so the error travels as a value. The shape is the
// one the `adt` node renders for a built-in Result: `{ kind, value }`.
function revlCheckedDivTrunc(a: bigint, b: bigint): { kind: "Ok"; value: bigint } | { kind: "Err"; value: string } {
  if (b === 0n) return { kind: "Err", value: 'revl: division by zero' }
  if (a === REVL_I64_MIN && b === -1n) return { kind: "Err", value: 'revl: Int overflow' }
  return { kind: "Ok", value: revlI64(a / b) }
}
function revlCheckedDivFloor(a: bigint, b: bigint): { kind: "Ok"; value: bigint } | { kind: "Err"; value: string } {
  if (b === 0n) return { kind: "Err", value: 'revl: division by zero' }
  if (a === REVL_I64_MIN && b === -1n) return { kind: "Err", value: 'revl: Int overflow' }
  const q = a / b
  return a % b !== 0n && (a < 0n) !== (b < 0n)
    ? { kind: "Ok", value: revlI64(q - 1n) }
    : { kind: "Ok", value: revlI64(q) }
}
function revlCheckedDivEuclid(a: bigint, b: bigint): { kind: "Ok"; value: bigint } | { kind: "Err"; value: string } {
  if (b === 0n) return { kind: "Err", value: 'revl: division by zero' }
  if (a === REVL_I64_MIN && b === -1n) return { kind: "Err", value: 'revl: Int overflow' }
  const q = a / b
  if (a % b >= 0n) return { kind: "Ok", value: revlI64(q) }
  return b > 0n ? { kind: "Ok", value: revlI64(q - 1n) } : { kind: "Ok", value: revlI64(q + 1n) }
}
function revlCheckedMod(a: bigint, b: bigint): { kind: "Ok"; value: bigint } | { kind: "Err"; value: string } {
  if (b === 0n) return { kind: "Err", value: 'revl: division by zero' }
  const m = b < 0n ? -b : b
  return { kind: "Ok", value: ((a % m) + m) % m }
}"""


def _uses_int_arith(node) -> bool:
    """Does anything in this IR call a named integer division or modulo?
    The total (`checked_*`) forms count too: they route through the same
    helpers' tier (and need `revlI64` for the one overflowing quotient)."""
    if isinstance(node, dict):
        if node.get("method") in _TS_INT_ARITH or node.get("method") in _TS_CHECKED_DIV:
            return True
        return any(_uses_int_arith(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_int_arith(v) for v in node)
    return False


def _uses_bounded_int(node) -> bool:
    """Does this IR do Int `+`, `-` or `*`? The bound check travels with the
    module only where it is needed, matching the python backend."""
    if isinstance(node, dict):
        if (node.get("kind") == "bin" and node.get("op") in ("+", "-", "*")
                and node.get("operands") == "Int"):
            return True
        if (node.get("kind") == "un" and node.get("op") == "-"
                and node.get("operands") == "Int"):
            return True
        return any(_uses_bounded_int(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_bounded_int(v) for v in node)
    return False


# `JSON.stringify` THROWS on a BigInt ("Do not know how to serialize a
# BigInt"), and the assert diagnostics render both operand values — so on this
# tier a failing assertion over `Int` would have reported a TypeError from the
# reporter instead of the values that disagreed. This renders revl values
# directly: a bigint as its digits (no `n`, matching every other tier's
# rendering of the same number), and NaN/Infinity as themselves rather than as
# the `null` JSON.stringify turns them into.
_REVL_SHOW_HELPER = """function revlShow(v: unknown): string {
  if (typeof v === 'bigint') return v.toString()
  if (typeof v === 'number') return String(v)
  if (Array.isArray(v)) return '[' + v.map(revlShow).join(', ') + ']'
  if (v !== null && typeof v === 'object') {
    const o = v as Record<string, unknown>
    return '{' + Object.keys(o).map((k) => JSON.stringify(k) + ': ' + revlShow(o[k])).join(', ') + '}'
  }
  return JSON.stringify(v) ?? String(v)
}"""


def _revl_helpers(ir: dict) -> list[str]:
    """The helper functions this document actually needs, in dependency order.

    Emitting them unconditionally would leave dead functions in every module;
    scanning is cheap and keeps the output honest about what it uses. `revlI64`
    comes first because the named integer operations call it.
    """
    out: list[str] = []
    if _uses_equality(ir):
        out.extend([_REVL_EQ_HELPER, ""])
    if _uses_assert(ir):
        out.extend([_REVL_SHOW_HELPER, ""])
    if _uses_bounded_int(ir) or _uses_int_arith(ir):
        out.extend([_REVL_I64_HELPER, ""])
    if _uses_bounded_int32(ir):
        out.extend([_REVL_I32_HELPER, ""])
    if _uses_int_arith(ir):
        out.extend([_REVL_INT_ARITH_HELPER, ""])
    if _uses_str_methods(ir):
        out.extend([_REVL_STR_HELPER, ""])
    return out


def _uses_bounded_int32(node) -> bool:
    """Does this IR need `revlI32` — Int32 `+`/`-`/`*`, an Int32 negation, or a
    `to_int32` narrowing? Emitted only where used, like the i64 bound."""
    if isinstance(node, dict):
        if (node.get("kind") == "bin" and node.get("op") in ("+", "-", "*")
                and node.get("operands") == "Int32"):
            return True
        if (node.get("kind") == "un" and node.get("op") == "-"
                and node.get("operands") == "Int32"):
            return True
        if node.get("kind") == "builtin" and node.get("method") == "to_int32":
            return True
        return any(_uses_bounded_int32(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_bounded_int32(v) for v in node)
    return False


def _uses_assert(node) -> bool:
    """Does this IR carry an `assert` step? Only those render values."""
    if isinstance(node, dict):
        if node.get("step") == "assert":
            return True
        return any(_uses_assert(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_assert(v) for v in node)
    return False


_TS_EQUALITY_OPS = ("==", "===", "!=", "!==")

# Structural equality, matching python's `==` on dicts/lists and java's
# `Objects.equals`. Key order is irrelevant (as in python), arrays and objects
# never compare equal to each other, and NaN/-0 fall through to `===` so they
# behave exactly as they do on the reference tier.
#
# `Int` is a bigint, and `===` is already the right comparison for one: it is
# value equality between bigints (`1n === 1n`) and never conflates one with a
# `number` (`1n === 1` is false), so an `Int` and a `Float` do not compare
# equal by accident. Nested bigints reach the same leaf through the recursion.
_REVL_EQ_HELPER = """function revlEq(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) {
    return false
  }
  const arrA = Array.isArray(a), arrB = Array.isArray(b)
  if (arrA !== arrB) return false
  if (arrA && arrB) {
    const xs = a as unknown[], ys = b as unknown[]
    return xs.length === ys.length && xs.every((x, i) => revlEq(x, ys[i]))
  }
  if (a instanceof Map && b instanceof Map) {
    // revl equality is structural and order-independent (syntax-2.0 §3.4);
    // for maps that means same key set, equal value under every key.
    if (a.size !== b.size) return false
    for (const [k, v] of a.entries()) {
      if (!b.has(k) || !revlEq(v, b.get(k))) return false
    }
    return true
  }
  const ka = Object.keys(a as object), kb = Object.keys(b as object)
  if (ka.length !== kb.length) return false
  return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k)
    && revlEq((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]))
}"""


# A `Str` is a sequence of Unicode code points (docs/strings.md); a JS string
# is UTF-16, so its `.length`/`.charAt`/`.charCodeAt`/`.slice`/`.indexOf` count
# and index in code units — 2 for one astral char. These helpers reinterpret a
# string through its code points (`Array.from` iterates by code point) while
# leaving List/Bytes receivers on their native element operations, so
# `"😀".length()` is 1 and `charCodeAt(0)` is the scalar 128512, not a
# surrogate. Receiver type is not known statically on this tier, so the branch
# is at runtime on `typeof x === "string"`.
_REVL_STR_HELPER = """function revlLen(x: string | ArrayLike<unknown>): bigint {
  return BigInt(typeof x === "string" ? Array.from(x).length : x.length)
}
function revlSlice<T>(x: string | T[], a: bigint, b: bigint): string | T[] {
  const i = Number(a), j = Number(b)
  return typeof x === "string" ? Array.from(x).slice(i, j).join("") : x.slice(i, j)
}
function revlCharAt(s: string, i: bigint): string {
  const c = Array.from(s)[Number(i)]
  return c === undefined ? "" : c
}
function revlCharCodeAt(s: string, i: bigint): bigint {
  const c = Array.from(s)[Number(i)]
  return BigInt(c === undefined ? NaN : (c.codePointAt(0) as number))
}
function revlIndexOf(x: string | unknown[], v: unknown): bigint {
  if (typeof x === "string") {
    const at = x.indexOf(v as string)
    return BigInt(at < 0 ? -1 : Array.from(x.slice(0, at)).length)
  }
  return BigInt(x.indexOf(v))
}"""

_STR_METHOD_NAMES = {"length", "slice", "charAt", "charCodeAt", "indexOf"}


def _uses_str_methods(node) -> bool:
    """Does this IR call one of the code-point string helpers — a `length`
    field-read (`len`) or a `length`/`slice`/`charAt`/`charCodeAt`/`indexOf`
    builtin? These share a receiver with List/Bytes, so the helper dispatches
    at runtime; it is emitted only where one of these forms appears."""
    if isinstance(node, dict):
        if node.get("kind") == "len":
            return True
        if node.get("kind") == "builtin" and node.get("method") in _STR_METHOD_NAMES:
            return True
        return any(_uses_str_methods(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_str_methods(v) for v in node)
    return False


def _uses_equality(node) -> bool:
    """Equality appears both in expressions and in `assert` steps; both are
    `bin` nodes, so one scan covers the helper's use in either position."""
    """Does anything in this IR compare with `==`/`!=`?

    Emitting the helper unconditionally would leave a dead function in every
    module, which `tsc` is entitled to complain about; scanning is cheap and
    keeps the output honest about what it needs.
    """
    if isinstance(node, dict):
        if node.get("kind") == "bin" and node.get("op") in _TS_EQUALITY_OPS:
            return True
        return any(_uses_equality(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_equality(v) for v in node)
    return False


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
    ctx = _Ctx(types, functions, externs)
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
    ctx = _Ctx(types, functions, externs)
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


def _uses_spawn(ir: dict) -> bool:
    """True iff any component body contains a `spawn` acquisition node, so the
    module must import the `spawn` runtime helper. Non-spawning documents skip
    the import entirely and stay byte-identical to the pre-feature output."""
    found = False

    def walk(node) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            if node.get("kind") == "spawn":
                found = True
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(ir.get("components"))
    return found


def _runtime_imports(ir: dict, runtime_import: str) -> str:
    """The `import { ... } from '<runtime>'` line. `spawn` is added only when a
    spawn node is present (docs/design-v2-instances.md phase 2)."""
    names = ["host"]
    if _uses_spawn(ir):
        names.append("spawn")
    return f"import {{ {', '.join(names)} }} from '{runtime_import}'"


def _emit_v1(ir: dict, *, runtime_import: str) -> str:
    """Emit a v1/v2 component module (docs/backend-ir.md)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be an object")
    if ir.get("ir_version") not in (1, 2):
        raise EmitError(f"unsupported ir_version: {ir.get('ir_version')!r}")

    services = ir.get("services") or {}
    components = ir.get("components") or []
    # One document-level context threaded into every component body, so a
    # component that spells a top-level call as a `fn` node resolves it, and a
    # 2.0 expression mixed into the body renders against the same type context.
    doc_ctx = _Ctx(ir.get("types") or {}, ir.get("functions") or [],
                   ir.get("externs") or [])

    out: list[str] = [
        "// Generated by revl backends/typescript/emit.py — do not edit.",
        "// Target runtime: cordis v4 (https://github.com/cordiverse/cordis).",
        "import type { Context } from 'cordis'",
        _runtime_imports(ir, runtime_import),
        "",
    ]

    # A v1/v2 component body carries ordinary expressions too (`if (config.limit
    # < 1)`, Int arithmetic in a method body), so it needs the same helpers a
    # 2.0 document does — without this a v1 module referencing `revlI64` or
    # `revlEq` would emit a call to a function that is not there.
    out.extend(_revl_helpers(ir))

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
            # services 2.0 §5: an async operation returns a Promise on this tier.
            if method.get("async"):
                returns = f"Promise<{returns}>"
            if method.get("emission"):
                out.append("  /** emission — crosses the system boundary (DESIGN.md §3.5) */")
            if method.get("idempotent"):
                out.append("  /** idempotent — safe to re-deliver; the runtime may "
                           "auto-retry a transient failure (item 44) */")
            out.append(f"  {mname}({params}): {returns}")
        out.append("}")
        out.append("")

    out.extend(_context_augmentation(components))

    seen = set()
    for component in components:
        if component.get("name") in seen:
            raise EmitError(f"duplicate component name: {component.get('name')!r}")
        seen.add(component.get("name"))
        out.extend(_component(component, services, doc_ctx))
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
    # Document-level context for component bodies (see _emit_v1); pure fn/test
    # bodies build their own below, matching the pre-refactor per-pass split.
    doc_ctx = _Ctx(types, functions, externs)

    out: list[str] = [
        "// Generated by revl backends/typescript/emit.py — do not edit.",
        "// Target runtime: cordis v4 (https://github.com/cordiverse/cordis).",
        "import type { Context } from 'cordis'",
        _runtime_imports(ir, runtime_import),
    ]
    if tests:
        out.append("import { expect, it } from 'vitest'")
    out.append("")

    out.extend(_revl_helpers(ir))

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
            # services 2.0 §5: an async operation returns a Promise on this tier.
            if method.get("async"):
                returns = f"Promise<{returns}>"
            if method.get("emission"):
                out.append("  /** emission — crosses the system boundary (DESIGN.md §3.5) */")
            if method.get("idempotent"):
                out.append("  /** idempotent — safe to re-deliver; the runtime may "
                           "auto-retry a transient failure (item 44) */")
            out.append(f"  {mname}({params}): {returns}")
        out.append("}")
        out.append("")

    out.extend(_context_augmentation(components))

    seen = set()
    for component in components:
        if component.get("name") in seen:
            raise EmitError(f"duplicate component name: {component.get('name')!r}")
        seen.add(component.get("name"))
        out.extend(_component(component, services, doc_ctx))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------ typed holes

def _refuse_holes(ir: dict) -> None:
    """A typed hole is an unmet obligation, not code (docs/holes.md).

    Emitting one would put a placeholder into TypeScript and make tsc the
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
        f"refusing to emit TypeScript: this document still has {len(found)} typed "
        f"hole(s) — {where}. A hole type-checks so the surrounding draft can "
        f"be checked, but it has no implementation and there is nothing to "
        f"lower. Fill every hole, then emit (docs/holes.md)."
    )

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
                f"lifecycle test {test.get('name')!r} is not lowerable on the {'cordis (TS)'} tier: "
                "it drives a live composition (load/call/unload) and asserts R4 "
                "residue-freedom through the host runtime's introspection, which only the "
                "reference tier implements — run it with `revl test --backend py` "
                "(docs/syntax-2.0.md §7.1)"
            )


def emit(ir: dict, *, runtime_import: str = "../runtime.ts") -> str:
    """Emit one TypeScript module for an IR document (docs/backend-ir.md)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be an object")
    _refuse_holes(ir)

    _refuse_fault_tests(ir)

    _refuse_lifecycle_tests(ir.get("tests") or [])
    version = ir.get("ir_version")
    # The unified `_Ctx` carrying the document type context is built inside
    # each _emit_* pass and threaded into every body (component and 2.0),
    # replacing the former module-global context stack.
    if version in (1, 2):
        return _emit_v1(ir, runtime_import=runtime_import)
    if version == 3:
        return _emit_v3(ir, runtime_import=runtime_import)
    raise EmitError(f"unsupported ir_version: {version!r}")


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

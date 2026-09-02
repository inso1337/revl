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
- `let-effect` / `effect` steps -> plain evaluation + `yield $revl_frame.bracket(...)`.
  A witnessed acquisition (item 243) instead registers through
  `$revl_frame.transactional(...)`, Ok-conditional. Both, plus a compensation
  (`emit ... compensate ...` -> `$revl_frame.compensation(...)`, item 247),
  join the SAME LIFO stack via one `Frame` per activation (item 243 Slice 2b,
  docs/design/teardown-contract.md) — see runtime.ts's Frame section for the
  two-phase abort mechanism (the `begin`/`drain` sentinel yields).
- `provide` steps -> `yield ctx.provide(name, impl)`.  The withdrawal inverse
  is the runtime's own (R5); yielding the wrapper reparents it into the body
  effect at the correct LIFO position.
- `emit` steps -> plain calls; a `compensate` clause additionally registers a
  compensation entry (see above).
- `req` expressions -> `ctx.<name>` (the fiber's committed view; stays
  readable during teardown).
- `effect` steps inside provide-method bodies -> `ctx.effect(() => ...)`,
  which joins the component fiber's accumulator (coeffect operations are
  effects). NOT routed through `Frame` (item 243's witnessed/transactional
  entry kind is activation-body-only, matching the frontend's own refusal of
  a witnessed call outside effect position; ordinary method-body
  brackets/compensations are unchanged by this slice).
- `format` expressions -> template literals.

CLI: `python3 emit.py <ir.json> [> out.ts]`.
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
from typing import Any, Optional

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
            "Bool": "boolean", "Float": "number", "Bytes": "Uint8Array"}

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


def _is_async_fn_type(type_name: object) -> bool:
    """True for a function type whose declared return is `Async[…]` — the
    item-92 spelling that colors a first-class callback parameter."""
    if not isinstance(type_name, str):
        return False
    fn = _split_fn_type(type_name.strip())
    if fn is None:
        return False
    _, returns = fn
    return returns.strip().startswith("Async[")


def _ts_type(name: object, known_types: "frozenset[str]" = frozenset()) -> str:
    """Surface type -> TS type (IR v1/A6).

    `known_types` is the set of type names this document actually *declares*
    (and therefore emits an `interface`/`type` alias for — see `_emit_ts_types`).
    A bare name is rendered by that declared name only when it is in the set;
    any other unrecognised name is opaque and maps to `unknown`.

    This distinction is the whole point of the pass (harness findings #19 vs the
    item-89 regression). A record-typed service parameter in an IR v3 document —
    where the record IS declared — must render by name (`List[Msg]` -> `Msg[]`)
    so the emitted service interface lines up with the emitted `interface Msg`.
    But an IR v1/v2 document has no `type` declarations at all: a name like
    `examples/user_cache.rvl`'s `Row` (`List[Row]`) resolves to nothing, so it
    is opaque and erases to `unknown` — exactly as the java tier erases the same
    `Row` to `Object` and rust to `Value`. Rendering it by name emitted a
    dangling `Row[]` that `tsc` rejects with `Cannot find name 'Row'`.
    """
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
        rendered = ", ".join(
            f"a{i}: {_ts_type(p, known_types)}" for i, p in enumerate(params))
        return f"(({rendered}) => {_ts_type(returns, known_types)})"
    generic = re.match(r"^(\w+)\[(.+)\]$", name)
    if generic:
        head, inner = generic.group(1), generic.group(2)
        if head == "List":
            return f"{_ts_type(inner, known_types)}[]"
        if head == "Opt":
            return f"{_ts_type(inner, known_types)} | undefined"
        if head == "Map":
            return (f"Map<{_ts_type(inner.split(',')[0].strip(), known_types)}, "
                    f"{_ts_type(inner.split(',')[1].strip(), known_types)}>")
        if head == "Result":
            return (f'{{ kind: "Ok"; value: {_ts_type(inner.split(",")[0].strip(), known_types)} }}'
                    f' | {{ kind: "Err"; value: {_ts_type(inner.split(",")[1].strip(), known_types)} }}')
    # A declared v3 record/ADT name (Msg, ToolReq, ...) is emitted as an
    # interface/type alias above; route it through the v3 renderer so a service
    # signature carrying that record renders it by name (harness finding #19).
    # An *undeclared* name has nothing to render against and stays opaque.
    if name in known_types:
        return _ts_v3_type(name)
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


def _mangle(name: str) -> str:
    """Rename a syntactically-valid identifier that collides with a *JS/TS*
    reserved word, so a valid revl identifier that happens to be a JS keyword
    (`class`, `function`, `new`, …) emits and RUNS instead of crashing at emit
    (roadmap item 165).

    The scheme is the A3 append-`_` rename `src/revl/lower.py::_safe_name` and
    `backends/java/emit.py::_fn_name` already use for revl-keyword bindings.
    It is a pure function of the name, so the declaration site and every use
    site (and the `_Scope.locals` membership checks, which store the mangled
    form) agree without threading a table around, and it must ALSO be
    INJECTIVE: two distinct revl identifiers may never land on one JS
    identifier.

    The naive "append `_` while the name is reserved" loop is a pure function
    but NOT injective: it maps `function` to `function_` and leaves the equally
    legal revl identifier `function_` alone, so both reach `function_` and the
    two bindings collide. Here that is a LOUD break (`node --check` reports
    "Identifier 'function_' has already been declared"), but on the python tier
    the same shape silently CAPTURES, so the rule is fixed identically on every
    tier rather than left to a downstream compiler that CI does not run.

    The injective rule: escape a name iff the name OR any name reachable from
    it by dropping trailing `_` is reserved, and escape it by exactly ONE `_`.
    Names whose underscore-stripped root is reserved shift up one rung of the
    `kw`/`kw_`/`kw__` ladder (`function` -> `function_`, `function_` ->
    `function__`), which is injective; every other name is returned unchanged
    and can never equal a shifted name, because a shifted name's root is
    reserved and an unchanged name's root is not. The output is never itself
    reserved: no member of `JS_RESERVED` ends in `_`.

    Only a name whose root is reserved can change, so no existing program that
    does not name a JS keyword changes its emitted output. This is TARGET
    keywords only; the host roots stay routed through `host.<name>` in
    `_v3_var` and the emitter scaffolding stays rejected below."""
    root = name
    while root:
        if root in JS_RESERVED:
            return name + "_"
        if not root.endswith("_"):
            break
        root = root[:-1]
    return name


def _ident(name: object, role: str) -> str:
    if not isinstance(name, str) or not IDENT_RE.match(name):
        raise EmitError(f"invalid {role} identifier: {name!r}")
    if name in EMITTER_RESERVED:
        raise EmitError(
            f"{role} identifier collides with emitter scaffolding: {name!r}"
        )
    return _mangle(name)


def _string(value: str) -> str:
    # json.dumps produces a valid TS double-quoted string literal.
    return json.dumps(value)


def _prop_key(name: object, role: str) -> str:
    """A record PROPERTY KEY as it appears in an object literal or interface.

    A JS reserved word is emitted as its RAW quoted key (`"function":`), NOT
    the `_mangle`d bare identifier (`function_:`), so the key a revl-declared
    record carries at runtime is the SAME string a dynamic JSON value carries
    (item 279). `_mangle` is right for bindings — where revl controls both the
    declaration and every use — but a record field also has to match runtime
    data that a `json_parse` produced with the unrenamed key, so the two
    representations must agree on the raw word. A non-reserved, valid-identifier
    name stays a bare key, so no existing program changes its output.

    A property key is therefore NEVER `_mangle`d — not even the injective
    ladder shift (`function_` -> `function__`). The key has to be the raw revl
    field name whatever that name is, because that is the string the runtime
    value is keyed by; a shifted key would read a field that does not exist.
    Keeping the key raw is also what keeps the field space injective here: raw
    is the identity, and distinct revl fields stay distinct keys."""
    if isinstance(name, str) and name in JS_RESERVED:
        return _string(name)
    return _raw_field(name, role)


def _raw_field(name: object, role: str) -> str:
    """Validate a record FIELD name and return it verbatim.

    Same validation as `_ident` (shape, emitter scaffolding) minus the
    `_mangle` rename, which a field name must not get: the emitted key has to
    stay the raw revl field name so it matches the runtime key (item 279)."""
    if not isinstance(name, str) or not IDENT_RE.match(name):
        raise EmitError(f"invalid {role} identifier: {name!r}")
    if name in EMITTER_RESERVED:
        raise EmitError(
            f"{role} identifier collides with emitter scaffolding: {name!r}"
        )
    return name


def _member(target: str, name: object, role: str, *, optional: bool = False) -> str:
    """Read property `name` off `target`.

    A JS reserved word is reached through a bracket access with the raw key
    (`obj["function"]`), the counterpart to `_prop_key`: it targets the same
    unrenamed key, so it reaches both a revl-declared record and a dynamic
    JSON value whose runtime key is the raw word (item 279). Bare dot access
    is kept for every ordinary field, so no existing program changes.

    The dot path goes through `_raw_field`, not `_ident`: a field READ must
    name the same raw key `_prop_key` wrote, so it must not pick up `_mangle`'s
    injective ladder shift either."""
    if isinstance(name, str) and name in JS_RESERVED:
        bracket = f"?.[{_string(name)}]" if optional else f"[{_string(name)}]"
        return f"{target}{bracket}"
    dot = "?." if optional else "."
    return f"{target}{dot}{_raw_field(name, role)}"


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
        # item 167: routed requires (item 162's `routes` IR) — read through a
        # per-key router proxy the apply() builds, not a single-realm committed
        # view (see the `req` branch of `_expr`).
        self.routes: dict = component.get("routes") or {}

    def child(self) -> "_Scope":
        child = _Scope.__new__(_Scope)
        child.component = self.component
        child.requires = self.requires
        child.config_fields = self.config_fields
        child.locals = set(self.locals)
        child.routes = self.routes
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
    call = f"{name}({args})"
    # async extern call (roadmap item 80, docs/design/async-extern.md §5):
    # await it, parenthesized so it stays atomic in a larger expression. The
    # frontend admits such a call only inside an async provide method, so
    # `in_async` must hold — otherwise the checker is wrong and this crashes
    # honestly rather than emitting an un-awaited Promise into a sync function.
    if name in ctx.async_names:
        if not ctx.in_async:
            raise EmitError(
                f"async extern `{name}` called outside an async context — the "
                f"frontend async-coloring check should have refused this (A1)"
            )
        return f"(await {call})"
    return call


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


# item 397: a compare-and-set host verb (`insert_if_absent`) whose site-spelled
# `undo` must be RESULT-GUARDED, registered only when the CAS actually
# inserted. A `false` CAS (key already present) inserted nothing, so its inverse
# is the identity; replaying `remove(k)` at teardown would delete the WINNING
# claimant's entry, the exact corruption single-use exists to prevent. Mirrors
# backends/python/emit.py and backends/go/emit.py, which guard the same way.
_MAP_CAS_VERBS = frozenset({"insert_if_absent"})


def _is_map_cas(acquire: object) -> bool:
    """Whether an acquisition node is a result-guarded map CAS (item 397)."""
    return (isinstance(acquire, dict) and acquire.get("kind") == "call"
            and acquire.get("method") in _MAP_CAS_VERBS)


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
            f"{_prop_key(k, 'record field')}: {_expr(v, ctx)}"
            for k, v in node.get("fields") or []
        )
        return "{" + fields + "}"

    if kind == "record_update":
        # functional record update (docs/records.md §2): spread the base,
        # then let the updated fields override it — a fresh object either way
        base = _expr(node.get("base"), ctx)
        overrides = ", ".join(
            f"{_prop_key(k, 'record field')}: {_expr(v, ctx)}"
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
            rendered = f"{target_ts}.{method}({args})"
            # item 141 await-seed: an emission of an async service op — through a
            # req key (`emit agent.run_in(...)`) — returns a Promise on this
            # tier. Await it wherever it lands in an async body: not only a
            # statement/return, but a NESTED expression position such as a
            # ternary arm, so no Promise leaks un-awaited (e.g. `${reply}` in a
            # template). `in_async` distinguishes the async provide method (and
            # async arrow, which renders `async (…) => …` and so may await) from
            # a sync body, whose in_async is False and where an async op cannot
            # appear (the frontend colours it).
            scope = ctx.component_scope
            if ctx.in_async and not ctx.in_arrow and isinstance(target, dict) \
                    and target.get("kind") == "req" and scope is not None \
                    and (scope.requires.get(target.get("name")), method) in ctx.async_ops:
                return f"(await {rendered})"
            return rendered
        callee_node = node.get("callee")
        callee = _expr(callee_node, ctx)
        if not (isinstance(callee_node, dict) and callee_node.get("kind") in _V3_ATOMIC_KINDS):
            callee = f"({callee})"
        args = ", ".join(_expr(arg, ctx) for arg in node.get("args") or [])
        call = f"{callee}({args})"
        # async extern call (roadmap item 80, docs/design/async-extern.md §5):
        # await it, parenthesized so it stays atomic inside a larger expression
        # (`f(x) + 1` -> `(await f(x)) + 1`). The frontend admits such a call
        # only inside an async provide method, so `in_async` must hold — if it
        # does not the checker is wrong, and this crashes honestly rather than
        # emitting an un-awaited Promise into a sync function.
        if isinstance(callee_node, dict) and callee_node.get("kind") == "var" \
                and (callee_node.get("name") in ctx.async_names
                     or callee_node.get("name") in ctx.async_locals):
            if not ctx.in_async:
                raise EmitError(
                    f"async callable `{callee_node.get('name')}` called outside an "
                    f"async context — the frontend async-coloring check should "
                    f"have refused this (A1)"
                )
            return f"(await {call})"
        return call

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
            # item 167: a routed require reads through its router proxy, which
            # re-resolves a live per-realm worker on every call (failover). The
            # proxy is a local the apply() built; a provide-method's
            # `<key>.<op>(…)` closes over it.
            if name in scope.routes:
                return f"_revl_route_{_ident(name, 'requirement')}"
            # Committed-view access: resolved through the fiber's snapshot, so
            # it stays readable during this component's own teardown (R3).
            return f"ctx.{_ident(name, 'requirement')}"
        if kind == "host":
            fn = node.get("fn")
            if not isinstance(fn, str) or not all(IDENT_RE.match(p) for p in fn.split(".")):
                raise EmitError(f"invalid host builtin: {fn!r}")
            _refuse_missing_host_root(fn)
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
        if node.get("op") == "~":
            # Int32 bitwise complement (item 366): JS `~` returns a signed i32
            # `number`, so it needs no re-wrap and never traps.
            return f"(~{operand})"
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
        if node.get("sized_length"):
            # item 104 (cross-tier): property-form `.length` on a sized value in
            # a component position (the frontend keeps it a `field` node here and
            # marks it, since this emitter has no static type at the field site).
            # It is the code-point/element count, not a record member read — the
            # same `revlLen` the `len` node routes through (code points for a
            # `Str`, not UTF-16 units).
            return f"revlLen({target})"
        return _member(target, node.get("name"), "field")

    if kind == "index":
        target_node = node.get("target")
        target = _expr(target_node, ctx)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        # A STRING-literal index is a property read on a dynamic/host value
        # (`tc["function"]`), not a List index — the checker only admits it on
        # an `Any`/host receiver, never on a `List`. Coercing it through
        # `Number(...)` (the List path) produced `tc[Number("function")]`
        # (`tc[NaN]`, always undefined). Emit the raw key straight (item 279).
        index_node = node["index"]
        if isinstance(index_node, dict) and index_node.get("kind") == "lit" \
                and isinstance(index_node.get("value"), str):
            return f"{target}[{_string(index_node['value'])}]"
        # The index is an `Int` (bigint) and JS indexes with a `number`; TS
        # refuses a bigint index outright ("cannot be used as an index type").
        return f"{target}[{_int_as_number(index_node, ctx)}]"

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
        return _ts_builtin(node.get("method"), target, args, arg_nodes, ctx,
                           node.get("recv"))

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
        # FR-1 (roadmap 77a): an arrow literal in a provide-method body binds
        # its parameters in the arrow's body scope. The emitted `((msgs2) =>
        # ...)` already binds the names, but the body renders against the
        # enclosing component scope, where the fallback renderer checks every
        # `name` against `scope.locals` and refused the parameter as unbound
        # (`msgs2` — the name the frontend fix (1debdf2) now binds). Mirror
        # `_v3_arm_body`: a child scope adds the params (`.add`, not `.bind` —
        # the lambda's parameters shadow in TS exactly as they shadow in revl,
        # so this is not the single-assignment rebinding). A pure 2.0 fn/test
        # body has no component scope and resolves names verbatim, so nothing
        # is bound there.
        # item 92: an arrow the checker typed against `(…) -> Async[T]` carries
        # `"async": true`. It renders `async (…) => …` and its body renders in
        # an async context (so an internal async-callable call is awaited).
        # Equally load-bearing: a *sync* arrow renders its body with
        # `in_async=False` — inheriting the enclosing `in_async` would let an
        # `await` land inside a non-async arrow, a tsc error.
        is_async = bool(node.get("async"))
        scope = ctx.component_scope
        if names and scope is not None:
            arrow_scope = scope.child()
            for p in names:
                arrow_scope.locals.add(_ident(p, "arrow parameter"))
            body_ctx = ctx.with_scope(arrow_scope, in_async=is_async, in_arrow=True)
        else:
            body_ctx = ctx.with_scope(ctx.component_scope, in_async=is_async,
                                      in_arrow=True)
        body = _expr(node["body"], body_ctx)
        # Mutable `var` captures are snapshotted by value at arrow-creation
        # time (docs/expressible-iteration.md Semantics), the py tier's
        # `lambda x, n=n: ...` (backends/python/emit.py). JS default
        # parameters cannot spell that — a parameter initializer's right-hand
        # side resolves to the parameter itself and hits the TDZ — so the
        # snapshot is an IIFE AROUND THE ARROW that shadows each capture with
        # its current value: `((n) => ((x) => (x + n)))(n)`, evaluated when
        # the arrow literal is created, exactly like a python default arg.
        # (Wrapping the arrow *body* instead would re-snapshot on every call
        # and observe the rebound `var` — a silent wrong answer.) An arrow
        # with no captures is emitted exactly as before.
        # item 435(b): the `async` here follows the declared type, not the
        # rendered body, so `(msgs) => model.complete(msgs)` emitted `async
        # (msgs: any) => (ctx.model.complete(msgs))`, a resolution hop over a
        # Promise the body already returns, measured at 2 excess microtask
        # turns and 2 excess Promise allocations per operation call
        # (`bench/codegen/typescript/cases/async_arrow.ts`). Drop the keyword
        # when the body renders no `await` AND the body IS that un-awaited
        # emission Promise: `async (p) => e` and `(p) => e` then have the same
        # TS type, `(p) => Promise<T>`, so the arrow stays assignable wherever
        # it was before. Any other body keeps `async`, because an arrow over a
        # plain value would otherwise return `T` where the callee's parameter
        # type says `Promise<T>`.
        if is_async and not _renders_await(body) \
                and _v3_emission_call_node(node.get("body"), body_ctx):
            is_async = False
        prefix = "async " if is_async else ""
        captures = node.get("captures") or []
        if captures:
            bound = [f"{_ident(c, 'capture')}: any" for c in captures]
            args = [_ident(c, "capture") for c in captures]
            return (f"(({', '.join(bound)}) => ({prefix}({params}) => ({body})))"
                    f"({', '.join(args)})")
        return f"({prefix}({params}) => ({body}))"

    if kind == "match":
        return _v3_match_expr(node, ctx)

    if kind == "do":
        return _v3_do_expr(node, ctx)

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
        return _member(target, node.get("name"), "optional field", optional=True)

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

    if kind in ("subscribe", "stream-merge"):
        # item 130: a stream subscription suspends a fiber. Slice 1 shipped the
        # py reference and Slice 3 the go/rust blocking erasure; the ts tier
        # takes the same `async function*` shape as py (design §4.6) but has not
        # been written or run, so refuse honestly rather than emit a
        # subscription whose queue-vs-cancel race has never been exercised.
        raise EmitError(
            "a stream subscription suspends a fiber; the ts lowering (the same "
            "queue-vs-cancel race the py reference runs) is not implemented — "
            "streams run on py, go and rust (item 130 §4.6); try `--backend py`"
        )

    raise EmitError(f"unsupported expression kind {kind!r}")


def _method_body(steps: list, ctx: "_Ctx", indent: str,
                 method_is_async: bool = False, frame_var: Optional[str] = None,
                 provide_name: Optional[str] = None,
                 method_name: Optional[str] = None) -> list[str]:
    """Steps inside a provide-method body.

    These run while the component is ACTIVE; `effect` steps go through
    `ctx.effect` so their undos join the component fiber's accumulator. An
    `async` service operation (services 2.0 §5) lowers to an `async` method,
    whose body may `await` a host async value or an instance disposal (A1);
    a `sync` method rejects `await`, matching the reference tier.

    item 318: a WITNESSED effect in a method body is the per-tool-call H1 seam —
    it does NOT go through `ctx.effect` (unsound: disposed before the body's
    `drain`), but registers into the component's activation `Frame` directly via
    `_method_witnessed_step`. `frame_var` is that frame (always present when a
    method body has a witnessed effect, because `_needs_frame` forces it).
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
        elif kind in ("effect", "let-effect") \
                and _witnessed_extern(step.get("acquire"), ctx) is not None:
            # item 318: a per-tool-call witnessed fs mutation. Registered into
            # the component activation frame (parked for `drain`), NOT adopted as
            # a sibling `ctx.effect` — see `_method_witnessed_step` /
            # `Frame.transactionalMethod`.
            wit = _witnessed_extern(step.get("acquire"), ctx)
            ctx._counter[0] += 1
            site = f"{provide_name or 'provide'}.{method_name or 'method'}#{ctx._counter[0]}"
            bind = None
            if kind == "let-effect":
                bind = scope.bind(step["bind"])
            _method_witnessed_step(step, wit, ctx, indent, lines, frame_var, bind, site)
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
            if bind is not None and _is_map_cas(step.get("acquire")):
                # item 397: result-guarded undo. A `false` CAS registers the
                # identity inverse (a no-op disposer), so teardown never removes
                # the winning claimant's entry.
                lines.append(f"{indent}  return {bind} ? () => {undo} : () => {{}}")
            else:
                lines.append(f"{indent}  return () => {undo}")
            lines.append(f"{indent}}})")
        elif kind == "emit":
            if step.get("compensate") is not None:
                # item 247 (method-body compensate remainder): a method-body `emit ... compensate ...` is a first-
                # class COMPENSATION on the component's activation frame (the
                # method-body analog of item 247's activation-body site), NOT a
                # bare `ctx.effect(() => { ...; return () => <offset> })` bracket.
                # A bare bracket is disposed by cordis BEFORE the body `drain`, so
                # it fires the offset on a CLEAN unload (destroying the
                # deliverable), interleaves with proof inverses, and is unguarded.
                # `frame.compensationMethod` makes it abort-only: discharged on a
                # commit, drained in Phase 2 after every proof inverse, guarded
                # and residue-collected. Fire the emission first, then register —
                # mirrors the activation-body site and py's `_method_step`. Args
                # are bound to temps HERE, at registration (the "no data hazard"
                # reason for the phase split).
                lines.append(f"{indent}{_expr(step['expr'], ctx)}")
                ctx._counter[0] += 1
                n = ctx._counter[0]
                site = f"{provide_name or 'provide'}.{method_name or 'method'}#{n}"
                comp_node = step["compensate"]
                bound_call = _bind_call_temps(comp_node, ctx, lines, indent, f"$revl_comp{n}")
                if bound_call is not None:
                    target_ts, key_str, method, temps = bound_call
                    run_ts = _replay_call(comp_node, target_ts, method, temps)
                    crossing = _crossing_literal(key_str, method, temps, site)
                    args_list = f"[{', '.join(temps)}]"
                else:
                    snap = f"$revl_comp{n}"
                    lines.append(f"{indent}const {snap} = {_expr(comp_node, ctx)}")
                    run_ts = snap
                    crossing = _crossing_literal(provide_name or "provide", "compensate", [], site)
                    args_list = "[]"
                lines.append(
                    f"{indent}{frame_var}.compensationMethod({crossing}, "
                    f"{_string(method if bound_call is not None else 'compensate')}, "
                    f"{args_list}, () => {run_ts})"
                )
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


def _provide_impl(step: dict, ctx: "_Ctx", services: dict, indent: str,
                  frame_var: Optional[str] = None) -> list[str]:
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
        # The implementation's param types must line up with the service
        # interface's (the object is `satisfies <Service>`), so both resolve
        # names against the same declared-type set — the document's `types`,
        # carried on the rendering context. In a v1/v2 document that set is
        # empty, so an undeclared name (`Row`) erases to `unknown` on both sides.
        known_types = frozenset(ctx.types)
        sig = ", ".join(
            f"{p}: {_ts_type(spec.get('type'), known_types)}"
            for p, spec in zip(params, spec_params)
        )
        # services 2.0 §5: an async operation lowers to an async method, whose
        # body may await (A1). The flag lives on the service declaration, the
        # single source of truth for the operation's shape.
        method_is_async = bool(declared[name].get("async"))
        prefix = "async " if method_is_async else ""
        lines.append(f"{indent}{prefix}{name}({sig}) {{")
        lines.extend(_method_body(method.get("body") or [],
                                  ctx.with_scope(body_scope, in_async=method_is_async),
                                  indent + "  ", method_is_async,
                                  frame_var, provide_name=service_name,
                                  method_name=name))
        lines.append(f"{indent}}},")
    return lines


# ---------------------------------------------------------------------------
# item 243 Slice 2b: the witnessed/compensation teardown loop
# (docs/design/teardown-contract.md, docs/design/243-witnessed-externs.md).
#
# Every activation-body step whose disposer joins the Frame's LIFO stack
# needs a `Crossing` (key/method/args/site) for its residue records. The
# crossing's `args` must be captured ONCE, at registration — never re-read at
# teardown (the contract's "no data hazard" reason for the phase split) — so
# a recognised call shape (component `target.method(args)`, a plain `fn`
# call, or a `host` builtin call) has each arg bound to a temp BEFORE the
# real call runs, and the real call is rebuilt from those same temps: no
# double evaluation, and the temps are `const`, so a later `assign` step
# elsewhere in the body cannot retroactively change what a deferred
# compensation closure sees.


def _crossing_key_name(target: Any) -> str:
    """A plain, compile-time STRING name for a call target — `Crossing.key`
    is typed `string` (a capability/service key an operator reads), never the
    runtime value itself. Covers every target shape `_expr` renders a local
    /requirement reference from; falls back to `'?'` for anything else rather
    than raising (a crossing's key is diagnostic, not load-bearing)."""
    if isinstance(target, dict):
        if target.get("kind") == "req":
            return target.get("name") or "?"
        if target.get("kind") == "name":
            return target.get("id") or "?"
        if target.get("kind") == "var":
            return target.get("name") or "?"
    return "?"


def _bind_call_temps(node: dict, ctx: "_Ctx", out: list[str], indent: str,
                     tmp_prefix: str) -> tuple[Optional[str], str, str, list[str]] | None:
    """Bind a recognised call's target + args to temps (registration-time
    capture) and return `(target_ts, key_str, method, temp_names)`, or `None`
    for an unrecognised shape. `target_ts` is the renderable TS expression the
    replayed call is invoked on (`None` for a bare `fn`/`host` call, which has
    no separate target); `key_str` is always a plain string, for
    `Crossing.key`. `out`/`indent` receive the `const` binding lines."""
    if not isinstance(node, dict):
        return None
    if "target" in node and "method" in node:
        target = node.get("target")
        target_tmp = f"{tmp_prefix}_key"
        out.append(f"{indent}const {target_tmp} = {_expr(target, ctx)}")
        temps = []
        for i, arg in enumerate(node.get("args") or []):
            tmp = f"{tmp_prefix}_arg{i}"
            out.append(f"{indent}const {tmp} = {_expr(arg, ctx)}")
            temps.append(tmp)
        return (target_tmp, _crossing_key_name(target), node.get("method"), temps)
    if node.get("kind") in ("fn", "host"):
        name = node.get("name") if node.get("kind") == "fn" else (node.get("fn") or "")
        temps = []
        for i, arg in enumerate(node.get("args") or []):
            tmp = f"{tmp_prefix}_arg{i}"
            out.append(f"{indent}const {tmp} = {_expr(arg, ctx)}")
            temps.append(tmp)
        return (None, name, name, temps)
    return None


def _call_method_name(node: Any) -> str:
    """Best-effort callee name for a residue record's `attempted.call` /
    `Frame.bracket`'s `undoMethod`, from any of the call shapes this document
    uses. Never raises — falls back to a generic label for anything else.

    An extern's declared `undo`/`compensate` slot is lowered by
    `src/revl/lower.py`'s `_lower_extern_expr` (-> `_lower_pure_expr`), which
    always produces the v3-dialect `{"kind": "call", "callee": {"kind":
    "var", "name": ...}, "args": [...]}` shape — even inside a v1-`ir_version`
    document, since an extern's own body is lowered independently of the
    document dialect its callers use. A component-site `undo`/`compensate`
    (a plain body step) instead uses whichever call shape that document's own
    steps use (`target`/`method`, `fn`, or `host`)."""
    if not isinstance(node, dict):
        return "undo"
    if "method" in node:
        return node.get("method") or "undo"
    if node.get("kind") == "fn":
        return node.get("name") or "undo"
    if node.get("kind") == "host":
        return node.get("fn") or "undo"
    if node.get("kind") == "call":
        callee = node.get("callee")
        if isinstance(callee, dict) and callee.get("kind") == "var":
            return callee.get("name") or "undo"
    return "undo"


def _acquire_method_name(node: Any) -> str:
    """The name of the ACQUISITION half of a bracket crossing.

    Separate from `_call_method_name` because that function also serves the
    `undo` slot, where `"undo"` is the right fallback. Here it is not: a
    residue record exists to name the crossing an operator must check by hand,
    and `Frame.referentOf` renders `method` as `<key>.<method>()`, so a
    fabricated name sends them looking for a method that does not exist.

    A `spawn` acquisition carries no `method`/`name`/`fn`, so it reached the
    generic fallback and a spawn crossing rendered as `w1.undo()`. It is named
    for the `spawn(ctx, Worker, ...)` call that actually performs it. Every
    other acquisition shape in the corpus (`fn`, `host`, `call`) resolves in
    `_call_method_name` already."""
    if isinstance(node, dict) and node.get("kind") == "spawn":
        return "spawn"
    return _call_method_name(node)


def _replay_call(node: dict, target_ts: Optional[str], method: str, temps: list[str]) -> str:
    """Rebuild the call expression from the temps `_bind_call_temps` bound,
    so the deferred (Phase-2 / undo) invocation never re-reads the original
    argument expressions."""
    args = ", ".join(temps)
    if "target" in node and "method" in node:
        return f"{target_ts}.{_ident(method, 'method')}({args})"
    if node.get("kind") == "fn":
        return f"{_ident(method, 'name')}({args})"
    if node.get("kind") == "host":
        return f"host.{method}({args})"
    raise EmitError(f"unreachable: _replay_call on unrecognised node {node!r}")


def _crossing_literal(key_str: str, method: str, arg_temps: list[str], site: str) -> str:
    args = ", ".join(arg_temps)
    return (f"{{ key: {_string(key_str)}, method: {_string(method or '?')}, "
           f"args: [{args}], site: {_string(site)} }}")


def _bracket_yield(frame_var: Optional[str], key: str, method: str, site: str,
                   undo_method: str, arrow: str) -> str:
    """One bracket disposer, routed through `Frame.bracket` so a Phase-1 raise
    is CAUGHT and RECORDED (`bracket-fault`) instead of breaking cordis'
    sequential disposal chain and starving every earlier-registered
    (later-disposed) inverse — docs/design/teardown-contract.md, "A failed
    inverse never skips the remaining Phase-1 inverses".

    runtime.ts's own Frame doc states the Phase-1 guarantee holds only
    "PROVIDED each disposer catches its own failure"; a bare `yield () =>
    <undo>` does not, so an EMITTED program had the hole even though the
    hand-built `Frame.bracket` API was already correct and unit-tested. The
    guard cannot sit in the runtime on this tier the way it does on py: the
    one value every disposer passes through also carries `ctx.provide`'s
    cordis effect object, whose identity the unwind branches on.

    The crossing's `args` stay empty on purpose — a bracket's acquisition
    arguments are not bound to temps (binding them would move every
    acquisition's emitted shape), and the record's job here is to NAME the
    inverse that faulted, which `key`/`method`/`undoMethod` already do.

    `frame_var` is `None` only where this component has no accumulator; the
    bare arrow is kept there, unchanged."""
    if frame_var is None:
        return f"yield {arrow}"
    crossing = _crossing_literal(key, method, [], site)
    return f"yield {frame_var}.bracket({crossing}, {_string(undo_method)}, {arrow})"


def _witnessed_extern(acquire: Any, ctx: "_Ctx") -> Optional[dict]:
    """The witnessed extern descriptor a step's acquisition calls, or `None`
    (mirrors backends/python/emit.py `_ComponentEmitter._witnessed_extern`).
    A witnessed effect is spelled as an effect-position call to a `witnessed`
    extern; lower.py emits that as a plain `fn`-kind acquisition node with no
    `undo` key (`_lower_effect_step`), so matching the callee name against the
    witnessed table is how a call site is told apart from an ordinary bracket."""
    if not ctx.witnessed or not isinstance(acquire, dict):
        return None
    if acquire.get("kind") != "fn":
        return None
    return ctx.witnessed.get(acquire.get("name"))


def _witnessed_step(step: dict, ext: dict, ctx: "_Ctx", indent: str,
                    lines: list[str], frame_var: str, bind: Optional[str],
                    site: str) -> None:
    """Emit a witnessed effect (item 243): run the mutation, and on `Ok`
    register the extern's DECLARED inverse into the Frame as a TRANSACTIONAL
    entry carrying the `Ok` witness. Mirrors
    backends/python/emit.py._witnessed_step's Ok-conditional registration;
    unlike a bracket (which always replays), this entry's disposer replays
    ONLY on abort and is discharged on a clean commit
    (`Frame.transactional`)."""
    ctx._counter[0] += 1
    n = ctx._counter[0]
    tmp = f"$revl_wit{n}"
    acquire = step["acquire"]
    bound = _bind_call_temps(acquire, ctx, lines, indent, f"{tmp}_acq")
    if bound is None:  # pragma: no cover — lower.py always emits a `fn` node here
        raise EmitError(f"witnessed acquisition has an unrecognised shape: {acquire!r}")
    target_ts, key_str, method, temps = bound
    call_ts = _replay_call(acquire, target_ts, method, temps)
    crossing = _crossing_literal(key_str, method, temps, site)
    lines.append(f"{indent}const {tmp} = {call_ts}")
    lines.append(f"{indent}if ({tmp}.kind === 'Ok') {{")
    undo_node = ext["undo"]
    # 243's Slice-1-as-implemented note 1: `undo` reuses the acquire slot and
    # binds `result` to the `Ok` payload. `result` is a synthetic arrow
    # parameter this codegen introduces, not an IR `let` binding, so it must
    # be declared on a CHILD scope (mirrors `_method_body`'s per-parameter
    # `scope.child()`) — rendering against the activation scope directly
    # would raise "reference to unbound name 'result'" (`_expr`'s `name`
    # branch checks `scope.locals`).
    undo_method = _call_method_name(undo_node)
    undo_scope = ctx.component_scope.child()
    undo_scope.locals.add("result")
    undo_ts = _expr(undo_node, ctx.with_scope(undo_scope))
    lines.append(
        f"{indent}  yield {frame_var}.transactional({crossing}, {_string(undo_method)}, "
        f"(result) => {undo_ts}, {tmp}.value)"
    )
    lines.append(f"{indent}}}")
    if bind is not None:
        lines.append(f"{indent}const {bind} = {tmp}")


def _method_witnessed_step(step: dict, ext: dict, ctx: "_Ctx", indent: str,
                           lines: list[str], frame_var: str, bind: Optional[str],
                           site: str) -> None:
    """Emit a witnessed effect inside a PROVIDE-METHOD body (item 318): the
    per-tool-call H1 seam. Run the mutation, and on `Ok` register the extern's
    DECLARED inverse into the ENCLOSING COMPONENT'S activation frame as a
    transactional entry carrying the `Ok` witness. Mirrors
    backends/python/emit.py._method_witnessed_step.

    The activation-body form (`_witnessed_step`) `yield`s the disposer into the
    body generator's own LIFO stack. A method body has no such generator, and
    adopting the entry as a sibling `ctx.effect` is unsound on this cordis-style
    tier (disposed BEFORE the body's `drain`, so a clean unload would observe
    `committed` still false and wrongly revert the deliverable — see
    `Frame.transactionalMethod`). So this calls the frame DIRECTLY:
    `frame.transactionalMethod(...)` parks the entry for `drain` to dispose once
    the commit-vs-abort bit is settled. On `Err` nothing is registered
    (Ok-conditional): a failed mutation touched nothing, so it schedules no
    rollback. `frame_var` is the component's activation `Frame`, in scope in
    every method body (the method closure captures the `apply`-local)."""
    ctx._counter[0] += 1
    n = ctx._counter[0]
    tmp = f"$revl_wit{n}"
    acquire = step["acquire"]
    bound = _bind_call_temps(acquire, ctx, lines, indent, f"{tmp}_acq")
    if bound is None:  # pragma: no cover — lower.py always emits a `fn` node here
        raise EmitError(f"witnessed acquisition has an unrecognised shape: {acquire!r}")
    target_ts, key_str, method, temps = bound
    call_ts = _replay_call(acquire, target_ts, method, temps)
    crossing = _crossing_literal(key_str, method, temps, site)
    lines.append(f"{indent}const {tmp} = {call_ts}")
    lines.append(f"{indent}if ({tmp}.kind === 'Ok') {{")
    undo_node = ext["undo"]
    # 243's Slice-1-as-implemented note 1: `undo` reuses the acquire slot and
    # binds `result` to the `Ok` payload — a synthetic arrow parameter this
    # codegen introduces, declared on a CHILD scope (mirrors `_witnessed_step`).
    undo_method = _call_method_name(undo_node)
    undo_scope = ctx.component_scope.child()
    undo_scope.locals.add("result")
    undo_ts = _expr(undo_node, ctx.with_scope(undo_scope))
    lines.append(
        f"{indent}  {frame_var}.transactionalMethod({crossing}, {_string(undo_method)}, "
        f"(result) => {undo_ts}, {tmp}.value)"
    )
    lines.append(f"{indent}}}")
    if bind is not None:
        lines.append(f"{indent}const {bind} = {tmp}")


def _method_body_needs_frame(steps: list, ctx: "_Ctx") -> bool:
    """True iff a provide-method body registers a witnessed (transactional)
    entry (item 318) OR a compensation (item 247 (method-body compensate remainder)) — the per-tool-call cases that
    need the component's activation `Frame`. A WITNESSED effect parks a
    transactional inverse (`transactionalMethod`); a method-body
    `emit ... compensate ...` parks a compensation (`compensationMethod`) — both
    must ride the frame's commit/abort discipline, NOT a bare `ctx.effect(...)`
    bracket (which cordis disposes before the body `drain`, firing the offset on
    a CLEAN unload — the item-247 soundness bug left on the method-body site).
    An ordinary method-body bracket still stays the pre-existing bare
    `ctx.effect(...)`, so the gate stays tight."""
    for step in steps or []:
        kind = step.get("step")
        if kind in ("let-effect", "effect") \
                and _witnessed_extern(step.get("acquire"), ctx) is not None:
            return True
        if kind == "emit" and step.get("compensate") is not None:
            return True
    return False


def _needs_frame(component: dict, ctx: "_Ctx") -> bool:
    """True iff this component's activation body registers at least one
    transactional (witnessed) or compensation entry — the two entry kinds
    that actually need the `Frame` apparatus (item 243 Slice 2b).

    A plain bracket never needs `Frame` (it stays the pre-existing bare
    `yield () => <undo>`, matching backends/python/emit.py byte-for-byte —
    see `_component_step`'s comment), so a component using ONLY brackets, and
    a document with no such component at all, must emit with NO `Frame`
    construction and no `begin`/`drain` sentinels — byte-identical to before
    this slice. Walks `if` branches (the only nesting an activation-body step
    reaches in this document's `body` list), but does NOT descend into a
    `timer` step's nested body: a timer body is emission-only (item 57 —
    `_component_step`'s own invariant check refuses anything else), so it can
    never carry a `compensate`, and a witnessed call is refused outside
    activation effect position, so a timer body never contributes either
    way."""
    def walk(steps: list) -> bool:
        for step in steps or []:
            kind = step.get("step")
            if kind in ("let-effect", "effect") and _witnessed_extern(step.get("acquire"), ctx) is not None:
                return True
            if kind == "emit" and step.get("compensate") is not None:
                return True
            if kind == "if":
                if walk(step.get("then") or []) or walk(step.get("else") or []):
                    return True
            # item 318: a provide-method body that does a WITNESSED effect
            # (per-tool-call H1) registers into this activation frame, so the
            # component needs `Frame` even when its activation body alone would
            # not. Only witnessed method effects count (see
            # `_method_body_needs_frame`) — an ordinary method-body bracket does
            # not, keeping the gate tight.
            if kind == "provide":
                for method in step.get("methods") or []:
                    if _method_body_needs_frame(method.get("body") or [], ctx):
                        return True
        return False

    return walk(component.get("body") or [])


def _has_bracket(component: dict) -> bool:
    """True iff this component's activation body yields at least one BRACKET
    disposer (an ordinary `let-effect`/`effect` acquisition, or a `timer`'s
    cancel inverse).

    Such a component needs a `Frame` too — not for the `begin`/`drain`
    sentinels (`_needs_frame` still governs those, so teardown SEMANTICS are
    unchanged), but purely as the accumulator `Frame.bracket` records a
    Phase-1 `bracket-fault` into. Without one a raising inverse breaks cordis'
    sequential disposal chain and silently starves every earlier-registered
    entry (docs/design/teardown-contract.md; see `_bracket_yield`).

    A witnessed acquisition is NOT a bracket — it registers through
    `Frame.transactional`, which `_needs_frame` already covers."""
    def walk(steps: list) -> bool:
        for step in steps or []:
            kind = step.get("step")
            if kind in ("let-effect", "effect"):
                return True
            if kind == "timer":
                return True
            if kind == "if":
                if walk(step.get("then") or []) or walk(step.get("else") or []):
                    return True
        return False

    return walk(component.get("body") or [])


def _component_body(component: dict, services: dict, indent: str, doc_ctx: "_Ctx",
                    frame_var: Optional[str]) -> list[str]:
    """The activation body, lowered into one ctx.effect generator."""
    ctx = doc_ctx.with_scope(_Scope(component))
    lines: list[str] = []
    for step in component.get("body") or []:
        _component_step(step, component, services, ctx, indent, lines, frame_var)
    return lines


def _ts_emission_is_async(expr: dict, ctx: "_Ctx") -> bool:
    """True if a timer-body emission returns a `Promise` on this tier (item 170).

    A timer body the frontend coloured `async` reaches an async op; each such
    emission spawns a tracked in-flight `Promise` rather than firing-and-
    forgetting an un-awaited one. This mirrors the await-seed condition in
    `_expr`'s component-`call` branch (a req-target async service op, or a call
    to an async extern / async-colored callable), and the py backend's
    `_py_reaches_coroutine` gate on `_timer`. A timer body is emissions-only
    (`emit …`), so the emission expression is a single call and this top-level
    check suffices."""
    if not isinstance(expr, dict) or expr.get("kind") != "call":
        return False
    if "target" in expr:
        target = expr.get("target")
        scope = ctx.component_scope
        if isinstance(target, dict) and target.get("kind") == "req" \
                and scope is not None:
            method = expr.get("method")
            return (scope.requires.get(target.get("name")), method) in ctx.async_ops
        return False
    callee = expr.get("callee")
    return isinstance(callee, dict) and callee.get("kind") == "var" \
        and (callee.get("name") in ctx.async_names
             or callee.get("name") in ctx.async_locals)


def _await_statement(expr: dict, ctx: "_Ctx") -> str:
    """Render `expr` as a KEYWORD-LED `await …` statement (item 131).

    The activation-body await positions that discard the awaited value — the
    `await` step, an `await emit`, and an unbound `effect await` — emit an
    expression statement. Rendered under an in_async view, the shared `_expr`
    seeds the `await` for whatever the position suspends on: a req-target async
    op (`ctx.w.heat()` -> `(await ctx.w.heat())`), an async extern, or an async-
    colored fn (the sources the await step widened to under item 131). The
    `await` is hoisted to the front so the statement never begins with `(`,
    which JS ASI would otherwise glue to a preceding `yield () => <undo>` arrow
    (a real merge — verified: it makes `await` land in a sync arrow, a syntax
    error). This is exactly the `await <call>` shape the plain await step has
    always emitted; a req-op await is therefore byte-identical to before."""
    actx = ctx.with_scope(ctx.component_scope, in_async=True)
    rendered = _expr(expr, actx)
    if rendered.startswith("(await ") and rendered.endswith(")"):
        return "await " + rendered[len("(await "):-1]
    return "await " + rendered


def _component_step(step: dict, component: dict, services: dict, ctx: "_Ctx",
                    indent: str, lines: list[str], frame_var: Optional[str]) -> None:
    """One step of the activation body, appended to `lines`.

    Recursive because `if` branches hold ordinary body steps. `frame_var` is
    the emitted `Frame` local this component's `apply` builds (item 243
    Slice 2b) — every disposer this step yields is registered through it, so
    the three entry kinds (bracket / transactional / compensation) share its
    one LIFO stack (docs/design/teardown-contract.md).
    """
    scope = ctx.component_scope
    provides = component.get("provides") or {}
    kind = step.get("step")
    if kind in ("let-effect", "effect"):
        bind = step.get("bind") if kind == "let-effect" else None
        wit = _witnessed_extern(step.get("acquire"), ctx)
        if wit is not None:
            ctx._counter[0] += 1
            site = f"{component['name']}.body#{ctx._counter[0]}"
            bound = bind
            if kind == "let-effect":
                # reserve the surface name now (single-assignment check),
                # even though `_witnessed_step` binds it from a temp below —
                # matches every other `let-effect` branch's ordering.
                bound = scope.bind(step["bind"])
            _witnessed_step(step, wit, ctx, indent, lines, frame_var, bound, site)
        else:
            # An ordinary bracket: UNCHANGED, byte-for-byte, from before this
            # slice — a bare `yield () => <undo>`, exactly mirroring
            # backends/python/emit.py's plain (non-witnessed) `let-effect`/
            # `effect` branch (`yield lambda: <undo>`, emit.py:934/944). It is
            # NOT routed through `Frame.bracket`: py's own reference keeps the
            # plain acquire as a bare disposer the Frame's accumulator never
            # sees (only a witnessed call registers through the Frame), so
            # matching that byte-for-byte is what keeps every non-witnessed,
            # non-compensating program's emission identical to before this
            # slice (`_needs_frame`, below, is what makes the whole `Frame`
            # apparatus itself conditional on the same basis).
            # item 131: an async-flagged acquisition awaits its landed result
            # (not the in-flight Promise), THEN registers the inverse — the
            # `yield () => undo` is the next action in the same generator step,
            # so registration is boundary-atomic with the acquisition (design §4
            # clause 1). The `undo` stays sync (rule 3 keeps teardown
            # suspension-free). A sync acquisition carries no flag and is
            # byte-identical to before.
            cas_bind: Optional[str] = None
            if step.get("async"):
                if kind == "let-effect":
                    bind_name = scope.bind(step["bind"])
                    cas_bind = bind_name
                    # `const c = await …` — a `const`-led statement is ASI-safe,
                    # so the awaited call keeps its `(await …)` shape here.
                    actx = ctx.with_scope(ctx.component_scope, in_async=True)
                    lines.append(f"{indent}const {bind_name} = {_expr(step['acquire'], actx)}")
                else:
                    lines.append(f"{indent}{_await_statement(step['acquire'], ctx)}")
            else:
                acquire = _expr(step["acquire"], ctx)
                if kind == "let-effect":
                    bind_name = scope.bind(step["bind"])
                    cas_bind = bind_name
                    lines.append(f"{indent}const {bind_name} = {acquire}")
                else:
                    lines.append(f"{indent}{acquire}")
            undo = _expr(step["undo"], ctx)
            acquire_node = step.get("acquire") or {}
            b_key = step.get("bind") or _acquire_method_name(acquire_node)
            b_method = _acquire_method_name(acquire_node)
            b_site = f"{component['name']}.body:{b_key}"
            b_undo = _call_method_name(step.get("undo"))
            if cas_bind is not None and _is_map_cas(step.get("acquire")):
                # item 397: result-guarded undo. A `false` CAS registers the
                # identity inverse (a no-op disposer), so teardown never removes
                # the winning claimant's entry. Mirrors py's `yield lambda:
                # (<undo> if <bind> else None)`.
                arrow = f"{cas_bind} ? () => {undo} : () => {{}}"
            else:
                arrow = f"() => {undo}"
            lines.append(indent + _bracket_yield(
                frame_var, b_key, b_method, b_site, b_undo, arrow))
    elif kind == "emit":
        # item 131: `await emit …` awaits the boundary crossing so the emission
        # actually fires — a bare async emit would leave a floating, unordered
        # Promise. Emitted keyword-led (`await …`) so it never begins with `(`;
        # the compensation registers after, as in the sync spelling. A sync emit
        # carries no flag and is byte-identical to before.
        if step.get("async"):
            lines.append(f"{indent}{_await_statement(step['expr'], ctx)}")
        else:
            lines.append(f"{indent}{_expr(step['expr'], ctx)}")
        if step.get("compensate") is not None:
            # item 247: a compensation entry — audit-facing, best-effort,
            # ABORT-ONLY, Phase 2 (never on a clean unload). Args are bound to
            # temps HERE, at registration, per the contract's "no data
            # hazard" — a deferred Phase-2 closure never re-reads a variable
            # that a later step in this same body might have reassigned.
            ctx._counter[0] += 1
            n = ctx._counter[0]
            site = f"{component['name']}.body#{n}"
            comp_node = step["compensate"]
            bound_call = _bind_call_temps(comp_node, ctx, lines, indent, f"$revl_comp{n}")
            if bound_call is not None:
                target_ts, key_str, method, temps = bound_call
                run_ts = _replay_call(comp_node, target_ts, method, temps)
                crossing = _crossing_literal(key_str, method, temps, site)
                args_list = f"[{', '.join(temps)}]"
            else:
                # unrecognised compensate shape: eagerly snapshot its value
                # now (registration time) rather than close over a live
                # binding — see this function's module doc on the fallback's
                # documented limitation for a non-call compensate expression.
                snap = f"$revl_comp{n}"
                lines.append(f"{indent}const {snap} = {_expr(comp_node, ctx)}")
                run_ts = snap
                crossing = _crossing_literal(component["name"], "compensate", [], site)
                args_list = "[]"
            lines.append(
                f"{indent}yield {frame_var}.compensation({crossing}, "
                f"{_string(method if bound_call is not None else 'compensate')}, "
                f"{args_list}, () => {run_ts})"
            )
    elif kind == "await":
        # v1/A1: the await lands (inertia), then the yield closes the
        # iteration so a divert during the await skips every later step.
        # item 131 widens the await step's suspension sources from req-ops /
        # `Job.run` to also include an async extern or an async-colored fn;
        # `_await_statement` renders under an in_async view so those await too.
        # A req-op await stays byte-identical (`await ctx.w.heat()`).
        lines.append(f"{indent}{_await_statement(step['expr'], ctx)}")
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
        lines.extend(_provide_impl(step, ctx, services, indent + "  ", frame_var))
        lines.append(f"{indent}}} satisfies {_ident(step['service'], 'service')})")
    elif kind == "if":
        # An activation guard (A8). Branches hold ordinary body steps, so a
        # `yield` inside one keeps its place in the accumulator's LIFO order —
        # being a generator body makes that work with no handling here.
        lines.append(f"{indent}if ({_expr(step['cond'], ctx)}) {{")
        for nested in step.get("then") or []:
            _component_step(nested, component, services, ctx, indent + "  ", lines, frame_var)
        if step.get("else"):
            lines.append(f"{indent}}} else {{")
            for nested in step["else"]:
                _component_step(nested, component, services, ctx, indent + "  ", lines, frame_var)
        lines.append(f"{indent}}}")
    elif kind == "fail":
        # A8: refusing activation is a throw out of the body. Whatever the
        # accumulator already holds is reverted by the runtime, so a partly
        # activated component still leaves no residue (R4).
        lines.append(f"{indent}throw new Error({_expr(step['message'], ctx)})")
    elif kind == "timer":
        # A timer (item 57): a revertible schedule. The firing closure holds
        # the body's emissions; `host.scheduleEvery`/`scheduleAfter` register it
        # with the clock coeffect and return a handle, and the yielded
        # `() => handle.cancel()` is the derived inverse — so unloading the
        # component cancels the timer through the same accumulator LIFO that
        # reverts every other effect (no orphaned interval; docs/time-coeffect.md).
        ctx._counter[0] += 1
        n = ctx._counter[0]
        fn = f"$revl_timer_{n}"
        handle = f"$revl_timer_{n}_h"
        verb = "scheduleEvery" if step.get("mode") == "every" else "scheduleAfter"
        emissions = step.get("body") or []
        for emission in emissions:
            if emission.get("step") != "emit":  # pragma: no cover — lowerer invariant
                raise EmitError(f"a timer body carries emissions only, "
                                f"found {emission.get('step')!r}")
        interval = int(step["interval_ms"])
        if not step.get("async"):
            lines.append(f"{indent}const {fn} = () => {{")
            for emission in emissions:
                lines.append(f"{indent}  {_expr(emission['expr'], ctx)}")
            lines.append(f"{indent}}}")
            lines.append(f"{indent}const {handle} = host.{verb}({interval}, {fn})")
            lines.append(indent + _bracket_yield(
                frame_var, handle, verb, f"{component['name']}.body:{handle}",
                "cancel", f"() => {handle}.cancel()"))
            return
        # async in-flight window (item 170): a timer body the frontend coloured
        # `async` reaches an async op, whose emission returns a `Promise`. Each
        # async emission is *spawned* as a tracked in-flight task (a per-timer
        # Set) rather than fired-and-forgotten un-awaited: the firing returns
        # immediately, and the harness's `_revl_settle` after a clock advance
        # drains the microtask-queued in-flight work to quiescence before the
        # next statement observes it (docs/time-coeffect.md §advance). The
        # inverse cancels the schedule AND drops every still-in-flight task, so a
        # torn-down timer leaves no orphaned in-flight reference (R4/A8) — the
        # sync path's residue-free teardown extended to the async case. (A JS
        # Promise is not abortable, so "drop in-flight" clears tracking rather
        # than aborting mid-flight the way py's `task.cancel()` does; the settle
        # before any assert has already drained the window, and a still-pending
        # body's effect lands in its own component's ledger, reverted there.) A
        # sync timer body carries no `async` key and emits byte-identically.
        inflight = f"$revl_timer_{n}_inflight"
        lines.append(f"{indent}const {inflight} = new Set<Promise<void>>()")
        lines.append(f"{indent}const {fn} = () => {{")
        for emission in emissions:
            expr = emission["expr"]
            rendered = _expr(expr, ctx)
            if _ts_emission_is_async(expr, ctx):
                # spawn the suspension into the in-flight window and track it so
                # the inverse can drop it; a settled task removes itself.
                lines.append(f"{indent}  const _revl_task: Promise<void> = "
                             f"Promise.resolve({rendered})"
                             f".then(() => {{ {inflight}.delete(_revl_task) }}, "
                             f"() => {{ {inflight}.delete(_revl_task) }})")
                lines.append(f"{indent}  {inflight}.add(_revl_task)")
            else:
                # a sync emission in a mixed body still fires inline
                lines.append(f"{indent}  {rendered}")
        lines.append(f"{indent}}}")
        lines.append(f"{indent}const {handle} = host.{verb}({interval}, {fn})")
        lines.append(indent + _bracket_yield(
            frame_var, handle, verb, f"{component['name']}.body:{handle}",
            "cancel", f"() => {{ {handle}.cancel(); {inflight}.clear() }}"))
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
    # item 167: routed requires (item 162's `routes` IR).
    routes = component.get("routes") or {}

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
    for key in routes:
        if key not in requires:
            raise EmitError(f"{name}: routed key {key!r} is not a requirement")

    lines = _config_interface(component)
    lines.append(f"export const {name} = {{")
    lines.append(f"  name: {_string(name)},")
    # item 167: routed keys never enter the inject gate — they have no
    # single-realm provider (the workers live in the named realms), so a fiber
    # waiting on one would pend forever. The router proxy resolves them lazily.
    inject_keys = [k for k in requires if k not in routes]
    if intercept:
        # v2: dict-form inject — non-null values are copied into the fiber
        # context's intercept chain (the consumer-declared d(k)); null marks a
        # required-but-not-intercepted key.
        inject = {key: intercept.get(key) for key in inject_keys}
        lines.append(f"  inject: {_json(inject)},")
    else:
        inject = ", ".join(_string(k) for k in inject_keys)
        lines.append(f"  inject: [{inject}],")
    if provides:
        keys = ", ".join(_string(k) for k in provides)
        lines.append(f"  provide: [{keys}],")

    # item 131: a body containing an `await` step (or an async-flagged
    # `effect`/`let-effect`/`emit` — an awaited acquisition or emission) compiles
    # to an async generator, whose awaited acquisitions land on LATER microtask
    # turns, AFTER a synchronous `apply` would already have returned. `apply`
    # must therefore be `async` and `await` that effect (see the `await
    # ctx.effect(...)` site below) so the fiber only reaches ACTIVE — and
    # `fiber.await()` only resolves — once the awaited acquisition has landed,
    # exactly as the py tier's await-to-ACTIVE waits for it. Timer steps are
    # excluded: a timer's async flag colors its OWN runtime-awaited firing (item
    # 170), not the activation body generator.
    is_async = any(
        step.get("step") == "await"
        or (step.get("step") in ("effect", "let-effect", "emit")
            and step.get("async"))
        for step in component.get("body") or []
    )
    apply_kw = "async apply" if is_async else "apply"

    if fields:
        lines.append(f"  {apply_kw}(ctx: Context, rawConfig: {name}Config) {{")
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
        lines.append(f"  {apply_kw}(ctx: Context) {{")

    # item 243 Slice 2b: the activation's teardown accumulator. `_needs_frame`
    # is the SENTINEL gate — only a transactional (witnessed) or compensation
    # entry needs `begin`/`drain`, and that is unchanged. `_has_bracket` is
    # the RESIDUE gate: a bracket-only component still builds the Frame, with
    # no sentinels, purely so `Frame.bracket` has somewhere to record a
    # Phase-1 `bracket-fault` (see `_bracket_yield`). Building it is
    # side-effect-free (the constructor reads two env bounds and joins a
    # WeakMap), so teardown behaviour is unchanged for those components except
    # that a raising inverse is now caught instead of starving every
    # earlier-registered entry.
    needs_frame = _needs_frame(component, doc_ctx)
    frame_var = "$revl_frame" if (needs_frame or _has_bracket(component)) else None
    if frame_var is not None:
        lines.append(f"    const {frame_var} = new Frame(ctx, {_string(name)})")

    # item 167: build one router proxy per routed key before the body, so a
    # provide-method reading `<key>` fans each call out across its worker realms
    # (round-robin/least_loaded, re-resolving liveness per call).
    for key in routes:
        route = routes[key]
        realms = "[" + ", ".join(_string(r) for r in route.get("realms") or []) + "]"
        strategy = _string(route["strategy"]) if route.get("strategy") else "undefined"
        lines.append(
            f"    const _revl_route_{_ident(key, 'requirement')} = "
            f"revlRouter(ctx, {_string(key)}, {realms}, {strategy})"
        )

    # One generator per body: cordis runs disposers of a single effect
    # strictly sequentially (LIFO); top-level fiber effects would be
    # disposed concurrently (see REPORT.md, finding 1). A body containing
    # an `await` step compiles to an async generator (v1/A1) — see the
    # `is_async` computation above the `apply` signature (item 131), which also
    # made `apply` `async`.
    generator = "async function*" if is_async else "function*"
    # item 131: an async body's `ctx.effect(async function* ...)` drives its
    # awaited acquisitions on LATER microtask turns, so the acquisitions
    # themselves (ACQ B in async_effect_composition) and the disposers the
    # generator yields land AFTER a synchronous `apply` would have returned.
    # `ctx.effect(...)` returns a thenable wrapper whose `.then` resolves only
    # once that async generator has run to completion — but the wrapper is a
    # FUNCTION, so returning it from `apply` makes cordis' fiber-body runner
    # collect it as a plain disposer (`_execute`, `typeof effect === "function"`,
    # node_modules/cordis:809) and never await it. So `await` the wrapper inside
    # an `async apply`: `apply` then returns a real Promise, which `_execute`
    # DOES chain onto (`"then" in effect`, :814), so `_reload` (and thus
    # `fiber.await()`) resolves only after the awaited acquisition has landed —
    # exactly as the py tier's await-to-ACTIVE waits for it. The wrapper is
    # already registered for disposal by the `ctx.effect` call itself
    # (node_modules/cordis:892), so awaiting its VALUE does not dispose it; LIFO
    # teardown across the suspension is unchanged. A SYNC body's generator runs
    # to completion synchronously inside `ctx.effect`, so it stays a bare,
    # non-awaited statement (unchanged, byte-identical for every prior program).
    effect_stmt = "await " if is_async else ""
    lines.append(f"    {effect_stmt}ctx.effect({generator} () {{")
    # item 243 Slice 2b: two sentinel yields bracket the ordinary steps, ONLY
    # when this component needs `Frame` at all (see above). `begin` yielded
    # FIRST -> disposed LAST (cordis LIFO): on abort it is the Phase-2
    # post-unwind hook; `drain` yielded LAST -> disposed FIRST, only reached
    # if the body ran to completion, and is the commit signal every
    # earlier-registered entry reads at its OWN disposal time. See
    # runtime.ts's Frame section doc for the full mechanism.
    if needs_frame:
        lines.append(f"      yield {frame_var}.begin")
    lines.extend(_component_body(component, services, "      ", doc_ctx, frame_var))
    if needs_frame:
        lines.append(f"      yield {frame_var}.drain")
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
    # `Any` is the type algebra's wildcard for values whose static type only
    # the runtime knows (stdlib/json.rvl `json_parse(s: Str) -> Any`). It maps
    # to TS `any`, not `unknown`: `any` is assignable in both directions, so a
    # parsed `Any` flows into a typed position (`let tc: ToolCall =
    # json_parse(s)`) *and* supports field access (`tc.name`) under `strict`,
    # which `unknown` would reject. Without this entry `_ts_v3_type("Any")`
    # fell through to `_ident` and emitted a bare `Any` that `tsc` rejects with
    # `Cannot find name 'Any'` in every non-signature position (roadmap 79).
    "Any": "any",
    # `Never` is the empty type; TS spells it `never`.
    "Never": "never",
}

_TS_V3_BIN_OPS = {
    "==": "===", "===": "===", "!=": "!==", "!==": "!==",
    "<": "<", ">": ">", "<=": "<=", ">=": ">=",
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
    "&&": "&&", "||": "||",
    # Int32 bitwise operators (item 366, docs/arithmetic.md). Int32 is a JS
    # `number`, and JS's `& | ^ << >>` all coerce to a signed 32-bit int, mask a
    # shift count to its low 5 bits (mod 32), and return a signed i32 `number` —
    # which is exactly the Int32 semantics, so no `revlI32` re-wrap is needed.
    # `>>` is the arithmetic (sign-propagating) shift (`>>>` would be logical).
    "&": "&", "|": "|", "^": "^", "<<": "<<", ">>": ">>",
}

_HOST_ROOTS = {"Pool", "Map", "Job"}
_BUILTIN_CONSTRUCTORS = {"Some", "None", "Ok", "Err"}

# item 416a: host roots this tier's `runtime.ts` does NOT implement. A `host.<X>`
# call is emitted verbatim against whatever `host` supplies, so a root with no
# runtime behind it produced a program that compiled here and died in the
# consumer's own build with a name error — a SILENT EMIT where the design
# promises a refusal. `subscribe` was already refused; `Stream.source()` alone
# was not, so the honest refusal only fired for half the surface. Refuse the
# whole root, in the shape wasm uses, and name the tiers that do carry it.
_UNIMPLEMENTED_HOST_ROOTS = {
    "Stream": (
        "opens a stream, and a stream subscription suspends a fiber. This tier "
        "has no `Stream`/`Subscription` runtime primitive at all (`runtime.ts` "
        "implements Pool, Map and Job), so the emitted program would name a "
        "host object that does not exist: streams run on py, go and rust "
        "(item 130 §4.6, tracked for this tier as roadmap 419e); try "
        "`--backend py`"
    ),
}


def _refuse_missing_host_root(fn: str) -> None:
    """Refuse a host builtin whose ROOT this tier has no runtime for, instead of
    emitting a call against a name the target does not define."""
    root = fn.split(".")[0]
    reason = _UNIMPLEMENTED_HOST_ROOTS.get(root)
    if reason is not None:
        raise EmitError(f"`{fn}` {reason}")

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
        if base == "Async":
            # item 92: an async function-type return `(…) -> Async[T]` renders
            # `Promise<T>`, so a colored callback parameter types as
            # `((a0: …) => Promise<T>)` through the FN branch above, and its
            # awaited call sites line up with the `Promise` it returns.
            return f"Promise<{_ts_v3_type(args[0])}>"
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
                 component_scope=None, counter=None, in_async=False,
                 services: dict = None) -> None:
        self.types = types or {}
        # item 141: async service operations, keyed `(service, method)`. An
        # emission of one through a req key (`emit agent.run_in(...)`) returns a
        # Promise on this tier; the await-seed in `_expr`'s component-`call`
        # branch awaits it wherever it lands in an async body — including a
        # NESTED expression position such as a ternary arm — so no Promise leaks
        # un-awaited (e.g. into a template string). Mirrors py's
        # `_PY_ASYNC_SVC_OPS`.
        self.async_ops: set = {
            (svc, method)
            for svc, spec in (services or {}).items()
            for method, mspec in (spec.get("methods") or {}).items()
            if mspec.get("async")
        }
        self.function_names = {fn.get("name") for fn in functions or []}
        self.extern_names = {ext.get("name") for ext in externs or []}
        # item 243 (docs/design/243-witnessed-externs.md): witnessed externs by
        # name, so a call site can be recognised as a transactional effect and
        # register its DECLARED inverse (not a site-spelled one) into the
        # Frame accumulator. Mirrors backends/python/emit.py's
        # `_ComponentEmitter.witnessed`. Absent/empty for every program that
        # uses no witnessed extern, so their emission is unaffected.
        self.witnessed = {
            ext["name"]: ext for ext in (externs or [])
            if ext.get("class") == "witnessed"
        }
        # async callables (roadmap item 80): call sites naming one are awaited
        # (docs/design/async-extern.md §5). Seeded from async externs *and*
        # phase-2 async-colored module fns (both carry `"async": True` on their
        # IR entry, stamped by the frontend fixed point) — so a call to a
        # colored helper from another async context is awaited just like an
        # extern call. `in_async` tracks whether the body currently being
        # rendered may await (an `async fn` provide method, or a colored fn).
        self.async_names = {ext.get("name") for ext in externs or []
                            if ext.get("async")} | {
                            fn.get("name") for fn in functions or []
                            if fn.get("async")}
        # async value locals (roadmap item 92): the *parameters* of the body
        # currently rendered whose declared type is an async function type
        # `(…) -> Async[T]`. A call through one returns a `Promise<T>` and is
        # awaited just like a named async callable — but it is a per-body set
        # (a local name), not a document-level one, so it is threaded through
        # `with_scope`, not seeded here.
        self.async_locals: set = set()
        self.in_async = in_async
        # item 141: are we rendering INSIDE an arrow body? An async arrow renders
        # `async (…) => (<tail>)` and returning a Promise from it flattens, so the
        # await-seed stays OUT of arrow bodies — keeping the item-92 async-arrow
        # shape byte-identical and matching the py backend's arrow suppression.
        self.in_arrow = False
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

    def with_scope(self, scope, in_async=None, async_locals=None,
                   in_arrow=None) -> "_Ctx":
        view = _Ctx.__new__(_Ctx)
        view.types = self.types
        view.function_names = self.function_names
        view.extern_names = self.extern_names
        view.witnessed = self.witnessed
        view.async_names = self.async_names
        view.async_ops = self.async_ops
        view.async_locals = self.async_locals if async_locals is None else async_locals
        view.in_async = self.in_async if in_async is None else in_async
        view.in_arrow = self.in_arrow if in_arrow is None else in_arrow
        view.case_names = self.case_names
        view._counter = self._counter
        view.component_scope = scope
        return view


def _v3_var(node: dict, ctx: "_Ctx") -> str:
    name = node.get("name")
    # a keyword-named local/function/case is renamed at its *use* the same way
    # `_ident` renamed it at its declaration; the host roots take the
    # `host.<name>` branch (their names are never keywords) (item 165)
    mangled = _ident(name, "name")
    if name in ctx.function_names or name in ctx.extern_names or name in ctx.case_names:
        return mangled
    if name in _HOST_ROOTS:
        return f"host.{name}"
    if name == "None":
        return "undefined"
    if name in ("Some", "Ok", "Err"):
        return "((value) => value)"
    return mangled


def _ts_builtin(method, target: str, args: list, arg_nodes: list, ctx: "_Ctx",
                recv: str | None = None) -> str:
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
    # Codepoint-at-index scan (item 276, docs/stdlib-2.0.md §Str.codepoint_at):
    # the Unicode scalar at index i, via the same astral-aware helper as
    # charCodeAt (JS `.charCodeAt` would answer a lone surrogate).
    if method == "codepoint_at":
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
    # `to_int` is ALSO the Str parse (FR-9, docs/stdlib-2.0.md §Str.to_int):
    # `revlParseInt` answers `bigint | undefined` — the tier's Opt[Int] —
    # rejecting empty/partial/`+`-prefixed spellings and anything out of the
    # i64 range, so `BigInt` never sees a string it would throw on.
    if method == "to_int":
        if recv == "Str":
            return f"revlParseInt({target})"
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
    # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith).
    # A code-point prefix of a string is a UTF-16 prefix (code-point
    # boundaries never split), so the native startsWith/endsWith are exact.
    if method == "startsWith":
        return f"{target}.startsWith({args[0]})"
    if method == "endsWith":
        return f"{target}.endsWith({args[0]})"
    # Single-character ASCII classification (item 233, docs/stdlib-2.0.md
    # §Str.is_alnum), mirroring the python backend's native forms
    # (backends/python/emit.py §is_digit/is_alpha/is_alnum/is_space) and the
    # rust backend (backends/rust/emit.py). JS `<=`/`<` on strings is UTF-16
    # code-unit lexicographic order, and code-unit order IS code-point order
    # for ASCII, so it matches python's chained string comparison exactly. It
    # stays total the same way: an empty receiver compares less than `"0"`, so
    # the verdict is `false` rather than a fault, and multi-character input
    # (outside the per-character contract) never raises. The receiver is bound
    # once by an arrow IIFE (`_rc`) — correct even when it has side effects,
    # since these builtins re-reference it — with no revl-fn call.
    if method == "is_digit":
        return f'((_rc: string) => "0" <= _rc && _rc <= "9")({target})'
    if method == "is_alpha":
        return (f'((_rc: string) => ("a" <= _rc && _rc <= "z") '
                f'|| ("A" <= _rc && _rc <= "Z"))({target})')
    if method == "is_alnum":
        return (f'((_rc: string) => ("0" <= _rc && _rc <= "9") '
                f'|| ("a" <= _rc && _rc <= "z") '
                f'|| ("A" <= _rc && _rc <= "Z"))({target})')
    # is_space: space, tab, LF, CR — equality with each element (python uses
    # tuple membership; a `String.includes` would wrongly match the empty
    # receiver, which is a substring of every string).
    if method == "is_space":
        return (f'((_rc: string) => _rc === " " || _rc === "\\t" '
                f'|| _rc === "\\n" || _rc === "\\r")({target})')
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


def _renders_await(text: str) -> bool:
    """Does this rendered fragment actually contain an `await` operator?

    Item 435(a)/(b): the async colour has to follow what was RENDERED, because
    `ctx.in_async` answers a question about the ENCLOSING function and not
    about this expression.

    The test is the literal `"await "`, not a word-boundary regex, for two
    reasons. Every `await` this emitter writes is followed by a space
    (`(await {rendered})`, `(await (async (…`), so the substring is exact for
    the operator; and `selfhost/emit_ts.rvl` has to mirror this predicate
    byte-for-byte with no regex engine, where `Str.indexOf` is the whole
    implementation. It errs in one direction only: a stray `await ` inside a
    string literal, or one belonging to a nested `async` arrow that already
    flattens its own Promise, keeps an `async` that is not strictly needed.
    That is exactly the shape emitted before this change, so a false positive
    costs a microtask turn and can never cost correctness.
    """
    return "await " in text


def _v3_emission_call_node(node, ctx: "_Ctx") -> bool:
    """Is this IR node a call to an ASYNC service operation through a `req`?

    That is the one expression the emitter renders as a bare, un-awaited
    Promise: the `call` branch of `_expr` suppresses its await-seed inside an
    arrow (`ctx.in_async and not ctx.in_arrow`). Item 435(b) needs to tell that
    node apart from a body that evaluates to a plain value, because dropping
    `async` from an arrow is only type-preserving when the body is already the
    Promise the arrow was going to return.
    """
    if not isinstance(node, dict) or node.get("kind") != "call":
        return False
    target = node.get("target")
    if not (isinstance(target, dict) and target.get("kind") == "req"):
        return False
    scope = ctx.component_scope
    if scope is None:
        return False
    return (scope.requires.get(target.get("name")), node.get("method")) in ctx.async_ops


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


def _v3_do_expr(node: dict, ctx: "_Ctx") -> str:
    """A statement-block match arm (`=> { let x = …; expr }`) lowered inline in
    a provide-method body (roadmap item 361): an immediately-invoked arrow so
    the block's `let` bindings and final value live in their own scope. In an
    async method the arrow is `async` and its invocation awaited, so an async
    extern reached in the block is awaited within the method's in-flight
    window (the same async-shape the match IIFE uses for its arm arrows)."""
    if ctx.component_scope is None:
        raise EmitError("a `do` block arm requires a component/method body")
    inner = ctx.component_scope.child()
    body_ctx = ctx.with_scope(inner)
    lines: list[str] = []
    for st in node.get("stmts") or []:
        if st.get("step") != "let":
            raise EmitError(f"unsupported step in a `do` block arm: {st.get('step')!r}")
        value = _expr(st.get("value"), body_ctx)
        name = inner.bind(st.get("name"))
        keyword = "let" if st.get("mutable") else "const"
        lines.append(f"{keyword} {name} = {value};")
    lines.append(f"return {_expr(node.get('tail'), body_ctx)};")
    a = "async " if ctx.in_async else ""
    body = " ".join(lines)
    call = f"({a}() => {{ {body} }})()"
    return f"(await {call})" if ctx.in_async else call


def _v3_match_expr(node: dict, ctx: "_Ctx") -> str:
    tmp = ctx.new_match_tmp()
    scrutinee = _expr(node.get("scrutinee"), ctx)
    arms = node.get("arms") or []

    # In an async context (an `async fn` provide method or a phase-2 colored
    # fn, docs/design/async-extern.md §3) an arm body may `await` an async
    # callable. The IIFE-and-arm-arrows the match lowers to must then be
    # `async`, and their invocations awaited, or the `await` lands in a sync
    # arrow, a tsc error.
    #
    # Item 435(a): the colour follows the RENDERED body, not the enclosing
    # function. `ctx.in_async` is true for the whole body of an async-coloured
    # `fn`, so colouring by it wrapped a match whose every arm is a bound
    # variable or a literal in a Promise that was allocated, resolved and
    # awaited around a value already in hand, measured at 2 excess microtask
    # turns and 4 excess Promise allocations per evaluation
    # (`bench/codegen/typescript/cases/match_sync_arms.ts`). An arm arrow is
    # now `async` only when that arm's body renders an `await`, and the IIFE
    # only when the assembled body does; the `await` at each call site follows
    # the same decision. The py tier reached this from the other direction
    # (`backends/python/emit.py` `_match_expr(..., awaited=…)`, item 263).
    #
    # `colour_by_text` is False inside an ARROW, where the unconditional
    # colour stays. The await-seed for a `req` emission is deliberately
    # suppressed there (`ctx.in_async and not ctx.in_arrow`, the `call` branch
    # of `_expr`), so an arm body can be an un-awaited Promise with no `await`
    # in its text and the arm arrow's own `async`/`await` pair is what resolves
    # it. De-colouring there would hand the caller a Promise for a value.
    colour_by_text = ctx.in_async and not ctx.in_arrow

    def arm_async(body: str) -> bool:
        if not ctx.in_async:
            return False
        return (not colour_by_text) or _renders_await(body)

    def wrap(body_lines: list[str]) -> str:
        # `(await ( <fn> )( <scrut> ))` when async: the await wraps the whole
        # *invocation*, not the function object. Else `( <fn> )( <scrut> )`.
        outer = ctx.in_async and (
            (not colour_by_text) or _renders_await("\n".join(body_lines)))
        a = "async " if outer else ""
        pre = "(await (" if outer else "("
        post_tail = ")" if outer else ""
        return "\n".join(
            [f"{pre}{a}({tmp}) => {{", *body_lines, f"}})({scrutinee}){post_tail}"])

    # Opt is `value | undefined` (not tagged): Some/None discriminate on
    # undefined, and Some binds the scrutinee itself.
    if any(arm.get("pattern") in ("Some", "None") for arm in arms):
        lines: list[str] = []
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
                    lines.append(f"  if ({tmp} !== undefined) return (await (async ({b}) => ({body}))({tmp}))"
                                 if arm_async(body)
                                 else f"  if ({tmp} !== undefined) return (({b}) => ({body}))({tmp})")
                else:
                    lines.append(f"  if ({tmp} !== undefined) return ({body})")
        lines.append(wildcard if wildcard is not None
                     else '  throw new TypeError("non-exhaustive match")')
        return wrap(lines)

    lines = [f"  switch ({tmp}.kind) {{"]
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
            lines.append(f"      return (await (async ({bind}) => ({body}))({tmp}.value))"
                         if arm_async(body)
                         else f"      return (({bind}) => ({body}))({tmp}.value)")
        else:
            lines.append(f"      return ({body})")
    if wildcard is None:
        lines.append("    default:")
        lines.append('      throw new TypeError("non-exhaustive match")')
    else:
        lines.append("    default:")
        lines.append(wildcard)
    lines.append("  }")
    return wrap(lines)



# item 379 (docs/design/379-break-continue.md): the frame-neutrality invariant is
# enforced whole-IR in the frontend; this is the cheap per-emitter guard.
_LOOP_REGISTERING_STEPS = frozenset({
    "effect", "let-effect", "emit", "timer", "approval", "spawn",
})


def _guard_frame_neutral_loop(body) -> None:
    for child in body or []:
        if isinstance(child, dict) and child.get("step") in _LOOP_REGISTERING_STEPS:
            raise EmitError(
                f"frame-neutral loop invariant: a `{child['step']}` step inside a "
                "while/for body (docs/design/379-break-continue.md)")


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
        _guard_frame_neutral_loop(node.get("body"))
        out.append(f"{'  ' * indent}while ({_expr(node['cond'], ctx)}) {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{'  ' * indent}}}")
    elif step == "for":
        _guard_frame_neutral_loop(node.get("body"))
        bind = _ident(node.get("bind"), "loop binding")
        out.append(f"{'  ' * indent}for (const {bind} of {_expr(node['iterable'], ctx)}) {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{'  ' * indent}}}")
    elif step == "break":
        out.append(f"{'  ' * indent}break")
    elif step == "continue":
        out.append(f"{'  ' * indent}continue")
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
            # item 143: the assert temporaries carry a `$` sigil — not in revl's
            # identifier alphabet — so `const $revl_l = <left>` can never read a
            # user binding named `l`/`r` before its own TDZ ends (the same
            # collision-proof convention as `$revl_match_N`). A plain `l`/`r`
            # collided with a `let r = …` in scope: `const l = r, r = …` read the
            # block's own not-yet-initialised `r` (ReferenceError, finding #39).
            out.append(f"{pad}{{ const $revl_l = {left}, $revl_r = {right};")
            out.append(f"{pad}  expect(revlEq($revl_l, $revl_r), {shown} + "
                       f'"\\n  left  = " + revlShow($revl_l) + '
                       f'"\\n  right = " + revlShow($revl_r)).toBe({want}) }}')
        elif test_mode:
            out.append(f"{pad}expect({_expr(expr, ctx)}).toBeTruthy()")
        elif expr.get("kind") == "bin" and expr.get("op") in (
                "==", "===", "!=", "!==", "<", ">", "<=", ">="):
            op = _TS_V3_BIN_OPS[expr["op"]]
            left = _expr(expr["left"], ctx)
            right = _expr(expr["right"], ctx)
            shown = json.dumps(f"{left} {op} {right}")
            # item 143: `$`-sigil temporaries can't collide with a user binding.
            out.append(f"{pad}{{ const $revl_l = {left}, $revl_r = {right};")
            out.append(f"{pad}  if (!($revl_l {op} $revl_r)) throw new Error({shown} + "
                       f'"\\n  left  = " + revlShow($revl_l) + '
                       f'"\\n  right = " + revlShow($revl_r)) }}')
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
                        "codepoint_at": (0,), "repeat": (0,)}
_TS_INT_RESULT_BUILTINS = {"length", "indexOf", "charCodeAt", "codepoint_at"}

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


_TS_ROUTER_SRC = """// item 167: the emitted realization of a routed require (item 162's `routes`
// IR), mirroring src/revl/run.py::_Router. A component that
// `requires <k> in realms("w1"…"wN") strategy(...)` provides <k> once
// downstream (G2) while fanning each call out across the worker realms. The
// proxy holds no worker handle — it re-resolves the live per-realm handle on
// every call (`ctx.root.isolate(k, realmLabel(w)).reflect.get(k)`, nullish for
// a non-ACTIVE provider), so a withdrawn worker drops out and its calls go to
// the survivors (reactive failover).
function revlRouter(
  ctx: Context,
  key: string,
  realms: string[],
  strategy?: string,
): any {
  const root = ctx.root
  const strat = strategy ?? 'round_robin'
  let cursor = 0
  const served: Record<string, number> = {}
  for (const r of realms) served[r] = 0
  const handle = (realm: string): any =>
    (root as any).isolate(key, realmLabel(realm)).reflect.get(key)
  const live = (): Array<[string, any]> =>
    realms.map((r) => [r, handle(r)] as [string, any]).filter(([, h]) => h != null)
  const select = (): any => {
    const l = live()
    if (l.length === 0) {
      throw new Error(
        `revl: router for ${JSON.stringify(key)} has no live worker: all ` +
          `${realms.length} realm(s) (${realms.join(', ')}) have withdrawn`,
      )
    }
    let chosen: [string, any] | undefined
    if (strat === 'least_loaded') {
      // route to the live realm served fewest; ties keep declaration order
      chosen = l.reduce((a, b) => (served[b[0]] < served[a[0]] ? b : a))
    } else {
      // round_robin — next live realm in declaration order
      const n = realms.length
      for (let off = 0; off < n; off++) {
        const cand = realms[(cursor + off) % n]
        const m = l.find(([rlm]) => rlm === cand)
        if (m) {
          cursor = (cursor + off + 1) % n
          chosen = m
          break
        }
      }
    }
    served[chosen![0]]++
    return chosen![1]
  }
  return new Proxy(
    {},
    {
      get(_t, method) {
        if (typeof method !== 'string') return undefined
        return (...args: any[]) => (select() as any)[method](...args)
      },
    },
  )
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
    if _uses_parse_int(ir):
        out.extend([_REVL_PARSE_INT_HELPER, ""])
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
# surrogate. The runtime dispatch is on `typeof x === "string"`.
#
# `revlSlice` is *overloaded* rather than one union signature: the frontend
# statically knows the receiver kind (Str/List/Bytes) and spells it into the
# emitted signatures (fn params, config fields, service interfaces), so TS
# resolves each call site to `string` / `T[]` / `Uint8Array` and a method
# chained on the result — `rest.slice(10, len).split(" ")`,
# `parts.slice(1, n).join(" ")`, `xs.slice(0, 2).push(v)` — typechecks. One
# union return `string | T[]` made every such chain a `tsc` error (FR-7: the
# TS tier emitted code its own compiler rejected). The `unknown` overload
# keeps a receiver whose kind genuinely cannot be pinned (an untyped/`any`
# position — previously the union signature still accepted it) compiling to
# the union rather than failing overload resolution, and the last signature
# is the implementation: it keeps the runtime dispatch for that same case,
# where lying with a cast would be worse than admitting the union.
_REVL_STR_HELPER = """function revlLen(x: string | ArrayLike<unknown>): bigint {
  return BigInt(typeof x === "string" ? Array.from(x).length : x.length)
}
function revlSlice(x: string, a: bigint, b: bigint): string
function revlSlice(x: Uint8Array, a: bigint, b: bigint): Uint8Array
function revlSlice<T>(x: T[], a: bigint, b: bigint): T[]
function revlSlice(x: unknown, a: bigint, b: bigint): string | Uint8Array | unknown[]
function revlSlice(x: unknown, a: bigint, b: bigint): string | Uint8Array | unknown[] {
  const i = Number(a), j = Number(b)
  return typeof x === "string"
    ? Array.from(x).slice(i, j).join("")
    : (x as string | Uint8Array | unknown[]).slice(i, j)
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

_STR_METHOD_NAMES = {"length", "slice", "charAt", "charCodeAt", "codepoint_at",
                     "indexOf"}


# Str.to_int (FR-9, docs/stdlib-2.0.md §Str.to_int): total on the ASCII digits
# with an optional leading `-`, `undefined` (the tier's Opt None) otherwise.
# The regex gates before BigInt so a bad spelling never throws; the range
# check then enforces the i64 bound, because `BigInt` is unbounded.
_REVL_PARSE_INT_HELPER = """function revlParseInt(s: string): bigint | undefined {
  if (!/^-?\\d+$/.test(s)) return undefined
  const n = BigInt(s)
  if (n < -9223372036854775808n || n > 9223372036854775807n) return undefined
  return n
}"""


def _uses_parse_int(node) -> bool:
    """Does this IR call `Str.to_int` (the parse form, not the Int32 widen)?
    Only that form needs `revlParseInt`; the widen lowers to bare `BigInt`."""
    if isinstance(node, dict):
        if (node.get("kind") == "builtin" and node.get("method") == "to_int"
                and node.get("recv") == "Str"):
            return True
        return any(_uses_parse_int(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_parse_int(v) for v in node)
    return False


def _uses_str_methods(node) -> bool:
    """Does this IR call one of the code-point string helpers — a `length`
    field-read (`len`) or a `length`/`slice`/`charAt`/`charCodeAt`/`indexOf`
    builtin? These share a receiver with List/Bytes, so the helper dispatches
    at runtime; it is emitted only where one of these forms appears."""
    if isinstance(node, dict):
        if node.get("kind") == "len":
            return True
        # item 104: a component-position sized `.length` stays a `field` node
        # marked `sized_length` and emits `revlLen(...)`, so the helper must be
        # emitted for it too (else `revlLen` is undefined at runtime).
        if node.get("kind") == "field" and node.get("sized_length"):
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
                lines.append(f"  {_prop_key(field, 'record field')}: {_ts_v3_type(ftype)}")
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
        # a phase-2 async-colored fn (docs/design/async-extern.md §3) emits as
        # `async function …: Promise<T>`, and its body is rendered in an async
        # context so every call to an async callable is awaited (see `_expr`).
        # The color was decided by the frontend fixed point and stamped on the
        # IR entry — the emitter only reads `.get("async")`, mirroring the
        # extern signature form at _emit_ts_externs.
        if fn.get("async"):
            # item 92: a parameter declared `(…) -> Async[T]` is an async value
            # local — a call through it returns a Promise and is awaited.
            async_locals = {
                _ident(p.get("name"), "parameter name")
                for p in fn.get("params") or []
                if _is_async_fn_type(p.get("type"))
            }
            fn_ctx = ctx.with_scope(ctx.component_scope, in_async=True,
                                    async_locals=async_locals)
            lines.append(
                f"export async function {name}({params}): Promise<{returns}> {{")
        else:
            fn_ctx = ctx
            lines.append(f"export function {name}({params}): {returns} {{")
        if not fn.get("body"):
            lines.append("  // (empty body)")
        else:
            for stmt in fn["body"]:
                _v3_stmt(stmt, fn_ctx, lines, 2, test_mode=False)
        lines.append("}")
        lines.append("")
    return lines


def _emit_ts_ref_runtime() -> list[str]:
    """item 396 option B: the module-level ref machinery. `_revl_ref_path` joins
    a root the RUNNER provides at run time (`globalThis.__REVL_REF_ROOT__`, set
    from the spec `run_ts` writes) with the recorded root-relative path — so the
    emitted text carries no machine-specific path. `_revl_require` loads a
    synchronous ESM graph for a sync ref (node's require(esm) support)."""
    return [
        "const _REVL_REFS = new Map<string, any>()",
        "const _revl_require = _revl_createRequire(import.meta.url)",
        "function _revl_ref_path(rel: string): string {",
        "  const root = (globalThis as any).__REVL_REF_ROOT__",
        "  if (root === undefined)",
        "    throw new Error('revl: no host-ref root set; the runner must set "
        "globalThis.__REVL_REF_ROOT__ (item 396 option B)')",
        "  return _revl_pathmod.resolve(root, rel)",
        "}",
        # item 410: a stdlib-origin ref resolves against a SECOND runner-provided
        # root (the install tree), never the user root. No fallback in either
        # direction: a stdlib-kind thunk with no stdlib root set fails loudly
        # naming the missing knob, and a user thunk never reads this global. The
        # two globals ARE the two trust domains, and the emitted text stays
        # machine-independent.
        "function _revl_ref_path_stdlib(rel: string): string {",
        "  const root = (globalThis as any).__REVL_STDLIB_REF_ROOT__",
        "  if (root === undefined)",
        "    throw new Error('revl: no stdlib host-ref root set; the runner must "
        "set globalThis.__REVL_STDLIB_REF_ROOT__ (item 410 two-root scheme)')",
        "  return _revl_pathmod.resolve(root, rel)",
        "}",
        "",
    ]


def _emit_ts_ref_thunk(name: str, params_decl: str, arg_names: str,
                       returns: str, ext: dict, ref: dict) -> list[str]:
    """The lazy import thunk for a `@ts ref` extern. Sync goes through
    `createRequire` (a synchronous ESM load); async through a dynamic `import()`.
    The colour assertion mirrors py's where the tier allows: a declared-SYNC ref
    whose resolved symbol is an async function (`constructor.name ===
    'AsyncFunction'`) is refused at first call. The async direction cannot be
    asserted structurally (an ordinary function may legitimately return a
    promise), so it stays awaited-by-name — loud-wrong at worst, never silent.
    The `constructor.name` check has the same evasion the py `iscoroutinefunction`
    check has (a wrapper/callable-instance can hide the async shape); stated, not
    claimed away (design re-review #3)."""
    rel = ref["path"]
    symbol = _ident(ref["symbol"], "ref symbol")
    where = f"{rel}#{symbol}"
    is_async = bool(ext.get("async"))
    # item 410: a stdlib-origin ref (`"root": "stdlib"`) resolves against the
    # install root (`_revl_ref_path_stdlib`); a user ref against the user root
    # (`_revl_ref_path`, unchanged). Kind-dispatched, no cross-domain fallback.
    path_fn = ("_revl_ref_path_stdlib" if ref.get("root") == "stdlib"
               else "_revl_ref_path")
    lines: list[str] = []
    if is_async:
        lines.append(
            f"export async function {name}({params_decl}): Promise<{returns}> {{")
        lines.append(f"  let _f = _REVL_REFS.get({_string(name)})")
        lines.append("  if (_f === undefined) {")
        lines.append(f"    const _m = await import("
                     f"_revl_pathToFileURL({path_fn}({_string(rel)})).href)")
        lines.append(f"    _f = _m[{_string(symbol)}]")
        lines.append(f"    if (typeof _f !== 'function') throw new Error("
                     f"{_string(f'revl extern `{name}`: {where} is not a function')})")
        lines.append(f"    _REVL_REFS.set({_string(name)}, _f)")
        lines.append("  }")
        lines.append(f"  return await _f({arg_names})")
        lines.append("}")
    else:
        lines.append(f"export function {name}({params_decl}): {returns} {{")
        lines.append(f"  let _f = _REVL_REFS.get({_string(name)})")
        lines.append("  if (_f === undefined) {")
        lines.append(f"    const _m = _revl_require({path_fn}({_string(rel)}))")
        lines.append(f"    _f = _m[{_string(symbol)}]")
        lines.append(f"    if (typeof _f !== 'function') throw new Error("
                     f"{_string(f'revl extern `{name}`: {where} is not a function')})")
        lines.append("    if (_f.constructor && _f.constructor.name === 'AsyncFunction')")
        lines.append(f"      throw new Error({_string(f'revl extern `{name}` is declared sync but {where} is an async function; declare the extern async or ref a sync symbol')})")
        lines.append(f"    _REVL_REFS.set({_string(name)}, _f)")
        lines.append("  }")
        lines.append(f"  return _f({arg_names})")
        lines.append("}")
    lines.append("")
    return lines


def _ts_extern_config_scaffold() -> list[str]:
    """Module-level config seam for document-global config externs (item 378,
    Stage 5). Mirrors the py tier's `_REVL_EXTERN_CONFIG` map + fail-loud
    `_revl_extern_config` helper (backends/python/emit.py `_emit_externs`): a
    mutable module-global config map, keyed by extern name, that a composition
    driver fills at plug time, and a lookup that THROWS, naming the extern,
    when a required (non-defaulted) field is absent, instead of handing the body
    an empty object that fails late with an opaque `undefined`. A defaults-only
    extern still resolves to its defaults driver-free. Emitted only when a
    config extern is present, so a no-config program is byte-identical.
    """
    return [
        "export const _REVL_EXTERN_CONFIG: "
        "Record<string, Record<string, unknown>> = {};",
        "",
        "function _revlExternConfig(",
        "  name: string, required: string[], "
        "defaults: Record<string, unknown>,",
        "): Record<string, unknown> {",
        "  const cfg = _REVL_EXTERN_CONFIG[name];",
        "  if (cfg === undefined) {",
        "    if (required.length > 0) {",
        "      throw new Error(",
        '        "config extern `" + name + "` called before plug-time " +',
        '        "configuration was installed (required config: " +',
        '        required.join(", ") + "); configure it through the run " +',
        "        \"driver's config seam\",",
        "      );",
        "    }",
        "    return { ...defaults };",
        "  }",
        "  const missing = required.filter((f) => !(f in cfg));",
        "  if (missing.length > 0) {",
        "    throw new Error(",
        '      "config extern `" + name + "` called before plug-time " +',
        '      "configuration was installed (missing required config: " +',
        '      missing.join(", ") + ")",',
        "    );",
        "  }",
        "  return { ...defaults, ...cfg };",
        "}",
        "",
    ]


def _ts_extern_config_bind(ext: dict) -> str:
    """The `const _revl_config = ...` first-body line for a config extern, or
    None. Passes the required (non-defaulted) field names and the resolved
    defaults from the schema to the fail-loud helper, mirroring the py bind."""
    schema = ext.get("config")
    if not schema:
        return None
    name = ext.get("name")
    required = [f["name"] for f in schema if f.get("default") is None]
    defaults = {f["name"]: f["default"] for f in schema
                if f.get("default") is not None}
    return (f"const _revl_config = _revlExternConfig("
            f"{json.dumps(name)}, {json.dumps(required)}, "
            f"{json.dumps(defaults)});")


def _emit_ts_externs(externs: list) -> list[str]:
    lines: list[str] = []
    # item 378 Stage 5: emit the config seam once, before the externs, when any
    # extern carries a config schema (byte-identical when none do).
    if any(ext.get("config") for ext in externs):
        lines.extend(_ts_extern_config_scaffold())
    for ext in externs:
        name = _ident(ext.get("name"), "extern name")
        params = ", ".join(
            f"{_ident(p.get('name'), 'extern parameter name')}: {_ts_v3_type(p.get('type'))}"
            for p in ext.get("params") or []
        )
        returns = _ts_v3_type(ext.get("returns"))
        bodies = ext.get("bodies") or {}
        refs = ext.get("refs") or {}
        # item 396 option B: a `@ts ref` extern emits a lazy import thunk.
        if "ts" in refs and "ts" not in bodies:
            arg_names = ", ".join(
                _ident(p.get("name"), "extern parameter name")
                for p in ext.get("params") or [])
            lines.extend(_emit_ts_ref_thunk(name, params, arg_names, returns,
                                            ext, refs["ts"]))
            continue
        if "ts" not in bodies:
            raise EmitError(
                f"extern `{name}` has no @ts body — not portable to this backend "
                f"(available: {', '.join(sorted(set(bodies) | set(refs))) or 'none'})"
            )
        # async extern (roadmap item 80, docs/design/async-extern.md §5): emit
        # an `async function` returning `Promise<T>`; the verbatim @ts body may
        # use `await`. Every admitted call site awaits it (see `_expr`). The
        # signature form mirrors the async service-op interface typing at
        # emit.py:2137/2222.
        # item 378 Stage 5: a config extern binds `_revl_config` as the first
        # body line, mirroring the py bind (backends/python/emit.py). None for a
        # no-config extern, so its body splices byte-identically.
        config_bind = _ts_extern_config_bind(ext)
        if ext.get("async"):
            lines.append(
                f"export async function {name}({params}): Promise<{returns}> {{")
            if config_bind:
                lines.append("  " + config_bind)
            body = textwrap.dedent(bodies["ts"].strip("\n"))
            if body:
                for line in body.splitlines() or [""]:
                    lines.append("  " + line)
            else:
                lines.append("  // (empty @ts body)")
            lines.append("}")
            lines.append("")
            continue
        lines.append(f"export function {name}({params}): {returns} {{")
        if config_bind:
            lines.append("  " + config_bind)
        body = textwrap.dedent(bodies["ts"].strip("\n"))
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
        if test.get("lifecycle"):
            # lifecycle tests are emitted by _emit_ts_lifecycle_tests — their
            # body is a script over a live composition, not pure statements
            continue
        lines.append(f"it({_string(test.get('name'))}, () => {{")
        if not test.get("body"):
            lines.append("  // (empty test body)")
        else:
            for stmt in test["body"]:
                _v3_stmt(stmt, ctx, lines, 2, test_mode=True)
        lines.append("})")
        lines.append("")
    return lines


def _emit_ts_lifecycle_tests(tests: list, types: dict, functions: list,
                             externs: list, services: dict,
                             components: list) -> list[str]:
    """`lifecycle test` blocks (syntax-2.0 §7.1) as async vitest cases driving
    a live cordis-ts context (FR-5).

    A lifecycle test is a script over a *live* composition: load components
    into a ``new Context()`` via the runtime's ``plug`` helper, call through
    provision keys (``ctx.<key>`` — the same committed-view access the emitted
    components use), unload them LIFO, and assert the runtime holds nothing.
    ``assert no_residue`` reuses the runtime's R4 introspection
    (``snapshotRuntime``/``assertNoResidue``, backends/typescript/runtime.ts),
    which mirrors the py reference tier's residue check including its R1
    live-host-resource accounting.
    """
    provided: dict[str, str] = {}
    for component in components:
        for key, service in (component.get("provides") or {}).items():
            provided[key] = service
    method_tables = {
        sname: (svc.get("methods") or {})
        for sname, svc in services.items()
    }
    ctx = _Ctx(types, functions, externs)
    lines: list[str] = []
    for test in tests:
        if not test.get("lifecycle"):
            continue
        where = f"lifecycle test {test['name']!r}"
        lines.append(f"it({_string(test.get('name'))}, async () => {{")
        lines.append("  // drives the composition on a real cordis context and")
        lines.append("  // proves no residue after LIFO teardown (FR-5 / §7.1).")
        lines.append("  const root = new Context()")
        if any(s.get("step") == "advance" for s in test.get("body") or []):
            # item 102: the clock coeffect is a module-global; reset it so this
            # test's `advance` steps start from t=0 and see only its own timers,
            # independent of any earlier lifecycle test in the file.
            lines.append("  host.clockReset()")
        lines.append("  const _revl_baseline = snapshotRuntime(root)")
        lines.append("  const _revl_fibers = new Map<string, any>()")
        for step in test.get("body") or []:
            kind = step.get("step")
            if kind == "load":
                component = step["component"]
                cfg = step.get("config") or {}
                cfg_items = ", ".join(
                    f"{_ident(field, 'config field')}: {_expr(value, ctx)}"
                    for field, value in cfg.items())
                lines.append("  {")
                lines.append(f"    const _f = plug(root, {component}, {{{cfg_items}}})")
                lines.append("    await _f")
                lines.append(f"    _revl_fibers.set({_string(component)}, await _f)")
                lines.append("    await _revl_settle()")
                lines.append("  }")
            elif kind == "unload":
                component = step["component"]
                lines.append(f"  await (await _revl_fibers.get({_string(component)})).dispose()")
                lines.append("  _revl_fibers.delete(" + _string(component) + ")")
                lines.append("  await _revl_settle()")
            elif kind == "call":
                key = step["key"]
                service = provided.get(key)
                if service is None:  # pragma: no cover — the lowerer rejects it
                    raise EmitError(f"{where}: no provider for key {key!r}")
                method = (method_tables.get(service) or {}).get(step["method"])
                if method is None:  # pragma: no cover — the lowerer rejects it
                    raise EmitError(f"{where}: unknown method {step['method']!r}")
                args = ", ".join(_expr(arg, ctx) for arg in step.get("args") or [])
                await_ = "await " if method.get("async") else ""
                call = f"root.{_ident(key, 'provision key')}.{_ident(step['method'], 'method')}({args})"
                bind = step.get("bind")
                if bind is not None:
                    lines.append(f"  const {_ident(bind, 'lifecycle binding')} = {await_}{call}")
                else:
                    lines.append(f"  {await_}{call}")
                lines.append("  await _revl_settle()")
            elif kind == "assert":
                # reuse the pure-test assert rendering (equality goes through
                # revlEq + vitest's matcher, with both sides in the message)
                _v3_stmt({"step": "assert", "expr": step["expr"]}, ctx, lines,
                         2, test_mode=True)
            elif kind == "advance":
                # item 102: drive the clock coeffect forward. A firing is a
                # deterministic timeline step, so `_revl_settle` after it lets
                # the fired body's async work (if any) settle before the next
                # statement observes it (docs/time-coeffect.md §advance).
                lines.append(f"  host.clockAdvance({int(step['ms'])})")
                lines.append("  await _revl_settle()")
            elif kind == "assert_no_residue":
                lines.append("  // R4 + R1: same introspection the py reference")
                lines.append("  // tier's `assert no_residue` performs.")
                lines.append("  assertNoResidue(root, _revl_baseline)")
            else:  # pragma: no cover — the lowerer emits nothing else
                raise EmitError(f"{where}: unknown lifecycle step {kind!r}")
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


def _uses_routes(ir: dict) -> bool:
    """True iff any component carries a routed require (item 162's `routes` IR),
    so the module imports `realmLabel` and emits the router helper. A document
    with no routed require stays byte-identical to the pre-feature output."""
    return any(c.get("routes") for c in ir.get("components") or [])


def _uses_lifecycle_tests(ir: dict) -> bool:
    """True iff the document carries any `lifecycle test` block (§7.1), so the
    module imports the runtime's plug + residue-introspection helpers and the
    value `Context` import those drivers need."""
    return any(t.get("lifecycle") for t in (ir.get("tests") or []))


def _uses_frame(ir: dict, doc_ctx: "_Ctx") -> bool:
    """True iff some component registers a transactional (witnessed) or
    compensation entry (item 243 Slice 2b), reusing `_needs_frame` per
    component — the document-level check the import line needs, ahead of any
    per-component rendering. A document with neither feature imports no
    `record`; `Frame` itself is imported whenever any component brackets at
    all (`_uses_bracket_frame`), because a bracket's Phase-1 guard records
    into it."""
    return any(_needs_frame(c, doc_ctx) for c in ir.get("components") or [])


def _uses_bracket_frame(ir: dict) -> bool:
    """True iff some component yields a bracket disposer, so the module needs
    the `Frame` import for `Frame.bracket`'s Phase-1 guard (`_has_bracket`)."""
    return any(_has_bracket(c) for c in ir.get("components") or [])


def _runtime_imports(ir: dict, runtime_import: str, doc_ctx: "_Ctx") -> str:
    """The `import { ... } from '<runtime>'` line. `spawn` is added only when a
    spawn node is present (docs/design-v2-instances.md phase 2); the lifecycle
    drivers add `plug` + the no-residue introspection (FR-5, §7.1)."""
    names = ["host"]
    if _uses_frame(ir, doc_ctx):
        # item 243 Slice 2b: a document with a transactional (witnessed) or
        # compensation entry imports `Frame`, the activation's teardown
        # accumulator (docs/design/teardown-contract.md), and `record` so a
        # witnessed/compensating extern's own `@ts` body can participate in
        # the same shared observability trace every host builtin uses. A
        # document using neither feature imports neither name — byte-identical
        # to before this slice.
        names += ["Frame", "record"]
    elif _uses_bracket_frame(ir):
        # a bracket-only document imports `Frame` alone: its brackets record a
        # Phase-1 `bracket-fault` into one (see `_bracket_yield`), but nothing
        # here reaches a witnessed/compensating extern's `@ts` body, so
        # `record` stays out.
        names.append("Frame")
    if _uses_routes(ir):
        # item 167: the router resolves its worker realms by label.
        names.append("realmLabel")
    if _uses_spawn(ir):
        names.append("spawn")
    if _uses_lifecycle_tests(ir):
        names += ["plug", "snapshotRuntime", "assertNoResidue"]
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
                   ir.get("externs") or [], services=services)

    out: list[str] = [
        "// Generated by revl backends/typescript/emit.py — do not edit.",
        "// Target runtime: cordis v4 (https://github.com/cordiverse/cordis).",
        "import type { Context } from 'cordis'",
        _runtime_imports(ir, runtime_import, doc_ctx),
        "",
    ]

    # A v1/v2 component body carries ordinary expressions too (`if (config.limit
    # < 1)`, Int arithmetic in a method body), so it needs the same helpers a
    # 2.0 document does — without this a v1 module referencing `revlI64` or
    # `revlEq` would emit a call to a function that is not there.
    out.extend(_revl_helpers(ir))

    # item 167: the emitted realization of a routed require (item 162's `routes`
    # IR), mirroring src/revl/run.py::_Router. Emitted only for a routed-require
    # program, so a document with no routed require is byte-identical.
    if _uses_routes(ir):
        out.append(_TS_ROUTER_SRC)
        out.append("")

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
    doc_ctx = _Ctx(types, functions, externs, services=services)

    out: list[str] = [
        "// Generated by revl backends/typescript/emit.py — do not edit.",
        "// Target runtime: cordis v4 (https://github.com/cordiverse/cordis).",
        # lifecycle drivers construct a live Context, so they need the value,
        # not just the type; pure documents keep the type-only import.
        ("import { Context } from 'cordis'"
         if _uses_lifecycle_tests(ir) else "import type { Context } from 'cordis'"),
        _runtime_imports(ir, runtime_import, doc_ctx),
    ]
    if tests:
        out.append("import { expect, it } from 'vitest'")
    # item 396 option B: a `@ts ref` extern emits a lazy thunk that resolves the
    # ref'd module at CALL time through a root the runner provides (so the
    # artifact text stays machine-independent) and imports the symbol then —
    # never a module-top static import (which would run host code at module
    # evaluation, the load-time execution point B forbids, and could not resolve
    # from the _gen placement anyway). Emitted only when some extern carries a
    # ts ref, so a ref-free module is byte-identical.
    if any(ext.get("refs", {}).get("ts") for ext in externs):
        out.append("import { createRequire as _revl_createRequire } from 'node:module'")
        out.append("import { pathToFileURL as _revl_pathToFileURL } from 'node:url'")
        out.append("import * as _revl_pathmod from 'node:path'")
    out.append("")

    if any(ext.get("refs", {}).get("ts") for ext in externs):
        out.extend(_emit_ts_ref_runtime())

    out.extend(_revl_helpers(ir))
    if _uses_lifecycle_tests(ir):
        out.append("const _revl_settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0))")
        out.append("")

    if types:
        out.extend(_emit_ts_types(types))
    if externs:
        out.extend(_emit_ts_externs(externs))
    if functions:
        out.extend(_emit_ts_functions(functions, types, externs))
    if tests:
        pure = [t for t in tests if not t.get("lifecycle")]
        lifecycle = [t for t in tests if t.get("lifecycle")]
        if pure:
            out.extend(_emit_ts_tests(pure, types, functions, externs))
        if lifecycle:
            out.extend(_emit_ts_lifecycle_tests(
                lifecycle, types, functions, externs,
                ir.get("services") or {}, components))

    # Service interfaces (coeffect interfaces, DESIGN.md §3.1). This document
    # declares its record/ADT types (emitted above by _emit_ts_types), so a
    # record referenced only here — never in a component body — still renders by
    # name and resolves to its emitted interface.
    known_types = frozenset(types)
    for sname, service in services.items():
        _ident(sname, "service")
        out.append(f"export interface {sname} {{")
        for mname, method in (service.get("methods") or {}).items():
            _ident(mname, "method")
            params = ", ".join(
                f"{_ident(p.get('name'), 'parameter')}: {_ts_type(p.get('type'), known_types)}"
                for p in method.get("params") or []
            )
            returns = (_ts_type(method["returns"], known_types)
                       if method.get("returns") else "void")
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


def _refuse_deferred_emissions(ir: dict) -> None:
    """Roadmap 245 Decision 2 tier gate: a CALL to a `deferred` emission needs a
    session-owner runtime (the deferral queue and the commit verb) this tier does
    not have yet, so refuse it at emit time — surfaced through EmitError, this
    tier's existing refusal channel. The reachability check and the single
    canonical wording live in `revl.session_commit`, shared by all five ownerless
    tiers so six backends do not invent six messages; a declared-but-never-called
    deferred extern emits cleanly (call-site keyed)."""
    try:
        from revl.errors import RevlError
        from revl.session_commit import (
            refuse_approval_on_ownerless_tier,
            refuse_deferred_on_ownerless_tier,
        )
    except ModuleNotFoundError:  # standalone `python3 emit.py` — put src/ on the path
        import pathlib
        import sys as _sys
        src = pathlib.Path(__file__).resolve().parents[2] / "src"
        if src.is_dir() and str(src) not in _sys.path:
            _sys.path.insert(0, str(src))
        from revl.errors import RevlError
        from revl.session_commit import (
            refuse_approval_on_ownerless_tier,
            refuse_deferred_on_ownerless_tier,
        )
    try:
        refuse_deferred_on_ownerless_tier(ir, "typescript")
        refuse_approval_on_ownerless_tier(ir, "typescript")
    except RevlError as exc:
        raise EmitError(exc.message) from None


def _emit_temporal(ir: dict) -> str:
    """Dispatch to the Temporal emission target (roadmap item 253, §4).

    `--target temporal` is a rendering MODE of this emitter, not a new tier. The
    Temporal sink lives in the sibling `emit_temporal.py` and reuses this
    module's machinery, so this module is registered as `sys.modules["emit"]`
    and its directory put on the path before the import, whatever name the
    caller loaded emit.py under (`revl_bundle_typescript_emit`, a per-test name,
    or `__main__`)."""
    import importlib
    import os
    import sys as _sys
    import types

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in _sys.path:
        _sys.path.insert(0, here)
    # Point the canonical name `emit` at THIS module's namespace so the sink's
    # `from emit import ...` shares one `EmitError` class and one renderer set.
    # emit.py is loaded under several names (`revl_bundle_typescript_emit` for
    # the bundle, a per-test name, `__main__` for the standalone CLI) and is not
    # always registered in sys.modules under its own `__name__`, so bind a proxy
    # module whose namespace is this one — unless a real `emit` module already
    # carries this exact `EmitError` (the standalone/test load), which we keep.
    existing = _sys.modules.get("emit")
    if existing is None or getattr(existing, "EmitError", None) is not EmitError:
        proxy = types.ModuleType("emit")
        proxy.__dict__.update(globals())
        _sys.modules["emit"] = proxy
    emit_temporal = importlib.import_module("emit_temporal")
    return emit_temporal.emit_temporal(ir)


def emit(ir: dict, *, runtime_import: str = "../runtime.ts",
         target: str = "cordis") -> str:
    """Emit one TypeScript module for an IR document (docs/backend-ir.md).

    `target` selects the RENDERING of this emitter (roadmap item 253 §4), a
    dimension orthogonal to `--backend`: `"cordis"` (the default) is this
    backend's native cordis-v4 runtime, byte-identical to before item 253;
    `"temporal"` renders the same IR walk against the Temporal TS SDK. A target
    is NOT a new tier — nothing new to boot or place."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be an object")
    if target == "temporal":
        # The Temporal target owns its own closed-allowlist refusal (a derived
        # allowlist, not the cordis tier's open blocklist), so it does not run
        # the cordis refusals below; a hole/fault-test still can't reach it
        # because the frontend never admits one into a mappable component.
        return _emit_temporal(ir)
    if target != "cordis":
        raise EmitError(
            f"unknown emit target {target!r}: this backend renders `cordis` "
            f"(default) or `temporal` (roadmap item 253)")
    _refuse_holes(ir)
    _refuse_deferred_emissions(ir)

    _refuse_fault_tests(ir)

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

"""revl -> cordis-go backend.

Emits idiomatic Go targeting **github.com/0xdenny218/stc-go** (pinned in
backends/go/scenarios/go.mod) — a Go implementation of the same
spatiotemporal-composability paradigm revl compiles for. `emit(ir) -> str`
produces one Go source file (package `emitted`).

Backend contract (DESIGN.md §7), mapped to stc-go:

  | revl                    | Go (stc-go)                                            |
  |-------------------------|--------------------------------------------------------|
  | service                 | `type <Name> interface { <M>(…) … }`                   |
  | component               | `func <Name>(cfg) stc.Component` (Apply closure)       |
  | requires k: S           | `Inject: []stc.Key{_key_k}` + `stc.Service[S](ctx, …)` |
  | provides k: S           | `impl <Comp>_<k>` + `ctx.Provide(_key_k, S(impl))`     |
  | effect E undo U         | `ctx.Effect(func() stc.Inverse { E; return func()…U })`|
  | isolate k in realm("R") | load-site `ctx.Isolate(_key_k, _revlRealm("R"))`       |
  | emit                    | plain method call                                      |
  | format                  | `fmt.Sprintf(…)`                                        |

Realm placement is applied to the load-target context (the emitted
`Load<Name>` helper), NOT inside the Apply body — isolating inside Apply runs
after stc-go's Inject gate has already evaluated on the un-isolated context,
which strands a realm-scoped consumer in Pending forever. This mirrors the
same fix carried in the Rust backend (`_revl_realm`).

Scope: ir_version 1 and 2 components (effect/inverse, config+defaults,
provide/inject, provide-method bodies, isolate, intercept). v3 pure
functions and spawn/instance-parametric IR are out of scope.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Optional


class EmitError(ValueError):
    pass


# Dispatcher conformance (roadmap item 76a). This file carries TWO expression
# dispatchers — `_expr` (component/method bodies, the stc-go live world) and
# `_go_v3_expr` (pure fn bodies) — and the sets below declare, as data, the IR
# expression kinds each one must render, plus the kinds each one deliberately
# refuses with a named tier-limit EmitError (never the "unsupported expr
# kind" fall-through). tests/test_expr_dispatcher_conformance.py checks them
# against the frontend schema (src/revl/lower.py: EXPR_KINDS /
# EXPR_KINDS_FN / EXPR_KINDS_COMPONENT).
#
# Component-position refusals are real v1/v2 limits of the stc-go world: an
# anonymous record literal has no declared record type to render (declaring
# one routes the document to the typed-core path, which does not carry a live
# component), match/arrow/`?.` have no lowering there yet, and bare Opt/Result
# construction outside return position is refused by the tier's tuple-Opt
# design. Each refusal names the limit and a workaround. `hole` is refused at
# the document level by the pre-emit walk.
EXPR_DISPATCHERS: dict[str, frozenset[str]] = {
    "component": frozenset({
        "bin", "builtin", "call", "config", "field", "fn", "format",
        "host", "if", "index", "instance-get", "list", "lit", "maplit",
        "name", "req", "spawn", "un", "var",
    }),
    "fn": frozenset({
        "adt", "arrow", "bin", "builtin", "call", "field", "if", "index",
        "interp", "len", "list", "lit", "maplit", "match", "name", "optcall",
        "optfield", "record", "un", "var",
    }),
}
EXPR_REFUSED: dict[str, frozenset[str]] = {
    # kinds the component dispatcher deliberately refuses (named tier limits)
    "component": frozenset({
        "adt",        # bare Opt/Result construction outside return position
        "arrow",      # arrow values have no lowering in the stc-go world yet
        "match",      # no match lowering in the stc-go world yet
        "optcall",    # `?.` has no lowering in the stc-go world yet
        "optfield",   # `?.` has no lowering in the stc-go world yet
        "record",     # needs a declared record type the v1/v2 tier carries none of
        "record_update",
    }),
    # kinds the fn dispatcher deliberately refuses
    "fn": frozenset({
        "record_update",  # docs/records.md §6 — "lift it into a helper fn instead"
    }),
}
# kinds refused at the document level on every position
EXPR_REFUSED_DOCUMENT: frozenset[str] = frozenset({"hole"})


# --------------------------------------------------------------------------
# identifiers / types
# --------------------------------------------------------------------------

def _camel(name: str) -> str:
    """snake_or_lower -> UpperCamel (exported Go identifier)."""
    parts = str(name).replace("-", "_").split("_")
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _lower_camel(name: str) -> str:
    c = _camel(name)
    return c[:1].lower() + c[1:] if c else c


def _key_var(name: str) -> str:
    return "_key" + _camel(name)


def _realm_helper_name() -> str:
    return "_revlRealm"


_PRIM = {
    "Str": "string",
    "Int": "int",
    "Int32": "int32",
    "Float": "float64",
    "Bool": "bool",
    "Unit": "",
}

# When rendering an ir_version-3 document that contains a component, the
# integer width converges with the pure v3 tier (int64) so the shared stdlib
# preamble (revlListLen &c., which return int64) type-checks against emitted
# method bodies. ir_version 1/2 keep `int`, byte-for-byte with the frozen
# scenarios. Set per emit() call; never read outside a single call.
_V3_MODE = False


def _go_widen_int(expr: str) -> str:
    """A revl `Int` expression as a Go int64, for strconv.FormatInt (item 434 (f)).

    The pure v3 tier already lowers `Int` to int64; the ir_version 1/2
    component tier lowers it to `int`, which FormatInt does not accept.
    """
    return expr if _V3_MODE else "int64(%s)" % expr

# The declared record/variant types of the current document (ir["types"]),
# for the component/method renderer: record literals need the struct name for
# their field set, and ADT construction/match need the case -> (adt, payload)
# layout. Only meaningful under _V3_MODE; empty for v1/v2.
_V3_TYPES: dict = {}

# The document's v3 typed-core types (records/ADTs) are materialized as Go
# structs/sealed interfaces in THIS package, so a component/provide-method body
# lowers record literals, field access, ADT construction and `match` against
# them. True on two paths:
#   * emit_placement's typed-core composition (pure tier + live components +
#     interop bridge, one module), and
#   * emit()'s live stc-go path when the document declares types alongside a
#     live component (a v3 provide method taking/returning a record — item 139).
# Record struct fields are emitted EXPORTED + json-tagged on EVERY v3 path now
# (item 390) — see `_v3_field_ident` / `_emit_v3_go_types`. This flag no longer
# gates the field export/tag (that was the `{}` json_stringify defect: the pure
# typed tier left fields unexported, so `encoding/json` dropped them). It still
# gates the component/method-body lowerings below (record literals, field
# access, ADT construction/match resolve against `_V3_TYPES` only in placement /
# live-typed mode). v1/v2 documents leave it False.
_V3_TYPED_COMPONENTS = False


def _go_type(t) -> str:
    """Map a revl type name to a Go type (value position)."""
    if t is None:
        return ""
    t = str(t).strip()
    if t in _PRIM:
        if t == "Int" and _V3_MODE:
            return "int64"
        return _PRIM[t]
    if t.startswith("List[") and t.endswith("]"):
        return "[]" + _go_type(t[5:-1])
    if t.startswith("Opt[") and t.endswith("]"):
        # Value/parameter position: Opt lowers to a pointer `*T` (nil == None),
        # which carries the presence bit a bare `T` cannot. The (T, bool) tuple
        # form is return-position only (see _go_return); no scenario uses an
        # Opt value/param, so this pointer form is corpus-only.
        return "*" + _go_type(t[4:-1])
    if t.startswith("Map[") and t.endswith("]"):
        k, v = _v3_split_generic(t[4:-1])
        return "map[%s]%s" % (_go_type(k), _go_type(v))
    if t == "Row":
        return "Row"
    # v3 typed-core: a declared user type (record/variant) renders with the
    # name `_emit_v3_go_types` gave it (_v3_ident, not _camel — snake_case
    # type names must agree between the declaration and every use site).
    if _V3_MODE and t in _V3_TYPES:
        return _v3_ident(t, "type name")
    # Unknown / user type: pass through as an exported identifier.
    return _camel(t)


def _go_return(t):
    """Return-position lowering. Opt[T] -> '(T, bool)'; Unit -> ''."""
    if t is None:
        return ""
    t = str(t).strip()
    if t.startswith("Opt[") and t.endswith("]"):
        return "(%s, bool)" % _go_type(t[4:-1])
    if t.startswith("Result[") and t.endswith("]"):
        # Result[T, E] -> (T, E, bool): the bool is the ok-flag. Ok(v) spreads
        # to `v, zeroE, true`; Err(e) to `zeroT, e, false`. No scenario returns
        # Result, so this branch is corpus-only (byte-identical preserved).
        ok, err = _v3_split_generic(t[7:-1])
        return "(%s, %s, bool)" % (_go_type(ok), _go_type(err))
    return _go_type(t)


def _go_zero(surface) -> str:
    """A Go zero value for a surface type — used to pad Opt/Result spreads."""
    gt = _go_type(surface)
    if gt in ("int", "int32", "int64", "float64"):
        return "0"
    if gt == "string":
        return '""'
    if gt == "bool":
        return "false"
    if gt.startswith("[]") or gt.startswith("map["):
        return "nil"
    return gt + "{}"


# --------------------------------------------------------------------------
# expression rendering
# --------------------------------------------------------------------------

# The struct receiver of a provide-impl method. `revl`-prefixed so a
# provide-method parameter can never collide with it — the same reserved-
# namespace convention the lifecycle-test receiver (`revlT`) uses so a user
# binding named `t` cannot shadow *testing.T. Before this the receiver was the
# bare `s`, so a provide method with a param named `s` (e.g. `fn area(s: Shape)
# -> Int`) emitted `func (s *C_k) Area(s Shape)` — Go rejects "s redeclared in
# this block" (roadmap item 147). Held as one constant so both `_Env` (which
# renders `<recv>.field` / `<recv>.cfg` / `<recv>.ctx`) and the method-signature
# emitter agree.
_METHOD_RECEIVER = "revlSelf"


class _Env:
    """Renders name references. `receiver` is '' at Apply top-level (bare
    locals) or the provide-impl method receiver (`_METHOD_RECEIVER`) inside a
    provide method, where names resolve to struct fields `<receiver>.<field>`."""

    def __init__(self, binds, reqs, config_fields, params=None, receiver=""):
        self.binds = set(binds)
        self.reqs = set(reqs)
        self.config_fields = set(config_fields)
        self.params = set(params or [])
        self.receiver = receiver
        # surface types of locals/params, for stdlib-method dispatch and the
        # `??` / ternary result-type inference.
        self.var_types: dict[str, str | None] = {}
        # item 113: the Go value type to instantiate the generic host Map with
        # at a `Map.new()` acquisition (`MapNew[int64]()`); set transiently by
        # the let-effect emitter, None everywhere else.
        self.map_new_value: str | None = None

    def _prefix(self) -> str:
        return (self.receiver + ".") if self.receiver else ""

    def name_ref(self, ident: str) -> str:
        if ident in self.params:
            return _safe_local(ident)
        if ident in self.binds:
            return self._prefix() + _bind_field(ident)
        if ident in self.reqs:
            return self._prefix() + _req_field(ident)
        # Fall back to a bare local (e.g. a let bind not tracked).
        return _safe_local(ident)

    def config_ref(self, field: str) -> str:
        base = (self.receiver + ".cfg") if self.receiver else "cfg"
        return "%s.%s" % (base, _camel(field))

    def req_ref(self, name: str) -> str:
        return self._prefix() + _req_field(name)

    def ctx_ref(self) -> str:
        return (self.receiver + ".ctx") if self.receiver else "ctx"


_GO_KEYWORDS = {"type", "range", "func", "map", "chan", "select", "go",
                "defer", "return", "var", "const", "package", "import"}


def _safe_local(name: str) -> str:
    """Rename a revl identifier that collides with a *Go* keyword so it emits
    as a usable Go local (roadmap item 165).

    The rename must be a pure function of the name (declaration site and use
    sites agree without a table) AND INJECTIVE: two distinct revl identifiers
    may never land on one Go identifier. The naive "`n + '_'` if reserved" map
    is pure but not injective — it sends `func` to `func_` and leaves the
    equally legal revl identifier `func_` alone, so both reach `func_`. Here
    that breaks loudly (`go build` reports "no new variables on left side of
    :="), but the python tier silently CAPTURES on the same shape, so the rule
    is fixed identically on every tier rather than left to a downstream
    compiler CI does not run.

    The injective rule: escape a name iff the name OR any name reachable from
    it by dropping trailing `_` is a Go keyword, and escape it by exactly ONE
    `_`. Names whose underscore-stripped root is a keyword shift up one rung of
    the `kw`/`kw_`/`kw__` ladder (`func` -> `func_`, `func_` -> `func__`),
    which is injective; every other name is returned unchanged and can never
    equal a shifted name, because a shifted name's root is a keyword and an
    unchanged name's root is not. The output is never itself a keyword: no
    member of `_GO_KEYWORDS` ends in `_`. Only a name whose root is a keyword
    can change, so no existing program's output moves."""
    n = str(name)
    root = n
    while root:
        if root in _GO_KEYWORDS:
            return n + "_"
        if not root.endswith("_"):
            break
        root = root[:-1]
    return n


def _bind_field(name: str) -> str:
    return _lower_camel(name)


def _req_field(name: str) -> str:
    return _lower_camel(name)


def _expr(node, env: _Env, expected=None) -> str:
    """Render one expression in a component/method body.

    Post-D1 convergence: this is the single expression renderer for the stc-go
    tier. It resolves names through `env` (params / struct fields / config /
    reqs) and handles the full surface expression set — bin/un/if/list/index/
    stdlib-builtin/Opt-Result construction — mirroring the pure v3 renderer
    (`_go_v3_expr`) but in the environment-aware, tuple-Opt world of a live
    component. `expected` is the surface type the value flows into (a method's
    declared return type, a let's inferred type), used for `??`/ternary result
    typing.
    """
    if not isinstance(node, dict):
        raise EmitError("expr must be an object: %r" % (node,))
    # An implicit Int -> Float coercion site (docs/arithmetic.md): go's
    # untyped-constant rule used to absorb it silently; the marker makes the
    # conversion explicit in the emitted source.
    if node.get("widen") == "Float":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return "float64(%s)" % (_expr(inner, env, expected),)
    kind = node.get("kind")
    if kind == "name":
        return env.name_ref(node["id"])
    if kind == "var":  # pure-tier name shape, seen inside v3 documents
        return env.name_ref(node.get("name") or node.get("id"))
    if kind == "config":
        return env.config_ref(node["field"])
    if kind == "req":
        return env.req_ref(node["name"])
    if kind == "host":
        fn = node["fn"]  # e.g. "Pool.open" -> PoolOpen
        recv, _, meth = fn.partition(".")
        go = _camel(recv) + _camel(meth)
        args = ", ".join(_expr(a, env) for a in node.get("args", []))
        # item 113: the host Map is generic; Go cannot infer `V` from the
        # argument-less constructor, so pin it explicitly (`MapNew[int64]()`).
        # The value type is supplied by the enclosing let-effect; a Map.new with
        # no learned type falls back to `string` (the historical surface).
        if fn == "Map.new":
            return "%s[%s](%s)" % (go, getattr(env, "map_new_value", None)
                                   or "string", args)
        if recv == "Stream":
            # item 130: `Stream.source()` opens a stream provider; the host
            # runtime carrying it is emitted only when a document reaches a
            # stream (see `_COMP_NEEDS_STREAM`).
            _flag_stream()
        return "%s(%s)" % (go, args)
    if kind == "subscribe":
        # item 130 Slice 3 (design §4.6, the go row): this tier ERASES the async
        # color — `next` is a blocking two-case `select` on the item channel and
        # the CANCEL channel, and `close` closes the cancel channel. So the
        # bracket inverse is reachable off the teardown goroutine even while a
        # `next` is parked: teardown never has to wait for the provider.
        _flag_stream()
        _refuse_unlowered_stream_surface(node, "cordis-go")
        stream = _stream_head(node.get("stream") or {}, env)
        policy = node.get("policy") or "error"
        capacity = int(node.get("buffer") or 0)
        return "StreamSubscribe(%s, %s, %d)" % (stream, _go_string(policy),
                                                capacity)
    if kind == "call":
        # A built-in Opt/Result constructor arriving as call(callee=Some, ...).
        callee = node.get("callee")
        if callee is not None:
            # A method call on an expression: `<recv>.<method>(args)`. The 2.0
            # dialect spells this `callee = field(target=<recv>, name=<method>)`
            # — the shape the instance accessor `s.<key>.method(..)` produces
            # (the `field`'s target is the `instance-get`). The component
            # dialect's own method call arrives via `target`/`method` below, so
            # `field` here is unambiguously a method selector, not struct-field
            # access (records are unsupported on this tier).
            if callee.get("kind") == "field":
                recv = _expr(callee.get("target"), env)
                meth = _camel(callee.get("name"))
                args = ", ".join(_expr(a, env) for a in node.get("args", []))
                return "%s.%s(%s)" % (recv, meth, args)
            nm = callee.get("name") or callee.get("id")
            if nm in ("Some", "None", "Ok", "Err"):
                raise EmitError(
                    "Opt/Result construction is only supported in return "
                    "position on the cordis-go tier (got a bare value)"
                )
            src = _expr(callee, env)
            args = ", ".join(_expr(a, env) for a in node.get("args", []))
            return "%s(%s)" % (src, args)
        target = _expr(node["target"], env)
        meth = _camel(node["method"])
        args = ", ".join(_expr(a, env) for a in node.get("args", []))
        return "%s.%s(%s)" % (target, meth, args)
    if kind == "format":
        arg_nodes = node.get("args", [])
        return _format(node["template"],
                       [_expr(a, env) for a in arg_nodes],
                       [_comp_infer(a, env) for a in arg_nodes])
    if kind == "str":
        return _go_string(node.get("value", ""))
    if kind == "int":
        return str(int(node.get("value", 0)))
    if kind == "bool":
        return "true" if node.get("value") else "false"
    if kind == "lit":
        return _go_literal(node.get("value"))
    if kind == "bin":
        op = node.get("op")
        if op == "??":
            # Opt[T] ?? T -> unwrap the (value, ok) tuple inline. The Opt is a
            # multi-value expression (a service/host call), so bind it first.
            gt = (_go_type(expected) if expected
                  else _go_type(_comp_infer(node.get("right"), env)) or "any")
            left = _expr(node["left"], env)
            right = _expr(node["right"], env, _comp_infer(node.get("right"), env))
            return ("func() %s { _v, _ok := %s; if _ok { return _v }; "
                    "return %s }()" % (gt, left, right))
        if op in ("<<", ">>"):
            # Int32 shift: mask the count to 0..31 unsigned (Go neither masks
            # the count nor accepts a negative signed one), matching the v3
            # path and wasm/JS (docs/arithmetic.md, item 366).
            return "(%s %s (uint32(%s) & 31))" % (
                _expr(node["left"], env), op, _expr(node["right"], env))
        go_op = _V3_GO_BIN_OPS.get(op)
        if go_op is None:
            raise EmitError("unsupported binary operator: %r" % (op,))
        return "(%s %s %s)" % (_expr(node["left"], env), go_op,
                               _expr(node["right"], env))
    if kind == "un":
        operand = _expr(node.get("operand"), env)
        if node.get("op") == "!":
            return "(!%s)" % operand
        if node.get("op") == "~":
            # Int32 bitwise complement (item 366): Go's unary `^`.
            return "(^%s)" % operand
        if node.get("op") == "-":
            if node.get("operands") == "Int":
                # `-x` is `0 - x`; negating Int.MIN overflows, so it traps
                # through the same checked subtraction as any Int `-`
                # (docs/arithmetic.md; wasm lowers `-x` the same way).
                env.needs_overflow = True
                return "revlSub(0, %s)" % operand
            return "(-%s)" % operand
        raise EmitError("unsupported unary operator: %r" % (node.get("op"),))
    if kind == "if":  # ternary
        gt = (_go_type(expected) if expected
              else _go_type(_comp_infer(node.get("then"), env))
              or _go_type(_comp_infer(node.get("else"), env)) or "any")
        cond = _expr(node.get("cond"), env)
        then = _expr(node.get("then"), env, expected)
        els = _expr(node.get("else"), env, expected)
        return ("func() %s { if %s { return %s }; return %s }()"
                % (gt, cond, then, els))
    if kind == "list":
        elem = None
        if isinstance(expected, str) and expected.startswith("List[") and expected.endswith("]"):
            elem = expected[5:-1]
        items = node.get("items") or []
        if elem is None and items:
            elem = _comp_infer(items[0], env)
        go_elem = _go_type(elem) if elem else "any"
        rendered = ", ".join(_expr(it, env) for it in items)
        return "[]%s{%s}" % (go_elem, rendered)
    if kind == "maplit":
        # `Map.empty()` (docs/stdlib-2.0.md §Map): same positional-inference
        # limit as the v3 tier — refuse rather than mis-emit. The pin is the
        # expected Map type: a typed fn return/parameter, or the annotated
        # `let/var x: Map[K, V] = Map.empty()` the frontend threads onto the
        # node as `expected` (roadmap 76b) — the author's own annotation.
        if expected is None:
            expected = node.get("expected")
        if isinstance(expected, str) and expected.startswith("Map[") and expected.endswith("]"):
            k, v = _v3_split_generic(expected[4:-1])
            return "map[%s]%s{}" % (_go_type(k), _go_type(v))
        raise EmitError(
            "an untyped empty Map needs an expected Map type on this tier "
            "(Go infers literals positionally, not from later use) - pin it "
            "via a typed fn return, or an annotated `let`/`var` "
            "declaration (the positions this tier actually reads)")
    if kind == "index":
        return "%s[%s]" % (_expr(node.get("target"), env),
                           _expr(node.get("index"), env))
    if kind == "builtin":
        target_node = node.get("target")
        target = _expr(target_node, env)
        args = [_expr(a, env) for a in node.get("args") or []]
        return _comp_builtin(node.get("method"),
                             _comp_infer(target_node, env), target, args)
    if kind == "instance-get":
        return _instance_get_expr(node, env)
    if kind == "spawn":
        return _spawn_expr(node, env)
    if kind == "field":
        # `.length` on a sized value — the one field the frontend produces on
        # a non-record in component positions (a pure fn body spells the same
        # access as the `len` node). Records are a v3 typed-core surface: with
        # the document's declared types in scope (placement mode) a field
        # access on a record lowers to the struct field; otherwise it is a
        # named tier limit rather than a silent fall-through.
        if node.get("name") == "length":
            global _COMP_NEEDS_STDLIB
            _COMP_NEEDS_STDLIB = True
            target_node = node.get("target")
            target = _expr(target_node, env)
            rt = _comp_infer(target_node, env)
            return ("revlStrLen(%s)" if rt == "Str" else "revlListLen(%s)") % target
        if _V3_MODE and _V3_TYPED_COMPONENTS:
            target_node = node.get("target")
            tt = _comp_infer(target_node, env)
            if isinstance(tt, str) and tt in _V3_TYPES \
                    and _V3_TYPES[tt].get("kind") == "record":
                target = _expr(target_node, env)
                if target_node.get("kind") not in ("name", "var", "call", "host",
                                                    "instance-get", "index", "field"):
                    target = "(%s)" % target
                return "%s.%s" % (target, _v3_field_ident(node.get("name")))
        raise EmitError(
            "field access is only lowerable on a sized value's `.length` "
            "in the stc-go component world (records need a declared record "
            "type, and declaring one routes the document to the typed-core "
            "path, which carries no live component) - lift it into a "
            "helper fn instead")
    if kind == "fn":
        # a call to a top-level `fn` by name (component dialect). In the v3
        # typed-core world a document declaring a pure `fn` AND a component
        # routes to the placement path, where the fn is a real declaration the
        # method body can call; otherwise unreachable in practice — a named
        # tier limit beats a fall-through.
        name = _v3_ident(node.get("name"), "function")
        args = ", ".join(_expr(a, env) for a in node.get("args") or [])
        return "%s(%s)" % (name, args)
    if kind == "record":
        # v3 typed-core only: a record literal needs the document's declared
        # record type for its field set (the v1/v2 tier carries none).
        if not (_V3_MODE and _V3_TYPED_COMPONENTS and _V3_TYPES):
            raise EmitError(
                "record is not lowerable in the stc-go component world "
                "(ir_version 1/2 documents carry no record/ADT types, and this "
                "tier has no record lowering in component bodies) - "
                "lift it into a helper fn instead")
        fields = node.get("fields") or []
        tname = _v3_record_type_for_fields([k for k, _ in fields])
        body = ", ".join(
            "%s: %s" % (_v3_field_ident(k), _expr(v, env)) for k, v in fields)
        return "%s{%s}" % (tname, body)
    if kind == "adt":
        case = node.get("case")
        if _V3_MODE and _V3_TYPED_COMPONENTS and case in _v3_case_layout():
            return _v3_comp_construct(node, env)
        raise EmitError(
            "Opt/Result construction is only supported in return position on "
            "the cordis-go tier (got a bare value)")
    if kind == "match":
        if _V3_MODE:
            # v3 method bodies: user ADTs lower to a type switch (needs the
            # declared types, only present in placement mode); Opt/Result lower
            # against the tuple convention and work in any v3 component.
            return _go_comp_match(node, env, expected)
        raise EmitError(
            "match is not lowerable in the stc-go component world yet "
            "(ir_version 1/2 documents carry no record/ADT types, and this "
            "tier has no match lowering in component bodies) - "
            "lift it into a helper fn instead")
    if kind in ("arrow", "optfield", "optcall"):
        raise EmitError(
            f"{kind} is not lowerable in the stc-go component world yet "
            f"(ir_version 1/2 documents carry no record/ADT types, and this "
            f"tier has no match/arrow/`?.` lowering in component bodies) - "
            f"lift it into a helper fn instead")
    if kind == "record_update":
        raise EmitError(
            "functional record update `{r | f = e}` is not emitted by the go "
            "backend yet (implemented tiers: python, typescript) - see "
            "docs/records.md §6; lift it into a helper fn instead")
    raise EmitError("unsupported expr kind: %r" % (kind,))


def _v3_field_ident(field: str) -> str:
    """Record struct field name in Go: EXPORTED (UpperCamel) on every path
    (item 390). `encoding/json` only marshals exported fields, so an unexported
    field made `json_stringify(record)` return `{}` on the go tier while py/ts
    emitted the real object; exporting the field (paired with a `json:"<revl>"`
    tag in `_emit_v3_go_types` that preserves the source field name in the wire
    bytes) makes records byte-identical across tiers. The exported spelling is
    used at EVERY site — struct declaration, literal construction, and field
    read/write — so record round-trips stay consistent. Capitalizing also
    sidesteps the `_v3_ident` reserved-word mangling: no Go keyword is
    UpperCamel, so `_camel` never collides with one."""
    return _camel(field)


def _v3_record_type_for_fields(fields) -> str:
    """The declared record type whose field set is exactly `fields`, or a
    named EmitError (mirrors `_V3GoCtx.record_type_for_fields` over the
    module-level `_V3_TYPES`)."""
    key = tuple(sorted(fields))
    match: str | None = None
    for name, spec in _V3_TYPES.items():
        if spec.get("kind") == "record" \
                and tuple(sorted(spec.get("fields") or {})) == key:
            if match is not None:
                raise EmitError(
                    "cannot infer Go struct type for record literal with "
                    f"fields {sorted(fields)!r} — more than one declared "
                    "record has exactly those fields"
                )
            match = name
    if match is None:
        raise EmitError(
            "cannot infer Go struct type for record literal with fields "
            f"{sorted(fields)!r} — no record declared in this document has "
            "exactly those fields"
        )
    return _v3_ident(match, "type name")


def _v3_case_layout() -> dict[str, tuple[str, str | None]]:
    """case name -> (ADT type name, payload surface), across every declared
    variant. First declaration wins; ambiguous case names are resolved by the
    node's own `type` key at the call site (the checker freezes it)."""
    out: dict[str, tuple[str, str | None]] = {}
    for name, spec in _V3_TYPES.items():
        if spec.get("kind") != "variant":
            continue
        for case in spec.get("cases") or []:
            cname = case.get("name")
            if cname not in out:
                out[cname] = (name, case.get("payload"))
    return out


def _v3_case_payload(adt: str, case: str) -> str | None:
    """The payload surface of one case of a declared variant, else None."""
    spec = _V3_TYPES.get(adt) or {}
    for c in spec.get("cases") or []:
        if c.get("name") == case:
            return c.get("payload")
    return None


def _v3_comp_construct(node, env: _Env) -> str:
    """A user-ADT construction in a component body: `<Variant><Case>{Value: ..}`
    (or `<Variant><Case>{}` for a nullary case) — mirroring
    `_go_v3_construct`'s user-variant branch, the tagged-union shape the v3
    tier emits (`type Step = Final(Str) | NeedTool(..)` -> `StepFinal{...}`)."""
    case = node.get("case")
    layout = _v3_case_layout()
    adt = node.get("type") or layout.get(case, (None, None))[0]
    if not adt or adt not in _V3_TYPES \
            or _V3_TYPES[adt].get("kind") != "variant":
        raise EmitError(
            f"cannot resolve ADT case {case!r} to a declared variant")
    payload = _v3_case_payload(adt, case)
    args = [_expr(a, env) for a in node.get("args") or []]
    struct = "%s%s" % (_v3_ident(adt, "type name"), case)
    if payload is None:
        if args:
            raise EmitError(f"variant case `{case}` takes no payload")
        return "%s{}" % struct
    if len(args) != 1:
        raise EmitError(f"variant case `{case}` takes exactly one payload")
    return "%s{Value: %s}" % (struct, args[0])


def _go_comp_match(node, env: _Env, expected) -> str:
    """A `match` in a component/method body (v3 typed-core placement).

    User ADTs lower to the same sealed-interface type switch the pure tier
    uses; Opt/Result lower against the stc-go tuple convention (`(T, bool)` /
    `(T, E, bool)`), so their scrutinee must be a multi-value call. Both are
    immediately-applied func literals, so the whole match is one Go
    expression exactly like the pure tier's `_go_v3_match`."""
    scrut_node = node.get("scrutinee")
    st = _comp_infer(scrut_node, env)
    arms = node.get("arms") or []
    exp_t = _go_type(expected) if expected else None
    if not exp_t:
        for arm in arms:
            exp_t = _go_type(_comp_infer(arm.get("body"), env))
            if exp_t:
                break
    exp_t = exp_t or "any"

    is_opt = isinstance(st, str) and st.startswith("Opt[") and st.endswith("]")
    is_result = isinstance(st, str) and st.startswith("Result[") and st.endswith("]")
    if is_opt or is_result:
        return _go_comp_match_tuple(node, env, expected, st, is_opt, is_result,
                                    exp_t)

    scrutinee = _expr(scrut_node, env, st)
    layout = _v3_case_layout()
    lines = ["func() %s {" % exp_t]
    lines.append("\tswitch _m := %s.(type) {" % scrutinee)
    has_wild = False
    saved_types = dict(env.var_types)
    try:
        for arm in arms:
            pattern = arm.get("pattern")
            bind = arm.get("bind")
            if pattern == "_":
                has_wild = True
                lines.append("\tdefault:")
                lines.append("\t\t_ = _m")
                lines.append("\t\treturn %s" % _expr(arm.get("body"), env, expected))
                continue
            adt = (st if (isinstance(st, str) and st in _V3_TYPES)
                   else layout.get(pattern, (None, None))[0])
            if not adt or adt not in _V3_TYPES \
                    or _V3_TYPES[adt].get("kind") != "variant":
                raise EmitError(
                    f"cannot resolve match case {pattern!r} against scrutinee "
                    f"type {st!r} — no declared variant provides it")
            payload = _v3_case_payload(adt, pattern)
            case_type = "%s%s" % (_v3_ident(adt, "type name"), pattern)
            lines.append("\tcase %s:" % case_type)
            # `bind == "_"` is a wildcarded payload (`Case(_) => ..`): there is
            # no name to hold the value, so it is discarded exactly like an
            # unbound arm rather than assigned to and read back from a literal
            # `_` (Go: "cannot use _ as value").
            if bind and bind != "_":
                if payload is None:
                    raise EmitError(
                        f"match arm binds {bind!r} but case {pattern!r} of "
                        f"{adt} has no payload")
                env.var_types[bind] = payload
                lines.append("\t\t%s := _m.Value" % _safe_local(bind))
                lines.append("\t\t_ = %s" % _safe_local(bind))
            else:
                lines.append("\t\t_ = _m")
            lines.append("\t\treturn %s" % _expr(arm.get("body"), env, expected))
    finally:
        env.var_types = saved_types
    if not has_wild:
        lines.append("\tdefault:")
        lines.append("\t\t_ = _m")
        lines.append('\t\tpanic("unreachable: non-exhaustive match")')
    lines.append("\t}")
    lines.append("}()")
    return "\n".join(lines)


def _go_comp_match_tuple(node, env: _Env, expected, st, is_opt, is_result,
                         exp_t) -> str:
    """`match` over an Opt/Result scrutinee in a component body.

    The stc-go world carries Opt/Result as `(T, bool)` / `(T, E, bool)`
    tuples in call positions, so the scrutinee must be a multi-value call
    (a bare Opt/Result value cannot be a single Go expression)."""
    scrut_node = node.get("scrutinee")
    if scrut_node.get("kind") not in ("call", "host", "builtin"):
        raise EmitError(
            "match over an Opt/Result scrutinee on the cordis-go tier needs a "
            f"call-shaped scrutinee (Opt/Result are return-position tuples); "
            f"got a {scrut_node.get('kind')!r} scrutinee")
    arms = node.get("arms") or []
    ok_arm = next((a for a in arms if a.get("pattern") in ("Some", "Ok")), None)
    err_arm = next((a for a in arms if a.get("pattern") in ("None", "Err")), None)
    wild = next((a for a in arms if a.get("pattern") == "_"), None)
    if scrut_node.get("kind") == "builtin" \
            and scrut_node.get("method") in _GO_CHECKED_DIV:
        # the total division forms: the stc-go Result tuple, multi-value
        scrutinee = _comp_checked_div_expr(scrut_node, env)
    else:
        scrutinee = _expr(scrut_node, env, st)
    saved_types = dict(env.var_types)
    try:
        if is_opt:
            inner = st[4:-1]
            lines = ["func() %s {" % exp_t]
            lines.append("\t_v, _ok := %s" % scrutinee)
            if ok_arm is not None:
                bind = ok_arm.get("bind")
                lines.append("\tif _ok {")
                # `bind == "_"` (`Some(_) => ..`) has no name to hold the
                # value; discard it the same way an unbound arm does instead
                # of assigning to and reading back a literal `_`.
                if bind and bind != "_":
                    env.var_types[bind] = inner
                    lines.append("\t\t%s := _v" % _safe_local(bind))
                    lines.append("\t\t_ = %s" % _safe_local(bind))
                else:
                    lines.append("\t\t_ = _v")
                lines.append("\t\treturn %s" % _expr(ok_arm.get("body"), env, expected))
                lines.append("\t}")
            elif wild is not None:
                lines.append("\tif _ok {")
                lines.append("\t\treturn %s" % _expr(wild.get("body"), env, expected))
                lines.append("\t}")
            lines.append("\t_ = _v")
            if err_arm is not None:
                bind = err_arm.get("bind")
                if bind and bind != "_":
                    env.var_types[bind] = inner
                    lines.append("\t%s := _v; _ = %s" % (_safe_local(bind), _safe_local(bind)))
                lines.append("\treturn %s" % _expr(err_arm.get("body"), env, expected))
            elif wild is not None:
                lines.append("\treturn %s" % _expr(wild.get("body"), env, expected))
            else:
                lines.append('\tpanic("unreachable: non-exhaustive match")')
            lines.append("}()")
            return "\n".join(lines)
        ok, err = _v3_split_generic(st[7:-1])
        lines = ["func() %s {" % exp_t]
        lines.append("\t_v, _e, _ok := %s" % scrutinee)
        if ok_arm is not None:
            bind = ok_arm.get("bind")
            lines.append("\tif _ok {")
            # `bind == "_"` (`Ok(_) => ..`) has no name to hold the value;
            # discard it the same way an unbound arm does instead of
            # assigning to and reading back a literal `_`.
            if bind and bind != "_":
                env.var_types[bind] = ok
                lines.append("\t\t%s := _v" % _safe_local(bind))
                lines.append("\t\t_ = %s" % _safe_local(bind))
            else:
                lines.append("\t\t_ = _v")
            lines.append("\t\treturn %s" % _expr(ok_arm.get("body"), env, expected))
            lines.append("\t}")
        elif wild is not None:
            lines.append("\tif _ok {")
            lines.append("\t\treturn %s" % _expr(wild.get("body"), env, expected))
            lines.append("\t}")
        lines.append("\t_ = _e")
        if err_arm is not None:
            bind = err_arm.get("bind")
            if bind and bind != "_":
                env.var_types[bind] = err
                lines.append("\t%s := _e; _ = %s" % (_safe_local(bind), _safe_local(bind)))
            lines.append("\treturn %s" % _expr(err_arm.get("body"), env, expected))
        elif wild is not None:
            lines.append("\treturn %s" % _expr(wild.get("body"), env, expected))
        else:
            lines.append('\tpanic("unreachable: non-exhaustive match")')
        lines.append("}()")
        return "\n".join(lines)
    finally:
        env.var_types = saved_types


def _spawn_expr(node, env: _Env) -> str:
    """Lower an instance-parametric `spawn` acquisition to a per-target helper
    call (docs/design-v2-instances.md, phase 1).

    `spawn` is the acquisition of a `let-effect` step, so this renders the
    single Go expression the step binds to the handle. The heavy lifting —
    isolating each provided key into a FRESH LOCAL realm (a distinct
    `*stc.Realm` per spawn, so two instances of one component never collide)
    and plugging the target *template* as a CHILD FIBER of the spawner — lives
    in the emitted `revlSpawn<Target>` helper; here we just pass the spawner's
    context and the config that flowed through the spawn.
    """
    target = node.get("component")
    if not isinstance(target, str) or not target.isidentifier():
        raise EmitError("bad spawn component %r" % (target,))
    parent = env.ctx_ref()
    cfg = node.get("config") or {}
    if cfg:
        fields = ", ".join(
            "%s: %s" % (_camel(k), _expr(v, env)) for k, v in cfg.items()
        )
        return "revlSpawn%s(%s, %sConfig{%s})" % (
            _camel(target), parent, _camel(target), fields)
    return "revlSpawn%s(%s)" % (_camel(target), parent)


def _instance_get_expr(node, env: _Env) -> str:
    """Lower the instance accessor `s.<key>` (docs/design-v2-instances.md).

    `s : Instance[C]` is a name bound to a `spawn` handle — the emitted
    `*RevlSpawnHandle`, whose `Ctx()` exposes the child's own isolated context
    (the same LOCAL realm the matching `spawn` isolated the provided key into,
    a fresh `*stc.Realm` with no cross-instance sharing). Resolving `key`
    through that context yields THIS instance's provision and no other's:
    `stc.Service[<Svc>](s.Ctx(), _key<Key>)` — the exact realm-scoped read a
    `requires` uses, but against the handle's stored context rather than the
    spawner's own. `service` is the frozen inline typing result, so no
    re-derivation. The service is wrapped in an IIFE so `s.<key>.method(..)`
    (the enclosing `call`/`field` node) chains straight onto the resolved
    value; the `(svc, err)` tuple `stc.Service` returns cannot be a method
    receiver directly.

    Supervision-tree addressing holds because only the handle holder reaches
    that context: the root and any sibling (isolated into a different local
    realm) resolve a non-nil error and the zero-value service — the negative
    the scenario proves by reading the same key at the root. The frontend
    already rejected a `key` the target does not provide (a compile error,
    never lowered here)."""
    handle = _expr(node.get("target"), env)
    service = node.get("service")
    if not isinstance(service, str) or not service:
        raise EmitError("instance-get: bad frozen service type %r" % (service,))
    key = node.get("key")
    if not isinstance(key, str) or not key.isidentifier():
        raise EmitError("instance-get: bad key %r" % (key,))
    svc = _camel(service)
    return ("func() %s { _svc, _ := stc.Service[%s](%s.Ctx(), %s); return _svc }()"
            % (svc, svc, handle, _key_var(key)))


def _comp_infer(node, env: _Env):
    """Best-effort surface type of a component-body expression, else None."""
    if not isinstance(node, dict):
        return None
    k = node.get("kind")
    if k == "lit":
        v = node.get("value")
        if isinstance(v, bool):
            return "Bool"
        if isinstance(v, int):
            return "Int"
        if isinstance(v, float):
            return "Float"
        if isinstance(v, str):
            return "Str"
        return None
    if k == "int":
        return "Int"
    if k == "bool":
        return "Bool"
    if k in ("str", "format"):
        return "Str"
    if k in ("name", "var"):
        return env.var_types.get(node.get("id") or node.get("name"))
    if k == "list":
        items = node.get("items") or []
        el = _comp_infer(items[0], env) if items else None
        return "List[%s]" % (el or "Int")
    if k == "index":
        tt = _comp_infer(node.get("target"), env)
        if isinstance(tt, str) and tt.startswith("List[") and tt.endswith("]"):
            return tt[5:-1]
        return None
    if k == "bin":
        op = node.get("op")
        if op in ("==", "===", "!=", "!==", "<", ">", "<=", ">=", "&&", "||"):
            return "Bool"
        return _comp_infer(node.get("left"), env) or _comp_infer(node.get("right"), env)
    if k == "un":
        return "Bool" if node.get("op") == "!" else _comp_infer(node.get("operand"), env)
    if k == "builtin":
        return _v3_builtin_ret_type(node.get("method"), _comp_infer(node.get("target"), env))
    if k == "maplit":
        # `Map.empty()` — the empty literal carries its pin when the author's
        # annotation supplied one (roadmap 76b); otherwise it stays unknown.
        return node.get("expected")
    if k == "record":
        # record literal -> the declared record type whose field set matches
        try:
            return _v3_record_type_for_fields(
                [f[0] for f in node.get("fields") or []])
        except EmitError:
            return None
    if k == "adt":
        # construction -> the ADT type the checker froze onto the node (e.g.
        # `Result[Any, Any]` for Ok/Err, `Found` for a user case)
        t = node.get("type")
        if t:
            return t
        return _v3_case_layout().get(node.get("case"), (None, None))[0]
    if k == "match":
        # a match's value type is its scrutinee's
        return _comp_infer(node.get("scrutinee"), env)
    if k == "field":
        tt = _comp_infer(node.get("target"), env)
        if isinstance(tt, str) and tt in _V3_TYPES \
                and _V3_TYPES[tt].get("kind") == "record":
            return (_V3_TYPES[tt].get("fields") or {}).get(node.get("name"))
        return None
    return None


# stdlib helpers referenced by component method bodies live in the shared v3
# preamble; using any one flags the preamble + its imports into the module.
_COMP_NEEDS_STDLIB = False
# Map value helpers (revlMapSet/Get/Has) referenced by component bodies:
# flags _V3_MAP_PREAMBLE (+ the Opt preamble its Get answers into).
_COMP_NEEDS_MAP = False
# Str.to_int (revlParseInt) referenced by a component body: flags the Opt
# preamble (the helper's return type) plus the helper itself.
_COMP_NEEDS_PARSE_INT = False
# `Int.to_str` / an interpolated Int in a component body renders through
# strconv.FormatInt rather than fmt.Sprintf("%d") (item 434 (f)): flags the
# `strconv` import, which this tier does not otherwise always carry.
_COMP_NEEDS_STRCONV = False
# A `timer` step (item 57) in a component body: flags the clock coeffect +
# timer scheduler preamble (_TIMER_PREAMBLE). Timers lower to a revertible
# schedule whose inverse is cancellation, wired into the same effect ledger.
_COMP_NEEDS_TIMER = False
# Per-emit counter for unique timer local names (`_revlTimer1`, `_revlTimer2`).
_TIMER_COUNTER = 0
# item 130 Slice 3: a `subscribe` bracket (or any `Stream.*` host verb) in a
# component body flags the stream host runtime (`_STREAM_PREAMBLE`) — the
# cancel-channel `select` this tier's `next` parks on. A document that uses no
# stream leaves it False and emits byte-identically to before.
_COMP_NEEDS_STREAM = False


def _flag_stream() -> None:
    """Pull the stream host runtime into this module's preamble (item 130)."""
    global _COMP_NEEDS_STREAM
    _COMP_NEEDS_STREAM = True

# item 243/247 (docs/design/teardown-contract.md): witnessed externs by name,
# so a component step's acquisition can be recognised as a `transactional`
# entry and register its DECLARED inverse (not a site-spelled one) into the
# per-activation frame. Absent/empty for every document that declares no
# `witnessed` extern, so their emission stays byte-identical. Rebuilt per
# `emit()` call (mirrors `_V3_TYPES`).
_WITNESSED_EXTERNS: dict = {}
# Whether the document needs the `RevlFrame` teardown accumulator preamble
# (any component uses a witnessed effect or an `emit ... compensate ...`).
# Flags `_TEARDOWN_PREAMBLE` + the `time`/`os`/`strconv` imports into the
# module; a document that uses neither stays byte-identical to before.
_COMP_NEEDS_TEARDOWN = False
# item 318: whether some provide-METHOD body registers a witnessed effect into
# its component's activation frame (the per-tool-call H1 seam). When set, the
# extended teardown preamble is emitted (the `deferred`/`aborting` frame state,
# the `Abort`/`registerMethodWitnessed` methods, the frame registry). A document
# with only activation-body witnessed effects / compensations leaves this False
# and emits the base preamble byte-identically.
_COMP_NEEDS_METHOD_WITNESSED = False
# Per-emit counter for unique witnessed-step local names (`_revlWit1`, …).
_WITNESSED_COUNTER = 0
# item 322 Slice 1: record mode. When True, a witnessed transactional step also
# writes a durable discharge-descriptor to the go WAL sink (revlRecordTransactional)
# and the recording preamble is emitted. Default False -> byte-identical output.
_RECORD_MODE = False


def _comp_builtin(method, recv_surface, target, args):
    """Map a `.length()/.push()/…` stdlib call to a revl* helper (Go)."""
    global _COMP_NEEDS_STDLIB
    _COMP_NEEDS_STDLIB = True
    is_str = (recv_surface == "Str")
    if method == "length":
        return ("revlStrLen(%s)" if is_str else "revlListLen(%s)") % target
    if method == "push":
        return "revlListPush(%s, %s)" % (target, args[0])
    if method == "concat":
        return (("revlStrConcat(%s, %s)" if is_str else "revlListConcat(%s, %s)")
                % (target, args[0]))
    if method == "slice":
        return (("revlStrSlice(%s, %s, %s)" if is_str else "revlListSlice(%s, %s, %s)")
                % (target, args[0], args[1]))
    if method == "indexOf":
        return (("revlStrIndexOf(%s, %s)" if is_str else "revlListIndexOf(%s, %s)")
                % (target, args[0]))
    if method == "split":
        return "revlStrSplit(%s, %s)" % (target, args[0])
    if method == "join":
        return "revlJoin(%s, %s)" % (target, args[0])
    if method == "repeat":
        return "revlStrRepeat(%s, %s)" % (target, args[0])
    if method == "charAt":
        return "revlStrCharAt(%s, %s)" % (target, args[0])
    if method == "charCodeAt":
        return "revlStrCharCodeAt(%s, %s)" % (target, args[0])
    # Codepoint-at-index scan (item 276, docs/stdlib-2.0.md §Str.codepoint_at):
    # the Unicode scalar at code-point index i, via the same rune-indexed
    # helper as charCodeAt.
    if method == "codepoint_at":
        return "revlStrCharCodeAt(%s, %s)" % (target, args[0])
    # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith).
    if method == "startsWith":
        return "strings.HasPrefix(%s, %s)" % (target, args[0])
    if method == "endsWith":
        return "strings.HasSuffix(%s, %s)" % (target, args[0])
    # Int/Int32 width conversions (docs/arithmetic.md): Int32 widen is the
    # identity int64; Str.to_int (FR-9) parses to the sealed RevlOpt.
    if method == "to_int":
        if is_str:
            global _COMP_NEEDS_PARSE_INT
            _COMP_NEEDS_PARSE_INT = True
            return "revlParseInt(%s)" % (target,)
        return "int64(%s)" % (target,)
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str): strconv.FormatInt
    # base 10 is exact decimal for an int64 and takes the int64 directly, where
    # fmt.Sprintf("%d", x) boxes it into an `any` first (item 434 (f)).
    if method == "to_str":
        global _COMP_NEEDS_STRCONV
        _COMP_NEEDS_STRCONV = True
        return "strconv.FormatInt(%s, 10)" % _go_widen_int(target)
    # The Map value type (docs/stdlib-2.0.md §Map): the same helpers the v3
    # tier uses; they live in _V3_MAP_PREAMBLE, pulled in by
    # _COMP_NEEDS_MAP at module assembly.
    global _COMP_NEEDS_MAP
    if method == "set":
        _COMP_NEEDS_MAP = True
        return "revlMapSet(%s, %s, %s)" % (target, args[0], args[1])
    if method == "lookup":
        _COMP_NEEDS_MAP = True
        return "revlMapGet(%s, %s)" % (target, args[0])
    if method == "has":
        _COMP_NEEDS_MAP = True
        return "revlMapHas(%s, %s)" % (target, args[0])
    # The iteration/remove step (docs/stdlib-2.0.md §Map): the same helpers,
    # in _V3_MAP_PREAMBLE. revlMapKeys sorts a copy of the key set into
    # canonical (UTF-8 byte) order — go's range order is randomized.
    if method == "size":
        _COMP_NEEDS_MAP = True
        return "int64(len(%s))" % target
    if method == "keys":
        _COMP_NEEDS_MAP = True
        return "revlMapKeys(%s)" % (target,)
    if method == "remove":
        _COMP_NEEDS_MAP = True
        return "revlMapRemove(%s, %s)" % (target, args[0])
    if method in _GO_CHECKED_DIV:
        # The total division forms answer a Result, which the stc-go world
        # carries as a (T, E, bool) tuple — only valid in the two positions
        # that accept a multi-value expression: a `match` scrutinee and a
        # `return` against a Result-typed method. A bare value use would
        # emit Go that cannot compile, so it is refused here with the way
        # out named (mirrors the Opt tuple convention on this tier).
        raise EmitError(
            f"{method} is only lowerable on the cordis-go tier as a `match` "
            f"scrutinee or in a Result-returning `return` (Result is a "
            f"return-position tuple here)")
    raise EmitError("unknown stdlib method: %r" % (method,))


def _comp_checked_div_expr(node, env: _Env) -> str:
    """The total division forms (docs/arithmetic.md) as the stc-go tier's
    Result tuple `(T, E, bool)`: an immediately-applied func literal that
    evaluates each operand once, returns `(0, reason, false)` on a zero
    divisor (and on the Int.MIN / -1 overflow for the truncating form), else
    the quotient. Only valid where a multi-value expression is: a `match`
    scrutinee or a Result-returning `return`."""
    method = node.get("method")
    target = _expr(node.get("target"), env)
    args = [_expr(a, env) for a in node.get("args") or []]
    if len(args) != 1:
        raise EmitError(f"{method} takes exactly one divisor")
    if method != "checked_div_trunc":
        # revlDivFloor / revlDivEuclid / revlMod live in the pure-tier
        # int-arith preamble, which the stc-go component path does not emit —
        # a named tier limit rather than a reference to an undefined helper.
        raise EmitError(
            f"{method} is not lowerable in the stc-go component world yet "
            f"(the int-arith helpers are pure-tier only); use "
            f"`checked_div_trunc` here")
    quotient = "_a / _b"
    return (
        "func() (int64, string, bool) { "
        f"var _a, _b int64; _a, _b = {target}, {args[0]}; "
        f'if _b == 0 {{ return 0, "{_GO_DIV_ZERO_MSG}", false }}; '
        "if _a == (-9223372036854775807 - 1) && _b == -1 "
        '{ return 0, "revl: Int overflow", false }; '
        f"return ({quotient}), \"\", true }}()"
    )


def _format(template: str, args: list[str], arg_types: list | None = None) -> str:
    """revl format template with $0,$1 placeholders -> a Go `string` expression.

    `fmt.Sprintf` with `%v` takes `...any`, so every substituted operand is
    boxed into an interface (item 434 (f)). Where each operand's surface type
    is known the template instead renders as a `+` chain of `string`-typed
    pieces: one allocation for the result and no boxing. `%v` remains the
    fallback for an operand whose type could not be inferred.
    """
    types = list(arg_types or [])
    out: list[str] = []          # the fmt.Sprintf template
    pieces: list[str] = []       # `string`-typed operands of the `+` chain
    text: list[str] = []         # literal run pending flush into `pieces`
    used = 0
    typed = True
    wants_strconv = False

    def flush_text() -> None:
        if text:
            pieces.append(_go_string("".join(text)))
            text.clear()

    i = 0
    while i < len(template):
        c = template[i]
        if c == "$" and i + 1 < len(template) and template[i + 1].isdigit():
            j = i + 1
            while j < len(template) and template[j].isdigit():
                j += 1
            out.append("%v")
            # The k-th placeholder takes the k-th argument, which is the
            # mapping this function has always used (the index digits are not
            # read); keep it so nothing but the boxing changes.
            arg = args[used] if used < len(args) else None
            at = types[used] if used < len(types) else None
            used += 1
            i = j
            if arg is None:
                typed = False
                continue
            flush_text()
            if at == "Str":
                pieces.append(arg)
            elif at in ("Int", "Int32"):
                wants_strconv = True
                inner = _go_widen_int(arg) if at == "Int" else "int64(%s)" % arg
                pieces.append("strconv.FormatInt(%s, 10)" % inner)
            elif at == "Bool":
                wants_strconv = True
                pieces.append("strconv.FormatBool(%s)" % arg)
            else:
                typed = False
            continue
        if c == "%":
            out.append("%%")
        else:
            out.append(c)
        text.append(c)
        i += 1
    flush_text()
    fmt_str = _go_string("".join(out))
    if not args:
        return "fmt.Sprintf(%s)" % fmt_str
    if typed and used == len(args):
        if wants_strconv:
            global _COMP_NEEDS_STRCONV
            _COMP_NEEDS_STRCONV = True
        if len(pieces) == 1:
            return pieces[0]
        return "(%s)" % " + ".join(pieces)
    return "fmt.Sprintf(%s, %s)" % (fmt_str, ", ".join(args))


def _go_string(s: str) -> str:
    """A Go double-quoted string literal, escaped *from code points*.

    The IR stores a `Str` literal as Unicode scalar values (docs/strings.md).
    Go source is UTF-8: a BMP scalar spells as `\\uXXXX` (4 hex) and an astral
    scalar as `\\UXXXXXXXX` (8 hex). `json.dumps` instead emits the astral
    scalar as a UTF-16 surrogate pair (`\\ud83d\\ude00`), which Go rejects as an
    invalid Unicode code point — the reason astral literals used to fail to
    compile here. ASCII and BMP non-ASCII stay byte-identical to the old
    `json.dumps` output (printable ASCII verbatim; `\\uXXXX` for `é` etc.), so
    only astral literals change and v1 goldens stay frozen.
    """
    s = str(s)
    out = ['"']
    for ch in s:
        cp = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif 0x20 <= cp <= 0x7E:
            out.append(ch)
        elif cp <= 0xFFFF:
            out.append("\\u%04x" % cp)
        else:
            out.append("\\U%08x" % cp)
    out.append('"')
    return "".join(out)


# --------------------------------------------------------------------------
# services
# --------------------------------------------------------------------------

def _emit_services(services: dict) -> list[str]:
    out = []
    for sname, sdef in services.items():
        out.append("// service %s" % sname)
        out.append("type %s interface {" % _camel(sname))
        for mname, m in sdef.get("methods", {}).items():
            params = ", ".join(
                "%s %s" % (_safe_local(p["name"]), _go_type(p["type"]))
                for p in m.get("params", [])
            )
            ret = _go_return(m.get("returns"))
            if m.get("idempotent"):
                # delivery semantics (item 44): safe to re-deliver, so the
                # runtime may auto-retry a transient failure of this emission
                out.append("\t// idempotent: the runtime may auto-retry a transient failure")
            sig = "\t%s(%s)" % (_camel(mname), params)
            if ret:
                sig += " " + ret
            out.append(sig)
        out.append("}")
        out.append("")
    return out


# --------------------------------------------------------------------------
# provide-impl methods
# --------------------------------------------------------------------------

def _collect_refs(body, binds_out, reqs_out):
    """Walk a step/expr tree, recording referenced binds/reqs (best effort;
    over-capture is harmless)."""
    if isinstance(body, dict):
        k = body.get("kind")
        if k == "req":
            reqs_out.add(body["name"])
        if body.get("step") is not None or k is not None:
            for v in body.values():
                _collect_refs(v, binds_out, reqs_out)
    elif isinstance(body, list):
        for x in body:
            _collect_refs(x, binds_out, reqs_out)


def _method_returns_value(m) -> bool:
    return bool(m.get("params") is not None) and _go_return(_method_ret(m)) != ""


def _method_ret(m):
    # provide-block method params are names only; the return type comes from
    # the service declaration, resolved by the caller.
    return m.get("_ret")


def _emit_provide_impl(comp_name, prov_name, service_name, methods, services,
                       binds, reqs, has_config, out):
    """Emit the impl struct + methods for one `provide` block."""
    struct = "%s_%s" % (comp_name, prov_name)
    svc = services.get(service_name, {})
    svc_methods = svc.get("methods", {})

    # item 318 / item-247 method-body remainder: a provide block whose method
    # registers a per-tool-call frame entry — a witnessed effect
    # (`registerMethodWitnessed`) or an `emit ... compensate ...`
    # (`registerMethodCompensation`) — holds the enclosing component's activation
    # frame so that entry can be parked on it. Only such a block gains the field,
    # so every other provide impl stays byte-identical.
    has_method_frame = any(
        _method_body_has_witnessed(m.get("body"))
        or _method_body_has_compensate(m.get("body"))
        for m in methods)

    # struct fields: ctx + config + every bind + every req (over-capture ok).
    out.append("type %s struct {" % struct)
    out.append("\tctx *stc.Context")
    if has_method_frame:
        out.append("\trevlFrame *RevlFrame")
    if has_config:
        out.append("\tcfg %sConfig" % _camel(comp_name))
    for b in binds:
        out.append("\t%s %s%s" % (_bind_field(b), _bind_star(b), _host_of_bind(b)))
    for r in reqs:
        out.append("\t%s %s" % (_req_field(r), _camel(_service_of_req(r, services, reqs_map=None))))
    out.append("}")
    out.append("")

    for m in methods:
        mname = m["name"]
        decl = svc_methods.get(mname, {})
        params_decl = decl.get("params", [])
        ret = _go_return(decl.get("returns"))
        # map param names to declared types
        ptypes = {p["name"]: p["type"] for p in params_decl}
        go_params = ", ".join(
            "%s %s" % (_safe_local(pn), _go_type(ptypes.get(pn, "any")))
            for pn in m.get("params", [])
        )
        sig = "func (%s *%s) %s(%s)" % (_METHOD_RECEIVER, struct,
                                        _camel(mname), go_params)
        if ret:
            sig += " " + ret
        sig += " {"
        out.append(sig)
        env = _Env(binds, reqs, _config_fields_flag(has_config),
                   params=m.get("params", []), receiver=_METHOD_RECEIVER)
        for pn in m.get("params", []):
            env.var_types[pn] = ptypes.get(pn)
        _emit_method_body(m.get("body", []), env, out, 1,
                          ret_surface=decl.get("returns"))
        out.append("}")
        out.append("")


def _emit_go_router_struct(cname, key, service_name, route, services, out):
    """item 173: the emitted realization of a routed require on cordis-go,
    mirroring src/revl/run.py::_Router and the rust `_emit_router_struct`.

    A per-(component, key) struct implementing the required service interface.
    It holds no worker handle — every method re-resolves the live per-realm
    handle off the strict, single-realm liveness-checked read the stc-go fork
    adds (`stc.ServiceInRealm[Svc](ctx, key, _revlRealm(realm))`, which returns
    ok=false for a realm with no ACTIVE provider and — unlike plain resolve —
    never falls back up the realm chain to the router's own root provision). So
    a withdrawn worker realm drops out of the live set and its calls go to the
    survivors — reactive failover from the emitted body. The struct is wired as
    the component's handle for the routed key, so a provide-method's
    `<key>.<op>(..)` forwards straight through it (G2: one provider downstream).
    """
    struct = "revlRouter%s%s" % (cname, _camel(key))
    ctor = "newRevlRouter%s%s" % (cname, _camel(key))
    svc = _camel(service_name)
    realms = list(route.get("realms") or [])
    strategy = route.get("strategy") or "round_robin"
    realm_lits = ", ".join(_go_string(r) for r in realms)
    realm_fn = _realm_helper_name()
    methods = (services.get(service_name, {}) or {}).get("methods", {}) or {}

    out.append("type %s struct {" % struct)
    out.append("\tctx      *stc.Context")
    out.append("\tkey      stc.Key")
    out.append("\trealms   []string")
    out.append("\tstrategy string")
    out.append("\tmu       sync.Mutex")
    out.append("\tcursor   int")
    out.append("\tserved   map[string]uint64")
    out.append("}")
    out.append("")
    out.append("func %s(ctx *stc.Context) *%s {" % (ctor, struct))
    out.append("\treturn &%s{" % struct)
    out.append("\t\tctx:      ctx,")
    out.append("\t\tkey:      %s," % _key_var(key))
    out.append("\t\trealms:   []string{%s}," % realm_lits)
    out.append("\t\tstrategy: %s," % _go_string(strategy))
    out.append("\t\tserved:   map[string]uint64{},")
    out.append("\t}")
    out.append("}")
    out.append("")
    # live: strict per-realm resolution; a withdrawn realm is simply absent.
    out.append(f"func (r *{struct}) _revlLive() ([]string, map[string]{svc}) {{")
    out.append("\tlabels := []string{}")
    out.append(f"\thandles := map[string]{svc}{{}}")
    out.append("\tfor _, realm := range r.realms {")
    out.append(f"\t\tif h, ok := stc.ServiceInRealm[{svc}](r.ctx, r.key, {realm_fn}(realm)); ok {{")
    out.append("\t\t\tlabels = append(labels, realm)")
    out.append("\t\t\thandles[realm] = h")
    out.append("\t\t}")
    out.append("\t}")
    out.append("\treturn labels, handles")
    out.append("}")
    out.append("")
    # select: strategy over the live set, re-checked every call (failover).
    out.append(f"func (r *{struct}) _revlSelect() {svc} {{")
    out.append("\tlabels, handles := r._revlLive()")
    out.append("\tif len(labels) == 0 {")
    out.append("\t\tpanic(fmt.Sprintf(\"revl: router for %s has no live worker "
               "(realms %v all withdrawn)\", r.key, r.realms))")
    out.append("\t}")
    out.append("\tr.mu.Lock()")
    out.append("\tdefer r.mu.Unlock()")
    out.append("\tif r.strategy == \"least_loaded\" {")
    out.append("\t\tbest := labels[0]")
    out.append("\t\tfor _, l := range labels[1:] {")
    out.append("\t\t\tif r.served[l] < r.served[best] {")
    out.append("\t\t\t\tbest = l")
    out.append("\t\t\t}")
    out.append("\t\t}")
    out.append("\t\tr.served[best]++")
    out.append("\t\treturn handles[best]")
    out.append("\t}")
    out.append("\tn := len(r.realms)")
    out.append("\tfor off := 0; off < n; off++ {")
    out.append("\t\tcand := r.realms[(r.cursor+off)%n]")
    out.append("\t\tif h, ok := handles[cand]; ok {")
    out.append("\t\t\tr.cursor = (r.cursor + off + 1) % n")
    out.append("\t\t\tr.served[cand]++")
    out.append("\t\t\treturn h")
    out.append("\t\t}")
    out.append("\t}")
    out.append("\tpanic(\"revl: router selection unreachable\")")
    out.append("}")
    out.append("")
    # the service interface, forwarding each op through a fresh selection.
    for mname, decl in methods.items():
        params_decl = decl.get("params", []) or []
        ret = _go_return(decl.get("returns"))
        go_params = ", ".join("%s %s" % (_safe_local(p["name"]), _go_type(p["type"]))
                              for p in params_decl)
        sig = "func (r *%s) %s(%s)" % (struct, _camel(mname), go_params)
        if ret:
            sig += " " + ret
        out.append(sig + " {")
        args = ", ".join(_safe_local(p["name"]) for p in params_decl)
        call = "r._revlSelect().%s(%s)" % (_camel(mname), args)
        out.append(("\treturn %s" if ret else "\t%s") % call)
        out.append("}")
        out.append("")


def _config_fields_flag(has_config):
    # placeholder: config field membership isn't needed for ref detection,
    # config refs are explicit ('config' kind). Return empty set.
    return set()


def _emit_method_body(body, env: _Env, out, indent, ret_surface=None):
    pad = "\t" * indent
    for step in body:
        s = step.get("step")
        if s == "return":
            _emit_return(step.get("expr"), ret_surface, env, out, pad)
        elif s in ("let", "var"):
            name = _safe_local(step["name"])
            surface = _comp_infer(step.get("value"), env)
            if surface is not None:
                env.var_types[step["name"]] = surface
            out.append("%s%s := %s" % (pad, name, _expr(step["value"], env, surface)))
            out.append("%s_ = %s" % (pad, name))
        elif s == "assign":
            name = _safe_local(step["name"])
            out.append("%s%s = %s" % (pad, name,
                                      _expr(step["value"], env,
                                            env.var_types.get(step["name"]))))
        elif s == "effect":
            wit = _witnessed_extern(step.get("acquire"))
            if wit is not None:
                # item 318: a witnessed crossing PER TOOL CALL — park its
                # inverse on the component's activation frame, not as a bracket.
                _emit_method_witnessed_step(out, pad, step, wit, env)
            else:
                _emit_effect_step(step, env, out, indent)
        elif s == "emit":
            # item-247 method-body remainder: a method-body `emit ... compensate
            # ...` is a first-class COMPENSATION on the component's activation
            # frame, NOT a plain call that drops the offset (the silent-wrong
            # placeholder this fixes) and NOT a `ctx.Effect` bracket (which stc-go
            # disposes at the wrong time — the disposal-ordering hazard the
            # method-witnessed seam avoids). Fire the emission inline, then park
            # the offset via `registerMethodCompensation`: discharged on a clean
            # commit (the emission was the deliverable), enqueued for Phase 2 on
            # abort (fired after every proof inverse, guarded, residue-collected).
            # The frame is reached the same way as the witnessed seam
            # (`receiver.revlFrame`, wired at provide construction).
            comp_node = step.get("compensate")
            out.append("%s%s" % (pad, _expr(step["expr"], env)))
            if comp_node is not None:
                compensate_call = _expr(comp_node, env)
                key, method = _call_descriptor(comp_node)
                frame = "%s.revlFrame" % env.receiver
                out.append("%s%s.registerMethodCompensation(%s, %s, func() error { %s; return nil })" %
                           (pad, frame, _go_string(key), _go_string(method), compensate_call))
                global _COMP_NEEDS_TEARDOWN, _COMP_NEEDS_METHOD_WITNESSED
                _COMP_NEEDS_TEARDOWN = True
                # the EXT frame (deferred slice, Abort, the registerMethod* seam)
                # is what a method-registered entry parks onto — the same
                # apparatus the method-witnessed path needs.
                _COMP_NEEDS_METHOD_WITNESSED = True
        elif s == "let-effect":
            # item 397: the ONLY let-effect admitted in a provide-method body is
            # a result-declared host CAS (`insert_if_absent`). The bind is a
            # local `bool`; the site-spelled undo is registered on the same
            # per-activation accumulator as the bare method-body effect
            # (`r.ctx.Effect`), guarded on the result so a `false` CAS's inverse
            # is the identity.
            if not _is_map_cas(step.get("acquire")):
                raise EmitError("let-effect not allowed inside a provide method")
            bind = _safe_local(step["bind"])
            acquire = _expr(step["acquire"], env)
            undo = step.get("undo")
            ctx = env.ctx_ref()
            out.append("%svar %s bool" % (pad, bind))
            out.append("%s%s.Effect(func() stc.Inverse {" % (pad, ctx))
            out.append("%s\t%s = %s" % (pad, bind, acquire))
            if undo is not None:
                out.append("%s\treturn func() error { if %s { %s }; return nil }"
                           % (pad, bind, _expr(undo, env)))
            else:
                out.append("%s\treturn nil" % pad)
            out.append("%s})" % pad)
            out.append("%s_ = %s" % (pad, bind))
        else:
            raise EmitError("unsupported method step: %r" % (s,))


def _construction_case(node):
    """(case, arg_nodes) when node builds Some/None/Ok/Err, else None."""
    if not isinstance(node, dict):
        return None
    k = node.get("kind")
    if k == "adt" and node.get("case") in ("Some", "None", "Ok", "Err"):
        return node["case"], node.get("args") or []
    if k == "call":
        callee = node.get("callee") or {}
        nm = callee.get("name") or callee.get("id")
        if nm in ("Some", "None", "Ok", "Err"):
            return nm, node.get("args") or []
    if k in ("var", "name"):
        nm = node.get("name") or node.get("id")
        if nm == "None":
            return "None", []
    return None


def _emit_return(expr, ret_surface, env: _Env, out, pad):
    """Emit a `return`, spreading an Opt/Result construction into the tuple
    convention (Opt[T] -> `v, ok`; Result[T,E] -> `v, e, ok`)."""
    if expr is None:
        out.append("%sreturn" % pad)
        return
    cc = _construction_case(expr)
    rs = ret_surface.strip() if isinstance(ret_surface, str) else ""
    if cc and rs.startswith("Opt[") and cc[0] in ("Some", "None"):
        inner = rs[4:-1]
        if cc[0] == "Some":
            out.append("%sreturn %s, true" % (pad, _expr(cc[1][0], env, inner)))
        else:
            out.append("%sreturn %s, false" % (pad, _go_zero(inner)))
        return
    if cc and rs.startswith("Result[") and cc[0] in ("Ok", "Err"):
        ok, err = _v3_split_generic(rs[7:-1])
        if cc[0] == "Ok":
            out.append("%sreturn %s, %s, true"
                       % (pad, _expr(cc[1][0], env, ok), _go_zero(err)))
        else:
            out.append("%sreturn %s, %s, false"
                       % (pad, _go_zero(ok), _expr(cc[1][0], env, err)))
        return
    if rs.startswith("Opt[") and isinstance(expr, dict) \
            and expr.get("kind") not in ("call", "host"):
        # Returning an Opt-valued expression (a `*T` pointer — e.g. an Opt
        # parameter) into the (T, bool) return convention: deref-or-default.
        # A call/host already yields the (T, bool) tuple, so it returns direct.
        inner = rs[4:-1]
        out.append("%sif _o := %s; _o != nil { return *_o, true }"
                   % (pad, _expr(expr, env, ret_surface)))
        out.append("%sreturn %s, false" % (pad, _go_zero(inner)))
        return
    if rs.startswith("Result[") and isinstance(expr, dict) \
            and expr.get("kind") == "builtin" \
            and expr.get("method") in _GO_CHECKED_DIV:
        # The total division forms answer a Result, carried on this tier as
        # the (T, E, bool) tuple: the multi-value IIFE returns straight into
        # the method's tuple signature.
        out.append("%sreturn %s" % (pad, _comp_checked_div_expr(expr, env)))
        return
    out.append("%sreturn %s" % (pad, _expr(expr, env, ret_surface)))


def _emit_effect_step(step, env: _Env, out, indent):
    """A bare `effect ... undo ...` step (inside Apply body or a method)."""
    pad = "\t" * indent
    acquire = _expr(step["acquire"], env)
    undo = step.get("undo")
    ctx = env.ctx_ref()
    out.append("%s%s.Effect(func() stc.Inverse {" % (pad, ctx))
    out.append("%s\t%s" % (pad, acquire))
    if undo is not None:
        out.append("%s\treturn func() error { %s; return nil }" % (pad, _expr(undo, env)))
    else:
        out.append("%s\treturn nil" % pad)
    out.append("%s})" % pad)


# --------------------------------------------------------------------------
# host binding types
# --------------------------------------------------------------------------

_BIND_HOST = {}  # bind name -> host type (populated per component)
_BIND_MAP_VALUE = {}  # Map bind name -> Go value type (item 113)
_BIND_IS_PTR = {}  # bind name -> whether the local/field is a pointer (item 320)
_FN_RET: dict = {}  # fn/extern name -> declared return type (item 320)


def _host_of_bind(bind):
    return _BIND_HOST.get(bind, "any")


def _bind_is_ptr(bind) -> bool:
    # item 320: host objects and spawn handles are live pointer resources
    # (`*T`); a value-typed acquisition (plain fn / service-method call) is a
    # plain value. Default True keeps host/spawn binds — the only kinds emitted
    # before item 320 — byte-identical.
    return _BIND_IS_PTR.get(bind, True)


def _bind_star(bind) -> str:
    return "*" if _bind_is_ptr(bind) else ""


def _acquire_value_go_type(acquire, services, requires) -> str:
    """Go VALUE type of a non-host/non-spawn `let-effect` acquisition (item
    320). Resolves a service-method call's declared return; falls back to
    `any`, which still holds any value result (so it compiles) when the
    return type cannot be resolved statically."""
    if not isinstance(acquire, dict):
        return "any"
    if acquire.get("kind") == "call" and "method" in acquire:
        target = acquire.get("target") or {}
        if target.get("kind") == "req":
            svc_name = (requires or {}).get(target.get("name"))
            svc = (services or {}).get(svc_name or "", {}) or {}
            method = (svc.get("methods") or {}).get(acquire.get("method"), {}) or {}
            rt = method.get("returns")
            if rt:
                return _go_type(rt) or "any"
    # A plain fn / extern acquisition (`kind == "fn"`, or a `call` with a
    # callee name): resolve the declared return type from the document's
    # fn/extern registry (item 320).
    name = acquire.get("name") or ((acquire.get("callee") or {}).get("name"))
    if name and name in _FN_RET:
        rt = _FN_RET.get(name)
        if rt:
            return _go_type(rt) or "any"
    return "any"


# --------------------------------------------------------------------------
# host Map value type (item 113, FR-4)
#
# The host `Map.new()` object is generic over its value type (`type Map[V any]`
# in the runtime), so a `Map[Str, Int]` counter or `Map[Str, List[Msg]]` ledger
# lowers Insert/Get against the *declared* value type instead of a hardcoded
# String. Go cannot infer `V` from the argument-less `MapNew()`, so emit must
# pin it at the acquisition. The value type is learned from `insert` call sites
# across the whole component (like backends/rust/emit.py's _map_value_rust_type)
# — the surface type of the value argument, then mapped to a Go type. No site
# pinning a concrete type falls back to `string` (the historical surface, and
# what a write-free / read-only Map keeps).
#
# The value type is learned from ANY map value-writing verb, not the literal
# name "insert": a CAS-only writer (`insert_if_absent`, item 397) must pin `V`
# just as `insert` does, or the Map would emit with the string default and
# mistype (item 402). Every value-writer takes the value as arg[1].
# --------------------------------------------------------------------------

# Map verbs that write a value at arg[1]; each pins the host Map's value type V.
_MAP_VALUE_WRITERS = ("insert", "insert_if_absent")

# item 397: the compare-and-set host verb whose bound result is a `bool` and
# whose site-spelled `undo` must be RESULT-GUARDED (registered only when the CAS
# actually inserted — a `false` CAS's inverse is the identity, so teardown never
# removes the winning claimant's entry).
_MAP_CAS_VERBS = ("insert_if_absent",)


def _is_map_cas(acquire) -> bool:
    """Whether a lowered acquisition node is a result-guarded map CAS."""
    return (isinstance(acquire, dict) and acquire.get("kind") == "call"
            and acquire.get("method") in _MAP_CAS_VERBS)


def _map_insert_value_types(node, bind, env, out):
    """Collect the surface types of the value argument at every map
    value-writing call (`insert`, `insert_if_absent`, ...) on `bind` inside an
    expression node (recurses). The value type `V` is inferred structurally
    from the value argument of ANY writer, never by matching a single verb
    name, so a CAS-only writer still pins a concrete `V` (item 402)."""
    if isinstance(node, dict):
        if node.get("kind") == "call" and node.get("method") in _MAP_VALUE_WRITERS:
            target = node.get("target")
            if (isinstance(target, dict)
                    and (target.get("id") or target.get("name")) == bind):
                args = node.get("args") or []
                if len(args) >= 2:
                    t = _comp_infer(args[1], env)
                    if t is not None and "Never" not in str(t):
                        out.append(t)
        for v in node.values():
            _map_insert_value_types(v, bind, env, out)
    elif isinstance(node, list):
        for x in node:
            _map_insert_value_types(x, bind, env, out)


def _infer_map_value_go_type(comp, services, bind):
    """The Go value type of the host Map bound to `bind`, learned from its
    `insert` sites across the activation body and every provide method. Falls
    back to `string` when nothing pins a concrete value type."""
    candidates: list[str] = []
    body = comp.get("body") or []
    for step in body:
        if step.get("step") == "provide":
            service = services.get(step.get("service") or "") or {}
            svc_methods = service.get("methods") or {}
            for method in step.get("methods") or []:
                decl = svc_methods.get(method.get("name") or "", {})
                ptypes = {p["name"]: p["type"] for p in decl.get("params", [])}
                env = _Env([], [], set(), params=list(ptypes),
                           receiver=_METHOD_RECEIVER)
                env.var_types.update(ptypes)
                for body_step in method.get("body") or []:
                    _scan_step_for_inserts(body_step, bind, env, candidates)
        else:
            env = _Env([], [], set())
            _scan_step_for_inserts(step, bind, env, candidates)
    for t in candidates:
        gt = _go_type(t)
        if gt and gt != "any":
            return gt
    return "string"


def _scan_step_for_inserts(step, bind, env, candidates):
    """Walk one component/method body step for `insert` value types, tracking
    `let`/`var` bindings so a value referenced through a local still types."""
    if not isinstance(step, dict):
        return
    kind = step.get("step")
    if kind in ("let", "var"):
        surface = _comp_infer(step.get("value"), env)
        if surface is not None:
            env.var_types[step.get("name")] = surface
        _map_insert_value_types(step.get("value"), bind, env, candidates)
        return
    for key in ("acquire", "undo", "value", "expr", "setup", "body",
                "then", "else"):
        v = step.get(key)
        if isinstance(v, list):
            for item in v:
                if item is not None and item.get("step"):
                    _scan_step_for_inserts(item, bind, env, candidates)
                else:
                    _map_insert_value_types(item, bind, env, candidates)
        elif isinstance(v, dict):
            if v.get("step"):
                _scan_step_for_inserts(v, bind, env, candidates)
            else:
                _map_insert_value_types(v, bind, env, candidates)


_REQ_SERVICE = {}  # req name -> service type


def _service_of_req(name, services, reqs_map):
    return _REQ_SERVICE.get(name, "any")


# --------------------------------------------------------------------------
# components
# --------------------------------------------------------------------------

def _emit_config_struct(comp, out) -> bool:
    cfg = comp.get("config", [])
    if not cfg:
        return False
    name = _camel(comp["name"])
    out.append("type %sConfig struct {" % name)
    for f in cfg:
        out.append("\t%s %s" % (_camel(f["name"]), _go_type(f["type"])))
    out.append("}")
    out.append("")
    # Defaults constructor.
    out.append("func Default%sConfig() %sConfig {" % (name, name))
    out.append("\treturn %sConfig{" % name)
    for f in cfg:
        if f.get("default") is not None:
            out.append("\t\t%s: %s," % (_camel(f["name"]), _default_lit(f["default"], f["type"])))
    out.append("\t}")
    out.append("}")
    out.append("")
    return True


def _default_lit(value, t):
    gt = _go_type(t)
    if gt == "string":
        return _go_string(value)
    if gt == "bool":
        return "true" if value else "false"
    return str(value)


def _emit_component(comp, services, out):
    global _BIND_HOST, _REQ_SERVICE, _BIND_MAP_VALUE, _BIND_IS_PTR
    name = comp["name"]
    cname = _camel(name)
    requires = comp.get("requires", {}) or {}
    provides = comp.get("provides", {}) or {}
    body = comp.get("body", []) or []

    # per-component maps
    _REQ_SERVICE = dict(requires)
    _BIND_HOST = {}
    _BIND_MAP_VALUE = {}
    _BIND_IS_PTR = {}
    for step in body:
        if step.get("step") == "let-effect":
            bind = step["bind"]
            acquire = step["acquire"]
            akind = acquire.get("kind")
            # item 320: only a `host` object or a `spawn` handle is a live
            # pointer resource. Any OTHER acquisition — a plain fn or a
            # service-method call — binds the call's VALUE result, so declare
            # it by the acquisition's actual return type, NOT as `*T`. Before
            # this every bound acquisition was emitted `var x *T`, which does
            # not compile when the acquisition returns a value type (e.g.
            # `let lock = effect db.query(..)` where `query` returns
            # `List[Row]`: `var lock *any` cannot hold `[]Row`).
            if _is_map_cas(acquire):
                # item 397: a CAS binds a checked `bool`, not a host pointer.
                host = "bool"
                _BIND_IS_PTR[bind] = False
            elif akind == "subscribe":
                # item 130: a `subscribe` bracket binds a live `*Subscription`
                # — a host resource whose inverse (`Close`) trips the cancel
                # channel, so it is a pointer resource exactly like a Pool.
                host = "Subscription"
                _BIND_IS_PTR[bind] = True
            elif akind in ("host", "spawn"):
                host = _host_type_of_acquire(acquire)
                # item 113 (FR-4): the host Map is generic over its value type.
                # Pin `V` from the component's `insert` sites so every reference
                # type (field, local, acquisition) instantiates `Map[V]`.
                if acquire.get("kind") == "host" and acquire.get("fn") == "Map.new":
                    gv = _infer_map_value_go_type(comp, services, bind)
                    _BIND_MAP_VALUE[bind] = gv
                    host = "%s[%s]" % (host, gv)
                _BIND_IS_PTR[bind] = True
            else:
                host = _acquire_value_go_type(acquire, services, requires)
                _BIND_IS_PTR[bind] = False
            _BIND_HOST[bind] = host

    binds = [s["bind"] for s in body if s.get("step") == "let-effect"]
    reqs = list(requires.keys())
    # item 173: a routed require (item 162 `routes` IR) has no single-realm
    # provider — it resolves per named realm through the emitted router struct,
    # never a `stc.Service`/`Inject` single handle. Empty for every routes-less
    # component, so such a component emits byte-identically to before.
    routes = comp.get("routes") or {}
    reqs_gated = [r for r in reqs if r not in routes]
    has_config = bool(comp.get("config"))

    # config struct
    _emit_config_struct(comp, out)

    # component constructor
    if has_config:
        out.append("func %s(cfg %sConfig) stc.Component {" % (cname, cname))
    else:
        out.append("func %s() stc.Component {" % cname)
    out.append("\treturn stc.Component{")
    out.append("\t\tName: %s," % _go_string(name))
    if reqs_gated:
        out.append("\t\tInject: []stc.Key{%s}," % ", ".join(_key_var(r) for r in reqs_gated))
    if provides:
        out.append("\t\tProvide: []stc.Key{%s}," % ", ".join(_key_var(p) for p in provides))
    out.append("\t\tApply: func(ctx *stc.Context) (stc.Inverse, error) {")

    # item 243/247: a component using a witnessed effect or a compensation
    # needs the per-activation teardown accumulator. `_revlFrame` is created
    # FIRST, and its Phase-2 drain is registered as the FIRST `ctx.Effect`
    # call — stc-go's `unwind()` runs registered inverses LIFO (last
    # registered runs first), so being first-registered makes this the LAST
    # inverse to run in the unwind, i.e. after every bracket/transactional
    # inverse and every compensation-enqueue has already run (Phase 1 is
    # complete by construction before this fires). On a clean commit
    # `runCompensationPhase` is a no-op (a5a — compensations never ran, so
    # there is nothing to drain).
    needs_frame = _body_needs_frame(body)
    if needs_frame:
        global _COMP_NEEDS_TEARDOWN
        _COMP_NEEDS_TEARDOWN = True
        out.append("\t\t\t_revlFrame := newRevlFrame()")
        out.append("\t\t\tif err := ctx.Effect(func() stc.Inverse {")
        out.append("\t\t\t\treturn func() error { _revlFrame.runCompensationPhase(); return nil }")
        out.append("\t\t\t}); err != nil {")
        out.append("\t\t\t\treturn nil, err")
        out.append("\t\t\t}")

    env = _Env(binds, reqs, set(), receiver="")

    # requires -> Service resolution. A routed key gets a router value (its
    # emitted body re-resolves live per-realm handles per call) instead of a
    # single `stc.Service` handle.
    for rname, svc in requires.items():
        if rname in routes:
            out.append("\t\t\t%s := newRevlRouter%s%s(ctx)" %
                        (_req_field(rname), cname, _camel(rname)))
            continue
        out.append("\t\t\t%s, err := stc.Service[%s](ctx, %s)" %
                    (_req_field(rname), _camel(svc), _key_var(rname)))
        out.append("\t\t\tif err != nil {")
        out.append("\t\t\t\treturn nil, err")
        out.append("\t\t\t}")

    # component body steps (let-effect, effect, provide)
    for step in body:
        _emit_component_step(comp, step, services, env, out)

    # underscore-guard binds and reqs so unused locals never break the build.
    for b in binds:
        out.append("\t\t\t_ = %s" % _bind_field(b))
    for r in reqs:
        out.append("\t\t\t_ = %s" % _req_field(r))
    if has_config:
        out.append("\t\t\t_ = cfg")

    if needs_frame:
        # The activation's OWN returned Inverse is appended to stc-go's
        # inverses slice AFTER every `ctx.Effect` call the body made (stc-go
        # appends it once `Apply` returns, from the orchestrator's
        # `cmdApplied` handling), so it becomes the LAST-registered — hence
        # the FIRST to run on any later unwind. That is the commit marker:
        # flipping `committed` here, before any step's own inverse runs,
        # mirrors the py reference tier's `Frame.drain` (yielded last, so
        # cordis disposes it first). Reached only if `Apply` runs to
        # completion — a mid-body failure returns `nil, err` earlier and
        # this line never executes, so `committed` correctly stays false
        # (the Go zero value) on an abort.
        out.append("\t\t\treturn func() error { _revlFrame.commit(); return nil }, nil")
    else:
        out.append("\t\t\treturn nil, nil")
    out.append("\t\t},")
    out.append("\t}")
    out.append("}")
    out.append("")

    # provide-impl structs/methods
    for step in body:
        if step.get("step") == "provide":
            svc = step["service"]
            _emit_provide_impl(cname, step["name"], svc, step.get("methods", []),
                               services, binds, reqs, has_config, out)

    # item 173: one router struct per routed require, implementing the required
    # service interface by strict per-realm resolution + strategy + failover.
    for rkey, route in routes.items():
        _emit_go_router_struct(cname, rkey, requires[rkey], route, services, out)


def _collect_host_calls(node, acc):
    """Record (receiver, method) of every `host` expr not covered by the fixed
    host runtime (Pool / Map / Stream)."""
    if isinstance(node, dict):
        if node.get("kind") == "host":
            recv, _, meth = str(node.get("fn", "")).partition(".")
            # item 130: `Stream` has a REAL runtime on this tier (the cancel-
            # channel select, `_STREAM_PREAMBLE`), so it must not also get a
            # `func StreamSource(_args ...any) any` no-op stub — that would
            # redeclare the real constructor and drop the semantics.
            if recv and recv not in ("Pool", "Map", "Stream"):
                acc.add((recv, meth))
        for v in node.values():
            _collect_host_calls(v, acc)
    elif isinstance(node, list):
        for x in node:
            _collect_host_calls(x, acc)


def _emit_host_stubs(ir) -> list[str]:
    """Deterministic stubs for host receivers this document references beyond
    Pool/Map (e.g. an awaited `Job.run`). Emitted only when referenced, so the
    Pool/Map-only scenarios stay byte-identical."""
    acc: set = set()
    _collect_host_calls(ir, acc)
    if not acc:
        return []
    out = ["// ---- generated host stubs (hosts beyond the fixed runtime) ----------"]
    for recv, meth in sorted(acc):
        out.append("func %s(_args ...any) any {" % (_camel(recv) + _camel(meth)))
        out.append("\thostRecord(%s)" % _go_string("%s.%s" % (recv, meth)))
        out.append("\treturn nil")
        out.append("}")
        out.append("")
    return out


# --------------------------------------------------------------------------
# witnessed effects + compensation: the three-entry-kind teardown loop
# (items 243/247, docs/design/teardown-contract.md)
# --------------------------------------------------------------------------


def _witnessed_extern(acquire):
    """The witnessed extern descriptor a step's acquisition calls, or None.

    A witnessed effect (item 243) is spelled as a component-step call to a
    `witnessed` extern; the step's acquisition renders as an IR `fn` node
    (`{"kind": "fn", "name": ..., "args": [...]}`), so matching its name
    against `_WITNESSED_EXTERNS` is how the emitter tells a transaction from
    an ordinary bracket. Returns None for every other acquisition, so a
    non-witnessed effect emits exactly as before (mirrors backends/python/
    emit.py `_ComponentEmitter._witnessed_extern`)."""
    if not _WITNESSED_EXTERNS or not isinstance(acquire, dict):
        return None
    if acquire.get("kind") != "fn":
        return None
    return _WITNESSED_EXTERNS.get(acquire.get("name"))


def _method_body_has_witnessed(body) -> bool:
    """True iff a provide-METHOD body (a flat step list) carries a witnessed
    `effect` step (item 318). `let-effect` is not allowed inside a method, so a
    witnessed crossing there is always a bare `effect` step calling a witnessed
    extern."""
    for step in body or []:
        if step.get("step") == "effect" and _witnessed_extern(step.get("acquire")) is not None:
            return True
    return False


def _method_body_has_compensate(body) -> bool:
    """True iff a provide-METHOD body carries an `emit ... compensate ...` step
    (the item-247 method-body compensate remainder). Its compensation is parked
    on the component's activation frame (`registerMethodCompensation`), so the
    provide impl needs the `revlFrame` field exactly like a method-witnessed one.
    Unlike the activation-body site, a method-body compensation must NOT be a
    plain `ctx.Effect` disposer (it would fire on a clean unload — the item-247
    soundness bug this closes on go); the frame seam is what makes it abort-only,
    Phase-2, and discharged on commit."""
    for step in body or []:
        if step.get("step") == "emit" and step.get("compensate") is not None:
            return True
    return False


def _provide_has_method_frame(provide_step) -> bool:
    """True iff any method of a `provide` step registers a per-tool-call entry
    onto the component activation frame — a witnessed effect (item 318) or an
    `emit ... compensate ...` (item-247 method-body remainder). Either makes the
    provide impl struct need a `revlFrame` field and the frame be handed to it."""
    return any(_method_body_has_witnessed(m.get("body"))
               or _method_body_has_compensate(m.get("body"))
               for m in provide_step.get("methods", []) or [])


def _body_needs_frame(steps) -> bool:
    """True iff some step in `steps` (recursing into `if`/`then`/`else`) is a
    witnessed effect or an `emit ... compensate ...`, OR a `provide` block whose
    method registers a witnessed effect (item 318) — i.e. this component's Apply
    needs the `RevlFrame` teardown accumulator. A component using none of these
    gets no frame and emits byte-identically to before."""
    for step in steps or []:
        kind = step.get("step")
        if kind in ("let-effect", "effect") and _witnessed_extern(step.get("acquire")) is not None:
            return True
        if kind == "emit" and step.get("compensate") is not None:
            return True
        if kind == "provide" and _provide_has_method_frame(step):
            return True
        if kind == "if":
            if _body_needs_frame(step.get("then")) or _body_needs_frame(step.get("else")):
                return True
    return False


def _call_descriptor(node) -> tuple[str, str]:
    """Best-effort static (key, method) naming for a residue record's
    `crossing`/`attempted` — the WAL discharge-descriptor's `receiver`/
    `method` (docs/design/teardown-contract.md, "WAL descriptor"), recovered
    directly from the AST at emit time (unlike the py reference tier's
    bytecode introspection, the Go emitter has the call site's own node in
    hand, so this is exact rather than best-effort-by-necessity)."""
    if not isinstance(node, dict):
        return ("call", "call")
    kind = node.get("kind")
    if kind == "call" and "method" in node:
        target = node.get("target") or {}
        key = target.get("name") or target.get("id") or "call"
        return (str(key), str(node.get("method")))
    if kind == "fn":
        name = str(node.get("name") or "call")
        return (name, name)
    if kind == "host":
        fn = str(node.get("fn", "call"))
        recv, _, meth = fn.partition(".")
        return (recv or fn, meth or fn)
    return ("call", "call")


def _witnessed_result_types(returns) -> tuple[str, str]:
    """`Result[W, E]` (a witnessed extern's declared return) -> the Go (W, E)
    type strings, resolved against the document's declared types — the
    typed-core mapper (`_go_v3_type`), not the host-world `_go_type`, because
    a witness is ordinarily a declared record (`Stash`), not a host scalar."""
    t = str(returns).strip()
    if not (t.startswith("Result[") and t.endswith("]")):
        raise EmitError(
            f"a witnessed extern must return Result[W, E], got {returns!r}")
    ok, err = _v3_split_generic(t[7:-1])
    return _go_v3_type(ok, _V3_TYPES), _go_v3_type(err, _V3_TYPES)


def _emit_witnessed_step(out, pad, step, ext, env, bind: Optional[str]) -> None:
    """Emit a witnessed effect (item 243): run the mutation inside the SAME
    `ctx.Effect` the bracket path uses — `install` performs the forward
    action and returns the paired inverse, exactly stc-go's own contract —
    and on the `Ok` branch return a TRANSACTIONAL inverse: it DISCHARGES (a
    no-op; the mutation is the deliverable and persists) on a clean commit,
    and REPLAYS the declared inverse against the captured witness on an
    abort. On `Err` the install returns a nil inverse, so stc-go registers
    NOTHING (Ok-conditional — a failed mutation touched nothing, so it must
    schedule no rollback).

    A panicking inverse is caught and recorded as `restore-residue` (243 rule
    6 — the inverse is fallible by design) rather than propagating: Phase 1
    must run to completion no matter what one inverse does (docs/design/
    teardown-contract.md, "continue-and-record, uniform, two severities")."""
    global _WITNESSED_COUNTER
    _WITNESSED_COUNTER += 1
    n = _WITNESSED_COUNTER
    inner = pad + "\t"
    inner2 = inner + "\t"
    inner3 = inner2 + "\t"

    acquire = _expr(step["acquire"], env)
    ok_t, err_t = _witnessed_result_types(ext.get("returns"))
    result_t = "RevlResult[%s, %s]" % (ok_t, err_t)
    ok_t_full = "RevlOk[%s, %s]" % (ok_t, err_t)
    result_var = "_revlWit%d" % n
    ok_var = "_revlOk%d" % n
    isok_var = "_revlIsOk%d" % n

    ext_name = str(ext.get("name"))
    undo_node = ext.get("undo") or {}
    undo_callee = undo_node.get("callee") or {}
    undo_name = str(undo_callee.get("name") or undo_callee.get("id") or "undo")
    # `result` is the witnessed extern's own binder for the Ok payload passed
    # to its declared `undo` (docs/design/243-witnessed-externs.md, "Slice 1
    # as implemented" #1); naming the Go local literally `result` means the
    # generic name resolver's fallback (an unrecognised identifier renders
    # unchanged) already resolves the undo expression's `result` reference to
    # it — no env plumbing needed.
    undo_expr = _expr(undo_node, env)

    out.append("%svar %s %s" % (pad, result_var, result_t))
    out.append("%sif err := ctx.Effect(func() stc.Inverse {" % pad)
    out.append("%s%s = %s" % (inner, result_var, acquire))
    out.append("%sif %s, %s := %s.(%s); %s {" %
               (inner, ok_var, isok_var, result_var, ok_t_full, isok_var))
    out.append("%sresult := %s.Value" % (inner2, ok_var))
    out.append("%s_ = result" % inner2)
    if _RECORD_MODE:
        # item 322 Slice 1: the durable exit. At REGISTRATION (this closure runs
        # during Apply, when the mutation happens) write the discharge-descriptor
        # — the re-issuable named call recover replays LIFO to undo the mutation
        # — and fsync it, so a crash BEFORE commit is still recoverable from the
        # log alone. The witness is stringified as the referent argument.
        out.append('%srevlRecordTransactional(%s, %s, []string{fmt.Sprintf("%%v", result)})'
                   % (inner2, _go_string(ext_name), _go_string(undo_name)))
    out.append("%sreturn func() (_revlErr error) {" % inner2)
    out.append("%sif _revlFrame.committed {" % inner3)
    out.append("%s\t// item 243 a5a: discharge — the mutation is the" % inner3)
    out.append("%s\t// deliverable and persists; witness GC'd (out of scope)." % inner3)
    out.append("%s\treturn nil" % inner3)
    out.append("%s}" % inner3)
    out.append("%sdefer func() {" % inner3)
    out.append("%s\tif r := recover(); r != nil {" % inner3)
    out.append("%s\t\t_revlFrame.recordResidue(RevlTeardownRecord{" % inner3)
    out.append("%s\t\t\tKind: %s, CrossingKey: %s, CrossingMethod: %s," %
               (inner3, _go_string("restore-residue"), _go_string(ext_name), _go_string(undo_name)))
    out.append("%s\t\t\tAttemptedCall: %s, AttemptedPhase: 1," % (inner3, _go_string(undo_name)))
    out.append("%s\t\t\tErrorType: %s, ErrorMessage: fmt.Sprint(r)," % (inner3, _go_string("panic")))
    out.append("%s\t\t\tOutcome: %s, Referent: %s," % (inner3, _go_string("failed"), _go_string(ext_name)))
    out.append("%s\t\t\tHint: %s + %s + %s," %
               (inner3, _go_string("the witnessed inverse "), _go_string(undo_name),
                _go_string(" panicked during abort replay; verify and finish by hand")))
    out.append("%s\t\t})" % inner3)
    out.append("%s\t}" % inner3)
    out.append("%s}()" % inner3)
    out.append("%s%s" % (inner3, undo_expr))
    out.append("%sreturn nil" % inner3)
    out.append("%s}" % inner2)
    out.append("%s}" % inner)
    out.append("%sreturn nil" % inner)
    out.append("%s}); err != nil {" % pad)
    out.append("%sreturn nil, err" % inner)
    out.append("%s}" % pad)
    if bind is not None:
        out.append("%s%s = %s" % (pad, bind, result_var))
    global _COMP_NEEDS_TEARDOWN
    _COMP_NEEDS_TEARDOWN = True


def _emit_method_witnessed_step(out, pad, step, ext, env) -> None:
    """Emit a witnessed effect inside a PROVIDE-METHOD body (item 318): the
    per-tool-call H1 seam, the go mirror of backends/python/emit.py's
    `_method_witnessed_step` + `Frame.transactional_method`.

    Run the forward mutation INLINE in the method (not inside a `ctx.Effect` —
    the activation-body path uses `ctx.Effect` because stc-go yields its inverse
    into the LIFO teardown stack, but a method body runs AFTER activation, so a
    `ctx.Effect` registered here would land LATER in that stack than the
    activation's commit marker and therefore run BEFORE it on a clean unload —
    reading `committed` still false and WRONGLY REVERTING THE DELIVERABLE, the
    exact disposal-ordering hazard item 318 found on py). Instead, on the `Ok`
    branch the extern's DECLARED inverse is PARKED on the component's activation
    frame via `registerMethodWitnessed`; the commit marker (`commit()`) disposes
    it once the commit-vs-abort bit is settled — discharge (no-op, the mutation
    persists) on a clean commit, replay against the captured witness on an abort.
    On `Err` nothing is parked (Ok-conditional): a failed mutation touched
    nothing, so it schedules no rollback.

    A panicking inverse is caught and recorded as `restore-residue` (243 rule 6),
    never propagated — the same continue-and-record discipline as the
    activation-body path."""
    global _WITNESSED_COUNTER, _COMP_NEEDS_TEARDOWN, _COMP_NEEDS_METHOD_WITNESSED
    _WITNESSED_COUNTER += 1
    n = _WITNESSED_COUNTER
    inner = pad + "\t"
    inner2 = inner + "\t"

    acquire = _expr(step["acquire"], env)
    ok_t, err_t = _witnessed_result_types(ext.get("returns"))
    result_t = "RevlResult[%s, %s]" % (ok_t, err_t)
    ok_t_full = "RevlOk[%s, %s]" % (ok_t, err_t)
    result_var = "_revlWit%d" % n
    ok_var = "_revlOk%d" % n
    isok_var = "_revlIsOk%d" % n

    ext_name = str(ext.get("name"))
    undo_node = ext.get("undo") or {}
    undo_callee = undo_node.get("callee") or {}
    undo_name = str(undo_callee.get("name") or undo_callee.get("id") or "undo")
    undo_expr = _expr(undo_node, env)
    # the enclosing component's activation frame, held as an impl-struct field
    # (`revlSelf.revlFrame`), wired at `provide` construction time.
    frame = "%s.revlFrame" % env.receiver

    out.append("%svar %s %s" % (pad, result_var, result_t))
    out.append("%s%s = %s" % (pad, result_var, acquire))
    out.append("%sif %s, %s := %s.(%s); %s {" %
               (pad, ok_var, isok_var, result_var, ok_t_full, isok_var))
    out.append("%sresult := %s.Value" % (inner, ok_var))
    out.append("%s_ = result" % inner)
    out.append("%s%s.registerMethodWitnessed(func() (_revlErr error) {" % (inner, frame))
    out.append("%sif %s.committed {" % (inner2, frame))
    out.append("%s\t// item 318 a5a: discharge — the mutation is the deliverable" % inner2)
    out.append("%s\t// and persists; witness GC'd (out of scope)." % inner2)
    out.append("%s\treturn nil" % inner2)
    out.append("%s}" % inner2)
    out.append("%sdefer func() {" % inner2)
    out.append("%s\tif r := recover(); r != nil {" % inner2)
    out.append("%s\t\t%s.recordResidue(RevlTeardownRecord{" % (inner2, frame))
    out.append("%s\t\t\tKind: %s, CrossingKey: %s, CrossingMethod: %s," %
               (inner2, _go_string("restore-residue"), _go_string(ext_name), _go_string(undo_name)))
    out.append("%s\t\t\tAttemptedCall: %s, AttemptedPhase: 1," % (inner2, _go_string(undo_name)))
    out.append("%s\t\t\tErrorType: %s, ErrorMessage: fmt.Sprint(r)," % (inner2, _go_string("panic")))
    out.append("%s\t\t\tOutcome: %s, Referent: %s," % (inner2, _go_string("failed"), _go_string(ext_name)))
    out.append("%s\t\t\tHint: %s + %s + %s," %
               (inner2, _go_string("the witnessed inverse "), _go_string(undo_name),
                _go_string(" panicked during abort replay; verify and finish by hand")))
    out.append("%s\t\t})" % inner2)
    out.append("%s\t}" % inner2)
    out.append("%s}()" % inner2)
    out.append("%s%s" % (inner2, undo_expr))
    out.append("%sreturn nil" % inner2)
    out.append("%s})" % inner)
    out.append("%s}" % pad)
    _COMP_NEEDS_TEARDOWN = True
    _COMP_NEEDS_METHOD_WITNESSED = True


def _refuse_unlowered_stream_surface(node, tier: str) -> None:
    """Refuse the item-130 Slice 2 surface this blocking tier does not lower.

    Slice 2 shipped `map`/`filter`/`take` and the three non-default backpressure
    policies on the py reference tier only; Slice 3 lowered subscribe/next/close
    and the `merge` fan-in here. Emitting a subscription that SILENTLY dropped a
    combinator chain, a lossy policy or a drain window would be the worst
    outcome available: the program would run and quietly disagree with the
    reference tier. Refuse by name instead, the same call the wasm tier makes
    for the whole surface."""
    if node.get("stages"):
        raise EmitError(
            "a stream combinator chain (`map`/`filter`/`take`) is not lowered "
            "on the %s tier; the derived-stream chain runs on the py reference "
            "tier (item 130 Slice 2) while this tier lowers subscribe / next / "
            "close and `merge` (Slice 3) — try `--backend py`" % tier)
    policy = node.get("policy") or "error"
    if policy != "error":
        raise EmitError(
            "backpressure policy `%s` is not lowered on the %s tier; this tier "
            "lowers the default `error` policy (a full bounded buffer faults "
            "with `Faulted(overflow)` and closes, no silent loss). "
            "`drop_newest`/`drop_oldest`/`block` run on the py reference tier "
            "(item 130 §4.4) — try `--backend py`" % (policy, tier))
    if node.get("drain") is not None:
        raise EmitError(
            "a `drain` window is the `block`-policy drain interval and is not "
            "lowered on the %s tier; it fires on the deterministic test clock, "
            "which lives on the py reference tier (item 130 §8) — try "
            "`--backend py`" % tier)


def _stream_head(node, env) -> str:
    """The stream a `subscribe` acquires: a plain source, or a `merge(a, b)`
    fan-in (item 130 Slice 3). Recursive — a merged stream is itself a stream.
    Every link is a DERIVED stream OWNED by the subscription, so `Close` unwinds
    the whole chain off the ONE bracket the subscribe registers and each plain
    source is left to its own."""
    if isinstance(node, dict) and node.get("kind") == "stream-merge":
        args = ", ".join(_stream_head(src, env)
                         for src in node.get("sources") or [])
        return "StreamMerge(%s)" % args
    return _expr(node, env)


def _is_stream_next(expr) -> bool:
    """True for `<sub>.next()` where `<sub>` is a `subscribe` bracket's bind
    (item 130). The bind table already records the acquisition's Go type, so
    this recognises a subscription's suspension verb without re-walking the IR."""
    if not isinstance(expr, dict) or expr.get("kind") != "call":
        return False
    if expr.get("method") != "next":
        return False
    target = expr.get("target") or {}
    name = target.get("id") if target.get("kind") == "name" else None
    return name is not None and _BIND_HOST.get(name) == "Subscription"


def _host_type_of_acquire(acquire):
    if acquire.get("kind") == "host":
        recv = acquire["fn"].split(".")[0]
        name = _camel(recv)
        # placement mode renames the host-runtime type when a declared record
        # collides (see _host_runtime); the impl struct field must agree.
        if _V3_TYPED_COMPONENTS and name in _V3_TYPES:
            return "Revl" + name
        return name
    if acquire.get("kind") == "spawn":
        # A `spawn` acquisition binds a live instance handle, not a host object.
        return "RevlSpawnHandle"
    return "any"


def _emit_component_step(comp, step, services, env: _Env, out, indent=3):
    s = step.get("step")
    cname = _camel(comp["name"])
    pad = "\t" * indent
    inner = pad + "\t"
    if s == "let-effect":
        bind = step["bind"]
        wit = _witnessed_extern(step.get("acquire"))
        if wit is not None:
            # item 243: a witnessed acquisition registers a TRANSACTIONAL
            # entry (not a bracket) — a dedicated codegen path, since its
            # result type is the extern's own Result[W, E], not the generic
            # host/spawn type `_host_of_bind` assumes.
            _emit_witnessed_step(out, pad, step, wit, env, bind=_bind_field(bind))
        else:
            # item 113: use the generic host type learned in _emit_component
            # (`Map[int64]`, `RevlMap[Msg]`, …), not the bare base name.
            host = _host_of_bind(bind)
            env.map_new_value = _BIND_MAP_VALUE.get(bind)
            acquire = _expr(step["acquire"], env)
            env.map_new_value = None
            undo = step.get("undo")
            out.append("%svar %s %s%s" % (pad, _bind_field(bind), _bind_star(bind), host))
            out.append("%sif err := ctx.Effect(func() stc.Inverse {" % pad)
            # A block-effect setup (`effect { let k = 1  Map.new() } undo …`)
            # runs its statements before the acquire, inside the effect closure.
            for setup_step in step.get("setup") or []:
                _emit_method_body([setup_step], env, out, indent + 1)
            out.append("%s%s = %s" % (inner, _bind_field(bind), acquire))
            if undo is not None and _is_map_cas(step["acquire"]):
                # item 397: result-guarded undo — identity inverse on a `false`
                # CAS, so teardown never removes the winner's entry.
                out.append("%sreturn func() error { if %s { %s }; return nil }"
                           % (inner, _bind_field(bind), _expr(undo, env)))
            elif undo is not None:
                out.append("%sreturn func() error { %s; return nil }" % (inner, _expr(undo, env)))
            else:
                out.append("%sreturn nil" % inner)
            out.append("%s}); err != nil {" % pad)
            out.append("%sreturn nil, err" % inner)
            out.append("%s}" % pad)
    elif s == "effect":
        wit = _witnessed_extern(step.get("acquire"))
        if wit is not None:
            _emit_witnessed_step(out, pad, step, wit, env, bind=None)
        else:
            # bare effect step at component top level
            acquire = _expr(step["acquire"], env)
            undo = step.get("undo")
            out.append("%sif err := ctx.Effect(func() stc.Inverse {" % pad)
            out.append("%s%s" % (inner, acquire))
            if undo is not None:
                out.append("%sreturn func() error { %s; return nil }" % (inner, _expr(undo, env)))
            else:
                out.append("%sreturn nil" % inner)
            out.append("%s}); err != nil {" % pad)
            out.append("%sreturn nil, err" % inner)
            out.append("%s}" % pad)
    elif s == "await":
        # cordis-go's Apply runs synchronously; an awaited host call is just a
        # blocking call whose result is discarded (the A1 ordering boundary is
        # the statement position, preserved here).
        if _is_stream_next(step.get("expr")):
            # item 130: `await sub.next()` on this tier is the blocking
            # cancel-channel `select` (design §4.6, the go row). Two of its three
            # outcomes are terminals, and they are NOT the same: `Closed` (an
            # orderly provider close, or the owner's own `close` tripping the
            # cancel channel) is an ordinary value the activation carries on
            # from, while `Faulted` (a provider abort, or a bounded-buffer
            # overflow under the default `error` policy) FAILS the activation —
            # so the accumulated prefix, subscription bracket included, reverts
            # LIFO and the stream is closed. Never a silent drop of either.
            out.append("%sif _, err := %s; err != nil {"
                       % (pad, _expr(step["expr"], env)))
            out.append("%sreturn nil, err" % inner)
            out.append("%s}" % pad)
        else:
            out.append("%s%s" % (pad, _expr(step["expr"], env)))
    elif s == "emit":
        # `emit X compensate Y` (item 247, docs/design/teardown-contract.md):
        # perform the emission; the compensation is a `compensation` entry,
        # not a bracket. On a clean commit it is DISCHARGED — never run, the
        # forward emission was the deliverable (a5a). On an abort it does NOT
        # run inline: it is ENQUEUED onto the frame's Phase-2 queue and runs
        # only after every bracket/transactional inverse has replayed
        # (Phase 1 first, in full, no matter what Phase 2 later does) —
        # `runCompensationPhase` drains the queue, best-effort and bounded,
        # via the goroutine-abandon pattern (go's per-tier obligation).
        emit_call = _expr(step["expr"], env)
        comp_node = step.get("compensate")
        if comp_node is not None:
            compensate_call = _expr(comp_node, env)
            key, method = _call_descriptor(comp_node)
            out.append("%sif err := ctx.Effect(func() stc.Inverse {" % pad)
            out.append("%s%s" % (inner, emit_call))
            out.append("%sreturn func() error {" % inner)
            out.append("%s\tif _revlFrame.committed {" % inner)
            out.append("%s\t\treturn nil // item 247 a5a: discharge — never runs" % inner)
            out.append("%s\t}" % inner)
            out.append("%s\t_revlFrame.enqueue(%s, %s, func() error { %s; return nil })" %
                       (inner, _go_string(key), _go_string(method), compensate_call))
            out.append("%s\treturn nil" % inner)
            out.append("%s}" % inner)
            out.append("%s}); err != nil {" % pad)
            out.append("%sreturn nil, err" % inner)
            out.append("%s}" % pad)
            global _COMP_NEEDS_TEARDOWN
            _COMP_NEEDS_TEARDOWN = True
        else:
            out.append("%s%s" % (pad, emit_call))
    elif s == "timer":
        # A `timer` step (item 57): a revertible schedule. Arming the timer is
        # the acquire, cancellation its derived inverse — wired into the SAME
        # `ctx.Effect` ledger (LIFO) that reverts a Pool or a provision, so
        # unloading the component provably cancels the timer with no orphaned
        # interval (residue-free, the leak the residue probe hunts cannot
        # occur). The clock does not advance on its own: RevlClockAdvance drives
        # it, so a firing is a deterministic timeline step, not a wall-clock
        # race (docs/time-coeffect.md). The firing closure holds the timer
        # body's emissions and runs at activation-time stratum with the
        # component's declared capabilities (each `emit` lowers through the same
        # path a top-level emission does, so G4/G8 reach is audited).
        global _COMP_NEEDS_TIMER, _TIMER_COUNTER
        _COMP_NEEDS_TIMER = True
        mode = step.get("mode")
        schedule = "revlScheduleEvery" if mode == "every" else "revlScheduleAfter"
        interval = int(step.get("interval_ms"))
        _TIMER_COUNTER += 1
        handle = "_revlTimer%d" % _TIMER_COUNTER
        emissions = step.get("body") or []
        out.append("%svar %s *RevlTimer" % (pad, handle))
        out.append("%sif err := ctx.Effect(func() stc.Inverse {" % pad)
        out.append("%s%s = %s(%d, func() {" % (inner, handle, schedule, interval))
        for em in emissions:
            if em.get("step") != "emit":  # lowerer invariant (scope: emissions)
                raise EmitError("timer body carries emissions only, found %r"
                                % (em.get("step"),))
            out.append("%s\t%s" % (inner, _expr(em.get("expr"), env)))
        out.append("%s})" % inner)
        # the derived inverse: cancellation, yielded into the disposer stack.
        out.append("%sreturn func() error { %s.Cancel(); return nil }" % (inner, handle))
        out.append("%s}); err != nil {" % pad)
        out.append("%sreturn nil, err" % inner)
        out.append("%s}" % pad)
    elif s == "if":
        out.append("%sif %s {" % (pad, _expr(step.get("cond"), env)))
        for sub in step.get("then") or []:
            _emit_component_step(comp, sub, services, env, out, indent + 1)
        if step.get("else"):
            out.append("%s} else {" % pad)
            for sub in step["else"]:
                _emit_component_step(comp, sub, services, env, out, indent + 1)
        out.append("%s}" % pad)
    elif s == "fail":
        out.append("%sreturn nil, fmt.Errorf(%s)" % (pad, _expr(step.get("message"), env)))
    elif s == "provide":
        pname = step["name"]
        svc = step["service"]
        struct = "%s_%s" % (cname, pname)
        # resolve method return types from service decl for the impl emit
        for m in step.get("methods", []):
            decl = services.get(svc, {}).get("methods", {}).get(m["name"], {})
            m["_ret"] = decl.get("returns")
        # build the impl value, wiring ctx + config + binds + reqs
        fields = ["ctx: ctx"]
        # item 318: hand the frame to a provide block whose method registers a
        # witnessed effect (the `_revlFrame` local exists — a method-witnessed
        # component always needs the frame, see `_body_needs_frame`).
        if _provide_has_method_frame(step):
            fields.append("revlFrame: _revlFrame")
        if comp.get("config"):
            fields.append("cfg: cfg")
        for b in [x["bind"] for x in comp.get("body", []) if x.get("step") == "let-effect"]:
            fields.append("%s: %s" % (_bind_field(b), _bind_field(b)))
        for r in (comp.get("requires", {}) or {}).keys():
            fields.append("%s: %s" % (_req_field(r), _req_field(r)))
        out.append("%s_impl%s := &%s{%s}" % (pad, _camel(pname), struct, ", ".join(fields)))
        out.append("%sif _, err := ctx.Provide(%s, %s(_impl%s)); err != nil {" %
                   (pad, _key_var(pname), _camel(svc), _camel(pname)))
        out.append("%sreturn nil, err" % inner)
        out.append("%s}" % pad)
    elif s == "intercept":
        # handled at load site (metadata); no-op in Apply
        pass
    else:
        raise EmitError("unsupported component step: %r" % (s,))


# --------------------------------------------------------------------------
# keys, realm helper, load helpers
# --------------------------------------------------------------------------

def _emit_keys(ir, out):
    seen = {}
    for comp in ir.get("components", []):
        for name, svc in (comp.get("provides", {}) or {}).items():
            seen[name] = svc
        for name, svc in (comp.get("requires", {}) or {}).items():
            seen.setdefault(name, svc)
    for name, svc in seen.items():
        out.append("var %s = stc.NewKey[%s](%s)" % (_key_var(name), _camel(svc), _go_string(name)))
    if seen:
        out.append("")


def _emit_realm_helper(ir, out):
    # emit only if any component isolates a key or routes one (item 173: a
    # router resolves its worker realms by label through this same interner).
    if not any(c.get("isolate") or c.get("routes") for c in ir.get("components", [])):
        return
    out.append("var (")
    out.append("\t_revlRealmMu sync.Mutex")
    out.append("\t_revlRealmBy = map[string]*stc.Realm{}")
    out.append(")")
    out.append("")
    out.append("// %s interns a realm by name under the root realm so every load" % _realm_helper_name())
    out.append("// site naming the same realm shares the same *stc.Realm (provKey is")
    out.append("// keyed by pointer, not name).")
    out.append("func %s(name string) *stc.Realm {" % _realm_helper_name())
    out.append("\t_revlRealmMu.Lock()")
    out.append("\tdefer _revlRealmMu.Unlock()")
    out.append("\tif r, ok := _revlRealmBy[name]; ok {")
    out.append("\t\treturn r")
    out.append("\t}")
    out.append("\tr := stc.NewRealm(stc.RootRealm(), name)")
    out.append("\t_revlRealmBy[name] = r")
    out.append("\treturn r")
    out.append("}")
    out.append("")


def _emit_load_helpers(ir, out):
    for comp in ir.get("components", []):
        name = comp["name"]
        cname = _camel(name)
        isolate = comp.get("isolate") or {}
        intercept = comp.get("intercept") or {}
        has_config = bool(comp.get("config"))
        if has_config:
            out.append("// Load%s isolates the load-target per the component's realm"
                       " placement, then loads it." % cname)
            out.append("func Load%s(target *stc.Context, cfg %sConfig) *stc.Fiber {" % (cname, cname))
        else:
            out.append("func Load%s(target *stc.Context) *stc.Fiber {" % cname)
        if isolate or intercept:
            out.append("\tctx := target.Child()")
            for key, realm in isolate.items():
                out.append("\tctx.Isolate(%s, %s(%s))" %
                           (_key_var(key), _realm_helper_name(), _go_string(realm)))
            for key, meta in intercept.items():
                out.append("\tctx.Intercept(%s, %s)" % (_key_var(key), _go_literal(meta)))
            target_ctx = "ctx"
        else:
            target_ctx = "target"
        if has_config:
            out.append("\treturn %s.Load(%s(cfg))" % (target_ctx, cname))
        else:
            out.append("\treturn %s.Load(%s())" % (target_ctx, cname))
        out.append("}")
        out.append("")




def _go_lifecycle_arg(node, payload_surface, env):
    """An Opt payload argument rendered in the payload's Go type.

    `revlEq` compares through `any`, and Go's untyped-constant rule would turn
    a bare `4` into `int` while the Opt payload is `int64` (v3) / `int32`
    (Int32) — DeepEqual would then say the values differ. Pin the literal to
    the payload's Go type instead of letting the `any` context infer it."""
    if isinstance(node, dict) and node.get("kind") == "lit":
        value = node.get("value")
        if isinstance(value, int) and payload_surface in ("Int", "Int32"):
            return "%s(%s)" % (_go_type(payload_surface), value)
        if isinstance(value, float) and payload_surface == "Float":
            return "float64(%s)" % repr(value)
    return _expr(node, env, payload_surface)


def _go_lifecycle_assert(expr, bind_types, env, where):
    """A Go bool expression for a lifecycle `assert` step.

    The cordis-go tier carries Opt only in return position (`(T, bool)`), so a
    `bind == Some(..)` / `bind == None` comparison over an Opt-typed lifecycle
    binding is lowered against the tuple explicitly — `revlEq` on the value
    plus the presence bit. Plain scalar/structural asserts lower through the
    ordinary component expression renderer. Anything else (an Opt-typed
    binding used outside this shape) refuses loudly: the tier cannot express
    it, and a silently-wrong assertion is worse than none."""
    if expr.get("kind") == "var" and bind_types.get(expr.get("name"), "").startswith("Opt["):
        raise EmitError(
            f"{where}: an Opt-typed lifecycle binding can only be asserted "
            f"against `Some(..)` or `None` on the cordis-go tier (Opt is "
            f"return-position only); got {expr!r}")
    if expr.get("kind") != "bin" or expr.get("op") not in ("==", "!=", "===", "!=="):
        return _expr(expr, env)
    op = expr.get("op")
    left, right = expr.get("left"), expr.get("right")
    negate = op in ("!=", "!==")
    if left.get("kind") == "var":
        name = left.get("name")
        surface = bind_types.get(name, "")
        if surface.startswith("Opt["):
            go_bind = _safe_local(name)
            payload = surface[4:-1]
            # `Some(..)` is a call; `None` lowers to a bare var
            if right.get("kind") == "call":
                callee = right.get("callee") or {}
                cname = callee.get("name")
                if cname == "Some":
                    args = right.get("args") or []
                    if len(args) != 1:
                        raise EmitError(f"{where}: `Some(..)` takes exactly one argument")
                    arg = _go_lifecycle_arg(args[0], payload, env)
                    inner = "(%s.ok && revlEq(%s.value, %s))" % (go_bind, go_bind, arg)
                    return "(!(%s))" % inner if negate else inner
            elif right.get("kind") == "var" and right.get("name") == "None":
                inner = "(!%s.ok)" % go_bind
                return "(!(%s))" % inner if negate else inner
            raise EmitError(
                f"{where}: an Opt-typed lifecycle binding can only be asserted "
                f"against `Some(..)` or `None` on the cordis-go tier (Opt is "
                f"return-position only); got {expr!r}")
    return _expr(expr, env)


def _component_config_fields(name, components):
    for comp in components:
        if comp["name"] == name:
            return [(f.get("name"), f.get("type")) for f in (comp.get("config") or [])]
    return []


def _component_has_config(name, components):
    for comp in components:
        if comp["name"] == name:
            return bool(comp.get("config"))
    return False


def _emit_stc_lifecycle_tests(ir, out) -> None:
    """`lifecycle test` blocks (syntax-2.0 §7.1) as `func TestXxx(t *testing.T)`
    driving the live stc-go runtime (FR-5).

    A lifecycle test is a script over a *live* composition: load components
    into a fresh ``stc.Context`` via the generated ``LoadX`` helpers, call
    through provision keys with typed ``stc.Service`` resolutions (the exact
    read the placement runner's probes use), unload them LIFO, and assert the
    runtime holds nothing. ``assert no_residue`` proves R4 with the registry
    mirror of the ``registry().len() == 0`` shape
    (``len(root.Fibers()) == 0``) and R1 with the host runtime's live-resource
    counter, matching the py reference tier's pairing.
    """
    tests = [t for t in (ir.get("tests") or []) if t.get("lifecycle")]
    if not tests:
        return
    components = ir.get("components") or []
    services = ir.get("services") or {}
    if not services:
        raise EmitError(
            "a lifecycle test loads components and calls through provision "
            "keys, so it needs at least one service in the document to drive; "
            "this document declares none"
        )
    provided: dict[str, str] = {}
    for comp in components:
        for key, svc in (comp.get("provides") or {}).items():
            provided[key] = svc
    method_tables = {sname: (svc.get("methods") or {})
                     for sname, svc in services.items()}
    out.append("// ---- lifecycle tests (docs/syntax-2.0.md §7.1) ----------------")
    out.append("// A `lifecycle test` drives the composition on a live stc-go")
    out.append("// context: load components, call through provision keys, unload")
    out.append("// LIFO, and assert no residue (R4 registry + R1 host resources).")
    out.append("type revlOptPair[T any] struct {")
    out.append("\tvalue T")
    out.append("\tok    bool")
    out.append("}")
    out.append("")
    out.append("func revlEq(a, b any) bool { return reflect.DeepEqual(a, b) }")
    out.append("")
    used: set = set()
    for test in tests:
        tname = _go_v3_test_name(test.get("name") or "lifecycle", used)
        where = "lifecycle test %s" % _go_string(test["name"])
        env = _Env([], [], [])
        bind_types: dict[str, str] = {}
        out.append("func %s(revlT *testing.T) {" % tname)
        out.append("\troot := stc.New()")
        out.append("\t_fibers := map[string]*stc.Fiber{}")
        # item 102: the clock coeffect is package-global; reset it so this
        # test's `advance` steps start from t=0 and see only its own timers,
        # independent of any earlier lifecycle test in the file (mirrors the
        # py/ts tiers' Clock/clockReset at test start).
        if any(s.get("step") == "advance" for s in test.get("body") or []):
            out.append("\tRevlClockReset()")
        for step in test.get("body") or []:
            kind = step.get("step")
            if kind == "load":
                component = step["component"]
                cname = _camel(component)
                cfg = step.get("config") or {}
                fields = ", ".join(
                    "%s: %s" % (_camel(fname), _expr(cfg[fname], env, ftype))
                    for fname, ftype in _component_config_fields(component, components)
                    if fname in cfg)
                if _component_has_config(component, components):
                    sig = "Load%s(root, %sConfig{%s})" % (cname, cname, fields)
                else:
                    sig = "Load%s(root)" % cname
                out.append("\t{")
                out.append("\t\t_f := %s" % sig)
                out.append("\t\tif err := _f.Ready(stdctx.Background()); err != nil {")
                out.append('\t\t\trevlT.Fatalf("%%s: %%v", %s, err)'
                           % _go_string(where + ": load " + component))
                out.append("\t\t}")
                out.append("\t\t_fibers[%s] = _f" % _go_string(component))
                out.append("\t}")
            elif kind == "unload":
                component = step["component"]
                out.append("\tif _f, _ok := _fibers[%s]; _ok {" % _go_string(component))
                out.append("\t\t_f.Dispose()")
                out.append("\t\t// disposal is orchestrated asynchronously; a")
                out.append("\t\t// reload must not collide with the old fiber's")
                out.append("\t\t// provisions, so wait for it to be Gone")
                out.append("\t\tfor i := 0; i < 200 && _f.State() != stc.StateGone; i++ {")
                out.append("\t\t\ttime.Sleep(5 * time.Millisecond)")
                out.append("\t\t}")
                out.append("\t\tdelete(_fibers, %s)" % _go_string(component))
                out.append("\t}")
            elif kind == "call":
                key = step["key"]
                service = provided.get(key)
                if service is None:  # pragma: no cover — the lowerer rejects it
                    raise EmitError(f"{where}: no provider for key {key!r}")
                method = (method_tables.get(service) or {}).get(step["method"])
                if method is None:  # pragma: no cover — the lowerer rejects it
                    raise EmitError(f"{where}: unknown method {step['method']!r}")
                bind = step.get("bind")
                ret_surface = method.get("returns")
                params = method.get("params") or []
                args = ", ".join(
                    _expr(a, env, params[i].get("type") if i < len(params) else None)
                    for i, a in enumerate(step.get("args") or []))
                mcall = "(_svc).%s(%s)" % (_camel(step["method"]), args)
                if bind is not None and str(ret_surface or "").startswith("Opt["):
                    payload = ret_surface[4:-1]
                    go_payload = _go_type(payload)
                    bind_types[bind] = ret_surface
                    env.var_types[bind] = ret_surface
                    # the binding outlives the resolution block, so it is
                    # declared before it (a revl binding is test-scoped)
                    out.append("\tvar %s revlOptPair[%s]" % (_safe_local(bind), go_payload))
                    out.append("\t{")
                    out.append("\t\t_svc, _err := stc.Service[%s](root, %s)" %
                               (_camel(service), _key_var(key)))
                    out.append("\t\tif _err != nil {")
                    out.append('\t\t\trevlT.Fatalf("%%s: %%v", %s, _err)'
                               % _go_string(where + ": " + key + " is ACTIVE (R2)"))
                    out.append("\t\t}")
                    out.append("\t\t_r, _ok := %s" % mcall)
                    out.append("\t\t%s = revlOptPair[%s]{value: _r, ok: _ok}"
                               % (_safe_local(bind), go_payload))
                    out.append("\t}")
                elif bind is not None:
                    bind_types[bind] = ret_surface or ""
                    env.var_types[bind] = ret_surface
                    go_bind = _go_type(ret_surface) if ret_surface else ""
                    out.append("\tvar %s %s" % (_safe_local(bind), go_bind))
                    out.append("\t{")
                    out.append("\t\t_svc, _err := stc.Service[%s](root, %s)" %
                               (_camel(service), _key_var(key)))
                    out.append("\t\tif _err != nil {")
                    out.append('\t\t\trevlT.Fatalf("%%s: %%v", %s, _err)'
                               % _go_string(where + ": " + key + " is ACTIVE (R2)"))
                    out.append("\t\t}")
                    out.append("\t\t%s = %s" % (_safe_local(bind), mcall))
                    out.append("\t}")
                else:
                    out.append("\t{")
                    out.append("\t\t_svc, _err := stc.Service[%s](root, %s)" %
                               (_camel(service), _key_var(key)))
                    out.append("\t\tif _err != nil {")
                    out.append('\t\t\trevlT.Fatalf("%%s: %%v", %s, _err)'
                               % _go_string(where + ": " + key + " is ACTIVE (R2)"))
                    out.append("\t\t}")
                    if ret_surface:
                        out.append("\t\t_ = %s" % mcall)
                    else:
                        out.append("\t\t%s" % mcall)
                    out.append("\t}")
            elif kind == "assert":
                out.append("\tif !(%s) {" % _go_lifecycle_assert(
                    step["expr"], bind_types, env, where))
                out.append("\t\trevlT.Fatalf(%s)" % _go_string(where + ": assertion failed"))
                out.append("\t}")
            elif kind == "assert_no_residue":
                out.append("\t// R4 + R1: the composition must leave the live")
                out.append("\t// runtime holding nothing — the orchestrator reaps")
                out.append("\t// disposed fibers asynchronously, so poll briefly.")
                out.append("\tfor i := 0; i < 200; i++ {")
                out.append("\t\tif len(root.Fibers()) == 0 && revlHostLive() == 0 {")
                out.append("\t\t\tbreak")
                out.append("\t\t}")
                out.append("\t\ttime.Sleep(5 * time.Millisecond)")
                out.append("\t}")
                out.append("\tif !(len(root.Fibers()) == 0 && revlHostLive() == 0) {")
                out.append("\t\trevlT.Fatalf(%s, len(root.Fibers()), revlHostLive())"
                           % _go_string(where + ": residue — %d fiber(s), %d host resource(s) (R4/R1)"))
                out.append("\t}")
            elif kind == "advance":
                # item 102: drive the clock coeffect forward (go has the same
                # deterministic Clock as py/ts). A firing is a timeline step, so
                # any due timer bodies run synchronously inside RevlClockAdvance
                # before the next statement observes their effect
                # (docs/time-coeffect.md §advance).
                out.append("\tRevlClockAdvance(%d)" % int(step["ms"]))
            else:  # pragma: no cover — the lowerer emits nothing else
                raise EmitError(f"{where}: unknown lifecycle step {kind!r}")
        out.append("}")
        out.append("")

# --------------------------------------------------------------------------
# instance-parametric spawn (docs/design-v2-instances.md, phase 1)
# --------------------------------------------------------------------------

def _iter_spawn_nodes(ir):
    """Yield every `spawn` acquire node in the document (a `let-effect` step's
    acquire whose kind is "spawn")."""
    for comp in ir.get("components", []):
        for step in comp.get("body", []) or []:
            if step.get("step") == "let-effect":
                acq = step.get("acquire") or {}
                if acq.get("kind") == "spawn":
                    yield acq


def _uses_spawn(ir) -> bool:
    return next(_iter_spawn_nodes(ir), None) is not None


def _spawn_targets(ir):
    """Ordered map target-name -> {"has_config", "realm_keys"} for every
    component spawned in the document. `realm_keys` is the sorted list of keys
    the target provides, each isolated into a fresh local realm at spawn time
    (carried on the spawn node as `realms`)."""
    by_name = {c["name"]: c for c in ir.get("components", [])}
    targets: dict[str, dict] = {}
    for acq in _iter_spawn_nodes(ir):
        name = acq.get("component")
        if name in targets:
            continue
        decl = by_name.get(name, {})
        targets[name] = {
            "has_config": bool(decl.get("config")),
            "realm_keys": list(acq.get("realms") or []),
        }
    return targets


def _emit_spawn_support(ir, out):
    """Emit the `RevlSpawnHandle` type and one `revlSpawn<Target>` helper per
    spawned component. Emitted only when the document spawns, so non-spawning
    programs stay byte-identical."""
    if not _uses_spawn(ir):
        return
    out.append("// ---- instance-parametric spawn (docs/design-v2-instances.md) ----------")
    out.append("// A live spawned-component instance. The instance is a CHILD FIBER of its")
    out.append("// spawner — its own nested teardown scope. Dispose() unloads that fiber,")
    out.append("// running the instance's LIFO teardown NOW, independent of the spawner, so")
    out.append("// a request-scoped instance is reclaimed when the request ends and is never")
    out.append("// deferred to the component's teardown. Dispose is idempotent (the fiber is")
    out.append("// taken once), so the spawner's own undo — w.dispose() — is a harmless no-op")
    out.append("// once the instance is already gone: an un-disposed instance still cannot")
    out.append("// outlive its spawner, but a disposed one is reclaimed early.")
    out.append("type RevlSpawnHandle struct {")
    out.append("\tmu    sync.Mutex")
    out.append("\tfiber *stc.Fiber")
    out.append("\tctx   *stc.Context")
    out.append("}")
    out.append("")
    out.append("func newRevlSpawnHandle(fiber *stc.Fiber, ctx *stc.Context) *RevlSpawnHandle {")
    out.append("\treturn &RevlSpawnHandle{fiber: fiber, ctx: ctx}")
    out.append("}")
    out.append("")
    out.append("// Dispose unloads the instance's fiber (its LIFO teardown). Idempotent.")
    out.append("func (h *RevlSpawnHandle) Dispose() error {")
    out.append("\th.mu.Lock()")
    out.append("\tf := h.fiber")
    out.append("\th.fiber = nil")
    out.append("\th.mu.Unlock()")
    out.append("\tif f != nil {")
    out.append("\t\tf.Dispose()")
    out.append("\t}")
    out.append("\treturn nil")
    out.append("}")
    out.append("")
    out.append("// Ctx exposes the instance's own isolated context, so its spawner (the sole")
    out.append("// holder of this handle) can resolve a provision the instance published in")
    out.append("// its private local realm. A sibling, isolated into a different local realm,")
    out.append("// cannot reach it — supervision-tree addressing.")
    out.append("func (h *RevlSpawnHandle) Ctx() *stc.Context {")
    out.append("\th.mu.Lock()")
    out.append("\tdefer h.mu.Unlock()")
    out.append("\t// the instance's provision lives in the FIBER's live context (set at")
    out.append("\t// async load, replaced on inertial reload), not the pre-load child ctx")
    out.append("\t// the handle was constructed with — resolve through the fiber so the")
    out.append("\t// accessor reads the instance's own realm, exactly like the py")
    out.append("\t// reference's SpawnHandle.get (docs/design-v2-instances.md).")
    out.append("\tif h.fiber != nil {")
    out.append("\t\tif c := h.fiber.Context(); c != nil {")
    out.append("\t\t\treturn c")
    out.append("\t\t}")
    out.append("\t}")
    out.append("\treturn h.ctx")
    out.append("}")
    out.append("")
    out.append("// Fiber exposes the live fiber (nil once disposed) for lifecycle assertions.")
    out.append("func (h *RevlSpawnHandle) Fiber() *stc.Fiber {")
    out.append("\th.mu.Lock()")
    out.append("\tdefer h.mu.Unlock()")
    out.append("\treturn h.fiber")
    out.append("}")
    out.append("")
    for name, info in _spawn_targets(ir).items():
        cname = _camel(name)
        keys = info["realm_keys"]
        if info["has_config"]:
            sig = "func revlSpawn%s(parent *stc.Context, cfg %sConfig) *RevlSpawnHandle {" % (cname, cname)
        else:
            sig = "func revlSpawn%s(parent *stc.Context) *RevlSpawnHandle {" % cname
        out.append("// revlSpawn%s plugs a fresh %s instance as a CHILD FIBER of the" % (cname, cname))
        out.append("// spawner, each provided key isolated into its OWN fresh local realm — a")
        out.append("// distinct *stc.Realm per call, so two instances never collide on a")
        out.append("// provision (disjoint by construction, no label). The handle it returns")
        out.append("// reclaims exactly this instance on Dispose().")
        out.append(sig)
        out.append("\tchild := parent.Child()")
        for key in keys:
            out.append("\tchild.Isolate(%s, stc.NewRealm(stc.RootRealm(), %s))" %
                       (_key_var(key), _go_string("spawn:%s:%s" % (name, key))))
        if info["has_config"]:
            out.append("\tfiber := child.Load(%s(cfg))" % cname)
        else:
            out.append("\tfiber := child.Load(%s())" % cname)
        out.append("\t// stc-go loads asynchronously: the instance is 'live' (its provisions")
        out.append("\t// resolvable) only once its fiber is Active. Wait for that here so the")
        out.append("\t// handle the spawner binds is a live instance — the accessor's")
        out.append("\t// s.<key>.method() reads through it without racing the async load")
        out.append("\t// (matches the reference tier's synchronous spawn semantics).")
        out.append("\tif err := fiber.Ready(stdctx.Background()); err != nil {")
        out.append("\t\tpanic(\"revl: spawned instance \" + %s + \" failed to activate: \" + err.Error())" % _go_string(name))
        out.append("\t}")
        out.append("\treturn newRevlSpawnHandle(fiber, child)")
        out.append("}")
        out.append("")


def _go_literal(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return _go_string(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, list):
        return "[]any{%s}" % ", ".join(_go_literal(x) for x in v)
    if isinstance(v, dict):
        return "map[string]any{%s}" % ", ".join(
            "%s: %s" % (_go_string(k), _go_literal(val)) for k, val in v.items())
    return "nil"


# --------------------------------------------------------------------------
# module header + host runtime
# --------------------------------------------------------------------------

def _host_runtime() -> str:
    """The emitted host runtime, with the Int width matched to the mode.

    ir_version 1/2 components keep `int` (the frozen scenarios, byte-for-byte);
    a v3-mode document converges on `int64` so the host Pool's Int positions
    type-check against v3 component bodies (whose Int is int64). Before this,
    a v3 document whose component opens a Pool or executes a statement failed
    to compile: `PoolOpen(config.url, config.pool_size)` passed an int64 into
    an `int` parameter (docs/conformance.md; surfaced by FR-5's lifecycle
    tests on the go tier).

    In v3 PLACEMENT mode, a declared record whose name collides with a legacy
    host-runtime type (Row/Map/Pool) renames the host side to `Revl<Name>`;
    the component codegen resolves the same host names through
    `_host_type_of_acquire`, so both sides agree (the collision is the go
    mirror of the rename java applies to a `double` function, docs/backend-
    ir-v3.md §v3 — a type name the host already owns)."""
    int_ty = "int64" if _V3_MODE else "int"
    src = _HOST_RUNTIME.replace("@INT@", int_ty)
    if _V3_TYPED_COMPONENTS:
        for name in _HOST_RUNTIME_RENAMES:
            if name in _V3_TYPES:
                src = re.sub(r"\b%s\b" % name, "Revl" + name, src)
    return src


def _needs_sync(ir) -> bool:
    return any(c.get("isolate") for c in ir.get("components", [])) or True


# Legacy host-runtime type names a v3 typed-core document may declare as a
# record. Placement mode renames the HOST side to `Revl<Name>` so the declared
# record struct keeps its name (see _host_runtime / _host_type_of_acquire).
_HOST_RUNTIME_RENAMES = ("Row", "Map", "Pool")


_HOST_RUNTIME = r'''// ---- host runtime (minimal, recording) --------------------------------

// R1 live-resource accounting (docs/backend-ir.md §Required semantics, the
// same pairing the py reference tier's `assert no_residue` checks): every host
// object acquired must be released by its `undo`, or the lifecycle
// `assert no_residue` fails. The counter is package-wide, so it is per-test
// and cross-test safe: a clean test returns to zero.
var _revlLiveMu sync.Mutex
var _revlLiveHostResources int

func revlHostAcquire() { _revlLiveMu.Lock(); _revlLiveHostResources++; _revlLiveMu.Unlock() }
func revlHostRelease() { _revlLiveMu.Lock(); _revlLiveHostResources--; _revlLiveMu.Unlock() }
func revlHostLive() int {
	_revlLiveMu.Lock()
	defer _revlLiveMu.Unlock()
	return _revlLiveHostResources
}
// A deterministic in-memory stand-in for revl host objects, instrumented so
// scenarios can assert the exact effect/undo order of emitted code.

var _hostMu sync.Mutex
var _hostLog []string

func hostRecord(op string) {
	_hostMu.Lock()
	_hostLog = append(_hostLog, op)
	_hostMu.Unlock()
}

// HostMarks returns an ordered snapshot of host operations.
func HostMarks() []string {
	_hostMu.Lock()
	defer _hostMu.Unlock()
	out := make([]string, len(_hostLog))
	copy(out, _hostLog)
	return out
}

// HostReset clears the host op log (call between scenarios).
func HostReset() {
	_hostMu.Lock()
	_hostLog = nil
	_hostMu.Unlock()
}

// Row is a query result row.
type Row = map[string]string

// Pool is a deterministic in-memory connection pool.
type Pool struct {
	url  string
	size @INT@
}

func PoolOpen(url string, size @INT@) *Pool {
	hostRecord("pool.open")
	revlHostAcquire()
	return &Pool{url: url, size: size}
}
func (p *Pool) Close()               { hostRecord("pool.close"); revlHostRelease() }
func (p *Pool) Query(sql string) []Row {
	hostRecord("pool.query:" + sql)
	return nil
}
func (p *Pool) Execute(sql string) @INT@ {
	hostRecord("pool.execute:" + sql)
	return 0
}

// Map is a thread-safe map with Str keys. The value type is generic — each
// site's `Map.new()` pins `V` from how the map is used (FR-4: a revl
// `Map[Str, Int]` counter or `Map[Str, List[Msg]]` ledger, not only String),
// mirroring backends/rust/emit.py's `struct Map<V>`. Emit instantiates it at
// the acquisition (`MapNew[int64]()`), so the boundary carries the declared
// value type and Insert/Get type-check against the component's real values.
type Map[V any] struct {
	mu sync.Mutex
	m  map[string]V
}

func MapNew[V any]() *Map[V] {
	hostRecord("map.new")
	revlHostAcquire()
	return &Map[V]{m: map[string]V{}}
}
func (m *Map[V]) Drop() { hostRecord("map.drop"); revlHostRelease() }
func (m *Map[V]) Insert(k string, v V) {
	m.mu.Lock()
	m.m[k] = v
	m.mu.Unlock()
}

// InsertIfAbsent is the atomic compare-and-set (item 397). The per-op mutex is
// held across BOTH the membership test AND the insert, so the whole CAS is one
// critical section: no concurrent caller can witness the probe and the write as
// separable steps. Returns whether it inserted; a false (key already present)
// leaves the existing value untouched. Under N concurrent callers on one map,
// exactly one receives true.
func (m *Map[V]) InsertIfAbsent(k string, v V) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.m[k]; ok {
		return false
	}
	m.m[k] = v
	return true
}
func (m *Map[V]) Remove(k string) {
	m.mu.Lock()
	delete(m.m, k)
	m.mu.Unlock()
}
func (m *Map[V]) Get(k string) (V, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	v, ok := m.m[k]
	return v, ok
}

// Iteration surface (docs/stdlib-2.0.md §Map): the checker promises
// `size()`/`keys()` on a host `Map.new()` receiver too, and emit lowers both
// as method calls on this object. `Size` is the entry count as the tier's
// revl Int (@INT@, matching the service-method return type); `Keys`
// yields the keys in ascending canonical Str order (UTF-8 byte lexicographic —
// go string < is exactly code-point order, and slices.Sort on []string orders
// by <, so the order is identical to the insertion sort this replaced, as it
// is in revlMapKeys). keys() IS the Map iteration surface, so this sits on
// every map traversal: O(n log n), not O(n^2) (item 434 (h)). Both are
// read-only queries, no host trace — like Get.
func (m *Map[V]) Size() @INT@ {
	m.mu.Lock()
	defer m.mu.Unlock()
	return @INT@(len(m.m))
}
func (m *Map[V]) Keys() []string {
	m.mu.Lock()
	ks := make([]string, 0, len(m.m))
	for k := range m.m {
		ks = append(ks, k)
	}
	m.mu.Unlock()
	slices.Sort(ks)
	return ks
}
'''


# ==========================================================================
# ir_version 3 — the pure / typed-core tier (docs/syntax-2.0.md).
#
# v3 is PURE: no stc-go runtime. It lowers to ordinary Go, so `go build`
# + `go test` IS the execution gate. Structure mirrors backends/rust/emit.py
# (the closest compiled/typed analog) in Go idioms:
#
#   * records            -> Go structs (fields keep their revl names, unexported)
#   * user ADTs/enums    -> a sealed-interface + case-struct tagging, and
#                           `match` -> a Go type switch (mirrors the JAVA tier's
#                           sealed variants; Go has no native sum type)
#   * built-in Opt/Result-> generic sealed interfaces RevlOpt[T]/RevlResult[T,E]
#   * stdlib builtins    -> typed helpers, generics for the List overloads
#   * `test` blocks      -> func TestXxx(t *testing.T) asserting computed values
#
# Reserved-name safety, fn-type parsing and generic-arg splitting are local to
# this section so the v1/v2 path (which must stay byte-identical) is untouched.
# ==========================================================================

_GO_RESERVED = {
    "break", "case", "chan", "const", "continue", "default", "defer", "else",
    "fallthrough", "for", "func", "go", "goto", "if", "import", "interface",
    "map", "package", "range", "return", "select", "struct", "switch", "type",
    "var", "nil", "true", "false", "iota",
}

_V3_PRIM = {
    "Int": "int64",
    "Int32": "int32",
    "Str": "string",
    "Bool": "bool",
    "Float": "float64",
    "Bytes": "[]byte",
}


def _v3_ident(name, role: str) -> str:
    """The pure-v3 counterpart of `_safe_local`, over the full Go keyword set.

    Same injective rule (see `_safe_local`): escape a name iff the name OR any
    name reachable from it by dropping trailing `_` is a Go keyword, by exactly
    one `_`. The plain "reserved -> name + '_'" map sent both `func` and the
    equally legal revl identifier `func_` to `func_`, so two distinct revl
    locals became one Go local and `go build` reported "no new variables on
    left side of :=" — a loud break here, a SILENT capture on the python tier
    from the same shape."""
    if not isinstance(name, str) or not name:
        raise EmitError(f"invalid {role} identifier: {name!r}")
    root = name
    while root:
        if root in _GO_RESERVED:
            return name + "_"
        if not root.endswith("_"):
            break
        root = root[:-1]
    return name


def _v3_split_generic(inner: str) -> list[str]:
    """Split `A, B` at top-level commas (depth-aware for nested generics)."""
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


def _erase_async(ret: str) -> str:
    """Erase the async color from a function-type return (roadmap item 92/94).

    `Async[T]` colors a first-class callback on the py/ts tiers (async/await);
    the go tier has no async-fn machinery, so an `Async[T]` return erases to its
    concrete `T` — `(Str) -> Async[Str]` renders `func(string) string`, not the
    invalid `func(string) Async[Str]` that leaked before. `Async` is
    position-restricted to a fn-type return (typecheck.py), so this is the only
    site it can reach the emitter.
    """
    r = ret.strip()
    if r.startswith("Async[") and r.endswith("]"):
        return r[len("Async["):-1].strip()
    return ret


def _v3_split_fn_type(name: str):
    """`(A, B) -> C` -> (["A","B"], "C") or None when not a function type."""
    if "->" not in name:
        return None
    depth = 0
    for i in range(len(name) - 1):
        ch = name[i]
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "-" and name[i + 1] == ">" and depth == 0:
            params_src = name[:i].strip()
            returns = name[i + 2:].strip()
            if not (params_src.startswith("(") and params_src.endswith(")")):
                return None
            inner = params_src[1:-1].strip()
            params = [p for p in _v3_split_generic(inner)] if inner else []
            return params, returns
    return None


def _go_v3_type(t, types: dict) -> str:
    """Surface type -> Go type for the v3 tier."""
    if t is None or t == "" or t == "Unit":
        return ""
    t = str(t).strip()
    if t in _V3_PRIM:
        return _V3_PRIM[t]
    if t == "Any":
        # revl's `Any` wildcard — a value whose static type only the runtime
        # knows (docs/stdlib-json.md) — erases to Go's `any` (interface{}),
        # the mirror of rust's `cordis::Value`. A JSON document decoded with
        # `encoding/json` into an `any` is exactly this shape.
        return "any"
    fn = _v3_split_fn_type(t)
    if fn is not None:
        params, returns = fn
        rendered = ", ".join(_go_v3_type(p, types) for p in params)
        ret = _go_v3_type(_erase_async(returns), types)
        return f"func({rendered}) {ret}".rstrip()
    if "[" in t and t.endswith("]"):
        head = t[: t.index("[")]
        inner = t[t.index("[") + 1: -1]
        if head == "List":
            return "[]" + _go_v3_type(inner, types)
        if head == "Opt":
            return f"RevlOpt[{_go_v3_type(inner, types)}]"
        if head == "Result":
            ok, err = _v3_split_generic(inner)
            return f"RevlResult[{_go_v3_type(ok, types)}, {_go_v3_type(err, types)}]"
        if head == "Map":
            k, v = _v3_split_generic(inner)
            return f"map[{_go_v3_type(k, types)}]{_go_v3_type(v, types)}"
    if isinstance(types, dict) and t in types:
        return _v3_ident(t, "type name")
    # Unknown named type: pass through as an identifier.
    return _v3_ident(t, "type name")


class _V3GoCtx:
    """Names and layouts visible to the v3 Go expression/statement emitters."""

    def __init__(self, types: dict, functions: list, externs: list) -> None:
        self.types = types or {}
        self.var_types: dict[str, str | None] = {}
        self.function_ret: dict[str, str | None] = {
            fn.get("name"): fn.get("returns") for fn in functions or []
        }
        self.extern_ret: dict[str, str | None] = {
            ex.get("name"): ex.get("returns") for ex in externs or []
        }
        # Declared parameter surface types, keyed by fn/extern name — the flow
        # target for a call argument. Threading these lets a bare `None`, an
        # untyped empty list, or a `Some(literal)` in argument position pick up
        # the concrete element type the callee's signature pins (item 280);
        # without it they erased to `None`/`[]any`/`RevlSome[any]` at the call.
        self.function_params: dict[str, list] = {
            fn.get("name"): [p.get("type") for p in fn.get("params") or []]
            for fn in functions or []
        }
        self.extern_params: dict[str, list] = {
            ex.get("name"): [p.get("type") for p in ex.get("params") or []]
            for ex in externs or []
        }
        # `{name: "List"|"Map"}` for the current function's uniquely-owned
        # collection locals; see `_v3_self_rebind_locals` (item 434 (a)/(b)).
        self.linear_locals: dict = {}
        self.case_adt: dict[str, str | None] = {}
        self.case_payload: dict[str, str | None] = {}
        self.record_by_fields: dict[tuple, str | None] = {}
        self.ret_type: str | None = None
        # feature flags set during rendering / used at module assembly
        self.needs_fmt = False
        self.used_stdlib = False
        self.needs_reflect = False      # structural `==` on a non-scalar
        self.needs_float_div = False    # `/` (true division, IEEE at zero)
        self.needs_ftoa = False         # canonical Float -> Str in interpolation
        self.needs_int_arith = False    # div_floor / div_euclid / mod
        self.needs_overflow = False     # trapping + - * on Int
        self.needs_overflow32 = False   # trapping + - * on Int32, and to_int32
        self.needs_parse_int = False    # Str.to_int (revlParseInt helper)
        # Int -> Str through strconv.FormatInt rather than fmt.Sprintf("%d"):
        # `%d` takes ...any and boxes the operand (item 434 (f)).
        self.needs_strconv = False
        # Stdlib packages an extern @go body asked to be hoisted into the
        # module's import block via a `//revl:import <path>` directive
        # (see _emit_v3_go_externs). A verbatim extern body cannot carry its
        # own `import`, so the directive is the principled seam.
        self.extern_imports: set[str] = set()
        for name, spec in self.types.items():
            if spec.get("kind") == "record":
                key = tuple(sorted((spec.get("fields") or {}).keys()))
                self.record_by_fields[key] = (
                    name if key not in self.record_by_fields else None
                )
            elif spec.get("kind") == "variant":
                for case in spec.get("cases") or []:
                    cname = case.get("name")
                    self.case_payload[cname] = case.get("payload")
                    self.case_adt[cname] = (
                        None if cname in self.case_adt else name
                    )

    def record_type_for_fields(self, fields: list) -> str:
        key = tuple(sorted(fields))
        name = self.record_by_fields.get(key)
        if name is None:
            raise EmitError(
                "cannot infer Go struct type for record literal with fields "
                f"{sorted(fields)!r} — no unique record has exactly those fields"
            )
        return _v3_ident(name, "type name")


# The total, value-returning division forms (docs/arithmetic.md): same
# rounding as the faulting operations, Err(reason) at a zero divisor.
_GO_CHECKED_DIV = ("checked_div_trunc", "checked_div_floor",
                   "checked_div_euclid", "checked_mod")
_GO_DIV_ZERO_MSG = "revl: division by zero"


def _v3_builtin_ret_type(method, recv_type):
    if method in ("length", "indexOf", "charCodeAt", "codepoint_at",
                  "div_trunc", "div_floor", "div_euclid", "mod"):
        return "Int"
    # `to_int` is BOTH the Int32 widen and the Str parse (FR-9): the result
    # type follows the receiver, exactly as the checker dispatches it.
    if method == "to_int":
        return "Opt[Int]" if recv_type == "Str" else "Int"
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str).
    if method == "to_str":
        return "Str"
    if method == "to_int32":
        return "Int32"
    # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith).
    if method in ("startsWith", "endsWith"):
        return "Bool"
    # The total forms (docs/arithmetic.md) produce a Result value.
    if method in ("checked_div_trunc", "checked_div_floor",
                  "checked_div_euclid", "checked_mod"):
        return "Result[Int, Str]"
    if method in ("charAt", "repeat", "join"):
        return "Str"
    if method == "split":
        return "List[Str]"
    if method in ("slice", "concat", "push"):
        return recv_type
    # The Map value type (docs/stdlib-2.0.md §Map).
    if method == "set":
        return recv_type
    if method == "remove":
        return recv_type
    if method == "size":
        return "Int"
    if method == "keys":
        return "List[Str]"
    if method == "has":
        return "Bool"
    if method == "lookup":
        if isinstance(recv_type, str) and recv_type.startswith("Map[") and recv_type.endswith("]"):
            _, v = _v3_split_generic(recv_type[4:-1])
            return f"Opt[{v}]"
        return None
    return None


def _go_v3_infer_type(node, ctx: _V3GoCtx):
    """Surface type of an expression when knowable, else None."""
    if not isinstance(node, dict):
        return None
    # a marked Int -> Float coercion site yields a Float (docs/arithmetic.md):
    # without this, `let x: Float = 3` declared `var x int64 = float64(3)`.
    if node.get("widen") == "Float":
        return "Float"
    if node.get("widen") == "Int":
        return "Int"  # Int32 widened to Int
    kind = node.get("kind")
    if kind == "lit":
        v = node.get("value")
        if isinstance(v, bool):
            return "Bool"
        if isinstance(v, int):
            return "Int"
        if isinstance(v, float):
            return "Float"
        if isinstance(v, str):
            return "Str"
        return None
    if kind in ("var", "name"):
        return ctx.var_types.get(node.get("name") or node.get("id"))
    if kind == "interp":
        return "Str"
    if kind == "field":
        tt = _go_v3_infer_type(node.get("target"), ctx)
        spec = ctx.types.get(tt) if isinstance(tt, str) else None
        if spec and spec.get("kind") == "record":
            return (spec.get("fields") or {}).get(node.get("name"))
        return None
    if kind == "index":
        tt = _go_v3_infer_type(node.get("target"), ctx)
        if isinstance(tt, str) and tt.startswith("List[") and tt.endswith("]"):
            return tt[5:-1]
        return None
    if kind == "len":
        # `xs.length` in a pure fn body: Int, whatever the sized receiver.
        return "Int"
    if kind == "builtin":
        return _v3_builtin_ret_type(
            node.get("method"), _go_v3_infer_type(node.get("target"), ctx)
        )
    if kind == "maplit":
        # `Map.empty()` — the empty literal carries its pin when the author's
        # annotation supplied one (roadmap 76b); otherwise it stays unknown.
        return node.get("expected")
    if kind == "list":
        exp = node.get("expected")
        if isinstance(exp, str) and exp.startswith("List[") and exp.endswith("]"):
            return exp
        items = node.get("items") or []
        if items:
            el = _go_v3_infer_type(items[0], ctx)
            return f"List[{el}]" if el else None
        return None
    if kind == "call":
        callee = node.get("callee") or {}
        if callee.get("kind") == "var":
            nm = callee.get("name")
            if nm in ("Some", "None", "Ok", "Err"):
                return _go_v3_infer_ctor(nm, node.get("args") or [], ctx)
            return ctx.function_ret.get(nm) or ctx.extern_ret.get(nm)
        return None
    if kind == "bin":
        op = node.get("op")
        if op in ("==", "===", "!=", "!==", "<", ">", "<=", ">=", "&&", "||"):
            return "Bool"
        if node.get("operands") == "Int32" and op in ("+", "-", "*"):
            return "Int32"  # Int32 arithmetic stays Int32 (docs/arithmetic.md)
        if op in ("&", "|", "^", "<<", ">>"):
            return "Int32"  # bitwise ops are Int32-only (docs/arithmetic.md)
        if op == "+":
            lt = _go_v3_infer_type(node.get("left"), ctx)
            rt = _go_v3_infer_type(node.get("right"), ctx)
            if lt == "Str" or rt == "Str":
                return "Str"
            return lt or rt
        if op == "/":
            return "Float"  # true division (docs/arithmetic.md)
        if op in ("-", "*", "%"):
            return "Int"
        return None
    if kind == "un":
        if node.get("op") == "!":
            return "Bool"
        if node.get("op") == "~":
            return "Int32"  # bitwise complement is Int32-only (docs/arithmetic.md)
        return _go_v3_infer_type(node.get("operand"), ctx)
    if kind == "if":
        return (_go_v3_infer_type(node.get("then"), ctx)
                or _go_v3_infer_type(node.get("else"), ctx))
    if kind == "record":
        try:
            return ctx.record_type_for_fields([f[0] for f in node.get("fields") or []])
        except EmitError:
            return None
    if kind == "adt":
        case = node.get("case")
        if case in ("Some", "None", "Ok", "Err"):
            return _go_v3_infer_ctor(case, node.get("args") or [], ctx)
        return ctx.case_adt.get(case)
    return None


def _go_v3_infer_ctor(case, arg_nodes, ctx):
    """Surface type of a built-in Opt/Result construction, when the argument
    reveals the element type — `Some(7)` -> `Opt[Int]`. None stays unknown
    (`Opt[any]`) since a bare `None` carries no element (item 280)."""
    if case in ("Some", "None"):
        el = _go_v3_infer_type(arg_nodes[0], ctx) if arg_nodes else None
        return f"Opt[{el or 'any'}]"
    el = _go_v3_infer_type(arg_nodes[0], ctx) if arg_nodes else None
    if case == "Ok":
        return f"Result[{el or 'any'}, any]"
    return f"Result[any, {el or 'any'}]"


_V3_GO_BIN_OPS = {
    "==": "==", "===": "==", "!=": "!=", "!==": "!=",
    "<": "<", ">": ">", "<=": "<=", ">=": ">=",
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%", "&&": "&&", "||": "||",
    # Int32 bitwise operators (item 366, docs/arithmetic.md). `& | ^` are native
    # on int32. The shifts are rendered specially (`_go_v3_expr`): Go does NOT
    # mask the shift count (`1 << 32` is 0, not 1) and panics on a negative
    # signed count, so the count is taken as `uint32(n) & 31` to match the
    # spec's mod-32 rule and wasm/JS.
    "&": "&", "|": "|", "^": "^", "<<": "<<", ">>": ">>",
}

_V3_GO_ATOMIC = {"var", "name", "lit", "call", "field", "index", "builtin"}

# Types Go can compare with `==` without it being either wrong or a compile
# error. Everything else (records, lists, ADTs, Opt/Result) goes through
# revlEq.
_GO_SCALARS = {"Int", "Int32", "Float", "Str", "Bool"}


def _v3_has_any(surface) -> bool:
    """Whether a surface type contains an erased `any` component — a whole-word
    match so a user type whose name merely embeds the letters (e.g. `Company`)
    is not mistaken for one. Used to pick the fully concrete operand of an
    equality so both sides emit the same generic instantiation (item 302)."""
    return bool(isinstance(surface, str) and re.search(r"\bany\b", surface))


def _go_v3_is_interface(surface, types) -> bool:
    """Whether a revl surface type lowers to a Go *interface* on this tier.

    The sealed sum types do: Opt[T] -> RevlOpt (interface), Result[T,E] ->
    RevlResult (interface), and a declared user variant -> its `is<Name>()`
    interface. Records, lists, maps and scalars lower to concrete Go types.
    A type-switch (`x.(type)`) is only legal on an interface, and a value that
    flows into a match must be stored in its interface type for the switch to
    both compile and discriminate — item 280.
    """
    if not isinstance(surface, str):
        return False
    s = surface.strip()
    if s == "any":  # Go's empty interface — a type-switch on it is legal
        return True
    if (s.startswith("Opt[") and s.endswith("]")) or (
            s.startswith("Result[") and s.endswith("]")):
        return True
    spec = types.get(s)
    return bool(spec) and spec.get("kind") == "variant"


def _go_v3_lit(node: dict) -> str:
    value = node.get("value")
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "nil"
    if isinstance(value, str):
        return _go_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # `float64(...)`, not a bare literal. Go evaluates *untyped constant*
        # arithmetic at arbitrary precision, so `0.1 + 0.2` folds to exactly
        # 0.3 at compile time and compares equal to it — which is not IEEE 754
        # binary64, the semantics revl specifies (docs/arithmetic.md). Typing
        # the literal forces ordinary float64 arithmetic.
        return f"float64({value!r})"
    raise EmitError(f"unsupported v3 literal: {node!r}")


def _go_v3_construct(ctx: _V3GoCtx, case: str, arg_renders: list, expected,
                     arg_nodes=None):
    """Render an ADT/Opt/Result construction from a case name + rendered args.

    `arg_nodes` are the unrendered argument expressions, used to recover the
    concrete element type of a built-in `Some`/`Ok`/`Err` when no expected
    Opt/Result type pins it — so `Some(7)` is `RevlSome[int64]`, not the
    `RevlSome[any]` that later defeats a type-switch (item 280)."""
    adt = ctx.case_adt.get(case)
    if adt is not None:
        # user variant (monomorphic): `<Variant><Case>{...}`
        payload = ctx.case_payload.get(case)
        struct = f"{_v3_ident(adt, 'type name')}{case}"
        if payload is None:
            if arg_renders:
                raise EmitError(f"variant case `{case}` takes no payload")
            return f"{struct}{{}}"
        if len(arg_renders) != 1:
            raise EmitError(f"variant case `{case}` takes exactly one payload")
        return f"{struct}{{Value: {arg_renders[0]}}}"
    # built-in Opt / Result — need the type arguments from the expected type.
    exp = (expected or "").strip() if isinstance(expected, str) else ""
    if case in ("Some", "None"):
        if exp.startswith("Opt[") and exp.endswith("]"):
            inner = exp[4:-1]
        elif case == "Some" and arg_nodes:
            # No expected Opt type at this site: recover the element type from
            # the argument so `Some(7)` keeps its `int64` and a later
            # type-switch on the concrete case still matches (item 280).
            inner = _go_v3_infer_type(arg_nodes[0], ctx) or "any"
        else:
            inner = "any"
        got = _go_v3_type(inner, ctx.types) or "any"
        if case == "None":
            if arg_renders:
                raise EmitError("`None` takes no arguments")
            return f"RevlNone[{got}]{{}}"
        return f"RevlSome[{got}]{{Value: {arg_renders[0]}}}"
    if case in ("Ok", "Err"):
        if exp.startswith("Result[") and exp.endswith("]"):
            ok, err = _v3_split_generic(exp[7:-1])
        elif arg_nodes:
            # Only the present side's type is knowable from the argument; the
            # other stays `any`. Enough to keep a matched concrete case aligned.
            got_side = _go_v3_infer_type(arg_nodes[0], ctx) or "any"
            ok, err = (got_side, "any") if case == "Ok" else ("any", got_side)
        else:
            ok, err = "any", "any"
        got_ok = _go_v3_type(ok, ctx.types) or "any"
        got_err = _go_v3_type(err, ctx.types) or "any"
        payload = arg_renders[0] if arg_renders else ""
        if case == "Ok":
            return f"RevlOk[{got_ok}, {got_err}]{{Value: {payload}}}"
        return f"RevlErr[{got_ok}, {got_err}]{{Value: {payload}}}"
    raise EmitError(f"unknown ADT constructor {case!r}")


def _go_v3_expr(node, ctx: _V3GoCtx, expected=None) -> str:
    if not isinstance(node, dict) or "kind" not in node:
        raise EmitError(f"malformed v3 expression: {node!r}")
    # An implicit Int -> Float coercion site (docs/arithmetic.md): go's
    # untyped-constant rule used to absorb it silently; the marker makes the
    # conversion explicit in the emitted source.
    if node.get("widen") == "Float":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"float64({_go_v3_expr(inner, ctx, expected)})"
    # An Int32 -> Int widening site (docs/arithmetic.md): int32 does not
    # implicitly convert to int64 in Go, so the lossless widening is spelled
    # out where the frontend marked it.
    if node.get("widen") == "Int":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"int64({_go_v3_expr(inner, ctx, expected)})"
    kind = node["kind"]

    if kind == "lit":
        return _go_v3_lit(node)

    if kind in ("var", "name"):
        name = node.get("name") or node.get("id")
        if name == "None":
            # A bare `None` (returned, annotated, or passed) is the built-in
            # Opt constructor, not an identifier — it must lower to a typed
            # RevlNone, picking up the element type from the flow target
            # (item 280); without this it emitted `None`, an undefined ident.
            return _go_v3_construct(ctx, "None", [], expected)
        if name in ctx.case_adt and not (node.get("args") is not None):
            # bare nullary variant reference is not expected in the fixtures;
            # constructions arrive as `adt`. Fall through to a plain ident.
            pass
        return _v3_ident(name, "name")

    if kind == "adt":
        arg_nodes = node.get("args") or []
        args = [_go_v3_expr(a, ctx) for a in arg_nodes]
        return _go_v3_construct(ctx, node["case"], args, expected,
                                arg_nodes=arg_nodes)

    if kind == "bin":
        op = node.get("op")
        if op == "??":
            ctx.used_stdlib = True  # revlOptOr lives in the opt preamble
            left = _go_v3_expr(node["left"], ctx)
            right = _go_v3_expr(node["right"], ctx)
            return f"revlOptOr({left}, {right})"
        go_op = _V3_GO_BIN_OPS.get(op)
        if go_op is None:
            raise EmitError(f"unsupported v3 binary operator {op!r}")
        if op in ("==", "===", "!=", "!=="):
            # revl has ONE equality and it is structural (syntax-2.0 §3.4).
            # Go `==` is value equality for comparable structs but a *compile
            # error* on slices ("slice can only be compared to nil"), so a
            # record holding a List is not comparable at all. Scalars keep the
            # native operator; everything else goes through revlEq.
            lt = _go_v3_infer_type(node.get("left"), ctx)
            rt = _go_v3_infer_type(node.get("right"), ctx)
            if not (lt in _GO_SCALARS and rt in _GO_SCALARS):
                # Element-type recovery across the two operands (item 302, the
                # value-level sibling of 280). revlEq is reflect.DeepEqual,
                # which is FALSE across two different generic instantiations:
                # a bare `Err("")` / `None` / `[]` erases to
                # RevlErr[any, string] / RevlNone[any] / []any, and DeepEqual
                # against the other side's concrete RevlErr[int64, string] /
                # RevlNone[string] / []string reports unequal even though the
                # values match on py. Pin BOTH sides to whichever operand type
                # is fully concrete (no `any`), so equal values emit as the
                # identical Go type. A construction/empty-list renderer only
                # honours an expected pin whose shape matches, so threading the
                # chosen type into the concrete side (e.g. a `probe()` call) is
                # a no-op there.
                exp = None
                for cand in (lt, rt):
                    if isinstance(cand, str) and not _v3_has_any(cand):
                        exp = cand
                        break
                left = _go_v3_expr(node["left"], ctx, exp)
                right = _go_v3_expr(node["right"], ctx, exp)
                ctx.needs_reflect = True
                call = f"revlEq({left}, {right})"
                return call if op in ("==", "===") else f"(!{call})"
        # Scalar equality and every non-equality operator: render operands
        # plainly. (Non-scalar equality returned above with element-type
        # recovery threaded into both sides.)
        left = _go_v3_expr(node["left"], ctx)
        right = _go_v3_expr(node["right"], ctx)
        if op in ("+", "-", "*") and node.get("operands") == "Int":
            # Int overflow traps (docs/arithmetic.md). Go has no checked
            # arithmetic in the standard library, so the helpers detect it.
            ctx.needs_overflow = True
            helper = {"+": "revlAdd", "-": "revlSub", "*": "revlMul"}[op]
            return f"{helper}({left}, {right})"
        if op in ("+", "-", "*") and node.get("operands") == "Int32":
            # Int32 traps at the i32 edge; Go's int32 wraps, so the helpers
            # detect it exactly as the i64 ones do (docs/arithmetic.md).
            ctx.needs_overflow32 = True
            helper = {"+": "revlAddI32", "-": "revlSubI32", "*": "revlMulI32"}[op]
            return f"{helper}({left}, {right})"
        if op == "/":
            # `/` is true division and yields Float (docs/arithmetic.md). Go
            # `/` on two int64 is integer division, and a *constant* `1.0/0.0`
            # is a compile error where IEEE defines +Inf — the helper makes it
            # a runtime float division, which is both.
            ctx.needs_float_div = True
            if node.get("operands") in ("Int", "Int32"):
                return f"revlDiv(float64({left}), float64({right}))"
            return f"revlDiv({left}, {right})"
        if op in ("<<", ">>"):
            # Int32 shift: mask the count to 0..31 as an unsigned value, because
            # Go neither masks the count nor accepts a negative signed one. `<<`
            # drops the high bits (int32 two's complement); `>>` on the signed
            # int32 is arithmetic (docs/arithmetic.md, item 366).
            return f"({left} {op} (uint32({right}) & 31))"
        return f"({left} {go_op} {right})"

    if kind == "un":
        operand = _go_v3_expr(node.get("operand"), ctx)
        if node.get("op") == "!":
            return f"(!{operand})"
        if node.get("op") == "~":
            # Int32 bitwise complement (item 366): Go spells bitwise NOT as the
            # unary `^`. A bit op, so it never traps.
            return f"(^{operand})"
        if node.get("op") == "-":
            if node.get("operands") == "Int":
                ctx.needs_overflow = True
                return f"revlSub(0, {operand})"
            if node.get("operands") == "Int32":
                ctx.needs_overflow32 = True
                return f"revlSubI32(0, {operand})"
            return f"(-{operand})"
        raise EmitError(f"unsupported v3 unary operator {node.get('op')!r}")

    if kind == "call":
        callee = node.get("callee") or {}
        arg_nodes = node.get("args") or []
        cname = callee.get("name") if callee.get("kind") == "var" else None
        is_ctor = cname in ctx.case_adt or cname in ("Some", "None", "Ok", "Err")
        # For an ordinary fn/extern call, flow each declared parameter type into
        # its argument so a bare `None`, an empty list, or a `Some(literal)` in
        # argument position lands as the callee's concrete element type, not the
        # erased default (item 280). Constructor args take their type from the
        # construction's own expected instead.
        param_types = None
        if cname and not is_ctor:
            param_types = (ctx.function_params.get(cname)
                           or ctx.extern_params.get(cname))
        arg_renders = []
        for i, a in enumerate(arg_nodes):
            exp = param_types[i] if param_types and i < len(param_types) else None
            arg_renders.append(_go_v3_expr(a, ctx, exp))
        if is_ctor:
            return _go_v3_construct(ctx, cname, arg_renders, expected,
                                    arg_nodes=arg_nodes)
        callee_src = _go_v3_expr(callee, ctx)
        return f"{callee_src}({', '.join(arg_renders)})"

    if kind == "field":
        target_node = node.get("target")
        target = _go_v3_expr(target_node, ctx)
        if target_node.get("kind") not in _V3_GO_ATOMIC:
            target = f"({target})"
        return f"{target}.{_v3_field_ident(node.get('name'))}"

    if kind == "index":
        target_node = node.get("target")
        target = _go_v3_expr(target_node, ctx)
        if target_node.get("kind") not in _V3_GO_ATOMIC:
            target = f"({target})"
        return f"{target}[{_go_v3_expr(node.get('index'), ctx)}]"

    if kind == "len":
        # `xs.length` in a pure fn body lowers to the `len` node (the frontend
        # spells the same access as a `field` in component positions). Go's
        # `len()` is bytes on a string, and revl length is code points, so the
        # sized-value helpers from _V3_STDLIB_PREAMBLE are used — the same
        # dispatch the `length` builtin already makes.
        ctx.used_stdlib = True
        target_node = node.get("target")
        target = _go_v3_expr(target_node, ctx)
        rt = _go_v3_infer_type(target_node, ctx)
        return f"revlStrLen({target})" if rt == "Str" else f"revlListLen({target})"

    if kind == "record_update":
        raise EmitError(
            "functional record update `{r | f = e}` is not emitted by the go "
            "backend yet (implemented tiers: python, typescript) - see "
            "docs/records.md §6; lift it into a helper fn instead")

    if kind == "record":
        fields = node.get("fields") or []
        type_name = ctx.record_type_for_fields([k for k, _ in fields])
        body = ", ".join(
            f"{_v3_field_ident(k)}: {_go_v3_expr(v, ctx)}" for k, v in fields
        )
        return f"{type_name}{{{body}}}"

    if kind == "list":
        elem = None
        # The flow target pins the element type; when the call site gives none,
        # fall back to the annotation the frontend threaded onto the node
        # (`let xs: List[T] = []`, roadmap 76b) — the only source for an *empty*
        # list, which otherwise erased to `[]any` (item 280).
        pin = expected if isinstance(expected, str) else node.get("expected")
        if isinstance(pin, str) and pin.startswith("List[") and pin.endswith("]"):
            elem = pin[5:-1]
        items = node.get("items") or []
        if elem is None and items:
            elem = _go_v3_infer_type(items[0], ctx)
        go_elem = _go_v3_type(elem, ctx.types) if elem else "any"
        rendered = ", ".join(_go_v3_expr(it, ctx) for it in items)
        return f"[]{go_elem}{{{rendered}}}"

    if kind == "maplit":
        # `Map.empty()` (docs/stdlib-2.0.md §Map). Go infers composite
        # literals positionally, never from later use, so an unpinned empty
        # map is refused rather than emitted as non-compiling Go — the same
        # honesty as an untyped empty list on tiers that cannot infer it. The
        # pin is the expected Map type: a typed fn return/parameter, or the
        # annotated `let/var x: Map[K, V] = Map.empty()` the frontend threads
        # onto the node as `expected` (roadmap 76b).
        if expected is None:
            expected = node.get("expected")
        if isinstance(expected, str) and expected.startswith("Map[") and expected.endswith("]"):
            k, v = _v3_split_generic(expected[4:-1])
            return f"map[{_go_v3_type(k, ctx.types)}]{_go_v3_type(v, ctx.types)}{{}}"
        raise EmitError(
            "an untyped empty Map needs an expected Map type on this tier "
            "(Go infers literals positionally, not from later use) - pin it "
            "via a typed fn return, or an annotated `let`/`var` "
            "declaration (the positions this tier actually reads)")

    if kind == "arrow":
        names = node.get("params") or []
        declared = list(node.get("param_types") or [])
        declared += [None] * (len(names) - len(declared))
        # Untyped lambda parameters (`v => v + 1`) would default to `any`, on
        # which arithmetic will not type-check. Recover a type from how the
        # parameter is used in the body before falling back.
        for pi, (pname, ptype) in enumerate(zip(names, declared)):
            if ptype is None:
                declared[pi] = _infer_arrow_param(pname, node.get("body"), ctx)
        # Bind inferred param surface types so the body renders against them.
        saved = dict(ctx.var_types)
        for pname, ptype in zip(names, declared):
            if ptype is not None:
                ctx.var_types[pname] = ptype
        params = ", ".join(
            f"{_v3_ident(p, 'arrow parameter')} {_go_v3_type(t, ctx.types) or 'any'}"
            for p, t in zip(names, declared)
        )
        body = _go_v3_expr(node.get("body"), ctx)
        body_t = _go_v3_type(_go_v3_infer_type(node.get("body"), ctx), ctx.types)
        ctx.var_types = saved
        ret = f" {body_t}" if body_t else ""
        return f"func({params}){ret} {{ return {body} }}"

    if kind == "if":
        exp_t = _go_v3_type(expected, ctx.types) if expected else _go_v3_type(
            _go_v3_infer_type(node.get("then"), ctx), ctx.types)
        exp_t = exp_t or "any"
        cond = _go_v3_expr(node.get("cond"), ctx)
        then = _go_v3_expr(node.get("then"), ctx, expected)
        els = _go_v3_expr(node.get("else"), ctx, expected)
        return f"func() {exp_t} {{ if {cond} {{ return {then} }}; return {els} }}()"

    if kind == "builtin":
        target_node = node.get("target")
        target = _go_v3_expr(target_node, ctx)
        if target_node.get("kind") not in _V3_GO_ATOMIC:
            target = f"({target})"
        args = [_go_v3_expr(a, ctx) for a in node.get("args") or []]
        return _go_v3_builtin(ctx, node.get("method"), target_node, target, args)

    if kind == "interp":
        return _go_v3_interp(node, ctx)

    if kind == "match":
        return _go_v3_match(node, ctx, expected)

    if kind == "optfield":
        return _go_v3_optchain(node, ctx, field=node.get("name"))

    if kind == "optcall":
        return _go_v3_optchain(node, ctx, method=node.get("method"),
                               args=node.get("args") or [])

    raise EmitError(f"unsupported v3 expression kind {kind!r} in Go backend")


def _infer_arrow_param(name, body, ctx):
    """Recover an untyped lambda parameter's surface type from its first use in
    the body (the other side of a binary op, an index/builtin receiver)."""
    found = []

    def walk(node):
        if found or not isinstance(node, dict):
            return
        if node.get("kind") == "bin":
            for a, b in ((node.get("left"), node.get("right")),
                         (node.get("right"), node.get("left"))):
                if isinstance(a, dict) and a.get("kind") in ("var", "name") \
                        and (a.get("name") or a.get("id")) == name:
                    other = _go_v3_infer_type(b, ctx)
                    if other:
                        found.append(other)
                        return
        for v in node.values():
            if isinstance(v, (dict, list)):
                walk(v)

    def walk_any(node):
        if isinstance(node, list):
            for x in node:
                walk_any(x)
        elif isinstance(node, dict):
            walk(node)
            for v in node.values():
                walk_any(v)

    walk_any(body)
    return found[0] if found else None


def _go_v3_builtin(ctx, method, target_node, target, args):
    ctx.used_stdlib = True
    rt = _go_v3_infer_type(target_node, ctx)
    is_str = (rt == "Str")
    if method == "length":
        return f"revlStrLen({target})" if is_str else f"revlListLen({target})"
    if method == "push":
        return f"revlListPush({target}, {args[0]})"
    if method == "concat":
        return (f"revlStrConcat({target}, {args[0]})" if is_str
                else f"revlListConcat({target}, {args[0]})")
    if method == "slice":
        return (f"revlStrSlice({target}, {args[0]}, {args[1]})" if is_str
                else f"revlListSlice({target}, {args[0]}, {args[1]})")
    if method == "indexOf":
        return (f"revlStrIndexOf({target}, {args[0]})" if is_str
                else f"revlListIndexOf({target}, {args[0]})")
    if method == "split":
        return f"revlStrSplit({target}, {args[0]})"
    if method == "join":
        return f"revlJoin({target}, {args[0]})"
    if method == "repeat":
        return f"revlStrRepeat({target}, {args[0]})"
    if method == "charAt":
        return f"revlStrCharAt({target}, {args[0]})"
    if method == "charCodeAt":
        return f"revlStrCharCodeAt({target}, {args[0]})"
    # Codepoint-at-index scan (item 276, docs/stdlib-2.0.md §Str.codepoint_at):
    # the Unicode scalar at code-point index i, via the same rune-indexed
    # helper as charCodeAt.
    if method == "codepoint_at":
        return f"revlStrCharCodeAt({target}, {args[0]})"
    # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith):
    # HasPrefix/HasSuffix compare bytes, and a code-point prefix of a UTF-8
    # string is exactly a byte prefix.
    if method == "startsWith":
        return f"strings.HasPrefix({target}, {args[0]})"
    if method == "endsWith":
        return f"strings.HasSuffix({target}, {args[0]})"
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str): strconv.FormatInt
    # base 10 is exact decimal for an int64, and unlike fmt.Sprintf("%d", x) it
    # takes the int64 directly instead of boxing it into an `any` (item 434
    # (f): 2 allocs/16 B -> 1 alloc/4 B). Int32 widens first; FormatInt's
    # parameter is int64.
    if method == "to_str":
        ctx.needs_strconv = True
        if rt == "Int32":
            return f"strconv.FormatInt(int64({target}), 10)"
        return f"strconv.FormatInt({target}, 10)"
    # The Map value type (docs/stdlib-2.0.md §Map): persistent Go maps —
    # `set` copies into a fresh map, `lookup` answers the sealed RevlOpt.
    if method == "set":
        return f"revlMapSet({target}, {args[0]}, {args[1]})"
    if method == "lookup":
        return f"revlMapGet({target}, {args[0]})"
    if method == "has":
        return f"revlMapHas({target}, {args[0]})"
    # The iteration/remove step (docs/stdlib-2.0.md §Map): the same helpers
    # as the component tier, in _V3_MAP_PREAMBLE.
    if method == "size":
        return f"int64(len({target}))"
    if method == "keys":
        return f"revlMapKeys({target})"
    if method == "remove":
        return f"revlMapRemove({target}, {args[0]})"
    # Integer division and modulo (docs/arithmetic.md). Go `/` truncates and
    # `%` takes the dividend's sign, which is what revl specifies, so
    # div_trunc is native; the other three are helpers so every tier computes
    # the same thing.
    # Int/Int32 width conversions (docs/arithmetic.md). Widening Int32 -> Int
    # is a plain int64 conversion; narrowing Int -> Int32 re-imposes the 32-bit
    # bound through revlToI32, which panics out of range. `to_int` is ALSO the
    # Str parse (FR-9, docs/stdlib-2.0.md §Str.to_int): revlParseInt answers
    # the tier's sealed RevlOpt — None for empty/partial/`+` spellings and for
    # out-of-i64-range values, which strconv-style overflow would otherwise
    # have to throw for.
    if method == "to_int":
        if rt == "Str":
            ctx.needs_parse_int = True
            return f"revlParseInt({target})"
        return f"int64({target})"
    if method == "to_int32":
        ctx.needs_overflow32 = True
        return f"revlToI32({target})"
    if method == "div_trunc":
        ctx.needs_int_arith = True
        return f"revlDivTrunc({target}, {args[0]})"
    if method in ("div_floor", "div_euclid", "mod"):
        ctx.needs_int_arith = True
        helper = {"div_floor": "revlDivFloor", "div_euclid": "revlDivEuclid",
                  "mod": "revlMod"}[method]
        return f"{helper}({target}, {args[0]})"
    # The total forms (docs/arithmetic.md): same quotient as the faulting
    # operation, but a zero divisor yields Err(reason) instead of panicking —
    # `fail` is refused in a pure fn, so the error travels as a value. An
    # immediately-applied func literal evaluates each operand exactly once;
    # the concrete RevlOk/RevlErr instantiations satisfy the interface.
    if method in _GO_CHECKED_DIV:
        if method != "checked_div_trunc":
            ctx.needs_int_arith = True
        quotient = {"checked_div_trunc": "_a / _b",
                    "checked_div_floor": "revlDivFloor(_a, _b)",
                    "checked_div_euclid": "revlDivEuclid(_a, _b)",
                    "checked_mod": "revlMod(_a, _b)"}[method]
        overflow_err = "" if method == "checked_mod" else (
            'if _a == (-9223372036854775807 - 1) && _b == -1 { '
            'return RevlErr[int64, string]{Value: "revl: Int overflow"} }; ')
        return (f'func(_a, _b int64) RevlResult[int64, string] {{ '
                f'if _b == 0 {{ return RevlErr[int64, string]'
                f'{{Value: "{_GO_DIV_ZERO_MSG}"}} }}; '
                f'{overflow_err}'
                f'return RevlOk[int64, string]{{Value: {quotient}}} }}'
                f'({target}, {args[0]})')
    raise EmitError(f"unknown v3 builtin method {method!r}")


def _go_v3_interp(node: dict, ctx: _V3GoCtx) -> str:
    """`${..}` interpolation, rendered from the operand types the emitter knows.

    `fmt.Sprintf` with `%v` takes `...any`, so every operand is boxed into an
    interface: `${a}/${b}` over two `Str` measured 3 allocs / 48 B where the
    same emitter's `a + "/" + b` measured 1 / 16 (roadmap item 434 (f)). Where
    every part's type is known, each renders to a `string`-typed piece and the
    whole interpolation is a `+` chain: one allocation for the result, no
    boxing, and no `fmt` import at all. `%v` stays only as the fallback for a
    part whose type could not be inferred.
    """
    # Every part is rendered exactly once; both spellings are built in the same
    # pass so the fallback is a choice at the end rather than a second walk
    # (rendering twice would double any ctx feature flag a part sets).
    pieces: list[str] = []
    fmt_parts: list[str] = []
    args: list[str] = []
    typed = True
    wants_strconv = False
    for part_kind, value in node.get("parts") or []:
        if part_kind == "text":
            text = str(value)
            pieces.append(_go_string(text))
            fmt_parts.append(text.replace("%", "%%"))
            continue
        vt = _go_v3_infer_type(value, ctx)
        rendered = _go_v3_expr(value, ctx)
        if vt == "Float":
            # A `Float` renders through the canonical ECMAScript form, not
            # Go's `%v` (`%v` matches for these values but diverges elsewhere,
            # e.g. `1e-07` vs `1e-7`); see docs/strings.md. revlFtoa already
            # answers a string, so it is the piece on both paths.
            ctx.needs_ftoa = True
            pieces.append(f"revlFtoa({rendered})")
            fmt_parts.append("%s")
            args.append(f"revlFtoa({rendered})")
            continue
        fmt_parts.append("%v")
        args.append(rendered)
        if vt == "Str":
            pieces.append(rendered)
        elif vt in ("Int", "Int32"):
            # strconv.FormatInt renders the same decimal `%v`/`%d` does for an
            # integer, from the int64 itself rather than through an `any`.
            wants_strconv = True
            inner = rendered if vt == "Int" else f"int64({rendered})"
            pieces.append(f"strconv.FormatInt({inner}, 10)")
        elif vt == "Bool":
            # `%v` on a bool is "true"/"false", which is FormatBool exactly.
            wants_strconv = True
            pieces.append(f"strconv.FormatBool({rendered})")
        else:
            typed = False
    if not args:
        return _go_string("".join(fmt_parts))
    if typed:
        if wants_strconv:
            ctx.needs_strconv = True
        # A lone piece is already a `string`; `+` needs at least two operands.
        if len(pieces) == 1:
            return pieces[0]
        return "(" + " + ".join(pieces) + ")"
    ctx.needs_fmt = True
    return f"fmt.Sprintf({_go_string(''.join(fmt_parts))}, {', '.join(args)})"


def _go_v3_optchain(node, ctx: _V3GoCtx, *, field=None, method=None, args=None):
    """`?.field` / `?.method(..)` -> revlOptMap over the Opt payload."""
    ctx.used_stdlib = True
    target_node = node.get("target")
    target = _go_v3_expr(target_node, ctx)
    tt = _go_v3_infer_type(target_node, ctx)
    if not (isinstance(tt, str) and tt.startswith("Opt[") and tt.endswith("]")):
        raise EmitError(
            f"optional chaining requires an Opt[..] receiver, got {tt!r}"
        )
    payload = tt[4:-1]
    go_payload = _go_v3_type(payload, ctx.types)
    # infer the closure's return type so Go can infer revlOptMap's B.
    if field is not None:
        spec = ctx.types.get(payload)
        ret_surface = None
        if spec and spec.get("kind") == "record":
            ret_surface = (spec.get("fields") or {}).get(field)
        body = f"_x.{_v3_field_ident(field)}"
    else:
        arg_renders = [_go_v3_expr(a, ctx) for a in args or []]
        # optcall receiver payload becomes _x; render the builtin against it.
        ret_surface = _v3_builtin_ret_type(method, payload)
        body = _go_v3_builtin(
            ctx, method, {"kind": "var", "name": "__optx"}, "_x", arg_renders
        )
    go_ret = _go_v3_type(ret_surface, ctx.types) if ret_surface else "any"
    return (f"revlOptMap({target}, func(_x {go_payload}) {go_ret} "
            f"{{ return {body} }})")


def _go_v3_match(node: dict, ctx: _V3GoCtx, expected) -> str:
    scrut_node = node.get("scrutinee")
    scrutinee = _go_v3_expr(scrut_node, ctx)
    st = _go_v3_infer_type(scrut_node, ctx)
    arms = node.get("arms") or []
    exp_t = _go_v3_type(expected, ctx.types) if expected else None
    if not exp_t:
        for arm in arms:
            exp_t = _go_v3_type(_go_v3_infer_type(arm.get("body"), ctx), ctx.types)
            if exp_t:
                break
    exp_t = exp_t or "any"

    # A scrutinee whose Go type is concrete (a scalar, record, list, or a
    # narrowed Opt/variant case struct) cannot back a type-switch — `x.(type)`
    # is a compile error on a non-interface. The only pattern that can match a
    # concrete value here is the wildcard, so lower to its body directly rather
    # than to a switch (item 280).
    if st is not None and not _go_v3_is_interface(st, ctx.types):
        wild = next((a for a in arms if a.get("pattern") == "_"), None)
        if wild is not None:
            lines = [f"func() {exp_t} {{"]
            wbind = wild.get("bind")
            # `wbind == "_"` has no name to hold the value; discard it the
            # same way an unbound arm does (Go: "cannot use _ as value").
            if wbind and wbind != "_":
                ctx.var_types[wbind] = st
                lines.append(f"\t{_v3_ident(wbind, 'match bind')} := {scrutinee}")
                lines.append(f"\t_ = {_v3_ident(wbind, 'match bind')}")
            else:
                lines.append(f"\t_ = {scrutinee}")
            lines.append(f"\treturn {_go_v3_expr(wild.get('body'), ctx, expected)}")
            lines.append("}()")
            return "\n".join(lines)

    # classify the scrutinee's ADT family
    is_opt = isinstance(st, str) and st.startswith("Opt[") and st.endswith("]")
    is_result = isinstance(st, str) and st.startswith("Result[") and st.endswith("]")
    opt_inner = st[4:-1] if is_opt else None
    res_ok = res_err = None
    if is_result:
        res_ok, res_err = _v3_split_generic(st[7:-1])

    lines = [f"func() {exp_t} {{"]
    # item 313: a scrutinee that is not a bare identifier — an Opt/Result
    # CONSTRUCTOR LITERAL such as `match Ok(1) { .. }` — renders to a composite
    # literal (`RevlOk[int64, any]{Value: 1}`). Placed straight into the
    # type-switch init clause that is invalid Go twice over: the `{` of the
    # composite is read as the switch body (`expected '}', found Value`), and
    # `.(type)` requires an interface but the composite is a concrete case
    # struct. Bind it to an interface-typed temp first so the switch both
    # parses and can discriminate; an identifier scrutinee keeps the inline
    # form byte-for-byte (a variable is already interface-typed).
    scrut_kind = scrut_node.get("kind") if isinstance(scrut_node, dict) else None
    if scrut_kind in ("var", "name"):
        switch_operand = scrutinee
    else:
        iface_t = (_go_v3_type(st, ctx.types) if st else "") or "any"
        lines.append(f"\tvar _s {iface_t} = {scrutinee}")
        switch_operand = "_s"
    lines.append(f"\tswitch _m := {switch_operand}.(type) {{")
    has_wild = False
    for arm in arms:
        pattern = arm.get("pattern")
        bind = arm.get("bind")
        if pattern == "_":
            has_wild = True
            lines.append("\tdefault:")
            lines.append("\t\t_ = _m")
            body = _go_v3_expr(arm.get("body"), ctx, expected)
            lines.append(f"\t\treturn {body}")
            continue
        # determine the case struct type and the bound payload's surface type
        if is_opt:
            got = _go_v3_type(opt_inner, ctx.types)
            case_type = (f"RevlSome[{got}]" if pattern == "Some"
                         else f"RevlNone[{got}]")
            payload_surface = opt_inner if pattern == "Some" else None
        elif is_result:
            got_ok = _go_v3_type(res_ok, ctx.types)
            got_err = _go_v3_type(res_err, ctx.types)
            case_type = (f"RevlOk[{got_ok}, {got_err}]" if pattern == "Ok"
                         else f"RevlErr[{got_ok}, {got_err}]")
            payload_surface = res_ok if pattern == "Ok" else res_err
        else:
            adt = st if isinstance(st, str) else ctx.case_adt.get(pattern)
            if adt is None:
                raise EmitError(f"cannot resolve match case {pattern!r}")
            case_type = f"{_v3_ident(adt, 'type name')}{pattern}"
            payload_surface = ctx.case_payload.get(pattern)
        lines.append(f"\tcase {case_type}:")
        # `bind == "_"` (`Case(_) => ..`) has no name to hold the value;
        # discard it the same way an unbound arm does, rather than declaring
        # and reading back a literal `_` (Go: "cannot use _ as value").
        if bind and bind != "_":
            ctx.var_types[bind] = payload_surface
            gobind = _v3_ident(bind, "match bind")
            lines.append(f"\t\t{gobind} := _m.Value")
            # A payload bound but never read in the arm body is a Go
            # `declared and not used` build error; pin it so an unused
            # payload compiles, mirroring the wildcard path above (item 304).
            lines.append(f"\t\t_ = {gobind}")
        else:
            lines.append("\t\t_ = _m")
        body = _go_v3_expr(arm.get("body"), ctx, expected)
        lines.append(f"\t\treturn {body}")
    if not has_wild:
        lines.append("\tdefault:")
        lines.append("\t\t_ = _m")
        lines.append('\t\tpanic("unreachable: non-exhaustive match")')
    lines.append("\t}")
    lines.append("}()")
    return "\n".join(lines)


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


# ---------------------------------------------------------------------------
# The self-rebind (unique-ownership) analysis, roadmap item 434 (a) and (b).
#
# `out = out.push(x)` and `m = m.set(k, v)` lower through revlListPush /
# revlMapSet, which COPY: the correct lowering for a persistent value, and a
# quadratic for the loop idiom, where the previous value is dead the instant
# the assignment lands. Measured, building a 1000-element list: 1001 allocs /
# 4,274,103 B against a hand-written `append`'s 1 / 8,192; 1000 map insertions:
# 3989 / 19,168,881 against an in-place `m[k] = v`'s 5 / 54,609.
#
# A destructive lowering is only sound where nothing else can observe the
# write, so this recognises the shape that makes the value UNIQUELY OWNED by
# the binding rather than special-casing the assignment. A name qualifies when
# BOTH hold over the whole function body:
#
#   1. every write to it is either a fresh allocation this function made (a
#      list literal or a `Map.empty()`/map literal) or a self-rebind through
#      one of the write builtins below, with at least one of each; and
#   2. every other occurrence of the name is a read that retains no reference
#      to the value: the receiver of a read-only builtin, an index target, a
#      `for ... of` iterable, or the returned expression.
#
# Together these keep the value linear: it is never bound to a second name,
# passed as an argument, stored into another collection, or captured, so no
# alias exists to see an in-place write. `return` is allowed because the rule
# closes over calls too. A caller binds the result with a `let` whose value is
# a CALL, not a fresh literal, so the caller's binding never qualifies and
# never writes destructively into what it was handed.
_V3_LINEAR_READ_METHODS = frozenset({
    "length", "indexOf", "slice", "concat", "join", "size", "keys",
    "lookup", "has", "get", "contains",
})
# method -> the collection shape it writes, and the destructive Go statement.
_V3_LINEAR_WRITE_METHODS = {"push": "List", "set": "Map", "remove": "Map"}


def _v3_walk_nodes(node):
    """Every dict reachable from `node`: statements and expressions alike."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _v3_walk_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _v3_walk_nodes(value)


def _v3_self_rebind_locals(fn_node: dict) -> dict:
    """The `{name: shape}` map of uniquely-owned collection locals in `fn_node`."""
    body = fn_node.get("body") or []
    shape: dict = {}
    seeded: set = set()
    rebound: set = set()
    disqualified: set = {p.get("name") for p in fn_node.get("params") or []}
    permitted: set = set()  # id() of the `var` nodes an occurrence may sit at

    for stmt in _v3_walk_nodes(body):
        if stmt.get("step") not in ("let", "assign"):
            continue
        name = stmt.get("name")
        value = stmt.get("value")
        value = value if isinstance(value, dict) else {}
        kind = value.get("kind")
        if kind in ("list", "maplit"):
            found = "List" if kind == "list" else "Map"
            target = None
        elif kind == "builtin" and value.get("method") in _V3_LINEAR_WRITE_METHODS:
            target = value.get("target")
            if not (isinstance(target, dict) and target.get("kind") == "var"
                    and target.get("name") == name):
                disqualified.add(name)
                continue
            found = _V3_LINEAR_WRITE_METHODS[value["method"]]
        else:
            disqualified.add(name)
            continue
        if shape.setdefault(name, found) != found:
            disqualified.add(name)
            continue
        if target is None:
            seeded.add(name)
        else:
            rebound.add(name)
            permitted.add(id(target))

    for node in _v3_walk_nodes(body):
        candidates = []
        if node.get("kind") == "builtin" and node.get("method") in _V3_LINEAR_READ_METHODS:
            candidates.append(node.get("target"))
        elif node.get("kind") in ("index", "len"):
            candidates.append(node.get("target"))
        elif node.get("step") == "for":
            candidates.append(node.get("iterable"))
        elif node.get("step") == "return":
            candidates.append(node.get("expr"))
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("kind") == "var":
                permitted.add(id(candidate))

    for node in _v3_walk_nodes(body):
        if node.get("kind") == "var" and id(node) not in permitted:
            disqualified.add(node.get("name"))

    return {name: found for name, found in shape.items()
            if name not in disqualified and name in seeded and name in rebound}


def _go_v3_self_rebind(node: dict, ctx: _V3GoCtx, name: str):
    """The destructive Go statement for a qualifying self-rebind, else None.

    `_v3_self_rebind_locals` has already decided that the binding uniquely owns
    its value, so the copy the persistent helper makes is unobservable and the
    in-place form is the faithful lowering.
    """
    raw = node.get("name")
    if raw not in ctx.linear_locals:
        return None
    value = node.get("value")
    if not (isinstance(value, dict) and value.get("kind") == "builtin"):
        return None
    method = value.get("method")
    target = value.get("target") or {}
    if method not in _V3_LINEAR_WRITE_METHODS or target.get("name") != raw:
        return None
    args = [_go_v3_expr(a, ctx) for a in value.get("args") or []]
    if method == "push":
        return f"{name} = append({name}, {args[0]})"
    if method == "set":
        return f"{name}[{args[0]}] = {args[1]}"
    return f"delete({name}, {args[0]})"


def _go_v3_stmt(node: dict, ctx: _V3GoCtx, out: list, indent: int, *, t_name=None) -> None:
    pad = "\t" * indent
    step = node.get("step")
    if step == "let":
        name = _v3_ident(node.get("name"), "binding")
        inferred = _go_v3_infer_type(node.get("value"), ctx)
        if inferred is not None:
            ctx.var_types[node.get("name")] = inferred
        value = _go_v3_expr(node.get("value"), ctx, inferred)
        # A bare integer/float literal binding defaults to Go `int`/`float64`
        # via `:=`, which then mismatches the int64/float64 the tier uses for
        # Int/Float. Pin the type so later arithmetic against typed values (a
        # function parameter, a loop element) stays well-typed.
        go_t = _go_v3_type(inferred, ctx.types) if inferred else ""
        if go_t in ("int64", "float64"):
            out.append(f"{pad}var {name} {go_t} = {value}")
        elif inferred and _go_v3_is_interface(inferred, ctx.types):
            # An Opt/Result/variant binding must hold its *interface* type, not
            # the concrete case struct `:=` would infer (e.g. `RevlSome[int64]`)
            # — otherwise a later `match` type-switch on it is a Go compile
            # error ("not an interface") and never discriminates (item 280).
            out.append(f"{pad}var {name} {go_t} = {value}")
        else:
            out.append(f"{pad}{name} := {value}")
        out.append(f"{pad}_ = {name}")
    elif step == "assign":
        raw = node.get("name")
        name = _v3_ident(raw, "binding")
        destructive = _go_v3_self_rebind(node, ctx, name)
        if destructive is not None:
            out.append(f"{pad}{destructive}")
        else:
            value = _go_v3_expr(node.get("value"), ctx, ctx.var_types.get(raw))
            out.append(f"{pad}{name} = {value}")
    elif step == "return":
        if node.get("expr") is None:
            out.append(f"{pad}return")
        else:
            out.append(f"{pad}return {_go_v3_expr(node['expr'], ctx, ctx.ret_type)}")
    elif step == "if":
        out.append(f"{pad}if {_go_v3_expr(node['cond'], ctx)} {{")
        for child in node.get("then") or []:
            _go_v3_stmt(child, ctx, out, indent + 1, t_name=t_name)
        if node.get("else"):
            out.append(f"{pad}}} else {{")
            for child in node["else"]:
                _go_v3_stmt(child, ctx, out, indent + 1, t_name=t_name)
        out.append(f"{pad}}}")
    elif step == "while":
        _guard_frame_neutral_loop(node.get("body"))
        out.append(f"{pad}for {_go_v3_expr(node['cond'], ctx)} {{")
        for child in node.get("body") or []:
            _go_v3_stmt(child, ctx, out, indent + 1, t_name=t_name)
        out.append(f"{pad}}}")
    elif step == "for":
        _guard_frame_neutral_loop(node.get("body"))
        bind = _v3_ident(node.get("bind"), "loop binding")
        it_node = node.get("iterable")
        it_t = _go_v3_infer_type(it_node, ctx)
        if isinstance(it_t, str) and it_t.startswith("List[") and it_t.endswith("]"):
            ctx.var_types[node.get("bind")] = it_t[5:-1]
        out.append(f"{pad}for _, {bind} := range {_go_v3_expr(it_node, ctx)} {{")
        out.append(f"{pad}\t_ = {bind}")
        for child in node.get("body") or []:
            _go_v3_stmt(child, ctx, out, indent + 1, t_name=t_name)
        out.append(f"{pad}}}")
    elif step == "break":
        out.append(f"{pad}break")
    elif step == "continue":
        out.append(f"{pad}continue")
    elif step == "expr":
        out.append(f"{pad}_ = {_go_v3_expr(node['expr'], ctx)}")
    elif step == "assert":
        expr = _go_v3_expr(node["expr"], ctx)
        tn = t_name or "t"
        out.append(f"{pad}if !({expr}) {{")
        out.append(f'{pad}\t{tn}.Fatalf("assertion failed: %s", {_go_string(expr)})')
        out.append(f"{pad}}}")
    else:
        raise EmitError(f"unsupported v3 statement step {step!r}")


def _go_v3_test_name(name, used: set) -> str:
    raw = name if isinstance(name, str) else str(name)
    base = "Test"
    for part in re.split(r"[^A-Za-z0-9]+", raw):
        if part:
            base += part[:1].upper() + part[1:]
    if base == "Test":
        base = "TestCase"
    candidate = base
    i = 1
    while candidate in used:
        i += 1
        candidate = f"{base}{i}"
    used.add(candidate)
    return candidate


def _emit_v3_go_types(types: dict) -> list[str]:
    out: list[str] = []
    for name, spec in types.items():
        gname = _v3_ident(name, "type name")
        if spec.get("kind") == "record":
            out.append(f"type {gname} struct {{")
            # Exported + json-tagged on EVERY path (item 390): `encoding/json`
            # ignores unexported fields, so a record must expose exported Go
            # fields to marshal at all, and the `json:"<revl-name>"` tag pins the
            # wire key to the source field name so json_stringify(record) is
            # byte-identical to py/ts. (Formerly only the placement/bridge path
            # tagged these; the pure tier left them unexported, which is the `{}`
            # defect this fixes.) Guard against two source fields colliding onto
            # one exported Go identifier — that would silently drop a field.
            seen: dict[str, str] = {}
            for field, ftype in (spec.get("fields") or {}).items():
                gfield = _v3_field_ident(field)
                if gfield in seen:
                    raise EmitError(
                        f"record {name!r}: fields {seen[gfield]!r} and {field!r} "
                        f"both lower to the exported Go field {gfield!r}; rename "
                        "one so record fields stay distinct on the go tier")
                seen[gfield] = field
                out.append(f"\t{gfield} {_go_v3_type(ftype, types)}"
                           f" `json:\"{field}\"`")
            out.append("}")
            out.append("")
        elif spec.get("kind") == "variant":
            marker = f"is{gname}"
            out.append(f"// {gname} is a sealed sum type (revl ADT); {marker}() seals it.")
            out.append(f"type {gname} interface {{ {marker}() }}")
            for case in spec.get("cases") or []:
                cname = case.get("name")
                cstruct = f"{gname}{cname}"
                payload = case.get("payload")
                if payload is None:
                    out.append(f"type {cstruct} struct{{}}")
                else:
                    out.append(f"type {cstruct} struct {{ Value {_go_v3_type(payload, types)} }}")
                out.append(f"func ({cstruct}) {marker}() {{}}")
            out.append("")
        else:
            raise EmitError(f"unsupported v3 type kind {spec.get('kind')!r} for {name!r}")
    return out


# item 378 Stage 5: package-level config seam for document-global config
# externs. Mirrors the py tier's `_REVL_EXTERN_CONFIG` map + fail-loud
# `_revl_extern_config` helper: a mutable package-global config map, keyed by
# extern name, that a composition driver fills at plug time, and a lookup that
# PANICS, naming the extern, when a required (non-defaulted) field is absent,
# instead of handing the body a zero value that fails opaquely later. A
# defaults-only extern still resolves to its defaults driver-free. The string
# joins are open-coded so the seam needs no `strings` import (the extern body's
# own imports are hoisted separately). Emitted only when a config extern is
# present, so a no-config program is byte-identical.
_GO_EXTERN_CONFIG_SCAFFOLD = [
    "var _REVL_EXTERN_CONFIG = map[string]map[string]any{}",
    "",
    "func _revlExternConfig(name string, required []string, "
    "defaults map[string]any) map[string]any {",
    "\tout := map[string]any{}",
    "\tfor k, v := range defaults {",
    "\t\tout[k] = v",
    "\t}",
    "\tcfg, ok := _REVL_EXTERN_CONFIG[name]",
    "\tif !ok {",
    "\t\tif len(required) > 0 {",
    "\t\t\tmsg := \"\"",
    "\t\t\tfor i, f := range required {",
    "\t\t\t\tif i > 0 {",
    "\t\t\t\t\tmsg += \", \"",
    "\t\t\t\t}",
    "\t\t\t\tmsg += f",
    "\t\t\t}",
    "\t\t\tpanic(\"config extern `\" + name + \"` called before plug-time \" +",
    "\t\t\t\t\"configuration was installed (required config: \" + msg + \"); \" +",
    "\t\t\t\t\"configure it through the run driver's config seam\")",
    "\t\t}",
    "\t\treturn out",
    "\t}",
    "\tmissing := \"\"",
    "\tn := 0",
    "\tfor _, f := range required {",
    "\t\tif _, present := cfg[f]; !present {",
    "\t\t\tif n > 0 {",
    "\t\t\t\tmissing += \", \"",
    "\t\t\t}",
    "\t\t\tmissing += f",
    "\t\t\tn++",
    "\t\t}",
    "\t}",
    "\tif n > 0 {",
    "\t\tpanic(\"config extern `\" + name + \"` called before plug-time \" +",
    "\t\t\t\"configuration was installed (missing required config: \" + "
    "missing + \")\")",
    "\t}",
    "\tfor k, v := range cfg {",
    "\t\tout[k] = v",
    "\t}",
    "\treturn out",
    "}",
    "",
]


def _go_extern_config_bind(ext: dict) -> str:
    """The `_revl_config := ...` first-body line for a config extern, or None.
    `_revl_config` is a `map[string]any`; the verbatim @go body reads a field as
    `_revl_config["field"]` and asserts its type, exactly as the py body reads
    the resolved dict."""
    schema = ext.get("config")
    if not schema:
        return None
    name = ext.get("name")
    required = [f["name"] for f in schema if f.get("default") is None]
    defaults = {f["name"]: f["default"] for f in schema
                if f.get("default") is not None}
    req_lit = "[]string{%s}" % ", ".join(_go_string(f) for f in required)
    return (f"_revl_config := _revlExternConfig("
            f"{_go_string(name)}, {req_lit}, {_go_literal(defaults)})")


def _emit_v3_go_externs(externs: list, ctx: _V3GoCtx) -> list[str]:
    out: list[str] = []
    # item 378 Stage 5: emit the config seam once, before the externs, when any
    # extern carries a config schema (byte-identical when none do).
    if any(ext.get("config") for ext in externs):
        out.extend(_GO_EXTERN_CONFIG_SCAFFOLD)
    for ext in externs:
        name = _v3_ident(ext.get("name"), "extern name")
        params = ", ".join(
            f"{_v3_ident(p.get('name'), 'extern parameter')} {_go_v3_type(p.get('type'), ctx.types)}"
            for p in ext.get("params") or []
        )
        ret = _go_v3_type(ext.get("returns"), ctx.types)
        sig_ret = f" {ret}" if ret else ""
        bodies = ext.get("bodies") or {}
        # Require a native @go body — like the rust (@rs) and java (@java) tiers.
        # Never fall back to another tier's body: emitting @ts text as Go only
        # "works" for trivial expressions and would silently ship broken Go for
        # anything else. A missing @go body is a portability boundary, not a gap.
        if "go" not in bodies:
            raise EmitError(
                f"extern `{name}` has no @go body — not portable to this backend "
                f"(available: {', '.join(sorted(bodies)) or 'none'})"
            )
        body = bodies["go"].strip()
        out.append(f"func {name}({params}){sig_ret} {{")
        # item 378 Stage 5: a config extern binds `_revl_config` as the first
        # body line; None for a no-config extern (byte-identical body splice).
        config_bind = _go_extern_config_bind(ext)
        if config_bind:
            out.append("\t" + config_bind)
        body_lines = _hoist_go_imports(body, ctx) if body else []
        if body_lines:
            for line in body_lines:
                out.append("\t" + line.rstrip())
        else:
            out.append("\t// (empty extern body)")
        out.append("}")
        out.append("")
    return out


# A verbatim extern body cannot spell its own `import` (Go allows imports only
# at file scope, and the emitter hoists the module's imports into one block).
# The seam is a directive line — `//revl:import encoding/json` — that a @go
# body places among its statements: the emitter records the package on the
# module's import set and drops the directive from the emitted function body.
# It is a comment to Go's own lexer, so a body that skipped emit (e.g. copied
# by hand) still compiles; here it is structured data the emitter acts on.
_GO_IMPORT_DIRECTIVE = re.compile(r'^\s*//revl:import\s+(.+?)\s*$')


def _hoist_go_imports(body: str, ctx: "_V3GoCtx") -> list[str]:
    """Split a @go extern body into emittable lines, pulling any
    `//revl:import <path>` directive out to the module import set (ctx)."""
    kept: list[str] = []
    for line in body.splitlines():
        m = _GO_IMPORT_DIRECTIVE.match(line)
        if m:
            path = m.group(1).strip().strip('"')
            if path:
                ctx.extern_imports.add(path)
            continue
        kept.append(line)
    return kept


def _emit_v3_go_functions(functions: list, ctx: _V3GoCtx) -> list[str]:
    out: list[str] = []
    for fn in functions:
        name = _v3_ident(fn.get("name"), "function name")
        ctx.var_types = {p.get("name"): p.get("type") for p in fn.get("params") or []}
        ctx.ret_type = fn.get("returns")
        ctx.linear_locals = _v3_self_rebind_locals(fn)
        params = ", ".join(
            f"{_v3_ident(p.get('name'), 'parameter')} {_go_v3_type(p.get('type'), ctx.types)}"
            for p in fn.get("params") or []
        )
        ret = _go_v3_type(fn.get("returns"), ctx.types)
        sig_ret = f" {ret}" if ret else ""
        out.append(f"func {name}({params}){sig_ret} {{")
        for stmt in fn.get("body") or []:
            _go_v3_stmt(stmt, ctx, out, 1)
        out.append("}")
        out.append("")
    return out


def _emit_v3_go_tests(tests: list, ctx: _V3GoCtx) -> list[str]:
    out: list[str] = []
    used: set = set()
    for test in tests:
        tname = _go_v3_test_name(test.get("name"), used)
        ctx.var_types = {}
        ctx.ret_type = None
        ctx.linear_locals = _v3_self_rebind_locals(test)
        # `revlT` (not `t`) so a user binding named `t` can't shadow it.
        out.append(f"func {tname}(revlT *testing.T) {{")
        for stmt in test.get("body") or []:
            _go_v3_stmt(stmt, ctx, out, 1, t_name="revlT")
        out.append("}")
        out.append("")
    return out


# The witnessed/compensation teardown accumulator (items 243/247, docs/
# design/teardown-contract.md) — the go mirror of backends/python/runtime.py's
# `Frame`. One `RevlFrame` per activation, created at the top of `Apply`
# whenever the component uses a witnessed effect or a compensation; a
# component using neither never allocates one (byte-identical emission).
#
# `committed` is written exactly once, synchronously, by the commit-marker
# inverse (`_revlFrame.commit()`, returned as Apply's own outer Inverse — see
# `_emit_component`) before any other registered inverse in this activation's
# stack runs; every later read (from the SAME unwind, the SAME goroutine) is
# therefore ordered after that write by plain program order, with no data
# race and no lock needed on the field itself.
#
# Phase 2 (`runCompensationPhase`) is registered as the FIRST `ctx.Effect`
# call, so on stc-go's LIFO unwind it is the LAST inverse to run in this
# activation's stack — after every bracket/transactional inverse and every
# compensation's Phase-1 enqueue have already happened, i.e. Phase 1 always
# completes in full before Phase 2 starts (docs/design/teardown-contract.md,
# "why two phases", reason 1).
_TEARDOWN_PREAMBLE = '''// ---- witnessed/compensation teardown accumulator (items 243/247, docs/design/teardown-contract.md) ----

// RevlTeardownRecord is one entry of the merged residue schema (docs/design/
// teardown-contract.md, "the merged residue schema"): a Phase-1 inverse that
// failed (`bracket-fault` / `restore-residue`) or a Phase-2 compensation that
// failed, timed out, or was never attempted (`compensation-residue`).
type RevlTeardownRecord struct {
	Kind           string // "restore-residue" | "bracket-fault" | "compensation-residue"
	CrossingKey    string
	CrossingMethod string
	AttemptedCall  string
	AttemptedPhase int    // 1 or 2
	ErrorType      string // e.g. "panic" | "deadline-expired" | "per-call-timeout"
	ErrorMessage   string
	Outcome        string // "failed" | "unknown" | "not-attempted"
	Referent       string // what is still out in the world
	Hint           string // recovery hint for the operator
}

// revlCompEntry is one queued Phase-2 compensation: the offsetting call,
// captured at registration (`emit X compensate Y`), never re-read at
// teardown (docs/design/teardown-contract.md, "no data hazard").
type revlCompEntry struct {
	key    string
	method string
	run    func() error
}

// RevlFrame is one component activation's teardown accumulator: the
// commit-vs-abort bit, the Phase-2 compensation queue, and the surfaced
// residue. Bracket inverses need none of this — they stay plain `ctx.Effect`
// disposers, unchanged.
type RevlFrame struct {
	committed bool // see the package-level note above: single-writer, no lock
	mu        sync.Mutex
	pending   []revlCompEntry
	residue   []RevlTeardownRecord
}

func newRevlFrame() *RevlFrame {
	return &RevlFrame{}
}

// commit flips the discriminator a clean unload reads (item 243 decision 1,
// the go mirror of `Frame._committed`/`Frame.drain`): every transactional
// entry and every compensation observes `committed == true` from here on,
// meaning discharge instead of replay/run.
func (f *RevlFrame) commit() {
	f.committed = true
}

// enqueue defers one compensation to Phase 2 (item 247): called from a
// compensation's inverse when the activation is aborting, never on a commit
// (the commit branch returns before reaching this). Entries queue in the
// order stc-go's unwind visits them, which is already reverse-registration
// (LIFO) order — "LIFO within itself" (docs/design/teardown-contract.md)
// falls out for free from the enqueue order, no re-sorting needed.
func (f *RevlFrame) enqueue(key, method string, run func() error) {
	f.mu.Lock()
	f.pending = append(f.pending, revlCompEntry{key: key, method: method, run: run})
	f.mu.Unlock()
}

func (f *RevlFrame) recordResidue(rec RevlTeardownRecord) {
	f.mu.Lock()
	f.residue = append(f.residue, rec)
	f.mu.Unlock()
}

// Residue is a snapshot of the merged residue records this activation's
// abort teardown surfaced (docs/design/teardown-contract.md, "surface
// (residue)"). Empty on a clean commit or when nothing failed.
func (f *RevlFrame) Residue() []RevlTeardownRecord {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]RevlTeardownRecord, len(f.residue))
	copy(out, f.residue)
	return out
}

// revlNoCompensationBound is the "0 means no bound" sentinel: a Duration long
// enough that a timer armed with it never meaningfully fires (~292 years),
// so `runOneCompensation`'s select always resolves via the real call instead.
const revlNoCompensationBound = time.Duration(1<<63 - 1)

// revlEnvDurationMS reads a `REVL_*_MS` env var once per call (docs/design/
// teardown-contract.md, "Budget values… read once at activation"; reading it
// per Phase-2 run is equivalent for a value that a process never mutates
// mid-run, and keeps the accumulator free of package-level init ordering).
// Unset or unparsable falls back to `defaultMS`; `"0"` returns 0, the
// caller's "no bound" signal.
func revlEnvDurationMS(name string, defaultMS int64) time.Duration {
	v, ok := os.LookupEnv(name)
	if !ok || v == "" {
		return time.Duration(defaultMS) * time.Millisecond
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil || n < 0 {
		return time.Duration(defaultMS) * time.Millisecond
	}
	return time.Duration(n) * time.Millisecond
}

// runCompensationPhase is Phase 2 of the two-phase abort (docs/design/
// teardown-contract.md): best-effort, bounded, and it never runs on a clean
// commit (a5a — discharge, the queue is simply never drained). Every queued
// compensation not yet started when the budget expires is recorded
// `compensation-residue` with `error: deadline-expired`, `attempted: false` —
// every skip is recorded, nothing silently dropped.
func (f *RevlFrame) runCompensationPhase() {
	if f.committed {
		return
	}
	f.mu.Lock()
	pending := f.pending
	f.pending = nil
	f.mu.Unlock()
	if len(pending) == 0 {
		return
	}

	budgetMS := revlEnvDurationMS("REVL_COMPENSATION_BUDGET_MS", 5000)
	perCallMS := revlEnvDurationMS("REVL_COMPENSATION_PER_CALL_MS", 1000)
	hasDeadline := budgetMS != 0
	var deadline time.Time
	if hasDeadline {
		deadline = time.Now().Add(budgetMS)
	}

	for _, entry := range pending {
		if hasDeadline && !time.Now().Before(deadline) {
			f.recordResidue(RevlTeardownRecord{
				Kind: "compensation-residue", CrossingKey: entry.key, CrossingMethod: entry.method,
				ErrorType: "deadline-expired",
				ErrorMessage: "the phase-2 compensation budget (REVL_COMPENSATION_BUDGET_MS) expired " +
					"before this compensation started",
				Outcome: "not-attempted", AttemptedPhase: 2, Referent: entry.key,
				Hint: "the phase-2 budget expired before " + entry.key + "." + entry.method +
					" ran; verify and finish by hand",
			})
			continue
		}
		bound := revlNoCompensationBound
		if perCallMS != 0 {
			bound = perCallMS
		}
		if hasDeadline {
			if remaining := time.Until(deadline); remaining < bound {
				bound = remaining
			}
		}
		f.runOneCompensation(entry, bound)
	}
}

// runOneCompensation runs one Phase-2 compensation with go's normative
// in-call preemption: abandon-the-wait (docs/design/teardown-contract.md,
// per-tier table). The call runs in its own goroutine; the runtime waits up
// to `bound` and, on timeout, stops waiting and moves on to the next
// compensation — the call keeps running DETACHED, recorded `outcome:
// unknown` (the emission may still land after this abort completed).
//
// The goroutine MUST recover its own panic and route it over the channel
// (go's per-tier obligation, docs/design/teardown-contract.md, "Per-tier
// loop obligations"): an unrecovered panic in a detached goroutine kills the
// whole process, turning a best-effort phase into a crash. And abandonment
// relaxes seriality — once one compensation is abandoned and the next one
// starts, both may be running concurrently; "LIFO within itself" pins the
// START order of Phase-2 compensations only, never mutual exclusion.
func (f *RevlFrame) runOneCompensation(entry revlCompEntry, bound time.Duration) {
	done := make(chan error, 1) // buffered: an abandoned goroutine's send never blocks
	go func() {
		defer func() {
			if r := recover(); r != nil {
				done <- fmt.Errorf("panic: %v", r)
			}
		}()
		done <- entry.run()
	}()

	if bound < 0 {
		bound = 0
	}
	timer := time.NewTimer(bound)
	defer timer.Stop()
	select {
	case err := <-done:
		if err != nil {
			f.recordResidue(RevlTeardownRecord{
				Kind: "compensation-residue", CrossingKey: entry.key, CrossingMethod: entry.method,
				ErrorType: "compensation-failed", ErrorMessage: err.Error(),
				Outcome: "failed", AttemptedCall: entry.method, AttemptedPhase: 2, Referent: entry.key,
				Hint: "the compensation " + entry.key + "." + entry.method +
					" failed; verify and finish by hand",
			})
		}
	case <-timer.C:
		// abandon-the-wait: the call keeps running detached (its own
		// panic-guard already contains whatever it does).
		f.recordResidue(RevlTeardownRecord{
			Kind: "compensation-residue", CrossingKey: entry.key, CrossingMethod: entry.method,
			ErrorType:    "per-call-timeout",
			ErrorMessage: "compensation exceeded its per-call bound (REVL_COMPENSATION_PER_CALL_MS); abandoned in flight",
			Outcome:      "unknown", AttemptedCall: entry.method, AttemptedPhase: 2, Referent: entry.key,
			Hint: "the compensation " + entry.key + "." + entry.method +
				" was abandoned in flight; it may still land — verify by hand",
		})
	}
}
'''


# item 318: the extended teardown accumulator, emitted in place of the base
# preamble when some provide-METHOD registers a witnessed effect (per-tool-call
# H1). It adds, to the base `RevlFrame`, exactly the state the py reference tier
# carries in `Frame._deferred_transactional`/`_aborting` and disposes in
# `Frame.drain`:
#
#   * `aborting` — the session-level abort discriminator (item 245's reject
#     seam). A component that activated cleanly reaches `commit()` on ANY later
#     unload and would implicitly commit every parked inverse; `Abort()` sets
#     this first so `commit()` leaves `committed` false and each parked inverse
#     replays instead.
#   * `deferred` — the parked provide-method witnessed inverses. They are NOT
#     stc-go disposers (registering one as a sibling `ctx.Effect` after
#     activation lands it LATER in the LIFO stack than the commit marker, so a
#     clean unload runs it with `committed` still false and wrongly reverts the
#     deliverable — the disposal-ordering hazard). `commit()` (the commit
#     marker, which stc-go runs FIRST on unwind because it is registered LAST)
#     disposes them AFTER settling `committed`, the go mirror of `Frame.drain`
#     disposing `_deferred_transactional`.
#   * a package-level frame registry — the go mirror of the py tier's
#     `_frame_for_ctx`, the seam a session/test reaches a live frame through to
#     `Abort()` it before unload.
#
# The two field additions and the `commit()` rewrite mean the base preamble text
# cannot be reused verbatim; the injections below are exact-string replacements
# on the base so any drift in the base fails loudly at emit time.
_METHOD_WITNESSED_STRUCT_BASE = '''type RevlFrame struct {
	committed bool // see the package-level note above: single-writer, no lock
	mu        sync.Mutex
	pending   []revlCompEntry
	residue   []RevlTeardownRecord
}'''

_METHOD_WITNESSED_STRUCT_EXT = '''type RevlFrame struct {
	committed bool // see the package-level note above: single-writer, no lock
	aborting  bool // item 318: session-level abort seam — see (*RevlFrame).Abort
	mu        sync.Mutex
	pending   []revlCompEntry
	residue   []RevlTeardownRecord
	deferred  []func() error // item 318: parked provide-method witnessed inverses
}'''

_METHOD_WITNESSED_COMMIT_BASE = '''func (f *RevlFrame) commit() {
	f.committed = true
}'''

_METHOD_WITNESSED_COMMIT_EXT = '''func (f *RevlFrame) commit() {
	// item 318: an Abort() may have requested revert of already-applied
	// per-tool-call work; honour it before flipping the bit every parked and
	// activation-body inverse reads.
	if !f.aborting {
		f.committed = true
	}
	// Dispose the parked provide-method witnessed inverses HERE, now that the
	// commit-vs-abort bit is settled (the go mirror of Frame.drain disposing
	// `_deferred_transactional`): on a commit each discharges (mutation
	// persists, witness GC'd), on an abort each replays (reverts). They are not
	// stc-go disposers, so this is their sole disposal — no double-free with the
	// fiber's own unwind. commit() is the LAST-registered inverse, hence stc-go
	// runs it FIRST on unwind, so this is ordered before any body inverse runs.
	//
	// item 369: replay in reverse INVOCATION order (LIFO), NOT registration
	// order. `deferred` is appended newest-last as each provide-method fires
	// (registerMethodWitnessed), so it must be drained newest-FIRST — exactly
	// like the activation-body path, where stc-go unwinds its disposer stack
	// LIFO. On a COMMIT order is immaterial (every entry no-op discharges); on
	// an ABORT two inverses whose paths OVERLAP must undo newest-first or a FIFO
	// replay leaves residue or DESTROYS pre-session data (every stdlib/fs.rvl
	// inverse is idempotent-and-total, so the oldest inverse runs first, no-ops,
	// and the newer one undoes into the hole — G7, 243 §2). Mirrors the py/ts
	// runtimes.
	f.mu.Lock()
	deferred := f.deferred
	f.deferred = nil
	f.mu.Unlock()
	for i := len(deferred) - 1; i >= 0; i-- {
		_ = deferred[i]()
	}
}'''

_METHOD_WITNESSED_NEWFRAME_BASE = '''func newRevlFrame() *RevlFrame {
	return &RevlFrame{}
}'''

_METHOD_WITNESSED_NEWFRAME_EXT = '''// item 318: a package-level registry of every activation frame created, the go
// mirror of the py reference tier's `_frame_for_ctx` — the seam a session/test
// reaches a live frame through to Abort() it before unload (item 245's reject
// UX will drive this in production; the H1 exec test drives it directly).
var (
	_revlFrameRegMu sync.Mutex
	_revlFrameReg   []*RevlFrame
)

func newRevlFrame() *RevlFrame {
	f := &RevlFrame{}
	_revlFrameRegMu.Lock()
	_revlFrameReg = append(_revlFrameReg, f)
	_revlFrameRegMu.Unlock()
	return f
}

// RevlFrames returns a snapshot of every activation frame created since the
// last RevlResetFrames — the seam for reaching a live frame to Abort it.
func RevlFrames() []*RevlFrame {
	_revlFrameRegMu.Lock()
	defer _revlFrameRegMu.Unlock()
	out := make([]*RevlFrame, len(_revlFrameReg))
	copy(out, _revlFrameReg)
	return out
}

// RevlResetFrames clears the frame registry (call between scenarios, like
// HostReset), so a test can find its own activation's sole frame.
func RevlResetFrames() {
	_revlFrameRegMu.Lock()
	_revlFrameReg = nil
	_revlFrameRegMu.Unlock()
}

// Abort marks this activation as ABORTING (item 318, the go mirror of
// Frame.abort): its next teardown replays every parked per-tool-call witnessed
// inverse instead of committing it. Idempotent; a plain unload never calls it,
// so a commit stays a commit.
func (f *RevlFrame) Abort() {
	f.aborting = true
}

// registerMethodWitnessed parks one provide-method witnessed inverse (item
// 318): a per-tool-call mutation whose rollback must outlive the method call
// and survive until the component/session commits or aborts. It is NOT a
// stc-go disposer (see commit() for why); commit() disposes it once the
// commit-vs-abort bit is settled.
func (f *RevlFrame) registerMethodWitnessed(run func() error) {
	f.mu.Lock()
	f.deferred = append(f.deferred, run)
	f.mu.Unlock()
}

// registerMethodCompensation parks one provide-method `emit ... compensate ...`
// offset (the item-247 method-body remainder): the compensation analog of
// registerMethodWitnessed, and the method-body analog of the activation-body
// `emit ... compensate ...` (item 247). A method body runs AFTER activation, so
// the offset must outlive the method call and is owed ONLY on an abort, never on
// a clean commit (the emission it offsets was the deliverable). It is NOT a
// stc-go disposer (see commit() for the disposal-ordering hazard); commit()
// disposes the parked closure once the commit-vs-abort bit is settled. On a
// COMMIT the closure discharges (never runs). On an ABORT it hands the offset to
// `enqueue`, so runCompensationPhase (registered first, hence run LAST on the
// unwind) fires it in Phase 2 — after every bracket/transactional/method-
// witnessed inverse in this activation has completed, guarded and residue-
// collected. `key`/`method` are the offsetting call's descriptor for the WAL and
// residue, captured here at registration (the "no data hazard" rule).
func (f *RevlFrame) registerMethodCompensation(key, method string, run func() error) {
	f.mu.Lock()
	f.deferred = append(f.deferred, func() error {
		if f.committed {
			return nil // discharge — the emission was the deliverable
		}
		f.enqueue(key, method, run) // abort: defer to Phase 2
		return nil
	})
	f.mu.Unlock()
}'''


# item 322 Slice 1: the go host recording channel. A durable, fsync'd JSON-Lines
# WAL sink whose records the tier-agnostic recovery core (src/revl/wal.py +
# src/revl/recovery.py) reads back to roll a crashed session back. Self-contained
# (os + encoding/json + sync, all already or additionally imported in record
# mode); emitted only when `record=True`, so a non-recording program never
# carries it and stays byte-identical. Opened from the REVL_WAL env var at
# process start; if that is unset the sink is nil and every record call is a
# no-op, so a record-mode binary run without REVL_WAL behaves exactly as a
# non-recording one. The record shapes match the py writer (backends/python/
# replay.py) field-for-field: `discharge-descriptor` (the re-issuable named call
# for one transactional inverse), `discharge` (the commit-path proof recover
# skips), and the terminal `activation-complete` marker.
_RECORD_PREAMBLE = '''// ---- durable WAL recording sink (item 322 Slice 1, the go host recording channel) ----

const revlWALGuarantee = "the WAL records each committed effect's step identity, boundary " +
	"classification and inverse DESCRIPTOR (not its closure). On restart, " +
	"recovery runs the reconstructible boundary inverses newest-first (LIFO); " +
	"in-process inverses are moot (their captured memory died with the " +
	"process) and closure-only boundary inverses are reported as residue, " +
	"never silently claimed to have run."

// revlWAL is the process's durable append-only log. One line per record, JSON,
// flushed + fsync'd before the call that wrote it returns — the write-ahead
// discipline the py tier uses, so a record a caller saw acknowledged is on disk
// before the effect it describes is allowed to matter.
type revlWAL struct {
	mu   sync.Mutex
	f    *os.File
	seq  int
	seqs []int
}

var revlWALSink *revlWAL

// revlWALOpen wires the sink to REVL_WAL (unset -> no-op recording) and stamps
// the header. Called once from this package's init.
func revlWALOpen() {
	path := os.Getenv("REVL_WAL")
	if path == "" {
		return
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return
	}
	revlWALSink = &revlWAL{f: f}
	revlWALSink.write(map[string]any{
		"record": "header", "walVersion": 1, "generation": 1,
		"guarantee": revlWALGuarantee,
	})
}

func init() { revlWALOpen() }

func (w *revlWAL) write(rec map[string]any) {
	line, err := json.Marshal(rec)
	if err != nil {
		return
	}
	_, _ = w.f.Write(append(line, '\\n'))
	_ = w.f.Sync()
}

// revlRecordTransactional appends the discharge-descriptor for one witnessed
// transactional inverse: the re-issuable named call {receiver, method, args}
// recover replays LIFO to undo the mutation, plus the forward `origin` it
// reverses. Fsync'd before it returns, so a crash after this call still leaves
// the inverse re-issuable from the log alone.
func revlRecordTransactional(receiver, method string, args []string) {
	if revlWALSink == nil {
		return
	}
	w := revlWALSink
	w.mu.Lock()
	defer w.mu.Unlock()
	seq := w.seq
	w.seq++
	w.seqs = append(w.seqs, seq)
	ia := make([]any, len(args))
	for i, a := range args {
		ia[i] = a
	}
	call := map[string]any{"receiver": receiver, "method": method, "args": ia}
	w.write(map[string]any{
		"record": "discharge-descriptor", "seq": seq, "entry": "transactional",
		"call": call, "origin": call, "witness": nil, "idempotency": nil,
	})
}

// revlRecordDischarge writes the commit-path proof that every recorded
// transactional seq COMMITTED, so recover SKIPS it — a committed transaction is
// never rolled back. Called on a clean unload, never on a crash.
func revlRecordDischarge() {
	if revlWALSink == nil {
		return
	}
	w := revlWALSink
	w.mu.Lock()
	defer w.mu.Unlock()
	ia := make([]any, len(w.seqs))
	for i, s := range w.seqs {
		ia[i] = s
	}
	w.write(map[string]any{"record": "discharge", "discharged": ia})
}

// revlRecordActivationComplete stamps the terminal marker: its presence is the
// whole roll-forward decision, its absence (a crash) is roll-back. Written only
// after a clean unload.
func revlRecordActivationComplete() {
	if revlWALSink == nil {
		return
	}
	w := revlWALSink
	w.mu.Lock()
	defer w.mu.Unlock()
	w.write(map[string]any{"record": "activation-complete", "generation": 1, "components": []any{}})
}
'''


def _teardown_preamble(method_witnessed: bool) -> str:
    """The teardown accumulator preamble. Byte-identical to the base
    `_TEARDOWN_PREAMBLE` unless a provide-method registers a witnessed effect,
    in which case the frame gains the item-318 deferred/abort state (see the
    injection constants above)."""
    if not method_witnessed:
        return _TEARDOWN_PREAMBLE
    s = _TEARDOWN_PREAMBLE
    for base, ext in (
        (_METHOD_WITNESSED_STRUCT_BASE, _METHOD_WITNESSED_STRUCT_EXT),
        (_METHOD_WITNESSED_COMMIT_BASE, _METHOD_WITNESSED_COMMIT_EXT),
        (_METHOD_WITNESSED_NEWFRAME_BASE, _METHOD_WITNESSED_NEWFRAME_EXT),
    ):
        if base not in s:
            raise EmitError(
                "item 318: teardown preamble drifted — cannot inject the "
                "method-witnessed extension (base fragment not found)")
        s = s.replace(base, ext, 1)
    return s


# The clock coeffect + timer scheduler (item 57), the go mirror of
# backends/python/runtime.py's Clock/TimerHandle — deterministic tick-for-tick.
# Time advances only when RevlClockAdvance is called; a firing is a step in the
# timeline, never a wall-clock race, so replay is reproducible. Arming records
# `timer#N.schedule` and takes a live-resource slot (revlHostAcquire); cancel /
# a spent `after` records `timer#N.cancel` and returns the slot — so a leaked
# `every` timer surfaces through the same R1 residue accounting a leaked Pool
# does. Emitted only when a component body carries a `timer` step.
_TIMER_PREAMBLE = '''// ---- clock coeffect + timer scheduler (item 57, docs/time-coeffect.md) --
// The go mirror of backends/python/runtime.py's Clock/TimerHandle: time moves
// only on RevlClockAdvance, firing due timers earliest-first (ties by arm
// order) and re-arming `every` across the span — deterministic for replay.

type RevlTimer struct {
	serial     int
	mode       string // "every" | "after"
	intervalMs int64
	body       func()
	state      string // "live" | "cancelled" | "done"
	nextAt     int64
	fired      int
}

func (t *RevlTimer) tag() string { return fmt.Sprintf("timer#%d", t.serial) }

// Cancel is the schedule's inverse — idempotent, and a no-op once the timer is
// spent (a fired `after`). Running it on teardown proves the schedule leaves no
// residue: it returns the live-resource slot arming took.
func (t *RevlTimer) Cancel() bool {
	_revlClockMu.Lock()
	defer _revlClockMu.Unlock()
	if t.state != "live" {
		return false
	}
	t.state = "cancelled"
	hostRecord(t.tag() + ".cancel")
	revlHostRelease()
	return true
}

var (
	_revlClockMu      sync.Mutex
	_revlClockNow     int64
	_revlClockSerial  int
	_revlClockTimers  []*RevlTimer
	_revlClockFirings [][2]int64 // (timer serial, fired-at ms) in fire order
)

func revlSchedule(mode string, intervalMs int64, body func()) *RevlTimer {
	_revlClockMu.Lock()
	defer _revlClockMu.Unlock()
	_revlClockSerial++
	t := &RevlTimer{
		serial: _revlClockSerial, mode: mode, intervalMs: intervalMs,
		body: body, state: "live", nextAt: _revlClockNow + intervalMs,
	}
	_revlClockTimers = append(_revlClockTimers, t)
	hostRecord(fmt.Sprintf("%s.schedule %s %dms", t.tag(), mode, intervalMs))
	revlHostAcquire()
	return t
}

// revlScheduleEvery arms a periodic timer against the clock coeffect (`every`).
func revlScheduleEvery(intervalMs int64, body func()) *RevlTimer {
	return revlSchedule("every", intervalMs, body)
}

// revlScheduleAfter arms a one-shot delayed timer against the clock (`after`).
func revlScheduleAfter(intervalMs int64, body func()) *RevlTimer {
	return revlSchedule("after", intervalMs, body)
}

// RevlClockAdvance advances logical time by ms, firing every timer that comes
// due — earliest first, ties broken by arm order — and re-arming `every` timers
// across the whole span. Returns the number of firings.
func RevlClockAdvance(ms int64) int {
	_revlClockMu.Lock()
	target := _revlClockNow + ms
	count := 0
	for {
		var due *RevlTimer
		for _, t := range _revlClockTimers {
			if t.state != "live" || t.nextAt > target {
				continue
			}
			if due == nil || t.nextAt < due.nextAt ||
				(t.nextAt == due.nextAt && t.serial < due.serial) {
				due = t
			}
		}
		if due == nil {
			break
		}
		_revlClockNow = due.nextAt
		if due.mode == "every" {
			due.nextAt += due.intervalMs
		}
		due.fired++
		_revlClockFirings = append(_revlClockFirings,
			[2]int64{int64(due.serial), _revlClockNow})
		hostRecord(fmt.Sprintf("%s.fire #%d at %dms", due.tag(), due.fired, _revlClockNow))
		body := due.body
		_revlClockMu.Unlock()
		body() // run the firing body without the clock lock (scope: emissions)
		_revlClockMu.Lock()
		if due.mode == "after" {
			// a one-shot is spent once it fires; release through the same
			// `cancel` verb so the residue trace stays balanced and teardown's
			// own Cancel() is a clean no-op.
			due.state = "done"
			hostRecord(due.tag() + ".cancel")
			revlHostRelease()
		}
		count++
	}
	_revlClockNow = target
	_revlClockMu.Unlock()
	return count
}

// RevlClockNow is the current logical time in ms.
func RevlClockNow() int64 {
	_revlClockMu.Lock()
	defer _revlClockMu.Unlock()
	return _revlClockNow
}

// RevlClockPending counts live timers — a teardown that abandons one leaves
// this > 0 (the countable no-orphaned-interval proof).
func RevlClockPending() int {
	_revlClockMu.Lock()
	defer _revlClockMu.Unlock()
	n := 0
	for _, t := range _revlClockTimers {
		if t.state == "live" {
			n++
		}
	}
	return n
}

// RevlClockFirings is the recorded firing log: (timer serial, fired-at ms).
func RevlClockFirings() [][2]int64 {
	_revlClockMu.Lock()
	defer _revlClockMu.Unlock()
	out := make([][2]int64, len(_revlClockFirings))
	copy(out, _revlClockFirings)
	return out
}

// RevlClockReset returns the clock to time zero with no timers (call between
// scenarios, like HostReset).
func RevlClockReset() {
	_revlClockMu.Lock()
	defer _revlClockMu.Unlock()
	_revlClockNow = 0
	_revlClockSerial = 0
	_revlClockTimers = nil
	_revlClockFirings = nil
}
'''


# ---- Stream[T] on the blocking tier (item 130 Slice 3) --------------------
#
# docs/design/130-stream-reactive-types.md §4.6, the go row: this tier ERASES
# the async color. `next` is a `select` on the item channel and the CANCEL
# channel; `close` closes the cancel channel. That is the whole reason the
# blocking tiers are their own slice — the select is what makes the bracket
# inverse reachable off the teardown goroutine (§9 Part A), so a `next` parked
# on a provider that never emits cannot make teardown deadlock behind it.
#
# The two guarantees this preamble is accountable for, and where they live:
#
#   * cancellation-first `next` (§9 Part A) — `Subscription.Next` probes the
#     cancel channel BEFORE the buffer, then parks in the three-case select
#     whose cancel case `Close` closes. `Close` is synchronous and never waits
#     for the park to drain.
#   * a terminal always reaches an outstanding `next` (§9 Part B) — a provider
#     `Close`/`Fault` delivers `Closed`/`Faulted` to every live subscription,
#     and `merge` counts its upstreams so a fan-in whose last source closes
#     still delivers `Closed` rather than stranding the consumer.
#
# Emitted only for a document that reaches a stream (`_COMP_NEEDS_STREAM`), so
# every stream-free program on this tier stays byte-identical (exit test §10.9).
_STREAM_PREAMBLE = '''// ---- Stream[T] reactive host (item 130, docs/design/130-stream-reactive-types.md)
// The blocking-tier lowering: `next` is a cancel-channel select, `close` closes
// the cancel channel. Unloading the subscription owner runs `Close` (the bracket
// inverse), which resolves a parked `Next` as the `Closed` terminal — the core
// guarantee, delivered by the same LIFO teardown any bracket rides.

// streamClosedT is the `Closed` terminal — a VALUE, mirroring the py reference
// tier's STREAM_CLOSED sentinel. A `Faulted` terminal is an error instead, so
// the two outcomes cannot be confused at a call site.
type streamClosedT struct{}

// StreamClosed is the singleton `Closed` terminal value.
var StreamClosed any = streamClosedT{}

// IsStreamClosed reports whether a `Next` result is the Closed terminal.
func IsStreamClosed(v any) bool { _, ok := v.(streamClosedT); return ok }

// StreamBufferCapacity is the bounded buffer every subscription gets: there are
// no unbounded buffers (design §4.4). Overflow under the default `error` policy
// is a `Faulted(overflow)` terminal — deterministic, never a silent drop.
const StreamBufferCapacity = 8

var (
	_revlStreamMu   sync.Mutex
	_revlStreams    []*Stream
	_revlStreamSubs []*Subscription
)

// Stream is the PROVIDER side: a source (`Stream.source()`) or the derived
// stream behind `subscribe merge(a, b)`. Both are the same object, so a merged
// stream is itself a terminal-delivering provider another `merge` can take.
type Stream struct {
	mu          sync.Mutex
	kind        string // "source" | "merge"
	subs        []*Subscription
	down        []*Stream // merged streams fed by this one
	up          []*Stream // sources feeding this merged stream
	pending     int       // upstream sources not yet terminal (merged only)
	state       string    // "open" | "closed" | "faulted"
	faultReason string
	released    bool
}

// StreamSource opens a provider. It takes a live-resource slot; `Close`
// returns it, so a source that outlives its owner shows up as residue.
func StreamSource() *Stream {
	s := &Stream{kind: "source", state: "open"}
	_revlStreamMu.Lock()
	_revlStreams = append(_revlStreams, s)
	_revlStreamMu.Unlock()
	hostRecord("stream.source open")
	revlHostAcquire()
	return s
}

// StreamMerge is the fan-in behind `subscribe merge(a, b)` (design §1). The
// merged stream is NOT a bracket of its own: it is DERIVED, owned by the
// subscription opened on it, so multi-source teardown rides the ONE bracket the
// `subscribe` registers. The subscription's Close closes the merge; closing the
// merge detaches it from both sources; each source is left to its own bracket.
// One LIFO stack, no orphaned fan-in, and no source left feeding — or holding a
// reference to — a merged stream whose owner is gone.
func StreamMerge(a *Stream, b *Stream) *Stream {
	m := &Stream{kind: "merge", state: "open", pending: 2, up: []*Stream{a, b}}
	_revlStreamMu.Lock()
	_revlStreams = append(_revlStreams, m)
	_revlStreamMu.Unlock()
	hostRecord("stream.merge open")
	revlHostAcquire()
	a.attachDown(m)
	b.attachDown(m)
	return m
}

func (s *Stream) attachDown(m *Stream) {
	s.mu.Lock()
	if s.state != "open" {
		state, reason := s.state, s.faultReason
		s.mu.Unlock()
		m.upstreamTerminal(state, reason)
		return
	}
	s.down = append(s.down, m)
	s.mu.Unlock()
}

func (s *Stream) detachDown(m *Stream) {
	s.mu.Lock()
	for i, d := range s.down {
		if d == m {
			s.down = append(s.down[:i], s.down[i+1:]...)
			break
		}
	}
	s.mu.Unlock()
}

func (s *Stream) detach(sub *Subscription) {
	s.mu.Lock()
	for i, x := range s.subs {
		if x == sub {
			s.subs = append(s.subs[:i], s.subs[i+1:]...)
			break
		}
	}
	s.mu.Unlock()
}

// Emit delivers one item to the single consumer (and into any merged stream fed
// by this provider). A no-op once terminal.
func (s *Stream) Emit(item string) bool {
	s.mu.Lock()
	open := s.state == "open"
	s.mu.Unlock()
	if !open {
		return false
	}
	hostRecord("stream.emit " + item)
	s.forward(item)
	return true
}

func (s *Stream) forward(item string) {
	s.mu.Lock()
	if s.state != "open" {
		s.mu.Unlock()
		return
	}
	subs := append([]*Subscription(nil), s.subs...)
	downs := append([]*Stream(nil), s.down...)
	s.mu.Unlock()
	for _, sub := range subs {
		sub.deliver(item)
	}
	for _, d := range downs {
		d.forward(item)
	}
}

// Close is the provider's terminal-delivering inverse (§9 Part B) and, for a
// merged stream, its detach from both upstreams. Idempotent, and the
// live-resource slot is released exactly once — so a provider that FAULTED and
// is then unloaded still leaves no residue.
func (s *Stream) Close() bool {
	s.mu.Lock()
	first := s.state == "open"
	if first {
		s.state = "closed"
	}
	release := !s.released
	s.released = true
	kind := s.kind
	subs := append([]*Subscription(nil), s.subs...)
	downs := append([]*Stream(nil), s.down...)
	ups := append([]*Stream(nil), s.up...)
	s.up = nil
	s.mu.Unlock()
	if first {
		for _, sub := range subs {
			sub.terminate("closed", "")
		}
		for _, d := range downs {
			d.upstreamTerminal("closed", "")
		}
	}
	// A merged stream leaves its upstreams on the way out. A DERIVED upstream
	// (a nested `merge`) is owned by this one, so it closes with it; a plain
	// source is left to its own bracket.
	for _, u := range ups {
		u.detachDown(s)
		if u.kind != "source" {
			u.Close()
		}
	}
	if release {
		hostRecord("stream." + kind + " close")
		revlHostRelease()
	}
	return first
}

// Fault is a provider abort: every outstanding `Next` resolves to `Faulted`,
// never a silent pending (design §4.3). It does not release the slot — the
// bracket inverse `Close` still runs on teardown and releases it there.
func (s *Stream) Fault(reason string) bool {
	s.mu.Lock()
	if s.state != "open" {
		s.mu.Unlock()
		return false
	}
	s.state = "faulted"
	s.faultReason = reason
	subs := append([]*Subscription(nil), s.subs...)
	downs := append([]*Stream(nil), s.down...)
	s.mu.Unlock()
	hostRecord("stream." + s.kind + " fault " + reason)
	for _, sub := range subs {
		sub.terminate("faulted", reason)
	}
	for _, d := range downs {
		d.upstreamTerminal("faulted", reason)
	}
	return true
}

// upstreamTerminal is how a merged stream learns one of its sources is done.
// A FAULT propagates at once (no silent loss). An orderly CLOSE only counts
// down: the fan-in stays live while any source is, so one source's death never
// strands a consumer the other source can still feed — and when the LAST source
// closes, the merged stream delivers its own `Closed`, so a parked `Next` is
// terminated rather than left waiting on a dead fan-in.
func (m *Stream) upstreamTerminal(kind string, reason string) {
	m.mu.Lock()
	if m.state != "open" {
		m.mu.Unlock()
		return
	}
	if kind == "faulted" {
		m.state = "faulted"
		m.faultReason = reason
	} else {
		m.pending--
		if m.pending > 0 {
			m.mu.Unlock()
			return
		}
		m.state = "closed"
		kind = "closed"
	}
	subs := append([]*Subscription(nil), m.subs...)
	downs := append([]*Stream(nil), m.down...)
	m.mu.Unlock()
	for _, sub := range subs {
		sub.terminate(kind, reason)
	}
	for _, d := range downs {
		d.upstreamTerminal(kind, reason)
	}
}

// Subscription is the CONSUMER side: a single-consumer acquisition whose
// inverse is `Close`. `items` is the bounded buffer, `cancel` is the cancel
// channel teardown closes, `term` carries a provider terminal.
type Subscription struct {
	src    *Stream
	policy string
	items  chan string
	cancel chan struct{}
	term   chan struct{}
	mu     sync.Mutex
	kind   string // "" | "closed" | "faulted"
	reason string
	closed bool
	termed bool
}

// StreamSubscribe opens the single-consumer subscription a `subscribe` bracket
// binds. `capacity` is the declared `buffer` (0 = the default); every buffer is
// BOUNDED either way, since there are no unbounded buffers (design §4.4).
// Subscribing to an already-terminal provider terminates immediately, so the
// first `Next` cannot park on a provider that is already gone.
func StreamSubscribe(src *Stream, policy string, capacity int) *Subscription {
	if capacity <= 0 {
		capacity = StreamBufferCapacity
	}
	sub := &Subscription{
		src:    src,
		policy: policy,
		items:  make(chan string, capacity),
		cancel: make(chan struct{}),
		term:   make(chan struct{}),
	}
	src.mu.Lock()
	src.subs = append(src.subs, sub)
	terminal := src.state != "open"
	state, reason := src.state, src.faultReason
	src.mu.Unlock()
	_revlStreamMu.Lock()
	_revlStreamSubs = append(_revlStreamSubs, sub)
	_revlStreamMu.Unlock()
	hostRecord("stream.subscribe")
	revlHostAcquire()
	if terminal {
		sub.terminate(state, reason)
	}
	return sub
}

func (sub *Subscription) deliver(item string) {
	sub.mu.Lock()
	dead := sub.closed || sub.termed
	sub.mu.Unlock()
	if dead {
		return
	}
	select {
	case sub.items <- item:
	default:
		switch sub.policy {
		case "", "error":
			hostRecord("stream.overflow")
			sub.terminate("faulted", "overflow")
		default:
			panic("revl: backpressure policy " + sub.policy +
				" is not lowered on the cordis-go tier")
		}
	}
}

func (sub *Subscription) terminate(kind string, reason string) {
	sub.mu.Lock()
	if sub.closed || sub.termed {
		sub.mu.Unlock()
		return
	}
	sub.termed = true
	sub.kind, sub.reason = kind, reason
	sub.mu.Unlock()
	close(sub.term)
}

// Next parks until an item, a provider terminal, or the cancel channel.
// Returns the item; `StreamClosed` on a `Closed` terminal (an orderly provider
// close, or the owner's own `Close` tripping the cancel channel); an error on a
// `Faulted` one.
//
// CANCELLATION-FIRST (§9 Part A): the cancel channel is probed BEFORE the
// buffer, so a `Close` racing a buffered item still wins. Go's `select` picks
// among ready cases at RANDOM, which is exactly why the priority is spelled as
// non-blocking probes ahead of the blocking select instead of left to it.
func (sub *Subscription) Next() (any, error) {
	select {
	case <-sub.cancel:
		return StreamClosed, nil
	default:
	}
	select {
	case item := <-sub.items:
		return item, nil
	default:
	}
	select {
	case <-sub.term:
		return sub.terminal()
	default:
	}
	// The blocking cancel-channel select. Whichever arrives first ends the park,
	// and the cancel case is the one TEARDOWN closes from another goroutine —
	// the reason a parked `Next` can never make the bracket inverse unreachable.
	select {
	case <-sub.cancel:
		return StreamClosed, nil
	case item := <-sub.items:
		return item, nil
	case <-sub.term:
		return sub.terminal()
	}
}

func (sub *Subscription) terminal() (any, error) {
	sub.mu.Lock()
	kind, reason := sub.kind, sub.reason
	sub.mu.Unlock()
	if kind == "faulted" {
		if reason == "" {
			reason = "faulted"
		}
		return nil, fmt.Errorf("stream faulted: %s", reason)
	}
	return StreamClosed, nil
}

// Close is the bracket inverse: trip the cancel channel synchronously, detach
// the listener, release the slot. Infallible, idempotent, and it NEVER waits
// for a parked `Next` to drain — closing `cancel` is what resolves that park.
//
// A DERIVED upstream (a `merge(a, b)` fan-in) is owned by this subscription
// rather than by a bracket of its own, so closing here closes it too — and
// closing a merge is what detaches it from both sources. The sources stay on
// their own brackets, which keeps the LIFO close-order proof exact.
func (sub *Subscription) Close() bool {
	sub.mu.Lock()
	if sub.closed {
		sub.mu.Unlock()
		return false
	}
	sub.closed = true
	sub.mu.Unlock()
	close(sub.cancel)
	sub.src.detach(sub)
	hostRecord("stream.close")
	revlHostRelease()
	if sub.src.kind != "source" {
		sub.src.Close()
	}
	return true
}

// StreamPending is the residue probe: unreleased providers plus live
// (un-closed) subscriptions. Zero after a clean unload proves every bracket
// inverse ran and no host listener outlived its owner.
func StreamPending() int {
	_revlStreamMu.Lock()
	streams := append([]*Stream(nil), _revlStreams...)
	subs := append([]*Subscription(nil), _revlStreamSubs...)
	_revlStreamMu.Unlock()
	n := 0
	for _, s := range streams {
		s.mu.Lock()
		if !s.released {
			n++
		}
		s.mu.Unlock()
	}
	for _, sub := range subs {
		sub.mu.Lock()
		if !sub.closed {
			n++
		}
		sub.mu.Unlock()
	}
	return n
}

// StreamProviders is the harness handle on this package's providers in opening
// order, so a scenario can drive one (Emit/Close/Fault) from ANOTHER goroutine
// — the go mirror of the py reference's `Stream.sources()`.
func StreamProviders() []*Stream {
	_revlStreamMu.Lock()
	defer _revlStreamMu.Unlock()
	return append([]*Stream(nil), _revlStreams...)
}

// StreamLiveSubscriptions counts subscriptions whose `Close` has not run.
func StreamLiveSubscriptions() int {
	_revlStreamMu.Lock()
	subs := append([]*Subscription(nil), _revlStreamSubs...)
	_revlStreamMu.Unlock()
	n := 0
	for _, sub := range subs {
		sub.mu.Lock()
		if !sub.closed {
			n++
		}
		sub.mu.Unlock()
	}
	return n
}

// StreamReset clears the provider/subscription registry (call between scenarios).
func StreamReset() {
	_revlStreamMu.Lock()
	_revlStreams = nil
	_revlStreamSubs = nil
	_revlStreamMu.Unlock()
}
'''


# The pure-tier runtime preamble. Groups are emitted only when used, but every
# helper here is an ordinary package-level declaration — Go never errors on an
# unused func/type, only on unused imports (which is why the group flags gate
# the `import` block, not the helpers).
_V3_OPT_PREAMBLE = '''// ---- built-in Opt as a generic sealed interface -----------------------
type RevlOpt[T any] interface{ isRevlOpt() }
type RevlSome[T any] struct{ Value T }

func (RevlSome[T]) isRevlOpt() {}

type RevlNone[T any] struct{}

func (RevlNone[T]) isRevlOpt() {}

func revlOptMap[A any, B any](o RevlOpt[A], f func(A) B) RevlOpt[B] {
\tif s, ok := o.(RevlSome[A]); ok {
\t\treturn RevlSome[B]{Value: f(s.Value)}
\t}
\treturn RevlNone[B]{}
}

func revlOptOr[T any](o RevlOpt[T], d T) T {
\tif s, ok := o.(RevlSome[T]); ok {
\t\treturn s.Value
\t}
\treturn d
}
'''

# Str.to_int (FR-9, docs/stdlib-2.0.md §Str.to_int): total on the ASCII digits
# with an optional leading `-`, RevlNone otherwise. Parsed by hand (no
# strconv) so the helper needs no import; the uint64 accumulator allows the
# one out-of-i64-magnitude digit string that is still IN range — `-9223372036854775808`
# (Int.MIN) — while every larger magnitude is None, matching the Int bound.
_V3_PARSE_INT_HELPER = '''// ---- Str.to_int (FR-9, docs/stdlib-2.0.md §Str.to_int) ----------------
func revlParseInt(s string) RevlOpt[int64] {
\tif s == "" {
\t\treturn RevlNone[int64]{}
\t}
\tneg := false
\ti := 0
\tif s[0] == '-' {
\t\tneg = true
\t\ti = 1
\t\tif len(s) == 1 {
\t\t\treturn RevlNone[int64]{}
\t\t}
\t}
\tconst lim = uint64(9223372036854775808) // Int.MAX + 1
\tvar n uint64
\tfor ; i < len(s); i++ {
\t\tc := s[i]
\t\tif c < '0' || c > '9' {
\t\t\treturn RevlNone[int64]{}
\t\t}
\t\tn = n*10 + uint64(c-'0')
\t\tif n > lim {
\t\t\treturn RevlNone[int64]{}
\t\t}
\t}
\tif neg {
\t\tif n == lim {
\t\t\treturn RevlSome[int64]{Value: -9223372036854775807 - 1}
\t\t}
\t\treturn RevlSome[int64]{Value: -int64(n)}
\t}
\tif n == lim {
\t\treturn RevlNone[int64]{}
\t}
\treturn RevlSome[int64]{Value: int64(n)}
}
'''

_V3_RESULT_PREAMBLE = '''// ---- built-in Result as a generic sealed interface --------------------
type RevlResult[T any, E any] interface{ isRevlResult() }
type RevlOk[T any, E any] struct{ Value T }

func (RevlOk[T, E]) isRevlResult() {}

type RevlErr[T any, E any] struct{ Value E }

func (RevlErr[T, E]) isRevlResult() {}
'''

_V3_MAP_PREAMBLE = '''// ---- Map value type (docs/stdlib-2.0.md §Map): persistent Go maps -----
func revlMapSet[K comparable, V any](m map[K]V, k K, v V) map[K]V {
	out := make(map[K]V, len(m)+1)
	for kk, vv := range m {
		out[kk] = vv
	}
	out[k] = v
	return out
}

func revlMapGet[K comparable, V any](m map[K]V, k K) RevlOpt[V] {
	if v, ok := m[k]; ok {
		return RevlSome[V]{Value: v}
	}
	return RevlNone[V]{}
}

func revlMapHas[K comparable, V any](m map[K]V, k K) bool {
	_, ok := m[k]
	return ok
}

func revlMapRemove[K comparable, V any](m map[K]V, k K) map[K]V {
	out := make(map[K]V, len(m))
	for kk, vv := range m {
		if kk != k {
			out[kk] = vv
		}
	}
	return out
}

// revlMapKeys yields the keys in ascending canonical Str order (UTF-8 byte
// lexicographic, which go `string <` is exactly, and slices.Sort orders
// []string by `<`, so the emitted order is identical to the hand-rolled
// insertion sort this replaced). Map.keys() IS the iteration surface for Map
// (docs/stdlib-2.0.md §Map), so it sits on the path of every map traversal a
// program does: O(n log n), not the O(n^2) the "keys come in small sets"
// premise assumed (roadmap item 434 (h)).
func revlMapKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}
'''

_V3_FTOA_HELPER = r'''// revlFtoa renders a Float as ECMAScript Number::toString does (the canonical
// cross-tier Float -> Str form, docs/strings.md): shortest round-trip digits,
// "1e+21"/"NaN"/"Infinity", a whole-number float as "0", negative zero as "0".
func revlFtoa(x float64) string {
	if math.IsNaN(x) {
		return "NaN"
	}
	if math.IsInf(x, 1) {
		return "Infinity"
	}
	if math.IsInf(x, -1) {
		return "-Infinity"
	}
	if x == 0 {
		return "0"
	}
	sign := ""
	if x < 0 {
		sign = "-"
		x = -x
	}
	s := strconv.FormatFloat(x, 'e', -1, 64) // shortest mantissa: d[.ddd]e±dd
	mant := s
	exp := 0
	if e := strings.IndexByte(s, 'e'); e >= 0 {
		mant = s[:e]
		exp, _ = strconv.Atoi(s[e+1:])
	}
	intpart := mant
	frac := ""
	if d := strings.IndexByte(mant, '.'); d >= 0 {
		intpart = mant[:d]
		frac = mant[d+1:]
	}
	digits := intpart + frac
	point := len(intpart) + exp
	i := 0
	for i < len(digits)-1 && digits[i] == '0' {
		i++
		point--
	}
	digits = digits[i:]
	j := len(digits)
	for j > 1 && digits[j-1] == '0' {
		j--
	}
	digits = digits[:j]
	if digits == "0" {
		return "0"
	}
	k := len(digits)
	n := point
	var body string
	switch {
	case k <= n && n <= 21:
		body = digits + strings.Repeat("0", n-k)
	case 0 < n && n <= 21:
		body = digits[:n] + "." + digits[n:]
	case -6 < n && n <= 0:
		body = "0." + strings.Repeat("0", -n) + digits
	default:
		e := n - 1
		m := digits
		if k > 1 {
			m = digits[:1] + "." + digits[1:]
		}
		esign := "+"
		if e < 0 {
			esign = "-"
			e = -e
		}
		body = m + "e" + esign + strconv.Itoa(e)
	}
	return sign + body
}'''

_V3_STDLIB_PREAMBLE = '''// ---- stdlib builtins (docs/stdlib-2.0.md); positions are code-point based
func revlStrLen(s string) int64 { return int64(utf8.RuneCountInString(s)) }

func revlStrSlice(s string, a, b int64) string {
\tr := []rune(s)
\tn := int64(len(r))
\tif a < 0 {
\t\ta = 0
\t}
\tif a > n {
\t\ta = n
\t}
\tif b > n {
\t\tb = n
\t}
\tif b < a {
\t\tb = a
\t}
\treturn string(r[a:b])
}

func revlStrIndexOf(s, sub string) int64 {
\trs := []rune(s)
\trn := []rune(sub)
\tif len(rn) == 0 {
\t\treturn 0
\t}
\tif len(rn) > len(rs) {
\t\treturn -1
\t}
\tfor i := 0; i+len(rn) <= len(rs); i++ {
\t\tok := true
\t\tfor j := range rn {
\t\t\tif rs[i+j] != rn[j] {
\t\t\t\tok = false
\t\t\t\tbreak
\t\t\t}
\t\t}
\t\tif ok {
\t\t\treturn int64(i)
\t\t}
\t}
\treturn -1
}

func revlStrConcat(a, b string) string { return a + b }

func revlStrSplit(s, sep string) []string {
\tif sep == "" {
\t\tr := []rune(s)
\t\tout := make([]string, len(r))
\t\tfor i, c := range r {
\t\t\tout[i] = string(c)
\t\t}
\t\treturn out
\t}
\treturn strings.Split(s, sep)
}

func revlStrRepeat(s string, n int64) string {
\tif n < 0 {
\t\tn = 0
\t}
\treturn strings.Repeat(s, int(n))
}

// revlStrCharAt / revlStrCharCodeAt walk to the code point at index i with
// utf8.DecodeRuneInString instead of materializing `[]rune(s)`. Both are
// code-point indexed exactly as before (docs/strings.md); the difference is
// that one read no longer allocates a copy of the whole string, so a scan loop
// stops being quadratic in bytes (item 434 (c): a 780-code-point scan measured
// 781 allocs / 2,496,127 B, one whole-string rune copy per character, against
// 0 / 0 for the hand-written `for _, r := range s`). An out-of-range index
// falls through to the `[]rune(s)[i]` it always was, so it panics with the
// same Go index-out-of-range error on exactly the same inputs.
func revlStrCharAt(s string, i int64) string {
\tif i >= 0 {
\t\tt := s
\t\tfor j := int64(0); len(t) > 0; j++ {
\t\t\tr, w := utf8.DecodeRuneInString(t)
\t\t\tif j == i {
\t\t\t\tif r == utf8.RuneError && w == 1 {
\t\t\t\t\t// invalid encoding: []rune(s) substitutes U+FFFD, so
\t\t\t\t\t// answer the replacement character, not the raw byte
\t\t\t\t\treturn string(utf8.RuneError)
\t\t\t\t}
\t\t\t\treturn t[:w] // a substring shares s's bytes: no allocation
\t\t\t}
\t\t\tt = t[w:]
\t\t}
\t}
\treturn string([]rune(s)[i])
}

func revlStrCharCodeAt(s string, i int64) int64 {
\tif i >= 0 {
\t\tt := s
\t\tfor j := int64(0); len(t) > 0; j++ {
\t\t\tr, w := utf8.DecodeRuneInString(t)
\t\t\tif j == i {
\t\t\t\treturn int64(r)
\t\t\t}
\t\t\tt = t[w:]
\t\t}
\t}
\treturn int64([]rune(s)[i])
}

func revlJoin(xs []string, sep string) string { return strings.Join(xs, sep) }

func revlListLen[T any](xs []T) int64 { return int64(len(xs)) }

func revlListSlice[T any](xs []T, a, b int64) []T {
\tn := int64(len(xs))
\tif a < 0 {
\t\ta = 0
\t}
\tif a > n {
\t\ta = n
\t}
\tif b > n {
\t\tb = n
\t}
\tif b < a {
\t\tb = a
\t}
\tout := make([]T, b-a)
\tcopy(out, xs[a:b])
\treturn out
}

func revlListConcat[T any](a, b []T) []T {
\tout := make([]T, 0, len(a)+len(b))
\tout = append(out, a...)
\tout = append(out, b...)
\treturn out
}

func revlListPush[T any](xs []T, x T) []T {
\tout := make([]T, 0, len(xs)+1)
\tout = append(out, xs...)
\tout = append(out, x)
\treturn out
}

func revlListIndexOf[T comparable](xs []T, x T) int64 {
\tfor i, v := range xs {
\t\tif v == x {
\t\t\treturn int64(i)
\t\t}
\t}
\treturn -1
}
'''


def _emit_v3_go(ir: dict, package: str) -> str:
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    if not types and not functions and not externs and not tests:
        raise EmitError(
            "v3 IR document has no types, functions, externs, or tests to emit"
        )

    ctx = _V3GoCtx(types, functions, externs)
    # Render bodies first so feature flags (fmt / stdlib) settle before imports.
    body: list[str] = []
    if types:
        body.extend(_emit_v3_go_types(types))
    if externs:
        body.extend(_emit_v3_go_externs(externs, ctx))
    if functions:
        body.extend(_emit_v3_go_functions(functions, ctx))
    if tests:
        body.extend(_emit_v3_go_tests(tests, ctx))

    blob = json.dumps({
        "types": types, "functions": functions,
        "externs": externs, "tests": tests,
    })
    used_opt = ("Opt[" in blob) or ('"optfield"' in blob) or ('"optcall"' in blob)
    # Str.to_int answers a RevlOpt too: the helper's return type needs the
    # Opt preamble in the module even though no `Opt[` ever appears in the
    # source (the builtin's result type is carried by the checker, not the IR).
    if ctx.needs_parse_int:
        used_opt = True
    # The Map value type (docs/stdlib-2.0.md §Map): its helpers answer Opt
    # (`lookup`), so using any of them pulls the Opt preamble in too.
    used_map = ('"maplit"' in blob) or any(
        f'"method": "{m}"' in blob for m in ("set", "lookup", "has",
                                             "size", "keys", "remove"))
    if used_map:
        used_opt = True
    # The total division forms produce a Result value without the source ever
    # spelling `Result[` — their method names put Result in the module too.
    used_result = ("Result[" in blob) or ("checked_div_" in blob) or ("checked_mod" in blob)
    # A `Some(literal)` / `None` / `Ok`/`Err` with no `Opt[`/`Result[` spelled
    # in the IR (element type recovered from the argument, item 280) still
    # emits the sealed types — scan the rendered body so the matching preamble
    # is never dropped and the module compiles.
    body_blob = "\n".join(body)
    if any(t in body_blob for t in ("RevlOpt[", "RevlSome[", "RevlNone[")):
        used_opt = True
    if any(t in body_blob for t in ("RevlResult[", "RevlOk[", "RevlErr[")):
        used_result = True

    imports: list[str] = []
    if tests:
        imports.append('\t"testing"')
    if ctx.needs_fmt:
        imports.append('\t"fmt"')
    if ctx.needs_reflect:
        imports.append('\t"reflect"')
    if ctx.used_stdlib:
        imports.append('\t"strings"')
        imports.append('\t"unicode/utf8"')
    if ctx.needs_ftoa:
        imports.append('\t"math"')
        imports.append('\t"strconv"')
        imports.append('\t"strings"')
    if ctx.needs_strconv:
        imports.append('\t"strconv"')
    # _V3_MAP_PREAMBLE's revlMapKeys sorts with slices.Sort (item 434 (h)).
    if used_map:
        imports.append('\t"slices"')
    # packages a @go extern body hoisted via `//revl:import` (e.g. encoding/json)
    for path in sorted(ctx.extern_imports):
        imports.append(f'\t"{path}"')

    out: list[str] = []
    out.append("// Code generated by backends/go/emit.py — DO NOT EDIT.")
    out.append("// revl -> cordis-go (ir_version 3, pure typed-core tier): ordinary Go.")
    out.append(f"package {package}")
    out.append("")
    if imports:
        out.append("import (")
        out.extend(sorted(set(imports)))
        out.append(")")
        out.append("")
    if ctx.needs_reflect:
        # Structural equality. Go `==` is a compile error on slices, so a
        # record holding a List cannot use it at all; DeepEqual compares
        # float64 with `==`, so NaN stays unequal to itself as IEEE requires.
        out.append("func revlEq(a, b any) bool { return reflect.DeepEqual(a, b) }")
        out.append("")
    if ctx.needs_float_div:
        # A function, not an expression: Go rejects a *constant* `1.0 / 0.0`
        # at compile time, where IEEE defines +Inf. Through a call it is an
        # ordinary runtime float division, which is what revl specifies.
        out.append("func revlDiv(a, b float64) float64 { return a / b }")
        out.append("")
    if ctx.needs_ftoa:
        out.append(_V3_FTOA_HELPER)
        out.append("")
    if ctx.needs_overflow:
        out.append("func revlAdd(a, b int64) int64 {")
        out.append("\ts := a + b")
        out.append("\tif (a > 0 && b > 0 && s < 0) || (a < 0 && b < 0 && s >= 0) {")
        out.append('\t\tpanic("revl: Int overflow")')
        out.append("\t}")
        out.append("\treturn s")
        out.append("}")
        out.append("")
        out.append("func revlSub(a, b int64) int64 {")
        out.append("\td := a - b")
        out.append("\tif (b < 0 && d < a) || (b > 0 && d > a) {")
        out.append('\t\tpanic("revl: Int overflow")')
        out.append("\t}")
        out.append("\treturn d")
        out.append("}")
        out.append("")
        out.append("func revlMul(a, b int64) int64 {")
        out.append("\tif a == 0 || b == 0 {")
        out.append("\t\treturn 0")
        out.append("\t}")
        out.append("\tp := a * b")
        out.append("\tif p/b != a {")
        out.append('\t\tpanic("revl: Int overflow")')
        out.append("\t}")
        out.append("\treturn p")
        out.append("}")
        out.append("")
    if ctx.needs_overflow32:
        # Int32 traps at the i32 edge (docs/arithmetic.md). Go's int32 wraps,
        # so each op is computed in int64 (which cannot overflow for two i32
        # inputs) and range-checked before narrowing. revlToI32 is the checked
        # Int -> Int32 narrowing.
        out.append("func revlAddI32(a, b int32) int32 { return revlToI32("
                   "int64(a) + int64(b)) }")
        out.append("func revlSubI32(a, b int32) int32 { return revlToI32("
                   "int64(a) - int64(b)) }")
        out.append("func revlMulI32(a, b int32) int32 { return revlToI32("
                   "int64(a) * int64(b)) }")
        out.append("func revlToI32(v int64) int32 {")
        out.append("\tif v < -2147483648 || v > 2147483647 {")
        out.append('\t\tpanic("revl: Int32 overflow")')
        out.append("\t}")
        out.append("\treturn int32(v)")
        out.append("}")
        out.append("")
    if ctx.needs_int_arith:
        out.append("func revlDivTrunc(a, b int64) int64 {")
        out.append("\tif a == (-9223372036854775807 - 1) && b == -1 {")
        out.append('\t\tpanic("revl: Int overflow")')
        out.append("\t}")
        out.append("\treturn a / b")
        out.append("}")
        out.append("")
        out.append("func revlDivFloor(a, b int64) int64 {")
        out.append("\tif a == (-9223372036854775807 - 1) && b == -1 {")
        out.append('\t\tpanic("revl: Int overflow")')
        out.append("\t}")
        out.append("\tq := a / b")
        out.append("\tif a%b != 0 && ((a < 0) != (b < 0)) {")
        out.append("\t\tq--")
        out.append("\t}")
        out.append("\treturn q")
        out.append("}")
        out.append("")
        out.append("func revlDivEuclid(a, b int64) int64 {")
        out.append("\tif b > 0 {")
        out.append("\t\treturn revlDivFloor(a, b)")
        out.append("\t}")
        out.append("\treturn -revlDivFloor(a, -b)")
        out.append("}")
        out.append("")
        out.append("func revlMod(a, b int64) int64 {")
        out.append("\tm := b")
        out.append("\tif m < 0 {")
        out.append("\t\tm = -m")
        out.append("\t}")
        out.append("\tr := a % m")
        out.append("\tif r < 0 {")
        out.append("\t\tr += m")
        out.append("\t}")
        out.append("\treturn r")
        out.append("}")
        out.append("")
    if used_opt:
        out.append(_V3_OPT_PREAMBLE)
    if ctx.needs_parse_int:
        # after the Opt preamble: revlParseInt's return type is RevlOpt
        out.append(_V3_PARSE_INT_HELPER)
    if used_result:
        out.append(_V3_RESULT_PREAMBLE)
    if used_map:
        out.append(_V3_MAP_PREAMBLE)
    if ctx.used_stdlib:
        out.append(_V3_STDLIB_PREAMBLE)
    out.extend(body)
    return "\n".join(out).rstrip() + "\n"


def _refuse_holes(ir: dict) -> None:
    """A typed hole is an unmet obligation, not code (docs/holes.md).

    Emitting one would put a placeholder into Go and make the Go toolchain
    the thing that complains — in its own vocabulary, about a line revl
    wrote. revl already knows the draft is unfinished, so the refusal belongs
    here, before a single character is emitted (mirrors the other five
    backends).
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
        f"refusing to emit Go: this document still has {len(found)} typed "
        f"hole(s) — {where}. A hole type-checks so the surrounding draft can "
        f"be checked, but it has no implementation and there is nothing to "
        f"lower. Fill every hole, then emit (docs/holes.md)."
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
        refuse_deferred_on_ownerless_tier(ir, "go")
        refuse_approval_on_ownerless_tier(ir, "go")
    except RevlError as exc:
        raise EmitError(exc.message) from None


_REVL_SYNC_SUFFIX = "_revl_sync"


def _dedup_colour_erased_poly_externs(ir: dict) -> dict:
    """item 388, stage 6: on a colour-erasing tier (go/rust/java/wasm — suspension
    is not a function colour) a caller-decided-colour extern's two clones — `X`
    (async) and `X_revl_sync` (sync) — emit the SAME blocking host function.
    Collapse them to ONE: drop the sync clone and rewrite its call sites to `X`.

    Detected structurally: a `_revl_sync` extern whose origin twin is present with
    identical `bodies`. A poly extern instantiated in only one colour has no twin,
    so it is emitted unchanged under whatever name survived. Non-destructive (the
    shared IR is also emitted by py/ts, which keep both colours), and a no-op that
    returns the IR untouched when no such pair exists (every existing golden is
    byte-identical)."""
    externs = ir.get("externs") or []
    by_name = {e.get("name"): e for e in externs}
    alias: dict = {}
    kept: list = []
    for e in externs:
        name = e.get("name") or ""
        if name.endswith(_REVL_SYNC_SUFFIX):
            origin = name[: -len(_REVL_SYNC_SUFFIX)]
            twin = by_name.get(origin)
            if twin is not None and twin.get("bodies") == e.get("bodies"):
                alias[name] = origin
                continue
        kept.append(e)
    if not alias:
        return ir

    def _rewrite(node):
        if isinstance(node, dict):
            return {k: (alias[v] if k == "name" and isinstance(v, str)
                        and v in alias else _rewrite(v))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [_rewrite(x) for x in node]
        return node

    ir = dict(ir)
    ir["externs"] = kept
    for key in ("components", "functions", "tests", "prop_tests"):
        if key in ir:
            ir[key] = _rewrite(ir[key])
    return ir


def emit(ir: dict, package: str = "emitted", package_name: str | None = None,
         record: bool = False) -> str:
    # `package_name` is the conformance harness's per-case naming kwarg (the
    # same one the java tier takes); accept it as an alias for `package`.
    if package_name is not None:
        package = package_name
    ir = _dedup_colour_erased_poly_externs(ir)  # item 388, stage 6
    # item 322 Slice 1: `record=True` wires the witnessed teardown to a durable
    # WAL sink (the go host recording channel) so a crash BEFORE commit is
    # recoverable by `revl recover`. It is OFF by default and gated everywhere
    # it touches emission, so a non-recording program (every existing golden)
    # emits byte-identically; only a program emitted in record mode carries the
    # recording preamble and the per-descriptor `revlRecordTransactional` calls.
    global _RECORD_MODE
    _RECORD_MODE = record
    ver = ir.get("ir_version")
    if ver not in (1, 2, 3):
        raise EmitError("cordis-go backend targets ir_version 1, 2 or 3, got %r" % (ver,))
    _refuse_holes(ir)
    _refuse_deferred_emissions(ir)
    # Instance-parametric `spawn` (docs/design-v2-instances.md, phase 1) is an
    # acquisition inside a `let-effect` step (acquire.kind == "spawn"); it is
    # lowered below to a child-fiber plug on the real stc-go runtime. The old
    # `comp["spawn"]/["instance"]` reject shape never matched the frozen IR
    # (spawn is a step's acquire, not a component key), so it was inert; it is
    # removed now that the lowering exists.

    # ir_version-3 routing:
    #   * No component, or any top-level pure declaration (functions / types /
    #     externs / tests) present -> the pure typed-core path (`_emit_v3_go`):
    #     ordinary Go, no stc runtime (the v3_* fixtures, and the pure-fn /
    #     record / ADT / test corpus cases whose component is incidental).
    #   * A component and NOTHING top-level -> a live stc-go component whose
    #     method/step bodies use v3 expressions; the stc-go path below renders
    #     them with the converged expression renderer.
    has_top_level = bool(ir.get("functions") or ir.get("types")
                         or ir.get("externs") or ir.get("tests"))
    has_lifecycle = any(t.get("lifecycle") for t in (ir.get("tests") or []))
    # a `spawn` needs the stdctx import too: revlSpawn* waits for the child
    # fiber to be Active before returning the handle (see _emit_spawn_support).
    has_spawn = _spawn_targets(ir) and any(
        comp.get("body") or comp.get("provides") for comp in ir.get("components", []))
    if has_lifecycle and not ir.get("components"):
        raise EmitError(
            "a lifecycle test drives components over a live stc-go runtime, "
            "but this document declares no components — there is nothing to "
            "load or assert no residue over"
        )
    if ver == 3 and (not ir.get("components") or (has_top_level and not has_lifecycle)):
        # A `lifecycle test` is a script over a live composition, so the
        # document must stay on the stc-go runtime path even though it also
        # carries top-level `test` blocks (FR-5); the pure path would drop the
        # components and refuse the lifecycle steps.
        return _emit_v3_go(ir, package)

    global _V3_MODE, _V3_TYPES, _V3_TYPED_COMPONENTS
    global _COMP_NEEDS_STDLIB, _COMP_NEEDS_MAP, _COMP_NEEDS_PARSE_INT
    global _COMP_NEEDS_STRCONV
    global _COMP_NEEDS_TIMER, _TIMER_COUNTER
    global _WITNESSED_EXTERNS, _COMP_NEEDS_TEARDOWN, _WITNESSED_COUNTER
    global _COMP_NEEDS_METHOD_WITNESSED, _FN_RET, _COMP_NEEDS_STREAM
    _COMP_NEEDS_TIMER = False
    _TIMER_COUNTER = 0
    _COMP_NEEDS_STREAM = False
    # item 243/247: witnessed externs by name, for this document's component
    # steps (see `_witnessed_extern`); empty for a document with none, so
    # every existing v1/v2 golden emits exactly as before.
    _WITNESSED_EXTERNS = {
        ext["name"]: ext for ext in (ir.get("externs") or [])
        if ext.get("class") == "witnessed"
    }
    # item 320: declared return types of every top-level fn and extern, so a
    # value-typed `let x = effect <fn call>` bracket acquisition can be
    # declared by its ACTUAL return type instead of `*T`. Empty for a document
    # with no such acquisition, so existing goldens are untouched.
    _FN_RET = {}
    for _fn in (ir.get("functions") or []):
        if _fn.get("name"):
            _FN_RET[_fn["name"]] = _fn.get("returns")
    for _ext in (ir.get("externs") or []):
        if _ext.get("name"):
            _FN_RET[_ext["name"]] = _ext.get("returns")
    _COMP_NEEDS_TEARDOWN = False
    _COMP_NEEDS_METHOD_WITNESSED = False
    _WITNESSED_COUNTER = 0
    # item 102: a lifecycle test's `advance` step drives the clock coeffect
    # (RevlClockAdvance / RevlClockReset), which lives in the timer preamble.
    # The timer components in scope normally flag it, but an `advance` alone is
    # enough to require it — pull the preamble in regardless.
    if any(
        s.get("step") == "advance"
        for t in (ir.get("tests") or []) if t.get("lifecycle")
        for s in (t.get("body") or [])
    ):
        _COMP_NEEDS_TIMER = True
    _V3_MODE = (ver == 3)
    # item 139: a v3 document reaching emit()'s live stc-go path may declare
    # record/ADT types that a component's provide method takes or returns (a
    # `provide s { fn make(n, a) -> Person { ... } }` over a live composition,
    # kept here — not routed to the pure typed-core path — by a lifecycle test
    # driving it). Materialize those types as Go structs/sealed interfaces in
    # this same package and put the component/method renderer in typed-core
    # mode, so record literals, field access, ADT construction and `match`
    # lower against them exactly as they do in the placement path — instead of
    # the v1/v2 "record is not lowerable in the stc-go component world" refusal.
    # v1/v2 documents (and v3 documents with no declared types) leave the flag
    # off, so their output stays byte-identical to the frozen scenarios.
    _V3_TYPES = (ir.get("types") or {}) if ver == 3 else {}
    _V3_TYPED_COMPONENTS = bool(_V3_TYPES)
    _COMP_NEEDS_STDLIB = False
    _COMP_NEEDS_MAP = False
    _COMP_NEEDS_PARSE_INT = False
    _COMP_NEEDS_STRCONV = False

    # Emit the body first so `_COMP_NEEDS_STDLIB` settles before the import
    # block and preamble are assembled. For ir_version 1/2 no v3 feature is
    # exercised, so the flag stays False and the output is byte-identical to
    # the frozen scenarios.
    body: list[str] = []
    if _V3_TYPES:
        # the declared typed-core types come first, so the component impls and
        # lifecycle tests below reference already-declared structs/interfaces.
        body.extend(_emit_v3_go_types(_V3_TYPES))
    # Top-level `extern` declarations reachable from a live component's body
    # (item 243: a `witnessed[caps] fn ... undo ...`, plus whatever `pure`
    # extern its declared undo calls). `_emit_v3_go` already renders extern
    # bodies for the pure typed-core path; this document instead carries a
    # component AND a lifecycle test (or, for a compensation-only v1/v2
    # document, no top-level externs at all — this block is then a no-op),
    # so it stays on THIS path (see the ir_version-3 routing above) and would
    # otherwise never see its extern bodies emitted at all. Additive and
    # gated on `externs` being non-empty: a v1/v2 document never declares
    # one (confirmed byte-identical for every existing golden).
    externs = ir.get("externs") or []
    extern_ctx = _V3GoCtx(_V3_TYPES, ir.get("functions") or [], externs)
    if externs:
        body.extend(_emit_v3_go_externs(externs, extern_ctx))
    body.extend(_emit_services(ir.get("services", {})))
    _emit_keys(ir, body)
    _emit_realm_helper(ir, body)
    for comp in ir.get("components", []):
        _emit_component(comp, ir.get("services", {}), body)
    _emit_load_helpers(ir, body)
    _emit_spawn_support(ir, body)
    _emit_stc_lifecycle_tests(ir, body)

    # An extern's Result-typed return (every witnessed extern's declared
    # shape) needs the sealed-interface preamble; scanning the rendered body
    # (mirrors `_emit_v3_go`'s own `used_result` detection) catches it
    # wherever it appears — the extern's own signature or a component step's
    # `RevlOk[...]` type assertion (`_emit_witnessed_step`) — without having
    # to enumerate every producing site by hand.
    body_blob = "\n".join(body)
    needs_result_preamble = any(
        t in body_blob for t in ("RevlResult[", "RevlOk[", "RevlErr["))

    out: list[str] = []
    out.append("// Code generated by backends/go/emit.py — DO NOT EDIT.")
    out.append("// revl -> cordis-go, targeting github.com/0xdenny218/stc-go.")
    out.append("package %s" % package)
    out.append("")
    out.append("import (")
    out.append('\t"fmt"')
    out.append('\t"sync"')
    if has_lifecycle or has_spawn:
        out.append('\tstdctx "context"')
    if has_lifecycle:
        out.append('\t"reflect"')
        out.append('\t"testing"')
        out.append('\t"time"')
    if _COMP_NEEDS_STDLIB:
        out.append('\t"strings"')
        out.append('\t"unicode/utf8"')
    if _COMP_NEEDS_TEARDOWN:
        # `runCompensationPhase`'s budget/deadline (`time` — already imported
        # above when `has_lifecycle`, so guarded to keep Go's single-import
        # rule), the two `REVL_COMPENSATION_*_MS` env reads (`os`, `strconv`).
        if not has_lifecycle:
            out.append('\t"time"')
        out.append('\t"os"')
        out.append('\t"strconv"')
    if _COMP_NEEDS_STRCONV and not _COMP_NEEDS_TEARDOWN:
        # Int.to_str renders through strconv.FormatInt (item 434 (f)); the
        # teardown block above already imports strconv when it is present, and
        # Go rejects the same path twice.
        out.append('\t"strconv"')
    # The host runtime's Map.Keys and _V3_MAP_PREAMBLE's revlMapKeys both sort
    # with slices.Sort (item 434 (h)); the host runtime is unconditional on
    # this tier, so the import is too.
    out.append('\t"slices"')
    if _RECORD_MODE and _COMP_NEEDS_TEARDOWN:
        # item 322 Slice 1: the durable WAL sink marshals records with
        # encoding/json ("os" is already pulled in by the teardown block above).
        out.append('\t"encoding/json"')
    out.append("")
    out.append('\tstc "github.com/0xdenny218/stc-go"')
    out.append(")")
    out.append("")
    out.append("var _ = fmt.Sprintf")
    out.append("")
    out.append(_host_runtime())
    if _COMP_NEEDS_STDLIB:
        out.append(_V3_STDLIB_PREAMBLE)
    if _COMP_NEEDS_MAP or _COMP_NEEDS_PARSE_INT:
        # revlMapGet / revlParseInt answer a RevlOpt, so the Opt preamble
        # comes first.
        out.append(_V3_OPT_PREAMBLE)
    if _COMP_NEEDS_MAP:
        out.append(_V3_MAP_PREAMBLE)
    if _COMP_NEEDS_PARSE_INT:
        out.append(_V3_PARSE_INT_HELPER)
    if needs_result_preamble:
        out.append(_V3_RESULT_PREAMBLE)
    if _COMP_NEEDS_TEARDOWN:
        out.append(_teardown_preamble(_COMP_NEEDS_METHOD_WITNESSED))
        if _RECORD_MODE:
            out.append(_RECORD_PREAMBLE)
    if _COMP_NEEDS_TIMER:
        out.append(_TIMER_PREAMBLE)
    if _COMP_NEEDS_STREAM:
        out.append(_STREAM_PREAMBLE)

    out.extend(body)

    out.extend(_emit_host_stubs(ir))

    return "\n".join(out) + "\n"


# ==========================================================================
# interop bridge (docs/interop-bridge.md §3): generated per-service proxy +
# stub-dispatch, so a cordis-go process can consume/serve a cross-process key.
# cordis-go services are static Go interfaces, so this is codegen (a
# runtime-generic proxy is impossible in Go, unlike py attribute access) — the
# same shape backends/rust/emit.py::_emit_bridge takes for cordis-rs traits.
#
# This block is ADDITIVE and self-contained: it is emitted into a SEPARATE Go
# file (`bridge_gen.go`) alongside the ordinary module (`gen.go`, from emit()),
# so the v1/v2 module output stays byte-identical (conformance never sees it).
# The canonical wire encoding matches backends/python/bridge.py exactly: scalars
# / lists / records / Opt cross as plain JSON, ADT / Result cases as
# {"$kind","$value"}. --------------------------------------------------------


def _go_result_inner(rtype: str):
    """`Result[T, E]` -> (T_go, E_go), else None."""
    t = str(rtype).strip()
    if t.startswith("Result[") and t.endswith("]"):
        ok, err = _v3_split_generic(t[7:-1])
        return _go_type(ok), _go_type(err)
    return None


def _go_opt_inner(rtype: str):
    """`Opt[T]` -> T_go, else None."""
    t = str(rtype).strip()
    if t.startswith("Opt[") and t.endswith("]"):
        return _go_type(t[4:-1])
    return None


def _go_bridge_arg_expr(param) -> str:
    """A proxy method argument as an `any` for the wire args list. Scalars,
    lists, maps, records and Opt (`*T`, nil==null) json.Marshal canonically, so
    the local name is passed straight through — matching python's plain-JSON
    encoding for those shapes. A v3 typed-core variant (or a container of one)
    needs its generated `_revlEncode<Variant>` to reach the canonical
    {"$kind","$value"} wire shape (records cross as plain JSON via their
    json-tagged exported fields)."""
    t = str(param["type"]).strip()
    local = _safe_local(param["name"])
    enc = _bridge_encode_expr(t, local)
    return enc if enc is not None else local


def _v3_declared_variant(t) -> str | None:
    """`t` (a surface type name) when it is a variant declared in the current
    document, else None."""
    t = str(t).strip()
    if t in _V3_TYPES and _V3_TYPES[t].get("kind") == "variant":
        return t
    return None


def _revl_encode_fn(t) -> str:
    return "_revlEncode%s" % _v3_ident(str(t).strip(), "type name")


def _revl_decode_fn(t) -> str:
    return "_revlDecode%s" % _v3_ident(str(t).strip(), "type name")


def _bridge_encode_expr(revl_type, expr) -> str | None:
    """None when `expr` (of static revl type) is already canonical on the wire
    (json marshals it correctly); else the Go expression encoding it. Recurses
    through List/Opt so a container of variants reaches the canonical shapes
    (variants as {"$kind","$value"}, records as plain JSON)."""
    t = str(revl_type).strip()
    if _v3_declared_variant(t):
        return "%s(%s)" % (_revl_encode_fn(t), expr)
    if t.startswith("List[") and t.endswith("]"):
        inner = t[5:-1]
        enc = _bridge_encode_expr(inner, "_x")
        if enc is None:
            return None
        return ("func() []any { _in := %s; _out := make([]any, len(_in)); "
                "for _i, _x := range _in { _out[_i] = %s }; return _out }()"
                % (expr, enc))
    if t.startswith("Opt[") and t.endswith("]"):
        inner = t[4:-1]
        enc = _bridge_encode_expr(inner, "_o")
        if enc is None:
            return None
        return ("func() any { if %s == nil { return nil }; "
                "_o := *%s; return %s }()" % (expr, expr, enc))
    return None


def _bridge_decode_expr(revl_type, raw_expr) -> str | None:
    """None when the plain `var x T; json.Unmarshal(raw, &x)` statement form
    decodes correctly; else the Go expression decoding `raw_expr` (a
    json.RawMessage) into the static type. Recurses through List/Opt for
    containers of variants."""
    t = str(revl_type).strip()
    if _v3_declared_variant(t):
        return "%s(%s)" % (_revl_decode_fn(t), raw_expr)
    if t.startswith("List[") and t.endswith("]"):
        inner = t[5:-1]
        dec = _bridge_decode_expr(inner, "_e")
        if dec is None:
            return None
        return ("func() %s { var _r []json.RawMessage; "
                "_ = json.Unmarshal(%s, &_r); _out := make(%s, len(_r)); "
                "for _i, _e := range _r { _out[_i] = %s }; return _out }()"
                % (_go_type(t), raw_expr, _go_type(t), dec))
    if t.startswith("Opt[") and t.endswith("]"):
        inner = t[4:-1]
        dec = _bridge_decode_expr(inner, "_p")
        if dec is None:
            return None
        return ("func() %s { if len(%s) == 0 || string(%s) == \"null\" "
                "{ return nil }; _p := %s; return &_p }()"
                % (_go_type(t), raw_expr, raw_expr, dec))
    return None


def _emit_go_proxy_method(struct, mname, params, ret_revl, out):
    go_params = ", ".join(
        "%s %s" % (_safe_local(p["name"]), _go_type(p["type"])) for p in params)
    ret_sig = _go_return(ret_revl)
    sig = "func (p *%s) %s(%s)" % (struct, _camel(mname), go_params)
    if ret_sig:
        sig += " " + ret_sig
    out.append(sig + " {")
    argvec = ", ".join(_go_bridge_arg_expr(p) for p in params)
    out.append('\t_v, _err := p.client.Call(p.key, %s, []any{%s})'
               % (_go_string(mname), argvec))
    out.append("\tif _err != nil {")
    out.append("\t\tpanic(_err)")
    out.append("\t}")
    out.append("\t_ = _v")
    _emit_go_proxy_decode(ret_revl, out)
    out.append("}")
    out.append("")


def _emit_go_proxy_decode(ret_revl, out):
    """Decode the reply `_v` (json.RawMessage) into the method's Go return.

    v3 typed-core: a user ADT reply (or one nested in Opt/Result) decodes via
    the generated `_revlDecode<Variant>` from its canonical {"$kind","$value"}
    wire shape; records decode via json.Unmarshal on their json-tagged
    exported fields."""
    if ret_revl is None or str(ret_revl).strip() in ("", "Unit"):
        return  # void
    result = _go_result_inner(str(ret_revl))
    if result is not None:
        ok_go, err_go = result
        ok_revl, err_revl = _v3_split_generic(str(ret_revl)[7:-1])
        out.append('\tvar _tag struct {')
        out.append('\t\tKind  string          `json:"$kind"`')
        out.append('\t\tValue json.RawMessage `json:"$value"`')
        out.append("\t}")
        out.append("\t_ = json.Unmarshal(_v, &_tag)")
        out.append('\tif _tag.Kind == "Ok" {')
        ok_dec = _bridge_decode_expr(ok_revl, "_tag.Value")
        if ok_dec is not None:
            out.append("\t\t_ok := %s" % ok_dec)
        else:
            out.append("\t\tvar _ok %s" % ok_go)
            out.append("\t\t_ = json.Unmarshal(_tag.Value, &_ok)")
        out.append("\t\tvar _zeroE %s" % err_go)
        out.append("\t\treturn _ok, _zeroE, true")
        out.append("\t}")
        out.append("\tvar _zeroT %s" % ok_go)
        err_dec = _bridge_decode_expr(err_revl, "_tag.Value")
        if err_dec is not None:
            out.append("\t_err2 := %s" % err_dec)
        else:
            out.append("\tvar _err2 %s" % err_go)
            out.append("\t_ = json.Unmarshal(_tag.Value, &_err2)")
        out.append("\treturn _zeroT, _err2, false")
        return
    opt = _go_opt_inner(str(ret_revl))
    if opt is not None:
        out.append('\tif len(_v) == 0 || string(_v) == "null" {')
        out.append("\t\tvar _zero %s" % opt)
        out.append("\t\treturn _zero, false")
        out.append("\t}")
        if _v3_declared_variant(opt):
            out.append("\t_d := %s(_v)" % _revl_decode_fn(opt))
            out.append("\treturn &_d, true")
        else:
            out.append("\tvar _r %s" % opt)
            out.append("\t_ = json.Unmarshal(_v, &_r)")
            out.append("\treturn _r, true")
        return
    dec = _bridge_decode_expr(str(ret_revl), "_v")
    if dec is not None:
        out.append("\t_r := %s" % dec)
        out.append("\treturn _r")
        return
    out.append("\tvar _r %s" % _go_type(ret_revl))
    out.append("\t_ = json.Unmarshal(_v, &_r)")
    out.append("\treturn _r")


def _emit_go_dispatch_encode(call, ret_revl, out):
    """Encode a stub method's return value to the reply `value` (an `any`)."""
    if ret_revl is None or str(ret_revl).strip() in ("", "Unit"):
        out.append("\t\t%s" % call)
        out.append("\t\treturn nil, nil")
        return
    result = _go_result_inner(str(ret_revl))
    if result is not None:
        ok_revl, err_revl = _v3_split_generic(str(ret_revl)[7:-1])
        out.append("\t\t_okv, _errv, _ok := %s" % call)
        out.append("\t\tif _ok {")
        ok_enc = _bridge_encode_expr(ok_revl, "_okv")
        out.append('\t\t\treturn map[string]any{"$kind": "Ok", "$value": %s}, nil'
                   % (ok_enc if ok_enc is not None else "_okv"))
        out.append("\t\t}")
        err_enc = _bridge_encode_expr(err_revl, "_errv")
        out.append('\t\treturn map[string]any{"$kind": "Err", "$value": %s}, nil'
                   % (err_enc if err_enc is not None else "_errv"))
        return
    opt = _go_opt_inner(str(ret_revl))
    if opt is not None:
        out.append("\t\t_v, _ok := %s" % call)
        out.append("\t\tif !_ok {")
        out.append("\t\t\treturn nil, nil")
        out.append("\t\t}")
        if _v3_declared_variant(opt):
            out.append("\t\treturn %s(*_v), nil" % _revl_encode_fn(opt))
        else:
            out.append("\t\treturn _v, nil")
        return
    enc = _bridge_encode_expr(str(ret_revl), call)
    if enc is not None:
        out.append("\t\treturn %s, nil" % enc)
        return
    out.append("\t\treturn %s, nil" % call)


def _emit_go_dispatch(sname, methods, out):
    cs = _camel(sname)
    out.append("func _revlDispatch%s(svc %s, method string, "
               "args []json.RawMessage) (any, error) {" % (cs, cs))
    out.append("\tswitch method {")
    for mname, m in methods.items():
        params = m.get("params", []) or []
        out.append("\tcase %s:" % _go_string(mname))
        for i, p in enumerate(params):
            dec = _bridge_decode_expr(str(p["type"]), "_revlArg(args, %d)" % i)
            if dec is not None:
                out.append("\t\ta%d := %s" % (i, dec))
            else:
                out.append("\t\tvar a%d %s" % (i, _go_type(p["type"])))
                out.append("\t\t_ = json.Unmarshal(_revlArg(args, %d), &a%d)" % (i, i))
        call = "svc.%s(%s)" % (_camel(mname),
                               ", ".join("a%d" % i for i in range(len(params))))
        _emit_go_dispatch_encode(call, m.get("returns"), out)
    out.append("\t}")
    out.append('\treturn nil, fmt.Errorf("method %%q is not exported for '
               'service %s", method)' % sname)
    out.append("}")
    out.append("")


def _emit_v3_bridge_helpers(types: dict) -> list[str]:
    """Per-variant `_revlEncode<Variant>` / `_revlDecode<Variant>` for the
    bridge: the canonical ADT wire shape {"$kind", "$value"} (the same
    encoding backends/python/bridge.py uses), mirroring how the v3 tier
    represents variants (sealed interface + case structs). Records need no
    helpers — their placement-mode json-tagged exported fields marshal
    canonically. Payloads recurse through `_bridge_encode_expr` /
    `_bridge_decode_expr` so a variant payload nested in List/Opt also
    round-trips."""
    out: list[str] = []
    for name, spec in (types or {}).items():
        if spec.get("kind") != "variant":
            continue
        gname = _v3_ident(name, "type name")
        enc = _revl_encode_fn(name)
        dec = _revl_decode_fn(name)
        out.append("// %s encodes a %s to the canonical ADT wire shape" % (enc, gname))
        out.append('// ({"$kind", "$value"} — docs/interop-bridge.md §3).')
        out.append("func %s(v %s) any {" % (enc, gname))
        out.append("\tswitch c := v.(type) {")
        for case in spec.get("cases") or []:
            cname = case.get("name")
            cstruct = "%s%s" % (gname, cname)
            payload = case.get("payload")
            if payload is None:
                out.append("\tcase %s:" % cstruct)
                out.append('\t\treturn map[string]any{"$kind": %s}' % _go_string(cname))
                continue
            pv = _bridge_encode_expr(str(payload), "c.Value")
            out.append("\tcase %s:" % cstruct)
            out.append('\t\treturn map[string]any{"$kind": %s, "$value": %s}'
                       % (_go_string(cname), pv if pv is not None else "c.Value"))
        out.append("\t}")
        out.append('\tpanic("revl: unhandled %s case in bridge encode")' % gname)
        out.append("}")
        out.append("")
        out.append("func %s(raw json.RawMessage) %s {" % (dec, gname))
        out.append("\tvar _tag struct {")
        out.append('\t\tKind  string          `json:"$kind"`')
        out.append('\t\tValue json.RawMessage `json:"$value"`')
        out.append("\t}")
        out.append("\t_ = json.Unmarshal(raw, &_tag)")
        out.append("\tswitch _tag.Kind {")
        for case in spec.get("cases") or []:
            cname = case.get("name")
            cstruct = "%s%s" % (gname, cname)
            payload = case.get("payload")
            out.append("\tcase %s:" % _go_string(cname))
            if payload is None:
                out.append("\t\treturn %s{}" % cstruct)
                continue
            pv = _bridge_decode_expr(str(payload), "_tag.Value")
            if pv is not None:
                out.append("\t\treturn %s{Value: %s}" % (cstruct, pv))
            else:
                out.append("\t\tvar _p %s" % _go_type(payload))
                out.append("\t\t_ = json.Unmarshal(_tag.Value, &_p)")
                out.append("\t\treturn %s{Value: _p}" % cstruct)
        out.append("\t}")
        out.append('\tpanic("revl: unknown %s case in bridge decode: " + _tag.Kind)' % gname)
        out.append("}")
        out.append("")
    return out


def _emit_go_bridge(ir: dict) -> list[str]:
    """Emit the placement bridge for one composition: per-service proxy structs
    (consumer side) + stub dispatch (provider side), and the fixed-name entry
    points the runner (main.go) calls. Empty when the composition declares no
    services (nothing crosses)."""
    services = ir.get("services", {}) or {}
    components = ir.get("components", []) or []
    if not services:
        return []

    provided: dict[str, str] = {}   # key -> service, over provided keys
    seen: dict[str, str] = {}       # key -> service, over provided + required
    for comp in components:
        for key, svc in (comp.get("provides", {}) or {}).items():
            provided[key] = svc
            seen[key] = svc
        for key, svc in (comp.get("requires", {}) or {}).items():
            seen.setdefault(key, svc)

    out: list[str] = []
    for sname, sdef in services.items():
        methods = sdef.get("methods", {}) or {}
        struct = "%sProxy" % _camel(sname)
        # consumer-side proxy struct implementing the service interface
        out.append("// %s forwards %s calls to a remote stub over the bridge."
                   % (struct, _camel(sname)))
        out.append("type %s struct {" % struct)
        out.append("\tclient *bridge.Client")
        out.append("\tkey    string")
        out.append("}")
        out.append("")
        for mname, m in methods.items():
            _emit_go_proxy_method(struct, mname, m.get("params", []) or [],
                                  m.get("returns"), out)
        # provider-side dispatch (the declared method allowlist, G8)
        _emit_go_dispatch(sname, methods, out)

    # arg accessor: a missing positional arg decodes as JSON null.
    out.append("func _revlArg(args []json.RawMessage, i int) json.RawMessage {")
    out.append("\tif i < len(args) {")
    out.append("\t\treturn args[i]")
    out.append("\t}")
    out.append('\treturn json.RawMessage("null")')
    out.append("}")
    out.append("")

    # key -> service name (over provided keys)
    out.append("// RevlServiceOf names the service a provided key exports.")
    out.append("func RevlServiceOf(key string) (string, bool) {")
    out.append("\tswitch key {")
    for key, svc in provided.items():
        out.append("\tcase %s:" % _go_string(key))
        out.append("\t\treturn %s, true" % _go_string(svc))
    out.append("\t}")
    out.append('\treturn "", false')
    out.append("}")
    out.append("")

    # no-residue proof: does `key` still resolve to a provider in ctx? The
    # once-mode runner (revl run --backend go --once) reads this after a full
    # LIFO teardown — a provided key whose provide-inverse ran must fail to
    # resolve. This is the go mirror of the py driver's reflect.store check
    # and the rust runner's reflect().services() check; stc-go has no public
    # provision enumeration, so the generated per-key switch is the honest
    # read (generality is codegen on this tier, exactly like the proxies).
    out.append("// RevlStillProvided reports whether `key` currently resolves to a")
    out.append("// service in ctx (the once-mode no-residue check).")
    out.append("func RevlStillProvided(ctx *stc.Context, key string) bool {")
    out.append("\tswitch key {")
    for key, svc in provided.items():
        cs = _camel(svc)
        out.append("\tcase %s:" % _go_string(key))
        out.append("\t\t_, err := stc.Service[%s](ctx, %s)" % (cs, _key_var(key)))
        out.append("\t\treturn err == nil")
    out.append("\t}")
    out.append("\treturn false")
    out.append("}")
    out.append("")

    # consumer: build a plugin that provides `key` via the right proxy. The
    # runner owns the *bridge.Client (so it can also Monitor the same socket).
    out.append("func _revlProxyComponent(pname string, k stc.Key, value any) "
               "stc.Component {")
    out.append("\treturn stc.Component{")
    out.append("\t\tName:    pname,")
    out.append("\t\tProvide: []stc.Key{k},")
    out.append("\t\tApply: func(ctx *stc.Context) (stc.Inverse, error) {")
    out.append("\t\t\treturn ctx.Provide(k, value)")
    out.append("\t\t},")
    out.append("\t}")
    out.append("}")
    out.append("")
    out.append("// RevlProxyComponent is a component that provides `key` with a proxy")
    out.append("// forwarding to the stub `client` is connected to.")
    out.append("func RevlProxyComponent(key, service string, client *bridge.Client) "
               "(stc.Component, bool) {")
    out.append("\tswitch key {")
    for key, svc in seen.items():
        struct = "%sProxy" % _camel(svc)
        out.append("\tcase %s:" % _go_string(key))
        out.append('\t\treturn _revlProxyComponent(%s, %s, &%s{client: client, key: key}), true'
                   % (_go_string(struct), _key_var(key), struct))
    out.append("\t}")
    out.append("\treturn stc.Component{}, false")
    out.append("}")
    out.append("")

    # provider/probe: resolve a provided key and dispatch to it. On a consumer
    # process the key resolves to the proxy; on a provider, to the real impl —
    # so this same entry point serves the seam AND drives probes.
    out.append("// RevlInvoke dispatches one call against the service currently")
    out.append("// providing `key` in ctx (a proxy or the local impl).")
    out.append("func RevlInvoke(ctx *stc.Context, key, method string, "
               "args []json.RawMessage) (any, error) {")
    out.append("\tswitch key {")
    for key, svc in provided.items():
        cs = _camel(svc)
        out.append("\tcase %s:" % _go_string(key))
        out.append("\t\tsvc, err := stc.Service[%s](ctx, %s)" % (cs, _key_var(key)))
        out.append("\t\tif err != nil {")
        out.append("\t\t\treturn nil, err")
        out.append("\t\t}")
        out.append("\t\treturn _revlDispatch%s(svc, method, args)" % cs)
    out.append("\t}")
    out.append('\treturn nil, fmt.Errorf("key %q is not provided by this process", key)')
    out.append("}")
    out.append("")

    # component name -> loaded Fiber, building typed config from the placement
    # spec's `config` object (keyed by PascalCase component name).
    out.append("// RevlLoad loads a component by name with config from the spec.")
    out.append("func RevlLoad(target *stc.Context, name string, "
               "config map[string]json.RawMessage) (*stc.Fiber, bool) {")
    out.append("\tswitch name {")
    for comp in components:
        cname = _camel(comp["name"])
        fields = comp.get("config") or []
        out.append("\tcase %s:" % _go_string(comp["name"]))
        if fields:
            out.append("\t\tcfg := Default%sConfig()" % cname)
            out.append("\t\tif _raw, _ok := config[%s]; _ok {" % _go_string(comp["name"]))
            out.append("\t\t\tvar _m map[string]json.RawMessage")
            out.append("\t\t\t_ = json.Unmarshal(_raw, &_m)")
            for f in fields:
                out.append("\t\t\tif _v, _ok := _m[%s]; _ok {" % _go_string(f["name"]))
                out.append("\t\t\t\t_ = json.Unmarshal(_v, &cfg.%s)" % _camel(f["name"]))
                out.append("\t\t\t}")
            out.append("\t\t}")
            out.append("\t\treturn Load%s(target, cfg), true" % cname)
        else:
            out.append("\t\treturn Load%s(target), true" % cname)
    out.append("\t}")
    out.append("\treturn nil, false")
    out.append("}")
    out.append("")
    return out


def _emit_v3_placement(ir: dict, package: str) -> str:
    """A v3 typed-core composition for the placement runner, in ONE package:
    the pure typed-core tier (record structs, ADT sealed interfaces, pure
    `fn`s, externs, plain `test` blocks — ordinary Go) PLUS the live stc-go
    components (service interfaces, keys, impls, load helpers) PLUS the
    interop bridge. This is the go mirror of the rust tier's `_emit_v3`
    (types + components in one module), extended with the bridge the placement
    runner links against.

    Record structs are emitted with EXPORTED, json-tagged fields (`_V3_TYPED_COMPONENTS`)
    so record values survive the bridge's plain-JSON wire encoding — the go
    mirror of the rust tier's serde derives. The pure tier (`emit`) keeps
    unexported fields byte-for-byte with the frozen fixtures."""
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    components = ir.get("components") or []
    ctx = _V3GoCtx(types, functions, externs)

    global _V3_MODE, _V3_TYPES, _V3_TYPED_COMPONENTS
    global _COMP_NEEDS_STDLIB, _COMP_NEEDS_MAP, _COMP_NEEDS_PARSE_INT
    global _COMP_NEEDS_STRCONV
    global _COMP_NEEDS_TIMER, _TIMER_COUNTER, _COMP_NEEDS_STREAM
    _V3_MODE = True
    _V3_TYPES = types
    _V3_TYPED_COMPONENTS = True
    _COMP_NEEDS_STDLIB = False
    _COMP_NEEDS_MAP = False
    _COMP_NEEDS_PARSE_INT = False
    _COMP_NEEDS_STRCONV = False
    _COMP_NEEDS_TIMER = False
    _TIMER_COUNTER = 0
    _COMP_NEEDS_STREAM = False

    has_lifecycle = any(t.get("lifecycle") for t in tests)
    has_spawn = _spawn_targets(ir) and any(
        comp.get("body") or comp.get("provides") for comp in components)

    # render every body first so the feature flags settle before assembly
    body: list[str] = []
    if types:
        body.extend(_emit_v3_go_types(types))
    if externs:
        body.extend(_emit_v3_go_externs(externs, ctx))
    if functions:
        body.extend(_emit_v3_go_functions(functions, ctx))
    pure_tests = [t for t in tests if not t.get("lifecycle")]
    if pure_tests:
        body.extend(_emit_v3_go_tests(pure_tests, ctx))
    if has_lifecycle:
        # a lifecycle test is a script over the live composition (FR-5): it
        # drives the stc-go LoadX helpers below, so it is emitted after them
        # and must reference them (it appends to `body` in place).
        _emit_stc_lifecycle_tests(ir, body)

    # ---- live stc-go components -----------------------------------------
    body.append("")
    body.append("// ---- live stc-go components (placement) -------------------")
    body.extend(_emit_services(ir.get("services", {})))
    _emit_keys(ir, body)
    _emit_realm_helper(ir, body)
    for comp in components:
        _emit_component(comp, ir.get("services", {}), body)
    _emit_load_helpers(ir, body)
    _emit_spawn_support(ir, body)
    host_stubs = _emit_host_stubs(ir)

    # feature flags, mirroring _emit_v3_go's blob scan for the pure tier
    blob = json.dumps({
        "types": types, "functions": functions,
        "externs": externs, "tests": tests,
    })
    used_opt = ("Opt[" in blob) or ('"optfield"' in blob) or ('"optcall"' in blob)
    if ctx.needs_parse_int:
        used_opt = True
    used_map = ('"maplit"' in blob) or any(
        f'"method": "{m}"' in blob for m in ("set", "lookup", "has",
                                             "size", "keys", "remove"))
    if used_map:
        used_opt = True
    used_result = ("Result[" in blob) or ("checked_div_" in blob) or ("checked_mod" in blob)

    imports: list[str] = []
    if pure_tests or has_lifecycle:
        imports.append('\t"testing"')
    if ctx.needs_fmt:
        imports.append('\t"fmt"')
    if has_lifecycle:
        imports.append('\t"reflect"')
        imports.append('\t"time"')
        imports.append('\tstdctx "context"')
    if ctx.needs_reflect and not has_lifecycle:
        imports.append('\t"reflect"')
    if ctx.used_stdlib or _COMP_NEEDS_STDLIB:
        imports.append('\t"strings"')
        imports.append('\t"unicode/utf8"')
    if ctx.needs_ftoa:
        imports.append('\t"math"')
        imports.append('\t"strconv"')
        imports.append('\t"strings"')
    if ctx.needs_strconv or _COMP_NEEDS_STRCONV:
        imports.append('\t"strconv"')
    # The host runtime's Map.Keys and _V3_MAP_PREAMBLE's revlMapKeys both sort
    # with slices.Sort (item 434 (h)); the host runtime is unconditional on
    # this tier, so the import is too.
    imports.append('\t"slices"')
    if has_spawn:
        imports.append('\tstdctx "context"')
    # the stc-go side always needs fmt + sync + the runtime
    imports.append('\t"fmt"')
    imports.append('\t"sync"')
    imports.append('\tstc "github.com/0xdenny218/stc-go"')

    out: list[str] = []
    out.append("// Code generated by backends/go/emit.py — DO NOT EDIT.")
    out.append("// revl -> cordis-go (ir_version 3, typed-core + live components):")
    out.append("// the pure typed-core tier (records/ADTs/pure fns) plus the stc-go")
    out.append("// components and the interop bridge, in one package (placement).")
    out.append("package %s" % package)
    out.append("")
    if imports:
        out.append("import (")
        out.extend(sorted(set(imports)))
        out.append(")")
        out.append("")
    out.append("var _ = fmt.Sprintf")
    out.append("")
    out.append(_host_runtime())
    if ctx.needs_reflect and not has_lifecycle:
        # structural equality (record/ADT `==` through DeepEqual)
        out.append("func revlEq(a, b any) bool { return reflect.DeepEqual(a, b) }")
        out.append("")
    if ctx.needs_float_div:
        out.append("func revlDiv(a, b float64) float64 { return a / b }")
        out.append("")
    if ctx.needs_ftoa:
        out.append(_V3_FTOA_HELPER)
        out.append("")
    if ctx.needs_overflow:
        out.append("func revlAdd(a, b int64) int64 {")
        out.append("\ts := a + b")
        out.append("\tif (a > 0 && b > 0 && s < 0) || (a < 0 && b < 0 && s >= 0) {")
        out.append('\t\tpanic("revl: Int overflow")')
        out.append("\t}")
        out.append("\treturn s")
        out.append("}")
        out.append("")
        out.append("func revlSub(a, b int64) int64 {")
        out.append("\td := a - b")
        out.append("\tif (b < 0 && d < a) || (b > 0 && d > a) {")
        out.append('\t\tpanic("revl: Int overflow")')
        out.append("\t}")
        out.append("\treturn d")
        out.append("}")
        out.append("")
        out.append("func revlMul(a, b int64) int64 {")
        out.append("\tif a == 0 || b == 0 {")
        out.append("\t\treturn 0")
        out.append("\t}")
        out.append("\tp := a * b")
        out.append("\tif p/b != a {")
        out.append('\t\tpanic("revl: Int overflow")')
        out.append("\t}")
        out.append("\treturn p")
        out.append("}")
        out.append("")
    if ctx.needs_overflow32:
        out.append("func revlAddI32(a, b int32) int32 { return revlToI32("
                   "int64(a) + int64(b)) }")
        out.append("func revlSubI32(a, b int32) int32 { return revlToI32("
                   "int64(a) - int64(b)) }")
        out.append("func revlMulI32(a, b int32) int32 { return revlToI32("
                   "int64(a) * int64(b)) }")
        out.append("func revlToI32(v int64) int32 {")
        out.append("\tif v < -2147483648 || v > 2147483647 {")
        out.append('\t\tpanic("revl: Int32 overflow")')
        out.append("\t}")
        out.append("\treturn int32(v)")
        out.append("}")
        out.append("")
    if ctx.needs_int_arith:
        out.append("func revlDivTrunc(a, b int64) int64 {")
        out.append("\tif a == (-9223372036854775807 - 1) && b == -1 {")
        out.append('\t\tpanic("revl: Int overflow")')
        out.append("\t}")
        out.append("\treturn a / b")
        out.append("}")
        out.append("")
        out.append("func revlDivFloor(a, b int64) int64 {")
        out.append("\tif a == (-9223372036854775807 - 1) && b == -1 {")
        out.append('\t\tpanic("revl: Int overflow")')
        out.append("\t}")
        out.append("\tq := a / b")
        out.append("\tif a%b != 0 && ((a < 0) != (b < 0)) {")
        out.append("\t\tq--")
        out.append("\t}")
        out.append("\treturn q")
        out.append("}")
        out.append("")
        out.append("func revlDivEuclid(a, b int64) int64 {")
        out.append("\tif b > 0 {")
        out.append("\t\treturn revlDivFloor(a, b)")
        out.append("\t}")
        out.append("\treturn -revlDivFloor(a, -b)")
        out.append("}")
        out.append("")
        out.append("func revlMod(a, b int64) int64 {")
        out.append("\tm := b")
        out.append("\tif m < 0 {")
        out.append("\t\tm = -m")
        out.append("\t}")
        out.append("\tr := a % m")
        out.append("\tif r < 0 {")
        out.append("\t\tr += m")
        out.append("\t}")
        out.append("\treturn r")
        out.append("}")
        out.append("")
    if used_opt:
        out.append(_V3_OPT_PREAMBLE)
    if ctx.needs_parse_int or _COMP_NEEDS_PARSE_INT:
        # after the Opt preamble: revlParseInt's return type is RevlOpt
        out.append(_V3_PARSE_INT_HELPER)
    if used_result:
        out.append(_V3_RESULT_PREAMBLE)
    if used_map or _COMP_NEEDS_MAP:
        out.append(_V3_MAP_PREAMBLE)
    if ctx.used_stdlib or _COMP_NEEDS_STDLIB:
        out.append(_V3_STDLIB_PREAMBLE)
    if _COMP_NEEDS_TIMER:
        out.append(_TIMER_PREAMBLE)
    if _COMP_NEEDS_STREAM:
        out.append(_STREAM_PREAMBLE)
    out.extend(body)
    out.extend(host_stubs)

    return "\n".join(out).rstrip() + "\n"


def emit_placement(ir: dict, package: str = "emitted") -> str:
    """The Go source for a placement runner's `emitted` package: the ordinary
    module (proxied/served interfaces, impls, load helpers) followed by the
    interop-bridge file (proxy/stub/dispatch + runner entry points). Emitted
    as two logical files concatenated with a form-feed sentinel the build step
    splits on, so each carries its own import block.

    A v3 typed-core composition (components + top-level pure declarations)
    takes the combined path: the typed-core tier and the live stc-go
    components in one module, plus the bridge — records and ADTs cross the
    seam (records as json-tagged structs, variants as {"$kind","$value"})."""
    ir = _dedup_colour_erased_poly_externs(ir)  # item 388, stage 6
    has_top_level = bool(ir.get("functions") or ir.get("types")
                         or ir.get("externs") or ir.get("tests"))
    if ir.get("ir_version") == 3:
        if not ir.get("components"):
            raise EmitError(
                "placement on the go backend needs at least one live "
                "component to boot; this v3 document is pure typed-core "
                "(no components)")
        if has_top_level:
            module = _emit_v3_placement(ir, package)
        else:
            module = emit(ir, package)
    else:
        module = emit(ir, package)
    bridge_lines = _emit_go_bridge(ir)
    if not bridge_lines:
        raise EmitError("placement needs at least one `service` to bridge")
    bridge_lines.extend(_emit_v3_bridge_helpers(ir.get("types") or {}))
    header = [
        "// Code generated by backends/go/emit.py (placement bridge) — DO NOT EDIT.",
        "// revl interop bridge over a Unix socket; wire-compatible with",
        "// backends/python/bridge.py (docs/interop-bridge.md §3).",
        "package %s" % package,
        "",
        "import (",
        '\t"encoding/json"',
        '\t"fmt"',
        "",
        '\tstc "github.com/0xdenny218/stc-go"',
        "",
        '\t"revl.goplacement/bridge"',
        ")",
        "",
        "var _ = json.Marshal",
        "var _ = fmt.Sprintf",
        "",
    ]
    bridge_src = "\n".join(header + bridge_lines) + "\n"
    # \f (form feed) sentinel separates gen.go from bridge_gen.go for the build.
    return module + "\f" + bridge_src


def main(argv):
    # `--record` (item 322 Slice 1) wires the witnessed teardown to a durable
    # WAL sink for crash recovery; off by default so ordinary emission is
    # byte-identical.
    args = [a for a in argv[1:] if a != "--record"]
    record = "--record" in argv[1:]
    if not args:
        print("usage: emit.py <ir.json> [package] [--record]", file=sys.stderr)
        return 2
    package = args[1] if len(args) > 1 else "emitted"
    with open(args[0], encoding="utf-8") as f:
        ir = json.load(f)
    sys.stdout.write(emit(ir, package, record=record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

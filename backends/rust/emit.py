"""revl backend-IR -> Rust emitter for the cordis-rs runtime.

Target: `cordis-rs` 0.3.0 (docs.rs/cordis-rs) — a Rust port of cordis 4.x, the
same runtime model the TypeScript backend targets. `emit(ir) -> str` produces
one Rust module (crate root) from an IR document.

Mapping (DESIGN.md §7 — the backend contract is small):

- service     -> `pub trait <Name>: Send + Sync { fn <m>(&self, ...) -> ... }`
- component   -> `pub fn <snake>() -> PluginHandle` built with
                 `plugin_sync` and `Inject`.
- requires    -> `Inject::new([...])` + `ctx.require::<dyn <Svc>>("key")?`
- provides    -> `impl <Svc> for <Comp><Key> { ... }` +
                 `ctx.provide_arc("key", Arc::new(impl) as Arc<dyn <Svc>>)?`
- effect/undo -> `let x = Arc::new(<acquire>); ctx.effect(label, move || { <undo>; Ok(()) })?;`
- config      -> `#[derive(Clone)] struct <Comp>Config { ... }`, read as `config.<field>`
- emit        -> a plain method call (the emission marker is a revl-checker
                 concern; at runtime it is just a call).
- format      -> `format!(...)`.

Documented limits (tracked in docs/v2.0-roadmap.md):

- Host objects (`Pool`/`Map`/`Job`) are a minimal real runtime: `Map` is a
  thread-safe `HashMap<String, V>` with `V` inferred per site from the IR
  (FR-4 — the session ledger `Map[Str, List[Msg]]` compiles), `Pool` is a real
  bounded connection pool over a deterministic in-memory database, and
  `Job::run` returns a real cancellable future.  Pool/Job semantics are
  defined once for every tier in backends/python/runtime.py under
  ".. _pool-job-semantics:".
- Component `await` steps lower to `plugin_async` + `.await`; cordis-rs drives
  the activation future for the fiber (A1).
- Config `default` values are emitted on `impl Default for <Comp>Config`, and
  the plugin body reconstructs its local config through `..Default::default()`.

CLI: `python3 emit.py <ir.json> [> out.rs]`.
"""

from __future__ import annotations

import json
import re
import sys

__all__ = ["emit", "EmitError"]

CRATE = "cordis-rs"

TYPE_MAP = {
    "Str": "String",
    "Int": "i64",
    "Int32": "i32",
    "Float": "f64",
    "Bool": "bool",
    "Bytes": "Vec<u8>",
    "Unit": "()",
}

_HOST_STUBS = {
    "Pool": {
        "open": ("Self", ["String", "i64"]),
        "close": ("()", []),
        "query": ("Vec<Value>", ["String"]),
        "execute": ("i64", ["String"]),
    },
    "Map": {
        "new": ("Self", []),
        "drop": ("()", []),
        "insert": ("()", ["String", "String"]),
        "remove": ("()", ["String"]),
        "get": ("Option<String>", ["String"]),
    },
    "Job": {
        "run": ("String", ["String"]),
    },
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_RUST_RESERVED = {
    "as", "break", "const", "continue", "crate", "else", "enum", "extern",
    "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod",
    "move", "mut", "pub", "ref", "return", "self", "Self", "static", "struct",
    "super", "trait", "true", "type", "unsafe", "use", "where", "while",
    "async", "await", "dyn",
}
_EMITTER_RESERVED = {"ctx", "config", "root", "plugin"}

# Method names that collide with Rust's `Drop::drop` destructor must be renamed.
_METHOD_RENAMES = {"drop": "drop_"}


def _mname(name: str) -> str:
    return _METHOD_RENAMES.get(name, name)


class EmitError(ValueError):
    """The IR document violates the backend contract."""


# Dispatcher conformance (roadmap item 76a). This tier converged to ONE
# expression renderer (`_render_expr`, wrapped by `_expr`) covering both IR
# dialects, so the table below has a single entry: every kind the frontend can
# produce in either position must render through it, or be deliberately
# refused with a named tier-limit EmitError — never the "unsupported
# expression kind" fall-through. The refused kinds here are genuine tier
# limits, each with a named refusal and a workaround in the emitter.
# tests/test_expr_dispatcher_conformance.py checks this table against
# src/revl/lower.py's EXPR_KINDS and against the renderer's source. `hole` is
# refused at the document level by the pre-emit walk.
EXPR_DISPATCHERS: dict[str, frozenset[str]] = {
    "renderer": frozenset({
        "adt", "arrow", "bin", "builtin", "call", "config", "field", "fn",
        "format", "host", "if", "index", "instance-get", "interp", "len",
        "list", "lit", "maplit", "match", "name", "record", "req", "spawn",
        "un", "var",
    }),
}
EXPR_REFUSED: frozenset[str] = frozenset({
    # functional record update (docs/records.md §6): refused with a named
    # error — "lift it into a helper fn instead"
    "record_update",
    # optional chaining (docs/syntax-2.0.md §3.2): refused with a named
    # error — "unwrap with `match` or `??` for now"
    "optfield", "optcall",
    "hole",
})


def _is_fn_type(name: object) -> bool:
    """Is this surface type a function type, `(P, ...) -> R`?

    A function type is the one surface spelling that is not `Head[Args]`
    (docs/function-types.md), so it can be recognised without a full parse:
    a leading parenthesised group whose closing paren is followed by `->`.
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


# Surface types whose Rust lowering is `Copy` — passing them by value never
# moves, so a repeated by-value use needs no clone. Everything else (Str, List,
# Opt, Map, records, ADTs) lowers to a non-`Copy` owned value.
_RUST_COPY_TYPES = {"Int", "Bool", "Float"}


def _arg_ref_name(arg_node: object) -> str | None:
    """The identifier an argument names, when the argument is a bare variable
    reference (`var`/`name`/`req`) rather than a fresh-temporary expression.

    Only bare references risk a use-after-move: a call, literal, or constructor
    already produces its own owned value, so it is never wrapped.
    """
    if not isinstance(arg_node, dict):
        return None
    kind = arg_node.get("kind")
    if kind == "var":
        return arg_node.get("name")
    if kind == "name":
        return arg_node.get("id") or arg_node.get("name")
    if kind == "req":
        return arg_node.get("name")
    return None


def _body_multi_use(body: object, counts: dict[str, int]) -> None:
    """Count every bare variable reference in a function body, so a binding
    consumed by value more than once can be `.clone()`d at each move.

    A `let`/`for` binding whose surface type the emitter cannot infer (a string
    index, a block expression) is typed None, so `_by_value_arg` cannot tell
    Copy from non-Copy for it. The safe fallback is reuse: a name referenced
    once can always be moved, but a name referenced more than once must clone at
    each by-value use or the second use borrows a moved value (E0382). Only the
    reference kinds (`var`/`name`/`req`) are counted; a binding's def site is a
    plain string field, so it is never miscounted as a use.
    """
    if isinstance(body, dict):
        if body.get("kind") in ("var", "name", "req"):
            ident = body.get("id") or body.get("name")
            if ident is not None:
                counts[ident] = counts.get(ident, 0) + 1
        for value in body.values():
            _body_multi_use(value, counts)
    elif isinstance(body, list):
        for item in body:
            _body_multi_use(item, counts)


def _by_value_field_clone(arg_node: object, rendered: str,
                          ctx: "_V3Ctx") -> str | None:
    """`f"{rendered}.clone()"` when the by-value argument is a non-Copy field
    read `base.field` whose `base` is used more than once in the body, else
    `None` (let the caller handle it).

    revl field access is a value READ (a copy), never a move-out of the owning
    struct: `is_bad(c.e)` then `IfN {{ cond: c.e, .. }}` reads `c.e` twice, and
    `return c` still needs the whole `c`. Rust would MOVE the non-Copy field out
    of `c` on the first by-value use, stranding every later use of `c`/`c.e`
    (E0382). So a non-Copy field read is cloned exactly when its base binding is
    reused — the same body-level reuse signal `_by_value_arg` uses for a bare
    name. A single-use base (`relabel`'s `n.at`, `n` read once) stays a move,
    byte-identical to before; a Copy field (`p.y: Int`) never clones; and an
    un-inferable base/field type is left untouched (conservative: no needless
    clone, so a fixture the emitter cannot type stays byte-identical)."""
    if not isinstance(arg_node, dict) or arg_node.get("kind") != "field":
        return None
    # Walk a (possibly nested) field chain `root.f1.f2..` down to its root
    # binding: `ck.ctx_.caps` partially moves `ck`, so the root's reuse is what
    # decides the clone, and the field type is resolved along the chain.
    chain: list[str] = []
    cur = arg_node
    while isinstance(cur, dict) and cur.get("kind") == "field":
        chain.append(cur.get("name"))
        cur = cur.get("target")
    if not (isinstance(cur, dict) and cur.get("kind") in ("var", "name", "req")):
        return None  # a root that is itself an index/call is not a plain reuse
    root = cur.get("id") or cur.get("name")
    if root is None or root not in ctx.multi_use:
        return None
    ty = ctx.var_types.get(root)
    for field in reversed(chain):
        ty = (ctx.record_field_types(ty).get(field)
              if isinstance(ty, str) else None)
    # A known-Copy field (`p.y: Int`) never needs a clone; a known non-Copy
    # field, or one whose type the emitter cannot resolve (a loop/match binding
    # root), is cloned — the root is reused, so moving the field out would strand
    # the later use (E0382). An un-inferable Copy field is the only false clone
    # this admits, and cloning a Copy value is sound, just redundant.
    if isinstance(ty, str) and ty.split("[", 1)[0].strip() in _RUST_COPY_TYPES:
        return None
    return f"{rendered}.clone()"


def _by_value_arg(arg_node: object, rendered: str, ctx: "_V3Ctx") -> str:
    """Wrap a by-value free-function argument so the call cannot consume a value
    the caller still needs (E0382). revl passes values, not moves, so a non-Copy
    value argument is `.clone()`d — always sound, since revl values are immutable
    — and a function-typed argument is passed by reference (`&F: Fn`), because an
    `impl Fn(..)` parameter is not `Clone`. Copy scalars and non-identifier
    argument expressions (fresh temporaries) are passed through untouched.

    When the argument's surface type is unknown (a binding the emitter could not
    infer, such as a string index or a block expression), Copy cannot be ruled
    out, so the clone decision falls back to reuse: a name used more than once in
    the body is cloned at each by-value move, while a single-use name is left
    untouched. That keeps a lone move byte-identical to before and clones only
    the reused shape that would otherwise fail to build.
    """
    field_clone = _by_value_field_clone(arg_node, rendered, ctx)
    if field_clone is not None:
        return field_clone
    name = _arg_ref_name(arg_node)
    if name is None:
        return rendered
    if name in ctx.borrowed_params:
        # This argument is a borrowed `&str` parameter reaching an OWNED slot (a
        # `String` callee param, a record field, a constructor payload). An owned
        # `String` is produced with `.to_string()`, never `.clone()` (which stays
        # a `&str`). The borrow analysis keeps a borrowed param out of owned
        # slots, so this is a safety net that materialises rather than miscompiles.
        return f"{rendered}.to_string()"
    ty = ctx.var_types.get(name)
    if ty is not None:
        if _is_fn_type(ty):
            return f"&{rendered}"
        if str(ty).split("[", 1)[0].strip() in _RUST_COPY_TYPES:
            return rendered
        return f"{rendered}.clone()"
    if name in ctx.multi_use:
        return f"{rendered}.clone()"
    return rendered


def _borrow_str_arg(arg_node: object, rendered: str, ctx: "_V3Ctx") -> str:
    """Render an argument bound for a borrowed `&str` callee parameter (item 282).

    The callee only reads the string, so the caller lends a borrow instead of
    cloning it. A bare borrowed `&str` parameter passed straight through is
    already a `&str`, so it goes untouched (no needless re-borrow); every other
    string expression — an owned `String` local, a literal, a call result — is
    borrowed with `&`, and `&String`/`&&str` both coerce to the `&str` slot.
    """
    name = _arg_ref_name(arg_node)
    if name is not None and name in ctx.borrowed_params:
        return rendered
    if isinstance(arg_node, dict) and arg_node.get("kind") in _ATOMIC_KINDS:
        return f"&{rendered}"
    return f"&({rendered})"


def _by_value_tail(node: object, rendered: str, ctx: "_V3Ctx") -> str:
    """Clone a bare identifier used as the tail VALUE of an `if`-expression
    branch when the same binding is read again in the body.

    Unlike a call argument (always consumed), a branch tail only strands a later
    use when the binding is reused: `let kind = if .. { "arrow" } else { op }`
    moves `op`, so a following `op.len()` borrows a moved value (E0382). A
    single-use tail stays a move (byte-identical to before, no needless clone),
    and a known-Copy binding never clones. Reuse is the same body-level signal
    `_by_value_arg` uses for an un-inferred value.
    """
    name = _arg_ref_name(node)
    if name is None:
        return rendered
    if name in ctx.borrowed_params:
        # A borrowed `&str` param materialised into an owned position (a branch
        # tail typed `Str`) becomes an owned `String` via `.to_string()`, never
        # `.clone()` (which would keep it a `&str`). The borrow analysis keeps a
        # borrowed param out of owned positions, so this is a safety net.
        return f"{rendered}.to_string()"
    ty = ctx.var_types.get(name)
    if ty is not None and str(ty).split("[", 1)[0].strip() in _RUST_COPY_TYPES:
        return rendered
    if name in ctx.multi_use:
        return f"{rendered}.clone()"
    return rendered


def _by_value_reuse(node: object, rendered: str, ctx: "_V3Ctx") -> str:
    """Clone a value used by-value in a position that moves ONLY on reuse -- a
    `match` scrutinee, a list element, a builtin/host method argument, a `for`
    iterable. Unlike `_by_value_arg` (a free-fn/service argument, always passed
    by value, so a known non-Copy is cloned even when used once), these positions
    consume the value in place, so a single use can stay a move (byte-identical
    to before) and only a REUSED binding must clone: a bare non-Copy name read
    again in the body, or a non-Copy field whose base is reused. `_body_multi_use`
    is the reuse signal; a Copy value and a fresh temporary go through untouched."""
    field_clone = _by_value_field_clone(node, rendered, ctx)
    if field_clone is not None:
        return field_clone
    return _by_value_tail(node, rendered, ctx)


# `Str` values that a callee only READS lower to a borrowed `&str` parameter
# instead of an owned `String`, so the call passes a borrow rather than cloning
# the whole string (item 282). These are the string builtins whose *argument*
# reads its operand — after the `RevlStrOps`/`RevlStrListOps` traits take `&str`
# arguments, a `&str` operand coerces into them exactly as a `String` does — so
# a borrowed param handed to one of these arg slots stays read-only.
_STR_READONLY_ARG_BUILTINS = frozenset({
    "concat", "indexOf", "split", "join", "startsWith", "endsWith",
})

# Builtins whose `_v3_builtin` lowering takes its argument BY REFERENCE (`&arg`),
# so the argument is never moved and needs no reuse clone: the read-only string
# ops above plus the Map key probes (`lookup`/`has`/`remove` all borrow `&key`).
# Every other builtin (`push`, `set`, ...) MOVES its non-Copy argument in.
_BORROW_ARG_BUILTINS = _STR_READONLY_ARG_BUILTINS | frozenset({
    "lookup", "has", "remove",
})


def _free_fn_call(node: object, function_names: "frozenset[str] | set") -> tuple:
    """`(callee_name, arg_nodes)` when `node` is a call to a first-party free
    function, else `(None, None)`.

    Only free functions declared in this document are borrow-aware: their
    parameter list is under the emitter's control, so a read-only `Str` param
    can be lowered to `&str`. Externs (their `@rs` body is hand-written against
    `String`), constructors, service methods, and closure-valued locals are not,
    so a call to any of those is treated as a by-value boundary.
    """
    if not isinstance(node, dict):
        return None, None
    kind = node.get("kind")
    if kind == "fn":
        name = node.get("name")
    elif kind == "call" and isinstance(node.get("callee"), dict):
        callee = node["callee"]
        name = callee.get("name") if callee.get("kind") == "var" else None
    else:
        return None, None
    if name in function_names:
        return name, node.get("args") or []
    return None, None


def _str_param_escapes(body: object, params: "set[str]",
                       fn_borrow: "dict[str, frozenset]",
                       function_names: "frozenset[str] | set") -> "set[str]":
    """The subset of `params` (a function's `Str` parameter names) that a `&str`
    lowering could NOT represent: they reach a position that needs an owned
    `String`.

    A bare parameter reference is read-only — safe to borrow — only where its
    immediate slot proves it: the receiver of a builtin (every `Str` builtin is
    pure), a `&str` builtin argument, an equality/`+` operand or an interpolation
    hole (all read by reference), or an argument to a free-fn slot the fixpoint
    still holds borrowable. Every other slot (a `return` value, a record field, a
    constructor/ADT payload, a `List` element, a `let` right-hand side, an owned
    call argument) moves the value, so a parameter reached there escapes and must
    stay an owned `String`. Unhandled shapes recurse in the escaping position, so
    the pass only ever borrows a provably read-only parameter.
    """
    escaped: set[str] = set()

    def walk(node: object, safe: bool) -> None:
        if isinstance(node, dict):
            kind = node.get("kind")
            if kind in ("var", "name", "req"):
                ident = node.get("id") or node.get("name")
                if ident in params and not safe:
                    escaped.add(ident)
                return
            if kind == "builtin":
                walk(node.get("target"), True)
                arg_safe = node.get("method") in _STR_READONLY_ARG_BUILTINS
                for a in node.get("args") or []:
                    walk(a, arg_safe)
                return
            if kind == "len":
                walk(node.get("target"), True)
                return
            if kind == "interp":
                for _pk, pv in node.get("parts") or []:
                    walk(pv, True)
                return
            if kind == "bin":
                op = node.get("op")
                operand_safe = op in ("==", "!=", "+")
                walk(node.get("left"), operand_safe)
                walk(node.get("right"), operand_safe)
                return
            cname, arg_nodes = _free_fn_call(node, function_names)
            if cname is not None:
                borrow = fn_borrow.get(cname, frozenset())
                for idx, a in enumerate(arg_nodes):
                    walk(a, idx in borrow)
                return
            for value in node.values():
                walk(value, False)
        elif isinstance(node, list):
            for item in node:
                walk(item, safe)

    walk(body, False)
    return escaped


def _functions_use_stdlib(functions: list) -> bool:
    """True when some function body carries a `builtin`/`len` node — the same
    stdlib signal `_uses_stdlib` gates the helper traits on, scoped to functions
    (the only surface the borrow pass reshapes)."""
    found = False

    def walk(node: object) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            if node.get("kind") in ("builtin", "len"):
                found = True
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(functions)
    return found


def _compute_str_param_borrows(functions: list) -> "dict[str, frozenset]":
    """Map each free function to the set of parameter INDICES whose `Str`
    argument the callee only reads, so the call can pass `&str` (a borrow)
    instead of cloning the whole string (item 282).

    This is an interprocedural fixpoint: a parameter threaded straight through to
    another function's parameter is borrowable only when THAT parameter is, and
    the self-host lexer threads `source` through a dozen scan helpers. Start with
    every `Str` parameter a candidate, then repeatedly drop any that
    `_str_param_escapes` finds in an owned position (including a pass-through to a
    slot that has itself just been dropped). The set only shrinks, so it settles.

    A `pub` function is exempt: its signature is the module's external contract,
    called by hand-written Rust (the bench/test harness main, an embedder) that
    expects the owned `Str` lowering (`String`), so its params stay owned. The
    hot per-token clone item 282 targets lives in the module-internal scan
    helpers, which a `pub` entry still lends its owned string to by borrow.

    The pass is gated on the module actually using the stdlib (a `builtin`/`len`
    node in some function): borrowing only reshapes the string-scanning surface,
    which is exactly the surface the self-hosted `emit_rust.rvl` port leaves OUT,
    so a stdlib-free module (`fn cat(a, b) = a + b`) still emits byte-identically
    to that port. A module with no builtin has nothing item 282 speeds up anyway.
    """
    if not _functions_use_stdlib(functions):
        return {fn.get("name"): frozenset() for fn in functions}
    function_names = frozenset(
        fn.get("name") for fn in functions if fn.get("name")
    )
    borrow: dict[str, frozenset] = {}
    for fn in functions:
        name = fn.get("name")
        if fn.get("public"):
            borrow[name] = frozenset()
            continue
        borrow[name] = frozenset(
            idx for idx, p in enumerate(fn.get("params") or [])
            if p.get("type") == "Str"
        )
    changed = True
    while changed:
        changed = False
        for fn in functions:
            name = fn.get("name")
            candidates = {
                p.get("name") for idx, p in enumerate(fn.get("params") or [])
                if idx in borrow[name]
            }
            if not candidates:
                continue
            escaped = _str_param_escapes(
                fn.get("body") or [], candidates, borrow, function_names)
            if escaped:
                kept = frozenset(
                    idx for idx in borrow[name]
                    if (fn.get("params") or [])[idx].get("name") not in escaped
                )
                if kept != borrow[name]:
                    borrow[name] = kept
                    changed = True
    return borrow


def _render_param_type(borrowed: bool, ftype: object, types: dict) -> str:
    """The Rust type of a free-function parameter, `&str` when the borrow
    analysis lowered this read-only `Str` param to a borrow (item 282), else the
    owned lowering `_rust_type` gives it."""
    if borrowed:
        return "&str"
    return _rust_type(ftype, types, position="param")


# A declared function type still has no single Rust lowering — but the choice
# is not a guess once the *position* is known (docs/function-types.md §4).
# `_rust_type` threads that position: a `fn`/`extern` parameter or return is an
# `impl Fn(..)` (rustc monomorphises it, exactly as a hand-written Rust
# signature would), while an *escaping* position — a struct field, an ADT
# payload, a `List`/`Opt`/`Map` element, a service-method signature (a trait
# object's methods must stay object-safe) — still has no representation the
# emitter can construct without boxing arrows at their creation site, so those
# remain refused by name. Locals were never affected: rustc infers their
# closure type.
_FN_TYPE_REFUSAL = (
    "a declared function type ({name}) is not lowerable in an escaping "
    "position on the Rust tier. A `fn`/`extern` parameter or return now lowers "
    "to `impl Fn(..)`, but a value that escapes — a struct field, an ADT "
    "payload, a `List`/`Opt`/`Map` element, or a service-method signature — "
    "wants `Box<dyn Fn(..)>` constructed where the arrow is created, which "
    "revl's type does not yet carry enough position to do. Arrows bound to a "
    "local `let` and called in the same function still lower (rustc infers the "
    "closure type). See docs/function-types.md."
)

# Positions in which `impl Fn(..)` is a valid, monomorphisable lowering: the
# argument and return positions of a free `fn`/`extern`. Everywhere else a
# function type either escapes (must box) or would break trait object-safety.
_IMPL_FN_POSITIONS = frozenset({"param", "return"})


def _rust_fn_type(name: str, types: dict | None, position: str) -> str:
    """A declared function type `(P, ...) -> R` -> its Rust lowering.

    `param`/`return` positions get `impl Fn(P, ...) -> R`; any other position
    escapes and is refused by name (see `_FN_TYPE_REFUSAL`).
    """
    if position not in _IMPL_FN_POSITIONS:
        raise EmitError(_FN_TYPE_REFUSAL.format(name=name))
    params, ret = _split_fn_type(name)
    rendered = ", ".join(_rust_type(p, types) for p in params)
    return f"impl Fn({rendered}) -> {_rust_type(_erase_async(ret), types)}"


def _erase_async(ret: str) -> str:
    """Erase the async color from a function-type return (roadmap item 92/94).

    `Async[T]` colors a first-class callback on the py/ts tiers (async/await);
    the rust tier has no async-fn machinery, so an `Async[T]` return erases to
    its concrete `T` — `(Str) -> Async[Str]` lowers `impl Fn(String) -> String`,
    not the `impl Fn(String) -> Value` that leaked when the unknown `Async` head
    fell through to the opaque `Value` fallback. `Async` is position-restricted
    to a fn-type return (typecheck.py), so this is the only site it reaches here.
    """
    r = ret.strip()
    if r.startswith("Async[") and r.endswith("]"):
        return r[len("Async["):-1].strip()
    return ret


def _split_fn_type(name: str) -> tuple[list[str], str]:
    """Split `(P1, P2, ...) -> R` into its parameter types and return type.

    The leading parenthesised group holds the (comma-separated) parameters; the
    type after the matching `->` is the return, itself a full type. `() -> R`
    yields no parameters.
    """
    text = name.strip()
    depth = 0
    for i, ch in enumerate(text):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
            if depth == 0:
                inner = text[1:i].strip()
                rest = text[i + 1:].lstrip()
                ret = rest[2:].strip() if rest.startswith("->") else ""
                params = _split_generic(inner) if inner else []
                return params, ret
    raise EmitError(f"malformed function type: {name!r}")


def _rust_type(name: object, types: dict | None = None,
               position: str = "value") -> str:
    """Surface type -> Rust type. Unknown named types map to cordis `Value`.

    When `types` is supplied (IR v3), user record/variant names are mapped to
    their emitted Rust type names instead of the opaque `Value` fallback.

    `position` selects the lowering for a declared function type (see
    `_rust_fn_type`); it defaults to the escaping `"value"` position, so a
    function type nested inside a container (`List[(Int) -> Int]`) is refused
    even when the enclosing declaration is a parameter.
    """
    if not isinstance(name, str) or not name:
        return "Value"
    if _is_fn_type(name):
        return _rust_fn_type(name, types, position)
    if name in TYPE_MAP:
        return TYPE_MAP[name]
    types = types or {}
    if name in types:
        return _ident(name, "type name")
    generic = re.match(r"^(\w+)\[(.+)\]$", name)
    if generic:
        head, inner = generic.group(1), generic.group(2)
        if head == "List":
            return f"Vec<{_rust_type(inner, types)}>"
        if head == "Opt":
            return f"Option<{_rust_type(inner, types)}>"
        if head == "Map":
            k, v = _split_generic(inner)
            return f"std::collections::HashMap<{_rust_type(k, types)}, {_rust_type(v, types)}>"
        if head == "Result":
            ok, err = _split_generic(inner)
            return f"Result<{_rust_type(ok, types)}, {_rust_type(err, types)}>"
    return "Value"


def _split_generic(inner: str) -> list[str]:
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


def _mangle(name: str) -> str:
    """Rename a syntactically-valid identifier that collides with a *Rust*
    reserved word (`fn`, `impl`, `match`, `struct`, `move`, …) so a valid revl
    identifier that happens to be a Rust keyword emits and RUNS instead of
    crashing at emit (roadmap item 165).

    The scheme is the A3 append-`_` rename `src/revl/lower.py::_safe_name` (and
    `backends/java/emit.py::_fn_name`) already use for revl keywords: append `_`
    until the name is free. It is a pure function of the name, so the
    declaration site and every use site agree without a table. A non-reserved
    name is returned unchanged, so no existing program — none of which can name
    a Rust keyword, those crash today — changes its emitted output.

    The same rename also covers `_EMITTER_RESERVED` (`ctx`/`config`/`root`/
    `plugin`, roadmap item 269): those names are reserved because the emitter's
    OWN scaffolding emits them as bare tokens (`|ctx, config|`, `self.ctx`), but
    a user record field or local of the same name is legitimate (selfhost/
    checker.rvl has a `ctx` field and a `ctx` local). Escaping the user name to
    `ctx_`, which the emitter never emits raw, moves it clear of the scaffolding
    rather than refusing the program, and keeps the reservation: the emitter's
    internal `ctx` still owns the bare `ctx` token."""
    while name in _RUST_RESERVED or name in _EMITTER_RESERVED:
        name += "_"
    return name


def _ident(name: object, role: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise EmitError(f"invalid {role} identifier: {name!r}")
    return _mangle(name)


def _snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and (name[i - 1].islower() or name[i - 1].isdigit()):
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _string(value) -> str:
    """A Rust double-quoted string literal, escaped *from code points*.

    The IR stores a `Str` literal as Unicode scalar values (docs/strings.md).
    Rust source is UTF-8 and spells a non-ASCII scalar as `\\u{XXXX}` — it
    rejects the lone-surrogate `\\uXXXX` escapes `json.dumps` emits, which is
    why every non-ASCII literal used to fail to compile on this tier.

    One escape dialect for the whole function: a non-`str` input (the list of
    requirement names behind `Inject::new([...])`) is serialized structurally,
    with every string element escaped through the same code-point path. The
    old `json.dumps` fallback is gone — byte-stability is never grounds for a
    dual code path (docs/conformance.md, "Golden policy").
    """
    if isinstance(value, list):
        return "[" + ", ".join(_string(item) for item in value) + "]"
    if not isinstance(value, str):
        raise EmitError(f"cannot serialize {type(value).__name__} as a Rust "
                        f"string literal: {value!r}")
    out = ['"']
    for ch in value:
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
        elif cp >= 0x80 and ch.isprintable():
            # Rust source is UTF-8, so a printable non-ASCII scalar can appear
            # literally inside a `"..."` string. Emitting it literally (rather
            # than `\u{XXXX}`) keeps the literal free of braces, which is what
            # makes it safe in a format-macro position: an `assert!`/`format!`
            # argument re-scans the string's *value* for `{...}`, and a
            # `\u{2014}` that reaches such a position (directly, or after a
            # second escaping pass has turned it into a literal `\u{2014}` in
            # the source) is read as `{2014}` -> "invalid reference to
            # positional argument 2014". The literal form has no brace to
            # collide, and the fix is position-independent. `\u{...}` is
            # reserved below for the lone-surrogate / unprintable case, which
            # cannot appear literally in UTF-8 source. (item 135, finding #35)
            out.append(ch)
        else:
            out.append("\\u{%x}" % cp)
    out.append('"')
    return "".join(out)


def _rust_v3_lit(node: dict) -> str:
    """v3 literal: strings are owned `String` (revl `Str` is `String`)."""
    value = node.get("value")
    if value is None:
        return "None"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return f"String::from({_string(value)})"
    if isinstance(value, int):
        return f"{value}i64"
    if isinstance(value, float):
        return f"{value}f64"
    raise EmitError(f"unsupported literal node: {node!r}")


def _default_for_rust_type(ftype: str) -> str:
    """Return a conservative Rust default expression for a config field type."""
    if ftype == "String":
        return "String::new()"
    if ftype == "i64":
        return "0i64"
    if ftype == "i32":
        return "0i32"
    if ftype == "f64":
        return "0f64"
    if ftype == "bool":
        return "false"
    if ftype == "Vec<u8>":
        return "Vec::new()"
    if ftype == "()":
        return "()"
    return "Default::default()"


def _config_default_lit(value: object, ftype: str) -> str:
    """Render an IR config default (or a type fallback) as a Rust expression."""
    if value is None:
        return _default_for_rust_type(ftype)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return f"String::from({_string(value)})"
    if isinstance(value, int):
        return f"{value}i64"
    if isinstance(value, float):
        return f"{value}f64"
    raise EmitError(f"unsupported config default value: {value!r}")


def _intercept_json_type(value, base: str, defs: list[str], type_names: dict, counter: list[int]) -> str:
    """Return a concrete Rust type for an intercept metadata value.

    Dictionaries become generated `struct`s (preserving field names) because
    cordis-rs has no serde_json dependency; lists become `Vec<T>`. The generated
    definitions are appended to `defs` so they can be emitted once.
    """
    if isinstance(value, dict):
        if id(value) in type_names:
            return type_names[id(value)]
        counter[0] += 1
        name = f"{base}Intercept{counter[0]}"
        type_names[id(value)] = name
        lines = ["#[derive(Clone)]", f"struct {name} {{"]
        for key, item in value.items():
            field = _ident(key, "intercept field")
            field_type = _intercept_json_type(item, name, defs, type_names, counter)
            lines.append(f"    {field}: {field_type},")
        lines.append("}")
        defs.extend(lines)
        defs.append("")
        return name
    if isinstance(value, list):
        if not value:
            return "Vec<()>"
        item_type = _intercept_json_type(value[0], base, defs, type_names, counter)
        for item in value[1:]:
            if _intercept_json_type(item, base, defs, type_names, counter) != item_type:
                raise EmitError("heterogeneous intercept metadata arrays are not supported")
        return f"Vec<{item_type}>"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "i64"
    if isinstance(value, float):
        return "f64"
    if value is None:
        return "()"
    if isinstance(value, str):
        return "String"
    raise EmitError(f"unsupported intercept metadata value: {value!r}")


def _intercept_json_lit(value, base: str, defs: list[str], type_names: dict, counter: list[int]) -> str:
    if isinstance(value, dict):
        name = _intercept_json_type(value, base, defs, type_names, counter)
        fields = ", ".join(
            f"{_ident(k, 'intercept field')}: {_intercept_json_lit(v, base, defs, type_names, counter)}"
            for k, v in value.items()
        )
        return f"{name} {{ {fields} }}"
    if isinstance(value, list):
        items = ", ".join(
            _intercept_json_lit(item, base, defs, type_names, counter) for item in value
        )
        return f"vec![{items}]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i64"
    if isinstance(value, float):
        return f"{value}f64"
    if value is None:
        return "()"
    if isinstance(value, str):
        return f"String::from({_string(value)})"
    raise EmitError(f"unsupported intercept metadata value: {value!r}")


# ---------------------------------------------------------------------------
# item 243 Slice 2b (rust): the witnessed-effects three-entry-kind teardown
# loop. docs/design/teardown-contract.md is authoritative; the runtime
# mechanism is `_revl_teardown_preamble` below. This block only DETECTS
# whether a component needs that mechanism, so a program using neither a
# `witnessed` extern nor `emit ... compensate` emits byte-identically to
# before this slice (mirrors backends/python/emit.py's `self.witnessed` gate).
# ---------------------------------------------------------------------------


def _step_is_witnessed_effect(step: dict, witnessed: dict) -> bool:
    """Is this an `effect`/`let-effect` step whose acquisition calls a
    `witnessed` extern? Mirrors `backends/python/emit.py`'s
    `_witnessed_extern` name-match (the checker refuses a witnessed call
    anywhere else, so this is the only shape a component body carries one
    in)."""
    if not witnessed or step.get("step") not in ("effect", "let-effect"):
        return False
    acquire = step.get("acquire")
    return (isinstance(acquire, dict) and acquire.get("kind") == "fn"
            and acquire.get("name") in witnessed)


def _body_has_witnessed(steps: list | None, witnessed: dict) -> bool:
    for step in steps or []:
        if _step_is_witnessed_effect(step, witnessed):
            return True
        if step.get("step") == "if":
            if (_body_has_witnessed(step.get("then"), witnessed)
                    or _body_has_witnessed(step.get("else"), witnessed)):
                return True
    return False


def _body_has_compensation(steps: list | None) -> bool:
    for step in steps or []:
        if step.get("step") == "emit" and step.get("compensate") is not None:
            return True
        if step.get("step") == "if":
            if (_body_has_compensation(step.get("then"))
                    or _body_has_compensation(step.get("else"))):
                return True
    return False


def _method_bodies_have_compensation(component: dict) -> bool:
    for step in component.get("body") or []:
        if step.get("step") != "provide":
            continue
        for method in step.get("methods") or []:
            if _body_has_compensation(method.get("body")):
                return True
    return False


def _method_bodies_have_witnessed(component: dict, witnessed: dict) -> bool:
    """item 324: does any PROVIDE-METHOD body carry a witnessed effect (a
    per-tool-call fs mutation)? The per-tool-call H1 gate: unlike a witnessed
    effect in the ACTIVATION body (which `revl_teardown_begin` already sees),
    one that fires from a method registers its transactional inverse into the
    enclosing component's activation frame LATER, per request — the component
    still needs the `RevlTeardown` accumulator built at activation so
    `revl_teardown_of` can recover it. Mirrors `_method_bodies_have_compensation`
    above (the method-body compensation case Slice 2b already handled)."""
    for step in component.get("body") or []:
        if step.get("step") != "provide":
            continue
        for method in step.get("methods") or []:
            if _body_has_witnessed(method.get("body"), witnessed):
                return True
    return False


def _component_needs_teardown(component: dict, witnessed: dict) -> bool:
    body = component.get("body") or []
    return (_body_has_witnessed(body, witnessed)
            or _body_has_compensation(body)
            or _method_bodies_have_compensation(component)
            or _method_bodies_have_witnessed(component, witnessed))


# item 322 Slice 2: record mode. When True, a witnessed transactional step also
# writes a durable discharge-descriptor to the rust WAL sink
# (revl_record_transactional) and the recording preamble is emitted. Default
# False -> byte-identical output (every existing golden is the guard). Mirrors
# backends/go/emit.py's `_RECORD_MODE`.
_RECORD_MODE = False


def _witnessed_extern_for(env: "_Env", acquire: object) -> dict | None:
    """The witnessed extern descriptor a step's acquisition calls, or None.
    Mirrors `backends/python/emit.py::_ComponentEmitter._witnessed_extern`."""
    if not env.witnessed or not isinstance(acquire, dict):
        return None
    if acquire.get("kind") != "fn":
        return None
    return env.witnessed.get(acquire.get("name"))


class _Env:
    """Per-component lowering context."""

    def __init__(
        self,
        component: dict,
        services: dict,
        types: dict | None = None,
        functions: list | None = None,
        externs: list | None = None,
        components: list | None = None,
    ):
        self.component = component
        self.services = services
        self.name = component["name"]
        self.reqs: dict[str, str] = dict(component.get("requires") or {})
        self.provides: dict[str, str] = dict(component.get("provides") or {})
        # item 167: routed requires (item 162's `routes` IR) — a required key
        # bound across N named realms with a strategy. Its handle is a router
        # struct (re-resolving live workers per call), not a single `ctx.require`.
        self.routes: dict[str, dict] = dict(component.get("routes") or {})
        self.types: dict = types or {}
        self.functions: list = functions or []
        self.externs: list = externs or []
        # Every component in the document, so a `spawn` acquisition can resolve
        # its target template's config shape (whether it takes a typed
        # `<Comp>Config` or unit `()`), which the shared expression renderer
        # needs to build the plug-time config value.
        self.components: list = components or []
        self._v3_ctx: _V3Ctx | None = None
        # Per-component counter for unique timer local names (item 57).
        self.timer_counter = 0
        # Per-component counter for unique witnessed-step temp names (item 243).
        self.wit_counter = 0
        # Activation-body `let`/`let-effect` bind names seen so far, in source
        # order — so a later `emit ... compensate` closure knows which of the
        # names it references are local Arc-wrapped bindings that need a
        # `.clone()` before the `move` closure captures them (item 247 / the
        # teardown-contract two-phase abort). Populated by `_emit_step` as it
        # walks the body.
        self.activation_binds: list[str] = []
        # item 243 (docs/design/243-witnessed-externs.md): witnessed externs by
        # name, so an activation-body call site can be recognised as a
        # transactional effect and register its DECLARED inverse (not a
        # site-spelled one) into the teardown accumulator. Absent/empty for
        # every program that uses no witnessed extern, so their emission stays
        # byte-identical (mirrors backends/python/emit.py's `self.witnessed`).
        self.witnessed: dict[str, dict] = {
            ext["name"]: ext for ext in self.externs
            if ext.get("class") == "witnessed"
        }
        # docs/design/teardown-contract.md: does this component need the
        # per-activation teardown accumulator (RevlTeardown) at all? True iff
        # it registers at least one `transactional` (witnessed) or
        # `compensation` (`emit ... compensate`, activation- or method-body)
        # entry. Gated so a program using neither emits byte-identically to
        # before this slice.
        self.needs_teardown: bool = _component_needs_teardown(component, self.witnessed)

    def v3_ctx(self) -> _V3Ctx:
        if self._v3_ctx is None:
            self._v3_ctx = _V3Ctx(self.types, self.functions, self.externs,
                                  self.components)
        return self._v3_ctx


def _expr(node: dict, env: _Env, rename: dict[str, str] | None = None) -> str:
    """Lower a component-body expression via the single Rust expression renderer.

    Component bodies carry a per-scope ``rename`` map (``pool`` -> ``self.pool``
    for captured effects/requirements); the shared type table lives on
    ``env.v3_ctx()``. Both are handed to ``_render_expr``, which is the one
    function that lowers every IR expression kind on this tier.
    """
    return _render_expr(node, env.v3_ctx(), rename or {})


def _format(template: str, args: list[str]) -> str:
    # IR format templates use `$0`/`$1` placeholders and `$$` for a literal `$`
    # (A4). Map them onto Rust `format!` `{0}`/`{1}`. Literal text must have
    # its braces doubled or `format!` reads them as (invalid) format specs.
    def literal(text: list[str]) -> str:
        return "".join(text).replace("{", "{{").replace("}", "}}")

    pieces, i, buf = [], 0, []
    while i < len(template):
        ch = template[i]
        if ch == "$":
            if template[i : i + 2] == "$$":
                buf.append("$")
                i += 2
                continue
            j = i + 1
            while j < len(template) and template[j].isdigit():
                j += 1
            if j > i + 1:
                pieces.append(literal(buf) + "{" + template[i + 1 : j] + "}")
                buf = []
                i = j
                continue
        buf.append(ch)
        i += 1
    pieces.append(literal(buf))
    return f"format!({_string(''.join(pieces))}, {', '.join(args)})"


def _emit_service_traits(services: dict, types: dict | None = None) -> list[str]:
    types = types or {}
    out: list[str] = []
    for sname, service in services.items():
        _ident(sname, "service")
        out.append(f"pub trait {sname}: Send + Sync {{")
        for mname, method in (service.get("methods") or {}).items():
            _ident(mname, "method")
            params = ", ".join(
                f"{_ident(p.get('name'), 'parameter')}: {_rust_type(p.get('type'), types)}"
                for p in method.get("params") or []
            )
            ret = _rust_type(method.get("returns"), types) if method.get("returns") else "()"
            if method.get("emission"):
                out.append("    /// emission: crosses the system boundary (DESIGN.md §3.5)")
            if method.get("idempotent"):
                out.append("    /// idempotent: safe to re-deliver — the runtime "
                           "may auto-retry a transient failure (item 44)")
            out.append(f"    fn {mname}(&self, {params}) -> {ret};")
        out.append("}")
        out.append("")
    return out


def _emit_host_stubs(ir: dict) -> list[str]:
    """Emit the minimal revl host runtime for objects used by this document.

    The objects are intentionally small and dependency-free (std only): ``Map``
    is a real ``String -> String`` map, ``Pool`` is a real bounded connection
    pool over a deterministic in-memory database, and ``Job`` is a real
    cancellable future so component ``await`` steps have genuine async state to
    await.  None of them panic with ``todo!()``.

    Pool/Job semantics are normative and shared across tiers — see
    ``backends/python/runtime.py``, section ``.. _pool-job-semantics:``.  This
    tier reports misuse (exhaustion, use-after-close, awaiting a cancelled
    job) by panicking, because the emitted call sites are infallible.
    """
    used: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("kind") == "host":
                used.add((node.get("fn") or "").split(".")[0])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(ir.get("components"))
    walk(ir.get("functions"))
    walk(ir.get("tests"))
    walk(ir.get("types"))

    out: list[str] = []
    lifecycle_present = any(t.get("lifecycle") for t in (ir.get("tests") or []))
    # A timer takes a live-resource slot on arming (its schedule ↔ cancel joins
    # the same R1 acquire/release accounting), so it needs the counter too.
    timer_present = _uses_timer(ir.get("components") or [])
    # item 130: a stream provider/subscription is a live host resource too — it
    # takes a slot on acquisition and returns it in `close`, so a listener that
    # outlives its owner surfaces as residue.
    if ("Map" in used or "Pool" in used or "Stream" in used
            or lifecycle_present or timer_present):
        # R1 live-resource accounting (docs/backend-ir.md §Required semantics,
        # the same pairing the py reference tier's `assert no_residue` checks):
        # every host object acquired must be released by its `undo`, or the
        # lifecycle `assert no_residue` fails. The counter is process-wide, so
        # it is per-test and cross-test safe: a clean test returns to zero.
        # Thread-local: `cargo test` runs each #[test] on its own thread, and
        # a process-wide counter would race across tests running in parallel.
        out.extend([
            "/// R1 live-resource counter (lifecycle `assert no_residue`).",
            "/// Thread-local because `cargo test` runs tests on parallel",
            "/// threads: each test must observe only its own acquisitions.",
            "thread_local! {",
            "    static REVL_LIVE_HOST_RESOURCES: std::cell::Cell<i64> = const {",
            "        std::cell::Cell::new(0)",
            "    };",
            "}",
            "",
        ])
    if "Map" in used:
        out.extend(
            [
                # FR-4 (docs/v2.0-roadmap.md item 77(c)): the host Map is
                # generic over its value type, `V` inferred per site from how
                # the map is used (the session ledger is `Map[Str, List[Msg]]`).
                # Every revl value type derives Clone on this tier, so the
                # `get` copy (via `.cloned()`) is total.
                "/// revl host object: a small thread-safe map with String keys.",
                "/// The value type is generic — each site's `Map.new()` pins `V`",
                "/// (FR-4: `Map[Str, List[Msg]]` and friends, not just String).",
                "pub struct Map<V> {",
                "    inner: std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, V>>>,",
                "}",
                "impl<V> Map<V> {",
                "    pub fn new() -> Self {",
                "        REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() + 1));",
                "        Self {",
                "            inner: std::sync::Arc::new(std::sync::Mutex::new(std::collections::HashMap::new())),",
                "        }",
                "    }",
                "    pub fn drop_(&self) {",
                "        REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() - 1));",
                "        self.inner.lock().unwrap().clear();",
                "    }",
                "    pub fn insert(&self, key: String, value: V) {",
                "        self.inner.lock().unwrap().insert(key, value);",
                "    }",
                "    // The atomic compare-and-set (item 397). ONE `lock()` spans the",
                "    // membership test AND the insert via the entry API, so no thread",
                "    // can witness the probe and the write as separable steps. Returns",
                "    // whether it inserted; a `false` (key present) leaves the existing",
                "    // value untouched. Under N concurrent callers, exactly one `true`.",
                "    pub fn insert_if_absent(&self, key: String, value: V) -> bool {",
                "        use std::collections::hash_map::Entry;",
                "        match self.inner.lock().unwrap().entry(key) {",
                "            Entry::Occupied(_) => false,",
                "            Entry::Vacant(e) => { e.insert(value); true }",
                "        }",
                "    }",
                "    pub fn remove(&self, key: &String) {",
                "        self.inner.lock().unwrap().remove(key);",
                "    }",
                "    // Iteration surface (docs/stdlib-2.0.md §Map): the checker",
                "    // promises `size()`/`keys()` on a host `Map.new()` receiver, and",
                "    // emit lowers both as method calls on this object. `size` is the",
                "    // entry count; `keys` yields the keys in ascending canonical Str",
                "    // order — Rust's `String: Ord` is byte-wise over UTF-8, which is",
                "    // exactly code-point order, so a plain sort is canonical. Both are",
                "    // read-only queries, no host trace.",
                "    pub fn size(&self) -> i64 {",
                "        self.inner.lock().unwrap().len() as i64",
                "    }",
                "    pub fn keys(&self) -> Vec<String> {",
                "        let mut ks: Vec<String> =",
                "            self.inner.lock().unwrap().keys().cloned().collect();",
                "        ks.sort();",
                "        ks",
                "    }",
                "}",
                "impl<V: Clone> Map<V> {",
                "    // The key is borrowed, not moved: a component that reads then",
                "    // writes the same key (the session ledger) keeps owning it.",
                "    pub fn get(&self, key: &String) -> Option<V> {",
                "        self.inner.lock().unwrap().get(key).cloned()",
                "    }",
                "}",
                "",
            ]
        )
    if "Pool" in used:
        out.extend(
            [
                "/// revl host object: a bounded connection pool over a deterministic",
                "/// in-memory database (no driver dependency).  Semantics are shared",
                "/// across tiers — backends/python/runtime.py, `.. _pool-job-semantics:`:",
                "/// `size` connections numbered 1..size, acquire/release accounting,",
                "/// statements borrow a connection for their duration, `close` releases",
                "/// everything, and exhaustion / use-after-close panic.",
                "pub struct Pool {",
                "    url: String,",
                "    size: i64,",
                "    state: std::sync::Mutex<PoolState>,",
                "}",
                "struct PoolState {",
                "    idle: Vec<i64>,",
                "    in_use: Vec<i64>,",
                "    closed: bool,",
                "}",
                "impl Pool {",
                "    pub fn open(url: String, size: i64) -> Self {",
                "        if size < 1 {",
                "            panic!(\"pool size must be an integer >= 1 (got {})\", size);",
                "        }",
                "        REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() + 1));",
                "        Self {",
                "            url,",
                "            size,",
                "            state: std::sync::Mutex::new(PoolState {",
                "                idle: (1..=size).collect(),",
                "                in_use: Vec::new(),",
                "                closed: false,",
                "            }),",
                "        }",
                "    }",
                "    pub fn url(&self) -> String {",
                "        self.url.clone()",
                "    }",
                "    pub fn capacity(&self) -> i64 {",
                "        if self.state.lock().unwrap().closed { 0i64 } else { self.size }",
                "    }",
                "    pub fn in_use(&self) -> i64 {",
                "        self.state.lock().unwrap().in_use.len() as i64",
                "    }",
                "    pub fn available(&self) -> i64 {",
                "        self.state.lock().unwrap().idle.len() as i64",
                "    }",
                "    // NB: never panic while the guard is held — a panic under the",
                "    // lock poisons the Mutex, and every later call would then fail",
                "    // with a PoisonError instead of the intended message (which is",
                "    // not what the other tiers do: there the pool stays usable).",
                "    fn borrow_conn(&self, op: &str) -> i64 {",
                "        let outcome = {",
                "            let mut state = self.state.lock().unwrap();",
                "            if state.closed {",
                "                Err(format!(\"pool.{} after close/drop — use-after-free\", op))",
                "            } else if state.idle.is_empty() {",
                "                Err(format!(",
                "                    \"pool.{} exhausted (size={}, in_use={})\",",
                "                    op,",
                "                    self.size,",
                "                    state.in_use.len()",
                "                ))",
                "            } else {",
                "                let conn = state.idle.remove(0);",
                "                state.in_use.push(conn);",
                "                Ok(conn)",
                "            }",
                "        };",
                "        match outcome {",
                "            Ok(conn) => conn,",
                "            Err(message) => panic!(\"{}\", message),",
                "        }",
                "    }",
                "    fn give_back(&self, conn: i64) {",
                "        let mut state = self.state.lock().unwrap();",
                "        if let Some(pos) = state.in_use.iter().position(|c| *c == conn) {",
                "            state.in_use.remove(pos);",
                "        }",
                "        state.idle.push(conn);",
                "        state.idle.sort_unstable();",
                "    }",
                "    pub fn acquire(&self) -> i64 {",
                "        self.borrow_conn(\"acquire\")",
                "    }",
                "    pub fn release(&self, conn: i64) {",
                "        let outcome = {",
                "            let state = self.state.lock().unwrap();",
                "            if state.closed {",
                "                Err(String::from(\"pool.release after close/drop — use-after-free\"))",
                "            } else if !state.in_use.contains(&conn) {",
                "                Err(format!(\"pool.release conn={} is not checked out\", conn))",
                "            } else {",
                "                Ok(())",
                "            }",
                "        };",
                "        if let Err(message) = outcome {",
                "            panic!(\"{}\", message);",
                "        }",
                "        self.give_back(conn);",
                "    }",
                "    pub fn close(&self) {",
                "        let already_closed = {",
                "            let mut state = self.state.lock().unwrap();",
                "            if state.closed {",
                "                true",
                "            } else {",
                "                state.in_use.clear();",
                "                state.idle.clear();",
                "                state.closed = true;",
                "                false",
                "            }",
                "        };",
                "        if already_closed {",
                "            panic!(\"pool.close after close/drop — use-after-free\");",
                "        }",
                "        REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() - 1));",
                "    }",
                "    pub fn query(&self, _sql: String) -> Vec<Value> {",
                "        let conn = self.borrow_conn(\"query\");",
                "        self.give_back(conn);",
                "        Vec::new()",
                "    }",
                "    pub fn execute(&self, _sql: String) -> i64 {",
                "        let conn = self.borrow_conn(\"execute\");",
                "        self.give_back(conn);",
                "        1i64",
                "    }",
                "}",
                "",
            ]
        )
    if "Job" in used:
        out.extend(
            [
                "/// revl host object: a cancellable asynchronous unit of work.",
                "/// Semantics are shared across tiers — backends/python/runtime.py,",
                "/// `.. _pool-job-semantics:`: a job completes after exactly",
                "/// `Job::TICKS` scheduler turns (no timer, so tests stay deterministic),",
                "/// carries a cancellation token, and panics if awaited after",
                "/// cancellation.  `Job::spawn` hands back the handle (the tier-neutral",
                "/// `Job.run` of the other backends); `Job::run` is the async shorthand",
                "/// the emitted `await Job.run(name)` call site uses.",
                "pub struct Job;",
                "/// 0 = pending, 1 = done, 2 = cancelled",
                "#[derive(Clone)]",
                "pub struct JobToken(std::sync::Arc<std::sync::atomic::AtomicU8>);",
                "impl JobToken {",
                "    /// pending -> cancelled (true); a no-op returning false otherwise.",
                "    pub fn cancel(&self) -> bool {",
                "        self.0",
                "            .compare_exchange(",
                "                0u8,",
                "                2u8,",
                "                std::sync::atomic::Ordering::SeqCst,",
                "                std::sync::atomic::Ordering::SeqCst,",
                "            )",
                "            .is_ok()",
                "    }",
                "    pub fn state(&self) -> &'static str {",
                "        match self.0.load(std::sync::atomic::Ordering::SeqCst) {",
                "            1u8 => \"done\",",
                "            2u8 => \"cancelled\",",
                "            _ => \"pending\",",
                "        }",
                "    }",
                "}",
                "pub struct JobHandle {",
                "    name: String,",
                "    remaining: u32,",
                "    state: std::sync::Arc<std::sync::atomic::AtomicU8>,",
                "}",
                "impl JobHandle {",
                "    pub fn name(&self) -> String {",
                "        self.name.clone()",
                "    }",
                "    pub fn token(&self) -> JobToken {",
                "        JobToken(self.state.clone())",
                "    }",
                "    pub fn state(&self) -> &'static str {",
                "        self.token().state()",
                "    }",
                "    pub fn cancel(&self) -> bool {",
                "        self.token().cancel()",
                "    }",
                "}",
                "impl std::future::Future for JobHandle {",
                "    type Output = String;",
                "    fn poll(",
                "        self: std::pin::Pin<&mut Self>,",
                "        cx: &mut std::task::Context<'_>,",
                "    ) -> std::task::Poll<String> {",
                "        let this = self.get_mut();",
                "        match this.state.load(std::sync::atomic::Ordering::SeqCst) {",
                "            1u8 => return std::task::Poll::Ready(this.name.clone()),",
                "            2u8 => panic!(\"job \\\"{}\\\" cancelled\", this.name),",
                "            _ => {}",
                "        }",
                "        if this.remaining == 0u32 {",
                "            this.state",
                "                .store(1u8, std::sync::atomic::Ordering::SeqCst);",
                "            return std::task::Poll::Ready(this.name.clone());",
                "        }",
                "        this.remaining -= 1u32;",
                "        cx.waker().wake_by_ref();",
                "        std::task::Poll::Pending",
                "    }",
                "}",
                "impl Job {",
                "    /// scheduler turns of simulated work (same number on every tier)",
                "    pub const TICKS: u32 = 5u32;",
                "    /// Schedule a cancellable job.  The handle is the future; take",
                "    /// `handle.token()` first if you need to cancel it from elsewhere.",
                "    pub fn spawn(name: String) -> JobHandle {",
                "        JobHandle {",
                "            name,",
                "            remaining: Self::TICKS,",
                "            state: std::sync::Arc::new(std::sync::atomic::AtomicU8::new(0u8)),",
                "        }",
                "    }",
                "    /// Run a job to completion — the `await Job.run(name)` form emitted",
                "    /// code uses.  On this path a divert cancels by dropping the future;",
                "    /// use `spawn` when the host needs an explicit cancellation token.",
                "    pub async fn run(name: String) -> String {",
                "        Self::spawn(name).await",
                "    }",
                "}",
                "",
            ]
        )
    if "Stream" in used:
        out.extend(_STREAM_HOST_RUST)
    return out


# item 130 Slice 3 — `Stream[T]` on cordis-rs (a BLOCKING tier).
#
# docs/design/130-stream-reactive-types.md §4.6, the rust row: this tier erases
# the async color; `next` blocks on a race between the item queue and the
# subscription's CANCEL signal, and `close` trips that signal. The design names
# `crossbeam`'s `select!`; the emitted crate carries only cordis and serde, so
# the same race is spelled with the std primitive that expresses it — a `Mutex`
# + `Condvar` park whose wake conditions are exactly item / terminal / cancel,
# with the cancel checked FIRST. The guarantee is identical and the dependency
# set is unchanged; the priority has to be explicit either way, since neither a
# `select!` nor a condvar wake orders ready cases on its own.
#
# The two properties this block is accountable for:
#   * §9 Part A — `close` trips the cancel flag and notifies, synchronously,
#     without waiting for the parked `next`. So the bracket inverse is reachable
#     off the teardown thread and teardown never deadlocks behind a park.
#   * §9 Part B — a provider `close`/`fault` delivers `Closed`/`Faulted` to every
#     live subscription; `merge` counts its upstreams so the LAST source's close
#     still terminates a consumer parked on the fan-in.
_STREAM_HOST_RUST = r'''
/// revl host object: the `Stream[T]` provider/consumer pair (item 130).
/// The blocking-tier lowering — `next` parks on a race between the item queue
/// and the cancel signal; `close` trips the cancel signal. Unloading the
/// subscription owner runs `close` (the bracket inverse), which resolves a
/// parked `next` as the `Closed` terminal: the core guarantee, delivered by the
/// same LIFO teardown any bracket rides.
#[derive(Debug, PartialEq)]
pub enum StreamNext {
    /// one item
    Item(String),
    /// the `Closed` terminal — an orderly provider close, or the owner's own
    /// `close` tripping the cancel signal
    Closed,
}

/// The bounded buffer every subscription gets: there are no unbounded buffers
/// (design §4.4). Overflow under the default `error` policy is a
/// `Faulted(overflow)` terminal — deterministic, never a silent drop.
pub const STREAM_BUFFER_CAPACITY: usize = 8;

static REVL_STREAM_IDS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

fn revl_stream_next_id() -> u64 {
    REVL_STREAM_IDS.fetch_add(1, std::sync::atomic::Ordering::SeqCst)
}

#[derive(Default)]
struct StreamState {
    subs: Vec<std::sync::Arc<SubscriptionInner>>,
    down: Vec<std::sync::Arc<StreamInner>>,
    up: Vec<std::sync::Arc<StreamInner>>,
    /// upstream sources not yet terminal (a merged stream only)
    pending: usize,
    /// 0 open, 1 closed, 2 faulted
    state: u8,
    reason: String,
    released: bool,
}

struct StreamInner {
    id: u64,
    kind: &'static str,
    st: std::sync::Mutex<StreamState>,
}

/// The PROVIDER side: a source (`Stream.source()`) or the derived stream behind
/// `subscribe merge(a, b)`. Both are the same object, so a merged stream is
/// itself a terminal-delivering provider another `merge` can take.
#[derive(Clone)]
pub struct Stream {
    inner: std::sync::Arc<StreamInner>,
}

#[derive(Default)]
struct StreamRegistry {
    streams: Vec<std::sync::Arc<StreamInner>>,
    subs: Vec<std::sync::Arc<SubscriptionInner>>,
    /// ordered trace of stream host operations, so a scenario can assert the
    /// exact acquire/inverse ORDER (the LIFO teardown the core guarantee is)
    marks: Vec<String>,
}

/// Process-wide registry of providers and subscriptions, so a scenario can
/// drive a provider from another thread and assert residue (the rust mirror of
/// the py reference's `Stream.sources()` / `Stream.pending()`).
static REVL_STREAM_REGISTRY: std::sync::OnceLock<std::sync::Mutex<StreamRegistry>> =
    std::sync::OnceLock::new();

fn revl_stream_registry<R>(f: impl FnOnce(&mut StreamRegistry) -> R) -> R {
    let cell = REVL_STREAM_REGISTRY
        .get_or_init(|| std::sync::Mutex::new(StreamRegistry::default()));
    let mut guard = cell.lock().unwrap_or_else(|e| e.into_inner());
    f(&mut guard)
}

fn revl_stream_record(mark: String) {
    revl_stream_registry(|r| r.marks.push(mark));
}

/// The ordered stream host trace (acquire/inverse marks), so a scenario can
/// assert LIFO teardown — the go tier's `HostMarks` under another name.
pub fn revl_stream_marks() -> Vec<String> {
    revl_stream_registry(|r| r.marks.clone())
}

impl Stream {
    fn make(kind: &'static str, up: Vec<std::sync::Arc<StreamInner>>) -> Self {
        let pending = up.len();
        let inner = std::sync::Arc::new(StreamInner {
            id: revl_stream_next_id(),
            kind,
            st: std::sync::Mutex::new(StreamState {
                up,
                pending,
                ..Default::default()
            }),
        });
        revl_stream_registry(|r| r.streams.push(inner.clone()));
        REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() + 1));
        revl_stream_record(format!("stream.{} open", kind));
        Stream { inner }
    }

    /// Open a provider.
    pub fn source() -> Self {
        Self::make("source", Vec::new())
    }

    /// The fan-in behind `subscribe merge(a, b)` — one derived stream from two
    /// (design §1). NOT a bracket of its own: the merged stream is DERIVED,
    /// owned by the subscription opened on it, so multi-source teardown rides
    /// the ONE bracket the `subscribe` registers. The subscription's `close`
    /// closes the merge; closing the merge detaches it from both upstreams;
    /// each source is left to its own bracket. One LIFO stack, and no source
    /// keeps feeding — or holding a reference to — a fan-in whose owner is gone.
    pub fn merge(a: &Stream, b: &Stream) -> Self {
        let m = Self::make("merge", vec![a.inner.clone(), b.inner.clone()]);
        a.inner.attach_down(&m.inner);
        b.inner.attach_down(&m.inner);
        m
    }

    /// Open the single-consumer subscription a `subscribe` bracket binds.
    /// `capacity` is the declared `buffer` (0 = the default); every buffer is
    /// BOUNDED either way, since there are no unbounded buffers (design §4.4).
    pub fn subscribe(src: &Stream, policy: &str, capacity: usize) -> Subscription {
        Subscription::open(src.inner.clone(), policy, capacity)
    }

    /// Deliver one item to the single consumer (and into any merged stream fed
    /// by this provider). A no-op once terminal.
    pub fn emit(&self, item: String) -> bool {
        if self.inner.st.lock().unwrap().state != 0u8 {
            return false;
        }
        revl_stream_record(format!("stream.emit {}", item));
        self.inner.forward(&item);
        true
    }

    /// The provider's terminal-delivering inverse (§9 Part B) and, for a merged
    /// stream, its detach from both upstreams. Idempotent; the live-resource
    /// slot is released exactly once, so a provider that FAULTED and is then
    /// unloaded still leaves no residue.
    pub fn close(&self) -> bool {
        self.inner.close()
    }

    /// A provider abort: every outstanding `next` resolves to `Faulted`, never a
    /// silent pending (design §4.3).
    pub fn fault(&self, reason: String) -> bool {
        self.inner.fault(&reason)
    }

    pub fn kind(&self) -> &'static str {
        self.inner.kind
    }
}

impl StreamInner {
    fn attach_down(&self, m: &std::sync::Arc<StreamInner>) {
        let terminal = {
            let mut st = self.st.lock().unwrap();
            if st.state == 0u8 {
                st.down.push(m.clone());
                None
            } else {
                Some((st.state, st.reason.clone()))
            }
        };
        if let Some((state, reason)) = terminal {
            m.upstream_terminal(state, &reason);
        }
    }

    fn detach_down(&self, id: u64) {
        let mut st = self.st.lock().unwrap();
        st.down.retain(|d| d.id != id);
    }

    fn detach_sub(&self, id: u64) {
        let mut st = self.st.lock().unwrap();
        st.subs.retain(|s| s.id != id);
    }

    fn forward(&self, item: &str) {
        let (subs, downs) = {
            let st = self.st.lock().unwrap();
            if st.state != 0u8 {
                return;
            }
            (st.subs.clone(), st.down.clone())
        };
        for sub in subs {
            sub.deliver(item);
        }
        for d in downs {
            d.forward(item);
        }
    }

    fn close(&self) -> bool {
        let (first, release, subs, downs, ups) = {
            let mut st = self.st.lock().unwrap();
            let first = st.state == 0u8;
            if first {
                st.state = 1u8;
            }
            let release = !st.released;
            st.released = true;
            let ups = std::mem::take(&mut st.up);
            (first, release, st.subs.clone(), st.down.clone(), ups)
        };
        if first {
            for sub in subs {
                sub.terminate(1u8, "");
            }
            for d in downs {
                d.upstream_terminal(1u8, "");
            }
        }
        // A merged stream leaves its upstreams on the way out. A DERIVED
        // upstream (a nested `merge`) is owned by this one, so it closes with
        // it; a plain source is left to its own bracket.
        for u in ups {
            u.detach_down(self.id);
            if u.kind != "source" {
                u.close();
            }
        }
        if release {
            REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() - 1));
            revl_stream_record(format!("stream.{} close", self.kind));
        }
        first
    }

    fn fault(&self, reason: &str) -> bool {
        let (subs, downs) = {
            let mut st = self.st.lock().unwrap();
            if st.state != 0u8 {
                return false;
            }
            st.state = 2u8;
            st.reason = reason.to_string();
            (st.subs.clone(), st.down.clone())
        };
        revl_stream_record(format!("stream.{} fault {}", self.kind, reason));
        for sub in subs {
            sub.terminate(2u8, reason);
        }
        for d in downs {
            d.upstream_terminal(2u8, reason);
        }
        true
    }

    /// How a merged stream learns one of its sources is done. A FAULT
    /// propagates at once — no silent loss. An orderly CLOSE only counts down:
    /// the fan-in stays live while any source is, so one source's death never
    /// strands a consumer the other can still feed, and when the LAST source
    /// closes the merged stream delivers its own `Closed` — a parked `next` is
    /// terminated, never left waiting on a dead fan-in.
    fn upstream_terminal(&self, state: u8, reason: &str) {
        let out = {
            let mut st = self.st.lock().unwrap();
            if st.state != 0u8 {
                None
            } else if state == 2u8 {
                st.state = 2u8;
                st.reason = reason.to_string();
                Some((2u8, st.subs.clone(), st.down.clone()))
            } else {
                st.pending = st.pending.saturating_sub(1);
                if st.pending > 0 {
                    None
                } else {
                    st.state = 1u8;
                    Some((1u8, st.subs.clone(), st.down.clone()))
                }
            }
        };
        if let Some((kind, subs, downs)) = out {
            for sub in subs {
                sub.terminate(kind, reason);
            }
            for d in downs {
                d.upstream_terminal(kind, reason);
            }
        }
    }
}

#[derive(Default)]
struct SubscriptionState {
    items: std::collections::VecDeque<String>,
    /// the cancel signal `close` trips — checked BEFORE the buffer
    cancelled: bool,
    /// 0 none, 1 closed, 2 faulted
    terminal: u8,
    reason: String,
}

struct SubscriptionInner {
    id: u64,
    src: std::sync::Arc<StreamInner>,
    policy: String,
    capacity: usize,
    st: std::sync::Mutex<SubscriptionState>,
    wake: std::sync::Condvar,
}

/// The CONSUMER side: a single-consumer acquisition whose inverse is `close`.
/// `next` parks on the item/terminal/cancel race; `close` trips the cancel
/// signal and wakes the park — synchronously, never waiting for it.
#[derive(Clone)]
pub struct Subscription {
    inner: std::sync::Arc<SubscriptionInner>,
}

impl Subscription {
    fn open(src: std::sync::Arc<StreamInner>, policy: &str, capacity: usize) -> Self {
        let inner = std::sync::Arc::new(SubscriptionInner {
            id: revl_stream_next_id(),
            src: src.clone(),
            policy: policy.to_string(),
            capacity: if capacity == 0 { STREAM_BUFFER_CAPACITY } else { capacity },
            st: std::sync::Mutex::new(SubscriptionState::default()),
            wake: std::sync::Condvar::new(),
        });
        let terminal = {
            let mut st = src.st.lock().unwrap();
            st.subs.push(inner.clone());
            if st.state == 0u8 {
                None
            } else {
                Some((st.state, st.reason.clone()))
            }
        };
        revl_stream_registry(|r| r.subs.push(inner.clone()));
        REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() + 1));
        revl_stream_record(String::from("stream.subscribe"));
        // Subscribing to an already-terminal provider terminates at once, so the
        // first `next` cannot park on a provider that is already gone.
        if let Some((state, reason)) = terminal {
            inner.terminate(state, &reason);
        }
        Subscription { inner }
    }

    /// Park until an item, a provider terminal, or the cancel signal.
    /// `Err(reason)` is the `Faulted` terminal; the emitted call site turns it
    /// into an activation failure, so the accumulated prefix — the subscription
    /// bracket included — reverts LIFO.
    pub fn next(&self) -> Result<StreamNext, String> {
        self.inner.next()
    }

    /// The bracket inverse: trip the cancel signal, wake the park, detach the
    /// listener, release the slot. Infallible, idempotent, and it NEVER waits
    /// for a parked `next` to drain.
    ///
    /// A DERIVED upstream (a `merge(a, b)` fan-in) is owned by this subscription
    /// rather than by a bracket of its own, so closing here closes it too — and
    /// closing a merge is what detaches it from both sources, which stay on
    /// their own brackets. The LIFO close-order proof is unchanged.
    pub fn close(&self) -> bool {
        let closed = self.inner.close();
        if closed && self.inner.src.kind != "source" {
            self.inner.src.close();
        }
        closed
    }
}

impl SubscriptionInner {
    fn deliver(&self, item: &str) {
        let mut st = self.st.lock().unwrap();
        if st.cancelled || st.terminal != 0u8 {
            return;
        }
        if st.items.len() >= self.capacity {
            // backpressure `error` (the default, §4.4): a full bounded buffer is
            // a terminal Faulted(overflow) — deterministic, no silent loss.
            if self.policy.is_empty() || self.policy == "error" {
                st.terminal = 2u8;
                st.reason = String::from("overflow");
                self.wake.notify_all();
                return;
            }
            panic!(
                "revl: backpressure policy {} is not lowered on the cordis-rs tier",
                self.policy
            );
        }
        st.items.push_back(item.to_string());
        self.wake.notify_all();
    }

    fn terminate(&self, kind: u8, reason: &str) {
        let mut st = self.st.lock().unwrap();
        if st.cancelled || st.terminal != 0u8 {
            return;
        }
        st.terminal = kind;
        st.reason = reason.to_string();
        self.wake.notify_all();
    }

    fn next(&self) -> Result<StreamNext, String> {
        let mut st = self.st.lock().unwrap();
        loop {
            // CANCELLATION-FIRST (§9 Part A): the cancel signal is checked BEFORE
            // the buffer, so a `close` racing a buffered item still wins and a
            // withdrawn owner never observes one more item after teardown began.
            if st.cancelled {
                return Ok(StreamNext::Closed);
            }
            if let Some(item) = st.items.pop_front() {
                return Ok(StreamNext::Item(item));
            }
            if st.terminal == 2u8 {
                let reason = if st.reason.is_empty() {
                    String::from("faulted")
                } else {
                    st.reason.clone()
                };
                return Err(format!("stream faulted: {}", reason));
            }
            if st.terminal == 1u8 {
                return Ok(StreamNext::Closed);
            }
            // The park. `close` (run by the TEARDOWN thread) and a provider
            // terminal both wake it; nothing else can hold it.
            st = self.wake.wait(st).unwrap();
        }
    }

    fn close(&self) -> bool {
        {
            let mut st = self.st.lock().unwrap();
            if st.cancelled {
                return false;
            }
            st.cancelled = true;
            self.wake.notify_all();
        }
        self.src.detach_sub(self.id);
        REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() - 1));
        revl_stream_record(String::from("stream.close"));
        true
    }
}

/// Residue probe: unreleased providers plus live (un-closed) subscriptions.
/// Zero after a clean unload proves every bracket inverse ran and no host
/// listener outlived its owner.
pub fn revl_stream_pending() -> usize {
    revl_stream_registry(|r| {
        r.streams
            .iter()
            .filter(|s| !s.st.lock().unwrap().released)
            .count()
            + r.subs
                .iter()
                .filter(|s| !s.st.lock().unwrap().cancelled)
                .count()
    })
}

/// Live (un-closed) subscriptions.
pub fn revl_stream_live_subscriptions() -> usize {
    revl_stream_registry(|r| {
        r.subs
            .iter()
            .filter(|s| !s.st.lock().unwrap().cancelled)
            .count()
    })
}

/// The providers this process opened, in opening order, so a scenario can drive
/// one from ANOTHER thread — the rust mirror of the py reference's
/// `Stream.sources()`.
pub fn revl_stream_providers() -> Vec<Stream> {
    revl_stream_registry(|r| {
        r.streams
            .iter()
            .map(|inner| Stream {
                inner: inner.clone(),
            })
            .collect()
    })
}

/// Clear the provider/subscription registry (call between scenarios).
pub fn revl_stream_reset() {
    revl_stream_registry(|r| {
        r.streams.clear();
        r.subs.clear();
        r.marks.clear();
    });
}
'''.splitlines()


def _binds(component: dict) -> list[str]:
    return [s["bind"] for s in component.get("body") or [] if s.get("step") == "let-effect"]


def _refuse_unlowered_stream_surface(node, tier: str) -> None:
    """Refuse the item-130 Slice 2 surface this blocking tier does not lower.

    Slice 2 shipped `map`/`filter`/`take` and the three non-default backpressure
    policies on the py reference tier only; Slice 3 lowered subscribe/next/close
    and the `merge` fan-in here. Emitting a subscription that SILENTLY dropped a
    combinator chain, a lossy policy or a drain window would be the worst
    outcome available: the program would run and quietly disagree with the
    reference tier. Refuse by name instead."""
    if node.get("stages"):
        raise EmitError(
            "a stream combinator chain (`map`/`filter`/`take`) is not lowered "
            f"on the {tier} tier; the derived-stream chain runs on the py "
            "reference tier (item 130 Slice 2) while this tier lowers "
            "subscribe / next / close and `merge` (Slice 3) — try `--backend py`")
    policy = node.get("policy") or "error"
    if policy != "error":
        raise EmitError(
            f"backpressure policy `{policy}` is not lowered on the {tier} tier; "
            "this tier lowers the default `error` policy (a full bounded buffer "
            "faults with `Faulted(overflow)` and closes, no silent loss). "
            "`drop_newest`/`drop_oldest`/`block` run on the py reference tier "
            "(item 130 §4.4) — try `--backend py`")
    if node.get("drain") is not None:
        raise EmitError(
            "a `drain` window is the `block`-policy drain interval and is not "
            f"lowered on the {tier} tier; it fires on the deterministic test "
            "clock, which lives on the py reference tier (item 130 §8) — try "
            "`--backend py`")


def _stream_head(node, ctx, rename) -> str:
    """The stream a `subscribe` acquires: a plain source, or a `merge(a, b)`
    fan-in (item 130 Slice 3). Recursive — a merged stream is itself a stream.

    The fan-in BORROWS its sources: each is a live `Arc<Stream>` activation bind
    the component still needs afterwards (its own `undo`, the provide struct), so
    moving one here would be a use-after-move. `&Arc<Stream>` deref-coerces to
    `&Stream`. Every link is a DERIVED stream owned by the subscription, so
    `close` unwinds the whole chain off the ONE bracket the subscribe registers
    and each plain source is left to its own."""
    if isinstance(node, dict) and node.get("kind") == "stream-merge":
        args = ", ".join("&" + _stream_head(src, ctx, rename)
                         for src in node.get("sources") or [])
        return f"Stream::merge({args})"
    return _render_expr(node, ctx, rename)


def _subscription_binds(component: dict) -> set:
    """Activation binds holding a `subscribe` bracket's subscription (item 130)."""
    return {s.get("bind") for s in component.get("body") or []
            if s.get("step") == "let-effect"
            and (s.get("acquire") or {}).get("kind") == "subscribe"}


def _is_stream_next(expr, env) -> bool:
    """True for `<sub>.next()` where `<sub>` names a subscription bind."""
    if not isinstance(expr, dict) or expr.get("kind") != "call":
        return False
    if expr.get("method") != "next":
        return False
    target = expr.get("target") or {}
    name = target.get("id") or target.get("name")
    return name in _subscription_binds(getattr(env, "component", {}) or {})


def _has_config(component: dict) -> bool:
    return bool(component.get("config"))


# The plugin closure's local `config` may be *partially moved* by an effect
# closure that captures a config field (`move || { .. config.tag .. }`), so a
# later `config.clone()` at a provide-construction site would fail (E0382). The
# provide struct is therefore built from a full clone taken up front, before any
# effect runs — this is the name of that binding.
_PROVIDE_CONFIG_LOCAL = "__revl_provide_config"


def _references_config(node: object) -> bool:
    """Does this IR subtree read component config (a `config` expression node)?"""
    if isinstance(node, dict):
        if node.get("kind") == "config":
            return True
        return any(_references_config(v) for v in node.values())
    if isinstance(node, list):
        return any(_references_config(v) for v in node)
    return False


def _provision_methods(component: dict, key: str) -> list:
    provide = next(
        (s for s in component.get("body") or []
         if s.get("step") == "provide" and s.get("name") == key),
        None,
    )
    return (provide or {}).get("methods") or []


def _provision_uses_config(component: dict, key: str) -> bool:
    """Does provision `key`'s method surface read config? Only then does its
    provide struct need to capture config — capturing it unconditionally would
    both add dead fields and force a `config.clone()` that races effect closures
    which partially move config (the Worker scenario)."""
    return _has_config(component) and any(
        _references_config(m) for m in _provision_methods(component, key)
    )


def _component_provide_uses_config(component: dict) -> bool:
    return _has_config(component) and any(
        _provision_uses_config(component, key)
        for key in (component.get("provides") or {})
    )


def _config_struct_field(component: dict, key: str) -> list[str]:
    """The provide-struct field capturing component config, so a provide-method
    body can read `self.config.<field>` — `config` is otherwise not in method
    scope (E0425). Empty unless this provision's methods read config."""
    if not _provision_uses_config(component, key):
        return []
    cname = _ident(component.get("name"), "component")
    return [f"    config: {cname}Config,"]


def _config_ctor_field(component: dict, key: str) -> list[str]:
    """The struct-construction fragment for the captured config field, sourced
    from the up-front clone (`_PROVIDE_CONFIG_LOCAL`) so it survives any effect
    closure that partially moved the plugin's local `config`."""
    if not _provision_uses_config(component, key):
        return []
    return [f"config: {_PROVIDE_CONFIG_LOCAL}.clone()"]


def _emit_provide_config_local(component: dict, indent: int) -> list[str]:
    """The up-front `let __revl_provide_config = config.clone();`, emitted right
    after config application (before any effect) when a provision captures config."""
    if not _component_provide_uses_config(component):
        return []
    return [f"{'    ' * indent}let {_PROVIDE_CONFIG_LOCAL} = config.clone();"]


def _host_of(component: dict, bind: str, map_values: dict[str, str] | None = None) -> str:
    for s in component.get("body") or []:
        if s.get("step") == "let-effect" and s.get("bind") == bind:
            acquire = s.get("acquire") or {}
            # A `spawn` acquisition binds a live-instance handle, not a host
            # resource: the binding's type is the emitted `RevlSpawnHandle`,
            # so a provide-method that captures it can call `.dispose()`.
            if acquire.get("kind") == "spawn":
                return "RevlSpawnHandle"
            # item 397: a result-declared host CAS binds the atomic `bool`
            # result (`let fresh = Arc::new(ledger.insert_if_absent(...))`), not
            # a host resource, so a provide struct that captures it holds an
            # `Arc<bool>`, never the opaque `Arc<Value>` fallback (E0308).
            if _is_map_cas(acquire):
                return "bool"
            host = (acquire.get("fn") or "").split(".")[0] or "Value"
            # FR-4: the host Map is generic over its value type, learned from
            # the IR's `insert` sites (defaults to the historical `String`).
            if host == "Map" and map_values is not None:
                return f"Map<{map_values.get(bind, 'String')}>"
            return host
    return "Value"


# ---------------------------------------------------------------------------
# FR-4: the host Map's value type, learned from the IR (docs/v2.0-roadmap.md
# item 77(c)).  The frontend types a Map by its value parameter — every
# `store.insert(k, v)` names the map's value type — so the emitter carries it
# into the generic `Map<V>` it emits: provider-struct fields become
# `Arc<Map<Vec<Msg>>>` and the constructor becomes `Map::<Vec<Msg>>::new()`.
# This is a *tiny* oracle for the shapes an `insert` value takes in practice
# (a parameter, a literal, a list, a stdlib result like `push`, a free-fn or
# required-service call, a record literal, an ADT case).  Anything it cannot
# prove stays `None` and the emitter falls back to `String` — the historical
# surface — so String-valued maps keep emitting byte-identically.
# ---------------------------------------------------------------------------


def _map_value_expr_type(node: dict, var_types: dict, env: _Env) -> str | None:
    """Best-effort *surface* type of an expression for Map value inference."""
    if not isinstance(node, dict):
        return None
    kind = node.get("kind")
    if kind in ("name", "var"):
        return var_types.get(node.get("id") or node.get("name"))
    if kind == "lit":
        value = node.get("value")
        if isinstance(value, bool):
            return "Bool"
        if isinstance(value, int):
            return "Int"
        if isinstance(value, float):
            return "Float"
        if isinstance(value, str):
            return "Str"
        return None
    if kind in ("format", "interp"):
        return "Str"
    if kind == "list":
        items = node.get("items") or []
        if not items:
            return None  # an untyped `[]` pins nothing
        first = _map_value_expr_type(items[0], var_types, env)
        if first is None:
            return None
        return f"List[{first}]"
    if kind == "builtin":
        method = node.get("method")
        args = node.get("args") or []
        # `push` returns the receiver list extended by its argument, so the
        # result's element type is the argument's type even when the receiver
        # (e.g. a `store.get(k) ?? []` binding) has no known surface type.
        if method == "push" and args:
            elem = _map_value_expr_type(args[0], var_types, env)
            if elem is not None:
                return f"List[{elem}]"
        if method in ("length", "charCodeAt", "codepoint_at", "indexOf",
                      "to_int"):
            return "Int"
        if method in ("charAt", "join", "repeat", "to_str", "slice"):
            return "Str"
        if method == "split":
            return "List[Str]"
        if method == "concat":
            return _map_value_expr_type(node.get("target"), var_types, env)
        return None
    if kind == "fn":
        # free-function call: the declared return type
        name = node.get("name")
        for fn in env.functions:
            if fn.get("name") == name:
                return fn.get("returns")
        return None
    if kind == "call":
        target = node.get("target")
        if isinstance(target, dict) and target.get("kind") == "req":
            service_name = (env.component.get("requires") or {}).get(target.get("name"))
            if service_name is not None:
                service = env.services.get(service_name)
                if service is not None:
                    decl = ((service.get("methods") or {})
                            .get(node.get("method") or "") or {})
                    return decl.get("returns")
        return None
    if kind == "record":
        try:
            return env.v3_ctx().record_type_for_fields(
                [k for k, _ in node.get("fields") or []])
        except EmitError:
            return None
    if kind == "adt":
        # lowered ADT construction carries its type on the node
        return node.get("type")
    if kind == "bin" and node.get("op") == "??":
        # `a ?? b` with an unknown left (a host `get`) is circular — the map
        # value type is exactly what we are learning; use the right side only
        # when it types concretely (a literal fallback).
        right = _map_value_expr_type(node.get("right"), var_types, env)
        if right is not None and "Never" not in right:
            return right
        return None
    return None


# Map verbs that write a value at arg[1]; each pins the host Map's value type V.
# Inferring V from ANY writer (not the literal name "insert") lets a CAS-only
# writer (`insert_if_absent`, item 397) pin a concrete V instead of the String
# default (item 402).
_MAP_VALUE_WRITERS = ("insert", "insert_if_absent")

# item 397: the compare-and-set host verb whose bound result is a `bool` and
# whose site-spelled undo is registered only when the CAS actually inserted.
_MAP_CAS_VERBS = ("insert_if_absent",)


def _is_map_cas(acquire) -> bool:
    """Whether a lowered acquisition node is a result-guarded map CAS."""
    return (isinstance(acquire, dict) and acquire.get("kind") == "call"
            and acquire.get("method") in _MAP_CAS_VERBS)


def _map_expr_inserts(node, bind: str, var_types: dict, env: _Env,
                      candidates: list[str]) -> None:
    """Collect candidate value types from any map value-writing call
    (`insert`, `insert_if_absent`, ...) on `bind` anywhere in an expression;
    recurses into sub-expressions."""
    if not isinstance(node, dict):
        return
    if node.get("kind") == "call":
        target = node.get("target")
        if (isinstance(target, dict) and target.get("kind") == "name"
                and target.get("id") == bind
                and node.get("method") in _MAP_VALUE_WRITERS):
            args = node.get("args") or []
            if len(args) >= 2:
                t = _map_value_expr_type(args[1], var_types, env)
                if t is not None and "Never" not in t:
                    candidates.append(t)
        for arg in node.get("args") or []:
            _map_expr_inserts(arg, bind, var_types, env, candidates)
        _map_expr_inserts(target, bind, var_types, env, candidates)
        return
    for value in node.values():
        if isinstance(value, list):
            for item in value:
                _map_expr_inserts(item, bind, var_types, env, candidates)
        elif isinstance(value, dict):
            _map_expr_inserts(value, bind, var_types, env, candidates)


def _map_insert_candidates(step: dict, bind: str, var_types: dict, env: _Env,
                           candidates: list[str]) -> None:
    """Walk one component-body step for `insert` calls on `bind`."""
    kind = step.get("step")
    if kind in ("effect", "let-effect"):
        _map_expr_inserts(step.get("acquire"), bind, var_types, env, candidates)
        _map_expr_inserts(step.get("undo"), bind, var_types, env, candidates)
        for nested in step.get("setup") or []:
            _map_insert_candidates(nested, bind, var_types, env, candidates)
    elif kind in ("let", "assign"):
        _map_expr_inserts(step.get("value"), bind, var_types, env, candidates)
    elif kind == "return":
        _map_expr_inserts(step.get("expr"), bind, var_types, env, candidates)
    elif kind == "if":
        for nested in step.get("then") or []:
            _map_insert_candidates(nested, bind, var_types, env, candidates)
        for nested in step.get("else") or []:
            _map_insert_candidates(nested, bind, var_types, env, candidates)


def _map_value_rust_type(env: _Env, bind: str) -> str | None:
    """The Rust value type of a host Map binding, learned from its `insert`
    call sites across the whole component (activation body + every provide
    method). `None` when no site pins a concrete type — the emitter then
    falls back to `String` (the historical surface)."""
    candidates: list[str] = []
    for step in env.component.get("body") or []:
        if step.get("step") == "provide":
            service = env.services.get(step.get("service") or "")
            if service is None:
                continue
            for method in step.get("methods") or []:
                var_types = {
                    p.get("name"): p.get("type")
                    for p in ((service.get("methods") or {})
                              .get(method.get("name") or "", {})).get("params") or []
                }
                for body_step in method.get("body") or []:
                    _map_insert_candidates(body_step, bind, var_types, env, candidates)
        else:
            _map_insert_candidates(step, bind, {}, env, candidates)
    if not candidates:
        return None
    distinct: list[str] = []
    for t in candidates:
        if t not in distinct:
            distinct.append(t)
    # A genuinely mixed map cannot be one revl `Map[Str, V]`; the first
    # concrete candidate (document order) is deterministic, and a real
    # conflict surfaces loudly in rustc at the mismatched `insert`.
    return distinct[0]


def _component_map_values(env: _Env) -> dict[str, str]:
    """bind -> Rust value type for every host Map binding in the component."""
    out: dict[str, str] = {}
    for s in env.component.get("body") or []:
        if s.get("step") != "let-effect":
            continue
        acquire = s.get("acquire") or {}
        if acquire.get("kind") != "host" or not (acquire.get("fn") or "").startswith("Map."):
            continue
        surface = _map_value_rust_type(env, s["bind"])
        out[s["bind"]] = _rust_type(surface, env.types) if surface else "String"
    return out


def _param_type(env: _Env, key: str, mname: str, p: str) -> str:
    service = env.provides[key]
    methods = env.services[service]["methods"]
    for mp in methods.get(mname, {}).get("params", []):
        if mp["name"] == p:
            return mp["type"]
    return "Value"


def _method_return(env: _Env, key: str, mname: str):
    service = env.provides[key]
    return env.services[service]["methods"].get(mname, {}).get("returns")


def _pure_method_statements(env: _Env, method: dict, rename: dict) -> str:
    """A pure method body as Rust statements.

    The result is inlined into `fn name(..) -> T { <here> }`, so a plain
    binding is a `let`, and the trailing `return` step keeps its keyword.
    """
    steps = method.get("body") or []
    # The expression-body fast path only applies to `return <expr>`. A bare
    # `return` (void service op, `{"step": "return", "expr": null}`) has no
    # expression to inline and falls through to the statement path below.
    if (
        len(steps) == 1
        and steps[0].get("step") == "return"
        and steps[0].get("expr") is not None
    ):
        return _expr(steps[0]["expr"], env, rename)

    parts: list[str] = []
    scope = dict(rename)
    for step in steps:
        kind = step.get("step")
        if kind == "let":
            name = _ident(step.get("name"), "binding")
            scope.pop(name, None)  # a local shadows an outer rename
            parts.append(f"let {'mut ' if step.get('mutable') else ''}"
                         f"{name} = {_expr(step['value'], env, scope)};")
        elif kind == "assign":
            name = _ident(step.get("name"), "binding")
            parts.append(f"{name} = {_expr(step['value'], env, scope)};")
        elif kind == "return":
            expr = step.get("expr")
            parts.append("return;" if expr is None
                         else f"return {_expr(expr, env, scope)};")
        else:
            raise EmitError(
                f"{env.name}.{method.get('name')}: a pure method body admits "
                f"bindings and a return in the Rust backend, not {kind!r}"
            )
    return " ".join(parts)


def _method_body(env: _Env, method: dict) -> str:
    rename = {b: f"self.{b}" for b in _binds(env.component)}
    rename.update({local: f"self.{local}" for local in env.reqs})
    if _has_config(env.component):
        rename["config"] = "self.config"
    return _pure_method_statements(env, method, rename)


def _method_body_pure_new(env: _Env, method: dict) -> str:
    return _pure_method_statements(env, method, _method_scope_rename(env))


def _component_has_effectful_methods(component: dict) -> bool:
    for step in component.get("body") or []:
        if step.get("step") != "provide":
            continue
        for method in step.get("methods") or []:
            for body_step in method.get("body") or []:
                # `let-effect` (item 397: a method-body host CAS) is effectful
                # too — it registers a guarded inverse on the activation frame.
                if body_step.get("step") in ("effect", "emit", "let-effect"):
                    return True
    return False


def _component_uses_await(component: dict) -> bool:
    return any(
        step.get("step") == "await"
        for step in component.get("body") or []
    )


def _emit_config_struct(component: dict, out: list[str]) -> str:
    """Emit `<Comp>Config` plus its `Default` implementation.

    Returns the Rust type to pass to `plugin_sync`/`plugin_async` (or `()` when
    the component has no config). The `Default` impl carries IR default values;
    required fields fall back to the zero value for their Rust type.
    """
    fields = component.get("config") or []
    if not fields:
        return "()"
    cname = _ident(component.get("name"), "component")
    out.append("#[derive(Clone)]")
    out.append(f"struct {cname}Config {{")
    for field in fields:
        fname = _ident(field.get("name"), "config field")
        out.append(f"    {fname}: {_rust_type(field.get('type'))},")
    out.append("}")
    out.append("")
    out.append(f"impl Default for {cname}Config {{")
    out.append("    fn default() -> Self {")
    out.append("        Self {")
    for field in fields:
        fname = _ident(field.get("name"), "config field")
        ftype = _rust_type(field.get("type"))
        out.append(
            f"            {fname}: {_config_default_lit(field.get('default'), ftype)},"
        )
    out.append("        }")
    out.append("    }")
    out.append("}")
    out.append("")
    return f"{cname}Config"


def _emit_config_application(component: dict, config_ty: str, indent: int) -> list[str]:
    """Reconstruct the plugin config locally using `..Default::default()`.

    `plugin_sync`/`plugin_async` hand the component a fully-populated
    `Arc<CompConfig>`. Rust struct fields are not optional, so the runtime
    cannot observe "missing" fields; emitting the local construction keeps the
    generated component the single place where `<Comp>Config` is built and
    makes `Default` part of that construction.
    """
    if config_ty == "()":
        return []
    fields = component.get("config") or []
    pad = "    " * indent
    lines = [f"{pad}let config = {config_ty} {{"]
    for field in fields:
        fname = _ident(field.get("name"), "config field")
        lines.append(f"{pad}    {fname}: config.{fname}.clone(),")
    lines.append(f"{pad}    ..Default::default()")
    lines.append(f"{pad}}};")
    return lines


def _method_has_effectful_steps(method: dict) -> bool:
    return any(
        body_step.get("step") in ("effect", "emit", "let-effect")
        for body_step in method.get("body") or []
    )


def _method_scope_rename(env: _Env) -> dict[str, str]:
    rename = {b: f"self.{b}" for b in _binds(env.component)}
    for req in env.reqs:
        rename[req] = f"self.{req}"
    if _has_config(env.component):
        rename["config"] = "self.config"
    return rename


def _method_undo_rename(env: _Env, method: dict) -> dict[str, str]:
    rename = {b: f"{b}_undo" for b in _binds(env.component)}
    for req in env.reqs:
        rename[req] = f"{req}_undo"
    for param in method.get("params") or []:
        rename[param] = f"{param}_undo"
    return rename


def _expr_var_names(node: object, acc: set[str]) -> None:
    """Collect every bare variable name referenced in an IR expression subtree.

    Used to find which method-body locals an `undo`/`compensate` closure reads,
    so exactly those are pre-cloned for the `move` closure (item 114)."""
    if isinstance(node, dict):
        if node.get("kind") in ("var", "name", "req"):
            ident = node.get("id") or node.get("name")
            if ident is not None:
                acc.add(ident)
        for value in node.values():
            _expr_var_names(value, acc)
    elif isinstance(node, list):
        for item in node:
            _expr_var_names(item, acc)


def _acquire_moved_locals(node: object, ctx: "_V3Ctx", acc: set[str]) -> None:
    """Method-body locals the acquire consumes *by value without a clone*.

    Only one acquire construct moves a bare local uncloned: a host-Map method
    that takes its argument by value — `insert(key, value)` (its `get`/`remove`
    borrow the key, and service-call / record / free-fn arguments are already
    cloned by `_by_value_arg`). Those bare-identifier arguments are the ones an
    `undo` that re-reads them must clone ahead of (item 114)."""
    if isinstance(node, dict):
        if node.get("kind") == "call" and "callee" not in node:
            target = node.get("target") or {}
            recv = target.get("id") or target.get("name") or ""
            recv_ty = str(ctx.var_types.get(recv) or "")
            if recv_ty.startswith("Map[") and node.get("method") not in ("get", "remove"):
                for arg in node.get("args") or []:
                    if isinstance(arg, dict) and arg.get("kind") in ("var", "name"):
                        ident = arg.get("id") or arg.get("name")
                        if ident is not None:
                            acc.add(ident)
        for value in node.values():
            _acquire_moved_locals(value, ctx, acc)
    elif isinstance(node, list):
        for item in node:
            _acquire_moved_locals(item, ctx, acc)


def _undo_reclone_locals(acquire_node: object, undo_node: object,
                         body_locals: set[str], ctx: "_V3Ctx") -> set[str]:
    """Method-body locals an `undo`/`compensate` closure reads *and* the paired
    acquire consumed by value — exactly the locals the move closure would find
    already moved (E0382). Empty when there is no undo."""
    if undo_node is None:
        return set()
    undo_refs: set[str] = set()
    _expr_var_names(undo_node, undo_refs)
    moved: set[str] = set()
    _acquire_moved_locals(acquire_node, ctx, moved)
    return undo_refs & moved & body_locals


def _method_undo_clones(env: _Env, method: dict, out: list[str], indent: int) -> None:
    pad = "    " * indent
    for bind in _binds(env.component):
        out.append(f"{pad}let {bind}_undo = self.{bind}.clone();")
    for req in env.reqs:
        out.append(f"{pad}let {req}_undo = self.{req}.clone();")
    for param in method.get("params") or []:
        out.append(f"{pad}let {param}_undo = {param}.clone();")


def _provide_let_type(env: _Env, value: object, ctx: "_V3Ctx") -> str | None:
    """Surface type of a `let` right-hand side inside a provide-method body.

    Extends `_v3_infer_type` (the item-101 mechanism — var alias / ADT /
    free-fn return / list / Str-Float-certain) with the one source that only
    exists in a provide body and never in a pure 2.0 fn: a *required-service
    method call* (`let answer = model.complete(seed)`). Its declared return type
    is read from the service signature, so a later by-value use of the local —
    an emit/effect through a record or a service-call argument — clones instead
    of moving it (E0382, roadmap item 114). Conservative like `_v3_infer_type`:
    it names a type only when the call resolves to a declared method return."""
    inferred = _v3_infer_type(value, ctx)
    if inferred is not None:
        return inferred
    if isinstance(value, dict) and value.get("kind") == "call" and "callee" not in value:
        target = value.get("target") or {}
        method = value.get("method")
        if target.get("kind") == "req":
            service = env.reqs.get(target.get("name"))
            if service and service in env.services:
                methods = env.services[service].get("methods") or {}
                return (methods.get(method) or {}).get("returns")
    return None


def _method_body_lines(env: _Env, method: dict, out: list[str], indent: int) -> None:
    """Lower effect/emit/return steps in a provide-method body.

    Effectful methods get an `Arc<Context>` on the impl struct, so the method
    keeps the plain service-trait signature and effect registration is fire and
    forget (`let _ = self.ctx.effect(...)`), matching the TS backend's
    `ctx.effect(() => ...)` which also cannot propagate a Rust `Result` through
    a non-`Result` method signature.
    """
    pad = "    " * indent
    rename = _method_scope_rename(env)
    # Method-body locals bound so far (`let key = ...`). A `move` undo closure
    # that reads one must capture a *clone* of it, because the acquire it pairs
    # with may have already consumed the original by value (a host-Map
    # `insert(key, ..)` moves its key without a call-site clone) — item 114.
    body_locals: set[str] = set()
    for index, step in enumerate(method.get("body") or []):
        kind = step.get("step")
        if kind == "return":
            if step.get("expr") is None:
                out.append(f"{pad}return;")
            else:
                out.append(f"{pad}return {_expr(step['expr'], env, rename)};")
        elif kind == "effect":
            wit = _witnessed_extern_for(env, step.get("acquire"))
            if wit is not None:
                # item 324: a witnessed effect in a provide-method body — the
                # per-tool-call H1 gate. Register the extern's DECLARED inverse
                # as a `transactional` entry on the enclosing component's
                # activation frame (via `revl_teardown_of(&self.ctx)`), NOT as a
                # plain always-replaying bracket. See `_emit_method_witnessed_step`
                # for the disposal-ordering analysis (rust's eager commit means
                # no park-for-drain is needed, unlike py).
                _emit_method_witnessed_step(env, method, step, wit, out, indent)
                continue
            # bracket (acquire): unchanged by item 243/247 — replays on every
            # teardown, clean unload and abort alike (docs/design/
            # teardown-contract.md's "bracket... unchanged" row).
            undo_rename = _method_undo_rename(env, method)
            acquire_rename = dict(rename)
            for param in method.get("params") or []:
                acquire_rename[param] = f"{param}.clone()"
            label = _string(f"{env.name}.{method.get('name')}.{kind}.{index}")
            undo_node = step.get("undo")
            acquire_node = step.get("acquire") or step.get("expr")
            _method_undo_clones(env, method, out, indent)
            # Pre-clone into `<l>_undo`, before the acquire runs, each
            # method-body local the undo reads AND the acquire consumes by
            # value (a host-Map `insert(key, ..)` moves its key without a
            # call-site clone), so the move closure owns a copy the acquire's
            # move cannot invalidate (item 114). Locals the acquire only
            # borrows (`prev.push(..)`) or clones stay bare.
            for local in sorted(_undo_reclone_locals(
                    acquire_node, undo_node, body_locals, env.v3_ctx())):
                out.append(f"{pad}let {local}_undo = {local}.clone();")
                undo_rename[local] = f"{local}_undo"
            acquire = _expr(acquire_node, env, acquire_rename)
            out.append(f"{pad}let _ = {acquire};")
            undo = _expr(undo_node, env, undo_rename)
            out.append(
                f"{pad}let _ = self.ctx.effect({label}, move || {{ {undo}; Ok(()) }});"
            )
        elif kind == "emit":
            acquire_rename = dict(rename)
            for param in method.get("params") or []:
                acquire_rename[param] = f"{param}.clone()"
            acquire_node = step.get("expr")
            acquire = _expr(acquire_node, env, acquire_rename)
            out.append(f"{pad}let _ = {acquire};")
            if step.get("compensate") is None:
                continue
            # `compensation` (item 247 / the teardown-contract two-phase
            # abort): recover this activation's `RevlTeardown` through the
            # SAME fiber's `ctx` (`revl_teardown_of` reads the metadata
            # `revl_teardown_begin` stored there at activation), then register
            # a disposer that discharges on commit or queues onto phase 2 on
            # abort — never runs immediately, unlike the old placeholder.
            undo_rename = _method_undo_rename(env, method)
            _method_undo_clones(env, method, out, indent)
            for local in sorted(_undo_reclone_locals(
                    acquire_node, step.get("compensate"), body_locals, env.v3_ctx())):
                out.append(f"{pad}let {local}_undo = {local}.clone();")
                undo_rename[local] = f"{local}_undo"
            out.append(f"{pad}let _revl_teardown = revl_teardown_of(&self.ctx);")
            _emit_compensation_registration(
                env, step["compensate"], f"{env.name}.{method.get('name')}.compensate.{index}",
                out, indent, undo_rename, ctx_expr="self.ctx", propagate=False)
        elif kind in ("let", "assign"):
            name = _ident(step.get("name"), "binding")
            # Seed the local's inferred type into the shared type table so a
            # later by-value use of it — a record field, a service-call/emit
            # argument (`emit sessions.append(id, Msg { content: answer })` then
            # `return answer`) — clones instead of moving it (E0382). Item 101
            # did this for the pure-fn path (`_v3_stmt`) but the effectful
            # provide-method renderer was missed, so a `let`-bound non-Copy local
            # reused after an emit/effect still moved (roadmap item 114).
            # `_by_value_arg` clones only a still-typed non-Copy identifier;
            # Copy scalars and fresh temporaries stay untouched, and over-cloning
            # a still-live immutable revl value is sound.
            inferred = _provide_let_type(env, step.get("value"), env.v3_ctx())
            if kind == "let":
                rename.pop(name, None)  # a local shadows an outer rename
                out.append(f"{pad}let {'mut ' if step.get('mutable') else ''}"
                           f"{name} = {_expr(step['value'], env, rename)};")
            else:
                out.append(f"{pad}{name} = {_expr(step['value'], env, rename)};")
            if inferred is not None:
                env.v3_ctx().var_types[step.get("name")] = inferred
            if kind == "let" and step.get("name") is not None:
                body_locals.add(step.get("name"))
        elif kind == "let-effect":
            # item 397: the only let-effect admitted in a provide-method body is
            # a result-declared host CAS (`insert_if_absent`). Mirror the bare
            # `effect` bracket, but BIND the acquire's `bool` result and guard
            # the site-spelled undo on it — a `false` CAS's inverse is the
            # identity, so teardown never removes the winner's entry.
            if not _is_map_cas(step.get("acquire")):
                raise EmitError(
                    "let-effect not allowed inside a provide method "
                    "(Rust backend)")
            bind = _ident(step["bind"], "binding")
            undo_rename = _method_undo_rename(env, method)
            acquire_rename = dict(rename)
            for param in method.get("params") or []:
                acquire_rename[param] = f"{param}.clone()"
            label = _string(f"{env.name}.{method.get('name')}.let-effect.{index}")
            undo_node = step.get("undo")
            acquire_node = step.get("acquire")
            _method_undo_clones(env, method, out, indent)
            for local in sorted(_undo_reclone_locals(
                    acquire_node, undo_node, body_locals, env.v3_ctx())):
                out.append(f"{pad}let {local}_undo = {local}.clone();")
                undo_rename[local] = f"{local}_undo"
            acquire = _expr(acquire_node, env, acquire_rename)
            out.append(f"{pad}let {bind} = {acquire};")
            env.v3_ctx().var_types[step.get("bind")] = "Bool"
            body_locals.add(step.get("bind"))
            undo = _expr(undo_node, env, undo_rename)
            out.append(
                f"{pad}let _ = self.ctx.effect({label}, "
                f"move || {{ if {bind} {{ {undo}; }} Ok(()) }});"
            )
        elif kind == "await":
            raise EmitError("await steps are not allowed inside method bodies (A1)")
        else:
            raise EmitError(f"unsupported method body step in Rust backend: {kind!r}")



def _emit_component_new(component: dict, services: dict, ir: dict | None = None) -> list[str]:
    """v2 components + effectful provide-method bodies.

    This is used for any component that needs more than the original v1 spike:
    `isolate`, `intercept`, or a provide method containing `effect`/`emit`.
    """
    ir = ir or {}
    env = _Env(
        component,
        services,
        types=ir.get("types"),
        functions=ir.get("functions"),
        externs=ir.get("externs"),
        components=ir.get("components"),
    )
    name = component["name"]
    cname = _ident(name, "component")
    snake = _snake(name)
    isolate = component.get("isolate") or {}
    intercept = component.get("intercept") or {}
    has_effectful = _component_has_effectful_methods(component)

    for local, service in env.reqs.items():
        _ident(local, "requirement")
        if service not in services:
            raise EmitError(f"requirement {local!r} names unknown service {service!r}")
    for key, service in env.provides.items():
        _ident(key, "provision")
        if service not in services:
            raise EmitError(f"provision {key!r} names unknown service {service!r}")
    for key in isolate:
        if key not in env.reqs and key not in env.provides:
            raise EmitError(f"{name}: isolate key {key!r} is not declared")
    for key in intercept:
        if key not in env.reqs:
            raise EmitError(f"{name}: intercept key {key!r} is not a requirement")

    out: list[str] = []

    config_ty = _emit_config_struct(component, out)
    # FR-4: bind -> Rust value type for each host Map binding, so provider
    # struct fields and `Map::new()` carry a concrete `V` instead of leaving
    # `Map<_>` open.
    map_values = _component_map_values(env)

    for key, service in env.provides.items():
        _ident(key, "provision")
        struct = f"{cname}{_camel(key)}"
        out.append(f"struct {struct} {{")
        for b in _binds(component):
            out.append(f"    {_ident(b, 'binding')}: Arc<{_host_of(component, b, map_values)}>,")
        if env.reqs:
            for local, req_service in env.reqs.items():
                out.append(f"    {local}: Arc<Box<dyn {req_service}>>,")
        out.extend(_config_struct_field(component, key))
        if has_effectful:
            out.append("    ctx: Arc<cordis::Context>,")
        out.append("}")
        out.append(f"impl {service} for {struct} {{")
        provide = next(
            (s for s in component.get("body") or []
             if s.get("step") == "provide" and s.get("name") == key),
            {"methods": []},
        )
        for method in provide.get("methods") or []:
            original_mname = method.get("name")
            mname = _ident(_mname(original_mname), "method")
            params = ", ".join(
                f"{p}: {_rust_type(_param_type(env, key, original_mname, p), env.types)}"
                for p in method.get("params") or []
            )
            ret = (_rust_type(_method_return(env, key, original_mname), env.types)
                   if _method_return(env, key, original_mname) else "()")
            # A provide-method body is rendered by the shared expression renderer, which
            # needs the parameter types to tell a string `+` from a numeric one.
            # Host Map bindings are registered too (as a `Map[...]` marker) so the
            # renderer knows their `get`/`remove` keys must be borrowed (FR-4).
            env.v3_ctx().var_types = {
                **{b: f"Map[{v}]" for b, v in map_values.items()},
                **{p: _param_type(env, key, original_mname, p)
                   for p in method.get("params") or []},
            }
            if _method_has_effectful_steps(method):
                out.append(f"    fn {mname}(&self, {params}) -> {ret} {{")
                _method_body_lines(env, method, out, indent=2)
                out.append("    }")
            else:
                out.append(f"    fn {mname}(&self, {params}) -> {ret} {{ {_method_body_pure_new(env, method)} }}")
        out.append("}")
        out.append("")


    if intercept:
        inject_parts = []
        for local in env.reqs:
            if local in intercept:
                base = f"{cname}{_camel(local)}"
                defs: list[str] = []
                type_names: dict = {}
                counter = [0]
                # called for its side effect: it appends the generated struct
                # defs to `defs` (emitted below). Its return type string is not
                # needed here, only the literal from `_intercept_json_lit`.
                _intercept_json_type(intercept[local], base, defs, type_names, counter)
                meta_lit = _intercept_json_lit(intercept[local], base, defs, type_names, counter)
                out.extend(defs)
                inject_parts.append(f".require_with({_string(local)}, {meta_lit})")
            else:
                inject_parts.append(f".require({_string(local)})")
        inject = "Inject::none()" + "".join(inject_parts)
    else:
        inject = _rust_inject(env)

    # item 167: a per-(component, key) router struct for each routed require.
    for key in env.routes:
        out.extend(_emit_router_struct(env, cname, key, env.reqs[key], env.routes[key]))

    uses_await = _component_uses_await(component)
    plugin_fn = "cordis::plugin_async::<{0}, _, _>" if uses_await else "cordis::plugin_sync::<{0}, _>"
    closure = "        |ctx, config| async move {" if uses_await else "        |ctx, config| {"
    out.append(f"pub fn {snake}() -> cordis::PluginHandle {{")
    out.append(f"    {plugin_fn.format(config_ty)}(")
    out.append(f"        {_string(name)},")
    out.append(f"        cordis::{inject},")
    out.append(closure)
    if env.needs_teardown:
        _emit_teardown_begin(env, out, indent=3)
    out.extend(_emit_config_application(component, config_ty, indent=3))
    out.extend(_emit_provide_config_local(component, indent=3))
    # Realm isolation is NOT applied here. cordis evaluates a plugin's reactive
    # `Inject` gate against the context the plugin is registered on, before this
    # closure ever runs — so isolating `ctx` inside the body cannot scope the
    # gate, and an isolated `requires kv in realm("t")` would hang Pending
    # forever (the fiber's `meta.isolates` never carries the realm). Isolation
    # is instead applied at plug time via `_revl_isolate_ctx` below, mirroring
    # the python/typescript backends' `plug()` helper. `ctx` is therefore
    # already the isolated context here, so provides/requires resolve in-realm.
    _emit_req_bindings(env, cname, out, indent=3)
    for step in component.get("body") or []:
        if step.get("step") == "provide":
            key = step.get("name")
            service = step.get("service")
            struct = f"{env.name}{_camel(key)}"
            fields = ", ".join(
                [f"{_ident(b, 'binding')}: {b}.clone()" for b in _binds(env.component)]
                + [f"{local}: {local}.clone()" for local in env.reqs]
                + _config_ctor_field(env.component, key)
                + (["ctx: Arc::new(ctx.clone())"] if has_effectful else [])
            )
            out.append(f"            let {key}_box: Box<dyn {service}> = Box::new({struct} {{ {fields} }});")
            out.append(f"            ctx.provide({_string(key)}, {key}_box)?;")
        else:
            _emit_step(step, env, out, indent=3)
    if env.needs_teardown:
        _emit_teardown_commit(env, out, indent=3)
    out.append("            Ok(cordis::PluginOutput::none())")
    out.append("        },")
    out.append("    )")
    out.append("}")
    out.append("")
    return out


def _emit_component_auto(component: dict, services: dict, ir: dict | None = None) -> list[str]:
    if (
        not (component.get("isolate") or component.get("intercept"))
        and not _component_has_effectful_methods(component)
    ):
        return _emit_component(component, services, ir)
    return _emit_component_new(component, services, ir)


def _collect_realm_labels(components: list) -> list[str]:
    """Every distinct realm-label string the program isolates on, sorted.

    Sorting makes index assignment a pure function of the label *set*, so the
    same source lowers to the same Isolation values on every build.
    """
    labels: set[str] = set()
    for component in components:
        for realm in (component.get("isolate") or {}).values():
            labels.add(realm)
        # item 167: a router resolves its worker realms by label too; every such
        # realm has a provider (G2, verified at link time) so it is normally
        # already collected, but register it explicitly to be robust.
        for route in (component.get("routes") or {}).values():
            for realm in route.get("realms") or []:
                labels.add(realm)
    return sorted(labels)


def _revl_realm_helper(components: list) -> list[str]:
    """Lower each distinct realm label to a STABLE, DETERMINISTIC Isolation
    from a reserved high region of the u64 scope space.

    cordis-rs mints framework scope labels from a monotonic counter that
    starts at 1 and increments by 1 (`RootInner::scope`,
    cordis-rs-0.3.0/src/context.rs:86-88), so every framework label lands in
    the low range [1, 2^63). We tag realm labels with the top bit set, which
    that counter cannot reach without 2^63 allocations — provably disjoint
    from framework labels (the collision `Isolation::from_raw`'s own docs
    warn about, context.rs:20-25). Distinct labels get distinct indices, so
    equal strings share a realm and no two realms ever collide. Unlike the
    former `DefaultHasher`, this registry is fixed build-to-build and depends
    on no std hashing internals.
    """
    labels = _collect_realm_labels(components)
    lines = [
        "pub fn _revl_realm(label: &str) -> cordis::Isolation {",
        "    // Top bit reserved for realm labels: disjoint from cordis-rs's",
        "    // monotonic scope counter (starts at 1, +1 each isolate()).",
        "    const REVL_REALM_TAG: u64 = 0x8000_0000_0000_0000;",
        "    let index: u64 = match label {",
    ]
    for i, label in enumerate(labels):
        lines.append(f"        {_string(label)} => {i},")
    lines.append(
        '        other => panic!("revl: realm label {other:?} missing from '
        'compile-time registry"),'
    )
    lines.extend(
        [
            "    };",
            "    cordis::Isolation::from_raw(REVL_REALM_TAG | index)",
            "}",
            "",
        ]
    )
    return lines



def _emit_router_struct(env: "_Env", cname: str, key: str, service: str,
                        route: dict) -> list[str]:
    """item 167: the emitted realization of a routed require (item 162's
    `routes` IR) on cordis-rs, mirroring src/revl/run.py::_Router.

    A per-(component, key) struct implementing the required service trait. It
    holds no worker handle — every trait call re-resolves the live per-realm
    handle off the same strict, realm-scoped committed-view read a normal
    require uses (`ctx.root().isolate_with(k, realm(w)).get::<Box<dyn S>>(k)`,
    which cordis-rs returns `Ok(None)` for a non-ACTIVE provider). So a
    withdrawn worker drops out of the live set and its calls go to the
    survivors — reactive failover from the emitted body. The struct is wrapped
    as the component's `Arc<Box<dyn S>>` handle, so a provide-method's
    `<key>.<op>(…)` forwards straight through it (G2: one provider downstream).
    """
    struct = f"RevlRouter{cname}{_camel(key)}"
    boxed = f"std::sync::Arc<Box<dyn {service}>>"
    realms = list(route.get("realms") or [])
    strategy = route.get("strategy") or "round_robin"
    realm_lits = ", ".join(f"{_string(r)}.to_string()" for r in realms)
    methods = (env.services.get(service) or {}).get("methods") or {}

    out: list[str] = [
        f"struct {struct} {{",
        "    ctx: cordis::Context,",
        "    key: String,",
        "    realms: Vec<String>,",
        "    strategy: String,",
        "    cursor: std::sync::Mutex<usize>,",
        "    served: std::sync::Mutex<std::collections::HashMap<String, u64>>,",
        "}",
        f"impl {struct} {{",
        f"    fn _revl_new(ctx: cordis::Context) -> {boxed} {{",
        "        std::sync::Arc::new(Box::new(Self {",
        "            ctx: ctx.root(),",
        f"            key: {_string(key)}.to_string(),",
        f"            realms: vec![{realm_lits}],",
        f"            strategy: {_string(strategy)}.to_string(),",
        "            cursor: std::sync::Mutex::new(0),",
        "            served: std::sync::Mutex::new(std::collections::HashMap::new()),",
        "        }))",
        "    }",
        f"    fn _revl_live(&self) -> Vec<(String, {boxed})> {{",
        "        let mut out = Vec::new();",
        "        for realm in &self.realms {",
        "            let scoped = self.ctx.isolate_with("
        "self.key.as_str(), _revl_realm(realm.as_str()));",
        f"            if let Ok(Some(handle)) = scoped.get::<Box<dyn {service}>>"
        "(self.key.as_str()) {",
        "                out.push((realm.clone(), handle));",
        "            }",
        "        }",
        "        out",
        "    }",
        f"    fn _revl_select(&self) -> {boxed} {{",
        "        let live = self._revl_live();",
        "        if live.is_empty() {",
        "            panic!(\"revl: router for {:?} has no live worker: all {} "
        "realm(s) ({}) have withdrawn\", self.key, self.realms.len(), "
        "self.realms.join(\", \"));",
        "        }",
        "        if self.strategy == \"least_loaded\" {",
        "            let served = self.served.lock().unwrap();",
        "            let mut best: usize = 0;",
        "            for i in 1..live.len() {",
        "                let ci = *served.get(&live[i].0).unwrap_or(&0);",
        "                let cb = *served.get(&live[best].0).unwrap_or(&0);",
        "                if ci < cb { best = i; }",
        "            }",
        "            drop(served);",
        "            let realm = live[best].0.clone();",
        "            *self.served.lock().unwrap().entry(realm).or_insert(0) += 1;",
        "            live[best].1.clone()",
        "        } else {",
        "            let n = self.realms.len();",
        "            let mut cursor = self.cursor.lock().unwrap();",
        "            let start = *cursor;",
        "            for off in 0..n {",
        "                let cand = &self.realms[(start + off) % n];",
        "                if let Some((realm, handle)) = live.iter().find(|(r, _)| r == cand) {",
        "                    *cursor = (start + off + 1) % n;",
        "                    drop(cursor);",
        "                    *self.served.lock().unwrap().entry(realm.clone())"
        ".or_insert(0) += 1;",
        "                    return handle.clone();",
        "                }",
        "            }",
        "            unreachable!()",
        "        }",
        "    }",
        "}",
        f"impl {service} for {struct} {{",
    ]
    for mname, method in methods.items():
        rmname = _mname(mname)
        params = ", ".join(
            f"{_ident(p.get('name'), 'parameter')}: {_rust_type(p.get('type'), env.types)}"
            for p in method.get("params") or []
        )
        args = ", ".join(_ident(p.get("name"), "parameter")
                         for p in method.get("params") or [])
        ret = _rust_type(method.get("returns"), env.types) if method.get("returns") else "()"
        out.append(f"    fn {rmname}(&self, {params}) -> {ret} {{ "
                   f"self._revl_select().{rmname}({args}) }}")
    out.append("}")
    out.append("")
    return out


def _emit_component(component: dict, services: dict, ir: dict | None = None) -> list[str]:
    ir = ir or {}
    env = _Env(
        component,
        services,
        types=ir.get("types"),
        functions=ir.get("functions"),
        externs=ir.get("externs"),
        components=ir.get("components"),
    )
    name = component["name"]
    cname = _ident(name, "component")
    snake = _snake(name)
    out: list[str] = []

    config_ty = _emit_config_struct(component, out)
    # FR-4: bind -> Rust value type for each host Map binding, so provider
    # struct fields and `Map::new()` carry a concrete `V` instead of leaving
    # `Map<_>` open.
    map_values = _component_map_values(env)

    for key, service in env.provides.items():
        _ident(key, "provision")
        struct = f"{cname}{_camel(key)}"
        out.append(f"struct {struct} {{")
        for b in _binds(component):
            out.append(f"    {_ident(b, 'binding')}: Arc<{_host_of(component, b, map_values)}>,")
        # a provide-method may call a required service, so the provider owns
        # the same bindings the effectful path captures (java does this too)
        for local, req_service in env.reqs.items():
            out.append(f"    {local}: Arc<Box<dyn {req_service}>>,")
        out.extend(_config_struct_field(component, key))
        out.append("}")
        out.append(f"impl {service} for {struct} {{")
        provide = next(
            (s for s in component.get("body") or []
             if s.get("step") == "provide" and s.get("name") == key),
            {"methods": []},
        )
        for method in provide.get("methods") or []:
            mname = _ident(method.get("name"), "method")
            params = ", ".join(
                f"{p}: {_rust_type(_param_type(env, key, mname, p), env.types)}"
                for p in method.get("params") or []
            )
            ret = (_rust_type(_method_return(env, key, mname), env.types)
                   if _method_return(env, key, mname) else "()")
            # A provide-method body is rendered by the shared expression renderer, which
            # needs the parameter types to tell a string `+` from a numeric one.
            # Host Map bindings are registered too (as a `Map[...]` marker) so the
            # renderer knows their `get`/`remove` keys must be borrowed (FR-4).
            env.v3_ctx().var_types = {
                **{b: f"Map[{v}]" for b, v in map_values.items()},
                **{p: _param_type(env, key, mname, p)
                   for p in method.get("params") or []},
            }
            out.append(f"    fn {mname}(&self, {params}) -> {ret} {{ {_method_body(env, method)} }}")
        out.append("}")
        out.append("")

    # item 167: a per-(component, key) router struct for each routed require.
    for key in env.routes:
        out.extend(_emit_router_struct(env, cname, key, env.reqs[key], env.routes[key]))

    inject = _rust_inject(env)
    uses_await = _component_uses_await(component)
    plugin_fn = "cordis::plugin_async::<{0}, _, _>" if uses_await else "cordis::plugin_sync::<{0}, _>"
    closure = "        |ctx, config| async move {" if uses_await else "        |ctx, config| {"
    out.append(f"pub fn {snake}() -> cordis::PluginHandle {{")
    out.append(f"    {plugin_fn.format(config_ty)}(")
    out.append(f"        {_string(name)},")
    out.append(f"        cordis::{inject},")
    out.append(closure)
    if env.needs_teardown:
        _emit_teardown_begin(env, out, indent=3)
    out.extend(_emit_config_application(component, config_ty, indent=3))
    out.extend(_emit_provide_config_local(component, indent=3))
    _emit_req_bindings(env, cname, out, indent=3)
    for step in component.get("body") or []:
        _emit_step(step, env, out, indent=3)
    if env.needs_teardown:
        _emit_teardown_commit(env, out, indent=3)
    out.append("            Ok(cordis::PluginOutput::none())")
    out.append("        },")
    out.append("    )")
    out.append("}")
    out.append("")
    return out


def _rust_inject(env: "_Env") -> str:
    """The `Inject` gate. item 167: routed keys never enter the gate — they have
    no single-realm provider (the workers live in the named realms), so a fiber
    waiting on one would hang Pending forever; the router resolves them per call."""
    gated = [k for k in env.reqs if k not in env.routes]
    return "Inject::none()" if not gated else f"Inject::new({_string(gated)})"


def _emit_teardown_begin(env: "_Env", out: list[str], indent: int) -> None:
    """item 243 Slice 2b: open this activation's `RevlTeardown` FIRST (before
    any user step registers an effect), so the phase-2 drain hook it
    registers is disposed LAST by cordis-rs's LIFO unload — see
    `_revl_teardown_preamble`. `ctx` is rebound to the extended context
    `revl_teardown_begin` returns, so every later use of `ctx` in this
    activation (provide-struct construction included) carries the teardown
    state a provide-method later recovers via `revl_teardown_of`."""
    pad = "    " * indent
    label = _string(env.name + ".teardown.phase2")
    out.append(f"{pad}let (ctx, _revl_teardown) = revl_teardown_begin(&ctx, {label})?;")


def _emit_teardown_commit(env: "_Env", out: list[str], indent: int) -> None:
    """The abort-vs-commit discriminator: flip `committed` right before this
    activation reports success, mirroring `Frame.drain` on py — every
    transactional/compensation disposer collected so far discharges (rather
    than replaying) from this instant on, whenever cordis-rs eventually
    disposes this fiber for real."""
    pad = "    " * indent
    out.append(
        f"{pad}_revl_teardown.committed.store(true, std::sync::atomic::Ordering::Release);"
    )


def _emit_req_bindings(env: "_Env", cname: str, out: list[str], indent: int) -> None:
    """Bind each required service in the plugin closure. A routed key (item 167)
    binds a router struct that re-resolves live workers per call; a plain
    require binds the single active provider through the Inject gate."""
    pad = "    " * indent
    for local, service in env.reqs.items():
        if local in env.routes:
            struct = f"RevlRouter{cname}{_camel(local)}"
            out.append(f"{pad}let {local}: std::sync::Arc<Box<dyn {service}>> = "
                       f"{struct}::_revl_new(ctx.clone());")
        else:
            out.append(f"{pad}let {local} = ctx.require::<Box<dyn {service}>>({_string(local)})?;")


def _emit_setup_value(node: dict, env: _Env) -> str:
    """Lower a setup value.

    revl `Str` is emitted as `String`, and `_render_expr` already lowers every
    string literal to an owned `String::from(..)`, so a setup `let` binding
    never risks being inferred as `&str` and needs no extra coercion here.
    """
    return _expr(node, env)


def _emit_setup_step(step: dict, env: _Env, out: list[str], indent: int) -> None:
    """Lower a pure block-effect setup step (stratum-1)."""
    pad = "    " * indent
    kind = step.get("step")
    if kind == "let":
        name = _ident(step.get("name"), "setup binding")
        out.append(f"{pad}let mut {name} = {_emit_setup_value(step.get('value'), env)};")
    elif kind == "assign":
        name = _ident(step.get("name"), "setup assign")
        out.append(f"{pad}{name} = {_emit_setup_value(step.get('value'), env)};")
    elif kind == "expr":
        out.append(f"{pad}let _ = {_expr(step.get('expr'), env)};")
    elif kind == "if":
        out.append(f"{pad}if {_expr(step.get('cond'), env)} {{")
        for nested in step.get("then") or []:
            _emit_setup_step(nested, env, out, indent + 1)
        if step.get("else"):
            out.append(f"{pad}}} else {{")
            for nested in step.get("else") or []:
                _emit_setup_step(nested, env, out, indent + 1)
        out.append(f"{pad}}}")
    elif kind == "assert":
        out.append(f"{pad}assert!({_expr(step.get('expr'), env)});")
    else:
        raise EmitError(f"unsupported setup step in Rust backend: {kind!r}")


def _emit_witnessed_step(env: "_Env", step: dict, ext: dict, out: list[str],
                         indent: int, bind: str | None) -> None:
    """Emit a witnessed effect (item 243): run the mutation, and on `Ok`
    register the extern's DECLARED inverse as a `transactional` entry in the
    activation's `RevlTeardown` (docs/design/teardown-contract.md). Mirrors
    backends/python/emit.py's `_witnessed_step`: unlike a bracket (a plain
    `ctx.effect` that always replays), this disposer reads `committed` at
    DISPOSAL time — discharge (drop the witness, do nothing) on a clean
    commit, replay the declared inverse on abort. Registration is
    unconditional over the `Ok` branch; an `Err` mutation touched nothing, so
    it registers no rollback (243 rule: Ok-conditional). `bind`, when given,
    holds the WHOLE `Result` (not unwrapped) — same as py's `_witnessed_step`."""
    pad = "    " * indent
    n = env.wit_counter
    env.wit_counter += 1
    tmp = f"_revl_wit{n}"
    witv = f"_revl_witv{n}"
    out.append(f"{pad}let {tmp} = {_expr(step.get('acquire'), env)};")
    witness_ty = _rust_type(ext.get("witness"), env.types)
    label = _string(env.name + "." + (step.get("bind") or "effect") + ".witnessed")
    out.append(f"{pad}if let Ok(ref {witv}) = {tmp} {{")
    out.append(f"{pad}    let result: {witness_ty} = {witv}.clone();")
    if _RECORD_MODE:
        # item 322 Slice 2: the durable exit. At REGISTRATION (this branch runs
        # during activation, when the mutation happened) write the
        # discharge-descriptor — the re-issuable named call recover replays LIFO
        # to undo the mutation — and fsync it, so a crash BEFORE commit is still
        # recoverable from the log alone. `result` (the Ok witness) is
        # stringified as the referent argument; the borrow ends before `result`
        # moves into the disposer closure below. Mirrors the go tier's
        # `revlRecordTransactional` call at the same point.
        undo_callee = (ext.get("undo") or {}).get("callee") or {}
        undo_name = str(undo_callee.get("name") or undo_callee.get("id") or "undo")
        out.append(
            f'{pad}    revl_record_transactional({_string(ext.get("name"))}, '
            f'{_string(undo_name)}, vec![format!("{{}}", result)]);')
    out.append(f"{pad}    let _revl_state = _revl_teardown.clone();")
    undo = _expr(ext["undo"], env)
    out.append(f"{pad}    ctx.effect({label}, move || {{")
    out.append(f"{pad}        if !_revl_state.committed.load(std::sync::atomic::Ordering::Acquire) {{")
    out.append(f"{pad}            let _ = {undo};")
    out.append(f"{pad}        }}")
    out.append(f"{pad}        Ok(())")
    out.append(f"{pad}    }})?;")
    out.append(f"{pad}}}")
    if bind is not None:
        out.append(f"{pad}let {bind} = {tmp};")


def _emit_method_witnessed_step(env: "_Env", method: dict, step: dict, ext: dict,
                                out: list[str], indent: int) -> None:
    """Emit a witnessed effect inside a PROVIDE-METHOD body — item 324, THE
    per-tool-call H1 gate (docs/design/243-witnessed-externs.md,
    docs/design/teardown-contract.md). Mirrors backends/python/emit.py's
    `_method_witnessed_step` / `Frame.transactional_method`: run the per-call
    mutation, and on `Ok` register the extern's DECLARED inverse as a
    `transactional` entry on the ENCLOSING COMPONENT's activation frame,
    recovered through `self.ctx` (the SAME fiber `revl_teardown_begin` extended
    at activation — `revl_teardown_of` reads the metadata stored there). The
    method returns, but the inverse must outlive the call: it survives on the
    component-long activation frame until the component/session commits or
    aborts.

    THE SOUNDNESS HAZARD, and why rust does NOT have it. On cordis-py the
    obvious "adopt the entry as a sibling effect" is unsound: py flips
    `_committed` LAZILY, inside `Frame.drain` at teardown, and cordis-py
    disposes an adopted sibling effect BEFORE that drain — so on a clean unload
    the disposer would observe `_committed` still False and wrongly revert the
    deliverable. py's fix PARKS the entry (`_deferred_transactional`) for `drain`
    to dispose after the commit bit is settled. The rust tier flips `committed`
    EAGERLY, at activation-end (`_emit_teardown_commit`, the same instant py's
    drain would). By the time a per-tool-call method runs and registers this
    sibling `self.ctx.effect` disposer, `committed` is ALREADY settled (True on
    a live activation), and cordis-rs disposes it in the fiber's single LIFO
    unload pass where it reads that settled bit by construction — discharge
    (drop the witness, do nothing) on a clean commit, replay the declared
    inverse on abort (`revl_abort` cleared `committed` before unload). No
    park-for-drain discipline is needed: eager commit sidesteps the premature-
    disposal window entirely. Registration is fire-and-forget (`let _ =
    self.ctx.effect(...)`) — a non-`Result` method signature cannot `?`-propagate
    — matching every other method-body registration, and unconditional over the
    `Ok` branch (an `Err` mutation touched nothing, so it schedules no
    rollback: 243's Ok-conditional rule)."""
    pad = "    " * indent
    n = env.wit_counter
    env.wit_counter += 1
    tmp = f"_revl_wit{n}"
    witv = f"_revl_witv{n}"
    # The shared expression renderer already clones a typed non-Copy param used
    # by value as a call argument (`_by_value_arg`), so the acquire needs only
    # the component-scope rename — no extra param `.clone()` (that would double).
    out.append(f"{pad}let {tmp} = {_expr(step.get('acquire'), env, _method_scope_rename(env))};")
    witness_ty = _rust_type(ext.get("witness"), env.types)
    label = _string(f"{env.name}.{method.get('name')}.witnessed")
    undo = _expr(ext["undo"], env)
    out.append(f"{pad}if let Ok(ref {witv}) = {tmp} {{")
    out.append(f"{pad}    let result: {witness_ty} = {witv}.clone();")
    out.append(f"{pad}    let _revl_state = revl_teardown_of(&self.ctx);")
    out.append(f"{pad}    let _ = self.ctx.effect({label}, move || {{")
    out.append(f"{pad}        if !_revl_state.committed.load(std::sync::atomic::Ordering::Acquire) {{")
    out.append(f"{pad}            let _ = {undo};")
    out.append(f"{pad}        }}")
    out.append(f"{pad}        Ok(())")
    out.append(f"{pad}    }});")
    out.append(f"{pad}}}")


def _emit_compensation_registration(env: "_Env", compensate_node: dict, label_text: str,
                                    out: list[str], indent: int, rename: dict[str, str],
                                    ctx_expr: str = "ctx", propagate: bool = True) -> None:
    """The shared tail of a `compensation` entry registration (item 247, the
    teardown-contract two-phase abort): wrap the (already-renamed) compensate
    call in a boxed `FnOnce`, and register a disposer that discharges on
    commit or QUEUES onto the activation's phase-2 queue on abort, instead of
    running immediately — see `_revl_teardown_preamble` for why that queueing
    is what gives Phase 1 (bracket + transactional) priority over Phase 2
    (compensation) for free, from cordis-rs's own LIFO dispose order.

    `ctx_expr` is `"ctx"` (`?`-propagated, `propagate=True`) at activation
    level and `"self.ctx"` (fire-and-forget, `propagate=False`, matching every
    other method-body registration) inside a provide method."""
    pad = "    " * indent
    call = _expr(compensate_node, env, rename=rename)
    label = _string(label_text)
    tail = "?;" if propagate else ";"
    lead = "" if propagate else "let _ = "
    out.append(f"{pad}let _revl_state = _revl_teardown.clone();")
    out.append(f"{pad}let _revl_call: Box<dyn FnOnce() + Send> = Box::new(move || {{ let _ = {call}; }});")
    out.append(f"{pad}{lead}{ctx_expr}.effect({label}, move || {{")
    out.append(f"{pad}    if !_revl_state.committed.load(std::sync::atomic::Ordering::Acquire) {{")
    out.append(f"{pad}        _revl_state.phase2.lock().unwrap().push(")
    out.append(f"{pad}            RevlPendingCompensation {{ label: {label}.to_string(), call: _revl_call }});")
    out.append(f"{pad}    }}")
    out.append(f"{pad}    Ok(())")
    out.append(f"{pad}}}){tail}")


def _emit_activation_compensation(env: "_Env", step: dict, out: list[str], indent: int) -> None:
    """`emit ... compensate` at activation level. Requires `env.needs_teardown`
    (the caller guarantees `_revl_teardown`/`ctx` are the extended locals from
    `_emit_teardown_begin`). Pre-clones every `req` and every prior
    activation-body `let-effect` bind the compensate expression reads, so the
    `move` closure owns copies the forward `emit` call cannot have moved out
    from under it (mirrors the existing bracket-undo req cloning above)."""
    pad = "    " * indent
    referenced: set[str] = set()
    _expr_var_names(step.get("compensate"), referenced)
    rename: dict[str, str] = {}
    for req in env.reqs:
        req_c = f"{req}_comp"
        out.append(f"{pad}let {req_c} = {req}.clone();")
        rename[req] = req_c
    for local in sorted(referenced & set(env.activation_binds)):
        local_c = f"{local}_comp"
        out.append(f"{pad}let {local_c} = {local}.clone();")
        rename[local] = local_c
    _emit_compensation_registration(
        env, step["compensate"], env.name + ".compensate", out, indent, rename)


def _emit_step(step: dict, env: _Env, out: list[str], indent: int) -> None:
    pad = "    " * indent
    kind = step.get("step")
    if kind == "let-effect":
        for setup in step.get("setup") or []:
            _emit_setup_step(setup, env, out, indent)
        wit = _witnessed_extern_for(env, step.get("acquire"))
        if wit is not None:
            _emit_witnessed_step(env, step, wit, out, indent, bind=_ident(step["bind"], "binding"))
            env.activation_binds.append(step["bind"])
            return
        bind = _ident(step["bind"], "binding")
        env.activation_binds.append(step["bind"])
        acquire = _expr(step["acquire"], env)
        # FR-4: pin the host Map's value type at the constructor so rustc
        # never has to infer it — a binding no provider struct captures, or
        # one whose only typed use is a `get`, would leave `Map<V>` open.
        acq = step.get("acquire") or {}
        if acq.get("kind") == "host" and acq.get("fn") == "Map.new":
            v = _component_map_values(env).get(step["bind"], "String")
            acquire = f"Map::<{v}>::new()"
        out.append(f"{pad}let {bind} = Arc::new({acquire});")
        undo_name = f"{bind}_undo"
        out.append(f"{pad}let {undo_name} = {bind}.clone();")
        undo_rename = {step["bind"]: undo_name}
        for req in env.reqs:
            req_undo = f"{req}_undo"
            out.append(f"{pad}let {req_undo} = {req}.clone();")
            undo_rename[req] = req_undo
        is_cas = _is_map_cas(step.get("acquire"))
        if is_cas:
            # item 397: a result-declared host CAS binds an `Arc<bool>`. Its
            # site-spelled undo removes from a PRIOR activation bind (the
            # ledger); a bare `move` closure would consume that bind, leaving
            # the later provide struct's `.clone()` a use-after-move (E0382).
            # Reclone every referenced prior activation bind into the closure,
            # exactly as the reqs are recloned above.
            referenced: set[str] = set()
            _expr_var_names(step.get("undo"), referenced)
            for local in sorted(referenced & set(env.activation_binds)):
                if local == step["bind"]:
                    continue
                local_undo = f"{local}_undo"
                out.append(f"{pad}let {local_undo} = {local}.clone();")
                undo_rename[local] = local_undo
        undo = _expr(step["undo"], env, rename=undo_rename)
        label = _string(env.name + "." + step["bind"] + ".undo")
        if is_cas:
            # result-guarded undo: the identity inverse on a `false` CAS, so
            # teardown never removes the winner's entry (`*` derefs the
            # `Arc<bool>` clone the closure owns).
            out.append(
                f"{pad}ctx.effect({label}, "
                f"move || {{ if *{undo_name} {{ {undo}; }} Ok(()) }})?;")
        else:
            out.append(f"{pad}ctx.effect({label}, move || {{ {undo}; Ok(()) }})?;")
    elif kind == "effect":
        for setup in step.get("setup") or []:
            _emit_setup_step(setup, env, out, indent)
        wit = _witnessed_extern_for(env, step.get("acquire"))
        if wit is not None:
            _emit_witnessed_step(env, step, wit, out, indent, bind=None)
            return
        acquire = _expr(step["acquire"], env)
        undo_rename: dict[str, str] = {}
        for req in env.reqs:
            req_undo = f"{req}_undo"
            out.append(f"{pad}let {req_undo} = {req}.clone();")
            undo_rename[req] = req_undo
        undo = _expr(step["undo"], env, rename=undo_rename)
        out.append(f"{pad}let _ = {acquire};")
        label = _string(env.name + ".effect")
        out.append(f"{pad}ctx.effect({label}, move || {{ {undo}; Ok(()) }})?;")
    elif kind == "emit":
        out.append(f"{pad}let _ = {_expr(step['expr'], env)};")
        if step.get("compensate") is not None:
            _emit_activation_compensation(env, step, out, indent)
    elif kind == "timer":
        # A `timer` step (item 57, docs/time-coeffect.md): a revertible
        # schedule. Arming the timer is the acquire, cancellation its derived
        # inverse — registered through the SAME `ctx.effect` ledger that reverts
        # a Pool or a provision, so unloading the component provably cancels the
        # timer with no orphaned interval (residue-free; the leak the residue
        # probe hunts cannot occur). The firing closure holds the body's
        # emissions and runs at activation-time stratum with the component's
        # declared capabilities — each `emit` lowers through the same path a
        # top-level emission does, so G4/G8 reach is audited. Time advances only
        # on `revl_clock_advance`, so a firing is a deterministic timeline step,
        # never a wall-clock race.
        mode = step.get("mode")
        schedule = "revl_schedule_every" if mode == "every" else "revl_schedule_after"
        interval = int(step.get("interval_ms"))
        env.timer_counter += 1
        n = env.timer_counter
        # Clone each required service the firing body may capture, under a
        # per-timer name, so the `move` closure owns its own handle and a second
        # timer (or a later step) can still use the original (no E0382). Mirrors
        # how `effect`/`let-effect` clone reqs into their undo closures.
        rename: dict[str, str] = {}
        for req in env.reqs:
            cloned = f"{req}_t{n}"
            out.append(f"{pad}let {cloned} = {req}.clone();")
            rename[req] = cloned
        out.append(f"{pad}let _revl_timer_{n} = {schedule}({interval}, move || {{")
        for em in step.get("body") or []:
            if em.get("step") != "emit":  # lowerer invariant (scope: emissions)
                raise EmitError(
                    f"timer body carries emissions only, found {em.get('step')!r}")
            out.append(f"{pad}    let _ = {_expr(em.get('expr'), env, rename=rename)};")
        out.append(f"{pad}}});")
        # the derived inverse: cancellation, yielded into the disposer stack.
        label = _string(env.name + ".timer.undo")
        out.append(f"{pad}ctx.effect({label}, move || {{ "
                   f"revl_cancel(_revl_timer_{n}); Ok(()) }})?;")
    elif kind == "fail":
        message = _expr(step.get("message"), env)
        out.append(
            f"{pad}return Err(cordis::CordisError::with_message("
            f"cordis::ErrorCode::Plugin, {message}));"
        )
    elif kind == "if":
        out.append(f"{pad}if {_expr(step.get('cond'), env)} {{")
        for nested in step.get("then") or []:
            _emit_step(nested, env, out, indent + 1)
        if step.get("else"):
            out.append(f"{pad}}} else {{")
            for nested in step.get("else") or []:
                _emit_step(nested, env, out, indent + 1)
        out.append(f"{pad}}}")
    elif kind == "await":
        # item 131 repair: rust erases method async-ness (async-extern.md §2
        # family 2), so a req-target async op or an async-colored fn returns a
        # plain value here, not a future — a blanket `.await` is a rustc error
        # on it (`heat()` yields `String`, not `impl Future`). Only the host
        # async seam (`Job.run`, which cordis-rs drives as a real future via
        # `plugin_async`) takes `.await`; every other awaitable erases to a plain
        # blocking call, matching the go/java erasure. The A1 ordering boundary
        # is the statement position, preserved either way. This is the latent
        # tier bug item 131's widened await-step admission would otherwise make
        # live (design §5, the rust slice).
        expr = step.get("expr")
        rendered = _expr(expr, env)
        if isinstance(expr, dict) and expr.get("kind") == "host" \
                and expr.get("fn") == "Job.run":
            out.append(f"{pad}{rendered}.await;")
        elif _is_stream_next(expr, env):
            # item 130 Slice 3: `await sub.next()` blocks on the item/terminal/
            # cancel race. Two of the three outcomes are terminals and they are
            # NOT the same: `Closed` (an orderly provider close, or the owner's
            # own `close` tripping the cancel signal) is an ordinary value the
            # activation carries on from, while `Faulted` (a provider abort, or
            # an `error`-policy overflow) FAILS the activation — so the prefix,
            # subscription bracket included, reverts LIFO. Never a silent drop.
            out.append(
                f"{pad}{rendered}.map_err(|e| cordis::CordisError::with_message("
                f"cordis::ErrorCode::Plugin, e))?;")
        else:
            out.append(f"{pad}{rendered};")
    elif kind == "provide":
        key = step.get("name")
        service = step.get("service")
        struct = f"{env.name}{_camel(key)}"
        fields = ", ".join(
            [f"{_ident(b, 'binding')}: {b}.clone()" for b in _binds(env.component)]
            + [f"{local}: {local}.clone()" for local in env.reqs]
            + _config_ctor_field(env.component, key)
        )
        out.append(f"{pad}let {key}_box: Box<dyn {service}> = Box::new({struct} {{ {fields} }});")
        out.append(f"{pad}ctx.provide({_string(key)}, {key}_box)?;")
    else:
        raise EmitError(f"unsupported component step in Rust backend: {kind!r}")


_V3_BIN_OPS = {
    "==": "==",
    "===": "==",
    "!=": "!=",
    "!==": "!=",
    "<": "<",
    ">": ">",
    "<=": "<=",
    ">=": ">=",
    "+": "+",
    "-": "-",
    "*": "*",
    "/": "/",
    "%": "%",
    "&&": "&&",
    "||": "||",
}

# Expression kinds that render as a Rust postfix-safe primary, so a `.method()`,
# `.field`, `[index]` or `.unwrap_or_else(..)` suffix can be appended without
# wrapping them in parentheses. The set is the union of both dialects' atomic
# kinds: the 2.0 primaries (`var`/`name`/`field`/`index`/`call`/`lit`) plus the
# component primaries (`req`/`config`/`host`), which never appear in a 2.0 body
# but must stay paren-free when a component `??`/receiver builds on them.
_ATOMIC_KINDS = {
    "var", "name", "field", "index", "call", "lit", "req", "config", "host",
}
_V3_HOST_ROOTS = set(_HOST_STUBS)
_V3_BUILTIN_CONSTRUCTORS = {"Some", "None", "Ok", "Err"}


def _v3_is_str(node: object, ctx: "_V3Ctx") -> bool:
    """Is this expression certainly a `Str`?

    Deliberately conservative — it answers "certainly yes" or "unknown", never
    guesses. A false positive would render a numeric `+` as a `format!` and
    produce a `String` where an `i64` is expected, so the only sources trusted
    here are a string literal, a template, a binding whose declared or
    inferred type is `Str`, and a `+` over those.
    """
    if not isinstance(node, dict):
        return False
    kind = node.get("kind")
    if kind == "lit":
        return isinstance(node.get("value"), str)
    if kind == "interp":
        return True
    if kind in ("name", "var"):
        return ctx.var_types.get(node.get("id") or node.get("name")) == "Str"
    if kind == "bin" and node.get("op") == "+":
        return _v3_is_str(node.get("left"), ctx) or _v3_is_str(node.get("right"), ctx)
    return False


def _list_element_type(surface: object) -> str | None:
    """The element surface type of a `List[T]` surface type, else None."""
    if isinstance(surface, str):
        m = re.match(r"^List\[(.+)\]$", surface)
        if m:
            return m.group(1).strip()
    return None


def _v3_is_empty_list(node: object) -> bool:
    return (isinstance(node, dict) and node.get("kind") == "list"
            and not node.get("items"))


def _v3_empty_vec_elem_types(body: object, ctx: "_V3Ctx") -> dict:
    """Map each local bound to an EMPTY list literal (`let out = []`) to its
    element surface type, when the body makes it knowable.

    An empty `vec![]` gives Rust no element type, and the accumulator idiom
    (`let mut out = []` then `out = out.push(x)` / `out = res`) reaches its type
    only through later statements, so rustc reports `type annotations needed`
    (E0282). This pre-pass recovers the element type from the pushes into the
    binding and from aliasing assignments, so the `let` can be annotated
    `Vec<T>`. It is conservative -- an un-inferable binding is simply left
    unannotated (byte-identical to before) -- and runs on a scratch `var_types`
    so it never perturbs the real emission."""
    empties: set = set()

    def collect(node: object) -> None:
        if isinstance(node, dict):
            if node.get("step") in ("let", "assign") and _v3_is_empty_list(node.get("value")):
                empties.add(node.get("name"))
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    collect(body)
    if not empties:
        return {}

    saved = ctx.var_types
    scratch = dict(saved)
    ctx.var_types = scratch
    try:
        # Forward-infer every `let`/`assign` type (params seed `scratch`), twice
        # so a value referring to an earlier-typed local resolves; this types the
        # indexed elements / call results that later feed a push.
        def forward(node: object) -> None:
            if isinstance(node, dict):
                if node.get("step") in ("let", "assign"):
                    t = _v3_infer_type(node.get("value"), ctx)
                    if t is not None:
                        scratch[node.get("name")] = t
                elif node.get("step") == "for":
                    # `for x in xs` types the loop binding as the element of the
                    # iterable, so a push of `x` types the accumulator.
                    it = _list_element_type(_v3_infer_type(node.get("iterable"), ctx))
                    if it is not None:
                        scratch[node.get("bind")] = it
                for v in node.values():
                    forward(v)
            elif isinstance(node, list):
                for v in node:
                    forward(v)

        forward(body)
        forward(body)

        elem: dict = {n: None for n in empties}

        def value_elem(val: object) -> str | None:
            """The element surface type a value would give an empty-vec binding."""
            if _v3_is_empty_list(val):
                return None
            if isinstance(val, dict):
                if val.get("kind") == "list" and val.get("items"):
                    return _v3_infer_type(val["items"][0], ctx)
                if val.get("kind") == "builtin" and val.get("method") in ("push", "concat", "slice"):
                    tgt = val.get("target")
                    if val.get("method") == "push" and val.get("args"):
                        t = _v3_infer_type(val["args"][0], ctx)
                        if t is not None:
                            return t
                    if isinstance(tgt, dict) and tgt.get("kind") == "var" and tgt.get("name") in elem:
                        return elem[tgt.get("name")]
                if val.get("kind") in ("var", "name", "req"):
                    nm = val.get("id") or val.get("name")
                    if nm in elem and elem[nm] is not None:
                        return elem[nm]
                    return _list_element_type(scratch.get(nm))
            return None

        def contributions(node: object, acc: list) -> None:
            if isinstance(node, dict):
                if node.get("kind") == "builtin" and node.get("method") == "push":
                    tgt = node.get("target")
                    if isinstance(tgt, dict) and tgt.get("kind") == "var" \
                            and tgt.get("name") in elem and node.get("args"):
                        acc.append((tgt.get("name"), _v3_infer_type(node["args"][0], ctx)))
                if node.get("step") in ("let", "assign") and node.get("name") in elem:
                    acc.append((node.get("name"), value_elem(node.get("value"))))
                for v in node.values():
                    contributions(v, acc)
            elif isinstance(node, list):
                for v in node:
                    contributions(v, acc)

        changed = True
        while changed:
            changed = False
            acc: list = []
            contributions(body, acc)
            for name, t in acc:
                if isinstance(t, str) and elem.get(name) is None:
                    elem[name] = t
                    changed = True
        return {n: t for n, t in elem.items() if isinstance(t, str)}
    finally:
        ctx.var_types = saved


def _v3_infer_type(node: object, ctx: "_V3Ctx") -> str | None:
    """The surface type of an expression when it is knowable, else None.

    Conservative: it names a type only when the node makes it certain — a
    variable alias, an ADT construction, a free-function/extern call's declared
    return, a list literal, or a `Str`/`Float`-certain node. The result seeds
    `var_types`, so a `let`-bound local (`let dec = decode(..)`) is typed the
    same way a parameter is, and a later by-value use of it can decide whether a
    clone is needed (`_by_value_arg`).
    """
    if isinstance(node, dict):
        kind = node.get("kind")
        if kind in ("name", "var", "req"):
            return ctx.var_types.get(node.get("id") or node.get("name"))
        if kind == "lit":
            # A bare `Int`/`Bool` literal is a Copy scalar, so typing it keeps a
            # reused loop counter (`var i = 0` pushed each iteration) from being
            # needlessly `.clone()`d. `bool` is checked first (it subclasses int).
            value = node.get("value")
            if isinstance(value, bool):
                return "Bool"
            if isinstance(value, int):
                return "Int"
        if kind == "adt":
            return ctx.case_adt.get(node.get("case"))
        if kind == "fn":
            return ctx.fn_returns.get(node.get("name"))
        if kind == "call" and "callee" in node:
            callee = node.get("callee") or {}
            if callee.get("kind") == "var":
                cn = callee.get("name")
                if cn in ctx.case_adt:
                    return ctx.case_adt.get(cn)
                if cn in ctx.fn_returns:
                    return ctx.fn_returns.get(cn)
        if kind == "list":
            return "List"
        if kind == "index":
            base_ty = _v3_infer_type(node.get("target"), ctx)
            if isinstance(base_ty, str):
                inner = _list_element_type(base_ty)
                if inner is not None:
                    return inner
        if kind == "field":
            base_ty = _v3_infer_type(node.get("target"), ctx)
            if isinstance(base_ty, str):
                ft = ctx.record_field_types(base_ty).get(node.get("name"))
                if isinstance(ft, str):
                    return ft
        if kind == "record":
            # Only when the field-set names EXACTLY ONE record; a field-set
            # several records share (`{e, i}` -> `PR`/`AtExpr`) is ambiguous, so
            # this must not guess a type and clobber a binding's more specific
            # existing type. `record_by_fields` is already `None` for a shared
            # field-set, so it is the unambiguous-only signal.
            key = tuple(sorted(k for k, _ in node.get("fields") or []))
            return ctx.record_by_fields.get(key)
        if kind == "match":
            # Every arm yields the same type, so the first arm body the emitter
            # can type names the `match`'s type. This lets a `let x = match ..`
            # bound value (e.g. a lookup-with-default) carry a type, so a later
            # by-value use of it -- including one consumed inside a loop -- clones
            # under the known-non-Copy rule instead of moving (E0382).
            for arm in node.get("arms") or []:
                arm_ty = _v3_infer_type(arm.get("body"), ctx)
                if isinstance(arm_ty, str):
                    return arm_ty
    if _v3_is_str(node, ctx):
        return "Str"
    if _v3_is_float(node):
        return "Float"
    return None


def _v3_is_float(node: object) -> bool:
    """Is this expression *syntactically* certain to be a `Float`? A Float
    literal, a `/` (true division), a Float-annotated arithmetic node, or a
    unary minus of one — the same node-local proof the other tiers use so a
    `${aFloat}` routes through the canonical renderer (docs/strings.md)."""
    if not isinstance(node, dict):
        return False
    kind = node.get("kind")
    if kind == "lit":
        value = node.get("value")
        return isinstance(value, float) and not isinstance(value, bool)
    if kind == "bin":
        return node.get("op") == "/" or node.get("operands") == "Float"
    if kind == "un":
        return node.get("op") == "-" and _v3_is_float(node.get("operand"))
    return False


class _V3Ctx:
    """Names visible to lowered IR v3 expression/statement emitters."""

    def __init__(self, types: dict, functions: list, externs: list,
                 components: list | None = None) -> None:
        self.types = types or {}
        self.function_names = {fn.get("name") for fn in functions or []}
        self.extern_names = {ext.get("name") for ext in externs or []}
        # Declared return type of every free function / extern, so a `let`
        # binding to a call can be typed (`let dec = decode(..)` -> `Reply`) and
        # a later by-value use knows to clone (see `_by_value_arg`).
        self.fn_returns: dict[str, str | None] = {
            fn.get("name"): fn.get("returns") for fn in functions or []
        }
        for ext in externs or []:
            self.fn_returns.setdefault(ext.get("name"), ext.get("returns"))
        # target component name -> whether it declares a typed config struct.
        # A `spawn` acquisition uses this to build the plug-time config value:
        # `<Comp>Config { .. }` when the template has config fields, else `()`.
        self.spawn_target_has_config: dict[str, bool] = {
            comp.get("name"): bool(comp.get("config"))
            for comp in components or []
        }
        # Binding -> surface type, seeded from declared signatures and from
        # `let` right-hand sides. Rust is the only tier that needs this: `+`
        # on strings has ownership rules (`String + &str` only), so the
        # renderer must know when a `+` is a concatenation. See `_v3_is_str`.
        self.var_types: dict[str, str | None] = {}
        # Declared return type of the fn currently being emitted, so a `return`
        # of an anonymous record literal has the target-type context that
        # disambiguates a non-unique field-set (item 268). Set per fn by
        # `_emit_v3_functions`; None everywhere the context is unknown.
        self.current_return: str | None = None
        # Bindings referenced more than once in the fn body currently being
        # emitted. A by-value use of one whose surface type is unknown must
        # clone (see `_by_value_arg`); reset per fn by `_emit_v3_functions`,
        # empty everywhere the reuse context is not established.
        self.multi_use: set[str] = set()
        # Local name -> element surface type for a binding introduced as an empty
        # `vec![]` (E0282 annotation); reset per fn by `_emit_v3_functions`, empty
        # in every other emit context (no empty-vec annotation needed there).
        self.vec_elems: dict = {}
        # Free function name -> set of parameter INDICES lowered to `&str`
        # (read-only `Str` params, item 282). Computed once from the whole
        # function list so a call site knows which argument slots take a borrow;
        # shared by every ctx (function bodies, tests, lifecycle tests).
        self.fn_borrow: dict[str, frozenset] = _compute_str_param_borrows(
            functions or [])
        # Parameters of the function CURRENTLY being emitted that are borrowed
        # `&str` (a subset of its `Str` params). Set per fn by
        # `_emit_v3_functions`, empty in every other emit context, so a read of a
        # borrowed param renders as the `&str` it already is.
        self.borrowed_params: set[str] = set()
        self.case_adt: dict[str, str | None] = {}
        self.case_payload: dict[str, str | None] = {}
        self.record_by_fields: dict[tuple[str, ...], str | None] = {}
        # Recursive-ADT edges that carry a `Box` indirection (E0072), keyed by
        # RAW `(enum, case)` / `(record, field)` — the same keys
        # `_recursive_boxed_edges` and `_emit_v3_types` use, so construction Box-
        # wraps exactly the edges the declaration boxed.
        self.boxed_cases, self.boxed_fields = _recursive_boxed_edges(self.types)
        for name, spec in self.types.items():
            _ident(name, "type name")
            if spec.get("kind") == "record":
                key = tuple(sorted((spec.get("fields") or {}).keys()))
                self.record_by_fields[key] = name if key not in self.record_by_fields else None
            elif spec.get("kind") == "variant":
                for case in spec.get("cases") or []:
                    cname = case.get("name")
                    payload = case.get("payload")
                    self.case_payload[cname] = payload
                    if cname in self.case_adt:
                        self.case_adt[cname] = None
                    else:
                        self.case_adt[cname] = name

    def field_is_boxed(self, type_ident: str, field: str) -> bool:
        """Whether the record whose EMITTED name is `type_ident` carries `field`
        behind a `Box` (a recursive struct-field edge, E0072). `boxed_fields` is
        keyed by the raw type name, so map the mangled ident back; empty in the
        common case (recursion is broken at an enum payload, needing no boxed
        field), so this is a cheap no-op then."""
        if not self.boxed_fields:
            return False
        for raw in self.types:
            if _ident(raw, "type name") == type_ident:
                return (raw, field) in self.boxed_fields
        return False

    def _records_with_fields(self, key: tuple[str, ...]) -> list[str]:
        """Every declared record whose field-set is EXACTLY `key`, sorted for a
        stable, name-independent choice."""
        return sorted(
            name
            for name, spec in self.types.items()
            if spec.get("kind") == "record"
            and tuple(sorted((spec.get("fields") or {}).keys())) == key
        )

    def _same_shape(self, names: list[str]) -> bool:
        """True when every named record in `names` declares the same field ->
        type mapping, so they are interchangeable and picking any one emits a
        correct struct for a literal that names none of them explicitly."""
        shapes = {
            tuple(sorted((self.types[n].get("fields") or {}).items()))
            for n in names
        }
        return len(shapes) == 1

    def _disambiguate_record(self, key: tuple[str, ...], expected: str | None,
                             what: str) -> str:
        """Resolve an anonymous record with field-set `key` to a named struct.

        Preference order (item 268): a declared target type whose field-set
        matches exactly (`return`/`let`/nested-field context); then a record
        that uniquely owns the field-set; then, when several records share it,
        a deterministic pick, but only when those records are structurally
        identical, so the choice cannot emit an ill-typed struct."""
        candidates = self._records_with_fields(key)
        if expected is not None and expected in candidates:
            return expected
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) >= 2 and self._same_shape(candidates):
            return candidates[0]
        if not candidates:
            raise EmitError(
                f"cannot infer Rust struct type for record {what} with fields "
                f"{list(key)!r}: no record declares exactly those fields"
            )
        raise EmitError(
            f"cannot infer Rust struct type for record {what} with fields "
            f"{list(key)!r}: {candidates!r} share them and differ in shape; "
            f"annotate the target type"
        )

    def record_type_for_fields(self, fields: list[str],
                               expected: str | None = None) -> str:
        key = tuple(sorted(fields))
        return _ident(self._disambiguate_record(key, expected, "literal"),
                      "type name")

    def record_field_types(self, type_name: str) -> dict[str, str | None]:
        """The declared field -> type map of an emitted record type, so a field
        VALUE can be lowered with its target type as context. Empty for a
        synthesized/unknown name."""
        spec = self.types.get(type_name)
        if spec is None or spec.get("kind") != "record":
            return {}
        return dict(spec.get("fields") or {})

    def record_type_for_names(self, names: list[str],
                              expected: str | None = None) -> str:
        # Destructuring names a (possibly proper) subset of the record's fields,
        # so this stays subset-based; but when several records admit the subset
        # the same target-type context and structural-identity tie-breakers the
        # literal side uses resolve it, so the two agree on one name (item 268).
        wanted = set(names)
        candidates = sorted(
            name
            for name, spec in self.types.items()
            if spec.get("kind") == "record" and wanted <= set(spec.get("fields") or {})
        )
        if expected is not None and expected in candidates:
            return _ident(expected, "type name")
        if len(candidates) == 1:
            return _ident(candidates[0], "type name")
        if len(candidates) >= 2 and self._same_shape(candidates):
            return _ident(candidates[0], "type name")
        raise EmitError(
            f"cannot infer Rust struct type for record destructuring {names!r}"
        )

    def constructor(self, name: str, args: list[str]) -> str:
        adt = self.case_adt.get(name)
        if adt is not None:
            payload = self.case_payload.get(name)
            if payload is None:
                if args:
                    raise EmitError(f"`{name}` takes no payload")
                return f"{adt}::{name}"
            if len(args) != 1:
                raise EmitError(f"`{name}` takes exactly one payload argument")
            arg = args[0]
            if (adt, name) in self.boxed_cases:
                # recursive payload -> the variant holds `Box<Payload>` (E0072);
                # move the constructed payload onto the heap to match.
                arg = f"Box::new({arg})"
            return f"{adt}::{name}({arg})"
        if name == "None":
            if args:
                raise EmitError("`None` takes no arguments")
            return "None"
        if name in ("Some", "Ok", "Err"):
            if len(args) != 1:
                raise EmitError(f"`{name}` takes exactly one argument")
            return f"{name}({args[0]})"
        if name in self.case_adt:
            raise EmitError(f"ambiguous ADT constructor {name!r}")
        raise EmitError(f"unknown ADT constructor {name!r}")

    def match_pattern(self, arm: dict) -> str:
        pattern = arm.get("pattern")
        if pattern == "_":
            return "_"
        bind = arm.get("bind")
        adt = self.case_adt.get(pattern)
        if adt is not None:
            prefix = f"{adt}::{pattern}"
        elif pattern in _V3_BUILTIN_CONSTRUCTORS:
            prefix = pattern
        elif pattern in self.case_adt:
            raise EmitError(f"ambiguous match case {pattern!r}")
        else:
            raise EmitError(f"unknown match case {pattern!r}")
        if bind:
            return f"{prefix}({_ident(bind, 'match bind')})"
        return prefix



def _render_expr(node: dict, ctx: _V3Ctx, rename: dict[str, str] | None = None,
                 expected: str | None = None) -> str:
    """Lower one IR expression node to a Rust expression.

    This is the *single* expression renderer for the Rust tier. It covers both
    IR dialects a document can mix:

    * the component ("v1") dialect — ``req``/``config``/``host``/``format``/
      ``fn`` and the component-shaped ``call`` (``target``/``method``); and
    * the 2.0 dialect — ``callee``/``args`` calls, ``match``, ``arrow``, ADTs,
      the stdlib ``builtin`` surface, string ``+`` via ``format!``, …

    Where a kind exists in *both* dialects the renderer dispatches on **shape**
    (which distinguishing keys are present), never on kind alone — see ``call``,
    which reads ``callee`` for the 2.0 form and ``target``/``method`` for the
    component form. ``rename`` is the component-capture map (``pool`` ->
    ``self.pool``); a 2.0 body passes no rename, so the map is simply empty
    there. String literals lower to owned ``String`` in every position (revl
    ``Str`` is ``String``), so no dialect-specific literal coercion is needed.
    """
    if not isinstance(node, dict) or "kind" not in node:
        raise EmitError(f"malformed expression: {node!r}")
    # An implicit Int -> Float coercion site (docs/arithmetic.md). This tier
    # is the one that *refused* `ident(3)` with E0308 before the marker
    # existed — the widening was real but invisible; now it is emitted.
    if node.get("widen") == "Float":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"({_render_expr(inner, ctx, rename)} as f64)"
    # An Int32 -> Int widening site (docs/arithmetic.md): i32 does not coerce to
    # i64 in Rust (E0308), so the lossless widening is written where the
    # frontend marked it, exactly as the Float case is.
    if node.get("widen") == "Int":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"({_render_expr(inner, ctx, rename)} as i64)"
    rename = rename or {}
    kind = node["kind"]

    if kind == "lit":
        return _rust_v3_lit(node)

    if kind == "name":
        # A component capture (`self.pool`) arrives via the rename map already
        # rendered as a Rust expression; otherwise it is a plain identifier.
        original = node.get("id")
        if rename and original in rename:
            return rename[original]
        return _ident(original, "name")

    if kind == "var":
        name = node.get("name")
        # a keyword-named local/case is renamed at its *use* the same way
        # `_ident` renamed it at its declaration (item 165); the built-in
        # constructors (`Ok`/`Some`/…) are native Rust and never keywords
        mangled = _ident(name, "name")
        if name in ctx.case_adt:
            adt = ctx.case_adt.get(name)
            if adt is not None:
                return f"{adt}::{mangled}"
            raise EmitError(f"ambiguous ADT case name {name!r}")
        if name in _V3_BUILTIN_CONSTRUCTORS:
            return name
        return mangled

    if kind == "req":
        # component dialect: a required capability, possibly a `self.`-capture.
        original = node.get("name")
        if rename and original in rename:
            return rename[original]
        return _ident(original, "requirement")

    if kind == "config":
        # Component guards (`if (config.n < 1) { fail .. }`) reach this renderer
        # through the surrounding `bin`/`un` node, and `_method_body`/setup put a
        # local `let config = <Comp>Config { .. }` in scope, so config reads the
        # same way whether it arrives via the component or the 2.0 path. Inside a
        # provide-method body there is no local `config`, so the method rename map
        # points the base at the captured struct field (`self.config`).
        base = (rename.get("config") if rename else None) or "config"
        return f"{base}.{_ident(node.get('field'), 'config field')}.clone()"

    if kind == "host":
        # component dialect: `Pool.open(..)` -> `Pool::open(..)`.
        fn = node.get("fn")  # e.g. "Pool.open"
        host, _, method = fn.partition(".")
        rendered = [_render_expr(a, ctx, rename) for a in node.get("args") or []]
        return f"{host}::{_mname(method)}({', '.join(rendered)})"

    if kind == "subscribe":
        # item 130 Slice 3 (design §4.6, the rust row): this tier ERASES the
        # async color — `next` blocks on the item/terminal/CANCEL race and
        # `close` trips the cancel signal, so the bracket inverse is reachable
        # off the teardown thread even while a `next` is parked.
        _refuse_unlowered_stream_surface(node, "cordis-rs")
        policy = node.get("policy") or "error"
        capacity = int(node.get("buffer") or 0)
        stream = _stream_head(node.get("stream") or {}, ctx, rename)
        return (f"Stream::subscribe(&{stream}, {_string(policy)}, "
                f"{capacity}usize)")

    if kind == "format":
        # component dialect: `$0`/`$1` template -> Rust `format!`.
        template = node.get("template") or ""
        args = [_render_expr(a, ctx, rename) for a in node.get("args") or []]
        return _format(template, args)

    if kind == "fn":
        # component dialect: a free-function call `name(..)`.
        name = _ident(node.get("name"), "function")
        borrow = ctx.fn_borrow.get(node.get("name"), frozenset())
        args = ", ".join(
            _borrow_str_arg(a, _render_expr(a, ctx, rename), ctx) if idx in borrow
            else _by_value_arg(a, _render_expr(a, ctx, rename), ctx)
            for idx, a in enumerate(node.get("args") or [])
        )
        return f"{name}({args})"

    if kind == "adt":
        # tagged ADT construction: user variants -> `Enum::Case(..)`, built-in
        # Result -> native `Ok(..)`/`Err(..)`. Reuses the constructor logic
        # the call/var paths already use.
        adt_args = node.get("args") or []
        return ctx.constructor(
            node["case"],
            [_by_value_arg(a, _render_expr(a, ctx, rename), ctx) for a in adt_args]
        )

    if kind == "bin":
        if node.get("op") == "??":
            # `a ?? b` (docs/syntax-2.0.md §3.2): `Opt[T]` is `Option<T>` on
            # this tier, so the absent case is `None`. `unwrap_or_else` keeps
            # `b` lazy — `??` must not evaluate its right operand when `a` is
            # present, and `unwrap_or` would evaluate it unconditionally. This
            # is the sole `??` lowering; both dialects funnel through here now.
            left = _render_expr(node["left"], ctx, rename)
            if node["left"].get("kind") not in _ATOMIC_KINDS:
                left = f"({left})"
            return f"{left}.unwrap_or_else(|| {_render_expr(node['right'], ctx, rename)})"
        if node.get("op") == "+" and (
                _v3_is_str(node.get("left"), ctx) or _v3_is_str(node.get("right"), ctx)):
            # Rust's `+` on strings takes `String + &str` only: `&str + String`
            # and `String + String` are both errors, and which one a revl `+`
            # becomes depends on where each side came from. `format!` accepts
            # every combination and always yields `String`, which is what `Str`
            # lowers to. (Both spellings shipped broken until `cargo check` ran
            # over the emitted code — docs/conformance.md.)
            return (f'format!("{{}}{{}}", {_render_expr(node["left"], ctx, rename)}, '
                    f'{_render_expr(node["right"], ctx, rename)})')
        if node.get("op") in ("&", "|", "^", "<<", ">>"):
            # Int32 bitwise operators (item 366, docs/arithmetic.md). `& | ^`
            # are native on i32 and never trap. Shifts mask the count to 0..31
            # (mod 32): rust panics on a shift amount >= 32, so masking keeps
            # `<<`/`>>` panic-free, and it matches wasm/JS. `<<` drops the high
            # bits (i32 two's complement); `>>` on the signed i32 is arithmetic.
            left = _render_expr(node["left"], ctx, rename)
            right = _render_expr(node["right"], ctx, rename)
            if node["op"] in ("&", "|", "^"):
                return f"({left} {node['op']} {right})"
            return f"({left} {node['op']} (({right}) & 31))"
        op = _V3_BIN_OPS.get(node.get("op"))
        if op is None:
            raise EmitError(f"unsupported binary operator {node.get('op')!r}")
        left = _render_expr(node["left"], ctx, rename)
        right = _render_expr(node["right"], ctx, rename)
        if node.get("op") in ("+", "-", "*") and node.get("operands") == "Int":
            # Int is bounded 64-bit and overflow TRAPS (docs/arithmetic.md):
            # silent wraparound is the failure mode revl exists to remove, and
            # rust only checks in debug builds by default.
            m = {"+": "checked_add", "-": "checked_sub", "*": "checked_mul"}[node["op"]]
            return (f'({left}).{m}({right}).expect("revl: Int overflow")')
        if node.get("op") in ("+", "-", "*") and node.get("operands") == "Int32":
            # Int32 traps at the i32 edge; rust's own `*`/`+`/`-` only check in
            # debug, so the checked forms make it release-safe too
            # (docs/arithmetic.md).
            m = {"+": "checked_add", "-": "checked_sub", "*": "checked_mul"}[node["op"]]
            return (f'({left}).{m}({right}).expect("revl: Int32 overflow")')
        if node.get("op") == "/" and node.get("operands") in ("Int", "Int32"):
            # `/` is true division and yields Float (docs/arithmetic.md), but
            # rust `/` on two i64 is integer division — it would compute 3 for
            # `7 / 2` and then fail to typecheck against the f64 the checker
            # declared. Widen both sides; IEEE division on f64 follows, so a
            # zero divisor gives +/-inf rather than the i64 panic.
            return f"(({left}) as f64 / ({right}) as f64)"
        return f"({left} {op} {right})"

    if kind == "un":
        operand = _render_expr(node.get("operand"), ctx, rename)
        if node.get("op") == "!":
            return f"(!{operand})"
        if node.get("op") == "~":
            # Int32 bitwise complement (item 366): rust spells bitwise NOT on
            # an integer as `!` (the same token it uses for logical not on
            # bool). A bit op, so it never traps.
            return f"(!{operand})"
        if node.get("op") == "-":
            return f"(-{operand})"
        raise EmitError(f"unsupported unary operator {node.get('op')!r}")

    if kind == "call":
        # Shape dispatch: the 2.0 form carries `callee`; the component form
        # carries `target`/`method`. Reading the wrong child would silently emit
        # the wrong receiver, so the presence of `callee` — not the kind — is
        # what selects the form.
        if "callee" in node:
            callee_node = node.get("callee") or {}
            arg_nodes = node.get("args") or []
            arg_exprs = [_render_expr(a, ctx, rename) for a in arg_nodes]
            args = ", ".join(arg_exprs)
            # A method/host call MOVES each by-value argument into the callee, so
            # a reused non-Copy argument (`amb.push(cn)` then `ct.remove(&cn)`,
            # `surf.insert(e.parent, ..)` then a later `e.parent`) is cloned under
            # the reuse rule; a borrowed key (`get`/`remove`) keeps its `&`.
            reuse_args = [
                _by_value_reuse(a, r, ctx) for a, r in zip(arg_nodes, arg_exprs)
            ]
            if callee_node.get("kind") == "field":
                target_node = callee_node.get("target") or {}
                method = callee_node.get("name")
                if target_node.get("kind") == "var" and target_node.get("name") in _V3_HOST_ROOTS:
                    hargs = list(reuse_args)
                    if target_node["name"] == "Map" and method in ("get", "remove") and arg_exprs:
                        # FR-4: the host Map borrows its key, so the caller keeps
                        # owning it (read-then-write on one key compiles).
                        hargs = [f"&{arg_exprs[0]}", *reuse_args[1:]]
                    return f"{target_node['name']}::{_mname(method)}({', '.join(hargs)})"
                target = _render_expr(target_node, ctx, rename)
                if target_node.get("kind") not in _ATOMIC_KINDS:
                    target = f"({target})"
                margs = reuse_args
                if (target_node.get("kind") == "var"
                        and method in ("get", "remove")
                        and str(ctx.var_types.get(target_node.get("name") or "") or "")
                            .startswith("Map[")):
                    # FR-4: same borrow for a host-Map binding read through a
                    # 2.0-style callee (`store.get(k)` in a pure fn body).
                    margs = ([f"&{arg_exprs[0]}", *reuse_args[1:]] if arg_exprs else [])
                return f"{target}.{_ident(method, 'method')}({', '.join(margs)})"
            callee_name = callee_node.get("name") if callee_node.get("kind") == "var" else None
            if callee_name is not None and (
                callee_name in ctx.case_adt or callee_name in _V3_BUILTIN_CONSTRUCTORS
            ):
                # An ADT/Some/Ok payload MOVES its value in (an owned slot like a
                # record field), so a non-Copy payload is cloned via `_by_value_arg`.
                return ctx.constructor(callee_name, [
                    _by_value_arg(a, r, ctx)
                    for a, r in zip(arg_nodes, arg_exprs)])
            callee = _render_expr(callee_node, ctx, rename)
            if callee_node.get("kind") not in _ATOMIC_KINDS:
                callee = f"({callee})"
            # A free-function / function-value call passes its arguments by value
            # (revl value semantics), so a non-Copy value or `impl Fn` argument
            # reused after the call would otherwise move (E0382). A read-only
            # `Str` param the callee lowered to `&str` takes a borrow instead of
            # a clone (item 282), so its whole string is never copied at the call.
            borrow = ctx.fn_borrow.get(callee_name, frozenset())
            bv_args = ", ".join(
                _borrow_str_arg(a, r, ctx) if idx in borrow
                else _by_value_arg(a, r, ctx)
                for idx, (a, r) in enumerate(zip(node.get("args") or [], arg_exprs))
            )
            return f"{callee}({bv_args})"
        # component form: `target.method(args)`.
        target = node.get("target") or {}
        method = _ident(_mname(node.get("method")), "method")
        arg_nodes = node.get("args") or []
        arg_exprs = [_render_expr(a, ctx, rename) for a in arg_nodes]
        recv_ty = str(
            ctx.var_types.get(target.get("id") or target.get("name") or "") or "")
        is_host_map = recv_ty.startswith("Map[")
        if (is_host_map and node.get("method") in ("get", "remove")):
            # FR-4: the host Map borrows its key (revl `Str` keys), so the
            # caller keeps owning it — a component that reads then writes the
            # same key (the session ledger) compiles without a clone.
            if arg_exprs:
                arg_exprs[0] = f"&{arg_exprs[0]}"
            args = ", ".join(arg_exprs)
        elif is_host_map:
            # Other host-Map methods (`insert`, ...) are calls on the
            # first-party generic `Map<V>`; the value is MOVED in, so a reused
            # non-Copy argument still clones under the reuse rule.
            args = ", ".join(
                _by_value_reuse(a, r, ctx) for a, r in zip(arg_nodes, arg_exprs))
        else:
            # A service-method call passes its arguments by value (revl value
            # semantics): the generated trait methods take `String`/records/ADTs
            # by value, so a non-Copy variable reused after the call would move
            # (E0382) — the item-93 argument clone, extended to service-call
            # arguments (roadmap item 101). Copy scalars and fresh temporaries
            # are untouched.
            args = ", ".join(
                _by_value_arg(a, r, ctx) for a, r in zip(arg_nodes, arg_exprs)
            )
        if target.get("kind") == "req":
            recv = _ident(target.get("name"), "requirement")
            if rename and target.get("name") in rename:
                recv = rename[target.get("name")]
        else:
            recv = _render_expr(target, ctx, rename)
        return f"{recv}.{method}({args})"

    if kind == "field":
        target_node = node.get("target")
        target = _render_expr(target_node, ctx, rename)
        if target_node.get("kind") not in _ATOMIC_KINDS:
            target = f"({target})"
        if node.get("sized_length"):
            # item 104 (cross-tier): property-form `.length` on a sized value in
            # a component position — the code-point (Str) / element (List) count
            # via the helper trait (`String::len` is bytes), the same as the
            # `len` node, NOT a struct field access.
            return f"{target}.revl_length()"
        return f"{target}.{_ident(node.get('name'), 'field')}"

    if kind == "index":
        target_node = node.get("target")
        target = _render_expr(target_node, ctx, rename)
        if target_node.get("kind") not in _ATOMIC_KINDS:
            target = f"({target})"
        # revl Int is i64; Rust indexing wants usize.
        return f"({target})[({_render_expr(node['index'], ctx, rename)}) as usize].clone()"

    if kind == "if":
        # Each branch tail is a value position: a bare non-Copy binding reused
        # after the `if`-expression must clone here or the later read borrows a
        # moved value (E0382). A fresh temporary or a Copy scalar stays bare.
        then_v = _by_value_tail(
            node["then"], _render_expr(node["then"], ctx, rename), ctx)
        else_v = _by_value_tail(
            node["else"], _render_expr(node["else"], ctx, rename), ctx)
        return f"if {_render_expr(node['cond'], ctx, rename)} {{ {then_v} }} else {{ {else_v} }}"

    if kind == "record_update":
        raise EmitError(
            "functional record update `{r | f = e}` is not emitted by the rust "
            "backend yet (implemented tiers: python, typescript) — see "
            "docs/records.md §6; lift it into a helper fn instead")

    if kind == "record":
        fields = node.get("fields") or []
        type_name = ctx.record_type_for_fields([k for k, _ in fields], expected)
        # The struct's declared field types, so a field VALUE that is itself an
        # anonymous record with an ambiguous field-set resolves to the type this
        # struct declares for it (`expected`), the same target-type context a
        # `return`/`let` supplies at the top level (item 268).
        field_types = ctx.record_field_types(type_name)
        # A record construction moves each field value into the struct. A
        # non-Copy bare variable used as a field value would therefore be
        # consumed here (E0382) if the caller still needs it afterward — e.g.
        # `sessions.append(id, Msg { content: answer })` then `return answer`
        # (roadmap item 101). `_by_value_arg` clones the reused non-Copy value
        # (sound: revl values are immutable), leaving Copy scalars and fresh
        # temporaries untouched. A fn-typed field is impossible here — it is an
        # escaping position the type layer already refuses (`_FN_TYPE_REFUSAL`).
        def _field_value(k, v):
            rendered = _by_value_arg(
                v, _render_expr(v, ctx, rename, field_types.get(k)), ctx)
            if ctx.field_is_boxed(type_name, _ident(k, "record field")):
                # recursive struct field -> the struct holds `Box<T>` (E0072);
                # move the field value onto the heap to match.
                rendered = f"Box::new({rendered})"
            return rendered

        body = ", ".join(
            f"{_ident(k, 'record field')}: {_field_value(k, v)}"
            for k, v in fields
        )
        return f"{type_name} {{ {body} }}"

    if kind == "list":
        # A list literal MOVES each element into the `Vec`, so a reused non-Copy
        # element (`vec![root_]` with `root_` read again) must clone; a Copy or
        # single-use element goes through untouched.
        return ("vec![" + ", ".join(
            _by_value_reuse(item, _render_expr(item, ctx, rename), ctx)
            for item in node.get("items") or []) + "]")

    if kind == "maplit":
        # `Map.empty()` (docs/stdlib-2.0.md §Map). rustc infers the map's
        # parameters from later use, so an empty literal is context-free.
        return "std::collections::HashMap::new()"

    if kind == "arrow":
        params = ", ".join(_ident(p, "arrow parameter") for p in node.get("params") or [])
        return f"move |{params}| {{ {_render_expr(node['body'], ctx, rename)} }}"

    if kind == "len":
        target = _render_expr(node.get("target"), ctx, rename)
        # Via the helper trait: String::len is bytes, revl length is elements.
        return f"{target}.revl_length()"

    if kind == "builtin":
        target_node = node.get("target")
        target = _render_expr(target_node, ctx, rename)
        if target_node.get("kind") not in _ATOMIC_KINDS:
            target = f"({target})"
        arg_nodes = node.get("args") or []
        args = [_render_expr(a, ctx, rename) for a in arg_nodes]
        # `push`/`set` MOVE their non-Copy argument into the collection, so a
        # reused value must clone (`stack.revl_push(name)` then `name` again).
        # The borrow-arg builtins (`concat`/`indexOf`/... and the Map key
        # probes) take `&arg`, so they never move it and are left untouched.
        method = node.get("method")
        if method not in _BORROW_ARG_BUILTINS:
            args = [
                _by_value_reuse(a, r, ctx) for a, r in zip(arg_nodes, args)]
        elif method == "indexOf" and arg_nodes:
            # A List `revl_index_of` needle is `&T` (`&String` for `List[Str]`),
            # not `&str`. Item 282 may have borrowed a read-only `Str` param to
            # `&str` (safe for the *string* indexOf), but as a List needle that
            # renders `&&str` (E0308), so a borrowed `&str` arg is materialised to
            # an owned `String` here (`&s.to_string()` -> `&String`).
            recv_ty = _v3_infer_type(target_node, ctx)
            a0 = arg_nodes[0]
            if (isinstance(recv_ty, str) and recv_ty.startswith("List[")
                    and isinstance(a0, dict) and a0.get("kind") in ("var", "name", "req")
                    and (a0.get("id") or a0.get("name")) in ctx.borrowed_params):
                args[0] = f"{args[0]}.to_string()"
        return _v3_builtin(method, target, args, node.get("recv"))

    if kind == "match":
        return _v3_match_expr(node, ctx, rename)

    if kind == "interp":
        return _v3_interp(node, ctx, rename)

    if kind in ("optfield", "optcall"):
        raise EmitError(
            f"optional chaining (`?.`) is not yet lowerable on the Rust tier "
            f"({kind!r}); unwrap with `match` or `??` for now"
        )

    if kind == "spawn":
        return _v3_spawn(node, ctx, rename)

    if kind == "instance-get":
        return _v3_instance_get(node, ctx, rename)

    raise EmitError(f"unsupported expression kind {kind!r} in Rust backend")


def _v3_spawn(node: dict, ctx: _V3Ctx, rename: dict[str, str]) -> str:
    """Lower an instance-parametric `spawn` acquisition (docs/design-v2-instances.md).

    `spawn` is the acquisition of a `let-effect` step, so this renders the
    expression the enclosing step wraps in `Arc::new(..)` and binds to the
    handle. It plugs the target *template* as a CHILD FIBER of the spawner —
    each key the template provides isolated into a FRESH LOCAL realm
    (`Context::isolate`, no label -> a distinct `Isolation` per spawn, so two
    instances never collide) — and returns a `RevlSpawnHandle` wrapping that
    fiber. cordis-rs registers the child fiber as a `ctx.plugin()` effect on
    the spawner's frame (registry.rs:501-512), so it is the spawner's own
    nested teardown scope: the safety net that stops an un-disposed instance
    outliving its spawner, while `handle.dispose()` reclaims it earlier.
    """
    target = node.get("component")
    if not isinstance(target, str) or not target.isidentifier():
        raise EmitError(f"bad spawn component {target!r}")
    ctx_expr = rename.get("ctx", "ctx")
    # Each provided key -> its own fresh local realm, applied at plug time (the
    # child fiber's Inject gate resolves against this isolated context, exactly
    # as `_revl_isolate_ctx` does for a statically composed realm placement).
    isolate_chain = ctx_expr
    for key in node.get("realms") or []:
        isolate_chain += f".isolate({_string(key)})"
    if ctx.spawn_target_has_config.get(target):
        cfg_ty = f"{_ident(target, 'component')}Config"
        fields = "".join(
            f"{_ident(k, 'config field')}: {_render_expr(v, ctx, rename)}, "
            for k, v in (node.get("config") or {}).items()
        )
        cfg_expr = f"{cfg_ty} {{ {fields}..Default::default() }}"
    else:
        cfg_expr = "()"
    plug_fn = _snake(target)
    return (
        "{ let __revl_sctx = " + isolate_chain + "; "
        f"let __revl_fiber = __revl_sctx.plugin({plug_fn}(), {cfg_expr}); "
        "RevlSpawnHandle::new(__revl_fiber, __revl_sctx) }"
    )


def _v3_instance_get(node: dict, ctx: _V3Ctx, rename: dict[str, str]) -> str:
    """Lower the instance accessor `s.<key>` (docs/design-v2-instances.md).

    `s : Instance[C]` is a name bound to a `spawn` handle — the emitted
    `RevlSpawnHandle`, which stored the child's own isolated context (the same
    LOCAL realm the matching `spawn` isolated the provided key into). Reading
    `s.<key>` resolves `key` through THAT context, yielding this instance's
    provision and no other's: `RevlSpawnHandle::get` delegates to
    `Context::get_unchecked` (cordis-rs-0.3.0/src/context.rs:358), the
    realm-scoped read. Provisions are stored as `Box<dyn Service>` (the same
    boxing `ctx.provide`/`ctx.require` use), so the type argument mirrors a
    `require`, and the returned `Arc<Box<dyn Service>>` is directly method-
    callable — `s.<key>.method(..)` is the enclosing `call`/`field` node.

    Supervision-tree addressing holds because only the handle holder reaches
    that context: a sibling isolated into a different local realm, and the
    root, resolve `None` (the negative the scenario proves). `service` is the
    frozen inline typing result, so no re-derivation here. The two `expect`s
    are the crash-only contract for a read the frontend already proved sound —
    `key` is a provision of `C` (else a compile error, never emitted) and the
    instance is live when its spawner reads it."""
    handle = _render_expr(node.get("target"), ctx, rename)
    service = node.get("service")
    if not isinstance(service, str) or not service:
        raise EmitError(f"instance-get: bad frozen service type {service!r}")
    key = node.get("key")
    if not isinstance(key, str) or not key.isidentifier():
        raise EmitError(f"instance-get: bad key {key!r}")
    return (
        f"{handle}.get::<Box<dyn {service}>>({_string(key)})"
        '.expect("revl: instance-get resolution failed")'
        '.expect("revl: instance provision absent")'
    )


# The total, value-returning division forms (docs/arithmetic.md): same
# rounding as the faulting operations, Err(reason) at a zero divisor.
_CHECKED_DIVS = ("checked_div_trunc", "checked_div_floor",
                 "checked_div_euclid", "checked_mod")
_DIV_ZERO_MSG = "revl: division by zero"


def _v3_checked_div(method: str, target: str, arg: str) -> str:
    """The total forms (docs/arithmetic.md): same quotient as the faulting
    operation, but a zero divisor yields Err(reason) instead of panicking —
    `fail` is refused in a pure fn, so the error travels as a value. The
    turbofish pins Result<i64, String> so the expression is context-free;
    operands are bound once, since a block evaluates them exactly once."""
    ok = {
        "checked_div_trunc": "a / b",
        "checked_div_floor":
            "{ let q = a / b; if a % b != 0 && ((a < 0) != (b < 0)) { q - 1 } else { q } }",
        "checked_div_euclid": "a.div_euclid(b)",
        "checked_mod": "a.rem_euclid(b).abs()",
    }[method]
    return (f"{{ let a = ({target}); let b = ({arg}); "
            f'if b == 0 {{ Err::<i64, String>("{_DIV_ZERO_MSG}".to_string()) }} '
            f'else {{ Ok::<i64, String>({ok}) }} }}')


def _v3_builtin(method: str, target: str, args: list[str],
                recv: str | None = None) -> str:
    """The stdlib surface (docs/stdlib-2.0.md), dispatched via the Revl*Ops
    helper traits so every (method, Str|List) pair from the spec table
    compiles — Rust resolves the receiver type statically. `recv` carries
    the receiver's static type only where the lowering must dispatch on it
    (`to_int`: the Int32 widen vs the Str parse)."""
    if method == "length":
        return f"{target}.revl_length()"
    if method == "push":
        return f"{target}.revl_push({args[0]})"
    if method == "concat":
        return f"{target}.revl_concat(&{args[0]})"
    if method == "slice":
        return f"{target}.revl_slice({args[0]}, {args[1]})"
    if method == "charAt":
        return f"{{ {target}.chars().nth(({args[0]}) as usize).unwrap().to_string() }}"
    if method == "charCodeAt":
        return f"{{ {target}.chars().nth(({args[0]}) as usize).unwrap() as u32 as i64 }}"
    # Codepoint-at-index scan (item 276, docs/stdlib-2.0.md §Str.codepoint_at):
    # the Unicode scalar at code-point index i. Same char-indexed lowering as
    # charCodeAt on this tier (the O(n)-per-access cost is item 277/282's
    # separate concern — byte-identical to charCodeAt's shape here).
    if method == "codepoint_at":
        return f"{{ {target}.chars().nth(({args[0]}) as usize).unwrap() as u32 as i64 }}"
    if method == "indexOf":
        return f"{target}.revl_index_of(&{args[0]})"
    if method == "split":
        return f"{target}.revl_split(&{args[0]})"
    if method == "join":
        return f"{target}.revl_join(&{args[0]})"
    if method == "repeat":
        return f"{target}.revl_repeat({args[0]})"
    # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith):
    # `str::starts_with`/`ends_with` match on char-boundary patterns, so a
    # code-point prefix of a UTF-8 string is exactly a prefix here.
    if method == "startsWith":
        return f"{target}.revl_starts_with(&{args[0]})"
    if method == "endsWith":
        return f"{target}.revl_ends_with(&{args[0]})"
    # The Map value type (docs/stdlib-2.0.md §Map): a std HashMap, cloned on
    # write. Every revl value type derives Clone on this tier, so the copy
    # is total; `lookup` answers Option<V> (the tier's Opt) via cloned().
    if method == "set":
        return f"{{ let mut c = {target}.clone(); c.insert({args[0]}, {args[1]}); c }}"
    if method == "lookup":
        return f"{target}.get(&{args[0]}).cloned()"
    if method == "has":
        return f"{target}.contains_key(&{args[0]})"
    # The iteration/remove step (docs/stdlib-2.0.md §Map). String: Ord is
    # UTF-8 byte order — the canonical Str order on this tier. keys collects
    # into a Vec<String> (the List[Str] representation) and sorts a copy;
    # remove clones before deleting, so the receiver never mutates.
    if method == "size":
        return f"({target}.len() as i64)"
    if method == "keys":
        return (f"{{ let mut ks: std::vec::Vec<String> = "
                f"{target}.keys().cloned().collect(); ks.sort(); ks }}")
    if method == "remove":
        return f"{{ let mut c = {target}.clone(); c.remove(&{args[0]}); c }}"
    # Integer division and modulo (docs/arithmetic.md). Rust `/` already
    # truncates and carries div_euclid/rem_euclid in std (stable since 1.38);
    # div_floor is spelled out rather than using the much newer
    # `i64::div_floor`, so the emitted crate does not need a recent toolchain.
    # Int/Int32 width conversions (docs/arithmetic.md). Widening Int32 -> Int is
    # a lossless `as i64`; narrowing Int -> Int32 goes through `i32::try_from`,
    # which returns Err out of range — `.expect` turns that into the trap.
    # `to_int` is ALSO the Str parse (FR-9, docs/stdlib-2.0.md §Str.to_int):
    # `str::parse::<i64>` is total on the ASCII digits (leading `-` allowed)
    # and answers `Err` for empty/partial/`+` spellings AND out-of-i64-range
    # values, so `.ok()` is exactly the tier's `Opt[Int]`.
    if method == "to_int":
        if recv == "Str":
            return f"{{ ({target}).parse::<i64>().ok() }}"
        return f"(({target}) as i64)"
    if method == "to_int32":
        return f'(i32::try_from({target}).expect("revl: Int32 overflow"))'
    if method == "div_trunc":
        return f"(({target}) / ({args[0]}))"
    if method == "div_floor":
        return (f"{{ let (a, b) = (({target}), ({args[0]})); let q = a / b; "
                f"if a % b != 0 && ((a < 0) != (b < 0)) {{ q - 1 }} else {{ q }} }}")
    if method == "div_euclid":
        return f"(({target}).div_euclid({args[0]}))"
    if method == "mod":
        return f"(({target}).rem_euclid({args[0]}).abs())"
    # The total forms (docs/arithmetic.md): same quotient as the faulting
    # operation, but a zero divisor yields Err(reason) instead of panicking —
    # `fail` is refused in a pure fn, so the error travels as a value. The
    # turbofish pins Result<i64, String> so the expression is context-free;
    # operands are bound once, since a block evaluates them exactly once.
    if method in _CHECKED_DIVS:
        return _v3_checked_div(method, target, args[0])
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str): i64::to_string
    # is exact decimal over the whole range, Int.MIN included, and String is
    # this tier's Str.
    if method == "to_str":
        return f"({target}).to_string()"
    # Single-character ASCII classification (item 233, docs/stdlib-2.0.md
    # §Str.is_alnum), mirroring the python backend's native forms
    # (backends/python/emit.py §is_digit/is_alpha/is_alnum/is_space). The
    # receiver is a `String` on this tier, so a `&str` view bound once (`_rc`)
    # drives ASCII code-point-order comparison: byte order on `str` IS
    # code-point order for ASCII, matching python's chained string comparison.
    # It stays total exactly as python's does: an empty receiver compares less
    # than `"0"`, so it is `false` rather than a fault, and multi-character
    # input (outside the per-character contract) never panics. Binding once is
    # correct even when the receiver has side effects, with no closure overhead.
    if method == "is_digit":
        return f'{{ let _rc: &str = &{target}; "0" <= _rc && _rc <= "9" }}'
    if method == "is_alpha":
        return (f'{{ let _rc: &str = &{target}; '
                f'("a" <= _rc && _rc <= "z") || ("A" <= _rc && _rc <= "Z") }}')
    if method == "is_alnum":
        return (f'{{ let _rc: &str = &{target}; '
                f'("0" <= _rc && _rc <= "9") || ("a" <= _rc && _rc <= "z") '
                f'|| ("A" <= _rc && _rc <= "Z") }}')
    # is_space: space, tab, LF, CR, equality with each element (python uses
    # tuple membership; a `str::contains` would wrongly match the empty
    # receiver, which is a substring of every string).
    if method == "is_space":
        return (f'{{ let _rc: &str = &{target}; '
                f'_rc == " " || _rc == "\\t" || _rc == "\\n" || _rc == "\\r" }}')
    raise EmitError(f"unknown builtin method {method!r}")


def _stdlib_helper_traits() -> list[str]:
    """Emitted once per module when any builtin/len node is present.

    Parity notes: string positions are char-based (matching the Python
    backend, where str indexing is per code point); `revl_index_of` returns
    -1 when absent on both hosts; `revl_push`/`revl_concat` are persistent
    (docs/stdlib-2.0.md).
    """
    return [
        # Implemented for `str`, not `String`, so BOTH an owned `String` receiver
        # (via its deref to `str`) and a borrowed `&str` parameter (item 282)
        # reach the same read-only surface, and the arguments take `&str` so a
        # borrowed operand coerces in exactly as `&String` does.
        "trait RevlStrOps {",
        "    fn revl_length(&self) -> i64;",
        "    fn revl_slice(&self, a: i64, b: i64) -> String;",
        "    fn revl_index_of(&self, needle: &str) -> i64;",
        "    fn revl_concat(&self, other: &str) -> String;",
        "    fn revl_split(&self, sep: &str) -> Vec<String>;",
        "    fn revl_repeat(&self, n: i64) -> String;",
        "    fn revl_starts_with(&self, prefix: &str) -> bool;",
        "    fn revl_ends_with(&self, suffix: &str) -> bool;",
        "}",
        "impl RevlStrOps for str {",
        "    fn revl_length(&self) -> i64 { self.chars().count() as i64 }",
        "    fn revl_slice(&self, a: i64, b: i64) -> String {",
        "        self.chars().skip(a.max(0) as usize).take((b - a).max(0) as usize).collect()",
        "    }",
        "    fn revl_index_of(&self, needle: &str) -> i64 {",
        "        let hay: Vec<char> = self.chars().collect();",
        "        let nee: Vec<char> = needle.chars().collect();",
        "        if nee.is_empty() { return 0; }",
        "        if nee.len() > hay.len() { return -1; }",
        "        for i in 0..=(hay.len() - nee.len()) {",
        "            if hay[i..i + nee.len()] == nee[..] { return i as i64; }",
        "        }",
        "        -1",
        "    }",
        "    fn revl_concat(&self, other: &str) -> String { format!(\"{}{}\", self, other) }",
        "    fn revl_split(&self, sep: &str) -> Vec<String> {",
        "        if sep.is_empty() {",
        "            self.chars().map(|c| c.to_string()).collect()",
        "        } else {",
        "            self.split(sep).map(|s| s.to_string()).collect()",
        "        }",
        "    }",
        "    fn revl_repeat(&self, n: i64) -> String { self.repeat(n.max(0) as usize) }",
        "    fn revl_starts_with(&self, prefix: &str) -> bool { self.starts_with(prefix) }",
        "    fn revl_ends_with(&self, suffix: &str) -> bool { self.ends_with(suffix) }",
        "}",
        "trait RevlStrListOps {",
        "    fn revl_join(&self, sep: &str) -> String;",
        "}",
        "impl RevlStrListOps for Vec<String> {",
        "    fn revl_join(&self, sep: &str) -> String { self.join(sep) }",
        "}",
        "trait RevlListOps<T> {",
        "    fn revl_length(&self) -> i64;",
        "    fn revl_slice(&self, a: i64, b: i64) -> Vec<T>;",
        "    fn revl_index_of(&self, needle: &T) -> i64;",
        "    fn revl_concat(&self, other: &Vec<T>) -> Vec<T>;",
        "    fn revl_push(&self, item: T) -> Vec<T>;",
        "}",
        "impl<T: Clone + PartialEq> RevlListOps<T> for Vec<T> {",
        "    fn revl_length(&self) -> i64 { self.len() as i64 }",
        "    fn revl_slice(&self, a: i64, b: i64) -> Vec<T> {",
        "        // JS slice semantics: out-of-range bounds clamp, never panic.",
        "        let len = self.len();",
        "        let a2 = (a.max(0) as usize).min(len);",
        "        let b2 = (b.max(0) as usize).min(len).max(a2);",
        "        self[a2..b2].to_vec()",
        "    }",
        "    fn revl_index_of(&self, needle: &T) -> i64 {",
        "        self.iter().position(|x| x == needle).map(|i| i as i64).unwrap_or(-1)",
        "    }",
        "    fn revl_concat(&self, other: &Vec<T>) -> Vec<T> {",
        "        let mut _v = self.clone(); _v.extend(other.iter().cloned()); _v",
        "    }",
        "    fn revl_push(&self, item: T) -> Vec<T> {",
        "        let mut _v = self.clone(); _v.push(item); _v",
        "    }",
        "}",
        "",
    ]


def _v3_interp(node: dict, ctx: _V3Ctx, rename: dict[str, str] | None = None) -> str:
    parts = node.get("parts") or []
    format_parts: list[str] = []
    args: list[str] = []
    for kind, value in parts:
        if kind == "text":
            format_parts.append(value.replace("{", "{{").replace("}", "}}"))
        elif _v3_is_float(value):
            # A `Float` renders through the canonical ECMAScript form, not
            # Rust's `{}` (`format!("{}", 1e21)` is the full 22-digit expansion
            # and `-0.0` is `-0`); see docs/strings.md.
            format_parts.append("{}")
            args.append(f"revl_ftoa({_render_expr(value, ctx, rename)})")
        else:  # ["expr", ir_node]
            format_parts.append("{}")
            args.append(_render_expr(value, ctx, rename))
    joined = "".join(format_parts)
    if not args:
        return f"format!({_string(joined)})"
    return f"format!({_string(joined)}, {', '.join(args)})"


def _v3_match_expr(node: dict, ctx: _V3Ctx, rename: dict[str, str] | None = None) -> str:
    scrut_node = node.get("scrutinee")
    # A `match` consumes its scrutinee (a bound pattern moves the payload out),
    # so a reused non-Copy scrutinee must be cloned or every later use borrows a
    # moved value (E0382): `match recv { Var(n) => .. }` then `infer(recv)`. A
    # single-use scrutinee stays a move, byte-identical to before.
    scrutinee = _by_value_reuse(
        scrut_node, _render_expr(scrut_node, ctx, rename), ctx)
    arms = node.get("arms") or []
    lines = [f"match {scrutinee} {{"]
    for arm in arms:
        pattern = ctx.match_pattern(arm)
        body = _render_expr(arm.get("body"), ctx, rename)
        bind = arm.get("bind")
        # `bind == "_"` is a wildcarded payload (`Arrow(_) => ..`): the pattern
        # already renders as `Expr::Arrow(_)` via `match_pattern`, and there is
        # no name to reference in the body. `_` is not a value in Rust (E0425:
        # "in expressions, `_` can only be used on the left-hand side of an
        # assignment"), so it must never reach the unboxing `let` below or the
        # `*_` dereference this branch would otherwise emit.
        if bind and bind != "_" and (ctx.case_adt.get(arm.get("pattern")), arm.get("pattern")) \
                in ctx.boxed_cases:
            # The payload of a recursive case is `Box<Payload>` (E0072), so the
            # bind is a `Box`, not the payload. Unbox it up front (`let b = *b;`)
            # so the arm body reads the owned payload exactly as an unboxed
            # binding would -- no per-use deref, and byte-identical to the
            # non-recursive case (a non-boxed enum grows no such wrapper).
            b = _ident(bind, "match bind")
            body = f"{{ let {b} = *{b}; {body} }}"
        lines.append(f"    {pattern} => {body},")
    if not any(arm.get("pattern") == "_" for arm in arms):
        # lower.py has already checked exhaustiveness for known ADTs.
        lines.append("    _ => unreachable!(),")
    lines.append("}")
    return "\n".join(lines)



def _v3_let_pattern(node: dict, ctx: _V3Ctx, out: list[str], indent: int) -> None:
    pad = "    " * indent
    value = _render_expr(node.get("value"), ctx)
    pattern = node.get("pattern")
    names = [_ident(n, "binding") for n in node.get("names") or []]
    keyword = "let mut" if node.get("mutable") else "let"
    if pattern == "record":
        # The destructured value's type, when known, is target-type context for
        # a subset that several records admit (item 268).
        expected = _v3_infer_type(node.get("value"), ctx)
        type_name = ctx.record_type_for_names(node.get("names") or [], expected)
        fields = ", ".join(names)
        out.append(f"{pad}{keyword} {type_name} {{ {fields} }} = {value};")
    elif pattern == "list":
        tmp = f"_revl_list_{indent}_{len(out)}"
        out.append(f"{pad}let {tmp} = {value};")
        for index, name in enumerate(names):
            out.append(f"{pad}{keyword} {name} = {tmp}[{index}].clone();")
        rest = node.get("rest")
        if rest:
            out.append(f"{pad}{keyword} {_ident(rest, 'binding')} = {tmp}[{len(names)}..].to_vec();")
    else:
        raise EmitError(f"unsupported let_pattern kind {pattern!r}")


# The persistent-collection methods whose 2.0 lowering is a functional
# clone-then-mutate-then-return: `revl_push` (List) and Map `set`/`remove` each
# deep-copy the whole receiver, mutate the copy, and hand it back
# (docs/collections.md). When such a call is bound straight back to its OWN
# receiver -- `out = out.push(x)` -- the pre-image of `out` is overwritten and
# can never be read again, so the copy is pure waste. Item 284 rewrites that one
# shape to an in-place mutation, turning the self-host lexer's O(tokens^2)
# accumulator append (item 283: ~85% of the rust native gap, 12.1M allocs/pass)
# into O(1) amortised.
#
# Each entry renders the in-place statement from the receiver identifier and the
# already-rendered argument expressions. Only methods that resolve to a SINGLE
# receiver type are listed, so no receiver-type disambiguation is needed: `push`
# is List-only and `set`/`remove` are Map-only, whereas `concat` is defined on
# both Str and List and is deliberately left out (its receiver type is not known
# at this node). The in-place value equals the clone-then-return value: a discard
# of `HashMap::insert`/`remove`'s returned Option is the only difference, and the
# resulting collection is identical.
def _v3_inplace_persistent(method: str, recv: str, args: list[str]) -> str | None:
    if method == "push" and len(args) == 1:
        return f"{recv}.push({args[0]});"
    if method == "set" and len(args) == 2:
        return f"{recv}.insert({args[0]}, {args[1]});"
    if method == "remove" and len(args) == 1:
        return f"{recv}.remove(&{args[0]});"
    return None


def _v3_self_append_inplace(target_name, recv: str, value_node,
                            ctx: "_V3Ctx") -> str | None:
    """The in-place rewrite of a self-reassigned persistent append, or None.

    Fires only for `<v> = <v>.<persistent>(..)`: the assignment target and the
    call's receiver must name the SAME local, read as a bare variable. That is
    the one shape the rewrite can prove both dead and uniquely owned:

    * dead -- the call result rebinds the receiver over its own value, so the
      pre-image is unreachable after this statement; and
    * uniquely owned -- every persistent method borrows its receiver
      (`&self` / `self.clone()`), so a second *live* owner of that buffer could
      only arise from a by-value move of the receiver. Every such move the
      backend emits already clones the value first (`_by_value_arg`/
      `_by_value_tail`, record-field and closure captures), and a bare
      `let a = out` that then reuses `out` fails to compile today (E0382,
      moved-then-reused). So in exactly the cases that compile, the receiver is
      the sole owner and the in-place write yields the identical value.

    Restricted to a bare `var` receiver (what a plain-fn body produces) so no
    rename-map indirection can make the printed receiver differ from the target.
    """
    if not isinstance(value_node, dict) or value_node.get("kind") != "builtin":
        return None
    tgt = value_node.get("target")
    if not isinstance(tgt, dict) or tgt.get("kind") != "var":
        return None
    if tgt.get("name") != target_name:
        return None
    method = value_node.get("method")
    arg_nodes = value_node.get("args") or []
    rendered = [_render_expr(a, ctx) for a in arg_nodes]
    # `push`/`set` MOVE the appended value(s) into the collection, so a reused
    # non-Copy value (`amb = amb.push(cn)` with `cn` read again) must clone; the
    # receiver's own liveness is proven separately by this rewrite. `remove`
    # borrows its key (`&k`), so it is left untouched.
    if method in ("push", "set"):
        rendered = [
            _by_value_reuse(a, r, ctx) for a, r in zip(arg_nodes, rendered)]
    return _v3_inplace_persistent(method, recv, rendered)


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


def _v3_stmt(node: dict, ctx: _V3Ctx, out: list[str], indent: int, *, test_mode: bool = False) -> None:
    pad = "    " * indent
    step = node.get("step")

    if step in ("let", "assign"):
        name = _ident(node.get("name"), "binding")
        inferred = _v3_infer_type(node.get("value"), ctx)
        # A binding already carrying a declared type (a re-`assign`, or a `let`
        # over a typed local) is target-type context for a record-literal RHS
        # whose field-set is not unique (item 268).
        expected = ctx.var_types.get(node.get("name"))
        # item 284: a self-reassigned persistent append (`out = out.push(x)`)
        # lowers to an in-place mutation. Only an `assign` qualifies -- a `let`
        # introduces a NEW binding, so its RHS receiver is a different value that
        # stays live. The receiver type is unchanged, so `var_types` still holds.
        inplace = (_v3_self_append_inplace(node.get("name"), name,
                                           node.get("value"), ctx)
                   if step == "assign" else None)
        if inplace is not None:
            if inferred is not None:
                ctx.var_types[node.get("name")] = inferred
            out.append(f"{pad}{inplace}")
            return
        value = _render_expr(node.get("value"), ctx, expected=expected)
        # A `let`/`assign` whose RHS is a bare non-Copy binding MOVES it, so a
        # later read of that binding borrows a moved value (E0382):
        # `let mut rendered = base;` then `base.concat(..)`. This is the same
        # reuse shape a branch tail hits, so it clones under the same rule --
        # bare identifier, reused in the body, not known-Copy -- and a single-use
        # RHS stays a move (byte-identical to before).
        value = _by_value_tail(node.get("value"), value, ctx)
        if inferred is not None:
            ctx.var_types[node.get("name")] = inferred
        if step == "let":
            keyword = "let mut" if node.get("mutable") else "let"
            annot = ""
            # An empty `vec![]` gives rustc no element type; when the body's
            # pushes/aliases make it knowable, annotate so the accumulator idiom
            # (`let mut out = []; .. out = out.push(x)`) compiles (E0282).
            if _v3_is_empty_list(node.get("value")):
                elem = ctx.vec_elems.get(node.get("name"))
                if isinstance(elem, str):
                    annot = f": Vec<{_rust_type(elem, ctx.types)}>"
            out.append(f"{pad}{keyword} {name}{annot} = {value};")
        else:
            out.append(f"{pad}{name} = {value};")
    elif step == "return":
        if node.get("expr") is None:
            out.append(f"{pad}return;")
        else:
            out.append(f"{pad}return "
                       f"{_render_expr(node['expr'], ctx, expected=ctx.current_return)};")
    elif step == "if":
        out.append(f"{pad}if {_render_expr(node['cond'], ctx)} {{")
        for child in node.get("then") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        if node.get("else"):
            out.append(f"{pad}}} else {{")
            for child in node["else"]:
                _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "while":
        _guard_frame_neutral_loop(node.get("body"))
        out.append(f"{pad}while {_render_expr(node['cond'], ctx)} {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "for":
        _guard_frame_neutral_loop(node.get("body"))
        bind = _ident(node.get("bind"), "loop binding")
        # `for x in v` consumes `v` via `.into_iter()`; a reused non-Copy Vec
        # binding iterated twice (`for ln in lines { .. } .. for ln in lines`)
        # moves on the first loop, so it is cloned when reused (a single-use
        # iterable stays a move). Cloning the Vec keeps the loop binding owned, so
        # the body is unchanged -- unlike a `&v` borrow, which would retype `x`.
        iterable = _by_value_tail(
            node["iterable"], _render_expr(node["iterable"], ctx), ctx)
        out.append(f"{pad}for {bind} in {iterable} {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "break":
        out.append(f"{pad}break;")
    elif step == "continue":
        out.append(f"{pad}continue;")
    elif step == "let_pattern":
        _v3_let_pattern(node, ctx, out, indent)
    elif step == "expr":
        out.append(f"{pad}let _ = {_render_expr(node['expr'], ctx)};")
    elif step == "assert":
        out.append(f"{pad}assert!({_render_expr(node['expr'], ctx)});")
    else:
        raise EmitError(f"unsupported fn statement step {step!r}")



def _recursive_boxed_edges(types: dict) -> tuple[set, set]:
    """Return ``(boxed_cases, boxed_fields)`` — the enum-variant payload edges
    and record-field edges that must be ``Box``-indirected so a recursive ADT is
    finite-sized on the Rust tier (E0072), which also breaks the drop-check cycle
    those recursive types otherwise trigger (E0391).

    revl admits recursive datatypes (a self-host AST `Expr` whose cases carry
    per-case structs — `BinN`, `IfN`, ... — that again contain `Expr` fields).
    Rust requires a heap indirection on some edge of every containment *cycle* or
    the type has infinite size. `Vec`/`Opt`/`Map`/`Box` already ARE indirection,
    so only a *direct* (bare) user-type field/payload can be recursive; a
    `List[Expr]` field never is. We break every cycle with the fewest boxes,
    preferring an ENUM-variant payload edge over a struct field: boxing an enum
    payload needs no read-site change (`Box<T>` auto-derefs for field access and
    a field can be moved out of the box), whereas a boxed struct field would
    force a deref at every read. Keys are the RAW type/member names (exactly as
    they appear in `types`), so the declaration site, the constructor, and the
    record literal all agree without threading the mangled idents.
    """
    adj: dict[str, list[tuple[str, str, str]]] = {}
    enum_edges: list[tuple[str, str, str]] = []   # (owner, case, target)
    field_edges: list[tuple[str, str, str]] = []  # (owner, field, target)
    for name, spec in types.items():
        edges: list[tuple[str, str, str]] = []
        if spec.get("kind") == "record":
            for field, ftype in (spec.get("fields") or {}).items():
                # a *bare* user-type name is direct containment; a generic
                # (`List[..]`/`Opt[..]`/`Map[..]`) is not in `types`, so it is
                # already indirection and cannot make the type infinite.
                if isinstance(ftype, str) and ftype in types:
                    edges.append(("field", field, ftype))
                    field_edges.append((name, field, ftype))
        elif spec.get("kind") == "variant":
            for case in spec.get("cases") or []:
                payload = case.get("payload")
                if isinstance(payload, str) and payload in types:
                    edges.append(("case", case.get("name"), payload))
                    enum_edges.append((name, case.get("name"), payload))
        adj[name] = edges

    boxed_cases: set = set()
    boxed_fields: set = set()

    def reaches(start: str, goal: str) -> bool:
        """Can `goal` be reached from `start` in the graph with the
        already-boxed (now-indirected) edges removed?"""
        seen, stack = set(), [start]
        while stack:
            node = stack.pop()
            for kind, member, target in adj.get(node, []):
                if kind == "case" and (node, member) in boxed_cases:
                    continue
                if kind == "field" and (node, member) in boxed_fields:
                    continue
                if target == goal:
                    return True
                if target not in seen:
                    seen.add(target)
                    stack.append(target)
        return False

    # Greedy feedback-edge selection. An edge owner->target lies on a cycle iff
    # `target` can still reach `owner`; box the first such edge, preferring enum
    # payloads (in declaration order) over struct fields, then repeat until no
    # cycle remains. Each pass boxes at most one edge, so this terminates.
    changed = True
    while changed:
        changed = False
        for owner, case, target in enum_edges:
            if (owner, case) in boxed_cases:
                continue
            if reaches(target, owner):
                boxed_cases.add((owner, case))
                changed = True
                break
        if changed:
            continue
        for owner, field, target in field_edges:
            if (owner, field) in boxed_fields:
                continue
            if reaches(target, owner):
                boxed_fields.add((owner, field))
                changed = True
                break
    return boxed_cases, boxed_fields


def _emit_v3_types(types: dict) -> list[str]:
    # PartialEq is not decoration: revl has one equality and it is
    # structural (syntax-2.0 §3.4), so `{a: 1} == {a: 1}` must compile and
    # be true here as it is on python. Without the derive, rustc refuses
    # the comparison outright (E0369) and legal revl fails on this tier.
    out: list[str] = []
    boxed_cases, boxed_fields = _recursive_boxed_edges(types)
    # Two records with the IDENTICAL field->type shape are interchangeable in
    # revl (structural equality, item 268), so a value of one is admitted where
    # the other is declared -- `let one = [Bind {..}]` then `ArrowN { params:
    # one }` with `params: List[ParamN]`. Rust sees `Bind` and `ParamN` as
    # distinct nominal types (E0308), so the emitter designates ONE canonical
    # struct per shape (first in declaration order) and lowers its structural
    # twins to `pub type Twin = Canonical;`. The alias shares the canonical's
    # constructor, fields, derives and serde form, so every twin is one Rust
    # type and the two `Vec<_>`s unify. Distinct shapes (the common case, and the
    # whole checked-in corpus) get no alias, so output stays byte-identical there.
    canonical: dict[str, str] = {}   # raw record name -> canonical raw name
    _by_shape: dict[tuple, str] = {}
    for rn, sp in types.items():
        if sp.get("kind") == "record":
            shape = tuple(sorted((sp.get("fields") or {}).items()))
            canonical[rn] = _by_shape.setdefault(shape, rn)
    for raw_name, spec in types.items():
        name = _ident(raw_name, "type name")
        if spec.get("kind") == "record":
            twin = canonical.get(raw_name)
            if twin is not None and twin != raw_name:
                out.append(
                    f"pub type {name} = {_ident(twin, 'type name')};")
                out.append("")
                continue
            out.append("#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]")
            out.append(f"pub struct {name} {{")
            for field, ftype in (spec.get("fields") or {}).items():
                rendered = _rust_type(ftype, types)
                if (raw_name, field) in boxed_fields:
                    rendered = f"Box<{rendered}>"
                out.append(f"    {_ident(field, 'record field')}: {rendered},")
            out.append("}")
        elif spec.get("kind") == "variant":
            out.append("#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]")
            out.append('#[serde(tag = "$kind", content = "$value")]')
            out.append(f"pub enum {name} {{")
            for case in spec.get("cases") or []:
                cname = _ident(case.get("name"), "case name")
                payload = case.get("payload")
                if payload is None:
                    out.append(f"    {cname},")
                else:
                    rendered = _rust_type(payload, types)
                    if (raw_name, case.get("name")) in boxed_cases:
                        rendered = f"Box<{rendered}>"
                    out.append(f"    {cname}({rendered}),")
            out.append("}")
        else:
            raise EmitError(f"unsupported type kind {spec.get('kind')!r} for {name!r}")
        out.append("")
    return out


def _emit_v3_functions(functions: list, types: dict, externs: list) -> list[str]:
    ctx = _V3Ctx(types, functions, externs)
    out: list[str] = []
    for fn in functions:
        name = _ident(fn.get("name"), "function name")
        ctx.var_types = {p.get("name"): p.get("type") for p in fn.get("params") or []}
        ctx.current_return = fn.get("returns")
        counts: dict[str, int] = {}
        _body_multi_use(fn.get("body") or [], counts)
        ctx.multi_use = {n for n, c in counts.items() if c > 1}
        # Element types for this fn's empty-`vec![]` locals, so the `let` can be
        # annotated `Vec<T>` (E0282). Computed before the body renders and after
        # `var_types` is seeded with the params it reads.
        ctx.vec_elems = _v3_empty_vec_elem_types(fn.get("body") or [], ctx)
        # A read-only `Str` param lowers to a borrowed `&str` (item 282): the
        # caller lends the string instead of cloning it. `_render_param_type`
        # spells the borrow, and `ctx.borrowed_params` tells the body renderer
        # the param is already a `&str`.
        borrow = ctx.fn_borrow.get(fn.get("name"), frozenset())
        param_list = fn.get("params") or []
        ctx.borrowed_params = {
            param_list[idx].get("name") for idx in borrow
        }
        params = ", ".join(
            f"{_ident(p.get('name'), 'parameter name')}: "
            f"{_render_param_type(idx in borrow, p.get('type'), types)}"
            for idx, p in enumerate(param_list)
        )
        returns = _rust_type(fn.get("returns"), types, position="return")
        visibility = "pub " if fn.get("public") else ""
        out.append(f"{visibility}fn {name}({params}) -> {returns} {{")
        if not fn.get("body"):
            out.append("    todo!()")
        else:
            for stmt in fn["body"]:
                _v3_stmt(stmt, ctx, out, 1)
        out.append("}")
        out.append("")
    return out


# item 378 Stage 5: module-level config seam for document-global config externs.
# Mirrors the py tier's `_REVL_EXTERN_CONFIG` map + fail-loud
# `_revl_extern_config` helper: a mutable module-global config map (a
# `OnceLock<Mutex<..>>`, the safe-Rust equivalent of a plug-time mutable
# global), keyed by extern name, that a composition driver fills at plug time,
# and a lookup that PANICS, naming the extern, when a required (non-defaulted)
# field is absent, instead of handing the body an empty map that fails opaquely
# later. A defaults-only extern still resolves to its defaults driver-free.
#
# Rust has no dynamic value top-type with literal defaults (unlike ts `unknown`
# / go `any` / java `Object`), so the config map is `HashMap<String, String>`
# and a rust-bodied config extern is restricted to `Str` config fields
# (`_rust_extern_config_bind` refuses a non-Str field LOUDLY, redirecting to
# @py or option (c)). This covers the design's motivating case (provider
# identity is a string); a typed heterogeneous carrier is a separate value-
# carrier design step. Fully-qualified `std::` paths so the seam adds no `use`
# (which could duplicate the module's own imports). Emitted only when a config
# extern is present, so a no-config program is byte-identical.
_RUST_EXTERN_CONFIG_SCAFFOLD = [
    "fn _revl_extern_config_store() -> &'static std::sync::Mutex<",
    "    std::collections::HashMap<String, "
    "std::collections::HashMap<String, String>>,",
    "> {",
    "    static STORE: std::sync::OnceLock<",
    "        std::sync::Mutex<std::collections::HashMap<String, "
    "std::collections::HashMap<String, String>>>,",
    "    > = std::sync::OnceLock::new();",
    "    STORE.get_or_init(|| std::sync::Mutex::new("
    "std::collections::HashMap::new()))",
    "}",
    "",
    "#[allow(dead_code)]",
    "fn _revl_extern_config(",
    "    name: &str,",
    "    required: &[&str],",
    "    defaults: &[(&str, &str)],",
    ") -> std::collections::HashMap<String, String> {",
    "    let mut out: std::collections::HashMap<String, String> = "
    "std::collections::HashMap::new();",
    "    for (k, v) in defaults {",
    "        out.insert((*k).to_string(), (*v).to_string());",
    "    }",
    "    let store = _revl_extern_config_store().lock().unwrap();",
    "    match store.get(name) {",
    "        None => {",
    "            if !required.is_empty() {",
    "                panic!(",
    "                    \"config extern `{}` called before plug-time "
    "configuration was installed (required config: {}); configure it through "
    "the run driver's config seam\",",
    "                    name,",
    "                    required.join(\", \")",
    "                );",
    "            }",
    "        }",
    "        Some(cfg) => {",
    "            let missing: Vec<&str> = required",
    "                .iter()",
    "                .copied()",
    "                .filter(|f| !cfg.contains_key(*f))",
    "                .collect();",
    "            if !missing.is_empty() {",
    "                panic!(",
    "                    \"config extern `{}` called before plug-time "
    "configuration was installed (missing required config: {})\",",
    "                    name,",
    "                    missing.join(\", \")",
    "                );",
    "            }",
    "            for (k, v) in cfg {",
    "                out.insert(k.clone(), v.clone());",
    "            }",
    "        }",
    "    }",
    "    out",
    "}",
    "",
]


def _rust_extern_config_bind(ext: dict) -> str:
    """The `let _revl_config = ...` first-body line for a config extern, or None.
    `_revl_config` is a `HashMap<String, String>`; the verbatim @rs body reads a
    field as `_revl_config["field"]` (a `&String`). Refuses a non-`Str` config
    field LOUDLY: the rust map is string-valued, so a heterogeneous field has no
    faithful home on this tier yet."""
    schema = ext.get("config")
    if not schema:
        return None
    name = ext.get("name")
    for field in schema:
        if field.get("type") != "Str":
            raise EmitError(
                f"config extern `{name}`: field `{field.get('name')}` has type "
                f"`{field.get('type')}`, but the @rs config seam is string-valued "
                f"and supports only `Str` config fields today. Give this extern a "
                f"@py body, or use option (c) (a home component that `requires` "
                f"the service). See docs/design/378-sync-extern-service-reach.md.")
    required = [f["name"] for f in schema if f.get("default") is None]
    defaults = [(f["name"], f["default"]) for f in schema
                if f.get("default") is not None]
    req_lit = "&[%s]" % ", ".join(_string(f) for f in required)
    def_lit = "&[%s]" % ", ".join(
        f"({_string(k)}, {_string(v)})" for k, v in defaults)
    return (f"let _revl_config = _revl_extern_config("
            f"{_string(name)}, {req_lit}, {def_lit});")


def _emit_v3_externs(externs: list, types: dict) -> list[str]:
    out: list[str] = []
    # item 378 Stage 5: emit the config seam once, before the externs, when any
    # extern carries a config schema (byte-identical when none do).
    if any(ext.get("config") for ext in externs):
        out.extend(_RUST_EXTERN_CONFIG_SCAFFOLD)
    for ext in externs:
        name = _ident(ext.get("name"), "extern name")
        params = ", ".join(
            f"{_ident(p.get('name'), 'extern parameter name')}: "
            f"{_rust_type(p.get('type'), types, position='param')}"
            for p in ext.get("params") or []
        )
        returns = _rust_type(ext.get("returns"), types, position="return")
        bodies = ext.get("bodies") or {}
        if "rs" not in bodies:
            raise EmitError(
                f"extern `{name}` has no @rs body — not portable to this backend "
                f"(available: {', '.join(sorted(bodies)) or 'none'})"
            )
        out.append(f"fn {name}({params}) -> {returns} {{")
        # item 378 Stage 5: a config extern binds `_revl_config` as the first
        # body line; None for a no-config extern (byte-identical body splice).
        config_bind = _rust_extern_config_bind(ext)
        if config_bind:
            out.append("    " + config_bind)
        body = bodies["rs"].strip()
        if body:
            for line in body.splitlines() or [""]:
                out.append("    " + line)
        else:
            out.append("    // (empty @rs body)")
        out.append("}")
        out.append("")
    return out


def _emit_v3_tests(tests: list, types: dict, functions: list, externs: list) -> list[str]:
    ctx = _V3Ctx(types, functions, externs)
    out: list[str] = []
    used: set[str] = set()
    for test in tests:
        if test.get("lifecycle"):
            # lifecycle tests are emitted by _emit_v3_lifecycle_tests — their
            # body is a script over a live composition, not pure statements
            continue
        base = _snake(test.get("name") or "test")
        base = re.sub(r"[^A-Za-z0-9_]", "_", base)
        if not base or base[0].isdigit():
            base = "test_" + base
        name = base
        counter = 0
        while name in used:
            counter += 1
            name = f"{base}_{counter}"
        used.add(name)
        out.append("#[test]")
        out.append(f"fn {name}() {{")
        if not test.get("body"):
            out.append("    // (empty test body)")
        else:
            for stmt in test["body"]:
                _v3_stmt(stmt, ctx, out, 1, test_mode=True)
        out.append("}")
        out.append("")
    return out


def _emit_v3_lifecycle_tests(tests: list, types: dict, functions: list,
                             externs: list, services: dict,
                             components: list) -> list[str]:
    """`lifecycle test` blocks (syntax-2.0 §7.1) as ``#[test]`` fns driving a
    live cordis-rs context.

    A lifecycle test is a script over a *live* composition: load components
    into a ``cordis::Context``, call through provision keys, unload them LIFO,
    and assert the runtime holds nothing afterwards. It lowers to the tier's
    native test idiom exactly the way a plain ``test`` block does (FR-5):
    ``#[test]`` fns that reuse the generated bridge plumbing — ``_revl_load``
    for loads, typed ``ctx.require`` calls through the service traits for
    calls — and prove ``assert no_residue`` with the same introspection the
    ``run --once`` driver uses (``registry().len() == 0`` and
    ``reflect().services().len() == 0``, docs/backend-ir.md R4).

    The lowerer checks the script statically (every key's provider must be
    loaded, G2 provision disjointness), so a refused call is a compile error
    before any of this runs; here the script becomes Rust.
    """
    if not services:
        raise EmitError(
            "a lifecycle test loads components and calls through provision "
            "keys, so it needs at least one service in the document to drive; "
            "this document declares none"
        )
    provided: dict[str, str] = {}
    for component in components:
        for key, service in (component.get("provides") or {}).items():
            provided[key] = service
    method_tables = {
        sname: (svc.get("methods") or {})
        for sname, svc in services.items()
    }
    ctx = _V3Ctx(types, functions, externs, components)
    out: list[str] = []
    used: set[str] = set()
    for test in tests:
        if not test.get("lifecycle"):
            continue
        where = f"lifecycle test {_string(test['name'])}"
        base = _snake(test.get("name") or "lifecycle")
        base = re.sub(r"[^A-Za-z0-9_]", "_", base)
        if not base or base[0].isdigit():
            base = "lifecycle_" + base
        name = f"revl_lifecycle_{base}"
        counter = 0
        while name in used:
            counter += 1
            name = f"revl_lifecycle_{base}_{counter}"
        used.add(name)
        out.append("#[test]")
        out.append(f"fn {name}() {{")
        out.append("    // drives the composition on a real cordis-rs context and")
        out.append("    // proves no residue after LIFO teardown (FR-5 / §7.1).")
        out.append("    let root = cordis::Context::new();")
        if _lifecycle_uses_advance(test):
            # item 112: the clock coeffect is a thread-local the cargo-test
            # thread pool may reuse across tests; reset it so this test's
            # `advance` steps start from t=0 and fire only its own timers,
            # independent of any earlier lifecycle test (mirrors the py tier's
            # `Clock.reset()`). Load happens below, so this test's timers survive.
            out.append("    revl_clock_reset();")
        out.append("    let mut _revl_fibers: Vec<(&str, cordis::Fiber)> = Vec::new();")
        for step in test.get("body") or []:
            kind = step.get("step")
            if kind == "load":
                component = step["component"]
                snake = _snake(component)
                cfg = step.get("config") or {}
                cfg_items = ", ".join(
                    f"{_string(field)}: {_render_expr(value, ctx, {})}"
                    for field, value in cfg.items())
                out.append("    {")
                out.append("        let _cfg = serde_json::json!("
                           f'{{{_string(component)}: {{{cfg_items}}}}});')
                out.append(f'        let _f = _revl_load(&root, {_string(snake)}, &_cfg)'
                           f".expect({_string(where + ': load ' + component)});")
                out.append(f'        _f.wait().expect({_string(where + ": " + component + " did not reach ACTIVE (R2)")});')
                out.append(f"        _revl_fibers.push(({_string(snake)}, _f));")
                out.append("    }")
            elif kind == "unload":
                component = step["component"]
                snake = _snake(component)
                out.append(f'    if let Some(pos) = _revl_fibers.iter().position(|(n, _)| *n == {_string(snake)}) {{')
                out.append("        let (_, _f) = _revl_fibers.remove(pos);")
                out.append(f"        _f.dispose().expect({_string(where + ': unload ' + component)});")
                out.append("    }")
            elif kind == "call":
                key = step["key"]
                service = provided.get(key)
                if service is None:  # pragma: no cover — the lowerer rejects it
                    raise EmitError(f"{where}: no provider for key {key!r}")
                method_name = _mname(step["method"])
                method = (method_tables.get(service) or {}).get(step["method"])
                if method is None:  # pragma: no cover — the lowerer rejects it
                    raise EmitError(f"{where}: unknown method {step['method']!r}")
                args = ", ".join(_render_expr(arg, ctx, {})
                                 for arg in step.get("args") or [])
                call = (f'root.require::<Box<dyn {service}>>({_string(key)})'
                        f".expect({_string(where + ': ' + key + ' is ACTIVE (R2)')})"
                        f".{method_name}({args})")
                bind = step.get("bind")
                if bind is not None:
                    out.append(f"    let {_ident(bind, 'lifecycle binding')} = {call};")
                    if method.get("returns"):
                        ctx.var_types[bind] = method.get("returns")
                else:
                    out.append(f"    let _ = {call};")
            elif kind == "assert":
                out.append(f"    assert!({_render_expr(step['expr'], ctx, {})}, "
                           f"{_string(where + ': assertion failed')});")
            elif kind == "advance":
                # item 112 (rust half): drive the clock coeffect forward, firing
                # every timer that comes due (`revl_clock_advance`, item 99). A
                # firing is a deterministic timeline step, so a later `assert`
                # can observe the fired emissions — the rust mirror of the py/ts
                # `Clock.advance(ms)` (docs/time-coeffect.md §advance). The body
                # of a fired timer runs synchronously inside the advance loop, so
                # no settle step is needed on this tier.
                out.append(f"    let _ = revl_clock_advance({int(step['ms'])});")
            elif kind == "assert_no_residue":
                out.append("    // R4 + R1: the composition must leave the live runtime")
                out.append("    // holding nothing — no plugin, no provided service, no")
                out.append("    // unreleased host resource — the same checks the py")
                out.append("    // reference tier's `assert no_residue` performs and the")
                out.append("    // registry/reflect half of `revl run --once`.")
                out.append("    assert!(root.registry().len() == 0"
                           " && root.reflect().services().len() == 0"
                           " && REVL_LIVE_HOST_RESOURCES.with(|c| c.get()) == 0,")
                msg = where + ": residue \u2014 the host runtime still holds state (R4/R1)"
                out.append("            " + _string(msg) + ");")
            else:  # pragma: no cover — the lowerer emits nothing else
                raise EmitError(f"{where}: unknown lifecycle step {kind!r}")
        out.append("}")
        out.append("")
    return out



def _module_header(version: int) -> list[str]:
    allow = (
        "#![allow(dead_code, unused_variables)]"
        if version == 1
        else "#![allow(dead_code, unused_variables, unused_mut, unused_imports, unused_parens, unreachable_patterns)]"
    )
    return [
        f"//! Generated by the revl cordis-rs backend (ir_version {version}): do not edit.",
        f"//! Target: {CRATE} 0.3.x (docs.rs/cordis-rs).",
        allow,
        "",
        "use std::sync::Arc;",
        "use cordis::Value;",
        "",
    ]


def _needs_realm_helper(components: list) -> bool:
    return any(component.get("isolate") for component in components)


def _uses_spawn(components: list) -> bool:
    """True when any component acquires an instance via `spawn`."""
    for component in components:
        for step in component.get("body") or []:
            if (step.get("step") == "let-effect"
                    and (step.get("acquire") or {}).get("kind") == "spawn"):
                return True
    return False


def _uses_timer(components: list) -> bool:
    """True when any component body (or a nested `if` branch) arms a timer."""
    def walk(steps) -> bool:
        for step in steps or []:
            if step.get("step") == "timer":
                return True
            if step.get("step") == "if" and (
                    walk(step.get("then")) or walk(step.get("else"))):
                return True
        return False

    return any(walk(component.get("body")) for component in components)


def _lifecycle_uses_advance(test: dict) -> bool:
    """True iff a lifecycle test drives the clock coeffect (an `advance` step).

    An `advance`-using test needs `revl_clock_advance`/`revl_clock_reset` from
    the timer preamble even in the (degenerate) case where no loaded component
    arms a timer, so the preamble emission keys off this as well as `_uses_timer`
    (item 112)."""
    return any(step.get("step") == "advance" for step in test.get("body") or [])


def _tests_use_advance(tests: list) -> bool:
    return any(t.get("lifecycle") and _lifecycle_uses_advance(t)
               for t in tests or [])


def _revl_timer_preamble() -> list[str]:
    """The clock coeffect + timer scheduler (item 57), the rust mirror of
    backends/python/runtime.py's Clock/TimerHandle — deterministic tick-for-tick.

    Time moves only on `revl_clock_advance`, which fires due timers earliest
    first (ties by arm order) and re-arms `every` across the span, so a firing
    is a reproducible timeline step rather than a wall-clock race. Arming takes
    a live-resource slot (the same thread-local `REVL_LIVE_HOST_RESOURCES` a
    Pool/Map takes); cancel — and a spent `after` — returns it, so a leaked
    `every` timer surfaces through the exact R1 residue accounting an
    `assert no_residue` performs. Thread-local like that counter, so parallel
    `cargo test` threads never share a clock. A timer handle is just its
    serial (`u64`): the cancel closure the disposer stack holds captures only
    that, so it stays `Send` with no `Rc` escaping the arming thread."""
    return [
        "/// clock coeffect + timer scheduler (item 57, docs/time-coeffect.md):",
        "/// the rust mirror of backends/python/runtime.py's Clock/TimerHandle.",
        "struct RevlTimer {",
        "    serial: u64,",
        "    mode: &'static str, // \"every\" | \"after\"",
        "    interval_ms: i64,",
        "    body: std::rc::Rc<dyn Fn()>,",
        "    status: &'static str, // \"live\" | \"cancelled\" | \"done\"",
        "    next_at: i64,",
        "    fired: u64,",
        "}",
        "",
        "#[derive(Default)]",
        "struct RevlClock {",
        "    now: i64,",
        "    serial: u64,",
        "    timers: Vec<RevlTimer>,",
        "    firings: Vec<(u64, i64)>, // (timer serial, fired-at ms) in fire order",
        "}",
        "",
        "thread_local! {",
        "    static REVL_CLOCK: std::cell::RefCell<RevlClock> =",
        "        std::cell::RefCell::new(RevlClock::default());",
        "}",
        "",
        "fn revl_schedule(mode: &'static str, interval_ms: i64,",
        "                 body: std::rc::Rc<dyn Fn()>) -> u64 {",
        "    REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() + 1));",
        "    REVL_CLOCK.with(|c| {",
        "        let mut clock = c.borrow_mut();",
        "        clock.serial += 1;",
        "        let serial = clock.serial;",
        "        let next_at = clock.now + interval_ms;",
        "        clock.timers.push(RevlTimer {",
        "            serial, mode, interval_ms, body,",
        "            status: \"live\", next_at, fired: 0,",
        "        });",
        "        serial",
        "    })",
        "}",
        "",
        "/// Arm a periodic timer against the clock coeffect (`every`).",
        "fn revl_schedule_every(interval_ms: i64, body: impl Fn() + 'static) -> u64 {",
        "    revl_schedule(\"every\", interval_ms, std::rc::Rc::new(body))",
        "}",
        "",
        "/// Arm a one-shot delayed timer against the clock coeffect (`after`).",
        "fn revl_schedule_after(interval_ms: i64, body: impl Fn() + 'static) -> u64 {",
        "    revl_schedule(\"after\", interval_ms, std::rc::Rc::new(body))",
        "}",
        "",
        "/// The schedule's inverse — idempotent, a no-op once the timer is spent.",
        "/// Running it on teardown returns the live-resource slot arming took, so",
        "/// the schedule leaves no residue.",
        "fn revl_cancel(serial: u64) -> bool {",
        "    let released = REVL_CLOCK.with(|c| {",
        "        let mut clock = c.borrow_mut();",
        "        if let Some(t) = clock.timers.iter_mut().find(|t| t.serial == serial) {",
        "            if t.status == \"live\" {",
        "                t.status = \"cancelled\";",
        "                return true;",
        "            }",
        "        }",
        "        false",
        "    });",
        "    if released {",
        "        REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() - 1));",
        "    }",
        "    released",
        "}",
        "",
        "/// Advance logical time by `ms`, firing every timer that comes due —",
        "/// earliest first, ties broken by arm order — and re-arming `every`",
        "/// timers across the whole span. Returns the number of firings.",
        "pub fn revl_clock_advance(ms: i64) -> usize {",
        "    let target = REVL_CLOCK.with(|c| c.borrow().now) + ms;",
        "    let mut count = 0usize;",
        "    loop {",
        "        // pick the earliest-due live timer, advance the clock to it, and",
        "        // extract its body — all under one borrow that is dropped before",
        "        // the body runs (the body is emissions-only and never re-enters).",
        "        let step = REVL_CLOCK.with(|c| {",
        "            let mut clock = c.borrow_mut();",
        "            let mut pick: Option<usize> = None;",
        "            for i in 0..clock.timers.len() {",
        "                let t = &clock.timers[i];",
        "                if t.status != \"live\" || t.next_at > target {",
        "                    continue;",
        "                }",
        "                match pick {",
        "                    None => pick = Some(i),",
        "                    Some(j) => {",
        "                        let tj = &clock.timers[j];",
        "                        if t.next_at < tj.next_at",
        "                            || (t.next_at == tj.next_at && t.serial < tj.serial)",
        "                        {",
        "                            pick = Some(i);",
        "                        }",
        "                    }",
        "                }",
        "            }",
        "            let i = pick?;",
        "            clock.now = clock.timers[i].next_at;",
        "            if clock.timers[i].mode == \"every\" {",
        "                let iv = clock.timers[i].interval_ms;",
        "                clock.timers[i].next_at += iv;",
        "            }",
        "            clock.timers[i].fired += 1;",
        "            let serial = clock.timers[i].serial;",
        "            let now = clock.now;",
        "            clock.firings.push((serial, now));",
        "            let body = clock.timers[i].body.clone();",
        "            let mode = clock.timers[i].mode;",
        "            Some((i, body, mode))",
        "        });",
        "        let (i, body, mode) = match step {",
        "            Some(v) => v,",
        "            None => break,",
        "        };",
        "        body();",
        "        if mode == \"after\" {",
        "            // a one-shot is spent once it fires; release through the same",
        "            // slot-return path cancel uses so the residue trace stays",
        "            // balanced and teardown's own revl_cancel is a clean no-op.",
        "            REVL_CLOCK.with(|c| c.borrow_mut().timers[i].status = \"done\");",
        "            REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() - 1));",
        "        }",
        "        count += 1;",
        "    }",
        "    REVL_CLOCK.with(|c| c.borrow_mut().now = target);",
        "    count",
        "}",
        "",
        "/// Current logical time in ms.",
        "pub fn revl_clock_now() -> i64 {",
        "    REVL_CLOCK.with(|c| c.borrow().now)",
        "}",
        "",
        "/// Live timers — a teardown that abandons one leaves this > 0 (the",
        "/// countable no-orphaned-interval proof).",
        "pub fn revl_clock_pending() -> usize {",
        "    REVL_CLOCK.with(|c| c.borrow().timers.iter()",
        "        .filter(|t| t.status == \"live\").count())",
        "}",
        "",
        "/// The recorded firing log: (timer serial, fired-at ms) in fire order.",
        "pub fn revl_clock_firings() -> Vec<(u64, i64)> {",
        "    REVL_CLOCK.with(|c| c.borrow().firings.clone())",
        "}",
        "",
        "/// Return the clock to time zero with no timers (call between scenarios).",
        "pub fn revl_clock_reset() {",
        "    REVL_CLOCK.with(|c| {",
        "        let mut clock = c.borrow_mut();",
        "        clock.now = 0;",
        "        clock.serial = 0;",
        "        clock.timers.clear();",
        "        clock.firings.clear();",
        "    });",
        "}",
        "",
    ]


def _revl_spawn_handle() -> list[str]:
    """The value a `spawn` acquisition binds: a live component instance, torn
    down by its own `.dispose()` (docs/design-v2-instances.md, phase 1).

    The instance is a CHILD FIBER of its spawner — its own nested teardown
    scope. `dispose()` unloads that fiber, running the instance's LIFO teardown
    NOW, independent of the spawner: a request-scoped instance is reclaimed
    when the request ends, never deferred to the component's teardown. Disposal
    is idempotent (a `Mutex<Option<Fiber>>` taken once), so the spawner's own
    inverse — and cordis-rs's `ctx.plugin()` parent-effect safety net — are
    harmless no-ops once the instance is already gone. `get` reads a provision
    the instance published in ITS local realm: only the holder of this handle
    (the spawner) can reach it; a sibling, isolated into a different local
    realm, cannot (supervision-tree addressing)."""
    return [
        "/// A live spawned-component instance (docs/design-v2-instances.md).",
        "pub struct RevlSpawnHandle {",
        "    fiber: std::sync::Mutex<Option<cordis::Fiber>>,",
        "    ctx: cordis::Context,",
        "}",
        "",
        "impl RevlSpawnHandle {",
        "    pub fn new(fiber: cordis::Fiber, ctx: cordis::Context) -> Self {",
        "        Self { fiber: std::sync::Mutex::new(Some(fiber)), ctx }",
        "    }",
        "    /// Unload the instance's fiber (its LIFO teardown). Idempotent.",
        "    pub fn dispose(&self) -> cordis::Result<()> {",
        "        let taken = self.fiber.lock().unwrap().take();",
        "        if let Some(fiber) = taken { fiber.dispose()?; }",
        "        Ok(())",
        "    }",
        "    /// Read a provision the instance published, in its own local realm.",
        "    pub fn get<T: Send + Sync + 'static>(&self, name: &str)",
        "        -> cordis::Result<Option<std::sync::Arc<T>>> {",
        "        self.ctx.get_unchecked::<T>(name)",
        "    }",
        "    /// The instance's fiber lifecycle state (Active while live).",
        "    pub fn state(&self) -> Option<cordis::FiberState> {",
        "        self.fiber.lock().unwrap().as_ref().map(|f| f.state())",
        "    }",
        "}",
        "",
    ]


def _revl_teardown_preamble() -> list[str]:
    """The three-entry-kind teardown loop's shared runtime substrate: item 243
    (`transactional`, witnessed) plus the unified two-phase abort
    (docs/design/teardown-contract.md), first-party on cordis-rs.

    cordis-rs disposes one fiber's registered effects in a SINGLE
    reverse-registration (LIFO) pass (`Fiber::dispose_effects`, upstream —
    not ours to change, fork+PR only), mixing every kind together: there is
    no second accumulator to plug a Phase-2 pass into. The contract
    anticipates exactly this shape ("The teardown algorithm"): a tier may
    implement the phase split "by having the compensation disposer enqueue
    itself when invoked during an abort unwind and draining the queue in a
    post-unwind hook". This is that hook, built entirely from cordis-rs's
    public API, no upstream change:

    * `RevlTeardown` is the per-activation accumulator: `committed` is the
      abort-vs-commit discriminator (py's `Frame._committed` — here flipped
      right before the emitted `apply` returns `Ok`, the same instant py's
      `Frame.drain` flips it: both mark "this activation is not unwinding,
      it succeeded"). `phase2` is the queue a compensation disposer feeds
      instead of running immediately.
    * The state rides on the fiber's own `Context`, via `Context::extend`/
      `metadata` (cordis-rs's typed per-context metadata slot), instead of a
      bespoke global registry — so a provide-method's stored `ctx` (the SAME
      fiber, looked up later when a method runs) recovers the identical
      instance. No global map, no manual lifecycle bookkeeping: it lives as
      long as the `Arc` clones that hold it, one of which is this same
      `Context`.
    * The FIRST effect registered in the whole activation is the phase-2
      drain hook (`revl_teardown_begin`'s own `ctx.effect` call, emitted
      before any user step). Because cordis-rs disposes LIFO, "registered
      first" means "disposed LAST" — after every bracket and transactional
      inverse in this activation has already run to completion. Phase 1
      finishing before Phase 2 starts falls out of registration order alone;
      no separate stack walk is needed.
    * A `transactional` disposer reads `committed` at DISPOSAL time (not
      registration time, which is unknowable until later) exactly like py's
      `_Transactional`: discharge (drop the witness, do nothing) on commit,
      replay the declared inverse on abort.
    * A `compensation` disposer reads `committed` too: discharge on commit
      (never runs — the forward emission is the deliverable), or on abort
      QUEUE itself onto `phase2` instead of running immediately — so every
      compensation the one LIFO pass encounters lands in the queue in
      reverse-registration order, i.e. already LIFO within itself, for free.
      `revl_drain_phase2` (run by the sentinel, last) then runs the queue in
      that order, honoring `REVL_COMPENSATION_BUDGET_MS` BETWEEN calls. rust
      has no in-call preemption of a synchronous compensation
      (teardown-contract.md, rust row: a compensation closure is not
      guaranteed `Send` to a helper thread, so even go's abandon-the-wait
      shape is unavailable) — `REVL_COMPENSATION_PER_CALL_MS` is read and
      carried for config-surface parity (the two env vars are read once, at
      activation, per the contract), but the between-call deadline is the
      only bound this tier can honor, exactly as specced.
    """
    return [
        "/// item 243 / docs/design/teardown-contract.md: the per-activation",
        "/// three-entry-kind teardown accumulator (transactional + compensation).",
        "/// See `_revl_teardown_preamble` in the emitter for the design.",
        "struct RevlTeardown {",
        "    committed: std::sync::atomic::AtomicBool,",
        "    phase2: std::sync::Mutex<Vec<RevlPendingCompensation>>,",
        "    budget_ms: u64,",
        "    #[allow(dead_code)] // read for config-surface parity; see the",
        "    // preamble docstring — rust has no in-call preemption to bound with it.",
        "    per_call_ms: u64,",
        "}",
        "",
        "struct RevlPendingCompensation {",
        "    label: String,",
        "    call: Box<dyn FnOnce() + Send>,",
        "}",
        "",
        "fn revl_compensation_budget_ms() -> u64 {",
        '    std::env::var("REVL_COMPENSATION_BUDGET_MS").ok()',
        "        .and_then(|v| v.parse::<u64>().ok())",
        "        .unwrap_or(5000)",
        "}",
        "",
        "fn revl_compensation_per_call_ms() -> u64 {",
        '    std::env::var("REVL_COMPENSATION_PER_CALL_MS").ok()',
        "        .and_then(|v| v.parse::<u64>().ok())",
        "        .unwrap_or(1000)",
        "}",
        "",
        "/// item 324: the out-of-band abort registry — the faithful mirror of the",
        "/// py runtime's `_FRAME_BY_CTX` + `_sole_frame`. A session-level reject",
        "/// (item 245's explicit commit/abort UX) runs OUTSIDE the fiber and must",
        "/// reach an already-activated component's `RevlTeardown` to clear",
        "/// `committed`, so its next unload REPLAYS the per-tool-call (and",
        "/// activation-body) transactional inverses instead of discharging them.",
        "/// The state itself lives on the fiber's (private) extended context, which",
        "/// no out-of-fiber caller can reach — cordis-rs's `Context::extend` derives",
        "/// a child whose metadata the parent/fiber context cannot see — so this",
        "/// weak, label-keyed registry is the reach-in seam. Weak so a disposed",
        "/// activation's teardown is collected normally; the registry never keeps",
        "/// one alive.",
        "#[allow(clippy::type_complexity)]",
        "static REVL_TEARDOWN_REGISTRY: std::sync::OnceLock<",
        "    std::sync::Mutex<Vec<(String, std::sync::Weak<RevlTeardown>)>>>",
        "    = std::sync::OnceLock::new();",
        "",
        "fn revl_teardown_registry()",
        "    -> &'static std::sync::Mutex<Vec<(String, std::sync::Weak<RevlTeardown>)>> {",
        "    REVL_TEARDOWN_REGISTRY.get_or_init(|| std::sync::Mutex::new(Vec::new()))",
        "}",
        "",
        "fn revl_teardown_remember(label: &str, state: &std::sync::Arc<RevlTeardown>) {",
        "    revl_teardown_registry().lock().unwrap()",
        "        .push((label.to_string(), std::sync::Arc::downgrade(state)));",
        "}",
        "",
        "/// Abort every live activation registered under `label`: clear `committed`",
        "/// so the next teardown REPLAYS its transactional inverses (py's",
        "/// `Frame.abort`). Idempotent; skips dead weak entries. The driver/harness",
        "/// calls this before unloading the fiber to reject the session's work.",
        "#[allow(dead_code)]",
        "fn revl_abort(label: &str) {",
        "    let registry = revl_teardown_registry().lock().unwrap();",
        "    for (entry_label, weak) in registry.iter() {",
        "        if entry_label == label {",
        "            if let Some(state) = weak.upgrade() {",
        "                state.committed.store(false, std::sync::atomic::Ordering::Release);",
        "            }",
        "        }",
        "    }",
        "}",
        "",
        "/// Register the phase-2 drain hook FIRST — so cordis-rs's LIFO dispose",
        "/// runs it LAST, after every bracket/transactional inverse — and return",
        "/// the shared accumulator plus a `Context` extended to carry it, so a",
        "/// provide-method on this same fiber can recover it later via",
        "/// `revl_teardown_of`.",
        "fn revl_teardown_begin(ctx: &cordis::Context, label: &str)",
        "    -> cordis::Result<(cordis::Context, std::sync::Arc<RevlTeardown>)> {",
        "    let state = std::sync::Arc::new(RevlTeardown {",
        "        committed: std::sync::atomic::AtomicBool::new(false),",
        "        phase2: std::sync::Mutex::new(Vec::new()),",
        "        budget_ms: revl_compensation_budget_ms(),",
        "        per_call_ms: revl_compensation_per_call_ms(),",
        "    });",
        '    let ctx = ctx.extend("_revl_teardown", state.clone());',
        "    revl_teardown_remember(label, &state);  // item 324: out-of-band abort reach-in",
        "    let sentinel = state.clone();",
        "    ctx.effect(label.to_string(), move || {",
        "        if !sentinel.committed.load(std::sync::atomic::Ordering::Acquire) {",
        "            revl_drain_phase2(&sentinel);",
        "        }",
        "        Ok(())",
        "    })?;",
        "    Ok((ctx, state))",
        "}",
        "",
        "/// A provide-method's own recovery of its activation's teardown state",
        "/// (`self.ctx` is the same fiber `revl_teardown_begin` extended).",
        "fn revl_teardown_of(ctx: &cordis::Context) -> std::sync::Arc<RevlTeardown> {",
        '    (*ctx.metadata::<std::sync::Arc<RevlTeardown>>("_revl_teardown")',
        "        .ok()",
        "        .flatten()",
        '        .expect("revl: teardown state missing — a compensated effect ran \\',
        '                outside an activation that registered one"))',
        "        .clone()",
        "}",
        "",
        "/// Phase 2: best-effort compensation replay, LIFO within itself (the",
        "/// queue already holds that order — see the preamble docstring), bounded",
        "/// by `REVL_COMPENSATION_BUDGET_MS` checked BETWEEN calls (rust has no",
        "/// in-call preemption of a synchronous compensation — see the rust row",
        "/// of docs/design/teardown-contract.md). A panicking compensation is",
        "/// caught (best-effort, never fails the abort) and logged; the loop",
        "/// continues to the next queued compensation either way.",
        "fn revl_drain_phase2(state: &RevlTeardown) {",
        "    let queued: Vec<RevlPendingCompensation> = {",
        "        let mut guard = state.phase2.lock().unwrap();",
        "        std::mem::take(&mut *guard)",
        "    };",
        "    if queued.is_empty() {",
        "        return;",
        "    }",
        "    let unbounded = state.budget_ms == 0;",
        "    let deadline = std::time::Instant::now()",
        "        + std::time::Duration::from_millis(state.budget_ms);",
        "    for pending in queued {",
        "        if !unbounded && std::time::Instant::now() >= deadline {",
        "            eprintln!(",
        '                "revl: compensation {:?} skipped (deadline-expired, budget={}ms)",',
        "                pending.label, state.budget_ms,",
        "            );",
        "            continue;",
        "        }",
        "        let label = pending.label;",
        "        let outcome = std::panic::catch_unwind(",
        "            std::panic::AssertUnwindSafe(pending.call));",
        "        if outcome.is_err() {",
        '            eprintln!("revl: compensation {:?} failed", label);',
        "        }",
        "    }",
        "}",
        "",
    ]


def _revl_record_preamble() -> list[str]:
    """item 322 Slice 2: the durable WAL recording sink — the rust host
    recording channel, the faithful mirror of backends/go/emit.py's
    `_RECORD_PREAMBLE`.

    A witnessed transactional step, in record mode, writes the re-issuable
    inverse's discharge-descriptor to a host-visible WAL file (`$REVL_WAL`) and
    fsyncs it (`File::sync_all`) BEFORE the emitting call returns, so a crash
    before commit still leaves the inverse re-issuable from the log alone —
    exactly the write-ahead discipline the py tier uses and the go tier
    mirrors. The JSONL schema is byte-for-byte the py one src/revl/wal.py
    documents (`header` / `discharge-descriptor` / `discharge` /
    `activation-complete`), read back by the tier-agnostic core with no py
    backend on the path.

    Gated: emitted only in record mode (`emit(ir, record=True)` /
    `emit.py --record`). Unset `REVL_WAL` makes every recording call a no-op, so
    the runtime is inert unless a host opts in; the whole preamble is absent when
    record mode is off, so every existing golden emits byte-identically.

    Direct-file-write, same mechanism as go (rust has a real filesystem). The
    sink is a process-global opened once (`OnceLock`) from `REVL_WAL`, stamping
    the header at open; the seq counter and committed-seq list live on it behind
    a `Mutex`, matching go's `revlWAL{mu, f, seq, seqs}`."""
    guarantee = (
        "the WAL records each committed effect's step identity, boundary "
        "classification and inverse DESCRIPTOR (not its closure). On restart, "
        "recovery runs the reconstructible boundary inverses newest-first (LIFO); "
        "in-process inverses are moot (their captured memory died with the "
        "process) and closure-only boundary inverses are reported as residue, "
        "never silently claimed to have run."
    )
    return [
        "// ---- durable WAL recording sink (item 322 Slice 2, the rust host recording channel) ----",
        "",
        "/// The single sentence recovery is allowed to claim, written verbatim into",
        "/// every WAL header — byte-identical to src/revl/wal.py's `WAL_GUARANTEE`.",
        f"const REVL_WAL_GUARANTEE: &str = {_string(guarantee)};",
        "",
        "/// The process's durable append-only log. One line per record, JSON,",
        "/// flushed + fsync'd (`sync_all`) before the call that wrote it returns —",
        "/// the write-ahead discipline the py tier uses, so a record a caller saw",
        "/// acknowledged is on disk before the effect it describes is allowed to",
        "/// matter. Mirrors the go tier's `revlWAL`.",
        "struct RevlWal {",
        "    file: std::fs::File,",
        "    seq: u64,",
        "    seqs: Vec<u64>,",
        "}",
        "",
        "impl RevlWal {",
        "    fn write(&mut self, rec: &serde_json::Value) {",
        "        use std::io::Write;",
        "        if let Ok(mut line) = serde_json::to_string(rec) {",
        "            line.push('\\n');",
        "            let _ = self.file.write_all(line.as_bytes());",
        "            let _ = self.file.sync_all(); // fsync per record",
        "        }",
        "    }",
        "}",
        "",
        "static REVL_WAL_SINK: std::sync::OnceLock<Option<std::sync::Mutex<RevlWal>>>",
        "    = std::sync::OnceLock::new();",
        "",
        "/// Wire the sink to `REVL_WAL` (unset -> no-op recording) and stamp the",
        "/// header the first time it is touched. Opened append-only so a producer",
        "/// process writes one continuous log.",
        "fn revl_wal_sink() -> Option<&'static std::sync::Mutex<RevlWal>> {",
        "    REVL_WAL_SINK",
        "        .get_or_init(|| {",
        '            let path = match std::env::var("REVL_WAL") {',
        "                Ok(p) if !p.is_empty() => p,",
        "                _ => return None,",
        "            };",
        "            let file = match std::fs::OpenOptions::new()",
        "                .create(true).write(true).append(true).open(&path)",
        "            {",
        "                Ok(f) => f,",
        "                Err(_) => return None,",
        "            };",
        "            let mut wal = RevlWal { file, seq: 0, seqs: Vec::new() };",
        "            wal.write(&serde_json::json!({",
        '                "record": "header", "walVersion": 1, "generation": 1,',
        '                "guarantee": REVL_WAL_GUARANTEE,',
        "            }));",
        "            Some(std::sync::Mutex::new(wal))",
        "        })",
        "        .as_ref()",
        "}",
        "",
        "/// Append one witnessed transactional inverse's discharge-descriptor: the",
        "/// re-issuable named call `{receiver, method, args}` recover replays LIFO to",
        "/// undo the mutation, plus the forward `origin` it reverses. Fsync'd before",
        "/// it returns, so a crash after this call still leaves the inverse",
        "/// re-issuable from the log alone. No-op when `REVL_WAL` is unset.",
        "pub fn revl_record_transactional(receiver: &str, method: &str, args: Vec<String>) {",
        "    if let Some(sink) = revl_wal_sink() {",
        "        let mut wal = sink.lock().unwrap();",
        "        let seq = wal.seq;",
        "        wal.seq += 1;",
        "        wal.seqs.push(seq);",
        '        let call = serde_json::json!({ "receiver": receiver, "method": method, "args": args });',
        "        wal.write(&serde_json::json!({",
        '            "record": "discharge-descriptor", "seq": seq, "entry": "transactional",',
        '            "call": call, "origin": call, "witness": serde_json::Value::Null,',
        '            "idempotency": serde_json::Value::Null,',
        "        }));",
        "    }",
        "}",
        "",
        "/// The commit-path proof that every recorded transactional seq COMMITTED,",
        "/// so recover SKIPS it — a committed transaction is never rolled back.",
        "/// Called on a clean unload, never on a crash.",
        "pub fn revl_record_discharge() {",
        "    if let Some(sink) = revl_wal_sink() {",
        "        let mut wal = sink.lock().unwrap();",
        "        let seqs = wal.seqs.clone();",
        '        wal.write(&serde_json::json!({ "record": "discharge", "discharged": seqs }));',
        "    }",
        "}",
        "",
        "/// The terminal marker: its PRESENCE is the whole roll-forward decision,",
        "/// its ABSENCE (a crash) is roll-back. Written only after a clean unload.",
        "pub fn revl_record_activation_complete() {",
        "    if let Some(sink) = revl_wal_sink() {",
        "        let mut wal = sink.lock().unwrap();",
        "        wal.write(&serde_json::json!({",
        '            "record": "activation-complete", "generation": 1, "components": []',
        "        }));",
        "    }",
        "}",
        "",
    ]


def _uses_stdlib(ir: dict) -> bool:
    """True when any builtin/len node appears anywhere in the document — or a
    sized `.length` field (item 104): a component-position property-form
    `.length` stays a `field` node marked `sized_length`, and its emit routes
    through the `revl_length` helper trait, so the trait must be emitted for it
    too (else `String::revl_length` is an undefined method, E0599)."""
    found = False

    def walk(node) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            if node.get("kind") in ("builtin", "len") or (
                    node.get("kind") == "field" and node.get("sized_length")):
                found = True
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(ir.get("components"))
    walk(ir.get("functions"))
    walk(ir.get("tests"))
    return found


def _uses_float_interp(ir: dict) -> bool:
    """True when any `${…}` template interpolates a provably-`Float`
    expression, so the canonical Float renderer is emitted only then."""
    found = False

    def walk(node) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            if node.get("kind") == "interp":
                for part in node.get("parts") or []:
                    if (isinstance(part, (list, tuple)) and len(part) == 2
                            and part[0] == "expr" and _v3_is_float(part[1])):
                        found = True
                        return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(ir.get("components"))
    walk(ir.get("functions"))
    walk(ir.get("tests"))
    return found


def _revl_ftoa_helper() -> list[str]:
    """Canonical Float -> Str: ECMAScript Number::toString (docs/strings.md).
    `{:e}` supplies the shortest round-trip digits; this reformats them into
    the ES notation so `${aFloat}` agrees with every other tier."""
    return [
        "fn revl_ftoa(x: f64) -> String {",
        '    if x.is_nan() { return "NaN".to_string(); }',
        "    if x.is_infinite() {",
        '        return (if x < 0.0 { "-Infinity" } else { "Infinity" }).to_string();',
        "    }",
        '    if x == 0.0 { return "0".to_string(); }',
        '    let sign = if x < 0.0 { "-" } else { "" };',
        '    let s = format!("{:e}", x.abs());',
        "    let (mant, exp): (&str, i64) = match s.find('e') {",
        "        Some(i) => (&s[..i], s[i + 1..].parse().unwrap()),",
        "        None => (s.as_str(), 0),",
        "    };",
        "    let (intpart, frac) = match mant.find('.') {",
        "        Some(i) => (&mant[..i], &mant[i + 1..]),",
        '        None => (mant, ""),',
        "    };",
        '    let mut digits = format!("{}{}", intpart, frac);',
        "    let mut point = intpart.len() as i64 + exp;",
        "    let mut lead = 0;",
        "    let b = digits.clone();",
        "    let bb = b.as_bytes();",
        "    while lead + 1 < bb.len() && bb[lead] == b'0' { lead += 1; point -= 1; }",
        "    digits = digits[lead..].to_string();",
        "    while digits.len() > 1 && digits.ends_with('0') { digits.pop(); }",
        '    if digits == "0" { return "0".to_string(); }',
        "    let k = digits.len() as i64;",
        "    let n = point;",
        "    let body = if k <= n && n <= 21 {",
        '        format!("{}{}", digits, "0".repeat((n - k) as usize))',
        "    } else if 0 < n && n <= 21 {",
        "        let p = n as usize;",
        '        format!("{}.{}", &digits[..p], &digits[p..])',
        "    } else if -6 < n && n <= 0 {",
        '        format!("0.{}{}", "0".repeat((-n) as usize), digits)',
        "    } else {",
        "        let e = n - 1;",
        "        let m = if k > 1 {",
        '            format!("{}.{}", &digits[..1], &digits[1..])',
        "        } else {",
        "            digits.clone()",
        "        };",
        '        let (esign, ea) = if e < 0 { ("-", -e) } else { ("+", e) };',
        '        format!("{}e{}{}", m, esign, ea)',
        "    };",
        '    format!("{}{}", sign, body)',
        "}",
    ]


# --------------------------------------------------------------------------
# interop bridge (docs/interop-bridge.md §3): generated per-service proxy +
# stub, so a cordis-rs process can consume/serve a cross-process key. cordis-rs
# services are static traits, so this is codegen (a runtime-generic proxy is
# impossible in Rust, unlike py attribute access or node's `Proxy`).
# --------------------------------------------------------------------------

# Scalar returns get lenient, hand-written JSON conversions (byte-identical to
# the first cut, so the golden holds). Anything else that is not the opaque
# cordis `Value` is marshalled generically through serde (records, ADTs, Map,
# Result, and their Vec/Option nestings, once the emitted types derive
# Serialize/Deserialize). `Value` alone stays unmarshallable: it is opaque.
_SCALAR_OPTION = ("Option<String>", "Option<i64>", "Option<bool>", "Option<f64>")


def _bridge_serde_ok(rtype: str) -> bool:
    """True when `rtype` can cross via serde: not the opaque cordis `Value`."""
    return not re.search(r"\bValue\b", rtype)


def _split_result(rtype: str):
    """`Result<T, E>` -> (T, E), else None. std `Result` serializes
    externally-tagged, so the bridge encodes it to the canonical
    `{"$kind","$value"}` by hand rather than via serde."""
    match = re.match(r"^Result<(.+)>$", rtype.strip())
    if not match:
        return None
    inner, depth = match.group(1), 0
    for i, ch in enumerate(inner):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            return inner[:i].strip(), inner[i + 1:].strip()
    return None


def _result_ok_err(value: str, ok_ty: str, err_ty: str) -> str:
    """Rust expr building a std Result from a canonical `{"$kind","$value"}`."""
    payload = '_r.get("$value").cloned().unwrap_or(serde_json::Value::Null)'
    return (f'{{ let _r = {value}.clone(); '
            f'if _r.get("$kind").and_then(|k| k.as_str()) == Some("Ok") {{ '
            f'Ok(serde_json::from_value::<{ok_ty}>({payload}).expect("bridge: decode Ok")) }} '
            f'else {{ Err(serde_json::from_value::<{err_ty}>({payload}).expect("bridge: decode Err")) }} }}')


def _result_to_json(call: str) -> str:
    """Rust expr encoding a std Result to the canonical `{"$kind","$value"}`."""
    return (f'{{ match {call} {{ '
            f'Ok(_v) => serde_json::json!({{"$kind": "Ok", "$value": serde_json::to_value(&_v).unwrap_or(serde_json::Value::Null)}}), '
            f'Err(_e) => serde_json::json!({{"$kind": "Err", "$value": serde_json::to_value(&_e).unwrap_or(serde_json::Value::Null)}}) }} }}')


def _bridge_arg_ser(name: str, rtype: str) -> str:
    """Serialize a proxy method argument to a serde_json::Value."""
    if rtype in ("String", "i64", "f64", "bool"):
        return f"serde_json::json!({name})"
    if _split_result(rtype):
        return _result_to_json(name)
    if _bridge_serde_ok(rtype):
        return f"serde_json::to_value(&{name}).unwrap_or(serde_json::Value::Null)"
    return "serde_json::Value::Null"  # opaque Value param: not marshalled


def _bridge_arg_extract(index: int, rtype: str) -> str | None:
    """Extract a stub-dispatch argument from `args[index]` at type `rtype`."""
    if rtype == "String":
        return f'args[{index}].as_str().unwrap_or("").to_string()'
    if rtype == "i64":
        return f"args[{index}].as_i64().unwrap_or(0)"
    if rtype == "f64":
        return f"args[{index}].as_f64().unwrap_or(0.0)"
    if rtype == "bool":
        return f"args[{index}].as_bool().unwrap_or(false)"
    result = _split_result(rtype)
    if result:
        return _result_ok_err(f"args[{index}]", result[0], result[1])
    if _bridge_serde_ok(rtype):
        return f'serde_json::from_value::<{rtype}>(args[{index}].clone()).expect("bridge: decode arg")'
    return None  # opaque Value param: this method is not served


def _bridge_ret_deser(value: str, rtype: str) -> str | None:
    """Turn a reply serde_json::Value into the method's Rust return type."""
    table = {
        "i64": f"{value}.as_i64().unwrap_or(0)",
        "String": f'{value}.as_str().unwrap_or("").to_string()',
        "bool": f"{value}.as_bool().unwrap_or(false)",
        "f64": f"{value}.as_f64().unwrap_or(0.0)",
        "()": f"{{ let _ = {value}; }}",
        "Option<String>": f"{value}.as_str().map(|s| s.to_string())",
        "Option<i64>": f"{value}.as_i64()",
        "Option<bool>": f"{value}.as_bool()",
        "Vec<Value>": (f"{value}.as_array().map(|a| a.iter().map(|x| "
                       f"Value::new(x.to_string())).collect()).unwrap_or_default()"),
    }
    if rtype in table:
        return table[rtype]
    result = _split_result(rtype)
    if result:
        return _result_ok_err(value, result[0], result[1])
    if _bridge_serde_ok(rtype):
        return f'serde_json::from_value::<{rtype}>({value}.clone()).expect("bridge: decode return")'
    return None  # opaque cordis Value: not marshallable


def _bridge_ret_ser(call: str, rtype: str) -> str:
    """Serialize a stub method's return value to a serde_json::Value."""
    if rtype in ("i64", "String", "bool", "f64") or rtype in _SCALAR_OPTION:
        return f"serde_json::json!({call})"
    if rtype == "()":
        return f"{{ {call}; serde_json::Value::Null }}"
    if rtype == "Vec<Value>":
        return (f"{{ let _r = {call}; serde_json::json!(_r.iter().map(|v| "
                f"v.downcast::<String>().map(|s| (*s).clone()).unwrap_or_default())"
                f".collect::<Vec<_>>()) }}")
    if _split_result(rtype):
        return _result_to_json(call)
    if _bridge_serde_ok(rtype):
        return f"{{ let _r = {call}; serde_json::to_value(&_r).unwrap_or(serde_json::Value::Null) }}"
    return f"{{ let _ = {call}; serde_json::Value::Null }}"  # opaque Value return


def _emit_bridge(ir: dict) -> list[str]:
    services = ir.get("services") or {}
    components = ir.get("components") or []
    types = ir.get("types") or {}
    if not services:
        return []
    provided: dict[str, str] = {}
    for component in components:
        for key, service in (component.get("provides") or {}).items():
            provided[key] = service

    out: list[str] = [
        "// ---- interop bridge (generated; docs/interop-bridge.md) ----",
        "use std::io::{BufRead, Write};",
        "use std::os::unix::net::UnixStream;",
        "",
        "fn _revl_rpc(socket: &str, key: &str, method: &str, args: Vec<serde_json::Value>) -> serde_json::Value {",
        "    let request = serde_json::json!({ \"key\": key, \"method\": method, \"args\": args });",
        "    let mut stream = None;",
        "    for _ in 0..200 {",
        "        match UnixStream::connect(socket) {",
        "            Ok(s) => { stream = Some(s); break; }",
        "            Err(_) => std::thread::sleep(std::time::Duration::from_millis(50)),",
        "        }",
        "    }",
        "    let stream = stream.expect(\"bridge connect (provider never came up)\");",
        "    let mut writer = stream.try_clone().expect(\"clone stream\");",
        "    let mut line = serde_json::to_string(&request).unwrap();",
        "    line.push('\\n');",
        "    writer.write_all(line.as_bytes()).expect(\"write request\");",
        "    let mut reader = std::io::BufReader::new(stream);",
        "    let mut response = String::new();",
        "    reader.read_line(&mut response).expect(\"read reply\");",
        "    let reply: serde_json::Value = serde_json::from_str(&response).expect(\"parse reply\");",
        "    if !reply[\"ok\"].as_bool().unwrap_or(false) {",
        "        panic!(\"bridge remote error: {}\", reply[\"error\"]);",
        "    }",
        "    reply[\"value\"].clone()",
        "}",
        "",
    ]

    for sname, service in services.items():
        methods = service.get("methods") or {}
        # consumer-side proxy
        out.append(f"pub struct {sname}Proxy {{ pub socket: String, pub key: String }}")
        out.append(f"impl {sname} for {sname}Proxy {{")
        for mname, method in methods.items():
            params = method.get("params") or []
            plist = ", ".join(f"{p['name']}: {_rust_type(p.get('type'), types)}" for p in params)
            ret = _rust_type(method.get("returns"), types) if method.get("returns") else "()"
            argvec = ", ".join(_bridge_arg_ser(p["name"], _rust_type(p.get("type"), types)) for p in params)
            deser = _bridge_ret_deser("_v", ret)
            out.append(f"    fn {_mname(mname)}(&self, {plist}) -> {ret} {{")
            if deser is None:
                out.append(f'        panic!("bridge proxy: unsupported return type for {sname}.{mname}");')
            else:
                out.append(f'        let _v = _revl_rpc(&self.socket, &self.key, "{mname}", vec![{argvec}]);')
                out.append(f"        {deser}")
            out.append("    }")
        out.append("}")
        # provider-side dispatch
        out.append(f"fn _revl_dispatch_{_snake(sname)}(svc: &dyn {sname}, method: &str, "
                   "args: &[serde_json::Value]) -> serde_json::Value {")
        out.append("    match method {")
        for mname, method in methods.items():
            params = method.get("params") or []
            ret = _rust_type(method.get("returns"), types) if method.get("returns") else "()"
            extracts = [_bridge_arg_extract(i, _rust_type(p.get("type"), types)) for i, p in enumerate(params)]
            if any(e is None for e in extracts):
                out.append(f'        "{mname}" => serde_json::Value::Null, // unmarshalled param type')
                continue
            call = f"svc.{_mname(mname)}({', '.join(extracts)})"
            out.append(f'        "{mname}" => {_bridge_ret_ser(call, ret)},')
        out.append("        _ => serde_json::Value::Null,")
        out.append("    }")
        out.append("}")
        out.append("")

    # key -> service name (over provided keys)
    out.append("pub fn _revl_service_of(key: &str) -> Option<&'static str> {")
    out.append("    match key {")
    for key, service in provided.items():
        out.append(f'        "{key}" => Some("{service}"),')
    out.append("        _ => None,")
    out.append("    }")
    out.append("}")
    out.append("")

    # consumer: a plugin that provides `key` via the right proxy
    out.append("pub fn _revl_proxy_plugin(key: &str, service: &str, socket: String) "
               "-> Option<cordis::PluginHandle> {")
    out.append("    let key_string = key.to_string();")
    out.append("    match service {")
    for sname in services:
        out.append(f'        "{sname}" => Some(cordis::plugin_sync::<(), _>(')
        out.append(f'            "{sname}Proxy",')
        out.append("            cordis::Inject::none(),")
        out.append("            move |ctx, _config| {")
        out.append(f"                let proxy: Box<dyn {sname}> = Box::new({sname}Proxy "
                   "{ socket: socket.clone(), key: key_string.clone() });")
        out.append("                ctx.provide(key_string.as_str(), proxy)?;")
        out.append("                Ok(cordis::PluginOutput::none())")
        out.append("            },")
        out.append("        )),")
    out.append("        _ => None,")
    out.append("    }")
    out.append("}")
    out.append("")

    # provider/probe: require a locally-provided key and dispatch to it
    out.append("pub fn _revl_invoke(ctx: &cordis::Context, key: &str, method: &str, "
               "args: &[serde_json::Value]) -> serde_json::Value {")
    out.append("    match key {")
    for key, service in provided.items():
        out.append(f'        "{key}" => match ctx.require::<Box<dyn {service}>>("{key}") {{')
        out.append(f"            Ok(svc) => _revl_dispatch_{_snake(service)}(&**svc, method, args),")
        out.append("            Err(_) => serde_json::Value::Null,")
        out.append("        },")
    out.append("        _ => serde_json::Value::Null,")
    out.append("    }")
    out.append("}")
    out.append("")

    # component name -> plugin handle
    out.append("pub fn plugin_by_name(name: &str) -> Option<cordis::PluginHandle> {")
    out.append("    match name {")
    for component in components:
        snake = _snake(component["name"])
        out.append(f'        "{snake}" => Some({snake}()),')
    out.append("        _ => None,")
    out.append("    }")
    out.append("}")
    out.append("")

    # component name -> context isolated for that component's realm placements.
    # cordis fixes a fiber's isolation scope at plug time (its `Inject` gate is
    # evaluated against this context, before the plugin body runs), so every
    # plug site must isolate the context HERE rather than inside the plugin
    # body. This mirrors the python/typescript `plug()` helper and is what
    # lets an isolated `requires kv in realm("t")` reactively link to an
    # isolated provider in the same realm instead of hanging Pending.
    out.append("pub fn _revl_isolate_ctx(ctx: &cordis::Context, name: &str) -> cordis::Context {")
    out.append("    match name {")
    for component in components:
        isolate = component.get("isolate") or {}
        if not isolate:
            continue
        snake = _snake(component["name"])
        expr = "ctx"
        for key, realm in isolate.items():
            expr += f".isolate_with({_string(key)}, _revl_realm({_string(realm)}))"
        out.append(f'        "{snake}" => {expr},')
    out.append("        _ => ctx.clone(),")
    out.append("    }")
    out.append("}")
    out.append("")

    # component name -> loaded Fiber, building the typed config from the
    # placement spec's `config` object (keyed by component name).
    out.append("pub fn _revl_load(ctx: &cordis::Context, name: &str, config: &serde_json::Value) "
               "-> Option<cordis::Fiber> {")
    out.append("    match name {")
    for component in components:
        snake = _snake(component["name"])
        pascal = component["name"]
        fields = component.get("config") or []
        out.append(f'        "{snake}" => {{')
        # Isolate the registration context so the fiber carries the realm
        # scope its reactive gate and provisions resolve against.
        out.append(f'            let ctx = _revl_isolate_ctx(ctx, "{snake}");')
        if fields:
            out.append(f'            let _c = config.get("{pascal}").cloned().unwrap_or(serde_json::Value::Null);')
            out.append(f"            Some(ctx.plugin({snake}(), {pascal}Config {{")
            for field in fields:
                fname = _ident(field.get("name"), "config field")
                ftype = _rust_type(field.get("type"))
                default = _config_default_lit(field.get("default"), ftype)
                if ftype == "String":
                    out.append(f'                {fname}: _c.get("{fname}").and_then(|v| v.as_str())'
                               f".map(|s| s.to_string()).unwrap_or_else(|| " + default + "),")
                elif ftype == "i64":
                    out.append(f'                {fname}: _c.get("{fname}").and_then(|v| v.as_i64())'
                               f".unwrap_or({default}),")
                elif ftype == "f64":
                    out.append(f'                {fname}: _c.get("{fname}").and_then(|v| v.as_f64())'
                               f".unwrap_or({default}),")
                elif ftype == "bool":
                    out.append(f'                {fname}: _c.get("{fname}").and_then(|v| v.as_bool())'
                               f".unwrap_or({default}),")
                else:
                    out.append(f"                {fname}: {default},")
            out.append("            }))")
        else:
            out.append(f"            Some(ctx.plugin({snake}(), ()))")
        out.append("        },")
    out.append("        _ => None,")
    out.append("    }")
    out.append("}")
    out.append("")
    return out


def _uses_teardown(components: list, externs: list) -> bool:
    """item 243 Slice 2b: does any component need `RevlTeardown` (a
    `witnessed` extern call, or `emit ... compensate`, activation- or
    method-body)? Gates `_revl_teardown_preamble` so a program using
    neither emits byte-identically to before this slice."""
    witnessed = {ext["name"]: ext for ext in externs if ext.get("class") == "witnessed"}
    return any(_component_needs_teardown(c, witnessed) for c in components)


def _emit_components(ir: dict, components: list) -> list[str]:
    out: list[str] = []
    out.extend(_emit_service_traits(ir.get("services") or {}, ir.get("types") or {}))
    out.extend(_emit_host_stubs(ir))
    if _uses_timer(components) or _tests_use_advance(ir.get("tests") or []):
        out.extend(_revl_timer_preamble())
    if _uses_stdlib(ir):
        out.extend(_stdlib_helper_traits())
    if _uses_float_interp(ir):
        out.extend(_revl_ftoa_helper())
    if _needs_realm_helper(components):
        out.extend(_revl_realm_helper(components))
    if _uses_spawn(components):
        out.extend(_revl_spawn_handle())
    if _uses_teardown(components, ir.get("externs") or []):
        out.extend(_revl_teardown_preamble())
        if _RECORD_MODE:
            # item 322 Slice 2: the durable WAL sink rides alongside the teardown
            # accumulator (a witnessed transactional step needs both). Gated so a
            # non-record emission — every existing golden — is byte-identical.
            out.extend(_revl_record_preamble())
    for component in components:
        out.extend(_emit_component_auto(component, ir.get("services") or {}, ir))
    out.extend(_emit_bridge(ir))
    return out


def _emit_v1(ir: dict) -> str:
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")
    out = _module_header(1)
    out.extend(_emit_components(ir, components))
    return "\n".join(out).rstrip() + "\n"


def _emit_v2(ir: dict) -> str:
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")
    out = _module_header(2)
    out.extend(_emit_components(ir, components))
    return "\n".join(out).rstrip() + "\n"


def _emit_v3(ir: dict) -> str:
    components = ir.get("components") or []
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    if not components and not types and not functions and not externs and not tests:
        raise EmitError("IR document has no components, types, functions, externs, or tests")
    out = _module_header(3)
    if types:
        out.extend(_emit_v3_types(types))
    if externs:
        out.extend(_emit_v3_externs(externs, types))
    if functions:
        out.extend(_emit_v3_functions(functions, types, externs))
    if tests:
        pure = [t for t in tests if not t.get("lifecycle")]
        lifecycle = [t for t in tests if t.get("lifecycle")]
        if pure:
            out.extend(_emit_v3_tests(pure, types, functions, externs))
        if lifecycle:
            out.extend(_emit_v3_lifecycle_tests(
                lifecycle, types, functions, externs,
                ir.get("services") or {}, components))
    out.extend(_emit_components(ir, components))
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------ typed holes

def _refuse_holes(ir: dict) -> None:
    """A typed hole is an unmet obligation, not code (docs/holes.md).

    Emitting one would put a placeholder into Rust and make rustc the
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
        f"refusing to emit Rust: this document still has {len(found)} typed "
        f"hole(s) — {where}. A hole type-checks so the surrounding draft can "
        f"be checked, but it has no implementation and there is nothing to "
        f"lower. Fill every hole, then emit (docs/holes.md)."
    )

# A `fault test` is executed by driving a real activation and inspecting the
# runtime's residue afterwards (docs/fault-tests.md).  The cordis-rs tier
# has no such driver, so it is refused loudly instead of being dropped on the
# floor: a silently-missing fault test is a guarantee nobody is checking.
def _refuse_fault_tests(ir) -> None:
    fault_tests = (ir or {}).get("fault_tests") or []
    if not fault_tests:
        return
    names = ", ".join(repr(unit.get("name")) for unit in fault_tests)
    raise EmitError(
        f"fault tests do not lower to the cordis-rs tier ({names}) — `fault test` runs "
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
        refuse_deferred_on_ownerless_tier(ir, "rust")
        refuse_approval_on_ownerless_tier(ir, "rust")
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


def emit(ir: dict, record: bool = False) -> str:
    """Emit one Rust module (crate root) for an IR document.

    `record=True` (item 322 Slice 2) wires the witnessed teardown to a durable
    WAL sink (the rust host recording channel) so a crash BEFORE commit is
    recoverable by `revl recover`. It is OFF by default and gated everywhere it
    touches emission, so a non-recording program — every existing golden — emits
    byte-identically; only a program emitted in record mode carries the
    recording preamble and the per-descriptor `revl_record_transactional` calls.
    Mirrors backends/go/emit.py's `emit(..., record=...)`.
    """
    global _RECORD_MODE
    _RECORD_MODE = record
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    ir = _dedup_colour_erased_poly_externs(ir)  # item 388, stage 6
    _refuse_holes(ir)
    _refuse_deferred_emissions(ir)

    _refuse_fault_tests(ir)

    version = ir.get("ir_version")
    if version == 1:
        return _emit_v1(ir)
    if version == 2:
        return _emit_v2(ir)
    if version == 3:
        return _emit_v3(ir)
    raise EmitError(
        f"unsupported ir_version: {version!r} — the Rust backend targets "
        f"ir_version 1, 2 (realms), and 3 (types/functions/match)"
    )


def cargo_toml(name: str = "revl_components") -> str:
    """A minimal Cargo.toml for the emitted crate."""
    return (
        "[package]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
        "\n"
        "[dependencies]\n"
        'cordis = { package = "cordis-rs", version = "0.3" }\n'
        'serde = { version = "1", features = ["derive"] }\n'
        'serde_json = "1"\n'
    )


def _main(argv: list[str]) -> int:
    # item 322 Slice 2: `--record` wires the witnessed teardown to a durable WAL
    # sink (mirrors backends/go/emit.py's flag). Off by default -> byte-identical.
    args = [a for a in argv[1:] if a != "--record"]
    record = "--record" in argv[1:]
    if len(args) != 1:
        print("usage: python3 emit.py <ir.json|-> [--record]", file=sys.stderr)
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
    sys.stdout.write(emit(ir, record=record))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))





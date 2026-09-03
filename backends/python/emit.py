"""revl backend-IR → cordis-py emitter.

``emit(ir)`` turns one IR document (docs/backend-ir.md, ir_version 0) into a
single idiomatic Python module: one plugin dict per component, lowered onto
the cordis-py runtime.

Lowering scheme (see runtime.Frame for the R1 rationale):

The per-activation context handle, resolved config, and component Frame are
bound in emitted body scope as ``_revl_ctx`` / ``_revl_config`` /
``_revl_frame`` — the reserved ``_revl_*`` namespace no user identifier can
enter — so a revl var named ``ctx`` no longer collides (item 156).

* the whole component body compiles to **one** ``_revl_ctx.effect(generator)``;
  each ``let-effect`` / ``effect`` step yields its undo expression as the
  inverse, in step order, so the runtime's per-effect LIFO disposer is the
  component's accumulator;
* ``provide`` steps yield the runtime's own ``_revl_ctx.provide`` effect into
  that accumulator and populate it with ``_revl_ctx.set`` — the withdrawal
  inverse is entirely runtime-derived (R5);
* ``req`` expressions compile to ``_revl_ctx.<name>`` committed-view attribute
  access, which stays readable during the component's own teardown (R3);
* ``effect`` steps inside provide-method bodies compile to ``_revl_ctx.effect``
  calls adopted by the component's Frame, joining the accumulator (R1);
* ``emit`` steps compile to plain calls — nothing accumulated.
"""

from __future__ import annotations

import copy
import keyword
import re
import textwrap
from typing import Any, Optional

IR_VERSION = 1

_HOST_ROOTS = {"Pool", "Map", "Job", "Stream"}

# The module-level `from runtime import …` names the emitter injects. Every one
# of them shared the same failure mode: a user identifier the checker accepts
# would collide with the import in module scope and blow up (or silently shadow)
# deep in this backend. Two coherent fixes, applied by kind (roadmap items 156,
# 160):
#
#   * The per-activation scaffolding locals (`_revl_ctx` / `_revl_config` /
#     `_revl_frame`) already live in the reserved `_revl_*` namespace, which
#     `_ident` forbids any user identifier from entering (item 156).
#
#   * `_IMPORT_ALIAS` extends that trick to the pure-emitter runtime imports —
#     the format/frame/config-schema helpers, the spawn/timer/lifecycle drivers.
#     None of these are language-surface builtins the user names directly, so
#     the emitter fully controls every reference site: each is imported `as
#     _revl_<name>` and referenced through the alias, so a user identifier
#     `fmt` / `Frame` / `schedule_every` / … now compiles and RUNS (item 160).
#     A name in this map is therefore NOT reserved.
#
# What CANNOT be aliased is the host-root triple `Pool` / `Map` / `Job`
# (`_HOST_ROOTS`): these are language builtins the user writes verbatim
# (`Map.new()`), and in v3 fn/test bodies a builtin reference and a user var of
# the same name are the identical `var` node — the emitter cannot tell them
# apart to rewrite only one. Aliasing is intractable, so they stay guarded, now
# uniformly: `Job` used to be missing from `_RESERVED` (unguarded — a spurious
# `from runtime import Job` was emitted for a user `Job` and silently shadowed
# it; the builtin would break if actually used), and joins its siblings here.
# `self` is the emitted Python method receiver; the frontend's `_safe_name`
# already mangles a user `self` to `self_`, so it can never reach this backend,
# but it stays listed as defense-in-depth.
_IMPORT_ALIAS = {
    "fmt": "_revl_fmt",
    "Frame": "_revl_Frame",
    "ConfigSchema": "_revl_ConfigSchema",
    "spawn": "_revl_spawn",
    "schedule_every": "_revl_schedule_every",
    "schedule_after": "_revl_schedule_after",
    "plug": "_revl_plug",
    "set_trace": "_revl_set_trace",
    "retry_idempotent": "_revl_retry_idempotent",
    "validate_response": "_revl_validate",
    "validate_retry": "_revl_validate_retry",
    "validate_retry_async": "_revl_validate_retry_async",
    "Clock": "_revl_Clock",
    "SessionOwner": "_revl_SessionOwner",
    "set_session_owner": "_revl_set_session_owner",
    "clear_session_owner": "_revl_clear_session_owner",
    "mark_secret": "_revl_mark_secret",
    "secret_result": "_revl_secret_result",
}
_RESERVED = _HOST_ROOTS | {"self"}


def _runtime_ref(name: str) -> str:
    """The in-module name a runtime import is referenced by: its `_revl_*`
    alias when it has one, else the bare name (the guarded host roots)."""
    return _IMPORT_ALIAS.get(name, name)

# Members cordis-py's Context already owns. A provision key colliding with one
# used to compile and then die at activation with `property "runtime" is
# already declared as accessor`; the host knows its own surface, so the
# rejection belongs here rather than in the (host-agnostic) frontend.
# Derived from `dir(Context())` plus the mixin-provided members that resolve
# through __getattr__ (fiber/registry/events/reflect mixins).
_CONTEXT_MEMBERS = {
    "baseUrl", "events", "extend", "fiber", "intercept", "is_", "isolate",
    "logger", "reflect", "registry", "root",
    "runtime", "effect", "inject", "plugin",
    "on", "once", "emit", "parallel", "serial", "bail", "waterfall",
    "get", "set", "provide", "accessor", "mixin",
}


class EmitError(ValueError):
    """The IR document cannot be lowered by this backend."""


# Dispatcher conformance (roadmap item 76a). This file carries TWO expression
# dispatchers — `_ComponentEmitter._expr` (component/method bodies) and the
# module-level `_expr` (fn bodies) — and the sets below declare, as data, the
# IR expression kinds each one must render. tests/test_expr_dispatcher_
# conformance.py checks them against the frontend schema (src/revl/lower.py:
# EXPR_KINDS / EXPR_KINDS_FN / EXPR_KINDS_COMPONENT): a kind the frontend can
# produce in a position must be handled or deliberately refused by every
# dispatcher that serves that position, and a dispatcher's declared set must
# match the branches in its source. "Did you patch both paths" is a red test,
# not a 15-minute stall. `hole` is refused at the document level by the
# pre-emit walk, so it never reaches either dispatcher.
EXPR_DISPATCHERS: dict[str, frozenset[str]] = {
    "component": frozenset({
        "adt", "arrow", "bin", "builtin", "call", "config", "field", "fn",
        "format", "host", "if", "index", "instance-get", "list", "lit",
        "maplit", "match", "name", "optcall", "optfield", "record",
        "record_update", "req", "spawn", "un", "var",
    }),
    "fn": frozenset({
        "adt", "arrow", "bin", "builtin", "call", "field", "if", "index",
        "interp", "len", "list", "lit", "maplit", "match", "optcall",
        "optfield", "record", "record_update", "un", "var",
    }),
}
# kinds this tier deliberately refuses everywhere (document-level or
# position-agnostic); each must raise a named tier-limit EmitError, never the
# "unknown expression kind" fall-through.
EXPR_REFUSED: frozenset[str] = frozenset({"hole"})


# ---------------------------------------------------------------- async (item 92)
#
# py has no fn-level async machinery in v1/v2 — module fns are plain `def` and
# only provide methods go async. Item 92 adds `async def` colored fns and awaited
# call sites, mirroring the ts slice. Documents-wide facts drive the await
# decisions; they are set once at the top of `emit()` and read by both the module
# `_expr` and the `_ComponentEmitter` renderer.
#
# item 115 (async-extern.md §8): an *async* extern is no longer erased to a
# blocking `def`. It emits an `async def` (so its verbatim @py body may `await`
# a host operation — e.g. `await fiber.dispose()` in an `unload`) and every
# admitted call site awaits it, closing the finding-#32 gap that made a host
# suspension inexpressible on py. A NON-async extern still erases to a blocking
# `def` and is never awaited. So the py await-seed now matches ts's shape:
# ts awaits {async externs, colored fns, async locals}; py awaits
# {async externs, colored fns, async locals, async service ops}.
_PY_COLORED_FNS: set = set()        # module fn names emitted `async def`
_PY_ASYNC_EXTERNS: set = set()      # extern names emitted `async def` (item 115)
_PY_ASYNC_SVC_OPS: set = set()      # {(service_name, method_name)} async operations
# per-body context, threaded through the stateless module `_expr`:
_PY_AWAIT_LOCALS: set = set()       # async-typed parameter names of the body being rendered
_PY_IN_ASYNC: bool = False          # is the body being rendered an `async def`
_PY_IN_ARROW: bool = False          # is the body being rendered inside an arrow (item 141/264)
_PY_USES_AS_ASYNC: bool = False     # did any body need the `_revl_as_async` wrapper


def _py_yields_coroutine(node: Any, requires: Any = None,
                         async_locals: "frozenset[str]" = frozenset()) -> bool:
    """True if evaluating `node` produces an awaitable on the py tier — a call
    of an async service op through a req key, an async-colored module fn, an
    async extern (item 115: now an `async def`, no longer erased/blocking), or
    an async value local."""
    if not isinstance(node, dict):
        return False
    kind = node.get("kind")
    if kind == "call":
        target = node.get("target")
        if isinstance(target, dict) and target.get("kind") == "req" and requires:
            svc = requires.get(target.get("name"))
            if (svc, node.get("method")) in _PY_ASYNC_SVC_OPS:
                return True
        callee = node.get("callee")
        # a provision method call off a spawn handle (`w.<key>.<method>(...)`,
        # item 106): the receiver is an `instance-get` carrying the service
        # type the handle's key yields, frozen inline by the lowering. It
        # suspends exactly as a `req`-target async op does — mirror that check
        # against the same async-op table so an arrow tail-calling a spawned
        # async worker renders as a plain coroutine lambda, not a sync wrap.
        if isinstance(callee, dict) and callee.get("kind") == "field":
            recv = callee.get("target")
            if isinstance(recv, dict) and recv.get("kind") == "instance-get" \
                    and (recv.get("service"), callee.get("name")) in _PY_ASYNC_SVC_OPS:
                return True
        if isinstance(callee, dict) and callee.get("kind") == "var":
            nm = callee.get("name")
            if nm in _PY_COLORED_FNS or nm in _PY_ASYNC_EXTERNS or nm in async_locals:
                return True
    if kind == "fn" and node.get("name") in (_PY_COLORED_FNS | _PY_ASYNC_EXTERNS):
        return True
    return False


def _py_reaches_coroutine(node: Any, requires: Any = None,
                          async_locals: "frozenset[str]" = frozenset()) -> bool:
    """True if a coroutine is produced anywhere in `node` — a nested async arrow
    is a value, pruned."""
    if isinstance(node, dict):
        if node.get("kind") == "arrow" and node.get("async"):
            return False
        if _py_yields_coroutine(node, requires, async_locals):
            return True
        return any(_py_reaches_coroutine(v, requires, async_locals)
                   for v in node.values())
    if isinstance(node, list):
        return any(_py_reaches_coroutine(v, requires, async_locals) for v in node)
    return False


def _py_async_arrow(body: Any, params: str, render, requires=None,
                    async_locals: "frozenset[str]" = frozenset()) -> str:
    """Emit an async-flagged arrow (item 92 §4). A lambda cannot be `async`, so
    three statically-decided shapes:
      1. a tail call of an async callable — the plain lambda returns the
         coroutine; the awaiting call site settles it. No wrapper.
      2. a statically sync body (the coerced mock arrow) — wrap in
         `_revl_as_async` so `await`-ing it yields the value.
      3. an internal (non-tail) await — inexpressible in a lambda; refused."""
    global _PY_USES_AS_ASYNC
    rendered_body = render(body)
    if _py_yields_coroutine(body, requires, async_locals):
        return f"lambda {params}: {rendered_body}"
    if _py_reaches_coroutine(body, requires, async_locals):
        raise EmitError(
            "an async arrow body on the py tier must be a single call of an "
            "async callable or fully sync — hoist the mixed body into a named "
            "fn (it will be async-colored) (docs/design/async-function-values.md)"
        )
    _PY_USES_AS_ASYNC = True
    return f"_revl_as_async(lambda {params}: {rendered_body})"


def _transactional_register_kwargs(ext: dict) -> str:
    """item 309: the extra `.transactional(...)` kwargs carrying an extern's
    idempotency register, or `""` when none is declared.

    Emitted ONLY when the author declared `undo idempotent` / `idempotent(key:)`,
    so a pre-309 witnessed extern's emitted code is byte-identical (additivity).
    The kwargs let the runtime fence-vs-free the abort Phase-1 apply and thread
    the register into the WAL descriptor a fresh-process recover reads."""
    parts: list = []
    if ext.get("undo_idempotent"):
        parts.append("undo_idempotent=True")
    if ext.get("register"):
        parts.append(f"register={ext['register']!r}")
    if ext.get("idempotency_key"):
        parts.append(f"idempotency={ext['idempotency_key']!r}")
    return (", " + ", ".join(parts)) if parts else ""


def _deferred_register_kwargs(ext: dict, args: list) -> str:
    """item 440 §(b): the extra `.enqueue_deferred(...)` kwargs that put a
    deferred emission's idempotency register — and the key's VALUE at this call
    site — onto the WAL descriptor, or `""` when the extern declares none.

    Emitted ONLY when the author declared `idempotent`/`idempotent(key: p)`, so a
    pre-440 deferred emission's emitted code is byte-identical. The KEY VALUE (not
    the parameter name) is what rides the log: a fresh-process re-issue must send
    the remote the same key it saw the first time, which is the argument at the
    key parameter's position, read off the descriptor. Without these, recover has
    no evidence about the emission and leaves it human-finish."""
    register = ext.get("register")
    if not register:
        return ""
    parts = [f"register={register!r}"]
    key = ext.get("idempotency_key")
    if key:
        names = [p.get("name") for p in ext.get("params") or []]
        if key in names:
            index = names.index(key)
            if index < len(args):
                parts.append(f"idempotency={args[index]}")
    return ", " + ", ".join(parts)


def _mangle(name: str) -> str:
    """Rename a syntactically-valid identifier that collides with a *Python*
    reserved word, so a valid revl identifier that happens to be a Python
    keyword (`from`, `class`, `lambda`, …) emits and RUNS instead of crashing
    at emit (roadmap item 165).

    The scheme is the A3 append-`_` rename `src/revl/lower.py::_safe_name` (and
    `backends/java/emit.py::_fn_name`) already use for revl-keyword bindings.
    It is a pure function of the name, so the declaration site and every use
    site agree without threading a table around, and it must ALSO be INJECTIVE:
    two distinct revl identifiers may never land on one Python identifier.

    The naive "append `_` while the name is a keyword" loop is a pure function
    but NOT injective: it maps `lambda` to `lambda_` and leaves the equally
    legal revl identifier `lambda_` alone, so both reach `lambda_` and the
    second binding silently CAPTURES the first (a local rebinding, a `def` that
    overwrites a `def`, a dataclass annotation that overwrites an annotation) —
    a wrong-value bug, not a compile error.

    The injective rule: escape a name iff the name OR any name reachable from
    it by dropping trailing `_` is a keyword, and escape it by exactly ONE `_`.
    That splits the identifier space in two halves that cannot meet. Names
    whose underscore-stripped root is a keyword shift up one rung of the
    `kw`/`kw_`/`kw__` ladder (`lambda`->`lambda_`, `lambda_`->`lambda__`),
    which is injective because the shift is; every other name is returned
    byte-for-byte unchanged, and can never equal a shifted name because a
    shifted name's root is a keyword and an unchanged name's root is not.
    The output is never itself a keyword: no Python keyword ends in `_` except
    the soft `_`, and `_` is only produced from the empty name, which is not an
    identifier. `_` itself has keyword root `_` and so escapes to `__`, exactly
    as the old loop did.

    Only a name whose root is a keyword can change, so no existing program that
    does not name a keyword changes its emitted output. This is TARGET keywords
    only; the host roots (`Map`/`Pool`/`Job`) are not keywords and stay guarded
    in `_ident`."""
    root = name
    while root:
        if keyword.iskeyword(root) or keyword.issoftkeyword(root):
            return name + "_"
        if not root.endswith("_"):
            break
        root = root[:-1]
    return name


def _ident(name: Any, what: str) -> str:
    if not isinstance(name, str) or not name.isidentifier():
        raise EmitError(f"{what} {name!r} is not a usable Python identifier")
    # The emitter's scaffolding lives in the `_revl*` namespace (`_revl_ctx`,
    # `_revl_config`, the `__revl_destructure_*` temps, the `_IMPORT_ALIAS`
    # `_revl_<name>` aliases): a user identifier that enters it would collide.
    # That is the ONLY leading-underscore namespace the emitter claims. A plain
    # leading-underscore name (`_v`, `_x`) is an ordinary Python identifier and
    # the well-worn "bound but unused" idiom (e.g. a match arm that ignores its
    # payload, `Some(_v) => ...`); it is a valid Python local, does NOT collide,
    # and already emits verbatim on the module-`fn` path and on the other tiers
    # (a go regression, examples/regressions/fuzz_go_ead437e4.rvl, pins it).
    # Reserving ALL of `_*` refused it LATE here with a raw `EmitError`
    # traceback for a program the checker and every other tier accept (roadmap
    # item 408). Narrow the guard to the namespace actually reserved so the
    # component/method binding path emits it verbatim like the rest.
    if name in _RESERVED or (name.startswith("_") and name.lstrip("_").startswith("revl")):
        raise EmitError(f"{what} {name!r} collides with emitter scaffolding")
    return _mangle(name)


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_") if part)


class _Lines:
    def __init__(self) -> None:
        self._lines: list[str] = []
        # Monotonic index handed to destructure temporaries so their names are
        # a deterministic property of emission order, not object identity
        # (`id(node)` was nondeterministic across re-parses — item 179).
        self._destructure_seq = 0

    def next_destructure_seq(self) -> int:
        self._destructure_seq += 1
        return self._destructure_seq

    def add(self, indent: int, text: str = "") -> None:
        self._lines.append(("    " * indent + text) if text else "")

    def extend(self, other: "_Lines") -> None:
        self._lines.extend(other._lines)

    def text(self) -> str:
        return "\n".join(self._lines)


def _uses_bounded_int(node) -> bool:
    """Does this IR do Int `+`, `-` or `*`, or negate an Int? The bound check
    is emitted only where it is needed, so modules that never do Int
    arithmetic are unchanged."""
    if isinstance(node, dict):
        if (node.get("kind") == "bin" and node.get("op") in ("+", "-", "*")
                and node.get("operands") == "Int"):
            return True
        # unary minus on an Int goes through the bound too: it is `0 - x`
        if (node.get("kind") == "un" and node.get("op") == "-"
                and node.get("operands") == "Int"):
            return True
        # integer division overflows at Int.MIN/-1 (quotient 2^63); the
        # faulting forms are bound the same way (mod cannot overflow)
        if (node.get("kind") == "builtin"
                and node.get("method") in ("div_trunc", "div_floor", "div_euclid")):
            return True
        return any(_uses_bounded_int(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_bounded_int(v) for v in node)
    return False


def _uses_bounded_int32(node) -> bool:
    """Does this IR do Int32 `+`/`-`/`*`, negate an Int32, or narrow with
    `to_int32`? The i32 bound helper is emitted only where it is used."""
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


def _uses_i32_shl(node) -> bool:
    """Does this IR do an Int32 `<<`? Left shift is the one bitwise op whose
    result can leave the 32-bit range (a bit op, not a trap), so python
    re-wraps it through `_revl_i32_wrap`; `&`/`|`/`^`/`>>`/`~` all stay in
    range for in-range operands and need no helper (docs/arithmetic.md)."""
    if isinstance(node, dict):
        if node.get("kind") == "bin" and node.get("op") == "<<":
            return True
        return any(_uses_i32_shl(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_i32_shl(v) for v in node)
    return False


def _uses_true_division(node) -> bool:
    """Does anything in this IR divide with `/`? The IEEE helper is emitted
    only where it is used, so modules that never divide stay unchanged."""
    if isinstance(node, dict):
        if node.get("kind") == "bin" and node.get("op") == "/":
            return True
        return any(_uses_true_division(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_true_division(v) for v in node)
    return False


def _uses_builtin(node, *methods: str) -> bool:
    """Does this IR call any of these stdlib builtins? The preamble helper each
    one lowers to (item 436 F6) is emitted only where it is used, exactly as
    `_revl_div` and `_revl_ftoa` already are."""
    if isinstance(node, dict):
        # `?.m(..)` is an `optcall` node, NOT a `builtin` one, but it goes
        # through the very same `_render_builtin` table, so it needs the very
        # same helper emitted.
        if node.get("kind") in ("builtin", "optcall") \
                and node.get("method") in methods:
            return True
        return any(_uses_builtin(v, *methods) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_builtin(v, *methods) for v in node)
    return False


def _uses_opt_to_int(node) -> bool:
    """Is a `to_int` reached through `?.`, whose node carries no receiver type?
    Only then is the payload-dispatching wrapper emitted."""
    if isinstance(node, dict):
        if node.get("kind") == "optcall" and node.get("method") == "to_int":
            return True
        return any(_uses_opt_to_int(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_opt_to_int(v) for v in node)
    return False


def _uses_trunc_rem(node) -> bool:
    """Does this IR take a truncated remainder (`%` on Int or Float)? Python's
    own `%` floors, so this is the one operator the tier has to build."""
    if isinstance(node, dict):
        if (node.get("kind") == "bin" and node.get("op") == "%"
                and node.get("operands") in ("Int", "Float")):
            return True
        return any(_uses_trunc_rem(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_trunc_rem(v) for v in node)
    return False


def _uses_float_interp(node) -> bool:
    """Does any `${…}` template interpolate a provably-`Float` expression?
    The canonical `Float -> Str` helper is emitted only then, so modules
    without float interpolation stay byte-identical (docs/strings.md)."""
    if isinstance(node, dict):
        if node.get("kind") == "interp":
            for part in node.get("parts") or []:
                if isinstance(part, (list, tuple)) and len(part) == 2 \
                        and part[0] == "expr" and _is_float_expr(part[1]):
                    return True
        return any(_uses_float_interp(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_uses_float_interp(v) for v in node)
    return False


# Canonical Float -> Str: ECMAScript Number::toString (docs/strings.md). `repr`
# gives the shortest round-trip digits; this reformats them into the ES
# notation (integer/fraction/exponent thresholds, `-0` -> `"0"`).
_REVL_ROUTER_SRC = '''class _RevlNoLiveWorker(RuntimeError):
    """Every realm a routed require fans out to has withdrawn — no live leg
    left to serve (a runtime pool exhaustion, not a link-time diagnostic)."""


class _RevlRouter:
    """Emitted realization of a routed require (item 162 `routes` IR), mirroring
    src/revl/run.py::_Router. Re-resolves the live per-realm handle on every
    call, so a withdrawn worker drops out and calls go to the survivors."""

    def __init__(self, ctx, key, realms, strategy):
        self._root = ctx.root
        self._key = key
        self._realms = list(realms)
        self._strategy = strategy or "round_robin"
        self._cursor = 0
        self._served = {realm: 0 for realm in self._realms}

    def _handle(self, realm):
        scoped = self._root.isolate(self._key, realm_label(realm))
        return scoped.reflect.get(self._key)

    def _live(self):
        out = []
        for realm in self._realms:
            handle = self._handle(realm)
            if handle is not None:
                out.append((realm, handle))
        return out

    def _select(self):
        live = self._live()
        if not live:
            raise _RevlNoLiveWorker(
                "revl: router for %r has no live worker: all %d realm(s) (%s) "
                "have withdrawn" % (self._key, len(self._realms),
                                    ", ".join(self._realms)))
        if self._strategy == "least_loaded":
            realm, handle = min(live, key=lambda rh: self._served[rh[0]])
        else:  # round_robin — next live realm in declaration order
            n = len(self._realms)
            realm = handle = None
            for offset in range(n):
                cand = self._realms[(self._cursor + offset) % n]
                match = next((rh for rh in live if rh[0] == cand), None)
                if match is not None:
                    self._cursor = (self._cursor + offset + 1) % n
                    realm, handle = match
                    break
        self._served[realm] += 1
        return realm, handle

    def __getattr__(self, method):
        # `_`-prefixed lookups are never routed service ops — refuse them so a
        # traceable probe passes the proxy through raw.
        if method.startswith("_"):
            raise AttributeError(method)

        def call(*args):
            _realm, handle = self._select()
            return getattr(handle, method)(*args)

        return call


def _revl_router(ctx, key, realms, strategy):
    return _RevlRouter(ctx, key, realms, strategy)'''


# item 310, `cache pure`: the body-level memo table. The seam gate memoizes a
# `cache pure` SERVICE METHOD at the call (mcp/session.py), but a `cache pure`
# plain `fn` is reached from inside a body, where no seam exists — so the memo
# for it lives in the emitted module, which is where the call actually happens.
#
# Sound by construction and by construction only: `_check_cache_declarations`
# admits `cache pure` on a plain fn ONLY when the emission fixed point says its
# reach crosses nothing, and G6 then gives equal-arguments-equal-result outright
# (bodies outside effect forms are pure, captures are by value, no revl value is
# ever mutated in place). There is no authority in the key because there is no
# authority in the reach: a crossing-free result is not derived from anyone's
# grant, so re-delivering it cannot launder one. That is the whole argument, and
# it is why this is the one cache class with no ledger interaction at all.
#
# The key is a WHITELIST over the value shapes this backend emits, never a
# fallback: an unrecognized shape answers `_REVL_NOMEMO` and the call is simply
# not memoized. A missing entry is always sound (a miss recomputes a pure
# function), so an unknown shape costs performance and never correctness.
_REVL_MEMO_SRC = '''_REVL_MEMO = {}
_REVL_NOMEMO = object()
_REVL_MISS = object()


def _revl_memo_key(v):
    """A hashable STRUCTURAL key for a revl value, or `_REVL_NOMEMO`."""
    if v is None or isinstance(v, (bool, int, float, str, bytes)):
        return (type(v).__name__, v)
    if isinstance(v, (list, tuple)):
        parts = []
        for item in v:
            key = _revl_memo_key(item)
            if key is _REVL_NOMEMO:
                return _REVL_NOMEMO
            parts.append(key)
        return ("seq", tuple(parts))
    if isinstance(v, dict):
        parts = []
        for name in v:
            if not isinstance(name, str):
                return _REVL_NOMEMO
            key = _revl_memo_key(v[name])
            if key is _REVL_NOMEMO:
                return _REVL_NOMEMO
            parts.append((name, key))
        parts.sort()
        return ("rec", tuple(sorted(parts)))
    if callable(v) or getattr(v, "__dict__", None):
        return _REVL_NOMEMO
    slots = getattr(type(v), "__slots__", None)
    if slots == ():
        return ("adt", type(v).__name__)
    if slots == ("value",):
        key = _revl_memo_key(v.value)
        if key is _REVL_NOMEMO:
            return _REVL_NOMEMO
        return ("adt", type(v).__name__, key)
    return _REVL_NOMEMO'''


_REVL_FTOA_SRC = '''def _revl_ftoa(x):
    """Canonical Float -> Str: ECMAScript Number::toString (docs/strings.md)."""
    if x != x:
        return "NaN"
    if x == float("inf"):
        return "Infinity"
    if x == float("-inf"):
        return "-Infinity"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    s = repr(abs(x))
    if "e" in s or "E" in s:
        mant, _, exp_s = s.replace("E", "e").partition("e")
        exp = int(exp_s)
    else:
        mant, exp = s, 0
    intpart, _, frac = mant.partition(".")
    digits = intpart + frac
    point = len(intpart) + exp
    i = 0
    while i < len(digits) - 1 and digits[i] == "0":
        i += 1
        point -= 1
    digits = digits[i:].rstrip("0") or "0"
    if digits == "0":
        return "0"
    k = len(digits)
    n = point
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
        body = mantissa + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + body'''


# The total, value-returning division forms (docs/arithmetic.md): same
# rounding as the faulting operations, Err(reason) at a zero divisor.
_CHECKED_DIVS = ("checked_div_trunc", "checked_div_floor",
                 "checked_div_euclid", "checked_mod")
_DIV_ZERO_MSG = "revl: division by zero"

# The receiver type a builtin reached through `?.` has: the `optcall` IR node
# carries no `recv` tag (unlike a `builtin` node, which lowering annotates for
# exactly this reason), so the one lowering that dispatches on it (`to_int`,
# whose Int32 row is the identity and whose Str row parses) decides at
# runtime instead.
_RECV_VIA_OPT = "?"


# `??`, `?.` and `?.()` must evaluate their LEFT OPERAND ONCE
# (docs/syntax-2.0.md §3.2). Rendering it twice, once for the presence test and
# once for the result, calls a call-shaped operand twice: a semantic defect
# before it is a cost (item 436 F7, measured at 1500 extern invocations per
# 1000 `??`). The ts tier inherits single evaluation from JS's own `??` and the
# rust tier from `unwrap_or_else`; this tier binds the operand with a walrus,
# the technique already used for `is_alpha`'s receiver.
#
# The temp is named after the OPT-CHAIN HEIGHT of the whole `??`/`?.` node, so
# a chain nested in an argument can never clobber the one above it: the outer
# node's height is strictly greater than every opt node under it, and some
# builtins (`join`, `has`) render the argument BEFORE the receiver. The height
# is a function of the node alone, so no emitter state is threaded through the
# expression walk and the self-host port needs none either.
def _opt_height(node: Any) -> int:
    """1 + the deepest `??`/`?.`/`?.()` nested under `node`; 0 if there is none."""
    if isinstance(node, list):
        return max((_opt_height(v) for v in node), default=0)
    if not isinstance(node, dict):
        return 0
    inner = max((_opt_height(v) for v in node.values()), default=0)
    if node.get("kind") in ("optfield", "optcall") or (
            node.get("kind") == "bin" and node.get("op") == "??"):
        return inner + 1
    return inner


def _opt_bind(node: dict, target: Any, rendered: str) -> tuple[str, str]:
    """`(binder, reader)` for an optional chain's left operand: the binder goes
    in the presence test, the reader wherever the value is used. An operand
    that is trivially re-readable (a local, a literal, `Map.empty()`) keeps the
    plain double render, which costs nothing and leaves the common spelling
    byte-identical to what a developer writes."""
    if isinstance(target, dict) and target.get("kind") in _INLINE_TRIVIAL:
        return f"({rendered})", rendered
    name = f"_ov{_opt_height(node)}"
    return f"({name} := {rendered})", name


# The bounded-arithmetic temp. A single name is enough for ANY nesting depth:
# the walrus lives in the condition of a conditional expression, which python
# evaluates before either branch, so an inner `+` has finished reading `_bi`
# before the outer one rebinds it, and the outer branch reads the value the
# outer bind just wrote. Nothing else in an emitted module touches the name.
_BOUNDED_TMP = "_bi"


def _bounded(operation: str, width: int) -> str:
    """Impose the Int/Int32 bound on `operation` without a helper frame.

    `_revl_i64(a + b)` entered a Python frame for every bounded `+`, `-` and
    `*`, and the calls nest — `total + i * i - i` was three frames for one
    statement (roadmap item 436 F5). The range test is two comparisons against
    module constants, so it inlines as a chained comparison and the frame is
    paid only on the trapping path, which raises anyway.
    """
    tmp = _BOUNDED_TMP
    return (f"({tmp} if _REVL_I{width}_MIN <= ({tmp} := {operation}) "
            f"<= _REVL_I{width}_MAX else _revl_i{width}({tmp}))")


# The field-read temp; see `_field_read`. Same single-name argument as
# `_BOUNDED_TMP`: the walrus sits in a condition python evaluates first, so a
# nested read has finished with the name before the outer read rebinds it.
_FIELD_TMP = "_fv"

# Every name the expression renderer may spell as a walrus target of its own:
# the bounded-arithmetic temp, the field-read temp, the char-class temp, and
# the `?.`/`??` chain temps (`_ov<height>`). `_match_expr` needs the set because
# python refuses an assignment expression that rebinds the iteration variable of
# the comprehension it sits in, so a payload bound under one of these names has
# to keep the older walrus binder.
_WALRUS_TEMPS = frozenset({_BOUNDED_TMP, _FIELD_TMP, "_rc"})
_OPT_TMP_RE = re.compile(r"_ov\d+\Z")


def _walrus_temp(name: str) -> bool:
    return name in _WALRUS_TEMPS or _OPT_TMP_RE.match(name) is not None


def _field_read(target: str, name: str, opt: bool = False,
                rereadable: bool = False) -> str:
    """`p.x`, rendered INLINE rather than through a `_revl_field` call.

    Record literals are dicts and ADT payloads are objects, so the read has to
    dispatch on the receiver's shape — but the dispatch is one `isinstance`,
    which does not need a Python frame around it. `_revl_field(p, 'x')` cost a
    frame per field read, on every record-shaped path in the program (roadmap
    item 436 F4). What is left — the `isinstance` itself — is the part only a
    frontend marker can remove, and item 445 owns that.

    `opt` is the item-380 TOTAL read: an absent key (or a non-record receiver)
    is the Opt's empty case, never a raise. `rereadable` says the caller has
    already bound the target to a name (the `?.` chain has), so no temp is
    needed.
    """
    if rereadable:
        got, tmp = target, target
    else:
        tmp = _FIELD_TMP
        got = f"({tmp} := {target})"
    if opt:
        return (f"({tmp}.get({name!r}) if isinstance({got}, dict) "
                f"else getattr({tmp}, {name!r}, None))")
    return (f"({tmp}[{name!r}] if isinstance({got}, dict) "
            f"else getattr({tmp}, {name!r}))")


def _render_builtin(method, target: str, args: list, recv: str | None = None) -> str:
    """The stdlib surface (docs/stdlib-2.0.md), rendered as portable Python.
    `push`/`concat` are persistent (value semantics); `indexOf` returns -1
    when absent on both hosts. `recv` is the receiver's static type, carried
    only where the lowering must dispatch on it (`to_int`: the Int32 widen is
    the identity on python, the Str parse is not)."""
    if method == "length":
        return f"len({target})"
    if method == "push":
        return f"({target} + [{args[0]}])"
    if method == "slice":
        return f"{target}[{args[0]}:{args[1]}]"
    if method == "charAt":
        return f"{target}[{args[0]}]"
    if method == "charCodeAt":
        return f"ord({target}[{args[0]}])"
    # Codepoint-at-index scan (item 276, docs/stdlib-2.0.md §Str.codepoint_at):
    # the Unicode scalar at index i, returned directly. `ord(s[i])` allocates
    # only the transient 1-char slice `ord` consumes — no persistent 1-char Str
    # the self-host lexer would otherwise index a second time via `code0`.
    if method == "codepoint_at":
        return f"ord({target}[{args[0]}])"
    if method == "concat":
        return f"({target} + {args[0]})"
    if method == "indexOf":
        # A preamble helper, not a lambda built and applied at every evaluation
        # (item 436 F6): one frame, no function-object allocation.
        return f"_revl_index_of({target}, {args[0]})"
    if method == "split":
        # JS-shape split: "" -> 1-char strings (py str.split("") raises).
        return f"_revl_split({target}, {args[0]})"
    if method == "join":
        return f"{args[0]}.join({target})"
    if method == "repeat":
        return f"({target} * {args[0]})"
    # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith):
    # python's startswith/endswith count in code points, the exact semantics.
    if method == "startsWith":
        return f"{target}.startswith({args[0]})"
    if method == "endsWith":
        return f"{target}.endswith({args[0]})"
    # Single-character ASCII classification (item 233, docs/stdlib-2.0.md
    # §Str.is_alnum): a native inline test, no revl-fn call and no 1-char
    # `ord` round-trip. Chained string comparison is ASCII code-point order
    # AND empty-safe (`"a" <= "" <= "z"` is False, so an empty receiver is
    # false rather than faulting; multi-character input is outside the
    # per-character contract but stays total — it never raises). is_alpha/is_alnum
    # reference the receiver more than once, so a walrus binds it a single
    # time (`_rc`) — correct even when the receiver has side effects, with no
    # lambda-call overhead (the whole point of the builtin is to be cheap).
    if method == "is_digit":
        return f'("0" <= {target} <= "9")'
    if method == "is_alpha":
        return (f'(("a" <= (_rc := {target}) <= "z") '
                f'or ("A" <= _rc <= "Z"))')
    if method == "is_alnum":
        return (f'(("0" <= (_rc := {target}) <= "9") '
                f'or ("a" <= _rc <= "z") or ("A" <= _rc <= "Z"))')
    # is_space: space, tab, LF, CR — tuple membership (a bare `in " \t\n\r"`
    # would wrongly match the empty string, which is a substring of every str).
    if method == "is_space":
        return f'({target} in (" ", "\\t", "\\n", "\\r"))'
    # The Map value type (docs/stdlib-2.0.md §Map): a python dict, copied
    # on write — `{**m, k: v}` IS the persistent set.
    if method == "set":
        return f"({{**{target}, {args[0]}: {args[1]}}})"
    if method == "lookup":
        # dict.get answers None when absent: exactly the Opt None case.
        return f"{target}.get({args[0]})"
    if method == "has":
        return f"({args[0]} in {target})"
    # The iteration/remove step (docs/stdlib-2.0.md §Map). python str
    # comparison IS canonical (code-point) order, so sorted() is exact;
    # remove copies without the key, receiver untouched.
    if method == "size":
        return f"len({target})"
    if method == "keys":
        return f"sorted({target})"
    if method == "remove":
        # A dict COMPREHENSION, not `dict(<generator>)`: the comprehension is
        # one frame and builds the dict directly, where the generator form
        # entered four (the `dict` call, the genexpr frame, and its resumes)
        # for the same elements (roadmap item 436 F2).
        return ("{" + f"kk: vv for kk, vv in {target}.items() "
                f"if kk != {args[0]}" + "}")
    # Integer division and modulo (docs/arithmetic.md). Python's `//` floors
    # and its `%` takes the divisor's sign, so div_floor is native and the
    # Euclidean remainder is `a % abs(b)`; truncation has to be built.
    if method == "div_trunc":
        return _bounded(f"_revl_div_trunc({target}, {args[0]})", 64)
    if method == "div_floor":
        return _bounded(f"{target} // {args[0]}", 64)
    if method == "div_euclid":
        return _bounded(f"_revl_div_euclid({target}, {args[0]})", 64)
    if method == "mod":
        return f"({target} % abs({args[0]}))"
    # Int/Int32 width conversions (docs/arithmetic.md). python has one int
    # type, so widening Int32 -> Int is the identity; narrowing Int -> Int32
    # re-imposes the 32-bit bound through `_revl_i32`, which traps out of range.
    if method == "to_int":
        if recv in ("Str", _RECV_VIA_OPT):
            # Str.to_int (FR-9, docs/stdlib-2.0.md §Str.to_int): total on the
            # ASCII digits with an optional leading `-`, `None` otherwise —
            # including out of the i64 range, which is None like every other
            # non-digit (the tier's ints are unbounded, so the bound must be
            # checked here rather than by int()).
            if recv == _RECV_VIA_OPT:
                # reached through `?.`, whose node carries no receiver type:
                # dispatch on the payload, the same split `indexOf` makes
                # between a List and a Str receiver.
                return f"_revl_opt_to_int({target})"
            return f"_revl_str_to_int({target})"
        return f"({target})"
    if method == "to_int32":
        return f"_revl_i32({target})"
    # The total forms (docs/arithmetic.md): same quotient as the faulting
    # operation, but a zero divisor yields Err(reason) instead of raising —
    # the whole point is that a pure fn cannot `fail`, so the error travels
    # as a value. Ok/Err are the tagged classes emitted when the IR uses
    # Result (gated in `_uses_builtin_result`).
    if method in _CHECKED_DIVS:
        return f"_revl_{method}({target}, {args[0]})"
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str): python ints on
    # this tier are already i64-clamped, so str() is the exact decimal.
    if method == "to_str":
        return f"str({target})"
    raise EmitError(f"unknown builtin method {method!r}")


# item 397: a compare-and-set host verb (`insert_if_absent`) whose site-spelled
# `undo` must be RESULT-GUARDED — registered only when the CAS actually
# inserted. A `false` CAS (key already present) inserted nothing, so its
# inverse is the identity; replaying `remove(k)` at teardown would delete the
# WINNING claimant's entry, the exact corruption single-use exists to prevent.
_MAP_CAS_VERBS = frozenset({"insert_if_absent"})


def _is_map_cas(acquire: Any) -> bool:
    """Whether an acquisition node is a result-guarded map CAS (item 397)."""
    return (isinstance(acquire, dict) and acquire.get("kind") == "call"
            and acquire.get("method") in _MAP_CAS_VERBS)


class _ComponentEmitter:
    def __init__(self, component: dict, services: dict, externs: list | None = None,
                 plan_groups: list | None = None) -> None:
        self.ir = component
        self.services = services
        # item 259 slice 2: every declared extern by name, so an `emit` step's
        # forward-delivery idempotence (the fan-out eligibility gate) is readable
        # off a host-extern emission the same way a req-target emission reads it
        # off its service method spec.
        self._extern_by_name = {e["name"]: e for e in (externs or [])}
        # item 243 (docs/design/243-witnessed-externs.md): witnessed externs by
        # name, so a call site can be recognised as a transactional effect and
        # register its DECLARED inverse (not a site-spelled one) into the
        # accumulator. Absent/empty for every program that uses no witnessed
        # extern, so their emission stays byte-identical.
        self.witnessed = {
            ext["name"]: ext for ext in (externs or [])
            if ext.get("class") == "witnessed"
        }
        # item 245 (docs/design/245-session-commit.md, Decision 2): deferred
        # emission externs by name. A call to one does not fire at the site — it
        # enqueues a descriptor onto the session's deferral queue and returns
        # Unit; the session commit flushes it (or an abort drops it). Absent/empty
        # for every program that declares no `deferred` extern, so their emission
        # stays byte-identical.
        self.deferred = {
            ext["name"]: ext for ext in (externs or [])
            if ext.get("class") == "emission" and ext.get("deferred")
        }
        self.name = _ident(component.get("name"), "component name")
        self.requires = {
            _ident(local, "requires key"): service
            for local, service in (component.get("requires") or {}).items()
        }
        self.provides = component.get("provides") or {}
        self.config_fields = component.get("config") or []
        self.snake = _snake(self.name)
        self.uses: set[str] = set()
        self._counter = 0
        # item 92: is the method body currently rendered an `async def`? A call
        # to a colored fn / async local is awaited only then (the frontend
        # admits such a call only inside an async op).
        self._in_async = False
        # item 141: are we rendering INSIDE an arrow body? An async arrow's tail
        # emission stays a plain coroutine-returning lambda (item 92) that its
        # awaiting call site settles — so the await-seed must NOT fire inside an
        # arrow, only in the method body's own statement/return/expression spots.
        self._in_arrow = False
        # v2: realm placements and intercept metadata (docs/design-v2-realms.md)
        self.isolate = component.get("isolate") or {}
        self.intercept = component.get("intercept") or {}
        # item 167: routed requires (item 162's `routes` IR) — a required key
        # bound across N named realms with a strategy. The emitted body must
        # fan out across those realms (mirror src/revl/run.py::_Router), not
        # resolve a single-realm handle. A routed key is read through a
        # `_revl_route_<key>` proxy the apply() builds, and is NOT in the
        # fiber's inject gate (it has no single-realm provider — the workers
        # live in the named realms — so injecting it would pend forever).
        self.routes = component.get("routes") or {}
        for key in self.routes:
            if key not in self.requires:
                raise EmitError(f"{self.name}: routed key {key!r} is not a requirement")
        for key in self.isolate:
            if key not in self.requires and key not in self.provides:
                raise EmitError(f"{self.name}: isolate key {key!r} is not declared")
        for key in self.intercept:
            if key not in self.requires:
                raise EmitError(f"{self.name}: intercept key {key!r} is not a requirement")
        # item 259 slice 2: the checked fan-out plan for THIS component, reduced to
        # the groups the runtime may actually fire concurrently in the activation
        # body (`id(leader step) -> [member steps]`). Empty for every body with no
        # provable parallelism, so its emission is byte-identical to before.
        self._parallel_leaders = self._build_parallel_leaders(plan_groups)

    def _build_parallel_leaders(self, plan_groups: list | None) -> dict:
        """Map each fan-out group's LEADER step to its member steps, keeping only
        groups the activation-body driver can safely fan out (item 259 slice 2,
        the CONSERVATIVE path). A group qualifies only when:

          * it has more than one member (a singleton is a plain sequential emit);
          * every member is a top-level step of THIS activation body, appearing as
            a PHYSICALLY CONTIGUOUS run (no intervening step) - a group split by a
            pure filler step stays sequential, so the concurrent fires never
            reorder around anything between them;
          * every member passes `_group_eligible` (idempotent forward delivery,
            compensation idempotent-or-absent, on-task audit records, awaited).

        Any group that fails a clause degrades to sequential - the worst case is
        no speedup, never a wrong grouping (§6)."""
        body = self.ir.get("body") or []
        pos = {id(step): i for i, step in enumerate(body)}
        leaders: dict = {}
        for group in plan_groups or []:
            if len(group) < 2:
                continue
            positions = [pos.get(id(step)) for step in group]
            if any(p is None for p in positions):
                continue  # a member lives in a nested provide/timer body - skip
            if positions != list(range(positions[0], positions[0] + len(positions))):
                continue  # not physically contiguous (filler between members)
            if not all(self._group_eligible(step) for step in group):
                continue
            leaders[id(group[0])] = list(group)
        return leaders

    def _emission_shape(self, expr: Any) -> Optional[str]:
        """Classify an `emit` step's expression: "req" (a req-target emission),
        "extern" (a direct host-extern emission), "spawn" (a spawn-handle
        provision call), or None. The fan-out admits only "req"/"extern": a
        spawn-handle emission records through the OFF-TASK spawn recorder
        (`_revl_record_spawn`), so its audit escapes the branch sink (§3.2)."""
        if not isinstance(expr, dict):
            return None
        kind = expr.get("kind")
        target = expr.get("target")
        if kind == "call" and isinstance(target, dict) and target.get("kind") == "req":
            return "req"
        if kind == "call":
            callee = expr.get("callee")
            if isinstance(callee, dict) and callee.get("kind") == "field":
                recv = callee.get("target")
                if isinstance(recv, dict) and recv.get("kind") == "instance-get":
                    return "spawn"
        if kind == "fn":
            return "extern"
        return None

    def _emission_idempotent(self, expr: Any) -> bool:
        """Whether an `emit` step's forward delivery is declared `idempotent`
        (item 44/309). Read off the service method spec for a req-target emission,
        or the extern IR for a host-extern emission."""
        if not isinstance(expr, dict):
            return False
        kind = expr.get("kind")
        target = expr.get("target")
        if kind == "call" and isinstance(target, dict) and target.get("kind") == "req":
            svc = self.services.get(self.requires.get(target.get("name"))) or {}
            spec = (svc.get("methods") or {}).get(expr.get("method")) or {}
            return bool(spec.get("idempotent"))
        if kind == "fn":
            return bool((self._extern_by_name.get(expr.get("name")) or {}).get("idempotent"))
        return False

    def _call_declares_idempotent(self, expr: Any) -> bool:
        """Whether a call expression's target operation is DECLARED idempotent -
        forward `idempotent` (item 44/309) or inverse `undo_idempotent` (item 309,
        the flag recovery.py reads). Used to decide whether a member's
        COMPENSATION is safe to re-run or skip: a compensation whose operation is
        idempotent leaves the world in the same state whether it runs once, twice,
        or (under a divert) not at all."""
        if not isinstance(expr, dict):
            return False
        kind = expr.get("kind")
        target = expr.get("target")
        if kind == "call" and isinstance(target, dict) and target.get("kind") == "req":
            svc = self.services.get(self.requires.get(target.get("name"))) or {}
            spec = (svc.get("methods") or {}).get(expr.get("method")) or {}
            return bool(spec.get("idempotent") or spec.get("undo_idempotent"))
        if kind == "fn":
            ext = self._extern_by_name.get(expr.get("name")) or {}
            return bool(ext.get("idempotent") or ext.get("undo_idempotent"))
        return False

    def _compensation_idempotent_or_absent(self, step: dict) -> bool:
        """The CRITICAL conservative restriction (item 259, S3.3/S5/S8, the
        adversarial-review fix): a multi-emission group may form ONLY from
        emissions whose COMPENSATION is itself idempotent-or-absent - NOT merely
        whose forward delivery is idempotent.

        `absent`: the `emit` step declares no `compensate` clause (the common case,
        trivially safe). `idempotent`: the compensation operation is declared
        idempotent (`_call_declares_idempotent`). A member with a PRESENT,
        non-idempotent compensation stays sequential (a singleton), because a
        fault or an A1 divert that fires it and then runs (or, at the divert
        boundary, skips) its compensation would not leave the same world state a
        skipped sequential tail would - the exact soundness gap this restriction
        closes by construction (§3.3 invariant E)."""
        compensate = step.get("compensate")
        if compensate is None:
            return True  # absent - trivially safe
        return self._call_declares_idempotent(compensate)

    def _group_eligible(self, step: dict) -> bool:
        """Whether one `emit` step may join a concurrent fan-out group (item 259
        slice 2, the C2/E fail-safe). ALL of:

          * it is an awaited (`async`) `emit` step - a real suspension, so the
            branches' round trips are actually in flight at once and the body is
            already the `async def` generator the fan-out needs;
          * forward delivery is declared `idempotent` (item 44/309) - safe to
            over-fire under a fault or an A1 divert;
          * its COMPENSATION is idempotent-or-absent (the CRITICAL restriction,
            `_compensation_idempotent_or_absent`) - NOT merely idempotent forward
            delivery. A member carrying a present, non-idempotent compensation
            stays sequential, so a divert can never leave a fired member with a
            real, un-runnable compensation (§3.3/§5/§8);
          * its audit records are produced synchronously on-task - a req-target or
            direct host-extern emission, NOT a spawn-handle provision call whose
            records route off-task (§3.2);
          * it is not an approval crossing, a deferred emission, or a validated
            emission - each carries stateful seam machinery the conservative slice
            keeps sequential rather than reason about under concurrency."""
        if step.get("step") != "emit":
            return False
        if not step.get("async"):
            return False
        expr = step.get("expr")
        if self._emission_shape(expr) not in ("req", "extern"):
            return False
        if not self._emission_idempotent(expr):
            return False
        if not self._compensation_idempotent_or_absent(step):
            return False
        if step.get("approval") is not None:
            return False
        if self._deferred_extern(expr) is not None:
            return False
        if self._validated_call(expr) is not None:
            return False
        return True

    # -- expressions --------------------------------------------------------

    def _stream_head(self, node, where: str) -> str:
        """The stream a `subscribe` acquires: a plain source, or a `merge(a, b)`
        fan-in (item 130 Slice 3). Recursive, because a merged stream is itself a
        stream. Every link is a DERIVED stream owned by the subscription, so
        `close` unwinds the whole chain off the ONE bracket the subscribe
        registers, leaving each plain source to its own."""
        if isinstance(node, dict) and node.get("kind") == "stream-merge":
            args = ", ".join(self._stream_head(src, where)
                             for src in node.get("sources") or [])
            return f"Stream.merge({args})"
        return self._expr(node, where)

    def _expr(self, expr: Any, where: str) -> str:
        if not isinstance(expr, dict) or "kind" not in expr:
            raise EmitError(f"{where}: malformed expression {expr!r}")
        kind = expr["kind"]
        if kind == "builtin":
            t = self._expr(expr.get("target"), where)
            a = [self._expr(x, where) for x in expr.get("args") or []]
            return _render_builtin(expr.get("method"), t, a, expr.get("recv"))
        if kind == "maplit":
            # `Map.empty()` (docs/stdlib-2.0.md §Map)
            return "{}"
        if kind == "lit":
            return repr(expr.get("value"))
        if kind == "name":
            return _ident(expr.get("id"), f"{where}: name")
        if kind == "config":
            field = expr.get("field")
            if not any(spec.get("name") == field for spec in self.config_fields):
                raise EmitError(f"{where}: unknown config field {field!r}")
            return f"_revl_config[{field!r}]"
        if kind == "req":
            name = expr.get("name")
            if name not in self.requires:
                raise EmitError(f"{where}: req {name!r} is not declared in requires")
            # item 167: a routed require reads through its router proxy, which
            # re-resolves a live per-realm worker on every call (failover). The
            # proxy is a local `_revl_route_<key>` the apply() builds before the
            # body; a provide-method's `<key>.<op>(…)` closes over it.
            if name in self.routes:
                return f"_revl_route_{name}"
            # committed-view access: resolves through the fiber's store, so it
            # stays readable during this component's own teardown (R3)
            return f"_revl_ctx.{name}"
        if kind == "call":
            if "target" in expr:
                target = self._expr(expr.get("target"), where)
                method = expr.get("method")
                if not isinstance(method, str) or not method.isidentifier():
                    raise EmitError(f"{where}: bad method name {method!r}")
                args = ", ".join(self._expr(arg, where) for arg in expr.get("args") or [])
                rendered = f"{target}.{method}({args})"
            else:
                # `Some(x)` is the identity on this tier (item 436 F8) — the
                # same special case the module-fn `_expr` makes, so a component
                # body does not build and apply `(lambda _v: _v)` either.
                callee_node = expr.get("callee")
                call_args = expr.get("args") or []
                if isinstance(callee_node, dict) \
                        and callee_node.get("kind") == "var" \
                        and callee_node.get("name") == "Some" and len(call_args) == 1:
                    return self._expr(call_args[0], where)
                callee = self._expr(callee_node, where)
                args = ", ".join(self._expr(arg, where) for arg in call_args)
                rendered = f"{callee}({args})"
            # item 141 await-seed: an emission of an async service op — through a
            # req key (`emit model.complete(p)`) or a spawn handle — produces a
            # coroutine. Await it wherever it lands in an async body: not only a
            # statement/return, but a NESTED expression position such as a
            # ternary arm (`p == "go" ? emit m.complete(p) : "idle"`), so no
            # coroutine leaks unawaited. `_py_yields_coroutine` is the same
            # predicate the arrow renderer uses, so the two stay in lockstep.
            awaited = (self._in_async and not self._in_arrow
                       and _py_yields_coroutine(expr, self.requires))
            settled = f"(await {rendered})" if awaited else rendered
            # item 257: the validate-on-response seam. A `validated` emission's
            # SETTLED response is checked revl-side against the schema derived
            # from its return type (regardless of the provider), and the revl ADT
            # is constructed from the validated tag/value. A malformed response is
            # a typed fault (retry is Slice 2). Byte-identical for any non-
            # `validated` call: `_validated_call` returns None.
            validated = self._validated_call(expr)
            if validated is not None:
                schema, ctors, retry = validated
                if retry:
                    # item 257 (Slice 2, §5.2): the read-with-a-cost retry loop.
                    # On a `ResponseValidationError` the loop re-fires ONLY the
                    # model completion call (`rendered`, a fresh coroutine per
                    # attempt in the async case), up to `retry` times, then
                    # surfaces the typed fault. The validate seam sits at the
                    # forward crossing BEFORE the value binds, so no downstream
                    # `emit`/witnessed effect exists to double (§5.3). Passing the
                    # call as a thunk (not the already-`settled` value) is what
                    # lets the loop re-issue the completion and nothing else.
                    if awaited:
                        self.uses.add("validate_retry_async")
                        return (f"(await _revl_validate_retry_async("
                                f"lambda: {rendered}, {retry}, {schema!r}, "
                                f"{where!r}, {ctors}))")
                    self.uses.add("validate_retry")
                    return (f"_revl_validate_retry(lambda: {rendered}, {retry}, "
                            f"{schema!r}, {where!r}, {ctors})")
                self.uses.add("validate_response")
                return (f"_revl_validate({settled}, {schema!r}, {where!r}, "
                        f"{ctors})")
            return settled
        if kind == "host":
            fn = expr.get("fn") or ""
            root, _, rest = fn.partition(".")
            if root not in _HOST_ROOTS or not rest.isidentifier():
                raise EmitError(f"{where}: unknown host builtin {fn!r}")
            self.uses.add(root)
            args = ", ".join(self._expr(arg, where) for arg in expr.get("args") or [])
            return f"{fn}({args})"
        if kind == "subscribe":
            # item 130: `subscribe <stream>` opens a single-consumer subscription
            # on the stream source and registers the bracket. `Stream.subscribe`
            # returns a Subscription whose `next()` awaits an item raced against a
            # cancel future and whose `close()` trips that future synchronously —
            # so the bracket inverse is reachable off the teardown path even while
            # a `next` is parked (design §4.6, the cancellation-first fix).
            self.uses.add("Stream")
            stream = self._stream_head(expr.get("stream") or {}, where)
            policy = expr.get("policy") or "error"
            # `_revl_ctx` lets the subscription observe owner withdrawal so a
            # parked `next` resolves as `Closed` when the owner unloads (§9 Part
            # A) — the cancellation-first fix that keeps the bracket inverse
            # reachable and teardown deadlock-free on the event-loop tier.
            extra = ""
            # item 130 Slice 2: the derived-stream combinator chain, the declared
            # buffer capacity and the `block`-policy drain window. Each is
            # appended only when DECLARED, so a Slice 1 subscription still emits
            # the exact three-argument call (byte-identity, §10.9).
            stages = expr.get("stages") or []
            if stages:
                rendered = []
                for stage in stages:
                    name = stage.get("stage")
                    if name == "take":
                        rendered.append(f"('take', {int(stage.get('count'))})")
                    else:
                        # a G6-pure arrow (rule 3.5) — a plain python lambda
                        rendered.append(
                            f"({name!r}, {self._expr(stage.get('fn'), where)})")
                extra += f", stages=[{', '.join(rendered)}]"
            if expr.get("buffer") is not None:
                extra += f", capacity={int(expr.get('buffer'))}"
            if expr.get("drain") is not None:
                extra += f", drain_ms={int(expr.get('drain'))}"
            return f"Stream.subscribe({stream}, {policy!r}, _revl_ctx{extra})"
        if kind == "fn":
            name = _ident(expr.get("name"), f"{where}: function")
            args = ", ".join(self._expr(arg, where) for arg in expr.get("args") or [])
            # item 92: a call to a colored (async def) module fn returns a
            # coroutine — await it inside an async method body. item 115: an
            # async extern is likewise an `async def`, so await its call too
            # (an `unload` awaiting `host_dispose(fiber)`, async-extern.md §8).
            # item 141: suppressed inside an arrow body — a colored tail call
            # there stays a plain coroutine-returning lambda its awaiting call
            # site settles, never an `await` inside a lambda.
            if self._in_async and not self._in_arrow \
                    and name in (_PY_COLORED_FNS | _PY_ASYNC_EXTERNS):
                return f"(await {name}({args}))"
            return f"{name}({args})"
        if kind == "match":
            # the ADT eliminator, now legal in component/method bodies; the
            # module-level renderer already knows the node shape
            arms = expr.get("arms") or []
            # item 263: an arm that crosses an async boundary inside an async
            # method renders an `await`; the walrus binder keeps it out of the
            # scrutinee/payload lambdas (which are sync frames).
            awaited = self._in_async and any(
                _py_reaches_coroutine(arm.get("body"), self.requires)
                for arm in arms)
            return _match_expr(self._expr(expr.get("scrutinee"), where),
                               [{**arm, "body": _RenderedBody(
                                   self._expr(arm.get("body"), where),
                                   arm.get("body"))}
                                for arm in arms],
                               awaited=awaited)
        if kind == "do":
            # a statement-block match arm lowered inline (item 361): each `let`
            # becomes a walrus bind carried by a `(<binds>, <tail>)[-1]` tuple,
            # so an `await` in a value or the tail lands in the enclosing
            # `async def` frame — the match holding this arm renders its awaited
            # (walrus) form when the arm reaches a coroutine, never a sync
            # lambda (item 263).
            binds = []
            for st in expr.get("stmts") or []:
                if st.get("step") != "let":
                    raise EmitError(
                        f"{where}: unsupported step in a block arm: {st.get('step')!r}")
                nm = _ident(st.get("name"), f"{where}: block-arm binding")
                binds.append(f"({nm} := {self._expr(st.get('value'), where)})")
            tail = self._expr(expr.get("tail"), where)
            if binds:
                return f"({', '.join(binds)}, {tail})[-1]"
            return f"({tail})"
        if kind == "adt":
            case = _ident(expr.get("case"), f"{where}: adt case")
            args = ", ".join(self._expr(a, where) for a in expr.get("args") or [])
            return f"{case}({args})"
        if kind == "var":
            name = expr.get("name")
            # Opt is host-None/value: `Some(x)` is identity, `None` is Python
            # None. (Result Ok/Err are tagged now — built via the `adt` node.)
            if name == "None":
                return "None"
            if name == "Some":
                return "(lambda _v: _v)"
            return _ident(name, f"{where}: variable")
        if kind == "field":
            name = expr.get("name")
            if not isinstance(name, str) or not name.isidentifier():
                raise EmitError(f"{where}: bad field name {name!r}")
            if expr.get("sized_length"):
                # item 104 (cross-tier): property-form `.length` on a sized value
                # in a component position — the code-point (Str) / element (List)
                # count, NOT a record `getattr` (which raises on a str). python's
                # `len` counts code points, matching `.length()` and the fn-body
                # `len` node. The frontend marks this only on a sized target, so
                # a record field literally named `length` still reads its slot.
                return f"len({self._expr(expr.get('target'), where)})"
            # record literals are dicts; ADT payloads are objects — the read
            # dispatches on the shape INLINE (item 436 F4). An `Opt[T]`-declared
            # field reads TOTAL (item 380): absent -> None, the Opt's empty case.
            return _field_read(self._expr(expr.get("target"), where), name,
                               opt=bool(expr.get("opt")))
        if kind == "index":
            return f"{self._expr(expr.get('target'), where)}[{self._expr(expr.get('index'), where)}]"
        if kind == "bin":
            if expr.get("op") == "??":
                # `x ?? d`: `Opt[T]` is represented as `T | None` at runtime
                # (matching the TS backend's `T | undefined` shape). The left
                # operand is evaluated ONCE; see `_opt_bind`.
                left = expr.get("left")
                rhs = self._expr(expr.get("right"), where)
                binder, reader = _opt_bind(expr, left, self._expr(left, where))
                if binder == f"({reader})":
                    return f"({rhs} if {reader} is None else {reader})"
                return f"({reader} if {binder} is not None else {rhs})"
            op = _PY_BIN_OPS.get(expr.get("op"))
            if op is None:
                raise EmitError(f"{where}: unsupported binary operator {expr.get('op')!r}")
            return f"({self._expr(expr.get('left'), where)} {op} {self._expr(expr.get('right'), where)})"
        if kind == "un":
            if expr.get("op") == "!":
                return f"(not {self._expr(expr.get('operand'), where)})"
            if expr.get("op") == "-":
                return f"(-{self._expr(expr.get('operand'), where)})"
            raise EmitError(f"{where}: unsupported unary operator {expr.get('op')!r}")
        if kind == "if":
            return (f"({self._expr(expr.get('then'), where)} if "
                    f"{self._expr(expr.get('cond'), where)} else "
                    f"{self._expr(expr.get('else'), where)})")
        if kind == "record":
            return "{" + ", ".join(
                f"{name!r}: {self._expr(value, where)}"
                for name, value in expr.get("fields") or []
            ) + "}"
        if kind == "record_update":
            # functional record update (docs/records.md §2): a fresh dict
            # spreading the base, then the updated fields overriding it
            base = self._expr(expr.get("base"), where)
            parts = [f"**{base}"] + [
                f"{name!r}: {self._expr(value, where)}"
                for name, value in expr.get("updates") or []
            ]
            return "{" + ", ".join(parts) + "}"
        if kind == "list":
            return "[" + ", ".join(self._expr(item, where) for item in expr.get("items") or []) + "]"
        if kind == "arrow":
            params = ", ".join(_mangle(p) for p in expr.get("params") or [])
            prev_arrow = self._in_arrow
            self._in_arrow = True  # item 141: suppress the await-seed in the body
            try:
                if expr.get("async"):
                    # item 92: an async-flagged callback arrow. A lambda cannot be
                    # `async`, so the shape is decided statically (tail-coroutine ->
                    # plain lambda; sync -> `_revl_as_async` wrap; mixed -> refused).
                    return _py_async_arrow(
                        expr.get("body"), params,
                        lambda b: self._expr(b, where), requires=self.requires)
                return f"lambda {params}: {self._expr(expr.get('body'), where)}"
            finally:
                self._in_arrow = prev_arrow
        if kind == "format":
            self.uses.add("fmt")
            template = expr.get("template")
            if not isinstance(template, str):
                raise EmitError(f"{where}: format template must be a string")
            args = "".join(", " + self._expr(arg, where) for arg in expr.get("args") or [])
            return f"{_runtime_ref('fmt')}({template!r}{args})"
        if kind == "optfield":
            name = expr.get("name")
            if not isinstance(name, str) or not name.isidentifier():
                raise EmitError(f"{where}: bad optional field name {name!r}")
            # `x?.name`: short-circuit on Opt-None.
            binder, reader = _opt_bind(
                expr, expr.get("target"), self._expr(expr.get("target"), where))
            return (f"(None if {binder} is None else "
                    f"{_field_read(reader, name, rereadable=True)})")
        if kind == "optcall":
            method = expr.get("method")
            if not isinstance(method, str) or not method.isidentifier():
                raise EmitError(f"{where}: bad optional method name {method!r}")
            # the method is a stdlib builtin, rendered by the same table a
            # plain `.m(..)` uses; see the fn-body `optcall` branch.
            binder, reader = _opt_bind(
                expr, expr.get("target"), self._expr(expr.get("target"), where))
            args = [self._expr(a, where) for a in expr.get("args") or []]
            body = _render_builtin(method, reader, args, _RECV_VIA_OPT)
            return f"(None if {binder} is None else {body})"
        if kind == "spawn":
            # instance-parametric components (docs/design-v2-instances.md):
            # `spawn(_revl_ctx, <Component>, {config}, (realms,))` plugs a fresh child
            # instance, each provided key isolated into its own local realm. The
            # target is a module-level plugin dict emitted like any component.
            self.uses.add("spawn")
            target = expr.get("component")
            if not isinstance(target, str) or not target.isidentifier():
                raise EmitError(f"{where}: bad spawn component {target!r}")
            cfg = "{" + ", ".join(
                f"{k!r}: {self._expr(v, where)}"
                for k, v in (expr.get("config") or {}).items()) + "}"
            realms = tuple(expr.get("realms") or ())
            return f"{_runtime_ref('spawn')}(_revl_ctx, {target}, {cfg}, {realms!r})"
        if kind == "lease-acquire":
            # capability lease (item 294 Slice 2): the acquire binds a lease
            # HANDLE to the standing grant the session already minted from the
            # approved ticket (session._enforce_lease_gate raised it before boot).
            # `_revl_frame.acquire_lease` resolves the live lease grant for this
            # component + capability cone and returns the handle; the disposer's
            # `lease-revoke` retires it on the LIFO teardown.
            cap = expr.get("capability")
            ttl = expr.get("ttlMs")
            uses = expr.get("uses")
            return (f"_revl_frame.acquire_lease({cap!r}, "
                    f"{ttl!r}, {uses!r})")
        if kind == "lease-revoke":
            handle = expr.get("handle")
            if not isinstance(handle, str) or not handle.isidentifier():
                raise EmitError(f"{where}: bad lease handle {handle!r}")
            return f"{handle}.revoke()"
        if kind == "instance-get":
            # instance-parametric components (docs/design-v2-instances.md):
            # `s.<key>` reads a provision off a spawn handle. The handle
            # (`target`) is a `SpawnHandle`, whose `.get(key)` resolves the key
            # through the instance's own private local realm — only the spawner
            # holding this handle reaches it (supervision-tree addressing).
            target = self._expr(expr.get("target"), where)
            key = expr.get("key")
            if not isinstance(key, str) or not key.isidentifier():
                raise EmitError(f"{where}: bad instance-get key {key!r}")
            return f"{target}.get({key!r})"
        raise EmitError(f"{where}: unknown expression kind {kind!r}")

    # -- steps --------------------------------------------------------------

    def _label(self, suffix: str) -> str:
        self._counter += 1
        return f"{self.name}.{suffix}#{self._counter}"

    def _setup_step(self, out: _Lines, indent: int, step: dict, where: str) -> None:
        """A pure setup step inside a block-effect acquisition."""
        kind = step.get("step")
        if kind == "let":
            name = _ident(step.get("name"), f"{where}: setup bind")
            out.add(indent, f"{name} = {self._expr(step.get('value'), where)}")
        elif kind == "assign":
            name = _ident(step.get("name"), f"{where}: setup assign")
            out.add(indent, f"{name} = {self._expr(step.get('value'), where)}")
        elif kind == "expr":
            out.add(indent, self._expr(step.get("expr"), where))
        elif kind == "if":
            out.add(indent, f"if {self._expr(step.get('cond'), where)}:")
            for nested in step.get("then") or []:
                self._setup_step(out, indent + 1, nested, where)
            if step.get("else"):
                out.add(indent, "else:")
                for nested in step.get("else") or []:
                    self._setup_step(out, indent + 1, nested, where)
        elif kind == "assert":
            out.add(indent, f"assert {self._expr(step.get('expr'), where)}")
        else:
            raise EmitError(f"{where}: unknown setup step {kind!r}")

    def _body_step(self, out: _Lines, indent: int, step: dict, where: str) -> None:
        """A step at activation-body level — lines inside the body generator."""
        kind = step.get("step")
        if kind == "let-effect":
            if step.get("setup"):
                for setup in step["setup"]:
                    self._setup_step(out, indent, setup, where)
            wit = self._witnessed_extern(step.get("acquire"))
            if wit is not None:
                self._witnessed_step(out, indent, step, wit, where,
                                     bind=_ident(step.get("bind"), f"{where}: bind"))
            else:
                bind = _ident(step.get("bind"), f"{where}: bind")
                # item 131: an async-flagged acquisition awaits its landed
                # result, THEN registers the inverse — the inverse yield is the
                # next action in the same generator step, so registration is
                # boundary-atomic with the acquisition (design §4 clause 1).
                aw = "await " if step.get("async") else ""
                out.add(indent, f"{bind} = {aw}{self._expr(step.get('acquire'), where)}")
                undo = self._expr(step.get('undo'), where)
                if _is_map_cas(step.get("acquire")):
                    # result-guarded undo (item 397): identity inverse on a
                    # `false` CAS, so teardown never removes the winner's entry.
                    out.add(indent, f"yield lambda: ({undo} if {bind} else None)")
                else:
                    out.add(indent, f"yield lambda: {undo}")
        elif kind == "effect":
            if step.get("setup"):
                for setup in step["setup"]:
                    self._setup_step(out, indent, setup, where)
            wit = self._witnessed_extern(step.get("acquire"))
            if wit is not None:
                self._witnessed_step(out, indent, step, wit, where, bind=None)
            else:
                # item 131: an unbound async acquisition awaits before the
                # inverse yield, same boundary-atomic shape as the bound form.
                aw = "await " if step.get("async") else ""
                out.add(indent, f"{aw}{self._expr(step.get('acquire'), where)}")
                out.add(indent, f"yield lambda: {self._expr(step.get('undo'), where)}")
        elif kind == "fail":
            out.add(indent, f"raise RuntimeError({self._expr(step.get('message'), where)})")
        elif kind == "if":
            out.add(indent, f"if {self._expr(step.get('cond'), where)}:")
            for nested in step.get("then") or []:
                self._body_step(out, indent + 1, nested, where)
            if step.get("else"):
                out.add(indent, "else:")
                for nested in step.get("else") or []:
                    self._body_step(out, indent + 1, nested, where)
        elif kind == "emit":
            deferred = self._deferred_extern(step.get("expr"))
            if deferred is not None:
                self._deferred_step(out, indent, step, deferred, where)
                out.add(0)
                return
            # item 131: `await emit …` awaits the boundary crossing so the
            # emission actually fires (a bare async emit builds a coroutine that
            # never runs). The compensation registers AFTER, exactly as the sync
            # spelling registers after the fire (design §4 clause 1).
            aw = "await " if step.get("async") else ""
            out.add(indent, f"{aw}{self._emit_fire(step, where)}")
            if step.get("compensate") is not None:
                # item 247 (docs/design/teardown-contract.md): a compensation
                # is a first-class COMPENSATION entry on the frame's shared
                # per-activation LIFO stack, not a bare disposer — it
                # discharges (never runs) on a clean commit and runs
                # best-effort in PHASE 2 of an abort, only after every proof
                # inverse (bracket + transactional) in this activation has
                # completed. This is the two-phase model, not the old
                # single-interleaved-LIFO ordering the A5 TCK case predates
                # (docs/contract-errata.md's TCK A5 respec, a5a/a5b); it is
                # compensation, not inversion (§6.1).
                out.add(indent, "yield _revl_frame.compensation(lambda: "
                                 f"{self._expr(step.get('compensate'), where)})")
        elif kind == "approval":
            self._approval_step(out, indent, step, where)
        elif kind == "await":
            # A1: the await lands (inertia, paper §4.3.3), then the yield
            # closes the iteration — a divert during the await therefore
            # skips every later step instead of running to the next yield
            out.add(indent, f"await {self._expr(step.get('expr'), where)}")
            out.add(indent, "yield None  # iteration boundary (A1)")
        elif kind == "stream-iter":
            self._stream_iter(out, indent, step, where)
        elif kind == "timer":
            self._timer(out, indent, step, where)
        elif kind == "provide":
            self._provide(out, indent, step, where)
        elif kind == "return":
            raise EmitError(f"{where}: 'return' is only valid inside provide-method bodies")
        else:
            raise EmitError(f"{where}: unknown step {kind!r}")
        out.add(0)

    def _validated_call(self, expr: dict):
        """Item 257: if this `call` node is an `emit` on a `validated` service
        emission (through a req key), return `(schema, ctors, retry)` for the
        validate seam; else None. `schema` is the derived boundary schema carried
        on the method IR; `ctors` is a Python dict-literal mapping each case tag to
        its emitted ADT case class (or `None` when the validated return is not a
        tagged variant, e.g. a record or primitive, and the validated value is used
        as-is); `retry` is the Slice-2 validation-retry budget (§5.2), `0` when no
        `retry` clause was declared (one attempt, the Slice-1 seam)."""
        target = expr.get("target")
        if not (isinstance(target, dict) and target.get("kind") == "req"):
            return None
        svc_name = self.requires.get(target.get("name"))
        spec = (((self.services.get(svc_name) or {}).get("methods") or {})
                .get(expr.get("method")) or {})
        if not spec.get("validated"):
            return None
        return (spec.get("response_schema"),
                self._ctor_map(spec.get("response_schema")),
                spec.get("retry") or 0)

    def _ctor_map(self, schema) -> str:
        """The tag -> ADT-case-class dict literal for a discriminated-union
        response schema, or `"None"` when the schema is not a tagged union."""
        tags = []
        for arm in (schema or {}).get("oneOf") or []:
            tag = ((arm.get("properties") or {}).get("tag") or {}).get("const")
            if isinstance(tag, str):
                tags.append(tag)
        if not tags:
            return "None"
        return "{" + ", ".join(
            f"{tag!r}: {_ident(tag, 'adt case')}" for tag in tags) + "}"

    def _emit_parallel_group(self, out: "_Lines", indent: int, members: list,
                             where: str) -> None:
        """Render a proved-independent run of `emit` steps as a concurrent
        fire-then-join (item 259 slice 2, design §4.1). The members' host round
        trips are fired concurrently under one cordis loop; the join is
        single-threaded and in PLAN ORDER - flush each branch's buffered audit
        records, then register each SUCCESSFUL branch's compensation onto the
        activation's LIFO stack, then re-raise the first fault.

        The shape is byte-identical in EFFECT to the sequential
        `await <fire>; yield compensation` of each member on a clean run: the
        audit replays in plan order (byte-identical trace) and the compensations
        register in plan order (a correct LIFO stack). It diverges only on a fault
        or a divert, where it registers the members that actually fired (in plan
        order) and re-raises - teardown-EFFECT equivalent, not byte-identical
        `accumulated` (§3.3)."""
        self.uses.update({"_revl_parallel", "_revl_flush", "_revl_raise_first"})
        out.add(indent, "_revl_group = await _revl_parallel([")
        for member in members:
            # each fire is an un-awaited coroutine thunk; `_revl_branch` awaits it
            # under a branch-local record sink so mid-fire records buffer (C1).
            out.add(indent + 1, f"lambda: {self._emit_fire(member, where)},")
        out.add(indent, "])")
        for pos, member in enumerate(members):
            out.add(indent, f"_revl_flush(_revl_group[{pos}].records)")
            if member.get("compensate") is not None:
                # register the compensation only for a branch that actually fired,
                # in plan order - a faulted/diverted member registers nothing (its
                # forward effect is compensated only if it landed). §3.3 invariant P.
                out.add(indent, f"if _revl_group[{pos}].ok:")
                out.add(indent + 1, "yield _revl_frame.compensation(lambda: "
                                    f"{self._expr(member.get('compensate'), where)})")
        # re-raise the first fault AFTER every fired member's compensation is on
        # the stack, so the L-Raise teardown unwinds a correctly-ordered stack.
        out.add(indent, "_revl_raise_first(_revl_group)")

    def _emit_fire(self, step: dict, where: str) -> str:
        """The Python expression that fires an `emit` step's host body. When the
        step carries a `with a` approval edge (item 246), the fire is wrapped in
        `_revl_frame.approval_crossing(a, "C", lambda: <fire>)`: the frame checks
        and consumes the token durably before the body runs (Decision 3). No edge
        emits byte-identically to before."""
        fire = self._expr(step.get("expr"), where)
        approval = step.get("approval")
        if approval is None:
            return fire
        handle = self._expr(approval.get("expr"), where)
        cap = approval.get("capability")
        return (f"_revl_frame.approval_crossing({handle}, {cap!r}, "
                f"lambda: {fire})")

    def _approval_step(self, out: "_Lines", indent: int, step: dict,
                       where: str) -> None:
        """`let a = await approval[C] { fields }` (item 246): resolve the standing
        `Approval[C]` for this component from the owner ledger and bind the handle
        `with` threads to the crossing."""
        cap = step.get("capability")
        fields = ", ".join(
            f"{name!r}: {self._expr(value, where)}"
            for name, value in step.get("fields") or [])
        out.add(indent,
                f"{step['bind']} = _revl_frame.request_approval({cap!r}, "
                f"{{{fields}}})")

    def _deferred_extern(self, expr: Any) -> Optional[dict]:
        """The deferred emission extern an `emit` step's expression calls, or
        None (item 245, docs/design/245-session-commit.md, Decision 2).

        A deferred emission is spelled as a bare `emit <extern>(...)` whose
        callee renders as an IR `fn` node — the same shape a witnessed call
        takes — so matching its name against the deferred table is how the
        emitter tells a class-(b) enqueue from a class-(c) immediate emission.
        Returns None for every other expression, so non-deferred emissions emit
        byte-identically to before."""
        if not self.deferred or not isinstance(expr, dict):
            return None
        if expr.get("kind") != "fn":
            return None
        return self.deferred.get(expr.get("name"))

    def _deferred_step(self, out: _Lines, indent: int, step: dict, ext: dict,
                       where: str) -> None:
        """Emit a deferred emission (item 245): DO NOT invoke the host body.
        Append the descriptor to the session's deferral queue (and the WAL) and
        return Unit. The host body runs exactly once, at the session commit's
        flush, or never (on abort). This single-lowering property — one enqueue,
        no direct fire — is what makes the commit manifest's enumeration provably
        exhaustive (Decision 4).

        The queue entry carries a serializable named-call descriptor (receiver,
        method, captured args — never a closure, 243 rule 4) for the WAL and the
        manifest, plus a zero-arg thunk that fires the real host body at flush.
        `_revl_frame.enqueue_deferred` refuses if no session owner is registered:
        on the py tier the driver is always the owner, and the five ownerless
        tiers refuse a deferred call at emit (Decision 2's tier gate, Slice 2)."""
        expr = step.get("expr")
        method = expr.get("name")
        args = [self._expr(arg, where) for arg in expr.get("args") or []]
        fire = self._expr(expr, where)
        out.add(indent,
                f"_revl_frame.enqueue_deferred({method!r}, {method!r}, "
                f"[{', '.join(args)}], lambda: {fire}"
                f"{_deferred_register_kwargs(ext, args)})")

    def _witnessed_extern(self, acquire: Any) -> Optional[dict]:
        """The witnessed extern descriptor a step's acquisition calls, or None.

        A witnessed effect (item 243) is spelled as an effect-position call to a
        `witnessed` extern; a component step call renders as an IR `fn` node, so
        matching its name against the witnessed table is how the emitter tells a
        transaction from an ordinary bracket. Returns None for every other
        acquisition, so non-witnessed effects emit byte-identically to before."""
        if not self.witnessed or not isinstance(acquire, dict):
            return None
        if acquire.get("kind") != "fn":
            return None
        return self.witnessed.get(acquire.get("name"))

    def _witnessed_step(self, out: _Lines, indent: int, step: dict, ext: dict,
                        where: str, bind: Optional[str]) -> None:
        """Emit a witnessed effect (item 243): run the mutation, and on `Ok`
        register the extern's DECLARED inverse into the accumulator as a
        TRANSACTIONAL entry carrying the `Ok` witness.

        Unlike a bracket (`yield lambda: <undo>`, replays on every teardown),
        this yields a transactional disposer that replays ONLY on abort and is
        discharged on a clean commit (`Frame.transactional` / `_Transactional`).
        The inverse is the extern's own `undo` — no site-spelled undo; the
        accumulator owns it — and it binds the `Ok` payload as `result` (the
        implicit witness binder, docs/design/243 'Slice 1 as implemented' #1).
        On `Err` nothing is registered: a failed mutation touched nothing, so it
        must not schedule a rollback (Ok-conditional)."""
        self._counter += 1
        tmp = f"_revl_wit{self._counter}"
        undo = self._expr(ext["undo"], where)  # e.g. `restore(result)`
        out.add(indent, f"{tmp} = {self._expr(step.get('acquire'), where)}")
        out.add(indent, f"if isinstance({tmp}, Ok):")
        # item 309: pass the idempotency register to the transactional entry ONLY
        # when the author declared it, so a witnessed extern with no register
        # emits byte-identical code (the additivity discipline). The register
        # gates free-vs-fenced abort-Phase-1 fencing and free-vs-fenced recover.
        extra = _transactional_register_kwargs(ext)
        out.add(indent + 1,
                f"yield _revl_frame.transactional((lambda result: {undo}), "
                f"{tmp}.value{extra})")
        if bind is not None:
            out.add(indent, f"{bind} = {tmp}")

    def _method_witnessed_step(self, out: _Lines, indent: int, step: dict, ext: dict,
                               where: str, bind: Optional[str]) -> None:
        """Emit a witnessed effect inside a PROVIDE-METHOD body (item 318): the
        per-tool-call H1 seam. Run the mutation, and on `Ok` register the
        extern's DECLARED inverse into the ENCLOSING COMPONENT'S activation
        frame as a transactional entry carrying the `Ok` witness.

        The activation-body form (`_witnessed_step`) `yield`s the disposer into
        the body generator's own LIFO stack. A method body has no such
        generator, and adopting the entry as a sibling `ctx.effect` is unsound
        (cordis disposes it before the body's `drain`, so a clean unload would
        observe `_committed` still False and wrongly revert the deliverable —
        see `Frame.transactional_method`). So this calls the frame directly:
        `_revl_frame.transactional_method(...)` parks the entry for `drain` to
        dispose once the commit-vs-abort bit is settled. On `Err` nothing is
        registered (Ok-conditional): a failed mutation touched nothing, so it
        schedules no rollback. `_revl_frame` is in scope in every method body
        (it is the component's activation frame the method closes over)."""
        self._counter += 1
        tmp = f"_revl_wit{self._counter}"
        undo = self._expr(ext["undo"], where)  # e.g. `restore(result)`
        out.add(indent, f"{tmp} = {self._expr(step.get('acquire'), where)}")
        out.add(indent, f"if isinstance({tmp}, Ok):")
        # TODO(309-slice3): thread the idempotency register (item 309) into the
        # provide-method transactional entry too, mirroring `_witnessed_step`, so
        # a method-seam witnessed inverse (item 318) fences its abort Phase-1 apply
        # and recovers free-vs-fenced. The activation-body path is wired; this
        # narrower per-tool-call seam is deferred.
        out.add(indent + 1,
                f"_revl_frame.transactional_method((lambda result: {undo}), {tmp}.value)")
        if bind is not None:
            out.add(indent, f"{bind} = {tmp}")

    def _stream_iter(self, out: _Lines, indent: int, step: dict,
                     where: str) -> None:
        """A `stream-iter` body step (item 130 Slice 4): `every <x> in <sub>
        { … }`, the async-iteration form.

        The whole form is defined by the three operations Slice 1 shipped, so it
        adds no runtime primitive and no new teardown accounting:

            while True:
                <x> = await <sub>.next()
                yield None            # iteration boundary (A1)
                if Stream.is_closed(<x>):
                    break
                <body>

        Three properties are load-bearing and each is a line above:

        * the `yield` sits immediately after the await, exactly as the plain
          `await` step emits it — the await LANDS (inertia, paper §4.3.3) and the
          yield closes the iteration, so a divert while the consumer is parked
          abandons the loop instead of running one more body turn. This is what
          keeps the bracket inverse reachable off the teardown path: `close`
          (or owner withdrawal) trips the cancel token, the parked `next`
          resolves as `Closed`, and teardown never waits on the provider (§9
          Part A);
        * a `Closed` terminal ENDS the loop rather than entering the body — the
          terminal is not an item, and running the effectful callback on it would
          be the silent-data invention the design forbids;
        * a `Faulted` terminal is NOT tested for, because `next` RAISES it
          (`StreamFaulted`). The exception propagates out of the loop and out of
          the body generator, the activation fails, and the accumulated prefix —
          subscription bracket included — reverts LIFO, which CLOSES the
          subscription (A8, §4.7). That is the "a failed handler does not leave a
          subscription active" obligation, and it is delivered by not catching
          anything here.

        A `fail` in the body raises the same way, for the same reason."""
        self.uses.add("Stream")
        item = _mangle(_ident(step.get("bind"), f"{where}: stream item"))
        subject = self._expr(step.get("subject"), where)
        out.add(indent, "while True:")
        out.add(indent + 1, f"{item} = await {subject}.next()")
        out.add(indent + 1, "yield None  # iteration boundary (A1)")
        out.add(indent + 1, f"if Stream.is_closed({item}):")
        out.add(indent + 2, "break")
        body = step.get("body") or []
        if not body:  # pragma: no cover — the parser rejects an empty body
            raise EmitError(f"{where}: an `every … in` body is empty")
        for inner in body:
            self._body_step(out, indent + 1, inner, where)

    def _timer(self, out: _Lines, indent: int, step: dict, where: str) -> None:
        """A `timer` body step (item 57): a revertible schedule.

        The firing closure holds the timer body's emissions; `schedule_every`/
        `schedule_after` register it with the clock coeffect and return a
        handle, and the yielded `handle.cancel()` is the derived inverse — so
        unloading the component drains this like any other effect and provably
        cancels the timer (no orphaned interval; residue-free teardown).  The
        clock does not advance on its own: `revl test`/replay drives it, which
        is what makes a firing a deterministic timeline step rather than a
        wall-clock race (docs/time-coeffect.md).

        item 170: a timer body the checker coloured `async` (it reaches an
        async op — a req-target async service operation, an async extern, or a
        colored fn) fires into an `Async[T]` in-flight window (item 106). Each
        async emission is *spawned* as a tracked asyncio task on the running
        loop rather than run inline: the firing returns immediately, and the
        harness's `_revl_settle` after a clock advance drains the in-flight work
        to quiescence (docs/time-coeffect.md §advance already awaits it). The
        inverse cancels the schedule AND every still-in-flight task, so unload
        leaves no orphaned in-flight async work — the sync path's residue-free
        teardown extended to the async case (R4/A8). A sync timer body carries
        no `async` key and emits byte-identically to before."""
        mode = step.get("mode")
        schedule = "schedule_every" if mode == "every" else "schedule_after"
        self.uses.add(schedule)
        self._counter += 1
        fn = f"_timer_{self._counter}"
        handle = f"{fn}_h"
        interval = int(step.get("interval_ms"))
        emissions = step.get("body") or []
        for emission in emissions:
            if emission.get("step") != "emit":  # pragma: no cover — lowerer invariant
                raise EmitError(f"{where}: a timer body carries emissions only, "
                                f"found {emission.get('step')!r}")

        if not step.get("async"):
            out.add(indent, f"def {fn}():")
            if not emissions:  # pragma: no cover — the parser rejects an empty body
                out.add(indent + 1, "pass")
            for emission in emissions:
                out.add(indent + 1, self._expr(emission.get("expr"), where))
            out.add(indent, f"{handle} = {_runtime_ref(schedule)}({interval}, {fn})")
            out.add(indent, f"yield lambda: {handle}.cancel()")
            return

        # async in-flight window (item 170): the firing spawns each async
        # emission as a tracked task, and the inverse cancels the schedule plus
        # any task still in flight — so a torn-down timer leaves no orphaned
        # in-flight async work (R4/A8).
        self.uses.add("__asyncio__")
        inflight = f"{fn}_inflight"
        out.add(indent, f"{inflight} = set()")
        out.add(indent, f"def {fn}():")
        if not emissions:  # pragma: no cover — the parser rejects an empty body
            out.add(indent + 1, "pass")
        for emission in emissions:
            expr = emission.get("expr")
            rendered = self._expr(expr, where)
            if _py_reaches_coroutine(expr, self.requires):
                # spawn the suspension into the in-flight window and track it so
                # the inverse can cancel it; a done task drops itself from the set
                out.add(indent + 1, f"_revl_task = _revl_asyncio.ensure_future({rendered})")
                out.add(indent + 1, f"{inflight}.add(_revl_task)")
                out.add(indent + 1, f"_revl_task.add_done_callback({inflight}.discard)")
            else:
                # a sync emission in a mixed body still runs inline
                out.add(indent + 1, rendered)
        out.add(indent, f"{handle} = {_runtime_ref(schedule)}({interval}, {fn})")
        cancel = f"{fn}_cancel"
        out.add(indent, f"def {cancel}():")
        out.add(indent + 1, f"{handle}.cancel()")
        out.add(indent + 1, f"for _revl_task in list({inflight}):")
        out.add(indent + 2, "_revl_task.cancel()")
        out.add(indent, f"yield {cancel}")

    def _provide(self, out: _Lines, indent: int, step: dict, where: str) -> None:
        name = _ident(step.get("name"), f"{where}: provide key")
        if name in _CONTEXT_MEMBERS:
            raise EmitError(
                f"{where}: provision key {name!r} collides with a cordis-py "
                f"Context member — `_revl_ctx.{name}` already exists, so the "
                f"provision would fail at activation. Rename the key."
            )
        service = step.get("service")
        if service not in self.services:
            raise EmitError(f"{where}: provide {name!r} names unknown service {service!r}")
        if self.provides.get(name) != service:
            raise EmitError(f"{where}: provide {name!r} does not match the component header")
        cls = f"_{_pascal(name)}"
        out.add(indent, f"class {cls}:")
        out.add(indent + 1, f'"""service {service}, provided at key "{name}"."""')
        methods = step.get("methods") or []
        if not methods:
            out.add(indent + 1, "pass")
        for method in methods:
            out.add(0)
            self._method(out, indent + 1, name, method, where)
        out.add(0)
        # runtime-derived revertible provision (R5): the withdrawal inverse is
        # _revl_ctx.provide's own disposer, yielded into the component accumulator
        out.add(indent, f"yield _revl_ctx.provide({name!r})")
        out.add(indent, f"_revl_ctx.set({name!r}, {cls}())")

    def _method(self, out: _Lines, indent: int, provide_name: str, method: dict, where: str) -> None:
        name = _ident(method.get("name"), f"{where}: method name")
        service = self.services.get(self.provides.get(provide_name)) or {}
        spec = (service.get("methods") or {}).get(name)
        if spec is None:
            raise EmitError(f"{where}: method {name!r} is not part of the provided service")
        params = [_ident(param, f"{where}.{name}: param") for param in method.get("params") or []]
        # v1/A6: method params are the surface names binding the body and may
        # differ from the service's declared names; only the arity must agree
        if len(params) != len(spec.get("params") or []):
            raise EmitError(f"{where}: method {name!r} arity does not match the service")
        mwhere = f"{where}.{name}"
        # services 2.0 (§5): async operations lower to Python async methods,
        # whose bodies may `await` host async values (no divert boundary)
        method_is_async = bool(spec.get("async"))
        out.add(
            indent,
            f"{'async ' if method_is_async else ''}def {name}(self{''.join(', ' + p for p in params)}):",
        )
        # item 421 F6: a parameter the service declared `Secret[T]` is a declared
        # DISCLOSURE RECEIVER. The qualifier itself is stripped in `taint.py`, so
        # `params[i]["secret"]` is the only surviving record that this position is
        # confidential, read here exactly as `confidential.SecretIndex` reads it
        # in the recorder. Registering the value at the head of the receiver is
        # what lets `runtime._record` scrub it out of the operator console trace
        # when the body goes on to use it as a `Map` key (the shipped
        # `demo/components/user_cache.rvl` idiom), a `pool.query`, or a stream
        # item. Emitted ONLY for a method that actually declares one, so every
        # other emitted module is byte-identical.
        secret_params = [
            p for index, p in enumerate(params)
            if isinstance((spec.get("params") or [])[index], dict)
            and (spec.get("params") or [])[index].get("secret")
        ]
        if secret_params:
            self.uses.add("mark_secret")
            out.add(indent + 1,
                    f"{_runtime_ref('mark_secret')}({', '.join(secret_params)})")
        body = method.get("body") or []
        if not body:
            if not secret_params:
                out.add(indent + 1, "pass")
            return
        prev_async = self._in_async
        self._in_async = method_is_async
        try:
            for step in body:
                self._method_step(out, indent + 1, provide_name, name, step, mwhere, method_is_async)
        finally:
            self._in_async = prev_async

    def _method_step(
        self,
        out: _Lines,
        indent: int,
        provide_name: str,
        method_name: str,
        step: dict,
        where: str,
        method_is_async: bool,
    ) -> None:
        """A step inside a provide-method body — runs while ACTIVE; effect
        steps must join the component's accumulator (via the Frame)."""
        kind = step.get("step")
        label = f"{provide_name}.{method_name}"
        if kind == "let":
            # a plain value binding inside a method body
            name = _ident(step.get("name"), f"{where}: let")
            out.add(indent, f"{name} = {self._expr(step.get('value'), where)}")
            return
        if kind == "assign":
            name = _ident(step.get("name"), f"{where}: assign")
            out.add(indent, f"{name} = {self._expr(step.get('value'), where)}")
            return
        if kind == "effect":
            wit = self._witnessed_extern(step.get("acquire"))
            if wit is not None:
                self._method_witnessed_step(out, indent, step, wit, where, bind=None)
            else:
                fn = f"_effect_{self._counter}"
                out.add(indent, f"def {fn}():")
                out.add(indent + 1, self._expr(step.get("acquire"), where))
                out.add(indent + 1, f"yield lambda: {self._expr(step.get('undo'), where)}")
                out.add(indent, f"_revl_frame.adopt(_revl_ctx.effect({fn}, {self._label(label)!r}))")
        elif kind == "let-effect":
            wit = self._witnessed_extern(step.get("acquire"))
            if wit is not None:
                self._method_witnessed_step(
                    out, indent, step, wit, where,
                    bind=_ident(step.get("bind"), f"{where}: bind"))
            else:
                bind = _ident(step.get("bind"), f"{where}: bind")
                acquire = self._expr(step.get("acquire"), where)
                undo = self._expr(step.get("undo"), where)
                if _is_map_cas(step.get("acquire")):
                    # result-guarded undo (item 397): the accumulator entry
                    # receives the bound Bool; on a `false` CAS the inverse is
                    # the identity, so teardown leaves the winner's entry alone.
                    undo_fn = f"lambda {bind}: ({undo} if {bind} else None)"
                else:
                    undo_fn = f"lambda {bind}: {undo}"
                out.add(
                    indent,
                    f"{bind} = _revl_frame.acquire({self._label(label)!r}, "
                    f"lambda: {acquire}, {undo_fn})",
                )
        elif kind == "emit":
            deferred = self._deferred_extern(step.get("expr"))
            if deferred is not None:
                self._deferred_step(out, indent, step, deferred, where)
            elif step.get("compensate") is not None:
                # item 247 (method-body compensate remainder) (docs/design/teardown-contract.md): a method-body
                # `emit ... compensate ...` is a first-class COMPENSATION on the
                # component's activation frame, exactly as the activation-body
                # site is (item 247) — NOT a bare `yield lambda: <offset>` adopted
                # as a sibling effect. A bare adopted disposer is a BRACKET: cordis
                # disposes it before the body drain, so it fires the offset on a
                # CLEAN commit (destroying the deliverable), interleaves with the
                # proof inverses, and is unguarded. Routing through
                # `Frame.compensation_method` (the compensation analog of item
                # 318's `transactional_method`) makes it abort-only: discharged on
                # a clean commit, drained in Phase 2 after every proof inverse,
                # guarded and residue-collected. Fire the emission first, then
                # register — the sync spelling of the activation body's
                # `<fire>; yield _revl_frame.compensation(...)`.
                out.add(indent, self._emit_fire(step, where))
                out.add(indent, "_revl_frame.compensation_method(lambda: "
                                f"{self._expr(step.get('compensate'), where)})")
            else:
                out.add(indent, self._emit_fire(step, where))
        elif kind == "return":
            if step.get("expr") is None:
                out.add(indent, "return")
            else:
                out.add(indent, f"return {self._expr(step.get('expr'), where)}")
        elif kind == "await":
            if not method_is_async:
                raise EmitError(f"{where}: 'await' is not allowed inside sync provide-method bodies (A1)")
            out.add(indent, f"await {self._expr(step.get('expr'), where)}")
        elif kind == "provide":
            raise EmitError(f"{where}: nested 'provide' inside a method body is not lowerable")
        else:
            raise EmitError(f"{where}: unknown step {kind!r}")

    # -- component ----------------------------------------------------------

    def emit(self) -> _Lines:
        self.uses.add("Frame")
        out = _Lines()
        where = self.name

        if self.config_fields:
            self.uses.add("ConfigSchema")
            out.add(0, f"_{self.snake.upper()}_CONFIG = {_runtime_ref('ConfigSchema')}([")
            for spec in self.config_fields:
                field = _ident(spec.get("name"), f"{where}: config field")
                out.add(1, f"({field!r}, {spec.get('type')!r}, {spec.get('default')!r}),")
            # item 256 Slice 3: the fields declared `Secret[T]`. The runtime keeps
            # their values out of the `<name>.config` trace line (stdout under
            # `revl run`, the `revl_load` response under MCP) while still handing
            # the real value to the component. Emitted only when the author
            # declared one, so every existing module stays byte-identical.
            secret = [spec.get("name") for spec in self.config_fields
                      if spec.get("secret")]
            out.add(0, f"], secret={secret!r})" if secret else "])")
            out.add(0)
            out.add(0)

        # A1: a body containing an `await` step compiles to an async
        # generator; the runtime treats each yield as an iteration boundary
        # and the await as an in-flight iteration (paper §4.3.2-3). item 131:
        # an async-flagged `effect`/`let-effect`/`emit` step (an awaited
        # acquisition or emission) is likewise a suspension in the body and
        # forces the `async def` generator. Timer steps are excluded: a timer's
        # `async` flag (item 170) colors its OWN runtime-awaited firing, not the
        # activation body generator, so it must not flip the body to async here.
        # item 130 Slice 4: a `stream-iter` step awaits `<sub>.next()` once per
        # delivered item, so it is a suspension in the body exactly as an
        # `await` step is, and it forces the `async def` generator the same way.
        is_async = any(
            step.get("step") in ("await", "stream-iter")
            or (step.get("step") in ("effect", "let-effect", "emit")
                and step.get("async"))
            for step in self.ir.get("body") or [])

        out.add(0, f"def _{self.snake}_apply(_revl_ctx, _revl_config):")
        out.add(1, f"_revl_frame = {_runtime_ref('Frame')}(_revl_ctx, {self.name!r})")
        # item 167: build one router proxy per routed key before the body, so a
        # provide-method reading `<key>` fans each call out across its worker
        # realms (round-robin/least_loaded, re-resolving liveness per call).
        for key in self.routes:
            route = self.routes[key]
            realms = list(route.get("realms") or [])
            strategy = route.get("strategy")
            out.add(1, f"_revl_route_{key} = _revl_router("
                       f"_revl_ctx, {key!r}, {realms!r}, {strategy!r})")
        out.add(0)
        out.add(1, f"{'async def' if is_async else 'def'} _body():")
        # item 247 second-pass (F1): two sentinel yields bracket the ordinary
        # steps (mirrors the ts/go tiers). `begin` yielded FIRST -> disposed
        # LAST (cordis LIFO): the Phase-2 post-unwind hook, at the bottom of the
        # unwind stack, so a POST-activation abort's activation-body offsets —
        # which enqueue only when cordis disposes them, BELOW `drain` — are
        # drained after Phase 1 completes instead of being lost. `drain` yielded
        # LAST -> disposed FIRST, the commit signal every earlier entry reads.
        out.add(2, "yield _revl_frame.begin")
        # item 259 slice 2: a group-aware walk - a run of `emit` steps the checker
        # proved independent fires concurrently (`_emit_parallel_group`); every
        # other step, and every un-grouped emit, renders exactly as before.
        steps = self.ir.get("body") or []
        i = 0
        while i < len(steps):
            step = steps[i]
            group = self._parallel_leaders.get(id(step)) if isinstance(step, dict) else None
            if group is not None:
                self._emit_parallel_group(out, 2, group, where)
                i += len(group)  # members are contiguous (checked in the leader map)
            else:
                self._body_step(out, 2, step, where)
                i += 1
        out.add(2, "yield _revl_frame.drain")
        out.add(0)
        out.add(1, "_revl_frame.install(_body)")
        out.add(0)
        out.add(0)
        out.add(0, f"{self.name} = {{")
        out.add(1, f"'name': {self.name!r},")
        # item 167: routed keys never enter the inject gate — they have no
        # single-realm provider, so a fiber waiting on one would pend forever.
        # The router proxy resolves them lazily per call instead.
        inject_keys = [key for key in self.requires if key not in self.routes]
        if self.intercept:
            # v2: dict-form inject — non-null values land in the fiber
            # context's intercept chain (the consumer-declared d(k))
            inject = {key: self.intercept.get(key) for key in inject_keys}
            out.add(1, f"'inject': {inject!r},")
        else:
            out.add(1, f"'inject': {inject_keys!r},")
        out.add(1, f"'apply': _{self.snake}_apply,")
        if self.config_fields:
            # cordis-py reads Config off dict plugins via dict.get (fork
            # commit 1c5e6f1), so the schema rides on the plugin dict and the
            # runtime's resolve_config validates/resolves before apply runs.
            out.add(1, f"'Config': _{self.snake.upper()}_CONFIG,")
        if self.isolate:
            # v2: realm placements, applied by runtime.plug() BEFORE
            # ctx.plugin — the fiber's context chain is fixed at plugin time
            out.add(1, f"'isolate': {dict(self.isolate)!r},")
        out.add(0, "}")
        # Block-effect setup can reference host roots through pure v3 `var`
        # nodes (e.g. Pool.open inside `effect { ... }`); collect them even
        # though the old `host` fast path was not used.
        self.uses.update(_find_host_roots(self.ir.get("body") or []))
        return out


# ---------------------------------------------------------------------------
# v2.0 (ir_version 3): types & pure functions (docs/syntax-2.0.md §2–§3)
# ---------------------------------------------------------------------------

_PY_TYPE = {"Int": "int", "Float": "float", "Bool": "bool", "Str": "str", "Bytes": "bytes", "Unit": "None"}

_PY_BIN_OPS = {
    "==": "==", "===": "==", "!=": "!=", "!==": "!=",
    "<": "<", ">": ">", "<=": "<=", ">=": ">=",
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
    "&&": "and", "||": "or",
}


def _split_fn_type(name: str) -> tuple[list[str], str] | None:
    """`"(Int, Str) -> Bool"` -> `(["Int", "Str"], "Bool")`, else None.

    A function type (docs/function-types.md) is the one surface type that is
    not spelled `Head[Args]`, so it is recognised before the generic path.
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
                return (_split_types(inner) if inner else []), rest[2:].strip()
    return None


def _is_async_fn_type(type_name: Any) -> bool:
    """True for a function type whose declared return is `Async[…]` — the
    item-92 spelling that colors a first-class callback parameter."""
    if not isinstance(type_name, str):
        return False
    fn = _split_fn_type(type_name.strip())
    if fn is None:
        return False
    return fn[1].strip().startswith("Async[")


def _split_types(inner: str) -> list[str]:
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


def _py_type(type_name: str) -> str:
    type_name = type_name.strip()
    fn = _split_fn_type(type_name)
    if fn is not None:
        # a function type `(Int, Str) -> Bool` (docs/function-types.md).
        # Python function values are plain callables, so the annotation is the
        # only thing that carries the signature here.
        params, returns = fn
        rendered = ", ".join(_py_type(p) for p in params)
        return f"Callable[[{rendered}], {_py_type(returns)}]"
    if "[" in type_name:
        base = type_name[: type_name.index("[")]
        inner = type_name[type_name.index("[") + 1: type_name.rindex("]")]
        args = _split_types(inner)
        if base == "Opt":
            return f"Optional[{_py_type(args[0])}]"
        if base == "List":
            return f"list[{_py_type(args[0])}]"
        if base == "Map":
            return f"dict[{_py_type(args[0])}, {_py_type(args[1])}]"
        if base == "Result":
            # tagged at runtime (Ok/Err classes); the annotation is advisory
            return "Any"
        return base + "[" + ", ".join("Any" for _ in args) + "]"
    if type_name in _PY_TYPE:
        return _PY_TYPE[type_name]
    return type_name  # named record/variant type or generic param


# The `typing` names `_py_type` can put in a record annotation. `Union` was
# imported alongside them and is not one of them, so every module that declared
# a type paid for a name nothing could ever spell (roadmap item 436 F9).
_PY_TYPING_NAMES = ("Any", "Callable", "Optional")


def _typing_imports(types: dict) -> list:
    """The `typing` names the emitted record annotations actually mention.

    Record annotations are the only place `_py_type` is rendered, so a module
    whose type declarations are all variants imports nothing at all.
    """
    used: set = set()
    for spec in types.values():
        if spec.get("kind") != "record":
            continue
        for ftype in (spec.get("fields") or {}).values():
            # the same identifier tokenisation the forward-ref check uses, so
            # the self-host port can reuse `ident_tokens` and agree exactly
            used.update(re.findall(r"[A-Za-z_]\w*", _py_type(ftype)))
    return [n for n in _PY_TYPING_NAMES if n in used]


def _emit_types(types: dict) -> "_Lines":
    out = _Lines()
    # THE EXTERNAL CONTRACT OF AN EMITTED RECORD (docs/records.md §7).
    #
    # A record VALUE is a plain dict keyed by the revl field name, spelled
    # exactly as the source spells it: `{'id': 0, 'from': 'a'}`. That holds for
    # every producer and every consumer, INSIDE the module and OUT: a record
    # literal, a `record_update` spread, a field read, a destructure, a value a
    # host hands in across a service boundary, and a value `src/revl/fault.py`
    # generates for a `prop test` or an auto-mock. There is one representation,
    # and the class below is not it.
    #
    # The class is a SHAPE DECLARATION — annotations only, no constructor. It
    # cannot be the value carrier even in principle: a field's CLASS ATTRIBUTE
    # is `_mangle`d for Python keyword collisions while its RUNTIME KEY is the
    # raw revl name, so `type Q = { from: Str }` emits `class Q: from_: str`
    # against a read of `q['from']`. An instance of `Q` therefore answers no
    # field read the emitter writes. Constructing it is always wrong, and the
    # `getattr` fallback in `_field_read` is for ADT payloads (real objects),
    # not for records.
    #
    # It used to carry `@dataclass`, which built an `__init__`, `__repr__` and
    # `__eq__` for that never-constructed class at every module load: executing
    # `tests/fixtures/emit_py_corpus/types.rvl`'s three record declarations was
    # 17246 bytecode instructions, 468 Python frames and a 105973 B tracemalloc
    # peak, against 237, 9 and 14711 without the decorator (roadmap item 436
    # F9; `bench/codegen/python/run.py --load` measures this). The decorator is
    # gone and the class stays, because the field names and types are what
    # makes the emitted module readable next to the revl source.
    #
    # Forward-reference support: revl types may be mutually recursive (a
    # record referencing an ADT defined later, or vice versa), but Python
    # evaluates class-body annotations at class-definition time, so a bare
    # name would raise NameError. We cannot use `from __future__ import
    # annotations` (PEP 563), which would leave a consumer that resolves these
    # annotations reaching for a module it exec'd without registering in
    # sys.modules. Instead, quote only the annotations that actually reference
    # a not-yet-emitted type: a string annotation is inert, and nothing in the
    # emitted module resolves one.
    all_names = {_ident(name, "type name") for name in types}
    emitted: set[str] = set()

    def _ann(ftype: str) -> str:
        """Render one field/payload annotation, quoting forward refs."""
        rendered = _py_type(ftype)
        mentioned = set(re.findall(r"[A-Za-z_]\w*", ftype))
        if mentioned & (all_names - emitted):
            return repr(rendered)
        return rendered

    for name, spec in types.items():
        name = _ident(name, "type name")
        if spec["kind"] == "record":
            out.add(0, f"class {name}:")
            if not spec["fields"]:
                out.add(1, "pass")
            for field, ftype in spec["fields"].items():
                # the field is a class attribute name here (a real Python
                # identifier), so a keyword-named field is renamed; record
                # VALUES are dicts read by string key, so this annotation-only
                # rename never has to agree with a runtime attribute access,
                # and it stays INJECTIVE so two revl fields can never collapse
                # onto one annotation (item 165)
                out.add(1, f"{_mangle(field)}: {_ann(ftype)}")
            emitted.add(name)
        else:
            out.add(0, f"class {name}:")
            out.add(1, "__slots__ = ()")
            emitted.add(name)  # base precedes its cases; case refs are never forward
            out.add(0)
            for case in spec["cases"]:
                cname = _ident(case["name"], "case name")
                if case["payload"] is None:
                    out.add(0, f"class {cname}({name}):")
                    out.add(1, "__slots__ = ()")
                    # value-semantic equality: all instances of a no-payload
                    # case are equal
                    out.add(1, "def __eq__(self, other):")
                    out.add(2, f"return isinstance(other, {cname})")
                    out.add(1, "def __hash__(self):")
                    out.add(2, f"return hash({cname!r})")
                else:
                    out.add(0, f"class {cname}({name}):")
                    out.add(1, '__slots__ = ("value",)')
                    out.add(1, "def __init__(self, value):")
                    out.add(2, "self.value = value")
                    out.add(1, "def __eq__(self, other):")
                    out.add(2, f"return isinstance(other, {cname}) and other.value == self.value")
                    out.add(1, "def __hash__(self):")
                    out.add(2, f"return hash(({cname!r}, self.value))")
                out.add(0)
        out.add(0)
    return out


def _is_float_expr(node: object) -> bool:
    """Is this expression *syntactically* certain to be a `Float`?

    Only what the node proves on its own — a Float literal, a `/` (true
    division always yields Float), a Float-annotated arithmetic node, or a
    unary minus of one. This mirrors the TypeScript backend's proof; the
    emitter has no type environment, so a False answer only costs the default
    `str()`, which is already correct for a non-Float. See docs/strings.md.
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


def _interp_fstring(parts) -> str:
    """Emit a `${…}` template as a string concatenation.

    Each interpolated segment is a full expression (`["expr", ir_node]`),
    stringified with `str(...)`; text segments are Python string literals.
    Concatenation (not an f-string) so an interpolated expression may itself
    contain quotes or braces. A `Float` renders through `_revl_ftoa` (the
    canonical ECMAScript `Number::toString` form), not python's `str` — `str`
    gives `nan`/`0.0`/`-0.0`, which no other tier produces (docs/strings.md).
    """
    pieces: list[str] = []
    for kind, value in parts:
        if kind == "text":
            pieces.append(repr(value))
        elif _is_float_expr(value):
            pieces.append(f"_revl_ftoa({_expr(value)})")
        else:  # ["expr", ir_node]
            pieces.append(f"str({_expr(value)})")
    if not pieces:
        return "''"
    return "(" + " + ".join(pieces) + ")"


def _arm_source(body: object) -> object:
    """The IR node an arm body was rendered from.

    The component renderer hands `_match_expr` bodies it has ALREADY rendered
    (`_RenderedBody`), whose text is opaque to the checks below; each one keeps
    the node it came from so both renderers answer the same question. `None`
    means "no node available", and both checks read that as "assume the worst".
    """
    return body.source if isinstance(body, _RenderedBody) else body


def _arm_body_is_bind(body: object, bind: str) -> bool:
    """Is this arm body EXACTLY the payload bind — `Some(v) => v`?

    `var` (a fn body) and `name` (a component body) are the two nodes that are
    a bare local read, and both render as the identifier itself. `Some` and
    `None` are rendered specially by `_expr` (identity and the host `None`), so
    a bind under either name is left to the binder rather than inlined.
    """
    node = _arm_source(body)
    if not isinstance(node, dict) or bind in ("Some", "None"):
        return False
    kind = node.get("kind")
    if kind == "var":
        return node.get("name") == bind
    if kind == "name":
        return node.get("id") == bind
    return False


def _arm_body_mentions(body: object, bind: str) -> bool:
    """Could this arm body read `bind`? Answered CONSERVATIVELY: true if the
    name occurs anywhere in the subtree at all, as a node value or an object
    key, whether or not it is in a position that reads a local.

    A false positive costs the arm its binder-free spelling and nothing else. A
    false negative would delete a live bind, so the walk deliberately does not
    reason about which node kinds carry a name: an arrow parameter list, a
    nested match's `bind`, a record key and a plain `var` all count the same,
    and anything the walk does not recognise counts as a mention.
    """
    if isinstance(body, _RenderedBody) and body.source is None:
        return True  # rendered with no node behind it: keep the binder
    node = _arm_source(body)
    if isinstance(node, str):
        return node == bind
    if isinstance(node, list):
        return any(_arm_body_mentions(item, bind) for item in node)
    if isinstance(node, dict):
        return any(key == bind or _arm_body_mentions(value, bind)
                   for key, value in node.items())
    return False  # a scalar leaf: a literal, a flag, or an absent field


def _match_expr(scrutinee: str, arms: list, awaited: bool = False) -> str:
    """Emit a match expression as a nested `isinstance` chain.

    Python has no expression-level `elif`, so the chain is built from nested
    conditional expressions. The scrutinee is evaluated exactly once into
    `match`; payload arms bind the case's `.value` before the arm body runs.
    A wildcard arm becomes the chain's final `else`.

    The scrutinee is bound by a walrus carried in the first arm's test (item
    436 F3); each payload bind normally rides a one-shot lambda. But a lambda
    is a SYNC frame: an arm body that crosses an async
    boundary renders an `await`, and `await` inside a lambda is a py
    `SyntaxError` (item 263 — the arm helper hoisted out of an async body must
    inherit its color). So the awaited payload bind needs a binder that is not
    a lambda but is still a SCOPE.

    A walrus is not (roadmap item 163): `(v := match.value)` assigns in the
    ENCLOSING frame, so an arm bind named after a local of the function holding
    the match silently clobbers it — `let v = 5; let r = match o { Some(v) =>
    await f(v), … }; r + v` read the payload back as `v`. A nested match
    rebinding the name and an arrow in the arm body capturing it went wrong the
    same way, and every non-awaited spelling of those three was already correct
    because the lambda gave the bind a scope of its own.

    A comprehension is a scope and admits `await` (PEP 530), so the awaited
    binder is `[<body> for <bind> in (<payload>,)][0]`: the bind is arm-local
    exactly as the lambda's parameter is, an arrow in the body closes over the
    comprehension's cell, and every `await` still lands directly in the
    enclosing `async def`. The payload is evaluated once, before the body, in
    both forms.

    TWO ARM SHAPES NEED NO BINDER AT ALL, and those are the ones this emits
    without a frame (item 436 F3's remaining half). An arm whose body never
    mentions the bind (`Err(e) => -1`) drops the binder outright: the payload
    is `match` or `match.value`, a name and a `__slots__` read, so not
    evaluating it is not observable. An arm whose body IS the bind (`Some(v)
    => v`, `Ok(v) => v`, the unwrap that `match` mostly exists for) becomes the
    payload expression itself. Neither introduces a name, so neither can
    shadow, clobber or share a cell, which is what makes them safe where the
    walrus is not. A bind USED INSIDE a larger body keeps its lambda: rendering
    that body with the bind replaced by the payload needs the arm body rewritten
    before `_expr` sees it, and `selfhost/emit_py.rvl` reads the IR through a
    read-only value API with no way to build the rewritten node (item 429
    requires the two emitters to agree byte-for-byte).
    """
    # `match` is a revl keyword, so it can never be a user binding in the
    # revl source. Python 3.10+ treats it as a soft keyword, which is still
    # legal as a lambda parameter and as a walrus target.
    tmp = "match"

    def bind_payload(bind: str, body: str, payload: str, node: object) -> str:
        # The two binder-free arm shapes first (item 436 F3): a body that is
        # the bind becomes the payload, and a body that cannot read the bind
        # keeps neither the binder nor the payload evaluation.
        if _arm_body_is_bind(node, bind):
            return payload
        if not _arm_body_mentions(node, bind):
            return body
        # `await`-free arm -> the classic one-shot lambda; an awaited arm ->
        # a one-element comprehension, whose iteration variable is a scope the
        # `await` in the body can still be spelled inside.
        if awaited:
            if _walrus_temp(bind):
                # Python refuses a walrus that rebinds a comprehension's
                # iteration variable, and a body reaching one of the emitter's
                # own walrus temps (`_bi` and friends) does exactly that when
                # the payload is bound under that name. Such a bind is already
                # clobbered by the scaffolding that owns the name, with or
                # without a match, so keep the pre-item-163 spelling rather
                # than turn a wrong value into a `SyntaxError`.
                return f"(({bind} := {payload}), {body})[1]"
            return f"[{body} for {bind} in ({payload},)][0]"
        return f"(lambda {bind}: {body})({payload})"

    def branch(arm: dict, rest: str | None, head: str) -> str:
        """`head` reads the scrutinee in THIS arm's condition — for the first
        arm it carries the walrus that binds it, everywhere else it is `match`
        itself. A conditional expression evaluates its condition FIRST, so the
        bind is complete before any arm body (or any later arm's test) runs."""
        pattern = arm.get("pattern")
        node = arm.get("body")
        body = _expr(node)
        if pattern == "_":
            return body
        bind = arm.get("bind")
        # Opt is host-None/value (not a tagged class): Some/None discriminate
        # on None, and Some binds the scrutinee itself. Result/user ADTs are
        # tagged (isinstance), binding the payload `.value`.
        if pattern == "None":
            cond = f"{head} is None"
        elif pattern == "Some":
            cond = f"{head} is not None"
            if bind:
                body = bind_payload(bind, body, tmp, node)
        else:
            if bind:
                body = bind_payload(bind, body, f"{tmp}.value", node)
            cond = f"isinstance({head}, {pattern})"
        if rest is None:
            return f"({body} if {cond} else (_ for _ in ()).throw(TypeError('non-exhaustive match')))"
        return f"({body} if {cond} else {rest})"

    # The scrutinee bind rides the FIRST arm's test rather than a one-shot
    # `lambda match: …` (roadmap item 436 F3): the lambda was a function object
    # built and a frame entered at every evaluation, to bind one name. `match`
    # is a revl keyword, so no user binding can collide with the walrus target,
    # and a nested match inside an arm body may reuse the name freely — the
    # outer chain has finished reading `match` before any body is evaluated.
    # A leading wildcard arm (or no arm at all) has no test to carry the bind,
    # so those bind the SCRUTINEE with a `((match := …), <chain>)[1]` tuple
    # instead. That walrus is safe where a payload bind's is not: its target is
    # the revl keyword `match`, which no user binding can be named.
    folds = bool(arms) and arms[0].get("pattern") != "_"
    result = None
    for i, arm in reversed(list(enumerate(arms))):
        head = f"({tmp} := {scrutinee})" if (folds and i == 0) else tmp
        result = branch(arm, result, head)
    if result is None:
        result = "(_ for _ in ()).throw(TypeError('non-exhaustive match'))"
    if folds:
        return result
    return f"(({tmp} := {scrutinee}), {result})[1]"


def _expr(node: dict) -> str:
    global _PY_IN_ARROW
    if isinstance(node, dict) and node.get("kind") == "__rendered__":
        return node["text"]
    # An implicit Int -> Float coercion site (docs/arithmetic.md): the
    # frontend marks it, this tier renders the conversion. python would
    # otherwise absorb `3` silently, which is exactly the invisibility the
    # marker exists to remove.
    if isinstance(node, dict) and node.get("widen") == "Float":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"float({_expr(inner)})"
    kind = node["kind"]
    if kind == "lit":
        return repr(node["value"])
    if kind == "adt":
        # tagged ADT value: `Case(payload)` / `Case()`. The case class is
        # either a user variant (emitted by _emit_types) or the built-in
        # Result Ok/Err (emitted in the preamble).
        case = _ident(node["case"], "adt case")
        args = ", ".join(_expr(a) for a in node.get("args") or [])
        return f"{case}({args})"
    if kind == "var":
        name = node["name"]
        if name == "None":
            return "None"
        if name == "Some":
            return "(lambda _v: _v)"  # Opt is host-None/value: Some is identity
        # A bare var reference may be a host root (`Map`) or a user local; only
        # the latter can be a Python keyword, and `_mangle` leaves the roots
        # (non-keywords) untouched, so this stays the single consistent rename
        # site for a keyword-named local's *use* (item 165).
        return _mangle(name)
    if kind == "bin":
        if node["op"] == "??":
            left = node["left"]
            rhs = _expr(node["right"])
            binder, reader = _opt_bind(node, left, _expr(left))
            if binder == f"({reader})":
                # a bare local/literal: re-reading it is free and observable
                # only as the same value, so keep the plain spelling
                return f"({rhs} if {reader} is None else {reader})"
            # the walrus binds in the presence test, which a conditional
            # expression evaluates FIRST, so the `then` branch reads a value
            # the operand produced exactly once
            return f"({reader} if {binder} is not None else {rhs})"
        if node["op"] in ("+", "-", "*") and node.get("operands") == "Int":
            # Int is bounded 64-bit and overflow TRAPS (docs/arithmetic.md).
            # python is arbitrary precision, so it is the tier that has to
            # *impose* the bound rather than detect it — without this, a
            # program that overflows on every other tier quietly succeeds here,
            # which is the reference tier disagreeing with all five others.
            #
            # The bound is imposed INLINE (roadmap item 436 F5): the in-range
            # answer, which is every answer a correct program produces, no
            # longer costs a Python frame. `_revl_i64` stays as the trapping
            # tail, so the raise and its message are still written once.
            return _bounded(f"{_expr(node['left'])} {node['op']} "
                            f"{_expr(node['right'])}", 64)
        if node["op"] in ("+", "-", "*") and node.get("operands") == "Int32":
            # Int32 traps at the 32-bit edge, the same imposition at half the
            # width (docs/arithmetic.md).
            return _bounded(f"{_expr(node['left'])} {node['op']} "
                            f"{_expr(node['right'])}", 32)
        if node["op"] == "/":
            # true division, IEEE at zero (docs/arithmetic.md)
            return f"_revl_div({_expr(node['left'])}, {_expr(node['right'])})"
        if node["op"] == "%" and node.get("operands") in ("Int", "Float"):
            # `%` is the TRUNCATED remainder — it takes the sign of the
            # dividend, as in TypeScript (§0) — and pairs with `div_trunc` so
            # that (a.div_trunc(b)) * b + a % b == a. Python's `%` floors and
            # takes the sign of the *divisor*, so it is the one tier that has
            # to build this. The Euclidean remainder is `mod`, which is a
            # different operation with a different name (docs/arithmetic.md).
            # The same form serves Int and Float (it is `math.fmod` written
            # out), so an emitted module needs no import for it.
            # A preamble helper, not a lambda built and applied at every
            # evaluation (item 436 F6).
            return f"_revl_rem({_expr(node['left'])}, {_expr(node['right'])})"
        if node["op"] in ("&", "|", "^"):
            # Int32 bitwise AND/OR/XOR (item 366). These are bit patterns, not
            # arithmetic, so they never trap. python's ints are signed and, for
            # two operands already in i32 range, `&`/`|`/`^` produce a result
            # that is also in i32 range — the sign bit propagates consistently
            # in python's two's-complement view — so no re-wrap is needed.
            return (f"({_expr(node['left'])} {node['op']} "
                    f"{_expr(node['right'])})")
        if node["op"] == ">>":
            # Arithmetic (sign-extending) right shift; the count is taken mod
            # 32 (`& 31`), matching wasm/JS. python `>>` is arithmetic and the
            # magnitude only shrinks, so the result stays in i32 range.
            return (f"({_expr(node['left'])} >> "
                    f"({_expr(node['right'])} & 31))")
        if node["op"] == "<<":
            # Left shift wraps into 32-bit two's complement (a bit op, no
            # trap): the count is taken mod 32 and the result is re-wrapped.
            return (f"_revl_i32_wrap({_expr(node['left'])} << "
                    f"({_expr(node['right'])} & 31))")
        op = _PY_BIN_OPS.get(node["op"])
        if op is None:
            raise EmitError(f"unsupported binary operator {node['op']!r}")
        return f"({_expr(node['left'])} {op} {_expr(node['right'])})"
    if kind == "un":
        if node["op"] == "!":
            return f"(not {_expr(node['operand'])})"
        if node["op"] == "~":
            # Int32 bitwise complement (item 366): `~x == -x - 1`, which stays
            # in i32 range for any in-range `x`, so no re-wrap is needed.
            return f"(~{_expr(node['operand'])})"
        if node["op"] == "-":
            if node.get("operands") == "Int":
                # Negation is `0 - x`, and `0 - Int.MIN` overflows: it goes
                # through the bound like any other subtraction (docs/
                # arithmetic.md). Without this, `-Int.MIN` — which traps on
                # rust and wasm — quietly came back as 2^63 here, out of the
                # range python itself imposes on every other operation.
                return f"_revl_i64(-{_expr(node['operand'])})"
            if node.get("operands") == "Int32":
                return f"_revl_i32(-{_expr(node['operand'])})"
            return f"(-{_expr(node['operand'])})"
        raise EmitError(f"unsupported unary operator {node['op']!r}")
    if kind == "call":
        # `Some(x)` is the identity on this tier (roadmap item 436 F8): `Opt[T]`
        # is `T | None` at runtime, so the argument IS the answer. Rendering the
        # callee first would build `(lambda _v: _v)` and immediately apply it —
        # one function object and one frame to hand back what it was given.
        # `Some` is a builtin case name the frontend never lets a user rebind,
        # so the callee name settles this without a type environment. A BARE
        # `Some` (passed as a value, e.g. `xs.map(Some)`) still needs the
        # lambda, and the `var` arm below keeps emitting it.
        callee_node = node["callee"]
        if isinstance(callee_node, dict) and callee_node.get("kind") == "var" \
                and callee_node.get("name") == "Some" and len(node["args"]) == 1:
            return _expr(node["args"][0])
        call = f"{_expr(node['callee'])}({', '.join(_expr(a) for a in node['args'])})"
        # item 92: awaiting a colored fn or an async value local, in an async
        # body. item 115: an async extern is now an `async def` too, so it joins
        # the await-seed (a NON-async extern still erases to a blocking `def`
        # and is excluded — its result is not awaitable). The frontend admits
        # such a call only in an async context, so `_PY_IN_ASYNC` holds here.
        callee = node.get("callee")
        # item 141/264: suppressed inside an arrow body — a colored tail call
        # there stays a plain coroutine-returning lambda its awaiting call site
        # settles, never an `await` trapped in a (sync) lambda frame.
        if _PY_IN_ASYNC and not _PY_IN_ARROW and isinstance(callee, dict) \
                and callee.get("kind") == "var" \
                and (callee.get("name") in _PY_COLORED_FNS
                     or callee.get("name") in _PY_ASYNC_EXTERNS
                     or callee.get("name") in _PY_AWAIT_LOCALS):
            return f"(await {call})"
        return call
    if kind == "field":
        if node.get("sized_length"):
            # item 104 (cross-tier): property-form `.length` on a sized value —
            # the code-point/element count, not a record `getattr`. python's
            # `len` counts code points.
            return f"len({_expr(node['target'])})"
        # record literals are dicts; ADT payloads are objects — the read
        # dispatches on the shape INLINE (item 436 F4). An `Opt[T]`-declared
        # field reads TOTAL (item 380): absent -> None, the Opt's empty case.
        return _field_read(_expr(node["target"]), node["name"],
                           opt=bool(node.get("opt")))
    if kind == "index":
        return f"{_expr(node['target'])}[{_expr(node['index'])}]"
    if kind == "if":
        return f"({_expr(node['then'])} if {_expr(node['cond'])} else {_expr(node['else'])})"
    if kind == "record":
        return "{" + ", ".join(f"{k!r}: {_expr(v)}" for k, v in node["fields"]) + "}"
    if kind == "list":
        return "[" + ", ".join(_expr(e) for e in node["items"]) + "]"
    if kind == "len":
        return f"len({_expr(node['target'])})"
    if kind == "builtin":
        return _render_builtin(
            node.get("method"), _expr(node["target"]),
            [_expr(a) for a in node.get("args") or []],
            node.get("recv"))
    if kind == "maplit":
        # `Map.empty()` (docs/stdlib-2.0.md §Map)
        return "{}"
    if kind == "arrow":
        params = [_mangle(p) for p in node["params"]]
        captures = node.get("captures") or []
        lambda_params = ", ".join(
            params + [f"{_mangle(name)}={_mangle(name)}" for name in captures])
        prev_arrow = _PY_IN_ARROW
        _PY_IN_ARROW = True  # item 141/264: suppress the await-seed in the body
        try:
            if node.get("async"):
                # a pure-fn body has no req keys, so an async arrow here reaches
                # a coroutine only through a colored fn / async local (rules
                # 1-2). Thread the async-typed params (item 264) so a tail call
                # of one is recognised as a coroutine and renders as a plain
                # lambda, not a broken `_revl_as_async(lambda: (await …))`.
                return _py_async_arrow(node["body"], lambda_params, _expr,
                                       async_locals=frozenset(_PY_AWAIT_LOCALS))
            return f"lambda {lambda_params}: {_expr(node['body'])}"
        finally:
            _PY_IN_ARROW = prev_arrow
    if kind == "match":
        # item 263: inside an async-colored module fn an arm may reach a
        # coroutine (a colored fn / async extern / async local), rendering an
        # `await`; switch the binder to the walrus form so it is not trapped in
        # the scrutinee/payload lambdas.
        awaited = _PY_IN_ASYNC and any(
            _py_reaches_coroutine(arm.get("body"), async_locals=_PY_AWAIT_LOCALS)
            for arm in node["arms"])
        return _match_expr(_expr(node["scrutinee"]), node["arms"], awaited=awaited)
    if kind == "record_update":
        # functional record update (docs/records.md §2): fresh dict spreading
        # the base, updated fields overriding it
        base = _expr(node.get("base"))
        parts = [f"**{base}"] + [
            f"{name!r}: {_expr(value)}"
            for name, value in node.get("updates") or []
        ]
        return "{" + ", ".join(parts) + "}"
    if kind == "interp":
        return _interp_fstring(node["parts"])
    if kind == "optfield":
        binder, reader = _opt_bind(node, node["target"], _expr(node["target"]))
        return (f"(None if {binder} is None else "
                f"{_field_read(reader, node['name'], rereadable=True)})")
    if kind == "optcall":
        # `?.m(..)`: the method is a STDLIB builtin (the checker types an
        # optcall through `builtin_check`, so there is no host-method row to
        # reach), and it must be rendered by the same table a plain `.m(..)`
        # goes through. Emitting `payload.m(..)` instead put a method python
        # values do not have on the receiver (`'str' object has no attribute
        # 'length'`), which item 436 caught executing what the byte-agreement
        # oracle only ever emitted.
        binder, reader = _opt_bind(node, node["target"], _expr(node["target"]))
        args = [_expr(a) for a in node.get("args") or []]
        body = _render_builtin(node["method"], reader, args, _RECV_VIA_OPT)
        return f"(None if {binder} is None else {body})"
    raise EmitError(f"unsupported expression kind {kind!r}")


class _RenderedBody(dict):
    """An arm body already rendered by the component emitter; `_expr` returns
    it unchanged so `_match_expr` can be shared by both renderers.

    `source` is the IR node the text was rendered from, which is what
    `_match_expr`'s binder-free arm checks read (item 436 F3): without it a
    rendered body is opaque, and "does this body mention the bind?" could only
    be answered by keeping the binder.
    """

    def __init__(self, text: str, source: object = None) -> None:
        super().__init__(kind="__rendered__", text=text)
        self.source = source


def _let_pattern_stmt(node: dict, out: "_Lines", indent: int) -> None:
    """Emit a ``let_pattern`` step by evaluating the RHS once into a temp.

    The temp is named from a per-``_Lines`` monotonic counter incremented in
    deterministic emission order, so re-parsing the same IR yields identical
    bytes (item 179). The ``__revl_`` reserved prefix keeps it clear of user
    identifiers, and the counter keeps sibling destructures distinct.
    """
    tmp = f"__revl_destructure_{out.next_destructure_seq()}"
    out.add(indent, f"{tmp} = {_expr(node['value'])}")
    if node["pattern"] == "record":
        for name in node["names"]:
            # the binding is a fresh local, so mangle its keyword collisions.
            # The READ is `_field_read`, exactly as `row.name` is: a record
            # value is a dict (see `_emit_types`), so the plain `{tmp}.{name}`
            # this used to emit raised `AttributeError` on every record value
            # an emitted module actually produces. Nothing caught it because
            # the only destructure test constructed the record CLASS, which no
            # emitted module ever does. `rereadable`: `tmp` is already a name.
            out.add(indent, f"{_mangle(name)} = {_field_read(tmp, name, rereadable=True)}")
    elif node["pattern"] == "list":
        names = [_mangle(n) for n in node["names"]]
        rest = node.get("rest")
        if rest is None:
            if len(names) == 1:
                out.add(indent, f"{names[0]} = {tmp}[0]")
            else:
                out.add(indent, f"{', '.join(names)} = {tmp}")
        else:
            out.add(indent, f"{', '.join(names)}, *{_mangle(rest)} = {tmp}")
    else:
        raise EmitError(f"unsupported let_pattern kind {node['pattern']!r}")


# item 379 (docs/design/379-break-continue.md): the frame-neutrality invariant
# is enforced whole-IR in the frontend (`_validate_no_loop_scoped_registration`);
# this is the cheap per-emitter belt-and-suspenders — a teardown-registering step
# must never be a loop body's child on any tier.
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
# In-place accumulation (item 436 F1, on item 445's frontend marker)
# ---------------------------------------------------------------------------
# `List.push`, `Map.set`, `Map.remove` and a functional record update are
# PERSISTENT: each renders a whole copy of its receiver with one entry changed
# (`(xs + [v])`, `{**m, k: v}`, `{**p, 'x': v}`). In the ordinary accumulation
# loop, `var out = []; for (..) { out = out.push(..) }`, that is one full
# container copy per step, so a loop the developer wrote as O(n) is EMITTED as
# O(n^2). No opcode or profile counter can see it: `xs + [v]` is one bytecode
# instruction that copies `len(xs)` pointers in C, and the audit measured this
# exact loop at 0.96x in ops against 500x in elements copied at n=1000. It is
# not a synthetic shape either: `stdlib/list.rvl` writes `list_map`,
# `list_filter` and `list_dedup` as push loops, so `xs.map(f)` carries it.
#
# The rewrite is `out.append(v)`; all of its difficulty is proving the copy
# unobservable, and THAT PROOF NO LONGER LIVES HERE. It is an aliasing question
# about the source — is the object this binding names reachable through any
# other name — and item 436 answered it in this file while item 434 had already
# answered the same question, independently, in `backends/go/emit.py`. Item 445
# lifted the single answer into the frontend (`src/revl/ownership.py`), where it
# is also FLOW-SENSITIVE, so a name that escapes at one point and is reborn from
# a fresh literal before the next write is owned at that write — which is what
# takes `stdlib/list.rvl`'s `list_sort` off its emitted cubic.
#
# What arrives here are two markers on the IR, and they state a FACT rather than
# an instruction: this tier still decides what to do with them.
#
#   `assign` step, `"unique": True`
#       the binding owns its object outright at this write, so `out.append(v)`
#       is the faithful lowering of `out = out.push(v)`.
#
#   `assign` step, `"unique": "copy"`, with `"unique_birth": "List"|"Map"` on
#   the `let` that introduced the name
#       the local is born off ANOTHER name (`var out = m`), and is owned only
#       because this tier materialises a defensive copy at that birth: ONE copy,
#       where the persistent form made one per write. A parameter is the
#       CALLER's object, so the copy is what makes writing through the local
#       legitimate at all.
#
# Absent means "not proven", which is always the persistent form. `concat` is
# deliberately never marked: it is defined on both Str and List, the receiver
# type is not known at that node, and a python `str` cannot be mutated at all
# (the same split the go tier hit, item 434).

# The container each `unique_birth` shape materialises as.
_PY_BIRTH_CONTAINER = {"List": "list", "Map": "dict"}


def _py_inplace_write(node: dict) -> dict | None:
    """The `out = out.push(v)` value this tier may render destructively, else
    None. `"copy"` is honoured because `_fn_stmt` implements the birth copy the
    marker is conditional on."""
    if node.get("step") != "assign" or not node.get("unique"):
        return None
    value = node.get("value")
    if not isinstance(value, dict):
        return None
    if value.get("kind") == "record_update":
        return value
    return value if value.get("method") in _PY_INPLACE_METHODS else None


# The persistent methods with an in-place equivalent, and their arity, kept
# here because `_py_inplace_stmts` renders one statement per method.
_PY_INPLACE_METHODS = {"push": 1, "set": 2, "remove": 1}


def _py_inplace_stmts(name: str, value: dict) -> list[str]:
    """The in-place statements replacing a proven-unique persistent copy.
    Operand evaluation order matches the copying form they replace."""
    recv = _mangle(name)
    if value.get("kind") == "record_update":
        # `.update(<mapping>)` builds the whole replacement BEFORE writing any
        # of it, so a swap (`{ p | x = p.y, y = p.x }`) stays simultaneous
        # exactly as the `{**p, ..}` spread was
        pairs = ", ".join(f"{n!r}: {_expr(v)}" for n, v in value["updates"])
        return [f"{recv}.update({{{pairs}}})"]
    args = value.get("args") or []
    method = value["method"]
    if method == "push":
        return [f"{recv}.append({_expr(args[0])})"]
    if method == "remove":
        # `Map.remove` is TOTAL (an absent key is not an error), so `pop` with
        # a default, never `del`
        return [f"{recv}.pop({_expr(args[0])}, None)"]
    # `set`: the spread evaluated receiver, then key, then value; a subscript
    # assignment evaluates the VALUE first, so a key with an observable effect
    # would move. Bind it first when it is not a bare read.
    key_node, val_node = args
    key = _expr(key_node)
    if isinstance(key_node, dict) and key_node.get("kind") in _INLINE_TRIVIAL:
        return [f"{recv}[{key}] = {_expr(val_node)}"]
    return [f"_revl_key = {key}", f"{recv}[_revl_key] = {_expr(val_node)}"]


def _fn_stmt(node: dict, out: "_Lines", indent: int) -> None:
    step = node["step"]
    if step in ("let", "assign"):
        name = node["name"]
        write = _py_inplace_write(node)
        if write is not None:
            for line in _py_inplace_stmts(name, write):
                out.add(indent, line)
            return
        container = _PY_BIRTH_CONTAINER.get(node.get("unique_birth"))
        if step == "let" and container is not None:
            # born off another name: ONE copy here, where the persistent form
            # made one per write, and what makes writing through the local
            # legitimate when the source is the caller's object
            out.add(indent, f"{_mangle(name)} = {container}({_expr(node['value'])})")
            return
        out.add(indent, f"{_mangle(node['name'])} = {_expr(node['value'])}")
    elif step == "let_pattern":
        _let_pattern_stmt(node, out, indent)
    elif step == "return":
        if node["expr"] is None:
            out.add(indent, "return")
        else:
            out.add(indent, f"return {_expr(node['expr'])}")
    elif step == "if":
        out.add(indent, f"if {_expr(node['cond'])}:")
        for s in node["then"]:
            _fn_stmt(s, out, indent + 1)
        if node["else"]:
            out.add(indent, "else:")
            for s in node["else"]:
                _fn_stmt(s, out, indent + 1)
    elif step == "while":
        _guard_frame_neutral_loop(node["body"])
        out.add(indent, f"while {_expr(node['cond'])}:")
        if not node["body"]:
            out.add(indent + 1, "pass")
        else:
            for s in node["body"]:
                _fn_stmt(s, out, indent + 1)
    elif step == "for":
        _guard_frame_neutral_loop(node["body"])
        out.add(indent, f"for {_mangle(node['bind'])} in {_expr(node['iterable'])}:")
        if not node["body"]:
            out.add(indent + 1, "pass")
        else:
            for s in node["body"]:
                _fn_stmt(s, out, indent + 1)
    elif step == "break":
        out.add(indent, "break")
    elif step == "continue":
        out.add(indent, "continue")
    elif step == "expr":
        out.add(indent, _expr(node["expr"]))
    elif step == "assert":
        _emit_assert(node["expr"], out, indent)
    else:
        raise EmitError(f"unsupported fn statement step {step!r}")


_ASSERT_COMPARISONS = ("==", "===", "!=", "!==", "<", ">", "<=", ">=")


def _emit_assert(expr: dict, out: "_Lines", indent: int) -> None:
    """`assert a == b`, with both operand values in the failure message.

    A bare `assert` carries no message, so the runner has nothing to print but
    "assertion failed" (src/revl/test.py) — the least useful thing a test
    framework can say, and it costs whoever wrote the test a debugging session
    to recover what the emitter already knew. An in-file `test` block almost
    always asserts a comparison, so that case binds each side to a temporary
    (evaluated exactly once, which a naive re-render would not guarantee) and
    reports both values alongside the expression as emitted. Anything else
    keeps the plain form.
    """
    if expr.get("kind") == "bin" and expr.get("op") in _ASSERT_COMPARISONS:
        op = _PY_BIN_OPS[expr["op"]]
        left = _expr(expr["left"])
        right = _expr(expr["right"])
        out.add(indent, f"_revl_lhs = {left}")
        out.add(indent, f"_revl_rhs = {right}")
        # the shown text is a repr, never an f-string, so quotes and braces in
        # the rendered source cannot break out of the literal
        shown = repr(f"{left} {op} {right}")
        out.add(indent, f"assert _revl_lhs {op} _revl_rhs, {shown}"
                        " + '\\n  left  = ' + repr(_revl_lhs)"
                        " + '\\n  right = ' + repr(_revl_rhs)")
        return
    out.add(indent, f"assert {_expr(expr)}")


def _is_cache_pure(decl: dict) -> bool:
    """item 310: does this declaration carry `cache pure`? `pure_fn` is the only
    class that reaches a plain `fn` (the checker refuses the other two there),
    and it is the only one with no ledger interaction, so it is the only one a
    backend may honour on its own."""
    return ((decl.get("cache") or {}).get("class") == "pure_fn")


def _uses_cache_pure(ir: dict) -> bool:
    return any(_is_cache_pure(fn) for fn in ir.get("functions") or [])


def _emit_memo_wrapper(name: str, params: list, is_async: bool) -> "_Lines":
    """The public `cache pure` entry point: a structural-key memo in front of
    the real body, which is emitted under `_revl_uncached_<name>`.

    The public NAME keeps the wrapper, so a call site and a first-class value
    reference reach the memo identically — there is no spelling that gets the
    un-memoized body by accident. The key is namespaced by the fn name, so one
    table serves the module without two fns ever colliding on equal arguments."""
    inner = f"_revl_uncached_{name}"
    args = ", ".join(_ident(p["name"], "parameter name") for p in params)
    call = f"{'await ' if is_async else ''}{inner}({args})"
    key_parts = ", ".join([repr(name)] + [_ident(p["name"], "parameter name")
                                          for p in params])
    out = _Lines()
    out.add(0, f"{'async def' if is_async else 'def'} {name}({args}):")
    out.add(1, '"""item 310 `cache pure`: memoized on the structural args key."""')
    out.add(1, f"_revl_k = _revl_memo_key(({key_parts},))")
    out.add(1, "if _revl_k is _REVL_NOMEMO:")
    out.add(2, f"return {call}")
    out.add(1, "_revl_v = _REVL_MEMO.get(_revl_k, _REVL_MISS)")
    out.add(1, "if _revl_v is _REVL_MISS:")
    out.add(2, f"_revl_v = {call}")
    out.add(2, "_REVL_MEMO[_revl_k] = _revl_v")
    out.add(1, "return _revl_v")
    out.add(0)
    return out


def _emit_functions(functions: list) -> "_Lines":
    global _PY_IN_ASYNC, _PY_AWAIT_LOCALS, _PY_IN_ARROW
    out = _Lines()
    for fn in functions:
        name = _ident(fn["name"], "function name")
        params = ", ".join(_ident(p["name"], "parameter name") for p in fn["params"])
        # item 92: a phase-2 async-colored fn emits `async def`; its body renders
        # in an async context with its async-typed parameters as the await-locals
        # (a call through one yields a coroutine to settle). item 264: an
        # async-typed param is an async local of a SYNC fn too — the enclosing
        # `def` never awaits it (that is gated on `_PY_IN_ASYNC`), but an arrow
        # re-passing it must still render it as a coroutine tail call.
        is_async = bool(fn.get("async"))
        _PY_IN_ASYNC = is_async
        _PY_IN_ARROW = False
        _PY_AWAIT_LOCALS = {p["name"] for p in fn["params"]
                            if _is_async_fn_type(p.get("type"))}
        # item 310: a `cache pure` fn renders its real body under a private name
        # and gains a memo wrapper under the public one (below), so every caller
        # — including a first-class value reference — goes through the table.
        memoized = _is_cache_pure(fn)
        if memoized:
            name = f"_revl_uncached_{name}"
        out.add(0, f"{'async def' if is_async else 'def'} {name}({params}):")
        if not fn.get("body"):
            out.add(1, "pass")
        for stmt in fn.get("body") or []:
            _fn_stmt(stmt, out, 1)
        out.add(0)
        if memoized:
            out.extend(_emit_memo_wrapper(
                _ident(fn["name"], "function name"), fn["params"], is_async))
    _PY_IN_ASYNC = False
    _PY_IN_ARROW = False
    _PY_AWAIT_LOCALS = set()
    return out


def _ref_dotted(rel_path: str) -> str:
    """`src/host/engine.py` -> `src.host.engine`, the py import specifier derived
    from the IR ref's root-relative path. Self-contained (the backend has no
    `revl` import); mirrors `revl.hostref.dotted_module`."""
    stem = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    return stem.replace("/", ".")


def _emit_py_ref_thunk(name: str, params: str, ext: dict, ref: dict) -> "_Lines":
    """item 396 option B: the lazy import thunk for a `@py ref` extern.

    Sync and async share one shape (host execution at first call, symbol cached
    in `_REVL_REFS`, the resolved symbol carrying the declared colour). The
    colour claim is unverifiable at compile (G8), so it is asserted HOST-NATIVELY
    at first call — the earliest gate this design owns:

    - A declared-SYNC ref whose symbol is a plain `async def` catches on
      `inspect.iscoroutinefunction`, and a value-level `inspect.isawaitable`
      guard on the result closes the wrapper/callable-instance shapes the
      function-object check cannot see (a plain `def` returning a coroutine).
      This makes a ref STRICTER than an inline body for an extern that
      intentionally returns an awaitable handle — a stated, deliberate trade.
      Neither check is a proof: `iscoroutinefunction` sees only the plain
      `async def` shape (design re-review #3).
    - A declared-ASYNC ref is NOT hard-refused when the symbol is not an
      `iscoroutinefunction` (that would falsely refuse an lru_cache-wrapped
      async fn or a callable instance whose `__call__` is async). `await _f(...)`
      is loud-wrong at worst if the symbol returns a non-awaitable — never
      silent — so the async direction stays awaited-by-name.
    """
    dotted = _ref_dotted(ref["path"])
    symbol = _ident(ref["symbol"], "ref symbol")
    where = f"{dotted}.{symbol}"
    out = _Lines()
    is_async = bool(ext.get("async"))
    out.add(0, f"{'async def' if is_async else 'def'} {name}({params}):")
    out.add(1, f"_f = _REVL_REFS.get({name!r})")
    out.add(1, "if _f is None:")
    out.add(2, f"from {dotted} import {symbol} as _f")
    if not is_async:
        out.add(2, "if _inspect.iscoroutinefunction(_f):")
        out.add(3, "raise TypeError(")
        out.add(4, f"\"revl extern `{name}` is declared sync but "
                   f"{where} is a coroutine \"")
        out.add(4, "\"function; declare the extern `async` or ref a sync symbol\")")
    out.add(2, f"_REVL_REFS[{name!r}] = _f")
    call_args = params
    if is_async:
        out.add(1, f"return await _f({call_args})")
    else:
        out.add(1, f"_r = _f({call_args})")
        out.add(1, "if _inspect.isawaitable(_r):")
        out.add(2, "raise TypeError(")
        out.add(3, f"\"revl extern `{name}` is declared sync but "
                   f"{where} returned \"")
        out.add(3, "\"an awaitable; declare the extern `async` or ref a sync "
                   "symbol\")")
        out.add(1, "return _r")
    out.add(0)
    return out


def _emit_externs(externs: list) -> "_Lines":
    out = _Lines()
    # item 256 Slice 1: the composition secrets map, keyed by secret name, and a
    # FAIL-LOUD lookup. The driver (src/revl/run.py) resolves each bound secret's
    # value once at plug and installs it here; a body reads the key as its FIRST
    # local (`openai_key = _revl_secret("openai_key")`, injected below). Emitted
    # ONLY when some emission extern carries a bound secret, so a secret-free
    # program is byte-identical. The value is NEVER logged, defaulted, or echoed:
    # the helper names the secret but never its value, and there is no defaults
    # path (unlike config) - a bound key with no installed value is a hard error
    # at the extern call, never a silent None (§3).
    if any(ext.get("secrets") for ext in externs):
        out.add(0, "_REVL_SECRETS = {}")
        out.add(0)
        out.add(0, "def _revl_secret(_name):")
        out.add(1, "if _name not in _REVL_SECRETS:")
        out.add(2, "raise RuntimeError(")
        out.add(3, "\"capability-bound secret `\" + _name + \"` was not \"")
        out.add(3, "\"installed before its extern body ran; the run driver \"")
        out.add(3, "\"must resolve it at plug (item 256). No default exists \"")
        out.add(3, "\"for a secret.\")")
        out.add(1, "return _REVL_SECRETS[_name]")
        out.add(0)
    # item 379 / option (b) of docs/design/378-sync-extern-service-reach.md: the
    # composition config map for document-global config externs, keyed by extern
    # name. The driver (src/revl/run.py) resolves each config extern's schema
    # once at plug time and installs the resolved dict here, exactly as it
    # supplies a component's config at plug. Emitted ONLY when some extern
    # carries a `config` schema, so a program with no config extern is
    # byte-identical.
    if any(ext.get("config") for ext in externs):
        out.add(0, "_REVL_EXTERN_CONFIG = {}")
        out.add(0)
        # item 395 (Fable review): FAIL-LOUD config lookup. The old
        # `_REVL_EXTERN_CONFIG.get(name) or {}` fallback handed a config extern an
        # empty dict whenever plug-time configuration was never installed — a
        # module imported OUTSIDE the run.py driver silently got `{}`, so a body
        # reading `_revl_config["provider"]` failed LATE with a bare KeyError that
        # never named the extern or the real cause. This helper RAISES at the
        # extern call, naming the extern, when a REQUIRED (non-defaulted) field is
        # absent. A defaults-only extern (empty `required`) still resolves to its
        # defaults when unconfigured, so it keeps working driver-free.
        out.add(0, "def _revl_extern_config(_name, _required, _defaults):")
        out.add(1, "_cfg = _REVL_EXTERN_CONFIG.get(_name)")
        out.add(1, "if _cfg is None:")
        out.add(2, "if _required:")
        out.add(3, "raise RuntimeError(")
        out.add(4, "\"config extern `\" + _name + \"` called before plug-time \"")
        out.add(4, "\"configuration was installed (required config: \" +")
        out.add(4, "\", \".join(_required) + \"); configure it through the run \"")
        out.add(4, "\"driver's config seam\")")
        out.add(2, "return dict(_defaults)")
        out.add(1, "_missing = [_f for _f in _required if _f not in _cfg]")
        out.add(1, "if _missing:")
        out.add(2, "raise RuntimeError(")
        out.add(3, "\"config extern `\" + _name + \"` called before plug-time \"")
        out.add(3, "\"configuration was installed (missing required config: \" +")
        out.add(3, "\", \".join(_missing) + \")\")")
        out.add(1, "return {**_defaults, **_cfg}")
        out.add(0)
    for ext in externs:
        name = _ident(ext["name"], "extern name")
        params = ", ".join(_ident(p["name"], "extern parameter name") for p in ext["params"])
        bodies = ext.get("bodies") or {}
        refs = ext.get("refs") or {}
        # item 421 F6: the extern's declared return carried `Secret[T]`, so its
        # result is where a confidential value ENTERS the value world (item 256
        # §7a). A decorator marks it there, the narrowest place: one wrapper per
        # declaration rather than one per call site, so a sink further down with
        # no positional marking of its own (the host trace `_record` prints to
        # the operator console) can scrub it. It wraps whichever `def` follows,
        # the inline body and the `@py ref` thunk alike, and never touches the
        # verbatim body. Emitted ONLY for a `Secret[T]`-returning extern, so every
        # other module is byte-identical.
        if ext.get("secret_return"):
            out.add(0, f"@{_runtime_ref('secret_result')}")
        # item 396 option B: a `@py ref sym from "module.py"` extern emits a LAZY
        # import THUNK — never a body — that imports the host symbol at the
        # extern's FIRST CALL, inside the extern frame, and caches it. A module-
        # top import would run the referenced module at artifact LOAD, outside
        # every classification/approval/witness gate; the thunk keeps host
        # execution where an inline body's execution is (design §"Emit, py").
        if "py" in refs and "py" not in bodies:
            out.extend(_emit_py_ref_thunk(name, params, ext, refs["py"]))
            continue
        if "py" not in bodies:
            raise EmitError(
                f"extern `{name}` has no @py body — not portable to this backend "
                f"(available: {', '.join(sorted(set(bodies) | set(refs))) or 'none'})"
            )
        # item 115 (async-extern.md §8): an async extern emits an `async def`
        # so its verbatim @py body may `await` a host operation; every admitted
        # call site awaits it (see the await-seed and `_py_yields_coroutine`).
        # A non-async extern stays a blocking `def`, unchanged.
        kw = "async def" if ext.get("async") else "def"
        out.add(0, f"{kw} {name}({params}):")
        # item 256 Slice 1: a bound secret is injected as the FIRST body local of
        # every emission extern whose declared capability it is bound to, and
        # NOWHERE else (a component/service method body has no `_revl_secret` call
        # emitted into it, so the name resolves nowhere outside a bound body - the
        # "nowhere else" property, enforced by construction here in the extern
        # emitter loop, §3). The verbatim @py body then reads the key as a
        # host-scope local and hands it straight to its provider call. Emitted only
        # for a bound extern, so a non-bound extern's `def`/body is byte-identical.
        for _sname in ext.get("secrets") or []:
            out.add(1, f"{_ident(_sname, 'secret name')} = _revl_secret({_sname!r})")
        # item 379: a config extern binds `_revl_config` in its body scope as the
        # first local, mirroring how a component method binds it (emit.py:1425,
        # read at emit.py:734). The verbatim @py body then reads typed config
        # (`_revl_config["provider"]`) instead of `os.environ`. The dict is the
        # composition value the driver resolved once at plug.
        #
        # item 395 (Fable review): resolve through the FAIL-LOUD helper, passing
        # the REQUIRED (non-defaulted) field names and the resolved defaults from
        # the schema. When plug-time configuration was never installed and a
        # required field is absent, the helper RAISES at the extern call naming
        # the extern — no more silent `{}`. A defaults-only extern still resolves
        # to its defaults driver-free. Emitted only for a config extern, so a
        # no-config extern's `def`/body is byte-identical.
        cfg_schema = ext.get("config")
        if cfg_schema:
            required = [f["name"] for f in cfg_schema if f.get("default") is None]
            defaults = {f["name"]: f["default"] for f in cfg_schema
                        if f.get("default") is not None}
            out.add(1, f"_revl_config = _revl_extern_config("
                       f"{name!r}, {required!r}, {defaults!r})")
        body = textwrap.dedent(bodies["py"].strip("\n"))
        if body:
            for line in body.splitlines() or [""]:
                out.add(1, line)
        elif not ext.get("config") and not ext.get("secrets"):
            out.add(1, "pass")
        out.add(0)
    return out


def _emit_tests(tests: list) -> "_Lines":
    out = _Lines()
    out.add(0, "REVL_TESTS = []")
    out.add(0)
    if any(test.get("lifecycle") for test in tests):
        out.extend(_emit_lifecycle_harness())
    for index, test in enumerate(tests):
        fn_name = f"test_{index}"
        if test.get("lifecycle"):
            out.extend(_emit_lifecycle_test(test, fn_name))
            out.add(0, f"REVL_TESTS.append(({test['name']!r}, {fn_name}))")
            out.add(0)
            continue
        out.add(0, f"def {fn_name}():")
        if not test.get("body"):
            out.add(1, "pass")
        for stmt in test.get("body") or []:
            _fn_stmt(stmt, out, 1)
        out.add(0)
        out.add(0, f"REVL_TESTS.append(({test['name']!r}, {fn_name}))")
        out.add(0)
    return out


def _emit_fault_tests(fault_tests: list, names: list) -> "_Lines":
    """Emit the fault-test manifest (docs/fault-tests.md).

    A fault test is not code in the module: it is an *experiment on* the
    module, driven by the harness in `src/revl/fault.py`, which re-emits the
    component with a `fail` step spliced in at the injection point.  What the
    module carries is therefore the declaration itself, so an emitted module
    is self-describing and the harness never has to be handed the IR
    separately.  This is the py tier's answer to "lower it or refuse it";
    the other four tiers refuse (they have no fault-test driver).
    """
    out = _Lines()
    out.add(0, "REVL_FAULT_TESTS = [")
    for unit in fault_tests:
        component = unit.get("component")
        if component not in names:
            raise EmitError(
                f"fault test {unit.get('name')!r} names unknown component {component!r}")
        at = unit.get("at") or {}
        entry = {
            "name": unit.get("name"),
            "component": component,
            "step": at.get("step"),
            "effect": at.get("effect"),
            "assert": list(unit.get("assert") or []),
            "config": dict(unit.get("config") or {}),
        }
        out.add(1, f"{entry!r},")
    out.add(0, "]")
    return out


# ---------------------------------------------------------------------------
# v2.0 §7.1: lifecycle tests
#
# A lifecycle test is a script over a *live* composition, so it compiles to an
# async driver run with `asyncio.run` and registered in REVL_TESTS like any
# other test (the `revl test` runner is unchanged).
#
# `assert no_residue` is R4 from docs/backend-ir.md §Required semantics, taken
# from the reference suite that defines it for this tier
# (backends/python/tests/test_semantics.py::
#  test_r4_no_residue_after_unloading_everything): the composition leaves the
# host runtime with no bindings, listeners, or effects. It has a second half,
# because the first half alone cannot fail for any component the compiler
# accepts — the emitted module hand-rolls no teardown at all (R5), so the
# runtime's own introspection always returns to baseline. The falsifiable part
# of residue-freedom is R1's: every host resource acquired during the test
# must have been released by its `undo`. Both halves are asserted.
# ---------------------------------------------------------------------------

# The reference tier's host-builtin vocabulary (docs/backend-ir.md §Host
# builtins; runtime.Pool / runtime.Map): acquisition verb -> release verb.
# A timer (item 57) joins the same table: `schedule` is acquired at activation,
# `cancel` releases it on teardown, so an uncancelled timer surfaces as residue
# through the exact machinery that catches a pool left open (docs/time-coeffect.md).
_LIFECYCLE_ACQUIRE = {"new": "drop", "open": "close", "schedule": "cancel"}


_LIFECYCLE_HARNESS = '''
def _revl_residue(root):
    """R4 (docs/backend-ir.md): what the host runtime holds for a composition."""
    return {
        "listeners": {n: len(cbs) for n, cbs in root.events._hooks.items() if cbs},
        "effects": root.fiber._disposables.length,
        "provisions": sorted(impl.name for impl in root.reflect.store.values()),
        "runtimes": root.registry.size,
    }


def _revl_unreleased(events):
    """Host resources acquired during the test and never released (R1)."""
    live = {}
    for event in events:
        tag, _, verb = event.split(" ", 1)[0].rpartition(".")
        if not tag:
            continue
        if verb in _REVL_ACQUIRE:
            live[tag] = verb
        elif verb in _REVL_ACQUIRE.values():
            live.pop(tag, None)
    return sorted(
        "{} ({}() with no {}())".format(tag, verb, _REVL_ACQUIRE[verb])
        for tag, verb in live.items()
    )


def _revl_no_residue(root, baseline, events, where):
    """`assert no_residue` — R4 plus the R1 property that makes it falsifiable."""
    now = _revl_residue(root)
    changed = ["{}: {!r} -> {!r}".format(k, baseline[k], now[k])
               for k in now if now[k] != baseline[k]]
    unreleased = _revl_unreleased(events)
    if not changed and not unreleased:
        return
    detail = []
    if changed:
        detail.append("host runtime still holds " + "; ".join(changed) + " (R4)")
    if unreleased:
        detail.append("host resources never released: " + ", ".join(unreleased) + " (R1)")
    raise AssertionError(where + ": residue \u2014 " + " | ".join(detail))


async def _revl_settle():
    for _ in range(20):
        await _revl_asyncio.sleep(0)


async def _revl_call(root, key, method, args, where):
    impl = root.get(key)
    if impl is None:
        raise AssertionError(
            "{}: no provider for key {!r} \u2014 its component is loaded but not ACTIVE; "
            "a component with an unmet `requires` stays PENDING (R2)".format(where, key))
    # delivery semantics (item 44): the driver may auto-retry a transient
    # failure iff the checked property says this emission is idempotent. A
    # non-idempotent emission gets exactly one delivery attempt.
    idempotent = method in _REVL_IDEMPOTENT.get(key, ())
    return await _revl_retry_idempotent(
        lambda: getattr(impl, method)(*args),
        idempotent=idempotent, where=where)
'''


def _emit_lifecycle_harness() -> "_Lines":
    out = _Lines()
    out.add(0, f"_REVL_ACQUIRE = {_LIFECYCLE_ACQUIRE!r}")
    out.add(0)
    for line in _LIFECYCLE_HARNESS.strip("\n").split("\n"):
        out.add(0, line)
    out.add(0)
    out.add(0)
    return out




def _emit_lifecycle_test(test: dict, fn_name: str) -> "_Lines":
    name = test["name"]
    where = f"lifecycle test {name!r}"
    out = _Lines()
    out.add(0, f"def {fn_name}():")
    out.add(1, "# cordis-py is imported here, not at module scope: a document may")
    out.add(1, "# mix pure `test` blocks with lifecycle ones, and only the latter")
    out.add(1, "# need a runtime.")
    out.add(1, "from cordis import Context")
    out.add(0)
    uses_abort = _lifecycle_uses_abort(test)
    out.add(1, "async def _run():")
    out.add(2, "root = Context()")
    if _lifecycle_uses_clock(test):
        # item 102: the clock coeffect is a module-global; reset it so this
        # test's `advance` steps start from t=0 and see only its own timers,
        # independent of any earlier lifecycle test in the file.
        out.add(2, "_revl_Clock.reset()")
    out.add(2, "events = []")
    out.add(2, "_revl_fibers = {}")
    if uses_abort:
        # item 377: register a 245 session-commit owner BEFORE any component
        # loads, so every activation frame joins its live-frame registry and an
        # `abort` step can mark them aborting (the exact seam `Session.abort`
        # drives, docs/design/245-session-commit.md). Cleared in `finally`, so a
        # later lifecycle test in the same file gets the pre-245 world back.
        out.add(2, "_revl_owner = _revl_SessionOwner()")
        out.add(2, "_revl_set_session_owner(_revl_owner)")
    out.add(2, "_revl_set_trace(events.append)")
    out.add(2, "try:")
    out.add(3, "baseline = _revl_residue(root)")
    body = test.get("body") or []
    if not body:
        out.add(3, "pass")
    for step in body:
        _lifecycle_step(out, 3, step, where)
    out.add(2, "finally:")
    out.add(3, "_revl_set_trace(None)")
    if uses_abort:
        out.add(3, "_revl_clear_session_owner()")
    out.add(3, "for fiber in reversed(list(_revl_fibers.values())):")
    out.add(4, "try:")
    out.add(5, "await fiber.dispose()")
    out.add(4, "except Exception:")
    out.add(5, "pass")
    out.add(0)
    out.add(1, "_revl_asyncio.run(_run())")
    out.add(0)
    return out


def _lifecycle_step(out: "_Lines", indent: int, step: dict, where: str) -> None:
    kind = step.get("step")
    if kind == "load":
        component = _ident(step["component"], "component name")
        config = ", ".join(f"{name!r}: {_expr(value)}"
                           for name, value in (step.get("config") or {}).items())
        out.add(indent, f"_revl_fibers[{component!r}] = _revl_plug(root, {component}, {{{config}}})")
        out.add(indent, "await _revl_settle()")
    elif kind == "unload":
        component = _ident(step["component"], "component name")
        out.add(indent, f"await _revl_fibers.pop({component!r}).dispose()")
        out.add(indent, "await _revl_settle()")
    elif kind == "call":
        args = ", ".join(_expr(arg) for arg in step.get("args") or [])
        call = (f"await _revl_call(root, {step['key']!r}, {step['method']!r}, "
                f"[{args}], {where!r})")
        bind = step.get("bind")
        out.add(indent, f"{_ident(bind, 'lifecycle binding')} = {call}" if bind else call)
        out.add(indent, "await _revl_settle()")
    elif kind == "advance":
        # item 102: drive the clock coeffect forward. A firing is a
        # deterministic timeline step, so `_revl_settle` after it lets the
        # fired body's async work (if any) run to quiescence before the next
        # statement observes it (docs/time-coeffect.md §advance).
        out.add(indent, f"_revl_Clock.advance({int(step['ms'])})")
        out.add(indent, "await _revl_settle()")
    elif kind == "abort":
        # item 377 (F-H1.7): drive the enclosing session frame's 245 abort,
        # mirroring `revl.mcp.session.Session.abort` exactly — mark every live
        # frame aborting (so its teardown reverts rather than commits), replay
        # the witnessed inverses by disposing LIFO, then finalize. The witnessed
        # mutations revert and the deferral queue is dropped, so the workspace is
        # left byte-identical to before (docs/design/245-session-commit.md).
        out.add(indent, "_revl_owner.begin_abort()   # mark frames abort, drop queue")
        out.add(indent, "for _fiber in reversed(list(_revl_fibers.values())):")
        out.add(indent + 1, "try:")
        out.add(indent + 2, "await _fiber.dispose()   # replay inverses")
        out.add(indent + 1, "except Exception:")
        out.add(indent + 2, "pass")
        out.add(indent, "_revl_fibers.clear()")
        out.add(indent, "_revl_owner.finalize_abort()")
        # the aborted session is over; a fresh owner means any component loaded
        # after the abort is a clean new session (Session.abort resets), never
        # inheriting the aborted verdict.
        out.add(indent, "_revl_owner = _revl_SessionOwner()")
        out.add(indent, "_revl_set_session_owner(_revl_owner)")
        out.add(indent, "await _revl_settle()")
    elif kind == "assert_no_residue":
        out.add(indent, f"_revl_no_residue(root, baseline, events, {where!r})")
    elif kind == "assert":
        rendered = _expr(step["expr"])
        out.add(indent, f"assert {rendered}, {where + ': assertion failed'!r}")
    else:  # pragma: no cover — the lowerer emits nothing else
        raise EmitError(f"{where}: unknown lifecycle step {kind!r}")


def _lifecycle_uses_abort(test: dict) -> bool:
    """True iff a lifecycle test drives a session abort (an `abort` step, item
    377). Only such a test registers the 245 session-commit owner and imports
    its symbols — an abort-free lifecycle test stays byte-identical to before."""
    return any(step.get("step") == "abort" for step in test.get("body") or [])


def _lifecycle_uses_clock(test: dict) -> bool:
    """True iff a lifecycle test drives the clock coeffect (an `advance` step).

    Only such a test needs the `Clock` import and the per-test reset — a
    lifecycle test with no `advance` stays byte-identical to its pre-item-102
    output."""
    return any(step.get("step") == "advance" for step in test.get("body") or [])


def _find_host_roots(nodes) -> set[str]:
    """Host builtins referenced by pure fn/test bodies.

    Component emitters track their own imports through `_ComponentEmitter._expr`,
    but `_expr` for v3 functions/tests has no collector. Walk the lowered nodes
    for `var` references that name a host root so tests such as `Map.new()` work
    in a tests-only module.
    """
    found: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("kind") == "var" and node.get("name") in _HOST_ROOTS:
                found.add(node["name"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(nodes)
    return found



def _uses_builtin_result(ir: dict) -> bool:
    """True if the IR constructs or matches the built-in Result (Ok/Err) —
    an `adt` node typed Result, a `match` arm on Ok/Err, or a call to one of
    the total division forms (which *produce* a Result). Used to decide
    whether to emit the built-in Result classes: emitting them into every
    module would add classes no program references, so the gate is dead-code
    hygiene — the emitted module carries only the names the program uses."""
    def walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("kind") == "adt" and str(node.get("type", "")).startswith("Result"):
                return True
            if node.get("kind") == "match":
                if any(arm.get("pattern") in ("Ok", "Err") for arm in node.get("arms") or []):
                    return True
            if node.get("method") in _CHECKED_DIVS:
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False

    # item 243: a witnessed extern returns `Result[Witness, Error]` and its
    # emitted call site branches on `Ok` to register the transactional inverse,
    # so the Result classes must be present even if no surface `match`/`adt`
    # names them. Any witnessed extern is enough to require them.
    if any(ext.get("class") == "witnessed" for ext in ir.get("externs") or []):
        return True
    # item 362: a pure/total extern that RETURNS a Result builds `Ok(..)`/
    # `Err(..)` in its host body (e.g. `json_try_parse`), so the classes must
    # be present even when no surface `match`/`adt` names them — the same
    # reasoning as the witnessed case, extended to any Result-returning extern.
    if any(str(ext.get("returns", "")).startswith("Result")
           for ext in ir.get("externs") or []):
        return True
    return walk(ir.get("components")) or walk(ir.get("functions")) or walk(ir.get("tests"))


# ------------------------------------------------------------ typed holes

def _refuse_holes(ir: dict) -> None:
    """A typed hole is an unmet obligation, not code (docs/holes.md).

    Emitting one would put a placeholder into Python and make CPython the
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
        f"refusing to emit Python: this document still has {len(found)} typed "
        f"hole(s) — {where}. A hole type-checks so the surrounding draft can "
        f"be checked, but it has no implementation and there is nothing to "
        f"lower. Fill every hole, then emit (docs/holes.md)."
    )


# ---------------------------------------------------------------------------
# py-tier inlining of small pure functions (roadmap item 231a)
#
# CPython pays a real per-call frame cost, and revl's pure functional style
# leans on tiny helper fns on the hot path. The self-host LEXER's per-byte
# scan calls `code0`/`is_alpha`/`is_digit`/`is_space` once per source byte
# (item 229 found the lexer the heaviest stage on CPython for exactly this
# reason). The native tiers inline these for free; this pass gives the py tier
# the same win by folding a small pure helper's body into its call sites, so a
# hot loop pays an inline comparison instead of a function call.
#
# It is an OPTIMIZER, so it is deliberately conservative: it inlines only where
# the substitution is provably behavior-preserving and skips everything else:
#
#   * only a v3 pure `fn` (no effects, sync) with 0 or 1 parameters, whose body
#     is the "guarded return" shape (zero or more `if (c) { return X }` guards
#     followed by one terminal `return Y`), which folds into one conditional
#     expression `(X if c else Y)`. Anything with a loop, a `let`, an `else`, a
#     multi-statement guard, or more than one parameter is left alone. Capping
#     at one parameter removes every argument-evaluation-ORDER concern.
#   * calls are rewritten only inside pure `fn` and `test` bodies (the module
#     `_expr` domain, where arguments are themselves pure); component/method
#     orchestration (where an argument could carry an effect) is never touched
#     (a module-fn call there is a `fn` node, not the `call` node this rewrites).
#   * the fully-expanded template must reference nothing but its parameter (no
#     free global name, no residual call, no host root), so there is no
#     name-capture hazard when the body lands in the caller's scope.
#   * per call site, the argument is substituted only when doing so cannot
#     change evaluation: used exactly once in an eagerly-evaluated position (any
#     argument), used more than once (the argument must be effect-free so
#     re-evaluating it is invisible), or used zero/only-conditionally (the
#     argument must be a bare name/literal that neither raises nor has effects).
#
# A helper that fails any check keeps its call: a correct un-inlined call is
# always a better outcome than a risky inline.
# ---------------------------------------------------------------------------

# Template size cap: the point is the small hot helpers, not folding a big
# list-returning fn (`keywords()`) into every call site.
_INLINE_MAX_NODES = 24
# Expression kinds whose evaluation may carry an effect (or resolve a live host
# value): an argument containing one must never be duplicated or elided.
_INLINE_IMPURE = frozenset({"call", "optcall", "req", "spawn", "instance-get",
                            "host"})
# Kinds that neither raise nor have an effect, safe to duplicate OR drop: a
# bare read of a local/literal/empty-map.
_INLINE_TRIVIAL = frozenset({"var", "name", "lit", "config", "maplit"})


def _inline_bound_names(node: Any, out: set) -> set:
    """Every name a body BINDS: a `let`/`var`, a `for` bind, a destructure, an
    arrow parameter, a match-arm payload.

    The frontend refuses a binder that shadows a module callable *visible to
    its module* (`lower._refuse_callable_shadowing`), but the emitted namespace
    is FLAT over the whole merged program while resolution is per-module: two
    modules that never `use` each other may each own the name (selfhost's
    `fn step` in lexer.rvl against a local `step` in emit_py.rvl). A rewrite
    keyed on the bare name alone therefore reaches calls that resolve to a
    local, so a name the body binds is off limits — the same discipline
    `revl.ownership` applies to its retention summary.
    """
    if isinstance(node, list):
        for item in node:
            _inline_bound_names(item, out)
        return out
    if not isinstance(node, dict):
        return out
    if "step" in node:
        # a STATEMENT binds its `name`/`bind`/`rest`; an expression's `name` is
        # a read, not a binding
        for key in ("name", "bind", "rest"):
            if isinstance(node.get(key), str):
                out.add(node[key])
        for name in node.get("names") or []:
            if isinstance(name, str):
                out.add(name)
    elif node.get("kind") == "arrow":
        for name in node.get("params") or []:
            if isinstance(name, str):
                out.add(name)
    elif node.get("kind") == "match":
        for arm in node.get("arms") or []:
            if isinstance(arm, dict) and isinstance(arm.get("bind"), str):
                out.add(arm["bind"])
    for child in node.values():
        _inline_bound_names(child, out)
    return out


def _inline_node_count(node: Any) -> int:
    if isinstance(node, dict):
        return 1 + sum(_inline_node_count(v) for v in node.values())
    if isinstance(node, list):
        return sum(_inline_node_count(v) for v in node)
    return 0


def _inline_free_names(node: Any, out: set) -> None:
    """Every identifier a `var`/`name` node references in an expression."""
    if isinstance(node, dict):
        if node.get("kind") in ("var", "name"):
            nm = node.get("name") if node.get("kind") == "var" else node.get("id")
            if nm is not None:
                out.add(nm)
        for v in node.values():
            _inline_free_names(v, out)
    elif isinstance(node, list):
        for v in node:
            _inline_free_names(v, out)


def _inline_contains(node: Any, kinds: frozenset) -> bool:
    if isinstance(node, dict):
        if node.get("kind") in kinds:
            return True
        return any(_inline_contains(v, kinds) for v in node.values())
    if isinstance(node, list):
        return any(_inline_contains(v, kinds) for v in node)
    return False


def _inline_count_var(node: Any, name: str) -> int:
    n = 0
    if isinstance(node, dict):
        if node.get("kind") == "var" and node.get("name") == name:
            n += 1
        for v in node.values():
            n += _inline_count_var(v, name)
    elif isinstance(node, list):
        for v in node:
            n += _inline_count_var(v, name)
    return n


def _inline_eager_vars(node: Any) -> set:
    """Parameter names an expression is GUARANTEED to evaluate: the ones whose
    argument is therefore evaluated unconditionally when the inlined body runs.

    Conservative by construction: a name is reported only where control
    provably reaches it (the always-taken side of a short-circuit / ternary,
    the receiver of a builtin whose args may ride a conditional lambda).
    Under-reporting is safe (it just demands a trivial argument); over-reporting
    would not be, so this never guesses."""
    if not isinstance(node, dict):
        return set()
    kind = node.get("kind")
    if kind == "var":
        return {node.get("name")}
    if kind == "bin":
        if node.get("op") in ("&&", "||", "??"):
            # the right operand is short-circuited; only the left is guaranteed
            return _inline_eager_vars(node.get("left"))
        return _inline_eager_vars(node.get("left")) | _inline_eager_vars(node.get("right"))
    if kind == "un":
        return _inline_eager_vars(node.get("operand"))
    if kind == "if":
        # the condition always runs; a name on BOTH arms is still guaranteed
        return (_inline_eager_vars(node.get("cond"))
                | (_inline_eager_vars(node.get("then"))
                   & _inline_eager_vars(node.get("else"))))
    if kind == "builtin":
        # the receiver is always evaluated; some builtins wrap their args in a
        # conditional lambda, so args are not counted
        return _inline_eager_vars(node.get("target"))
    if kind == "index":
        return _inline_eager_vars(node.get("target")) | _inline_eager_vars(node.get("index"))
    if kind in ("field", "optfield"):
        return _inline_eager_vars(node.get("target"))
    if kind == "list":
        out: set = set()
        for item in node.get("items") or []:
            out |= _inline_eager_vars(item)
        return out
    if kind == "match":
        return _inline_eager_vars(node.get("scrutinee"))
    # record / format / interp / arrow / adt / call / ... : nothing provably
    # eager for the parameter (kept empty, which forces a trivial-arg demand)
    return set()


def _inline_replace_var(node: Any, name: str, arg: dict) -> Any:
    """A fresh copy of `node` with every `var name` replaced by a fresh copy of
    `arg` (never mutates either input)."""
    if isinstance(node, dict):
        if node.get("kind") == "var" and node.get("name") == name:
            return copy.deepcopy(arg)
        return {k: _inline_replace_var(v, name, arg) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_replace_var(v, name, arg) for v in node]
    return node


def _inline_substitute(param: str | None, template: dict, arg: dict | None) -> dict | None:
    """Substitute `arg` for the single parameter in `template`, returning None
    when the substitution would not be behavior-preserving. `template` is not
    mutated."""
    if param is None:
        return copy.deepcopy(template)
    uses = _inline_count_var(template, param)
    arg_trivial = arg.get("kind") in _INLINE_TRIVIAL
    arg_pure = not _inline_contains(arg, _INLINE_IMPURE)
    if param in _inline_eager_vars(template):
        # the argument is evaluated unconditionally at least once
        ok = True if uses == 1 else arg_pure  # once: any arg; more: no effects
    else:
        # only conditionally (or never) evaluated -> the arg must neither raise
        # nor have an effect if it is skipped or duplicated
        ok = arg_trivial
    if not ok:
        return None
    return _inline_replace_var(template, param, arg)


def _fn_inline_template(fn: dict) -> tuple[str | None, dict] | None:
    """`(param, expr)` if `fn` is a small pure fn whose body is the guarded
    return shape, else None. The guards fold into one conditional expression."""
    if fn.get("async"):
        return None
    # item 310: a `cache pure` fn is never inlined. The author declared that the
    # call is worth a table lookup, and inlining would copy the body to every
    # call site where no memo can see it — the declaration would silently do
    # nothing. (Inlining is behaviour-preserving, so this costs speed, never
    # correctness, on a fn small enough to have been a candidate.)
    if _is_cache_pure(fn):
        return None
    params = fn.get("params") or []
    if len(params) > 1:
        return None
    body = fn.get("body") or []
    if not body:
        return None
    *guards, last = body
    if last.get("step") != "return" or last.get("expr") is None:
        return None
    expr = last["expr"]
    for guard in reversed(guards):
        if guard.get("step") != "if" or guard.get("else"):
            return None
        then = guard.get("then") or []
        if len(then) != 1 or then[0].get("step") != "return":
            return None
        ret = then[0].get("expr")
        cond = guard.get("cond")
        if ret is None or cond is None:
            return None
        expr = {"kind": "if", "cond": cond, "then": ret, "else": expr}
    param = params[0]["name"] if params else None
    return param, copy.deepcopy(expr)


def _inline_templates(functions: list) -> dict:
    """`{name: (param, expanded_expr)}` for every safely-inlinable fn. Nested
    candidate calls are expanded (so `is_space` folds in `code0`); a fn that
    cannot fully expand to a closed, side-effect-free template is dropped."""
    raw: dict = {}
    for fn in functions:
        tmpl = _fn_inline_template(fn)
        if tmpl is not None:
            raw[fn["name"]] = tmpl

    memo: dict = {}

    def inline_calls(node: Any, stack: frozenset, bound: frozenset) -> Any:
        if isinstance(node, dict):
            node = {k: inline_calls(v, stack, bound) for k, v in node.items()}
            callee = node.get("callee")
            if node.get("kind") == "call" and isinstance(callee, dict) \
                    and callee.get("kind") == "var" and callee.get("name") in raw \
                    and callee.get("name") not in stack \
                    and callee.get("name") not in bound:
                name = callee["name"]
                template = expand(name, stack)
                if template is not None:
                    param = raw[name][0]
                    args = node.get("args") or []
                    if (param is None) == (len(args) == 0):
                        sub = _inline_substitute(
                            param, template, args[0] if args else None)
                        if sub is not None:
                            return sub
            return node
        if isinstance(node, list):
            return [inline_calls(v, stack, bound) for v in node]
        return node

    def expand(name: str, stack: frozenset) -> dict | None:
        if name in memo:
            return memo[name]
        param = raw[name][0]
        # the template's own binders (an arrow parameter, a match-arm payload)
        # shadow a candidate of the same name inside it, exactly as in a caller
        bound = _inline_bound_names(raw[name][1],
                                    {param} if param is not None else set())
        result = inline_calls(copy.deepcopy(raw[name][1]), stack | {name},
                              frozenset(bound))
        allowed = {param} if param is not None else set()
        names: set = set()
        _inline_free_names(result, names)
        if (names - allowed) or _inline_contains(result, _INLINE_IMPURE) \
                or _inline_node_count(result) > _INLINE_MAX_NODES:
            result = None
        memo[name] = result
        return result

    final: dict = {}
    for name in raw:
        expanded = expand(name, frozenset())
        if expanded is not None:
            final[name] = (raw[name][0], expanded)
    return final


def _inline_pure_fns(ir: dict) -> dict:
    """Return a copy of `ir` with calls to safely-inlinable pure fns folded into
    their call sites, inside pure `fn` bodies. The fn definitions are kept (a fn
    may still be referenced as a value), so this only rewrites call sites; an
    unused def is harmless.

    Only `fn` bodies are rewritten, not `test` bodies: hot loops live in fns
    (the self-host stages are fns), a `test` block is an assertion and never a
    hot path, and leaving test bodies alone keeps the call-site spelling a
    cross-tier assertion pins (tests/test_cross_tier_execution.py) intact."""
    templates = _inline_templates(ir.get("functions") or [])
    if not templates:
        return ir

    def rewrite(node: Any, bound: frozenset) -> Any:
        if isinstance(node, dict):
            node = {k: rewrite(v, bound) for k, v in node.items()}
            callee = node.get("callee")
            if node.get("kind") == "call" and isinstance(callee, dict) \
                    and callee.get("kind") == "var" \
                    and callee.get("name") in templates \
                    and callee.get("name") not in bound:
                param, template = templates[callee["name"]]
                args = node.get("args") or []
                if (param is None) == (len(args) == 0):
                    sub = _inline_substitute(
                        param, template, args[0] if args else None)
                    if sub is not None:
                        return sub
            return node
        if isinstance(node, list):
            return [rewrite(v, bound) for v in node]
        return node

    ir = copy.deepcopy(ir)
    for fn in ir.get("functions") or []:
        body = fn.get("body") or []
        # a call through a name this body binds is not a call to the module fn
        # of that name (see `_inline_bound_names`), so it is left alone. Taken
        # over the WHOLE body rather than per-position: a correct un-inlined
        # call is always a better outcome than a risky inline.
        bound = _inline_bound_names(body, {p.get("name") for p in fn.get("params") or []})
        fn["body"] = rewrite(body, frozenset(n for n in bound if isinstance(n, str)))
    return ir


def _parallel_step_groups(ir: dict) -> dict:
    """Per-component fan-out plan (item 259 slice 2): each component name maps to a
    list of groups, each group a list of `emit` step dicts the checker proved
    independent. Derived from the SAME `src/revl/parallel` partition the audit
    surface renders, so the runtime fan-out can never diverge from the checked
    plan.

    Imported lazily and FAIL-SOFT: a backend-only context where the `revl`
    frontend is not importable, or any plan-derivation failure, degrades to `{}` -
    every group then emits sequentially, byte-identical to pre-259. The fan-out is
    a speedup, never a correctness dependency, so it must never break codegen."""
    try:
        from revl.parallel import parallel_plan_steps  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - frontend absent: degrade to sequential
        return {}
    try:
        return parallel_plan_steps(ir)
    except Exception:  # noqa: BLE001 - a plan failure must never break codegen
        return {}


def emit(ir: dict) -> str:
    """Lower one IR document to a cordis-py Python module (as source text)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    _refuse_holes(ir)
    if ir.get("ir_version") not in (IR_VERSION, 2, 3):
        raise EmitError(f"unsupported ir_version {ir.get('ir_version')!r} (expected {IR_VERSION}, 2, or 3)")

    # item 231a: fold small pure helper fns into their call sites, cutting the
    # per-call CPython frame cost on hot loops (the self-host lexer's per-byte
    # scan). Behavior-preserving and conservative; see `_inline_pure_fns`.
    ir = _inline_pure_fns(ir)

    services = ir.get("services") or {}
    components = ir.get("components") or []
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    fault_tests = ir.get("fault_tests") or []
    if not components and not types and not functions and not externs and not tests:
        raise EmitError("IR document has no components, types, functions, externs, or tests")

    # item 92/115: the document-wide async facts that drive the py await
    # decisions, set before any body renders (the module `_expr` and the
    # component emitter read them). item 115: async externs now emit `async def`
    # and so join the await-seed; a non-async extern still erases to a blocking
    # `def` and is deliberately absent.
    global _PY_COLORED_FNS, _PY_ASYNC_EXTERNS, _PY_ASYNC_SVC_OPS, _PY_USES_AS_ASYNC
    _PY_COLORED_FNS = {fn.get("name") for fn in functions if fn.get("async")}
    _PY_ASYNC_EXTERNS = {ext.get("name") for ext in externs if ext.get("async")}
    _PY_ASYNC_SVC_OPS = {
        (svc_name, m_name)
        for svc_name, svc in services.items()
        for m_name, spec in (svc.get("methods") or {}).items()
        if spec.get("async")
    }
    _PY_USES_AS_ASYNC = False

    # item 259 slice 2: the checked fan-out plan, per component (empty in a
    # backend-only context where the revl frontend is not importable, or when no
    # body has a provable parallel group - either way the emission is sequential
    # and byte-identical to before).
    plan_groups = _parallel_step_groups(ir)
    emitters = [
        _ComponentEmitter(component, services, externs,
                          plan_groups.get(component.get("name")))
        for component in components
    ]
    bodies = [emitter.emit() for emitter in emitters]

    names = [emitter.name for emitter in emitters]
    if len(set(names)) != len(names):
        raise EmitError("duplicate component names")

    lifecycle = [test for test in tests if test.get("lifecycle")]
    # item 170: an async-coloured timer body spawns its firing into an asyncio
    # in-flight window, so its emitter marks `__asyncio__`. That is a gate for
    # the `import asyncio as _revl_asyncio` line, not a runtime import — strip it
    # from the `from runtime import …` set. A document with no async timer and
    # no lifecycle test is byte-identical to before (no asyncio import).
    uses_async_timer = any("__asyncio__" in emitter.uses for emitter in emitters)
    uses = sorted(
        (set().union(*(emitter.uses for emitter in emitters)) - {"__asyncio__"})
        | _find_host_roots(functions)
        | _find_host_roots(tests)
        # §7.1: the lifecycle driver loads components through the realm-aware
        # `plug` and reads the host-builtin trace to detect unreleased resources.
        # `retry_idempotent` (item 44) gives the driver its auto-retry right for
        # idempotent emissions — see `_REVL_IDEMPOTENT` and `_revl_call` below.
        | ({"plug", "set_trace", "retry_idempotent"} if lifecycle else set())
        # item 102: only a lifecycle test with an `advance` step drives the
        # clock coeffect, so `Clock` is imported (for `advance`/`reset`) only
        # then — a timer-free document's output is unchanged.
        | ({"Clock"} if any(_lifecycle_uses_clock(t) for t in lifecycle) else set())
        # item 377: only a lifecycle test with an `abort` step drives the 245
        # session-commit owner (`begin_abort`/`finalize_abort`), so the owner
        # symbols are imported only then — an abort-free document is unchanged.
        | ({"SessionOwner", "set_session_owner", "clear_session_owner"}
           if any(_lifecycle_uses_abort(t) for t in lifecycle) else set())
        # item 167: a routed require resolves its worker realms by label, so the
        # emitted router needs the runtime's realm-label registry.
        | ({"realm_label"} if any(c.get("routes") for c in components) else set())
        # item 421 F6: a `Secret[T]`-returning extern is decorated so its result
        # is registered at its origin (`_emit_externs`). The externs are rendered
        # outside any component emitter, so the import is gated here rather than
        # through `emitter.uses`.
        | ({"secret_result"} if any(ext.get("secret_return") for ext in externs)
           else set())
    )

    # Delivery semantics (item 44): the reference runtime driver may auto-retry
    # a transient failure of an emission *iff* it is declared `idempotent`. Map
    # each provision key to the set of its idempotent method names so the
    # `_revl_call` dispatch can consult the checked property at the call site.
    idempotent_map: dict[str, set[str]] = {}
    for component in components:
        for key, svc_name in (component.get("provides") or {}).items():
            methods = (services.get(svc_name) or {}).get("methods") or {}
            idem = {m for m, spec in methods.items() if spec.get("idempotent")}
            if idem:
                idempotent_map[key] = idem

    out = _Lines()
    out.add(0, f'"""Generated by the revl cordis-py backend (ir_version {ir.get("ir_version", IR_VERSION)}) — do not edit.')
    out.add(0)
    out.add(0, f"Components: {', '.join(names)}")
    out.add(0, '"""')
    out.add(0)
    if uses:
        imported = ", ".join(
            f"{name} as {_IMPORT_ALIAS[name]}" if name in _IMPORT_ALIAS else name
            for name in uses
        )
        out.add(0, f"from runtime import {imported}")
        out.add(0)
    # item 396 option B: a `@py ref` extern emits a lazy import thunk that caches
    # the resolved host symbol in this module-level dict and asserts its colour
    # via `inspect`. `import inspect` is stdlib (no USER code runs at load — the
    # host module is imported only inside the thunk, at first call). Emitted only
    # when some extern carries a ref, so a ref-free module is byte-identical.
    if any(ext.get("refs") for ext in externs):
        out.add(0, "import inspect as _inspect")
        out.add(0, "_REVL_REFS = {}")
        out.add(0)
    if lifecycle or uses_async_timer:
        # the lifecycle driver runs under `asyncio.run`, and (item 170) an
        # async-coloured timer body spawns its firing onto the running loop —
        # either needs asyncio in scope.
        out.add(0, "import asyncio as _revl_asyncio")
        if uses_async_timer and not lifecycle:
            out.add(0)
    if lifecycle:
        # delivery semantics (item 44): {key: {idempotent method names}} — the
        # checked retry-eligibility the `_revl_call` dispatch consults
        rendered_idem = "{" + ", ".join(
            f"{key!r}: {{{', '.join(map(repr, sorted(idem)))}}}"
            for key, idem in sorted(idempotent_map.items())
        ) + "}"
        out.add(0, f"_REVL_IDEMPOTENT = {rendered_idem}")
        out.add(0)
    if _uses_bounded_int(ir):
        out.add(0, "_REVL_I64_MIN = -(2 ** 63)")
        out.add(0, "_REVL_I64_MAX = 2 ** 63 - 1")
        out.add(0)
        out.add(0, "def _revl_i64(v):")
        out.add(0, '    """Int is bounded 64-bit and overflow traps. python is '
                   'arbitrary"""')
        out.add(0, '    """precision, so the bound is imposed here."""')
        out.add(0, "    if v < _REVL_I64_MIN or v > _REVL_I64_MAX:")
        out.add(0, "        raise OverflowError('revl: Int overflow')")
        out.add(0, "    return v")
        out.add(0)
    if _uses_bounded_int32(ir):
        out.add(0, "_REVL_I32_MIN = -(2 ** 31)")
        out.add(0, "_REVL_I32_MAX = 2 ** 31 - 1")
        out.add(0)
        out.add(0, "def _revl_i32(v):")
        out.add(0, '    """Int32 is bounded 32-bit and overflow traps '
                   '(docs/arithmetic.md)."""')
        out.add(0, "    if v < _REVL_I32_MIN or v > _REVL_I32_MAX:")
        out.add(0, "        raise OverflowError('revl: Int32 overflow')")
        out.add(0, "    return v")
        out.add(0)
    if _uses_i32_shl(ir):
        out.add(0, "def _revl_i32_wrap(v):")
        out.add(0, '    """Wrap an Int32 `<<` result into 32-bit two\'s '
                   'complement (docs/arithmetic.md)."""')
        out.add(0, "    v &= 0xFFFFFFFF")
        out.add(0, "    return v - 0x100000000 if v & 0x80000000 else v")
        out.add(0)
    if _uses_true_division(ir):
        # `/` is IEEE true division (docs/arithmetic.md): a zero divisor gives
        # +/-inf, and 0/0 gives NaN. Python is the only tier that raises
        # instead, so it is the only one that needs this.
        out.add(0, "import math as _revl_math")
        out.add(0)
        out.add(0, "def _revl_div(a, b):")
        out.add(0, '    """IEEE 754 true division: python raises where every '
                   'other tier follows IEEE."""')
        out.add(0, "    if b:")
        out.add(0, "        return a / b")
        out.add(0, "    if a == 0:")
        out.add(0, "        return float('nan')")
        out.add(0, "    return (_revl_math.copysign(float('inf'), a)")
        out.add(0, "            * _revl_math.copysign(1.0, b))")
        out.add(0)
    # The stdlib lowerings that need more than one expression: a MODULE-LEVEL
    # `def`, gated on use, rather than a lambda built and applied at every
    # evaluation (item 436 F6). Same shape as `_revl_div` above — one frame, no
    # function-object allocation — and each is emitted only where it is used.
    if _uses_trunc_rem(ir):
        out.add(0, "def _revl_rem(a, b):")
        out.add(0, '    """The TRUNCATED remainder: it takes the sign of the '
                   'DIVIDEND, as in"""')
        out.add(0, '    """TypeScript. python\'s own `%` floors and takes the '
                   'divisor\'s sign."""')
        out.add(0, "    return abs(a) % abs(b) if a >= 0 else -(abs(a) % abs(b))")
        out.add(0)
    if _uses_builtin(ir, "div_trunc"):
        out.add(0, "def _revl_div_trunc(a, b):")
        out.add(0, '    """Division that TRUNCATES toward zero '
                   '(docs/arithmetic.md)."""')
        out.add(0, "    return (abs(a) // abs(b) if (a < 0) == (b < 0)")
        out.add(0, "            else -(abs(a) // abs(b)))")
        out.add(0)
    if _uses_builtin(ir, "div_euclid"):
        out.add(0, "def _revl_div_euclid(a, b):")
        out.add(0, '    """Euclidean division: the remainder is never negative '
                   '(docs/arithmetic.md)."""')
        out.add(0, "    return a // b if b > 0 else -(a // -b)")
        out.add(0)
    if _uses_builtin(ir, "indexOf"):
        out.add(0, "def _revl_index_of(v, n):")
        out.add(0, '    """First index of `n`, -1 when absent — a Str receiver '
                   'or a List one."""')
        out.add(0, "    if isinstance(v, str):")
        out.add(0, "        return v.find(n)")
        out.add(0, "    return v.index(n) if n in v else -1")
        out.add(0)
    if _uses_builtin(ir, "split"):
        out.add(0, "def _revl_split(v, s):")
        out.add(0, '    """JS-shape split: an empty separator yields 1-char '
                   'strings (py raises)."""')
        out.add(0, "    return list(v) if s == \"\" else v.split(s)")
        out.add(0)
    if _uses_builtin(ir, "to_int"):
        # FR-9, docs/stdlib-2.0.md §Str.to_int: total on the ASCII digits with
        # an optional leading `-`, `None` otherwise — including out of the i64
        # range, which is None like every other non-digit (the tier's ints are
        # unbounded, so the bound must be checked here rather than by int()).
        out.add(0, "def _revl_str_to_int(s):")
        out.add(0, '    """Str.to_int: the ASCII digits with an optional '
                   'leading `-`, else None."""')
        out.add(0, "    if s == \"\" or s == \"-\" or not s.isascii():")
        out.add(0, "        return None")
        out.add(0, "    if not (s.isdigit() or (s[0] == \"-\" and s[1:].isdigit())):")
        out.add(0, "        return None")
        out.add(0, "    n = int(s)")
        out.add(0, "    return n if -(2**63) <= n <= 2**63 - 1 else None")
        out.add(0)
        if _uses_opt_to_int(ir):
            # reached through `?.`, whose node carries no receiver type:
            # dispatch on the payload, the same split `indexOf` makes.
            out.add(0, "def _revl_opt_to_int(v):")
            out.add(0, '    """`?.to_int()`: the node carries no receiver type, '
                       'so dispatch on the payload."""')
            out.add(0, "    return _revl_str_to_int(v) if isinstance(v, str) else v")
            out.add(0)
    for checked in _CHECKED_DIVS:
        if not _uses_builtin(ir, checked):
            continue
        # The total forms (docs/arithmetic.md): the same quotient as the
        # faulting operation, but a zero divisor yields Err(reason) instead of
        # raising — a pure fn cannot `fail`, so the error travels as a value.
        quotient = {
            "checked_div_trunc":
                "abs(a) // abs(b) if (a < 0) == (b < 0) else -(abs(a) // abs(b))",
            "checked_div_floor": "a // b",
            "checked_div_euclid": "a // b if b > 0 else -(a // -b)",
            "checked_mod": "a % abs(b)",
        }[checked]
        out.add(0, f"def _revl_{checked}(a, b):")
        out.add(0, f'    """Total `{checked[len("checked_"):]}`: a zero divisor '
                   'is Err(reason), never a raise."""')
        out.add(0, "    if b == 0:")
        out.add(0, f"        return Err({_DIV_ZERO_MSG!r})")
        out.add(0, f"    q = {quotient}")
        if checked == "checked_mod":
            # a remainder cannot leave the range its operands are already in
            out.add(0, "    return Ok(q)")
        else:
            # a quotient of 2^63 (Int.MIN/-1) does not fit i64 -> Err, not a value
            out.add(0, "    if -(2**63) <= q <= 2**63 - 1:")
            out.add(0, "        return Ok(q)")
            out.add(0, "    return Err('revl: Int overflow')")
        out.add(0)
    if _uses_float_interp(ir):
        # Canonical Float -> Str (docs/strings.md): the ECMAScript
        # Number::toString shortest-round-trip form, so `${aFloat}` agrees with
        # every other tier. python's own `str(float)` gives `nan`/`0.0`/`-0.0`,
        # none of which match. `repr` supplies the shortest digits; the rest is
        # the ES notation rule (a shared spec each tier spells in host syntax).
        for line in _REVL_FTOA_SRC.splitlines():
            out.add(0, line)
        out.add(0)
    if _uses_cache_pure(ir):
        # item 310, `cache pure`: the body-level memo table and its structural
        # key. Emitted only when some fn declares the clause, so every existing
        # module is byte-identical.
        for line in _REVL_MEMO_SRC.splitlines():
            out.add(0, line)
        out.add(0)
    # built-in Result is a tagged ADT (so `match` can discriminate Ok/Err),
    # unless a user type shadows the name. Opt stays host-None, so it needs
    # no class. Emitted only when the IR actually uses Result — an unused
    # Ok/Err class in every module would be dead code (docs/conformance.md,
    # "Golden policy": output is right because it is right, not because a
    # fixture wants the bytes).
    if _uses_builtin_result(ir):
        user_cases = {
            case["name"]
            for spec in types.values() if spec.get("kind") == "variant"
            for case in spec.get("cases") or []
        }
        for builtin in ("Ok", "Err"):
            if builtin in user_cases:
                continue
            out.add(0, f"class {builtin}:")
            out.add(1, '__slots__ = ("value",)')
            out.add(1, "def __init__(self, value=None):")
            out.add(2, "self.value = value")
            out.add(1, "def __eq__(self, other):")
            out.add(2, f"return isinstance(other, {builtin}) and other.value == self.value")
            out.add(1, "def __hash__(self):")
            out.add(2, f"return hash(({builtin!r}, self.value))")
            out.add(0)
    if types:
        # Only what the record annotations below actually mention (item 436
        # F9): `dataclasses` is no longer imported at all, `Union` never was
        # spellable by `_py_type`, and a variant-only module imports nothing.
        typing_names = _typing_imports(types)
        if typing_names:
            out.add(0, f"from typing import {', '.join(typing_names)}")
            out.add(0)
        out.extend(_emit_types(types))
    if functions:
        out.extend(_emit_functions(functions))
    if externs:
        out.extend(_emit_externs(externs))
    if tests:
        out.extend(_emit_tests(tests))
    if fault_tests:
        out.extend(_emit_fault_tests(fault_tests, names))
    out.add(0, "SERVICES = {")
    for service_name, service in services.items():
        out.add(1, f"{service_name!r}: {{")
        if service.get("commutative"):
            out.add(2, "'commutative': True,")
        for method_name, spec in (service.get("methods") or {}).items():
            # v1/A6: params are typed in the IR; the runtime table keeps names
            param_names = [param.get("name") for param in spec.get("params") or []]
            metadata = [
                f"'params': {param_names!r}",
                f"'emission': {bool(spec.get('emission'))!r}",
            ]
            if spec.get("async"):
                metadata.append("'async': True")
            if spec.get("commutative"):
                metadata.append("'commutative': True")
            # delivery semantics (item 44): the runtime consults this flag to
            # decide whether a transient failure of this emission may be retried
            if spec.get("idempotent"):
                metadata.append("'idempotent': True")
            out.add(2, f"{method_name!r}: {{{', '.join(metadata)}}},")
        out.add(1, "},")
    out.add(0, "}")
    out.add(0)
    out.add(0)
    # item 92: the sync->async coercion wrapper, emitted only when a colored
    # arrow with a statically-sync body needed it (the mock's `msgs => "…"`).
    # `await`-ing a plain value raises on py, so a sync arrow flowing into an
    # async slot is lifted into an `async def` that returns the value.
    if _PY_USES_AS_ASYNC:
        out.add(0, "def _revl_as_async(_f):")
        out.add(1, "async def _g(*_a, **_k):")
        # belt-and-suspenders (item 106): `_f` is classified sync here, so a
        # bare `return _f(...)` normally suffices. But if a misclassification
        # ever routes a coroutine-returning body through this wrapper (e.g. a
        # handle-emission arrow the color analysis failed to see), returning it
        # would leak an unawaited coroutine. Await whatever is awaitable, and
        # pass a genuinely-sync result straight through — correct either way.
        out.add(2, "_r = _f(*_a, **_k)")
        out.add(2, "if hasattr(_r, \"__await__\"):")
        out.add(3, "return await _r")
        out.add(2, "return _r")
        out.add(1, "return _g")
        out.add(0)
        out.add(0)
    # item 167: the emitted realization of a routed require (item 162's `routes`
    # IR), mirroring src/revl/run.py::_Router. A component that
    # `requires <k> in realms("w1"…"wN") strategy(...)` provides `<k>` once
    # downstream (G2) while fanning each call out across the worker realms. The
    # proxy holds no worker handle — it re-resolves the live per-realm handle on
    # every call off the same committed-view lookup a normal require uses
    # (`root.isolate(k, realm(w)).reflect.get(k)`, None for a non-ACTIVE
    # provider), so a withdrawn worker drops out and its calls go to survivors
    # (reactive failover). Emitted only for a routed-require program.
    if any(c.get("routes") for c in components):
        for line in _REVL_ROUTER_SRC.splitlines():
            out.add(0, line)
        out.add(0)
        out.add(0)
    for body in bodies:
        out.extend(body)
        out.add(0)
        out.add(0)
    out.add(0, "COMPONENTS = {" + ", ".join(f"{name!r}: {name}" for name in names) + "}")
    return out.text() + "\n"


if __name__ == "__main__":
    import json
    import sys

    with open(sys.argv[1], encoding="utf-8") as handle:
        print(emit(json.load(handle)), end="")

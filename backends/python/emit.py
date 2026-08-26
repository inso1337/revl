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

_HOST_ROOTS = {"Pool", "Map", "Job"}

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
    "Clock": "_revl_Clock",
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


def _mangle(name: str) -> str:
    """Rename a syntactically-valid identifier that collides with a *Python*
    reserved word, so a valid revl identifier that happens to be a Python
    keyword (`from`, `class`, `lambda`, …) emits and RUNS instead of crashing
    at emit (roadmap item 165).

    The scheme is the A3 append-`_` rename `src/revl/lower.py::_safe_name` (and
    `backends/java/emit.py::_fn_name`) already use for revl-keyword bindings:
    append `_` until the name is no longer a keyword. It is a pure function of
    the name, so the declaration site and every use site agree without
    threading a table around — the single property the mangling must preserve.
    A non-keyword name is returned byte-for-byte unchanged, so no existing
    program (none of which can currently name a Python keyword — those crash
    today) changes its emitted output. This is TARGET keywords only; the host
    roots (`Map`/`Pool`/`Job`) are not keywords and stay guarded in `_ident`."""
    while keyword.iskeyword(name) or keyword.issoftkeyword(name):
        name += "_"
    return name


def _ident(name: Any, what: str) -> str:
    if not isinstance(name, str) or not name.isidentifier():
        raise EmitError(f"{what} {name!r} is not a usable Python identifier")
    if name in _RESERVED or name.startswith("_"):
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
    if method == "concat":
        return f"({target} + {args[0]})"
    if method == "indexOf":
        return (f"(lambda _v, _n: _v.find(_n) if isinstance(_v, str) "
                f"else (_v.index(_n) if _n in _v else -1))({target}, {args[0]})")
    if method == "split":
        # JS-shape split: "" -> 1-char strings (py str.split("") raises).
        return (f"(lambda _v, _s: list(_v) if _s == \"\" "
                f"else _v.split(_s))({target}, {args[0]})")
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
        return (f"(dict((kk, vv) for kk, vv in {target}.items() "
                f"if kk != {args[0]}))")
    # Integer division and modulo (docs/arithmetic.md). Python's `//` floors
    # and its `%` takes the divisor's sign, so div_floor is native and the
    # Euclidean remainder is `a % abs(b)`; truncation has to be built.
    if method == "div_trunc":
        return (f"_revl_i64((lambda _a, _b: abs(_a) // abs(_b) if (_a < 0) == (_b < 0) "
                f"else -(abs(_a) // abs(_b)))({target}, {args[0]}))")
    if method == "div_floor":
        return f"_revl_i64({target} // {args[0]})"
    if method == "div_euclid":
        return (f"_revl_i64((lambda _a, _b: _a // _b if _b > 0 else -(_a // -_b))"
                f"({target}, {args[0]}))")
    if method == "mod":
        return f"({target} % abs({args[0]}))"
    # Int/Int32 width conversions (docs/arithmetic.md). python has one int
    # type, so widening Int32 -> Int is the identity; narrowing Int -> Int32
    # re-imposes the 32-bit bound through `_revl_i32`, which traps out of range.
    if method == "to_int":
        if recv == "Str":
            # Str.to_int (FR-9, docs/stdlib-2.0.md §Str.to_int): total on the
            # ASCII digits with an optional leading `-`, `None` otherwise —
            # including out of the i64 range, which is None like every other
            # non-digit (the tier's ints are unbounded, so the bound must be
            # checked here rather than by int()).
            return (f"(lambda _s: (None if (_s == \"\" or _s == \"-\" "
                    f"or not _s.isascii() "
                    f"or not (_s.isdigit() or (_s[0] == \"-\" and _s[1:].isdigit())) "
                    f"or not (-(2**63) <= (_n := int(_s)) <= 2**63 - 1)) "
                    f"else _n))({target})")
        return f"({target})"
    if method == "to_int32":
        return f"_revl_i32({target})"
    # The total forms (docs/arithmetic.md): same quotient as the faulting
    # operation, but a zero divisor yields Err(reason) instead of raising —
    # the whole point is that a pure fn cannot `fail`, so the error travels
    # as a value. Ok/Err are the tagged classes emitted when the IR uses
    # Result (gated in `_uses_builtin_result`).
    if method in _CHECKED_DIVS:
        quotient = {
            "checked_div_trunc":
                "abs(_a) // abs(_b) if (_a < 0) == (_b < 0) else -(abs(_a) // abs(_b))",
            "checked_div_floor": "_a // _b",
            "checked_div_euclid": "_a // _b if _b > 0 else -(_a // -_b)",
            "checked_mod": "_a % abs(_b)",
        }[method]
        if method == "checked_mod":
            return (f"(lambda _a, _b: Ok({quotient}) if _b != 0 "
                    f"else Err({_DIV_ZERO_MSG!r}))({target}, {args[0]})")
        # a quotient of 2^63 (Int.MIN/-1) does not fit i64 -> Err, not a value
        return (f"(lambda _a, _b: Err({_DIV_ZERO_MSG!r}) if _b == 0 "
                f"else (Ok(_q) if -(2**63) <= (_q := {quotient}) <= 2**63 - 1 "
                f"else Err('revl: Int overflow')))({target}, {args[0]})")
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str): python ints on
    # this tier are already i64-clamped, so str() is the exact decimal.
    if method == "to_str":
        return f"str({target})"
    raise EmitError(f"unknown builtin method {method!r}")


class _ComponentEmitter:
    def __init__(self, component: dict, services: dict, externs: list | None = None) -> None:
        self.ir = component
        self.services = services
        # item 243 (docs/design/243-witnessed-externs.md): witnessed externs by
        # name, so a call site can be recognised as a transactional effect and
        # register its DECLARED inverse (not a site-spelled one) into the
        # accumulator. Absent/empty for every program that uses no witnessed
        # extern, so their emission stays byte-identical.
        self.witnessed = {
            ext["name"]: ext for ext in (externs or [])
            if ext.get("class") == "witnessed"
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

    # -- expressions --------------------------------------------------------

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
                callee = self._expr(expr.get("callee"), where)
                args = ", ".join(self._expr(arg, where) for arg in expr.get("args") or [])
                rendered = f"{callee}({args})"
            # item 141 await-seed: an emission of an async service op — through a
            # req key (`emit model.complete(p)`) or a spawn handle — produces a
            # coroutine. Await it wherever it lands in an async body: not only a
            # statement/return, but a NESTED expression position such as a
            # ternary arm (`p == "go" ? emit m.complete(p) : "idle"`), so no
            # coroutine leaks unawaited. `_py_yields_coroutine` is the same
            # predicate the arrow renderer uses, so the two stay in lockstep.
            if self._in_async and not self._in_arrow \
                    and _py_yields_coroutine(expr, self.requires):
                return f"(await {rendered})"
            return rendered
        if kind == "host":
            fn = expr.get("fn") or ""
            root, _, rest = fn.partition(".")
            if root not in _HOST_ROOTS or not rest.isidentifier():
                raise EmitError(f"{where}: unknown host builtin {fn!r}")
            self.uses.add(root)
            args = ", ".join(self._expr(arg, where) for arg in expr.get("args") or [])
            return f"{fn}({args})"
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
                                   self._expr(arm.get("body"), where))}
                                for arm in arms],
                               awaited=awaited)
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
            # record literals are dicts; ADT payloads are objects — the
            # preamble helper reads either shape
            return f"_revl_field({self._expr(expr.get('target'), where)}, {name!r})"
        if kind == "index":
            return f"{self._expr(expr.get('target'), where)}[{self._expr(expr.get('index'), where)}]"
        if kind == "bin":
            if expr.get("op") == "??":
                lhs = self._expr(expr.get("left"), where)
                rhs = self._expr(expr.get("right"), where)
                # `x ?? d`: `Opt[T]` is represented as `T | None` at runtime
                # (matching the TS backend's `T | undefined` shape).
                return f"({rhs} if {lhs} is None else {lhs})"
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
            target = self._expr(expr.get("target"), where)
            # `x?.name`: short-circuit on Opt-None.
            return f"(None if ({target}) is None else _revl_field({target}, {name!r}))"
        if kind == "optcall":
            method = expr.get("method")
            if not isinstance(method, str) or not method.isidentifier():
                raise EmitError(f"{where}: bad optional method name {method!r}")
            target = self._expr(expr.get("target"), where)
            args = ", ".join(self._expr(a, where) for a in expr.get("args") or [])
            return f"(None if ({target}) is None else ({target}).{method}({args}))"
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
                out.add(indent, f"{bind} = {self._expr(step.get('acquire'), where)}")
                out.add(indent, f"yield lambda: {self._expr(step.get('undo'), where)}")
        elif kind == "effect":
            if step.get("setup"):
                for setup in step["setup"]:
                    self._setup_step(out, indent, setup, where)
            wit = self._witnessed_extern(step.get("acquire"))
            if wit is not None:
                self._witnessed_step(out, indent, step, wit, where, bind=None)
            else:
                out.add(indent, self._expr(step.get("acquire"), where))
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
            out.add(indent, self._expr(step.get("expr"), where))
            if step.get("compensate") is not None:
                # A5: compensation joins the accumulator exactly like an
                # inverse (LIFO); it is compensation, not inversion (§6.1)
                out.add(indent, f"yield lambda: {self._expr(step.get('compensate'), where)}")
        elif kind == "await":
            # A1: the await lands (inertia, paper §4.3.3), then the yield
            # closes the iteration — a divert during the await therefore
            # skips every later step instead of running to the next yield
            out.add(indent, f"await {self._expr(step.get('expr'), where)}")
            out.add(indent, "yield None  # iteration boundary (A1)")
        elif kind == "timer":
            self._timer(out, indent, step, where)
        elif kind == "provide":
            self._provide(out, indent, step, where)
        elif kind == "return":
            raise EmitError(f"{where}: 'return' is only valid inside provide-method bodies")
        else:
            raise EmitError(f"{where}: unknown step {kind!r}")
        out.add(0)

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
        out.add(indent + 1,
                f"yield _revl_frame.transactional((lambda result: {undo}), {tmp}.value)")
        if bind is not None:
            out.add(indent, f"{bind} = {tmp}")

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
        body = method.get("body") or []
        if not body:
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
            fn = f"_effect_{self._counter}"
            out.add(indent, f"def {fn}():")
            out.add(indent + 1, self._expr(step.get("acquire"), where))
            out.add(indent + 1, f"yield lambda: {self._expr(step.get('undo'), where)}")
            out.add(indent, f"_revl_frame.adopt(_revl_ctx.effect({fn}, {self._label(label)!r}))")
        elif kind == "let-effect":
            bind = _ident(step.get("bind"), f"{where}: bind")
            acquire = self._expr(step.get("acquire"), where)
            undo = self._expr(step.get("undo"), where)
            out.add(
                indent,
                f"{bind} = _revl_frame.acquire({self._label(label)!r}, "
                f"lambda: {acquire}, lambda {bind}: {undo})",
            )
        elif kind == "emit":
            if step.get("compensate") is not None:
                fn = f"_emit_{self._counter}"
                out.add(indent, f"def {fn}():")
                out.add(indent + 1, self._expr(step.get("expr"), where))
                out.add(indent + 1, f"yield lambda: {self._expr(step.get('compensate'), where)}")
                out.add(indent, f"_revl_frame.adopt(_revl_ctx.effect({fn}, {self._label(label)!r}))")
            else:
                out.add(indent, self._expr(step.get("expr"), where))
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
            out.add(0, "])")
            out.add(0)
            out.add(0)

        # A1: a body containing an `await` step compiles to an async
        # generator; the runtime treats each yield as an iteration boundary
        # and the await as an in-flight iteration (paper §4.3.2-3)
        is_async = any(step.get("step") == "await" for step in self.ir.get("body") or [])

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
        for step in self.ir.get("body") or []:
            self._body_step(out, 2, step, where)
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


def _emit_types(types: dict) -> "_Lines":
    out = _Lines()
    # Forward-reference support: revl types may be mutually recursive (a
    # record referencing an ADT defined later, or vice versa), but Python
    # evaluates class-body annotations at class-definition time, so a bare
    # name would raise NameError. We cannot use `from __future__ import
    # annotations` (PEP 563): @dataclass's InitVar/ClassVar detection calls
    # sys.modules.get(cls.__module__).__dict__ on every string annotation,
    # which crashes for consumers that exec() the module without registering
    # it in sys.modules. Instead, quote only the annotations that actually
    # reference a not-yet-emitted type; dataclasses treat any string
    # annotation as lazy, and the ADTs here are plain classes (no
    # InitVar/ClassVar introspection), so quoting is always safe.
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
            out.add(0, "@dataclass")
            out.add(0, f"class {name}:")
            if not spec["fields"]:
                out.add(1, "pass")
            for field, ftype in spec["fields"].items():
                # the field is a dataclass attribute name here (a real Python
                # identifier), so a keyword-named field is renamed; record
                # VALUES are dicts read by string key through `_revl_field`, so
                # this annotation-only rename never has to agree with a runtime
                # attribute access (item 165)
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


def _match_expr(scrutinee: str, arms: list, awaited: bool = False) -> str:
    """Emit a match expression as a nested `isinstance` chain.

    Python has no expression-level `elif`, so the chain is built from nested
    conditional expressions. The scrutinee is evaluated exactly once into
    `match`; payload arms bind the case's `.value` before the arm body runs.
    A wildcard arm becomes the chain's final `else`.

    The scrutinee-once binding and each payload bind normally ride a one-shot
    lambda. But a lambda is a SYNC frame: an arm body that crosses an async
    boundary renders an `await`, and `await` inside a lambda is a py
    `SyntaxError` (item 263 — the arm helper hoisted out of an async body must
    inherit its color). When `awaited` is set the binder switches to walrus
    assignments carried by a `(<bind>, <body>)[1]` tuple instead, so every
    `await` lands directly in the enclosing `async def` and none is trapped in
    a lambda. The two forms are otherwise byte-identical.
    """
    # `match` is a revl keyword, so it can never be a user binding in the
    # revl source. Python 3.10+ treats it as a soft keyword, which is still
    # legal as a lambda parameter and as a walrus target.
    tmp = "match"

    def bind_payload(bind: str, body: str, payload: str) -> str:
        # `await`-free arm -> the classic one-shot lambda; an awaited arm ->
        # a walrus bind so the body (which carries the `await`) stays at the
        # frame's top level rather than inside a lambda.
        if awaited:
            return f"(({bind} := {payload}), {body})[1]"
        return f"(lambda {bind}: {body})({payload})"

    def branch(arm: dict, rest: str | None) -> str:
        pattern = arm.get("pattern")
        body = _expr(arm.get("body"))
        if pattern == "_":
            return body
        bind = arm.get("bind")
        # Opt is host-None/value (not a tagged class): Some/None discriminate
        # on None, and Some binds the scrutinee itself. Result/user ADTs are
        # tagged (isinstance), binding the payload `.value`.
        if pattern == "None":
            cond = f"{tmp} is None"
        elif pattern == "Some":
            cond = f"{tmp} is not None"
            if bind:
                body = bind_payload(bind, body, tmp)
        else:
            if bind:
                body = bind_payload(bind, body, f"{tmp}.value")
            cond = f"isinstance({tmp}, {pattern})"
        if rest is None:
            return f"({body} if {cond} else (_ for _ in ()).throw(TypeError('non-exhaustive match')))"
        return f"({body} if {cond} else {rest})"

    result = None
    for arm in reversed(arms):
        result = branch(arm, result)
    if result is None:
        result = "(_ for _ in ()).throw(TypeError('non-exhaustive match'))"
    if awaited:
        return f"(({tmp} := {scrutinee}), {result})[1]"
    return f"(lambda {tmp}: {result})({scrutinee})"


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
            lhs = _expr(node["left"])
            rhs = _expr(node["right"])
            return f"({rhs} if {lhs} is None else {lhs})"
        if node["op"] in ("+", "-", "*") and node.get("operands") == "Int":
            # Int is bounded 64-bit and overflow TRAPS (docs/arithmetic.md).
            # python is arbitrary precision, so it is the tier that has to
            # *impose* the bound rather than detect it — without this, a
            # program that overflows on every other tier quietly succeeds here,
            # which is the reference tier disagreeing with all five others.
            return (f"_revl_i64({_expr(node['left'])} {node['op']} "
                    f"{_expr(node['right'])})")
        if node["op"] in ("+", "-", "*") and node.get("operands") == "Int32":
            # Int32 traps at the 32-bit edge, the same imposition at half the
            # width (docs/arithmetic.md).
            return (f"_revl_i32({_expr(node['left'])} {node['op']} "
                    f"{_expr(node['right'])})")
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
            lhs, rhs = _expr(node["left"]), _expr(node["right"])
            return (f"(lambda _a, _b: abs(_a) % abs(_b) if _a >= 0 "
                    f"else -(abs(_a) % abs(_b)))({lhs}, {rhs})")
        op = _PY_BIN_OPS.get(node["op"])
        if op is None:
            raise EmitError(f"unsupported binary operator {node['op']!r}")
        return f"({_expr(node['left'])} {op} {_expr(node['right'])})"
    if kind == "un":
        if node["op"] == "!":
            return f"(not {_expr(node['operand'])})"
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
        # record literals are dicts; ADT payloads are objects — the preamble
        # helper reads either shape
        return f"_revl_field({_expr(node['target'])}, {node['name']!r})"
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
        target = _expr(node["target"])
        return f"(None if ({target}) is None else _revl_field({target}, {node['name']!r}))"
    if kind == "optcall":
        target = _expr(node["target"])
        args = ", ".join(_expr(a) for a in node.get("args") or [])
        return f"(None if ({target}) is None else ({target}).{node['method']}({args}))"
    raise EmitError(f"unsupported expression kind {kind!r}")


class _RenderedBody(dict):
    """An arm body already rendered by the component emitter; `_expr` returns
    it unchanged so `_match_expr` can be shared by both renderers."""

    def __init__(self, text: str) -> None:
        super().__init__(kind="__rendered__", text=text)


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
            # the binding is a fresh local, so mangle its keyword collisions;
            # the attribute-read spelling is preserved byte-for-byte
            out.add(indent, f"{_mangle(name)} = {tmp}.{name}")
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


def _fn_stmt(node: dict, out: "_Lines", indent: int) -> None:
    step = node["step"]
    if step in ("let", "assign"):
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
        out.add(indent, f"while {_expr(node['cond'])}:")
        if not node["body"]:
            out.add(indent + 1, "pass")
        else:
            for s in node["body"]:
                _fn_stmt(s, out, indent + 1)
    elif step == "for":
        out.add(indent, f"for {_mangle(node['bind'])} in {_expr(node['iterable'])}:")
        if not node["body"]:
            out.add(indent + 1, "pass")
        else:
            for s in node["body"]:
                _fn_stmt(s, out, indent + 1)
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
        out.add(0, f"{'async def' if is_async else 'def'} {name}({params}):")
        if not fn.get("body"):
            out.add(1, "pass")
        for stmt in fn.get("body") or []:
            _fn_stmt(stmt, out, 1)
        out.add(0)
    _PY_IN_ASYNC = False
    _PY_IN_ARROW = False
    _PY_AWAIT_LOCALS = set()
    return out


def _emit_externs(externs: list) -> "_Lines":
    out = _Lines()
    for ext in externs:
        name = _ident(ext["name"], "extern name")
        params = ", ".join(_ident(p["name"], "extern parameter name") for p in ext["params"])
        bodies = ext.get("bodies") or {}
        if "py" not in bodies:
            raise EmitError(
                f"extern `{name}` has no @py body — not portable to this backend "
                f"(available: {', '.join(sorted(bodies)) or 'none'})"
            )
        # item 115 (async-extern.md §8): an async extern emits an `async def`
        # so its verbatim @py body may `await` a host operation; every admitted
        # call site awaits it (see the await-seed and `_py_yields_coroutine`).
        # A non-async extern stays a blocking `def`, unchanged.
        kw = "async def" if ext.get("async") else "def"
        out.add(0, f"{kw} {name}({params}):")
        body = textwrap.dedent(bodies["py"].strip("\n"))
        if body:
            for line in body.splitlines() or [""]:
                out.add(1, line)
        else:
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
    out.add(1, "async def _run():")
    out.add(2, "root = Context()")
    if _lifecycle_uses_clock(test):
        # item 102: the clock coeffect is a module-global; reset it so this
        # test's `advance` steps start from t=0 and see only its own timers,
        # independent of any earlier lifecycle test in the file.
        out.add(2, "_revl_Clock.reset()")
    out.add(2, "events = []")
    out.add(2, "_revl_fibers = {}")
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
    elif kind == "assert_no_residue":
        out.add(indent, f"_revl_no_residue(root, baseline, events, {where!r})")
    elif kind == "assert":
        rendered = _expr(step["expr"])
        out.add(indent, f"assert {rendered}, {where + ': assertion failed'!r}")
    else:  # pragma: no cover — the lowerer emits nothing else
        raise EmitError(f"{where}: unknown lifecycle step {kind!r}")


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

    def inline_calls(node: Any, stack: frozenset) -> Any:
        if isinstance(node, dict):
            node = {k: inline_calls(v, stack) for k, v in node.items()}
            callee = node.get("callee")
            if node.get("kind") == "call" and isinstance(callee, dict) \
                    and callee.get("kind") == "var" and callee.get("name") in raw \
                    and callee.get("name") not in stack:
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
            return [inline_calls(v, stack) for v in node]
        return node

    def expand(name: str, stack: frozenset) -> dict | None:
        if name in memo:
            return memo[name]
        result = inline_calls(copy.deepcopy(raw[name][1]), stack | {name})
        param = raw[name][0]
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

    def rewrite(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: rewrite(v) for k, v in node.items()}
            callee = node.get("callee")
            if node.get("kind") == "call" and isinstance(callee, dict) \
                    and callee.get("kind") == "var" and callee.get("name") in templates:
                param, template = templates[callee["name"]]
                args = node.get("args") or []
                if (param is None) == (len(args) == 0):
                    sub = _inline_substitute(
                        param, template, args[0] if args else None)
                    if sub is not None:
                        return sub
            return node
        if isinstance(node, list):
            return [rewrite(v) for v in node]
        return node

    ir = copy.deepcopy(ir)
    for fn in ir.get("functions") or []:
        fn["body"] = rewrite(fn.get("body") or [])
    return ir


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

    emitters = [_ComponentEmitter(component, services, externs) for component in components]
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
        # item 167: a routed require resolves its worker realms by label, so the
        # emitted router needs the runtime's realm-label registry.
        | ({"realm_label"} if any(c.get("routes") for c in components) else set())
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
    if _uses_float_interp(ir):
        # Canonical Float -> Str (docs/strings.md): the ECMAScript
        # Number::toString shortest-round-trip form, so `${aFloat}` agrees with
        # every other tier. python's own `str(float)` gives `nan`/`0.0`/`-0.0`,
        # none of which match. `repr` supplies the shortest digits; the rest is
        # the ES notation rule (a shared spec each tier spells in host syntax).
        for line in _REVL_FTOA_SRC.splitlines():
            out.add(0, line)
        out.add(0)
    out.add(0, "def _revl_field(v, name):")
    out.add(0, '    """Record literals are dicts, ADT payloads are objects."""')
    out.add(0, "    return v[name] if isinstance(v, dict) else getattr(v, name)")
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
        out.add(0, "from dataclasses import dataclass")
        out.add(0, "from typing import Any, Callable, Optional, Union")
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

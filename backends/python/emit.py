"""revl backend-IR → cordis-py emitter.

``emit(ir)`` turns one IR document (docs/backend-ir.md, ir_version 0) into a
single idiomatic Python module: one plugin dict per component, lowered onto
the cordis-py runtime.

Lowering scheme (see runtime.Frame for the R1 rationale):

* the whole component body compiles to **one** ``ctx.effect(generator)``;
  each ``let-effect`` / ``effect`` step yields its undo expression as the
  inverse, in step order, so the runtime's per-effect LIFO disposer is the
  component's accumulator;
* ``provide`` steps yield the runtime's own ``ctx.provide`` effect into that
  accumulator and populate it with ``ctx.set`` — the withdrawal inverse is
  entirely runtime-derived (R5);
* ``req`` expressions compile to ``ctx.<name>`` committed-view attribute
  access, which stays readable during the component's own teardown (R3);
* ``effect`` steps inside provide-method bodies compile to ``ctx.effect``
  calls adopted by the component's Frame, joining the accumulator (R1);
* ``emit`` steps compile to plain calls — nothing accumulated.
"""

from __future__ import annotations

import keyword
import re
import textwrap
from typing import Any

IR_VERSION = 1

_HOST_ROOTS = {"Pool", "Map", "Job"}

# names the emitted scaffolding owns; an IR identifier colliding with one of
# these would capture the wrong binding, so the emitter rejects it (the IR
# contract defines no identifier lexicon — see REPORT.md)
_RESERVED = {"ctx", "config", "frame", "self", "fmt", "Pool", "Map", "ConfigSchema", "Frame"}

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


def _ident(name: Any, what: str) -> str:
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise EmitError(f"{what} {name!r} is not a usable Python identifier")
    if name in _RESERVED or name.startswith("_"):
        raise EmitError(f"{what} {name!r} collides with emitter scaffolding")
    return name


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_") if part)


class _Lines:
    def __init__(self) -> None:
        self._lines: list[str] = []

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


def _render_builtin(method, target: str, args: list) -> str:
    """The stdlib surface (docs/stdlib-2.0.md), rendered as portable Python.
    `push`/`concat` are persistent (value semantics); `indexOf` returns -1
    when absent on both hosts."""
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
    def __init__(self, component: dict, services: dict) -> None:
        self.ir = component
        self.services = services
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
        # v2: realm placements and intercept metadata (docs/design-v2-realms.md)
        self.isolate = component.get("isolate") or {}
        self.intercept = component.get("intercept") or {}
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
            return _render_builtin(expr.get("method"), t, a)
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
            return f"config[{field!r}]"
        if kind == "req":
            name = expr.get("name")
            if name not in self.requires:
                raise EmitError(f"{where}: req {name!r} is not declared in requires")
            # committed-view access: resolves through the fiber's store, so it
            # stays readable during this component's own teardown (R3)
            return f"ctx.{name}"
        if kind == "call":
            if "target" in expr:
                target = self._expr(expr.get("target"), where)
                method = expr.get("method")
                if not isinstance(method, str) or not method.isidentifier():
                    raise EmitError(f"{where}: bad method name {method!r}")
                args = ", ".join(self._expr(arg, where) for arg in expr.get("args") or [])
                return f"{target}.{method}({args})"
            callee = self._expr(expr.get("callee"), where)
            args = ", ".join(self._expr(arg, where) for arg in expr.get("args") or [])
            return f"{callee}({args})"
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
            return f"{name}({args})"
        if kind == "match":
            # the ADT eliminator, now legal in component/method bodies; the
            # module-level renderer already knows the node shape
            return _match_expr(self._expr(expr.get("scrutinee"), where),
                               [{**arm, "body": _RenderedBody(
                                   self._expr(arm.get("body"), where))}
                                for arm in expr.get("arms") or []])
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
            params = ", ".join(expr.get("params") or [])
            return f"lambda {params}: {self._expr(expr.get('body'), where)}"
        if kind == "format":
            self.uses.add("fmt")
            template = expr.get("template")
            if not isinstance(template, str):
                raise EmitError(f"{where}: format template must be a string")
            args = "".join(", " + self._expr(arg, where) for arg in expr.get("args") or [])
            return f"fmt({template!r}{args})"
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
            # `spawn(ctx, <Component>, {config}, (realms,))` plugs a fresh child
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
            return f"spawn(ctx, {target}, {cfg}, {realms!r})"
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
            bind = _ident(step.get("bind"), f"{where}: bind")
            out.add(indent, f"{bind} = {self._expr(step.get('acquire'), where)}")
            out.add(indent, f"yield lambda: {self._expr(step.get('undo'), where)}")
        elif kind == "effect":
            if step.get("setup"):
                for setup in step["setup"]:
                    self._setup_step(out, indent, setup, where)
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
        elif kind == "provide":
            self._provide(out, indent, step, where)
        elif kind == "return":
            raise EmitError(f"{where}: 'return' is only valid inside provide-method bodies")
        else:
            raise EmitError(f"{where}: unknown step {kind!r}")
        out.add(0)

    def _provide(self, out: _Lines, indent: int, step: dict, where: str) -> None:
        name = _ident(step.get("name"), f"{where}: provide key")
        if name in _CONTEXT_MEMBERS:
            raise EmitError(
                f"{where}: provision key {name!r} collides with a cordis-py "
                f"Context member — `ctx.{name}` already exists, so the "
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
        # ctx.provide's own disposer, yielded into the component accumulator
        out.add(indent, f"yield ctx.provide({name!r})")
        out.add(indent, f"ctx.set({name!r}, {cls}())")

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
        for step in body:
            self._method_step(out, indent + 1, provide_name, name, step, mwhere, method_is_async)

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
            out.add(indent, f"frame.adopt(ctx.effect({fn}, {self._label(label)!r}))")
        elif kind == "let-effect":
            bind = _ident(step.get("bind"), f"{where}: bind")
            acquire = self._expr(step.get("acquire"), where)
            undo = self._expr(step.get("undo"), where)
            out.add(
                indent,
                f"{bind} = frame.acquire({self._label(label)!r}, "
                f"lambda: {acquire}, lambda {bind}: {undo})",
            )
        elif kind == "emit":
            if step.get("compensate") is not None:
                fn = f"_emit_{self._counter}"
                out.add(indent, f"def {fn}():")
                out.add(indent + 1, self._expr(step.get("expr"), where))
                out.add(indent + 1, f"yield lambda: {self._expr(step.get('compensate'), where)}")
                out.add(indent, f"frame.adopt(ctx.effect({fn}, {self._label(label)!r}))")
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
            out.add(0, f"_{self.snake.upper()}_CONFIG = ConfigSchema([")
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

        out.add(0, f"def _{self.snake}_apply(ctx, config):")
        out.add(1, f"frame = Frame(ctx, {self.name!r})")
        out.add(0)
        out.add(1, f"{'async def' if is_async else 'def'} _body():")
        for step in self.ir.get("body") or []:
            self._body_step(out, 2, step, where)
        out.add(2, "yield frame.drain")
        out.add(0)
        out.add(1, "frame.install(_body)")
        out.add(0)
        out.add(0)
        out.add(0, f"{self.name} = {{")
        out.add(1, f"'name': {self.name!r},")
        if self.intercept:
            # v2: dict-form inject — non-null values land in the fiber
            # context's intercept chain (the consumer-declared d(k))
            inject = {key: self.intercept.get(key) for key in self.requires}
            out.add(1, f"'inject': {inject!r},")
        else:
            out.add(1, f"'inject': {list(self.requires.keys())!r},")
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
                out.add(1, f"{field}: {_ann(ftype)}")
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


def _match_expr(scrutinee: str, arms: list) -> str:
    """Emit a match expression as a nested `isinstance` chain.

    Python has no expression-level `elif`, so the chain is built from nested
    conditional expressions inside a one-shot lambda. The scrutinee is
    evaluated exactly once into `match`; payload arms bind the case's
    `.value` by immediately invoking another lambda whose parameter is the
    revl bind name. A wildcard arm becomes the chain's final `else`.
    """
    # `match` is a revl keyword, so it can never be a user binding in the
    # revl source. Python 3.10+ treats it as a soft keyword, which is still
    # legal as a lambda parameter.
    tmp = "match"

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
                body = f"(lambda {bind}: {body})({tmp})"
        else:
            if bind:
                body = f"(lambda {bind}: {body})({tmp}.value)"
            cond = f"isinstance({tmp}, {pattern})"
        if rest is None:
            return f"({body} if {cond} else (_ for _ in ()).throw(TypeError('non-exhaustive match')))"
        return f"({body} if {cond} else {rest})"

    result = None
    for arm in reversed(arms):
        result = branch(arm, result)
    if result is None:
        result = "(_ for _ in ()).throw(TypeError('non-exhaustive match'))"
    return f"(lambda {tmp}: {result})({scrutinee})"


def _expr(node: dict) -> str:
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
        return name
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
        return f"{_expr(node['callee'])}({', '.join(_expr(a) for a in node['args'])})"
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
            [_expr(a) for a in node.get("args") or []])
    if kind == "maplit":
        # `Map.empty()` (docs/stdlib-2.0.md §Map)
        return "{}"
    if kind == "arrow":
        params = list(node["params"])
        captures = node.get("captures") or []
        lambda_params = ", ".join(params + [f"{name}={name}" for name in captures])
        return f"lambda {lambda_params}: {_expr(node['body'])}"
    if kind == "match":
        return _match_expr(_expr(node["scrutinee"]), node["arms"])
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
    """Emit a ``let_pattern`` step by evaluating the RHS once into a temp."""
    tmp = f"__revl_destructure_{id(node)}"
    out.add(indent, f"{tmp} = {_expr(node['value'])}")
    if node["pattern"] == "record":
        for name in node["names"]:
            out.add(indent, f"{name} = {tmp}.{name}")
    elif node["pattern"] == "list":
        names = node["names"]
        rest = node.get("rest")
        if rest is None:
            if len(names) == 1:
                out.add(indent, f"{names[0]} = {tmp}[0]")
            else:
                out.add(indent, f"{', '.join(names)} = {tmp}")
        else:
            out.add(indent, f"{', '.join(names)}, *{rest} = {tmp}")
    else:
        raise EmitError(f"unsupported let_pattern kind {node['pattern']!r}")


def _fn_stmt(node: dict, out: "_Lines", indent: int) -> None:
    step = node["step"]
    if step in ("let", "assign"):
        out.add(indent, f"{node['name']} = {_expr(node['value'])}")
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
        out.add(indent, f"for {node['bind']} in {_expr(node['iterable'])}:")
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
    out = _Lines()
    for fn in functions:
        name = _ident(fn["name"], "function name")
        params = ", ".join(_ident(p["name"], "parameter name") for p in fn["params"])
        out.add(0, f"def {name}({params}):")
        if not fn.get("body"):
            out.add(1, "pass")
        for stmt in fn.get("body") or []:
            _fn_stmt(stmt, out, 1)
        out.add(0)
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
        out.add(0, f"def {name}({params}):")
        body = bodies["py"].strip()
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
_LIFECYCLE_ACQUIRE = {"new": "drop", "open": "close"}


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
    result = getattr(impl, method)(*args)
    if _revl_inspect.isawaitable(result):
        result = await result
    return result
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
    out.add(2, "events = []")
    out.add(2, "_revl_fibers = {}")
    out.add(2, "set_trace(events.append)")
    out.add(2, "try:")
    out.add(3, "baseline = _revl_residue(root)")
    body = test.get("body") or []
    if not body:
        out.add(3, "pass")
    for step in body:
        _lifecycle_step(out, 3, step, where)
    out.add(2, "finally:")
    out.add(3, "set_trace(None)")
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
        out.add(indent, f"_revl_fibers[{component!r}] = plug(root, {component}, {{{config}}})")
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
    elif kind == "assert_no_residue":
        out.add(indent, f"_revl_no_residue(root, baseline, events, {where!r})")
    elif kind == "assert":
        rendered = _expr(step["expr"])
        out.add(indent, f"assert {rendered}, {where + ': assertion failed'!r}")
    else:  # pragma: no cover — the lowerer emits nothing else
        raise EmitError(f"{where}: unknown lifecycle step {kind!r}")
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
    whether to emit the built-in Result classes (keeps v1 goldens intact)."""
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


def emit(ir: dict) -> str:
    """Lower one IR document to a cordis-py Python module (as source text)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    _refuse_holes(ir)
    if ir.get("ir_version") not in (IR_VERSION, 2, 3):
        raise EmitError(f"unsupported ir_version {ir.get('ir_version')!r} (expected {IR_VERSION}, 2, or 3)")

    services = ir.get("services") or {}
    components = ir.get("components") or []
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    fault_tests = ir.get("fault_tests") or []
    if not components and not types and not functions and not externs and not tests:
        raise EmitError("IR document has no components, types, functions, externs, or tests")

    emitters = [_ComponentEmitter(component, services) for component in components]
    bodies = [emitter.emit() for emitter in emitters]

    names = [emitter.name for emitter in emitters]
    if len(set(names)) != len(names):
        raise EmitError("duplicate component names")

    lifecycle = [test for test in tests if test.get("lifecycle")]
    uses = sorted(
        set().union(*(emitter.uses for emitter in emitters))
        | _find_host_roots(functions)
        | _find_host_roots(tests)
        # §7.1: the lifecycle driver loads components through the realm-aware
        # `plug` and reads the host-builtin trace to detect unreleased resources
        | ({"plug", "set_trace"} if lifecycle else set())
    )

    out = _Lines()
    out.add(0, f'"""Generated by the revl cordis-py backend (ir_version {ir.get("ir_version", IR_VERSION)}) — do not edit.')
    out.add(0)
    out.add(0, f"Components: {', '.join(names)}")
    out.add(0, '"""')
    out.add(0)
    if uses:
        out.add(0, f"from runtime import {', '.join(uses)}")
        out.add(0)
    if lifecycle:
        out.add(0, "import asyncio as _revl_asyncio")
        out.add(0, "import inspect as _revl_inspect")
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
    # no class. Emitted only when the IR actually uses Result, so v1 goldens
    # stay byte-identical.
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
            out.add(2, f"{method_name!r}: {{{', '.join(metadata)}}},")
        out.add(1, "},")
    out.add(0, "}")
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

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
        if kind == "var":
            return _ident(expr.get("name"), f"{where}: variable")
        if kind == "field":
            name = expr.get("name")
            if not isinstance(name, str) or not name.isidentifier():
                raise EmitError(f"{where}: bad field name {name!r}")
            return f"{self._expr(expr.get('target'), where)}.{name}"
        if kind == "index":
            return f"{self._expr(expr.get('target'), where)}[{self._expr(expr.get('index'), where)}]"
        if kind == "bin":
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
        if self.config_fields:
            out.add(1, f"config = _{self.snake.upper()}_CONFIG.resolve(config)")
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


def _split_types(inner: str) -> list[str]:
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


def _py_type(type_name: str) -> str:
    type_name = type_name.strip()
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
            return f"Union[{_py_type(args[0])}, {_py_type(args[1])}]"
        return base + "[" + ", ".join("Any" for _ in args) + "]"
    if type_name in _PY_TYPE:
        return _PY_TYPE[type_name]
    return type_name  # named record/variant type or generic param


def _emit_types(types: dict) -> "_Lines":
    out = _Lines()
    for name, spec in types.items():
        name = _ident(name, "type name")
        if spec["kind"] == "record":
            out.add(0, "@dataclass")
            out.add(0, f"class {name}:")
            if not spec["fields"]:
                out.add(1, "pass")
            for field, ftype in spec["fields"].items():
                out.add(1, f"{field}: {_py_type(ftype)}")
        else:
            out.add(0, f"class {name}:")
            out.add(1, "__slots__ = ()")
            out.add(0)
            for case in spec["cases"]:
                cname = _ident(case["name"], "case name")
                if case["payload"] is None:
                    out.add(0, f"class {cname}({name}):")
                    out.add(1, "__slots__ = ()")
                else:
                    out.add(0, f"class {cname}({name}):")
                    out.add(1, '__slots__ = ("value",)')
                    out.add(1, "def __init__(self, value):")
                    out.add(2, "self.value = value")
                out.add(0)
        out.add(0)
    return out


def _interp_fstring(parts) -> str:
    segs = ['f"']
    for kind, text in parts:
        if kind == "text":
            segs.append(text.replace("\\", "\\\\").replace('"', '\\"').replace("{", "{{").replace("}", "}}"))
        else:
            segs.append("{" + text + "}")
    segs.append('"')
    return "".join(segs)


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
    kind = node["kind"]
    if kind == "lit":
        return repr(node["value"])
    if kind == "var":
        return node["name"]
    if kind == "bin":
        op = _PY_BIN_OPS.get(node["op"])
        if op is None:
            raise EmitError(f"unsupported binary operator {node['op']!r}")
        return f"({_expr(node['left'])} {op} {_expr(node['right'])})"
    if kind == "un":
        if node["op"] == "!":
            return f"(not {_expr(node['operand'])})"
        if node["op"] == "-":
            return f"(-{_expr(node['operand'])})"
        raise EmitError(f"unsupported unary operator {node['op']!r}")
    if kind == "call":
        return f"{_expr(node['callee'])}({', '.join(_expr(a) for a in node['args'])})"
    if kind == "field":
        return f"{_expr(node['target'])}.{node['name']}"
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
    if kind == "arrow":
        params = list(node["params"])
        captures = node.get("captures") or []
        lambda_params = ", ".join(params + [f"{name}={name}" for name in captures])
        return f"lambda {lambda_params}: {_expr(node['body'])}"
    if kind == "match":
        return _match_expr(_expr(node["scrutinee"]), node["arms"])
    if kind == "interp":
        return _interp_fstring(node["parts"])
    raise EmitError(f"unsupported expression kind {kind!r}")


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
        out.add(indent, f"assert {_expr(node['expr'])}")
    else:
        raise EmitError(f"unsupported fn statement step {step!r}")


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
    for index, test in enumerate(tests):
        fn_name = f"test_{index}"
        out.add(0, f"def {fn_name}():")
        if not test.get("body"):
            out.add(1, "pass")
        for stmt in test.get("body") or []:
            _fn_stmt(stmt, out, 1)
        out.add(0)
        out.add(0, f"REVL_TESTS.append(({test['name']!r}, {fn_name}))")
        out.add(0)
    return out


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



def emit(ir: dict) -> str:
    """Lower one IR document to a cordis-py Python module (as source text)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    if ir.get("ir_version") not in (IR_VERSION, 2, 3):
        raise EmitError(f"unsupported ir_version {ir.get('ir_version')!r} (expected {IR_VERSION}, 2, or 3)")

    services = ir.get("services") or {}
    components = ir.get("components") or []
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    if not components and not types and not functions and not externs and not tests:
        raise EmitError("IR document has no components, types, functions, externs, or tests")

    emitters = [_ComponentEmitter(component, services) for component in components]
    bodies = [emitter.emit() for emitter in emitters]

    names = [emitter.name for emitter in emitters]
    if len(set(names)) != len(names):
        raise EmitError("duplicate component names")

    uses = sorted(
        set().union(*(emitter.uses for emitter in emitters))
        | _find_host_roots(functions)
        | _find_host_roots(tests)
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
    if types:
        out.add(0, "from dataclasses import dataclass")
        out.add(0, "from typing import Any, Optional, Union")
        out.add(0)
        out.extend(_emit_types(types))
    if functions:
        out.extend(_emit_functions(functions))
    if externs:
        out.extend(_emit_externs(externs))
    if tests:
        out.extend(_emit_tests(tests))
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

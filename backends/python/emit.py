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
            target = self._expr(expr.get("target"), where)
            method = expr.get("method")
            if not isinstance(method, str) or not method.isidentifier():
                raise EmitError(f"{where}: bad method name {method!r}")
            args = ", ".join(self._expr(arg, where) for arg in expr.get("args") or [])
            return f"{target}.{method}({args})"
        if kind == "host":
            fn = expr.get("fn") or ""
            root, _, rest = fn.partition(".")
            if root not in _HOST_ROOTS or not rest.isidentifier():
                raise EmitError(f"{where}: unknown host builtin {fn!r}")
            self.uses.add(root)
            args = ", ".join(self._expr(arg, where) for arg in expr.get("args") or [])
            return f"{fn}({args})"
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

    def _body_step(self, out: _Lines, indent: int, step: dict, where: str) -> None:
        """A step at activation-body level — lines inside the body generator."""
        kind = step.get("step")
        if kind == "let-effect":
            bind = _ident(step.get("bind"), f"{where}: bind")
            out.add(indent, f"{bind} = {self._expr(step.get('acquire'), where)}")
            out.add(indent, f"yield lambda: {self._expr(step.get('undo'), where)}")
        elif kind == "effect":
            out.add(indent, self._expr(step.get("acquire"), where))
            out.add(indent, f"yield lambda: {self._expr(step.get('undo'), where)}")
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
        out.add(indent, f"def {name}(self{''.join(', ' + p for p in params)}):")
        body = method.get("body") or []
        if not body:
            out.add(indent + 1, "pass")
            return
        for step in body:
            self._method_step(out, indent + 1, provide_name, name, step, mwhere)

    def _method_step(self, out: _Lines, indent: int, provide_name: str, method_name: str, step: dict, where: str) -> None:
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
            raise EmitError(f"{where}: 'await' is not allowed inside provide-method bodies (A1)")
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
        return out


def emit(ir: dict) -> str:
    """Lower one IR document to a cordis-py Python module (as source text)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    if ir.get("ir_version") not in (IR_VERSION, 2):
        raise EmitError(f"unsupported ir_version {ir.get('ir_version')!r} (expected {IR_VERSION} or 2)")

    services = ir.get("services") or {}
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")

    emitters = [_ComponentEmitter(component, services) for component in components]
    bodies = [emitter.emit() for emitter in emitters]

    names = [emitter.name for emitter in emitters]
    if len(set(names)) != len(names):
        raise EmitError("duplicate component names")

    uses = sorted(set().union(*(emitter.uses for emitter in emitters)))

    out = _Lines()
    out.add(0, '"""Generated by the revl cordis-py backend (ir_version 1) — do not edit.')
    out.add(0)
    out.add(0, f"Components: {', '.join(names)}")
    out.add(0, '"""')
    out.add(0)
    out.add(0, f"from runtime import {', '.join(uses)}")
    out.add(0)
    out.add(0, "SERVICES = {")
    for service_name, service in services.items():
        out.add(1, f"{service_name!r}: {{")
        for method_name, spec in (service.get("methods") or {}).items():
            # v1/A6: params are typed in the IR; the runtime table keeps names
            param_names = [param.get("name") for param in spec.get("params") or []]
            out.add(
                2,
                f"{method_name!r}: {{'params': {param_names!r}, "
                f"'emission': {bool(spec.get('emission'))!r}}},",
            )
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

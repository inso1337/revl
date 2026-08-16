"""Checking and lowering: typed AST -> backend IR (docs/backend-ir.md).

Guarantee enforcement map (DESIGN.md §4):
  G1 undeclared access        -> _lower_postfix name resolution
  G2 provision disjointness   -> link()
  G3 dependency cycles        -> link()
  G4 inverse-or-emit          -> parser (missing undo) + emission checks here
  G5 no teardown registration -> by construction (undo is an expression;
                                 there is no syntactic position for effects)
  G6 purity outside effects   -> by construction (statement grammar)
  A2 no acquisition after provide -> check_component body scan
"""

from __future__ import annotations

from .errors import RevlError
from .parser import (
    ComponentDecl,
    EffectStmt,
    EmitStmt,
    Interp,
    LetEffect,
    Lit,
    Postfix,
    Program,
    ProvideStmt,
    ReturnStmt,
    ServiceDecl,
)

IR_VERSION = 0


class Env:
    def __init__(self, component: ComponentDecl, services: dict[str, ServiceDecl], filename: str):
        self.component = component
        self.services = services
        self.filename = filename
        self.config_fields = {f.name for f in component.config}
        self.requires = dict()  # local -> service name
        for local, svc, line in component.requires:
            if svc not in services:
                raise RevlError(filename, line, f"unknown service `{svc}` in `requires` of {component.name}")
            if local in self.requires:
                raise RevlError(filename, line, f"duplicate requirement name `{local}` in {component.name}")
            self.requires[local] = svc
        self.locals: set[str] = set()
        self.params: list[str] = []

    def bind_local(self, name: str, line: int):
        if name in self.locals or name in self.requires or name in self.params:
            raise RevlError(self.filename, line, f"name `{name}` is already bound in {self.component.name}")
        self.locals.add(name)


def check_and_lower(program: Program) -> dict:
    services = {}
    for svc in program.services:
        if svc.name in services:
            raise RevlError(program.filename, svc.line, f"duplicate service `{svc.name}`")
        services[svc.name] = svc

    components = []
    seen = set()
    for comp in program.components:
        if comp.name in seen:
            raise RevlError(program.filename, comp.line, f"duplicate component `{comp.name}`")
        seen.add(comp.name)
        components.append(_lower_component(comp, services, program.filename))

    _link(program, components)

    return {
        "ir_version": IR_VERSION,
        "services": {
            name: {
                "methods": {
                    m.name: {"params": list(m.params), "emission": m.emission}
                    for m in svc.methods.values()
                }
            }
            for name, svc in services.items()
        },
        "components": components,
    }


# ---------------------------------------------------------------- components

def _lower_component(comp: ComponentDecl, services: dict[str, ServiceDecl], filename: str) -> dict:
    env = Env(comp, services, filename)

    provides = {}
    for key, svc, line in comp.provides:
        if svc not in services:
            raise RevlError(filename, line, f"unknown service `{svc}` in `provides` of {comp.name}")
        if key in provides:
            raise RevlError(filename, line, f"duplicate provision key `{key}` in {comp.name}")
        provides[key] = svc

    body = []
    provided_keys: set[str] = set()
    provide_seen_line: int | None = None
    for stmt in comp.body:
        if isinstance(stmt, (LetEffect, EffectStmt)) and provide_seen_line is not None:
            raise RevlError(
                filename, stmt.line,
                "acquisition after `provide` — an effect acquired after a provision "
                "would be reverted while dependents can still call the service",
                hint="move acquisitions above the `provide` block (linker rule A2)",
            )
        if isinstance(stmt, LetEffect):
            acquire = _lower_expr(stmt.acquire, env, mode="setup")
            env.bind_local(stmt.bind, stmt.line)
            undo = _lower_expr(stmt.undo, env, mode="undo")
            body.append({"step": "let-effect", "bind": stmt.bind, "acquire": acquire, "undo": undo})
        elif isinstance(stmt, EffectStmt):
            body.append({
                "step": "effect",
                "acquire": _lower_expr(stmt.acquire, env, mode="setup"),
                "undo": _lower_expr(stmt.undo, env, mode="undo"),
            })
        elif isinstance(stmt, EmitStmt):
            body.append({"step": "emit", "expr": _lower_emit(stmt, env)})
        elif isinstance(stmt, ProvideStmt):
            provide_seen_line = stmt.line
            body.append(_lower_provide(stmt, provides, provided_keys, env))
        else:  # pragma: no cover — grammar prevents it
            raise RevlError(filename, stmt.line, "unexpected statement in component body")

    return {
        "name": comp.name,
        "config": [{"name": f.name, "type": f.type, "default": f.default} for f in comp.config],
        "requires": dict(env.requires),
        "provides": provides,
        "body": body,
    }


def _lower_provide(stmt: ProvideStmt, provides: dict[str, str], provided_keys: set[str], env: Env) -> dict:
    filename = env.filename
    comp = env.component
    if stmt.key not in provides:
        raise RevlError(filename, stmt.line, f"`{stmt.key}` is not declared in the `provides` clause of {comp.name}")
    if stmt.key in provided_keys:
        raise RevlError(filename, stmt.line, f"provision `{stmt.key}` is installed twice in {comp.name}")
    provided_keys.add(stmt.key)

    svc = env.services[provides[stmt.key]]
    methods = []
    implemented = set()
    for method in stmt.methods:
        decl = svc.methods.get(method.name)
        if decl is None:
            raise RevlError(filename, method.line,
                            f"`{method.name}` is not a method of service {svc.name}")
        if len(method.params) != len(decl.params):
            raise RevlError(
                filename, method.line,
                f"method `{method.name}` of provision `{stmt.key}` takes {len(method.params)} "
                f"params but service {svc.name} declares {len(decl.params)}",
            )
        if method.name in implemented:
            raise RevlError(filename, method.line, f"duplicate method `{method.name}` in provision `{stmt.key}`")
        implemented.add(method.name)

        saved = env.params
        env.params = list(method.params)
        mbody = []
        returned = False
        for mstmt in method.body:
            if returned:
                raise RevlError(filename, mstmt.line, "unreachable statement after `return`")
            if isinstance(mstmt, EffectStmt):
                mbody.append({
                    "step": "effect",
                    "acquire": _lower_expr(mstmt.acquire, env, mode="setup"),
                    "undo": _lower_expr(mstmt.undo, env, mode="undo"),
                })
            elif isinstance(mstmt, EmitStmt):
                mbody.append({"step": "emit", "expr": _lower_emit(mstmt, env)})
            elif isinstance(mstmt, ReturnStmt):
                mbody.append({"step": "return", "expr": _lower_expr(mstmt.expr, env, mode="setup")})
                returned = True
            else:  # pragma: no cover
                raise RevlError(filename, mstmt.line, "unexpected statement in method body")
        env.params = saved
        methods.append({"name": method.name, "params": list(method.params), "body": mbody})

    missing = set(svc.methods) - implemented
    if missing:
        name = sorted(missing)[0]
        raise RevlError(filename, stmt.line,
                        f"provision `{stmt.key}` is missing method `{name}` declared by service {svc.name}")

    return {"step": "provide", "name": stmt.key, "service": svc.name, "methods": methods}


# ---------------------------------------------------------------- expressions

def _lower_emit(stmt: EmitStmt, env: Env) -> dict:
    node = _lower_expr(stmt.expr, env, mode="emit")
    if not _is_emission_call(node, env):
        desc = _node_desc(node)
        raise RevlError(env.filename, stmt.line,
                        f"`emit` on {desc}, which is not declared `emission`",
                        hint="only calls to `emission` service operations cross the boundary; "
                             "drop the `emit` marker (G4)")
    return node


def _is_emission_call(node: dict, env: Env) -> bool:
    if node.get("kind") != "call":
        return False
    target = node["target"]
    if target.get("kind") != "req":
        return False
    svc = env.services[env.requires[target["name"]]]
    decl = svc.methods.get(node["method"])
    return decl is not None and decl.emission


def _lower_expr(expr, env: Env, mode: str):
    """mode: 'setup' | 'undo' | 'emit'.

    Emission calls are illegal in 'setup' (must be marked `emit`; the outer
    call of an `emit` statement is verified by `_lower_emit`) and — documented
    v0 exception — permitted bare in 'undo', where the expression position
    leaves no room for a marker (DESIGN §3.5 note; the compensate slot
    arrives with IR v1/A5).
    """
    if isinstance(expr, Lit):
        return {"kind": "lit", "value": expr.value}
    if isinstance(expr, Interp):
        template = []
        args = []
        for kind, value in expr.parts:
            if kind == "text":
                template.append(value)
            else:
                template.append(f"${len(args)}")
                args.append(_lower_expr(Postfix(value, [], expr.line), env, mode))
        return {"kind": "format", "template": "".join(template), "args": args}
    if isinstance(expr, Postfix):
        return _lower_postfix(expr, env, mode)
    raise RevlError(env.filename, getattr(expr, "line", 0), "unsupported expression")  # pragma: no cover


def _lower_postfix(expr: Postfix, env: Env, mode: str):
    comp = env.component
    head = expr.head
    ops = list(expr.ops)

    if head == "config":
        if not ops or ops[0].args is not None:
            raise RevlError(env.filename, expr.line, "`config` is accessed as `config.<field>`")
        field = ops.pop(0)
        if field.name not in env.config_fields:
            raise RevlError(env.filename, field.line,
                            f"`{field.name}` is not a config field of {comp.name}")
        node = {"kind": "config", "field": field.name}
    elif head in env.params or head in env.locals:
        node = {"kind": "name", "id": head}
    elif head in env.requires:
        if not ops or ops[0].args is None:
            raise RevlError(env.filename, expr.line,
                            f"a required service is used through its methods: `{head}.<method>(...)`")
        node = {"kind": "req", "name": head}
    elif head[:1].isupper() and ops and ops[0].args is not None:
        call = ops.pop(0)
        node = {
            "kind": "host",
            "fn": f"{head}.{call.name}",
            "args": [_lower_expr(a, env, mode) for a in call.args],
        }
    else:
        declared = ", ".join(f"`{r}`" for r in env.requires) or "<nothing>"
        raise RevlError(
            env.filename, expr.line,
            f"`{head}` is not a declared requirement of {comp.name}",
            hint=f"component {comp.name} requires {declared} — add `requires {head}: <Service>`?",
        )

    for op in ops:
        if op.args is None:
            raise RevlError(env.filename, op.line,
                            f"field access `.{op.name}` is not supported in v0 — only method calls")
        if node["kind"] == "req":
            svc = env.services[env.requires[node["name"]]]
            decl = svc.methods.get(op.name)
            if decl is None:
                raise RevlError(env.filename, op.line,
                                f"`{node['name']}.{op.name}` is not a method of service {svc.name}")
            if len(op.args) != len(decl.params):
                raise RevlError(env.filename, op.line,
                                f"`{node['name']}.{op.name}` takes {len(decl.params)} "
                                f"argument(s), {len(op.args)} given")
            if decl.emission and mode == "setup":
                raise RevlError(
                    env.filename, op.line,
                    f"call to emission `{node['name']}.{op.name}` must be marked `emit` (G4)",
                    hint="an emission crosses the system boundary and cannot be reverted; "
                         "`emit` makes that visible at the call site",
                )
        node = {
            "kind": "call",
            "target": node,
            "method": op.name,
            "args": [_lower_expr(a, env, mode) for a in op.args],
        }
    return node


def _node_desc(node: dict) -> str:
    if node.get("kind") == "call":
        target = node["target"]
        if target.get("kind") == "req":
            return f"`{target['name']}.{node['method']}`"
        return f"a call to `{node['method']}`"
    return f"a {node.get('kind', 'value')} expression"


# ---------------------------------------------------------------- linker

def _link(program: Program, components: list[dict]) -> None:
    provider_of: dict[str, str] = {}
    lines = {comp["name"]: decl.line for comp, decl in zip(components, program.components)}
    for comp in components:
        for key in comp["provides"]:
            if key in provider_of:
                raise RevlError(
                    program.filename, lines[comp["name"]],
                    f"provision conflict: key `{key}` is provided by both "
                    f"{provider_of[key]} and {comp['name']} (G2)",
                )
            provider_of[key] = comp["name"]

    # edges: provider -> consumer along each resolvable key
    graph: dict[str, list[str]] = {comp["name"]: [] for comp in components}
    for comp in components:
        for key in comp["requires"]:
            provider = provider_of.get(key)
            if provider is not None and provider != comp["name"]:
                graph[provider].append(comp["name"])
            elif provider == comp["name"]:
                raise RevlError(program.filename, lines[comp["name"]],
                                f"component {comp['name']} requires a key it provides itself (`{key}`) (G3)")

    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(name: str):
        state[name] = 1
        stack.append(name)
        for succ in graph[name]:
            if state.get(succ) == 1:
                cycle = stack[stack.index(succ):] + [succ]
                raise RevlError(program.filename, lines[succ],
                                "dependency cycle: " + " -> ".join(cycle) + " (G3)")
            if state.get(succ, 0) == 0:
                visit(succ)
        stack.pop()
        state[name] = 2

    for comp in components:
        if state.get(comp["name"], 0) == 0:
            visit(comp["name"])

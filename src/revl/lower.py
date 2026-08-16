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

import keyword

from .errors import RevlError
from .parser import (
    AwaitStmt,
    ComponentDecl,
    EffectStmt,
    EmitStmt,
    Interp,
    InterceptStmt,
    IsolateStmt,
    LetEffect,
    Lit,
    Postfix,
    Program,
    ProvideStmt,
    ReturnStmt,
    ServiceDecl,
)

IR_VERSION = 1
IR_VERSION_V2 = 2  # emitted only when a compiled component uses realms/interception

# the default shared realm (paper Def. 28: an unisolated key resolves to
# its own realm); rendered as "shared" in diagnostics
SHARED_REALM = ""

# A3: identifiers that must never appear verbatim in emitted code on either
# host. Python keywords come from the keyword module; the rest is a curated
# union of TS reserved words and backend-adapter names.
_HOST_RESERVED = {
    "ctx", "config", "frame", "fiber", "self",
    "function", "var", "let", "const", "new", "class", "this", "typeof",
    "delete", "in", "of", "instanceof", "void", "export", "default",
    "require", "module", "exports", "import", "yield", "async", "await",
    "true", "false", "null", "undefined", "NaN",
}


def _safe_name(name: str, taken: set[str]) -> str:
    candidate = name
    while (
        candidate in _HOST_RESERVED
        or keyword.iskeyword(candidate)
        or keyword.issoftkeyword(candidate)
        or candidate in taken
    ):
        candidate += "_"
    return candidate


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
        self.locals: dict[str, str] = {}  # surface name -> host-safe IR name (A3)
        self.params: dict[str, str] = {}
        self._taken: set[str] = set()

    def bind_local(self, name: str, line: int) -> str:
        if name in self.locals or name in self.requires or name in self.params:
            raise RevlError(self.filename, line, f"name `{name}` is already bound in {self.component.name}")
        safe = _safe_name(name, self._taken)
        self._taken.add(safe)
        self.locals[name] = safe
        return safe

    def bind_params(self, names: list[str], line: int) -> dict[str, str]:
        params: dict[str, str] = {}
        taken = set(self._taken)
        for name in names:
            if name in params:
                raise RevlError(self.filename, line, f"duplicate parameter `{name}`")
            safe = _safe_name(name, taken)
            taken.add(safe)
            params[name] = safe
        return params


def check_and_lower(program: Program, ambient: dict | None = None) -> dict:
    """Check and lower a program, optionally against an *ambient* composition
    (a running manifest, DESIGN §4's runtime-admission gate): ambient services
    are in scope without redeclaration, and G2/G3 are checked over the union
    of ambient and newly compiled components.

    `ambient`: {"services": <v1 services table>, "components": [<manifest
    component entries>]} — see compile_files for how it is derived.
    """
    ambient = ambient or {}
    ambient_services = {
        name: _service_from_ir(name, spec)
        for name, spec in (ambient.get("services") or {}).items()
    }

    services: dict[str, ServiceDecl] = {}
    for svc in program.services:
        if svc.name in services:
            raise RevlError(program.filename, svc.line, f"duplicate service `{svc.name}`")
        prior = ambient_services.get(svc.name)
        if prior is not None and not _service_equal(svc, prior):
            raise RevlError(
                program.filename, svc.line,
                f"service `{svc.name}` differs from the running manifest",
                hint="an admitted component must agree with the composition's "
                     "interface for the key it touches (interface drift, DESIGN §6.6)",
            )
        services[svc.name] = svc
    for name, svc in ambient_services.items():
        services.setdefault(name, svc)

    components = []
    seen = set()
    for comp in program.components:
        if comp.name in seen:
            raise RevlError(program.filename, comp.line, f"duplicate component `{comp.name}`")
        seen.add(comp.name)
        components.append(_lower_component(comp, services, program.filename))

    manifest = _link(program, components, ambient.get("components") or [])

    uses_v2 = any(comp.get("isolate") or comp.get("intercept") for comp in components)

    return {
        "ir_version": IR_VERSION_V2 if uses_v2 else IR_VERSION,
        "services": {
            name: {
                "methods": {
                    m.name: {
                        "params": [{"name": p, "type": t} for p, t in m.params],
                        "returns": m.returns,
                        "emission": m.emission,
                    }
                    for m in svc.methods.values()
                }
            }
            for name, svc in services.items()
        },
        "components": components,
        "manifest": manifest,
    }


def _service_from_ir(name: str, spec: dict) -> ServiceDecl:
    """Rebuild a checkable ServiceDecl from a v1 IR services entry."""
    from .parser import MethodDecl

    methods = {}
    for mname, mspec in (spec.get("methods") or {}).items():
        params = [(p.get("name"), p.get("type")) for p in mspec.get("params") or []]
        methods[mname] = MethodDecl(mname, params, mspec.get("returns"), bool(mspec.get("emission")), 0)
    return ServiceDecl(name, methods, 0)


def _service_equal(a: ServiceDecl, b: ServiceDecl) -> bool:
    def shape(svc: ServiceDecl):
        return {
            m.name: (tuple(m.params), m.returns, m.emission)
            for m in svc.methods.values()
        }

    return shape(a) == shape(b)


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
    isolate: dict[str, str] = {}
    intercept: dict[str, dict] = {}
    action_seen = False
    for stmt in comp.body:
        if isinstance(stmt, (IsolateStmt, InterceptStmt)):
            # prelude rule: realm/metadata declarations derive the resolution
            # context (Def. 27/29) and must precede every dependency access
            if action_seen:
                kw = "isolate" if isinstance(stmt, IsolateStmt) else "intercept"
                raise RevlError(
                    filename, stmt.line,
                    f"`{kw}` must precede every effect, emit, await, and provide statement",
                    hint="realm and metadata declarations derive the resolution context "
                         "before any dependency access (prelude rule, docs/design-v2-realms.md)",
                )
            if isinstance(stmt, IsolateStmt):
                if stmt.key not in env.requires and stmt.key not in provides:
                    raise RevlError(
                        filename, stmt.line,
                        f"`{stmt.key}` is not a declared requirement or provision of {comp.name}",
                        hint="`isolate` targets a key from the component header (G1)",
                    )
                if stmt.key in isolate:
                    raise RevlError(filename, stmt.line,
                                    f"key `{stmt.key}` is isolated twice in {comp.name}")
                isolate[stmt.key] = stmt.realm
            else:
                if stmt.key in provides and stmt.key not in env.requires:
                    raise RevlError(
                        filename, stmt.line,
                        f"`intercept` applies to required keys only — `{stmt.key}` is a provision",
                        hint="interception is the component-declared metadata d(k) of Def. 30, "
                             "whose domain is the dependency set; providers receive metadata "
                             "from their consumers' declarations",
                    )
                if stmt.key not in env.requires:
                    raise RevlError(
                        filename, stmt.line,
                        f"`{stmt.key}` is not a declared requirement of {comp.name}",
                        hint="`intercept` targets a key from the `requires` clause (G1)",
                    )
                if stmt.key in intercept:
                    raise RevlError(filename, stmt.line,
                                    f"key `{stmt.key}` is intercepted twice in {comp.name}")
                intercept[stmt.key] = stmt.metadata
            continue
        action_seen = True
        if isinstance(stmt, (LetEffect, EffectStmt)) and provide_seen_line is not None:
            raise RevlError(
                filename, stmt.line,
                "acquisition after `provide` — an effect acquired after a provision "
                "would be reverted while dependents can still call the service",
                hint="move acquisitions above the `provide` block (linker rule A2)",
            )
        if isinstance(stmt, LetEffect):
            acquire = _lower_expr(stmt.acquire, env, mode="setup")
            safe = env.bind_local(stmt.bind, stmt.line)
            undo = _lower_expr(stmt.undo, env, mode="undo")
            body.append({"step": "let-effect", "bind": safe, "acquire": acquire, "undo": undo})
        elif isinstance(stmt, EffectStmt):
            body.append({
                "step": "effect",
                "acquire": _lower_expr(stmt.acquire, env, mode="setup"),
                "undo": _lower_expr(stmt.undo, env, mode="undo"),
            })
        elif isinstance(stmt, EmitStmt):
            body.append(_lower_emit_step(stmt, env))
        elif isinstance(stmt, AwaitStmt):
            body.append({"step": "await", "expr": _lower_expr(stmt.expr, env, mode="setup")})
        elif isinstance(stmt, ProvideStmt):
            provide_seen_line = stmt.line
            body.append(_lower_provide(stmt, provides, provided_keys, env))
        else:  # pragma: no cover — grammar prevents it
            raise RevlError(filename, stmt.line, "unexpected statement in component body")

    lowered = {
        "name": comp.name,
        "source": comp.source or filename,
        "config": [{"name": f.name, "type": f.type, "default": f.default} for f in comp.config],
        "requires": dict(env.requires),
        "provides": provides,
        "body": body,
    }
    # v2 fields appear only when used, so v1 documents stay byte-identical
    if isolate:
        lowered["isolate"] = isolate
    if intercept:
        lowered["intercept"] = intercept
    return lowered


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
        env.params = env.bind_params(method.params, method.line)
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
                mbody.append(_lower_emit_step(mstmt, env))
            elif isinstance(mstmt, ReturnStmt):
                mbody.append({"step": "return", "expr": _lower_expr(mstmt.expr, env, mode="setup")})
                returned = True
            else:  # pragma: no cover
                raise RevlError(filename, mstmt.line, "unexpected statement in method body")
        safe_params = [env.params[p] for p in method.params]
        env.params = saved
        methods.append({"name": method.name, "params": safe_params, "body": mbody})

    missing = set(svc.methods) - implemented
    if missing:
        name = sorted(missing)[0]
        raise RevlError(filename, stmt.line,
                        f"provision `{stmt.key}` is missing method `{name}` declared by service {svc.name}")

    return {"step": "provide", "name": stmt.key, "service": svc.name, "methods": methods}


# ---------------------------------------------------------------- expressions

def _lower_emit_step(stmt: EmitStmt, env: Env) -> dict:
    node = _lower_expr(stmt.expr, env, mode="emit")
    if not _is_emission_call(node, env):
        desc = _node_desc(node)
        raise RevlError(env.filename, stmt.line,
                        f"`emit` on {desc}, which is not declared `emission`",
                        hint="only calls to `emission` service operations cross the boundary; "
                             "drop the `emit` marker (G4)")
    step = {"step": "emit", "expr": node}
    if stmt.compensate is not None:
        # compensation is teardown-position: emissions are permitted bare (A5)
        step["compensate"] = _lower_expr(stmt.compensate, env, mode="undo")
    return step


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
                template.append(value.replace("$", "$$"))  # A4: literal dollars
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
        node = {"kind": "name", "id": env.params.get(head) or env.locals[head]}
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

def _link(program: Program, components: list[dict], ambient_components: list[dict]) -> dict:
    """G2/G3 over the union of ambient (running) and new components, and the
    composition manifest (cordisc-compatible schema: components with
    name/file/inject/provides, plus loadOrder)."""
    lines = {comp["name"]: decl.line for comp, decl in zip(components, program.components)}

    entries: list[dict] = []
    for amb in ambient_components:
        entry = {
            "name": amb.get("name"),
            "file": amb.get("file", ""),
            "inject": list(amb.get("inject") or []),
            "provides": list(amb.get("provides") or []),
        }
        if amb.get("isolate"):
            entry["isolate"] = dict(amb["isolate"])
        if amb.get("intercept"):
            entry["intercept"] = dict(amb["intercept"])
        entries.append(entry)
    for comp in components:
        entry = {
            "name": comp["name"],
            "file": comp.get("source", ""),
            "inject": sorted(comp["requires"]),
            "provides": sorted(comp["provides"]),
        }
        if comp.get("isolate"):
            entry["isolate"] = dict(comp["isolate"])
        if comp.get("intercept"):
            entry["intercept"] = dict(comp["intercept"])
        entries.append(entry)

    def _line(name: str) -> int:
        return lines.get(name, 1)

    def _realm(entry: dict, key: str) -> str:
        return (entry.get("isolate") or {}).get(key, SHARED_REALM)

    # v2: provision disjointness is per-(key, realm) — same key in different
    # realms is the multi-tenancy feature, same realm is the conflict. The
    # realm is named only when it isn't the shared one, so v1 diagnostics
    # are unchanged.
    provider_of: dict[tuple[str, str], str] = {}
    for entry in entries:
        for key in entry["provides"]:
            realm = _realm(entry, key)
            where = "" if realm == SHARED_REALM else f" in realm `{realm}`"
            if (key, realm) in provider_of:
                raise RevlError(
                    program.filename, _line(entry["name"]),
                    f"provision conflict: key `{key}`{where} is provided "
                    f"by both {provider_of[(key, realm)]} and {entry['name']} (G2)",
                )
            provider_of[(key, realm)] = entry["name"]

    # edges: provider -> consumer where the consumer's realm for a key
    # matches the provider's — realm separation legitimately breaks cycles
    graph: dict[str, list[str]] = {entry["name"]: [] for entry in entries}
    indegree: dict[str, int] = {entry["name"]: 0 for entry in entries}
    for entry in entries:
        for key in entry["inject"]:
            provider = provider_of.get((key, _realm(entry, key)))
            if provider == entry["name"]:
                raise RevlError(program.filename, _line(entry["name"]),
                                f"component {entry['name']} requires a key it provides itself (`{key}`) (G3)")
            if provider is not None:
                graph[provider].append(entry["name"])
                indegree[entry["name"]] += 1

    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(name: str):
        state[name] = 1
        stack.append(name)
        for succ in graph[name]:
            if state.get(succ) == 1:
                cycle = stack[stack.index(succ):] + [succ]
                raise RevlError(program.filename, _line(succ),
                                "dependency cycle: " + " -> ".join(cycle) + " (G3)")
            if state.get(succ, 0) == 0:
                visit(succ)
        stack.pop()
        state[name] = 2

    for entry in entries:
        if state.get(entry["name"], 0) == 0:
            visit(entry["name"])

    # providers-first load order (Kahn, stable in entry order)
    order: list[str] = []
    ready = [e["name"] for e in entries if indegree[e["name"]] == 0]
    while ready:
        name = ready.pop(0)
        order.append(name)
        for succ in graph[name]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                ready.append(succ)

    return {"components": entries, "loadOrder": order}

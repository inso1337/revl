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

from . import holes
from .errors import RevlError
from .why import CHAIN, SET, TraceStep, WhyTrace
from .typecheck import (
    CASES_KEY,
    FNS_KEY,
    _SIZED_HEADS,
    check_ast,
    collect_tparams,
    render_type,
    mark_tparams,
    check_type_wellformed,
    compatible,
    format_type,
    host_check,
    infer_ast,
    infer_ir,
    mismatch,
    pin_hole,
    null_error,
    parse_type,
)
from .parser import (
    AssertStmt,
    AssignStmt,
    AwaitStmt,
    CallStmt,
    ComponentDecl,
    EffectStmt,
    EmitExpr,
    EmitStmt,
    ExternDecl,
    FailStmt,
    ExprArrow,
    ExprBin,
    ExprCall,
    ExprField,
    ExprHole,
    ExprIf,
    ExprIndex,
    ExprList,
    ExprLit,
    ExprMatch,
    ExprOptCall,
    ExprOptField,
    ExprRecord,
    ExprStmt,
    ExprUn,
    ExprVar,
    FnDecl,
    ForStmt,
    IfStmt,
    Interp,
    InterceptStmt,
    IsolateStmt,
    LetEffect,
    LetPatternStmt,
    LetStmt,
    LoadStmt,
    ListPattern,
    Lit,
    Postfix,
    Program,
    ProvideStmt,
    RecordPattern,
    ResidueStmt,
    ReturnStmt,
    ServiceDecl,
    TestDecl,
    TypeDecl,
    UnloadStmt,
    WhileStmt,
)

IR_VERSION = 1

# finding 6: the specified stdlib surface (docs/stdlib-2.0.md). Method calls
# on values must name one of these (arity-checked); everything else is a
# compile error — never a verbatim pass-through to whatever the host object
# happens to have. Names are chosen to be collision-free with the v1 host
# stub objects (open/close/query/execute/new/get/insert/remove/drop).
_BUILTIN_METHODS = {
    "length": 0, "push": 1, "slice": 2, "charAt": 1,
    "charCodeAt": 1, "indexOf": 1, "concat": 1,
    "split": 1, "join": 1, "repeat": 1,
}


def _is_host_valued(expr, scope) -> bool:
    """A receiver holding a HOST object (Map.new(), Pool.open(...), or a let
    bound to one): its methods belong to the host stub, not the stdlib
    table, and stay verbatim."""
    from .parser import ExprCall as _C, ExprField as _F, ExprVar as _V

    if isinstance(expr, _V):
        return scope.get(expr.name) == "host"
    if isinstance(expr, _C) and isinstance(expr.callee, _F)             and isinstance(expr.callee.target, _V):
        return expr.callee.target.name in _HOST_CALLABLES
    return False
IR_VERSION_V2 = 2  # emitted only when a compiled component uses realms/interception
IR_VERSION_V3 = 3  # emitted when a program uses full-language features (fn/type)

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
    def __init__(self, component: ComponentDecl, services: dict[str, ServiceDecl], filename: str,
                 types: dict | None = None):
        self.component = component
        self.services = services
        self.filename = filename
        self.types = types or {}
        # names whose call reaches an irreversible host effect (set by
        # check_and_lower once externs/fns are lowered)
        self.emitting_fns: set = set()
        # the same relation refined to *which* boundaries each one reaches:
        # name -> capability set (docs/capabilities.md)
        self.emitting_caps: dict[str, set] = {}
        # why-trace support for the above (why.py); None when unavailable,
        # in which case rejections carry no derivation but are unchanged
        self.emission_evidence: "_EmissionEvidence | None" = None
        # component-body type environment: safe-name -> type, plus the
        # "config.<field>" and "req.<local>" markers infer_ir resolves
        self.type_env: dict[str, str] = {}
        for cfg_field in component.config:
            self.type_env[f"config.{cfg_field.name}"] = cfg_field.type
        self.config_fields = {f.name for f in component.config}
        self.requires = dict()  # local -> service name
        for local, svc, line in component.requires:
            if svc not in services:
                raise RevlError(filename, line, f"unknown service `{svc}` in `requires` of {component.name}")
            if local in self.requires:
                raise RevlError(filename, line, f"duplicate requirement name `{local}` in {component.name}")
            self.requires[local] = svc
            self.type_env[f"req.{local}"] = svc
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


# ---------------------------------------------------------------------------
# v2.0: type declarations & pure functions (docs/syntax-2.0.md §2–§3)
# ---------------------------------------------------------------------------

# builtin (non-record) type heads: destructuring a value of one of these with
# a record/list pattern is a type error, not a host pass-through
_BUILTIN_NONRECORD = {"Str", "Int", "Bool", "Float", "Bytes", "Unit",
                      "List", "Map", "Opt", "Result"}

# host roots a pure fn may call without an explicit binding (DESIGN §7 builtins)
_HOST_CALLABLES = {"Map", "Pool", "Job"}
# Opt/Result constructors, recognized so `Some(x)`/`Ok(r)` resolve (syntax-2.0 §2)
_BUILTIN_CONSTRUCTORS = {"Some", "None", "Ok", "Err"}


# Builtin heads a `type X = Y` right-hand side may name. `Any`/`Never` are the
# type algebra's wildcards; the rest are the declared builtin surface.
_ALIASABLE_BUILTINS = _BUILTIN_NONRECORD | {"Any", "Never"}


def _alias_target(decl: TypeDecl, declared: set[str]) -> str | None:
    """The type `type X = Y` aliases, or None when the decl is a real variant.

    `type X = Y` is TypeScript's alias spelling, and syntax-2.0's governing
    principle is that no construct may exist in both languages with silently
    different meaning. So the split follows TypeScript's own:

    - Where TypeScript *compiles* it — `Y` names an existing type — revl means
      what TypeScript means: a transparent alias. `type Sku = Str` used to
      declare a one-case variant whose case was named `Str`, which made the
      author's own alias unusable (`f("abc")` was refused for a `Sku`
      parameter) while `return Str` was accepted as a case constructor.
    - Where TypeScript *rejects* it — `Y` is undeclared (TS2304) — revl is free
      to mean something else, because there is no shared meaning to diverge
      from. `type Status = Pending` keeps its one-case-variant reading, which
      is how an opaque nominal is spelled today.

    A payload makes it a newtype (`type W = Wrap(Int)`), never an alias.
    """
    if decl.fields or len(decl.cases) != 1:
        return None
    case = decl.cases[0]
    if case.payload is not None:
        return None
    head, args = parse_type(case.name)
    if args:
        return case.name  # a type application; the parser only builds these here
    if head in _ALIASABLE_BUILTINS or head in declared:
        return case.name
    return None


def _resolve_type_aliases(program: Program, filename: str) -> None:
    """Erase transparent type aliases from the program, in place.

    Aliases are substituted at every declaration site and their declarations
    dropped, so nothing downstream — the type table, the checker, the IR, the
    backends — ever sees the alias name. That is what `transparent` means, and
    it is the reading TypeScript has: `Sku` and `Str` are interchangeable in
    both directions. A *nominal* alias would be a distinct type needing
    construction syntax revl does not have, and would re-commit the very sin
    this fixes (both languages compiling `type X = Y` with different meanings).
    """
    declared = {d.name for d in program.type_decls}
    aliases: dict[str, TypeDecl] = {}
    for decl in program.type_decls:
        target = _alias_target(decl, declared)
        if target is None:
            continue
        if decl.params:
            raise RevlError(
                filename, decl.line,
                f"type alias `{decl.name}` cannot declare type parameters",
                hint="an alias is substituted verbatim, so it has nothing to "
                     "instantiate — drop the parameters, or declare a variant "
                     "with named cases (syntax-2.0 §2)",
            )
        # an alias is a declaration, so its right-hand side is checked here
        # rather than only where the alias happens to be used
        check_type_wellformed(filename, decl.line, target)
        aliases[decl.name] = decl
    if not aliases:
        return

    def expand(type_name: str, stack: tuple) -> str:
        head, args = parse_type(type_name)
        if args:
            return format_type(head, [expand(a, stack) for a in args])
        if head not in aliases:
            return head
        if head in stack:
            chain = " -> ".join(stack[stack.index(head):] + (head,))
            raise RevlError(
                filename, aliases[head].line,
                f"type alias cycle: {chain}",
                hint="an alias is substituted verbatim, so a cycle has no "
                     "expansion — break it, or declare one of them as a variant",
            )
        return expand(_alias_target(aliases[head], declared), stack + (head,))

    resolved = {name: expand(_alias_target(decl, declared), (name,))
                for name, decl in aliases.items()}

    def subst(type_name):
        if not type_name:
            return type_name
        head, args = parse_type(type_name)
        if args:
            return format_type(head, [subst(a) for a in args])
        return resolved.get(head, head)

    # every declaration site that carries a type annotation; kept in step with
    # `_validate_declared_types` below, which enumerates the same surface
    for fn in program.fn_decls:
        for p in fn.params:
            p.type = subst(p.type)
        fn.returns = subst(fn.returns)
    for ext in program.externs:
        for p in ext.params:
            p.type = subst(p.type)
        ext.returns = subst(ext.returns)
    for svc in program.services:
        for m in svc.methods.values():
            m.params = [(pname, subst(ptype)) for pname, ptype in m.params]
            m.returns = subst(m.returns)
    for decl in program.type_decls:
        for fld in decl.fields:
            fld.type = subst(fld.type)
        for case in decl.cases:
            case.payload = subst(case.payload)
    for comp in program.components:
        for cfg in comp.config:
            cfg.type = subst(cfg.type)
        for stmt in comp.body:
            if not isinstance(stmt, ProvideStmt):
                continue
            for method in stmt.methods:
                method.param_types = [subst(t) for t in method.param_types]
                method.returns = subst(method.returns)
                _subst_body_annotations(method.body, subst)
    for fn in program.fn_decls:
        _subst_body_annotations(fn.body, subst)
    for test in program.tests:
        _subst_body_annotations(test.body, subst)

    program.type_decls = [d for d in program.type_decls if d.name not in aliases]


def _subst_body_annotations(stmts: list, subst) -> None:
    """Expand type aliases in the annotations a *body* can carry.

    `let g: Handler = …` and `(v: Handler) => …` are type annotations like any
    other, but they live inside statements rather than at a declaration site,
    so the declaration sweep above cannot reach them — and an alias that
    survived here would reach the checker as an undeclared type name."""
    def walk_expr(expr) -> None:
        if isinstance(expr, ExprArrow):
            expr.param_types = [subst(t) for t in expr.param_types]
            walk_expr(expr.body)
        elif isinstance(expr, ExprBin):
            walk_expr(expr.left)
            walk_expr(expr.right)
        elif isinstance(expr, ExprUn):
            walk_expr(expr.operand)
        elif isinstance(expr, ExprCall):
            walk_expr(expr.callee)
            for arg in expr.args:
                walk_expr(arg)
        elif isinstance(expr, (ExprField, ExprOptField)):
            walk_expr(expr.target)
        elif isinstance(expr, ExprOptCall):
            walk_expr(expr.target)
            for arg in expr.args:
                walk_expr(arg)
        elif isinstance(expr, ExprIndex):
            walk_expr(expr.target)
            walk_expr(expr.index)
        elif isinstance(expr, ExprIf):
            walk_expr(expr.cond)
            walk_expr(expr.then)
            walk_expr(expr.otherwise)
        elif isinstance(expr, ExprRecord):
            for _, value in expr.fields:
                walk_expr(value)
        elif isinstance(expr, ExprList):
            for item in expr.items:
                walk_expr(item)
        elif isinstance(expr, ExprMatch):
            walk_expr(expr.scrutinee)
            for _, _, body in expr.arms:
                walk_expr(body)
        elif isinstance(expr, Interp):
            for kind, part in expr.parts:
                if kind == "expr":
                    walk_expr(part)

    def walk_stmt(stmt) -> None:
        if isinstance(stmt, LetStmt):
            stmt.type = subst(stmt.type)
            walk_expr(stmt.value)
        elif isinstance(stmt, (LetPatternStmt, AssignStmt)):
            walk_expr(stmt.value)
        elif isinstance(stmt, (ExprStmt, AssertStmt, FailStmt, AwaitStmt)):
            walk_expr(stmt.expr)
        elif isinstance(stmt, ReturnStmt):
            if stmt.expr is not None:
                walk_expr(stmt.expr)
        elif isinstance(stmt, IfStmt):
            walk_expr(stmt.cond)
            for child in list(stmt.then) + list(stmt.otherwise or []):
                walk_stmt(child)
        elif isinstance(stmt, WhileStmt):
            walk_expr(stmt.cond)
            for child in stmt.body:
                walk_stmt(child)
        elif isinstance(stmt, ForStmt):
            walk_expr(stmt.iterable)
            for child in stmt.body:
                walk_stmt(child)

    for stmt in stmts:
        walk_stmt(stmt)


def _validate_declared_types(program: Program, filename: str) -> None:
    """Reject malformed type annotations (bare builtin generics like `Opt`,
    `List[]`) at every declaration site before checking begins — otherwise a
    zero-arg generic reaches the type algebra and crashes it."""
    for fn in program.fn_decls:
        for p in fn.params:
            check_type_wellformed(filename, p.line, p.type)
        check_type_wellformed(filename, fn.line, fn.returns)
    for ext in program.externs:
        for p in ext.params:
            check_type_wellformed(filename, p.line, p.type)
        check_type_wellformed(filename, ext.line, ext.returns)
    for svc in program.services:
        for m in svc.methods.values():
            for _, ptype in m.params:
                check_type_wellformed(filename, m.line, ptype)
            check_type_wellformed(filename, m.line, m.returns)
    for decl in program.type_decls:
        for field in decl.fields:
            check_type_wellformed(filename, field.line, field.type)
        for case in decl.cases:
            check_type_wellformed(filename, case.line, case.payload)
    for comp in program.components:
        for cfg in comp.config:
            check_type_wellformed(filename, cfg.line, cfg.type)


def _lower_type_decls(program: Program, filename: str) -> dict:
    types: dict[str, dict] = {}
    for decl in program.type_decls:
        if decl.name in types:
            raise RevlError(filename, decl.line, f"duplicate type `{decl.name}`")
        if decl.fields:
            fields: dict[str, str] = {}
            for field in decl.fields:
                if field.name in fields:
                    raise RevlError(filename, field.line,
                                    f"duplicate field `{field.name}` in record `{decl.name}`")
                fields[field.name] = field.type
            types[decl.name] = {"params": decl.params, "kind": "record", "fields": fields}
        else:
            cases: list[dict] = []
            seen: set[str] = set()
            for case in decl.cases:
                if case.name in seen:
                    raise RevlError(filename, case.line,
                                    f"duplicate case `{case.name}` in type `{decl.name}`")
                seen.add(case.name)
                cases.append({"name": case.name, "payload": case.payload})
            types[decl.name] = {"params": decl.params, "kind": "variant", "cases": cases}
    return types


def _fn_call_graph(program: Program) -> dict[str, set[str]]:
    """Direct-call graph over the file's functions.

    Host roots and constructors are not function declarations, so they are
    ignored: only `ExprVar` callees whose name is another `fn` are edges.
    """
    fn_names = {fn.name for fn in program.fn_decls}
    graph = {fn.name: set() for fn in program.fn_decls}

    def collect_expr(expr) -> None:
        if isinstance(expr, ExprCall):
            if isinstance(expr.callee, ExprVar) and expr.callee.name in fn_names:
                graph[decl.name].add(expr.callee.name)
            collect_expr(expr.callee)
            for arg in expr.args:
                collect_expr(arg)
        elif isinstance(expr, ExprBin):
            collect_expr(expr.left)
            collect_expr(expr.right)
        elif isinstance(expr, ExprUn):
            collect_expr(expr.operand)
        elif isinstance(expr, ExprField):
            collect_expr(expr.target)
        elif isinstance(expr, ExprIndex):
            collect_expr(expr.target)
            collect_expr(expr.index)
        elif isinstance(expr, ExprIf):
            collect_expr(expr.cond)
            collect_expr(expr.then)
            collect_expr(expr.otherwise)
        elif isinstance(expr, ExprRecord):
            for _, value in expr.fields:
                collect_expr(value)
        elif isinstance(expr, ExprList):
            for item in expr.items:
                collect_expr(item)
        elif isinstance(expr, ExprArrow):
            collect_expr(expr.body)

    def collect_stmt(stmt) -> None:
        if isinstance(stmt, LetStmt):
            collect_expr(stmt.value)
        elif isinstance(stmt, LetPatternStmt):
            collect_expr(stmt.value)
        elif isinstance(stmt, AssignStmt):
            collect_expr(stmt.value)
        elif isinstance(stmt, ReturnStmt):
            if stmt.expr is not None:
                collect_expr(stmt.expr)
        elif isinstance(stmt, IfStmt):
            collect_expr(stmt.cond)
            for branch in (stmt.then, stmt.otherwise or []):
                for child in branch:
                    collect_stmt(child)
        elif isinstance(stmt, WhileStmt):
            collect_expr(stmt.cond)
            for child in stmt.body:
                collect_stmt(child)
        elif isinstance(stmt, ForStmt):
            collect_expr(stmt.iterable)
            for child in stmt.body:
                collect_stmt(child)
        elif isinstance(stmt, (ExprStmt, AssertStmt)):
            collect_expr(stmt.expr)

    for decl in program.fn_decls:
        for stmt in decl.body:
            collect_stmt(stmt)
    return graph


def _reaches_self(start: str, graph: dict[str, set[str]]) -> bool:
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        for succ in graph.get(node, ()):
            if succ == start:
                return True
            if succ not in seen:
                seen.add(succ)
                stack.append(succ)
    return False


def _check_verified_totality(program: Program, filename: str) -> None:
    """Totality gate for `verified fn` (syntax-2.0 §7).

    This first cut is deliberately conservative: a verified function may not
    participate in any cycle in the direct-call graph, because the checker
    cannot currently prove structural descent. Loop bodies are traversed so
    recursion through `while`/`for` is still caught.
    """
    verified = [fn for fn in program.fn_decls if fn.verified]
    if not verified:
        return
    graph = _fn_call_graph(program)
    for decl in verified:
        if _reaches_self(decl.name, graph):
            raise RevlError(
                filename,
                decl.line,
                f"verified fn `{decl.name}` is not total: it participates in "
                "direct or mutual recursion (verified totality, syntax-2.0 §7)",
                hint="use structural recursion on a structurally smaller value, "
                     "or a syntactically bounded loop",
            )


def _has_return(stmts) -> bool:
    """True when a `return` appears anywhere in this statement tree."""
    for stmt in stmts:
        if isinstance(stmt, ReturnStmt):
            return True
        if isinstance(stmt, IfStmt):
            if _has_return(stmt.then) or _has_return(stmt.otherwise or []):
                return True
        elif isinstance(stmt, (WhileStmt, ForStmt)):
            if _has_return(stmt.body):
                return True
    return False


def _definitely_returns(stmts) -> bool:
    """True when control cannot reach the end of `stmts` without returning.

    Deliberately the same conservative rule Java and Rust apply, so a body this
    accepts is a body those tiers accept:

    - a `return` terminates;
    - an `if` terminates only when it has an `else` *and* both arms terminate
      (a bare `if` may be skipped);
    - `for` and a conditional `while` may run zero times, so neither terminates;
    - `while (true)` diverges (there is no `break` in the grammar), so nothing
      after it is reachable — Java and Rust agree, and no tier needs a value.
    """
    for stmt in stmts:
        if isinstance(stmt, ReturnStmt):
            return True
        if isinstance(stmt, IfStmt):
            if (stmt.otherwise is not None
                    and _definitely_returns(stmt.then)
                    and _definitely_returns(stmt.otherwise)):
                return True
        elif isinstance(stmt, WhileStmt):
            if isinstance(stmt.cond, ExprLit) and stmt.cond.value is True:
                return True
    return False


def _check_returns_on_every_path(decl: FnDecl, filename: str) -> None:
    """A fn with a declared return type must return on every path.

    Falling off the end is a portability trap, not a nicety: Python (the
    reference backend) silently yields `None`, TypeScript yields `undefined`,
    while Rust refuses with E0308 and Java with "missing return statement". A
    program the checker accepts must compile on every tier, so the strict
    reading is the frontend's.
    """
    if not decl.returns or _definitely_returns(decl.body):
        return
    last_line = getattr(decl.body[-1], "line", decl.line) if decl.body else decl.line
    if not _has_return(decl.body):
        raise RevlError(
            filename, decl.line,
            f"`{decl.name}` is declared to return `{decl.returns}` but its body "
            f"never returns a value",
            hint=f"end the body with `return <{decl.returns}>`, or drop the "
                 f"`-> {decl.returns}` annotation if the fn produces nothing — "
                 "revl has no implicit result (rust E0308, java \"missing return "
                 "statement\")",
            code="T1", category="type-mismatch",
            expected=decl.returns, actual=None,
        )
    raise RevlError(
        filename, last_line,
        f"`{decl.name}` is declared to return `{decl.returns}` but control can "
        f"reach the end of its body without a `return`",
        hint="every path must return: give the trailing `if` an `else` that "
             "returns, or add a final `return` after it — a `for`/`while` may "
             "run zero times and never counts (rust E0308, java \"missing "
             "return statement\")",
        code="T1", category="type-mismatch",
        expected=decl.returns, actual=None,
    )


def _lower_fns(program: Program, filename: str, types: dict | None = None) -> list:
    _check_verified_totality(program, filename)
    types = types or {}
    default_callables = (
        _HOST_CALLABLES
        | _BUILTIN_CONSTRUCTORS
        | {fn.name for fn in program.fn_decls}
        | {ext.name for ext in program.externs}
    )
    fns: list[dict] = []
    seen: set[str] = set()
    for decl in program.fn_decls:
        if decl.name in seen:
            raise RevlError(filename, decl.line, f"duplicate function `{decl.name}`")
        seen.add(decl.name)
        # the body sees the *marked* signature: this fn's own type parameters
        # are wildcards inside it (they are universally quantified there), while
        # a one-letter nominal type stays checked
        sig = (types.get(FNS_KEY) or {}).get(decl.name) or {}
        marked_params = sig.get("params") or [p.type for p in decl.params]
        marked_returns = sig.get("returns", decl.returns)
        scope: dict[str, bool] = {}
        type_env: dict[str, str] = {}
        for param, marked in zip(decl.params, marked_params):
            if param.name in scope:
                raise RevlError(filename, param.line,
                                f"duplicate parameter `{param.name}` in fn {decl.name}")
            scope[param.name] = False
            type_env[param.name] = marked
        module_callables = program.fn_scopes.get(id(decl), default_callables)
        callables = _HOST_CALLABLES | _BUILTIN_CONSTRUCTORS | set(module_callables) | {ext.name for ext in program.externs}
        alias_fns = program.fn_alias_scopes.get(id(decl), {})
        body: list[dict] = []
        for stmt in decl.body:
            _lower_pure_stmt(stmt, scope, callables, alias_fns, body, filename, type_env, types,
                             expected_return=marked_returns)
        _check_returns_on_every_path(decl, filename)
        entry = {
            "name": decl.name,
            "params": [{"name": p.name, "type": p.type} for p in decl.params],
            "returns": decl.returns,
            "public": decl.public,
            "body": body,
        }
        if decl.verified:
            entry["verified"] = True
        if decl.source:
            _retarget_holes(entry["body"], decl.source)
        fns.append(entry)
    return fns


def _signature_table(program: Program, types: dict | None = None) -> dict:
    """{name: {"params": [type...], "returns": type|None, "tparams": set}} for
    fns + externs.

    Each signature's implicit type parameters (single-uppercase names that are
    not declared types) are marked here, once, so the rest of the checker can
    tell a universally quantified `T` from a nominal type that merely has a
    one-letter name. Marked types never reach the IR — this table is the
    checker's view, and `_lower_fns`/`_lower_externs` emit the author's
    spelling."""
    declared = {name: spec for name, spec in (types or {}).items()
                if not name.startswith("__")}
    sigs: dict = {}
    for decl in list(program.fn_decls) + list(program.externs):
        raw_params = [p.type for p in decl.params]
        tparams = collect_tparams(raw_params + [decl.returns], declared)
        sigs[decl.name] = {
            "params": [mark_tparams(t, tparams) for t in raw_params],
            "returns": mark_tparams(decl.returns, tparams),
            "tparams": tparams,
        }
    return sigs


def _case_table(types: dict) -> dict:
    """ADT constructor table: case name -> {"adt", "payload"}. Builtins first;
    ambiguous user cases (same name in two ADTs) are dropped to stay silent."""
    cases: dict = {
        "Some": {"adt": "Opt[Any]", "payload": None},
        "None": {"adt": "Opt[Any]", "payload": None},
        "Ok": {"adt": "Result[Any, Any]", "payload": None},
        "Err": {"adt": "Result[Any, Any]", "payload": None},
    }
    ambiguous: set = set()
    for type_name, spec in types.items():
        if type_name.startswith("__") or spec.get("kind") != "variant":
            continue
        for case in spec.get("cases", []):
            name = case["name"]
            # `Some`/`None` are reserved for `Opt` (host-null representation);
            # a user ADT reusing them is ambiguous and dropped.
            if name in ("Some", "None"):
                ambiguous.add(name)
                cases.pop(name, None)
                continue
            # a user ADT may reuse `Ok`/`Err`: the user's declaration shadows
            # the built-in `Result` (the docs' own `type Outcome = Ok(Row) | …`
            # does exactly this).
            if name in ("Ok", "Err") and cases.get(name, {}).get("adt", "").startswith("Result"):
                cases[name] = {"adt": type_name, "payload": case["payload"]}
                continue
            if name in cases or name in ambiguous:
                ambiguous.add(name)
                cases.pop(name, None)
                continue
            cases[name] = {"adt": type_name, "payload": case["payload"]}
    return cases


def _arrow_param_types(expr) -> list:
    """An arrow's parameter types, one per parameter (None where unknown).

    The checker writes what it resolved back onto the AST node
    (`typecheck.py::_resolve_arrow`), so this is either the author's `(v: Int)`
    annotations, the types the expected function type supplied, or Nones when
    the arrow is still on the unchecked frontier."""
    written = list(getattr(expr, "param_types", None) or [])
    written += [None] * (len(expr.params) - len(written))
    return written[:len(expr.params)]


def _expr_static_type(expr, type_env: dict, types: dict) -> str | None:
    """Best-effort static type of a pure expression.

    match exhaustiveness is a best-effort check: when the scrutinee's type is
    not recoverable from the local type environment, lowering still proceeds
    (the Python emitter adds a runtime fallback for those cases).
    """
    # delegated to the bidirectional checker's inference (non-raising form)
    return infer_ast(expr, type_env, types)


def _type_arg(type_name: str | None, base: str) -> str | None:
    """Best-effort inner type of ``base[...]``."""
    if type_name and type_name.startswith(base + "["):
        return type_name[len(base) + 1 : -1]
    return None


def _is_sized_type(type_name: str | None) -> bool:
    return type_name in ("Str", "Bytes") or bool(type_name and type_name.startswith("List["))


def _variant_case_payload(types: dict, type_name: str | None, case_name: str) -> str | None:
    spec = types.get(type_name or "")
    if spec is None or spec.get("kind") != "variant":
        return None
    for case in spec.get("cases", []):
        if case["name"] == case_name:
            return case["payload"]
    return None


def _arm_payload_type(scrutinee_type: str | None, pattern: str, types: dict) -> str | None:
    """The payload type bound by a match arm. User variants come from the case
    table; built-in Opt/Result payloads come from the scrutinee's type args
    (`Opt[T]` -> Some binds T; `Result[T, E]` -> Ok binds T, Err binds E)."""
    user = _variant_case_payload(types, scrutinee_type, pattern)
    if user is not None:
        return user
    head, args = parse_type(scrutinee_type)
    if head == "Opt" and pattern == "Some" and args:
        return args[0]
    if head == "Result" and args and len(args) == 2:
        if pattern == "Ok":
            return args[0]
        if pattern == "Err":
            return args[1]
    return None


def _check_match_exhaustiveness(expr: ExprMatch, type_env: dict, types: dict, filename: str) -> None:
    type_name = _expr_static_type(expr.scrutinee, type_env, types)
    spec = types.get(type_name or "")
    if spec is None or spec.get("kind") != "variant":
        return
    # An arm naming something that is not a case of the scrutinee's ADT has no
    # meaning on any tier: Java emits a `case` label for a constant that does
    # not exist ("cannot find symbol") and the Rust emitter raises EmitError.
    # Python/TS silently never take the arm, so the divergence is a portability
    # bug rather than a compile error there — which is exactly what revl exists
    # to refuse.
    declared = [case["name"] for case in spec.get("cases", [])]
    for pattern, _, _ in expr.arms:
        if pattern != "_" and pattern not in declared:
            raise RevlError(
                filename, expr.line,
                f"`{pattern}` is not a case of `{type_name}` "
                f"(cases: {', '.join(f'`{c}`' for c in declared)})",
                hint="a match arm names one of the ADT's declared cases, or `_` for "
                     "a catch-all — a bare name is not a binding pattern "
                     "(syntax-2.0 §3.3); check the spelling or add the case to "
                     f"`type {type_name}`",
            )
    covered = {pattern for pattern, _, _ in expr.arms if pattern != "_"}
    if "_" in {pattern for pattern, _, _ in expr.arms}:
        return
    missing = [case["name"] for case in spec.get("cases", []) if case["name"] not in covered]
    if not missing:
        return
    if len(missing) == 1:
        rendered = f"`{missing[0]}`"
        plural = "case"
    else:
        rendered = ", ".join(f"`{name}`" for name in missing)
        plural = "cases"
    raise RevlError(filename, expr.line, f"non-exhaustive match: missing {plural} {rendered}")


def _mutable_free_vars(expr, scope: dict, bound: set[str] | None = None) -> set[str]:
    """Mutable `var` names referenced by ``expr`` and not shadowed by a
    lambda parameter.  Arrow literals snapshot these by value."""
    bound = set(bound or ())
    if isinstance(expr, ExprVar):
        if expr.name not in bound and scope.get(expr.name) is True:
            return {expr.name}
        return set()
    if isinstance(expr, ExprArrow):
        return _mutable_free_vars(expr.body, scope, bound | set(expr.params))
    if isinstance(expr, ExprBin):
        return _mutable_free_vars(expr.left, scope, bound) | _mutable_free_vars(expr.right, scope, bound)
    if isinstance(expr, ExprUn):
        return _mutable_free_vars(expr.operand, scope, bound)
    if isinstance(expr, ExprCall):
        found = _mutable_free_vars(expr.callee, scope, bound)
        for arg in expr.args:
            found |= _mutable_free_vars(arg, scope, bound)
        return found
    if isinstance(expr, ExprField):
        return _mutable_free_vars(expr.target, scope, bound)
    if isinstance(expr, ExprIndex):
        return _mutable_free_vars(expr.target, scope, bound) | _mutable_free_vars(expr.index, scope, bound)
    if isinstance(expr, ExprIf):
        return (
            _mutable_free_vars(expr.cond, scope, bound)
            | _mutable_free_vars(expr.then, scope, bound)
            | _mutable_free_vars(expr.otherwise, scope, bound)
        )
    if isinstance(expr, ExprRecord):
        found: set[str] = set()
        for _, value in expr.fields:
            found |= _mutable_free_vars(value, scope, bound)
        return found
    if isinstance(expr, ExprList):
        found = set()
        for item in expr.items:
            found |= _mutable_free_vars(item, scope, bound)
        return found
    if isinstance(expr, ExprMatch):
        found = _mutable_free_vars(expr.scrutinee, scope, bound)
        for _, bind, body in expr.arms:
            arm_bound = set(bound)
            if bind is not None:
                arm_bound.add(bind)
            found |= _mutable_free_vars(body, scope, arm_bound)
        return found
    return set()


class _LaxScope(dict):
    """Permissive scope for extern undo/compensate refs.

    Host blocks are verbatim text; their undo/compensate expressions are
    host-level names (e.g. `close(socket)`) and are not checked against revl
    function scope.
    """

    def __contains__(self, key) -> bool:
        return True


def _lower_extern_expr(expr, filename: str) -> dict:
    return _lower_pure_expr(expr, _LaxScope(), set(), {}, filename)


def _lower_externs(program: Program, filename: str) -> list:
    externs: list[dict] = []
    seen: set[str] = set()
    for decl in program.externs:
        if decl.name in seen:
            raise RevlError(filename, decl.line, f"duplicate extern `{decl.name}`")
        seen.add(decl.name)
        if decl.classification == "acquire" and decl.undo is None:
            raise RevlError(
                filename, decl.line,
                f"acquire extern `{decl.name}` must declare `undo` (G4)",
                hint="an `acquire` crosses into an observable effect and needs a teardown inverse",
            )
        if decl.classification == "pure" and (decl.undo is not None or decl.compensate is not None):
            raise RevlError(
                filename, decl.line,
                f"pure extern `{decl.name}` cannot declare `undo` or `compensate`",
                hint="`pure` means no observable effect, so there is nothing to invert or compensate",
            )
        if decl.classification == "emission" and decl.undo is not None:
            raise RevlError(
                filename, decl.line,
                f"emission extern `{decl.name}` cannot declare `undo`",
                hint="emissions are one-way boundary crossings; use `compensate` for a best-effort cleanup",
            )
        bodies: dict[str, str] = {}
        for body in decl.bodies:
            if body.backend in bodies:
                raise RevlError(filename, body.line,
                                f"duplicate @{body.backend} body for extern `{decl.name}`")
            bodies[body.backend] = body.text
        entry: dict = {
            "name": decl.name,
            "class": decl.classification,
            "params": [{"name": p.name, "type": p.type} for p in decl.params],
            "returns": decl.returns,
            "bodies": bodies,
        }
        if decl.undo is not None:
            entry["undo"] = _lower_extern_expr(decl.undo, filename)
        if decl.compensate is not None:
            entry["compensate"] = _lower_extern_expr(decl.compensate, filename)
        externs.append(entry)
    return externs


def _lower_tests(program: Program, filename: str, types: dict,
                 services: dict | None = None) -> list:
    """Lower `test` and `lifecycle test` blocks to IR v3 test units (§7/§7.1).

    `types` (the case/signature table) is threaded through so a `test` body is
    the same expression scope a `fn` body is: nullary user-ADT constructors
    resolve as values (`let s = FirstTime`) and statements are type-checked.
    """
    if not program.tests:
        return []
    callables = _HOST_CALLABLES | _BUILTIN_CONSTRUCTORS | {fn.name for fn in program.fn_decls}
    tests: list[dict] = []
    seen: set[str] = set()
    for decl in program.tests:
        if decl.name in seen:
            raise RevlError(filename, decl.line, f"duplicate test `{decl.name}`")
        seen.add(decl.name)
        if decl.lifecycle:
            tests.append({
                "name": decl.name,
                "lifecycle": True,
                "body": _lower_lifecycle_body(decl, program, services or {}, filename,
                                              callables, types),
            })
            continue
        scope: dict[str, bool] = {}
        type_env: dict[str, str] = {}
        body: list[dict] = []
        for stmt in decl.body:
            _lower_pure_stmt(stmt, scope, callables, {}, body, filename, type_env, types)
        tests.append({"name": decl.name, "body": body})
    return tests


# `fail at effect X` is sugar: it resolves to the index of the body step that
# binds X, so every backend and the runner see exactly one addressing scheme
# (a step index).  Index is the primitive because it is total — *every* body
# step has one, including `emit`, `provide`, `await` and unbound `effect`
# blocks — while a name only exists for `let effect NAME = …`.  The name form
# is the refactor-stable one to write, so both spellings are kept and the
# diagnostics always report the pair.
_FAULT_ASSERTS = (
    "failed", "no-residue", "inverses-lifo", "no-emissions", "siblings-unaffected",
)


def _fault_effect_index(body: list, name: str) -> int | None:
    """1-based index of the top-level `let-effect` step binding *name*."""
    for index, step in enumerate(body, 1):
        if step.get("step") == "let-effect" and step.get("bind") == name:
            return index
    return None


def _lower_fault_tests(program: Program, components: list, filename: str) -> list:
    """Lower `fault test` blocks to IR fault units (docs/fault-tests.md).

    Runs after component lowering: the injection point is validated against
    the *lowered* body, so `fail at step 4` is a compile error when the
    component only has three steps rather than a confusing runtime miss.
    """
    if not program.fault_tests:
        return []
    by_name = {component["name"]: component for component in components}
    units: list[dict] = []
    seen: set[str] = set()
    for decl in program.fault_tests:
        if decl.name in seen:
            raise RevlError(filename, decl.line, f"duplicate fault test `{decl.name}`")
        seen.add(decl.name)
        component = by_name.get(decl.component)
        if component is None:
            known = ", ".join(sorted(by_name)) or "(none in this composition)"
            raise RevlError(filename, decl.line,
                            f"fault test `{decl.name}` names unknown component `{decl.component}`",
                            hint=f"components in this composition: {known}")
        body = component.get("body") or []
        if not body:
            raise RevlError(filename, decl.line,
                            f"component `{decl.component}` has an empty activation body — "
                            f"there is no point at which it can fail")
        if decl.at_effect is not None:
            step = _fault_effect_index(body, decl.at_effect)
            if step is None:
                bindings = [s.get("bind") for s in body if s.get("step") == "let-effect"]
                known = ", ".join(f"`{b}`" for b in bindings) or "(none)"
                raise RevlError(
                    filename, decl.line,
                    f"fault test `{decl.name}`: component `{decl.component}` has no "
                    f"`let … effect` step bound to `{decl.at_effect}`",
                    hint=f"effect bindings in `{decl.component}`: {known}")
        else:
            step = decl.at_step
            if step > len(body):
                raise RevlError(
                    filename, decl.line,
                    f"fault test `{decl.name}`: `fail at step {step}` is past the end of "
                    f"`{decl.component}` (its activation body has {len(body)} step(s))")
        known_config = {field.get("name") for field in component.get("config") or []}
        for key in decl.config:
            if key not in known_config:
                fields = ", ".join(sorted(known_config)) or "(none)"
                raise RevlError(
                    filename, decl.line,
                    f"fault test `{decl.name}`: `{decl.component}` has no config field `{key}`",
                    hint=f"config fields of `{decl.component}`: {fields}")
        asserts: list[str] = []
        for kind, line in decl.asserts:
            if kind not in _FAULT_ASSERTS:  # pragma: no cover — parser gates the spelling
                raise RevlError(filename, line, f"unknown fault-test assertion `{kind}`")
            if kind not in asserts:
                asserts.append(kind)
        unit = {
            "name": decl.name,
            "component": decl.component,
            "at": {"step": step},
            "assert": asserts,
        }
        if decl.at_effect is not None:
            unit["at"]["effect"] = decl.at_effect
        if decl.config:
            unit["config"] = dict(decl.config)
        units.append(unit)
    return units

def _lower_lifecycle_body(decl: TestDecl, program: Program, services: dict, filename: str,
                          callables: set, types: dict) -> list:
    """Lower a `lifecycle test` body (syntax-2.0 §7.1).

    The body is a *linear script* over a live composition, so the checker can
    track exactly what is loaded and what keys are provided at every point —
    every diagnostic below is a compile error, not a runtime surprise.

    G2 (provision disjointness) is what makes this tractable and is also the
    reason there is no `swap C -> C2` statement: two components may not
    provide the same key in one document, so a replacement *provider* is not
    expressible; a replacement *instance* is `unload C` then `load C with
    { ... }`, which this statement set already spells.
    """
    components = {comp.name: comp for comp in program.components}
    loaded: dict[str, ComponentDecl] = {}    # component name -> decl
    provided: dict[str, str] = {}            # provision key -> component name
    scope: dict[str, bool] = {}
    type_env: dict[str, str] = {}
    body: list[dict] = []

    def _known() -> str:
        return ", ".join(f"`{name}`" for name in sorted(components)) or "<none>"

    for stmt in decl.body:
        if isinstance(stmt, LoadStmt):
            comp = components.get(stmt.component)
            if comp is None:
                raise RevlError(filename, stmt.line,
                                f"unknown component `{stmt.component}`",
                                hint=f"a lifecycle test loads components declared in this "
                                     f"document: {_known()}")
            if stmt.component in loaded:
                raise RevlError(
                    filename, stmt.line,
                    f"`{stmt.component}` is already loaded",
                    hint="one instance per component at a time — `unload` it first; two live "
                         "providers of one key is exactly what G2 forbids (§7.1)",
                )
            config = _lower_lifecycle_config(stmt, comp, filename, scope, callables,
                                             type_env, types)
            for key, _svc, _line in comp.provides:
                provided[key] = comp.name
            loaded[comp.name] = comp
            body.append({"step": "load", "component": comp.name, "config": config})
        elif isinstance(stmt, UnloadStmt):
            if stmt.component not in loaded:
                if stmt.component not in components:
                    raise RevlError(filename, stmt.line,
                                    f"unknown component `{stmt.component}`",
                                    hint=f"declared components: {_known()}")
                raise RevlError(filename, stmt.line,
                                f"`{stmt.component}` is not loaded at this point")
            comp = loaded.pop(stmt.component)
            for key, _svc, _line in comp.provides:
                provided.pop(key, None)
            body.append({"step": "unload", "component": comp.name})
        elif isinstance(stmt, CallStmt):
            body.append(_lower_lifecycle_call(stmt, provided, components, services, filename,
                                              scope, callables, type_env, types))
        elif isinstance(stmt, ResidueStmt):
            body.append({"step": "assert_no_residue"})
        elif isinstance(stmt, AssertStmt):
            expr = stmt.expr
            if isinstance(expr, ExprVar) and expr.name not in scope:
                raise RevlError(
                    filename, stmt.line,
                    f"unknown lifecycle assertion `{expr.name}`",
                    hint="the lifecycle assertion is `assert no_residue` (§7.1); anything else "
                         "after `assert` is a Bool expression over this test's `let` bindings",
                )
            _bool_cond(expr, type_env, types, filename, "assert")
            body.append({"step": "assert",
                         "expr": _lower_pure_expr(expr, scope, callables, {}, filename,
                                                  type_env, types)})
        else:  # pragma: no cover — the lifecycle grammar produces nothing else
            raise RevlError(filename, getattr(stmt, "line", decl.line),
                            "unexpected statement in a lifecycle test body")

    return body


def _lower_lifecycle_config(stmt: LoadStmt, comp: ComponentDecl, filename: str, scope: dict,
                            callables: set, type_env: dict, types: dict) -> dict:
    """Check and lower `load C with { field: expr, ... }` against C's `config`."""
    fields = {cfg.name: cfg for cfg in comp.config}
    given: dict[str, dict] = {}
    for name, value, line in stmt.config:
        cfg = fields.get(name)
        if cfg is None:
            known = ", ".join(f"`{f}`" for f in fields) or "<none>"
            raise RevlError(filename, line,
                            f"`{name}` is not a config field of {comp.name}",
                            hint=f"config fields of {comp.name}: {known}")
        if name in given:
            raise RevlError(filename, line, f"duplicate config field `{name}`")
        check_ast(value, cfg.type, type_env, types, filename,
                  f"config field `{name}` of {comp.name}")
        given[name] = _lower_pure_expr(value, scope, callables, {}, filename, type_env, types)
    missing = [cfg.name for cfg in comp.config if cfg.default is None and cfg.name not in given]
    if missing:
        listed = ", ".join(f"`{name}`" for name in missing)
        raise RevlError(filename, stmt.line,
                        f"`load {comp.name}` is missing required config {listed}",
                        hint=f"write `load {comp.name} with {{ {missing[0]}: ... }}`")
    return given


def _lower_lifecycle_call(stmt: CallStmt, provided: dict, components: dict, services: dict,
                          filename: str, scope: dict, callables: set,
                          type_env: dict, types: dict) -> dict:
    """Check and lower `call key.op(args)` / `let x = call key.op(args)`.

    A lifecycle test drives the composition from *outside* it — it is not a
    provider — so G4's `emit` marker does not apply here: the bound G4
    enforces is a service declaration bounding *its providers*, and a test has
    no declaration to exceed (§7.1).
    """
    owner = provided.get(stmt.key)
    if owner is None:
        keys = sorted({key for comp in components.values() for key, _s, _l in comp.provides})
        if stmt.key in keys:
            raise RevlError(
                filename, stmt.line,
                f"no provider for key `{stmt.key}` at this point",
                hint="load the component that provides it before calling through the key",
            )
        listed = ", ".join(f"`{key}`" for key in keys) or "<none>"
        raise RevlError(filename, stmt.line,
                        f"unknown provision key `{stmt.key}`",
                        hint=f"keys provided in this document: {listed}")
    comp = components[owner]
    service_name = next(svc for key, svc, _line in comp.provides if key == stmt.key)
    svc = services.get(service_name)
    if svc is None:  # pragma: no cover — checked when the component was lowered
        raise RevlError(filename, stmt.line, f"unknown service `{service_name}`")
    method = svc.methods.get(stmt.method)
    if method is None:
        listed = ", ".join(f"`{name}`" for name in svc.methods) or "<none>"
        raise RevlError(filename, stmt.line,
                        f"`{stmt.key}.{stmt.method}` is not an operation of service "
                        f"{service_name}",
                        hint=f"operations of {service_name}: {listed}")
    if len(stmt.args) != len(method.params):
        raise RevlError(filename, stmt.line,
                        f"`{stmt.key}.{stmt.method}` takes {len(method.params)} "
                        f"argument(s), {len(stmt.args)} given")
    args = []
    for arg, (pname, ptype) in zip(stmt.args, method.params):
        check_ast(arg, ptype, type_env, types, filename,
                  f"`{stmt.key}.{stmt.method}` argument `{pname}`")
        args.append(_lower_pure_expr(arg, scope, callables, {}, filename, type_env, types))
    node = {"step": "call", "key": stmt.key, "method": stmt.method, "args": args}
    if stmt.bind is not None:
        if stmt.bind in scope:
            raise RevlError(filename, stmt.line,
                            f"`{stmt.bind}` is already declared in this lifecycle test")
        scope[stmt.bind] = False
        if method.returns:
            type_env[stmt.bind] = method.returns
        node["bind"] = stmt.bind
    return node


def _bool_cond(expr, type_env: dict, types: dict, filename: str, where: str) -> None:
    # a condition is a check position like any other: `if (hole "…")` is a
    # `Bool` obligation, not an untyped hole (docs/holes.md)
    if pin_hole(expr, "Bool", filename, f"`{where}` condition"):
        return
    t = infer_ast(expr, type_env, types, filename)
    if t is not None and t != "Bool":
        raise mismatch(filename, getattr(expr, "line", 0), f"`{where}` condition", "Bool", t)


def _lower_pure_stmt(stmt, scope: dict, callables: set, alias_fns: dict, body: list, filename: str,
                     type_env: dict | None = None, types: dict | None = None,
                     expected_return: str | None = None) -> None:
    type_env = type_env if type_env is not None else {}
    types = types if types is not None else {}
    if isinstance(stmt, LetStmt):
        if stmt.name in scope:
            raise RevlError(filename, stmt.line, f"`{stmt.name}` is already declared in this function")
        # host provenance: a let bound to a host constructor call carries
        # host-object methods, exempt from the stdlib method table
        if not stmt.mutable and _is_host_valued(stmt.value, scope):
            scope[stmt.name] = "host"
        else:
            scope[stmt.name] = stmt.mutable
        declared = getattr(stmt, "type", None)
        if declared is not None:
            # `let g: (Int) -> Int = v => v + 1` — the annotation is the
            # checking position for the right-hand side, which is what gives an
            # un-annotated arrow its parameter and return types.
            check_type_wellformed(filename, stmt.line, declared)
            check_ast(stmt.value, declared, type_env, types, filename,
                      f"`let {stmt.name}: {render_type(declared)}`")
            type_env[stmt.name] = declared
        else:
            inferred = infer_ast(stmt.value, type_env, types, filename)
            if inferred is not None:
                type_env[stmt.name] = inferred
        body.append({"step": "let", "name": stmt.name,
                     "value": _lower_pure_expr(stmt.value, scope, callables, alias_fns, filename, type_env, types),
                     "mutable": stmt.mutable})
    elif isinstance(stmt, LetPatternStmt):
        _lower_let_pattern_stmt(stmt, scope, callables, alias_fns, body, filename, type_env, types)
    elif isinstance(stmt, AssignStmt):
        if stmt.name not in scope:
            raise RevlError(filename, stmt.line, f"`{stmt.name}` is not declared in this function",
                            hint="declare it with `let` (single-assignment) or `var` (mutable)")
        if not scope[stmt.name]:
            raise RevlError(filename, stmt.line,
                            f"cannot reassign `{stmt.name}` — it is `let` (single-assignment)",
                            hint="declare it with `var` to make it mutable (syntax-2.0 §3.5)")
        if stmt.op == "=":
            value = stmt.value
        else:
            # `x += e` desugars to `x = x + e`; the parser only composes the
            # operator, so lowering never has to remember it beyond this point.
            value = ExprBin(stmt.op[:-1], ExprVar(stmt.name, stmt.line), stmt.value, stmt.line)
        declared = type_env.get(stmt.name)
        if isinstance(value, ExprArrow) and declared:
            # reassigning a `var` of function type: the arrow needs the
            # declaration as its *checking* position, exactly as at the `let`.
            # Inference alone returns nothing for an un-annotated arrow, so the
            # comparison below would pass anything (docs/function-types.md).
            check_ast(value, declared, type_env, types, filename,
                      f"assignment to `{stmt.name}` (a `{render_type(declared)}` variable)")
        inferred = infer_ast(value, type_env, types, filename)
        if declared and inferred and not compatible(declared, inferred):
            raise mismatch(filename, stmt.line,
                           f"assignment to `{stmt.name}` (a `{render_type(declared)}` variable)",
                           declared, inferred)
        if inferred is not None and declared is None:
            type_env[stmt.name] = inferred
        body.append({"step": "assign", "name": stmt.name,
                     "value": _lower_pure_expr(value, scope, callables, alias_fns, filename, type_env, types)})
    elif isinstance(stmt, ReturnStmt):
        if stmt.expr is not None:
            check_ast(stmt.expr, expected_return, type_env, types, filename, "this function's return")
        elif expected_return:
            raise RevlError(filename, stmt.line,
                            f"bare `return` in a function declared to return `{expected_return}`")
        lowered = (None if stmt.expr is None else
                   _lower_pure_expr(stmt.expr, scope, callables, alias_fns,
                                    filename, type_env, types))
        if lowered is not None and expected_return:
            lowered = _inject_opt(expected_return,
                                  infer_ast(stmt.expr, type_env, types, filename),
                                  lowered)
        body.append({"step": "return", "expr": lowered})
    elif isinstance(stmt, IfStmt):
        then: list[dict] = []
        _bool_cond(stmt.cond, type_env, types, filename, "if")
        for s in stmt.then:
            _lower_pure_stmt(s, scope, callables, alias_fns, then, filename, type_env, types, expected_return)
        otherwise = None
        if stmt.otherwise is not None:
            otherwise = []
            for s in stmt.otherwise:
                _lower_pure_stmt(s, scope, callables, alias_fns, otherwise, filename, type_env, types, expected_return)
        body.append({"step": "if", "cond": _lower_pure_expr(stmt.cond, scope, callables, alias_fns, filename, type_env, types),
                     "then": then, "else": otherwise})
    elif isinstance(stmt, WhileStmt):
        _bool_cond(stmt.cond, type_env, types, filename, "while")
        cond = _lower_pure_expr(stmt.cond, scope, callables, alias_fns, filename, type_env, types)
        inner_scope = dict(scope)
        inner_type_env = dict(type_env)
        inner_body: list[dict] = []
        for s in stmt.body:
            _lower_pure_stmt(s, inner_scope, callables, alias_fns, inner_body, filename, inner_type_env, types, expected_return)
        body.append({"step": "while", "cond": cond, "body": inner_body})
    elif isinstance(stmt, ForStmt):
        if stmt.bind in scope:
            raise RevlError(filename, stmt.line, f"`{stmt.bind}` is already declared in this function")
        iter_diag = infer_ast(stmt.iterable, type_env, types, filename)
        if iter_diag is not None and parse_type(iter_diag)[0] != "List":
            raise RevlError(filename, stmt.line,
                            f"`for ... of` iterates a `List[...]`, got `{render_type(iter_diag)}`")
        iterable = _lower_pure_expr(stmt.iterable, scope, callables, alias_fns, filename, type_env, types)
        inner_scope = dict(scope)
        inner_type_env = dict(type_env)
        inner_scope[stmt.bind] = False
        iter_type = _expr_static_type(stmt.iterable, type_env, types)
        element_type = _type_arg(iter_type, "List")
        if element_type is not None:
            inner_type_env[stmt.bind] = element_type
        inner_body = []
        for s in stmt.body:
            _lower_pure_stmt(s, inner_scope, callables, alias_fns, inner_body, filename, inner_type_env, types, expected_return)
        body.append({"step": "for", "bind": stmt.bind, "iterable": iterable, "body": inner_body})
    elif isinstance(stmt, ExprStmt):
        infer_ast(stmt.expr, type_env, types, filename)
        body.append({"step": "expr", "expr": _lower_pure_expr(stmt.expr, scope, callables, alias_fns, filename, type_env, types)})
    elif isinstance(stmt, AssertStmt):
        _bool_cond(stmt.expr, type_env, types, filename, "assert")
        body.append({"step": "assert", "expr": _lower_pure_expr(stmt.expr, scope, callables, alias_fns, filename, type_env, types)})
    else:  # pragma: no cover — grammar prevents it
        raise RevlError(filename, getattr(stmt, "line", 1), "unexpected statement in fn body")


def _lower_let_pattern_stmt(stmt: LetPatternStmt, scope: dict, callables: set, alias_fns: dict,
                            body: list, filename: str, type_env: dict, types: dict) -> None:
    """Lower a `let`/`var` destructuring to one ``let_pattern`` step."""
    pattern = stmt.pattern
    if isinstance(pattern, RecordPattern):
        names = list(pattern.fields)
        if not names:
            raise RevlError(filename, pattern.line, "record destructuring needs at least one field")
        if len(set(names)) != len(names):
            raise RevlError(filename, pattern.line, "duplicate name in record destructuring")
        for name in names:
            if name in scope:
                raise RevlError(filename, stmt.line, f"`{name}` is already declared in this function")
        value_type = _expr_static_type(stmt.value, type_env, types)
        spec = types.get(value_type or "")
        if spec is not None:
            if spec.get("kind") != "record":
                raise RevlError(
                    filename, stmt.line,
                    f"record destructuring requires a record, but `{render_type(value_type)}` is not a record",
                )
            fields = spec.get("fields", {})
            for name in names:
                if name not in fields:
                    raise RevlError(filename, pattern.line,
                                    f"`{name}` is not a field of record `{render_type(value_type)}`")
        elif value_type is not None and parse_type(value_type)[0] in _BUILTIN_NONRECORD:
            raise RevlError(
                filename, stmt.line,
                f"record destructuring requires a record, but `{render_type(value_type)}` is not a record",
            )
        value_ir = _lower_pure_expr(stmt.value, scope, callables, alias_fns, filename, type_env, types)
        for name in names:
            scope[name] = stmt.mutable
            if spec is not None:
                type_env[name] = spec.get("fields", {})[name]
        body.append({
            "step": "let_pattern",
            "pattern": "record",
            "names": names,
            "value": value_ir,
            "mutable": stmt.mutable,
        })
        return

    if isinstance(pattern, ListPattern):
        if not pattern.binds:
            raise RevlError(filename, pattern.line,
                            "list destructuring needs at least one binding before `...rest`")
        names = list(pattern.binds)
        if pattern.rest is not None:
            names.append(pattern.rest)
        if len(set(names)) != len(names):
            raise RevlError(filename, pattern.line, "duplicate name in list destructuring")
        for name in names:
            if name in scope:
                raise RevlError(filename, stmt.line, f"`{name}` is already declared in this function")
        value_type = _expr_static_type(stmt.value, type_env, types)
        if value_type is not None and parse_type(value_type)[0] != "List" and (
            types.get(value_type) is not None
            or parse_type(value_type)[0] in _BUILTIN_NONRECORD
        ):
            raise RevlError(
                filename, stmt.line,
                f"list destructuring requires a `List[...]`, but `{render_type(value_type)}` is not a list",
            )
        value_ir = _lower_pure_expr(stmt.value, scope, callables, alias_fns, filename, type_env, types)
        element_type = _type_arg(value_type, "List")
        for name in pattern.binds:
            scope[name] = stmt.mutable
            if element_type is not None:
                type_env[name] = element_type
        if pattern.rest is not None:
            scope[pattern.rest] = stmt.mutable
            if value_type is not None:
                type_env[pattern.rest] = value_type
        body.append({
            "step": "let_pattern",
            "pattern": "list",
            "names": pattern.binds,
            "rest": pattern.rest,
            "value": value_ir,
            "mutable": stmt.mutable,
        })
        return

    raise RevlError(filename, stmt.line, "unexpected destructuring pattern")


def _tagged_case(name: str, types: dict) -> dict | None:
    """If `name` is a *tagged* ADT constructor — the built-in `Result`
    (`Ok`/`Err`) or any user variant case — return its case-table entry.
    `Opt` (`Some`/`None`) is not tagged: it stays host-null/value, so it is
    excluded here and handled as identity/null."""
    if name in ("Some", "None"):
        return None
    return (types.get(CASES_KEY) or {}).get(name)


def _lower_hole(expr: ExprHole, filename: str) -> dict:
    """`hole` -> the IR's one non-executable node (docs/holes.md).

    A hole must know its type by now: the checker pins it from context in
    check position, and `hole[T]` states it outright. Anywhere else the
    honest answer is a rejection — guessing would hand the author a type the
    compiler invented, which is exactly the drowning-in-noise failure holes
    exist to fix.
    """
    type_name = expr.known_type
    if type_name is None:
        raise RevlError(
            filename, expr.line,
            "this `hole` has no expected type — nothing in its context says "
            "what it must eventually be",
            hint="annotate it (`hole[Str] \"why\"`), or put it somewhere with a "
                 "declared type: a `fn`'s `-> T`, a service method's return, or "
                 "an argument of a declared function (docs/holes.md)",
            code="T3", category="hole",
        )
    check_type_wellformed(filename, expr.line, type_name)
    node = {"kind": "hole", "type": type_name, "file": filename, "line": expr.line}
    if expr.message is not None:
        node["message"] = expr.message
    return node


def _retarget_holes(node, source: str) -> None:
    """Point holes at the file they were written in.

    Lowering runs over one merged program, so its `filename` is the first
    root path; a declaration carries its own provenance and an obligation an
    agent must go and fill is worthless with the wrong path on it.
    """
    if isinstance(node, dict):
        if node.get("kind") == "hole":
            node["file"] = source
        for value in node.values():
            _retarget_holes(value, source)
    elif isinstance(node, list):
        for value in node:
            _retarget_holes(value, source)


def _lower_pure_expr(expr, scope: dict, callables: set, alias_fns: dict, filename: str,
                     type_env: dict | None = None, types: dict | None = None) -> dict:
    type_env = type_env if type_env is not None else {}
    types = types if types is not None else {}
    if isinstance(expr, ExprHole):
        return _lower_hole(expr, filename)
    # ADT construction (Result / user variants) lowers to a tagged `adt` node
    # (Opt's Some/None are not tagged — handled as identity/null downstream).
    _cases = types.get(CASES_KEY) or {}
    if isinstance(expr, ExprCall) and isinstance(expr.callee, ExprVar) \
            and _tagged_case(expr.callee.name, types) is not None:
        info = _cases[expr.callee.name]
        return {"kind": "adt", "type": info["adt"], "case": expr.callee.name,
                "args": [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types)
                         for a in expr.args]}
    if isinstance(expr, ExprVar):
        info = _tagged_case(expr.name, types)
        if info is not None and info.get("payload") is None \
                and not str(info.get("adt", "")).startswith(("Result", "Opt")):
            return {"kind": "adt", "type": info["adt"], "case": expr.name, "args": []}
    # Module-namespace call: `alias.fn(args)` desugars to the imported public
    # function by its original name (IR functions are top-level, not nested
    # in namespace objects).
    if (
        isinstance(expr, ExprCall)
        and isinstance(expr.callee, ExprField)
        and isinstance(expr.callee.target, ExprVar)
    ):
        alias = expr.callee.target.name
        if alias in alias_fns:
            name = expr.callee.name
            if name not in alias_fns[alias]:
                raise RevlError(
                    filename,
                    expr.callee.line,
                    f"`{alias}.{name}` is not a public function in module `{alias}`",
                    hint=f"`use ... as {alias}` imports only `pub` declarations (G1)",
                )
            return {
                "kind": "call",
                "callee": {"kind": "var", "name": name},
                "args": [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types) for a in expr.args],
            }
    if isinstance(expr, ExprLit):
        if expr.value is None:
            raise null_error(filename, expr.line)
        return {"kind": "lit", "value": expr.value}
    if isinstance(expr, ExprVar):
        if expr.name not in scope and expr.name not in callables:
            raise RevlError(filename, expr.line, f"`{expr.name}` is not declared in this function",
                            hint="declare it with `let`/`var` or add it as a parameter (G1)")
        return {"kind": "var", "name": expr.name}
    if isinstance(expr, ExprBin):
        return {"kind": "bin", "op": expr.op,
                "left": _lower_pure_expr(expr.left, scope, callables, alias_fns, filename, type_env, types),
                "right": _lower_pure_expr(expr.right, scope, callables, alias_fns, filename, type_env, types)}
    if isinstance(expr, ExprUn):
        return {"kind": "un", "op": expr.op,
                "operand": _lower_pure_expr(expr.operand, scope, callables, alias_fns, filename, type_env, types)}
    if isinstance(expr, ExprCall):
        _callee = expr.callee
        _host_receiver = isinstance(_callee, ExprField) and (
            _is_host_valued(_callee.target, scope)
            # the constructor root itself (Map.new(), Pool.open(...)):
            or (isinstance(_callee.target, ExprVar)
                and _callee.target.name in _HOST_CALLABLES
                and _callee.target.name not in scope)
        )
        if isinstance(expr.callee, ExprField) and not _host_receiver:
            method = expr.callee.name
            arity = _BUILTIN_METHODS.get(method)
            if arity is None:
                raise RevlError(
                    filename, expr.line,
                    f"no builtin method `{method}` on values — the stdlib surface is "
                    f"{', '.join(sorted(_BUILTIN_METHODS))} (docs/stdlib-2.0.md)",
                    hint="records carry data, not methods; call functions as `f(x)`, "
                         "and call arrows through a `let` binding",
                )
            if len(expr.args) != arity:
                raise RevlError(filename, expr.line,
                                f"builtin `{method}` takes {arity} argument(s), "
                                f"{len(expr.args)} given")
            return {"kind": "builtin", "method": method,
                    "target": _lower_pure_expr(expr.callee.target, scope, callables, alias_fns, filename, type_env, types),
                    "args": [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types) for a in expr.args]}
        return {"kind": "call", "callee": _lower_pure_expr(expr.callee, scope, callables, alias_fns, filename, type_env, types),
                "args": [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types) for a in expr.args]}
    if isinstance(expr, ExprField):
        target_type = _expr_static_type(expr.target, type_env, types)
        if expr.name == "length" and _is_sized_type(target_type):
            return {"kind": "len",
                    "target": _lower_pure_expr(expr.target, scope, callables, alias_fns, filename, type_env, types)}
        return {"kind": "field", "target": _lower_pure_expr(expr.target, scope, callables, alias_fns, filename, type_env, types),
                "name": expr.name}
    if isinstance(expr, ExprIndex):
        return {"kind": "index", "target": _lower_pure_expr(expr.target, scope, callables, alias_fns, filename, type_env, types),
                "index": _lower_pure_expr(expr.index, scope, callables, alias_fns, filename, type_env, types)}
    if isinstance(expr, ExprIf):
        return {"kind": "if", "cond": _lower_pure_expr(expr.cond, scope, callables, alias_fns, filename, type_env, types),
                "then": _lower_pure_expr(expr.then, scope, callables, alias_fns, filename, type_env, types),
                "else": _lower_pure_expr(expr.otherwise, scope, callables, alias_fns, filename, type_env, types)}
    if isinstance(expr, ExprRecord):
        for name, field_expr in expr.fields:
            if isinstance(field_expr, ExprVar) and scope.get(field_expr.name) is True:
                raise RevlError(
                    filename,
                    field_expr.line,
                    f"`var` `{field_expr.name}` cannot be used in a record literal — "
                    "a `var` never escapes its function (syntax-2.0 §3.5)",
                    hint="copy its current value into a `let` first, or use it directly outside a record",
                )
        return {"kind": "record",
                "fields": [[name, _lower_pure_expr(e, scope, callables, alias_fns, filename, type_env, types)]
                           for name, e in expr.fields]}
    if isinstance(expr, ExprList):
        return {"kind": "list",
                "items": [_lower_pure_expr(e, scope, callables, alias_fns, filename, type_env, types) for e in expr.items]}
    if isinstance(expr, ExprArrow):
        inner = dict(scope)
        inner_type_env = dict(type_env)
        param_types = _arrow_param_types(expr)
        for param, ptype in zip(expr.params, param_types):
            inner[param] = False
            if ptype:
                inner_type_env[param] = ptype
            else:
                inner_type_env.pop(param, None)
        captures = sorted(_mutable_free_vars(expr.body, scope, set(expr.params)))
        node = {"kind": "arrow", "params": expr.params, "captures": captures,
                "body": _lower_pure_expr(expr.body, inner, callables, alias_fns, filename, inner_type_env, types)}
        # IR v3: an arrow that the checker typed carries its signature, so a
        # backend can declare it instead of guessing (docs/function-types.md).
        # Both keys are absent together when the arrow is still untyped.
        if any(p is not None for p in param_types) or expr.returns:
            node["param_types"] = param_types
            node["returns"] = expr.returns
        return node
    if isinstance(expr, ExprMatch):
        scrutinee_type = _expr_static_type(expr.scrutinee, type_env, types)
        _check_match_exhaustiveness(expr, type_env, types, filename)
        scrutinee = _lower_pure_expr(expr.scrutinee, scope, callables, alias_fns, filename, type_env, types)
        arms = []
        for pattern, bind, body in expr.arms:
            inner_scope = dict(scope)
            inner_type_env = dict(type_env)
            payload_type = _arm_payload_type(scrutinee_type, pattern, types)
            if bind is not None:
                inner_scope[bind] = False
                inner_type_env.pop(bind, None)
                if payload_type is not None:
                    inner_type_env[bind] = payload_type
            arm = {
                "pattern": pattern,
                "bind": bind,
                "body": _lower_pure_expr(body, inner_scope, callables, alias_fns, filename, inner_type_env, types),
            }
            # payload type helps backends that must cast (e.g. Java's tagged
            # Result); other emitters ignore it
            if payload_type is not None:
                arm["payload_type"] = payload_type
            arms.append(arm)
        return {"kind": "match", "scrutinee": scrutinee, "arms": arms}
    if isinstance(expr, ExprOptField):
        return {
            "kind": "optfield",
            "target": _lower_pure_expr(expr.target, scope, callables, alias_fns, filename, type_env, types),
            "name": expr.name,
        }
    if isinstance(expr, ExprOptCall):
        return {
            "kind": "optcall",
            "target": _lower_pure_expr(expr.target, scope, callables, alias_fns, filename, type_env, types),
            "method": expr.method,
            "args": [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types) for a in expr.args],
        }
    if isinstance(expr, Interp):
        # each `${...}` segment is a full expression, lowered (and G1/type-
        # checked) like any other; text segments pass through
        parts: list = []
        for kind, value in expr.parts:
            if kind == "text":
                parts.append(["text", value])
            else:
                parts.append(["expr", _lower_pure_expr(
                    value, scope, callables, alias_fns, filename, type_env, types)])
        return {"kind": "interp", "parts": parts}
    raise RevlError(filename, getattr(expr, "line", 1), "unexpected expression in fn body")


def _inject_opt(expected: str | None, actual: str | None, node: dict) -> dict:
    """Materialize the `T` -> `Opt[T]` injection the checker accepts.

    `compatible` lets a `T` stand where an `Opt[T]` is declared (typecheck.py,
    "Opt discipline"), but accepting the *type* is only half of it: the value
    still has to become an optional. Nothing did that, so a method declared
    `-> Opt[Int]` returning `1` emitted `Option<i64> { 1 }` on rust and
    `Optional<Long> { return 1L; }` on java — both rejected by their
    compilers. python and TypeScript never noticed, which is why it survived:
    the injection is invisible on an untyped tier.

    Done here rather than in each backend because the frontend is the single
    IR producer, and because deciding it in a backend needs the very type
    information the emitters do not carry.
    """
    if actual is None:
        return node
    ehead, eargs = parse_type(expected)
    if ehead != "Opt" or not eargs:
        return node
    if parse_type(actual)[0] == "Opt":
        return node          # already an optional; injecting would double-wrap
    return {"kind": "call", "callee": {"kind": "var", "name": "Some"}, "args": [node]}


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

    component_callables = (
        _HOST_CALLABLES
        | _BUILTIN_CONSTRUCTORS
        | {fn.name for fn in program.fn_decls}
        | {ext.name for ext in program.externs}
    )

    # types and signatures are built first so component lowering can
    # type-check service/fn call sites (the sound-typing milestone)
    _resolve_type_aliases(program, program.filename)
    _validate_declared_types(program, program.filename)
    types = _lower_type_decls(program, program.filename)
    types[FNS_KEY] = _signature_table(program, types)
    types[CASES_KEY] = _case_table(types)
    fns = _lower_fns(program, program.filename, types)
    externs = _lower_externs(program, program.filename)
    # One fixed point, two consumers: `emitting_caps` is what it computes
    # (docs/capabilities.md), `witness` is why (why.py). Evidence never
    # decides a rejection, it only explains one.
    emission_evidence = _EmissionEvidence(program)
    emitting_caps = _emitting_capabilities(fns, externs, emission_evidence.witness)
    emitting_fns = set(emitting_caps)

    components = []
    seen = set()
    for comp in program.components:
        if comp.name in seen:
            raise RevlError(program.filename, comp.line, f"duplicate component `{comp.name}`")
        seen.add(comp.name)
        lowered_comp = _lower_component(comp, services, program.filename,
                                        component_callables, types, emitting_fns,
                                        emitting_caps, emission_evidence)
        if comp.source:
            _retarget_holes(lowered_comp, comp.source)
        components.append(lowered_comp)

    fault_tests = _lower_fault_tests(program, components, program.filename)

    manifest = _link(program, components, ambient.get("components") or [])

    # lifecycle tests are lowered last: they check against the component
    # declarations, so a broken component must report itself first
    tests = _lower_tests(program, program.filename, types, services)

    uses_components_2 = any(
        isinstance(stmt, (FailStmt, IfStmt))
        or (isinstance(stmt, (LetEffect, EffectStmt)) and stmt.setup)
        for comp in program.components
        for stmt in comp.body
    )
    uses_v2 = any(comp.get("isolate") or comp.get("intercept") for comp in components)
    uses_v3 = any(not name.startswith("__") for name in types) or bool(fns) or bool(externs) or bool(tests)
    uses_v3 = uses_v3 or any(
        svc.commutative or any(m.async_ or m.commutative for m in svc.methods.values())
        for svc in services.values()
    )
    uses_v3 = uses_v3 or uses_components_2
    # a `fault_tests` section is an additive v3 feature; the version bump is
    # itself a guard, so a consumer that predates the section refuses the
    # whole document instead of silently dropping the fault tests
    uses_v3 = uses_v3 or bool(fault_tests)

    def _has_builtin(node) -> bool:
        if isinstance(node, dict):
            # a tagged ADT construction is a v3 feature too (`adt` node)
            if node.get("kind") in ("builtin", "adt"):
                return True
            return any(_has_builtin(v) for v in node.values())
        if isinstance(node, list):
            return any(_has_builtin(v) for v in node)
        return False

    uses_v3 = uses_v3 or any(_has_builtin(comp.get("body")) for comp in components)

    result = {
        "ir_version": IR_VERSION_V3 if uses_v3 else (IR_VERSION_V2 if uses_v2 else IR_VERSION),
        "services": {
            name: {
                **({"commutative": True} if svc.commutative else {}),
                "methods": {
                    m.name: {
                        "params": [{"name": p, "type": t} for p, t in m.params],
                        "returns": m.returns,
                        "emission": m.emission,
                        # only a *scoped* emission carries the key: bare
                        # `emission` means "any capability", and its absence
                        # is exactly that (so no pre-capability IR changes)
                        **({"capabilities": list(m.capabilities)}
                           if m.capabilities is not None else {}),
                        **({"async": True} if m.async_ else {}),
                        **({"commutative": True} if m.commutative else {}),
                    }
                    for m in svc.methods.values()
                },
            }
            for name, svc in services.items()
        },
        "components": components,
        "manifest": manifest,
    }
    user_types = {name: spec for name, spec in types.items() if not name.startswith("__")}
    if user_types:
        result["types"] = user_types
    if fns:
        result["functions"] = fns
    if externs:
        result["externs"] = externs
    if tests:
        result["tests"] = tests
    # the obligation ledger (docs/holes.md). Present only when the draft has
    # holes, so an IR document for finished code is byte-identical to before.
    obligations = holes.collect(result)
    if obligations:
        result["holes"] = obligations

    if fault_tests:
        result["fault_tests"] = fault_tests
    return result


def _service_from_ir(name: str, spec: dict) -> ServiceDecl:
    """Rebuild a checkable ServiceDecl from a v1 IR services entry."""
    from .parser import MethodDecl

    methods = {}
    for mname, mspec in (spec.get("methods") or {}).items():
        params = [(p.get("name"), p.get("type")) for p in mspec.get("params") or []]
        methods[mname] = MethodDecl(
            mname,
            params,
            mspec.get("returns"),
            bool(mspec.get("emission")),
            0,
            async_=bool(mspec.get("async")),
            commutative=bool(mspec.get("commutative")),
            capabilities=(tuple(mspec["capabilities"])
                          if mspec.get("capabilities") is not None else None),
        )
    return ServiceDecl(name, methods, 0, commutative=bool(spec.get("commutative")))


def _service_equal(a: ServiceDecl, b: ServiceDecl) -> bool:
    def shape(svc: ServiceDecl):
        return (
            svc.commutative,
            {
                m.name: (tuple(m.params), m.returns, m.emission, m.async_,
                         m.commutative, m.capabilities)
                for m in svc.methods.values()
            },
        )

    return shape(a) == shape(b)


# ---------------------------------------------------------------- components

def _component_scope(env: Env) -> dict[str, str]:
    scope = {name: safe for name, safe in env.locals.items()}
    scope.update(env.params)
    return scope


def _component_req_call(env: Env, root: str, method: str, args: list, line: int) -> dict:
    if root not in env.requires:
        raise RevlError(env.filename, line,
                        f"`{root}` is not a declared requirement of {env.component.name}")
    svc = env.services[env.requires[root]]
    decl = svc.methods.get(method)
    if decl is None:
        raise RevlError(env.filename, line,
                        f"`{root}.{method}` is not a method of service {svc.name}")
    if len(args) != len(decl.params):
        raise RevlError(env.filename, line,
                        f"`{root}.{method}` takes {len(decl.params)} "
                        f"argument(s), {len(args)} given")
    if decl.emission and getattr(env, "_expr_mode", "setup") == "setup":
        raise RevlError(
            env.filename, line,
            f"call to emission `{root}.{method}` must be marked `emit` (G4)",
            hint="an emission crosses the system boundary and cannot be reverted; "
                 "`emit` makes that visible at the call site",
        )
    for arg, (pname, ptype) in zip(args, decl.params):
        actual = infer_ir(arg, env.type_env, env.types, env.services)
        if ptype and actual and not compatible(ptype, actual):
            raise mismatch(env.filename, line,
                           f"`{root}.{method}` argument `{pname}`", ptype, actual)
    return {"kind": "call", "target": {"kind": "req", "name": root},
            "method": method, "args": args}


def _lower_component_pure_expr(expr, env: Env, scope: dict[str, str], callables: set,
                               pure_only: bool = False) -> dict:
    filename = env.filename
    line = getattr(expr, "line", 0)

    if isinstance(expr, ExprHole):
        return _lower_hole(expr, filename)

    # ADT construction (Result / user variants) — same tagged `adt` node as
    # the pure-fn path; Opt's Some/None stay untagged.
    cases = env.types.get(CASES_KEY) or {}
    if isinstance(expr, ExprCall) and isinstance(expr.callee, ExprVar) \
            and _tagged_case(expr.callee.name, env.types) is not None:
        info = cases[expr.callee.name]
        return {"kind": "adt", "type": info["adt"], "case": expr.callee.name,
                "args": [_lower_component_pure_expr(a, env, scope, callables, pure_only)
                         for a in expr.args]}
    if isinstance(expr, ExprVar):
        info = _tagged_case(expr.name, env.types)
        if info is not None and info.get("payload") is None \
                and not str(info.get("adt", "")).startswith(("Result", "Opt")):
            return {"kind": "adt", "type": info["adt"], "case": expr.name, "args": []}

    if isinstance(expr, EmitExpr):
        # the value of an irreversible call; the marker stays at the call site
        saved_mode = getattr(env, "_expr_mode", "setup")
        env._expr_mode = "emit"
        try:
            node = _lower_component_pure_expr(expr.expr, env, scope, callables, pure_only)
        finally:
            env._expr_mode = saved_mode
        if not _is_emission_call(node, env):
            raise RevlError(
                filename, expr.line,
                f"`emit` on {_node_desc(node)}, which is not declared `emission`",
                hint="only calls to `emission` service operations cross the boundary; "
                     "drop the `emit` marker (G4)",
                code="G4", category="emission",
            )
        return node
    if isinstance(expr, ExprMatch):
        # the ADT eliminator, available in component and method bodies too:
        # a component consuming a Result should not have to call out to a fn
        scrutinee = _lower_component_pure_expr(expr.scrutinee, env, scope,
                                               callables, pure_only)
        _check_match_exhaustiveness(expr, env.type_env, env.types, filename)
        arms = []
        for pattern, bind, body in expr.arms:
            inner = dict(scope)
            if bind is not None:
                safe = _safe_name(bind, set(scope.values()))
                inner[bind] = safe
            arms.append({
                "pattern": pattern,
                "bind": inner.get(bind) if bind is not None else None,
                "body": _lower_component_pure_expr(body, env, inner, callables, pure_only),
            })
        return {"kind": "match", "scrutinee": scrutinee, "arms": arms}
    if isinstance(expr, ExprLit):
        if expr.value is None:
            raise null_error(filename, line)
        return {"kind": "lit", "value": expr.value}
    if isinstance(expr, Interp):
        return _lower_expr(expr, env, mode=getattr(env, "_expr_mode", "setup"))
    if isinstance(expr, ExprVar):
        name = expr.name
        if name in scope:
            return {"kind": "name", "id": scope[name]}
        info = _tagged_case(name, env.types)
        if info is not None and info.get("payload") is None \
                and not str(info.get("adt", "")).startswith(("Result", "Opt")):
            return {"kind": "adt", "type": info["adt"], "case": name, "args": []}
        if name in callables:
            return {"kind": "var", "name": name}
        if getattr(env, "_plain_body", False):
            declared = ", ".join(f"`{r}`" for r in env.requires) or "<nothing>"
            raise RevlError(
                filename, line,
                f"`{name}` is not a declared requirement of {env.component.name}",
                hint=f"component {env.component.name} requires {declared} — "
                     f"add `requires {name}: <Service>`?",
            )
        raise RevlError(filename, line,
                        f"`{name}` is not declared in this component effect block",
                        hint="declare it with `let` in the effect block, or use a "
                             "requirement/config field (G1)")
    if isinstance(expr, ExprField):
        if isinstance(expr.target, ExprVar) and expr.target.name == "config":
            if expr.name not in env.config_fields:
                raise RevlError(filename, line,
                                f"`{expr.name}` is not a config field of {env.component.name}")
            return {"kind": "config", "field": expr.name}
        return {"kind": "field",
                "target": _lower_component_pure_expr(expr.target, env, scope, callables,
                                                     pure_only),
                "name": expr.name}
    if isinstance(expr, ExprCall):
        args = [_lower_component_pure_expr(a, env, scope, callables, pure_only)
                for a in expr.args]
        if isinstance(expr.callee, ExprField) and isinstance(expr.callee.target, ExprVar):
            root = expr.callee.target.name
            method = expr.callee.name
            if root in _HOST_CALLABLES:
                if pure_only:
                    raise RevlError(
                        filename, line,
                        f"host builtin `{root}.{method}` is an effect and cannot appear "
                        "in pure setup",
                        hint="move the acquisition to the final expression of the effect block (G6)",
                    )
                host_check(f"{root}.{method}",
                           [infer_ir(a, env.type_env, env.types, env.services)
                            for a in args],
                           filename, line)
                return {"kind": "host", "fn": f"{root}.{method}", "args": args}
            if root in env.requires:
                if pure_only:
                    raise RevlError(
                        filename, line,
                        f"call to required service `{root}.{method}` is an effect and cannot "
                        "appear in pure setup",
                        hint="service calls must be acquired with `effect ... undo ...` or "
                             "marked `emit` (G4/G6)",
                    )
                return _component_req_call(env, root, method, args, line)
            if method in _BUILTIN_METHODS:
                if len(args) != _BUILTIN_METHODS[method]:
                    raise RevlError(filename, line,
                                    f"builtin `{method}` takes {_BUILTIN_METHODS[method]} "
                                    f"argument(s), {len(args)} given")
                return {"kind": "builtin", "method": method,
                        "target": _lower_component_pure_expr(expr.callee.target, env, scope,
                                                             callables, pure_only),
                        "args": args}
            if root in scope:
                # A method on a local that is a *known* stdlib-bearing value
                # (Str/List/Bytes) must be a builtin — builtins were already
                # handled above, so a non-builtin here is a typo/misuse, not a
                # host method. Receivers of unknown/host type infer to None and
                # stay lenient (host provenance is exempt — docs/stdlib-2.0.md).
                recv_t = infer_ir({"kind": "name", "id": scope[root]},
                                  env.type_env, env.types, env.services)
                if parse_type(recv_t)[0] in _SIZED_HEADS:
                    raise RevlError(
                        filename, line,
                        f"no builtin method `{method}` on `{recv_t}` — the stdlib surface is "
                        f"{', '.join(sorted(_BUILTIN_METHODS))} (docs/stdlib-2.0.md)",
                        hint="records carry data, not methods; call functions as `f(x)` (G6)",
                    )
                return {"kind": "call",
                        "target": {"kind": "name", "id": scope[root]},
                        "method": method, "args": args}
        if isinstance(expr.callee, ExprVar):
            name = expr.callee.name
            # ADT/Opt construction lowers exactly as it does in a `fn` body:
            # tagged variants to an `adt` node, Some/None to identity/null.
            # Emitting a plain call here would name a constructor the host
            # runtimes do not define (they are not functions).
            if _tagged_case(name, env.types) is not None:
                info = (env.types.get(CASES_KEY) or {})[name]
                return {"kind": "adt", "type": info["adt"], "case": name, "args": args}
            if name in _BUILTIN_CONSTRUCTORS:
                return {"kind": "call", "callee": {"kind": "var", "name": name},
                        "args": args}
            if name in callables:
                return {"kind": "fn", "name": name, "args": args}
        if isinstance(expr.callee, ExprField) and expr.callee.name in _BUILTIN_METHODS:
            method = expr.callee.name
            if len(args) != _BUILTIN_METHODS[method]:
                raise RevlError(filename, line,
                                f"builtin `{method}` takes {_BUILTIN_METHODS[method]} "
                                f"argument(s), {len(args)} given")
            return {"kind": "builtin", "method": method,
                    "target": _lower_component_pure_expr(expr.callee.target, env, scope,
                                                         callables, pure_only),
                    "args": args}
        return {"kind": "call",
                "callee": _lower_component_pure_expr(expr.callee, env, scope, callables,
                                                     pure_only),
                "args": args}
    if isinstance(expr, ExprBin):
        return {"kind": "bin", "op": expr.op,
                "left": _lower_component_pure_expr(expr.left, env, scope, callables,
                                                   pure_only),
                "right": _lower_component_pure_expr(expr.right, env, scope, callables,
                                                    pure_only)}
    if isinstance(expr, ExprUn):
        return {"kind": "un", "op": expr.op,
                "operand": _lower_component_pure_expr(expr.operand, env, scope, callables,
                                                      pure_only)}
    if isinstance(expr, ExprIndex):
        return {"kind": "index",
                "target": _lower_component_pure_expr(expr.target, env, scope, callables,
                                                     pure_only),
                "index": _lower_component_pure_expr(expr.index, env, scope, callables,
                                                    pure_only)}
    if isinstance(expr, ExprIf):
        return {"kind": "if",
                "cond": _lower_component_pure_expr(expr.cond, env, scope, callables,
                                                   pure_only),
                "then": _lower_component_pure_expr(expr.then, env, scope, callables,
                                                   pure_only),
                "else": _lower_component_pure_expr(expr.otherwise, env, scope, callables,
                                                   pure_only)}
    if isinstance(expr, ExprRecord):
        return {"kind": "record",
                "fields": [[name, _lower_component_pure_expr(e, env, scope, callables,
                                                             pure_only)]
                           for name, e in expr.fields]}
    if isinstance(expr, ExprList):
        return {"kind": "list",
                "items": [_lower_component_pure_expr(e, env, scope, callables, pure_only)
                          for e in expr.items]}
    if isinstance(expr, ExprArrow):
        node = {"kind": "arrow", "params": expr.params,
                "body": _lower_component_pure_expr(expr.body, env, scope, callables,
                                                   pure_only)}
        param_types = _arrow_param_types(expr)
        if any(p is not None for p in param_types) or expr.returns:
            node["param_types"] = param_types
            node["returns"] = expr.returns
        return node
    if isinstance(expr, ExprOptField):
        return {
            "kind": "optfield",
            "target": _lower_component_pure_expr(expr.target, env, scope, callables, pure_only),
            "name": expr.name,
        }
    if isinstance(expr, ExprOptCall):
        return {
            "kind": "optcall",
            "target": _lower_component_pure_expr(expr.target, env, scope, callables, pure_only),
            "method": expr.method,
            "args": [_lower_component_pure_expr(a, env, scope, callables, pure_only) for a in expr.args],
        }
    raise RevlError(filename, line, "unsupported expression in component effect block",
                    hint="block-effect setup is stratum-1 pure code (G6)")


def _lower_component_setup_stmt(stmt, env: Env, scope: dict[str, str], callables: set,
                                mutables: set[str], out: list) -> None:
    filename = env.filename

    def _sweep(node, line):
        # Run the raising type oracle over a lowered setup node (HOLE 3(c) /
        # HOLE 2): definite operator/builtin misuse in stratum-1 setup code now
        # raises, exactly as it does in a `fn` body. Unknown/host operands infer
        # to None and are left alone. Returns the inferred type (or None).
        return infer_ir(node, env.type_env, env.types, env.services, filename, line)

    if isinstance(stmt, LetStmt):
        safe = env.bind_local(stmt.name, stmt.line)
        scope[stmt.name] = safe
        if stmt.mutable:
            mutables.add(stmt.name)
        value = _lower_component_pure_expr(stmt.value, env, scope, callables,
                                           pure_only=True)
        inferred = _sweep(value, stmt.line)
        if inferred is not None:
            env.type_env[safe] = inferred
        out.append({"step": "let", "name": safe, "value": value})
    elif isinstance(stmt, AssignStmt):
        if stmt.name not in scope:
            raise RevlError(filename, stmt.line,
                            f"`{stmt.name}` is not declared in this effect block",
                            hint="declare it with `let`/`var` first (G1)")
        if stmt.name not in mutables:
            raise RevlError(filename, stmt.line,
                            f"cannot reassign `{stmt.name}` — it is `let` (single-assignment)",
                            hint="declare it with `var` to make it mutable (syntax-2.0 §3.5)")
        value = _lower_component_pure_expr(stmt.value, env, scope, callables,
                                           pure_only=True)
        _sweep(value, stmt.line)
        out.append({"step": "assign", "name": scope[stmt.name], "value": value})
    elif isinstance(stmt, ExprStmt):
        expr = _lower_component_pure_expr(stmt.expr, env, scope, callables,
                                          pure_only=True)
        _sweep(expr, stmt.line)
        out.append({"step": "expr", "expr": expr})
    elif isinstance(stmt, IfStmt):
        cond = _lower_component_pure_expr(stmt.cond, env, scope, callables,
                                          pure_only=True)
        cond_t = _sweep(cond, stmt.line)
        if cond_t is not None and cond_t != "Bool":
            raise mismatch(filename, stmt.line, "`if` condition", "Bool", cond_t)
        then: list[dict] = []
        for s in stmt.then:
            _lower_component_setup_stmt(s, env, scope, callables, mutables, then)
        otherwise = None
        if stmt.otherwise is not None:
            otherwise = []
            for s in stmt.otherwise:
                _lower_component_setup_stmt(s, env, scope, callables, mutables, otherwise)
        out.append({"step": "if", "cond": cond, "then": then})
        if otherwise is not None:
            out[-1]["else"] = otherwise
    elif isinstance(stmt, AssertStmt):
        expr = _lower_component_pure_expr(stmt.expr, env, scope, callables,
                                          pure_only=True)
        assert_t = _sweep(expr, stmt.line)
        if assert_t is not None and assert_t != "Bool":
            raise mismatch(filename, stmt.line, "`assert`", "Bool", assert_t)
        out.append({"step": "assert", "expr": expr})
    else:
        raise RevlError(filename, getattr(stmt, "line", 0),
                        "unsupported statement in effect block setup",
                        hint="effect block setup is stratum-1 pure code: "
                             "`let`/`var`/`if`/pure calls are allowed (G6)")


def _lower_component_guard_stmts(stmts: list, env: Env, callables: set) -> list[dict]:
    out: list[dict] = []
    for stmt in stmts:
        if isinstance(stmt, FailStmt):
            out.append({
                "step": "fail",
                "message": _lower_component_pure_expr(
                    stmt.message, env, _component_scope(env), callables,
                    pure_only=True,
                ),
            })
        elif isinstance(stmt, IfStmt):
            out.append(_lower_component_if(stmt, env, callables))
        else:
            raise RevlError(env.filename, getattr(stmt, "line", 0),
                            "only `fail` (and nested `if` guards) may appear in a component guard",
                            hint="component `if` is for deliberate L-Raise decisions, not general "
                                 "control flow (G6)")
    return out


def _lower_component_if(stmt: IfStmt, env: Env, callables: set) -> dict:
    scope = _component_scope(env)
    # a guard condition is a `Bool` position, so a hole there is a `Bool`
    # obligation rather than an untyped one (docs/holes.md)
    pin_hole(stmt.cond, "Bool", env.filename, "`if` guard condition")
    step = {
        "step": "if",
        "cond": _lower_component_pure_expr(stmt.cond, env, scope, callables,
                                           pure_only=True),
        "then": _lower_component_guard_stmts(stmt.then, env, callables),
    }
    if stmt.otherwise is not None:
        step["else"] = _lower_component_guard_stmts(stmt.otherwise, env, callables)
    return step


def _config_default_type(value) -> str | None:
    """Surface type of a config-default literal (bool before int: bool is an
    int subclass)."""
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Int"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, str):
        return "Str"
    return None


def _calls_in(node, found: set) -> None:
    """Function/extern names a lowered node calls. Component bodies lower a
    call to `{kind: fn, name}`; pure fn bodies to `{kind: call, callee:
    {kind: var, name}}`."""
    if isinstance(node, dict):
        if node.get("kind") == "fn" and isinstance(node.get("name"), str):
            found.add(node["name"])
        callee = node.get("callee")
        if node.get("kind") == "call" and isinstance(callee, dict) \
                and callee.get("kind") == "var" and isinstance(callee.get("name"), str):
            found.add(callee["name"])
        for value in node.values():
            _calls_in(value, found)
    elif isinstance(node, list):
        for value in node:
            _calls_in(value, found)


def _emitting_fns(fns: list, externs: list, witness: dict | None = None) -> set:
    """Names whose call reaches an irreversible host effect: `emission`
    externs, and functions that reach one transitively. An `acquire` extern
    is *revertible* (it carries an inverse), so it is deliberately not one.

    `witness` (optional, filled in place) records *why* each derived name is
    in the set: `witness[caller] = callee`, the edge that put it there. The
    verdict is unchanged either way — this is only the evidence the fixed
    point would otherwise throw away (why.py)."""
    return set(_emitting_capabilities(fns, externs, witness))


def _emitting_capabilities(fns: list, externs: list,
                           witness: dict | None = None) -> dict[str, set]:
    """`_emitting_fns` refined from a boolean to a *set*: name -> the
    capabilities its call reaches (docs/capabilities.md).

    A host capability is named by the `emission` extern itself — that extern
    *is* the boundary — so `extern emission fn send` contributes `send`, and a
    `fn` contributes the union of what it calls, not its own name. The fixed
    point is the same least one `_emitting_fns` took, now over sets instead of
    a flag, which is why a capability propagates through a chain of `fn`s.

    `witness` is filled in place as the same walk proceeds — the set and the
    derivation come from one traversal, so they cannot disagree."""
    caps: dict[str, set] = {ext["name"]: {ext["name"]}
                            for ext in externs if ext.get("class") == "emission"}
    calls: dict[str, set] = {}
    for fn in fns:
        called: set = set()
        _calls_in(fn.get("body") or [], called)
        calls[fn["name"]] = called

    changed = True
    while changed:  # least fixed point over the call graph
        changed = False
        for name, called in calls.items():
            reached: set = set()
            for callee in called:
                reached |= caps.get(callee, set())
            if reached and not reached <= caps.get(name, set()):
                if witness is not None and name not in caps:
                    # of the callees that prove the point, take the one with
                    # the shortest onward chain (ties by name): the author is
                    # asked to read the shortest derivation, deterministically
                    witness[name] = min(
                        sorted(c for c in called if caps.get(c)),
                        key=lambda callee: _witness_depth(callee, witness))
                caps.setdefault(name, set()).update(reached)
                changed = True
    return caps


def _capability_hint(service: str, method: str, declared, extra: list[str]) -> str:
    """The repair for a provider that exceeds its declared capability set."""
    nameable = [cap for cap in extra if cap != "*"]
    if not nameable:  # nothing to widen *to* — the boundary has no name
        return (f"only a named boundary can be granted — give the emission a "
                f"capability (a required key or an `emission` extern) or declare "
                f"`emission fn {method}(...)` without a scope in service "
                f"`{service}` (G4)")
    widened = list(declared) + [cap for cap in nameable if cap not in declared]
    return (f"a capability-scoped emission bounds *where* a provider may cross "
            f"the boundary — widen the declaration to "
            f"`emission[{', '.join(widened)}] fn {method}(...)` in service "
            f"`{service}`, or route this emission through a declared "
            f"capability (G4)")


def _witness_depth(name: str, witness: dict) -> int:
    """Hops from `name` down to the emission that made it emitting. The
    witness graph is acyclic by construction — an entry is only ever written
    for a name that was *not* yet emitting, pointing at one that was — but
    the guard keeps a malformed map from hanging the compiler."""
    depth, seen = 0, {name}
    while name in witness:
        name = witness[name]
        if name in seen:
            break
        seen.add(name)
        depth += 1
    return depth


def _emission_chain(name: str, witness: dict) -> list[str]:
    """`name` followed to the emission it reaches, e.g.
    ["writeThrough", "audit_log", "audit_write"]."""
    chain, seen = [name], {name}
    while name in witness:
        name = witness[name]
        if name in seen:
            break
        seen.add(name)
        chain.append(name)
    return chain


class _EmissionEvidence:
    """The G4 fixed point's evidence: the witness edge behind each derived
    emitting name, plus enough of the declaration table to give every hop in
    a chain a source location.

    Deliberately a companion of `_emitting_fns` rather than a change to it:
    the set that decides the verdict stays exactly what it was."""

    def __init__(self, program: Program) -> None:
        self.witness: dict[str, str] = {}
        self._files = getattr(program, "decl_files", None) or {}
        self._fallback_file = program.filename
        self._decls: dict[str, object] = {}
        for decl in program.fn_decls:
            self._decls.setdefault(decl.name, decl)
        for decl in program.externs:
            self._decls.setdefault(decl.name, decl)
        self.emission_externs = {
            decl.name for decl in program.externs
            if decl.classification == "emission"
        }

    def locate(self, decl) -> tuple[str | None, int | None]:
        if decl is None:
            return (None, None)
        return (self._files.get(id(decl)) or self._fallback_file or None,
                getattr(decl, "line", None))

    def capabilities_of(self, name: str) -> tuple:
        """Which capabilities `name`'s emission reaches.

        Empty today: G4's `emitting_fns` is a plain set, so "emission" is all
        the analysis knows. This is the single seam for the capability-set
        work — the moment a declaration carries a `capabilities` attribute
        every why-trace starts showing it, in the rendering and in the JSON
        alike, with no other edit (see `TraceStep.capabilities`)."""
        return tuple(getattr(self._decls.get(name), "capabilities", ()) or ())

    def step_for(self, name: str, last: bool) -> TraceStep:
        decl = self._decls.get(name)
        file, line = self.locate(decl)
        emission = last or name in self.emission_externs
        return TraceStep(name, "emission" if emission else "call", file, line,
                         "emission" if emission else None,
                         self.capabilities_of(name))

    def chain_steps(self, name: str) -> list[TraceStep]:
        chain = _emission_chain(name, self.witness)
        return [self.step_for(hop, index == len(chain) - 1)
                for index, hop in enumerate(chain)]


def _method_emissions(body: list, env: "Env",
                      steps_out: dict | None = None) -> tuple[list[str], set]:
    """What calling a provide-method irreversibly causes: `emit` steps and
    reachable emitting functions/externs. Teardown-position emissions count
    — calling the method schedules them.

    Returns `(evidence, capabilities)`: the human-readable call sites, and the
    *set* of boundaries they cross (docs/capabilities.md). A call through a
    required key `db` is capability `db` — the key is composition-wide (G2
    makes it unique), so it names the same boundary to every reader; host code
    is named by the `emission` extern it reaches.

    `steps_out` (optional, filled in place) maps each returned label to the
    derivation behind it: a list of `TraceStep`s running from the callee the
    method names down to the emission it reaches. Additive out-parameter for
    the same reason `_emitting_fns` takes one — the labels, and so the
    message, are byte-identical whether or not evidence is collected."""
    found: list[str] = []
    seen: set = set()
    caps: set = set()

    def note(label: str, steps: list | None = None) -> None:
        if label not in seen:
            seen.add(label)
            found.append(label)
            if steps_out is not None and steps is not None:
                steps_out[label] = steps

    def service_emission_step(local: str | None, method: str | None) -> list:
        """The terminal step for `local.method`, an `emission fn` on the
        service bound to `local`."""
        service = env.services.get(env.requires.get(local) or "")
        decl = service.methods.get(method) if service is not None else None
        evidence = getattr(env, "emission_evidence", None)
        # a MethodDecl has a line but no file of its own; its service does
        file, _ = evidence.locate(service) if evidence is not None else (None, None)
        line = getattr(decl, "line", None)
        label = f"{local}.{method}"
        detail = "emission" if service is None else f"emission `{service.name}.{method}`"
        # same seam as `_EmissionEvidence.capabilities_of`, for the service
        # side: whatever the operation declares it may reach, the trace shows
        capabilities = tuple(getattr(decl, "capabilities", ()) or ())
        return [TraceStep(label, "emission", file, line, detail, capabilities)]

    def walk(node):
        if isinstance(node, dict):
            if node.get("step") == "emit":
                expr = node.get("expr") or {}
                target = expr.get("target") or {}
                if target.get("kind") == "req":
                    note(f"{target.get('name')}.{expr.get('method')}",
                         service_emission_step(target.get("name"), expr.get("method")))
                    caps.add(target.get("name"))
                else:
                    note("a host emission")
                    # every host emission reaches a named `emission` extern
                    # (`_is_emission_call` admits nothing else); `*` is the
                    # unreachable-in-practice fallback, and it is deliberately
                    # a capability no `emission[...]` list can name, so an
                    # unnameable boundary fails the bound rather than passing it
                    reached: set = set()
                    _calls_in(expr, reached)
                    if not reached & env.emitting_fns:
                        caps.add("*")
            # an emission may also appear in value position (`let r = emit …`)
            target = node.get("target")
            if node.get("kind") == "call" and isinstance(target, dict) \
                    and target.get("kind") == "req":
                service = env.services.get(env.requires.get(target.get("name")) or "")
                decl = (service.methods.get(node.get("method"))
                        if service is not None else None)
                if decl is not None and decl.emission:
                    note(f"{target.get('name')}.{node.get('method')}",
                         service_emission_step(target.get("name"), node.get("method")))
                    caps.add(target.get("name"))
            calls: set = set()
            _calls_in(node, calls)
            for name in sorted(calls & env.emitting_fns):
                evidence = getattr(env, "emission_evidence", None)
                note(f"{name}()",
                     evidence.chain_steps(name) if evidence is not None else None)
                caps.update(env.emitting_caps.get(name) or {"*"})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    return found, caps


def _lower_component(comp: ComponentDecl, services: dict[str, ServiceDecl], filename: str,
                     callables: set | None = None, types: dict | None = None,
                     emitting_fns: set | None = None,
                     emitting_caps: dict | None = None,
                     emission_evidence: "_EmissionEvidence | None" = None) -> dict:
    env = Env(comp, services, filename, types)
    env.emitting_fns = emitting_fns or set()
    env.emitting_caps = emitting_caps or {}
    env.emission_evidence = emission_evidence
    env.callables = callables or set()  # module fns/externs/hosts for unified expressions

    # a config default must fit its declared field type (config typing);
    # a `null` default is the documented optional exception and is allowed
    for cfg in comp.config:
        if cfg.default is None:
            continue
        lit_type = _config_default_type(cfg.default)
        if lit_type is not None and not compatible(cfg.type, lit_type):
            raise mismatch(filename, cfg.line,
                           f"config field `{cfg.name}` default", cfg.type, lit_type)

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
            if stmt.setup:
                saved_locals = dict(env.locals)
                saved_taken = set(env._taken)
                saved_type_env = dict(env.type_env)
                setup_steps: list[dict] = []
                scope = _component_scope(env)
                mutables: set[str] = set()
                for setup_stmt in stmt.setup:
                    _lower_component_setup_stmt(setup_stmt, env, scope, callables or set(),
                                                mutables, setup_steps)
                acquire = _lower_component_pure_expr(stmt.acquire, env, scope, callables or set())
                env.locals = saved_locals
                env._taken = saved_taken
                # setup-let types are block-scoped; drop them so a recycled safe
                # name (env._taken is restored above) can't read a stale type
                env.type_env = saved_type_env
            else:
                setup_steps = []
                acquire = _lower_expr(stmt.acquire, env, mode="setup")
            safe = env.bind_local(stmt.bind, stmt.line)
            acquired_type = infer_ir(acquire, env.type_env, env.types, env.services)
            if acquired_type is not None:
                env.type_env[safe] = acquired_type
            undo = _lower_expr(stmt.undo, env, mode="undo")
            step = {"step": "let-effect", "bind": safe, "acquire": acquire, "undo": undo}
            if setup_steps:
                step["setup"] = setup_steps
            body.append(step)
        elif isinstance(stmt, EffectStmt):
            if stmt.setup:
                saved_locals = dict(env.locals)
                saved_taken = set(env._taken)
                saved_type_env = dict(env.type_env)
                setup_steps = []
                scope = _component_scope(env)
                mutables = set()
                for setup_stmt in stmt.setup:
                    _lower_component_setup_stmt(setup_stmt, env, scope, callables or set(),
                                                mutables, setup_steps)
                acquire = _lower_component_pure_expr(stmt.acquire, env, scope, callables or set())
                env.locals = saved_locals
                env._taken = saved_taken
                env.type_env = saved_type_env
            else:
                setup_steps = []
                acquire = _lower_expr(stmt.acquire, env, mode="setup")
            step = {"step": "effect", "acquire": acquire,
                    "undo": _lower_expr(stmt.undo, env, mode="undo")}
            if setup_steps:
                step["setup"] = setup_steps
            body.append(step)
        elif isinstance(stmt, FailStmt):
            body.append({
                "step": "fail",
                "message": _lower_component_pure_expr(
                    stmt.message, env, _component_scope(env), callables or set(),
                    pure_only=True,
                ),
            })
        elif isinstance(stmt, IfStmt):
            body.append(_lower_component_if(stmt, env, callables or set()))
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
        if method.async_ != decl.async_:
            raise RevlError(
                filename, method.line,
                f"method `{method.name}` of provision `{stmt.key}` is "
                f"{'async' if method.async_ else 'not async'} but service {svc.name} "
                f"declares it {'async' if decl.async_ else 'not async'}",
            )
        if method.name in implemented:
            raise RevlError(filename, method.line, f"duplicate method `{method.name}` in provision `{stmt.key}`")
        implemented.add(method.name)

        # optional param annotations (syntax-2.0: models write `fn query(sql:
        # Str)` on autopilot): well-formed and checked against the service's
        # declared type (A6 — the service is the source of truth).
        for surface, annotation, (_, svc_ptype) in zip(
            method.params, method.param_types or [None] * len(method.params), decl.params
        ):
            if annotation is None:
                continue
            check_type_wellformed(filename, method.line, annotation)
            if svc_ptype and not (compatible(svc_ptype, annotation)
                                  and compatible(annotation, svc_ptype)):
                raise mismatch(
                    filename, method.line,
                    f"parameter `{surface}` of `{method.name}` (from service `{svc.name}`)",
                    svc_ptype, annotation)
        # optional `-> T` return annotation, checked against the service
        if method.returns is not None:
            check_type_wellformed(filename, method.line, method.returns)
            if decl.returns and not (compatible(decl.returns, method.returns)
                                     and compatible(method.returns, decl.returns)):
                raise mismatch(
                    filename, method.line,
                    f"return type of `{method.name}` (from service `{svc.name}`)",
                    decl.returns, method.returns)

        saved = env.params
        env.params = env.bind_params(method.params, method.line)
        # method params carry the service's declared types (A6): surface
        # names bind the body, the service contributes the signature
        saved_tenv = dict(env.type_env)
        method_locals: dict[str, str] = {}
        for surface, (_, ptype) in zip(method.params, decl.params):
            env.type_env[env.params[surface]] = ptype
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
            elif isinstance(mstmt, AwaitStmt):
                if not decl.async_:
                    raise RevlError(
                        filename, mstmt.line,
                        "`await` is only allowed in a component body",
                        hint="a provide method runs while the component is ACTIVE; iteration "
                             "boundaries (paper §4.3.2) exist only during activation (A1)",
                    )
                mbody.append({"step": "await", "expr": _lower_expr(mstmt.expr, env, mode="setup")})
            elif isinstance(mstmt, LetStmt):
                # a plain value binding: name an intermediate result instead
                # of nesting every call into a single expression
                if mstmt.name in env.params or mstmt.name in method_locals:
                    raise RevlError(filename, mstmt.line,
                                    f"`{mstmt.name}` is already bound in `{method.name}`")
                safe = _safe_name(mstmt.name, set(env.params.values()) | set(method_locals.values()))
                value = _lower_expr(mstmt.value, env, mode="setup")
                method_locals[mstmt.name] = safe
                env.params[mstmt.name] = safe  # visible to later statements
                if mstmt.type is not None:
                    # a `let x: T` annotation in a provide-method body. It is
                    # recorded rather than ignored so `infer_ir` sees it, but
                    # this is stratum 3: the value is *not* checked against it
                    # (docs/function-types.md §limits).
                    check_type_wellformed(filename, mstmt.line, mstmt.type)
                    env.type_env[safe] = mstmt.type
                mbody.append({"step": "let", "name": safe, "value": value,
                              "mutable": bool(mstmt.mutable)})
            elif isinstance(mstmt, AssignStmt):
                if mstmt.name not in method_locals:
                    raise RevlError(filename, mstmt.line,
                                    f"`{mstmt.name}` is not declared in `{method.name}`",
                                    hint="declare it with `let` (single-assignment) or "
                                         "`var` (mutable)")
                mbody.append({"step": "assign", "name": method_locals[mstmt.name],
                              "value": _lower_expr(mstmt.value, env, mode="setup")})
            elif isinstance(mstmt, ReturnStmt):
                if mstmt.expr is None:
                    # a void operation: `fn f(x) { return }`
                    if decl.returns:
                        raise RevlError(
                            filename, mstmt.line,
                            f"`{method.name}` returns `{decl.returns}` but this "
                            "`return` carries no value")
                    mbody.append({"step": "return", "expr": None})
                    returned = True
                    continue
                # a hole in return position takes the *service's* declared
                # return type: the service is the source of truth for the
                # signature (A6), so it is also the source of the obligation
                pin_hole(mstmt.expr, decl.returns, filename,
                         f"`{method.name}` returns")
                lowered_return = _lower_expr(mstmt.expr, env, mode="setup")
                if decl.returns:
                    actual = infer_ir(lowered_return, env.type_env, env.types, env.services)
                    if actual and not compatible(decl.returns, actual):
                        raise mismatch(filename, mstmt.line,
                                       f"`{method.name}` returns", decl.returns, actual)
                    lowered_return = _inject_opt(decl.returns, actual, lowered_return)
                mbody.append({"step": "return", "expr": lowered_return})
                returned = True
            else:  # pragma: no cover
                raise RevlError(filename, mstmt.line, "unexpected statement in method body")
        if decl.returns and not returned:
            # same guarantee as a `fn` with a declared return, on the other
            # surface that has one: the emitted java method and rust trait impl
            # both fall off the end ("missing return statement" / E0308) while
            # python hands the caller a silent None
            raise RevlError(
                comp.source or filename, method.line,
                f"`{method.name}` implements `{svc.name}.{method.name}`, which "
                f"returns `{render_type(decl.returns)}`, but this body never "
                f"returns a value",
                hint=f"end the body with `return <{render_type(decl.returns)}>` — a "
                     f"provider must produce what its service promises, or "
                     f"consumers bound to `{svc.name}` receive nothing (rust E0308, "
                     'java "missing return statement")',
                code="T1", category="type-mismatch",
                expected=decl.returns, actual=None,
            )
        safe_params = [env.params[p] for p in method.params]
        env.params = saved
        env.type_env = saved_tenv

        # A service declaration is an *upper bound* on its providers' effects:
        # consumers bind to the service, not to this component, and a provider
        # may be purer than declared but never less. Without this, a plain
        # declaration hides an irreversible call from every consumer — and from
        # the G8 audit, which enumerates a caller's emissions by reading the
        # declarations of the methods it calls.
        if not decl.emission:
            caused_steps: dict[str, list] = {}
            caused, _caps = _method_emissions(mbody, env, caused_steps)
            if caused:
                evidence = ", ".join(f"`{item}`" for item in caused)
                # the derivation for the *first* culprit: the message already
                # names them all, and one worked example is what the author
                # needs to see. The chain runs method -> ... -> emission.
                chain = caused_steps.get(caused[0]) or []
                why = None
                if chain:
                    head = TraceStep(method.name, "provide-method",
                                     comp.source or filename, method.line,
                                     f"provision `{stmt.key}`")
                    why = WhyTrace(kind="emission-propagation",
                                   subject=f"{svc.name}.{method.name}",
                                   steps=[head, *chain], shape=CHAIN)
                raise RevlError(
                    # the offending body lives in the component's own file,
                    # which is not the merged program filename
                    comp.source or filename, method.line,
                    f"`{svc.name}.{method.name}` is declared plain, but this "
                    f"implementation reaches {evidence}",
                    hint=f"a service declaration bounds what its providers may do — "
                         f"mark it `emission fn {method.name}(...)` in service "
                         f"`{svc.name}`, or move the irreversible call out of this "
                         f"method (G4)",
                    code="G4", category="emission-propagation",
                    why=why,
                )
        elif decl.capabilities is not None:
            # the same upper bound, one refinement finer: `emission[db]` says
            # *where* a provider may cross, so the body's capability set must
            # be a subset. A provider using fewer capabilities is purer than
            # declared, which is the sound direction (docs/capabilities.md).
            caused, used = _method_emissions(mbody, env)
            extra = sorted(used - set(decl.capabilities))
            if extra:
                declared = ", ".join(decl.capabilities)
                offending = ", ".join(
                    "an unnameable host boundary" if cap == "*" else f"`{cap}`"
                    for cap in extra)
                evidence = ", ".join(f"`{item}`" for item in caused)
                raise RevlError(
                    comp.source or filename, method.line,
                    f"`{svc.name}.{method.name}` is declared "
                    f"`emission[{declared}]`, but this implementation emits "
                    f"through {offending}"
                    + (f" (reaching {evidence})" if evidence else ""),
                    hint=_capability_hint(svc.name, method.name,
                                          decl.capabilities, extra),
                    code="G4", category="emission-capability",
                )

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
    # an `emission` extern (or a function reaching one) is a boundary
    # crossing exactly as a service emission is, so `emit` marks it too
    if node.get("kind") == "fn" and node.get("name") in getattr(env, "emitting_fns", ()):
        return True
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
        if expr.value is None:
            raise null_error(env.filename, expr.line)
        return {"kind": "lit", "value": expr.value}
    if isinstance(expr, Interp):
        template = []
        args = []
        for kind, value in expr.parts:
            if kind == "text":
                template.append(value.replace("$", "$$"))  # A4: literal dollars
            else:
                template.append(f"${len(args)}")
                # `value` is a full expression AST; lower it in this context
                args.append(_lower_expr(value, env, mode))
        return {"kind": "format", "template": "".join(template), "args": args}
    if isinstance(expr, Postfix):
        return _lower_postfix(expr, env, mode)
    # v2.0 unified grammar: component positions carry the full pure-expression
    # stratum; the mode/context ride on env so the shared lowering needs no
    # signature churn (finding 1 — strata compose)
    saved = getattr(env, "_expr_mode", None), getattr(env, "_plain_body", None)
    env._expr_mode, env._plain_body = mode, True
    try:
        return _lower_component_pure_expr(
            expr, env, _component_scope(env), getattr(env, "callables", set()))
    finally:
        env._expr_mode, env._plain_body = saved


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
        host_args = [_lower_expr(a, env, mode) for a in call.args]
        host_check(f"{head}.{call.name}",
                   [infer_ir(a, env.type_env, env.types, env.services)
                    for a in host_args],
                   env.filename, expr.line)
        node = {"kind": "host", "fn": f"{head}.{call.name}", "args": host_args}
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
            lowered = [_lower_expr(a, env, mode) for a in op.args]
            for arg, (pname, ptype) in zip(lowered, decl.params):
                actual = infer_ir(arg, env.type_env, env.types, env.services)
                if ptype and actual and not compatible(ptype, actual):
                    raise mismatch(env.filename, op.line,
                                   f"`{node['name']}.{op.name}` argument `{pname}`",
                                   ptype, actual)
            node = {"kind": "call", "target": node, "method": op.name, "args": lowered}
            continue
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

    # why-trace locations: a locally compiled component knows its own file and
    # declaration line; an *ambient* entry (read back from a running manifest)
    # has a file but no line, so `line` stays None there rather than lying.
    by_name = {entry["name"]: entry for entry in entries}

    def _where(name: str) -> tuple[str | None, int | None]:
        entry = by_name.get(name) or {}
        file = entry.get("file") or program.filename or None
        return (file, lines.get(name))

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
                first = provider_of[(key, realm)]
                # both exhibits, both locations: the message names them, the
                # trace says where to go and look (why.py)
                detail = f"provides `{key}`{where}"
                why = WhyTrace(
                    kind="provision-conflict", subject=key, shape=SET,
                    steps=[
                        TraceStep(first, "provider", *_where(first), detail),
                        TraceStep(entry["name"], "provider",
                                  *_where(entry["name"]), detail),
                    ])
                raise RevlError(
                    program.filename, _line(entry["name"]),
                    f"provision conflict: key `{key}`{where} is provided "
                    f"by both {provider_of[(key, realm)]} and {entry['name']} (G2)",
                    why=why,
                )
            provider_of[(key, realm)] = entry["name"]

    # edges: provider -> consumer where the consumer's realm for a key
    # matches the provider's — realm separation legitimately breaks cycles
    graph: dict[str, list[str]] = {entry["name"]: [] for entry in entries}
    indegree: dict[str, int] = {entry["name"]: 0 for entry in entries}
    # which key carries each provider -> consumer edge, so a cycle can say
    # *what* is being waited on at every hop and not just who waits
    edge_key: dict[tuple[str, str], str] = {}
    for entry in entries:
        for key in entry["inject"]:
            provider = provider_of.get((key, _realm(entry, key)))
            if provider == entry["name"]:
                name = entry["name"]
                raise RevlError(
                    program.filename, _line(name),
                    f"component {name} requires a key it provides itself (`{key}`) (G3)",
                    why=WhyTrace(
                        kind="dependency-cycle", subject=name, shape=CHAIN,
                        steps=[TraceStep(name, "component", *_where(name),
                                         f"provides and requires `{key}`")]),
                )
            if provider is not None:
                graph[provider].append(entry["name"])
                edge_key.setdefault((provider, entry["name"]), key)
                indegree[entry["name"]] += 1

    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(name: str):
        state[name] = 1
        stack.append(name)
        for succ in graph[name]:
            if state.get(succ) == 1:
                cycle = stack[stack.index(succ):] + [succ]
                # one step per hop: each names the key it provides onward, so
                # the closing repeat shows the edge that shuts the loop
                steps = [
                    TraceStep(node, "component", *_where(node),
                              (f"provides `{edge_key[(node, cycle[i + 1])]}`"
                               if i + 1 < len(cycle) else None))
                    for i, node in enumerate(cycle)
                ]
                raise RevlError(program.filename, _line(succ),
                                "dependency cycle: " + " -> ".join(cycle) + " (G3)",
                                why=WhyTrace(kind="dependency-cycle", subject=succ,
                                             steps=steps, shape=CHAIN))
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

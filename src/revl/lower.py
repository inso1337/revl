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
from .typecheck import (
    CASES_KEY,
    FNS_KEY,
    check_ast,
    check_type_wellformed,
    compatible,
    infer_ast,
    infer_ir,
    mismatch,
    null_error,
    parse_type,
)
from .parser import (
    AssertStmt,
    AssignStmt,
    AwaitStmt,
    ComponentDecl,
    EffectStmt,
    EmitStmt,
    ExternDecl,
    FailStmt,
    ExprArrow,
    ExprBin,
    ExprCall,
    ExprField,
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
    ListPattern,
    Lit,
    Postfix,
    Program,
    ProvideStmt,
    RecordPattern,
    ReturnStmt,
    ServiceDecl,
    TestDecl,
    TypeDecl,
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
        scope: dict[str, bool] = {}
        type_env: dict[str, str] = {}
        for param in decl.params:
            if param.name in scope:
                raise RevlError(filename, param.line,
                                f"duplicate parameter `{param.name}` in fn {decl.name}")
            scope[param.name] = False
            type_env[param.name] = param.type
        module_callables = program.fn_scopes.get(id(decl), default_callables)
        callables = _HOST_CALLABLES | _BUILTIN_CONSTRUCTORS | set(module_callables) | {ext.name for ext in program.externs}
        alias_fns = program.fn_alias_scopes.get(id(decl), {})
        body: list[dict] = []
        for stmt in decl.body:
            _lower_pure_stmt(stmt, scope, callables, alias_fns, body, filename, type_env, types,
                             expected_return=decl.returns)
        entry = {
            "name": decl.name,
            "params": [{"name": p.name, "type": p.type} for p in decl.params],
            "returns": decl.returns,
            "public": decl.public,
            "body": body,
        }
        if decl.verified:
            entry["verified"] = True
        fns.append(entry)
    return fns


def _signature_table(program: Program) -> dict:
    """{name: {"params": [type...], "returns": type|None}} for fns + externs."""
    sigs: dict = {}
    for fn_decl in program.fn_decls:
        sigs[fn_decl.name] = {"params": [p.type for p in fn_decl.params],
                              "returns": fn_decl.returns}
    for ext in program.externs:
        sigs[ext.name] = {"params": [p.type for p in ext.params],
                          "returns": ext.returns}
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


def _check_match_exhaustiveness(expr: ExprMatch, type_env: dict, types: dict, filename: str) -> None:
    type_name = _expr_static_type(expr.scrutinee, type_env, types)
    spec = types.get(type_name or "")
    if spec is None or spec.get("kind") != "variant":
        return
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


def _lower_tests(program: Program, filename: str) -> list:
    """Lower `test` blocks to IR v3 test units (syntax-2.0 §7)."""
    if not program.tests:
        return []
    callables = _HOST_CALLABLES | _BUILTIN_CONSTRUCTORS | {fn.name for fn in program.fn_decls}
    tests: list[dict] = []
    seen: set[str] = set()
    for decl in program.tests:
        if decl.name in seen:
            raise RevlError(filename, decl.line, f"duplicate test `{decl.name}`")
        seen.add(decl.name)
        scope: dict[str, bool] = {}
        body: list[dict] = []
        for stmt in decl.body:
            _lower_pure_stmt(stmt, scope, callables, {}, body, filename)
        tests.append({"name": decl.name, "body": body})
    return tests


def _bool_cond(expr, type_env: dict, types: dict, filename: str, where: str) -> None:
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
        inferred = infer_ast(value, type_env, types, filename)
        declared = type_env.get(stmt.name)
        if declared and inferred and not compatible(declared, inferred):
            raise mismatch(filename, stmt.line,
                           f"assignment to `{stmt.name}` (a `{declared}` variable)",
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
        body.append({"step": "return",
                     "expr": None if stmt.expr is None else _lower_pure_expr(stmt.expr, scope, callables, alias_fns, filename, type_env, types)})
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
                            f"`for ... of` iterates a `List[...]`, got `{iter_diag}`")
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
                    f"record destructuring requires a record, but `{value_type}` is not a record",
                )
            fields = spec.get("fields", {})
            for name in names:
                if name not in fields:
                    raise RevlError(filename, pattern.line,
                                    f"`{name}` is not a field of record `{value_type}`")
        elif value_type is not None and parse_type(value_type)[0] in _BUILTIN_NONRECORD:
            raise RevlError(
                filename, stmt.line,
                f"record destructuring requires a record, but `{value_type}` is not a record",
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
                f"list destructuring requires a `List[...]`, but `{value_type}` is not a list",
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


def _lower_pure_expr(expr, scope: dict, callables: set, alias_fns: dict, filename: str,
                     type_env: dict | None = None, types: dict | None = None) -> dict:
    type_env = type_env if type_env is not None else {}
    types = types if types is not None else {}
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
        for param in expr.params:
            inner[param] = False
            inner_type_env.pop(param, None)
        captures = sorted(_mutable_free_vars(expr.body, scope, set(expr.params)))
        return {"kind": "arrow", "params": expr.params, "captures": captures,
                "body": _lower_pure_expr(expr.body, inner, callables, alias_fns, filename, inner_type_env, types)}
    if isinstance(expr, ExprMatch):
        scrutinee_type = _expr_static_type(expr.scrutinee, type_env, types)
        _check_match_exhaustiveness(expr, type_env, types, filename)
        scrutinee = _lower_pure_expr(expr.scrutinee, scope, callables, alias_fns, filename, type_env, types)
        arms = []
        for pattern, bind, body in expr.arms:
            inner_scope = dict(scope)
            inner_type_env = dict(type_env)
            if bind is not None:
                inner_scope[bind] = False
                inner_type_env.pop(bind, None)
                payload_type = _variant_case_payload(types, scrutinee_type, pattern)
                if payload_type is not None:
                    inner_type_env[bind] = payload_type
            arms.append({
                "pattern": pattern,
                "bind": bind,
                "body": _lower_pure_expr(body, inner_scope, callables, alias_fns, filename, inner_type_env, types),
            })
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
        # G1: names interpolated in `${name}` (or `${a.b.c}`) are real
        # references and must resolve, exactly like a bare ExprVar (the
        # component-body path already checks these via _lower_postfix).
        # Only the head of a dotted chain is a scope name; the tail is
        # field access, which the backend f-string emits verbatim.
        for kind, value in expr.parts:
            if kind == "var":
                head = value.split(".", 1)[0]
                if head not in scope and head not in callables:
                    raise RevlError(filename, getattr(expr, "line", 1),
                                    f"`{head}` is not declared in this function",
                                    hint="declare it with `let`/`var` or add it as a parameter (G1)")
        return {"kind": "interp", "parts": expr.parts}
    raise RevlError(filename, getattr(expr, "line", 1), "unexpected expression in fn body")


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
    _validate_declared_types(program, program.filename)
    types = _lower_type_decls(program, program.filename)
    types[FNS_KEY] = _signature_table(program)
    types[CASES_KEY] = _case_table(types)
    fns = _lower_fns(program, program.filename, types)
    externs = _lower_externs(program, program.filename)
    tests = _lower_tests(program, program.filename)

    components = []
    seen = set()
    for comp in program.components:
        if comp.name in seen:
            raise RevlError(program.filename, comp.line, f"duplicate component `{comp.name}`")
        seen.add(comp.name)
        components.append(_lower_component(comp, services, program.filename, component_callables,
                                           types))

    manifest = _link(program, components, ambient.get("components") or [])

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
        )
    return ServiceDecl(name, methods, 0, commutative=bool(spec.get("commutative")))


def _service_equal(a: ServiceDecl, b: ServiceDecl) -> bool:
    def shape(svc: ServiceDecl):
        return (
            svc.commutative,
            {
                m.name: (tuple(m.params), m.returns, m.emission, m.async_, m.commutative)
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
                return {"kind": "call",
                        "target": {"kind": "name", "id": scope[root]},
                        "method": method, "args": args}
        if isinstance(expr.callee, ExprVar) and expr.callee.name in callables:
            return {"kind": "fn", "name": expr.callee.name, "args": args}
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
        return {"kind": "arrow", "params": expr.params,
                "body": _lower_component_pure_expr(expr.body, env, scope, callables,
                                                   pure_only)}
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
    if isinstance(stmt, LetStmt):
        safe = env.bind_local(stmt.name, stmt.line)
        scope[stmt.name] = safe
        if stmt.mutable:
            mutables.add(stmt.name)
        out.append({
            "step": "let",
            "name": safe,
            "value": _lower_component_pure_expr(stmt.value, env, scope, callables,
                                                pure_only=True),
        })
    elif isinstance(stmt, AssignStmt):
        if stmt.name not in scope:
            raise RevlError(filename, stmt.line,
                            f"`{stmt.name}` is not declared in this effect block",
                            hint="declare it with `let`/`var` first (G1)")
        if stmt.name not in mutables:
            raise RevlError(filename, stmt.line,
                            f"cannot reassign `{stmt.name}` — it is `let` (single-assignment)",
                            hint="declare it with `var` to make it mutable (syntax-2.0 §3.5)")
        out.append({
            "step": "assign",
            "name": scope[stmt.name],
            "value": _lower_component_pure_expr(stmt.value, env, scope, callables,
                                                pure_only=True),
        })
    elif isinstance(stmt, ExprStmt):
        out.append({
            "step": "expr",
            "expr": _lower_component_pure_expr(stmt.expr, env, scope, callables,
                                               pure_only=True),
        })
    elif isinstance(stmt, IfStmt):
        then: list[dict] = []
        for s in stmt.then:
            _lower_component_setup_stmt(s, env, scope, callables, mutables, then)
        otherwise = None
        if stmt.otherwise is not None:
            otherwise = []
            for s in stmt.otherwise:
                _lower_component_setup_stmt(s, env, scope, callables, mutables, otherwise)
        out.append({
            "step": "if",
            "cond": _lower_component_pure_expr(stmt.cond, env, scope, callables,
                                               pure_only=True),
            "then": then,
        })
        if otherwise is not None:
            out[-1]["else"] = otherwise
    elif isinstance(stmt, AssertStmt):
        out.append({
            "step": "assert",
            "expr": _lower_component_pure_expr(stmt.expr, env, scope, callables,
                                               pure_only=True),
        })
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


def _lower_component(comp: ComponentDecl, services: dict[str, ServiceDecl], filename: str,
                     callables: set | None = None, types: dict | None = None) -> dict:
    env = Env(comp, services, filename, types)
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
                setup_steps: list[dict] = []
                scope = _component_scope(env)
                mutables: set[str] = set()
                for setup_stmt in stmt.setup:
                    _lower_component_setup_stmt(setup_stmt, env, scope, callables or set(),
                                                mutables, setup_steps)
                acquire = _lower_component_pure_expr(stmt.acquire, env, scope, callables or set())
                env.locals = saved_locals
                env._taken = saved_taken
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
                setup_steps = []
                scope = _component_scope(env)
                mutables = set()
                for setup_stmt in stmt.setup:
                    _lower_component_setup_stmt(setup_stmt, env, scope, callables or set(),
                                                mutables, setup_steps)
                acquire = _lower_component_pure_expr(stmt.acquire, env, scope, callables or set())
                env.locals = saved_locals
                env._taken = saved_taken
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

        saved = env.params
        env.params = env.bind_params(method.params, method.line)
        # method params carry the service's declared types (A6): surface
        # names bind the body, the service contributes the signature
        saved_tenv = dict(env.type_env)
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
            elif isinstance(mstmt, ReturnStmt):
                lowered_return = _lower_expr(mstmt.expr, env, mode="setup")
                if decl.returns:
                    actual = infer_ir(lowered_return, env.type_env, env.types, env.services)
                    if actual and not compatible(decl.returns, actual):
                        raise mismatch(filename, mstmt.line,
                                       f"`{method.name}` returns", decl.returns, actual)
                mbody.append({"step": "return", "expr": lowered_return})
                returned = True
            else:  # pragma: no cover
                raise RevlError(filename, mstmt.line, "unexpected statement in method body")
        safe_params = [env.params[p] for p in method.params]
        env.params = saved
        env.type_env = saved_tenv
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
                args.append(_lower_expr(Postfix(value, [], expr.line), env, mode))
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

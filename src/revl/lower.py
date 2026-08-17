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
    AssertStmt,
    AssignStmt,
    AwaitStmt,
    ComponentDecl,
    EffectStmt,
    EmitStmt,
    ExternDecl,
    ExprArrow,
    ExprBin,
    ExprCall,
    ExprField,
    ExprIf,
    ExprIndex,
    ExprList,
    ExprLit,
    ExprMatch,
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


# ---------------------------------------------------------------------------
# v2.0: type declarations & pure functions (docs/syntax-2.0.md §2–§3)
# ---------------------------------------------------------------------------

# host roots a pure fn may call without an explicit binding (DESIGN §7 builtins)
_HOST_CALLABLES = {"Map", "Pool", "Job"}
# Opt/Result constructors, recognized so `Some(x)`/`Ok(r)` resolve (syntax-2.0 §2)
_BUILTIN_CONSTRUCTORS = {"Some", "None", "Ok", "Err"}


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
            _lower_pure_stmt(stmt, scope, callables, alias_fns, body, filename, type_env, types)
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


def _expr_static_type(expr, type_env: dict, types: dict) -> str | None:
    """Best-effort static type of a pure expression.

    match exhaustiveness is a best-effort check: when the scrutinee's type is
    not recoverable from the local type environment, lowering still proceeds
    (the Python emitter adds a runtime fallback for those cases).
    """
    if isinstance(expr, ExprVar):
        return type_env.get(expr.name)
    if isinstance(expr, ExprField):
        target = _expr_static_type(expr.target, type_env, types)
        spec = types.get(target or "")
        if spec is not None and spec.get("kind") == "record":
            return spec.get("fields", {}).get(expr.name)
        return None
    if isinstance(expr, ExprLit):
        if isinstance(expr.value, bool):
            return "Bool"
        if isinstance(expr.value, int):
            return "Int"
        if isinstance(expr.value, float):
            return "Float"
        if isinstance(expr.value, str):
            return "Str"
        return None
    if isinstance(expr, ExprList):
        if not expr.items:
            return "List[Never]"
        item_type = _expr_static_type(expr.items[0], type_env, types)
        return f"List[{item_type or 'Any'}]"
    return None


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


def _lower_pure_stmt(stmt, scope: dict, callables: set, alias_fns: dict, body: list, filename: str,
                     type_env: dict | None = None, types: dict | None = None) -> None:
    type_env = type_env if type_env is not None else {}
    types = types if types is not None else {}
    if isinstance(stmt, LetStmt):
        if stmt.name in scope:
            raise RevlError(filename, stmt.line, f"`{stmt.name}` is already declared in this function")
        scope[stmt.name] = stmt.mutable
        inferred = _expr_static_type(stmt.value, type_env, types)
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
        inferred = _expr_static_type(value, type_env, types)
        if inferred is not None:
            type_env[stmt.name] = inferred
        body.append({"step": "assign", "name": stmt.name,
                     "value": _lower_pure_expr(value, scope, callables, alias_fns, filename, type_env, types)})
    elif isinstance(stmt, ReturnStmt):
        body.append({"step": "return",
                     "expr": None if stmt.expr is None else _lower_pure_expr(stmt.expr, scope, callables, alias_fns, filename, type_env, types)})
    elif isinstance(stmt, IfStmt):
        then: list[dict] = []
        for s in stmt.then:
            _lower_pure_stmt(s, scope, callables, alias_fns, then, filename, type_env, types)
        otherwise = None
        if stmt.otherwise is not None:
            otherwise = []
            for s in stmt.otherwise:
                _lower_pure_stmt(s, scope, callables, alias_fns, otherwise, filename, type_env, types)
        body.append({"step": "if", "cond": _lower_pure_expr(stmt.cond, scope, callables, alias_fns, filename, type_env, types),
                     "then": then, "else": otherwise})
    elif isinstance(stmt, WhileStmt):
        cond = _lower_pure_expr(stmt.cond, scope, callables, alias_fns, filename, type_env, types)
        inner_scope = dict(scope)
        inner_type_env = dict(type_env)
        inner_body: list[dict] = []
        for s in stmt.body:
            _lower_pure_stmt(s, inner_scope, callables, alias_fns, inner_body, filename, inner_type_env, types)
        body.append({"step": "while", "cond": cond, "body": inner_body})
    elif isinstance(stmt, ForStmt):
        if stmt.bind in scope:
            raise RevlError(filename, stmt.line, f"`{stmt.bind}` is already declared in this function")
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
            _lower_pure_stmt(s, inner_scope, callables, alias_fns, inner_body, filename, inner_type_env, types)
        body.append({"step": "for", "bind": stmt.bind, "iterable": iterable, "body": inner_body})
    elif isinstance(stmt, ExprStmt):
        body.append({"step": "expr", "expr": _lower_pure_expr(stmt.expr, scope, callables, alias_fns, filename, type_env, types)})
    elif isinstance(stmt, AssertStmt):
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


def _lower_pure_expr(expr, scope: dict, callables: set, alias_fns: dict, filename: str,
                     type_env: dict | None = None, types: dict | None = None) -> dict:
    type_env = type_env if type_env is not None else {}
    types = types if types is not None else {}
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
    if isinstance(expr, Interp):
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

    components = []
    seen = set()
    for comp in program.components:
        if comp.name in seen:
            raise RevlError(program.filename, comp.line, f"duplicate component `{comp.name}`")
        seen.add(comp.name)
        components.append(_lower_component(comp, services, program.filename))

    manifest = _link(program, components, ambient.get("components") or [])

    types = _lower_type_decls(program, program.filename)
    fns = _lower_fns(program, program.filename, types)
    externs = _lower_externs(program, program.filename)
    tests = _lower_tests(program, program.filename)

    uses_v2 = any(comp.get("isolate") or comp.get("intercept") for comp in components)
    uses_v3 = bool(types) or bool(fns) or bool(externs) or bool(tests)

    result = {
        "ir_version": IR_VERSION_V3 if uses_v3 else (IR_VERSION_V2 if uses_v2 else IR_VERSION),
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
    if types:
        result["types"] = types
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

"""Bidirectional type checking for revl 2.0 (sound where declared).

Design (see the "type safety" milestone discussion):

- Every *boundary* already carries declared types (services, fns, externs,
  config, ADTs), so no global inference is needed: expressions are checked
  *against* declarations and inferred locally.
- The checker is **sound where types are known and silent where they are
  not**: a definite mismatch between two known types is an error; positions
  whose types cannot be recovered (host-valued objects, arrows) stay
  unchecked and are the documented gradual frontier.
- `null` has no type: absence is `Opt[T]` (syntax-2.0 §2). The literal is
  rejected in every expression position (config defaults use a separate
  grammar and keep it).
- Opt discipline: `T` is accepted where `Opt[T]` is expected (injection);
  `Opt[T]` where `T` is expected is an error with an unwrap hint.
- Generics: `Never` (empty list) and `Any` are wildcards; single-uppercase
  type names (declared type parameters) are treated as wildcards — full
  instantiation is deferred.

Two expression dialects are covered:
- `infer_ast`   — parser AST (Expr*) used by pure fn bodies (stratum 1);
  raises on definite operator/branch mismatches when `filename` is given.
- `infer_ir`    — lowered IR nodes used by component bodies (stratum 3).
"""

from __future__ import annotations

from .errors import RevlError

# reserved keys carried inside the `types` table (type names never start
# with an underscore, so these cannot collide)
FNS_KEY = "__fns__"      # {name: {"params": [type...], "returns": type|None}}
CASES_KEY = "__cases__"  # {case: {"adt": name, "payload": type|None}}

_NUMERIC = {"Int", "Float"}
_SIZED_HEADS = {"Str", "Bytes", "List"}


# ---------------------------------------------------------------- algebra

def parse_type(name: str | None) -> tuple[str | None, list[str]]:
    """"List[Row]" -> ("List", ["Row"]); "Str" -> ("Str", [])."""
    if not name:
        return None, []
    if "[" not in name or not name.endswith("]"):
        return name, []
    head, _, rest = name.partition("[")
    inner = rest[:-1]
    args, depth, start = [], 0, 0
    for i, ch in enumerate(inner):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(inner[start:i].strip())
            start = i + 1
    args.append(inner[start:].strip())
    return head, args


# builtin parametric type heads and their exact arity
_GENERIC_ARITY = {"Opt": 1, "List": 1, "Map": 2, "Result": 2}


def check_type_wellformed(filename: str, line: int, type_name: str | None) -> None:
    """Reject a malformed declared type annotation (a builtin generic head
    used with the wrong number of arguments, e.g. bare `Opt` or `List`).
    Recurses into type arguments. User/nominal heads are not arity-checked."""
    if not type_name:
        return
    head, args = parse_type(type_name)
    arity = _GENERIC_ARITY.get(head or "")
    if arity is not None and len(args) != arity:
        example = {"Opt": "Opt[Int]", "List": "List[Int]",
                   "Map": "Map[Str, Int]", "Result": "Result[Int, Str]"}[head]
        raise RevlError(
            filename, line,
            f"`{head}` takes {arity} type argument(s), got {len(args)} "
            f"(`{type_name}`)",
            hint=f"write e.g. `{example}` — a bare `{head}` is not a type",
        )
    for arg in args:
        check_type_wellformed(filename, line, arg)


def _is_wildcard(name: str | None) -> bool:
    return (
        name is None
        or name in ("Any", "Never")
        or (len(name) == 1 and name.isupper())  # declared type parameter
    )


def compatible(expected: str | None, actual: str | None) -> bool:
    """May a value of type `actual` flow into a position typed `expected`?"""
    if _is_wildcard(expected) or _is_wildcard(actual):
        return True
    if expected == actual:
        return True
    ehead, eargs = parse_type(expected)
    ahead, aargs = parse_type(actual)
    if ehead == "Float" and ahead == "Int":
        return True  # numeric widening
    if ehead == "Opt":
        einner = eargs[0] if eargs else None  # bare `Opt` degrades to wildcard
        if ahead == "Opt":
            return compatible(einner, aargs[0] if aargs else None)
        return compatible(einner, actual)  # T -> Opt[T] injection
    if ehead == ahead and len(eargs) == len(aargs):
        return all(compatible(e, a) for e, a in zip(eargs, aargs))
    return False


def join(a: str | None, b: str | None) -> str | None:
    """Common type of two branches, or None when unknown."""
    if a is None or b is None:
        return None
    if compatible(a, b):
        return a
    if compatible(b, a):
        return b
    return None


def mismatch(filename: str, line: int, where: str,
             expected: str | None, actual: str | None) -> RevlError:
    hint = None
    ahead, _ = parse_type(actual)
    ehead, _ = parse_type(expected)
    if ahead == "Opt" and ehead != "Opt":
        hint = ("unwrap the optional first: `match` on it, or use `??` "
                "to supply a fallback (syntax-2.0 §2)")
    return RevlError(filename, line,
                     f"{where} expects `{expected}`, got `{actual}`", hint,
                     code="T1", category="type-mismatch",
                     expected=expected, actual=actual)


def null_error(filename: str, line: int) -> RevlError:
    return RevlError(
        filename, line,
        "`null` has no type in revl — absence is `Opt[T]`",
        hint="use `None` for an absent optional, or restructure with `match`/`??` "
             "(syntax-2.0 §2; `null` remains legal only as a config default)",
        code="T2", category="null-safety",
    )


# ------------------------------------------------------- AST inference

def _binop_type(op: str, lt: str | None, rt: str | None,
                filename: str | None, line: int):
    if op in ("==", "!=", "===", "!=="):
        if filename and lt and rt and not (compatible(lt, rt) or compatible(rt, lt)):
            raise mismatch(filename, line, f"`{op}` comparison between `{lt}` and", rt, lt)
        return "Bool"
    if op in ("<", "<=", ">", ">="):
        for t in (lt, rt):
            if filename and t and parse_type(t)[0] not in _NUMERIC | {"Str"}:
                raise RevlError(filename, line, f"`{op}` cannot order `{t}` values")
        return "Bool"
    if op in ("&&", "||"):
        for t in (lt, rt):
            if filename and t and t != "Bool":
                raise mismatch(filename, line, f"operand of `{op}`", "Bool", t)
        return "Bool"
    if op == "??":
        lhead, largs = parse_type(lt)
        if lhead == "Opt":
            inner = largs[0] if largs else None
            return join(inner, rt) or inner or rt
        if filename and lt and not _is_wildcard(lt):
            # `??` supplies a fallback for an absent optional; on a value that
            # is always present it is meaningless, and the tiers that model
            # Opt as Option/Optional cannot even render it
            raise RevlError(
                filename, line,
                f"`??` needs an optional on the left, got `{lt}`",
                hint="`a ?? b` supplies a fallback when `a` is absent — a "
                     "non-optional is always present, so the fallback is dead "
                     "(syntax-2.0 §2)",
                code="T1", category="type-mismatch",
            )
        return lt or rt
    if op == "+":
        if lt == "Str" or rt == "Str":
            if filename and (
                (lt and lt != "Str" and lt not in _NUMERIC)
                or (rt and rt != "Str" and rt not in _NUMERIC)
            ):
                bad = lt if lt != "Str" else rt
                raise mismatch(filename, line, "operand of string `+`", "Str", bad)
            return "Str" if (lt == "Str" and rt == "Str") else None
        # fall through to numeric
    if op in ("+", "-", "*", "/", "%"):
        for t in (lt, rt):
            if filename and t and t not in _NUMERIC:
                raise mismatch(filename, line, f"operand of `{op}`", "Int", t)
        if lt == "Float" or rt == "Float":
            return "Float"
        if lt == "Int" and rt == "Int":
            return "Int"
        return None
    return None


_BUILTIN_SIG = {
    # method: (receiver family, [arg types or "@elem"], return or "@self"/"@elem")
    "length": ("sized", [], "Int"),
    "push": ("List", ["@elem"], "@self"),
    "slice": ("sized", ["Int", "Int"], "@self"),
    "charAt": ("Str", ["Int"], "Str"),
    "charCodeAt": ("Str", ["Int"], "Int"),
    "concat": ("sized", ["@self"], "@self"),
    "indexOf": ("sized", ["@member"], "Int"),
    "split": ("Str", ["Str"], "List[Str]"),
    "join": ("List", ["Str"], "Str"),
    "repeat": ("Str", ["Int"], "Str"),
}


# Host builtins (DESIGN §7). Each backend already carried its own copy of
# these signatures — rust in a table, java in its stubs, wasm as an i32
# assumption, python/TS not at all — so four tiers disagreed about a contract
# no one enforced. `await Job.run(1)` type-checked and then failed in `rustc`
# and `javac`. The frontend owns it now; the tiers implement it.
#
# Arguments only. A host builtin's *result* is a host-valued object, which
# typecheck.py's header already enumerates as part of the unchecked frontier
# (and which `revl audit` surfaces via G8) — so nothing here claims to know
# what comes back.
_HOST_ARG_SIG: dict[str, list[str]] = {
    "Map.new": [],
    "Map.drop": [],
    "Map.insert": ["Str", "Str"],
    "Map.remove": ["Str"],
    "Map.get": ["Str"],
    "Pool.open": ["Str", "Int"],
    "Pool.close": [],
    "Pool.query": ["Str"],
    "Pool.execute": ["Str"],
    "Job.run": ["Str"],
}


def host_check(fn: str, arg_types: list, filename: str | None, line: int) -> None:
    """Check a host builtin call against its declared argument types."""
    params = _HOST_ARG_SIG.get(fn)
    if params is None or not filename:
        return
    if len(arg_types) != len(params):
        raise RevlError(
            filename, line,
            f"host builtin `{fn}` takes {len(params)} argument"
            f"{'' if len(params) == 1 else 's'}, got {len(arg_types)}",
            hint=f"the signature is `{fn}({', '.join(params) or ''})`",
            code="HOST-ARITY", category="host-boundary")
    for expected, actual in zip(params, arg_types):
        if actual and not compatible(expected, actual):
            raise mismatch(filename, line, f"host builtin `{fn}` argument",
                           expected, actual)


def builtin_check(method: str, target_type: str | None, arg_types: list,
                  filename: str | None, line: int) -> str | None:
    """Type a stdlib method call; raises on definite mismatches."""
    sig = _BUILTIN_SIG.get(method)
    if sig is None:
        return None
    family, params, ret = sig
    thead, targs = parse_type(target_type)
    if filename and target_type is not None:
        if family == "sized" and thead not in _SIZED_HEADS:
            raise RevlError(filename, line,
                            f"builtin `{method}` needs a Str/Bytes/List receiver, got `{target_type}`")
        if family in ("List", "Str") and thead != family:
            raise RevlError(filename, line,
                            f"builtin `{method}` needs a {family} receiver, got `{target_type}`")
    elem = targs[0] if thead == "List" and targs else None
    for spec, actual in zip(params, arg_types):
        expected = {"@elem": elem, "@member": elem if thead == "List" else ("Str" if thead == "Str" else None), "@self": target_type}.get(spec, spec)
        if filename and expected and actual and not compatible(expected, actual):
            raise mismatch(filename, line, f"builtin `{method}` argument", expected, actual)
    if ret == "@self":
        return target_type
    if ret == "@elem":
        return elem
    return ret


def infer_ast(expr, tenv: dict, types: dict, filename: str | None = None) -> str | None:
    """Best-effort type of a parser-AST expression. With `filename`, definite
    operator/branch/argument mismatches raise; without it, never raises."""
    from .parser import (
        ExprArrow, ExprBin, ExprCall, ExprField, ExprIf, ExprIndex,
        ExprList, ExprLit, ExprMatch, ExprRecord, ExprUn, ExprVar, Interp, Lit,
    )

    line = getattr(expr, "line", 0)
    if isinstance(expr, (ExprLit, Lit)):
        v = expr.value
        if v is None:
            if filename:
                raise null_error(filename, line)
            return None
        if isinstance(v, bool):
            return "Bool"
        if isinstance(v, int):
            return "Int"
        if isinstance(v, float):
            return "Float"
        if isinstance(v, str):
            return "Str"
        return None
    if isinstance(expr, Interp):
        return "Str"
    if isinstance(expr, ExprVar):
        if expr.name == "None":
            return "Opt[Any]"
        return tenv.get(expr.name)
    if isinstance(expr, ExprBin):
        lt = infer_ast(expr.left, tenv, types, filename)
        rt = infer_ast(expr.right, tenv, types, filename)
        return _binop_type(expr.op, lt, rt, filename, line)
    if isinstance(expr, ExprUn):
        t = infer_ast(expr.operand, tenv, types, filename)
        if expr.op == "!":
            if filename and t and t != "Bool":
                raise mismatch(filename, line, "operand of `!`", "Bool", t)
            return "Bool"
        if filename and t and t not in _NUMERIC:
            raise mismatch(filename, line, "operand of unary `-`", "Int", t)
        return t
    if isinstance(expr, ExprField):
        target = infer_ast(expr.target, tenv, types, filename)
        thead, _ = parse_type(target)
        if expr.name == "length" and (thead in _SIZED_HEADS):
            return "Int"
        spec = types.get(target or "")
        if spec is not None and spec.get("kind") == "record":
            fields = spec.get("fields", {})
            if filename and expr.name not in fields:
                raise RevlError(filename, line,
                                f"`{target}` has no field `{expr.name}` "
                                f"(fields: {', '.join(sorted(fields)) or 'none'})")
            return fields.get(expr.name)
        return None
    if isinstance(expr, ExprIndex):
        target = infer_ast(expr.target, tenv, types, filename)
        it = infer_ast(expr.index, tenv, types, filename)
        thead, targs = parse_type(target)
        if filename and it and thead in ("List", "Str") and it != "Int":
            raise mismatch(filename, line, "index", "Int", it)
        if thead == "List":
            return targs[0] if targs else None
        if thead == "Str":
            return "Str"
        return None
    if isinstance(expr, ExprIf):
        infer_ast(expr.cond, tenv, types, filename)
        a = infer_ast(expr.then, tenv, types, filename)
        b = infer_ast(expr.otherwise, tenv, types, filename)
        if filename and a and b and join(a, b) is None:
            raise RevlError(filename, line,
                            f"ternary branches disagree: `{a}` vs `{b}`")
        return join(a, b)
    if isinstance(expr, ExprList):
        item = None
        for e in expr.items:
            t = infer_ast(e, tenv, types, filename)
            item = t if item is None else join(item, t)
        return f"List[{item}]" if item else "List[Never]"
    if isinstance(expr, ExprRecord):
        for _, value in expr.fields:
            infer_ast(value, tenv, types, filename)
        return None  # anonymous; named via check_ast against an expected record
    if isinstance(expr, ExprCall):
        arg_types = [infer_ast(a, tenv, types, filename) for a in expr.args]
        if isinstance(expr.callee, ExprVar):
            name = expr.callee.name
            case = (types.get(CASES_KEY) or {}).get(name)
            if case is not None:
                if filename and case["payload"] and arg_types and arg_types[0] and \
                        not compatible(case["payload"], arg_types[0]):
                    raise mismatch(filename, line, f"`{name}(...)` payload",
                                   case["payload"], arg_types[0])
                if name == "Some":
                    return f"Opt[{arg_types[0] or 'Any'}]"
                if name == "Ok":
                    return f"Result[{arg_types[0] or 'Any'}, Any]"
                if name == "Err":
                    return f"Result[Any, {arg_types[0] or 'Any'}]"
                return case["adt"]
            sig = (types.get(FNS_KEY) or {}).get(name)
            if sig is not None:
                if filename:
                    for i, (p, a) in enumerate(zip(sig["params"], arg_types)):
                        if p and a and not compatible(p, a):
                            raise mismatch(filename, line,
                                           f"argument {i + 1} of `{name}(...)`", p, a)
                return sig["returns"]
        if isinstance(expr.callee, ExprField):
            target_t = infer_ast(expr.callee.target, tenv, types, filename)
            return builtin_check(expr.callee.name, target_t, arg_types, filename, line)
        return None
    if isinstance(expr, ExprMatch):
        result = None
        scrutinee_t = infer_ast(expr.scrutinee, tenv, types, filename)
        spec = types.get(scrutinee_t or "")
        for pattern, bind, body in expr.arms:
            inner = dict(tenv)
            if bind is not None:
                payload = None
                if spec is not None and spec.get("kind") == "variant":
                    for case in spec.get("cases", []):
                        if case["name"] == pattern:
                            payload = case["payload"]
                if payload is not None:
                    inner[bind] = payload
                else:
                    inner.pop(bind, None)
            t = infer_ast(body, inner, types, filename)
            result = t if result is None else join(result, t)
        return result
    if isinstance(expr, ExprArrow):
        return None
    return None


def check_ast(expr, expected: str | None, tenv: dict, types: dict,
              filename: str, where: str) -> None:
    """Bidirectional check of a parser-AST expression against `expected`."""
    from .parser import ExprIf, ExprList, ExprMatch, ExprRecord

    line = getattr(expr, "line", 0)
    if expected is None:
        infer_ast(expr, tenv, types, filename)
        return
    spec = types.get(expected or "")
    if isinstance(expr, ExprRecord) and spec is not None and spec.get("kind") == "record":
        declared = spec.get("fields", {})
        given = {name for name, _ in expr.fields}
        missing = sorted(set(declared) - given)
        extra = sorted(given - set(declared))
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing {', '.join(f'`{m}`' for m in missing)}")
            if extra:
                parts.append(f"unknown {', '.join(f'`{e}`' for e in extra)}")
            raise RevlError(filename, line,
                            f"record literal for `{expected}` has {'; '.join(parts)}")
        for name, value in expr.fields:
            check_ast(value, declared.get(name), tenv, types, filename,
                      f"field `{name}` of `{expected}`")
        return
    ehead, eargs = parse_type(expected)
    if isinstance(expr, ExprList) and ehead == "List":
        for item in expr.items:
            check_ast(item, eargs[0], tenv, types, filename, f"element of `{expected}`")
        return
    if isinstance(expr, ExprIf):
        infer_ast(expr.cond, tenv, types, filename)
        check_ast(expr.then, expected, tenv, types, filename, where)
        check_ast(expr.otherwise, expected, tenv, types, filename, where)
        return
    if isinstance(expr, ExprMatch):
        # Each arm body is checked against the expectation individually, with
        # the arm's payload binding in scope. Inferring the *joined* arm type
        # and checking that once misses disagreeing arms: when arms conflict
        # the join is None (unknown), so a single `compatible` check silently
        # passes. Per-arm check-position closes that hole while staying silent
        # where an arm's type is genuinely unknown.
        scrutinee_t = infer_ast(expr.scrutinee, tenv, types, filename)
        spec = types.get(scrutinee_t or "")
        for pattern, bind, body in expr.arms:
            inner = dict(tenv)
            if bind is not None:
                payload = None
                if spec is not None and spec.get("kind") == "variant":
                    for case in spec.get("cases", []):
                        if case["name"] == pattern:
                            payload = case["payload"]
                if payload is not None:
                    inner[bind] = payload
                else:
                    inner.pop(bind, None)
            check_ast(body, expected, inner, types, filename, where)
        return
    actual = infer_ast(expr, tenv, types, filename)
    if actual and not compatible(expected, actual):
        raise mismatch(filename, line, where, expected, actual)


# ------------------------------------------------------- IR inference (components)

def infer_ir(node, tenv: dict, types: dict, services: dict,
             filename: str | None = None, line: int = 0) -> str | None:
    """Best-effort type of a lowered component-body IR node. `services` maps
    service name -> ServiceDecl; `tenv` maps *safe* names -> types.

    With `filename`, definite operator/builtin mismatches raise (the component
    effect-setup op sweep — HOLE 2 / HOLE 3(c)); without it, it is a pure type
    oracle that never raises. Unknown/host-valued operands infer to `None` and
    are left alone in both modes, so the sweep stays silent where unknown."""
    if not isinstance(node, dict):
        return None
    kind = node.get("kind")
    if kind == "lit":
        v = node.get("value")
        if isinstance(v, bool):
            return "Bool"
        if isinstance(v, int):
            return "Int"
        if isinstance(v, float):
            return "Float"
        if isinstance(v, str):
            return "Str"
        return None
    if kind in ("format", "interp"):
        return "Str"
    if kind == "name":
        return tenv.get(node.get("id"))
    if kind == "var":
        return tenv.get(node.get("name"))
    if kind == "config":
        return tenv.get(f"config.{node.get('field')}")
    if kind == "call":
        target = node.get("target")
        if isinstance(target, dict) and target.get("kind") == "req":
            svc = services.get(tenv.get(f"req.{target.get('name')}") or "")
            if svc is not None:
                decl = svc.methods.get(node.get("method"))
                if decl is not None:
                    return decl.returns
        return None
    if kind == "fn":
        sig = (types.get(FNS_KEY) or {}).get(node.get("name"))
        return sig["returns"] if sig else None
    if kind == "builtin":
        target_t = infer_ir(node.get("target"), tenv, types, services, filename, line)
        args = [infer_ir(a, tenv, types, services, filename, line) for a in node.get("args") or []]
        return builtin_check(node.get("method"), target_t, args, filename, line)
    if kind == "bin":
        lt = infer_ir(node.get("left"), tenv, types, services, filename, line)
        rt = infer_ir(node.get("right"), tenv, types, services, filename, line)
        return _binop_type(node.get("op"), lt, rt, filename, line)
    if kind == "un":
        if node.get("op") == "!":
            return "Bool"
        return infer_ir(node.get("operand"), tenv, types, services, filename, line)
    if kind == "len":
        return "Int"
    if kind == "field":
        target = infer_ir(node.get("target"), tenv, types, services, filename, line)
        spec = types.get(target or "")
        if spec is not None and spec.get("kind") == "record":
            return spec.get("fields", {}).get(node.get("name"))
        return None
    if kind == "index":
        target = infer_ir(node.get("target"), tenv, types, services, filename, line)
        thead, targs = parse_type(target)
        if thead == "List":
            return targs[0] if targs else None
        if thead == "Str":
            return "Str"
        return None
    if kind == "if":
        return join(infer_ir(node.get("then"), tenv, types, services, filename, line),
                    infer_ir(node.get("else"), tenv, types, services, filename, line))
    return None

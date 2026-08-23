"""Bidirectional type checking for revl 2.0 (sound where declared).

Design (see the "type safety" milestone discussion):

- Every *boundary* already carries declared types (services, fns, externs,
  config, ADTs), so no global inference is needed: expressions are checked
  *against* declarations and inferred locally.
- The checker is **sound where types are known and silent where they are
  not**: a definite mismatch between two known types is an error; positions
  whose types cannot be recovered stay unchecked and are the documented
  gradual frontier. What is still on that frontier:
    * host-valued objects (`Map.new()`, `Pool.open(..)`, `Job.run(..)` and
      every member reached through one) — the call form's *arguments* are
      checked against `_HOST_ARG_SIG`, a constructor form infers its family so
      receiver-form method calls are checked too (`_HOST_FAMILIES`), but every
      *result* is opaque;
    * an arrow with **no expected type and no parameter annotations**
      (`let g = v => v + 1`) — its body is still walked, but the arrow
      itself has no type. An arrow in checking position, or one whose
      parameters are annotated, is typed and checked (see "function types");
    * function *values* inside a component body (stratum 3): `infer_ir`
      types no arrow and no call through one, so an arrow that reaches a
      `provide` method body is unchecked even where the surrounding service
      signature names a function type. Stratum 1 (`fn`/`test` bodies) is
      where function types are checked (docs/function-types.md §limits).
- Function types (docs/function-types.md): `(Int, Str) -> Bool` is a type
  like any other. `parse_type` normalises it to the head `FN_HEAD` with
  `[param..., return]`, so the whole algebra below — unify, substitute,
  compatible, tparam marking — works on it without a special case beyond
  variance.
- `null` has no type: absence is `Opt[T]` (syntax-2.0 §2). The literal is
  rejected in every expression position (config defaults use a separate
  grammar and keep it).
- Opt discipline: `T` is accepted where `Opt[T]` is expected (injection);
  `Opt[T]` where `T` is expected is an error with an unwrap hint.
- Generics: `Never` (empty list) and `Any` are wildcards. A single-uppercase
  name in a `fn` signature that is not a declared type is that fn's implicit
  type parameter; it is marked when the signature table is built, is a wildcard
  only inside that fn's own body, and is unified against the actual arguments
  at every call site. A single-uppercase name that *is* declared (`type S = A |
  B`) is an ordinary nominal type and is checked as one.

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

# The head `parse_type` reports for a function type `(P, ...) -> R`, whose
# args are `[P, ..., R]` — the return type last. It is deliberately spelled
# with characters no identifier may contain, so it can never collide with a
# user type name the way a reserved word like `Fn` would.
FN_HEAD = "->"


def _split_top_level(text: str) -> list[str]:
    """Split on commas outside `[...]` and `(...)`."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def _split_fn_type(name: str) -> tuple[list[str], str] | None:
    """`"(Int, Str) -> Bool"` -> `(["Int", "Str"], "Bool")`, else None.

    Only a *leading* parenthesised list followed by `->` is a function type;
    the `->` must be at paren/bracket depth 0 so that a nested function type
    (`(Int) -> ((Str) -> Bool)`) does not split at the wrong arrow.
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
                params = _split_top_level(inner) if inner else []
                return params, rest[2:].strip()
    return None


def format_type(head: str | None, args: list[str]) -> str | None:
    """The inverse of `parse_type`: rebuild a type from head + arguments."""
    if head is None:
        return None
    if head == FN_HEAD:
        *params, returns = args
        return f"({', '.join(params)}) -> {returns}"
    if not args:
        return head
    return f"{head}[{', '.join(args)}]"


def parse_type(name: str | None) -> tuple[str | None, list[str]]:
    """"List[Row]" -> ("List", ["Row"]); "Str" -> ("Str", []).

    A function type normalises to `(FN_HEAD, [param..., return])`, which is
    what lets the rest of the algebra treat it as an ordinary type
    application (`format_type` puts the surface spelling back).
    """
    if not name:
        return None, []
    fn = _split_fn_type(name.strip())
    if fn is not None:
        params, returns = fn
        return FN_HEAD, params + [returns]
    if "[" not in name or not name.endswith("]"):
        return name, []
    head, _, rest = name.partition("[")
    return head, _split_top_level(rest[:-1])


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


# ------------------------------------------------- type parameters
#
# A `fn`/`extern` signature declares type parameters two ways, both feeding the
# same machinery. Implicitly: a single-uppercase name that is not a declared
# type (`fn id(x: T) -> T`). Explicitly (roadmap item 6): a `[T, U]` list after
# the name (`fn id[T](x: T) -> T`, `fn first[Elem](xs: List[Elem]) -> Elem`),
# which is a strict superset — it can name a parameter the single-uppercase
# heuristic would miss, and states intent. Marking those names *once*, when the
# signature table is built, is what lets the rest of the checker stop guessing:
#
#   - a marked name (`?T`) is a wildcard inside the fn's own body, where it is
#     genuinely universally quantified, and is unified against the actual
#     argument at every call site;
#   - an unmarked single-uppercase name is an ordinary nominal type and is
#     checked like any other, so `type S = A | B` is no longer silently
#     unchecked everywhere merely for being one letter long.
#
# The marker never leaves the checker: the IR carries the author's spelling,
# and `render_type` strips it before any diagnostic is rendered.

_TPARAM = "?"


def is_tparam_name(name: str, declared: dict) -> bool:
    """Would `name`, written in a fn signature, be an implicit type parameter?"""
    return len(name) == 1 and name.isupper() and name not in declared


# type heads that always name a concrete type; an explicit `[T]` parameter may
# not shadow one (nor a user-declared type). See `validate_explicit_tparams`.
_BUILTIN_TYPE_NAMES = {
    "Int", "Float", "Str", "Bool", "Bytes", "Unit",
    "Opt", "List", "Map", "Result", "Any", "Never",
}


def validate_explicit_tparams(names, declared: dict,
                              filename: str | None, line: int) -> set[str]:
    """Validate an explicit `fn id[T, U](...)` list and return it as a set.

    Shadowing is *rejected*, not silently allowed: a name in `[...]` may not
    collide with a builtin or a user-declared type. revl's implicit-generics
    rule closed exactly the hole where a one-letter name silently wildcarded a
    real `type S = A | B`; letting an explicit `[S]` reopen it — even scoped to
    one body — would resurrect the same "is this the ADT or a type variable?"
    ambiguity. Renaming the parameter is cheap and unambiguous, so we reject
    (docs/generics.md). Duplicates are already caught in the parser; guarded
    here too for programmatically-built decls."""
    seen: set[str] = set()
    for name in names:
        if name in seen:
            if filename:
                raise RevlError(filename, line,
                                f"duplicate type parameter `{name}`")
            continue
        if name in _BUILTIN_TYPE_NAMES or name in declared:
            if filename:
                what = "a builtin type" if name in _BUILTIN_TYPE_NAMES \
                    else "a declared type"
                raise RevlError(
                    filename, line,
                    f"type parameter `{name}` shadows {what}",
                    hint="rename the type parameter — a `[...]` name may not "
                         "collide with a builtin or declared type "
                         "(docs/generics.md)",
                )
            continue
        seen.add(name)
    return seen


def collect_tparams(type_names, declared: dict, explicit=()) -> set[str]:
    """Type parameters mentioned anywhere in these declared types.

    Implicit ones are single-uppercase names that are not declared types. The
    `explicit` set adds names the author declared with `fn id[T](...)`: those
    are type parameters regardless of the single-uppercase heuristic, so an
    author can name one the heuristic would miss (`fn first[Elem](xs:
    List[Elem]) -> Elem`). An explicit name is included even if it never
    appears in a parameter or return, so its mere declaration marks the
    signature generic — exactly the same set the implicit path would build."""
    explicit = set(explicit)
    found: set[str] = set(explicit)

    def walk(name: str | None) -> None:
        if not name:
            return
        head, args = parse_type(name)
        if head and not args and (head in explicit
                                  or is_tparam_name(head, declared)):
            found.add(head)
        for arg in args:
            walk(arg)

    for name in type_names:
        walk(name)
    return found


def mark_tparams(type_name: str | None, tparams: set[str]) -> str | None:
    """Rewrite `T` -> `?T` inside a declared type, recursing into arguments."""
    if not type_name or not tparams:
        return type_name
    head, args = parse_type(type_name)
    if head and not args:
        return _TPARAM + head if head in tparams else head
    return format_type(head, [mark_tparams(a, tparams) or a for a in args])


def render_type(type_name: str | None) -> str | None:
    """The author's spelling of a checker-internal type, for diagnostics."""
    return type_name.replace(_TPARAM, "") if type_name else type_name


def unify(param: str | None, actual: str | None, subst: dict) -> bool:
    """Match a (marked) parameter type against an argument type, growing
    `subst`. Returns False only on a *definite* conflict; unknowns pass."""
    if param is None or actual is None:
        return True
    head, args = parse_type(param)
    if head and head.startswith(_TPARAM) and not args:
        if _is_wildcard(actual):
            return True  # nothing to learn from an unknown argument
        prior = subst.get(head)
        if prior is None:
            subst[head] = actual
            return True
        widened = join(prior, actual)
        if widened is None:
            return False
        subst[head] = widened
        return True
    ahead, aargs = parse_type(actual)
    if head == "Opt" and args and ahead != "Opt":
        return unify(args[0], actual, subst)  # T -> Opt[T] injection
    if head == ahead and len(args) == len(aargs):
        return all(unify(p, a, subst) for p, a in zip(args, aargs))
    return compatible(param, actual)


def substitute(type_name: str | None, subst: dict) -> str | None:
    """Apply a unifier; type parameters it did not bind keep their marker
    (and so stay wildcards downstream, exactly as before)."""
    if not type_name or not subst:
        return type_name
    head, args = parse_type(type_name)
    if head and not args:
        return subst.get(head, head)
    return format_type(head, [substitute(a, subst) or a for a in args])


def _is_wildcard(name: str | None) -> bool:
    return (
        name is None
        or name in ("Any", "Never")
        or name.startswith(_TPARAM)  # implicit fn type parameter
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
    if ehead == FN_HEAD:
        # A function value flows where a function type is expected only if it
        # accepts everything that position will pass it and returns something
        # the position can use: parameters contravariant, result covariant.
        # The generic elementwise rule below would make parameters covariant,
        # which accepts `(Int) -> X` where `(Float) -> X` is required and then
        # hands the callee a Float.
        if ehead != ahead or len(eargs) != len(aargs):
            return False
        return (all(compatible(a, e) for e, a in zip(eargs[:-1], aargs[:-1]))
                and compatible(eargs[-1], aargs[-1]))
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
    expected, actual = render_type(expected), render_type(actual)
    return RevlError(filename, line,
                     f"{where} expects `{expected}`, got `{actual}`", hint,
                     code="T1", category="type-mismatch",
                     expected=expected, actual=actual)


def opt_escape_error(filename: str, line: int, what: str, target: str,
                     inner: str | None, alt: str | None = None) -> RevlError:
    """`Opt[T]` reached through as if it were `T`.

    The README's headline guarantee is that `T` flows into `Opt[T]` but never
    silently back out. Rejecting `return o` while accepting `o.n` would let the
    inner type escape one step later, so every *access* through an optional is
    refused here too."""
    target = render_type(target)
    inner_s = render_type(inner) or "the wrapped value"
    hint = (f"unwrap it first — `match` on the optional, or `??` to supply a "
            f"fallback — then {what} on the `{inner_s}`")
    if alt:
        hint += f"; or write `{alt}` to short-circuit and get an `Opt[...]` back"
    hint += " (syntax-2.0 §2)"
    return RevlError(
        filename, line,
        f"{what} on `{target}`: the optional wrapper has no such member — "
        f"`T` flows into `Opt[T]`, never silently back out",
        hint=hint, code="T1", category="null-safety",
    )


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
            raise mismatch(filename, line, f"`{op}` comparison between `{render_type(lt)}` and", rt, lt)
        return "Bool"
    if op in ("<", "<=", ">", ">="):
        for t in (lt, rt):
            if filename and t and parse_type(t)[0] not in _NUMERIC | {"Str"}:
                raise RevlError(filename, line, f"`{op}` cannot order `{render_type(t)}` values")
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
                f"`??` needs an optional on the left, got `{render_type(lt)}`",
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
        if op == "/":
            # `/` is TRUE division and yields Float even on two Ints, because
            # §0 governs: `/` is spelled exactly as TypeScript spells it, and
            # in TypeScript `7 / 2` is 3.5. Typing it `Int` was the reason
            # python and TypeScript "disagreed" with rust — they were being
            # faithful to the syntax and the checker was not.
            # Integer division has its own spellings (docs/arithmetic.md):
            # `div_trunc`, `div_floor`, `div_euclid`.
            if lt in _NUMERIC and rt in _NUMERIC:
                return "Float"
            return None
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
    # Integer division and modulo, named rather than defaulted (§0 keeps `/`
    # and `%` meaning what TypeScript means by them; these say what they do).
    # docs/arithmetic.md gives the definitions and the divergence they close.
    "div_trunc": ("Int", ["Int"], "Int"),
    "div_floor": ("Int", ["Int"], "Int"),
    "div_euclid": ("Int", ["Int"], "Int"),
    # The total, value-returning forms (docs/arithmetic.md, "Still open"):
    # same rounding convention as their faulting counterparts, but a zero
    # divisor yields `Err(reason)` instead of faulting — `fail` cannot serve
    # here (it is a component construct, refused in a pure fn), so the error
    # travels as a value. The Err payload is `Str` because it is the one
    # type every tier can hold in a Result without new representation.
    "checked_div_trunc": ("Int", ["Int"], "Result[Int, Str]"),
    "checked_div_floor": ("Int", ["Int"], "Result[Int, Str]"),
    "checked_div_euclid": ("Int", ["Int"], "Result[Int, Str]"),
    "checked_mod": ("Int", ["Int"], "Result[Int, Str]"),
    # The Map value type (docs/stdlib-2.0.md §Map): persistent, Str-keyed.
    # Method names are deliberately disjoint from the host verb set
    # (`open/close/query/execute/new/get/insert/remove/drop`) so the two
    # namespaces stay collision-free by construction; dispatch is also by
    # receiver kind, since a host-family receiver routes to
    # _HOST_FAMILIES before this table is ever consulted. `@elem` below
    # means the map's VALUE parameter (targs[1]), not its key.
    "set": ("Map", ["Str", "@elem"], "@self"),
    "lookup": ("Map", ["Str"], "Opt[@elem]"),
    "has": ("Map", ["Str"], "Bool"),
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


# Host-object *families*, derived from _HOST_ARG_SIG's dotted names. The
# constructor form (`Map.new()`, `Pool.open(..)`) infers the family as its
# static type, and a method call on a family-typed receiver is checked here:
# an unknown method is a compile error (the typo'd `m.putt(k)` used to emit
# `_revl_field(m, 'putt')(..)` — a guaranteed AttributeError at host runtime,
# invisible to every tier's compiler), and arguments are checked exactly like
# the call form. What stays deliberately opaque is the *result*: no entry
# claims to know what a stub returns, so values flowing OUT of host objects —
# and receivers whose provenance no constructor pins (an extern's return) —
# remain on the G8 audit surface, not the checked one (docs/contract-errata.md).
_HOST_FAMILIES: dict[str, dict[str, list[str]]] = {}
for _dotted, _params in _HOST_ARG_SIG.items():
    _family, _, _method = _dotted.partition(".")
    _HOST_FAMILIES.setdefault(_family, {})[_method] = _params


def host_family_check(family: str, method: str, arg_types: list,
                      filename: str | None, line: int) -> None:
    """Check a method call on a family-typed host receiver."""
    methods = _HOST_FAMILIES[family]
    params = methods.get(method)
    if params is None:
        if filename:
            raise RevlError(
                filename, line,
                f"`{family}` has no method `{method}` "
                f"(its surface: {', '.join(sorted(methods))})",
                hint="host objects are checked against the stub surface spelled "
                     "in docs/stdlib-2.0.md — a misspelled method compiles on "
                     "every tier and only fails at the host runtime",
                code="HOST-METHOD", category="host-boundary")
        return
    if filename and len(arg_types) != len(params):
        raise RevlError(
            filename, line,
            f"host `{family}.{method}` takes {len(params)} argument"
            f"{'' if len(params) == 1 else 's'}, got {len(arg_types)}",
            hint=f"the signature is `.{method}({', '.join(params) or ''})`",
            code="HOST-ARITY", category="host-boundary")
    for expected, actual in zip(params, arg_types):
        if filename and expected and actual and not compatible(expected, actual):
            raise mismatch(filename, line,
                           f"host `{family}.{method}` argument", expected, actual)


def builtin_check(method: str, target_type: str | None, arg_types: list,
                  filename: str | None, line: int) -> str | None:
    """Type a stdlib method call; raises on definite mismatches."""
    # a method call on a constructor-tracked host receiver (`store.get(k)`
    # where `store = Map.new()`): checked against the family surface, result
    # opaque (see _HOST_FAMILIES)
    if target_type in _HOST_FAMILIES:
        host_family_check(target_type, method, arg_types, filename, line)
        return None
    sig = _BUILTIN_SIG.get(method)
    if sig is None:
        return None
    family, params, ret = sig
    thead, targs = parse_type(target_type)
    if filename and target_type is not None:
        if family == "sized" and thead not in _SIZED_HEADS:
            raise RevlError(filename, line,
                            f"builtin `{method}` needs a Str/Bytes/List receiver, got `{render_type(target_type)}`")
        if family in ("List", "Str", "Int", "Map") and thead != family:
            raise RevlError(filename, line,
                            f"builtin `{method}` needs a {family} receiver, got `{render_type(target_type)}`")
    # `@elem` is the element/value parameter: a List's single argument for
    # List receivers, a Map's second (value) argument for Map receivers.
    elem = None
    if thead == "List" and targs:
        elem = targs[0]
    elif thead == "Map" and len(targs) == 2:
        elem = targs[1]
    for spec, actual in zip(params, arg_types):
        expected = {"@elem": elem, "@member": elem if thead == "List" else ("Str" if thead == "Str" else None), "@self": target_type}.get(spec, spec)
        if filename and expected and actual and not compatible(expected, actual):
            raise mismatch(filename, line, f"builtin `{method}` argument", expected, actual)
    if ret == "@self" and elem == "Never":
        # Bottom-typed receiver — the empty literal `[]` / `Map.empty()`.
        # Its element type is a wildcard, so the argument checks above
        # proved NOTHING (compatible(Never, anything) is True), and the
        # @self result would flow into ANY Map[Str, X] / List[T] the same
        # way. Learn the element type from a concrete argument and carry it
        # in the rebuilt container, so `[].push("s")` types as List[Str]
        # and is refused where List[Int] is expected. When no argument
        # offers a concrete type (holes, unknowns), behavior is unchanged.
        learned = None
        for spec, actual in zip(params, arg_types):
            if not actual or _is_wildcard(actual):
                continue
            if spec == "@elem":
                learned = actual
            elif spec == "@self":
                ahead, aargs = parse_type(actual)
                if ahead == thead and aargs and not _is_wildcard(aargs[-1]):
                    learned = aargs[-1]
        if learned is not None:
            return format_type(thead,
                               [learned] if thead == "List" else [targs[0], learned])
    if ret == "@self":
        return target_type
    if ret == "@elem":
        return elem
    if ret == "Opt[@elem]":
        return f"Opt[{elem or 'Never'}]"
    return ret


def pin_hole(expr, expected: str | None, filename: str | None = None,
             where: str = "this hole") -> bool:
    """Give a typed hole the type its context expects (docs/holes.md).

    Returns True when `expr` is a hole. An annotated hole keeps its own
    annotation and is *checked* against the expectation, so `hole[Str]` in an
    `Int` position is a real diagnostic rather than a silent coercion — the
    whole point of a hole is that it carries a type it must eventually meet.
    """
    from .parser import ExprHole

    if not isinstance(expr, ExprHole):
        return False
    if expr.type is not None:
        if filename and expected and not compatible(expected, expr.type):
            raise mismatch(filename, getattr(expr, "line", 0), where,
                           expected, expr.type)
        return True
    if expected is not None and not _is_wildcard(expected):
        expr.resolved = expected
    return True


# `Int` is 64-bit two's complement (docs/arithmetic.md). The bounds live here,
# beside the checker, because a literal outside them has no tier-independent
# meaning: an arbitrary-precision host reads it exactly, wasm reads an i64 bit
# pattern, and the tiers then disagree about the same source text.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _reject_int_literal_range(filename: str | None, line: int, v: int) -> None:
    """Refuse an `Int` literal outside the i64 range at compile time.

    Gated on `filename` like every definite checker refusal: the no-filename
    form is a pure type oracle and never raises.
    """
    if filename is None or _INT64_MIN <= v <= _INT64_MAX:
        return
    raise RevlError(
        filename, line,
        f"Int literal `{v}` is outside the 64-bit range",
        hint="`Int` is 64-bit two's complement "
             "([-9223372036854775808, 9223372036854775807]); a literal beyond the "
             "bound reads differently per tier, so it never reaches one "
             "(docs/arithmetic.md)",
    )


def infer_ast(expr, tenv: dict, types: dict, filename: str | None = None) -> str | None:
    """Best-effort type of a parser-AST expression. With `filename`, definite
    operator/branch/argument mismatches raise; without it, never raises."""
    from .parser import (
        ExprArrow, ExprBin, ExprCall, ExprField, ExprHole, ExprIf, ExprIndex,
        ExprList, ExprLit, ExprMatch, ExprOptCall, ExprOptField, ExprRecord,
        ExprUn, ExprVar, Interp, Lit,
    )

    line = getattr(expr, "line", 0)
    if isinstance(expr, ExprHole):
        # A hole *is* its expected type: whatever the checker has already
        # pinned to it (annotation or context). Unknown here is not an error
        # — the surrounding form may still supply it in check position; the
        # refusal for a hole that never learns its type is raised at lowering.
        return expr.known_type
    if isinstance(expr, (ExprLit, Lit)):
        v = expr.value
        if v is None:
            if filename:
                raise null_error(filename, line)
            return None
        if isinstance(v, bool):
            return "Bool"
        if isinstance(v, int):
            # `Int` is 64-bit two's complement and arithmetic that leaves the
            # range faults (docs/arithmetic.md). A literal outside the range
            # has no single meaning across tiers — an arbitrary-precision host
            # reads it exactly while wasm reads an i64 bit pattern — so it is
            # refused here, where it is one diagnostic instead of a behaviour
            # per tier. This is also why `Int.MIN` has no spelling: writing
            # `-9223372036854775808` negates the out-of-range literal
            # `9223372036854775808`, which this rule refuses.
            _reject_int_literal_range(filename, line, v)
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
        if expr.name in tenv:
            return tenv[expr.name]
        # a bare nullary user-ADT constructor is a value of its ADT (`let s =
        # FirstTime`). Typing it here is what lets `match FirstTime { ... }`
        # reach the case/exhaustiveness checks instead of degrading to unknown.
        # Opt/Result constructors are excluded: they are not nullary values.
        case = (types.get(CASES_KEY) or {}).get(expr.name)
        if (case is not None and case.get("payload") is None
                and not str(case.get("adt", "")).startswith(("Opt", "Result"))):
            return case["adt"]
        return None
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
        thead, targs = parse_type(target)
        if filename and thead == "Opt":
            raise opt_escape_error(filename, line, f"field access `.{expr.name}`",
                                   target, targs[0] if targs else None,
                                   alt=f"?.{expr.name}")
        if expr.name == "length" and (thead in _SIZED_HEADS):
            return "Int"
        spec = types.get(target or "")
        if spec is not None and spec.get("kind") == "record":
            fields = spec.get("fields", {})
            if filename and expr.name not in fields:
                raise RevlError(filename, line,
                                f"`{render_type(target)}` has no field `{expr.name}` "
                                f"(fields: {', '.join(sorted(fields)) or 'none'})")
            return fields.get(expr.name)
        return None
    if isinstance(expr, (ExprOptField, ExprOptCall)):
        # `a?.b` short-circuits on absence, so it *requires* an optional on the
        # left and always yields an optional on the right. On a non-optional it
        # is dead syntax the strict tiers cannot render (Rust/Java have no
        # `?.` on a plain value); the result staying `Opt[...]` is what keeps
        # the inner type from escaping the wrapper.
        target = infer_ast(expr.target, tenv, types, filename)
        thead, targs = parse_type(target)
        member = expr.name if isinstance(expr, ExprOptField) else expr.method
        if filename and target and not _is_wildcard(target) and thead != "Opt":
            raise RevlError(
                filename, line,
                f"`?.` needs an optional on the left, got `{render_type(target)}`",
                hint=f"`{render_type(target)}` is always present, so the short-circuit is "
                     f"dead — write `.{member}` (syntax-2.0 §2)",
                code="T1", category="type-mismatch",
            )
        if thead != "Opt":
            return None
        inner = targs[0] if targs else None
        if isinstance(expr, ExprOptCall):
            args = [infer_ast(a, tenv, types, filename) for a in expr.args]
            result = builtin_check(expr.method, inner, args, filename, line)
        else:
            spec = types.get(inner or "")
            ihead, _ = parse_type(inner)
            if inner == "Opt" or ihead == "Opt":
                result = None
            elif member == "length" and ihead in _SIZED_HEADS:
                result = "Int"
            elif spec is not None and spec.get("kind") == "record":
                fields = spec.get("fields", {})
                if filename and member not in fields:
                    raise RevlError(filename, line,
                                    f"`{render_type(inner)}` has no field `{member}` "
                                    f"(fields: {', '.join(sorted(fields)) or 'none'})")
                result = fields.get(member)
            else:
                result = None
        if result is None:
            return None
        # already-optional inner results are not double-wrapped
        return result if parse_type(result)[0] == "Opt" else f"Opt[{result}]"
    if isinstance(expr, ExprIndex):
        target = infer_ast(expr.target, tenv, types, filename)
        it = infer_ast(expr.index, tenv, types, filename)
        thead, targs = parse_type(target)
        if filename and thead == "Opt":
            raise opt_escape_error(filename, line, "index `[...]`", target,
                                   targs[0] if targs else None)
        if filename and thead == "Str":
            # `Str` is not indexable: the spec's string surface is `charAt` /
            # `charCodeAt` / `slice` (docs/stdlib-2.0.md). Rust indexes bytes
            # (E0277 for an integer index) and Java has no operator at all, so
            # `s[0]` is not portable even where Python and TS accept it.
            raise RevlError(
                filename, line,
                "`Str` has no index operator — `[...]` indexes a `List` only",
                hint="use `s.charAt(i)` for the character (or `s.slice(i, j)` "
                     "for a substring, `s.charCodeAt(i)` for the code point) "
                     "— docs/stdlib-2.0.md",
                code="T1", category="type-mismatch",
            )
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
                            f"ternary branches disagree: `{render_type(a)}` vs `{render_type(b)}`")
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
            # a local of function type shadows everything else: `let g = ...`
            # / a `(Int) -> Int` parameter is the callee, not a same-named
            # top-level fn or ADT case
            local = tenv.get(name)
            if parse_type(local)[0] == FN_HEAD:
                return call_function_value(expr, local, f"`{name}`", arg_types,
                                           tenv, types, filename, line)
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
                params = sig["params"]
                if filename:
                    if len(expr.args) != len(params):
                        rendered = ", ".join(render_type(p) or "_"
                                             for p in params) or "no arguments"
                        raise RevlError(
                            filename, line,
                            f"`{name}` takes {len(params)} argument(s), "
                            f"{len(expr.args)} given",
                            hint=f"`{name}` is declared `({rendered})` — revl has no "
                                 "default, optional, or variadic parameters, so every "
                                 "call supplies exactly the declared arity",
                            code="T1", category="type-mismatch",
                        )
                if not sig.get("tparams"):
                    # a hole in argument position learns its type from the
                    # declared parameter (docs/holes.md). Monomorphic
                    # signatures only: a generic `T` position would pin a
                    # wildcard, and a hole never takes a type it cannot mean.
                    for param_type, arg in zip(params, expr.args):
                        pin_hole(arg, param_type)
                    _check_arrow_args(expr.args, params, tenv, types, filename,
                                      f"`{name}(...)`")
                    if filename:
                        for i, (p, a) in enumerate(zip(params, arg_types)):
                            if p and a and not compatible(p, a):
                                raise mismatch(filename, line,
                                               f"argument {i + 1} of `{name}(...)`", p, a)
                    return sig["returns"]
                # generic: instantiate the signature against this call's
                # arguments rather than letting every `T` position pass
                subst: dict = {}
                for i, (p, a) in enumerate(zip(params, arg_types)):
                    if unify(p, a, subst):
                        continue
                    if not filename:
                        return None
                    bound = substitute(p, subst)
                    raise mismatch(filename, line,
                                   f"argument {i + 1} of `{name}(...)`", bound, a)
                _check_arrow_args(expr.args, [substitute(p, subst) for p in params],
                                  tenv, types, filename, f"`{name}(...)`")
                return substitute(sig["returns"], subst)
        if isinstance(expr.callee, ExprField):
            # a constructor form (`Map.new()`, `Pool.open(..)`) infers the
            # *family* — that is what lets receiver-form calls on the bound
            # value be checked against the stub surface (_HOST_FAMILIES)
            if isinstance(expr.callee.target, ExprVar):
                ctor_root = expr.callee.target.name or ""
                # the Map VALUE constructor (docs/stdlib-2.0.md §Map): the
                # empty map is `Map[Str, Never]` — bottom-typed, so it flows
                # into any `Map[Str, V]` exactly as the untyped `[]` flows
                # into any `List[T]`. Deliberately checked BEFORE the host
                # family: `empty` is not a host verb and never will be
                # (namespace disjointness is pinned by test).
                if ctor_root == "Map" and expr.callee.name == "empty":
                    if filename and arg_types:
                        raise RevlError(
                            filename, line,
                            f"`Map.empty()` takes no arguments, {len(arg_types)} given",
                            hint="build up an empty map with `set`: "
                                 "`Map.empty().set(\"k\", v)`",
                        )
                    return "Map[Str, Never]"
                ctor = _HOST_FAMILIES.get(ctor_root)
                if ctor is not None and expr.callee.name in ctor:
                    dotted = f"{expr.callee.target.name}.{expr.callee.name}"
                    host_check(dotted, arg_types, filename, line)
                    return expr.callee.target.name
            target_t = infer_ast(expr.callee.target, tenv, types, filename)
            return builtin_check(expr.callee.name, target_t, arg_types, filename, line)
        # any other callee expression: an arrow applied in place, a function
        # value read out of a `let` chain, …
        callee_t = infer_ast(expr.callee, tenv, types, filename)
        if parse_type(callee_t)[0] == FN_HEAD:
            return call_function_value(expr, callee_t, "this call's callee",
                                       arg_types, tenv, types, filename, line)
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
        # An arrow in *inference* position has no expected type to read its
        # parameters off, so it is typed only when the author wrote them:
        # `(v: Int) => v + 1` has type `(Int) -> Int`. Without annotations the
        # arrow still has no type (it is the last item on the frontier the
        # header enumerates) — but its *body* is an ordinary expression over
        # the enclosing scope, and skipping it let every check above leak:
        # `(x) => s[0]` and `(x) => o.name` were accepted inside an arrow and
        # refused outside it. Parameters shadow into the unknown; free
        # variables keep their types.
        annotations = arrow_annotations(expr)
        inner = dict(tenv)
        for param, ptype in zip(expr.params, annotations):
            if ptype:
                inner[param] = ptype
            else:
                inner.pop(param, None)
        body_t = infer_ast(expr.body, inner, types, filename)
        if expr.params and any(a is None for a in annotations):
            return None
        return _resolve_arrow(expr, list(annotations), body_t)
    return None


def arrow_annotations(expr) -> list:
    """The author's `(v: Int) => ...` parameter annotations, one per
    parameter (None where the parameter was written bare)."""
    written = list(getattr(expr, "param_types", None) or [])
    written += [None] * (len(expr.params) - len(written))
    return written[:len(expr.params)]


def _resolve_arrow(expr, param_types: list, returns: str | None) -> str:
    """Record an arrow's now-known parameter/return types on the AST node and
    return its function type.

    Lowering reads these back off the node (`lower.py::_lower_pure_expr`), so
    the types the checker recovered are exactly the ones that reach the IR and
    the backends; an unknown component degrades to the `Any` wildcard rather
    than to a guess."""
    # `render_type` strips the implicit-type-parameter marker: the IR carries
    # the author's spelling, and the marker never leaves the checker (see
    # "type parameters" above). Without it, checking an arrow against an
    # uninstantiated `(T) -> T` would write `?T` into the IR.
    resolved = [render_type(p) or "Any" for p in param_types]
    expr.param_types = resolved
    expr.returns = render_type(returns) or "Any"
    return format_type(FN_HEAD, resolved + [expr.returns])


def _check_arrow_args(args, params, tenv: dict, types: dict,
                      filename: str | None, what: str) -> None:
    """Give arrow arguments their checking position.

    The generic argument loop compares *inferred* types, and an un-annotated
    arrow infers to nothing — so an arrow handed to a `fn` would stay both
    unchecked and untyped. Checking it against the declared parameter type is
    what types it, and what puts its parameter/return types on the AST node
    for lowering."""
    from .parser import ExprArrow

    if not filename:
        return
    for i, (arg, param) in enumerate(zip(args, params)):
        if isinstance(arg, ExprArrow) and parse_type(param)[0] == FN_HEAD:
            check_ast(arg, param, tenv, types, filename,
                      f"argument {i + 1} of {what}")


def call_function_value(expr, fn_type: str, what: str, arg_types: list,
                        tenv: dict, types: dict,
                        filename: str | None, line: int) -> str | None:
    """Type a call through a value of function type (a `let`-bound arrow, a
    parameter, a record field read into a `let` …)."""
    _, parts = parse_type(fn_type)
    params, returns = parts[:-1], parts[-1]
    if filename and len(expr.args) != len(params):
        rendered = ", ".join(render_type(p) or "_" for p in params) or "no arguments"
        raise RevlError(
            filename, line,
            f"{what} is a `{render_type(fn_type)}` and takes {len(params)} "
            f"argument(s), {len(expr.args)} given",
            hint=f"its parameters are `({rendered})` — revl has no default, "
                 "optional, or variadic parameters, so every call supplies "
                 "exactly the declared arity",
            code="T1", category="type-mismatch",
        )
    _check_arrow_args(expr.args, params, tenv, types, filename, what)
    if filename:
        for i, (p, a) in enumerate(zip(params, arg_types)):
            if p and a and not compatible(p, a):
                raise mismatch(filename, line, f"argument {i + 1} of {what}", p, a)
    return returns


def _check_arrow(expr, expected: str, ehead, eargs, tenv: dict, types: dict,
                 filename: str, where: str, line: int) -> None:
    """Check an arrow against an expected type — the checking position that
    takes arrows off the unchecked frontier.

    The expected function type supplies the parameter types the arrow never
    wrote, so the body is checked in an environment where they are known, and
    against the expected *return* type. A parameter the author did annotate
    must still accept what this position will pass it (contravariance), so an
    annotation can only ever be wider than the expectation."""
    if _is_wildcard(expected):
        infer_ast(expr, tenv, types, filename)
        return
    if ehead != FN_HEAD:
        raise RevlError(
            filename, line,
            f"{where} expects `{render_type(expected)}`, got an arrow",
            hint="an arrow is a function value; write the expected type as a "
                 "function type, e.g. `(Int) -> Str` (docs/function-types.md)",
            code="T1", category="type-mismatch",
            expected=render_type(expected), actual="a function value",
        )
    want_params, want_return = eargs[:-1], eargs[-1]
    if len(expr.params) != len(want_params):
        rendered = ", ".join(render_type(p) or "_" for p in want_params) or "no parameters"
        raise RevlError(
            filename, line,
            f"{where} expects `{render_type(expected)}` — {len(want_params)} "
            f"parameter(s), but this arrow declares {len(expr.params)}",
            hint=f"the expected parameters are `({rendered})`",
            code="T1", category="type-mismatch",
        )
    annotations = arrow_annotations(expr)
    resolved: list = []
    inner = dict(tenv)
    for name, written, want in zip(expr.params, annotations, want_params):
        if written and not compatible(written, want):
            raise mismatch(filename, line,
                           f"parameter `{name}` of this arrow (from {where})",
                           want, written)
        resolved.append(written or want)
        if resolved[-1]:
            inner[name] = resolved[-1]
        else:
            inner.pop(name, None)
    check_ast(expr.body, want_return, inner, types, filename,
              f"the body of this arrow (from {where})")
    _resolve_arrow(expr, resolved, want_return)


def check_ast(expr, expected: str | None, tenv: dict, types: dict,
              filename: str, where: str) -> None:
    """Bidirectional check of a parser-AST expression against `expected`."""
    from .parser import ExprArrow, ExprIf, ExprList, ExprMatch, ExprRecord

    line = getattr(expr, "line", 0)
    # a hole in check position takes the expectation as its type and is done:
    # it has no sub-expressions to check, and it satisfies whatever is asked
    # of it by construction (docs/holes.md)
    if pin_hole(expr, expected, filename, where):
        return
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
    if isinstance(expr, ExprArrow):
        _check_arrow(expr, expected, ehead, eargs, tenv, types, filename, where, line)
        return
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
            # the same bound the parser-AST checker enforces; IR literals come
            # from already-checked source, so this is the belt to its braces
            _reject_int_literal_range(filename, line, v)
            return "Int"
        if isinstance(v, float):
            return "Float"
        if isinstance(v, str):
            return "Str"
        return None
    if kind in ("format", "interp"):
        return "Str"
    if kind == "hole":
        # a lowered hole always carries the type it was admitted with
        return node.get("type")
    if kind == "name":
        return tenv.get(node.get("id"))
    if kind == "var":
        return tenv.get(node.get("name"))
    if kind == "config":
        return tenv.get(f"config.{node.get('field')}")
    if kind == "spawn":
        # a spawn expression yields an instance handle; the type names the
        # component instantiated (docs/design-v2-instances.md). It is a
        # host-frontier value — its one operation, `.dispose()`, is the
        # acquisition's inverse — so the type is advisory, like any acquired
        # value's, and is never structurally compared.
        return f"Instance[{node.get('component')}]"
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
        name = node.get("name")
        sig = (types.get(FNS_KEY) or {}).get(name)
        if sig is None:
            return None
        args = node.get("args") or []
        if filename and len(args) != len(sig["params"]):
            rendered = ", ".join(render_type(p) or "_"
                                 for p in sig["params"]) or "no arguments"
            raise RevlError(
                filename, line,
                f"`{name}` takes {len(sig['params'])} argument(s), {len(args)} given",
                hint=f"`{name}` is declared `({rendered})` — revl has no default, "
                     "optional, or variadic parameters, so every call supplies "
                     "exactly the declared arity",
                code="T1", category="type-mismatch",
            )
        subst: dict = {}
        for i, (p, a) in enumerate(zip(sig["params"], args)):
            at = infer_ir(a, tenv, types, services, filename, line)
            if unify(p, at, subst):
                continue
            if filename:
                raise mismatch(filename, line, f"argument {i + 1} of `{name}(...)`",
                               substitute(p, subst), at)
            return None
        return substitute(sig["returns"], subst)
    if kind == "builtin":
        target_t = infer_ir(node.get("target"), tenv, types, services, filename, line)
        args = [infer_ir(a, tenv, types, services, filename, line) for a in node.get("args") or []]
        return builtin_check(node.get("method"), target_t, args, filename, line)
    if kind == "maplit":
        # `Map.empty()` (docs/stdlib-2.0.md §Map): bottom-typed empty value.
        return "Map[Str, Never]"

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
        thead, targs = parse_type(target)
        if filename and thead == "Opt":
            raise opt_escape_error(filename, line,
                                   f"field access `.{node.get('name')}`", target,
                                   targs[0] if targs else None,
                                   alt=f"?.{node.get('name')}")
        spec = types.get(target or "")
        if spec is not None and spec.get("kind") == "record":
            return spec.get("fields", {}).get(node.get("name"))
        return None
    if kind == "index":
        target = infer_ir(node.get("target"), tenv, types, services, filename, line)
        thead, targs = parse_type(target)
        if filename and thead == "Opt":
            raise opt_escape_error(filename, line, "index `[...]`", target,
                                   targs[0] if targs else None)
        if filename and thead == "Str":
            raise RevlError(
                filename, line,
                "`Str` has no index operator — `[...]` indexes a `List` only",
                hint="use `s.charAt(i)` for the character (or `s.slice(i, j)` "
                     "for a substring, `s.charCodeAt(i)` for the code point) "
                     "— docs/stdlib-2.0.md",
                code="T1", category="type-mismatch",
            )
        if thead == "List":
            return targs[0] if targs else None
        if thead == "Str":
            return "Str"
        return None
    if kind == "if":
        return join(infer_ir(node.get("then"), tenv, types, services, filename, line),
                    infer_ir(node.get("else"), tenv, types, services, filename, line))
    return None

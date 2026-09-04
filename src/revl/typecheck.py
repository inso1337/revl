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
      names an arrow whose signature the lowering proved complete, but does
      not walk its body and types no call through one, so an arrow that
      reaches a `provide` method body is still unchecked inside. Stratum 1
      (`fn`/`test` bodies) is where function types are checked
      (docs/function-types.md §limits);
    * a `let x: T` annotation in a `provide` method body: the annotation is
      recorded so later reads are typed against it, but the bound value is
      not checked against it (same §limits).
- `Any` and `Value` launder in both directions by design (the gradual
  frontier and the erased dynamic document of stdlib/value.rvl, item 180).
  `Never` does NOT: it is the checker's inferred bottom (`List[Never]` for
  `[]`, `Map[Str, Never]` for `Map.empty()`) and it is uninhabited, so it is
  ONE-WAY. A `Never` flows out into any position, vacuously; nothing flows
  in. The two-way version was an unchecked cast with no cast syntax.
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
  B`) is an ordinary nominal type and is checked as one. An explicit `[T]`
  list (roadmap 75(c)) turns the implicit heuristic OFF for that signature:
  only the listed names are type parameters, and a stray one-letter name is an
  ordinary undeclared (opaque nominal) type that errors where it is used.

Two expression dialects are covered:
- `infer_ast`   — parser AST (Expr*) used by pure fn bodies (stratum 1);
  raises on definite operator/branch mismatches when `filename` is given.
- `infer_ir`    — lowered IR nodes used by component bodies (stratum 3).
- `check_ast` / `check_ir` are the CHECK positions of each dialect: an
  expectation pushed inward (a record literal named against a declared
  record's field set, each `if`/`match` arm checked on its own) rather than
  one `compatible` call on a joined inferred type. Both strata have one, and
  they must refuse the same programs (the item 392/404/405 parity contract).
"""

from __future__ import annotations

import dataclasses
import math

from .errors import RevlError

# reserved keys carried inside the `types` table (type names never start
# with an underscore, so these cannot collide)
FNS_KEY = "__fns__"      # {name: {"params": [type...], "returns": type|None}}
CASES_KEY = "__cases__"  # {case: {"adt": name, "payload": type|None}}

_NUMERIC = {"Int", "Float", "Int32"}
_SIZED_HEADS = {"Str", "Bytes", "List"}


# ---------------------------------------------------------------- algebra

# The head `parse_type` reports for a function type `(P, ...) -> R`, whose
# args are `[P, ..., R]` — the return type last. It is deliberately spelled
# with characters no identifier may contain, so it can never collide with a
# user type name the way a reserved word like `Fn` would.
FN_HEAD = "->"


def _split_top_level(text: str) -> list[str]:
    """Split on commas outside `[...]`, `(...)` and `{...}`.

    The brace clause keeps a *structural record type* (`{x: Int, y: Int}`,
    item 71) atomic when it appears as a field value or type argument, so a
    nested record shape is not split at its own comma.
    """
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "[({":
            depth += 1
        elif ch in "])}":
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


# ------------------------------------------------- structural record types
#
# An anonymous record literal (`let a = { h: "x" }`) has no nominal name to look
# up, so it used to infer as `None` — and an update `{ a | f = e }` on it escaped
# field checking entirely (the wrong-answer-class gap fenced in
# docs/contract-errata.md, "Record updates on receivers with no named type").
# It now infers a STRUCTURAL record type, spelled `{field: Type, ...}` with the
# fields in canonical (sorted) order, so the update is field-checked against the
# literal's own shape.
#
# This spelling lives ONLY inside the checker. Like item 11's `?T` widening
# marker it never reaches the IR: the `record`/`record_update` IR nodes carry no
# type, an inferred `let` type is not emitted, and the emitted `types` table is
# nominal (it filters reserved/synthetic keys). At any *declared* boundary the
# structural type unifies field-wise with the nominal record it meets — the
# `List[Never]` bottom rule falls straight out of the elementwise `compatible`
# recursion, since `Never` is already a wildcard there. `render_type` carries the
# shape verbatim (stripping only the `?T` marker), so a diagnostic reads it back
# as the author wrote it.
#
# "Unifies field-wise with the nominal record it meets" is now a CHECK, not a
# comment: `compatible` resolves the nominal side through the declared-type
# table (`nominal_record_fields`) and compares field sets and field types. It
# used to return True for EVERY structural-vs-anything pair on the theory that
# the boundary would catch a real mismatch; it did not, and a record literal was
# admitted as `Str`, `Bool`, an ADT, `List[Int]`, `(Int) -> Int` and `Int` — in
# both directions. An unresolvable head now fails CLOSED.

def structural_fields(name: str | None) -> dict[str, str | None] | None:
    """`"{a: Int, h: Str}"` -> `{"a": "Int", "h": "Str"}`; None if not one."""
    if not name:
        return None
    name = name.strip()
    if not (name.startswith("{") and name.endswith("}")):
        return None
    inner = name[1:-1].strip()
    if not inner:
        return {}
    fields: dict[str, str | None] = {}
    for part in _split_top_level(inner):
        key, sep, val = part.partition(":")
        if not sep:
            continue
        fields[key.strip()] = val.strip() or None
    return fields


def format_structural(fields: dict[str, str | None]) -> str:
    """Canonical spelling of a structural record type (fields sorted)."""
    body = ", ".join(f"{k}: {fields[k] or 'Any'}" for k in sorted(fields))
    return "{" + body + "}"


# builtin parametric type heads and their exact arity
_GENERIC_ARITY = {"Opt": 1, "List": 1, "Map": 2, "Result": 2}


def check_type_wellformed(filename: str, line: int, type_name: str | None,
                          *, allow_async_param: bool = False) -> None:
    """Reject a malformed declared type annotation (a builtin generic head
    used with the wrong number of arguments, e.g. bare `Opt` or `List`).
    Recurses into type arguments. User/nominal heads are not arity-checked.

    `Async[T]` (roadmap item 92) is a *position-restricted* annotation, never
    a value type: it is legal only as the return of a function type, and — in
    v1 — only when that function type is a module `fn` parameter
    (`allow_async_param=True`). Every other declaration site leaves the flag
    False, so an async function type there is refused with a "not yet" hint."""
    _check_type_wf(filename, line, type_name, type_name,
                   allow_async=allow_async_param, in_fn_return=False)


def _check_type_wf(filename: str, line: int, type_name: str | None,
                   root: str | None, *, allow_async: bool,
                   in_fn_return: bool) -> None:
    if not type_name:
        return
    head, args = parse_type(type_name)
    if head == "Approval":
        # item 246, Decision 3, invariant 5 (non-persistence): `Approval[C]` is
        # produced ONLY by `await approval[C]` and is non-denotable as a written
        # annotation, so it cannot appear in a snapshot, a handoff, a spawn
        # config, or any record/signature that would carry it across the session
        # boundary. A smuggled value fails the session binding at the crossing
        # (the runtime half); this keeps the honest program honest.
        raise RevlError(
            filename, line,
            f"`{type_name}` cannot be written as a type — an `Approval[C]` is "
            "produced only by `await approval[C] { ... }` and cannot be stored, "
            "returned, or persisted",
            hint="thread the approval to its crossing with `emit … with a` in the "
                 "same activation body; it may not cross a snapshot, handoff, or "
                 "spawn boundary (item 246, non-persistence)",
            code="G4", category="approval",
        )
    if head == "Async":
        if not in_fn_return:
            raise RevlError(
                filename, line,
                f"`Async[T]` is not a value type (`{type_name}`) — it may only "
                "be the return type of a function type, e.g. "
                "`(List[Msg]) -> Async[Str]`",
                hint="drop the `Async` wrapper here; a first-class async result "
                     "is carried by a function type's return, not by a value "
                     "(docs/design/async-function-values.md)",
                code="A1", category="async-propagation",
            )
        if not allow_async:
            raise RevlError(
                filename, line,
                f"an async function type (`{root}`) is only supported as a "
                "module `fn` parameter in v1, not in this position",
                hint="declare the async callback as a parameter of a top-level "
                     "`fn`, e.g. `fn agent_loop(…, complete: (List[Msg]) -> "
                     "Async[Str]) -> Str` (docs/design/async-function-values.md)",
                code="A1", category="async-propagation",
            )
        if len(args) != 1:
            raise RevlError(
                filename, line,
                f"`Async` takes 1 type argument, got {len(args)} (`{type_name}`)",
                hint="write e.g. `Async[Str]`",
            )
        # inside the wrapped T, `Async` may not appear again.
        _check_type_wf(filename, line, args[0], root,
                       allow_async=False, in_fn_return=False)
        return
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
    for i, arg in enumerate(args):
        # Only the *immediate* function type's return position may carry an
        # async color: a parameter that is itself a function type drops the
        # permission (higher-order async is a filed follow-up), while a
        # curried return keeps it.
        if head == FN_HEAD:
            is_return = i == len(args) - 1
            _check_type_wf(filename, line, arg, root,
                           allow_async=allow_async and is_return,
                           in_fn_return=is_return)
        else:
            _check_type_wf(filename, line, arg, root,
                           allow_async=False, in_fn_return=False)


# ------------------------------------------------- config is data, not a capability
#
# item 378 (docs/design/378-sync-extern-service-reach.md) asserts "Config is
# static data, not a capability, so the capability gate is untouched". That was
# only a COMMENT. `config_block` parses a field type with the FULL type grammar,
# so `config { handler: (Str) -> Str }` or `config { p: SomeService }` compiled,
# and a provider invoking `config.handler(x)` reached host emission with no
# ticket, no reach attribution, and no `emission[caps]` check: a live callable
# arrives at plug/spawn/load time (or through the embedding API) and is invoked
# past every authority fold. This makes the assertion a CHECK: a config value is
# injected as static data, so its declared type must be built, transitively, out
# of data. Scalars, records/ADTs/aliases, and `Opt`/`List`/`Map` of data are all
# fine, so the legitimate data-config feature (`config { url: Str, retries: Int,
# opts: Options }`) is untouched.
#
# The walk is an ALLOWLIST, not a denylist of two forbidden heads. Enumerating
# "not an arrow, not a service" left `Any` (and `Value`, `Never`, and every
# opaque nominal) passing: none of them is an arrow head or a declared service
# name, none resolves in `type_defs`, and none has type arguments — so the walk
# fell off its own end and admitted the field. `compatible` then treats `Any`
# as a wildcard, so `config.v("payload")` typechecks as a CALL and emits
# `_revl_config['v']('payload')`: a live callable injected at plug/spawn/load
# time is invoked past every authority fold, which is item 378 verbatim. A head
# that is not *provably* data is now refused, so a new denotable type cannot
# reopen the hole merely by not being on a list.

_CONFIG_DATA_HINT = (
    "a config field must be static data (a scalar, or a record/list/Opt of "
    "data); an arrow type / a service cannot be a config field, because config "
    "is not a capability channel (item 378)"
)

# Every head a config field may reach. Anything else is refused: a config value
# is deserialised from a static table, so its type must name a shape that table
# can hold.
_CONFIG_DATA_SCALARS = {"Int", "Int32", "Float", "Str", "Bool", "Bytes", "Unit"}
_CONFIG_DATA_CONTAINERS = {"Opt", "List", "Map", "Result"}
# The erased/dynamic and bottom types. Each is a `compatible` wildcard in at
# least one direction, so admitting one as a config field would hand the body a
# value of *any* runtime shape — a callable included — with no refusal left
# downstream. `Never` is additionally uninhabited, so a `Never` config field
# could never be supplied honestly in the first place.
_CONFIG_ERASED = {"Any", "Value", "Never"}


def check_config_field_is_data(filename: str, line: int, field_name: str,
                               owner: str, type_name: str | None, *,
                               service_names: set[str],
                               type_defs: dict) -> None:
    """Refuse a config field whose declared type can carry a live callable or a
    capability. `service_names` is the set of declared `service` names; each
    entry of `type_defs` is a lowered type-table shape
    (`{"kind": "record", "fields": {name: type}}` or
    `{"kind": "variant", "cases": [{"name":…, "payload":…}]}`), used to resolve a
    nominal record/ADT/alias into its component types."""
    _walk_config_type(filename, line, field_name, owner, type_name, type_name,
                      service_names=service_names, type_defs=type_defs,
                      visited=frozenset(), tparams=frozenset())


def _config_data_error(filename: str, line: int, field_name: str, owner: str,
                       root: str | None, offender: str) -> RevlError:
    return RevlError(
        filename, line,
        f"config field `{field_name}` of {owner} has type `{root}`, which "
        f"reaches {offender}; a config field must be static data",
        hint=_CONFIG_DATA_HINT,
        code="G4", category="config-data",
    )


def _is_type_expression(name: str | None, type_defs: dict,
                        tparams: frozenset) -> bool:
    """Is this "case name" actually an alias RHS rather than a nullary tag?

    `type Rows = List[Row]` and `type H = (Str) -> Str` both parse into a
    single "case" whose *name* is the aliased type; `type Color = Red | Green`
    parses into nullary tags that name nothing. A spelling that carries type
    arguments, a function arrow, a record shape, a builtin type name, a type
    parameter in scope, or a declared type is the former."""
    if not name:
        return False
    if structural_fields(name) is not None:
        return True
    head, args = parse_type(name)
    return bool(args) or head == FN_HEAD or head in _BUILTIN_TYPE_NAMES \
        or head in tparams or head in type_defs


def _walk_config_type(filename: str, line: int, field_name: str, owner: str,
                      type_name: str | None, root: str | None, *,
                      service_names: set[str], type_defs: dict,
                      visited: frozenset, tparams: frozenset) -> None:
    if not type_name:
        return
    type_name = type_name.strip()

    def walk(target, *, visited=visited, tparams=tparams):
        _walk_config_type(filename, line, field_name, owner, target, root,
                          service_names=service_names, type_defs=type_defs,
                          visited=visited, tparams=tparams)

    # A structural record literal type `{a: T, ...}` (item 71) carries its field
    # types inline; recurse into each so a smuggled arrow field is caught.
    sfields = structural_fields(type_name)
    if sfields is not None:
        for ftype in sfields.values():
            walk(ftype)
        return
    head, args = parse_type(type_name)
    if head == FN_HEAD:
        raise _config_data_error(filename, line, field_name, owner, root,
                                 "an arrow (function) type")
    if head in service_names:
        raise _config_data_error(filename, line, field_name, owner, root,
                                 f"the service `{head}`")
    if head in tparams:
        # A type parameter of the record/ADT currently being resolved
        # (`type Box[T] = { v: T }` reached from `config { b: Box[Int] }`). Its
        # binding is the type argument at the use site, which this walk visits
        # in its own right, so the parameter itself carries nothing.
        return
    if head in _CONFIG_DATA_CONTAINERS or head in _CONFIG_DATA_SCALARS:
        for arg in args:
            walk(arg)
        return
    info = type_defs.get(head or "")
    if info is None:
        # Not a scalar, not a container, not a declared record/ADT/alias, not a
        # type parameter in scope: nothing here proves the field is data, so the
        # walk refuses rather than falling off its end (item 378).
        offender = (f"the erased type `{head}`" if head in _CONFIG_ERASED
                    else f"the opaque type `{head}`")
        raise _config_data_error(filename, line, field_name, owner, root,
                                 offender)
    # A nominal record/ADT/alias: resolve it and walk its component types.
    # Guard against a recursive type (`type Tree = Node(Tree)`) with the
    # visited set (a cycle through data heads is still data).
    if head not in visited:
        child_visited = visited | {head}
        child_tparams = frozenset(info.get("params") or ())
        if info.get("kind") == "record":
            for ftype in (info.get("fields") or {}).values():
                walk(ftype, visited=child_visited, tparams=child_tparams)
        else:
            for case in info.get("cases") or []:
                # A variant case carries either a parenthesised payload
                # (`Hit(Row)`) or, for an alias RHS (`type Rows = List[Row]`),
                # the target type spelled as the sole case name. A NULLARY tag
                # (`type Color = Red | Green`) carries no data at all, so it is
                # not a type to walk — telling the two apart matters now that an
                # unresolvable head is refused instead of ignored.
                payload = case.get("payload")
                if payload is not None:
                    walk(payload, visited=child_visited, tparams=child_tparams)
                    continue
                name = case.get("name")
                if _is_type_expression(name, type_defs, child_tparams):
                    walk(name, visited=child_visited, tparams=child_tparams)
    # A user generic head carries data in its type arguments too; walk them with
    # the OUTER type-parameter scope (they are written at this use site).
    for arg in args:
        walk(arg)


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


# item 386, Stage 2: the poison sentinel. When a component-body statement's
# type-check raises a definite mismatch (T1/T2), the statement-boundary recovery
# in `lower.py` records that ONE diagnostic and, for a binding form, binds the
# failed value to `POISON` in the type environment before resuming at the next
# statement. `POISON` is ABSORBING (every algebra operation treats it as a
# wildcard, so a type derived from it stays unknown) and SILENT (it is
# compatible with everything in both directions, so no later use of the poisoned
# binding raises a second, fabricated mismatch). A diagnostic is emitted only
# where poison is BORN — the failing statement — never where it PROPAGATES,
# which is exactly what stops one real mismatch from spawning N cascades at every
# later use of the poisoned binding. Like `_TPARAM` and `FN_HEAD`, the spelling
# uses characters no identifier or declared type may contain, so it can never
# collide with a user type and never has to be stripped from a rendered
# diagnostic (poison is silent, so it never reaches one).
POISON = "!poison"


def is_poison(type_name: str | None) -> bool:
    """Is this the Stage-2 poison sentinel (item 386)?"""
    return type_name == POISON


def is_tparam_name(name: str, declared: dict) -> bool:
    """Would `name`, written in a fn signature, be an implicit type parameter?"""
    return len(name) == 1 and name.isupper() and name not in declared


# type heads that always name a concrete type; an explicit `[T]` parameter may
# not shadow one (nor a user-declared type). See `validate_explicit_tparams`.
_BUILTIN_TYPE_NAMES = {
    "Int", "Int32", "Float", "Str", "Bool", "Bytes", "Unit",
    "Opt", "List", "Map", "Result", "Any", "Never",
    # `Value` — the stdlib erased-dynamic type (stdlib/value.rvl, item 180): a
    # reserved builtin so no `[Value]` tparam or `type Value = ...` may shadow
    # the type whose total accessor surface navigates a dynamic IR document.
    "Value",
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
    signature generic — exactly the same set the implicit path would build.

    Type-parameter hygiene (roadmap 75(c)): an explicit list turns the
    implicit heuristic OFF for that signature. Declared means declared — with
    `fn f[T](...)`, only `T` is a type parameter; a stray one-letter name
    (`E`) is an ordinary undeclared (opaque nominal) type and is checked like
    any other, so a typo'd name errors where it is used instead of silently
    quantifying and wildcarding at every call site (docs/generics.md)."""
    explicit = set(explicit)
    found: set[str] = set(explicit)
    implicit = not explicit  # `[T]` present => declared means declared

    def walk(name: str | None) -> None:
        if not name:
            return
        head, args = parse_type(name)
        if head and not args and (head in explicit
                                  or (implicit and is_tparam_name(head, declared))):
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


def unify(param: str | None, actual: str | None, subst: dict,
          types: dict | None = None) -> bool:
    """Match a (marked) parameter type against an argument type, growing
    `subst`. Returns False only on a *definite* conflict; unknowns pass."""
    if param is None or actual is None:
        return True
    head, args = parse_type(param)
    if head and head.startswith(_TPARAM) and not args:
        if _is_wildcard(actual):
            return True  # nothing to learn from an unknown argument
        if parse_type(actual)[0] == "Async":
            # A generic combinator must not smuggle an async color into an
            # expression type through a bound `?T` (item 92 §2); refuse rather
            # than widen. `substitute` therefore can never produce `Async`.
            return False
        prior = subst.get(head)
        if prior is None:
            subst[head] = actual
            return True
        widened = join(prior, actual, types)
        if widened is None:
            return False
        subst[head] = widened
        return True
    ahead, aargs = parse_type(actual)
    if head == "Opt" and args and ahead != "Opt":
        return unify(args[0], actual, subst, types)  # T -> Opt[T] injection
    if head == ahead and len(args) == len(aargs):
        return all(unify(p, a, subst, types) for p, a in zip(args, aargs))
    return compatible(param, actual, types)


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
        or name == POISON            # item 386 Stage 2: absorbing + silent
        or name.startswith(_TPARAM)  # implicit fn type parameter
    )


def nominal_record_fields(type_name: str | None,
                          types: dict | None) -> dict | None:
    """`{field: type}` of a DECLARED nominal record type, else None.

    A generic record resolves at its instantiation: `Box[Int]` against
    `type Box[T] = { v: T }` yields `{v: Int}`, the declared field types with
    the type arguments substituted. A wrong arity, or a head that is not a
    declared record, stays unresolved — and an unresolved head is REFUSED
    where a structural record meets it, never admitted."""
    if not types or not type_name:
        return None
    head, args = parse_type(type_name)
    if not head:
        return None
    spec = types.get(head)
    if not (isinstance(spec, dict) and spec.get("kind") == "record"):
        return None
    fields = spec.get("fields") or {}
    params = list(spec.get("params") or ())
    if not params:
        return fields if not args else None
    if len(args) != len(params):
        return None
    subst = dict(zip(params, args))
    return {name: substitute(ftype, subst) for name, ftype in fields.items()}


def compatible(expected: str | None, actual: str | None,
               types: dict | None = None) -> bool:
    """May a value of type `actual` flow into a position typed `expected`?

    `types` is the declared-type table, when the caller has one. It is what
    lets a STRUCTURAL record type (`{id: Int}`, item 71) be resolved against
    the nominal record it meets. Without it a structural type meeting anything
    but another structural type is refused: the pre-fix `return True` there was
    a two-way unchecked cast reachable from any unannotated expression
    position (F3), admitting a record literal as `Str`, `Bool`, an ADT,
    `List[Int]`, `(Int) -> Int` and `Int`, and admitting a scalar into a
    structural field. Fail closed, and give every real boundary the table."""
    # `Never` is the checker's inferred BOTTOM (`List[Never]` for `[]`,
    # `Map[Str, Never]` for `Map.empty()`), and it used to sit in `_is_wildcard`
    # beside `Any`, which made it launder in BOTH directions: `pub fn nv(x:
    # Never) -> Int { return x }` admitted `nv("s")`, and the same hole one
    # level down admitted a `List[Int]` into a `List[Never]` parameter and then
    # read its elements out as anything. That is the F3 unchecked cast under a
    # different spelling. A bottom is one-way: it flows OUT of a `Never`
    # position into any other (vacuously, since nothing inhabits it), and
    # NOTHING flows in. So the bottom keeps every inferred spelling denotable
    # and usable (`let xs: List[Int] = []` and a written `Map[Str, Never]`
    # receiver still check, and a value-type mismatch under one still reports
    # the specific mismatch) while the laundering direction is refused.
    # `Any`/`Value` keep their documented two-way laundering (item 180), so an
    # `Any` on the actual side still flows in.
    if parse_type(expected)[0] == "Never" and not _is_wildcard(actual):
        return False
    if _is_wildcard(expected) or _is_wildcard(actual):
        return True
    # `Value` is the stdlib erased-dynamic type (stdlib/value.rvl, roadmap item
    # 180): the runtime union of every host-representable shape (record / list /
    # scalar / null). Any concrete value flows INTO a `Value` position (it is
    # boxed into the host's dynamic representation), and a `Value` flows OUT into
    # any typed position (the total accessors are the checked exits). So — like
    # `Any`, but a NAMED, reusable type with a documented accessor surface — it
    # is compatible with anything in both directions. Narrower than adding it to
    # `_is_wildcard`: this touches value-flow compatibility only, leaving tparam
    # unification and inference joins to treat `Value` as the ordinary nominal
    # it is. A `List[Value]` / `Opt[Value]` reaches this rule elementwise via the
    # same-head recursion below, so a container of dynamic values is admitted too.
    if parse_type(expected)[0] == "Value" or parse_type(actual)[0] == "Value":
        return True
    if expected == actual:
        return True
    e_struct = structural_fields(expected)
    a_struct = structural_fields(actual)
    # A structural record meets a NOMINAL record field-wise (item 71). That is
    # the ONE mixed case that may be admitted, and it needs the declared-type
    # table to resolve the nominal's fields; resolution is one-sided on purpose,
    # so two *nominal* records never become structurally interchangeable.
    if e_struct is not None and a_struct is None:
        a_struct = nominal_record_fields(actual, types)
    elif a_struct is not None and e_struct is None:
        e_struct = nominal_record_fields(expected, types)
    if e_struct is not None and a_struct is not None:
        # Two record shapes unify field-wise: the same field set, each value
        # type compatible (the `List[Never]` bottom rule is the elementwise
        # recursion, `Never` being a wildcard).
        if set(e_struct) != set(a_struct):
            return False
        return all(compatible(e_struct[k], a_struct[k], types) for k in e_struct)
    # An unresolved mixed case falls through to the head algebra below. A
    # structural type's canonical spelling (`{a: Int}`) is its own head with no
    # arguments, so it matches no scalar, container, arrow or ADT head and the
    # tail returns False — while `Opt`/`Async` on the *expected* side still get
    # their injection/coercion rules first, and can reach the record inside.
    ehead, eargs = parse_type(expected)
    ahead, aargs = parse_type(actual)
    if ehead == "Float" and ahead == "Int":
        return True  # numeric widening
    # Int32 widens losslessly into Int and (through Int) into Float. Both are
    # implicit at value-flow positions; the reverse (Int -> Int32) can lose
    # bits and is refused here, so it must be spelled `.to_int32()`
    # (docs/arithmetic.md, "Sized integers").
    if ehead in ("Int", "Float") and ahead == "Int32":
        return True
    if ehead == "Async":
        # `Async[T]` appears only as an *expected* return type (wellformedness
        # confines it to a function-type return). A sync value coerces in — a
        # non-suspending function is a degenerate async one — so
        # `compatible("Async[T]", actual)` reduces to `compatible(T, actual)`.
        # The reverse (`compatible("T", "Async[U]")`) is a head mismatch and
        # falls through to `False` below: async never silently flows into sync.
        # Two async returns (`Async[T]` vs `Async[U]`) meet elementwise via the
        # generic same-head rule at the tail of this function.
        if ahead != "Async":
            return compatible(eargs[0] if eargs else None, actual, types)
    if ehead == FN_HEAD:
        # A function value flows where a function type is expected only if it
        # accepts everything that position will pass it and returns something
        # the position can use: parameters contravariant, result covariant.
        # The generic elementwise rule below would make parameters covariant,
        # which accepts `(Int) -> X` where `(Float) -> X` is required and then
        # hands the callee a Float.
        if ehead != ahead or len(eargs) != len(aargs):
            return False
        return (all(compatible(a, e, types) for e, a in zip(eargs[:-1], aargs[:-1]))
                and compatible(eargs[-1], aargs[-1], types))
    if ehead == "Opt":
        einner = eargs[0] if eargs else None  # bare `Opt` degrades to wildcard
        if ahead == "Opt":
            return compatible(einner, aargs[0] if aargs else None, types)
        return compatible(einner, actual, types)  # T -> Opt[T] injection
    if ehead == ahead and len(eargs) == len(aargs):
        return all(compatible(e, a, types) for e, a in zip(eargs, aargs))
    return False


def join(a: str | None, b: str | None, types: dict | None = None) -> str | None:
    """Common type of two branches, or None when unknown."""
    if a is None or b is None:
        return None
    if compatible(a, b, types):
        return a
    if compatible(b, a, types):
        return b
    return None


def widen_bottom(declared: str | None, actual: str | None,
                 types: dict | None = None) -> str | None:
    """Widen a binding whose type still carries the checker's inferred BOTTOM.

    `var m = Map.empty()` types `m` as `Map[Str, Never]` and `var xs = []` as
    `List[Never]`: the accumulator idiom names its element type at the first
    reassignment, not at the declaration. `Never` is one-way (nothing flows
    into a bottom), so `m = m.set(k, 1)` is not a `compatible` flow. It is the
    point where the element type is LEARNED, exactly as `builtin_check` learns
    it for a bottom-typed receiver.

    Returns the widened type when `actual` only fills bottoms in `declared`
    (same head, same arity, every non-bottom position still compatible), else
    None so the caller reports the mismatch. This is deliberately NOT `join`:
    join would also widen `Int` to `Float`, which is a real refusal at an
    assignment."""
    if not declared or not actual:
        return None
    if declared == "Never":
        return actual
    dhead, dargs = parse_type(declared)
    ahead, aargs = parse_type(actual)
    if not dargs or dhead != ahead or len(dargs) != len(aargs):
        return None
    widened: list[str] = []
    grew = False
    for d, a in zip(dargs, aargs):
        inner = widen_bottom(d, a, types)
        if inner is not None and inner != d:
            widened.append(inner)
            grew = True
        elif compatible(d, a, types):
            widened.append(d)
        else:
            return None
    return format_type(dhead, widened) if grew else None


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


def incomparable(filename: str, line: int, op: str,
                 lt: str | None, rt: str | None) -> RevlError:
    """Two operands of `==`/`!=` that have no common type.

    NOT `mismatch`: equality has no expected/actual direction (neither side is
    the position the other must fit), and `mismatch`'s renderer supplies its own
    verb ("... expects `X`, got `Y`"). Composing the two produced the
    ungrammatical "`==` comparison between `Rec0` and expects `{f0: Bool}`, got
    `Rec0`". The `expected`/`actual` fields are still populated (left, then
    right) so the structured consumers that read them off a T1 keep working."""
    left, right = render_type(lt), render_type(rt)
    return RevlError(
        filename, line,
        f"`{op}` cannot compare `{left}` with `{right}` — the operands have "
        "no type in common",
        hint="compare values of the same type, or destructure the two sides "
             "and compare their fields",
        code="T1", category="type-mismatch",
        expected=left, actual=right)


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
                filename: str | None, line: int, types: dict | None = None):
    if op in ("==", "!=", "===", "!=="):
        # `types` is the declared-type table: without it a record LITERAL
        # compared with a value of a declared record type (`p == { x: 1, y: 2 }`)
        # meets an unresolvable nominal and fails closed now that `compatible`
        # decides structural-vs-nominal instead of waving it through.
        if filename and lt and rt and not (compatible(lt, rt, types)
                                           or compatible(rt, lt, types)):
            raise incomparable(filename, line, op, lt, rt)
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
    if op in ("&", "|", "^", "<<", ">>"):
        # Bitwise operators are Int32-only (item 366, docs/arithmetic.md).
        # Int (64-bit) is deliberately excluded: `<<` grows without bound on
        # the arbitrary-precision hosts (python `int`, ts `BigInt`), so a
        # uniform 64-bit two's-complement shift would need a re-imposed wrap on
        # exactly those tiers — a per-tier divergence, the kind the sized-int
        # design avoids. Int32 has a fixed hardware width everywhere, so the
        # lowering is uniform. Bitwise ops do NOT trap (bit patterns, not
        # arithmetic); `>>` is the arithmetic (sign-extending) shift and the
        # shift count is taken mod 32. For a shift, both operands are Int32
        # (the count too) and the result is Int32.
        for t in (lt, rt):
            if filename and t and parse_type(t)[0] != "Int32":
                is_int = parse_type(t)[0] == "Int"
                raise RevlError(
                    filename, line,
                    f"`{op}` requires `Int32` operands, got `{render_type(t)}`",
                    hint=("bitwise operators are Int32-only — narrow with "
                          "`.to_int32()` (docs/arithmetic.md)") if is_int else
                         "bitwise operators are Int32-only (docs/arithmetic.md)",
                    code="T1", category="type-mismatch")
        return "Int32"
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
        # `+ - * %` do not silently mix widths. Int32 widens to Int only at
        # value-flow positions (let/return/argument), never inside arithmetic:
        # a mixed-width binop is a request to make the conversion visible, not a
        # place to hide one. `.to_int()` widens, `.to_int32()` narrows
        # (docs/arithmetic.md). `/` is exempt above — it always yields Float, so
        # both sides widen to Float regardless and nothing is lost.
        if "Int32" in (lt, rt) and lt != rt and lt is not None and rt is not None:
            if filename:
                raise RevlError(
                    filename, line,
                    f"`{op}` does not mix `{render_type(lt)}` and "
                    f"`{render_type(rt)}`",
                    hint="convert explicitly — `.to_int()` widens an Int32 to "
                         "Int, `.to_int32()` narrows an Int to Int32 "
                         "(docs/arithmetic.md)",
                    code="T1", category="type-mismatch")
            return None
        if op == "%" and (lt == "Int32" or rt == "Int32"):
            # `%` (and the named `div_*`/`mod`) stay Int-only in this pass: the
            # remainder is width-agnostic in value but its zero-divisor *fault*
            # is not uniform once Int32 is a `number` on ts / an i32 on wasm.
            # Widen with `.to_int()` to take a remainder (docs/arithmetic.md).
            if filename:
                raise RevlError(
                    filename, line,
                    "`%` is Int-only; widen the Int32 operands with `.to_int()` "
                    "first (docs/arithmetic.md)",
                    code="T1", category="type-mismatch")
            return None
        if lt == "Float" or rt == "Float":
            return "Float"
        if lt == "Int32" and rt == "Int32":
            return "Int32"
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
    # Codepoint-at-index scan (roadmap item 276, docs/stdlib-2.0.md
    # §Str.codepoint_at): the Unicode scalar at code-point index i, returned
    # directly so the self-host lexer's per-byte hot path stops spelling it as
    # `code0(source.charAt(j))` (a 1-char Str alloc + a revl-fn round-trip).
    # Str receiver, one Int index, Int result — the family of charCodeAt.
    "codepoint_at": ("Str", ["Int"], "Int"),
    "concat": ("sized", ["@self"], "@self"),
    "indexOf": ("sized", ["@member"], "Int"),
    "split": ("Str", ["Str"], "List[Str]"),
    "join": ("List", ["Str"], "Str"),
    "repeat": ("Str", ["Int"], "Str"),
    # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith):
    # protocol parsing is prefix-tagged (`FINAL `, `TOOL_CALL `), and the
    # harness hit a real off-by-one (`"TOOL_CALL "` is 10 chars, sliced 9)
    # that `slice`-then-compare cannot catch. Str-only, matching the family
    # of the other Str-builtins.
    "startsWith": ("Str", ["Str"], "Bool"),
    "endsWith": ("Str", ["Str"], "Bool"),
    # Single-character ASCII classification (roadmap item 233, docs/stdlib-2.0.md
    # §Str.is_alnum). Str-only, no argument, Bool result — the family of the
    # other Str builtins. Recognized so the self-host lexer's per-byte hot path
    # (`is_alnum(source.charAt(j))` etc.) lowers to a native inline test on the
    # py tier instead of a revl-fn call. ASCII, single code point: is_digit is
    # `0`-`9`; is_alpha is `a`-`z`/`A`-`Z` (letters only — NOT `_`); is_alnum is
    # their union; is_space is space/tab/LF/CR. The receiver is a one-char Str
    # (an empty receiver is false and no input faults; multi-character input is
    # outside the per-character contract — the lexer only ever hands one char).
    "is_alnum": ("Str", [], "Bool"),
    "is_digit": ("Str", [], "Bool"),
    "is_alpha": ("Str", [], "Bool"),
    "is_space": ("Str", [], "Bool"),
    # Integer division and modulo, named rather than defaulted (§0 keeps `/`
    # and `%` meaning what TypeScript means by them; these say what they do).
    # docs/arithmetic.md gives the definitions and the divergence they close.
    "div_trunc": ("Int", ["Int"], "Int"),
    "div_floor": ("Int", ["Int"], "Int"),
    "div_euclid": ("Int", ["Int"], "Int"),
    # Width conversions between Int and Int32 (docs/arithmetic.md, "Sized
    # integers"). `.to_int()` is the explicit spelling of the lossless Int32 ->
    # Int widening (which is also implicit at value-flow positions).
    # `.to_int32()` narrows Int -> Int32; it can lose bits, so it is ALWAYS
    # explicit and re-imposes the 32-bit bound at runtime, trapping
    # `revl: Int32 overflow` exactly as Int traps at the i64 edge.
    # `to_int` is also the `Str -> Opt[Int]` parsing builtin (FR-9): the first
    # method whose spelling is shared by two receiver families, so its entry
    # is a dict keyed by family rather than a single sig — `builtin_check`
    # picks the row by the receiver head and refuses a receiver that matches
    # neither.
    "to_int": {
        "Int32": ("Int32", [], "Int"),
        "Str": ("Str", [], "Opt[Int]"),
    },
    "to_int32": ("Int", [], "Int32"),
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
    # The iteration/remove step (docs/stdlib-2.0.md §Map): size and keys are
    # methods like their siblings (no free-function namespace); keys() yields
    # ascending canonical Str order — a pure function of the key set, pinned
    # per tier by tests. remove() is persistent (new map) and TOTAL: an
    # absent key is a no-op returning an equal map, never an error.
    "size": ("Map", [], "Int"),
    # `keys` is spelled for two receiver families: the Map key set (above) AND
    # the Value record-key enumeration (roadmap item 189 — the dot-method form
    # of stdlib/value.rvl's `value_keys`). Select the row by the receiver head,
    # exactly as `to_int` does (Int32-widen vs Str-parse). The Value row is one
    # of the four Value dot-accessors (`.field`/`.str`/`.list`/`.keys`); each
    # lowers to a plain call of its `value_*` free function, so the dot form is
    # PURE SUGAR that emits byte-identically to the nested free-function form.
    "keys": {
        "Map": ("Map", [], "List[Str]"),
        "Value": ("Value", [], "List[Str]"),
    },
    # ------------------------------------------------------------- Value access
    # The Value dot-method accessors (roadmap item 189, DECIDED option A):
    # receiver-first sugar for stdlib/value.rvl's `value_*` free functions, so
    # `node.field("callee").field("name").str()` reads left-to-right instead of
    # the inside-out `value_str(value_field(value_field(node, "callee"),
    # "name"))`. Registered here (and in lower._BUILTIN_METHODS) as builtin
    # methods like `.charAt()` — NO new IR expr-kind. Each lowers to a plain
    # CALL of its `value_*` equivalent (value_field/value_str/value_list, and
    # `keys` above), so the emitted code is byte-identical to the free-function
    # spelling on every tier value.rvl runs on. `.keys()` shares its name with
    # `Map.keys()` above, hence the multi-receiver dict row. Total accessors:
    # a shape mismatch reads back a typed default (never a fault), inherited
    # verbatim from the free-function bodies (docs/stdlib-value.md).
    "field": ("Value", ["Str"], "Value"),
    "str": ("Value", [], "Str"),
    "list": ("Value", [], "List[Value]"),
    "remove": ("Map", ["Str"], "@self"),
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str): decimal
    # spelling, total over the whole i64 range including Int.MIN. A method
    # on the Int family — the same dispatch div_trunc rides — because revl
    # has no free-function namespace to pollute.
    "to_str": ("Int", [], "Str"),
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
    "Map.insert_if_absent": ["Str", "Str"],
    "Map.remove": ["Str"],
    "Map.get": ["Str"],
    "Pool.open": ["Str", "Int"],
    "Pool.close": [],
    "Pool.query": ["Str"],
    "Pool.execute": ["Str"],
    "Job.run": ["Str"],
    # item 130 (docs/design/130-stream-reactive-types.md). Exactly the Slice 1
    # revl SURFACE, no more: `Stream.source()` opens the reference-tier provider,
    # and `close` is the terminal-delivering inverse the subscription's core
    # guarantee rests on; `Subscription` is the host-local family a `subscribe`
    # binds — `next` awaits an item raced against a cancel token, `close` is the
    # synchronous bracket inverse. Results stay opaque.
    #
    # The provider-DRIVING verbs (`emit`/`fault`) are deliberately absent. A
    # Slice 1 test provider is driven from the harness, never from revl source
    # (design §8: "a test provider `emit`s items explicitly"), and `emit` is a
    # revl keyword that cannot parse as a method call at all. Listing them would
    # grow the shared host-verb namespace — the disjointness invariant pinned in
    # tests/test_map_value_type.py — for a surface no admitted program can reach.
    # item 130 Slice 3 adds NO verb here: `merge(a, b)` is parsed in the
    # `subscribe` head (parser.subscribe_form), the one position the surface
    # already controls, exactly as Slice 2 parses `map`/`filter`/`take` as
    # stages. A `<src>.merge(..)` method spelling would grow this shared
    # namespace for a combinator, which is what the disjointness invariant
    # forbids — so the exact-set pin in tests/test_map_value_type.py is
    # untouched by this slice.
    "Stream.source": [],
    "Stream.close": [],
    "Subscription.next": [],
    "Subscription.close": [],
}


# Host builtin *results* (item 397). The frontier deliberately types arguments
# and not results — a host stub returns a host-valued object the frontend does
# not model (see the _HOST_ARG_SIG header). `insert_if_absent` is the first
# host verb whose result the program MUST consume in checked code: the atomic
# CAS returns a `Bool` the caller branches on with a pure `if`. So the frontier
# gains a result column for exactly the verbs that declare one; every other
# host verb's result stays opaque (`builtin_check` returns None below). This is
# a deliberate one-verb breach of the opaque-result frontier, not a policy
# change (docs/design/397-insert-if-absent.md §The result type).
_HOST_RESULT_SIG: dict[str, str] = {
    "Map.insert_if_absent": "Bool",
}


# Host ACQUIRE verbs: the constructor of a host family whose release is a
# SEPARATE verb, mapped to that release. Calling one opens a host resource that
# nothing reclaims until its inverse runs, so the call belongs in an acquisition
# bracket (`effect <acquire> undo <release>`) and nowhere else — the bracket is
# what registers the release with the activation's teardown accumulator.
#
# Derived by hand rather than from `_HOST_ARG_SIG` because the pairing is a
# semantic claim, not a naming one: `Job.run` is deliberately absent (a job is
# fire-and-forget; the family declares no release verb), and `Map.empty` is a
# VALUE constructor, not a host acquisition, intercepted long before any host
# path.
_HOST_ACQUIRE_VERBS: dict[str, str] = {
    "Map.new": "drop",
    "Pool.open": "close",
    "Stream.source": "close",
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


# The stdlib method table (`_BUILTIN_SIG` here, `_BUILTIN_METHODS` in
# lower.py) and the host-verb surface (`_HOST_FAMILIES` above) must stay
# disjoint except for the ONE documented overlap (`remove`,
# docs/stdlib-2.0.md §Map). This is checked HERE, at table-edit time — the
# moment either module is imported — not at golden-diff time: the mapiter run
# proved that editing one table silently reclassifies every call site on the
# other side (adding `remove` turned every host stub's `store.remove(k)` into
# a stdlib builtin and broke 16 tests) with the disjointness invariant living
# only in a test assertion (dogfood/findings-mapiter.md §2). Dispatch is by
# receiver kind, so any overlap beyond the sanctioned one is a landmine the
# tests only find later.
_HOST_VERB_NAMES = frozenset(
    _method for _methods in _HOST_FAMILIES.values() for _method in _methods)
_SANCTIONED_OVERLAP = frozenset({"remove"})


def _check_method_namespace_disjoint(builtin_names, table: str) -> None:
    """Fail at table-edit time if a stdlib method name collides with a host
    verb beyond the documented `remove` overlap."""
    extra = set(builtin_names) & _HOST_VERB_NAMES - _SANCTIONED_OVERLAP
    if extra:
        raise RuntimeError(
            "stdlib/host method-name collision in " + table + ": "
            + ", ".join(sorted(f"`{n}`" for n in extra))
            + " appear in both the stdlib method table and the host stub "
            "surface; only `remove` is a documented overlap "
            "(docs/stdlib-2.0.md §Map) — rename one side at the table, "
            "not in a test"
        )


_check_method_namespace_disjoint(_BUILTIN_SIG, "_BUILTIN_SIG")


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
                  filename: str | None, line: int,
                  types: dict | None = None) -> str | None:
    """Type a stdlib method call; raises on definite mismatches.

    `types` is the declared-type table, passed through to `compatible` so a
    structural record literal argument still meets the nominal element type of
    the receiver (`xs: List[Row]` then `xs.push({ id: 1 })`). Without it that
    comparison fails closed, which is right for an unresolvable head and wrong
    for a resolvable one."""
    # item 386, Stage 2: a poisoned receiver (a binding whose initializer was a
    # recovered type error) is folded to the unknown receiver here, so the
    # receiver-family refusals below stay silent — `compatible` already treats
    # POISON as a wildcard, but the family checks compare type heads directly,
    # and re-reporting a mismatch at every later use of the poisoned binding is
    # exactly the cascade the sentinel exists to prevent. The diagnostic was
    # already emitted where the poison was born.
    if is_poison(target_type):
        target_type = None
    # a method call on a constructor-tracked host receiver (`store.get(k)`
    # where `store = Map.new()`): checked against the family surface, result
    # opaque (see _HOST_FAMILIES)
    if target_type in _HOST_FAMILIES:
        host_family_check(target_type, method, arg_types, filename, line)
        # A result-declared host verb (item 397: `insert_if_absent -> Bool`)
        # returns its declared type instead of the opaque `None`; every other
        # host verb's result stays on the G8 audit surface.
        return _HOST_RESULT_SIG.get(f"{target_type}.{method}")
    sig = _BUILTIN_SIG.get(method)
    if sig is None:
        return None
    if isinstance(sig, dict):
        # A method spelled for several receiver families (`to_int`: the Int32
        # widening AND the Str parse — docs/arithmetic.md and docs/stdlib-2.0.md
        # §Str.to_int). Select the row by the receiver head; a receiver that
        # matches no row is refused, listing the admitted families — the same
        # shape as the single-family error below, so the two paths read alike.
        thead, _ = parse_type(target_type)
        row = sig.get(thead)
        if row is None:
            if filename and target_type is not None:
                raise RevlError(
                    filename, line,
                    f"builtin `{method}` has no form for a "
                    f"`{render_type(target_type)}` receiver "
                    f"(its receiver families: {', '.join(sorted(sig))})",
                    code="T1", category="type-mismatch")
            return None
        family, params, ret = row
    else:
        family, params, ret = sig
    thead, targs = parse_type(target_type)
    if filename and target_type is not None:
        if family == "sized" and thead not in _SIZED_HEADS:
            raise RevlError(filename, line,
                            f"builtin `{method}` needs a Str/Bytes/List receiver, got `{render_type(target_type)}`")
        if family in ("List", "Str", "Int", "Int32", "Map", "Value") and thead != family:
            raise RevlError(filename, line,
                            f"builtin `{method}` needs a {family} receiver, got `{render_type(target_type)}`")
    # `@elem` is the element/value parameter: a List's single argument for
    # List receivers, a Map's second (value) argument for Map receivers.
    elem = None
    if thead == "List" and targs:
        elem = targs[0]
    elif thead == "Map" and len(targs) == 2:
        elem = targs[1]
    if elem == "Never":
        # Bottom-typed receiver: the empty literal `[]` / `Map.empty()`. Its
        # element type is the checker's INFERRED BOTTOM, not a constraint this
        # call has to satisfy: `Never` is one-way, so comparing a concrete
        # argument against it in the sweep below would refuse every honest
        # `[].push("s")`. The element type is the thing to LEARN here. Take it
        # from a concrete argument and carry it in the rebuilt receiver, so
        # `[].push("s")` types as `List[Str]` and is refused where `List[Int]`
        # is expected, and so the argument sweep then checks against the
        # LEARNED element instead of against the bottom. When no argument
        # offers a concrete type (holes, unknowns), nothing is learned and the
        # receiver stays bottom-typed.
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
            elem = learned
            target_type = format_type(
                thead, [elem] if thead == "List" else [targs[0], elem])
            targs = parse_type(target_type)[1]
    for spec, actual in zip(params, arg_types):
        expected = {"@elem": elem, "@member": elem if thead == "List" else ("Str" if thead == "Str" else None), "@self": target_type}.get(spec, spec)
        if filename and expected and actual \
                and not compatible(expected, actual, types):
            raise mismatch(filename, line, f"builtin `{method}` argument", expected, actual)
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


# Past this many digits the literal is QUOTED BY DESCRIPTION rather than by
# value. A diagnostic that pastes a five-thousand-digit number is unreadable,
# and building that string is itself a fault: CPython refuses `str(v)` past
# 4300 digits (issue #311), so the message that names the mistake becomes the
# `ValueError` the reader sees instead.
_INT_LITERAL_SHOWN_DIGITS = 40
_LOG10_2 = 0.30102999566398120  # digits per bit, for a count without str(v)


def int_literal_range_error(filename: str, line: int, *,
                            value: int | None = None,
                            digits: int | None = None) -> RevlError:
    """The one `Int`-outside-the-range diagnostic.

    Two callers, one message. The checker has the VALUE; the lexer, for a run
    of digits too long to convert at all, has only the COUNT (issue #311) —
    and a literal that long is out of range by the count alone, since i64's
    largest value has nineteen digits.
    """
    if digits is None:
        assert value is not None
        magnitude = abs(value)
        if magnitude < 10 ** _INT_LITERAL_SHOWN_DIGITS:
            shown = f"`{value}`"
        else:
            digits = int(magnitude.bit_length() * _LOG10_2) + 1
            shown = f"with about {digits} decimal digits"
    else:
        shown = f"with {digits} decimal digits"
    return RevlError(
        filename, line,
        f"Int literal {shown} is outside the 64-bit range",
        hint="`Int` is 64-bit two's complement "
             "([-9223372036854775808, 9223372036854775807]); a literal beyond the "
             "bound reads differently per tier, so it never reaches one "
             "(docs/arithmetic.md)",
    )


def _reject_int_literal_range(filename: str | None, line: int, v: int) -> None:
    """Refuse an `Int` literal outside the i64 range at compile time.

    Gated on `filename` like every definite checker refusal: the no-filename
    form is a pure type oracle and never raises.
    """
    if filename is None or _INT64_MIN <= v <= _INT64_MAX:
        return
    raise int_literal_range_error(filename, line, value=v)


def _reject_float_literal_range(filename: str | None, line: int, v: float) -> None:
    """Refuse a `Float` literal that folded to a non-finite value (issue #312).

    The lexer reads a decimal float with `float(...)`, so an exponent past the
    binary64 range (`1e999`) folds to IEEE `inf` — a value revl has no
    *spelling* for. Every emitter then prints the host's repr of that value as
    a bare word: py `inf`, rust `inff64`, java `infd`, go `float64(inf)` — an
    UNBOUND IDENTIFIER, so the program does not compile (rust/java/go) or dies
    at runtime with a `NameError` (py). TypeScript alone renders `Infinity` and
    runs, which makes the same source text mean six different things.

    That is the same argument as `_reject_int_literal_range`, one type over: a
    literal with no tier-independent meaning is refused here, where it is one
    diagnostic, instead of six behaviours. The bound is the *literal's*, not
    arithmetic's — `1e308 * 10.0` still overflows to `inf` at runtime, exactly
    as IEEE 754 says it should.
    """
    if filename is None or math.isfinite(v):
        return
    what = "infinite" if math.isinf(v) else "not a number"
    raise RevlError(
        filename, line,
        f"Float literal is {what}: it is outside the range of a 64-bit float",
        hint="`Float` is IEEE 754 binary64 (docs/arithmetic.md); a literal "
             "past its largest finite value has no spelling in revl and no "
             "single reading across tiers, so it never reaches one — write a "
             "finite literal",
    )


def _extend_arm_tenv(stmt, tenv: dict, types: dict, filename: str | None) -> None:
    """Extend a block-arm's tail scope with one of its statements.

    Only a `let`/`var` binding contributes a name the tail can read; the
    imperative statements (`while`, `if`, assignments) declare nothing at the
    block's own level, and their full check happens at lowering. A declared
    type is authoritative; otherwise the initialiser is inferred."""
    from .parser import LetStmt
    if isinstance(stmt, LetStmt):
        declared = getattr(stmt, "type", None)
        tenv[stmt.name] = (declared if declared is not None
                           else infer_ast(stmt.value, tenv, types, filename))


def infer_ast(expr, tenv: dict, types: dict, filename: str | None = None) -> str | None:
    """Best-effort type of a parser-AST expression. With `filename`, definite
    operator/branch/argument mismatches raise; without it, never raises."""
    from .parser import (
        ExprArrow, ExprBin, ExprBlockArm, ExprCall, ExprField, ExprHole,
        ExprIf, ExprIndex, ExprList, ExprLit, ExprMatch, ExprOptCall,
        ExprOptField, ExprRecord, ExprRecordUpdate, ExprUn, ExprVar, Interp,
        Lit, LIST_TRANSFORMS, desugar_list_transform,
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
            # the Float twin of the bound above: `1e999` folds to `inf`, which
            # every emitter prints as an unbound name (issue #312).
            _reject_float_literal_range(filename, line, v)
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
        # item 380 (the soundness hole): a bare reference to a top-level `fn`
        # is a first-class function value (docs/function-types.md — arrows and
        # fn names alike can be "checked, stored, passed and returned"), so it
        # has a type: the fn's function type `(params...) -> returns`. Before
        # this it inferred to `None` (unknown), and `None` short-circuits every
        # downstream compatibility check — so `return f` in a `-> Str` fn, or
        # `apply(wrong_sig_fn)`, type-checked as ANY type and silently
        # miscompiled. Typing it here is what lets the return/argument check
        # REFUSE the mismatch, while a return of a correctly function-typed
        # value (item 92/342) still passes because the types now agree. Generic
        # and unit-returning fns are left untyped (unchanged): a `_TPARAM`
        # marker must not leak into a rendered type, and a bare value use of
        # either is exotic — leaving them at `None` is a no-op, never a
        # regression.
        sig = (types.get(FNS_KEY) or {}).get(expr.name)
        if sig is not None and not sig.get("tparams") \
                and sig.get("returns") is not None:
            return format_type(FN_HEAD, list(sig["params"]) + [sig["returns"]])
        return None
    if isinstance(expr, ExprBin):
        lt = infer_ast(expr.left, tenv, types, filename)
        rt = infer_ast(expr.right, tenv, types, filename)
        return _binop_type(expr.op, lt, rt, filename, line, types)
    if isinstance(expr, ExprUn):
        t = infer_ast(expr.operand, tenv, types, filename)
        if expr.op == "!":
            if filename and t and t != "Bool":
                raise mismatch(filename, line, "operand of `!`", "Bool", t)
            return "Bool"
        if expr.op == "~":
            # Bitwise complement is Int32-only (item 366), matching the binary
            # bitwise operators; it does not trap. `~x == -x - 1` within the
            # 32-bit range.
            if filename and t and parse_type(t)[0] != "Int32":
                is_int = parse_type(t)[0] == "Int"
                raise RevlError(
                    filename, line,
                    f"`~` requires an `Int32` operand, got `{render_type(t)}`",
                    hint=("bitwise `~` is Int32-only — narrow with `.to_int32()` "
                          "(docs/arithmetic.md)") if is_int else
                         "bitwise `~` is Int32-only (docs/arithmetic.md)",
                    code="T1", category="type-mismatch")
            return "Int32"
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
        # item 380: a field read off a value whose static type is `Any`/`Value`
        # (the erased-dynamic types — a parsed JSON body, `json_parse`'s result)
        # is the 279/299 silent-divergence class: py raises `KeyError` on an
        # absent key, ts yields `undefined`, and neither is a defensible total
        # answer for a field the author declared present. REFUSE it here so the
        # divergence is a compile error, not a runtime surprise, and point the
        # author at the two designed surfaces: cast to a record (an `Opt[T]`
        # field then reads TOTAL — `let e: E = v; e.kind ?? default`), or walk
        # the erased value with the total shape accessors (stdlib/value.rvl:
        # `value_is_object` / `value_opt` / `value_field_or`).
        if filename and thead in ("Any", "Value"):
            raise RevlError(
                filename, line,
                f"field read `.{expr.name}` on a value of type "
                f"`{render_type(target)}` — an erased value has no known fields",
                hint=("bind it to a record type first "
                      f"(`let e: SomeRecord = …; e.{expr.name}` — an `Opt[T]` "
                      "field then reads back the empty Opt on absence), or walk "
                      "it with stdlib/value.rvl (`value_is_object(v)`, "
                      f"`value_opt(v, \"{expr.name}\")`, `value_field_or`)"),
                code="T1", category="type-mismatch")
        struct = structural_fields(target)
        if struct is not None:
            # a read through an anonymous record binding is checked too (item 71)
            if filename and expr.name not in struct:
                raise RevlError(filename, line,
                                f"`{render_type(target)}` has no field `{expr.name}` "
                                f"(fields: {', '.join(sorted(struct)) or 'none'})")
            return struct.get(expr.name)
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
            result = builtin_check(expr.method, inner, args, filename, line, types)
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
        if filename and a and b and join(a, b, types) is None:
            raise RevlError(filename, line,
                            f"ternary branches disagree: `{render_type(a)}` vs `{render_type(b)}`")
        return join(a, b, types)
    if isinstance(expr, ExprList):
        item = None
        for e in expr.items:
            t = infer_ast(e, tenv, types, filename)
            item = t if item is None else join(item, t, types)
        return f"List[{item}]" if item else "List[Never]"
    if isinstance(expr, ExprRecord):
        # An anonymous literal infers a STRUCTURAL record type from its fields
        # (item 71). At a declared boundary `check_ast` still names the literal
        # against the expected nominal record directly; this type is what lets
        # an update `{ a | f = e }` on a `let`-bound literal be field-checked
        # instead of escaping. A field whose own type is unknown degrades to
        # `Any` in the shape, keeping the record's other fields checkable.
        shape: dict[str, str | None] = {}
        for name, value in expr.fields:
            shape[name] = infer_ast(value, tenv, types, filename)
        return format_structural(shape)
    if isinstance(expr, ExprRecordUpdate):
        # docs/records.md §3: `base` must carry a record type; every updated
        # field must exist there and its replacement must match the declared
        # field type. The result's type is `base`'s type.
        base_t = infer_ast(expr.base, tenv, types, filename)
        if base_t is None:
            for _, value in expr.updates:
                infer_ast(value, tenv, types, filename)
            return None
        # A structural base (an anonymous `let`-bound literal, item 71) is
        # field-checked against its own shape — the same three §3 rules, named
        # against the structural type. This is the path that used to escape.
        struct = structural_fields(base_t)
        spec = types.get(base_t)
        if struct is not None:
            declared = struct
        elif spec is not None and spec.get("kind") == "record":
            declared = spec.get("fields", {})
        else:
            declared = None
        # Without a filename this is the non-raising oracle: report the type
        # when it is soundly known, otherwise bow out.
        if filename is None:
            if declared is not None:
                for name, value in expr.updates:
                    if name in declared:
                        infer_ast(value, tenv, types, filename)
                    else:
                        return None
                return base_t
            for _, value in expr.updates:
                infer_ast(value, tenv, types, filename)
            return None
        if declared is not None and struct is not None:
            for name, value in expr.updates:
                if name not in declared:
                    raise RevlError(
                        filename, line,
                        f"record update names `{name}`, which is not a field of "
                        f"`{render_type(base_t)}`",
                        hint=f"fields: {', '.join(f'`{f}`' for f in sorted(declared))}",
                    )
                check_ast(value, declared.get(name), tenv, types, filename,
                          f"update of field `{name}`")
            return base_t
        if spec is None or spec.get("kind") != "record":
            raise RevlError(
                filename, line,
                f"record update requires a record type, "
                f"`{render_type(base_t) or 'unknown'}` is not one",
                hint="`{r | f = e}` copies `r` with field `f` replaced — it only "
                     "applies to a named record type (docs/records.md §2)",
            )
        declared = spec.get("fields", {})
        for name, value in expr.updates:
            if name not in declared:
                raise RevlError(
                    filename, line,
                    f"record update names `{name}`, which is not a field of "
                    f"`{render_type(base_t)}`",
                    hint=f"fields: {', '.join(f'`{f}`' for f in sorted(declared))}",
                )
            check_ast(value, declared.get(name), tenv, types, filename,
                      f"update of field `{name}`")
        return base_t
    if isinstance(expr, ExprBlockArm):
        inner = dict(tenv)
        for stmt in expr.stmts:
            _extend_arm_tenv(stmt, inner, types, filename)
        return infer_ast(expr.tail, inner, types, filename)
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
                        not compatible(case["payload"], arg_types[0], types):
                    raise mismatch(filename, line, f"`{name}(...)` payload",
                                   case["payload"], arg_types[0])
                # A case call with NO argument. Zero args at a payload-carrying
                # case is deliberately ACCEPTED here — the payload check above
                # skips it on `arg_types and arg_types[0]`, and
                # tests/test_selfhost_checker.py pins `Circle()` as accepted
                # alongside the widening and unknown-argument cases — so the
                # only question is what it infers. For a user ADT that is
                # `case["adt"]` below and there is nothing to read; for the
                # three builtin cases it used to be `arg_types[0]`, which on an
                # empty list threw a bare `IndexError` out of the checker.
                # `fn k() -> Opt[Int] { return Some() }` was enough to do it.
                #
                # `Any` is the answer the two lines already reach for when an
                # argument's type cannot be worked out, and a missing argument
                # is that same absence of information, so it needs no second
                # rule — just the guard the list read never had. Found by
                # tools/fuzz_frontend.py.
                first = arg_types[0] if arg_types else None
                if name == "Some":
                    return f"Opt[{first or 'Any'}]"
                if name == "Ok":
                    return f"Result[{first or 'Any'}, Any]"
                if name == "Err":
                    return f"Result[Any, {first or 'Any'}]"
                return case["adt"]
            sig = (types.get(FNS_KEY) or {}).get(name)
            if sig is not None:
                params = sig["params"]
                # roadmap item 187: a call may omit trailing defaulted
                # parameters. `required` is the count of leading parameters
                # without a default; anything from `required` to `len(params)`
                # arguments is well-formed. Signatures with no defaults keep
                # `required == len(params)`, so the check is unchanged for them.
                required = sig.get("required", len(params))
                if filename:
                    if not (required <= len(expr.args) <= len(params)):
                        rendered = ", ".join(render_type(p) or "_"
                                             for p in params) or "no arguments"
                        want = (f"{len(params)}" if required == len(params)
                                else f"{required} to {len(params)}")
                        raise RevlError(
                            filename, line,
                            f"`{name}` takes {want} argument(s), "
                            f"{len(expr.args)} given",
                            hint=f"`{name}` is declared `({rendered})`"
                                 + ("" if required == len(params) else
                                    " — trailing parameters with a default may be "
                                    "omitted, but revl calls are positional so only "
                                    "trailing arguments can be dropped"),
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
                            if p and a and not compatible(p, a, types):
                                raise mismatch(filename, line,
                                               f"argument {i + 1} of `{name}(...)`", p, a)
                    return sig["returns"]
                # generic: instantiate the signature against this call's
                # arguments rather than letting every `T` position pass
                subst: dict = {}
                for i, (p, a) in enumerate(zip(params, arg_types)):
                    if unify(p, a, subst, types):
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
            # item 383: receiver-first list transforms (`xs.map(f)` etc.)
            # desugar to their generic free function so the existing
            # generic-call + arrow-argument inference types them; a builtin
            # sig cannot express `map`'s result `List[<f's return>]`. Skipped
            # for a host-family receiver — that stays on the host stub surface.
            if (expr.callee.name in LIST_TRANSFORMS
                    and target_t not in _HOST_FAMILIES):
                return infer_ast(desugar_list_transform(expr), tenv, types,
                                 filename)
            return builtin_check(expr.callee.name, target_t, arg_types, filename,
                                 line, types)
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
            result = t if result is None else join(result, t, types)
        return result
    if isinstance(expr, ExprArrow):
        # item 75(a) §3.1 — an arrow in *inference* position has no expected
        # type to read its parameters off, so each parameter is its written
        # annotation or ⊥ (unknown), and the result is the written return
        # annotation or, when the body cannot depend on a ⊥ parameter (§3.2),
        # what the body infers to.
        #
        # An arrow ALWAYS types, as a function type with every ⊥ rendered
        # `Any`. Returning `None` here (which is what a single bare parameter
        # used to do) threw away the arity too, and arity is purely syntactic
        # and never in doubt — that is what let `let f = (x) => "s"` pass its
        # `Str` into an `Int` position, and `f(1, 2, 3)` compile, in silence.
        #
        # The body is typed either way: it is an ordinary expression over the
        # enclosing scope, and skipping it let every check above leak.
        # Parameters shadow into the unknown; free variables keep their types.
        refuse_self_declared_async(expr, filename)
        if getattr(expr, "param_types", None) is not None:
            # A checking position already resolved this node — a `let`, a
            # `return` or a call argument is checked and then *inferred*, the
            # same object twice. The position wins over the annotation (§3.1)
            # and its result is already recorded, so re-deriving from the
            # written annotations alone here would throw it away again.
            #
            # `resolved_type` and not a rebuild from `param_types`/`returns`:
            # those two are the IR's spelling, with the implicit-type-parameter
            # marker stripped, and this value goes back into `unify` at the
            # enclosing call site. An unsolved `?B` rebuilt as `B` is a
            # concrete opaque nominal, and unification binds it.
            return expr.resolved_type
        annotations = arrow_annotations(expr)
        inner = dict(tenv)
        for param, ptype in zip(expr.params, annotations):
            if ptype:
                inner[param] = ptype
            else:
                inner.pop(param, None)
        independent = not arrow_depends_on_unknown_param(expr, annotations)
        written_return = getattr(expr, "written_returns", None)
        if written_return and independent and filename:
            # the annotation is a claim about the body, so check it — but only
            # where the body cannot mention a ⊥ parameter. Under a ⊥ parameter
            # the body's type is `Any`-infected (or half-solved, §3.2), so the
            # check could only pass vacuously or misfire.
            check_ast(expr.body, written_return, inner, types, filename,
                      "the body of this arrow (from its return annotation)")
            returns = written_return
        else:
            # the body is walked either way — it is an ordinary expression over
            # the enclosing scope, and skipping it let every check above leak
            body_t = infer_ast(expr.body, inner, types, filename)
            # §3.2: inferring a result from a body typed under unknown
            # parameters is not sound — `[x]` infers `List[Never]` and
            # `{ a: x }` infers `{a: Any}`, half-solved types that look known
            # and are not. So the result stays ⊥ whenever a ⊥ parameter occurs
            # free in the body. `(x) => x + 1` keeps behaving exactly as it
            # does today; `(x) => "s"` does not.
            returns = written_return or (body_t if independent else None)
        return _resolve_arrow(expr, list(annotations), returns)
    return None


def arrow_annotations(expr) -> list:
    """The author's `(v: Int) => ...` parameter annotations, one per
    parameter (None where the parameter was written bare)."""
    written = list(getattr(expr, "written_param_types", None) or [])
    written += [None] * (len(expr.params) - len(written))
    return written[:len(expr.params)]


def arrow_depends_on_unknown_param(expr, resolved: list) -> bool:
    """Does a ⊥ (still unknown) parameter occur free in the arrow's body?

    item 75(a) §3.2, the bottom-parameter-independence rule. Syntactic,
    decidable and deliberately conservative: it refuses to name a result that
    a parameter of unknown type could have shaped, and it under-approximates
    (`(x) => str_of(x)` has a result that provably does not depend on `x`, and
    this rule still declines it — widening needs a dependency analysis)."""
    unknown = {name for name, t in zip(expr.params, resolved) if not t}
    if not unknown:
        return False
    return bool(unknown & _free_names(expr.body, frozenset()))


def _free_names(expr, bound: frozenset) -> set:
    """Every variable name read in `expr` and not shadowed by an inner binder.

    Recursion is over dataclass *fields* rather than a hand-written list of
    child attributes, so an expression node this module has never heard of is
    still descended into. Missing an occurrence would let §3.2 name a result
    it must not name, so the walk errs towards visiting too much."""
    from .parser import ExprArrow, ExprBlockArm, ExprMatch, ExprVar, LetStmt

    if expr is None or isinstance(expr, (str, int, float, bool)):
        return set()
    if isinstance(expr, (list, tuple)):
        found: set = set()
        for item in expr:
            found |= _free_names(item, bound)
        return found
    if isinstance(expr, ExprVar):
        return set() if expr.name in bound else {expr.name}
    if isinstance(expr, ExprArrow):
        return _free_names(expr.body, bound | frozenset(expr.params))
    if isinstance(expr, ExprMatch):
        found = _free_names(expr.scrutinee, bound)
        for _, bind, body in expr.arms:
            inner = bound | (frozenset([bind]) if bind is not None else frozenset())
            found |= _free_names(body, inner)
        return found
    if isinstance(expr, ExprBlockArm):
        # Only a `let` contributes a name the rest of the block can read (the
        # imperative statements declare nothing at the block's own level), so
        # only a `let` shadows here — treating an assignment's target as a
        # binder would *hide* an occurrence, which is the unsafe direction.
        # Every statement is walked generically for the expressions it holds.
        inner = set(bound)
        found = set()
        for stmt in expr.stmts:
            found |= _free_names(stmt, frozenset(inner))
            if isinstance(stmt, LetStmt) and isinstance(stmt.name, str):
                inner.add(stmt.name)
        return found | _free_names(expr.tail, frozenset(inner))
    if not dataclasses.is_dataclass(expr):
        return set()
    found = set()
    for f in dataclasses.fields(expr):
        found |= _free_names(getattr(expr, f.name, None), bound)
    return found


def _mentions_async(type_: str | None) -> bool:
    """Does `type_` name `Async[...]` anywhere inside it?

    Well-formedness confines `Async` to a function type's return, so this is
    the whole of "at the top level or as the return of a function type it
    names" (item 75(a) rule C1) with no case analysis."""
    if not type_:
        return False
    head, args = parse_type(type_)
    if head == "Async":
        return True
    return any(_mentions_async(a) for a in args)


def refuse_self_declared_async(expr, filename: str | None) -> None:
    """Rule C1 — colour is positional; an arrow may not declare its own.

    `"async": true` on a lowered arrow is not a label, it is a *certificate*
    that some declaration promised to await the arrow: `_refuse_leaky_pure_arrow`
    skips a flagged arrow, and callee collection with `stop_async_arrows` stops
    descending at one. A written `Async[...]` return annotation would forge that
    certificate with no consumer behind it, laundering arbitrary nested async
    reach out of the enclosing scope's reach set. So the annotation is refused
    at the source, and rule C2 (asserted at the lowering site) keeps
    `written_returns` out of the flag."""
    written = getattr(expr, "written_returns", None)
    if not filename or not _mentions_async(written):
        return
    raise RevlError(
        filename, getattr(expr, "line", 0) or 0,
        "an arrow may not declare its own async colour",
        hint="an arrow is coloured by the position it flows into, e.g. "
             "`let g: (Int) -> Async[Str] = ...` or a parameter declared "
             "`(Int) -> Async[Str]`, because that declaration is what makes "
             "the consumer await it (docs/function-types.md, "
             "docs/design/async-function-values.md)",
        code="A1", category="async-propagation",
    )


def _resolve_arrow(expr, param_types: list, returns: str | None) -> str:
    """Record an arrow's now-known parameter/return types on the AST node and
    return its function type.

    Lowering reads these back off the node (`lower.py::_lower_pure_expr`), so
    the types the checker recovered are exactly the ones that reach the IR and
    the backends; an unknown component stays `None` on the node — which is what
    lets lowering tell a complete signature from a partial one (item 75(a) §4)
    — and renders `Any` in the function type the checker hands back."""
    # `render_type` strips the implicit-type-parameter marker: the IR carries
    # the author's spelling, and the marker never leaves the checker (see
    # "type parameters" above). Without it, checking an arrow against an
    # uninstantiated `(T) -> T` would write `?T` into the IR.
    expr.param_types = [render_type(p) for p in param_types]
    expr.returns = render_type(returns)
    # …and the checker's own view of the same resolution keeps the marker. An
    # arrow checked against a generic position resolves to `?B`, which is a
    # *wildcard*; it has to stay one, because a `return`/`let` is checked and
    # then inferred — the same node twice — and the second pass hands this type
    # to `unify`. Reconstructing it from the stripped fields instead offered
    # unification the opaque nominal `B`, which it dutifully bound `?B := B`;
    # that is how `map_([1, 2], n => n + 1)` came to check its arrow body
    # against `B` and refuse an `Int`.
    expr.resolved_type = format_type(
        FN_HEAD, [p or "Any" for p in param_types] + [returns or "Any"])
    return expr.resolved_type


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
        phead, pargs = parse_type(param)
        if isinstance(arg, ExprArrow) and phead == FN_HEAD:
            check_ast(arg, param, tenv, types, filename,
                      f"argument {i + 1} of {what}")
        elif not isinstance(arg, ExprArrow) and phead == FN_HEAD and pargs \
                and parse_type(pargs[-1])[0] == "Async":
            # item 92 v1: only an arrow may flow into an async parameter — a
            # bare value would need an `as_async` coercion wrapper the blocking
            # backends do not yet erase (a filed follow-up).
            raise RevlError(
                filename, getattr(arg, "line", 0) or 0,
                f"argument {i + 1} of {what} is declared "
                f"`{render_type(param)}` (async), but only an arrow may be "
                "passed into an async parameter in v1",
                hint="wrap it in an arrow, e.g. `x => f(x)`, so the emitter can "
                     "place the async boundary "
                     "(docs/design/async-function-values.md)",
                code="A1", category="async-propagation",
            )


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
            if p and a and not compatible(p, a, types):
                raise mismatch(filename, line, f"argument {i + 1} of {what}", p, a)
    # Elimination: calling an async-typed value yields the *unwrapped* `T` — the
    # tier-level await is implicit (item 92 §2). No expression ever has type
    # `Async[T]`; admission that this call sits in an async context is lower's
    # job, not the checker's.
    rhead, rargs = parse_type(returns)
    if rhead == "Async":
        return rargs[0] if rargs else None
    if returns == "Any":
        # item 75(a) §3.2/§6: `Any` in a function type's return is the *absence*
        # of a claim — it is how a ⊥ result (an arrow whose body depends on an
        # un-annotated parameter) renders. Handing `Any` back as the call's
        # inferred type would make the ordinary operand checks (`x + f(1)`) and
        # the erased-value checks refuse a call that was silent before this
        # item, which is exactly the migration class §6 rules out. Unknown, not
        # `Any`; arity and the argument positions were already checked above.
        return None
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
    refuse_self_declared_async(expr, filename)
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
        if written and not compatible(written, want, types):
            raise mismatch(filename, line,
                           f"parameter `{name}` of this arrow (from {where})",
                           want, written)
        resolved.append(written or want)
        if resolved[-1]:
            inner[name] = resolved[-1]
        else:
            inner.pop(name, None)
    # The body is checked against the *unwrapped* return: the async color is a
    # tier property, not part of the value's shape (item 92 §2). `_resolve_arrow`
    # still writes the async-headed `want_return` back onto the node, so the
    # color reaches the IR through the existing pipe.
    body_return = want_return
    rhead, rargs = parse_type(want_return)
    if rhead == "Async":
        body_return = rargs[0] if rargs else None
    # item 75(a) §3.1: a written return annotation is checked against what the
    # position asks for (covariantly), and against the *unwrapped* return when
    # the position is async — rule C3, an annotation on an arrow that will be
    # coerced into an `Async[T]` slot names the sync inner type `T`.
    written_return = getattr(expr, "written_returns", None)
    if written_return and body_return and not compatible(body_return, written_return, types):
        raise mismatch(filename, line,
                       f"the return type of this arrow (from {where})",
                       body_return, written_return)
    check_ast(expr.body, body_return, inner, types, filename,
              f"the body of this arrow (from {where})")
    # The *position's* return reaches the node, not the annotation's: it is the
    # one that can carry an async colour, and rule C1 has already refused a
    # written `Async[...]`, so this is where colour comes from and the only
    # place it can. Where the position says nothing more than the annotation
    # does, the annotation is the more precise of the two.
    _resolve_arrow(expr, resolved,
                   written_return if (written_return and rhead != "Async"
                                      and _is_wildcard(want_return))
                   else want_return)


def check_ast(expr, expected: str | None, tenv: dict, types: dict,
              filename: str, where: str) -> None:
    """Bidirectional check of a parser-AST expression against `expected`."""
    from .parser import ExprArrow, ExprBlockArm, ExprIf, ExprList, ExprMatch, \
        ExprRecord, ExprRecordUpdate

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
    if isinstance(expr, ExprRecordUpdate):
        # docs/records.md §3: the result is `base`'s type, so the expectation
        # is checked against that; the field updates are checked per-field.
        base_t = infer_ast(expr.base, tenv, types, filename)
        struct = structural_fields(base_t)
        spec = types.get(base_t or "")
        if struct is not None:
            declared = struct  # anonymous base: field-check against its shape (item 71)
        elif spec is not None and spec.get("kind") == "record":
            declared = spec.get("fields", {})
        else:
            declared = None
        if declared is not None:
            for name, value in expr.updates:
                if struct is not None and name not in declared:
                    raise RevlError(
                        filename, line,
                        f"record update names `{name}`, which is not a field of "
                        f"`{render_type(base_t)}`",
                        hint=f"fields: {', '.join(f'`{f}`' for f in sorted(declared))}",
                    )
                check_ast(value, declared.get(name), tenv, types, filename,
                          f"update of field `{name}` of `{render_type(base_t)}`")
        if base_t and not compatible(expected, base_t, types):
            raise mismatch(filename, line, where, expected,
                           render_type(base_t) or base_t)
        return
    if isinstance(expr, ExprBlockArm):
        # A `let`/`var` in the arm block extends the arm's scope for the tail;
        # the imperative statements (`while`, `if`, assignments) add no
        # tail-visible name and are validated in full at lowering, where the
        # ordinary fn-body machinery runs. The tail is checked against the
        # expectation like any other arm body.
        inner = dict(tenv)
        for stmt in expr.stmts:
            _extend_arm_tenv(stmt, inner, types, filename)
        check_ast(expr.tail, expected, inner, types, filename, where)
        return
    actual = infer_ast(expr, tenv, types, filename)
    struct = structural_fields(actual)
    if struct is not None and spec is not None and spec.get("kind") == "record":
        # An anonymous record literal flowing into a declared nominal record is
        # the boundary where the structural type unifies with the nominal
        # (item 71): the field set must match and each field type must be
        # compatible (the `List[Never]` bottom rule is the per-field recursion).
        declared = spec.get("fields", {})
        missing = sorted(set(declared) - set(struct))
        extra = sorted(set(struct) - set(declared))
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing {', '.join(f'`{m}`' for m in missing)}")
            if extra:
                parts.append(f"unknown {', '.join(f'`{e}`' for e in extra)}")
            raise RevlError(filename, line,
                            f"{where} expects `{expected}`, but the record has "
                            f"{'; '.join(parts)}")
        for name, ftype in struct.items():
            if ftype and not compatible(declared.get(name), ftype, types):
                raise mismatch(filename, line,
                               f"field `{name}` of `{expected}`",
                               declared.get(name), ftype)
        return
    if actual and not compatible(expected, actual, types):
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
            _reject_float_literal_range(filename, line, v)
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
    if kind == "instance-get":
        # a provision read off a spawn handle (`s.<key>`) yields the service
        # the target component declares at that key (docs/design-v2-instances.md).
        # It is a host-frontier value — resolved through the handle's own local
        # realm at runtime — so the type is advisory, like a spawn handle's own,
        # and is never structurally compared. The key was validated to be a
        # provision at lower time, so `service` is always present.
        return node.get("service")
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
            if unify(p, at, subst, types):
                continue
            if filename:
                raise mismatch(filename, line, f"argument {i + 1} of `{name}(...)`",
                               substitute(p, subst), at)
            return None
        return substitute(sig["returns"], subst)
    if kind == "builtin":
        target_t = infer_ir(node.get("target"), tenv, types, services, filename, line)
        args = [infer_ir(a, tenv, types, services, filename, line) for a in node.get("args") or []]
        return builtin_check(node.get("method"), target_t, args, filename, line,
                             types)
    if kind == "maplit":
        # `Map.empty()` (docs/stdlib-2.0.md §Map): bottom-typed empty value.
        return "Map[Str, Never]"
    if kind == "list":
        # a list literal is pinned by its elements (mirrors `infer_ast`'s
        # ExprList), so `[1, 2].length()` types as `List[Int]`, not None
        item = None
        for e in node.get("items") or []:
            t = infer_ir(e, tenv, types, services, filename, line)
            item = t if item is None else join(item, t, types)
        return f"List[{item}]" if item else "List[Never]"
    if kind == "record":
        # item 405: an anonymous record literal infers a STRUCTURAL record type
        # from its fields (mirrors `infer_ast`'s `ExprRecord`, item 71). Without
        # this, the lowered path left an anonymous binding untyped (None), so a
        # later `a.missing` read on it was never field-checked inside a provide
        # method — the residual of the 404 coverage class. Naming the structural
        # type here is what lets the `field` case above refuse the bad read; at a
        # declared boundary the structural type still meets the nominal record
        # field-wise (`compatible`), so a valid record-literal return stays clean.
        shape: dict[str, str | None] = {}
        for name, value in node.get("fields") or []:
            shape[name] = infer_ir(value, tenv, types, services, filename, line)
        return format_structural(shape)
    if kind == "record_update":
        # F4: `infer_ir` had NO case for a record update, so stratum 3 accepted
        # `{ r | nope = 1 }` and `{ r | id = "not an int" }` that a `fn` body
        # (stratum 1, `infer_ast`'s `ExprRecordUpdate`) refuses by name and by
        # type. Mirror the same three docs/records.md §3 rules — the field must
        # exist, its replacement must match, the result is the base's type — on
        # the lowered dialect. The base's shape is resolved either structurally
        # (an anonymous literal, item 71) or nominally through `types`.
        base_t = infer_ir(node.get("base"), tenv, types, services, filename, line)
        struct = structural_fields(base_t)
        declared = struct if struct is not None \
            else nominal_record_fields(base_t, types)
        for name, value in node.get("updates") or []:
            vt = infer_ir(value, tenv, types, services, filename, line)
            if declared is None:
                # a host-frontier or otherwise unrecoverable base: stay silent,
                # exactly as `infer_ast` does when the base type is unknown
                continue
            if filename and name not in declared:
                raise RevlError(
                    filename, line,
                    f"record update names `{name}`, which is not a field of "
                    f"`{render_type(base_t)}`",
                    hint=f"fields: {', '.join(f'`{f}`' for f in sorted(declared))}",
                )
            ftype = declared.get(name)
            if filename and vt and ftype and not compatible(ftype, vt, types):
                raise mismatch(filename, line, f"update of field `{name}`",
                               ftype, vt)
        return base_t
    if kind == "adt":
        # F4: ADT-case construction had no `infer_ir` case, so a provide body
        # accepted `P("str")` where `type P = P(Int)` — refused in a `fn` body
        # by `infer_ast`'s case-table payload check. Same diagnostic here.
        tname = node.get("type")
        case_name = node.get("case")
        args = node.get("args") or []
        spec = types.get(tname or "")
        generic = isinstance(spec, dict) and bool(spec.get("params"))
        payload = None
        if isinstance(spec, dict) and spec.get("kind") == "variant" and not generic:
            for case in spec.get("cases") or []:
                if case.get("name") == case_name:
                    payload = case.get("payload")
                    break
        for i, arg in enumerate(args):
            at = infer_ir(arg, tenv, types, services, filename, line)
            if i == 0 and filename and payload and at \
                    and not compatible(payload, at, types):
                raise mismatch(filename, line, f"`{case_name}(...)` payload",
                               payload, at)
        # A GENERIC ADT's construction names the bare head (`Box`), not the
        # instantiation (`Box[Int]`) — the case table carries the declaration's
        # own spelling. Reporting the bare head would make an honest
        # `-> Box[Int] = B(1)` a head-arity mismatch, so a generic ADT stays
        # unknown here (stratum 1 has its own separate gap on the same shape,
        # filed; this is not the place to invent an answer it does not give).
        return None if generic else tname
    if kind == "match":
        # F4: `infer_ir` had no `match` case, so every arm body escaped the
        # raising sweep and the eliminator's own type was unknown. Each arm is
        # inferred with its payload binding in scope (the lowering writes the
        # bound name and `payload_type` onto the arm) and the arm types join,
        # mirroring `infer_ast`'s `ExprMatch`.
        result = None
        first = True
        for arm in node.get("arms") or []:
            inner = dict(tenv)
            bind = arm.get("bind")
            if bind is not None:
                payload_type = arm.get("payload_type")
                if payload_type is not None:
                    inner[bind] = payload_type
                else:
                    inner.pop(bind, None)
            t = infer_ir(arm.get("body"), inner, types, services, filename, line)
            result, first = (t if first else join(result, t, types)), False
        return result
    if kind == "arrow":
        # An arrow whose signature the lowering proved complete carries it in
        # the IR (`param_types` + `returns`); that is exactly the case where a
        # function type is known, so name it. A partially annotated arrow keeps
        # both keys absent and stays untyped — stratum 3 still does not CHECK an
        # arrow body (docs/function-types.md §limits, item 75(a) §5.3).
        param_types = node.get("param_types")
        returns = node.get("returns")
        if param_types is not None and returns:
            return format_type(FN_HEAD,
                               [p or "Any" for p in param_types] + [returns])
        return None

    if kind == "bin":
        lt = infer_ir(node.get("left"), tenv, types, services, filename, line)
        rt = infer_ir(node.get("right"), tenv, types, services, filename, line)
        return _binop_type(node.get("op"), lt, rt, filename, line, types)
    if kind == "un":
        # item 404: bring the lowered-node unary checks to parity with
        # `infer_ast`'s `ExprUn` (stratum 1). Before this, `!` returned `Bool`
        # unconditionally and `~`/`-` returned the operand type unchecked, so a
        # provide-method (stratum 3) accepted `~n` on a non-`Int32` or `-s` on a
        # non-numeric that a `fn`/`test` body refuses — the item-392 class.
        op = node.get("op")
        t = infer_ir(node.get("operand"), tenv, types, services, filename, line)
        if op == "!":
            if filename and t and t != "Bool":
                raise mismatch(filename, line, "operand of `!`", "Bool", t)
            return "Bool"
        if op == "~":
            # Bitwise complement is Int32-only (item 366), matching the binary
            # bitwise operators; it does not trap. `~x == -x - 1` in 32-bit range.
            if filename and t and parse_type(t)[0] != "Int32":
                is_int = parse_type(t)[0] == "Int"
                raise RevlError(
                    filename, line,
                    f"`~` requires an `Int32` operand, got `{render_type(t)}`",
                    hint=("bitwise `~` is Int32-only — narrow with `.to_int32()` "
                          "(docs/arithmetic.md)") if is_int else
                         "bitwise `~` is Int32-only (docs/arithmetic.md)",
                    code="T1", category="type-mismatch")
            return "Int32"
        if filename and t and t not in _NUMERIC:
            raise mismatch(filename, line, "operand of unary `-`", "Int", t)
        return t
    if kind == "len":
        return "Int"
    if kind == "field":
        target = infer_ir(node.get("target"), tenv, types, services, filename, line)
        thead, targs = parse_type(target)
        name = node.get("name")
        if filename and thead == "Opt":
            raise opt_escape_error(filename, line,
                                   f"field access `.{name}`", target,
                                   targs[0] if targs else None,
                                   alt=f"?.{name}")
        # item 392: the provide-method twin of the item 380(2) refusal in
        # `infer_ast`. A field read off a value whose static type is
        # `Any`/`Value` (the erased-dynamic types — a `json_parse` result) is
        # the 279/299 silent-divergence class: py raises `KeyError` on an absent
        # key, ts yields `undefined`, and neither is a defensible total answer.
        # `infer_ast` (stratum 1 — fn/test/module-fn bodies) already refuses it;
        # component-body typing runs through this lowered path (stratum 3), which
        # bypassed the check, so the SAME expression compiled clean inside a
        # `provide` method body — the same context-scoping gap as the earlier
        # `.length`-in-provide-method bug. Apply the identical refusal here so the
        # divergence is a compile error on every tier, wherever the read sits.
        if filename and thead in ("Any", "Value"):
            raise RevlError(
                filename, line,
                f"field read `.{name}` on a value of type "
                f"`{render_type(target)}` — an erased value has no known fields",
                hint=("bind it to a record type first "
                      f"(`let e: SomeRecord = …; e.{name}` — an `Opt[T]` "
                      "field then reads back the empty Opt on absence), or walk "
                      "it with stdlib/value.rvl (`value_is_object(v)`, "
                      f"`value_opt(v, \"{name}\")`, `value_field_or`)"),
                code="T1", category="type-mismatch")
        # item 405: a read through an anonymous / structural record binding is
        # field-checked in a `fn`/`test` body (`infer_ast`, item 71); apply the
        # same refusal here so a provide-method body (stratum 3) no longer
        # accepts `a.missing` on a `{h: Str}` binding. Mirrors `infer_ast`.
        struct = structural_fields(target)
        if struct is not None:
            if filename and name not in struct:
                raise RevlError(filename, line,
                                f"`{render_type(target)}` has no field `{name}` "
                                f"(fields: {', '.join(sorted(struct)) or 'none'})")
            return struct.get(name)
        spec = types.get(target or "")
        if spec is not None and spec.get("kind") == "record":
            fields = spec.get("fields", {})
            # item 404: a read of a field a known record does not declare is
            # refused in a `fn`/`test` body (`infer_ast`); apply the same
            # refusal here so a provide-method body (stratum 3) no longer
            # accepts `p.missing` on a record `p`.
            if filename and name not in fields:
                raise RevlError(filename, line,
                                f"`{render_type(target)}` has no field `{name}` "
                                f"(fields: {', '.join(sorted(fields)) or 'none'})")
            return fields.get(name)
        return None
    if kind in ("optfield", "optcall"):
        # item 405: the provide-method (stratum 3) twin of `infer_ast`'s
        # `ExprOptField`/`ExprOptCall`. `a?.b` short-circuits on absence, so it
        # REQUIRES an optional on the left and always yields an optional on the
        # right; on a non-optional it is dead syntax the strict tiers cannot
        # render (Rust/Java have no `?.` on a plain value). `infer_ir` had no
        # node case, so `?.` on a non-`Opt` inferred to None and the refusal
        # never fired inside a provide method — the item-392/404 coverage gap.
        # Apply `infer_ast`'s refusals (same diagnostics) here.
        target = infer_ir(node.get("target"), tenv, types, services, filename, line)
        thead, targs = parse_type(target)
        member = node.get("name") if kind == "optfield" else node.get("method")
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
        if kind == "optcall":
            args = [infer_ir(a, tenv, types, services, filename, line)
                    for a in node.get("args") or []]
            result = builtin_check(node.get("method"), inner, args, filename,
                                   line, types)
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
    if kind == "index":
        target = infer_ir(node.get("target"), tenv, types, services, filename, line)
        it = infer_ir(node.get("index"), tenv, types, services, filename, line)
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
        # item 404: a non-`Int` index is refused in a `fn`/`test` body
        # (`infer_ast`); apply the same refusal here so a provide-method body
        # (stratum 3) no longer accepts `xs[s]` where a bare `fn` refuses it.
        if filename and it and thead in ("List", "Str") and it != "Int":
            raise mismatch(filename, line, "index", "Int", it)
        if thead == "List":
            return targs[0] if targs else None
        if thead == "Str":
            return "Str"
        return None
    if kind == "if":
        return join(infer_ir(node.get("then"), tenv, types, services, filename, line),
                    infer_ir(node.get("else"), tenv, types, services, filename, line),
                    types)
    return None


def check_ir(node, expected: str | None, tenv: dict, types: dict,
             services: dict, filename: str, line: int, where: str) -> None:
    """Bidirectional check of a lowered IR expression against `expected`.

    F4: stratum 1 (`fn`/`test`) has `check_ast`; stratum 3 (component and
    `provide` method bodies) had only `compatible(declared, infer_ir(...))` —
    inference, never a check position. So the shapes whose refusal lives in
    CHECK position (a record literal against a declared record's field set)
    were admitted inside a `provide` body while the identical `fn` body refused
    them, breaking the item 392/404/405 parity contract at a declared service
    boundary. This is `check_ast`'s structure over the IR dialect: the same
    positions push the expectation inward, and the tail is the same
    structural-meets-nominal resolution followed by `compatible`."""
    if expected is None or not isinstance(node, dict):
        infer_ir(node, tenv, types, services, filename, line)
        return
    kind = node.get("kind")
    if kind == "hole":
        # a lowered hole was already pinned with the type it was admitted at
        return
    spec = types.get(expected or "")
    if kind == "record" and isinstance(spec, dict) and spec.get("kind") == "record":
        declared = spec.get("fields", {})
        given = {name for name, _ in node.get("fields") or []}
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
        for name, value in node.get("fields") or []:
            check_ir(value, declared.get(name), tenv, types, services, filename,
                     line, f"field `{name}` of `{expected}`")
        return
    ehead, eargs = parse_type(expected)
    if kind == "list" and ehead == "List":
        for item in node.get("items") or []:
            check_ir(item, eargs[0] if eargs else None, tenv, types, services,
                     filename, line, f"element of `{expected}`")
        return
    if kind == "if":
        infer_ir(node.get("cond"), tenv, types, services, filename, line)
        check_ir(node.get("then"), expected, tenv, types, services, filename,
                 line, where)
        check_ir(node.get("else"), expected, tenv, types, services, filename,
                 line, where)
        return
    if kind == "match":
        # per-arm check position, for the reason `check_ast` gives: the JOIN of
        # disagreeing arms is None (unknown), so one `compatible` on the joined
        # type passes silently where each arm individually would be refused.
        infer_ir(node.get("scrutinee"), tenv, types, services, filename, line)
        for arm in node.get("arms") or []:
            inner = dict(tenv)
            bind = arm.get("bind")
            if bind is not None:
                payload_type = arm.get("payload_type")
                if payload_type is not None:
                    inner[bind] = payload_type
                else:
                    inner.pop(bind, None)
            check_ir(arm.get("body"), expected, inner, types, services,
                     filename, line, where)
        return
    if kind == "record_update":
        # docs/records.md §3: the result is the base's type. `infer_ir` runs the
        # per-field update checks; the expectation is checked against the base.
        base_t = infer_ir(node, tenv, types, services, filename, line)
        if base_t and not compatible(expected, base_t, types):
            raise mismatch(filename, line, where, expected,
                           render_type(base_t) or base_t)
        return
    actual = infer_ir(node, tenv, types, services, filename, line)
    struct = structural_fields(actual)
    if struct is not None and isinstance(spec, dict) \
            and spec.get("kind") == "record":
        # the declared boundary where a structural record meets the nominal one
        # (item 71) — named field-wise, exactly as `check_ast` names it
        declared = spec.get("fields", {})
        missing = sorted(set(declared) - set(struct))
        extra = sorted(set(struct) - set(declared))
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing {', '.join(f'`{m}`' for m in missing)}")
            if extra:
                parts.append(f"unknown {', '.join(f'`{e}`' for e in extra)}")
            raise RevlError(filename, line,
                            f"{where} expects `{expected}`, but the record has "
                            f"{'; '.join(parts)}")
        for name, ftype in struct.items():
            if ftype and not compatible(declared.get(name), ftype, types):
                raise mismatch(filename, line, f"field `{name}` of `{expected}`",
                               declared.get(name), ftype)
        return
    if actual and not compatible(expected, actual, types):
        raise mismatch(filename, line, where, expected, actual)

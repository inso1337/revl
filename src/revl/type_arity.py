"""Type-argument count of a declared type, checked where the type is written.

Issue #2942: the builtin generic heads (`Opt`, `List`, `Map`, `Result`) have
their arity checked by `typecheck.check_type_wellformed`, but a head the
program declares itself did not. `type Mb = Jj(Int) | Nn` written as `Mb[Int]`,
or `type Box[T] = { v: T }` written as `Box[Int, Int]`, type-checked, and the
checker then looked the applied spelling up in a table that has no entry of
that shape (a match over `Mb[Int]` missing `Nn` was admitted).

This pass runs over the parsed program before checking begins. It builds the
table of declared heads and their parameter counts, then walks every written
type annotation (the same field table the alias sweep uses,
`lower._ANNOTATION_FIELDS`) and refuses an application whose argument count
differs from its declaration, recursing into type arguments, function types
and structural records. The message keeps the builtin refusal's shape
(`` `List` takes 1 type argument(s), got 2 ``) so one marker covers both.

What it deliberately leaves alone:

- A declared generic head written with NO arguments (`Box` for
  `type Box[T]`). The checker reads that spelling today (a bare generic
  record literal is expanded from its declaration), so whether it is a type
  is a separate design question, not part of this fix.
- A head the program does not declare (a builtin, an imported or ambient
  type, a type parameter): there is no declaration to count against here.
- A declaration that reuses a builtin head's name (`type Opt = A | B`): the
  checker reads `Opt[Int]` as the builtin there, so the builtin's own check
  (or none) still decides it.
- An alias that declares parameters (`type A[T] = Box[T]`): alias resolution
  refuses the declaration itself, which names the real fault.
- A name that a declaration in scope binds as a type parameter
  (`type W[Mb] = { v: Mb[Int] }`, `fn f[Mb](..)`): inside that declaration
  the name is the parameter, not the top-level type.
"""

from __future__ import annotations

import dataclasses

from .errors import RevlError
from .parser import (ExternDecl, FnDecl, MethodDecl, Program, TypeDecl,
                     recursion_headroom)
from .taint import _QUALIFIERS
from .typecheck import _BUILTIN_TYPE_NAMES, parse_type, structural_fields

# Heads the checker gives a builtin meaning to whatever the program declares:
# the builtin types, the taint qualifiers, and the position-restricted heads
# `check_type_wellformed` refuses or admits by name.
_BUILTIN_HEADS = frozenset(_BUILTIN_TYPE_NAMES) | frozenset(_QUALIFIERS) | {
    "Async", "Delegate", "Approval", "Criterion", "Guard", "Fn"}


def declared_type_arity(program: Program) -> dict[str, int]:
    """Each type the program declares, by name, with its parameter count.
    A duplicate declaration is refused by the lowering pass; the first one
    wins here so this pass never raises on its behalf."""
    from .lower import _alias_target  # lazily, as in the walk below

    declared = {d.name for d in program.type_decls}
    arity: dict[str, int] = {}
    for decl in program.type_decls:
        if decl.name in _BUILTIN_HEADS:
            continue
        if decl.params and _alias_target(decl, declared) is not None:
            continue
        arity.setdefault(decl.name, len(decl.params or ()))
    return arity


def _params_text(count: int) -> str:
    return "no type parameters" if count == 0 else (
        f"{count} type parameter" + ("" if count == 1 else "s"))


def check_type_text(filename: str, line: int, type_name: str | None,
                    arity: dict[str, int]) -> None:
    """Refuse a declared head applied to the wrong number of type arguments
    anywhere inside `type_name`."""
    if not type_name or not arity:
        return
    fields = structural_fields(type_name)
    if fields is not None:
        for ftype in fields.values():
            check_type_text(filename, line, ftype, arity)
        return
    head, args = parse_type(type_name)
    want = arity.get(head or "")
    if want is not None and args and len(args) != want:
        if want == 0:
            hint = (f"`{head}` is declared with no type parameters; "
                    f"write `{head}` without arguments")
        else:
            hint = (f"`{head}` is declared with {_params_text(want)}; "
                    f"write exactly {want} argument(s) inside `{head}[...]`")
        raise RevlError(
            filename, line,
            f"`{head}` takes {want} type argument(s), got {len(args)} "
            f"(`{type_name.strip()}`)",
            hint=hint,
        )
    for arg in args:
        check_type_text(filename, line, arg, arity)


def _scoped(arity: dict[str, int], bound) -> dict[str, int]:
    """`arity` without the names a declaration binds as type parameters."""
    names = set(bound or ())
    if not names & arity.keys():
        return arity
    return {k: v for k, v in arity.items() if k not in names}


def check_declared_type_arity(program: Program, filename: str) -> None:
    """Walk every written type annotation in `program` and refuse a declared
    type head given the wrong number of type arguments (issue #2942)."""
    arity = declared_type_arity(program)
    if not arity:
        return
    # imported lazily: `lower` imports a great deal, and this module is
    # imported by `compiler` next to it.
    from .lower import _ANNOTATION_FIELDS

    seen: set[int] = set()

    def walk(node, table: dict[str, int], file: str, line: int) -> None:
        if node is None or isinstance(node, (str, bytes, int, float, bool)):
            return
        if isinstance(node, (list, tuple, set, frozenset)):
            for item in node:
                walk(item, table, file, line)
            return
        if isinstance(node, dict):
            for item in node.values():
                walk(item, table, file, line)
            return
        if not dataclasses.is_dataclass(node) or id(node) in seen:
            return
        seen.add(id(node))
        line = getattr(node, "line", None) or line
        # a declaration carried in from an imported module names its own
        # file, not the importer's (the provenance issue #1904 records)
        file = program.decl_files.get(id(node)) or (
            getattr(node, "source", None) if isinstance(node, FnDecl)
            else None) or file
        if isinstance(node, TypeDecl):
            table = _scoped(table, node.params)
            # an alias keeps its right-hand side as the name of its one case
            # (`type A = Box[Int]`); the parser builds a bracketed case name
            # only there, so a bracket marks the written type.
            if (not node.fields and len(node.cases) == 1
                    and node.cases[0].payload is None
                    and "[" in (node.cases[0].name or "")):
                check_type_text(file, line, node.cases[0].name, table)
        elif isinstance(node, (FnDecl, ExternDecl)):
            table = _scoped(table, node.type_params)
        annotated = next((names for kind, names in _ANNOTATION_FIELDS.items()
                          if isinstance(node, kind)), ())
        for name in annotated:
            value = getattr(node, name)
            for written in (value if isinstance(value, list) else [value]):
                if isinstance(written, str):
                    check_type_text(file, line, written, table)
        if isinstance(node, MethodDecl):
            for _, ptype in node.params:
                check_type_text(file, line, ptype, table)
        for fld in dataclasses.fields(node):
            if fld.name not in annotated:
                walk(getattr(node, fld.name), table, file, line)

    # the same stack the parser gave itself to accept the tree (issue #310).
    # A tree deeper than that is refused by `check_and_lower` with its own
    # nesting diagnostic, so this pass steps aside rather than crash.
    with recursion_headroom():
        try:
            walk(program, arity, filename, 0)
        except RecursionError:
            return

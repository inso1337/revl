"""Which declarations of transport-carried text are still the operator's (#1715).

An agent that edits a file the operator wrote sends back the whole file, most of
it unchanged. Trust follows the text, declaration by declaration: a declaration
whose parse is identical to one in the operator's own text of the same file
(the file on disk, inside the sanctioned roots) is the operator's, and keeps the
trust it had at load. Every other declaration in transport-carried text is the
agent's, and the untrusted-author profile applies to it in full.

"Identical" is structural: the two declarations parse to the same tree once
source positions are ignored, so moving an operator declaration down a few
lines does not make it the agent's, while any change to its body, signature or
host code does. Matching is a multiset per declaration list, so a duplicated
operator declaration is trusted once, not twice.

This module only computes the split. `compiler.compile_files` applies the
profile to the agent's part (`delta_program`), and its loader exempts the
operator's own `use` paths from untrusted-author confinement (`operator_uses`).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import fields, is_dataclass, replace

from .errors import RevlError
from .parser import Parser, Program

# Program lists whose items are declarations that can be matched one by one.
# `assets` is a registry of expressions inside those declarations, matched the
# same way; the remaining fields are bookkeeping the parser and merge fill in.
_DECL_LISTS = tuple(f.name for f in fields(Program)
                    if f.name not in ("filename", "fn_scopes", "fn_alias_scopes",
                                      "decl_files"))


def _position_field(name: str) -> bool:
    return name == "line" or name.endswith("_line")


# A field annotated with a tuple whose LAST member is an `int` carries the source
# line there: `provides: list[tuple[str, str, int]]  # (key, service, line)`,
# `uses: list[tuple[str, int]]`, `site: tuple[str, int] | None`, and the rest.
_LINE_TUPLE = re.compile(r"tuple\[[^\[\]]*,\s*int\]")


def _drop_line(value):
    """Strip the trailing line from each tuple of a line-carrying field."""
    if isinstance(value, tuple) and value:
        return value[:-1]
    if isinstance(value, list):
        return [_drop_line(item) for item in value]
    return value


def _field_shape(node, f):
    value = getattr(node, f.name)
    if isinstance(f.type, str) and _LINE_TUPLE.search(f.type):
        value = _drop_line(value)
    return shape(value)


def shape(node):
    """A hashable form of an AST node with every source position removed."""
    if is_dataclass(node) and not isinstance(node, type):
        return (type(node).__name__,
                tuple((f.name, _field_shape(node, f))
                      for f in fields(node) if not _position_field(f.name)))
    if isinstance(node, (list, tuple)):
        return ("seq", tuple(shape(item) for item in node))
    if isinstance(node, dict):
        # a dict-valued field can carry a position too (`MethodDecl.route` is
        # `{"method", "path", "line"}`)
        return ("map", tuple(sorted((repr(k), shape(v)) for k, v in node.items()
                                    if not (isinstance(k, str) and _position_field(k)))))
    if isinstance(node, (set, frozenset)):
        return ("set", tuple(sorted(repr(item) for item in node)))
    if node is None or isinstance(node, (str, int, float, bool)):
        return node
    # anything else compares by its repr; an unequal repr only ever makes a
    # declaration the agent's, which is the safe direction
    return ("repr", repr(node))


def _parse(text: str, filename: str) -> Program | None:
    try:
        return Parser(text, filename).parse()
    except RevlError:
        return None


def trusted_indices(agent_text: str, operator_text: str,
                    filename: str) -> dict[str, frozenset[int]]:
    """For each declaration list, the indices of the agent text's items that are
    the operator's. Both texts are parsed fresh, so the indices refer to the
    parser's order, which is the order of `module.program`'s lists. Text that
    does not parse trusts nothing."""
    agent, operator = _parse(agent_text, filename), _parse(operator_text, filename)
    if agent is None or operator is None:
        return {}
    trusted: dict[str, frozenset[int]] = {}
    for name in _DECL_LISTS:
        available = Counter(shape(item) for item in getattr(operator, name))
        kept = set()
        for index, item in enumerate(getattr(agent, name)):
            key = shape(item)
            if available[key] > 0:
                available[key] -= 1
                kept.add(index)
        trusted[name] = frozenset(kept)
    return trusted


def delta_program(program: Program, trusted: dict[str, frozenset[int]],
                  placements: frozenset = frozenset()) -> Program:
    """`program` with only the agent's declarations: every item of a declaration
    list whose index is not trusted. The nodes are `program`'s own, so a check
    run over the delta sees exactly what the merge lowers.

    `placements` (`operator_placements`) are the realm placements the
    operator's own text makes. A changed component keeps them out of its delta
    copy (issue #1851): editing a method of a realm-isolated operator
    component leaves its `isolate` clause the operator's, so the untrusted-
    author profile does not read it as the agent naming a realm. Only a clause
    identical to one in the operator's same-named component is dropped; one
    that moves the component to another realm, or a new component's, stays in
    the delta and is refused as before."""
    kept = {name: [item for index, item in enumerate(getattr(program, name))
                   if index not in trusted.get(name, frozenset())]
            for name in _DECL_LISTS}
    if placements:
        kept["components"] = [_without_placements(comp, placements)
                              for comp in kept["components"]]
    return replace(program, **kept)


def _realm_statements(comp) -> list:
    from .parser import IsolateStmt, RouteStmt  # noqa: PLC0415 - import cycle

    return [stmt for stmt in comp.body if isinstance(stmt, (IsolateStmt, RouteStmt))]


def _without_placements(comp, placements: frozenset):
    """A copy of `comp` without the realm statements the operator wrote in its
    same-named component. The original node, which the merge lowers, is
    untouched."""
    ours = {id(stmt) for stmt in _realm_statements(comp)
            if (comp.name, shape(stmt)) in placements}
    if not ours:
        return comp
    return replace(comp, body=[stmt for stmt in comp.body if id(stmt) not in ours])


def operator_placements(operator_text: str, filename: str) -> frozenset:
    """`(component name, statement shape)` of every top-level `isolate ... in
    realm(...)` / `realms(...)` statement in the operator's own text."""
    program = _parse(operator_text, filename)
    if program is None:
        return frozenset()
    return frozenset((comp.name, shape(stmt)) for comp in program.components
                     for stmt in _realm_statements(comp))


def empty_program(program: Program) -> Program:
    """The agent's part of a module read from disk: nothing."""
    return replace(program, **{name: [] for name in _DECL_LISTS})


def operator_uses(operator_text: str, filename: str) -> frozenset[str]:
    """The `use` paths the operator's own text of a file names."""
    program = _parse(operator_text, filename)
    return frozenset(use.path for use in program.uses) if program else frozenset()


__all__ = ["shape", "trusted_indices", "delta_program", "empty_program",
           "operator_uses", "operator_placements"]

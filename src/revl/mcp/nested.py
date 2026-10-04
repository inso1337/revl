"""Nested symbols: a method inside a component, an operation of a service, a
field of a type (issue #1733).

`revl_source` and `{symbol}` edits address top-level declarations; to change one
method an agent still read and resent the whole component. A nested symbol is a
dotted path under a top-level declaration:

* ``Component.key`` a whole `provide key { ... }` block, and
  ``Component.key.method`` one method in it; ``Component.method`` when exactly
  one provide block has a method of that name;
* ``Service.operation``;
* ``Type.field`` or ``Type.Case``.

A member's span is line based: it starts on the member's own line and runs
until the brackets opened inside it are closed and the next member, or the
enclosing block's closing brace, begins. It is only ever used once it is
PROVED to be exactly that member: deleting the span and re-parsing the buffer
has to give the same enclosing declaration with that one member missing and
nothing else different, and an edit has to re-parse to the same enclosing
declaration with only that member changed. A member that shares a line with
something else fails the proof and is not addressable; a `range` or `anchor`
edit still reaches it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from ..errors import RevlError
from ..parser import Parser, ProvideStmt


class NestedError(RuntimeError):
    """A nested symbol could not be found or proved."""


@dataclass(frozen=True)
class Member:
    path: tuple[str, ...]   # under the top-level declaration
    kind: str
    line: int


_OPENERS, _CLOSERS = "([{", ")]}"


def members(node, kind: str) -> list[Member]:
    """The addressable members of a top-level declaration."""
    out: list[Member] = []
    if kind == "component":
        for stmt in node.body:
            if isinstance(stmt, ProvideStmt):
                out.append(Member((stmt.key,), "provision", stmt.line))
                out += [Member((stmt.key, m.name), "method", m.line)
                        for m in stmt.methods]
    elif kind == "service":
        out += [Member((name,), "operation", m.line)
                for name, m in node.methods.items()]
    elif kind == "type":
        out += [Member((f.name,), "field", f.line) for f in node.fields]
        out += [Member((c.name,), "case", c.line) for c in node.cases]
    return out


def member_nodes(node, kind: str) -> dict:
    """path -> (member kind, AST node), for comparing two versions."""
    out: dict = {}
    if kind == "component":
        for stmt in node.body:
            if isinstance(stmt, ProvideStmt):
                for m in stmt.methods:
                    out[(stmt.key, m.name)] = ("method", m)
    elif kind == "service":
        out.update({(name,): ("operation", m) for name, m in node.methods.items()})
    elif kind == "type":
        out.update({(f.name,): ("field", f) for f in node.fields})
        out.update({(c.name,): ("case", c) for c in node.cases})
    return out


def _bare(node, kind: str):
    """`node` with every member's own content removed: what is left is the
    declaration's frame (its header, its non-member statements, and for a
    component the provide blocks' keys)."""
    node = copy.deepcopy(node)
    if kind == "component":
        for stmt in node.body:
            if isinstance(stmt, ProvideStmt):
                stmt.methods = [m.name for m in stmt.methods]
    elif kind == "service":
        node.methods = sorted(node.methods)
    elif kind == "type":
        node.fields = [f.name for f in node.fields]
        node.cases = [c.name for c in node.cases]
    return node


def member_changes(kind: str, old, new) -> list[tuple] | None:
    """The members that differ between two versions of a declaration, as
    (path, member kind, change), or None when the declaration's frame itself
    changed (then the whole declaration is what changed)."""
    from ..operator_text import shape  # noqa: PLC0415

    if kind not in ("component", "service", "type") \
            or shape(_bare(old, kind)) != shape(_bare(new, kind)):
        return None
    was, now = member_nodes(old, kind), member_nodes(new, kind)
    out = []
    for path in list(now) + [p for p in was if p not in now]:
        if path not in was:
            out.append((path, now[path][0], "added"))
        elif path not in now:
            out.append((path, was[path][0], "removed"))
        elif shape(was[path][1]) != shape(now[path][1]):
            out.append((path, now[path][0], "changed"))
    return out


def find(node, kind: str, rest: tuple[str, ...]) -> Member:
    """The member `rest` names. A single name also matches a component method
    when exactly one provide block has it."""
    found = [m for m in members(node, kind) if m.path == rest]
    if not found and kind == "component" and len(rest) == 1:
        found = [m for m in members(node, kind)
                 if m.kind == "method" and m.path[-1] == rest[0]]
    if len(found) != 1:
        what = "no" if not found else "more than one"
        known = ", ".join(".".join(m.path) for m in members(node, kind)) or "none"
        raise NestedError(f"{what} member of {kind} `{node.name}` matches "
                          f"`{'.'.join(rest)}` (members: {known})")
    return found[0]


def _item_lines(node, kind: str) -> list[int]:
    """Every line at which something inside the declaration begins."""
    lines = [m.line for m in members(node, kind)]
    if kind == "component":
        lines += [getattr(stmt, "line", 0) for stmt in node.body]
    return sorted({line for line in lines if line})


def span(text: str, name: str, node, kind: str, member: Member) -> tuple[int, int]:
    """The member's (first, last) lines, before the proof."""
    from ..lexer import lex  # noqa: PLC0415

    after = [line for line in _item_lines(node, kind) if line > member.line]
    next_line = after[0] if after else None
    try:
        tokens = [t for t in lex(text, name) if t.line >= member.line]
    except RevlError as error:
        raise NestedError(f"{name} does not lex: {error}") from None
    depth, last = 0, member.line
    for tok in tokens:
        if tok.kind == "eof":
            break
        if tok.kind in _CLOSERS and depth == 0:
            break
        if depth == 0 and next_line is not None and tok.line >= next_line:
            break
        if tok.kind in _OPENERS:
            depth += 1
        elif tok.kind in _CLOSERS:
            depth -= 1
        last = tok.line
    return member.line, last


# ---------------------------------------------------------------- the proof

def _drop(node, kind: str, member: Member):
    """A copy of `node` without `member`."""
    node = copy.deepcopy(node)
    if kind == "component":
        if member.kind == "provision":
            node.body = [s for s in node.body
                         if not (isinstance(s, ProvideStmt) and s.key == member.path[0])]
        else:
            for stmt in node.body:
                if isinstance(stmt, ProvideStmt) and stmt.key == member.path[0]:
                    stmt.methods = [m for m in stmt.methods if m.name != member.path[1]]
    elif kind == "service":
        node.methods = {k: v for k, v in node.methods.items() if k != member.path[0]}
    elif kind == "type":
        node.fields = [f for f in node.fields if f.name != member.path[0]]
        node.cases = [c for c in node.cases if c.name != member.path[0]]
    return node


def _program(text: str, name: str):
    try:
        return Parser(text, name).parse()
    except RevlError:
        return None


def _top(program, kind: str, top_name: str):
    from .symbols import _KINDS  # noqa: PLC0415

    field = next(f for f, k in _KINDS.items() if k == kind)
    hits = [item for item in getattr(program, field) if item.name == top_name]
    return hits[0] if len(hits) == 1 else None


def _others(program, kind: str, top_name: str) -> object:
    """Every top-level declaration except the enclosing one, position free."""
    from ..operator_text import shape  # noqa: PLC0415
    from .symbols import _KINDS  # noqa: PLC0415

    out = []
    for field, item_kind in _KINDS.items():
        for item in getattr(program, field):
            if not (item_kind == kind and getattr(item, "name", None) == top_name):
                out.append((item_kind, shape(item)))
    return sorted(out, key=repr)


def _splice(text: str, first: int, last: int, middle: str) -> str:
    lines = text.split("\n")
    return "\n".join(lines[:first - 1] + ([middle.rstrip("\n")] if middle else [])
                     + lines[last:])


def prove(text: str, name: str, kind: str, top_name: str, member: Member,
          first: int, last: int) -> None:
    """Deleting the span leaves the enclosing declaration minus exactly this
    member, and every other declaration as it was."""
    from ..operator_text import shape  # noqa: PLC0415

    before = _program(text, name)
    after = _program(_splice(text, first, last, ""), name)
    ok = before is not None and after is not None
    if ok:
        old, new = _top(before, kind, top_name), _top(after, kind, top_name)
        ok = (old is not None and new is not None
              and shape(_drop(old, kind, member)) == shape(new)
              and _others(before, kind, top_name) == _others(after, kind, top_name))
    if not ok:
        raise NestedError(
            f"`{top_name}.{'.'.join(member.path)}` could not be isolated: its "
            "lines are not exactly that member (it may share a line with "
            "something else), so it is not addressable by symbol; use a "
            "`range` or `anchor` edit")


def prove_replacement(text: str, new_text: str, name: str, kind: str,
                      top_name: str, member: Member) -> None:
    """The edited buffer has the same enclosing declaration, with only this
    member changed, and every other declaration as it was."""
    from ..operator_text import shape  # noqa: PLC0415

    before, after = _program(text, name), _program(new_text, name)
    if after is None:
        return  # the compile reports the syntax error with its own message
    old, new = _top(before, kind, top_name), _top(after, kind, top_name)
    still = new is not None and any(m.path == member.path and m.kind == member.kind
                                    for m in members(new, kind))
    if not (still and shape(_drop(old, kind, member)) == shape(_drop(new, kind, member))
            and _others(before, kind, top_name) == _others(after, kind, top_name)):
        raise NestedError(
            f"the replacement for `{top_name}.{'.'.join(member.path)}` changes "
            "more than that member (or no longer declares it); send exactly the "
            "member's new text")


def node_of(text: str, name: str, kind: str, top_name: str):
    program = _program(text, name)
    node = _top(program, kind, top_name) if program is not None else None
    if node is None:
        raise NestedError(f"`{top_name}` could not be read from {name}")
    return node


__all__ = ["NestedError", "Member", "members", "member_nodes", "member_changes",
           "find", "span", "prove", "prove_replacement", "node_of"]

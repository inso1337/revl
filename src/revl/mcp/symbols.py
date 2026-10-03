"""Symbol-addressed reads and edits of the server-side source (issue #1714).

To change one component an agent used to receive the whole file. Measured on
`examples/app/notes.rvl` (token proxy ceil(chars/4)): the file is about 4,858
tokens, the `NotesHttp` component 373, its code without comments 144, and its
code plus the declarations it names 297. Addressing is what saves, and it needs
nothing but the parser.

A symbol is a top-level declaration: a service, component, type, function,
extern, test and the rest. It is addressed as

* ``"Name"``: the one declaration with that name across the session's buffers;
* ``"<buffer>:Name"``: the declaration with that name in one buffer (a loaded
  file's path, an in-memory module's key, or ``source``);
* ``"<buffer>:<line>"``: the declaration that contains that line.

A declaration's span starts at its first line and runs to the line before the
next top-level declaration, less the blank and comment lines between them
(those belong to what follows). A span is only used once it parses, on its own,
as exactly that one declaration, so a construct the parser keeps elsewhere can
never be swallowed into a neighbour's span and then overwritten by an edit.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import RevlError
from ..parser import Parser, Program


class SymbolError(RuntimeError):
    """A symbol could not be found, was ambiguous, or could not be isolated.
    A result the agent reads; nothing was changed."""


@dataclass(frozen=True)
class Decl:
    name: str
    kind: str
    start: int   # 1-based first line of the declaration itself
    end: int     # 1-based last line, inclusive


# Program list -> the kind a symbol of it reports. `uses` and `assets` are not
# symbols, but a `use` line still bounds the declaration above it.
_KINDS = {"services": "service", "components": "component", "type_decls": "type",
          "event_decls": "event", "fn_decls": "fn", "externs": "extern",
          "secrets": "secret", "retentions": "retention", "model_roles": "model role",
          "model_councils": "model council", "tests": "test",
          "fault_tests": "fault test", "prop_tests": "property test",
          "compositions": "composition", "layers": "layer"}


def _parse(text: str, name: str) -> Program:
    try:
        return Parser(text, name).parse()
    except RevlError as error:
        raise SymbolError(f"{name} does not parse, so its symbols cannot be "
                          f"addressed: {error}") from None


def _starts(program: Program) -> dict[int, tuple[str, str]]:
    """First line -> (name, kind) of every top-level declaration. An event is
    also a record type on the same line; the event wins."""
    starts: dict[int, tuple[str, str]] = {}
    for use in program.uses:
        starts.setdefault(use.line, ("", "use"))
    for field, kind in _KINDS.items():
        for item in getattr(program, field):
            name = getattr(item, "name", None)
            if isinstance(name, str) and name:
                if kind == "event" or item.line not in starts:
                    starts[item.line] = (name, kind)
    return starts


def _trivial(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("//")


def declarations(text: str, name: str) -> list[Decl]:
    """Every named top-level declaration of `text`, with its line span."""
    lines = text.split("\n")
    starts = _starts(_parse(text, name))
    ordered = sorted(starts)
    out: list[Decl] = []
    for index, start in enumerate(ordered):
        decl_name, kind = starts[start]
        end = (ordered[index + 1] - 1) if index + 1 < len(ordered) else len(lines)
        while end > start and _trivial(lines[end - 1]):
            end -= 1
        if kind != "use":
            out.append(Decl(decl_name, kind, start, end))
    return out


def leading_comment_start(text: str, decl: Decl) -> int:
    """The first line of the comment block directly above `decl` (no blank
    line between), or `decl.start` when there is none."""
    lines = text.split("\n")
    first = decl.start
    while first > 1 and lines[first - 2].strip().startswith("//"):
        first -= 1
    return first


def span_text(text: str, first: int, last: int) -> str:
    return "\n".join(text.split("\n")[first - 1:last]) + "\n"


def isolate(text: str, decl: Decl, name: str) -> str:
    """The declaration's own text, checked to parse as exactly that one
    declaration."""
    own = span_text(text, decl.start, decl.end)
    program = _parse(own, name)
    found = [(n, k) for n, k in _starts(program).values() if k != "use"]
    if found != [(decl.name, decl.kind)] or program.uses:
        raise SymbolError(
            f"{decl.kind} `{decl.name}` in {name} could not be isolated from its "
            "neighbours (its lines do not parse as that one declaration), so it "
            "is not addressable by symbol; use a `range` or `anchor` edit")
    return own


# ---------------------------------------------------------------- buffers

def buffers(vs: dict) -> list[tuple[tuple[str, str], str]]:
    """Every buffer of a working set as ``((kind, key), text)``."""
    out = []
    if vs.get("source") is not None:
        out.append((("source", "source"), vs["source"]))
    for path in vs.get("files") or []:
        text = (vs.get("files_content") or {}).get(path)
        if text is not None:
            out.append((("file", path), text))
    for key, text in (vs.get("modules") or {}).items():
        out.append((("module", key), text))
    return out


def _buffer_named(vs: dict, wanted: str):
    from .edit import EditError, _resolve_buffer  # noqa: PLC0415 — cycle

    try:
        return _resolve_buffer(vs, wanted)
    except EditError as error:
        raise SymbolError(str(error)) from None


def _split(symbol: str) -> tuple[str | None, str]:
    """``"buffer:Name"`` -> (buffer, Name); a bare name has no buffer. The last
    colon splits, so a path that contains one still works."""
    if ":" in symbol:
        buffer, _, rest = symbol.rpartition(":")
        if buffer and rest:
            return buffer, rest
    return None, symbol


def resolve(vs: dict, symbol: str) -> tuple[tuple[str, str], str, Decl]:
    """The buffer, its text and the declaration `symbol` addresses."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise SymbolError("`symbol` must be a declaration name, `<buffer>:Name`, "
                          "or `<buffer>:<line>`")
    wanted_buffer, rest = _split(symbol.strip())
    candidates = buffers(vs)
    if wanted_buffer is not None:
        buffer = _buffer_named(vs, wanted_buffer)
        candidates = [(b, t) for b, t in candidates if b == buffer]
    hits = []
    for buffer, text in candidates:
        for decl in declarations(text, buffer[1]):
            if (rest.isdigit() and decl.start <= int(rest) <= decl.end) \
                    or decl.name == rest:
                hits.append((buffer, text, decl))
    if not hits:
        raise SymbolError(f"no top-level declaration matches `{symbol}`")
    if len(hits) > 1:
        where = ", ".join(f"{b[1]}:{d.name}" for b, _, d in hits)
        raise SymbolError(f"`{symbol}` names more than one declaration ({where}); "
                          "qualify it as `<buffer>:Name`")
    return hits[0]


# ---------------------------------------------------------------- nested

@dataclass(frozen=True)
class Nested:
    """A member under a top-level declaration, with its proved line span."""
    top: Decl
    member: object      # nested.Member
    first: int
    last: int

    @property
    def path(self) -> str:
        return ".".join((self.top.name,) + self.member.path)


def _top_and_rest(symbol: str) -> tuple[str, tuple[str, ...]]:
    """``[buffer:]Top.a.b`` -> (``[buffer:]Top``, ("a", "b"))."""
    wanted_buffer, rest = _split(symbol.strip())
    if rest.isdigit() or "." not in rest:
        return symbol.strip(), ()
    top, *members = rest.split(".")
    if not top or not all(members):
        raise SymbolError(f"`{symbol}` is not a symbol path (Name or Name.member)")
    return (f"{wanted_buffer}:{top}" if wanted_buffer else top), tuple(members)


def locate(vs: dict, symbol: str):
    """The buffer, its text, the top-level declaration, and the proved nested
    member (None for a top-level symbol) that `symbol` addresses."""
    from . import nested  # noqa: PLC0415

    top_symbol, rest = _top_and_rest(symbol)
    buffer, text, decl = resolve(vs, top_symbol)
    if not rest:
        return buffer, text, decl, None
    try:
        node = nested.node_of(text, buffer[1], decl.kind, decl.name)
        member = nested.find(node, decl.kind, rest)
        first, last = nested.span(text, buffer[1], node, decl.kind, member)
        nested.prove(text, buffer[1], decl.kind, decl.name, member, first, last)
    except nested.NestedError as error:
        raise SymbolError(str(error)) from None
    return buffer, text, decl, Nested(decl, member, first, last)


def _comment_start(text: str, line: int) -> int:
    lines = text.split("\n")
    first = line
    while first > 1 and lines[first - 2].strip().startswith("//"):
        first -= 1
    return first


def _requires_of(text: str, name: str, decl: Decl) -> dict[str, str]:
    """A component's require locals -> the services they name."""
    if decl.kind != "component":
        return {}
    program = Parser(text, name).parse()
    comp = next(c for c in program.components if c.name == decl.name)
    return {local: service for local, service, _line in comp.requires}


def _read_member(vs, buffer, text, found: Nested, deps: bool, comments: bool) -> dict:
    first = _comment_start(text, found.first) if comments else found.first
    own = span_text(text, found.first, found.last)
    shown = span_text(text, first, found.last) if comments else own
    result = {"symbol": found.path, "kind": found.member.kind,
              "parent": found.top.name, "parentKind": found.top.kind,
              "buffer": buffer[1], "line": found.first,
              "text": render(shown, comments, buffer[1])}
    if deps:
        names = _names_in(own, buffer[1])
        services = {svc for local, svc in _requires_of(text, buffer[1],
                                                       found.top).items()
                    if local in names}
        result["deps"] = [
            describe(b, d, render(isolate(t, d, b[1]), comments, b[1]))
            for b, t, d in _named(vs, names | services, (buffer, found.top))]
    return result


def _named(vs: dict, names: set[str], exclude) -> list:
    out = []
    for buffer, buffer_text in buffers(vs):
        for decl in declarations(buffer_text, buffer[1]):
            if decl.name in names and (buffer, decl) != exclude:
                out.append((buffer, buffer_text, decl))
    return out


# ---------------------------------------------------------------- reading

def render(text: str, comments: bool, name: str) -> str:
    """A declaration's text, verbatim with comments, or canonical without them
    (the formatter's own rendering, comment lines dropped)."""
    if comments:
        return text
    from ..formatter import format_source  # noqa: PLC0415

    return format_source(text, name, comments=False)


def _names_in(text: str, name: str) -> set[str]:
    from ..lexer import lex  # noqa: PLC0415

    try:
        return {tok.value for tok in lex(text, name) if tok.kind == "ident"}
    except RevlError:
        return set()


def dependencies(vs: dict, own: tuple[tuple[str, str], Decl], text: str) -> list:
    """The other top-level declarations, in any buffer, that `text` names."""
    names = _names_in(text, own[0][1])
    out = []
    for buffer, buffer_text in buffers(vs):
        for decl in declarations(buffer_text, buffer[1]):
            if decl.name in names and (buffer, decl) != own:
                out.append((buffer, buffer_text, decl))
    return out


def describe(buffer, decl: Decl, text: str) -> dict:
    return {"symbol": decl.name, "kind": decl.kind, "buffer": buffer[1],
            "line": decl.start, "text": text}


def read(vs: dict, symbol: str, *, deps: bool = False,
         comments: bool = True) -> dict:
    """`revl_source`'s answer: the addressed declaration and, with `deps`, the
    declarations it names."""
    buffer, text, decl, found = locate(vs, symbol)
    if found is not None:
        return _read_member(vs, buffer, text, found, deps, comments)
    own = isolate(text, decl, buffer[1])
    first = leading_comment_start(text, decl) if comments else decl.start
    shown = span_text(text, first, decl.end) if comments else own
    result = describe(buffer, decl, render(shown, comments, buffer[1]))
    if deps:
        result["deps"] = [
            describe(b, d, render(isolate(t, d, b[1]), comments, b[1]))
            for b, t, d in dependencies(vs, (buffer, decl), own)]
    return result


# ---------------------------------------------------------------- editing

def replace(vs: dict, symbol: str, replacement: str) -> tuple[tuple[str, str], str, dict]:
    """Replace the addressed declaration's lines (not the comments above it)
    with `replacement`. Returns the buffer, its new text and the edit's echo."""
    buffer, text, decl, found = locate(vs, symbol)
    if found is not None:
        return _replace_member(buffer, text, found, replacement)
    isolate(text, decl, buffer[1])
    lines = text.split("\n")
    body = replacement if replacement.endswith("\n") else replacement + "\n"
    new_text = ("\n".join(lines[:decl.start - 1])
                + ("\n" if decl.start > 1 else "")
                + body + "\n".join(lines[decl.end:]))
    return buffer, new_text, {"form": "symbol", "symbol": decl.name,
                              "kind": decl.kind, "line": decl.start}


def _replace_member(buffer, text: str, found: Nested, replacement: str):
    """Splice a member's new text in place of its proved span, then prove the
    result changed that member and nothing else."""
    from . import nested  # noqa: PLC0415

    lines = text.split("\n")
    indent = lines[found.first - 1][:len(lines[found.first - 1])
                                     - len(lines[found.first - 1].lstrip())]
    body = [line if not line.strip() or line.startswith(indent) else indent + line
            for line in replacement.rstrip("\n").split("\n")]
    new_text = "\n".join(lines[:found.first - 1] + body + lines[found.last:])
    try:
        nested.prove_replacement(text, new_text, buffer[1], found.top.kind,
                                 found.top.name, found.member)
    except nested.NestedError as error:
        raise SymbolError(str(error)) from None
    return buffer, new_text, {"form": "symbol", "symbol": found.path,
                              "kind": found.member.kind, "line": found.first}


def remove(vs: dict, symbol: str) -> tuple[tuple[str, str], str, dict]:
    """Delete the addressed declaration and the comment block directly above
    it (issue #1695: a withdrawal). Returns the buffer, its new text and the
    edit's echo."""
    buffer, text, decl, found = locate(vs, symbol)
    if found is not None:
        first, end = _comment_start(text, found.first), found.last
    else:
        isolate(text, decl, buffer[1])
        first, end = leading_comment_start(text, decl), decl.end
    lines = text.split("\n")
    kept = lines[:first - 1] + lines[end:]
    # do not leave two blank lines where the declaration was
    while first - 1 < len(kept) and first > 1 and not kept[first - 2].strip() \
            and not kept[first - 1].strip():
        del kept[first - 1]
    return buffer, "\n".join(kept), {
        "form": "remove", "symbol": found.path if found else decl.name,
        "kind": found.member.kind if found else decl.kind,
        "line": found.first if found else decl.start}


# ---------------------------------------------------------------- what changed

def _shapes(text: str, name: str) -> dict[tuple[str, str], tuple] | None:
    """(kind, name) -> (shape, node) for every top-level declaration."""
    from ..operator_text import shape  # noqa: PLC0415

    try:
        program = Parser(text, name).parse()
    except RevlError:
        return None
    out = {}
    for field, kind in _KINDS.items():
        for item in getattr(program, field):
            item_name = getattr(item, "name", None)
            if isinstance(item_name, str) and item_name:
                out[(kind, item_name)] = (shape(item), item)
    return out


def _changed(buffer, key, old_node, new_node) -> list[dict]:
    """A changed declaration, reported at the finest level that holds: its
    members when only members changed (issue #1733), else the whole of it."""
    from . import nested  # noqa: PLC0415

    kind, name = key
    members = nested.member_changes(kind, old_node, new_node)
    if not members:
        return [{"symbol": name, "kind": kind, "buffer": buffer[1],
                 "change": "changed"}]
    return [{"symbol": ".".join((name,) + path), "kind": member_kind,
             "parent": name, "parentKind": kind, "buffer": buffer[1],
             "change": change} for path, member_kind, change in members]


def touched(before: dict, after: dict) -> list[dict]:
    """The top-level declarations a change added, changed or removed, buffer by
    buffer. A buffer that does not parse on either side is reported whole."""
    old = dict(buffers(before))
    new = dict(buffers(after))
    out = []
    for buffer in list(new) + [b for b in old if b not in new]:
        if old.get(buffer) == new.get(buffer):
            continue
        was = _shapes(old[buffer], buffer[1]) if buffer in old else {}
        now = _shapes(new[buffer], buffer[1]) if buffer in new else {}
        if was is None or now is None:
            out.append({"buffer": buffer[1], "change": "unparsed"})
            continue
        for key in list(now) + [k for k in was if k not in now]:
            if key in was and key in now:
                if was[key][0] != now[key][0]:
                    out += _changed(buffer, key, was[key][1], now[key][1])
                continue
            out.append({"symbol": key[1], "kind": key[0], "buffer": buffer[1],
                        "change": "added" if key not in was else "removed"})
    return out


__all__ = ["SymbolError", "declarations", "resolve", "read", "replace", "remove",
           "touched"]

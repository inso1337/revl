"""One parse of one text, shared by the read-only readers of one tool call
(issue #2194).

A single `revl_check` used to parse the text it was sent three times before
compiling it (the path jail, the authoring gate and the completer each built
their own `Parser`) and to lex it once more in the formatter's token gate, all
on the same text under the same name. Each of those readers only LOOKS at the
result, so inside one MCP tool call they now share it.

The rules that keep this an identity, not a cache:

* A result is reused only for the same `(text, filename)`. Nothing is keyed on
  a path, so a file that changes on disk is simply a different text.
* The memo lives for one tool call (`scope`) and is dropped at its end. With
  no scope open, `program` and `tokens` are exactly `Parser(...).parse()` and
  `lex(...)`, so a caller outside the MCP server sees no difference.
* Only readers that do not mutate what they are given may use it. A compile
  still makes its own parse: the compiler owns and rewrites its tree.
* A failure is shared as well: a reader that would have raised raises the same
  error.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_MEMO: ContextVar[dict | None] = ContextVar("revl_shared_parse", default=None)


class _Entry:
    """What is known about one `(text, filename)`: its tokens, its program,
    and the error that stopped either."""

    __slots__ = ("tokens", "lex_error", "program", "parse_error", "parsed")

    def __init__(self) -> None:
        self.tokens = None
        self.lex_error = None
        self.program = None
        self.parse_error = None
        self.parsed = False


@contextmanager
def scope():
    """Share parses for the duration of one tool call. A nested scope (a tool
    call made from inside another one, such as an undo) starts empty."""
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def program(text: str, filename: str):
    """`Parser(text, filename).parse()`, for a reader that does not mutate it.
    Raises what that parse raises."""
    from .parser import Parser  # noqa: PLC0415 - lazy, like the readers' own imports

    memo = _MEMO.get()
    if memo is None:
        return Parser(text, filename).parse()
    entry = memo.setdefault((text, filename), _Entry())
    if not entry.parsed:
        entry.parsed = True
        try:
            parser = Parser(text, filename)
        except Exception as error:  # noqa: BLE001 - shared, then re-raised
            entry.lex_error = entry.parse_error = error
        else:
            if entry.tokens is None:
                entry.tokens = parser.toks
            try:
                entry.program = parser.parse()
            except Exception as error:  # noqa: BLE001 - shared, then re-raised
                entry.parse_error = error
    if entry.parse_error is not None:
        raise entry.parse_error
    return entry.program


def tokens(text: str, filename: str) -> list:
    """`lex(text, filename)`, for a reader that does not mutate the tokens.
    Raises what that lex raises."""
    from . import lexer  # noqa: PLC0415 - looked up per call, as `lex` is

    memo = _MEMO.get()
    if memo is None:
        return lexer.lex(text, filename)
    entry = memo.setdefault((text, filename), _Entry())
    if entry.tokens is None and entry.lex_error is None:
        try:
            entry.tokens = lexer.lex(text, filename)
        except Exception as error:  # noqa: BLE001 - shared, then re-raised
            entry.lex_error = error
    if entry.lex_error is not None:
        raise entry.lex_error
    return entry.tokens

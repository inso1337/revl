"""A text document and the position math the protocol speaks.

The LSP addresses text by `(line, character)` — both zero-based, lines split
on `\n`, characters counted in UTF-16 code units by the spec. revl source is
ASCII-dominant and the compiler reasons in one-based lines, so this module is
the single place the two coordinate systems meet: nothing else in the package
does off-by-one arithmetic.

For slice 1 characters are counted as Python code points, not UTF-16 units.
Every position a revl program actually reaches (identifiers, keywords) is in
the ASCII plane where the two agree; the seam is documented rather than hidden
so a later slice can widen it without hunting for the assumption.
"""

from __future__ import annotations

from dataclasses import dataclass

# identifier characters, matching the lexer's word class closely enough to pick
# the symbol under a cursor
_WORD = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


@dataclass
class Position:
    """A zero-based LSP position."""
    line: int
    character: int


def lines_of(text: str) -> list[str]:
    """The document split into lines, newline stripped. A trailing newline
    does not add an empty final line, matching how editors count."""
    return text.split("\n")


def line_text(text: str, line: int) -> str:
    """The source of one zero-based line, or `''` past the end."""
    rows = lines_of(text)
    if 0 <= line < len(rows):
        return rows[line]
    return ""


def word_at(text: str, position: Position) -> tuple[str, int, int] | None:
    """The identifier under `position` as `(word, start_char, end_char)`.

    Returns None when the cursor is not on an identifier character, so a
    hover over punctuation or whitespace resolves to nothing rather than to
    a neighbouring token.
    """
    row = line_text(text, position.line)
    col = position.character
    if col < 0 or col > len(row):
        return None
    # a cursor sitting just past the last character of a word (col == end)
    # still refers to that word, so probe the character to the left too
    if col == len(row) or row[col] not in _WORD:
        if col == 0 or row[col - 1] not in _WORD:
            return None
        col -= 1
    start = col
    while start > 0 and row[start - 1] in _WORD:
        start -= 1
    end = col
    while end < len(row) and row[end] in _WORD:
        end += 1
    return row[start:end], start, end


def find_symbol_column(text: str, line: int, name: str) -> int | None:
    """The zero-based column of `name` as a whole word on a one-based source
    line, or None. Used to tighten a diagnostic range onto the token the
    compiler named when it carries no column of its own."""
    if line < 1:
        return None
    row = line_text(text, line - 1)
    for col in _whole_word_spans(row, name):
        return col
    return None


def _whole_word_spans(row: str, name: str):
    if not name:
        return
    start = 0
    while True:
        idx = row.find(name, start)
        if idx < 0:
            return
        before = idx == 0 or row[idx - 1] not in _WORD
        after = idx + len(name) >= len(row) or row[idx + len(name)] not in _WORD
        if before and after:
            yield idx
        start = idx + len(name)

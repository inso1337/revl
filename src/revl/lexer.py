"""Lexer for revl v0."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RevlError

KEYWORDS = {
    "service", "component", "requires", "provides", "config", "let",
    "effect", "undo", "emit", "emission", "provide", "fn", "return",
    "true", "false", "null",
    # reserved for later tiers
    "extern", "acquire", "pure", "compensate", "await", "verified", "commutative",
}

SYMBOLS = {"{", "}", "(", ")", "[", "]", ",", ":", "=", "."}


@dataclass
class Token:
    kind: str          # 'ident' | 'kw' | 'int' | 'string' | 'arrow' | symbol | 'eof'
    value: object
    line: int


def lex(source: str, filename: str) -> list[Token]:
    tokens: list[Token] = []
    i, line, n = 0, 1, len(source)
    while i < n:
        c = source[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif source.startswith("//", i):
            while i < n and source[i] != "\n":
                i += 1
        elif source.startswith("->", i):
            tokens.append(Token("arrow", "->", line))
            i += 2
        elif c == '"':
            i, parts = _lex_string(source, i + 1, line, filename)
            tokens.append(Token("string", parts, line))
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            word = source[i:j]
            tokens.append(Token("kw" if word in KEYWORDS else "ident", word, line))
            i = j
        elif c.isdigit():
            j = i
            while j < n and source[j].isdigit():
                j += 1
            tokens.append(Token("int", int(source[i:j]), line))
            i = j
        elif c in SYMBOLS:
            tokens.append(Token(c, c, line))
            i += 1
        else:
            raise RevlError(filename, line, f"unexpected character {c!r}")
    tokens.append(Token("eof", None, line))
    return tokens


def _lex_string(source: str, i: int, line: int, filename: str):
    """String with `$ident` interpolation; `$$` is a literal dollar.

    Returns (index-after-closing-quote, parts) where parts is a list of
    ("text", str) and ("var", name) segments.
    """
    parts: list[tuple[str, str]] = []
    buf = []
    n = len(source)
    while i < n:
        c = source[i]
        if c == '"':
            if buf:
                parts.append(("text", "".join(buf)))
            return i + 1, parts
        if c == "\n":
            raise RevlError(filename, line, "unterminated string literal")
        if c == "$":
            if i + 1 < n and source[i + 1] == "$":
                buf.append("$")
                i += 2
                continue
            j = i + 1
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            name = source[i + 1 : j]
            if not name:
                raise RevlError(filename, line, "`$` in a string must be followed by a name (or doubled as `$$`)")
            if buf:
                parts.append(("text", "".join(buf)))
                buf = []
            parts.append(("var", name))
            i = j
            continue
        buf.append(c)
        i += 1
    raise RevlError(filename, line, "unterminated string literal")

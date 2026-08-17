"""Lexer for revl v0."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RevlError

KEYWORDS = {
    "service", "component", "requires", "provides", "config", "let",
    "effect", "undo", "emit", "emission", "provide", "fn", "return",
    "true", "false", "null",
    # v2: realms & interception
    "isolate", "intercept", "realm", "in", "with",
    # v2.0 full-language (docs/syntax-2.0.md)
    "type", "use", "pub", "var", "while", "for", "of", "if", "else",
    "match", "test", "assert", "async", "as",
    # reserved for later tiers
    "extern", "acquire", "pure", "compensate", "await", "verified", "commutative",
}

SYMBOLS = {"{", "}", "(", ")", "[", "]", ",", ":", "=", "."}

# Multi-character operators, longest first so `===` lexes before `==`.
OPERATORS = ("===", "!==", "=>", "?.", "??", "<=", ">=", "==", "!=", "&&", "||", "->")

# Single-character operator tokens (checked after OPERATORS).
SINGLE_OPERATORS = "+-*/%<>!?;|@"


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
        elif any(source.startswith(op, i) for op in OPERATORS):
            for op in OPERATORS:
                if source.startswith(op, i):
                    tokens.append(Token("arrow" if op == "->" else op, op, line))
                    i += len(op)
                    break
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
        elif c == "@" and i + 1 < n and (source[i + 1].isalpha() or source[i + 1] == "_"):
            # Host block: `@backend { <verbatim, brace-balanced> }`.
            # The body is host text, not revl, so it is consumed here by
            # scanning balanced braces rather than tokenizing the contents.
            j = i + 1
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            backend = source[i + 1:j]
            k = j
            while k < n and source[k].isspace():
                k += 1
            if k < n and source[k] == "{":
                depth = 0
                p = k
                while p < n:
                    if source[p] == "{":
                        depth += 1
                    elif source[p] == "}":
                        depth -= 1
                        if depth == 0:
                            body = source[k + 1:p]
                            tokens.append(Token("hostbody", (backend, body), line))
                            line += source[i:p + 1].count("\n")
                            i = p + 1
                            break
                    p += 1
                else:
                    raise RevlError(filename, line, f"unterminated @{backend} host body")
            else:
                tokens.append(Token("@", "@", line))
                i += 1
        elif c in SYMBOLS or c in SINGLE_OPERATORS:
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

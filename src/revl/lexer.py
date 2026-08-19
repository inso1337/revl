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
    # instance-parametric components (docs/design-v2-instances.md)
    "spawn",
    # v2.0 full-language (docs/syntax-2.0.md)
    "type", "use", "pub", "var", "while", "for", "of", "if", "else",
    "match", "test", "assert", "async", "as", "fail",
    # typed holes (docs/holes.md): a placeholder with a type and no body
    "hole",
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
    kind: str          # 'ident' | 'kw' | 'int' | 'float' | 'string' | 'template' | 'arrow' | symbol | 'eof'
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
            i, value = _lex_string(source, i + 1, line, filename)
            tokens.append(Token("string", value, line))
        elif c == '`':
            i, parts, line = _lex_template(source, i + 1, line, filename)
            tokens.append(Token("template", parts, line))
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
            # A float needs a fraction or an exponent. `.` only starts one when
            # a digit follows, so `7.div_trunc(2)` stays an Int and a method
            # call; `1e10` only lexes as a float when real digits follow the
            # exponent marker, so `1e` is an Int beside an ident, not an error.
            is_float = False
            if j < n and source[j] == "." and j + 1 < n and source[j + 1].isdigit():
                j += 1
                while j < n and source[j].isdigit():
                    j += 1
                is_float = True
            if j < n and source[j] in "eE":
                k = j + 1
                if k < n and source[k] in "+-":
                    k += 1
                if k < n and source[k].isdigit():
                    while k < n and source[k].isdigit():
                        k += 1
                    j = k
                    is_float = True
            text = source[i:j]
            if is_float:
                tokens.append(Token("float", float(text), line))
            else:
                tokens.append(Token("int", int(text), line))
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
    """Plain double-quoted string; `$` is an ordinary literal character —
    except that `$identifier` is rejected: it was interpolation in 1.x, and
    letting it silently become a literal is exactly the uncanny-valley trap
    syntax-2.0 warns about (§9).

    Returns (index-after-closing-quote, text).
    """
    import re as _re

    buf: list[str] = []
    n = len(source)
    while i < n:
        c = source[i]
        if c == '"':
            text = "".join(buf)
            # both legacy forms had a 1.x meaning that a 2.0 plain string
            # silently changes: `$ident` was interpolation, `$$` was the
            # escape for one literal dollar
            stale = _re.search(r"\$\$|\$[A-Za-z_][A-Za-z0-9_]*", text)
            if stale:
                raise RevlError(
                    filename, line,
                    f"`{stale.group(0)}` in a plain string — this was "
                    f"{'an escaped dollar' if stale.group(0) == '$$' else 'interpolation'} "
                    f"in 1.x and would silently change meaning",
                    hint="run `revl fmt --migrate` to convert to a template literal, "
                         "or write a backtick template: interpolation is `${name}`, "
                         "and a literal dollar needs no escape there (§9)",
                )
            return i + 1, text
        if c == "\n":
            raise RevlError(filename, line, "unterminated string literal")
        buf.append(c)
        i += 1
    raise RevlError(filename, line, "unterminated string literal")


def _lex_template(source: str, i: int, line: int, filename: str):
    """Backtick template with `${expr}` interpolation; bare `$` is literal.

    Returns (index-after-closing-backtick, parts, line) where parts is a list
    of ("text", str) and ("expr", raw_source) segments. The `${...}` body is
    captured as raw source with balanced braces; the parser re-parses it into
    a full expression (§3.2).
    """
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    n = len(source)
    start_line = line
    while i < n:
        c = source[i]
        if c == '`':
            if buf:
                parts.append(("text", "".join(buf)))
            return i + 1, parts, line
        if c == "\n":
            buf.append(c)
            line += 1
            i += 1
            continue
        if c == "$" and i + 1 < n and source[i + 1] == "{":
            # capture the interpolated expression as raw source, balancing
            # nested braces (record literals, etc.)
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if source[j] == "{":
                    depth += 1
                elif source[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif source[j] == "\n":
                    line += 1
                j += 1
            if depth != 0:
                raise RevlError(filename, line, "unterminated `${` interpolation")
            inner = source[i + 2:j].strip()
            if not inner:
                raise RevlError(filename, line, "empty `${}` interpolation")
            if buf:
                parts.append(("text", "".join(buf)))
                buf = []
            parts.append(("expr", inner))
            i = j + 1
            continue
        buf.append(c)
        i += 1
    raise RevlError(filename, start_line, "unterminated template literal")

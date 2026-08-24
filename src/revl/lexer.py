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
    # delivery semantics (docs/delivery-semantics.md, roadmap item 44): an
    # `idempotent` emission may be safely re-delivered, so the runtime earns
    # the right to auto-retry it on transient failure.
    "idempotent",
}

SYMBOLS = {"{", "}", "(", ")", "[", "]", ",", ":", "=", "."}

# Multi-character operators, longest first so `===` lexes before `==`.
OPERATORS = ("===", "!==", "=>", "?.", "??", "<=", ">=", "==", "!=", "&&", "||", "->")

# Single-character operator tokens (checked after OPERATORS).
SINGLE_OPERATORS = "+-*/%<>!?;|@"


# --- string/comment-aware raw-brace balancing --------------------------------
#
# Two constructs capture a run of raw source bounded by balanced braces: the
# `${ ... }` of a backtick template and the `{ ... }` body of an `@backend`
# host block. Counting `{`/`}` naively miscounts a brace that sits inside a
# string, char literal, or block comment — `` `v=${m.lookup("}")}` `` would
# truncate at the `}` in the string, and `@java { var s = "}" }` would close
# early. The balancer below skips over such spans so only *structural* braces
# move the depth.
#
# LINE comments are deliberately NOT skipped inside host bodies. revl uses `}`
# as the host-body terminator regardless of host-language comment rules, and
# the importers (openapi/wit) emit single-line placeholder bodies whose closing
# brace sits right after a line comment on the same line — `@ts { // ... here }`,
# `@py { # ... }`, `@wasm { ;; ... }`. Treating those as comment-to-EOL would
# swallow the terminator. Only bounded spans (strings, char/rune literals, and
# block comments), where an interior `}` is unambiguous, are skipped. A revl
# `${ ... }` interpolation is real revl source with no such generator, so it
# does honour `//` line comments there.


class _Trivia:
    """Which string/comment forms a raw-brace scan must skip for a context.

    `strings` maps an opening quote char to how it closes: "escape" (a `\\`
    escapes the next char, single line), "raw" (no escapes, may span lines —
    Go raw strings, backtick templates), or "char" (a char/rune literal that
    may also be a Rust lifetime, so it is skipped only when it *looks* like a
    closed literal). `block_comments` are bounded `(open, close)` pairs;
    `triples` are triple-quote string openers (Python). `line_comments` apply
    only to revl interpolations — see the module note above on why host bodies
    leave them out.
    """

    __slots__ = ("strings", "line_comments", "block_comments", "triples")

    def __init__(self, strings, line_comments=(), block_comments=(), triples=()):
        self.strings = strings
        self.line_comments = line_comments
        self.block_comments = block_comments
        self.triples = triples  # triple-quote openers, e.g. '"""', "'''"


# A `${ ... }` interpolation is revl source: `"` strings, `` ` `` nested
# templates, and `//` line comments can each carry an unstructural brace.
_REVL_TRIVIA = _Trivia(
    strings={'"': "escape", "`": "raw"},
    line_comments=("//",),
)

# Per-backend host-language trivia (strings + block comments only; see the note
# above on line comments). C-family share `/* */`; Python has no block comment
# but triple-quoted strings; wasm text uses `(; ;)`. `'` is a char/rune literal
# in Go/Java/Rust (Rust lifetimes handled by "char" being best-effort) but a
# full string in Python/TypeScript.
_C_FAMILY = _Trivia(
    strings={'"': "escape", "'": "char", "`": "raw"},
    block_comments=(("/*", "*/"),),
)
_TS_TRIVIA = _Trivia(
    strings={'"': "escape", "'": "escape", "`": "raw"},
    block_comments=(("/*", "*/"),),
)
_PY_TRIVIA = _Trivia(
    strings={'"': "escape", "'": "escape"},
    triples=('"""', "'''"),
)
_HOST_TRIVIA = {
    # keyed on the `@backend` name as written in source (run.py KNOWN_BACKENDS
    # plus the long aliases the docs use)
    "rust": _C_FAMILY,
    "go": _C_FAMILY,
    "java": _C_FAMILY,
    "ts": _TS_TRIVIA,
    "typescript": _TS_TRIVIA,
    "node": _TS_TRIVIA,
    "js": _TS_TRIVIA,
    "py": _PY_TRIVIA,
    "python": _PY_TRIVIA,
    "wasm": _Trivia(
        strings={'"': "escape"},
        block_comments=(("(;", ";)"),),
    ),
}


def _char_literal_end(source: str, i: int, n: int) -> int | None:
    """`source[i]` is `'`. Return the index just past a well-formed char/rune
    literal (`'x'`, `'\\n'`, `'\\u{1F600}'`), or None when it does not look like
    one — e.g. a Rust lifetime `'a` — so `'` is then treated as ordinary text.
    """
    j = i + 1
    if j >= n or source[j] == "\n":
        return None
    if source[j] == "\\":
        j += 2
        while j < n and source[j] not in "'\n":
            j += 1
        return j + 1 if j < n and source[j] == "'" else None
    # a single non-quote char immediately followed by the closing quote
    if source[j] != "'" and j + 1 < n and source[j + 1] == "'":
        return j + 2
    return None


def _skip_trivia(source: str, i: int, n: int, tv: _Trivia) -> int:
    """If a string/char literal or comment begins at `source[i]`, return the
    index just past it; otherwise return `i` unchanged. An unterminated literal
    or comment consumes to end of input (the caller then reports the brace
    imbalance). Braces inside the skipped span never affect brace depth.
    """
    c = source[i]
    for opener, closer in tv.block_comments:
        if source.startswith(opener, i):
            end = source.find(closer, i + len(opener))
            return n if end < 0 else end + len(closer)
    for opener in tv.line_comments:
        if source.startswith(opener, i):
            j = source.find("\n", i)
            return n if j < 0 else j
    for triple in tv.triples:
        if source.startswith(triple, i):
            j = i + len(triple)
            while j < n and not source.startswith(triple, j):
                if source[j] == "\\":
                    j += 1
                j += 1
            return j + len(triple) if j < n else n
    mode = tv.strings.get(c)
    if mode == "char":
        end = _char_literal_end(source, i, n)
        return end if end is not None else i
    if mode == "raw":
        j = source.find(c, i + 1)
        return n if j < 0 else j + 1
    if mode == "escape":
        j = i + 1
        while j < n:
            if source[j] == "\\":
                j += 2
                continue
            if source[j] == c or source[j] == "\n":
                return j + (source[j] == c)
            j += 1
        return n
    return i


def _match_brace(source: str, open_idx: int, tv: _Trivia) -> int | None:
    """`source[open_idx]` is the opening `{`. Return the index of the matching
    `}`, counting only structural braces (those outside strings/comments per
    `tv`), or None if the input ends first.
    """
    n = len(source)
    depth = 0
    i = open_idx
    while i < n:
        c = source[i]
        if c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
            i += 1
        else:
            j = _skip_trivia(source, i, n, tv)
            i = j if j > i else i + 1
    return None


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
                # Balance braces string/comment-aware so a `}` inside a host
                # string or comment (`"}"`, `// }`) does not close early.
                tv = _HOST_TRIVIA.get(backend, _C_FAMILY)
                p = _match_brace(source, k, tv)
                if p is None:
                    raise RevlError(filename, line, f"unterminated @{backend} host body")
                body = source[k + 1:p]
                tokens.append(Token("hostbody", (backend, body), line))
                line += source[i:p + 1].count("\n")
                i = p + 1
            else:
                tokens.append(Token("@", "@", line))
                i += 1
        elif c == ";":
            # `;` lexes fine (it sits in SINGLE_OPERATORS for host bodies),
            # but the grammar never uses it — and agents coming from
            # C-family languages reach for it as a statement separator
            raise RevlError(
                filename, line,
                "unexpected ';'",
                hint="statements are newline-separated — `;` is not a "
                     "statement separator in revl; put each statement on "
                     "its own line")
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
            # nested braces (record literals, etc.). Balancing is string- and
            # comment-aware so a `}` inside a string — `${m.lookup("}")}` — or
            # after a `//` does not close the interpolation early.
            j = _match_brace(source, i + 1, _REVL_TRIVIA)
            if j is None:
                raise RevlError(filename, line, "unterminated `${` interpolation")
            line += source[i:j].count("\n")
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

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
    # verified state hand-off on hot-swap (roadmap item 53, the `code_change`
    # gap): a stateful provider's live state crosses to its successor,
    # type-checked at admission (docs/state-handoff.md).
    "handoff",
    # instance-parametric components (docs/design-v2-instances.md)
    "spawn",
    # time as a coeffect (docs/time-coeffect.md, roadmap item 57): `every`/
    # `after` acquire a revertible schedule whose inverse is cancellation.
    "every", "after",
    # v2.0 full-language (docs/syntax-2.0.md)
    "type", "use", "pub", "var", "while", "for", "of", "if", "else",
    "match", "test", "assert", "async", "as", "fail",
    # loop control flow (docs/design/379-break-continue.md, roadmap item 379):
    # bare `break`/`continue`, valid only inside a `while`/`for` body.
    "break", "continue",
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
# `<<`/`>>` are the Int32 bitwise shifts (docs/arithmetic.md, item 366); they
# precede `<`/`>` so a doubled angle lexes as one shift token, and revl spells
# generics with `[]` (never `<>`), so there is no `List<T>` ambiguity for `>>`.
OPERATORS = ("===", "!==", "=>", "?.", "??", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||", "->")

# Single-character operator tokens (checked after OPERATORS). `&`, `^` and `~`
# are the Int32 bitwise AND/XOR/NOT (item 366); `&&`/`||` in OPERATORS above are
# matched first, so a lone `&` still reaches here. `|` (already present) doubles
# as bitwise OR and the variant/record-update separator — the parser tells them
# apart by grammar position (docs/arithmetic.md).
SINGLE_OPERATORS = "+-*/%<>!?;|@&^~"


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
        elif source.startswith('"""', i):
            # Triple-quoted verbatim string: raw text (newlines included) up to
            # the next `"""`. Distinct from the single-`"` form only in that it
            # may span lines; both yield the same `string` token kind.
            start_line = line
            i, value, line = _lex_triple_string(source, i + 3, line, filename)
            tokens.append(Token("string", value, start_line))
        elif c == '"':
            i, value = _lex_string(source, i + 1, line, filename)
            tokens.append(Token("string", value, line))
        elif c == "'":
            # Single-quoted string (item 382). revl has no char type, so `'a'`
            # / `'hello'` lex as a `Str`, identical in token kind and escape
            # handling to `"..."` — the same dual-spelling muscle-memory ergonomic
            # revl already grants with `==`/`===`. Only `\'` and `\\` are escapes.
            i, value = _lex_string(source, i + 1, line, filename, quote="'")
            tokens.append(Token("string", value, line))
        elif c == '`':
            i, parts, line = _lex_template(source, i + 1, line, filename)
            tokens.append(Token("template", parts, line))
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            word = source[i:j]
            # Item 380: a leading `f"..."` (a Python f-string by muscle memory)
            # is not revl syntax — `f` lexes as an identifier and `"..."` as a
            # separate string, so `return f"hi {name}"` silently parses as
            # `return f` (the identifier) plus a dead string statement, and
            # with an `f` in scope it type-checks unchecked. The `f`/`F` glued
            # directly to a `"` (no space) is unambiguous — revl has no
            # construct where an identifier abuts a string literal — so redirect
            # to a backtick template rather than let it miscompile.
            if word in ("f", "F") and j < n and source[j] == '"':
                raise RevlError(
                    filename, line,
                    f"`{word}\"...\"` is not a revl string — revl has no "
                    "f-string prefix",
                    hint="interpolation needs a backtick template: write "
                         "`` `hi ${name}` `` (docs/strings.md)",
                )
            tokens.append(Token("kw" if word in KEYWORDS else "ident", word, line))
            i = j
        elif c.isdigit():
            i, tok = _lex_number(source, i, line, filename)
            tokens.append(tok)
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
        elif c in SYMBOLS or c in SINGLE_OPERATORS:
            tokens.append(Token(c, c, line))
            i += 1
        elif c == "#":
            # item 384: `#` is the Python/shell line-comment lead-in — revl
            # comments are `//` (line 242). Redirect instead of the opaque
            # `unexpected character '#'`.
            raise RevlError(
                filename, line,
                "revl has no `#` comments",
                hint="a line comment is `// ...` (syntax-2.0 §3.2)",
            )
        else:
            raise RevlError(filename, line, f"unexpected character {c!r}")
    tokens.append(Token("eof", None, line))
    return tokens


# Non-decimal integer prefixes (item 381): the letter after a leading `0`
# selects the radix. The prefix letter and the a-f hex digits may be either
# case, matching Python/JS so a model's `0XFF` or `0xff` both lex.
_RADIX_PREFIX = {
    "x": (16, "hexadecimal", "0123456789abcdefABCDEF"),
    "b": (2, "binary", "01"),
    "o": (8, "octal", "01234567"),
}


def _scan_grouped_digits(source, i, line, filename, valid, what):
    """Scan a run of `valid` digits with `_` group separators (item 381).

    `_` is a separator only: it may not lead, trail, or double, and there must
    be at least one digit. Returns (index-after-run, digits-without-separators).
    """
    n = len(source)
    buf: list[str] = []
    prev_underscore = False
    while i < n:
        c = source[i]
        if c == "_":
            if not buf or prev_underscore:
                raise RevlError(
                    filename, line,
                    f"'_' in {what} literal must appear between digits",
                )
            prev_underscore = True
            i += 1
        elif c in valid:
            buf.append(c)
            prev_underscore = False
            i += 1
        else:
            break
    if not buf:
        raise RevlError(filename, line, f"{what} literal requires at least one digit")
    if prev_underscore:
        raise RevlError(
            filename, line,
            f"'_' in {what} literal must appear between digits",
        )
    return i, "".join(buf)


def _lex_number(source: str, i: int, line: int, filename: str):
    """Number literal: decimal int/float plus the item-381 additions —
    `0x`/`0b`/`0o` non-decimal integers and `_` digit-group separators.

    Decimal behavior is byte-identical to before for any input without a `_`:
    a float needs a fraction or exponent; `.` only starts a fraction when a
    digit follows (so `7.foo()` stays an int + method call), and `1e` with no
    exponent digits stays an int beside an ident. Non-decimal literals are
    always ints (no fraction/exponent). Returns (index-after-number, Token).
    """
    n = len(source)
    # Non-decimal: a leading `0` followed by a radix letter.
    if source[i] == "0" and i + 1 < n and source[i + 1] in "xXbBoO":
        base, what, valid = _RADIX_PREFIX[source[i + 1].lower()]
        i, digits = _scan_grouped_digits(source, i + 2, line, filename, valid, what)
        return i, Token("int", int(digits, base), line)

    # Decimal integer part (the caller guarantees source[i] is a digit).
    i, int_digits = _scan_grouped_digits(source, i, line, filename, "0123456789", "number")
    num = int_digits
    is_float = False
    if i < n and source[i] == "." and i + 1 < n and source[i + 1].isdigit():
        i, frac = _scan_grouped_digits(source, i + 1, line, filename, "0123456789", "number")
        num += "." + frac
        is_float = True
    if i < n and source[i] in "eE":
        k = i + 1
        sign = ""
        if k < n and source[k] in "+-":
            sign = source[k]
            k += 1
        if k < n and source[k].isdigit():
            i, exp = _scan_grouped_digits(source, k, line, filename, "0123456789", "number")
            num += "e" + sign + exp
            is_float = True
    if is_float:
        return i, Token("float", float(num), line)
    return i, Token("int", int(num), line)


def _lex_string(source: str, i: int, line: int, filename: str, quote: str = '"'):
    """Plain quoted string (`quote` is the closing delimiter, `"` or `'`).

    The escape set is deliberately minimal: `\\"` yields a literal `"` and
    `\\\\` a literal `\\`, so a string may contain either (item 183). Every
    other backslash sequence is preserved verbatim — `\\n` is a literal
    backslash and an `n`, not a newline — matching the no-escape semantics of
    the triple-quoted form (docs/strings.md).

    `$` is an ordinary literal character — including the shapes `$identifier`
    and `$$` (item 203). In 2.0 interpolation lives ONLY in backtick templates
    (`` `${name}` ``, `_lex_template`), so a bare `$` in a plain `"..."` carries
    no special meaning and `"call $int_add"` / `"$x"` lex as literal text. The
    legacy 1.x reading (`$name` = interpolation, `$$` = an escaped dollar) is
    gone; `revl fmt --migrate` still rewrites a legacy `"$name"` to a template,
    now admitted by the migrate-specific gate policy (a deliberate semantic
    upgrade rather than an equivalence-preserving reformat) instead of relying
    on this lexer to reject the input — see `formatter.ir_equivalent`
    (token_preserving=False), `revl.fmt`, and tests/test_fmt.py.

    Returns (index-after-closing-quote, text).
    """
    buf: list[str] = []
    n = len(source)
    while i < n:
        c = source[i]
        if c == "\\" and i + 1 < n and source[i + 1] in (quote, "\\"):
            # `\<quote>` and `\\` are the only escapes: emit the escaped
            # character and consume both. A `\` before anything else is a literal
            # backslash (so `\n` stays two characters — no escape processing).
            buf.append(source[i + 1])
            i += 2
            continue
        if c == quote:
            text = "".join(buf)
            if quote == '"':
                _reject_dollar_interpolation(text, line, filename)
            return i + 1, text
        if c == "\n":
            raise RevlError(filename, line, "unterminated string literal")
        buf.append(c)
        i += 1
    raise RevlError(filename, line, "unterminated string literal")


def _reject_dollar_interpolation(text: str, line: int, filename: str) -> None:
    """Item 380: a `${...}` inside a plain `"..."` string is a silent-wrong
    interpolation — 2.0 interpolation lives ONLY in backtick templates, so the
    `${...}` is emitted as LITERAL text (`"hi ${name}"` compiled clean and
    produced the literal `${name}`). Redirect to a backtick template instead of
    silently accepting it (§0/§10 exclusion-diagnostic philosophy).

    Precise, not eager: fires only on a *complete* `${...}` shape (a closing
    brace after the `${`), and never when the string carries a backtick — a
    plain string that contains a backtick is deliberately quoting template or
    shell source as DATA (the selfhost lexer/parser fixtures `"`hi ${name}!`"`,
    and the ts emitter's fragment `"${"` which has no closing brace), not a
    mistaken interpolation."""
    at = text.find("${")
    if at == -1 or "`" in text or "}" not in text[at + 2:]:
        return
    raise RevlError(
        filename, line,
        'a plain `"..."` string does not interpolate — the `${...}` is emitted '
        "as literal text",
        hint="interpolation needs a backtick template: write "
             "`` `hi ${name}` `` (docs/strings.md)",
    )


def _lex_triple_string(source: str, i: int, line: int, filename: str):
    """Triple-quoted verbatim string `\"\"\" ... \"\"\"`.

    The body is the *literal* characters between the delimiters, newlines and
    all — there is no escape processing and no `${...}` interpolation, matching
    the no-escape semantics of the single-`\"` form (`\"a\\nb\"` is a literal
    backslash-`n`). Its one added power is that the body may span lines, so an
    agent can author a multi-line `.rvl` literal without concatenation.

    Only `\"\"\"` closes the string; a lone `\"` or `\"\"` inside the body is
    ordinary text. A single newline immediately after the opening `\"\"\"` is
    stripped, so a literal that opens on its own line does not begin with a
    blank line (Python/Swift/Kotlin all do this). `\"\"\"\"\"\"` is the empty
    string.

    Returns (index-after-closing-delimiter, text, line).
    """
    n = len(source)
    # Strip a single leading newline right after the opening delimiter.
    if i < n and source[i] == "\n":
        line += 1
        i += 1
    start_line = line
    buf: list[str] = []
    while i < n:
        if source.startswith('"""', i):
            return i + 3, "".join(buf), line
        c = source[i]
        if c == "\n":
            line += 1
        buf.append(c)
        i += 1
    raise RevlError(
        filename, start_line, "unterminated triple-quoted string literal")


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

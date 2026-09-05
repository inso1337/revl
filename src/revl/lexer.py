"""Lexer for revl v0."""

from __future__ import annotations

import unicodedata
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
    # Stream[T] reactive types (docs/design/130-stream-reactive-types.md, item
    # 130): `subscribe <stream> undo <close>` acquires a single-consumer
    # subscription, an `effect`-position acquisition that registers a bracket.
    # `next`/`close` stay ordinary method calls (they need no keyword).
    "subscribe",
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


# ---------------------------------------------------------------------------
# Identifier alphabet.
#
# revl identifiers are ASCII: `[A-Za-z_][A-Za-z0-9_]*`. This is a FRONTEND rule
# (enforced here, in the lexer) rather than a per-emitter one, because the
# alternative is a soundness hole rather than a style question.
#
# `str.isalpha()`/`str.isalnum()` are the full Unicode classes, so a permissive
# lexer admits names that the checker sees as DISTINCT but a host tier sees as
# the SAME name. CPython NFKC-normalizes identifiers at parse time (PEP 3131),
# so `ｓend` (U+FF53 FULLWIDTH LATIN SMALL LETTER S) and `send` are one Python
# function: a plain `fn ｓend` would capture an `extern emission fn send`'s
# binding and smuggle an irreversible host call past the emission checker, which
# had already accepted the program because to IT the two names differ. The same
# merge happens for the ligature `ﬁlter`/`filter` and for a superscript
# continuation (`x²` normalizes to `x2`). The tiers also already DISAGREE about
# admission: the rust and java emitters match `^[A-Za-z_][A-Za-z0-9_]*$` and the
# typescript emitter `^[A-Za-z_$][A-Za-z0-9_$]*$`, so the very names python
# silently merges are hard errors there.
#
# ASCII restores the invariant by construction, on two counts:
#   * every ASCII identifier is an NFKC fixed point, so two identifiers that are
#     distinct to the checker stay distinct on the python tier — there is no
#     normalized-uniqueness check to keep in sync, because normalization is the
#     identity on the admitted set; and
#   * `[A-Za-z_][A-Za-z0-9_]*` is a subset of what EVERY emitter accepts, so an
#     identifier admitted by the frontend renders on every tier.
#
# The cost is real: a non-English identifier (`café`, `größe`, `имя`) is refused,
# even though it is NFKC-stable and would round-trip through python fine. That
# cost is already being paid today — the rust and java emitters reject those
# names outright, so such a program is not portable now; this makes the existing
# restriction honest and uniform instead of a per-tier surprise. Widening later
# to a UAX-31 profile (XID_Start/XID_Continue plus an NFKC-stability check,
# which is what keeps `ｓend`/`ﬁlter`/`x²` out) is a compatible extension once
# the ASCII-only emitters gain a mangling scheme for non-ASCII names.
#
# Nothing else narrows: string literals, templates, comments and `@host` bodies
# stay full Unicode. Only NAMES are constrained.


_IDENT_START = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENT_CONT = frozenset(_IDENT_START | set("0123456789"))
_ASCII_DIGIT = frozenset("0123456789")


def _ident_char(c: str) -> bool:
    """True for any character a Unicode-permissive lexer would take as
    identifier text: a letter or digit in any script, `_`, or a combining /
    connector mark that would attach to one."""
    return c.isalnum() or c == "_" or unicodedata.category(c) in ("Mn", "Mc", "Pc")


def _reject_non_ascii_ident(source: str, i: int, line: int, filename: str):
    """Refuse the identifier-shaped run of text containing `source[i]`.

    Reached when a non-ASCII identifier character is seen, either leading a word
    or glued to an ASCII run (`x²`). The whole run is reported, not the single
    offending character, so the diagnostic names what the author wrote.
    """
    n = len(source)
    start = i
    while start > 0 and _ident_char(source[start - 1]):
        start -= 1
    end = i
    while end < n and _ident_char(source[end]):
        end += 1
    word = source[start:end]
    bad = next(c for c in word if not c.isascii())
    try:
        named = unicodedata.name(bad)
    except ValueError:                                    # pragma: no cover
        named = "unnamed"
    where = f"U+{ord(bad):04X} {named}"
    normalized = unicodedata.normalize("NFKC", word)
    if normalized != word:
        hint = (
            f"revl identifiers are ASCII `[A-Za-z_][A-Za-z0-9_]*`. `{word}` is "
            f"not a distinct name on every tier: python normalizes identifiers "
            f"to NFKC (PEP 3131), so it is the SAME function as `{normalized}` "
            f"there, while the rust, java and typescript emitters refuse it. "
            f"Write `{normalized}` if that is the name you meant, or pick "
            f"another ASCII name; non-ASCII text belongs in a string literal."
        )
    else:
        hint = (
            "revl identifiers are ASCII `[A-Za-z_][A-Za-z0-9_]*` — the rust, "
            "java and typescript emitters reject non-ASCII names, so such a "
            "program would not render on every tier. Transliterate the name; "
            "non-ASCII text belongs in a string literal."
        )
    raise RevlError(
        filename, line,
        f"non-ASCII character in identifier `{word}` ({where})",
        hint=hint,
    )


@dataclass
class Token:
    kind: str          # 'ident' | 'kw' | 'int' | 'float' | 'string' | 'template' | 'arrow' | symbol | 'eof'
    value: object
    line: int


def lex(source: str, filename: str, line_offset: int = 0) -> list[Token]:
    """Tokenize `source`. Lines are one-based and counted from `line_offset`.

    `line_offset` exists for source that is a FRAGMENT of a larger file: the
    body of a `${...}` interpolation is re-lexed on its own, and without an
    offset every token in it — and every diagnostic the checker later raises on
    it — claimed line 1 of the enclosing file (issue #313). Zero, the default,
    is a whole file starting at line 1.
    """
    if line_offset == 0 and source.startswith("\ufeff"):
        source = source[1:]
    for position, character in enumerate(source):
        if 0xD800 <= ord(character) <= 0xDFFF:
            line = line_offset + source.count("\n", 0, position) + 1
            raise RevlError(filename, line, "source contains a lone surrogate")
    tokens: list[Token] = []
    i, line, n = 0, 1 + line_offset, len(source)
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
            # The token's line is where the template OPENS, not where it closes
            # (issue #313). A template spanning lines 2-5 used to report every
            # error against it — a type mismatch on the whole literal, say — at
            # line 5, pointing the reader at the closing backtick instead of at
            # the expression. `"""` strings above already do this correctly.
            start_line = line
            i, parts, line, suspect, interp_lines = _lex_template(
                source, i + 1, line, filename)
            tok = Token("template", parts, start_line)
            # Side-band, for the same reason `stray_backtick` is: the absolute
            # line each `${...}` body starts on, so the parser can re-parse it
            # with a line offset. Token equality and every accepted program lex
            # identically without it.
            tok.interp_lines = interp_lines
            if suspect is not None:
                # Side-band marker for the parser's stray-backtick diagnostic
                # (item 365). Not a Token field, so token equality and every
                # accepted program lex identically; only the error path reads it.
                tok.stray_backtick = suspect
            tokens.append(tok)
        elif c in _IDENT_START:
            j = i
            while j < n and source[j] in _IDENT_CONT:
                j += 1
            if j < n and _ident_char(source[j]):
                # A non-ASCII identifier character glued to an ASCII run — the
                # `x²` case, which NFKC-normalizes to `x2` on the python tier.
                # Refuse the whole word rather than lex a truncated `x` and let
                # the tail surface as some unrelated syntax error.
                _reject_non_ascii_ident(source, j, line, filename)
            word = source[i:j]
            # Item 380: a leading `f"..."` (a Python f-string by muscle memory)
            # is not revl syntax — `f` lexes as an identifier and `"..."` as a
            # separate string, so `return f"hi {name}"` silently parses as
            # `return f` (the identifier) plus a dead string statement, and
            # with an `f` in scope it type-checks unchecked. The `f`/`F` glued
            # directly to a quote (no space) is unambiguous — revl has no
            # construct where an identifier abuts a string literal — so redirect
            # to a backtick template rather than let it miscompile.
            #
            # Item 382 made `'...'` a second, equal spelling of `Str`, so `f'…'`
            # reaches the identical silent mis-parse; both quotes are covered
            # here, and the diagnostic quotes back the spelling that was written.
            if word in ("f", "F") and j < n and source[j] in "\"'":
                q = source[j]
                raise RevlError(
                    filename, line,
                    f"`{word}{q}...{q}` is not a revl string — revl has no "
                    "f-string prefix",
                    hint="interpolation needs a backtick template: write "
                         "`` `hi ${name}` `` (docs/strings.md)",
                )
            tokens.append(Token("kw" if word in KEYWORDS else "ident", word, line))
            i = j
        elif c in _ASCII_DIGIT:
            i, tok = _lex_number(source, i, line, filename)
            tokens.append(tok)
        elif c == "@" and i + 1 < n and source[i + 1] in _IDENT_START:
            # Host block: `@backend { <verbatim, brace-balanced> }`.
            # The body is host text, not revl, so it is consumed here by
            # scanning balanced braces rather than tokenizing the contents.
            j = i + 1
            while j < n and source[j] in _IDENT_CONT:
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
        elif c == "\ufeff":
            raise RevlError(filename, line, "byte-order mark is only allowed at the start of a file")
        elif _ident_char(c):
            # Non-ASCII, but identifier-shaped: a name a Unicode-permissive
            # lexer would have accepted. Refused here, in the frontend, so no
            # tier can admit what another rejects (see the alphabet note above).
            _reject_non_ascii_ident(source, i, line, filename)
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
    # `.isdigit()`/`.isascii()` together: the digit tests below are ASCII-only
    # to match `_scan_grouped_digits`, whose alphabet is `0123456789`. Without
    # the `.isascii()` guard a non-ASCII digit (`²`, `١`) opens a fraction or an
    # exponent that then has no valid digit to scan.
    if i < n and source[i] == "." and i + 1 < n and source[i + 1] in _ASCII_DIGIT:
        i, frac = _scan_grouped_digits(source, i + 1, line, filename, "0123456789", "number")
        num += "." + frac
        is_float = True
    if i < n and source[i] in "eE":
        k = i + 1
        sign = ""
        if k < n and source[k] in "+-":
            sign = source[k]
            k += 1
        if k < n and source[k] in _ASCII_DIGIT:
            i, exp = _scan_grouped_digits(source, k, line, filename, "0123456789", "number")
            num += "e" + sign + exp
            is_float = True
    if is_float:
        return i, Token("float", float(num), line)
    try:
        value = int(num)
    except ValueError as error:
        raise RevlError(filename, line, "Int literal is outside the 64-bit range") from error
    return i, Token("int", value, line)


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
            # Both plain spellings, not just `"`: item 382 made `'...'` an equal
            # spelling of `Str`, so `'hi ${name}'` reaches the same silent-wrong
            # literal-`${name}` outcome the guard exists to catch. The
            # triple-quoted form is deliberately left out — it is the verbatim
            # spelling used to carry template and shell source as DATA, which is
            # the same false positive the backtick carve-out below covers.
            _reject_dollar_interpolation(text, line, filename)
            return i + 1, text
        if c == "\n":
            raise RevlError(filename, line, "unterminated string literal")
        buf.append(c)
        i += 1
    raise RevlError(filename, line, "unterminated string literal")


def _reject_dollar_interpolation(text: str, line: int, filename: str) -> None:
    """Item 380: a `${...}` inside a plain `"..."`/`'...'` string is a silent-wrong
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


def _closing_backtick_is_stray(source: str, body_start: int, close: int) -> bool:
    """Heuristic for item 365: does the backtick at `source[close]` look like a
    STRAY backtick that closed the template early rather than its real end?

    revl backtick templates most often carry a host language (JS/HTML/CSS), and
    a host `//` line comment or `/* … */` block comment can legitimately contain
    a backtick — ``// read the `answer` field``. Since the template has no
    backtick escape, that first embedded backtick closes the template and the
    host tail reparses as revl. We flag the close as suspect when BOTH hold:

    * the closing backtick's own line, within the template body, opens a host
      comment before the backtick — a `//` on the line, or an unclosed `/*`
      anywhere earlier in the body — so the backtick sits *inside* a comment; and
    * host text still trails the backtick on the same physical line, which a
      normally-terminated template almost never leaves.

    Both conditions are about the ERROR shape only: the return value is read
    solely when the surrounding parse has already failed, so a false positive
    can at worst reword a genuine error and can never reject accepted source.
    """
    line_start = source.rfind("\n", body_start, close) + 1
    if line_start < body_start:
        line_start = body_start
    line_before = source[line_start:close]

    # A `//` on the closing line, or an as-yet-unclosed `/*` from earlier in the
    # body, means the backtick is inside a host comment.
    in_line_comment = "//" in line_before
    open_block = _has_open_block_comment(source, body_start, close)
    if not (in_line_comment or open_block):
        return False

    # Live host text must trail the backtick on the same physical line.
    line_end = source.find("\n", close + 1)
    trailing = source[close + 1: line_end if line_end != -1 else len(source)]
    return trailing.strip() != ""


def _has_open_block_comment(source: str, start: int, end: int) -> bool:
    """True when the last `/*` before `end` (at or after `start`) has no closing
    `*/` before `end` — i.e. a host block comment is still open at `end`."""
    last_open = source.rfind("/*", start, end)
    if last_open == -1:
        return False
    return source.find("*/", last_open + 2, end) == -1


def _lex_template(source: str, i: int, line: int, filename: str):
    """Backtick template with `${expr}` interpolation; bare `$` is literal.

    Returns (index-after-closing-backtick, parts, line, suspect, interp_lines)
    where parts is a list of ("text", str) and ("expr", raw_source) segments.
    The `${...}` body is captured as raw source with balanced braces; the parser
    re-parses it into a full expression (§3.2).

    `interp_lines` holds one absolute line per ("expr", ...) part, in order: the
    line the interpolation's first non-space character sits on. The parser feeds
    it to the sub-parser as a line offset so a diagnostic inside `${...}` names
    the line it is actually on rather than line 1 (issue #313).

    `suspect` is `None`, or `(start_line, close_line)` when the closing backtick
    looks like a STRAY backtick that closed the template early — one sitting
    inside a host-language `//` line comment or an open `/*` block comment, with
    live host text still trailing it on the same line (item 365). revl has no
    backtick escape, so a contributor's `` // read the `answer` field `` closes
    the template at the first embedded backtick and its tail reparses as revl
    declarations; the parser turns this flag into a diagnostic that points back
    here instead of naming the unrelated identifier the tail happens to hold.
    The flag never changes what LEXES — it only lets the parser reword an error
    it was already going to raise on the mis-parsed tail.
    """
    parts: list[tuple[str, str]] = []
    interp_lines: list[int] = []
    buf: list[str] = []
    n = len(source)
    start_line = line
    body_start = i
    while i < n:
        c = source[i]
        if c == '`':
            if buf:
                parts.append(("text", "".join(buf)))
            suspect = None
            if _closing_backtick_is_stray(source, body_start, i):
                suspect = (start_line, line)
            return i + 1, parts, line, suspect, interp_lines
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
            dollar_line = line
            j = _match_brace(source, i + 1, _REVL_TRIVIA)
            if j is None:
                raise RevlError(filename, dollar_line,
                                "unterminated `${` interpolation")
            line += source[i:j].count("\n")
            raw_inner = source[i + 2:j]
            inner = raw_inner.strip()
            if not inner:
                raise RevlError(filename, dollar_line, "empty `${}` interpolation")
            if buf:
                parts.append(("text", "".join(buf)))
                buf = []
            parts.append(("expr", inner))
            # `.strip()` above can drop leading newlines — `${\n  nme}` — so the
            # expression starts further down than the `$` does.
            lead = len(raw_inner) - len(raw_inner.lstrip())
            interp_lines.append(dollar_line + raw_inner[:lead].count("\n"))
            i = j + 1
            continue
        buf.append(c)
        i += 1
    raise RevlError(filename, start_line, "unterminated template literal")

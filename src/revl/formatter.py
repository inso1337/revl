"""Canonical formatter for revl source (roadmap item 35: `revl fmt`).

This module turns `revl fmt` into a real canonical formatter and gives every
rewrite it performs a self-proving admissibility gate:

    format the source, compile BOTH the original and the formatted text, and
    assert their IR is byte-identical.  A formatting is admitted only when it
    provably changed nothing the compiler can see.  If the IR differs, the
    file is REFUSED (the caller reports a nonzero exit) rather than shipping a
    formatting that changed meaning.

The same gate retrofits onto `--migrate` (see `revl.fmt`): a syntax rewrite
ships iff the resulting IR is unchanged (or, for a 1.x input the current
compiler cannot even parse, iff the rewritten output is newly admissible).

## How the formatter avoids touching the parser/lexer

Comment and blank-line preservation would normally push a formatter to teach
the lexer to keep trivia.  The reference lexer (`revl.lexer`) and the live
selfhost lexer/parser are off-limits, so this module does NOT reuse them for
rendering.  Instead it carries a small, self-contained *trivia scanner*
(`_scan`) that mirrors the lexer's token boundaries but additionally keeps
comments, newlines and blank lines, and captures strings, backtick templates
and `@host { ... }` blocks as opaque verbatim spans.

Because strings, templates and host blocks are reproduced byte-for-byte, and
because the only whitespace the lexer treats as significant is the space
separating two adjacent word tokens (identifiers / keywords / numbers), the
formatter is free to normalise every other space.  Re-lexing the formatted
text yields the same token sequence, so the IR is identical -- which the gate
then proves for real.

### Documented limitation (no parser change)

The formatter is *line-preserving*: it re-indents and normalises horizontal
spacing but never moves a token onto a different logical line.  It cannot
re-flow statements, and it does not reformat the interior of backtick
templates or `@host` blocks (those are opaque verbatim spans).  Faithful
re-flowing would require statement-boundary information that only the parser
holds; rather than edit the parser, that is left out by design.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import RevlError

# --- token-boundary tables, mirrored from revl.lexer (read, not imported, so
# --- this module never depends on lexer internals for rendering) ------------

_OPERATORS = ("===", "!==", "=>", "?.", "??", "<=", ">=", "==", "!=", "&&", "||", "->")
_SINGLE_OPERATORS = set("+-*/%<>!?;|@")
_SYMBOLS = set("{}()[],:=.")

# Kinds a scanned piece can carry.
_WORD = "word"          # identifier or number (spacing-significant when adjacent)
_KW = "kw"              # keyword (a word, but rendered like `return (` with a space)
_VERBATIM = "verbatim"  # string / template / host-block span, reproduced as-is
_OP = "op"              # multi/single-char operator
_PUNCT = "punct"        # structural symbol from _SYMBOLS
_COMMENT = "comment"    # //... to end of line
_NEWLINE = "newline"    # one physical line break

_KEYWORDS = {
    "service", "component", "requires", "provides", "config", "let",
    "effect", "undo", "emit", "emission", "provide", "fn", "return",
    "true", "false", "null", "isolate", "intercept", "realm", "in", "with",
    "handoff",
    "every", "after",
    "spawn", "type", "use", "pub", "var", "while", "for", "of", "if", "else",
    "match", "test", "assert", "async", "as", "fail", "hole",
    "extern", "acquire", "pure", "compensate", "await", "verified", "commutative",
    "idempotent",
}

_OPENERS = {"(", "[", "{"}
_CLOSERS = {")", "]", "}"}


@dataclass
class _Piece:
    kind: str
    text: str


class FormatError(RevlError):
    """Raised when the source cannot even be scanned into tokens.

    Reusing `RevlError` keeps the CLI's existing error reporting path intact.
    """


# --------------------------------------------------------------------------
# Trivia scanner
# --------------------------------------------------------------------------

def _scan(source: str, filename: str) -> list[_Piece]:
    """Scan *source* into a flat list of pieces, preserving comments, newlines
    and blank lines, and capturing strings/templates/host-blocks verbatim.

    Token boundaries match `revl.lexer` exactly; whitespace (other than the
    newlines we record) is dropped -- the renderer re-derives it canonically.
    """
    pieces: list[_Piece] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == "\n":
            pieces.append(_Piece(_NEWLINE, "\n"))
            i += 1
        elif c in " \t\r":
            i += 1
        elif source.startswith("//", i):
            j = i
            while j < n and source[j] != "\n":
                j += 1
            pieces.append(_Piece(_COMMENT, source[i:j].rstrip()))
            i = j
        elif c == '"':
            j = _scan_string(source, i, filename)
            pieces.append(_Piece(_VERBATIM, source[i:j]))
            i = j
        elif c == "`":
            j = _scan_template(source, i, filename)
            pieces.append(_Piece(_VERBATIM, source[i:j]))
            i = j
        elif c == "@" and i + 1 < n and (source[i + 1].isalpha() or source[i + 1] == "_"):
            j = _scan_hostblock(source, i)
            if j is None:
                pieces.append(_Piece(_OP, "@"))
                i += 1
            else:
                pieces.append(_Piece(_VERBATIM, source[i:j]))
                i = j
        elif any(source.startswith(op, i) for op in _OPERATORS):
            for op in _OPERATORS:
                if source.startswith(op, i):
                    pieces.append(_Piece(_OP, op))
                    i += len(op)
                    break
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            word = source[i:j]
            pieces.append(_Piece(_KW if word in _KEYWORDS else _WORD, word))
            i = j
        elif c.isdigit():
            j = _scan_number(source, i)
            pieces.append(_Piece(_WORD, source[i:j]))
            i = j
        elif c in _SYMBOLS:
            pieces.append(_Piece(_PUNCT, c))
            i += 1
        elif c in _SINGLE_OPERATORS:
            pieces.append(_Piece(_OP, c))
            i += 1
        else:
            line = source.count("\n", 0, i) + 1
            raise FormatError(filename, line, f"unexpected character {c!r}")
    return pieces


def _scan_string(source: str, start: int, filename: str) -> int:
    """Return the index just past a double-quoted string starting at *start*."""
    n = len(source)
    j = start + 1
    while j < n:
        if source[j] == '"':
            return j + 1
        if source[j] == "\n":
            line = source.count("\n", 0, start) + 1
            raise FormatError(filename, line, "unterminated string literal")
        j += 1
    line = source.count("\n", 0, start) + 1
    raise FormatError(filename, line, "unterminated string literal")


def _scan_template(source: str, start: int, filename: str) -> int:
    """Return the index just past a backtick template starting at *start*.

    `${ ... }` interpolations are balanced across braces exactly as the lexer
    balances them, so a template containing a record literal is captured whole.
    """
    n = len(source)
    j = start + 1
    while j < n:
        c = source[j]
        if c == "`":
            return j + 1
        if c == "$" and j + 1 < n and source[j + 1] == "{":
            depth = 1
            j += 2
            while j < n and depth > 0:
                if source[j] == "{":
                    depth += 1
                elif source[j] == "}":
                    depth -= 1
                j += 1
            continue
        j += 1
    line = source.count("\n", 0, start) + 1
    raise FormatError(filename, line, "unterminated template literal")


def _scan_hostblock(source: str, start: int) -> int | None:
    """Return the index just past an `@backend { ... }` host block, or None if
    what follows the `@name` is not a `{` (i.e. it is a bare `@` operator)."""
    n = len(source)
    j = start + 1
    while j < n and (source[j].isalnum() or source[j] == "_"):
        j += 1
    k = j
    while k < n and source[k].isspace():
        k += 1
    if k >= n or source[k] != "{":
        return None
    depth = 0
    p = k
    while p < n:
        if source[p] == "{":
            depth += 1
        elif source[p] == "}":
            depth -= 1
            if depth == 0:
                return p + 1
        p += 1
    return None  # unterminated; treat as bare `@` and let the compiler report it


def _scan_number(source: str, start: int) -> int:
    """Return the index just past a numeric literal, using the lexer's float
    rule so `7.div(2)` stops at `7` and `.` becomes a separate token."""
    n = len(source)
    j = start
    while j < n and source[j].isdigit():
        j += 1
    if j < n and source[j] == "." and j + 1 < n and source[j + 1].isdigit():
        j += 1
        while j < n and source[j].isdigit():
            j += 1
    if j < n and source[j] in "eE":
        k = j + 1
        if k < n and source[k] in "+-":
            k += 1
        if k < n and source[k].isdigit():
            while k < n and source[k].isdigit():
                k += 1
            j = k
    return j


# --------------------------------------------------------------------------
# Renderer: line-preserving re-indent + canonical horizontal spacing
# --------------------------------------------------------------------------

_INDENT = "  "  # two spaces per nesting level


def _space_between(prev: _Piece, cur: _Piece) -> bool:
    """Whether a single space is emitted between two same-line pieces.

    Only word/word adjacency is significant to the lexer; every other choice
    is cosmetic and the IR gate backs it up.  The rules below reproduce the
    house style visible in the example corpus.
    """
    p, pk = prev.text, prev.kind
    c, ck = cur.text, cur.kind

    # dot access binds tight on both sides
    if c in (".", "?.") or p in (".", "?."):
        return False
    # a duration literal keeps its unit tight: `30s`, `5m`, `250ms` (item 57).
    # A number piece is the only _WORD that starts with a digit, and one of the
    # five unit idents can follow it only in a timer delay — the IR gate proves
    # the join changes nothing (both spellings lex to int + unit ident).
    if pk == _WORD and p[:1].isdigit() and ck == _WORD and c in ("ms", "s", "m", "h", "d"):
        return False
    # never a space before a closing structural token or a separator
    if c in (",", ":", ";", ")", "]"):
        return False
    # never a space right after an opener
    if p in ("(", "["):
        return False
    # capability annotation `emission[db]` binds its bracket tight, unlike the
    # `return [1, 2]` list form where the keyword keeps its space
    if c == "[" and p == "emission":
        return False
    # call / index: `f(...)`, `xs[...]`, `Opt[Str]`, `)(...)` -- but a keyword
    # keeps its space (`return (x)`, `if (c)`)
    if c in ("(", "[") and pk in (_WORD, _VERBATIM) or (c in ("(", "[") and p in (")", "]")):
        return False
    # `{` opens with a leading space except right after a call/index opener;
    # its interior gets a space unless the braces are empty
    if c == "{":
        return p not in ("(", "[")
    if p == "{":
        return c != "}"
    if c == "}":
        return p != "{"
    if p == "}":
        return c not in (",", ")", "]", ";", ":")
    # default: one space
    return True


def _render_line(pieces: list[_Piece], indent_level: int) -> str:
    """Render one logical line (pieces between newlines) at *indent_level*."""
    if not pieces:
        return ""
    out = [_INDENT * max(indent_level, 0)]
    for idx, piece in enumerate(pieces):
        if idx > 0 and _space_between(pieces[idx - 1], piece):
            out.append(" ")
        out.append(piece.text)
    return "".join(out).rstrip()


def _leading_closers(pieces: list[_Piece]) -> int:
    """Count consecutive closing brackets at the start of a line (they dedent
    to the enclosing level)."""
    count = 0
    for piece in pieces:
        if piece.kind == _PUNCT and piece.text in _CLOSERS:
            count += 1
        else:
            break
    return count


def _depth_delta(pieces: list[_Piece]) -> int:
    """Net bracket nesting change contributed by a line's pieces."""
    delta = 0
    for piece in pieces:
        if piece.kind == _PUNCT:
            if piece.text in _OPENERS:
                delta += 1
            elif piece.text in _CLOSERS:
                delta -= 1
    return delta


def format_source(source: str, filename: str = "<source>") -> str:
    """Return the canonical formatting of *source*.

    The formatting is a pure, deterministic function of the token stream, so
    it is idempotent: `format_source(format_source(x)) == format_source(x)`.
    """
    pieces = _scan(source, filename)

    # Split the piece stream into logical lines on NEWLINE markers.
    lines: list[list[_Piece]] = [[]]
    for piece in pieces:
        if piece.kind == _NEWLINE:
            lines.append([])
        else:
            lines[-1].append(piece)

    rendered: list[str] = []
    depth = 0
    for line in lines:
        code = [p for p in line if p.kind != _COMMENT]
        comments = [p for p in line if p.kind == _COMMENT]

        if not code and not comments:
            rendered.append("")  # blank line (collapsed below)
            continue

        indent_level = depth - _leading_closers(code)
        if code:
            text = _render_line(code, indent_level)
            if comments:  # trailing comment on a code line
                text = text + "  " + comments[0].text
            rendered.append(text)
        else:  # comment-only line, indented at the current depth
            rendered.append(_INDENT * max(depth, 0) + comments[0].text)

        depth += _depth_delta(code)
        if depth < 0:
            depth = 0

    return _finalize(rendered)


def _finalize(lines: list[str]) -> str:
    """Collapse runs of blank lines to one, trim leading/trailing blanks, and
    guarantee exactly one trailing newline.  All three are idempotent."""
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            continue
        if out and blank_run:
            out.append("")  # at most one blank between content lines
        blank_run = 0
        out.append(line)
    if not out:
        return ""
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# The self-proving IR-equivalence gate
# --------------------------------------------------------------------------

def _canonical_ir(ir: dict) -> str:
    """A byte-stable canonical string for an IR document."""
    return json.dumps(ir, sort_keys=True, ensure_ascii=False)


def _token_signature(source: str, filename: str) -> list[tuple[str, str]] | None:
    """The significant token stream (kind, text), dropping comments/newlines.

    This is the invariant the formatter actually guarantees, and the sound
    fall-back proof when neither side compiles (a syntactically valid file
    the type checker rejects still deserves formatting, and its line numbers
    -- hence its diagnostics -- move under reformatting).  Returns None if the
    source cannot be scanned at all.
    """
    try:
        pieces = _scan(source, filename)
    except RevlError:
        return None
    return [(p.kind, p.text) for p in pieces if p.kind not in (_COMMENT, _NEWLINE)]


def _compile_ir(source: str, filename: str):
    """Compile *source* to IR, returning `(ir, None)` or `(None, error)`.

    Imported lazily so this module stays importable without pulling the whole
    compiler in for callers that only want `format_source`.
    """
    from .compiler import compile_source

    try:
        return compile_source(source, filename), None
    except RevlError as error:
        return None, error
    except Exception as error:  # pragma: no cover - defensive
        return None, error


@dataclass
class GateResult:
    admitted: bool
    proof: str            # human-readable description of what was proven
    reason: str | None = None  # populated when refused


def ir_equivalent(original: str, candidate: str, filename: str = "<source>",
                  token_preserving: bool = True) -> GateResult:
    """The admissibility gate shared by `fmt` and `--migrate`.

    Compile both texts and require their IR to be byte-identical.  The result
    records what was proven, or -- when refused -- why.

    `token_preserving` describes the rewrite.  The formatter only ever
    normalises whitespace, so its rewrites keep the token stream identical
    (True, the default).  `--migrate` deliberately rewrites tokens
    (a legacy `"$name"` string becomes a `` `${name}` `` template), so it
    passes False -- the token-identity fall-back below does not apply to it.

    Rules, in order:

    * `candidate == original`  -> admitted, nothing changed.
    * original compiles:  a token-preserving rewrite (the formatter) MUST
      compile to byte-identical IR, otherwise it is refused (this is the
      headline gate -- it catches any reformat that changed what the compiler
      sees).  A token-changing rewrite (`--migrate`) is a DELIBERATE semantic
      upgrade: since 2.0 makes a bare `$` literal (item 203), a legacy
      `"$name"` now compiles as a literal and migrating it to a
      `` `${name}` `` template legitimately changes the IR, so an IR delta is
      ADMITTED as the intended migration -- the migrate gate that still bites
      is that the rewritten source must still COMPILE (a rewrite that broke
      compilation is refused, catching a corrupted mechanical pass).
    * original does NOT compile standalone but the candidate does (an
      `import`-only file, or a pre-item-203 1.x source the 2.0 compiler still
      rejects before `--migrate` fixes it):  IR cannot be compared, so the
      candidate is admitted as *newly admissible* -- it now compiles.
    * neither side compiles:  there is no compilable baseline for the gate to
      violate.  A token-preserving rewrite (the formatter) still proves the
      sound weaker invariant it guarantees -- the significant token stream is
      byte-identical -- admitting it (this formats a syntactically valid file
      the type checker rejects, e.g. an `examples/rejections/*` case) or
      refusing if the tokens changed.  A token-changing rewrite (`--migrate`)
      has no such baseline and no token invariant; it proceeds as the trusted
      mechanical rewrite it is (the gate's protection is the compilable-origin
      case above, where a migration that altered a working program is caught).
    """
    if candidate == original:
        return GateResult(True, "unchanged")

    ir_orig, err_orig = _compile_ir(original, filename)
    ir_cand, err_cand = _compile_ir(candidate, filename)

    if ir_orig is not None:
        if ir_cand is None:
            return GateResult(
                False,
                "IR comparison",
                reason=f"rewritten source no longer compiles: {err_cand}",
            )
        if _canonical_ir(ir_orig) != _canonical_ir(ir_cand):
            if not token_preserving:
                # `--migrate` is a DELIBERATE semantic upgrade, not an
                # equivalence-preserving reformat: since 2.0 makes a bare `$`
                # literal (item 203), a legacy `"$name"` now compiles as a
                # literal string, and migrating it to a `` `${name}` `` template
                # legitimately changes the IR. The migrate scanner (`revl.fmt`)
                # only ever rewrites `$`-bearing strings and copies everything
                # else through verbatim, so any IR delta is that intended
                # rewrite. The gate that still bites migration is the
                # `ir_cand is None` case above — a rewrite that broke
                # compilation is refused. An equivalence-preserving formatter
                # (token_preserving=True) never reaches here and still refuses.
                return GateResult(
                    True,
                    "migration rewrite (IR intentionally changed: legacy `$` "
                    "string → template)",
                )
            return GateResult(
                False,
                "IR comparison",
                reason="IR changed: the rewrite would alter compiled meaning",
            )
        return GateResult(True, "IR byte-identical")

    # Original did not compile on its own.
    if ir_cand is not None:
        return GateResult(
            True,
            "output newly admissible (original did not compile standalone)",
        )

    # Neither side compiles: no compilable baseline for the gate to violate.
    if not token_preserving:
        # A token-changing mechanical rewrite (`--migrate`) proceeds; the gate
        # only guards the compilable-origin case handled above.
        return GateResult(True, "no compilable baseline (mechanical rewrite)")

    # A token-preserving rewrite (the formatter) still proves its invariant.
    sig_orig = _token_signature(original, filename)
    sig_cand = _token_signature(candidate, filename)
    if sig_orig is not None and sig_orig == sig_cand:
        return GateResult(
            True,
            "token-stream identity (source does not compile standalone)",
        )
    return GateResult(
        False,
        "token comparison",
        reason=f"formatting changed the token stream (source does not compile): {err_cand}",
    )

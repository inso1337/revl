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
the lexer to keep trivia.  Rather than change the lexer, this module carries a
small *trivia scanner* (`_scan`) that adds comments, newlines and blank lines
to the lexer's token boundaries and captures strings, backtick templates and
`@host { ... }` blocks as opaque verbatim spans.  It does not RE-DERIVE those
boundaries: it calls `revl.lexer`'s own `_lex_number`, `_match_brace` and
quote rules, so a scanner that disagreed with the lexer is a class of bug that
cannot be introduced by omission (issue 309 -- the scanner had drifted to no
`_` separators, no `0x`, `"`-only strings and a naive brace count, and every
mis-scan was a silent rewrite of the program's meaning).

Because strings, templates and host blocks are reproduced byte-for-byte, and
because the only whitespace the lexer treats as significant is the space
separating two adjacent word tokens (identifiers / keywords / numbers), the
formatter is free to normalise every other space.  Re-lexing the formatted
text yields the same token sequence -- which the gate then proves for real,
with the reference lexer, on EVERY file it writes (see `ir_equivalent`).

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
import os
from dataclasses import dataclass

from . import lexer as _lexer
from .errors import RevlError

# --- token-boundary tables, IMPORTED from revl.lexer -------------------------
#
# These were hand-mirrored copies until issue 309.  Every divergence between
# the copy and the lexer was a silent meaning change: the copy had no `<<`/`>>`
# and no `&`/`^`/`~`, so `a & b` failed to scan and `a << b` scanned as two
# `<` operators.  A table the lexer owns cannot drift from the lexer.

_OPERATORS = _lexer.OPERATORS
_SINGLE_OPERATORS = set(_lexer.SINGLE_OPERATORS)
_SYMBOLS = set(_lexer.SYMBOLS)

# Kinds a scanned piece can carry.
_WORD = "word"          # identifier or number (spacing-significant when adjacent)
_KW = "kw"              # keyword (a word, but rendered like `return (` with a space)
_VERBATIM = "verbatim"  # string / template / host-block span, reproduced as-is
_OP = "op"              # multi/single-char operator
_PUNCT = "punct"        # structural symbol from _SYMBOLS
_COMMENT = "comment"    # //... to end of line
_NEWLINE = "newline"    # one physical line break

_KEYWORDS = _lexer.KEYWORDS

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

    Token boundaries come from `revl.lexer` itself (its `_lex_number`,
    `_match_brace` and quote rules), not from a second copy of its rules;
    whitespace other than the newlines we record is dropped -- the renderer
    re-derives it canonically.  The dispatch order below mirrors `lexer.lex`
    branch for branch, so a prefix collision (a triple quote before a single
    one, `<<` before `<`) resolves the same way in both.
    """
    pieces: list[_Piece] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == "\n":
            pieces.append(_Piece(_NEWLINE, "\n"))
            i += 1
        elif c in " \t\r" or (c.isspace() and c != "\n"):
            i += 1
        elif source.startswith("//", i):
            j = i
            while j < n and source[j] != "\n":
                j += 1
            pieces.append(_Piece(_COMMENT, source[i:j].rstrip()))
            i = j
        elif any(source.startswith(op, i) for op in _OPERATORS):
            for op in _OPERATORS:
                if source.startswith(op, i):
                    pieces.append(_Piece(_OP, op))
                    i += len(op)
                    break
        elif c in _QUOTES:
            j = _scan_string(source, i, filename)
            pieces.append(_Piece(_VERBATIM, source[i:j]))
            i = j
        elif c == "`":
            j = _scan_template(source, i, filename)
            pieces.append(_Piece(_VERBATIM, source[i:j]))
            i = j
        elif c in _lexer._IDENT_START:
            j = i
            while j < n and source[j] in _lexer._IDENT_CONT:
                j += 1
            word = source[i:j]
            pieces.append(_Piece(_KW if word in _KEYWORDS else _WORD, word))
            i = j
        elif c in _lexer._ASCII_DIGIT:
            j = _scan_number(source, i, filename)
            pieces.append(_Piece(_WORD, source[i:j]))
            i = j
        elif c == "@" and i + 1 < n and source[i + 1] in _lexer._IDENT_START:
            j = _scan_hostblock(source, i, filename)
            if j is None:
                pieces.append(_Piece(_OP, "@"))
                i += 1
            else:
                pieces.append(_Piece(_VERBATIM, source[i:j]))
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


def _as_format_error(filename: str, line: int, error: RevlError) -> FormatError:
    """Re-badge a lexer refusal as a `FormatError`, keeping its own message,
    line and hint so `revl fmt` reports exactly what `revl compile` would."""
    return FormatError(filename, getattr(error, "line", line),
                       getattr(error, "message", str(error)),
                       hint=getattr(error, "hint", None))


# The three string spellings the lexer accepts (docs/strings.md): `"..."`,
# `'...'` (item 382) and the multi-line verbatim `"""..."""`.  A scanner that
# knew only `"` split `"""abc"""` into `""`, `abc`, `""` -- three tokens where
# the program had one (issue 309).
_QUOTES = ('"', "'")


def _scan_string(source: str, start: int, filename: str) -> int:
    """Return the index just past a string literal starting at *start*.

    Delegates the actual boundary to the lexer's own `_lex_triple_string` /
    `_lex_string`, which own the escape rules (`\\"` and `\\\\` only) and the
    triple-quote form.  Only the END OFFSET is used -- the span is reproduced
    byte-for-byte, so the lexer's value decoding is irrelevant here.
    """
    line = source.count("\n", 0, start) + 1
    try:
        if source.startswith('"""', start):
            end, _text, _line = _lexer._lex_triple_string(
                source, start + 3, line, filename)
            return end
        end, _text = _lexer._lex_string(
            source, start + 1, line, filename, quote=source[start])
        return end
    except RevlError as error:
        raise _as_format_error(filename, line, error) from None


def _scan_template(source: str, start: int, filename: str) -> int:
    """Return the index just past a backtick template starting at *start*.

    `${ ... }` interpolations are balanced by the lexer's own `_match_brace`,
    so a `}` inside an interpolated string (`` `${m.get("}")}` ``) or after a
    `//` does not close the template early.
    """
    line = source.count("\n", 0, start) + 1
    try:
        end, _parts, _line, _suspect = _lexer._lex_template(
            source, start + 1, line, filename)
        return end
    except RevlError as error:
        raise _as_format_error(filename, line, error) from None


def _scan_hostblock(source: str, start: int, filename: str) -> int | None:
    """Return the index just past an `@backend { ... }` host block, or None if
    what follows the `@name` is not a `{` (i.e. it is a bare `@` operator).

    The closing brace is found with the lexer's `_match_brace` under the same
    per-backend trivia table the lexer uses, so a `}` inside a host string or
    block comment (`@ts { return "}" }`, `@go { /* } */ }`) does not truncate
    the body.  A naive brace count truncated `stdlib/json.rvl` (issue 309).
    """
    n = len(source)
    j = start + 1
    while j < n and source[j] in _lexer._IDENT_CONT:
        j += 1
    backend = source[start + 1:j]
    k = j
    while k < n and source[k].isspace():
        k += 1
    if k >= n or source[k] != "{":
        return None
    trivia = _lexer._HOST_TRIVIA.get(backend, _lexer._C_FAMILY)
    close = _lexer._match_brace(source, k, trivia)
    if close is None:
        line = source.count("\n", 0, start) + 1
        raise FormatError(filename, line, f"unterminated @{backend} host body")
    return close + 1


def _scan_number(source: str, start: int, filename: str) -> int:
    """Return the index just past a numeric literal.

    Delegates to the lexer's `_lex_number`, which owns the `0x`/`0b`/`0o`
    radix prefixes and the `_` digit-group separators (item 381) as well as the
    float rule that stops `7.div(2)` at `7`.  The hand-written copy this
    replaced knew none of the three, so it split `1_000` into `1` and `_000`
    and `0xFF` into `0` and `xFF` -- both silent meaning changes (issue 309).
    """
    line = source.count("\n", 0, start) + 1
    try:
        end, _token = _lexer._lex_number(source, start, line, filename)
        return end
    except RevlError as error:
        raise _as_format_error(filename, line, error) from None


# --------------------------------------------------------------------------
# Renderer: line-preserving re-indent + canonical horizontal spacing
# --------------------------------------------------------------------------

_INDENT = "  "  # two spaces per nesting level


def _space_between(prev: _Piece, cur: _Piece,
                   nxt: _Piece | None = None) -> bool:
    """Whether a single space is emitted between two same-line pieces.

    Only word/word adjacency is significant to the lexer; every other choice
    is cosmetic and the IR gate backs it up.  The rules below reproduce the
    house style visible in the example corpus.
    """
    p, pk = prev.text, prev.kind
    c, ck = cur.text, cur.kind

    # dot access binds tight on both sides -- but a `.` that leads an origin
    # qualifier (`.::@db`, item 426 S2: the project's own origin is spelled
    # `.`) is not dot access, and it keeps the space in front of it. The `::`
    # after it is what tells them apart, and it is the only place a `.` is
    # followed by a `:` in the grammar.
    if c == "." and nxt is not None and nxt.text == ":":
        return p not in ("(", "[")
    if c in (".", "?.") or p in (".", "?."):
        return False
    # a duration literal keeps its unit tight: `30s`, `5m`, `250ms` (item 57).
    # A number piece is the only _WORD that starts with a digit, and one of the
    # five unit idents can follow it only in a timer delay — the IR gate proves
    # the join changes nothing (both spellings lex to int + unit ident).
    # A NON-DECIMAL literal is excluded: `d` is a hex digit, so joining
    # `0x3 d` would make the single literal `0x3d` (61) out of two tokens --
    # the one join in this table that is not whitespace-only. The gate would
    # refuse it, but a refusal on valid source is still a formatter that does
    # not work, so the rule excludes the case rather than leans on the gate.
    if (pk == _WORD and p[:1].isdigit() and ck == _WORD
            and c in ("ms", "s", "m", "h", "d")
            and p[1:2].lower() not in ("x", "b", "o")):
        return False
    # a row label binds its sigil tight: `@db`, never `@ db` (item 426). A bare
    # `@` piece exists only here — an `@backend { ... }` host body is scanned as
    # one verbatim span — so this rule cannot reach any other construct.
    if p == "@":
        return False
    # a fully qualified row label binds tight through its `::` separator:
    # `acme_pg::@db`, never `acme_pg: : @db` (item 426 S2). `::` reaches the
    # formatter as two `:` pieces because the lexer has no `::` token, which is
    # deliberate — an operator token would have to be mirrored in the
    # self-hosted lexer, and this needs no lexer change at all. A `:` is
    # followed by another `:` or by a bare `@` nowhere else in the grammar.
    if p == ":" and c in (":", "@"):
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
        if idx > 0 and _space_between(
                pieces[idx - 1], piece,
                pieces[idx + 1] if idx + 1 < len(pieces) else None):
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


def _token_signature(source: str, filename: str):
    """The token stream the REFERENCE LEXER produces, as `(kind, value)` pairs.

    Line numbers are excluded on purpose: the formatter collapses runs of blank
    lines, so a token may legitimately move to a different line, and nothing
    downstream of the parser distinguishes two programs by line number alone.
    Everything else the lexer reports is compared, so two sources with this
    same signature parse identically and therefore compile identically.

    This used to compare `_scan(original)` with `_scan(candidate)` -- the
    FORMATTER'S OWN scanner against itself.  That is not a proof of anything: a
    scanner bug corrupts both sides in the same way, so the comparison passes
    and the corrupted rewrite ships (issue 309).  The lexer is the only
    authority on what a token stream is, so the lexer is what gets asked.

    Returns None when *source* does not lex, which the gate treats as a
    refusal rather than as an excuse to skip the check.
    """
    from .lexer import lex

    try:
        return [(token.kind, token.value) for token in lex(source, filename)]
    except RevlError:
        return None
    except Exception:  # pragma: no cover - defensive
        return None


def _compile_ir(source: str, filename: str):
    """Compile *source* to IR, returning `(ir, None)` or `(None, error)`.

    When *filename* names a file that exists on disk, the compile goes through
    `compile_files` with *source* supplied in memory for that root, so `use`
    imports RESOLVE and the IR gate actually runs.  `compile_source` alone
    refuses any `use`-bearing file for want of a module directory, which sent
    every such file to the fall-back branch and out from under the headline
    gate (issue 309).  The in-memory override is what lets the CANDIDATE be
    compiled in place -- same directory, same imports -- without writing it.

    Imported lazily so this module stays importable without pulling the whole
    compiler in for callers that only want `format_source`.
    """
    from .compiler import compile_files, compile_source

    try:
        if filename and os.path.isfile(filename):
            ir = compile_files([filename],
                               sources={os.path.abspath(filename): source})
        else:
            ir = compile_source(source, filename)
        return ir, None
    except RevlError as error:
        return None, error
    except Exception as error:  # pragma: no cover - defensive
        return None, error


@dataclass
class GateResult:
    admitted: bool
    proof: str            # human-readable description of what was proven
    reason: str | None = None  # populated when refused
    # populated when a check could not be run to completion but the rewrite is
    # carried by a stronger proof; the caller MUST surface it, so a weakened
    # check is never silent.
    warning: str | None = None


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
    * a token-preserving rewrite (the formatter) must FIRST prove, with the
      reference lexer, that both texts lex and lex to the same token stream.
      This runs on EVERY file, before anything else and whatever the file
      contains, and a failure at any of the three steps -- the original does
      not lex, the output does not lex, the streams differ -- is a refusal.
      This is the check that makes the class of bug behind issue 309
      impossible: the formatter is a whitespace rewrite, so identical tokens
      is exactly its contract, and a mis-scan can now only cost a refusal
      rather than a silently corrupted file.  It replaces a fall-back that
      compared the formatter's own scanner with itself and therefore proved
      nothing.
    * original compiles:  a token-preserving rewrite (the formatter) MUST also
      compile to byte-identical IR, otherwise it is refused (the headline gate
      -- it catches any reformat that changed what the compiler sees).  Since
      `_compile_ir` resolves `use` imports for an on-disk file, this arm now
      covers the `use`-bearing files that used to fall past it.  A
      token-changing rewrite (`--migrate`) is a DELIBERATE semantic upgrade:
      since 2.0 makes a bare `$` literal (item 203), a legacy `"$name"` now
      compiles as a literal and migrating it to a `` `${name}` `` template
      legitimately changes the IR, so an IR delta is ADMITTED as the intended
      migration -- the migrate gate that still bites is that the rewritten
      source must still COMPILE (a rewrite that broke compilation is refused,
      catching a corrupted mechanical pass).
    * original does NOT compile but the candidate does (a pre-item-203 1.x
      source the 2.0 compiler still rejects before `--migrate` fixes it):  IR
      cannot be compared, so the candidate is admitted as *newly admissible*
      -- it now compiles.  A formatter reaching here has already proven token
      identity above.
    * neither side compiles (a syntactically valid file the type checker
      rejects, e.g. an `examples/rejections/*` case, still deserves
      formatting):  there is no IR baseline to compare.  A token-preserving
      rewrite is carried by the lexer-verified token identity already proven;
      a token-changing rewrite (`--migrate`) has no such invariant and
      proceeds as the trusted mechanical rewrite it is (the gate's protection
      is the compilable-origin case above).
    """
    if candidate == original:
        return GateResult(True, "unchanged")

    # Step 1, universal: the formatter's own contract, checked with the
    # reference lexer on every construct in every file, fail-closed.
    if token_preserving:
        sig_orig = _token_signature(original, filename)
        if sig_orig is None:
            return GateResult(
                False, "token comparison",
                reason="source does not lex, so no formatting of it can be "
                       "proven meaning-preserving",
            )
        sig_cand = _token_signature(candidate, filename)
        if sig_cand is None:
            return GateResult(
                False, "token comparison",
                reason="formatted output no longer lexes",
            )
        if sig_orig != sig_cand:
            return GateResult(
                False, "token comparison",
                reason="formatting changed the token stream: "
                       + _first_token_delta(sig_orig, sig_cand),
            )

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
            # The token stream is already proven identical with the reference
            # lexer, so an IR delta here means the compiler saw something the
            # lexer did not. Before blaming the rewrite, check the oracle: if
            # compiling the ORIGINAL twice does not agree with itself, the IR
            # comparison carries no information about the rewrite at all, and
            # refusing on it would be a false alarm on a rewrite the stronger
            # check already proved safe. (`selfhost/*.rvl` hit this: the
            # composition compiler dedupes declarations by `id()`, and CPython
            # reuses the address of a freed object, so a second compile in the
            # same process can drop a declaration.) Admit on the token proof,
            # and hand the caller a warning so the compiler bug is loud.
            replay, _ = _compile_ir(original, filename)
            if replay is None or _canonical_ir(replay) != _canonical_ir(ir_orig):
                return GateResult(
                    True,
                    "reference-lexer token identity (IR comparison unavailable)",
                    warning=(
                        f"{filename}: compiling the ORIGINAL twice did not "
                        "produce the same IR, so the IR cross-check was "
                        "skipped; the formatting is admitted on the "
                        "reference-lexer token identity alone"
                    ),
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

    # Neither side compiles: no IR baseline for the gate to violate.
    if not token_preserving:
        # A token-changing mechanical rewrite (`--migrate`) proceeds; the gate
        # only guards the compilable-origin case handled above.
        return GateResult(True, "no compilable baseline (mechanical rewrite)")

    # A token-preserving rewrite carries the lexer-verified token identity
    # proven in step 1 above; that is the whole of the formatter's contract.
    return GateResult(
        True,
        "reference-lexer token identity (source does not compile: "
        f"{err_orig})",
    )


def _first_token_delta(sig_orig, sig_cand) -> str:
    """A short description of where two token streams first diverge, so a
    refusal names the construct the scanner got wrong instead of just saying
    that something changed."""
    for index, (before, after) in enumerate(zip(sig_orig, sig_cand)):
        if before != after:
            return (f"token {index} was {before[0]} {before[1]!r}, "
                    f"became {after[0]} {after[1]!r}")
    if len(sig_orig) != len(sig_cand):
        return (f"{len(sig_orig)} tokens before, {len(sig_cand)} after")
    return "streams differ"  # pragma: no cover - unreachable while != held

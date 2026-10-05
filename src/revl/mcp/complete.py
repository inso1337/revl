"""Server-side completion of terse source (issue #1700).

An agent saves output by leaving out punctuation that is not a decision. The
parser refuses two such forms (measured on #1820's head), and this module
inserts what they lack:

* match arms separated by newlines alone: `A => 1` newline `B => 2` gets the
  `,` after `1`. A line counts as a new arm only if it holds `=>` at the
  arm's own depth, so a continued expression is never split;
* an `if` without parentheses: `if x > 1 {` becomes `if (x > 1) {`, the
  condition being everything up to the first `{` at its own depth on the same
  line.

Completion never changes the language: `revl compile` still refuses these
forms, so a file on disk stays in the one canonical grammar. It runs only on
text that does NOT parse, and its result is used only if it DOES parse;
anything else (no insertion found, an insertion that still does not parse,
a condition whose first `{` is a record literal) leaves the text exactly as
sent, so the compile reports the agent's own error. Every insertion is inside
one line, so diagnostics keep their line numbers. The completed text then goes
through the formatter's IR-equivalence gate like any other (`canonical.py`).
"""

from __future__ import annotations

from ..errors import RevlError

_OPEN, _CLOSE = "([{", ")]}"


def complete(text: str, filename: str = "<candidate>.rvl") -> tuple[str, list]:
    """`(text, insertions)`: the completed text and what was inserted, as
    `[{line, inserted}]`, or `text` unchanged and `[]`."""
    if _parses(text, filename):
        return text, []
    try:
        pieces = _code_pieces(text)
    except RevlError:
        return text, []
    inserts = _match_arm_commas(pieces) + _if_parens(pieces)
    if not inserts:
        return text, []
    completed = text
    for offset, insert in sorted(inserts, reverse=True):
        completed = completed[:offset] + insert + completed[offset:]
    if not _parses(completed, filename):
        return text, []
    report = [{"line": text.count("\n", 0, offset) + 1, "inserted": insert}
              for offset, insert in sorted(inserts)]
    return completed, report


def _parses(text: str, filename: str) -> bool:
    from ..parser import Parser  # noqa: PLC0415 - the parser imports the lexer
    try:
        Parser(text, filename).parse()
    except RevlError:
        return False
    except Exception:  # noqa: BLE001 - a parser crash is not a completion
        return False
    return True


def _code_pieces(text: str) -> list:
    """The scanner's pieces with their spans, comments dropped (newlines kept:
    they are what separates arms)."""
    from ..formatter import _scan  # noqa: PLC0415
    return [p for p in _scan(text, "<complete>") if p.kind != "comment"]


def _match_arm_commas(pieces: list) -> list:
    """`(offset, ",")` before every line break that ends a match arm and is
    followed by another arm at the same depth."""
    out = []
    stack: list[str] = []          # "match" or "other", per open bracket
    pending_match = False
    last_code = None               # the last code piece at the current depth
    for i, piece in enumerate(pieces):
        if piece.kind == "kw" and piece.text == "match":
            pending_match = True
        if piece.kind == "punct" and piece.text in _OPEN:
            stack.append("match" if piece.text == "{" and pending_match else "other")
            if piece.text == "{":
                pending_match = False
            last_code = piece
            continue
        if piece.kind == "punct" and piece.text in _CLOSE:
            if stack:
                stack.pop()
            last_code = piece
            continue
        if piece.kind == "newline":
            continue
        starts_line = i > 0 and pieces[i - 1].kind == "newline"
        if starts_line and stack and stack[-1] == "match" \
                and _arm_starts(pieces, i) and last_code is not None \
                and not (last_code.kind == "punct" and last_code.text in ",{"):
            out.append((last_code.end, ","))
        last_code = piece
    return out


def _arm_starts(pieces: list, i: int) -> bool:
    """Whether the line starting at piece `i` holds `=>` at its own depth."""
    depth = 0
    for piece in pieces[i:]:
        if piece.kind == "newline" and depth == 0:
            return False
        if piece.kind == "punct" and piece.text in _OPEN:
            depth += 1
        elif piece.kind == "punct" and piece.text in _CLOSE:
            if depth == 0:
                return False
            depth -= 1
        elif depth == 0 and piece.kind == "op" and piece.text == "=>":
            return True
        elif depth == 0 and piece.kind == "punct" and piece.text == ",":
            return False
    return False


def _if_parens(pieces: list) -> list:
    """`(offset, "(")` and `(offset, ")")` around every `if` condition written
    without parentheses, when its block's `{` is on the same line."""
    out = []
    for i, piece in enumerate(pieces):
        if not (piece.kind == "kw" and piece.text == "if"):
            continue
        if i + 1 >= len(pieces) or pieces[i + 1].kind == "newline":
            continue
        if pieces[i + 1].kind == "punct" and pieces[i + 1].text == "(":
            continue                       # already written with parentheses
        brace = _condition_end(pieces, i + 1)
        if brace is None or brace == i + 1:
            continue                       # ambiguous or empty: leave it
        out.append((pieces[i + 1].start, "("))
        out.append((pieces[brace - 1].end, ")"))
    return out


def _condition_end(pieces: list, start: int) -> int | None:
    """The index of the `{` that opens the `if`'s block: the first `{` at the
    condition's own depth on the same line. None when the line ends first or a
    bracket closes below the condition (then there is nothing unambiguous to
    wrap)."""
    depth = 0
    for j in range(start, len(pieces)):
        piece = pieces[j]
        if piece.kind == "newline":
            return None
        if piece.kind == "punct" and piece.text == "{" and depth == 0:
            return j
        if piece.kind == "punct" and piece.text in _OPEN:
            depth += 1
        elif piece.kind == "punct" and piece.text in _CLOSE:
            if depth == 0:
                return None
            depth -= 1
        elif depth == 0 and piece.kind == "op" and piece.text == "=>":
            return None
    return None

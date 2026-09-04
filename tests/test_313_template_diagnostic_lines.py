"""A fault in a template is reported at the line it is on.

Issue #313, two independent line bugs in backtick templates:

  1. Every `${...}` body was re-parsed by a fresh `Parser` that started
     counting at line 1, so every AST node it produced — and every checker
     refusal on one — claimed line 1 of the file no matter where the template
     sat. A misspelt name inside an interpolation on line 6 was reported at
     line 1.
  2. The `template` token took the line the lexer had reached AFTER consuming
     the literal, i.e. the closing backtick's. An error against a multi-line
     template as a whole pointed at its last line instead of its first. The
     triple-quoted string branch two cases above it already took the
     opening line.

Both are read by the LSP too, through `analysis._range_for`, so the wrong line
became a squiggle on the wrong line.

The assertions are on the exact line and, through the LSP range, the exact
column — "a diagnostic appeared" was already true before the fix.

Non-vacuity: undo `start_line` in `lexer.lex`'s template branch and the
whole-template cases fail; undo the `line_offset` in `_parse_template_parts`
and the interpolation cases fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.lexer import lex  # noqa: E402
from revl.lsp.analysis import compute_diagnostics  # noqa: E402


def _refuse(source: str) -> RevlError:
    with pytest.raises(RevlError) as caught:
        compile_source(source, "t.rvl")
    error = caught.value
    return (getattr(error, "errors", None) or [error])[0]


# ------------------------------------------- 1: inside a `${...}` interpolation

#: the template sits on line 6; `nme` is a typo for the parameter `name`
UNDECLARED_IN_INTERPOLATION = """\
// line 1
// line 2
// line 3

fn f(name: Str) -> Str {
  return `hello ${nme}`
}
"""


def test_a_checker_refusal_inside_an_interpolation_names_the_real_line():
    error = _refuse(UNDECLARED_IN_INTERPOLATION)

    assert error.line == 6, f"reported line {error.line}, the template is on 6"
    assert "nme" in str(error)


def test_the_lsp_squiggles_the_interpolation_not_line_one():
    # The user-visible half: `_range_for` turns the error's line into a range,
    # so a line-1 error squiggled the first line of the file. Zero-based here.
    diagnostics = compute_diagnostics(UNDECLARED_IN_INTERPOLATION)

    assert len(diagnostics) == 1
    span = diagnostics[0]["range"]
    assert span["start"]["line"] == 5, diagnostics
    assert span["end"]["line"] == 5
    # and tightened onto `nme` itself, not the whole line: column 17 is where
    # `nme` starts in `  return `hello ${nme}``.
    line6 = UNDECLARED_IN_INTERPOLATION.split("\n")[5]
    assert line6[span["start"]["character"]:span["end"]["character"]] == "nme"


def test_an_interpolation_deeper_into_a_multi_line_template():
    # The `${` opens on line 3 but the expression itself is on line 4: the
    # offset is taken from the first non-space character, not from the `$`.
    source = (
        "fn f(a: Str) -> Str {\n"      # 1
        "  return `x\n"                # 2
        "y ${\n"                       # 3
        "  nme\n"                      # 4
        "} z`\n"                       # 5
        "}\n")                         # 6

    assert _refuse(source).line == 4


def test_a_parse_error_inside_an_interpolation_names_the_real_line():
    source = "\n\n\n\nfn f(a: Int) -> Str {\n  return `v=${a +}`\n}\n"

    error = _refuse(source)
    assert error.line == 6
    # the "related cosmetic" in the issue: the eof token's value is None, and
    # `found None` reads as a literal rather than as the input running out.
    assert "found end of file" in str(error)
    assert "found None" not in str(error)


def test_a_second_interpolation_gets_its_own_line():
    # The offsets are per-`${...}`, in order — not one offset for the template.
    source = (
        "fn f(a: Int) -> Str {\n"      # 1
        "  return `${a}\n"             # 2
        "${nme}`\n"                    # 3
        "}\n")                         # 4

    assert _refuse(source).line == 3


def test_a_single_line_template_still_reports_its_own_line():
    # The offset arithmetic must not shift the ordinary case.
    source = "fn f() -> Str {\n  return `hi ${nme}`\n}\n"
    assert _refuse(source).line == 2


# --------------------------------------- 2: the template token's own line

def test_a_multi_line_template_is_reported_at_its_opening_line():
    # The `return` is on line 2; the template closes on line 5. The refusal is
    # about the returned value, so it belongs on line 2.
    source = "fn f() -> Int {\n  return `a\n\n\nb`\n}\n"

    assert _refuse(source).line == 2


def test_the_template_token_carries_its_opening_line():
    # Directly, so a later reader sees which end of the literal the line means.
    tokens = lex("let x = 1\nlet t = `a\nb\nc`\n", "t.rvl")
    template = [t for t in tokens if t.kind == "template"]
    assert len(template) == 1
    assert template[0].line == 2, "the template opens on line 2 and closes on 4"


def test_a_triple_quoted_string_is_unchanged():
    # It already reported its opening line; the template fix brings the two
    # into agreement rather than moving this one.
    tokens = lex('let s = """a\nb\nc"""\n', "t.rvl")
    assert [t.line for t in tokens if t.kind == "string"] == [1]


def test_the_lexer_records_one_start_line_per_interpolation():
    tokens = lex("let t = `a${x}\nb${\n  y}c`\n", "t.rvl")
    template = [t for t in tokens if t.kind == "template"][0]
    # `x` is on line 1, `y` on line 3 (the `${` opens on 2, `y` sits on 3).
    assert template.interp_lines == [1, 3]


def test_lex_accepts_a_line_offset():
    # The mechanism the sub-parse rides on, on its own.
    assert [t.line for t in lex("a\nb", "t.rvl")] == [1, 2, 2]
    assert [t.line for t in lex("a\nb", "t.rvl", line_offset=40)] == [41, 42, 42]


def test_a_clean_template_still_compiles():
    # None of the above may change what is ACCEPTED.
    ir = compile_source("fn f(name: Str) -> Str {\n  return `hi ${name}\nbye`\n}\n",
                        "t.rvl")
    assert ir["ir_version"] == 3

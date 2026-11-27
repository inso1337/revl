"""Issue #3499: a failed `assert` prints the source position and the revl
source text of the asserted expression, not the lowered host expression.

Before, the py tier's failure line showed the emitted Python
(`(r['status'] if isinstance(r, dict) else getattr(r, 'status')) == 303`) and
no file:line — with two field accesses it was unreadable, and in a file with
many same-shaped asserts the failing one had to be matched back to source by
eye. The values (left/right) were already printed.

The parser now captures the raw source text of each `assert` statement
(`AssertStmt.src`, sliced from the `assert` keyword to the end of the
expression, whitespace-collapsed, one unbalanced trailing `}` trimmed for the
one-liner `test "x" { assert ... }` form), the lowering carries it with the
line and file on the assert IR step, and the py emitter shows
`FAIL inline: g.rvl:4: assert r.status == 303` followed by `left`/`right`.
The lowered host expression is no longer shown.

Checked here (py tier via run_py, stdout captured):
  * the issue's exact repro: `g.rvl:4: assert r.status == 303` + values;
  * a one-liner test block: `assert 1 == 2` with no trailing brace;
  * a record literal in the expression keeps its balanced braces;
  * a multi-line assert collapses to one line;
  * a `&&` chain (split per #3513) shows the whole chain's source and the
    failed operand's values;
  * the parser captures `src` on every assert statement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.parser import parse_text  # noqa: E402
from revl.test import run_py  # noqa: E402

ISSUE_REPRO = """\
type R = { status: Int }
test "inline" {
  let r: R = { status: 200 }
  assert r.status == 303
}
"""


def _run(src: str, capsys, name: str = "g.rvl"):
    ir = compile_source(src, name)
    verdict, _ = run_py(ir)
    return ir, verdict, capsys.readouterr().out


def test_issue_repro_prints_source_position_and_text(capsys):
    _, verdict, out = _run(ISSUE_REPRO, capsys)
    assert verdict == "fail"
    assert "FAIL inline: g.rvl:4: assert r.status == 303" in out, out
    assert "left  = 200" in out and "right = 303" in out, out
    # the lowered host expression is gone from the failure line
    assert "isinstance" not in out and "getattr" not in out, out


def test_one_liner_test_block_trims_the_closing_brace(capsys):
    _, verdict, out = _run('test "one-liner" { assert 1 == 2 }\n', capsys)
    assert verdict == "fail"
    assert "g.rvl:1: assert 1 == 2" in out, out
    assert "assert 1 == 2 }" not in out, out


def test_record_literal_in_expression_keeps_its_braces():
    program = parse_text('test "rec" { assert { a: 1 } == { a: 1 } }\n', "g.rvl")
    srcs = [s.src for s in program.tests[0].body if getattr(s, "src", None)]
    assert srcs == ["assert { a: 1 } == { a: 1 }"]


def test_multiline_assert_collapses_to_one_line(capsys):
    src = (
        'test "multi" {\n'
        '  assert 1 ==\n'
        '    2\n'
        '}\n'
    )
    _, verdict, out = _run(src, capsys)
    assert verdict == "fail"
    assert "g.rvl:2: assert 1 == 2" in out, out


def test_and_chain_shows_chain_source_and_failed_operand_values(capsys):
    _, verdict, out = _run(
        'test "and" { assert 1 == 1 && 2 == 3 }\n', capsys)
    assert verdict == "fail"
    assert "g.rvl:1: assert 1 == 1 && 2 == 3" in out, out
    assert "left  = 2" in out and "right = 3" in out, out


def test_parser_captures_src_on_every_assert():
    program = parse_text(ISSUE_REPRO, "g.rvl")
    srcs = [s.src for s in program.tests[0].body if getattr(s, "src", None)]
    assert srcs == ["assert r.status == 303"]


def test_assert_step_carries_line_src_file(capsys):
    ir, _, _ = _run(ISSUE_REPRO, capsys)
    steps = [s for t in ir["tests"] for s in t["body"] if s["step"] == "assert"]
    assert len(steps) == 1
    assert steps[0]["line"] == 4
    assert steps[0]["src"] == "assert r.status == 303"
    assert steps[0]["file"] == "g.rvl"

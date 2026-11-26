"""Issue #3513: a failed `assert a && b` reports which operand was false, with
its values.

Before, an `assert` over a top-level `&&` printed only "assertion failed": no
expression, no operand, no values — while a single comparison printed both
sides. The author had to split the line into two `assert`s and rerun to learn
which half failed.

At lowering, a top-level `&&` chain in an `assert` is now split into one
assert step per operand (left to right), so each failure reports the first
false operand exactly as a single comparison does (`b == 2`, `left = 1`,
`right = 2` on the py tier). Short-circuit semantics are unchanged — the
first false operand's assert raises first. Single comparisons and bare Bool
exprs are lowered exactly as before (one step, same shape).

Checked here (py tier via run_py):
  * the issue's exact repro: `assert a == 1 && b == 2` fails with `b == 2`
    and both values in the message;
  * a three-operand chain reports the FIRST false operand;
  * a passing `&&` chain passes;
  * a single comparison still reports as before;
  * the IR for a non-`&&` assert is one assert step, unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.test import run_py  # noqa: E402


def _run(src: str, capsys):
    """run_py prints the per-test FAIL lines (with the assert message) to
    stdout; the returned detail carries only the summary. Capture both."""
    ir = compile_source(src, "f.rvl")
    verdict, _detail = run_py(ir)
    out = capsys.readouterr().out
    return ir, verdict, out


def test_issue_repro_reports_the_false_operand_with_values(capsys):
    _, verdict, out = _run(
        "fn one() -> Int { return 1 }\n"
        "test \"and\" {\n"
        "  let a = one()\n"
        "  let b = one()\n"
        "  assert a == 1 && b == 2\n"
        "}\n", capsys)
    assert verdict == "fail"
    assert "b == 2" in out, out
    assert "left  = 1" in out and "right = 2" in out, out


def test_three_operand_chain_reports_the_first_false_one(capsys):
    _, verdict, out = _run(
        "test \"chain\" { assert 1 == 1 && 2 == 2 && 3 == 4 }\n", capsys)
    assert verdict == "fail"
    assert "3 == 4" in out, out
    assert "left  = 3" in out and "right = 4" in out, out


def test_second_operand_false_reports_the_second(capsys):
    _, verdict, out = _run(
        "test \"chain\" { assert 1 == 1 && 2 == 3 }\n", capsys)
    assert verdict == "fail"
    assert "2 == 3" in out, out


def test_passing_and_chain_passes(capsys):
    _, verdict, out = _run(
        "fn one() -> Int { return 1 }\n"
        "test \"ok\" { assert one() == 1 && one() == 1 }\n", capsys)
    assert verdict == "pass", out


def test_single_comparison_unchanged(capsys):
    ir, verdict, out = _run("test \"s\" { assert 1 == 2 }\n", capsys)
    assert verdict == "fail"
    assert "1 == 2" in out and "left  = 1" in out, out
    # exactly one assert step, same shape as before the change
    steps = [s for t in ir["tests"] for s in t["body"] if s["step"] == "assert"]
    assert len(steps) == 1 and set(steps[0]) == {"step", "expr"}


def test_and_chain_splits_into_one_step_per_operand(capsys):
    ir, verdict, _ = _run(
        "test \"c\" { assert 1 == 1 && 2 == 2 && 3 == 3 }\n", capsys)
    assert verdict == "pass"
    steps = [s for t in ir["tests"] for s in t["body"] if s["step"] == "assert"]
    assert len(steps) == 3
    assert all(set(s) == {"step", "expr"} for s in steps)


def test_bare_bool_operand_in_chain_stays_plain(capsys):
    ir, verdict, _ = _run(
        "test \"b\" { let flag = false\n assert 1 == 1 && flag }\n", capsys)
    assert verdict == "fail"
    steps = [s for t in ir["tests"] for s in t["body"] if s["step"] == "assert"]
    assert len(steps) == 2

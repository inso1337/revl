"""Issue #3513: a failed `assert a && b` said only "assertion failed".

The top-level `&&` chain now lowers to one assert step per operand
(`lower._assert_operands`), so the failure names the first false operand with
the left/right values a single comparison already reported. Short-circuit
semantics are unchanged: the first false operand's assert raises before the
next step runs.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402


def _run(src: str, capsys) -> tuple[str, str]:
    ir = compile_source(src, "f.rvl", modules={})
    status, _summary = RUNNERS["py"](ir)
    return status, capsys.readouterr().out   # the FAIL detail prints to stdout


def test_a_failed_and_chain_reports_the_first_false_comparison_with_values(capsys):
    status, out = _run(
        'fn one() -> Int { return 1 }\ntest "and" {\n'
        '  let a = one()\n  let b = one()\n  assert a == 1 && b == 2\n}\n', capsys)
    assert status == "fail"
    assert "b == 2" in out          # WHICH operand was false
    assert "left  = 1" in out       # its comparison's values, as a single
    assert "right = 2" in out       # comparison already reported


def test_a_failed_and_chain_reports_the_first_operand_when_it_is_false(capsys):
    status, out = _run(
        'fn one() -> Int { return 1 }\ntest "and" {\n'
        '  let a = one()\n  let b = one()\n  assert a == 2 && b == 3\n}\n', capsys)
    assert status == "fail"
    assert "a == 2" in out and "b == 3" not in out


def test_a_passing_and_chain_passes(capsys):
    status, _out = _run(
        'fn one() -> Int { return 1 }\ntest "and" {\n'
        '  let a = one()\n  let b = one()\n  assert a == 1 && b == 1\n}\n', capsys)
    assert status == "pass"


def test_the_and_chain_still_short_circuits(capsys):
    """The second operand is not evaluated when the first is false: the
    failure is the assertion, never the operand's own error."""
    status, out = _run(
        'extern pure fn boom() -> Bool = @py { raise RuntimeError("evaluated") }\n'
        'test "t" {\n  assert false && boom()\n}\n', capsys)
    assert status == "fail"
    assert "assertion failed" in out
    assert "evaluated" not in out

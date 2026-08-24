"""A failing `assert` in a `test` block must say what the values were.

Before this, both backends threw away the operands the emitter already had:
python emitted a bare `assert`, so the runner (src/revl/test.py) had nothing
to print but "assertion failed", and TypeScript emitted
`expect(x).toBeTruthy()`, which reports `false` and no diff. Recovering the
actual values then cost whoever wrote the test a debugging session over
information the compiler held all along.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

SOURCE = """
pub fn add(a: Int, b: Int) -> Int { return a + b }

test "comparison" { assert add(2, 2) == 5 }
test "ordering" { assert add(2, 2) < 3 }
test "plain" { assert add(1, 0) > 0 }
"""


def _emit(backend: str) -> str:
    spec = importlib.util.spec_from_file_location(
        f"emit_{backend}_assertdiag", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(SOURCE, "assertdiag.rvl"))


def _run_python_tests():
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(_emit("python"), "assertdiag.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return dict(namespace["REVL_TESTS"])


def test_python_failing_comparison_reports_both_operands():
    tests = _run_python_tests()
    with pytest.raises(AssertionError) as failure:
        tests["comparison"]()
    message = str(failure.value)
    assert "add(2, 2) == 5" in message, message
    assert "left  = 4" in message, message
    assert "right = 5" in message, message


def test_python_failing_ordering_reports_both_operands():
    tests = _run_python_tests()
    with pytest.raises(AssertionError) as failure:
        tests["ordering"]()
    assert "left  = 4" in str(failure.value)
    assert "right = 3" in str(failure.value)


def test_python_operands_are_evaluated_once():
    """Each side is bound to a temporary and the comparison is made on those.

    The naive version re-renders the operand inside the message, which would
    call it twice — fine for a pure expression's result, wasteful for anything
    expensive, and wrong the moment the stratum grows anything observable. The
    property is structural: the `assert` compares temporaries, never the
    rendered call.
    """
    lines = [line.strip() for line in _emit("python").splitlines()]
    assert "_revl_lhs = add(2, 2)" in lines
    assert any(line.startswith("assert _revl_lhs == _revl_rhs") for line in lines)
    assert not any(line.startswith("assert add(2, 2)") for line in lines)


def test_python_passing_test_still_passes():
    _run_python_tests()["plain"]()


def test_typescript_equality_asserts_through_revl_equality():
    """Assertions must use the *language's* equality, not vitest's.

    This first shipped as `expect(l).toStrictEqual(r)`, for the diff. That was
    wrong once Float became IEEE: `toStrictEqual` uses Object.is and calls
    `NaN` equal to `NaN`, while revl's `==` says they differ — so a passing
    assertion could be testing the opposite of what the program means. It now
    routes through `revlEq` and carries both values in the message, which is
    what the matcher's diff was buying.
    """
    emitted = _emit("typescript")
    # item 143: the assert temporaries carry a `$` sigil so they cannot collide
    # with a user binding named `l`/`r` (a `let r = …` then `assert r == …`).
    assert "expect(revlEq($revl_l, $revl_r)" in emitted, emitted
    assert "function revlEq" in emitted
    # a non-equality comparison keeps the truthy form rather than guessing a
    # matcher for every operator
    assert "toBeTruthy()" in emitted

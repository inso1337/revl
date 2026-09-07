"""Issue #542: deeply-nested statements must be refused at parse time.

`if`/`while`/`for` bodies nest by recursion in the parser, and once a program
nested them a few hundred deep it PASSED parse and type-check — the frontend
raises its own `recursion_headroom` (12000 frames) to accept exactly this — and
then died with an uncaught `RecursionError` in every AST-walking emitter, which
runs at python's DEFAULT recursion limit. That surfaced as a raw traceback out
of `revl emit` (exit 1) and, through `revl.gate` admit, as an unhandled server
fault instead of a refusal.

The fix bounds statement nesting in the parser (`parser.NESTING_LIMIT`, the same
bound expression nesting already carries), so such input is refused cleanly with
a diagnostic that names the limit, well before any emitter is reached.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # `backends` is a namespace package at the root

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
import revl.parser as P  # noqa: E402


def _nested_ifs(depth: int) -> str:
    """A function whose body nests `depth` `if` statements, innermost `return`."""
    return ("fn f() -> Int { " + "if (true) { " * depth
            + "return 1" + " }" * depth + "\nreturn 0 }")


# The emitters started raising an uncaught RecursionError near this depth (at
# python's default recursion limit); the parser bound sits comfortably below it.
_CRASHED_THE_EMITTERS = 300


def test_deep_statement_nesting_is_a_diagnostic_not_a_recursion_error():
    """The 300-deep nest that used to pass the frontend and crash the emitters
    is now refused at parse time — a `RevlError`, never a `RecursionError`."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(_nested_ifs(_CRASHED_THE_EMITTERS), "issue_542.rvl")
    assert "nest" in str(excinfo.value)


def test_the_statement_nesting_bound_is_stated_in_the_refusal():
    """A limit nobody can read is a crash with better manners: the refusal
    names `NESTING_LIMIT`."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(_nested_ifs(_CRASHED_THE_EMITTERS), "issue_542.rvl")
    assert str(P.NESTING_LIMIT) in str(excinfo.value)


def test_deep_statement_nesting_never_reaches_an_emitter():
    """`compile_source` runs parse + check + lower; the refusal fires in the
    parser, so the RecursionError-prone emitters are never handed the tree.
    (Belt and braces: assert no RecursionError escapes.)"""
    try:
        compile_source(_nested_ifs(_CRASHED_THE_EMITTERS), "issue_542.rvl")
    except RevlError:
        pass
    except RecursionError:  # pragma: no cover - the bug this pins
        pytest.fail("statement nesting reached the recursion limit instead of "
                    "being refused at parse time")


@pytest.mark.parametrize("depth", [10, 50, 150])
def test_ordinary_statement_nesting_still_compiles_and_emits(depth):
    """The bound sits far above anything real code writes: a program nesting
    statements this deep still compiles AND emits on every backend without a
    RecursionError."""
    import importlib

    ir = compile_source(_nested_ifs(depth), "issue_542.rvl")
    for backend in ("python", "rust", "typescript", "wasm"):
        module = importlib.import_module(f"backends.{backend}.emit")
        module.emit(ir)  # must not raise RecursionError

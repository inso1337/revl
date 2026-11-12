"""`/` over two `Int`s on the wasm tier: TRUE division, never `i64.div_s`.

Issue #2201. `Int / Int` is TRUE division, so `7 / 2` is `3.5`
(docs/arithmetic.md §"`/` is true division"). The tier used to refuse it by name
because it had no Float value representation. Float is a native f64 value now,
so `/` lowers to `f64.div` over converted operands and answers `3.5`; the bad
lowering this file guards against is `i64.div_s`, which answered a silent `3`
and trapped at `7/0`. Integer division keeps its own spellings
(`div_trunc`/`div_floor`/`div_euclid`, and `%` as `i64.rem_s`).

Both emit paths are exercised: the v3 fn body and the component provide-method
body. Non-over-refusal is pinned too: `%` over two Ints keeps `i64.rem_s`, the
named integer division keeps `i64.div_s`, and a Float `/` still reaches
`f64.div`.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))

from _backend_import import backend_emitter  # noqa: E402

#: (a) the v3 fn-body path: the issue's own program, whose body interpolates the
#: quotient of two Ints. The quotient is a Float value now (rendering a
#: non-integer Float is the separate `$f64_to_str` fence, so this module is
#: checked for lowering, not invoked).
FN_SRC = """
fn half(a: Int, b: Int) -> Str { return `${a / b}` }
"""

#: (b) the provide-method body path, which routed through
#: `_scalar_operator` -> `_bin_instr` just the same.
COMPONENT_SRC = """
service Calc {
  fn half(a: Int, b: Int) -> Str
}

component CalcImpl provides calc: Calc {
  provide calc {
    fn half(a: Int, b: Int) -> Str { return `${a / b}` }
  }
}
"""

#: (c) `%` is Int-only and must keep its native instruction.
REM_SRC = """
fn rem(a: Int, b: Int) -> Int { return a % b }
"""


def _emit(source: str):
    return backend_emitter("wasm").emit(compile_source(source))


def _wat(source: str) -> str:
    emitted = _emit(source)
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


def test_fn_body_true_division_lowers_to_f64():
    """(a) A `fn` body interpolating `${a / b}` over two Ints lowers the
    operands through `f64.convert_i64_s` and divides with `f64.div`; the
    integer-division instruction must not appear."""
    wat = _wat(FN_SRC)
    assert "(f64.convert_i64_s" in wat, wat
    assert "(f64.div)" in wat, wat
    assert "(i64.div_s" not in wat, wat


def test_provide_method_body_true_division_lowers_to_f64():
    """(b) The component provide-method body path takes the same lowering; both
    paths emitted `(i64.div_s)` before issue #2201."""
    wat = _wat(COMPONENT_SRC)
    assert "(f64.convert_i64_s" in wat, wat
    assert "(f64.div)" in wat, wat
    assert "(i64.div_s" not in wat, wat


@pytest.mark.skipif(shutil.which("wasmtime") is None, reason="wasmtime not installed")
def test_true_division_answers_three_point_five_on_wasmtime(tmp_path):
    """The executed half: `7 / 2` returns 3.5, not the old silent 3."""
    wat = _wat("pub fn half(a: Int, b: Int) -> Float = a / b")
    path = tmp_path / "half.wat"
    path.write_text(wat, encoding="utf-8")
    out = subprocess.run(["wasmtime", "--invoke", "half", str(path), "7", "2"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert float(out.stdout.strip().splitlines()[-1]) == 3.5


def test_int_remainder_still_lowers():
    """(c) No over-refusal: `%` over two Ints is Int-only, and keeps the native
    `i64.rem_s` (which traps at a zero divisor, the fault every tier gives)."""
    wat = _wat(REM_SRC)
    assert "(i64.rem_s)" in wat, wat


def test_integer_division_builtins_keep_their_instructions():
    """`div_trunc` is a real `i64.div_s` and must stay one: the change is about
    the `/` OPERATOR, not about integer division."""
    wat = _wat("fn t(a: Int, b: Int) -> Int { return a.div_trunc(b) }")
    assert "(i64.div_s" in wat, wat
    emitter = backend_emitter("wasm")
    # `/` is never lowered by `_bin_instr`; the Float path handles it.
    assert emitter._bin_instr("/", "Int") is None
    assert emitter._bin_instr("/", "Int32") is None
    assert "/" not in emitter._RAW_INT_OPS


def test_float_operand_division_still_lowers():
    """`Float / Float` reaches `f64.div`, and an Int-operand `/` widens to it
    rather than refusing."""
    wat = _wat("fn f() -> Str { return `${1.5 / 2.5}` }")
    assert "(f64.div)" in wat, wat

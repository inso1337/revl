"""`/` over two `Int`s on the wasm tier: refuse by name, never `i64.div_s`.

Issue #2201. `Int / Int` is TRUE division, so `7 / 2` is `3.5` (docs/arithmetic.md
§"`/` is true division"). The wasm tier lowers `Int`/`Bool` and refuses `Float`
by name, and it was refusing everything Float EXCEPT the one position it does
lower a Float in — a `${…}` interpolation part feeding `$f64_to_str`. There,
`_RAW_INT_OPS` lowered `/` to `i64.div_s` and the module answered a silent `3`,
`-3`, and trapped at `7/0`. That is the wrong direction for a tier whose whole
contract is to fail closed with a named refusal, and `f64.div` is no better:
`$f64_to_str` traps on a finite float that is not integer-valued
(docs/strings.md §"Remaining wasm WAT work"), where the language documents `/`
as total. So `/` is refused by name, exactly as `Float` is everywhere else on
this tier, and the integer divisions keep their own spellings
(`div_trunc`/`div_floor`/`div_euclid`, and `%` as `i64.rem_s`).

Both emit paths had it — the v3 fn body and the component provide-method body —
so both are pinned here, plus the non-over-refusal side (`%` still lowers, and a
Float-operand `/` in the renderer position is untouched).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))

from _backend_import import backend_emitter  # noqa: E402

#: (a) the v3 fn-body path: the issue's own program, whose body interpolates the
#: quotient of two Ints.
FN_SRC = """
fn half(a: Int, b: Int) -> Str { return `${a / b}` }
"""

#: (b) the provide-method body path, which emitted `i64.div_s` through
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

#: The generic guard's wording, which a Float-named refusal must NOT be: that
#: message says nothing about Float and hides the tier's documented contract.
GENERIC = "only lowerable for Int/Int32"


def _emit(source: str):
    return backend_emitter("wasm").emit(compile_source(source))


def _wat(source: str) -> str:
    emitted = _emit(source)
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


def test_fn_body_true_division_refuses_by_name():
    """(a) A `fn` body interpolating `${a / b}` over two Ints is refused at emit
    time, and the refusal names `Float` — not the generic operator guard."""
    with pytest.raises(ValueError) as excinfo:
        _emit(FN_SRC)
    message = str(excinfo.value)
    assert "Float" in message, message
    assert GENERIC not in message, message


def test_provide_method_body_true_division_refuses_by_name():
    """(b) The component provide-method body path refuses the same way. Both
    paths emitted `(i64.div_s)` before this fix."""
    with pytest.raises(ValueError) as excinfo:
        _emit(COMPONENT_SRC)
    message = str(excinfo.value)
    assert "Float" in message, message
    assert GENERIC not in message, message


def test_int_remainder_still_lowers():
    """(c) No over-refusal: `%` over two Ints is Int-only, and keeps the native
    `i64.rem_s` (which traps at a zero divisor, the fault every tier gives)."""
    wat = _wat(REM_SRC)
    assert "(i64.rem_s)" in wat, wat


def test_no_module_is_emitted_for_true_division_over_ints():
    """(d) No module this tier emits for a `/` over Ints contains an
    `i64.div_s` produced by `/`.

    Asserted on the raised-refusal path (the choice this file makes): `emit`
    raises, so it returns no module at all for either path — there are no bytes
    to inspect — and the white-box half pins the same fact at the decision point,
    so a later change cannot reintroduce the instruction under a different error.
    `test_integer_division_builtins_keep_their_instructions` is the positive
    control: the instruction still exists, reached by `div_trunc`.
    """
    for src in (FN_SRC, COMPONENT_SRC):
        with pytest.raises(ValueError):
            _emit(src)
    emitter = backend_emitter("wasm")
    assert emitter._bin_instr("/", "Int") is None
    assert emitter._bin_instr("/", "Int32") is None
    assert "/" not in emitter._RAW_INT_OPS


def test_integer_division_builtins_keep_their_instructions():
    """The companion to (c): `div_trunc` is a real `i64.div_s` and must stay
    one — the refusal is about the `/` OPERATOR, not about integer division."""
    wat = _wat("fn t(a: Int, b: Int) -> Int { return a.div_trunc(b) }")
    assert "(i64.div_s" in wat, wat


def test_float_operand_division_in_the_renderer_position_still_lowers():
    """The one position this tier does lower a Float in — a `${…}` part — is
    untouched: `Float / Float` still reaches `f64.div`, so the fix did not
    over-reach into the Float-operand branch."""
    wat = _wat("fn f() -> Str { return `${1.5 / 2.5}` }")
    assert "(f64.div)" in wat, wat

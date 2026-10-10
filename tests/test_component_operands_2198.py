"""The component stratum annotates arithmetic `operands`, as the fn stratum does.

Issue #2198 (emitter/quality audit QUAL-01). `_lower_pure_expr`'s `ExprBin` arm
records `node["operands"]` for the typed arithmetic operators, and every tier's
renderer keys on it: it is what makes `Int / Int` true division and `%` a
TRUNCATED remainder (docs/arithmetic.md) instead of each host language's own
operator. `_lower_component_pure_expr` — the second, drifted copy of that arm —
kept only the `Str` relational annotation from item 458 and dropped the
arithmetic one, so the same `/`, `%`, `+`, `-`, `*` written inside a `provide`
method, an activation `let`-effect argument or an `effect` argument reached
every backend UNANNOTATED. Each then fell back to its host operator: `-7 % 2`
was `1` on python and `-1` on java, go failed to build and rust failed to
type-check, for one program.

Both strata now annotate through ONE helper (`_operand_annotation` in
`src/revl/lower.py`), so the annotation cannot drift again — this file pins
that by asserting the two strata's IR side by side rather than each in
isolation.

Asserted here:
  a. `/` and `%` over two Ints inside a provide method AND inside an activation
     `effect` argument carry `operands == "Int"`, exactly as the same
     expression in a module `fn` does;
  b. the python tier EXECUTES the provide-method program and returns the
     fn-stratum answers, with the fn stratum executed alongside it;
  c. the emitted python for an activation `effect` argument holding `+` no
     longer renders the raw host operator.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests", ROOT / "backends" / "python"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

emit = backend_emitter("python")

#: `docs/arithmetic.md`: `Int % Int` is the remainder truncated toward zero (the
#: sign of the DIVIDEND) and `Int / Int` is IEEE true division. So the answers
#: this program must produce are `-1` and `-3.5` on every tier.
DIVIDEND = -7
DIVISOR = 2
TRUNCATED_REMAINDER = -1
TRUE_QUOTIENT = -3.5

#: The issue's evidence verbatim: `config { a: Int = -7, b: Int = 2 }` read from
#: inside a provide method.
PROVIDE_SOURCE = """
service Arith {
  fn rem() -> Int
  fn quo() -> Float
}
component A provides arith: Arith {
  config { a: Int = -7, b: Int = 2 }
  provide arith {
    fn rem() { return config.a % config.b }
    fn quo() { return config.a / config.b }
  }
}
"""

#: The same two expressions in the stratum that never drifted, so the test
#: asserts agreement instead of two independent spellings of one expectation.
FN_SOURCE = """
pub fn rem(a: Int, b: Int) -> Int { return a % b }
pub fn quo(a: Int, b: Int) -> Float { return a / b }
"""

#: The same arithmetic as ACTIVATION `effect` arguments — the second component
#: position the item names (an activation body has no plain `let`: G6 records
#: effects, so the value expression reaches the component stratum through the
#: effect's argument).
EFFECT_SOURCE = """
type Slot = Opaque
extern acquire fn open_slot(n: Int) -> Slot undo close_slot(result) = @py { return n }
extern pure fn close_slot(t: Slot) -> Unit = @py { return }
extern acquire fn open_ratio(f: Float) -> Slot undo close_slot(result) = @py { return f }

service Arith { fn tag() -> Int }
component A provides arith: Arith {
  config { a: Int = -7, b: Int = 2 }
  let t1 = effect open_slot(config.a + config.b) undo close_slot(t1)
  let t2 = effect open_slot(config.a % config.b) undo close_slot(t2)
  let t3 = effect open_ratio(config.a / config.b) undo close_slot(t3)
  provide arith { fn tag() { return 1 } }
}
"""

#: The same three operators in a module `fn`.
FN_SOURCE_THREE = """
pub fn plus(a: Int, b: Int) -> Int { return a + b }
pub fn rem(a: Int, b: Int) -> Int { return a % b }
pub fn quo(a: Int, b: Int) -> Float { return a / b }
"""

#: `operands` is a decision, not a constant: a Float operand selects the Float
#: path (no Int overflow trap, no truncated remainder), in BOTH strata.
FN_SOURCE_FLOAT = """
pub fn scaled(x: Float, y: Int) -> Float { return x / y }
"""

FLOAT_SOURCE = """
service Ratio { fn scaled(x: Float, y: Int) -> Float }
component R provides ratio: Ratio {
  provide ratio {
    fn scaled(x, y) { return x / y }
  }
}
"""

ARITHMETIC_OPS = ("/", "%", "+", "-", "*")

_cordis_only = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the component body runs on the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)


def _arithmetic_operands(source: str) -> list[tuple[str, object]]:
    """Every arithmetic `bin` node's `(op, operands)` in a compiled program."""
    found: list[tuple[str, object]] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("kind") == "bin" and node.get("op") in ARITHMETIC_OPS:
                found.append((node["op"], node.get("operands")))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(compile_source(source, "t.rvl"))
    return sorted(found, key=lambda pair: (ARITHMETIC_OPS.index(pair[0]),
                                          pair[1] or ""))


# -- (a) the annotation is on both sides ------------------------------------

def test_module_fn_stratum_annotates_two_ints():
    assert _arithmetic_operands(FN_SOURCE) == [("/", "Int"), ("%", "Int")]


def test_provide_method_stratum_annotates_exactly_as_the_fn_stratum():
    # the component side is compared against the fn side, not against a second
    # hand-written expectation: the point of the item is that ONE decision
    # reaches both
    assert _arithmetic_operands(PROVIDE_SOURCE) == _arithmetic_operands(FN_SOURCE)
    assert _arithmetic_operands(PROVIDE_SOURCE) == [("/", "Int"), ("%", "Int")]


def test_effect_argument_stratum_annotates_exactly_as_the_fn_stratum():
    assert _arithmetic_operands(EFFECT_SOURCE) == _arithmetic_operands(FN_SOURCE_THREE)
    assert _arithmetic_operands(EFFECT_SOURCE) == [
        ("/", "Int"), ("%", "Int"), ("+", "Int")]


def test_a_float_operand_is_annotated_float_in_both_strata():
    # non-vacuity: the annotation discriminates, it is not `Int` everywhere
    assert _arithmetic_operands(FN_SOURCE_FLOAT) == [("/", "Float")]
    assert _arithmetic_operands(FLOAT_SOURCE) == _arithmetic_operands(FN_SOURCE_FLOAT)


# -- (b) the provide method EXECUTES with the fn stratum's answers -----------

def _fn_stratum_answers() -> tuple[int, float]:
    """Run the module-`fn` program on python: the answers of the stratum whose
    renderer was never broken."""
    namespace: dict = {}
    exec(compile(emit.emit(compile_source(FN_SOURCE, "t.rvl")), "<fn>", "exec"),
         namespace)
    return namespace["rem"](DIVIDEND, DIVISOR), namespace["quo"](DIVIDEND, DIVISOR)


def test_py_fn_stratum_answers_are_the_contract():
    # the baseline the provide-method program is held to, measured rather than
    # written down twice
    assert _fn_stratum_answers() == (TRUNCATED_REMAINDER, TRUE_QUOTIENT)


async def _activate(source: str, component: str, key: str):
    """Plug `component` on the cordis-py runtime, settle, and return its
    provided service (`backends/python/tests/test_router_scenario.py`)."""
    import asyncio  # noqa: PLC0415

    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415

    module_src = emit.emit(compile_source(source, "t.rvl"))
    module = types.ModuleType("component_operands_exec")
    exec(compile(module_src, "component_operands_exec.py", "exec"), module.__dict__)
    root = Context()
    runtime_mod.plug(root, getattr(module, component))
    for _ in range(10):
        await asyncio.sleep(0)
    return root.reflect.get(key)


@_cordis_only
async def test_py_provide_method_answers_match_the_fn_stratum():
    arith = await _activate(PROVIDE_SOURCE, "A", "arith")
    assert (arith.rem(), arith.quo()) == _fn_stratum_answers()
    # and the answers are the contract's, not merely equal to each other
    assert arith.rem() == TRUNCATED_REMAINDER
    assert arith.quo() == TRUE_QUOTIENT


# -- the emitted python, the site of the host-operator fallback --------------

def test_py_provide_method_calls_the_shared_arithmetic_helpers():
    code = emit.emit(compile_source(PROVIDE_SOURCE, "t.rvl"))
    assert ("return _revl_rem(_revl_config['a'], _revl_config['b'])"
            in code)
    assert ("return _revl_div(_revl_config['a'], _revl_config['b'])"
            in code)


# -- (c) an activation `effect` argument no longer takes the host operator ---

def test_py_effect_argument_plus_goes_through_the_annotated_path():
    code = emit.emit(compile_source(EFFECT_SOURCE, "t.rvl"))
    raw = "open_slot((_revl_config['a'] + _revl_config['b']))"
    # the pre-fix rendering, verbatim: the argument handed straight to the host
    assert raw not in code
    # and the expression is still there — inside the bounded (overflow-trapping)
    # form the annotation selects
    assert "_revl_config['a'] + _revl_config['b']" in code
    assert "_revl_i64" in code
    assert "open_slot(_revl_rem(_revl_config['a'], _revl_config['b']))" in code
    assert "open_ratio(_revl_div(_revl_config['a'], _revl_config['b']))" in code

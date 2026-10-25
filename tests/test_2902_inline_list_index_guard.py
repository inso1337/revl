"""The python tier inlines the negative-index guard of a List subscript (#2902).

`xs[i]` used to route through a preamble function `_revl_index(xs, i)`, a call
on every list read. The guard (#549: python would read a negative index from
the end, every other tier faults) now sits inside the subscript, and only the
fault it raises is a function.

What this file pins:

  * the emitted module has no `_revl_index(` call at a subscript;
  * the inlined form raises the same error type and message as the old helper
    for a negative index and for an index past the end, on every index shape
    the emitter spells differently (a variable, a literal, an expression);
  * the list expression and the index expression are each evaluated exactly
    once, the list first, whether or not the guard fires (an index that is a
    call is the case that would show a double evaluation);
  * nested subscripts and an awaited match arm still compile and answer.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

SOURCE = """
extern pure fn mk() -> List[Int] = @py { CALLS.append(('mk',)); return [10, 20, 30] }
extern pure fn probe(i: Int) -> Int = @py { CALLS.append(('probe', i)); return i }
extern pure fn grid() -> List[List[Int]] = @py { CALLS.append(('grid',)); return [[1, 0], [0, 1]] }
pub fn by_call(i: Int) -> Int { return mk()[probe(i)] }
pub fn by_var(xs: List[Int], i: Int) -> Int { return xs[i] }
pub fn by_lit(xs: List[Int]) -> Int { return xs[2] }
pub fn by_expr(xs: List[Int], i: Int) -> Int { return xs[i + 1] }
pub fn nested(g: List[List[Int]], i: Int) -> Int { return g[i][g[i][0]] }
pub fn nested_call(i: Int) -> Int { return grid()[probe(i)][probe(0)] }
"""

# What the removed helper did, kept here as the reference for "same error".
NEG_MESSAGE = "revl: negative list index"


def _old_helper(xs, i):
    if i < 0:
        raise IndexError(NEG_MESSAGE)
    return xs[i]


def _emitted(source: str = SOURCE) -> str:
    emitted = backend_emitter("python").emit(compile_source(source, "inline_guard_2902.rvl"))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


def _module(source: str = SOURCE) -> dict:
    calls: list = []
    ns: dict = {"CALLS": calls}
    exec(compile(_emitted(source), "<inline_guard_2902>", "exec"), ns)
    ns["_calls"] = calls
    return ns


def _outcome(fn, *args):
    try:
        return ("value", fn(*args))
    except Exception as exc:  # noqa: BLE001 - the type and message ARE the datum
        return ("error", type(exc), exc.args)


# ------------------------------------------------------------------ static

def test_no_subscript_goes_through_a_helper_call():
    src = _emitted()
    assert "_revl_index(" not in src, (
        "a List subscript still calls the preamble helper; the guard is meant "
        "to be inline")
    assert "def _revl_neg_index" in src
    assert src.count("_revl_neg_index()") >= 4


def test_a_program_with_no_list_subscript_carries_no_guard_helper():
    src = _emitted("pub fn f(n: Int) -> Int { return n + 1 }")
    assert "_revl_neg_index" not in src


# --------------------------------------------------------------- error parity

XS = [10, 20, 30]


@pytest.mark.parametrize("i", [-1, -3, -4, -100, 0, 1, 2, 3, 4, 4096])
def test_variable_index_matches_the_old_helper(i):
    ns = _module()
    assert _outcome(ns["by_var"], XS, i) == _outcome(_old_helper, XS, i)


@pytest.mark.parametrize("i", [-2, -1, 0, 1, 2, 5])
def test_expression_index_matches_the_old_helper(i):
    ns = _module()
    assert _outcome(ns["by_expr"], XS, i) == _outcome(_old_helper, XS, i + 1)


@pytest.mark.parametrize("xs", [[10, 20, 30], [1], []])
def test_literal_index_matches_the_old_helper(xs):
    ns = _module()
    assert _outcome(ns["by_lit"], xs) == _outcome(_old_helper, xs, 2)


def test_the_negative_and_past_the_end_errors_are_the_documented_ones():
    ns = _module()
    assert _outcome(ns["by_var"], XS, -1) == ("error", IndexError, (NEG_MESSAGE,))
    kind, etype, args = _outcome(ns["by_var"], XS, 3)
    assert (kind, etype) == ("error", IndexError)
    assert args != (NEG_MESSAGE,)


# ------------------------------------------------------------ single evaluation

@pytest.mark.parametrize("i", [1, 2, -1, 7])
def test_a_call_index_is_evaluated_once_and_after_the_list(i):
    ns = _module()
    expect = _outcome(_old_helper, [10, 20, 30], i)
    assert _outcome(ns["by_call"], i) == expect
    assert ns["_calls"] == [("mk",), ("probe", i)], (
        "the list expression and the index call must each run exactly once, "
        "list first, on the value path and on both fault paths")


@pytest.mark.parametrize("i", [0, 1, -1, 5])
def test_nested_subscripts_evaluate_every_operand_once(i):
    ns = _module()
    grid = [[1, 0], [0, 1]]
    # the old spelling: _revl_index(_revl_index(grid(), probe(i)), probe(0))
    want = _outcome(lambda: _old_helper(_old_helper(grid, i), 0))
    assert _outcome(ns["nested_call"], i) == want
    assert ns["_calls"].count(("grid",)) == 1
    probes = [c for c in ns["_calls"] if c[0] == "probe"]
    # probe(0) is only reached when the outer subscript did not fault
    assert probes == ([("probe", i), ("probe", 0)] if want[0] == "value"
                      else [("probe", i)])


@pytest.mark.parametrize("i", [0, 1, -1, 2])
def test_nested_variable_subscripts_match_the_old_helper(i):
    ns = _module()
    g = [[1, 0], [0, 1]]

    def old(g, i):
        return _old_helper(_old_helper(g, i), _old_helper(_old_helper(g, i), 0))

    assert _outcome(ns["nested"], g, i) == _outcome(old, g, i)


# ----------------------------------------------------- scopes the walrus must survive

ASYNC_SOURCE = """
extern emission async fn twice(n: Int) -> Int = @py { return n * 2 }
fn wrap(n: Int) -> Opt[Int] { return Some(n) }
pub fn pick(xs: List[Int], i: Int) -> Int {
  return match wrap(xs[i]) { Some(v) => twice(xs[v - 10]), None => 0 }
}
"""


def test_an_arm_bound_to_the_guard_temp_name_still_runs():
    """`_revl_i` is a walrus target, so an awaited arm that binds that name must
    keep the walrus binder (a comprehension may not rebind a walrus target)."""
    source = ASYNC_SOURCE.replace(
        "Some(v) => twice(xs[v - 10])", "Some(_revl_i) => twice(xs[_revl_i - 10])")
    ns = _module(source)
    assert asyncio.run(ns["pick"]([1, 11, 3], 1)) == 22


def test_an_awaited_match_arm_over_subscripts_still_runs():
    ns = _module(ASYNC_SOURCE)
    xs = [1, 11, 3]
    assert asyncio.run(ns["pick"](xs, 1)) == 22


COMPONENT_SOURCE = """
service Probe { fn at(xs: List[Int], i: Int) -> Int }
component C provides probe: Probe {
  provide probe { fn at(xs: List[Int], i: Int) = xs[i] }
}
"""


def test_a_component_body_subscript_carries_the_inline_guard():
    src = _emitted(COMPONENT_SOURCE)
    assert "_revl_index(" not in src
    assert "i if i >= 0 else _revl_neg_index()" in src


# ------------------------------------------------- the self-hosted emitter agrees

def test_the_selfhosted_emitter_spells_every_index_shape_like_the_reference():
    """selfhost/emit_py.rvl must stay byte-identical (roadmap item 429) on the
    shapes the corpus does not all reach: a call target (parenthesized), a call
    index and a nested subscript (walrus form), a variable index, a literal."""
    import test_selfhost_emit_py as shpy

    namespace = shpy._exec_emitted()
    reference = shpy._load_reference_emit()
    for source in (SOURCE, ASYNC_SOURCE, COMPONENT_SOURCE):
        ir = compile_source(source, "inline_guard_2902.rvl")
        assert namespace["emit_py_src"](ir) == reference.emit(ir)

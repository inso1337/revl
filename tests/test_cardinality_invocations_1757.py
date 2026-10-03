"""Issue #1757: an emitting arrow crosses once per INVOCATION, not once per
literal (item 260 Slice 1, docs/design/260 §2.1).

Before, `count_expr` summed an arrow literal's crossings once wherever the
literal sat, so a helper that called its arrow twice, or a `let`-bound arrow
called twice, reported `{"bound": 1}`: a proved ceiling that was wrong. Now a
bound arrow is counted at each call of its name, an arrow handed to a top-level
fn is counted as many times as that fn invokes the parameter on one path, and
an emitting arrow anywhere the fold cannot follow is `unbounded`.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.cardinality import cardinality  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

HEAD = """
type Steps = { step: (List[Str]) -> Str }
service Model { emission[model] fn complete(m: List[Str]) -> Str }
service Loop { emission[model] fn run(s: Str) -> Str }
"""


def _model(helpers: str, body: str):
    """The `model` ceiling of a component whose provide method runs `body`."""
    src = HEAD + helpers + """
component Agent requires model: Model provides agent: Loop {
  provide agent { fn run(x) -> Str {
BODY
  } }
}
""".replace("BODY", body)
    card = cardinality(compile_source(src, "t.rvl"))
    return (card.get("Agent") or {}).get("per_capability", {}).get("model")


ARROW = "m => emit model.complete(m)"


# ------------------------------------------- counted once per invocation

def test_a_helper_that_invokes_its_arrow_twice_counts_two():
    helpers = """
fn twice(msgs: List[Str], step: (List[Str]) -> Str) -> Str {
  let a = step(msgs)
  return step(msgs)
}
"""
    assert _model(helpers, f"return twice([x], {ARROW})") == {"bound": 2,
                                                              "kind": "bounded"}


def test_a_helper_counts_the_worst_path_not_every_arm():
    """Only one branch runs, so the ceiling is the worse branch (3), not the
    sum of both (4). An early `return` with no `else` is over-approximated as
    falling through, which can only raise the ceiling, never lower it."""
    helpers = """
fn pick(msgs: List[Str], step: (List[Str]) -> Str, deep: Bool) -> Str {
  if (deep) {
    let a = step(msgs)
    let b = step(msgs)
    return step(msgs)
  } else {
    return step(msgs)
  }
}
"""
    assert _model(helpers, f"return pick([x], {ARROW}, true)") == {
        "bound": 3, "kind": "bounded"}


def test_a_let_bound_arrow_called_twice_counts_two():
    body = f"""    let f = {ARROW}
    let a = f([x])
    return f([x])"""
    assert _model("", body) == {"bound": 2, "kind": "bounded"}


def test_a_let_bound_arrow_handed_to_a_helper_counts_its_invocations():
    helpers = """
fn thrice(msgs: List[Str], step: (List[Str]) -> Str) -> Str {
  let a = step(msgs)
  let b = step(msgs)
  return step(msgs)
}
"""
    body = f"""    let f = {ARROW}
    return thrice([x], f)"""
    assert _model(helpers, body) == {"bound": 3, "kind": "bounded"}


def test_a_let_bound_arrow_handed_to_a_certified_loop_is_folded():
    """The Slice 2 resolver reads a bound arrow's crossings too: before, the
    name argument counted nothing and the literal counted once."""
    helpers = """
fn run_loop(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let r = step(msgs)
  return run_loop(msgs, step, n - 1)
}
"""
    body = f"""    let f = {ARROW}
    return run_loop([x], f, 5)"""
    assert _model(helpers, body) == {"bound": 5, "kind": "bounded"}


# ------------------------------------------- unbounded, never undercounted

ESCAPES = {
    "alias in the helper": ("""
fn via(msgs: List[Str], step: (List[Str]) -> Str) -> Str {
  let g = step
  let a = g(msgs)
  return g(msgs)
}
""", f"return via([x], {ARROW})"),
    "record field in the helper": ("""
fn via(msgs: List[Str], s: Steps) -> Str {
  let g = s.step
  let a = g(msgs)
  return g(msgs)
}
""", f"return via([x], {{ step: {ARROW} }})"),
    "passed on to another fn": ("""
fn inner(msgs: List[Str], step: (List[Str]) -> Str) -> Str { return step(msgs) }
fn via(msgs: List[Str], step: (List[Str]) -> Str) -> Str {
  let a = inner(msgs, step)
  return inner(msgs, step)
}
""", f"return via([x], {ARROW})"),
    "stored in a list in the method": ("", f"""    let fs = [{ARROW}]
    let f = fs[0]
    let a = f([x])
    return f([x])"""),
}


@pytest.mark.parametrize("shape", sorted(ESCAPES))
def test_an_arrow_whose_invocations_cannot_be_counted_is_unbounded(shape):
    helpers, body = ESCAPES[shape]
    entry = _model(helpers, body)
    assert entry is not None, "the crossing was dropped"
    assert entry["kind"] == "unbounded", entry
    assert "invocations cannot be counted" in entry["reason"]


def test_a_helper_that_invokes_its_arrow_in_a_loop_is_unbounded():
    helpers = """
fn spin(msgs: List[Str], step: (List[Str]) -> Str) -> Str {
  var i = 0
  var r = ""
  while (i < 3) {
    r = step(msgs)
    i = i + 1
  }
  return r
}
"""
    entry = _model(helpers, f"return spin([x], {ARROW})")
    assert entry["kind"] == "unbounded"
    assert "inside a loop" in entry["reason"]


# ------------------------------------------- controls: unchanged counts

def test_one_invocation_is_still_one():
    helpers = """
fn once(msgs: List[Str], step: (List[Str]) -> Str) -> Str { return step(msgs) }
"""
    assert _model(helpers, f"return once([x], {ARROW})") == {"bound": 1,
                                                             "kind": "bounded"}


def test_a_crossing_in_a_data_argument_is_evaluated_once():
    helpers = """
fn echo(s: Str, k: Int) -> Str { return s }
"""
    body = "    return echo(emit model.complete([x]), 2)"
    assert _model(helpers, body) == {"bound": 1, "kind": "bounded"}


def test_a_direct_emit_is_unchanged():
    body = "    let a = emit model.complete([x])\n    return emit model.complete([x])"
    assert _model("", body) == {"bound": 2, "kind": "bounded"}

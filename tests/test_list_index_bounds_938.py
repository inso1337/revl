"""Static `List` index bounds (issue #938, roadmap item 485).

`["a"][5]` and `xs[0 - 1]` (with `xs` a list literal in scope) used to compile
and then fault at runtime — and not uniformly: py/go/rust/java raise, ts reads
`undefined` and wasm reads `0`. The `Str` arm of the checker already refused a
literal index on a known receiver (`"ab"[9]` is a coded `T1`), so the silence on
`List` read as a gap rather than a decision.

The close is the same refusal, applied where the length is knowable: an index
that folds to an integer literal against a `List` whose length the checker can
see is refused at the frontend, before any emitter runs
(`check_list_index_bounds` in `src/revl/lower.py`).

What this file pins, in both directions:

  * every shape that must be REFUSED, with the code and the line;
  * every shape that must still be ACCEPTED — in particular `xs[i]`, the index
    the checker cannot fold, which issue #938 explicitly leaves alone. A pass
    that refused it would be a bounds analysis, and a wrong one.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _refuse(source: str, filename: str = "bounds.rvl") -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, filename)
    return excinfo.value


def _accept(source: str, filename: str = "bounds.rvl") -> None:
    compile_source(source, filename)


# ------------------------------------------------------- refused: the bound case

def test_literal_index_on_a_list_literal_is_refused():
    err = _refuse('pub fn f() -> Str { return ["a"][5] }')
    assert "index 5 is out of range for a 1-element `List`" in err.message
    assert "0 .. 0" in err.message


def test_literal_index_on_a_let_bound_list_literal_is_refused():
    err = _refuse('pub fn f() -> Str { let xs = ["a"]\n  return xs[5] }')
    assert "index 5 is out of range for a 1-element `List`" in err.message


def test_negative_index_is_refused_and_says_why():
    err = _refuse('pub fn f() -> Str { let xs = ["a"]\n  return xs[0 - 1] }')
    assert "index -1 is out of range for a 1-element `List`" in err.message
    # the negative case has its own reading: no wrap, no end-relative
    assert "negative index" in err.message
    assert "§549" in err.message


def test_a_folded_negative_index_is_refused():
    # `0 - 1` is a `bin` node, not a literal, so the fold is what catches it
    err = _refuse('pub fn f() -> Str { let xs = ["a", "b"]\n  return xs[1 - 2] }')
    assert "index -1 is out of range for a 2-element `List`" in err.message


def test_a_folded_product_index_is_refused():
    err = _refuse('pub fn f() -> Str { let xs = ["a"]\n  return xs[2 * 3] }')
    assert "index 6 is out of range for a 1-element `List`" in err.message


def test_an_empty_list_refuses_every_index():
    err = _refuse('pub fn f() -> Str { let xs: List[Str] = []\n  return xs[0] }')
    assert "index 0 is out of range for a 0-element `List`" in err.message
    assert "it is empty" in err.message


def test_the_refusal_is_coded_and_carries_the_line():
    err = _refuse('pub fn f() -> Str {\n  let xs = ["a"]\n  return xs[5]\n}')
    assert err.code == "T1"
    assert err.category == "type-mismatch"
    assert err.line == 3


def test_the_refusal_carries_a_hint():
    err = _refuse('pub fn f() -> Str { return ["a"][5] }')
    assert "length" in err.hint
    assert "docs/stdlib-2.0.md" in err.hint


# --------------------------------------------- refused: nested and scoped shapes

def test_a_refusal_inside_an_if_arm_is_found():
    err = _refuse(
        'pub fn f(n: Int) -> Str {\n'
        '  if (n > 0) { return ["a"] } else { return ["a", "b", "c"][9] }\n'
        '}'
    )
    assert "index 9 is out of range for a 3-element `List`" in err.message


def test_a_refusal_inside_a_while_body_is_found():
    err = _refuse(
        'pub fn f(n: Int) -> Str {\n'
        '  let xs = ["a"]\n'
        '  while (n > 0) { n = n - 1  return xs[4] }\n'
        '  return xs[0]\n'
        '}'
    )
    assert "index 4 is out of range for a 1-element `List`" in err.message


def test_a_refusal_inside_a_for_body_is_found():
    err = _refuse(
        'pub fn f(ys: List[Int]) -> Int {\n'
        '  let xs = ["a"]\n'
        '  for (y of ys) { return xs[y] }\n'
        '  return xs[3]\n'
        '}'
    )
    assert "index 3 is out of range for a 1-element `List`" in err.message


def test_a_refusal_inside_a_match_block_arm_is_found():
    err = _refuse(
        'pub fn f(o: Opt[Int]) -> Str {\n'
        '  return match o {\n'
        '    Some(v) => { let xs = ["a"]\n      xs[v] },\n'
        '    None => { ["a", "b"][7] }\n'
        '  }\n'
        '}'
    )
    assert "index 7 is out of range for a 2-element `List`" in err.message


def test_a_refusal_inside_a_test_body_is_found():
    err = _refuse('test "t" { assert ["a"][2] == "a" }')
    assert "index 2 is out of range for a 1-element `List`" in err.message


def test_a_refusal_inside_a_prop_test_body_is_found():
    err = _refuse('prop test "p" (n: Int) { assert ["a"][9] == "a" }')
    assert "index 9 is out of range for a 1-element `List`" in err.message


def test_a_refusal_inside_an_interpolation_is_found():
    err = _refuse('pub fn f() -> Str { let xs = ["a"]\n  return `v=${xs[5]}` }')
    assert "index 5 is out of range for a 1-element `List`" in err.message


def test_a_refusal_inside_a_component_effect_setup_is_found():
    """`let … = effect { … }` is only legal in a component activation body, so
    this is the one position that reaches `LetEffect.setup`."""
    err = _refuse(
        'component C {\n'
        '  let seen = effect { let n = ["a"][6].length()  Map.new() }'
        ' undo seen.drop()\n'
        '}'
    )
    assert "index 6 is out of range for a 1-element `List`" in err.message


# ------------------------------- refused: the component-only action statements
# A component activation body is an action list, not a statement list, so these
# positions are reachable nowhere else in the grammar.

def test_a_refusal_inside_a_timer_body_is_found():
    err = _refuse('component C {\n  every 5s { emit ["a"][7] }\n}')
    assert "index 7 is out of range for a 1-element `List`" in err.message


def test_a_refusal_inside_a_one_shot_timer_body_is_found():
    err = _refuse('component C {\n  after 5s { emit ["a"][7] }\n}')
    assert "index 7 is out of range for a 1-element `List`" in err.message


def test_a_refusal_inside_a_stream_iteration_body_is_found():
    err = _refuse(
        'service Sink { emission fn write(v: Str) }\n'
        'component C requires sink: Sink {\n'
        '  let src = effect Stream.source() undo src.close()\n'
        '  let sub = subscribe src undo sub.close()\n'
        '  every o in sub { emit sink.write(["a"][7]) }\n'
        '}'
    )
    assert "index 7 is out of range for a 1-element `List`" in err.message


def test_a_refusal_inside_a_provide_method_body_is_found():
    err = _refuse(
        'service KV { fn get(k: Str) -> Str }\n'
        'component C provides kv: KV {\n'
        '  provide kv { fn get(k: Str) = ["a"][7] }\n'
        '}'
    )
    assert "index 7 is out of range for a 1-element `List`" in err.message


def test_the_stream_iteration_binding_is_not_a_list_length():
    """`o` is bound by `every o in sub`, so it must be forgotten — otherwise a
    later index into it would be compared against a stale length."""
    _accept(
        'service Sink { emission fn write(v: Str) }\n'
        'component C requires sink: Sink {\n'
        '  let src = effect Stream.source() undo src.close()\n'
        '  let sub = subscribe src undo sub.close()\n'
        '  every o in sub { emit sink.write(o) }\n'
        '}'
    )


def test_a_provide_method_parameter_is_not_a_list_length():
    _accept(
        'service KV { fn get(k: Str) -> Str }\n'
        'component C provides kv: KV {\n'
        '  provide kv { fn get(k: Str) = k }\n'
        '}'
    )


# ------------------------------------------------- accepted: the unbounded case

def test_a_parameter_index_is_still_accepted():
    """The whole point of NOT writing a bounds analysis: `xs[i]` is a runtime
    property, and issue #938 explicitly leaves it alone (row 3)."""
    _accept('pub fn pick(xs: List[Str], i: Int) -> Str { return xs[i] }')


def test_a_call_result_index_is_still_accepted():
    _accept(
        'pub fn idx() -> Int { return 5 }\n'
        'pub fn pick(xs: List[Str]) -> Str { return xs[idx()] }'
    )


def test_an_in_range_literal_index_is_accepted():
    _accept('pub fn f() -> Str { let xs = ["a", "b", "c"]\n  return xs[2] }')


def test_the_last_in_range_index_is_accepted():
    _accept('pub fn f() -> Str { return ["a", "b"][1] }')


# ------------------------------------------------- accepted: no stale refusal

def test_a_reassigned_binding_is_not_bounded_by_its_first_length():
    """`xs` starts as a 1-element literal, so `xs[2]` would be out of range —
    but it is reassigned to a 3-element literal first, so it is in range. A
    pass that remembered the first length would refuse a program that runs."""
    _accept(
        'pub fn f() -> Str {\n'
        '  var xs = ["a"]\n'
        '  xs = ["a", "b", "c"]\n'
        '  return xs[2]\n'
        '}'
    )


def test_a_grown_binding_is_not_bounded_by_its_first_length():
    """`xs` starts as a 1-element literal and is grown by `push`, so `xs[1]` is
    in range. A pass that remembered the first length would refuse a program
    that runs — this is the false-refusal guard for the whole design."""
    _accept(
        'pub fn f() -> Str {\n'
        '  var xs = ["a"]\n'
        '  xs = xs.push("b")\n'
        '  return xs[1]\n'
        '}'
    )


def test_a_mutable_binding_is_never_bounded():
    """The `var` exclusion, learned from `selfhost/parser.rvl` (the self-host
    compiler refused itself here): `var inner = []` at the top of a function,
    grown by `inner = inner.push(…)` inside a `while`, then read as `inner[0]`
    after a runtime `inner.length() != 1` guard. A nested block gets its own
    scope copy, so the enclosing scope would keep the length 0 and refuse a
    program that runs. An immutable `let` cannot be reassigned, so it is the
    only shape whose literal length is true for the whole scope."""
    _accept(
        'pub fn f(ts: List[Str]) -> Str {\n'
        '  var inner = []\n'
        '  while (ts.length() > 0) { inner = inner.push("x") }\n'
        '  if (inner.length() != 1) { return "no" }\n'
        '  return inner[0]\n'
        '}'
    )


def test_a_block_scoped_binding_does_not_leak_out_of_its_arm():
    """`zs` is 1 element inside the `then` arm; the `else` arm is a different
    scope, so an index into a 3-element list there is not compared against 1."""
    _accept(
        'pub fn f(n: Int) -> Str {\n'
        '  if (n > 0) { let zs = ["q"]  return zs[0] }\n'
        '  else { let zs = ["a", "b", "c"]  return zs[2] }\n'
        '}'
    )


def test_an_index_into_a_non_list_is_untouched():
    """The `Str` arm owns its own refusal; this pass must not pre-empt it."""
    err = _refuse('pub fn f() -> Str { return "ab"[9] }')
    assert "`Str` has no index operator" in err.message


def test_an_index_into_an_optional_receiver_is_untouched():
    """The `Opt` arm owns its own refusal (`null-safety`), raised before this
    pass runs; the bounds message must not pre-empt it."""
    err = _refuse(
        'pub fn f(o: Opt[List[Str]]) -> Str { return o[0] }', "opt.rvl"
    )
    assert "the optional wrapper has no such member" in err.message
    assert err.category == "null-safety"


# ------------------------------------------------------- accepted: real programs

def test_a_bounded_read_through_length_is_accepted():
    _accept(
        'pub fn f(xs: List[Str]) -> Str {\n'
        '  if (xs.length() > 0) { return xs[0] }\n'
        '  return ""\n'
        '}'
    )


def test_an_in_range_read_of_a_nested_literal_is_accepted():
    _accept('pub fn f() -> Str { return [["a", "b"], ["c"]][1][0] }')


def test_the_module_still_compiles_a_whole_program():
    """A sanity check that the pass is not refusing on the shape of a body it
    has already lowered once — the walk runs before lowering, once."""
    _accept(
        'type Point = {x: Int, y: Int}\n'
        'pub fn label(xs: List[Str], n: Int) -> Str {\n'
        '  let names = ["lo", "mid", "hi"]\n'
        '  if (n < 3) { return names[n] }\n'
        '  return names[0]\n'
        '}\n'
        'test "labels" { assert label(["a"], 0) == "lo" }\n'
    )

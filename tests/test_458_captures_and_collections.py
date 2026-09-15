"""Closure capture and the `List` edges, executed on every tier (item 458, #721).

Item 458's defect class is a construct that COMPILES on all six tiers and
BEHAVES DIFFERENTLY on one of them. `tests/test_458_logical_short_circuit.py`
pinned the `&&` face, `tests/test_458_str_ordering.py` the ordering face, and
the `Int.MIN` face landed in `tests/test_cross_tier_execution.py`. This file
pins four more, found the same way: by EXECUTING one source on all six tiers
and comparing against the py reference, never by reading an emitter.

MEASURED on this machine before the fix (py is the reference throughout;
openjdk 26.0.2, cargo 1.9x, wasmtime, node 22, go 1.2x):

  closure reads an enclosing `var`,      py 6 · ts 6 · wasm 6 · rust 6 ·
  `var` reassigned after the arrow       java 6 · **go 105**
  (`docs/closures.md`: by value)

  `[{id:1},{id:2}].indexOf({id:2})`      py 1 · go 1 · rust 1 · java 1 ·
                                         **ts -1** · wasm refused by name

  `[10,20,30,40].slice(2, 99).length()`  py 2 · ts 2 · go 2 · rust 2 ·
                                         java 2 · **wasm 97**
  `[10,20,30,40].slice(3, 1)`            py/ts/go/rust/java empty ·
                                         **wasm traps**
  `"abcd".slice(-2, -1)`                 py/ts/go/rust/java "c" · **wasm ""**

  `let xs: List[Int] = []`               py/ts/go/wasm/java compile and run ·
                                         **rust DOES NOT COMPILE** (E0282)

Four distinct causes:

  - **the go tier ignored the arrow's `captures` list.** docs/closures.md
    decides that a revl closure captures strictly BY VALUE, and the front end
    owns which names those are: `src/revl/lower.py::_mutable_free_vars` writes
    them onto the arrow node so no backend re-derives them. python binds them
    as default arguments, typescript as an IIFE around the arrow, java as a
    `final` copy, rust by `move`. Go's arrow arm read the list not at all, and
    a Go closure captures the enclosing VARIABLE — so the tier answered
    whatever the `var` held when the closure RAN. The idiom was already in the
    same file: `_emit_pins` writes `k := k` for a derived inverse, under a
    comment that says why.

  - **`revlIndexOf` searched a list with `Array.prototype.indexOf`.** That is
    `===`, which is IDENTITY for a record or a nested list, so a structurally
    equal element read back as absent. This is the founding defect of
    tests/test_cross_tier_execution.py (`{a: 1} == {a: 1}` lowered to JS
    `===`) surviving one level down, inside a helper written after it.

  - **the wasm `$list_slice` used both `Int` bounds raw.** No clamp, no
    end-relative negative, no empty-when-reversed — so a high bound past the
    end produced a length past the end and copied bytes from beyond the list,
    and a negative or reversed pair produced a negative length that reached
    `$alloc` as a huge unsigned size. `$str_cp_slice` clamped but skipped the
    end-relative step, so a negative bound went to 0 rather than counting back.
    docs/stdlib-2.0.md §slice settled all three readings for every tier under
    issue #549; this tier was not in the guard that pinned them.

  - **the rust tier dropped the declared type of an empty list literal.**
    `_v3_empty_vec_elem_types` recovers an accumulator's element type from the
    pushes that come LATER, which is the hard case, and never asked the
    annotation the author wrote — so `let xs: List[Int] = []` emitted `let xs =
    vec![];` and rustc refused the whole module with E0282. The `expected` key
    naming `List[Int]` was on the literal in the IR the whole time.

NON-VACUITY CONTROLS, which pass BEFORE and AFTER:
`test_an_arrow_with_no_captures_is_unpinned_on_go` proves the go pin is
conditional on the front end's list rather than wrapping every arrow;
`test_str_index_of_keeps_the_native_scan_on_ts` proves the indexOf fix did not
put a structural walk on the Str receiver's hot path; and
`test_wasm_still_refuses_list_index_of_by_name` proves the wasm work did not
turn an honest refusal into a silent lowering — a tier that cannot match the
reference must REFUSE BY NAME.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

#: wasm executes every probe in this file except the two it refuses BY NAME
#: (see the refusal tests at the foot), so it belongs in the fast set here
#: rather than being left unmeasured the way the bitwise guard left it.
FAST_TIERS = ("py", "ts", "go", "wasm")
SLOW_TIERS = ("rust", "java")

CAPTURES = """
fn snap_assign() -> Int { var n = 1  let f = (x: Int) => x + n  n = 100  return f(5) }
fn snap_compound() -> Int { var n = 1  let f = (x: Int) => x + n  n += 10  return f(5) }
fn two_captures() -> Int { var a = 1  var b = 2  let f = (x: Int) => x + a * 10 + b  a = 9  b = 9  return f(0) }
fn snap_str() -> Str { var s = "a"  let f = (x: Str) => s + x  s = "z"  return f("!") }
fn snap_list() -> Int { var xs = [1, 2]  let f = () => xs.length()  xs = [1, 2, 3, 4]  return f() }
fn while_var() -> Int {
  var i = 0
  var total = 0
  while (i < 3) { let f = () => i  i = i + 1  total = total + f() }
  return total
}
fn for_body_var() -> Int {
  var total = 0
  for (i of [1, 2, 3]) { var j = i  let f = () => j  j = 0  total = total + f() }
  return total
}
fn immutable_capture() -> Int { let n = 7  let f = (x: Int) => x + n  return f(1) }

test "a later assignment does not reach the closure"  { assert snap_assign() == 6 }
test "nor does a later compound assignment"           { assert snap_compound() == 6 }
test "every capture is snapshotted, not just one"     { assert two_captures() == 12 }
test "a Str capture snapshots"                        { assert snap_str() == "a!" }
test "a List capture snapshots"                       { assert snap_list() == 2 }
test "a while-loop var snapshots per iteration"       { assert while_var() == 3 }
test "a for-loop body var snapshots per iteration"    { assert for_body_var() == 6 }
test "an immutable capture is unaffected"             { assert immutable_capture() == 8 }
"""

#: `indexOf` over elements whose equality is STRUCTURAL (syntax-2.0 §3.4), not
#: identity. The scalar rows are the control inside the same document: they were
#: right on every tier before, and must stay right.
LIST_INDEX_OF = """
type Row = { id: Int }
pub fn ints() -> List[Int] { return [10, 20, 30] }
pub fn dups() -> List[Int] { return [7, 8, 7] }
pub fn empty() -> List[Int] { return [] }
pub fn strs() -> List[Str] { return ["a", "b"] }
pub fn rows() -> List[Row] { return [{ id: 1 }, { id: 2 }] }
pub fn nested() -> List[List[Int]] { return [[1], [2, 3]] }

test "an absent element is -1"           { assert ints().indexOf(99) == 0 - 1 }
test "a duplicate answers the first"     { assert dups().indexOf(7) == 0 }
test "an empty list is -1"               { assert empty().indexOf(1) == 0 - 1 }
test "a present element is its index"    { assert ints().indexOf(20) == 1 }
test "Str elements compare by value"     { assert strs().indexOf("b") == 1 }
test "a record compares structurally"    { assert rows().indexOf({ id: 2 }) == 1 }
test "a structural miss is still -1"     { assert rows().indexOf({ id: 9 }) == 0 - 1 }
test "a nested list compares that way too" { assert nested().indexOf([2, 3]) == 1 }
"""

#: docs/stdlib-2.0.md §slice: bounds are end-relative, clamp into
#: [0, length], and the slice is empty when the high bound lands below the low
#: one — "the python/JS reading, on every tier (issue #549)".
SLICE_BOUNDS = """
pub fn xs() -> List[Int] { return [10, 20, 30, 40] }
pub fn s() -> Str { return "abcd" }

test "a high bound below the low one is empty"   { assert xs().slice(3, 1).length() == 0 }
test "a high bound past the end clamps"          { assert xs().slice(2, 99).length() == 2 }
test "both bounds past the end is empty"         { assert xs().slice(10, 20).length() == 0 }
test "a negative low bound counts from the end"  { assert xs().slice(0 - 2, 4).length() == 2 }
test "so does a negative high bound"             { assert xs().slice(0 - 3, 0 - 1).length() == 2 }
test "below -length clamps to the start"         { assert xs().slice(0 - 99, 4).length() == 4 }
test "the sliced elements are the right ones"    { assert xs().slice(0 - 2, 4)[0] == 30 }
test "Str agrees: high below low"                { assert s().slice(3, 1) == "" }
test "Str agrees: past the end"                  { assert s().slice(2, 99) == "cd" }
test "Str agrees: both negative"                 { assert s().slice(0 - 2, 0 - 1) == "c" }
test "Str agrees: below -length"                 { assert s().slice(0 - 99, 4) == "abcd" }
test "Str agrees: both past the end"             { assert s().slice(10, 20) == "" }
"""

#: an empty list literal whose type the AUTHOR wrote. The accumulator forms
#: below it are the ones that already worked, kept in the same document so a
#: regression in either direction is one failing row.
EMPTY_LIST_TYPES = """
fn annotated() -> Int { let xs: List[Int] = []  return xs.length() }
fn annotated_str() -> Int { let ys: List[Str] = []  return ys.length() }
fn annotated_walked() -> Int {
  var n = 0
  let xs: List[Int] = []
  for (x of xs) { n = n + 1 }
  return n
}
fn accumulator() -> Int { var xs: List[Int] = []  xs = xs.push(1)  return xs.length() }
pub fn returned() -> List[Int] { return [] }

test "an annotated empty list is empty"      { assert annotated() == 0 }
test "of any element type"                   { assert annotated_str() == 0 }
test "and walking it visits nothing"         { assert annotated_walked() == 0 }
test "an accumulator still works"            { assert accumulator() == 1 }
test "a returned empty list still works"     { assert returned().length() == 0 }
"""

PROBES = {
    "closures capture by value": CAPTURES,
    "List.indexOf is structural": LIST_INDEX_OF,
    "slice bounds are normalised": SLICE_BOUNDS,
    "an empty list literal keeps its declared type": EMPTY_LIST_TYPES,
}

#: what each tier can REPRESENT. wasm has no structural equality beyond the
#: scalars, so `indexOf` over a record is refused BY NAME there rather than
#: answered wrongly — which is the outcome this file wants when a tier cannot
#: match the reference, and is asserted as such below.
REFUSED = {("wasm", "List.indexOf is structural")}


def _run(tier: str, source: str) -> tuple[str, str]:
    return RUNNERS[tier](compile_source(source, "captures_458.rvl"))


def _emit(backend: str, source: str) -> str:
    emitted = backend_emitter(backend).emit(compile_source(source))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


# ---------------------------------------------------------------- executed
#
# The only way this class of defect is found: every probe here emitted cleanly
# on the tier that got it wrong.

@pytest.mark.parametrize("name", sorted(PROBES))
@pytest.mark.parametrize("tier", FAST_TIERS)
def test_probe_agrees_on_the_fast_tiers(tier: str, name: str):
    if (tier, name) in REFUSED:
        pytest.skip(f"{tier} refuses {name!r} by name (asserted separately)")
    status, message = _run(tier, PROBES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} disagrees about {name!r}: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("name", sorted(PROBES))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_probe_agrees_on_the_slow_tiers(tier: str, name: str):
    status, message = _run(tier, PROBES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} disagrees about {name!r}: {message}"


# ------------------------------------------------- the go by-value capture
#
# Static, so it runs with no toolchain at all — and it is the half that says
# WHY, which a value assertion cannot.

ONE_CAPTURE = """
fn probe() -> Int { var n = 1  let f = (x: Int) => x + n  n = 100  return f(5) }
"""
NO_CAPTURE = """
fn probe() -> Int { let f = (x: Int) => x + 1  return f(5) }
"""


def test_a_go_arrow_pins_each_capture_by_value():
    emitted = _emit("go", ONE_CAPTURE)
    assert "n := n" in emitted, (
        "a Go closure captures the enclosing VARIABLE, so an arrow that reads a "
        "`var` the body reassigns later must take its own copy where the arrow "
        f"literal is built (docs/closures.md):\n{emitted}")
    assert "func() func(" in emitted, (
        "the copy has to be taken at arrow-creation time, which on this tier "
        f"means an IIFE around the arrow rather than a pin in the body:\n{emitted}")


def test_an_arrow_with_no_captures_is_unpinned_on_go():
    """NON-VACUITY. The front end names the captures; the backend pins those
    and nothing else. An arrow that reads only its own parameters must emit
    exactly as it did before, with no wrapper and no copy."""
    emitted = _emit("go", NO_CAPTURE)
    assert "func() func(" not in emitted, emitted
    assert "f := func(x int64) int64 { return revlAdd(x, 1) }" in emitted, emitted


@pytest.mark.parametrize("backend", ["python", "typescript", "java"])
def test_the_other_tiers_already_bound_the_capture(backend: str):
    """NON-VACUITY, and the evidence that go was the outlier rather than the
    rule: three tiers were already spelling the same contract in their own
    idiom before this change, and none of them moved."""
    needles = {"python": "n=n", "typescript": "((n: any) =>",
               "java": "__revl_capture_f_n"}
    emitted = _emit(backend, ONE_CAPTURE)
    assert needles[backend] in emitted, emitted


# ------------------------------------------------ the ts structural indexOf

RECORD_INDEX_OF = """
type Row = { id: Int }
pub fn at(xs: List[Row], r: Row) -> Int { return xs.indexOf(r) }
"""
STR_INDEX_OF = """
pub fn at(s: Str, needle: Str) -> Int { return s.indexOf(needle) }
"""


def test_list_index_of_searches_by_structural_equality_on_ts():
    emitted = _emit("typescript", RECORD_INDEX_OF)
    assert "revlEq(x[i], v)" in emitted, (
        "revl has ONE equality and it is structural (syntax-2.0 §3.4); "
        "`Array.prototype.indexOf` is `===`, which is identity for a record, "
        f"so a structurally equal element read back as absent:\n{emitted}")
    assert "function revlEq" in emitted, (
        "the helper `revlIndexOf` now calls has to be in scope even when the "
        f"document contains no `==` of its own:\n{emitted}")


def test_str_index_of_keeps_the_native_scan_on_ts():
    """NON-VACUITY. A `Str` receiver has no structural elements to walk — its
    needle is a substring, and the native scan is both correct and the fast
    path. The fix must not have put an element loop on it."""
    emitted = _emit("typescript", STR_INDEX_OF)
    assert 'const at = x.indexOf(v as string)' in emitted, emitted


@pytest.mark.parametrize("backend", ["python", "go", "rust", "java"])
def test_the_already_structural_tiers_are_untouched(backend: str):
    """NON-VACUITY. These four answered the record row correctly before the
    change, so nothing about it should reach them."""
    assert "revlEq(x[i], v)" not in _emit(backend, RECORD_INDEX_OF)


# --------------------------------------------------- the wasm slice bounds

def test_wasm_list_slice_normalises_its_bounds():
    emitted = _emit("wasm", SLICE_BOUNDS)
    assert "$list_slice" in emitted, emitted
    body = emitted.split("$list_slice", 1)[1].split("(func ", 1)[0]
    assert "i32.load (local.get $s)" in body, (
        "the normalisation needs the list's own length; without it the helper "
        f"cannot clamp or count from the end:\n{body}")
    assert body.count("i32.lt_s") >= 4, (
        "a negative bound counts from the end, both bounds clamp into "
        "[0, length], and a reversed pair is empty — three readings "
        f"docs/stdlib-2.0.md §slice settles for every tier:\n{body}")


def test_wasm_str_slice_counts_a_negative_bound_from_the_end():
    emitted = _emit("wasm", SLICE_BOUNDS)
    body = emitted.split("$str_cp_slice", 1)[1].split("(func ", 1)[0]
    assert "(local.set $a (i32.add (local.get $a) (local.get $cplen)))" in body, (
        "a negative bound is END-RELATIVE, not clamped to zero: clamping made "
        f'`"abcd".slice(-2, -1)` the empty string here and "c" everywhere else:'
        f"\n{body}")


# ------------------------------------------- the rust declared empty vector

ANNOTATED_EMPTY = """
fn probe() -> Int { let xs: List[Int] = []  return xs.length() }
"""
UNANNOTATED_ACCUMULATOR = """
fn probe() -> Int { var xs = []  xs = xs.push(1)  return xs.length() }
"""


def test_rust_annotates_a_declared_empty_vector():
    emitted = _emit("rust", ANNOTATED_EMPTY)
    assert "let xs: Vec<i64> = vec![];" in emitted, (
        "an empty `vec![]` gives rustc no element type and it refuses the "
        "module with E0282; the declared `List[Int]` is on the literal in the "
        f"IR and is the most direct answer there is:\n{emitted}")


def test_rust_still_recovers_an_unannotated_accumulator():
    """NON-VACUITY. Reading the annotation is an ADDITIONAL source, not a
    replacement: the harder case — an empty literal with no annotation, typed
    only by a later push — still has to be recovered the way it was."""
    emitted = _emit("rust", UNANNOTATED_ACCUMULATOR)
    assert "let mut xs: Vec<i64> = vec![];" in emitted, emitted


# ------------------------------------------------------- honest refusals
#
# A tier that cannot match the reference must REFUSE BY NAME. A silent wrong
# answer is the worst outcome of the three; a refusal is the honest one, and it
# has to survive a change that made the tier do more.

def test_wasm_still_refuses_list_index_of_by_name():
    # the wasm backend raises its own `EmitError`, which is not a `RevlError`;
    # what matters is that the refusal NAMES the construct and the reason.
    with pytest.raises(Exception) as failure:
        _emit("wasm", RECORD_INDEX_OF)
    message = str(failure.value)
    assert "indexOf" in message, message
    assert "hosted backend" in message, message


def test_wasm_still_refuses_a_record_equality_by_name():
    with pytest.raises(Exception) as failure:
        _emit("wasm", 'type R = { id: Int }\n'
                      'pub fn eq(a: R, b: R) -> Bool { return a == b }\n')
    assert "equality on this tier is lowerable" in str(failure.value)

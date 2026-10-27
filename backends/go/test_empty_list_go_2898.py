"""An untyped empty list builds on the go tier, or is refused (issue #2898).

WHAT WAS WRONG. `var out = []` carries no element type in the IR, and the go
emitter guessed one: `[]any{}` on the pure tier, the tier's `List[Int]`
slice in a provide method (`[]int64{}` on ir_version 3, `[]int{}` before). Go fixes a composite literal's element type at the literal, never
from a later `append`, so a list of strings returned as `List[Str]` failed
`go build` (`cannot use out (variable of type []any) as []string`). The same
gap broke `list_sort` and `list_dedup` from stdlib/list.rvl on go.

WHAT IS EMITTED NOW. The binding takes the type of where it flows: a
declared return (`return out`), a declared parameter (`f(out)`), or another
binding it is assigned to (`out = res`, the `list_sort` shape), a record
field, an element of another list, or else the type of what is pushed into
it. Two disagreeing targets are refused by name. With no evidence at all,
a binding that is only sized (or appended to) and never assigned anything
else keeps the slice main gave it, which builds whatever the element type; any other read is refused by name, as the wasm tier does, instead of
printing a module that does not build.

Issue #2915: the evidence is keyed by binding, not by name, so two `out`
in disjoint sibling blocks each take their own type. Issue #2918: untyped
empty lists assigned to one another (`out = a`, `out = out.concat(a)`)
share their evidence and one type, `[]any{}` when nothing typed reads
them, and `out = out.concat(xs)` types `out` as `xs` is.

The run tests execute the program's `test` blocks on py and on go and assert
both pass. They need `go` and skip without it; a skip is not a pass.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (ROOT / "src", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from revl import compile_files, compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_go_emit_2898", HERE / "emit.py")
go_emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(go_emit)

STDLIB = ROOT / "stdlib" / "list.rvl"

# `dup` is the program from the issue. `sorted_set` reaches the stdlib's
# `list_sort` (an empty `res` that only flows on through `out = res`) and
# `list_dedup` (an empty `out` passed to `list_contains` and returned).
PROGRAM = """\
use "stdlib/list.rvl" { list_sort, list_dedup }

pub fn dup(xs: List[Str]) -> List[Str] {
  var out = []
  for (x of xs) { out = out.push(x) }
  return out
}

pub fn sorted_set(xs: List[Str]) -> List[Str] { return list_sort(list_dedup(xs)) }

type Bag = { items: List[Str], n: Int }

// `items` flows only into a record field
pub fn bag(xs: List[Str]) -> Bag {
  var items = []
  for (x of xs) { items = items.push(x) }
  return { items: items, n: items.length() }
}

// `row` flows only as an element of `out`
pub fn windows(n: Int) -> List[List[Int]] {
  var out = []
  var row = []
  var i = 0
  while (i < n) {
    row = row.push(i)
    out = out.push(row)
    i = i + 1
  }
  return out
}

// `seen` flows nowhere typed; what is pushed into it decides
pub fn count(xs: List[Str]) -> Int {
  var seen = []
  for (x of xs) { seen = seen.push(x) }
  return seen.length()
}

test "the pushed strings come back" {
  assert dup(["b", "a", "b"]).join("|") == "b|a|b"
  assert dup([]).length() == 0
}
test "stdlib sort and dedup" {
  assert list_sort(["c", "a", "b"]).join("|") == "a|b|c"
  assert list_dedup(["b", "a", "b"]).join("|") == "b|a"
  assert sorted_set(["c", "a", "c", "b"]).join("|") == "a|b|c"
}
test "record, element and push targets" {
  assert bag(["x", "y"]).items.join("|") == "x|y"
  assert windows(3).length() == 3
  assert windows(3)[2].length() == 3
  assert count(["p", "q"]) == 2
}
"""

# nothing receives or fills these, and they are never read but sized: any
# element type builds, so they keep main's `[]any{}` (py and java run them)
UNHINTED = """\
pub fn sized() -> Int {
  var out = []
  return out.length()
}
pub fn unused() -> Int {
  let values = []
  return 1
}
test "unhinted" {
  assert sized() == 0
  assert unused() == 1
}
"""

# assigned another list, then only sized: the assignment fixes the type
ASSIGNED = """\
pub fn a() -> Int {
  var out = []
  let xs = ["a"]
  out = xs
  return out.length()
}
test "assigned" {
  assert a() == 1
}
"""

ASSIGNED_METHOD = """\
service Cnt { fn count(n: Int) -> Int }
component C provides cnt: Cnt {
  provide cnt {
    fn count(n) {
      var out = []
      let xs = ["a"]
      out = xs
      return out.length()
    }
  }
}
"""

# read as an element of a list literal: there is no typed target to take
UNPINNED = """\
pub fn w() -> List[List[Int]] {
  var row = []
  return [row]
}
"""

# returned as List[Int] and handed to a List[Str] parameter: no one Go type
CONFLICT = """\
fn takes(xs: List[Str]) -> Bool { return xs.length() == 0 }
pub fn c() -> List[Int] {
  var out = []
  let e = takes(out)
  return out
}
"""

# a provide method that only pushes Ints and returns the count
COUNTER = """\
service Cnt { fn count(n: Int) -> Int }
component C provides cnt: Cnt {
  provide cnt {
    fn count(n) {
      var acc = []
      var i = 0
      while (i < n) {
        acc = acc.push(i)
        i = i + 1
      }
      return acc.length()
    }
  }
}
"""

COMPONENT = """\
service Dup { fn dup(xs: List[Str]) -> List[Str] }
component D provides dup: Dup {
  provide dup {
    fn dup(xs) {
      var out = []
      for (x of xs) { out = out.push(x) }
      return out
    }
  }
}
"""


@pytest.fixture(scope="module")
def program_ir(tmp_path_factory):
    # `use` resolves beside the importing file, so the stdlib sits next to it
    d = tmp_path_factory.mktemp("empty_list_2898")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "list.rvl").write_text(
        STDLIB.read_text(encoding="utf-8"), encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(PROGRAM, encoding="utf-8")
    return compile_files([str(main)])


def test_an_empty_list_takes_the_type_it_flows_into(program_ir):
    src = go_emit.emit(program_ir)
    assert "out := []any{}" not in src, src
    assert "out := []string{}" in src, src  # dup, list_dedup, list_sort
    assert "res := []string{}" in src, src  # list_sort's `out = res`
    assert "items := []string{}" in src, src  # a record field
    assert "row := []int64{}" in src, src  # an element of `out`
    assert "seen := []string{}" in src, src  # what is pushed


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_the_empty_list_program_like_py(program_ir):
    py_status, py_message = RUNNERS["py"](program_ir)
    assert py_status == "pass", py_message
    status, message = RUNNERS["go"](program_ir)
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert "3 test(s) passed" in message and "3 test(s) passed" in py_message, (
        py_message, message)


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_an_unhinted_list_only_sized_builds_and_runs_like_py():
    ir = compile_source(UNHINTED, "unhinted_2898.rvl")
    src = go_emit.emit(ir)
    assert "out := []any{}" in src and "values := []any{}" in src, src
    py_status, py_message = RUNNERS["py"](ir)
    assert py_status == "pass", py_message
    status, message = RUNNERS["go"](compile_source(UNHINTED, "unhinted_2898.rvl"))
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message


def test_an_empty_list_with_no_typed_target_is_refused_by_name():
    with pytest.raises(go_emit.EmitError,
                       match="needs an expected List type: no typed target found "
                             "for `row = \\[\\]`"):
        go_emit.emit(compile_source(UNPINNED, "unpinned_2898.rvl"))


def test_an_empty_list_with_conflicting_targets_is_refused_by_name():
    with pytest.raises(go_emit.EmitError,
                       match="conflicting expected List types: `out = \\[\\]` "
                             "flows into List\\[Int\\] and List\\[Str\\]"):
        go_emit.emit(compile_source(CONFLICT, "conflict_2898.rvl"))


def test_an_annotated_empty_list_still_needs_no_target():
    src = go_emit.emit(compile_source(
        UNPINNED.replace("var row = []", "var row: List[Int] = []"),
        "annotated_2898.rvl"))
    assert "[]int64{}" in src, src


def test_a_provide_method_empty_list_takes_the_declared_return():
    src = go_emit.emit(compile_source(COMPONENT, "component_2898.rvl"), "probe")
    assert "out := []string{}" in src, src
    assert "out := []int64{}" not in src, src


def test_go_builds_the_provide_method_module():
    import validate  # noqa: PLC0415

    validator = validate.VALIDATORS["go"]
    reason = validator.unavailable()
    if reason:
        pytest.skip("go toolchain unavailable: %s" % reason)
    src = go_emit.emit(compile_source(COMPONENT, "component_2898.rvl"), "probe")
    status, detail = validator.check([("dup", src)])["dup"]
    assert status == validate.OK, detail


def test_a_provide_method_types_an_empty_list_from_its_pushes():
    src = go_emit.emit(compile_source(COUNTER, "counter_2898.rvl"), "probe")
    assert "acc := []int64{}" in src, src


def test_go_builds_the_provide_method_counter():
    import validate  # noqa: PLC0415

    validator = validate.VALIDATORS["go"]
    reason = validator.unavailable()
    if reason:
        pytest.skip("go toolchain unavailable: %s" % reason)
    src = go_emit.emit(compile_source(COUNTER, "counter_2898.rvl"), "probe")
    status, detail = validator.check([("cnt", src)])["cnt"]
    assert status == validate.OK, detail


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_an_assigned_list_takes_the_source_type_and_runs_like_py():
    ir = compile_source(ASSIGNED, "assigned_2898.rvl")
    assert "out := []string{}" in go_emit.emit(ir)
    py_status, py_message = RUNNERS["py"](ir)
    assert py_status == "pass", py_message
    status, message = RUNNERS["go"](compile_source(ASSIGNED, "assigned_2898.rvl"))
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message


def test_go_builds_a_provide_method_list_assigned_another_list():
    import validate  # noqa: PLC0415

    validator = validate.VALIDATORS["go"]
    reason = validator.unavailable()
    if reason:
        pytest.skip("go toolchain unavailable: %s" % reason)
    src = go_emit.emit(compile_source(ASSIGNED_METHOD, "assigned_m_2898.rvl"), "probe")
    assert "out := []string{}" in src, src
    status, detail = validator.check([("assigned", src)])["assigned"]
    assert status == validate.OK, detail


# Issue #2915: two bindings of one name in disjoint sibling blocks are two
# bindings. The hints were keyed by name across the function, so both `out`
# took one element type (and the second loop's `x` the first loop's type),
# and the module failed `go build`.
SHADOWED = """\
pub fn pick(xs: List[Str], b: Bool) -> List[Int] {
  if (b) {
    var out = []
    for (x of xs) { out = out.push(x) }
    return [out.join("|").length()]
  } else {
    var out = []
    return out
  }
}

pub fn both(xs: List[Str], ns: List[Int]) -> Int {
  var a = []
  for (x of xs) { a = a.push(x) }
  var b = []
  for (x of ns) { b = b.push(x) }
  return a.length() + b.length()
}

test "each binding keeps its own element type" {
  assert pick(["ab", "c"], true)[0] == 4
  assert pick(["ab"], false).length() == 0
  assert both(["a"], [1, 2]) == 3
}
"""

# Issue #2918: untyped empty lists assigned to one another share one type,
# `[]any{}` when nothing typed reads them; a concat on the list itself types
# it from the argument.
ALIASED = """\
pub fn alias() -> Int {
  var a = []
  var out = []
  out = a
  return out.length()
}

pub fn alias_pushed(xs: List[Str]) -> Int {
  var a = []
  for (x of xs) { a = a.push(x) }
  var out = []
  out = a
  return out.length()
}

pub fn grow() -> Int {
  var out = []
  out = out.concat(["a"])
  return out.length()
}

test "aliased and concatenated untyped lists" {
  assert alias() == 0
  assert alias_pushed(["p", "q"]) == 2
  assert grow() == 1
}
"""

ALIASED_METHOD = """\
service Cnt { fn count(n: Int) -> Int }
component C provides cnt: Cnt {
  provide cnt {
    fn count(n) {
      var a = []
      var out = []
      out = a
      return out.length()
    }
  }
}
"""

CONCAT_METHOD = """\
service Cnt { fn count(n: Int) -> Int }
component C provides cnt: Cnt {
  provide cnt {
    fn count(n) {
      var out = []
      out = out.concat(["a"])
      return out.length()
    }
  }
}
"""


def _runs_like_py(source, filename):
    py_status, py_message = RUNNERS["py"](compile_source(source, filename))
    assert py_status == "pass", py_message
    status, message = RUNNERS["go"](compile_source(source, filename))
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    return py_message, message


def _go_builds(source, filename):
    import validate  # noqa: PLC0415

    validator = validate.VALIDATORS["go"]
    reason = validator.unavailable()
    if reason:
        pytest.skip("go toolchain unavailable: %s" % reason)
    src = go_emit.emit(compile_source(source, filename), "probe")
    status, detail = validator.check([("probe", src)])["probe"]
    assert status == validate.OK, detail
    return src


def test_shadowed_empty_lists_each_take_their_own_type():
    src = go_emit.emit(compile_source(SHADOWED, "shadowed_2915.rvl"))
    assert "out := []string{}" in src and "out := []int64{}" in src, src
    assert "a := []string{}" in src and "b := []int64{}" in src, src


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_shadowed_empty_lists_like_py():
    py_message, message = _runs_like_py(SHADOWED, "shadowed_2915.rvl")
    assert "1 test(s) passed" in message, (py_message, message)


def test_aliased_and_concatenated_empty_lists_are_typed():
    src = go_emit.emit(compile_source(ALIASED, "aliased_2918.rvl"))
    assert "a := []any{}" in src and "out := []any{}" in src, src
    assert "a := []string{}" in src and "out := []string{}" in src, src


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_aliased_and_concatenated_empty_lists_like_py():
    py_message, message = _runs_like_py(ALIASED, "aliased_2918.rvl")
    assert "1 test(s) passed" in message, (py_message, message)


def test_go_builds_a_provide_method_list_aliased_to_another_empty_list():
    src = _go_builds(ALIASED_METHOD, "aliased_m_2918.rvl")
    assert "a := []int64{}" in src and "out := []int64{}" in src, src


def test_go_builds_a_provide_method_list_concatenated_onto_itself():
    src = _go_builds(CONCAT_METHOD, "concat_m_2918.rvl")
    assert "out := []string{}" in src, src


# An annotated empty list is typed, not a member of the untyped class: what
# is assigned or concatenated from it gives its type to the untyped one.
ANNOTATED_SOURCE = """\
pub fn ann() -> Int {
  var a: List[Str] = []
  var out = []
  out = a
  return out.length()
}

pub fn ann2(n: Int) -> Int {
  let a: List[Int] = []
  var out = []
  out = out.concat(a)
  return out.length() + n
}

test "an annotated source types the untyped list" {
  assert ann() == 0
  assert ann2(1) == 1
}
"""

ANNOTATED_SOURCE_METHOD = ALIASED_METHOD.replace("var a = []", "var a: List[Str] = []")

# a match arm binds over its own body: its `x` is not the sibling block's
SHADOWED_BY_ARM = """\
fn g(xs: List[Int]) -> Int { return xs.length() }

pub fn f(o: Opt[List[Int]], c: Bool) -> List[Str] {
  if (c) {
    var x = []
    x = x.push("a")
    return [x.join("|")]
  }
  let n = match o {
    Some(x) => g(x),
    None => 0,
  }
  return ["b"]
}

test "the arm binding is its own" { assert f(None, true)[0] == "a" }
"""


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_an_untyped_list_fed_from_an_annotated_one_like_py():
    src = go_emit.emit(compile_source(ANNOTATED_SOURCE, "annotated_src_2918.rvl"))
    assert "out := []string{}" in src and "out := []int64{}" in src, src
    _runs_like_py(ANNOTATED_SOURCE, "annotated_src_2918.rvl")


def test_go_builds_a_provide_method_list_fed_from_an_annotated_one():
    src = _go_builds(ANNOTATED_SOURCE_METHOD, "annotated_src_m_2918.rvl")
    assert "out := []string{}" in src, src


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_a_name_rebound_by_a_match_arm_like_py():
    src = go_emit.emit(compile_source(SHADOWED_BY_ARM, "arm_2915.rvl"))
    assert "x := []string{}" in src, src
    _runs_like_py(SHADOWED_BY_ARM, "arm_2915.rvl")

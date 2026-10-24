"""An untyped empty list builds on the go tier, or is refused (issue #2898).

WHAT WAS WRONG. `var out = []` carries no element type in the IR, and the go
emitter guessed one: `[]any{}` on the pure tier, `[]int64{}` in a provide
method. Go fixes a composite literal's element type at the literal, never
from a later `append`, so a list of strings returned as `List[Str]` failed
`go build` (`cannot use out (variable of type []any) as []string`). The same
gap broke `list_sort` and `list_dedup` from stdlib/list.rvl on go.

WHAT IS EMITTED NOW. The binding takes the type of where it flows: a
declared return (`return out`), a declared parameter (`f(out)`), or another
binding it is assigned to (`out = res`, the `list_sort` shape), a record
field, an element of another list, or else the type of what is pushed into
it. Two disagreeing targets are refused by name. With no evidence at all,
a binding that is only sized (or appended to) keeps main's `[]any{}`, which
builds whatever the element type; any other read is refused by name, as the wasm tier does, instead of
printing a module that does not build.

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

"""Issue #2901: a TypeScript `==` against a Str, Int or Bool literal is `===`.

`revlEq` opens with `a === b` and answers false for any pair where either side
is not an object, so a literal operand makes the call and the bare operator
agree on every value. The emitter writes the operator where the other operand
is known to have the literal's type, and keeps `revlEq` everywhere else.

The literal is written `(5n as bigint)`: tsc narrows a `boolean` inside
`if (!b)` and types `const s = "b"` as the literal `"b"`, and a bare
`b === true` or `s === "a"` there is a TS2367 error.

An Int literal needs the IR to state the other operand's Int type (a literal,
annotated arithmetic or negation, a Map index with `value_type` Int): a bare
name can be an `Opt[Float]`, and a Float or Int32 Map value carries no widen
marker, so `number === bigint` would not typecheck. Those keep `revlEq`, as do
structural operands, Float and None literals and widened operands (#2715).
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402


def _emit_ts(source: str) -> str:
    spec = importlib.util.spec_from_file_location(
        "emit_ts_2901", ROOT / "backends" / "typescript" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(source, "lit_eq_2901.rvl"))


def _body(emitted: str, fn: str) -> str:
    """The emitted text of one exported function, signature to closing brace."""
    start = emitted.index(f"function {fn}(")
    return emitted[start:emitted.index("\n}", start)]


LITERALS = """
pub fn s_eq(s: Str) -> Bool { return s == "a" }
pub fn s_ne(s: Str) -> Bool { return s != "a" }
pub fn s_flip(s: Str) -> Bool { return "a" == s }
pub fn s_opt(s: Opt[Str]) -> Bool { return s == "a" }
pub fn b_eq(b: Bool) -> Bool { return b == true }
pub fn b_ne(b: Bool) -> Bool { return b != false }
pub fn i_arith(n: Int) -> Bool { return n * 2 == 4 }
pub fn i_neg(n: Int) -> Bool { return -n == 3 }
pub fn i_index(m: Map[Str, Int]) -> Bool { return m["a"] != 3 }
pub fn s_index(m: Map[Str, Str]) -> Bool { return m["a"] == "x" }
pub fn both(a: Int) -> Bool { return 1 == 2 }
"""

STRUCTURAL = """
type Row = { id: Int }
pub fn rec(a: Row, b: Row) -> Bool { return a == b }
pub fn lst(a: List[Int], b: List[Int]) -> Bool { return a == b }
pub fn opt(a: Opt[Int], b: Opt[Int]) -> Bool { return a == b }
pub fn opt_none(a: Opt[Int]) -> Bool { return a == None }
pub fn opt_float(x: Opt[Float]) -> Bool { return x == 3 }
pub fn opt_int(x: Opt[Int]) -> Bool { return x == 3 }
pub fn same(a: Int, b: Int) -> Bool { return a == b }
pub fn i_var(n: Int) -> Bool { return n == 3 }
pub fn map_float(m: Map[Str, Float]) -> Bool { return m["a"] == 3 }
pub fn map_i32(m: Map[Str, Int32]) -> Bool { return m["a"] == 3 }
pub fn list_index(xs: List[Str]) -> Bool { return xs[0] == "a" }
pub fn i_div(n: Int) -> Bool { return n / 2 == 4 }
pub fn fl(x: Float) -> Bool { return x == 3.0 }
pub fn mixed(x: Float) -> Bool { return x == 3 }
pub fn widened(x: Int) -> Bool { return x == 3.0 }
"""


def test_literal_comparison_emits_strict_equality():
    emitted = _emit_ts(LITERALS)
    for fn, want in [
        ("s_eq", '(s === ("a" as string))'), ("s_ne", '(s !== ("a" as string))'),
        ("s_flip", '(("a" as string) === s)'), ("s_opt", '(s === ("a" as string))'),
        ("b_eq", "(b === (true as boolean))"), ("b_ne", "(b !== (false as boolean))"),
        ("i_arith", "=== (4n as bigint))"), ("i_neg", "=== (3n as bigint))"),
        ("i_index", '(revlMapIndex(m, "a") !== (3n as bigint))'),
        ("s_index", '(revlMapIndex(m, "a") === ("x" as string))'),
        ("both", "((1n as bigint) === (2n as bigint))"),
    ]:
        body = _body(emitted, fn)
        assert want in body, f"{fn}: {body}"
        assert "revlEq(" not in body, f"{fn}: {body}"


def test_untyped_structural_and_mixed_comparisons_keep_revl_eq():
    """An Int literal against an operand whose Int type the IR does not
    state keeps `revlEq`: a bare name may be an `Opt[Float]`, and a Map
    index of Float or Int32 carries no widen marker."""
    emitted = _emit_ts(STRUCTURAL)
    for fn in ("rec", "lst", "opt", "opt_none", "opt_float", "opt_int", "same",
               "i_var", "map_float", "map_i32", "list_index", "i_div", "fl",
               "mixed", "widened"):
        body = _body(emitted, fn)
        assert "revlEq(" in body, f"{fn}: {body}"
        assert "===" not in body, f"{fn}: {body}"
    assert "revlEq(a, b)" in _body(emitted, "rec")
    assert "revlEq(a, b)" in _body(emitted, "lst")
    assert "revlEq(a, b)" in _body(emitted, "opt")
    assert 'revlEq(revlMapIndex(m, "a"), 3n)' in _body(emitted, "map_float")
    # #2715: the Int against Float comparisons are unchanged, byte for byte
    assert "revlEq(x, 3.0)" in _body(emitted, "fl")
    assert "revlEq(x, Number(3n))" in _body(emitted, "mixed")
    assert "revlEq(Number(x), 3.0)" in _body(emitted, "widened")


NARROWED = """
pub fn narrowed(b: Bool, n: Int) -> Bool {
  if (!b) {
    if (b == true) { return true }
  }
  let k = n * 1
  let s = "b"
  return k * 1 == 5 || s == "a" || 1 == 2
}
pub fn map_float(m: Map[Str, Float]) -> Bool { return m["a"] == 3 }
pub fn map_i32(m: Map[Str, Int32]) -> Bool { return m["a"] == 3 }
pub fn opt_float(x: Opt[Float]) -> Bool { return x == 3 }
"""


def _tsc(tsc: Path, path: Path, text: str) -> tuple[int, list[str]]:
    """Typecheck one module; return the exit status and its error lines.

    The two runtime imports are dropped: this pure-function module reads
    neither, and outside the backend directory they do not resolve.
    """
    body = "\n".join(line for line in text.splitlines()
                     if not line.startswith("import "))
    path.write_text(body + "\n", encoding="utf-8")
    result = subprocess.run(
        [str(tsc), "--noEmit", "--strict", "--skipLibCheck", "--target",
         "es2022", "--module", "esnext", "--moduleResolution", "bundler",
         str(path)],
        capture_output=True, text=True, cwd=path.parent)
    return result.returncode, [line for line in result.stdout.splitlines()
                               if "error TS" in line]


def test_strict_equality_typechecks_under_narrowing(tmp_path):
    """A bare `b === true` inside `if (!b)`, `s === "a"` for `const s = "b"`,
    `1n === 2n`, and an Int literal against a `number` (a Float or Int32 Map
    value, an `Opt[Float]`) are TS2367 errors; the emitted module has none."""
    tsc = ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "tsc"
    if not tsc.exists() or shutil.which("node") is None:
        pytest.skip("tsc is not installed (npm ci in backends/typescript)")
    emitted = _emit_ts(NARROWED)
    assert "(b === (true as boolean))" in emitted
    status, errors = _tsc(tsc, tmp_path / "typed.ts", emitted)
    assert (status, errors) == (0, [])
    # control: the same module with bare literals is refused, three times
    bare = emitted
    for typed, plain in [("(true as boolean)", "true"), ('("a" as string)', '"a"'),
                         ("(1n as bigint)", "1n"), ("(2n as bigint)", "2n")]:
        bare = bare.replace(typed, plain)
    # and the shapes that keep revlEq would be refused as `===`
    for call in ('revlEq(revlMapIndex(m, "a"), 3n)', "revlEq(x, 3n)"):
        assert call in bare
        left, right = call[len("revlEq("):-1].rsplit(", ", 1)
        bare = bare.replace(call, f"({left} === {right})")
    status, errors = _tsc(tsc, tmp_path / "bare.ts", bare)
    assert status != 0
    assert len([e for e in errors if "TS2367" in e]) >= 6, errors
    assert all("TS2367" in e for e in errors), errors


RUN = """
pub fn s_eq(s: Str) -> Bool { return s == "a" }
pub fn s_ne(s: Str) -> Bool { return s != "a" }
pub fn i_eq(n: Int) -> Bool { return n == 3 }
pub fn i_eq_flip(n: Int) -> Bool { return 3 == n }
pub fn i_ne(n: Int) -> Bool { return n != 3 }
pub fn neg_eq(n: Int) -> Bool { return n == -5 }
pub fn big_eq(n: Int) -> Bool { return n == 9007199254740993 }
pub fn big_ne(n: Int) -> Bool { return n != 9007199254740993 }
pub fn b_eq(b: Bool) -> Bool { return b == true }
pub fn b_ne(b: Bool) -> Bool { return b != true }
pub fn ar_eq(n: Int) -> Bool { return n * 2 == 6 }
pub fn ar_big(n: Int) -> Bool { return n + 1 == 9007199254740994 }
pub fn neg_ne(n: Int) -> Bool { return -n != 5 }
pub fn f_eq(x: Float) -> Bool { return x == 3 }
pub fn f_lit(x: Int) -> Bool { return x == 3.0 }
pub fn const_var() -> Bool { let k = 4
  return k == 4 && k != 5 }
pub fn lits() -> Bool { return 1 == 1 && 1 != 2 && "a" != "b" && true != false }
pub fn narrowed(b: Bool) -> Bool {
  if (!b) {
    if (b == true) { return true }
    return b == false
  }
  return b == true
}

test "Str against a literal" {
  assert s_eq("a")
  assert !s_eq("b")
  assert !s_eq("")
  assert s_ne("b")
  assert !s_ne("a")
}
test "Int against a literal" {
  assert i_eq(3)
  assert i_eq_flip(3)
  assert !i_eq(4)
  assert !i_eq_flip(4)
  assert i_ne(4)
  assert !i_ne(3)
}
test "negative Int" {
  assert neg_eq(0 - 5)
  assert !neg_eq(5)
  assert !neg_eq(0 - 4)
}
test "Int above 2^53 is exact" {
  assert big_eq(9007199254740993)
  assert !big_eq(9007199254740992)
  assert !big_eq(9007199254740994)
  assert big_ne(9007199254740992)
  assert !big_ne(9007199254740993)
}
test "Int arithmetic against a literal" {
  assert ar_eq(3)
  assert !ar_eq(4)
  assert ar_big(9007199254740993)
  assert !ar_big(9007199254740992)
  assert neg_ne(5)
  assert !neg_ne(0 - 5)
}
test "Bool against a literal" {
  assert b_eq(true)
  assert !b_eq(false)
  assert b_ne(false)
  assert !b_ne(true)
}
test "Int and Float comparisons keep their answers" {
  assert f_eq(3.0)
  assert f_lit(3)
  assert !f_lit(4)
}
test "a variable bound to a literal" {
  assert const_var()
}
test "a literal against a literal" {
  assert lits()
}
test "a narrowed Bool against a literal" {
  assert narrowed(true)
  assert narrowed(false)
}
"""


def test_literal_comparisons_agree_across_values():
    status, message = RUNNERS["ts"](compile_source(RUN, "lit_eq_2901_run.rvl"))
    if status == "skip":
        pytest.skip(f"ts: {message}")
    assert status == "pass", message
    status_py, message_py = RUNNERS["py"](compile_source(RUN, "lit_eq_2901_run.rvl"))
    assert status_py == "pass", f"py reference: {message_py}"

"""List `indexOf`, `Str` ordering and untyped empty lists on wasm, EXECUTED (#3145).

Three construct-level refusals stopped the wasm tier from emitting self-host
files the Go tier already compiles:

    xs.indexOf(s)                 on a List: "the element comparison has no
                                  representation here"
    c >= "A"                      "relational operator '>=' is only lowerable
                                  for Int/Int32" (a Str is ordered too)
    var out = []                  "an untyped empty list literal needs an
                                  expected List type" (the type is what the
                                  block later pushes or returns)

This file emits each shape, validates the module and runs it on `wasmtime`, so
what is claimed is the answer the reference tiers give, not only that the
instruction exists.  The toolchain gate follows this backend's policy
(test_canonical_abi.py): a missing `wasm-tools`/`wasmtime` is FATAL under
REVL_REQUIRE_WASMTIME (CI) and a named skip otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")


def _emitter():
    spec = importlib.util.spec_from_file_location(
        "revl_wasm_emit_3145", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wasmtime_binary() -> str | None:
    found = shutil.which("wasmtime")
    if found:
        return found
    fallback = Path(os.path.expanduser("~/.wasmtime/bin/wasmtime"))
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


def _toolchain() -> tuple[str, str]:
    wasm_tools = shutil.which("wasm-tools")
    wasmtime = _wasmtime_binary()
    if not wasm_tools or not wasmtime:
        if _REQUIRE:
            pytest.fail(
                "wasm-tools/wasmtime are missing, so the #3145 shapes cannot be "
                "executed. REVL_REQUIRE_WASMTIME is set (CI), so this fails "
                "instead of skipping.", pytrace=False)
        pytest.skip("wasm-tools/wasmtime not installed "
                    "(set REVL_REQUIRE_WASMTIME=1 to make this a failure)")
    return wasm_tools, wasmtime


def _emit_text(source: str) -> str:
    ir = compile_source(source)
    emitted = _emitter().emit({
        "ir_version": 3,
        "types": ir.get("types") or {},
        "functions": ir["functions"],
        "externs": [],
        "tests": [],
    })
    return "\n".join(v for v in emitted.values() if isinstance(v, str))


def _build(source: str, tmp: Path, name: str) -> tuple[str, Path]:
    wasm_tools, wasmtime = _toolchain()
    wat, wasm = tmp / f"{name}.wat", tmp / f"{name}.wasm"
    wat.write_text(_emit_text(source), encoding="utf-8")
    subprocess.run([wasm_tools, "parse", str(wat), "-o", str(wasm)],
                   check=True, capture_output=True, text=True, timeout=120)
    subprocess.run([wasm_tools, "validate", "--features", "all", str(wasm)],
                   check=True, capture_output=True, text=True, timeout=120)
    return wasmtime, wasm


def _invoke(wasmtime: str, wasm: Path, export: str, *args: int) -> int:
    result = subprocess.run(
        [wasmtime, "run", "--invoke", export, str(wasm), *[str(a) for a in args]],
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(
            f"{export}({', '.join(str(a) for a in args)}) did not return: "
            f"{result.stderr.strip().splitlines()[-1] if result.stderr.strip() else '?'}")
    return int(result.stdout.strip().splitlines()[-1])


SOURCE = """
pub fn idx_str(k: Int) -> Int {
  let xs = ["a", "bc", "", "bc"]
  if (k == 0) { return xs.indexOf("a") }
  if (k == 1) { return xs.indexOf("bc") }
  if (k == 2) { return xs.indexOf("") }
  if (k == 3) { return xs.indexOf("zz") }
  return xs.indexOf("b")
}
pub fn idx_str_empty_list() -> Int {
  let xs: List[Str] = []
  return xs.indexOf("a")
}
pub fn idx_int(k: Int) -> Int {
  let xs = [10, 20, 30, 20]
  return xs.indexOf(k)
}
pub fn idx_bool(k: Bool) -> Int {
  let xs = [false, false]
  return xs.indexOf(k)
}
pub fn idx_i32(k: Int32) -> Int {
  let xs: List[Int32] = [1.to_int32(), 0.to_int32() - 5.to_int32()]
  return xs.indexOf(k)
}
fn ord_ge(a: Str, b: Str) -> Bool { return a >= b }
fn ord_gt(a: Str, b: Str) -> Bool { return a > b }
fn ord_le(a: Str, b: Str) -> Bool { return a <= b }
fn ord_lt(a: Str, b: Str) -> Bool { return a < b }
fn bit(b: Bool) -> Int { return b ? 1 : 0 }
pub fn ord(k: Int) -> Int {
  if (k == 0) { return bit("A" >= "A") }
  if (k == 1) { return bit("a" >= "A") }
  if (k == 2) { return bit("Z" >= "a") }
  if (k == 3) { return bit("ab" < "abc") }
  if (k == 4) { return bit("abc" < "ab") }
  if (k == 5) { return bit("" < "a") }
  if (k == 6) { return bit("a" < "") }
  if (k == 7) { return bit("b" > "abc") }
  if (k == 8) { return bit("abc" <= "abc") }
  if (k == 9) { return bit("abd" <= "abc") }
  if (k == 10) { return bit("é" > "z") }
  if (k == 11) { return bit("😀" > "豈") }
  return bit("" >= "")
}
fn is_upper1(c: Str) -> Bool { return c >= "A" && c <= "Z" }
pub fn upper(k: Int) -> Int {
  if (k == 0) { return bit(is_upper1("Q")) }
  if (k == 1) { return bit(is_upper1("q")) }
  if (k == 2) { return bit(is_upper1("A")) }
  return bit(is_upper1("["))
}
fn join_all(xs: List[Str]) -> Str {
  var out = []
  var i = 0
  while (i < xs.length()) {
    out = out.push(xs[i].concat("!"))
    i += 1
  }
  return out.join(",")
}
pub fn pushed_in_loop() -> Int {
  return join_all(["a", "bc", "d"]).length()
}
pub fn pushed_for_loop() -> Int {
  var lens = []
  for (s of ["a", "bcd", "ef"]) {
    lens = lens.push(s.concat("."))
  }
  return lens.join("").length()
}
fn evens(n: Int) -> List[Int] {
  var out = []
  var i = 0
  while (i < n) {
    if (i % 2 == 0) { out = out.push(i) }
    i += 1
  }
  return out
}
pub fn returned_only() -> Int { return evens(7).length() }
fn bare() -> List[Str] {
  var acc = []
  return acc
}
pub fn returned_empty() -> Int { return bare().length() }
type Tok = { kind: Str, n: Int }
fn tok_n(t: Tok) -> Int { return t.n }
pub fn pushed_record() -> Int {
  var toks = []
  var i = 0
  while (i < 3) {
    let t = { kind: "k", n: i * 7 }
    if (t.kind != "") { toks = toks.push(t) }
    i += 1
  }
  return tok_n(toks[2]) + toks.length()
}
type Sig = { ok: Bool, params: List[Str], returns: Str }
fn bad() -> Sig { return { ok: false, params: [], returns: "" } }
fn nothing() -> List[Str] { return [] }
pub fn record_field_empty() -> Int {
  return bad().params.length() + nothing().length() + bad().returns.length()
}
pub fn sized_then_pushed() -> Int {
  var xs = []
  let before = xs.length()
  xs = xs.push("x")
  let n = xs.length()
  return before * 10 + n
}
"""


@pytest.fixture(scope="module")
def module(tmp_path_factory):
    return _build(SOURCE, tmp_path_factory.mktemp("w3145"), "m")


@pytest.mark.parametrize("export,args,expected", [
    ("idx_str", (0,), 0),
    ("idx_str", (1,), 1),
    ("idx_str", (2,), 2),
    ("idx_str", (3,), -1),
    ("idx_str", (4,), -1),
    ("idx_str_empty_list", (), -1),
    ("idx_int", (10,), 0),
    ("idx_int", (20,), 1),
    ("idx_int", (30,), 2),
    ("idx_int", (99,), -1),
    ("idx_bool", (0,), 0),
    ("idx_bool", (1,), -1),
    ("idx_i32", (1,), 0),
    ("idx_i32", (-5,), 1),
    ("idx_i32", (7,), -1),
])
def test_list_index_of_answers_what_the_reference_tiers_answer(module, export, args, expected):
    wasmtime, wasm = module
    assert _invoke(wasmtime, wasm, export, *args) == expected


@pytest.mark.parametrize("k,expected", [
    (0, 1), (1, 1), (2, 0), (3, 1), (4, 0), (5, 1), (6, 0), (7, 1),
    (8, 1), (9, 0), (10, 1), (11, 1), (12, 1),
])
def test_str_ordering_is_code_point_order(module, k, expected):
    wasmtime, wasm = module
    assert _invoke(wasmtime, wasm, "ord", k) == expected


@pytest.mark.parametrize("k,expected", [(0, 1), (1, 0), (2, 1), (3, 0)])
def test_the_selfhost_is_upper1_guard(module, k, expected):
    wasmtime, wasm = module
    assert _invoke(wasmtime, wasm, "upper", k) == expected


@pytest.mark.parametrize("export,expected", [
    ("pushed_in_loop", len("a!,bc!,d!")),
    ("pushed_for_loop", len("a.bcd.ef.")),
    ("returned_only", 4),
    ("returned_empty", 0),
    ("pushed_record", 14 + 3),
    ("sized_then_pushed", 1),
    ("record_field_empty", 0),
])
def test_an_untyped_empty_list_takes_its_type_from_how_the_block_uses_it(
        module, export, expected):
    wasmtime, wasm = module
    assert _invoke(wasmtime, wasm, export) == expected


def test_an_empty_list_nothing_types_is_still_refused():
    from revl.errors import RevlError  # noqa: F401
    with pytest.raises(Exception, match="untyped empty list literal"):
        _emit_text("pub fn f() -> Int { let xs = []\n  return xs.length() }")


def test_conflicting_pushes_keep_the_named_refusal():
    with pytest.raises(Exception, match="untyped empty list literal"):
        _emit_text(
            "pub fn f(b: Bool) -> Int {\n  var xs = []\n"
            "  if (b) { xs = xs.push(1) } else { xs = xs.push(\"a\") }\n"
            "  return xs.length() }")


def test_the_shared_ir_is_not_mutated_by_the_inference():
    ir = compile_source("pub fn f() -> Int { var xs = []\n  xs = xs.push(1)\n"
                        "  return xs.length() }")
    before = repr(ir["functions"])
    _emitter().emit({"ir_version": 3, "types": ir.get("types") or {},
                     "functions": ir["functions"], "externs": [], "tests": []})
    assert repr(ir["functions"]) == before


def test_a_list_without_an_element_comparison_keeps_the_named_refusal():
    with pytest.raises(Exception, match="indexOf is not lowerable on this tier yet"):
        _emit_text(
            "type P = { a: Int }\n"
            "pub fn f() -> Int { let xs = [{ a: 1 }]\n"
            "  return xs.indexOf({ a: 1 }) }")


def test_helpers_are_pulled_in_only_when_used():
    text = _emit_text("pub fn f(a: Int) -> Int { return a + 1 }")
    for helper in ("$str_cmp", "$list_index_of_str", "$list_index_of_slot"):
        assert helper not in text

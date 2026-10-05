"""`try e`: Result propagation in a `fn` body. Issue #1900.

A checked parser over a list had to end every element in a placeholder arm,
because a match arm cannot `return` and nothing propagated an `Err`. `let x =
try e` binds the `Ok` payload of `e` and, when `e` is an `Err`, returns that
`Err` from the enclosing fn (docs/syntax-2.0.md §3.6).

The decision on #1900 fixes the shape:

* a CONTEXTUAL prefix operator (an identifier everywhere it does not start an
  operand, so `fn try`, `s.try(x)` and a local named `try` still compile),
  allowed only as a whole `let` initializer or `return`
  operand of a `fn` or `verified fn` body, and not in a provide method;
* `try e` has type `T` for `e: Result[T, E]`, and the enclosing fn must return
  `Result[_, E]` with the same `E`, else it is refused naming `E`;
* an `emit` operand is refused;
* it desugars to IR that already exists, with no new IR kind.

tests/fixtures/try_expr/ is the corpus: `ok_` admitted (each with an in-file
test the tiers run), `t1_` refused. tests/test_gate_reference_census.py holds
the self-host gate to the same verdicts, and tests/test_selfhost_lower_ir.py
holds its IR to the reference's byte for byte.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "try_expr"

_POSITION = ("`try` may only stand as a whole `let` initializer or `return` "
             "operand in a `fn` body (issue #1900)")

REFUSED = {
    "t1_operand_not_result":
        "the operand of `try` expects `Result[_, _]`, got `Int`",
    "t1_return_not_result":
        "`try` propagates `Err(Str)`, so the enclosing fn's return expects "
        "`Result[_, Str]`, got `Int`",
    "t1_no_return_type":
        "`try` propagates `Err(Str)`, so the enclosing fn's return must be "
        "`Result[_, Str]`, but it declares none",
    "t1_different_err":
        "`try` propagates `Err(Int)`, so the enclosing fn's return expects "
        "`Result[_, Int]`, got `Result[Int, Str]`",
    "t1_nested_operand": _POSITION,
    "t1_call_argument": _POSITION,
    "t1_condition": _POSITION,
    "t1_annotation_mismatch": "`let x: Str` expects `Str`, got `Int`",
    "t1_return_payload_mismatch":
        "this function's return expects `Result[Int, Str]`, got `Int`",
    "t1_emit_operand":
        "`try` cannot take an `emit` operand: bind the crossing's value with "
        "`let` first (issue #1900)",
    "t1_provide_method":
        "`try` is not allowed in a provide method or component body yet: a "
        "returned `Err` does not settle the unit there (issue #1900)",
}
ADMITTED = ("ok_annotated", "ok_branches", "ok_contextual_name", "ok_fresh_names",
            "ok_list_parser", "ok_return_operand", "ok_sequence")

_PARSE = 'fn parse(n: Int) -> Result[Int, Str] { return n > 0 ? Ok(n) : Err("neg") }\n'


def _src(stem: str) -> str:
    return (CORPUS / f"{stem}.rvl").read_text(encoding="utf-8")


def _kinds(node, out: set) -> set:
    if isinstance(node, dict):
        if "kind" in node:
            out.add((node["kind"], node.get("op")))
        for value in node.values():
            _kinds(value, out)
    elif isinstance(node, list):
        for value in node:
            _kinds(value, out)
    return out


def test_the_corpus_is_the_refusals_and_the_admissions():
    assert sorted(p.stem for p in CORPUS.glob("*.rvl")) == sorted(
        list(REFUSED) + list(ADMITTED))


@pytest.mark.parametrize("stem", sorted(REFUSED))
def test_the_reference_refuses_the_document(stem):
    with pytest.raises(RevlError) as excinfo:
        compile_source(_src(stem), f"{stem}.rvl")
    assert excinfo.value.message == REFUSED[stem]
    assert excinfo.value.code == "T1"


@pytest.mark.parametrize("stem", ADMITTED)
def test_the_reference_admits_the_document_with_no_new_ir_kind(stem):
    ir = compile_source(_src(stem), f"{stem}.rvl")
    kinds = _kinds(ir["functions"], set())
    assert ("un", "try") not in kinds
    assert "try" not in {k for k, _ in kinds}


def test_the_desugar_is_the_decided_shape():
    """`let x = try e` is a `let` of the operand, an `if` over a two-arm
    boolean match whose then-branch returns the `Err`, and the binding read
    out of a one-armed `Ok` match. The condition's arms bind fresh names, one
    per case, so no name is bound at two types (one wasm local each)."""
    src = _PARSE + ("fn f(n: Int) -> Result[Int, Str] {\n"
                    "  let x = try parse(n)\n  return Ok(x)\n}\n")
    body = [f for f in compile_source(src)["functions"] if f["name"] == "f"][0]["body"]
    var = {"kind": "var", "name": "try_0"}
    assert body[0] == {"step": "let", "name": "try_0", "mutable": False,
                       "value": {"kind": "call", "callee": {"kind": "var", "name": "parse"},
                                 "args": [{"kind": "var", "name": "n"}]}}
    assert body[1] == {"step": "if", "else": None, "cond": {
        "kind": "match", "scrutinee": var, "arms": [
            {"pattern": "Ok", "bind": "try_0_ok", "body": {"kind": "lit", "value": False},
             "payload_type": "Int"},
            {"pattern": "Err", "bind": "try_0_err", "body": {"kind": "lit", "value": True},
             "payload_type": "Str"}]},
        "then": [{"step": "return", "expr": {"kind": "match", "scrutinee": var, "arms": [
            {"pattern": "Err", "bind": "try_0_err", "payload_type": "Str",
             "body": {"kind": "adt", "type": "Result[Any, Any]", "case": "Err",
                      "args": [{"kind": "var", "name": "try_0_err"}]}}]}}]}
    assert body[2] == {"step": "let", "name": "x", "mutable": False, "value": {
        "kind": "match", "scrutinee": var, "arms": [
            {"pattern": "Ok", "bind": "try_0_ok", "payload_type": "Int",
             "body": {"kind": "var", "name": "try_0_ok"}}]}}


def test_the_fresh_names_skip_every_name_the_fn_spells():
    """In ok_fresh_names the fn spells `try_0` and `try_1_ok` itself, and
    `try_2_err` only inside an interpolation (a module fn it calls there). A
    `k` is skipped when any of `try_k`, `try_k_err`, `try_k_ok` is spelled, so
    0, 1 and 2 are all ruled out and the fn's two `try`s take 3 and 4."""
    body = [f for f in compile_source(_src("ok_fresh_names"))["functions"]
            if f["name"] == "names"][0]["body"]
    temps = [s["name"] for s in body if s["step"] == "let" and s["name"].startswith("try_")]
    # the author's own `let try_0` and `let try_1_ok`, then the two desugars
    assert temps == ["try_0", "try_3", "try_1_ok", "try_4"]


@pytest.mark.parametrize("decl", ["fn", "verified fn"])
def test_a_var_binding_and_a_verified_fn_are_admitted(decl):
    src = _PARSE + (f"{decl} f(n: Int) -> Result[Int, Str] {{\n"
                    "  var x = try parse(n)\n  x = x + 1\n  return Ok(x)\n}\n")
    compile_source(src)


def test_an_expression_bodied_fn_is_a_return_operand():
    src = _PARSE + ("fn wrap(n: Int) -> Result[Result[Int, Str], Str] { return Ok(parse(n)) }\n"
                    "fn f(n: Int) -> Result[Int, Str] = try wrap(n)\n")
    compile_source(src)


def test_a_block_arm_refuses_try():
    """The arm is lambda-lifted into a helper fn, so its `return` could only
    leave the helper (docs/records.md §4)."""
    src = _PARSE + ("fn f(n: Int) -> Result[Int, Str] {\n"
                    "  let z = match parse(n) { Ok(v) => {\n"
                    "    let w = try parse(v)\n    w\n  }, Err(e) => 0 }\n"
                    "  return Ok(z)\n}\n")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    assert excinfo.value.message == (
        "`try` is not allowed in a match block arm: the arm is lifted into a "
        "helper fn, so it cannot return from this one (issue #1900)")


def test_a_test_body_has_no_result_to_return():
    src = _PARSE + 'test "t" {\n  let x = try parse(1)\n  assert x == 1\n}\n'
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    assert excinfo.value.message == REFUSED["t1_no_return_type"]


def test_try_is_contextual_not_a_keyword():
    """`try` is the prefix operator only before a token that starts its
    operand and could not follow a plain name. A fn, a provide method and a
    local may still be named `try`, and a call `try(x)`, `try - 1` or a method
    `s.try(x)` keep the meaning they had before the operator existed."""
    from revl.lexer import KEYWORDS
    assert "try" not in KEYWORDS
    src = ("service S { fn try(x: Int) -> Int }\n"
           "component P provides s: S { provide s { fn try(x) { return x + 1 } } }\n"
           "fn try(n: Int) -> Int { return n + 1 }\n"
           "fn f(n: Int) -> Int {\n  let a = try(n)\n  return a\n}\n"
           "fn g(n: Int) -> Int {\n  let try = n\n  return try - 1\n}\n")
    ir = compile_source(src)
    f = [x for x in ir["functions"] if x["name"] == "f"][0]
    assert f["body"][0]["value"]["kind"] == "call"
    assert "try_0" not in json.dumps(ir)


# ---- the tiers run it ---------------------------------------------------------

def _run(backend: str, stem: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "revl", "test", "--backend", backend,
         str(CORPUS / f"{stem}.rvl")],
        capture_output=True, text=True, timeout=900, cwd=ROOT)


needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the py runner needs the pinned cordis fork (backends/python/setup.sh)")
needs_node = pytest.mark.skipif(
    not (ROOT / "backends" / "typescript" / "node_modules").exists()
    or shutil.which("node") is None,
    reason="the ts runner needs backends/typescript/node_modules and node")


@needs_cordis
@pytest.mark.parametrize("stem", ADMITTED)
def test_py_runs_it(stem):
    proc = _run("py", stem)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[py] pass" in proc.stdout + proc.stderr


@needs_node
@pytest.mark.parametrize("stem", ADMITTED)
def test_ts_runs_it(stem):
    proc = _run("ts", stem)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[ts] pass" in proc.stdout + proc.stderr


needs_wasmtime = pytest.mark.skipif(
    shutil.which("wasmtime") is None, reason="the wasm runner needs wasmtime")


# ok_annotated returns a `Result[Float, Str]`, and a Float payload is outside
# the wasm tier's value surface; the rest run there.
@needs_wasmtime
@pytest.mark.parametrize("stem", [s for s in ADMITTED if s != "ok_annotated"])
def test_wasm_runs_it(stem):
    proc = _run("wasm", stem)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[wasm] pass" in proc.stdout + proc.stderr


def test_the_wasm_golden_source_is_the_corpus_list_parser():
    """backends/wasm/golden/try_expr.wat is emitted from try_expr.revl, whose
    functions are the corpus's checked list parser; the byte check is
    backends/wasm/test_v3_emit.py's."""
    golden = ROOT / "backends" / "wasm" / "golden" / "try_expr.revl"
    ir = compile_source(golden.read_text(encoding="utf-8"))
    assert {f["name"] for f in ir["functions"]} >= {"parse_field", "parse_fields"}
    assert json.dumps(ir["functions"]).count('"try_0"') > 0

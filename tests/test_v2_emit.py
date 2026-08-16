"""v2.0: type/fn lowering to IR v3 and cordis-py emission (syntax-2.0 §2–§3)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import emit  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402


def _compile_emit(source):
    ir = compile_source(source)
    ns = {}
    exec(compile(emit.emit(ir), "emitted.py", "exec"), ns)
    return ir, ns


def test_types_and_fns_lower_to_ir_v3():
    ir = compile_source(
        """
        type Row = { id: Int, name: Str }
        type Outcome = Ok(Row) | NotFound | Invalid(Str)
        fn add(a: Int, b: Int) -> Int { return a + b }
        """
    )
    assert ir["ir_version"] == 3
    assert ir["types"]["Row"] == {"params": [], "kind": "record", "fields": {"id": "Int", "name": "Str"}}
    assert ir["types"]["Outcome"]["kind"] == "variant"
    assert [c["name"] for c in ir["types"]["Outcome"]["cases"]] == ["Ok", "NotFound", "Invalid"]
    assert ir["functions"][0]["name"] == "add"


def test_v1_documents_stay_at_version_1():
    ir = compile_source("service A { fn ping() } component C { let x = effect Map.new() undo x.drop() }")
    assert ir["ir_version"] == 1
    assert "types" not in ir and "functions" not in ir


def test_emit_executes_types_and_functions():
    _, ns = _compile_emit(
        """
        type Row = { id: Int, name: Str }
        type Outcome = Ok(Row) | NotFound | Invalid(Str)

        fn add(a: Int, b: Int) -> Int { return a + b }
        fn classify(n: Int) -> Str {
          if (n < 0) return "neg"
          return n === 0 ? "zero" : "pos"
        }
        fn first(xs: List[Int]) -> Int { return xs[0] }

        component Pinger { let p = effect Map.new() undo p.drop() }
        """
    )
    assert ns["add"](1, 2) == 3
    assert ns["classify"](-1) == "neg"
    assert ns["classify"](0) == "zero"
    assert ns["classify"](5) == "pos"
    assert ns["first"]([7, 8]) == 7
    row = ns["Row"](id=1, name="x")
    assert row.id == 1 and row.name == "x"
    assert issubclass(ns["Ok"], ns["Outcome"])
    assert isinstance(ns["NotFound"](), ns["Outcome"])
    assert ns["Invalid"]("bad").value == "bad"


def test_match_expression_emits_and_executes():
    _, ns = _compile_emit(
        """
        type Row = { id: Int, name: Str }
        type Outcome = Ok(Row) | NotFound | Invalid(Str)

        fn describe(outcome: Outcome) -> Str {
          return match outcome {
            Ok(row) => row.name,
            NotFound => "not found",
            Invalid(why) => why,
          }
        }
        fn label(outcome: Outcome) -> Str {
          return match outcome {
            Ok(row) => row.name,
            _ => "other",
          }
        }
        """
    )
    row = ns["Row"](id=1, name="ada")
    assert ns["describe"](ns["Ok"](row)) == "ada"
    assert ns["describe"](ns["NotFound"]()) == "not found"
    assert ns["describe"](ns["Invalid"]("bad")) == "bad"
    assert ns["label"](ns["NotFound"]()) == "other"
    assert ns["label"](ns["Ok"](ns["Row"](id=2, name="bob"))) == "bob"


def test_match_nonexhaustive_over_known_variant_rejected():
    with pytest.raises(RevlError, match=r"non-exhaustive match: missing case `Invalid`"):
        compile_source(
            """
            type Row = { id: Int, name: Str }
            type Outcome = Ok(Row) | NotFound | Invalid(Str)
            fn describe(outcome: Outcome) -> Str {
              return match outcome {
                Ok(row) => row.name,
                NotFound => "-",
              }
            }
            """
        )


def test_let_reassignment_rejected():
    with pytest.raises(RevlError, match="cannot reassign `n`"):
        compile_source("fn f() -> Int { let n = 1 n = 2 return n }")


def test_undeclared_var_rejected():
    with pytest.raises(RevlError, match="`missing` is not declared"):
        compile_source("fn f() -> Int { return missing }")


def test_duplicate_field_rejected():
    with pytest.raises(RevlError, match="duplicate field `id`"):
        compile_source("type Row = { id: Int, id: Str }")


def test_duplicate_case_rejected():
    with pytest.raises(RevlError, match="duplicate case `NotFound`"):
        compile_source("type T = A | NotFound | NotFound")

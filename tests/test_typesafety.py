"""Sound-typing milestone: definite mismatches between known types are
compile errors; unknown positions stay silent (the gradual frontier).

Each test names the discipline it pins. Sources are inline; the two
headline cases also exist as rejection files (T1/T2)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


def _err(source: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source)
    return str(excinfo.value)


DB = "service Database { fn query(sql: Str) -> List[Row]\n  emission fn execute(sql: Str) -> Int }\n"


# ---- service boundaries ----------------------------------------------------

def test_service_arg_type_mismatch():
    err = _err(DB + """
component P requires db: Database {
  let rows = effect db.query(42) undo db.query("x")
}""")
    assert "`db.query` argument `sql` expects `Str`, got `Int`" in err


def test_service_return_feeds_inference():
    # rows: List[Row] via the service's declared return; List[Row] into a
    # Str-typed argument is a definite mismatch two steps later
    err = _err(DB + """
component P requires db: Database {
  let rows = effect db.query("a") undo db.query("x")
  let again = effect db.query(rows) undo db.query("y")
}""")
    assert "expects `Str`, got `List[Row]`" in err


def test_provide_method_params_are_service_typed():
    err = _err("""
service Cache { fn get(key: Str) -> Opt[Str] }
service Database { fn query(sql: Str) -> List[Row] }
component C requires db: Database provides cache: Cache {
  provide cache {
    fn get(key) {
      emit db.execute(key)
      return None
    }
  }
}""".replace("fn query(sql: Str) -> List[Row]",
             "fn query(sql: Str) -> List[Row]\n  emission fn execute(n: Int) -> Int"))
    assert "`db.execute` argument `n` expects `Int`, got `Str`" in err


def test_provide_method_return_checked():
    err = _err("""
service Cache { fn get(key: Str) -> Opt[Str] }
component C provides cache: Cache {
  provide cache { fn get(key) { return 42 } }
}""")
    assert "`get` returns expects `Opt[Str]`, got `Int`" in err


# ---- null safety -----------------------------------------------------------

def test_null_rejected_in_component_expression():
    err = _err(DB + """
component P requires db: Database {
  let rows = effect db.query(null) undo db.query("x")
}""")
    assert "`null` has no type in revl" in err


def test_null_rejected_in_fn_body():
    err = _err("fn f() -> Int { return null }")
    assert "`null` has no type in revl" in err


def test_none_is_the_typed_absence():
    ir = compile_source("""
service Cache { fn get(key: Str) -> Opt[Str] }
component C provides cache: Cache {
  provide cache { fn get(key) { return None } }
}""")
    assert ir["components"][0]["name"] == "C"


# ---- Opt discipline --------------------------------------------------------

def test_opt_does_not_flow_into_plain_type():
    err = _err("""
fn first(xs: List[Str]) -> Opt[Str] { return Some("x") }
fn shout(s: Str) -> Str { return s.concat("!") }
fn f(xs: List[Str]) -> Str { return shout(first(xs)) }
""")
    assert "expects `Str`, got `Opt[Str]`" in err
    assert "unwrap" in err  # the model-facing hint


def test_plain_type_flows_into_opt():
    ir = compile_source("fn f() -> Opt[Str] { return \"present\" }")
    assert ir["functions"][0]["returns"] == "Opt[Str]"


# ---- pure functions --------------------------------------------------------

def test_fn_arg_type_mismatch():
    err = _err("""
fn double(n: Int) -> Int { return n * 2 }
fn f() -> Int { return double("nope") }
""")
    assert "argument 1 of `double(...)` expects `Int`, got `Str`" in err


def test_fn_return_type_mismatch():
    err = _err("fn f() -> Int { return \"nope\" }")
    assert "this function's return expects `Int`, got `Str`" in err


def test_var_keeps_its_type():
    err = _err("""
fn f() -> Int {
  var n = 0
  n = "drift"
  return n
}""")
    assert "assignment to `n` (a `Int` variable) expects `Int`, got `Str`" in err


def test_operator_mismatch():
    err = _err("fn f(s: Str) -> Int { return s - 1 }")
    assert "operand of `-` expects `Int`, got `Str`" in err


def test_condition_must_be_bool():
    err = _err("fn f(n: Int) -> Int { if (n) { return 1 } return 0 }")
    assert "`if` condition expects `Bool`, got `Int`" in err


def test_record_field_and_unknown_field():
    err = _err("""
type Row = { id: Int, name: Str }
fn f(r: Row) -> Str { return r.nom }
""")
    assert "`Row` has no field `nom`" in err


def test_record_literal_checked_against_expected():
    err = _err("""
type Row = { id: Int, name: Str }
fn mk() -> Row { return { id: 1 } }
""")
    assert "record literal for `Row` has missing `name`" in err


def test_unknown_positions_stay_silent():
    # host-valued objects are the documented gradual frontier: no false errors
    ir = compile_source("""
component P {
  let pool = effect Pool.open("u", 1) undo pool.close()
}""")
    assert ir["components"][0]["name"] == "P"

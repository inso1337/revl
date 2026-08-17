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


# ---- match in check-position (HOLE 3(b)) -----------------------------------

def test_match_arm_checked_in_check_position():
    # disagreeing arms join to an unknown type; per-arm check-position still
    # catches the arm that violates the expected return type
    err = _err("""
type S = A | B
fn f(s: S) -> Int { return match s { A => 1, B => "back" } }
""")
    assert "this function's return expects `Int`, got `Str`" in err


def test_match_agreeing_arms_still_accepted():
    ir = compile_source("""
type S = A | B
fn f(s: S) -> Int { return match s { A => 1, B => 2 } }
""")
    assert ir["functions"][0]["returns"] == "Int"


# ---- nullary ADT ctor as a value in a `test` block (HOLE 1) ----------------

def test_nullary_ctor_resolves_in_test_block():
    # a test body is the same expression scope a fn body is: a bare nullary
    # variant resolves as a value instead of being rejected as undeclared
    ir = compile_source("""
type State = FirstTime | Returning
fn describe(s: State) -> Str { return match s { FirstTime => "new", Returning => "back" } }
test "nullary ctor is a value" { let s = FirstTime  assert describe(s) == "new" }
""")
    let_step = ir["tests"][0]["body"][0]
    assert let_step["value"] == {"kind": "adt", "type": "State",
                                 "case": "FirstTime", "args": []}


# ---- component effect-setup op sweep (HOLE 2 / HOLE 3(c)) -------------------

SETUP = ("component C {{ config {{ p: Int = 1 }}\n"
         "  let c = effect {{ {body}  Pool.open(\"u\", config.p) }} undo c.close()\n}}")


def test_setup_operator_mismatch_rejected():
    err = _err(SETUP.format(body="let bad = 1 + true"))
    assert "operand of `+` expects `Int`, got `Bool`" in err


def test_setup_if_condition_must_be_bool():
    err = _err(SETUP.format(body="if (5) { let q = 1 }"))
    assert "`if` condition expects `Bool`, got `Int`" in err


def test_setup_bogus_stdlib_method_rejected():
    err = _err(SETUP.format(body="let m = \"h\"  let z = m.bogusMethod()"))
    assert "no builtin method `bogusMethod` on `Str`" in err


def test_setup_valid_stdlib_and_operators_accepted():
    ir = compile_source(SETUP.format(
        body="let region = config.p  let label = \"c\" + \"onn\""))
    assert ir["components"][0]["name"] == "C"


def test_setup_host_method_stays_silent():
    # host-object methods are the documented, fenced gradual frontier: a method
    # on a host-provenance local (a Map) infers to an unknown and is left alone
    ir = compile_source("""
component Cache {
  let store = effect Map.new() undo store.drop()
  let warm = effect { let n = store.size()  Pool.open("u", n) } undo warm.close()
}""")
    assert ir["components"][0]["name"] == "Cache"

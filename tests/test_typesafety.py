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


# ---- typing follow-ups: programs the strict tiers refuse -------------------
#
# Each block below closed a hole where the checker accepted a program that
# rust/java will not compile. The rejection is paired with the legal spellings
# it must NOT touch: the guarantee is "refuse the unportable", not "refuse the
# unfamiliar".


# 1/2 — a declared return type must be produced on every path.
# python silently returns None, TS undefined; rust E0308, javac
# "missing return statement".

def test_fn_body_that_never_returns_is_rejected():
    err = _err("fn f() -> Int { let x = 1 }")
    assert "`f` is declared to return `Int` but its body never returns a value" in err
    assert "rust E0308" in err  # the diagnostic names the tier that refuses it


def test_fn_with_bare_if_falls_off_the_end():
    err = _err("fn f(b: Bool) -> Int { if (b) { return 1 } }")
    assert "control can reach the end of its body without a `return`" in err


def test_loop_body_return_does_not_count_as_a_path():
    # a `for`/`while` may run zero times — java and rust agree
    err = _err("fn f(xs: List[Int]) -> Int { for (x of xs) { return x } }")
    assert "control can reach the end of its body without a `return`" in err


def test_returning_fns_that_are_legal_stay_accepted():
    ir = compile_source("""
fn unit_needs_no_return(x: Int) { let y = x + 1 }
fn both_arms(b: Bool) -> Int { if (b) { return 1 } else { return 2 } }
fn nested(b: Bool, c: Bool) -> Int {
  if (b) { if (c) { return 1 } else { return 2 } } else { return 3 }
}
fn trailing(b: Bool) -> Int { if (b) { return 1 }  return 2 }
fn after_loop(xs: List[Int]) -> Int { for (x of xs) { let y = x }  return 0 }
fn diverging() -> Int { while (true) { let x = 1 } }
""")
    assert [fn["name"] for fn in ir["functions"]] == [
        "unit_needs_no_return", "both_arms", "nested", "trailing",
        "after_loop", "diverging"]


# 3 — call arity is exact (no defaults, no varargs): rust E0061,
# javac "cannot be applied to given types".

def test_call_with_too_many_arguments_is_rejected():
    err = _err("fn g(a: Int) -> Int { return a }\nfn h() -> Int { return g(1, 2) }")
    assert "`g` takes 1 argument(s), 2 given" in err


def test_call_with_too_few_arguments_is_rejected():
    err = _err("fn g(a: Int, b: Int) -> Int { return a + b }\n"
               "fn h() -> Int { return g(1) }")
    assert "`g` takes 2 argument(s), 1 given" in err


def test_extern_call_arity_is_checked_too():
    err = _err("extern pure fn hash(s: Str) -> Int\n"
               "  = @py { return hash(s) }\n"
               'fn h() -> Int { return hash("a", "b") }')
    assert "`hash` takes 1 argument(s), 2 given" in err


def test_correct_arity_still_accepted():
    ir = compile_source("fn g(a: Int, b: Int) -> Int { return a + b }\n"
                        "fn h() -> Int { return g(1, 2) }")
    assert len(ir["functions"]) == 2


# 4 — nothing reaches *through* an Opt. `T` flows into `Opt[T]` and never
# silently back out (README headline): rust E0609, javac "cannot find symbol".

OPT = "type Row = { name: Str, tags: List[Str] }\n"


def test_field_access_through_an_optional_is_rejected():
    err = _err(OPT + "fn f(o: Opt[Row]) -> Str { return o.name }")
    assert "field access `.name` on `Opt[Row]`" in err
    assert "`T` flows into `Opt[T]`, never silently back out" in err
    assert "?.name" in err  # the diagnostic names the fix


def test_index_through_an_optional_is_rejected():
    err = _err("fn f(o: Opt[List[Int]]) -> Int { return o[0] }")
    assert "index `[...]` on `Opt[List[Int]]`" in err


def test_builtin_method_on_an_optional_is_rejected():
    err = _err("fn f(o: Opt[Str]) -> Int { return o.length }")
    assert "field access `.length` on `Opt[Str]`" in err


def test_optional_chain_yields_an_optional_not_the_inner_type():
    # `o?.name` is `Opt[Str]`, so returning it as `Str` is the ordinary
    # unwrap-first mismatch rather than a silent escape
    err = _err(OPT + "fn f(o: Opt[Row]) -> Str { return o?.name }")
    assert "this function's return expects `Str`, got `Opt[Str]`" in err


def test_optional_chain_on_a_nonoptional_is_rejected():
    err = _err(OPT + "fn f(r: Row) -> Opt[Str] { return r?.name }")
    assert "`?.` needs an optional on the left, got `Row`" in err


def test_optional_chain_field_must_exist_on_the_inner_record():
    err = _err(OPT + "fn f(o: Opt[Row]) -> Opt[Str] { return o?.bogus }")
    assert "`Row` has no field `bogus`" in err


def test_legal_optional_uses_stay_accepted():
    ir = compile_source(OPT + """
type Outer = { row: Row }
fn chained(o: Opt[Outer]) -> Opt[Str] { return o?.row?.name }
fn one_hop(o: Opt[Row]) -> Opt[Str] { return o?.name }
fn with_fallback(o: Opt[Row]) -> Str { return o?.name ?? "anonymous" }
fn unwrapped(o: Opt[Row]) -> Str { return match o { Some(r) => r.name, None => "" } }
fn injected(r: Row) -> Opt[Row] { return Some(r) }
fn opt_method(o: Opt[Str]) -> Opt[Int] { return o?.length() }
""")
    assert len(ir["functions"]) == 6


def test_field_through_optional_rejected_in_a_component_body():
    # the same discipline inside the component effect-setup sweep: the
    # requirement's declared `Opt[Row]` return may not be reached through
    err = _err("""
type Row = { name: Str }
service Finder { fn find(k: Str) -> Opt[Row] }
component C requires fx: Finder {
  let hit = effect fx.find("k") undo fx.find("k")
  let c = effect { let n = hit.name  Pool.open(n, 1) } undo c.close()
}""")
    assert "the optional wrapper has no such member" in err


# 5 — `Str` is not indexable; the specified surface is charAt/slice.
# rust E0277, javac "cannot find symbol" (the emitter renders `.get(i)`).

def test_str_index_is_rejected():
    err = _err("fn f(s: Str) -> Str { return s[0] }")
    assert "`Str` has no index operator" in err
    assert "s.charAt(i)" in err


def test_list_index_and_charat_stay_accepted():
    ir = compile_source("""
fn nth(xs: List[Int]) -> Int { return xs[0] }
fn ch(s: Str) -> Str { return s.charAt(0) }
fn code(s: Str) -> Int { return s.charCodeAt(0) }
fn sub(s: Str) -> Str { return s.slice(0, 2) }
""")
    assert len(ir["functions"]) == 4


# 6 — a match arm must name a declared case (or `_`).
# javac "cannot find symbol"; the rust emitter raises EmitError.

def test_unknown_match_case_is_rejected():
    err = _err("type Status = Active | Retired\n"
               "fn f(s: Status) -> Int { return match s { Active => 1, Retired => 2, Pending => 3 } }")
    assert "`Pending` is not a case of `Status` (cases: `Active`, `Retired`)" in err


def test_unknown_match_case_rejected_even_with_a_wildcard_arm():
    # `_` silences exhaustiveness, but a *misspelled* case is still a typo
    err = _err("type Status = Active | Retired\n"
               "fn f(s: Status) -> Int { return match s { Actve => 1, _ => 0 } }")
    assert "`Actve` is not a case of `Status`" in err


def test_wildcard_and_payload_arms_stay_accepted():
    ir = compile_source("""
type Status = Active | Retired
type Outcome = Won(Int) | Lost(Str)
fn a(s: Status) -> Int { return match s { Active => 1, _ => 0 } }
fn b(o: Outcome) -> Int { return match o { Won(n) => n, Lost(why) => why.length } }
fn c(o: Opt[Int]) -> Int { return match o { Some(v) => v, None => 0 } }
fn d(r: Result[Int, Str]) -> Int { return match r { Ok(v) => v, Err(e) => 0 } }
""")
    assert len(ir["functions"]) == 4


# ---- generic instantiation (roadmap "Typing follow-ups" #6) ----------------
#
# A single-uppercase name in a fn signature that is not a declared type is that
# fn's implicit type parameter. It is a wildcard only inside that fn's own
# body; at a call site it is unified against the actual arguments. A single
# uppercase name that *is* declared is an ordinary nominal type.

def test_generic_return_is_instantiated_at_the_call_site():
    err = _err("fn ident(x: T) -> T { return x }\n"
               'fn g() -> Int { return ident("hello") }')
    assert "this function's return expects `Int`, got `Str`" in err


def test_generic_parameters_must_agree_across_arguments():
    err = _err("fn pair(a: T, b: T) -> T { return a }\n"
               'fn g() -> Int { return pair(1, "x") }')
    assert "argument 2 of `pair(...)` expects `Int`, got `Str`" in err


def test_generic_under_a_constructor_is_instantiated():
    err = _err("fn head(xs: List[T]) -> T { return xs[0] }\n"
               "fn g() -> Str { return head([1, 2]) }")
    assert "this function's return expects `Str`, got `Int`" in err


def test_independent_type_parameters_do_not_collide():
    ir = compile_source("fn fst(a: T, b: E) -> T { return a }\n"
                        'fn g() -> Int { return fst(1, "x") }')
    assert len(ir["functions"]) == 2


def test_generic_opt_injection_still_holds_at_the_call_site():
    ir = compile_source("fn wrap(x: T) -> Opt[T] { return Some(x) }\n"
                        "fn g() -> Opt[Int] { return wrap(1) }")
    assert len(ir["functions"]) == 2


def test_declared_one_letter_type_is_no_longer_a_wildcard():
    # `type S = A | B` used to be unchecked *everywhere* purely because its
    # name is one character long — a strictly worse hole than the generics one
    err = _err("type S = A | B\nfn f(s: S) -> Int { return s }")
    assert "this function's return expects `Int`, got `S`" in err


def test_declared_one_letter_type_still_flows_where_it_should():
    ir = compile_source("type S = A | B\nfn f(s: S) -> S { return s }")
    assert ir["functions"][0]["returns"] == "S"


def test_type_parameter_marker_never_reaches_the_ir():
    ir = compile_source("fn ident(x: T) -> T { return x }\n"
                        'fn g() -> Str { return ident("hi") }')
    assert ir["functions"][0]["returns"] == "T"
    assert ir["functions"][0]["params"] == [{"name": "x", "type": "T"}]


# ---- the same checks inside arrow bodies and component bodies -------------

def test_arrow_bodies_are_checked_like_any_expression():
    err = _err("fn f(s: Str) -> Int { let g = (x) => s[0]\n  return 1 }")
    assert "`Str` has no index operator" in err


def test_arrow_body_call_arity_is_checked():
    err = _err("fn h(a: Int) -> Int { return a }\n"
               "fn f() -> Int { let g = (x) => h(1, 2)\n  return 1 }")
    assert "`h` takes 1 argument(s), 2 given" in err


def test_arrow_parameters_stay_unknown():
    # an arrow's own parameters are un-annotated: the body must still typecheck
    # against the enclosing scope without inventing types for them
    ir = compile_source("fn f(s: Str) -> Int { let g = (x) => x + 1\n"
                        "  let h = (s) => s + 1\n  return 1 }")
    assert ir["functions"][0]["name"] == "f"


def test_component_body_call_arity_is_checked():
    err = _err("fn h(a: Int) -> Int { return a }\n"
               'component C { let c = effect { let n = h(1, 2)  Pool.open("u", n) }'
               " undo c.close() }")
    assert "`h` takes 1 argument(s), 2 given" in err


def test_component_body_call_argument_type_is_checked():
    err = _err("fn h(a: Int) -> Int { return a }\n"
               'component C { let c = effect { let n = h("x")  Pool.open("u", n) }'
               " undo c.close() }")
    assert "argument 1 of `h(...)` expects `Int`, got `Str`" in err


def test_provide_method_must_return_what_the_service_promises():
    err = _err("service Store { fn get(key: Str) -> Str }\n"
               "component M provides store: Store {\n"
               "  provide store { fn get(key) { let n = key } }\n}")
    assert "`get` implements `Store.get`, which returns `Str`, but this body never returns a value" in err


def test_provide_method_with_no_declared_return_needs_none():
    ir = compile_source("service Sink { fn ping(key: Str) }\n"
                        "component M provides sink: Sink {\n"
                        "  provide sink { fn ping(key) { let n = key } }\n}")
    assert ir["components"][0]["name"] == "M"


# ---- `type X = Y` is a transparent alias ----------------------------------
#
# It used to parse as a one-case variant whose single case was named `Y`, so
# `type Sku = Str` made the author's own alias unusable (`f("abc")` refused for
# a `Sku` parameter) while `return Str` was accepted as a case constructor.
#
# syntax-2.0's governing principle is that no construct may exist in both
# languages with silently different meaning, so the split follows TypeScript's
# own (verified against tsc --strict):
#   `type Sku = string`             tsc: compiles, transparent both ways
#   `type Sku = Ident`   (undecl.)  tsc: error TS2304 "Cannot find name"
#   `type K = Ident | Keyword`      tsc: error TS2304
# Where TypeScript compiles it, revl now means the same thing. Where
# TypeScript rejects it, revl keeps its variant reading — there is no shared
# meaning to diverge from.

def test_alias_accepts_its_target_type():
    ir = compile_source('type Sku = Str\n'
                        'fn f(s: Sku) -> Sku { return s }\n'
                        'fn g() -> Sku { return f("abc") }')
    assert len(ir["functions"]) == 2


def test_alias_is_transparent_in_both_directions():
    # `Str` flows into a `Sku` position and back out, exactly as in TypeScript
    ir = compile_source("type Sku = Str\n"
                        "fn out(s: Sku) -> Str { return s }\n"
                        "fn into(s: Str) -> Sku { return s }")
    assert [f["returns"] for f in ir["functions"]] == ["Str", "Str"]


def test_alias_name_is_no_longer_a_case_constructor():
    # the second half of the bug: `Str` resolved as the variant's nullary case
    err = _err("type Sku = Str\nfn g() -> Sku { return Str }")
    assert "`Str` is not declared in this function" in err


def test_alias_does_not_weaken_checking():
    err = _err("type Sku = Str\nfn f(s: Sku) -> Int { return s }")
    assert "this function's return expects `Int`, got `Str`" in err


def test_alias_targets_records_variants_and_applications():
    ir = compile_source("""
type Row = { id: Int }
type Status = Active | Retired
type RowAlias = Row
type StatusAlias = Status
type Rows = List[Row]
type MaybeRow = Row?
fn a(r: RowAlias) -> Int { return r.id }
fn b(s: StatusAlias) -> Int { return match s { Active => 1, Retired => 2 } }
fn c(rs: Rows) -> Int { return rs[0].id }
fn d(m: MaybeRow) -> Opt[Row] { return m }
""")
    assert len(ir["functions"]) == 4


def test_alias_resolves_transitively_and_under_constructors():
    ir = compile_source("type Sku = Str\ntype Code = Sku\n"
                        "fn f(xs: List[Code]) -> Str { return xs[0] }")
    assert ir["functions"][0]["params"] == [{"name": "xs", "type": "List[Str]"}]


def test_alias_is_erased_from_the_ir_at_every_declaration_site():
    # transparency means the alias never reaches the type table, the IR or a
    # backend — an emitted `Sku` would name a type no tier defines
    import json
    ir = compile_source("""
type Sku = Str
type Row = { id: Int, sku: Sku }
service Catalog { fn lookup(sku: Sku) -> Opt[Row] }
fn tag(s: Sku) -> Str { return s }
component Store provides catalog: Catalog {
  config { prefix: Sku = "sku-" }
  provide catalog { fn lookup(sku: Sku) -> Opt[Row] { return None } }
}
""")
    assert "Sku" not in json.dumps(ir)
    assert ir["types"]["Row"]["fields"]["sku"] == "Str"
    assert ir["services"]["Catalog"]["methods"]["lookup"]["params"][0]["type"] == "Str"
    assert ir["functions"][0]["params"][0]["type"] == "Str"
    assert ir["components"][0]["config"][0]["type"] == "Str"


def test_alias_cycle_is_refused():
    err = _err("type Handle = Ref\ntype Ref = Handle\nfn f(h: Handle) -> Int { return 1 }")
    assert "type alias cycle: Handle -> Ref -> Handle" in err


def test_union_type_is_refused_with_a_naming_diagnostic():
    err = _err("type Row = { id: Int }\ntype Payload = List[Row] | Str\n"
               "fn f(p: Payload) -> Int { return 1 }")
    assert "revl has no union types" in err


def test_alias_cannot_carry_type_parameters():
    err = _err("type P[T] = List[T]\nfn f(p: P) -> Int { return 1 }")
    assert "type alias `P` cannot declare type parameters" in err


def test_alias_target_is_wellformed_checked_at_the_declaration():
    # not merely where the alias is used
    err = _err("type Bad = Opt\nfn f(x: Int) -> Int { return x }")
    assert "`Opt` takes 1 type argument(s), got 0" in err


# false positives: every legal spelling the alias rule must not touch

def test_genuine_enum_still_works():
    ir = compile_source("type TokenKind = Ident | Keyword | IntLit\n"
                        "fn f(t: TokenKind) -> Int {\n"
                        "  return match t { Ident => 1, Keyword => 2, IntLit => 3 } }")
    assert ir["types"]["TokenKind"]["kind"] == "variant"


def test_adt_with_payloads_and_shadowed_builtin_cases_still_works():
    # the documented idiom where user cases shadow built-in `Ok`/`Err`
    ir = compile_source("""
type Row = { id: Int }
type Outcome = Ok(Row) | NotFound | Invalid(Str)
fn f(o: Outcome) -> Int {
  return match o { Ok(r) => r.id, NotFound => 0, Invalid(s) => s.length }
}""")
    assert [c["name"] for c in ir["types"]["Outcome"]["cases"]] == \
        ["Ok", "NotFound", "Invalid"]


def test_single_case_newtype_with_a_payload_is_not_an_alias():
    ir = compile_source("type Wrapper = Wrap(Int)\n"
                        "fn f(w: Wrapper) -> Int { return match w { Wrap(n) => n } }")
    assert ir["types"]["Wrapper"]["kind"] == "variant"


def test_single_nullary_undeclared_case_stays_an_opaque_nominal():
    # `type Status = Pending` is how an opaque nominal is spelled, and is
    # exactly where TypeScript errors — so revl keeps its own meaning
    ir = compile_source("type Status = Pending\n"
                        "fn f(s: Status) -> Int { return match s { Pending => 1 } }")
    assert ir["types"]["Status"]["kind"] == "variant"


def test_alias_does_not_collide_with_implicit_type_parameters():
    # a one-letter *alias* resolves to its target; a one-letter *undeclared*
    # name in a signature stays that fn's type parameter
    err = _err('type S = Str\nfn f(x: S) -> S { return x }\n'
               'fn g() -> Int { return f("a") }')
    assert "this function's return expects `Int`, got `Str`" in err
    ir = compile_source('type S = Str\nfn f(x: S) -> S { return x }\n'
                        'fn g() -> Str { return f("a") }')
    assert ir["functions"][0]["returns"] == "Str"

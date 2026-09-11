"""Three soundness holes in the typechecker, found by an adversarial audit.

F3 — a STRUCTURAL record type was compatible with every type in BOTH
directions. `compatible` returned True for any structural-vs-nominal or
structural-vs-scalar pair, on the justification that `check_ast` would refuse a
definite mismatch at the declared boundary. It would not: `check_ast`'s
structural branch only fires when the expected type resolves to a declared
record, so an expected `Int`/`Str`/`Bool`/ADT/`List`/arrow never reached a
refusal, and component bodies never run `check_ast` at all. A record literal
was admitted as `Str`, `Bool`, an ADT, `List[Int]`, `(Int) -> Int`, `Opt[Int]`
and as an `Int` argument, and a scalar was admitted into a structural field
position. That is an unchecked cast with no cast syntax, reachable from any
unannotated expression.

F4 — stratum 3 (component / `provide` method bodies) had no CHECK position and
no `infer_ir` case for `record_update`, `match`, `arrow` or ADT construction,
so four refusals a `fn` body makes were admitted across a declared service
boundary. The item 392/404/405 parity contract says a provide-method body must
refuse what a `fn` body refuses; each parity row below asserts both strata.

F5 — the config data walk (item 378) enumerated two forbidden heads (an arrow
and a declared service) and let everything else fall off its end, so
`config { v: Any }` compiled and `config.v("payload")` typechecked as a call —
a live callable invoked past every authority fold, which is the exact hole item
378 closed for arrows. The walk is now an allowlist of data heads.

Also here: `Never` is no longer a TWO-WAY wildcard. It is the checker's
inferred bottom and uninhabited, so a value flowing INTO a `Never` position was
the same unchecked cast as F3 with a different spelling. It is one-way now: a
bottom flows out into any position, nothing flows in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.diagnostics import report  # noqa: E402


def _ok(src: str):
    return compile_source(src, "t.rvl")


def _refused(src: str) -> str:
    with pytest.raises(RevlError) as ei:
        compile_source(src, "t.rvl")
    return str(ei.value)


def _diag(src: str) -> dict:
    """The structured diagnostic, not its rendering.

    `_refused` returns `str(RevlError)`, which does NOT carry the code: the
    `T-UNRESOLVED` message and the `T1` message of the same comparison differ
    only by the `; <nominal> has no declaration in this compilation` suffix, and
    the T1 rendering is the same prefix. A message-only assertion therefore
    cannot see which code was chosen — which is exactly how a deleted `why=`
    argument survives a green suite.
    """
    with pytest.raises(RevlError) as ei:
        compile_source(src, "t.rvl")
    record = report(ei.value)
    assert record["ok"] is False
    return record["diagnostics"][0]


# ------------------------------------------------------------------ F3
#
# A structural record literal reaching a position typed as something that is
# NOT a record. Every one of these compiled clean before, and the emitted
# python then failed at runtime (`unsupported operand type(s) for +: 'dict' and
# 'int'`, `'dict' object is not callable`).

def test_f3_record_is_not_a_str():
    err = _refused("pub fn f() -> Str { return { a: 1 } }\n")
    assert "expects `Str`" in err and "{a: Int}" in err


def test_f3_record_is_not_a_bool():
    err = _refused("pub fn f() -> Bool { return { a: 1 } }\n")
    assert "expects `Bool`" in err and "{a: Int}" in err


def test_f3_record_is_not_an_adt():
    err = _refused(
        "type Color = Red | Green\n"
        "pub fn f() -> Color { return { a: 1 } }\n")
    assert "expects `Color`" in err and "{a: Int}" in err


def test_f3_record_is_not_a_list():
    err = _refused("pub fn f() -> List[Int] { return { a: 1 } }\n")
    assert "expects `List[Int]`" in err and "{a: Int}" in err


def test_f3_record_is_not_a_function():
    err = _refused("pub fn f() -> (Int) -> Int { return { a: 1 } }\n")
    assert "expects `(Int) -> Int`" in err and "{a: Int}" in err


def test_f3_record_is_not_an_opt_int():
    err = _refused("pub fn f() -> Opt[Int] { return { a: 1 } }\n")
    assert "expects `Opt[Int]`" in err and "{a: Int}" in err


def test_f3_record_is_not_an_int_argument():
    err = _refused(
        "pub fn g(n: Int) -> Int { return n + 1 }\n"
        "pub fn f() -> Int { return g({ a: 1 }) }\n")
    assert "argument 1 of `g(...)`" in err and "expects `Int`" in err


def test_f3_scalar_does_not_flow_into_a_structural_field():
    """The REVERSE direction: `{ r | a = 5 }` where `a` is `{b: Int}`. The
    admitted program then ran into `'int' object has no attribute 'b'`."""
    err = _refused("""
pub fn f() -> Int {
  let r = { a: { b: 1 } }
  let r2 = { r | a = 5 }
  return r2.a.b
}
""")
    assert "update of field `a`" in err
    assert "expects `{b: Int}`" in err and "got `Int`" in err


def test_f3_crosses_a_declared_service_boundary():
    """`fn hello(name) = { id: 1, junk: name }` provided for a `-> Str`
    method compiled clean: the structural type met the declared `Str` at the
    provider boundary and `compatible` waved it through."""
    err = _refused("""
service Hello { fn hello(name: Str) -> Str }
component C provides h: Hello {
  provide h { fn hello(name: Str) -> Str = { id: 1, junk: name } }
}
""")
    assert "`hello` returns expects `Str`" in err


# -- F3 controls: the nominal spelling was always checked, and every
#    legitimate structural-meets-nominal flow must stay admitted -------------

def test_f3_control_nominal_record_still_refused_by_name():
    err = _refused("""
type Row = { id: Int }
pub fn f(r: Row) -> Int { return r }
""")
    assert "expects `Int`, got `Row`" in err


def test_f3_control_record_literal_still_meets_its_nominal_record():
    _ok("""
type Row = { id: Int, name: Str }
pub fn f() -> Row { return { id: 1, name: "x" } }
""")
    _ok("""
type Row = { id: Int }
pub fn g(r: Row) -> Int { return r.id }
pub fn f() -> Int { return g({ id: 1 }) }
""")


def test_f3_control_record_literal_injects_into_opt_and_list():
    _ok("""
type Row = { id: Int }
pub fn f() -> Opt[Row] { return { id: 1 } }
pub fn g() -> List[Row] { return [{ id: 1 }, { id: 2 }] }
""")


def test_f3_control_structural_binding_still_reads_its_own_fields():
    _ok("""
pub fn f() -> Int {
  let r = { a: 1, b: "x" }
  return r.a
}
""")


# ------------------------------------------------------------------ F4
#
# The parity contract (items 392/404/405): a `provide` method body must refuse
# what a `fn` body refuses. Each test asserts BOTH strata, so a future
# regression in either one fails here.

_SVC = 'service S { fn go() -> Int }\n'


def test_f4_parity_record_update_names_an_unknown_field():
    stratum1 = _refused("""
pub fn f() -> Int {
  let r = { id: 1, name: "x" }
  let u = { r | nope = 1 }
  return 1
}
""")
    stratum3 = _refused(_SVC + """
component C provides s: S {
  provide s { fn go() -> Int {
    let r = { id: 1, name: "x" }
    let u = { r | nope = 1 }
    return 1
  } }
}
""")
    for err in (stratum1, stratum3):
        assert "record update names `nope`" in err


def test_f4_parity_record_update_replaces_a_field_with_the_wrong_type():
    src = """
type Row = { id: Int, name: Str }
"""
    stratum1 = _refused(src + """
pub fn f() -> Int {
  let r: Row = { id: 1, name: "x" }
  let u = { r | id = "not an int" }
  return u.id
}
""")
    stratum3 = _refused(src + _SVC + """
component C provides s: S {
  provide s { fn go() -> Int {
    let r: Row = { id: 1, name: "x" }
    let u = { r | id = "not an int" }
    return u.id
  } }
}
""")
    for err in (stratum1, stratum3):
        assert "update of field `id` expects `Int`, got `Str`" in err


def test_f4_parity_adt_construction_checks_its_payload():
    src = "type P = P(Int)\n"
    stratum1 = _refused(src + """
pub fn f() -> Int {
  let p = P("str")
  return 1
}
""")
    stratum3 = _refused(src + _SVC + """
component C provides s: S {
  provide s { fn go() -> Int {
    let p = P("str")
    return 1
  } }
}
""")
    for err in (stratum1, stratum3):
        assert "`P(...)` payload expects `Int`, got `Str`" in err


def test_f4_parity_record_literal_against_a_declared_record():
    src = "type Row = { id: Int, name: Str }\n"
    stratum1 = _refused(src + "pub fn f() -> Row { return { id: 1 } }\n")
    stratum3 = _refused(src + """
service S { fn go() -> Row }
component C provides s: S {
  provide s { fn go() -> Row { return { id: 1 } } }
}
""")
    for err in (stratum1, stratum3):
        assert "record literal for `Row` has missing `name`" in err


def test_f4_control_stratum3_checks_that_already_passed_stay_green():
    """Scalar returns, cross-component arguments and match exhaustiveness were
    already checked in stratum 3; closing the gap must not disturb them."""
    err = _refused("""
service S { fn go() -> Int }
component C provides s: S { provide s { fn go() -> Int = "nope" } }
""")
    assert "`go` returns expects `Int`, got `Str`" in err

    err = _refused("""
type Color = Red | Green
service S { fn go(c: Color) -> Int }
component C provides s: S {
  provide s { fn go(c: Color) -> Int = match c { Red => 1 } }
}
""")
    assert "non-exhaustive match" in err and "Green" in err


def test_f4_control_valid_stratum3_bodies_still_compile():
    _ok("""
type Row = { id: Int, name: Str }
type Color = Red | Green
service S { fn go(c: Color) -> Row }
component C provides s: S {
  provide s { fn go(c: Color) -> Row {
    let base = { id: 1, name: "x" }
    let bumped = { base | id = 2 }
    return match c { Red => bumped, Green => base }
  } }
}
""")


# -------------------------- an undeclared nominal is unresolved, not a mismatch
#
# The `T-UNRESOLVED` guarantee (src/revl/diagnostics.py): "a type this
# compilation does not declare is refused as unresolved, never reported as a
# mismatch it cannot check". It held for a bare nominal return, a record field
# and a `let` annotation (#844) and for nothing else. Two holes, both the same
# defect — the checker reported a disagreement it never computed:
#
#   * a nominal NESTED inside a builtin head: `-> Opt[Row]` against a bare
#     record literal (`unresolved_nominal_reason` bailed out because the HEAD
#     was `Opt`, a builtin, so it never looked inside);
#   * a nominal in ARGUMENT position: the four argument-position `mismatch(...)`
#     sites passed no `why=` at all, so they could not report the reason even
#     when the reason helper had one.
#
# The proof that neither is a real mismatch is the declared-type equivalent:
# every program below compiles CLEAN with the nominal declared. The only
# differing fact is compilation scope.
#
# The same class has three more positions, found by sweeping every declared
# boundary rather than by reading the report: an ADT case payload (both
# strata), a record update's field (stratum 3 — stratum 1 already reported it)
# and a builtin method's argument. They are fixed the same way and pinned here.
#
# These rows assert the CODE and the CATEGORY, not the message. The message of
# the `T1` fallback is the same prefix with the `; <nominal> has no declaration in
# this compilation` suffix removed, so a message-only assertion passes with the
# fix reverted — see `_diag`. Same parity contract as F4: a `provide` method
# body refuses what a `fn` body refuses, so each row asserts both strata.

_ROW_CALL = ('pub fn take(r: Row) -> Int { return r.n }\n'
             'pub fn caller() -> Int { return take({ name: "x", n: 1 }) }\n')

_ROW_CALL_S3 = ('service S { fn go() -> Int }\n'
                'fn take(r: Row) -> Int { return r.n }\n'
                'component C provides s: S {\n'
                '  provide s { fn go() -> Int { return take({ name: "x", n: 1 }) } }\n'
                '}\n')


def _assert_unresolved(src: str, message_start: str) -> dict:
    d = _diag(src)
    assert d["code"] == "T-UNRESOLVED", d
    assert d["category"] == "unresolved-type", d
    assert message_start in d["message"], d
    assert d["message"].endswith("`Row` has no declaration in this compilation"), d
    assert "refused as unresolved" in d["guarantee"]
    assert "declare the type in this compilation" in d["fix"]
    assert d["code"] != "T1" and d["category"] != "type-mismatch"
    return d


def test_f4_unresolved_nominal_nested_in_a_builtin_head():
    """`-> Opt[Row]` against a bare record literal. With `Row` declared as
    `{ name: Str, n: Int }` this is CLEAN, so `Opt` was the only thing standing
    between the literal and the declaration it satisfies."""
    stratum1 = 'pub fn f() -> Opt[Row] { return { name: "x", n: 1 } }\n'
    stratum3 = ('service S { fn go() -> Opt[Row] }\n'
                'component C provides s: S {\n'
                '  provide s { fn go() -> Opt[Row] { return { name: "x", n: 1 } } }\n'
                '}\n')
    for src in (stratum1, stratum3):
        _assert_unresolved(src, "expects `Opt[Row]`, got `{n: Int, name: Str}`")
    # the declared equivalent is the proof the comparison was never made
    _ok("pub type Known = { name: Str, n: Int }\n"
        "pub fn f() -> Opt[Known] { return { name: \"x\", n: 1 } }\n")


def test_f4_unresolved_nominal_nested_in_a_builtin_head_on_both_sides():
    """`Opt[Row]` against `Some({...})`: the same hole reached by the
    elementwise rule instead of the `T -> Opt[T]` injection."""
    src = 'pub fn f() -> Opt[Row] { return Some({ name: "x", n: 1 }) }\n'
    _assert_unresolved(src, "expects `Opt[Row]`, got `Opt[{n: Int, name: Str}]`")
    _ok("pub type Known = { name: Str, n: Int }\n"
        "pub fn f() -> Opt[Known] { return Some({ name: \"x\", n: 1 }) }\n")


def test_f4_unresolved_nominal_in_argument_position():
    """Argument position passed no `why=` at all, so the reason helper could not
    reach the diagnostic even where it had one. Both strata, and the stratum-3
    row goes through `infer_ir`'s own argument site rather than the stratum-1
    call sites."""
    for src in (_ROW_CALL, _ROW_CALL_S3):
        _assert_unresolved(src, "argument 1 of `take(...)` expects `Row`, got")
    _ok("pub type Known = { name: Str, n: Int }\n"
        + _ROW_CALL.replace("Row", "Known"))


def test_f4_unresolved_nominal_in_a_container_argument():
    """The same argument hole behind a builtin head: `List[Row]` for a
    `List[{...}]`. (The return position already reported this one.)"""
    src = ('pub fn f(xs: List[Row]) -> Int { return 0 }\n'
           'pub fn caller() -> Int { return f([{ name: "x", n: 1 }]) }\n')
    _assert_unresolved(src, "expects `List[Row]`, got `List[{n: Int, name: Str}]`")
    _ok('pub type Known = { name: Str, n: Int }\n'
        'pub fn f(xs: List[Known]) -> Int { return 0 }\n'
        'pub fn caller() -> Int { return f([{ name: "x", n: 1 }]) }\n')


def test_f4_unresolved_nominal_in_a_generic_call_argument():
    """A call to a GENERIC function has its own argument site (the signature is
    instantiated before the parameter is compared), so this is a separate
    position from the monomorphic one above and is pinned separately."""
    src = ('fn g[T](x: T, r: Row) -> Int { return r.n }\n'
           'pub fn caller() -> Int { return g(1, { name: "x", n: 1 }) }\n')
    _assert_unresolved(src, "argument 2 of `g(...)` expects `Row`, got")


def test_f4_unresolved_nominal_through_a_function_value():
    """A call through a function-typed VALUE (`call_function_value`) is a third
    argument site with its own message shape."""
    src = ('pub fn caller(h: (Row) -> Int) -> Int { return h({ name: "x", n: 1 }) }\n')
    _assert_unresolved(src, "argument 1 of `h` expects `Row`, got")


def test_f4_unresolved_nominal_behind_a_declared_generic_record():
    """`Box[Row]` where `type Box[T] = { v: T }` IS declared: the descent has to
    follow the record rule through the declared instantiation to the `Row` at
    the field, which is what `compatible` compares."""
    src = ('pub type Box[T] = { v: T }\n'
           'pub fn f() -> Box[Row] { return { v: { name: "x", n: 1 } } }\n')
    _assert_unresolved(src, "expects `Box[Row]`, got")
    _ok('pub type Known = { name: Str, n: Int }\n'
        'pub type Box[T] = { v: T }\n'
        'pub fn f() -> Box[Known] { return { v: { name: "x", n: 1 } } }\n')


def test_f4_unresolved_nominal_in_an_adt_payload():
    """A `T(Row)` case's payload is a declared boundary like any other, and its
    `mismatch(...)` site passed no `why=` — in BOTH strata. The stratum-3 row
    is the `infer_ir` payload site, which is a separate line from the stratum-1
    one, so each is pinned."""
    stratum1 = ('type P = P(Row)\n'
                'pub fn f() -> Int { let p = P({ name: "x", n: 1 }) return 1 }\n')
    stratum3 = ('type P = P(Row)\n'
                'service S { fn go() -> Int }\n'
                'component C provides s: S {\n'
                '  provide s { fn go() -> Int {\n'
                '    let p = P({ name: "x", n: 1 })\n'
                '    return 1\n'
                '  } }\n'
                '}\n')
    for src in (stratum1, stratum3):
        _assert_unresolved(src, "`P(...)` payload expects `Row`, got")
    _ok('type Known = { name: Str, n: Int }\n'
        'type P = P(Known)\n'
        'pub fn f() -> Int { let p = P({ name: "x", n: 1 }) return 1 }\n')


def test_f4_unresolved_nominal_in_a_record_update():
    """A record update's per-field check is the third declared boundary that
    passed no `why=`. Stratum 1 reported `T-UNRESOLVED` already; stratum 3
    (`infer_ir`'s own update site) reported `T1` for the identical program."""
    stratum1 = ('pub type A = { v: Row }\n'
                'pub fn f(a: A) -> A { return { a | v = { name: "x", n: 1 } } }\n')
    stratum3 = ('type A = { v: Row }\n'
                'service S { fn go() -> Int }\n'
                'component C provides s: S {\n'
                '  provide s { fn go() -> Int {\n'
                '    let a: A = { v: { name: "x", n: 1 } }\n'
                '    let b = { a | v = { name: "x", n: 1 } }\n'
                '    return 1\n'
                '  } }\n'
                '}\n')
    for src in (stratum1, stratum3):
        _assert_unresolved(src, "update of field `v`")
    _ok('pub type Known = { name: Str, n: Int }\n'
        'pub type A = { v: Known }\n'
        'pub fn f(a: A) -> A { return { a | v = { name: "x", n: 1 } } }\n')


def test_f4_unresolved_nominal_in_a_builtin_method_argument():
    """A builtin method's parameter is its receiver's element type, so
    `xs.push({...})` on a `List[Row]` is the argument hole again — this site
    passed no `why=` either."""
    stratum1 = ('pub fn f(xs: List[Row]) -> Int {\n'
                '  let ys = xs.push({ name: "x", n: 1 })\n'
                '  return 1\n'
                '}\n')
    stratum3 = ('service S { fn go(xs: List[Row]) -> Int }\n'
                'component C provides s: S {\n'
                '  provide s { fn go(xs: List[Row]) -> Int {\n'
                '    let ys = xs.push({ name: "x", n: 1 })\n'
                '    return 1\n'
                '  } }\n'
                '}\n')
    for src in (stratum1, stratum3):
        _assert_unresolved(src, "builtin `push` argument expects `Row`, got")


def test_f4_unresolved_nominal_a_scalar_meeting_it_is_still_a_mismatch():
    """The documented OPPOSITE case, and the reason the descent is not a search
    for a nominal anywhere in the expected type: a scalar states no shape, so
    `Int` against an undeclared `Row` is a comparison the checker really made.
    The record literal is what makes the report false; nothing else does."""
    for src in ('pub fn f() -> Row { return 1 }\n',
                'pub fn f() -> Opt[Row] { return 1 }\n'):
        d = _diag(src)
        assert d["code"] == "T1", d
        assert d["category"] == "type-mismatch", d
        assert "has no declaration" not in d["message"], d
    d = _diag('pub type Known = { name: Str, n: Int }\n'
              'pub fn f() -> Opt[Known] { return { name: "x" } }\n')
    assert d["code"] == "T1" and d["category"] == "type-mismatch", d


def test_f4_unresolved_nominal_the_three_positions_844_fixed_stay_unresolved():
    """The positions that already reported `T-UNRESOLVED` before this change
    (bare return, record field, `let` annotation) are regression guards for the
    `why=` argument that supplies the reason: with it deleted they fall back to
    `T1` byte-for-byte."""
    cases = [
        ('pub fn f() -> Row { return { name: "x", n: 1 } }\n',
         "this function's return expects `Row`, got"),
        ('pub type A = { r: Row }\n'
         'pub fn f() -> A { return { r: { name: "x", n: 1 } } }\n',
         "field `r` of `A` expects `Row`, got"),
        ('pub fn f() -> Int { let x: Row = { name: "x", n: 1 } return 1 }\n',
         "`let x: Row` expects `Row`, got"),
    ]
    for src, message_start in cases:
        _assert_unresolved(src, message_start)


def test_f4_control_the_t1_mismatches_around_these_positions_stay_t1():
    """Nothing else about the mismatch contract moved: a record that disagrees
    with a DECLARED type, and a nominal that is declared but whose arity is
    wrong, are still ordinary mismatches."""
    d = _diag('pub type Known = { name: Str, n: Int }\n'
              'pub fn f() -> Known { return { name: "x" } }\n')
    assert d["code"] == "T1" and d["category"] == "type-mismatch", d
    d = _diag('pub type Box[T] = { v: T }\n'
              'pub fn f() -> Box[Row] { return { w: { name: "x", n: 1 } } }\n')
    assert d["code"] == "T1" and d["category"] == "type-mismatch", d
    # the three boundaries above, with a DECLARED type on the left instead of an
    # undeclared nominal, are ordinary mismatches too
    for src in ('type P = P(Int)\n'
                'pub fn f() -> Int { let p = P("str") return 1 }\n',
                'pub type A = { v: Int }\n'
                'pub fn f(a: A) -> A { return { a | v = "str" } }\n',
                'pub fn f(xs: List[Int]) -> Int {\n'
                '  let ys = xs.push("str")\n'
                '  return 1\n'
                '}\n'):
        d = _diag(src)
        assert d["code"] == "T1" and d["category"] == "type-mismatch", d
        assert "has no declaration" not in d["message"], d


# ------------------------------------------------------------------ F5
#
# item 378 verbatim: "a config value is injected as static data, so its
# declared type must be built, transitively, out of data".

def test_f5_config_any_is_not_data():
    """The exploit: `config { v: Any }` + `config.v("payload")` compiled,
    emitted `_revl_config['v']('payload')`, and executing it with a callable
    in the config table reached the host past every authority fold."""
    err = _refused("""
service S { fn go() -> Str }
component C provides s: S {
  config { v: Any }
  provide s { fn go() -> Str = config.v("payload") }
}
""")
    assert "must be static data" in err and "`Any`" in err


@pytest.mark.parametrize("type_name", ["Any", "Value", "Mystery",
                                       "Opt[Any]", "List[Value]",
                                       "Map[Str, Any]"])
def test_f5_non_data_config_types_refused(type_name):
    err = _refused("""
service S { fn go() -> Str }
component C provides s: S {
  config { v: %s }
  provide s { fn go() -> Str = "x" }
}
""" % type_name)
    assert "must be static data" in err


def test_f5_config_never_refused():
    """`Never` is uninhabited, so no config value could ever supply one: the
    data walk refuses it with the rest of the non-data heads (item 378)."""
    err = _refused("""
service S { fn go() -> Str }
component C provides s: S {
  config { v: Never }
  provide s { fn go() -> Str = "x" }
}
""")
    assert "must be static data" in err


def test_f5_extern_config_any_refused():
    err = _refused(
        'extern pure fn thing(x: Str) -> Str\n'
        '  config { v: Any }\n'
        '  = @py { return x }\n')
    assert "must be static data" in err
    assert "extern `thing`" in err


def test_f5_control_the_six_item_378_refusals_stay_green():
    """bare arrow, arrow behind an alias, arrow in a record, arrow as an ADT
    payload, `Opt[arrow]`, and `Opt[Service]`."""
    head = "service Greet { fn greet(x: Str) -> Str }\n"
    body = '  provide g { fn greet(x: Str) -> Str = "x" }\n}\n'
    cases = [
        ("", "handler: (Str) -> Str", "arrow"),
        ("type H = (Str) -> Str\n", "handler: H", "arrow"),
        ("type Box = { h: (Str) -> Str }\n", "b: Box", "arrow"),
        ("type W = Wrap((Str) -> Str)\n", "w: W", "arrow"),
        ("", "h: Opt[(Str) -> Str]", "arrow"),
        ("service Cap { fn go() -> Str }\n", "c: Opt[Cap]", "service `Cap`"),
    ]
    for decls, field, offender in cases:
        err = _refused(head + decls
                       + "component Worker provides g: Greet {\n"
                       + "  config { %s }\n" % field + body)
        assert "must be static data" in err
        assert offender in err


def test_f5_control_data_config_untouched():
    """The legitimate feature item 378 exists to preserve: scalars, records,
    ADTs, aliases, generics and containers of data."""
    _ok("""
service S { fn go() -> Str }
type Options = { url: Str, retries: Int }
type Mode = Fast | Slow
type Row = { id: Int }
type Rows = List[Row]
type Box[T] = { v: T }
component C provides s: S {
  config {
    url: Str, retries: Int, ratio: Float, on: Bool, blob: Bytes,
    opts: Options, mode: Mode, rows: Rows, boxed: Box[Int],
    tags: List[Str], lookup: Map[Str, Int], maybe: Opt[Row]
  }
  provide s { fn go() -> Str = config.url }
}
""")


# ------------------------------------------- `Never` is a ONE-WAY bottom
#
# Same root as F3: `_is_wildcard` made `Never` compatible with everything in
# BOTH directions, so any `Never` position was an unchecked cast. A bottom is
# one-way: it flows OUT into any position (vacuously, nothing inhabits it) and
# NOTHING flows in. That closes the cast without making the checker's own
# inferred spellings (`List[Never]`, `Map[Str, Never]`) undenotable, which they
# have to stay: they are what `[]` and `Map.empty()` infer.
# `Any` and `Value` keep their DOCUMENTED laundering (the gradual frontier and
# stdlib/value.rvl's erased dynamic type); `Never` never had one.

def test_nothing_flows_into_a_never_position():
    err = _refused("pub fn nv(x: Never) -> Int { return x }\n"
                   "pub fn caller() -> Int { return nv(\"s\") }\n")
    assert "expects `Never`, got `Str`" in err


def test_nothing_flows_into_a_nested_never_position():
    """The same cast one level down: a `List[Int]` used to be admitted into a
    `List[Never]` parameter, and its elements then read out as anything."""
    err = _refused("pub fn nv(xs: List[Never]) -> Int { return 1 }\n"
                   "pub fn caller() -> Int { return nv([1, 2]) }\n")
    assert "expects `List[Never]`, got `List[Int]`" in err


def test_a_bottom_still_flows_out_into_any_position():
    """The other direction is sound and must stay: nothing inhabits `Never`, so
    a `Never`-typed expression in any position is vacuously well typed."""
    _ok("pub fn nv(x: Never) -> Int { return x }\n")
    _ok("pub fn f() -> List[Int] { return [] }\n")


def test_never_stays_available_as_the_inferred_bottom():
    """`[]` and `Map.empty()` still infer `List[Never]` / `Map[Str, Never]`,
    and a binding that carries the bottom WIDENS at its first reassignment
    instead of laundering."""
    _ok("""
pub fn f() -> Int {
  let xs = []
  let m = Map.empty()
  return xs.length() + m.size()
}
""")
    _ok("""
pub fn build(n: Int) -> Map[Str, Int] {
  var m = Map.empty()
  m = m.set("k", n)
  return m
}
""")


def test_any_and_value_keep_their_documented_laundering():
    """Deliberately unchanged: `Any` is the gradual frontier and `Value` is the
    erased dynamic type with a total accessor surface (item 180). Narrowing
    either is a language decision, not a fix to this audit."""
    _ok("pub fn launder(x: Any) -> Int { return x }\n")
    _ok("pub fn launder(x: Value) -> Int { return x }\n")

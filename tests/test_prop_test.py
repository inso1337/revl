"""`prop test` — property testing with type-derived generators (roadmap item 37).

`prop test "name" (params) { assert … }` states a property that must hold for
EVERY value of its parameters. The generators are DERIVED from the parameter
types the checker knows: the i64 edge values for `Int`, both arms of every
`Opt`, empty and non-empty `List`s, every constructor of an ADT, and each field
of a record. On failure the runner SHRINKS the offending input to a minimal
counterexample. Scope is the py reference tier (docs/prop-test.md).

Layers:

* **frontend** — `prop test` parses (additive contextual keyword), the checker
  validates every parameter type is generatable, and the lowered body + typed
  parameters land in the IR `prop_tests` section. No runtime needed.
* **generation** — pure, type-directed value builders and the coverage fold.
  Needs only the backend emitter (a prop body is a pure function), not cordis.
* **shrinking** — the greedy minimiser over the same value shapes. Pure.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl import fault as fault_mod  # noqa: E402


# ============================================================================
# frontend — parsing, validation, IR shape (no runtime)
# ============================================================================

def test_prop_test_parses_into_the_ir_with_typed_parameters():
    src = '''
    prop test "commutes" (a: Int, b: Int) {
      assert a + b == b + a
    }
    '''
    ir = compile_source(src, "t.rvl")
    assert len(ir["prop_tests"]) == 1
    unit = ir["prop_tests"][0]
    assert unit["name"] == "commutes"
    assert unit["params"] == [{"name": "a", "type": "Int"}, {"name": "b", "type": "Int"}]
    assert unit["body"]  # the lowered assert body


def test_prop_is_a_contextual_keyword_and_still_usable_as_an_identifier():
    # `prop` only heads a declaration when followed by `test`; elsewhere it is
    # an ordinary name, so adding the form breaks no existing program.
    src = '''
    fn f(prop: Int) -> Int { return prop }
    prop test "p" (x: Int) { assert f(x) == x }
    '''
    ir = compile_source(src, "t.rvl")
    assert ir["prop_tests"][0]["name"] == "p"


def test_prop_test_requires_at_least_one_assert():
    src = '''
    prop test "nothing" (x: Int) { let y = x }
    '''
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    assert "asserts nothing" in str(excinfo.value)


def test_prop_test_requires_parameters():
    src = '''
    prop test "empty" () { assert true }
    '''
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    assert "no parameters" in str(excinfo.value)


def test_an_ungeneratable_parameter_type_is_a_compile_error():
    # a bare `Map` (or any non-derivable type) cannot be generated; the checker
    # says so at compile time rather than letting the runner discover it
    src = '''
    prop test "bad" (m: Map[Str, Int]) { assert true }
    '''
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    assert "cannot be generated" in str(excinfo.value)


def test_a_self_containing_record_is_rejected():
    src = '''
    type Loop = { next: Loop }
    prop test "recur" (x: Loop) { assert true }
    '''
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    assert "contains itself" in str(excinfo.value)


def test_a_recursive_adt_with_a_base_case_is_accepted():
    # an ADT may refer to itself (it has a base case); the depth-bounded
    # generator handles it, so this compiles rather than being rejected
    src = '''
    type Tree = Leaf | Branch(Tree)
    prop test "tree" (t: Tree) { assert t == t }
    '''
    ir = compile_source(src, "t.rvl")
    assert ir["prop_tests"][0]["params"][0]["type"] == "Tree"


def test_a_prop_test_bumps_the_ir_version():
    ir = compile_source('prop test "p" (x: Int) { assert x == x }', "t.rvl")
    assert ir["ir_version"] == 3


def test_prop_units_reads_the_section():
    ir = compile_source('prop test "p" (x: Int) { assert x == x }', "t.rvl")
    units = fault_mod.prop_units(ir)
    assert len(units) == 1 and units[0]["name"] == "p"
    # a document without prop tests yields none
    assert fault_mod.prop_units({}) == []


# ============================================================================
# generation — the type-derived generators and the coverage fold (pure-ish)
# ============================================================================

def test_int_edge_values_include_the_i64_boundaries():
    edges = fault_mod._edge_values("Int", {}, None)
    assert 0 in edges and 1 in edges and -1 in edges
    assert fault_mod._I64_MAX in edges and fault_mod._I64_MIN in edges


def test_opt_edge_values_cover_both_arms():
    edges = fault_mod._edge_values("Opt[Int]", {}, None)
    assert None in edges                      # the None arm
    assert any(v is not None for v in edges)  # a Some(...) arm


def test_list_edge_values_cover_empty_and_non_empty():
    edges = fault_mod._edge_values("List[Int]", {}, None)
    assert [] in edges
    assert any(len(v) > 0 for v in edges)


def test_type_parsing_splits_nested_applications():
    assert fault_mod._parse_prop_type("List[Opt[Int]]") == ("List", ["Opt[Int]"])
    assert fault_mod._parse_prop_type("Int") == ("Int", [])


def test_coverage_fold_reports_edges_arms_lengths_and_constructors():
    types = {"Shape": {"kind": "variant",
                       "cases": [{"name": "Circle", "payload": "Int"},
                                 {"name": "Point", "payload": None}]}}
    cov = fault_mod._new_coverage()
    fault_mod._observe(0, "Int", types, cov)
    fault_mod._observe(fault_mod._I64_MAX, "Int", types, cov)
    fault_mod._observe(None, "Opt[Int]", types, cov)
    fault_mod._observe(5, "Opt[Int]", types, cov)
    fault_mod._observe([], "List[Int]", types, cov)
    fault_mod._observe([1], "List[Int]", types, cov)
    assert {0, fault_mod._I64_MAX} <= cov["int_edges"]
    assert cov["opt"] == {"None", "Some"}
    assert cov["list"] == {"empty", "nonempty"}


# ============================================================================
# shrinking — the greedy minimiser (pure over generated value shapes)
# ============================================================================

def test_int_shrink_moves_toward_zero():
    cands = list(fault_mod._shrink_value(1000, "Int", {}, None))
    assert 0 in cands
    assert all(abs(c) < 1000 for c in cands)


def test_list_shrink_drops_elements_and_shrinks_them():
    cands = list(fault_mod._shrink_value([5, 6], "List[Int]", {}, None))
    assert [] in cands                     # can empty the list
    assert [6] in cands or [5] in cands    # can drop an element


def test_opt_shrink_collapses_some_to_none():
    cands = list(fault_mod._shrink_value(7, "Opt[Int]", {}, None))
    assert None in cands


def test_shrink_args_minimises_a_failing_tuple():
    # property "a >= 0"; any negative fails. Shrinking must land on -1.
    def run_once(args):
        return (args[0] >= 0, "a >= 0")
    minimal = fault_mod._shrink_args((-987654,), ["Int"], {}, None, run_once)
    assert minimal == (-1,)


# ============================================================================
# execution — running the property on the py reference tier (emitter only)
# ============================================================================

def test_a_true_property_passes(capsys):
    src = '''
    prop test "antisymmetric" (a: Int, b: Int) {
      assert !(a < b && b < a)
    }
    '''
    ir = compile_source(src, "t.rvl")
    failures, dossier = fault_mod.run_prop_units(ir, fault_mod.prop_units(ir))
    out = capsys.readouterr().out
    assert failures == 0
    assert dossier["status"] == "passed"
    assert "PASS antisymmetric" in out


def test_a_false_property_fails_with_a_shrunk_minimal_counterexample(capsys):
    src = '''
    prop test "non-negative" (a: Int) {
      assert a >= 0
    }
    '''
    ir = compile_source(src, "t.rvl")
    failures, dossier = fault_mod.run_prop_units(ir, fault_mod.prop_units(ir))
    out = capsys.readouterr().out
    assert failures == 1
    assert dossier["status"] == "failed"
    ce = dossier["properties"][0]["counterexample"]
    # the reported counterexample is the MINIMAL one, not the first messy hit
    assert ce["args"] == "a=-1"
    assert "counterexample (shrunk): a=-1" in out


def test_generators_cover_adt_constructors_opt_arms_and_list_lengths(capsys):
    src = '''
    type Shape = Circle(Int) | Rect(Int) | Point
    prop test "coverage" (s: Shape, xs: List[Int], m: Opt[Int]) {
      assert s == s
      assert xs == xs
      assert m == m
    }
    '''
    ir = compile_source(src, "t.rvl")
    failures, dossier = fault_mod.run_prop_units(ir, fault_mod.prop_units(ir))
    out = capsys.readouterr().out
    assert failures == 0
    # every ADT constructor visited, both Opt arms, empty and non-empty lists
    assert "3/3 constructor(s) — all constructors visited" in out
    assert "arms: None, Some" in out
    assert "lengths: empty, nonempty" in out


def test_i64_edge_values_are_visited(capsys):
    src = '''
    prop test "edges" (a: Int) { assert a == a }
    '''
    ir = compile_source(src, "t.rvl")
    fault_mod.run_prop_units(ir, fault_mod.prop_units(ir))
    out = capsys.readouterr().out
    # all nine guaranteed i64 edge values are reached
    assert f"{len(fault_mod._I64_EDGES)} i64 edge value(s) visited" in out


def test_a_record_field_property_holds_over_generated_records(capsys):
    src = '''
    type Money = { cents: Int, currency: Str }
    fn amount(m: Money) -> Int { return m.cents }
    prop test "field round-trips" (m: Money) { assert amount(m) == m.cents }
    '''
    ir = compile_source(src, "t.rvl")
    failures, _ = fault_mod.run_prop_units(ir, fault_mod.prop_units(ir))
    assert failures == 0


def test_an_adt_counterexample_shrinks_to_a_minimal_case(capsys):
    src = '''
    type T = A(Int) | B(Int) | Z
    prop test "never a payload case" (t: T) { assert t == Z }
    '''
    ir = compile_source(src, "t.rvl")
    failures, dossier = fault_mod.run_prop_units(ir, fault_mod.prop_units(ir))
    assert failures == 1
    ce = dossier["properties"][0]["counterexample"]
    # minimal: a payload case with the zero payload (A(0) or B(0))
    assert ce["args"] in ("t=A(0)", "t=B(0)")


def test_the_example_program_passes(capsys):
    ir = compile_source((ROOT / "examples" / "prop_test.rvl").read_text(), "ex.rvl")
    failures, dossier = fault_mod.run_prop_units(ir, fault_mod.prop_units(ir))
    assert failures == 0
    assert dossier["counts"]["props"] == 3


def test_prop_test_surfaces_through_revl_test(capsys):
    from revl.test import test_command
    src = '''
    prop test "p" (a: Int) { assert a == a }
    '''
    ir = compile_source(src, "t.rvl")
    rc = test_command(ir, backend="py")
    out = capsys.readouterr().out
    assert rc == 0
    assert "prop test(s) held" in out


def test_the_report_states_its_py_runtime_scope_and_the_horizon(capsys):
    ir = compile_source('prop test "p" (a: Int) { assert a == a }', "t.rvl")
    fault_mod.run_prop_units(ir, fault_mod.prop_units(ir))
    out = capsys.readouterr().out
    assert "py reference tier" in out
    assert "DERIVED from the parameter types" in out
    assert "SHRUNK" in out


# ============================================================================
# dossier — gauntlet-compatible shape (no runtime; fabricated results)
# ============================================================================

def test_dossier_shape_is_gauntlet_compatible():
    unit = {"name": "p", "params": [{"name": "a", "type": "Int"}], "body": []}
    results = [(unit, "pass", {"rounds": 40, "coverage": []})]
    dossier = fault_mod._prop_dossier(results, random_rounds=64)
    assert dossier["kind"] == "tested"
    assert dossier["roadmapItem"] == 37
    assert dossier["status"] == "passed"
    assert set(dossier["counts"]) == {"props", "passed", "failed", "randomRounds"}


def test_dossier_records_the_shrunk_counterexample():
    unit = {"name": "p", "params": [{"name": "a", "type": "Int"}], "body": []}
    results = [(unit, "fail", {"rounds": 5,
                               "counterexample": {"args": "a=-1", "reason": "a >= 0",
                                                  "raw": "a=-999"}})]
    dossier = fault_mod._prop_dossier(results, random_rounds=64)
    assert dossier["status"] == "failed"
    assert dossier["properties"][0]["counterexample"]["args"] == "a=-1"


def test_prop_dossier_is_empty_passed_without_prop_tests():
    dossier = fault_mod.prop_dossier({})
    assert dossier["status"] == "passed"
    assert dossier["counts"]["props"] == 0

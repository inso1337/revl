"""The gate/reference divergence census, held at its recorded baseline.

`crates/revl-gate` embeds `selfhost/lower.rvl`'s `admit_src`, and the two
implementations disagree in places. `tests/test_gate_crate_admit.py` pins the
hand-written oracle corpus and `tools/fuzz_frontend.py --stage gate` can stumble
on a bypass; neither ENUMERATES the disagreement. This file runs
`tools/gate_reference_census.py` over the whole census corpus and fails on any
change from `tools/gate_reference_census_baseline.json`, in either direction:

  * a NEW divergence is a regression — including, and especially, a new FALSE
    REJECTION. A change that "fixes" a bypass by refusing more programs lands
    in that bucket and nowhere else, which is the control this repo needs:
    an 85-file false-rejection regression once shipped with every unit test
    green, because no test compared verdicts over the whole corpus.
  * a divergence that is GONE means a fix landed and the baseline is stale.
    Re-record it (`python3 tools/gate_reference_census.py --record`) in the same
    commit, so the shrinking allowance is visible in the diff rather than
    silently generous.
  * a `false-admit` case — the reference refuses under a guarantee this gate
    claims to decide and the gate raises no objection — is a GATE BYPASS. The
    open ones are listed by name in `KNOWN_BYPASSES` below, with the family each
    belongs to, and `test_the_open_bypass_surface_is_exactly_the_named_list`
    fails on any case that is not on that list. A count would let the list churn
    silently; a named list has to be edited, in a diff somebody reads.

This runs in the `frontend` job's plain `pytest tests/ -q`: no cargo, no wasm
toolchain, ~30 seconds. The `--engine crate` half, which asks the real crate the
same questions, needs a rust toolchain and lives in
`tests/test_gate_crate_admit.py` with the rest of the crate's differential.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def _census():
    spec = importlib.util.spec_from_file_location(
        "gate_reference_census", ROOT / "tools" / "gate_reference_census.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def census():
    return _census()


@pytest.fixture(scope="module")
def measured(census):
    """The census itself: one self-host build, one pass over the corpus."""
    reference, oracle = census._reference()
    cases = census.load_corpus(oracle)
    return cases, census.run(cases, census.SelfhostEngine(), reference)


def test_the_corpus_is_not_empty(measured):
    cases, _ = measured
    assert len(cases) > 300, \
        f"the census corpus collapsed to {len(cases)} programs; a directory " \
        f"moved out from under tools/gate_reference_census.py"


def test_no_bypass_and_no_new_divergence(census, measured):
    """The gate must not admit what the reference refuses under a guarantee it
    claims, and must not diverge anywhere the baseline does not already say."""
    _, (buckets, details) = measured
    baseline = json.loads(census.BASELINE.read_text())
    problems = census.compare(buckets, baseline)
    if problems:
        report = "\n".join(f"  {line}" for line in problems[:40])
        extra = "" if len(problems) <= 40 else \
            f"\n  ... and {len(problems) - 40} more"
        pytest.fail(
            f"the gate/reference census moved:\n{report}{extra}\n\n"
            f"A NEW entry is a regression — a new false rejection counts. A "
            f"MISSING entry means a fix landed: re-record with\n"
            f"  python3 tools/gate_reference_census.py --record\n\n"
            f"{census.report(buckets)}")
    # the details map is what makes a future regression readable; a baseline
    # that has drifted into recording nothing would pass the comparison above
    # while measuring nothing at all.
    assert details or not any(
        name.split("/", 1)[0] in census.TRACKED for name in buckets)


# The OPEN BYPASS SURFACE, named case by case.
#
# Each of these is a program the reference refuses under a guarantee this gate
# claims to decide, and the gate raises no objection to it. They are listed —
# not merely counted — so the list can be read, worked down, and never grow by
# accident: `--record` would happily write a new one into the baseline, and this
# is what stops that from passing unnoticed.
#
# Item 131 §3's effect-composition `await`/async pairing over `effect` / `emit`
# steps (lower.py `_admit_effect_async` / `_admit_emit_async`, rules 1-3) is
# CLOSED: the statement now carries its own `await` marker (`Stmt.awaited`), so
# `stmt_a1_verdict` decides the exact pairing and the suspending-teardown rule
# per statement, and the name fence (`setup_async_verdict`) prunes the awaited
# steps it must not see. The two entries that remain are each in a layer this
# gate deliberately does not run:
#
# THE TYPE LAYER (docs/design/457). The bulk of this list is the type-layer
# gap named executably at slice T0: 46 fixtures over `examples/rejections/`
# that the reference refuses in its type checker and `selfhost/lower.rvl`'s
# `admit_src` admits, because the gate runs no type layer yet. They used to sit
# in `no-objection-out-of-slice` (the classifier tagged every one "OUT:"); T0
# taught `tests/test_selfhost_lower.py::_classify` the type vocabulary, so they
# now surface here where they can be worked down. `TYPE_LAYER_GAP` in that same
# file pins the divergence per fixture; the two lists move together. Each later
# slice (T1..T4) refuses a family for real, at which point its fixtures leave
# BOTH lists and the census baseline is re-recorded.
KNOWN_BYPASSES = {
    # -- fn-body binding rules (G1/G6) --
    "examples/rejections/g1_template_undeclared.rvl",
    "examples/rejections/v2_undeclared_fn_var.rvl",
    "examples/rejections/v2_let_reassignment.rvl",
    "examples/rejections/v2_compound_assign_on_let.rvl",
    "examples/rejections/v2_duplicate_let_block_scope.rvl",
    "examples/rejections/shadowed_module_fn_call.rvl",
    "examples/rejections/g6_closure_mutates_capture.rvl",
    # -- expression typing (T1/T2) --
    "examples/rejections/t2_null_in_expression.rvl",
    "examples/rejections/t11_field_through_opt.rvl",
    "examples/rejections/t12_str_index.rvl",
    "examples/rejections/t14_optional_chain_on_nonoptional.rvl",
    "examples/rejections/t21_int32_narrow_implicit.rvl",
    "examples/rejections/t22_int32_width_mix.rvl",
    "examples/rejections/t23_int32_remainder.rvl",
    "examples/rejections/t28_bitwise_non_int32.rvl",
    "examples/rejections/t26_anon_record_update_wrong_type.rvl",
    "examples/rejections/t27_anon_record_update_undeclared_field.rvl",
    "examples/rejections/t36_float_literal_range.rvl",
    # -- calls and signatures --
    "examples/rejections/t10_call_arity.rvl",
    "examples/rejections/t15_generic_call_site.rvl",
    "examples/rejections/v2_map_set_value_mismatch.rvl",
    "examples/rejections/v2_map_value_unknown_method.rvl",
    "examples/rejections/arith_zero_divisor.rvl",
    "examples/rejections/t24_opaque_receiver_builtin.rvl",
    "examples/rejections/host_method_not_on_surface.rvl",
    "examples/rejections/g4_extern_undo_wrong_arg_type.rvl",
    # -- arrows and function values --
    "examples/rejections/t17_arrow_body_unchecked.rvl",
    "examples/rejections/t32_arrow_value_result_flows.rvl",
    "examples/rejections/t33_arrow_value_arity.rvl",
    "examples/rejections/t35_arrow_annotation_not_quantified.rvl",
    # typecheck.py's `an arrow may not declare its own async colour` — carried
    # with `code="A1"`, so the classifier already read it in-slice before T0,
    # but it is decided in the reference TYPE layer this gate does not run.
    # Flips with the arrows/function-values slice (T2c), with its siblings above.
    "examples/rejections/t34_arrow_self_declared_async.rvl",
    # -- return paths and match --
    "examples/rejections/t8_missing_return.rvl",
    "examples/rejections/t9_return_path_incomplete.rvl",
    "examples/rejections/t13_unknown_match_case.rvl",
    "examples/rejections/v2_match_nonexhaustive.rvl",
    # -- declarations --
    "examples/rejections/t18_type_alias_cycle.rvl",
    "examples/rejections/t6_bare_generic.rvl",
    "examples/rejections/t5_destructure_nonrecord.rvl",
    # -- provide-method and component bodies --
    "examples/rejections/t1_service_arg_type.rvl",
    "examples/rejections/t4_field_arg_type.rvl",
    "examples/rejections/t7_provide_param_annotation_mismatch.rvl",
    "examples/rejections/t16_provide_method_missing_return.rvl",
    "examples/rejections/t31_index_non_int_provide_method.rvl",
    "examples/rejections/t3_config_default_type.rvl",
    "examples/rejections/a6_method_not_in_service.rvl",
    "examples/rejections/g6_method_local_shadows_component.rvl",
    # -- NOT the type layer, and pre-dating this design --
    # `_check_spawn_attenuation`'s PARAMETERIZED capability-widening refusal
    # (item 294): `fs.write(path="/etc")` is not within the held
    # `fs.write(path="/tmp")` cone. The gate's capability model is token-level
    # (`fs`, `*`), so a same-token narrower/wider valuation is invisible to it.
    # Closing it needs the `cap_order` (T,P)-pair cone/ceiling algebra ported
    # into the gate, a much larger change than a `with { ... }` reader.
    "examples/rejections/g4_spawn_widens_parameter.rvl",
}


def test_the_open_bypass_surface_is_exactly_the_named_list(census, measured):
    """The bypass direction, capped by name.

    A bypass that is not on this list fails: the gate started admitting
    something the reference refuses under a guarantee it claims. A listed case
    that no longer bypasses also fails, so a fix has to delete its line here and
    say so in the diff."""
    _, (buckets, _) = measured
    found = set(census.bypasses(buckets))
    added = sorted(found - KNOWN_BYPASSES)
    fixed = sorted(KNOWN_BYPASSES - found)
    assert not added, (
        "NEW GATE BYPASS — the reference refuses these under a guarantee this "
        "gate decides and the gate raised no objection:\n  "
        + "\n  ".join(added))
    assert not fixed, (
        "these no longer bypass the gate; delete them from KNOWN_BYPASSES and "
        "re-record the census baseline:\n  " + "\n  ".join(fixed))


def test_the_frontier_mirror_matches_the_rust(census):
    """The census's fast engine reads the crate's frontier guard through a
    python mirror of `crates/revl-gate/src/frontier.rs::scan`. Its TABLES are
    imported from the generator, so only the scanning can drift — held here
    against the cases `frontier.rs`'s own unit tests state."""
    frontier = (ROOT / "crates" / "revl-gate" / "src" / "frontier.rs").read_text()
    assert "fn strip_literals" in frontier, \
        "frontier.rs no longer has the scan this mirror was written against"

    scan = census.build_frontier_scan()
    # a literal or a comment cannot trigger the scan (frontier.rs's
    # `a_literal_cannot_trigger_the_scan`)
    assert scan('fn f() -> Str { return ".is_digit" }') is None
    assert scan("// .is_digit()\nfn f(x: Int) -> Int { return x }") is None
    # an excluded builtin in member position is a gap
    # (`an_excluded_builtin_in_member_position_is_a_gap`)
    spec = importlib.util.spec_from_file_location(
        "census_generator", ROOT / "tools" / "build_gate_crate.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    tables = generator.frontier_tables()
    if tables["builtins"]:
        name = tables["builtins"][0]
        assert scan(f"fn f(x: Str) -> Bool {{ return x.{name}() }}") is not None
    # an oversized source is a gap (`an_oversized_source_is_a_gap`)
    assert scan("x" * (262145)) is not None

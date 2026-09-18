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
# gap named executably at slice T0: 43 fixtures over `examples/rejections/`
# that the reference refuses in its type checker and `selfhost/lower.rvl`'s
# `admit_src` admits, because the gate runs no type layer yet. (The
# self-declared async-colour arrow left this gap once the gate learned to parse
# an arrow's written return annotation and refuse a self-declared `Async[…]`
# colour — rule C1 — so it now agrees with the reference.) They used to sit
# in `no-objection-out-of-slice` (the classifier tagged every one "OUT:"); T0
# taught `tests/test_selfhost_lower.py::_classify` the type vocabulary, so they
# now surface here where they can be worked down. `TYPE_LAYER_GAP` in that same
# file pins the divergence per fixture; the two lists move together. Each later
# slice (T1..T4) refuses a family for real, at which point its fixtures leave
# BOTH lists and the census baseline is re-recorded.
KNOWN_BYPASSES = {
    # -- fn-body binding rules (G1/G6) --
    # The ASSIGNMENT half landed with item 391's binding-discipline slice (the
    # `let`/`var`/parameter scope walk over a module `fn` body, plus the
    # arrow-body write form): `v2_let_reassignment`,
    # `v2_compound_assign_on_let`, `v2_duplicate_let_block_scope` and
    # `g6_closure_mutates_capture` now refuse with the reference's message
    # byte-for-byte and are struck from this list, and the callable-shadowing
    # slice has since struck `shadowed_module_fn_call` the same way. What
    # remains needs machinery neither slice builds: resolving a name READ
    # against the whole callable universe, which is what both G1 rows below
    # want.
    "examples/rejections/g1_template_undeclared.rvl",
    "examples/rejections/v2_undeclared_fn_var.rvl",
    # -- expression typing (T1/T2) --
    # The fn-body STATEMENT layer (docs/design/457 T3a) closed this family for
    # the module-`fn` surface: `t2`, `t11`, `t12`, `t21`, `t22`, `t23`, `t26`,
    # `t27`, `t28`, `t29`, `t36` and `dynamic_reserved_key` now refuse with the
    # reference's own sentence and have been struck from this list. What remains
    # is the provide-method BODY, whose type environment — requirement handles,
    # activation locals, config fields, the service signature — is the component
    # slice's to build; the gate walks one over the empty environment today,
    # which decides `null` and the `Float` literal bound and stays silent about
    # every rule a name would answer for. The optional-chain rule (T2d) has
    # since landed and `t14_optional_chain_on_nonoptional` is struck from here.
    "examples/rejections/t30_field_read_on_any_provide_method.rvl",
    # -- calls and signatures --
    # CLOSED WHOLE by docs/design/457 T2b: the signature table with its marked
    # type parameters, the arity window, `unify`/`substitute` at a generic call
    # site, the host stub surface, `_BUILTIN_SIG` with its receiver families and
    # bottom learning, and the four refusals the reference makes while LOWERING
    # a method call. All nine of this family's fixtures now refuse with the
    # reference's own tag and sentence and are struck from this list.
    # -- arrows and function values --
    # CLOSED WHOLE by docs/design/457 T2c: item 75(a) §3.1/§3.2 inference (an
    # arrow always types, as a function type with every bottom rendered `Any`),
    # rule G (an arrow annotation never quantifies — it resolves against the
    # ENCLOSING `fn`'s type parameters and nothing else), and
    # `call_function_value`'s exact arity and per-argument check. All four
    # fixtures now refuse with the reference's own sentence and are struck from
    # this list.
    # -- return paths and match --
    # CLOSED WHOLE. The RETURN-PATH half landed with docs/design/457 T3b
    # (`fb_function` runs `_check_returns_on_every_path` over the statement tree
    # the fn-body walk already builds), and the MATCH half with the
    # exhaustiveness rule: the declaration scan records each ADT's own case list
    # and the lowering walk asks `_check_match_exhaustiveness`'s two questions
    # where `_lower_pure_expr` asks them. All four fixtures now refuse with the
    # reference's own tag and sentence and are struck from this list.
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
    # -- NOT the type layer, and pre-dating this design --
    # `_check_spawn_attenuation`'s PARAMETERIZED capability-widening refusal
    # (item 294): `fs.write(path="/etc")` is not within the held
    # `fs.write(path="/tmp")` cone. The gate's capability model is token-level
    # (`fs`, `*`), so a same-token narrower/wider valuation is invisible to it.
    # Closing it needs the `cap_order` (T,P)-pair cone/ceiling algebra ported
    # into the gate, a much larger change than a `with { ... }` reader.
    "examples/rejections/g4_spawn_widens_parameter.rvl",
    # Its BUDGET twin, under the same missing algebra: `net(calls=1000)` is not
    # within the held `net(calls=100)` ceiling, and a token-level model sees the
    # bare `net` on both sides.
    #
    # It is NEW HERE and not newly admitted. It was a `tag-mismatch/G4->BAD`:
    # both components spell an annotated provide method (`fn go() -> Int`), the
    # gate's `p_prov_methods` could not parse one, and the whole component
    # failed with `BAD|bad provide block in component Child` BEFORE any spawn
    # check ran. The census read that as a refusal with the wrong tag, which
    # flattered the gate — it was not deciding the guarantee at all. With the
    # parse fixed the program reaches the attenuation fold and the gate's real
    # state shows: the same token-level blindness its parameter twin above has
    # had since item 294. t29/t30 (a `pub extern` parse refusal) and t25 (a
    # type-parameter list) are the same story from earlier slices, and the
    # accepted twin `examples/budget_attenuation.rvl` left `false-reject/BAD`
    # in the same change.
    "examples/rejections/g4_spawn_widens_budget.rvl",
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


# --- the false-admission guard (docs/design/457, issue #346) -----------------
#
# The gate HAS an admission arm now (`revl_gate::issue_admission`): it upgrades a
# no-objection to an ISSUED admission where the source is inside the admission
# surface, and both census engines exercise it. So `false-admission` is a live
# release-blocking guard rather than a scaffold, and the tests below hold three
# separate things: the CLASSIFIER routes an issued admission correctly, the
# zero-tolerance handling cannot be baselined away, and the bucket is empty over
# a corpus that actually contains issued admissions.


def test_an_issued_admission_the_reference_refuses_is_a_false_admission(census):
    # in-slice refusal
    assert census.bucket(("G3", "held providers disagree"),
                         ("admitted", ("G3", "held providers disagree"))) \
        == census.ADMISSION
    # out-of-slice refusal (the type layer): still a false admission, because an
    # ISSUED admission claims the reference admits — unlike a no-objection, which
    # forgives an out-of-slice refusal since it never claimed a green.
    assert census.bucket(("OUT:type mismatch", "x is Int, not Str"),
                         ("admitted", ("", "x is Int, not Str"))) \
        == census.ADMISSION
    # a bare refusal with no code is still refused, so still a false admission
    assert census.bucket(("TYPE", "ternary branches disagree"),
                         ("admitted", ("", ""))) == census.ADMISSION


def test_an_admission_the_reference_admits_agrees(census):
    assert census.bucket(("", ""), ("admitted", ("", ""))) == "agree-admit"


def test_a_no_objection_out_of_slice_is_forgiven_but_an_admission_is_not(census):
    ref = ("OUT:type mismatch", "x is Int, not Str")
    # a no-objection to an out-of-slice refusal is the documented, tolerated
    # state; the SAME reference verdict against an issued admission is not.
    assert census.bucket(ref, ("no_objection", "")) == "no-objection-out-of-slice"
    assert census.bucket(ref, ("admitted", ("", ""))) == census.ADMISSION


def test_false_admission_is_never_baselined_and_always_fails_check(census):
    assert census.ADMISSION in census.NEVER_BASELINED
    assert census.ADMISSION in census.TRACKED
    buckets = {census.ADMISSION: ["oracle-reject:some_refused_program"]}
    # never in a baseline -> compare() reports it even against a full baseline
    # that happens to list it (a hand-edited baseline cannot buy tolerance).
    problems = census.compare(buckets, {"buckets": dict(buckets)})
    assert any("FALSE ADMISSION" in p for p in problems), problems
    assert census.false_admissions(buckets) == \
        ["oracle-reject:some_refused_program"]


def test_bypasses_does_not_swallow_the_false_admission_bucket(census):
    # `false-admission` shares `false-admit` as a string prefix; the named-list
    # bypass surface must key on the exact bucket, not startswith.
    buckets = {
        "false-admit/G3": ["a.rvl"],
        census.ADMISSION: ["b.rvl"],
    }
    assert census.bypasses(buckets) == ["a.rvl"]
    assert census.false_admissions(buckets) == ["b.rvl"]


def test_run_routes_an_admitted_gate_kind_to_false_admission(census):
    """End to end through run(): an engine that ISSUES an admission for a
    reference-refused program lands in `false-admission`, with a readable
    details entry."""
    cases = [("agree", "ok"), ("bad_inslice", "r1"), ("bad_outslice", "r2")]
    refs = {
        "ok": ("", ""),
        "r1": ("G3", "held providers disagree"),
        "r2": ("OUT:type mismatch", "x is Int, not Str"),
    }

    class _Engine:
        name = "fake"

        def verdicts(self, sources):
            table = {
                "ok": ("admitted", ("", "")),
                "r1": ("admitted", ("G3", "held providers disagree")),
                "r2": ("admitted", ("", "x is Int, not Str")),
            }
            for src in sources:
                yield table[src]

    buckets, details = census.run(cases, _Engine(), lambda s: refs[s])
    assert buckets.get("agree-admit") == ["agree"]
    assert sorted(buckets.get(census.ADMISSION, [])) == ["bad_inslice",
                                                         "bad_outslice"]
    # the details map records the issued admission's code/message like a refusal
    assert details["bad_inslice"]["gate"]["kind"] == "admitted"
    assert details["bad_inslice"]["gate"]["code"] == "G3"
    assert details["bad_inslice"]["reference"]["tag"] == "G3"


def test_no_issued_admission_is_one_the_reference_refuses(measured):
    """THE release-blocking direction, measured over the whole census corpus.

    Every admission the gate ISSUES must be one the reference admits. A single
    entry here means a host could read a rust admission as a green and run code
    the reference never admitted, which is the defect class the whole
    admission-gate arc exists to prevent. Never baselined, never allowed by
    name."""
    _, (buckets, _) = measured
    assert buckets.get("false-admission", []) == [], (
        "a gate ISSUED an admission the reference refuses — this is the "
        "release-blocking direction; see docs/design/457 / issue #346")


def test_the_admission_arm_actually_fires_over_the_corpus(census, measured):
    """The other half of the guard above, and the one that keeps it from being a
    vacuum.

    A bucket that is empty because NOTHING is ever admitted proves nothing: that
    was the state before the arm opened, and it read exactly the same. So the arm
    is measured for non-vacuity too — the engine must issue real admissions over
    the corpus, and every one of them must land in `agree-admit`.

    `ADMISSION_PROGRAMS` is why there are any: every real `.rvl` in the tree
    declares a component or an `fn` body and so sits outside the admission
    surface, which would leave the arm exercised on zero inputs."""
    cases, (buckets, _) = measured
    engine = census.SelfhostEngine()
    issued = [case_id for (case_id, _), verdict
              in zip(cases, engine.verdicts(src for _, src in cases))
              if verdict[0] == "admitted"]
    assert len(issued) >= 6, (
        "the admission arm issued nothing over the census corpus, so the "
        f"false-admission guard measured nothing: {issued}")
    agreed = set(buckets.get("agree-admit", []))
    assert set(issued) <= agreed, (
        "an issued admission did not land in `agree-admit`:\n  "
        + "\n  ".join(sorted(set(issued) - agreed)))


def test_the_admission_near_misses_are_withheld(census):
    """The near misses in `ADMISSION_PROGRAMS` sit one token outside the surface,
    and two of them are the certifier's OWN obligations: the reference refuses a
    duplicate service and a duplicate method, and the native gate raises no
    objection to either, so a certifier that skipped them would issue an
    admission the reference refuses. Held here directly, by name, rather than
    only through the bucket — a corpus edit that dropped them would otherwise
    make the guard quietly weaker."""
    certify = census.build_admission_certify()
    programs = dict(census.ADMISSION_PROGRAMS)
    near_misses = [name for name in programs if name.startswith("near_miss_")]
    assert len(near_misses) >= 8, near_misses
    for name in near_misses:
        assert not certify(programs[name]), (
            f"{name} is a NEAR MISS and must not be certified")
    inside = [name for name in programs if not name.startswith("near_miss_")]
    for name in inside:
        assert certify(programs[name]), (
            f"{name} is inside the surface and must be certified")


def test_the_admission_mirror_matches_the_rust(census):
    """The census's fast engine reads the crate's admission arm through a python
    mirror of `crates/revl-gate/src/admission.rs::certify`. Its TABLES are
    imported from the generator, so only the WALK can drift — held here against
    the cases `admission.rs`'s own unit tests state, plus the derived
    vocabularies the walk is written over."""
    admission_rs = (ROOT / "crates" / "revl-gate" / "src" / "admission.rs") \
        .read_text(encoding="utf-8")
    assert "fn certify(" in admission_rs and "fn certify_into(" in admission_rs, \
        "admission.rs no longer has the certifier this mirror was written against"

    certify = census.build_admission_certify()
    # `an_interface_only_source_is_certified`
    assert certify("service Store {\n  fn get(key: Str) -> Str\n"
                   "  fn put(key: Str, value: Str)\n}\n")
    # `the_empty_source_is_the_empty_composition_and_is_certified`
    for source in ("", "   \n", "// just a note\n"):
        assert certify(source)
    # `a_scalar_alias_is_certified_and_usable_in_a_signature`
    assert certify("type Key = Str\nservice S {\n  fn get(k: Key) -> Key\n}\n")
    # `an_alias_of_an_alias_is_not_certified`
    assert not certify("type A = Str\ntype B = A\n")
    # `a_term_of_any_kind_leaves_the_surface`
    for source in (
        "fn id(x: Int) -> Int { return x }",
        "component C provides s: S {\n  provide s {\n    fn f(x) = x\n  }\n}\n",
        "type R = { id: Int }",
        "service S {\n  fn f(x: List[Int]) -> Int\n}\n",
        'use "./other.rvl" { S }\n',
        "pub service S {\n  fn f(x: Int) -> Int\n}\n",
    ):
        assert not certify(source), source
    # `the_two_obligations_the_native_gate_does_not_carry`
    assert not certify("service A {\n  fn f(x: Int) -> Int\n}\n"
                       "service A {\n  fn g(x: Int) -> Int\n}\n")
    assert not certify("service A {\n  fn f(x: Int) -> Int\n"
                       "  fn f(y: Int) -> Int\n}\n")
    # `a_declaration_may_not_shadow_a_reference_builtin_type`
    for source in ("service Int {\n}\n", "type Opt = Str\n", "type Principal = Str\n"):
        assert not certify(source), source
    # `a_type_outside_the_scalar_vocabulary_leaves_the_surface`
    assert not certify("service S {\n  fn f(x: Unknown) -> Int\n}\n")
    assert not certify("service S {\n  fn f(x: Any) -> Int\n}\n")
    # `a_non_ascii_byte_leaves_the_surface`
    assert not certify("service \u00dcnicode {}")
    assert certify("service S {\n  fn f(x: Str) -> Str // caf\u00e9\n}\n")
    # `a_duplicate_parameter_name_leaves_the_surface`
    assert not certify("service S {\n  fn f(x: Int, x: Int) -> Int\n}\n")
    # `an_unterminated_declaration_leaves_the_surface`
    for source in ("service S {", "service S {\n  fn f(x: Int\n}", "type A =", "service"):
        assert not certify(source), source

    # and the tables the walk is written over are the generator's own, carried
    # into the rust verbatim
    spec = importlib.util.spec_from_file_location(
        "census_generator_admission", ROOT / "tools" / "build_gate_crate.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    tables = generator.admission_tables()
    assert "Int" in tables["scalars"] and "Opt" not in tables["scalars"]
    for name in tables["scalars"]:
        assert f'    "{name}",' in admission_rs
    assert generator.admission_surface_id(generator.source_digest()) in admission_rs


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
    # and so is one with too many items at a single bracket level
    # (`too_many_items_at_one_level_is_a_gap`)
    flat = "fn f() -> Int { return g(%s) }" % ", ".join(
        "1" for _ in range(generator.MAX_LEVEL_ITEMS + 1))
    assert len(flat) < generator.MAX_SOURCE_BYTES // 10
    assert scan(flat) is not None

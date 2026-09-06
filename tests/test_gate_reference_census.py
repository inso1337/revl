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
KNOWN_BYPASSES = {
    # typecheck.py's `an arrow may not declare its own async colour` — carried
    # with `code="A1"`, so the oracle classifier reads it in-slice, but it is
    # decided in the reference TYPE layer, which this gate does not run at all.
    # Closing it means porting a type-checker rule, not a statement-model fix.
    "examples/rejections/t34_arrow_self_declared_async.rvl",
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


# --- the false-admission scaffold (docs/design/457, issue #346) --------------
#
# The gate has no `Admitted` arm yet, so nothing lands in `false-admission`
# today. These pin the CLASSIFIER and the zero-tolerance handling so the bucket
# is a working guard the day the arm opens, not a line that has to be wired up
# under pressure while the dangerous direction is already live.


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


def test_the_false_admission_scaffold_is_empty_today(measured):
    """No gate arm issues an admission yet, so the bucket is empty over the whole
    census corpus. When it stops being empty, the arm has opened and the exit
    tests in tests/test_inprocess_gate_rust.py take over."""
    _, (buckets, _) = measured
    assert buckets.get("false-admission", []) == [], (
        "a gate ISSUED an admission the reference refuses — this is the "
        "release-blocking direction; see docs/design/457 / issue #346")


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

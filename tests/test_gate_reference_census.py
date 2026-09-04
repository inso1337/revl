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
# The whole list is ONE reference family, item 131 §3's effect-composition
# `await`/async pairing over `effect` / `emit` steps (lower.py
# `_admit_effect_async` / `_admit_emit_async`, rules 1-3), plus the type-layer
# arrow-colour refusal. The gate decides A1 by NAME REACH over a whole body
# (`setup_async_verdict`); these rules are per-STATEMENT and read the `await`
# marker and the `undo`/`compensate` slots separately, which the gate's
# statement model does not carry. Closing them is a statement-model change, not
# a predicate fix.
KNOWN_BYPASSES = {
    # rule 3, `compensate` reaches a suspension
    "examples/rejections/a1_async_compensate_suspends.rvl",
    # rule 1, `effect` acquires through an async op without `await`
    "examples/rejections/a1_async_effect_not_awaited.rvl",
    # rule 1, `emit` step reaches an async op without `await`
    "examples/rejections/a1_async_emit_step_not_awaited.rvl",
    # rule 2, `await emit` on an emission that reaches nothing async
    "examples/rejections/a1_await_emit_sync.rvl",
    # rule 2, `effect await` on an acquisition that reaches nothing async
    "examples/rejections/a1_effect_await_sync.rvl",
    # typecheck.py's `an arrow may not declare its own async colour` — carried
    # with `code="A1"`, so the oracle classifier reads it in-slice, but it is
    # decided in the reference TYPE layer, which this gate does not run at all
    "examples/rejections/t34_arrow_self_declared_async.rvl",
    # `_check_spawn_attenuation`'s capability-widening refusal over a spawn's
    # `with { ... }` config block, which the gate skips structurally
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

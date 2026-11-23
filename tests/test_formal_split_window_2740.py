"""The SK row of the formal differential harness, and the flush reading of the
O row: the crash window of a witnessed mutation and the roll-forward
`flush-residue` window (issue #2740).

`RevL.Lemmas.SemStep.witnessed` appends the descriptor and creates the referent
in one step. The runtime does not: the emitted call site runs the forward
extern and registers the inverse only on its `Ok` branch. `RevL.Lemmas.SplitStep`
splits the step into a mutation and a record, so a crash between them can be
written down: the referent is in the world and no record names it. The SK row
compares the model's cut of every split body (`splitRun`) with what the real
WAL reader and `revl.recovery.recover` do over the same cut, performed in the
order the runtime performs it.

The O row grew two columns, `flushed` and `owed`: a roll-forward's reading of
the deferral queue (`RevL.Lemmas.deferralView`), against `recover`'s own
`deferrals` / `owedFlushes`.

`make formal` diffs the two sides and needs a Lean toolchain; this module runs
in the plain `pytest tests/` job and pins the Python half: the order the
runtime logs in, the reference's own observations, and the mutant that shows
the row tells the two orders apart.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from _load_by_path import load_by_path  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

A8 = ROOT / "formal" / "RevL" / "Theorems" / "A8_WalDischarge.lean"
WAL = ROOT / "formal" / "RevL" / "Lemmas" / "WalLemmas.lean"
ORACLE = ROOT / "formal" / "harness" / "Oracle.lean"
GATE = ROOT / "formal" / "scripts" / "run_gate.sh"
CHECK = ROOT / "formal" / "CheckAxioms.lean"
NONVAC = ROOT / "formal" / "scripts" / "nonvacuity.tsv"
STATUS = ROOT / "formal" / "STATUS.md"


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_split_window",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def observed(harness):
    harness._SPLIT_MARKS.clear()
    return {name: harness.split_observation(name, codes, cut)
            for name, codes, cut in harness._split_scenarios()}


# ---------------------------------------------- the order the runtime logs in

_SOURCE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash() -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    return Ok({'path': 'p', 'bak': 'b'})\n"
    "}\n"
    "component StashOk {\n"
    "  effect stash()\n"
    "}\n"
)


def _emit_backend():
    spec = importlib.util.spec_from_file_location(
        "py_emit_split_window", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_emitted_call_site_runs_the_extern_before_it_registers_the_inverse():
    """The anchor of the split step. The forward extern is the line that
    mutates; the registration is the `yield` that writes the descriptor
    (`Frame.transactional`). The registration sits on the `Ok` branch of the
    call, after it, which is the window `SplitStep.mutate` and
    `SplitStep.record` are the two halves of."""
    body = _emit_backend().emit(compile_source(_SOURCE, "split_window.rvl"))
    lines = body.splitlines()
    call = next(i for i, line in enumerate(lines)
                if line.strip().startswith("_revl_wit") and "= stash(" in line)
    ok = next(i for i, line in enumerate(lines)
              if "isinstance(_revl_wit" in line and "Ok)" in line)
    register = next(i for i, line in enumerate(lines)
                    if "yield _revl_frame.transactional(" in line)
    assert call < ok < register


# -------------------------------------- what the reference does at each cut

def test_the_corpus_is_every_cut_of_every_body(harness):
    scenarios = harness._split_scenarios()
    names = [name for name, _codes, _cut in scenarios]
    assert len(names) == len(set(names))
    for codes in (("W",), ("U", "E"), ("W", "W", "W")):
        actions = harness._split_actions(codes)
        cuts = {cut for _n, c, cut in scenarios if c == codes}
        assert cuts == set(range(len(actions) + 1))
    assert len(harness.split_scenario_rows()) == len(scenarios)


def test_a_witnessed_step_is_two_actions_and_an_emission_is_one(harness):
    assert harness._split_actions(("W",)) == [
        ("mutate", 1), ("record", ("descriptor", "transactional", True), 1)]
    assert harness._split_actions(("U",))[1][1] == (
        "descriptor", "transactional", False)
    assert harness._split_actions(("E",)) == [("emit", 1)]


def test_a_crash_inside_the_window_leaves_the_referent_in_the_world(observed):
    """`RevL.A8.crash_window_witness`, observed on the reference. Body `W W`
    cut after the second extern returned: seq 1 is mutated and recorded, seq 2
    is mutated and not recorded. Recover applies seq 1 and reports nothing,
    and seq 2 is still in the world."""
    assert observed["a8split/WW/3"] == ("rolledBack", (1,), (), (2,))
    # one step later the record exists and recovery restores the world
    assert observed["a8split/WW/4"] == ("rolledBack", (1, 2), (), ())
    # the cut before the first record: the mutation is in the world and nothing
    # is in the log, so recovery has nothing to apply
    assert observed["a8split/WW/1"] == ("rolledBack", (), (), (1,))


def test_an_emission_beside_the_window_stays_reported_and_in_the_world(observed):
    """The emission is residue and stays in the world; the orphan is in the
    world and not in the residue. The two differ exactly at the window."""
    assert observed["a8split/EW/2"] == ("rolledBack", (), (1,), (1, 2))
    assert observed["a8split/EW/3"] == ("rolledBack", (2,), (1,), (1,))


def test_the_undeclared_inverse_window_is_the_same(observed):
    assert observed["a8split/U/1"] == ("rolledBack", (), (), (1,))
    assert observed["a8split/U/2"] == ("rolledBack", (1,), (), ())


def test_the_coverage_ratchet_has_its_witnesses(harness, observed):
    assert harness.split_coverage(observed) == []


def test_the_coverage_ratchet_bites(harness, observed, monkeypatch):
    monkeypatch.setattr(harness, "_SPLIT_MARKS", {})
    assert len(harness.split_coverage(observed)) == 7


# ----------------------------------------------- the mutant the row rejects

def test_a_runtime_that_logged_first_is_told_apart_from_the_real_order(harness):
    """The mutant order. If the descriptor were written AHEAD of the forward
    extern, the window would hold a record with no referent instead of a
    referent with no record. The reference observation tells the two orders
    apart at the cut the model pins, so a reference performed in the mutant
    order disagrees with the model's `SplitStep` (the run of `make formal` with
    `REVL_FORMAL_SPLIT_ORDER=record-first` shows it end to end). This test does
    not run the runtime: a runtime that swapped the order is caught by the
    emitted-source anchor above."""
    real = harness.split_observation("a8split/WW/3", ("W", "W"), 3)
    mutant = harness.split_observation("a8split/WW/3", ("W", "W"), 3,
                                       order="record-first")
    assert real == ("rolledBack", (1,), (), (2,))
    assert mutant != real
    assert mutant == ("rolledBack", (1, 2), (), ())


def test_every_cut_inside_a_window_separates_the_orders(harness, observed):
    snapshot = dict(harness._SPLIT_MARKS)
    separated = 0
    for name, codes, cut in harness._split_scenarios():
        if not any(c in "WU" for c in codes):
            continue
        mutant = harness.split_observation(name, codes, cut,
                                           order="record-first")
        if mutant != observed[name]:
            separated += 1
    assert separated > 0
    # a probe in the other order must not clobber the configured sweep's marks
    assert harness._SPLIT_MARKS == snapshot
    assert harness.split_coverage(observed) == []


def test_the_default_order_is_the_runtime_order(harness):
    assert harness._SPLIT_ORDER == "runtime"
    assert harness._split_actions(("W",)) == harness._split_actions(
        ("W",), "runtime")


def test_the_oracle_refuses_a_step_it_has_no_form_for():
    oracle = ORACLE.read_text(encoding="utf-8")
    assert "names a step the model has no form for" in oracle
    assert "malformed SK row" in oracle


# ------------------------------------------- the flush reading of the O row

@pytest.fixture(scope="module")
def recovered(harness):
    harness._RECOVERY_MARKS.clear()
    return {name: harness.recovery_observation(name, records, failing)
            for name, records, failing in harness._wal_logs()}


def test_a_flush_that_raised_is_owed_and_one_that_returned_is_flushed(recovered):
    # (outcome, applied, residue, flushed, owed)
    assert recovered["a8r4/dq/d0/f0/r/both"] == (
        "rolledForward", [], None, [], [1])
    assert recovered["a8r4/dq/d0/f0/f/both"] == (
        "rolledForward", [], None, [1], [])
    assert recovered["a8r4/dq/d0/f0/r/approved"] == (
        "rolledForward", [], None, [], [1])
    assert recovered["a8r4/dq/d0/f0/f/approved"] == (
        "rolledForward", [], None, [1], [])


def test_an_approved_emission_with_no_flush_record_is_owed_not_attempted(recovered):
    assert recovered["a8r4/dq/d0/f0/n/both"][3:] == ([], [1])
    assert recovered["a8r4/dq/d0/f0/n/approved"][3:] == ([], [1])


def test_an_unapproved_queue_is_dropped_never_owed(recovered):
    assert recovered["a8r4/dq/d0/f0/n/complete"][3:] == ([], [])
    assert recovered["a8r4/dq/d0/f0/n/none"][3] is None   # a roll-back


def test_a_raise_does_not_hide_the_entry_behind_it(recovered):
    """Two queued emissions: the first raised, the second returned."""
    assert recovered["a8r4/dqdq/d0/f0/rf/both"][3:] == ([2], [1])
    assert recovered["a8r4/dqdq/d0/f0/fr/both"][3:] == ([1], [2])


def test_the_flush_columns_are_n_a_outside_a_roll_forward(recovered):
    for name, row in recovered.items():
        if row[0] != "rolledForward":
            assert row[3] is None and row[4] is None, name
        else:
            assert row[3] is not None and row[4] is not None, name


def test_the_recovery_ratchet_names_the_flush_behaviours(harness, recovered):
    assert harness.recovery_coverage(recovered) == []
    marks = set().union(*harness._RECOVERY_MARKS.values())
    assert {"flush-confirmed", "flush-raised", "flush-unattempted",
            "flush-dropped"} <= marks


def test_the_oracle_row_carries_the_two_columns(harness):
    row = ("O\ta8r4/dq/d0/f0/r/both\toutcome=rolledForward\treplayed=\t"
           "residue=n/a\tflushed=\towed=1\n")
    parsed = harness.parse_verdicts(row).recoveries["a8r4/dq/d0/f0/r/both"]
    assert parsed == ("rolledForward", (), None, (), (1,))
    sk = ("SK\ta8split/WW/3\toutcome=rolledBack\treplayed=1\tresidue=\t"
          "remaining=2\n")
    assert harness.parse_verdicts(sk).splits["a8split/WW/3"] == (
        "rolledBack", (1,), (), (2,))


# ------------------------------------------------- the model and its gates

THEOREMS = (
    "commit_flush_is_discharged_or_owed", "flush_residue_run",
    "flush_crash_cut_witness", "flush_hypotheses_are_inhabited",
    "split_complete_is_sem", "crash_window_recovery",
    "orphan_is_invisible_to_recovery", "window_breaks_exact_residue",
    "crash_window_witness", "window_hypotheses_are_inhabited",
    "window_log_is_the_log_before_the_mutation", "mutate_witness",
)


def test_the_model_states_both_windows_and_every_gate_registers_them():
    lean = A8.read_text(encoding="utf-8")
    for name in THEOREMS:
        assert f"theorem {name}" in lean, name
        assert f"RevL.A8.{name}" in CHECK.read_text(encoding="utf-8"), name
        assert f"RevL.A8.{name}" in GATE.read_text(encoding="utf-8"), name
        assert f"RevL.A8.{name}" in NONVAC.read_text(encoding="utf-8"), name
        assert f"`RevL.A8.{name}`" in STATUS.read_text(encoding="utf-8"), name
    assert "sorry" not in lean
    wal = WAL.read_text(encoding="utf-8")
    assert "sorry" not in wal
    for name in ("splitSteps_collapse", "semSteps_has_split",
                 "splitSteps_trace", "splitRun_steps",
                 "deferClass_after_flush"):
        assert f"theorem {name}" in wal, name


def test_the_oracle_bridges_are_registered():
    oracle = ORACLE.read_text(encoding="utf-8")
    for name in ("mem_owedOf_iff", "owedSeqLabels_nil_of_unapproved",
                 "mem_flushedOf_iff", "skCut_is_a_run",
                 "pending_survives_recovery"):
        assert f"theorem {name}" in oracle, name
        assert f"RevLOracle.{name}" in oracle
        assert f"RevLOracle.{name}" in GATE.read_text(encoding="utf-8"), name

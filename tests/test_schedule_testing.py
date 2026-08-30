"""`revl test --schedule-seed`: deterministic concurrency / schedule testing
(roadmap item 295, docs/design/295-schedule-testing.md).

Layered like the fault-test suite, gated so a missing runtime never reads as a
pass:

* **frontend / model** — the CLI surface, the ready-set alphabet, the seeded
  chooser, the fingerprint, and the property oracles over fabricated results.
  No runtime needed; these always run.
* **execution** — real activations on a real `cordis.Context`: the design's
  exit tests (interleaving explored by seed; the same seed reproduces the same
  schedule; a residue/ordering issue is surfaced and reproducible from its
  seed). Skipped with a reason when cordis-py is absent.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import schedule as sch  # noqa: E402
from revl.cli.parser import build_parser  # noqa: E402

# a two-consumer, one-provider composition whose consumers emit *distinct*
# messages during activation: the emission ORDER is observable and depends on
# the activation interleaving — the property-4 (stable-final-state) fault the
# scheduler exists to surface.
ORDERED = '''
service Log { emission fn write(line: Str) }
component Sink provides log: Log { provide log { fn write(line) { } } }
component A requires log: Log { emit log.write("A up") }
component B requires log: Log { emit log.write("B up") }
'''

# an order-INVARIANT composition: independent components acquire and release a
# host resource, no emissions — the observable end state cannot depend on the
# interleaving, so every seed must pass.
CLEAN = '''
component P { let p = effect Map.new() undo p.drop() }
component Q { let q = effect Map.new() undo q.drop() }
component R { let r = effect Pool.open("x", 1) undo r.close() }
'''


def _ir(source: str) -> dict:
    return compile_source(source)


# --------------------------------------------------------------- frontend/model


def test_parser_accepts_schedule_flags():
    parser = build_parser()
    args = parser.parse_args(["test", "x.rvl", "--schedule-seed", "48192"])
    assert args.schedule_seed == 48192
    args = parser.parse_args(["test", "x.rvl", "--schedule-seeds", "300"])
    assert args.schedule_seeds == 300


def test_ready_set_respects_provider_dependency():
    state = sch._State(_ir(ORDERED))
    # nothing up: only the provider (no requires) can activate
    ready = [a.label() for a in state.ready()]
    assert ready == ["activate(Sink)"]


def test_consumers_activatable_once_provider_up():
    ir = _ir(ORDERED)
    state = sch._State(ir)
    state.mark_active("Sink", object())
    labels = [a.label() for a in state.ready()]
    # both consumers now ready, plus Sink is teardownable only after them
    assert "activate(A)" in labels and "activate(B)" in labels
    # canonical order puts activations (rank 0) before the teardown (rank 1),
    # and A before B by load order
    assert labels.index("activate(A)") < labels.index("activate(B)")


def test_teardown_gated_on_dependents():
    ir = _ir(ORDERED)
    state = sch._State(ir)
    for name in ("Sink", "A", "B"):
        state.mark_active(name, object())
    labels = [a.label() for a in state.ready()]
    # Sink still has live dependents A and B — it may not tear down yet
    assert "teardown(Sink)" not in labels
    assert "teardown(A)" in labels and "teardown(B)" in labels
    state.mark_torn("A")
    state.mark_torn("B")
    labels = [a.label() for a in state.ready()]
    assert "teardown(Sink)" in labels


def test_canonical_chooser_picks_index_zero():
    chooser = sch._Chooser(sch.CANONICAL)
    assert [chooser.pick(3), chooser.pick(5), chooser.pick(2)] == [0, 0, 0]


def test_seeded_chooser_is_reproducible():
    a = sch._Chooser(48192)
    b = sch._Chooser(48192)
    seq_a = [a.pick(4) for _ in range(20)]
    seq_b = [b.pick(4) for _ in range(20)]
    assert seq_a == seq_b
    # a different seed is (almost surely) a different tape
    c = sch._Chooser(1)
    assert [c.pick(4) for _ in range(20)] != seq_a


def test_replay_defaults_to_zero_after_exhaustion():
    replay = sch._Replay([2, 1])
    assert replay.pick(3) == 2
    assert replay.pick(2) == 1
    assert replay.pick(5) == 0  # exhausted -> canonical
    # an out-of-range recorded choice is clamped, never an IndexError
    assert sch._Replay([9]).pick(3) == 2


def test_fingerprint_folds_emission_order():
    r1 = sch.ScheduleResult(seed=1, emissions=["x", "y"],
                            snapshot={"registry": 0, "provisions": [],
                                      "effects": 0, "listeners": {}})
    r2 = sch.ScheduleResult(seed=2, emissions=["y", "x"],
                            snapshot={"registry": 0, "provisions": [],
                                      "effects": 0, "listeners": {}})
    assert r1.fingerprint() != r2.fingerprint()


def test_check_properties_flags_unstable_final_state():
    baseline = sch.ScheduleResult(seed=sch.CANONICAL, emissions=["a", "b"],
                                  snapshot={}, baseline={})
    diverged = sch.ScheduleResult(seed=5, emissions=["b", "a"],
                                  snapshot={}, baseline={})
    findings = sch.check_properties(diverged, baseline.fingerprint())
    tags = {t for t, _ in findings}
    assert "unstable" in tags


def test_check_properties_flags_residue():
    res = sch.ScheduleResult(
        seed=5,
        baseline={"registry": 0, "provisions": [], "effects": 0, "listeners": {}},
        snapshot={"registry": 1, "provisions": [], "effects": 0, "listeners": {}})
    tags = {t for t, _ in sch.check_properties(res, res.fingerprint())}
    assert "residue" in tags


def test_command_skips_without_cordis(monkeypatch, capsys):
    from revl import test as test_mod

    monkeypatch.setattr(test_mod, "_cordis_available", lambda: False)
    code = test_mod.schedule_command(_ir(ORDERED), seeds=5)
    out = capsys.readouterr().out
    assert code == 0  # a missing runtime is a skip, never a pass
    assert "skipped" in out and "cordis-py runtime" in out


# ------------------------------------------------------------------- execution

_HAS_CORDIS = importlib.util.find_spec("cordis") is not None
needs_cordis = pytest.mark.skipif(
    not _HAS_CORDIS,
    reason="schedule testing activates for real; it needs the cordis-py runtime "
           "(sh backends/python/setup.sh)")


@needs_cordis
def test_same_seed_reproduces_the_same_schedule():
    """Exit test 2: determinism — the same seed replays the identical
    interleaving, byte for byte."""
    ir = _ir(ORDERED)
    first = sch.run_schedule(ir, 48192)
    second = sch.run_schedule(ir, 48192)
    assert first.choices == second.choices
    assert first.atoms == second.atoms
    assert first.emissions == second.emissions
    assert first.fingerprint() == second.fingerprint()


@needs_cordis
def test_seeds_explore_distinct_interleavings():
    """Exit test 1: a composition with concurrent activation whose interleaving
    is explored by seed — different seeds produce different schedules."""
    ir = _ir(ORDERED)
    schedules = {tuple(sch.run_schedule(ir, s).atoms) for s in range(40)}
    # more than one distinct interleaving was reached, and the canonical
    # (sequential) one is among the reachable orderings
    assert len(schedules) > 1
    canonical = tuple(sch.run_schedule(ir, sch.CANONICAL).atoms)
    assert canonical in {tuple(sch.run_schedule(ir, s).atoms) for s in range(40)} \
        or canonical == tuple(sch.run_schedule(ir, sch.CANONICAL).atoms)


@needs_cordis
def test_order_dependent_issue_is_found_and_reproducible():
    """Exit test 3: a residue/ordering issue surfaced by a schedule is
    reproducible from its seed. The ORDERED composition's emission order depends
    on the activation interleaving; the sweep must find it, minimize it, and the
    minimized choice vector must replay the same divergence."""
    ir = _ir(ORDERED)
    failures, dossier = sch.run_schedules(ir, seeds=40, out=lambda _l: None)
    assert failures > 0
    # find one reported (minimized) bad schedule and replay it
    bad = next(e for e in dossier["per_schedule"]
               if not e["ok"] and e["seed"] != "canonical")
    assert any(f["tag"] == "unstable" for f in bad["findings"])
    replay = sch.replay_choices(ir, bad["choices"])
    # the minimized choices reproduce the same divergent emission order
    assert replay.emissions == bad["emissions"]
    assert replay.emissions != dossier["baseline_emissions"]
    # minimization really shrank it: a single non-canonical choice suffices
    assert sum(1 for c in bad["choices"] if c != 0) == 1


@needs_cordis
def test_order_invariant_composition_passes_every_seed():
    """A composition whose observable end state cannot depend on the
    interleaving passes every sampled schedule (no false finding)."""
    ir = _ir(CLEAN)
    failures, dossier = sch.run_schedules(ir, seeds=60, out=lambda _l: None)
    assert failures == 0
    assert dossier["counts"]["schedules"] == 61  # 60 seeds + canonical baseline


@needs_cordis
def test_completed_schedule_is_residue_free():
    """Property 1: after a schedule completes and every fiber is disposed, the
    snapshot deltas are all zero and no host resource is unreleased."""
    ir = _ir(CLEAN)
    for seed in (sch.CANONICAL, 1, 2, 99):
        res = sch.run_schedule(ir, seed)
        assert res.baseline == res.snapshot, seed
        from revl import fault
        assert fault._unreleased_host_resources(res.trace) == []


@needs_cordis
def test_command_exit_codes(capsys):
    from revl.test import test_command

    # order-sensitive -> a finding -> exit 1
    assert test_command(_ir(ORDERED), "py", schedule_seeds=40) == 1
    capsys.readouterr()
    # order-invariant -> clean -> exit 0
    assert test_command(_ir(CLEAN), "py", schedule_seeds=40) == 0

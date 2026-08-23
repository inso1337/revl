"""The kit's own tests: they prove the *scoring discipline* with synthetic
adapters (no runtime needed), and — when the cordis-py backend is importable —
smoke-test the worked py adapter end to end.

The discipline under test is the whole point of a TCK: pending never reads as
pass, a pinned divergence is recorded rather than failed, and a pinned
divergence that starts meeting the ideal *fails* so the baseline can only move
deliberately.

Run:  backends/python/.venv/bin/python -m pytest tck/tests/test_tck.py -q
(the discipline tests need no runtime and pass under any interpreter with
pytest; the py smoke test skips if cordis is not importable.)
"""

from __future__ import annotations

import pytest

from tck.adapter import Observation, RuntimeAdapter
from tck.runner import Outcome, run_suite
from tck.spec import CATALOG, Kind, runtime_cases


class _ScriptedAdapter(RuntimeAdapter):
    """Returns a canned Observation per case id; unknown ids -> unsupported."""

    def __init__(self, name: str, scripted: dict[str, Observation]) -> None:
        self.name = name
        self.runtime_version = "test"
        self._scripted = scripted

    def supports(self, case_id: str) -> bool:
        return case_id in self._scripted

    def run(self, case_id: str) -> Observation:
        return self._scripted[case_id]


def _result(report, case_id):
    return next(r for r in report.results if r.case.id == case_id)


# -- ideal / divergent observations for the live pinned case (cordis-rs A1) -
# a8_async_body_failure was cordis-py's pinned divergence until
# harden-fiber-lifecycle 1316174 fixed it; the discipline tests below now use
# the one remaining live pin, cordis-rs's A1 divert-at-boundary.

def _a1_ideal_obs() -> Observation:
    return Observation(trace=["pool.query SELECT pg_advisory_unlock(42)"],
                       states={"migrator": "PENDING"})


def _a1_rs_divergent_obs() -> Observation:  # cordis-rs's behavior
    return Observation(
        trace=["pool.execute INSERT INTO migration_log VALUES (42)",
               "pool.query SELECT pg_advisory_unlock(42)"],
        states={"migrator": "FAILED"})


# ---------------------------------------------------------------------------
# pending is never pass
# ---------------------------------------------------------------------------

def test_compile_time_requirements_are_pending_not_pass():
    adapter = _ScriptedAdapter("any", {})
    report = run_suite(adapter)
    for case in CATALOG:
        if case.kind == Kind.COMPILE_TIME:
            assert _result(report, case.id).outcome is Outcome.PENDING


def test_unsupported_case_is_pending_not_pass():
    adapter = _ScriptedAdapter("sparse", {})  # supports nothing
    report = run_suite(adapter)
    for case in runtime_cases():
        assert _result(report, case.id).outcome is Outcome.PENDING
    assert report.ok  # pending does not sink the run


# ---------------------------------------------------------------------------
# a pinned divergence: recorded for the pinned runtime, fails for others
# ---------------------------------------------------------------------------

def test_pinned_runtime_records_divergence():
    adapter = _ScriptedAdapter("cordis-rs",
                               {"a1_divert_at_boundary": _a1_rs_divergent_obs()})
    report = run_suite(adapter)
    r = _result(report, "a1_divert_at_boundary")
    assert r.outcome is Outcome.DIVERGENCE
    assert "cordis-rs" in r.divergence.runtimes


def test_pin_that_starts_meeting_the_ideal_fails():
    # cordis-rs suddenly diverting-at-boundary (the ideal) means the pin is
    # stale; the kit must FAIL until someone re-baselines it deliberately.
    adapter = _ScriptedAdapter("cordis-rs",
                               {"a1_divert_at_boundary": _a1_ideal_obs()})
    report = run_suite(adapter)
    r = _result(report, "a1_divert_at_boundary")
    assert r.outcome is Outcome.FAIL
    assert "stale" in r.detail
    assert not report.ok


def test_unpinned_runtime_meeting_the_ideal_passes_a_pinned_case():
    # a runtime that does NOT share cordis-rs's A1 divergence passes on the ideal
    # (this is also what the reference tier does — the pin is scoped to cordis-rs).
    adapter = _ScriptedAdapter("cordis-zig",
                               {"a1_divert_at_boundary": _a1_ideal_obs()})
    r = _result(run_suite(adapter), "a1_divert_at_boundary")
    assert r.outcome is Outcome.PASS


def test_unpinned_runtime_exhibiting_a_foreign_divergence_fails():
    # cordis-zig behaving like cordis-rs's known divergence is a finding, not a pass.
    adapter = _ScriptedAdapter("cordis-zig",
                               {"a1_divert_at_boundary": _a1_rs_divergent_obs()})
    r = _result(run_suite(adapter), "a1_divert_at_boundary")
    assert r.outcome is Outcome.FAIL
    assert "unexpected divergence" in r.detail


# ---------------------------------------------------------------------------
# worked example: the real cordis-py runtime, if importable
# ---------------------------------------------------------------------------

def _py_adapter_or_skip():
    try:
        from tck.adapters.py_adapter import build
        return build()
    except Exception as exc:  # cordis / backend not installed in this interpreter
        pytest.skip(f"cordis-py backend not importable: {exc!r}")


def test_py_reference_run_is_ok_and_a8_async_now_conforms():
    adapter = _py_adapter_or_skip()
    report = run_suite(adapter)
    assert report.ok, [(_r.case.id, _r.detail) for _r in report.results
                       if _r.outcome is Outcome.FAIL]
    # R1-R5, A1, A5, A8(sync AND async), G7 are green on the reference tier.
    # a8_async_body_failure joined this list once harden-fiber-lifecycle 1316174
    # landed the async body FAILED like the sync path (its pin is retired).
    for cid in ("r1_lifo_recovery", "r2_reactive_resolution", "r3_withdrawal_ordering",
                "r4_no_residue", "r5_derived_withdrawal", "a1_divert_at_boundary",
                "a5_compensate_lifo", "a8_sync_failure_contained",
                "a8_async_body_failure", "g7_lifo_complete_teardown"):
        assert _result(report, cid).outcome is Outcome.PASS, cid

"""Capability-aware runtime metrics (roadmap item 122, docs/revl-metrics.md).

The metric computation is pure over the :mod:`revl.why_runtime` trace
vocabulary, so it is pinned here on hand-built traces — no runtime needed. Each
test constructs a synthetic **v2** trace with the events built by
`why_runtime.make_event` / `make_emit_event` (the same builders the run driver
uses), so the fixtures cannot drift from the real record shape.

The three metrics, and their honest degrade on a v1 trace (no `ts`), are each
pinned against a trace whose expected numbers are computed by hand.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import metrics as m  # noqa: E402
from revl import why_runtime as wr  # noqa: E402


def _v2_trace() -> list[dict]:
    """A composition run exercising every metric at once:

    * two `emit` crossings on two different capabilities (one capability twice);
    * two FAILED withdraws — one classified `A8`, one with no code (a bare crash);
    * two completed load->withdraw lifecycles with known ts deltas (2.5s, 1.5s).
    """
    events: list[dict] = []
    seq = 0

    def nxt() -> int:
        nonlocal seq
        cur = seq
        seq += 1
        return cur

    # -- two completed lifecycles (component + gen pairing) -----------------
    # Ledger gen 1: load @100.0, withdraw @102.5  -> 2.5s
    events.append(wr.make_event(nxt(), 1, wr.LOAD, "Ledger",
                                "PENDING -> ACTIVE", wr.cause_boot(), ts=100.0))
    # UserCache gen 1: load @200.0, withdraw @201.5 -> 1.5s
    events.append(wr.make_event(nxt(), 1, wr.LOAD, "UserCache",
                                "PENDING -> ACTIVE",
                                wr.cause_requirements(
                                    [{"component": "Ledger", "key": "db"}]),
                                ts=200.0))

    # -- two emit crossings on two capabilities -----------------------------
    events.append(wr.make_emit_event(nxt(), 1, "Ledger", "Audit", "audit.write",
                                     wr.cause_trigger("crossed by step-back"),
                                     ts=201.0))
    events.append(wr.make_emit_event(nxt(), 1, "Ledger", "Mailer", "mail.send",
                                     wr.cause_trigger("crossed by step-back"),
                                     ts=201.2))
    # a second Audit crossing, so Audit=2, Mailer=1
    events.append(wr.make_emit_event(nxt(), 1, "Ledger", "Audit", "audit.write",
                                     wr.cause_trigger("crossed by step-back"),
                                     ts=201.3))

    # -- two FAILED withdraws (one classified A8, one unclassified) ----------
    events.append(wr.make_event(nxt(), 1, wr.WITHDRAW, "UserCache",
                                "ACTIVE -> FAILED",
                                wr.cause_trigger("boom", code="A8"), ts=201.5))
    events.append(wr.make_event(nxt(), 1, wr.WITHDRAW, "Ledger",
                                "ACTIVE -> FAILED",
                                wr.cause_trigger("bare crash, no RevlError"),
                                ts=102.5))
    return events


def _v1_trace() -> list[dict]:
    """A pre-schema-v2 trace: load/withdraw with NO `ts`, no `emit`, no `code`.
    Built by hand as v1-shaped records (the additive fields simply absent)."""
    return [
        {"v": 1, "seq": 0, "gen": 1, "event": "load", "component": "Ledger",
         "transition": "PENDING -> ACTIVE", "cause": {"kind": "boot"}},
        {"v": 1, "seq": 1, "gen": 1, "event": "withdraw", "component": "Ledger",
         "transition": "ACTIVE -> DISPOSED",
         "cause": {"kind": "trigger", "detail": "operator"}},
    ]


# --------------------------------------------------------------------------
# metric 1: emissions by capability
# --------------------------------------------------------------------------


def test_emissions_bucketed_by_capability_and_key():
    metrics = m.compute_metrics(_v2_trace())
    emissions = metrics["emissions"]
    assert emissions["total"] == 3
    assert emissions["by_capability"] == {"Audit": 2, "Mailer": 1}
    assert emissions["by_key"] == {"audit.write": 2, "mail.send": 1}


# --------------------------------------------------------------------------
# metric 2: failures by G-rule (code), unclassified bucket for a bare crash
# --------------------------------------------------------------------------


def test_failures_bucketed_by_code_with_unclassified():
    metrics = m.compute_metrics(_v2_trace())
    failures = metrics["failures"]
    assert failures["total"] == 2
    assert failures["by_code"] == {"A8": 1, "unclassified": 1}


def test_clean_withdraw_is_not_a_failure():
    # a DISPOSED/PENDING withdraw never counts as a failure — only FAILED does
    events = [
        wr.make_event(0, 1, wr.WITHDRAW, "Ledger", "ACTIVE -> DISPOSED",
                      wr.cause_trigger("operator"), ts=1.0),
        wr.make_event(1, 1, wr.WITHDRAW, "UserCache", "ACTIVE -> PENDING",
                      wr.cause_provider_withdrawn("Ledger", "db"), ts=1.0),
    ]
    failures = m.compute_metrics(events)["failures"]
    assert failures["total"] == 0
    assert failures["by_code"] == {}


# --------------------------------------------------------------------------
# metric 3: average lifecycle duration (paired by component + gen)
# --------------------------------------------------------------------------


def test_avg_lifecycle_duration():
    metrics = m.compute_metrics(_v2_trace())
    duration = metrics["lifecycleDuration"]
    assert duration["count"] == 2
    # (2.5 + 1.5) / 2 == 2.0
    assert duration["avg_seconds"] == 2.0
    assert duration["by_component"]["Ledger"] == {"count": 1, "avg_seconds": 2.5}
    assert duration["by_component"]["UserCache"] == {"count": 1, "avg_seconds": 1.5}


def test_generation_pairs_the_matching_lifecycle():
    # same component, two generations: each load pairs with its own gen's
    # withdraw, so two lifecycles are measured, not a cross-gen mismatch.
    events = [
        wr.make_event(0, 1, wr.LOAD, "Svc", "PENDING -> ACTIVE",
                      wr.cause_boot(), ts=10.0),
        wr.make_event(1, 1, wr.WITHDRAW, "Svc", "ACTIVE -> DISPOSED",
                      wr.cause_trigger("swap"), ts=13.0),   # 3.0s
        wr.make_event(2, 2, wr.LOAD, "Svc", "PENDING -> ACTIVE",
                      wr.cause_boot(), ts=20.0),
        wr.make_event(3, 2, wr.WITHDRAW, "Svc", "ACTIVE -> DISPOSED",
                      wr.cause_trigger("stop"), ts=21.0),   # 1.0s
    ]
    duration = m.compute_metrics(events)["lifecycleDuration"]
    assert duration["by_component"]["Svc"] == {"count": 2, "avg_seconds": 2.0}
    assert duration["count"] == 2


def test_unmatched_load_or_withdraw_contributes_no_duration():
    # a still-active load (no withdraw) and an orphan withdraw (no load) are
    # simply not counted — neither is a completed lifecycle.
    events = [
        wr.make_event(0, 1, wr.LOAD, "StillUp", "PENDING -> ACTIVE",
                      wr.cause_boot(), ts=1.0),
        wr.make_event(1, 1, wr.WITHDRAW, "Orphan", "ACTIVE -> DISPOSED",
                      wr.cause_trigger("x"), ts=5.0),
    ]
    duration = m.compute_metrics(events)["lifecycleDuration"]
    assert duration["count"] == 0
    assert duration["avg_seconds"] is None
    assert duration["by_component"] == {}


# --------------------------------------------------------------------------
# v1 graceful degradation: duration unavailable, detected via missing ts
# --------------------------------------------------------------------------


def test_v1_trace_reports_duration_unavailable():
    metrics = m.compute_metrics(_v1_trace())
    assert metrics["lifecycleDuration"] == {"unavailable": "ts not present (trace schema v1)"}
    # the other two still compute: no emit events -> 0; a clean DISPOSED
    # withdraw is not a failure -> 0
    assert metrics["emissions"]["total"] == 0
    assert metrics["failures"]["total"] == 0


def test_v1_failure_without_code_buckets_unclassified():
    # a v1 FAILED withdraw carries no `code` -> the unclassified bucket, not a
    # fabricated code; and no `ts` -> duration unavailable (detected by data).
    events = [
        {"v": 1, "seq": 0, "gen": 1, "event": "load", "component": "Ledger",
         "transition": "PENDING -> ACTIVE", "cause": {"kind": "boot"}},
        {"v": 1, "seq": 1, "gen": 1, "event": "withdraw", "component": "Ledger",
         "transition": "ACTIVE -> FAILED",
         "cause": {"kind": "trigger", "detail": "boom"}},
    ]
    metrics = m.compute_metrics(events)
    assert metrics["failures"]["by_code"] == {"unclassified": 1}
    assert "unavailable" in metrics["lifecycleDuration"]


# --------------------------------------------------------------------------
# --json shape, and the CLI
# --------------------------------------------------------------------------


def test_json_shape_is_the_computed_document():
    metrics = m.compute_metrics(_v2_trace())
    # the machine-readable document carries all three metrics under stable keys
    assert set(metrics) == {"events", "emissions", "failures", "lifecycleDuration"}
    assert set(metrics["emissions"]) == {"total", "by_capability", "by_key"}
    assert set(metrics["failures"]) == {"total", "by_code"}
    assert set(metrics["lifecycleDuration"]) == {"count", "avg_seconds", "by_component"}
    # round-trips through JSON unchanged (it is plain data)
    assert json.loads(json.dumps(metrics)) == metrics


def test_render_is_a_human_table():
    rendered = m.render(m.compute_metrics(_v2_trace()))
    assert "emissions by capability" in rendered
    assert "Audit" in rendered
    assert "failures by G-rule" in rendered
    assert "A8" in rendered
    assert "avg lifecycle duration" in rendered


def _write_trace(tmp_path: Path, events: list[dict]) -> str:
    path = tmp_path / "run.jsonl"
    wr.write_trace(events, str(path))
    return str(path)


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "revl", "metrics", *argv],
        cwd=ROOT, capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""})


def test_cli_human_and_json_exit_zero(tmp_path):
    trace = _write_trace(tmp_path, _v2_trace())

    human = _run_cli(trace)
    assert human.returncode == 0
    assert "emissions by capability" in human.stdout
    assert "A8" in human.stdout

    machine = _run_cli(trace, "--json")
    assert machine.returncode == 0
    payload = json.loads(machine.stdout)
    assert payload["emissions"]["by_capability"] == {"Audit": 2, "Mailer": 1}
    assert payload["failures"]["by_code"] == {"A8": 1, "unclassified": 1}
    assert payload["lifecycleDuration"]["avg_seconds"] == 2.0


def test_cli_v1_trace_exits_zero_with_unavailable_duration(tmp_path):
    trace = _write_trace(tmp_path, _v1_trace())
    result = _run_cli(trace, "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "unavailable" in payload["lifecycleDuration"]

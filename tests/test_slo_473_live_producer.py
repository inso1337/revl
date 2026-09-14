"""The LIVE PRODUCER of the SLO contract — roadmap item 473, issue #825.

Slice 2 (`tests/test_slo_473.py`) landed the whole of the observed half as a
pure computation over a RECORDED trace: `slo.observations`, `slo.measure`, the
`on breach` dispatch, the signed `revl.slo-receipt`, `slo.attach_generation`
and the E4 rollout gate, driven by `revl slo`. `docs/design/473-slo-contracts.md`
named exactly one thing missing from the item's exit criterion, in its own
premises table and again in its Exit section:

    the producer inside `Session` | absent | nothing calls `Monitor` from a
    live generation yet; the measurement runs over a RECORDED trace

This file pins that producer, and the two things it needed to be more than a
call site:

  * **The per-call population.** A p95 over the crossings a step-back walk
    visited is not a p95 over the calls the run made. Item 250 Slice 3a already
    writes one `model-decision` WAL record per model completion, AT THE
    CROSSING, carrying the same revl-measured bracket the trace hop carries. So
    the live percentile needs no fourth `why_runtime` event kind and no
    `SCHEMA_VERSION` bump: it needs the record that already exists. Which
    population a verdict was over is NAMED, on the verdict and inside the
    signed body, because the two are different claims.

  * **The pause as a state the session is in.** `slo.pause` writes a latch;
    without a reader that refuses, a paused session keeps dispatching and the
    declared response is a line in a document rather than a stop. So a pause in
    force refuses `call` — distinctly from an E-Stop, because the instance is
    alive and nothing is stranded — and says how to leave it.

The non-vacuity control runs throughout: a compliant generation is sealed with
a receipt, proceeds, and writes NO latch; a session whose composition declares
no `slo` block records no trace, gains no history member and reports no `slo`
key at all.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import slo  # noqa: E402
from revl.compiler import compile_files  # noqa: E402

KEY = b"an-slo-signing-key-for-the-live-producer"

BILLING = """
service Billing {
  emission[net(time="1s", requests=100)] fn charge(account: Str, amount: Int) -> Bool
}

component BillingSvc provides billing: Billing {
  provide billing {
    fn charge(account, amount) = true
  }
}
"""

CONSUMER = """
service Checkout {
  emission fn pay(account: Str) -> Bool
}

component CheckoutSvc requires billing: Billing provides checkout: Checkout {
  provide checkout {
    fn pay(account) = emit billing.charge(account, 100)
  }
}
"""


def project(tmp_path: Path, slo_block: str, name: str = "base") -> Path:
    """A two-row composition whose `slo` clause is `slo_block` verbatim, plus
    the sources its rows name."""
    (tmp_path / "billing.rvl").write_text(BILLING)
    (tmp_path / "consumer.rvl").write_text(CONSUMER)
    doc = tmp_path / f"{name}.rvl"
    doc.write_text(f"""
composition {name} {{
  {slo_block}
  row @checkout from "consumer.rvl" provides checkout
  row @billing from "billing.rvl" provides billing
}}
""")
    return doc


def decisions(n: int, seconds: float, component: str = "CheckoutSvc") -> list:
    """`n` durable `model-decision` records, the shape
    `replay.WriteAheadLog.record_model_decision` writes: one per completion, at
    the crossing, carrying the revl-measured bracket."""
    return [{"record": "model-decision", "component": component,
             "stepIndex": i, "outcome": "validated",
             "llm": {"latencyProvenance": "revl-measured-bracket",
                     "latencySeconds": seconds}}
            for i in range(n)]


def hops(n: int, seconds: float, component: str = "CheckoutSvc") -> list:
    """`n` trace `emit` hops — the slice-2 population, recorded by the driver
    during a step-back walk."""
    return [{"event": "emit", "component": component, "gen": 1, "ts": float(i),
             "llm": {"latencyProvenance": "revl-measured-bracket",
                     "latencySeconds": seconds}}
            for i in range(n)]


# ------------------------------------------- the per-call latency population

def test_the_run_s_own_completions_are_the_percentile_s_population():
    """The load-bearing upgrade. A `model-decision` record is written at the
    crossing by the generation that made it, so a percentile over those records
    is a percentile over the calls the run made."""
    obs = slo.observations([], decisions(30, 0.4))
    assert obs["latencySource"] == slo.LATENCY_FROM_WAL
    assert len(obs["latencies"]) == 30
    assert obs["decisions"] == 30


def test_a_replayed_hop_population_is_never_pooled_with_the_run_s_own_calls():
    """The two sources describe the SAME crossings — the WAL record and the
    trace hop are keyed on one another by `(component, stepIndex)` — so pooling
    them would count every completion twice and halve the rank a p95 selects.
    The run's own records win, and the trace hops are not added."""
    obs = slo.observations(hops(30, 9.0), decisions(30, 0.4))
    assert obs["latencySource"] == slo.LATENCY_FROM_WAL
    assert len(obs["latencies"]) == 30
    assert max(obs["latencies"]) == 0.4


def test_with_no_completions_recorded_the_trace_hops_are_still_the_fallback():
    """Byte-compatible with slice 2: a run that wrote no WAL is measured
    exactly as before, and says which source that was."""
    obs = slo.observations(hops(30, 3.0), [])
    assert obs["latencySource"] == slo.LATENCY_FROM_TRACE
    assert len(obs["latencies"]) == 30
    obs = slo.observations([], [])
    assert obs["latencySource"] == slo.LATENCY_NONE


def test_a_host_reported_latency_is_not_folded_into_a_revl_measured_percentile():
    """The scope constraint, on the new source too: a payload that does not
    claim `revl-measured-bracket` is a host number, and it is counted as
    skipped rather than measured."""
    records = decisions(4, 0.4)
    for record in records[:3]:
        record["llm"]["latencyProvenance"] = "host-reported"
    obs = slo.observations([], records)
    assert len(obs["latencies"]) == 1
    assert obs["hostReportedLatencies"] == 3


def test_the_wal_record_kind_agrees_with_the_wal_reader_s_own_constant():
    """`slo` restates the record kind so it imports no runtime; a drift would
    leave every completion invisible to the measurement while the WAL still
    carried it."""
    from revl.wal import RECORD_MODEL_DECISION

    assert slo.WAL_MODEL_DECISION == RECORD_MODEL_DECISION


def test_decisions_from_wal_reads_a_real_log_and_fail_softs_on_a_bad_one(tmp_path):
    """Fail SOFT, deliberately: no samples is `insufficient`, which this module
    already refuses to read as `holding`, so a measurement source that is gone
    degrades the datum honestly instead of breaking the generation."""
    path = tmp_path / "session.wal"
    lines = [{"record": "header", "walVersion": 1, "generation": 1,
              "guarantee": "x"}]
    lines += decisions(3, 0.5)
    lines += [{"record": "activation-complete", "generation": 1,
               "components": []}]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert len(slo.decisions_from_wal(str(path))) == 3

    assert slo.decisions_from_wal(None) == []
    assert slo.decisions_from_wal(str(tmp_path / "absent.wal")) == []
    torn = tmp_path / "garbage.wal"
    torn.write_text("this is not a write-ahead log\n")
    assert slo.decisions_from_wal(str(torn)) == []


# --------------------------------------------------- the population, named

def test_the_verdict_and_the_signed_body_name_which_population_it_measured(tmp_path):
    """A receipt that reported a number without its population would let a
    replay-visited p95 and the run's own p95 read identically. Both readings
    are inside the SIGNED body, and a freshly signed receipt verifies."""
    from revl.composition import resolve_file

    doc = project(tmp_path, "slo { p95_latency: 2s }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    monitor = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          generation=3, key=KEY, decisions=decisions(30, 3.0),
                          latch=str(tmp_path / "s.estop"),
                          now="2026-01-01T00:00:00+00:00")
    verdict = monitor.seal()
    assert verdict["verdicts"]["p95_latency_ms"]["measurement"] == \
        slo.LATENCY_FROM_WAL
    assert monitor.receipt["observed"]["latencySource"] == slo.LATENCY_FROM_WAL
    assert monitor.receipt["observed"]["samples"]["decisions"] == 30
    # SIGNING DISCIPLINE: every member the verifier covers is a member the
    # signer covered, so a receipt this call just signed verifies.
    assert slo.verify_receipt(monitor.receipt, KEY) == (True, "")


# --------------------------------------------------- seal versus observe

def test_a_generation_that_held_its_contract_is_still_sealed_with_a_receipt(tmp_path):
    """The design's exit reads "a generation that runs under a declared
    contract produces a verifiable receipt on its generation-history entry" —
    every generation, not only the ones that failed. A history carrying
    receipts only for breaches makes their ABSENCE ambiguous between "it held"
    and "nobody measured"."""
    from revl.composition import resolve_file

    doc = project(tmp_path, 'slo { p95_latency: 2s on breach halt }')
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    latch = tmp_path / "session.estop"
    monitor = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          generation=1, latch=str(latch), key=KEY,
                          decisions=decisions(30, 0.2))
    verdict = monitor.seal()
    assert verdict["breached"] == []
    assert slo.verify_receipt(monitor.receipt, KEY) == (True, "")
    assert verdict["verdicts"]["p95_latency_ms"]["verdict"] == slo.HOLDING
    # NON-VACUITY: sealed, and NOTHING was done about it.
    assert not latch.exists()
    assert "dispatch" not in monitor.receipt["response"]


def test_a_boundary_records_a_breach_and_arms_nothing(tmp_path):
    """The hazard this split exists to remove. A generation boundary is the
    moment the measured generation stops running, and the latch a pause writes
    OUTLIVES the process — so a boundary that dispatched would arm a stop
    against the successor generation, or against the next process to read that
    latch, for a breach neither of them committed."""
    from revl.composition import resolve_file

    doc = project(tmp_path, "slo { p95_latency: 2s on breach halt }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    latch = tmp_path / "session.estop"
    monitor = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          generation=6, latch=str(latch), key=KEY,
                          decisions=decisions(30, 9.0))
    verdict = monitor.record()
    # The breach is measured and named IN FULL ...
    assert verdict["breached"] == ["p95_latency_ms"]
    assert verdict["action"] == slo.HALT
    assert monitor.receipt["response"]["breached"] == ["p95_latency_ms"]
    assert slo.verify_receipt(monitor.receipt, KEY) == (True, "")
    # ... and NOTHING was done about it, which the receipt states by the
    # absence of a dispatch rather than by claiming an action it did not take.
    assert not latch.exists()
    assert monitor.dispatch is None
    assert "dispatch" not in monitor.receipt["response"]

    # `seal` is the dispatching form, for a caller that owns the latch's whole
    # lifetime (`revl slo --seal`, where the operator named it on the command
    # line).
    sealing = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          generation=6, latch=str(latch), key=KEY,
                          decisions=decisions(30, 9.0))
    sealing.seal()
    assert latch.exists()
    assert sealing.receipt["response"]["dispatch"]["armed"] is True


def test_observe_still_signs_only_a_breach(tmp_path):
    """`seal` is additive: the in-flight probe keeps slice 2's contract, where
    a receipt is the account of an action taken."""
    from revl.composition import resolve_file

    doc = project(tmp_path, "slo { p95_latency: 2s }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    monitor = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          generation=1, key=KEY, decisions=decisions(30, 0.2))
    assert monitor.observe()["breached"] == []
    assert monitor.receipt is None


# ------------------------------------------------ the pause, as a state

def test_a_pause_and_a_halt_are_told_apart_by_what_is_in_force(tmp_path):
    latch = tmp_path / "s.estop"
    slo.pause(latch=str(latch), reason="measured breach", now=1.0)
    state = slo.latch_state(str(latch))
    assert state["verdict"] == slo.PAUSED and state["resumable"] is True
    assert slo.paused(str(latch))["reason"] == "measured breach"

    other = tmp_path / "h.estop"
    slo.halt(latch=str(other), reason="measured breach", now=1.0)
    assert slo.latch_state(str(other))["verdict"] == slo.HALTED
    # A halted instance is NOT merely paused, so the narrower question answers
    # None rather than reporting a dead instance as recoverable.
    assert slo.paused(str(other)) is None


def test_a_stop_that_cannot_be_classified_reads_as_halted_not_paused(tmp_path):
    """FAIL CLOSED. A pause is the WEAKER claim (alive, nothing stranded), so
    an unclassifiable stop must never be read as one — that would talk a host
    into resuming an instance that was killed."""
    latch = tmp_path / "weird.estop"
    latch.write_text("not json at all")
    assert slo.latch_state(str(latch))["verdict"] == slo.HALTED
    assert slo.paused(str(latch)) is None

    forged = tmp_path / "forged.estop"
    forged.write_text(json.dumps({"halted": True, "verdict": "paused",
                                  "resumable": False}))
    assert slo.latch_state(str(forged))["verdict"] == slo.HALTED

    assert slo.latch_state(str(tmp_path / "absent.estop")) is None
    assert slo.paused(None) is None


# ================================================== the live session producer

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the live producer needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`, then run this file under "
           "`backends/python/.venv/bin/pytest`",
)


def _session(tmp_path, doc, *, record=True, wal=True):
    """A live session running the composition's sources, with its contract
    discovered from the admission inputs the way `load` does it."""
    from revl.mcp.session import Session

    ir = compile_files([str(tmp_path / "consumer.rvl"),
                        str(tmp_path / "billing.rvl")])
    session = Session()
    if wal:
        session._wal_path = str(tmp_path / "session.wal")
    session.load(ir, origin={"files": [str(doc)]}, record=record)
    if wal:
        session._ensure_wal_open()
    return session


def _write_decisions(session, n, seconds, component="CheckoutSvc"):
    """Append `n` completions through the session's OWN WAL writer — the same
    `record_model_decision` the crossing seam calls, so the producer reads the
    records the running generation actually wrote."""
    wal = session.recorder.wal
    for index in range(n):
        wal.record_model_decision(
            component=component, step_index=index, outcome="validated",
            llm={"latencyProvenance": "revl-measured-bracket",
                 "latencySeconds": seconds})


@needs_runtime
def test_a_live_generation_produces_a_verifiable_receipt_on_its_own_entry(
        tmp_path, monkeypatch):
    """THE EXIT CLAUSE. Nothing in this test calls `revl slo`: the running
    session measures its own generation, signs the receipt and files it against
    its own generation-history entry."""
    monkeypatch.setenv(slo.KEY_ENV, KEY.decode())
    doc = project(tmp_path, "slo { p95_latency: 2s }")
    session = _session(tmp_path, doc)
    try:
        assert session.slo_status()["declared"] is True
        assert session.call("checkout", "pay", ["acct"])["result"] is True
        _write_decisions(session, 30, 0.2)

        report = session.slo_seal()
        assert report["signed"] is True and report["attached"] is True
        assert report["breached"] == []

        entry = [e for e in session._history if e["generation"] == 1][0]
        receipt = entry["sloReceipt"]
        assert receipt["kind"] == slo.RECEIPT_KIND
        assert receipt["generation"] == 1
        assert slo.verify_receipt(receipt, KEY) == (True, "")
        assert receipt["observed"]["latencySource"] == slo.LATENCY_FROM_WAL
        assert receipt["verdicts"]["p95_latency_ms"]["verdict"] == slo.HOLDING
        # exported, so `revl plan`'s E4 reads THESE bytes
        exported = session.history_document()["generations"]
        assert exported[0]["sloReceipt"] == receipt
        # NON-VACUITY: a compliant generation proceeds and stops nothing.
        assert session.slo_paused() is None
        assert session.call("checkout", "pay", ["acct"])["result"] is True
        assert not (tmp_path / "session.wal.estop").exists()
    finally:
        session.unload()


@needs_runtime
def test_a_live_breach_pauses_the_running_session_and_the_receipt_names_it(
        tmp_path, monkeypatch):
    """The other half of the exit clause: a LIVE breach — the generation is
    still running when it is measured — takes the declared response, and the
    session is genuinely stopped rather than merely reported on."""
    monkeypatch.setenv(slo.KEY_ENV, KEY.decode())
    doc = project(tmp_path, "slo { p95_latency: 2s on breach pause }")
    session = _session(tmp_path, doc)
    try:
        assert session.call("checkout", "pay", ["acct"])["result"] is True
        _write_decisions(session, 30, 3.0)

        report = session.slo_observe()
        assert report["breached"] == ["p95_latency_ms"]
        assert report["action"] == slo.PAUSE
        assert report["dispatch"]["armed"] is True

        record = session.slo_paused()
        assert record["verdict"] == slo.PAUSED and record["resumable"] is True
        assert "p95_latency_ms" in record["reason"]
        assert session.state()["slo"]["paused"] is True

        # The stop is a stop: a new boundary crossing is refused, and the
        # refusal is the PAUSE refusal, not the E-Stop's.
        from revl.mcp.session import SessionError

        with pytest.raises(SessionError) as exc:
            session.call("checkout", "pay", ["acct"])
        assert "PAUSED" in str(exc.value)
        assert "nothing is stranded" in str(exc.value)
        assert "revl recover" not in str(exc.value)

        # ... and lifting the latch resumes dispatch, which is the whole
        # difference between a pause and a halt.
        Path(record["latch"]).unlink()
        assert session.slo_paused() is None
        assert session.call("checkout", "pay", ["acct"])["result"] is True
    finally:
        session.unload()


@needs_runtime
def test_the_teardown_boundary_seals_the_last_generation_without_stopping_it(
        tmp_path, monkeypatch):
    """The session teardown is the last generation boundary, so the final
    generation gets its receipt there — and, because a boundary records rather
    than acts, an unload never leaves a latch behind for the next process."""
    monkeypatch.setenv(slo.KEY_ENV, KEY.decode())
    doc = project(tmp_path, "slo { p95_latency: 2s on breach halt }")
    session = _session(tmp_path, doc)
    filed = None
    try:
        _write_decisions(session, 30, 9.0)      # a breaching generation
    finally:
        session._seal_generation()
        filed = [e for e in session._history if e["generation"] == 1][0]
        session.unload()
    receipt = filed["sloReceipt"]
    assert receipt["response"]["breached"] == ["p95_latency_ms"]
    assert slo.verify_receipt(receipt, KEY) == (True, "")
    assert "dispatch" not in receipt["response"]
    assert not (tmp_path / "session.wal.estop").exists()


@needs_runtime
def test_the_swap_boundary_seals_the_generation_it_ends(tmp_path, monkeypatch):
    """A generation stops being the live one at a swap, so that is where its
    account is closed — before the teardown, while it is still the generation
    that ran."""
    monkeypatch.setenv(slo.KEY_ENV, KEY.decode())
    doc = project(tmp_path, "slo { p95_latency: 2s }")
    session = _session(tmp_path, doc)
    try:
        _write_decisions(session, 30, 0.2)
        ir = compile_files([str(tmp_path / "consumer.rvl"),
                            str(tmp_path / "billing.rvl")])
        session.swap(ir, origin={"files": [str(doc)]})
        assert session._generation == 2

        sealed = [e for e in session._history if e["generation"] == 1][0]
        assert slo.verify_receipt(sealed["sloReceipt"], KEY) == (True, "")
        assert sealed["sloReceipt"]["generation"] == 1
        # gen 2 has not ended, so it has no receipt yet: a receipt is an
        # account of a generation that RAN.
        live = [e for e in session._history if e["generation"] == 2][0]
        assert "sloReceipt" not in live
    finally:
        session.unload()


@needs_runtime
def test_a_session_whose_composition_declares_no_slo_block_is_unaffected(
        tmp_path, monkeypatch):
    """The byte-identity property at the producer. No contract means: no trace
    recorded, no WAL read, no history member, no `slo` key on `state()`, and
    every verb answering exactly what it answered before item 473."""
    monkeypatch.setenv(slo.KEY_ENV, KEY.decode())
    doc = project(tmp_path, "")
    session = _session(tmp_path, doc)
    try:
        assert session.slo_status()["declared"] is False
        assert session._driver.tracing is False
        assert session.slo_seal() == {
            "declared": False,
            "note": "the running composition declares no `slo` block"}
        assert session.slo_observe()["declared"] is False
        state = session.state()
        assert "slo" not in state
        assert session.call("checkout", "pay", ["acct"])["result"] is True
        assert all("sloReceipt" not in e for e in session._history)
        assert all("sloReceipt" not in g
                   for g in session.history_document()["generations"])
    finally:
        session.unload()


@needs_runtime
def test_a_declared_contract_arms_the_trace_the_measurement_reads(
        tmp_path, monkeypatch):
    """A contract is a request to be measured. Without this the causal trace
    was only ever recorded under `--trace`/`--withdraw`, so a live generation
    under a declared SLO recorded no lifecycle event at all."""
    monkeypatch.setenv(slo.KEY_ENV, KEY.decode())
    doc = project(tmp_path, "slo { p95_latency: 2s }")
    session = _session(tmp_path, doc)
    try:
        assert session._driver.tracing is True
        assert session._slo_events(), "the generation recorded no causal event"
    finally:
        session.unload()


@needs_runtime
def test_an_unsigned_session_says_so_rather_than_filing_nothing_quietly(
        tmp_path, monkeypatch):
    """With no key there is no receipt, and "no receipt" must not be reported
    as a clean generation."""
    monkeypatch.delenv(slo.KEY_ENV, raising=False)
    monkeypatch.delenv(slo.KEY_FILE_ENV, raising=False)
    doc = project(tmp_path, "slo { p95_latency: 2s }")
    session = _session(tmp_path, doc)
    try:
        _write_decisions(session, 30, 0.2)
        report = session.slo_seal()
        assert report["declared"] is True
        assert report["signed"] is False and report["attached"] is False
        assert "REVL_SLO_KEY" in report["unsigned"]
        assert all("sloReceipt" not in e for e in session._history)
    finally:
        session.unload()

"""Roadmap item 253's exit test: the derived saga on a REAL Temporal server.

The item's exit criterion is one sentence: "one saga demo running on a real
Temporal dev server with an injected mid-saga fault compensating in derived
order". Everything else about the target is checked against emitted TEXT, which
proves what revl wrote and nothing about what Temporal does with it. This
module runs the emitter's own output — never a hand-written stand-in — against
a live server and measures three things the text alone cannot establish:

  1. the compensations drain in the G7 LIFO order revl DERIVED from source
     order (`payments.refund` before `flights.cancel`), after a fault injected
     mid-saga;
  2. the crossing whose declaration carries `idempotent(key: card)` — item
     309's `keyed` register, the one class Slice 2's derivation promotes — is
     actually RETRIED by the platform (its host body fails once and the saga
     gets past it);
  3. the crossing that declares nothing is actually NOT retried. This is the
     load-bearing negative: the whole reason the derivation refuses to promote
     `declared`, `undo idempotent` and the folded `register: "read"` is that a
     Temporal retry re-runs a real effect, and a run that quietly attempted a
     non-idempotent activity twice would make every one of those refusals
     theatre.

THE GATE. `REVL_TEMPORAL_DEV_SERVER` holds the address of a running
`temporal server start-dev`. It is set in the `temporal-exit` CI job, which
provisions the pinned Temporal CLI and the SDK the same way `backend-wasm`
provisions wasmtime; `tests/test_env_gated_skips_run_somewhere.py` (item 445)
is what stops it from being read here and set nowhere, which is how a gated
suite reports green while measuring nothing. Past the gate NOTHING skips: a
missing node, a missing install and an unreachable server are all FAILURES, so
a broken CI provisioning step cannot degrade back into a silent skip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "tests" / "temporal_exit"
SAGA = PROJECT / "saga.revl"
# Written fresh by the test from the emitter, then handed to the worker's
# bundler. Not committed (see the directory's .gitignore): a stale copy would
# let this pass against code that is no longer what revl emits.
GENERATED = PROJECT / "workflows.generated.ts"

# A BARE read, deliberately: nothing but CI (or a developer with a dev server
# up) can supply a value, which is what makes item 445's audit see this as a
# gate rather than an override.
_DEV_SERVER_ENV = "REVL_TEMPORAL_DEV_SERVER"


def _address() -> str:
    address = os.environ.get(_DEV_SERVER_ENV)
    if not address:
        pytest.skip(
            f"{_DEV_SERVER_ENV} is unset: no Temporal dev server to run the "
            f"item-253 exit saga against. Start one with `temporal server "
            f"start-dev` and set {_DEV_SERVER_ENV}=127.0.0.1:7233."
        )
    return address


def _emit_workflow() -> str:
    """The emitter's own `--target temporal` output for the exit saga."""
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "emit", "--backend", "typescript",
         "--target", "temporal", str(SAGA)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _run_saga(address: str, workflow: str) -> dict:
    """Run the emitted workflow on the live server; return the run journal."""
    # Past the gate, an absent toolchain is a FAILURE. A skip here would be the
    # exact shape item 445 exists to stop: green, and measuring nothing.
    assert shutil.which("node"), (
        "node is not on PATH, but the Temporal exit gate is on. The job that "
        "sets it must provision node.")
    assert (PROJECT / "node_modules" / "@temporalio" / "worker").is_dir(), (
        f"the Temporal SDK is not installed in {PROJECT}. Run `npm ci` there; "
        f"the CI job that sets the gate does this before pytest.")
    GENERATED.write_text(workflow, encoding="utf-8")
    proc = subprocess.run(
        ["node", "run_saga.js", address, GENERATED.name],
        capture_output=True, text=True, cwd=str(PROJECT), timeout=300,
    )
    assert proc.returncode == 0, (
        f"the saga runner failed against {address}:\n{proc.stderr[-4000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def live_run() -> dict:
    address = _address()
    workflow = _emit_workflow()
    run = _run_saga(address, workflow)
    run["workflow"] = workflow
    return run


def _names(run: dict) -> list:
    return [entry["name"] for entry in run["journal"]]


def test_emitted_policies_say_what_the_run_should_do(live_run):
    """The claim half. `settle` carries the key, so it is in the retryable
    group; the crossing with no evidence is not. Asserted here so a run that
    matches is matching a DERIVED claim, not a coincidence."""
    workflow = live_run["workflow"]
    retryable = workflow.split("retry: DEDUP_SAFE_RETRY", 1)[0].rsplit("const {", 1)[1]
    assert "settle" in retryable
    at_most_once = workflow.split("retry: AT_MOST_ONCE", 1)[0].rsplit("const {", 1)[1]
    assert "boomTrigger" in at_most_once and "settle" not in at_most_once


def test_the_saga_aborts_with_the_residue_envelope(live_run):
    """The injected fault aborts the run, and the failure still carries the
    outstanding/worldRemaining/proof envelope `revl recover` guarantees."""
    failure = live_run["failure"]
    assert failure is not None, "the injected mid-saga fault did not abort the run"
    assert failure["type"] == "SagaAbort"
    assert live_run["report"]["proof"] == "revl-saga-abort"
    assert live_run["report"]["worldRemaining"] == 2
    assert failure["details"] == [live_run["report"]]


def test_compensations_drain_in_the_derived_lifo_order(live_run):
    """G7, measured: the two compensations run in reverse source order, after
    the fault and before the residue sink. Nobody wrote that order — the
    emitter derived it from the same accounting `recovery.py` uses."""
    names = _names(live_run)
    assert names.index("boomTrigger") < names.index("paymentsRefund")
    assert names.index("paymentsRefund") < names.index("flightsCancel")
    assert names.index("flightsCancel") < names.index("recordResidue")
    # and nothing compensated a crossing that has no registered inverse
    assert "settle" in names and names.count("recordResidue") == 1


def test_the_keyed_crossing_was_actually_retried(live_run):
    """The evidence-derived policy, honoured by the real platform: `settle`
    declares `idempotent(key: card)`, its host body fails once, and the run
    gets past it — which can only happen if Temporal re-delivered it."""
    attempts = [e["attempt"] for e in live_run["journal"] if e["name"] == "settle"]
    assert attempts == [1, 2], live_run["journal"]


def test_the_crossing_without_evidence_was_never_retried(live_run):
    """The load-bearing negative. `boom.trigger` declares nothing, so the
    derivation left it at `maximumAttempts: 1`. Its host body always fails, so
    a second entry here would mean the platform re-ran an effect no evidence
    said was safe to re-run — a silent double-apply, and the failure mode every
    refusal in the derivation exists to prevent."""
    attempts = [e["attempt"] for e in live_run["journal"] if e["name"] == "boomTrigger"]
    assert attempts == [1], live_run["journal"]
    # the compensations are at-most-once for the same reason
    for name in ("paymentsRefund", "flightsCancel"):
        assert [e["attempt"] for e in live_run["journal"] if e["name"] == name] == [1]

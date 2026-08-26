"""Crash recovery on the WASM tier, end to end (roadmap item 322, Slice 2).

The wasm analog of tests/test_go_crash_recovery.py, driven by a REAL cordis-wasm
process. Slice 1 generalized the WAL/recovery core so `revl recover` reads a WAL
produced by any tier (src/revl/wal.py); Slice 2 gives the wasm tier — the one
tier with NO direct filesystem — a durable WAL path via stdout-framing:

* a witnessed wasm composition (backends/wasm/scenarios/crashproof) is emitted in
  record mode, so its witnessed transactional registration FRAMES the
  discharge-descriptor's runtime values out through the `coeffect:revl:wal.record`
  host import;
* the host-side drain (revl.run_wasm.wal_descriptor + write_wal_record) assembles
  the py-schema record and fsyncs it to a host-visible WAL file;
* the wasm process is CRASHED mid-session (os._exit before commit), leaving the
  descriptor on disk with no `discharge` / `activation-complete` marker;
* `revl recover` — the SAME tier-agnostic core the py/go tiers use, with no wasm
  runtime on the path — reads the wasm WAL, re-issues the wasm tier's declared
  inverse (`deleteRow("row#1")`) LIFO, and lands residue-free;
* the clean control (no crash) writes the discharge + terminal marker, and
  recover rolls forward instead.

Requires the cordis-wasm runtime; skipped with a reason when it is unavailable
(the runtime-availability convention the wasm run tier uses), so a CI box without
it does not red on it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import wal as wal_core  # noqa: E402
from revl.recovery import recover  # noqa: E402
from revl.run_wasm import (  # noqa: E402
    _cordis_wasm_dir,
    _cordis_wasm_python,
    wasm_runtime_reason,
)

PRODUCER = (ROOT / "backends" / "wasm" / "scenarios" / "crashproof"
            / "crash_producer.py")


@pytest.fixture(scope="module")
def wasm_runtime():
    """Gate on the cordis-wasm runtime, exactly as `revl run --backend wasm`
    does — a missing runtime is a skip-with-reason, never a red."""
    reason = wasm_runtime_reason()
    if reason is not None:
        pytest.skip(f"cordis-wasm runtime not available: {reason}")
    return _cordis_wasm_python()


def _run_producer(python: str, wal: Path, *, crash: bool) -> subprocess.CompletedProcess:
    env = {
        "REVL_WAL": str(wal),
        "CORDIS_WASM": str(_cordis_wasm_dir()),
        "PATH": "/usr/bin:/bin",
    }
    if crash:
        env["REVL_CRASH_BEFORE_COMMIT"] = "1"
    return subprocess.run(
        [python, str(PRODUCER)], env=env, capture_output=True, text=True,
    )


def test_a_wasm_process_crash_mid_session_rolls_back_from_the_durable_wal(
        wasm_runtime, tmp_path):
    """The proof: a real cordis-wasm process registers a witnessed mutation
    (durable descriptor, framed out and fsynced by the host drain), then CRASHES
    before commit. recover reads the wasm-produced WAL through the tier-agnostic
    core and rolls the wasm tier's inverse back clean."""
    wal = tmp_path / "wasm_crash.wal"
    proc = _run_producer(wasm_runtime, wal, crash=True)

    # the process really died mid-session (os._exit(137)), not a graceful stop
    assert proc.returncode == 137, proc.stderr

    # the durable WAL carries the descriptor but NO commit markers — read it
    # through the same tier-agnostic reader recover uses
    loaded = wal_core.read_wal(str(wal))
    descriptors = [r for r in loaded["records"]
                   if r.get("record") == "discharge-descriptor"]
    assert len(descriptors) == 1
    assert descriptors[0]["entry"] == "transactional"
    assert descriptors[0]["call"] == {
        "receiver": "beginRow", "method": "deleteRow", "args": ["row#1"]}
    assert not [r for r in loaded["records"] if r.get("record") == "discharge"]
    assert loaded["complete"] is False   # no activation-complete: the crash

    # recover re-issues the wasm tier's declared inverse and lands residue-free
    report = recover(str(wal))
    assert report["verdict"] == "rolled-back"
    assert [s["seq"] for s in report["transactionalRolledBack"]] == [0]
    assert report["dischargedSkipped"] == []
    # the mutation's referent was cleared by the re-issued inverse
    assert "beginRow:row#1" not in report["residue"]["worldRemaining"]
    assert report["residue"]["clean"] is True
    assert "LIFO" in report["decision"]


def test_the_clean_control_commits_and_rolls_forward(wasm_runtime, tmp_path):
    """The contrast: no crash. The wasm process unloads cleanly, stamps the
    discharge + terminal marker, and recover rolls forward — the committed
    mutation is retained, never rolled back."""
    wal = tmp_path / "wasm_clean.wal"
    proc = _run_producer(wasm_runtime, wal, crash=False)
    assert proc.returncode == 0, proc.stderr

    loaded = wal_core.read_wal(str(wal))
    assert loaded["complete"] is True    # activation-complete present
    discharged = [r for r in loaded["records"] if r.get("record") == "discharge"]
    assert discharged == [{"record": "discharge", "discharged": [0]}]

    report = recover(str(wal))
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is True

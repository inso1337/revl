"""Crash recovery on the RUST tier, end to end (roadmap item 322, Slice 2).

The rust analog of tests/test_crash_recovery.py's "simulated kill -9" case, but
driven by a REAL rust process. Slice 1 generalized the WAL/recovery core so
`revl recover` reads a WAL produced by any tier (src/revl/wal.py); Slice 1 proved
it on go and this proves it on rust:

* a witnessed rust composition (backends/rust/scenarios/crashproof) is emitted in
  record mode, so its witnessed transactional mutation writes a durable
  discharge-descriptor to a host-visible WAL file and fsyncs it (`File::sync_all`);
* the rust process is CRASHED mid-session (`process::exit(137)` before commit),
  leaving the descriptor on disk with no `discharge` / `activation-complete`;
* `revl recover` — the SAME tier-agnostic core the py tier uses, with no py
  backend on the path — reads the rust WAL, re-issues the rust tier's declared
  inverse (`deleteRow(row#1)`) LIFO, and lands residue-free;
* the clean control (no crash) writes the discharge + terminal marker, and
  recover rolls forward instead.

Requires a cordis-rs toolchain; skipped with a reason when cargo/cordis-rs is
unavailable (the same runtime-availability gate the rust run tier uses,
:func:`revl.run_rust.rust_runtime_reason`), so a box without it does not red.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import wal as wal_core  # noqa: E402
from revl.recovery import recover  # noqa: E402
from revl.run_rust import rust_runtime_reason  # noqa: E402

SCENARIO = ROOT / "backends" / "rust" / "scenarios" / "crashproof"
MANIFEST = SCENARIO / "Cargo.toml"


@pytest.fixture(scope="module")
def producer_binary():
    """Compile the crash-producer binary once for the module."""
    reason = rust_runtime_reason()
    if reason is not None:
        pytest.skip(
            "cordis-rs runtime not available; the rust crash-recovery proof "
            f"needs it: {reason}")
    build = subprocess.run(
        ["cargo", "build", "--bin", "crash_producer",
         "--manifest-path", str(MANIFEST)],
        capture_output=True, text=True,
    )
    if build.returncode != 0:
        pytest.fail(f"building the rust crash producer failed:\n{build.stderr}")
    binary = SCENARIO / "target" / "debug" / "crash_producer"
    assert binary.exists(), f"expected {binary} after cargo build"
    return binary


def _run_producer(binary: Path, wal: Path, *, crash: bool) -> subprocess.CompletedProcess:
    env = {"REVL_WAL": str(wal), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if crash:
        env["REVL_CRASH_BEFORE_COMMIT"] = "1"
    return subprocess.run([str(binary)], env=env, capture_output=True, text=True)


def test_a_rust_process_crash_mid_session_rolls_back_from_the_durable_wal(
        producer_binary, tmp_path):
    """The proof: a real rust process registers a witnessed mutation (durable
    descriptor), then CRASHES before commit. recover reads the rust-written WAL
    through the tier-agnostic core and rolls the rust tier's inverse back clean."""
    wal = tmp_path / "rust_crash.wal"
    proc = _run_producer(producer_binary, wal, crash=True)

    # the process really died mid-session (process::exit(137)), not a graceful stop
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

    # recover re-issues the rust tier's declared inverse and lands residue-free
    report = recover(str(wal))
    assert report["verdict"] == "rolled-back"
    assert [s["seq"] for s in report["transactionalRolledBack"]] == [0]
    assert report["dischargedSkipped"] == []
    # the mutation's referent was cleared by the re-issued inverse
    assert "beginRow:row#1" not in report["residue"]["worldRemaining"]
    assert report["residue"]["clean"] is True
    assert "LIFO" in report["decision"]


def test_the_clean_control_commits_and_rolls_forward(producer_binary, tmp_path):
    """The contrast: no crash. The rust process disposes cleanly, stamps the
    discharge + terminal marker, and recover rolls forward — the committed
    mutation is retained, never rolled back."""
    wal = tmp_path / "rust_clean.wal"
    proc = _run_producer(producer_binary, wal, crash=False)
    assert proc.returncode == 0, proc.stderr

    loaded = wal_core.read_wal(str(wal))
    assert loaded["complete"] is True    # activation-complete present
    discharged = [r for r in loaded["records"] if r.get("record") == "discharge"]
    assert discharged == [{"record": "discharge", "discharged": [0]}]

    report = recover(str(wal))
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is True

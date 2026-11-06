"""Issue #2758 / roadmap item 469: the drift monitor and its actuators.

:mod:`revl.drift` landed the pure half of the control: the admitted manifest,
the verdict over a re-measurement, the configured response and the WAL record.
It re-hashes nothing and suspends nothing. This file pins the RUNNING half, the
three things the item's Exit line needs:

  * a monitor that re-measures local subjects and builds the reading the verdict
    takes. A tool is an emitted artifact tree (hashed by deploy's own
    `artifact_digest`), a policy is a file (hashed by `drift.digest`). A
    declaration whose bytes cannot be measured is reported unverified with its
    cause, never silently compared as a match;
  * actuators that carry out only the two responses that NARROW authority:
    `read-only` arms a latch the py runtime checks at every effectful crossing,
    and `suspend` arms the item-443 E-Stop latch. `re-admit` and `rollback`
    change what runs, so this slice refuses them by name;
  * one durable `drift-detected` record per non-matched pass, in the WAL shape
    `revl.wal` already reads.

The load-bearing distinctions this file is written to break if they are
weakened:

  * a deleted path is `missing` (a measured absence, drift, suspend by default),
    while a subject that is present but unmeasurable, or an admitted subject
    with no declaration at all, is `unverified` (a blind spot, no response by
    default) - absence of evidence is not drift;
  * a response is a downgrade: the monitor never fires more than the policy
    granted, never replaces a response it cannot carry out with a different one,
    and never widens authority.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import deploy, drift, drift_monitor, estop, wal  # noqa: E402
import runtime as py_runtime  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_runtime():
    def reset():
        py_runtime.arm_read_only_latch(None)
        py_runtime.arm_estop_latch(None)
        py_runtime.clear_estop()

    reset()
    yield
    reset()


def _workspace(tmp_path):
    """One admitted tool tree and one admitted policy file, plus their
    declarations. Bytes are fixed so the test can edit one subject at a time."""
    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "tool.py").write_bytes(b"tool:checker@1\n")
    policy = tmp_path / "policy.json"
    policy.write_bytes(b'{"allow": ["read"]}\n')
    declarations = [
        {"role": "tool", "name": "checker", "kind": "tree", "path": str(tool)},
        {"role": "policy", "name": "main", "kind": "file", "path": str(policy)},
    ]
    return tool, policy, declarations


def _document(tmp_path):
    tool, policy, declarations = _workspace(tmp_path)
    return drift_monitor.admit(declarations), tool, policy


def _without(document, subject):
    """A copy of the document with one declaration removed, which is the shape
    "the reading did not produce this admitted subject" takes."""
    return {
        **document,
        "subjects": [
            row
            for row in document["subjects"]
            if f"{row['role']}:{row['name']}" != subject
        ],
    }


# ---------------------------------------------------------------------------
# admission: the manifest is derived, never typed
# ---------------------------------------------------------------------------


def test_admission_derives_the_manifest_from_the_local_bytes(tmp_path):
    tool, policy, declarations = _workspace(tmp_path)
    document = drift_monitor.admit(declarations)

    assert document["version"] == drift_monitor.MONITOR_VERSION
    assert document["manifest"] == {
        "tool": {"checker": deploy.artifact_digest(tool)},
        "policy": {"main": drift.digest(policy.read_bytes())},
    }
    for row in document["subjects"]:
        assert row["path"] == str(Path(row["path"]).absolute())
        assert Path(row["path"]).is_absolute()


def test_admission_refuses_a_subject_it_cannot_measure(tmp_path):
    absent = {
        "role": "tool",
        "name": "gone",
        "kind": "tree",
        "path": str(tmp_path / "gone"),
    }
    with pytest.raises(drift_monitor.DriftMonitorError):
        drift_monitor.admit([absent])

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(drift_monitor.DriftMonitorError):
        drift_monitor.admit(
            [{"role": "tool", "name": "empty", "kind": "tree", "path": str(empty)}]
        )

    target = tmp_path / "target.txt"
    target.write_bytes(b"bytes")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(drift_monitor.DriftMonitorError):
        drift_monitor.admit(
            [{"role": "policy", "name": "link", "kind": "file", "path": str(link)}]
        )


def test_a_misspelt_or_duplicate_declaration_is_refused(tmp_path):
    _tool, _policy, declarations = _workspace(tmp_path)
    with pytest.raises(drift_monitor.DriftMonitorError):
        drift_monitor.admit([*declarations, declarations[0]])
    with pytest.raises(drift_monitor.DriftMonitorError):
        drift_monitor.admit([{**declarations[0], "paht": "/tmp"}])
    with pytest.raises(drift_monitor.DriftMonitorError):
        drift_monitor.admit([{**declarations[0], "kind": "directory"}])
    with pytest.raises(drift_monitor.DriftMonitorError):
        drift_monitor.admit([{**declarations[0], "role": "plugin"}])


# ---------------------------------------------------------------------------
# acceptance 1: an edited subject is drifted within one pass and suspend acts
# ---------------------------------------------------------------------------


def test_an_edited_tool_is_drifted_within_one_pass_and_suspend_is_applied(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")

    first = drift_monitor.run_pass(document, wal_path=w)
    assert first["verdict"] == drift.VERDICT_MATCHED
    assert first["record"] is None, "a matched pass writes no record"
    assert not Path(w).exists()

    (tool / "tool.py").write_bytes(b"tool:checker@2-rewritten\n")

    second = drift_monitor.run_pass(document, wal_path=w)
    assert second["verdict"] == drift.VERDICT_DRIFTED
    assert second["response"] == drift.RESPONSE_SUSPEND
    assert second["applied"] is True
    assert second["unapplied"] is False
    assert second["actuation"]["actuator"] == "suspend"

    halt = estop.read_latch(w + ".estop")
    assert halt is not None and halt["halted"] is True
    assert "drift" in halt["reason"].lower() or "suspend" in halt["reason"].lower()
    assert halt["operator"] == "revl.drift"


def test_the_pass_writes_one_wal_record_a_reader_can_index(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")

    result = drift_monitor.run_pass(
        document, wal_path=w, generation=7, at="2026-01-01T00:00:00+00:00"
    )
    loaded = wal.read_wal(w)
    assert loaded["torn"] is False
    assert loaded["header"]["walVersion"] == wal.WAL_VERSION
    assert loaded["header"]["generation"] == 7

    records = drift.drift_records(loaded["records"])
    assert len(records) == 1
    record = records[0]
    assert record["verdict"] == drift.VERDICT_DRIFTED
    assert record["response"] == drift.RESPONSE_SUSPEND
    assert record["subject"] == "tool:checker"
    assert record["generation"] == 7
    assert record["at"] == "2026-01-01T00:00:00+00:00"
    assert record["counts"]["drifted"] == 1
    assert [row["subject"] for row in record["subjects"]] == ["tool:checker"]
    assert record["actuation"]["applied"] is True
    assert record["monitor"]["version"] == drift_monitor.MONITOR_VERSION
    assert result["record"] == record


def test_a_torn_record_is_sealed_before_the_next_pass_appends(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = tmp_path / "run.wal"
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")
    drift_monitor.run_pass(document, wal_path=str(w))

    # An interrupted append leaves a non-newline-terminated tail (§535).
    with open(w, "a", encoding="utf-8") as handle:
        handle.write('{"record": "drift-detected", "torn": ')

    (tool / "tool.py").write_bytes(b"tool:checker@3\n")
    drift_monitor.run_pass(document, wal_path=str(w))

    loaded = wal.read_wal(str(w))
    assert loaded["torn"] is False
    assert len(drift.drift_records(loaded["records"])) == 2


# ---------------------------------------------------------------------------
# acceptance 2: a subject the reading did not produce is unverified, no response
# ---------------------------------------------------------------------------


def test_a_subject_missing_from_the_reading_is_unverified_with_no_response(tmp_path):
    document, _tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    result = drift_monitor.run_pass(_without(document, "policy:main"), wal_path=w)

    assert result["verdict"] == drift.VERDICT_UNVERIFIED
    assert result["response"] == drift.RESPONSE_NONE
    assert result["applied"] is False
    assert result["unapplied"] is False
    assert not Path(w + ".estop").exists()
    assert not Path(w + ".read-only").exists()

    # It is loud: the record carries the unverified row and why it was not read.
    record = drift.drift_records(wal.read_wal(w)["records"])[0]
    rows = {row["subject"]: row for row in record["subjects"]}
    assert rows["policy:main"]["status"] == drift.STATUS_UNVERIFIED
    unmeasured = {row["subject"]: row for row in record["monitor"]["unmeasured"]}
    assert unmeasured["policy:main"]["status"] == "unverified"
    assert "no local path" in unmeasured["policy:main"]["reason"]
    assert any(
        "unverified: policy:main" in line for line in drift_monitor.render(result)
    )


def test_an_unmeasurable_subject_is_unverified_not_matched(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    # Present but no longer measurable AS A TREE: an empty artifact is refused
    # by deploy's digest, so the monitor has no reading for it.
    (tool / "tool.py").unlink()

    result = drift_monitor.run_pass(document, wal_path=w)
    assert result["verdict"] == drift.VERDICT_UNVERIFIED
    assert result["response"] == drift.RESPONSE_NONE
    assert not Path(w + ".estop").exists()
    row = {row["subject"]: row for row in result["report"]["subjects"]}["tool:checker"]
    assert row["status"] == drift.STATUS_UNVERIFIED
    unmeasured = {row["subject"]: row for row in result["reading"]["unmeasured"]}
    assert "empty" in unmeasured["tool:checker"]["reason"]


def test_a_deleted_path_is_missing_drift_not_a_blind_spot(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    for child in tool.iterdir():
        child.unlink()
    tool.rmdir()

    result = drift_monitor.run_pass(document, wal_path=w)
    assert result["verdict"] == drift.VERDICT_DRIFTED
    row = {row["subject"]: row for row in result["report"]["subjects"]}["tool:checker"]
    assert row["status"] == drift.STATUS_MISSING
    assert result["response"] == drift.RESPONSE_SUSPEND
    assert estop.read_latch(w + ".estop") is not None


# ---------------------------------------------------------------------------
# acceptance 3: no response path widens authority
# ---------------------------------------------------------------------------


def test_the_actuator_refuses_the_responses_that_change_what_runs(tmp_path):
    for response in (drift.RESPONSE_RE_ADMIT, drift.RESPONSE_ROLLBACK):
        with pytest.raises(drift_monitor.DriftActuationError):
            drift_monitor.apply_response(
                response, reason="test", wal_path=str(tmp_path / "run.wal")
            )
    assert not list(tmp_path.glob("*.estop"))
    assert not list(tmp_path.glob("*.read-only"))


def test_a_narrowing_response_never_arms_the_other_latch(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")

    read_only = drift_monitor.run_pass(
        document, wal_path=w, control={"onDrift": drift.RESPONSE_READ_ONLY}
    )
    assert read_only["response"] == drift.RESPONSE_READ_ONLY
    assert read_only["applied"] is True
    assert Path(w + ".read-only").exists()
    assert not Path(w + ".estop").exists()
    assert drift_monitor.read_read_only(w + ".read-only")["readOnly"] is True
    assert estop.read_latch(w + ".estop") is None


def test_a_configured_none_response_removes_no_authority(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")

    result = drift_monitor.run_pass(
        document, wal_path=w, control={"onDrift": drift.RESPONSE_NONE}
    )
    assert result["verdict"] == drift.VERDICT_DRIFTED
    assert result["response"] == drift.RESPONSE_NONE
    assert result["applied"] is False
    assert not Path(w + ".estop").exists()
    assert not Path(w + ".read-only").exists()


def test_a_role_override_narrows_only_that_role(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")

    result = drift_monitor.run_pass(
        document,
        wal_path=w,
        control={
            "onDrift": drift.RESPONSE_SUSPEND,
            "byRole": {"tool": drift.RESPONSE_READ_ONLY},
        },
    )
    assert result["response"] == drift.RESPONSE_READ_ONLY
    assert Path(w + ".read-only").exists()
    assert not Path(w + ".estop").exists()


def test_a_response_it_cannot_carry_out_is_recorded_unapplied(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")

    result = drift_monitor.run_pass(
        document, wal_path=w, control={"onDrift": drift.RESPONSE_RE_ADMIT}
    )
    assert result["response"] == drift.RESPONSE_RE_ADMIT
    assert result["applied"] is False
    assert result["unapplied"] is True
    assert "re-admit" in result["actuation"]["refused"]
    assert not Path(w + ".estop").exists()
    assert not Path(w + ".read-only").exists()

    record = drift.drift_records(wal.read_wal(w)["records"])[0]
    assert record["response"] == drift.RESPONSE_RE_ADMIT
    assert record["actuation"]["applied"] is False
    assert record["actuation"]["refused"]


def test_a_rollback_with_no_pinned_target_is_recorded_not_claimed(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")

    result = drift_monitor.run_pass(
        document, wal_path=w, control={"onDrift": drift.RESPONSE_ROLLBACK}
    )
    # The demanded response is not lost: the record says rollback was required
    # and the actuation says why it could not be carried out.
    assert result["response"] == drift.RESPONSE_ROLLBACK
    assert result["applied"] is False
    assert result["unapplied"] is True
    assert "pinned artifact" in result["actuation"]["refused"]
    record = drift.drift_records(wal.read_wal(w)["records"])[0]
    assert record["response"] == drift.RESPONSE_ROLLBACK
    assert record["actuation"]["applied"] is False
    assert record["actuation"]["refused"]


# ---------------------------------------------------------------------------
# the read-only actuator is honoured by the py runtime, and only narrows
# ---------------------------------------------------------------------------


def test_the_py_runtime_refuses_an_effectful_crossing_while_read_only_is_armed(
    tmp_path, monkeypatch
):
    latch = tmp_path / "run.read-only"
    drift_monitor.arm_read_only(
        str(latch), reason="drift control: tool:checker drifted"
    )
    monkeypatch.setenv(drift_monitor.READ_ONLY_LATCH_ENV, str(latch))
    py_runtime.arm_read_only_latch(None)

    assert py_runtime.read_only_engaged() is True

    ran = []

    @py_runtime.estop_gated("checker.send")
    def effectful():
        ran.append(True)
        return "effect applied"

    with pytest.raises(py_runtime.ReadOnlyRefused) as caught:
        effectful()
    assert ran == []
    assert "checker.send" in str(caught.value)
    assert caught.value.record["reason"] == "drift control: tool:checker drifted"

    # A read is not an effectful crossing, so it is not gated: the narrowing is
    # "no new world-changing crossing", not a second E-Stop.
    def read():
        return "still readable"

    assert read() == "still readable"

    latch.unlink()
    assert py_runtime.read_only_engaged() is False
    assert effectful() == "effect applied"
    assert ran == [True]


def test_the_two_read_only_readers_agree_on_a_malformed_latch(tmp_path, monkeypatch):
    latch = tmp_path / "run.read-only"
    latch.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(drift_monitor.READ_ONLY_LATCH_ENV, str(latch))
    py_runtime.arm_read_only_latch(None)

    assert drift_monitor.read_read_only(str(latch))["readOnly"] is True
    assert py_runtime.read_only_engaged() is True

    latch.unlink()
    assert drift_monitor.read_read_only(str(latch)) is None
    assert py_runtime.read_only_engaged() is False


def test_a_read_only_hold_is_idempotent_and_keeps_the_first_reason(tmp_path):
    latch = str(tmp_path / "run.read-only")
    first = drift_monitor.arm_read_only(latch, reason="first drift")
    second = drift_monitor.arm_read_only(latch, reason="second drift")
    assert first["armed"] is True
    assert second["armed"] is False and second["alreadyArmed"] is True
    assert drift_monitor.read_read_only(latch)["reason"] == "first drift"


def test_render_says_when_an_actuation_is_not_in_force(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")
    result = drift_monitor.run_pass(
        document, wal_path=w, control={"onDrift": drift.RESPONSE_RE_ADMIT}
    )
    text = "\n".join(drift_monitor.render(result))
    assert "NOT in force" in text
    assert "refused" in text


# ---------------------------------------------------------------------------
# fail-closed monitor document and actuator inputs
# ---------------------------------------------------------------------------


def test_a_malformed_control_is_refused_even_when_everything_matches(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = tmp_path / "run.wal"
    with pytest.raises(drift_monitor.DriftMonitorError):
        drift_monitor.run_pass(
            document, wal_path=str(w), control={"onDrfit": "suspend"}
        )
    assert not w.exists(), "a control the monitor cannot read writes no record"

    (tool / "tool.py").write_bytes(b"tool:checker@2\n")
    with pytest.raises(drift_monitor.DriftMonitorError):
        drift_monitor.run_pass(
            document, wal_path=str(w), control={"onDrfit": "suspend"}
        )
    assert not w.exists()
    assert not Path(str(w) + ".estop").exists()
    assert not Path(str(w) + ".read-only").exists()


def test_apply_response_needs_a_latch_to_hold(tmp_path):
    for response in (drift.RESPONSE_READ_ONLY, drift.RESPONSE_SUSPEND):
        with pytest.raises(drift_monitor.DriftActuationError):
            drift_monitor.apply_response(response, reason="test")
    with pytest.raises(drift_monitor.DriftActuationError):
        drift_monitor.arm_read_only("", reason="test")
    with pytest.raises(drift_monitor.DriftActuationError):
        drift_monitor.arm_suspend("", reason="test")


def test_a_torn_header_is_replaced_before_the_record_is_appended(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = tmp_path / "run.wal"
    w.write_text('{"record": "header", "walVersion": 1', encoding="utf-8")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")

    drift_monitor.run_pass(document, wal_path=str(w))
    loaded = wal.read_wal(str(w))
    assert loaded["torn"] is False
    assert loaded["header"]["walVersion"] == wal.WAL_VERSION
    assert len(drift.drift_records(loaded["records"])) == 1


def test_a_suspend_is_not_masked_by_a_read_only_hold(tmp_path, monkeypatch):
    read_only = tmp_path / "run.read-only"
    suspend = tmp_path / "run.estop"
    drift_monitor.arm_read_only(str(read_only), reason="read-only first")
    drift_monitor.arm_suspend(str(suspend), reason="suspend second")
    monkeypatch.setenv(drift_monitor.READ_ONLY_LATCH_ENV, str(read_only))
    py_runtime.arm_estop_latch(str(suspend))

    @py_runtime.estop_gated("checker.send")
    def effectful():
        return "effect applied"

    with pytest.raises(py_runtime.EstopHalted):
        effectful()
    assert py_runtime.estop_engaged() is True


def test_a_read_only_hold_refuses_a_direct_effectful_seam(tmp_path, monkeypatch):
    latch = tmp_path / "run.read-only"
    drift_monitor.arm_read_only(str(latch), reason="direct seam")
    monkeypatch.setenv(drift_monitor.READ_ONLY_LATCH_ENV, str(latch))
    py_runtime.arm_read_only_latch(None)

    fired = []
    deferred = py_runtime._Deferred("svc", "send", [], lambda: fired.append(True))
    with pytest.raises(py_runtime.ReadOnlyRefused):
        deferred.fire()
    assert fired == []
    assert deferred.fired is False, "a refused deferred crossing stays owed"

"""Issue #2758 / roadmap item 469: the drift monitor and its actuators.

:mod:`revl.drift` landed the pure half of the control: the admitted manifest,
the verdict over a re-measurement, the configured response and the WAL record.
It re-hashes nothing and suspends nothing. This file pins the RUNNING half, the
three things the item's Exit line needs:

  * a monitor that re-measures local subjects and builds the reading the verdict
    takes. A tool is an emitted artifact tree (hashed by deploy's own
    `artifact_digest`), a policy is a file (hashed by `drift.digest`). An
    admitted subject whose bytes can no longer be measured as its declared kind
    is reported drifted with its cause, never silently compared as a match;
  * actuators that carry out only the two responses that NARROW authority:
    `read-only` arms a latch the py runtime checks at every effectful crossing,
    and `suspend` arms the item-443 E-Stop latch. `re-admit` and `rollback`
    change what runs, so this slice refuses them by name;
  * one durable `drift-detected` record per non-matched pass, in the WAL shape
    `revl.wal` already reads.

The load-bearing distinctions this file is written to break if they are
weakened:

  * a deleted path is `missing` (a measured absence, drift, suspend by default),
    and an admitted subject that is present but no longer measurable as its
    kind (a symlink swap, an unreadable file, a tree turned into a file, an
    emptied tree) is `drifted`, while an admitted subject with no declaration at
    all is `unverified` (a blind spot, no response by default) - absence of
    evidence is not drift, but a changed kind or readability is;
  * a response is a downgrade: the monitor never fires more than the policy
    granted, never replaces a response it cannot carry out with a different one,
    and never widens authority.
"""

from __future__ import annotations

import importlib.util
import os
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


def _assert_changed_is_drift(result, w, subject, cause):
    """An admitted subject that can no longer be measured as its kind is
    drifted, earns the default `onDrift` (suspend), and carries its cause."""
    assert result["verdict"] == drift.VERDICT_DRIFTED
    row = {row["subject"]: row for row in result["report"]["subjects"]}[subject]
    assert row["status"] == drift.STATUS_DRIFTED
    assert cause in row["reason"]
    assert result["response"] == drift.RESPONSE_SUSPEND
    assert result["applied"] is True
    assert estop.read_latch(w + ".estop") is not None
    unmeasured = {row["subject"]: row for row in result["reading"]["unmeasured"]}
    assert unmeasured[subject]["status"] == "drifted"
    assert cause in unmeasured[subject]["reason"]


def test_an_emptied_tree_is_drifted_not_unverified(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    # Present but no longer measurable AS A TREE: an empty artifact is refused
    # by deploy's digest. Admission measured a non-empty tree, so this is drift.
    (tool / "tool.py").unlink()

    result = drift_monitor.run_pass(document, wal_path=w)
    _assert_changed_is_drift(result, w, "tool:checker", "empty")


def test_a_file_swapped_for_a_symlink_is_drifted(tmp_path):
    document, _tool, policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    # The symlink points at the SAME bytes: the hash would match, which is
    # exactly why the swap must not be read as a blind spot.
    target = tmp_path / "elsewhere.json"
    target.write_bytes(policy.read_bytes())
    policy.unlink()
    policy.symlink_to(target)

    result = drift_monitor.run_pass(document, wal_path=w)
    _assert_changed_is_drift(result, w, "policy:main", "symlink")


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads a mode-000 file, so the subject stays readable",
)
def test_an_unreadable_file_is_drifted(tmp_path):
    document, _tool, policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    policy.chmod(0)
    try:
        result = drift_monitor.run_pass(document, wal_path=w)
    finally:
        policy.chmod(0o644)
    _assert_changed_is_drift(result, w, "policy:main", "cannot be read")


def test_a_tree_replaced_by_a_file_is_drifted(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    data = (tool / "tool.py").read_bytes()
    (tool / "tool.py").unlink()
    tool.rmdir()
    tool.write_bytes(data)

    result = drift_monitor.run_pass(document, wal_path=w)
    _assert_changed_is_drift(result, w, "tool:checker", "tool:checker")


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


def test_a_narrowing_response_never_arms_the_other_latch(tmp_path, monkeypatch):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")
    monkeypatch.setenv(drift_monitor.READ_ONLY_LATCH_ENV, w + ".read-only")

    read_only = drift_monitor.run_pass(
        document,
        wal_path=w,
        control={"onDrift": drift.RESPONSE_READ_ONLY},
        tier="py",
    )
    assert read_only["response"] == drift.RESPONSE_READ_ONLY
    assert read_only["applied"] is True
    assert read_only["actuation"]["armed"] is True
    assert read_only["actuation"]["enforced"] is True
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

    # That a read still proceeds is pinned on a compiled component below
    # (test_a_compiled_read_method_proceeds_under_read_only_and_its_write_refuses).

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
    assert loaded["header"]["generation"] == 1
    assert loaded["header"]["guarantee"] == wal.WAL_GUARANTEE
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


def test_the_header_comes_from_the_one_shared_builder():
    header = wal.header_record(3)
    assert header == {
        "record": "header",
        "walVersion": wal.WAL_VERSION,
        "generation": 3,
        "guarantee": wal.WAL_GUARANTEE,
    }
    with pytest.raises(ValueError):
        wal.header_record(0)


def test_read_only_is_armed_but_not_applied_without_a_named_tier(tmp_path):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")

    result = drift_monitor.run_pass(
        document, wal_path=w, control={"onDrift": drift.RESPONSE_READ_ONLY}
    )
    assert result["applied"] is False
    assert result["unapplied"] is True
    assert result["actuation"]["armed"] is True
    assert result["actuation"]["enforced"] is False
    assert "armed, not enforced" in "\n".join(drift_monitor.render(result))

    # The file is still armed: a host that points its runtime at it later sees
    # the narrowing without needing a second pass.
    assert drift_monitor.read_read_only(w + ".read-only")["readOnly"] is True


def test_every_wired_effectful_seam_refuses_under_read_only(tmp_path, monkeypatch):
    latch = tmp_path / "run.read-only"
    drift_monitor.arm_read_only(str(latch), reason="seam coverage")
    monkeypatch.setenv(drift_monitor.READ_ONLY_LATCH_ENV, str(latch))
    py_runtime.arm_read_only_latch(None)

    frame = py_runtime.Frame(object(), "svc")

    @py_runtime.estop_gated("svc.gated")
    def gated():
        return "ran"

    seams = {
        "extern_emit": lambda: py_runtime.extern_emit(
            object(), "svc.send", lambda: "ran", ()
        ),
        "let-effect": lambda: frame.acquire("step", lambda: 1, lambda v: None),
        "deferred emit": lambda: frame.enqueue_deferred(
            "svc", "send", [], lambda: None
        ),
        "approval crossing": lambda: frame.approval_crossing(
            {"passthrough": True}, "cap", lambda: "ran"
        ),
        "deferred fire": lambda: py_runtime._Deferred(
            "svc", "send", [], lambda: None
        ).fire(),
        "estop_gated": gated,
    }
    for label, call in seams.items():
        with pytest.raises(py_runtime.ReadOnlyRefused):
            call()


# ---------------------------------------------------------------------------
# applied only when the runtime reads the latch the monitor wrote
# ---------------------------------------------------------------------------


def test_read_only_is_not_applied_when_the_runtime_reads_another_latch(
    tmp_path, monkeypatch
):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")
    control = {"onDrift": drift.RESPONSE_READ_ONLY}

    monkeypatch.delenv(drift_monitor.READ_ONLY_LATCH_ENV, raising=False)
    unset = drift_monitor.run_pass(document, wal_path=w, control=control, tier="py")
    assert unset["actuation"]["armed"] is True
    assert unset["actuation"]["enforced"] is False
    assert unset["applied"] is False
    assert unset["unapplied"] is True

    monkeypatch.setenv(
        drift_monitor.READ_ONLY_LATCH_ENV, str(tmp_path / "other.read-only")
    )
    other = drift_monitor.run_pass(document, wal_path=w, control=control, tier="py")
    assert other["applied"] is False
    assert other["actuation"]["enforced"] is False

    monkeypatch.setenv(drift_monitor.READ_ONLY_LATCH_ENV, w + ".read-only")
    named = drift_monitor.run_pass(document, wal_path=w, control=control, tier="py")
    assert named["applied"] is True
    assert named["actuation"]["enforced"] is True


def test_read_only_follows_the_runtime_explicit_latch_over_the_ambient_one(
    tmp_path, monkeypatch
):
    document, tool, _policy = _document(tmp_path)
    w = str(tmp_path / "run.wal")
    (tool / "tool.py").write_bytes(b"tool:checker@2\n")
    monkeypatch.setenv(drift_monitor.READ_ONLY_LATCH_ENV, w + ".read-only")
    # The runtime in this process is armed on a different path, so it never
    # reads the latch the monitor writes, whatever the environment says.
    py_runtime.arm_read_only_latch(str(tmp_path / "explicit.read-only"))
    result = drift_monitor.run_pass(
        document,
        wal_path=w,
        control={"onDrift": drift.RESPONSE_READ_ONLY},
        tier="py",
    )
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# a remote call through the placement bridge honours the hold
# ---------------------------------------------------------------------------


def test_a_remote_bridge_call_is_refused_under_read_only_before_it_is_encoded(
    tmp_path, monkeypatch
):
    import bridge

    latch = tmp_path / "run.read-only"
    drift_monitor.arm_read_only(str(latch), reason="remote row")
    monkeypatch.setenv(drift_monitor.READ_ONLY_LATCH_ENV, str(latch))
    py_runtime.arm_read_only_latch(None)

    # This client has no socket, and an opaque argument raises SeamMarshalError
    # at encode: reaching either would fail differently from the refusal.
    client = object.__new__(bridge._Client)
    with pytest.raises(py_runtime.ReadOnlyRefused, match="db.execute"):
        client.call("db", "execute", [object()])

    # A suspend is never masked by the read-only hold.
    py_runtime.estop("drill", operator="ops")
    with pytest.raises(py_runtime.EstopHalted):
        client.call("db", "execute", [object()])


# ---------------------------------------------------------------------------
# a compiled component: reads proceed, writes refuse
# ---------------------------------------------------------------------------


_COMPILED = """
extern emission fn record(t: Str) -> Unit = @py {
    import os
    with open(os.environ['REVL_RO_COUNT'], 'a', encoding='utf-8') as f:
        f.write('record' + chr(10))
    return None
}
service Ledger {
  fn peek(t: Str) -> Str
  emission fn write(t: Str)
}
component Book provides ledger: Ledger {
  provide ledger {
    fn peek(t) { return t }
    fn write(t) { emit record(t) }
  }
}
"""


@pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="drives a live cordis-py composition; install the pinned fork with "
    "`sh backends/python/setup.sh`",
)
def test_a_compiled_read_method_proceeds_under_read_only_and_its_write_refuses(
    tmp_path, monkeypatch
):
    from revl.compiler import compile_source
    from revl.mcp.session import Session

    count = tmp_path / "count.log"
    monkeypatch.setenv("REVL_RO_COUNT", str(count))
    latch = tmp_path / "run.read-only"
    session = Session()
    session.load(compile_source(_COMPILED, "read_only_2758.rvl"))
    runtime = session._driver.runtime
    try:
        runtime.arm_read_only_latch(str(latch))
        drift_monitor.arm_read_only(str(latch), reason="compiled read")
        assert runtime.read_only_engaged() is True

        # The read goes through the same compiled dispatch as the write, so a
        # hold that refused reads would raise here.
        assert session.call("ledger", "peek", ["page"])["result"] == "page"

        with pytest.raises(runtime.ReadOnlyRefused, match="record"):
            session.call("ledger", "write", ["entry"])
        assert not count.exists(), "the refused write ran its host body"

        latch.unlink()
        session.call("ledger", "write", ["entry"])
        assert count.read_text(encoding="utf-8").splitlines() == ["record"]
    finally:
        runtime.arm_read_only_latch(None)
        session._reset()

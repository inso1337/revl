"""Roadmap item 469 / issue #821: enforced policy-drift control.

Item 469 asks for a control that keeps the identity admission established true
AFTER admission: a live system is the admitted one exactly while every admitted
subject - model, tool, policy, dependency, peer - still hashes to the value
admission bound. The slice this file pins is the pure half of that control (see
docs/design/469-enforced-policy-drift-control.md): the admitted identity
manifest, the verdict over a re-measurement of it, the response the configured
policy fires, and the durable record of the decision.

The properties that matter, and that this file is written to break if they are
weakened:

  * a re-measured subject that does not hash to its admitted digest is `drifted`
    and fires the configured response;
  * a subject the admitted manifest names that the live reading did NOT produce
    is `unverified`, never `matched`, and fires no response unless the control
    configures one: the reason to remove authority has to be a measured
    mismatch, not a blind spot (the posture item 477's liveness gate takes);
  * a response is a downgrade: it is the strongest response the configured
    policy grants over the drift set and never more, so a drifted subject cannot
    earn authority nobody configured;
  * `rollback` without a pinned target is refused rather than downgraded.

The monitor that re-measures live subjects, the actuators that carry a response
out, and the CLI verb that reads the record back are the follow-up stages named
in the design note. Nothing here reads a clock or a filesystem except the WAL
round trip, so the whole surface is testable with no runtime.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import drift  # noqa: E402
from revl import wal as w  # noqa: E402


MODEL = drift.digest(b"model:revl-mini@1")
TOOL = drift.digest(b"tool:checker@1")
TRACER = drift.digest(b"tool:tracer@1")
PEER = drift.digest(b"peer:gateway@1")

ADMITTED = {
    "model": {"revl-mini": MODEL},
    "tool": {"checker": TOOL, "tracer": TRACER},
    "peer": {"gateway": PEER},
}

OTHER_MODEL = drift.digest(b"model:revl-mini@2-rewritten")
OTHER_PEER = drift.digest(b"peer:other@1")
PINNED = drift.digest(b"pinned-artifact-tree")


def live(*changed):
    """The admitted manifest re-measured, with `changed` applied.

    Each entry is `(role, name, value)`. `value=None` is the reading that
    reports the subject absent; a name the manifest does not hold is a subject
    the live composition has and admission never named; a subject nobody
    changed is measured at its admitted digest."""
    out = {role: dict(subjects) for role, subjects in ADMITTED.items()}
    for role, name, value in changed:
        out.setdefault(role, {})[name] = value
    return out


def entry(report, subject):
    for row in report["subjects"]:
        if row["subject"] == subject:
            return row
    raise AssertionError(f"{subject} is not in the report: {report['subjects']}")


def rank(response):
    return drift._RANK[response]


# ---------------------------------------------------------------------------
# the verdict over a re-measurement of the admitted identity
# ---------------------------------------------------------------------------


def test_the_admitted_identity_verifies_as_matched():
    report = drift.detect(ADMITTED, live())
    assert report["verdict"] == drift.VERDICT_MATCHED
    assert report["counts"] == {
        "matched": 4,
        "drifted": 0,
        "missing": 0,
        "unexpected": 0,
        "unverified": 0,
    }
    assert {row["status"] for row in report["subjects"]} == {drift.STATUS_MATCHED}
    assert drift.detect(ADMITTED, live())["guarantee"] == drift.DRIFT_GUARANTEE


def test_an_induced_hash_drift_is_detected():
    report = drift.detect(ADMITTED, live(("model", "revl-mini", OTHER_MODEL)))
    assert report["verdict"] == drift.VERDICT_DRIFTED
    row = entry(report, "model:revl-mini")
    assert row["status"] == drift.STATUS_DRIFTED
    assert row["admitted"] == MODEL
    assert row["live"] == OTHER_MODEL
    # the subjects nobody touched are still the admitted ones
    assert entry(report, "tool:checker")["status"] == drift.STATUS_MATCHED


def test_an_absent_admitted_subject_is_missing_not_unverified():
    report = drift.detect(ADMITTED, live(("tool", "checker", None)))
    row = entry(report, "tool:checker")
    assert row["status"] == drift.STATUS_MISSING
    assert row["live"] is None
    assert report["verdict"] == drift.VERDICT_DRIFTED


def test_a_live_subject_admission_never_named_is_unexpected():
    report = drift.detect(
        ADMITTED, live(("peer", "sidecar", drift.digest(b"peer:sidecar@1")))
    )
    row = entry(report, "peer:sidecar")
    assert row["status"] == drift.STATUS_UNEXPECTED
    assert row["admitted"] is None
    assert report["verdict"] == drift.VERDICT_DRIFTED


def test_a_subject_the_reading_did_not_produce_is_unverified_and_never_matched():
    partial = live()
    del partial["tool"]["tracer"]
    report = drift.detect(ADMITTED, partial)
    assert report["verdict"] == drift.VERDICT_UNVERIFIED
    row = entry(report, "tool:tracer")
    assert row["status"] == drift.STATUS_UNVERIFIED
    assert row["live"] is None
    # absent evidence is not a measured mismatch, and it fires no response
    assert drift.resolve(report)["response"] == drift.RESPONSE_NONE
    # but nothing about that subject is reported as verified either
    assert report["counts"][drift.STATUS_MATCHED] == 3


def test_a_malformed_live_reading_is_unverified_with_its_cause():
    report = drift.detect(ADMITTED, live(("tool", "checker", "0xdeadbeef")))
    row = entry(report, "tool:checker")
    assert row["status"] == drift.STATUS_UNVERIFIED
    assert row["status"] != drift.STATUS_DRIFTED
    assert "0xdeadbeef" in row["reason"]
    assert report["verdict"] == drift.VERDICT_UNVERIFIED


def test_a_missing_live_reading_leaves_every_admitted_subject_unverified():
    report = drift.detect(ADMITTED)
    assert report["verdict"] == drift.VERDICT_UNVERIFIED
    assert report["counts"][drift.STATUS_DRIFTED] == 0
    assert report["counts"][drift.STATUS_UNVERIFIED] == 4


# ---------------------------------------------------------------------------
# the response the configured control fires
# ---------------------------------------------------------------------------


def test_a_drift_fires_the_suspend_default():
    report = drift.detect(ADMITTED, live(("peer", "gateway", OTHER_PEER)))
    decision = drift.resolve(report)
    assert decision["response"] == drift.RESPONSE_SUSPEND
    assert decision["trigger"] == "drift"
    assert decision["subject"] == "peer:gateway"


def test_an_unverified_subject_fires_no_response_by_default_but_may_be_configured_to():
    partial = live()
    del partial["model"]["revl-mini"]
    report = drift.detect(ADMITTED, partial)
    assert drift.resolve(report)["response"] == drift.RESPONSE_NONE
    configured = drift.resolve(report, {"onUnverified": drift.RESPONSE_RE_ADMIT})
    assert configured["response"] == drift.RESPONSE_RE_ADMIT
    assert configured["trigger"] == "unverified"
    assert configured["subject"] == "model:revl-mini"
    # an unverified subject never earns the drift default
    assert configured["response"] != drift.DEFAULT_RESPONSE


def test_a_per_role_override_fires_only_for_that_role():
    policy = {
        "onDrift": drift.RESPONSE_SUSPEND,
        "byRole": {"peer": drift.RESPONSE_READ_ONLY},
    }
    report = drift.detect(
        ADMITTED,
        live(("model", "revl-mini", OTHER_MODEL), ("peer", "gateway", OTHER_PEER)),
    )
    decision = drift.resolve(report, policy)
    fired = {row["subject"]: row["response"] for row in decision["subjects"]}
    assert fired == {
        "model:revl-mini": drift.RESPONSE_SUSPEND,
        "peer:gateway": drift.RESPONSE_READ_ONLY,
    }
    # the effective response is the strongest one, and the decisive subject is
    # the first that holds it
    assert decision["response"] == drift.RESPONSE_SUSPEND
    assert decision["subject"] == "model:revl-mini"


def test_the_effective_response_is_the_strongest_over_the_drift_set():
    policy = {
        "onDrift": drift.RESPONSE_READ_ONLY,
        "byRole": {"peer": drift.RESPONSE_SUSPEND},
    }
    report = drift.detect(
        ADMITTED,
        live(("model", "revl-mini", OTHER_MODEL), ("peer", "gateway", OTHER_PEER)),
    )
    decision = drift.respond(report, policy)
    assert decision["response"] == drift.RESPONSE_SUSPEND
    assert decision["subject"] == "peer:gateway"


def test_a_response_never_exceeds_what_the_policy_grants():
    policies = [
        None,
        {"onDrift": drift.RESPONSE_READ_ONLY},
        {"onDrift": drift.RESPONSE_RE_ADMIT, "onUnverified": drift.RESPONSE_SUSPEND},
        {"onDrift": drift.RESPONSE_ROLLBACK, "byRole": {"peer": drift.RESPONSE_NONE}},
        {
            "byRole": {"model": drift.RESPONSE_READ_ONLY},
            "onUnverified": drift.RESPONSE_ROLLBACK,
        },
        {"onDrift": drift.RESPONSE_NONE, "onUnverified": drift.RESPONSE_NONE},
    ]
    reports = [
        drift.detect(ADMITTED, live()),
        drift.detect(ADMITTED, live(("peer", "gateway", OTHER_PEER))),
        drift.detect(ADMITTED, live(("model", "revl-mini", OTHER_MODEL))),
        drift.detect(ADMITTED, live(("tool", "checker", None))),
        drift.detect(
            ADMITTED, live(("peer", "sidecar", drift.digest(b"peer:sidecar@1")))
        ),
        drift.detect(ADMITTED, live(("tool", "checker", "not-a-digest"))),
        drift.detect(ADMITTED),
        drift.detect(
            ADMITTED,
            live(
                ("model", "revl-mini", OTHER_MODEL),
                ("peer", "gateway", None),
                ("peer", "sidecar", drift.digest(b"peer:sidecar@1")),
            ),
        ),
    ]
    for policy in policies:
        config = drift.normalize_policy(policy)
        ceiling = max(
            [rank(config["onDrift"]), rank(config["onUnverified"])]
            + [rank(value) for value in config["byRole"].values()]
        )
        for report in reports:
            # with a pinned target, so a rollback decision can be carried out and
            # the bound is what is under test rather than the refusal
            decision = drift.resolve(report, policy, rollback_to=PINNED)
            assert decision["response"] in drift.RESPONSES
            assert rank(decision["response"]) <= ceiling, (policy, report["verdict"])
            if decision["subject"] is not None:
                decisive = {row["subject"]: row for row in decision["subjects"]}
                assert rank(decisive[decision["subject"]]["response"]) == rank(
                    decision["response"]
                )
                assert all(
                    rank(row["response"]) <= rank(decision["response"])
                    for row in decision["subjects"]
                )


def test_nothing_drifted_so_the_control_grants_nothing():
    # a control configured to roll back grants nothing when nothing drifted, so
    # authority can only go back up through a fresh admission
    decision = drift.resolve(
        drift.detect(ADMITTED, live()), {"onDrift": drift.RESPONSE_ROLLBACK}
    )
    assert decision["response"] == drift.RESPONSE_NONE
    assert decision["subjects"] == []
    assert decision["trigger"] == "none"
    assert decision["subject"] is None


def test_a_rollback_without_a_pinned_target_is_refused():
    report = drift.detect(ADMITTED, live(("peer", "gateway", OTHER_PEER)))
    policy = {"onDrift": drift.RESPONSE_ROLLBACK}
    with pytest.raises(drift.DriftError) as excinfo:
        drift.resolve(report, policy)
    assert "no pinned artifact digest" in str(excinfo.value)
    # the decision itself still reports what the policy configured
    assert drift.respond(report, policy)["response"] == drift.RESPONSE_ROLLBACK


def test_a_rollback_with_a_pinned_target_is_allowed():
    report = drift.detect(ADMITTED, live(("peer", "gateway", OTHER_PEER)))
    pinned = drift.digest(b"pinned-artifact-tree")
    decision = drift.resolve(
        report, {"onDrift": drift.RESPONSE_ROLLBACK}, rollback_to=pinned
    )
    assert decision["response"] == drift.RESPONSE_ROLLBACK


def test_a_pinned_target_that_is_not_a_digest_is_refused():
    report = drift.detect(ADMITTED, live(("peer", "gateway", OTHER_PEER)))
    for bad in (None, "", "abc", 12):
        with pytest.raises(drift.DriftError):
            drift.resolve(report, {"onDrift": drift.RESPONSE_ROLLBACK}, rollback_to=bad)


def test_a_rollback_is_not_demanded_when_the_policy_does_not_fire_it():
    report = drift.detect(ADMITTED, live(("peer", "gateway", OTHER_PEER)))
    decision = drift.resolve(report, {"onDrift": drift.RESPONSE_SUSPEND})
    assert decision["response"] == drift.RESPONSE_SUSPEND


# ---------------------------------------------------------------------------
# fail closed on a manifest or a policy that cannot be read as what it claims
# ---------------------------------------------------------------------------


def test_a_manifest_outside_the_drift_vocabulary_is_refused():
    with pytest.raises(drift.DriftError) as excinfo:
        drift.detect({"container": {"box": MODEL}})
    assert "not one of the drift roles" in str(excinfo.value)


def test_a_manifest_row_that_is_not_a_digest_is_refused():
    with pytest.raises(drift.DriftError):
        drift.detect({"model": {"revl-mini": "latest"}})
    with pytest.raises(drift.DriftError):
        drift.detect({"model": {"revl-mini": MODEL.upper()}})
    with pytest.raises(drift.DriftError):
        drift.detect({"model": "revl-mini"})
    with pytest.raises(drift.DriftError):
        drift.detect(["model"])
    with pytest.raises(drift.DriftError):
        drift.detect(ADMITTED, ["peer"])


def test_a_policy_this_control_does_not_read_is_refused():
    with pytest.raises(drift.DriftError) as excinfo:
        drift.normalize_policy({"onDrfit": drift.RESPONSE_SUSPEND})
    assert "is not read by this control" in str(excinfo.value)
    with pytest.raises(drift.DriftError):
        drift.normalize_policy({"onDrift": "halt"})
    with pytest.raises(drift.DriftError):
        drift.normalize_policy({"byRole": {"container": drift.RESPONSE_SUSPEND}})
    with pytest.raises(drift.DriftError):
        drift.normalize_policy({"byRole": "peer"})
    with pytest.raises(drift.DriftError):
        drift.normalize_policy("suspend")


def test_the_default_policy_is_the_fail_closed_one():
    assert drift.normalize_policy() == {
        "version": drift.DRIFT_CONTROL_VERSION,
        "onDrift": drift.RESPONSE_SUSPEND,
        "onUnverified": drift.RESPONSE_NONE,
        "byRole": {},
    }
    assert drift.DEFAULT_RESPONSE == drift.RESPONSE_SUSPEND
    assert drift.DEFAULT_UNVERIFIED_RESPONSE == drift.RESPONSE_NONE


def test_the_response_order_is_the_item_vocabulary():
    assert drift.RESPONSES == (
        drift.RESPONSE_NONE,
        drift.RESPONSE_READ_ONLY,
        drift.RESPONSE_RE_ADMIT,
        drift.RESPONSE_SUSPEND,
        drift.RESPONSE_ROLLBACK,
    )
    assert drift.ROLES == ("model", "tool", "policy", "dependency", "peer")
    assert drift.DRIFT_CONTROL_VERSION == 1
    assert drift.RECORD_DRIFT_DETECTED == "drift-detected"


# ---------------------------------------------------------------------------
# the durable record of the decision
# ---------------------------------------------------------------------------


def test_the_record_names_the_verdict_the_response_and_the_policy():
    report = drift.detect(
        ADMITTED,
        live(("peer", "gateway", OTHER_PEER), ("tool", "checker", None)),
    )
    decision = drift.resolve(report, {"onDrift": drift.RESPONSE_READ_ONLY})
    record = drift.wal_record(report, decision, generation=3, at="2026-02-01T00:00:00Z")
    assert record["record"] == drift.RECORD_DRIFT_DETECTED
    assert record["verdict"] == drift.VERDICT_DRIFTED
    assert record["response"] == drift.RESPONSE_READ_ONLY
    assert record["subject"] == "peer:gateway"
    assert record["generation"] == 3
    assert record["at"] == "2026-02-01T00:00:00Z"
    assert record["policy"] == decision["policy"]
    assert record["counts"] == report["counts"]
    assert [row["subject"] for row in record["subjects"]] == [
        "peer:gateway",
        "tool:checker",
    ]
    assert record["subjects"][0]["live"] == OTHER_PEER
    # a matched subject is not evidence of drift and is not recorded as any
    assert "model:revl-mini" not in json.dumps(record)
    assert record["guarantee"] == drift.DRIFT_GUARANTEE


def test_the_record_leaves_the_policy_that_was_in_force_recoverable():
    report = drift.detect(ADMITTED, live(("peer", "gateway", OTHER_PEER)))
    decision = drift.resolve(report, {"byRole": {"peer": drift.RESPONSE_RE_ADMIT}})
    record = drift.wal_record(report, decision)
    assert record["policy"]["byRole"] == {"peer": drift.RESPONSE_RE_ADMIT}
    assert record["policy"]["onDrift"] == drift.RESPONSE_SUSPEND
    assert record["at"] is None
    assert record["generation"] is None


def test_the_ordinary_wal_reader_reads_the_record_back(tmp_path):
    report = drift.detect(ADMITTED, live(("peer", "gateway", OTHER_PEER)))
    record = drift.wal_record(report, drift.resolve(report), generation=1)
    path = tmp_path / "wal.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "record": "header",
                    "walVersion": w.WAL_VERSION,
                    "generation": 1,
                    "guarantee": w.WAL_GUARANTEE,
                },
                record,
                {"record": "activation-complete"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = w.read_wal(str(path))
    assert loaded["header"]["walVersion"] == w.WAL_VERSION
    assert loaded["complete"] is True
    assert loaded["torn"] is False
    assert drift.drift_records(loaded["records"]) == [record]
    # the new kind is indexed by its own reader and is invisible to the existing
    # one, so recovery's view of the WAL does not change
    assert w.model_decisions(loaded["records"]) == {}


def test_the_record_history_is_in_recorded_order():
    partial = live()
    del partial["peer"]["gateway"]
    quiet = drift.detect(ADMITTED, partial)
    loud = drift.detect(ADMITTED, live(("peer", "gateway", OTHER_PEER)))
    records = [
        {"record": "header"},
        drift.wal_record(quiet, drift.resolve(quiet)),
        drift.wal_record(loud, drift.resolve(loud)),
        {"record": "activation-complete"},
    ]
    assert [row["verdict"] for row in drift.drift_records(records)] == [
        drift.VERDICT_UNVERIFIED,
        drift.VERDICT_DRIFTED,
    ]
    assert drift.drift_records([{"record": "header"}]) == []
    assert drift.drift_records([{"record": "model-decision", "component": "x"}]) == []


def test_the_render_names_every_subject_and_the_response():
    report = drift.detect(ADMITTED, live(("peer", "gateway", OTHER_PEER)))
    lines = drift.render(report, drift.resolve(report))
    assert lines[0] == "drift: drifted"
    assert any(line.startswith("  peer:gateway: drifted") for line in lines)
    assert any(line.startswith("  model:revl-mini: matched") for line in lines)
    assert any(line.startswith("response: suspend") for line in lines)
    assert any("matched 3, drifted 1, missing 0" in line for line in lines)
    assert len(drift.render(report)) == len(lines) - 1

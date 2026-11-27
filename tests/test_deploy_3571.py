"""Issue #3571: `read_decision` must compare federation and generation, and a
`revl deploy` without `--bundle` must SAY when it skips the participant order
refusal while still writing its decision record.

Two behaviour changes:

* `read_decision(path, *, federation_id=None, generation=None)` only lets a
  decision-shaped record decide when it names the run being settled. A
  stranded participant of an EARLIER run (its WAL's latest opening carries the
  earlier generation) is no longer settled by a LATER run's record.
  `settle_stranded` derives the expected generation from the WAL's latest
  opening (`generation`/`header` boundary record, else the header).
* `revl deploy` without `--bundle` has no IR, so the issue-#2725 participant
  order refusal cannot run; the COMMIT path now prints a stderr warning instead
  of staying silent (refusing would break the base no-bundle flow; the
  decision record it writes keeps crash settlement working).
"""

import argparse
import json
from pathlib import Path

from revl import deploy


def _write_lines(path, *records):
    Path(path).write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8")


def _stranded_wal(path, generation, *, later_opening=None):
    """A participant WAL stranded mid-update, latest opening at `generation`.

    With `later_opening` set, the WAL was reused (#3147): an earlier header at
    `generation`, then a `generation` boundary record opening
    `later_opening` — the run a stranded participant of THAT opening belongs
    to."""
    records = [{"record": "header", "walVersion": 1, "generation": generation,
                "guarantee": "test"}]
    if later_opening is not None:
        records.append({"record": "generation", "generation": later_opening})
    records.append({"record": "discharge-descriptor", "seq": 1,
                    "entry": "transactional",
                    "call": {"receiver": "db", "method": "removeRow",
                             "args": ["r1"]},
                    "origin": "Billing"})
    _write_lines(path, *records)


# --- read_decision: the constraints pick WHICH record decides ---------------


def test_read_decision_unconstrained_keeps_last_record_wins(tmp_path):
    decision = str(tmp_path / "decision.jsonl")
    deploy.write_commit_approval(decision, federation_id="fed", generation=1,
                                 participants=["a"])
    deploy.write_decision_started(decision, federation_id="fed", generation=2)
    kind, record = deploy.read_decision(decision)
    assert kind is None                      # the pending record clears approval
    assert record["record"] == deploy.FEDERATION_PENDING


def test_read_decision_constrains_by_generation(tmp_path):
    decision = str(tmp_path / "decision.jsonl")
    deploy.write_commit_approval(decision, federation_id="fed", generation=1,
                                 participants=["a"])
    deploy.write_decision_started(decision, federation_id="fed", generation=2)
    # settling the generation-1 run: the later generation-2 pending does NOT
    # clear it — the generation-1 approval is the last record OF THAT RUN
    kind, record = deploy.read_decision(decision, generation=1)
    assert kind == deploy.FEDERATION_APPROVED
    assert record["generation"] == 1
    # settling the generation-2 run: its own pending decides "nothing yet"
    kind, record = deploy.read_decision(decision, generation=2)
    assert kind is None
    assert record["record"] == deploy.FEDERATION_PENDING
    # a generation no record names: no decision, the definite roll-back answer
    assert deploy.read_decision(decision, generation=3) == (None, None)


def test_read_decision_constrains_by_federation(tmp_path):
    decision = str(tmp_path / "decision.jsonl")
    deploy.write_commit_approval(decision, federation_id="fed-a", generation=1,
                                 participants=["a"])
    deploy.write_abort_decision(decision, federation_id="fed-b", generation=1,
                                reason="peer failed", failed="b")
    kind, _ = deploy.read_decision(decision, federation_id="fed-a")
    assert kind == deploy.FEDERATION_APPROVED
    kind, _ = deploy.read_decision(decision, federation_id="fed-b")
    assert kind == deploy.FEDERATION_ABORTED
    assert deploy.read_decision(decision, federation_id="fed-c") == (None, None)


def test_read_decision_skips_a_record_whose_generation_is_not_an_int(tmp_path):
    decision = str(tmp_path / "decision.jsonl")
    _write_lines(decision, {"record": deploy.FEDERATION_APPROVED,
                           "federation": "fed", "generation": "soon"})
    assert deploy.read_decision(decision, generation=1) == (None, None)


# --- _wal_latest_generation: the run a stranded participant belongs to -------


def test_wal_latest_generation_reads_the_last_opening(tmp_path):
    wal = str(tmp_path / "p.wal")
    _stranded_wal(wal, 1)
    assert deploy._wal_latest_generation(wal) == 1
    _stranded_wal(wal, 1, later_opening=2)
    assert deploy._wal_latest_generation(wal) == 2
    assert deploy._wal_latest_generation(str(tmp_path / "missing.wal")) is None


# --- settle_stranded: a later run's record must not settle an earlier run -----


def test_a_later_runs_approval_does_not_settle_an_earlier_stranded_participant(
        tmp_path):
    wal = str(tmp_path / "p.wal")
    decision = str(tmp_path / "decision.jsonl")
    _stranded_wal(wal, generation=1)          # this participant is the gen-1 run
    deploy.write_commit_approval(decision, federation_id="fed", generation=2,
                                 participants=["billing"])   # a LATER run's record
    verdict = deploy.settle_stranded(wal, decision)
    assert verdict["verdict"] == "rolled-back", verdict
    assert verdict["federationDecision"] == "none"


def test_the_same_later_runs_record_settles_that_runs_stranded_participant(
        tmp_path):
    wal = str(tmp_path / "p.wal")
    decision = str(tmp_path / "decision.jsonl")
    _stranded_wal(wal, generation=1, later_opening=2)   # reused WAL, latest = 2
    deploy.write_commit_approval(decision, federation_id="fed", generation=2,
                                 participants=["billing"])
    verdict = deploy.settle_stranded(wal, decision)
    assert verdict["verdict"] == "rolled-forward", verdict
    assert verdict["federationDecision"] == deploy.FEDERATION_APPROVED


def test_a_same_generation_pending_still_clears_the_approval(tmp_path):
    wal = str(tmp_path / "p.wal")
    decision = str(tmp_path / "decision.jsonl")
    _stranded_wal(wal, generation=1)
    deploy.write_commit_approval(decision, federation_id="fed", generation=1,
                                 participants=["billing"])
    deploy.write_decision_started(decision, federation_id="fed", generation=1)
    verdict = deploy.settle_stranded(wal, decision)
    assert verdict["verdict"] == "rolled-back", verdict
    assert verdict["federationDecision"] == "none"


# --- deploy without --bundle: warn, do not stay silent ------------------------


def _deploy_args(map_path, **overrides):
    args = dict(map=str(map_path), dry_run=False, json=True, bundle=None,
                key=None, backend="python", require_gauntlet=False,
                require_conformance=False, ssh_exe=None, scp_exe=None)
    args.update(overrides)
    return argparse.Namespace(**args)


_APPLIED = {"protocol": deploy.PROTOCOL, "verdict": deploy.DEPLOY_APPLIED,
            "phase": "commit", "participants": {}, "commitLedger": [],
            "residue": {"clean": True, "outstanding": [], "proof": "test"}}


def _minimal_map(tmp_path):
    map_path = tmp_path / "map.toml"
    map_path.write_text('[processes.p1]\ncomponents = ["c1"]\n',
                        encoding="utf-8")
    return map_path


def test_deploy_without_bundle_warns_that_the_order_refusal_is_skipped(
        tmp_path, monkeypatch, capsys):
    from revl import wal as wal_module
    monkeypatch.setattr(wal_module, "default_wal_dir",
                        lambda: str(tmp_path / "wal"))
    monkeypatch.setattr(deploy, "deploy_local_map", lambda *a, **k: _APPLIED)
    rc = deploy.deploy_command(_deploy_args(_minimal_map(tmp_path)))
    assert rc == 0
    err = capsys.readouterr().err
    assert "without --bundle" in err
    assert "order refusal" in err
    # the decision record is still written: crash settlement keeps working
    decision = tmp_path / "wal" / "deploy" / "map" / "0" / "decision.jsonl"
    kind, record = deploy.read_decision(str(decision))
    assert kind is None
    assert record["record"] == deploy.FEDERATION_PENDING


def test_deploy_with_an_admitted_bundle_does_not_warn(
        tmp_path, monkeypatch, capsys):
    from revl import participant_classes as pc
    from revl import wal as wal_module
    monkeypatch.setattr(wal_module, "default_wal_dir",
                        lambda: str(tmp_path / "wal"))
    monkeypatch.setattr(deploy, "admit_bundle_chain",
                        lambda *a, **k: {"verdict": deploy.ACCEPT,
                                         "backend": "python",
                                         "artifact_hash": "0" * 16,
                                         "link": "signer"})
    monkeypatch.setattr(deploy, "_read_bundle_ir", lambda bundle: {})
    monkeypatch.setattr(pc, "plan_refusals", lambda ir, placement: [])
    monkeypatch.setattr(pc, "participant_classes",
                        lambda ir, placement: {})
    monkeypatch.setattr(deploy, "deploy_local_map", lambda *a, **k: _APPLIED)
    rc = deploy.deploy_command(_deploy_args(_minimal_map(tmp_path),
                                            bundle=str(tmp_path / "b")))
    assert rc == 0
    assert "order refusal" not in capsys.readouterr().err

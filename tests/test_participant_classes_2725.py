"""Typed participant classes and one durable decision for a multi-node step
(issue #2725).

Four properties are pinned:

1. **One class per participant.** A deploy map names which components a
   participant process holds; each participant's class is derived from the
   declarations those components' crossings already carry. The test covers all
   five named classes (`prepare-reversible`, `deferred-irreversible`,
   `recoverable-forward`, `compensable`, `irreversible-needs-approval`) plus
   the bare `irreversible` fallback.
2. **One plan-time refusal.** An `irreversible-needs-approval` participant
   ordered before a participant that can still fail is refused, and the refusal
   names both.
3. **One report.** `revl audit --deploy-map` prints the class per participant
   and refuses the same unsafe plan (the reporting surface and the gate read
   the same derivation).
4. **One durable decision.** `revl deploy` writes `decision.jsonl` next to its
   state and `revl recover --decision` reads it: a commit-approved record rolls
   a stranded participant forward, an abort/pending/absent record rolls it
   back, and an unreadable record fails closed rather than guessing.
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from revl import deploy, participant_classes as pc  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.compiler import compile_source  # noqa: E402


SOURCE = """\
type H = { id: Int }
extern acquire fn open_h(p: Str) -> H undo close_h(result) = @py { return {"id": 1} }
extern pure fn close_h(h: H) -> Unit = @py { return }
extern emission fn rev(x: Str) -> Str compensate rollback() = @py { return x }
extern pure fn rollback() -> Unit = @py { return }
extern emission deferred fn hold(x: Str) -> Unit = @py { return None }
extern witnessed fn witness_me(p: Str) -> Result[Str, Str] undo release(result) = @py { return Ok(p) }
extern pure fn release(w: Str) -> Unit = @py { return }
extern emission fn charge(n: Int) = @py { return }
extern emission fn gate(n: Int) requires approval = @py { return }
service Ops { fn ping() -> Int }
component Open { let h = effect open_h("p") undo close_h(h) }
component Rev { emit rev("x") compensate rollback() }
component Hold { emit hold("x") }
component Wit { let r = effect witness_me("p") }
component Plain { emit charge(1) }
component Gate provides ops: Ops {
  let a = await approval["gate"] { amount: 1 }
  emit gate(2) with a
  provide ops { fn ping() = 1 }
}
"""


def _ir(tmp_path, name="app.rvl"):
    return compile_source(SOURCE, str(tmp_path / name))


def _placement(**processes):
    return {"processes": {name: {"components": list(components)}
                          for name, components in processes.items()}}


def _map(tmp_path, placement, name="deploy.toml"):
    """A deploy map in the TOML shape `revl deploy` and `revl audit` read."""
    lines = []
    for process, conf in placement["processes"].items():
        lines.append(f"[processes.{process}]")
        lines.append("components = ["
                     + ", ".join(json.dumps(c) for c in conf["components"])
                     + "]")
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. one class per participant, derived from declarations
# ---------------------------------------------------------------------------


def test_each_of_the_five_classes_is_derived(tmp_path):
    ir = _ir(tmp_path)
    placement = _placement(open=["Open"], rev=["Rev"], hold=["Hold"],
                           wit=["Wit"], plain=["Plain"], gate=["Gate"])
    classes = pc.participant_classes(ir, placement)

    assert classes["open"]["class"] == pc.PREPARE_REVERSIBLE
    assert classes["rev"]["class"] == pc.COMPENSABLE
    assert classes["hold"]["class"] == pc.DEFERRED_IRREVERSIBLE
    assert classes["wit"]["class"] == pc.RECOVERABLE_FORWARD
    assert classes["plain"]["class"] == pc.IRREVERSIBLE
    assert classes["gate"]["class"] == pc.IRREVERSIBLE_NEEDS_APPROVAL


def test_a_participant_with_no_crossing_reads_no_crossing(tmp_path):
    ir = _ir(tmp_path)
    classes = pc.participant_classes(ir, _placement(idle=[]))
    assert classes["idle"]["class"] == pc.NO_CROSSING
    assert classes["idle"]["raisedBy"] == []


def test_the_worst_crossing_sets_the_participant_class(tmp_path):
    ir = _ir(tmp_path)
    # Rev is compensable; Gate is irreversible-needs-approval. Together the
    # participant is the weaker (more constraining) of the two.
    classes = pc.participant_classes(ir, _placement(mixed=["Rev", "Gate"]))
    assert classes["mixed"]["class"] == pc.IRREVERSIBLE_NEEDS_APPROVAL


# ---------------------------------------------------------------------------
# 2. the plan-time refusal: an approved irreversible step must be last
# ---------------------------------------------------------------------------


def test_an_approved_irreversible_participant_before_a_fallible_one_is_refused(
        tmp_path):
    ir = _ir(tmp_path)
    # `gate` sorts before `hold`/`plain`/`rev`/`wit`: the commit order
    # `deploy_local_map` builds.
    placement = _placement(gate=["Gate"], hold=["Hold"])
    refusals = pc.plan_refusals(ir, placement)
    assert len(refusals) == 1
    refusal = refusals[0]
    assert refusal["rule"] == pc.RULE_APPROVAL_ORDER
    assert refusal["process"] == "gate"
    assert refusal["before"] == "hold"
    assert "`gate`" in refusal["reason"]
    assert "`hold`" in refusal["reason"]


def test_the_approved_participant_may_commit_last(tmp_path):
    ir = _ir(tmp_path)
    placement = _placement(hold=["Hold"], zz_gate=["Gate"])
    assert pc.plan_refusals(ir, placement) == []


def test_admit_deploy_map_refuses_the_unsafe_order_when_given_the_ir(tmp_path):
    ir = _ir(tmp_path)
    placement = _placement(gate=["Gate"], hold=["Hold"])
    verdict = deploy.admit_deploy_map(placement, ir=ir)
    assert verdict.ok is False
    refusal = next(r for r in verdict.refusals
                   if r["rule"] == pc.RULE_APPROVAL_ORDER)
    assert "`gate`" in refusal["reason"] and "`hold`" in refusal["reason"]


def test_admit_deploy_map_without_the_ir_is_unchanged(tmp_path):
    placement = _placement(gate=["Gate"], hold=["Hold"])
    verdict = deploy.admit_deploy_map(placement)
    assert verdict.ok is True
    assert verdict.refusals == ()


def test_a_component_the_map_places_but_the_ir_lacks_is_refused(tmp_path):
    ir = _ir(tmp_path)
    placement = _placement(db=["NotInTheComposition"])
    refusals = pc.plan_refusals(ir, placement)
    assert any(r["rule"] == pc.RULE_UNKNOWN_COMPONENT for r in refusals)
    verdict = deploy.admit_deploy_map(placement, ir=ir)
    assert verdict.ok is False
    assert any(r["rule"] == pc.RULE_UNKNOWN_COMPONENT
               for r in verdict.refusals)


# ---------------------------------------------------------------------------
# 3. `revl audit --deploy-map` reports the classes and refuses the plan
# ---------------------------------------------------------------------------


def _audit(tmp_path, capsys, map_path, *extra):
    app = tmp_path / "app.rvl"
    app.write_text(SOURCE, encoding="utf-8")
    rc = main(["audit", str(app), "--deploy-map", str(map_path), *extra])
    return rc, capsys.readouterr().out


def test_audit_prints_one_class_per_participant_and_refuses_the_plan(
        tmp_path, capsys):
    map_path = _map(tmp_path, _placement(gate=["Gate"], hold=["Hold"]))
    rc, out = _audit(tmp_path, capsys, map_path)
    assert rc == 1, out
    assert "participant classes" in out
    assert "[irreversible-needs-approval]" in out
    assert "[deferred-irreversible]" in out
    assert "participant order REFUSED" in out
    assert "`gate`" in out and "`hold`" in out


def test_audit_admits_a_plan_with_the_approved_participant_last(tmp_path, capsys):
    map_path = _map(tmp_path, _placement(hold=["Hold"], zz_gate=["Gate"]))
    rc, out = _audit(tmp_path, capsys, map_path)
    assert rc == 0, out
    assert "participant order REFUSED" not in out
    assert "[irreversible-needs-approval]" in out


def test_audit_json_carries_the_participant_table_additively(tmp_path, capsys):
    map_path = _map(tmp_path, _placement(hold=["Hold"], zz_gate=["Gate"]))
    rc, out = _audit(tmp_path, capsys, map_path, "--json")
    assert rc == 0, out
    document = json.loads(out)
    assert document["participantClasses"]["hold"]["class"] == pc.DEFERRED_IRREVERSIBLE
    assert (document["participantClasses"]["zz_gate"]["class"]
            == pc.IRREVERSIBLE_NEEDS_APPROVAL)
    assert "participantRefusals" not in document


def test_audit_without_deploy_map_is_byte_identical(tmp_path, capsys):
    app = tmp_path / "app.rvl"
    app.write_text(SOURCE, encoding="utf-8")
    rc = main(["audit", str(app), "--json"])
    document = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "participantClasses" not in document


# ---------------------------------------------------------------------------
# 4. the durable decision: written by deploy, read by recover
# ---------------------------------------------------------------------------


def _stranded_wal(tmp_path, identity="db"):
    """A real participant WAL: one applied reversible effect, no completion."""
    from revl import _deploy_participant as dp

    wal = tmp_path / f"{identity}.wal"
    participant = dp._Participant({
        "identity": identity,
        "world": str(tmp_path / f"{identity}.world.json"),
        "wal": str(wal),
        "effects": [{"name": "row1", "reversible": True}],
    })
    for effect in participant.effects:
        participant._apply_one(effect)
    return wal


def test_recover_reads_the_abort_decision_and_rolls_back(tmp_path, capsys):
    wal = _stranded_wal(tmp_path)
    decision = tmp_path / "decision.jsonl"
    deploy.write_abort_decision(str(decision), federation_id="app",
                               generation=0, reason="edge failed",
                               failed="edge")
    rc = main(["recover", "--wal", str(wal), "--decision", str(decision),
               "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "rolled-back"
    assert report["federationDecision"] == deploy.FEDERATION_ABORTED
    assert rc in (0, 3)


def test_recover_reads_the_approval_decision_and_rolls_forward(tmp_path, capsys):
    wal = _stranded_wal(tmp_path)
    decision = tmp_path / "decision.jsonl"
    deploy.write_commit_approval(str(decision), federation_id="app",
                                generation=0, participants=["db"])
    rc = main(["recover", "--wal", str(wal), "--decision", str(decision),
               "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "rolled-forward"
    assert report["federationDecision"] == deploy.FEDERATION_APPROVED
    assert rc == 0


def test_recover_refuses_an_unreadable_decision_rather_than_guessing(
        tmp_path, capsys):
    wal = _stranded_wal(tmp_path)
    decision = tmp_path / "decision.jsonl"
    decision.write_text("{not json\n", encoding="utf-8")
    rc = main(["recover", "--wal", str(wal), "--decision", str(decision)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "UNRESOLVED" in out
    assert "will NOT guess" in out


def test_a_pending_record_clears_a_previous_approval(tmp_path):
    decision = tmp_path / "decision.jsonl"
    deploy.write_commit_approval(str(decision), federation_id="app",
                                 generation=0, participants=["db"])
    assert deploy.read_decision(str(decision))[0] == deploy.FEDERATION_APPROVED
    deploy.write_decision_started(str(decision), federation_id="app",
                                  generation=0)
    declared, record = deploy.read_decision(str(decision))
    assert declared is None
    assert record["record"] == deploy.FEDERATION_PENDING


def test_deploy_command_writes_the_decision_next_to_its_state(
        tmp_path, monkeypatch, capsys):
    """The CLI computes the state dir and passes the decision path down; a
    conductor killed after deciding leaves the file `revl recover` reads."""
    from revl import wal as wal_module

    state_root = tmp_path / "state"
    monkeypatch.setattr(wal_module, "default_wal_dir", lambda: str(state_root))
    map_path = _map(tmp_path, _placement(db=["Db"]))
    args = Namespace(map=str(map_path), dry_run=False, json=True, bundle=None,
                     key=None, backend="python", require_gauntlet=False,
                     require_conformance=False)
    rc = deploy.deploy_command(args)
    assert rc == 0, capsys.readouterr().out
    decision = (state_root / "deploy" / map_path.stem / "0"
                / deploy.DECISION_FILE)
    assert decision.is_file()
    kinds = [json.loads(line)["record"]
             for line in decision.read_text(encoding="utf-8").splitlines()]
    assert kinds == [deploy.FEDERATION_PENDING, deploy.FEDERATION_APPROVED]
    assert deploy.read_decision(str(decision))[0] == deploy.FEDERATION_APPROVED


def test_a_stranded_peer_settles_against_the_conductor_abort_decision(
        tmp_path, capsys):
    """The acceptance scenario end to end: a conductor decides ABORT and dies
    before telling a peer. The peer's WAL is stranded, the decision file is what
    `revl recover` reads, and the answer is roll back, not a guess."""
    class _Participant:
        def __init__(self, identity, ok):
            self.identity = identity
            self._ok = ok

        def prepare(self):
            return {"ok": True}

        def commit(self):
            return {"ok": self._ok, "pid": 1}

        def abort(self):
            return {"ok": True, "pid": 1,
                    "residue": {"clean": True, "outstanding": []}}

    decision = tmp_path / "decision.jsonl"
    report = deploy.run_deploy(
        [_Participant("db", True), _Participant("edge", False)],
        approval_path=str(decision))
    assert report["verdict"] == deploy.DEPLOY_ABORTED_CLEAN, report

    # `db` committed and was never told to unwind: its WAL is stranded.
    wal = _stranded_wal(tmp_path)
    rc = main(["recover", "--wal", str(wal), "--decision", str(decision),
               "--json"])
    settled = json.loads(capsys.readouterr().out)
    assert settled["verdict"] == "rolled-back"
    assert settled["federationDecision"] == deploy.FEDERATION_ABORTED
    assert rc in (0, 3)


def test_a_committed_participant_settles_back_under_an_abort_decision(tmp_path):
    """A real participant runs its whole commit, so its WAL carries
    `activation-complete`; a `revl federation-apply` conductor then decides
    ABORT and dies before sending it. The record decides the verdict: roll back. The inverses run
    against the world handed in, here the model, which ends empty; the
    participant's own world file is not what settle rewrites."""
    from revl import _deploy_participant as dp
    from revl.recovery import DictWorld

    wal = tmp_path / "db.wal"
    participant = dp._Participant({
        "identity": "db",
        "world": str(tmp_path / "db.world.json"),
        "wal": str(wal),
        "effects": [{"name": "row1", "reversible": True},
                    {"name": "row2", "reversible": True}],
    })
    assert participant.commit()["ok"] is True
    assert '"activation-complete"' in wal.read_text(encoding="utf-8")
    decision = tmp_path / "decision.jsonl"
    deploy.write_abort_decision(str(decision), federation_id="app",
                               generation=0, reason="edge failed",
                               failed="edge",
                               writer=deploy.FEDERATION_APPLY_WRITER)
    world = DictWorld()
    settled = deploy.settle_stranded(str(wal), str(decision), world=world)
    assert settled["verdict"] == "rolled-back", settled
    assert settled["residue"]["clean"] is True, settled
    assert settled["world"] == "model"
    assert world.remaining() == []   # the model world, not db.world.json


def test_a_redeploy_killed_before_it_commits_keeps_the_earlier_run(tmp_path):
    """`revl deploy` reuses one WAL across runs (issue #3147). Run 1 commits
    and is approved; run 2 writes its pending record and dies before the
    participant writes anything. The WAL is still run 1's, so settle keeps it:
    rolled forward, no inverse run."""
    from revl import _deploy_participant as dp
    from revl.recovery import DictWorld

    spec = {"identity": "db", "world": str(tmp_path / "db.world.json"),
            "wal": str(tmp_path / "db.wal"),
            "effects": [{"name": "row1"}, {"name": "row2"}]}
    decision = str(tmp_path / "decision.jsonl")
    deploy.write_decision_started(decision, federation_id="m", generation=0)
    assert dp._Participant(spec).commit()["ok"] is True
    deploy.write_commit_approval(decision, federation_id="m", generation=0,
                                 participants=["db"])
    deploy.write_decision_started(decision, federation_id="m", generation=0)
    settled = deploy.settle_stranded(spec["wal"], decision, world=DictWorld())
    assert settled["verdict"] == "rolled-forward", settled
    assert settled["federationDecision"] == "none"
    assert settled["committedEffects"] == 2, settled
    assert settled["worldCalls"] == 0, settled   # no inverse was run


def _reused_wal(tmp_path, plan):
    """One WAL file reused by several openings (issue #3147). ``plan`` is a
    list of ``(file, completed)``: each opening writes one reversible effect
    and, when completed, commits its activation and run."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                           / "backends" / "python"))
    import replay

    path = str(tmp_path / "reused.wal")
    for generation, (name, completed) in enumerate(plan, 1):
        wal = replay.WriteAheadLog(path, ir={"composition": "p"},
                                   generation=generation).open()
        wal.record_boundary("Store", f"write {name}", resource=f"file:{name}",
                            inverse_op={"receiver": "fs", "method": "unlink",
                                        "args": [name]})
        if completed:
            wal.commit_activation(["Store"])
            wal.commit_run()
        wal.close()
    return path


def test_committed_false_on_two_crashed_openings_inverts_as_much_as_none(
        tmp_path):
    """A stronger "did not commit" answer must not roll back LESS. With two
    openings that both crashed, `committed=None` inverts both (the trailing
    uncommitted openings); `committed=False` inverts the same effects."""
    from revl.recovery import DictWorld, recover

    path = _reused_wal(tmp_path, [("a", False), ("b", False)])
    plain = recover(path, world=DictWorld())
    answered = recover(path, world=DictWorld(), committed=False)
    assert plain["verdict"] == answered["verdict"] == "rolled-back"
    assert plain["worldCalls"] == 2, plain
    assert answered["worldCalls"] == plain["worldCalls"], answered


def test_committed_false_on_two_completed_openings_keeps_the_first(tmp_path):
    """Both openings completed, and the caller says the step did not commit:
    only the latest opening is inverted; the earlier opening's committed write
    is kept."""
    from revl.recovery import DictWorld, recover

    path = _reused_wal(tmp_path, [("a", True), ("b", True)])
    answered = recover(path, world=DictWorld(), committed=False)
    assert answered["verdict"] == "rolled-back", answered
    assert answered["worldCalls"] == 1, answered
    assert [e["label"] for e in answered["ran"]] == ["write b"], answered

"""Violations recorded against a pool member (issue #2722, first slice).

The pool admits a peer, raises it on attested receipts, and lowers it only when
an operator signs a withdrawal. Nothing records the events that SHOULD lower a
member: a receipt for another artifact, an attestation that rejects a result, an
offer that drifted, repeated unanswered probes. This slice is the record and
nothing else: a closed list of violation kinds, one record per detection in the
roster's append-only ledger, and `pool status` showing them.

The claims, and the mutation that makes each fail:

* V1  a peer that returns a receipt for a different artifact is recorded with
      the `receipt-artifact-mismatch` kind, its tier, caps, budgets and
      evidence are unchanged, and no other member gains a record. Mutation:
      record from the receipt body without binding it to the member, or move
      the tier in the same branch.
* V2  the record is in the ROSTER ledger and survives a reload, and
      `pool status` renders it (human view and `--json`). Mutation: keep the
      violation on the in-process roster only, or omit it from the JSON.
* V3  the kind list is CLOSED: an invented kind is refused before anything is
      appended. Mutation: accept any string.
* V4  a violation cannot be recorded against a peer the pool never admitted.
      Mutation: record against an arbitrary peer id.
* V5  a refusal that is NOT one of the named events (here a result that does
      not hash to its receipt) records nothing. Mutation: record every refusal.
* V6  recording is record-only: the member row is unchanged, and a record
      survives the withdrawal that later removes the member. Mutation: make
      the record a member field that withdrawal deletes.
* V7  a receipt refused on its artifact is recorded only when it is this
      membership's answer to the task being settled: one issued before the
      membership began, or for another task (a replay from another pool
      directory with the same pool id), is refused as before and records
      nothing. Mutation: record on the refusal link alone.
"""

import io
import json
import sys
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl import peer_offer, peer_pool as pp  # noqa: E402
from revl import pool_dispatch as pd  # noqa: E402
from revl import pool_receipt as pr  # noqa: E402
from revl.attest import key_id  # noqa: E402
from revl.lawful_retry import EffectClass  # noqa: E402
from revl.peer_offer import Attestation, PeerOffer  # noqa: E402

OP_KEY = b"pool-operator-key-2722"
WORKER = "peer-worker"
OTHER = "peer-other"

#: Fixtures only. `identity_from_seed` derives the scalar from the seed, so
#: these are published private keys and are used nowhere but here.
WORKER_ID = pi.identity_from_seed(WORKER, b"seed/worker/2722")
OTHER_ID = pi.identity_from_seed(OTHER, b"seed/other/2722")
OPERATOR_ID = pi.identity_from_seed("operator", b"seed/operator/2722")
ATTESTOR_ID = pi.identity_from_seed("attestor", b"seed/attestor/2722")

POOL_CEILING = ("compute()",)
ENTRY_CAPS = ("compute()",)

#: A boundary-free composition: the shape the dispatcher sends.
PURE_SOURCE = b"""pub fn add(a: Int, b: Int) -> Int {
  return a + b
}

test "add is addition" {
  assert add(2, 3) == 5
}
"""

#: The same program with one host extern, so its bytes and digest differ from
#: PURE_SOURCE and a receipt naming it is a receipt for another artifact.
CROSSING_SOURCE = b"""extern pure fn shout(line: Str) -> Int = @py {
  import sys
  sys.stdout.write(line)
  return 1
}

pub fn add(a: Int, b: Int) -> Int {
  return a + b
}

test "add is addition" {
  assert add(2, 3) == 5
}
"""

PURE_DIGEST = pd.artifact_digest(PURE_SOURCE)
CROSSING_DIGEST = pd.artifact_digest(CROSSING_SOURCE)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def make_charter(**overrides) -> dict:
    spec = dict(
        pool_id="lab-violation",
        ceiling=POOL_CEILING,
        tiers={pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS)},
        admit_key_ids=(key_id(OP_KEY),),
        revoke_key_ids=(key_id(OP_KEY),),
        attest_key_ids=(ATTESTOR_ID.key_id,),
        artifact_digests=(PURE_DIGEST, CROSSING_DIGEST),
        identity_mode=pp.MODE_ASYMMETRIC)
    spec.update(overrides)
    return pp.sign_charter(pp.PoolCharter(**spec), OP_KEY)


def _join(charter_record, peer_id, identity, *, nonce, artifact=PURE_DIGEST):
    offer = PeerOffer(peer_id=peer_id,
                      attestation=Attestation(trust="verified"),
                      grant_ceiling=POOL_CEILING)
    body = pp.JoinRequest(
        pool_id=charter_record["pool_id"],
        charter_digest=pp.canonical_digest(charter_record),
        peer_id=peer_id,
        offer=peer_offer.sign_offer_identity(offer, identity),
        artifact_digest=artifact,
        nonce=nonce,
        issued_at=pp._iso(pp._utc_now()))
    return pp.sign_join_identity(body, identity)


def pool_on_disk(tmp_path):
    """A pool directory with two admitted members, written the way the CLI
    writes it, so a test reads what an operator would read."""
    record = make_charter()
    directory = pi.IdentityDirectory()
    for identity in (WORKER_ID, OTHER_ID, ATTESTOR_ID):
        directory.register(identity.public())
    roster = pp.Roster(record["pool_id"], pp.canonical_digest(record))
    for n, (peer, identity) in enumerate(((WORKER, WORKER_ID),
                                          (OTHER, OTHER_ID))):
        verdict = pp.admit(
            record, _join(record, peer, identity, nonce=f"n-{n}"),
            charter_key=OP_KEY, peer_keys={}, directory=directory,
            admitting_key_id=key_id(OP_KEY), roster=roster)
        assert verdict["verdict"] == pp.ADMIT, verdict
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / pp.CHARTER_FILE).write_text(json.dumps(record),
                                            encoding="utf-8")
    pp.save_roster(pool_dir, roster)
    pp.save_directory(pool_dir, directory)
    return pool_dir, record, roster


def signed_task(record, *, task_id: str) -> dict:
    body = pd.build_task(
        pool_id=record["pool_id"],
        charter_digest=pp.canonical_digest(record),
        peer_id=WORKER, task_id=task_id, artifact=PURE_SOURCE,
        runner=pd.RUNNER_TEST_PY, effect_class=EffectClass.PURE,
        grant={"caps": sorted(ENTRY_CAPS), "budgets": {}},
        policy_digest=pp.canonical_digest(record))
    return pd.sign_task(body, OPERATOR_ID)


def dispatch_a_task(pool_dir, record, *, task_id: str) -> dict:
    task = signed_task(record, task_id=task_id)
    ledger = pd.load_ledger(pool_dir)
    written = ledger.dispatch(task_id=task_id, peer_id=WORKER,
                              artifact_digest=PURE_DIGEST,
                              runner=pd.RUNNER_TEST_PY,
                              effect_class=EffectClass.PURE.value, task=task)
    assert written["ok"], written
    pd.save_ledger(pool_dir, ledger)
    return task


def answer_with_artifact(pool_dir, record, roster, *, task_id: str,
                         artifact_digest: str, result=None) -> dict:
    """Send one task and settle an answer whose receipt names another
    artifact."""
    task = dispatch_a_task(pool_dir, record, task_id=task_id)
    result = result if result is not None else {"exit_code": 0}
    receipt = pr.issue_receipt(
        pool_id=record["pool_id"], task_id=task_id,
        artifact_digest=artifact_digest, result=result, identity=WORKER_ID,
        task_digest=pr.receipt_digest(task),
        grant={"caps": sorted(ENTRY_CAPS), "budgets": {}},
        policy_digest=pp.canonical_digest(record))
    return pd._settle(
        pool_dir=pool_dir, peer_id=WORKER, task_id=task_id,
        member=roster.members[WORKER],
        answer={"ok": True, "receipt": receipt, "result": result},
        attesting_identity=ATTESTOR_ID, task=task)


def status_json(pool_dir) -> dict:
    args = type("Args", (), {"dir": str(pool_dir), "json": True,
                             "require_live": None})()
    out = io.StringIO()
    with redirect_stdout(out):
        assert pp._status_command(args) == 0
    return json.loads(out.getvalue())


# ---------------------------------------------------------------------------
# V1, V2: the recorded event, and where it is shown
# ---------------------------------------------------------------------------


def test_a_receipt_for_another_artifact_is_recorded_and_moves_no_member(
        tmp_path):
    pool_dir, record, roster = pool_on_disk(tmp_path)
    before = {peer: roster.members[peer] for peer in (WORKER, OTHER)}

    outcome = answer_with_artifact(pool_dir, record, roster, task_id="t-bad",
                                   artifact_digest=CROSSING_DIGEST)

    assert outcome["link"] == pd.LINK_RECEIPT_REFUSED, outcome
    assert outcome["receipt_link"] == pr.LINK_RECEIPT_ARTIFACT, outcome

    _charter, reread = pp.load_pool(pool_dir)
    recorded = pp.violations_of(reread, WORKER)
    assert [event["violation"] for event in recorded] == [
        pp.VIOLATION_RECEIPT_ARTIFACT], recorded
    assert recorded[0]["peer_id"] == WORKER
    assert recorded[0]["link"] == pr.LINK_RECEIPT_ARTIFACT
    assert recorded[0]["pool_id"] == record["pool_id"]

    # RECORD ONLY: the tier and everything that grants is exactly what it was.
    assert reread.members[WORKER] == before[WORKER]
    assert reread.members[WORKER].tier == pp.ENTRY_TIER
    assert reread.members[WORKER].caps == ENTRY_CAPS
    assert reread.members[WORKER].budgets == {}
    assert reread.members[WORKER].evidence == 0

    # One member's violation is not another member's.
    assert pp.violations_of(reread, OTHER) == ()
    assert reread.members[OTHER] == before[OTHER]

    # The delivery is settled and refused, not outstanding.
    ledger = pd.load_ledger(pool_dir)
    assert ledger.tasks["t-bad"]["state"] == pd.STATE_REFUSED
    assert ledger.outstanding() == {}


def test_pool_status_shows_the_recorded_violation(tmp_path):
    pool_dir, record, roster = pool_on_disk(tmp_path)
    answer_with_artifact(pool_dir, record, roster, task_id="t-bad",
                         artifact_digest=CROSSING_DIGEST)

    _charter, reread = pp.load_pool(pool_dir)
    rendered = pp.render_status(record, reread)
    assert "violations 1" in rendered
    assert f"{WORKER}  {pp.VIOLATION_RECEIPT_ARTIFACT}" in rendered

    payload = status_json(pool_dir)
    assert payload["violations"] == {
        WORKER: {pp.VIOLATION_RECEIPT_ARTIFACT: 1}}
    # The event is in the roster the JSON carries, not only in the summary.
    events = payload["roster"]["events"]
    assert any(event.get("kind") == pp.VIOLATION_KIND
               and event.get("peer_id") == WORKER for event in events)


# ---------------------------------------------------------------------------
# V3, V4: the list is closed, and a record names a member
# ---------------------------------------------------------------------------


def test_the_violation_kind_list_is_closed(tmp_path):
    _pool_dir, _record, roster = pool_on_disk(tmp_path)

    assert pp.VIOLATION_KINDS == (
        pp.VIOLATION_RECEIPT_ARTIFACT,
        pp.VIOLATION_ATTESTATION_REJECTED,
        pp.VIOLATION_OFFER_DRIFT,
        pp.VIOLATION_PROBE_UNANSWERED,
    )
    with pytest.raises(pp.PoolError):
        pp.record_violation(roster, WORKER, "invented-violation")
    assert pp.violation_events(roster) == ()


def test_every_named_kind_can_be_recorded(tmp_path):
    _pool_dir, _record, roster = pool_on_disk(tmp_path)
    for kind in pp.VIOLATION_KINDS:
        pp.record_violation(roster, WORKER, kind, detail=f"observed {kind}")
    assert [event["violation"] for event in pp.violation_events(roster)] == \
        list(pp.VIOLATION_KINDS)


def test_a_violation_cannot_be_recorded_against_a_peer_never_admitted(tmp_path):
    _pool_dir, _record, roster = pool_on_disk(tmp_path)
    with pytest.raises(pp.PoolError):
        pp.record_violation(roster, "peer-stranger",
                            pp.VIOLATION_RECEIPT_ARTIFACT)
    assert pp.violation_events(roster) == ()


def test_the_receipt_refusal_mapping_names_the_two_delivery_events():
    """The mapping from a receipt-check link to the closed list, pinned so a
    later link cannot be folded into a violation by accident."""
    assert pd._violation_for_receipt_refusal(
        pr.LINK_RECEIPT_ARTIFACT, "") == pp.VIOLATION_RECEIPT_ARTIFACT
    assert pd._violation_for_receipt_refusal(
        pr.LINK_ATTESTATION_AUTHORITY,
        "rejected") == pp.VIOLATION_ATTESTATION_REJECTED
    # An attestation that was ADMITTED is refused for some other reason, and a
    # result that does not hash to its receipt breaks no stated term.
    assert pd._violation_for_receipt_refusal(
        pr.LINK_ATTESTATION_AUTHORITY, pr.ADMITTED) == ""
    assert pd._violation_for_receipt_refusal(
        pd.LINK_RESULT_DIGEST, "") == ""


# ---------------------------------------------------------------------------
# V5, V6: not every refusal is a violation, and a record is not authority
# ---------------------------------------------------------------------------


def test_a_result_that_does_not_hash_to_its_receipt_records_no_violation(
        tmp_path):
    pool_dir, record, roster = pool_on_disk(tmp_path)
    task = dispatch_a_task(pool_dir, record, task_id="t-hash")
    receipt = pr.issue_receipt(
        pool_id=record["pool_id"], task_id="t-hash",
        artifact_digest=PURE_DIGEST, result={"exit_code": 0},
        identity=WORKER_ID, task_digest=pr.receipt_digest(task),
        grant={"caps": sorted(ENTRY_CAPS), "budgets": {}},
        policy_digest=pp.canonical_digest(record))

    outcome = pd._settle(
        pool_dir=pool_dir, peer_id=WORKER, task_id="t-hash",
        member=roster.members[WORKER],
        answer={"ok": True, "receipt": receipt, "result": {"exit_code": 1}},
        attesting_identity=ATTESTOR_ID, task=task)

    assert outcome["link"] == pd.LINK_RESULT_DIGEST, outcome
    _charter, reread = pp.load_pool(pool_dir)
    assert pp.violation_events(reread) == ()


def test_a_record_survives_the_withdrawal_that_removes_the_member(tmp_path):
    pool_dir, record, roster = pool_on_disk(tmp_path)
    pp.record_violation(roster, WORKER, pp.VIOLATION_RECEIPT_ARTIFACT,
                        detail="a receipt named another artifact")
    pp.save_roster(pool_dir, roster)

    receipt = pp.withdraw(record, WORKER, "operator withdrew the peer",
                          charter_key=OP_KEY, roster=roster,
                          revoking_key_id=key_id(OP_KEY))
    assert receipt["verdict"] == pp.WITHDRAW, receipt
    assert WORKER not in roster.members
    pp.save_roster(pool_dir, roster)

    _charter, reread = pp.load_pool(pool_dir)
    assert WORKER not in reread.members
    recorded = pp.violations_of(reread, WORKER)
    assert [event["violation"] for event in recorded] == [
        pp.VIOLATION_RECEIPT_ARTIFACT]
    # The human view still names it: a withdrawn peer is not a peer that never
    # broke a term.
    rendered = pp.render_status(record, reread)
    assert f"{WORKER}  {pp.VIOLATION_RECEIPT_ARTIFACT}" in rendered


def test_pool_status_escapes_control_characters_in_a_detail(tmp_path):
    """A detail holds part of the peer's signed artifact digest, which nothing
    checks is hex. The rendered line escapes it; the stored record does not."""
    pool_dir, record, roster = pool_on_disk(tmp_path)
    hostile = "\x1b[2J\x1b[31mred\nviolations 0"
    pp.record_violation(roster, WORKER, pp.VIOLATION_RECEIPT_ARTIFACT,
                        detail=hostile)
    pp.save_roster(pool_dir, roster)

    _charter, reread = pp.load_pool(pool_dir)
    rendered = pp.render_status(record, reread)
    assert "\x1b" not in rendered
    assert "\\x1b[2J\\x1b[31mred\\nviolations 0" in rendered
    # The newline did not forge a second line.
    assert not any(line.startswith("violations 0")
                   for line in rendered.splitlines())

    # The JSON file and the --json view keep the bytes as recorded.
    events = status_json(pool_dir)["roster"]["events"]
    assert [event["detail"] for event in events
            if event.get("kind") == pp.VIOLATION_KIND] == [hostile]


# ---------------------------------------------------------------------------
# V7: a receipt is recorded against the membership and task it answers only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["replay-from-another-pool", "stale-only",
                                  "other-task-only"])
def test_a_receipt_from_another_membership_or_task_records_nothing(
        tmp_path, case):
    """The pool check refuses the artifact before it reads the issue time or
    the task, so a receipt this key signed in another pool directory with the
    same pool id reaches the settle path refused on ``receipt-artifact``. It
    stays refused, and nothing is recorded against the member of THIS pool.
    Mutation: record on the refusal link alone."""
    # Pool A: the same charter, so the same pool id, in another directory.
    record_a = make_charter()
    task_a = signed_task(record_a, task_id="t-a")

    pool_dir, record, roster = pool_on_disk(tmp_path)
    task_b = dispatch_a_task(pool_dir, record, task_id="t-b")
    admitted = pp._parse_iso(roster.members[WORKER].admitted_at)
    earlier = admitted - timedelta(minutes=5)
    later = admitted + timedelta(seconds=1)
    issued_at, answered = {
        "replay-from-another-pool": (earlier, task_a),
        "stale-only": (earlier, task_b),
        "other-task-only": (later, task_a),
    }[case]
    receipt = pr.issue_receipt(
        pool_id=record_a["pool_id"], task_id=answered["task_id"],
        artifact_digest=CROSSING_DIGEST, result={"exit_code": 0},
        identity=WORKER_ID, at=issued_at,
        task_digest=pr.receipt_digest(answered),
        grant={"caps": sorted(ENTRY_CAPS), "budgets": {}},
        policy_digest=pp.canonical_digest(record_a))

    outcome = pd._settle(
        pool_dir=pool_dir, peer_id=WORKER, task_id="t-b",
        member=roster.members[WORKER],
        answer={"ok": True, "receipt": receipt, "result": {"exit_code": 0}},
        attesting_identity=ATTESTOR_ID, task=task_b)

    # Refused exactly as before: the reason a caller sees is unchanged.
    assert outcome["link"] == pd.LINK_RECEIPT_REFUSED, outcome
    assert outcome["receipt_link"] == pr.LINK_RECEIPT_ARTIFACT, outcome
    assert pd.load_ledger(pool_dir).tasks["t-b"]["state"] == pd.STATE_REFUSED
    _charter, reread = pp.load_pool(pool_dir)
    assert pp.violation_events(reread) == ()

    # Positive control: this membership's own receipt for B's own task, with
    # the wrong artifact, is still recorded.
    answer_with_artifact(pool_dir, record, reread, task_id="t-b2",
                         artifact_digest=CROSSING_DIGEST)
    _charter, reread = pp.load_pool(pool_dir)
    assert [event["violation"] for event in pp.violations_of(reread, WORKER)] \
        == [pp.VIOLATION_RECEIPT_ARTIFACT]

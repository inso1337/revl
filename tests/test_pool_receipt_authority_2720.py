"""A receipt records the grant, policy, outcome and budget claim of the run
(issue #2720).

What is under test, in the order the issue states it.

1. THE ECHO IS CHECKED. The operator signs a task that names the granted
   capability set and the policy digest. The peer's receipt echoes the task
   digest, that grant and that policy digest, and `check_receipt` compares the
   echo with the task. A receipt built with a grant the task did not grant is
   refused; before this change there was no field to disagree about.

2. TAMPERING BREAKS BOTH BINDINGS. Changing any of the new fields after the
   peer signs breaks the peer's signature, and the receipt digest the attestor
   signed no longer covers the record in hand. The two failures are asserted
   separately because they are two different statements about the same bytes.

3. THE VERSION FIELD DECIDES. A 1.0 receipt still verifies: it is a real
   statement about work. It does NOT count as evidence for a promotion that
   requires the authority binding. And a 1.1 receipt with a missing field is
   malformed, never read as a 1.0 receipt that happened to drop a member.

What is a CLAIM, not a proof. `outcome` is the peer's own report of how its
run ended and `budget_consumed` is its own report of what it used. Nothing here
checks either against reality; the correctness check remains the attestor.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl import peer_pool as pp, pool_receipt as pr  # noqa: E402
from revl import pool_dispatch as pd  # noqa: E402
from revl.attest import key_id  # noqa: E402

OP_KEY = b"pool-operator-key-2720"

GOOD_ARTIFACT = "d" * 64
POOL_CEILING = ('fs.read(path="/data")',)
ENTRY_CAPS = ('fs.read(path="/data/in")',)
RESULT = {"rows": 3, "checksum": "ok"}

WORKER = "peer-worker"
ATTESTOR = "pool-attestor"

WORKER_ID = pi.identity_from_seed(WORKER, b"seed/worker/2720")
ATTESTOR_ID = pi.identity_from_seed(ATTESTOR, b"seed/attestor/2720")
OPERATOR_ID = pi.identity_from_seed("operator", b"seed/operator/2720")


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def make_charter(**overrides) -> dict:
    spec = dict(
        pool_id="lab-2720",
        ceiling=POOL_CEILING,
        tiers={pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS)},
        admit_key_ids=(key_id(OP_KEY),),
        revoke_key_ids=(key_id(OP_KEY),),
        attest_key_ids=(ATTESTOR_ID.key_id,),
        artifact_digests=(GOOD_ARTIFACT,),
        identity_mode=pp.MODE_ASYMMETRIC)
    spec.update(overrides)
    return pp.sign_charter(pp.PoolCharter(**spec), OP_KEY)


def signed_task(record, *, task_id="task-1", grant=None, policy_digest=None):
    """A real signed 1.1 task: the operator's decision about this work."""
    grant = grant if grant is not None else {
        "caps": sorted(ENTRY_CAPS), "budgets": {"retries": 2}}
    policy = (policy_digest if policy_digest is not None
              else pp.canonical_digest(record))
    body = {
        "kind": pd.TASK_KIND, "version": pd.TASK_VERSION,
        "pool_id": record["pool_id"],
        "charter_digest": pp.canonical_digest(record),
        "peer_id": WORKER, "task_id": task_id,
        "artifact_digest": GOOD_ARTIFACT, "artifact": "",
        "runner": pd.RUNNER_TEST_PY, "effect_class": "pure",
        "issued_at": pp._iso(pp._utc_now()),
        "grant": grant, "policy_digest": policy,
    }
    return pd.sign_task(body, OPERATOR_ID)


def bound_receipt(record, *, task=None, grant=None, policy_digest=None,
                  outcome=pr.COMPLETED, budget_consumed=None):
    task = task if task is not None else signed_task(record)
    return pr.issue_receipt(
        pool_id=record["pool_id"], task_id=task["task_id"],
        artifact_digest=GOOD_ARTIFACT, result=RESULT, identity=WORKER_ID,
        task_digest=pr.receipt_digest(task),
        grant=grant if grant is not None else task["grant"],
        policy_digest=(policy_digest if policy_digest is not None
                       else task["policy_digest"]),
        outcome=outcome,
        budget_consumed=(budget_consumed if budget_consumed is not None
                         else {"wall_seconds": 0.25}))


def directory():
    pinned = pi.IdentityDirectory()
    pinned.register(WORKER_ID.public())
    pinned.register(ATTESTOR_ID.public())
    return pinned


def joined(record):
    """Admit the worker, so `check_receipt` has an admission to check against.

    The evidence path is not what is under test here; this is the same
    admission gate `tests/test_pool_receipts_524.py` builds, reduced to one
    peer because the authority binding needs one."""
    from revl import peer_offer
    from revl.peer_offer import Attestation, PeerOffer

    pinned = directory()
    roster = pp.Roster(record["pool_id"], pp.canonical_digest(record))
    offer = PeerOffer(peer_id=WORKER, attestation=Attestation(trust="verified"),
                      grant_ceiling=POOL_CEILING)
    join = pp.sign_join_identity(pp.JoinRequest(
        pool_id=record["pool_id"], charter_digest=pp.canonical_digest(record),
        peer_id=WORKER,
        offer=peer_offer.sign_offer_identity(offer, WORKER_ID),
        artifact_digest=GOOD_ARTIFACT, nonce="n-1",
        issued_at=pp._iso(pp._utc_now())), WORKER_ID)
    verdict = pp.admit(record, join, charter_key=OP_KEY, peer_keys={},
                       directory=pinned, admitting_key_id=key_id(OP_KEY),
                       roster=roster)
    assert verdict["verdict"] == pp.ADMIT, verdict
    return roster, pinned


def check(receipt, record, roster, pinned, *, task=None, require_bound=False):
    return pr.check_receipt(
        receipt, pr.attest_receipt(receipt, identity=ATTESTOR_ID),
        pool_id=record["pool_id"], peer_id=WORKER,
        artifact_digest=GOOD_ARTIFACT,
        admitted_at=roster.members[WORKER].admitted_at,
        attest_key_ids=(ATTESTOR_ID.key_id,), directory=pinned,
        task=task, require_bound=require_bound)


# ---------------------------------------------------------------------------
# 1. the echo is checked against the task that decided it
# ---------------------------------------------------------------------------


def test_a_receipt_echoing_a_grant_the_task_did_not_grant_is_refused():
    """Acceptance 1. The receipt is genuinely signed by the peer and genuinely
    attested; the ONLY wrong thing is that its grant is wider than the task's.
    Before issue #2720 the receipt had no grant field, so this test could not
    exist: the two records could not disagree about authority."""
    record = make_charter()
    roster, pinned = joined(record)
    task = signed_task(record)
    wider = {"caps": sorted(POOL_CEILING), "budgets": {"retries": 2}}
    receipt = bound_receipt(record, task=task, grant=wider)

    verdict = check(receipt, record, roster, pinned, task=task)
    assert not verdict.counts
    assert verdict.link == pr.LINK_RECEIPT_GRANT, verdict.as_dict()
    assert verdict.peer.verified and verdict.attestor.verified, (
        "the refusal is the binding, not the arithmetic: both signatures are "
        "real")


def test_a_receipt_echoing_a_policy_the_task_did_not_name_is_refused():
    """The policy half of the same binding. The run is claimed under a policy
    the operator did not sign into the task, which is exactly what a reader
    must be able to catch from the echo."""
    record = make_charter()
    roster, pinned = joined(record)
    task = signed_task(record)
    receipt = bound_receipt(record, task=task, policy_digest="9" * 64)

    verdict = check(receipt, record, roster, pinned, task=task)
    assert not verdict.counts
    assert verdict.link == pr.LINK_RECEIPT_POLICY, verdict.as_dict()


def test_a_bound_receipt_without_its_task_verifies_nowhere():
    """A task digest nobody can recompute is not a check. The receipt is
    refused on `receipt-task` rather than trusted or guessed at."""
    record = make_charter()
    roster, pinned = joined(record)
    receipt = bound_receipt(record, task=signed_task(record))

    verdict = check(receipt, record, roster, pinned)
    assert not verdict.counts
    assert verdict.link == pr.LINK_RECEIPT_TASK, verdict.as_dict()


@pytest.mark.parametrize("member,wrong", [
    ("task_id", "task-2"),
    ("peer_id", "peer-other"),
    ("pool_id", "other-pool"),
    ("artifact_digest", "e" * 64),
])
def test_a_receipt_and_the_task_it_answers_must_agree_on_identity(
        member, wrong):
    """A digest match names a RECORD, and the record must describe the work the
    receipt claims. Here the peer signs a receipt for task-1 whose
    `task_digest` points at a record that differs in one identity member: the
    digest check alone would accept it, and the receipt would cite evidence for
    work it does not describe. Each member is refused on `receipt-task`."""
    record = make_charter()
    roster, pinned = joined(record)
    task = signed_task(record)
    task[member] = wrong
    task = pd.sign_task(task, OPERATOR_ID)
    receipt = pr.issue_receipt(
        pool_id=record["pool_id"], task_id="task-1",
        artifact_digest=GOOD_ARTIFACT, result=RESULT, identity=WORKER_ID,
        task_digest=pr.receipt_digest(task), grant=task["grant"],
        policy_digest=task["policy_digest"], outcome=pr.COMPLETED,
        budget_consumed={})
    assert receipt["task_digest"] == pr.receipt_digest(task)

    verdict = check(receipt, record, roster, pinned, task=task)
    assert not verdict.counts, f"{member} was accepted: {verdict.as_dict()}"
    assert verdict.link == pr.LINK_RECEIPT_TASK, verdict.as_dict()
    assert member in verdict.reason, verdict.reason


def test_a_budget_claim_that_is_not_a_non_negative_number_is_malformed():
    """The budget map is a claim, but it has to be a claim ABOUT something: a
    negative or non-numeric amount is a malformed receipt, not a smaller
    one."""
    with pytest.raises(pr.ReceiptError, match="budget_consumed"):
        bound_receipt(record=make_charter(), budget_consumed={"retries": -1})


# ---------------------------------------------------------------------------
# 2. every new field is inside both bindings
# ---------------------------------------------------------------------------


NEW_FIELDS = ("task_digest", "grant", "policy_digest", "outcome",
              "budget_consumed")


def test_changing_any_new_field_after_signing_breaks_the_peer_signature():
    """Acceptance 2, the peer half. Each mutation is made after signing, so
    the arithmetic over the bytes must fail for every one of them."""
    record = make_charter()
    roster, pinned = joined(record)
    task = signed_task(record)
    receipt = bound_receipt(record, task=task)
    edits = {
        "task_digest": "9" * 64,
        "grant": {"caps": sorted(POOL_CEILING), "budgets": {"retries": 2}},
        "policy_digest": "9" * 64,
        "outcome": pr.FAILED,
        "budget_consumed": {"wall_seconds": 99.0},
    }
    for field in NEW_FIELDS:
        edited = {**receipt, field: edits[field]}
        verdict = check(edited, record, roster, pinned, task=task)
        assert verdict.link == pr.LINK_RECEIPT_SIGNATURE, (
            f"{field}: {verdict.as_dict()}")


def test_changing_any_new_field_breaks_the_attestor_binding_too():
    """Acceptance 2, the attestor half, kept separate. The attestor signed the
    DIGEST of the peer's receipt; if a new field is folded into that body (as
    ``receipt_digest`` covers the whole record), changing it moves the digest
    the attestation names."""
    record = make_charter()
    receipt = bound_receipt(record, task=signed_task(record))
    attestation = pr.attest_receipt(receipt, identity=ATTESTOR_ID)
    assert attestation["receipt_digest"] == pr.receipt_digest(receipt)

    edits = {
        "task_digest": "9" * 64,
        "grant": {"caps": sorted(POOL_CEILING), "budgets": {"retries": 2}},
        "policy_digest": "9" * 64,
        "outcome": pr.FAILED,
        "budget_consumed": {"wall_seconds": 99.0},
    }
    for field in NEW_FIELDS:
        edited = {**receipt, field: edits[field]}
        assert pr.receipt_digest(edited) != attestation["receipt_digest"], (
            f"{field} is not inside the digest the attestor signs")


# ---------------------------------------------------------------------------
# 3. the version field decides old from new
# ---------------------------------------------------------------------------


def test_an_old_receipt_verifies_but_cannot_be_evidence_for_a_bound_gate():
    """Acceptance 3. A 1.0 receipt is read as 1.0, not guessed at from its
    members. It verifies: the work really happened. It does not count for a
    promotion that requires the authority binding, and it is REFUSED on a
    named link rather than silently ignored."""
    record = make_charter()
    roster, pinned = joined(record)
    old = pr.issue_receipt(pool_id=record["pool_id"], task_id="task-old",
                           artifact_digest=GOOD_ARTIFACT, result=RESULT,
                           identity=WORKER_ID)

    verdict = check(old, record, roster, pinned)
    assert verdict.counts, verdict.as_dict()
    assert verdict.peer.verified and verdict.attestor.verified

    counted = pr.count_evidence(
        [(old, pr.attest_receipt(old, identity=ATTESTOR_ID))],
        pool_id=record["pool_id"], peer_id=WORKER,
        artifact_digest=GOOD_ARTIFACT,
        admitted_at=roster.members[WORKER].admitted_at,
        attest_key_ids=(ATTESTOR_ID.key_id,), directory=pinned)
    assert counted.counted == 0
    assert [r.link for r in counted.rejected] == [pr.LINK_UNBOUND_RECEIPT]


def test_a_version_1_1_receipt_missing_a_new_member_is_malformed():
    """The version field decides, not the presence of a member. A record that
    says 1.1 and omits the grant is not read as a 1.0 receipt; it is refused
    on shape, because guessing is how a missing check becomes a passing one."""
    record = make_charter()
    roster, pinned = joined(record)
    task = signed_task(record)
    receipt = bound_receipt(record, task=task)
    for field in NEW_FIELDS:
        broken = {k: v for k, v in receipt.items() if k != field}
        verdict = check(broken, record, roster, pinned, task=task)
        assert verdict.link == pr.LINK_RECEIPT_SHAPE, (
            f"{field}: {verdict.as_dict()}")


def test_a_version_1_0_receipt_is_read_as_1_0_even_with_the_new_members():
    """The other direction of "the version decides". A record that SAYS 1.0 is
    read as 1.0 even if someone bolted the 1.1 members onto it: it verifies,
    and a promotion that requires the binding does not count it. Reading the
    members instead would let a record opt into a stronger shape by naming
    fields the checker never required."""
    record = make_charter()
    roster, pinned = joined(record)
    task = signed_task(record)
    body = {k: v for k, v in bound_receipt(record, task=task).items()
            if k not in ("key_id", "sign_alg", "public_key", "signature")}
    body["version"] = pr.RECEIPT_VERSION_V1
    old_looking = pi.sign_record(pr.RECEIPT_DOMAIN, body, WORKER_ID)

    verdict = check(old_looking, record, roster, pinned, task=task)
    assert verdict.counts, verdict.as_dict()

    counted = pr.count_evidence(
        [(old_looking, pr.attest_receipt(old_looking, identity=ATTESTOR_ID),
          task)],
        pool_id=record["pool_id"], peer_id=WORKER,
        artifact_digest=GOOD_ARTIFACT,
        admitted_at=roster.members[WORKER].admitted_at,
        attest_key_ids=(ATTESTOR_ID.key_id,), directory=pinned)
    assert counted.counted == 0
    assert [r.link for r in counted.rejected] == [pr.LINK_UNBOUND_RECEIPT]


def test_a_1_1_receipt_with_an_unknown_outcome_or_bad_budget_is_malformed():
    record = make_charter()
    roster, pinned = joined(record)
    task = signed_task(record)
    receipt = bound_receipt(record, task=task)
    for field, value in (("outcome", "maybe"), ("budget_consumed",
                                                {"wall_seconds": -1})):
        broken = {**receipt, field: value}
        verdict = check(broken, record, roster, pinned, task=task)
        assert verdict.link == pr.LINK_RECEIPT_SHAPE, (
            f"{field}: {verdict.as_dict()}")


def test_the_outcome_is_the_peers_measurement_not_the_exit_code():
    """A program is free to exit 124, so a timeout is the peer's observation
    of the call it made (``execute`` records both), never an inference from
    the exit code."""
    assert pd._outcome_of({"exit_code": 0}) == pr.COMPLETED
    assert pd._outcome_of({"exit_code": 1}) == pr.FAILED
    assert pd._outcome_of({"exit_code": 124}) == pr.FAILED
    assert pd._outcome_of({"exit_code": 124, "timed_out": True}) == pr.TIMED_OUT


def test_an_unknown_receipt_version_is_refused_on_shape():
    record = make_charter()
    roster, pinned = joined(record)
    receipt = bound_receipt(record, task=signed_task(record))
    future = {**receipt, "version": "9.9"}
    assert check(future, record, roster, pinned).link == pr.LINK_RECEIPT_SHAPE


# ---------------------------------------------------------------------------
# 4. what a promotion requires
# ---------------------------------------------------------------------------


def test_bound_receipts_count_as_evidence_and_the_promotion_cites_them():
    """The positive path: the same gate admits a receipt that carries the
    authority binding, so the rule above is not a gate that refuses
    everything."""
    record = make_charter(
        tiers={pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS),
               "replayable": pp.TierGrant(caps=POOL_CEILING,
                                          evidence_required=1)})
    roster, pinned = joined(record)
    task = signed_task(record)
    receipt = bound_receipt(record, task=task)
    counted = pr.count_evidence(
        [(receipt, pr.attest_receipt(receipt, identity=ATTESTOR_ID), task)],
        pool_id=record["pool_id"], peer_id=WORKER,
        artifact_digest=GOOD_ARTIFACT,
        admitted_at=roster.members[WORKER].admitted_at,
        attest_key_ids=(ATTESTOR_ID.key_id,), directory=pinned)
    assert counted.counted == 1, counted.as_dict()
    assert counted.task_ids == ("task-1",)

    promotion = pp.promote(
        record, WORKER, "replayable", charter_key=OP_KEY, roster=roster,
        receipts=[(receipt, pr.attest_receipt(receipt, identity=ATTESTOR_ID),
                   task)],
        directory=pinned)
    assert promotion["verdict"] == pp.PROMOTE, promotion
    assert promotion["evidence_digests"] == [pr.receipt_digest(receipt)]

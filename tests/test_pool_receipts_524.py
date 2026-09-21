"""Signed execution receipts and the evidence recount (item 524, issue #1198).

What is under test, in the order it matters.

1. NON-VACUITY. Before this change `Membership.evidence` was an integer the
   CALLER handed the promotion gate. Every receipt in the corpus below would
   have promoted a peer under the old gate, because the old gate never looked
   at a receipt at all; it read a number. Each is now refused on a different
   named link, and an honest control is admitted by the same code path. A gate
   that refuses nothing and a gate that refuses everything are both excluded.

2. A STATED COUNT IS NOT EVIDENCE. The count is `len(evidence_digests)` and
   the digests are receipts this pool verified. `tests/test_peer_pool_admission
   .py::test_an_evidence_count_cannot_be_stated_at_all` holds the constructor
   and the roster file; this file holds the gate.

3. THE ATTRIBUTION SHAPE SURVIVES (issue #1278). A receipt check is never a
   boolean. A receipt signed under a key that was later revoked is
   `verified=True, status="revoked"`: a real past act that confers nothing.
   An operator auditing a ledger has to be able to tell that from a forgery,
   and a bare `False` for both would destroy the distinction.

4. THE EXIT TEST, clause by clause. Item 524's exit test is two machines
   joining a pool, one running a task the other admits, the result verified by
   hash and receipt, and a peer leaving mid-task leaving the ledger in a stated
   state. Each clause has a test below and each test says in its docstring
   which clause it is and what it does NOT reach.
"""

import ast
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl import peer_offer, peer_pool as pp, pool_receipt as pr  # noqa: E402
from revl.attest import key_id  # noqa: E402
from revl.peer_offer import Attestation, PeerOffer, ResourceOffer  # noqa: E402

OP_KEY = b"pool-operator-key-524-receipts"

GOOD_ARTIFACT = "d" * 64
OTHER_ARTIFACT = "e" * 64
POOL_CEILING = ('fs.read(path="/data")',)
ENTRY_CAPS = ('fs.read(path="/data/in")',)

#: The two peers of the exit test. `worker` runs the task; `admitter` is the
#: second member, and it is a member rather than a bystander because the exit
#: test says TWO machines join.
WORKER = "peer-worker"
ADMITTER = "peer-admitter"

#: Deterministic identities. `identity_from_seed`'s docstring says why this is
#: fixtures-only: the scalar is a hash of the seed, so a seed committed to a
#: repository is a published private key.
WORKER_ID = pi.identity_from_seed(WORKER, b"seed/worker/524")
ADMITTER_ID = pi.identity_from_seed(ADMITTER, b"seed/admitter/524")

#: The attesting authority. A THIRD key, distinct from the admit key and from
#: either peer's, because the pool's authority view splits admit from attest
#: and a single compromised key must not do both.
ATTESTOR = "pool-attestor"
ATTESTOR_ID = pi.identity_from_seed(ATTESTOR, b"seed/attestor/524")

#: An attacker's key pair. Perfectly well formed; nobody pinned it.
INTRUDER = "intruder"
INTRUDER_ID = pi.identity_from_seed(INTRUDER, b"seed/intruder/524")

RESULT = {"rows": 3, "checksum": "ok"}


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def make_charter(**overrides) -> dict:
    spec = dict(
        pool_id="lab-524",
        ceiling=POOL_CEILING,
        tiers={
            pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS),
            "replayable": pp.TierGrant(caps=POOL_CEILING, evidence_required=3),
        },
        admit_key_ids=(key_id(OP_KEY),),
        revoke_key_ids=(key_id(OP_KEY),),
        attest_key_ids=(ATTESTOR_ID.key_id,),
        artifact_digests=(GOOD_ARTIFACT,),
        identity_mode=pp.MODE_ASYMMETRIC)
    spec.update(overrides)
    return pp.sign_charter(pp.PoolCharter(**spec), OP_KEY)


def make_offer(peer_id) -> PeerOffer:
    return PeerOffer(
        peer_id=peer_id,
        attestation=Attestation(trust="verified", region="eu", hardware="x86",
                                resource_offer=ResourceOffer(4, 1 << 30, 60.0)),
        grant_ceiling=POOL_CEILING)


def identity_join(charter_record, peer_id, identity, *, nonce="n-1",
                  artifact=GOOD_ARTIFACT) -> dict:
    body = pp.JoinRequest(
        pool_id=charter_record["pool_id"],
        charter_digest=pp.canonical_digest(charter_record),
        peer_id=peer_id,
        offer=peer_offer.sign_offer_identity(make_offer(peer_id), identity),
        artifact_digest=artifact,
        nonce=nonce,
        issued_at=pp._iso(pp._utc_now()))
    return pp.sign_join_identity(body, identity)


def pinned_directory() -> pi.IdentityDirectory:
    """Every public key the operator pins. The INTRUDER is deliberately absent:
    that is what makes it an intruder."""
    directory = pi.IdentityDirectory()
    for identity in (WORKER_ID, ADMITTER_ID, ATTESTOR_ID):
        directory.register(identity.public())
    return directory


def joined_pool():
    """Exit-test clause 1: two peers join one pool. Returns everything the
    other clauses need."""
    record = make_charter()
    directory = pinned_directory()
    roster = pp.Roster(record["pool_id"], pp.canonical_digest(record))
    for n, (peer, identity) in enumerate(((WORKER, WORKER_ID),
                                          (ADMITTER, ADMITTER_ID))):
        verdict = pp.admit(
            record, identity_join(record, peer, identity, nonce=f"n-{n}"),
            charter_key=OP_KEY, peer_keys={}, directory=directory,
            admitting_key_id=key_id(OP_KEY), roster=roster)
        assert verdict["verdict"] == pp.ADMIT, verdict
    return record, roster, directory


def honest_receipt(record, *, task_id="task-1", result=RESULT,
                   identity=WORKER_ID, artifact=GOOD_ARTIFACT, at=None):
    return pr.issue_receipt(pool_id=record["pool_id"], task_id=task_id,
                            artifact_digest=artifact, result=result,
                            identity=identity, at=at)


def honest_pair(record, **kwargs):
    receipt = honest_receipt(record, **kwargs)
    return receipt, pr.attest_receipt(receipt, identity=ATTESTOR_ID)


def signed_body(body, identity, domain):
    """A receipt assembled member by member, so a test can put something in it
    that `issue_receipt` would not."""
    return pi.sign_record(domain, body, identity)


def check(pair, record, roster, directory, **kwargs):
    member = roster.members[kwargs.pop("peer_id", WORKER)]
    charter = pp.charter_from_record(record)
    return pr.check_receipt(
        pair[0], pair[1], pool_id=charter.pool_id, peer_id=member.peer_id,
        artifact_digest=member.artifact_digest,
        admitted_at=member.admitted_at,
        attest_key_ids=charter.attest_key_ids, directory=directory, **kwargs)


# ---------------------------------------------------------------------------
# 1. the exit test, clause by clause
# ---------------------------------------------------------------------------


def test_two_peers_join_one_pool_each_attributable_to_its_own_key():
    """Exit-test clause 1: two machines join a pool.

    What this does NOT reach: these are two peers in one process, not two
    machines. Nothing here opens a socket, and the pool cannot yet hand a
    member work over a wire (see the PR body for what that actually needs).
    What IS real is that each membership is attributable to a distinct key
    pair, which is the property the machine boundary would rest on."""
    _, roster, _ = joined_pool()
    assert set(roster.members) == {WORKER, ADMITTER}
    for peer, identity in ((WORKER, WORKER_ID), (ADMITTER, ADMITTER_ID)):
        member = roster.members[peer]
        assert member.identity == pp.IDENTITY_ASYMMETRIC
        assert member.key_id == identity.key_id
        assert member.tier == pp.ENTRY_TIER
        assert member.evidence == 0, "a peer joins with no evidence"
    assert roster.members[WORKER].key_id != roster.members[ADMITTER].key_id


def test_a_result_is_verified_by_hash_and_by_receipt():
    """Exit-test clause 3: the result is verified by hash AND by receipt.

    Two separate checks, kept separate because they fail for different
    reasons. The hash says the bytes in hand are the bytes the receipt is
    about; the receipt says the peer signed for them."""
    record, roster, directory = joined_pool()
    pair = honest_pair(record)

    ok, reason = pr.verify_result(pair[0], RESULT)
    assert ok, reason
    bad, reason = pr.verify_result(pair[0], {"rows": 4, "checksum": "ok"})
    assert not bad
    assert "hashes to" in reason

    verdict = check(pair, record, roster, directory)
    assert verdict.counts
    assert verdict.peer.confers_authority
    assert verdict.attestor.confers_authority
    assert verdict.attestor.key_id == ATTESTOR_ID.key_id


def test_a_peer_leaving_mid_task_leaves_the_ledger_in_a_stated_state():
    """Exit-test clause 4: a peer leaves mid-task and the ledger says what
    that did. Three disjoint words, and the receipts it delivered are in
    `retained` because withdrawing a peer does not un-observe its work."""
    record, roster, directory = joined_pool()
    counted = pr.count_evidence(
        [honest_pair(record, task_id=f"task-{n}") for n in range(3)],
        pool_id=record["pool_id"], peer_id=WORKER,
        artifact_digest=GOOD_ARTIFACT,
        admitted_at=roster.members[WORKER].admitted_at,
        attest_key_ids=(ATTESTOR_ID.key_id,), directory=directory)
    promotion = pp.promote(record, WORKER, "replayable", charter_key=OP_KEY,
                           roster=roster,
                           receipts=[honest_pair(record, task_id=f"task-{n}")
                                     for n in range(3)],
                           directory=directory)
    assert promotion["verdict"] == pp.PROMOTE, promotion
    assert counted.counted == 3

    # It leaves with work still outstanding.
    roster.outstanding[WORKER] = ["task-7", "task-9"]
    receipt = pp.withdraw(record, WORKER, "host seized", charter_key=OP_KEY,
                          roster=roster, directory=directory,
                          revoking_key_id=key_id(OP_KEY))
    assert receipt["verdict"] == pp.WITHDRAW

    # The three words, and they are disjoint.
    assert receipt["retained"]["evidence"] == 3
    assert sorted(receipt["orphaned"]) == ["task-7", "task-9"]
    assert WORKER not in roster.members
    # The second peer is untouched: a withdrawal names one member.
    assert ADMITTER in roster.members

    # The admission is still in the ledger. A withdrawn peer and a peer that
    # never joined must not render the same.
    kinds = [e.get("verdict") for e in roster.events]
    assert pp.ADMIT in kinds and pp.WITHDRAW in kinds


# ---------------------------------------------------------------------------
# 2. non-vacuity: the forgery corpus
# ---------------------------------------------------------------------------


def corpus(record, roster):
    """Receipts that would ALL have promoted a peer under the old gate, which
    read a caller-supplied integer and never looked at a receipt.

    Each entry is ``(name, expected_link, build)``."""
    pool_id = record["pool_id"]
    admitted = roster.members[WORKER].admitted_at

    def intruder_signed():
        """The intruder signs a receipt in the worker's name. Its key is well
        formed and nobody pinned it."""
        receipt = pr.issue_receipt(
            pool_id=pool_id, task_id="task-1", artifact_digest=GOOD_ARTIFACT,
            result=RESULT, identity=INTRUDER_ID)
        receipt = {**receipt, "peer_id": WORKER}
        return receipt, pr.attest_receipt(receipt, identity=ATTESTOR_ID)

    def tampered():
        """An honest receipt whose result is swapped after signing. The point
        of signing a digest rather than a description."""
        receipt, attestation = honest_pair(record)
        forged = {**receipt, "result_digest": pr.result_digest({"rows": 99})}
        return forged, attestation

    def wrong_artifact():
        return honest_pair(record, artifact=OTHER_ARTIFACT)

    def wrong_pool():
        receipt = signed_body(
            {"kind": pr.RECEIPT_KIND, "version": pr.RECEIPT_VERSION,
             "pool_id": "some-other-pool", "peer_id": WORKER,
             "task_id": "task-1", "artifact_digest": GOOD_ARTIFACT,
             "result_digest": pr.result_digest(RESULT),
             "issued_at": pp._iso(pp._utc_now())},
            WORKER_ID, pr.RECEIPT_DOMAIN)
        return receipt, pr.attest_receipt(receipt, identity=ATTESTOR_ID)

    def wrong_peer():
        """Signed by the admitter, presented as the worker's."""
        receipt = pr.issue_receipt(
            pool_id=pool_id, task_id="task-1", artifact_digest=GOOD_ARTIFACT,
            result=RESULT, identity=ADMITTER_ID)
        return receipt, pr.attest_receipt(receipt, identity=ATTESTOR_ID)

    def another_peer_in_the_body():
        """Signed by the worker's OWN pinned key, naming the admitter. The
        signature arithmetic is real, so this is refused on the binding rather
        than on the arithmetic, which is the distinction the two links keep
        apart."""
        receipt = signed_body(
            {"kind": pr.RECEIPT_KIND, "version": pr.RECEIPT_VERSION,
             "pool_id": pool_id, "peer_id": ADMITTER, "task_id": "task-1",
             "artifact_digest": GOOD_ARTIFACT,
             "result_digest": pr.result_digest(RESULT),
             "issued_at": pp._iso(pp._utc_now())},
            WORKER_ID, pr.RECEIPT_DOMAIN)
        return receipt, pr.attest_receipt(receipt, identity=ATTESTOR_ID)

    def stale():
        """Issued before this membership began. Evidence a previous
        incarnation earned does not carry across a withdrawal."""
        before = pp._parse_iso(admitted) - timedelta(days=1)
        return honest_pair(record, at=before)

    def self_attested():
        receipt = honest_receipt(record)
        return receipt, pr.attest_receipt(receipt, identity=WORKER_ID)

    def unauthorised_attestor():
        """Attested by a peer whose key IS pinned and is not named by the
        charter as attesting authority."""
        receipt = honest_receipt(record)
        return receipt, pr.attest_receipt(receipt, identity=ADMITTER_ID)

    def forged_attestation():
        receipt = honest_receipt(record)
        attestation = pr.attest_receipt(receipt, identity=INTRUDER_ID)
        return receipt, {**attestation, "attestor": ATTESTOR}

    def swapped_attestation():
        """A real attestation over a DIFFERENT receipt, moved onto this one."""
        receipt = honest_receipt(record, task_id="task-1")
        other = honest_receipt(record, task_id="task-2")
        return receipt, pr.attest_receipt(other, identity=ATTESTOR_ID)

    def refused_verdict():
        receipt = honest_receipt(record)
        return receipt, pr.attest_receipt(receipt, identity=ATTESTOR_ID,
                                          verdict="refused")

    def shapeless():
        return {"kind": "not-a-receipt"}, {"kind": "not-an-attestation"}

    return (
        ("a receipt signed by an unpinned key", pr.LINK_RECEIPT_SIGNATURE,
         intruder_signed),
        ("a result swapped after signing", pr.LINK_RECEIPT_SIGNATURE, tampered),
        ("a receipt for an artifact the peer was not admitted with",
         pr.LINK_RECEIPT_ARTIFACT, wrong_artifact),
        ("a receipt for another pool", pr.LINK_RECEIPT_POOL, wrong_pool),
        ("another peer's receipt presented as this one's",
         pr.LINK_RECEIPT_SIGNATURE, wrong_peer),
        ("a receipt whose body names another peer", pr.LINK_RECEIPT_PEER,
         another_peer_in_the_body),
        ("a receipt predating this membership", pr.LINK_STALE_RECEIPT, stale),
        ("a peer attesting its own work", pr.LINK_SELF_ATTESTATION,
         self_attested),
        ("an attestor the charter does not name",
         pr.LINK_ATTESTATION_AUTHORITY, unauthorised_attestor),
        ("an attestation signed by an unpinned key",
         pr.LINK_ATTESTATION_SIGNATURE, forged_attestation),
        ("an attestation moved from another receipt",
         pr.LINK_RECEIPT_BINDING, swapped_attestation),
        ("an attestation that does not say admitted",
         pr.LINK_ATTESTATION_AUTHORITY, refused_verdict),
        ("a record that is not a receipt", pr.LINK_RECEIPT_SHAPE, shapeless),
    )


def test_every_forged_receipt_is_refused_on_its_named_link():
    """The non-vacuity table. Every entry would have promoted a peer under the
    old gate; every one is refused here, each on a different stated link."""
    record, roster, directory = joined_pool()
    seen = set()
    for name, expected, build in corpus(record, roster):
        verdict = check(build(), record, roster, directory)
        assert not verdict.counts, f"{name} was counted as evidence"
        assert verdict.link == expected, (
            f"{name}: refused on {verdict.link!r}, expected {expected!r} "
            f"({verdict.reason})")
        seen.add(verdict.link)
    assert len(seen) == 10, (
        f"the corpus must exercise distinct links, not one link thirteen "
        f"times; it reached {sorted(seen)}")

    # The two links the corpus cannot reach, reached here, so that between
    # them every declared link is reached AT RUNTIME and not merely named.
    pair = honest_pair(record, task_id="task-solo")
    replayed = pr.count_evidence(
        [pair, pair], pool_id=record["pool_id"], peer_id=WORKER,
        artifact_digest=GOOD_ARTIFACT,
        admitted_at=roster.members[WORKER].admitted_at,
        attest_key_ids=(ATTESTOR_ID.key_id,), directory=directory)
    seen.update(r.link for r in replayed.rejected)

    directory.revoke(WORKER, WORKER_ID.key_id, reason="host seized")
    seen.add(check(pair, record, roster, directory).link)

    assert seen == set(pr.LINKS), (
        f"declared and never reached: {sorted(set(pr.LINKS) - seen)}")


def test_the_honest_control_is_admitted_by_the_same_code_path():
    """The control. It differs from the corpus only in being well formed, and
    it goes through `check_receipt`, not around it. Without this the table
    above is satisfied by a gate that refuses everything."""
    record, roster, directory = joined_pool()
    verdict = check(honest_pair(record), record, roster, directory)
    assert verdict.counts
    assert verdict.link == ""


def test_the_corpus_all_clears_the_check_that_existed_before():
    """The other half of non-vacuity: each forgery is a well-formed signed
    record whose signature arithmetic is real wherever it claims to be, so it
    is refused on POOL grounds and not because it fell apart."""
    record, roster, directory = joined_pool()
    counted = pr.count_evidence(
        [build() for _, _, build in corpus(record, roster)],
        pool_id=record["pool_id"], peer_id=WORKER,
        artifact_digest=GOOD_ARTIFACT,
        admitted_at=roster.members[WORKER].admitted_at,
        attest_key_ids=(ATTESTOR_ID.key_id,), directory=directory)
    assert counted.counted == 0, (
        f"the whole corpus counted {counted.counted} receipts as evidence")
    assert len(counted.rejected) == 13


# ---------------------------------------------------------------------------
# 3. replay
# ---------------------------------------------------------------------------


def test_a_replayed_receipt_counts_once_and_is_recorded_not_dropped():
    """Delivered twice must be impossible or VISIBLE. The second receipt for a
    task counts for nothing and appears in `rejected`, so the ledger can show
    that a second delivery was refused rather than silently absorbed."""
    record, roster, directory = joined_pool()
    pair = honest_pair(record, task_id="task-1")
    counted = pr.count_evidence(
        [pair, pair, pair], pool_id=record["pool_id"], peer_id=WORKER,
        artifact_digest=GOOD_ARTIFACT,
        admitted_at=roster.members[WORKER].admitted_at,
        attest_key_ids=(ATTESTOR_ID.key_id,), directory=directory)
    assert counted.counted == 1
    assert len(counted.rejected) == 2
    assert {r.link for r in counted.rejected} == {pr.LINK_REPLAYED_RECEIPT}


def test_re_signing_the_same_task_does_not_buy_a_second_count():
    """Deduplication is on the TASK, not the receipt digest. A peer can
    re-sign the same work with a new timestamp and get a fresh digest for
    free, so counting distinct digests would let it inflate its own
    evidence at no cost."""
    record, roster, directory = joined_pool()
    now = pp._utc_now()
    pairs = [honest_pair(record, task_id="task-1", at=now + timedelta(seconds=n))
             for n in range(5)]
    digests = {pr.receipt_digest(p[0]) for p in pairs}
    assert len(digests) == 5, "the fixture must really produce 5 digests"
    counted = pr.count_evidence(
        pairs, pool_id=record["pool_id"], peer_id=WORKER,
        artifact_digest=GOOD_ARTIFACT,
        admitted_at=roster.members[WORKER].admitted_at,
        attest_key_ids=(ATTESTOR_ID.key_id,), directory=directory)
    assert counted.counted == 1
    assert counted.task_ids == ("task-1",)


def test_the_count_does_not_depend_on_the_order_receipts_arrive_in():
    record, roster, directory = joined_pool()
    pairs = [honest_pair(record, task_id=f"task-{n}") for n in range(4)]
    args = dict(pool_id=record["pool_id"], peer_id=WORKER,
                artifact_digest=GOOD_ARTIFACT,
                admitted_at=roster.members[WORKER].admitted_at,
                attest_key_ids=(ATTESTOR_ID.key_id,), directory=directory)
    forward = pr.count_evidence(pairs, **args)
    backward = pr.count_evidence(list(reversed(pairs)), **args)
    assert forward.digests == backward.digests
    assert forward.counted == 4


# ---------------------------------------------------------------------------
# 4. the promotion gate recounts
# ---------------------------------------------------------------------------


def test_promotion_recounts_and_cites_the_receipts_it_counted():
    """The positive path, and the citation. A promotion names the receipt
    digests it counted, so a third party can recheck the arithmetic instead of
    being told the answer."""
    record, roster, directory = joined_pool()
    pairs = [honest_pair(record, task_id=f"task-{n}") for n in range(3)]
    receipt = pp.promote(record, WORKER, "replayable", charter_key=OP_KEY,
                         roster=roster, receipts=pairs, directory=directory)
    assert receipt["verdict"] == pp.PROMOTE, receipt
    assert roster.members[WORKER].tier == "replayable"
    assert receipt["evidence"] == 3
    assert receipt["evidence_digests"] == [pr.receipt_digest(p[0])
                                           for p in sorted(
                                               pairs,
                                               key=lambda p: (
                                                   p[0]["issued_at"],
                                                   pr.receipt_digest(p[0])))]
    assert receipt["evidence_key_ids"] == [ATTESTOR_ID.key_id]
    assert roster.members[WORKER].evidence_digests == tuple(
        receipt["evidence_digests"])


def test_two_honest_receipts_and_one_forgery_do_not_reach_three():
    """The measurement that makes the recount load-bearing. Under the old gate
    a caller said `evidence=3` and was believed. Here two real receipts plus
    anything at all is two, and the member stays where it was."""
    record, roster, directory = joined_pool()
    forged = pr.issue_receipt(
        pool_id=record["pool_id"], task_id="task-9",
        artifact_digest=GOOD_ARTIFACT, result=RESULT, identity=INTRUDER_ID)
    pairs = [honest_pair(record, task_id="task-0"),
             honest_pair(record, task_id="task-1"),
             ({**forged, "peer_id": WORKER},
              pr.attest_receipt(forged, identity=ATTESTOR_ID))]
    receipt = pp.promote(record, WORKER, "replayable", charter_key=OP_KEY,
                         roster=roster, receipts=pairs, directory=directory)
    assert receipt["link"] == pp.LINK_PROMOTION_EVIDENCE
    assert receipt["has"] == 2 and receipt["requires"] == 3
    assert receipt["stays_at"] == pp.ENTRY_TIER
    assert roster.members[WORKER].tier == pp.ENTRY_TIER
    assert pr.LINK_RECEIPT_SIGNATURE in receipt["refused"]


def test_a_promotion_with_no_directory_counts_nothing():
    """The fail-closed direction of the new parameter. A caller that omits the
    directory has given the gate no way to attribute a receipt, and the answer
    to that is zero evidence, not unchecked evidence."""
    record, roster, _ = joined_pool()
    pairs = [honest_pair(record, task_id=f"task-{n}") for n in range(3)]
    receipt = pp.promote(record, WORKER, "replayable", charter_key=OP_KEY,
                         roster=roster, receipts=pairs)
    assert receipt["link"] == pp.LINK_PROMOTION_EVIDENCE
    assert receipt["has"] == 0


# ---------------------------------------------------------------------------
# 5. the attribution shape (issue #1278)
# ---------------------------------------------------------------------------


def test_a_receipt_under_a_revoked_key_verified_and_confers_nothing():
    """Issue #1278's shape, which this must not flatten. `verified` is
    arithmetic and never changes once true; `status` carries the revocation.
    A revoked peer's receipt is a TRUE statement about work that really
    happened and it confers no authority, and an operator auditing the ledger
    has to be able to tell that from a forgery."""
    record, roster, directory = joined_pool()
    pair = honest_pair(record)

    before = check(pair, record, roster, directory)
    assert before.counts

    directory.revoke(WORKER, WORKER_ID.key_id, reason="host seized")
    after = check(pair, record, roster, directory)

    assert after.peer.verified is True, (
        "the signature arithmetic did not stop being true")
    assert after.peer.status == pi.KEY_REVOKED
    assert after.peer.confers_authority is False
    assert not after.counts
    assert after.link == pr.LINK_RECEIPT_KEY_AUTHORITY

    # And it is NOT the same finding as a forgery, which is the whole point.
    forged = check(({**pair[0], "result_digest": pr.result_digest({"x": 1})},
                    pair[1]), record, roster, directory)
    assert forged.peer.verified is False
    assert forged.link != after.link


def test_a_receipt_check_is_never_reduced_to_a_boolean():
    """Structural, not behavioural. `ReceiptCheck` carries both attributions
    and `counts` is the conjunction spelled out, so a reader cannot mistake
    which of the two questions a bare boolean answered."""
    record, roster, directory = joined_pool()
    verdict = check(honest_pair(record), record, roster, directory)
    assert isinstance(verdict.peer, pi.Attribution)
    assert isinstance(verdict.attestor, pi.Attribution)
    assert verdict.counts == (
        not verdict.link and verdict.peer.confers_authority
        and verdict.attestor.confers_authority)
    rendered = verdict.as_dict()
    for half in ("peer", "attestor"):
        assert set(rendered[half]) >= {"verified", "status",
                                       "confers_authority"}


# ---------------------------------------------------------------------------
# 6. what a shared-key member can prove, which is nothing
# ---------------------------------------------------------------------------


def test_a_shared_key_member_can_accumulate_no_evidence():
    """A receipt is an asymmetric record, so a member that joined under a
    shared key can produce no countable receipt and cannot be promoted above
    the entry tier.

    That is deliberate and it is the argument for tightening
    `PoolCharter.identity_mode`: an HMAC authenticates under a secret the pool
    ALSO holds, so it cannot show that the peer produced a result rather than
    the pool itself, and evidence the verifier could have manufactured is not
    evidence."""
    shared = b"shared-secret/worker"
    record = make_charter(identity_mode=pp.MODE_MIXED)
    directory = pi.IdentityDirectory()
    directory.register(ATTESTOR_ID.public())
    roster = pp.Roster(record["pool_id"], pp.canonical_digest(record))
    body = pp.JoinRequest(
        pool_id=record["pool_id"],
        charter_digest=pp.canonical_digest(record),
        peer_id=WORKER,
        offer=peer_offer.sign_offer(make_offer(WORKER), shared),
        artifact_digest=GOOD_ARTIFACT, nonce="n-shared",
        issued_at=pp._iso(pp._utc_now()))
    verdict = pp.admit(record, pp.sign_join(body, shared), charter_key=OP_KEY,
                       peer_keys={WORKER: shared}, directory=directory,
                       admitting_key_id=key_id(OP_KEY), roster=roster)
    assert verdict["verdict"] == pp.ADMIT, verdict
    assert roster.members[WORKER].identity == pp.IDENTITY_SHARED_KEY

    # Its best effort at a receipt: the pool holds the same secret, so there is
    # no key pair for it to sign with that anybody pinned.
    pairs = [honest_pair(record, task_id=f"task-{n}") for n in range(3)]
    promotion = pp.promote(record, WORKER, "replayable", charter_key=OP_KEY,
                           roster=roster, receipts=pairs, directory=directory)
    assert promotion["link"] == pp.LINK_PROMOTION_EVIDENCE
    assert promotion["has"] == 0
    assert roster.members[WORKER].tier == pp.ENTRY_TIER


# ---------------------------------------------------------------------------
# 7. link honesty
# ---------------------------------------------------------------------------


def test_every_declared_link_is_named_by_some_refusal_in_the_module():
    """No link is declared and left unreachable (which reads as a check that
    exists and does not), and none is passed that was not declared."""
    tree = ast.parse(Path(pr.__file__).read_text(encoding="utf-8"))
    passed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("LINK_"):
            passed.add(node.id)
    declared = {name for name in dir(pr) if name.startswith("LINK_")}
    assert declared - passed == set(), (
        f"declared and never passed: {sorted(declared - passed)}")
    assert len(pr.LINKS) == len(declared)
    assert len(set(pr.LINKS)) == len(pr.LINKS)


def test_the_checker_returns_a_verdict_for_anything_at_all():
    """A hostile record must not be able to choose a traceback over a
    refusal: the one path that must not raise is the refusal path."""
    record, roster, directory = joined_pool()
    for junk in (None, 3, "receipt", [], {"kind": pr.RECEIPT_KIND},
                 {"kind": pr.RECEIPT_KIND, "pool_id": object()}):
        verdict = check((junk, junk), record, roster, directory)
        assert not verdict.counts
        assert verdict.link

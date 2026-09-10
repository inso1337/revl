"""Attested-TEE placement and signed results (roadmap item 475, issue #827).

These pin the exit criterion: a placement whose requirements include an attested
trusted-execution environment is REFUSED unless the peer proves, by remote
attestation, that it runs the approved bundle inside a permitted enclave, in a
permitted region, with the outbound network forbidden, and the composition
receives a result only when the receipt binds those bytes to that attested run.

The refusals are the point, so most of this file is one. The load-bearing one is
:func:`test_proof_signed_with_the_peers_own_key_is_refused`: symmetric crypto
proves only that the holder of a key authored a record, so an attestation the
peer can mint is worth exactly as much as the peer's own assertion, and the
verifier has to say so. Then the tampered, wrong-bundle, wrong-enclave,
wrong-region, wrong-challenge, stale, expired, future-dated and replayed proofs
each get their own refusal, and the last section drives the whole thing through
``peer_offer.offer_eligible`` so the accept path of an attested slot is shown
unreachable by assertion alone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import attest, tee_attestation as tee  # noqa: E402
from revl.peer_offer import (  # noqa: E402
    Attestation,
    OfferError,
    PeerOffer,
    PlacementSlot,
    offer_eligible,
    sign_offer,
    verify_offer,
)
from revl.tee_attestation import (  # noqa: E402
    EnclaveEvidence,
    ResultReceipt,
    TeeError,
    TeeRequirement,
    receipt_admits,
    sign_evidence,
    sign_result_receipt,
    tee_admits,
    verify_evidence,
    verify_result_receipt,
)

# Two DIFFERENT keys, and the whole security story lives in the difference: the
# peer holds PEER_KEY (it signs its own offer), the attestation authority holds
# ATTESTER_KEY (it signs what the enclave is), the enclave holds ENCLAVE_KEY (it
# signs the result it produced).
PEER_KEY = b"pool-shared-secret"
ATTESTER_KEY = b"attestation-authority-key"
ENCLAVE_KEY = b"enclave-result-signing-key"

PEER_ID = "peer-1"
OTHER_PEER_ID = "peer-2"
MEASUREMENT = "ab" * 32
OTHER_MEASUREMENT = "cd" * 32
REGION = "eu-central"
OTHER_REGION = "us-east"
NONCE = "challenge-827-0001"

BUNDLE = attest.canonical_hash({"bundle": "payments", "revision": 3})
OTHER_BUNDLE = attest.canonical_hash({"bundle": "other", "revision": 1})
RESULT = {"settled": True, "count": 3}
OTHER_RESULT = {"settled": True, "count": 4}

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def make_requirement(**overrides) -> TeeRequirement:
    fields = {
        "bundle": BUNDLE,
        "measurements": frozenset({MEASUREMENT}),
        "regions": frozenset({REGION}),
        "nonce": NONCE,
        "max_age_s": 120.0,
    }
    fields.update(overrides)
    return TeeRequirement(**fields)


def make_evidence(**overrides) -> EnclaveEvidence:
    fields = {
        "peer_id": PEER_ID,
        "bundle": BUNDLE,
        "measurement": MEASUREMENT,
        "region": REGION,
        "nonce": NONCE,
        "issued_at": tee.stamp(NOW - timedelta(seconds=5)),
        "expires_at": tee.stamp(NOW + timedelta(seconds=60)),
    }
    fields.update(overrides)
    return EnclaveEvidence(**fields)


def make_proof(**overrides) -> dict:
    return sign_evidence(make_evidence(**overrides), ATTESTER_KEY)


def admits(record, requirement=None, **overrides):
    """``tee_admits`` with every gate configured for acceptance, so a test only
    has to change the one fact it is about. The ledger defaults to a fresh set
    per call; a test that cares about replay passes its own."""
    fields = {
        "peer_id": PEER_ID,
        "peer_key": PEER_KEY,
        "attester_key": ATTESTER_KEY,
        "now": NOW,
        "replay_ledger": set(),
    }
    fields.update(overrides)
    return tee_admits(record, requirement or make_requirement(), **fields)


def make_receipt(**overrides) -> dict:
    fields = {
        "peer_id": PEER_ID,
        "bundle": BUNDLE,
        "nonce": NONCE,
        "result_hash": tee.result_identity(RESULT),
        "issued_at": tee.stamp(NOW - timedelta(seconds=5)),
    }
    fields.update(overrides)
    return sign_result_receipt(ResultReceipt(**fields), ENCLAVE_KEY)


def accepts(record, *, result=RESULT, **overrides):
    fields = {
        "enclave_key": ENCLAVE_KEY,
        "peer_key": PEER_KEY,
        "peer_id": PEER_ID,
        "bundle": BUNDLE,
        "nonce": NONCE,
        "now": NOW,
        "replay_ledger": set(),
    }
    fields.update(overrides)
    return receipt_admits(record, result=result, **fields)


# --------------------------------------------------------- the accept path


def test_signed_evidence_is_admitted():
    ok, reason = admits(make_proof())
    assert ok, reason
    assert reason.startswith("admitted")


def test_signing_evidence_is_deterministic():
    assert make_proof() == make_proof()


def test_key_id_is_the_attesters_and_discloses_no_key():
    record = make_proof()
    assert record["key_id"] == attest.key_id(ATTESTER_KEY)
    assert ATTESTER_KEY.decode() not in json.dumps(record)


def test_evidence_survives_a_json_round_trip():
    """The proof crosses a wire, so the MAC must be over bytes a JSON round trip
    reproduces exactly (that is what ``attest._canonical_bytes`` buys)."""
    wire = json.loads(json.dumps(make_proof()))
    ok, reason = admits(wire)
    assert ok, reason


def test_verify_evidence_alone_checks_authenticity_not_the_placement():
    """``verify_evidence`` answers "is this authentic", never "is this for my
    slot": an authentic proof for another region still verifies."""
    record = make_proof(region=OTHER_REGION)
    ok, reason = verify_evidence(record, ATTESTER_KEY)
    assert ok, reason
    refused, why = admits(record)
    assert not refused and OTHER_REGION in why


# --------------------------------------------------- nothing in, nothing out


def test_absent_proof_is_refused():
    for absent in (None, "proof", 7, ["proof"]):
        ok, reason = admits(absent)
        assert not ok and "not an object" in reason
    ok, reason = admits({})
    assert not ok and "no attestation signature" in reason


def test_unsigned_evidence_is_refused():
    body = make_evidence().body()
    ok, reason = verify_evidence(body, ATTESTER_KEY)
    assert not ok and "no attestation signature" in reason
    assert not admits(body)[0]


def test_no_attester_key_is_refused():
    ok, reason = admits(make_proof(), attester_key=None)
    assert not ok and "no attester key" in reason


def test_evidence_for_an_unknown_peer_is_refused():
    ok, reason = admits(make_proof(peer_id=OTHER_PEER_ID))
    assert not ok and OTHER_PEER_ID in reason and PEER_ID in reason


# ------------------------------------------------- the load-bearing refusal


def test_proof_signed_with_the_peers_own_key_is_refused():
    """If the peer holds the attester key, the "proof" is the peer's assertion
    wearing a MAC, so the requirement must refuse it rather than admit it."""
    self_signed = sign_evidence(make_evidence(), PEER_KEY)
    ok, reason = admits(self_signed, attester_key=PEER_KEY)
    assert not ok
    assert "peer's own offer key" in reason


def test_proof_the_attester_key_does_not_vouch_for_is_refused():
    """The same record, checked against the authority's key: the peer signed a
    proof it cannot make anyone believe, because it is not the authority."""
    self_signed = sign_evidence(make_evidence(), PEER_KEY)
    ok, reason = admits(self_signed)
    assert not ok and "signature mismatch" in reason


def test_a_proof_with_no_peer_key_is_refused():
    """The separation is the load-bearing gate, so a caller that names no peer
    key cannot be shown to have passed it. ``None`` and the empty key are refused
    explicitly rather than compared against an empty key, which would have made
    the comparison vacuously false and admitted the proof."""
    for absent in (None, b"", bytearray()):
        ok, reason = admits(make_proof(), peer_key=absent)
        assert not ok, absent
        assert "no peer key" in reason, absent


def test_a_proof_with_a_non_bytes_peer_key_is_refused():
    """A key of the wrong TYPE is absent, not comparable: the old
    ``bytes(key or b"")`` comparison raised on a ``str``."""
    ok, reason = admits(make_proof(), peer_key="pool-shared-secret")
    assert not ok and "no peer key" in reason


def test_receipt_signed_with_the_peers_own_key_is_refused():
    self_signed = sign_result_receipt(
        ResultReceipt(PEER_ID, BUNDLE, NONCE, tee.result_identity(RESULT),
                      tee.stamp(NOW)), PEER_KEY)
    ok, reason = accepts(self_signed, enclave_key=PEER_KEY)
    assert not ok and "peer's own offer key" in reason


def test_a_receipt_with_no_peer_key_is_refused():
    for absent in (None, b"", bytearray()):
        ok, reason = accepts(make_receipt(), peer_key=absent)
        assert not ok, absent
        assert "no peer key" in reason, absent


# --------------------------------------------------------- tampered proofs


def test_tampered_measurement_breaks_the_proof():
    record = make_proof()
    record["measurement"] = OTHER_MEASUREMENT
    ok, reason = admits(record)
    assert not ok and "signature mismatch" in reason


def test_tampered_region_breaks_the_proof():
    record = make_proof()
    record["region"] = OTHER_REGION
    ok, reason = admits(record)
    assert not ok and "signature mismatch" in reason


def test_tampered_bundle_breaks_the_proof():
    record = make_proof()
    record["bundle"] = OTHER_BUNDLE
    ok, reason = admits(record)
    assert not ok and "signature mismatch" in reason


def test_tampered_expiry_breaks_the_proof():
    """The freshness window is inside the MAC, so an expired proof cannot be
    edited into a live one."""
    record = make_proof(issued_at=tee.stamp(NOW - timedelta(seconds=200)),
                        expires_at=tee.stamp(NOW - timedelta(seconds=120)))
    assert not admits(record)[0]
    record["expires_at"] = tee.stamp(NOW + timedelta(seconds=600))
    ok, reason = admits(record)
    assert not ok and "signature mismatch" in reason


def test_swapping_the_key_id_breaks_the_proof():
    record = make_proof()
    record["key_id"] = attest.key_id(PEER_KEY)
    ok, reason = admits(record)
    assert not ok and "signature mismatch" in reason


def test_an_envelope_from_another_protocol_is_refused_even_when_authentic():
    """A MAC proves authorship, not meaning. A record the attester signed with
    its own key but that does not DECLARE itself enclave evidence is refused on
    the envelope, so nothing can be reinterpreted as a proof."""
    body = dict(make_evidence().body(), kind="revl.peer-offer")
    body["key_id"] = attest.key_id(ATTESTER_KEY)
    signed = {k: v for k, v in body.items() if k != "signature"}
    body["signature"] = hmac.new(
        ATTESTER_KEY,
        tee.EVIDENCE_SIGN_DOMAIN + attest._canonical_bytes(signed),
        hashlib.sha256).hexdigest()
    ok, reason = verify_evidence(body, ATTESTER_KEY)
    assert not ok and "revl.peer-offer" in reason


def test_domain_separation_from_a_peer_offer():
    """A signed offer is not enclave evidence, and enclave evidence is not an
    offer: the two protocols share a primitive and must not share a signature."""
    offer = sign_offer(PeerOffer(peer_id=PEER_ID, attestation=Attestation("local")),
                       ATTESTER_KEY)
    ok, reason = verify_evidence(offer, ATTESTER_KEY)
    assert not ok and "signature mismatch" in reason
    ok, reason = verify_offer(make_proof(), PEER_KEY)
    assert not ok and "signature mismatch" in reason


# ------------------------------------------- the wrong bundle, enclave, place


def test_proof_for_another_bundle_is_refused():
    ok, reason = admits(make_proof(bundle=OTHER_BUNDLE))
    assert not ok and OTHER_BUNDLE in reason and BUNDLE in reason


def test_proof_for_an_enclave_outside_the_permitted_set_is_refused():
    ok, reason = admits(make_proof(measurement=OTHER_MEASUREMENT))
    assert not ok and OTHER_MEASUREMENT in reason


def test_proof_from_another_region_is_refused():
    ok, reason = admits(make_proof(region=OTHER_REGION))
    assert not ok and OTHER_REGION in reason and REGION in reason


def test_proof_without_a_region_is_refused():
    """An evidence that declines to name a place is not evidence of a place."""
    ok, reason = admits(make_proof(region=""))
    assert not ok and "''" in reason


def test_proof_answering_another_challenge_is_refused():
    ok, reason = admits(make_proof(nonce="challenge-somewhere-else"))
    assert not ok and "challenge" in reason


# ------------------------------------------------------------- outbound net


def test_outbound_network_allowed_is_refused_under_a_forbidden_requirement():
    ok, reason = admits(make_proof(outbound_network="allowed"))
    assert not ok and "allowed" in reason and "forbidden" in reason


def test_a_requirement_cannot_ask_for_an_open_network():
    """``forbidden`` is the only posture this item defines: anything else asks
    for a worker whose egress is unchecked, which is not the placement."""
    with pytest.raises(TeeError, match="outbound_network"):
        make_requirement(outbound_network="allowed")


def test_an_unknown_network_posture_is_refused_at_build_time():
    with pytest.raises(TeeError, match="outbound_network"):
        make_evidence(outbound_network="maybe")


# --------------------------------------------------------------- freshness


def test_expired_proof_is_refused():
    expired = make_proof(issued_at=tee.stamp(NOW - timedelta(seconds=200)),
                         expires_at=tee.stamp(NOW - timedelta(seconds=60)))
    ok, reason = admits(expired)
    assert not ok and "expired" in reason


def test_stale_proof_is_refused():
    old = make_proof(issued_at=tee.stamp(NOW - timedelta(seconds=300)),
                     expires_at=tee.stamp(NOW + timedelta(seconds=600)))
    ok, reason = admits(old, requirement=make_requirement(max_age_s=60.0))
    assert not ok and "max_age_s" in reason


def test_future_dated_proof_is_refused():
    """A stamp ahead of the verifier's clock is a self-issued forgery, not a
    clock skew: proof of a run that has not happened yet is no proof at all."""
    future = make_proof(issued_at=tee.stamp(NOW + timedelta(seconds=600)),
                        expires_at=tee.stamp(NOW + timedelta(seconds=900)))
    ok, reason = admits(future)
    assert not ok and "ahead of" in reason


def test_a_requirement_cannot_accept_a_proof_forever():
    for unbounded in (0, 0.0, -1, None, True):
        with pytest.raises(TeeError, match="max_age_s"):
            make_requirement(max_age_s=unbounded)


# ------------------------------------------------------------------ replay


def test_replayed_proof_is_refused():
    record = make_proof()
    ledger: set = set()
    first, reason = admits(record, replay_ledger=ledger)
    assert first, reason
    second, reason = admits(record, replay_ledger=ledger)
    assert not second and "replay" in reason
    # the same proof against a caller that has consumed nothing is admitted,
    # which isolates the refusal above to the ledger rather than the proof.
    assert admits(record, replay_ledger=set())[0]


def test_replay_ledger_is_required():
    ok, reason = admits(make_proof(), replay_ledger=None)
    assert not ok and "no replay ledger" in reason


def test_a_refused_proof_does_not_consume_the_challenge():
    """Fail-closed must not mean fail-destructive: an offer refused for an
    unrelated reason must leave the honest peer's challenge answerable."""
    ledger: set = set()
    refused, _ = admits(make_proof(region=OTHER_REGION), replay_ledger=ledger)
    assert not refused and ledger == set()
    admitted, reason = admits(make_proof(), replay_ledger=ledger)
    assert admitted, reason
    assert ledger == {(NONCE, PEER_ID)}


def test_the_challenge_is_consumed_per_peer():
    ledger: set = set()
    assert admits(make_proof(), replay_ledger=ledger)[0]
    assert admits(make_proof(peer_id=OTHER_PEER_ID), peer_id=OTHER_PEER_ID,
                  replay_ledger=ledger)[0]
    assert ledger == {(NONCE, PEER_ID), (NONCE, OTHER_PEER_ID)}


# ----------------------------------------------- the requirement is checkable


def test_a_requirement_without_a_permitted_measurement_is_refused():
    with pytest.raises(TeeError, match="permitted measurement"):
        make_requirement(measurements=frozenset())


def test_a_requirement_without_a_permitted_region_is_refused():
    with pytest.raises(TeeError, match="permitted region"):
        make_requirement(regions=frozenset())


def test_a_requirement_without_a_challenge_is_refused():
    with pytest.raises(TeeError, match="nonce"):
        make_requirement(nonce="")


def test_a_requirement_names_a_bundle_by_content_hash():
    with pytest.raises(TeeError, match="content hash"):
        make_requirement(bundle="payments-v3")
    with pytest.raises(TeeError, match="content hash"):
        make_requirement(bundle=BUNDLE.upper())


def test_a_requirement_refuses_a_measurement_that_is_not_a_digest():
    with pytest.raises(TeeError, match="hex digest"):
        make_requirement(measurements=frozenset({"not-a-measurement"}))


def test_bundle_identity_is_the_canonical_content_hash():
    assert tee.bundle_identity({"bundle": "payments", "revision": 3}) == BUNDLE
    with pytest.raises(TeeError, match="canonical"):
        tee.bundle_identity({"unhashable": object()})


def test_requirement_normalizes_its_permitted_sets():
    requirement = make_requirement(measurements=[MEASUREMENT],
                                   regions={REGION})
    assert requirement.measurements == frozenset({MEASUREMENT})
    assert requirement.regions == frozenset({REGION})


# ------------------------------------------------------- signed results back


def test_signed_result_receipt_is_admitted():
    ok, reason = accepts(make_receipt())
    assert ok, reason
    assert reason.startswith("admitted")


def test_result_receipt_survives_a_json_round_trip():
    ok, reason = accepts(json.loads(json.dumps(make_receipt())))
    assert ok, reason


def test_tampered_result_receipt_is_refused():
    record = make_receipt()
    record["result_hash"] = tee.result_identity(OTHER_RESULT)
    ok, reason = accepts(record)
    assert not ok and "signature mismatch" in reason


def test_a_receipt_for_another_result_is_refused():
    """The composition recomputes the hash over the bytes it actually received,
    so a genuine receipt for a different result cannot be laundered onto this
    one."""
    ok, reason = accepts(make_receipt(result_hash=tee.result_identity(OTHER_RESULT)))
    assert not ok and "not the result that arrived" in reason


def test_a_receipt_for_another_run_is_refused():
    ok, reason = accepts(make_receipt(nonce="challenge-somewhere-else"))
    assert not ok and "challenge" in reason


def test_a_receipt_for_another_bundle_is_refused():
    ok, reason = accepts(make_receipt(bundle=OTHER_BUNDLE))
    assert not ok and OTHER_BUNDLE in reason


def test_a_receipt_from_another_peer_is_refused():
    ok, reason = accepts(make_receipt(peer_id=OTHER_PEER_ID))
    assert not ok and OTHER_PEER_ID in reason


def test_an_unsigned_receipt_is_refused():
    body = ResultReceipt(PEER_ID, BUNDLE, NONCE, tee.result_identity(RESULT),
                         tee.stamp(NOW)).body()
    ok, reason = verify_result_receipt(body, ENCLAVE_KEY)
    assert not ok and "no signature" in reason
    assert not accepts(body)[0]


def test_a_stale_receipt_is_refused():
    old = make_receipt(issued_at=tee.stamp(NOW - timedelta(seconds=600)))
    ok, reason = accepts(old, max_age_s=60.0)
    assert not ok and "max_age_s" in reason


def test_a_replayed_receipt_is_refused():
    record = make_receipt()
    ledger: set = set()
    assert accepts(record, replay_ledger=ledger)[0]
    ok, reason = accepts(record, replay_ledger=ledger)
    assert not ok and "replay" in reason


def test_a_receipt_without_a_delivery_ledger_is_refused():
    ok, reason = accepts(make_receipt(), replay_ledger=None)
    assert not ok and "no delivery ledger" in reason


def test_domain_separation_between_receipt_and_evidence():
    ok, reason = verify_result_receipt(make_proof(), ENCLAVE_KEY)
    assert not ok and "signature mismatch" in reason
    ok, reason = verify_evidence(make_receipt(), ENCLAVE_KEY)
    assert not ok and "signature mismatch" in reason


# ------------------------------------ the typed requirement on a real slot


def slot(**overrides) -> PlacementSlot:
    fields = {"trust_floor": "attested", "attested_tee": make_requirement()}
    fields.update(overrides)
    return PlacementSlot(**fields)


def make_offer(trust="attested", proof=None, peer_id=PEER_ID) -> dict:
    return sign_offer(
        PeerOffer(peer_id=peer_id, attestation=Attestation(trust=trust),
                  tee_proof=proof),
        PEER_KEY)


def eligible(record, placement=None, **overrides):
    fields = {"attester_key": ATTESTER_KEY, "tee_ledger": set(), "now": NOW}
    fields.update(overrides)
    return offer_eligible(record, placement or slot(), PEER_KEY, **fields)


def test_an_asserted_trust_level_does_not_fill_an_attested_slot():
    """Fail-before, pass-after: the same offer, claiming ``attested``, is refused
    with no proof and eligible with one. The claim is worth nothing."""
    before, reason = eligible(make_offer(trust="attested"))
    assert not before and "attestation:" in reason and "no enclave evidence" in reason
    after, reason = eligible(make_offer(trust="attested", proof=make_proof()))
    assert after, reason


def test_an_attested_slot_refuses_a_proof_for_another_bundle():
    ok, reason = eligible(make_offer(proof=make_proof(bundle=OTHER_BUNDLE)))
    assert not ok and "attestation:" in reason and OTHER_BUNDLE in reason


def test_an_attested_slot_refuses_a_proof_for_another_region():
    ok, reason = eligible(make_offer(proof=make_proof(region=OTHER_REGION)))
    assert not ok and "attestation:" in reason and OTHER_REGION in reason


def test_an_attested_slot_refuses_a_tampered_proof():
    proof = make_proof()
    proof["measurement"] = OTHER_MEASUREMENT
    ok, reason = eligible(make_offer(proof=proof))
    assert not ok and "attestation:" in reason and "tampered" in reason


def test_an_attested_slot_refuses_a_replayed_proof():
    record = make_offer(proof=make_proof())
    ledger: set = set()
    first, reason = eligible(record, tee_ledger=ledger)
    assert first, reason
    second, reason = eligible(record, tee_ledger=ledger)
    assert not second and "replay" in reason


def test_an_attested_slot_refuses_without_a_ledger():
    ok, reason = eligible(make_offer(proof=make_proof()), tee_ledger=None)
    assert not ok and "no replay ledger" in reason


def test_an_attested_slot_refuses_without_the_attester_key():
    ok, reason = eligible(make_offer(proof=make_proof()), attester_key=None)
    assert not ok and "no attester key" in reason


def test_an_attested_slot_refuses_when_the_peer_holds_the_attester_key():
    """The configuration mistake that would make the whole gate vacuous."""
    ok, reason = eligible(make_offer(proof=make_proof()), attester_key=PEER_KEY)
    assert not ok and "peer's own offer key" in reason


def test_re_signing_the_offer_around_a_forged_proof_does_not_help():
    """The peer CAN re-sign its offer around any proof it likes (it holds the
    offer key), which is exactly why the offer's signature is not enough: the
    proof has to verify under a key the peer does not hold."""
    forged = sign_evidence(make_evidence(bundle=OTHER_BUNDLE), PEER_KEY)
    record = make_offer(proof=forged)
    ok, _ = verify_offer(record, PEER_KEY)
    assert ok, "the offer signature is valid; the proof is what fails"
    refused, reason = eligible(record)
    assert not refused and "attestation:" in reason


def test_the_offer_signature_covers_the_proof():
    """A peer cannot drop a proof it has already presented and have the record
    still verify: ``tee_proof`` rides inside the signed body."""
    record = make_offer(proof=make_proof())
    assert verify_offer(record, PEER_KEY)[0]
    dropped = {k: v for k, v in record.items() if k != "tee_proof"}
    assert not verify_offer(dropped, PEER_KEY)[0]
    added = dict(sign_offer(PeerOffer(peer_id=PEER_ID,
                                      attestation=Attestation("attested")),
                            PEER_KEY), tee_proof=make_proof())
    assert not verify_offer(added, PEER_KEY)[0]


def test_a_slot_that_demands_nothing_is_unaffected():
    """The fast path: an ordinary slot ignores an offered proof, still admits an
    offer that carries none, and consumes nothing."""
    ordinary = PlacementSlot(trust_floor="verified")
    ledger: set = set()
    ok, reason = eligible(make_offer(trust="verified", proof=make_proof()),
                          placement=ordinary, tee_ledger=ledger,
                          attester_key=None)
    assert ok, reason
    assert ledger == set()
    ok, reason = eligible(make_offer(trust="verified"), placement=ordinary,
                          tee_ledger=None, attester_key=None)
    assert ok, reason


def test_an_offer_cannot_carry_a_non_object_proof():
    with pytest.raises(OfferError, match="tee_proof"):
        PeerOffer(peer_id=PEER_ID, attestation=Attestation("attested"),
                  tee_proof="i-am-a-proof")


# -------------------------------- the retry path refuses rather than degrades


def test_a_replay_of_an_attested_placement_refuses_rather_than_degrading():
    """``lawful_retry`` replays through the same gate but configures no attester,
    so a retry of an attested base slot finds NO eligible peer instead of
    quietly falling back to an unattested one. A valid proof does not change
    that: without an attestation root to check it against, the dispatcher has
    nothing to check it with, and refused is the only safe answer."""
    from revl import lawful_retry
    from revl.peer_authority import Grant

    attempt = lawful_retry.Attempt(
        lawful_retry.EffectClass.PURE,
        Grant("lost-peer", ('fs.read(path="/data/jobs")',), {"retries": 3}),
        base_slot=slot())
    decision = lawful_retry.dispatch_on_loss(
        [Grant("root", ('fs.read(path="/data")',), {"retries": 9})],
        attempt, candidates=[make_offer(proof=make_proof())], key=PEER_KEY)
    assert decision.disposition is lawful_retry.Disposition.REFUSE
    assert decision.peer_id is None
    assert "attestation:" in decision.reason

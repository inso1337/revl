"""The result half of an attested placement: one admission, one delivery
(roadmap item 475, issue #827).

#859 landed the typed requirement and `tee_admits`, #964 landed the
placement-file spelling, and the attestation root landed real quote formats. All
three decide the OFFER side: which peer may run the bundle. The item's own
sentence has a second half — "the composition receives only signed results and
receipts" — and until this file there was no caller of `receipt_admits` anywhere
in `src/`, so a composition that admitted a peer correctly could then accept
whatever came back.

Three fail-OPEN shapes follow from a delivery check that is not attached to an
admission, and all three are closed here by making the admission produce the only
object a result can arrive through:

* **no gate at all.** Nothing forced the result half to be checked.
* **re-supplied correlation.** `receipt_admits` takes `peer_id`, `bundle` and
  `nonce` from its caller, so the delivery was only as good as the caller's
  memory of what was admitted. `AttestedRun` takes them from the admission.
* **a per-call ledger.** `receipt_admits` refuses when no delivery ledger is
  given, but a caller handing it a FRESH ledger each time satisfies that check
  while permitting unlimited replay. `test_a_fresh_ledger_per_call_is_the_replay_
  hole_the_run_closes` shows both sides of exactly that.

Every refusal here is a refusal: an ambiguous receipt never delivers. The accept
paths at the top and `test_the_control_...` at the bottom are the non-vacuity
controls — without them a file of refusals would pass just as well if the run
refused everything.
"""

from __future__ import annotations

import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import attest  # noqa: E402
from revl import placement as plc  # noqa: E402
from revl import tee_attestation as tee  # noqa: E402
from revl import tee_quote as q  # noqa: E402
from revl.peer_offer import (  # noqa: E402
    Attestation,
    PeerOffer,
    PlacementSlot,
    offer_admission,
    offer_eligible,
    sign_offer,
)
from revl.tee_attestation import (  # noqa: E402
    RECEIPT_KEY_UNBOUND_NOTE,
    AttestedRun,
    EnclaveEvidence,
    ResultReceipt,
    TeeError,
    TeeRequirement,
    open_attested_run,
    receipt_admits,
    receipt_key_binding,
    sign_evidence,
    sign_result_receipt,
)

# Three keys, and the whole story is in the difference: the peer signs its own
# offer with PEER_KEY, the attestation authority signs what the enclave is with
# ATTESTER_KEY, and the ENCLAVE signs the result it produced with ENCLAVE_KEY.
# The peer does not hold the third one; that is what a receipt proves.
PEER_KEY = b"pool-shared-secret"
ATTESTER_KEY = b"attestation-authority-key"
ENCLAVE_KEY = b"enclave-result-signing-key"
OTHER_ENCLAVE_KEY = b"some-other-enclave-the-operator-also-runs"

PEER_ID = "peer-1"
OTHER_PEER_ID = "peer-2"
MEASUREMENT = "ab" * 32
OTHER_MEASUREMENT = "cd" * 32
REGION = "eu-central"
NONCE = "challenge-827-delivery-0001"
OTHER_NONCE = "challenge-827-delivery-0002"

BUNDLE = attest.canonical_hash({"bundle": "payments", "revision": 3})
OTHER_BUNDLE = attest.canonical_hash({"bundle": "other", "revision": 1})
RESULT = {"settled": True, "count": 3}
OTHER_RESULT = {"settled": True, "count": 4}

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
ENCLAVE_KEY_ID = receipt_key_binding(ENCLAVE_KEY)


# ------------------------------------------------------------------ builders


def requirement(**overrides) -> TeeRequirement:
    fields = {"bundle": BUNDLE, "measurements": frozenset({MEASUREMENT}),
              "regions": frozenset({REGION}), "nonce": NONCE, "max_age_s": 120.0}
    fields.update(overrides)
    return TeeRequirement(**fields)


def evidence(**overrides) -> EnclaveEvidence:
    fields = {"peer_id": PEER_ID, "bundle": BUNDLE, "measurement": MEASUREMENT,
              "region": REGION, "nonce": NONCE,
              "issued_at": tee.stamp(NOW - timedelta(seconds=5)),
              "expires_at": tee.stamp(NOW + timedelta(seconds=60)),
              "receipt_key_id": ENCLAVE_KEY_ID}
    fields.update(overrides)
    return EnclaveEvidence(**fields)


def proof(**overrides) -> dict:
    return sign_evidence(evidence(**overrides), ATTESTER_KEY)


def opens(record=None, req=None, **overrides):
    """`open_attested_run` with every gate configured for admission, so a test
    changes only the fact it is about."""
    fields = {"peer_id": PEER_ID, "peer_key": PEER_KEY,
              "enclave_key": ENCLAVE_KEY, "attester_key": ATTESTER_KEY,
              "now": NOW, "replay_ledger": set()}
    fields.update(overrides)
    return open_attested_run(proof() if record is None else record,
                             req or requirement(), **fields)


def admitted_run(**overrides) -> AttestedRun:
    run, reason = opens(**overrides)
    assert run is not None, reason
    return run


def receipt(**overrides) -> dict:
    fields = {"peer_id": PEER_ID, "bundle": BUNDLE, "nonce": NONCE,
              "result_hash": tee.result_identity(RESULT),
              "issued_at": tee.stamp(NOW - timedelta(seconds=5))}
    key = overrides.pop("key", ENCLAVE_KEY)
    fields.update(overrides)
    return sign_result_receipt(ResultReceipt(**fields), key)


# --------------------------------------------------------- the accept path


def test_an_admitted_run_accepts_the_result_it_authorized():
    """The non-vacuity control for everything below: the whole path works."""
    run = admitted_run()
    assert run.peer_id == PEER_ID and run.bundle == BUNDLE and run.nonce == NONCE
    assert run.receipt_key_bound and not run.delivered
    ok, reason = run.accept_result(receipt(), RESULT, now=NOW)
    assert ok, reason
    assert reason.startswith("admitted: the result is signed by the attested")
    assert run.delivered


def test_the_admission_reason_is_the_verifiers_own():
    """`open_attested_run` must not grow a second opinion about whether a peer is
    admitted: its reason is `tee_admits`' reason, byte for byte."""
    record = proof()
    run, mine = opens(record)
    theirs_ok, theirs = tee.tee_admits(
        record, requirement(), peer_id=PEER_ID, peer_key=PEER_KEY,
        attester_key=ATTESTER_KEY, now=NOW, replay_ledger=set())
    assert run is not None and theirs_ok
    assert mine == theirs


def test_a_development_rooted_delivery_says_so_in_its_own_reason():
    """Same discipline as `DEV_ROOT_NOTE` on the admission: a verdict reached
    under the symmetric-MAC verifier must not read like a hardware-rooted one."""
    ok, reason = admitted_run().accept_result(receipt(), RESULT, now=NOW)
    assert ok
    assert q.DEV_ROOT_NOTE in reason


# ------------------------------------- the run cannot be opened: the receipt key


def test_a_run_whose_results_could_never_be_checked_is_refused_before_it_starts():
    """No receiving key means no result can ever be checked against the enclave.
    Refusing at admission is the same verdict taken early enough to matter: the
    composition can still choose another peer instead of holding an unusable
    result from a worker that already ran."""
    run, reason = opens(enclave_key=b"")
    assert run is None
    assert "no receiving key provided" in reason
    assert "refused before the worker is asked to run it" in reason


def test_the_peers_own_offer_key_as_the_receiving_key_is_refused_at_admission():
    ledger: set = set()
    run, reason = opens(enclave_key=PEER_KEY, replay_ledger=ledger)
    assert run is None
    assert "the peer's own offer key" in reason
    assert ledger == set(), (
        "a run refused on the composition's own misconfiguration must not burn "
        "the peer's challenge: the peer did nothing wrong")


def test_a_receipt_key_the_attestation_did_not_name_is_refused():
    """The binding. `receipt_key_id` is covered by the attester's signature, so
    it is an attested fact: the enclave states which key it signs results with. A
    composition holding a different key is refused, because a receipt that
    verifies under some other key the operator provisioned says nothing about the
    enclave that was admitted here."""
    run, reason = opens(enclave_key=OTHER_ENCLAVE_KEY)
    assert run is None
    assert ENCLAVE_KEY_ID in reason
    assert receipt_key_binding(OTHER_ENCLAVE_KEY) in reason
    assert "not a result from the attested run" in reason


def test_an_attestation_that_names_no_receipt_key_is_refused_by_default():
    """The ambiguous case refuses. An unnamed receipt key makes the key operator
    configuration rather than an attested fact, and this surface will not call
    that an attested delivery unless the caller says so by name."""
    run, reason = opens(proof(receipt_key_id=None))
    assert run is None
    assert "does not name the key the enclave signs its results with" in reason


def test_an_unbound_receipt_key_is_allowed_only_by_name_and_labelled():
    """The pre-binding evidence shape still works, and every verdict it reaches
    says what it was reached on."""
    run, reason = opens(proof(receipt_key_id=None),
                        require_bound_receipt_key=False)
    assert run is not None, reason
    assert not run.receipt_key_bound
    ok, delivery = run.accept_result(receipt(), RESULT, now=NOW)
    assert ok, delivery
    assert RECEIPT_KEY_UNBOUND_NOTE in delivery


def test_editing_the_receipt_key_into_a_signed_record_breaks_the_signature():
    """The peer carries the evidence, so it can rewrite it; what it cannot do is
    re-sign it. Pointing the composition at a key the peer holds is a rewrite."""
    record = proof(receipt_key_id=None)
    record["receipt_key_id"] = receipt_key_binding(PEER_KEY)
    run, reason = opens(record)
    assert run is None
    assert "attestation signature mismatch" in reason


def test_a_receipt_key_that_is_present_but_unreadable_is_refused():
    """Present-but-unparseable is refused rather than read as absent: a member
    that cannot be compared would otherwise silently downgrade a bound receipt
    key to an unbound one."""
    body = evidence(receipt_key_id=None).body()
    body["receipt_key_id"] = "not-a-fingerprint"
    body["key_id"] = attest.key_id(ATTESTER_KEY)
    body["signature"] = tee._sign(body, ATTESTER_KEY, tee.EVIDENCE_SIGN_DOMAIN)
    run, reason = opens(body)
    assert run is None
    assert "receipt_key_id is present but is not a key fingerprint" in reason


def test_a_malformed_receipt_key_is_refused_when_the_evidence_is_built():
    with pytest.raises(TeeError) as caught:
        evidence(receipt_key_id="zz" * 8)
    assert "must be a key fingerprint" in str(caught.value)


def test_an_evidence_that_names_no_receipt_key_carries_no_such_member():
    """The member is additive: an evidence that names no receipt key has exactly
    the bytes it had before the member existed, so no committed quote changes
    meaning."""
    assert "receipt_key_id" not in evidence(receipt_key_id=None).body()
    assert evidence().body()["receipt_key_id"] == ENCLAVE_KEY_ID


def test_the_binding_fingerprint_discloses_no_key_material():
    assert ENCLAVE_KEY_ID == attest.key_id(ENCLAVE_KEY)
    assert ENCLAVE_KEY.hex()[:16] not in ENCLAVE_KEY_ID
    with pytest.raises(TeeError):
        receipt_key_binding(b"")


# ------------------------------- the run cannot be opened: the admission refuses


@pytest.mark.parametrize("overrides,fragment", [
    ({"bundle": OTHER_BUNDLE}, "not the approved bundle"),
    ({"measurement": OTHER_MEASUREMENT}, "is not in the requirement's permitted"),
    ({"region": "us-east"}, "which is not in the requirement's permitted"),
    ({"nonce": OTHER_NONCE}, "not this placement's challenge"),
    ({"peer_id": OTHER_PEER_ID}, "a proof is bound to the peer it was issued for"),
])
def test_an_unattested_run_yields_no_delivery_path(overrides, fragment):
    """Every admission refusal must leave the caller with NO run: there is no
    object to deliver a result through, so the result half cannot be reached by a
    peer the offer half refused."""
    run, reason = opens(proof(**overrides))
    assert run is None
    assert fragment in reason


def test_a_proof_the_peer_signed_itself_yields_no_delivery_path():
    run, reason = opens(sign_evidence(evidence(), PEER_KEY),
                        attester_key=PEER_KEY)
    assert run is None
    assert "the peer's own offer key" in reason


def test_a_development_rooted_run_is_refused_when_a_hardware_root_is_required():
    run, reason = opens(require_hardware_root=True)
    assert run is None
    assert "no production attestation root" in reason


def test_no_replay_ledger_yields_no_delivery_path():
    run, reason = opens(replay_ledger=None)
    assert run is None
    assert "no replay ledger was supplied" in reason


# ------------------------------------------------------- the delivery refuses


def test_a_result_with_no_receipt_is_refused():
    ok, reason = admitted_run().accept_result({}, RESULT, now=NOW)
    assert not ok
    assert "carries no signature" in reason


def test_a_result_that_is_not_even_a_record_is_refused():
    ok, reason = admitted_run().accept_result("a receipt, honest", RESULT, now=NOW)
    assert not ok
    assert "not an object" in reason


def test_a_receipt_the_peer_signed_itself_is_refused():
    """The headline on the delivery side. The peer holds its own offer key and
    not the enclave's, so a result it fabricated outside the enclave carries a
    receipt that verifies under the wrong key."""
    ok, reason = admitted_run().accept_result(receipt(key=PEER_KEY), RESULT,
                                              now=NOW)
    assert not ok
    assert "signature mismatch" in reason


def test_a_receipt_signed_by_another_enclave_is_refused():
    ok, reason = admitted_run().accept_result(receipt(key=OTHER_ENCLAVE_KEY),
                                              RESULT, now=NOW)
    assert not ok
    assert "signature mismatch" in reason


def test_a_receipt_that_covers_some_other_result_is_refused():
    """The receipt names a content hash and the run recomputes it over the bytes
    that actually arrived, so a valid receipt cannot be laundered onto a
    substituted result."""
    ok, reason = admitted_run().accept_result(receipt(), OTHER_RESULT, now=NOW)
    assert not ok
    assert "not the result that arrived" in reason


def test_a_receipt_from_another_run_is_refused():
    ok, reason = admitted_run().accept_result(receipt(nonce=OTHER_NONCE), RESULT,
                                              now=NOW)
    assert not ok
    assert "not bound to the run that was admitted" in reason


def test_a_receipt_naming_another_peer_is_refused():
    ok, reason = admitted_run().accept_result(receipt(peer_id=OTHER_PEER_ID),
                                              RESULT, now=NOW)
    assert not ok
    assert "not 'peer-1'" in reason


def test_a_receipt_for_another_bundle_is_refused():
    ok, reason = admitted_run().accept_result(receipt(bundle=OTHER_BUNDLE),
                                              RESULT, now=NOW)
    assert not ok
    assert "not the approved bundle" in reason


def test_a_stale_receipt_is_refused():
    old = receipt(issued_at=tee.stamp(NOW - timedelta(hours=3)))
    ok, reason = admitted_run().accept_result(old, RESULT, now=NOW)
    assert not ok
    assert "older than the accepted max_age_s" in reason


def test_a_future_dated_receipt_is_refused():
    ahead = receipt(issued_at=tee.stamp(NOW + timedelta(minutes=10)))
    ok, reason = admitted_run().accept_result(ahead, RESULT, now=NOW)
    assert not ok
    assert "ahead of now" in reason


def test_a_run_delivers_once():
    run = admitted_run()
    ok, _ = run.accept_result(receipt(), RESULT, now=NOW)
    assert ok
    ok, reason = run.accept_result(receipt(), RESULT, now=NOW)
    assert not ok
    assert reason.startswith("replay:")


def test_a_refused_delivery_does_not_burn_the_run():
    """The other direction of the same ledger: a refusal must not consume the
    run's one delivery, or a hostile third party could deny an honest worker its
    result by presenting junk first."""
    run = admitted_run()
    ok, _ = run.accept_result(receipt(key=PEER_KEY), RESULT, now=NOW)
    assert not ok and not run.delivered
    ok, reason = run.accept_result(receipt(), RESULT, now=NOW)
    assert ok, reason


def test_a_fresh_ledger_per_call_is_the_replay_hole_the_run_closes():
    """The reason the run owns its ledger. `receipt_admits` refuses when no
    delivery ledger is supplied, but a caller that hands it a NEW ledger each
    call satisfies that check and replays without limit. Both halves are asserted
    here, so the hole and its closure are visible in one place."""
    good = receipt()
    for _ in range(3):
        ok, _ = receipt_admits(good, enclave_key=ENCLAVE_KEY, peer_key=PEER_KEY,
                               peer_id=PEER_ID, bundle=BUNDLE, nonce=NONCE,
                               result=RESULT, now=NOW, replay_ledger=set())
        assert ok, "the primitive cannot see a ledger the caller keeps replacing"
    run = admitted_run()
    assert run.accept_result(good, RESULT, now=NOW)[0]
    for _ in range(3):
        ok, reason = run.accept_result(good, RESULT, now=NOW)
        assert not ok and reason.startswith("replay:")


def test_a_run_cannot_be_told_which_run_it_is():
    """The correlation is structural, not a convention a caller can get wrong:
    `accept_result` has no parameter for the peer, the bundle or the challenge,
    so a delivery is always checked against the facts the ADMISSION decided."""
    taken = set(inspect.signature(AttestedRun.accept_result).parameters)
    assert not taken & {"peer_id", "bundle", "nonce", "enclave_key", "peer_key",
                        "replay_ledger"}


def test_one_runs_receipt_does_not_deliver_through_another():
    """Two admitted runs, each internally valid, and neither accepts the other's
    result. Without the run object this is exactly the mistake a composition
    makes by passing the wrong correlation values."""
    first = admitted_run()
    second = admitted_run(record=proof(nonce=OTHER_NONCE),
                          req=requirement(nonce=OTHER_NONCE))
    for_second = receipt(nonce=OTHER_NONCE)
    ok, reason = first.accept_result(for_second, RESULT, now=NOW)
    assert not ok and "not bound to the run that was admitted" in reason
    ok, reason = second.accept_result(for_second, RESULT, now=NOW)
    assert ok, reason


def test_a_run_keeps_its_keys_out_of_repr():
    run = admitted_run()
    printed = repr(run)
    assert "pool-shared-secret" not in printed
    assert "enclave-result-signing-key" not in printed
    assert PEER_ID in printed and NONCE in printed


# ------------------------------------------------ through the pool: the offer


def offer(record=None, trust="attested", peer_id=PEER_ID) -> dict:
    return sign_offer(
        PeerOffer(peer_id=peer_id, attestation=Attestation(trust=trust),
                  tee_proof=proof() if record is None else record),
        PEER_KEY)


def test_the_pool_hands_back_the_run_a_result_must_arrive_through():
    slot = PlacementSlot(attested_tee=requirement())
    run, reason = offer_admission(offer(), slot, PEER_KEY,
                                  enclave_key=ENCLAVE_KEY,
                                  attester_key=ATTESTER_KEY, tee_ledger=set(),
                                  now=NOW)
    assert run is not None, reason
    assert run.accept_result(receipt(), RESULT, now=NOW)[0]


def test_a_slot_that_demands_no_attested_tee_gets_no_run():
    """A delivery gate is meaningful only because an attestation decided which
    peer, which bundle and which challenge the result is bound to. A slot that
    demanded none of that has nothing to bind to, so it is refused rather than
    handed a run that would read as a guarantee this surface cannot make."""
    run, reason = offer_admission(offer(), PlacementSlot(), PEER_KEY,
                                  enclave_key=ENCLAVE_KEY,
                                  attester_key=ATTESTER_KEY, tee_ledger=set(),
                                  now=NOW)
    assert run is None
    assert "there is no attested run for a result to arrive through" in reason


def test_an_offer_that_fails_a_later_gate_yields_no_run():
    """The attestation is admitted and the trust floor is not met. The run must
    not survive a partial pass, or a composition would hold a delivery path for a
    peer it was not eligible to use."""
    slot = PlacementSlot(attested_tee=requirement(), trust_floor="local")
    run, reason = offer_admission(offer(), slot, PEER_KEY,
                                  enclave_key=ENCLAVE_KEY,
                                  attester_key=ATTESTER_KEY, tee_ledger=set(),
                                  now=NOW)
    assert run is None
    assert "is below the slot floor" in reason


def test_the_pool_verdict_is_unchanged_by_the_delivery_half():
    """`offer_eligible` and `offer_admission` must say the same thing about the
    same offer, byte for byte, on the gates they share."""
    record = offer(record=proof(measurement=OTHER_MEASUREMENT))
    slot = PlacementSlot(attested_tee=requirement())
    _, theirs = offer_eligible(record, slot, PEER_KEY,
                               attester_key=ATTESTER_KEY, tee_ledger=set(),
                               now=NOW)
    _, mine = offer_admission(record, slot, PEER_KEY, enclave_key=ENCLAVE_KEY,
                              attester_key=ATTESTER_KEY, tee_ledger=set(),
                              now=NOW)
    assert mine == theirs


def test_an_offer_with_no_evidence_yields_no_run():
    slot = PlacementSlot(attested_tee=requirement())
    bare = sign_offer(PeerOffer(peer_id=PEER_ID,
                                attestation=Attestation(trust="attested")),
                      PEER_KEY)
    run, reason = offer_admission(bare, slot, PEER_KEY, enclave_key=ENCLAVE_KEY,
                                  attester_key=ATTESTER_KEY, tee_ledger=set(),
                                  now=NOW)
    assert run is None
    assert "carries no enclave evidence" in reason


# --------------------------------------- through the placement file: the demand


def attest_table(**overrides) -> dict:
    table = {"requires": plc.TEE_WORD_ATTESTED, "bundle": BUNDLE,
             "measurements": [MEASUREMENT], "region": REGION, "nonce": NONCE}
    table.update(overrides)
    return table


def placement_file(table=None) -> dict:
    entry = {"components": ["Worker"], "backend": "py"}
    if table is not None:
        entry["attest"] = table
    return {"processes": {"worker": entry}}


def test_the_placement_file_spelling_reaches_the_delivery_gate():
    run, reason = plc.admit_run_for_process(
        placement_file(attest_table()), "worker", offer(),
        offer_key=PEER_KEY, enclave_key=ENCLAVE_KEY, attester_key=ATTESTER_KEY,
        tee_ledger=set(), now=NOW)
    assert run is not None, reason
    assert run.bundle == BUNDLE and run.nonce == NONCE
    ok, delivery = run.accept_result(receipt(), RESULT, now=NOW)
    assert ok, delivery


def test_a_process_that_demands_no_attested_tee_gets_no_run():
    run, reason = plc.admit_run_for_process(
        placement_file(), "worker", offer(), offer_key=PEER_KEY,
        enclave_key=ENCLAVE_KEY, attester_key=ATTESTER_KEY, tee_ledger=set(),
        now=NOW)
    assert run is None
    assert "declares no `[attest]` table" in reason


def test_a_malformed_attest_table_gets_no_run():
    run, reason = plc.admit_run_for_process(
        placement_file(attest_table(requires="whatever_you_say")), "worker",
        offer(), offer_key=PEER_KEY, enclave_key=ENCLAVE_KEY,
        attester_key=ATTESTER_KEY, tee_ledger=set(), now=NOW)
    assert run is None
    assert reason.startswith("placement:")


def test_the_placement_file_verdict_is_still_the_verifiers_own():
    """The single-verdict property #964 established, now on the delivery entry
    point: `admit_run_for_process` and `admit_peer_for_process` must refuse the
    same offer with the same words."""
    record = offer(record=proof(measurement=OTHER_MEASUREMENT))
    _, theirs = plc.admit_peer_for_process(
        placement_file(attest_table()), "worker", record, offer_key=PEER_KEY,
        attester_key=ATTESTER_KEY, tee_ledger=set(), now=NOW)
    _, mine = plc.admit_run_for_process(
        placement_file(attest_table()), "worker", record, offer_key=PEER_KEY,
        enclave_key=ENCLAVE_KEY, attester_key=ATTESTER_KEY, tee_ledger=set(),
        now=NOW)
    assert mine == theirs


# ----------------------------------------------- the hardware-rooted delivery

TDX_AK = q.private_key_from_seed(q.CURVE_P256, b"revl-test/delivery-tdx-ak")
TDX_PCK = q.private_key_from_seed(q.CURVE_P256, b"revl-test/delivery-tdx-pck")
TDX_PCK_PUB = q.derive_public_key(q.CURVE_P256, TDX_PCK)
TDX_PCK_ID = q.public_key_id(TDX_PCK_PUB)
MRTD = "a1" * 48


def hardware_root() -> q.HardwareRoot:
    return q.HardwareRoot(
        platform_keys=[q.PinnedKey("P-256", TDX_PCK_PUB, "tdx-pck")])


def tdx_proof(**overrides) -> dict:
    fields = {"measurement": MRTD}
    fields.update(overrides)
    return tee.build_tdx_evidence(evidence(**fields), attest_private_key=TDX_AK,
                                  platform_private_key=TDX_PCK,
                                  platform_key_id=TDX_PCK_ID)


def hardware_requirement(**overrides) -> TeeRequirement:
    return requirement(measurements=frozenset({MRTD}), **overrides)


def test_a_hardware_rooted_run_delivers_and_is_not_labelled_development():
    """The strongest end-to-end shape this build has: a real TDX quote chained to
    a key the operator pinned admits the run, and the enclave's receipt delivers
    the result. Neither the admission nor the delivery carries a weakening note."""
    run, reason = opens(tdx_proof(), hardware_requirement(),
                        attester_key=None, root=hardware_root())
    assert run is not None, reason
    assert run.hardware_rooted and run.receipt_key_bound
    ok, delivery = run.accept_result(receipt(), RESULT, now=NOW)
    assert ok, delivery
    assert q.DEV_ROOT_NOTE not in delivery
    assert RECEIPT_KEY_UNBOUND_NOTE not in delivery


def test_the_receipt_key_a_quote_binds_cannot_be_rewritten():
    """`receipt_key_id` rides in the body the quote's `report_data` digests, so
    repointing the composition at another key breaks the hardware signature. This
    is what makes the binding an attested fact on the hardware path rather than a
    member the peer fills in."""
    record = tdx_proof()
    record["receipt_key_id"] = receipt_key_binding(OTHER_ENCLAVE_KEY)
    run, reason = opens(record, hardware_requirement(), attester_key=None,
                        root=hardware_root(), enclave_key=OTHER_ENCLAVE_KEY)
    assert run is None
    assert "the attestation root refused the quote" in reason


def test_the_control_a_hardware_rooted_run_with_nothing_tampered_delivers():
    """Non-vacuity for the hardware section: the identical construction, with no
    member rewritten, admits and delivers."""
    run, reason = opens(tdx_proof(), hardware_requirement(), attester_key=None,
                        root=hardware_root())
    assert run is not None, reason
    assert run.accept_result(receipt(), RESULT, now=NOW)[0]

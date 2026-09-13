"""An attested-TEE placement decided by a real quote and a pinned root
(roadmap item 475, issue #827).

#859 landed the typed requirement and a fail-closed verifier, #964 landed the
placement-file spelling, and both deferred their verdict to `tee_admits`. What
`tee_admits` had to decide with was a symmetric MAC, which proves that the holder
of a key authored the record — so the demand was only as strong as the assumption
that the peer did not hold that key, and the module said so.

This file pins the hardware-rooted verdict. A real Intel TDX Quote v4 and a real
AMD SEV-SNP attestation report, at the vendors' own offsets, verified with ECDSA
up a chain that terminates in a key the OPERATOR pinned. The peer holds no part
of that chain.

The refusals are the property, and they are grouped by what an attacker would
try: sign the quote yourself (a forged root), run a different enclave than you
claim (a wrong measurement, in both directions), reuse a proof (a replay, in both
directions), and hand the verifier bytes it might mis-read (malformed structure).
Each one is a refusal by name. Nothing in here admits on a claim.

Every tampered case RE-SIGNS what it can: the offer signature is recomputed
around a forged proof, and the evidence body is re-signed after a member is
changed. A test that only corrupted a signature would show that a broken forgery
is caught, not that a well-made one is.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from revl import placement as plc  # noqa: E402
from revl import tee_attestation as tee  # noqa: E402
from revl import tee_quote as q  # noqa: E402
from revl.attest import canonical_hash  # noqa: E402
from revl.peer_offer import (  # noqa: E402
    Attestation,
    PeerOffer,
    PlacementSlot,
    offer_eligible,
    sign_offer,
)
from revl.tee_attestation import (  # noqa: E402
    EnclaveEvidence,
    TeeError,
    TeeRequirement,
    build_sev_snp_evidence,
    build_tdx_evidence,
    tee_admits,
    verify_evidence,
)

FIXTURES = ROOT / "tests" / "fixtures" / "tee"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())

PEER_KEY = b"pool-shared-secret"
PEER_ID = "peer-1"
OTHER_PEER_ID = "peer-2"
REGION = "eu-central"
OTHER_REGION = "us-east"
NONCE = "challenge-827-hardware-0001"
OTHER_NONCE = "challenge-827-hardware-0002"
BUNDLE = canonical_hash({"bundle": "payments", "revision": 3})
OTHER_BUNDLE = canonical_hash({"bundle": "other", "revision": 1})
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

# A TDX MRTD and a SEV-SNP launch measurement are both 48 bytes, so the
# requirement's permitted set is spelled in 96 hex digits here.
MRTD = "a1" * 48
OTHER_MRTD = "b2" * 48
SEV_MEASUREMENT = "c3" * 48

TDX_AK = q.private_key_from_seed(q.CURVE_P256, b"revl-test/tdx-attestation-key")
TDX_PCK = q.private_key_from_seed(q.CURVE_P256, b"revl-test/tdx-pck")
TDX_OTHER_PCK = q.private_key_from_seed(q.CURVE_P256, b"revl-test/tdx-pck-elsewhere")
SEV_VCEK = q.private_key_from_seed(q.CURVE_P384, b"revl-test/sev-vcek")

TDX_PCK_PUB = q.derive_public_key(q.CURVE_P256, TDX_PCK)
TDX_OTHER_PCK_PUB = q.derive_public_key(q.CURVE_P256, TDX_OTHER_PCK)
SEV_VCEK_PUB = q.derive_public_key(q.CURVE_P384, SEV_VCEK)

TDX_PCK_ID = q.public_key_id(TDX_PCK_PUB)
SEV_VCEK_ID = q.public_key_id(SEV_VCEK_PUB)


def hardware_root(**overrides) -> q.HardwareRoot:
    """The operator's root: the TDX platform key and the SEV-SNP VCEK, pinned.
    Nothing the peer supplies can add to it."""
    fields = dict(platform_keys=[q.PinnedKey("P-256", TDX_PCK_PUB, "tdx-pck"),
                                q.PinnedKey("P-384", SEV_VCEK_PUB, "sev-vcek")])
    fields.update(overrides)
    return q.HardwareRoot(**fields)


def requirement(**overrides) -> TeeRequirement:
    fields = {"bundle": BUNDLE, "measurements": frozenset({MRTD}),
              "regions": frozenset({REGION}), "nonce": NONCE, "max_age_s": 120.0}
    fields.update(overrides)
    return TeeRequirement(**fields)


def evidence(**overrides) -> EnclaveEvidence:
    fields = {"peer_id": PEER_ID, "bundle": BUNDLE, "measurement": MRTD,
              "region": REGION, "nonce": NONCE,
              "issued_at": tee.stamp(NOW - timedelta(seconds=5)),
              "expires_at": tee.stamp(NOW + timedelta(seconds=60))}
    fields.update(overrides)
    return EnclaveEvidence(**fields)


def tdx_proof(*, platform_private_key=None, platform_key_id=None,
              **overrides) -> dict:
    """A hardware-rooted evidence record: the evidence, quoted by a TD whose
    quote chains to the pinned platform key."""
    quote_fields = {k[6:]: overrides.pop(k) for k in list(overrides)
                    if k.startswith("quote_")}
    return build_tdx_evidence(
        evidence(**overrides),
        attest_private_key=TDX_AK,
        platform_private_key=TDX_PCK if platform_private_key is None else platform_private_key,
        platform_key_id=TDX_PCK_ID if platform_key_id is None else platform_key_id,
        **quote_fields)


def sev_proof(**overrides) -> dict:
    return build_sev_snp_evidence(
        evidence(measurement=SEV_MEASUREMENT, **overrides),
        vcek_private_key=SEV_VCEK, platform_key_id=SEV_VCEK_ID)


def admits(record, req=None, **overrides):
    """`tee_admits` with every gate configured for acceptance, so a test changes
    only the fact it is about."""
    fields = {"peer_id": PEER_ID, "peer_key": PEER_KEY, "root": hardware_root(),
              "now": NOW, "replay_ledger": set()}
    fields.update(overrides)
    return tee_admits(record, req or requirement(), **fields)


# ------------------------------------------------------------- the accept path


def test_a_tdx_quote_chained_to_a_pinned_root_is_admitted():
    ok, reason = admits(tdx_proof())
    assert ok, reason
    assert reason.startswith("admitted")
    assert q.DEV_ROOT_NOTE not in reason, (
        "a hardware-rooted admission must NOT carry the development label, or the "
        "label stops meaning anything")


def test_a_sev_snp_report_chained_to_a_pinned_root_is_admitted():
    ok, reason = admits(
        sev_proof(), requirement(measurements=frozenset({SEV_MEASUREMENT})))
    assert ok, reason
    assert q.DEV_ROOT_NOTE not in reason


def test_the_hardware_verdict_names_the_quote_it_verified():
    ok, reason = verify_evidence(tdx_proof(), root=hardware_root(), now=NOW)
    assert ok
    assert "TDX quote" in reason and TDX_PCK_ID in reason
    assert "the claims that quote binds" in reason


def test_a_development_admission_is_labelled_and_a_hardware_one_is_not():
    """The labelling is on the ADMISSION, not only on the refusals: an operator
    reading a log line has to be able to see that nothing hardware rooted was
    checked."""
    dev = tee.sign_evidence(evidence(), b"an attestation authority key")
    ok, dev_reason = admits(dev, attester_key=b"an attestation authority key",
                            root=None)
    assert ok, dev_reason
    assert q.DEV_ROOT_NOTE in dev_reason
    ok, hw_reason = admits(tdx_proof())
    assert ok and q.DEV_ROOT_NOTE not in hw_reason


# ------------------------------------------- refusal 1: the root is not the peer's


def test_a_quote_signed_by_an_unpinned_platform_is_refused():
    """The headline. A complete, internally consistent TDX quote, re-signed by a
    platform key the operator never pinned and honestly naming that key, is
    refused: the peer can build a quote, and it cannot build one that reaches the
    root."""
    forged = tdx_proof(platform_private_key=TDX_OTHER_PCK,
                       platform_key_id=q.public_key_id(TDX_OTHER_PCK_PUB))
    ok, reason = admits(forged)
    assert not ok
    assert "no attestation root holds a platform key" in reason


def test_naming_the_pinned_fingerprint_over_an_unpinned_signature_is_refused():
    """The sharper variant: the record names the fingerprint the root DOES hold,
    while the quote was signed by another platform. The root looks the pinned key
    up and the signature fails under it."""
    forged = tdx_proof(platform_private_key=TDX_OTHER_PCK,
                       platform_key_id=TDX_PCK_ID)
    ok, reason = admits(forged)
    assert not ok
    assert "not signed by the platform key pinned as" in reason


def test_an_attestation_key_the_qe_report_does_not_certify_is_refused():
    """The middle hop. Without it the attestation key is a public key the peer
    chose, and the platform signature would certify nothing about it."""
    ok, reason = admits(tdx_proof(quote_qe_report_data=b"\x00" * 64))
    assert not ok
    assert "does not bind this attestation key" in reason


def test_an_evidence_that_names_the_peers_own_offer_key_as_the_platform_is_refused():
    proof = tdx_proof()
    proof["key_id"] = tee.key_id(PEER_KEY)
    ok, reason = admits(proof)
    assert not ok
    assert "terminates inside the peer" in reason


def test_a_root_that_pins_nothing_relevant_admits_nothing():
    ok, reason = admits(tdx_proof(), root=q.HardwareRoot(
        platform_keys=[q.PinnedKey("P-256", TDX_OTHER_PCK_PUB)]))
    assert not ok and "no attestation root holds a platform key" in reason


# --------------------------------------- refusal 2: the wrong measurement, twice


def test_a_permitted_measurement_the_hardware_does_not_report_is_refused():
    """The direction the cross-check exists for. The evidence CLAIMS a permitted
    measurement, the quote is properly signed, and the quote's own register says a
    different enclave ran. `report_data` binds what the workload said; the register
    is what the platform measured, and only the second is evidence."""
    claimed = evidence()
    # Built by hand, because the reference attester CANNOT produce this record:
    # `build_tdx_evidence` takes the MRTD from the evidence it is quoting, so a
    # body and a register that disagree have to be assembled deliberately.
    report_data = tee.attester_report_data(
        claimed, sign_alg=q.SIGN_ALG_TDX, platform_key_id=TDX_PCK_ID)
    quote = q.build_tdx_quote(report_data=report_data,
                              mrtd=bytes.fromhex(OTHER_MRTD),
                              attest_private_key=TDX_AK,
                              platform_private_key=TDX_PCK)
    lying = tee.quote_evidence(claimed, sign_alg=q.SIGN_ALG_TDX, quote=quote,
                               platform_key_id=TDX_PCK_ID)
    ok, reason = admits(lying)
    assert not ok
    assert "quote's own measurement register reads" in reason
    assert "is not the enclave the evidence describes" in reason


def test_a_measurement_outside_the_requirements_permitted_set_is_refused():
    """The ordinary direction: the evidence and the quote agree, and the enclave
    is not one the placement permits."""
    honest = build_tdx_evidence(evidence(measurement=OTHER_MRTD),
                               attest_private_key=TDX_AK,
                               platform_private_key=TDX_PCK,
                               platform_key_id=TDX_PCK_ID)
    ok, reason = admits(honest)
    assert not ok
    assert "is not in the requirement's permitted set" in reason


def test_a_measurement_that_is_not_a_hardware_register_width_is_refused():
    """A 32-byte digest is a legal `TeeRequirement` measurement (SGX MRENCLAVE is
    32 bytes) and cannot be a TD's MRTD, so the attester refuses to build a quote
    that would misdescribe its own register."""
    with pytest.raises(TeeError, match="48 bytes"):
        build_tdx_evidence(evidence(measurement="ab" * 32),
                           attest_private_key=TDX_AK,
                           platform_private_key=TDX_PCK,
                           platform_key_id=TDX_PCK_ID)


def test_a_debuggable_td_is_refused_however_well_it_is_signed():
    ok, reason = admits(tdx_proof(quote_td_attributes=b"\x01" + b"\x00" * 7))
    assert not ok
    assert "debug bit is set" in reason


# -------------------------------------------------- refusal 3: replay, two ways


def test_a_consumed_challenge_is_refused_the_second_time():
    ledger: set = set()
    proof = tdx_proof()
    ok, reason = admits(proof, replay_ledger=ledger)
    assert ok, reason
    again, reason = admits(proof, replay_ledger=ledger)
    assert not again and "replay" in reason


def test_a_refused_proof_does_not_burn_the_challenge():
    """The ledger is mutated on success only, so probing with a forgery cannot
    deny an honest peer its own challenge."""
    ledger: set = set()
    bad, _ = admits(tdx_proof(platform_private_key=TDX_OTHER_PCK),
                    replay_ledger=ledger)
    assert not bad and ledger == set()
    ok, reason = admits(tdx_proof(), replay_ledger=ledger)
    assert ok, reason


def test_a_quote_reused_under_a_new_challenge_is_refused():
    """The replay that the MAC path could only catch with a ledger. Take a valid
    quote, rewrite the evidence body around it to answer THIS placement's
    challenge, and the hardware signature no longer covers the body: the quote's
    `report_data` is the digest of the body it was issued for."""
    captured = tdx_proof(nonce=OTHER_NONCE)
    replayed = dict(captured)
    replayed["nonce"] = NONCE
    ok, reason = admits(replayed)
    assert not ok
    assert "report_data does not match" in reason


@pytest.mark.parametrize("member,value", [
    ("peer_id", OTHER_PEER_ID),
    ("bundle", OTHER_BUNDLE),
    ("region", OTHER_REGION),
    ("outbound_network", "allowed"),
    ("expires_at", "2099-01-01T00:00:00+00:00"),
    ("issued_at", "2026-05-01T00:00:00+00:00"),
])
def test_rewriting_any_claim_around_a_valid_quote_is_refused(member, value):
    """`report_data` is the digest of the whole body, so the hardware signature
    covers every claim the hardware itself does not measure. There is no member a
    peer can edit and keep the quote."""
    proof = tdx_proof()
    proof[member] = value
    ok, reason = admits(proof)
    assert not ok
    assert "report_data does not match" in reason, f"{member}: {reason}"


def test_a_quote_issued_for_another_peer_is_refused():
    ok, reason = admits(tdx_proof(peer_id=OTHER_PEER_ID))
    assert not ok
    assert "a proof is bound to the peer it was issued for" in reason


def test_an_expired_quote_is_refused():
    ok, reason = admits(tdx_proof(
        issued_at=tee.stamp(NOW - timedelta(seconds=300)),
        expires_at=tee.stamp(NOW - timedelta(seconds=10))))
    assert not ok and "expired" in reason


def test_a_stale_quote_is_refused():
    ok, reason = admits(
        tdx_proof(issued_at=tee.stamp(NOW - timedelta(seconds=600)),
                  expires_at=tee.stamp(NOW + timedelta(seconds=600))),
        requirement(max_age_s=60.0))
    assert not ok and "older than the requirement's max_age_s" in reason


def test_a_future_dated_quote_is_refused():
    ok, reason = admits(tdx_proof(
        issued_at=tee.stamp(NOW + timedelta(seconds=300)),
        expires_at=tee.stamp(NOW + timedelta(seconds=900))))
    assert not ok and "ahead of" in reason


def test_a_quote_with_no_replay_ledger_is_refused():
    ok, reason = admits(tdx_proof(), replay_ledger=None)
    assert not ok and "no replay ledger" in reason


# ------------------------------------------- refusal 4: malformed, not mis-read


def test_an_evidence_that_declares_a_quote_format_and_carries_no_quote_is_refused():
    proof = tdx_proof()
    del proof[tee.QUOTE_FIELD]
    ok, reason = admits(proof)
    assert not ok and "carries no 'quote'" in reason


@pytest.mark.parametrize("value", ["", "zz", "abc", 7, None, [], {"a": 1}])
def test_an_unreadable_quote_member_is_refused_rather_than_parsed(value):
    proof = tdx_proof()
    proof[tee.QUOTE_FIELD] = value
    ok, reason = admits(proof)
    assert not ok, value
    assert ("carries no 'quote'" in reason or "not hex" in reason
            or "malformed TDX quote" in reason)


def test_a_truncated_quote_is_refused():
    proof = tdx_proof()
    proof[tee.QUOTE_FIELD] = proof[tee.QUOTE_FIELD][:400]
    ok, reason = admits(proof)
    assert not ok and "malformed TDX quote" in reason


def test_a_quote_whose_declared_length_does_not_close_it_is_refused():
    proof = tdx_proof()
    proof[tee.QUOTE_FIELD] = (bytes.fromhex(proof[tee.QUOTE_FIELD]) + b"\x00" * 4).hex()
    ok, reason = admits(proof)
    assert not ok and "bytes follow it" in reason


@pytest.mark.parametrize("record", [None, "a string", 7, [], b"bytes"])
def test_evidence_that_is_not_an_object_is_refused_rather_than_read(record):
    ok, reason = admits(record)
    assert not ok and "not an object" in reason


def test_a_malformed_endorsement_member_is_refused():
    proof = tdx_proof()
    proof[tee.ENDORSEMENT_FIELD] = "not an object"
    ok, reason = admits(proof)
    assert not ok and "not an object" in reason


# ------------------------------------ refusal 5: the two verifiers cannot cross


def test_a_symmetric_mac_evidence_is_refused_by_a_hardware_root():
    """A hardware root does not accept a record a shared secret could have
    written, even a well-formed one: that is the whole reason it exists."""
    dev = tee.sign_evidence(evidence(), b"an attestation authority key")
    ok, reason = admits(dev)
    assert not ok
    assert "a hardware attestation root decides a hardware quote" in reason


def test_a_hardware_quote_is_refused_by_the_development_verifier():
    ok, reason = admits(tdx_proof(), root=None,
                        attester_key=b"an attestation authority key")
    assert not ok
    assert "needs a hardware attestation root" in reason


def test_supplying_both_a_root_and_an_attester_key_refuses():
    """Two verifiers that can disagree is the defect class this item is about, so
    the ambiguity refuses instead of one of them silently winning."""
    ok, reason = admits(tdx_proof(), attester_key=b"a shared secret")
    assert not ok and "pass one" in reason


def test_supplying_neither_a_root_nor_a_key_refuses():
    ok, reason = admits(tdx_proof(), root=None)
    assert not ok
    assert "nothing to verify the evidence against" in reason


def test_requiring_a_hardware_root_refuses_the_development_verifier_outright():
    """One switch, checked before anything is read, for a caller that must not be
    satisfied by a shared secret."""
    dev = tee.sign_evidence(evidence(), b"an attestation authority key")
    ok, reason = admits(dev, root=None, attester_key=b"an attestation authority key",
                        require_hardware_root=True)
    assert not ok
    assert "no production attestation root" in reason
    ok, reason = admits(tdx_proof(), require_hardware_root=True)
    assert ok, reason


def test_no_peer_key_still_refuses_on_the_hardware_path():
    for key in (None, b"", "a string", 7):
        ok, reason = admits(tdx_proof(), peer_key=key)
        assert not ok, key
        assert "no peer key provided" in reason


# ------------------------------------------------ through the pool and the file


def offer(proof=None, trust="attested", peer_id=PEER_ID) -> dict:
    return sign_offer(
        PeerOffer(peer_id=peer_id, attestation=Attestation(trust=trust),
                  tee_proof=proof),
        PEER_KEY)


def test_an_attested_slot_is_filled_only_by_a_hardware_rooted_offer():
    slot = PlacementSlot(attested_tee=requirement())
    ok, reason = offer_eligible(offer(), slot, PEER_KEY, root=hardware_root(),
                                tee_ledger=set(), now=NOW)
    assert not ok and "no enclave evidence" in reason
    ok, reason = offer_eligible(offer(proof=tdx_proof()), slot, PEER_KEY,
                                root=hardware_root(), tee_ledger=set(), now=NOW)
    assert ok, reason


def test_re_signing_the_offer_around_a_forged_quote_does_not_help():
    """The offer signature covers `tee_proof`, so the peer can sign the wrapper
    honestly; what it cannot do is reach the root."""
    slot = PlacementSlot(attested_tee=requirement())
    forged = offer(proof=tdx_proof(platform_private_key=TDX_OTHER_PCK))
    ok, reason = offer_eligible(forged, slot, PEER_KEY, root=hardware_root(),
                                tee_ledger=set(), now=NOW)
    assert not ok
    assert reason.startswith("attestation:")
    assert "not signed by the platform key pinned as" in reason


def attest_table(**overrides) -> dict:
    table = {"requires": plc.TEE_WORD_ATTESTED, "bundle": BUNDLE,
             "measurements": [MRTD], "region": REGION, "nonce": NONCE}
    table.update(overrides)
    return table


def placement_file(table=None) -> dict:
    entry = {"components": ["Worker"], "backend": "py"}
    if table is not None:
        entry["attest"] = table
    return {"processes": {"worker": entry}}


def test_the_placement_file_spelling_reaches_the_hardware_root():
    ok, reason = plc.admit_peer_for_process(
        placement_file(attest_table()), "worker", offer(proof=tdx_proof()),
        offer_key=PEER_KEY, root=hardware_root(), tee_ledger=set(), now=NOW)
    assert ok, reason


def test_the_placement_file_verdict_is_still_the_verifiers_own():
    """The single-verdict property #964 established, now on the hardware path: the
    file spelling and `offer_eligible` must say the SAME thing about the same
    offer, byte for byte, or the placement surface has grown a second opinion."""
    record = offer(proof=tdx_proof(platform_private_key=TDX_OTHER_PCK))
    _, mine = plc.admit_peer_for_process(
        placement_file(attest_table()), "worker", record,
        offer_key=PEER_KEY, root=hardware_root(), tee_ledger=set(), now=NOW)
    slot, problem = plc.process_placement_slot(
        placement_file(attest_table()), "worker")
    assert problem is None
    _, theirs = offer_eligible(record, slot, PEER_KEY, root=hardware_root(),
                               tee_ledger=set(), now=NOW)
    assert mine == theirs


def test_the_placement_file_can_require_a_hardware_root():
    dev = tee.sign_evidence(evidence(), b"an attestation authority key")
    ok, reason = plc.admit_peer_for_process(
        placement_file(attest_table()), "worker", offer(proof=dev),
        offer_key=PEER_KEY, attester_key=b"an attestation authority key",
        require_hardware_root=True, tee_ledger=set(), now=NOW)
    assert not ok
    assert "no production attestation root" in reason


def test_a_placement_without_the_key_is_unchanged_by_the_root():
    """Additivity: a process that declares nothing pays nothing, and a root that
    is configured is not a demand that one be met."""
    ledger: set = set()
    ok, reason = plc.admit_peer_for_process(
        placement_file(), "worker", offer(trust="verified", proof=tdx_proof()),
        offer_key=PEER_KEY, root=hardware_root(), tee_ledger=ledger, now=NOW)
    assert ok, reason
    assert ledger == set()


# --------------------------------------------------- the committed fixture pins


def test_the_committed_good_fixtures_verify_against_the_manifests_own_root():
    """The fixture directory is the layout pin, and this is the link between it and
    the placement path: the same `verify_quote` these records go through decides
    the committed bytes."""
    for name, key in (("tdx_quote_good.bin", "tdx_platform"),
                      ("sev_snp_report_good.bin", "sev_vcek")):
        entry = MANIFEST["keys"][key]
        root = q.HardwareRoot(platform_keys=[
            q.PinnedKey(entry["curve"], bytes.fromhex(entry["public_key"]))])
        fmt = (q.SIGN_ALG_TDX if name.startswith("tdx") else q.SIGN_ALG_SEV_SNP)
        verdict = q.verify_quote(
            (FIXTURES / name).read_bytes(), sign_alg=fmt, root=root,
            expect_report_data=bytes.fromhex(MANIFEST["report_data"]),
            platform_key_id=entry["key_id"])
        assert verdict.ok, verdict.reason

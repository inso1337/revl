"""Signed peer capability + attestation advertisement (#480, Primitive 1).

These pin the offer signing/verification round-trip (grounded on the same
``attest`` hmac/canonical-hash primitive), the MAC-then-envelope order, domain
separation from an ``attest`` attestation, tamper detection, and the three-gate
placement match — signature, attested facets, and the ``grant_ceiling`` covering
the slot grant via the very ``cap_order.covers_set`` the monotonicity invariant
uses. Adversarial cases from the design (A7 forged attestation; ceiling that
does not cover the grant) refuse fail-closed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import attest, cap_order  # noqa: E402
from revl.peer_offer import (  # noqa: E402
    Attestation,
    OfferError,
    PeerOffer,
    PlacementSlot,
    ResourceNeed,
    ResourceOffer,
    offer_eligible,
    sign_offer,
    verify_offer,
)

KEY = b"pool-shared-secret"
OTHER_KEY = b"a-different-secret"


def make_offer(trust="verified", region="us-east", hardware="x86",
               cores=8, memory_bytes=1 << 34, max_time_s=600.0,
               ceiling=('fs.read(path="/data")', "net.fetch"),
               peer_id="peer-1") -> PeerOffer:
    return PeerOffer(
        peer_id=peer_id,
        attestation=Attestation(
            trust=trust, region=region, hardware=hardware,
            resource_offer=ResourceOffer(cores, memory_bytes, max_time_s)),
        grant_ceiling=tuple(ceiling))


# ------------------------------------------------------------- sign / verify


def test_sign_verify_round_trip():
    record = sign_offer(make_offer(), KEY)
    ok, reason = verify_offer(record, KEY)
    assert ok, reason


def test_signing_is_deterministic():
    a = sign_offer(make_offer(), KEY)
    b = sign_offer(make_offer(), KEY)
    assert a == b


def test_wrong_key_fails_verification():
    record = sign_offer(make_offer(), KEY)
    ok, reason = verify_offer(record, OTHER_KEY)
    assert not ok and "signature mismatch" in reason


def test_key_id_is_present_and_non_secret():
    record = sign_offer(make_offer(), KEY)
    assert record["key_id"] == attest.key_id(KEY)
    assert KEY.decode() not in str(record)


def test_tampering_with_trust_breaks_signature():
    record = sign_offer(make_offer(trust="verified"), KEY)
    record["attestation"]["trust"] = "local"  # forge a higher trust
    ok, reason = verify_offer(record, KEY)
    assert not ok and "signature mismatch" in reason


def test_tampering_with_ceiling_breaks_signature():
    record = sign_offer(make_offer(), KEY)
    record["grant_ceiling"].append("db.write")  # smuggle in more authority
    ok, reason = verify_offer(record, KEY)
    assert not ok and "signature mismatch" in reason


def test_missing_signature_is_a_refusal_not_a_crash():
    record = sign_offer(make_offer(), KEY)
    del record["signature"]
    ok, reason = verify_offer(record, KEY)
    assert not ok and "no signature" in reason


def test_no_key_refuses():
    record = sign_offer(make_offer(), KEY)
    ok, reason = verify_offer(record, b"")
    assert not ok and "no signing key" in reason


# --------------------------------------------------------- domain separation


def test_an_attestation_does_not_verify_as_a_peer_offer():
    # A2/cross-protocol: a revl.attestation MAC'd with the SAME key must not read
    # as a peer offer. Domain separation (revl.peer-offer/v1 vs revl.attestation)
    # makes it fail the MAC, not merely the envelope. Build an attestation-domain
    # record with attest's own signer so the ONLY difference from a peer offer is
    # the domain-separation prefix.
    body = {
        "kind": attest.ATTEST_KIND, "version": attest.ATTEST_VERSION,
        "verdict": attest.VERDICT_ADMITTED, "hash_alg": attest.HASH_ALG,
        "composition_hash": "0" * 64, "guarantees": ["G1"],
        "checker": {"compiler": "x", "ruleset": "0" * 64},
        "timestamp": "2026-09-07T00:00:00+00:00", "sign_alg": attest.SIGN_ALG,
        "signer": None, "key_id": attest.key_id(KEY),
    }
    att = {**body, "signature": attest._sign(body, KEY)}
    # authentic under attest's own verifier (same key, attestation domain)...
    assert attest.verify_attestation(att, KEY)[0]
    # ...but the peer-offer verifier rejects it at the MAC (different domain).
    ok, reason = verify_offer(att, KEY)
    assert not ok and "signature mismatch" in reason


def test_a_peer_offer_does_not_verify_as_an_attestation():
    record = sign_offer(make_offer(), KEY)
    ok, reason = attest.verify_attestation(record, KEY)
    assert not ok


# ------------------------------------------------------------ envelope checks


def test_unknown_trust_level_refused_at_construction():
    with pytest.raises(OfferError):
        Attestation(trust="platinum")


def test_negative_resource_refused_at_construction():
    with pytest.raises(OfferError):
        Attestation(trust="verified", resource_offer=ResourceOffer(cores=-1))


def test_unparseable_ceiling_refused_at_construction():
    with pytest.raises(cap_order.CapError):
        PeerOffer("p", Attestation(trust="verified"),
                  grant_ceiling=("fs.read(bogus=1)",))


def test_authentic_but_wrong_kind_refused_by_envelope():
    record = sign_offer(make_offer(), KEY)
    record["kind"] = "revl.something-else"
    record["signature"] = None  # force re-sign path off; recompute below
    # re-sign so the MAC passes and only the envelope can catch it
    from revl import peer_offer as po
    record.pop("signature")
    record["signature"] = po._sign(record, KEY)
    ok, reason = verify_offer(record, KEY)
    assert not ok and "envelope refused" in reason and "kind" in reason


# ------------------------------------------------------- placement matching


def slot(trust_floor="verified", regions=None, hardware=None,
         need=ResourceNeed(), grant=('fs.read(path="/data/jobs")',),
         budgets=None) -> PlacementSlot:
    return PlacementSlot(
        trust_floor=trust_floor,
        regions=frozenset(regions) if regions is not None else None,
        hardware=frozenset(hardware) if hardware is not None else None,
        need=need, grant=tuple(grant), budgets=budgets or {})


def test_eligible_when_everything_matches():
    record = sign_offer(make_offer(), KEY)
    eligible, reason = offer_eligible(record, slot(), KEY)
    assert eligible, reason


def test_forged_trust_is_caught_by_the_signature_A7():
    # design A7: a peer advertises a trust it did not earn. If it forges the
    # signed record, the signature fails; a HONEST verified offer simply does not
    # clear an `attested` floor.
    honest = sign_offer(make_offer(trust="verified"), KEY)
    eligible, reason = offer_eligible(honest, slot(trust_floor="attested"), KEY)
    assert not eligible and "below the slot floor" in reason


def test_trust_at_or_above_floor_is_eligible():
    record = sign_offer(make_offer(trust="local"), KEY)
    eligible, reason = offer_eligible(record, slot(trust_floor="attested"), KEY)
    assert eligible, reason


def test_region_not_in_allowed_set_is_ineligible():
    record = sign_offer(make_offer(region="eu-west"), KEY)
    eligible, reason = offer_eligible(record, slot(regions=["us-east"]), KEY)
    assert not eligible and "region" in reason


def test_hardware_not_in_allowed_set_is_ineligible():
    record = sign_offer(make_offer(hardware="arm64"), KEY)
    eligible, reason = offer_eligible(record, slot(hardware=["x86"]), KEY)
    assert not eligible and "hardware" in reason


def test_insufficient_resources_are_ineligible():
    record = sign_offer(make_offer(cores=2), KEY)
    eligible, reason = offer_eligible(record, slot(need=ResourceNeed(cores=4)), KEY)
    assert not eligible and "resource offer" in reason


def test_ceiling_must_cover_the_slot_grant_seam_to_primitive_3():
    # (c): the slot would hand a grant the peer's advertised ceiling does not
    # cover, so the peer is ineligible. This is the seam to Primitive 3: a peer
    # is never offered a grant beyond its own ceiling.
    record = sign_offer(make_offer(ceiling=('fs.read(path="/data")',)), KEY)
    eligible, reason = offer_eligible(record, slot(grant=("db.write",)), KEY)
    assert not eligible and "ceiling" in reason


def test_ceiling_covers_a_narrower_slot_grant():
    record = sign_offer(make_offer(ceiling=('fs.read(path="/data")',)), KEY)
    eligible, reason = offer_eligible(
        record, slot(grant=('fs.read(path="/data/jobs/42")',)), KEY)
    assert eligible, reason


def test_a_wider_slot_grant_than_ceiling_cone_is_refused():
    # ceiling is fs.read(path="/data/jobs"); slot wants the wider fs.read(path="/data")
    record = sign_offer(make_offer(ceiling=('fs.read(path="/data/jobs")',)), KEY)
    eligible, reason = offer_eligible(
        record, slot(grant=('fs.read(path="/data")',)), KEY)
    assert not eligible and "ceiling" in reason


def test_unsigned_offer_is_never_matched():
    record = sign_offer(make_offer(), KEY)
    record["signature"] = "deadbeef"
    eligible, reason = offer_eligible(record, slot(), KEY)
    assert not eligible and "signature" in reason

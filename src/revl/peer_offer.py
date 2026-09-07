"""Signed peer capability + attestation advertisement for the verifiable
private peer pool (#480, Primitive 1).

A private pool needs a peer to PROVE, not merely claim, what it is willing to
run and what it can attest about itself before the dispatcher (Primitive 2) will
hand it a grant. This module is that proof: a :class:`PeerOffer` binds a stable
peer identity to an :class:`Attestation` (trust level, region, hardware, and the
resources it will lend) and a ``grant_ceiling`` — the MOST authority the peer
will ever accept — and signs the whole record so a verifier can confirm its
provenance without trusting the wire.

Why this reuses ``attest`` wholesale
------------------------------------
``attest.py`` already signs "this exact composition passed the gate": a
canonical, sort-keyed, separator-stable serialization hashed and MAC'd with a
stdlib ``hmac`` keyed signature, plus a non-secret ``key_id`` fingerprint so a
verifier learns WHICH key signed without seeing it. A peer offer signs "this
exact peer offers this exact attestation and grant ceiling" — the same
primitive, a different payload. So this module borrows ``attest``'s
``_canonical_bytes`` and ``key_id`` verbatim and mirrors its
MAC-then-envelope verification order. It pulls in no new crypto dependency
(``attest`` is deliberately stdlib-only), and it touches no lexer, typecheck,
selfhost, or rust emit — so it is Python-only and drags no gate crate.

The MAC carries its OWN domain-separation prefix (:data:`SIGN_DOMAIN`,
``revl.peer-offer/v1``), distinct from ``attest``'s ``revl.attestation/v2`` and
``deploy``'s receipt domain. Without it, ``hmac(key, canonical(body))`` is the
same construction all three use, so an attestation or a deploy receipt could be
replayed as a peer offer under a shared key. Three protocols, three domains.

What a signature does and does NOT prove
----------------------------------------
The signature proves PROVENANCE — that the holder of a known key authored this
offer — not good runtime behavior. A peer that advertises ``trust: verified`` it
did not earn is still caught only at the level the placement file is trusted
(design A7): matching happens on a verified signature, but the wire stays
hostile and the #475 hostile-wire TCK is the consumer that keeps it honest. The
monotonicity invariant (Primitive 3) bounds what a peer may RECEIVE; it does not
bound what a malicious peer may DO with received authority — that is the
sandbox/seam's job.

The seam to Primitive 3
-----------------------
:func:`offer_eligible` matches an offer against a :class:`PlacementSlot`. Point
(c) of that match — the offer's ``grant_ceiling`` must COVER the grant the slot
would hand it — is computed with the very ``cap_order.covers_set`` the
authority-monotonicity invariant uses. So the dispatcher never offers a peer a
grant the peer's own advertised ceiling does not cover, and the invariant
guarantees that ceiling is itself covered by the delegating composition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from . import cap_order
from .attest import NotCanonicalizable, _canonical_bytes, key_id

# The peer-offer envelope identity (mirrors `attest`'s kind/version idea: a
# self-identifying tag plus a MAJOR.MINOR line, additive within a MAJOR).
OFFER_KIND = "revl.peer-offer"
OFFER_VERSION = "1.0"

# One signature algorithm today; the member exists and is VALIDATED so an
# asymmetric upgrade is an additive change, exactly as `attest.SIGN_ALG`.
SIGN_ALG = "hmac-sha256"

#: The domain-separation prefix folded into every peer-offer MAC. It carries the
#: envelope version, so a v1 offer cannot be replayed as a future v2 one, and it
#: differs from `attest.SIGN_DOMAIN` / the deploy-receipt domain so a signature
#: from one protocol never verifies as another under a shared key.
SIGN_DOMAIN = b"revl.peer-offer/v1\x00"

SIGNATURE_FIELD = "signature"

#: Peer trust levels, LEAST to MOST trusted. `verified` is the floor a signed
#: offer clears by having its signature verify; `attested` adds a hardware/root
#: attestation; `local` is a peer under the operator's own control. The order is
#: what a placement's `trust >= verified` floor and the dispatcher's
#: secret-bearing gate (`>= attested`, "never a bare verified offer") compare
#: against. `trusted` is accepted as a synonym for `local` (design prose names
#: the {attested, local} set "trusted").
TRUST_ORDER: dict[str, int] = {"verified": 0, "attested": 1, "local": 2}
_TRUST_ALIASES = {"trusted": "local"}


class OfferError(ValueError):
    """A peer offer is malformed at construction time (bad trust level, a
    negative resource, an unparseable grant-ceiling capability). Distinct from a
    verification refusal, which is reported as ``(ok, reason)`` and never
    raises, so a hostile peer-supplied record cannot break that contract."""


def _trust_rank(level: str) -> int:
    """The rank of a trust level, resolving the `trusted`->`local` synonym.
    Raises :class:`OfferError` for an unknown level — fail-closed: an
    unrecognized trust word is never silently treated as some default."""
    canon = _TRUST_ALIASES.get(level, level)
    if canon not in TRUST_ORDER:
        raise OfferError(
            f"unknown trust level {level!r}; expected one of "
            f"{', '.join(sorted(TRUST_ORDER))}")
    return TRUST_ORDER[canon]


@dataclass(frozen=True)
class ResourceOffer:
    """The resources a peer will lend: CPU cores, memory, and the longest job
    it will accept. All non-negative; ``max_time_s`` is a wall-clock ceiling in
    seconds. Compared against a slot's :class:`ResourceNeed` facet-by-facet."""

    cores: int = 0
    memory_bytes: int = 0
    max_time_s: float = 0.0

    def satisfies(self, need: "ResourceNeed") -> bool:
        """True iff this offer meets or exceeds every facet of ``need``."""
        return (self.cores >= need.cores
                and self.memory_bytes >= need.memory_bytes
                and self.max_time_s >= need.max_time_s)

    def as_dict(self) -> dict:
        return {"cores": self.cores, "memory_bytes": self.memory_bytes,
                "max_time_s": self.max_time_s}


@dataclass(frozen=True)
class ResourceNeed:
    """The minimum resources a placement slot needs. An offer is resource-
    eligible iff its :class:`ResourceOffer` ``satisfies`` this."""

    cores: int = 0
    memory_bytes: int = 0
    max_time_s: float = 0.0


@dataclass(frozen=True)
class Attestation:
    """What a peer PROVES about itself, not merely claims: its trust level and
    the placement facets a slot matches against.

    ``trust`` is one of :data:`TRUST_ORDER` (or the ``trusted`` synonym).
    ``region`` and ``hardware`` are opaque discrete facets matched against a
    slot's allowed sets. ``resource_offer`` is what the peer will lend."""

    trust: str
    region: str = ""
    hardware: str = ""
    resource_offer: ResourceOffer = field(default_factory=ResourceOffer)

    def __post_init__(self) -> None:
        _trust_rank(self.trust)  # fail-closed on an unknown level, at build time
        for facet in ("cores", "memory_bytes", "max_time_s"):
            if getattr(self.resource_offer, facet) < 0:
                raise OfferError(
                    f"resource_offer.{facet} is negative; a resource offer is a "
                    f"non-negative allowance")

    def as_dict(self) -> dict:
        return {"trust": self.trust, "region": self.region,
                "hardware": self.hardware,
                "resource_offer": self.resource_offer.as_dict()}


@dataclass(frozen=True)
class PeerOffer:
    """A peer's signed advertisement: identity + attestation + grant ceiling.

    ``grant_ceiling`` is the MOST authority the peer will ever accept, spelled in
    ``cap_order``'s grammar. The dispatcher (Primitive 2) may hand this peer only
    a grant this ceiling COVERS (`cap_order.covers_set` empty), which is the seam
    to the monotonicity invariant (Primitive 3)."""

    peer_id: str
    attestation: Attestation
    grant_ceiling: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.peer_id:
            raise OfferError("a peer offer needs a non-empty peer_id")
        # fail-closed: an unparseable ceiling capability raises now, not silently
        # at match time where it might read as "covers nothing" and be skipped.
        for cap in self.grant_ceiling:
            cap_order.parse_cap(cap)

    def ceiling_caps(self) -> list[cap_order.Cap]:
        return [cap_order.parse_cap(c) for c in self.grant_ceiling]

    def body(self) -> dict:
        """The signed body of the offer — every member EXCEPT the signature, in
        a shape whose canonical bytes are a pure function of the offer's
        content. ``grant_ceiling`` is sorted so member order never changes the
        signature."""
        return {
            "kind": OFFER_KIND,
            "version": OFFER_VERSION,
            "peer_id": self.peer_id,
            "attestation": self.attestation.as_dict(),
            "grant_ceiling": sorted(self.grant_ceiling),
            "sign_alg": SIGN_ALG,
        }


def _sign(body: Mapping, key: bytes) -> str:
    """HMAC-SHA256 over :data:`SIGN_DOMAIN` ++ the canonical body bytes, hex.
    ``body`` is the record with any ``signature`` member removed; canonical
    serialization sorts keys, so member order does not affect the signature."""
    import hashlib  # noqa: PLC0415 — lazy, no import side effect
    import hmac  # noqa: PLC0415

    signed = {k: v for k, v in body.items() if k != SIGNATURE_FIELD}
    return hmac.new(bytes(key), SIGN_DOMAIN + _canonical_bytes(signed),
                    hashlib.sha256).hexdigest()


def sign_offer(offer: PeerOffer, key: bytes) -> dict:
    """Build a signed peer-offer record: the offer body plus a ``key_id``
    fingerprint and a ``signature`` over the whole body. Pure and deterministic
    given ``(offer, key)`` — the same inputs always produce byte-identical
    output, which is what makes the round-trip testable.

    The signature covers every member (kind, version, peer_id, attestation,
    grant_ceiling, sign_alg, key_id), so altering, dropping, or adding any one
    breaks it exactly as a tampered attestation does."""
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise OfferError("signing key must be non-empty bytes")
    body = offer.body()
    body["key_id"] = key_id(bytes(key))
    body[SIGNATURE_FIELD] = _sign(body, key)
    return body


def _validate_envelope(record: Mapping) -> str:
    """Is this authentic record even a peer offer of the shape we accept?
    Returns a refusal reason, or ``""`` when well formed. A MAC proves
    authorship, not that what was authored means what a reader assumes, so every
    fixed-meaning member is checked here (mirrors ``attest._validate_envelope``).
    """
    for member, expected in (("kind", OFFER_KIND),
                             ("version", OFFER_VERSION),
                             ("sign_alg", SIGN_ALG)):
        if record.get(member) != expected:
            return (f"envelope refused: {member} is {record.get(member)!r}, "
                    f"expected {expected!r}")

    peer_id = record.get("peer_id")
    if not isinstance(peer_id, str) or not peer_id:
        return f"envelope refused: peer_id is not a non-empty string ({peer_id!r})"

    att = record.get("attestation")
    if not isinstance(att, dict):
        return f"envelope refused: attestation is not an object ({att!r})"
    trust = att.get("trust")
    canon = _TRUST_ALIASES.get(trust, trust) if isinstance(trust, str) else trust
    if canon not in TRUST_ORDER:
        return (f"envelope refused: attestation.trust is {trust!r}, expected one "
                f"of {', '.join(sorted(TRUST_ORDER))}")
    for facet in ("region", "hardware"):
        if not isinstance(att.get(facet), str):
            return f"envelope refused: attestation.{facet} is not a string ({att.get(facet)!r})"
    ro = att.get("resource_offer")
    if not isinstance(ro, dict):
        return f"envelope refused: attestation.resource_offer is not an object ({ro!r})"
    for facet in ("cores", "memory_bytes", "max_time_s"):
        val = ro.get(facet)
        if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 0:
            return (f"envelope refused: resource_offer.{facet} is not a "
                    f"non-negative number ({val!r})")

    ceiling = record.get("grant_ceiling")
    if not isinstance(ceiling, list) or not all(isinstance(c, str) for c in ceiling):
        return f"envelope refused: grant_ceiling is not a list of capability strings ({ceiling!r})"
    try:
        for cap in ceiling:
            cap_order.parse_cap(cap)
    except cap_order.CapError as error:
        return f"envelope refused: grant_ceiling has an unparseable capability ({error})"

    kid = record.get("key_id")
    import re  # noqa: PLC0415
    if not isinstance(kid, str) or not re.fullmatch(r"[0-9a-f]{16}", kid):
        return f"envelope refused: key_id is not a key fingerprint ({kid!r})"
    return ""


def verify_offer(record: Mapping, key: bytes) -> tuple[bool, str]:
    """Check a signed peer-offer record with ``key``. Returns ``(ok, reason)``
    and NEVER raises — a hostile peer-supplied record must not be able to break
    that contract (the wire is hostile; #475 is the consumer).

    Order mirrors ``attest.verify_attestation``: prove authenticity (the MAC)
    FIRST, then validate the envelope. A ``revl.attestation`` or a deploy receipt
    signed with this key fails the MAC here — domain separation — rather than
    being read as a peer offer."""
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False, "no signing key provided"
    if not isinstance(record, Mapping):
        return False, "peer offer is not an object"
    given = record.get(SIGNATURE_FIELD)
    if not isinstance(given, str):
        return False, "peer offer has no signature"

    import hmac  # noqa: PLC0415
    try:
        expected = _sign(record, key)
    except NotCanonicalizable as error:
        return False, f"peer offer cannot be verified: {error}"
    if not hmac.compare_digest(expected, given):
        return False, ("signature mismatch: wrong key, or the offer was tampered "
                       "with after signing")

    envelope = _validate_envelope(record)
    if envelope:
        return False, envelope
    return True, "valid: peer offer is authentic and well formed"


@dataclass(frozen=True)
class PlacementSlot:
    """A placement slot the dispatcher wants to fill: the constraints a peer must
    satisfy and the grant the slot would hand a matched peer.

    ``trust_floor`` is the minimum trust level (`trust >= floor`). ``regions`` /
    ``hardware`` are the allowed discrete sets — ``None`` means "any". ``need`` is
    the minimum resource offer. ``grant`` / ``budgets`` are the authority the slot
    hands the peer; an eligible peer's ``grant_ceiling`` must COVER ``grant``."""

    trust_floor: str = "verified"
    regions: Optional[frozenset[str]] = None
    hardware: Optional[frozenset[str]] = None
    need: ResourceNeed = field(default_factory=ResourceNeed)
    grant: tuple[str, ...] = ()
    budgets: Mapping[str, int] = field(default_factory=dict)

    def grant_caps(self) -> list[cap_order.Cap]:
        return [cap_order.parse_cap(c) for c in self.grant]


def offer_eligible(record: Mapping, slot: PlacementSlot, key: bytes
                   ) -> tuple[bool, str]:
    """Is a signed offer ELIGIBLE for ``slot``? Returns ``(eligible, reason)``.

    Three gates, in order (design "Matching against placement constraints"):

    (a) the signature verifies (:func:`verify_offer`) — no unsigned or tampered
        offer is ever considered;
    (b) the attested facets satisfy the slot: ``trust`` at or above the floor,
        ``region``/``hardware`` in the allowed sets, resources at or above need;
    (c) the offer's ``grant_ceiling`` COVERS the grant the slot would hand it,
        under ``cap_order.covers_set`` — the peer never receives a grant its own
        advertised ceiling does not cover. This is the seam to Primitive 3.

    Never raises: an unparseable slot grant is reported as ineligible, so a
    caller iterating candidate peers cannot be crashed by one bad slot."""
    ok, reason = verify_offer(record, key)
    if not ok:
        return False, f"signature: {reason}"

    att = record["attestation"]
    if _trust_rank(att["trust"]) < _trust_rank(slot.trust_floor):
        return False, (f"trust {att['trust']!r} is below the slot floor "
                       f"{slot.trust_floor!r}")
    if slot.regions is not None and att.get("region", "") not in slot.regions:
        return False, (f"region {att.get('region', '')!r} not in the slot's "
                       f"allowed set {sorted(slot.regions)}")
    if slot.hardware is not None and att.get("hardware", "") not in slot.hardware:
        return False, (f"hardware {att.get('hardware', '')!r} not in the slot's "
                       f"allowed set {sorted(slot.hardware)}")
    ro = att["resource_offer"]
    offered = ResourceOffer(int(ro["cores"]), int(ro["memory_bytes"]),
                            float(ro["max_time_s"]))
    if not offered.satisfies(slot.need):
        return False, (f"resource offer {offered.as_dict()} does not meet the "
                       f"slot need cores>={slot.need.cores}, "
                       f"memory_bytes>={slot.need.memory_bytes}, "
                       f"max_time_s>={slot.need.max_time_s}")

    try:
        ceiling = [cap_order.parse_cap(c) for c in record["grant_ceiling"]]
        wanted = slot.grant_caps()
    except cap_order.CapError as error:
        return False, f"a capability spelling is unparseable: {error}"
    uncovered = cap_order.covers_set(ceiling, wanted)
    if uncovered:
        names = ", ".join(sorted(f"`{c.to_str()}`" for c in uncovered))
        return False, (f"the slot grant exceeds the peer's advertised ceiling: "
                       f"{names} not covered by grant_ceiling")
    return True, "eligible: signature verifies, facets satisfy, ceiling covers the grant"

"""Attested-TEE placement and signed results (roadmap item 475, issue #827).

The verifiable private peer pool (#480, ``peer_offer``) matches a peer against a
placement slot on facts the peer ASSERTS and signs: trust level, region,
hardware, resources, and a capability ceiling. A slot that needs a confidential
worker cannot be satisfied by an assertion, because the assertion is made by
exactly the party whose honesty is in question. It needs remote attestation:
evidence, produced for the enclave, that the thing about to run the bundle is the
thing the operator approved.

This module is the typed requirement, the evidence, and the fail-closed
verifier, plus the other half of the same claim: the composition accepts a result
only when that result arrives signed and bound to the challenge the placement
issued.

Three objects
-------------
* :class:`TeeRequirement` is what a placement DEMANDS, as checkable facts rather
  than prose: the approved bundle's content hash, the permitted enclave
  measurements, the permitted regions, the required network posture (outbound
  forbidden), a challenge nonce, and the maximum age of an acceptable proof. A
  requirement with no permitted measurement, no region, no challenge, or a
  network posture other than ``forbidden`` is refused when it is BUILT, because
  each of those makes the requirement unfalsifiable.
* :class:`EnclaveEvidence` is what the attester returns: the peer it is about,
  the bundle, the measurement, the region, the challenge it answers, its
  validity window and the network posture, all covered by a MAC.
* :class:`ResultReceipt` is what travels with a result: the peer, the bundle, the
  challenge and the hash of the result bytes.

The crypto is the primitive ``attest`` and ``peer_offer`` already use: a
canonical, sort-keyed, separator-stable serialization, MAC'd with stdlib
``hmac``, plus a non-secret ``key_id`` fingerprint and its own
domain-separation prefix, so a ``revl.attestation``, a ``revl.peer-offer``, a
``revl.deploy.receipt`` and one of these records never verify as each other
under a shared key.

Two roots, and only one of them is an attestation root
-----------------------------------------------------
A record's ``sign_alg`` says which verifier may decide it, and the two can never
be crossed.

**The attestation root** (``revl.tee_quote``, :class:`~revl.tee_quote.HardwareRoot`)
decides a record whose ``sign_alg`` is a real quote format: an Intel TDX Quote v4
(``tdx-quote-v4-ecdsa-p256``) or an AMD SEV-SNP attestation report
(``sev-snp-report-v2-ecdsa-p384``). The bytes are parsed as the vendor defines
them and verified with ECDSA up a chain that terminates in a public key the
OPERATOR pinned out of band. The peer holds no part of that chain, so a forged
quote has nothing to sign with and a replayed one answers the wrong challenge.
Two bindings make the hardware signature cover the whole claim rather than only
the hardware's own fields:

* every claim the hardware does not measure — the peer, the bundle, the region,
  the network posture, the validity window, the challenge — is digested into the
  64-byte ``report_data``, the one field of either format that the workload
  chooses (:func:`evidence_report_data`). Alter any member and the quote stops
  matching;
* the measurement the record STATES is compared against the measurement register
  the hardware REPORTED (MRTD for a TD, the launch measurement for a SEV-SNP
  guest), because ``report_data`` binds what the workload said and the register
  is what the platform measured. Without that comparison an enclave running
  anything at all could bind a body naming a permitted measurement.

One honest limit, stated rather than papered over: ``bundle`` is a LABEL, and no
hardware format measures it. What the hardware proves is the MEASUREMENT, and the
requirement's permitted-measurement set is where the operator states which
measurements correspond to the approved bundle. A permitted set that is
re-attested as builds move, instead of baked into the requirement, is roadmap
item 469.

**The development verifier** (:class:`~revl.tee_quote.DevMacRoot`) decides a
record whose ``sign_alg`` is ``hmac-sha256``. It is not an attestation root: a
MAC proves only that the holder of a key authored the record, so if the PEER ever
holds the attester key the requirement degenerates into an assertion. It is kept
because callers holding only a shared secret would otherwise break, and it is
fenced three ways — it reports ``is_production = False``, constructing one
requires ``acknowledged_dev_only=True`` by name, and every verdict it reaches,
admission included, carries ``tee_quote.DEV_ROOT_NOTE`` in its own reason text.
``tee_admits(..., require_hardware_root=True)`` refuses it outright.

The key separation survives both. :func:`tee_admits` refuses when the attester key
is the peer's own offer key, when the record names that key as the platform that
signed its quote, and when no peer key is supplied at all (the empty key
included), because a separation that cannot be checked is not a separation.
:func:`receipt_admits` refuses on the same ground for a receipt key; wiring
receipts into the composition's call path is the other half of this item and is
still open.

Fail-closed, everywhere
-----------------------
Every ambiguous case refuses: an absent proof, a proof for another peer, another
bundle, another measurement, another region or another challenge; an expired or
stale proof; a proof or receipt presented twice for the same challenge; a network
posture other than the one demanded; an unparseable member; a receipt whose
signature, whose result bytes or whose run do not match. Every verifier returns
``(ok, reason)`` and refuses rather than raises on a malformed proof, so a
hostile record cannot break a caller that iterates candidate peers.

The one exception is INHERITED from the pool and is stated rather than papered
over. A signature is checked with ``hmac.compare_digest``, exactly as
``peer_offer.verify_offer`` checks one, and that call raises ``TypeError:
comparing strings with non-ASCII characters is not supported`` when the
record's ``signature`` member is a ``str`` carrying a non-ASCII character. So a
record with such a signature raises out of these verifiers instead of being
refused. The comparison is deliberately left identical to the pool's, so the two
verifiers behave the same way; an ASCII check before it would close the hole and
is a follow-up rather than part of this slice.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, MutableSet, Optional

from .attest import NotCanonicalizable, _canonical_bytes, canonical_hash, key_id
from .tee_quote import (
    DEV_ROOT_NOTE,
    QUOTE_SIGN_ALGS,
    SIGN_ALG_SEV_SNP,
    SIGN_ALG_TDX,
    AttestationRoot,
    DevMacRoot,
    HardwareRoot,
    QuoteFormatError,
    RootError,
    build_sev_snp_report,
    build_tdx_quote,
    require_production_root,
    verify_quote,
)

# The two envelope identities. Same discipline as `attest`/`peer_offer`: a
# self-identifying kind plus a MAJOR.MINOR line, additive within a MAJOR.
EVIDENCE_KIND = "revl.tee-evidence"
EVIDENCE_VERSION = "1.0"
RECEIPT_KIND = "revl.tee-receipt"
RECEIPT_VERSION = "1.0"

# The DEVELOPMENT signature algorithm, and it is named twice on purpose: `SIGN_ALG`
# is what it has always been called and what a pre-root record carries, and
# `DEV_SIGN_ALG` is what it IS. A record carrying it is decided by the
# symmetric-MAC verifier, which is not an attestation root (`tee_quote`
# `DevMacRoot`): it proves that the holder of a key authored the record, so it is
# a development and test verifier and every verdict reached through it says so.
SIGN_ALG = "hmac-sha256"
DEV_SIGN_ALG = SIGN_ALG

#: The hardware-rooted algorithms, from `tee_quote`. An evidence carrying one of
#: these is decided by parsing a real vendor quote and verifying it against a key
#: the OPERATOR pinned, which the peer does not hold.
EVIDENCE_SIGN_ALGS = (SIGN_ALG, SIGN_ALG_TDX, SIGN_ALG_SEV_SNP)

#: Domain separation for the 64 bytes a quote must carry. `report_data` is the
#: only field of a TDX quote or a SEV-SNP report that the workload chooses, so it
#: is where every claim the hardware does not itself measure gets bound: the
#: digest is taken over the evidence's whole canonical body, which makes the
#: hardware signature cover the peer, the bundle, the region, the posture, the
#: window and above all the CHALLENGE. SHA-512 is not a taste: its 64-byte digest
#: is exactly the width both formats give the field, so no truncation or padding
#: decision exists to get wrong.
EVIDENCE_BIND_DOMAIN = b"revl.tee-evidence.report-data/v1\x00"

#: Where a hardware quote rides in the evidence record, hex-encoded. It REPLACES
#: `signature`: the quote is the signature.
QUOTE_FIELD = "quote"
#: The optional vendor endorsement that lets a pinned vendor root speak for a
#: platform key the operator has not pinned directly.
ENDORSEMENT_FIELD = "platform_endorsement"

#: Domain-separation prefixes. The evidence and the receipt are two protocols,
#: and both differ from `attest.SIGN_DOMAIN`, `peer_offer.SIGN_DOMAIN` and the
#: deploy-receipt domain, so no record from one verifies as another.
EVIDENCE_SIGN_DOMAIN = b"revl.tee-evidence/v1\x00"
RECEIPT_SIGN_DOMAIN = b"revl.tee-receipt/v1\x00"

SIGNATURE_FIELD = "signature"

#: The only network posture this item defines a requirement for. A worker that
#: holds an attested TEE and an open outbound network is not the confidential
#: placement: the composition's inputs and outputs would leave through a channel
#: the enclave does not mediate, so "outbound allowed" is a request for no
#: requirement at all and is refused where it is spelled.
OUTBOUND_FORBIDDEN = "forbidden"
OUTBOUND_ALLOWED = "allowed"
_OUTBOUND_POSTURES = (OUTBOUND_ALLOWED, OUTBOUND_FORBIDDEN)

#: How far the attester's clock may be AHEAD of the verifier's before an
#: evidence's `issued_at` reads as a forgery rather than a skew. A proof issued
#: in the future is not a proof of a run that has happened.
CLOCK_SKEW_S = 30.0

#: The default freshness window. A requirement tightens it; there is no value
#: here that makes a proof acceptable forever, because a proof of the past is
#: not a proof of the bundle being placed now.
DEFAULT_MAX_AGE_S = 120.0

# A canonical content hash: exactly what `attest.canonical_hash` emits, and the
# identity this module binds a bundle or a result to.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# An enclave measurement: a hex digest whose LENGTH is vendor-defined (32 bytes
# for an SGX MRENCLAVE, 48 for a TDX MRTD), so any even-length hex string of at
# least 16 bytes is accepted and compared by exact string equality.
_MEASUREMENT_RE = re.compile(r"(?:[0-9a-f]{2}){16,}")
_KEY_ID_RE = re.compile(r"[0-9a-f]{16}")


class TeeError(ValueError):
    """A TEE requirement, evidence or receipt is malformed at construction time
    (no permitted measurement, no region, no challenge, an unknown network
    posture, a member that is not a digest). Distinct from a verification
    refusal, which is reported as ``(ok, reason)`` and refuses rather than raises
    on a malformed proof, so a hostile peer-supplied record cannot break that
    contract (the module docstring records the one inherited exception, a
    non-ASCII ``signature`` string)."""


def bundle_identity(bundle: Any) -> str:
    """The approved-bundle identity: the ``attest.canonical_hash`` of the bundle
    document, the same content hash item 127 binds a verdict to. Takes the
    bundle itself (any canonicalizable document) and returns the hex digest a
    :class:`TeeRequirement` is spelled with."""
    try:
        return canonical_hash(bundle)
    except NotCanonicalizable as error:
        raise TeeError(f"the bundle has no canonical content hash: {error}") from error


def result_identity(result: Any) -> str:
    """The identity of a result value, by the same content hash. The composition
    recomputes this over the bytes it actually received and requires the
    enclave's receipt to name it."""
    try:
        return canonical_hash(result)
    except NotCanonicalizable as error:
        raise TeeError(f"the result has no canonical content hash: {error}") from error


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _instant(text: Any) -> Optional[datetime]:
    """Parse an ISO 8601 instant, or ``None`` when the string is not one. A naive
    stamp reads as UTC (the attester's clock is not the verifier's locale)."""
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def stamp(moment: datetime) -> str:
    """The canonical spelling of an instant in this module: UTC, ISO 8601, with
    an explicit offset. Used for `issued_at`/`expires_at` so an evidence the
    attester built and a proof the verifier parses share one spelling."""
    return moment.astimezone(timezone.utc).isoformat()


def _require_sha256(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise TeeError(
            f"{what} must be a canonical content hash (64 lowercase hex digits, "
            f"as `revl.attest.canonical_hash` produces), got {value!r}")
    return value


def _require_measurement(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _MEASUREMENT_RE.fullmatch(value):
        raise TeeError(
            f"{what} must be a lowercase hex digest of at least 16 bytes (an "
            f"enclave measurement, whose length is vendor-defined), got {value!r}")
    return value


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise TeeError(f"{what} must be a non-empty string, got {value!r}")
    return value


def _require_window(issued_at: Any, expires_at: Any) -> tuple[datetime, datetime]:
    issued = _instant(issued_at)
    expires = _instant(expires_at)
    if issued is None or expires is None:
        raise TeeError(
            f"issued_at/expires_at must be ISO 8601 instants, got "
            f"issued_at={issued_at!r} expires_at={expires_at!r}")
    if expires <= issued:
        raise TeeError(
            f"the validity window is empty or backwards: expires_at="
            f"{expires_at!r} is not after issued_at={issued_at!r}")
    return issued, expires


def _key_bytes(key: Any) -> bytes:
    """A caller-supplied key as bytes, or ``b""`` when there is no usable key.

    A key of the wrong type is ABSENT rather than compared. The ``bytes(key or
    b"")`` idiom this replaces raised ``TypeError`` on a ``str`` key, because
    ``bytes("k")`` needs an encoding, so a caller reaching a gate directly with
    one would have crashed the verifier instead of being refused by it."""
    return bytes(key) if isinstance(key, (bytes, bytearray)) else b""


# --------------------------------------------------------------------------
# The requirement a placement states
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TeeRequirement:
    """What a placement DEMANDS of an attested worker, as checkable facts.

    ``bundle`` is the approved bundle's content hash (:func:`bundle_identity`).
    ``measurements`` is the set of permitted enclave measurements: an empty set
    would accept any enclave, so it is refused. ``regions`` is the set of
    permitted regions, on the same terms. ``nonce`` is the challenge this
    placement issues; evidence must answer THIS challenge and a challenge is
    consumable once (see :func:`tee_admits`). ``outbound_network`` is the
    required posture, and ``forbidden`` is the only value this item defines:
    anything else asks for no requirement. ``max_age_s`` bounds how old a proof
    may be at the moment it is checked.
    """

    bundle: str
    measurements: frozenset[str]
    nonce: str
    regions: frozenset[str]
    outbound_network: str = OUTBOUND_FORBIDDEN
    max_age_s: float = DEFAULT_MAX_AGE_S

    def __post_init__(self) -> None:
        _require_sha256(self.bundle, "the approved bundle")
        _require_text(self.nonce, "the challenge nonce")
        measurements = frozenset(self.measurements)
        if not measurements:
            raise TeeError(
                "a TEE requirement with no permitted measurement admits any "
                "enclave, so it is not a requirement; name at least one "
                "measurement")
        for index, measurement in enumerate(sorted(measurements)):
            _require_measurement(measurement, f"permitted measurement {index}")
        object.__setattr__(self, "measurements", measurements)
        regions = frozenset(self.regions)
        if not regions:
            raise TeeError(
                "a TEE requirement with no permitted region admits an enclave "
                "anywhere, so it is not a requirement; name at least one region")
        for region in sorted(regions):
            _require_text(region, "a permitted region")
        object.__setattr__(self, "regions", regions)
        if self.outbound_network != OUTBOUND_FORBIDDEN:
            raise TeeError(
                f"a TEE requirement demands outbound_network = "
                f"{OUTBOUND_FORBIDDEN!r}; got {self.outbound_network!r}, which "
                f"asks for a worker whose network posture is not constrained, "
                f"so the placement would admit a proof it never checks")
        if not isinstance(self.max_age_s, (int, float)) or isinstance(self.max_age_s, bool) or self.max_age_s <= 0:
            raise TeeError(
                f"max_age_s must be a positive number of seconds, got "
                f"{self.max_age_s!r}; an unbounded age admits a proof of any "
                f"past run")


# --------------------------------------------------------------------------
# What the attester returns
# --------------------------------------------------------------------------


def _sign(body: Mapping, key: bytes, domain: bytes) -> str:
    """HMAC-SHA256 over ``domain`` ++ the canonical body bytes, hex. ``body`` is
    the record with any ``signature`` member removed; canonical serialization
    sorts keys, so member order does not affect the signature."""
    signed = {k: v for k, v in body.items() if k != SIGNATURE_FIELD}
    return hmac.new(bytes(key), domain + _canonical_bytes(signed),
                    hashlib.sha256).hexdigest()


def _public_members(record: Mapping) -> dict:
    return {k: v for k, v in record.items() if k != SIGNATURE_FIELD}


@dataclass(frozen=True)
class EnclaveEvidence:
    """The attester's answer to one challenge: what is running, where, and with
    what network posture.

    Built by the attester (the enclave, or the service that quotes it) and
    signed by it; the peer only carries it. ``region`` may be the empty string,
    which no requirement's permitted set contains, so an evidence that declines
    to place the enclave fails the region check rather than skipping it.
    """

    peer_id: str
    bundle: str
    measurement: str
    region: str
    nonce: str
    issued_at: str
    expires_at: str
    outbound_network: str = OUTBOUND_FORBIDDEN

    def __post_init__(self) -> None:
        _require_text(self.peer_id, "the attested peer")
        _require_sha256(self.bundle, "the attested bundle")
        _require_measurement(self.measurement, "the enclave measurement")
        if not isinstance(self.region, str):
            raise TeeError(f"the region must be a string, got {self.region!r}")
        _require_text(self.nonce, "the answered nonce")
        _require_window(self.issued_at, self.expires_at)
        if self.outbound_network not in _OUTBOUND_POSTURES:
            raise TeeError(
                f"outbound_network must be one of "
                f"{', '.join(repr(p) for p in _OUTBOUND_POSTURES)}, got "
                f"{self.outbound_network!r}")

    def body(self, sign_alg: str = SIGN_ALG) -> dict:
        """The signed body: every member except the signature (or the quote), in a
        shape whose canonical bytes are a pure function of the evidence's content
        and the algorithm that will cover it.

        ``sign_alg`` is part of the body rather than appended to it, so a record
        cannot be re-presented as though a different verifier had decided it: the
        bytes a hardware quote binds and the bytes a MAC covers differ in this
        member, and each refuses the other's."""
        if sign_alg not in EVIDENCE_SIGN_ALGS:
            raise TeeError(
                f"sign_alg must be one of "
                f"{', '.join(repr(a) for a in EVIDENCE_SIGN_ALGS)}, got "
                f"{sign_alg!r}")
        return {
            "kind": EVIDENCE_KIND,
            "version": EVIDENCE_VERSION,
            "peer_id": self.peer_id,
            "bundle": self.bundle,
            "measurement": self.measurement,
            "region": self.region,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "outbound_network": self.outbound_network,
            "sign_alg": sign_alg,
        }


def sign_evidence(evidence: EnclaveEvidence, attester_key: bytes) -> dict:
    """Build the signed evidence record: the body plus a ``key_id`` fingerprint
    and a signature over the whole body. Deterministic given
    ``(evidence, attester_key)``.

    The key is the ATTESTER's, not the peer's: see the module docstring on why
    :func:`tee_admits` refuses an evidence verified with the peer's own key."""
    if not isinstance(attester_key, (bytes, bytearray)) or not attester_key:
        raise TeeError("the attester key must be non-empty bytes")
    body = evidence.body()
    body["key_id"] = key_id(bytes(attester_key))
    body[SIGNATURE_FIELD] = _sign(body, attester_key, EVIDENCE_SIGN_DOMAIN)
    return body


def evidence_report_data(body: Mapping) -> bytes:
    """The 64 bytes a hardware quote must carry for this evidence body.

    A TDX quote and a SEV-SNP report each measure what is RUNNING; nothing in
    either format knows about a peer id, a bundle label, a region, a network
    posture or a placement's challenge. The one field the workload controls is
    the 64-byte ``report_data``, so that is where those claims are bound: this
    digest covers the evidence's whole canonical body, and the hardware signature
    covers the digest. Change any member and the quote no longer matches, which
    is what makes a replayed or re-purposed quote refuse.

    Raises :class:`TeeError` for a body with no canonical spelling, so a caller
    with an ``(ok, reason)`` contract refuses rather than crashing."""
    try:
        return hashlib.sha512(
            EVIDENCE_BIND_DOMAIN + _canonical_bytes(_public_quote_members(body))
        ).digest()
    except NotCanonicalizable as error:
        raise TeeError(f"the evidence body has no canonical spelling: {error}") from error


def _public_quote_members(record: Mapping) -> dict:
    """The members a quote's ``report_data`` digest covers: everything except the
    quote itself, its endorsement, and the MAC field. Those three are the
    envelope around the claim, not part of it."""
    skip = (SIGNATURE_FIELD, QUOTE_FIELD, ENDORSEMENT_FIELD)
    return {k: v for k, v in record.items() if k not in skip}


def quote_evidence(evidence: EnclaveEvidence, *, sign_alg: str, quote: bytes,
                   platform_key_id: str,
                   endorsement: Optional[Mapping] = None) -> dict:
    """Assemble a HARDWARE-rooted evidence record: the body, the fingerprint of
    the platform key that signed the quote, and the quote itself.

    There is no MAC. The quote is the signature, and it is only a signature over
    this evidence because the quote's ``report_data`` is
    :func:`evidence_report_data` of the body — which the caller building the
    quote is responsible for having done, and which :func:`verify_evidence`
    re-derives and compares rather than trusts.

    ``platform_key_id`` names WHICH pinned key the verifier should ask its root
    for. It is a hint, not an authority: the root decides whether it holds that
    key, and a fingerprint it does not hold is a refusal."""
    if sign_alg not in QUOTE_SIGN_ALGS:
        raise TeeError(
            f"a hardware-rooted evidence carries a quote format, one of "
            f"{', '.join(repr(a) for a in QUOTE_SIGN_ALGS)}; got {sign_alg!r}")
    if not isinstance(quote, (bytes, bytearray)) or not quote:
        raise TeeError("the quote must be non-empty bytes")
    if not isinstance(platform_key_id, str) or not _KEY_ID_RE.fullmatch(platform_key_id):
        raise TeeError(
            f"platform_key_id must be a key fingerprint (16 lowercase hex "
            f"digits), got {platform_key_id!r}")
    body = evidence.body(sign_alg)
    body["key_id"] = platform_key_id
    if endorsement is not None:
        if not isinstance(endorsement, Mapping):
            raise TeeError(
                f"the platform endorsement must be an object or absent, got "
                f"{type(endorsement).__name__}")
        body[ENDORSEMENT_FIELD] = dict(endorsement)
    body[QUOTE_FIELD] = bytes(quote).hex()
    return body


def attester_report_data(evidence: EnclaveEvidence, *, sign_alg: str,
                         platform_key_id: str) -> bytes:
    """The 64 bytes an attester must put in a quote's ``report_data`` for this
    evidence. The attester's half of :func:`evidence_report_data`: the verifier
    re-derives the same digest from the record it receives, so the two agree by
    construction rather than by convention."""
    body = evidence.body(sign_alg)
    body["key_id"] = platform_key_id
    return evidence_report_data(body)


def build_tdx_evidence(evidence: EnclaveEvidence, *, attest_private_key: int,
                       platform_private_key: int, platform_key_id: str,
                       endorsement: Optional[Mapping] = None,
                       **quote_fields) -> dict:
    """The reference TDX attester: quote this evidence and wrap it in a record.

    ``evidence.measurement`` IS the MRTD, so it must be a 48-byte hex digest and
    it goes into the quote rather than beside it: a record whose stated
    measurement is not the register the quote carries is refused by
    :func:`verify_evidence`, and building it any other way would produce exactly
    that refusal. Extra keyword arguments reach
    :func:`~revl.tee_quote.build_tdx_quote`, so a test can build a quote that is
    wrong in one way and right in every other."""
    mrtd = bytes.fromhex(evidence.measurement)
    if len(mrtd) != 48:
        raise TeeError(
            f"a TDX MRTD is 48 bytes (96 hex digits); this evidence's measurement "
            f"is {len(mrtd)} bytes, so it cannot be the register a TD quote "
            f"carries")
    report_data = attester_report_data(
        evidence, sign_alg=SIGN_ALG_TDX, platform_key_id=platform_key_id)
    quote = build_tdx_quote(report_data=report_data, mrtd=mrtd,
                            attest_private_key=attest_private_key,
                            platform_private_key=platform_private_key,
                            **quote_fields)
    return quote_evidence(evidence, sign_alg=SIGN_ALG_TDX, quote=quote,
                          platform_key_id=platform_key_id,
                          endorsement=endorsement)


def build_sev_snp_evidence(evidence: EnclaveEvidence, *, vcek_private_key: int,
                           platform_key_id: str,
                           endorsement: Optional[Mapping] = None,
                           **report_fields) -> dict:
    """The reference SEV-SNP attester. ``evidence.measurement`` is the guest's
    48-byte launch measurement, on the same terms as
    :func:`build_tdx_evidence`."""
    measurement = bytes.fromhex(evidence.measurement)
    if len(measurement) != 48:
        raise TeeError(
            f"a SEV-SNP launch measurement is 48 bytes (96 hex digits); this "
            f"evidence's measurement is {len(measurement)} bytes")
    report_data = attester_report_data(
        evidence, sign_alg=SIGN_ALG_SEV_SNP, platform_key_id=platform_key_id)
    report = build_sev_snp_report(report_data=report_data,
                                 measurement=measurement,
                                 vcek_private_key=vcek_private_key,
                                 **report_fields)
    return quote_evidence(evidence, sign_alg=SIGN_ALG_SEV_SNP, quote=report,
                          platform_key_id=platform_key_id,
                          endorsement=endorsement)


def _resolve_root(root: Any, attester_key: Any) -> tuple[Optional[AttestationRoot], str]:
    """The root a verification runs against, or a refusal reason.

    Exactly one of the two ways in. ``root`` is the attestation root proper.
    ``attester_key`` is the pre-root spelling and resolves to the development
    symmetric-MAC verifier, which is not an attestation root; it is accepted so
    that callers holding only a shared secret keep working, and every verdict it
    reaches is labelled."""
    if root is not None and attester_key not in (None, b""):
        return None, ("both an attestation root and an attester key were "
                      "supplied, and they can disagree; pass one")
    if root is not None:
        if not isinstance(root, AttestationRoot):
            return None, (f"an attestation root is a tee_quote.AttestationRoot, "
                          f"got {type(root).__name__}")
        return root, ""
    key = _key_bytes(attester_key)
    if not key:
        return None, ("no attestation root and no attester key provided, so there "
                      "is nothing to verify the evidence against; an unverifiable "
                      "proof is refused rather than admitted")
    return DevMacRoot(key, acknowledged_dev_only=True), ""


def _validate_evidence(record: Mapping, sign_alg: str) -> str:
    """Is this authentic record even an enclave evidence of the shape we accept?
    Returns a refusal reason, or ``""`` when well formed. A signature proves
    authorship, not that what was authored means what a reader assumes, so every
    fixed-meaning member is checked here (mirrors ``peer_offer._validate_envelope``)."""
    for member, expected in (("kind", EVIDENCE_KIND),
                             ("version", EVIDENCE_VERSION),
                             ("sign_alg", sign_alg)):
        if record.get(member) != expected:
            return (f"envelope refused: {member} is {record.get(member)!r}, "
                    f"expected {expected!r}")
    peer_id = record.get("peer_id")
    if not isinstance(peer_id, str) or not peer_id:
        return f"envelope refused: peer_id is not a non-empty string ({peer_id!r})"
    bundle = record.get("bundle")
    if not isinstance(bundle, str) or not _SHA256_RE.fullmatch(bundle):
        return (f"envelope refused: bundle is not a canonical content hash "
                f"({bundle!r})")
    measurement = record.get("measurement")
    if not isinstance(measurement, str) or not _MEASUREMENT_RE.fullmatch(measurement):
        return (f"envelope refused: measurement is not a hex digest of at least "
                f"16 bytes ({measurement!r})")
    if not isinstance(record.get("region"), str):
        return f"envelope refused: region is not a string ({record.get('region')!r})"
    nonce = record.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return f"envelope refused: nonce is not a non-empty string ({nonce!r})"
    posture = record.get("outbound_network")
    if posture not in _OUTBOUND_POSTURES:
        return (f"envelope refused: outbound_network is {posture!r}, expected one "
                f"of {', '.join(repr(p) for p in _OUTBOUND_POSTURES)}")
    if _instant(record.get("issued_at")) is None:
        return (f"envelope refused: issued_at is not an ISO 8601 instant "
                f"({record.get('issued_at')!r})")
    expires = _instant(record.get("expires_at"))
    if expires is None:
        return (f"envelope refused: expires_at is not an ISO 8601 instant "
                f"({record.get('expires_at')!r})")
    if expires <= _instant(record.get("issued_at")):
        return ("envelope refused: the validity window is empty or backwards, so "
                f"the evidence was never valid ({record.get('issued_at')!r} to "
                f"{record.get('expires_at')!r})")
    kid = record.get("key_id")
    if not isinstance(kid, str) or not _KEY_ID_RE.fullmatch(kid):
        return f"envelope refused: key_id is not a key fingerprint ({kid!r})"
    return ""


def verify_evidence(record: Mapping, attester_key: Optional[bytes] = None, *,
                    root: Optional[AttestationRoot] = None,
                    now: Optional[datetime] = None) -> tuple[bool, str]:
    """Check a signed evidence record against an attestation root. Returns
    ``(ok, reason)`` and refuses rather than raises on a malformed proof; the
    module docstring records the one inherited exception (a non-ASCII
    ``signature`` string, on the MAC path only).

    Two roots, and the record's own ``sign_alg`` chooses which one may decide it,
    so the two can never be crossed:

    * a :class:`~revl.tee_quote.HardwareRoot` decides a record whose ``sign_alg``
      is a quote format. The chain runs from a key the OPERATOR pinned down to
      the quote, and the quote's ``report_data`` must be
      :func:`evidence_report_data` of this record's own body, so the hardware
      signature covers every claim the record makes. The measurement the record
      states is additionally compared against the measurement register the
      hardware reported, because ``report_data`` binds what the workload SAID and
      the register is what the platform MEASURED;
    * a :class:`~revl.tee_quote.DevMacRoot` decides a record whose ``sign_alg`` is
      ``hmac-sha256``, and says so in the reason. It is the pre-root verifier,
      kept so callers holding only a shared secret still work.

    Order mirrors ``peer_offer.verify_offer``: prove authenticity FIRST, then
    validate the envelope. A record from another protocol signed with this key
    fails the MAC here (domain separation) rather than being read as enclave
    evidence. This function checks the record's OWN consistency; whether it
    satisfies a placement is :func:`tee_admits`."""
    if not isinstance(record, Mapping):
        return False, "enclave evidence is not an object"
    try:
        resolved, problem = _resolve_root(root, attester_key)
    except RootError as error:
        return False, f"the attestation root is unusable: {error}"
    if resolved is None:
        return False, problem
    declared = record.get("sign_alg")
    if isinstance(resolved, HardwareRoot):
        if declared not in QUOTE_SIGN_ALGS:
            return False, (
                f"a hardware attestation root decides a hardware quote, but this "
                f"evidence declares sign_alg {declared!r}; a symmetric-MAC record "
                f"is not evidence against a hostile peer and is refused here "
                f"rather than verified with the wrong primitive")
        return _verify_quote_evidence(record, resolved, declared, now)
    if declared not in (SIGN_ALG, None):
        return False, (
            f"this evidence declares sign_alg {declared!r}, which needs a hardware "
            f"attestation root; the development symmetric-MAC verifier cannot "
            f"decide a quote {DEV_ROOT_NOTE}")
    key = resolved.key  # type: ignore[union-attr]
    given = record.get(SIGNATURE_FIELD)
    if not isinstance(given, str):
        return False, "enclave evidence carries no attestation signature"
    try:
        expected = _sign(record, key, EVIDENCE_SIGN_DOMAIN)
    except NotCanonicalizable as error:
        return False, f"enclave evidence cannot be verified: {error}"
    if not hmac.compare_digest(expected, given):
        return False, ("attestation signature mismatch: wrong attester key, or the "
                       "evidence was tampered with after it was issued")
    envelope = _validate_evidence(record, SIGN_ALG)
    if envelope:
        return False, envelope
    return True, f"valid: enclave evidence is authentic and well formed {DEV_ROOT_NOTE}"


def _verify_quote_evidence(record: Mapping, root: HardwareRoot, sign_alg: str,
                           now: Optional[datetime]) -> tuple[bool, str]:
    """The hardware half of :func:`verify_evidence`.

    Every step refuses rather than raises. The order matters: the quote's own
    chain to the pinned root is proven before anything the record says is read as
    meaningful, and the measurement cross-check comes last because it is the one
    comparison that needs both halves."""
    raw = record.get(QUOTE_FIELD)
    if not isinstance(raw, str) or not raw:
        return False, (
            f"the evidence declares the hardware format {sign_alg!r} but carries "
            f"no {QUOTE_FIELD!r}, so there is nothing for the attestation root to "
            f"verify")
    try:
        blob = bytes.fromhex(raw)
    except ValueError:
        return False, f"the evidence's {QUOTE_FIELD!r} member is not hex"
    try:
        expect = evidence_report_data(record)
    except TeeError as error:
        return False, f"enclave evidence cannot be verified: {error}"
    endorsement = record.get(ENDORSEMENT_FIELD)
    if endorsement is not None and not isinstance(endorsement, Mapping):
        return False, (f"the evidence's {ENDORSEMENT_FIELD!r} member is not an "
                       f"object")
    try:
        verdict = verify_quote(
            blob, sign_alg=sign_alg, root=root, expect_report_data=expect,
            platform_key_id=record.get("key_id"), endorsement=endorsement, now=now)
    except QuoteFormatError as error:
        return False, f"the attestation root refused the quote: {error}"
    if not verdict.ok:
        return False, f"the attestation root refused the quote: {verdict.reason}"
    envelope = _validate_evidence(record, sign_alg)
    if envelope:
        return False, envelope
    if record["measurement"] != verdict.measurement:
        return False, (
            f"the evidence states measurement {record['measurement']}, but the "
            f"quote's own measurement register reads {verdict.measurement}: the "
            f"enclave that signed this quote is not the enclave the evidence "
            f"describes")
    return True, (f"valid: {verdict.reason}, and the evidence's claims are the "
                  f"claims that quote binds")


def tee_admits(record: Mapping, requirement: TeeRequirement, *,
               peer_id: str, peer_key: bytes,
               attester_key: Optional[bytes] = None,
               root: Optional[AttestationRoot] = None,
               require_hardware_root: bool = False,
               now: Optional[datetime] = None,
               replay_ledger: Optional[MutableSet[tuple[str, str]]] = None
               ) -> tuple[bool, str]:
    """Does this evidence satisfy ``requirement`` for ``peer_id``? Returns
    ``(admitted, reason)`` and refuses rather than raises on a malformed proof;
    the module docstring records the one inherited exception (a non-ASCII
    ``signature`` string).

    The gates, in order:

    1. **key separation** — an evidence verified with the peer's own offer key,
       or naming that key as the platform that signed its quote, proves nothing
       (the peer could have written it), so it is refused before anything else is
       even read;
    2. **authenticity** — the attestation root, then the envelope. Pass ``root``
       for a hardware-rooted verdict (a real TDX or SEV-SNP quote, chained to a
       key the operator pinned) or ``attester_key`` for the development
       symmetric-MAC verifier, which is not an attestation root and labels every
       verdict it reaches. Passing neither refuses;
    3. **the peer** the evidence names, which must be the peer being placed;
    4. **the bundle** the enclave runs, which must be the approved one;
    5. **the measurement**, which must be in the permitted set;
    6. **the region**, which must be in the permitted set;
    7. **the network posture**, which must equal the requirement's;
    8. **the challenge**, which the evidence must answer;
    9. **freshness** — not issued beyond the clock skew, not expired, and not
       older than ``max_age_s``;
    10. **replay** — the challenge must not have been consumed for this peer.

    ``replay_ledger`` is the caller's ledger of ``(nonce, peer_id)`` pairs. It is
    required (a proof with no ledger cannot be checked for replay, so it is
    refused) and it is MUTATED on success only: a refused proof does not burn the
    challenge, so an honest peer can still answer it.

    ``require_hardware_root=True`` refuses before gate 1 unless ``root`` is a
    production attestation root, so a caller that must not be satisfied by the
    development symmetric-MAC verifier says so once instead of inspecting the
    verdict's reason text afterwards."""
    if not isinstance(record, Mapping):
        return False, "enclave evidence is not an object"
    if require_hardware_root:
        try:
            require_production_root(root)
        except RootError as error:
            return False, f"no production attestation root: {error}"
    peer = _key_bytes(peer_key)
    if not peer:
        return False, (
            "no peer key provided, so the key separation cannot be checked: "
            "without the peer's own offer key a proof the peer signed itself "
            "would be accepted, and an unchecked separation is not a separation")
    if attester_key is not None and _key_bytes(attester_key) == peer:
        return False, (
            "the evidence is verified with the peer's own offer key, so it proves "
            "nothing: a peer that signs its own attestation can attest any bundle "
            "it never ran. Verify the evidence against the attestation authority's "
            "key, which the peer does not hold")
    # The same separation on the hardware path, and scoped to it. The peer cannot
    # hold the private half of a pinned root key by construction, but it CAN name
    # its own offer key as the platform that signed: the fingerprint construction
    # is shared (`attest.key_id`), so the two are comparable. A root that does not
    # hold that fingerprint refuses anyway; this gate is what refuses a root
    # MISCONFIGURED to pin it, and it gives the sharper reason either way. On the
    # MAC path the separation is `attester_key != peer_key` above, and the
    # signature covers `key_id`, so a swapped fingerprint is caught there instead.
    if (record.get("sign_alg") in QUOTE_SIGN_ALGS
            and record.get("key_id") == key_id(peer)):
        return False, (
            "the evidence names the peer's own offer key as the platform that "
            "signed its quote, so the chain terminates inside the peer: an "
            "attestation root is a key the peer does not hold")
    ok, reason = verify_evidence(record, attester_key, root=root, now=now)
    if not ok:
        return False, reason

    if record["peer_id"] != peer_id:
        return False, (f"the evidence attests peer {record['peer_id']!r}, not "
                       f"{peer_id!r}: a proof is bound to the peer it was issued for")
    if record["bundle"] != requirement.bundle:
        return False, (f"the enclave runs bundle {record['bundle']}, not the approved "
                       f"bundle {requirement.bundle}")
    if record["measurement"] not in requirement.measurements:
        return False, (f"the enclave measurement {record['measurement']} is not in "
                       f"the requirement's permitted set "
                       f"{sorted(requirement.measurements)}")
    if record["region"] not in requirement.regions:
        return False, (f"the enclave sits in region {record['region']!r}, which is not "
                       f"in the requirement's permitted set "
                       f"{sorted(requirement.regions)}")
    if record["outbound_network"] != requirement.outbound_network:
        return False, (f"the evidence reports outbound_network = "
                       f"{record['outbound_network']!r}, but the requirement demands "
                       f"{requirement.outbound_network!r}")
    if record["nonce"] != requirement.nonce:
        return False, (f"the evidence answers challenge {record['nonce']!r}, not this "
                       f"placement's challenge {requirement.nonce!r}")

    moment = now if now is not None else _now_utc()
    issued = _instant(record["issued_at"])
    expires = _instant(record["expires_at"])
    if issued > moment + timedelta(seconds=CLOCK_SKEW_S):
        return False, (f"the evidence was issued at {record['issued_at']}, ahead of "
                       f"now, so it is not a proof of a run that has happened "
                       f"(a self-issued future stamp is a forgery, not a clock skew)")
    if moment > expires:
        return False, (f"the evidence expired at {record['expires_at']}; an expired "
                       f"proof is refused rather than accepted on the strength of an "
                       f"earlier run")
    age = (moment - issued).total_seconds()
    if age > requirement.max_age_s:
        return False, (f"the evidence is {age:.0f}s old, older than the requirement's "
                       f"max_age_s={requirement.max_age_s:g}; a stale proof describes "
                       f"the past, not the worker being placed now")

    if replay_ledger is None:
        return False, ("the placement requires attestation but no replay ledger was "
                       "supplied, so the challenge could be answered twice and the "
                       "proof is refused")
    consumed = (requirement.nonce, peer_id)
    if consumed in replay_ledger:
        return False, (f"replay: challenge {requirement.nonce!r} was already consumed "
                       f"for peer {peer_id!r}, so this is a captured proof presented "
                       f"a second time")
    replay_ledger.add(consumed)
    verdict = ("admitted: the evidence proves the approved bundle in a permitted "
               "enclave and region, with outbound network forbidden, fresh and "
               "unconsumed")
    if record.get("sign_alg") not in QUOTE_SIGN_ALGS:
        # The whole point of labelling the admission and not only the refusals: an
        # operator reading this line has to be able to see that nothing hardware
        # rooted was checked.
        return True, f"{verdict} {DEV_ROOT_NOTE}"
    return True, verdict


# --------------------------------------------------------------------------
# The other half: what comes back from an attested worker
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultReceipt:
    """The receipt that travels with a result from an attested worker.

    It names the peer, the approved bundle, the challenge the run answered and
    the content hash of the result, so the result is bound to the attested run
    rather than to the transport that delivered it. The composition recomputes
    the hash over the bytes it actually received (:func:`receipt_admits`), so a
    receipt for some other result cannot be laundered onto this one.
    """

    peer_id: str
    bundle: str
    nonce: str
    result_hash: str
    issued_at: str

    def __post_init__(self) -> None:
        _require_text(self.peer_id, "the signing peer")
        _require_sha256(self.bundle, "the bundle the run was attested for")
        _require_text(self.nonce, "the answered nonce")
        _require_sha256(self.result_hash, "the result")
        if _instant(self.issued_at) is None:
            raise TeeError(
                f"issued_at must be an ISO 8601 instant, got {self.issued_at!r}")

    def body(self) -> dict:
        return {
            "kind": RECEIPT_KIND,
            "version": RECEIPT_VERSION,
            "peer_id": self.peer_id,
            "bundle": self.bundle,
            "nonce": self.nonce,
            "result_hash": self.result_hash,
            "issued_at": self.issued_at,
            "sign_alg": SIGN_ALG,
        }


def sign_result_receipt(receipt: ResultReceipt, enclave_key: bytes) -> dict:
    """Sign a result receipt with the ENCLAVE's key: the composition may then
    insist the result came from the attested environment and not from the peer
    process around it."""
    if not isinstance(enclave_key, (bytes, bytearray)) or not enclave_key:
        raise TeeError("the receiving key must be non-empty bytes")
    body = receipt.body()
    body["key_id"] = key_id(bytes(enclave_key))
    body[SIGNATURE_FIELD] = _sign(body, enclave_key, RECEIPT_SIGN_DOMAIN)
    return body


def _validate_receipt(record: Mapping) -> str:
    for member, expected in (("kind", RECEIPT_KIND),
                             ("version", RECEIPT_VERSION),
                             ("sign_alg", SIGN_ALG)):
        if record.get(member) != expected:
            return (f"envelope refused: {member} is {record.get(member)!r}, "
                    f"expected {expected!r}")
    peer_id = record.get("peer_id")
    if not isinstance(peer_id, str) or not peer_id:
        return f"envelope refused: peer_id is not a non-empty string ({peer_id!r})"
    for member in ("bundle", "result_hash"):
        value = record.get(member)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            return (f"envelope refused: {member} is not a canonical content hash "
                    f"({value!r})")
    nonce = record.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return f"envelope refused: nonce is not a non-empty string ({nonce!r})"
    if _instant(record.get("issued_at")) is None:
        return (f"envelope refused: issued_at is not an ISO 8601 instant "
                f"({record.get('issued_at')!r})")
    kid = record.get("key_id")
    if not isinstance(kid, str) or not _KEY_ID_RE.fullmatch(kid):
        return f"envelope refused: key_id is not a key fingerprint ({kid!r})"
    return ""


def verify_result_receipt(record: Mapping, enclave_key: bytes) -> tuple[bool, str]:
    """Check a signed result receipt with the enclave's key. ``(ok, reason)``,
    refusing rather than raising on a malformed receipt (the module docstring
    records the one inherited exception, a non-ASCII ``signature`` string).
    Whether it covers the result that actually arrived, and
    whether it belongs to the attested run, is :func:`receipt_admits`."""
    if not isinstance(enclave_key, (bytes, bytearray)) or not enclave_key:
        return False, "no receiving key provided"
    if not isinstance(record, Mapping):
        return False, "result receipt is not an object"
    given = record.get(SIGNATURE_FIELD)
    if not isinstance(given, str):
        return False, "result receipt carries no signature"
    try:
        expected = _sign(record, enclave_key, RECEIPT_SIGN_DOMAIN)
    except NotCanonicalizable as error:
        return False, f"result receipt cannot be verified: {error}"
    if not hmac.compare_digest(expected, given):
        return False, ("signature mismatch: wrong receiving key, or the receipt was "
                       "tampered with after it was signed")
    envelope = _validate_receipt(record)
    if envelope:
        return False, envelope
    return True, "valid: result receipt is authentic and well formed"


def receipt_admits(record: Mapping, *, enclave_key: bytes, peer_key: bytes,
                   peer_id: str, bundle: str, nonce: str, result: Any,
                   now: Optional[datetime] = None,
                   max_age_s: float = DEFAULT_MAX_AGE_S,
                   replay_ledger: Optional[MutableSet[tuple[str, str]]] = None
                   ) -> tuple[bool, str]:
    """Accept a result only when its receipt proves it came from the attested
    run. Returns ``(admitted, reason)`` and refuses rather than raises on a
    malformed receipt (the module docstring records the one inherited exception,
    a non-ASCII ``signature`` string).

    Gates, in order: the key separation (a receipt the peer can sign itself
    proves nothing about the enclave), the signature and envelope, the peer, the
    approved bundle, the challenge (which binds the result to the attested run,
    not to a different one), the content hash of the bytes actually received,
    freshness, and the delivery ledger (the same receipt cannot deliver the same
    run's result twice).

    ``replay_ledger`` is a DELIVERY ledger, distinct from the admission ledger
    :func:`tee_admits` consumes; the two record different events."""
    if not isinstance(record, Mapping):
        return False, "result receipt is not an object"
    peer = _key_bytes(peer_key)
    if not peer:
        return False, (
            "no peer key provided, so the key separation cannot be checked: "
            "without the peer's own offer key a receipt the peer signed itself "
            "would be accepted, and an unchecked separation is not a separation")
    if _key_bytes(enclave_key) == peer:
        return False, (
            "the receipt is verified with the peer's own offer key, so it proves "
            "nothing about the enclave: a peer can sign a result it fabricated "
            "outside the attested environment. Verify the receipt against the "
            "enclave's key, which the peer does not hold")
    ok, reason = verify_result_receipt(record, enclave_key)
    if not ok:
        return False, reason

    if record["peer_id"] != peer_id:
        return False, (f"the receipt was issued by peer {record['peer_id']!r}, not "
                       f"{peer_id!r}")
    if record["bundle"] != bundle:
        return False, (f"the receipt covers bundle {record['bundle']}, not the "
                       f"approved bundle {bundle}")
    if record["nonce"] != nonce:
        return False, (f"the receipt answers challenge {record['nonce']!r}, not the "
                       f"attested run's challenge {nonce!r}, so the result is not "
                       f"bound to the run that was admitted")
    try:
        received = result_identity(result)
    except TeeError as error:
        return False, f"the received result cannot be identified: {error}"
    if record["result_hash"] != received:
        return False, (f"the receipt covers result {record['result_hash']}, not the "
                       f"result that arrived ({received})")

    moment = now if now is not None else _now_utc()
    issued = _instant(record["issued_at"])
    if issued > moment + timedelta(seconds=CLOCK_SKEW_S):
        return False, (f"the receipt was issued at {record['issued_at']}, ahead of "
                       f"now, so it cannot cover a result already received")
    age = (moment - issued).total_seconds()
    if age > max_age_s:
        return False, (f"the receipt is {age:.0f}s old, older than the accepted "
                       f"max_age_s={max_age_s:g}")

    if replay_ledger is None:
        return False, ("no delivery ledger was supplied, so the same receipt could "
                       "deliver one run's result twice; the result is refused")
    delivered = (nonce, peer_id)
    if delivered in replay_ledger:
        return False, (f"replay: the receipt for challenge {nonce!r} from peer "
                       f"{peer_id!r} has already delivered a result")
    replay_ledger.add(delivered)
    return True, ("admitted: the result is signed by the attested enclave, bound to "
                  "the admitted run, and fresh")

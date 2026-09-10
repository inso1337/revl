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

The property that makes the verification non-vacuous
----------------------------------------------------
Symmetric crypto has one sharp edge here: a MAC proves only that the holder of a
key authored the record. If the PEER holds the attester key, the peer can mint
its own "proof" that it runs a bundle it has never run, and the requirement
degenerates into an assertion. So a requirement is enforced together with a key
separation: :func:`tee_admits` refuses when the attester key is the peer's own
offer key, and :func:`receipt_admits` refuses when the receipt key is. In a real
deployment the attester key belongs to whoever quotes the enclave (the hardware
root, or the attestation service mediating it), and the requirement is
meaningful exactly to the extent that the peer does not hold that key. The
limitation is stated here rather than papered over: an asymmetric, hardware-
rooted quote, checkable with a public key, is the follow-up, and the ``sign_alg``
member exists so that migration is additive.

Fail-closed, everywhere
-----------------------
Every ambiguous case refuses: an absent proof, a proof for another peer, another
bundle, another measurement, another region or another challenge; an expired or
stale proof; a proof or receipt presented twice for the same challenge; a network
posture other than the one demanded; an unparseable member; a receipt whose
signature, whose result bytes or whose run do not match. Every verifier returns
``(ok, reason)`` and never raises, so a hostile record cannot break a caller that
iterates candidate peers.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, MutableSet, Optional

from .attest import NotCanonicalizable, _canonical_bytes, canonical_hash, key_id

# The two envelope identities. Same discipline as `attest`/`peer_offer`: a
# self-identifying kind plus a MAJOR.MINOR line, additive within a MAJOR.
EVIDENCE_KIND = "revl.tee-evidence"
EVIDENCE_VERSION = "1.0"
RECEIPT_KIND = "revl.tee-receipt"
RECEIPT_VERSION = "1.0"

# One signature algorithm today; the member is VALIDATED, not merely recorded,
# so an asymmetric upgrade is an additive change.
SIGN_ALG = "hmac-sha256"

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
    refusal, which is reported as ``(ok, reason)`` and never raises, so a
    hostile peer-supplied record cannot break that contract."""


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

    def body(self) -> dict:
        """The signed body: every member except the signature, in a shape whose
        canonical bytes are a pure function of the evidence's content."""
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
            "sign_alg": SIGN_ALG,
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


def _validate_evidence(record: Mapping) -> str:
    """Is this authentic record even an enclave evidence of the shape we accept?
    Returns a refusal reason, or ``""`` when well formed. A MAC proves
    authorship, not that what was authored means what a reader assumes, so every
    fixed-meaning member is checked here (mirrors ``peer_offer._validate_envelope``)."""
    for member, expected in (("kind", EVIDENCE_KIND),
                             ("version", EVIDENCE_VERSION),
                             ("sign_alg", SIGN_ALG)):
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


def verify_evidence(record: Mapping, attester_key: bytes) -> tuple[bool, str]:
    """Check a signed evidence record with the attester's key. Returns
    ``(ok, reason)`` and NEVER raises.

    Order mirrors ``peer_offer.verify_offer``: prove authenticity (the MAC)
    FIRST, then validate the envelope. A record from another protocol signed with
    this key fails the MAC here (domain separation) rather than being read as
    enclave evidence. This function checks the record's OWN consistency; whether
    it satisfies a placement is :func:`tee_admits`."""
    if not isinstance(attester_key, (bytes, bytearray)) or not attester_key:
        return False, "no attester key provided"
    if not isinstance(record, Mapping):
        return False, "enclave evidence is not an object"
    given = record.get(SIGNATURE_FIELD)
    if not isinstance(given, str):
        return False, "enclave evidence carries no attestation signature"
    try:
        expected = _sign(record, attester_key, EVIDENCE_SIGN_DOMAIN)
    except NotCanonicalizable as error:
        return False, f"enclave evidence cannot be verified: {error}"
    if not hmac.compare_digest(expected, given):
        return False, ("attestation signature mismatch: wrong attester key, or the "
                       "evidence was tampered with after it was issued")
    envelope = _validate_evidence(record)
    if envelope:
        return False, envelope
    return True, "valid: enclave evidence is authentic and well formed"


def tee_admits(record: Mapping, requirement: TeeRequirement, *,
               peer_id: str, peer_key: bytes, attester_key: bytes,
               now: Optional[datetime] = None,
               replay_ledger: Optional[MutableSet[tuple[str, str]]] = None
               ) -> tuple[bool, str]:
    """Does this evidence satisfy ``requirement`` for ``peer_id``? Returns
    ``(admitted, reason)`` and never raises.

    The gates, in order:

    1. **key separation** — an evidence verified with the peer's own offer key
       proves nothing (the peer could have written it), so it is refused before
       anything else is even read;
    2. **authenticity** — the MAC under the attester key, then the envelope;
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
    challenge, so an honest peer can still answer it."""
    if not isinstance(record, Mapping):
        return False, "enclave evidence is not an object"
    if bytes(attester_key or b"") == bytes(peer_key or b""):
        return False, (
            "the evidence is verified with the peer's own offer key, so it proves "
            "nothing: a peer that signs its own attestation can attest any bundle "
            "it never ran. Verify the evidence against the attestation authority's "
            "key, which the peer does not hold")
    ok, reason = verify_evidence(record, attester_key)
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
    return True, ("admitted: the evidence proves the approved bundle in a permitted "
                  "enclave and region, with outbound network forbidden, fresh and "
                  "unconsumed")


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
    never raises. Whether it covers the result that actually arrived, and
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
    run. Returns ``(admitted, reason)`` and never raises.

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
    if bytes(enclave_key or b"") == bytes(peer_key or b""):
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

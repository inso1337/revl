"""Signed execution receipts for the private peer pool (item 524, issue #1198).

What this closes
================

Before this module ``peer_pool.Membership.evidence`` was an integer a CALLER
handed the promotion gate, and ``promote`` took the key fingerprints that were
said to have signed that evidence as another caller-supplied list. The gate
checked that the number was large enough and that the names were in the
charter's ``attest_key_ids``. It never checked that any receipt existed.

A peer that states its own evidence count is a peer that can state any evidence
count, so that was a fail-open on the one arrow in the pool that RAISES
authority. This module makes the count arithmetic over signed bytes:
``peer_pool.promote`` recounts it here from receipts, and there is no longer a
parameter through which a number can be asserted.

The shape of a receipt
======================

Two signatures, because two different parties are saying two different things
and collapsing them would lose the distinction that matters.

1. The **execution receipt** is signed by the PEER, over what it ran and what
   came out: the pool, itself, the task, the artifact digest it was admitted
   with, and the digest of the result. Under asymmetric identity (issue #1278)
   this is attributable to the peer by anyone holding its public key, so it is
   the peer's own non-repudiable statement "I ran this and produced that".

2. The **attestation** is signed by an ATTESTING AUTHORITY named in the
   charter, over the DIGEST of the peer's receipt. The attestor is saying "I
   checked that result and it is admissible". A peer cannot sign this for
   itself; that is :data:`LINK_SELF_ATTESTATION` and it is refused even if an
   operator has mistakenly pinned the peer's own key as attesting authority.

The attestor signs the receipt's digest rather than a copy of its body, so the
binding is by hash: an attestation cannot be moved onto a different receipt,
and there is no second copy of the claim that could disagree with the first.

Why every verdict here is an :class:`~revl.peer_identity.Attribution`
=====================================================================

Issue #1278's shape, kept rather than flattened. A receipt check answers two
separate questions and this module never reduces them to one boolean:
``verified`` is arithmetic over bytes and never changes once true, ``status``
is a live reading of the signing key's authority, and ``confers_authority`` is
the conjunction. So a receipt signed under a key that was later revoked has
``verified=True, status="revoked"``: a true statement about work that really
happened, conferring nothing now. It does not count toward a promotion and it
is not treated as a forgery either, because an operator auditing the ledger
needs to tell those two apart.

Counting requires ``confers_authority`` on BOTH signatures, which is the
conservative direction throughout: a receipt whose key has been rotated away
from stops counting, and the only thing that can do is leave a peer at a lower
tier than it might have reached. It can never raise one.

A consequence worth stating: a receipt is an asymmetric record, so a member
that joined a pool under a shared key (``identity_mode`` ``shared-key`` or the
``mixed`` default) can produce no countable receipt at all and therefore cannot
be promoted above the entry tier. That is deliberate. An HMAC authenticates
under a secret the pool also holds, so it cannot prove to the pool that the
PEER produced a result rather than the pool itself, and evidence that the
verifier could have manufactured is not evidence.

Replay
======

Two receipts for the same ``task_id`` count ONCE. Deduplication is on the task,
not on the receipt digest, because a peer can re-sign the same work with a new
timestamp and get a fresh digest for free; only the task identity is stable.
A resubmitted receipt is refused on :data:`LINK_REPLAYED_RECEIPT` and recorded
rather than dropped, so a delivered-twice attempt is visible in the ledger
instead of being silently absorbed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import peer_identity
from .attest import NotCanonicalizable, _canonical_bytes
from .peer_identity import (
    Attribution,
    IdentityDirectory,
    KEY_UNKNOWN,
    PeerIdentity,
)

# ---------------------------------------------------------------------------
# kinds and domains
# ---------------------------------------------------------------------------

RECEIPT_KIND = "revl.pool-execution-receipt"
RECEIPT_VERSION = "1.0"

ATTESTATION_KIND = "revl.pool-receipt-attestation"
ATTESTATION_VERSION = "1.0"

#: Distinct from ``attest``'s, ``peer_offer``'s, the deploy receipt's and each
#: other's. Without separate domains one protocol's signature would verify as
#: another's, and an attestation over a digest would be replayable as an
#: execution receipt over a body that happened to canonicalize the same way.
RECEIPT_DOMAIN = b"revl.pool-execution-receipt/v1\x00"
ATTESTATION_DOMAIN = b"revl.pool-receipt-attestation/v1\x00"

#: The verdict an attestation carries. Named rather than free text so an
#: attestation that says nothing cannot be read as saying yes.
ADMITTED = "admitted"

# ---------------------------------------------------------------------------
# refusal links
# ---------------------------------------------------------------------------
#
# Lowercase links, following `peer_pool` and `revl.deploy`. Nothing here enters
# `revl.diagnostics.GUARANTEES`: a receipt refusal is a decision about a
# deployment, not a verdict about a program, and there is no program to write
# under `examples/rejections/` for "this receipt was already counted".

#: The record is not a receipt at all: wrong kind, missing member, unparseable.
LINK_RECEIPT_SHAPE = "receipt-shape"
#: The peer's signature does not verify, or its key is not pinned.
LINK_RECEIPT_SIGNATURE = "receipt-signature"
#: The peer's key verified but confers no authority now (revoked, superseded).
LINK_RECEIPT_KEY_AUTHORITY = "receipt-key-authority"
#: The receipt names another pool.
LINK_RECEIPT_POOL = "receipt-pool"
#: The receipt names another peer.
LINK_RECEIPT_PEER = "receipt-peer"
#: The receipt names an artifact that is not the one this member was admitted
#: with. The candidate a peer runs must be the candidate that was admitted.
LINK_RECEIPT_ARTIFACT = "receipt-artifact"
#: The receipt predates this member's admission. Evidence a previous
#: incarnation earned does not carry across a withdrawal.
LINK_STALE_RECEIPT = "stale-receipt"
#: The attestation's signature does not verify, or its key is not pinned.
LINK_ATTESTATION_SIGNATURE = "attestation-signature"
#: The attesting key is not one the charter names as attesting authority, or it
#: verified and confers no authority now.
LINK_ATTESTATION_AUTHORITY = "attestation-authority"
#: The attestation was signed by the peer whose work it attests.
LINK_SELF_ATTESTATION = "self-attestation"
#: The attestation's ``receipt_digest`` is not the digest of the receipt it was
#: presented with, so it attests something else.
LINK_RECEIPT_BINDING = "receipt-binding"
#: A second receipt for a task already counted.
LINK_REPLAYED_RECEIPT = "replayed-receipt"

LINKS: tuple[str, ...] = (
    LINK_RECEIPT_SHAPE,
    LINK_RECEIPT_SIGNATURE,
    LINK_RECEIPT_KEY_AUTHORITY,
    LINK_RECEIPT_POOL,
    LINK_RECEIPT_PEER,
    LINK_RECEIPT_ARTIFACT,
    LINK_STALE_RECEIPT,
    LINK_ATTESTATION_SIGNATURE,
    LINK_ATTESTATION_AUTHORITY,
    LINK_SELF_ATTESTATION,
    LINK_RECEIPT_BINDING,
    LINK_REPLAYED_RECEIPT,
)


class ReceiptError(ValueError):
    """Raised only by the ISSUING side, where the caller holds the key and a
    mistake is a programming error. The CHECKING side never raises: a hostile
    record must not be able to choose between a verdict and a traceback."""


# ---------------------------------------------------------------------------
# time and digests
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def result_digest(result: Any) -> str:
    """The hash a receipt pins a result by.

    ``bytes`` are hashed as they are; anything else is canonicalized first, so
    a structured result has one spelling and two orderings of the same mapping
    do not produce two digests."""
    if isinstance(result, (bytes, bytearray)):
        return hashlib.sha256(bytes(result)).hexdigest()
    return hashlib.sha256(_canonical_bytes(result)).hexdigest()


def receipt_digest(record: Mapping[str, Any]) -> str:
    """The digest an attestation binds to.

    Over the WHOLE signed record, signature included, so two receipts that
    differ only in their signature are two different digests and an attestation
    cannot be slid from one onto the other."""
    return hashlib.sha256(_canonical_bytes(dict(record))).hexdigest()


def verify_result(record: Mapping[str, Any], result: Any) -> tuple[bool, str]:
    """Does ``result`` hash to what this receipt claims?

    The hash half of "verified by hash and receipt". Separate from the
    signature check because they fail for different reasons: a signature
    failure says the receipt is not the peer's, and this says the bytes in hand
    are not the bytes the receipt is about."""
    claimed = record.get("result_digest") if isinstance(record, Mapping) else None
    if not isinstance(claimed, str) or not claimed:
        return False, "the receipt pins no result_digest"
    try:
        actual = result_digest(result)
    except (NotCanonicalizable, TypeError, ValueError) as error:
        return False, f"the result cannot be digested: {error}"
    if actual != claimed:
        return False, (f"the result hashes to {actual[:16]}, the receipt pins "
                       f"{claimed[:16]}")
    return True, "the result is the one the receipt pins"


# ---------------------------------------------------------------------------
# issuing
# ---------------------------------------------------------------------------


def issue_receipt(*, pool_id: str, task_id: str, artifact_digest: str,
                  result: Any, identity: PeerIdentity,
                  at: Optional[datetime] = None) -> dict:
    """The peer signs what it ran and what came out.

    ``identity.peer_id`` is the peer named in the receipt; it is not a separate
    parameter, so a receipt cannot be issued in another peer's name by a caller
    that holds only its own key."""
    if not isinstance(identity, PeerIdentity):
        raise ReceiptError("issuing a receipt needs the peer's own PeerIdentity")
    for name, value in (("pool_id", pool_id), ("task_id", task_id),
                        ("artifact_digest", artifact_digest)):
        if not isinstance(value, str) or not value:
            raise ReceiptError(f"a receipt needs a non-empty {name}")
    body = {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "pool_id": pool_id,
        "peer_id": identity.peer_id,
        "task_id": task_id,
        "artifact_digest": artifact_digest,
        "result_digest": result_digest(result),
        "issued_at": _iso(at or _utc_now()),
    }
    return peer_identity.sign_record(RECEIPT_DOMAIN, body, identity)


def attest_receipt(record: Mapping[str, Any], *, identity: PeerIdentity,
                   verdict: str = ADMITTED,
                   at: Optional[datetime] = None) -> dict:
    """An attesting authority signs the digest of a peer's receipt.

    It signs the digest and not the body: there is then exactly one copy of
    what is being attested, so an attestation and the receipt it covers cannot
    drift apart."""
    if not isinstance(identity, PeerIdentity):
        raise ReceiptError("attesting needs the attestor's own PeerIdentity")
    if not isinstance(record, Mapping):
        raise ReceiptError("attesting needs a receipt record")
    body = {
        "kind": ATTESTATION_KIND,
        "version": ATTESTATION_VERSION,
        "pool_id": record.get("pool_id", ""),
        "peer_id": record.get("peer_id", ""),
        "task_id": record.get("task_id", ""),
        "receipt_digest": receipt_digest(record),
        "verdict": verdict,
        "attestor": identity.peer_id,
        "attested_at": _iso(at or _utc_now()),
    }
    return peer_identity.sign_record(ATTESTATION_DOMAIN, body, identity)


# ---------------------------------------------------------------------------
# checking one receipt
# ---------------------------------------------------------------------------


def _unattributed(reason: str, peer_id: str = "") -> Attribution:
    """The attribution of a record nobody can be named for. ``verified`` is
    False because no arithmetic succeeded, not because a forgery was proved."""
    return Attribution(peer_id=peer_id, key_id="", verified=False,
                       status=KEY_UNKNOWN, reason=reason)


@dataclass(frozen=True)
class ReceiptCheck:
    """One receipt, checked. Never a bare boolean.

    ``peer`` and ``attestor`` are the two :class:`Attribution` values, both
    always present, so "the peer's signature is real but its key is revoked"
    and "the peer's signature is a forgery" remain different findings. ``link``
    is empty exactly when the receipt counts."""

    task_id: str
    digest: str
    peer: Attribution
    attestor: Attribution
    link: str = ""
    reason: str = ""

    @property
    def counts(self) -> bool:
        """The conjunction spelled out. Both signatures must verify AND both
        keys must confer authority now AND every binding check must have
        passed, which is what an empty ``link`` means."""
        return (not self.link
                and self.peer.confers_authority
                and self.attestor.confers_authority)

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "digest": self.digest,
                "peer": self.peer.as_dict(),
                "attestor": self.attestor.as_dict(),
                "link": self.link, "reason": self.reason, "counts": self.counts}


def _refused(link: str, reason: str, *, task_id: str = "", digest: str = "",
             peer: Optional[Attribution] = None,
             attestor: Optional[Attribution] = None) -> ReceiptCheck:
    return ReceiptCheck(
        task_id=task_id, digest=digest,
        peer=peer if peer is not None else _unattributed(reason),
        attestor=attestor if attestor is not None else _unattributed(reason),
        link=link, reason=reason)


def _shape_ok(record: Any, kind: str) -> str:
    """The reason ``record`` is not a ``kind`` record, or an empty string.

    Every member a later check reads is required to be a string HERE, so the
    refusal path never indexes into something hostile and never raises."""
    if not isinstance(record, Mapping):
        return f"expected a {kind} record, got {type(record).__name__}"
    if record.get("kind") != kind:
        return f"expected kind {kind!r}, got {record.get('kind')!r}"
    required = ("pool_id", "peer_id", "task_id")
    if kind == RECEIPT_KIND:
        required += ("artifact_digest", "result_digest", "issued_at")
    else:
        required += ("receipt_digest", "verdict", "attestor")
    for member in required:
        if not isinstance(record.get(member), str) or not record.get(member):
            return f"its {member} is missing or is not a string"
    return ""


def check_receipt(record: Any, attestation: Any, *, pool_id: str,
                  peer_id: str, artifact_digest: str, admitted_at: str,
                  attest_key_ids: Iterable[str],
                  directory: IdentityDirectory,
                  when: Optional[datetime] = None) -> ReceiptCheck:
    """Check one receipt and its attestation against one member's admission.

    Never raises. A hostile record gets a verdict, like
    ``peer_offer.verify_offer`` and ``peer_pool``'s gate, because a record that
    can choose a traceback over a refusal has chosen its own outcome.

    The binding checks are all of the form "is this receipt about the thing the
    pool admitted": the same pool, the same peer, the same artifact digest, and
    issued no earlier than the admission that is being counted toward."""
    moment = when or _utc_now()

    shape = _shape_ok(record, RECEIPT_KIND)
    if shape:
        return _refused(LINK_RECEIPT_SHAPE, f"the receipt is malformed: {shape}")
    shape = _shape_ok(attestation, ATTESTATION_KIND)
    if shape:
        return _refused(LINK_RECEIPT_SHAPE,
                        f"the attestation is malformed: {shape}",
                        task_id=record["task_id"])

    task_id = record["task_id"]
    try:
        digest = receipt_digest(record)
    except (NotCanonicalizable, TypeError, ValueError) as error:
        return _refused(LINK_RECEIPT_SHAPE,
                        f"the receipt cannot be digested: {error}",
                        task_id=task_id)

    # Who signed the receipt, and may that signer act. Both questions, both
    # answered, before any binding check reads a member of the record.
    peer_attr = directory.attribute(RECEIPT_DOMAIN, record, peer_id,
                                    when=moment)
    attestor_name = attestation["attestor"]
    attestor_attr = directory.attribute(ATTESTATION_DOMAIN, attestation,
                                        attestor_name, when=moment)

    def refuse(link: str, reason: str) -> ReceiptCheck:
        return _refused(link, reason, task_id=task_id, digest=digest,
                        peer=peer_attr, attestor=attestor_attr)

    if not peer_attr.verified:
        return refuse(LINK_RECEIPT_SIGNATURE,
                      f"the receipt is not attributable to {peer_id!r}: "
                      f"{peer_attr.reason}")
    if not peer_attr.confers_authority:
        return refuse(LINK_RECEIPT_KEY_AUTHORITY,
                      f"the receipt verifies under key {peer_attr.key_id} and "
                      f"that key confers nothing now: {peer_attr.reason}")

    if record["pool_id"] != pool_id:
        return refuse(LINK_RECEIPT_POOL,
                      f"the receipt is for pool {record['pool_id']!r}, not "
                      f"{pool_id!r}")
    if record["peer_id"] != peer_id:
        return refuse(LINK_RECEIPT_PEER,
                      f"the receipt names peer {record['peer_id']!r}, not "
                      f"{peer_id!r}")
    if record["artifact_digest"] != artifact_digest:
        return refuse(LINK_RECEIPT_ARTIFACT,
                      f"the receipt is for artifact "
                      f"{record['artifact_digest'][:16]}, and {peer_id!r} was "
                      f"admitted running {artifact_digest[:16]}")

    issued = _parse_iso(record["issued_at"])
    admitted = _parse_iso(admitted_at)
    if issued is None:
        return refuse(LINK_RECEIPT_SHAPE,
                      f"issued_at {record['issued_at']!r} is not a timestamp")
    if admitted is not None and issued < admitted:
        return refuse(LINK_STALE_RECEIPT,
                      f"the receipt was issued at {record['issued_at']} and "
                      f"this membership began at {admitted_at}; evidence does "
                      f"not carry across a withdrawal")

    # The attestation. Its binding to THIS receipt is checked before its
    # authority, so an attestation for another receipt is refused as the wrong
    # binding rather than as an authority failure.
    if attestation["receipt_digest"] != digest:
        return refuse(LINK_RECEIPT_BINDING,
                      f"the attestation covers receipt "
                      f"{attestation['receipt_digest'][:16]} and this receipt "
                      f"is {digest[:16]}")
    if attestation["pool_id"] != pool_id:
        return refuse(LINK_RECEIPT_POOL,
                      f"the attestation is for pool "
                      f"{attestation['pool_id']!r}, not {pool_id!r}")
    if attestation.get("verdict") != ADMITTED:
        return refuse(LINK_ATTESTATION_AUTHORITY,
                      f"the attestation's verdict is "
                      f"{attestation.get('verdict')!r}, not {ADMITTED!r}")

    if not attestor_attr.verified:
        return refuse(LINK_ATTESTATION_SIGNATURE,
                      f"the attestation is not attributable to "
                      f"{attestor_name!r}: {attestor_attr.reason}")
    if attestor_name == peer_id or attestor_attr.key_id == peer_attr.key_id:
        return refuse(LINK_SELF_ATTESTATION,
                      f"{peer_id!r} attested its own work; a peer cannot "
                      f"attest its own promotion even if its key is pinned as "
                      f"attesting authority")
    if attestor_attr.key_id not in set(attest_key_ids):
        return refuse(LINK_ATTESTATION_AUTHORITY,
                      f"key {attestor_attr.key_id} is not named as attesting "
                      f"authority by this charter "
                      f"({', '.join(sorted(attest_key_ids)) or 'nobody'})")
    if not attestor_attr.confers_authority:
        return refuse(LINK_ATTESTATION_AUTHORITY,
                      f"the attestation verifies under key "
                      f"{attestor_attr.key_id} and that key confers nothing "
                      f"now: {attestor_attr.reason}")

    return ReceiptCheck(task_id=task_id, digest=digest, peer=peer_attr,
                        attestor=attestor_attr, link="",
                        reason=(f"task {task_id} run by {peer_id!r}, attested "
                                f"by {attestor_name!r}"))


# ---------------------------------------------------------------------------
# counting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceCount:
    """What a promotion may cite: the receipts that counted, by digest, and
    every one that did not, with the link that refused it.

    ``counted`` is ``len(digests)`` and not a separate number, so the count and
    the citation cannot disagree."""

    digests: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    attestor_key_ids: tuple[str, ...] = ()
    rejected: tuple[ReceiptCheck, ...] = field(default=())

    @property
    def counted(self) -> int:
        return len(self.digests)

    def as_dict(self) -> dict:
        return {"counted": self.counted, "digests": list(self.digests),
                "task_ids": list(self.task_ids),
                "attestor_key_ids": list(self.attestor_key_ids),
                "rejected": [r.as_dict() for r in self.rejected]}


def count_evidence(pairs: Sequence[Any], *, pool_id: str, peer_id: str,
                   artifact_digest: str, admitted_at: str,
                   attest_key_ids: Iterable[str],
                   directory: IdentityDirectory,
                   when: Optional[datetime] = None) -> EvidenceCount:
    """Count the distinct tasks this peer has a verified, attested receipt for.

    ``pairs`` is a sequence of ``(receipt, attestation)``. The fold is
    deterministic: pairs are ordered by the receipt's ``issued_at`` and then by
    its digest before counting, so which of two receipts for one task is the
    counted one does not depend on the order a caller happened to pass them in.

    Deduplication is on ``task_id``. A peer can re-sign the same work with a
    new timestamp and get a new digest for nothing, so the digest is not the
    stable identity of a unit of work and counting distinct digests would let a
    peer inflate its own evidence at no cost."""
    ordered = []
    for pair in pairs:
        try:
            record, attestation = pair
        except (TypeError, ValueError):
            ordered.append(("", "", pair, None))
            continue
        key_time = record.get("issued_at", "") \
            if isinstance(record, Mapping) else ""
        try:
            key_digest = receipt_digest(record) \
                if isinstance(record, Mapping) else ""
        except (NotCanonicalizable, TypeError, ValueError):
            key_digest = ""
        ordered.append((str(key_time), key_digest, record, attestation))
    ordered.sort(key=lambda row: (row[0], row[1]))

    digests: list[str] = []
    task_ids: list[str] = []
    attestors: list[str] = []
    rejected: list[ReceiptCheck] = []
    seen_tasks: set[str] = set()

    for _, _, record, attestation in ordered:
        check = check_receipt(
            record, attestation, pool_id=pool_id, peer_id=peer_id,
            artifact_digest=artifact_digest, admitted_at=admitted_at,
            attest_key_ids=attest_key_ids, directory=directory, when=when)
        if not check.counts:
            rejected.append(check)
            continue
        if check.task_id in seen_tasks:
            # Recorded, never dropped: a delivered-twice attempt that vanished
            # would be a delivery the ledger cannot show was refused.
            rejected.append(ReceiptCheck(
                task_id=check.task_id, digest=check.digest, peer=check.peer,
                attestor=check.attestor, link=LINK_REPLAYED_RECEIPT,
                reason=(f"task {check.task_id} already has a counted receipt; "
                        f"this one counts for nothing")))
            continue
        seen_tasks.add(check.task_id)
        digests.append(check.digest)
        task_ids.append(check.task_id)
        if check.attestor.key_id not in attestors:
            attestors.append(check.attestor.key_id)

    return EvidenceCount(digests=tuple(digests), task_ids=tuple(task_ids),
                         attestor_key_ids=tuple(sorted(attestors)),
                         rejected=tuple(rejected))

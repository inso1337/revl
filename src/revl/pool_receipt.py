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

1. The **execution receipt** is signed by the PEER, over what it ran, what
   came out and the authority it ran under (issue #2720): the pool, itself,
   the task digest, the granted capability set, the policy digest, the
   artifact digest it was admitted with, the result digest, the outcome and its
   own budget claim. Under asymmetric identity (issue #1278) this is
   attributable to the peer by anyone holding its public key, so it is the
   peer's own non-repudiable statement "I ran this, under this authority, and
   produced that".

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

The authority binding, and the version that carries it
======================================================

A receipt used to say who ran what and nothing about the authority the run
happened under (issue #2720). Version :data:`RECEIPT_VERSION` adds
``task_digest``, ``grant``, ``policy_digest``, ``outcome`` and
``budget_consumed``. The first three are CHECKED against the operator-signed
task presented to :func:`check_receipt`; the last two are the peer's own
CLAIMS, carried and never proven. The version field decides which shape a
record must have, so a 1.1 receipt that drops a member is malformed rather than
read as 1.0. A 1.0 receipt still verifies as a statement about work, but
:func:`count_evidence` requires the binding by default: authority is not
evidence for authority.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import peer_identity, pool_bundle
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
#: The receipt that records the authority the run happened under (issue #2720).
#: A 1.0 receipt is still a valid statement about work; it just cannot say what
#: grant or policy was in force, so it verifies and does not count as evidence
#: for a promotion that requires the binding.
RECEIPT_VERSION = "1.1"
#: The shape before issue #2720: no task digest, grant, policy digest, outcome
#: or budget claim. Kept as a named constant so the reader decides by VERSION
#: and never by looking at which members happen to be present.
RECEIPT_VERSION_V1 = "1.0"

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

#: How a RUN ended (issue #2720). A refusal happens BEFORE a run: the peer
#: returns a refusal record with a link and signs no receipt for work it did not
#: do, so ``REFUSED`` is reserved for a receipt a peer issues for a run the
#: runner itself declined and is not produced by this tree today. ``TIMED_OUT``
#: is measured by `pool_dispatch.execute`, never inferred from exit code 124,
#: because a program is free to exit 124 on its own.
COMPLETED = "completed"
REFUSED = "refused"
FAILED = "failed"
TIMED_OUT = "timed_out"
OUTCOMES: tuple[str, ...] = (COMPLETED, REFUSED, FAILED, TIMED_OUT)

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
#: The receipt names a task digest, and no task was presented, or the task
#: presented does not hash to it. A bound receipt is only checkable against the
#: task it answers.
LINK_RECEIPT_TASK = "receipt-task"
#: The receipt echoes a granted capability set (or its budgets) that is not the
#: one the operator signed into the task. One of the two says something the
#: other does not, and the task is the decision.
LINK_RECEIPT_GRANT = "receipt-grant"
#: The receipt echoes a policy digest that is not the one the operator signed
#: into the task. The run is claimed under a policy that was not in force.
LINK_RECEIPT_POLICY = "receipt-policy"
#: The receipt is version 1.0: it verifies, but it names no grant, policy,
#: outcome or budget claim, so it cannot count as evidence for a promotion that
#: requires the binding. Refused rather than read as the new shape.
LINK_UNBOUND_RECEIPT = "unbound-receipt"
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
    LINK_RECEIPT_TASK,
    LINK_RECEIPT_GRANT,
    LINK_RECEIPT_POLICY,
    LINK_UNBOUND_RECEIPT,
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
    cannot be slid from one onto the other. Also the digest a bound receipt
    pins its task by (``task_digest``): the whole signed task, signature
    included, so the peer's echo names the exact record it answered."""
    return hashlib.sha256(_canonical_bytes(dict(record))).hexdigest()


def grant_block(grant: Any) -> Optional[dict]:
    """A granted capability set with its budgets, normalized, or ``None``.

    One spelling for one grant: ``caps`` sorted and ``budgets`` sorted by key,
    so the task the operator signed and the receipt the peer echoes compare by
    value and not by the order two JSON writers happened to use. A budget is a
    plain non-negative integer, following `peer_authority`: a negative or
    fractional budget is not a smaller budget, it is a malformed one."""
    if not isinstance(grant, Mapping):
        return None
    caps = grant.get("caps")
    budgets = grant.get("budgets", {})
    if not isinstance(caps, (list, tuple)):
        return None
    if not all(isinstance(cap, str) and cap for cap in caps):
        return None
    if not isinstance(budgets, Mapping):
        return None
    clean: dict[str, int] = {}
    for name, amount in budgets.items():
        if not isinstance(name, str) or not name:
            return None
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            return None
        clean[name] = amount
    return {"caps": sorted(caps),
            "budgets": {name: clean[name] for name in sorted(clean)}}


def budget_block(consumed: Any) -> Optional[dict]:
    """A peer's claim of what it consumed, normalized, or ``None``.

    Numbers, not integers: the one quantity a runner here measures is wall-clock
    seconds. Nothing checks this map against the granted budgets and nothing
    should: it is the peer's own report, labelled as a claim wherever it is
    read."""
    if consumed is None:
        return {}
    if not isinstance(consumed, Mapping):
        return None
    clean: dict[str, Any] = {}
    for name, amount in consumed.items():
        if not isinstance(name, str) or not name:
            return None
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return None
        if isinstance(amount, float) and (amount != amount or amount < 0
                                          or amount == float("inf")):
            return None
        if isinstance(amount, int) and amount < 0:
            return None
        clean[name] = amount
    return {name: clean[name] for name in sorted(clean)}


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
                  at: Optional[datetime] = None,
                  bundle: Optional[Mapping[str, Any]] = None,
                  network_exposed: bool = False,
                  task_digest: Optional[str] = None,
                  grant: Optional[Mapping[str, Any]] = None,
                  policy_digest: Optional[str] = None,
                  outcome: str = COMPLETED,
                  budget_consumed: Optional[Mapping[str, Any]] = None) -> dict:
    """The peer signs what it ran and what came out.

    ``identity.peer_id`` is the peer named in the receipt; it is not a separate
    parameter, so a receipt cannot be issued in another peer's name by a caller
    that holds only its own key.

    ``bundle`` is the ``{digest, files}`` block of a multi-file artifact
    (`pool_bundle`). It goes inside the signed body, so the receipt names the
    bundle digest and every path, mode and file digest it covers, and
    :func:`check_receipt` refuses one whose block disagrees with its
    ``artifact_digest``.

    ``task_digest``, ``grant`` and ``policy_digest`` are the authority binding
    (issue #2720): the exact task the peer answered, the capability set that
    task granted and the policy digest it named. They are all-or-none. A
    receipt issued with them is version :data:`RECEIPT_VERSION`; one issued
    without them is version :data:`RECEIPT_VERSION_V1`, the old shape, which
    still verifies but cannot count for a promotion that requires the binding.
    ``outcome`` and ``budget_consumed`` travel with the binding; ``outcome`` is
    the run's ending, and ``budget_consumed`` is the peer's own report and a
    CLAIM, not a proof.

    ``network_exposed`` is passed to :func:`revl.peer_identity.sign_record`
    (issue #1460)."""
    if not isinstance(identity, PeerIdentity):
        raise ReceiptError("issuing a receipt needs the peer's own PeerIdentity")
    for name, value in (("pool_id", pool_id), ("task_id", task_id),
                        ("artifact_digest", artifact_digest)):
        if not isinstance(value, str) or not value:
            raise ReceiptError(f"a receipt needs a non-empty {name}")
    bound = (task_digest is not None or grant is not None
             or policy_digest is not None)
    if bound:
        if not isinstance(task_digest, str) or not task_digest:
            raise ReceiptError("a bound receipt needs the task digest it answers")
        if not isinstance(policy_digest, str) or not policy_digest:
            raise ReceiptError("a bound receipt needs the policy digest in force")
        granted = grant_block(grant)
        if granted is None:
            raise ReceiptError("a bound receipt needs a readable granted "
                               "capability set with budgets")
        if outcome not in OUTCOMES:
            raise ReceiptError(f"outcome {outcome!r} is not one of "
                               f"{', '.join(OUTCOMES)}")
        consumed = budget_block(budget_consumed)
        if consumed is None:
            raise ReceiptError("budget_consumed is not a map of non-negative "
                               "numbers")
    elif outcome != COMPLETED or budget_consumed:
        raise ReceiptError("an unbound receipt states no outcome and no budget "
                           "use; give it a task digest, grant and policy digest")
    body = {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION if bound else RECEIPT_VERSION_V1,
        "pool_id": pool_id,
        "peer_id": identity.peer_id,
        "task_id": task_id,
        "artifact_digest": artifact_digest,
        "result_digest": result_digest(result),
        "issued_at": _iso(at or _utc_now()),
    }
    if bound:
        body.update({"task_digest": task_digest, "grant": granted,
                     "policy_digest": policy_digest, "outcome": outcome,
                     "budget_consumed": consumed})
    if bundle is not None:
        body["bundle"] = {"digest": bundle.get("digest", ""),
                          "files": [dict(e) for e in bundle.get("files", ())]}
    return peer_identity.sign_record(RECEIPT_DOMAIN, body, identity,
                                     network_exposed=network_exposed)


def attest_receipt(record: Mapping[str, Any], *, identity: PeerIdentity,
                   verdict: str = ADMITTED,
                   at: Optional[datetime] = None,
                   network_exposed: bool = False) -> dict:
    """An attesting authority signs the digest of a peer's receipt.

    It signs the digest and not the body: there is then exactly one copy of
    what is being attested, so an attestation and the receipt it covers cannot
    drift apart. ``network_exposed`` is passed to
    :func:`revl.peer_identity.sign_record` (issue #1460)."""
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
    return peer_identity.sign_record(ATTESTATION_DOMAIN, body, identity,
                                     network_exposed=network_exposed)


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
    refusal path never indexes into something hostile and never raises. The
    version decides which members a receipt must carry: a 1.1 receipt without
    the authority binding is malformed, never quietly read as the 1.0 shape,
    because guessing is exactly how a missing field becomes a passing check."""
    if not isinstance(record, Mapping):
        return f"expected a {kind} record, got {type(record).__name__}"
    if record.get("kind") != kind:
        return f"expected kind {kind!r}, got {record.get('kind')!r}"
    if kind == RECEIPT_KIND:
        version = record.get("version")
        if version not in (RECEIPT_VERSION_V1, RECEIPT_VERSION):
            return (f"its version is {version!r}, not one this checker knows "
                    f"({RECEIPT_VERSION_V1!r} or {RECEIPT_VERSION!r})")
    required = ("pool_id", "peer_id", "task_id")
    if kind == RECEIPT_KIND:
        required += ("artifact_digest", "result_digest", "issued_at")
        if record.get("version") == RECEIPT_VERSION:
            required += ("task_digest", "policy_digest", "outcome")
    else:
        required += ("receipt_digest", "verdict", "attestor")
    for member in required:
        if not isinstance(record.get(member), str) or not record.get(member):
            return f"its {member} is missing or is not a string"
    if kind == RECEIPT_KIND and record.get("version") == RECEIPT_VERSION:
        if "grant" not in record or grant_block(record.get("grant")) is None:
            return ("its grant is missing or is not a capability set with "
                    "non-negative integer budgets")
        if record["outcome"] not in OUTCOMES:
            return (f"its outcome is {record['outcome']!r}, not one of "
                    f"{', '.join(OUTCOMES)}")
        if "budget_consumed" not in record \
                or budget_block(record.get("budget_consumed")) is None:
            return ("its budget_consumed is missing or is not a map of "
                    "non-negative numbers")
    return ""


def check_receipt(record: Any, attestation: Any, *, pool_id: str,
                  peer_id: str, artifact_digest: str, admitted_at: str,
                  attest_key_ids: Iterable[str],
                  directory: IdentityDirectory,
                  when: Optional[datetime] = None,
                  task: Optional[Mapping[str, Any]] = None,
                  require_bound: bool = False) -> ReceiptCheck:
    """Check one receipt and its attestation against one member's admission.

    Never raises. A hostile record gets a verdict, like
    ``peer_offer.verify_offer`` and ``peer_pool``'s gate, because a record that
    can choose a traceback over a refusal has chosen its own outcome.

    The binding checks are all of the form "is this receipt about the thing the
    pool admitted": the same pool, the same peer, the same artifact digest, and
    issued no earlier than the admission that is being counted toward.

    ``task`` is the signed task the receipt claims to answer. A version
    :data:`RECEIPT_VERSION` receipt REQUIRES it: the receipt echoes the task
    digest, the granted capability set and the policy digest, and each is
    compared against the operator-signed task, which is the party that decided
    them. The task's ``task_id``, ``peer_id``, ``pool_id`` and
    ``artifact_digest`` must also equal the receipt's, because a digest match
    names a record and the record has to describe the work the receipt claims;
    otherwise a receipt for one task could cite another task's record. Those
    are checkable facts. The receipt's ``outcome`` and ``budget_consumed`` are
    the peer's own claims and are carried through, not proven here.

    ``require_bound`` is what a promotion asks for. A version
    :data:`RECEIPT_VERSION_V1` receipt still VERIFIES with this flag false: the
    signature arithmetic is real and the work really happened. With it true,
    the receipt is refused on :data:`LINK_UNBOUND_RECEIPT` after every other
    check passes, because it names no authority and cannot be evidence for a
    gate that requires one. The version field decides which branch runs; the
    presence or absence of members never does."""
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
    if "bundle" in record:
        # One claim, two spellings: the artifact digest and the bundle block
        # the peer signed beside it. They must agree, or the receipt says two
        # different things about what ran.
        why = pool_bundle.described_problem(record["bundle"],
                                            record["artifact_digest"])
        if why:
            return refuse(LINK_RECEIPT_ARTIFACT,
                          f"the receipt names a bundle that is not its "
                          f"artifact: {why}")

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

    # The authority binding (issue #2720). The receipt echoes what the
    # operator-signed task decided, so a reader of the receipt alone can tell
    # which grant and policy the run happened under, and the echo is checkable
    # against the task rather than trusted. The ORDER is deliberate: a receipt
    # that names another task is refused as that before its attestation is
    # even looked at, because an attestation over the wrong task is a
    # downstream fact.
    if record["version"] == RECEIPT_VERSION:
        if not isinstance(task, Mapping):
            return refuse(LINK_RECEIPT_TASK,
                          "the receipt echoes a task digest and no task was "
                          "presented to check it against; a bound receipt is "
                          "only evidence together with the task it answers")
        try:
            expected_task = receipt_digest(task)
        except (NotCanonicalizable, TypeError, ValueError) as error:
            return refuse(LINK_RECEIPT_TASK,
                          f"the task presented cannot be digested: {error}")
        if record["task_digest"] != expected_task:
            return refuse(LINK_RECEIPT_TASK,
                          f"the receipt answers task {record['task_digest'][:16]} "
                          f"and the task presented is {expected_task[:16]}")
        # The digest matches, so the task is the record the receipt echoed.
        # That record must still be ABOUT the task the receipt names: a
        # receipt for task-1 whose peer signed a claim pointing at task-2's
        # record would otherwise verify against task-2 and cite it as
        # evidence for work it does not describe.
        for member in ("task_id", "peer_id", "pool_id", "artifact_digest"):
            if task.get(member) != record[member]:
                return refuse(
                    LINK_RECEIPT_TASK,
                    f"the receipt names {member} {record[member]!r} and the "
                    f"task it answers names {task.get(member)!r}; a receipt "
                    f"answers the task it names, not another record that "
                    f"carries the same digest")
        task_grant = grant_block(task.get("grant"))
        if task_grant is None:
            return refuse(LINK_RECEIPT_GRANT,
                          "the task presented carries no readable granted "
                          "capability set")
        receipt_grant = grant_block(record["grant"])
        if receipt_grant != task_grant:
            return refuse(LINK_RECEIPT_GRANT,
                          f"the receipt echoes a grant "
                          f"({', '.join(receipt_grant['caps']) or 'no caps'}; "
                          f"{len(receipt_grant['budgets'])} budgets) and the "
                          f"task granted "
                          f"({', '.join(task_grant['caps']) or 'no caps'}; "
                          f"{len(task_grant['budgets'])} budgets)")
        if record["policy_digest"] != task.get("policy_digest"):
            return refuse(LINK_RECEIPT_POLICY,
                          f"the receipt echoes policy "
                          f"{record['policy_digest'][:16]} and the task was "
                          f"signed under "
                          f"{str(task.get('policy_digest'))[:16] or 'no policy'}")

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

    if require_bound and record["version"] != RECEIPT_VERSION:
        # Last, not first: everything this receipt CAN say has been checked,
        # so an operator auditing a refusal sees the specific fault when there
        # is one and the weakness of the shape only when there is not.
        return refuse(LINK_UNBOUND_RECEIPT,
                      f"the receipt is version {record['version']} and carries "
                      f"no task, grant, policy, outcome or budget claim; it "
                      f"verifies but a promotion that requires the authority "
                      f"binding cannot count it")

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
                   when: Optional[datetime] = None,
                   require_bound: bool = True) -> EvidenceCount:
    """Count the distinct tasks this peer has a verified, attested receipt for.

    ``pairs`` is a sequence of ``(receipt, attestation)`` or
    ``(receipt, attestation, task)``; the task is what a version
    :data:`RECEIPT_VERSION` receipt is checked against. A pair without one is
    still accepted so an old ledger reads, and its receipt is refused on
    :data:`LINK_RECEIPT_TASK` if it claims the binding.

    ``require_bound`` defaults to TRUE, which is what a promotion requires: a
    version :data:`RECEIPT_VERSION_V1` receipt verifies and states no grant,
    policy, outcome or budget, so it cannot be evidence for authority it never
    named. The version field decides, never the members that happen to be
    present.

    The fold is deterministic: pairs are ordered by the receipt's ``issued_at``
    and then by its digest before counting, so which of two receipts for one
    task is the counted one does not depend on the order a caller happened to
    pass them in.

    Deduplication is on ``task_id``. A peer can re-sign the same work with a
    new timestamp and get a new digest for nothing, so the digest is not the
    stable identity of a unit of work and counting distinct digests would let a
    peer inflate its own evidence at no cost."""
    ordered = []
    for pair in pairs:
        record: Any = None
        attestation: Any = None
        task: Any = None
        try:
            if isinstance(pair, (tuple, list)) and len(pair) == 3:
                record, attestation, task = pair
            else:
                record, attestation = pair
        except (TypeError, ValueError):
            ordered.append(("", "", pair, None, None))
            continue
        key_time = record.get("issued_at", "") \
            if isinstance(record, Mapping) else ""
        try:
            key_digest = receipt_digest(record) \
                if isinstance(record, Mapping) else ""
        except (NotCanonicalizable, TypeError, ValueError):
            key_digest = ""
        ordered.append((str(key_time), key_digest, record, attestation, task))
    ordered.sort(key=lambda row: (row[0], row[1]))

    digests: list[str] = []
    task_ids: list[str] = []
    attestors: list[str] = []
    rejected: list[ReceiptCheck] = []
    seen_tasks: set[str] = set()

    for _, _, record, attestation, task in ordered:
        check = check_receipt(
            record, attestation, pool_id=pool_id, peer_id=peer_id,
            artifact_digest=artifact_digest, admitted_at=admitted_at,
            attest_key_ids=attest_key_ids, directory=directory, when=when,
            task=task if isinstance(task, Mapping) else None,
            require_bound=require_bound)
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

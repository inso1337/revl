"""The private peer pool as a declared, operable object (roadmap item 524).

Three kernels already decide the hard parts of a peer pool: ``peer_offer``
signs and matches one peer against one placement slot, ``peer_authority``
bounds what a delegate may receive, and ``lawful_retry`` decides replay versus
compensate when a peer is lost. What no module owned was the POOL itself: a
named thing an operator declares, a roster a peer joins, an authority view
naming who may admit and revoke, and a ledger that says what a peer leaving
does and does not undo. Without that the guarantee is exercised only inside the
test suite, which is exactly what issue #1198 reports.

This module is that object, and only that object. It is the pool declaration
plus the gate for JOINING it. Running work on a member, moving bytes over a
wire and delivering a result are still ``#421``'s network seam and the
``tee_attestation`` delivery path; nothing here opens a socket.

The trust progression, and where each arrow fails
-------------------------------------------------
Every arrow below increases a peer's authority, so every arrow states a failure
direction, and the direction is always the same one: a peer whose evidence is
missing gets the authority of a peer with no evidence, never the authority of
the peer whose evidence passed.

1. **A peer is named** -> the operator holds a key for it. No key, no read of
   anything the peer said (:data:`LINK_UNKNOWN_PEER`).
2. **The operator admitting it holds the authority to admit** -> the admitting
   key fingerprint is in the charter's ``admit_key_ids``. Checked BEFORE any
   peer-supplied member is parsed, so an operator with no admit authority
   cannot use the gate as an oracle (:data:`LINK_ADMITTING_AUTHORITY`).
3. **Identity is a key, not an address** -> the join record and the peer offer
   inside it are both signed by the peer, under the same key and with the same
   backing, and the offer's own ``peer_id`` must be the one the join claims
   (:data:`LINK_JOIN_SIGNATURE`, :data:`LINK_OFFER_SIGNATURE`,
   :data:`LINK_OFFER_IDENTITY`). Which backing is admitted is the charter's
   ``identity_mode``, and a peer with a pinned public key may use only the
   asymmetric one (:data:`LINK_IDENTITY_MODE`,
   :data:`LINK_IDENTITY_DOWNGRADE`). A key the directory does not hold signs
   nothing (:data:`LINK_UNKNOWN_KEY`); a key it holds as revoked or superseded
   still VERIFIES what it signed and admits nobody
   (:data:`LINK_REVOKED_KEY`).
4. **The peer agreed to THESE terms** -> the join names the charter by digest,
   not by name, so a peer that signed up to a narrow charter is not admitted
   under a wider one that reuses the name (:data:`LINK_POOL_IDENTITY`).
5. **The request is fresh and used once** -> a join outside the charter's
   window, or one whose ``(peer_id, nonce)`` the roster already spent, is
   refused (:data:`LINK_STALE_JOIN`, :data:`LINK_REPLAYED_JOIN`).
6. **The artifact is the admitted one** -> the join pins the candidate by
   digest and the charter lists the digests it admits
   (:data:`LINK_ARTIFACT_DIGEST`).
7. **The peer clears the floor** -> attested trust at or above the charter's
   floor (:data:`LINK_TRUST_FLOOR`).
8. **It enters at the bottom** -> a join issues :data:`ENTRY_TIER` and nothing
   else. There is no argument a peer can make in a join record that lands it
   higher (:data:`LINK_ENTRY_TIER`).
9. **It is handed only what the pool itself holds** -> the tier grant is a
   narrowing of the charter ceiling AND is covered by the peer's own advertised
   ceiling (:data:`LINK_GRANT_CEILING`).
10. **It is sent only work its tier admits** -> :func:`work_admissible` keys on
    ``lawful_retry.EffectClass``, the classification the dispatcher already
    consults, so there is no second, weaker effect taxonomy here. The entry tier
    admits pure work only.
11. **Authority rises only on evidence** -> :func:`promote` requires the tier's
    declared count of receipts attested by a key in the charter's
    ``attest_key_ids``. Missing evidence leaves the member where it was
    (:data:`LINK_PROMOTION_EVIDENCE`).
12. **Authority falls on withdrawal** -> :func:`withdraw` revokes the grant and
    says, in three separate words, what that does and does not undo.

The authority diff is a precondition, not a stage
-------------------------------------------------
Item 518 settled the shape for a promotion whose safety rests on a diff: make
the diff a PRECONDITION of issuing the thing, not a stage the thing passes
through, so there is no ordering in which a membership exists before its grant
was checked. The same move applies here and is enforced structurally.
:func:`_tier_grant` is reachable only from :func:`_ceiling_precondition`, a
:class:`Membership` is constructed only in :func:`_issue_membership`, and every
function that calls :func:`_issue_membership` also calls
:func:`_ceiling_precondition`. ``tests/test_peer_pool_admission.py`` walks this
module's own AST and asserts all three, so a later edit that computes a grant
somewhere else, or issues a membership without the diff, fails a test rather
than shipping.

In :func:`promote` the diff is also ahead of the only reading that is a
MEASUREMENT. A member's ``evidence`` is the count of attested receipts it
accumulated while it worked; the ceiling diff is a property of the charter and
the tier alone. Running the diff first means a tier the pool cannot lawfully
issue is refused without the peer's record being read at all, which is item
543's rule (issue #1222) that the authority diff gates entry to the measured
stages rather than being weighed after them. This module is registered as a
promotion path in :mod:`revl.promotion_barrier`, and that registry entry is
what holds the order.

Both entry and promotion diff the tier grant against the CHARTER CEILING, never
against the tier below. A ladder that measured each rung against the previous
one would let an error in rung 1 raise the ceiling for every rung above it; a
ladder measured against the charter cannot, because the charter is the same
fixed record at every rung.

Withdrawal is three words, not one
----------------------------------
Item 546 settled that accumulated state has no inverse and that "rolled back"
must not be one word covering both what is restored and what is merely
compensated. A peer's history is exactly that shape, so :class:`Withdrawal`
reports three disjoint sets and never a single boolean:

* ``revoked``, the caps and budgets the peer held and holds no longer. This IS
  an inverse: after a withdrawal the peer's authority is byte-for-byte the
  authority of a peer that never joined.
* ``retained``, the receipts it delivered, the evidence it accumulated, the
  effects already witnessed, and the highest tier it reached. Withdrawal does
  not un-observe any of it. It stays in the ledger, and it does NOT re-confer
  authority: a withdrawn peer that joins again re-enters at
  :data:`ENTRY_TIER`, because the evidence is a record of what happened and not
  a credential that survives its holder's removal.
* ``orphaned``, work dispatched to the peer that had not been delivered when
  it left. This is neither restored nor compensated here. It is named and
  handed to ``lawful_retry.dispatch_on_loss``, which is the module that already
  owns replay-versus-compensate, so the effect of a peer leaving on outstanding
  work is stated rather than implied.

What the signatures do and do not prove
---------------------------------------
Every signature here covers a per-protocol domain prefix plus the canonical
bytes of the record, and the covered set is DERIVED FROM THE RECORD rather than
from a hand-written field list (item 517's discipline, written after a receipt
in this tree carried a ``key_id`` that was added to the body after the MAC and
so was covered by nothing). Two backings sign that message:

* ``hmac-sha256`` under a key both parties hold. AUTHENTICATION and nothing
  more: the operator verifying a peer's join holds the key that signs it and
  could have produced it, so a compromised operator key forges a join for every
  peer whose key it holds, and no peer can prove to a third party what it did or
  did not sign.
* ``ecdsa-p256-sha256`` under the peer's own key pair
  (:mod:`revl.peer_identity`, issue #1278). The verifier holds only the public
  half, so a compromised operator key forges nothing, and any holder of that
  public half can check the record. That is non-repudiation UNDER ONE STATED
  ASSUMPTION, which is a deployment property and not a cryptographic one: the
  private half was generated on the peer's machine and never left it. An
  operator that generated a peer's key pair and handed it over can forge that
  peer's signatures exactly as it could forge its MAC. ``pool keygen`` draws on
  the peer's machine and ``peer_identity`` never moves a private scalar, so the
  honest deployment is the default one, but the assumption is stated rather than
  implied by the word "signed".

The charter's ``identity_mode`` says which backings a pool admits, ``pool
status`` reports the split per MEMBER, and there is no fallback in either
direction: ``sign_alg`` selects one verifier and its failure is the answer. A
peer the directory holds a key for cannot present a shared-key join at all, so
learning a legacy secret does not step an asymmetric peer back onto it.

What is NOT signed asymmetrically in this slice, stated rather than left to be
discovered: the CHARTER is still MAC'd under the operator's symmetric key, and
so is the ADMIT receipt. Both are the operator's own records, read by operators
who already hold that key, and neither is a peer's claim about itself. Moving
them is a separate change with a separate migration, and
``docs/design/555-asymmetric-peer-identity.md`` says so.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from . import cap_order, deploy, peer_authority, peer_identity, peer_offer
from .attest import NotCanonicalizable, _canonical_bytes, key_id
from .lawful_retry import EffectClass
from .peer_identity import (
    IDENTITY_ASYMMETRIC,
    IDENTITY_SHARED_KEY,
    IdentityDirectory,
    KEY_ACTIVE,
    PeerIdentity,
)

# ---------------------------------------------------------------------------
# envelope identities and MAC domains
# ---------------------------------------------------------------------------

#: The charter envelope: what an operator declares when standing a pool up.
CHARTER_KIND = "revl.pool-charter"
CHARTER_VERSION = "1.0"

#: A peer's request to join a named charter.
JOIN_KIND = "revl.pool-join"
JOIN_VERSION = "1.0"

#: The signed admission or refusal the gate returns.
RECEIPT_KIND = "revl.pool-receipt"
RECEIPT_VERSION = "1.0"

#: The two identity backings a pool record may carry. `sign_alg` SELECTS the
#: verifier; it never merely records which one was used. A record is checked
#: under the algorithm it names and under no other, so a join whose asymmetric
#: signature fails is REFUSED rather than retried as a MAC. There is deliberately
#: no fallback: a downgrade an attacker can trigger is not a migration path, and
#: "unverifiable" must land on refuse, not on the weaker check.
SIGN_ALG = "hmac-sha256"
SIGN_ALG_ECDSA = peer_identity.SIGN_ALG
SIGN_ALGS: tuple[str, ...] = (SIGN_ALG, SIGN_ALG_ECDSA)

#: What a charter admits. `asymmetric` is the setting issue #1278 exists to make
#: available and the one `revl pool init` writes; `shared-key` is item 524's
#: original deployment; `mixed` is the migration window, and it is the reason
#: `pool status` reports the split per member rather than a single word. A mixed
#: pool is as strong as its weakest member and the status output says so.
MODE_ASYMMETRIC = "asymmetric"
MODE_SHARED_KEY = "shared-key"
MODE_MIXED = "mixed"
IDENTITY_MODES: tuple[str, ...] = (MODE_ASYMMETRIC, MODE_SHARED_KEY, MODE_MIXED)

#: Which identity backings each mode admits.
MODE_ADMITS: dict[str, tuple[str, ...]] = {
    MODE_ASYMMETRIC: (IDENTITY_ASYMMETRIC,),
    MODE_SHARED_KEY: (IDENTITY_SHARED_KEY,),
    MODE_MIXED: (IDENTITY_ASYMMETRIC, IDENTITY_SHARED_KEY),
}

#: The backing each `sign_alg` spelling means. A spelling not in this map is
#: refused rather than defaulted.
_ALG_IDENTITY: dict[str, str] = {
    SIGN_ALG: IDENTITY_SHARED_KEY,
    SIGN_ALG_ECDSA: IDENTITY_ASYMMETRIC,
}

SIGNATURE_FIELD = "signature"

#: Three protocols, three domains. Without domain separation a charter, a join
#: and a pool receipt are the same `hmac(key, canonical(body))` construction, so
#: one could be replayed as another under a shared key, and the same reasoning
#: keeps all three distinct from `attest.SIGN_DOMAIN`, `peer_offer.SIGN_DOMAIN`
#: and the deploy-receipt domain.
CHARTER_DOMAIN = b"revl.pool-charter/v1\x00"
JOIN_DOMAIN = b"revl.pool-join/v1\x00"
RECEIPT_DOMAIN = b"revl.pool-receipt/v1\x00"

#: A withdrawal is its own protocol. It is signed by the OPERATOR's revoke key,
#: not by the peer's, so a third party can check who removed whom without asking
#: the operator that did it.
WITHDRAWAL_DOMAIN = b"revl.pool-withdrawal/v1\x00"

ADMIT = "ADMIT"
REFUSE = "REFUSE"
PROMOTE = "PROMOTE"
WITHDRAW = "WITHDRAW"

# ---------------------------------------------------------------------------
# the named refusal links
# ---------------------------------------------------------------------------
# Lowercase links, not new guarantee codes. A pool refusal is a decision about a
# deployment, not a verdict about a program: `revl.deploy` already draws that
# line (its `LINK_SIGNER`/`LINK_ARTIFACT`/... set), and following it keeps this
# module out of `revl.diagnostics.GUARANTEES`, which is the register of codes the
# CHECKER issues and which requires an `examples/rejections/` reproducer per
# entry. A pool admission has no program to reproduce.

LINK_UNKNOWN_PEER = "unknown-peer"
LINK_ADMITTING_AUTHORITY = "admitting-authority"
LINK_CHARTER_SIGNATURE = "charter-signature"
LINK_JOIN_SIGNATURE = "join-signature"
LINK_POOL_IDENTITY = "pool-identity"
LINK_STALE_JOIN = "stale-join"
LINK_REPLAYED_JOIN = "replayed-join"
LINK_REVOKED_PEER = "revoked-peer"
LINK_DUPLICATE_MEMBER = "duplicate-member"
LINK_OFFER_SIGNATURE = "offer-signature"
LINK_OFFER_IDENTITY = "offer-identity"
LINK_TRUST_FLOOR = "trust-floor"
LINK_ARTIFACT_DIGEST = "artifact-digest"
LINK_ENTRY_TIER = "entry-tier"
LINK_GRANT_CEILING = "grant-ceiling"
LINK_PROMOTION_EVIDENCE = "promotion-evidence"
LINK_NOT_A_MEMBER = "not-a-member"
LINK_UNKNOWN_TIER = "unknown-tier"
LINK_IDENTITY_MODE = "identity-mode"
LINK_IDENTITY_DOWNGRADE = "identity-downgrade"
LINK_UNKNOWN_KEY = "unknown-key"
LINK_REVOKED_KEY = "revoked-key"

#: Every link this module can refuse on. A test asserts the set is exactly the
#: links the code reaches, so a link cannot be added and left unreachable (which
#: reads as a gate that exists) nor reached and left unnamed.
REFUSAL_LINKS: tuple[str, ...] = (
    LINK_UNKNOWN_PEER,
    LINK_ADMITTING_AUTHORITY,
    LINK_CHARTER_SIGNATURE,
    LINK_JOIN_SIGNATURE,
    LINK_POOL_IDENTITY,
    LINK_STALE_JOIN,
    LINK_REPLAYED_JOIN,
    LINK_REVOKED_PEER,
    LINK_DUPLICATE_MEMBER,
    LINK_OFFER_SIGNATURE,
    LINK_OFFER_IDENTITY,
    LINK_TRUST_FLOOR,
    LINK_ARTIFACT_DIGEST,
    LINK_ENTRY_TIER,
    LINK_GRANT_CEILING,
    LINK_PROMOTION_EVIDENCE,
    LINK_NOT_A_MEMBER,
    LINK_UNKNOWN_TIER,
    LINK_IDENTITY_MODE,
    LINK_IDENTITY_DOWNGRADE,
    LINK_UNKNOWN_KEY,
    LINK_REVOKED_KEY,
)

# ---------------------------------------------------------------------------
# the tier ladder
# ---------------------------------------------------------------------------

#: The tier every admitted peer enters at. A join issues this and nothing else.
ENTRY_TIER = "probation"

#: The ladder, least to most authority. A member's tier indexes it.
TIER_ORDER: tuple[str, ...] = ("probation", "replayable", "durable")

#: What each tier may be SENT, keyed on the effect classification
#: ``lawful_retry`` already consults. This is a lookup into that enum, not a
#: second taxonomy: adding an effect class there and not here leaves it
#: inadmissible at every tier, which is the fail-closed direction.
TIER_EFFECT_CEILING: dict[str, EffectClass] = {
    "probation": EffectClass.PURE,
    "replayable": EffectClass.IDEMPOTENT_EXTERNAL,
    "durable": EffectClass.WITNESSED,
}

#: Effect classes NO pool tier admits, and the reason each is excluded. These
#: are refusals, not omissions: `lawful_retry` already says a deferred
#: irreversible commit is resolvable only at a trusted commit authority and a
#: secret-bearing effect never goes to a bare `verified` offer. A pool peer is
#: by construction a machine the operator does not trust, so neither is ever
#: reachable by climbing this ladder.
UNREACHABLE_EFFECTS: dict[EffectClass, str] = {
    EffectClass.DEFERRED_IRREVERSIBLE: (
        "an irreversible commit is resolvable only at a trusted commit "
        "authority and is never speculatively issued to a pool peer"),
    EffectClass.SECRET_BEARING: (
        "a secret-bearing effect needs a trusted, local or hardware-attested "
        "host; a pool peer is a machine the operator does not trust"),
}

#: The order effect classes are admitted in, least to most authority. A tier
#: admits every class at or below its ceiling.
_EFFECT_RANK: dict[EffectClass, int] = {
    EffectClass.PURE: 0,
    EffectClass.IDEMPOTENT_EXTERNAL: 1,
    EffectClass.WITNESSED: 2,
}


class PoolError(ValueError):
    """A pool object is malformed at CONSTRUCTION time (an unknown tier, an
    unparseable ceiling capability, an empty pool id). Distinct from an
    admission refusal, which is returned as a receipt and never raises, so a
    hostile peer-supplied record cannot break that contract."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def canonical_digest(record: Mapping[str, Any]) -> str:
    """The sha256 of a record's canonical bytes, hex. This is how a join names
    the charter it agreed to: by the bytes of its terms, not by its name."""
    return hashlib.sha256(_canonical_bytes(dict(record))).hexdigest()


def _mac(domain: bytes, body: Mapping[str, Any], key: bytes) -> str:
    """HMAC-SHA256 over ``domain`` ++ the canonical bytes of every member of
    ``body`` EXCEPT ``signature``.

    The covered set is DERIVED FROM THE RECORD. That is item 517's discipline
    and the defence against the failure it was written after: a signed receipt
    in this tree once MAC'd a hand-written field list while its body grew a
    ``key_id`` member afterwards, so the member every reader used to choose a
    verification key was the one member the signature did not cover. There is no
    field list here to fall out of date with the body."""
    covered = {k: v for k, v in body.items() if k != SIGNATURE_FIELD}
    return hmac.new(bytes(key), domain + _canonical_bytes(covered),
                    hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# the charter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierGrant:
    """The authority one rung of the ladder hands a member, and what it costs.

    ``caps``/``budgets`` are the grant, in ``cap_order``'s grammar and
    ``peer_authority``'s budget map. ``evidence_required`` is the number of
    attested execution receipts a member must have accumulated before
    :func:`promote` will move it here; the entry tier's is 0 because entry is
    not a promotion."""

    caps: tuple[str, ...] = ()
    budgets: Mapping[str, int] = field(default_factory=dict)
    evidence_required: int = 0

    def as_dict(self) -> dict:
        return {"caps": sorted(self.caps),
                "budgets": {k: self.budgets[k] for k in sorted(self.budgets)},
                "evidence_required": self.evidence_required}


@dataclass(frozen=True)
class PoolCharter:
    """A declared private pool: its identity, its ceiling, its ladder, and the
    operator authority view naming who may admit, revoke and attest.

    ``ceiling``/``ceiling_budgets`` are the MOST authority this pool will ever
    delegate to any member at any tier. Every tier grant is diffed against it,
    so the charter is a single fixed bound rather than a chain of rungs each
    trusting the one below.

    ``admit_key_ids``/``revoke_key_ids``/``attest_key_ids`` are the operator
    authority view, as non-secret ``attest.key_id`` fingerprints. Splitting them
    is the point: the operator who may admit a peer is not automatically the one
    who may sign the evidence that promotes it, so a single compromised admit
    key cannot walk a peer up the ladder on its own.

    ``artifact_digests`` are the candidate artifacts this pool admits, by hash.
    A join pins one; a digest not listed is refused.
    """

    pool_id: str
    ceiling: tuple[str, ...] = ()
    ceiling_budgets: Mapping[str, int] = field(default_factory=dict)
    tiers: Mapping[str, TierGrant] = field(default_factory=dict)
    admit_key_ids: tuple[str, ...] = ()
    revoke_key_ids: tuple[str, ...] = ()
    attest_key_ids: tuple[str, ...] = ()
    artifact_digests: tuple[str, ...] = ()
    trust_floor: str = "verified"
    join_window_s: int = 300
    identity_mode: str = MODE_MIXED

    def __post_init__(self) -> None:
        if not self.pool_id:
            raise PoolError("a pool charter needs a non-empty pool_id")
        peer_offer._trust_rank(self.trust_floor)
        for cap in self.ceiling:
            cap_order.parse_cap(cap)
        for name, amount in self.ceiling_budgets.items():
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise PoolError(
                    f"ceiling budget `{name}` is {amount!r}; a budget is a "
                    f"non-negative integer allowance")
        if ENTRY_TIER not in self.tiers:
            raise PoolError(
                f"a pool charter must declare its entry tier {ENTRY_TIER!r}; a "
                f"pool nobody can join at the bottom has no admission path")
        for name, grant in self.tiers.items():
            if name not in TIER_ORDER:
                raise PoolError(
                    f"unknown tier {name!r}; the ladder is "
                    f"{', '.join(TIER_ORDER)}")
            for cap in grant.caps:
                cap_order.parse_cap(cap)
        if self.join_window_s <= 0:
            raise PoolError(
                "join_window_s must be positive; a pool with no freshness "
                "window accepts a join record forever")
        if self.identity_mode not in IDENTITY_MODES:
            raise PoolError(
                f"unknown identity_mode {self.identity_mode!r}; a pool admits "
                f"one of {', '.join(IDENTITY_MODES)}. There is no default: a "
                f"pool whose identity backing was inferred is a pool nobody "
                f"decided the strength of")

    def ceiling_grant(self) -> peer_authority.Grant:
        """The pool's own authority, as the delegator every tier grant is
        diffed against."""
        return peer_authority.Grant(
            holder=f"pool:{self.pool_id}",
            caps=tuple(sorted(self.ceiling)),
            budgets=dict(self.ceiling_budgets))

    def body(self) -> dict:
        """The signed body: every member except the signature, in a shape whose
        canonical bytes are a pure function of the charter's content. Sequences
        are sorted so member order never changes the digest a join pins."""
        return {
            "kind": CHARTER_KIND,
            "version": CHARTER_VERSION,
            "pool_id": self.pool_id,
            "ceiling": sorted(self.ceiling),
            "ceiling_budgets": {k: self.ceiling_budgets[k]
                                for k in sorted(self.ceiling_budgets)},
            "tiers": {name: self.tiers[name].as_dict()
                      for name in sorted(self.tiers)},
            "admit_key_ids": sorted(self.admit_key_ids),
            "revoke_key_ids": sorted(self.revoke_key_ids),
            "attest_key_ids": sorted(self.attest_key_ids),
            "artifact_digests": sorted(self.artifact_digests),
            "trust_floor": self.trust_floor,
            "join_window_s": self.join_window_s,
            "identity_mode": self.identity_mode,
            "sign_alg": SIGN_ALG,
        }


def sign_charter(charter: PoolCharter, key: bytes) -> dict:
    """Sign a charter. Pure and deterministic given ``(charter, key)``: the same
    inputs always produce byte-identical output, which is what lets a peer pin
    the charter by digest and a verifier recompute it."""
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise PoolError("signing key must be non-empty bytes")
    body = charter.body()
    body["key_id"] = key_id(bytes(key))
    body[SIGNATURE_FIELD] = _mac(CHARTER_DOMAIN, body, key)
    return body


def _validate_charter_envelope(record: Mapping[str, Any]) -> str:
    """Is this authentic record a charter of the shape we accept? Returns a
    refusal reason, or ``""``. A MAC proves authorship, not that what was
    authored means what a reader assumes, so every fixed-meaning member is
    checked (mirrors ``peer_offer._validate_envelope``)."""
    for member, expected in (("kind", CHARTER_KIND),
                             ("version", CHARTER_VERSION),
                             ("sign_alg", SIGN_ALG)):
        if record.get(member) != expected:
            return (f"{member} is {record.get(member)!r}, expected "
                    f"{expected!r}")
    if not isinstance(record.get("pool_id"), str) or not record["pool_id"]:
        return f"pool_id is not a non-empty string ({record.get('pool_id')!r})"
    for member in ("ceiling", "admit_key_ids", "revoke_key_ids",
                   "attest_key_ids", "artifact_digests"):
        value = record.get(member)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return f"{member} is not a list of strings ({value!r})"
    tiers = record.get("tiers")
    if not isinstance(tiers, dict) or ENTRY_TIER not in tiers:
        return (f"tiers does not declare the entry tier {ENTRY_TIER!r} "
                f"({tiers!r})")
    if record.get("trust_floor") not in peer_offer.TRUST_ORDER and \
            record.get("trust_floor") not in peer_offer._TRUST_ALIASES:
        return f"trust_floor is {record.get('trust_floor')!r}"
    window = record.get("join_window_s")
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        return f"join_window_s is not a positive integer ({window!r})"
    if record.get("identity_mode") not in IDENTITY_MODES:
        return (f"identity_mode is {record.get('identity_mode')!r}, expected "
                f"one of {', '.join(IDENTITY_MODES)}")
    return ""


def verify_charter(record: Mapping[str, Any], key: bytes) -> tuple[bool, str]:
    """Check a signed charter. Returns ``(ok, reason)`` and NEVER raises.

    Order mirrors ``attest.verify_attestation`` and ``peer_offer.verify_offer``:
    prove authenticity FIRST, then validate the envelope, so nothing is read off
    a record whose author is not established."""
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False, "no verification key provided"
    if not isinstance(record, Mapping):
        return False, "charter is not an object"
    given = record.get(SIGNATURE_FIELD)
    if not isinstance(given, str):
        return False, "charter has no signature"
    try:
        expected = _mac(CHARTER_DOMAIN, record, key)
    except NotCanonicalizable as error:
        return False, f"charter cannot be verified: {error}"
    if not hmac.compare_digest(expected, given):
        return False, ("signature mismatch: wrong key, or the charter was "
                       "edited after signing")
    envelope = _validate_charter_envelope(record)
    if envelope:
        return False, f"envelope refused: {envelope}"
    return True, "valid: charter is authentic and well formed"


def charter_from_record(record: Mapping[str, Any]) -> PoolCharter:
    """Rebuild a :class:`PoolCharter` from a VERIFIED record. Call
    :func:`verify_charter` first; this trusts its input."""
    tiers = {name: TierGrant(caps=tuple(spec.get("caps", ())),
                             budgets=dict(spec.get("budgets", {})),
                             evidence_required=int(spec.get("evidence_required", 0)))
             for name, spec in record["tiers"].items()}
    return PoolCharter(
        pool_id=record["pool_id"],
        ceiling=tuple(record.get("ceiling", ())),
        ceiling_budgets=dict(record.get("ceiling_budgets", {})),
        tiers=tiers,
        admit_key_ids=tuple(record.get("admit_key_ids", ())),
        revoke_key_ids=tuple(record.get("revoke_key_ids", ())),
        attest_key_ids=tuple(record.get("attest_key_ids", ())),
        artifact_digests=tuple(record.get("artifact_digests", ())),
        trust_floor=record.get("trust_floor", "verified"),
        join_window_s=int(record.get("join_window_s", 300)),
        identity_mode=record.get("identity_mode", MODE_MIXED))


# ---------------------------------------------------------------------------
# the join request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JoinRequest:
    """A peer's request to join a charter, signed with the peer's own key.

    ``charter_digest`` pins the charter's canonical bytes. A peer signs up to a
    set of TERMS, not to a name: an operator who edits the ceiling and re-signs
    under the same ``pool_id`` produces a different digest, and every join
    against the old terms stops verifying. ``offer`` is the peer's signed
    :mod:`revl.peer_offer` record, carried INSIDE this signed body, so the
    ceiling the peer advertised cannot be swapped after the fact.
    ``artifact_digest`` pins the candidate the peer will run."""

    pool_id: str
    charter_digest: str
    peer_id: str
    offer: Mapping[str, Any]
    artifact_digest: str
    nonce: str
    issued_at: str

    def __post_init__(self) -> None:
        for name in ("pool_id", "charter_digest", "peer_id", "artifact_digest",
                     "nonce", "issued_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise PoolError(f"a join request needs a non-empty {name}")
        if not isinstance(self.offer, Mapping):
            raise PoolError("a join request carries a signed peer-offer object")

    def body(self) -> dict:
        return {
            "kind": JOIN_KIND,
            "version": JOIN_VERSION,
            "pool_id": self.pool_id,
            "charter_digest": self.charter_digest,
            "peer_id": self.peer_id,
            "offer": dict(self.offer),
            "artifact_digest": self.artifact_digest,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "sign_alg": SIGN_ALG,
        }


def sign_join_identity(join: JoinRequest, identity: PeerIdentity) -> dict:
    """Sign a join request with the peer's own ASYMMETRIC identity.

    The result is verifiable by ANY holder of the peer's public key. The
    :func:`sign_join` record is verifiable only by a holder of the peer's
    secret, and in a shared-key pool that set includes every party that could
    have forged it, which is why the two are not interchangeable and why the
    gate refuses to substitute one for the other."""
    body = join.body()
    body["sign_alg"] = SIGN_ALG_ECDSA
    return peer_identity.sign_record(JOIN_DOMAIN, body, identity)


def sign_join(join: JoinRequest, key: bytes) -> dict:
    """Sign a join request with the PEER's shared key (item 524's original
    backing). Authenticates; does not attribute. See
    :func:`sign_join_identity`."""
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise PoolError("signing key must be non-empty bytes")
    body = join.body()
    body["key_id"] = key_id(bytes(key))
    body[SIGNATURE_FIELD] = _mac(JOIN_DOMAIN, body, key)
    return body


def _validate_join_envelope(record: Mapping[str, Any]) -> str:
    for member, expected in (("kind", JOIN_KIND),
                             ("version", JOIN_VERSION)):
        if record.get(member) != expected:
            return f"{member} is {record.get(member)!r}, expected {expected!r}"
    if record.get("sign_alg") not in SIGN_ALGS:
        return (f"sign_alg is {record.get('sign_alg')!r}, expected one of "
                f"{', '.join(SIGN_ALGS)}")
    for member in ("pool_id", "charter_digest", "peer_id", "artifact_digest",
                   "nonce", "issued_at"):
        value = record.get(member)
        if not isinstance(value, str) or not value:
            return f"{member} is not a non-empty string ({value!r})"
    if not isinstance(record.get("offer"), Mapping):
        return f"offer is not an object ({record.get('offer')!r})"
    if _parse_iso(record.get("issued_at")) is None:
        return f"issued_at is not an ISO timestamp ({record.get('issued_at')!r})"
    return ""


def verify_join_identity(record: Mapping[str, Any],
                         public_key: bytes) -> tuple[bool, str]:
    """Check an asymmetrically signed join against a PINNED public key.

    ``(ok, reason)``; never raises. The key comes from the verifier's directory,
    never from the record: a verifier that took the key out of the record it is
    checking would accept anything an attacker signed with an attacker's key."""
    if not isinstance(record, Mapping):
        return False, "join request is not an object"
    ok, reason = peer_identity.verify_record(JOIN_DOMAIN, record, public_key)
    if not ok:
        return False, reason
    envelope = _validate_join_envelope(record)
    if envelope:
        return False, f"envelope refused: {envelope}"
    return True, "valid: join request is authentic under the pinned public key"


def verify_join(record: Mapping[str, Any], key: bytes) -> tuple[bool, str]:
    """Check a SHARED-KEY signed join request. ``(ok, reason)``; never raises.

    Refuses a record that names any other ``sign_alg`` before touching the MAC,
    so an asymmetric join can never be answered by the symmetric verifier even
    if a caller hands it one."""
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False, "no verification key provided"
    if not isinstance(record, Mapping):
        return False, "join request is not an object"
    if record.get("sign_alg") != SIGN_ALG:
        return False, (f"sign_alg is {record.get('sign_alg')!r}; this verifier "
                       f"checks {SIGN_ALG!r} and nothing else")
    given = record.get(SIGNATURE_FIELD)
    if not isinstance(given, str):
        return False, "join request has no signature"
    try:
        expected = _mac(JOIN_DOMAIN, record, key)
    except NotCanonicalizable as error:
        return False, f"join request cannot be verified: {error}"
    if not hmac.compare_digest(expected, given):
        return False, ("signature mismatch: wrong key, or the join request was "
                       "edited after signing")
    envelope = _validate_join_envelope(record)
    if envelope:
        return False, f"envelope refused: {envelope}"
    return True, "valid: join request is authentic and well formed"


# ---------------------------------------------------------------------------
# membership and the roster
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Membership:
    """One admitted peer: its tier, the grant that tier hands it, and the
    accumulated history that tier was earned with.

    Constructed ONLY by :func:`_issue_membership`, which is reachable only from
    a function that has already run :func:`_ceiling_precondition`. That is not a
    convention; ``tests/test_peer_pool_admission.py`` walks this module's AST and
    fails if it stops being true."""

    peer_id: str
    tier: str
    caps: tuple[str, ...]
    budgets: Mapping[str, int]
    artifact_digest: str
    admitted_at: str
    evidence: int = 0
    receipts: int = 0
    effects_witnessed: int = 0
    #: How this member proved who it is: `asymmetric` (a key pair, so its join
    #: is attributable to it by any holder of its public key) or `shared-key`
    #: (a MAC, so its join is attributable to anyone holding the shared secret).
    #: Recorded per MEMBER, not per pool, because during a migration a pool has
    #: both and the pool is as strong as its weakest member.
    identity: str = IDENTITY_SHARED_KEY
    #: The fingerprint of the key that signed the join. For an asymmetric member
    #: this is the public key a third party needs to re-check the ledger.
    key_id: str = ""

    def grant(self) -> peer_authority.Grant:
        return peer_authority.Grant(holder=f"peer:{self.peer_id}",
                                    caps=self.caps, budgets=dict(self.budgets))

    def as_dict(self) -> dict:
        return {"peer_id": self.peer_id, "tier": self.tier,
                "caps": sorted(self.caps),
                "budgets": {k: self.budgets[k] for k in sorted(self.budgets)},
                "artifact_digest": self.artifact_digest,
                "admitted_at": self.admitted_at, "evidence": self.evidence,
                "receipts": self.receipts,
                "effects_witnessed": self.effects_witnessed,
                "identity": self.identity, "key_id": self.key_id}


@dataclass(frozen=True)
class Withdrawal:
    """What a peer leaving does, in three disjoint words (item 546).

    ``revoked`` / ``revoked_budgets`` are restored: the peer's authority is
    again the authority of a peer that never joined, exactly. ``retained`` is
    not restored by anything and is not compensated either; it is the record of
    what happened, and it does not re-confer authority on a re-admission.
    ``orphaned`` is the outstanding work, which this module names and hands to
    ``lawful_retry.dispatch_on_loss`` rather than pretending to resolve."""

    peer_id: str
    reason: str
    at: str
    revoked: tuple[str, ...]
    revoked_budgets: Mapping[str, int]
    retained: Mapping[str, Any]
    orphaned: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"peer_id": self.peer_id, "reason": self.reason, "at": self.at,
                "revoked": sorted(self.revoked),
                "revoked_budgets": {k: self.revoked_budgets[k]
                                    for k in sorted(self.revoked_budgets)},
                "retained": dict(self.retained),
                "orphaned": sorted(self.orphaned)}


class Roster:
    """The pool's membership and its append-only event ledger.

    The ledger is the product surface `pool status` renders and the reason a
    withdrawal is legible: a member row says what a peer holds NOW, and the
    events say how it got there and what it left behind. Nothing is ever
    removed from ``events``; a withdrawal is an appended event, not a deletion,
    because deleting the admission would make a withdrawn peer indistinguishable
    from one that never joined and that is precisely the distinction item 546
    says must survive."""

    def __init__(self, pool_id: str, charter_digest: str):
        self.pool_id = pool_id
        self.charter_digest = charter_digest
        self.members: dict[str, Membership] = {}
        self.revoked: set[str] = set()
        self.spent_nonces: set[tuple[str, str]] = set()
        self.events: list[dict] = []
        self.outstanding: dict[str, list[str]] = {}

    # -- serialization ----------------------------------------------------

    def as_dict(self) -> dict:
        return {
            "kind": "revl.pool-roster",
            "version": "1.0",
            "pool_id": self.pool_id,
            "charter_digest": self.charter_digest,
            "members": {p: m.as_dict() for p, m in sorted(self.members.items())},
            "revoked": sorted(self.revoked),
            "spent_nonces": sorted([list(n) for n in self.spent_nonces]),
            "outstanding": {p: sorted(w) for p, w in sorted(self.outstanding.items())},
            "events": list(self.events),
        }

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> "Roster":
        roster = cls(record["pool_id"], record["charter_digest"])
        for peer, spec in record.get("members", {}).items():
            roster.members[peer] = Membership(
                peer_id=spec["peer_id"], tier=spec["tier"],
                caps=tuple(spec.get("caps", ())),
                budgets=dict(spec.get("budgets", {})),
                artifact_digest=spec.get("artifact_digest", ""),
                admitted_at=spec.get("admitted_at", ""),
                evidence=int(spec.get("evidence", 0)),
                receipts=int(spec.get("receipts", 0)),
                effects_witnessed=int(spec.get("effects_witnessed", 0)),
                identity=spec.get("identity", IDENTITY_SHARED_KEY),
                key_id=spec.get("key_id", ""))
        roster.revoked = set(record.get("revoked", ()))
        roster.spent_nonces = {tuple(n) for n in record.get("spent_nonces", ())}
        roster.outstanding = {p: list(w)
                              for p, w in record.get("outstanding", {}).items()}
        roster.events = list(record.get("events", ()))
        return roster

    def append(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def _refusal(link: str, reason: str, **extra) -> dict:
    """A refusal receipt. Always names a link, so a refusal is a mechanical
    check with a located cause and never a judgement call.

    There is deliberately no runtime assertion that ``link`` is in
    :data:`REFUSAL_LINKS`: this function is on the refusal path and the gate's
    contract is that it never raises, so an internal check here could turn a
    refusal into a crash on exactly the hostile input the refusal exists for.
    The link set is instead held STATICALLY. ``tests/test_peer_pool_admission``
    walks this module's AST and fails if any ``_refusal`` call names something
    outside :data:`REFUSAL_LINKS`, or if a link in it is never reached.

    The field set itself is ``deploy.refusal_receipt`` (issue #1332), which
    this module's own copy used to restate. The KIND and the VERSION stay this
    module's: a pool receipt and a deploy receipt are different records, signed
    in different domains so neither can be replayed as the other."""
    return deploy.refusal_receipt(RECEIPT_KIND, RECEIPT_VERSION, link, reason,
                                  **extra)


def _tier_grant(charter: PoolCharter, tier: str,
                peer_id: str) -> peer_authority.Grant:
    """The grant a tier hands a peer, read off the CHARTER and nothing else.

    Reachable only from :func:`_ceiling_precondition`, which is what makes the
    ceiling diff a precondition of holding a grant rather than a stage a grant
    passes through. An AST test in ``tests/test_peer_pool_admission.py`` holds
    that reachability."""
    spec = charter.tiers[tier]
    return peer_authority.Grant(holder=f"peer:{peer_id}",
                                caps=tuple(sorted(spec.caps)),
                                budgets=dict(spec.budgets))


def _ceiling_precondition(charter: PoolCharter, tier: str, peer_id: str,
                          offer_record: Optional[Mapping[str, Any]]
                          ) -> tuple[Optional[peer_authority.Grant], Optional[dict]]:
    """Compute the tier grant and PROVE it is admissible, in one step.

    Returns ``(grant, None)`` when the tier is issuable and ``(None, refusal)``
    when it is not. There is no ordering in which a caller holds the grant and
    has not run the diff, because the diff is what produces the grant.

    Two diffs, both fail-closed:

    * the tier grant against the CHARTER CEILING, via
      ``peer_authority.grant_widenings``: the pool never delegates authority it
      does not itself hold, and measuring against the charter rather than the
      tier below means an error in one rung cannot raise the ceiling for the
      rungs above it;
    * the tier grant against the PEER's own advertised ``grant_ceiling``, via
      ``cap_order.covers_set``: a peer is never handed more than it said it
      would accept, which is ``peer_offer``'s own seam to this invariant.
    """
    if tier not in charter.tiers:
        return None, _refusal(
            LINK_UNKNOWN_TIER,
            f"the charter declares no tier {tier!r}; it declares "
            f"{', '.join(sorted(charter.tiers)) or 'none'}")
    try:
        grant = _tier_grant(charter, tier, peer_id)
        widened_caps, widened_budgets = peer_authority.grant_widenings(
            charter.ceiling_grant(), grant)
    except (cap_order.CapError, ValueError) as error:
        return None, _refusal(
            LINK_GRANT_CEILING,
            f"the tier {tier!r} grant cannot be compared to the pool ceiling: "
            f"{error}")
    if widened_caps or widened_budgets:
        return None, _refusal(
            LINK_GRANT_CEILING,
            f"tier {tier!r} would hand the peer authority the pool does not "
            f"hold: {', '.join(widened_caps) or ''}"
            f"{'; ' if widened_caps and widened_budgets else ''}"
            f"{', '.join(f'{k}={g} over {h}' for k, h, g in widened_budgets)}",
            widened_caps=list(widened_caps),
            widened_budgets=[list(b) for b in widened_budgets])
    if offer_record is not None:
        try:
            advertised = [cap_order.parse_cap(c)
                          for c in offer_record.get("grant_ceiling", ())]
            beyond = cap_order.covers_set(advertised, grant.parsed_caps())
        except cap_order.CapError as error:
            return None, _refusal(
                LINK_GRANT_CEILING,
                f"the peer's advertised ceiling is unparseable: {error}")
        if beyond:
            return None, _refusal(
                LINK_GRANT_CEILING,
                f"tier {tier!r} would hand the peer more than its own "
                f"advertised ceiling accepts: "
                f"{', '.join(sorted(c.to_str() for c in beyond))}",
                beyond_offer_ceiling=sorted(c.to_str() for c in beyond))
    return grant, None


def _issue_membership(peer_id: str, tier: str, grant: peer_authority.Grant,
                      artifact_digest: str, at: str, *,
                      evidence: int = 0, receipts: int = 0,
                      effects_witnessed: int = 0,
                      identity: str = IDENTITY_SHARED_KEY,
                      key_id: str = "") -> Membership:
    """The single construction site of a :class:`Membership`. Takes the grant it
    is handed; it never computes one, so it cannot be reached with a grant the
    ceiling diff did not produce."""
    return Membership(peer_id=peer_id, tier=tier,
                      caps=tuple(sorted(grant.caps)), budgets=dict(grant.budgets),
                      artifact_digest=artifact_digest, admitted_at=at,
                      evidence=evidence, receipts=receipts,
                      effects_witnessed=effects_witnessed,
                      identity=identity, key_id=key_id)


def identity_backing(record: Mapping[str, Any]) -> Optional[str]:
    """Which identity backing a record's ``sign_alg`` names, or ``None``.

    ``None`` for anything not in :data:`_ALG_IDENTITY`, including a missing
    member and an unhashable one. Nothing is defaulted: a record whose backing
    could not be read is a record nobody knows the strength of, and the gate
    refuses it. The string check is not decoration: a hostile record can carry
    an object where a name belongs, and a dict lookup on it would raise on the
    refusal path, which is the one path that must not."""
    if not isinstance(record, Mapping):
        return None
    alg = record.get("sign_alg")
    if not isinstance(alg, str):
        return None
    return _ALG_IDENTITY.get(alg)


def admit(charter_record: Mapping[str, Any], join_record: Mapping[str, Any], *,
          charter_key: bytes, peer_keys: Optional[Mapping[str, bytes]] = None,
          admitting_key_id: str, roster: Roster,
          directory: Optional[IdentityDirectory] = None,
          now: Optional[datetime] = None) -> dict:
    """Decide whether a peer joins the pool, and at what tier.

    Returns a receipt: ``verdict: ADMIT`` with the issued tier and grant, or
    ``verdict: REFUSE`` naming one of :data:`REFUSAL_LINKS`. NEVER raises, because a
    hostile peer-supplied join record must not be able to break that contract,
    which is the same rule ``peer_offer.verify_offer`` holds and for the same
    reason: the wire is hostile.

    On ADMIT the roster is mutated: the member is added, the join nonce is
    spent, and the decision is appended to the event ledger. On REFUSE nothing
    is mutated except the nonce ledger is NOT touched, so a refused join can be
    retried after the operator fixes the cause without burning the peer's nonce.

    ``directory`` pins the peers' PUBLIC keys (issue #1278). ``peer_keys`` holds
    the shared secrets item 524 started with. A pool may be handed either or
    both, and the charter's ``identity_mode`` says which backings it admits;
    what no combination produces is a fallback. Each of the three checks below
    fails in the same direction:

    * a record whose ``sign_alg`` is unreadable or not admitted by the charter
      is refused (:data:`LINK_IDENTITY_MODE`), never read under a guess;
    * a peer the directory holds ANY key for, in any state, must present an
      asymmetric join; a shared-key join from it is refused
      (:data:`LINK_IDENTITY_DOWNGRADE`), which is what stops an attacker who
      learned a legacy secret from stepping the peer back onto it;
    * a signature that does not verify refuses on
      :data:`LINK_JOIN_SIGNATURE`, and a signature that verifies under a key
      that is revoked or rotated away from refuses on
      :data:`LINK_REVOKED_KEY`. The two are separate links because they are
      separate findings: one is a forgery, the other is a real act by a key
      that no longer acts.
    """
    when = now or _utc_now()
    peer_keys = dict(peer_keys or {})
    directory = directory if directory is not None else IdentityDirectory()

    ok, reason = verify_charter(charter_record, charter_key)
    if not ok:
        return _refusal(LINK_CHARTER_SIGNATURE, f"charter: {reason}")
    charter = charter_from_record(charter_record)
    charter_digest = canonical_digest(charter_record)

    # Arrow 2, before anything peer-supplied is parsed: an operator who may not
    # admit cannot use this gate as an oracle for whether some peer WOULD be
    # admitted.
    if admitting_key_id not in charter.admit_key_ids:
        return _refusal(
            LINK_ADMITTING_AUTHORITY,
            f"key {admitting_key_id} is not in the charter's admit authority "
            f"({', '.join(charter.admit_key_ids) or 'nobody'})")

    if not isinstance(join_record, Mapping):
        return _refusal(LINK_JOIN_SIGNATURE, "join request is not an object")
    claimed = join_record.get("peer_id")
    if not isinstance(claimed, str) or not claimed or (
            claimed not in peer_keys and not directory.has_identity(claimed)):
        return _refusal(
            LINK_UNKNOWN_PEER,
            f"no key is held for peer {claimed!r}; a private pool verifies a "
            f"peer against a key exchanged out of band, so an unknown peer has "
            f"no identity to check")

    # Which backing does this record claim, and does this charter admit it? Read
    # BEFORE any signature is checked, because choosing a verifier is the
    # decision, and a gate that tries verifiers until one passes is a gate an
    # attacker chooses the algorithm for.
    backing = identity_backing(join_record)
    if backing is None or backing not in MODE_ADMITS[charter.identity_mode]:
        return _refusal(
            LINK_IDENTITY_MODE,
            f"the join is signed {join_record.get('sign_alg')!r}; this pool's "
            f"identity mode is {charter.identity_mode!r}, which admits "
            f"{', '.join(MODE_ADMITS[charter.identity_mode])}",
            peer_id=claimed)

    # A peer that HAS an asymmetric identity keeps it. The directory is
    # consulted for any key in any state, not just an active one: a peer whose
    # key was revoked has not stopped being an asymmetric peer, and treating it
    # as one again would make revocation a route back to the weaker check.
    if backing == IDENTITY_SHARED_KEY and directory.has_identity(claimed):
        return _refusal(
            LINK_IDENTITY_DOWNGRADE,
            f"peer {claimed!r} has a pinned public key and presented a "
            f"{SIGN_ALG} join; an identity does not step back to a shared "
            f"secret because a record asked it to", peer_id=claimed)

    pinned = None
    if backing == IDENTITY_ASYMMETRIC:
        key_fingerprint = join_record.get("key_id")
        pinned = directory.lookup(claimed, key_fingerprint) \
            if isinstance(key_fingerprint, str) else None
        if pinned is None:
            return _refusal(
                LINK_UNKNOWN_KEY,
                f"the join names key {key_fingerprint!r}, which is not pinned "
                f"for peer {claimed!r}; a record does not introduce the key it "
                f"is checked under", peer_id=claimed)
        ok, reason = verify_join_identity(join_record, pinned.public_key)
        if not ok:
            return _refusal(LINK_JOIN_SIGNATURE, f"join: {reason}",
                            peer_id=claimed)
        status, status_reason = directory.authority(claimed, pinned.key_id,
                                                    when=when)
        if status != KEY_ACTIVE:
            # The signature IS genuine. Saying so in the refusal is the point:
            # the peer really signed this, and the key it signed with no longer
            # carries authority. Collapsing the two into one link would lose the
            # distinction the whole revocation story rests on.
            return _refusal(
                LINK_REVOKED_KEY,
                f"the join verifies under key {pinned.key_id}, so the peer did "
                f"sign it, and that key confers no authority now: "
                f"{status_reason}", peer_id=claimed, key_status=status,
                key_id=pinned.key_id)
    else:
        if claimed not in peer_keys:
            return _refusal(
                LINK_UNKNOWN_PEER,
                f"no shared key is held for peer {claimed!r}",
                peer_id=claimed)
        ok, reason = verify_join(join_record, peer_keys[claimed])
        if not ok:
            return _refusal(LINK_JOIN_SIGNATURE, f"join: {reason}",
                            peer_id=claimed)

    if join_record["pool_id"] != charter.pool_id:
        return _refusal(
            LINK_POOL_IDENTITY,
            f"the join names pool {join_record['pool_id']!r}, this charter is "
            f"{charter.pool_id!r}", peer_id=claimed)
    if join_record["charter_digest"] != charter_digest:
        return _refusal(
            LINK_POOL_IDENTITY,
            f"the join agreed to charter {join_record['charter_digest'][:16]}, "
            f"these terms are {charter_digest[:16]}: a peer signs up to a set "
            f"of terms, not to a pool name", peer_id=claimed)

    issued = _parse_iso(join_record["issued_at"])
    if issued is None or abs((when - issued).total_seconds()) > charter.join_window_s:
        return _refusal(
            LINK_STALE_JOIN,
            f"the join was issued at {join_record['issued_at']} and the "
            f"charter's window is {charter.join_window_s}s", peer_id=claimed)
    if (claimed, join_record["nonce"]) in roster.spent_nonces:
        return _refusal(
            LINK_REPLAYED_JOIN,
            f"the roster has already spent nonce {join_record['nonce']!r} for "
            f"peer {claimed!r}", peer_id=claimed)

    if claimed in roster.revoked:
        return _refusal(
            LINK_REVOKED_PEER,
            f"peer {claimed!r} was withdrawn from this pool and its key is "
            f"revoked; re-admission needs a fresh identity", peer_id=claimed)
    if claimed in roster.members:
        return _refusal(
            LINK_DUPLICATE_MEMBER,
            f"peer {claimed!r} is already a member at tier "
            f"{roster.members[claimed].tier!r}", peer_id=claimed)

    offer_record = join_record["offer"]
    # The offer is signed separately and must be signed the SAME way, by the
    # same key. An asymmetric join carrying a shared-key offer would leave the
    # advertised ceiling -- the member the grant is diffed against -- provable
    # only to a secret holder, which is half a migration and reads as a whole
    # one.
    if identity_backing(offer_record) != backing:
        return _refusal(
            LINK_IDENTITY_DOWNGRADE,
            f"the join is signed {join_record.get('sign_alg')!r} and carries "
            f"an offer signed "
            f"{offer_record.get('sign_alg') if isinstance(offer_record, Mapping) else None!r}; "
            f"both halves of a peer's claim are backed the same way or neither "
            f"is", peer_id=claimed)
    if backing == IDENTITY_ASYMMETRIC:
        ok, reason = peer_offer.verify_offer_identity(offer_record,
                                                      pinned.public_key)
    else:
        ok, reason = peer_offer.verify_offer(offer_record, peer_keys[claimed])
    if not ok:
        return _refusal(LINK_OFFER_SIGNATURE, f"peer offer: {reason}",
                        peer_id=claimed)
    if offer_record.get("peer_id") != claimed:
        return _refusal(
            LINK_OFFER_IDENTITY,
            f"the join is from {claimed!r} but carries an offer from "
            f"{offer_record.get('peer_id')!r}", peer_id=claimed)

    try:
        offered = peer_offer._trust_rank(offer_record["attestation"]["trust"])
        floor = peer_offer._trust_rank(charter.trust_floor)
    except (peer_offer.OfferError, KeyError, TypeError):
        return _refusal(
            LINK_TRUST_FLOOR,
            "the offer carries no readable trust level", peer_id=claimed)
    if offered < floor:
        return _refusal(
            LINK_TRUST_FLOOR,
            f"the peer attests trust {offer_record['attestation']['trust']!r}, "
            f"the charter's floor is {charter.trust_floor!r}", peer_id=claimed)

    if join_record["artifact_digest"] not in charter.artifact_digests:
        return _refusal(
            LINK_ARTIFACT_DIGEST,
            f"the join pins artifact {join_record['artifact_digest'][:16]}, "
            f"which this pool does not admit", peer_id=claimed)

    # Arrow 8. A join issues the entry tier, full stop. Nothing a peer writes in
    # a join record reaches a higher rung; that is `promote`'s job and it needs
    # evidence a peer cannot sign for itself.
    requested = join_record.get("tier", ENTRY_TIER)
    if requested != ENTRY_TIER:
        return _refusal(
            LINK_ENTRY_TIER,
            f"a join issues the entry tier {ENTRY_TIER!r}; a peer cannot join "
            f"straight into {requested!r}", peer_id=claimed)

    grant, refusal = _ceiling_precondition(charter, ENTRY_TIER, claimed,
                                           offer_record)
    if refusal is not None:
        refusal["peer_id"] = claimed
        return refusal

    member = _issue_membership(claimed, ENTRY_TIER, grant,
                               join_record["artifact_digest"], _iso(when),
                               identity=backing,
                               key_id=pinned.key_id if pinned is not None
                               else str(join_record.get("key_id", "")))
    roster.members[claimed] = member
    roster.spent_nonces.add((claimed, join_record["nonce"]))
    receipt = {
        "kind": RECEIPT_KIND, "version": RECEIPT_VERSION, "verdict": ADMIT,
        "pool_id": charter.pool_id, "charter_digest": charter_digest,
        "peer_id": claimed, "tier": ENTRY_TIER,
        "caps": sorted(grant.caps),
        "budgets": {k: grant.budgets[k] for k in sorted(grant.budgets)},
        "artifact_digest": join_record["artifact_digest"],
        "admitted_by": admitting_key_id, "at": _iso(when),
        "effect_ceiling": TIER_EFFECT_CEILING[ENTRY_TIER].value,
        "identity": member.identity, "key_id": member.key_id,
    }
    roster.append(receipt)
    return receipt


def promote(charter_record: Mapping[str, Any], peer_id: str, tier: str, *,
            charter_key: bytes, roster: Roster,
            evidence_key_ids: Sequence[str],
            now: Optional[datetime] = None) -> dict:
    """Raise a member's tier, if and only if the evidence for that tier exists.

    ``evidence_key_ids`` are the key fingerprints that signed the member's
    accumulated execution receipts. Every one of them must be in the charter's
    ``attest_key_ids``: a peer cannot attest its own promotion, and neither can
    an operator who holds only admit authority.

    The failure direction is the one every arrow here takes. A member with too
    little evidence, or with evidence signed by a key the charter does not name
    for attesting, STAYS WHERE IT IS. It does not inherit the tier it asked for,
    and it is not demoted either; a missing proof is not a violation.

    Like :func:`admit`, the tier grant is diffed against the charter ceiling and
    the diff is what produces the grant."""
    when = now or _utc_now()

    ok, reason = verify_charter(charter_record, charter_key)
    if not ok:
        return _refusal(LINK_CHARTER_SIGNATURE, f"charter: {reason}")
    charter = charter_from_record(charter_record)

    member = roster.members.get(peer_id)
    if member is None:
        return _refusal(
            LINK_NOT_A_MEMBER,
            f"peer {peer_id!r} is not a member of pool {charter.pool_id!r}",
            peer_id=peer_id)
    if tier not in charter.tiers:
        return _refusal(
            LINK_UNKNOWN_TIER,
            f"the charter declares no tier {tier!r}", peer_id=peer_id)

    # The ceiling diff runs HERE, ahead of the evidence threshold, and that
    # order is the point. `member.evidence` is the count of attested execution
    # receipts the peer accumulated while it worked, which is an observation of
    # what it did; the ceiling diff is a property of the charter and the tier
    # and needs no observation at all. Item 543 (issue #1222) is the rule that
    # the authority diff gates ENTRY to the stages that read measured evidence
    # rather than being weighed after them, so a tier the pool cannot lawfully
    # issue is refused without the peer's record ever being read. The call
    # depends only on `(charter, tier, peer_id)`, so nothing about the grant it
    # computes changes by standing earlier.
    grant, refusal = _ceiling_precondition(charter, tier, peer_id, None)
    if refusal is not None:
        refusal["peer_id"] = peer_id
        return refusal

    outside = sorted(set(evidence_key_ids) - set(charter.attest_key_ids))
    if outside:
        return _refusal(
            LINK_PROMOTION_EVIDENCE,
            f"evidence signed by {', '.join(outside)}, which the charter does "
            f"not name as attesting authority "
            f"({', '.join(charter.attest_key_ids) or 'nobody'})",
            peer_id=peer_id)

    required = charter.tiers[tier].evidence_required
    if member.evidence < required:
        return _refusal(
            LINK_PROMOTION_EVIDENCE,
            f"tier {tier!r} requires {required} attested receipts and "
            f"{peer_id!r} has {member.evidence}; it stays at "
            f"{member.tier!r}", peer_id=peer_id,
            stays_at=member.tier, has=member.evidence, requires=required)

    promoted = _issue_membership(
        peer_id, tier, grant, member.artifact_digest, member.admitted_at,
        evidence=member.evidence, receipts=member.receipts,
        effects_witnessed=member.effects_witnessed,
        identity=member.identity, key_id=member.key_id)
    roster.members[peer_id] = promoted
    receipt = {
        "kind": RECEIPT_KIND, "version": RECEIPT_VERSION, "verdict": PROMOTE,
        "pool_id": charter.pool_id, "peer_id": peer_id,
        "from_tier": member.tier, "tier": tier,
        "caps": sorted(grant.caps),
        "budgets": {k: grant.budgets[k] for k in sorted(grant.budgets)},
        "evidence": member.evidence, "at": _iso(when),
        "effect_ceiling": TIER_EFFECT_CEILING[tier].value,
    }
    roster.append(receipt)
    return receipt


def withdraw(charter_record: Mapping[str, Any], peer_id: str, reason: str, *,
             charter_key: bytes, roster: Roster,
             revoking_key_id: Optional[str] = None,
             revoking_identity: Optional[PeerIdentity] = None,
             directory: Optional[IdentityDirectory] = None,
             now: Optional[datetime] = None) -> dict:
    """Remove a peer and state, in three words, what that does.

    ``revoking_key_id`` must be in the charter's ``revoke_key_ids``: the
    operator authority view is not decoration, and the key that may admit is not
    automatically the key that may revoke.

    ``revoking_identity`` signs the withdrawal receipt asymmetrically, so a
    third party holding the operator's revoke public key can check who removed
    whom without asking the operator that did the removing. Passing it also
    supplies ``revoking_key_id``, which removes the way the two could disagree.

    ``directory`` is revoked against: the withdrawn peer's pinned key is marked
    revoked at this instant. What that does and does not do is the item-546
    split again, and the receipt says both halves. It removes the key's
    AUTHORITY, which has an inverse and is now exercised. It does not remove the
    key's ability to VERIFY: every join, offer and receipt the peer signed is
    still checkable by anyone holding the public half, which is the property a
    shared-key pool could not offer at all, because there revoking the key
    destroyed the only means of checking the history.

    The returned receipt carries a :class:`Withdrawal`'s three disjoint sets.
    The member row disappears from the roster; the ADMISSION EVENT DOES NOT.
    That is the item-546 point made operationally: a withdrawn peer and a peer
    that never joined must not render the same, because the first one ran work
    whose effects are still in the world."""
    when = now or _utc_now()
    if revoking_identity is not None:
        revoking_key_id = revoking_identity.key_id

    ok, verify_reason = verify_charter(charter_record, charter_key)
    if not ok:
        return _refusal(LINK_CHARTER_SIGNATURE, f"charter: {verify_reason}")
    charter = charter_from_record(charter_record)

    if revoking_key_id not in charter.revoke_key_ids:
        return _refusal(
            LINK_ADMITTING_AUTHORITY,
            f"key {revoking_key_id} is not in the charter's revoke authority "
            f"({', '.join(charter.revoke_key_ids) or 'nobody'})",
            peer_id=peer_id)
    member = roster.members.get(peer_id)
    if member is None:
        return _refusal(
            LINK_NOT_A_MEMBER,
            f"peer {peer_id!r} is not a member of pool {charter.pool_id!r}",
            peer_id=peer_id)

    withdrawal = Withdrawal(
        peer_id=peer_id, reason=reason, at=_iso(when),
        # RESTORED. After this the peer's authority is exactly the authority of a
        # peer that never joined: an inverse exists and this is it.
        revoked=tuple(sorted(member.caps)),
        revoked_budgets=dict(member.budgets),
        # NOT RESTORED, and not compensated either. These are observations. They
        # stay in the ledger and they do not re-confer authority: a re-admitted
        # identity starts again at the entry tier.
        retained={
            "receipts": member.receipts,
            "evidence": member.evidence,
            "effects_witnessed": member.effects_witnessed,
            "tier_reached": member.tier,
            "artifact_digest": member.artifact_digest,
            "admitted_at": member.admitted_at,
            "identity": member.identity,
            "key_id": member.key_id,
            "signatures_verifiable": member.identity == IDENTITY_ASYMMETRIC,
            "note": ("withdrawal revokes authority; it does not un-observe the "
                     "work the peer already delivered, and the evidence below "
                     "is history, not a credential that survives removal"),
            "identity_note": (
                "the peer's key is revoked: it confers no authority and its "
                "past signatures still verify under the public key above, so "
                "what it signed remains attributable to it"
                if member.identity == IDENTITY_ASYMMETRIC else
                "this member was admitted on a shared key, so what it signed "
                "is attributable to any holder of that secret and revoking it "
                "leaves nothing a third party can re-check"),
        },
        # NEITHER. Named and handed on, because deciding replay versus
        # compensate is `lawful_retry.dispatch_on_loss`'s job and duplicating
        # that decision here would be the second, weaker policy engine this item
        # exists to avoid.
        orphaned=tuple(sorted(roster.outstanding.get(peer_id, ()))))

    del roster.members[peer_id]
    roster.revoked.add(peer_id)
    roster.outstanding.pop(peer_id, None)
    # EVERY key the peer holds, not only the one that signed its join. A peer
    # that rotated after joining would otherwise leave the pool with an active
    # key still pinned, which reads in `pool status` as an identity that may
    # still act.
    keys_revoked = []
    if directory is not None:
        for pinned in list(directory.keys.get(peer_id, ())):
            if pinned.status == peer_identity.KEY_REVOKED:
                continue
            keys_revoked.append(directory.revoke(
                peer_id, pinned.key_id,
                reason=f"withdrawn from pool {charter.pool_id}: {reason}",
                at=when).key_id)
    receipt = {
        "kind": RECEIPT_KIND, "version": RECEIPT_VERSION, "verdict": WITHDRAW,
        "pool_id": charter.pool_id, "revoked_by": revoking_key_id,
        "keys_revoked": sorted(keys_revoked),
        **withdrawal.as_dict(),
    }
    if revoking_identity is not None:
        receipt = peer_identity.sign_record(WITHDRAWAL_DOMAIN, receipt,
                                            revoking_identity)
    roster.append(receipt)
    return receipt


def verify_withdrawal(record: Mapping[str, Any],
                      public_key: bytes) -> tuple[bool, str]:
    """Check a withdrawal receipt against the operator's revoke PUBLIC key.

    This is the surface the exit criterion names: a withdrawal any holder of the
    public key can verify. A withdrawal receipt that carries no asymmetric
    signature is refused here rather than waved through, because "unsigned" and
    "signed by somebody I cannot name" must not render the same."""
    return peer_identity.verify_record(WITHDRAWAL_DOMAIN, record, public_key)


def work_admissible(member: Membership,
                    effect_class: EffectClass) -> tuple[bool, str]:
    """May this member be SENT work of this effect class?

    Keyed on ``lawful_retry.EffectClass``, the classification the dispatcher
    already consults, so a pool tier is a bound on an existing taxonomy rather
    than a new one. Fail-closed in both directions: an effect class no tier
    admits is refused with the reason it is unreachable, and an effect class
    nothing here has heard of is refused rather than defaulted."""
    if effect_class in UNREACHABLE_EFFECTS:
        return False, (f"no pool tier admits {effect_class.value!r}: "
                       f"{UNREACHABLE_EFFECTS[effect_class]}")
    if effect_class not in _EFFECT_RANK:
        return False, (f"unknown effect class {effect_class!r}; a pool tier "
                       f"admits only classes it has been shown")
    ceiling = TIER_EFFECT_CEILING.get(member.tier)
    if ceiling is None:
        return False, f"member is at unknown tier {member.tier!r}"
    if _EFFECT_RANK[effect_class] > _EFFECT_RANK[ceiling]:
        return False, (f"tier {member.tier!r} admits {ceiling.value!r} and "
                       f"below; {effect_class.value!r} needs a higher tier")
    return True, (f"tier {member.tier!r} admits {effect_class.value!r}")


# ---------------------------------------------------------------------------
# the CLI: `revl pool init | join | status | withdraw`
# ---------------------------------------------------------------------------

CHARTER_FILE = "charter.json"
ROSTER_FILE = "roster.json"
#: The pinned PUBLIC halves. A separate file from the roster because it has a
#: different lifetime: a peer leaves the roster and its key stays here, revoked,
#: so the ledger it signed is still checkable.
IDENTITIES_FILE = "identities.json"


def _read_json(path) -> Any:
    from pathlib import Path  # noqa: PLC0415 (lazy)

    with open(Path(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path, payload) -> None:
    from pathlib import Path  # noqa: PLC0415

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def load_pool(directory) -> tuple[dict, Roster]:
    """Read a pool directory: its signed charter and its roster."""
    from pathlib import Path  # noqa: PLC0415

    base = Path(directory)
    charter_record = _read_json(base / CHARTER_FILE)
    roster_path = base / ROSTER_FILE
    if roster_path.exists():
        return charter_record, Roster.from_dict(_read_json(roster_path))
    return charter_record, Roster(charter_record["pool_id"],
                                  canonical_digest(charter_record))


def save_roster(directory, roster: Roster) -> None:
    from pathlib import Path  # noqa: PLC0415

    _write_json(Path(directory) / ROSTER_FILE, roster.as_dict())


def load_directory(pool_dir) -> IdentityDirectory:
    """The pool's pinned public keys. A pool with no identities file has an
    EMPTY directory, not an absent one: every peer is then unknown to the
    asymmetric path, which is the fail-closed reading."""
    from pathlib import Path  # noqa: PLC0415

    path = Path(pool_dir) / IDENTITIES_FILE
    if not path.exists():
        return IdentityDirectory()
    return IdentityDirectory.from_dict(_read_json(path))


def save_directory(pool_dir, directory: IdentityDirectory) -> None:
    from pathlib import Path  # noqa: PLC0415

    _write_json(Path(pool_dir) / IDENTITIES_FILE, directory.as_dict())


def identity_census(roster: Roster) -> dict:
    """How each member proved who it is, counted.

    The reason this is a report and not a flag: during a migration a pool holds
    both backings, and a pool is as strong as its weakest member. A deployment
    that shows one word for the whole pool hides exactly the member an attacker
    would go for."""
    counts = {IDENTITY_ASYMMETRIC: 0, IDENTITY_SHARED_KEY: 0}
    for member in roster.members.values():
        counts[member.identity] = counts.get(member.identity, 0) + 1
    weakest = IDENTITY_SHARED_KEY if counts.get(IDENTITY_SHARED_KEY) \
        else (IDENTITY_ASYMMETRIC if counts.get(IDENTITY_ASYMMETRIC) else "")
    return {
        "counts": counts,
        "weakest": weakest,
        "mixed": bool(counts.get(IDENTITY_ASYMMETRIC)
                      and counts.get(IDENTITY_SHARED_KEY)),
        "shared_key_members": sorted(
            peer for peer, m in roster.members.items()
            if m.identity == IDENTITY_SHARED_KEY),
    }


def render_status(charter_record: Mapping[str, Any], roster: Roster,
                  directory: Optional[IdentityDirectory] = None) -> str:
    """The operator view: who is in, at what tier, holding what, the authority
    view naming who may admit, revoke and attest, and HOW EACH MEMBER PROVED WHO
    IT IS.

    The identity column is not decoration. A pool in the middle of the move to
    key pairs has members of both kinds, and the one line that matters is the
    one naming the members whose joins are still forgeable by a secret
    holder."""
    charter = charter_from_record(charter_record)
    census = identity_census(roster)
    lines = [f"pool {charter.pool_id}",
             f"  charter   {canonical_digest(charter_record)[:16]}",
             f"  ceiling   {', '.join(sorted(charter.ceiling)) or '(none)'}",
             f"  floor     trust >= {charter.trust_floor}",
             "  authority",
             f"    admit   {', '.join(charter.admit_key_ids) or '(nobody)'}",
             f"    revoke  {', '.join(charter.revoke_key_ids) or '(nobody)'}",
             f"    attest  {', '.join(charter.attest_key_ids) or '(nobody)'}",
             f"  identity  mode={charter.identity_mode} "
             f"{IDENTITY_ASYMMETRIC}={census['counts'][IDENTITY_ASYMMETRIC]} "
             f"{IDENTITY_SHARED_KEY}={census['counts'][IDENTITY_SHARED_KEY]}"]
    if census["mixed"]:
        lines.append(
            f"    WEAKEST LINK: {', '.join(census['shared_key_members'])} "
            f"still join under a shared key, so their joins are forgeable by "
            f"any holder of that secret")
    lines.append(f"  members   {len(roster.members)}")
    for peer_id, member in sorted(roster.members.items()):
        ceiling = TIER_EFFECT_CEILING.get(member.tier)
        lines.append(
            f"    {peer_id}  tier={member.tier} "
            f"effects<={ceiling.value if ceiling else '?'} "
            f"evidence={member.evidence} "
            f"identity={member.identity}"
            f"{'/' + member.key_id if member.key_id else ''} "
            f"caps={', '.join(sorted(member.caps)) or '(none)'}")
    if roster.revoked:
        lines.append(f"  withdrawn {', '.join(sorted(roster.revoked))}")
    if directory is not None and directory.keys:
        lines.append("  keys")
        for row in directory.summary():
            lines.append(
                f"    {row['peer_id']}  active={row['active_key_id'] or '(none)'} "
                f"status={row['status']} superseded={row['superseded']} "
                f"revoked={row['revoked']}")
    lines.append(f"  events    {len(roster.events)}")
    return "\n".join(lines)


def pool_command(args) -> int:
    """`revl pool`: stand a private pool up, join a peer to it, read its
    membership, withdraw a peer.

    Every verb prints a receipt and exits nonzero on a refusal, so an operator
    script reads the exit status and an operator reads the named link."""
    from .attest import resolve_key  # noqa: PLC0415 (lazy)
    from .errors import RevlError  # noqa: PLC0415

    import sys  # noqa: PLC0415

    verb = args.pool_command

    # Reading the roster needs no key. That is the point of the ledger being a
    # product surface: an operator inspects membership without touching the
    # secret that admits, so `pool status` is safe to put in a dashboard or a
    # health check.
    if verb == "status":
        charter_record, roster = load_pool(args.dir)
        identities = load_directory(args.dir)
        if getattr(args, "json", False):
            print(json.dumps({"charter": charter_record,
                              "roster": roster.as_dict(),
                              "identities": identities.as_dict(),
                              "identity_census": identity_census(roster)},
                             indent=2, sort_keys=True))
        else:
            print(render_status(charter_record, roster, identities))
        return 0

    # `keygen` is the PEER's first step and touches no pool: it draws a key pair
    # on the machine that will hold it. The private half is written 0600 and is
    # never read by any other verb here; the public half is the file the peer
    # hands the operator out of band.
    if verb == "keygen":
        identity = peer_identity.generate_identity(args.peer_id)
        peer_identity.write_private_identity(args.out, identity)
        peer_identity.write_public_identity(args.public, identity.public())
        print(f"peer {args.peer_id} key {identity.key_id}\n"
              f"  private  {args.out} (mode 0600, never copy it anywhere)\n"
              f"  public   {args.public} (hand this to the operator and have "
              f"them check the fingerprint above over a second channel)")
        return 0

    if verb == "register":
        identities = load_directory(args.dir)
        public = peer_identity.load_public_identity(args.public)
        pinned = identities.register(public)
        save_directory(args.dir, identities)
        print(f"pinned {pinned.key_id} for peer {pinned.peer_id} "
              f"at {pinned.not_before}")
        return 0

    if verb == "rotate":
        identities = load_directory(args.dir)
        public = peer_identity.load_public_identity(args.public)
        old, new = identities.rotate(public, reason=args.reason)
        save_directory(args.dir, identities)
        print(f"peer {public.peer_id}: active key is now {new.key_id}\n"
              f"  superseded {old.key_id if old else '(none)'} "
              f"-- it still verifies everything it signed and authorises "
              f"nothing from now on")
        return 0

    if verb == "revoke-key":
        identities = load_directory(args.dir)
        revoked = identities.revoke(args.peer_id, args.key_id,
                                    reason=args.reason)
        save_directory(args.dir, identities)
        print(f"revoked {revoked.key_id} for peer {revoked.peer_id} "
              f"at {revoked.not_after}\n"
              f"  it confers no authority; the records it signed are still "
              f"verifiable under its public half, so the history stays "
              f"checkable")
        return 0

    # An identity key pair, when one was given, is what signs. The shared key is
    # resolved only when it is not, and the two are never both used for one
    # record: a record has one backing and the gate refuses a pair that mixes
    # them.
    identity = None
    identity_path = getattr(args, "identity_key", None)
    if identity_path:
        try:
            identity = peer_identity.load_private_identity(identity_path)
        except (OSError, ValueError, peer_identity.IdentityError) as error:
            print(f"error: {identity_path}: {error}", file=sys.stderr)
            return 2

    key = b""
    if identity is None:
        try:
            key = resolve_key(getattr(args, "key", None))
        except RevlError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    if verb == "init":
        if not key:
            try:
                key = resolve_key(getattr(args, "key", None))
            except RevlError as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
        charter = PoolCharter(
            pool_id=args.pool_id,
            ceiling=tuple(args.ceiling or ()),
            tiers={ENTRY_TIER: TierGrant(caps=tuple(args.entry_caps or ()))},
            admit_key_ids=(key_id(key),),
            revoke_key_ids=(key_id(key),) + tuple(
                peer_identity.load_public_identity(path).key_id
                for path in (args.revoke_identity or ())),
            attest_key_ids=(key_id(key),),
            artifact_digests=tuple(args.artifact or ()),
            trust_floor=args.trust_floor,
            identity_mode=args.identity)
        record = sign_charter(charter, key)
        _write_json(f"{args.dir}/{CHARTER_FILE}", record)
        roster = Roster(charter.pool_id, canonical_digest(record))
        save_roster(args.dir, roster)
        identities = load_directory(args.dir)
        save_directory(args.dir, identities)
        print(render_status(record, roster, identities))
        return 0

    if verb == "request":
        # The PEER side. It reads the charter it was handed and pins its digest,
        # so the terms it agreed to are the terms the gate checks it against.
        import secrets  # noqa: PLC0415

        charter_record = _read_json(args.charter)
        offer = peer_offer.PeerOffer(
            peer_id=args.peer_id,
            attestation=peer_offer.Attestation(
                trust=args.trust, region=args.region, hardware=args.hardware),
            grant_ceiling=tuple(args.ceiling or ()))
        # Both halves are backed the same way. The gate refuses a join whose
        # offer is signed differently, so producing such a pair here would only
        # move the refusal later.
        if identity is not None:
            signed_offer = peer_offer.sign_offer_identity(offer, identity)
        else:
            signed_offer = peer_offer.sign_offer(offer, key)
        join = JoinRequest(
            pool_id=charter_record["pool_id"],
            charter_digest=canonical_digest(charter_record),
            peer_id=args.peer_id,
            offer=signed_offer,
            artifact_digest=args.artifact,
            nonce=secrets.token_hex(16),
            issued_at=_iso(_utc_now()))
        if identity is not None:
            record = sign_join_identity(join, identity)
        else:
            record = sign_join(join, key)
        _write_json(args.out, record)
        named_key = record.get("key_id", "")
        print(f"wrote join request for {args.peer_id} against charter "
              f"{join.charter_digest[:16]} to {args.out}\n"
              f"  backing  {record['sign_alg']} key {named_key}")
        return 0

    # `join` and `withdraw` are OPERATOR verbs and the charter is signed with
    # the operator's symmetric key, so that key is resolved for them whether or
    # not an identity was also given.
    if not key:
        try:
            key = resolve_key(getattr(args, "key", None))
        except RevlError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    charter_record, roster = load_pool(args.dir)
    identities = load_directory(args.dir)

    if verb == "join":
        join_record = _read_json(args.join)
        peer_keys = {}
        if getattr(args, "peer_key", None):
            peer_keys = {join_record.get("peer_id", ""):
                         resolve_key(args.peer_key)}
        receipt = admit(charter_record, join_record, charter_key=key,
                        peer_keys=peer_keys, directory=identities,
                        admitting_key_id=key_id(key), roster=roster)
    elif verb == "withdraw":
        receipt = withdraw(charter_record, args.peer, args.reason,
                           charter_key=key, roster=roster,
                           directory=identities,
                           revoking_identity=identity,
                           revoking_key_id=None if identity is not None
                           else key_id(key))
    else:  # pragma: no cover - argparse constrains the verb set
        raise AssertionError(f"unknown pool verb {verb!r}")

    print(json.dumps(receipt, indent=2, sort_keys=True))
    if receipt["verdict"] == REFUSE:
        return 1
    save_roster(args.dir, roster)
    save_directory(args.dir, identities)
    return 0

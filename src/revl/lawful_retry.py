"""Lawful-retry dispatcher for the verifiable private peer pool (#480,
Primitive 2).

When a peer is LOST mid-execution — it dropped off the pool, timed out, or its
attempt failed — something must decide what to do with the work it was holding:
re-issue it to another peer (REPLAY), run the recorded inverse (COMPENSATE),
coordinate a durable handoff (WITNESSED work), or refuse to speculate at all
(irreversible commits). revl already OWNS the classification that answer keys
off — extern tiers and compensation classes (docs/design/243-witnessed-externs,
247-compensate). The dispatcher does not invent a taxonomy; it CONSULTS that one
and decides replay-vs-compensate per effect class:

===========================  ===============================================
effect class                 on peer loss
===========================  ===============================================
pure                         freely replayable to any eligible peer
idempotent-external          bounded retry (budget from Primitive 3)
witnessed / revertible       durable handoff + recovery coordination (WAL)
deferred-irreversible        only at a trusted commit authority; never
                             speculatively re-issued
secret-bearing               trusted/local/attested peers only (never a bare
                             verified offer)
===========================  ===============================================

Where the two other primitives bracket it
------------------------------------------
The dispatcher adds COORDINATION, not new authority, and it is bounded on both
sides by machinery that already exists:

* Every REPLAY is a NEW HOP in the delegation/retry chain, so it is passed
  through ``peer_authority.check_delegation_chain``: a retry may NARROW the lost
  attempt's grant, never widen it. The retry budget is itself one of the monotone
  quantities, so an unbounded retry storm is a static impossibility — the budget
  runs out down the chain. That is Primitive 3 holding the replay side.
* Every peer a REPLAY is offered to must clear ``peer_offer.offer_eligible``:
  a verified signature, satisfied facets, and a ``grant_ceiling`` that covers the
  retry grant. That is Primitive 1 holding the peer-selection side.
* Every COMPENSATE runs an inverse already recorded in the accumulator/WAL
  (``recovery.py``'s roll-back path), which by construction touches only
  boundaries the original crossing held — so it needs no new authority and is not
  modeled here beyond dispatching TO it.

Honest limits (what this dispatcher does NOT claim)
---------------------------------------------------
It decides a DISPOSITION and, for a replay, constructs and monotonicity-checks
the retry hop and selects an eligible peer. It does not perform transport (that
needs #421, the F8 network seam), it does not itself run a compensation (that is
``recovery.py``), and it meters the retry budget only as the opaque scalar
Primitive 3 bounds — not as a real rate limiter. It is the LAWFUL part of
lawful-retry: it refuses every unlawful re-dispatch fail-closed, and defers the
lawful ones to the machinery built to carry them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping, Optional, Sequence

from . import peer_offer
from .peer_authority import AuthorityWidening, Grant, check_delegation_chain

#: The retry-budget key on a :class:`Grant`'s ``budgets`` map (the same scalar
#: ``peer_authority`` treats as monotone). A bounded retry decrements it.
RETRY_BUDGET_KEY = "retries"


class EffectClass(str, Enum):
    """How an effect may be re-run on peer loss — the classification the
    dispatcher CONSULTS (extern tiers + compensation classes, docs 243/247).
    A ``str`` enum so a value read off an extern record or a wire payload
    compares directly."""

    PURE = "pure"
    IDEMPOTENT_EXTERNAL = "idempotent-external"
    WITNESSED = "witnessed"
    DEFERRED_IRREVERSIBLE = "deferred-irreversible"
    SECRET_BEARING = "secret-bearing"


class Disposition(str, Enum):
    """What the dispatcher decided to do with a lost attempt."""

    #: re-issue the work to another eligible peer (a new, narrowed chain hop).
    REPLAY = "replay"
    #: run the recorded inverse via ``recovery.py`` — no re-issue.
    COMPENSATE = "compensate"
    #: witnessed work: hand off durably and let WAL-based recovery coordinate.
    DURABLE_HANDOFF = "durable-handoff"
    #: an irreversible commit: resolvable only at a trusted commit authority,
    #: never speculatively re-issued to a pool peer.
    COMMIT_AUTHORITY_ONLY = "commit-authority-only"
    #: nothing lawful is possible (e.g. a replay budget is exhausted and no
    #: inverse is recorded): fail-closed rather than widen or guess.
    REFUSE = "refuse"


#: The minimum peer trust level a secret-bearing effect may be replayed to.
#: "trusted/local/attested peers only (never a bare verified offer)" — so the
#: floor is `attested`, one rank above the bare `verified` a signed offer clears.
SECRET_TRUST_FLOOR = "attested"


@dataclass(frozen=True)
class Attempt:
    """A lost attempt the dispatcher must resolve.

    ``effect_class`` keys the decision. ``grant`` is the authority the lost
    attempt held (a :class:`peer_authority.Grant`); a replay hop is a narrowing
    of it. ``has_recorded_inverse`` says whether a compensation is available for
    the compensate/witnessed paths (a recorded inverse in the accumulator/WAL).
    ``base_slot`` is the placement slot template a replay peer must satisfy
    (its ``grant``/``budgets`` are overridden with the computed retry grant)."""

    effect_class: EffectClass
    grant: Grant
    has_recorded_inverse: bool = False
    base_slot: Optional[peer_offer.PlacementSlot] = None


@dataclass(frozen=True)
class Decision:
    """The dispatcher's ruling on one lost attempt.

    ``disposition`` is the action. ``retry_grant`` is the narrowed grant a REPLAY
    hop carries (``None`` otherwise). ``peer_id`` names the chosen eligible peer
    for a REPLAY. ``reason`` is a human/agent-legible explanation. The whole
    thing is data a caller/audit view reads without parsing prose."""

    disposition: Disposition
    reason: str
    retry_grant: Optional[Grant] = None
    peer_id: Optional[str] = None


def classify_extern(facets: Mapping[str, object]) -> EffectClass:
    """Map an extern's recorded facets to an :class:`EffectClass`, so a caller
    with a real extern record (audit_diff/emission facets) does not hand-pick the
    class. Consults the same facet names those records carry — ``secret``,
    ``witnessed``, ``deferred``, ``idempotent`` — in precedence order:

    1. ``secret`` -> secret-bearing (the tightest gate wins regardless of else);
    2. ``deferred`` (a deferred-irreversible commit) -> deferred-irreversible;
    3. ``witnessed`` -> witnessed/revertible;
    4. ``idempotent`` (or ``undo_idempotent``) -> idempotent-external;
    5. otherwise -> pure.

    Precedence is deliberately tightest-first: an effect that is BOTH secret and
    idempotent is dispatched under the secret gate, never the looser one."""
    if facets.get("secret"):
        return EffectClass.SECRET_BEARING
    if facets.get("deferred"):
        return EffectClass.DEFERRED_IRREVERSIBLE
    if facets.get("witnessed"):
        return EffectClass.WITNESSED
    if facets.get("idempotent") or facets.get("undo_idempotent"):
        return EffectClass.IDEMPOTENT_EXTERNAL
    return EffectClass.PURE


def _retry_grant(attempt: Attempt, holder: str) -> Grant:
    """The narrowed grant a replay hop carries: the lost attempt's caps and
    budgets, with the retry budget decremented by one. Decrementing keeps the
    hop monotone-narrowing on the retry-budget axis and makes an unbounded retry
    storm impossible — the budget runs out down the chain.

    A grant that names no retry budget is treated as holding zero: there is no
    budget to spend, so no replay hop can be constructed (the caller checks
    :func:`retry_budget` first)."""
    budgets = dict(attempt.grant.budgets)
    budgets[RETRY_BUDGET_KEY] = max(0, retry_budget(attempt) - 1)
    return Grant(holder, attempt.grant.caps, budgets)


def retry_budget(attempt: Attempt) -> int:
    """The retry budget remaining on the lost attempt's grant, or 0 when it
    names none (fail-closed: an absent budget is not an unbounded one)."""
    return int(attempt.grant.budgets.get(RETRY_BUDGET_KEY, 0))


def _slot_for_retry(attempt: Attempt, retry_grant: Grant, *, trust_floor: str
                    ) -> peer_offer.PlacementSlot:
    """The placement slot a replay peer must satisfy: the attempt's base slot
    (region/hardware/resource facets) with the retry grant/budgets and the
    effect-class trust floor applied. The floor is raised, never lowered — a
    secret-bearing effect keeps its `attested` floor even if the base slot only
    asked for `verified`."""
    base = attempt.base_slot or peer_offer.PlacementSlot()
    floor = trust_floor
    if peer_offer._trust_rank(base.trust_floor) > peer_offer._trust_rank(floor):
        floor = base.trust_floor
    return replace(base, trust_floor=floor, grant=tuple(retry_grant.caps),
                   budgets=dict(retry_grant.budgets))


def _choose_peer(chain: Sequence[Grant], attempt: Attempt, retry_grant: Grant,
                 candidates: Sequence[Mapping], key: bytes, *, trust_floor: str
                 ) -> tuple[Optional[str], list[str]]:
    """Pick the first candidate peer whose signed offer is eligible for the
    retry slot AND whose new hop keeps the chain monotone. Returns
    ``(peer_id or None, rejection_reasons)``.

    Both gates must hold: Primitive 1 (:func:`peer_offer.offer_eligible`) proves
    the peer advertised a ceiling that covers the retry grant and satisfies the
    facets; Primitive 3 (:func:`check_delegation_chain`) proves the retry hop does
    not widen the chain. A candidate that fails either is skipped fail-closed, not
    stretched to fit."""
    slot = _slot_for_retry(attempt, retry_grant, trust_floor=trust_floor)
    rejected: list[str] = []
    for record in candidates:
        peer_id = record.get("peer_id") if isinstance(record, Mapping) else None
        eligible, reason = peer_offer.offer_eligible(record, slot, key)
        if not eligible:
            rejected.append(f"{peer_id!r}: {reason}")
            continue
        hop = replace(retry_grant, holder=str(peer_id))
        try:
            check_delegation_chain([*chain, hop])
        except AuthorityWidening as widening:
            rejected.append(f"{peer_id!r}: retry hop would widen the chain ({widening})")
            continue
        return str(peer_id), rejected
    return None, rejected


def dispatch_on_loss(chain: Sequence[Grant], attempt: Attempt, *,
                     candidates: Sequence[Mapping] = (),
                     key: bytes = b"") -> Decision:
    """Decide what to do with one lost attempt, keyed on its effect class.

    ``chain`` is the delegation/retry chain so far (``chain[0]`` the composition's
    own authority, later elements the peers/retries that led to this attempt).
    ``candidates`` are signed peer-offer records (from :func:`peer_offer.sign_offer`)
    a replay may be offered to; ``key`` verifies them. Returns a :class:`Decision`.

    Per the design table:

    * PURE — freely replayable; choose any eligible peer.
    * IDEMPOTENT_EXTERNAL — bounded retry; replay only while the retry budget is
      positive, else COMPENSATE (if an inverse is recorded) or REFUSE.
    * WITNESSED — never speculatively replayed; DURABLE_HANDOFF to WAL-based
      recovery.
    * DEFERRED_IRREVERSIBLE — never re-issued to a pool peer; COMMIT_AUTHORITY_ONLY.
    * SECRET_BEARING — replay only to a peer at or above the `attested` floor
      (never a bare `verified` offer).

    Fail-closed throughout: an exhausted budget with no inverse REFUSES; a replay
    with no eligible peer REFUSES; a retry hop that would widen is never taken."""
    ec = attempt.effect_class

    if ec is EffectClass.WITNESSED:
        return Decision(
            Disposition.DURABLE_HANDOFF,
            "witnessed/revertible work is not speculatively replayed; it is "
            "handed off durably for WAL-based recovery coordination (recovery.py)")

    if ec is EffectClass.DEFERRED_IRREVERSIBLE:
        return Decision(
            Disposition.COMMIT_AUTHORITY_ONLY,
            "a deferred-irreversible commit is resolvable only at a trusted "
            "commit authority; it is never speculatively re-issued to a pool peer")

    # The three replayable classes (pure, idempotent-external, secret-bearing)
    # all go through the same lawful-replay path; they differ only in the trust
    # floor a replay peer must clear and, for idempotent-external, in the budget.
    budget = retry_budget(attempt)
    if ec is EffectClass.IDEMPOTENT_EXTERNAL and budget <= 0:
        if attempt.has_recorded_inverse:
            return Decision(
                Disposition.COMPENSATE,
                "the retry budget is exhausted; the recorded inverse is run "
                "instead of re-issuing (recovery.py's roll-back path)")
        return Decision(
            Disposition.REFUSE,
            "the retry budget is exhausted and no inverse is recorded; refusing "
            "rather than replaying past the budget or widening it")

    trust_floor = (SECRET_TRUST_FLOOR if ec is EffectClass.SECRET_BEARING
                   else "verified")

    # A replay needs a budget to spend for every replayable class: the hop's
    # retry budget is the monotone quantity that must run out, so a grant naming
    # no retry budget cannot be lawfully replayed.
    if budget <= 0:
        if attempt.has_recorded_inverse:
            return Decision(
                Disposition.COMPENSATE,
                "no retry budget remains to spend on a replay; the recorded "
                "inverse is run instead")
        return Decision(
            Disposition.REFUSE,
            "no retry budget remains and no inverse is recorded; refusing rather "
            "than replaying with a budget the delegator did not hand down")

    retry_grant = _retry_grant(attempt, holder="<retry>")
    peer_id, rejected = _choose_peer(chain, attempt, retry_grant, candidates,
                                     key, trust_floor=trust_floor)
    if peer_id is None:
        detail = "; ".join(rejected) if rejected else "no candidates offered"
        return Decision(
            Disposition.REFUSE,
            f"no eligible peer for a lawful replay ({detail})")
    return Decision(
        Disposition.REPLAY,
        f"replaying {ec.value} work to {peer_id!r}: its offer covers the "
        f"narrowed retry grant and the retry hop keeps the chain monotone",
        retry_grant=replace(retry_grant, holder=peer_id), peer_id=peer_id)

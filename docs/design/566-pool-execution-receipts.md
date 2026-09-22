# Design: signed execution receipts and the evidence recount (item 524, issue #1198)

Status: landed. `src/revl/pool_receipt.py` issues, checks and counts execution
receipts; `peer_pool.promote` recounts its evidence from them and no longer
takes a count from a caller.

Read before this: [550-private-peer-pool.md](550-private-peer-pool.md) (the
pool declaration and the join gate this extends),
[555-asymmetric-peer-identity.md](555-asymmetric-peer-identity.md) (the key
lifecycle and the `Attribution` shape this reuses),
[540-shadow-promotion.md](540-shadow-promotion.md) (item 518, the precondition
shape), and `src/revl/peer_identity.py`.

## The fail-open this closes

`docs/design/550-private-peer-pool.md` named it under "what is left", item 3:

> `Membership.evidence` is an integer a caller increments today. It should be
> the count of receipts this pool has verified under a key in `attest_key_ids`,
> computed rather than supplied.

The shape of the hole was this. `Membership.evidence` was a dataclass field of
type `int`. `promote` compared it against the tier's `evidence_required` and
separately checked that a caller-supplied list `evidence_key_ids` was a subset
of the charter's `attest_key_ids`. Both inputs were assertions. Neither was
checked against a receipt, because there were no receipts.

So the gate read:

```python
required = charter.tiers[tier].evidence_required
if member.evidence < required:        # an integer somebody handed us
    return _refusal(...)
```

A peer that states its own evidence count is a peer that can state any evidence
count, and this is the one arrow in the pool that RAISES authority. The
pre-existing test suite demonstrated it without naming it: a test constructed a
member with `evidence=3` and asserted that the peer was promoted, and another
constructed one with `evidence=99`.

The count is now `len(Membership.evidence_digests)` and the digests are
receipts this pool verified. There is no parameter left through which a number
can be asserted, at any of the three places one used to enter: the `Membership`
constructor, `_issue_membership`, and a roster file on disk.

## A receipt is two signatures, not one

Two parties are saying two different things and collapsing them would lose the
distinction that decides whether the evidence is worth anything.

| | signed by | over | saying |
|---|---|---|---|
| execution receipt | the PEER | pool, peer, task, artifact digest, result digest, issue time | "I ran this and produced that" |
| attestation | an ATTESTING AUTHORITY named in the charter | the DIGEST of the receipt | "I checked that result and it is admissible" |

The attestor signs the receipt's digest rather than a copy of its body. There
is then exactly one copy of the claim, so an attestation cannot be slid onto a
different receipt and the two cannot drift apart. `receipt-binding` is the
refusal when someone tries.

The peer cannot sign the second row. That is `self-attestation`, and it is
refused before the charter's `attest_key_ids` are consulted, so it holds even
if an operator has mistakenly pinned the peer's own key as attesting authority.

Both domains are new and distinct from each other and from `attest`'s,
`peer_offer`'s, the pool charter's and the deploy receipt's. Without separate
domains one protocol's signature would verify as another's.

## Why the verdict is an `Attribution` and never a boolean

Issue #1278 established the shape and this keeps it rather than flattening it.
`verified` is arithmetic over bytes and never changes once true. `status` is a
live reading of the signing key's authority and does change. `confers_authority`
is the conjunction, spelled out so nobody has to remember which of the two a
bare boolean meant.

A receipt signed under a key that was later revoked is `verified=True,
status="revoked"`: a true statement about work that really happened, conferring
nothing now. An operator auditing a ledger must be able to tell that from a
forgery, and a `False` for both would destroy the distinction. `ReceiptCheck`
therefore carries BOTH attributions, always, and `counts` is the conjunction
of the two plus an empty refusal link.

Counting requires `confers_authority` on both signatures. That is the
conservative direction and it is the same direction everything else in the pool
takes: a receipt whose key has been rotated away from stops counting, and the
only thing that can do is leave a peer at a LOWER tier than it might have
reached. It can never raise one.

## Deduplication is on the task, not the digest

A peer can re-sign the same work with a new timestamp and get a fresh receipt
digest for free. Counting distinct digests would therefore let a peer inflate
its own evidence at no cost, so the count is of distinct `task_id`s.

A second receipt for a counted task is refused on `replayed-receipt` and
RECORDED rather than dropped. Item 524's delivery rule is that "delivered
twice" must be impossible or visible; a duplicate that vanished silently would
be a delivery the ledger cannot show was refused.

The fold is deterministic: pairs are ordered by the receipt's `issued_at` and
then by its digest before counting, so which of two receipts for one task is
the counted one does not depend on the order a caller passed them in.

## The count is a precondition, not a stage

Item 518's shape, applied to the second thing a promotion needs.
`_ceiling_precondition` is the only function that produces a tier grant;
`_evidence_precondition` is now the only function that produces an evidence
count, and it returns either a count or a refusal. `promote` is the only caller
of either. A count that did not come from the recount cannot reach the
threshold comparison because there is no other path to it.

A promotion CITES what it counted: `evidence_digests`, `evidence_tasks` and the
attesting `evidence_key_ids` go into the promotion receipt, along with every
refused receipt and its link. A third party can recheck the arithmetic instead
of being told the answer.

## What a shared-key member can prove, which is nothing

A receipt is an asymmetric record. A member that joined under a shared key can
produce no countable receipt at all, so it cannot be promoted above the entry
tier.

This is deliberate rather than an omission. An HMAC authenticates under a
secret the pool ALSO holds, so it cannot show that the PEER produced a result
rather than the pool itself, and evidence the verifier could have manufactured
is not evidence. `test_a_shared_key_member_can_accumulate_no_evidence` pins it.

It is also the argument for tightening `PoolCharter.identity_mode`, which
defaults to `mixed` for item-524 compatibility while `pool init` writes
`asymmetric`. See "What is left" below: the tightening is now safe on the
promotion path and is not yet safe on the admission path, and those are
different claims.

## Non-vacuity

Twelve receipts, each of which WOULD have promoted a peer under the old gate,
because the old gate never looked at a receipt. Each is refused here on a named
link, and an honest control differing only in being well formed goes through
the same function and is counted.

```
forged receipts in the corpus            12
counted as evidence by the new check      0 / 12
distinct refusal links exercised         >= 8
honest control, counted                   1 / 1
two honest receipts + one forgery         2, and the member stays put
```

The corpus covers: a receipt signed by an unpinned key, a result swapped after
signing, an artifact the peer was not admitted with, another pool, another
peer's receipt presented as this one's, a receipt predating the membership, a
peer attesting its own work, an attestor the charter does not name, an
attestation signed by an unpinned key, an attestation moved from another
receipt, an attestation whose verdict is not `admitted`, and a record that is
not a receipt.

## Refusals are links, not guarantee codes

Twelve lowercase links, following `peer_pool` and `revl.deploy`. Nothing enters
`revl.diagnostics.GUARANTEES`: a receipt refusal is a decision about a
deployment, not a verdict about a program, and there is no program to write
under `examples/rejections/` for "this receipt was already counted". An AST
walk asserts every declared link is passed by some call.

Nothing is published in `stdlib/crypto.rvl`. The stdlib still publishes exactly
the four symmetric primitives that `tests/test_stdlib_crypto.py` asserts it
publishes; this is a Python-side deployment module and it needs no `.rvl`
surface to do its job.

## What is left

**Dispatch over the wire.** Still not here, and item 524's exit test cannot
fully pass without it. But the reason given in
[550-private-peer-pool.md](550-private-peer-pool.md) is wrong and the blocker is
narrower than it claims. There is no "#421 network seam": roadmap item 421 is a
capability and codegen audit, and its network-seam sub-finding F8 hardened an
existing seam rather than building one. The genuine prerequisite is roadmap
item 118 (`revl deploy`, issue #79), because a peer is a machine boundary.
`deploy.VIA_PEER` exists solely to be refused by name, with reason
`peer-pool-unavailable`.

What is actually missing is narrower than "no transport exists". The
cross-machine channel, the pinned host key, bundle staging, the far-side
`deploy-admit` runner and a signed COMMIT receipt are all built and tested
under item 118. What is absent is the wiring: a dispatcher that turns a unit of
work into a request shaped like a `RemoteParticipant`, and the delivery ledger
it would write to. Measured by: two processes on two hosts, one `pool join`,
one task dispatched, the result returned.

**One-result delivery with a ledger.** `Roster.outstanding` is still populated
by the caller. The replay half of that rule is decided here, and it is the half
that needed a signature to decide: a duplicate result is refused on
`replayed-receipt` and recorded. What remains is the dispatch half, which
depends on the wire.

**Tightening `identity_mode`.** Safe on the promotion path now: a shared-key
member can accumulate no evidence and so cannot leave the entry tier. NOT yet
established on the admission path, where a `mixed` pool still admits a
shared-key join, and that is the fail-open #1290 named. Making the default
`asymmetric` is a migration decision about existing rosters, not a change this
item measured, so it is left with the measurement stated rather than done
quietly.

**`run --pool private`** and **liveness in `pool status`**, both unchanged from
550's list.

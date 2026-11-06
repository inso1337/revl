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
| execution receipt | the PEER | pool, peer, task, grant, policy digest, artifact digest, result digest, outcome, budget claim, issue time | "I ran this and produced that, under this authority" |
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

Thirteen receipts, each of which WOULD have promoted a peer under the old gate,
because the old gate never looked at a receipt. Each is refused here on a named
link, and an honest control differing only in being well formed goes through
the same function and is counted.

```
forged receipts in the corpus            13
counted as evidence by the new check      0 / 13
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

Sixteen lowercase links, following `peer_pool` and `revl.deploy`. Nothing enters
`revl.diagnostics.GUARANTEES`: a receipt refusal is a decision about a
deployment, not a verdict about a program, and there is no program to write
under `examples/rejections/` for "this receipt was already counted". An AST
walk asserts every declared link is passed by some call.

Nothing is published in `stdlib/crypto.rvl`. The stdlib still publishes exactly
the four symmetric primitives that `tests/test_stdlib_crypto.py` asserts it
publishes; this is a Python-side deployment module and it needs no `.rvl`
surface to do its job.

## The grant, the policy, the outcome and the budget claim (issue #2720)

A receipt used to say which peer ran which artifact and what result digest came
out, and nothing about the authority the peer ran under. A reader could not
tell, from the receipt alone, which capabilities the peer was granted, which
policy was in force, how the run ended or what it reports using. That is fixed
by two changes, one on each side of the channel.

### What the operator signs

`pool_dispatch.build_task` writes a 1.1 task that carries:

| field | value | decided by |
|---|---|---|
| `grant` | `{caps: [...], budgets: {...}}`, the member's tier grant | the operator, from the roster and the charter |
| `policy_digest` | the digest of the pool charter in force | the operator, from the charter it signs |

The grant is read from the member the dispatcher loaded, not accepted as a
parameter, so there is no path by which a caller hands a wider grant to the
task than the tier actually issued. The policy digest is the charter digest:
the charter IS the pool's policy, and a task that names a different policy than
the peer's charter is refused on `policy-identity` before the work is run.

### What the peer echoes

`pool_receipt.issue_receipt` writes a 1.1 receipt that adds:

| field | inside the peer signature | inside the attestation digest | checked | a claim |
|---|---|---|---|---|
| `task_digest` | yes | yes | the digest of the signed task presented to `check_receipt`, and the task's `task_id`, `peer_id`, `pool_id` and `artifact_digest` must equal the receipt's | |
| `grant` | yes | yes | equals the task's grant | |
| `policy_digest` | yes | yes | equals the task's policy digest | |
| `outcome` | yes | yes | one of `completed`, `failed`, `timed_out`, `refused` | which ending the peer observed |
| `budget_consumed` | yes | yes | a map of non-negative numbers | everything in it |

All five live in the signed body, so `receipt_digest` covers them and the
attestation, which signs that digest, binds them. Changing any of them after
the peer signs breaks the peer's signature AND moves the digest the attestor
signed away from the record in hand; the tests assert the two failures
separately because they are different statements about the same bytes.

`outcome` is the peer's own report of how its run ended. `refused` is in the
vocabulary for a receipt issued for a run the runner itself declined; the
dispatcher's peer refusals happen BEFORE a run and are refusal records with a
link, not receipts. `timed_out` is measured by the peer
(`pool_dispatch.execute`), never inferred from exit code 124, because a program
may exit 124 itself. `budget_consumed` is the peer's own report and is a CLAIM:
nothing here meters the operator's budget keys, and the one quantity this
runner measures is wall-clock seconds. A reader must treat it as the peer's
statement, never as arithmetic the pool verified.

### Checked facts and claims, stated once

- CHECKED: `task_digest` and the task's identity (`task_id`, `peer_id`,
  `pool_id`, `artifact_digest`), `grant`, `policy_digest`, the two signatures,
  the pool, the peer, the artifact digest, the admission time and the
  attestor's authority.
- CLAIMED: `outcome`, `budget_consumed`.
- NOT CLAIMED: that the run was correct or that the reported budget is true.
  The correctness check remains the attestor link, and the budget report has no
  second signature behind it.

A refusal names which of these failed: `receipt-task` (the receipt answers a
task that was not presented, or a different one), `receipt-grant` (the echoed
grant is not the task's), `receipt-policy` (the echoed policy is not the
task's). The task/grant/policy checks run before the attestation is looked at,
because an attestation over the wrong task is a downstream fact.

### The version field decides, and old receipts still verify

`RECEIPT_VERSION` is `1.1`; `RECEIPT_VERSION_V1` is `1.0`. `check_receipt`
reads the version and requires the members of THAT shape: a 1.1 receipt that
drops the grant is malformed on `receipt-shape`, never read as a 1.0 receipt
that happened to lose a member. A 1.0 receipt still VERIFIES under the default
`require_bound=False`: the signature arithmetic is real and the work really
happened. It does NOT count as evidence for a promotion that requires the
binding. `count_evidence` defaults `require_bound=True`, and it refuses such a
receipt on `unbound-receipt` after every other check passes, so an operator
sees the specific fault when there is one and the weakness of the shape only
when there is not.

The ledgers written before this change have deliveries with no stored task; a
1.1 receipt cannot be re-checked without one, so `check_receipt` refuses
`receipt-task` rather than trusting the echo. `evidence_pairs` returns the task
beside the pair when the ledger kept it. A pool that wants to promote on old
evidence must re-run the work; that is the fail-closed direction, because a
receipt that names no authority cannot be evidence for authority.

### Non-vacuity

`tests/test_pool_receipt_authority_2720.py` pins the three acceptance clauses:
a receipt built with a grant the task did not grant is refused on
`receipt-grant` while both signatures still verify; changing each new field
after signing breaks the peer signature and the attestor binding; a 1.0 receipt
verifies and is refused as evidence on `unbound-receipt`, while a 1.1 receipt
with a missing member is malformed rather than guessed at. The positive
control counts a bound receipt and promotes on it, so the gate is not one that
refuses everything.

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

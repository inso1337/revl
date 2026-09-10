# 471: Multi-party approval, the decision graph, and the override that is not a vote

Design note for roadmap item 471 (issue #823). It records what the item asked
for, what the tree actually had, the slice that lands with this note, and the
parts of the item that are deliberately left to a later slice.

Status: Slice 1 landed with this note (the policy clause, the session-side
decision protocol, the durable decision graph, the transport property that
carries a vote, and the suite that pins every refusal). Slice 2 is design only
here: the operator verbs for escalate/revoke/override, and the admission-time
receipt the item names.

## The item's premise, measured against the tree

Item 471 asks for configurable N-of-M approval with separation of duties, where
the proposer cannot be the sole approver, each vote bound to the exact candidate
hash, plan, resource target and expiry, the full decision graph recorded in the
WAL and receipt, handling denial, timeout, revoke, escalation and emergency
override, extending the approval machinery of items 246 and 251.

Measured against `267862fb`, some of that was already true and some of it is
stale. The stale half is worth writing down, because it is where the work
actually is.

**Already true.** The exact-hash binding and the deadline. The item 246 ticket
is already a hash-bound, single-use, expiring consent: `_issue_ticket` names the
reach-closure candidate hash, `_find_standing_approval` refuses a token whose
candidate hash no longer matches the call under it, and `_consume_approval`
spends the token durably before the crossing fires (consume-before-fire). The
deadline half is item 427 F8's dead latch: `_expired` latches `expiredAt` the
first time a record is past its `expiresAt`, so a record observed dead is dead
for good, and `_now_ms` refuses to read an injected clock below the last reading
it returned. Both are inherited here rather than rebuilt. The resource target is
also already carried: `_crossings` aggregates the reach closure (capability,
component, candidate hash) and the ticket embeds it, so a vote in this design is
bound to exactly the composition the ticket named rather than to a freshly
derived one.

**Stale.** There is no quorum vocabulary anywhere under `src/` before this
change. A search for "quorum", "N-of-M", "approver set" and "separation of
duties" finds the roadmap line and two `src/revl/deploy.py` sentences about a
cross-machine "quorum coordinator" that Slice 1 of item 118 deliberately does
not build, and nothing else. There is no notion of a proposer anywhere in the
approval path: the only identity is `_operator_token()`, so "the proposer cannot
be the sole approver" had no subject to test. There is no approval "plan" and no
"resource target" member distinct from the reach closure the ticket already
carries, so the item was asking for a binding that exists rather than one to
invent. There is no "receipt" artifact for an approval: the durable record today
is the `approval-granted` WAL record plus the ledger entry it keys into, and
this note keeps that shape rather than adding a second, parallel consent
artifact. Denial existed only as an explicit deny GRANT and as fail-closed
refusals, never as a recorded NO cast by an approver. Escalation and emergency
override did not exist at all. The DSL spelling the item's own exit criterion
quotes, `require 2 of {a,b,c}`, did not parse.

So the honest reading is: the item's binding, expiry and record substrate were
already there, and what was missing is the multi-party DECISION on top of them.

## The one thing to get right

**A count of votes is not a quorum unless the votes are distinct, named, bound
to one exact question, and not the proposer's own.** Every one of those is a
refusal, and each refusal is recorded before it is raised. A quorum that counts
repeats is one approver with a loop; a quorum that counts a vote cast against a
different candidate is a yes for one composition spent on another; a quorum the
proposer can close alone is separation of duties with a comment. Because the
question's own record is durable and readable, an attacker who can probe the
protocol must not be able to do it invisibly, so refusals are rows in the same
decision graph the votes are.

The second half: **an override is a different authority, never a shortcut to
quorum.** The emergency path is recorded as `satisfiedBy: "override"` with the
count it actually had, which is below `require` by construction. Nothing in the
record lets a later reader mistake the override for the quorum it stood in for,
and an override that carries no reason is refused. A path that admits a crossing
without quorum and does not say so is worse than no path at all.

Third: **the clause must be inert when it is not written.** A policy that names
no approver set keeps the byte-identical single-party shape (`require=1`, no
names), which is what stops a new vocabulary from changing any existing policy's
behaviour.

## What already exists (the landed foundation this reads)

* **The rule and its parse** (`src/revl/policy.py`). `ApprovalRule(capability,
  ttl_ms, require, approvers)` now carries a count and an approver set, with
  `is_quorum()` and `names_approvers()` the rest of the tree asks. The DSL
  branch for `requires approval` accepts `require N of {a, b, c}` on either side
  of `ttl <D>`; the JSON `approvals` branch accepts `require` and `of` together,
  and neither alone. A clause that cannot mean what it says is refused at parse
  time rather than at the crossing: a duplicate approver, `*` as an approver (a
  quorum counts NAMED operators), a count of zero, an empty set, a count larger
  than the set, and an unrecognised trailer all fail closed.
* **The item 246 two-step.** `revl_approve(hash)` mints a hash-bound,
  single-use approval; the identical re-issue crosses once and consumes it. Item
  471 does not replace this. A quorum approval mints through the same
  `_mint_ticket_entry`, so a multi-party consent is an ordinary ledger entry
  with the same liveness axes (component, candidate hash, session, round) and
  the same consume-before-fire spend.
* **The item 251 ledger.** The approval ledger and the distilled rules read the
  same entry, so a quorum approval is attributable in exactly the way a
  single-party one is.
* **The item 427 F8 latches.** `_expired` for the deadline, `_now_ms` for the
  clock floor.
* **The WAL.** `WriteAheadLog` is the durable, readable record. Quorum records
  are seq-free, as `approval-granted` and `approval-consumed` are: they are
  consent facts, not effects, and they consume no sequence.
* **The operator layer.** `_tool_approve` is the transport entry point, gated by
  the `approve` verb.

## Decision 1: the clause, and why it is a count of names

The clause is `require N of {a, b, c}` on the approval rule for a capability, in
the same `requires approval` grammar that already carries `ttl`. The approver
names are operator tokens, not roles: a role indirection would need the profile
grammar to resolve a token to a set, and this session has one operator, so a
role would be a set of one name with extra steps.

The parse refuses what it cannot enforce. `require 2 of {alice, alice}` is one
approver, not two. `require 2 of {*}` asks the runtime to count an unbounded set
that the rule does not bind. `require 3 of {a, b}` names too few approvers for
its own count. Each of those is a policy that cannot mean what it says, so it is
refused where it is written.

At the crossing, a rule whose count cannot be reached by the operators who are
left is refused before a ticket is issued: the proposer is excluded from the
eligible set, so a rule naming `{alice, bob}` with `require 2` and proposed by
`alice` leaves one eligible approver and is refused at `_issue_ticket`. That is
separation of duties applied to the policy rather than to a single vote.

## Decision 2: the vote protocol

A ticket whose rule demands a quorum cannot be answered by a bare
`approve_ticket(hash)`. It takes a vote: `approve_ticket(hash, vote="approve"
| "deny", as_token=<operator token>)`. A ticket whose rule names no approver set
refuses a vote, so the two shapes are mutually exclusive rather than
silently-reinterpreted.

Each vote goes through the same check series, in this order, and every branch
records a `quorum-refused` row BEFORE it raises:

| refusal | what it stops |
| --- | --- |
| `malformed-vote` | a vote that is neither `approve` nor `deny`, refused rather than coerced |
| `expired` | a vote past the question's own ttl, the rule's `ttl` carried by the ticket |
| `closed-decision` | a late vote on a question already satisfied or denied |
| the question's own outcome | a vote on a question that escalated, revoked or expired |
| `stale-candidate` | a vote whose record no longer matches the live candidate hash, component or plan |
| `unknown-approver` | a voter the rule does not name, including an unbound session |
| `proposer` | the operator who asked for the crossing approving its own question |
| `duplicate-voter` | a second vote from a name that already voted |

Only after all of that does the vote count. A counted vote either closes the
question (the count reaching `require`, minting the approval through the
ordinary ledger) or leaves it open with one more name against it.

Votes are not re-counted and not replayed. The record is keyed by a
round-scoped request id (`hash` for the first round, `hash#rN` after), so a
re-raise of the same crossing cannot discard the votes already cast, cannot
restart the deadline, and does not owe a fresh quorum. A re-issue inside the
same round lands on the same record; the round advances only once the question
is decided.

## Decision 3: the decision graph

The graph is durable, readable and append-only, and its kinds are a closed set.
`WriteAheadLog.record_quorum_event(kind, entry)` refuses a kind it does not
define, so a typo is a programming error rather than an unreadable row. The
kinds are `quorum-open`, `quorum-vote`, `quorum-refused`, `quorum-satisfied`,
`quorum-denied`, `quorum-expired`, `quorum-escalated`, `quorum-revoked` and
`quorum-override`.

Every record carries the binding (ticket hash, request id, candidate hash,
component, plan, session, round, capabilities), the rule as written, the
proposer, the approver set, the count and the `require`. The vote rows add the
voter and the vote; the refusal rows add the reason and the voter; the closing
rows add who closed it and how.

The item asks for the graph in "the WAL and receipt". There is no separate
receipt artifact in this tree for an approval, and this slice does not invent
one: the `approval-granted` record carries a `quorum` sub-dict (request id,
require, counted, approvers, proposer, `satisfiedBy`, the votes cast, and the
override when there was one), and the minted approval is the same ledger entry a
single-party approval is. The receipt-shaped artifact the item names belongs
with the admission-time consumer of that ledger entry, which is Slice 2.

`quorum_state(hash)` is the read-only reader over the graph: it reports the
question, the votes, the refusals and the outcome, and it is how a test or an
operator inspects a decision without mutating it.

## Decision 4: denial, timeout, escalation, revocation, and the override

**Denial.** An approver saying NO is a `deny` vote, recorded as `quorum-vote`
with `vote: "deny"`. A denial does not close the question by itself: it closes it
when the count is no longer reachable (`counted + remaining < require`), and the
closing record distinguishes `denied` (a human refused) from `unreachable` (the
arithmetic ran out). A denial that leaves the count reachable leaves the question
open, because a second approver may still carry it.

**Timeout.** The deadline is the ticket's own ttl, and the first vote past it
latches `expiredAt` and writes `quorum-expired` with the count it had. The latch
is inherited (item 427 F8) rather than reimplemented, so the timeout is
irreversible in the same way a grant's is. Note the honest limit: because
`_now_ms` cannot read an injected clock below its last reading, the rewound-clock
half of that property is defended by the inherited floor as well as by the latch,
and a test cannot separate the two.

**Escalation.** `escalate_ticket(hash, reason=...)` closes the vote path and
records `quorum-escalated`. Only a named approver may escalate; a bystander is
refused as `unknown-approver` and the refusal is recorded. An escalated question
is closed: a later vote is refused rather than counted.

**Revocation.** `revoke_ticket(hash, ...)` stops the votes counting and records
`quorum-revoked`, under the same insider-only rule as escalation. It is
deliberately distinct from the item 379 `revoke_standing_grant`, which retires an
already-minted standing grant: this withdraws a pending question.

**Emergency override.** `override_ticket(hash, reason=..., as_token=...)`
records `quorum-override`, then closes the question through the same
`_decide_satisfied` path with `satisfiedBy: "override"`, so the closing record
says the crossing was admitted WITHOUT the count. It refuses an unknown ticket, a
ticket that demands no quorum (there is nothing to override), a blank or missing
reason, a lapsed question, and a question already decided. An override that
bypassed a decided question would be a replay primitive for consent, so it is
refused and recorded.

## Decision 5: the honest bound

**A session binds exactly one operator.** `SESSION.operator` is bound once at
serve startup, and the WAL attributes every grant to `_operator_token()`. A
second approver identity cannot reach a live session through the current MCP
transport, so a session cannot gather distinct approver tokens over the wire.

What this design guarantees is therefore the protocol and its record, not the
transport of a second human: the count is of distinct NAMED approvers, so a
repeated vote from one name can never satisfy a quorum, and the set is bound to
the rule rather than to whatever the caller claims. The identity of a cast is
supplied as `as_token`, which is how the suite drives the second and later votes,
and the design note says so rather than pretending the wire had two people on it.
Making several operators addressable within one session is a transport item
(operator profiles and the item 55 verb grammar), not this one.

## Slice plan

Slice 1, landed with this note: the clause and its refusals in the policy; the
session-side protocol (`_open_quorum`, `_cast_vote`, `_decide_satisfied`,
`_decide_denied`, `_lapse_quorum`, `override_ticket`, `escalate_ticket`,
`revoke_ticket`, `quorum_state`); the nine WAL record kinds and their
seq-free write; the `quorum` sub-dict on `approval-granted`; the optional `vote`
and `asToken` properties on `revl_approve`; and
`tests/test_471_quorum_approval.py`.

Slice 2, design only: the operator verbs for override, escalate and revoke
(their own tools, hence their own doc surface and verb gating), the
admission-time receipt the item names, and the second-operator transport that
would let a quorum be gathered from more than one bound operator.

## Exit tests

The item's exit criterion: "a `require 2 of {a,b,c}` rule admits only on two
distinct valid votes bound to the candidate hash, refuses a proposer-self
approval, and records the decision graph in the WAL." Pinned by
`tests/test_471_quorum_approval.py`: `test_two_distinct_votes_admit_the_crossing
_and_one_does_not`, `test_the_proposer_can_never_satisfy_quorum_alone`,
`test_a_single_vote_never_admits_a_quorum_ticket`, `test_the_minted_approval_is
_bound_to_the_candidate_hash`, and `test_quorum_state_reports_the_graph_and_the
_refusals`.

## Relates to

* 246: the two-step ticket this extends, and the class-(c) decision chokepoint.
* 251: the ledger the minted approval lands in.
* 427 F8: the dead latch the deadline reuses.
* 344 / 379: the standing grant and its revocation, deliberately distinct from a
  pending-question revocation.
* 476: which names 471 as a dependency.
* issue #823: this item.

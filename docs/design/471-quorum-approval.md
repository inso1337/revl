# 471: Multi-party approval, the decision graph, and the override that is not a vote

Design note for roadmap item 471 (issue #823). It records what the item asked
for, what the tree actually had, the slice that lands with this note, and the
parts of the item that are deliberately left to a later slice.

Status: COMPLETE over two slices. Slice 1 landed with this note (the policy
clause, the session-side decision protocol, the durable decision graph, the
transport property that carries a vote, and the suite that pins every refusal).
Slice 2 landed the two halves that note left design-only: the operator verbs for
escalate / revoke / override, each with its own tool, doc surface and verb
gating, and the admission receipt. Decision 6 below is Slice 2's; the earlier
decisions are Slice 1's and are edited only where they claimed something Slice 2
changed. What remains open is named at the end, and none of it is item 471: the
second-operator transport is a transport item (operator profiles, item 55).

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

**No admission path may widen the rule.** Force the vote path at `_issue_ticket`
and stop there, and the quorum is bypassable: `_approval_decide_call` consults
three earlier paths before it issues a ticket, and each of them is ONE operator's
authority recorded once, not N distinct named approvers.

  * `_find_standing_approval` - an already-minted approval for the exact
    candidate, which is how a SATISFIED quorum's ledger entry is spent. This one
    must keep admitting; refusing it would deadlock the question it answers.
  * `_find_standing_grant` - the item 344 session-scoped grant, minted by
    `revl_approve(hash=<ticket>, uses=N)` or
    `revl_approve(capability=<cap>, uses=N)`.
  * `_find_auto_approve` - the item 251 distilled / hand-written rule that
    auto-approves a capability without prompting at all.

Both of the last two used to admit a quorum-gated crossing with zero votes and
left the question it opened unanswered beside a consumed crossing, which is the
one thing this design says is worse than no path at all. The invariant is
therefore carried by ONE predicate, `Session._multi_party_rules(ticket)`, which
returns every covering rule for the ticket's capabilities for which
`rule.names_approvers() or rule.is_quorum()` holds. It is the only place that
spelling lives, and every path that can create or match consent consults it:

| call site | what it does with it |
| --- | --- |
| `mint_standing_grant` (both routes) | refuses to mint: no grant for such a crossing can exist |
| `_find_standing_grant` | refuses to match: a grant minted before the rule was bound cannot spend through it |
| `_auto_rule_covers` | refuses to cover: a distilled rule cannot make the quorum rule decorative |
| `_ticket_approval_shape` | delegates to it, so the crossing-time refusal in the paragraph above shares the one predicate |

`_find_standing_approval` is deliberately NOT gated by it, for the reason above:
it is the second half of the vote path, not a bypass of it. The rule that
`require 1 of {a, b}` is refused too, and not only `require N of {...}` with N
greater than one: naming approvers at all is a statement about WHO may answer,
which one operator's standing authority is not a substitute for.

**The one kind the predicate forces onto the vote path: a lease ticket.** An
`effect lease` is acquired BEFORE any crossing fires, and `_enforce_lease_gate`
admits it only when a live lease-tagged standing grant covers it. Left there, a
`require N of {...}` rule over the leased capability would make the composition
unloadable rather than merely unauthorised: the mint is refused on both routes, so
no grant could exist, and no vote could ever reach the gate. The lease ticket is
therefore the one kind whose acquisition the predicate pushes onto the vote path,
and it keeps exactly one route of its own, taken in `_enforce_lease_gate` and
nowhere else:

  * the gate reads the decision for THAT lease ticket
    (`_satisfied_decision_for`) and mints the lease grant from the satisfied
    ledger entry itself, spending that entry once before the boot. The proof is
    re-derived inside the mint (`_decision_authorizes_grant`), never trusted: a
    copy of the entry, an entry for another ticket, a decision that is not
    `satisfied` by `votes` or `override`, a decision whose own hash, candidate
    hash or component is not the entry's, and a ticket the session no longer has
    outstanding all fail it;
  * `decision` is not a parameter of the public `mint_standing_grant`, so no
    operator request can take this route — `revl_approve(hash=<lease ticket>,
    uses=N)` and `revl_approve(capability=<leased capability>, uses=N)` both
    refuse, exactly as they do for a crossing;
  * the grant the gate mints is lease-tagged, and `_find_standing_grant` still
    refuses a multi-party ticket, so the lease handle admits no class-(c)
    crossing. The authority is still the N votes; the exception narrows WHO may
    ask, never WHICH rules bind.

One thing the invariant does not say, and the code does not do: it does not make
the lease gate independent of the rule. Before the votes arrive, and for a
decision that is absent, lapsed, denied, spent or bound to another ticket, the
lease load is REFUSED with the question open, exactly as it was before this
slice — the acquisition has a route, it does not have a bypass.

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

The item asks for the graph in "the WAL and receipt". Slice 1 put it in the WAL:
the `approval-granted` record carries a `quorum` sub-dict (request id, require,
counted, approvers, proposer, `satisfiedBy`, the votes cast, and the override when
there was one), and the minted approval is the same ledger entry a single-party
approval is. The receipt-shaped artifact the item names belongs with the
admission-time consumer of that ledger entry, and it is Decision 6 below.

`quorum_state(hash)` is the read-only reader over the graph: it reports the
question, the votes, the refusals and the outcome, without mutating it. Slice 1
left it unreachable from the transport (a way for a TEST to inspect a decision,
not for an operator to). Slice 2 gave it a verb: `revl_quorum` is that reader,
read-only and ungated, and it carries the admission receipt too.

## Decision 4: denial, timeout, escalation, revocation, and the override

**Reachability, stated once for the whole decision.** All five paths below are
shipped end to end as of Slice 2. *Denial* and *timeout* are part of the vote
path, which the transport has always reached: `revl_approve(hash=...,
vote="deny")` denies and the ticket's own ttl times the question out.
*Escalation*, *revocation* and the *emergency override* were session methods
(`escalate_ticket`, `revoke_ticket`, `override_ticket`) with no verb and no tool
behind them, and Slice 2 gave each one its transport: `revl_escalate`,
`revl_revoke`'s `hash` branch, and `revl_override`. What this decision pins is
still the PROTOCOL and its records; Decision 6 pins which authority reaches
each.

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
repeated vote from one name can never satisfy a quorum, and the eligible set is
the rule's own approver names, so a name the rule does not name is refused rather
than counted. The identity of a cast is supplied as `as_token`, which is how the
suite drives the second and later votes, and the design note says so rather than
pretending the wire had two people on it.

**`as_token` is caller-asserted, and that is the bound this slice does not
close.** It is a NAME, not a proof of possession: nothing in `_cast_vote` (nor in
`_tool_approve`, which forwards the property) verifies that the caller is the
operator it names. One bound operator can therefore satisfy a
`require 2 of {alice, bob, carol}` rule by asserting several of the rule's names
(`as_token="bob"`, then `as_token="carol"`), and the crossing is admitted. What
IS refused, and correctly, is every way of counting the same one name twice: a
second cast asserted under a name that already voted is refused as
`duplicate-voter`, and a differently-cased spelling is not one of the rule's
names at all, so it is refused as `unknown-approver` — neither can carry the
count, because the eligible set is compared literally. The self-quorum is a
self-asserted identity, not a miscount.

The bound is disclosed rather than papered over, and the minimal fix for a future
round is to bind the identity to a credential: have each operator hold a token
the session verifies (the operator profiles the slice plan already names as the
transport item), resolve `as_token` through that binding, and record the
authenticated subject on the `quorum-vote` row instead of reading the string the
caller supplied. Until then, `as_token` is what it says it is: the name of a
cast, asserted by the caller. Making several operators addressable within one
session is a transport item (operator profiles and the item 55 verb grammar), not
this one.

## Decision 6: the Slice 2 surface: the verbs, and the receipt

### The verbs, and why the override has one of its own

Slice 1's protocol was complete and unreachable. An operator on the wire could
open a question and vote on it; it could not hand a stalled one up, withdraw one,
or break the glass. Slice 2 adds three tools and changes no protocol:

| tool | reaches | operator verb |
| --- | --- | --- |
| `revl_escalate` | `escalate_ticket` | `approve` |
| `revl_revoke` (its new `hash` branch) | `revoke_ticket` | `approve` |
| `revl_override` | `override_ticket` | **`override`** |
| `revl_quorum` | `quorum_state` + the receipt | none (read-only) |

**Escalation and revocation gate under `approve`, and that is not laziness.**
Escalating CLOSES the vote path, and the path it leaves is the separately granted
override, so escalation only ever narrows authority: there is no authority to
address beyond the one to answer the question. Revoking a pending question
withdraws consent nobody gave yet, which is item 379's argument for gating a
grant's revocation under the same verb as its mint, applied to a question instead
of a grant.

**The override gets its own verb, and that is the decision.** It is the one path
that admits a class-(c) crossing WITHOUT the count its rule demands. `require N
of {...}` says N distinct named humans must answer; an operator trusted to cast
one of those N is not thereby trusted to stand in for all of them. Folding the
override into `approve` would hand every voter a one-operator bypass of the very
rule they vote under: the authority would be spelled `require N` and enforced
as `require 1`. So the emergency path is its own address in the profile: `may
approve on payments` authorizes votes on payments and no override at all, and
`may override on payments` is what an on-call operator holds. Its target set is
the crossing component, resolved through the same `_approve_targets` branch, not
the whole composition: an override decides ONE question about one candidate.

`revl_revoke` keeps two objects in one verb rather than growing a fourth tool,
because a pending QUESTION and a minted GRANT are both consent being withdrawn
under the same authority. They are separate branches, not one branch reading two
spellings: a `hash` never resolves to a grant and a `capability` never closes a
question.

### The receipt, and why it is derived rather than a tenth record kind

The item asks for the decision graph "in the WAL and receipt", and Slice 1 put it
in the WAL. What was missing was not the facts but the ARTIFACT: one hash-bound
document, for one crossing, that a reader can hold and check.

A second durable copy of facts the WAL already holds is a record that can
disagree with itself, and an audit that finds two answers has no answer. So the
receipt (`src/revl/mcp/quorum.py`) is a JOIN over the durable rows
(`quorum-open` for the binding, `quorum-vote` for each counted vote,
`quorum-refused` for each cast that was not, the closing row for the outcome, and
the `approval-granted` / `approval-consumed` pair for the authority and its
spend), carrying the binding, the rule as written, the proposer, every vote, every
refusal, the outcome, the override when there was one, and a `digest` over all of
it.

`verify_receipt` RE-DERIVES that body from the same rows and compares. The rows
it reads are the session's `_approval_records`, which `_record_quorum` writes in
lockstep with the WAL under the same keys, so there is one set of facts here and
not two: a verifier in a fresh process reads the log, and a verifier inside the
session reads the mirror, and they carry the same rows.

The two forgeries it exists for are different:

* edit a field and keep the digest, and the receipt does not hash to its own
  body: refused on `digest`;
* edit the binding AND recompute the digest, so the receipt is internally
  consistent and asserts a different candidate hash, is refused on `binding`,
  because the graph's own row carries the real one. **This is the property the
  item calls load-bearing, one level up:** a vote cannot float to another
  candidate, and neither can the receipt that says the votes happened.

A receipt whose request id names no question in the graph is refused as
`no-decision` rather than assumed, and a document of another kind or another
version is refused before its content is read at all.

**Admission time, not decision time.** The receipt is minted where the authority
is SPENT (`_consume_approval`, and `_commit_spends` for the activation gate's
two-phase spend), after the durable `approval-consumed` and before the fire. A
satisfied quorum that no crossing ever spent authorized nothing, and a receipt
naming a fire that never happened would be a false record. It is minted once per
request id: consume-before-fire spends an entry exactly once, so a second call
returns the artifact the first one minted rather than re-dating it.

### What a single-party crossing sees

Nothing. A rule that names no approvers and demands one opens no decision graph,
so it mints no receipt, and `revl_quorum` reports `quorum: false` with the reason
rather than inventing an empty graph. The clause is inert when it is not written,
which is Decision 1's third point holding through Slice 2.

## Slice plan

Slice 1, landed: the clause and its refusals in the policy; the session-side
protocol (`_open_quorum`, `_cast_vote`, `_decide_satisfied`, `_decide_denied`,
`_lapse_quorum`, `override_ticket`, `escalate_ticket`, `revoke_ticket`,
`quorum_state`); the nine WAL record kinds and their seq-free write; the `quorum`
sub-dict on `approval-granted`; the optional `vote` and `asToken` properties on
`revl_approve`; and `tests/test_471_quorum_approval.py`.

Slice 2, landed: `src/revl/mcp/quorum.py` (the receipt and the transport
adapters); the `revl_escalate`, `revl_override` and `revl_quorum` tools and
`revl_revoke`'s `hash` branch; the `override` operator verb and its target
resolution; `Session._mint_admission_receipt`, `quorum_receipt` and
`verify_quorum_receipt`; the doc surface for all four verbs; and
`tests/test_471_quorum_slice2.py`.

Not item 471, and still open: the second-operator transport that would let a
quorum be gathered from more than one bound operator, which is what closes the
`as_token` bound in Decision 5. It is a transport item (operator profiles and the
item 55 verb grammar), and Slice 2 does not narrow or widen it: `as_token` is
still the name of a cast, asserted by the caller, on every path including the
three new verbs.

## Exit tests

The item's exit criterion: "a `require 2 of {a,b,c}` rule admits only on two
distinct valid votes bound to the candidate hash, refuses a proposer-self
approval, and records the decision graph in the WAL." Pinned by
`tests/test_471_quorum_approval.py`: `test_two_distinct_votes_admit_the_crossing
_and_one_does_not`, `test_the_proposer_can_never_satisfy_quorum_alone`,
`test_a_single_vote_never_admits_a_quorum_ticket`, `test_the_minted_approval_is
_bound_to_the_candidate_hash`, and `test_quorum_state_reports_the_graph_and_the
_refusals`. The "no admission path may widen the rule" invariant of Decision 1 is
pinned by `test_a_quorum_gated_crossing_cannot_be_widened_into_a_standing_grant`,
`test_a_quorum_gated_capability_cannot_be_minted_proactively`,
`test_a_rule_that_only_names_approvers_also_refuses_a_standing_grant`,
`test_a_grant_minted_before_the_rule_was_bound_does_not_cover_it`,
`test_an_auto_approve_rule_never_covers_a_quorum_gated_crossing` and
`test_the_standing_grant_refusal_reaches_the_transport`, with
`test_a_single_party_crossing_still_takes_a_standing_grant` and
`test_an_auto_approve_rule_still_covers_a_single_party_crossing` holding the
other side of the line. The lease kind's one route is pinned by
`test_a_quorum_gated_lease_loads_only_after_its_own_votes` (refused before the
votes, admitted after, lease-tagged and bounded by the lease's own ttl and uses),
`test_a_quorum_gated_lease_is_refused_until_the_votes_arrive`,
`test_a_denied_lease_question_is_not_an_answer`,
`test_a_spent_lease_decision_answers_only_once`,
`test_the_lease_bridge_refuses_a_forged_or_foreign_decision`,
`test_the_lease_bridge_is_scoped_to_lease_tickets` and
`test_a_lease_decision_does_not_outlive_the_ticket_it_answered`, with
`test_a_lease_refusal_is_reasoned_and_names_the_route_that_answers_it` pinning
that the refusals carry the rule, the composition they leave open and the route
that unblocks them rather than failing bare.
`tests/test_capability_leases.py` cannot be exercised here (it is
`needs_cordis`-gated and the cordis runtime is absent), so the lease lifecycle is
driven in-process through `_enforce_lease_gate` rather than through `load()`.

Slice 2's own exits are in `tests/test_471_quorum_slice2.py`. The verbs:
`test_the_escalate_verb_closes_the_vote_path_and_names_its_outcome`,
`test_escalation_refuses_a_bystander_and_records_the_refusal`,
`test_the_revoke_verb_withdraws_a_pending_question` with
`test_revoke_still_retires_a_standing_grant` holding the other branch,
`test_the_override_verb_admits_without_the_count_and_says_so`,
`test_an_override_the_proposer_exercises_is_recorded_as_a_self_override`,
`test_an_override_with_no_stated_reason_is_refused_and_recorded`,
`test_an_override_of_a_decided_question_is_refused`,
`test_an_override_of_a_single_party_ticket_is_refused`, and the two
fail-closed tables over all three question-scoped verbs. The GATING, which is the
point of the separate verb: `test_the_override_verb_is_not_the_approve_verb` and
`test_the_override_verb_scopes_to_the_crossing_component`. The named outcomes:
`test_every_lifecycle_path_has_its_own_named_outcome` over all five, plus
`test_denial_closes_the_question_as_denied_and_names_the_deniers`,
`test_a_denial_that_leaves_the_count_reachable_leaves_it_open` and
`test_timeout_closes_the_question_as_expired`. The receipt:
`test_the_receipt_is_minted_at_admission_and_not_at_the_decision`,
`test_the_receipt_carries_the_whole_decision_graph`,
`test_the_receipt_verifies_against_the_durable_decision_graph`,
`test_an_edited_receipt_does_not_hash_to_its_own_body`,
`test_a_receipt_re_pointed_at_another_candidate_is_refused`,
`test_a_receipt_with_padded_votes_is_refused`,
`test_a_receipt_for_a_question_nobody_has_is_refused`,
`test_a_document_that_is_not_an_admission_receipt_is_refused`,
`test_the_override_receipt_says_override_and_carries_the_reason`,
`test_the_receipt_is_not_re_dated_by_a_second_read`, and
`test_a_single_party_approval_mints_no_quorum_receipt` for the inert side.

## Relates to

* 246: the two-step ticket this extends, and the class-(c) decision chokepoint.
* 251: the ledger the minted approval lands in.
* 427 F8: the dead latch the deadline reuses.
* 344 / 379: the standing grant and its revocation, deliberately distinct from a
  pending-question revocation.
* 476: which names 471 as a dependency.
* issue #823: this item.

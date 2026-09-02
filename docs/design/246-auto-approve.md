# Design: auto-approve-unless-irreversible, the permission policy as a type judgment (item 246)

Status: design proposed, revised after review. The policy decision table,
the decision chokepoint in `Session.call`, the activation gate on the
mutating verbs, the per-call approval two-step, the typed approval surface
with its five invariants, and the metrics are decided here. Item 246 needs
245 (landed 2026-08-26: the session owner, the commit manifest, the
hash-bound two-step, the class tags on the crossing surface). After 246 the
north-star order is 248 (the harness-boundary dogfood, the paper evaluation
of the whole 243-246 arc), then 252 (shell-to-witnessed, the biggest lever
on 248's number). The B foundation (294-full, 289, 256) queues behind those;
it does not belong in the exit-criterion's window.

## The one thing to get right

**The policy has no vocabulary for naming actions.** Its only input is the
checked effect class of the call, and its output is one of three postures:
proceed silently, proceed and enumerate at commit, or stop for a human. The
moment the policy can say "rm is fine" or "sendEmail is not", it is an
allowlist with extra steps, and the differentiating claim dies: an allowlist
cannot say which actions escaped it, and neither could we. Derivation is the
whole product. A call is class (a) because the checker proved a registered
inverse, class (b) because its `deferred` typing forbids it from firing
before the commit prompt, class (c) because it is an emission with neither.
Nothing at runtime, nothing in the harness, and nothing in this policy can
move a call between classes; 245 Decision 2 already made that a type
judgment, and 246 is deliberately just a reader of it.

The second half: **when a human is needed, the yes must be as checked as
the no.** A prompt whose answer is a boolean in harness memory can be
replayed for a different call, outlives the code it approved, and can be
spent by a confused deputy. So every approval 246 mints, whether the
operator-layer per-call ticket or the language-level `await approval`, is
bound to a hash of exactly what it approves, expires, dies with the
candidate it named, and cannot be replayed for another component. This is
the same bind-to-hash shape 245 Decision 4 built for the commit
(`revl_commit` enumerates, `revl_commit_confirm(hash)` fires, drift
refuses); 246 inherits it rather than inventing a second consent mechanism.

And the structural corollary the review sharpened: **the gate must sit
where the crossings actually happen, not where the tool names suggest they
do.** Class-(c) emissions fire from `revl_call`, from `revl_replay_forward`
re-invoking the same calls, and from activation bodies at every mutating
verb. A gate keyed on one tool name, or one that classifies only
provide-methods, is a policy with two open doors. Decision 2 fixes both:
the decision runs inside `Session.call` (the single chokepoint), and the
class map classifies activation reach so load and swap answer for their
own crossings.

## What already exists (the landed foundation this reads)

- **The three action classes, checked.** 245 Decision 2 fixed the class of
  an action as a total function of its extern classification: `witnessed`
  (243) is class (a), an `emission` extern with the `deferred` modifier is
  class (b), any other emission is class (c). The class rides the scope
  facts: every reached extern fact carries `class` (pure / acquire /
  emission / witnessed) and `deferred` (src/revl/query.py, the
  `"externs"` facts), and the crossing surface tags `actionClass`
  (src/revl/erase_report.py, `_crossings`). One gap, owned by Slice 1
  below: `_crossings` skips non-emission externs, so witnessed reaches are
  visible to the capability fixed point (src/revl/emission_analysis.py
  seeds `witnessed` externs into `_emitting_capabilities`) but not yet
  tagged `actionClass: "a"` on that aggregation.
- **The checked effect type at admission, at the right granularity.**
  `audit_diff.audit_report(ir)` builds the G8 surface from a compiled ir
  with no runtime; the MCP session already evaluates the item-33 sandbox
  over it before anything boots (src/revl/mcp/session.py,
  `_enforce_sandbox`). The per-scope granularity a per-call decision needs
  is in `query.Composition`: each component splits into its activation
  body and each provide-method (src/revl/query.py,
  `_scopes_of_component`), and every scope carries its emission and extern
  facts with `class` and `deferred`. That is the surface `_crossings`
  aggregates and `revl audit` prints. The MCP schema also derives a
  per-method reach (src/revl/mcp/schema.py, `_method_effects`:
  `reachesEmission`, `reachesHostCode`, `reachesCapabilities`), but that
  walk is advisory: it lacks the `*` first-class-value widening the
  `_emitting_capabilities` fixed point applies (emission_analysis.py
  widens a first-class reference to an emitting callable to `*`), so a
  callback-smuggled emission is visible to the fixed point and invisible
  to the schema walk. The class map below reads the checked surface, and
  `_method_effects` should converge on it, not the reverse (Decision 2).
- **The operator layer.** `operator.decide(session, tool_name, arguments)`
  gates every mutating management verb at the one dispatch point
  (src/revl/mcp/server.py, `handle`, before the handler runs), keyed by
  `TOOL_VERB`, refusing with a policy-style why-trace. `revl_call` is not
  in `TOOL_VERB`: driving the composition is ungated today. The item-55
  contract is back-compatibility by absence (no profile, no gate:
  `decide` returns ungated when no operator is bound, operator.py), and
  246 keeps that shape for the management verbs while Decision 4 names
  the one place absence-of-profile undermines this item.
- **The commit machinery 246 composes with.** `SessionOwner`
  (backends/python/runtime.py) holds the deferral queue, the discharge
  escrow, the live-frame registry, the hash-bound `manifest()` /
  `approve(hash)` two-step, and the `prompts` counters
  (`{"commit", "perCall", "residue"}`), with `perCall` explicitly reserved
  for this item. The manifest's `fired` list is 246's to fill (the
  class-(c) crossings already out, with 247's three-state tag: bare /
  compensated / unresolved).
- **The reversal semantics under the classes.** 243: a witnessed effect's
  inverse is checked, declaration-owned, WAL-serializable, idempotent on
  replay; 243 rule 6 states that a failed `restore` must surface a prompt
  or "auto-approved because revertible" silently degrades. 247: a
  compensation is audit-surface intention, never proof; a compensated
  emission is still class (c). 244: witnessed fs effects are confined to
  the session workspace root, which is what makes "auto-approve silently"
  survivable for fs authority at all (the roadmap's confinement note).

## Decision 1: the policy decision table

The decision is keyed on the action class of the call, where a call's class
is the worst class over every crossing its checked reach includes
(ordering: (c) > (b) > (a) > none). "Worst wins" because one prompt covers
the whole call or none of it, the same all-or-nothing rule admission and
the operator gate already use.

| class | derivation (checked) | policy posture | surfaced to the human | counted as |
|---|---|---|---|---|
| none | reach has no crossing: pure fns, `acquire` brackets only | proceed | never | not a boundary call |
| (a) revertible | every crossing is a `witnessed` extern with its registered inverse (243) | **auto-approve silently** | in the commit manifest's `witnessed` count; in the WAL | auto-approved-with-proof |
| (b) deferrable | every non-(a) crossing is a `deferred` emission (245) | **auto-approve; enumerate at commit** | one line of the 245 commit prompt (`summary`) | auto-approved-with-proof |
| (c) immediate | any emission crossing that is neither: fires at the call, no checked inverse (`compensate` does not change the class, 247) | **prompt per call** (Decision 2), or a standing typed approval (Decision 3) | before the call fires | prompted |

Three rows the table refuses to need:

- **An unclassified host call has no row** because it cannot exist: the
  checker refuses an extern without a classification, and a bare witnessed
  call that registers nothing is refused outside effect position (243
  rule 1). Escape from this table is a compiler bug, not a policy gap.
- **The unnameable reach `*`** (a bare `emission`, docs/capabilities.md) is
  class (c) with no narrowing: a boundary that cannot be named cannot be
  proven reversible, so it can never be auto-approved. This is the same
  stance the item-33 allow-lists take (`*` never satisfies a named allow).
  It also cannot be approved into: no policy rule can spell
  `capability * requires approval`'s inverse, no `await approval["*"]`
  exists, and the ticket for a `*` reach says so; the only yes a `*`
  crossing can receive is the per-call ticket, every time (exit test 17).
- **A failed inverse escalates after the fact.** Class (a) is
  auto-approved on the strength of the inverse; when an abort's `restore`
  itself fails, the restore-residue surfaces a prompt and increments
  `prompts.residue` (243 rule 6, teardown contract residue envelope). The
  auto-approval was still honest: the prompt happens exactly when the
  proof stopped holding, and says so.

## Decision 2: the decision chokepoint and the gates around it

**The class map: computed at admission, cached per generation, activation
scopes included.** At `load` and `swap`, alongside `_enforce_sandbox`, the
session builds a call classifier over EVERY scope of every component: each
provide-method AND each activation body, `(scope) -> {class, capabilities,
crossings}`. The derivation reads `query.Composition`'s per-scope facts
and the `_emitting_capabilities` fixed point over them (the reached
externs with their `class` and `deferred` flags, service emissions, and
the `*` widening for first-class function values): the same checked
surface `_crossings` aggregates and `revl audit` prints, so the map can
never disagree with the audit. It deliberately does NOT read the MCP
schema's `_method_effects`: that walk is advisory and misses the
first-class-value widening, so deriving the gate from it would let a
callback-carried emission through unclassified; `_method_effects` should
converge on the map's answer, not the reverse. The per-call decision is
then a dictionary lookup; nothing is compiled, walked, or re-derived on
the hot path. A new generation replaces the map atomically with the swap,
so a call decided against a stale map is impossible (the map and the
composition change together, under the same verb).

**The activation gate: load and swap are boundary calls too.** Class-(c)
emissions do not fire only from `revl_call`. An activation body runs its
effects at every verb that boots or reboots a generation: `revl_load`,
`revl_swap`, and the re-admitting verbs (`revl_edit`, `revl_undo`,
`revl_restore`), and today those verbs are gated only by operator
identity (`operator.decide`), never by effect class. Without this gate
the per-call prompt has a one-line bypass: move the emission into the
activation body of the component being swapped in, and the swap fires it
with no ticket ever issued. So under an enabled policy the class map's
activation-scope entries gate the mutating verbs themselves. A candidate
whose activation reach is class (c) does not boot: the load/swap response
becomes the same ticket two-step as a call, `{ok: false,
approvalRequired: true, ticket: {...}}` returned BEFORE any activation
body runs, the ticket naming the activation crossing; the approved
re-issue boots and the spend is consumed at the activation crossing
exactly as at a call crossing. A policy may instead refuse such a
candidate outright at admission, naming the crossing, for deployments
where activation-time class-(c) is never acceptable; either way nothing
fires before the answer. Class (a)/(b) activation reach follows the
table: (a) proceeds and counts, (b) enqueues, which is already
activation-safe under 245 (the enqueue is the only mid-session effect).
Exit test 12 pins the bypass shut.

**The gate walk is all-or-nothing (item 204).** A generation has many
activation bodies and the gate decides for all of them before any of them
fires, so the one-shot decide-spend-fire the per-call chokepoint uses does
not fit here: any body may refuse, and a spend made for an attempt that
then refuses has been charged to a boot that never happened. The walk
therefore RESERVES what covers each body (in memory, which makes the
record invisible to every later finder in the walk exactly as a real spend
would) and commits the whole set durably only once every body is covered,
still strictly before the generation is plugged. A walk that refuses
releases every reservation and writes nothing, so the ledger it leaves is
the ledger it found. Consuming as it went cost the operator 2**N - 1
prompts for N gated bodies, because each un-booted attempt ate the answers
already given and the retry re-asked them; the count is now exactly N,
over N + 1 load attempts. The reservation cannot leak authority: the walk
is straight-line synchronous code running before boot, so no crossing can
fire against a reserved token, and a released token is one that was never
spent, still spendable exactly once, at one crossing. Exit test 12b pins
the count and 12c pins the all-or-nothing.

**Where the gate runs: inside `Session.call`, not on a tool name.**
`operator.decide` answers *who may drive this verb* and stays at the
`server.handle` dispatch point, operator first: an operator who may not
act at all never sees an approval ticket. The new decision, *may this
specific call proceed without a human*, runs INSIDE `Session.call`, after
the target resolves and before `invoke()` runs. The placement closes a
real bypass: `Session.replay_forward` re-invokes `self.call(...)` for
every replayed call step (session.py), and a gate keyed on the tool name
`revl_call` at dispatch would never see those re-fired crossings, because
`revl_replay_forward` is not `revl_call`. With the decision in
`Session.call` there is exactly one chokepoint and every internal
re-invocation, present and future, passes through it. server.py still
carries a small hook so a refusal can shape the MCP response (the ticket
payload rides the tool result); if that hook is keyed by name it must
enumerate `BOUNDARY_TOOLS = {"revl_call", "revl_replay_forward"}`, and
the chokepoint in `Session.call` is the variant that stays correct as
internal callers are added, so the name set is a response-shaping detail
and never the gate itself. Exit test 13 covers the replay path.
`revl_call` stays out of `TOOL_VERB`: the management plane and the
boundary plane are different axes, and conflating them would let a
`may call` grant leak management authority or vice versa. The decision
reads the class map:

- class none / (a): proceed, count silently.
- class (b): proceed, count; the crossing surfaces at commit (the enqueue
  is the only thing that happens mid-session, 245 Decision 3, and
  dropping it on abort is free).
- class (c): look for a standing approval (Decision 3's ledger) bound to
  this call's ticket hash; found and valid, proceed and consume it;
  otherwise refuse with `approvalRequired` and the ticket.

**The per-call two-step (the class-(c) prompt).** MCP is request/response;
the server cannot block a call mid-flight waiting for a human. So the
prompt is the 245 shape, per call:

1. `revl_call` on an unapproved class-(c) target returns
   `{ok: false, approvalRequired: true, ticket: {...}}`. The ticket names
   what a yes would mean: component, key, method, an args digest, the
   capabilities reached, the crossing list, the candidate hash of the
   call's reach closure (Decision 3's one definition), and `hash`, a
   sha256 over the canonical JSON of all of it.
2. The harness relays the ticket to the human. `revl_approve(hash)` mints
   a ledger entry bound to that hash (Decision 3's binding, all five
   invariants). `revl_approve` joins `TOOL_VERB` under a new verb
   `approve`, so an operator profile scopes who may approve, exactly as
   `commit` scopes who may cross the session boundary.
3. The re-issued identical call recomputes the same hash, finds the
   standing approval, fires, and consumes it. A call with drifted args, a
   different method, or a swapped component recomputes a different hash
   and is refused with a fresh ticket: what the human approved is exactly
   what fires, never a superset. This is hash-binding doing the work
   revocation lists would otherwise do.

**The outstanding-ticket table.** `revl_approve(hash)` presents a bare
sha256; the capability, the component, the candidate hash, and the fields
the human saw are not recoverable from it. So the server retains every
ticket it issues, keyed by `hash`, and `revl_approve` REFUSES a hash it
did not issue: an approval can only be minted for a question the server
actually asked. Ticket lifetime follows the class map's: the table is
replaced atomically with the map at swap (the map and the composition
change together, under the same verb), so a ticket issued against a
previous generation is gone, not stale; the presented hash gets the
unknown-hash refusal and the caller re-issues the call for a fresh
ticket.

**The `approve` verb needs its own `_targets` branch.** `_targets` in
operator.py routes any verb without an explicit branch to
`_live_targets(ir)`, the whole live composition, and the authority check
is all-or-nothing over the targets. Left there, a subject-scoped grant
(`may approve on payments`) would be refused whenever ANY other component
is live, which makes scoped approval authority unusable in exactly the
multi-component sessions that need it. The branch resolves the presented
hash against the outstanding-ticket table to the ticket's component and
its realm labels, the same resolve-without-running pattern the `restore`
branch uses on the snapshot manifest (`_snapshot_targets`), and refuses
an unknown hash before the authority check runs.

**Back-compatibility, the item-55 clause.** With no approval policy
configured, the decision in `Session.call` returns ungated and every call
proceeds: today's behavior, byte for byte, every existing test green. The
policy is enabled at serve time (`revl mcp serve --approval-policy auto`,
or a policy file that names approval-required capabilities, Decision 3)
and is the intended default posture for agent sessions once 248's dogfood
has measured it; flipping the default is 248's call to propose, not this
item's to assume.

**An enabled policy requires recording.** A session has a WAL only when
it was loaded with `record: true`: `Session.load` constructs the
`Recorder` then or never (session.py), and the `SessionOwner`'s
`wal_getter` returns None without it. Without a WAL there is no
`approval-granted` record, no durable ticket spend (Decision 3's
consume-before-fire), and no answer to "which human decision authorized
this crossing"; a policy whose approvals evaporate is worse than none,
because it looks like one. So enabling the approval policy REQUIRES
recording: `revl mcp serve --approval-policy ...` refuses a `revl_load`
without `record: true`, and a policy file with a `requires approval` rule
admitted over a non-recording session refuses at load, each with a
diagnostic naming the requirement. Refuse, don't degrade: no in-memory
approval mode ships. Hash-binding and expiry would survive such a mode;
atomic consumption across a crash and the audit join would not, and a
mode that silently weakens invariant 5 is exactly what the
refuse-don't-degrade stance forbids.

**Unattended sessions.** Silence never approves (245 Decision 1). A
class-(c) call in a session with no one to relay the ticket to fails
closed with the ticket in the report. Whether an operator profile should
be able to pre-approve specific groups for unattended runs is 245 open
question 5, still open here, and 251 (approval distillation) is the
principled version of that answer.

## Decision 3: human approval as a typed effect

Item 246 folds external proposal #4: approval modeled in the language, not
as a UI condition beside it. The operator-layer ticket (Decision 2) is the
floor that needs no source change; the typed surface is how a component is
*written against* the requirement, and it is what makes the approval
confused-deputy-resistant rather than merely present.

**Who owns the requirement: the operator, not the author.** This is the
load-bearing choice. In the agent setting the component author is the
agent, so a declaration-owned requirement ("the author writes
`requires approval`") is a courtesy, not a floor: the agent can simply not
write it. The requirement therefore lives in the boundary policy file
(item 33), a new rule kind over the same capability tokens:

    capability production.payment requires approval
    capability prod.*             requires approval ttl 10m

evaluated at admission with the rest of the policy. The declaration-side
spelling also exists, for first-party code that knows its boundary is
sensitive, as a clause in the extern/method modifier position:

    extern emission[production.payment] fn charge(card: Str, amount: Int)
        requires approval

> **Shipped reality (item 343).** The dotted `production.payment` tokens
> above, and the `emission[production.payment]` scope, presume the
> realm-style scoped-emission capability that is the pending FEATURE half of
> roadmap item 343. In the shipped compiler an `emission` extern takes no
> `[caps]` scope — the parser refuses one (only `witnessed[caps]` is
> capability-scoped) — and an emission's capability token IS its extern name:
> the class map does `caps.add(name)`
> (`revl.mcp.approval.ClassMap._classify_direct`). So today a policy rule
> targets an emission by name and the declaration-side clause carries no
> bracket:
>
>     capability charge requires approval
>     extern emission fn charge(card: Str, amount: Int) requires approval
>
> (`tests/test_approval_typed.py` drives exactly this shape.) See
> docs/design/245-session-commit.md Decision 2 for the name-is-the-capability
> rule. The dotted spelling is kept in this section to show the grammar the
> 343 feature is meant to unlock.

The two compose as floor and acknowledgment: policy-imposed requirements
cannot be loosened from source, and a source-declared requirement holds
even with no policy file.

**The call-site surface.**

    let a = await approval["production.payment"] {
        amount: total, reason: "monthly invoice"
    }
    emit billing.charge(card, total) with a

- `await approval[C] { fields }` suspends until the operator grants or
  refuses, and yields a value of type `Approval[C]`. The fields are the
  human's evidence (rendered in the prompt), carried into the ledger and
  the WAL. The form is async-extern-shaped (the suspension machinery of
  items 80/115); it is the only producer of `Approval[C]`, and the type
  has no constructor, so an approval cannot be forged in-language.
- `with a` threads the approval to the crossing. The clause is the
  explicit dataflow that turns "unreachable without approval" into a type
  check instead of a new reachability analysis.

**The checker rules (the new obligation).** Enforced at lowering in
`_lower_provide`, beside the G4 capability-subset check, over the same
`_emitting_capabilities` fixed point (the reach set is already in hand;
the new work is an intersection with the approval-required set):

1. A call whose reach includes an approval-required capability `C` must
   carry `with e` where `e : Approval[C']` and `C` is within `C'`'s
   scope. A crossing without the edge is refused, naming the capability
   and the rule that requires it (policy line or declaration).
2. Transitivity is ordinary dataflow: a helper `fn` that performs the
   crossing takes `Approval[C]` as a typed parameter; the obligation
   checks at the crossing site and the type system carries the rest. A
   component reaching `C` with no approval edge anywhere is refused at
   admission (the policy evaluation over the audit graph, the same place
   the sandbox refuses today), before any runtime is touched. One edge of
   this rule, the service-mediated case, is deliberately deferred to open
   question 3 and fails closed meanwhile; see there.
3. `Approval[C]` is not storable across the session boundary: it may not
   appear in a snapshot, a handoff shape, or a spawn config (the runtime
   binding below makes a smuggled one worthless anyway, but the checker
   refusal keeps the honest program honest).

**The runtime binding.** A granted approval is a ledger entry, owned by
the `SessionOwner` beside the deferral queue and the escrow, and a WAL
record (`approval-granted`):

    {
      "requestId": ...,
      "capability": "production.payment",
      "component": <the crossing component's name>,
      "candidateHash": <the reach-closure hash, below>,
      "session": <session identity>,
      "fields": {...},           # amount, reason: what the human saw
      "grantedAt": ..., "expiresAt": ...,
      "consumed": false
    }

**`candidateHash` covers the reach closure, not one component.** A
component-scoped hash (sha256 of the crossing component's own semantic
entry) under-covers what the human approved: the behavior of the call
includes every provider it transitively reaches, and a swap of one of
THOSE providers (changed behavior, same names, caller untouched) would
leave every standing token for the caller alive. So `candidateHash` is a
sha256 over the canonical JSON of the semantic entries of the call's
REACH CLOSURE: the target's provider plus the providers of every required
service on the checked reach path of the call. Each entry is
`operator._semantic` (the IR entry minus `source`, exactly the
per-component equality `_changed_targets` uses to decide what a swap
touched), so the closure hash is a fold of comparisons the operator layer
already performs, over closure membership the class map's crossing facts
already name. The sound coarser fallback is the semantic hash of the
whole composition: it never misses a swap, at the cost of invalidating
tokens on swaps that could not affect the call; v1 may ship the fallback
first, but may not ship the component-scoped version. Decision 2's ticket
names this same hash and no other: one definition, both entry points.

At the crossing, the frame checks the token before the extern body runs:
unexpired, unconsumed, component matches the crossing frame, candidate
hash matches the live generation's reach-closure hash, session matches.

**Consume before fire, durably.** The ordering matters, and it is the
opposite of 245's flush bookkeeping. 245 records outcomes AFTER the fire
(`_flush` in backends/python/runtime.py: `d.fire()`, then
`wal.record_flushed(d.seq)`), and that is right for the deferral queue: a
flushed record before the fire would claim a crossing that may not have
happened, and the commit verdict, not a per-fire token, is the authority.
A single-use token inverts the stakes. If consumption rides the emission
record, a crash between the extern body and that record leaves the token
still valid on recover AND the emission already out; the retry fires
again on the same yes, and single-use was a lie exactly once. So the
spend is durable FIRST: an `approval-consumed` WAL record naming the
`requestId` is written and flushed before the extern body runs, then the
body fires, then the existing emission record is written naming the same
`requestId`. A crash between spend and fire leaves consumed-but-unfired:
an owed action that needs a FRESH approval, which is fail-closed (a human
is asked again; the world saw at most one fire on this yes). `revl
recover` reports a spend with no matching emission record in the 245
Decision 3 verdict shape: approval consumed, crossing unverified, owed.
The emission record still answers "which human decision authorized this
crossing" for every class-(c) crossing that needed one; the audit joins
the spend and the emission on `requestId`. That ledger is 248's
measurement substrate and 251's input. Exit test 14 cuts the WAL on both
sides of the spend.

**The five invariants, each with its mechanism:**

1. **Unreachable-without.** Static: the checker obligation above; a
   sensitive capability cannot be crossed without a value only
   `await approval` produces, and admission refuses a component with no
   approval edge on the path. Runtime, defense in depth: the frame check
   refuses the crossing without a valid token, so a hand-built IR or a
   backend bug still cannot cross silently (exit test 15 drives this half
   on its own).
2. **Hash-bound.** The token names `candidateHash`, computed over the
   call's reach closure against the live generation at mint time. The
   human approved this code and everything it reaches, not a component
   name.
3. **Expiring.** `expiresAt`, defaulted from the policy rule
   (`ttl 10m`; session-end at the latest, since `Approval` cannot be
   persisted). Checked at the crossing, not at mint, so a token that ages
   out mid-session refuses with a why-trace naming the expiry.
4. **Candidate-invalidates.** A swap, edit, or undo that changes ANY
   semantic entry in the closure changes the hash, so every standing
   token whose closure includes the changed provider fails the check,
   including a token held by an untouched caller whose transitively
   reached provider changed. No revocation bookkeeping exists to forget:
   the binding does the invalidating, the same trick as 245's
   stale-manifest refusal.
5. **Non-replayable.** The token names the component and the session, and
   is consumed at first crossing (single-use in v1), with the spend made
   durable before the fire (the ordering above), so not even a crash
   window exists in which a consumed token reads as fresh. A deputy
   component presenting a token minted for another component is refused;
   so is the same component replaying a consumed token, and so is any
   token from another session, including a later session over the same
   workspace and WAL. The refusal why-trace names which binding failed.

**The MCP seam for `await approval`.** The session drives the loop per
call (session.py: between tool calls the composition is idle), so a call
that suspends on an approval future returns
`{pending: true, approvalRequired: true, request: {...}}` with the fiber
parked; `revl_approve(requestId)` resolves the future, resumes the loop,
and returns the original call's result. Under `revl run` with a tty the
prompt is inline and synchronous. Unattended: refused, as in Decision 2.
The operator-layer ticket and the language-level request share the ledger
and the `revl_approve` verb: one consent mechanism, two entry points.

## Decision 4: the gate stack (composition with items 33 and 55)

Four gates, four questions, no overlap:

| gate | question | when | refusal |
|---|---|---|---|
| boundary policy (33) | may anything here ever reach C? | admission | refuses admission |
| operator capabilities (55) | may this operator drive this verb on this target? | verb dispatch | refuses the verb |
| **auto-approve policy (246)** | is this call reversible enough to proceed without a human? | per call, in `Session.call`, after 55 | returns the ticket |
| **typed approval (246)** | did a human say yes to this candidate for this crossing? | at the crossing | refuses the crossing |

Evaluation order per `revl_call`: operator gate, then class derivation,
then approval. Each refusal is a policy-style why-trace (revl.why), and
each is all-or-nothing over the call. `revl_approve` is itself gated by
55 (verb `approve`, with the `_targets` branch of Decision 2), so "who
may say yes" is scoped in the same profile grammar as "who may commit",
and an operator without `approve` cannot launder authority through the
prompt.

**Self-approval is the default identity model's hole.** With no operator
profile bound, EVERY verb is ungated: `operator.decide` returns ungated
whenever no operator is on the session (operator.py, the
`operator is None` early return), and that includes the new `approve`.
In that configuration the agent that received the ticket answers its own
ticket, and the class-(c) prompt is advisory, not a gate. Enabling the
approval policy is therefore only meaningful alongside an operator
profile that WITHHOLDS `approve` from the calling identity and grants it
to the human's; `revl mcp serve` with the policy enabled and no such
profile warns at startup naming the hole, and refusing outright is the
right default for agent-facing deployments (the warn-or-refuse choice is
a serve flag, the diagnostic is not optional). The `revl run` tty path is
exempt: its prompt is answered on the terminal, which is structurally a
human, not a verb the calling process can drive.

## Decision 5: the differentiating claim, stated carefully

Existing harnesses approximate this policy with allowlists ("these tool
names are safe") or coarse VM snapshots ("revert the disk"). Both fail the
same question: *which actions escaped?* An allowlist cannot enumerate the
side effects of an allowed tool; a snapshot cannot say which network
packets left before the revert. Here the decision is derived from the
checked effect type, and the residue enumeration is proof-backed,
because of three checked facts (they are 245 Decision 4's facts, inherited
verbatim):

1. every boundary crossing goes through a classified extern, and the G8
   surface enumerates every reached extern per component;
2. a class-(b) call has exactly one lowering, the enqueue, so the queue is
   the set of irreversible crossings the commit will cause;
3. class-(c) crossings are bracketed in the WAL (the `approval-consumed`
   spend before the fire, the emission record after, joined on
   `requestId`), so under this item every class-(c) crossing that needed
   an approval names the human decision that authorized it, and a spend
   with no emission record is a visible owed action, never a silent gap.

An action missing from the enumeration is a compiler bug, not a policy
gap. The honest caveats, so the paper claim survives review: reversal
does not undo observation, and concurrent-writer races are TOCTOU-bounded
(244's own caveats); a compensated crossing still left the system (247;
class (c) it stays); fs auto-approval is defensible because 244's
workspace-root confinement bounds *where*, and the shell remains an
all-emission surface until 252, which is exactly why 252 is the biggest
lever on 248's number.

## Decision 6: metrics

Counted where the prompts already are, in the `SessionOwner`:

- `prompts.perCall` increments on every class-(c) ticket returned and
  every `await approval` request surfaced (the reserved counter, live at
  last);
- `prompts.commit` and `prompts.residue` are unchanged (245 Decision 6);
- a new tally `approvals: {silent: n, atCommit: n, prompted: n}` counts
  boundary calls by posture at decision time.

The two headline numbers, reported in the commit manifest (`prompts`, and
246 now fills `fired` with the class-(c) crossings and their three-state
247 tags), in the abort report, and in `session.state()`:

- **percent auto-approved-with-proof** =
  `(silent + atCommit) / (silent + atCommit + prompted)`, over calls that
  reach at least one crossing (class none is not a boundary call and
  stays out of the denominator);
- **prompts-per-session** = the sum of the `prompts` counters; target ~1,
  and the claim is measurable: a session whose every action is class (a)
  or (b) shows exactly `{"commit": 1, "perCall": 0, "residue": 0}`.

248 measures both, before and after, on a real harness's tool layer. That
number is the exit criterion for the whole 243-246 arc; this item ships
the counters, 248 ships the evaluation.

## Slice plan

- **Slice 0: this doc.**
- **Slice 1: the operator-layer policy (py, MCP).** The per-generation
  class map built at load/swap from the query scope facts, activation
  scopes included; the approval decision inside `Session.call` plus the
  `BOUNDARY_TOOLS` response-shaping hook in server.py; the activation
  gate on the mutating verbs (the load/swap ticket two-step before boot);
  the class-(c) ticket, the outstanding-ticket table, and `revl_approve`
  (verb `approve` in `TOOL_VERB`, with its `_targets` branch resolving
  the ticket table); the approval ledger on `SessionOwner` with the five
  binding checks and the reach-closure hash; the `approval-granted` and
  `approval-consumed` WAL records and the `requestId` on emission
  records, in the consume-before-fire order; the
  enabled-policy-requires-recording refusal and the missing-`approve`
  profile warning at serve; the counters and the manifest's `fired`;
  `actionClass: "a"` for witnessed externs on the erase_report crossing
  aggregation (closing the noted gap). Off by default; the whole existing
  suite stays green with no policy configured.
- **Slice 2: the policy-owned requirement.** `capability C requires
  approval [ttl D]` in revl.policy (DSL and JSON), evaluated at admission
  beside the sandbox; the admission refusal for a component reaching an
  approval-required capability with no approval edge; the load-time
  refusal when such a policy meets a non-recording session.
- **Slice 3: the language surface (py tier).** `Approval[C]`,
  `await approval[C] { fields }` on the async-extern suspension seam, the
  `with` clause, the checker obligation in `_lower_provide`, the
  no-persistence rule, the frame-level crossing check with the durable
  spend. Ownerless tiers refuse `await approval` at emit with a 245-style
  tier-gate diagnostic (the suspension needs a session owner to route the
  request to); the static obligation checks on every tier. The
  service-mediated obligation edge is open question 3; the implementer
  should expect the fail-closed refusal described there and point the
  diagnostic at it.
- **Slice 4: 248 consumes.** The dogfood measurement; no new surface here.

## Exit tests

1. A witnessed effect auto-approves silently: no ticket, no prompt
   counter, the call proceeds, and the session's `approvals.silent`
   counts it.
2. A deferred emission auto-approves mid-session and appears in the
   commit manifest's `summary`; an all-(a)/(b) session ends with
   `prompts == {"commit": 1, "perCall": 0, "residue": 0}` and
   percent auto-approved-with-proof of 100.
3. An irreversible emission with no compensation prompts: `revl_call`
   returns `approvalRequired` with the ticket, the host body does not
   run; after `revl_approve(hash)` the identical call fires exactly once
   and consumes the approval; a second identical call is refused with a
   fresh ticket.
4. A compensated emission still prompts (class (c) unchanged by 247).
5. Hash-binding: an approval bound to ticket hash H is refused for hash
   H' (drifted args, different method, or a swap between mint and use,
   each covered); `revl_approve` on a hash the server never issued is
   refused by the outstanding-ticket table, and a ticket from a previous
   generation is unknown after the swap replaces the table.
6. An approval bound to candidate hash H is refused after a swap changes
   the component's semantic entry to H', with a why-trace naming the
   candidate-invalidates check; the same refusal when the swap changes a
   transitively reached PROVIDER on the call's checked reach path while
   the named component is untouched (the reach-closure hash, Decision 3).
7. Expiry: a token past `expiresAt` refuses the crossing (clock
   injected); non-replay: a token minted for component X presented by a
   deputy Y is refused, a consumed token is refused on reuse, and a token
   minted in one session is refused in a later session over the same
   workspace and WAL (cross-session replay).
8. Unreachable-without, static half: a component reaching a
   policy-declared approval-required capability with no `with` edge is
   refused at admission before any runtime boots; the declaration-owned
   spelling refuses at lowering the same way.
9. Operator composition: an operator whose profile lacks `approve`
   cannot mint an approval (the item-55 refusal shape); one with it can,
   and the granted crossing's WAL records name the `requestId`; a
   subject-scoped grant (`may approve on payments`) mints for a payments
   ticket while other components are live, and is refused for a ticket
   whose component sits outside the grant (the `_targets` approve
   branch).
10. No approval policy configured: byte-identical behavior to today,
    the whole existing suite plus the per-backend goldens green.
11. Restore-residue escalation: an abort whose witnessed inverse fails
    surfaces a residue prompt counted in `prompts.residue` (243 rule 6),
    so "auto-approved because revertible" never silently degrades to
    best-effort.
12. Activation gate: a swap candidate whose ACTIVATION body reaches a
    class-(c) emission returns `approvalRequired` with the ticket and
    does not boot (no activation effect runs); the approved re-issue
    boots and fires exactly once; the same emission moved from a
    provide-method into the activation body must not dodge the prompt,
    which is the bypass this test exists to keep shut.
13. Replay chokepoint: with the policy enabled, `revl_replay_forward`
    over a recorded class-(c) call is refused at the re-fired crossing
    with a ticket (the decision inside `Session.call` sees it even though
    the tool is not `revl_call`); the class-(a)/(b) steps of the same
    plan replay unhindered.
14. Crash-cut pair (the style of 245's test 5): cut the WAL after
    `approval-consumed` and before the emission record; recover reports
    the token spent and the crossing owed/unverified, and the retry
    demands a fresh approval, so no cut position exists where the token
    is valid while the emission is out. Cut before `approval-consumed`:
    the token is intact and nothing fired.
15. Unreachable-without, runtime half: a hand-built IR (no checker run)
    reaching an approval-required capability with no token is refused AT
    THE CROSSING by the frame check, independent of the static
    obligation.
16. Non-persistence: `Approval[C]` in a snapshot shape, a handoff shape,
    or a spawn config is refused by the checker; a value smuggled past it
    in a hand-built IR fails the session binding at the crossing.
17. The `*` row: a bare `emission` reach prompts as class (c), and no
    approval shape can name it: no policy rule grants it, no
    `await approval` form produces `Approval[*]`, and the per-call ticket
    is the only yes it can ever receive.
18. Recording required: enabling the approval policy over a session
    without `record: true` refuses at serve/load with the diagnostic
    naming the requirement; no in-memory ledger is silently substituted.

## Open questions (left deliberately)

1. **Unattended pre-approval.** Whether an operator profile may
   pre-approve capability groups for no-tty sessions (245 open question
   5). The principled answer is item 251's distillation (the ledger
   detects a repeated approval shape and offers a typed policy diff with
   its blast radius); until then, unattended class-(c) fails closed.
2. **Amount-scoped and multi-use approvals.** `approve up to 100` or
   `uses: n` bindings are expressible in the ledger entry but not
   designed here; single-use, exact-hash is v1. Widening a binding is a
   251-shaped decision because it is policy, not consent.
3. **Cross-component and service-mediated approval flow.** v1 refuses a
   token across components by construction (invariant 5). A workflow
   where component X legitimately brokers an approval for component Y is
   not supported; whether it should be a first-class delegation (with its
   own binding) or two approvals is open, and the confused-deputy default
   is the safe one. The typed surface meets the same question in
   service-mediated form: when an approval-required capability sits
   BEHIND a required service (the caller emits on `S`, and `S`'s provider
   performs the crossing), the `with` edge cannot thread across the
   interface unless the service method's signature itself carries
   `Approval[C]` as a parameter type, which v1 does not add. The checker
   obligation then refuses the provider at admission (it reaches `C` with
   no approval edge), so the gap is fail-closed either way: refused, not
   silently crossed. The Slice-3 implementer should expect exactly that
   refusal in the service-mediated case and point its diagnostic at this
   question.
4. **The ttl default.** 10 minutes is a placeholder; 248's dogfood
   should pick the number from real session lengths rather than this doc
   guessing it.

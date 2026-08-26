# Design: auto-approve-unless-irreversible, the permission policy as a type judgment (item 246)

Status: design proposed. The policy decision table, the operator-layer
decision point, the per-call approval two-step, the typed approval surface
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
- **The checked effect type at admission.** `audit_diff.audit_report(ir)`
  builds the G8 surface from a compiled ir with no runtime; the MCP session
  already evaluates the item-33 sandbox over it before anything boots
  (src/revl/mcp/session.py, `_enforce_sandbox`). Per provide-method, the
  MCP schema derives the observed reach from the checker
  (src/revl/mcp/schema.py, `_method_effects`: `reachesEmission`,
  `reachesHostCode`, `reachesCapabilities`), which is exactly the
  granularity a per-call decision needs.
- **The operator layer.** `operator.decide(session, tool_name, arguments)`
  gates every mutating management verb at the one dispatch point
  (src/revl/mcp/server.py, `handle`, before the handler runs), keyed by
  `TOOL_VERB`, refusing with a policy-style why-trace. `revl_call` is not
  in `TOOL_VERB`: driving the composition is ungated today. The item-55
  contract is back-compatibility by absence (no profile, no gate), and 246
  keeps that shape.
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
- **A failed inverse escalates after the fact.** Class (a) is
  auto-approved on the strength of the inverse; when an abort's `restore`
  itself fails, the restore-residue surfaces a prompt and increments
  `prompts.residue` (243 rule 6, teardown contract residue envelope). The
  auto-approval was still honest: the prompt happens exactly when the
  proof stopped holding, and says so.

## Decision 2: the decision point in operator.py

**The class map: computed at admission, cached per generation.** At `load`
and `swap`, alongside `_enforce_sandbox`, the session builds a call
classifier: `(key, method) -> {class, capabilities, crossings}` for every
provided operation, derived from the same scope facts `_method_effects`
reads (the reached service emissions, the reached externs with their
`class` and `deferred` flags). The per-call decision is then a dictionary
lookup; nothing is compiled, walked, or re-derived on the hot path. A new
generation replaces the map atomically with the swap, so a call decided
against a stale map is impossible (the map and the composition change
together, under the same verb).

**Where the gate runs: the same dispatch point as item 55, as a second,
orthogonal gate.** `operator.decide` answers *who may drive this verb*;
the new `operator.approve_call(session, name, arguments)` answers *may
this specific call proceed without a human*. Both run in
`server.handle` before the handler, operator first (an operator who may
not act at all never sees an approval ticket). `revl_call` stays out of
`TOOL_VERB`: the management plane and the boundary plane are different
axes, and conflating them would let a `may call` grant leak management
authority or vice versa. `approve_call` reads the class map:

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
   capabilities reached, the crossing list, the generation's candidate
   hash, and `hash`, a sha256 over the canonical JSON of all of it.
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

**Back-compatibility, the item-55 clause.** With no approval policy
configured, `approve_call` returns ungated and every call proceeds:
today's behavior, byte for byte, every existing test green. The policy is
enabled at serve time (`revl mcp serve --approval-policy auto`, or a
policy file that names approval-required capabilities, Decision 3) and is
the intended default posture for agent sessions once 248's dogfood has
measured it; flipping the default is 248's call to propose, not this
item's to assume.

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
   the sandbox refuses today), before any runtime is touched.
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
      "candidateHash": sha256(canonical JSON of the component's semantic IR),
      "session": <session identity>,
      "fields": {...},           # amount, reason: what the human saw
      "grantedAt": ..., "expiresAt": ...,
      "consumed": false
    }

`candidateHash` reuses the provenance-free comparison the operator layer
already has (`operator._semantic`: the IR entry minus `source`, exactly
the equality `_changed_targets` uses to decide what a swap touched). At
the crossing, the frame checks the token before the extern body runs:
unexpired, unconsumed, component matches the crossing frame, candidate
hash matches the live generation's semantic entry, session matches.
Consumption is marked atomically with the crossing's WAL record, which
also names the `requestId`: the audit surface can answer "which human
decision authorized this crossing" for every class-(c) crossing that
needed one. That ledger is 248's measurement substrate and 251's input.

**The five invariants, each with its mechanism:**

1. **Unreachable-without.** Static: the checker obligation above; a
   sensitive capability cannot be crossed without a value only
   `await approval` produces, and admission refuses a component with no
   approval edge on the path. Runtime, defense in depth: the frame check
   refuses the crossing without a valid token, so a hand-built IR or a
   backend bug still cannot cross silently.
2. **Hash-bound.** The token names `candidateHash`; the grant is computed
   against the live generation at mint time. The human approved this
   code, not this component name.
3. **Expiring.** `expiresAt`, defaulted from the policy rule
   (`ttl 10m`; session-end at the latest, since `Approval` cannot be
   persisted). Checked at the crossing, not at mint, so a token that ages
   out mid-session refuses with a why-trace naming the expiry.
4. **Candidate-invalidates.** A swap, edit, or undo changes the semantic
   entry, so every standing token for that component fails the hash
   check. No revocation bookkeeping exists to forget: the binding does
   the invalidating, the same trick as 245's stale-manifest refusal.
5. **Non-replayable.** The token names the component and the session, and
   is consumed at first crossing (single-use in v1). A deputy component
   presenting a token minted for another component is refused; so is the
   same component replaying a consumed token, and so is any token from
   another session. The refusal why-trace names which binding failed.

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
| **auto-approve policy (246)** | is this call reversible enough to proceed without a human? | per call, after 55 | returns the ticket |
| **typed approval (246)** | did a human say yes to this candidate for this crossing? | at the crossing | refuses the crossing |

Evaluation order per `revl_call`: operator gate, then class derivation,
then approval. Each refusal is a policy-style why-trace (revl.why), and
each is all-or-nothing over the call. `revl_approve` is itself gated by
55 (verb `approve`), so "who may say yes" is scoped in the same profile
grammar as "who may commit", and an operator without `approve` cannot
launder authority through the prompt.

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
3. class-(c) crossings are WAL-logged at fire, and under this item each
   carries the `requestId` of the approval that authorized it.

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
  class map built at load/swap; `operator.approve_call` and the
  `BOUNDARY_TOOLS` dispatch hook in server.py; the class-(c) ticket and
  `revl_approve` (verb `approve` in `TOOL_VERB`); the approval ledger on
  `SessionOwner` with the five binding checks; the `approval-granted` WAL
  record and the `requestId` on emission records; the counters and the
  manifest's `fired`; `actionClass: "a"` for witnessed externs on the
  erase_report crossing aggregation (closing the noted gap). Off by
  default; the whole existing suite stays green with no policy configured.
- **Slice 2: the policy-owned requirement.** `capability C requires
  approval [ttl D]` in revl.policy (DSL and JSON), evaluated at admission
  beside the sandbox; the admission refusal for a component reaching an
  approval-required capability with no approval edge.
- **Slice 3: the language surface (py tier).** `Approval[C]`,
  `await approval[C] { fields }` on the async-extern suspension seam, the
  `with` clause, the checker obligation in `_lower_provide`, the
  no-persistence rule, the frame-level crossing check. Ownerless tiers
  refuse `await approval` at emit with a 245-style tier-gate diagnostic
  (the suspension needs a session owner to route the request to); the
  static obligation checks on every tier.
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
   each covered).
6. An approval bound to candidate hash H is refused after a swap changes
   the component's semantic entry to H', with a why-trace naming the
   candidate-invalidates check.
7. Expiry: a token past `expiresAt` refuses the crossing (clock
   injected); non-replay: a token minted for component X presented by a
   deputy Y is refused, and a consumed token is refused on reuse.
8. Unreachable-without: a component reaching a policy-declared
   approval-required capability with no `with` edge is refused at
   admission before any runtime boots; the declaration-owned spelling
   refuses at lowering the same way.
9. Operator composition: an operator whose profile lacks `approve`
   cannot mint an approval (the item-55 refusal shape); one with it can,
   and the granted crossing's WAL record names the `requestId`.
10. No approval policy configured: byte-identical behavior to today,
    the whole existing suite plus the per-backend goldens green.
11. Restore-residue escalation: an abort whose witnessed inverse fails
    surfaces a residue prompt counted in `prompts.residue` (243 rule 6),
    so "auto-approved because revertible" never silently degrades to
    best-effort.

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
3. **Cross-component approval flow.** v1 refuses a token across
   components by construction (invariant 5). A workflow where component X
   legitimately brokers an approval for component Y is not supported;
   whether it should be a first-class delegation (with its own binding)
   or two approvals is open, and the confused-deputy default is the safe
   one.
4. **The ttl default.** 10 minutes is a placeholder; 248's dogfood
   should pick the number from real session lengths rather than this doc
   guessing it.

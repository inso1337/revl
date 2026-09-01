# 310: capability-aware caching

Roadmap item 310 (external proposal #9), design pass with the 243-261 arc.
The language distinguishes `cache` on a pure function, on a capability-backed
deterministic result, and on an external read, and it REQUIRES freshness
semantics for a cached external result. A cache changes authority and
consistency, not just performance: what an author may safely memoize is a
function of the EFFECT CLASS of the thing memoized, and revl's checker already
knows that class. This is design-first; nothing here is implemented.

The motivating gap is visible in the tree today: `examples/handoff_cache.rvl`
is a hand-rolled cache component (a `Map`-backed `Store`). It is a fine state
container, but nothing about it says what may be cached in it, for how long,
under whose authority, or when an entry is a lie. Every one of those questions
has a wrong default, and the wrong defaults are exactly the agent-memory,
service-discovery, and registry-resolution bugs the proposal names.

## The one thing to get right

A cache HIT must not bypass the authority a cache MISS requires.

If `f()` reaches `emission[net]` and its result is cached, a later caller that
gets a hit obtains the net-derived value WITHOUT crossing net this time. If
any authority surface (the approval class fold, the policy reach, the taint
fold, the audit boundary) sees only the miss path, the hit path is a laundered
capability: reached but not seen. This is precisely the 414 hole class (the
spawn-fold instance was A x 2: a spawned class-(c) emission that skipped the
246 prompt because the fold followed one seam and not the other). Item 310
must not mint crossing kind 11 with the same defect it was born to avoid.

The resolution, stated up front and elaborated in its own section: the
capability check fires on EVERY access, hit or miss. Statically, `cache` is
metadata on a crossing, never a subtraction from checked reach: the emission
fixed point (`emission_analysis.py`) is unchanged in shape, so every fold in
the 414 surface table sees a cached crossing identically on both paths.
Dynamically, a cache entry is scoped to the authority that produced it: an
access that does not hold live, covering authority cannot hit, and an entry
dies with the grant, approval, or composition generation that covered its
miss. A hit is a re-delivery of an already-authorized crossing's result, it is
never a way to obtain one without the authorization. WHERE that dynamic
check can fire is not a free choice: the shipped consent protocol decides
the transaction at the seam, before execution, so the stated semantics are
implementable today only where the hit/miss decision is visible to the seam
gate. The enforcement-point section derives the slice restriction from that
fact instead of pretending the runtime can ask a ledger it cannot reach.

## Background: what already exists, with the seams named

- **The effect classification.** Externs are `pure`, `acquire`, `witnessed`,
  or `emission` (plus `deferred` on an emission). The emission fixed point
  computes, per function, the set of boundary crossings its checked reach
  includes, and the a/b/c action classes (docs/harness-gate-guide.md) are a
  worst-over-reach fold over that set. Nothing at runtime can move a call
  between classes. 310's cache classes are the same kind of derived fact.
- **Author claims about host behavior (items 44, 309).** `idempotent` on an
  emission is a checked-shape, claimed-behavior declaration: the compiler
  verifies where it may appear, the author asserts what the host does, and the
  honest ledger says which is which. `cache` on a non-pure callee is the same
  trust tier, and this doc states so rather than pretending the checker can
  see inside a G8-opaque host body.
- **Durations and leases (item 294, shipped 246/344/379 runtime).**
  `policy.py` already parses `ttl <n>[ms|s|m|h]` for approval rules; the grant
  ledger already enforces `expiresAt` at the crossing and `remainingUses`
  consume-before-fire; 294's value order already says a shorter ttl is below a
  longer one. 310's `ttl` reuses all three: the spelling, the enforcement
  point, and the attenuation order. No parallel duration mechanism (the
  source-side LEXING of a duration is a small new spend, surface section:
  `_parse_ttl` is a policy string parser, not an rvl token).
- **The consent seam (245/246, shipped).** The whole authority transaction
  happens at the seam, once per top-level call, before execution:
  `_approval_decide_call` (mcp/session.py) classifies the call and durably
  consumes a grant use or an approval before `invoke()` runs. The runtime
  that executes the body has no ledger access at all; it sees only the
  process-global `_SESSION_OWNER` (backends/python/runtime.py). Consumption
  is per-call-atomic and refusal fires before any work starts. Any construct
  wanting a per-access authority decision must either live at that seam or
  build a crossing-level ledger transaction that does not exist today; this
  constraint shapes 310's implementable slice (enforcement-point section).
- **Idempotency keys (item 309).** A per-call dynamic key in a declared
  parameter role, grammar not manifest, because re-runnability is author
  knowledge. 310's freshness clauses sit in the same crux-table cell.
- **Taint (item 249).** Values carry origin provenance through an
  interprocedural fold; surface F of the 414 matrix. A cached value is a
  value; it does not get a provenance amnesty.
- **Witnessed inverses (item 243) and G7.** A witnessed crossing registers an
  inverse in the escrow; teardown completeness is derived. Any construct that
  can skip a witnessed crossing's execution skips its registration too.
- **414 reach-completeness.** Every authority-derivation surface must visit
  every crossing kind. 310 adds a crossing kind and therefore a row review.

## The three cache classes (question 1)

The class vocabulary is the roadmap's: `pure_fn`, `capability_result`,
`external_effect` (the IR values). The source spells them `cache pure`,
`cache capability`, `cache external` (surface section). The class an author
spells is CHECKED against the callee's derived reach, and a mismatch is
refused in both directions: one word, one meaning, visible in audit.

What the checker can and cannot derive, honestly: `pure` is fully checkable
(the emission fixed point says the reach crosses nothing). The boundary
between `capability` and `external` is NOT derivable: whether a host body's
result is a function of its arguments is a fact about the host, and host
bodies are G8-opaque. So `pure` is verified, and `capability` vs `external`
is an author claim checked for shape, stated as such, with the recording
roundtrip (item 37 property tests) as the out-of-band verification path,
exactly the 44/309 posture. The fail-closed default follows: an emission with
no cache declaration is not cacheable at all, and the checker never infers
the stronger claim from the weaker.

### `cache pure` (class `pure_fn`)

Memoization of a pure function. Sound by construction:

- **Applicability (checked).** The callee's checked reach crosses nothing: no
  emission, witnessed, or acquire crossing, directly or transitively. A `fn`
  whose reach includes any crossing refuses `cache pure`.
- **Soundness condition.** G6 gives it outright: bodies outside effect forms
  are pure, captures are by value, no revl value is ever mutated in place. So
  for equal argument values the result is equal, always, and replacing a call
  with a stored result is observationally equivalent. Nothing else is needed.
- **Key.** A structural digest of the argument values. revl values are
  immutable, so the digest is well-defined; the 246 ticket's args digest is
  the shipped precedent for hashing a call's arguments.
- **Freshness.** None, and none may be written: `cache pure ttl 5m` is
  refused (a pure result cannot go stale; a ttl on it is a category error and
  probably a misclassified external read).
- **Authority.** No crossing, no capability, no laundering surface. This
  class is the only one with no ledger interaction.

### `cache capability` (class `capability_result`)

A result computed WITH a capability but deterministic given inputs: a
registry resolution against a pinned index, a signature over a key handle, a
compilation via a gate service. The crossing is real (the host is consulted),
the claim is that the answer is a function of the arguments for as long as
the authorizing scope stands.

- **Applicability (checked shape).** Every crossing in the callee's reach is
  an emission crossing, and each carries the author's `cache capability`
  claim or is pure. A reach that includes a witnessed, acquire, `deferred`,
  or `compensate`-declaring crossing refuses (reconciliations section). Worst-over-reach applies: one
  undeclared crossing anywhere in the reach and the whole callee is
  uncacheable, the same shape as the a/b/c fold.
- **Soundness condition.** Three conjuncts, and all three are enforced, not
  assumed:
  1. determinism-given-args within the scope (the author's claim, honest-
     ledger tier);
  2. the entry key includes the FULL authority context: the capability token
     with its 294 parameter valuation, the grant id that authorized the miss,
     and the composition generation. Two callers under different grants never
     share an entry; a swap invalidates everything, the same way it
     invalidates a standing approval;
  3. the capability check fires on every access. A hit is admitted only
     after the same classification and ledger-liveness checks a miss
     performs, minus only the host body and the use consumption. No live
     covering grant, no hit: the access takes the miss path and THAT is
     refused exactly as an uncached call would be. Where in the shipped
     protocol this check can run is derived, not chosen, in the
     enforcement-point section, and it restricts the implementable slice
     to seam service methods.
- **Freshness.** No `invalidated_by`/`ttl` required: the scope bound IS the
  freshness bound. The entry dies when its authorizing grant dies (expiry,
  revocation, exhaustion) or the composition generation changes. A ttl MAY be
  added to narrow further; it can only narrow (294's order), never extend an
  entry past its grant.
- **What a hit does not do.** It does not consume `remainingUses` (the count
  lease bounds boundary crossings, and a hit crosses nothing), and it cannot
  be used to stretch a count lease either: an exhausted grant is not live, so
  its entries are already dead. A one-use lease therefore yields one miss and
  zero hits. Fail closed, no residue of authority. This bullet is only
  satisfiable where the gate can see the entry BEFORE it consumes, which is
  the seam; the enforcement-point section owns the mechanics.

### `cache external` (class `external_effect`)

A result from an external read: a user profile, a price, a DNS answer, agent
memory. The external world changes without notifying the composition, so a
stored copy is a claim about the past presented as the present. Unsound to
cache without explicit freshness, and refused without it.

- **Applicability (checked shape).** As `cache capability` (emission-only
  reach), with the `external` claim: the result depends on mutable state
  outside the composition. This is the weakest claim and therefore the
  default posture for any emission read; `capability` is the stronger claim
  an author must opt into.
- **Soundness condition.** Everything in `cache capability`'s conditions 2
  and 3 (authority-scoped entries, per-access checks), PLUS a declared
  staleness bound. An entry is valid until the FIRST of: its `ttl` expiring,
  a crossing of any `invalidated_by` token being observed, its authorizing
  grant dying, the composition generation changing, or (for an
  approval-required token) the approval that covered its miss expiring. The
  cache never answers from an entry past any of these lines; past the line,
  the access is a miss and re-crosses under full authority.
- **Freshness (REQUIRED).** At least one of `invalidated_by <token>` and
  `ttl <duration>` must be declared, or the program is refused at compile.
  This is the load-bearing rule and it is a refusal, not a warning or a
  default: a default ttl would be the compiler inventing a consistency model
  the author never stated.

## The freshness clauses and the refusal (question 2)

### `invalidated_by <token>`

Names an emission in the shared capability-token namespace (the same dotted
tokens `_capability_token` parses for scopes, policy rules, and grants; the
294 shared-namespace requirement). Semantics: the runtime observes every
boundary crossing it already mediates; when a crossing of the named token
fires anywhere in the composition, every cache entry declaring that token is
invalidated before the crossing's result is delivered. The write and the
invalidation are ordered by the same WAL that orders the crossing itself, so
a session never reads its own stale write.

`cache user_profile invalidated_by user.updated` therefore means: any
crossing of the `user.updated` emission kills the cached profiles. An event
that originates OUTSIDE the composition reaches it the only way anything
does, as a call on a provided service whose reach crosses `user.updated`; at
that crossing the invalidation fires. A change in the world that never enters
the composition through any declared boundary is invisible by construction,
which is exactly why `ttl` exists and why the two clauses compose (belt and
suspenders: `invalidated_by user.updated ttl 5m` bounds staleness at five
minutes even if the update event never arrives). Cross-composition
invalidation (multi-realm state, the proposal's fourth use) is scoped out to
the federation surface; the token grammar is deliberately the shared one so
it extends there without a new spelling.

An `invalidated_by` token must resolve: naming a token no service or extern
in the composition can ever cross is refused at admission (a subscription
nothing can fire is a freshness claim that can never come true; misspelled
tokens must not fail open into an effectively-ttl-less cache).

Scope, stated bluntly: "ordered by the same WAL" presumes ONE WAL. The
entry store is per-session and single-process. A single composition placed
across processes (the 414 kind 5/6 seams, distribute.py) has a child firing
its own extern crossings locally, where no shared WAL orders an
invalidating crossing against an entry held in another process. Until the
federation surface lands, a distributed placement REFUSES `cache` on any
method the placement splits from its invalidating crossings (admission
time, next to the existing distribution checks): a per-process private
cache with cross-process invalidation traffic is exactly the stale-read
bug this clause exists to prevent, and refusing is cheaper than a
consistency protocol nobody has designed.

### `ttl <duration>`

The shipped duration spelling, `<n>[ms|s|m|h]` with a bare number meaning
seconds (`policy.py` `_parse_ttl`), and 294's value order (shorter below
longer, bounded below absent). Enforcement is at the access, the same point
`expiresAt` is checked for grants (`_live_grant_for` invariant): an entry
older than its ttl is not consulted. Reuse is total: no second duration
grammar, no second clock discipline.

### The per-class requirement table

| class | freshness clauses | rule |
|---|---|---|
| `cache pure` | none allowed | a pure result cannot go stale; a clause is refused |
| `cache capability` | optional `ttl` (narrowing only) | the scope bound (grant + generation liveness) is the freshness bound |
| `cache external` | at least one of `invalidated_by`, `ttl` REQUIRED | refused at compile without one |

The refusal (compile-time, checker):

```
cache external requires a freshness bound: declare `invalidated_by <token>`
and/or `ttl <duration>`: an external result changes without notice, so an
unbounded cache serves the past as the present
```

And the mismatch refusals: `cache pure` on a callee whose reach crosses a
boundary ("the reach of `f` crosses `net.get`: a crossing result is not
pure; declare `cache capability` or `cache external`"), and `cache
capability`/`cache external` on a pure callee ("the reach of `f` crosses
nothing: declare `cache pure`", keeping the class words honest on the audit
surface).

## The capability-laundering hazard, resolved (question 3)

The brief's dichotomy is: either caching is restricted to pure/deterministic
results (no laundering possible), or the capability check fires on every
access. The first horn discards the item's entire value (external reads are
the use cases named in the proposal). 310 takes the second horn, and takes it
at every layer where 414 says a fold can go blind:

1. **Static reach is never reduced.** `cache` lowers to metadata on the
   crossing (`"cache": {"class": ..., "invalidated_by": [...], "ttl_ms": ...}`
   next to the 44/243 descriptor fields). The emission fixed point does not
   read it. Consequences, one per 414 surface: the approval ClassMap fold
   still classifies a cached class-(c) emission as class (c); policy
   `component_reach` still lists the token; the G8 audit boundary still
   prints the crossing (now with its cache line); the untrusted-author sweep
   still counts it; the seam refusal still sees a resource in its arguments;
   the taint fold still records the origin; the attenuation algebra still
   compares its valuation. A component that reaches `net` through a cached
   read reaches `net`, full stop.
2. **Admission classification is unchanged; consumption is hit-aware.** A
   caller needs the same capability grant to call a cached-read method as an
   uncached one, and there is no "read-through cache" service a
   low-authority component can require instead. The unqualified claim
   "admission is unchanged" is dropped, because it is false in one precise
   step: the seam gate checks entry liveness BEFORE it consumes a use, so a
   live hit skips the consumption a miss performs. Everything upstream of
   that step (classification, policy reach, refusal posture) is byte-for-
   byte today's; the enforcement-point section specifies the one reordered
   step and why it can only live at the seam.
3. **Entries are authority-scoped, not ambient.** The entry key includes the
   grant id and composition generation (soundness condition 2 above). The
   hazard's sharpest sub-case, caller A misses under grant GA, caller B holds
   no grant and asks the same question, is answered by construction: B's
   access has no live covering grant, so B cannot hit, and B's miss path
   refuses exactly as today. A hit is only reachable through the same door
   the miss went through.
4. **Consent is not outlived.** For an approval-required token (246), the
   entry records the approval that covered its miss and dies when that
   approval's ttl does. A cache must not convert one human `yes` into an
   unbounded stream of silent answers; it converts one `yes` into answers for
   exactly as long as the operator said a `yes` lasts. A class-(c) cached
   emission still prompts per miss; what the operator's `requires approval
   ttl` bounds is precisely how long the hit window stays open. One shipped
   fact makes this promise non-trivial: standing approvals are single-use
   (`_consume_approval` sets `consumed` after one use), so the approval
   that covered a miss is consumed the moment the entry is born, and a
   naive "hit requires an unconsumed approval" rule would close the window
   after zero hits. The enforcement-point section decides the rule
   deliberately: entry liveness binds to the approval record's ttl and
   revocation, not its `consumed` flag.
5. **Hits are on the record.** Every hit writes a WAL entry naming the entry
   and the miss crossing it re-delivers, and `session.state()` counts hits in
   their own counter (never folded into `silent`, which would overstate the
   auto-approved-with-proof number). `revl audit` prints the cache line on
   the boundary: class, clauses, and enforcement status per the honest
   ledger.
6. **414 gets a row.** "Cached emission, hit path" becomes crossing kind 11
   in the `CROSSINGS` table, in scope for every surface A-G, with A x 11
   (class fold sees the hit path) and F x 11 (taint fold sees it) as the
   high-value cells. The guard test forces the scope decision on every
   surface, so the next fold written cannot forget the hit path silently.
7. **414 gets a column too: the applicability fold is surface H.** The fold
   that computes "is this callee's reach cacheable" is itself an
   authority-derivation surface, and "plausibly correct if implemented over
   the ClassMap closure" is exactly the hand-wave the matrix exists to
   eliminate. Surface H is REQUIRED to be a worst-over-reach fold over the
   SAME closure the ClassMap folds, and it gets a 414-cell test per
   crossing kind, pinned: does the refusal follow the spawn/instance-get
   seam (kind 2)? the transitive closure (kind 4)? the `*` widening
   (kind 8)? Each kind answers with a test, not a sentence.
8. **The entry store is a value-copy surface (the E x 7 shape).** An entry
   copies a result (and digests args) into a store that outlives the call,
   which is the same hazard class as surface E's resource-in-seam-arguments
   refusal: a resource handle stored in an entry would be stored authority,
   re-deliverable to a later access. The applicability fold refuses a
   resource-carrying result or parameter type with a STRUCTURAL walk
   (nested records, variants, generic instantiations), never a type-name
   match: a resource renamed or wrapped two levels deep refuses identically.

What is deliberately NOT claimed: that the runtime can verify the host's
determinism claim (`cache capability`) or the completeness of an
`invalidated_by` subscription against the real world. Those are author
claims with an out-of-band verification path, and the audit surface says so.

## The enforcement point: consume-before-fire is load-bearing

The laundering section says WHAT must hold on a hit. This section says WHERE
it can hold in the shipped protocol, because the obvious placement cannot.

Today the whole authority transaction is at the seam, once per top-level
call, before execution: `_approval_decide_call` classifies the call and
durably consumes a grant use or an approval before `invoke()` runs, and the
runtime executing the body has no ledger access (only the process-global
`_SESSION_OWNER`). The obvious placement for the hit/miss decision is deep
in the runtime crossing, where the argument digest first exists. That
placement makes two claims of this design JOINTLY UNSATISFIABLE: "a hit
does not consume `remainingUses`" and "admission is unchanged". The seam
has already consumed a use by the time the runtime discovers the hit.
Repairing that by moving consumption into the crossing is worse: it
silently restructures 245/246 consent from per-call-atomic to
per-crossing, and it makes a refusal fire MID-body with partial work
already escrowed, two changes to shipped semantics this item has no
mandate to make.

The resolution is scoping, not machinery. The implementable slice is
`cache` on a SEAM SERVICE METHOD, where the call IS the crossing: the args
the seam already holds are the cache key, so the gate can act
pre-execution, in the exact spot `_approval_decide_call` runs today:

1. check entry liveness against the ledger the seam already owns;
2. on a live hit: deliver the entry, skip consumption, write the hit WAL
   record; no body runs;
3. on a miss: consume-before-fire and execute, byte-for-byte today's path.

Per-call atomicity, refusal-before-work, and hit-does-not-consume hold
simultaneously because the decision happens where the ledger lives. The
stated semantics work THERE, today, with a one-step reorder inside the
gate.

Three sharp edges at the seam, each decided here rather than discovered in
review:

- **Single-use approvals.** `_consume_approval` sets `consumed` after one
  use, so the approval covering a miss is consumed when the entry is born.
  Decision: an approval-covered entry's liveness binds to the approval
  record's ttl and revocation, NOT its `consumed` flag. The flag means
  "this approval's one authorized crossing happened", and that crossing is
  the miss that populated the entry; a hit re-delivers that crossing's
  result and needs no unconsumed use. One `yes` buys one miss plus hits
  for the same args until the ttl lapses; any new miss prompts again,
  exactly as today. The grant-side asymmetry is deliberate: an exhausted
  count lease (`remainingUses` 0) IS a liveness event and kills its
  entries, because a count lease bounds obtainable results while an
  approval ttl declares a time window.
- **Multiple covering grants.** `_find_standing_grant` returns a LIST. An
  entry records the ONE grant id the seam actually consumed on its miss,
  and hit liveness is checked against that recorded grant only. A hit
  never shops among covering grants: entry authority is the authority
  that produced it, not any authority that could have.
- **No-policy sessions.** A session with no policy has no ledger, so a
  `cache capability` or `cache external` entry would have no authority
  scope to be keyed on or to die with. Decision: no ledger, no entry
  store. Every access takes the miss path and the declaration is
  dynamically inert (compile-time checks still run). Fail closed into
  correctness: an unscoped entry is exactly the ambient store the
  laundering section forbids. `cache pure` is unaffected (it has no
  ledger interaction by construction).

The extern-deep-in-the-reach case, a cacheable crossing INTERIOR to the
called body where only the runtime sees the args, is real and is NOT
specified here. Making the semantics hold there needs a crossing-level
ledger transaction that does not exist: an emitted cache-step wrapper in
the `_witnessed_step` mold, an owner-carried liveness/consume callback so
the runtime can ask a question it currently cannot, WAL record ordering
between the seam's consumption record and the crossing's hit record, and
mid-execution refusal semantics for a crossing whose authority died
between admission and arrival. That is a later slice with its own design
pass (staged plan). Until it lands, the grammar still admits `cache` on an
extern declaration, but admission refuses it when the declared crossing is
not itself the seam method's direct body: "cache on an interior crossing
is not yet enforceable; declare it on the seam method". Grammar-forward,
enforcement-honest.

## Surface (question 4)

### Decision: source grammar, trailing clause

By the 294/309 crux table (placement follows which party knows the fact):
what may be memoized, and what event makes it stale, are facts about the
computation and its remote API, author knowledge, exactly like 294's
resource parameters and 309's idempotency claim. Source, not manifest. The
operator's levers remain grants and policy: an operator can narrow (revoke a
grant and the entries die; a policy approval ttl bounds the hit window) but
never has to author freshness semantics for code they did not write. A
policy rule to cap a declared ttl per token is listed as an open question,
not required for soundness (the grant lifetime already provides the ceiling).

The clause trails the signature, the `undo`-clause precedent, because it
carries arguments and the modifier head slot is already load-bearing
(`emission deferred idempotent` and 294's parameter brackets live there; a
three-clause cache head would be unreadable):

```revl sketch
// pure memoization: the whole clause is one word plus the class
fn shade(base: Color, light: Vec3) -> Color cache pure = ...

// capability-backed deterministic read: scope-bound, ttl optional
extern emission[registry.resolve] fn resolve(name: Str) -> Pin
    cache capability = @py { ... }

// external read: freshness REQUIRED, both clauses shown
service Users {
  emission fn get_profile(id: Str) -> Profile
      cache external invalidated_by user.updated ttl 5m
}
```

Grammar spend: one production, `cache` (`pure` | `capability` | `external`)
(`invalidated_by` token (`,` token)*)? (`ttl` duration)?, accepted after the
return type of a `fn`, an extern declaration, or a service-method
declaration, before `=` or `undo`. `cache` is a CONTEXTUAL keyword,
recognized only in that post-return-type clause slot, and NOT added to the
lexer's KEYWORDS set: this design's own motivating file uses `cache` as a
provided KEY name (`provides cache: Store`, `handoff cache`,
`provide cache` in examples/handoff_cache.rvl), and a global reservation
would break it and every user file shaped like it. The clause slot is
unambiguous (after a return type only clause heads may appear), so the
contextual read costs one token of lookahead, cheaper than the in-tree
migration plus the silent user-code break. `compensate` never contends for
the slot: a `compensate`-declaring emission is in the refused reach
(reconciliations section), so the two clauses cannot legally co-occur on
one declaration and no ordering rule between them is needed, worth saying
because both trail the signature.

On `ttl`, the reuse claim is the spelling and the value order, not the
parser: `_parse_ttl` is a policy STRING parser, and the rvl lexer has no
duration token (`5m` today lexes as a number followed by an identifier).
The clause therefore costs a small, real lexer/parser spend: a duration
literal or a number-ident juxtaposition rule scoped to the clause slot,
accepting exactly `_parse_ttl`'s forms (`<n>[ms|s|m|h]`, bare number is
seconds) so the two surfaces cannot drift. One parser test is required
because the namespaces overlap: an `invalidated_by` token literally named
`ttl` (`... invalidated_by ttl ttl 5m`) must parse as one dotted-namespace
token then one ttl clause, not swallow the clause head.

The class words are the roadmap's
`pure_fn` / `capability_result` / `external_effect` in the IR; the surface
spells them `pure` / `capability` / `external` because `pure` is already a
keyword, the modifier position makes `_fn`/`_result`/`_effect` redundant,
and one-word classes keep the clause scannable. `invalidated_by` takes
dotted tokens from the shared capability namespace; `ttl` takes the shipped
duration literal.

On a service method the clause is part of the interface contract: every
provider of the method inherits it, and audit attributes the cache to the
seam. On an extern it binds the host boundary directly; the spelling is
grammar-legal from slice 1 but admission-refused until the
interior-crossing slice lands, except where the extern is the seam
method's direct body (enforcement-point section). Declaring it on a
plain `fn` is only legal for `cache pure` (a plain fn with crossing reach
gets the mismatch refusal, which points at the emission that should carry
the declaration instead: freshness belongs on the boundary, not on a caller
of it, or two callers could declare contradictory staleness for one
crossing).

### Rejected alternatives

- **Manifest data.** Rejected as primary for the 309 reasons (author
  knowledge; one word one meaning; the audit-diff payoff wants the
  declaration on the reviewable source surface). The manifest keeps its 411
  role if a deployment needs to bind a symbolic ttl, same as 294 symbols.
- **Head-slot modifier** (`emission cache(external, ttl=5m) fn ...`).
  Rejected: the head slot is crowded, and the parenthesized keyword-argument
  shape would be a second spelling for what 294 brackets and trailing
  clauses already spell two other ways.
- **A stdlib cache component** (the `handoff_cache.rvl` pattern, blessed).
  Rejected as the mechanism: a library cache is exactly the authority-
  ambient store the laundering section forbids, and the checker cannot see
  through it to refuse an unsound memoization. It remains fine as a user
  data structure for values that never crossed a boundary.

## Reconciliations (question 5)

### G6: is a cache a branch around an effect?

The concern: a hit elides a re-execution of an effect, and eliding an effect
behind a condition is the shape G6 exists to forbid. Two answers, one per
half of the concern.

Syntactically, no new position opens. G6 constrains author-written bodies
(only effect forms; a bare expression has no effect to record). The hit/miss
decision is not author-written: it lives in the crossing implementation,
exactly where the class-(b) deferral queue already lives, and the shipped
precedent stands: `deferred` also conditionally elides (defers) a firing at
a declared boundary without anyone calling it a G6 violation, because the
author's body still just says `emit`. `cache` is the same kind of
boundary-declared, runtime-implemented behavior.

Semantically, the pure case is exempt because there is no effect to elide
(G6 is the proof), and the non-pure cases are made safe by the rule that the
elision is invisible to every checker surface: reach, class, policy, taint,
audit all treat the cached crossing as a crossing (laundering section, point
1). What G6 actually protects (no unrecorded, unreachable-by-analysis work)
is preserved by construction; what it must never become is a reason the
folds skip the hit path, which is the 414 row.

### Taint and provenance (249)

A cached value carries the provenance of what produced it. The entry stores
the miss result's origin set; a hit re-attaches it unchanged, so the taint
fold sees `external`-origin taint on the hit path exactly as on the miss
path (the F x 11 cell). No amnesty by storage: caching is not endorsement,
and an entry is not a `Trusted[T]` mint. Two footnotes, stated for honesty:
the KEY is derived from argument values and is hashed into an opaque digest,
never emitted, so keying on tainted data is not itself a flow to a sink; and
cache-timing observation (a caller inferring another caller's queries from
hit latency) is a side channel out of 310's scope, mitigated structurally by
grant-scoped entries (a caller can only ever hit entries its own authority
produced, so the cross-caller timing oracle mostly does not exist).

### Witnessed teardown (243, G7): refused

`cache` on a `witnessed` or `acquire` extern, or on any callee whose reach
includes one, is refused. Three independent reasons, any one sufficient: a
witnessed crossing is a world MUTATION, and "caching" a mutation means not
performing it, which is not a cache but a bug; a hit would skip registering
the inverse in the escrow, so abort/teardown would replay an incomplete
history (a G7 completeness hole); and the witnessed result's meaning is "the
world was changed and here is the receipt", which is exactly the thing a
second caller must never receive without a second change. The same argument
refuses `cache` where the reach includes a `deferred` class-(b) emission (a
queued write is still a write), and where it includes an emission declaring
`compensate` (the parser's emission compensation clause): a compensated
emission registers a `_Compensation` escrow entry at fire time whose
second phase runs on abort, so a hit that skips the firing skips the
registration, and an abort after a hit would replay an INCOMPLETE offset
history: the miss caller's abort compensates, the hit caller's abort has
nothing to run. One word in the refused set closes the hole: witnessed,
acquire, `deferred`, `compensate`. The refusal text names the offending
crossing and its class.

Cacheable reach is read-shaped by DECLARATION, not by construction: the
effect system has no read class, and a bare `emission` is write-capable
with no checked inverse. `cache external` on an emission whose G8-opaque
body actually WRITES is the mis-declaration failure mode, and it deserves
its name: ELIDED WRITE, one silently dropped write per repeated argument
tuple for the length of the ttl. The checker cannot see it (the 44/309
honest-ledger tier applies with full force); the recording roundtrip and
the audit cache line are the stated mitigations, and the open-questions
section carries a sampling-revalidation tripwire. One staleness note in
the same honest register: when every bound is absent (a `cache capability`
entry under a grant with no `expiresAt`, no ttl clause, no generation
change), the entry is fresh-by-definition for the entire session. That is
the declared semantics, not a bug, but the audit line should surface an
unbounded entry as such so an operator can cap it by policy.

### Leases and idempotency (294, 309)

`ttl` reuses 294 wholesale: the duration spelling, the ledger's expiry check
as the enforcement point, and the value order under which an operator's
shorter bound attenuates an author's longer one and never the reverse. Entry
lifetime coupling to grants is implemented ON the shipped grant ledger
(revocation, `expiresAt`, generation liveness), not beside it; 294's own
refusal of parallel mechanisms applies to 310 with full force. 309 is the
write-side sibling: `idempotent` earns an emission the right to be re-fired
safely, `cache` earns a read the right to not be re-fired at all, and the
two meet in recovery, where replaying a cached read with a live entry is
free in the strongest sense (no world touch, no key needed, `replay: free`
in 309's audit vocabulary). Neither claim implies the other and the
descriptors stay separate.

### Approvals (245/246)

Covered in the laundering section, points 4 and 5: a hit is silent but
recorded, hit windows are bounded by the covering approval's ttl for
approval-required tokens, misses keep their full a/b/c posture, and the
metrics never count a hit as a prompt avoided by proof of revertibility
(it is a prompt avoided by proof of freshness, its own counter).

## Staged plan (question 6)

Each slice lands with its checker refusals, IR fields, audit lines, and
tests; a slice is not "runtime works, honesty later".

- **Slice 1: `cache pure`.** Lexer keyword, trailing-clause grammar, the
  class-mismatch and no-clauses-on-pure refusals, IR field, and a cordis-py
  memo table keyed on the structural args digest. No ledger interaction.
  Smallest complete slice and the only one with zero security surface.
- **Slice 2: `cache capability` on SEAM SERVICE METHODS.** Emission-read
  declaration, authority-scoped entry store on the session ledger (recorded
  grant id + valuation + generation in the key), the seam-gate
  liveness-before-consume reorder (enforcement-point section: hit skips
  consumption, miss consumes-before-fire unchanged), the recorded-grant
  rule for multi-grant coverage, the no-policy inert rule, the
  interior-crossing admission refusal, the distributed-placement refusal,
  WAL hit records, the witnessed/acquire/deferred/compensate reach
  refusal, the structural resource-in-entry walk, `state()` hit counter,
  audit cache line, the 414 crossing-kind-11 row with the A x 11 cell, and
  surface H with its per-kind cells. This slice carries the laundering
  resolution and its exit tests, and it is implementable against the
  shipped consent protocol as it stands.
- **Slice 3: `cache external`.** The required-freshness refusal,
  `invalidated_by` (token resolution at admission, WAL-ordered invalidation
  at the named crossing, single-process scope), `ttl` via the new duration
  lexing matched to `_parse_ttl`'s forms and the shipped expiry check,
  approval-ttl coupling of hit windows (consumed-flag rule per the
  enforcement-point section), taint re-attachment and the F x 11 cell.
  Still seam methods only. The roadmap's `cache user_profile invalidated_by
  user.updated ttl 5m` example compiles, runs, and audits at the end of
  this slice.
- **Slice 4 (unscheduled, own design pass): interior crossings.** The
  crossing-level ledger transaction the enforcement-point section scopes
  out: an emitted cache-step wrapper in the `_witnessed_step` mold, an
  owner-carried liveness/consume callback, WAL ordering between seam
  consumption and crossing hit records, and mid-execution refusal
  semantics. Not started until slices 2 and 3 have soaked; until then the
  extern spelling stays admission-refused.

Out of order is refused by dependency: slice 3's soundness conditions are a
superset of slice 2's, and slice 4 changes shipped consent mechanics no
earlier slice touches.

## Exit tests

1. **Pure memoization.** A `cache pure` fn's host-visible evaluation count
   is 1 across repeated same-args calls and 2 across distinct args; results
   byte-identical to the uncached program.
2. **Mismatch refusals.** `cache pure` on a crossing reach refuses naming
   the crossing; `cache external` on a pure fn refuses; `cache pure ttl 5m`
   refuses.
3. **Required freshness.** `cache external` with neither clause is REFUSED
   at compile with the freshness message; adding either clause compiles.
   An `invalidated_by` token nothing in the composition can cross refuses
   at admission.
4. **No laundering on a hit.** Component A (granted) misses then hits; a
   same-args access from component B (ungranted) does NOT hit and its miss
   path is refused exactly as an uncached call; after revoking A's grant,
   A's next access misses (entries died with the grant). The approval class
   fold classifies the cached emission identically with caching on and off
   (the A x 11 cell), and `audit` prints the crossing on both paths.
5. **ttl expiry re-fetches.** A hit inside the ttl serves the entry (host
   count unchanged); an access past expiry re-crosses (host count
   increments) under full authority checks.
6. **`invalidated_by` re-fetches.** After a crossing of the named token, the
   next access misses; ordering test: a session that fires the invalidating
   emission then reads observes the fresh value, never its own stale write.
7. **Escrow-shaped refusals.** `cache` on a witnessed extern (and on a fn
   whose reach includes one) refuses; likewise a reach including an
   `acquire`, a `deferred` emission, or a `compensate`-declaring emission.
   The fault sweep covers ALL escrow entry kinds (`_Transactional`,
   `_Compensation`, witnessed inverses, acquire undos), not just
   `_Transactional`, and shows no entry of any kind is ever skipped by a
   cache in any admitted program.
8. **Provenance.** A hit's value carries the miss's taint origins (the
   F x 11 cell); a taint-flow policy rule that refuses the miss path refuses
   the hit path identically.
9. **Consent bound.** For an approval-required token, hits stop when the
   covering approval's ttl lapses; `state()` shows hits in their own
   counter, `prompts.perCall` unchanged by hits, incremented per miss.
   The consumed-flag rule holds: after the miss consumes the single-use
   approval, same-args hits continue until the ttl (not zero hits), and a
   different-args access prompts again.
10. **Seam consumption order.** On a miss, exactly one use is consumed
    BEFORE the body fires (WAL order: consumption record precedes fire
    record); on a hit, `remainingUses` is unchanged and no body runs; a
    refusal on the miss path fires before any work, never mid-body. With
    two covering grants, the entry hits only while its RECORDED grant is
    live: revoking that grant forces a miss (which consumes the other
    grant and re-keys) even though the other grant covered all along.
11. **No-policy inertness.** In a session with no policy, a `cache
    capability`/`cache external` program compiles, every access takes the
    miss path (host count increments per call), and no entry store exists;
    `cache pure` still memoizes.
12. **Distribution refusal.** A placement that splits a `cache`-declaring
    method across processes (distribute.py) refuses at admission, naming
    the method and the split; the same composition placed in one process
    admits.
13. **Surface H per-kind cells.** The applicability fold is computed over
    the same closure as the ClassMap, verified per 414 crossing kind: the
    refusal follows the spawn/instance-get seam (kind 2), the transitive
    closure (kind 4), and the `*` widening (kind 8), one cell test per
    kind. The structural resource walk refuses a resource nested in a
    record, in a variant arm, and under a generic instantiation, and a
    structurally-resource type with an innocent name refuses identically.
14. **Grammar edges.** `examples/handoff_cache.rvl` still compiles
    unchanged (`cache` as a provided key name is untouched by the
    contextual keyword); `... invalidated_by ttl ttl 5m` parses as the
    token `ttl` plus a ttl clause; `5m` in the clause slot lexes as one
    duration with `_parse_ttl`-identical semantics.

## Scoped out (and to whom)

- Cross-composition / multi-realm invalidation: the federation surface;
  the shared token namespace is the extension point.
- Operator policy caps on declared ttls (`capability X cache ttl <= 1m`):
  a 246-family policy extension; soundness does not need it (grant and
  approval lifetimes already bound the window).
- Verification of the determinism claim and of subscription completeness:
  the item-37 recording-roundtrip family, per the 44/309 posture.
- Cache sizing, eviction under memory pressure, and persistence across
  process restarts: runtime quality-of-service, no semantic content
  (eviction is always sound because a lost entry is just a miss).

## Open questions

1. Should a `cache capability` entry survive a hot-swap when the item-53
   handoff relation proves the successor state-compatible, or is generation
   death non-negotiable? (Current answer: dies with the generation; revisit
   with evidence that swap-frequency makes it matter.)
2. Is a per-entry `stale-while-revalidate` mode (serve the stale entry once
   while re-crossing in the background) expressible without giving the hit
   path a consistency model the author never declared? (Deferred; the async
   arc is the natural home.)
3. Does `invalidated_by` want glob patterns over tokens (the policy `covers`
   precedent) or exact tokens only? (Slice 3 starts exact; widening is
   backward-compatible.)
4. Should the runtime carry a sampling-revalidation tripwire for the
   elided-write failure mode: occasionally take the miss path on a
   would-be hit and compare result digests, where a divergence is hard
   evidence of a mis-declared `cache capability`/`cache external`? It is
   sound (the re-cross runs under authority the caller already holds and
   consumes as a miss), but it makes hit latency bimodal and its detection
   is probabilistic; possibly a `revl verify`-family mode rather than an
   always-on cost. (Open; pairs with the item-37 recording roundtrip.)

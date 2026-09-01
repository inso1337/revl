# Design: effect ownership modes, owned / borrowed / shared (item 308)

Status: design pass (PRODUCT-VISION, 243-261 arc). No implementation in this
doc. Companion docs: [243-witnessed-externs.md](243-witnessed-externs.md),
[teardown-contract.md](teardown-contract.md),
[247-compensate.md](247-compensate.md),
[294-parameterized-capabilities.md](294-parameterized-capabilities.md),
[363-per-component-tier-placement.md](363-per-component-tier-placement.md).

## What 308 asks

Explicit ownership on resources, with four verified properties: the OWNER
performs the final inverse; a BORROWER cannot close an owned resource; a
borrowed handle cannot outlive its owner; SHARED handles tear down by
refcount or lease. Plus: a realm boundary requires explicit transfer. The
item's own framing is the right one: extend the G4/G7 resource model from
"there is an inverse" to "the correct component owns the inverse". A
borrow-checker for resources, not for memory.

## The model that already exists (do not reinvent it)

revl already has an ownership model. It is implicit, and much of 308 is
naming it, then closing the holes the implicit model does not cover.

1. **The acquiring activation is already the owner.** `let conn = effect
   Pool.open(url) undo conn.close()` registers the inverse as a `bracket`
   entry in the acquiring activation's accumulator
   (docs/design/teardown-contract.md, "three entry kinds, one stack"). G7
   replays it LIFO at that activation's teardown, exactly once, commit or
   abort alike. Nobody else holds the entry. So "the owner performs the
   final inverse" is TRUE BY CONSTRUCTION today, for the accumulator
   channel. What is not checked is every other channel (below).

2. **Resource-typedness exists, but only at the seam.** A resource type is
   an `extern acquire` return, closed transitively over records and variant
   payloads (`src/revl/distribute.py`, `_resource_taint`, the item-363 F1
   hardening). That closure feeds exactly two consumers: the
   distributability verdict (`revl audit`) and the cross-process seam
   refusal (`src/revl/placement.py`, `resource_crossing_refusal`). The
   TYPECHECKER has no notion of a resource type at all (verified against
   `src/revl/typecheck.py`: the only mention of `acquire` is a comment).
   308's core mechanical move is promoting resource-typedness from a seam
   analysis into the frontend. That promotion is NOT safe verbatim: the
   base set is `ext["returns"]` as written, primitives included, and the
   corpus contains `extern acquire fn log_open(path: Str) -> Int`
   (examples/durable_log.rvl). Rule R0 below fixes the base before the
   closure moves.

3. **In-process handle flow is deliberately ungated.** `placement.py` says
   it in so many words: two components co-located in one process pass a
   handle in memory, ungated. A service method may take or return a `Sock`;
   a record may carry one; a provide method may hand it to a caller. This
   is where every hazard 308 names actually lives.

4. **Teardown ordering already protects the static topology.** R3 tears
   consumers down before providers, and G7 is LIFO within a frame. A
   consumer that borrowed a handle from its provider's service method dies
   before the provider closes that handle. So for the static composition,
   "a borrowed handle cannot outlive its owner" mostly holds by
   construction. It stops holding under dynamism: a `revl swap` replaces
   the provider (its teardown runs, the handle closes) while a consumer
   still holds a stored copy; the swap gate's resource parity note in the
   seam refusal exists precisely because of this shape.

5. **Closures capture by value** (docs/closures.md), which protects G7/A8
   inverse stability but does NOT confine a handle: a by-value capture of a
   handle copies the reference to host state, and the closure can outlive
   the frame that owns the bracket. Worse, the closure's TYPE erases the
   capture: a `() -> Int` value carrying a captured `Sock` is not
   resource-typed under the taint fixpoint, so a returned closure launders
   the handle across a service signature into another activation (the 414
   crossing catalogue, kind 8). B1 clause 2 exists for this.

6. **`spawn` is already an owned resource, but its config is a crossing.**
   The parser forces `spawn C ... undo s.dispose()` (parser.py, the G4
   hint at the spawn effect form): the spawning frame owns the spawned
   component and its teardown. 308's vocabulary applies to the spawn
   HANDLE unchanged; nothing new is needed for it in v1. Spawn CONFIG is
   different: `spawn C with { conn: conn }` seats a value in the child's
   activation for the child's whole lifetime (414 crossing kind 2), which
   is an escape by any name. B1 clause 6 refuses it for resource-tainted
   values.

What the implicit model does NOT prevent, today, in admitted programs:

- **Double-close.** The owner's accumulator will close `conn` at teardown,
  and nothing stops a body (the owner's own, or a borrower's) writing
  `emit conn.close()` or an `effect conn.close() undo reopen(conn)` step
  mid-session. G4 is satisfied (there is a marker or an inverse); the
  handle is closed twice.
- **A borrower closing.** Same hole seen from the other side: any component
  a handle reaches can call the closing extern on it.
- **A stored borrow outliving its owner under swap** (point 4 above), or a
  handle parked in a closure or a long-lived container and used after the
  bracket replayed.
- **A borrow escaping through the teardown machinery itself.** An
  effect-form `undo` expression captures arbitrary locals and lives on the
  per-activation LIFO stack until teardown: `undo conn.reset()` on a
  borrowed `conn` escapes into the borrower's teardown and replays after
  the owner was swapped out (use-after-free through the accumulator).
  Witnessed effect arguments and `emit ... compensate <expr>` arguments
  are the same shape. Compensations are worse: they run in teardown Phase
  2, AFTER Phase 1 closed every bracket, so a resource in compensation
  args is use-after-close in the plain same-frame case, no swap needed.
- **`handoff` re-seating a dead handle.** `handoff db: Session` with
  `Session = { conn: Sock }` re-seats the predecessor's resource vector on
  the successor, gated only by shape compatibility, while the
  predecessor's teardown closes `conn`: the successor starts warm with a
  dead handle.

So the honest answer to "new invariant or formalization?" is: BOTH, split
cleanly. Owner-runs-the-final-inverse is a formalization of what the
accumulator already enforces; no-close-outside-the-accumulator and
borrow-does-not-escape are genuinely new checks, currently absent from the
frontend entirely.

## The three modes, precisely

Each mode is a static discipline plus a runtime teardown consequence. The
runtime side reuses the teardown contract's entry kinds; only `shared` would
add one.

### `owned`

- **Who:** the activation that ran the acquire effect form (or spawn). One
  owner per handle, fixed for the handle's life; v1 has no transfer.
- **Static discipline:** the final inverse is the accumulator's, full stop.
  No hand-call of a resource's declared inverse anywhere, including the
  owner's own body (rule O1 below). The owner MAY freely store its own
  handle in its own activation state and return it from its provide
  methods; both stay within the frame the bracket lives in, and returning
  is exactly how a borrow is created. Ownership is not exemption from
  every rule: the owner may not capture its handle in a closure (B1
  clause 2) and may not place it in compensation arguments (B1 clause 5),
  because both positions outlive or outrun the bracket.
- **Runtime consequence:** none new. The existing `bracket` entry (or
  `transactional` for witnessed effects) IS owned-mode teardown: LIFO at
  the owner's frame, exactly once, G5-infallible by contract.

### `borrowed`

- **Who:** every other position a resource-typed value reaches: a service
  method parameter, a `fn` parameter, ANY call result (a provide method's
  or a plain `fn`'s), a resource-typed field of a record handed over.
- **Positional, not dataflow, identity.** Parameters and call results are
  ALWAYS borrows, even when they are dataflow-identical to a handle the
  receiving activation owns. The inference is positional because that is
  the safe direction: every wrong answer it can give is a false positive
  (a refusal of a program that happened to be fine), never an unsound
  admission, EXCEPT in positions the flow walk does not visit at all,
  which is exactly what B1 clauses 5-7 close. The named casualty: a
  checkout/checkin pool where the owner's provide method `checkin(c:
  Sock)` receives its own handle back. Positionally `c` is a borrow, so
  clause 1 refuses re-storing it, and no v1 analysis proves the round
  trip. That shape is deliberately inexpressible in v1: the owner keeps a
  free list keyed by an id, not by the handle, and lends per call.
- **Static discipline:** use, do not close, do not keep. (a) Rule O1
  applies (a borrower cannot invoke the declared inverse). (b) The borrow
  is confined to the receiving scope: it may not be stored into
  activation-level state, captured by a closure, returned onward from a
  provide method, inserted into a container or record that escapes the
  scope, placed in an undo, witnessed-argument, or compensate position,
  seated in spawn config, or carried by a handoff type (rule B1 below).
  Passing it further DOWN a call chain is fine; the callee's parameter is
  just another borrow.
- **Runtime consequence:** none, and that is the point. A borrow registers
  nothing in any accumulator; teardown of the borrower's frame does not
  touch the handle. "Cannot outlive the owner" is then a corollary: the
  borrow lives at most as long as the call into the owner that produced
  it, R3 orders whole-component lifetimes underneath, and the escape rule
  closes the stored-copy channel that swap made dangerous.
- **Not a lifetime system.** The check is a does-not-escape flow rule over
  the scope structure lower.py already has (activation body vs provide
  method vs nested block), the same altitude as the existing Opt-escape
  and closure-capture checks in typecheck.py. There are no lifetime
  parameters, no region variables, no annotations to solve for. A program
  that genuinely needs a longer-lived borrow does not get a fancier
  lifetime; it restructures so the owner holds the handle and lends it per
  call. That restriction is the feature.

### `shared`

- **Who:** N holders declared up front, teardown when the last one lets go.
- **Static discipline:** each holder registers a RELEASE, not the inverse;
  the true inverse is bound to the count reaching zero and runs exactly
  once.
- **Runtime consequence:** this is the only mode needing a new runtime
  shape, and there are two candidate mechanisms:
  - a refcounted entry: each holder's frame carries a `bracket` whose
    inverse is `release(handle)`; the runtime decrements, and the zero
    crossing fires the declared inverse. Needs a WAL story per 243 rule 4
    (the count must be reconstructible or the crash leaks the handle);
  - a 294 lease: the grant ledger is already the lease runtime
    (294 design, "the grant ledger is already the lease runtime"), the
    task lease already rides the verified LIFO disposer chain and is
    ttl-backstopped on a crash that skips teardown. A shared handle's
    teardown is then a lease bound to a holder count: releases consume,
    and exhaustion (count zero, or a liveness-confirmed expiry after a
    crash; see S1 on why bare ttl is not enough for teardown) triggers
    the inverse.
- **Recommendation: defer shared out of v1 entirely,** and when it lands,
  build it as the 294 lease binding, not a bespoke refcount. The lease
  runtime already solved the two hard parts (durable consume-before-fire
  accounting, and a crash backstop so a SIGKILLed holder cannot pin the
  count forever). A bespoke refcount would re-derive both, which is
  exactly the parallel-mechanism shape the 294 doc refuses. Section
  "Honest scope" details the cut.

## Spelling: annotation, inference, or manifest

Three candidates, and the manifest one should be rejected first.

**(a) Manifest.** The 363/411 precedent (placement is manifest data, zero
new grammar) is real but does not transfer. That precedent's own stated
test was: placement is a DEPLOYMENT concern, operator-owned, varying per
production topology with the source unchanged. Ownership is the opposite
kind of fact: it is author intent about a value's lifetime, fixed by how
the code is written, checked per binding and per signature position. A
manifest cannot address "this parameter of this method" without inventing
a worse grammar than source syntax, and an operator overriding an
ownership mode at deploy time is precisely the wrong actor making the
call. Rejected.

**(b) Full source annotation** (`owned Conn` / `&Conn` / `shared Conn`
everywhere a resource type appears). Honest Rust precedent, but it taxes
every signature for information the compiler already has: the acquire site
is unambiguous in source (the effect form), and every non-acquire position
can only be a borrow in v1. Annotating what is forced is ceremony.

**(c) Inferred modes with a minimal reserved surface. RECOMMENDED.**

- `owned` is implicit at the acquire binding. `let conn = effect
  Pool.open(url) undo conn.close()` is already the ownership claim; the
  effect form IS the annotation. No new syntax.
- `borrowed` is the inferred mode of every other resource-typed position:
  parameters, call results, resource-typed fields received from elsewhere.
  This matches what admitted programs already mean (handles passed around
  are de facto borrows), so inference changes no existing program's
  meaning; it only starts refusing the hazards.
- `shared` is a future contextual keyword at the acquire binding
  (`let pool = effect shared Pool.open(url) undo pool.close()`), reserved
  now, not implemented in v1. Contextual, not a lexer keyword, for the
  same reason `witnessed` is (243 Slice 1 refinement 2): the self-hosted
  lexer's keyword-set parity oracle stays untouched, and no program using
  `shared` as an identifier breaks.
- explicit transfer (`transfer conn`), which the item text requires at a
  realm boundary, is likewise reserved and NOT in v1: v1 has no transfer
  at all, an owned handle stays where it was acquired, and a realm or
  process boundary refuses it (next section). When transfer lands it is a
  source-level marker at the passing site, because it changes who runs
  the final inverse, which is author intent again.

The recommendation in one line: ownership is contract, so it lives at the
source level like `emission[...]` does; but the compiler infers the mode
from position, and source spelling is reserved for the two places where
intent genuinely diverges from the default (`shared`, `transfer`), neither
of which is in v1. v1 therefore ships ZERO new grammar, which also means
zero self-host parser/lexer porting for the surface.

## The checks, as static rules with refusal diagnostics

Precondition for all of them: lift the resource-type closure into the
frontend. `_resource_taint` (the acquire-return base set plus the
record/variant fixpoint) moves to a shared module the checker calls, and
`distribute.py` imports it from there. One implementation; the seam
refusal and the new frontend refusals cannot drift apart. But the base
set must be repaired first (R0), or the lift poisons the checker.

### R0: acquire returns are nominal handle types (the taint base repair)

**The problem.** `_resource_types` takes `ext["returns"]` verbatim, and
`_resource_in` matches the name by word-boundary regex over type strings.
The corpus already contains `extern acquire fn log_open(path: Str) -> Int`
(examples/durable_log.rvl). Promote that base set into the frontend
unmodified and `Int` is a resource type: every integer in the program is a
borrowed handle, O1 and B1 refuse arithmetic, and the language is
unwritable. This is not a corpus curiosity to carve out later; it is a
poisoned foundation, and slice 0 MUST decide it, because the report-only
sweep will otherwise present as a wall of false positives rather than a
reviewable carve-out list. (Independent of 308: the verbatim base set is
arguably a live over-taint at the seam TODAY, since `Int` in any
signature already trips the seam analysis's resource matching. R0 fixes
that too.)

**The two candidate fixes.**

- (a) **Nominal handles, RECOMMENDED.** An `acquire` (or `witnessed`)
  extern's return must be a NOMINAL opaque handle type: primitive and
  structural returns are refused at the declaration, with a diagnostic
  naming the fix (declare an opaque handle type and return that).
  Migration for durable_log: a nominal `LogHandle` wrapping the
  descriptor, threaded through the effect and its undo exactly as the
  `Int` is today. The handle type carries identity, which is what
  ownership tracks; a primitive cannot.
- (b) **Primitive exclusion.** Keep primitive returns legal but exclude
  primitives from the frontend base set, with an explicit note that
  primitive-handled resources get NO 308 protection (no O1, no B1, no
  mode). Rejected as the default: it silently exempts exactly the
  resources most likely to be raw file descriptors, and the exemption is
  invisible at the use site.

Option (a) is the recommendation. It costs one corpus migration and buys
a base set that means what the checker needs it to mean. The regex-based
`_resource_in` matcher should be replaced by structural type matching in
the shared module at the same time; nominal handles make that trivial.

### O1: no hand-call of a declared inverse (owner and borrower alike)

**Rule.** The set of closing operations is DEFINED as: the declared `undo`
callees of `acquire` (and `witnessed`) externs, plus `dispose` on a spawn
handle. A call to any of these, in any body position (pure call, effect
acquisition, emission), AND in any undo or compensate expression, on a
resource-typed argument, is refused. The accumulator owns the final
inverse; source code never performs it, and it cannot be smuggled into an
unrelated binding's undo or an emission's compensate either.

**The mandatory exemption.** The undo clause of the binding that acquires
THIS handle literally IS the declared inverse call: `let fd = effect
log_open(p) undo log_close(fd)` is the corpus acquire form
(examples/durable_log.rvl), and without an exemption O1 refuses every
acquire in the corpus. The exemption is exactly: the inverse call in the
acquiring binding's own undo clause, applied to the handle that binding
binds, scoped to that one binding. The same inverse called in any OTHER
binding's undo, or on any other handle, stays refused.

**Refusal.**

```
error: app.rvl:41: `conn.close()` is the declared inverse of `Pool.open`;
  the final inverse is owned by the acquiring activation's teardown (G7)
  and runs exactly once at unload. Calling it here would double-close.
  hint: if the resource must end early, that is an explicit-release
  surface revl does not have yet; let teardown run it.
```

**What this buys.** No-double-close through the declared channel, for
everyone: a borrower closing an owned handle and the owner racing its own
accumulator are the same refusal. It also hardens G7's meaning: the LIFO
replay set is now provably the COMPLETE set of closes, not merely a set.

**The honest limitation.** The checker knows a close only through the
declaration. An extern that closes a handle without being anyone's
declared inverse (`emission fn shutdown(c: Conn)`) is invisible to O1; the
type system cannot know host semantics. This is the same honesty line the
teardown contract draws for G5 bracket infallibility: the declaration
surface is the proof surface, and an extern author who closes out-of-band
has lied to it. Named here, not solved here.

### B1: a borrow does not escape its scope

**Rule.** A resource-typed value whose mode is borrowed (any resource-typed
value the current activation did not itself acquire) may not do any of the
following; clause 2 and the compensate half of clause 5 additionally bind
OWNED values, as noted inline:

1. be assigned or `effect`-inserted into activation-level state (a
   component `let`/`var` binding, or a container acquired at activation
   level);
2. be captured by a closure. This clause binds the OWNER too: a closure
   value's type (`() -> Int`) erases the capture, so a closure carrying a
   handle is invisible to the taint fixpoint and launders the handle
   across any signature (414 crossing kind 8), and an owner's closure
   returned across a service boundary outlives the owner's frame exactly
   like a stored borrow. Two candidate rules: taint the closure VALUE at
   arrow creation when any capture is tainted, then apply every B1 clause
   to tainted closure values regardless of the capturer's mode; or refuse
   closure capture of any resource-typed value outright, owner included.
   RECOMMENDED for v1: the outright refusal. It is a strictly smaller
   check, it has no closure-value plumbing to get wrong, and the sweep
   (slice 0) will show whether any legitimate capture exists; the
   closure-value taint is the later refinement if one does;
3. be returned from a provide method or a `fn` (only the OWNER returning
   its own handle from a provide method is admitted; that return is the
   borrow-creating move). Implementation note: the check keys on the
   TAINTED return type, not the bare handle type, or a `Session`
   wrapping a `Sock` walks out unrefused;
4. be placed into a record or collection value that then does any of the
   other clauses (the resource-taint fixpoint already identifies which
   types carry handles, so this is the same check applied to the carrying
   value). Implementation note: both orders must be caught,
   insert-then-store AND store-then-insert; a container already parked in
   activation state that later receives a borrow is the same escape as a
   loaded container being parked;
5. appear in an undo expression, in a witnessed effect's argument list,
   or in a compensate expression. An undo captures its free locals and
   lives on the per-activation LIFO stack until teardown: `undo
   conn.reset()` on a borrowed `conn` escapes into the borrower's
   teardown and replays after the owner is swapped out. Witnessed
   arguments ride the same accumulator. Compensations are stricter still:
   they run in teardown Phase 2, after Phase 1 has closed every bracket,
   so a resource-typed value in compensation arguments is use-after-close
   by phase ordering alone, same frame, no swap required. The compensate
   half of this clause therefore binds OWNED values too, not just
   borrows. It also has a WAL angle: a host handle serialized into
   compensation args is unreconstructible residue on crash recovery, a
   dead number in the log. (The one undo position exempt from this clause
   is the same one O1 exempts: the acquiring binding's own undo, on its
   own handle, which is the bracket being created, not an escape.);
6. appear as a spawn config value. `spawn C with { conn: conn }` seats
   the value in the child's activation for the child's whole lifetime
   (414 crossing kind 2), a per-invocation borrow escalated to an
   activation-lifetime hold. A child that needs a handle acquires its
   own, or calls the owner's service per use;
7. appear in a handoff type. `handoff db: Session` with `Session = {
   conn: Sock }` re-seats the predecessor's resource vector on the
   successor, gated only by shape compatibility, while the predecessor's
   teardown closes the handle: the successor starts warm holding a dead
   descriptor. v1 refuses a resource-tainted handoff type at admission,
   from the same shared taint module. A real handle transfer is "the
   bracket migrates", which is exactly what the deferred `transfer`
   marker means (next section); handoff shape-compat cannot express it.

Passing the borrow DOWN (as an argument to a `fn` or a further service
call) is admitted; the callee's parameter is a borrow with the same rules.

**Refusal.**

```
error: cache.rvl:57: borrowed resource `conn: Sock` (acquired by
  component `Db`) cannot be stored in activation state `slots`; a borrow
  is confined to the scope that received it and may not outlive its
  owner's bracket (G7). Restructure so `Db` holds the handle and lends it
  per call.
```

**Can revl express does-not-outlive without a lifetime system?** Yes, at
this granularity, because the frame/scope structure already stratifies
lifetimes coarsely: activation state lives to the frame's teardown,
method-scope bindings live per invocation, and a fixed set of constructs
package a scope up and move it. The clause list is that set: storage
widening (1), scope packaging (2), signature crossing (3), carriers (4),
the teardown machinery's own capture positions (5), and the two
activation-seating constructs (6, 7). The check is a per-scope flow walk
in the checker, the same shape as the existing Opt-escape analysis, not a
constraint solver. The list is a closed-world claim over the language's
crossing constructs (the 414 catalogue is the checklist); a future
construct that seats a value in another activation must add its clause
here, and the S1 holder count shares this same enumeration. What this
coarse stratification CANNOT express is a borrow legitimately parked
across method invocations but still shorter-lived than the owner; v1
refuses that shape deliberately (restructure so the owner holds it). If
real programs hit that wall, the escape hatch is a later, finer analysis,
not a v1 loosening.

**Owner carve-out, stated precisely.** The owner storing its OWN handle
in its OWN activation state is admitted (same frame, the bracket and the
store tear down together under G7 LIFO), and is the normal pool pattern:
acquire at activation, store, lend per call. The carve-out is provable
only where identity is syntactically evident: the flow from the acquiring
binding itself. A handle that leaves and comes back is a parameter or a
call result, hence a borrow by position (the checkin pool above), and
clauses 2 and 5's compensate half bind the owner regardless. B1's other
clauses bind borrows only.

**The honest limitation: retaining externs.** The flow walk sees revl
positions. An `emission fn register(c: Conn)` that retains the handle
host-side, or a bridge-implemented service that stores a borrow in its
host runtime, escapes through a surface no clause can see; the
declaration does not say "retains". This is the same declaration-is-the-
proof-surface line O1 draws for out-of-band closes. Cheap audit-only
mitigation, worth shipping with slice 2: `revl audit` lists every
resource-typed argument reaching a non-inverse extern or a
bridge-implemented service, so a human can review the retention surface
even though the checker cannot refuse it.

### S1: shared teardown, exactly once at last release (deferred with shared)

**Rule (direction fixed here; the shared slice finishes the spec).** A
`shared` acquire registers, in each holder's frame, a release entry; the
declared inverse is bound to the count's zero crossing and runs exactly
once, in the frame that performed the last release. Static side: `shared`
is only admitted at an acquire binding; holders are counted at the points
the handle crosses into another activation's scope (each crossing is a
lease consume in 294 vocabulary). The counting enumeration is B1's
crossing enumeration, the same module: a crossing B1 cannot see is a
holder S1 never counts, so every gap in one is a gap in the other, and a
clause added to B1 is a counted crossing in S1 by construction.

**Crash honesty, liveness-gated.** The count rides the 294 grant ledger
(durable consume-before-fire); without a backstop a refcount turns one
SIGKILL into a permanently pinned handle, which is why the
bespoke-refcount option loses. But the 294 grant ttl cannot be reused
bare: expiring a GRANT on wall-clock is fail-safe (the holder loses an
authority and re-requests), while firing an INVERSE on wall-clock is
fail-dangerous (it closes a handle a slow-but-alive holder still holds,
manufacturing exactly the use-after-close 308 exists to refuse). The
backstop is therefore liveness-gated: ttl lapse ARMS the reclaim,
and the inverse fires only once the runtime confirms the holder is gone
(process dead, activation torn down without release). A slow holder pins
the handle until it releases or dies; that is the correct bias for
teardown.

**Exactly-once under G7, with one named exception.** On the orderly path
the zero-crossing inverse joins the last releaser's LIFO stack as an
ordinary bracket entry at release time, so G5/G7 hold unchanged there: no
new teardown phase, no emission in teardown, and the residue schema
(teardown-contract.md) needs no new record kind; a failed shared inverse
is a `bracket-fault` like any bracket. The crash path is the exception,
and it must be named rather than waved at: a liveness-confirmed expiry
has NO releaser frame, so no LIFO stack exists to join. The executor is
the lease runtime's own reclaim step (the 294 ledger's expiry sweep,
which already runs out-of-frame for grant lapses), running the inverse
outside any activation. That IS a new out-of-frame teardown surface,
narrow as it is, and it needs its own residue record (a lease-lapse close
is not a `bracket-fault`; the audit surface must report it as a reclaim).
The earlier draft's blanket "no new teardown phase" claim held only for
the orderly path; the shared slice's spec owes the crash path this
executor, its residue kind, and its WAL entry.

## Reconciling with the seam (and the realm boundary)

The seam rule today (`resource_crossing_refusal`, tier-agnostic, every
cross-process seam the conductor wires): a resource-typed value cannot
cross a process boundary by copy, because a copy is a dead handle detached
from its undo contract. Per mode:

- **`owned` across a seam: stays refused.** 308 changes nothing except
  the diagnostic, which can now name the mode ("owned resource `Sock`
  cannot cross the process seam; its bracket lives in process `db`").
  The existing refusal is exactly owned-mode discipline enforced at the
  one boundary where copying could fake a transfer.
- **`borrowed` across a seam: refused in v1, and honestly probably
  forever in this form.** The principled alternative is a proxy: the
  handle stays home and operations forward, which is what the interop
  bridge already does for whole SERVICES. A borrowed handle as an ad-hoc
  sub-service would need generated proxy/stub per resource type, and it
  imports all four Waldo leaks into what source code reads as a local
  handle use, plus a teardown-across-crash story the bracket cannot give
  remotely. If a composition needs remote use of a resource, the existing
  answer is right: wrap it in a service and place the service with the
  handle. v1 keeps the refusal and improves its message.
- **`shared` across processes: refused, explicitly and permanently as a
  refcount.** Distributed reference counting under partial failure is a
  known-hard problem: a lost decrement pins the resource, a duplicated
  one double-frees, and solving it means timeouts and reconciliation,
  at which point it IS a lease. So the honest position: cross-process
  shared is out of scope for the refcount mechanism, and if it is ever
  wanted, it arrives as a 294 realm/task lease with ttl (already
  crash-honest, already audited), not as a distributed count. The design
  does not pretend otherwise.
- **The realm boundary ("requires explicit transfer").** v1 has no
  transfer, so the realm rule collapses to: an owned handle is also
  realm-bound; `isolate db in realm("tenant")` partitions providers, and
  a handle acquired in one realm reaching a component of another realm is
  refused by the same promoted resource-type check (the checker now sees
  resource types, so it can see realm labels next to them). The reserved
  `transfer` marker is the future surface that would move the bracket
  itself; deferred, with the note that transfer across a PROCESS seam
  additionally needs the WAL entry to move processes, which is why it is
  not a v1 afterthought.

## What it buys, and what it formalizes

| property | today | under 308 v1 |
|---|---|---|
| owner runs the final inverse | by construction (accumulator), unchecked elsewhere | formalized; O1 closes the out-of-band channel |
| no double-close | writable in admitted programs | refused (O1) |
| borrower cannot close | writable | refused (O1) |
| borrow cannot outlive owner | by construction for static topology (R3/G7); broken by stored copies under swap; closures can smuggle | refused (B1), including the swap shape |
| no resource in teardown-phase positions | writable; compensations are use-after-close by phase order | refused (B1 clause 5) |
| no borrow seated in another activation | writable via spawn config and handoff | refused (B1 clauses 6, 7) |
| shared teardown exactly once | no shared notion at all | direction fixed (S1, liveness-gated backstop, crash executor owed), deferred to the 294 lease |
| owned cannot cross a seam | refused (363/F1) | unchanged, mode-named diagnostic |

Relation to the arc: G5 is untouched in v1 (checker-only, no new
teardown-time surface; the orderly shared inverse is an ordinary bracket
entry, and the one out-of-frame exception is the deferred S1 crash
reclaim, named there). G7's guarantee strengthens from "the accumulated
inverses replay LIFO-completely" to "and the accumulated inverses are the
ONLY closes, so the replay set is the whole close-set". 247 compensations
are NOT orthogonal: compensations run in teardown Phase 2, after Phase 1
has closed every bracket, so any resource-typed value in compensation
arguments is use-after-close by phase ordering alone, and a handle
serialized into the compensation WAL is unreconstructible residue on
recovery. B1 clause 5 refuses resource-typed values (owned or borrowed)
in compensate positions for exactly this reason. 243 witnessed handles
are covered because witnessed externs sit in the same externs table and
their declared `undo` joins O1's closing set, and clause 5 keeps borrows
out of witnessed argument lists. The 295 schedule-testing note
(route shared-witnessed-resource interleaving findings into this design)
lands in the shared slice: interleaved release orders across fibers are
exactly what the S1 exit test permutes.

## Honest scope: the minimal useful v1

A full ownership system (transfer, finer borrow lifetimes, shared,
proxied remote borrows) is a deep type-system change, as the item says.
The minimal cut that pays for itself immediately:

**v1 = owned + borrowed, zero new grammar.**

1. R0: nominal-handle rule for acquire returns (declaration-site refusal
   of primitive/structural returns; durable_log migration), THEN promote
   the resource-taint closure into the frontend (shared module;
   distribute.py imports it).
2. O1: no hand-call of a declared inverse, in body, undo, or compensate
   positions, with the own-undo exemption.
3. B1: borrow does-not-escape (the seven clauses, with the owner
   carve-out as bounded above).
4. Mode-named diagnostics, including upgrading the seam refusal's message.
5. The retention audit: `revl audit` lists resource-typed arguments
   reaching non-inverse externs and bridge services (report-only, the B1
   limitation's mitigation).
6. The method-scope acquire decision, driven by the slice-0 corpus count
   (see the staged plan): either the early-release surface lands with
   slice 1, or v1 refuses method-scope acquires with a diagnostic naming
   the restructure. Not deciding is not an option: O1 plus
   activation-lifetime brackets makes a method-scope acquire
   leak-until-unload with no recourse, which is a worse behavior than
   either explicit choice.

Deferred, each with its landing place named: `shared` (a 294 lease
binding; spec S1 above, crash-path executor owed), explicit `transfer`
and realm-crossing moves (new source marker, WAL migration; also the only
honest future for handoff of resource-carrying state, per clause 7),
early release IF the slice-0 count comes back zero (an explicit-release
surface that discharges the bracket; interacts with 245 session commit),
remote borrows (a service, not a proxy, is the answer until proven
otherwise).

**Migration risk, stated.** B1 is conservative and could refuse an
existing admitted program that parks a received handle in activation
state. Before the flip: sweep the corpus (examples/, stdlib/, the
workloads, backends' scenario fixtures) with the check in report-only
mode. Every hit is either a real latent hazard (the point of the item) or
evidence for a carve-out that must then be argued into THIS doc, not
patched tier-locally. Emission goldens must stay byte-identical for every
program that does not trip a refusal: v1 is checker-only, no emit change,
which is the same additive discipline 243 Slice 1 held to.

**Self-host note (item 391 discipline).** v1 adds checker/lower refusals
and no grammar. The self-host stages are expression-only in
parser/checker.rvl with fn-body analysis in lower.rvl; whether O1/B1 need
a port is decided by which oracle covers refusal parity for rejection
fixtures. If the differential oracle only compares admitted-program
output, the port can be deferred with the gap named in the self-host
stage map; if rejection parity is asserted, the flow walk ports to
lower.rvl. Decide at slice 2, do not let it be discovered by a red oracle.

## Staged plan and exit tests

Slices are additive and land in order; each leaves the suite green and
per-backend goldens byte-identical (checker-only until the shared slice).

- **Slice 0: R0 decision + shared resource-type module + report-only
  sweep + the method-scope count.** First, land R0 (nominal acquire
  returns refused-at-declaration for primitives/structurals; migrate
  durable_log to a nominal `LogHandle`); the sweep is meaningless before
  this, because a primitive-poisoned base presents every `Int` as a
  borrow. Then move `_resource_taint` (and `_resource_types`,
  `_resource_in`, restated structurally) to a frontend module;
  distribute.py re-exports. A `revl audit` report-only listing of
  would-be O1/B1 hits over the corpus. Also: COUNT the method-scope
  acquire forms in the corpus (acquire effect forms not at activation
  scope); this count decides the F9 question (early-release sequenced
  with slice 1, or method-scope acquires refused in v1). Exit:
  distribute/placement tests unchanged; durable_log admitted under its
  migrated handle type; sweep output reviewed and carve-outs (if any)
  amended here; the method-scope decision recorded in this doc.
- **Slice 1: O1.** Exit tests:
  - owner closes ok: the standard bracket program unchanged, inverse runs
    once at teardown (existing TCK behavior re-pinned, goldens
    byte-identical);
  - the exemption holds: the corpus acquire form (`let fd = effect
    log_open(p) undo log_close(fd)`) stays admitted (positive fixture;
    this is the fixture that catches an O1 written without the
    exemption, which refuses every acquire in the corpus);
  - borrower closes refused: a component receiving a `Sock` parameter
    calls the declared `close`; rejection fixture in
    examples/rejections/ naming O1;
  - owner hand-close refused: the owner's own body calls its own declared
    inverse mid-session; rejection fixture (the double-close shape);
  - smuggled close refused: the declared inverse called inside an
    UNRELATED binding's undo expression, and inside a compensate
    expression; one rejection fixture each (the O1 position extension).
- **Slice 2: B1.** Exit tests:
  - borrowed escapes owner scope refused, ONE REJECTION FIXTURE PER
    CLAUSE: store into activation state (1); closure capture, both a
    borrow and the owner's own handle (2); non-owner return, keyed on a
    tainted carrier type, not just the bare handle (3); insertion into
    an escaping record/collection in both orders, insert-then-store and
    store-then-insert (4); a borrow in an undo expression, a borrow in a
    witnessed argument list, an OWNED handle in compensate args (5); a
    resource-tainted spawn config value (6); a resource-tainted handoff
    type (7);
  - phase-ordering premise pinned at runtime: a trace test (runs on py)
    demonstrating that Phase 1 brackets close before Phase 2
    compensations run in the same frame. Clause 5's compensate half rests
    on this ordering; the trace pins it so a future teardown-phase
    reordering cannot silently invalidate the rule's justification;
  - owner pool pattern still admitted: acquire at activation, store in
    own state, lend per provide call (positive fixture, runs on py);
  - swap shape covered: the consumer-stores-provider-handle program that
    motivated the rule is refused at check time (no runtime swap test
    needed; the point is it never compiles).
- **Slice 3 (deferred, sequenced with 294): shared.** Exit tests:
  - last release runs the inverse exactly once: N holders, release order
    permuted (including the acquiring frame releasing first), trace shows
    one close, in the last releaser's frame, LIFO-positioned;
  - a holder crashing without release: the ttl backstop fires the inverse
    once, and the residue/audit surface reports the lease lapse honestly;
  - shared across a process seam refused.
- **Seam re-pin (any slice): owned crosses seam refused.** Already green
  today via `resource_crossing_refusal`; re-pin with the mode-named
  diagnostic text so the message upgrade cannot regress silently.

## Open questions (left deliberately)

1. **Early release: no longer open-ended, decided by the slice-0 count.**
   O1 refuses mid-session close with a hint pointing at a surface that
   does not exist, and O1 plus activation-lifetime brackets makes a
   method-scope acquire leak-until-unload with no recourse. The decision
   procedure: slice 0 counts method-scope acquire forms in the corpus. If
   the count is nonzero, either the explicit-release surface (`release
   conn`: run the inverse now, discharge the bracket entry; interacts
   with 245's session escrow) is sequenced WITH slice 1, or v1 refuses
   method-scope acquires with a diagnostic naming the restructure (hoist
   the acquire to activation scope). If zero, early release defers as a
   follow-up item and the hint stands.
2. **Borrow-across-await.** An async provide method holding a borrow
   across an `await` extends the borrow's real duration without widening
   its scope. B1 admits it; whether the schedule-testing work (295) can
   interleave an owner teardown into that window on any tier decides if
   the rule needs an await clause.
3. **Rejection-parity oracle for self-host** (see the self-host note):
   which oracle family owns O1/B1 parity, decided at slice 2.

# Design: effect ownership modes, owned / borrowed / shared / transfer (item 308)
# Design: effect ownership modes, owned / borrowed / shared (item 308)

Status: OWNED+BORROWED v1 LANDED (2026-08-31, merged 66e2999); owned/borrowed
Status: OWNED+BORROWED v1 LANDED (2026-08-31, merged 66e2999) — owned/borrowed
inferred over the teardown accumulator with zero new grammar: R0 (acquire/
witnessed returns are nominal opaque handles), O1 (no hand-call of a declared
inverse), B1 (the seven-clause borrow-does-not-escape). F10 retention audit
SHIPPED and F9 method-scope early-release DECIDED/closed (the early-release form
is already refused, count zero); the inline § markers below are authoritative
per decision. SHARED landed: slice 3a reserved the contextual markers (PR #503)
and slice 3b is admitted by the parser and built on the reference tier (PR
#698), with the S1 spec below complete. TRANSFER's first surface is DECIDED in
TR1 below: a resource-typed `handoff` across a same-realm swap, where the
closing duty of the handle moves from predecessor to successor through a
swap transit ledger. TR1 is design only and decided, including OQ-T1 (the
receiving binding is seeded from the ledger and its own acquire does not
run); its implementation slice is next, and the acquire-head reservation in
`src/revl/parser.py` is the placeholder it replaces. PRODUCT-VISION, 243-261 arc.
per decision. SHARED + TRANSFER stay reserved contextual keywords (markers
landed, slice 3a / PR #503); the SHARED SLICE 3b SPEC IS NOW COMPLETE (S1
below, "S1 completed": the crash-path debt is paid by retiring the owned
out-of-frame ttl executor once cross-process shared is a premise, binding
teardown to the 294 counted-grant ledger, and re-using `revl recover` for the
whole-process crash with a new `reclaim` residue record). Implementation of
3b is the next slice and stays deferred, sequenced with item 294. TRANSFER
remains reserved and unspecced. PRODUCT-VISION, 243-261 arc.
Companion docs: [243-witnessed-externs.md](243-witnessed-externs.md),
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

## The modes, precisely
## The three modes, precisely

Each mode is a static discipline plus a runtime teardown consequence. The
runtime side reuses the teardown contract's entry kinds; `shared` added a
counted-grant runtime and `transfer` adds a nameable carrier for the
transferred bindings only (TR1).
runtime side reuses the teardown contract's entry kinds; only `shared` would
add one.

### `owned`

- **Who:** the activation that ran the acquire effect form (or spawn). One
  owner per handle, fixed for the handle's life unless the handle is
  transferred (TR1 below); v1 shipped no transfer.
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
- **Recommendation (S1 completed, PR #698):** build it as the 294 lease
  binding, not a bespoke refcount. The lease
- **Recommendation: defer shared out of v1 entirely,** and when it lands,
  build it as the 294 lease binding, not a bespoke refcount. The lease
  runtime already solved the two hard parts (durable consume-before-fire
  accounting, and a crash backstop so a SIGKILLed holder cannot pin the
  count forever). A bespoke refcount would re-derive both, which is
  exactly the parallel-mechanism shape the 294 doc refuses. Section
  "Honest scope" details the cut.

### `transfer`

- **Who:** the owner gives up a handle and the closing duty moves to one
  named receiver. The source does not keep a copy; the receiver is the
  sole owner and its teardown runs the close.
- **Static discipline:** a transfer is admitted only at a declared
  passing site, and the source never performs the close at all; the
  receiver accepts the duty explicitly, and the move is checked for
  exactly one owner.
- **Runtime consequence:** the closing DUTY of the handle migrates from
  the source frame to the receiver's through a swap transit ledger: one duty, one
  holder at every instant, never a copy and never a second registration.
  The reference tier has no nameable bracket entry today, so TR1 adds a
  carrier for the transferred bindings only. The first surface, its marker
  position, the protocol (including E-Stop and G7) and its exit tests are
  TR1 below.

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
- `shared` is a contextual keyword at the acquire binding
  (`let pool = effect shared Pool.open(url) undo pool.close()`), shipped
  in slice 3b. Contextual, not a lexer keyword, for the same reason
  `witnessed` is (243 Slice 1 refinement 2): the self-hosted lexer's
  keyword-set parity oracle stays untouched, and no program using
- `shared` is a future contextual keyword at the acquire binding
  (`let pool = effect shared Pool.open(url) undo pool.close()`), reserved
  now, not implemented in v1. Contextual, not a lexer keyword, for the
  same reason `witnessed` is (243 Slice 1 refinement 2): the self-hosted
  lexer's keyword-set parity oracle stays untouched, and no program using
  `shared` as an identifier breaks.
- explicit transfer is a source-level marker at the passing site, because
  it changes who runs the final inverse, which is author intent again.
  The marker position is now DECIDED (TR1): for the first surface it sits
  on the handoff state type (`handoff <key>: transfer <Type>`), not at
  the acquire head the parser currently reserves; a per-call marker
  remains the later surface. The item's realm-boundary requirement is not
  relaxed: realm-crossing transfer stays refused until its later slice
  (TR1 below, "Reconciling with the seam").
- explicit transfer (`transfer conn`), which the item text requires at a
  realm boundary, is likewise reserved and NOT in v1: v1 has no transfer
  at all, an owned handle stays where it was acquired, and a realm or
  process boundary refuses it (next section). When transfer lands it is a
  source-level marker at the passing site, because it changes who runs
  the final inverse, which is author intent again.

The recommendation in one line: ownership is contract, so it lives at the
source level like `emission[...]` does; but the compiler infers the mode
from position, and source spelling is reserved for the two places where
intent genuinely diverges from the default (`shared`, `transfer`). Shared
shipped its marker in slice 3b; transfer's marker is decided in TR1. The
owned/borrowed v1 itself shipped zero new grammar, which also means zero
self-host parser/lexer porting for that surface.
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

**The provide-method seam: what the author writes instead.** O1's
"acquiring binding's own `undo`" is a fix only where an acquiring binding
can exist. Inside a provide METHOD it cannot: only `spawn` (and a
result-declared host verb, item 397) may be acquired there, `result` is
not in scope in a site `undo` the way it is in the extern's own slot, and
re-minting a same-typed handle to release is exactly the double-close O1
refuses. So a seam that acquires a handle has no way to name it. The shape
that compiled before the site `undo` slot was argument-checked
(`_check_inverse_args`) passed something ELSE to the inverse; that is a
type error like any other.
refuses. So a seam that acquires a handle has no way to name it, and the
only shape that used to compile was one passing something ELSE to the
inverse. That shape compiled solely because the site `undo` slot was never
argument-checked; once it is (`_check_inverse_args`), it is a type error
like any other, and O1's hint at a seam names the reachable fix instead.

What a seam CAN write is the NAME-addressed release (item 2102): a site
`undo` that calls a release with the values the acquisition was given, in
the positions it was given them, re-deriving the resource from the same
address the acquisition used. `effect open_h(config.owner, id) undo
close_named(config.owner, id)` is that shape; a literal, a swapped pair,
another config field, a release called with none of the values, and no
site `undo` at all are each still refused. The release's callee is the
author's, exactly as the declared-inverse spelling leaves the inverse's
other arguments to the author; the VALUES are what is pinned.

The other reachable fix needs no new surface either. `witnessed` (item
243, extended to provide-method bodies by item 318) is the one
classification whose DECLARED inverse is actually replayed: the
acquisition carries no site `undo` at all, and `undo <inverse>(result)`
auto-registers on the enclosing activation's transactional accumulator,
once per acquisition, with `result` bound to what the acquisition
returned. That is precisely "release exactly the handle that was
acquired", on the frame whose teardown is the right one, on the `Ok`
branch only, which is why it is the wrong fix for a resource that must
also revert on a clean unload, where a site `undo` is what runs. Making
`result` available in a site `undo`, or admitting a general method-scope
acquire binding (the F9 surface, DECIDED below and not built), would each
be a second, weaker copy of one of these two; a handle-carrying form would
be a third. The refusal names both spellings.
That fix needs no new surface. `witnessed` (item 243, extended to
provide-method bodies by item 318) is the one classification whose
DECLARED inverse is actually replayed: the acquisition carries no site
`undo` at all, and `undo <inverse>(result)` auto-registers on the
enclosing activation's transactional accumulator, once per acquisition,
with `result` bound to what the acquisition returned. That is precisely
"release exactly the handle that was acquired", on the frame whose
teardown is the right one, on the `Ok` branch only. Making `result`
available in a site `undo`, or admitting a general method-scope acquire
binding (the F9 surface, DECIDED below and not built), would each be a
second, weaker copy of that mechanism; a handle-carrying form would be a
third. The refusals point at `witnessed` rather than inventing one.

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
   duty to close it moves", which is exactly what the `transfer` marker now
   decides (TR1 below); handoff shape-compat alone cannot express it,
   which is why a resource-typed handoff needs the marker.
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

**F10, SHIPPED (`src/revl/resources.py: retention_surface`).** One row per
resource-carrying parameter of a non-inverse extern or of a service
method, in `revl audit` and in its `--json` document under `retention`.
Declaration-derived and whole-program: a row exists because a signature
names a resource-carrying type, not because a body was walked, which is
what makes the surface stable (it moves only when a declaration moves),
enumerable, and diffable. A declared inverse is EXCLUDED: teardown calling
it with the handle exactly once is the contract working, not a retention
hazard. Present only when there is a row, so a handle-free composition's
audit document is byte-identical to before, the same conditional-presence
discipline `parallel_plan` and `secrets` hold to.

Every row is a MAY-retain, and the honesty runs in both directions. A row
does not prove a host body keeps the handle, and an ABSENT row does not
prove one does not: absence means only that no resource-typed parameter is
DECLARED. That is worth stating flatly because it bounds what a consumer
may do with this fact. `src/revl/ownership.py`'s whole-program retention
summary (item 445b) currently assumes every callee without a summary
retains everything, externs included, and it would be a real precision win
to narrow that. This surface CANNOT narrow it: a may-retain set is exactly
the assumption already in force. What it does is make the frontier
enumerable, which is the input a future `retains` / `borrows` declaration
marker would key on. Narrowing waits for that marker; nothing here is
licence to assume a non-retention that was never declared.

### F9, decided: method-scope early release

**The count came back zero, and that is not the whole answer.** The
decision procedure was a corpus count of method-scope acquire forms.
Parsing all 873 `.rvl` files with the real front end and walking every
`provide` method body finds FOUR method-scope acquisition sites, all four
`witnessed` mutation steps with no site `undo`
(`backends/go/scenarios/provide_method_witnessed.rvl:89,92`,
`backends/go/scenarios/method_compensate.rvl:52`,
`backends/java/scenarios/method_witnessed.rvl:56`), against 107
activation-scope acquisitions. GENERAL method-scope acquires: zero. Not
zero by accident either; `_lower_provide` refuses the form outright
zero by accident either — `_lower_provide` refuses the form outright
(`src/revl/lower.py`, "only `spawn` may be acquired inside a
provide-method body"), so option B of the original decision ("v1 refuses
method-scope acquires with a diagnostic naming the restructure") was
already true before this item was written, and is now pinned by a test so
it cannot regress quietly.

So the open question narrows to what CAN be acquired at method scope, and
that is two forms:

1. a result-declared host verb (item 397, today exactly
   `insert_if_absent`), which binds a checked `Bool`. No handle, no
   lifetime, nothing for F9 to decide.
2. `spawn`. This one yields a live instance, and it is already the one
2. `spawn`. This one yields a live instance — and it is already the one
   acquisition in the language with an early release.

**The surface exists, spelled in the grammar revl already had.** A spawn
is a CHILD FIBER (`docs/design-v2-instances.md`, item zero): its own
nested teardown scope. `w.dispose()` unloads it now, mid-method, written
`effect w.dispose() undo w.dispose()`. There is no explicit-release
surface owed, and building the general `release conn` form would be a
third weaker copy of a mechanism that already works, the same judgment
third weaker copy of a mechanism that already works — the same judgment
the provide-method seam paragraph reaches about `witnessed`.

**Why that does not disturb teardown**, which is the reason a general
release surface was deferred in the first place. Early release here
releases a SCOPE; it does not DISCHARGE AN ENTRY. The frame-adopted safety
net (`Frame.acquire`'s `lambda w: w.dispose()`) stays registered on the
enclosing activation's LIFO for its whole life, and `SpawnHandle.dispose`
is idempotent on its `_disposed` flag. The entry set G7 walks is exactly
what it was, so LIFO-completeness needs no re-argument.

**What a fault mid-scope does**, stated explicitly, proven at runtime in
`tests/test_ownership_f9_runtime_308.py`:

* **fault BEFORE the release.** The entry is registered and unrun. A
  provide-method fault is NOT an activation abort (`Frame._aborting` stays
  clear; only `Frame.abort()` or a session abort sets it), so it
  propagates to the caller and leaves the entry alone, and the entry runs
  at activation teardown in LIFO position, guarded. NOTHING IS MISSED.
* **fault AFTER the release.** The entry is registered and runs; the
  second dispose is a no-op. NOTHING RUNS TWICE.
* **E-Stop mid-scope (item 443's `halted`).** The entry is STRANDED either
  way: registered, not run, not dropped, kept for `revl recover`. Where
  the release already happened the stranded record names one entry whose
  replay is a no-op, so the halt OVER-reports by one. That is the R4-safe
  direction (residue is never under-reported) and it is precisely why
  direction — residue is never under-reported — and it is precisely why
  early release must not discharge: a discharge would drop the descriptor
  the halt is required to keep, and THAT would contradict
  `RevL.G7.estop_strands_everything`. Stranding acts on ENTRIES, early
  release acts on the EFFECT; the two ideas are adjacent and do not meet.

**What F9 therefore ADDS**, since the release surface needed nothing. The
nested scope's promise ("a request-scoped instance is reclaimed when the
request ends") was UNENFORCED. An `Instance[C]` is not an `extern
nested scope's promise — "a request-scoped instance is reclaimed when the
request ends" — was UNENFORCED. An `Instance[C]` is not an `extern
acquire` return, so it is not in the resource-taint base and no B1 clause
ever bound it: a provide method could return the handle, park it in
activation state, alias it, seat it in a carrier, or close over it, and
the instance then outlived the request it was scoped to while its
safety-net entry sat on the activation frame. F9 is B1's does-not-escape
rule applied to the one handle B1 could not see
(`_f9_instance_scope_scan`, `src/revl/lower.py`).

It keys on the BINDING, not on a type. `spawn` is syntactically evident at
the acquiring step, and putting `Instance[C]` in the taint base would also
bind the ACTIVATION-scope owner pool that the owner carve-out deliberately
admits. The admitted positions are a whitelist, not a blacklist: the
handle is read and not retained as the receiver of a host method call
(`w.dispose()`) and as the base of a provision read (`w.task.run(..)`),
which are the two things the instance surface is for. Everything else is
refused. Inside an arrow body even those two stop counting, because an
arrow's type erases what it closes over: B1 clause 2's reasoning, applied
arrow's type erases what it closes over — B1 clause 2's reasoning, applied
for B1 clause 2's reason.

**Additivity.** The corpus contains one method-scope spawn, in
`examples/rejections/g4_unmarked_handle_emission.rvl` (a rejection fixture
that returns an `Int`, not the handle); every other spawn is
activation-scope and stays admitted under the owner carve-out. A sweep of
all 258 compilable corpus files produces zero new refusals.

**What stays open.** `shared` landed (S1 completed, PR #698); `transfer` is
decided in TR1 below and its implementation slice is next. A general
explicit-release surface for an ACTIVATION-scope handle is still not in the
language, and O1's hint still says so honestly. F9 does not change either.
**What stays open.** `shared` and `transfer` remain deferred to item 294's
leases; a general explicit-release surface for an ACTIVATION-scope handle
is still not in the language, and O1's hint still says so honestly. F9
does not change either.

### S1: shared teardown, exactly once at last release (completed below; landed in PR #698)
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

### S1 completed (shared slice 3b): the debt paid, the executor retired

The three paragraphs above fix the DIRECTION. This subsection is the spec the
shared slice was told it owed, and it pays that debt by RESOLVING a tension the
earlier draft left implicit rather than by fleshing out the out-of-frame ttl
executor it named. The resolution follows from a fact stated two sections down
but not yet folded back into S1: **`shared` across a process seam is refused**
(see "Reconciling with the seam", `shared` row). Once that refusal is a
premise, every holder of a shared handle is co-located in ONE process with the
handle, and the crash shapes collapse to two, both already-shipped surfaces.

**Teardown vocabulary (the 294-lease binding, orderly path).** A `shared`
acquire mints a COUNTED grant in the 294 grant ledger, keyed off the acquiring
binding exactly as a task lease is (`{capability, component, session,
grantedAt, expiresAt, ...}` plus a `holders` count seeded at 1 for the
acquiring frame). The declared inverse is NOT a per-frame bracket; it is bound
to the count's zero crossing. Holders are counted at B1's crossing enumeration,
the same module and the same closed-world list: every position B1 clauses 1-7
name as a crossing into another activation's scope is a lease CONSUME
(count += 1, a durable ledger write); the receiving scope's teardown is a
RELEASE (count -= 1, a durable ledger write). This is the construction S1's
first paragraph named ("each crossing is a lease consume in 294 vocabulary");
the spec fix is only that the count is a ledger field, written
consume-before-fire (243 rule 4, the same fsync'd-append discipline
`replay-fence` uses), so it is reconstructible on recovery and never a bare
in-memory refcount. On the orderly path the frame that drives the count to zero
registers the declared inverse on its OWN LIFO stack as an ordinary `bracket`
entry, at release time, so G5/G7 are unchanged and a failed shared inverse is a
`bracket-fault` like any other. No new teardown phase, no new residue kind on
the orderly path. That half was already true in the draft; it is restated here
only so the crash half can say precisely what it does NOT reuse.

**Why the out-of-frame ttl executor is RETIRED, not specified.** The draft
owed "the executor, its residue kind, its WAL entry" for a liveness-gated
wall-clock reclaim. That executor existed to answer one danger: a ttl lapse
firing the inverse while a slow-but-alive holder still holds (the
manufactured-use-after-close S1 warns of). That danger requires a holder in a
DIFFERENT process from the reclaimer, so that the reclaimer can be alive to fire
while the holder is alive to be harmed. Cross-process `shared` is refused, so
that configuration is unreachable: a reclaimer and every holder share one
process's fate. Firing an inverse on wall-clock while that process is alive
would therefore always be firing on a live in-process holder (a hang, not a
crash), which is exactly the fail-dangerous close, so the shared slice fires NO
inverse on wall-clock at all. The 294 grant's ttl keeps only its shipped,
fail-safe role: lapsing the GRANT (an authority drop, the holder re-requests),
never firing the handle's inverse. A hung in-process holder pins the handle
until it releases or the process dies; that is S1's own "correct bias for
teardown", now with no wall-clock exception to it.

**Crash reclaim (the two shapes that remain).**

1. **Whole-process crash (SIGKILL / panic).** The handle, every holder, and the
   reclaimer die together; the OS reclaims a raw fd, and an external resource
   (a remote session, a lock) is left to `revl recover`. Recovery reads the
   durable ledger: a shared grant with `holders > 0` and an unrun zero-crossing
   inverse is RESIDUE whose forward referent still persists, so recover re-fires
   the declared inverse EXACTLY ONCE, gated by a new `shared-reclaim-fence` WAL
   record appended and fsync'd BEFORE the single attempt (the identical
   consume-before-fire discipline as `replay-fence`/`reissue-fence`: the fence
   proves an attempt was about to start, never that it ran, so a crash between
   the fence and the fire leaves "fenced-before-attempt, outcome unknown" and no
   double-close). The executor is thus `revl recover`, a SHIPPED out-of-frame
   surface, not a new expiry-sweep step in the ledger. This is the same shape
   the 294 task lease already has for crash ("ttl-backstopped on crash: a
   SIGKILL that skips teardown leaves the grant to lapse", 294 §"grant ledger");
   the only addition is that a shared grant's recovery re-FIRES an inverse where
   a task grant merely lets the authority lapse.
2. **E-Stop mid-count (item 443 `halted`).** Every registered release entry and
   the zero-crossing bracket are STRANDED, not run and not dropped, kept for
   `revl recover`, exactly as F9's spawn entry is. A holder halted before its
   release leaves the count OVER-reported (the handle stays pinned), which is
   the R4-safe direction (residue is never under-reported) and the same bias F9
   relies on. Nothing fires early.

**The residue kind (owed, and here it is).** When `revl recover` fires a shared
inverse from the ledger rather than a live frame, the outcome is reported as a
NEW residue record `reclaim` (fields: the capability/handle identity, the
`holders` count observed at reclaim, the reclaim basis `whole-process` or
`estop-stranded`, and `ok`/`err`). It is deliberately DISTINCT from
`bracket-fault`: a `bracket-fault` is an in-frame inverse that FAILED, whereas a
`reclaim` is an inverse that ran (or failed) OUTSIDE any activation during
recovery. A failed `reclaim` inverse carries its error in the same record
(there is no separate `reclaim-fault`; `ok: false` on the `reclaim` record is
the contract-grade fault, the merged-schema economy the teardown contract keeps
for `restore-residue`). `revl audit` surfaces `reclaim` rows so a lease-lapse
close is legible as a reclaim, which is the honesty the draft asked for.

**What this leaves genuinely open (named, not waved).** The count is exact only
to B1's crossing enumeration: a crossing B1 cannot see is a holder S1 never
counts, so the S1 count and the B1 refusal share one frontier and one honest
limitation (the retaining-extern surface F10 audits). Borrow-across-await (open
question 2) is the one in-process shape where a holder is alive but quiescent
for an unbounded window; because the slice fires no wall-clock inverse, an
awaiting holder simply pins the handle, which is correct, at the cost that a
truly-wedged holder pins it forever (the same liveness limit the whole language
accepts for a hung activation, not a new one shared introduces).

## TR1: transfer, the decided first surface (resource-typed handoff)

Status: DECIDED design, including OQ-T1 (the owner chose option B, see
"Decision and open questions"). Implementation not started; no grammar and
no runtime change has landed for it. The acquire-head reservation in `src/revl/parser.py`
(slice 3a, PR #503) is the placeholder TR1 replaces. The language reference
gains `transfer` when the implementation slice lands; documenting an
unimplemented spelling there would promise syntax the compiler refuses.

What TR1 moves is a DUTY: the obligation to close one handle, plus the live
handle itself. It does not lift an entry off a stack, because on the
reference tier an activation bracket is not an entry that can be lifted
(see "What carries a transferred bracket"). The design therefore adds a
nameable carrier, a refusal table and a three-holder protocol, and states
each against the code it touches.

### The surface and the marker position

The first transfer surface is a resource-typed `handoff` across a
same-realm provider swap. The predecessor declares that the state it
exports at replacement is transferred, not copied: the closing duty of the
named handles moves to the successor that re-provides the same key, whose
teardown then runs the close. The predecessor's teardown does not.

```
type Handles = { conn: Sock, log: LogHandle }

component Db provides db: DbService {
  handoff db: transfer Handles
  let conn = effect open_conn(config.url) undo close_conn(conn)
  let log = effect open_log(config.dir) undo close_log(log)
  // ...
}
```

The marker sits on the handoff state type, after the key's colon:
`handoff <key>: transfer <Type>`. That is the passing site for this
surface: the exported state is the value that moves. `transfer` stays a
CONTEXTUAL keyword, so the self-hosted lexer's keyword set is untouched and
`transfer` remains usable as an identifier and as a type name. The parser
rule that keeps this unambiguous: `transfer` is the marker only when it
directly follows the `:` of a `handoff` declaration AND the next token is
an identifier on the SAME source line. A `transfer` followed by `[`, by a
newline, or by anything else is an ordinary type name. (A state type is an
identifier or a parenthesized type there, `type_()` in
`src/revl/parser.py`; record types come from `type` aliases, as the example
does, so `{` is not a case.) The same-line rule matters because `route` and
`on` are plain identifiers, not keywords: without it, a type named
`transfer` ending a `handoff` line, followed on the next line by a `route`
or `on` statement, would read the next statement's first word as the type.

The acquire-head spelling the parser reserves today
(`effect transfer <acquire>`, the `marker == "transfer"` refusal in
`effect_form`, `src/revl/parser.py`) is retired: at the acquisition nothing
is being passed yet, and the marker changes who runs the final inverse,
which is a property of the passing site. The implementation slice replaces
that refusal; `effect transfer ...` then parses as an ordinary acquisition
of a callee named `transfer`, exactly as `effect shared()` does.

### The transferred vector

A transfer-marked handoff type must be a record type, and every field of it
must be resource-typed (an `extern acquire` handle, by the R0 taint base).
Each field names one activation-scope binding of the same name in the
component: `conn` and `log` above correspond to `let conn = effect ...`
and `let log = effect ...`. These bindings, and only these, form the
transferred vector. Correlation between predecessor and successor is by
FIELD NAME, never by position: the positional same-type correlation of the
copy path is a documented hazard (docs/design-v2-instances.md,
"Named hazard: same-typed positional collision") and the transfer path does
not inherit it.

The record may not carry non-handle fields in the first slice. A host
`Map` or `Pool` the component acquires is a `_Closable` in
`frame._resources` (`backends/python/runtime.py`, `_register_resource`) and
still crosses only by the copy path, which a transfer-marked slot does not
run; a component that needs both warm `Map` state and a transferred handle
is out of the first slice and says so in the refusal (T1).

### What carries a transferred bracket

On the py tier an activation bracket is an anonymous closure: the emitter
writes `yield lambda: <undo>` (`backends/python/emit.py`, the `let-effect`
step emission). The body generator hands that closure to cordis through
`Frame._tracked` and `Frame._guard`, which wraps it for continue-and-record
and the halt strand; the disposer list itself lives in cordis, not on the
frame, and cannot be edited. The runtime records that a bracket disposer is
"a bare closure with no entry object behind it" (`_record_phase1_residue`,
`_strand_registered`). `frame._resources` holds only `_Closable` host
resources (`Map`, `Pool`), never `extern acquire` handles. So there is no
entry to lift, and no list to lift it from.

TR1 introduces two carrier objects, for the transferred vector only.

- `_TransferDuty`: the single logical duty. Fields: `slot`, `name`, `args`
  (the evaluated acquire arguments), `handle`, `undo` (the evaluated
  inverse thunk), `holder` (the frame that currently owns it, or none) and
  `state`, one of `held`, `in-transit`, `claimed`, `closed`.
- `_TransferEntry`: a per-frame disposer, a callable the body YIELDS at the
  acquire statement's position (so it lands in cordis's LIFO list where the
  bare bracket would have), and which `_guard` wraps like a
  `_Transactional`. `_guard` today passes through any value that is not a
  function, `_Transactional` or `_Compensation`, so it is extended to wrap
  `_TransferEntry` too; otherwise the entry would skip continue-and-record
  and the halt strand. The entry is inert unless its frame holds the duty
  (see the protocol): it carries a reference to the duty and the frame that
  registered it, and acts only when `duty.holder is its frame`.

Three details make that work against the real `_guard`. First, `_guard`
decides the halted case BEFORE the disposer runs (`_guarded` calls
`_record_estop_stranded(_disposer)` and returns), so it cannot be left to
treat a `_TransferEntry` like a closure: it special-cases the entry and
delegates the halted decision to it, so an inert entry records nothing and a
claimed entry is not stranded in place of returning its duty. Second, the
entry's `_revl_entry` is the duty, so the existing dedup flag that
`_record_estop_stranded` sets on `getattr(disposer, "_revl_entry",
disposer)` lives on the duty and one duty is inventoried at most once
however many entries point at it; and `_strand_registered`, which today
enumerates only `_transactional`, `_compensations` and `_resources`, gains a
frame list of duties, filtered to the duties the frame holds and not owned
by a live transit ledger. Third, the entry is a callable object, never a
bound method (`_guard` returns a value whose `__self__` is the frame
unwrapped), and carries `_revl_method` so `_inverse_label` can name it in
residue.

For a component with a transfer-marked handoff, the emitter writes each
transferred binding as

```
_revl_frame._journal_begin()
conn = _revl_frame.transferable("conn", (config.url,), open_conn, close_conn)
yield _revl_frame.transfer_entry("conn")
```

The journaling prelude is the one the bare bracket carries (issue #1945,
host `Map` key judging), kept unchanged. On the claim path `call` does not
run, so the opened journal records no write and `_guard`'s take of it
reopens nothing; on the cold path `transferable` behaves as the bare
acquire did.

`transferable(name, args, call, undo)` returns the handle. Cold path (no
duty is in transit for this slot and name): it runs `call(*args)`, creates
the duty in state `held` with this frame as holder, and `transfer_entry`
returns its `_TransferEntry`. Claim path: see the protocol. Every other
bracket in the component stays an anonymous closure. A component without
the marker emits byte-identically to today; a marked component's IR gains
`handoff.transfer: true` and `transfer: true` on each transferred step,
absent otherwise. Other tiers refuse the marker at emit (T8) until they
carry the same objects; the reference tier is first, as `shared` was.

### The receiver and the admission rules

The receiver is the successor frame that re-provides the handoff key in the
same placement. This is the composition-level provider swap; a template's
per-instance state migration (item 10) is a different surface and is not
part of TR1.

Where a marked slot is exported by the running generation, the successor
has exactly one receiver or the swap is refused; there is no cold-start
fallback for that slot. Where no marked export is running (first load, a
successor over a handoff-less predecessor, `respawn`, an undo, a `--watch`
reload), `transferable` takes its cold path: it acquires normally and
nothing moves. Cold is a property of there being nothing to transfer, not a
fallback for a refused transfer.

Every refusal below is named, says where it runs, and names the repair.
Two places run them: lowering/admission (`_admit_handoff_replacement` in
`src/revl/admission.py`, only when a manifest is threaded, as the swap code
notes in `_plan_swap`) and a session gate that does not depend on the
manifest. The session gate is the authoritative one: it runs in
`_plan_swap`, after `_enforce_declared_disclosure` and before
`_capture_provider_state`, so a refusal leaves the running generation
untouched, as every other pre-teardown gate does. Both call one shared
function (`transfer_plan_refusals(old_ir, new_ir, migrate,
agent_authored)`), so the two cannot drift apart. The T5 refusal reuses
the G7 code and `ownership` category of `_b1_message("handoff", ...)`, but
not its text, which reads "cannot be carried by a `handoff` type" and would
mislead here; it gets its own message naming the missing accept.

B1 clause 7 refuses any resource-tainted handoff type today
(`src/revl/lower.py`, the `HandoffStmt` branch), which would refuse a marked
record of handles. The implementation slice exempts a handoff type carrying
the marker from clause 7 and validates it under T1 instead; an unmarked
resource-typed handoff stays refused by clause 7. That is why every T5 test
below that needs an UNMARKED successor of a marked predecessor uses an
unmarked non-resource type for it: an unmarked successor with the same
resource fields cannot lower at all.

| id | refusal | where |
|---|---|---|
| T1 | the marker on a handoff type that is not a record of resource-typed fields, or whose field has no same-name activation-scope acquire binding | lowering |
| T2 | a transferred binding is named in any other step's `undo`, `compensate` or witnessed argument, or in a provide method's effect `undo` | lowering |
| T3 | a transferred binding's own `undo` is not `<declared inverse>(<binding>)` with literal remaining arguments, or its acquire is conditional, not at activation top level, or reads anything but literals and config fields | lowering |
| T4 | the component is a template, has an async provide method, or uses `effect await` | lowering and session gate |
| T5 | no receiver for a running marked export: the successor declares no handoff, an unmarked one, a different field set or type, a different placement (a different realm, an added or dropped required or provided key), or two claimants | admission and session gate |
| T6 | the successor's acquire callee, argument expressions or `undo` for a transferred binding differ from the predecessor's | admission and session gate |
| T7 | the successor is agent-authored | session gate |
| T8 | the marker reaches an emitter whose tier has no transfer carrier | emit |
| T9 | a component with a transfer-marked handoff is placed in a process other than the session that swaps it | placement |

Decisions behind the table, each one a deliberate refusal rather than an
omission:

- **T2 and T3 (the predecessor's own teardown).** The predecessor's
  `_dispose_all` runs after the duty is lifted (see the protocol). Any
  other entry whose `undo` touches the handle would run against a handle
  the successor now owns, and would revert state the successor inherited
  (an `undo m.remove(k)` on a transferred map-like handle is exactly the
  data loss the copy path never had, because there the old teardown acts
  on the old copy). B1 admits an owned handle in other bindings' `undo`
  (`src/revl/lower.py`, the clause-5 carve-out for owned values), so TR1
  adds this refusal for transferred bindings only. T3 keeps the moved
  inverse closed over the handle alone: no predecessor-owned context
  (another binding, activation state) may be needed after the predecessor
  is gone, and its arguments are restricted so T6 can compare them.
- **T4 (quiescence).** No explicit drain of in-flight calls exists in
  `_plan_swap` or `_cut_over`: the session reaches `_dispose_all` through
  `self._run`, and async work the loop is running can progress while
  teardown awaits. The earlier draft of this section leaned on a drain it
  could not point to. TR1 instead refuses the components where a call can
  be in flight against the handle (async provide methods, `effect await`)
  and states that a quiesce point is the open work that would lift the
  refusal (OQ-T2).
- **T5, the added dependency.** The slot is `(key, placement)` and the
  placement covers every required key (`_handoff_slot`). A successor that
  adds or drops a required key is placed differently; for a running marked
  export that is refused, not cold-started, because the handle is bound to
  the frame's placement and there is no receiver for it elsewhere.
  Intended. The retire path is below. A marked successor over a running
  export of a different shape (an unmarked or `Map`-typed one) is refused
  by T5 itself, before teardown: `transfer_plan_refusals` compares the
  declared handoff shapes for a marked successor, field set and field types
  structurally, so a renamed record alias with the same fields counts as the
  same shape. The existing §5 gate in `_restore_provider_state` runs after
  the teardown and rolls back, so it stays as defence in depth and is not
  the gate this case relies on. Over no running marked export at all
  (first load, a handoff-less predecessor, a predecessor that is PENDING or
  FAILED and so holds no duties) the successor is cold, by the existing rule
  that a key nothing exported starts cold.
- **T6 (same acquisition).** Under the protocol the successor does not run
  its own acquire for a transferred binding, so the declared acquisition
  must be the one the predecessor ran. "Same" is two checks. Statically,
  the same extern callee, structurally equal argument expressions and the
  same `undo` after lowering. At the claim, the duty carries the evaluated
  `args` the predecessor acquired with, and `transferable` compares them
  with the arguments the successor evaluated; a config value that differs
  at load, which the static comparison cannot see, fails the claim and
  takes the rollback path. A successor that wants a different acquisition
  is not receiving a transfer.
- **T7 (agent-authored successor).** A transferred handle is live host
  reach, a strictly larger crossing than the copied state the disclosure
  gate (`_enforce_declared_disclosure`) lets an agent-authored successor
  inherit. Refused in the first slice when a running marked export would
  move to an agent-authored successor; with no running marked export
  nothing crosses and the successor is cold. Relaxing it is the owner's
  call.
- **T9 (process seam).** `resource_crossing_refusal` in
  `src/revl/placement.py` scans service method signatures, never a handoff
  state type, so it does not by itself refuse a marked slot. The
  implementation slice extends the placement check to read handoff types
  and refuse a marked component placed in a process other than the session
  that would swap it. Nothing crosses a seam when a marked provider merely
  has a remote consumer, so that is not refused; the hazard is a successor
  loaded in another process, because the transit ledger below is
  in-process and moving a duty across processes needs the WAL to move
  processes (see "Reconciling with the seam").
- **Retiring the marker.** A marked predecessor cannot be replaced by an
  unmarked or handoff-less successor through an ordinary swap (T5). The
  retire path is an explicit cold swap, `migrate="respawn"`: it lifts and
  moves nothing, so the predecessor's own teardown closes its handles and
  the successor starts cold.
- **`respawn` rollback, undo and `--watch`.** A rollback goes through
  `respawn` and an undo through `handoff_back` (issue #3293); neither is
  refused, and neither lifts: the lift is skipped for both, the
  predecessor's teardown closes its handles, and the restored generation
  starts cold for a marked slot. The undo dossier and the rollback's
  `stateDropped` report the slot as cold with reason
  `transfer-not-reversed`. A reverse move needs the protocol below run
  backwards and the restored generation's marker; it is not in the first
  slice. The `--watch` reload (`src/revl/run.py`) tears down and loads with
  no handoff at all, so a marked component is cold there too.
- **The swap report.** For a marked slot the copy path is skipped:
  `_capture_provider_state` takes no snapshot and `_restore_provider_state`
  does not iterate it. The forward swap's `handoff` block names the slot
  with `transferred: N` (the duties that moved) instead of `resources: N`.

### The protocol: three holders, one owner at every instant

A duty has exactly one holder at any instant. There are three, in order:
the predecessor frame, the swap's TRANSIT LEDGER, and the successor frame.
The ledger also keeps a reference to EVERY duty of the swap, in every state,
until commit or dispose, so that inventory and finalization never depend on
a frame that may already be disposed. The ledger is a third holding
structure; it is not a disposer stack and it
is never walked by either teardown phase (the teardown contract's "one
stack per activation" is kept for frames; the ledger exists only inside one
swap transaction). It is distinct from the item-245 session-owner escrow
(`_hold_for_session`), which holds witnessed and compensation entries for a
session verdict and never holds a bracket. The duty's `state` is `held`
(on a frame), `in-transit` (in the ledger, no holder), or `claimed`
(provisionally on the successor until the swap commits). The per-frame
`_TransferEntry` is the only thing cordis ever calls, and it acts as
follows:

- duty `held` by this entry's frame: run `undo` once, mark `closed`;
- duty `claimed` by this entry's frame: do NOT run `undo`; return the duty
  to the ledger (`in-transit`, no holder). This is what makes an
  unwinding successor safe: a claimed duty is inert on unwind;
- any other state or holder: do nothing.

1. **Lift.** In `_cut_over`, immediately before `driver._dispose_all`, the
   runtime moves each `held` duty of the predecessor's marked frames to the
   ledger: `holder` cleared, state `in-transit`, keyed `(slot, name)`. The
   predecessor's `_TransferEntry` objects stay in cordis's list (it cannot
   be edited) and are now inert, because their frame no longer holds the
   duty. Everything that can refuse the swap has already run in
   `_plan_swap`, so the predecessor is fully intact until this step. The
   handle stays open. Nothing is serialized: transfer bypasses
   `__revl_state__`.
2. **Predecessor teardown.** `_dispose_all` runs the rest of the
   predecessor's stack as usual. By T2 and T3 nothing on it touches a
   lifted handle, and the inert entries do nothing.
3. **Route and claim.** Before the successor loads, the session installs a
   route table `{successor component: slot}`, because a successor may be a
   differently-named component and is matched by slot, not by component
   name (`_handoff_acceptors`). When the successor loads, each
   `transferable(name, args, ...)` call finds the in-transit duty for
   `(slot of its component, name)`, compares `args` (T6), and, instead of
   running `call`, binds the ledger's handle: `holder` becomes this frame,
   state `claimed`. `transfer_entry` then yields a `_TransferEntry` for the
   duty at the acquire statement's position, so the successor's LIFO order
   is its own program order. The successor's acquire never runs, so an
   exclusive resource (a bound port, a lock, a single-session endpoint) is
   never opened twice and nothing derived from a fresh handle is later
   invalidated.
4. **Commit.** After the health gate (`_assert_successor_activated`) and
   `_restore_provider_state` succeed, at the end of `_cut_over`, the ledger
   asserts every duty was claimed and every `claimed` duty becomes `held`.
   An unclaimed duty is a failure and takes the rollback path.
5. **Rollback.** A failure of the successor's load, its health gate, the
   instance reconciliation or the provider-state restore reaches
   `_abort_swap` (the last two only for `StateIncompatible` today, so see
   the finalizer below for any other raise). A failure
   inside the successor's activation unwinds the successor's whole Phase-1
   list inside cordis before `_load` returns (the same path for the
   non-raising FAILED activation of item 372), so no step of `_abort_swap`
   can come first; the protocol therefore relies on the inert-on-unwind rule
   above: a claimed duty returns itself to the ledger instead of closing
   the handle. `_abort_swap` then disposes the successor, reloads the
   predecessor from code (the old frame is already disposed, so there is no
   stack to put anything back on), and the reloaded predecessor's
   `transferable` calls claim from the ledger exactly as in step 3, with
   its own slot route. The handle never closed and no snapshot is
   involved, which is what `_abort_swap`'s "state intact" promise needs.
   After the reload, `_abort_swap` commits the ledger as in step 4.

   **The finalizer.** Not every failure reaches `_abort_swap`: in
   `_cut_over`, `_dispose_all(self.ir)` and the lines up to the `try` are
   outside it, and `_reconcile_instances` and `_restore_provider_state` are
   guarded only against `StateIncompatible`; `_abort_swap` itself can raise
   in its own `_dispose_all` before the reload. Because a claimed duty is
   inert on unwind, a successor left live and uncommitted would later never
   close its handle. So the ledger carries a finalizer: `_cut_over` wraps
   everything from the lift to the commit in a `try/finally`, and
   `_abort_swap` wraps its whole body in one, and each finalizer, on any
   exit where the ledger is not committed, calls `ledger.dispose()`. That
   runs each duty still in the ledger exactly once in reverse lift order,
   including duties claimed by a frame that never committed, and records a
   failed inverse as `bracket-fault`. Nothing leaks and nothing runs twice.
   Neither hook exists today; adding both is part of the implementation
   slice.

Atomicity across slots: the session gate checks the whole cohort of marked
slots before the lift, and the ledger holds every lifted duty of one swap,
so a swap moves all its marked slots or none.

### E-Stop (443), G7 and recovery

- **E-Stop.** A move is an ownership change, not a discharge: it writes no
  `discharge` record and drops no inverse. Under `halted` every frame
  keeps its existing behaviour: `drain` does nothing, and `_guard` strands
  each plain entry it is handed instead of running it
  (`_record_estop_stranded`). A `_TransferEntry` is handled by the
  delegation described under "What carries a transferred bracket": it
  records nothing when its duty belongs to a live ledger or to another
  frame, and when its frame holds the duty outside any ledger the duty is
  stranded once, flagged on the duty. While a swap's ledger exists, the
  LEDGER inventories every one of its duties by duty state, `in-transit`
  and `claimed` alike, as `estop-stranded` records (outcome
  `not-attempted`, error type `estop`) naming the acquiring component and
  the binding, through `runtime.estop_residue()`; it does so by registering
  itself with the runtime at the lift. This is deliberate: `estop_residue()`
  walks only live frames (`_LIVE_FRAMES` is a weak set), and a disposed
  successor frame's record list can vanish, so a claimed duty must not
  depend on its frame to be reported. A claimed duty stays `claimed` under a
  halt; its entry does not return it to the ledger and does not run it.
  Neither `commit` nor `dispose` runs an inverse under a halt. A halt before
  the lift is not repeated by the lift: the lift re-checks the halt latch
  and refuses (the swap is refused, the predecessor's duties stay stranded
  as the halt recorded them), and the duty flag dedups any record that still
  arrives. A halt is reachable between the lift and the commit, with the
  predecessor frame already disposed and the successor possibly mid-load
  (`Frame.acquire` calls `_estop_check`, and `transferable` is specified to).
  A duty has one holder and one flag, so it appears in one inventory, once.
- **G7 (LIFO completeness).** Per frame, the stack discipline is unchanged:
  the predecessor's stack loses exactly the duties it no longer holds and
  the successor's gains exactly them, at the successor's own acquire
  positions, each inverse running once. The union over the two frames and
  the ledger is the close set.
- **What the formal theorems cover.** The Lean theorems in
  `formal/RevL/Theorems/G7_LifoComplete.lean` (`phase1_is_lifo`,
  `estop_strands_everything` and their neighbours) are statements about a
  single log. They hold for each frame's own log. They say nothing about a
  duty moving between two logs through a ledger, so TR1 claims no theorem
  for the move: the move is a new obligation pinned by the exit tests, and
  a two-holder log in the formalization is open work (OQ-T3).
- **Recovery: a requirement, scoped to the window.** `revl recover` walks
  `effect` records, acquires included (`src/revl/recovery.py`,
  `_roll_back`), but only for an opening that never reached
  `activation-complete`; an opening with the marker is rolled forward. That
  fixes what a move must and need not do. A kill BEFORE the lift is an
  ordinary running predecessor, exactly as for an unmarked bracket. A kill
  AFTER the successor's `activation-complete` is an ordinary completed
  activation holding an ordinary handle. Only the window between them,
  where the successor's opening has not completed, is new: the successor's
  acquire never runs, so that opening would have no acquire
  record for the handle, and recovery would roll it back without owing the
  handle that the predecessor's complete opening no longer reports. TR1
  therefore requires that the claim in step 3 appends an ordinary `effect`
  acquire record naming the same referent in the successor's opening, so a
  kill in the window is rolled back like any in-activation acquire, using
  the existing record kind and adding none. If the existing writer cannot
  express that, the finding reopens this design. The crash test below pins
  the window; the before-lift and after-completion cases are guards that the
  report equals an unmarked bracket's.

### Decision and open questions

- **OQ-T1: how does the receiving binding obtain the handle? DECIDED by
  the owner (PR #3325, 2026-11-22): option B.** On adoption the successor's
  binding is SEEDED from the transit ledger and its own acquire does not
  run (step 3). The rejected alternative, option A, runs the successor's
  acquire first and releases the fresh handle at adoption, so the
  transferred handle replaces it. The evidence behind the decision:
  - A fails for an exclusive resource. The fresh acquire runs while the
    predecessor's handle is still open (in transit or on a frame), so a
    bound port or a lock refuses it and the motivating case rolls back.
  - A leaves anything the successor derived from the fresh handle
    dangling after the release.
  - A cannot serve rollback. `_abort_swap` reloads the predecessor from
    code, which acquires again; with the handle held in the ledger that
    second acquire fails for the same exclusive resources, and if instead
    the held handle is closed first, the state that lived in the handle is
    gone, which breaks "state intact".
  - A is the shape the F9 precedent is closest to (an early release
    releases a scope without discharging an entry, see "F9, decided"), so
    it has a precedent where B has none: B skips an acquire the source
    text still contains, which T6 constrains.
  - B's WAL consequence: the successor's opening has no acquire record of
    its own, which is why the recovery requirement above has to add one.
    A would write it naturally.
  B is the only option that serves both adoption and rollback with one
  mechanism, and it is fail-closed: T6 refuses statically a successor whose
  acquire callee, arguments or `undo` differ from the predecessor's, a
  config value that differs at load fails the claim and rolls back, and the
  claim appends an ordinary acquire `effect` record so `revl recover` sees
  the handle. Option A is not carried forward.
- **OQ-T2: a quiesce point.** T4 refuses async components because the swap
  has no point where in-flight calls are known to have finished. A
  quiesce step in `_plan_swap` would lift T4; it is separate work.
- **OQ-T3: a two-holder log in the formalization.** See "What the formal
  theorems cover".

### The issue's checks, restated on this surface

Issue #2698 asked for four checks. On the first surface:

1. **The source cannot use the handle after the marker.** There is no
   source-order "after" in a prelude declaration. The check is that no
   predecessor code can CLOSE or revert through the handle once the duty is
   lifted: O1 refuses every hand close, T2 refuses a transferred binding in
   any other entry's `undo`, T3 keeps the moved inverse closed over the
   handle, and T4 refuses the components where a call could be in flight.
   No drain is claimed, and not every use stops: the lift precedes
   `_dispose_all`, which tears consumers down first, and a consumer's
   teardown can still call the provider's methods against the open
   in-transit handle. That is the same window a provider has today while
   its consumers unwind, the handle is open, and it ends before the
   successor loads.
2. **Exactly one owner exists after it.** One duty, one holder at every
   instant (frame, ledger, frame), with a single commit point and a
   rollback that returns the duty to the predecessor.
3. **The receiver's teardown includes the close (G7).** The successor's
   marked acceptance, with the same acquisition (T6), registers the duty on
   its own stack at its own position. A successor that cannot accept is
   refused (T5) with the G7 code, never cold-started.
4. **A borrow taken before the transfer cannot outlive it.** B1 forbids
   storing a borrow in another activation's state, so no activation holds
   one past its call. The in-flight window is closed by T4. A borrowed value
   cannot be in the transferred vector (B1 clause 1, T1).

### Exit tests (the implementation slice)

The new fixtures go in `tests/test_ownership_transfer_308.py`, runtime
cases on the py trace harness. Rule for every refusal test: it asserts the
specific T-id and message text, not merely that a `RevlError` is raised
(the marker is a parse error on main today, so a bare "raises" assertion
would pass there), and it is paired with a repaired program that is
admitted.

- **Move and close.** A same-realm provider holding a `Sock` is swapped;
  the successor closes it exactly once, the predecessor never does, and the
  trace shows the close in the successor's frame.
- **Exclusive resource.** A successor of a provider whose handle is an
  exclusive resource (a host stub that fails a second open) swaps and keeps
  serving; the successor's `call` is not invoked.
- **LIFO position.** A successor that acquires its own resources around the
  transferred binding closes them in its own reverse program order, the
  transferred handle at its acquire position, once.
- **Renamed successor.** The successor is a differently-named component
  that re-provides the key; the claim succeeds through the route table.
- **Predecessor undo touches the moved handle.** T2: a transferred binding
  named in another step's `undo` is refused at lowering, with the repair
  named.
- **Moved inverse needs predecessor context.** T3: an `undo` naming another
  binding, a config field or activation state is refused.
- **Double swap.** A to B to C moves the duty twice; the close runs once,
  in C, and the stranded inventory after a halt in C names one duty.
- **Accept rule.** T5: a successor with no handoff, an unmarked one (of a
  non-resource type: an unmarked resource-typed handoff cannot lower under
  B1 clause 7), a different field set, a `Map`-typed export shape, and two
  claimants are each refused before teardown with the running generation
  serving; the single same-placement marked successor, including one that
  names the same fields through a renamed record alias, is admitted.
- **Added dependency, dropped dependency and realm.** T5: a successor that
  adds a required key, drops one, or sits in another realm is refused, not
  cold-started.
- **Marked successor over no marked export.** Over a handoff-less
  predecessor, and over a predecessor that is PENDING or FAILED and holds
  no duties, it is cold (its acquire runs); over an unmarked or
  `Map`-typed running export it is refused by T5 before teardown.
- **Same acquisition.** T6: a successor whose callee, argument expressions
  or `undo` differ is refused statically; one whose config value differs at
  load fails the claim and rolls back.
- **Cold path.** A marked component's first load, a `respawn` rollback and
  a `--watch` reload each acquire normally and close once at teardown.
- **Agent-authored, template, async.** T7 and T4 each refuse with the
  running generation untouched, each with a paired admitted program.
- **Process-placed.** T9: a marked component placed in a process other
  than the session that swaps it is refused at placement, and a marked
  provider that merely has a remote consumer is admitted.
- **Retire path.** `migrate="respawn"` over a marked predecessor closes the
  predecessor's handles once and starts the successor cold.
- **Rollback and undo.** A rollback and an undo over a marked slot start
  cold with the report reason `transfer-not-reversed`.
- **Successor fails after the claim.** A successor whose activation raises
  mid-body, and one whose activation FAILS without raising (item 372),
  after claiming: the handle is not closed during the unwind (the claimed
  duty returns itself to the ledger), the reloaded predecessor serves with
  the same live handle (identity, not a copy), nothing is closed twice and
  nothing leaks.
- **Failure after the health gate.** The successor activates and claims,
  then `_restore_provider_state` or `_reconcile_instances` raises
  `StateIncompatible` and, separately, some other exception: the first
  rolls back with the live handle intact, the second reaches the finalizer,
  which closes the handle once and records a failed inverse as
  `bracket-fault`; neither leaves a live successor with a claimed,
  never-closed duty.
- **The predecessor's teardown raises.** `_dispose_all` of the predecessor
  raising after the lift reaches the finalizer: each in-transit duty is
  closed exactly once.
- **Rollback when the reload fails.** The reload raising, and the reload
  FAILING without raising (item 372), each leave the ledger disposed once:
  every remaining inverse runs once in reverse lift order and a failure is
  recorded as `bracket-fault`.
- **Unclaimed duty at commit.** A successor that never calls
  `transferable` for a transferred name (it is gone from its source, which
  T5 catches before teardown, so this is the runtime defence) fails the
  commit and rolls back with the handle intact.
- **Halt during the successor's load.** A halt after the lift and before
  the commit leaves every duty registered, not run, not dropped, each in
  `estop_residue()` exactly once, whether the duty is in transit or
  claimed on the successor, including when the successor frame has already
  been disposed.
- **Halt before the lift.** A halt that arrives, then a swap that proceeds:
  the lift refuses, the predecessor's duties stay stranded exactly as the
  halt recorded them, and no duty appears twice.
- **`_guard` under halt.** A unit test on `_guard`: an inert
  `_TransferEntry` records nothing; a claimed one neither runs nor strands
  its duty; an entry whose frame holds the duty outside a ledger strands it
  once.
- **Crash in a recorded session.** A kill in the window, after the lift and
  before the successor's `activation-complete`, rolls back with the handle
  reported outstanding while its referent persists, through the acquire
  record the claim appends; the test pins the exact report and that no new
  record kind appears. A kill before the lift and a kill after the
  successor's `activation-complete` report exactly what an unmarked bracket
  reports; those two are guards, not evidence.
- **Marker parsing.** The marker parses after `handoff <key>:`, and a
  marker on a non-resource type is refused with T1. The following already
  pass on main and are guards, not evidence of the change: `transfer` as a
  type name, `transfer[T]`, and `handoff k: transfer` ending a line with a
  `route` or `on` statement on the next line. `effect transfer <acquire>`
  is an ordinary acquisition of a callee named `transfer` (fails on main:
  it is refused today).
- **Seam.** A borrowed handle across a realm boundary is still refused by
  B1 (guard). The process-seam case is T9 above, not
  `resource_crossing_refusal`.
- **Tiers.** The marker on a non-py tier is refused at emit (T8).
- **Self-host.** `selfhost/lower.rvl` reads a `handoff` by skipping to the
  end of the line after reading the type with `type_at`, so it would read
  `transfer Handles` as a type named `transfer` and admit it. The marker is
  OUT of scope for the self-host ports in the first slice, exactly as the
  O1/B1 flow walk is: no marked program enters the self-host corpus, and
  the divergence is pinned in an executable test next to
  `tests/test_selfhost_ownership_gap_308.py` so it cannot be discovered by
  a red oracle. This pin passes on main (the reference refuses the marker
  there and the self-host reads it as a type), so it is a guard recording
  an accepted gap, not evidence for the change.
- **Guards (not evidence).** An unmarked program's emitted py and IR are
  byte-identical.

### What the implementation slice touches

Beyond the parser, lowering, admission, session gate and py runtime above:
the IR additions (`handoff.transfer`, step `transfer`) need the IR schema
under `schema/`, the TCK, and the grammar artifacts (`grammar/revl.ebnf`
through `revl grammar --write`, and `tree-sitter-revl`) for the contextual
marker, the placement check for T9, and the swap's reporting: the
`transfer-not-reversed` and `transferred: N` reports need
`live_state_components` and `_holds_state` (which read resources only) and
the undo and rollback dossiers to learn about marked slots. The self-host
ports and the other tiers are out (T8, and the self-host gap above).

### What the first surface does not include

The call-argument marker (`srv.take(transfer conn)`), realm-crossing
transfer, process-seam transfer, transfer of `shared` or `witnessed`
handles, a transfer reversed by undo, non-handle state in a transferred
slot, transfer to an agent-authored successor, async or template
components, a quiesce point, and a general explicit-release surface are
out. Each is named above with its refusal; none is approximated. The
per-call move is the next transfer surface after this one, and it will
reuse the surface-independent parts: the `_TransferDuty` carrier, the
three-holder protocol and the single-owner rule.

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
- **The realm boundary ("requires explicit transfer").** An owned handle
  is realm-bound: `isolate db in realm("tenant")` partitions providers, and
- **The realm boundary ("requires explicit transfer").** v1 has no
  transfer, so the realm rule collapses to: an owned handle is also
  realm-bound; `isolate db in realm("tenant")` partitions providers, and
  a handle acquired in one realm reaching a component of another realm is
  refused by the same promoted resource-type check (the checker now sees
  resource types, so it can see realm labels next to them). TR1 decides
  that transfer is the surface that moves the closing duty, and its first
  surface is a same-realm swap only: a transfer-marked handoff whose
  successor is placed in another realm is refused, never cold-started. A
  move across a PROCESS seam is still refused as well, because it needs
  the WAL to move processes, so it is not an afterthought of this slice.
  resource types, so it can see realm labels next to them). The reserved
  `transfer` marker is the future surface that would move the bracket
  itself; deferred, with the note that transfer across a PROCESS seam
  additionally needs the WAL entry to move processes, which is why it is
  not a v1 afterthought.

## What it buys, and what it formalizes

| property | today | under 308 |
| property | today | under 308 v1 |
|---|---|---|
| owner runs the final inverse | by construction (accumulator), unchecked elsewhere | formalized; O1 closes the out-of-band channel |
| no double-close | writable in admitted programs | refused (O1) |
| borrower cannot close | writable | refused (O1) |
| borrow cannot outlive owner | by construction for static topology (R3/G7); broken by stored copies under swap; closures can smuggle | refused (B1), including the swap shape |
| no resource in teardown-phase positions | writable; compensations are use-after-close by phase order | refused (B1 clause 5) |
| no borrow seated in another activation | writable via spawn config and handoff | refused (B1 clauses 6, 7) |
| shared teardown exactly once | no shared notion at all | S1 completed and landed (PR #698): 294 counted-grant ledger, orderly last-release as an ordinary bracket, whole-process crash via `revl recover` + `reclaim` residue, no wall-clock inverse |
| a handle can move with its closing duty | not expressible: the owner and its bracket stay together | DECIDED (TR1): a resource-typed `handoff` across a same-realm swap moves the closing duty from predecessor to successor through a swap transit ledger (OQ-T1 decided: option B); implementation next |
| shared teardown exactly once | no shared notion at all | spec COMPLETE (S1 completed): 294 counted-grant ledger, orderly last-release as an ordinary bracket, whole-process crash via `revl recover` + `reclaim` residue, no wall-clock inverse; impl deferred, sequenced with 294 |
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
5. The retention audit (F10, SHIPPED): `revl audit` lists resource-typed
   arguments reaching non-inverse externs and bridge services
   (report-only, the B1 limitation's mitigation).
6. The method-scope acquire decision (F9, DECIDED; see "F9, decided"):
6. The method-scope acquire decision (F9, DECIDED — see "F9, decided"):
   the count is zero because the general form is already refused, the one
   handle-yielding method-scope acquisition (`spawn`) already has its
   early release (`w.dispose()`, a nested teardown scope), and what F9
   adds is B1's does-not-escape rule extended to that handle. No new
   surface, no new grammar, no discharge.

Landed since this cut: `shared` (S1 above, PR #698). Decided and next:
`transfer` (TR1 above: a resource-typed handoff across a same-realm swap;
the call-argument marker, realm-crossing moves and process-seam moves stay
later slices; it is also the only honest future for handoff of
resource-carrying state, per clause 7). Still deferred: an
Deferred, each with its landing place named: `shared` (a 294 lease
binding; spec S1 above is now COMPLETE per "S1 completed", crash path resolved
by `revl recover` + the `reclaim` residue record, implementation the next
slice), explicit `transfer`
and realm-crossing moves (new source marker, WAL migration; also the only
honest future for handoff of resource-carrying state, per clause 7), an
explicit-release surface for an ACTIVATION-scope handle (F9 resolved the
method-scope half without one; this half still interacts with 245 session
commit and stays unbuilt), remote borrows (a service, not a proxy, is the
answer until proven otherwise).

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

**DECIDED (slice 2): port DEFERRED, gap named.** The self-host checker's
second half is not expression-only; it walks component/provide bodies for
second half is not expression-only — it walks component/provide bodies for
G4 emission-declaration and call-site typing, message-for-message with the
reference (`selfhost/checker.rvl` slice two, `tests/test_selfhost_checker.py`).
But it carries NO O1/B1 flow walk: those live in `src/revl/lower.py` over the
resource-taint base, and are not ported. Measured on the reference base
(a nominal `Sock`, its declared inverse, an `acquire`): a POSITIVE
owner-holds-and-lends program is admitted by BOTH sides; they AGREE, so a
positive ownership program is safe in the self-host corpus, while an O1
owner-holds-and-lends program is admitted by BOTH sides — they AGREE, so a
positive ownership program is safe in the self-host corpus — while an O1
borrower-close and a B1 non-owner-return are REFUSED by the reference (with
the item-308 message) and ACCEPTED by the self-host. So rejection parity
holds for everything the self-host actually checks, and the ONLY divergence
is an O1/B1 REJECTION, which no self-host corpus fixture spells today. The
port is deferred to the ownership-dialect self-host slice; until then no
O1/B1 rejection fixture may enter the self-host corpus (adding one would red
the oracle for this designed gap: the "discovered by a red oracle" outcome
the oracle for this designed gap — the "discovered by a red oracle" outcome
this note forbids). The gap is pinned executably in
`tests/test_selfhost_ownership_gap_308.py`: its two divergence pins flip when
the flow walk is ported, directing the porter to move the rejection fixtures
into the shared corpus and strike this deferral.

## Staged plan and exit tests

Slices are additive and land in order; each leaves the suite green and
per-backend goldens byte-identical (checker-only until the shared slice;
the transfer slice adds its marker, the carrier and the transit protocol).
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
  COUNT RECORDED: four method-scope acquisition sites, all `witnessed`
  mutation steps, against 107 activation-scope acquisitions; ZERO general
  method-scope acquires, because the form is already refused. See "F9,
  decided" for what that resolves and what it leaves.
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
- **Slice 3b: shared (spec completed above at "S1 completed"; landed in
  PR #698).** Exit tests, each grounded in the completed spec:
- **Slice 3b: shared (spec completed above at "S1 completed"; sequenced
  with 294).** Exit tests, each grounded in the completed spec:
  - `shared` admitted only at an acquire binding: the reserved-refusal from
    slice 3a flips to admission for `let p = effect shared Pool.open(url)
    undo p.close()`; a `shared` marker anywhere but the acquire-binding
    position stays refused (positive + guard);
  - last release runs the inverse exactly once: N holders, release order
    permuted (including the acquiring frame releasing first), trace shows
    one close, in the last-releaser's frame, LIFO-positioned as an ordinary
    `bracket` entry (the orderly path; runs on py). The 295 schedule-testing
    interleavings are the permutation source;
  - the holder count is a durable ledger field, not an in-memory refcount:
    a consume and a release each appear as a fsync'd ledger write, and a
    count reconstructed from the ledger after a simulated mid-count restart
    equals the live holder set (pins the consume-before-fire discipline);
  - whole-process crash: `revl recover` re-fires the declared inverse
    EXACTLY ONCE from a shared grant with `holders > 0`, gated by the
    `shared-reclaim-fence` (a re-run of recover after the fence but before
    the fire does NOT double-close), and the outcome is a `reclaim` residue
    record (basis `whole-process`), surfaced by `revl audit` as a reclaim
    and NOT as a `bracket-fault`;
  - E-Stop mid-count: the release entries and the zero-crossing bracket are
    STRANDED (kept, not run), the count is over-reported not under-reported
    (R4-safe), and the stranded set is what `revl recover` reads back;
  - NO wall-clock inverse: a shared grant whose ttl lapses while its process
    is alive does NOT fire the handle's inverse (only the 294 GRANT lapses,
    fail-safe); a test asserts the handle stays open while a holder is still
    counted, so a future change that wired ttl to the inverse reds here;
  - shared across a process seam refused: `resource_crossing_refusal`
    already refuses a shared (resource-typed) handle crossing; re-pin with
    the mode-named message ("shared resource ... refused across the process
    seam; cross-process shared is a distributed count, not a refcount").
  - self-host: no O1/B1/S1 rejection fixture enters the self-host corpus
    until the ownership-dialect slice ports the flow walk (the slice-2
    deferral in "Self-host note" extends to S1 unchanged; S1 is checker/
    recover surface, no new grammar).
- **Slice 3c: transfer (TR1 decided above, including OQ-T1;
  implementation next).** TR1 is the whole design: the marker at the handoff state type
  and its parse rule, the transferred vector and its carrier, the refusal
  table T1-T9, the three-holder protocol with its E-Stop and G7 placement,
  the realm and process-seam refusals, and the exit tests listed there. No
  grammar or runtime change has landed yet. This slice removes the
  acquire-head reservation, admits the contextual marker at the handoff
  type only, adds the carrier to the py tier and refuses the marker on the
  other tiers; the self-hosted lexer's keyword set is untouched because
  `transfer` stays contextual, and the self-host ports stay out of scope.
  OQ-T1 is decided (option B), so this slice can start.
- **Seam re-pin (any slice): owned crosses seam refused.** Already green
  today via `resource_crossing_refusal`; re-pin with the mode-named
  diagnostic text so the message upgrade cannot regress silently.

## Open questions (left deliberately)

0. **Transfer: OQ-T2 and OQ-T3 live in TR1, "Decision and open
   questions".** OQ-T1 (how the receiving binding obtains the handle) is
   decided there (option B). OQ-T2 (a quiesce point) and OQ-T3 (a
   two-holder log in the formalization) are follow-on work that lifts a
   first-slice refusal.

1. **Early release: CLOSED (F9). See "F9, decided".** The count is zero
   general method-scope acquires; the form is already refused, so option
   B was already in force. The one handle-yielding method-scope
   acquisition, `spawn`, already releases early through its own nested
   teardown scope, and doing so releases a SCOPE without DISCHARGING AN
   ENTRY, which is what keeps G7 and the `halted` verdict consistent with
   it. What was actually missing was enforcement of the scope's promise,
   and that is what F9 shipped. An explicit-release surface for an
   ACTIVATION-scope handle is still absent and O1's hint still says so.
2. **Borrow-across-await.** An async provide method holding a borrow
   across an `await` extends the borrow's real duration without widening
   its scope. B1 admits it; whether the schedule-testing work (295) can
   interleave an owner teardown into that window on any tier decides if
   the rule needs an await clause.
3. **Rejection-parity oracle for self-host: DECIDED (slice 2). See the
   self-host note.** The self-host checker covers component/provide typing and
   G4 but not the O1/B1 flow walk; the port is deferred and the one divergence
   (an O1/B1 REJECTION, which no self-host corpus fixture spells) is named and
   pinned in `tests/test_selfhost_ownership_gap_308.py` so it cannot be
   discovered by a red oracle.

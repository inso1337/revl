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
   analysis into the frontend.

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
   the frame that owns the bracket.

6. **`spawn` is already an owned resource.** The parser forces `spawn C ...
   undo s.dispose()` (parser.py, the G4 hint at the spawn effect form): the
   spawning frame owns the spawned component and its teardown. 308's
   vocabulary applies to it unchanged; nothing new is needed there in v1.

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
  is exactly how a borrow is created.
- **Runtime consequence:** none new. The existing `bracket` entry (or
  `transactional` for witnessed effects) IS owned-mode teardown: LIFO at
  the owner's frame, exactly once, G5-infallible by contract.

### `borrowed`

- **Who:** every other position a resource-typed value reaches: a service
  method parameter, a call result received from another component's provide
  method, a resource-typed field of a record handed over.
- **Static discipline:** use, do not close, do not keep. (a) Rule O1
  applies (a borrower cannot invoke the declared inverse). (b) The borrow
  is confined to the receiving scope: it may not be stored into
  activation-level state, captured by a closure, returned onward from a
  provide method, or inserted into a container or record that escapes the
  scope (rule B1 below). Passing it further DOWN a call chain is fine; the
  callee's parameter is just another borrow.
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
    exhaustion (count zero, or ttl on crash) triggers the inverse.
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
refusal and the new frontend refusals cannot drift apart.

### O1: no hand-call of a declared inverse (owner and borrower alike)

**Rule.** The set of closing operations is DEFINED as: the declared `undo`
callees of `acquire` (and `witnessed`) externs, plus `dispose` on a spawn
handle. A call to any of these, in any body position (pure call, effect
acquisition, emission), on a resource-typed argument, is refused. The
accumulator owns the final inverse; source code never performs it.

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
value the current activation did not itself acquire) may not:

1. be assigned or `effect`-inserted into activation-level state (a
   component `let`/`var` binding, or a container acquired at activation
   level);
2. be captured by a closure (any closure; v1 does not distinguish escaping
   from non-escaping closures);
3. be returned from a provide method (only the OWNER returning its own
   handle is admitted; that return is the borrow-creating move);
4. be placed into a record or collection value that then does any of 1-3
   (the resource-taint fixpoint already identifies which types carry
   handles, so this is the same check applied to the carrying value).

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
method-scope bindings live per invocation, closures are the only value
that packages a scope up and moves it. B1's four clauses are exactly the
three widening channels plus the transitive-carrier closure. The check is
a per-scope flow walk in the checker, the same shape as the existing
Opt-escape analysis, not a constraint solver. What this coarse
stratification CANNOT express is a borrow legitimately parked across
method invocations but still shorter-lived than the owner; v1 refuses
that shape deliberately (restructure so the owner holds it). If real
programs hit that wall, the escape hatch is a later, finer analysis, not
a v1 loosening.

**Owner carve-out, stated explicitly.** The owner storing its OWN handle
in its OWN activation state is admitted (same frame, the bracket and the
store tear down together under G7 LIFO), and is the normal pool pattern:
acquire at activation, store, lend per call. B1 binds borrows only.

### S1: shared teardown, exactly once at last release (deferred with shared)

**Rule (specced now, implemented with the shared slice).** A `shared`
acquire registers, in each holder's frame, a release entry; the declared
inverse is bound to the count's zero crossing and runs exactly once, in
the frame that performed the last release. Static side: `shared` is only
admitted at an acquire binding; holders are counted at the points the
handle crosses into another activation's scope (each crossing is a lease
consume in 294 vocabulary). Crash honesty: the count rides the 294 grant
ledger (durable consume-before-fire), and the ttl backstop bounds a
holder that died without releasing; without that backstop a refcount
turns one SIGKILL into a permanently pinned handle, which is why the
bespoke-refcount option loses.

**Exactly-once under G7.** The zero-crossing inverse joins the last
releaser's LIFO stack as an ordinary bracket entry at release time, so
G5/G7 hold unchanged: no new teardown phase, no emission in teardown, and
the residue schema (teardown-contract.md) needs no new record kind for it;
a failed shared inverse is a `bracket-fault` like any bracket.

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
| shared teardown exactly once | no shared notion at all | specced (S1), deferred to the 294 lease |
| owned cannot cross a seam | refused (363/F1) | unchanged, mode-named diagnostic |

Relation to the arc: G5 is untouched (no new teardown-time surface; the
shared inverse is an ordinary bracket entry). G7's guarantee strengthens
from "the accumulated inverses replay LIFO-completely" to "and the
accumulated inverses are the ONLY closes, so the replay set is the whole
close-set". 247 compensations are orthogonal (they offset emissions, they
never close resources; no interaction). 243 witnessed handles are covered
because witnessed externs sit in the same externs table and their
declared `undo` joins O1's closing set. The 295 schedule-testing note
(route shared-witnessed-resource interleaving findings into this design)
lands in the shared slice: interleaved release orders across fibers are
exactly what the S1 exit test permutes.

## Honest scope: the minimal useful v1

A full ownership system (transfer, finer borrow lifetimes, shared,
proxied remote borrows) is a deep type-system change, as the item says.
The minimal cut that pays for itself immediately:

**v1 = owned + borrowed, zero new grammar.**

1. Promote the resource-taint closure into the frontend (shared module;
   distribute.py imports it).
2. O1: no hand-call of a declared inverse.
3. B1: borrow does-not-escape (the four clauses, with the owner
   carve-out).
4. Mode-named diagnostics, including upgrading the seam refusal's message.

Deferred, each with its landing place named: `shared` (a 294 lease
binding; spec S1 above), explicit `transfer` and realm-crossing moves (new
source marker, WAL migration), early release (an explicit-release surface
that discharges the bracket; interacts with 245 session commit), remote
borrows (a service, not a proxy, is the answer until proven otherwise).

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

- **Slice 0: shared resource-type module + report-only sweep.** Move
  `_resource_taint` (and `_resource_types`, `_resource_in`) to a frontend
  module; distribute.py re-exports. A `revl audit` report-only listing of
  would-be O1/B1 hits over the corpus. Exit: distribute/placement tests
  unchanged; sweep output reviewed and carve-outs (if any) amended here.
- **Slice 1: O1.** Exit tests:
  - owner closes ok: the standard bracket program unchanged, inverse runs
    once at teardown (existing TCK behavior re-pinned, goldens
    byte-identical);
  - borrower closes refused: a component receiving a `Sock` parameter
    calls the declared `close`; rejection fixture in
    examples/rejections/ naming O1;
  - owner hand-close refused: the owner's own body calls its own declared
    inverse mid-session; rejection fixture (the double-close shape).
- **Slice 2: B1.** Exit tests:
  - borrowed escapes owner scope refused, one fixture per clause: store
    into activation state; closure capture; non-owner return; insertion
    into an escaping record/collection (the transitive-carrier shape,
    exercising the taint fixpoint);
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

1. **Early release.** O1 refuses mid-session close with a hint pointing at
   a surface that does not exist. Is an explicit `release conn` statement
   (run the inverse now, discharge the bracket entry) worth a small
   follow-up item, and how does it interact with 245's session escrow?
2. **Borrow-across-await.** An async provide method holding a borrow
   across an `await` extends the borrow's real duration without widening
   its scope. B1 admits it; whether the schedule-testing work (295) can
   interleave an owner teardown into that window on any tier decides if
   the rule needs an await clause.
3. **Rejection-parity oracle for self-host** (see the self-host note):
   which oracle family owns O1/B1 parity, decided at slice 2.

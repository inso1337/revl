# Design: `Stream[T]` reactive types (item 130)

Status: design proposed (2026-08-31). No implementation in this doc. This is a
large, design-first feature (~3-4 wk); everything here is a decision plus its
justification plus exit tests, to be built in the sliced plan at the end.
Base: `origin/main` @ `b5c85ee`. Every `file:line` anchor below was read at that
sha. Companion docs, all reused wholesale rather than re-derived:
[308-effect-ownership-modes.md](308-effect-ownership-modes.md) (the resource
ownership model this feature is an instance of),
[131-async-effect-composition.md](131-async-effect-composition.md) and
[async-extern.md](async-extern.md) (the async coloring and the non-blocking
provider),
[teardown-contract.md](teardown-contract.md) (the LIFO accumulator, G5/G7, the
residue schema), `docs/time-coeffect.md` (the timer, item 57, the reactive
coeffect this generalizes and whose wasm refusal it mirrors).

## What 130 asks, and the scope fence

The roadmap (docs/v2.0-roadmap.md item 130): a verified reactive sequence, a
provider emits values over time, a consumer processes them as they arrive;
provider teardown closes the stream, consumer teardown releases the
subscription (LIFO); the language-level form of reactive coeffects for
event-driven sagas. The blast radius the roadmap names is parser, typecheck,
lower, all six backends, and the async coloring (A1/106/131).

This document deliberately does NOT propose a general reactive calculus (no
`combineLatest`, no `switchMap`, no scheduler algebra, no hot/cold taxonomy).
v1 is exactly:

- the type `Stream[T]`, and a RESERVED richer form `Stream[T, State]` (below);
- a small API: `subscribe` / `next` / `close`, and the combinators
  `map` / `filter` / `take` / `merge`;
- the async-iteration form `every x in subscription { ... }`;
- one consumer per subscription, explicit `close`, bounded buffering, a
  deterministic test-time clock, documented refusals for the tiers and shapes
  v1 does not serve. No implicit multicast, no automatic retry.

The core is deliberately small because its teardown story has to be airtight,
and airtight is only cheap where the shape reuses machinery that already
proved itself. The rest of this doc is mostly an argument that the reuse is
exact.

## The model that already exists (do not reinvent it)

Four pieces of shipped machinery carry almost the whole feature. Naming them
here is most of the design.

1. **A subscription is an item-308 `acquire` resource.** `subscribe` is an
   `acquire`-classified extern returning a nominal opaque handle
   `Subscription[T]`; its declared `undo` is `close`. This is the exact shape
   `src/revl/resources.py` and the item-308 checks are built for:
   `acquire_return_is_nominal_handle` (`resources.py:46-68`) admits the bare
   nominal `Subscription`, the acquire form forces an `undo`
   (`lower.py:2561-2566`, "acquire extern must declare `undo` (G4)"), the
   inverse registers as a `bracket` entry on the acquiring activation's
   accumulator (teardown-contract.md, "three entry kinds, one stack"), O1
   forbids anyone else hand-calling `close` (`lower.py:7075-7115`, code=G7
   category=ownership), and B1 confines the handle from escaping its owner
   (`lower.py:7130-7411`). Nothing about the subscription's lifecycle is new;
   it is `owned`-mode discipline (`lower.py:6978-6985`) applied to a new
   handle type.

2. **The consumer body is a reactive coeffect body, exactly like a timer.**
   `every 30s { ... }` lowers to a body step `{"step": "timer", "mode":
   "every", "interval_ms": N, "body": [...]}` (`lower.py:8566`), driven by the
   runtime AFTER activation, whose inverse is the runtime's derived cancel (an
   R5 withdrawal), not an author-written `undo`. `docs/time-coeffect.md` calls
   time the canonical reactive coeffect (item 57). A stream is the same shape
   with the clock replaced by a value source: `every x in sub { body }` is a
   reactive body step, driven after activation, cancelled at teardown. This is
   the single most important structural decision in the doc, and section
   "Why `every` is a body step, not a loop" defends it.

3. **The provider does async I/O without blocking the consumer, per item
   131.** A stream provider is an `async` emission source (async-extern.md
   families 1 and 2). The consumer's `next` is a suspension the runtime awaits
   on the reactive body's behalf, and while it awaits, other fibers keep
   running (`backends/python/runtime.py` `asyncio.gather`, the R2 PENDING
   model, 131 doc section 4). The async coloring fixpoint
   (`_async_callables`, `emission_analysis.py:175`; stamped at
   `lower.py:5182-5183`) is the propagation vehicle, unchanged.

4. **Teardown is the shipped LIFO accumulator.** One per-activation LIFO
   disposer stack, `bracket` entries replay LIFO on commit AND abort alike
   (teardown-contract.md; `backends/python/runtime.py:1190` `Frame`), G5
   forbids emission in teardown (`diagnostics.py:29`), G7 is the derived LIFO
   (`diagnostics.py:31`), the no-residue proof is R4
   (`run.py:1139-1156`, `erase_report.py:338-366`), and mid-body failure
   reverts-and-contains under A8 / L-Raise (`diagnostics.py:46`). The stream
   close is a `bracket` inverse and needs no new entry kind.

A note the exploration flagged and this doc adopts: the task brief called the
teardown property "A8 (no residue)", but this repo splits that name. A8 is
mid-body revert-and-contain (`diagnostics.py:46`); the after-teardown
nothing-left proof is R4 (`run.py:1139-1156`); boundary residue after a crash
is a third notion (`recovery.py:_residue_proof`, `recovery.py:740`). Where
this doc says "no residue" it means R4, and it cites R4.

What is genuinely NEW, and therefore where the risk concentrates:

- a value SOURCE reactive coeffect (the timer's source is the clock, which is
  pure and infallible; a stream's source is a fallible async host producer);
- a bounded buffer between an async producer and a reactive consumer, with a
  backpressure policy the timer never needed;
- a terminal event (`closed`) that the timer, being endless-until-cancelled,
  never had;
- the six-tier delivery of an open-ended async producer, which is exactly
  where wasm (and, today, java) cannot follow.

## Surface syntax

### The type

```revl fragment
Stream[T]           // a source of T values over time; single consumer
Subscription[T]     // the acquired handle; a resource (item 308)
Event[T]            // variant: value(T) | closed(Reason)
Reason              // variant: ended | failed(Str) | stalled
```

`Stream[T]` is a first-class type usable in service signatures and records.
`Subscription[T]` is the nominal opaque handle an `acquire` produces; it is
resource-typed and therefore confined by 308 (it cannot cross a seam by copy,
cannot be stored to escape its owner, and so on). `Event[T]` and `Reason` are
ordinary variants, denotable and matchable.

`Stream[T, State]` is RESERVED, not in v1. The second parameter would be a
lifecycle typestate (`Created` / `Active` / `Paused` / `Closed`) checked at
compile time. The exploration confirmed revl has no typestate mechanism today
(no `resources/` state-parameter machinery; the resource handle is nominal and
stateless), so a real typestate is new checker work, and pause/resume (the
only transitions a typestate buys over the plain handle) overlaps the
backpressure policy below. v1 ships `Stream[T]` with the two states the
bracket already gives for free (Active after `subscribe`, Closed after `close`
or a terminal event) and reserves `Stream[T, State]` exactly as 308 reserves
`shared` and `transfer` (`lower.py:6988-6991`): a contextual surface named now,
implemented later, breaking no program that uses `State` as an identifier.

### The API

```revl sketch
// subscribe is an acquire extern; close is its declared inverse.
// The acquire form forces the undo clause (parser.py:16, G4).
let sub = effect await source.subscribe(buffer: 64, on_overflow: suspend)
          undo sub.close()

// next is the semantic primitive the consumer form is defined over.
// It suspends until the next event; its type is Event[T], never bare T.
//   sub.next() : Event[T]

// close() is the declared inverse. By O1 it is callable ONLY through the
// bracket above; a hand-call anywhere else is refused (lower.py:7100-7115).

// combinators build a NEW Stream description; the effect happens at subscribe.
let evens = source.filter(fn(x) { x % 2 == 0 }).map(fn(x) { x + 1 }).take(100)
let both  = left.merge(right)
```

`subscribe` takes the buffer bound and the overflow policy (see Backpressure).
`next` returns `Event[T]`, a variant, so the terminal is in the type: there is
no bare `T` that could be confused with a missing value, and the terminal is
delivered exactly once. `map` / `filter` / `take` / `merge` are pure
descriptions over `Stream[T]` (iterator-adapter style), returning a new
`Stream`; `take(n)` closes the subscription after n values (a synthesized
terminal); `merge` combines two sources into one single-consumer stream.

### `every x in subscription { ... }`

```revl sketch
component OrderProjector requires orders: OrderSource {
  let sub = effect await orders.subscribe(buffer: 128, on_overflow: suspend)
            undo sub.close()

  // a reactive body: driven by the runtime after activation, cancelled at
  // teardown BEFORE the bracket above closes the subscription (the guarantee).
  every order in sub {
    emit projection.apply(order)          // effectful body, capability-checked
  } closed reason {
    // Active-only: runs iff the terminal arrives while still delivering.
    // NOT a teardown hook. Cleanup that must run belongs in undo/compensate.
    emit log.note(`stream ended: ${reason}`)
  }
}
```

Grammar delta (one new body-step production, and a combinator/type surface
that rides the existing type and call grammar):

```
componentstmt := ...
  | 'every' IDENT 'in' expr '{' step* '}' ['closed' IDENT '{' step* '}']
```

`every ... in ...` is a component body step in the same family as `TimerStmt`
(`parser.py:993`), NOT an expression and NOT a statement inside a provide
method. The optional `closed <reason> { ... }` block is the terminal handler,
and it runs only during Active delivery (see the adversarial review). A later
slice may add the fused `every x in source { ... }` (subscribing inline, the
bracket synthesized by the step, closer to `every 30s`); v1 keeps the acquire
explicit so the resource is visible to a reader and to 308 unchanged.

### Why `every` is a body step, not a loop

The tempting spelling is a loop in the activation body:

```revl sketch
// REFUSED shape (illustration): this never lets the activation finish.
let sub = effect await source.subscribe(...) undo sub.close()
loop { match sub.next() { value(x) => handle(x), closed(r) => break } }
```

An activation body must run to completion to move the component `PENDING ->
ACTIVE` (`run.py:685`) and start serving. A consumer loop in the activation
body would never return, so the component would never become Active. The timer
solved this years ago: the reactive body is registered at activation and DRIVEN
by the runtime afterward. `every x in sub { }` takes the same shape. The
subscription bracket is registered at activation scope (owned, on the LIFO
stack); the `every` body is a runtime-driven consumer the teardown cancels.
`next` is therefore the protocol primitive the driver calls, not a call the
author loops on; direct `next` looping in author code is deferred (it needs a
non-activation async context, which is its own item). This is the reactive
coeffect the roadmap asked for, realized as the timer's generalization.

## The type and checker story

`Stream[T]` checks as an ordinary parameterized nominal type in
`typecheck.py`; it carries no async color in its type (async is a tier-level
property, never in the type language, per async-extern.md section 1's rejected
return-type-marker and `typecheck.py:199-268`). The reactive-ness lives in the
`subscribe`/`next` externs and the `every` body, not in `Stream[T]` itself.

The subscription-as-resource composes with 308 with no new rule:

- **R0.** `subscribe` returns `Subscription[T]`, a bare nominal handle, so
  `acquire_return_is_nominal_handle` (`resources.py:46-68`) admits it and the
  taint base (`resource_base`, `resources.py:71-81`) picks it up. `Stream[T]`
  itself is NOT a resource (it is a value description you may pass around and
  store); only the acquired `Subscription[T]` is. This split matters: it lets a
  service hand out a `Stream[T]` freely while the subscription that consumes it
  stays confined.
- **O1.** `close` joins `closing_ops` (`resources.py:143-154`); a hand-call is
  refused everywhere except the acquiring binding's own `undo`
  (`lower.py:7100-7115`). One close, owned by the accumulator.
- **B1.** The `Subscription[T]` handle is confined by the seven clauses
  (`lower.py:7181-7411`): it cannot be stored to outlive its owner, captured in
  a closure, returned onward, put in an escaping carrier, seated in
  spawn/handoff, or placed in an undo/witnessed/compensate position. The owner
  storing its own subscription in its own activation state is the normal
  carve-out.

Async coloring for `every` and `next` reuses item 131 exactly:

- `next` is a suspension source (a req-target async op / async-colored
  callable), so the `every` body is an async context, the same way a timer
  body reaching an async op is admitted, colored, and awaited by the runtime
  (`lower.py:4925-4936`; the `_async_reached_outside_provide` fence prunes
  `timer`/`await` steps at `lower.py:6204-6237`, and gains the `stream` step in
  the same prune list).
- The driver awaits `next` on the body's behalf; the author never writes
  `await` inside the `every` body for the pull itself. An effectful body that
  itself reaches an async op is awaited under the same coloring.
- **Teardown never suspends (131 Rule 3, `lower.py:6382-6393`).** `close` and
  any `undo`/`compensate` on the subscription must be synchronous. This is the
  one constraint the stream close path must respect, and it is exactly the
  roadmap's "explicit cancellation path": `close` is a synchronous host-local
  unsubscribe, never an awaited call. A `subscribe` extern whose declared
  `close` inverse is async is refused at the extern (mirroring
  async-extern.md's `acquire async` refusal).

The subscribe itself is an async acquisition, the item-131 shape
`let sub = effect await subscribe(...) undo sub.close()`, whose
boundary-atomic registration (the await lands, THEN the inverse yields, one
generator step, `backends/python/emit.py:1260-1272`) already guarantees there
is no observable state where the subscription was acquired but its close is
unregistered.

## The lifecycle and the CORE GUARANTEE

The subscription state machine, kept to the two states the bracket gives for
free in v1 (the reserved `Stream[T, State]` adds `Paused` later):

```
        subscribe (acquire)                close() / terminal event
Created ────────────────────▶ Active ─────────────────────────────▶ Closed
                                 │                                     ▲
                                 └─────── owner-unload teardown ───────┘
                                          (derived cancel, then close)
```

`Created` is the pre-acquire `Stream[T]` description; `Active` is the live
subscription (the bracket is on the stack, the `every` driver is running);
`Closed` is post-close, reached by an explicit terminal (`ended` / `failed`),
by `take(n)` exhaustion, or, decisively, by the owner's teardown. There is no
Active state without a registered close, by construction (below). This machine
is deliberately NOT the component lifecycle (`cordis.fiber.FiberState`:
PENDING/LOADING/ACTIVE/UNLOADING/DISPOSED/FAILED, `run.py:83`,
`why_runtime.py:46-54`); that coarser per-component machine governs the OWNER.
The coupling point is the one that matters: the owner's UNLOADING forces the
subscription to Closed before the owner reaches DISPOSED.

### CORE GUARANTEE

> Every admitted stream subscription has an explicit cancellation path, and
> unloading the subscription's owner closes the stream FIRST, before the owner
> disappears.

**Proof sketch, in two parts.**

Part 1, existence of the cancellation path (a static, admission-time
property):

1. `subscribe` is `acquire`-classified, so R0 forces its return to be a
   nominal handle (`resources.py:46-68`) and admission forces a declared
   `undo` (`lower.py:2561-2566`). There is no way to spell a subscribe that
   binds a `Subscription[T]` without a paired `close` in an `undo` clause; the
   grammar itself requires it (`parser.py:16`, "undo is not optional").
2. That `undo` lowers to a `bracket` entry on the acquiring activation's LIFO
   accumulator (`backends/python/runtime.py` `Frame`, teardown-contract.md).
3. A `bracket` inverse replays on clean unload AND on abort alike
   (teardown-contract.md's entry-kind table: "replays on clean unload: yes").
   So `close` runs on every teardown path, commit or abort.
4. By O1 no other close exists in the program (`lower.py:7100-7115`), so the
   accumulator's `close` is the WHOLE close set, not merely one member of it.

Therefore no admitted program has a subscription without exactly one
cancellation path, and that path is guaranteed to execute. This is the
same by-construction argument 308 makes for `owned` handles, applied to a new
handle type; nothing here is novel machinery, which is the point.

Part 2, owner-unload closes the stream FIRST (a runtime ordering property):

1. The subscription is acquired DURING the owner's activation, so its
   `bracket` sits ABOVE the owner's earlier activation brackets on the
   per-activation stack, and G7 replays the stack LIFO
   (`diagnostics.py:31`; `runtime.py:1195-1211`, `drain` yielded last sits at
   the top of the disposer stack). LIFO therefore runs `close` before the
   owner's earlier inverses, and all of it runs as part of the owner's own
   teardown, which completes before the owner's activation record is released.
   This is the exact ordering the spawn child already has (a spawned instance
   is a child fiber torn down by the spawner's own accumulated
   `s.dispose()` before the spawner's earlier inverses, `runtime.py:435-455`);
   a subscription is that shape with `close` in place of `dispose`.
2. The `every` reactive body is cancelled first, before the bracket close.
   The runtime stops driving the body (a derived cancel, the R5 withdrawal the
   timer already uses); an in-flight `next` await lands and the boundary
   closes without running a further body iteration (item 131 inertia,
   `backends/python/emit.py:986-989`). Delivery ceases, THEN `close` releases
   the handle.
3. G5 is not violated by closing in teardown, and this is the careful part.
   `close` is a `bracket` inverse, a host-local unsubscribe, NOT an emission.
   An emission is a one-way boundary crossing (`KIND_EMISSION`,
   `replay.py:75`; G5 refuses registering effects in teardown,
   `diagnostics.py:29`). Releasing a subscription is a release, the acquire
   half's inverse, explicitly allowed in teardown (teardown-contract.md entry
   table, `bracket`: "may emit in teardown: no", and yet it runs, because a
   release is not an emission). DELIVERING a value WOULD be an emission and is
   forbidden in teardown, which is precisely why step 2 stops delivery before
   step 3 closes. The order (cancel delivery, then release) is what keeps G5
   intact.
4. R4 holds: after teardown, `close` ran and the disposer stack is back to
   baseline (`run.py:1139-1156`). `close` is idempotent-on-replay (a
   re-issued unsubscribe on an already-dead provider is a no-op), so even the
   provider-already-gone case leaves no residue.

The two parts together are the guarantee: admission forces the path to exist,
G7 forces it to run first, G5 keeps the run legal, R4 confirms it leaves
nothing. Each clause cites shipped machinery, which is why a novel,
high-blast-radius feature can claim a rigorous teardown story: the teardown is
not novel, only the source feeding it is.

## Every hard-part decision, justified

### Single consumer, not multicast (START single)

v1 admits exactly one consumer per subscription. A subscription has one owner
(the acquiring activation), one bracket, one accumulator entry, so 308 `owned`
mode applies unmodified and the CORE GUARANTEE is by construction. Multicast is
not merely more code; it is a DIFFERENT teardown model. N consumers sharing one
source is 308 `shared` mode, whose teardown is "run the inverse at the last
release" and which 308 explicitly DEFERS to the item-294 lease binding, with a
liveness-gated crash backstop still owed (308 doc, section S1). Multicast would
also force implicit fan-out buffering (one slow consumer stalls the others) and
a shared-buffer backpressure policy. So single-consumer is not a simplification
we chose for convenience; it is the only shape whose teardown is already
solved. Multicast defers WITH `shared` and inherits its lease when that lands.

### Cancellation ownership: the acquiring activation

The subscription owner is the activation that ran
`effect await subscribe(...) undo sub.close()`, i.e. the consumer component.
This is 308 `owned` (`lower.py:6978-6985`). A subscription handle passed into a
`fn` or a service method is a 308 `borrow`: the callee may pull `next` (use)
but may not `close` (O1) and may not store or otherwise escape it (B1). So
"who may cancel" has one answer, checked, with no ambiguity: the owner, via
teardown; nobody by hand.

### Provider death: a terminal event, not a wedged PENDING

When the provider goes away, the consumer receives a terminal
`closed(failed)` event; it does NOT sit PENDING forever. Justification: PENDING
is revl's state for a dependency that has not YET come up (an unmet requirement,
`run.py:730-749`), a condition that can still resolve. A provider that DIED is
a different fact: it will never produce again. Modeling death as PENDING would
wedge the `every` driver on a `next` that never resolves, and the consumer
would make no progress and run no terminal handling until owner-unload. A
terminal event instead ends the `every` loop deterministically, lets the
`closed` handler run (while still Active), and lets the consumer proceed to its
own orderly teardown. The runtime CAN see provider death: provider withdrawal
drives the R2/R3 reactive cascade the runtime already runs
(`run.py:365-366`), and a withdrawn provider fiber synthesizes `closed(failed)`
into the consumer's buffer. (The residual case, a host source that is dead but
whose fiber never withdraws, is the CRITICAL finding in the adversarial review;
see there for the liveness-gated mitigation.)

### Backpressure: a bounded buffer with an explicit, named policy

v1 refuses unbounded buffering. `subscribe` takes a buffer bound and an
`on_overflow` policy, one of:

- `suspend` (the DEFAULT): the async provider's next emit awaits buffer space.
  Lossless, and it is exactly the item-131 non-blocking await (the provider
  parks, other fibers run). This is the honest default because it drops
  nothing.
- `drop_oldest` / `drop_newest`: bounded loss, for a source that must not be
  slowed. A drop is NOT silent: it emits a G8 audit crossing recording the
  discard, so the audit surface tells the truth about what was dropped.
- `fail`: overflow delivers a terminal `closed(failed)` and closes the stream.

A push-only host source that cannot be suspended (a webhook firehose) must
declare a `drop_*` or `fail` policy explicitly; `suspend` on such a source is a
refusal with a hint, because a `suspend` the provider cannot honor would
silently become an unbounded buffer. "The restriction is the feature," as 308
puts it: an undeclared, uncontrolled source is refused.

### Replay: none in v1, reserved behind a provider declaration

A v1 subscription delivers only values emitted after it enters Active. No
replay of past values to a late or second consumer. Replay implies a retained
durable buffer whose teardown and crash-recovery is its own hard problem (a
WAL of stream position), and with single-consumer, single-subscription v1 it
buys nothing. The surface is reserved: a provider may later declare
`replay(n)`, meaning a new subscription begins by delivering up to the last n
retained values. Deferred with the durable-cursor work below.

### The six-tier protocol: the shared minimal shape, and who serves it

The minimal shape every serving tier implements is a pull-with-terminal
protocol, agnostic to the tier's concurrency model:

1. `next() -> Event[T]`, `Event[T] = value(T) | closed(Reason)`;
2. at most one consumer;
3. a terminal delivered exactly once, after which no `value`;
4. `close()` idempotent and always available, synchronous (131 Rule 3);
5. per-source FIFO ordering; cross-source order (under `merge`) is
   unspecified, per A1 (ordering within a task is promised, interleaving
   across tasks is not, async-extern.md section 2 family 2).

Per tier, with the honest verdict:

| tier | mechanism | v1 verdict |
| --- | --- | --- |
| **py** | async generator / async iterator over a bounded `asyncio` buffer, `Clock`-driven for tests (`runtime.py:3004`) | REFERENCE, ships in Slice 1 |
| **ts** | `async function*` + fiber accumulator (the 131 colored-tier shape) | ships (Slice 2) |
| **go** | a bounded channel carrying `Event[T]` + `close(ch)` as terminal; consumer goroutine ranges it | ships (Slice 2); blocking, observably equivalent under A1 |
| **rust** | a bounded `sync_channel` with `Drop` as close; a driver thread | OPEN: rust has no async runtime today (timer.rs is blocking-scheduled). Either a blocking driver-thread delivery ships in Slice 3, or rust defers with java. OPEN anchor: the rust reactive-driver seam, confirm at Slice 3 |
| **java** | a `BlockingQueue` + terminal sentinel + `AutoCloseable` | DEFERS. java does not lower timers today (`test.py:455-456` skips them as a follow-on); stream delivery is gated behind java timer support, via the SAME skip mechanism |
| **wasm** | none | REFUSES, permanently (below) |

The honest verdict the brief asked for: py/ts/go are the v1 serving tiers;
rust is contingent on its driver seam; java defers exactly as it defers
timers; wasm refuses forever. The roadmap's "py/ts/rust/go/java support, wasm
refuses" is the ASPIRATION; the truthful v1 is "py/ts/go serve, rust and java
follow their existing async/timer readiness, wasm refuses," and pretending
otherwise would put java in a set it cannot join until its timer story lands.

### The wasm REFUSAL, mirroring the timer refusal exactly

wasm has no open-ended async host seam; its only awaitable is `await
Job.run(name)` (`backends/wasm/emit.py:1309-1313`). An open-ended async value
producer cannot be expressed, exactly as a timer cannot. Mirror the timer
double-refusal:

1. A pre-emit detector in `src/revl/test.py`, `_has_streams(ir)` beside
   `_has_timers` (`test.py:410-426`), and `_stream_follow_on(tier)` beside
   `_timer_follow_on` (`test.py:429-436`), wired at the top of `run_wasm`
   (`test.py:700-701`) so the tier reports a clean `("skip", reason)`:
   > streams (`Stream[T]`, `every ... in ...`, item 130) are not lowerable on
   > the wasm tier: the substrate has no async host seam beyond `Job.run`, so
   > an open-ended value producer cannot be expressed; streams lower on py,
   > ts, and go (docs/design/130-stream-reactive.md).
2. A NAMED `EmitError` in the wasm emitter for the `stream` step and the
   `Stream`/`Subscription` types, in the style of the awaited-effect refusal
   (`backends/wasm/emit.py:1256-1260`), so a `stream` step that reaches the
   emitter is a named tier-limit, never the generic "unknown step"
   fall-through at `backends/wasm/emit.py:1336`.

This is the SAME two-layer shape that keeps a wasm timer a clean skip rather
than an opaque emitter dump (`test.py:414-417`). An exit test asserts the
refusal text so it cannot regress to a silent accept.

### Effectful callbacks: allowed, capability and lifecycle checked

The `every` body, the `closed` handler, and the `map`/`filter`/`take`
callbacks may be effectful. Rules, all reused:

- An effectful callback or body reaching an `emission` extern is
  emission-colored and must carry the capability under the G4 emit-marker and
  the emission fixpoint (`_emitting_fns`, `emission_analysis.py:50-129`); an
  unclassified emitter is refused as it is anywhere else.
- A callback or body may NOT capture the subscription handle in a closure (B1
  clause 2, `lower.py:7333`) nor hand-call `close` (O1). This keeps the handle
  from escaping through the callback surface.
- Callbacks and bodies run ONLY during Active delivery, never in teardown, so
  G5 holds: no effectful callback can emit during teardown, because teardown
  cancels delivery before it closes (Part 2 step 2 of the proof).
- An async-reaching callback is admitted only where the driver awaits it, the
  same coloring the `every` body itself gets.

### Cross-realm: refused in v1, a bridged service is the answer

A `Subscription[T]` is resource-typed, so it cannot cross a process or realm
seam by copy (`resource_crossing_refusal`, `src/revl/placement.py`; the
promoted resource-type check the checker now sees). A cross-realm subscription
is refused with a mode-named diagnostic, the same stance 308 takes for a
remote borrow: the principled answer is not a proxied handle but a SERVICE
placed with the source, forwarding a `Stream[T]` over the bridge. The
bridge's own streaming protocol (how a `Stream[T]` service surface forwards
across a process boundary) is its own item, deferred; v1 refuses and names the
restructure.

### Crash recovery: non-reconstructible in v1

A live subscription is a host handle (a socket to Kafka, a webhook cursor)
whose in-flight position is ephemeral host-side state. Its inverse is a
closure over in-process memory, which `inverse_descriptor` correctly reports
as non-reconstructible (`backends/python/replay.py:1258-1261`, "closure over
in-process memory"). So after a crash, `revl recover` records the dropped
subscription as `unreconstructible` residue (`recovery.py:_record`, the
`unreconstructible` kind) and NEVER claims it resumed. This is the honest
verdict and it costs nothing: it is the existing recovery honesty seam applied
to a new handle. Resumable streams (a provider-declared durable cursor plus a
WAL of delivered positions) defer WITH replay, since they are the same durable
buffer problem.

### Events (external proposal #10): a Stream specialization, not a distinct surface

`event OrderCreated { ... }` and `on OrderCreated as e { ... }` are SUGAR over
`Stream[T]`, not a second mechanism. The mapping:

- `event OrderCreated { field: T, ... }` declares (a) a record type
  `OrderCreated`, and (b) a reactive coeffect KEY that provides
  `Stream[OrderCreated]`. The provide/inject graph resolves the emitting
  provider by name exactly as it resolves any coeffect key (`provider_of`,
  `lower.py:9146`; G2 one-provider-per-key, G3 no self-cycle,
  `lower.py:9160-9217`).
- `on OrderCreated as e { body }` desugars to acquiring the subscription for
  that key and running `every e in sub { body }`.

Justification for specialization over a distinct surface: a distinct event
surface would re-derive the lifecycle, the teardown, the backpressure, the
six-tier delivery, and the wasm refusal a SECOND time, and any drift between
the two would be a divergence bug. As a specialization, events inherit the
CORE GUARANTEE, the resource discipline, and every refusal for free. The one
thing events add is name-based provider wiring, and that is the provide/inject
graph doing its ordinary job (a stream is a reactive coeffect, the roadmap's
own framing). The event contract maps onto Stream with no gap: the event
record is `T`, the event source is the provided `Stream[T]`, the handler is an
`every`, and the subscription is the owned acquire resource with the guaranteed
close. Events defer to a slice AFTER the core Stream lands, but the mapping is
fixed here so the two cannot diverge.

## G-invariant interaction, stated once

- **G5 (`diagnostics.py:29`, teardown cannot register effects).** The stream
  `close` is a `bracket` inverse (a release), not an emission (a one-way
  crossing). Closing in teardown is legal; DELIVERING in teardown is not, and
  the teardown order (cancel the `every` driver, THEN close) guarantees no
  delivery happens during teardown. This is the one G5 subtlety and the proof's
  Part 2 step 3 is where it is discharged.
- **G7 (`diagnostics.py:31`, derived LIFO teardown).** The subscription
  bracket, acquired during the owner's activation, sits above the owner's
  earlier brackets and closes first under LIFO. Owner-unload-closes-first is a
  corollary of G7 plus the acquire-during-activation ordering, not a new rule.
- **R4 (`run.py:1139-1156`, the no-residue proof) and A8 (`diagnostics.py:46`,
  L-Raise revert-and-contain).** After teardown the disposer stack is back to
  baseline; a body that fails mid-delivery reverts its own effects LIFO and
  the subscription reaches a terminal `Closed(failed)`, then the bracket close
  runs. No residue either way.
- **G4 (capability containment) and the emission model.** An effectful
  callback or `every` body carries its emission capability under the existing
  emit-marker and fixpoint; the stream adds no new capability dimension, only
  new positions the existing checks run over.

## Adversarial self-review

At least five attacks; the CRITICAL is named. Each is an attempt to break the
CORE GUARANTEE or to slip a value/effect where it must not go.

**A1. Owner unloads while a value is mid-delivery.** An in-flight `next` await
lands during teardown; by inertia (131, `emit.py:986-989`) the boundary closes
before the next body iteration, so a value that was pulled from the buffer but
not yet processed is NOT run through the body. Is it lost silently? Decision:
v1 delivery is at-most-once, and a value landed-but-unprocessed at teardown is
reported in the residue `worldRemaining` (teardown-contract.md envelope), so it
is dropped HONESTLY, not silently. A future at-least-once mode would make `next`
two-phase (peek then ack) so an unacked value stays buffered as the provider's
problem. STATUS: mitigated (at-most-once, documented; two-phase reserved).

**A2. Provider dies leaving the consumer wedged. THE CRITICAL.** The chosen
provider-death behavior (a terminal event) depends on the runtime NOTICING the
death. It notices a provider FIBER withdrawal via the R2/R3 cascade
(`run.py:365-366`) and synthesizes `closed(failed)`. But a host source that is
dead-but-not-withdrawn (a silently half-open TCP connection, a webhook whose
sender vanished, no keepalive) produces no withdrawal and no error, so `next`
awaits forever and the `every` driver makes no progress. Note carefully what
this does and does not break: the CORE GUARANTEE still HOLDS (owner-unload
still force-closes the bracket, teardown is still safe, no residue); what fails
is LIVENESS (the consumer silently stops advancing, processing nothing, until
its owner unloads). A safety-preserving liveness stall is still a serious bug
in a saga projector, and it is invisible to the type system because "the host
went quiet" is not a compile-time fact. MITIGATION, two layers: (a)
liveness-gated terminal synthesis, mirroring 308's S1 liveness-gated reclaim,
so any provider-fiber withdrawal the runtime CAN see arms a `closed(failed)`;
(b) an optional subscription `idle_timeout` that synthesizes `closed(stalled)`
after a quiet interval, driven by the deterministic `Clock` (`runtime.py:3004`)
so it is testable. The residual, a host source with neither a withdrawal signal
nor an idle timeout configured, is a documented liveness limitation and is
OPEN: the type system cannot force a host to prove liveness, so the honest
position is that a stream over such a source SHOULD declare an `idle_timeout`,
and `revl audit` should list subscriptions that declare neither a
withdrawal-observable provider nor a timeout. This is the sharpest finding in
the design, and it is a liveness OPEN, not a safety hole.

**A3. An effectful `closed`/terminal handler emits during teardown (G5).** If
the terminal arrives as the owner is unloading, running the `closed` handler
would emit in teardown, violating G5. Mitigation: the `closed` handler is
Active-only. On owner-unload, delivery stops (the driver is cancelled) before
any teardown, so a terminal that arrives after teardown began does NOT run its
handler; the bracket close runs instead. Author cleanup that MUST run on unload
belongs in the subscription's `undo`/`compensate`, not in the `closed` branch,
and the doc says so at the surface. STATUS: mitigated (delivery-stops-before-
teardown; `closed` is Active-only best-effort, documented).

**A4. A subscription leaked past its owner's lifetime.** Storing the handle in
another component's activation state, capturing it in an escaping closure, or
returning it onward. Refused by 308 B1 clauses 1-7 (`lower.py:7181-7411`),
unchanged; the owner storing its own handle in its own state is the carve-out.
STATUS: closed by 308, no new work.

**A5. Backpressure buffer overflow.** An unbounded buffer OOMs; a silent drop
loses data invisibly. Refused: unbounded buffering is refused at `subscribe`;
a bounded buffer requires an explicit `on_overflow`; `suspend` is lossless;
`drop_*` emits a G8 audit crossing per discard, so no drop is silent; `fail`
terminates. STATUS: mitigated (explicit policy, audited drops).

**A6. An `every` body fails mid-delivery, leaving the subscription Active.**
Does one bad value wedge the stream open or silently skip? Decision: a body
failure is fail-stop for the consumer. A8 L-Raise reverts the body's own
effects and contains (`diagnostics.py:46`); the subscription transitions to
`Closed(failed)`, delivery stops, and the bracket close runs at owner teardown.
It does NOT silently skip and continue, which would hide the fault. STATUS:
mitigated (fail-stop, documented).

**A7. Cross-tier divergence in delivery order.** `merge` interleaves two
sources; a naive conformance test might assert a fixed cross-tier interleave
and then flap. Decision: cross-source order is UNSPECIFIED per A1; only
per-source FIFO is promised and tested. `merge` order is a PINNED DIVERGENCE in
the conformance matrix (the existing `Divergence` mechanism), never a bug.
STATUS: documented as a divergence, tests assert per-source FIFO only.

**A8. wasm silently accepts instead of refusing.** If a `stream` IR step
reaches the wasm emitter it would hit the generic "unknown step" fall-through
(`emit.py:1336`), a `fail`, not a clean refusal, and a future emitter edit
could conceivably grow an accidental partial branch. Mitigation: the two-layer
timer-style refusal (a `_has_streams` skip in `test.py` plus a NAMED wasm
`EmitError`), with an exit test asserting the refusal TEXT so a silent accept
cannot land. STATUS: mitigated (mirrors the timer double-refusal; text pinned
by a test).

## Sliced implementation plan

Slices are additive and land in order; each leaves the suite green and every
per-backend golden byte-identical for programs that do not use streams (the
IR carries an opt-in `stream` step and opt-in `Stream`/`Subscription` types,
exactly the additive discipline the async and timer families held to, so an
unused feature changes no emitted byte). Reminder from the standing wave gap:
`pytest tests/` does not run the per-backend golden suites; run each backend's
own suite after its `emit.py` changes.

**Slice 1: the smallest landable core.** `Stream[T]` / `Subscription[T]` /
`Event[T]` / `Reason` types; the `subscribe` (acquire, R0 nominal handle) /
`close` (declared inverse) externs and the bracket; the `every x in sub { }`
reactive body step (new IR `{"step": "stream", ...}` beside the timer step at
`lower.py:8566`) with the optional `closed` handler; `next` as the driver
primitive; async coloring of the body (reuse `_async_reached_outside_provide`
prune list, `lower.py:6204-6237`); the bounded buffer with `on_overflow:
suspend` default; py delivery and runtime (async generator over an asyncio
buffer, `Clock`-driven for tests); and the REFUSALS on all five other tiers
(ts/go/rust/java via the `test.py` skip, wasm via skip PLUS the named
`EmitError`). Files: `src/revl/parser.py`, `src/revl/lower.py`,
`src/revl/resources.py` (Stream/Subscription in the taint model),
`backends/python/emit.py`, `backends/python/runtime.py`, `src/revl/test.py`,
`backends/wasm/emit.py`. Exit tests below (the CORE GUARANTEE tests, py
delivery, the refusals). DEFERRED out of Slice 1: `map`/`filter`/`take`/
`merge`, ts/go/rust/java delivery, events, `Stream[T, State]`/pause, replay,
resumable recovery.

**Slice 2: ts and go delivery.** Lift the ts and go skips to real delivery (ts
the colored-tier `async function*` shape, go the bounded-channel shape).
Per-backend goldens. Files: `backends/typescript/emit.py`,
`backends/go/emit.py`, `src/revl/test.py` (remove those two skips).

**Slice 3: combinators, and rust delivery or its deferral.** `map`/`filter`/
`take` (pure stream adapters, effectful-callback capability checks) and `merge`
(with the pinned cross-source-order divergence). Rust delivery via a blocking
driver thread IF the reactive-driver seam is confirmed (OPEN anchor), else
rust stays a documented skip with java. Files: `src/revl/lower.py`,
`backends/*/emit.py`, `backends/rust/emit.py`.

**Slice 4: events.** `event T { ... }` and `on T as e { ... }` desugaring to a
provided `Stream[T]` coeffect key plus an `every`. Coeffect wiring through the
provide/inject graph (`lower.py:9146`). Files: `src/revl/parser.py`,
`src/revl/lower.py`.

**Slice 5 (deferred, sequenced with item 294 and the durable-cursor work):**
`Stream[T, State]` typestate (Paused/resume, the new checker construct),
backpressure pause/resume, `replay(n)`, resumable crash recovery (durable
cursor + WAL), and java delivery gated behind java timer support. Multicast
(`shared`-mode subscriptions) is out of even this slice and defers with 308
`shared`.

## Exit tests

Slice 1 (the guarantee is the headline, so its tests are first):

1. **Cancellation path exists (admission).** A component that subscribes
   compiles only with a `close` in the `undo` clause; the same program without
   the `undo` fails admission with the "acquire extern must declare `undo`
   (G4)" refusal (`lower.py:2561-2566`). A hand-call of `close` anywhere else
   is refused by O1 (`lower.py:7100-7115`); rejection fixture under
   `examples/rejections/`.
2. **Owner-unload closes first (runtime, py).** A component subscribes, the
   `every` body processes a few values, the owner unloads; the trace shows the
   `every` driver cancelled, THEN `close`, and `close` precedes the owner's
   earlier activation inverses (G7 LIFO). `assert` R4 no-residue
   (`run.py:1139-1156`).
3. **G5 held in teardown.** A trace test proving no `value` is delivered after
   teardown begins (delivery stops before close), and that `close` (a release)
   runs while no emission does.
4. **Provider death delivers a terminal.** The provider fiber withdraws
   mid-stream; the consumer receives `closed(failed)`, the `every` loop ends,
   the `closed` handler runs (Active), teardown then closes cleanly. The
   liveness variant: a configured `idle_timeout` synthesizes `closed(stalled)`
   under the deterministic `Clock`.
5. **Backpressure.** `suspend` parks the provider when the buffer is full and
   resumes on drain (no loss); `drop_oldest` drops and emits the audit
   crossing; unbounded/undeclared is refused.
6. **Refusals.** wasm reports the named skip AND the named `EmitError` (text
   pinned); ts/go/rust/java report the Slice-1 skip; a cross-realm subscription
   is refused with the resource-seam message.
7. **Byte-identity.** Every program not using streams emits byte-identically on
   all six tiers (per-backend goldens).

Later slices: ts/go delivery twins of tests 2-5; combinator semantics and the
`merge` per-source-FIFO (divergence-pinned) test; the events desugaring
round-trip; and, at Slice 5, the typestate transition refusals and the
durable-cursor recovery round-trip.

## Open questions (left deliberately)

1. **The rust reactive-driver seam (Slice 3).** rust has no async runtime and
   its timer is blocking-scheduled; whether stream delivery ships on rust with
   a blocking driver thread or defers with java is decided when the seam is
   examined at Slice 3. OPEN anchor.
2. **The provider-death liveness residual (A2, the CRITICAL).** A host source
   with neither a withdrawal-observable provider fiber nor a configured
   `idle_timeout` can stall the consumer without violating safety. v1's answer
   is to recommend `idle_timeout` and to have `revl audit` list the exposed
   subscriptions; whether the language should REFUSE such a subscription
   outright (forcing a timeout) is a policy question left open for the first
   serving-tier landing to inform.
3. **At-most-once vs a two-phase `next` (A1).** v1 is at-most-once with honest
   residue reporting of a landed-but-unprocessed value; a two-phase peek/ack
   `next` for at-least-once is reserved, decided by whether a real saga needs
   it.
4. **`Stream[T, State]` typestate mechanism.** No typestate exists to reuse
   (confirmed: no state-parameter machinery in `typecheck.py`/`lower.py`), so
   the reserved second parameter is genuinely new checker work whose shape
   (phantom parameter checked by the existing flow walk, vs a fuller
   construct) is decided at Slice 5.
5. **Self-host port (item 391 discipline).** v1 adds a parser production, a
   checker/coloring pass, and emitter work; whether the self-host stages need
   an O1/B1/stream-step port is decided by which oracle owns refusal parity for
   the stream rejection fixtures, at the slice that first asserts it, not
   discovered by a red oracle.

# Design: `Stream[T]` reactive types (item 130)

Status: SLICES 1-5 LANDED (the full v1 surface). This document turns the
roadmap's v1 spec (docs/v2.0-roadmap.md:3518) into an admission-and-lowering
design with a concrete Slice 1, decides every hard-part the roadmap lists, folds
in the typed-EVENTS proposal, and closes one CRITICAL that an adversarial pass
found (§9). The v1 surface has since landed across slices 1-5 on py: the type
and `Stream[T, State]`, the subscribe/next/close bracket with the
cancellation-first `next` and rule 3.6, `map`/`filter`/`take` with the declared
backpressure policies and the drain clock, the `merge` fan-in with the go/rust
blocking lowerings, `every … in` async iteration, and typed events; §4.5
(`replay`) and §6b (events) record inline what each slice shipped. Still open:
the iteration/handler forms on go/rust/java/typescript, the
`replay(n)`/`replay(from: <durable>)` declaration, and the reconstructible
crash-recovery case.

Base: `origin/main` @ `e513772`. Every `file:line` anchor below was read at
that sha. Every "admitted"/"refused" claim about *today's* checker is a claim
about machinery this item builds on (streams do not exist yet: a corpus grep
for `Stream[`, `subscribe`, `.next(` finds only the WIT importer's tokenizer
and the dash causal pane, neither related), and is labeled as such.

The roadmap's own instruction is the frame: **do not start with a general
reactive calculus.** v1 is `Stream[T]` plus a small, single-consumer API whose
whole novelty is that a subscription is an `acquire`/`undo` resource, so the
teardown machinery that already ships (docs/design/teardown-contract.md) closes
it. The design's job is to make that reuse exact and to name where it does not
reach.

## 0. The one guarantee this item exists to deliver

> **Core guarantee.** Every admitted stream subscription has an explicit
> cancellation path, and unloading its owner CLOSES the stream before the owner
> disappears.

Stated in the vocabulary that already carries teardown:

- A subscription is a **`bracket` entry** on the owner activation's single LIFO
  disposer stack (teardown-contract.md, "The three entry kinds, one stack"). Its
  acquisition is `subscribe`; its inverse is `close`.
- On clean unload the bracket inverse RUNS (teardown-contract.md commit path:
  "every `bracket` inverse RUNS ... releasing an acquired handle is always
  right"). On abort it replays in Phase 1, LIFO (R1, DESIGN.md §4 G7).
- So "unloading closes the stream" is not a new mechanism; it is G7 applied to a
  bracket whose handle happens to be a live listener. This doc's work is proving
  the bracket inverse is always *reachable* (§9), and refusing every shape where
  it would not be.

Everything else in this document is subordinate to that sentence.

## 1. Surface (v1)

A stream type, a state-indexed variant, three core operations, four combinators,
and one iteration form. Slices (§7) stage them; this is the whole v1 target so
the type and IR decisions below are made against it, not against Slice 1 alone.

```revl sketch
// A provider declares it emits a stream; the subscription is a bracket.
component OrderFeed provides orders: Stream[Order] {
  // provider-side resource, acquired and inverted like any effect
  let src = effect kafka.open("orders") undo src.close()
  provide orders { ... }              // emits Order values over time
}

// The consumer owns the subscription. `subscribe` is the acquisition;
// `close` is its declared inverse. Created -> Active on the bind.
component Fulfiller requires orders: Stream[Order] {
  let sub = subscribe orders undo sub.close()          // Stream[Order, Active]
  every o in sub {                    // async iteration, one consumer
    call ship.dispatch(o.id)
  }                                   // Active -> Closed at loop end or unload
}
```

Core (Slice 1):

- `subscribe <stream> undo <close>` — acquire a single-consumer subscription;
  an `effect`-position acquisition that registers a `bracket`.
- `<sub>.next()` — await the next item or a terminal event; a suspension point,
  async-colored (item 90 / A1).
- `<sub>.close()` — the declared inverse; trips the cancellation token and
  releases the host listener. Synchronous, non-emitting, infallible (G5).

Combinators (Slices 2-3), each a *derived stream* whose bracket chains to its
source's, so teardown stays one LIFO stack:

- `map(f)` / `filter(p)` / `take(n)` — pure transforms (`f`, `p` are G6-pure);
  the derived subscription's `close` calls the source's `close`.
- `merge(a, b)` — one consumer, two sources; the multi-source teardown ordering
  is the reason it is its own slice (§7).

Iteration (Slice 4): `every x in <sub> { <body> }`, an async-iteration form
whose body is an effect context (§4.7).

State index (Slice 1): `Stream[T, State]` with `State ∈ {Created, Active,
Paused, Closed}`. `subscribe` : `Stream[T]` → `Stream[T, Active]`; `close` :
`Stream[T, Active|Paused]` → `Stream[T, Closed]`. The checker uses the index for
linearity and use-after-close (§3); `Paused` is reserved for backpressure
(§4.4) and is not producible in Slice 1.

### Grammar delta

Three productions, each reusing an existing keyword or the `effect`-acquisition
shape:

```
componentstmt := ...
  | 'let' IDENT '=' 'subscribe' expr 'undo' expr      -- subscription bracket
  | 'every' IDENT 'in' expr block                     -- async iteration (S4)

type := ...
  | 'Stream' '[' type (',' type)? ']'                 -- T, optional State
```

`subscribe … undo …` is deliberately the `effect … undo …` shape (parser.py
already carries `let IDENT = effect … undo …`): a subscription IS an
acquisition, and spelling it like one is what makes the bracket registration and
the §9 reachability argument fall out of existing lowering rather than a new
path. `next`/`close` are ordinary method calls on the subscription value; they
need no grammar.

**The `every` collision, decided.** `every <n><unit> { emit* }` already parses
as a timer body (parser.py:195). The stream form is `every <IDENT> in <expr>
{ … }`. One token of lookahead after `every` disambiguates with no ambiguity:
a NUMBER-then-unit is the timer; an IDENT-then-`in` is stream iteration. The
alternative (a fresh keyword) was rejected because `on` is claimed by the
typed-events surface (§6) and `for` connotes a bounded loop the reactive form is
not. Recorded as judgment call 1.

## 2. Type formation and the async color

`Stream[T]` is a first-class type usable in `provides`/`requires` positions and
as a service operation's element type. Two rules fix its meaning:

1. **A `Stream[T]` value is not the items.** It is a capability to acquire a
   subscription, exactly as a `Database` value is not a connection. This is why
   `provides orders: Stream[Order]` type-checks with no async color on the
   *provision* — the color attaches to `next`, the suspension, not to holding the
   stream. It also closes the leak G6 names: a `Stream[T]` cannot be stored,
   returned, or aliased out of the component that requires it (§5, no
   first-class context), so a subscription cannot escape its owner.

2. **`next` is a suspension source; the color is name-based and must reach it.**
   `next` joins the union the async family already tracks (item 90 async-colored
   fns; item 92 rule 3 req-target async ops). `_async_callables`
   (src/revl/emission_analysis.py:223) is a *name-based* fixpoint, and item 131's
   review (docs/design/131-async-effect-composition.md §1) documented the exact
   failure mode of a name-based walk: a req-target async operation "passes
   straight through it." So the color for `next` must be seeded the way item 92's
   req-op reach is, not merely by adding a name to the callables set. §9 turns
   this from a note into the CRITICAL and its fix.

## 3. Admission rules

The checker admits a subscription only when its cancellation path is total. Six
rules, each a compile error with a one-fix hint; the diagnostics reuse the
`async-propagation` and `lifecycle` categories the teardown and async families
already emit.

1. **Single consumer, linear.** A `Stream[T]` may be `subscribe`d at most once,
   and the resulting subscription is linear: it must be `close`d on every path
   (the `undo` discharges this) and may not be used after `close` (the
   `Closed` index makes `next` on a closed subscription a type error). Rationale:
   multicast (§4.1) is refused, so a second `subscribe` on the same required
   stream is a compile error naming the bridge (§4.8) as the multi-consumer
   escape.

2. **`subscribe` requires an `undo`.** The bare form `let sub = subscribe
   orders` is refused with the same G4 reason plain `let` is refused in an
   activation body (docs/syntax-2.0.md §4b.3): a held resource with no inverse
   has no place on the accumulator. The fix is the `undo sub.close()` clause.

3. **`next` is a suspension and lives only where a suspension is legal.** `next`
   is admitted in an `every … in` body and in an `async fn` (the same positions
   item 131 admits `await`), and refused in a pure fn, a config default, an
   `undo`/`compensate` slot, and a G6-pure expression, with the item-131 message
   shape ("this position cannot suspend a fiber, A1"). The `undo`/`compensate`
   refusal is load-bearing: teardown never suspends (teardown-contract.md, the
   bound rule), so `close` — the inverse — must not itself `next`.

4. **`close` is infallible and non-emitting (G5).** A `close` expression
   reaching an `emit`, a fallible host op, or another suspension is refused. This
   is the bracket contract (teardown-contract.md: a bracket inverse "claimed G5
   infallibility"). A provider whose real close can fail declares it `witnessed`
   (a transactional entry) instead; that is a later item, refused at the extern
   in v1 the way item 131 refuses `witnessed async`.

5. **The transform arguments are pure.** `map(f)`/`filter(p)`'s `f`/`p` type in
   pure mode (G6); an effectful transform is refused with the hint to move the
   effect into the `every` body, where it is capability-and-lifecycle checked
   (§4.7). This keeps the combinator chain a pure derivation whose only effects
   are the source's bracket and the consumer's body.

6. **Provider death cannot be silent (the §9 rule).** A `Stream[T]` provision is
   admitted only if the provider's teardown delivers a terminal event to its
   consumer (§4.3). A provider that can disappear while a `next` is outstanding,
   without a terminal, is refused. This is the rule the core guarantee rests on;
   its mechanism and the CRITICAL it closes are §9.

## 4. Hard-part decisions

Each is stated as a call plus its rationale, in the roadmap's order.

### 4.1 Single-consumer, not multicast — START SINGLE

v1 is single-consumer. Rationale: multicast multiplies the teardown accounting
that the core guarantee depends on — a fan-out needs refcounting, per-subscriber
buffers, and a "last consumer closes the source" rule, which is three brackets
pretending to be one. A single consumer is one bracket, one `close`, one cancel
path, and the §9 argument is tractable. Multicast is a later item; the refusal
(rule 3.1) names the bridge (§4.8) for the genuine multi-consumer case.

### 4.2 Cancellation ownership — THE SUBSCRIPTION OWNER

The activation that ran `subscribe … undo close` owns cancellation. The bracket
lives on *that* activation's LIFO stack; *that* activation's teardown runs
`close`. Not the provider (a provider that could cancel a consumer's
subscription would be reaching across the isolation §5 forbids), and not a
global registry (which would put the inverse somewhere the per-activation
teardown loop never walks). This is the single most important call: it is what
makes "unloading its owner closes the stream" true by G7 rather than by
convention.

### 4.3 Provider-death behavior — A TERMINAL EVENT, NEVER SILENT PENDING

When a provider goes away, the consumer's next `next` resolves to a **terminal
event** (`Closed` for an orderly provider teardown, `Faulted(err)` for a provider
abort), never silence and never an indefinite park. Rationale and the
distinction that matters:

- The *component* requiring the stream may still go PENDING under R2
  (docs/syntax-2.0.md:646) — that is the coeffect layer reacting to an unmet
  requirement, and it is correct and unchanged. But the *subscription* is a
  different object: a live bracket with an outstanding `next`. R2 PENDING is
  reactivation-on-provider-return; a subscription whose provider died is not
  waiting to reactivate, it is done. Conflating the two is exactly the
  non-terminating-`next` hazard §9 is about.
- So: the component goes PENDING (coeffect layer); the subscription gets a
  terminal event and closes (resource layer). Two layers, two behaviors, stated
  together so an implementer does not pick one for both.

### 4.4 Backpressure — BOUNDED BUFFER + EXPLICIT POLICY, DEFAULT ERROR-CLOSE

Every subscription has a bounded buffer whose capacity and overflow policy are
declared at `subscribe`; there are no unbounded buffers. Policies:

| policy | on overflow | when to use |
|---|---|---|
| `error` (default) | terminal `Faulted(overflow)`; subscription closes | deterministic, no silent loss; the safe default |
| `drop_newest` | discard the incoming item | lossy-tolerant telemetry |
| `drop_oldest` | evict the buffer head | latest-wins gauges |
| `block` | provider suspends until the consumer drains | py/ts only; refused on tiers that cannot suspend the provider |

`Paused` (the reserved state index) is the `block`-policy state: a subscription
whose buffer is full and whose policy is `block` is `Stream[T, Paused]` until
drained. Default `error` was chosen over `drop_*` so that v1 never loses data
silently; a lossy stream is an explicit opt-in, matching the language's
"asynchrony is a declared property" stance (async-extern.md §3).

### 4.5 Replay — ONLY IF THE PROVIDER DECLARES IT

No replay by default: a consumer sees only items emitted after `subscribe`
returns. A provider may declare `replay(n)` (last-n) or `replay(from:
<durable>)`, and only then does a subscription receive the backlog before live
items. Rationale: replay is a durability claim (the provider must hold the
buffer, and after a crash must reconstruct it), so it is opt-in and it is what
gates crash-recovery reconstructibility (§4.9). An undeclared `replay` argument
at `subscribe` is a compile error.

**Shipped (frontend-only).** The `subscribe` head recognizes the two shapes,
`replay(n)` (last-n) and `replay(from: <durable>)` (a durable cursor), as an
order-free qualifier alongside `policy`/`buffer`/`drain`
(`parser._parse_replay_qual`). Both are REFUSED in v1, with two distinct
diagnostics: a malformed argument (`replay()`, `replay(0)`, `replay(x)`) is a
syntax error that names the two accepted shapes, and a well-formed one
(`replay(5)`, `replay(from: cursor)`) is refused as UNDECLARED — replay is a
durability claim only the provider can make, and the provider-side declaration
surface does not exist yet (it lands with the reconstructible crash-recovery it
gates, §4.9). So every `replay(…)` at a `subscribe` is currently the "undeclared
`replay` argument" compile error this section names. The refusal threads no IR
key: the `"replay"` slot §5 reserves stays absent until the provider declaration
and a tier that honors the backlog land together, so nothing ships a silently
inert (vacuous) durability claim in the meantime. The surface grammar is the one
seam this slice adds; flipping the refusal to an admission-plus-thread is the
later slice's whole job.

### 4.6 The minimal six-tier protocol — subscribe / next / close, wasm REFUSES

The protocol every tier that supports streams implements is exactly three
seams: `subscribe` (open a host listener, register the bracket), `next` (await
one item or a terminal), `close` (trip the cancel token, release the listener).

| tier | lowering |
|---|---|
| **py** | reference. `next` awaits an `asyncio.Queue` get raced against a cancel future; the body becomes the `async def` generator the async family already emits (backends/python/emit.py); the bracket `yield`s `lambda: sub.close()` |
| **ts** | same shape on the `async function*` fiber body; `next` awaits a queue-vs-cancel race; the frame's bracket entry carries `close` |
| **go** | erases async: `next` is a two-case `select` on the item channel and the cancel channel; `close` closes the cancel channel. Blocking, occupies the goroutine (A1 family 2) |
| **java** | erases: `next` is a `BlockingQueue.poll` interruptible by the cancel signal; `close` interrupts and drains |
| **rust** | erases: `next` is a `crossbeam` `select!` on the item and cancel receivers; `close` drops the sender |
| **wasm** | **REFUSES.** wasm has no async host seam (backends/wasm/emit.py:1251 already refuses awaited effect steps; lifecycle.py:134 refuses `advance`). A `subscribe`/`next` lowered here refuses with the same honest `EmitError`: "a stream subscription suspends a fiber; this tier awaits only `Job.run(name)`; streams live on the hosted and blocking backends (py/ts/go/java/rust)." |

The go/java/rust "erases to blocking" reading is the async family's family-2
argument (async-extern.md §2): those tiers' methods are blocking, ordering
within a task is promised and interleaving is not, so a stream consumer that
occupies its goroutine/thread is observably equivalent to the event-loop tiers'
fiber. The cancel-channel-in-the-`select` shape on those three tiers is what
makes `close` reachable off the teardown thread — the mechanism §9's fix
requires — and it is why those tiers are Slice 3 (§7), not Slice 1: getting the
select right is per-tier work the py reference should pin first.

### 4.7 Effectful callbacks — ALLOWED, CAPABILITY + LIFECYCLE CHECKED

The `every … in` body is a setup-mode effect context (the same mode a provide
method body types in, DESIGN.md §5): it may `call` service ops, `emit`, and
`effect … undo …`, all capability-checked against the consumer's row (G1) and
enumerable on its boundary (G8). The transforms (`map`/`filter`) stay pure
(rule 3.5); effects live only in the iteration body.

The lifecycle call that matters: **effects the body registers join the OWNER's
accumulator, not a per-item stack.** A `effect … undo …` inside `every o in sub`
pushes onto the same LIFO stack the subscription's bracket is on, so it tears
down with the owner, newest-first, and a per-iteration acquisition is refused
unless it also carries its own per-iteration `undo` that discharges before the
next `next` (the loop-body analogue of the activation rule). A **handler failure
aborts the iteration** (A8): the exception propagates out of the loop, the
activation fails, the accumulated prefix reverts LIFO — and because the
subscription bracket is on that prefix, the failure CLOSES the subscription.
That is precisely the typed-events obligation "a failed handler does not leave a
subscription active" (§6), delivered by A8 with no events-specific machinery.

### 4.8 Cross-realm — ONLY VIA AN EXPLICIT BRIDGE

A `Stream[T]` may not cross a realm boundary (docs/design-v2-realms.md) by being
required across it. A cross-realm stream is a `bridge` component: a consumer in
realm A and a provider in realm B, each with its own bracket in its own realm's
teardown, connected by a declared transport. Rationale: realms are the isolation
boundary; an implicit cross-realm subscription would be a single bracket whose
inverse must run in two teardown loops, which no tier can honor. The bridge makes
it two brackets, each local, each LIFO-correct. The bridge is also the sanctioned
multi-consumer escape (§4.1): fan-out is composed from bridges, not built into
`Stream[T]`.

### 4.9 Crash recovery — NON-RECONSTRUCTIBLE BY DEFAULT

A subscription's inverse is a live host listener (a socket, a Kafka consumer
group), which is closure-only and does not serialize. So by default a
subscription is **`unreconstructible`** — the exact residue kind the teardown
contract already defines (teardown-contract.md merged residue schema:
"`unreconstructible` ... a WAL entry whose descriptor cannot be re-issued
... is residue, never reported as run"). After a crash, `revl recover` reports
the subscription as unreconstructible residue and never claims it closed; an
operator closes the upstream by hand.

A provider that declared `replay(from: <durable>)` (§4.5) opts INTO
reconstructibility: the durable cursor is a WAL-serializable descriptor, so
recover can re-issue `subscribe` from the cursor and resume. This is the only
reconstructible case, and it is gated on the same declaration that gates replay,
so the two durability claims cannot diverge. Default stays non-reconstructible;
the honest report is the deliverable, not automatic resurrection.

## 5. IR and lowering

IR keys, all additive (IR version stays 3 by the async family's standing
argument: additive keys, blocking tiers ignore them, an old colored-tier emitter
fails loud at boot rather than silently wrong):

- the subscription bracket step gains `"subscribe": true` and a
  `"policy"`/`"buffer"`/`"replay"` triple from the backpressure and replay
  declarations;
- the `every … in` step is a new step kind `"stream-iter"` carrying the bind
  name, the subscription expression, and the body;
- `next` lowers as a suspension call flagged `"async": true` (the item-131
  step-flag precedent), so the emitter's async detection ("any await step or any
  async-flagged step", 131 §5) already turns the body into the async generator
  shape.

No `backends/*/runtime.*` change is intended on py/ts: the queue, the fiber
accumulator, and the cancel future are the async-extern runtime's existing
primitives (async frames, the Frame/fiber accumulators). go/java/rust add one
runtime helper each (the cancel-channel select), which is why they are their own
slice. wasm adds nothing but the refusal branch.

## 6. Typed EVENTS: a Stream specialization, NOT a distinct surface

**Call: events are `Stream[T]` with a schema contract and an idempotency key.**
`event OrderCreated { order_id: Str, timestamp: Int }` declares a record type
that is a stream element with a checked schema; `on OrderCreated as e { … }`
desugars to `every e in subscribe(<the OrderCreated stream>) { … }`. Every
obligation the external proposal lists maps onto a rule already in this doc:

| events proposal obligation | delivered by |
|---|---|
| schema compatibility | ordinary record type-check on `T` (G1) |
| consumer lifecycle | the subscription bracket (§0) |
| event ownership | single-consumer linearity (§3.1) |
| replay behavior | §4.5, provider-declared |
| handler idempotency | an idempotency key on the element (item 309's typed key), checked at the `on` |
| a failed handler does not leave a subscription active | A8 aborts the iteration and closes the bracket (§4.7) |
| duplicate handling | dedup against the idempotency key before the body runs |

Rationale for the specialization over a distinct surface: a distinct events
surface would duplicate the six-tier protocol (§4.6), the teardown accounting
(§0), and the refusal set (§3) — three things whose correctness is the whole
item — for a payload that differs only by a schema contract and a dedup key.
Events add exactly those two things on top of `Stream[T]` and nothing else.
They are Slice 5 (§7), gated on the iteration form (Slice 4) they desugar to.

### 6b. Slice 5, as shipped

The surface that landed:

```revl sketch
event OrderCreated(key: order_id) { order_id: Str, quantity: Int }

component Fulfiller requires ship: Ship {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  on OrderCreated as e in sub { emit ship.dispatch(e.order_id) }
}
```

`on … as` parses to the SAME `StreamIterStmt` and lowers to the same
`stream-iter` step as `every … in`, with an additive `event` block carrying the
name, the key, the window and the derived schema. One node, one lowering, one
emitted loop — so the two properties the guarantee rests on (the iteration
boundary immediately after the await, and a `Faulted` that is not caught) are
literally the same code rather than a second implementation that could drift.
The emitted loop gains one gate, `if not <contract>.admit(<x>): continue`, placed
after the await, after its `yield`, and after the terminal test; the contract
itself is built ONCE above the loop.

Three deviations from §6 as written, each stated rather than quiet:

1. **The handler names its subscription (`in <sub>`).** §6's `on <Event> as e
   { … }` resolves the event's stream through the provide/inject graph — "the
   event source is the provided `Stream[T]`" — which needs a REQUIRED
   `Stream[T]` capability the language does not have (`subscribe`'s own
   diagnostic says a required stream capability is a later slice). Naming the
   subscription keeps the source honest and keeps every Slice 2/3 qualifier
   (`policy`, `buffer`, `drain`, the combinator chain, `merge`) available to an
   event consumer, which an implicit `subscribe` would have stranded. When the
   coeffect wiring lands, the clause can become optional.
2. **The key is REQUIRED and the dedup window is BOUNDED.** §6 lists handler
   idempotency and duplicate handling as obligations events deliver; an event
   with no key delivers neither, so a key-less `event` is refused. The window is
   a fixed-size LRU of recently admitted keys — constant per handler, never one
   entry per delivered item — which bounds the claim as well as the memory: a
   redelivery further apart than the window runs the handler again. This
   collapses redeliveries; it is not a durable exactly-once claim, which needs
   the §4.5 cursor.
3. **Schema compatibility is checked on BOTH sides.** Statically the item has the
   event's record type inside the handler, so a field the event does not declare
   and a field used at the wrong type are compile errors. Dynamically each
   delivered item is validated against the derived schema at the boundary before
   the body runs, on item 257's machinery and under its §3.3 rule: an event whose
   shape is not exactly derivable is refused at compile time rather than shipped
   with a vacuous guarantee. A violation raises the same `Faulted` terminal a
   provider abort does, so it takes the path that already closes the
   subscription — §6's last row with no events-specific teardown.

§4.7's acquisition refusal is NOT lifted and the calculus is unchanged: the
dedup window is per handler, so it bounds nothing an `effect … undo …` per
delivered item would need bounded, and the per-iteration discharge still does
not exist.

Also carried by this slice, on the go tier: a document with top-level
declarations routes to that tier's pure typed-core path, which DROPS its
components. A typed event always brings a record declaration with it, so an
event program would have emitted a bare struct and silently never subscribed.
That path now refuses a dropped component that holds a stream, by name.

Still open on §6: the `replay(...)` row (§4.5) and the reconstructible
crash-recovery case (§4.9), and the iteration/handler forms on go, rust, java
and typescript.

## 7. Slices

The blast radius is parser + typecheck + lower + all six backends +
async-coloring, so a landable Slice 1 is the smallest cut that proves BOTH the
core guarantee and the refusal fence, on the reference tier.

| # | slice | stages / backends | surface | depends |
|---|---|---|---|---|
| **1** | **Type + core lifecycle + the guarantee, py + wasm-refusal** | parser (Stream type, `subscribe … undo …`, `next`/`close` calls, `Stream[T,State]`); typecheck (formation §2, linearity + use-after-close §3.1-3.4, `next` color §2.2); lower (bracket step, `subscribe`/`async` IR keys §5); **py** full runtime; **wasm** REFUSES; **ts** if cheap | `Stream[T]`, `Stream[T,State]`, `subscribe`/`next`/`close`, `error` backpressure only | — |
| **2** | Pure combinators + backpressure + test clock | typecheck + lower + py/ts | `map`/`filter`/`take`; `drop_*`/`block` policies (`Paused`); deterministic `advance`-driven firing (§8) | 1 |
| **3** | Blocking tiers + `merge` | **go/java/rust** emit + one runtime helper each; the multi-source teardown ordering | `merge`; go/java/rust lowering of Slices 1-2 | 1 (2 for combinators) |
| **4** | `every … in` async iteration | parser (`every IDENT in`), the effect-context body §4.7, handler-abort-closes | `every x in sub { … }` | 1; **item 131** (the body composes effects across the `next` suspension, so it needs awaited `effect`/`emit`) |
| **5** | Typed EVENTS | `event`/`on … as`, schema contract, idempotency key (item 309), dedup | `event E(key: f)`, `on E as e in sub { }` (§6b) | 4 |

**Slice 1 is the landable unit.** It ships the type, the three core operations,
the bracket lifecycle, and the core-guarantee exit tests (§10.1-10.3) on py,
with wasm refusing — which is enough to prove "unloading the owner closes the
stream" and to prove the refusal fence, and it touches no blocking tier. ts
rides Slice 1 only if it is the same `async function*` shape with no new runtime
(the 131 precedent: py and ts land together when ts is free, else ts is its own
slice). Everything that multiplies teardown accounting (combinators, merge,
blocking tiers) or adds a new body context (`every`, events) defers, each with
its dependency named. Slice 4's dependency on item 131 is the one cross-item
gate: the iteration body is the first place a stream `next` suspension and an
awaited `effect`/`emit` compose, and item 131 owns that composition.

## 8. The deterministic test-time clock

Time-based stream behavior (a `block`-policy drain interval, a future
`throttle`/`debounce`) is made deterministic by reusing the existing test clock:
`Clock.advance(ms)` / the `advance <n><unit>` lifecycle statement (item 57/102,
docs/time-coeffect.md:117). Item delivery itself is not clock-driven — a test
provider `emit`s items explicitly — but any time-windowed buffering fires on
`advance`, "a step in the timeline, not wall-clock" (time-coeffect.md:97). This
is also why wasm's refusal is consistent: wasm already skips `advance`
(lifecycle.py:134), so a tier that cannot advance the clock cannot run a
time-windowed stream either.

## 9. Adversarial review

### The CRITICAL: a `next` parked on a silently-dead provider makes the bracket inverse unreachable

The core guarantee says unloading the owner closes the stream. The design so far
models the subscription as a `bracket` whose inverse `close` runs during the
owner's teardown. Walk the failure:

1. The consumer is parked inside `await sub.next()`, waiting for the provider to
   emit. `next` (before this fix) resolves only when an item or a terminal event
   arrives.
2. The provider dies *silently* — the process holding the socket vanishes, or a
   provider abort forgets to notify its single consumer. No item, no terminal.
3. The owner is withdrawn. Teardown must run the bracket inverse `close`.

On the **blocking tiers** (go/java/rust), the owner activation occupies the very
goroutine/thread now parked in `next`. Teardown is synchronous
(teardown-contract.md, the bound rule). The thread cannot both be parked in
`next` and be running `close`. The inverse is **unreachable**: the subscription's
host listener, buffer, and provider-side registration outlive the owner. The
core guarantee is violated outright.

On the **event-loop tiers** (py/ts) it is subtler and still a violation: the
teardown loop runs on the event loop and can execute `close` — but if `close` is
modeled as "deliver a terminal to the consumer and wait for `next` to drain it,"
it enqueues *behind* a `next` that will never resolve, so teardown either hangs
or the fiber leaks. The owner reports gone (R4 sees its provisions withdrawn)
while its subscription's fiber is still parked. "Closed before the owner
disappears" is false.

Root cause: the name-based async color (§2.2) and the bracket model together let
a suspension (`next`) sit *between* the bracket's registration and its inverse's
reachability. The bracket contract assumes the inverse is always runnable; a
`next` that never resolves breaks that assumption. This is the same blind spot
item 131's review named in a different position — a name-based walk letting an
async operation through — surfacing here as a *liveness* failure of the core
guarantee rather than a soundness failure of an inverse.

### The fix (two parts, both folded into §3-§4 above)

**Part A — `close` is cancellation-first, never delivery-behind.** The awaited
object a consumer parks on in `next` is a RACE between (a) the next item/terminal
and (b) the subscription's own cancellation token. `close` (the bracket inverse)
trips the token synchronously and returns; it never waits for `next` to drain.
Tripping the token resolves the parked `next` with the terminal `Closed`
outcome. In the lowering (§4.6) this is: py/ts race the queue-get against a
cancel future; go/java/rust `select` the item channel against a cancel channel.
So on every tier `close` is reachable *without* the parked `next` having to
resolve on its own — on the blocking tiers because the cancel channel is a
separate `select` case the teardown side closes, on the event-loop tiers because
the cancel future is resolved by teardown, not by the consumer. This is the A1
divert-boundary reading (async-extern.md §4.3) applied to stream iteration:
withdrawal trips the token, the park resolves at that boundary as terminal, the
`every` loop sees the terminal and exits, and the remaining inverses replay LIFO.
This is admission rule 3.4 ("`close` is infallible, non-emitting, and does not
itself suspend") plus the §4.6 cancel-channel/cancel-future lowering, now
load-bearing rather than incidental.

**Part B — provider death is a terminal event, never silence (admission rule
3.6).** Refuse to admit a `Stream[T]` provision whose provider can vanish while a
`next` is outstanding without delivering a terminal. Mechanically: the provider
side is itself a bracket, and the checker requires the provider's teardown to
deliver `Closed` (orderly) or `Faulted(err)` (abort) to its single consumer as
part of its inverse — a host-local channel close, so G5-compatible (non-emitting
on the audit surface, cannot fail). Because provider and consumer teardown is one
LIFO cascade, the dangerous order is provider-first withdrawal; Part B guarantees
that even then the consumer's outstanding `next` resolves to `Faulted`/`Closed`
rather than hanging. Combined with Part A there is no third state: an outstanding
`next` is always terminated by exactly one of the owner's own teardown (Part A)
or a provider terminal (Part B), and the checker refuses any shape that admits a
provider with a third, silent exit.

Together, Parts A and B make the bracket inverse reachable on every admitted
program and on every tier, which is what the core guarantee (§0) asserts. The
exit tests §10.4-10.5 pin exactly these two paths; if either regresses, the
guarantee is a comment, not a fact.

## 10. Exit tests

1. **Subscription roundtrip (py).** `let sub = subscribe orders undo
   sub.close()`; load, receive N items through `next`, unload; `assert
   no_residue`; the trace shows `close` after the last `next` (R1). The
   finding-shaped twin: the same program without `undo` fails compile with the
   rule-3.2 diagnostic.
2. **Unload-closes-the-stream, LIFO (the core guarantee pinned).** Owner
   acquires a sync resource A, then `subscribe … undo close`; withdraw the owner
   while items are mid-flight; assert `close` runs before A's inverse (LIFO), the
   host listener is released, `no_residue` holds.
3. **Use-after-close and double-subscribe refused.** `next` on a `Stream[T,
   Closed]`; a second `subscribe` on one required stream — both compile errors
   (rule 3.1), the second naming the bridge.
4. **The CRITICAL Part A — cancel reaches a parked `next`.** Consumer parked in
   `next` on a provider that never emits; withdraw the owner; assert the park
   resolves as `Closed`, `close` runs, `no_residue` holds, and (on the go/java/
   rust twins once Slice 3 lands) the teardown does not deadlock the worker.
5. **The CRITICAL Part B — provider death is terminal.** Provider aborts with an
   outstanding consumer `next`; assert the consumer's `next` resolves to
   `Faulted`, the consumer's bracket closes, residue is empty; a provider that
   the fixture forces to exit silently must FAIL COMPILE with rule 3.6.
6. **Handler failure closes the subscription (A8, the events obligation).** An
   `every o in sub` body that `fail`s; assert the iteration aborts, the
   subscription bracket closes in the prefix reversal, `no_residue` holds.
7. **Backpressure `error` default.** Buffer overflow with the default policy
   yields a terminal `Faulted(overflow)` and closes; no silent loss.
8. **wasm refusal text.** A stream program lowered to wasm refuses with the §4.6
   `EmitError`, matching the shape of the existing awaited-step and `advance`
   refusals.
9. **Byte-identity.** Every program not using streams emits byte-identically on
   all six tiers (per-backend golden suites, run per-backend; `pytest tests/`
   alone does not run them — the standing wave gap).

## 11. Judgment calls a human should confirm

1. **`every` reuse vs a new keyword** (§1). This design reuses `every` with
   IDENT-`in` lookahead. Confirm over a fresh keyword; `on` is reserved for
   events (§6) and `for` misleads.
2. **Default backpressure = `error`** (§4.4). Recommended: a full buffer faults
   and closes rather than dropping. The alternative (`drop_oldest`) is friendlier
   to telemetry and lossier by default; the strict default matches "asynchrony is
   declared."
3. **Events are a Stream specialization** (§6), not a distinct surface.
   Recommended, to avoid duplicating the protocol/teardown/refusal set;
   reconsider only if a real events workload needs a shape `Stream[T]` cannot
   carry.
4. **The behavior flip has no legacy to break** because streams are new; but the
   CRITICAL's Part B refusal (rule 3.6) will reject provider shapes that "look
   fine" (a provider with no explicit terminal on teardown). Confirm the strict
   refusal over a lenient "warn and inject a terminal," which would make the
   guarantee runtime-conditional.
5. **Slice 1 backends = py + wasm-refusal, ts if free** (§7). Confirm that the
   core guarantee is allowed to be proven on the reference tier first, with the
   blocking tiers (where the CRITICAL's deadlock face lives) deferred to Slice 3
   behind the py-pinned cancel-channel design.

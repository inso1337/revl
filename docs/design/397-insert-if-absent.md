# 397: atomic insert_if_absent on the host Map

Design note for roadmap item 397 (`docs/v2.0-roadmap.md:4124`), the revl-harness
H48 want: a single-use approval ticket got every property except ATOMICITY,
because revl has no compare-and-set on a host Map. This is design-first. It
changes no parser, typecheck, lower, or emit code; it records the measured
problem, the current host-Map shape per tier, what "atomic" can honestly mean on
each host runtime, the G6 reconciliation, two surface options with trade-offs, a
recommendation, a staged plan, and exit tests an implementation agent can pick
up.

## The problem (measured)

H48 enforced single-use approval tickets in a gate. Every SEQUENTIAL threat
(stale tab, curl, retry, replay) is refused; what could not be claimed is that
two genuinely simultaneous approve-runs cannot both consume the same ticket.
The reason is structural, not an implementation gap in the harness:

- The host Map's `insert` overwrites unconditionally and returns nothing, on
  every tier that has it: py sets `self.data[key] = value` and returns `None`
  (`backends/python/runtime.py:2633-2636`), ts `this.data.set(key, value)`
  returning `void` (`backends/typescript/runtime.ts:558-563`), go locks and
  assigns (`backends/go/emit.py:2967-2971`), rust locks and inserts
  (`backends/rust/emit.py:1208`), java `values.put(key, value)` returning
  nothing revl can see (`backends/java/emit.py:2093-2095`).
- G6 (canonically "code outside effect forms is pure", `DESIGN.md:230`;
  worked at `docs/rejections.md:214-236`; the roadmap's "forbids branching
  around an effect" is the item's paraphrase of it) leaves no syntactic
  position for "insert only if absent" as author-written control flow: a
  component activation `if` admits only `fail` in its arms ("component `if`
  is for deliberate L-Raise decisions, not general control flow (G6)",
  `src/revl/lower.py:4944-4948`), and a provide-method body refuses `if`
  statements outright ("use a pure `if` expression in the method value instead
  (G6)", `src/revl/parser.py:1746-1753`).
- And even with the branch gone, the RESULT cannot be read where the claim
  lives: a provide method may run an unbound `effect ... undo ...` per
  request (`demo/components/user_cache.rvl:13-15` does), but binding an
  acquisition there is refused, "only `spawn` may be acquired inside a
  provide-method body" (`src/revl/lower.py:6271-6283`), a phase-1 scoping of
  the instances design ("a general method-body acquisition is a separate
  feature", the comment at `:6276-6277`). So even if `insert` returned a
  Bool today, a claim method could not name it.

So `claim_ticket` is necessarily a read-then-write: read the ledger, then
insert, with the decision made between the two. And "two claim calls can
interleave between the read and the write" is not hypothetical: the placement
bridge serves EVERY tier concurrently per connection. py opens one coroutine
per seam connection (`backends/python/bridge.py:385-415`, `asyncio.start_server`,
each `handle()` awaiting per request), ts mirrors it on the node loop
(`backends/typescript/bridge.ts:419-428`), go accepts each connection on its
own goroutine (`backends/go/placement_runner/bridge/bridge.go:173-183`), rust
spawns an OS thread per connection
(`backends/rust/placement_runner/src/main.rs:55-77`), and java a thread per
connection (`backends/java/placement/PlacementRunner.java:314-325`). Even on
the tiers whose host Map already holds a lock, the lock is per OPERATION, so
a read-then-write is two critical sections with a hole between them. Both
racers read an empty slot; both believe they consumed the ticket. "Consume exactly once" is the
shape of every approval, lock, lease, and idempotency key; revl currently
cannot express it against a host Map without a host-side escape hatch.

The escape hatch already exists in-repo and proves the want is real: the MCP
session's standing-approval ledger implements consume-before-fire in host
Python (`src/revl/mcp/session.py:1679` documents the discipline,
`:1714-1724` are `_consume_approval` / `_consume_grant`), and
`docs/design/246-auto-approve.md:429-478` states the invariant ("Non-replayable",
invariant 5) that a revl program should be able to state itself. Item 397 is
that capability moved into the language surface: one atomic
`insert_if_absent(k, v)` returning whether it inserted.

A repo-wide search confirms the design space is clean: `insert_if_absent`,
`putIfAbsent`, and compare-and-set appear nowhere except item 397's own roadmap
text. No host-Map mutator returns a value today, so this is also the first
host verb whose RESULT the frontend must model, which is a real seam decision,
not a table row (see "Surface and typing").

## Background: the two Maps, and which one this is

revl has two Map surfaces, deliberately disjoint (`docs/stdlib-2.0.md:126-140`;
the disjointness is enforced at table-edit time,
`src/revl/typecheck.py:931-939`):

- The VALUE Map: persistent, pure, structural. `set`/`lookup`/`has`/`size`/
  `keys`/`remove` in `_BUILTIN_SIG` (`src/revl/typecheck.py:826-848`), where
  `set` returns a fresh map and the receiver is untouched
  (`docs/stdlib-2.0.md:150-151`). A CAS is meaningless here: there is no
  shared cell to contend on. A pure `m.has(k) ? m : m.set(k, v)` is already
  expressible and already race-free, because nothing mutates.
- The HOST Map: a mutable in-memory store acquired as an effect,
  `let store = effect Map.new() undo store.drop()`, verbs
  `new/insert/remove/get/drop` in `_HOST_ARG_SIG`
  (`src/revl/typecheck.py:884-895`) plus the promised `size`/`keys` iteration
  surface (items 84/86/88). The worked example is
  `demo/components/user_cache.rvl:9-18`: reads flow as method-value
  expressions (`fn get(key) = store.get(key)`), mutations ride the effect
  stratum (`effect store.insert(key, value)` / `undo store.remove(key)`), and
  a method-time insert joins the activation frame's teardown accumulator.

H48's ledger is the host Map. Item 397 adds one verb to the HOST surface only.
The value Map is untouched (and `docs/stdlib-2.0.md:244-255` already settled
its total-vs-error stance for `remove`; the Bool result here resolves the
analogous question the other way, by reporting instead of erring).

One more current-shape fact matters for typing: the host frontier deliberately
knows ARGUMENTS but not RESULTS. `_HOST_ARG_SIG`'s header says "nothing here
claims to know what comes back" (`src/revl/typecheck.py:872-880`),
`builtin_check` returns `None` (opaque) for a host-family receiver
(`src/revl/typecheck.py:1004-1010`), and an acquisition binding refuses a type
annotation because "`effect` binds a host-valued object, whose type revl does
not model" (`src/revl/parser.py:1650-1663`). `insert_if_absent -> Bool` must
breach that frontier, narrowly, for one verb (see "Surface and typing").

## What "atomic" means per tier: is there even shared state to CAS?

The atomicity gap only bites under genuine concurrency. So the first design
question is the concurrency model per tier: who can observe a host Map between
a read and a write? The honest answer differs per tier, and on two tiers the
existing `insert` is not even self-consistent about it.

| tier | representation | who shares it | atomic mechanism for CAS |
|------|----------------|---------------|--------------------------|
| py   | plain `dict` in `class Map` (`backends/python/runtime.py:2592-2642`) | async tasks on ONE event loop, one per seam connection (`backends/python/bridge.py:385-415`) | run-to-completion of a synchronous op |
| ts   | `globalThis.Map` in `MapHandle` (`backends/typescript/runtime.ts:535-563`) | async tasks on ONE event loop (`backends/typescript/bridge.ts:419-428`) | run-to-completion of a synchronous op |
| go   | `struct { mu sync.Mutex; m map[string]V }` (`backends/go/emit.py:2956-2982`) | one goroutine per bridge connection, genuinely parallel (`backends/go/placement_runner/bridge/bridge.go:173-183`) | one `mu.Lock()` section spanning test and insert |
| rust | `Arc<Mutex<HashMap<String, V>>>` (`backends/rust/emit.py:1190-1235`) | one OS thread per bridge connection (`backends/rust/placement_runner/src/main.rs:55-77`) | one `lock()` spanning test and insert (entry API) |
| java | plain `java.util.HashMap<String, V>`, NO synchronization (`backends/java/emit.py:2083-2100`) | one thread per bridge connection (`backends/java/placement/PlacementRunner.java:314-325`); compensations on a `newCachedThreadPool` (`backends/java/emit.py:2965-2966`) | `ConcurrentHashMap.putIfAbsent` (and this fixes a latent gap, below) |
| wasm | none: host builtins are refused at compile time (`backends/wasm/emit.py:750-754`, `:142`; `docs/stdlib-2.0.md:257-263`) | nobody | inherits the named refusal |

Per-tier honesty, in detail:

- py. The reference runtime is normatively thread-free and deterministic: "no
  driver, no wall-clock, no threads, no timers", every wait a bounded number
  of cooperative scheduler turns (`backends/python/runtime.py`, the
  `.. _pool-job-semantics:` block, quoted as the single normative definition
  by `docs/design/295-schedule-testing.md:26-48`). Concurrency on py is task
  interleaving on one asyncio loop, and a task is preempted only at named
  suspension points ("between two suspension points the runtime never
  preempts", `295-schedule-testing.md:37-39`). So a SYNCHRONOUS
  `insert_if_absent` containing no await is atomic by construction. The race
  item 397 names is still real on this tier: a read-then-write that SPANS a
  suspension point (an `async fn` method interleaving at `await emit`, or two
  placement processes proxying into the owner, `docs/parallel-activation.md`)
  interleaves today, and parallel activation already runs independent branches
  concurrently on py (`docs/parallel-activation.md:64-65`), with the safety
  argument resting on G2/G3 key-disjointness, an argument that does NOT extend
  to two handlers of one component sharing one ledger Map. The GIL is not part
  of the contract; the loop is.
- ts. Same model, node's single-threaded event loop
  (`docs/design/teardown-contract.md:196-203` pins "single-threaded event
  loop" for this tier). A synchronous CAS is trivially atomic per task.
- go. Real parallelism exists and the runtime already knows it: every host
  Map op takes `mu` (`backends/go/emit.py:2967-2982`), and
  `docs/design/teardown-contract.md:208-226` documents that two compensations
  may genuinely run concurrently on this tier. The CAS is the existing lock
  held across the membership test AND the insert, one critical section instead
  of two.
- rust. Same shape: the mutex is already there
  (`backends/rust/emit.py:1194`), the CAS is one `lock()` using the entry API
  so the probe and the write cannot separate.
- java. The honest finding of this design: the java host Map is a bare
  `HashMap` with no synchronization while the tier's placement runner serves
  each bridge connection on its own thread
  (`backends/java/placement/PlacementRunner.java:314-325`). Today's `insert`
  is therefore not merely non-atomic on java, it is not memory-safe under
  concurrent `put`; the tiers disagree about a contract nobody wrote down,
  which is exactly the `_HOST_ARG_SIG` header's origin story
  (`src/revl/typecheck.py:872-876`) replayed at the value level. Landing
  `insert_if_absent` as `ConcurrentHashMap.putIfAbsent` and migrating the
  backing map makes the whole verb surface thread-safe in one move.
- wasm. No host Map exists to CAS; the verb inherits the existing named
  refusal, the same "deliberate tier limit, never a miscompile" shape the Map
  value type takes there (`docs/stdlib-2.0.md:257-263`).

And the boundary of the promise, stated so nobody over-reads it: the host Map
is IN-PROCESS memory on every tier. A "multi-worker dispatch pool" of separate
OS processes shares no host Map at all; in the placement model the ledger
component lives in ONE process and remote consumers reach it through proxies
(`docs/parallel-activation.md:49-53`), so every mutation lands in the owner's
runtime and the per-tier mechanisms above are sufficient. What
`insert_if_absent` does NOT provide is single-consumption across independently
booted runtimes sharing an external store; that needs a durable backing store
(the `Pool` family, or 246's WAL) and is out of scope here. The contract below
is written so a future shared-store Map can keep it unchanged. Where today's
runtime is effectively single-threaded (py, ts), the verb is still the correct
SHAPE: it collapses the read and the write into one suspension-free step, so
the program's correctness stops depending on where the tier's suspension
points happen to fall.

## G6 reconciliation: a value-branch, not an effect-guard

G6's mechanism is by construction: outside effect forms every statement is
pure (`src/revl/lower.py:10`, `docs/rejections.md:214-236`, `DESIGN.md:230`).
Two grammar facts carry it at the sites that matter here:

- Branching to decide WHETHER an effect runs is unwritable. An activation
  `if` arm admits only `fail` and nested guards
  (`src/revl/lower.py:4944-4948`); a provide-method body has no `if`
  statement at all (`src/revl/parser.py:1746-1753`). The fault machinery
  leans on this: an `if`-wrapped effect step can only be constructed in IR by
  hand, never from surface revl (`tests/test_fault_tests.py:228-245`,
  `src/revl/fault.py:541-543`).
- Branching on a VALUE an effect returned is already legal and SHIPPING. The
  emit corpus's own flagship does exactly it: `let token = effect bus.open()
  undo bus.close(token)` followed by `if (token < 0) { fail ... }`
  (`tests/fixtures/emit_py_corpus/services_body.rvl:10-14`, whose header
  comment names it "an `if` L-Raise guard containing `fail` (G6)"). In method
  bodies the parser's own hint endorses the value-branch: "use a pure `if`
  expression in the method value instead (G6)"
  (`src/revl/parser.py:1751`); a `let`-bound value flows into ternaries and
  `match` (docs/syntax-2.0.md admits both in method bodies).

`insert_if_absent` needs no change to either fact. The effect runs
UNCONDITIONALLY at its site, exactly like `effect store.insert(k, v)` today;
the conditional ("only if absent") moves INSIDE the primitive, which is the
only place it can be atomic; and the Bool that comes back is a plain value the
program branches on with the already-permitted pure `if` expression. There is
no effect-guarding branch anywhere in the source. So the answer to "does G6
permit branching on the result of a single atomic op" is yes, today, with no
rule change: the result-branch is a value-branch, and G6 never constrained
value-branches.

What stays inexpressible, stated honestly: the result can select VALUES, not
EFFECTS. A method cannot run a different `emit` on the claimed vs. duplicate
outcome, because that would be an effect-guarding branch, and it cannot `fail`
(A8 confines `fail` to activation bodies, `src/revl/parser.py:1736-1744`). The
claim method returns its Bool (or a value derived from it) and the CALLER
reacts, which is exactly how H48's gate consumes it. If per-outcome effects
become a measured want, that is a separate item about conditional effect
forms, not this one; nothing here forecloses it.

## Surface and typing

### The spelling

A host verb on the Map family, sibling of `insert`, bound with the existing
let-effect form:

```revl sketch
component TicketGate requires audit: Audit provides gate: Gate {
  let ledger = effect Map.new() undo ledger.drop()

  provide gate {
    fn claim(ticket, actor) {
      let fresh = effect ledger.insert_if_absent(ticket, actor)
                  undo ledger.remove(ticket)
      return fresh ? "claimed" : "already-consumed"
    }
  }
}
```

This sketch is refused TODAY at the let-effect line with "only `spawn` may be
acquired inside a provide-method body" (verified against this checkout), so
the sketch fence is honest, and the refusal names the one grammar extension
the design needs (next subsection).

Arguments mirror `insert`: the checker row is `["Str", "Str"]` beside
`Map.insert` in `_HOST_ARG_SIG` (`src/revl/typecheck.py:884-895`), and the
value parameter stays generic `V` on the tiers that genericized the host Map
(go `Map[V any]`, rust `Map<V>`, java `Map<V>`; items 113/176). Mirroring
`insert` inherits an existing frontend/backend disagreement honestly rather
than widening it: the checker row says the value is `Str` while every
emitter infers a generic `V` from insert call sites; this note keeps the two
rows consistent WITH EACH OTHER and leaves reconciling the frontier's value
type to its own item.

Mirroring `insert` also carries a hard emitter obligation that is easy to
miss: go, rust, java, and the self-host emitter all infer the host Map's `V`
by scanning for the LITERAL method name `"insert"` at acquisition sites
(`backends/go/emit.py:1584-1656`, `backends/rust/emit.py:1698-1775`,
`backends/java/emit.py:2585-2613`, `selfhost/emit_java.rvl:1474-1543`). A
component whose ONLY writer is `insert_if_absent` would otherwise silently
degrade `V` to the string default; every scanner must learn the new verb. The name is
chosen against the namespace rules: it collides with nothing in
`_BUILTIN_METHODS` (`src/revl/lower.py:183`), so the table-edit-time
disjointness check (`src/revl/typecheck.py:931-960`, which raises at module
import, not at test time) stays clean, and the pinned verb-set assertions in
`tests/test_map_value_type.py:54-62` must be extended deliberately, which is
that test doing its job.

### The one grammar extension: binding this acquisition in a method body

A provide method may already run the unbound form per request
(`effect store.insert(k, v) undo store.remove(k)`,
`demo/components/user_cache.rvl:13-15`, joining the activation frame's
teardown accumulator), but `let x = effect ...` in a method body is refused:
"only `spawn` may be acquired inside a provide-method body"
(`src/revl/lower.py:6271-6283`). The refusal's own comment scopes it: spawn
got a request-scoped nested teardown scope in phase 1 of the instances
design, and "a general method-body acquisition is a separate feature"
(`docs/design-v2-instances.md`).

This design lifts the restriction NARROWLY, not generally: a let-effect in a
provide-method body is additionally admitted when its acquire is a
result-declared host verb (today: exactly `insert_if_absent`) on an existing
host local. The reasons the phase-1 restriction exists do not apply to this
case:

- A spawn binding names a request-scoped INSTANCE needing its own nested
  teardown scope. This binding names a checked VALUE (`Bool`); there is no
  handle, no nested scope, nothing to dispose. The effect-and-undo pair joins
  the activation accumulator exactly as the unbound method-time `insert`
  does today; only the result gains a name.
- A general method-body acquisition would let a per-request effect acquire a
  host OBJECT whose lifetime outlives the request, the open question the
  phase-1 comment defers. A `Bool` has no lifetime. The general feature stays
  deferred, untouched.

Everything else about the site is unchanged: `verified effect` stays
activation-only (`src/revl/lower.py:6255-6270`), every other acquisition in a
method body stays refused, and the activation-body spelling of the same
let-effect works with no new rule (activation bodies already bind
acquisitions and already guard on their results,
`tests/fixtures/emit_py_corpus/services_body.rvl:10-14`).

### The result type: a narrow breach of the opaque-result frontier

The host frontier types arguments and deliberately not results
(`src/revl/typecheck.py:872-880`, `builtin_check` returning `None` at
`:1004-1010`). `insert_if_absent` is the first host verb whose result the
program MUST consume in checked code (the whole point is the pure `if` on it),
so the frontier gains a result column for exactly the verbs that declare one:

- A `_HOST_RESULT_SIG: dict[str, str]` beside `_HOST_ARG_SIG`, with one entry,
  `{"Map.insert_if_absent": "Bool"}`. `builtin_check`'s host-family branch
  returns the declared result instead of `None` when the verb has one.
- The let-effect binding gets the declared type. Today a `LetEffect` bind is
  host-provenance: untyped, annotation-refused
  (`src/revl/parser.py:1650-1663`), tracked in `env.host_locals` so its verb
  surface stays verbatim (`src/revl/lower.py:4641-4648`). A bind whose acquire
  is a result-declared host verb is instead entered in `env.type_env` as
  `Bool` and NOT entered in `host_locals`: it is a checked value, not a stub
  receiver. The annotation refusal can stay as-is (the type is inferred, and
  keeping the refusal avoids a new annotated-acquisition grammar); the hint
  text should learn the exception so the diagnostic does not lie.

This is a deliberate, one-verb breach, not a policy change: `get`'s result
stays opaque, `size`/`keys` keep their existing promised-surface path, and the
G8 audit surface (`docs/contract-errata.md`) shrinks by exactly the values the
frontend now vouches for.

### Classification: which stratum carries it

Not `pure`: it mutates shared state and its result is observable evidence of
that mutation; a pure classification would admit it into `fn` bodies, which
are "pure from the outside: no context, no effects, no host access"
(`docs/syntax-2.0.md:117-119`). Not an `emission`: `emit` is the one-way
outbound step for service calls and emission externs; this is an acquisition
against owned state with a meaningful inverse. Not `witnessed`/`acquire`
extern machinery either, because it is not an extern at all: the
pure/acquire/emission/witnessed classifications are EXTERN declaration
properties (`src/revl/parser.py:1098-1224`, `src/revl/lower.py:1957-1987`),
and the host Map is a builtin family the frontend owns.

The classification, precisely: a host verb in the effect stratum, spelled as
an acquisition (`let x = effect ... undo ...`), exactly where `Map.insert`
already lives by convention (`demo/components/user_cache.rvl:13-15`). The
bound result rides the let-effect binding that already exists for
acquisitions; the only new thing is that this binding is typed.

Two sharp edges, resolved:

- The statement form is refused. `effect ledger.insert_if_absent(k, v) undo
  ...` without a binding discards the Bool, and a CAS whose result nobody
  reads is a plain `insert` with extra steps, plus an unsound undo (next
  bullet). Lower refuses it with a redirect: bind the result with
  `let ... = effect ...`, or use `insert`.
- The undo is guarded by the result. A site-spelled `undo ledger.remove(k)`
  is correct only when the CAS actually inserted; replayed after a `false`,
  it would remove the WINNING claimant's entry at teardown, which is
  precisely the corruption single-use exists to prevent. G7 teardown is
  compiler-derived, so the derivation is the right place for the guard: the
  emitters register the site-spelled undo ONLY when the bound result is true.
  The emitted shape already supports this on every tier, because the undo
  closure already receives the binding (py: `bind = _revl_frame.acquire(label,
  lambda: acquire, lambda bind: undo)`, `backends/python/emit.py:1387-1394`);
  the change is to make registration conditional on the bind for this verb.
  The precedent for a result-aware, declaration-owned inverse is the
  witnessed-extern machinery, where the inverse receives the result and
  registers on the Ok branch with no site-spelled undo
  (`docs/design/243-witnessed-externs.md`; `backends/python/emit.py:1167-1181`;
  the grammar already admits undo-omission for witnessed calls,
  `src/revl/parser.py:1927-1945`).

### Option (a) vs option (b): where the undo lives

- Option (a), site-spelled undo with a derived guard (the sketch above). The
  author writes the inverse as for every other effect; the compiler registers
  it iff the CAS took. Cost: the guard is invisible at the site, a small
  piece of derivation magic, but of exactly the kind G7 already performs
  (teardown order is derived too, `docs/rejections.md:237-244`).
- Option (b), verb-owned inverse, no site-spelled undo: `let fresh = effect
  ledger.insert_if_absent(k, v)` with the runtime itself registering "remove
  iff I inserted", mirroring the witnessed form's undo-omission. Cost: it
  extends witnessed-style declaration-owned inverses from externs to a
  builtin family, new machinery in parser (admit the omission for this verb),
  lower (G4's undo-required gate, `src/revl/lower.py`, must learn the verb),
  and every emitter; and it makes this the only Map mutation whose teardown
  is invisible in source, where its siblings spell theirs.

Recommendation: option (a). It is the smaller mechanism, it keeps the
teardown visible and author-owned like every neighbouring effect, and the
guard is a one-line emission change per tier at a site that already holds the
bound value. Option (b) becomes attractive only if verb-owned inverses
generalize across the host surface, which is its own design.

## Cross-tier semantics contract

Like item 385 (`json_stringify` canonical bytes, `docs/v2.0-roadmap.md:4102`),
a host/stdlib operation must mean one thing on every tier, and the contract
must be written before the emitters are, per the house rule the collections
design states: "spec, then checker, then emitters, then tests"
(`docs/collections.md:22-28`). The contract:

1. Effect. If `k` is absent at the operation's linearization point, the map
   gains `k -> v` and the call returns `true`. Otherwise the map is unchanged
   (the existing value survives, `v` is discarded) and the call returns
   `false`. No other outcome exists; the operation never faults on a present
   key.
2. Atomicity. No observer that the tier's execution model admits can witness
   the membership test and the insert as separable steps: on go/rust/java the
   lock (or `putIfAbsent`) spans both; on py/ts the operation is a single
   synchronous, suspension-free step, atomic under run-to-completion. Under
   any admitted concurrency, N simultaneous `insert_if_absent(k, ...)` on one
   map yield exactly one `true`.
3. Determinism. The same sequential op sequence yields the same Bool sequence
   and the same subsequent `get`/`size`/`keys` observations on every tier
   that has the host Map, `keys()` in the already-pinned canonical Str order.
4. Teardown. The inverse of a `true` CAS is the site-spelled undo; a `false`
   CAS registers NO inverse (its inverse is the identity).
5. Trace. The host trace records the outcome: `<tag>.insert_if_absent <key>
   -> true|false` beside the existing `<tag>.insert <key>` record
   (`backends/python/runtime.py:2636`). The residue prover folds the trace
   vocabulary into its outstanding-key fingerprint
   (`src/revl/fault.py:1219-1269`), so the verb needs an explicit fingerprint
   rule: a `true` CAS counts as the key set (exactly as `insert`), a `false`
   CAS counts as nothing, or the residue fingerprint under- or over-counts.
6. wasm. The verb inherits the tier's named host-builtin refusal; refusing is
   conformant, miscompiling is not (`docs/stdlib-2.0.md:257-263`).

How conformance asserts it: the 385 template. A probe corpus runs the same op
sequence through each tier's runner (`RUNNERS`, `src/revl/test.py:719-727`)
and asserts the ONE shared expected observation list, not per-tier
self-consistency (`tests/test_json_cross_tier_bytes.py:1-38` is the model).
The concurrency half runs only where the tier admits real concurrency (a
goroutine/thread fan-in on go/rust/java; an interleaved two-task probe on py),
following the TCK's C1 pattern of a spec-level requirement that reports
pending until a runtime's adapter actually drives it
(`tck/spec.py:369-380`, `docs/collections.md:151-160`).

## Per-tier implementation sketch

- py (`backends/python/runtime.py`, `class Map`): membership test plus
  assignment plus record, one synchronous method, no await. Returns `bool`.
- ts (`backends/typescript/runtime.ts`, `MapHandle`): `if (this.data.has(k))
  return false; this.data.set(k, v); return true`, one synchronous method on
  the loop. Returns `boolean` (revl Bool is boolean on this tier).
- go (`backends/go/emit.py`, the `Map[V]` runtime block at `:2956`): one
  `mu.Lock()` section: comma-ok probe, conditional assign, return `bool`.
- rust (`backends/rust/emit.py`, the runtime block at `:1190`): one `lock()`,
  `match map.entry(k) { Occupied(_) => false, Vacant(e) => { e.insert(v);
  true } }`.
- java (`backends/java/emit.py`, the `Map<V>` class at `:2083`): migrate
  `values` to `java.util.concurrent.ConcurrentHashMap` and return
  `values.putIfAbsent(k, v) == null`. This also makes `insert`/`remove`/`get`
  thread-safe, closing the latent gap the concurrency table above records.
  (`ConcurrentHashMap` refuses null values; revl host inserts never pass
  null, and the existing `get` already wraps absence in `Optional`.)
- wasm (`backends/wasm/emit.py:114`, `:143`): extend the named refusal so the
  message cites the verb, same shape as `Map.new`.
- Emitters, all tiers with the verb: type the let-effect bind as the tier's
  Bool, and guard undo registration on it (py seam:
  `backends/python/emit.py:1387-1394`; each tier's let-effect analog).

No tier needs a representation change; Bool crosses every boundary already.

## Staged implementation plan

Each stage lands independently and keeps every golden green until its own
tests arrive.

- Stage 1 (frontend surface). Add the `Map.insert_if_absent` row to
  `_HOST_ARG_SIG` and the new `_HOST_RESULT_SIG`; return the declared result
  from `builtin_check`'s host branch; type the let-effect bind and keep it
  out of `host_locals`; admit the method-body let-effect for result-declared
  host verbs (the narrow lift of `src/revl/lower.py:6271-6283`); refuse the
  unbound statement form with the redirect hint; extend the pinned surface
  assertions (`tests/test_map_value_type.py:54-77`). Two adjacent gaps to
  decide, not silently inherit: `host_check` early-returns on an UNKNOWN verb
  (`src/revl/typecheck.py:899-901`) and the component-body `host_locals` path
  passes method calls through verbatim with no check
  (`src/revl/lower.py:4645-4650`), so today `store.insert_if_absent(k, v)`
  in a component body already compiles as an unchecked pass-through and
  would hit the host runtime's missing method at boot, the exact
  compiles-then-crashes shape item 84 fixed for `keys`/`size`
  (`dogfood/findings-harness-m6.md:15-24`). Stage 1 should route the
  `host_locals` method path through `host_family_check` so the verb (and its
  siblings) are checked where they are used. Exit: the sketch above compiles
  through lower; `fresh` types as `Bool` and a ternary on it typechecks; the
  statement form is refused with the redirect; a misspelled or wrong-arity
  host verb in a component body is refused at compile time; all existing
  host-verb tests pass untouched.
- Stage 2 (py and ts: runtime plus emit). Implement the runtime method on
  both tiers; emit the typed bind; guard the undo registration on the bound
  result. Exit: the sequential conformance sequence passes on py and ts; the
  teardown exit test passes (a `false` CAS leaves the winner's entry alone).
- Stage 3 (go, rust, java). Runtime blocks per the sketches; the java
  `ConcurrentHashMap` migration with its own review note (it changes the
  backing class of every java host Map); teach every `V`-inference scanner
  the new verb (`backends/go/emit.py:1584-1656`,
  `backends/rust/emit.py:1698-1775`, `backends/java/emit.py:2585-2613`, and
  the self-host mirror `selfhost/emit_java.rvl:1474-1543`). Exit: per-backend
  emit-and-run pins in `backends/{go,rust,java}/test_emit_*.py`; the
  thread/goroutine fan-in test yields exactly one `true`; a component whose
  only writer is `insert_if_absent` still pins a non-`Str` `V` on go, rust,
  and java.
- Stage 4 (conformance and docs). The cross-tier probe file modeled on
  `tests/test_json_cross_tier_bytes.py`; a `tools/conformance.py` `CASES` row
  and `make matrix` regeneration; the TCK requirement case (pending until
  driven); the `docs/stdlib-2.0.md` §Map host-surface update stating the
  contract; the fault-DSL verb; promote this note's sketch blocks per the
  doc-examples discipline. Exit: matrix and doc gates green.
- Stage 5 (self-host check, item 391). Per the self-host full-language goal,
  answer "does this need a self-host port?" explicitly: the verb lives in
  checker tables the self-host mirrors, so either add the row to the
  self-host checker in the same change or record it as a named frontier
  entry. Exit: self-host oracle agreement holds (diagnostics and emitted
  bytes for the corpus family unchanged, or the new surface gated behind its
  own key).

## Exit tests

- Sequential (every tier with the host Map, one shared expectation): on a
  fresh map, `insert_if_absent(k, a)` is `true`; `insert_if_absent(k, b)` is
  `false` and `get(k)` still reads `a`; `remove(k)` then
  `insert_if_absent(k, b)` is `true`. Identical Bool and observation
  sequences across py/ts/go/rust/java.
- Concurrency, parallel tiers: on go/rust/java, N concurrent workers all
  `insert_if_absent(k, self)` on one shared map; exactly one receives `true`,
  and `get(k)` reads the winner's value.
- Concurrency, loop tiers: on py, two interleaved async tasks each perform
  the claim; exactly one receives `true`. As the motivating negative, the
  same two tasks performing read-then-write with a suspension point between
  the read and the write BOTH claim, demonstrating the gap the verb closes.
- Teardown soundness: a component method CAS-claims a key another activation
  already holds; the `false` CAS registers no undo, and teardown leaves the
  winner's entry for the winner's own undo. The `true` case reverts exactly
  as `insert`'s undo does today.
- G6 unchanged: an `if` statement around an effect still refuses with the
  existing diagnostics; the pure ternary on the bound Bool compiles; a `fn`
  body naming the verb still refuses (no host access in pure code).
- The binding lift is exactly one verb wide: `let x = effect
  ledger.insert_if_absent(k, v) undo ...` compiles in a provide method;
  `let h = effect Map.new() undo h.drop()` in a provide method still refuses
  with the spawn-only message (`src/revl/lower.py:6279-6283`); `verified
  effect` stays activation-only.
- Statement form refused: `effect m.insert_if_absent(k, v) undo ...` without
  a binding is a compile error with the bind-or-use-insert redirect.
- wasm: the named refusal message cites `insert_if_absent`; no miscompile.
- Doc gate: this note's `revl sketch` blocks must NOT compile until the
  feature lands, then must be promoted (`tests/test_doc_examples.py:334`).

## The honest hard part (consolidated)

Three things this design cannot make true, said plainly. First, atomicity
stops at the process edge: the host Map is in-process memory on every tier,
so `insert_if_absent` makes single-consumption sound within one runtime
instance (which, under the placement model's proxies, is where one component's
ledger actually lives), and no in-memory verb can extend that across
independently booted runtimes; that is a durable-shared-store item, and the
contract here is written so such a Map could keep it verbatim. Second, the
result-typed host verb deliberately breaches the "arguments checked, results
opaque" frontier the host surface was built on; the breach is one verb wide
and the frontier rule stays the default, but it is a precedent, and the next
result-typed verb should cite this note rather than widen the seam silently.
Third, the derived undo guard puts a branch in emitted teardown that the
source does not spell; it is the same kind of derivation G7 already performs
for ordering, and the alternative (a site-spellable conditional undo) would
put an effect-guarding branch back in the author's hands, which is the exact
shape G6 exists to refuse. The java tier's unsynchronized `HashMap` is the
one place the current surface is not merely silent but wrong under its own
tier's concurrency, and stage 3 fixes it as a precondition of claiming the
contract there, not as a side quest.

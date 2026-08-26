# Design: async/await effect composition (item 131)

Status: design proposed. The scope boundary, the surface, the admission rules,
the LIFO-across-await argument, and the per-tier lowering are decided here;
implementation builds on the landed async family (items 80/90/92/106/115/117/
141/170/263/264) and changes no runtime.
Base: `origin/main` @ `9b60c48`. Every `file:line` anchor below was verified
against that sha, and every "admitted"/"refused" claim about today's checker
was reproduced by compiling a probe against it, not read off a doc.

Problem (docs/v2.0-roadmap.md:3165): async externs land and the runtime
supports async bodies; the remaining piece is explicit language-level
async/await for effect composition. A provider doing I/O should be able to
await during activation, register the inverse for what it acquired, and keep
the teardown LIFO and residue-free. The roadmap's own instruction is the crux:
scope carefully against what async externs already cover.

## 1. The scope boundary (the deliverable of this document)

The async surface is mostly built. What follows is the precise line between
what ships and what this item adds, established by reading the machinery and
by probing the checker at the base sha.

### Already covered, do not rebuild

| capability | where it lives | item |
| --- | --- | --- |
| `async` extern modifier; emission-only; no `compensate`; validity rules | src/revl/lower.py:1757-1815 | 80 |
| transitive async coloring of module fns (fixpoint, witness chains) | src/revl/emission_analysis.py `_async_callables`; stamp at lower.py:3072-3113 | 90 |
| `(…) -> Async[T]` function values, arrow admission, coercion, leak refusal | docs/design/async-function-values.md; lower.py:4005-4029 | 92 |
| async color through spawned-handle emissions; `_revl_as_async` coroutine pass-through | tests/test_async_handle_colour.py | 106 |
| py emits async externs as `async def`; py await seed includes them | backends/python/emit.py | 115 |
| sync provide method reaching a req-target async op refused in any expression position | lower.py:4843-4869 | 117 |
| req-target async ops auto-awaited inside async provide methods | tests/test_async_sync_scope.py:94 | 141 |
| timer bodies reaching async ops admitted, colored, awaited/cancelled by the runtime | lower.py:4925-4936 | 57/170 |
| `async fn` service operations; `await` of host async values in async methods (A1) | docs/syntax-2.0.md §5; parser.py:1426-1437 | services 2.0 |
| activation `await <expr>` step as a divert boundary, py/ts/go/java/wasm lowerings | backends/python/emit.py:985-990, backends/typescript/emit.py:1464-1468, backends/go/emit.py:2052-2056, backends/java/emit.py:3531, backends/wasm/emit.py:1108-1120 | v1/A1 |
| activation `await` of a **req-target async op** works end to end (`await w.heat()` emits `await _revl_ctx.w.heat()` + boundary yield) | probe against 9b60c48 | already green |
| the LIFO teardown machinery itself: one per-activation stack, three entry kinds, two-phase abort, discharge on commit | docs/design/teardown-contract.md | 243/247/245 |

The last row matters most: **item 131 needs no teardown change on any tier.**
An accumulator entry does not know whether a suspension separated its
registration from the abort, and nothing in the two-phase loop reads time.

### The genuine gap (what 131 adds)

Three step positions in an activation body cannot compose with a suspension
today, and two of them fail *silently*. Probed at the base sha:

| position | async extern / colored fn | req-target async op |
| --- | --- | --- |
| `await <call>` step | **refused** (lower.py:3145-3166: "reaches async extern … in a setup/activation body, which cannot suspend a fiber (A1)") | admitted, correct |
| `effect <call> undo U` acquisition | refused (same walk) | **admitted, silently wrong**: py binds the coroutine itself (`x = _revl_ctx.w.heat()`) and registers the inverse against it; ts binds the `Promise` |
| `emit <call>` step | refused (same walk) | **admitted, silently wrong**: py creates a coroutine and never awaits it, so the emission never fires; ts fires a floating unordered `Promise` |
| `undo` / `compensate` slot | refused (the walk covers undo exprs) | **admitted, silently wrong**: teardown would build a coroutine and drop it; the inverse or compensation never runs |

The refusal side is the fence item 80 built on purpose
(`_async_reached_outside_provide`, lower.py:3854-3881, prunes provide and
timer bodies and matches async *names*). The admitted-and-wrong side is the
fence's blind spot: that walk is name-based, so a req-target async operation
(rule 3 of the item-92 async-reach, `_req_op_is_async`, lower.py:3885-3897)
passes straight through it. Item 117 closed exactly this hole for provide
methods (`_reached_async_req_ops` runs at lower.py:4845 for method bodies
only); nothing runs it for activation bodies. This is the compiles-implies-runs
bug class the checker exists to prevent, live today in the proof surface
itself: an inverse registered against a coroutine object is residue the
`no_residue` machinery cannot even name.

So the item, scoped: **admit `await` in the three effect-composition step
positions, refuse the async-without-await spellings that today leak, and keep
teardown synchronous.** The runtime, the teardown contract, the coloring
fixpoints, the function-value machinery, and the six await-step lowerings all
stand as they are. The roadmap's one-line framing ("a provider doing I/O
awaits without blocking the consumer's coeffect activation") is two-thirds
shipped: the await step exists and the reactive activation model already keeps
consumers PENDING rather than blocked; what is missing is the await *composed
with* `effect` and `emit`, plus honesty in the positions that pretend to
compose today.

## 2. The surface

Three spellings, all in activation-body statement position, all with the
divert-boundary reading of `await` that is exclusive to activation bodies
(docs/syntax-2.0.md §5, §7.1):

```revl fragment
component PgDatabase provides db: Database {
  config { url: Str }

  // 1. async acquisition: await in effect position, inverse registered
  //    against the landed value
  let pool = effect await pool_open(config.url) undo pool.close()

  // 2. awaited emission step, optional compensation
  await emit audit.record(`pool up: ${config.url}`)
       compensate audit.retract(config.url)

  // 3. the existing await step, unchanged, now also legal on async
  //    externs and colored fns (today it is req-ops and Job.run only)
  await warm_caches(pool)

  provide db { ... }
}
```

Grammar delta (two productions touched, both one token):

```
componentstmt := ...
  | ['let' IDENT '='] 'effect' ['await'] expr 'undo' expr
  | 'await' 'emit' expr ['compensate' expr]
  | 'await' expr                                   -- existing step
```

`await` stays a statement-position keyword; there is still no await
*expression* anywhere in the language. `await emit` puts the boundary marker
outermost because that is what the step is: an iteration boundary whose
payload is an emission. G4's `emit` marker stays glued to the crossing call,
and the parser needs one token of lookahead after `await`
(src/revl/parser.py:1426, the existing `AwaitStmt` branch).

How this differs from an async extern call, precisely: an async extern call
inside an `async fn` provide method suspends *the method's own in-flight
window* and is awaited implicitly by the emitter (the A1 reading; the author
never spells `await` at a call site, docs/design/async-extern.md §2). In an
activation body the suspension opens a *divert window*: the runtime may
withdraw the component while the call is in flight, and the accumulated
inverses replay. Different meaning, therefore different syntax (the §0
governing principle): the activation spelling is explicit, always, and the
checker enforces the exact pairing both ways (§3).

### What an author may await in these positions

The union of every suspension source the async family already tracks: an
`async` extern, an async-colored fn (item 90), or a req-target `async fn`
service operation (item 92 rule 3). The `Async[T]`-typed parameter case does
not arise: activation bodies are not fn bodies and declare no parameters.

### Fences (v1, each with an honest refusal)

- **The block effect form does not take `await`.** `effect { …steps…; acq }`
  keeps its stratum-1 interior. The refusal hint names the escape: hoist the
  preparation into a module fn (it async-colors under item 90) and write
  `effect await prepped_open(cfg) undo …`. Lifting this later is additive.
- **No plain awaited binding.** `let x = await f()` stays refused in
  activation bodies for the same G6 reason plain `let` is refused there
  (docs/syntax-2.0.md §4b.3): a bound value with no inverse has no place on
  the accumulator. The binding form for an async result is spelling 1.
- **Witnessed acquisitions stay sync.** A `witnessed` extern cannot be
  `async` (lower.py:1782, unchanged), so `effect await` on one cannot arise.
  The Ok-conditional transactional registration across a suspension is real
  design work and its own item; refusing at the extern keeps it out of v1.
- **Method-time effects are untouched.** Only `spawn` may be acquired in a
  provide-method body (design-v2-instances), so no method-position variant of
  spelling 1 exists to design.

## 3. Admission rules (exact pairing, both directions)

The checker enforces that the spelling and the reach agree, the same
declared-property stance as everything async ("asynchrony, like
emission-ness, is a declared property consumers read off the service",
async-extern.md §3):

1. **Async without `await` is refused.** An `effect` acquisition, an `emit`
   step, an if-guard, or a `fail` message whose expression reaches a
   suspension source, with no `await` marker on the step, is a compile error
   naming the one-token fix:

   > component `PgDatabase` acquires through async operation `pool_open` but
   > the effect is not awaited; the binding would hold the in-flight value,
   > not the result (A1).
   > hint: write `effect await pool_open(...) undo ...`; the await is a
   > divert boundary (paper §4.3.2), so it is spelled, never inserted.
   > code=A1, category=async-propagation

   Mechanically: `_reached_async_req_ops` (lower.py:3899) starts running over
   activation step expressions, closing the probe table's silent column, and
   `_async_reached_outside_provide` (lower.py:3854) stops treating an
   await-marked step's expression as "outside" a suspension window. Both are
   walks that already exist; the change is where they point.

2. **`await` without async is refused.** `effect await` on a sync
   acquisition, or `await emit` on a sync emission op, errors ("nothing here
   suspends; drop `await`"). This is the same asymmetry `compatible` enforces
   for `Async[T]` types and keeps the marker meaningful: an `await` in an
   activation body is a real divert window, never decoration.

3. **Teardown never suspends.** `undo` and `compensate` slots reaching a
   req-target async op become compile errors, joining the existing rule that
   those slots may not call an async extern (async-extern.md §1). The message
   states the reason once: the two-phase abort loop is synchronous on every
   tier (docs/design/teardown-contract.md, the bound rule), and a suspension
   there would be a teardown that can hang or silently no-op. This closes the
   fourth probe row and is a behavior flip from silent-wrong to error.

4. **The direct-position rule is unchanged elsewhere.** Provide-method
   bodies, timer bodies, pure fns, tests, and lifecycle bodies keep their
   existing admissions and refusals byte-identical. The lower.py:3145 refusal
   text survives for genuinely unawaitable positions (an async reach inside a
   config default, an if-guard's condition), reworded only to name the new
   spellings in its hint.

Behavior flips, named for the record: the two admitted-and-leaky probe rows
(async op in effect bind, async op in bare emit step) and the two teardown
slots flip from compiling to erroring, with no grace period. The precedent is
item 92 judgment call 3, confirmed there: yesterday's compile was a silent
wrong answer at runtime, and the error names a one-token fix.

## 4. The LIFO teardown story (the novelty, and why it needs no new runtime)

G7 (DESIGN.md §4): derived teardown is LIFO-complete over accumulated
effects, by lowering. The question this item must answer: does that survive a
suspension between two registrations, and what does an abort during the
suspension do? The answer falls out of machinery that already ships, and the
"verified" in the roadmap's novelty claim means exit tests pinning each clause
below, not a new mechanism.

The emitted shape on the colored tiers (py shown; ts is the same shape with
`function*` and the fiber accumulator, backends/typescript/emit.py:1685-1708):

```python
async def _body():                       # backends/python/emit.py:1349
    pool = await pool_open(url)          # spelling 1: the acquisition awaits
    yield lambda: pool.close()           #   …then the inverse registers
    await _revl_ctx.audit.record(m)      # spelling 2: the emission awaits
    yield _revl_frame.compensation(...)  #   …then the compensation registers
    await warm_caches(pool)              # spelling 3, exactly today's step
    yield None                           #   iteration boundary (A1)
```

Four clauses, each grounded:

1. **Registration is boundary-atomic.** The runtime consumes the activation
   generator entry by entry; a divert lands only at yields, never inside a
   step (the protocol behind "a divert during the await therefore skips every
   later step", backends/python/emit.py:986-989). The inverse yield is the
   next action after the awaited acquisition resolves, inside the same
   generator step. So there is no observable state in which the acquisition
   happened but its inverse is unregistered: either the await has not
   resolved (no acquisition, nothing to revert) or the entry is on the stack.
   This is the invariant that makes async acquisition sound, and it is
   exactly why the awaited call must be a *step* and not an expression: an
   await buried mid-expression could interleave with other effect-bearing
   subexpressions, and the atomicity argument would need an evaluation-order
   proof per tier instead of a generator step.

2. **Abort during an await is the inertia case, already specified.** Paper
   §4.3.3, quoted in both colored emitters: the await lands, then the
   boundary closes the iteration. A withdrawal requested while the
   acquisition is in flight takes effect at the next boundary; by clause 1
   the entry is registered by then. Teardown replays the whole stack
   newest-first through the per-activation `Frame`, Phase 1 then Phase 2,
   precisely as the teardown contract specifies for any abort. The contract
   needs zero amendment: its loop walks a stack, and the stack does not
   record that a suspension happened between pushes.

3. **A failed async acquisition leaves no residue.** If the awaited call
   raises, the raise propagates out of the generator step before the inverse
   yield; the activation fails (A8), and the accumulated *prefix* reverts
   LIFO. The failed acquisition itself contributed no entry and no value,
   which is the correct accounting: an acquisition that never returned never
   acquired. (Contrast the silent status quo, where the "acquired value" is
   a coroutine object and the registered inverse is garbage.)

4. **Teardown itself never suspends.** Rule 3 of §3 plus the existing extern
   fences mean no undo, no transactional inverse, and no compensation can
   reach a suspension source. The two-phase abort loop therefore stays
   synchronous on every tier, and every bound, preemption, and residue rule
   in the teardown contract applies verbatim. Async effect composition is
   forward-path only, by construction.

And the non-blocking half of the roadmap sentence: while a provider's
activation awaits, its consumers are PENDING under R2, not blocked; the event
loop (py asyncio, ts microtasks) keeps driving other fibers, which is the
same property today's `await Job.run` step already exercises
(examples/migrator.rvl). On the blocking tiers the activation occupies its
goroutine or thread, which is observably equivalent under A1 (ordering within
a task is promised, interleaving is not; async-extern.md §2 family 2).

For item 132 (the mechanized proof): the awaited step is an iteration
boundary of the paper's §4.3 calculus, which the G7/A8 theorems already
quantify over. The one lemma this item adds to 132's queue is clause 1 as a
statement about the lowering: every lowered async acquisition step is
atomic-to-divert with its inverse registration. Recorded there, not proved
here.

## 5. IR and six-tier lowering

IR: one additive key, `"async": true`, on the `effect`/`let-effect`/`emit`
step nodes whose surface carried `await` (the timer precedent, exactly:
`step["async"] = True` at lower.py:4935). The `await` step node is unchanged.
IR version stays 3 by the async-family's standing argument (additive key; the
blocking tiers correctly ignore it; an old colored-tier emitter fails loud at
boot, never silently wrong). Emitter async detection extends from "any await
step" (backends/python/emit.py:1335, backends/typescript/emit.py:1691-1694)
to "any await step or any async-flagged step".

| tier | lowering of the three spellings |
| --- | --- |
| **py** | body already becomes `async def` generator when async steps exist; spelling 1: `bind = await <acquire>` then the existing `yield lambda: undo` (backends/python/emit.py:933-954 gains an `await` prefix off the step flag); spelling 2: `await <expr>` then the existing compensation entry (emit.py:975-984); spelling 3 exists (emit.py:985-990) and only its admission widens |
| **ts** | same three shapes on the `async function*` body (emit at backends/typescript/emit.py:1685-1708; the frame compensation entry at :1455-1463); the await-seed context for activation bodies gains the same sources the method path already awaits |
| **go** | the marker **erases**: acquisitions and emissions stay the blocking calls inside `ctx.Effect` closures they are today (backends/go/emit.py:2040-2056); correct because go service methods and extern bodies are blocking (async-extern.md §2 family 2), so the awaitable the flag tracks never exists |
| **java** | erases, same argument; the compensated-emission tracking (backends/java/emit.py:3272-3277) is untouched; the await step keeps `_await_join` for genuine host futures |
| **rust** | erases, same argument, plus one repair this item owns: the await *step* currently renders `<expr>.await` unconditionally (backends/rust/emit.py:2919-2920), which fails rustc for a req-op await since rust erases method async-ness and `heat()` returns `String`, not a future. The step erases to a plain call for non-host awaitables and keeps `.await` for the `plugin_async` host seam. A latent tier bug the widened admission would make live; fixed in the rust slice |
| **wasm** | refuses the new spellings with the existing honest shape (backends/wasm/emit.py:1108-1120 already refuses every awaitable but `Job.run`); the message gains the effect/emit step cases |

No `backends/*/runtime.*` file changes on any tier. That sentence is the
scope instruction discharged: the async-extern machinery (coloring, seeds,
async frames, the Frame/fiber accumulators) is reused whole, and this item is
a parser production, an admission pass, and per-emitter step prefixes.

## 6. Exit tests

1. **Async acquisition roundtrip (py, ts).** `let c = effect await
   open_conn(u) undo close_conn(c)` with a real async host body; lifecycle
   load, call through the provision, unload; `assert no_residue`; the trace
   shows `close` after `open` (R1). The finding-shaped fixture: the same
   program *without* `await` must fail compile with the §3.1 diagnostic.
2. **Abort during an in-flight acquisition (the novelty pinned).** Component
   acquires A (sync), then `effect await B undo …` where B's host body parks
   on a future the test controls; withdraw the component while B is in
   flight; assert: B lands (inertia), then teardown runs B's inverse before
   A's (LIFO), `no_residue` holds. py asyncio runtime test plus the ts
   vitest twin.
3. **Failed async acquisition.** B's awaited body raises: activation fails,
   A's inverse replays, B contributed no entry; residue empty and the trace
   shows no orphaned acquisition.
4. **Awaited emission with compensation.** `await emit … compensate …`; on
   clean unload the compensation discharges (a5a); on abort every Phase-1
   inverse precedes it (a5b). Both assertions already exist in the TCK for
   the sync spelling; the fixture is the awaited variant, and the contract's
   two-phase loop must not change to pass it.
5. **Refusal sweep.** Async-without-await in effect and emit positions; await
   on a sync acquisition; async reach in `undo`/`compensate` slots (the two
   silent holes, now errors); block-effect `await` fence; wasm refusal text;
   rust req-op await step compiles post-repair.
6. **Non-blocking activation.** Two independent components; one's activation
   parks on an awaited acquisition; the other activates and serves calls
   meanwhile; the parked one completes and both unload clean.
7. **Byte-identity.** Every program not using the new spellings emits
   byte-identically on all six tiers. Per-backend golden suites, run
   per-backend; `pytest tests/` alone does not run them (the standing wave
   gap).

## 7. Slices and effort

| # | slice | files | depends |
| --- | --- | --- | --- |
| 1 | parser: the two productions, AST carries the flag | src/revl/parser.py | - |
| 2 | admission + IR: §3 rules, step `async` flags, refusal fixtures under examples/rejections/ | src/revl/lower.py | 1 |
| 3 | py emitter + runtime tests (exit tests 1-4, 6 on py) | backends/python/emit.py, tests | 2 |
| 4 | ts emitter + vitest/tsc twins | backends/typescript/emit.py | 2, parallel with 3 |
| 5 | erasure tiers + wasm refusal + the rust await-step repair | backends/{go,java,rust,wasm}/emit.py | 2, parallel with 3/4 |
| 6 | docs (syntax-2.0 §4 paragraph, backend-ir step-flag line, async-extern.md cross-ref) + golden sweep + roadmap | docs/, goldens | 3-5 |

Estimate: **~2 weeks**, the low end of the roadmap's 2-3 wk, and the scoping
is why: the hard thing the roadmap names (async with verified LIFO teardown)
turns out to be an *argument plus exit tests* over machinery that ships, not
a runtime build. The genuinely new code is one admission pass and three small
emitter prefixes; the risk concentrates in slice 2 (the exact-pairing rules
touching the same walks four prior items tuned; the existing async test files
must stay green unmodified) and in the tier-4 golden discipline. If slice 2
uncovers interaction debt with the item-92 arrow walks, the estimate moves to
the roadmap's 3 wk ceiling, not past it.

## Judgment calls a human should confirm before implementation

1. **`await emit` vs `emit await`.** This design puts the boundary marker
   outermost (§2). The alternative reads better aloud but splits the
   statement head `emit` from its G4 role. Confirm the spelling.
2. **The behavior flips land with no grace period** (§3): four
   silently-wrong-at-runtime shapes become compile errors. Recommended yes,
   on the item-92 precedent; the sweep in slice 2 should still grep the
   corpus (examples/, dogfood/, tck/) for the shapes first.
3. **Exact pairing in both directions** (§3.2): refusing `await` on a sync
   acquisition costs authors an edit when a service op later drops its
   `async`. The alternative (tolerate redundant `await`) makes the marker
   decorative. Recommended strict; the admission gate already treats an
   async flip as a breaking change to a service (src/revl/admission.py).
4. **The rust await-step repair rides this item** (§5) rather than being
   filed separately. It is three lines in the rust slice and the tier table
   is incoherent without it; confirm the bundling.
5. **Block-effect `await` stays fenced** (§2) with the hoist-into-a-colored-fn
   hint. Lifting it means awaits interleaved with pure setup steps inside one
   acquisition, which reopens the clause-1 atomicity argument; confirm the
   fence.

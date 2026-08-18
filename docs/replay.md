# Backwards replay

*Time-travel over the effect accumulator — and a precise account of what it
refuses to promise.*

Implementation: `backends/python/replay.py` (the engine),
`src/revl/mcp/session.py` (the live session), `src/revl/run.py` (`--record`
and the REPL commands), `tests/test_replay.py`.

---

## 1. Why this exists here and nowhere else

A conventional debugger can step forwards. Stepping *backwards* normally
requires either a recording of every memory write, or a language that makes
every operation invertible by construction. Both are expensive, and neither
is something a runtime just happens to have lying around.

revl does have it lying around. Under Cordis every effect is registered with
its inverse (G4: **inverse-or-emit** — an effect either declares how to undo
itself or is classified as an emission), and the runtime keeps those inverses
in an accumulator that unwinds LIFO on teardown or failure (G7). So at any
instant, a live component is already holding:

* every effect that has run, **in order**,
* the inverse registered for each one,
* and, because the compiler separates them, an explicit record of which steps
  are *not* invertible.

Teardown consumes that accumulator all the way down. Backwards replay
consumes it **partially** — down to an arbitrary step — and then stops,
leaving the component alive and inspectable rather than dead. That is the
entire idea. The paradigm did the hard part; this is a viewport onto it.

## 2. The model

An activation is recorded as a **timeline**: an ordered list of steps, one per
accumulator event, plus the emissions that crossed the boundary in between.

| kind | what it is | has an inverse? |
| --- | --- | --- |
| `effect` | an author-declared effect (`effect … undo …`, `let … = effect … undo …`) | yes — the `undo` |
| `provision` | `provide k: S` — the runtime-derived withdrawal (R5) | yes |
| `emission` | a call to an `emission fn` — a one-way boundary crossing | **no** |
| `compensation` | an A5 `compensate` clause registered for an emission | it runs, but see §4.2 |
| `boundary` | an A1 iteration boundary (`await`) | nothing accumulated |
| `hinge` | `Frame.drain`, the runtime's adopted-effect scaffolding | not an author step |

Each step carries its index, its label, the emitted source line it came from,
and the *origin* that produced it — either the activation body or a specific
provided-service call. That origin is what makes forward replay expressible
at all (§5).

Four operations:

* **`timeline`** — the whole recording.
* **`inspect(k)`** — what the composition looks like at step `k`: active
  provisions, the accumulator still standing at or below `k` (newest first,
  i.e. the order a teardown would use), what has already been unwound, what
  lies ahead, and the emissions that happened at or before `k`.
* **`step_back(k)`** — run the registered inverses from the top of the
  accumulator down to `k`. **Nothing is disposed.** No fiber is torn down; the
  component stays live, its surviving provisions stay callable, and the
  accumulator can grow again afterwards.
* **`replay_forward(k)`** — re-run the tail after `k` by re-invoking the calls
  that produced it.

## 3. What it guarantees

Exactly one sentence, and it is in the code as `replay.GUARANTEE`, returned
verbatim on every report:

> the inverses registered for the unwound steps ran, newest first (LIFO).
> Whether that restores state is the application's own equivalence, not
> something the runtime observes or asserts.

That is the whole claim. There is deliberately no `restored: true` field, no
`stateEqual`, no snapshot comparison — a test in `tests/test_replay.py`
asserts that no key of any step-back report even contains the substring
`restor`, so the API cannot quietly grow one.

Two mechanical properties come with it, and those *are* guaranteed:

1. **Order.** Inverses run newest-first, in exact reverse registration order,
   spanning both activation-time effects and effects adopted later by
   provide-method calls. This is the same order G7 uses.
2. **Once-only.** Each inverse is wrapped in a single-flight cell that is
   shared between the replay engine and the runtime's own teardown. Whichever
   runs it first wins; the other is a no-op. A stepped-back `store.drop()`
   therefore cannot become a use-after-free when the session later unloads,
   and the timeline records *who* ran each inverse (`step_back` or `runtime`).

## 4. What it cannot promise

This section is the point of the document.

### 4.1 An inverse is not an undo of the world

`undo` is an expression the *author* wrote. The runtime runs it; it does not
verify it. `undo store.remove(key)` restores the map to a state the
application calls equivalent — but if something else read that key in
between, or the value was a mutable object handed out to a caller, or the
inverse is `pool.close()` on a pool whose connections were observed, then
"restored" is a claim about the application's own notion of equivalence, not
about program state.

Consequently: **stepping back to step `k` does not put the program in the
state it was in at step `k`.** It puts the program in the state that running
those inverses, in that order, produces. Under a correct set of inverses those
coincide. Verifying that they coincide is out of scope for the runtime, and
this tool never asserts it.

Practically, the things it will *not* catch:

* an `undo` that is wrong, partial, or a no-op;
* state reachable through a reference the component handed out;
* anything outside the process (files, sockets, other services) that an
  `acquire`-classified extern touched;
* time — a value derived from a clock does not come back.

### 4.2 Compensation is not inversion

Paper §6.1. An A5 `compensate` clause is *a second boundary crossing chosen to
offset the first*, not an inverse of it. `emit db.execute("INSERT …")
compensate db.execute("DELETE …")` issues a delete; it does not un-issue the
insert. Anything downstream that observed the insert — a trigger, a replica, a
webhook, a human — has already observed it.

The engine keeps these apart everywhere:

* a compensation is its own step kind, never reported as an `effect`;
* a step-back report separates `compensationsRan` from `inversesRan`, and
  separates `emissionsCompensated` (crossed, and something was done about it)
  from `emissionsCrossed` (crossed, bare);
* whenever a compensation runs, the report carries
  `warning: compensation is not inversion (paper §6.1) …`;
* and because a compensation is itself a boundary crossing, **running one
  appends a new `emission` step to the timeline**. Stepping back does not
  shrink the emission record; it grows it. `tests/test_replay.py` pins this.

### 4.3 Emissions are irreversible by definition

That is what the classification *means*. G4 forces every effect to be either
invertible or declared an emission, so an emission in the timeline is not a
gap in the recording — it is the compiler telling you, ahead of time, exactly
where the accumulator ends.

`step_back` therefore **refuses by default** when the range it would unwind
contains an emission with no compensation:

```
UserCache: stepping back to 2 crosses 1 uncompensated emission(s)
(db.execute). An emission cannot be undone — declare a `compensate` for it
(A5), or pass force=true to unwind the rest anyway and be told what was
crossed.
```

**Why refuse rather than skip.** Both are defensible, and skipping-with-a-flag
is the easier sell. The refusal wins on one argument: a step-back whose
report says "unwound to step 2" while an `INSERT` it stepped over is still
committed has told the reader something false, and flags are read after the
headline, not before. Making the caller pass `force` moves the irreversibility
from a footnote into a decision. The forced report is then explicit — it lists
every crossing under `emissionsCrossed` with
`warning_emissions: N emission(s) were crossed with no compensation. Those
effects are still out in the world.` The refusal is also *total*: when it
refuses, nothing runs at all, so there is no partially-unwound state to reason
about.

### 4.4 Forward replay re-runs calls; it does not resurrect state

`replay_forward(k)` re-invokes the *provided-service calls* whose effects
appear after `k`, in original order, deduplicated. It re-executes work. It
does not restore the state that work produced the first time — if the call is
non-deterministic, the second run is a second run.

And it is **not able to replay activation-body steps at all.** The emitted
component body compiles to a single generator (see `runtime.Frame` and
`backends/python/emit.py`); its yields are the accumulated inverses, and there
is no way to re-enter that generator at step 4 without re-running steps 0–3.
So an activation-body step in a forward plan is reported under
`notReplayable`, with the reason, rather than silently omitted or faked:

```json
{"step": 0, "label": "N/body/effect",
 "reason": "activation-body step — the component body is one generator; its
            tail cannot be re-entered without re-running its head. Reload the
            component instead."}
```

The honest remedy for those is a reload — `revl_swap` with the fixed source,
which is a full generation transition the session already supports and which
*does* re-run activation from the top.

### 4.5 Recording is not free, and it is not retroactive

Recording is opt-in and must be switched on **at load**: it works by handing
the emitted component body a delegating context in place of the real one, and
a fiber's context chain is fixed at plugin time. There is no way in
afterwards, and the session says so rather than pretending
(`revl_timeline` on a non-recorded session returns an error naming
`record: true`).

With recording off, nothing in this document is in the path at all.

## 5. How the recording is obtained

**Emitted output is not changed.** The golden files and the cross-tier emit
matrix stay byte-identical; no emitter was touched. Recording is a wrapper
installed around each component's `apply`, which passes the emitted body a
`_RecordingContext` that delegates every call to the real context and observes
the accumulator on the way through:

| emitted shape | what the recorder sees |
| --- | --- |
| `frame.install(_body)` | `ctx.effect(_body, 'C/body')` |
| `yield lambda: undo()` | a yield out of that generator |
| `yield ctx.provide('k')` | `ctx.provide` **and** its own yield |
| `yield frame.drain` | the accumulator hinge |
| `yield None` | the A1 iteration boundary |
| `frame.acquire(label, …)` | `ctx.effect(_setup, label)` |
| `frame.adopt(ctx.effect(…))` | `ctx.effect(fn, label)` |
| `ctx.db.execute(sql)` | an **emission** |

Two classifications are worth stating plainly, because they are assumptions
about the *emitter's shape* rather than facts about the runtime:

1. **Provisions are identified by object identity** — the disposer the
   recorder handed back from `ctx.provide(key)` is the object the body yields
   next. No guessing.
2. **Compensations are identified by source adjacency** — the emitter writes
   an `emit` step's call and its `yield lambda: <compensate>` on consecutive
   lines, so an inverse whose first line is exactly one past a recorded
   emission's call site is that emission's compensation.

`tests/test_replay.py` pins both against the real emitter. A
misclassification of (2) would mislabel a compensation as an ordinary inverse
in the *report*; it cannot make an un-runnable thing run.

Which methods are emissions comes from the compiler — the IR's `emission`
flag — not from a heuristic. Same direction of trust `revl mcp` uses to derive
MCP tool annotations: the language checked it, so the tool can rely on it.

**Known limit.** Emissions performed by a host `extern emission fn` are called
as free functions in emitted code, not through the context, so they do not
appear as `emission` steps. If such an extern declares a `compensate`, the
compensation *is* accumulated and does appear — classified as an ordinary
inverse, because there is no recorded emission adjacent to it. Service-level
emissions, which is what the IR and every tier model first-class, are complete.

## 6. Using it

### From MCP

```
revl_load           { source, record: true }
revl_call           { key, method, args }        # accumulate some work
revl_timeline       { component? }               # the recording
revl_inspect_step   { component?, at }           # the composition at step k
revl_step_back      { component?, to, force? }   # unwind, stay live
revl_replay_forward { component?, from }         # re-run the tail
```

`revl_step_back` and `revl_replay_forward` are annotated `destructiveHint:
true` — they run real inverses and real calls against a live system.
`revl_timeline` and `revl_inspect_step` are `readOnlyHint: true`.

A refused unwind comes back as `{ok: false, refused: true}` with the
diagnostic above — a result, not a crash.

### From the CLI

```
revl run app.rvl --record
revl> :timeline
revl> :inspect 2
revl> :back 2
revl> :back 2 !        # force across uncompensated emissions
revl> :forward 2
```

## 7. Status

The engine is python-tier only, which is where the reference runtime lives.

`tests/test_replay.py` has 39 tests in two layers.

**28 against a stub context.** These drive the pipeline — revl source →
frontend IR → the cordis-py emitter → the recorder → the timeline → step-back
and forward replay — against `FakeContext`, a stand-in implementing the slice
of the cordis context protocol an emitted component actually uses: an effect's
generator runs to completion at registration, its yielded disposers run LIFO
within the effect, and disposal is single-flight. They use the real
`runtime.Map`/`Pool` host builtins and assert against the real trace. They run
on any interpreter, with or without a runtime installed.

**11 against real cordis-py**, through the production path — `revl.mcp.Session`,
a real `cordis.Context`, real fibers. They are marked `@needs_cordis` and skip
where the runtime is absent. To run them:

```
backends/python/.venv/bin/python -m pytest tests/test_replay.py -q
```

What that layer establishes, observed rather than argued:

* the classification is not an artifact of the stub — a **real** `ctx.provide`
  disposer is still identified as a provision, by object identity;
* step-back restores the state its inverses guard, and leaves the fiber
  `ACTIVE` and the service callable — withdrawn is not disposed;
* **`unload` after a step-back still reports `noResidue: true`** on all four
  checks. This is the sharpest result: the once-only inverse is shared with
  the real fiber's teardown, so replaying `store.drop()` early neither
  double-frees it nor causes the runtime to skip anything else. R4 survives
  time travel;
* the emission refusal, the forced crossing, and compensation-appends-an-
  emission all behave as documented against a real `Database` provider;
* an `await` body — which compiles to an **async generator**, the riskiest
  thing the recorder re-wraps — records its A1 boundary with the fiber
  reaching `ACTIVE` and unwinding cleanly.

**Recording is observationally neutral on the failure path.** Two tests load a
component whose acquisition refuses (`boom://`) with recording off and on, and
assert the fiber state and residue verdict are identical. For a *sync* body
that state is `FAILED`, which is A8 as specified. For an *async* body it is
not: cordis-py routes an async body's mid-body failure to the effect guard
rather than the fiber's error slot, so inverses run LIFO with no residue but
the fiber stays `ACTIVE` instead of landing `FAILED`. That is a **cordis-py
gap, not a replay gap** — it reproduces identically with recording off, and
was found independently by the fault-injection work. The test deliberately
asserts *neutrality* rather than the state itself, so it keeps passing when
cordis-py fixes it.

A related nicety: the timeline is a useful witness of a failed activation. For
the sync failure it shows exactly one accumulated step, `undone: true`,
`undoneBy: "runtime"` — the partial unwind, recorded.

What is still **not** covered here: multi-generation replay across a
`revl_swap` (the recorder starts fresh timelines per generation, which is
tested, but stepping back into a superseded generation is not a thing this
supports); and the `extern emission` limit in §5. Other tiers (TS, rust, java,
wasm) accumulate the same inverses in the same order; nothing here has been
ported to them.

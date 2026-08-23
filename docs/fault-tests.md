# Fault tests

A `fault test` is a component author's way of asserting the paradigm's own
failure guarantee, in source, next to the component it is about.

```revl fragment
fault test "db dies mid-activation" for Store {
  fail at step 3
  assert failed
  assert no residue
  assert inverses lifo
  assert siblings unaffected
}
```

Before this existed, the L-Raise reading (IR contract amendment **A8**) and
**R4** ("unloading leaves no residue") were checked by hand-written scenario
scripts, one per backend — `backends/python/tests/test_v1_semantics.py`'s
`test_a8_mid_body_failure_reverts_and_contains`, the `boom://` refusal hook in
`backends/python/runtime.py`, `backends/rust/scenarios/`, and friends. Those
prove the property for the components *the scenario author wrote*. A `fault
test` proves it for **your** component, and the compiler and runner do the
bookkeeping.

---

## 1. Syntax

```
fault-test  := 'fault' 'test' STRING 'for' IDENT [ 'with' '{' field* '}' ] '{' clause* '}'
field       := IDENT ':' literal [',']
clause      := 'fail' 'at' ( 'step' INT | 'effect' (IDENT | STRING) )
             | 'assert' assertion
assertion   := 'failed'
             | 'no' 'residue'
             | 'no' 'emissions'
             | 'inverses' 'lifo'
             | 'siblings' 'unaffected'
```

Exactly one `fail at …` and at least one `assert …`; both are compile errors
otherwise. `with { … }` supplies the component's config for the activation
under test (literals only), and is checked against the component's declared
config fields at compile time.

**No new reserved word.** `fault` is a *contextual* keyword — it heads a
declaration only when the very next token is `test`. `for` and `with` are
existing keywords; `at`, `step`, `no`, `residue`, `emissions`, `inverses`,
`lifo`, `failed`, `siblings` and `unaffected` are matched by spelling as plain
identifiers. A program that already uses any of those names keeps working, and
`src/revl/lexer.py` (and the self-hosted lexer that mirrors it) is untouched.

### 1.1 The injection scheme, and why

Two spellings, one primitive:

| spelling | means |
|---|---|
| `fail at step N` | the activation dies **instead of** body step `N` (1-based) |
| `fail at effect X` | the activation dies instead of the step that binds `let X = effect …` |

**The step index is the primitive; the name is sugar resolved at lowering.**
An index is *total* — every body step has one, including `emit`, `provide`,
`await` and unbound `effect { … }` blocks — while a name only exists for
`let NAME = effect … undo …`. Reducing to one coordinate means every backend,
the runner and every diagnostic see a single scheme, so there is no second
addressing mode to keep consistent.

A name is nonetheless the better thing to *write*: it survives inserting a step
above it. So both are kept, `fail at effect X` lowers to `{"step": N,
"effect": "X"}`, and the diagnostics always print the pair
(`Store dies at step 2 (effect \`pool\`)`).

Semantics of "instead of": steps `1 … N-1` completed and accumulated their
inverses; step `N` and everything after it never runs. `fail at step 1` is the
degenerate case — nothing accumulated, nothing to revert.

Compile-time errors: unknown component, `N` past the end of the body, an
`effect` name that is not a `let … effect` binding in that component, a config
field the component does not declare, a duplicate fault-test name.

---

## 2. Mechanism

The harness (`src/revl/fault.py`) does not simulate anything.

1. **Splice.** Deep-copy the IR and insert an IR `fail` step at index `N-1` of
   the target component's body. `fail` is a step the frontend and all six
   backends already carry — it is how an author writes a *deliberate* L-Raise
   — so a fault test drives exactly the machinery the hand-written A8 scenarios
   drive. This is the "reuse, don't invent" call the design note asked for.
2. **Emit and load.** Emit that mutated IR with the ordinary cordis-py backend
   and exec it. Load every *other* component in manifest load order first, so
   the target activates against its real providers, not stubs.
3. **Snapshot.** Record the runtime baseline — registry size, provisions,
   root-fiber disposables, event-hook counts — the same four introspections
   `revl run` prints on teardown.
4. **Arm and activate.** `runtime.arm_fault_probe(<component>)` installs a
   `FaultProbe`; `Frame.install` then wraps the body generator so every
   yielded disposer is tagged with its accumulation index and records when it
   runs. Plug the target. The failure fires, the runtime unwinds.
5. **Interrogate.** Fiber state, sibling states, the two orders from the probe,
   and the snapshot deltas.
6. **Settle.** Dispose the failed handle (the host's job) and snapshot again.

### 2.1 Why the probe, and not the host trace

`backends/python/runtime.py` already records every stub-builtin operation, and
the existing A8 scenarios assert on that trace. A trace is not enough here: a
provision withdrawal (`yield ctx.provide('cache')`) and any inverse that is a
pure closure produce **no trace event at all** — and those are exactly the
inverses an L-Raise regression drops. The probe observes the accumulator
itself, so "3 inverses accumulated, 3 ran, newest first" is a measurement, not
an inference.

The probe is protocol-faithful: cordis iterates an effect body with a plain
`for` / `async for` and never `send`s or `throw`s into it (see its fiber
`_execute`), so a re-yielding wrapper is transparent. Only the named component
is instrumented; siblings run untouched.

---

## 3. What each assertion actually checks

| assertion | check |
|---|---|
| `assert failed` | the target fiber's state is `FiberState.FAILED` |
| `assert no residue` | (a) every accumulated inverse ran; (b) no provision the target added survived the unwind; (c) event-hook counts back to baseline; (d) after the host disposes the failed handle, registry size and root disposables are back to baseline exactly |
| `assert inverses lifo` | the recorded run order is exactly the reverse of the recorded accumulation order |
| `assert no emissions` | the activation performed no `emit` before the injection point |
| `assert siblings unaffected` | every other component in the composition is still `ACTIVE` |

`no residue` is checked in two phases on purpose. Immediately after the unwind
the failed fiber is **still registered** — that registration *is* A8's "the
component lands FAILED with the error recorded", and it is a handle the host
owns, not residue. So provisions and listeners are checked there, and registry
and effects are checked after the harness disposes the handle, which is what a
host does with a failed plugin. The harness always prints the registry
delta so the distinction is visible rather than assumed.

---

## 4. Emissions are reported, never reverted

An emission is irreversible by construction (syntax-2.0 §6.1): it has left the
component. `no residue` says nothing about it, and the harness will not let a
green fault test imply otherwise.

Every fault test — passing or failing — prints one `note:` line per emission
that ran before the injection point:

```
PASS db dies mid-activation [Store dies at step 3]
    note: irreversible: step 2: emit log.write('store coming up') — no inverse
          exists for an emission; it was NOT reverted by the unwind
```

If the `emit` carried a `compensate` (A5), the note says so and still refuses
the word "reverted":

```
    note: irreversible: step 2: emit bus.publish(…) — its `compensate` ran, but
          the emission itself stands (compensation is not inversion)
```

An emission inside an `if` before the injection point is reported as
`(conditional — inside an \`if\`, may not have run)`. The harness reads
emissions statically from the body prefix, so a conditional one cannot be
resolved without executing the predicate; saying "may" is the honest answer.

`assert no emissions` is the way to *require* that the chosen failure point is
upstream of every emission.

---

## 5. What a passing fault test proves — and what it assumes

**Proves**, for this component, on the cordis-py reference tier, at this one
injection point:

- every inverse the activation accumulated before the failure point ran, and
  ran newest-first;
- no provision it installed survived, and no event hook it added survived;
- once the host dropped the failed handle, the runtime was byte-for-byte back
  at the pre-activation baseline on all four introspections;
- (with `assert failed`) the failure became a fiber-level L-Raise rather than
  being swallowed;
- (with `assert siblings unaffected`) no other component in the composition
  changed state.

**Assumes / does not cover:**

- **One tier.** cordis-py only. A green fault test says nothing about cordis
  (TS), cordis-rs, cordis4j or wasm — those tiers *refuse* the construct
  (§6). Cross-tier L-Raise remains the job of the per-backend scenario suites.
- **One point.** `fail at step 3` proves step 3. Steps 2 and 4 are separate
  fault tests. There is no "all points" sweep.
- **Total steps only.** The injection point addresses top-level body steps. A
  step nested inside an `if` is not directly addressable, and when the prefix
  before the injection point contains an `if`, inverse labels degrade to
  ordinals (`inverse #2`) rather than claiming a source position that may not
  have executed.
- **Synchronous inverses observed synchronously.** The probe records the moment
  a disposer is *called*. If an inverse returns an awaitable that the runtime
  gathers, "ran" means "was invoked", not "completed".
- **Residue as the runtime can see it.** Registry, provisions, disposables and
  hooks. A component that leaks something the runtime does not track — a file
  descriptor, a row in a real database — leaks it silently. The probe's
  "every accumulated inverse ran" clause is the closest available proxy.
- **The host's stdlib is the stub stdlib.** `Pool`, `Map` and `Job` are the
  deterministic fakes in `backends/python/runtime.py`.

---

## 6. Tiers

`fault test` runs on the **python reference tier only**. The other four
emitters **refuse** a document that carries a `fault_tests` section, by name:

```
fault tests do not lower to the cordis-rs tier ('db dies mid-activation') —
`fault test` runs on the python reference tier only (docs/fault-tests.md).
```

That refusal is the guarantee against a silent mis-emit: a fault test that
quietly disappears is a guarantee nobody is checking. A document carrying fault
tests also lowers as `ir_version 3`, so a consumer that predates the section
rejects the whole document rather than dropping it.

`revl test --backend {ts,rust,java}` is friendlier than the raw emitter: it
strips the section, prints

```
[ts] note: 2 fault test(s) not run on this tier — `fault test` runs on the py
     reference tier only (docs/fault-tests.md)
```

and still runs the document's ordinary `test` blocks on that tier, so the two
kinds of test do not take each other down. The py tier lowers fault tests into
the emitted module as a `REVL_FAULT_TESTS` manifest, so an emitted module is
self-describing.

`revl test` on the py tier needs the cordis-py runtime, because a fault test
activates something for real. When it is absent the fault tests are reported as
**skipped, with the reason** — never as passed.

---

## 7. Reading a failure

A fault test never fails with a bare assertion. Every failure names the thing:

```
FAIL cache survives a dead pool [UserCache dies at step 3 (effect `pool`)]
    - residue in the service registry: provision `cache` survived the unwind
      (baseline provisions: `db`)
    - residue in the host: 1 of 3 accumulated inverse(s) never ran —
      step 1 (undo of effect `scratch`)
```

```
FAIL store unwinds in order [Store dies at step 4]
    - inverses ran out of LIFO order: unwind position 1 ran step 1 (undo of
      effect `scratch`), expected step 3 (undo of effect `pool`) —
      accumulation order was [step 1 (undo of effect `scratch`),
      step 3 (undo of effect `pool`)]
```

---

## 8. Divergences found by this feature

**cordis-py: an `await` in the body defeated A8's "lands FAILED" — RESOLVED.**

A component body containing an `await` step compiles to an *async* generator
(A1's iteration boundary). cordis-py *used to* run an async effect setup as a
task and route its failure to `_make_effect_guard`, which auto-disposes the
effect — the accumulated inverses ran, newest first, with no residue — but the
exception never reached the fiber's error slot, so the fiber stayed `ACTIVE`
instead of landing `FAILED`. A synchronous body raised straight out of `apply`
into `_start_reload`'s handler and landed `FAILED` correctly.

The first `fault test` written against an `await`-containing component found
this, reproduced with no revl code in the loop (a hand-built IR with
`let-effect` / `await` / `fail`, emitted and plugged directly, ended `ACTIVE`
with `map.drop` in the trace).

**Fixed upstream** in the pinned runtime
(`inso1337/cordis-py@harden-fiber-lifecycle` commit `1316174`, folded into
geohotstan/cordis-py#1): an async setup failure is now routed to the fiber's
error slot exactly as the sync path is, so the fiber lands `FAILED` with the
error recorded while the inverses still run LIFO. `assert failed` now holds on
an `await`-containing component, and
`test_fault_tests.py::test_an_await_body_lands_failed_like_a_sync_body` pins
it. See `docs/contract-errata.md` (entry now marked RESOLVED).

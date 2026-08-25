# Time as a coeffect

*Roadmap item 57 (timers as revertible coeffects); item 102 (the `advance`
lifecycle statement). Reference tiers: Python + TypeScript.*

Periodic and delayed work — a heartbeat, a warm-up, a retry sweep — has always
lived in host `setInterval`/`time.Timer` loops, *outside* every guarantee revl
makes. An interval armed in a host loop is invisible to the effect ledger: it is
not reverted when its component unloads, its firing body is not part of the
capability audit, and under replay it races the wall clock. Item 57 pulls timers
inside the language and makes each one a **textbook revertible effect**.

```revl
service Log { emission fn write(msg: Str) }

component Heartbeat requires log: Log {
  every 30s {
    emit log.write("tick")
  }
  after 5m {
    emit log.write("warmup done")
  }
}
```

`every <delay> { … }` is a periodic timer; `after <delay> { … }` fires once. The
delay is `<n><unit>` with no space — `250ms`, `30s`, `5m`, `1h`, `1d` — and the
body is one or more `emit` statements (see [Scope](#scope-first-slice)).

## The three properties

### 1. A timer is a revertible schedule

Arming a timer **acquires a schedule whose inverse is cancellation** — derived
teardown, exactly like every other effect. There is no hand-written cleanup: the
emitter yields `cancel()` into the same per-effect LIFO disposer stack that
reverts a `let-effect` acquisition, so unloading the component (or its enclosing
frame) provably cancels its timers. No interval outlives the activation that
armed it — the leak the residue probe (item 18) hunts cannot occur here.

In IR the construct lowers to an additive body step (`ir_version` stays 3):

```json
{"step": "timer", "mode": "every", "interval_ms": 30000,
 "body": [{"step": "emit", "expr": { … log.write("tick") … }}]}
```

The step carries no `undo` slot. Crossing *time* is not crossing the *system
boundary*, so the schedule is not itself an emission; its inverse is the
runtime's own `cancel`, derived like `ctx.provide`'s withdrawal (R5). The
emitted Python is the whole story:

```python
def _timer_1():
    ctx.log.write('tick')
_timer_1_h = schedule_every(30000, _timer_1)
yield lambda: _timer_1_h.cancel()          # the derived inverse
```

and the TypeScript is its mirror (`host.scheduleEvery(30000, …)` /
`yield () => $revl_timer_1_h.cancel()`).

Residue-freedom rides the *same* machinery a leaked `Pool` does: a timer traces
`timer#N.schedule` when armed and `timer#N.cancel` when reverted, and
`schedule → cancel` is registered in the runtime's acquire/release table
(`_LIFECYCLE_ACQUIRE` on Python, `liveResources` on TypeScript). An `every`
timer left uncancelled at teardown shows up as residue through the exact R1/R4
introspection that catches an open connection pool.

### 2. The firing body is audited (G4/G8)

A timer body **runs at activation-time stratum with the component's declared
capabilities**. It lowers through the same `emit` machinery and the same
component environment as a top-level emission, which buys the audit for free:

* a firing that reaches a service the component does not `require` is **refused**
  (G1/G4) exactly as a bare `emit` to that service would be — a timer cannot
  smuggle a boundary crossing past its declaration;
* the boundaries a firing *does* cross are **component reach**: the capability
  analysis (`_collect_emit_caps`) and every query surface (`revl audit`,
  `revl query emitters`) recurse into the timer body, so scheduled reach appears
  on the G8 surface like any other reach.

```
$ revl audit examples/heartbeat.rvl
component Heartbeat  (examples/heartbeat.rvl)
  requires: log
  boundary: emissions: log.write (0 compensated); capabilities: *
```

The `log.write` on that line is the timer's firing — audited, not hidden.

### 3. The clock is a coeffect (deterministic replay)

Under `revl test`/replay the **clock is a coeffect the harness provides**. Time
does not pass on its own: `Clock.now()` moves only when something calls
`Clock.advance(ms)`. A firing is therefore a **step in the timeline**, not a
wall-clock race — which is what lets a test assert "fires on the 3rd tick" and
lets the fault sweep (item 30) inject "fail at the third firing".

```python
seen = []
schedule_every(10, lambda: seen.append(Clock.now()))
assert Clock.now() == 0 and seen == []   # nothing fires unbidden
fired = Clock.advance(35)                 # the harness injects 35ms
assert fired == 3 and seen == [10, 20, 30]
assert Clock.firings()[2] == (1, 30)      # the 3rd firing, at 30ms
```

`Clock.advance` fires every due timer earliest-first, ties broken by arm order,
re-arming `every` timers across the whole span — a total, reproducible ordering
across any number of interleaved timers. A production driver would pump `advance`
from a real monotonic source; the reference tier keeps it explicit so tests are
deterministic. The Python (`backends/python/runtime.py`) and TypeScript
(`backends/typescript/runtime.ts`) clocks agree tick-for-tick.

### The `advance` lifecycle statement (item 102)

Property 3 above is driven by a *host* call to `Clock.advance`. Item 57 left a
gap: that call was not expressible **in the language**, so a `lifecycle test`
(syntax-2.0 §7.1) could prove a timer's *cancellation* (property 1) but never
its *firing* — `asyncio.sleep` cannot help, because the clock never moves on its
own. A revl author could not write "fires on the 3rd tick" as a test.

Item 102 adds the `advance <n><unit>` lifecycle statement. It is the only
statement that moves the clock coeffect, and it reuses item 57's duration units
(`ms`/`s`/`m`/`h`/`d`). After an `advance`, every due timer fires as a
deterministic timeline step, so the firing is observable through a plain `call`:

```revl
service Counter { fn count() -> Int  emission fn tick() }

component TickCounter provides counter: Counter {
  let store = effect Map.new() undo store.drop()
  provide counter {
    fn count() = store.size()
    fn tick() {                       // one distinct entry per firing
      let key = `tick-${store.size()}`
      effect store.insert(key, "fired")
      undo   store.remove(key)
    }
  }
}
component Heartbeat requires counter: Counter { every 10s { emit counter.tick() } }

lifecycle test "an every-timer fires on each advanced tick" {
  load TickCounter
  load Heartbeat
  advance 35s
  let ticks = call counter.count()
  assert ticks == 3                   // fired at 10s, 20s, 30s — the 3rd tick
  unload Heartbeat                    // cancellation is the schedule's inverse
  advance 100s
  let settled = call counter.count()
  assert settled == 3                 // no orphaned firing after teardown
  unload TickCounter
  assert no_residue
}
```

It lowers to an additive lifecycle step, `{"step": "advance", "ms": 35000}`,
which the reference emitters render against the same clock coeffect the timer
armed:

* **Python** — `Clock.advance(35000)`;
* **TypeScript** — `host.clockAdvance(35000)`.

A lifecycle test that advances the clock is reset to `t=0` on entry
(`Clock.reset()` / `host.clockReset()`) so its timeline is independent of any
earlier test in the file; a lifecycle test with no `advance` is byte-identical
to its pre-item-102 output (the `Clock` import and reset appear only when
needed). The pinned exit test is `examples/lifecycle_timer.rvl`, run on both
reference tiers by `tests/test_time_coeffect.py`.

`advance` is a *lifecycle* statement only — legal inside a `lifecycle test`
body, an ordinary identifier everywhere else, exactly like `load`/`unload`/`call`.

## Async timer bodies — the in-flight window (item 170)

Item 57's timer bodies could reach a required service only **synchronously**
(G4): a firing had no async colour, so a scheduled automation that fires an
`emission async fn` — the harness's `every 60s { emit agent.run_in("cron",
brief) }` — was unexpressible. Item 170 gives a timer body the same `Async[T]`
in-flight window (item 106) an async provide method gets.

**Frontend (admission + colouring).** A timer body that reaches an async op —
a req-target async service operation (`emit agent.run_in(...)`), an async
extern, or a phase-2 colored fn — is **ADMITTED and coloured async** rather than
refused. `_async_reached_outside_provide` now prunes `timer` steps exactly as it
prunes `provide` steps (both get their own in-flight window), and
`_lower_timer_step` stamps `"async": true` on the lowered `timer` step when
`_timer_body_reaches_async` fires (the same req-async-op + async-callable reach
`_arrow_reaches_async` uses). A sync timer body carries no `async` key and is
byte-identical to before.

**Runtime + py emit (await on tick, cancel in flight).** A firing is a
deterministic timeline step driven by the clock coeffect, and — like an async
provide method — its async work runs in an in-flight window the harness drains.
The py emitter renders an async-coloured timer body so each async emission is
**spawned as a tracked `asyncio` task** (`_revl_asyncio.ensure_future(...)`,
added to a per-timer in-flight set) rather than run inline: the firing returns
immediately, and the lifecycle harness's `_revl_settle` after a clock `advance`
awaits the in-flight work to quiescence (`docs/time-coeffect.md §advance`
already awaits it). The timer's derived inverse is widened accordingly — it
cancels the schedule **and** every still-in-flight task — so unload cancels the
pending timer plus any in-flight async work, leaving **no orphaned in-flight**
(R4/A8): the sync path's residue-free teardown extended to the async case. The
`asyncio` import appears only when an async timer (or a lifecycle test) needs it,
so a sync-timer document is unchanged. `examples/async_timer.rvl` runs the
every/after async timelines end-to-end on cordis-py.

This slice lands the **py reference tier** (frontend admission + colouring, and
the py emit + runtime contract). The **ts/rust/go** tiers already carry a timer
scheduler and an effect-ledger cancel inverse, but their timer emit renders a
*sync* firing closure; giving each an awaitable firing body + in-flight
cancellation (mirroring this py contract on `backends/{typescript,rust,go}/
emit.py` and each tier's runtime scheduler) is landed (item 223: ts emits the async firing + cancel; rust/go run via async-erasure — the `async` flag is a correct no-op where nothing suspends). **wasm**
still refuses timers honestly (below), so the async case is moot there until it
lowers timers at all.

## Scope (first slice)

The landed slice keeps a timer body to **`emit` statements**: the audited reach a
timer needs is exactly its emissions, and this keeps the semantics crisp. Those
emissions may now be **async** (item 170, above) — a timer body reaching an async
op is coloured async and awaited within its in-flight window. Richer
firing bodies — pure `let` bindings, `if` guards, nested effect acquisitions,
timers that arm timers, and `compensate` on a firing — are a documented
follow-on. A timer is an acquisition, so like every acquisition it must precede
any `provide` in the body (linker rule A2) and is not allowed inside a
provide-method body.

## Other tiers

Timers lower and run on **Python, TypeScript, go, and rust**. The go and rust
tiers mirror the reference contract tick-for-tick: each carries a
`Clock`/`RevlTimer` scheduler (`backends/go/emit.py`'s `_TIMER_PREAMBLE`,
`backends/rust/emit.py`'s `_revl_timer_preamble`) whose time advances only on
`RevlClockAdvance` / `revl_clock_advance` — firing due timers earliest-first,
ties by arm order, re-arming `every` across the span — so replay is
deterministic. A `timer` step lowers to a schedule armed inside the tier's
effect ledger (`ctx.Effect` on go, `ctx.effect` on rust) with cancellation as
the derived inverse, yielded into the same LIFO disposer stack every other
effect uses. Arming takes a live-resource slot (go's `revlHostAcquire`, rust's
`REVL_LIVE_HOST_RESOURCES`) that cancel — and a spent `after` — returns, so a
leaked `every` timer surfaces through the exact R1 residue accounting a leaked
Pool does. The exit tests
(`backends/go/scenarios/emitted/timer/gen_exec_test.go`,
`backends/rust/scenarios/timer.rs`) prove deterministic firing and
unload-cancels-no-residue by RUNNING on the real stc-go / cordis-rs runtimes.

On **go** the `advance` lifecycle step also drives that clock (item 102's go
half): a `lifecycle test`'s `advance <n><unit>` lowers to `RevlClockAdvance(N)`
(with `RevlClockReset()` at test start, so each test's timeline is independent),
firing the due timer bodies before the next statement observes them — the same
in-language firing the py/ts reference tiers assert. The go exit test lives in
`backends/go/scenarios/emitted/advance/gen_advance_test.go` (a self-running
`*_test.go` emitted from `advance.ir.json`), and `revl test --backend go
examples/lifecycle_timer.rvl` runs the every/after timelines end to end.

On **wasm** the construct still refuses honestly: the emitter rejects a `timer`
step rather than silently mis-lowering it, and `revl test` reports the refusal
as a clean skip —

> timers (`every`/`after`, item 57) are not yet lowerable on the wasm tier — a
> documented follow-on (docs/time-coeffect.md)

Lowering timers on the wasm tier is future work; the schedule/cancel + clock
contract in this document is the specification it will implement.

## Where it lives

| Concern | File |
| --- | --- |
| `every`/`after` keywords | `src/revl/lexer.py`, `selfhost/lexer.rvl`, `src/revl/formatter.py` |
| syntax → `TimerStmt` | `src/revl/parser.py` |
| syntax → `AdvanceStmt` (item 102) | `src/revl/parser.py` (`lifecycle_stmt`, `_advance_duration_ms`) |
| lowering → `timer` step, v3 gate, G4 reach | `src/revl/lower.py` |
| async colouring of a timer body (item 170) | `src/revl/lower.py` (`_lower_timer_step`, `_timer_body_reaches_async`, `_async_reached_outside_provide` prunes `timer`) |
| py async timer emit (spawn + tracked in-flight cancel) | `backends/python/emit.py` (`_timer`, the `import asyncio` gate) |
| lowering → `advance` step (item 102) | `src/revl/lower.py` (`_lower_lifecycle_body`) |
| clock coeffect + timer scheduler | `backends/python/runtime.py`, `backends/typescript/runtime.ts` |
| py/ts emitters (timer + `advance`) | `backends/python/emit.py`, `backends/typescript/emit.py` |
| go emitter (timer + `advance`, item 102 go half) | `backends/go/emit.py` (`_emit_stc_lifecycle_tests` lowers `advance` → `RevlClockAdvance`, `RevlClockReset` at test start) |
| `host.clockAdvance`/`clockReset` (item 102) | `backends/typescript/runtime.ts`; go's `RevlClockAdvance`/`RevlClockReset` live in `_TIMER_PREAMBLE` |
| honest `timer`-step refusal on wasm | `src/revl/test.py` |
| rust `advance` step → `revl_clock_advance(ms)` (item 112 rust half; drives item 99's Clock, clock reset at test start) | `backends/rust/emit.py` (`_emit_v3_lifecycle_tests`) |
| honest `advance`-step refusal on wasm (py/ts/go/rust drive it; wasm has no live-composition lifecycle machinery yet, so it refuses the whole lifecycle test) | `backends/wasm/emit.py` lifecycle dispatch |
| exit tests | `tests/test_time_coeffect.py`, `backends/typescript/tests/time_coeffect.test.ts` |
| examples | `examples/heartbeat.rvl` (timers), `examples/lifecycle_timer.rvl` (`advance`), `examples/async_timer.rvl` (async timer body, item 170) |

# Time as a coeffect

*Roadmap item 57. Reference tiers: Python + TypeScript.*

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

`advance` fires every due timer earliest-first, ties broken by arm order,
re-arming `every` timers across the whole span — a total, reproducible ordering
across any number of interleaved timers. A production driver would pump `advance`
from a real monotonic source; the reference tier keeps it explicit so tests are
deterministic. The Python (`backends/python/runtime.py`) and TypeScript
(`backends/typescript/runtime.ts`) clocks agree tick-for-tick.

## Scope (first slice)

The landed slice keeps a timer body to **`emit` statements**: the audited reach a
timer needs is exactly its emissions, and this keeps the semantics crisp. Richer
firing bodies — pure `let` bindings, `if` guards, nested effect acquisitions,
timers that arm timers, and `compensate` on a firing — are a documented
follow-on. A timer is an acquisition, so like every acquisition it must precede
any `provide` in the body (linker rule A2) and is not allowed inside a
provide-method body.

## Other tiers (documented follow-on)

Timers lower and run on the reference tiers, **Python and TypeScript**. On
**go, rust, and wasm** the construct refuses honestly: those emitters reject a
`timer` step rather than silently mis-lowering it, and `revl test` reports the
refusal as a clean skip —

> timers (`every`/`after`, item 57) are not yet lowerable on the go tier — the
> reference tiers are py + ts; a documented follow-on (docs/time-coeffect.md)

Lowering timers on those tiers is future work; the schedule/cancel + clock
contract in this document is the specification they will implement.

## Where it lives

| Concern | File |
| --- | --- |
| `every`/`after` keywords | `src/revl/lexer.py`, `selfhost/lexer.rvl`, `src/revl/formatter.py` |
| syntax → `TimerStmt` | `src/revl/parser.py` |
| lowering → `timer` step, v3 gate, G4 reach | `src/revl/lower.py` |
| clock coeffect + timer scheduler | `backends/python/runtime.py`, `backends/typescript/runtime.ts` |
| py/ts emitters | `backends/python/emit.py`, `backends/typescript/emit.py` |
| honest refusal on go/rust/wasm | `src/revl/test.py` |
| exit tests | `tests/test_time_coeffect.py`, `backends/typescript/tests/time_coeffect.test.ts` |
| example | `examples/heartbeat.rvl` |

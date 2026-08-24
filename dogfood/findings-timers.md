# Findings — timers in the harness (item 57 verification, finding #25)

Probe: the revl-harness verification of item 57 (`every`/`after` timers as
revertible schedules, clock-as-coeffect replay). Milestone 30 of the harness
(`src/timer_tests.rvl` + `tools/timer_demo.py` + `docs/item-57-verification.md`
in `~/Projects/revl-harness`). One finding, and one consumer-side adaptation.

## 1. The firing half of item 57 is not expressible in `revl test`

Item 57's contract has two halves:

1. **the schedule is revertible** — arming acquires a schedule whose inverse
   is cancellation, derived teardown, so unload cancels residue-free;
2. **the clock is a coeffect** — `Clock.now()` moves only when the harness
   calls `Clock.advance(ms)`, so a firing is a deterministic timeline step
   ("fires on the 3rd tick"), never a wall-clock race.

The harness proves half 1 **in-language** and it passes on py + ts:

```revl
component Heartbeat requires log: Log {
  every 250ms { emit log.write("tick") }
  after 2s    { emit log.write("warmup done") }
}

lifecycle test "the clock is a coeffect — nothing fires unbidden while live" {
  load Logger
  load Heartbeat
  let n = call log.count()
  assert n == 0
  unload Heartbeat
  unload Logger
  assert no_residue
}
```

But half 2 — "fires on the 3rd tick" — **cannot be written as a lifecycle
test**. The lifecycle statement set is only `load` / `unload` / `call` /
`assert` / `assert no_residue` (`src/revl/parser.py::_LIFECYCLE_STMT_WORDS`,
`src/revl/lower.py::_lower_lifecycle_body`). A firing happens only when
something calls `Clock.advance(ms)`, and nothing in-language can:
`asyncio.sleep` in the emitted harness cannot produce one, because the clock
never moves on its own. The harness proves half 2 with a host driver that
plays the role `docs/time-coeffect.md` reserves for "the harness":

```python
Clock.advance(250)            # -> 1 firing, exactly the 250ms tick
Clock.advance(500)            # -> 2 more (500, 750): "fires on the 3rd tick"
Clock.advance(1250)           # -> 5 ticks + the one-shot `after` at 2000ms
# unload -> Clock.pending() == 0; +1 year -> 0 firings, count frozen
```

**Ask (roadmap item 96):** an `advance <ms>` lifecycle step — parser
(`AdvanceStmt`), lowerer (`{"step": "advance", "ms": N}`), py emitter
(`Clock.advance(N)`), ts emitter (`host.clockAdvance(N)`), and docs — so the
whole item-57 contract is assertable in `revl test` on the reference tiers,
and the fault sweep can index "fail at the third firing" from the language.

## 2. `after` is a reserved keyword now — the harness adapted (no bug)

Item 57 made `after` (and `every`) reserved keywords. Existing revl programs
that used `after` as an identifier no longer compile; the diagnostic is clear
and the break is inherent to the syntax, so this is an adaptation, not a
finding — recorded here so the picking agent knows the harness already moved:
`src/approval_tests.rvl` / `src/session_tests.rvl` renamed `let after = …` to
`let pending_list` / `let after_list` (milestone 30, commit ff162bd).

## Verified green (the claims that do hold, pinned)

- `every 250ms` fires deterministically at 250/500/750/… — exact ticks, in
  arm order, ties broken by arm order (py driver asserts each firing).
- `after 2s` fires exactly once at 2000ms and is spent (`Clock.pending()`
  drops to the `every` timer only; the teardown `cancel()` is a clean no-op).
- unload runs the derived inverse: `Clock.pending() == 0`, no residue
  (`assert no_residue` passes on py + ts).
- a year of logical time after teardown produces 0 firings — no orphan.
- go/rust/wasm refuse timers honestly (`_timer_follow_on` skip).

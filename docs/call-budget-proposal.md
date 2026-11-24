# A bound on one call into a running session: a proposal

**Status: proposal, nothing implemented.** Issue #3320. Nothing here changes a
tier until the decisions at the end are confirmed.

## What is wrong

`Session.call` (`src/revl/mcp/session.py`) runs the operation as
`self._run(invoke())`, and `_run` is `self._loop.run_until_complete(coro)`. A
body that loops without an `await` never yields to the event loop, so a timeout
wrapped around the coroutine cannot fire, and the thread that made the call is
held until the process is killed.

Measured on main `63964644a`, py tier, CPython 3.12, with the reproduction from
the issue (`count(limit)` behind `work.run`), run in a worker thread that was
given 3 s and then an async exception:

- `session.call("work", "run", [1000])` returns `1000`.
- `session.call("work", "run", [-1])` is still running after 3 s. The only way
  out is a second thread raising in the calling thread
  (`PyThreadState_SetAsyncExc`). In that run the exception landed in the
  program's own code and the next call answered `1000`. The issue's second
  failure (the exception landing in the session's plumbing and leaving the loop
  stopped) is the other outcome of the same mechanism.

The ts tier has no session to put a deadline on. `revl run --backend ts`
(`src/revl/run_ts.py`) boots a node process, runs the once round trip and exits;
its interactive REPL is not wired. What the ts tier has is an emitted module
that a host calls directly, in node or, since #3315, in a page. A bound there
has to live in the emitted code and the ts runtime, not in a `Session`.

## What the measurements in the issue rule out

| Way to end a call | Why not as the design |
| --- | --- |
| A second thread raises in the caller (the stand-in) | Needs threads, so not in a page. The stop lands wherever the interpreter is: in a finaliser or a `gc.callbacks` callback it is thrown away (29 of 30 loops never stopped), in session plumbing it breaks the session. |
| A second web worker sets Pyodide's interrupt byte | Needs shared memory, so a cross-origin isolated page. A static host cannot set the two headers. |
| `sys.monitoring` on every backward jump and function start | Works anywhere on 3.12, but a 7 ms tick becomes 22 to 30 ms (about 3.2 times on the terrain chunk). Too dear to leave on. |
| A count the emitter writes into loops and function starts | The issue's estimate from plain Python: 1.2 times on a small loop when the count is a local of the function, 2.7 times on a shared object. Needs the emitter. |

The last row is the only one that works on both tiers, in a page, and does not
depend on where the interpreter happens to be when a stop arrives.

## Proposal

### 1. Mechanism: a cooperative count, emitted when asked for

The py and ts emitters add a count to each `while` and `for` and to each
function entry. When the count runs out, the code raises a typed refusal from
inside the program, at a statement boundary. A stop that is raised by the
program itself cannot land in a finaliser, a collector callback or the session's
event loop, so the session stays usable. What it leaves behind is section 4.

The count is opt-in at emit time. An emit without it is byte-identical to
today's, so every golden and every existing session is untouched, and the cost
is paid only by a host that asked.

Sketch for the py tier (not final text):

```python
def count(limit):
    n = 0
    _k = 0                       # a local: no shared write per turn
    while ((limit < 0) or (n < limit)):
        _k += 1
        if not _k & 255:
            _revl_spend(256)     # takes 256 steps off the call's budget; raises when it is spent
        n = ...
    return n
```

A function entry takes one step off the call's budget the same way. Spending a
chunk at a time should keep the per-turn cost near the local-count estimate in
the issue (1.2 times; the chunked form itself is untested, and the first slice
measures it), at the price of an overshoot of at most 255 steps per running loop.
Whether function entries need their own cheaper form is for the first slice to
measure on the playground's tick; the fallback is to count loops only, which
still bounds every loop that does not end and leaves a recursion that does not
end to the interpreter's own recursion limit.

### 2. Units: a step budget, and a deadline on the same tick

`Session.call(key, method, args=None, *, raw=False, steps=None, deadline=None)`.

- `steps` is the primary bound. It is a count, so it gives the same answer on a
  busy machine as on a quiet one, and it needs no clock, which is the only thing
  a page has. It does not count the collector, because a collection runs no
  emitted code.
- `deadline` (seconds) is a secondary bound, read from the monotonic clock
  at the same chunk tick, so it adds no cost of its own per turn. The collector
  is set aside by recording, in a `gc.callbacks` callback that only reads the
  clock and never raises, the time of each collection that ran on the watched
  thread, and subtracting it. That answers both findings in the second comment:
  a collection of a big heap is not charged to the call, and there is no stop
  for the callback to swallow, because the refusal is raised by the loop and not
  by another thread.
- With neither set, a call behaves as today.

### 3. The refusal

A new `BudgetExceeded(RuntimeError)` in `backends/python/runtime.py`, beside
`CeilingExceeded` (the declared-ceiling fault, #2688) and mirroring the wasm
runner's `ComponentTrap` reasons (`fuel`, `wall`). It carries `kind` (`steps` or
`deadline`), `limit` and `observed`. `Session.call` re-raises it as a
`SessionError` subclass that also names the operation (`work.run`), so the MCP
wire and the HTTP face report a refusal that names the call, as the issue asks.

### 4. What happens to the effects of a refused call

The issue's premise is that the effects of a refused call "are undone like any
other fault". That does not hold on main: a fault does not undo them. Setup: a
`Map` held in a component with `handoff`, one call that puts one key (size 1),
then a call that runs `effect m.insert(k, v) undo m.remove(k)` and overflows
(`Int` overflow, the existing `revl: Int overflow` trap). The second call
raises and `m.size()` is 2, not 1: the entry stays registered until an abort or
teardown. Only a UI transaction unit (`Frame.ui_transaction`, `_CallScope.settles`) settles its
own entries when its call fails.

So "state under `handoff` is what it was before the refused call" is a stronger
rule than a fault gives today. Proposal: a metered call runs in a settling call
scope, the same mechanism the UI transaction unit uses, so a refusal replays
that call's own compensations newest first and leaves other entries alone. This
makes a refusal different from a plain fault on purpose, and only in a call
that was given a bound.

### 5. `swap`

`Session.swap(..., steps=None, deadline=None)` bounds the successor's
activation, the load in `_cut_over` (`driver._load`). A refusal there takes the
rollback a failed activation already takes: `_abort_swap` reloads the
predecessor. Two things need care in the slice. First, an activation body that
fails is settled by the runner as a FAILED fiber without raising, and
`_assert_successor_activated` then raises a generic `swap rejected` error, so
the typed refusal has to be carried through that path (recorded when the fiber
fails, and re-raised as the refusal after the rollback). Second, `swap` runs
program code in other places too: the teardown of the old generation, the
instance reconcile and the provider-state restore. The bound covers the
successor's activation only. The predecessor reload in `_abort_swap` is the
way back and is never bounded, because a bound on it could leave nothing
running.

### 6. ts

Same counts from `backends/typescript/emit.py` (the provide-method renderer
`_method_body` and the module-function renderer `_v3_stmt`, which also renders
`test` bodies and must leave those uncounted), a spend function
and a budget in `runtime.ts`, and a wrapper the host calls an operation through,
for example `runWithBudget({steps}, () => svc.run(-1))`, which raises the typed
refusal. JavaScript has no other way to stop a synchronous loop on the thread
that runs it short of terminating a worker, so the emitted count is the only
option that works in the page as well as in node. The name of the wrapper is
settled in the ts slice.

### What this does not bound

- Host code: an `extern` that loops, and a builtin that does a lot of work in
  one step (a very large string repeat). A step is a loop turn or a function
  entry in revl code.
- Memory.
- A call parked on an `await` that never resolves. That does not hold the
  thread, and the wall `deadline` can bound it with `asyncio.wait_for`; the step
  budget does not.
- Callers other than `call` and `swap` (`act`, the HTTP face) until they pass
  the arguments through, which is mechanical and left to a later slice.

## Slices

Each one is reviewable and landable alone, and each changes the self-host
mirror (`selfhost/emit_py.rvl`, `selfhost/emit_ts.rvl`) with its emitter.

1. **py count and `Session.call`.** The emit option, `_revl_spend`,
   `BudgetExceeded`, `steps` and `deadline` on `call`. Tests: the issue's
   reproduction with a bound returns the refusal within it; the same session then
   answers `run(1000)` with `1000`; a call that finishes inside the bound
   returns what it returns today; an unmetered emit is byte-identical (goldens
   unchanged); a 300 MB heap does not make a 5 ms call exceed a 300 ms
   `deadline`. Measured on the playground's tick before it lands: the bar is
   about 1.3 times, against 3.2 for `sys.monitoring`.
2. **Effects and `swap`.** The settling call scope for a bounded call, and the
   bound on `swap` with its rollback. Test: `handoff` state is what it was before
   the refused call.
3. **ts.** The ts emitter and runtime, a test per environment the tier runs in
   (node; the Chrome gate from #3315), and the refusal in the ts runtime
   contract document.

## Decisions I need confirmed

1. **Steps as the primary bound, with `deadline` as a secondary on the same
   tick**, against a wall-clock deadline alone. Recommended: steps, because it
   does not depend on the machine or on the collector, and a page has no
   processor clock.
2. **Opt-in at emit time** (a session asks to be metered when it loads), against
   metering every session. Recommended: opt-in until the cost is measured on a
   real world, so existing sessions and goldens stay as they are. A bound passed
   to a session that was loaded unmetered would be refused by name, not ignored.
3. **A refused call undoes its own registered effects.** The issue assumes a
   fault already does, and it does not today (section 4). Against leaving them
   as a fault does and dropping the `handoff` clause from the acceptance.
4. **The refusal type**: `BudgetExceeded` in the runtime and a named
   `SessionError` subclass at `Session`, against reusing `CeilingExceeded`.
   Recommended: a new type, because `CeilingExceeded` is a declared capability
   ceiling and this one is set by the caller.
5. **The ts surface is a wrapper function in `runtime.ts`**, since the tier has
   no session, against waiting for a ts session.

# Findings — workflows (finding #27)

Probe: multi-agent fan-out + aggregation in the lighthouse workload. First design
spawned three workers and fanned tasks out through a per-index arrow whose
body is a ternary chain of `emit <handle>.<key>.<method>()` calls. One real
revl gap, plus the shipped workaround.

## 1. Async colour does not see spawned-handle emissions in arrow bodies

The spawned design (`effect spawn WorkflowWorker ...` then
`(task, i) => (i % 3 == 0 ? emit w1.wtask.run(task) : ...)`) compiled but
leaked a coroutine at runtime:

```
TypeError: can only concatenate str (not "coroutine") to str
RuntimeWarning: coroutine '_workflow_worker_apply..._Wtask.run' was never awaited
```

The emitted Python wrapped the arrow with `_revl_as_async(lambda task: ...
run(task))` — the **sync** classification — so `await run_one(task)` inside
`run_all` produced a coroutine object, not the awaited value.

Root cause is two layers:

- **analysis**: `_async_callables` (src/revl/emission_analysis.py) colours
  over the static call graph — `req`-based emissions (`model.complete`,
  service methods of a `requires`) are known async, but a spawned handle's
  service access (`w1.wtask.run`) is not, so the arrow body is not flagged
  async;
- **emitter**: `_py_async_arrow` then picks the sync branch and
  `_revl_as_async` is `async def _g(*a): return _f(*a)` — correct for a sync
  `_f`, but for a coroutine-returning `_f` it yields the coroutine object
  (`return` in an async fn does not await). `return await _f(*a)` would be
  defensive.

**Ask (roadmap item 98):** colour `handle.key.method()` emissions like
`req.key.method()` ones (the spawn handle's service type is known), and make
`_revl_as_async` `return await _f(*a)`.

## 2. Shipped workaround: requires-based workers + sublist dispatch

The workflow ships with the three workers as explicit `requires`
(`w1`/`w2`/`w3: WorkflowTask`, provided by `WorkerPool` under three keys —
G2-clean) and the task list split by index into sublists, each driven
through ONE worker with a plain tail-coroutine arrow (`task => emit
w1.run(task)` — a `req` emission, correctly coloured), then interleaved
back into task order. This also avoids the ternary-arrow shape entirely.
The workflow passes 2/2 py + ts; `tools/workflow_demo.py` PASS. When item
98 lands, the spawned design becomes expressible.

Also recorded: `agent_loop` in `src/components/agent.rvl` is now `pub` —
the first cross-file fn import in the harness (the worker reuses the
harness's own loop).

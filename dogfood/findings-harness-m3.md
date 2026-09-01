# findings — lighthouse workload: durable sessions + subagents (agent/harness-m3)

Durable session persistence + subagents, built on devwip @ 66bfc2a (plus
the fr13 extern-dedent fix, which this branch carries). Workload commit
`805ce91`.

## 1. Refusal log

- **`emit <handle>.<key>.<method>()` → frontend crash (KeyError)** —
  ```revl
  component Supervisor ... {
    let w = effect spawn WorkerAgent with { worker_id: config.worker_id } undo w.dispose()
    provide sup {
      fn delegate(prompt) = emit w.task.run(prompt)   // ← KeyError: 'target'
    }
  }
  ```
  ```
  File "src/revl/lower.py", line 3672, in _is_emission_call
      target = node["target"]
  KeyError: 'target'
  ```
  Verdict: **`caught-bug`-shaped gap** — the emission marker check handles
  only `req`-target call nodes; a spawn-handle provision access lowers to
  `instance-get`, which `_is_emission_call` doesn't know, so `emit` on it
  crashes instead of validating (or refusing). The non-emission spelling
  (`w.task.status()`) compiles and runs. Workaround that shipped: call the
  worker's emission method bare from a supervisor method declared
  `emission` — the boundary is marked one level up. Filed as roadmap
  item 82.
- **`log_open` with a relative path — `os.makedirs('')` crash** — the
  extern body was `os.makedirs(os.path.dirname(path))`; a bare filename
  has `dirname == ''`. Verdict: `caught-bug` (my bug, not revl's) — the
  harness fixed the body (`if d and not os.path.exists(d)`).
- **rust/go refuse the durable/subagent composition** — expected: the JSON
  stdlib has no @rs/@go bodies (item 81), so any composition using the
  harness's JSON protocol stops at py/ts. The durable components use no
  JSON, so they would run on rust/go — the refusal comes from the shared
  services file. Not filed separately; it is item 81's consequence.

## 2. Friction log

- `[slow]` **`spawn` has no example in the harness-facing docs.** The
  design doc (docs/design-v2-instances.md) is thorough but phase-1-flavored;
  finding the working spelling (`effect spawn C with {..} undo
  handle.dispose()`, then `handle.key.method(...)`) took a probe cycle and
  the crash above. Item 82's fix + a `guide-ai-agents.md` snippet would
  close it.
- `[slow]` **Extern `undo` slot is effectively documentation.** For
  `extern acquire fn log_open(path) -> Int undo log_close(1)`, the
  harness must still write the component-level
  `let fd = effect log_open(config.path) undo log_close(fd)` — the
  extern-level undo is a fixed expression, not a binding of the acquired
  value. Clear once understood; the doc (item 83a) is the ask.
- `[nit]` **Column-0 extern bodies** until item 78 lands — the harness
  keeps the workaround in `durable_log.rvl` (and `http.rvl`).

## 3. What revl gave us (this wave)

- **Instance-parametric components are real and clean.** The subagent
  pattern — supervisor spawns a worker with its own requires, own config,
  own session slice; disposal reverts the worker's effects — is the
  DSH-subagent analog, expressed as one `effect spawn … undo dispose()`.
  The lifecycle test proves the whole thing leaves no residue.
- **The durable resource discipline composes.** `effect Map.new() undo
  drop()` + `effect log_open(path) undo log_close(fd)` side by side in one
  activation: unloading replays both inverses in LIFO order and the
  runtime reports no residue, with the file persisting as the durable
  artifact. The acquire/undo model held up on a real host resource.
- **`no_residue` caught nothing this wave (no leak to catch)** — but the
  tests prove the two inverses (map drop, fd close) both ran, which is the
  assertion working as designed.

## 4. Time-to-green

- Compose → refuse → fix cycles: **2** (spawn-emit crash; `makedirs('')`).
- Longest stall: the spawn-emit crash — ~4 probe cycles to isolate the
  exact node shape (emission-call-through-handle vs non-emission) and
  find the working spelling. The KeyError gave the file:line, which
  shortened it; a diagnostic (item 82's real fix) would cut it to zero.

## 5. Cost ledger

- `missing-feature` — `emit` on `instance-get` targets (item 82); the
  harness worked around it by moving the `emission` declaration one level
  up, which is honest but cost a design detour.
- `docs-gap` — no spawn usage example in the agent guide (item 82
  sibling); extern `undo` semantics undocumented (item 83a).
- `env` — none.
- `diagnostic` — none new; the KeyError was at least precise.

**Single change that would cut the most cost next:** item 82 — make the
emission check understand `instance-get` targets. It removes the crash and
the spelling detour, and unlocks `emit` through a spawn handle as written.

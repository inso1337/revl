# findings — harness milestone 7 (agent/harness-m3, third wave)

Self-evolution — the harness rewriting its own composition, end to end.
Harness repo: `~/Projects/revl-harness`, commits `1664b36` / `9f22ad9`.

## 1. Refusal log

- **No new compile refusals this wave.** The evolver composed entirely of
  patterns already proven in milestones 1-6 (ternary + emit for branching,
  externs for host crossings, `Map` effects). The one notable *runtime*
  trip was environmental, not linguistic:
- **extern bodies cannot see injected module globals across the Session
  boundary** — the driver set `module.__REVL_SESSION__` on the emitted
  module it created, but `session.call` runs the extern inside the
  session's *own* freshly exec'd module (`revl_run_genN`), so the global
  was not there (`NameError: __REVL_SESSION__`). Verdict: `friction`
  (documentation-shaped, not a checker gap) — the fix was the demo's own
  pattern: delegate to an imported host bridge module
  (`tools/harness_evolve_bridge.py`) whose globals the driver sets.
  The demo's evolve_bridge.md documents this; the harness followed it.
  A doc line in guide-ai-agents.md ("extern bodies run in the session's
  emitted module; reach host state through an imported bridge, not
  injected globals") would save the next author the same probe.

## 2. Friction log

- `[slow]` **After a swap, the old composition's keys are gone** — calling
  `evolve.propose_candidate` on a good candidate then swapping left the
  harness without `evolve` (the swapped composition was just the new
  component). Correct behavior (the swap replaced the composition), but
  the demo had to reorder (refuse first, swap last) to keep serving.
  A note in the driver's output made it self-explanatory.
- `[nit]` **The mock model can't author real components** — `once(goal)`
  asks the model for source, but the mock echoes. The driver therefore
  authors candidates directly (simulating the model's output, as the
  demo's GREETER_V2 does). Honest: the *admission* is the tested half.

## 3. What revl gave us (this wave)

- **The lighthouse loop is ~60 lines of revl + one host bridge.** Boot →
  author → admit → hot-swap → revert-on-refusal, with the compiler
  deciding every step. The demo proved the same; the harness now *is* it.
- **`compile_source(..., manifest=running_ir)` is the admission gate and
  it worked first try** — a candidate that would break the running
  composition is refused before any runtime is touched.
- **The swap itself is derived teardown**: the running composition was
  disposed LIFO and the new one booted, then `no_residue: True` at the
  end. The guarantee held across a live self-modification.

## 4. Time-to-green

- Compose → refuse → fix cycles: **1** (the NameError global-injection),
  plus the demo-reorder (not a refusal, a sequence fix).
- Longest stall: the module-global boundary — ~2 probes (inject on the
  driver's module vs the session's). The bridge pattern (from the demo)
  resolved it.

## 5. Cost ledger

- `docs-gap` — extern bodies vs the session's module namespace; the demo's
  evolve_bridge comment carries the knowledge, guide-ai-agents.md doesn't.
- `env` — none. `diagnostic` — none.

**Single change that would cut the most cost next:** a two-line note in
guide-ai-agents.md (extern bodies reach host state via an imported bridge,
because they run in the session's own emitted module). It converts the
milestone's only real stall into a documented pattern.

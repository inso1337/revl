# findings — harness verification of item-80 slices 1-3 (agent/harness-m3)

## 1. Refusal log

- **`ModelProvider.complete` declared sync, reaches async extern `http_post`
  → refused with the A1 witness chain** — the slice-2 coloring worked first
  try against the harness: "declared sync, but this implementation reaches
  async extern `http_post` — a sync method has no in-flight window (A1)",
  with `complete -> http_post` as the why-trace. Verdict: **caught-bug
  (the checker doing its job)** — exactly the design's §3 diagnostic.
- **module fn reaching an async extern refused (phase 2)** —
  `agent_loop(msgs, complete: (List[Msg]) -> Str, ...)` calling the async
  `model.complete` through its callback is refused with "cannot carry the
  async color yet" — the design's phase-2 rule. Verdict: **`gap`
  (documented phase-2)** — blocks the harness's loop shape, not the direct
  path. Filed as roadmap item 90.

## 2. Friction log

- `[slow]` **emit + async composition** — an async emission service call
  still needs `emit` at the call site (`let raw = emit http_post(...)` in
  an async method), and the async extern's own call site is awaited by the
  ts emitter. The two markers coexist; the docs (design §1) show the extern
  body, but not the call-site spelling (`emit` stays). One line in
  guide-ai-agents.md ("an async emission is still marked `emit` at the
  call site") would save the next author a probe.

## 3. What revl gave us

- **Slice 3's exit test passed against the harness's exact bug.** The ts
  emitter now produces `export async function http_post(url: string, body:
  string): Promise<string>` with `await fetch` and awaited call sites; the
  emitted file's only tsc error is the standalone-check runtime.ts path
  resolution (the real suite resolves it). `Promise<string>` vs `string`
  is gone.

## 4. Time-to-green

- 1 probe cycle for the async provide-method spelling (the test file on
  the branch showed it); the A1 coloring and the ts emission worked on the
  first compile/typecheck after that.

## 5. Cost ledger

- `missing-feature` — module-fn async color (item 90, phase 2).
- `docs-gap` — the emit+async call-site spelling (one line ask above).
- `diagnostic` — the A1 message names the rule and the fix; the phase-2
  refusal names the phase. Both good.

**Single change that would cut the most cost next:** item 90 (module-fn
async color) — it is the last piece before the harness's full agent loop
runs on the ts tier with the real HTTP provider.

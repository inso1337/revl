# findings — lighthouse workload: ts-record-params (agent/fr86-ts-record-params)

## 1. Refusal log

- **`List[Msg]` service param emits `unknown[]` on ts** — the RealModel
  provider's `complete(history: List[Msg])` emitted
  `complete(history: unknown[]): string` in the service interface, and
  `tsc` rejected every call site (`Type 'unknown[]' is not assignable to
  type 'Msg[]'`). Verdict: **`gap` (ts emitter)** — the v1
  service-interface renderer (`_ts_type`) knows only the primitive
  `TYPE_MAP`; record names fall to `"unknown"`. The record interface
  *is* emitted (`export interface Msg`), so the fix is a fall-through:
  unknown names route to `_ts_v3_type` (which renders records). Filed as
  item 86; implemented on `agent/fr86-ts-record-params`; golden
  regenerated; 133 frontend/emit tests green. This is the ts half of
  item 81's "JSON narrowed multi-tier reach" — with it, the JSON
  protocol's *types* are correct on ts; only the async-extern gap
  (item 80) remains between the real provider and a green `tsc`.

## 2. Friction log

- `[slow]` **Two type renderers, one forgotten** — the ts emitter has
  `_ts_type` (v1) and `_ts_v3_type` (v3); service interfaces use the v1
  one, which predates records. The conformance suite validated emitted
  code with tsc, but the harness's record-typed service params were the
  first corpus case to cross the boundary — the same "two dispatchers"
  shape item 76a warns about (now three renderers with the same story).

## 3. What revl gave us

- **The tsc gate caught a real emitter gap the vitest path missed.**
  `revl test --backend ts` (vitest) passed; `tsc --noEmit` (the tier's
  real compiler) refused. The conformance `--validate` doctrine works
  when you run the real compiler.

## 4. Time-to-green

- 1 probe (read the emitted service interface, saw `unknown[]`, traced to
  `_ts_type`), 1 fix, golden regen, green. The fix took longer to
  explain than to write.

## 5. Cost ledger

- `missing-feature` — item 86 (fixed, branch pushed).
- `tooling` — the golden regen script (`backends/typescript/scripts/
  regen-golden.py`) worked; good.

**Single change that would cut the most cost next:** item 80 (async
extern bodies) — the one remaining tsc error on the real provider, and
the last blocker for a green DSH-tier deployment of the HTTP harness.

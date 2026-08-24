# findings — harness verification of item 90 (agent/harness-m3, sixth wave)

## 1. Refusal log

- **async callback arrow leaks its coroutine** — with items 80+90 merged,
  the harness's full async migration (http async, services async, mock/
  real_model/agent async) compiles, but the mock+loop lifecycle tests fail:
  `TypeError: the JSON object must be str, bytes or bytearray, not
  coroutine` + `coroutine '_Model.complete' was never awaited`. Verdict:
  **`gap` (finding #21)** — `agent_loop(msgs, complete: (List[Msg]) ->
  Str, ...)` is a sync module fn; item 90 colored *direct* module-fn calls
  but not a callback passed *through* a sync function type. The arrow
  `msgs => emit model.complete(msgs)` returns a coroutine/Promise at
  runtime; the loop's `resp = complete(current)` does not await (py and ts
  both). Item 90's roadmap claim ("the harness's `agent_loop` works
  unchanged") is falsified by execution. Filed as item 92.

## 2. Friction log

- `[slow]` **The claim in item 90 was wrong and only execution showed it.**
  The roadmap said the loop would work unchanged; the compile said yes, the
  runtime said no. The emitted code inspection (sync loop + non-awaiting
  call) located it in ~1 probe. A `compile --validate`-style runtime check
  (item 78 residual) would have caught it at the frontend.
- `[nit]` **Direct path vs loop path divergence** — the direct real-model
  path (`async fn complete` awaiting `http_post` inline) works; only the
  callback-funneled loop breaks. The harness keeps the sync loop (works
  today) and the async direct provider (migration-ready), per the migration
  doc.

## 3. What revl gave us

- **Items 80 + 90 are real and verified**: async externs typecheck on ts,
  the A1 coloring refuses sync->async with a witness chain, and the
  transitive coloring colors direct module-fn calls. The harness's ts-tier
  `Promise<string>` blocker is gone. The remaining gap is narrow and
  precisely located (function-typed callbacks).

## 4. Time-to-green

- 1 cycle (compile green -> runtime coroutine leak -> emitted-code
  inspection). The fix (item 92) is one of three sketched directions.

## 5. Cost ledger

- `missing-feature` — async function values (item 92).
- `docs-gap` — item 90's "works unchanged" claim (corrected by this
  finding).

**Single change that would cut the most cost next:** item 92 — async-aware
function types (or inferred arrow coloring). It is the last gap between the
harness's full agent loop and a green ts-tier deployment with the real
provider.

# findings — harness follow-up (agent/harness-dogfood, second wave)

The FR-1…FR-11 re-test: the lighthouse workload was re-run against the
merged features. Three per-tier follow-ups surfaced; each is a small, named
emitter gap, not a design problem. The workload (commit `34fad68`) was
re-tested on `origin/devwip` @ `84f3f6a`.

## 1. Refusal log

- **TS emitter: arrow parameters still unbound in component scope** —
  the FR-1 loop-shaped harness (`agent_loop(msgs, complete, call_tool,
  max_steps)` with provide-method arrows `msgs => emit model.complete(msgs)`)
  compiles and runs on py, but:
  ```
  [ts] fail: emitter refused: reference to unbound name 'msgs' in component 'Agent'
  ```
  Verdict: **`gap`** — FR-1 bound arrow params in the frontend (lower.py)
  and py executes them, but the ts component-dialect emitter checks names
  against `scope.locals` (backends/typescript/emit.py:456) and never adds
  arrow params to it, so the ts tier refuses the exact pattern FR-1 was
  built to unlock. The step-shaped harness (no arrows in method bodies)
  emits and boots on ts fine — so this is purely the arrow-in-method-body
  path. Same class of per-emitter lag the dispatcher-conformance map
  (item 76a) exists to catch.
- **java: host `Map` stub still `HashMap<String, String>`** — the session
  ledger (`Map[Str, List[Msg]]`) fails:
  ```
  error: incompatible types: no instance(s) of type variable(s) T exist so
  that List<T> conforms to String
      this.store.insert(id, revlPush((prev), msg));
  ```
  Verdict: **`gap`** — FR-4 genericized the rust tier's `Map` only;
  backends/java/emit.py:1956 still emits
  `HashMap<String, String> values`. The java emitter needs the same
  value-type threading FR-4 gave rust (or a generic `HashMap<String, V>`).
- **go: v3 typed-core composition not placeable** —
  ```
  error: placement on the go backend needs v1/v2 services; this
  composition is v3 typed-core (no live stc-go component)
  ```
  Verdict: `friction` (honest, documented) — FR-8 wired `revl run
  --backend go` for v1/v2; the harness's records/ADTs/arrows are v3, so
  the go run driver says no. Fine as a tier line; the FR-8 claim should
  say "v1/v2 compositions" so the capability is precise.

## 2. Friction log

- `[slow]` **Lifecycle tests on java still refuse by name** — FR-5 lowered
  them to py/ts/rust/go but java (cordis4j) and wasm remain
  skip-with-reason. The harness's no-residue proof is not yet on the JVM.
- `[nit]` **`revl test --all` summary counts refusals as "failed"** — the
  verdict column now prints `pass/skip:reason/fail` per tier, but the
  final `summary:` line still says "3 tier(s) failed" when two were skips.
  Cosmetic; the per-tier lines are correct.

## 3. What revl gave us (this wave)

- **FR-1's exact design shape worked first try.** The recursion-with-
  callbacks loop compiled and passed 3/3 lifecycle tests on py on the
  first compile — no probe cycles, unlike the pre-FR-1 arrow-scope
  debugging (which took three). The feature is real.
- **`Str.to_int()` and `startsWith` removed real code.** The hand-rolled
  15-line `parse_int` and the off-by-one-prone `slice(0, n) == "..."`
  prefix checks are gone from the harness; both tests still green.
- **`revl run --backend ts` / `--backend rust` both reach NO-RESIDUE** on
  the harness composition — the multi-runtime claim is now true on three
  tiers for the step-shaped harness, asserted end-to-end.

## 4. Time-to-green

- Compose → refuse → fix cycles: **0** for the loop shape on py (first
  compile green); **3** tier probes (ts/rust/go/java) to map the per-tier
  follow-ups above.
- Longest stall: none — each tier refusal named the tier and the construct.

## 5. Cost ledger

- `missing-feature` — (i) ts arrow-param scope in the component emitter
  (blocks the FR-1 loop shape on the DSH target tier — the highest-value
  follow-up), (ii) java Map value generics, (iii) go v3 placement.
- `tooling` — none new; the harness's two-venv setup remains (cost ledger
  note from wave 1).
- `diagnostic` — none this wave; the tier refusals were all accurate and
  actionable.

**Single change that would cut the most cost next:**
(i) — bind arrow params in the ts emitter's component scope, mirroring
FR-1's lower.py change. Then the loop-shaped harness (the lighthouse shape)
runs on ts, the DSH target tier, and the roadmap's lighthouse claim closes.

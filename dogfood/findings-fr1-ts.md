# dogfood findings — FR-1 on the TypeScript tier (arrow params in component scope)

**Branch:** `agent/fr1-ts-emit` — the FRONTEND half of FR-1 (checker + lowerer,
commit 1debdf2) is merged; this agent does the TS EMITTER half: the emitted
arrow literal must bind its `params` in the arrow's body scope.

## 1. Refusal log (every `revl` rejection you hit)

### R1 — `revl run --backend ts` on the harness loop shape (the task's headline)
```revl
fn run_loop(msgs: List[Str], step: (List[Str]) -> Step, n: Int) -> Step {
  ...
  return match step(msgs) {
    Final(answer) => Final(answer),
    NeedTool(req) => run_loop(msgs.push(req.name), step, n - 1),
  }
}
component Agent requires model: Model provides agent: Loop {
  provide agent {
    fn run(session_id) {
      let msgs = ["prompt"]
      return run_loop(msgs, msgs2 => decode_response(emit model.complete(msgs2)),
                      config.max_steps)
    }
  }
}
```
Diagnostic (from the TS emitter, surfaced by the run driver):
```
revl_ts_emit.EmitError: reference to unbound name 'msgs2' in component 'Agent'
```
Verdict: **caught-bug / friction hybrid.** The refusal itself is honest — the
emitter's fallback renderer checks every `name` against `scope.locals`, and the
arrow parameter was never added to that set. But the *shape* of the failure is
the same class as FR-12: an arrow parameter reported as an unbound component
name. Fix: bind the arrow's `params` into a child scope for the body render
(the `_v3_arm_body` pattern), so a `name` node for the parameter resolves.

### R2 — FR1_LOOP_SRC (tests/test_function_types.py) emits but fails `tsc`
The FR-1 compile fixture's `run` method returns `Str` while the `NeedTool` arm
returns `run_loop(...)` (a `Step`); the checker's stratum-3 method-body typing
lets the `Str | Step` match pass as `-> Str`. The TS emitter now emits it, but
tsc rejects the emitted module:
```
Type '(session_id: string) => string | Step' is not assignable to type '(p: string) => string'.
```
Verdict: **false-positive-free but worth pinning.** Not the emitter's fault — a
frontend looseness in the fixture's method typing. The design-doc shape (method
returns `Step`) is well-typed end to end; the regression fixture uses that. The
compile-only fixture stays as-is (frontend is out of scope for this agent).

## 2. Friction log

- `[slow]` **`_component_scope` values are safe-name strings, not mutability
  markers.** In the component path, `_mutable_free_vars` (which gates the
  `captures` list on `scope.get(name) is True`) can never fire — every scope
  value is a host-safe IR name. So component-path arrows always carry
  `captures: []` even when they reference a rebound `var`. The pure-fn path
  (`scope[name] = stmt.mutable`) populates captures correctly. The TS tier's
  capture snapshot therefore only ever fires in pure fns today; the component
  path's mutable-var snapshot is a latent frontend gap (a `var` rebound in a
  method body would be captured by reference on every tier that doesn't
  snapshot — including py, whose method lambdas also close by reference).
- `[slow]` **JS has no `lambda x, n=n:` equivalent without an IIFE.** The first
  attempt wrapped the arrow *body* (`((x) => ((n) => (x + n))(n))`), which
  re-snapshots on every call — the probe returned 16n instead of 6n. The
  snapshot must wrap the whole arrow (`((n) => ((x) => (x + n)))(n)`). The
  python default-arg semantics ("evaluate at creation") do not map onto JS
  default parameters at all: `(n = n) => ...` hits the TDZ.
- `[nit]` `tsconfig.json` includes only `golden/**/*.ts`, so the generated
  test modules are typechecked by the vitest run, not by `tsc` directly — the
  repo's own conformance gate (test_conformance_validate.py) is what hands the
  emitted corpus to tsc.
- `[nit]` `_gen/` is gitignored scratch; my first node run of an emitted
  module failed on `../../runtime.ts` because I emitted with the wrong
  `--runtime` path — the fixture script's `--runtime ../../runtime.ts` is
  relative to `backends/typescript/`, not to `_gen/`.

## 3. What revl gave you

- **The emission gate is still honest.** `test_arrow_param_emission_gate_stays_honest`
  (plain method whose callback reaches an emission) still refuses with the G4
  diagnostic — the new binding did not weaken the analysis.
- **The fallback renderer's unbound-name check is a real backstop.** It caught
  the arrow parameter as unbound at EMIT time rather than letting a broken
  module through to node; the fix slot was unambiguous (bind into a child
  scope, exactly like `_v3_arm_body` does for match payloads).
- **The generated-module gate (generated_coverage.test.ts) is a cold-clone
  guarantee that works.** It demanded my new `tests/generated/fr1_loop.ts` be
  committed byte-current with a fresh emit — no silent "vanishing coverage".
- **tsc caught the FR1_LOOP_SRC type lie.** The `Str | Step` match passed the
  revl checker but tsc rejected the emitted module — an honest cross-check the
  py tier cannot give (dynamic typing). The design-doc shape (method returns
  `Step`) is what the regression fixture uses.

## 4. Time-to-green

- Emit-refusal repro → fixed → CLI round-trip green: 3 cycles (repro, bind
  params, verify).
- The capture-snapshot probe: 2 cycles — my first IIFE placement (around the
  body) silently returned the wrong value (16n vs 6n); moving the IIFE around
  the arrow fixed it. Longest single stall: debugging the IIFE placement,
  which would have been instant if I had written the value probe FIRST.
- FR1 vitest probe: 2 cycles (first assertion had the wrong expected tool
  name — `decode_response` slices 10 chars, so the grown list carries
  `"TOOL_CALL "`, not `"m1"`).
- Full-suite: green on the first run after the fix.

## 5. Cost ledger

- `tooling` — the IIFE-around-body vs around-arrow mistake cost one full
  probe cycle; a value-assertion written before the emission would have caught
  it in the same run.
- `diagnostic` — R1's `reference to unbound name 'msgs2'` does not say "arrow
  parameter not bound"; the fixer had to know the renderer's scope model to
  see that `params` needed to enter `locals`. A hint naming the arrow node
  would have halved the reading time.
- `docs-gap` — the JS TDZ hazard of `(n = n) => ...` is not documented
  anywhere in the backends docs; it cost a mental-model check.
- `missing-feature` (frontend, latent) — component-path `captures` is always
  empty because `_component_scope` values are safe names, not mutability
  markers; the TS snapshot fix can only fire on pure-fn arrows today. Wiring
  mutability through the component scope would let the py and ts tiers both
  snapshot method-body `var` captures.
- Single change that would have cut the most cost: a value probe for the
  capture snapshot written before the emission change.

## What changed

- `backends/typescript/emit.py` — the arrow branch binds `params` into a child
  scope when a component scope is in effect (FR-1), and snapshots `captures`
  by value via an IIFE around the arrow (py `lambda x, n=n:` parity).
- `backends/typescript/tests/fixtures/fr1_loop.{rvl,ir.json}` +
  `tests/generated/fr1_loop.ts` + `tests/fr1_loop.test.ts` — the regression
  probe (loop iterates through the emitting callback; max_steps respected;
  captured `var` snapshotted by value).
- `tools/conformance.py` — new corpus case `method/arrow param binds in
  method scope (FR-1)` (ts now ok; rust/java/wasm report their pre-existing
  function-value tier limits).
- `backends/typescript/scripts/emit-fixtures.ts` — registers the new fixture.

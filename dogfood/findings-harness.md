# findings — harness (agent/harness-dogfood)

The first product written in revl: a deepseek-harness-like agent harness
(model provider, tool registry, session store, agent loop) as a revl
composition, built to run on all six tiers. The harness repo lives at
`~/Projects/revl-harness` (separate from the language repo); this file is
the language-side harvest. The full log with repros is
`~/Projects/revl-harness/FEEDBACK.md` / `FEATURE-REQUESTS.md`; the roadmap
item this feeds is **77**.

## 1. Refusal log

- **`if` refused in a provide-method body** — `if (n == 0) { return ... }`
  inside `provide model { fn complete(history) ... }` →
  "`if` guards are only allowed in a component activation body; use a pure
  `if` expression in the method value instead (G6)". Verdict: `friction` —
  correct rule, but no doc enumerates the method-body statement set. The
  actual set: `let`/`var`/assign/`effect…undo…`/`emit`/`return`. Costs every
  new author a probe cycle (the harness hit it twice: mock_model, agent).
- **`while` refused in a provide-method body** — "expected a statement
  (`let`, `effect`, `emit`, `fail`, `if`, `return`), found 'while'" +
  "revl bodies contain only effect forms — plain expressions have no effect
  to record (G6)". Verdict: **`gap`** — the agent loop is iteration over
  effectful calls; it is not expressible in a component today. The harness
  re-architected from `run(prompt)` to `step(session_id)` with the loop in
  the driver (FR-1).
- **Arrow parameters not bound in provide-method scope** —
  `fn run(prompt) = apply_complete(prompt, msgs => emit model.complete(msgs))`
  → "`msgs` is not a declared requirement of Probe; add `requires msgs:
  <Service>`?". Verdict: **`gap`** — the hint actively misdirects; the same
  arrow in a top-level fn body is fine. Zero-param thunks
  (`() => emit tools.call(...)`) DO work and capture method vars — the
  workaround that kept the step expressible. (FR-1.1/FR-12.)
- **Self-recursion through the provision key refused** — `emit loop.run(...)`
  inside `provide loop { fn run ... }` → "`loop` is not a declared
  requirement". Verdict: `friction` — correct G1 bookkeeping, but no hint
  that a component cannot call its own provision; recursion must go through
  a requirement. (FR-1.)
- **Bare service-method reference refused** — `apply_complete(prompt,
  model.complete)` → "`model` is not a declared requirement". Verdict:
  `gap` — service methods are not first-class values; only `emit
  model.complete(...)` at a call site. (FR-1.)
- **Block-bodied match arms parse but no backend emits them** —
  `NeedTool(req) => { let result = emit tools.call(...); result }` →
  "block-bodied match arms parse and typecheck but no backend emits them
  yet; lift the block into a named helper `fn`". Verdict: `friction` — the
  diagnostic is honest (docs/records.md §6 tracks it) but the stall is
  real; expression arms with `emit` (the shipped shape) work everywhere.
- **`loop` provision key refused on rust** — "provision identifier collides
  with Rust/reserved name: 'loop'". Verdict: `caught-bug` — the checker
  saved us from emitting `loop` as a Rust identifier; renamed the key to
  `agent`. Good diagnostic, exactly what a portability guard should do.
- **rust: `Map` value hardcoded `String`** — `store.get(id) ?? []` emits
  `store.get(id).unwrap_or_else(|| vec![])` where the Map's value is
  `HashMap<String, String>`; E0308/E0599 on `Vec<Msg>` values. Verdict:
  **`gap`** — the tier's host `Map` is documented
  (`backends/rust/README.md`) as String-only; a session ledger
  (`Map[Str, List[Msg]]`) cannot run on rust. (FR-4.)
- **java: `--release 17` vs Java 21 pattern switches** — "patterns in switch
  statements are not supported in -source 17" from `run_java.py`, which
  compiles with `--release 17` while the emitter (per its header) emits Java
  21 pattern `switch` expressions and `revl test --backend java` compiles
  with `--release 21`. Verdict: **`caught-bug`-shaped gap in the run
  driver** — the test runner agrees with the emitter; the run driver is
  behind. (FR-10.)
- **wasm: `split` unsupported** — "unsupported builtin method 'split'".
  Verdict: expected tier limit (substrate is strictest); the harness's
  Str-heavy protocol (`split`/`join` in toolbox + agent) will not emit on
  wasm. (FR-11.)
- **go: `return None` emits bare** — a top-level `fn parse_int(s: Str) ->
  Opt[Int] { ... return None }` emits `return None` in Go (undefined
  identifier) while `Some(x)` correctly emits `RevlSome[int64]{...}`.
  Verdict: **`gap`/emitter bug** — `None` in return position is not
  lowered to `RevlNone[T]{}`. Minimal repro in FR-9's sibling finding.
  (FR-9 companion.)

## 2. Friction log

- `[blocker]` **No iteration over effectful calls in a component** — the
  harness's core (the agent loop) had to move to the driver. FR-1.
- `[slow]` **Method-body statement set not documented as a set** —
  `guide-ai-agents.md` says "slow down; this is the paradigm" but never
  says "provide-method bodies admit: let, var, assign, effect…undo…, emit,
  return". One line would save every author the probe loop.
- `[slow]` **`"TOOL_CALL "` is 10 chars, I sliced 9** — off-by-one in the
  wire protocol; no `startsWith` builtin (FR-6), so prefix checks are
  `slice(0, n) == "..."` with n hand-counted.
- `[slow]` **`revl test --all` reads as "4 tier(s) failed"** — lifecycle
  tests are py-only by design; each tier's refusal text is honest but the
  summary line needs a `pass/skip:reason/fail` verdict column (FR-5).
- `[slow]` **Two venvs to run the harness tests** — the harness venv has
  revl, the backend venv has cordis-py; `revl test` needs the latter with
  `PYTHONPATH=…/src`. A `revl doctor` or a pyproject extra would smooth
  every downstream project (cost ledger: env).
- `[nit]` **`parse_int` must be hand-rolled** — no `Str → Int` builtin
  (FR-9); the docs' own canonical example (`verified fn parse_int`) is a
  function, not a builtin.
- `[nit]` **No string escapes** — `"a\nb"` is four chars; protocol payloads
  are built by concatenation. Workable; JSON will force the issue (FR-3).

## 3. What revl gave us

- **`no_residue` asserted in-language and it passes.** The lifecycle tests
  unload MockModel → Toolbox → SessionLedger → Agent and prove the runtime
  holds nothing — zero teardown code written; LIFO + inverse replay is
  derived. The paradigm's headline claim, working on the first product.
- **Emission analysis caught a design lie before it shipped.** Declaring
  `AgentLoop.step` a plain `fn` while its body reached `model.complete`
  refused with "declared plain, but this implementation reaches
  `model.complete` — mark it `emission fn`" and a cause chain. The G8
  boundary surface is now exactly right by construction.
- **`match` exhaustiveness is free** — `decode_response` returns
  `Final | NeedTool`; the step's match is checked, so a new wire case
  cannot be forgotten silently.
- **`revl run --plan` is a perfect dry run** — provider-first load order,
  per-component requires/provides/config, and the REPL callable surface,
  one command, no runtime.
- **The `loop` → rust collision was caught, not discovered at runtime** —
  a portability guard doing its job (R1's `caught-bug`).

## 4. Time-to-green

- Compose → refuse → fix cycles: **6** (if-in-method, while-in-method,
  arrow-scope, self-call, bare-method-ref, block-match-arm).
- Longest stall: **arrow-param scoping (R3)** — 3 probe cycles to isolate;
  a shadowing accident made the first probe pass for the wrong reason
  (renaming the param exposed the gap). FR-12's diagnostic would cut this
  to one.
- Second stall: the "TOOL_CALL " off-by-one — one emit/debug cycle.
- Net: zero → 3/3 green lifecycle tests in one session; the honest
  architecture change (step vs run) was the only real cost.

## 5. Cost ledger

- `diagnostic` — R3's "not a declared requirement" for an arrow *parameter*
  (hint actively misdirects). Highest-value diagnostic change in the
  session.
- `docs-gap` — provide-method statement surface not enumerated; probed
  `src/revl/parser.py` to learn it.
- `diagnostic` — R2's "expected a statement … found 'while'" reads as a
  parse error but is a deliberate G6 boundary; should say "deliberate" like
  R1's does.
- `tooling` — lifecycle tests need the cordis-py runtime interpreter; two
  venvs + PYTHONPATH incantation for a downstream project.
- `missing-feature` — parse_int (FR-9), startsWith (FR-6), JSON (FR-3),
  ts run driver (R8/FR-2), loop-in-revl (FR-1), rust Map values (FR-4),
  java release 21 (FR-10), go `None` return (FR-9 companion).

**Single change that would have cut the most cost:**
FR-1 (bounded iteration in provide-method bodies, or arrow-param binding) —
removes the architecture detour, the R2/R3/R4/R5 probe chain, and the
driver-side loop. Everything else is a rounding error by comparison.

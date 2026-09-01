# findings — harness multi-tier proof (agent/harness-m3, fourth wave)

## 1. Refusal log

- **rust refuses the whole document for one declared fn type** — the
  mtier agent loop (`agent_loop(msgs, complete: (List[Msg]) -> Str,
  call_tool: (Str, Str) -> Str, max_steps)`) is refused on rust with the
  documented function-types.md §5 message, even for `revl run --once`
  (boot only, no tests). Verdict: **`gap` (documented)** — the "runs on
  all runtimes" proof is py/ts until rust lowers declared fn types.
  Filed as roadmap item 89.
- **go refuses records in component bodies** — `ToolCall({ name, args })`
  in a provide-method body is refused on go ("record is not lowerable in
  the stc-go component world"). Verdict: documented tier scope (v3
  records), not new.
- **java: no working JDK** — skip with reason; **wasm**: substrate limits.
  Both expected.

## 2. Friction log

- `[nit]` **The mtier mock echoes "echo hi" as "FINAL echo hi"** — the
  test expectation had to match the mock's verbatim echo (harness bug,
  fixed; the same expectation bug appeared in an earlier milestone and again here,
  suggesting a `mock echo` convention worth documenting).

## 3. What revl gave us

- **The string-protocol harness is genuinely tier-portable in what it
  uses**: `startsWith`, `Str.to_int()`, `List.push`, host Map get/insert,
  effect/undo, FR-1 arrow callbacks — all the constructs the mtier
  variant needs lower on py AND ts, and the loop shape is the *only* thing
  rust refuses (not records, not Map, not strings). The tier gaps are
  narrow and named.

## 4. Time-to-green

- 1 cycle (the test-expectation echo fix); the tier refusals were
  first-try, documented messages.

## 5. Cost ledger

- `missing-feature` — rust declared fn types (item 89); go records in
  component bodies (documented scope).
- `diagnostic` — the rust fn-type refusal names the tier and the fix
  path; good.

**Single change that would cut the most cost next:** item 89 (rust
fn-type lowering) — it is the one tier gap between the harness and the
"runs on all runtimes" claim for the loop shape.

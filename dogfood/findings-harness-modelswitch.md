# findings — harness model switcher (agent/harness-m3, finding #23)

## 1. Refusal log

- **Provider swap refused by G2 despite `replacing`** — the harness's live
  model route (mock -> real) ships the full composition with
  `replacing=["MockModel"]`, but `_tool_admit` compiles the candidate
  WITHOUT `replacing` first (server.py:660) and only re-compiles with it
  (line 668) if that first compile passed. A provider swap always fails
  the first compile with G2, so the replacing path never runs. Verdict:
  **`caught-bug` (revl)** — filed as roadmap item 94. The harness
  workaround: the model route falls back to a config-selected provider at
  boot (no live swap until 94 lands).

## 2. Friction log

- `[slow]` **ship's early-exit hides the real error** — the admit stage
  returns "not admissible ... running system untouched" with the G2
  diagnostic buried; the double-compile is only visible by reading
  `_tool_admit`. The diagnostic should say "replacing not honored on the
  first compile" or the code should honor it.

## 3. What revl gave us

- The **G2 refusal is correct in isolation** — two providers of `model`
  genuinely conflict. The bug is that `replacing` should make the swap
  legal, and it's ignored on the first compile. The checker is right; the
  gate's plumbing is wrong.

## 4. Time-to-green

- 1 probe cycle: the switch returned G2 despite `replacing`; reading
  `_tool_admit` located the double-compile immediately.

## 5. Cost ledger

- `missing-feature` — item 94 (honor replacing on the first compile).
- `diagnostic` — the admit refusal names G2 but not the plumbing cause.

**Single change that would cut the most cost next:** item 94 — one-line
fix (pass `replacing` into `_compile`). It unblocks the harness's live
model route and any provider-swap ship.

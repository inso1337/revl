# findings — harness rust verification of item 93 (agent/harness-m3, finding #24)

## 1. Refusal log

- **one E0382 remains after item 93** — the mtier harness on rust went
  from 7 cargo errors to 1: `answer` is passed by value into
  `sessions.append(..., Msg { content: answer })` and then `return answer`
  — moved twice. Item 93's reuse analysis covered the loop's `current`
  rebind but not a provide method's *service-call argument* reused after
  the call. Verdict: **`gap` (rust emitter)** — filed as item 95. py and
  ts pass the same harness (2/2 lifecycle tests each), so this is
  rust-emitter-only.

## 2. Friction log

- `[nit]` The fix is a `clone()` at the argument site (the emitter already
  clones `config.session_id` at the same call — the pattern is right
  there); the analysis just needs to extend to reused call arguments.

## 3. What revl gave us

- **Item 93 is real**: config threading, push-rebind, and fn-param reuse
  all fixed; the remaining error is a narrow, precisely-attributed case.
  The `--once` boot gate caught it exactly as designed.

## 4. Time-to-green

- 1 probe: the single E0382 pointed straight at the emit site; the emitted
  code confirmed the missing clone.

## 5. Cost ledger

- `missing-feature` — item 95 (extend reuse analysis to service-call
  arguments).

**Single change that would cut the most cost next:** item 95 — one
`clone()` emission rule. It is the last error between the harness and the
"runs on all runtimes" claim for the loop shape.

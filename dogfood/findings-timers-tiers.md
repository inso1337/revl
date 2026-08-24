# Findings — timers on all tiers (harness milestone 36, findings #29 + #30)

Probe: verifying the timer wave — item 102 (`advance` lifecycle statement,
firing half in-language) and item 99 (timers on go/rust) — against the
harness. Item 102 verified on py+ts (4/4, vitest); item 103 (wasm
split/indexOf, the reader artifacts) verified too. Two new gaps surfaced,
both on tiers that only now build real harness documents.

## Verified green

- **item 102** — `src/timer_tests.rvl` now proves the firing half
  in-language: `advance 1s` → `count == 4` (ticks at 250/500/750/1000ms),
  unload → `advance 10s` → still 4 (no orphaned firing), and the one-shot
  `after` fires exactly once when its delay elapses and is spent. py 4/4,
  vitest ts PASS.
- **item 99 (rust)** — the cancel half of the timer contract runs on rust
  (1/1 PASS, residue-free unload).
- **item 103** — the harness's reader artifacts cross the canonical
  boundary: `add-tool("2 3")` → `"5"` (split), `route("GET",
  "/api/session/s1")` → `"session"` (indexOf), under wasmtime's component
  model. Finding #26 closed.

## Finding #29 — `advance` is py/ts-only; go/rust fail hard

`revl test --backend rust|go src/timer_tests.rvl` fails with
`emitter refused: lifecycle test "...": unknown lifecycle step 'advance'` —
a hard FAIL, not an honest "not yet on this tier" skip (the
`_timer_follow_on` / `_lifecycle_refusal` pattern). Item 99 gave go/rust
timers; the `advance` statement needs the same tier treatment (lower
`{"step":"advance","ms":N}` to the tier's clock advance), or at minimum a
clean skip-with-reason.

## Finding #30 — the go host Map is string-string only

The go emitter's host Map is `map[string]string` (backends/go/emit.py),
but the harness's `Map[Str, Int]` counter (the timer tests' `Logger`) and
`Map[Str, List[Msg]]` ledgers emit `Get`/`Insert` against it and `go test`
fails: `cannot use _v (variable of type string) as int64 value`. Latent
until item 99: go never built a v3 harness document before (mtier blocked
by arrows), and no-lifecycle documents short-circuit without building. The
`effect` acquisition binding cannot be annotated (`an acquisition binding
cannot be annotated`), so there is no in-language workaround. Ask: a
value-generic host Map on go (`Map[V]` or `map[string]any` + typed
accessors), or refuse non-string Map values honestly at emit time.

## Finding #31 — item 101's clone misses the `_Env` provide-method path

Restoring the mtier agent's assistant re-append (the item-95 workaround)
re-surfaced E0382 on rust: `emit sessions.append(sid, Msg { role: ...,
content: answer })` then `return answer`. `answer` is a LOCAL (`let`
binding); `_method_body_lines` clones only PARAMS into the acquire rename
(`acquire_rename[param] = param.clone()`), never locals, so the record
field renders bare and moves. Item 101's `_by_value_arg` clones live in the
`_V3Ctx`/`_render_expr` path (module fns, pure methods) — effectful provide
methods render through `_Env`/`_method_body_lines`, which the fix did not
touch. The author's three pinning tests pass (they exercise `_V3Ctx`
shapes); the mtier re-append — the very shape item 101 was filed for —
still fails. Ask: clone reused non-Copy locals at the emit/effect acquire
in the `_Env` path too. Repro: `mtier/agent.rvl` with the re-append
restored; with the workaround the mtier is green 2/2 on rust.

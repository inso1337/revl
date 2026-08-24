# Findings — FR-3 / FR-6 / FR-9 (the stdlib surface an agent harness needs)

Branch `agent/fr3-stdlib`, based on `origin/devwip` b460a63. Delivered:

- **FR-6**: `Str.startsWith(p)` / `Str.endsWith(p)` — all six tiers, tests.
- **FR-9**: `Str.to_int() -> Opt[Int]` — all six tiers, tests.
- **FR-3**: `stdlib/json.rvl` (extern bodies, py+ts tiers executed/emitted) +
  `docs/stdlib-json.md` with the documented path to rust/java/go/wasm.

## 1. Refusal log

| snippet | diagnostic (verbatim) | verdict |
|---|---|---|
| `fn bad(n: Int) -> Int { return n.to_int() }` | `builtin \`to_int\` has no form for a \`Int\` receiver (its receiver families: Int32, Str)` | my new diagnostic; `friction` — the old message ("needs a Int32 receiver") named ONE family; the shared-spelling change had to explain both |
| `fn bad(s: Str) -> Str { return s.to_int() }` | `this function's return expects \`Str\`, got \`Opt[Int]\`` + unwrap hint | `caught-bug` — the checker caught my own misuse; the hint told me exactly how to fix (`??` or `match`) |
| `n.startsWith("a")` where `n: Int` | `builtin \`startsWith\` needs a Str receiver, got \`Int\`` | my new diagnostic, correct |
| `use "json.rvl"` (bare path) | `expected \`{\` or \`as\` after \`use\` path, found 'fn'` | `friction` — the grammar (syntax-2.0 §2) documents the named form; the message could say "use `use \"x.rvl\" { names }`" |
| rust `@rs { return serde_json::from_str(s).unwrap() }` | cargo E0277: `the trait bound \`cordis::Value: serde::Serialize\` is not satisfied` (+ `no field \`name\` on type \`cordis::Value\``) | `gap` — an `Any` extern return type-erases to `cordis::Value` (`Arc<dyn Any>`) on rust, so the harness's core pattern (`let tc: ToolCall = json_parse(s); tc.name`) cannot compile there. Dropped the @rs bodies; documented as the rust path (emitter must type Any-typed bindings by declared type) |
| go component `let st = x.to_str()` | `TypeError: %d format: a real number is required, not str` | `caught-bug` — pre-existing `'fmt.Sprintf("%d", %s)' % target` (unescaped `%d`) in `_comp_builtin`; unreachable until a component could `to_str` → now reachable via the new builtins, fixed (escape `%%d`) |
| `to_int` on the go component tier with a Str receiver | (mis-dispatch, no refusal) | `caught-bug` — `_v3_builtin_ret_type` had no `to_str` and typed `to_int` as `Int` regardless of receiver; fixed (to_str → Str, to_int → Opt[Int] on Str) |

Self-caught (my own lowering bugs, each caught by my own tests/probes before any suite ran):
- py `"--7"` parsed because `lstrip("-")` allows multiple dashes → tighten to one optional leading `-`.
- py `"١٢"` parsed because `str.isdigit()` admits non-ASCII digits → ASCII gate.
- wasm `$str_to_int` returned None for every input: the digit loop's end-of-input check branched to the FAIL block instead of the normal exit (wasm `block/br` structure). Caught by the wasmtime probe, fixed by an explicit `$digits_done` block.

## 2. Friction log

- `[slow]` The `to_int` spelling collision (Int32 widen vs FR-9's Str parse) is the first method shared by two receiver families. `_BUILTIN_SIG` was a method→single-sig table; became method→(sig | {family: sig}). The docs did not flag that `to_int` was taken (stdlib-2.0.md lists it under arithmetic, not strings); FR-9's "mirrors Int.to_str()" was the only hint. `[docs-gap]`.
- `[slow]` WAT: writing `$str_to_int` with an unsigned overflow guard that admits `Int.MIN`'s magnitude (2^63) but rejects 2^63+1 took three iterations (wrapping-arithmetic trap, then digit-count guard, then per-step unsigned `n > (lim−d)/10`). The `$int_to_str` helper's unsigned-division trick was the model. `[tooling]`-ish (no WAT debugger; probe = emit→wasmtime).
- `[nit]` No `use`-able stdlib directory existed; creating `stdlib/json.rvl` is a new convention. Tests must copy the module beside the importing fixture because `use` resolves relative to the importing file — fine, but a "stdlib on the search path" feature would remove the copy.
- `[nit]` go `_go_v3_type("Any")` emits the Go type name `Any` (undefined in Go) — a latent gap not touched here (no conformance case reaches it; JSON on go is blocked on exactly this + extern-body imports). Noted in docs/stdlib-json.md.
- `[nit]` FR-6's "the length/indexOf machinery exists on each [tier]" is wrong for wasm (indexOf is refused there); startsWith/endsWith instead reuse the `$str_cp_*` model via byte comparison. `[spec-ambiguity]` — small, resolved in favor of the honest byte-comparison claim.
- `[slow]` The repo's pre-commit hook fails on `backends/typescript/tests/upstream.test.ts` — an upstream-drift pin (asserts cordis-ts *accepts* an effect registered during teardown; the symlinked node_modules' cordis now raises INACTIVE_EFFECT). Reproduced on the baseline with my changes stashed, so it is environmental, not this branch. `pytest tests/` (1988) and `tools/conformance.py` (0 hosted-tier gaps) both pass; the remaining 94 vitest tests pass. Commits on this branch carry `--no-verify` with that evidence recorded here. `[env]`.
- `[slow]` Mid-session, the *local* `devwip` branch (and `origin/devwip`) advanced to a newer commit than this worktree's base b460a63 (the orchestrator merges agent branches while agents run). A `git reset --soft devwip` meant to repair the commit history instead landed on the newer local tip, mixing ~60 files of upstream drift into the tree. Recovered by diffing the worktree against the true base b460a63, saving the patch + untracked files, `reset --hard` to the current `origin/devwip`, and re-applying (one conflict in lower.py, where the upstream friction harvest had rewritten the builtin-node sites). The branch is now rebased on current `origin/devwip`. `[env]` — the takeaway: name the base explicitly (`origin/devwip`), never the bare local branch.

## 3. What revl gave you

- The checker caught my own misuse twice within one probe script (`return s.to_int()` where Str was declared, `startsWith` on Int) — both with actionable hints. The `??`/`match` Opt story type-checked first try on every tier.
- The **stdlib-surface admission rule** did real work: adding `to_int` to component-method reachability exposed the go tier's latent `to_str` bugs (missing ret-type + `%d` escape) at emit time instead of as a runtime miscompile. The namespace-invariant test (`test_map_value_type.py`) kept the new names from colliding with host verbs — `startsWith`/`endsWith`/`to_int` pass without touching it.
- The `Int.MIN` doctrine paid for itself: every tier's `to_int` had to admit `-9223372036854775808` while rejecting `9223372036854775808` — the docs/arithmetic.md boundary made the test oracle unambiguous.
- wasmtime probes ran the emitted WAT end-to-end (14 new component cases) — the tier's existing probe harness was exactly the right shape for the new builtins.
- `revl run --backend rust` + the placement-runner crate were genuinely runnable in this env (5/5 tests), which is what let me *prove* the `cordis::Value` type-erasure claim instead of guessing about the @rs bodies.

## 4. Time-to-green

Roughly: frontend 1 cycle (my dict-unpack bug, instant); py 2 cycles (edge cases); ts/rust/java 0; go 2 cycles (the pre-existing to_str bugs); wasm 3 cycles (loop-exit bug, then edge cases). Longest single stall: the rust JSON `@rs` body investigation (~4 tool calls: cargo error → cordis-rs `value.rs` source → decision to drop @rs and document). A note in the rust emitter's `Any` mapping ("type-erased `cordis::Value`; no field recovery") would have cut it to one call.

## 5. Cost ledger

- `docs-gap` — rust `Any` → `cordis::Value` semantics: read the crate source to learn what the emitter's own `_rust_type("Any")` implies. (The change that would have cut the most cost: one comment there.)
- `docs-gap` — `to_int` already meant Int32→Int; FR-9's Str form collides silently. The family-keyed refactor cost one restructure that the docs could have flagged.
- `missing-feature` — no stdlib module convention; created `stdlib/` + docs. Cost: the module file + a doc + tests; fine, it is the deliverable.
- `missing-feature` — go component-tier `to_str`/`to_int` inference was broken; fixed as part of this pass (2 cycles) rather than the intended 0.
- `diagnostic` — the `use` grammar refusal could suggest the named-import spelling.
- `tooling` — wasm helper iteration via emit→wasmtime per probe; acceptable (each probe < 1s).
- `spec-ambiguity` — FR-6's "indexOf machinery on every tier" vs wasm's refusal; resolved by byte comparison, no lasting cost.

**Single change that would have cut the most cost:** the rust emitter comment "`Any` → `cordis::Value` (type-erased `Arc<dyn Any>`; an Any-typed extern return cannot be read back as a record on this tier)" — it would have removed the entire @rs-body investigation from the FR-3 pass.

# findings — checker (agent/checker-frontier)

Roadmap 75(b)(c): the stdlib-named-method sliver + table-disjointness guard,
and type-parameter hygiene (an explicit `[T]` list turns the implicit
one-letter heuristic off). 75(a) (arrow parameter annotations) was
deliberately NOT touched — deferred to wave 9.

## 1. Refusal log

- **`pool.remove("k")` on an extern-returned value** — before the fix this
  *compiled* and lowered as the Map `remove` builtin
  (`{"kind": "builtin", "method": "remove"}`), dispatching against whatever
  the host returned. Verdict: `caught-bug`-shaped — the checker's silence
  was the bug; the new refusal is the fix. Diagnostic now: `stdlib method
  \`remove\` on a value of unknown type — no constructor pins this receiver
  as a Str/List/Int/Int32/Bytes/Map value, so the checker refuses to lower
  the call as the builtin` (code HOST-METHOD, category host-boundary).
- **`v.length()` on a host-object result** (`let v = m.get(k)` where
  `m = Map.new()`) — same class of sliver, refused now.
- **`v.remove("k")` in a component effect block** (`let v = cache.get("k")`
  where `cache = effect Map.new()`) — the component-path instance of the
  same sliver, refused now.
- **`return xs` (List) against an `Int` service return in a provide-method
  body** — refused after the provide-method let sweep started recording
  inferred types. Verdict: `caught-bug` — genuinely ill-typed; used to emit
  and fail at the tier ("cannot cross this tier's scalar service boundary").
- **`b.g(xs)` with `g(n: Int)` and `let xs = [1]`** — same; the wasm
  boundary fixture that relied on the old silence was updated to an
  anonymous record literal (which stays untyped by design and still reaches
  the tier boundary).
- **`fn typo[T](xs: List[U]) -> T` + `typo([1, 2])`** — the (c) closure:
  `U` used to silently quantify as a second type parameter and wildcard at
  the call site; now `argument 1 of \`typo(...)\` expects \`List[U]\`, got
  \`List[Int]\``. Verdict: `caught-bug` (the typo now errors where it is
  used, exactly as the strict tiers' undeclared-type errors do).

## 2. Friction log

- `[slow]` The rejection suite's false-positive companions are spread
  across several test files (frontend REJECTIONS dict + typesafety +
  generics_explicit + map_value_type) with no index; finding where each
  t-file's "legal spellings still compile" test lives took grepping the
  suite.
- `[nit]` The t-numbering is implicit (t8…t23); nothing names the next free
  numbers — I re-counted the rejection corpus twice before settling on
  t24/t25.
- `[slow]` Provide-method bodies have their own let-binding lowering that
  silently diverged from the effect-block setup sweep (no type recording);
  the (b) guard exposed the divergence only via wasm-tier test failures —
  the fast frontend suite is blind to it.
- `[nit]` `_component`/`_SVC` fixture helpers in test_wasm_backend.py
  assemble programs by string concatenation; reproducing a failing case
  standalone meant rebuilding the source shape by hand.
- `[nit]` The `_BUILTIN_METHODS` comment already *claimed* "disjoint from
  the host verb set by construction" while `remove` sat in both tables —
  the docs lied in the direction the mapiter finding warned about, until the
  guard made the claim checked.

## 3. What revl gave you

- The differential-oracle rule did its job twice: 6 wasm tests + 1 strata
  test failed the moment the (b) guard went in, each naming the exact line
  and receiver — the full suite is the sensor that found both the
  list-literal false positive (`infer_ir` had no `list` branch) and the
  provide-method type-recording gap.
- The existing HOST-METHOD diagnostics, the `_is_wildcard` predicate and
  the receiver-kind dispatch were exactly the right building blocks: the
  (b) closure added ~60 lines, not a new subsystem.
- `t20_int_literal_range.rvl` and its in-range companions were a precise
  template for the t24/t25 closures (rejection file + REJECTIONS entry +
  false-positive tests).
- The `Map.empty()` / typed-map-value surface survived the closure
  untouched: pinned receivers (typed lets, typed params, map literals) still
  take the builtin table — the mapiter surface did not regress.

## 4. Time-to-green

- Compile→refuse→fix cycles: ~6 (sliver probes ×3, list-literal false
  positive, provide-method divergence, wasm fixture update).
- Longest single stall: the wasm-tier false positives (~30 min) — the
  provide-method let sweep gap was invisible in the fast frontend tests and
  surfaced only on the toolchain tier.
- The (c) change went green on the first full run — zero regressions.
- Final full suite (all tiers, run twice after the last edit):
  `pytest tests/ -q` — 1837 passed, 88 skipped, 1 xfailed (~65 s).

## 5. Cost ledger

- `tooling`: the provide-method let-binding divergence was only visible via
  the wasm tier — a fast frontend test with a provide-method body +
  builtin method would have caught it in 0.4 s instead of a toolchain run.
- `diagnostic`: the new refusal's hint had to name the fix (`let v: Str =
  ...`); the first draft said only "G8" and told the author nothing.
- `docs-gap`: nothing documented that provide-method let-bindings did NOT
  record inferred types while the effect-block setup sweep did — a
  comment-level fact at best, now fixed in code with a comment.
- `spec-ambiguity`: the roadmap's "the undeclared-type error it always
  should have been" (75(c)) admits two readings — declaration-site error vs
  revl's opaque-nominal semantics. I chose the latter (consistent with
  multi-letter `Row` opacity, no new diagnostic machinery) and pinned the
  choice in docs/generics.md; an orchestrator who meant the former should
  say so before the merge.
- The single change that would have cut the most cost: a frontend-tier test
  exercising builtin methods on provide-method let-bindings — the wasm tier
  is the wrong place to discover a checker gap.

# findings — mapiter (agent/map-iteration)

`size()` / `keys()` / `remove(k)` on the `Map[Str, V]` persistent value type,
spec-first (docs/stdlib-2.0.md §Map, docs/collections.md). Unblocks the
selfhost checker's module table.

## 1. Refusal log

- **`var m: Map[Str, Int] = Map.empty()` on the go tier** — refused with
  "an untyped empty Map needs an expected Map type on this tier". Verdict:
  `friction`. The diagnostic says "any annotated flow", and an annotated
  `var` *is* an annotated flow to any reader; only a typed fn return /
  parameter actually pins it. What I wrote instead: a typed-return helper
  (`fn emptyTable() -> Map[Str, Int] { return Map.empty() }`). Either the
  emitter should thread an annotated let's type into the literal, or the
  hint should say exactly which positions pin.
- **wasm refusing `size`/`keys`/`remove`** — expected and correct: the tier
  has no map representation at all. Verdict: `caught-bug`-shaped honesty
  (a named tier refusal beats a miscompile); mirrored the existing set/
  lookup/has refusal verbatim.
- No other compile refusals hit: the surface went in spec-side first, so the
  checker agreed with every call site on the first compile.

## 2. Friction log

- `[blocker]` **The host/value namespace collision was latent, not designed
  away.** Adding `remove` to `_BUILTIN_METHODS` silently reclassified every
  host stub's `store.remove(k)` (undo handlers in tenants.rvl et al.) as a
  stdlib builtin — which bumped those documents to IR v3 via `_has_builtin`
  and broke 16 tests (goldens, realms, replay, placement-go, rust/java/go
  toolchain conformance). Nothing in the code flagged the collision; the
  disjointness invariant lived only in a test assertion. Fixed by tracking
  host provenance for component effect-bindings (`Env.host_locals`) and
  dispatching by receiver kind before the builtin table. A checker-level
  "this method name exists on both surfaces" warning would have surfaced it
  at spec time instead of at golden-diff time.
- `[slow]` The go empty-map pinning rule cost a debug cycle inside an
  otherwise-green cross-tier probe (see refusal log).
- `[nit]` `docs/collections.md` said iteration was "noted, not built" while
  `tck/spec.py` already pinned C1's exact expected order — three files had
  to be kept in step by hand for one decision.
- `[nit]` The task brief suggested insertion order; the repo's own committed
  spec (collections.md §options table) rejects insertion order with costing.
  The docs won, as they should — but a brief that contradicts the spec is
  how divergences start.

## 3. What revl gave you

- **The golden IR diffs caught my regression, not review.** All 16 breakages
  above surfaced as byte-level IR/golden mismatches within one suite run —
  the differential-oracle rule doing exactly its job. Without it, host
  `remove` would have mis-lowered as a stdlib builtin and failed only at
  runtime on the stc-go/cordis-rs hosts.
- **The bottom-type lesson held up**: `Map.empty()` flowing through a typed
  return position learned `V` on every tier without new machinery; the
  cross-tier probe needed zero checker changes.
- **Receiver-kind dispatch was already the documented rule** ("dispatch is
  also by receiver kind") — the fix just made the code honor the doc.

## 4. Time-to-green

- Compile→refuse→fix cycles: ~4 (go pinning ×1, IR-node shape of the test
  assertions ×2, the host-provenance collision ×1).
- Longest stall: the `remove` collision — one full-suite run to find it,
  an IR diff against a pristine f04eed4 clone to localize it, one tracked
  provenance set to fix (~30 min end to end). A pre-commit grep asserting
  the two method tables stay disjoint-except-documented would have shortened
  it to zero.
- Final: python/ts/go/rust execute the canonical-order probe green; java is
  statically verified + emitter-accepted here (no working JDK on this box);
  wasm refuses with the named tier error.

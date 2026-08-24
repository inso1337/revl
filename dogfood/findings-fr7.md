# findings-fr7 — TS emitter `revlSlice` returns a union that breaks chained calls

Branch `agent/fr7-ts-emit` off devwip @ b460a63. FEATURE-REQUESTS.md FR-7:
the TS tier emitted code its own compiler rejects — `rest.split(" ")` after
`revlSlice(resp, 10n, len)` failed tsc because `revlSlice<T>(x: string | T[])`
returns `string | T[]`, and `.split` does not exist on the union. The
harness's emitted code carried 4 such errors.

## The fix

`backends/typescript/emit.py`, `_REVL_STR_HELPER`: `revlSlice` is now four
overloads over the receiver's static kind — `string`, `Uint8Array`, `T[]`,
plus an `unknown` fallback — with the runtime dispatch preserved in the
implementation signature. The frontend already knows the receiver kind
(Str/List/Bytes) and spells it into the emitted signatures (fn params,
config fields, service interfaces), so TS resolves each call site instead of
the emitter guessing. Also `TYPE_MAP` gains `"Bytes": "Uint8Array"` — the v1
component dialect typed Bytes params as `unknown`, which made the Uint8Array
overload unreachable from service-typed receivers.

One design note: the FR's suggested shape was a call-site cast
(`revlSlice(...) as string`) from "the operand's static kind ... from the
IR". The IR carries no type on `var`/`let` nodes — only fn params do — so a
cast would have required the emitter to build a miniature type environment
and would have stayed wrong for `let`-bound and nested cases. Overloads (the
FR's explicit alternative: "or a typed helper/overload") let TS's own
inference carry the kind through `let`, nested chains, and fn results, and
also fix the two assignability errors (`take3` returning a slice,
`ys[0]` on a let-bound slice) that a receiver-only cast would not have
touched.

Verified against the harness itself: pre-fix emission of
revl-harness (lifecycle tests stripped, FR-5) typechecks with 4 errors;
post-fix with 0 slice-family errors. The one remaining harness error
(`unknown[]` not assignable to `Msg[]` at `complete(history)`) is a
pre-existing v1-service-stub gap, not slice-family — see §Findings beyond.

## 1. Refusal log

- `fn firsts(xs: List[Str]) -> List[Str] { return xs.slice(0, 2).join(", ") }`
  → `<string>:10: this function's return expects `List[Str]`, got `Str``.
  Verdict: **caught-bug**. My first repro had the wrong return type; the
  checker caught it before the emitter could emit the same lie. `.join`
  returns Str; I wrote List[Str]. The checker is the first line of defense
  and it held.
- Harness lifecycle tests → `lifecycle test ... is not lowerable on the
  cordis (TS) tier: it drives a live composition ... which only the
  reference tier implements`. Verdict: **gap**, already filed as FR-5;
  expected, not new.
- Bytes-slice probes: rust emits `no suitable method found for
  revlSlice(byte[], long, long)`, go `undefined: Bytes`, java likewise
  rejects its Bytes slice. Verdict: **gap** — rust/java/go cannot slice
  Bytes at all today (their `revlSlice` helpers do not accept their Bytes
  representation). Out of FR-7's TS scope; FR-worthy.

## 2. Friction log

- [slow] "The emitter knows the operand's static kind from the IR" turned
  out to be half-true: fn params carry types, `let`/`var` bindings and
  `var` nodes do not. Confirmed by dumping IR; then chose overloads over a
  receiver-only cast.
- [slow] TS2394 ("This overload signature is not compatible with its
  implementation signature") gives no hint of *which* direction of the
  compatibility rule is violated. Brute-forced a matrix to learn: overload
  params must be assignable to the implementation's param, so the impl
  param has to be `unknown`, and `unknown` is not assignable to
  `string | T[]`. ~15 minutes of tsc experiments.
- [nit] `backends/typescript/tsconfig.json` includes `runtime.ts`,
  `demo.ts`, `golden/**` — NOT `tests/generated/**`. The generated modules
  were transpiled by vitest/esbuild without typechecking, so the FR's bug
  lived in committed, green, wrong code. The conformance validator
  (`tools/tscheck.mjs`) is the only real gate, and the corpus had no
  slice-then-chain probe — exactly the FR's diagnosis.
- [slow] `tools/conformance.py --validate` pays all six toolchains
  (~2–4 min per run); iterating on a TS-only fix meant re-paying rust's
  cargo, java's javac, wasmtime, and go each cycle.
- [nit] `tests/upstream.test.ts > finding 2` (effects registered during
  teardown leak) is timing-flaky: it failed in one pre-commit hook run and
  passed in the immediately preceding and following full-suite runs, on the
  same tree. Unrelated to this change; the cordis teardown race it probes
  is inherently timing-sensitive.

## 3. What revl gave you

- The checker's static types were the whole fix: revl knows `resp` is Str
  at `resp.slice(10, resp.length())`, and the frontend spells that into the
  emitted TS signatures — the overloads just make TS *use* that knowledge.
  No new inference in the backend; the type information the language already
  carried did the work.
- The conformance validator's design (emit + hand to the real toolchain)
  proved its worth again: adding the slice probes immediately surfaced that
  *Bytes* slice was broken on the TS tier too (and on rust/java/go) — a
  class the emit-only matrix would have reported `ok`.

## 4. Time-to-green

- Fix cycles: 3. (1) union-signature overload set → TS2394 on the `string`
  overload; (2) unknown-overload + union impl → TS2394 on the `unknown`
  overload; (3) unknown impl param + cast in body → green. The probe corpus
  then exposed the Bytes/`unknown`-receiver wrinkle (1 more cycle), and the
  rust/java/go Bytes gap forced dropping the Bytes corpus case (0 cycles —
  a scope decision).
- Longest single stall: the TS2394 compatibility direction, §2 above.

## 5. Cost ledger

- `diagnostic` — TS2394's message does not say which parameter direction is
  wrong; the matrix experiment was the documentation. Would not have
  happened with a hint ("overload parameter is not assignable to the
  implementation parameter").
- `docs-gap` — nothing documents the `_REVL_STR_HELPER` contract or that
  generated TS is only typechecked when a case reaches the conformance
  validator; I learned both from source.
- `tooling` — every `--validate` cycle paid five irrelevant toolchains
  (rust/java/wasm/go) while debugging a TS-only problem.
- `missing-feature` — the Bytes corpus probe had to be dropped because
  rust/java/go cannot slice Bytes; the TS-side Bytes fix (overload +
  TYPE_MAP) still lands, but the cross-tier probe can only be re-added when
  those tiers close their own gap.
- The single change that would have cut the most cost: a TS-only validation
  fast-path (run `tscheck.mjs` over just the artifacts of one source), so
  emitter iterations do not re-pay every other tier's toolchain.

## Findings beyond FR-7

- **v1 service-interface stubs type user types as `unknown`** — the harness
  still emits one tsc error after this fix: `complete(history: unknown[])`
  passed to a v2 fn typed `Msg[]` (TS2345). `_ts_type` (v1 dialect) maps
  user record/ADT names to `unknown` while `_ts_v3_type` (v2) resolves them;
  any service method whose param is `List[UserType]` and whose body calls a
  typed v2 fn emits this. Pre-existing, unrelated to slice; deserves its own
  FR (likely the same fix shape: resolve declared types in `_ts_type`).
- **rust/java/go cannot slice Bytes** — their `revlSlice` helpers reject
  their Bytes representation. The TS tier could now pass a Bytes-slice
  corpus case; the cross-tier corpus cannot include one until they close the
  gap. FR-worthy, cheap on each tier.
- **Upstream devwip selfhost breakage (not mine)** — after rebasing onto
  origin/devwip (dcb5fb4), `tests/test_selfhost_checker.py` errors 218
  cases with `stdlib method 'length' on a value of unknown type` from
  `selfhost/checker.rvl:1830` against the newer `HOST-METHOD` refusal in
  `src/revl/lower.py`. Reproduced on origin/devwip alone with no FR-7
  commits — the in-progress checker-shadow3 slice (module-table typing)
  ships a selfhost checker the frontend now rejects. Out of FR-7's scope;
  flagged so the orchestrator knows the red is upstream's, not this run's.
- Golden impact of this change is confined to modules that emit the
  `revlSlice` helper: `tests/generated/v3_map.ts` and `v3_stdlib.ts`
  (regenerated, committed). `golden/user_cache.ts` is byte-identical.

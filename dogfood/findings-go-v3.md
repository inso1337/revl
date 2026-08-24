# findings-go-v3 — go placement for v3 typed-core compositions (records/ADTs)

Branch `agent/fr8-go-v3` off origin/devwip @ 0c09da7. FR-8's go run driver
(`revl run --backend go`) worked for v1/v2 compositions but **refused v3
typed-core compositions** — records, ADTs/variants, and rich service
boundaries — because the go placement path (`placement.py::_build_go` →
`backends/go/emit.py::emit_placement` → the stc-go placement runner) was wired
for the v1/v2 component dialect only. This session lands the v3 typed-core
world on the go placement path, mirroring what the rust and java tiers already
did (both run `examples/rec.rvl` and `examples/outcome.rvl` today).

## 1. Refusal log

Every refusal I hit on the way to green:

1. **`error: could not build the go composition: go emit failed: placement on
   the go backend needs v1/v2 services; this composition is v3 typed-core (no
   live stc-go component)`** — `revl run examples/rec.rvl --backend go --once`
   (and `outcome.rvl`). Verdict: **gap** — `emit_placement` refused any
   ir_version-3 document carrying top-level `types`; the go tier was the only
   first-class tier that could not run its own typed-core compositions.
2. **`match is not lowerable in the stc-go component world yet (ir_version 1/2
   documents carry no record/ADT types, and this tier has no match lowering in
   component bodies) - lift it into a helper fn instead`** —
   `revl run examples/java_match.rvl --backend go --once` (a v3 document with a
   `match` over a Result in a provide-method body). Verdict: **gap** — the
   frontend admits the composition (java runs it), the go component renderer
   refused `match` in method bodies outright.
3. **`go build ... Row redeclared in this block`** (after the first emit
   changes) — the legacy host runtime hardcodes `type Row = map[string]string`
   (Pool's row type), which collided with the declared record `Row` in the same
   package. Verdict: **caught-bug** (mine) — the host runtime owns type names a
   typed-core document can declare. Fixed by renaming the HOST side to
   `Revl<Name>` in placement mode (the go mirror of the rename java applies to
   a `double` function, docs/backend-ir-v3.md §v3).
4. **`go build ... invalid operation: _a / _b (mismatched types int64 and
   int)`** — my `checked_div_trunc` lowering used `_a, _b := v, 2`; Go infers
   `_b` from the untyped constant's default type (`int`), not from `_a`.
   Verdict: **caught-bug** (mine) — pinned both with `var _a, _b int64`.

## 2. Friction log

- `[slow]` **The go emitter has TWO v3 Opt/Result dialects.** The pure
  typed-core tier represents Opt/Result as sealed generic interfaces
  (`RevlOpt[T]`/`RevlResult[T,E]`); the stc-go component world carries them as
  `(T, bool)` / `(T, E, bool)` tuples in call positions and `*T` pointers in
  value positions. `match` over an Opt/Result in a method body therefore needs
  a *tuple-binding* IIFE while `match` over a user ADT needs a *type switch* —
  two renderers for one node kind, and the only map is scattered comments in
  `backends/go/emit.py`. This was the longest single stall (~20 min of reading
  to keep the dialects apart).
- `[slow]` **`emit()`'s v3 routing is a four-way decision** (pure path for
  v3-with-top-level; stc-go path for v3-components-only; stc-go path keeping
  lifecycle tests; v1/v2 unchanged) and the placement path had to add a fifth:
  the combined typed-core + live-components module. The routing comment is the
  only documentation; the placement path's own refusal text claimed v3 could
  never place, which was stale the moment the pure tier landed.
- `[slow]` **The bridge's "records cross as plain JSON" claim was aspirational
  for Go.** The pure tier emits record structs with *unexported* fields, which
  `json.Marshal` silently turns into `{}`. The go mirror of the rust tier's
  serde derives is exported json-tagged fields — but only in placement mode,
  because the pure tier's emitted Go is a frozen, byte-identical golden.
  Keeping the two record spellings straight (same struct name, different field
  names) is a subtle invariant; see §3.
- `[nit]` The runner pads the subject column to 16 chars, so
  `out.index("swap  | Sched")` prefix-matches `"swap  | SchedUser"` — a test
  assertion bug I hit and fixed by matching `"swap  | Sched "`.
- `[nit]` Pre-commit hook runs the full suite on every commit (see
  findings-drivers.md §2); plan commits to be few.

## 3. What revl gave you

- **The IR was already v3-typed.** `examples/rec.rvl` and `examples/outcome.rvl`
  compiled to records in service returns, ADT constructions in method bodies,
  and `Result[Row, Str]` boundaries with zero frontend work — the entire gap
  was backend-side. The fix needed no language change.
- **The pure v3 tier was the spec for the component world.** `_go_v3_construct`,
  `_go_v3_match` and `_emit_v3_go_types` already rendered records as structs,
  variants as sealed interfaces, and matches as type switches; the component
  renderer mirrored them, so the two dialects stayed shape-consistent (same
  struct/case names, same match skeleton).
- **The frozen goldens protected the pure tier.** `test_v3_checked_in_generated_is_current`
  byte-compares fresh `emit()` output against `backends/go/v3/*`; putting the
  exported-field record spelling in *placement mode only* (`emit_placement`,
  a separate entry) is what let the bridge round-trip records without touching
  the frozen pure tier.
- **The stc-go runtime is dependency-free.** `revl run --backend go` builds the
  whole emitted package plus the placement runner in ~2 s with no network once
  the module cache is warm — the once-mode round-trip is a fast inner loop.
- **The cross-tier bridge contract held on the first real cross.** The python
  bridge client called a go-served `Scheduler` with an ADT argument
  (`{"$kind": "Final", "$value": "done"}`) and a record return came back as
  `{"id": 1, "name": "ada"}` — the canonical wire encoding
  (docs/interop-bridge.md §3) needed no changes.

## 4. Time-to-green

Compile→refuse→fix cycles: 5. (1) the placement refusal itself; (2) the
`Row`/host-runtime collision; (3) the `_a / _b` int64/int constant typing;
(4) the `match` guard being too strict for the no-types v3 path
(java_match.rvl); (5) the test assertion prefix collision. The longest single
debugging stall was mapping the two Opt/Result dialects (§2, ~20 min) — the
actual code changes after that were mechanical. Everything else was under two
minutes per cycle; the go toolchain names file:line precisely.

## 5. Cost ledger

- `docs-gap` — backend-ir-v3.md documents the pure tier's Opt/Result but not
  the stc-go component world's tuple convention; the placement bridge's
  "records cross as plain JSON" comment silently assumed json-marshalable
  records (the pure tier's unexported fields break that). ~25 min total across
  the session. **The single change that would have cut the most cost: a
  paragraph in backend-ir-v3.md stating the two-dialect rule — "pure tier:
  sealed `RevlOpt`/`RevlResult` interfaces; live stc-go components:
  `(T, bool)`/`(T, E, bool)` tuples in call positions, `*T` in value
  positions" — plus a note that placement-mode record structs are exported +
  json-tagged while the pure tier keeps unexported fields.**
- `tooling` — the full-suite pre-commit hook on each commit (~minutes per
  commit; three commits planned).
- `missing-feature` — the go tier had no v3 typed-core placement at all; the
  feature IS this session, so no separate cost line.
- `diagnostic` — the old placement refusal ("needs v1/v2 services") did not say
  *why* v3 could not place (the pure path drops components, so the bridge would
  not link). The new routing keeps an honest refusal only for pure-only
  documents ("nothing to boot").

## Remaining gaps (honest, named)

Landed: v3 typed-core compositions with components place and run on go —
record service returns, ADT-typed service boundaries, ADT `match` in
provide-method bodies, records/ADTs crossing the interop bridge
(record → json-tagged struct, variant → `{"$kind","$value"}`), and `match`
over a Result in a method body (`checked_div_trunc`). `examples/rec.rvl`,
`examples/outcome.rvl` and `examples/java_match.rvl` all round-trip `revl run
--backend go --once`.

Still refused on the go tier, each with a named `EmitError` (never wrong code):

- `arrow` values, `?.` (`optfield`/`optcall`), and functional record update
  `{r | f = e}` in component/method bodies — pre-existing v1/v2 tier limits,
  unchanged; pure `fn` bodies already lower them.
- `match` over an Opt/Result whose scrutinee is not a multi-value call (a bare
  Opt/Result value cannot be a single Go expression in the tuple world).
- `checked_div_floor` / `checked_div_euclid` / `checked_mod` in component
  bodies (the int-arith helpers are pure-tier only); `checked_div_trunc` works.
- Opt/Result construction in non-return value positions (tuple world;
  return-position construction has always worked).
- Pure-only v3 documents still refuse placement ("nothing to boot") — that is
  `revl test` territory, which works.

Carried in placement mode: plain `test` blocks and `lifecycle test` blocks
(the latter drive the live stc-go composition and compile to `go test`
functions in the combined module — verified by running the emitted module
under `go test`).

## What landed

- `backends/go/emit.py` — the v3 typed-core placement path: combined
  typed-core + live-component emit (`_emit_v3_placement`), placement-mode
  record structs (exported, json-tagged), host-runtime collision renames,
  record/ADT construction and `match` in the component renderer, `match` over
  Result tuples (`checked_div_trunc`), and the v3 bridge (per-variant
  `_revlEncode`/`_revlDecode`, recursive List/Opt encode/decode).
- `examples/v3_step_scheduler.rvl` — the agent-loop `Step` ADT composition
  (record return + ADT boundary + ADT match).
- `tests/test_run_go.py` — the v3 typed-core once-mode round-trip.
- `tests/test_placement_go.py` — a python client calling a go-served v3
  service: records and ADTs cross the seam and come back.
- `backends/go/test_emit_go.py` — placement emit-shape tests.

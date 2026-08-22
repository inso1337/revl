# The Go tier and IR v3 — an implementation plan

**Status: landed — kept as the implementation record.** The Go backend
(`backends/go/`) emits `ir_version` **1, 2 and 3**, sits in the conformance
matrix at **0 gaps**, and validates under `go build` (§Validation below). What
follows is the plan that work was built to: how each v3 construct maps to
idiomatic Go, targeting [stc-go](https://github.com/0xdenny218/stc-go).

The authority for *what* v3 is remains [docs/backend-ir-v3.md](backend-ir-v3.md)
(the generic IR contract) and [docs/syntax-2.0.md](syntax-2.0.md) (the source
language). This file is the Go-specific *how*, and it leans deliberately on how
the four other typed tiers already solved each problem — python, TypeScript,
rust and java all reached v3 first, so Go should copy proven patterns rather
than re-derive them.

## What v3 adds over the current Go backend

Per `docs/backend-ir-v3.md`, a v3 document carries, at the top level, `types`
(records, variants/ADTs), `functions` (pure `fn`), `externs` (classified
`pure`/`acquire`/`emission`), and `tests`; and its component/method/function
bodies use the full 2.0 **step** set (`let`/`assign`/`if`/`while`/`for`/
`return`/`fail`/`expr`/`assert`) and **expression** set (`bin`, `un`, `field`,
`index`, `if` ternary, `arrow`, `match`, `interp`, `call`, `adt`, `record`,
`list`, `optfield`, `optcall`, `builtin`). The Go tier already renders a subset
of the steps in provide-method bodies; v3 is mostly the **type layer and the
2.0 expression layer**.

## Construct-by-construct mapping

| v3 construct | Go (stc-go) | precedent to copy |
|---|---|---|
| record `type R = { a: Int }` | `type R struct { A int }` (exported fields) | rust struct / java record |
| pure `fn f(x: Int) -> Int` | `func F(x int64) int64` | any tier |
| `extern pure/acquire/emission` | a Go `func` (host boundary); `class` is advisory to the tier but load-bearing for G4/G8 upstream | keep the extern's own body per `@go` if the block model is adopted |
| in-language `test` | one `func TestX(t *testing.T)` in the emitted package, run under `go test` | this tier already *executes*, so tests are a natural fit — closer than the golden tiers |
| `match` on an ADT | a `switch v := x.(type)` type-switch over the variant interface | java sealed-interface `switch` |
| ternary `cond ? a : b` | Go has no ternary → a helper `func revlTernary[T any](c bool, a, b T) T` or an inlined IIFE; prefer the generic helper | wasm/rust inline |
| template `interp` | `fmt.Sprintf` (the tier already does this for v1 `format`) | existing Go `format` |
| `Int`/`Float`/`Bool`/`Str` | `int64`/`float64`/`bool`/`string` (match the current `_PRIM` map) | existing `_go_type` |

## The design decisions that are genuinely Go's own

These are where Go differs from the tiers that already shipped v3, so decide
them deliberately and record the choice in the emitter and the README.

1. **ADTs / sum types — Go has none.** Encode a variant type as a **sealed
   interface plus one struct per case**, exactly as the java tier does with
   sealed interfaces: `type Outcome interface { isOutcome() }`, with
   `type Found struct { … }` and a private `func (Found) isOutcome() {}` marker
   so only in-package variants satisfy it. `match` is then a type-switch. This
   is the closest idiomatic Go analog and it reuses java's proven shape.
   `Opt[T]` is *not* an ADT here — keep the existing `(T, bool)` convention (see
   below), and `Result[T,E]` is the one open question: a generic
   `type Result[T, E any] struct { … }` (Go 1.18+ generics) or a sealed
   interface; pick one and pin it.

2. **`Opt[T]` in expression position.** The tier already lowers `Opt` to
   `(T, bool)` in *return* position, which is idiomatic Go — but a `(T, bool)`
   tuple is **not a first-class value** and cannot be nested inside another
   expression, passed as one argument, or stored in a field. v3 puts `Opt` in
   all of those positions (`optfield`, `optcall`, an `Opt` field of a record,
   `?? `). Decide between: (a) a generic `type Opt[T any] struct { V T; Ok bool }`
   value type used everywhere `Opt` appears as a value, with the `(T, bool)`
   form kept only at the service-method boundary for idiom; or (b) pointers
   (`*T`) for optional values. (a) is more faithful to the type system and
   composes; (b) is more idiomatic but conflates "absent" with "nil pointer".
   This is the single biggest v3 modelling choice for Go — resolve it first.

3. **Arrows and function types.** An arrow with a known type lowers to a Go
   `func(...) ...` literal / `func` type (`(Int) -> Bool` → `func(int64) bool`).
   An arrow that is **still untyped** — the `let g = v => v + 1` case where the
   IR carries neither `param_types` nor `returns` (docs/backend-ir-v3.md §arrow)
   — has no Go type to write, exactly the wall the java tier hit. Copy java's
   resolution: **beta-reduce at the call site** (inline the body with arguments
   substituted) and **refuse** an untyped arrow in value position with a clear
   `EmitError`, rather than inventing a `func(any) any` that would fail to
   compile on a numeric body. Do not regress a typed arrow into a refusal.

4. **`match` exhaustiveness.** Go's type-switch is not exhaustiveness-checked,
   but the **frontend already proved exhaustiveness** — so emit a `default:`
   arm that `panic`s with a "non-exhaustive match" message purely as a
   compile/soundness backstop, exactly as java and rust do. When the arms
   already cover a sealed interface, the panic is unreachable by construction.

5. **Explicit generics `[T]`.** Go has type parameters (1.18+), so a generic
   `fn id[T](x: T) -> T` *could* lower to `func Id[T any](x T) T`. The frontend
   strips the parameter marker from the IR (docs/generics.md), so this is
   optional: the safe first cut is to monomorphise or treat the parameter as
   `any`, and only reach for Go generics if a scenario needs it. Note it,
   don't block v3 on it.

## The two lessons from the other backends, applied up front

Tonight's cross-tier work closed a whole bug class; Go should start on the
right side of it rather than repeat it.

- **One expression renderer, not two.** ts/rust/java each grew a v1/component
  renderer *and* a separate `_v3_expr`, and that split was "the root cause of
  nearly every divergence" (roadmap item 2) — each had to be re-merged into a
  single shape-dispatching function. python and wasm, which always had one
  renderer, had zero gaps. Go should add v3 kinds to its **existing** single
  expression renderer, never fork a second one.
- **Dispatch the `call` kind on shape, not kind.** `call` exists in *both*
  dialects — component form is `target`/`method`, 2.0 form is `callee`/`args`
  (docs/backend-ir-v3.md §dialect hazard). Keying on kind alone silently reads
  the wrong child; key on the presence of `callee` vs `target`. This is the one
  place the split fails *quietly*, and it bit every tier that got it wrong.

## Validation: the sixth tier, joined

`tools/conformance.py` now walks `TIERS = (python, typescript, rust, java,
wasm, go)`; `--validate` hands each tier's output to its real compiler, via a
`GoValidator` in `tools/validate.py` that runs `go build` over the emitted
package — the honest second question ("does the emitted code compile?"), the
same gate that caught a rust bug that had reported `ok` for months. This plan
predates the landing: Go emits v3, sits in the matrix at **0 gaps**, and
the README's tier count reads six. The Go backend also *executes* under `go
test`, which is stronger than compile-only.

## Not in scope for v3

`spawn` / instance-parametric IR (dynamic realms) stays out, on Go as on every
tier — it is unimplemented language-wide (roadmap item 10). The Go backend's
existing refusal for it is correct and should remain until the frontend lands
instances.

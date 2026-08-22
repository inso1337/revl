# Int32 — a proposal

**Status: proposed, not implemented.** This document specifies what `Int32`
requires so the work has a contract to target. Nothing here changes any
existing type; every section is additive.

## Motivation

`Int` is 64-bit two's complement with trapping overflow on all six tiers
(docs/arithmetic.md). That is the right default — but it is not free, and the
cost is concentrated exactly where a smaller integer pays:

* **Density and lanes.** Measured, not assumed (docs/v2.0-roadmap.md §12):
  i32 and i64 scalar arithmetic are identical here (0.99 vs 0.98 ns/iter),
  but a *vectorisable* loop is ~34% faster in i32 because SIMD gets twice
  the lanes. A static `Int32` serves that case; a dynamically-promoting
  integer never could, since a promotable array cannot be stored flat.
* **Trapping defeats auto-vectorisation.** The overflow check costs ~9% on
  scalar arithmetic and much more on vectorisable loops, where the branch
  breaks the vectorized body. `Int32` is an escape hatch for programs that
  want the width, not a change to the default.
* **Imports already meet it.** OpenAPI `type: integer, format: int32`
  collapses to `Int` today (`src/revl/import_openapi.py`: "every `number`
  becomes `Float`, so `format` distinctions (`int32`) are dropped"). A real
  `Int32` lets the importer preserve what the spec said instead of widening
  it silently.

## Surface syntax

* **Type name `Int32`.** No literal suffix. Integer literals remain `Int`;
  an `Int32` value arises from a checked conversion or inference from an
  explicitly-typed binding. This keeps one literal grammar and puts every
  narrowing at a visible site.
* **Widening `Int32 → Int` is implicit; narrowing `Int → Int32` is explicit**
  (`as Int32`, refusing out-of-range values at runtime like any other bound).
  Rationale: revl already widens `Int → Float` implicitly where lossless, and
  the recent soundness work refused *lossy* implicit coercion (out-of-range
  literals are now diagnosed at compile time). Lossless-widen /
  checked-narrow is the same rule applied to width that the language already
  applies to range.
* `_NUMERIC` in `src/revl/typecheck.py` gains `"Int32"`; `_BUILTIN_TYPE_NAMES`
  likewise. The single-uppercase generic heuristic is untouched.

## Semantics: trapping, with pinned divergences where a tier will not

The recommendation is **trapping overflow**, consistent with `Int`. But the
honest cost table differs per tier, and two tiers cannot trap natively:

| tier | native i32 | trap mechanism | cost |
| --- | --- | --- | --- |
| wasm | yes (`i32`) | existing `$int_add/sub/mul`-style checked helpers, 32-bit | near-zero extra; still faster than i64 on vectorisable loops — the point of the type |
| rust | yes (`i32`) | debug traps; **release wraps** | see divergence below |
| go | yes (`int32`) | wraps (Go spec) | needs explicit bound checks to trap |
| java | yes (`int`) | wraps (JLS §15.17) | needs explicit bound checks (Math.addExact or manual) |
| typescript | no | `number` holds i32 exactly (f64, 2^53 ≫ 2^31) | checks mirror the Int path, cheaper constants |
| python | no | mask/check against ±2^31 in `_revl_i64`-style helper | same shape as the existing i64 bound |

**Divergence decision required (the rust question).** `Int` solved this by
emitting checked helpers everywhere rust would wrap. The same approach works
for `Int32` (emit `checked_add(...).expect("revl: Int32 overflow")`-shaped
code), at rust-release scalar cost comparable to what `Int` already pays.
Recommendation: **trap via emitted checks on every tier**, and pin nothing —
unless measurement shows the vectorisable win disappears under the checks on
wasm, in which case pin `{rust-release: wrap}` in DIVERGENCES +
docs/contract-errata.md exactly as the unary-minus split was pinned. Do not
decide this from the armchair: land the wasm measurement first.

## IR: additive annotations, no version bump

The frozen-reference invariant holds that v1/v2/v3 documents stay
byte-identical regardless of later work — the arithmetic wave proved the
pattern by adding `operands: "Int"` annotations only where needed. `Int32`
follows it:

* operators over `Int32` carry `operands: "Int32"` (same sites that already
  annotate for `/`, `%`, and the bounded binops);
* literals, params, fields and returns carry their declared type as they
  already do (`ty`), so no new node kinds;
* **no `ir_version` bump**: old readers ignore annotations they do not know,
  and a document containing `Int32` simply does not exist before this feature.
  If a reader must distinguish, the presence of `operands: "Int32"` is the
  marker — the same way v2/v3 features announce themselves through what the
  document contains.

## Per-tier emission sketch

| tier | `Int32` renders as | arithmetic | overflow |
| --- | --- | --- | --- |
| python | `int` + `_revl_i32()` bound helper | helper-wrapped | OverflowError('revl: Int32 overflow') |
| typescript | `number` | plain JS ops | explicit min/max checks around add/sub/mul |
| rust | `i32` | `checked_*().expect(...)` (release-safe) | panic 'revl: Int32 overflow' |
| java | `int` | `Math.addExact/subtractExact/multiplyExact` | ArithmeticException('revl: Int32 overflow') |
| go | `int32` | helper funcs with pre-checks | panic('revl: Int32 overflow') |
| wasm | `i32` | `$int32_add/sub/mul` checked helpers | unreachable trap |

## Interactions

* **div_trunc family / `/` / `%`**: extend to `Int32` with the same
  operands-annotation dispatch; zero divisor faults uniformly as today.
* **checked_div_\*** (in flight): signatures gain `Int32` variants only if a
  use appears; do not multiply the surface preemptively.
* **Unary minus**: `-x` on `Int32` routes through the same bound re-imposition
  the Int fix landed (lower.py `un` nodes carry `operands`; the emitters'
  helpers enforce ±2^31).
* **Literal-range checker**: the new i64 literal diagnostic generalizes — an
  `Int32`-typed context refuses literals outside ±2^31 at the same site.
* **DIVERGENCES convention**: any tier that ends up wrapping rather than
  trapping gets a pinned row whose flip fails loudly, per the established
  errata process.

## Open questions

1. Does the wasm vectorisable win survive the trap checks? Measure before
   committing to trap-everywhere vs a pinned wrap divergence.
2. Should `Int32` arrays get flat storage guarantees (the SIMD argument
   strictly requires contiguity)? That is a data-layout promise, larger than
   this proposal.
3. `Integer` (arbitrary precision) interacts with conversion rules — decide
   its relationship to `Int32`/`Int` in one place when it is designed, not now.
4. Does the OpenAPI importer map `format: int32` to `Int32` immediately, or
   behind a flag until the type stabilizes?

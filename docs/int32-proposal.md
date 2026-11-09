# Int32 — an implemented proposal

**Status: implemented on all six tiers.** `Int32` shipped in 954a31eb4 ("Add
Int32: 32-bit sized integer with trapping overflow, all six tiers"), with the
bitwise operators following in the item 366 series (frontend in 2b3c54fd2).
The reference for what shipped is `docs/arithmetic.md`, "Sized integers:
`Int32`"; this document is kept as the original proposal and for its
rationale. Each section marks what shipped as proposed, what shipped
differently, and what did not ship; "What shipped, and what did not" at the end
is the summary.

## Motivation

`Int` is 64-bit two's complement with trapping overflow on all six tiers
(docs/arithmetic.md). That is the right default — but it is not free, and the
cost is concentrated exactly where a smaller integer pays:

* **Density and lanes.** Measured, not assumed
  (docs/roadmap-archive/07-open-in-rough-priority-order.md §12):
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
  (built as `.to_int32()`, refusing out-of-range values at runtime like any
  other bound; there is no `as` cast syntax in revl).
  Rationale: revl already widens `Int → Float` implicitly where lossless, and
  the recent soundness work refused *lossy* implicit coercion (out-of-range
  literals are now diagnosed at compile time). Lossless-widen /
  checked-narrow is the same rule applied to width that the language already
  applies to range.
* `_NUMERIC` in `src/revl/typecheck.py` gains `"Int32"`; `_BUILTIN_TYPE_NAMES`
  likewise. The single-uppercase generic heuristic is untouched.

**Shipped, with one spelling change.** The type name, the one literal grammar
and the `_NUMERIC` / `_BUILTIN_TYPE_NAMES` entries landed as proposed. The
narrowing is `.to_int32()` rather than `as Int32`: the method re-imposes the
32-bit bound and faults `revl: Int32 overflow` out of range, and `.to_int()`
spells the lossless widening. A bare `Int` literal is refused in an `Int32`
position (`let x: Int32 = 3` is a type error), so an `Int32` value is
introduced by the checked conversion or by an `Int32`-typed parameter, and
flows through `Int32` arithmetic.

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

**Shipped: trap everywhere, and nothing pinned.** The implementation traps via
emitted checks on all six tiers, rust release included, so no row was added to
the divergence/errata process. The per-tier mechanisms and their exact fault
text are the table in `docs/arithmetic.md`, "Sized integers: `Int32`". The wasm
measurement the paragraph above asks for was not recorded; see the open
questions.

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

**Shipped, plus one marker the proposal did not anticipate.** `+`, `-`, `*`
and unary `-` over `Int32` carry `"operands": "Int32"`, and the emitted
document stays `ir_version: 3`, as proposed. Each implicit `Int32 -> Int`
value-flow site also carries `"widen": "Int"`, the same additive coercion
marker the earlier `Int -> Float` work introduced, so a backend that keeps the
two widths apart emits the conversion instead of relying on a host rule.
Bitwise nodes carry no `operands` annotation: the operators are `Int32`-only,
so there is nothing to disambiguate.

## Per-tier emission sketch

| tier | `Int32` renders as | arithmetic | overflow |
| --- | --- | --- | --- |
| python | `int` + `_revl_i32()` bound helper | helper-wrapped | OverflowError('revl: Int32 overflow') |
| typescript | `number` | plain JS ops | explicit min/max checks around add/sub/mul |
| rust | `i32` | `checked_*().expect(...)` (release-safe) | panic 'revl: Int32 overflow' |
| java | `int` | `Math.addExact/subtractExact/multiplyExact` | ArithmeticException('revl: Int32 overflow') |
| go | `int32` | helper funcs with pre-checks | panic('revl: Int32 overflow') |
| wasm | `i32` | `$int32_add/sub/mul` checked helpers | unreachable trap |

**Shipped in the same shape, with the final helper names in
`docs/arithmetic.md`.** The tables above are the proposal's estimate, not the
final text: TypeScript re-imposes the bound through `revlI32` rather than by
inline checks, and every tier's shipped mechanism is the table in
`docs/arithmetic.md`. The implementation does not use the native wrap: it traps
via emitted checks on all six tiers, rust release included, and wasm has no
payload on the trap.

## Interactions

* **div_trunc family / `/` / `%`**: shipped only partly. `/` is exempt and
  always yields `Float`, so both sides widen to `Float`. `%` and the
  named `div_*` / `mod` stayed `Int`-only: their zero-divisor fault is not
  uniform once `Int32` is a `number` on TypeScript and an i32 on wasm. Widen
  the operands with `.to_int()` first, as `docs/arithmetic.md` says.
* **checked_div_\*** (in flight): no `Int32` variants were added; the
  proposal made them conditional on a use.
* **Unary minus**: shipped as proposed. `-x` on `Int32` carries
  `operands: "Int32"` on the `un` node and every emitter re-imposes the bound,
  so `-Int32.MIN` faults.
* **Literal-range checker**: not built as specified. There is no `Int32`
  literal-range diagnostic: a bare `Int` literal is refused in an `Int32`
  position as a type mismatch whether or not it would fit, and the bound is
  imposed at runtime by `.to_int32()`.
* **DIVERGENCES convention**: no pin was needed; all six tiers trap.

## Open questions

1. Does the wasm vectorisable win survive the trap checks? **Still open:** the
   type shipped on correctness and execution grounds, and no measurement of
   the win under the checks is recorded.
2. Should `Int32` arrays get flat storage guarantees (the SIMD argument
   strictly requires contiguity)? **Not built:** `docs/arithmetic.md` records
   that no data-layout promise is made.
3. `Integer` (arbitrary precision) interacts with conversion rules — decide
   its relationship to `Int32`/`Int` in one place when it is designed, not now.
   **Still open:** `Integer` is fenced and not built (docs/contract-errata.md,
   docs/integer-proposal.md).
4. Does the OpenAPI importer map `format: int32` to `Int32` immediately, or
   behind a flag until the type stabilizes? **Not done:** the importer still
   maps every `integer` to `Int` and drops `format` distinctions
   (docs/import-openapi.md).

## What shipped, and what did not

**As proposed:** the type name; one literal grammar (`Int` literals only); the
`_NUMERIC` / `_BUILTIN_TYPE_NAMES` entries; implicit lossless `Int32 -> Int`
widening; trapping `+`, `-`, `*` and unary `-` on all six tiers, with no pinned
divergence; additive `operands: "Int32"` and no `ir_version` bump; and the
six-tier renderings above. Cross-tier execution proves the arithmetic and the
faults (`tests/test_cross_tier_execution.py`; rust/java behind
`REVL_CROSS_TIER_SLOW=1`).

**Built differently:** narrowing is `.to_int32()` (checked, faulting out of
range), not `as Int32`; `.to_int()` spells the widening. An `Int32` is not
created from a bare literal, in range or not. The IR also carries a
`"widen": "Int"` marker at each implicit coercion site, which this proposal did
not foresee. The bitwise operators (`& | ^ ~ << >>`) were added after this
proposal and are not specified here: they are `Int32`-only and non-trapping,
with C/TypeScript precedence and a shift count taken mod 32
(`docs/arithmetic.md`, "Bitwise operators are `Int32`").

**Not built:** `%` and the named `div_*` / `mod` on `Int32`; mixed-width
arithmetic (`Int32 + Int` is refused, so the widening happens only at
value-flow positions); `Int32` variants of `checked_div_*`; the `Int32`
literal-range diagnostic; flat-storage guarantees for `Int32` arrays; the
OpenAPI `format: int32` mapping; and the wasm measurement the proposal asks
for. Each remains a task for a later pass, not a prerequisite of the shipped
type.

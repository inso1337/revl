# Integer — a proposal

**Status: proposed, not implemented.** This document specifies what the
arbitrary-precision `Integer` type requires so the work has a contract to
target. Nothing here changes any existing type; every section is additive.
It is the sibling of docs/int32-proposal.md (the small, flat, fast end of
the same roadmap item) and defers to it on shared questions.

## Motivation

`Int` is 64-bit two's complement with trapping overflow on all six tiers
(docs/arithmetic.md). Trapping was the right default: it makes overflow a
loud fault instead of a silent wrong answer. But a trap is still a program
that stops, and some programs do not want a bound at all:

* **Unbounded counters are the honest shape of some domains.** An event
  counter, an accumulating ledger total, a product of many factors — these
  have no natural ceiling. Today each must either prove its bound to the
  checker (the literal-range diagnostic refuses `2^63` outright) or accept
  that a long-enough run faults at runtime. `Integer` is the type whose
  answer to "what if it is bigger?" is "then it is bigger", not "trap".
* **The `Int.MIN` wart disappears one level up.** `-9223372036854775808`
  has no spelling in `Int` because the literal `9223372036854775808` is out
  of range before negation sees it (docs/arithmetic.md). In an
  `Integer`-typed position the magnitude is simply legal; the asymmetry is a
  property of bounded widths, not of integers.
* **Interop already wants it.** Python emits native ints (arbitrary
  precision today), TypeScript emits `bigint` (arbitrary precision today).
  A host round-trip through either tier can already produce a value outside
  ±2^63; the other four tiers cannot represent what those two would pass
  through. A named `Integer` gives cross-tier signatures a spelling for
  what python/TS hosts take for granted.
* **It is the reference semantics every width approximates.** The checker's
  literal rules, `%`'s sign law, and the division identities are all stated
  as facts about unbounded integers then narrowed to 64 bits. Having the
  unbounded type in the language makes the narrowing visible instead of
  implicit.

The cost is real and concentrated in one tier (see Per-tier representation);
that asymmetry, not the design, is why this is a proposal rather than work
in flight.

## Surface syntax

* **Type name `Integer`.** No literal suffix. This follows the `Int32`
  decision: one literal grammar, every conversion at a visible site.
* **Literals need no new grammar — they need a wider context rule.** An
  integer literal is parsed with arbitrary precision today and *rejected*
  past ±2^63 by the literal-range diagnostic (`t20_int_literal_range.rvl`)
  wherever an `Int` is expected. The proposal: in a position typed
  `Integer`, an integer literal carries its full magnitude — `let x:
  Integer = 9223372036854775808` compiles where the same literal against
  `Int` is refused. No suffix, no constructor call, and the existing
  diagnostic becomes context-sensitive instead of universal.
* **Widening is implicit along the lossless chain; narrowing is explicit.**
  The lattice is `Int32 → Int → Integer` (each step lossless, so each
  widening is implicit, exactly as `Int32 → Int` is proposed in
  docs/int32-proposal.md). Narrowing — `Integer → Int`, `Int → Int32` — is
  explicit (`as Int`, refusing out-of-range values at runtime like any
  other bound). `Float` sits outside the chain: `Integer → Float` is
  explicit and lossy above 2^53 (an f64 cannot distinguish), and
  `Float → Integer` is explicit and checked (refusing non-integral values)
  rather than truncating silently.
* `_NUMERIC` and `_BUILTIN_TYPE_NAMES` in `src/revl/typecheck.py` gain
  `"Integer"`. The single-uppercase generic heuristic is untouched.

```revl sketch
let total: Integer = 302231454903657293676544
let back: Int = total as Int          // explicit, traps if out of range
let approx: Float = total as Float    // explicit; exact only below 2^53
```

## Semantics: no overflow, ever — and what that pins

There is no overflow question: addition, subtraction and multiplication are
total on `Integer` on every tier. Division keeps today's contract
(`div_trunc` family, Euclidean `mod`, zero divisor faults uniformly). The
divergence surface therefore moves from arithmetic to *representation
boundaries*:

* **Serialization is not part of the type.** JSON has no bignum; a host
  that serializes an `Integer` may lose precision exactly as python's
  `json` does above 2^53. That is the host boundary (G8 audit surface),
  not a tier divergence — documented, not pinned.
* **`as Float` rounding**: IEEE nearest-even, identical on every tier that
  converts through f64. Asserted by execution like the rest of
  docs/arithmetic.md, not pinned as a divergence, since no tier can differ
  without being wrong about IEEE.

## IR: additive annotations, no version bump

Same pattern as `operands` and the proposed `Int32` annotations:

* literals, params, fields and returns carry their declared type as they
  already do (`ty`) — `"Integer"` is just a new spelling;
* operators over `Integer` need **no** `operands` annotation: there is no
  dispatch ambiguity to resolve, since every backend knows `Integer`
  operands are bignums by type;
* **no `ir_version` bump**: a document containing `Integer` cannot exist
  before this feature, and old readers ignore unknown type names exactly
  as they ignore unknown annotations. If a reader must distinguish, the
  presence of `"ty": "Integer"` is the marker.

## Per-tier representation sketch

| tier | renders as | small-value story | cost |
| --- | --- | --- | --- |
| python | `int` | native arbitrary precision — free | zero |
| typescript | `bigint` | native arbitrary precision — free | zero |
| java | `BigInteger` | heap object always; JDK has no tagged int | allocation per op |
| go | `math/big.Int` | heap object always; no small-value fast path in stdlib | allocation per op |
| rust | `ibig` or `malachite` | inline small-value storage (u64/u128 payload before heap) | near-zero below 2^64 |
| wasm | bignum **inside linear memory**, pointer-tagged | tag bit distinguishes immediate i63 from heap pointer; the bignum lives in the module's own heap | the whole cost of the type |

Notes the table compresses:

* **python and typescript are the proof the semantics are portable** — they
  get `Integer` for free, which means the conformance corpus can be written
  and executed before the expensive tiers exist, the way the wasm test
  runner let wasmtime verify what other tiers only static-check.
* **rust**: prefer `ibig` or `malachite`; both store small values inline,
  so the common case pays no allocation. This mirrors what the Int32
  proposal says about release-mode checked helpers: the default path must
  not be the slow path.
* **go/java**: stdlib-only (`math/big.Int`, `BigInteger`). No inline
  small-value trick exists in either standard library; every operation
  allocates. Acceptable — Go and Java programs reaching for unbounded
  integers are not latency-critical — but recorded honestly here rather
  than discovered later.
* **wasm is where the type lives or dies.** Confinement holds: a bignum
  allocated inside linear memory keeps every revl guarantee (no host
  escape, G6/G8 unchanged), and pointer tagging (odd = immediate i63,
  even = heap pointer, mirroring SMI schemes) keeps scalar-integer paths
  allocation-free. But the bignum itself — add/sub/mul/div/mod over
  sign-magnitude limbs in WAT — is a substantial, self-contained module,
  and it is new code the other five tiers do not need. Estimate honestly
  before starting: this is weeks of careful WAT, not an afternoon.

**Recommended phasing:** land the frontend (type name, context-sensitive
literal rule, conversion lattice) with python + typescript first, gated by
the conformance corpus; add rust via `ibig`; add java/go via their stdlib
bignums; build the wasm limb module last, behind the same
runtime-gated-test machinery wasmtime uses today. Each phase leaves the
suite green and every earlier phase untouched.

## Interactions

* **Literal-range diagnostic** (`t20`): becomes context-sensitive — refused
  past ±2^63 for `Int`, refused never for `Integer`-typed positions. The
  rejection file gains its positive counterpart (an in-`Integer`-range
  giant literal compiling).
* **checked_div_\* / div_trunc family**: extend naturally; `Integer`
  division is total modulo the zero-divisor fault exactly as `Int`'s is.
* **`Int.MIN` asymmetry**: stays for `Int` (it is a width fact); the errata
  entry gains a pointer noting `Integer` has no such hole.
* **Equality across widths**: `Integer(5) == Int(5)` should hold after
  implicit widening — equality compares mathematical values, not
  representations. Pinned by execution when the type lands.
* **DIVERGENCES convention**: expected empty for arithmetic; if any tier
  surprises (e.g. a `math/big` corner), pin it there per the established
  errata process.

## Open questions

1. What is the actual wasm limb-module size and the scalar-op overhead once
   tagging lands? Measure before phasing commits to it — the whole schedule
   hinges on this number.
2. Should `Integer` arrays get flat storage? They cannot (elements are
   variable-width); documents that need flat numeric arrays want `Int32`
   (see its SIMD motivation), and the two proposals should say so in each
   other's terms.
3. Do the importers gain anything immediately? WIT has no bignum and
   OpenAPI has none either — likely no importer change, recorded so nobody
   looks for one.
4. Serialization guidance: does revl emit a warning (not an error) when an
   `Integer` crosses a JSON-typed extern boundary? Decide with the G8 audit
   surface work, not inside this proposal.

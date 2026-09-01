# Arithmetic — what `/`, `%` and the named integer operations mean

revl's arithmetic is **specified**, not inherited. That distinction is the
whole point of this document: every tier used to compute whatever its host
language happened to compute, and because the IR carried no operand type, no
backend was even in a position to do otherwise.

## The rule that decides `/`

§0 governs: syntax revl shares with TypeScript means what TypeScript means,
and where the meaning must differ, the syntax must differ.

In TypeScript, `7 / 2` is `3.5`. So in revl:

```revl fragment
7 / 2        // 3.5 : Float — true division, on Int operands too
```

**`Int / Int` is `Float`.** This is not a concession to two backends; it is
what `/` says. The checker used to type it `Int` while python and TypeScript
produced `3.5`, which made the declared type and the runtime value disagree —
a soundness break, and the reason `Int`-returning division "worked" only on
rust. Rust was the tier out of step with the syntax, not the other two.

Declaring the result `Int` is now a type error, with the diagnostic naming
`Float`.

## Integer division has names

Because `/` is true division, integer division needs its own spelling — and
once you are naming it, there is no excuse for a default. The three
conventions disagree only on negatives, which is exactly where bugs live, so
revl makes you say which one you mean:

| operation | rounds toward | `(-7) op 2` | `(-7) op (-2)` |
|---|---|---|---|
| `a.div_trunc(b)` | zero (C, Java, Rust, JS `Math.trunc`) | `-3` | `3` |
| `a.div_floor(b)` | −∞ (python `//`, Haskell `div`) | `-4` | `3` |
| `a.div_euclid(b)` | whichever makes the remainder ≥ 0 | `-4` | `4` |

## `%` keeps TypeScript's meaning; `mod` is the mathematical one

`%` is the **truncated remainder**, taking the sign of the *dividend*. That is
what `%` means in TypeScript, so §0 requires it — but it is also the choice
that makes the surface coherent, because every remainder pairs with a division:

> **The pairing law.** For any `a` and non-zero `b`:
> `a.div_trunc(b) * b + a % b == a`, and
> `a.div_euclid(b) * b + a.mod(b) == a`.

`div_floor` deliberately has no remainder partner. What people reach for a
floored `%` to get is index safety, and `mod` gives that **strictly better** —
non-negative for *either* sign of the divisor, where floored is only
non-negative when the divisor is positive.

Publishing the law is what makes the convention checkable instead of a matter
of taste. `tests/test_cross_tier_execution.py` asserts both identities over
sixteen sign combinations, by executing them on every tier.

python was the outlier: its `%` floors and takes the sign of the *divisor*, so
it is the one tier that builds the truncated form rather than inheriting it.

`a.mod(b)` is the **Euclidean remainder**: always in `[0, |b|)`, whatever the
signs.

```revl fragment
(0 - 7) % 3           // -1   — sign of the dividend (TypeScript's `%`)
(0 - 7).mod(3)        //  2   — always non-negative
7.mod(0 - 3)          //  1   — the divisor's sign is irrelevant
```

`mod` is the operation almost every use of `%` actually wants. `i % n` is not
a valid index into an `n`-element list when `i` is negative; `i.mod(n)` always
is. This split is Boute's Euclidean definition (TOPLAS 1992), and it is the
one Ada draws as `rem`/`mod`, Haskell as `quot`/`rem` vs `div`/`mod`, and Rust
as `%`/`rem_euclid`.

## One definition, five tiers

Each operation is lowered so that every tier computes the *same* result rather
than inheriting a host rule:

| tier | `div_trunc` | `div_floor` | `div_euclid` | `mod` |
|---|---|---|---|---|
| python | built (`//` floors) | native `//` | built | `a % abs(b)` |

`%` itself is built on python for the same reason — its native `%` floors.
| typescript | native `/` (BigInt truncates) | built | built | built |
| rust | native `/` | built | `div_euclid` | `rem_euclid().abs()` |
| java | native `/` | `Math.floorDiv` | built | `Math.floorMod(a, abs(b))` |
| wasm | native `i64.div_s` | `$int_div_floor` | `$int_div_euclid` | `$int_mod` |
| go | native `/` | `revlDivFloor` | `revlDivEuclid` | `revlMod` |

All eight identities in `tests/test_cross_tier_execution.py`
(`INTEGER_ARITHMETIC`) are asserted by **executing** the emitted code, not by
comparing emitter output — python and TypeScript on every run, rust and java
behind `REVL_CROSS_TIER_SLOW=1`, and the wasm helpers verified on real
`wasmtime`.


### What the Go tier needed

Go arrived after this was specified, which made it a fair test of whether the
spec was written down well enough to port to. Four things it did not get for
free, each the same family as a bug already closed on another tier:

- **`==` is a compile error on slices** ("slice can only be compared to nil"),
  so a record holding a `List` cannot use it at all, and comparable structs
  compare field-wise. Non-scalars route through `revlEq`
  (`reflect.DeepEqual`); scalars keep the native operator, so the common case
  costs nothing. `DeepEqual` compares `float64` with `==`, so NaN stays
  unequal to itself as IEEE requires.
- **`/` on two `int64` is integer division**, and a *constant* `1.0 / 0.0` is
  a **compile error** where IEEE defines `+Inf`. Both are fixed by routing
  through `revlDiv(a, b float64)`: a call is not a constant expression, so it
  is an ordinary runtime float division.
- **Untyped constant arithmetic is arbitrary precision.** `0.1 + 0.2` folds to
  exactly `0.3` at compile time and compares equal to it — which is *not*
  IEEE 754 binary64. Every float literal is emitted as `float64(...)` to force
  ordinary float arithmetic. This one is unique to Go so far, and it is the
  kind of divergence only execution finds.
- The four named integer operations, of which `div_trunc` is native (Go `/`
  truncates) and `%` was already correct.

Integer division by zero panics, which is the uniform fault the other tiers
give.

## What makes this expressible: the IR carries operand types

An IR `bin` node used to carry `op`, `left` and `right` and nothing else, so no
backend could distinguish `Int / Int` from `Float / Float`. `/` and `%` now
carry an `operands` field (`"Int"` or `"Float"`) when the checker can
determine it. Only those two operators carry it — a comparison or a boolean
gains nothing from it, and the IR should not grow a field it does not use.



## `Float` is IEEE 754 binary64

Every host revl targets implements it natively — JS `number`, Rust `f64`,
Java `double`, Python `float` — so the commitment costs nothing to honour and
there is no better-specified alternative. Rationals and decimals are different
*types*, not a better `Float`.

Literals are written as you would expect, and `.` starts a fraction only when
a digit follows, so a method call on an integer is never mistaken for one:

```revl fragment
1.5
0.0
1e10
1.5e-3
7.div_trunc(2)     // Int, then a method call — not the float `7.`
```

### Two consequences that touch other guarantees

- **`==` on Float is IEEE, so it is not reflexive.** `NaN != NaN`. §3.4 says
  revl has "a single equality: structural value equality" — true for records,
  lists and every other value, and Float is the one place where the *IEEE*
  rule governs instead. This is not a divergence between tiers; every tier
  agrees. It is a caveat on the spec's wording.

  It also constrains the test runners: an assertion cannot go through
  vitest's `toStrictEqual`, which uses `Object.is` and calls `NaN` equal to
  itself. Assertions lower through revl's own equality instead.

- **`<` on Float is a partial order.** NaN is unordered against everything,
  including itself, so `a < b`, `a == b` and `a > b` can all be false. Any
  code that assumes a total order — sorting, binary search — is wrong on Float
  unless it excludes NaN first.

#
## `Int` is 64-bit two's complement, and overflow traps

Silent wraparound is the failure mode revl exists to remove, so `Int` has a
stated width and a stated behaviour at the edge of it: **64-bit two's
complement, and arithmetic that leaves the range faults.** It never wraps and
never silently widens.

The width costs nothing on the tiers that can express it — rust already mapped
`Int` to `i64`, java to `long`, go to `int64`. Only the *check* costs
anything, and measured on a dependent scalar chain it is about **9%**
(0.98 → 1.07 ns/iter); on a vectorisable reduction it is much worse, because
the check defeats auto-vectorisation. revl components index, count and size
things — the scalar case — so this is the right default, and a future
`Int32` is the answer for numeric kernels rather than making the default
unsafe.

| tier | how it traps |
|---|---|
| python | `_revl_i64` bound check — python is arbitrary precision, so it *imposes* the bound rather than detecting it |
| typescript | `revlI64` bound check — `Int` is `BigInt`, which is also arbitrary precision, so this tier imposes the bound too |
| rust | `checked_add` / `checked_sub` / `checked_mul` (rust's own default only checks in debug builds) |
| java | `Math.addExact` / `subtractExact` / `multiplyExact` |
| go | `revlAdd` / `revlSub` / `revlMul` — Go has no checked arithmetic in std |
| wasm | `$int_add` / `$int_sub` / `$int_mul` — wasm has no checked arithmetic either, so the sign test is emitted and the fault is `unreachable` |

Every hosted tier raises the same text, `revl: Int overflow`, so one guarantee
does not read as four different bugs. **wasm is the exception, and only on the
message:** a wasm trap carries no payload, so the tier can fault but cannot
say why in revl's words. It is the same limit that already applies to its
division-by-zero fault, and it is a property of the instruction set rather
than of the lowering.

### A literal outside the range never reaches a tier

The bound is a *compile-time* fact before it is a runtime one. An `Int`
literal outside `[-2^63, 2^63-1]` is refused by the checker
(`examples/rejections/t20_int_literal_range.rvl`), because the tiers cannot
agree on what such a text even means: python reads it at arbitrary precision,
wasm reads an i64 bit pattern, and the same source file is two different
programs. It used to be accepted and left to the tiers to disagree about;
now it is one diagnostic instead of a behaviour per tier.

This is also why `Int.MIN` has no spelling in the surface. Unary minus binds
to the *positive* literal, so writing `-9223372036854775808` negates an
out-of-range literal and is refused by the same rule. The negative bound is
reachable by computation from in-range literals — `0 - 9223372036854775807 -
1` — which is an ordinary runtime value on every tier, inside the range and
checked like any other arithmetic.

### Every tier holds it

Two did not, and both were closed by the ports written up below — wasm was
`i32` throughout its emitter, and TypeScript mapped `Int` to an IEEE double
exact only to 2^53. Neither was a platform limit: WebAssembly has native
`i64`, and JavaScript has `BigInt`.

Both are asserted by *execution* in `tests/test_cross_tier_execution.py` —
in-range arithmetic on every bounded tier, and overflow that must fault rather
than return a value — so neither can regress quietly.

Unary minus is arithmetic too: negation is `0 - x`, and `0 - Int.MIN`
overflows. python imposed the bound on `+`, `-` and `*` but let `-x` lower to
the host's unary minus, so `-Int.MIN` came back as 2^63 — out of the very
range python imposes a line earlier, silently. It now goes through `_revl_i64`
like any other subtraction, and the closure is asserted by execution. wasm had
this right from the start: its emitter spells negation as a subtraction from
zero through the checked helper, and rust's own `-` panics at the edge in the
debug builds `cargo test` runs. The three tiers that used to wrap or grow now
follow suit — go emits `revlSub(0, x)`, java `Math.negateExact(x)`, TypeScript
`revlI64(-x)` — so `-Int.MIN` faults on every tier, asserted by execution
across py/ts/go/java (`tests/test_cross_tier_execution.py`). No tier wraps.

### What the wasm port took

wasm was the other entry on that list — `i32` throughout its emitter, narrower
than every other tier and, until recently, undocumented. WebAssembly has
**native `i64`**, so the i32 was expedience rather than a platform constraint,
and it is closed: `Int` is i64 there now, and overflow traps.

The port was not a find-and-replace, because that emitter uses i32 for two
different things and only one of them moved:

- **values** — arithmetic, Int comparisons, Int literals, and every Int-typed
  parameter, local, result and global — are i64;
- **linear-memory addresses** stay i32, because wasm32 addressing is 32-bit.
  So do `Bool`, the string byte-length and list-count prefixes, the variant
  tag, and the loop cursors. `_wasm_ty` in `backends/wasm/emit.py` is the
  single place that decides which of the two a given revl type is.

Memory layout paid the rest of the cost. A record field, a list element and a
variant's payload used to be a 4-byte slot at `4 * index`; each is now an
8-byte slot at `8 * index`, always written and read as a whole i64 — a pointer
or Bool is zero-extended into the slot and wrapped back out. Uniform width is
what lets `$list_push`/`$list_concat`/`$list_slice` move elements without
knowing the element type, and reading a slot whose type the emitter could not
pin down returns the value that was written rather than half of it.

The coeffect and provision ABI moved with it: a service declared over `Int` is
`(param i64) (result i64)`, because truncating at a component boundary would
be precisely the silent narrowing this guarantee exists to remove. The one id
that stays i32 is `Job.run`'s interned job tag, which is a compile-time
discriminant and not an `Int` the program can observe.

The claims are executed, not asserted about text:
`backends/wasm/test_v3_emit.py` runs the full range, the trapping cases, the
four named divisions and the pairing law on real `wasmtime`.

**typescript used to sit here** and no longer does. It mapped `Int` to
`number`, an IEEE double exact only to 2^53, so `9223372036854775807` was not
a value it had and `9007199254740993 - 9007199254740992` was `0`. `Int` is now
`bigint` on that tier. What that touched, and the boundary rules it settled:

- every `Int` literal is a BigInt literal (`123n`); a `Float` literal stays
  `1.5`, and the frontend already lexes the two to distinct python types so
  they never blur;
- BigInt and number **do not mix** in JS (`1n + 1` throws `TypeError`), so
  every operation is rendered consistently typed from the IR's `operands`
  annotation — `Int` arithmetic in bigint, `Float` arithmetic in number;
- `/` yields `Float` even on two `Int`s, so both operands convert
  (`Number(a) / Number(b)`) and it stays IEEE: a zero divisor gives
  ±`Infinity`/`NaN`, never the `RangeError` BigInt `/` would raise. `Number()`
  rounds above 2^53, which is inherent to a binary64 result — the same
  rounding rust's `as f64` does;
- `div_trunc` is native (BigInt `/` truncates toward zero) and `%` is already
  the truncated remainder on bigint; `div_floor`, `div_euclid` and `mod` are
  built, and all four route through a guard so a zero divisor throws;
- the stdlib boundary converts in both directions: `length()`, `indexOf()` and
  `charCodeAt()` are `Int` and the JS APIs answer `number`, so they come back
  through `BigInt(...)`; `xs[i]`, `slice`, `charAt` and `repeat` take an `Int`
  the JS side needs as a `number`, so they go in through `Number(...)`;
- `JSON.stringify` **throws** on a BigInt, so the assert diagnostics and the
  host's resolved-config trace render values themselves — a bigint as its
  digits, with no `n` suffix, which is the text every other tier writes for
  the same number;
- `revlEq` needed nothing: `===` is value equality between bigints and never
  conflates one with a `number`, so an `Int` and a `Float` cannot compare
  equal by accident.

### `Int` widens into `Float`, and the IR now says where

`compatible("Float", "Int")` is true, so `1.5 + 2` and `ident(3)` (for
`fn ident(x: Float) -> Float`) both type-check. The `bin` node carries
`operands`, so the *arithmetic* case was always specifiable and every tier
got it right. The rest — a `call` argument, a `let` value, a `return`
expression — used to reach every backend as a bare `lit`/`var` with no
declared type on the node, the same shape of gap `operands` was created to
close for `/`. The tiers that keep `Int` and `Float` apart therefore split
on `ident(3)`, and not in the same direction:

| tier | before | now |
|---|---|---|
| python | `3` and `3.0` are both numbers — absorbed it | `ident(float(3))` |
| go | `3` is an untyped constant — absorbed it | `ident(float64(3))` |
| java | `long` -> `double` is an implicit widening (JLS 5.1.2) — absorbed it | `ident(((double) (3L)))` |
| rust | `ident(3i64)` against `f64` is **E0308**, a compile error | `ident((3i64 as f64))` |
| typescript | `ident(3n)` against `number`; `3n === 3` is false — a **wrong answer** | `ident(Number(3n))` |
| wasm | refuses `Float` (the tier lowers `Int`/`Bool` only) | refuses, unchanged |

A refusal was survivable and the wrong answer was not, so the gap was pinned
in `tests/test_cross_tier_execution.py` (`DIVERGENCES`) rather than left to
be rediscovered. It is now closed the same way `operands` closed `/`: the
frontend — the single IR producer, and the only stage that knows both types
— marks each coercion site on the node itself (`"widen": "Float"`), and
every backend emits the conversion explicitly instead of letting a host rule
absorb it. The marker is additive, so no `ir_version` changes and the
v1/v2/v3 reference documents stay byte-identical.

The marked sites are exactly the declared-`Float`-position ones: call
arguments (generic signatures are instantiated first, so a `T` bound to
`Float` by the call marks too), `let`/`assign` targets declared `Float`, and
`return` expressions of a `Float`-declared family. A `record` field or a
`list` element has no declared `Float` position to coerce toward and stays as
written. Pinned positively: a test asserts the marker is present in the IR,
and one per tier pins the emitted conversion text.

## Sized integers: `Int32`

`Int` is the right default — it indexes, counts and sizes things, which is the
scalar case, and there the 64-bit check costs about 9%. But a *vectorisable*
loop pays much more, because the overflow branch defeats auto-vectorisation,
and even without the check an i32 kernel is ~34% faster than i64 because SIMD
gets twice the lanes. `Int32` is the escape hatch for numeric kernels that want
the width — not a change to the default, and not a licence to wrap.

**`Int32` is 32-bit two's complement, and overflow traps** — the same
discipline `Int` has at 64 bits, at half the width. `a + b`, `a - b`, `a * b`
and `-a` fault at the i32 edge rather than wrapping; the fault is
`revl: Int32 overflow` on the tiers that can carry a message.

| tier | `Int32` is | how `+ - *` trap |
|---|---|---|
| python | `int` + `_revl_i32` bound | the bound is imposed (arbitrary precision) |
| typescript | `number` (a double holds every i32 exactly) | `revlI32` re-imposes the bound |
| rust | `i32` | `checked_add/sub/mul(..).expect("revl: Int32 overflow")` |
| java | `int` | `Math.addExact/subtractExact/multiplyExact` (the `int` overloads) |
| go | `int32` | `revlAddI32/SubI32/MulI32` (compute wide, re-impose the bound) |
| wasm | `i32` | `$int32_add/sub/mul` (compute in i64, narrow through `$int32_narrow`, `unreachable`) |

wasm is the tier the type is *for* — it has native `i32`, and a slot round-trips
an `Int32` bit-exactly (zero-extended in, wrapped out), so `Int32` lives in
records and lists there as well as in registers. As with `Int`, the wasm trap
carries no payload, so it faults without the message.

### The coercion rule: widen implicitly, narrow explicitly

The lattice is `Int32 → Int`, each step lossless. Widening runs that way and is
**implicit** at every value-flow position — a call argument, a `let`/`return`
target, a field — exactly as `Int → Float` widens. The IR marks each site
(`"widen": "Int"`, beside the existing `"widen": "Float"`) so the tiers that
keep the widths apart emit the conversion (`as i64`, `(long)`, `int64(..)`,
`BigInt(..)`, `i64.extend_i32_s`) instead of letting a host rule absorb it.

Narrowing — `Int → Int32` — can lose bits, so it is **always explicit and
checked**. There is no silent truncation and no `Int32` literal suffix: an
`Int32` value comes from `.to_int32()`, which re-imposes the 32-bit bound at
runtime and faults out of range, or from an `Int32`-typed parameter. `.to_int()`
spells the widening where you want it visible.

```revl fragment
let count: Int32 = total.to_int32()   // checked narrow; faults if total > 2^31-1
let wide: Int = count                 // implicit widen, Int32 -> Int
let back: Int = count.to_int() + 1    // .to_int() makes the widen explicit
```

This mirrors the range rule the language already applies: an out-of-range
literal is a compile error rather than a silent reinterpretation, and a
narrowing that can lose bits is refused rather than performed quietly. Three
worked rejections pin it — implicit narrowing
(`examples/rejections/t21_int32_narrow_implicit.rvl`), width-mixing in
arithmetic (`t22_int32_width_mix.rvl`), and `%` on `Int32` (`t23`).

### Bitwise operators are `Int32`, and they do not trap

A bit-manipulation kernel (SHA-256/HMAC, a bitset, a packing routine) needs `&`,
`|`, `^`, `~`, `<<` and `>>`. `Int32` carries them. `Int` does not, and the
asymmetry is deliberate.

**They are `Int32`-only.** `Int` is 64-bit two's complement in *value*, so a
bitwise meaning is mathematically well-defined at 64 bits. But two of the six
tiers host `Int` at arbitrary precision (python `int`, TypeScript `BigInt`), and
there `x << k` grows without bound. Keeping a uniform 64-bit result would mean
re-imposing a 64-bit mask on every shift on exactly those tiers, which is a
per-tier divergence of the same kind the sized-int design set out to avoid.
`Int32` has a fixed hardware width on every tier, so the lowering is one shape
everywhere. An `Int` operand is refused with a diagnostic that points at
`.to_int32()` (`examples/rejections/t28_bitwise_non_int32.rvl` pins the `Float`
case), and the motivating kernel, a 32-bit SHA-256 word, wanted `Int32`
regardless.

**Precedence follows C and TypeScript.** From loosest to tightest the new
operators sit: `|`, then `^`, then `&` (all below `==`/`!=`), then the shifts
`<< >>` (below the relational operators and above `+ -`); `~` joins the prefix
unaries. So `a & b == c` parses as `a & (b == c)` and `a + b << c` as
`(a + b) << c`, exactly as the shared syntax does in TypeScript (§0). Parenthesise
when the C precedence is not what you meant.

**They do not trap.** `+ - *` fault at the `Int32` edge because they are
*arithmetic*, where a value that leaves the range is a bug. A bitwise operator
produces a *bit pattern*, so `1 << 31` is `Int32.MIN` and `~0` is `-1`: results
that "overflow" the positive range but are exactly the intended bits. No bound
is imposed.

**`>>` is arithmetic.** The right shift sign-extends, so `(-8) >> 1` is `-4` and
`Int32.MIN >> 28` is `-8`. There is no logical-shift spelling in the surface;
mask first if you want zero-fill.

**The shift count is taken mod 32.** A count `k` shifts by `k & 31`, so
`x << 32 == x` and `x << -1 == x << 31`. This is not a third choice bolted on. It
is what wasm's `i32.shl`/`i32.shr_s`, JavaScript's `<<`/`>>` and Java's `int`
shifts all do natively, so four of the six tiers need no count-handling code at
all. The two that do not self-mask have it imposed: rust would *panic* on a
count of 32 or more, and go does a full unbounded shift (`1 << 32` is `0`
there), so both emit `& 31` (go as an unsigned count) to land on the same rule.
Trapping the count instead would have made the common `x >> (32 - n)` rotation,
whose count is 32 when `n == 0`, fault where every wasm/JS author expects a
no-op.

The one-line proof is the rotate every hash function needs, in pure revl:

```revl
fn rotl32(x: Int32, n: Int32) -> Int32 {
  return (x << n) | (x >> (32.to_int32() - n))
}
```

It compiles and runs to the **same** value on all six tiers
(`tests/test_cross_tier_execution.py`, `BITWISE`, and on real wasmtime in
`backends/wasm/test_v3_emit.py`). Per tier the lowering is: native `& | ^` and
`~` everywhere (`~` is `!` on rust, unary `^` on go, `xor -1` on wasm); shifts
native on wasm/java/ts, `& 31`-masked on rust/go, and on python a `<<` re-wraps
into 32-bit two's complement (`_revl_i32_wrap`) while `& | ^ >> ~` stay in range
for in-range operands and need no wrap.

### What is not `Int32` yet

`+ - *`, unary `-`, the comparisons, the bitwise operators (`& | ^ ~ << >>`, the
section above) and the two conversions are the surface. Three things stay
Int-only in this pass, each because extending it would ship a per-tier
divergence rather than close one:

- **`%` and the named `div_*` / `mod`.** The remainder is width-agnostic in
  value, but its zero-divisor *fault* is not uniform once `Int32` is a `number`
  on TypeScript and an i32 on wasm. Widen with `.to_int()` to take a remainder.
- **Mixed-width arithmetic.** `Int32 + Int` is refused: the widening is
  implicit at value positions, not inside a binop, so the conversion stays a
  visible request. (`/` is exempt — it always yields `Float`, so both sides
  widen to `Float` and nothing is lost.)
- **A flat-storage guarantee.** The SIMD argument wants contiguous `Int32`
  arrays; that is a data-layout promise larger than the type, and it is not
  made here.

Every claim above is *executed*, not asserted about text:
`tests/test_cross_tier_execution.py` runs in-range arithmetic, overflow that
must fault, checked narrowing that must fault out of range, `-Int32.MIN`, and
the widen/narrow coercions on py/ts/go/wasm, with rust/java behind
`REVL_CROSS_TIER_SLOW=1`.

## Division by zero

IEEE defines it as a *value*, not a fault: `1.0 / 0.0` is `+infinity`,
`-1.0 / 0.0` is `-infinity`, and `0.0 / 0.0` is `NaN`. Python raises
`ZeroDivisionError` and was the only tier out of step; it now goes through a
helper that returns the IEEE result.

This is why `/` by zero is *not* refused while `div_trunc(0)` and `mod(0)`
are: integer division at zero has no value, and float division does.

### The wasm tier refuses Float

This tier lowers `Int` and `Bool` and nothing else numeric, so `Float` is a
**deliberate limit** with a named refusal (`type 'Float' is not lowerable —
this tier supports Int/Bool`), not a silent approximation. It is a limit of the
emitter, not of WebAssembly, which has `f64`. That means `/` is unavailable
there too, since it yields Float. `div_trunc`, `div_floor`, `div_euclid` and
`mod` all work, at the full 64-bit range.

## Division by zero

Integer division and modulo have no value at zero, and every tier said so
differently: python raises, rust and wasm trap, java throws, and TypeScript
handed back `Infinity`/`NaN` — a *value*, where the checker had declared
`Int`. That is the same class of unsoundness as lowering structural `==` to
JS `===`, and it is closed the same way: TypeScript's integer operations go
through guarded helpers that throw, so all five tiers now fault.

A **literal** zero divisor never reaches any of them — the checker refuses it
(`examples/rejections/arith_zero_divisor.rvl`), because it is not a program
anyone meant to write:

```revl reject
fn bucket(key: Int) -> Int {
  return key.mod(0)
}
```

A *computed* divisor still faults at runtime; guard it if the program should
survive. `tests/test_cross_tier_execution.py` asserts that every tier faults
rather than inventing a value.

`/` is deliberately not refused at zero: it is true division on `Float`, where
IEEE defines ±infinity and `NaN` as *values*. See the Float section above.

## Still open

- **TypeScript is still not 64-bit.** `Int` has a stated width everywhere
  else — python imposes the bound, rust/java/go/wasm are all 64-bit and all
  fault at the edge — but TypeScript maps it to f64, exact only to 2^53, so
  `MAX + 1` silently loses precision there. Closing it means `BigInt`.
- ~~**An `Int` literal outside the range is not diagnosed.**~~ **Closed.** The
  checker now refuses an `Int` literal outside `[-2^63, 2^63-1]` — one
  diagnostic where the tiers used to disagree (python at arbitrary precision,
  wasm reading an i64 bit pattern). See "A literal outside the range never
  reaches a tier" above; it remains why `Int.MIN` has no spelling.

- **`Int` is 64-bit on every tier but wasm**, which is still `i32` (see
  above). python and TypeScript are arbitrary-precision hosts and impose the
  bound; rust, java and go carry it natively. The remaining gap is the wasm
  widening, not the specification.
- ~~**A total, value-returning form.**~~ **Closed.** The `checked_div_*`
  forms below return `Result[Int, Str]`, so a program handles a zero divisor
  without faulting. `fail` could not serve here: it is a component construct
  (A8) and is refused in a pure `fn`, so making division "fail" would have
  meant either extending `fail` into the pure stratum — which costs totality,
  the property `verified` rests on — or a `Result` form. The second is what
  was built.

## The total division forms

Each faulting operation has a value-returning counterpart, named by prefixing
`checked_`:

| operation | returns | rounding |
|---|---|---|
| `a.checked_div_trunc(b)` | `Result[Int, Str]` | toward zero (as `div_trunc`) |
| `a.checked_div_floor(b)` | `Result[Int, Str]` | toward −∞ (as `div_floor`) |
| `a.checked_div_euclid(b)` | `Result[Int, Str]` | Euclidean (as `div_euclid`) |
| `a.checked_mod(b)` | `Result[Int, Str]` | Euclidean remainder (as `mod`) |

`Ok(quotient)` carries exactly the quotient the faulting operation computes —
the rounding convention is not a second choice to make. A zero divisor yields
`Err("revl: division by zero")` instead of the fault:

```revl
fn ratio(a: Int, b: Int) -> Int {
  return match a.checked_div_trunc(b) { Ok(v) => v, Err(e) => 0 }
}
```

Two consequences worth stating:

- **A literal zero divisor is accepted here** and refused for the faulting
  operations (`arith_zero_divisor.rvl`). The refusal exists because `x.mod(0)`
  is never a program anyone meant to write; passing zero to the *checked*
  form is precisely the program it is for.
- **Overflow behaviour is unchanged from the unchecked forms**, per tier:
  python and TypeScript impose the 64-bit bound on the quotient (so
  `Int.MIN.checked_div_trunc(0 - 1)` faults there), rust traps, wasm traps,
  and java/go compute natively like their unchecked `/`. Totalising the zero
  case did not totalise the range.

| tier | Result representation for the Err payload |
|---|---|
| python | tagged `Ok`/`Err` classes (emitted when the IR uses Result) |
| typescript | `{ kind: "Ok" \| "Err", value }` — the built-in Result shape |
| rust | std `Result<i64, String>` (`Ok::<i64, String>(..)` turbofish) |
| java | `RevlResult<Long, String>` sealed interface, static helper methods |
| go | `RevlResult[int64, string]` (`RevlOk`/`RevlErr` structs) |
| wasm | a tagged cell (`[u32 tag][i64 payload]`), Err pooling the reason string |

All six tiers lower every form; there is no tier where `checked_div_*` is a
compile error. Execution is asserted in `tests/test_cross_tier_execution.py`
(`CHECKED_DIVISION`) on python, TypeScript, go and wasm; rust and java behind
`REVL_CROSS_TIER_SLOW=1`.

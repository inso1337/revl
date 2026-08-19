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
| typescript | `Math.trunc` | `Math.floor` | built | built |
| rust | native `/` | built | `div_euclid` | `rem_euclid().abs()` |
| java | native `/` | `Math.floorDiv` | built | `Math.floorMod(a, abs(b))` |
| wasm | native `i32.div_s` | `$int_div_floor` | `$int_div_euclid` | `$int_mod` |
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

### Division by zero

IEEE defines it as a *value*, not a fault: `1.0 / 0.0` is `+infinity`,
`-1.0 / 0.0` is `-infinity`, and `0.0 / 0.0` is `NaN`. Python raises
`ZeroDivisionError` and was the only tier out of step; it now goes through a
helper that returns the IEEE result.

This is why `/` by zero is *not* refused while `div_trunc(0)` and `mod(0)`
are: integer division at zero has no value, and float division does.

### The wasm tier refuses Float

wasm values are i32 on this tier, so `Float` is a **deliberate limit** with a
named refusal (`type 'Float' is not lowerable — this tier supports Int/Bool`),
not a silent approximation. That means `/` is unavailable there too, since it
yields Float. `div_trunc`, `div_floor`, `div_euclid` and `mod` all work.

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

- **`Int` has no stated width, and overflow is unspecified.** The tiers do not
  agree: python is arbitrary precision, rust/java/go are 64-bit, **wasm is
  i32**, and TypeScript is f64 — exact only to 2^53. `MAX + 1` grows on
  python, faults on rust, and silently loses precision on TypeScript. A language offering compile-time guarantees should not leave silent
  wraparound to the host.
- **A total, value-returning form.** `checked_div_*` returning
  `Result[Int, _]` would let a program handle a zero divisor without faulting.
  `fail` cannot serve here: it is a component construct (A8) and is refused in
  a pure `fn`, so making division "fail" would mean either extending `fail`
  into the pure stratum — which costs totality, the property `verified` rests
  on — or a `Result` form. The second is the better shape; neither is built.

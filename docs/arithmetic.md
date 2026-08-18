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

`%` is the **truncated remainder**, taking the sign of the *dividend*, because
that is what `%` means in TypeScript and §0 does not permit the same spelling
to mean something else.

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
| typescript | `Math.trunc` | `Math.floor` | built | built |
| rust | native `/` | built | `div_euclid` | `rem_euclid().abs()` |
| java | native `/` | `Math.floorDiv` | built | `Math.floorMod(a, abs(b))` |
| wasm | native `i32.div_s` | `$int_div_floor` | `$int_div_euclid` | `$int_mod` |

All eight identities in `tests/test_cross_tier_execution.py`
(`INTEGER_ARITHMETIC`) are asserted by **executing** the emitted code, not by
comparing emitter output — python and TypeScript on every run, rust and java
behind `REVL_CROSS_TIER_SLOW=1`, and the wasm helpers verified on real
`wasmtime`.

## What makes this expressible: the IR carries operand types

An IR `bin` node used to carry `op`, `left` and `right` and nothing else, so no
backend could distinguish `Int / Int` from `Float / Float`. `/` and `%` now
carry an `operands` field (`"Int"` or `"Float"`) when the checker can
determine it. Only those two operators carry it — a comparison or a boolean
gains nothing from it, and the IR should not grow a field it does not use.

## Still open

- **`%` on negatives diverges between tiers today.** python floors where
  everyone else truncates. §0 says python is the one to move; that changes the
  meaning of existing programs, so it is pinned in
  `tests/test_cross_tier_execution.py` and recorded in
  docs/contract-errata.md rather than changed silently. `mod` gives anyone a
  defined answer in the meantime.
- **`Int` has no stated width, and overflow is unspecified.** `MAX + 1` grows
  on python, faults on rust, and silently loses precision on TypeScript past
  2^53. A language offering compile-time guarantees should not leave silent
  wraparound to the host.
- **Division by zero is unspecified.** python raises, rust panics, and
  TypeScript yields `Infinity` — a silent non-value that propagates. `fail`
  (L-Raise) is the obvious answer and the language already has it.

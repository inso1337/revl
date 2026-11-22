# Float math and the conversion to Int: a proposal

**Status: proposed, design approved (names, slices and the semantics below).
Nothing in this document is implemented.** It answers
issue #3319. The names and the split into slices are a design decision; this
file exists so they can be settled before any tier changes. When a slice lands,
its semantics move into `docs/arithmetic.md` (next to the integer division
family) and this file records what shipped, as `docs/int32-proposal.md` does.

## The gap

`Float` supports arithmetic, comparison and rendering and nothing else
(docs/arithmetic.md, "`Float` is IEEE 754 binary64"). There is no square root,
no rounding, no trigonometry, and no `Float` to `Int`. `Int` widens to `Float`
implicitly where a `Float` is expected (docs/arithmetic.md, "`Int` widens into
`Float`"), but there is no explicit spelling, so a program writes `n * 1.0`. A program that does geometry or
physics declares each function as a host extern with one body per tier, which
ties the source to the tiers whose bodies were written, rests checkable logic
on unchecked host code, and gives no promise that the tiers agree. A text
compiled under `AdmissionProfile.untrusted_author(())` is refused as soon as it
reaches a host-body extern, so a game that draws anything is refused today.

## Principle: only exactly specified operations become builtins

IEEE 754 fixes the result of `+ - * /`, `sqrt`, `floor`, `ceil`, `trunc`,
`abs`, a rounding mode for conversions, and the sign of zero. Every operation
in slice 1 and 2 below is one of those, or a trivial composition of them, so its
result is **bit-identical on every tier by definition**, not by tolerance. A
NaN is compared as a class: its sign and payload bits are not pinned, as nothing
else in revl pins them. The
transcendental functions are not in that set: JS `Math.sin`, Rust, Java and Go
libm and a wasm host all differ in the last bit, and wasm has no `sin` at all.
Slice 3 therefore does not call a host libm (see there).

Replay of recorded frames needs the same input to give the same `Float` on every
run and every tier. That is the property each slice has to keep.

## Proposed names

Methods on a value, like the integer family (`a.div_floor(b)`), arity-checked
against the builtin method table, no import and no host extern.

### Slice 1: `Float` to `Float`, and `Int` to `Float`

| method | meaning | NaN | infinity | signed zero |
|---|---|---|---|---|
| `x.sqrt()` | correctly rounded square root | NaN | `+inf` gives `+inf`, `-inf` gives NaN | `sqrt(-0.0)` is `-0.0`; any negative gives NaN, never `0.0` |
| `x.abs()` | clears the sign bit | NaN | `inf` | `-0.0` gives `0.0` |
| `x.floor()` | toward negative infinity, stays a `Float` | NaN | unchanged | keeps the sign (`floor(-0.0)` is `-0.0`) |
| `x.ceil()` | toward positive infinity | NaN | unchanged | keeps the sign (`ceil(-0.5)` is `-0.0`) |
| `x.trunc()` | toward zero | NaN | unchanged | keeps the sign |
| `x.round()` | to nearest, ties away from zero (`2.5` gives `3.0`, `-2.5` gives `-3.0`) | NaN | unchanged | keeps the sign (`round(-0.4)` is `-0.0`) |
| `x.min(y)` | the smaller | NaN if either is NaN | ordinary | `-0.0` is smaller than `0.0` |
| `x.max(y)` | the larger | NaN if either is NaN | ordinary | `0.0` is larger than `-0.0` |
| `n.to_float()` on `Int` and `Int32` | the explicit spelling of the existing implicit widening, so `n * 1.0` is no longer needed | | | `Int32` is always exact; `Int` is exact up to 2^53, then rounds to nearest, ties to even |

Choices worth reviewing:

- **`round` is ties away from zero** (C `round`, Rust `round`, Go `Round`). It is
  not JS `Math.round` (ties toward `+inf`), not Python `round` (ties to even),
  not Java `Math.round` (ties toward `+inf`, and it returns a `long`) and not
  wasm `f64.nearest` (ties to even). **The python, typescript, java and wasm
  tiers therefore need a wrapper; rust and go use their native `round`.** The
  same wrapper serves `to_int_round` in slice 2. The wasm form is `t = trunc(x)`, then `t + copysign(1, x)`
  when `abs(x - t) >= 0.5`; `x - t` is exact, so this is also right at
  `0.49999999999999994`, where `floor(abs(x) + 0.5)` is wrong.
- **`sqrt` of a negative is NaN**, the IEEE answer. The extern quoted in the
  issue returns `0.0` there; a caller that wants that writes the guard.
- **`min` and `max` propagate NaN**, as wasm `f64.min`, JS `Math.min`, Java
  `Math.min` and Go `math.Min` do. Rust `f64::min` ignores a NaN operand, and
  Python `min` depends on argument order (`min(nan, 1.0)` is `nan`,
  `min(1.0, nan)` is `1.0`) and returns `0.0` for `min(0.0, -0.0)`, so both
  tiers need a small wrapper, for the NaN rule and the signed-zero rule. The alternative (ignore a NaN operand) would
  be a different pick, and is the one place a reviewer may prefer the other way.
- **A zero keeps its IEEE sign**, consistent with the Float `%` section: it is
  invisible in `to_str` and visible through a division. Python's `math.ceil`,
  `math.floor` and `math.trunc` return an `int` (losing `-0.0`), raise
  `OverflowError` on an infinity and `ValueError` on NaN, and `math.sqrt`
  raises on any negative including `-inf`. That tier needs wrappers that return
  a float, keep `-0.0` and pass NaN and infinities through.
- `abs`, `min` and `max` on `Int` are not asked for and are not proposed here.

### Slice 2: `Float` to `Int`

Named for what each does, in the style of `div_floor` and `div_trunc`, and
grouped with the other conversions (`to_int`, `to_int32`, `to_float`, `to_str`):

| method | meaning |
|---|---|
| `x.to_int_floor()` | `floor`, then convert |
| `x.to_int_trunc()` | `trunc`, then convert |
| `x.to_int_round()` | `round` (ties away from zero), then convert |
| `x.checked_to_int_floor()` and the same for `trunc` and `round` | `Result[Int, Str]` instead of a fault, as in the `checked_*` family |

NaN, an infinity, and a result outside `[-2^63, 2^63-1]` have no `Int` value.
They fault on every tier with one message, `revl: Float out of Int range`, the
way `Int` overflow faults with `revl: Int overflow`. The checked forms answer
`Err("revl: Float out of Int range")`, so a caller can handle it as data. The
bound is exact: `2^63` is the smallest `Float` that faults, and `-2^63` is in
range. There is deliberately no unnamed `to_int()` on a `Float`: `to_int` stays
`Int32` widening and `Str` parsing, and a bare rounding choice is the mistake
this family exists to prevent.

The faulting forms keep the `Int` rule that an edge is a fault and not a
wrapped value; the checked forms are the total counterpart. Whether slice 2
ships the checked forms with the faulting ones or as a follow-up is a size
question, not a semantic one.

### Slice 3: `sin`, `cos`, `atan2`, `pow`, `exp`, `log`

Free functions in a new `stdlib/math.rvl`, written in plain revl over the
slice 1 operations, so they are **bit-identical on every tier by construction**:
every primitive they use is exactly specified, and no host libm is involved.
This answers the question the issue leaves open (bit-identical or within a
tolerance) with bit-identical, and it is the only answer under which a recorded
frame replays the same on every tier. It also means the module declares no host
extern, so a program using it passes `AdmissionProfile.untrusted_author(())`
with nothing in `report["host_extern_sources"]`.

Costs and open points:

- Speed: a revl `sin` is slower than a native one. It should be measured on the
  playground's hot loop before anyone adds a native fast path, and a native
  fast path is acceptable only if it is proven bit-identical.
- Go may fuse `x*y+z` into an FMA on some architectures unless each product is
  converted explicitly (the Go spec says an explicit conversion stops it).
  Slice 3 has to emit that conversion and prove it with a test that runs on
  arm64.
- Whether `use "stdlib/math.rvl"` is admitted under `untrusted_author(())`
  needs to be confirmed in slice 3; if it is not, that is a gap in the profile,
  not a reason to make the functions builtins.
- The playground calls `sin(x)`, `sqrt(x)` and so on as free functions. The
  methods above would change those call sites. `stdlib/math.rvl` can therefore
  also export the slice 1 operations as free functions
  (`fn sqrt(x: Float) -> Float { return x.sqrt() }`), so the playground deletes
  its extern lines and adds one `use`, with no other change. This is the second
  naming decision to confirm.

## Why three slices

A builtin is not one change. The last single-method builtin (`Int.to_char`,
#2285) touched the checker table, the lowering table, all six emitters
(python, TypeScript, wasm, go, java, rust), their self-hosted counterparts under
`selfhost/`, the gate census, the per-tier corpora and the docs. Slice 1 is that
size once, with nine methods in two checker row shapes (one operand or two). Slice 2 adds a
new fault and the checked forms on top. Slice 3 adds no compiler primitive. Each
slice is reviewable alone and each leaves the language consistent.

## Acceptance, per slice

- Every operation compiles and runs on python, TypeScript, wasm, go, java and
  rust, with a test per tier against pinned values: `sqrt(2.0)`, `floor(-0.5)`
  (`-1.0`), `ceil(-0.5)` (`-0.0`), `round(2.5)`, `round(-2.5)`,
  `round(0.49999999999999994)` (`0.0`), `min(0.0, -0.0)`, `n.to_float()` at
  2^53 + 1.
- `Float` to `Int` of NaN, both infinities, `2^63` and a value below `-2^63`
  gives the same fault and message on every tier, with a test; `-2^63` converts.
- `docs/arithmetic.md` states the NaN and infinity behaviour of each operation.
- A game using `sin`, `sqrt` and a `Float` to `Int` conversion compiles under
  `untrusted_author(())` with no host extern in `report["host_extern_sources"]`
  (slice 3 for `sin`; slices 1 and 2 for the rest).

## Not in this proposal

`hsl` needs an `Int` written as two hex digits, and `stars` is a host function
for its cost (a whole backdrop in one call), which would have to be measured
again in plain revl. Neither is settled by Float math; both are separate issues
if they are wanted. `tan`, `asin`, `acos`, `atan`, `sinh` and friends are
mechanical once slice 3 exists and are left out until something asks for them.

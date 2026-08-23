# `prop test` — property testing with type-derived generators

*Upgrading "the author picked a few inputs" to "this property held for every
input the type can reach."*

Implementation: `src/revl/parser.py` (the additive `prop test` form),
`src/revl/typecheck.py`/`src/revl/lower.py` (parameter-type validation and the
`prop_tests` IR section), `src/revl/fault.py` (the type-directed generators,
the coverage fold, and the shrinker), `src/revl/test.py` (surfaced through
`revl test`), `tests/test_prop_test.py`, `examples/prop_test.rvl`.
Roadmap item 37.

---

## 1. The gap this closes

A `test` block pins a property at inputs the author happened to think of:

```revl fragment
test "add commutes" {
  assert add(2, 3) == add(3, 2)
}
```

That proves the law at `(2, 3)`. It says nothing about `0`, `-1`, the i64
boundary, or the thousand other inputs where an off-by-one or an overflow
actually lives. A `prop test` states the *general* claim and lets the checker
generate the inputs:

```revl fragment
prop test "add commutes" (a: Int, b: Int) {
  assert add(a, b) == add(b, a)
}
```

The parameters are **generated inputs**. The generators are **derived from the
parameter types the checker already fully knows** — nothing is registered or
hand-written. On failure the runner **shrinks** the offending input to a
minimal counterexample and reports *that*, not the first messy value it hit.

`prop` is a contextual keyword (like `fault` and `lifecycle`): it heads a
declaration only when followed by `test`, so it stays usable as an ordinary
identifier and the self-hosted lexer needs no new reserved word. The parameter
list is exactly a `fn` parameter list, and the body is the same pure statement
grammar a plain `test` body is — `assert` reads identically to everywhere else.

## 2. Writing one

```revl sketch
prop test "<name>" (p1: T1, p2: T2, …) {
  assert <bool expr over the parameters>
  …                      // at least one `assert`; more is fine
}
```

The body is pure code: `let`, `if`, calls to top-level `fn`s, `match`, and one
or more `assert`s. It never activates a component — a `prop test` is a property
of *values*, not of a live composition — so, unlike a `lifecycle` or `fault`
test, it does not need a running runtime.

A property that mentions no parameter, or asserts nothing, is a compile error:
the point of the form is a claim quantified over generated inputs.

## 3. Type-derived generation

Every parameter type is one the checker can **derive** a value from; a type it
cannot (a bare `Map`, or a record that transitively contains itself) is a
compile error, not a runtime surprise. The derivation, per type:

| type | what the generator visits |
| --- | --- |
| `Int` / `Int32` | the **i64/i32 edge values** — `0`, `±1`, `±2`, the min/max, and their neighbours — plus random values |
| `Bool` | both `true` and `false` |
| `Str` | the empty string, single characters, and random strings |
| `Float` | `0.0`, `±1.0`, and random magnitudes |
| `Opt[T]` | **both arms** — `None` and `Some(v)` — with `v` generated from `T` |
| `List[T]` | **empty and non-empty** lists, elements generated from `T` |
| record | each field generated from its type, with every field varied through its own edges |
| ADT (`A(..) \| B \| …`) | **every constructor**, payloads generated from their types; recursive ADTs are generated to a bounded depth |

Generation runs in two phases. A set of **coverage rounds** deterministically
walks the type's boundaries — every i64 edge, both `Opt` arms, empty/one/two
`List`s, every ADT constructor, each record field — so coverage is *guaranteed*,
not left to chance. Then **random rounds** search more widely for a
counterexample. `revl test` prints the coverage it achieved per parameter:

```
PASS structural reflexivity: property held over 84 generated input(s)
    coverage tags: List[Str] — lengths: empty, nonempty
    coverage hint: Opt[Int] — arms: None, Some
    coverage Shape: 3/3 constructor(s) — all constructors visited
```

## 4. Shrinking

When a round fails, the runner minimises the input: it repeatedly replaces one
component with a smaller value that *still fails*, until no single reduction
fails. "Smaller" is the reduction a human would try —

* an integer pulled toward zero,
* a shorter or empty string / list,
* a `Some(x)` collapsed to `None`,
* a payload ADT case collapsed to a base case,
* a record with one field shrunk.

So a property that is false for every negative integer reports the essence, not
the noise:

```
FAIL non-negative: property is false
    counterexample (shrunk): a=-1
    because: a >= 0
    (first failing input was: a=-9223372036854775807)
```

An input that makes the property *raise* — an overflow trap at an i64 edge, a
host precondition — is equally a counterexample (the property did not hold) and
is reported with the exception, never swallowed.

## 5. Running it

`revl test` runs the properties on the **py reference tier**. Because a prop
body is a pure function, the runner lowers it to an ordinary emitted function
(the same injection trick the fault runner uses for its `fail` step — no
backend emitter is touched), execs it, and calls it with generated arguments
in-process. It needs only the backend *emitter*, not the cordis runtime, so it
runs even where a `lifecycle` or `fault` test would be skipped.

```
$ revl test examples/prop_test.rvl
property tests — py reference tier (roadmap item 37)
  …
PASS structural reflexivity: property held over 84 generated input(s)
PASS less-than is antisymmetric: property held over 83 generated input(s)
PASS field round-trips: property held over 72 generated input(s)
checked 3 property/properties: 3 held, 0 broke
```

The other five tiers print a note that the properties did not run there, the
same way `fault test` and `verified effect` do.

## 6. Scope, and the cross-tier-fuzzer horizon

Scope is deliberately the py reference tier. The roadmap's **noted horizon** is
to compile a `prop test` to all six tiers as a *cross-tier differential
fuzzer*: run the same generated inputs on every backend and treat a divergence
as the finding. That is an emit-side feature — it touches every backend's
`emit.py` and the backend golden suite — and is **not built here**; this pass
keeps `prop test` a py-runtime feature like `fault test` and `verified effect`.

## 7. Relationship to `verified effect` (item 26)

Item 37 was meant to be "designed first," with item 26 (`verified effect`,
docs/verified-effect.md) as its first instance. Item 26 landed first, as a
bespoke runner (`src/revl/fault.py`): it is the **inverse-round-trip property**
— "for a generated activation, `undo ∘ do` leaves no observable residue" — and
its snapshot / randomize / compare loop is exactly what this general runner
does, specialized to one property with one kind of generated input (a component
activation) and one kind of assertion (the state fingerprint closed).

It is left in place and unchanged. Re-expressing it as a derived `prop test`
would need generators that produce *activations* rather than plain values (the
generated input surface is a component's `config`, and the property is checked
by activating and tearing down, not by calling a pure function). That is a
clean future unification once activation-valued generators exist; until then,
`verified effect` is item 37's hand-written first instance, and this document is
the general form the roadmap wanted designed around it.

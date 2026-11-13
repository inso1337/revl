# Function specs: the `ensures` clause

**Status:** implemented (2026-11-07, issue #2705, first slice). The clause is
sugar over [`prop test`](prop-test.md); the admission half of the issue is a
separate decision and is not built (see "What is not built").

A function can say what it is supposed to do, next to the function, and have
that claim checked over generated inputs. Before this, the checker could prove a
function well-typed, total and pure, and a `prop test` could state a property,
but the two lived apart, so nothing tied the claim to the function it describes.

---

## 1. Writing one

```revl
fn min0(x: Int) -> Int ensures result <= 0 {
  if (x < 0) {
    return x
  }
  return 0
}
```

`ensures` sits between the return type and the body. The expression is a pure
`Bool` claim over the function's parameters, plus the name `result` for the
value the body returns. `result` is the only name the clause adds; a function
with a spec cannot declare a parameter called `result`, because the clause would
have no way to name the return value.

The clause can also call the function by name when that reads better:

```revl
fn inc(x: Int) -> Int ensures inc(x) == x + 1 {
  return x + 1
}
```

`ensures` is a contextual spelling, the same discipline `cache`, `prop` and
`fault` use: it heads a clause only in this position, so a program that already
uses `ensures` as an ordinary identifier keeps parsing and the self-hosted lexer
needs no new keyword.

A spec that the expansion cannot express is refused with a diagnostic rather
than silently skipped:

- at least one parameter is required (the generated inputs come from the
  parameter types);
- the parameter types must be generatable, exactly as a `prop test`'s are;
- a generic function is refused, because a spec needs the concrete parameter
  types the generator derives values from;
- a function named `result` is refused, for the same reason a parameter of
  that name is.

A spec is only supported on a **top-level `fn`**. `ensures` on a service
operation, a provided method or an extern is refused by name: the check is a
`prop test` that calls the function with generated inputs, which only a
top-level `fn` can be. To state a claim about a method, write it on a top-level
`fn` and call that from the method.

## 2. What it checks

The clause is **sugar over `prop test`**, not a new checker. Lowering expands it
to a property test over the function's parameter types whose body is:

```revl fragment
let result = min0(x)
assert result <= 0
```

That generated unit is an ordinary `prop_tests` IR entry, so it runs through the
same type-directed generator, the same coverage fold, the same shrinker and the
same runner as a hand-written `prop test`. Two things follow directly:

- the coverage guarantees hold: the i64 edge values, both arms of every `Opt`,
  empty and non-empty `List`s, every constructor of an ADT, and every record
  field are visited;
- a failure is reported as the **minimal** counterexample, not the first messy
  input the search happened to hit.

```
$ revl test spec.rvl
FAIL min1 ensures: property is false
    counterexample (shrunk): x=0
    because: result <= 0
  left  = 1
  right = 0
```

The generated unit carries `spec_of` (the function) and `spec` (the clause's
spelling), so a runner result is attributable to the function rather than to an
anonymous property.

## 3. The audit records the spec and its last check

`revl audit` is a **static** surface: it compiles the program and renders what
it knows without running user code. So the result of checking a spec is a
_recorded_ fact, not something the audit can manufacture. `revl test --report
json` now carries a `function_specs` section, and `revl audit --spec-results`
reads it:

```
$ revl test --report json spec.rvl > report.json
$ revl audit --spec-results report.json spec.rvl
function specs (last recorded result):
  fn min0 ensures result <= 0  (last result: pass)
  fn min1 ensures result <= 0  (last result: fail)
```

Each recorded row carries a `digest`: a sha256 over the revl version (input
generation and shrinking are revl's), the clause, the function's
signature and body, every function, extern and type declaration it reaches
(its transitive callees and the types its parameters and callees use), and the
path of its file relative to the project root (the working directory). The
audit
recomputes it from the program it is auditing and shows a recorded result only
when the two agree. A record taken under another revl version, or before the
body, the clause, a callee or a reached type changed, a record for a function of the same name in another
file, and a record with no digest all read `unrun`, never the old result. A
report recorded in another checkout of the same tree, on CI say, still matches
when both runs start from the project root.

Without `--spec-results`, every spec is listed with `last result: unrun`.
`unrun` is not a failure and not a pass; it says no recorded run has checked the
claim yet. The same rows are in `revl audit --json` under `function_specs`, and
a program with no spec keeps its audit body byte-for-byte unchanged.

## 4. What is not built

- **No preconditions.** A spec is total over the type's generated values. A
  claim that assumes no overflow is therefore false at the i64 edges:
  `fn inc(x: Int) -> Int ensures result == x + 1` fails at `x = 9223372036854775807`,
  because `x + 1` overflows there. That is the honest result, and the shrinker
  reports it as the minimal failing input. A `requires`-style precondition is a
  later design question, deliberately not invented here.
- **Admission does not read the spec yet.** The issue's second slice, making a
  passing spec an admission input for untrusted authors at a fixed seed and
  budget, changes admission policy and needs its own decision on the budget and
  on how a wrong spec is reviewed. This slice adds the form, the check and the
  audit record; it does not change what is admitted.
- **Cross-tier fuzzing.** Like `prop test`, the check runs on the py reference
  tier. The cross-tier differential-fuzzer horizon noted in
  [prop-test.md](prop-test.md) applies here unchanged.

A spec is **evidence, never a substitute for the effect checks**. A function
whose spec passes but whose component reaches an undeclared capability is
refused by the existing G4/G8 checks exactly as before.

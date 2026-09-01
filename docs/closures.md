# Closures over mutable state

**Status:** decided and implemented (roadmap item 129). This document is the
decision record for how revl closures interact with function-local mutation
(`var`, syntax-2.0 §3.5) and why the derived-teardown guarantees (G7, A8) force
that decision.

## The question

revl 2.0 admits function-local mutation: `var`, `while`, `for` (syntax-2.0
§3.5). It also admits arrow functions (`x => e`, `(a, b) => e`). The two meet
when an arrow reads a `var` bound in an enclosing scope:

```revl
fn snapshot_capture() -> Int {
  var n = 1
  let f = (x: Int) => x + n   // f reads n
  n = 100                     // n is reassigned after f is built
  return f(5)                 // 6, not 105
}
```

Two capture disciplines are possible, and they are not interchangeable:

* **By value (snapshot).** `f` closes over the *value* of `n` at the moment the
  arrow is built. Later assignments to `n` do not reach `f`. `f(5)` is `6`.
* **By reference (shared cell).** `f` closes over the *cell* `n`. Reading `n`
  inside `f` reads whatever the cell holds when `f` runs, and (in a language
  that allowed it) writing `n` inside `f` would mutate the enclosing cell.
  `f(5)` would be `105`, and a closure could be a mutable-counter object.

TypeScript, the corpus revl's models write in, captures by reference. So the
choice is load-bearing: the same source has two different meanings, and one of
them is the one models expect out of habit.

## The decision

**revl closures capture strictly BY VALUE. Reference capture is refused.**

Concretely:

1. An arrow that *reads* an enclosing `var` snapshots its value at
   arrow-creation time. This is supported, and it is the only capture form the
   language has. The example above compiles and returns `6`.
2. An arrow that *writes* an enclosing binding (reference capture, the shared
   mutable cell) is refused at parse time with an explicit diagnostic. There is
   no statement-block arrow in revl: a closure body is a single pure
   expression, so the write form has no grammar. The one shape that spells the
   attempt, `(…) => { name = … }`, is caught and named rather than left to
   surface as an incidental record-literal parse error.

```revl reject
fn counter(step: Int) -> Int {
  var n = 0
  let bump = (by: Int) => { n = n + by  n }   // reference capture: refused
  return bump(step)
}
```

The diagnostic:

```
a closure cannot assign to `n`: captures are by value, not by reference (G6)
  a revl closure snapshots the values it reads (syntax-2.0 §3.5,
  docs/closures.md) — there is no shared mutable cell to write through.
  Return the computed value, or mutate the `var` `n` in the enclosing `fn`
  body, not inside the closure.
```

The executable form of this rejection lives at
`examples/rejections/g6_closure_mutates_capture.rvl`; the by-value acceptance
is pinned by `test_arrow_captures_var_by_value_at_creation_time` in
`tests/test_v2_emit.py`.

This is decision (A) of roadmap item 129 (refuse mutable capture), scoped
precisely: what is refused is the *shared cell* (reference capture), not the
reading of a mutable value. The rest of this document is why the refusal is not
a taste call but a requirement of G7 and A8.

## Why reference capture breaks G7 and A8

revl's whole claim is that the derived teardown is correct by construction. Two
guarantees state it (DESIGN.md §4):

* **G7** — derived teardown is LIFO-complete over the accumulated effects
  (paper Thm. 16). When a component deactivates, the runtime replays the
  inverses it accumulated, in reverse order, and every installed effect is
  undone.
* **A8** — a mid-body failure reverts and contains (L-Raise): the effects
  installed before the failure are rolled back, leaving no residue.

Both rest on a property that is easy to state and easy to lose: **an inverse is
a function of the values the forward effect used.** When `effect E undo U` runs,
`U` is the way to revert *this* `E`. The teardown accumulator holds `U` as an
entry, and G7/A8 replay it later, possibly much later, against state the
component has kept mutating in between. For the replay to revert exactly what
was installed, `U` must denote the same reversion at teardown time that it
denoted at install time. That is **value-semantic equality**: the accumulator
entry is a value, stable across the interval between install and teardown, not a
window onto live state.

By-value capture preserves this. An inverse (or any closure reachable from one)
closes over the *values* it read when it was built. Whatever the enclosing
`fn` does to its `var`s afterward, the accumulator entry is unchanged, because
it never held the cell in the first place. The snapshot at line 3 of
`snapshot_capture` is the whole mechanism in miniature: reassigning `n` cannot
reach `f`, so `f` means one fixed thing for its entire lifetime.

Reference capture destroys it. Suppose a closure could close over the *cell*
`n`, and suppose that closure sat inside an `undo`. Between install and
teardown the enclosing frame reassigns `n`. Now:

* The forward effect was computed against `n = n_install`.
* The inverse, replayed at teardown, reads `n = n_teardown ≠ n_install`.

The inverse no longer reverts the effect that was installed. It reverts some
other effect, the one described by the current cell, which was never applied.
G7's "LIFO-complete over the accumulated effects" still fires the entries in
order, but the entries are no longer the inverses of the forward effects, so
completeness is over the wrong set. A8's "reverts and contains" replays against
values the forward pass never used, so the no-residue proof loses its subject:
there is nothing it can point to as "the state before this effect," because the
closure that was supposed to name it now names a moving target.

The paper's recovery-exactness metatheorem (Def. 8, witnessed inverses) has as
a hypothesis that the inverse actually reverts the effect. revl's job (DESIGN.md
§1) is to discharge that hypothesis by construction rather than trust it as a
callback. A closure that reads a live mutable cell reintroduces exactly the gap
the language exists to close: the inverse becomes an unchecked function of
mutable state, and no amount of LIFO ordering rescues it. So reference capture
is not a feature revl is missing; it is a feature revl must not have.

## Why not just keep the silent snapshot

Before item 129 the read-capture case was accepted and snapshotted silently,
and the write-capture case failed with a record-literal parse error
(`expected :, found '='`) that named neither the binding nor the rule. The
snapshot is kept (it is the sound semantics), but the write case is now an
explicit refusal for the same reason every revl rejection is a deliverable
(DESIGN.md §9): a model that writes a TypeScript-style counter closure and gets
back a message naming `n` and pointing at the by-value rule learns the language;
a model that gets `expected :, found '='` learns nothing and guesses. The
diagnostic converts a silent semantic divergence from TypeScript into a refusal
that teaches the divergence.

## Scope and interaction notes

* **`var` still never escapes** (syntax-2.0 §3.5). By-value capture is one
  instance of the general rule that a `var` cannot outlive its scope, be stored
  in a record by reference, or be returned as a cell. Reading a `var`'s value
  into a record literal (`{ value: n }`, roadmap item 154) is the same
  value-copy the closure does, and is likewise fine.
* **Effect and provide bodies.** `var`/`while`/`for` are function-local
  (they parse only inside a `fn`/provide-method body). The teardown accumulator
  therefore never holds a closure over a mutable cell, because no such closure
  can be built. G7/A8 hold by construction, not by runtime discipline.
* **Backends.** The by-value snapshot is realized identically across the
  tiers: the IR arrow node carries a `captures` list, and each emitter binds
  those names to their current values at arrow-creation time (see the
  `captures` handling in `backends/*/emit.py`). No backend has a shared-cell
  path to diverge on, because the front end never emits one.

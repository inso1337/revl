# 3341. Sharing a sequence between two methods of a component

Issue #3341. **Status: proposed, not implemented.** This document picks one of
the two shapes the issue names, states the rules it would carry and lists what
is still open. No compiler change comes with it.

## The gap

Two provide methods of one component cannot share a sequence of steps that
calls a service the component requires. A method cannot call its sibling, a
`fn` is not a statement of a component body, and a module `fn` cannot name a
required service (the component's `requires` names are not in its scope, and
a service value is not a receiver there). What works today is a module `fn`
that is pure, reaches a host emission extern, or reads a resource handed in as
a value.

## The two shapes

### A. A private `fn` in the component body

```text
component Studio requires gate: Gate, publish: Out provides s: S {
  fn report(f: Verdict) -> Bool {
    var stop = false
    if (f.refused) { emit publish.say(...)
      stop = true }
    ...
    return stop
  }
  provide s {
    fn revert(...) {
      let f = emit gate.admit(...)
      let stop = emit report(f)
      if (stop) { return }
    }
  }
}
```

The body is lowered in the component's environment: the `requires` names and
the component's resources are in scope. A call from a provide method is an
ordinary call, marked `emit` exactly when the private fn emits.

### B. A service as a parameter type of a module `fn`

```text
fn report(out: Out, x: Int) -> Int { return out.say(x) }
```

The module `fn` receives the handle and calls it.

## Recommendation: A, lowered by expansion at the call site

### Why not B

1. **The crossing is judged in a body that belongs to no component.** A module
   `fn` is shared by every component of the program, while the marker, the
   approval floor, the provider upper bound and the `carrying(...)` attribution
   are decided per component from its `requires` clause.
   docs/design/1437-emit-marks-every-crossing.md (issue #1682) already
   judges a crossing through a provide method's own service-typed parameter
   in the method body (`Env.service_params`), which keeps the check modular.
   That precedent could be extended to a module `fn`, but the `fn` would then
   be judged against no declared bound (a method has its service
   operation's), so B needs a declaration for that bound, or caller-side
   checking, which 1437 rejected.
2. **A service becomes a value that crosses a `fn` boundary on every tier.**
   Today a service is a receiver in a component body, an arrow parameter and
   a provide method's parameter, each with a dedicated rule. A module `fn`
   parameter is a fourth place, with its own handling in the reference
   lowering, the self-host checker and the emitters, and the emission-class
   inference of a module `fn` (a fixed point over the fns it calls) would
   have to read the declared operation of a parameter's static type.
3. The one advantage of B, sharing across components, is not what the issue
   asks for.

### Why A

1. **The judgments that matter stay where they are.** The private fn's body is
   judged in the component's own environment with the component's own
   `requires`. The marker, the approval floor and the provider upper bound all
   run per calling method over the expanded body, and `carrying(...)`
   attribution is unchanged. One inference is new: the module-fn fixed point
   that decides whether a call is an emission (`emitting_fns` and
   `emitting_caps` in `lower.py`) has to learn private fns too, so an
   unmarked call to an emitting private fn is refused with the existing G4
   message.
2. **Nothing new reaches the emitters, if the expansion is a plain splice.**
   Each call is expanded while lowering: the calling method receives the
   statements of the private fn with its parameters bound as `let`s and its
   locals renamed per call site, so the IR is what the author would have
   written by copying the lines. All six tiers, the recorder and the self-host
   emitters then run it unchanged; the self-host needs the same expansion in
   `selfhost/lower.rvl` and the same refusals in its checker. A splice is
   plain only for a body whose single `return` is its last statement (rule 7).
   An early `return` in a nested branch needs a synthetic result variable and
   guard flags, and a `break` inside loops; that is new lowering, so it is
   left out of the first slice.
3. **The recorded steps name the service method crossed.** The steps in the
   expanded body are the author's `emit gate.admit(...)`, so nothing is
   recorded under the shared fn's name. This is the third acceptance point of
   the issue. The test must cover the value form (`let f = emit
   gate.admit(...)`), which is the form the playground uses and the form
   #2969 reports as recording no emission step for a module `fn` called with
   `emit`: the recorded step of a value-position emission through a required
   service is the thing to pin on every tier, not assume.
4. **The `effect` teardown contract is unchanged**, because `effect` stays out
   of a private fn (rule 4).

## Rules proposed for A

1. **Declaration.** `fn name(p: T, ...) -> R { ... }` in a component body,
   before or after the `provide` blocks. Parameter types are required (a
   private fn has no service to take them from); a value-returning fn
   declares `-> R`. Not exposed through `provides`. The name must be new in
   the component's scope (a `requires` name, a resource, a config field or
   another private fn is a duplicate).
2. **Scope.** The component's `requires` names, resources and config are in
   scope. The calling method's own parameters and locals are not.
3. **Body grammar.** The provide-method statement grammar: `let`, `var` and
   assignment, `emit`, `if`, `while`, `for` with `break` and `continue`,
   `return`. No `fail` (a provide method refuses it, A8) and no `effect`
   (rule 4).
4. **No `effect` and no resource declaration in a private fn.** Steps that
   open a recorded bracket stay at the top level of a provide method, so
   teardown order is as the method's author wrote it.
5. **Emission class is inferred**, as for a module `fn`: a private fn emits
   if its body contains an `emit` or calls a private fn that emits. A call to
   an emitting private fn must be marked `emit`; the unmarked call is the
   existing G4 refusal ("call to emission `name` must be marked `emit` (G4)").
   Reading a service operation that is not an `emission fn` is not an
   emission and needs no marker.
6. **Calls.** Only from a provide method or another private fn of the same
   component, and only as a statement or as the right-hand side of a `let`
   (`emit report(f)` or `let r = emit report(f)`), never nested in a larger
   expression, so the expansion does not have to hoist statements out of an
   expression and keep their evaluation order. No recursion, direct or
   mutual (the expansion must end), with a refusal naming the cycle. A
   private fn is not a value (it cannot be passed as an argument).
7. **`return` ends the private fn, not the calling method, and is the last
   statement of the body.** The caller tests the result and returns itself,
   as the playground's `if (f.refused) { emit publish(...) return }` already
   does with its own `return`. Whether a private fn may also end the calling
   method, or return early from a nested branch, is an open question below.

## Open questions for review

1. **Option A or B.** The recommendation above.
2. **May a private fn return early, or end the calling method?** Proposed no
   for the first slice (rule 7). The cost is a boolean or an `Opt` the caller
   tests. The benefit is that the expansion stays a plain statement splice
   and the method's return type check is unchanged. Allowing it is a
   follow-up that adds a synthetic result variable and guard flags.
3. **Typed parameters only**, or inference from the call site like an arrow?
   Proposed: typed only, as for a module `fn`.
4. **`async`.** A private fn called from an `async fn` provide method inherits
   its colour; a private fn is not itself declared `async`. Proposed for the
   first slice, to be confirmed on the tiers that run async methods.

## Slices

1. Parser and the reference lowering, with the checks of rules 1 to 7, the
   reference tier and the recorder test for the step names (value form
   included).
2. The self-host lowering and checker, and the `emit_py_corpus` fixture that
   makes both agree.
3. docs/syntax-2.0.md, and the playground's three `Gate` sequences as the
   first real user (the playground is not in this repository).

Each slice carries its own tests: a program where two methods share a private
fn that calls a required service on every tier that runs provide methods, an
unmarked call to an emitting private fn refused, and the recorded steps naming
the service operation.

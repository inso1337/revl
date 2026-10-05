/-!
# G4, the deferred-position rule: where a `deferred` emission may be reached

Item 400, extended by issue #1457, and stated here for issue #1742. A
`deferred` emission extern (`extern emission deferred fn deliver(...)`) does
not fire when it is called. An `emit`-marked call ENQUEUES it onto the session
deferral queue, and the queue fires at the session commit
(docs/design/245-session-commit.md). That queue exists only inside a component
activation or a provide-method body, so the checker refuses every other way of
reaching the extern, with code G4, category `deferred`
(`src/revl/lower.py`, the walk in `_refuse_teardown_externs_in_fn_bodies` and
`_deferred_value_refusal`):

* a CALL in the body of a `fn` or a `test`, including one inside an arrow
  written there: neither body has a session commit for the deferral to fire
  at;
* a VALUE reference anywhere, a component included (`apply(deliver, ...)`):
  whoever calls the value fires the host body at once, with no `emit` marker to
  enqueue it.

A CALL in a component is not this rule's business. An unmarked one is the
MARKER rule's refusal (`Oracle.g4OK` over the `U` rows); a marked one is the
enqueue the extern exists for.

## The model

The export carries one fact per reach of a `deferred` extern (`DR` row): the
SCOPE the reach sits in (`fn`, `test`, `component`) and its POSITION (`call`,
`arrow` for a call inside an arrow, `value`). The positions are read off the
same AST walk the checker makes; the rule is stated here. `legalB` is the rule
for one reach, `deferredB` for a file's reaches, and `deferredB_iff` is the
bridge the oracle's `DF` row rests on: the printed Bool is the declarative
`DeferredOK`.

## What this does not cover

The rule is about POSITIONS. Whether a component's marked call is the head of
its `emit` (and not an argument inside it) is the marker rule, and whether the
enqueued emission fires once at commit is the session-commit runtime, outside
this model.
-/

namespace RevL.G4Deferred

/-- Where a reach of a `deferred` extern sits. -/
inductive Scope where
  /-- The body of a top-level `fn`. -/
  | fn
  /-- The body of a `test` block. -/
  | test
  /-- A component: its activation body or one of its provide methods. -/
  | component
  deriving Repr, DecidableEq

/-- How the extern is reached. -/
inductive Pos where
  /-- The callee of a call (`deliver(a, b)`). -/
  | call
  /-- The callee of a call written inside an arrow (`(x) => deliver(x, y)`). -/
  | arrow
  /-- A reference that is not a callee: the extern as a function value. -/
  | value
  deriving Repr, DecidableEq

/-- One reach of a `deferred` extern (one `DR` fact). -/
structure Reach where
  scope : Scope
  pos : Pos
  deriving Repr, DecidableEq

/-- The rule for one reach, declaratively: only a call in a component may
reach a `deferred` extern, there it is the marker rule that decides the
rest, and a value is never legal. -/
def Legal (r : Reach) : Prop := r.scope = .component ∧ r.pos ≠ .value

/-- The rule for one reach, as the decider the oracle runs. -/
def legalB : Reach → Bool
  | ⟨.component, .call⟩ => true
  | ⟨.component, .arrow⟩ => true
  | _ => false

theorem legalB_iff (r : Reach) : legalB r = true ↔ Legal r := by
  obtain ⟨s, p⟩ := r
  cases s <;> cases p <;> simp [legalB, Legal]

/-- A file's verdict: every reach of a `deferred` extern is legal. -/
def DeferredOK (rs : List Reach) : Prop := ∀ r ∈ rs, Legal r

/-- The file verdict, as the decider the oracle runs. -/
def deferredB (rs : List Reach) : Bool := rs.all legalB

/-- **The `DF` verdict is the rule.** The oracle prints `deferredB`; this is
what makes the printed Bool the model's judgment. -/
theorem deferredB_iff (rs : List Reach) : deferredB rs = true ↔ DeferredOK rs := by
  simp only [deferredB, List.all_eq_true, DeferredOK]
  exact forall_congr' fun r => imp_congr_right fun _ => legalB_iff r

/-- A value reference is refused in every scope, a component included. -/
theorem value_never_legal (s : Scope) : ¬ Legal ⟨s, .value⟩ := by
  intro h
  exact h.2 rfl

/-- A call in a `fn` or `test` body is refused, inside an arrow or not:
neither body has a session commit. -/
theorem body_reach_refused (p : Pos) :
    ¬ Legal ⟨.fn, p⟩ ∧ ¬ Legal ⟨.test, p⟩ := by
  constructor <;> intro h <;> exact Scope.noConfusion h.1

/-- One illegal reach refuses the file, wherever it sits in the list. -/
theorem refused_of_mem (rs : List Reach) (r : Reach) (hr : r ∈ rs)
    (hbad : ¬ Legal r) : ¬ DeferredOK rs :=
  fun h => hbad (h r hr)

/-! ### The corpus shapes (tests/fixtures/deferred_reach/) -/

/-- `ok_emit_step.rvl`: the extern called under `emit` in a provide method. -/
def okEmitStep : List Reach := [⟨.component, .call⟩]

/-- `g4_call_in_fn_body.rvl`: a call in a `fn` body. -/
def callInFn : List Reach := [⟨.component, .call⟩, ⟨.fn, .call⟩]

/-- `g4_call_in_arrow_in_fn_body.rvl`: a call inside an arrow in a `fn` body. -/
def arrowInFn : List Reach := [⟨.fn, .arrow⟩, ⟨.component, .call⟩]

/-- `g4_value_in_component.rvl`: the extern passed as a value in a provide
method. -/
def valueInComponent : List Reach := [⟨.component, .value⟩]

theorem fixtures_decided :
    deferredB okEmitStep = true ∧ deferredB callInFn = false
      ∧ deferredB arrowInFn = false ∧ deferredB valueInComponent = false :=
  ⟨rfl, rfl, rfl, rfl⟩

/-- **Non-vacuity**: the rule both admits and refuses, and the refused file
differs from the admitted one by one reach. -/
theorem deferred_not_vacuous :
    DeferredOK okEmitStep ∧ ¬ DeferredOK callInFn ∧ ¬ DeferredOK valueInComponent :=
  ⟨(deferredB_iff _).mp rfl,
   fun h => absurd ((deferredB_iff _).mpr h) (by decide),
   fun h => absurd ((deferredB_iff _).mpr h) (by decide)⟩

end RevL.G4Deferred

/-!
# G4, the emission value rule: an emission is reached only by a call

Issues #2842 and #2834. An emission extern (`extern emission fn send(...)`)
crosses an irreversible boundary. The crossing is recorded as an emission
step and metered against its declared ceiling only at a call site the author
marked `emit`. A reference that hands the extern on as a value (bound to a
local, passed as an argument, stored in a record or a list, returned from a
`fn`) lets whoever calls the value cross the boundary with no such site. So
the checker refuses, with code G4, category `emission-value`
(`src/revl/lower.py`, `_emission_value_refusal` and `_emission_arrow_refusal`):

* a VALUE reference to an emission extern, or to a `fn` that reaches one,
  in a `fn` body or a component;
* a CALL to one of them written inside an arrow in a `fn` body: the arrow is
  itself such a value.

A CALL is not this rule's business. In a component an unmarked one is the
MARKER rule's refusal (`Oracle.g4OK` over the `U` rows), and so is an
unmarked call inside an arrow there; a marked one is the crossing the extern
exists for. In a `fn` body the call is reached through the `fn`, whose own
call site the marker rule judges.

## The model

The export carries one fact per reach (`EVR` row): the SCOPE it sits in
(`fn`, `component`) and its POSITION (`call`, `arrow` for a call inside an
arrow, `value`). The positions are read off the same AST walk as the `DR`
rows of `RevL.G4Deferred`; the names they are keyed by (the emission externs
and the `fn`s reaching one) are a closure the export computes on its own. The
rule is stated here. `legalB` is the rule for one reach, `emissionValueB` for
a file's reaches, and `emissionValueB_iff` is the bridge the oracle's `EV`
row rests on.

## What this does not cover

`test` bodies: the checker's rule does not read them, and the export carries
no row for them. An `async` emission used as a value is refused by the A1
rule first. Whether the `emit`-marked call then records its step and enforces
its ceiling at run time is the runtime's seam, outside this model.
-/

namespace RevL.G4EmissionValue

/-- Where a reach sits. -/
inductive Scope where
  /-- The body of a top-level `fn`. -/
  | fn
  /-- A component: its activation body or one of its provide methods. -/
  | component
  deriving Repr, DecidableEq

/-- How the name is reached. -/
inductive Pos where
  /-- The callee of a call (`send(x)`). -/
  | call
  /-- The callee of a call written inside an arrow (`(x) => send(x)`). -/
  | arrow
  /-- A reference that is not a callee: the name as a function value. -/
  | value
  deriving Repr, DecidableEq

/-- One reach of an emission, or of a `fn` reaching one (one `EVR` fact). -/
structure Reach where
  scope : Scope
  pos : Pos
  deriving Repr, DecidableEq

/-- The rule for one reach, declaratively: a call is always legal here, a
call inside an arrow only in a component, and a value never. -/
def Legal (r : Reach) : Prop :=
  r.pos = .call ∨ (r.scope = .component ∧ r.pos = .arrow)

/-- The rule for one reach, as the decider the oracle runs. -/
def legalB : Reach → Bool
  | ⟨_, .call⟩ => true
  | ⟨.component, .arrow⟩ => true
  | _ => false

theorem legalB_iff (r : Reach) : legalB r = true ↔ Legal r := by
  obtain ⟨s, p⟩ := r
  cases s <;> cases p <;> simp [legalB, Legal]

/-- A file's verdict: every reach is legal. -/
def EmissionValueOK (rs : List Reach) : Prop := ∀ r ∈ rs, Legal r

/-- The file verdict, as the decider the oracle runs. -/
def emissionValueB (rs : List Reach) : Bool := rs.all legalB

/-- **The `EV` verdict is the rule.** The oracle prints `emissionValueB`;
this is what makes the printed Bool the model's judgment. -/
theorem emissionValueB_iff (rs : List Reach) :
    emissionValueB rs = true ↔ EmissionValueOK rs := by
  simp only [emissionValueB, List.all_eq_true, EmissionValueOK]
  exact forall_congr' fun r => imp_congr_right fun _ => legalB_iff r

/-- A value reference is refused in every scope, a component included. -/
theorem value_never_legal (s : Scope) : ¬ Legal ⟨s, .value⟩ := by
  intro h
  rcases h with h | ⟨_, h⟩ <;> exact Pos.noConfusion h

/-- An arrow calling an emission in a `fn` body is refused: the arrow is the
emission handed on as a value. -/
theorem arrow_in_fn_refused : ¬ Legal ⟨.fn, .arrow⟩ := by
  intro h
  rcases h with h | ⟨h, _⟩
  · exact Pos.noConfusion h
  · exact Scope.noConfusion h

/-- A call is legal in every scope: the rule never refuses a named call. -/
theorem call_always_legal (s : Scope) : Legal ⟨s, .call⟩ := Or.inl rfl

/-- One illegal reach refuses the file, wherever it sits in the list. -/
theorem refused_of_mem (rs : List Reach) (r : Reach) (hr : r ∈ rs)
    (hbad : ¬ Legal r) : ¬ EmissionValueOK rs :=
  fun h => hbad (h r hr)

/-! ### The corpus shapes (tests/fixtures/emission_value/) -/

/-- `ok_emit_call.rvl`: the emission called in a `fn` and under `emit` in a
provide method, and the `fn` called under `emit` there. -/
def okEmitCall : List Reach :=
  [⟨.fn, .call⟩, ⟨.component, .call⟩, ⟨.component, .call⟩]

/-- `g4_value_in_component.rvl`: a `fn` reaching the emission bound to a
local in a provide method. -/
def valueInComponent : List Reach := [⟨.fn, .call⟩, ⟨.component, .value⟩]

/-- `g4_value_in_fn_body.rvl`: a `fn` returns the emission as a value. -/
def valueInFn : List Reach := [⟨.fn, .value⟩, ⟨.component, .call⟩]

/-- `g4_arrow_in_fn_body.rvl`: a `fn` returns an arrow calling the
emission. -/
def arrowInFn : List Reach := [⟨.fn, .arrow⟩, ⟨.component, .call⟩]

theorem fixtures_decided :
    emissionValueB okEmitCall = true ∧ emissionValueB valueInComponent = false
      ∧ emissionValueB valueInFn = false ∧ emissionValueB arrowInFn = false :=
  ⟨rfl, rfl, rfl, rfl⟩

/-- **Non-vacuity**: the rule both admits and refuses, and each refused file
differs from an admitted shape by one reach. -/
theorem emission_value_not_vacuous :
    EmissionValueOK okEmitCall ∧ ¬ EmissionValueOK valueInComponent
      ∧ ¬ EmissionValueOK valueInFn ∧ ¬ EmissionValueOK arrowInFn :=
  ⟨(emissionValueB_iff _).mp rfl,
   fun h => absurd ((emissionValueB_iff _).mpr h) (by decide),
   fun h => absurd ((emissionValueB_iff _).mpr h) (by decide),
   fun h => absurd ((emissionValueB_iff _).mpr h) (by decide)⟩

end RevL.G4EmissionValue

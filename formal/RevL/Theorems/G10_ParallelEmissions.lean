import RevL.Lemmas.WalLemmas

/-
RevL.Theorems.G10_ParallelEmissions: the two-effect parallel fan-out group (issue #2741).

`docs/design/2741-parallel-fanout-formalism.md` chooses an interleaving
extension of the sequential small-step relation. This L2 file carries the
extension relation over `SemStep`, the two-member group, the gate
`backends/python/emit.py`'s `_group_eligible` applies, and the observable
effect trace a run leaves, together with the effect-equivalence theorem.

The model is deliberately smaller than the runtime:
* a member has ONE forward effect step and an optional compensation;
* duplicate delivery is the worst case the gate exists for (a retry after a
  fault, or an external dispose mid-group), and idempotence collapses it;
* the group is the interleaving of its members, so a failed member
  (`Body.fail`, which is stuck in `SemStep`) does not stop the other member
  from taking its step. That is the fault case P09 withdraws byte-identity
  for.

Nothing here edits `SemStep`, the G7 teardown stack or the A8 WAL run.
-/

namespace RevL.Fanout

open RevL.Lemmas

/-! ## The interleaving extension of `SemStep` -/

/-- A group step: one member takes a `SemStep`, the other is unchanged.
This is the interleaving extension the note chooses. It adds no
constructor to `SemStep`, so G7 and A8 keep their statements. -/
inductive ParStep : (Config × Config) → (Config × Config) → Prop where
  | left : ∀ {a b c : Config}, SemStep a b → ParStep (a, c) (b, c)
  | right : ∀ {a c d : Config}, SemStep c d → ParStep (a, c) (a, d)

/-- A group run: the reflexive-transitive closure of `ParStep`. -/
inductive ParSteps : (Config × Config) → (Config × Config) → Prop where
  | refl : ∀ {g}, ParSteps g g
  | step : ∀ {g h k}, ParStep g h → ParSteps h k → ParSteps g k

/-- The first coordinate of a group run is a sequential run under
`SemStep`. -/
theorem ParSteps.left {g g' : Config × Config} (h : ParSteps g g') :
    SemSteps g.1 g'.1 := by
  induction h with
  | refl => exact .refl
  | step hs _ ih =>
    cases hs with
    | left hstep => exact .step hstep ih
    | right _ => exact ih

/-- The second coordinate of a group run is a sequential run under
`SemStep`. -/
theorem ParSteps.right {g g' : Config × Config} (h : ParSteps g g') :
    SemSteps g.2 g'.2 := by
  induction h with
  | refl => exact .refl
  | step hs _ ih =>
    cases hs with
    | left _ => exact ih
    | right hstep => exact .step hstep ih

/-! ## Observable effects -/

/-- One observable effect token. A forward token is a member's delivery; a
compensation token is the inverse the join registers for a member that
fired. -/
inductive Eff where
  | fwd (seq : Seq)
  | comp (seq : Seq)
  deriving DecidableEq, Repr

abbrev Trace := List Eff

/-- Add a forward token. An idempotent delivery adds it at most once; a
non-idempotent one adds a duplicate, which is the difference the gate
exists for. -/
def addFwd (idem : Bool) (s : Seq) (t : Trace) : Trace :=
  if idem then (if Eff.fwd s ∈ t then t else Eff.fwd s :: t)
  else Eff.fwd s :: t

/-- Add a compensation token under the same idempotence split. -/
def addComp (idem : Bool) (s : Seq) (t : Trace) : Trace :=
  if idem then (if Eff.comp s ∈ t then t else Eff.comp s :: t)
  else Eff.comp s :: t

theorem addFwd_idem (s : Seq) (t : Trace) :
    addFwd true s (addFwd true s t) = addFwd true s t := by
  unfold addFwd
  by_cases h : Eff.fwd s ∈ t
  · simp [h]
  · simp [h]

theorem addComp_idem (s : Seq) (t : Trace) :
    addComp true s (addComp true s t) = addComp true s t := by
  unfold addComp
  by_cases h : Eff.comp s ∈ t
  · simp [h]
  · simp [h]

/-- A compensation token never enters a forward append, whatever the
idempotence flag: the two effect constructors are disjoint. -/
theorem comp_mem_addFwd (idem : Bool) (x : Seq) (t : Trace) (s : Seq) :
    Eff.comp s ∈ addFwd idem x t ↔ Eff.comp s ∈ t := by
  unfold addFwd
  by_cases hi : idem
  · simp [hi]
    by_cases hm : Eff.fwd x ∈ t <;> simp [hm]
  · simp [hi]

/-- A compensation token enters a compensation append exactly when it is
the member's token or was already present, whatever the idempotence flag. -/
theorem comp_mem_addComp (idem : Bool) (x : Seq) (t : Trace) (s : Seq) :
    Eff.comp s ∈ addComp idem x t ↔ s = x ∨ Eff.comp s ∈ t := by
  unfold addComp
  by_cases hi : idem
  · simp [hi]
    by_cases hm : Eff.comp x ∈ t
    · simp [hm]
      intro hs
      simpa [hs] using hm
    · simp [hm]
  · simp [hi]

/-! ## Members and the gate -/

/-- A member either takes its forward step or faults before it. -/
inductive Mode where
  | fire
  | fault
  deriving DecidableEq, Repr

namespace Mode

def isFire : Mode → Bool
  | .fire => true
  | .fault => false

def isFault : Mode → Bool
  | .fire => false
  | .fault => true

end Mode

/-- One group member. `forwardIdem` and `compIdem` are the declarations
`_group_eligible` reads; `hasComp` says whether a `compensate` clause is
present. -/
structure Member where
  seq : Seq
  mode : Mode := .fire
  forwardIdem : Bool := true
  hasComp : Bool := false
  compIdem : Bool := false
  deriving DecidableEq, Repr

/-- The emitter's admission gate: idempotent forward delivery, and a
compensation that is idempotent or absent. -/
def Member.admitted (m : Member) : Prop :=
  m.forwardIdem = true ∧ (m.hasComp = false ∨ m.compIdem = true)

theorem admitted_forward {m : Member} (h : m.admitted) :
    m.forwardIdem = true := h.1

/-- Under the gate, a present compensation is declared idempotent. -/
theorem admitted_comp_idem {m : Member} (h : m.admitted)
    (hc : m.hasComp = true) : m.compIdem = true := by
  rcases h.2 with hA | hI
  · rw [hc] at hA; exact Bool.noConfusion hA
  · exact hI

/-! ## Traces of a run -/

/-- A firing member's forward attempt; a failed member takes no step. -/
def Member.fireTrace (m : Member) (t : Trace) : Trace :=
  if m.mode.isFire then addFwd m.forwardIdem m.seq t else t

/-- A firing member's compensation token, if it declared one. -/
def Member.compTrace (m : Member) (t : Trace) : Trace :=
  if m.mode.isFire then
    (if m.hasComp then addComp m.compIdem m.seq t else t)
  else t

/-- Fire every member in the list twice: the duplicate delivery a retry
after a fault or a divert can produce. The gate makes it harmless. -/
def firePack (ms : List Member) (t : Trace) : Trace :=
  ms.foldr (fun m acc => m.fireTrace (m.fireTrace acc)) t

/-- Compensate every member in the list twice: the duplicate the external
dispose residual can produce. -/
def compPack (ms : List Member) (t : Trace) : Trace :=
  ms.foldr (fun m acc => m.compTrace (m.compTrace acc)) t

/-- The members strictly before the first failed one, in order. -/
def takeFires : List Member → List Member
  | [] => []
  | m :: ms => if m.mode.isFault then [] else m :: takeFires ms

def anyFault : List Member → Bool
  | [] => false
  | m :: ms => m.mode.isFault || anyFault ms

/-- The parallel trace: the group fires every non-failed member in plan
order, then, if a member failed, the join registers every fired member's
compensation in plan order. A failed member contributes no forward token. -/
def parTrace (m1 m2 : Member) : Trace :=
  let ms := [m1, m2]
  let t := firePack ms []
  if anyFault ms then compPack ms t else t

/-- The sequential trace in an order: the prefix before the first failed
member fires once, then, on a fault, the fired prefix is compensated once
in plan order. `first = true` is plan order `[m1, m2]`; `false` swaps. -/
def seqTrace (first : Bool) (m1 m2 : Member) : Trace :=
  let ms := if first then [m1, m2] else [m2, m1]
  let fired := takeFires ms
  let t := fired.foldr (fun m acc => m.fireTrace acc) []
  if anyFault ms then fired.foldr (fun m acc => m.compTrace acc) t else t

/-! ## The two-effect group theorem -/

/-- Under the gate a firing member's compensation attempt may be read with
its idempotence fixed to true, once the member is known to fire. -/
theorem compTrace_fire_admitted {m : Member} (h : m.admitted)
    (hm : m.mode = .fire) (t : Trace) :
    m.compTrace t = (if m.hasComp then addComp true m.seq t else t) := by
  unfold Member.compTrace
  rw [hm]
  by_cases hc : m.hasComp
  · rw [admitted_comp_idem h hc]
    simp [Mode.isFire, hc]
  · simp [Mode.isFire, hc]

/-- **The two-effect group is effect-equivalent to some sequential order,
under the gate.** For an admitted two-member group, the parallel trace (with
the duplicate delivery a retry can produce) equals the sequential trace of
the order in which every firing member precedes the failed one. If no member
fails, plan order is the witness; if the plan's first member fails, the
witness is the reverse order. -/
theorem two_effect_group_effect_equiv (m1 m2 : Member)
    (h1 : m1.admitted) (h2 : m2.admitted) :
    parTrace m1 m2 = seqTrace m1.mode.isFire m1 m2 := by
  cases hm1 : m1.mode
  · cases hm2 : m2.mode
    · -- both members fire: only the forward idempotence is used
      simp [parTrace, seqTrace, firePack, takeFires, anyFault,
        Member.fireTrace, hm1, hm2, Mode.isFire, Mode.isFault,
        admitted_forward h1, admitted_forward h2, addFwd_idem]
    · -- the first member fires, the second fails
      by_cases hc1 : m1.hasComp
      · simp [parTrace, seqTrace, firePack, compPack, takeFires, anyFault,
          Member.fireTrace, Member.compTrace, hm1, hm2, hc1,
          Mode.isFire, Mode.isFault, admitted_forward h1, addFwd_idem,
          addComp_idem, admitted_comp_idem h1 hc1]
      · simp [parTrace, seqTrace, firePack, compPack, takeFires, anyFault,
          Member.fireTrace, Member.compTrace, hm1, hm2, hc1,
          Mode.isFire, Mode.isFault, admitted_forward h1, addFwd_idem]
  · cases hm2 : m2.mode
    · -- the first member fails, the second fires: the reverse order is the witness
      by_cases hc2 : m2.hasComp
      · simp [parTrace, seqTrace, firePack, compPack, takeFires, anyFault,
          Member.fireTrace, Member.compTrace, hm1, hm2, hc2,
          Mode.isFire, Mode.isFault, admitted_forward h2, addFwd_idem,
          addComp_idem, admitted_comp_idem h2 hc2]
      · simp [parTrace, seqTrace, firePack, compPack, takeFires, anyFault,
          Member.fireTrace, Member.compTrace, hm1, hm2, hc2,
          Mode.isFire, Mode.isFault, admitted_forward h2, addFwd_idem]
    · -- both members fail: both traces are empty
      simp [parTrace, seqTrace, firePack, compPack, takeFires, anyFault,
        Member.fireTrace, Member.compTrace, hm1, hm2, Mode.isFire,
        Mode.isFault]

/-- **A compensation token belongs to a member that fired and registered
one.** This is the trace-level half of "compensation runs only when
allowed": the join's compensation set is built from the branch results of
members that fired, never from a failed or unfired member. -/
theorem par_comp_token_is_a_registered_member (m1 m2 : Member) (s : Seq)
    (h : Eff.comp s ∈ parTrace m1 m2) :
    (m1.mode = .fire ∧ m1.hasComp = true ∧ s = m1.seq) ∨
      (m2.mode = .fire ∧ m2.hasComp = true ∧ s = m2.seq) := by
  cases hm1 : m1.mode <;> cases hm2 : m2.mode
  · -- both fire: no fault, so no compensation token at all
    simp [parTrace, anyFault, firePack, Member.fireTrace, hm1, hm2,
      Mode.isFire, Mode.isFault, comp_mem_addFwd] at h
  · -- the first member fires, the second fails
    cases hc1 : m1.hasComp
    · simp [parTrace, anyFault, firePack, compPack, Member.fireTrace,
        Member.compTrace, hm1, hm2, hc1, Mode.isFire, Mode.isFault,
        comp_mem_addFwd, comp_mem_addComp] at h ⊢
    · simp [parTrace, anyFault, firePack, compPack, Member.fireTrace,
        Member.compTrace, hm1, hm2, hc1, Mode.isFire, Mode.isFault,
        comp_mem_addFwd, comp_mem_addComp] at h ⊢
      exact h
  · -- the first member fails, the second fires
    cases hc2 : m2.hasComp
    · simp [parTrace, anyFault, firePack, compPack, Member.fireTrace,
        Member.compTrace, hm1, hm2, hc2, Mode.isFire, Mode.isFault,
        comp_mem_addFwd, comp_mem_addComp] at h ⊢
    · simp [parTrace, anyFault, firePack, compPack, Member.fireTrace,
        Member.compTrace, hm1, hm2, hc2, Mode.isFire, Mode.isFault,
        comp_mem_addFwd, comp_mem_addComp] at h ⊢
      exact h
  · -- no member fired, so there is no compensation token
    simp [parTrace, anyFault, firePack, compPack, Member.fireTrace,
      Member.compTrace, hm1, hm2, Mode.isFire, Mode.isFault] at h

/-- **Compensation runs only when allowed,** in the form the theorem uses:
a parallel compensation token names a member that fired, carried a
compensation, and under the gate that compensation is idempotent. -/
theorem compensation_only_when_allowed (m1 m2 : Member)
    (h1 : m1.admitted) (h2 : m2.admitted) (s : Seq)
    (h : Eff.comp s ∈ parTrace m1 m2) :
    (m1.mode = .fire ∧ m1.hasComp = true ∧ s = m1.seq ∧
        m1.compIdem = true) ∨
      (m2.mode = .fire ∧ m2.hasComp = true ∧ s = m2.seq ∧
        m2.compIdem = true) := by
  rcases par_comp_token_is_a_registered_member m1 m2 s h with
    ⟨hm, hc, hs⟩ | ⟨hm, hc, hs⟩
  · exact Or.inl ⟨hm, hc, hs, admitted_comp_idem h1 hc⟩
  · exact Or.inr ⟨hm, hc, hs, admitted_comp_idem h2 hc⟩

/-! ## Non-vacuity -/

/-- A clean two-effect group, both members admitted. -/
def cleanA : Member :=
  { seq := 1, mode := .fire, forwardIdem := true, hasComp := true,
    compIdem := true }

def cleanB : Member :=
  { seq := 2, mode := .fire, forwardIdem := true, hasComp := true,
    compIdem := true }

/-- The group's first member fails, the second fires and carries an
idempotent compensation. Plan order would fire nothing; the reverse order
fires and compensates the second member. -/
def faultA : Member :=
  { seq := 1, mode := .fault, forwardIdem := true }

def faultB : Member :=
  { seq := 2, mode := .fire, forwardIdem := true, hasComp := true,
    compIdem := true }

/-- The stale `accumulated` claim's counterexample: the fault case where
plan order is NOT the equivalent order. -/
theorem fault_group_plan_order_is_not_the_witness :
    parTrace faultA faultB ≠ seqTrace true faultA faultB := by decide

theorem fault_group_reverse_order_is_the_witness :
    parTrace faultA faultB = seqTrace false faultA faultB := by decide

theorem clean_group_plan_order_is_the_witness :
    parTrace cleanA cleanB = seqTrace true cleanA cleanB := by decide

theorem clean_members_admitted : cleanA.admitted ∧ cleanB.admitted := by
  constructor <;> unfold Member.admitted <;> decide

theorem fault_members_admitted : faultA.admitted ∧ faultB.admitted := by
  constructor <;> unfold Member.admitted <;> decide

theorem fault_group_registers_the_fired_compensation :
    Eff.comp 2 ∈ parTrace faultA faultB := by decide

/-- Dropping the forward-idempotence conjunct: a duplicate forward breaks
the equivalence. -/
def dupFwdA : Member :=
  { seq := 1, mode := .fire, forwardIdem := false }

theorem non_idempotent_forward_breaks_the_gate :
    ¬ ∃ first : Bool, parTrace dupFwdA cleanB = seqTrace first dupFwdA cleanB := by
  decide

/-- Dropping the compensation-idempotence-or-absent conjunct: a duplicate
compensation breaks the equivalence. -/
def badCompA : Member :=
  { seq := 1, mode := .fire, forwardIdem := true, hasComp := true,
    compIdem := false }

def faultB2 : Member :=
  { seq := 2, mode := .fault, forwardIdem := true }

theorem non_idempotent_compensation_breaks_the_gate :
    ¬ ∃ first : Bool,
      parTrace badCompA faultB2 = seqTrace first badCompA faultB2 := by
  decide

end RevL.Fanout

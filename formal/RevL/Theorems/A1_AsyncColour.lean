/-!
# A1, async colour: where a suspension may be reached

STATUS.md listed A1 with no theorem and no oracle row, because L0 has no
`await` (issue #1808). The checker refuses, with code A1, category
`async-propagation` (`src/revl/lower.py`):

* a provide method its service declares SYNC whose implementation reaches an
  async operation ("`Http.post` is declared sync, but this implementation
  reaches async extern `http_post`");
* an `effect` or `emit` step that reaches an async operation but is not
  awaited (rule 1);
* an `effect await` or `await emit` that reaches nothing async (rule 2: an
  `await` is a real divert window, never decoration);
* an `undo` or a `compensate` that reaches an async operation (rule 3:
  teardown is synchronous on every tier);

and, uncoded, a provide method whose own `async` differs from its service
declaration's ("method `stats` of provision `db` is not async but service
Database declares it async").

## The model

A file's async names are its async externs and its async service operations
(spelled `<Service>.<op>`), and its call graph is the module `fn`s with their
bare-name callees (the `FN` rows the G5 row already reads). `ReachesAsync`
is reach within a fuel bound: a name is async, or calls one that reaches
async with one step less. `reachB` decides it (`reachB_iff`).

A SITE is a position the rules above judge, with the heads it calls. Its kind
fixes what the rule asks: an awaited step must reach async, a sync method, a
non-awaited step and a teardown slot must not, and an async method is free.
`siteB_iff` bridges the oracle's `A1` row, and `SigOK` (the implementation's
colour is its declaration's) the `A1S` row.

## What this does not cover

An arrow's type carries no async colour (`a1_async_arrow_sync_type.rvl`):
the model has no arrow types, so that refusal stays out of fragment. Colour
polymorphism (a `fn` async only through its own callback parameter) is not
in the call graph, which holds bare-name callees only.
-/

namespace RevL.A1Async

/-- The module call graph: each `fn` with its bare-name callees. -/
abbrev Graph := List (String × List String)

/-- The callees of `n`; a name that is not a `fn` calls nothing. -/
def callees (g : Graph) (n : String) : List String :=
  match g.find? (fun e => e.1 == n) with
  | some e => e.2
  | none => []

/-- Reach of an async name from `n` within `k` call steps. -/
inductive ReachesAsync (g : Graph) (as : List String) : Nat → String → Prop where
  | base {k : Nat} {n : String} : n ∈ as → ReachesAsync g as k n
  | step {k : Nat} {n c : String} :
      c ∈ callees g n → ReachesAsync g as k c → ReachesAsync g as (k + 1) n

/-- The reach, as the decider the oracle runs. -/
def reachB (g : Graph) (as : List String) : Nat → String → Bool
  | 0, n => as.contains n
  | k + 1, n => as.contains n || (callees g n).any (reachB g as k)

theorem reachB_iff (g : Graph) (as : List String) :
    ∀ (k : Nat) (n : String), reachB g as k n = true ↔ ReachesAsync g as k n
  | 0, n => by
      constructor
      · intro h
        exact .base (by simpa [reachB] using h)
      · intro h
        cases h with
        | base hm => simpa [reachB] using hm
  | k + 1, n => by
      have ih := reachB_iff g as k
      constructor
      · intro h
        simp only [reachB, Bool.or_eq_true, List.any_eq_true] at h
        rcases h with hm | ⟨c, hc, hr⟩
        · exact .base (by simpa using hm)
        · exact .step hc ((ih c).mp hr)
      · intro h
        simp only [reachB, Bool.or_eq_true, List.any_eq_true]
        cases h with
        | base hm => exact Or.inl (by simpa using hm)
        | step hc hr => exact Or.inr ⟨_, hc, (ih _).mpr hr⟩

/-- More fuel never loses a reach. -/
theorem reach_mono (g : Graph) (as : List String) :
    ∀ (k : Nat) (n : String), ReachesAsync g as k n → ReachesAsync g as (k + 1) n := by
  intro k n h
  induction h with
  | base hm => exact .base hm
  | step hc _ ih => exact .step hc ih

/-- The positions the A1 rules judge. -/
inductive Kind where
  /-- A provide method whose service declares it sync. -/
  | syncMethod
  /-- A provide method whose service declares it async. -/
  | asyncMethod
  /-- An `effect` acquisition without `await`. -/
  | effect
  /-- An `effect await` acquisition. -/
  | effectAwait
  /-- An `emit` step without `await`. -/
  | emit
  /-- An `await emit` step. -/
  | emitAwait
  /-- A bracket's `undo` slot. -/
  | undo
  /-- An `emit` step's `compensate` slot. -/
  | compensate
  deriving Repr, DecidableEq

/-- A site reaches async when one of its heads does. -/
def SiteReaches (g : Graph) (as : List String) (fuel : Nat) (heads : List String) : Prop :=
  ∃ h ∈ heads, ReachesAsync g as fuel h

/-- **The rule** for one site. -/
def SiteOK (g : Graph) (as : List String) (fuel : Nat) (k : Kind) (heads : List String) :
    Prop :=
  match k with
  | .effectAwait | .emitAwait => SiteReaches g as fuel heads
  | .asyncMethod => True
  | _ => ¬ SiteReaches g as fuel heads

/-- The rule, as the decider the oracle runs. -/
def siteB (g : Graph) (as : List String) (fuel : Nat) (k : Kind) (heads : List String) :
    Bool :=
  let r := heads.any (reachB g as fuel)
  match k with
  | .effectAwait | .emitAwait => r
  | .asyncMethod => true
  | _ => !r

theorem reaches_iff (g : Graph) (as : List String) (fuel : Nat) (heads : List String) :
    heads.any (reachB g as fuel) = true ↔ SiteReaches g as fuel heads := by
  simp only [List.any_eq_true, SiteReaches]
  exact exists_congr fun h => and_congr_right fun _ => reachB_iff g as fuel h

/-- **The `A1` verdict is the rule.** -/
theorem siteB_iff (g : Graph) (as : List String) (fuel : Nat) (k : Kind)
    (heads : List String) : siteB g as fuel k heads = true ↔ SiteOK g as fuel k heads := by
  have hr := reaches_iff g as fuel heads
  cases k <;> simp only [siteB, SiteOK] <;>
    first
    | exact hr
    | exact ⟨fun _ => trivial, fun _ => rfl⟩
    | (constructor
       · intro h hs
         rw [← hr] at hs
         rw [hs] at h
         exact Bool.noConfusion h
       · intro h
         cases hb : heads.any (reachB g as fuel) with
         | false => rfl
         | true => exact absurd (hr.mp hb) h)

/-- A teardown slot that reaches async is refused, whichever slot. -/
theorem teardown_suspension_refused (g : Graph) (as : List String) (fuel : Nat)
    (heads : List String) (h : SiteReaches g as fuel heads) :
    ¬ SiteOK g as fuel .undo heads ∧ ¬ SiteOK g as fuel .compensate heads :=
  ⟨fun hok => hok h, fun hok => hok h⟩

/-- An `await` must name a real divert window: an awaited step that reaches
nothing async is refused. -/
theorem await_without_async_refused (g : Graph) (as : List String) (fuel : Nat)
    (heads : List String) (h : ¬ SiteReaches g as fuel heads) :
    ¬ SiteOK g as fuel .effectAwait heads ∧ ¬ SiteOK g as fuel .emitAwait heads :=
  ⟨h, h⟩

/-- The exact pairing: the same heads are admitted under exactly one of a
step and its awaited form. -/
theorem await_pairing_exact (g : Graph) (as : List String) (fuel : Nat)
    (heads : List String) :
    (SiteOK g as fuel .effect heads ↔ ¬ SiteOK g as fuel .effectAwait heads)
      ∧ (SiteOK g as fuel .emit heads ↔ ¬ SiteOK g as fuel .emitAwait heads) :=
  ⟨Iff.rfl, Iff.rfl⟩

/-- A provide method's colour is its service declaration's (the `A1S` row). -/
def SigOK (declared impl : Bool) : Prop := declared = impl

def sigB (declared impl : Bool) : Bool := declared == impl

theorem sigB_iff (declared impl : Bool) : sigB declared impl = true ↔ SigOK declared impl := by
  simp [sigB, SigOK]

/-! ### The corpus shapes -/

/-- `a1_async_undo_suspends.rvl`: `open_conn` calls the async extern `ho`,
and the `undo` calls the async op `W.heat`. -/
def undoGraph : Graph := [("open_conn", ["ho"])]
def undoAsync : List String := ["ho", "W.heat"]

theorem fixtures_decided :
    siteB undoGraph undoAsync 2 .effectAwait ["open_conn"] = true
      ∧ siteB undoGraph undoAsync 2 .effect ["open_conn"] = false
      ∧ siteB undoGraph undoAsync 2 .undo ["W.heat"] = false
      ∧ siteB undoGraph undoAsync 2 .undo ["W.cool"] = true
      ∧ siteB [] ["Model.complete"] 1 .syncMethod ["Model.complete"] = false
      ∧ siteB [] ["Model.complete"] 1 .asyncMethod ["Model.complete"] = true
      ∧ siteB [] [] 1 .emitAwait ["Log.note"] = false
      ∧ sigB true false = false :=
  ⟨rfl, rfl, rfl, rfl, rfl, rfl, rfl, rfl⟩

/-- **Non-vacuity**: the reach goes through a `fn`, the awaited acquisition
is admitted and its unawaited twin refused, and a teardown slot is admitted
or refused by what it calls. -/
theorem a1_not_vacuous :
    ReachesAsync undoGraph undoAsync 2 "open_conn"
      ∧ SiteOK undoGraph undoAsync 2 .effectAwait ["open_conn"]
      ∧ ¬ SiteOK undoGraph undoAsync 2 .effect ["open_conn"]
      ∧ ¬ SiteOK undoGraph undoAsync 2 .undo ["W.heat"]
      ∧ SiteOK undoGraph undoAsync 2 .undo ["W.cool"] :=
  ⟨(reachB_iff _ _ _ _).mp rfl,
   (siteB_iff _ _ _ _ _).mp rfl,
   fun h => absurd ((siteB_iff _ _ _ _ _).mpr h) (by decide),
   fun h => absurd ((siteB_iff _ _ _ _ _).mpr h) (by decide),
   (siteB_iff _ _ _ _ _).mp rfl⟩

end RevL.A1Async

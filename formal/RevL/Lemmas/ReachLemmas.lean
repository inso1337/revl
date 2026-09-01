import RevL.Typing

/-!
RevL.Lemmas.ReachLemmas — L1 lemma farm: confinement helpers shared by
the G1 and G6 theorem files (which, per the layering rules in
STATUS.md, may not import each other).
-/

namespace RevL.Lemmas

open RevL.Typing RevL.Syntax

/-- An expression admitted under `C` reaches only keys in `C` — the
expr-level half of confinement. -/
theorem reach_confined : ∀ {C : Ctx} {e : Expr}, ReachIn C e →
    ∀ k ∈ heads e, k ∈ C := by
  intro C e hr
  induction hr with
  | lit s =>
    intro k hk
    simp [heads] at hk
  | call k args hk hargs ih =>
    intro k' hk'
    simp only [heads, List.mem_cons] at hk'
    rcases hk' with rfl | h'
    · exact hk
    · obtain ⟨a, ha, hk'a⟩ := List.mem_flatMap.mp h'
      exact ih a ha k' hk'a

/-- A statement admitted under `C` reaches only keys in `C` — the
statement-level half, used by both G1 and G6. -/
theorem typedIn_confined : ∀ {C : Ctx} {s : Stmt}, TypedIn C s →
    ∀ k ∈ stmtHeads s, k ∈ C := by
  intro C s ht
  cases ht with
  | pure e hr => exact reach_confined hr
  | effect m u hm hu =>
    intro k hk
    rcases List.mem_append.mp hk with h | h
    · exact reach_confined hm k h
    · exact reach_confined hu k h
  | emit m hm => exact reach_confined hm

end RevL.Lemmas

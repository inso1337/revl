import RevL.Manifest

/-!
RevL.Lemmas.ManifestLemmas — L1 lemma farm: manifest helpers shared by
the G2/G3 theorem files (which may not import each other).
-/

namespace RevL.Lemmas

open RevL.Manifest

/-- Membership in a composition's provision surface comes from a
component: the bridge between `flatMap` membership and the ∃-form the
closure statement uses. -/
theorem mem_flatMap_provides : ∀ {comps : List LComponent} {k : String},
    k ∈ comps.flatMap (·.provides) → ∃ p ∈ comps, k ∈ p.provides := by
  intro comps k h
  induction comps with
  | nil => simp [List.flatMap] at h
  | cons c comps ih =>
    simp only [List.flatMap_cons, List.mem_append] at h
    rcases h with h | h
    · exact ⟨c, by simp, h⟩
    · obtain ⟨p, hp, hk⟩ := ih h
      exact ⟨p, by simp [hp], hk⟩

end RevL.Lemmas

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
theorem mem_flatMap_slots : ∀ {comps : List LComponent} {s : Slot},
    s ∈ comps.flatMap slots → ∃ p ∈ comps, s ∈ slots p := by
  intro comps s h
  induction comps with
  | nil => simp [List.flatMap] at h
  | cons c comps ih =>
    simp only [List.flatMap_cons, List.mem_append] at h
    rcases h with h | h
    · exact ⟨c, by simp, h⟩
    · obtain ⟨p, hp, hk⟩ := ih h
      exact ⟨p, by simp [hp], hk⟩

/-- The converse direction, in the shape the layering proof consumes. -/
theorem slots_mem_flatMap : ∀ {comps : List LComponent} {p : LComponent}
    {s : Slot}, p ∈ comps → s ∈ slots p → s ∈ comps.flatMap slots := by
  intro comps p s hp hs
  induction comps with
  | nil => cases hp
  | cons c comps ih =>
    simp only [List.flatMap_cons, List.mem_append]
    cases hp with
    | head => exact Or.inl hs
    | tail _ h => exact Or.inr (ih h)

/-- Every slot a component of an admitted composition consumes is
provided by that same composition. This is the `LinkOK` fact G3's
layering construction needs, restated in the farm so `G3_NoCycles` need
not import `G2_ProvisionDisjoint`. -/
theorem linkOK_needs_mem : ∀ {comps : List LComponent}, LinkOK comps →
    ∀ p ∈ comps, ∀ s ∈ needs p, s ∈ comps.flatMap slots := by
  intro comps h
  induction h with
  | nil => intro p hp; cases hp
  | cons c comps _ _ hcl _ ih =>
    intro p hp s hs
    simp only [List.flatMap_cons, List.mem_append]
    cases hp with
    | head => exact Or.inr (hcl s hs)
    | tail _ h => exact Or.inr (ih p h s hs)

/-- `rankOf` never exceeds the number of components: the top of the
layering is the head's own rank, `comps.length`. -/
theorem rankOf_le : ∀ (comps : List LComponent) (s : Slot),
    rankOf comps s ≤ comps.length := by
  intro comps s
  induction comps with
  | nil => simp [rankOf]
  | cons c cs ih =>
    by_cases hc : s ∈ slots c
    · simp [rankOf, hc]
    · simp only [rankOf, hc, if_false, List.length_cons]
      exact Nat.le_succ_of_le ih

/-- A slot the head provides sits strictly above everything the tail
can provide. -/
theorem rankOf_head : ∀ (c : LComponent) (cs : List LComponent) (s : Slot),
    s ∈ slots c → rankOf (c :: cs) s = cs.length + 1 := by
  intro c cs s hs
  simp [rankOf, hs]

/-- A slot the head does not provide keeps the tail's rank, so ranks
assigned deeper in the admission order are stable as components are
added on top. -/
theorem rankOf_tail : ∀ (c : LComponent) (cs : List LComponent) (s : Slot),
    s ∉ slots c → rankOf (c :: cs) s = rankOf cs s := by
  intro c cs s hs
  simp [rankOf, hs]

end RevL.Lemmas

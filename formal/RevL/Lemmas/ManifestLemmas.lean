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

-- --------------------------------------------- composition of admissions

/-- Tail closure: stripping the later-admitted prefix `B` off an admitted
composition leaves the earlier-admitted suffix `A` admitted.

`B ++ A` is the model's reverse-load-order shape, so the prefix `B` is
what was admitted *after* `A`. This is the hot-swap direction a user
needs: withdrawing work that was admitted on top of `A` cannot
invalidate `A`'s own admission, and no component of `A` has to be
re-checked. -/
theorem linkOK_append_tail : ∀ {A B : List LComponent},
    LinkOK (B ++ A) → LinkOK A := by
  intro A B
  induction B with
  | nil => intro h; exact h
  | cons _ B ih =>
      intro h
      cases h with
      | cons _ _ _ _ _ htail => exact ih htail

/-- Per-component admission of a later-admitted part `B` over a fixed
base composition `A`: the three conditions `LinkOK`'s `cons` checks, with
the base's own admission taken as given rather than re-derived.

`B` is in the model's reverse-load-order shape, so a component's
requirements must be provided strictly later in `B` or anywhere in `A`,
never by itself or by an earlier entry of `B`. These are the conditions
the issue asks for: distinct slots, no clash with the rest of the
composition, and needs provided in `B` or `A` in order. -/
inductive AdmittedOver (A : List LComponent) : List LComponent → Prop where
  | nil : AdmittedOver A []
  | cons : ∀ c B,
      List.Nodup (slots c) →
      (∀ s ∈ slots c, s ∉ (B ++ A).flatMap slots) →
      (∀ s ∈ needs c, s ∈ (B ++ A).flatMap slots) →
      AdmittedOver A B →
      AdmittedOver A (c :: B)

/-- Extension: a composition `A` that links, plus a part `B` each of
whose components is admitted over it, composes to an admitted
composition `B ++ A`.

The base's admission is a hypothesis rather than something the
per-component conditions replay, which is what makes this compositional:
`B` can be admitted against a fixed `A` without re-checking `A`, and
`linkOK_append_tail` recovers `A` from the result. -/
theorem linkOK_append_of_admittedOver : ∀ {A B : List LComponent},
    LinkOK A → AdmittedOver A B → LinkOK (B ++ A) := by
  intro A B hA hAB
  induction hAB with
  | nil => exact hA
  | cons c B hnd hdis hcl _ ih =>
      exact LinkOK.cons c (B ++ A) hnd hdis hcl ih

-- ------------------------------------------------------- non-vacuity

/-- The base of the positive composition: one store providing `kv`. -/
def baseStore : LComponent :=
  { name := "BaseStore", requires := [], provides := ["kv"] }

/-- Admitted over `baseStore`: it needs `kv` from the base and provides
`cache` to anything admitted after it. -/
def cacheOver : LComponent :=
  { name := "CacheOver", requires := ["kv"], provides := ["cache"] }

/-- A second provider of `kv` in the shared realm. It is the negative
control: the extension side condition refuses it over `baseStore`, and
the union it would build is a G2 provision conflict. -/
def rivalStore : LComponent :=
  { name := "RivalStore", requires := [], provides := ["kv"] }

/-- Non-vacuity, admitting side: a concrete `A` and `B` where extension
applies. `baseStore` links on its own, `cacheOver` is admitted over it,
and the union `[cacheOver, baseStore]` links, so the hypotheses of both
`linkOK_append_of_admittedOver` and `linkOK_append_tail` hold together
and the tail really is recovered unchanged. -/
theorem append_compose_witness :
    LinkOK [baseStore] ∧
      AdmittedOver [baseStore] [cacheOver] ∧
      LinkOK ([cacheOver] ++ [baseStore]) := by
  have hbase : LinkOK [baseStore] := by
    refine LinkOK.cons baseStore [] (by decide) (by decide) (by decide) LinkOK.nil
  have hadm : AdmittedOver [baseStore] [cacheOver] := by
    refine AdmittedOver.cons cacheOver [] (by decide) (by decide) (by decide)
      AdmittedOver.nil
  exact ⟨hbase, hadm, linkOK_append_of_admittedOver hbase hadm⟩

/-- Non-vacuity, refusing side: the same shape with a second `kv`
provider. `AdmittedOver` refuses it over `baseStore`, and the union it
would build does not link because two components provide the same slot.
This is what makes the extension side condition load-bearing rather than
a hypothesis nobody can fail. -/
theorem append_conflict_refused :
    ¬ AdmittedOver [baseStore] [rivalStore] ∧
      ¬ LinkOK ([rivalStore] ++ [baseStore]) := by
  constructor
  · intro h
    cases h with
    | cons _ _ _ hdis _ _ =>
      exact hdis ("kv", sharedRealm) (by decide) (by decide)
  · intro h
    cases h with
    | cons _ _ _ hdis _ _ =>
      exact hdis ("kv", sharedRealm) (by decide) (by decide)

end RevL.Lemmas

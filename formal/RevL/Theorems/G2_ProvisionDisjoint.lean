import RevL.Manifest
import RevL.Lemmas.ManifestLemmas

/-!
# G2 — provision disjointness

DESIGN.md §4: "Provision disjointness in a composition" (paper Def. 43;
enforced by the linker — two providers of one key is a link error). The
formal content: the incremental `LinkOK` judgment admits a composition
only when no key is provided twice, stated here as NoDup over the whole
provision surface. The same judgment yields requirement closure — the
linker's side of G1.
-/

namespace RevL.G2

open RevL.Manifest

/-- G2: the link judgment admits only provision-disjoint compositions. -/
theorem linkOK_provision_disjoint : ∀ comps : List LComponent,
    LinkOK comps → ProvidesDisjoint comps := by
  intro comps h
  induction h with
  | nil => exact List.nodup_nil
  | cons c comps hnd hdis _ _ ihnd =>
    simp only [ProvidesDisjoint, List.flatMap_cons]
    refine List.nodup_append.mpr ⟨hnd, ihnd, ?_⟩
    intro a ha b hb heq
    subst heq
    exact hdis _ ha hb

/-- The link judgment also yields requirement closure: every requirement
is provided within the composition. -/
theorem linkOK_requires_closed : ∀ comps : List LComponent,
    LinkOK comps → RequiresClosed comps := by
  intro comps h
  induction h with
  | nil => intro c hc; cases hc
  | cons c comps _ _ hcl _ ihnd =>
    intro p hp k hk
    cases hp with
    | head =>
      obtain ⟨q, hq, hkq⟩ := RevL.Lemmas.mem_flatMap_provides (hcl k hk)
      exact ⟨q, hq, hkq⟩
    | tail _ h =>
      obtain ⟨q, hq, hkq⟩ := ihnd p h k hk
      exact ⟨q, by simp [hq], hkq⟩

end RevL.G2

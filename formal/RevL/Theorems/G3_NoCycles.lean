import RevL.Manifest

/-!
# G3 — dependency cycles rejected

DESIGN.md §4: "Dependency cycles rejected" (§6.5). The formal content: a
composition that admits a *layering* — ranks strictly decreasing along
every provision→requirement dependency — admits no finite dependency
path from a key to itself. The layering is the certificate the linker's
topological check either finds or refutes.
-/

namespace RevL.G3

open RevL.Manifest

/-- Along a dependency path, ranks strictly decrease. -/
theorem depPath_rank_lt : ∀ {comps : List LComponent} {k₁ k₂ : String},
    ∀ rank : String → Nat, LayeredBy comps rank → DepPath comps k₁ k₂ →
    rank k₂ < rank k₁ := by
  intro comps k₁ k₂ rank hl hpath
  induction hpath with
  | step a hd =>
    obtain ⟨p, hp, hprov, hreq⟩ := hd
    exact hl p hp k₁ hprov a hreq
  | trans a b hrec hdep ih =>
    obtain ⟨p, hp, hprov, hreq⟩ := hdep
    exact Nat.lt_trans (hl p hp a hprov b hreq) ih

/-- G3: a layered composition admits no dependency cycle — the link
judgment's layering certificate cannot coexist with a path from a key
back to itself. -/
theorem no_dependency_cycles : ∀ (comps : List LComponent) (k : String),
    (∃ rank : String → Nat, LayeredBy comps rank) → DepPath comps k k → False := by
  intro comps k ⟨rank, hl⟩ hpath
  have hlt : rank k < rank k := depPath_rank_lt rank hl hpath
  exact absurd hlt (Nat.lt_irrefl (rank k))

end RevL.G3

/-
RevL.Lemmas.ListLemmas — L1 lemma farm (worker-owned, parallel-safe).

Pure utility lemmas over core structures. Farm rules: import nothing from
RevL except L0 (ideally nothing at all), never import a sibling Theorems
file, never import another Lemmas file. If a lemma needs a sibling's
lemma, promote it here or into L0 — do not cross-import.
-/

namespace RevL.Lemmas

theorem length_append {α : Type} : ∀ (as bs : List α),
    (as ++ bs).length = as.length + bs.length := by
  intro as bs
  induction as with
  | nil => simp
  | cons a as ih => simp [ih]; omega

theorem reverse_append {α : Type} : ∀ (as bs : List α),
    (as ++ bs).reverse = bs.reverse ++ as.reverse := by
  intro as bs
  induction as with
  | nil => simp
  | cons a as ih => simp [ih, List.append_assoc]

end RevL.Lemmas

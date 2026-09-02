import RevL.Typing

/-!
# G4 — inverse-or-emit

DESIGN.md §4: "Every mutation carries an inverse or an `emit` marker"
(paper Def. 8, witnessed inverses; enforced by the parser + emission
checks in `src/revl/lower.py`).

The formal content is the shape of `RevL.Typing.Typed`: there is no
constructor admitting a `raw` mutation. This theorem states the shape as
a sentence: every admitted mutation either carries an inverse or is an
explicit boundary crossing.
-/

namespace RevL.G4

open RevL.Typing RevL.Syntax

/-- G4: for every statement, if the checker admits it and it mutates,
then it carries an inverse or it is an explicit emission. -/
theorem inverse_or_emit : ∀ s, Typed s → IsMutation s → HasInverse s ∨ IsEmit s := by
  intro s ht hm
  cases hm with
  | effect m u => exact Or.inl (.effect m u)
  | emit m => exact Or.inr (.emit m)
  | raw _ => cases ht  -- no Typed constructor admits a raw mutation


-- ------------------------------------------------------- non-vacuity

/-- `w.db.insert(row)`. -/
def mutation : Expr := .call "db" [.lit "row"]

/-- `w.db.delete(row)`. -/
def undo : Expr := .call "db" [.lit "undo"]

/-- **Non-vacuity** (roadmap item 418, step 8), and the shape-level
weakness stated in the same breath. The hypotheses `Typed s` and
`IsMutation s` are jointly satisfiable, in BOTH conclusion branches, so
the theorem is not empty; and the `raw` branch is discharged because
`Typed` has no constructor for it, which is the syntactic argument item
418 flagged. The load-bearing statement over the classification lattice
is `RevL.G4Classified.g4_not_vacuous`, where the same refusal is a
computation on a representable term. -/
theorem g4_shape_not_vacuous :
    (Typed (.effect mutation undo) ∧ IsMutation (.effect mutation undo) ∧
      HasInverse (.effect mutation undo)) ∧
    (Typed (.emit mutation) ∧ IsMutation (.emit mutation) ∧
      IsEmit (.emit mutation)) ∧
    (Typed (.pure mutation) ∧ ¬ IsMutation (.pure mutation)) ∧
    (IsMutation (.raw mutation) ∧ ¬ Typed (.raw mutation)) :=
  ⟨⟨.effect _ _, .effect _ _, .effect _ _⟩,
   ⟨.emit _, .emit _, .emit _⟩,
   ⟨.pure _, fun h => by cases h⟩,
   ⟨.raw _, fun h => by cases h⟩⟩

end RevL.G4

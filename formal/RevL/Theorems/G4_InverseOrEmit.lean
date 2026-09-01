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

end RevL.G4

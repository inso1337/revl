import RevL.Typing
import RevL.Lemmas.ReachLemmas

/-!
# G6 — confinement

DESIGN.md §4: "Code outside effect forms is pure (confinement)" (paper
Def. 48); DESIGN.md pillar 3: "There is no global mutable state and no
ambient I/O. Everything a component reaches, it reaches through a
declared capability. 'Undeclared access' is a type error, not a proxy
trap."

The formal content: the ambient-context-free claim is *built into*
`ReachIn`/`TypedIn` — there is no constructor that touches a key outside
the declared context `C`, because the judgment has nowhere to get one
from. This theorem states that shape as a sentence.
-/

namespace RevL.G6

open RevL.Typing RevL.Syntax

/-- G6: whatever the checker admits under requirements `C` reaches only
keys declared in `C`. -/
theorem confinement : ∀ (C : Ctx) (s : Stmt), TypedIn C s →
    ∀ k ∈ stmtHeads s, k ∈ C :=
  fun _ _ ht => RevL.Lemmas.typedIn_confined ht

end RevL.G6

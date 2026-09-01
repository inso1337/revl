import RevL.Syntax

/-
RevL.Semantics — L0 effect log and derived teardown (architect-owned).

The teardown story (DESIGN.md §3.2, G7): every effect that ran during
activation left its inverse on an accumulated log, and teardown replays
the log *reversed* — LIFO over exactly the effects that ran. G7's formal
statement is that nothing is dropped and nothing is added: the replay is
in bijection with the log.
-/

namespace RevL.Semantics

open RevL.Syntax

/-- One entry of the effect log: an effect that ran, carrying the inverse
the syntax forced it to declare. -/
structure WitnessedEffect where
  inverse : Expr

/-- Derived teardown: replay every accumulated inverse, LIFO. -/
def teardown (log : List WitnessedEffect) : List Expr :=
  log.reverse.map (·.inverse)

/-- LIFO-completeness, length form (G7's "over exactly the effects that
ran"): teardown replays one inverse per witnessed effect — nothing
dropped, nothing invented. The full bijection statement is TODO
(formal/STATUS.md). -/
theorem teardown_length : ∀ log : List WitnessedEffect,
    (teardown log).length = log.length := by
  intro log
  simp [teardown]

/-- Teardown of an empty activation is empty (no residue from nothing). -/
theorem teardown_nil : teardown [] = [] := rfl

end RevL.Semantics

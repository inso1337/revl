import RevL.Semantics

/-!
# G7 — derived teardown is LIFO-complete over accumulated effects

DESIGN.md §4: G7 holds "by lowering" (paper Thm. 16). DESIGN.md §3.2:
"everything it does is undone — in derived, LIFO order — when it
deactivates", and the README: teardown is "LIFO over exactly the effects
that ran".

Three statements formalize "exactly": completeness (every witnessed
inverse is replayed), soundness (nothing unwitnessed is replayed), and
the position correspondence (replay position `i` is the `(n-1-i)`-th
witnessed effect — LIFO). Together with `RevL.Semantics.teardown_length`
(one replay per witnessed effect) these pin the replay set.
-/

namespace RevL.G7

open RevL.Semantics RevL.Syntax

/-- Completeness: every witnessed inverse is replayed. -/
theorem teardown_replays_all : ∀ (log : List WitnessedEffect) (w : WitnessedEffect),
    w ∈ log → w.inverse ∈ teardown log := by
  intro log w hw
  simp only [teardown, List.mem_reverse, List.mem_map]
  exact ⟨w, hw, rfl⟩

/-- Soundness: teardown replays nothing that was not witnessed. -/
theorem teardown_only_witnessed : ∀ (log : List WitnessedEffect) (e : Expr),
    e ∈ teardown log → ∃ w ∈ log, w.inverse = e := by
  intro log e he
  simp only [teardown, List.mem_map] at he
  obtain ⟨w, hw, rfl⟩ := he
  exact ⟨w, List.mem_reverse.mp hw, rfl⟩

/-- The LIFO equation: teardown replays the accumulated inverses in
reverse order — the replay set is the log's inverses, reversed. Position
`i` of the replay is the `(n-1-i)`-th witnessed effect (combine with
`List.getElem_reverse`). -/
theorem teardown_eq_reversed_inverses : ∀ log : List WitnessedEffect,
    teardown log = (log.map (·.inverse)).reverse := by
  intro log
  simp [teardown, List.map_reverse]

end RevL.G7

import RevL.Syntax

/-!
# G5 — teardown cannot register effects

DESIGN.md §4: G5 holds "by construction" — "an `undo` body has no way to
register new effects because the grammar gives it nowhere to put one"
(README; enforced by `UndoStmt`'s grammar in the parser).

The formalization is exactly that argument: undo bodies are a *separate*
inductive with no effect-introducing constructor, so the count of new
effect registrations in any undo body is provably zero.
-/

namespace RevL.G5

open RevL.Syntax

/-- A statement inside an `undo` body. Compare `RevL.Syntax.Stmt`: there
is no `effect` constructor here. A sequenced undo body is the derived
teardown of a composite; calls replay inverses, nothing more. -/
inductive UndoStmt where
  | call : Expr → UndoStmt
  | seq : UndoStmt → UndoStmt → UndoStmt

/-- The number of *new* effect registrations an undo body can perform. -/
def registrations : UndoStmt → Nat
  | .call _ => 0
  | .seq a b => registrations a + registrations b

/-- G5: no undo body — hence no derived teardown — registers a new
effect. -/
theorem teardown_registers_nothing : ∀ u : UndoStmt, registrations u = 0 := by
  intro u
  induction u with
  | call _ => rfl
  | seq a b iha ihb =>
    simp only [registrations]
    rw [iha, ihb]

-- ------------------------------------------------------- the finding

/-- An undo body that calls a boundary-crossing name. -/
def sneakyUndo : UndoStmt := .call (.call "db_insert" [.lit "row"])

/-- **This is a finding, not a non-vacuity witness** (roadmap item 418,
C6 and step 8). `registrations` ignores its argument: it is the constant
zero function, so `teardown_registers_nothing` is true of every undo body
including one whose only statement calls an emission. The review's probe
`∀ u v, registrations u = registrations v` succeeds, and here it is,
proved. The theorem above is therefore *contentless* rather than
vacuous: its hypothesis set is empty, and its conclusion holds by
definition rather than by any property of undo bodies.

The load-bearing G5 is `RevL.G5Classified`, whose `registrations` counts
calls whose *reached* classification crosses and whose
`registrations_depends_on_its_argument` refutes exactly this probe. -/
theorem registrations_ignores_its_argument :
    (∀ u v : UndoStmt, registrations u = registrations v) ∧
    registrations sneakyUndo = 0 := by
  refine ⟨fun u v => ?_, rfl⟩
  rw [teardown_registers_nothing u, teardown_registers_nothing v]

end RevL.G5

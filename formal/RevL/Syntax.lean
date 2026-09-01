/-
RevL.Syntax — L0 core syntax (architect-owned; changes gate on the architect).

A deliberately minimal calculus capturing the constructs the guarantee
theorems are about: statements that mutate, statements that pair a mutation
with an inverse, statements marked as boundary crossings, and undo bodies.

Porting notes (DESIGN.md §1, §4): this is the surface level of paper Def. 8
(witnessed inverses) and Def. 48 (confinement). Values are opaque — the
guarantees below are about *effects*, not about evaluation.
-/

namespace RevL.Syntax

/-- A pure expression. Only the shape matters here; the guarantee theorems
are about effect structure, not values. -/
inductive Expr where
  | lit : String → Expr
  | call : String → List Expr → Expr
  deriving Repr, BEq

/-- A statement in a component body (the activation). Four forms, and the
grammar is the point: `effect` *cannot be written* without its inverse
(the two expressions are one constructor), and there is no fifth form that
mutates without an inverse or a marker. -/
inductive Stmt where
  /-- A pure expression; touches nothing (paper Def. 48 confinement). -/
  | pure : Expr → Stmt
  /-- A mutation paired with its inverse — Def. 8's witnessed inverse,
  enforced by syntax rather than by discipline. -/
  | effect : Expr → Expr → Stmt
  /-- A mutation explicitly marked as crossing the system boundary
  (an auditable escape hatch, G8). -/
  | emit : Expr → Stmt
  /-- A mutation with *neither* an inverse nor a marker — the shape G4
  exists to forbid. -/
  | raw : Expr → Stmt
  deriving Repr, BEq

end RevL.Syntax

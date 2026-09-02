import RevL.Boundary

/-!
# G8 — the boundary surface is enumerable

DESIGN.md §4: "Boundary surface (externs, emissions) is enumerable"
(§6.1; surfaced by `revl audit`). The formal content, both directions:
every emission of a body appears on the audited surface, and — for a
typed body — everything on the surface is an emission (a `raw` leak
would surface here too, but G4 makes it untypable, so the audit of typed
code is exactly the emissions).
-/

namespace RevL.G8

open RevL.Boundary RevL.Typing RevL.Syntax

/-- Completeness: every emission's crossing appears on the audited
boundary surface. -/
theorem boundary_enumerates_emissions : ∀ (body : List Stmt) (s : Stmt),
    s ∈ body → IsEmit s → ∀ k ∈ stmtHeads s, k ∈ bodyBoundary body := by
  intro body s hs he k hk
  cases he with
  | emit m =>
    refine List.mem_flatMap.mpr ⟨Stmt.emit m, hs, ?_⟩
    exact hk

/-- Soundness: on a typed body, the boundary surface contains only
emissions' crossings — the audit enumerates exactly the declared
boundary. -/
theorem boundary_only_declared : ∀ (body : List Stmt) (k : String),
    (∀ s ∈ body, Typed s) → k ∈ bodyBoundary body →
    ∃ s ∈ body, IsEmit s ∧ k ∈ stmtHeads s := by
  intro body k hty hk
  obtain ⟨s, hs, hk'⟩ := List.mem_flatMap.mp hk
  refine ⟨s, hs, ?_, ?_⟩
  · cases s with
    | pure e => simp [boundaryOf] at hk'
    | effect m u => simp [boundaryOf] at hk'
    | emit m => exact IsEmit.emit m
    | raw m =>
      have hraw : Typed (.raw m) := hty _ hs
      cases hraw
  · cases s with
    | pure e => simp [boundaryOf] at hk'
    | effect m u => simp [boundaryOf] at hk'
    | emit m => exact hk'
    | raw m =>
      have hraw : Typed (.raw m) := hty _ hs
      cases hraw


-- ------------------------------------------------------- non-vacuity

/-- A declared crossing. -/
def emitStmt : Stmt := .emit (.call "db_insert" [.lit "row"])

/-- The SAME crossing inside an `effect`, which the marker-level model
scores as touching nothing. -/
def effectStmt : Stmt :=
  .effect (.call "db_insert" [.lit "row"]) (.call "db_delete" [.lit "row"])

/-- A typed body carrying both. -/
def auditedBody : List Stmt := [effectStmt, emitStmt]

/-- **Non-vacuity** (roadmap item 418, step 8), and the marker-level
weakness in the same statement. The hypotheses of
`boundary_only_declared` are satisfiable at a body with a NON-EMPTY
surface, so neither direction is a claim about the empty list; and the
last conjunct is the definitional escape the review named: `boundaryOf`
decides per constructor, so an `effect` carrying the very same crossing
contributes nothing to the audit. The load-bearing G8 is
`RevL.G8Classified`, whose surface is the reach fold applied uniformly to
all four statement forms. -/
theorem g8_marker_level_not_vacuous :
    (∀ s ∈ auditedBody, Typed s) ∧
    bodyBoundary auditedBody = ["db_insert"] ∧
    IsEmit emitStmt ∧
    boundaryOf effectStmt = [] := by
  refine ⟨?_, by simp [bodyBoundary, auditedBody, boundaryOf, effectStmt, emitStmt, heads],
    .emit _, by simp [boundaryOf, effectStmt]⟩
  intro s hs
  simp only [auditedBody, List.mem_cons, List.not_mem_nil, or_false] at hs
  rcases hs with rfl | rfl
  · exact Typed.effect _ _
  · exact Typed.emit _

end RevL.G8

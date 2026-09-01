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

end RevL.G8

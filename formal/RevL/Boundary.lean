import RevL.Typing

/-
RevL.Boundary — L0 boundary-surface model (architect-owned).

G8: "Boundary surface (externs, emissions) is enumerable" (§6.1). The
model: every statement contributes zero or more call heads to the
auditable boundary surface; `revl audit`'s formal counterpart is the
concatenation over a body. Internal effects (`effect`, reverted by their
witnessed inverse) never cross; emissions declare their crossing; an
unmarked `raw` mutation would appear here too — which is exactly why G4
makes it untypable.
-/

namespace RevL.Boundary

open RevL.Typing RevL.Syntax

/-- The boundary surface of one statement: call heads that cross the
system boundary. -/
def boundaryOf : Stmt → List String
  | .pure _ => []
  | .effect _ _ => []
  | .emit m => heads m
  | .raw m => heads m

/-- The enumerable boundary surface of a body (G8). -/
def bodyBoundary (body : List Stmt) : List String :=
  body.flatMap boundaryOf

end RevL.Boundary

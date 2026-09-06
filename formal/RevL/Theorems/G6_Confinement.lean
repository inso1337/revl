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


-- ------------------------------------------------------- non-vacuity

/-- A manifest declaring one key. -/
def declared : Ctx := ["db"]

/-- A statement that reaches only the declared key. -/
def okStmt : Stmt := .effect (.call "db" [.lit "row"]) (.call "db" [.lit "undo"])

/-- The same shape reaching an undeclared one. -/
def leakStmt : Stmt := .effect (.call "net" [.lit "row"]) (.call "db" [.lit "undo"])

/-- **Non-vacuity** (roadmap item 418, step 8). `TypedIn` admits a
statement with a NON-EMPTY reach surface, so `confinement`'s hypothesis
is satisfiable and its conclusion is not about an empty surface; and the
judgment is not universal, because the statement reaching an undeclared
key is refused. -/
theorem g6_not_vacuous :
    TypedIn declared okStmt ∧ stmtHeads okStmt ≠ [] ∧
    (∀ k ∈ stmtHeads okStmt, k ∈ declared) ∧
    ¬ TypedIn declared leakStmt := by
  have hok : TypedIn declared okStmt :=
    TypedIn.effect _ _ _
      (ReachIn.call _ "db" _ (by decide) (by
        intro a ha
        simp only [List.mem_cons, List.not_mem_nil, or_false] at ha
        rcases ha with rfl
        exact ReachIn.lit _ _))
      (ReachIn.call _ "db" _ (by decide) (by
        intro a ha
        simp only [List.mem_cons, List.not_mem_nil, or_false] at ha
        rcases ha with rfl
        exact ReachIn.lit _ _))
  refine ⟨hok, by simp [okStmt, stmtHeads, heads], confinement _ _ hok, ?_⟩
  intro h
  have hbad := confinement _ _ h "net" (by simp [leakStmt, stmtHeads, heads])
  simp [declared] at hbad

/-- **The confinement ROW is load-bearing** (issue 276, G6 oracle row). The
oracle's `C` row prints `∀ k ∈ stmtHeads s, k ∈ C` (`RevLOracle.confinedB_iff`);
this guards it against being a foregone `ok`. `leakStmt` reaches the undeclared
key `net`, so the confinement check REFUSES it under `declared` — and the
shipped-side perturbation the row exists to catch, extending the declared
context to ACCEPT that very leaking head, flips the verdict to accept. The
predicate genuinely turns on the leaking head; a vacuous row would be blind to
the difference, so this exhibits exactly the divergence the differential would
report if the reference drifted too permissive. -/
theorem g6_row_not_vacuous :
    ¬ (∀ k ∈ stmtHeads leakStmt, k ∈ declared)
    ∧ (∀ k ∈ stmtHeads leakStmt, k ∈ "net" :: declared) := by
  refine ⟨?_, ?_⟩
  · intro h
    have := h "net" (by simp [leakStmt, stmtHeads, heads])
    simp [declared] at this
  · intro k hk
    simp only [leakStmt, stmtHeads, heads, List.flatMap_cons, List.flatMap_nil,
      List.append_nil, List.mem_append, List.mem_cons, List.not_mem_nil,
      or_false] at hk
    rcases hk with rfl | rfl <;> simp [declared]

end RevL.G6

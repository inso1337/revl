import RevL.Typing
import RevL.Lemmas.ReachLemmas

/-!
# G1 — declared-only access

DESIGN.md §4: "Every requirement is declared; undeclared access cannot
be written" (paper Def. 25, declared-only access; enforced by the
checker's service/capability resolution).

Component-level statement: a component whose every body statement the
checker admits under *exactly its declared requirements* cannot reach a
key it never declared. The judgment-level content is G6; this composes
it over a component body.
-/

namespace RevL.G1

open RevL.Typing RevL.Syntax

/-- A component: its declared requirements plus its activation body. -/
structure Component where
  requires : Ctx
  body : List Stmt

/-- A component is confined when its body touches nothing beyond its
declared requirements. -/
def confined (comp : Component) : Prop :=
  ∀ s ∈ comp.body, ∀ k ∈ stmtHeads s, k ∈ comp.requires

/-- G1: admission under exactly the declared requirements implies the
component is confined — undeclared access cannot have been written. -/
theorem declared_only_access : ∀ comp : Component,
    (∀ s ∈ comp.body, TypedIn comp.requires s) → confined comp := by
  intro comp h
  intro s hs k hk
  exact RevL.Lemmas.typedIn_confined (h s hs) k hk


-- ------------------------------------------------------- non-vacuity

/-- A component declaring `db` and `log`, whose activation reads through
both and through nothing else. -/
def wiredComponent : Component :=
  { requires := ["db", "log"],
    body := [.pure (.call "log" [.lit "start"]),
             .effect (.call "db" [.lit "row"]) (.call "db" [.lit "undo"])] }

/-- The same body under a manifest that never declared `db`. -/
def underdeclared : Component :=
  { requires := ["log"], body := wiredComponent.body }

/-- **Non-vacuity** (roadmap item 418, step 8). The admission hypothesis
of `declared_only_access` is satisfiable at a component with a NON-EMPTY
body that really reaches its declared keys, and the conclusion is not
universal: drop `db` from the manifest and the same body is neither
admitted nor confined. -/
theorem g1_not_vacuous :
    (∀ s ∈ wiredComponent.body, TypedIn wiredComponent.requires s) ∧
    confined wiredComponent ∧
    wiredComponent.body ≠ [] ∧
    ¬ confined underdeclared ∧
    ¬ (∀ s ∈ underdeclared.body, TypedIn underdeclared.requires s) := by
  have hty : ∀ s ∈ wiredComponent.body, TypedIn wiredComponent.requires s := by
    intro s hs
    simp only [wiredComponent, List.mem_cons, List.not_mem_nil, or_false] at hs
    rcases hs with rfl | rfl
    · exact TypedIn.pure _ _
        (ReachIn.call _ "log" _ (by decide) (by
          intro a ha
          simp only [List.mem_cons, List.not_mem_nil, or_false] at ha
          rcases ha with rfl
          exact ReachIn.lit _ _))
    · exact TypedIn.effect _ _ _
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
  have hnc : ¬ confined underdeclared := by
    intro h
    have hmem : Stmt.effect (.call "db" [.lit "row"]) (.call "db" [.lit "undo"])
        ∈ underdeclared.body := by simp [underdeclared, wiredComponent]
    have hbad := h _ hmem "db" (by simp [stmtHeads, heads])
    simp [underdeclared] at hbad
  exact ⟨hty, declared_only_access _ hty, by simp [wiredComponent], hnc,
    fun h => hnc (declared_only_access _ h)⟩

end RevL.G1

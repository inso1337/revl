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

end RevL.G1

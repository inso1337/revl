/-
RevL.Manifest — L0 linker manifest model (architect-owned).

G2 (Def. 43) and G3 (§6.5) are properties of a *composition*, not of a
single component: which wiring keys are provided twice, and whether the
dependency relation over keys has a cycle. The model here is the shape
`revl link` checks: components with requirement/provision key sets.

Porting notes (DESIGN.md §4): G2 → paper Def. 43 (provision
disjointness); G3 → §6.5 (acyclic provisioning).
-/

namespace RevL.Manifest

/-- A component as the linker sees it: a name, the wiring keys it
requires, and the wiring keys it provides. Names are opaque — only list
membership matters. -/
structure LComponent where
  name : String
  requires : List String
  provides : List String

/-- Key-level dependency: `k₁` depends on `k₂` when some component
provides `k₁` and requires `k₂` — the provider of `k₁` cannot activate
before `k₂` exists. -/
def DependsOn (comps : List LComponent) (k₁ k₂ : String) : Prop :=
  ∃ p ∈ comps, k₁ ∈ p.provides ∧ k₂ ∈ p.requires

/-- A finite dependency path: `k₁` transitively depends on `k₂`, at
least one edge. -/
inductive DepPath (comps : List LComponent) : String → String → Prop where
  | step : ∀ k₁ k₂, DependsOn comps k₁ k₂ → DepPath comps k₁ k₂
  | trans : ∀ k₁ k₂ k₃, DepPath comps k₁ k₂ → DependsOn comps k₂ k₃ →
      DepPath comps k₁ k₃

/-- Provision disjointness (G2, Def. 43): no wiring key is provided by
two components. -/
def ProvidesDisjoint (comps : List LComponent) : Prop :=
  List.Nodup (comps.flatMap (·.provides))

/-- Requirement closure: every requirement of every component is
provided somewhere in the composition. -/
def RequiresClosed (comps : List LComponent) : Prop :=
  ∀ c ∈ comps, ∀ k ∈ c.requires, ∃ p ∈ comps, k ∈ p.provides

/-- A layering (§6.5's acyclicity condition in checkable form): ranks
strictly decrease along every provision→requirement dependency. -/
def LayeredBy (comps : List LComponent) (rank : String → Nat) : Prop :=
  ∀ p ∈ comps, ∀ k' ∈ p.provides, ∀ k ∈ p.requires, rank k < rank k'

/-- The link judgment, in the incremental form the linker implements:
components are admitted one at a time, each not double-providing a key
already provided, with its own provisions distinct, and every requirement
provided within the composition so far (itself included). -/
inductive LinkOK : List LComponent → Prop where
  | nil : LinkOK []
  | cons : ∀ c comps,
      List.Nodup c.provides →
      (∀ k ∈ c.provides, k ∉ comps.flatMap (·.provides)) →
      (∀ k ∈ c.requires, k ∈ (c :: comps).flatMap (·.provides)) →
      LinkOK comps →
      LinkOK (c :: comps)

end RevL.Manifest

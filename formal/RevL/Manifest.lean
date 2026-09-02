/-
RevL.Manifest — L0 linker manifest model (architect-owned).

G2 (Def. 43) and G3 (§6.5) are properties of a *composition*, not of a
single component: which wiring slots are provided twice, and whether the
dependency relation over those slots has a cycle. The model here is the
shape `revl link` checks: components with requirement/provision key sets,
each key placed in a realm.

The unit of both guarantees is the `(key, realm)` **slot**, not the bare
key. That is what revl enforces: `diagnostics.GUARANTEES["G2"]` reads
"provision disjointness: one provider per key (per realm)", and the
linker keys its `provider_of` table on `(key, realm)`, taking the realm
from the component's own `isolate` clauses (`lower._realm`, defaulting to
the shared realm). Two tenants providing `kv` in realms
`tenant_a`/`tenant_b` therefore link — `examples/tenants.rvl` is exactly
that program — while two providers of `kv` in one realm are a G2 error.
The same table resolves requirements, so realm separation also breaks
what would otherwise be a dependency cycle (`lower.py`, "edges: provider
-> consumer where the consumer's realm for a key matches the provider's").

Porting notes (DESIGN.md §4): G2 → paper Def. 43 (provision
disjointness within a realm); G3 → §6.5 (acyclic provisioning).
-/

namespace RevL.Manifest

/-- The realm a key with no `isolate` clause lives in. `lower._realm`
spells the same default (`SHARED_REALM`). -/
def sharedRealm : String := ""

/-- A wiring **slot**: the `(key, realm)` pair the linker's `provider_of`
table is indexed by. G2 is one provider per slot; G3 is acyclicity of the
dependency relation over slots. -/
abbrev Slot := String × String

/-- A component as the linker sees it: a name, the wiring keys it
requires, the wiring keys it provides, and the realm it places a key in.
Names are opaque — only list membership matters. `realm` models the
component's `isolate k in realm(r)` clauses; a key the component does not
isolate stays in `sharedRealm`, which is also the field's default, so a
realm-free component is still written `⟨n, reqs, provs⟩`. -/
structure LComponent where
  name : String
  requires : List String
  provides : List String
  realm : String → String := fun _ => sharedRealm

/-- The slots a component fills on the provision surface. -/
def slots (c : LComponent) : List Slot :=
  c.provides.map (fun k => (k, c.realm k))

/-- The slots a component consumes. A requirement resolves in the realm
*the consumer* places the key in — the linker looks up
`provider_of[(key, _realm(entry, key))]`. -/
def needs (c : LComponent) : List Slot :=
  c.requires.map (fun k => (k, c.realm k))

/-- Slot-level dependency: `s₁` depends on `s₂` when some component
provides `s₁` and requires `s₂` — the provider of `s₁` cannot activate
before `s₂` exists. -/
def DependsOn (comps : List LComponent) (s₁ s₂ : Slot) : Prop :=
  ∃ p ∈ comps, s₁ ∈ slots p ∧ s₂ ∈ needs p

/-- A finite dependency path: `s₁` transitively depends on `s₂`, at
least one edge. -/
inductive DepPath (comps : List LComponent) : Slot → Slot → Prop where
  | step : ∀ s₁ s₂, DependsOn comps s₁ s₂ → DepPath comps s₁ s₂
  | trans : ∀ s₁ s₂ s₃, DepPath comps s₁ s₂ → DependsOn comps s₂ s₃ →
      DepPath comps s₁ s₃

/-- Provision disjointness (G2, Def. 43): no wiring slot is provided by
two components. The same key in two *different* realms is two slots, so
it is admitted — that is the multi-tenancy feature, not a conflict. -/
def ProvidesDisjoint (comps : List LComponent) : Prop :=
  List.Nodup (comps.flatMap slots)

/-- Requirement closure: every slot every component consumes is provided
somewhere in the composition. -/
def RequiresClosed (comps : List LComponent) : Prop :=
  ∀ c ∈ comps, ∀ s ∈ needs c, ∃ p ∈ comps, s ∈ slots p

/-- A layering (§6.5's acyclicity condition in checkable form): ranks
strictly decrease along every provision→requirement dependency. -/
def LayeredBy (comps : List LComponent) (rank : Slot → Nat) : Prop :=
  ∀ p ∈ comps, ∀ s' ∈ slots p, ∀ s ∈ needs p, rank s < rank s'

/-- The link judgment, in the incremental form the linker implements:
components are admitted one at a time, each not double-providing a slot
already provided, with its own slots distinct, and every slot it consumes
provided by a component admitted **strictly before** it.

That last clause is the G3 side of the judgment, and it is why the list
is not a set: `comps` is the composition in reverse `loadOrder` — the
head is admitted last and may only depend on the tail. A component
cannot satisfy its own requirement, which is the refusal the linker
reports as "component N requires a key it provides itself (`k`) (G3)";
and, transitively, no cycle can be presented at all, which is the general
G3 refusal the linker's DFS reports. A program links iff *some* ordering
of its components derives `LinkOK` — the linker's Kahn pass is the search
for that ordering. -/
inductive LinkOK : List LComponent → Prop where
  | nil : LinkOK []
  | cons : ∀ c comps,
      List.Nodup (slots c) →
      (∀ s ∈ slots c, s ∉ comps.flatMap slots) →
      (∀ s ∈ needs c, s ∈ comps.flatMap slots) →
      LinkOK comps →
      LinkOK (c :: comps)

/-- The rank an admission order induces on slots: a slot provided by the
head outranks every slot the tail provides, and an unprovided slot sits
at the bottom. This is the layering certificate the linker's topological
pass computes; `RevL.G3.linkOK_layered` proves it is one. -/
def rankOf : List LComponent → Slot → Nat
  | [], _ => 0
  | c :: cs, s => if s ∈ slots c then cs.length + 1 else rankOf cs s

end RevL.Manifest

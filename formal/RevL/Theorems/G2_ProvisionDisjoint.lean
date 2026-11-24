import RevL.Manifest
import RevL.Lemmas.ManifestLemmas

/-!
# G2 — provision disjointness

DESIGN.md §4: "Provision disjointness in a composition" (paper Def. 43;
enforced by the linker — two providers of one key *in one realm* is a
link error). The formal content: the incremental `LinkOK` judgment admits
a composition only when no `(key, realm)` slot is provided twice, stated
here as NoDup over the whole provision surface. The same judgment yields
requirement closure — the linker's side of G1.

The unit is the slot, not the bare key, because that is the rule revl
enforces (`diagnostics.GUARANTEES["G2"]`: "one provider per key (per
realm)"; the linker's `provider_of` table is keyed on `(key, realm)`).
`realm_separation_admitted` and `same_realm_conflict_refused` below pin
both sides of that distinction to concrete compositions, so the statement
cannot be read as vacuous in either direction.
-/

namespace RevL.G2

open RevL.Manifest

/-- G2: the link judgment admits only provision-disjoint compositions. -/
theorem linkOK_provision_disjoint : ∀ comps : List LComponent,
    LinkOK comps → ProvidesDisjoint comps := by
  intro comps h
  induction h with
  | nil => exact List.nodup_nil
  | cons c comps hnd hdis _ _ ihnd =>
    simp only [ProvidesDisjoint, List.flatMap_cons]
    refine List.nodup_append.mpr ⟨hnd, ihnd, ?_⟩
    intro a ha b hb heq
    subst heq
    exact hdis _ ha hb

/-- The link judgment also yields requirement closure: every slot a
component consumes is provided within the composition. -/
theorem linkOK_requires_closed : ∀ comps : List LComponent,
    LinkOK comps → RequiresClosed comps := by
  intro comps h
  induction h with
  | nil => intro c hc; cases hc
  | cons c comps _ _ hcl _ ihnd =>
    intro p hp s hs
    cases hp with
    | head =>
      obtain ⟨q, hq, hsq⟩ := RevL.Lemmas.mem_flatMap_slots (hcl s hs)
      exact ⟨q, by simp [hq], hsq⟩
    | tail _ h =>
      obtain ⟨q, hq, hsq⟩ := ihnd p h s hs
      exact ⟨q, by simp [hq], hsq⟩

-- ------------------------------------------------------- non-vacuity

/-- `examples/tenants.rvl`: two tenant stores, both providing `kv`, each
isolated into its own realm. -/
def tenantAStore : LComponent :=
  { name := "TenantAStore", requires := [], provides := ["kv"],
    realm := fun _ => "tenant_a" }

def tenantBStore : LComponent :=
  { name := "TenantBStore", requires := [], provides := ["kv"],
    realm := fun _ => "tenant_b" }

/-- The same shape without the `isolate` clauses: two providers of `kv`
in the shared realm, which is the G2 provision conflict the linker
reports. -/
def sharedStoreA : LComponent :=
  { name := "StoreA", requires := [], provides := ["kv"] }

def sharedStoreB : LComponent :=
  { name := "StoreB", requires := [], provides := ["kv"] }

/-- Non-vacuity, admitting side: realm separation is real. Two components
providing the *same key* in *different realms* are provision-disjoint and
link — the `examples/tenants.rvl` composition the real checker accepts.
A key-indexed `ProvidesDisjoint` would refuse this. -/
theorem realm_separation_admitted :
    LinkOK [tenantAStore, tenantBStore] ∧
      ProvidesDisjoint [tenantAStore, tenantBStore] := by
  have hlink : LinkOK [tenantAStore, tenantBStore] := by
    refine LinkOK.cons _ _ (by decide) (by decide) (by decide) ?_
    exact LinkOK.cons _ _ (by decide) (by decide) (by decide) LinkOK.nil
  exact ⟨hlink, linkOK_provision_disjoint _ hlink⟩

/-- Non-vacuity, refusing side: G2 still bites. Drop the realms and the
same two components are *not* provision-disjoint, so by
`linkOK_provision_disjoint` they cannot link. The realm refinement
weakened nothing. -/
theorem same_realm_conflict_refused :
    ¬ ProvidesDisjoint [sharedStoreA, sharedStoreB] ∧
      ¬ LinkOK [sharedStoreA, sharedStoreB] := by
  have hnd : ¬ ProvidesDisjoint [sharedStoreA, sharedStoreB] := by
    intro h
    simp [ProvidesDisjoint, slots, sharedStoreA, sharedStoreB] at h
  exact ⟨hnd, fun h => hnd (linkOK_provision_disjoint _ h)⟩


-- ------------------------------------------------- G2 over the IR image

/-! ### G2 over the IR image (issue #2739)

The theorems above are stated over `RevL.Manifest.LComponent`, the linker's
shape. The backends do not consume that shape: they consume the lowered IR
document. Its `components` carry `provides` (key -> service,
`docs/backend-ir.md`) and a SPARSE `isolate` map (key -> realm, the shared
realm being the ABSENCE of an entry, `docs/backend-ir-v3.md`), and its
`manifest.templates` names the spawn targets, which are runtime instances and
not static composition members (`docs/design-v2-instances.md`, `lower._link`).
Nothing related the two shapes, so "if the checker admits it, the lowered IR
keeps G2" was corpus-measured by the `V` row and never stated over the
document the backends read.

An `IRDoc` below is the part of a lowered document the rule reads. `members`
is its static composition, the components its own `templates` list does not
name, `irSlots` are the `(key, realm)` slots a component fills, and
`IRDisjoint` is G2 restated over the members' slots.

`source_linkOK_ir_disjoint` is the statement that relates the two shapes: the
source composition passes the link judgment, and the document carries exactly
the source's slots (`hk`, a permutation), so the document is disjoint. The
hypothesis `hk` is what the `IRS` oracle row decides on the corpus
(`RevLOracle.irKeptB_iff`); it is not assumed to hold of lowering, only
measured. A theorem that took `LinkOK` of a model built FROM the document
would be a restatement, because a model with no requirements has `LinkOK`
exactly when it is provision-disjoint.

Witnesses keep it from being vacuous. `tenants_ir_kept` and
`tenants_ir_disjoint` are the admitted multi-realm composition's IR form.
`isolate_entry_is_load_bearing` drops both `isolate` entries (the lowering
defect this row exists to catch) and the document collides.
`lost_realm_is_caught_by_kept` drops one entry: the document is still
disjoint, which is why the `IRS` row exists beside the `IRD` row.
`template_exclusion_is_load_bearing` is `examples/tenant_attenuation.rvl`'s
shape: two spawn templates provide one key in one realm, the whole document
collides, and the static composition does not.
-/

/-- One component of the lowered IR document, restricted to the fields G2
reads: `provides` (the keys; the service each maps to is not G2's) and the
sparse `isolate` key -> realm map. -/
structure IRComponent where
  name : String
  provides : List String
  isolate : List (String × String) := []

/-- A lowered IR document as G2 reads it: its components and the names its own
`manifest.templates` lists. -/
structure IRDoc where
  components : List IRComponent
  templates : List String := []

/-- The realm an IR component places `key` in: its `isolate` entry, else the
shared realm. `lower._realm` reads the same map with the same default. -/
def realmOf (c : IRComponent) (key : String) : String :=
  match c.isolate.find? (fun e => e.1 = key) with
  | some e => e.2
  | none => sharedRealm

/-- The `(key, realm)` slots an IR component fills. -/
def irSlots (c : IRComponent) : List Slot :=
  c.provides.map (fun k => (k, realmOf c k))

/-- The document's static composition: the components its own `templates` list
does not name. A spawn target is instantiated at runtime into a fresh local
realm, so lowering lists it in `components` for the backends and in
`manifest.templates` for the linker, and never composes it statically. -/
def IRDoc.members (d : IRDoc) : List IRComponent :=
  d.components.filter (fun c => !d.templates.contains c.name)

/-- Every slot the document's static composition fills. -/
def IRDoc.slots (d : IRDoc) : List Slot :=
  d.members.flatMap irSlots

/-- G2 over the IR document: no wiring slot is provided twice by its static
composition. The unit is the slot, not the key, for the reason
`RevL.Manifest` gives: two tenants providing one key in different realms is
the multi-tenancy feature. -/
def IRDisjoint (d : IRDoc) : Prop :=
  List.Nodup d.slots

instance instDecIRDisjoint (d : IRDoc) : Decidable (IRDisjoint d) := by
  unfold IRDisjoint; infer_instance

/-- **G2 holds of the IR image.** If the source composition passes the link
judgment and the lowered document carries exactly its slots, the document's
static composition provides no slot twice. -/
theorem source_linkOK_ir_disjoint (src : List LComponent) (d : IRDoc)
    (hl : LinkOK src) (hk : d.slots.Perm (src.flatMap slots)) :
    IRDisjoint d := by
  have hd : ProvidesDisjoint src := linkOK_provision_disjoint _ hl
  exact hk.nodup_iff.mpr hd

/-- Given that the document keeps the source's slots, the document is disjoint
exactly when the source is. This is why the `IRD` row must agree with the `V`
row's `disjoint` column wherever `IRS` says `ok`. -/
theorem ir_disjoint_iff_source_disjoint (src : List LComponent) (d : IRDoc)
    (hk : d.slots.Perm (src.flatMap slots)) :
    IRDisjoint d ↔ ProvidesDisjoint src :=
  hk.nodup_iff

-- --------------------------------------------------------- the witnesses

/-- `examples/tenants.rvl`'s two tenant stores, as the IR document carries
them: one key, two realms, each in its own `isolate` entry. -/
def irTenantAStore : IRComponent :=
  { name := "TenantAStore", provides := ["kv"], isolate := [("kv", "tenant_a")] }

def irTenantBStore : IRComponent :=
  { name := "TenantBStore", provides := ["kv"], isolate := [("kv", "tenant_b")] }

def irTenants : IRDoc := { components := [irTenantAStore, irTenantBStore] }

/-- The same document after a lowering that lost BOTH `isolate` entries: each
key falls back to the shared realm, which is what `realmOf` does with an
absent entry. -/
def irTenantsNoRealms : IRDoc :=
  { components := [{ name := "TenantAStore", provides := ["kv"] },
                   { name := "TenantBStore", provides := ["kv"] }] }

/-- The same document after a lowering that lost ONE `isolate` entry. -/
def irTenantsOneLost : IRDoc :=
  { components := [irTenantAStore, { name := "TenantBStore", provides := ["kv"] }] }

/-- `examples/tenant_attenuation.rvl`'s shape: two workers spawned as
templates, each providing `worker` in the shared realm, beside the router that
spawns them. -/
def irSpawn : IRDoc :=
  { components := [{ name := "TenantAWorker", provides := ["worker"] },
                   { name := "TenantBWorker", provides := ["worker"] },
                   { name := "Router", provides := [] }],
    templates := ["TenantAWorker", "TenantBWorker"] }

/-- Non-vacuity, the hypothesis side: the tenants document keeps the tenants
composition's slots, so `source_linkOK_ir_disjoint` has a real instance
(`realm_separation_admitted` supplies `LinkOK`). -/
theorem tenants_ir_kept :
    irTenants.slots.Perm ([tenantAStore, tenantBStore].flatMap slots) := by
  decide

/-- The theorem's conclusion on that instance, through the theorem. -/
theorem tenants_ir_disjoint : IRDisjoint irTenants :=
  source_linkOK_ir_disjoint _ _ realm_separation_admitted.1 tenants_ir_kept

/-- Mutation witness: dropping both `isolate` entries makes the document
collide, so the `IRD` row's verdict flips on exactly the field a lowering
defect would lose. -/
theorem isolate_entry_is_load_bearing :
    IRDisjoint irTenants ∧ ¬ IRDisjoint irTenantsNoRealms := by
  refine ⟨?_, ?_⟩ <;> decide

/-- Dropping ONE entry leaves the document disjoint and moves a slot, so it is
the `IRS` row, not the `IRD` row, that catches a single lost realm. -/
theorem lost_realm_is_caught_by_kept :
    IRDisjoint irTenantsOneLost ∧
      ¬ irTenantsOneLost.slots.Perm ([tenantAStore, tenantBStore].flatMap slots) := by
  refine ⟨?_, ?_⟩ <;> decide

/-- The document's own `templates` list is what keeps spawn targets out of the
static composition: the whole component list collides on `worker`, and the
members do not. -/
theorem template_exclusion_is_load_bearing :
    IRDisjoint irSpawn ∧ ¬ List.Nodup (irSpawn.components.flatMap irSlots) := by
  refine ⟨?_, ?_⟩ <;> decide

end RevL.G2

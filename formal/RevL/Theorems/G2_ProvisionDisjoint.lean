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

The theorem above is stated over `RevL.Manifest.LComponent`, the linker's
shape. The backends do not consume that shape: they consume the lowered IR
document (`docs/backend-ir.md`), where each component carries `provides`
(key -> service) and a SPARSE `isolate` map (key -> realm; the shared realm is
the ABSENCE of an entry). Nothing related the two, so "if the checker admits
it, the lowered IR keeps G2" was corpus-measured by the `V` row and never
stated over the document the backends read.

An `IRComponent` below is the part of an IR component entry the rule reads;
`realmOf` is `lower._realm`'s lookup, `irSlots` are the slots the IR carries,
and `IRDisjoint` is G2 restated over them. `linkOK_ir_disjoint` proves that the
admission judgment the model already has yields provision disjointness of the
IR image, so the statement is about the same two fields the linker and the
backends read rather than about an abstract realm function beside them.

Two witnesses keep it from being vacuous. `tenants_link_ir` exhibits the
admitted multi-realm composition the theorem needs as its hypothesis, in IR
form. `isolate_entry_is_load_bearing` drops both `isolate` entries (the
lowering defect this row exists to catch) and shows the document is then
refused by G2, so the oracle's `IRD`/`IRS` rows are sensitive to exactly the
field a lowering mutation would lose.
-/

/-- One component of the lowered IR document, restricted to the fields G2
reads (`docs/backend-ir.md`, "components": `provides` is key -> service and
`isolate` is a sparse key -> realm map). -/
structure IRComponent where
  name : String
  provides : List String
  isolate : List (String × String) := []

/-- The realm an IR component places `key` in: its `isolate` entry, else the
shared realm. `lower._realm` reads the same map with the same default. -/
def realmOf (c : IRComponent) (key : String) : String :=
  match c.isolate.find? (fun e => e.1 = key) with
  | some e => e.2
  | none => sharedRealm

/-- The `(key, realm)` slots an IR component fills. -/
def irSlots (c : IRComponent) : List Slot :=
  c.provides.map (fun k => (k, realmOf c k))

/-- G2 over the IR document: no wiring slot is provided twice. The unit is the
slot, not the key, for the reason `RevL.Manifest` gives: two tenants providing
one key in different realms is the multi-tenancy feature. -/
def IRDisjoint (cs : List IRComponent) : Prop :=
  List.Nodup (cs.flatMap irSlots)

instance instDecIRDisjoint (cs : List IRComponent) : Decidable (IRDisjoint cs) := by
  unfold IRDisjoint; infer_instance

/-- The model an IR component denotes: the same name and provided keys, with
the realm function read off the `isolate` map. Defined FROM the IR, so the
theorem below relates the document to its own model. -/
def ofIR (c : IRComponent) : LComponent :=
  { name := c.name, requires := [], provides := c.provides, realm := realmOf c }

/-- The model an IR component denotes carries exactly its IR slots; `rfl`,
because `ofIR` and `irSlots` read the same `provides` and `realmOf`. It lets
the theorem below be a rewrite of `ProvidesDisjoint` rather than a second
calculation of the slots. -/
@[simp] theorem slots_ofIR (c : IRComponent) : slots (ofIR c) = irSlots c := rfl

/-- **G2 holds of the IR image.** If the linker admits the composition that the
IR components denote, no `(key, realm)` slot appears twice in the document the
backends consume. -/
theorem linkOK_ir_disjoint : ∀ cs : List IRComponent,
    LinkOK (cs.map ofIR) → IRDisjoint cs := by
  intro cs h
  have hd : ProvidesDisjoint (cs.map ofIR) := linkOK_provision_disjoint _ h
  simpa [ProvidesDisjoint, IRDisjoint, List.flatMap_map] using hd

-- --------------------------------------------------------- the witnesses

/-- `examples/tenants.rvl`'s two tenant stores, as the IR document carries
them: one key, two realms, each in its own `isolate` entry. -/
def irTenantAStore : IRComponent :=
  { name := "TenantAStore", provides := ["kv"], isolate := [("kv", "tenant_a")] }

def irTenantBStore : IRComponent :=
  { name := "TenantBStore", provides := ["kv"], isolate := [("kv", "tenant_b")] }

/-- The same provider after a lowering that lost its `isolate` entry: the key
falls back to the shared realm, which is what `realmOf` does with the absent
entry. Both stores lose theirs, as they would in one lowering pass. -/
def irTenantAStoreNoRealm : IRComponent :=
  { name := "TenantAStore", provides := ["kv"] }

def irTenantBStoreNoRealm : IRComponent :=
  { name := "TenantBStore", provides := ["kv"] }

/-- Non-vacuity, admitting side: the IR image of the tenants composition is
admitted by the model's own `LinkOK`, so `linkOK_ir_disjoint` has its
hypothesis here. -/
theorem tenants_link_ir : LinkOK ([irTenantAStore, irTenantBStore].map ofIR) := by
  refine LinkOK.cons _ _ (by decide) (by decide) (by decide) ?_
  exact LinkOK.cons _ _ (by decide) (by decide) (by decide) LinkOK.nil

/-- The theorem's conclusion on the same document, through the theorem. -/
theorem tenants_ir_disjoint : IRDisjoint [irTenantAStore, irTenantBStore] :=
  linkOK_ir_disjoint _ tenants_link_ir

/-- Non-vacuity, refusing side, and the mutation witness: the same document is
disjoint with its realms and not disjoint once the `isolate` entry is gone, so
the `IRD` row's verdict flips on exactly the field a lowering defect would
drop. -/
theorem isolate_entry_is_load_bearing :
    IRDisjoint [irTenantAStore, irTenantBStore] ∧
      ¬ IRDisjoint [irTenantAStoreNoRealm, irTenantBStoreNoRealm] := by
  refine ⟨?_, ?_⟩ <;> decide


end RevL.G2

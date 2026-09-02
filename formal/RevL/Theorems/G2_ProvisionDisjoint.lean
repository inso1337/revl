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

end RevL.G2

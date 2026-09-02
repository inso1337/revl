import RevL.Manifest
import RevL.Lemmas.ManifestLemmas

/-!
# G3 — dependency cycles rejected

DESIGN.md §4: "Dependency cycles rejected" (§6.5). The formal content is
in three parts:

1. `depPath_rank_lt` / `no_dependency_cycles` — a composition that admits
   a *layering* (ranks strictly decreasing along every
   provision→requirement dependency) admits no finite dependency path
   from a slot to itself.
2. `linkOK_layered` — the **bridge**: every `LinkOK` composition admits a
   layering, constructed as `rankOf`. Without it the layering hypothesis
   of (1) is unestablishable and G3 has no proof path from anything the
   model admits; with it, `LinkOK` alone excludes cycles.
3. `linkOK_no_cycles` — the two composed, which is the statement the
   linker actually implements.

The rank construction is the admission order itself: `rankOf comps s` is
the length of the suffix of `comps` beginning at `s`'s provider (0 when
nothing provides `s`). It is a layering exactly because `LinkOK` requires
each component's consumed slots to be provided *strictly deeper* in the
list — the model's form of the linker's two G3 refusals, "component N
requires a key it provides itself (`k`)" and "dependency cycle: ...".
`self_provision_refused` and `mutual_cycle_refused` track those two on
concrete compositions.
-/

namespace RevL.G3

open RevL.Manifest

/-- Along a dependency path, ranks strictly decrease. -/
theorem depPath_rank_lt : ∀ {comps : List LComponent} {s₁ s₂ : Slot},
    ∀ rank : Slot → Nat, LayeredBy comps rank → DepPath comps s₁ s₂ →
    rank s₂ < rank s₁ := by
  intro comps s₁ s₂ rank hl hpath
  induction hpath with
  | step a hd =>
    obtain ⟨p, hp, hprov, hreq⟩ := hd
    exact hl p hp s₁ hprov a hreq
  | trans a b hrec hdep ih =>
    obtain ⟨p, hp, hprov, hreq⟩ := hdep
    exact Nat.lt_trans (hl p hp a hprov b hreq) ih

/-- G3: a layered composition admits no dependency cycle — the link
judgment's layering certificate cannot coexist with a path from a slot
back to itself. -/
theorem no_dependency_cycles : ∀ (comps : List LComponent) (s : Slot),
    (∃ rank : Slot → Nat, LayeredBy comps rank) → DepPath comps s s → False := by
  intro comps s ⟨rank, hl⟩ hpath
  have hlt : rank s < rank s := depPath_rank_lt rank hl hpath
  exact absurd hlt (Nat.lt_irrefl (rank s))

/-- The admission order *is* a layering: `rankOf` decreases along every
provision→requirement edge of an admitted composition. -/
theorem linkOK_layeredBy_rankOf : ∀ comps : List LComponent,
    LinkOK comps → LayeredBy comps (rankOf comps) := by
  intro comps h
  induction h with
  | nil => intro p hp; cases hp
  | cons c comps _ hdis hcl hlink ih =>
    intro p hp s' hs' s hs
    cases hp with
    | head =>
      -- the head's own slots sit at the top of the layering, and
      -- everything it consumes is provided by the tail, strictly below.
      have hsmem : s ∈ comps.flatMap slots := hcl s hs
      have hnot : s ∉ slots c := fun hc => hdis s hc hsmem
      rw [RevL.Lemmas.rankOf_head c comps s' hs',
          RevL.Lemmas.rankOf_tail c comps s hnot]
      exact Nat.lt_succ_of_le (RevL.Lemmas.rankOf_le comps s)
    | tail _ hp' =>
      -- a deeper component keeps the ranks the tail assigned: provision
      -- disjointness puts neither slot in the head's surface.
      have hs'mem : s' ∈ comps.flatMap slots :=
        RevL.Lemmas.slots_mem_flatMap hp' hs'
      have hsmem : s ∈ comps.flatMap slots :=
        RevL.Lemmas.linkOK_needs_mem hlink p hp' s hs
      rw [RevL.Lemmas.rankOf_tail c comps s' (fun hc => hdis s' hc hs'mem),
          RevL.Lemmas.rankOf_tail c comps s (fun hc => hdis s hc hsmem)]
      exact ih p hp' s' hs' s hs

/-- The bridge C2 asks for: an admitted composition always *has* a
layering certificate, so `no_dependency_cycles`' hypothesis is
dischargeable from `LinkOK` rather than assumed. -/
theorem linkOK_layered : ∀ comps : List LComponent,
    LinkOK comps → ∃ rank : Slot → Nat, LayeredBy comps rank :=
  fun comps h => ⟨rankOf comps, linkOK_layeredBy_rankOf comps h⟩

/-- G3 as the linker states it: a composition the link judgment admits
has no dependency cycle, with no layering assumed anywhere. -/
theorem linkOK_no_cycles : ∀ (comps : List LComponent) (s : Slot),
    LinkOK comps → DepPath comps s s → False :=
  fun comps s h => no_dependency_cycles comps s (linkOK_layered comps h)

-- ------------------------------------------------------- non-vacuity

/-- `tests/test_why_traces.py`'s `Ouroboros`: one component that both
requires and provides `s`. The linker refuses it with "component
Ouroboros requires a key it provides itself (`s`) (G3)". -/
def ouroboros : LComponent :=
  { name := "Ouroboros", requires := ["s"], provides := ["s"] }

/-- `examples/rejections/g3_dependency_cycle.rvl`: Alpha requires what
Beta provides and vice versa. -/
def alpha : LComponent :=
  { name := "Alpha", requires := ["b"], provides := ["a"] }

def beta : LComponent :=
  { name := "Beta", requires := ["a"], provides := ["b"] }

/-- A Beta with its requirement dropped: the acyclic half of the same
fixture, kept as the positive control for `layering_exists_for_admitted`. -/
def betaRoot : LComponent :=
  { name := "Beta", requires := [], provides := ["b"] }

/-- Non-vacuity for the C2 fix: the self-provision program is refused.
Before the fix `LinkOK [ouroboros]` was derivable — the component
satisfied its own requirement — and the layering hypothesis was
unreachable. -/
theorem self_provision_refused : ¬ LinkOK [ouroboros] := by
  intro h
  cases h with
  | cons _ _ _ _ hcl _ =>
    exact absurd (hcl ("s", sharedRealm) (by decide)) (by decide)

/-- Non-vacuity for the general cycle: the mutual-dependency program is
refused in *both* orderings, which is what "a program links iff some
ordering derives `LinkOK`" needs in order to mean anything. -/
theorem mutual_cycle_refused :
    ¬ LinkOK [alpha, beta] ∧ ¬ LinkOK [beta, alpha] := by
  constructor
  · intro h
    cases h with
    | cons _ _ _ _ _ h' =>
      cases h' with
      | cons _ _ _ _ hcl' _ =>
        exact absurd (hcl' ("a", sharedRealm) (by decide)) (by decide)
  · intro h
    cases h with
    | cons _ _ _ _ _ h' =>
      cases h' with
      | cons _ _ _ _ hcl' _ =>
        exact absurd (hcl' ("b", sharedRealm) (by decide)) (by decide)

/-- The layering is not vacuously available: a composition that *does*
link gets a certificate, and it is the admission order. -/
theorem layering_exists_for_admitted :
    ∃ rank : Slot → Nat, LayeredBy [alpha, betaRoot] rank := by
  refine linkOK_layered _ ?_
  refine LinkOK.cons _ _ (by decide) (by decide) (by decide) ?_
  exact LinkOK.cons _ _ (by decide) (by decide) (by decide) LinkOK.nil

end RevL.G3

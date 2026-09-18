import RevL.Manifest
import RevL.Lemmas.ManifestLemmas

/-!
# A9 — every provide block is a declared slot

`diagnostics.GUARANTEES["A9"]`: a provide block's key must be named in the
component's `provides` clause. The checker refuses it in
`lower._lower_provide` — "`k` is not declared in the `provides` clause of
C (A9)" — and, one line later, refuses the same key installed twice
("provision `k` is installed twice in C", uncoded).
`examples/rejections/a9_provide_key_not_declared.rvl` is the fixture:
`component S provides skin1: Skin { provide skin { … } }`.

## Why this row matters to G2/G3

`RevL.Manifest.slots` — the universe `LinkOK`, `ProvidesDisjoint` and the
layering certificate are stated over — is computed from the `provides`
CLAUSE. A provide block keyed under a name the clause never announced is
an implementation the linker's slot table does not contain. A9 is the
bridge from what a component *installs* to what G2/G3 reason about: under
`A9OK` every installed block answers a `(key, realm)` slot of its own
component (`installed_block_is_slot`), and in an admitted composition that
slot is provided by no deeper component (`installed_block_uniquely_provided`).
Without `A9OK` some installed block is no slot of the component in any
realm (`undeclared_block_is_no_slot`), which is the fixture's shape.

## The model, and what it deliberately does not say

`LComponent` is L0 and carries the clause only. The blocks are modelled
BESIDE it (`Installed`), one key per `provide k { … }` block in body order,
so a double install is a repeated entry; nothing in L0 moves.

The checker enforces exactly two things about block keys, and only those
two are stated here:

  * `A9OK` — every block key is in the clause (code A9);
  * `NoDoubleInstall` — no key has two blocks (uncoded).

The CONVERSE — a declared key with no block — is NOT enforced by
`lower._lower_component`: `provided_keys` is read by the double-install
check alone, and a component may declare `provides k: S` and install
nothing (it is then a provider the linker admits and the runtime never
plugs). No theorem here claims otherwise.

The differential oracle (`harness/Oracle.lean`, the `A9` verdict row)
decides `a9B` over the `PB` fact rows the exporter reads off the provide
BLOCKS, independently of the `C` rows it reads off the clause;
`a9_row_not_vacuous` pins that the verdict moves with the block alone.
-/

namespace RevL.A9

open RevL.Manifest

/-- A component beside the keys its body INSTALLS: one entry per
`provide k { … }` block, in body order. `comp` is the L0 manifest view
(the `provides` CLAUSE); `blocks` is what the body actually plugs. -/
structure Installed where
  comp : LComponent
  blocks : List String

/-- A9: every installed block key is declared in the `provides` clause. -/
def A9OK (i : Installed) : Prop := ∀ k ∈ i.blocks, k ∈ i.comp.provides

/-- The uncoded sibling refusal, "provision `k` is installed twice": no
key has two blocks. -/
def NoDoubleInstall (i : Installed) : Prop := List.Nodup i.blocks

/-- The decision procedure the oracle prints. -/
def a9B (i : Installed) : Bool :=
  i.blocks.all (fun k => i.comp.provides.contains k)

/-- **The A9 verdict is the model's judgment.** -/
theorem a9B_iff (i : Installed) : a9B i = true ↔ A9OK i := by
  simp [a9B, A9OK, List.all_eq_true]

def noDoubleInstallB (i : Installed) : Bool := decide (List.Nodup i.blocks)

theorem noDoubleInstallB_iff (i : Installed) :
    noDoubleInstallB i = true ↔ NoDoubleInstall i := by
  simp [noDoubleInstallB, NoDoubleInstall]

/-- The slot an installed block answers: its key, in the realm the
component places that key (`slots` spells the clause the same way). -/
def installedSlots (i : Installed) : List Slot :=
  i.blocks.map (fun k => (k, i.comp.realm k))

-- ---------------------------------------------------- the bridge to G2/G3

/-- Under A9 every installed block is a `slots` member of its own
component: the G2/G3 universe covers every block that will answer a
resolution. -/
theorem installed_block_is_slot (i : Installed) (h : A9OK i) :
    ∀ k ∈ i.blocks, (k, i.comp.realm k) ∈ slots i.comp := by
  intro k hk
  exact List.mem_map.mpr ⟨k, h k hk, rfl⟩

/-- Without A9 some installed block is no slot of the component at all —
in no realm. This is the refused fixture's shape: `provide skin` under
`provides skin1`. -/
theorem undeclared_block_is_no_slot (i : Installed) (h : ¬ A9OK i) :
    ∃ k ∈ i.blocks, ∀ r, (k, r) ∉ slots i.comp := by
  unfold A9OK at h
  obtain ⟨k, hk⟩ := Classical.not_forall.mp h
  obtain ⟨hk, hnot⟩ := Classical.not_imp.mp hk
  refine ⟨k, hk, fun r hr => ?_⟩
  obtain ⟨k', hk', heq⟩ := List.mem_map.mp hr
  have hkk : k' = k := (Prod.mk.inj heq).1
  exact hnot (hkk ▸ hk')

/-- In an admitted composition, an installed block's slot is provided by
its own component and by no component admitted before it: `LinkOK`'s
disjointness clause reaches the block through A9. -/
theorem installed_block_uniquely_provided (c : LComponent)
    (cs : List LComponent) (blocks : List String)
    (hl : LinkOK (c :: cs)) (h : A9OK ⟨c, blocks⟩) :
    ∀ k ∈ blocks, (k, c.realm k) ∈ slots c ∧ (k, c.realm k) ∉ cs.flatMap slots := by
  intro k hk
  have hs : (k, c.realm k) ∈ slots c := installed_block_is_slot ⟨c, blocks⟩ h k hk
  cases hl with
  | cons _ _ _ hdis _ _ => exact ⟨hs, hdis _ hs⟩

/-- No double install means the installed slots are pairwise distinct:
one block per slot, which is the shape `LinkOK`'s `Nodup (slots c)` asks
of the clause. -/
theorem installed_slots_nodup (i : Installed) (h : NoDoubleInstall i) :
    List.Nodup (installedSlots i) := by
  unfold installedSlots NoDoubleInstall at *
  simp only [List.Nodup, List.pairwise_map]
  exact h.imp fun hne heq => hne (Prod.mk.inj heq).1

-- ------------------------------------------------------- non-vacuity

/-- `examples/rejections/a9_provide_key_not_declared.rvl`:
`component S provides skin1: Skin { provide skin { … } }`. -/
def fixtureS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin1"] }, ["skin"]⟩

/-- The fixture's first fix (its hint's "rename the provide block to a
declared key"): the block renamed to `skin1`. -/
def renamedS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin1"] }, ["skin1"]⟩

/-- The fixture's second fix ("add `skin: <Service>` to the `provides`
clause"): the clause grown to declare `skin`. -/
def declaredS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin1", "skin"] }, ["skin"]⟩

/-- The uncoded refusal's shape: `skin1` declared once and installed twice. -/
def twiceS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin1"] }, ["skin1", "skin1"]⟩

/-- Non-vacuity: the fixture is refused, and BOTH of its hint's fixes are
admitted; the renamed twin also links on its own, so
`installed_block_uniquely_provided`'s two hypotheses hold together. -/
theorem a9_not_vacuous :
    a9B fixtureS = false ∧ ¬ A9OK fixtureS ∧
    a9B renamedS = true ∧ A9OK renamedS ∧ LinkOK [renamedS.comp] ∧
    a9B declaredS = true ∧ A9OK declaredS := by
  refine ⟨by decide, fun h => absurd ((a9B_iff _).mpr h) (by decide),
    by decide, (a9B_iff _).mp (by decide), ?_, by decide, (a9B_iff _).mp (by decide)⟩
  exact LinkOK.cons _ _ (by decide) (by decide) (by decide) LinkOK.nil

/-- Anti-tautology: the two rules are distinct predicates, not one rule
twice. The double install satisfies A9 and fails `NoDoubleInstall`; the
fixture satisfies `NoDoubleInstall` and fails A9. -/
theorem a9_rules_are_distinct :
    A9OK twiceS ∧ ¬ NoDoubleInstall twiceS ∧
    NoDoubleInstall fixtureS ∧ ¬ A9OK fixtureS := by
  refine ⟨(a9B_iff _).mp (by decide), fun h => absurd ((noDoubleInstallB_iff _).mpr h) (by decide),
    (noDoubleInstallB_iff _).mp (by decide),
    fun h => absurd ((a9B_iff _).mpr h) (by decide)⟩

/-- The oracle row is mutation-sensitive to the BLOCK: the fixture and its
renamed twin have the same manifest (the same `C`/`M` facts) and the
verdict differs, so the `A9` row cannot be read off the clause. -/
theorem a9_row_not_vacuous :
    fixtureS.comp = renamedS.comp ∧ a9B fixtureS ≠ a9B renamedS := by
  exact ⟨rfl, by decide⟩

end RevL.A9

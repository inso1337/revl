import RevL.Manifest
import RevL.Lemmas.ManifestLemmas

/-!
# A9 — every provide block is a declared slot, and every declared slot is installed

`diagnostics.GUARANTEES["A9"]`: a provide block's key must be named in the
component's `provides` clause. The checker enforces it in BOTH directions:

  * `lower._lower_provide` — "`k` is not declared in the `provides` clause
    of C (A9)" (issue 1167), and one line later the same key installed
    twice ("provision `k` is installed twice in C", uncoded);
  * `lower._lower_component`, after the body walk — "`k` is declared in
    the `provides` clause of C but no `provide k { … }` block installs it
    (A9)" (issue #1172, PR #1184): the converse. Two body forms install a
    declared key, a `provide k { … }` block and the multi-realm bind
    `isolate k in realms(...)` (the IR's `routes`), whose key is realized
    as a routing proxy at load and never plugged as a fiber
    (`stdlib/router.rvl`'s `RoundRobin`, the live case).

Fixtures: `examples/rejections/a9_provide_key_not_declared.rvl`
(`component S provides skin1: Skin { provide skin { … } }`) and
`examples/rejections/a9_provides_without_block.rvl`
(`component S provides skin: Skin { let x = … }`, no block).
`tests/formal_corpus/a9_routes_installs_key.rvl` carries the admitted
routed shape (three `PoolWorker`s and a `RoundRobin`).

## Why this row matters to G2/G3

`RevL.Manifest.slots` — the universe `LinkOK`, `ProvidesDisjoint` and the
layering certificate are stated over — is computed from the `provides`
CLAUSE. A provide block keyed under a name the clause never announced is
an implementation the linker's slot table does not contain; a declared
key nothing installs is a slot the linker hands out with nothing behind
it (every consumer PENDING at run time, R2). A9 is the bridge from what a
component *installs* to what G2/G3 reason about: under `A9OK` every
installed block answers a `(key, realm)` slot of its own component
(`installed_block_is_slot`), in an admitted composition that slot is
provided by no deeper component (`installed_block_uniquely_provided`),
and with no route every declared slot has a block behind it
(`unrouted_needs_a_block`). Without the first direction some installed
block is no slot of the component in any realm
(`undeclared_block_is_no_slot`); without the second some declared slot is
refused (`declared_uninstalled_refused`).

## The model, and what it deliberately does not say

`LComponent` is L0 and carries the clause only. The blocks and the routed
keys are modelled BESIDE it (`Installed`), one block key per
`provide k { … }` in body order (a double install is a repeated entry) and
one routed key per `isolate k in realms(...)`; nothing in L0 moves.

`A9OK` is the conjunction of the two directions, `BlocksDeclared` and
`DeclaredInstalled`, kept as named predicates so the theorems about one
direction say which one they are about.

Not modelled, and said here so the row's reach is not overstated:

  * the checker SKIPS the converse for a body that recovered past a
    refused statement (item 386, Stage 2: the component is already failing
    on what the walk reached, and the refused statement may have been the
    block). The model has no notion of recovery, so such a component reads
    `fail` here while the checker reports the earlier code; that lands in
    `formal-found-other` (informational), never in a FATAL bucket;
  * the route's realm legs. The linker resolves a routed requirement per
    leg against `provider_of[(key, leg)]`, which `LComponent`'s one realm
    per key cannot express; the harness elides the routed requirement from
    the V row's manifest and carries the route only as the A9 installation
    fact (`PR` row). Item 162's "every routed realm needs a provider" check
    is therefore not under the V row.

The differential oracle (`harness/Oracle.lean`, the `A9` verdict row)
decides `a9B` over the `PB` (blocks) and `PR` (routes) fact rows the
exporter reads off the body, independently of the `C` rows it reads off
the clause; `a9_row_not_vacuous` pins that the verdict moves with the
block and with the route alone.
-/

namespace RevL.A9

open RevL.Manifest

/-- A component beside the keys its body INSTALLS: one entry per
`provide k { … }` block, in body order, and one per `isolate k in
realms(...)` bind. `comp` is the L0 manifest view (the `provides` CLAUSE);
`blocks` and `routed` are what the body actually plugs. -/
structure Installed where
  comp : LComponent
  blocks : List String
  routed : List String

/-- Direction 1 (issue 1167): every installed block key is declared. -/
def BlocksDeclared (i : Installed) : Prop := ∀ k ∈ i.blocks, k ∈ i.comp.provides

/-- Direction 2 (issue #1172): every declared key is installed, by a block
or by a `realms(...)` bind. -/
def DeclaredInstalled (i : Installed) : Prop :=
  ∀ k ∈ i.comp.provides, k ∈ i.blocks ∨ k ∈ i.routed

/-- A9, both directions. -/
def A9OK (i : Installed) : Prop := BlocksDeclared i ∧ DeclaredInstalled i

/-- The uncoded sibling refusal, "provision `k` is installed twice": no
key has two blocks. -/
def NoDoubleInstall (i : Installed) : Prop := List.Nodup i.blocks

def blocksDeclaredB (i : Installed) : Bool :=
  i.blocks.all (fun k => i.comp.provides.contains k)

def declaredInstalledB (i : Installed) : Bool :=
  i.comp.provides.all (fun k => i.blocks.contains k || i.routed.contains k)

/-- The decision procedure the oracle prints. -/
def a9B (i : Installed) : Bool := blocksDeclaredB i && declaredInstalledB i

theorem blocksDeclaredB_iff (i : Installed) :
    blocksDeclaredB i = true ↔ BlocksDeclared i := by
  simp [blocksDeclaredB, BlocksDeclared, List.all_eq_true]

theorem declaredInstalledB_iff (i : Installed) :
    declaredInstalledB i = true ↔ DeclaredInstalled i := by
  simp [declaredInstalledB, DeclaredInstalled, List.all_eq_true]

/-- **The A9 verdict is the model's judgment**, in both directions. -/
theorem a9B_iff (i : Installed) : a9B i = true ↔ A9OK i := by
  simp only [a9B, A9OK, Bool.and_eq_true, blocksDeclaredB_iff, declaredInstalledB_iff]

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
  exact List.mem_map.mpr ⟨k, h.1 k hk, rfl⟩

/-- Without direction 1 some installed block is no slot of the component
at all — in no realm. This is the first fixture's shape: `provide skin`
under `provides skin1`. -/
theorem undeclared_block_is_no_slot (i : Installed) (h : ¬ BlocksDeclared i) :
    ∃ k ∈ i.blocks, ∀ r, (k, r) ∉ slots i.comp := by
  unfold BlocksDeclared at h
  obtain ⟨k, hk⟩ := Classical.not_forall.mp h
  obtain ⟨hk, hnot⟩ := Classical.not_imp.mp hk
  refine ⟨k, hk, fun r hr => ?_⟩
  obtain ⟨k', hk', heq⟩ := List.mem_map.mp hr
  have hkk : k' = k := (Prod.mk.inj heq).1
  exact hnot (hkk ▸ hk')

/-- In an admitted composition, an installed block's slot is provided by
its own component and by no component admitted before it: `LinkOK`'s
disjointness clause reaches the block through A9. -/
theorem installed_block_uniquely_provided (i : Installed) (cs : List LComponent)
    (hl : LinkOK (i.comp :: cs)) (h : A9OK i) :
    ∀ k ∈ i.blocks, (k, i.comp.realm k) ∈ slots i.comp ∧
      (k, i.comp.realm k) ∉ cs.flatMap slots := by
  intro k hk
  have hs : (k, i.comp.realm k) ∈ slots i.comp := installed_block_is_slot i h k hk
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

-- ------------------------------------------------------ the converse

/-- A declared key that neither a block nor a route installs refuses the
component: the second fixture's shape, `provides skin` with no
`provide skin { … }`. -/
theorem declared_uninstalled_refused (i : Installed) (k : String)
    (hk : k ∈ i.comp.provides) (hb : k ∉ i.blocks) (hr : k ∉ i.routed) :
    ¬ A9OK i := by
  intro h
  rcases h.2 k hk with hb' | hr'
  · exact hb hb'
  · exact hr hr'

/-- With no route in play, A9 says exactly "every declared key has a
block": the converse as the checker states it for an ordinary provider. -/
theorem unrouted_needs_a_block (i : Installed) (hr : i.routed = []) (h : A9OK i) :
    ∀ k ∈ i.comp.provides, k ∈ i.blocks := by
  intro k hk
  rcases h.2 k hk with hb | hr'
  · exact hb
  · rw [hr] at hr'; cases hr'

/-- The exemption is real: a component whose every declared key is routed
satisfies A9 with NO block at all — `RoundRobin`'s shape, where the
`provides worker` clause is what the route needs and a block would be
refused (item 449). -/
theorem routed_installs_without_block (i : Installed) (hb : i.blocks = [])
    (hr : ∀ k ∈ i.comp.provides, k ∈ i.routed) : A9OK i := by
  refine ⟨fun k hk => ?_, fun k hk => Or.inr (hr k hk)⟩
  rw [hb] at hk; cases hk

-- ------------------------------------------------------- non-vacuity

/-- `examples/rejections/a9_provide_key_not_declared.rvl`:
`component S provides skin1: Skin { provide skin { … } }`. -/
def fixtureS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin1"] }, ["skin"], []⟩

/-- The fixture's first fix (its hint's "rename the provide block to a
declared key"): the block renamed to `skin1`. -/
def renamedS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin1"] }, ["skin1"], []⟩

/-- The fixture's second fix ("add `skin: <Service>` to the `provides`
clause") taken LITERALLY: the clause grown to declare `skin`, `skin1`
still declared and still uninstalled. Admitted before the converse;
refused by it now. -/
def halfDeclaredS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin1", "skin"] }, ["skin"], []⟩

/-- The second fix taken to completion: `skin` declared and the orphan
`skin1` dropped. -/
def declaredS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin"] }, ["skin"], []⟩

/-- `examples/rejections/a9_provides_without_block.rvl`: `component S
provides skin: Skin { let x = … }`, no block, no route. -/
def uninstalledS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin"] }, [], []⟩

/-- `stdlib/router.rvl`'s `RoundRobin` (and
`tests/formal_corpus/a9_routes_installs_key.rvl`): `requires worker`,
`provides worker`, `isolate worker in realms("w1", "w2", "w3")`, no block. -/
def roundRobin : Installed :=
  ⟨{ name := "RoundRobin", requires := ["worker"], provides := ["worker"] },
   [], ["worker"]⟩

/-- One of its backends: `provides worker` in realm `w1`, with a block. -/
def poolWorker1 : Installed :=
  ⟨{ name := "PoolWorker1", requires := [], provides := ["worker"],
     realm := fun _ => "w1" }, ["worker"], []⟩

/-- The uncoded refusal's shape: `skin1` declared once and installed twice. -/
def twiceS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin1"] }, ["skin1", "skin1"], []⟩

/-- Direction 1 failing alone: `skin1` installed, and an extra block
`skin` the clause never declared. -/
def extraBlockS : Installed :=
  ⟨{ name := "S", requires := [], provides := ["skin1"] }, ["skin1", "skin"], []⟩

/-- Non-vacuity, direction 1: the first fixture is refused; the renamed
twin is admitted and links on its own, so
`installed_block_uniquely_provided`'s two hypotheses hold together; the
hint's second fix is admitted once the orphan is dropped, and REFUSED by
the converse when taken literally (`halfDeclaredS`). -/
theorem a9_not_vacuous :
    a9B fixtureS = false ∧ ¬ A9OK fixtureS ∧
    a9B renamedS = true ∧ A9OK renamedS ∧ LinkOK [renamedS.comp] ∧
    a9B declaredS = true ∧ A9OK declaredS ∧
    a9B halfDeclaredS = false ∧ ¬ A9OK halfDeclaredS := by
  refine ⟨by decide, fun h => absurd ((a9B_iff _).mpr h) (by decide),
    by decide, (a9B_iff _).mp (by decide), ?_, by decide, (a9B_iff _).mp (by decide),
    by decide, fun h => absurd ((a9B_iff _).mpr h) (by decide)⟩
  exact LinkOK.cons _ _ (by decide) (by decide) (by decide) LinkOK.nil

/-- Non-vacuity, direction 2 (issue #1172): the second fixture is refused
with no block and no route; `RoundRobin` is admitted with no block because
its one declared key is routed; a backend with a block is admitted. -/
theorem a9_converse_not_vacuous :
    a9B uninstalledS = false ∧ ¬ A9OK uninstalledS ∧
    a9B roundRobin = true ∧ A9OK roundRobin ∧
    a9B poolWorker1 = true ∧ A9OK poolWorker1 := by
  exact ⟨by decide, fun h => absurd ((a9B_iff _).mpr h) (by decide),
    by decide, (a9B_iff _).mp (by decide), by decide, (a9B_iff _).mp (by decide)⟩

/-- Anti-tautology: the two DIRECTIONS are distinct predicates.
`halfDeclaredS` passes direction 1 and fails direction 2; `extraBlockS`
fails direction 1 and passes direction 2. -/
theorem a9_directions_are_distinct :
    BlocksDeclared halfDeclaredS ∧ ¬ DeclaredInstalled halfDeclaredS ∧
    ¬ BlocksDeclared extraBlockS ∧ DeclaredInstalled extraBlockS := by
  exact ⟨(blocksDeclaredB_iff _).mp (by decide),
    fun h => absurd ((declaredInstalledB_iff _).mpr h) (by decide),
    fun h => absurd ((blocksDeclaredB_iff _).mpr h) (by decide),
    (declaredInstalledB_iff _).mp (by decide)⟩

/-- Anti-tautology: A9 and the double install are distinct rules, not one
rule twice. The double install satisfies A9 and fails `NoDoubleInstall`;
the first fixture satisfies `NoDoubleInstall` and fails A9. -/
theorem a9_rules_are_distinct :
    A9OK twiceS ∧ ¬ NoDoubleInstall twiceS ∧
    NoDoubleInstall fixtureS ∧ ¬ A9OK fixtureS := by
  refine ⟨(a9B_iff _).mp (by decide), fun h => absurd ((noDoubleInstallB_iff _).mpr h) (by decide),
    (noDoubleInstallB_iff _).mp (by decide),
    fun h => absurd ((a9B_iff _).mpr h) (by decide)⟩

/-- The oracle row is mutation-sensitive to the BODY facts alone: three
pairs with the same manifest (the same `C`/`M` facts) and different
verdicts — a renamed block, an added block, and a dropped route. So the
`A9` row cannot be read off the clause, and the `PR` fact is
load-bearing. -/
theorem a9_row_not_vacuous :
    (fixtureS.comp = renamedS.comp ∧ a9B fixtureS ≠ a9B renamedS) ∧
    (uninstalledS.comp = declaredS.comp ∧ a9B uninstalledS ≠ a9B declaredS) ∧
    a9B roundRobin ≠ a9B { roundRobin with routed := [] } := by
  exact ⟨⟨rfl, by decide⟩, ⟨rfl, by decide⟩, by decide⟩

end RevL.A9

# revl formal backbone — proof status

Rules of the layering (enforced by imports, not by hope):

- **L0** (`RevL.Syntax`, `RevL.Typing`, `RevL.Semantics`) is
  architect-owned and frozen. Worker sessions do not edit it; a needed L0
  change blocks on the architect.
- **L1** (`RevL.Lemmas.*`) is the lemma farm. Farm files import L0 only
  (ideally core only) and never each other.
- **L2** (`RevL.Theorems.G*`) is one file per guarantee, one session per
  file. Worker files import L0/L1, **never each other**. Theorem names go
  into `CheckAxioms.lean` the moment they are stated.
- A theorem counts as *proved* only if the axioms gate passes for it:
  no `sorryAx`, no project-defined axiom. Lean's three standard
  foundation axioms (`propext`, `Classical.choice`, `Quot.sound`) are
  whitelisted; anything else fails `make formal`. (`#print axioms` on a
  `sorry`'d declaration reports `sorryAx`, so an unfinished proof can
  never pass.)

## Theorem status

| Theorem | Guarantee (DESIGN.md §4) | Status | Axioms | Notes |
|---|---|---|---|---|
| `RevL.G2.linkOK_provision_disjoint` | G2 — provision disjointness (Def. 43) | **proved** | `propext, Quot.sound` | from the incremental `LinkOK` judgment |
| `RevL.G2.linkOK_requires_closed` | G2/G1 — requirement closure | **proved** | `propext` | every requirement provided in-composition |
| `RevL.G3.depPath_rank_lt` | G3 — cycles rejected (§6.5) | **proved** | none | ranks strictly decrease along dep paths |
| `RevL.G3.no_dependency_cycles` | G3 | **proved** | none | a layering certificate excludes cycles |
| `RevL.G4.inverse_or_emit` | G4 — inverse-or-emit (Def. 8) | **proved** | none | content is the shape of `Typed` |
| `RevL.G5.teardown_registers_nothing` | G5 — teardown registers nothing | **proved** | none | undo bodies are a separate constructor set |
| `RevL.G6.confinement` | G6 — confinement (Def. 48) | **proved** | `propext, Quot.sound` | content is the shape of `TypedIn`/`ReachIn` |
| `RevL.G7.teardown_replays_all` | G7 — LIFO-completeness (Thm. 16) | **proved** | `propext, Quot.sound` | every witnessed inverse is replayed |
| `RevL.G7.teardown_only_witnessed` | G7 | **proved** | `propext, Quot.sound` | nothing unwitnessed is replayed |
| `RevL.G7.teardown_eq_reversed_inverses` | G7 | **proved** | `propext` | the LIFO equation; positions via `List.getElem_reverse` |
| `RevL.Semantics.teardown_length` | G7 — length form | **proved** | `propext` | one replay per witnessed effect |
| `RevL.G8.boundary_enumerates_emissions` | G8 — boundary enumerable (§6.1) | **proved** | `propext, Quot.sound` | completeness: every emission is on the audited surface |
| `RevL.G8.boundary_only_declared` | G8 | **proved** | `propext, Quot.sound` | soundness: on a typed body the surface is exactly the emissions |
| `RevL.CapCeilings.cap_order_partial` | item 294 — the `(T,P)` capability order | **proved** | `propext, Quot.sound` | `covers` is reflexive, transitive, antisymmetric |
| `RevL.CapCeilings.attenuation_monotone` | items 66/294 — attenuation is downward | **proved** | `propext` | a lineage never exceeds the root's declared authority |
| `RevL.CapCeilings.lineage_ceiling_le` | item 260 — budgets only shrink | **proved** | `propext, Quot.sound` | a dropped child ceiling reads as `+∞`, so it cannot escape |
| `RevL.CapCeilings.spend_within_budget` | item 260 — the runtime counter | **proved** | `propext, Quot.sound` | `remainingUses` is never overdrawn |
| `RevL.CapCeilings.budget_never_exceeds_root_ceiling` | item 260 — end to end | **proved** | `propext, Quot.sound` | static shrink composed with the dynamic counter |
| `RevL.CapCeilings.confinement_within_ceiling` | item 294 + G6 | **proved** | `propext, Quot.sound` | reach is bounded by `Γ`, `Γ` by the root ceiling |
| `RevL.CapCeilings.no_star_amplification` | item 66 — the host boundary | **proved** | `propext` | `*` is covered only by `*`, so it is never manufactured |
| `RevL.CapCeilings.parameter_widening_refused` | item 294 — non-vacuity | **proved** | `propext` | tracks `examples/rejections/g4_spawn_widens_parameter.rvl` |
| `RevL.CapCeilings.ceiling_check_not_subsumed` | item 260 — non-vacuity | **proved** | `propext` | the resource fold is ceiling-blind; the budget check is not |

(`propext` / `Quot.sound` are Lean's standard foundation axioms; the gate
whitelists exactly those three.)

## Item 133 — cross-tier agreement (`RevL.Theorems.CrossTier`)

`RevL.CrossTier` models each of the six backends by its *observable
profile* on the three DIVERGENCES axes — the numeric tag an unannotated
literal defaults to, the string unit, and the map iteration order — and
lowers a small value IR (`Atom`/`Value`, numeric literals carrying an
optional operand annotation) through a profile with `eval`.

The theorem states exactly the item-133 conditions and proves they
suffice: `cross_tier_agreement` shows any two `Conformant` profiles (code-
point string unit + canonical map order) lower a `WellAnnotated` IR (every
numeric literal operand-annotated) to the *same* `Value`;
`six_tier_agreement` is the corollary over the six-element `Tier`. The
numeric default is deliberately left free per tier, and
`annotation_necessary` exhibits python vs typescript disagreeing on a bare
literal — so the annotation hypothesis is load-bearing, not vacuous. No
`sorry`, no project axioms.

Deliberately out of scope (documented, not smuggled as axioms): that the
real emitters realise a conformant profile is the differential conformance
matrix's empirical obligation, not a Lean theorem; and map values are
modelled one level deep (nested maps are a mechanical extension of
`entries_agree`).

## Differential oracle (wired)

`harness/diff_corpus.py` + `harness/Oracle.lean`: parse every corpus
`.rvl` with revl's real parser, export one TSV row per *fact* — component
manifests (M), require-binding resolutions (R), declared emission methods
(E), per-statement classifications (T), and call facts with marker
context (U) — then run the Lean oracle (`RevL.Manifest` + a G4-shaped
body judgment, coded independently) over the same TSV and diff against a
plain-Python reference of the same semantics. A mismatch is definitional
drift between the model and the spec/extraction, and it fails
`make formal`.

Verdicts:

- **V rows (per file, G2/G3)**: provision disjointness + requirement
  closure over the whole composition (a component's requirements need not
  be its own provisions).
- **G rows (per component, G4-shaped)**: marker presence must equal the
  interface's declaration — a plain call to a declared emission method,
  or an `emit`'d call to a non-emission method, is refused.

Current status over the corpus: **289 files → 179 components → 531
statements → 127 manifest-bearing files → 306 verdicts compared
(127 files + 179 components), 306 agree, 0 mismatches** (28 parse-error
skips, loud).
A mismatch is definitional drift between the model and the
spec/extraction — this is the gate that keeps parallel edits to the
formal model honest.

Checker alignment (informational, not a gate): each parsed file is also
compiled with the real checker and its refusal code is compared against
the formal verdicts. Current buckets: 50 agree-accept, 2 agree-G2, 1
agree-G4, 36 formal-strict (formal clean, checker refuses for reasons
outside the modeled fragment), 5 missed-G4 (checker G4-refuses where the
shaped model sees no violation), 12 formal-found-other, 21 out-of-
fragment. The five missed-G4 files are known model-coverage gaps
(`examples/rejections/g4_capability_not_declared`,
`g4_emission_not_declared`, `g4_spawn_widens_capability`,
`g4_spawn_widens_parameter`, `g4_unmarked_handle_emission`) — they need
capability scope and spawn/wrapper reachability the shaped model does
not yet express.

## TODO (in dependency order)

1. **Close the modeled G4 gaps**: extend the export's call facts with the
   `g4_*_rejection` shapes (spawn-widened capability/parameter, handle
   emissions, undeclared-sink refusals) so the shaped G4 stops missing
   the five `missed-G4` files. Needs a reachability model for
   arrow/spawn bodies in the export, not a checker change.
2. **Capability ceilings/budgets** (2.0 features): parameterized
   capabilities over the `Ctx` model. **Mostly done.** The `(T,P)`
   algebra of `src/revl/cap_order.py` is modelled in the L1 farm
   `RevL.Lemmas.CapLemmas` (token + valuation, the component-wise path
   order, discrete resource values, numeric ceilings, the
   resource/ceiling split), and `RevL.Theorems.CapCeilings` proves the
   nine rows above: the order is a partial order, attenuation is monotone
   downward along a lineage of admitted spawns, budgets only shrink, the
   runtime counter is not overdrawn, and the whole thing composes with
   G6's confinement through the key-to-token bridge (`capKeys`).
   **What is left**: the model takes a component's *held* and *reached*
   capability sets as given, where `lower.py` derives them from the
   syntax (`_collect_emit_caps`, `_held_capabilities_pairs`,
   `_spawn_surface_closure`). Deriving them inside the L0 statement
   fragment needs the same spawn/arrow reachability TODO 1 needs, so the
   two should land together. Also not modelled: parse-time
   canonicalization (upstream of the order), and `cap_order.disjoint`'s
   deferred (D2) same-token clause.
3. **L3, deliberately deferred**: `Trusted[T]`/`Secret[T]`
   non-interference and WAL commit/abort discharge. Both extend L0 (taint
   is a checker feature, not part of the current core; commit/abort is a
   runtime state-machine refinement). Not near-term work.

## Conventions for worker sessions

- State the theorem first with `sorry`, register it in `CheckAxioms.lean`
  and in this file as *stuck/TODO*, then fill it. The gate stays honest
  because `sorryAx` fails the build — a red build on a stated theorem is
  the system working, not a regression.
- Porting map: DESIGN.md §4 row → paper object → `src/revl/lower.py`
  (checker side) → the `tests/` file that currently witnesses the
  guarantee by execution. The test is the *example suite* for the formal
  model, not a substitute for it.

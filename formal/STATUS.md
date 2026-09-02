# revl formal backbone — proof status

Rules of the layering (enforced by imports, not by hope):

- **L0** (`RevL.Syntax`, `RevL.Typing`, `RevL.Semantics`,
  `RevL.Manifest`, `RevL.Boundary` — the last two say so in their own
  headers) is architect-owned and frozen. Worker sessions do not edit it;
  a needed L0 change blocks on the architect.
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
| `RevL.G1.declared_only_access` | G1 — declared access (component level) | **proved** | `propext, Quot.sound` | undeclared access cannot be written |
| `RevL.G2.linkOK_provision_disjoint` | G2 — provision disjointness (Def. 43) | **proved** | `propext, Quot.sound` | from the incremental `LinkOK` judgment; the unit is the `(key, realm)` slot |
| `RevL.G2.linkOK_requires_closed` | G2/G1 — requirement closure | **proved** | `propext` | every consumed slot provided in-composition |
| `RevL.G2.realm_separation_admitted` | G2 — non-vacuity | **proved** | `propext, Quot.sound` | `examples/tenants.rvl`: one key, two realms, links |
| `RevL.G2.same_realm_conflict_refused` | G2 — non-vacuity | **proved** | `propext, Quot.sound` | drop the realms and the same pair cannot link |
| `RevL.G3.depPath_rank_lt` | G3 — cycles rejected (§6.5) | **proved** | none | ranks strictly decrease along dep paths |
| `RevL.G3.no_dependency_cycles` | G3 | **proved** | none | a layering certificate excludes cycles |
| `RevL.G3.linkOK_layeredBy_rankOf` | G3 — the layering construction | **proved** | `propext, Quot.sound` | the admission order is a layering: `rankOf` |
| `RevL.G3.linkOK_layered` | G3 — the bridge | **proved** | `propext, Quot.sound` | `LinkOK comps → ∃ rank, LayeredBy comps rank` |
| `RevL.G3.linkOK_no_cycles` | G3 — as the linker states it | **proved** | `propext, Quot.sound` | admitted composition ⇒ no cycle, nothing assumed |
| `RevL.G3.self_provision_refused` | G3 — non-vacuity | **proved** | `propext` | `Ouroboros` (requires a key it provides) cannot link |
| `RevL.G3.mutual_cycle_refused` | G3 — non-vacuity | **proved** | `propext` | `g3_dependency_cycle.rvl` refused in both orderings |
| `RevL.G3.layering_exists_for_admitted` | G3 — non-vacuity | **proved** | `propext, Quot.sound` | the certificate is reachable, not just refutable |
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
| `RevL.CapCeilings.derived_held_tokens_are_declared_keys` | TODO 2(a) — the `capKeys` bridge | **proved** | `propext, Quot.sound` | derived held tokens are exactly the declared wiring keys |
| `RevL.CapCeilings.derived_reach_is_emit_surface` | TODO 2(a) — `_collect_emit_caps_pairs` | **proved** | `propext, Quot.sound` | only `emit` contributes; an `emit` contributes its key's cone |
| `RevL.CapCeilings.unnameable_receiver_is_star` | TODO 2(a) — the named residue | **proved** | `propext` | handle / head-less receivers derive exactly `[*]` |
| `RevL.CapCeilings.derived_lineage` | TODO 2(a) — text to `Lineage` | **proved** | `propext` | an admitted activation spawn edge is a lineage edge |
| `RevL.CapCeilings.derived_attenuation_monotone` | TODO 2(a) — items 66/294 | **proved** | `propext, Quot.sound` | `attenuation_monotone` over derived sets; the closure carries the subtree |
| `RevL.CapCeilings.derived_lineage_ceiling_le` | TODO 2(a) — item 260 | **proved** | `propext, Quot.sound` | `lineage_ceiling_le` with its `Lineage` hypothesis discharged |
| `RevL.CapCeilings.derived_budget_never_exceeds_root_ceiling` | TODO 2(a) — item 260 | **proved** | `propext, Quot.sound` | the end-to-end budget claim, rooted in a component shape |
| `RevL.CapCeilings.derived_confinement_within_ceiling` | TODO 2(a) + G6 | **proved** | `propext, Quot.sound` | `TypedIn (capKeys Γ)` discharged from `TypedIn (reqKeys c)` |
| `RevL.CapCeilings.derived_no_star_amplification` | TODO 2(a) — item 66 | **proved** | `propext, Quot.sound` | the `*`-free side condition is itself derived |
| `RevL.CapCeilings.derivation_non_vacuous` | TODO 2(a) — non-vacuity | **proved** | `propext, Classical.choice, Quot.sound` | derived sets carry valuations; `g4_spawn_widens_parameter` refused from the text |
| `RevL.CapCeilings.derivation_refuses_unnameable` | TODO 2(a) — non-vacuity | **proved** | `propext, Classical.choice, Quot.sound` | a handle emission derives `*` and is not folded into the held key |
| `RevL.CapCeilings.derived_ceiling_check_not_subsumed` | TODO 2(a) — non-vacuity | **proved** | `propext, Classical.choice, Quot.sound` | both relations still load-bearing once the sets are derived |
| `RevL.G9.origin_persists_or_is_declassified` | G9 (item 249) — the core lemma | **proved** | `propext` | an origin survives a flow, or a declassifier on that path cleared it |
| `RevL.G9.no_authority_from_untrusted` | G9 — untrusted data gains no authority | **proved** | `propext` | a `Trusted[T]` sink admits a tainted value only after an explicit declassification of that origin |
| `RevL.G9.untrusted_gains_no_authority` | G9 — the refusal | **proved** | `propext` | a declassifier-free path cannot reach an authority sink |
| `RevL.G9.declassification_is_the_only_escape` | G9 (item 249, Decision 2) | **proved** | `propext` | the label is monotone along every non-declassifying step |
| `RevL.G9.secret_persists` | item 256 §4a.3 | **proved** | `propext` | a bound provider key has no declassifier, so it survives every admitted path |
| `RevL.G9.secret_confined` | item 256 — secret confinement | **proved** | `propext` | a bound key reaches no sink, `Secret[T]` receivers included (the A8 disjointness) |
| `RevL.G9.confidential_needs_declassification` | item 256 Slice 3 §7 | **proved** | `propext` | a `Secret[T]` value reaches a disclosure sink only through a declared `endorse[confidential]` |
| `RevL.G9.flow_declassifiers_granted` | item 249 Slice C | **proved** | `propext` | under `no_declassify` every declassifier on an admitted path is pre-granted |
| `RevL.G9.untrusted_author_needs_granted_declassifier` | items 249/329 | **proved** | `propext` | a self-minted declassifier does not count for a model-authored turn |
| `RevL.G9.declassifier_must_be_declared` | item 249 Slice C | **proved** | none | an undeclared `endorse[o]`, and `endorse[secret]` at all, are refused |
| `RevL.G9.taint_surface_within_declared_context` | G9 + G6 | **proved** | `propext, Classical.choice, Quot.sound` | origins are derived from the declared capability scope; reach bounds them |
| `RevL.G9.no_untrusted_without_a_declared_source` | G9 + G6 | **proved** | `propext, Classical.choice, Quot.sound` | with no untrusted-scoped requirement, untrusted data is unwritable |
| `RevL.G9.g9_not_vacuous` | G9 — non-vacuity | **proved** | `propext, Classical.choice, Quot.sound` | admits the endorsed path, refuses the bare one and the foreign-origin endorse |
| `RevL.G9.secret_rules_not_vacuous` | item 256 — non-vacuity | **proved** | `propext` | `confidential` downgrades, `secret` never does, self-minted flips to refused |
| `RevL.G9.authority_refusal_is_not_universal` | G9 — anti-tautology (item 418) | **proved** | `propext` | one flow, admitted at a disclosure sink and refused at an authority sink |
| `RevL.G9.sink_rules_are_distinct` | G9 — anti-tautology (item 418) | **proved** | `propext` | the four `Admits` rules separated pairwise; not one predicate four times |
| `RevL.G9.secret_refusal_is_load_bearing` | item 256 — anti-tautology (418) | **proved** | `propext` | the algebra CAN clear a `secret`; `taint.py`'s two refusals are what stop it |
| **G9 — path coverage** | G9 — the attested `G1..G9` set | **UNPROVED, unstatable** | — | see *G9* below: the checker must WALK every path; not expressible against the current L0 |

(`propext` / `Quot.sound` are Lean's standard foundation axioms; the gate
whitelists exactly those three.)

### G2/G3 restated over `(key, realm)` slots (roadmap item 418, step 1)

`RevL.Manifest` used to model G2 as `Nodup (flatMap provides)` over bare
keys and let a component satisfy its own requirement. Both were wrong
against the compiler:

- revl's G2 is per `(key, realm)` — `diagnostics.GUARANTEES["G2"]` reads
  "one provider per key (per realm)" and the linker's `provider_of` table
  is keyed on the pair, with the realm read from the component's
  `isolate` clauses. The model refused `examples/tenants.rvl`, which the
  compiler accepts and whose own header states the real rule. `LComponent`
  now carries a `realm : String → String` field (defaulting to
  `sharedRealm`, so a realm-free component literal is unchanged), and
  `slots`/`needs`/`DependsOn`/`DepPath`/`LayeredBy`/`ProvidesDisjoint`/
  `RequiresClosed`/`LinkOK` are all stated over slots. Lifting the
  *dependency* relation too is forced, not cosmetic: with two realms of
  one key, no key-indexed rank function can be a layering.
- `LinkOK` now requires each component's consumed slots to be provided
  **strictly deeper** in the list, not in `c :: comps`. That is the
  linker's "component N requires a key it provides itself (`k`) (G3)"
  refusal, and transitively its cycle refusal: `LinkOK comps` says
  `comps` is a valid reverse-`loadOrder` presentation, and a program
  links iff *some* ordering derives it.

The point of the second change is `RevL.G3.linkOK_layered`, the bridge
`LinkOK comps → ∃ rank, LayeredBy comps rank`. Before it,
`no_dependency_cycles`' layering hypothesis was not establishable from
anything the model admitted, so "cycles rejected" had no proof path;
`RevL.G3.linkOK_no_cycles` now states G3 with no layering assumed.

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

## G9 — untrusted data gains no authority (`RevL.Theorems.G9_NoAuthorityFromUntrusted`)

`src/revl/taint.py`, first line: "untrusted input cannot DIRECTLY create
authority." The model: a `Label` is a set of `Origin`s (the closed class
set `taint._ORIGIN_CLASSES`, plus `Origin.custom` for `_origin_of`'s
unclassified-scope residual), ordered by inclusion with bottom `{}` =
trusted and join = union. A `Flow` is one data-flow path as a list of
`Step`s: a crossing minting its *derived* origin, a join, an opaque hop,
and the one weakening step — an admitted `Declassifier`. Every label
operation the reference performs is one of those four.

Two declassifiers ship and both are modelled: the scoped
`endorse[<origin>](v, reason = "...")`, which clears **only** its own
declared origin (item 249, Finding 1) and only where the enclosing
declaration granted the slot; and the checked parser (a `verified fn`
returning `Trusted[T]`). The ambient originless `endorse(v)` is a *parse
error* in the reference (`parser._endorse_expr`), so it is deliberately
not a constructor — every declassification in a parseable program is
scoped and reasoned. `endorse[secret]` is refused before the
declared-slot check and a checked parser is refused on a
`secret`-carrying value, which together are why `secret_persists` holds
with no side conditions: a capability-bound provider key has no
declassifier anywhere in the language.

Four sink rules, kept distinct because they are genuinely different
predicates: `authority` (a `Trusted[T]` parameter, or a
shell/exec/terminal/policy scope under `taint_strict`) refuses any dirty
label; `disclosure` (an emission crossing, a plain extern call, a
provide-method return) refuses `secret` and `confidential`;
`secretReceiver` (a declared `Secret[T]` position) admits `confidential`
but still refuses `secret` — the A8 / CRITICAL 1 disjointness;
`unnameable` refuses everything.

Non-vacuity, per the `CrossTier.annotation_necessary` convention:
`g9_not_vacuous` computes `mintedBy ["web.fetch"] = web` from the real
scope string, admits the path carrying a declared `endorse[web]`, refuses
the same path without it, and refuses it again when the endorse names a
*foreign* origin. `secret_rules_not_vacuous` admits an
`endorse[confidential]` downgrade, refuses `secret` at every sink over
every path, and flips the same declared `endorse[web]` from admitted to
refused purely because the untrusted author minted it itself.

### Non-vacuity, against item 418's bar

Item 418's adversarial review found G4/G5/G6/G8 to be tautologies over a
chosen inductive (the identical statements hold of a typing relation
admitting nothing) and only 3 of 25 theorems to carry non-vacuity
evidence. G9 carries four guards, each a registered theorem:
`g9_not_vacuous` and `secret_rules_not_vacuous` exhibit **inhabited**
flows; `authority_refusal_is_not_universal` exhibits ONE declassifier-free
flow that a disclosure sink admits and an authority sink refuses, so the
conclusion turns on the sink rule rather than refusing everything;
`sink_rules_are_distinct` separates all four `Admits` rules pairwise (the
antidote to "G1 and G6 are literally the same theorem"); and
`secret_refusal_is_load_bearing` shows the algebra **can** clear a
`secret` — the same declassifier forms clear every other origin — so
`secret_persists` holds because of `taint.py`'s two explicit refusals,
not because the datatype lacks a constructor. Delete either refusal from
`kindOK` and the theorem becomes false.

### What G9 does NOT cover — the open obligation

**Path coverage is not proved, and it is where the real bugs are.**
`Flow` starts from a path that is *given*. That the checker *walks* every
path a program contains is a separate obligation, and it is the one that
actually broke: `taint._walk_component_methods` descends only into
`provide` steps, so a component's **activation body was never
taint-checked at all**, and a `Secret[T]` parameter was stripped inside
its own receiver body (both fixed on
`fix/taint-activation-body-and-secret-receiver`). Nothing in this file
would have caught either.

That obligation cannot be *stated* against the current L0, let alone
proved: L0 has no component bodies, no `provide`/activation distinction,
and no typed parameters — the exact structure both bugs live in. It is
therefore carried as the **UNPROVED, unstatable** row in the table above
rather than as a `sorry` on a statement that does not typecheck, and it
is why `attest.py:117-118`'s `G1..G9` claim is only partly backed for G9:
the flow *rule* is proved, the *coverage* of the walk is not. Closing it
needs L0 to grow component bodies with the provide/activation
distinction and typed parameters — item 418's ordered exit puts G9 at
step 9, after an operational semantics exists, and this is exactly why.

Also out of scope, documented rather than smuggled as axioms: the runtime
tag (Slice B, item 243) — nothing here claims a runtime property; the
interprocedural fixed point (`_Signature`, `_infer_signatures`) that
discovers *which* paths exist, the model proving what follows once a path
is exhibited; and that the checker labels the right positions as sinks,
which is extraction, not theorem. The origin half of that labelling **is**
proved: `taint_surface_within_declared_context` composes with G6 to show
every origin a statement can mint is one its declared context already
declares. No differential-oracle row references any of these definitions
— the oracle carries no taint verdicts, so item 418's C4 applies here
too: G9 is not covered by that gate.


## Differential oracle (two re-statements, not the model)

`harness/diff_corpus.py` + `harness/Oracle.lean`: parse every corpus
`.rvl` with revl's real parser, export one TSV row per *fact*, then run
`harness/Oracle.lean` over the same TSV and diff its verdicts against a
plain-Python reference of the same semantics. A mismatch fails
`make formal`.

**What this checks, and what it does not (roadmap item 418, C4).** The
oracle is *not* wired to the proved model. `Oracle.lean` imports
`RevL.Manifest` but uses no definition from it: every verdict comes from
its own unproved Lean (`disjointOK`, `closedOK`, `g4OK`, `capCovers`,
`attenOK`), and `diff_corpus.reference_from_tsv` is a third independent
re-implementation rather than a call into `src/revl/cap_order.py`. So the
harness compares two hand-written re-statements of the rules against each
other. That catches an extraction or transcription drift between them; it
cannot catch a definitional error in L0. Step 1 of item 418 demonstrated
this by accident: `ProvidesDisjoint` and `LinkOK` were restated over
`(key, realm)` slots — a change to what the model *decides* — and the
oracle's output was bit-identical, 343 of 343 agreeing before and after.
Making the verdicts compute from `RevL.Manifest` and the proved `Covers`,
and the Python reference call `src/revl/cap_order.py`, is item 418 step 6.
Until then, "the differential oracle agrees" is a claim about the
harness, not about the theorems.

Facts exported: component manifests (M), require-binding resolutions (R),
provide-key resolutions (C), per-statement classifications (T), call
facts with marker context (U), service-method emission bounds (B) and a
scoped bound's declared entries (Q), and the **reachability** facts that
let the model see past the marker — the capabilities a component's
`requires` bindings grant it (K), its activation emit-step surface (A),
the emission capabilities a provide method's body crosses (F), the
activation-body spawn edges (S), and the spawn handles (H) through which
`w.task.run(...)` resolves to the child's provision.

Verdicts:

- **V rows (per file, G2/G3)**: provision disjointness + requirement
  closure. Two rules here are known to be wrong, and are step 6's work
  rather than descriptions of revl. Disjointness is computed over bare
  keys, where revl's G2 is per `(key, realm)` — `RevL.Manifest` now
  models the real rule, but the exporter's `M` row carries no realm
  column, so the oracle cannot yet see one. Closure is computed *within
  a single file*, where revl closes requirements over the linked
  composition. Between them these two account for every one of the 36
  `formal-strict` files below.
- **G rows (per component, G4-shaped)**: marker presence must equal the
  interface's declaration — a plain call to a declared emission method,
  or an `emit`'d call to a non-emission method, is refused. Receivers
  include spawn handles, not just `requires` bindings.
- **P rows (per provide method, G4)**: a service declaration is an upper
  bound on its providers. The method's reached capabilities must sit
  inside its declared bound — `plain` admits none, bare `emission` admits
  any, `emission[...]` admits exactly the declared entries.
- **W rows (per activation spawn edge, G4/item 66/294)**: a spawned
  child's transitively closed reach must be covered by the spawner's held
  capabilities, comparing canonical `(token, params)` capabilities so a
  child under `fs(path="/etc")` is refused beneath a parent holding
  `fs(path="/tmp")`. Activation-body spawns only, matching
  `lower._activation_spawn_sites`: a provide-method spawn is already
  bounded by that method's `emission[...]` clause.

Current status over the corpus: **292 files → 182 components → 403
statements → 130 manifest-bearing files → 343 verdicts compared
(130 files + 182 components + 25 provide methods + 6 spawn edges), 343
agree, 0 mismatches** (28 parse-error skips, loud). A mismatch is drift
between the oracle and the reference — read the paragraph above for what
that does and does not cover.

Checker alignment (informational, not a gate — promoting
`formal-strict` / `missed-G4` / `missed-G2` to gate failures is item 418
step 7): each parsed file is also compiled with the real checker and its
refusal code is compared against the formal verdicts. Current buckets: 52
agree-accept, 2 agree-G2, 6 agree-G4, 36 formal-strict (the checker
*accepts* the file but the oracle does not), 13 formal-found-other, 21
out-of-fragment. **0 missed-G4**: the five
previously missed files are now modeled, each by the verdict row its
rejection comment names —
`g4_emission_not_declared` and `g4_capability_not_declared` by a P row
(provider exceeds its declared bound), `g4_spawn_widens_capability` and
`g4_spawn_widens_parameter` by a W row (spawn widens authority), and
`g4_unmarked_handle_emission` by a G row (unmarked crossing through a
spawn handle). No other bucket changed membership, so nothing was
manufactured: the only files that moved are the five.

Known fidelity limits of the shaped model, deliberately not papered over:

- An emission reached through a spawn handle, an emission extern, or a
  transitively-emitting named function contributes the unnameable `*`
  capability rather than a resolved boundary. That mirrors the checker's
  own `*`, but it is coarse: `*` is covered only by `*`.
- Capability **ceilings** are not modeled. The checker runs the crossing-
  coverage fold ceiling-blind (`_strip_ceilings`) and checks budgets
  separately; the model compares whole canonical capabilities. The corpus
  carries no integer-valued capability parameter today, so the two agree
  on it vacuously — this is TODO 2's work, not a live divergence.
- The 36 `formal-strict` files are **not** "the model being stricter than
  the fragment it covers". Every one of them is one of the two wrong
  rules in the oracle's V row, and the split is exactly: **32** files
  `V(ok, fail)` from the intra-file closure rule; **3** files
  `V(fail, ok)` from the bare-key disjointness rule
  (`examples/tenants.rvl`, `tests/fixtures/canary_tenants.rvl`,
  `tests/fixtures/erase_realms.rvl`); and **1**,
  `examples/tenant_attenuation.rvl`, tripping both. No file in the bucket
  is refused by a G, P or W row. Fixing the two rules in the oracle
  (step 6) should empty the bucket. The L0 side of the realm rule is
  already fixed — `RevL.Manifest.ProvidesDisjoint` admits all four of
  those files — but that alone moves nothing here, because the oracle
  does not read `RevL.Manifest` and the exporter emits no realm.

## TODO (in dependency order)

1. ~~**Close the modeled G4 gaps**~~ — **done**. The export now carries a
   reachability model for provide-method and spawn bodies (B/Q/C/K/A/F/S/H
   facts) and the oracle grew the P and W verdicts over it; the
   `missed-G4` bucket is empty and all five files are modeled by the rule
   their rejection comment names. No checker change: `src/revl/` is
   untouched. Residue, tracked above under *fidelity limits*: unnameable
   `*` for handle/extern-reached emissions, and ceilings deferred to 2.
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
   **What is left**, two halves, of which (a) has landed:
   (a) ~~*theorem side*~~ — **done**. `held` and `reach` are now
   FUNCTIONS of a component shape, not given lists:
   `stmtCaps`/`bodyReach` track `_collect_emit_caps_pairs` (emit steps
   only; a `req` receiver resolves to its wiring key's cone through
   `_cap_keyed`, anything else to `*`), `heldCaps` tracks
   `_held_capabilities_pairs`, `reachIn` tracks `_spawn_surface_closure`
   as a fuel-indexed unfolding, and `SpawnsAdmitted` tracks
   `_check_spawn_attenuation` over activation-body spawns only
   (`_activation_spawn_sites`). The `capKeys` bridge stops being an
   assumption: `derived_held_tokens_are_declared_keys` proves the derived
   held set's tokens are exactly the component's declared `requires`
   keys. Five of the nine theorems are re-stated with their `Lineage` (and
   for confinement, `TypedIn (capKeys Γ)`) hypotheses discharged from the
   program text — `derived_attenuation_monotone`,
   `derived_lineage_ceiling_le`,
   `derived_budget_never_exceeds_root_ceiling`,
   `derived_confinement_within_ceiling`,
   `derived_no_star_amplification`. The other four
   (`cap_order_partial`, `spend_within_budget`,
   `parameter_widening_refused`, `ceiling_check_not_subsumed`) never had a
   held/reached hypothesis to discharge; the last of them gains a derived
   twin anyway (`derived_ceiling_check_not_subsumed`).

   Named residue, stated rather than assumed away. L0 has no constructor
   for a spawn handle, an emission extern, a named function, a spawn
   site, or a manifest, so `Comp` carries `handles`, `spawns` and
   `requires` alongside the `body` (the reference reads the last two from
   the spawn registry and the manifest too), and an `emit` with no call
   head is the fragment's stand-in for the extern / emitting-function
   receivers. Every emission through one of those derives the unnameable
   `*`, exactly as the checker's `else caps.add("*")` does;
   `NameableEmission` is the hypothesis this forces, carried explicitly on
   `derived_no_star_amplification` and on half of
   `derived_confinement_within_ceiling`, and
   `derivation_refuses_unnameable` is the concrete price of dropping it.
   One precision loss: an L0 call head is the receiver ROOT, so a key's
   cone unions over the service's emission methods where the reference
   picks the method being called — the derived gate is therefore at least
   as strict as the checker, never looser. Still not modelled: parse-time
   canonicalization (upstream of the order), and `cap_order.disjoint`'s
   deferred (D2) same-token clause.
   (b) *oracle side* — teach the oracle the checker's ceiling-blind
   coverage fold plus its separate budget attenuation, so an
   integer-valued capability parameter is compared the way the checker
   compares it. The corpus has no such parameter today, so the two agree
   vacuously; this closes that gap rather than resting on it.
3. **L3**: `Trusted[T]`/`Secret[T]` non-interference — **done**, see
   *G9* below — and WAL commit/abort discharge, still deferred (a runtime
   state-machine refinement). The taint half turned out **not** to need an
   L0 change: taint is a property of a value flowing along a path, L0's
   `Ctx`/`ReachIn` is a property of which keys a statement touches, and
   the two meet at one bridge (`taint._origin_of`: the origin a crossing
   mints is read off its declared capability scope). So the label algebra
   went into the new L1 farm `RevL.Lemmas.TaintLemmas` and the guarantees
   into the new L2 file `RevL.Theorems.G9_NoAuthorityFromUntrusted`,
   exactly as items 294/66/260 went into `CapLemmas` + `CapCeilings`.
   **L0 is untouched and every pre-existing theorem's axiom set is
   unchanged.** What is proved is the flow *rule*; what remains open is
   the *coverage* of the checker's walk, which needs the L0 growth item
   418's ordered exit schedules ahead of it — see the G9 section for the
   named obligation and why it is not statable today.

## Conventions for worker sessions

- State the theorem first with `sorry`, register it in `CheckAxioms.lean`
  and in this file as *stuck/TODO*, then fill it. The gate stays honest
  because `sorryAx` fails the build — a red build on a stated theorem is
  the system working, not a regression.
- Porting map: DESIGN.md §4 row → paper object → `src/revl/lower.py`
  (checker side) → the `tests/` file that currently witnesses the
  guarantee by execution. The test is the *example suite* for the formal
  model, not a substitute for it.

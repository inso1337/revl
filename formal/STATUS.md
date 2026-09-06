# revl formal backbone — proof status

Rules of the layering. Roadmap item 418 recorded that this line used to
read "enforced by imports, not by hope" while nothing checked it;
`scripts/layering_gate.py` checks it now, and runs as the first step of
`scripts/run_gate.sh`:

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
- A theorem also needs a row in `scripts/nonvacuity.tsv` naming the
  concrete evidence that its hypotheses can all hold at once (roadmap item
  418, step 8). The axioms gate cannot tell a load-bearing theorem from a
  vacuous one: `#print axioms` is just as clean on a theorem whose
  hypotheses are unsatisfiable. `scripts/nonvacuity_gate.py` fails on a
  registered theorem with no row, on a witness that is not itself
  registered, on a self-witnessing row, and on a registered theorem this
  file does not name — so a proof and the record of it cannot drift
  apart. Two rows are marked `contentless` rather than witnessed,
  because they are true by definition
  rather than by any property of their subject; the gate prints both on
  every run so they cannot be quietly counted as proof.

## What the layer covers, and what it does not

The honest map, per guarantee code (`src/revl/diagnostics.py`'s
`GUARANTEES`). The table below the map lists every proved theorem; this is
the part a reader wants first, because a guarantee with no row here is a
guarantee this layer buys nothing for. "Oracle" is whether the
differential harness (`harness/diff_corpus.py`) compares the model's
verdict against the shipped checker on the corpus — a proved theorem with
no oracle row is checked against the *paper*, not against `src/revl`.

| Code | Formal status | Theorems | Oracle | The gap, and what kind of gap it is |
|---|---|---|---|---|
| **G1** declared access | partial | 2 | no | `declared_only_access` is real and witnessed, but its content is the shape of `Typed`/`ReachIn`: it says an undeclared access cannot be *written*, not that the checker *visits* every statement of a real component body. **Modelling limit** — L0 has no component bodies |
| **G2** provision disjointness | full | 4 | **yes** (V rows, 2 agree-G2) | stated over `(key, realm)` slots from the incremental `LinkOK`, and the oracle bites: change `Manifest.needs` to ignore the realm and four corpus files mismatch |
| **G3** acyclic dependencies | full | 9 | **yes** (V rows, 1 agree-G3) | the layering certificate is *derived* from `LinkOK`, so nothing is assumed. No known gap |
| **G4** inverse-or-emit | full over the lattice; the shape-level statement is weak and marked | 2 + 7 | **yes** (182 G rows, 25 P rows, 6 agree-G4) | `G4.inverse_or_emit` is shape-level and superseded. For the lattice form: the reach fold's **fuel bound** is real and named (`fold_must_run_to_stability`), `FnDecl.calls` stands in for `_calls_in` (an empirical obligation on the lowering), first-class dispatch is `*`, and `inverseOK` reads `undo` only where the reference walks `compensate` too. **Unbuilt work**, not modelling limits |
| **G5** teardown registers nothing | full over the lattice; the shape-level statement is **contentless** and registered as a finding | 2 + 15 | **yes** (209 U5 rows) | `G5.teardown_registers_nothing` is true by definition (`registrations` is constant zero) and says so in the registry. `G5Classified` carries the real count, including two operational runs. The oracle now reconstructs the file's `RevL.Lemmas.Prog` from the `EX`/`FN`/`PG` rows and decides `registrations` over each effect's inverse body (`Oracle.registrationsB`, `registrationsB_iff`); the reference recomputes the same reach fold independently from the TSV, and the two agree on all 209 teardowns. `prog_coverage` fails the gate unless the corpus carries a clean teardown (count 0) AND a caught crossing (`examples/rejections/g5_undo_fn_emission.rvl`, an `undo` reaching an emission through a `fn`), and `g5_row_not_vacuous` proves the count flips to 0 when the wrapping fn stops calling the emission. First-class dispatch (`star`) is `n/a` on both sides, outside the model as in G4 |
| **G6** purity outside effect forms | full at head granularity; the shape-level statement is the content of `TypedIn`/`ReachIn` | 3 | **yes** (596 C rows) | the row reconstructs each lowered statement from its exported heads (`Oracle.exprOfHeads`, proved non-lossy by `heads_exprOfHeads`) and decides `∀ k ∈ stmtHeads s, k ∈ C` with `confinedB` (`confinedB_iff`), against a declared context of the component's require locals (M) plus its require-held binding roots (K). The reference computes the same head-roots membership independently from the TSV, and the two agree on all 596 statements. A leak is a `fail` on both sides, so the row bites without an admitted violation to point at (the checker refuses those at parse); `confinement_coverage` fails the gate unless the corpus carries both a confined statement over a non-empty reach and a caught violation (281 today), and `g6_row_not_vacuous` proves the verdict flips when a leaking head is accepted. Still not under the row: the derived form (reach computed from program text) lives only in `CapCeilings.derived_confinement_within_ceiling`, and host builtins and let-bound locals count as reach, so a component using them is a faithful `fail` rather than a claim it is unsafe |
| **G7** derived LIFO teardown | full for *which* entries run, in *what order*, under *which verdict* — including the E-Stop | 32 + 7 | **yes** (267 D rows) | the row RUNS `backends/python/runtime.py` over an enumerated scenario corpus and diffs the reference's observed disposition against the model's predicted one, with a coverage ratchet (`teardown_coverage`) that fails the gate if the corpus stops distinguishing LIFO from FIFO, Phase 2 from Phase 1, or the three dispositions from one another. Still deliberately not modelled, and so not under the row: Phase-1 continue-and-record and its residue severities, the Phase-2 budget, escrow under a pending session verdict (item 245), cascading abort. This model says which entries run, **not what happens when one of them fails**. The cordis LIFO unwind of the activation-body stack is supplied by the harness, not observed — only `drain`'s own `reversed` loop (item 369) is revl's own ordering code. **Modelling limit, scoped on purpose** |
| **G8** boundary enumerable | full over the lattice; the marker-level statement is weak and marked | 3 + 9 | **yes** (598 S8 rows) | `G8.boundary_only_declared` rests on `boundaryOf (.effect _ _) = []` **by definition**. The lattice form drops the typing hypothesis entirely. The oracle now decides `RevL.G8Classified.stmtSurface` over each reconstructed statement's heads against the file's `Prog` (`Oracle.stmtSurfaceB`, `stmtSurfaceB_iff`); the reference recomputes the same reach caps independently from the `EX`/`FN` rows, and the two agree on all 598 surfaces. `prog_coverage` fails the gate unless the corpus carries both a non-empty and an empty surface, and `g8_row_not_vacuous` proves the surface goes empty when the wrapping fn stops reaching the crossing. First-class dispatch (`star`) is `n/a` on both sides |
| **G9** no authority from untrusted | rule proved; **coverage unproved and unstatable** | 18 | no | `Flow` starts from a path that is *given*. That the checker WALKS every path is where the real bugs were (`_walk_component_methods` skipped activation bodies entirely). **Modelling limit**: L0 has no component bodies, no provide/activation distinction and no typed parameters, so the obligation cannot be stated, let alone proved. Carried as the one **UNPROVED** row in the table |
| **G-SECRET / G-SECRET-FLOW** | partial, inside the G9 development | (within the 18) | no | `secret_persists`, `secret_confined` and `confidential_needs_declassification` prove the *rule*; they inherit G9's coverage gap exactly |
| **A1** iteration boundaries only during activation | **none** | 0 | no | not modelled at all: L0 has no `await` and no iteration boundary. **Unbuilt work**, and it needs L0 to grow first |
| **A2** no acquisition after a provision | **none** | 0 | no | L0 bodies carry no acquisition/provision ordering. **Unbuilt work** |
| **A3** host-safe identifiers | **none** | 0 | no | lexical, checked by extraction rather than by a theorem shape. **Out of scope by kind** |
| **A5** compensation accompanies an emission | **none** | 0 | no | G7 *models* the `compensation` entry kind and proves how it is disposed, but **nothing states that an emission must register one**. **Unbuilt work**, and the nearest thing to a surprise on this map |
| **A6** provide-methods match the service signature | **none** | 0 | partial | the oracle's P row is a *capability bound* check, not the signature match, and `methodBoundOK` is a private restatement. **Unbuilt work** |
| **A8** mid-body failure reverts and contains | full over the WAL model | 18 | **yes** (1620 O rows) | the row WRITES each scenario's records as a real JSON-Lines WAL and runs `src/revl/recovery.py` over it, diffing recover's own verdict, the set it actually applied to the `World`, and its reported residue against the model's `outcome` / `replayed` / `reported`. It found the legacy-`effect` family's item-309 fence branch missing from `RevL.Lemmas.dispose` (see below). Crash cuts covered: fence-to-apply, abort-then-crash, the approved-to-discharged window. **Not covered and not claimed**: a crash between a witnessed mutation and its record (the reference logs the descriptor *after* the forward extern returns), the roll-forward `flush-residue` surface, cascading abort, escrow. Durability is a floor, not a theorem |
| **A9** provide key declared in `provides` | **none** | 0 | no | **Unbuilt work** |
| **T1/T2/T3** typing, `Opt[T]`, holes | **none** | 0 | no | the type checker is outside the guarantee backbone. **Out of scope by kind** |
| **R4** no residue | full for the **abort path** | 9 | **yes** (the residue column of the 1620 O rows) | the column is the model's `reported`, diffed against `recover`'s `residue.outstanding`; it is printed only under `outcome = rolledBack`, which is R4's own scope condition. Stated over the abort; the roll-forward window's `flush-residue` surface is still not modelled and the column says `n/a` there rather than agreeing about a claim neither side makes. **Unbuilt work** |
| items 66/294/260 capability ceilings | full, with the held/reach sets **derived** from component shapes | 23 | **yes** (8 W rows) | the ceiling half is now EXERCISED: `examples/budget_attenuation.rvl` (50 ≤ 100, admitted) and `examples/rejections/g4_spawn_widens_budget.rvl` (1000 > 100, refused by the ceiling half alone — strip the ceilings and the resource fold finds nothing uncovered), with `attenuation_coverage` failing the gate if the corpus stops containing both. Before those two files the row agreed over 6 edges with `ceilingOKB` never entered. Also unmodelled: parse-time canonicalization and `cap_order.disjoint`'s D2 same-token clause |
| item 133 cross-tier agreement | full, under two named hypotheses | 4 | no | that the real emitters realise a `Conformant` profile is the differential conformance matrix's empirical obligation; map values are one level deep |

Three summary readings of that map:

- **The oracle reaches G2, G3, G4, G7, A8, R4 and the capability order.**
  G5, G6, G8 and G9 are still proved against the design documents and the
  reference source read by hand. That remains the largest gap in the
  layer, and it is a gap in *coverage of the gate*, not in the proofs. The
  four that are left are left for a reason and the reason is the same one:
  each is indexed by `RevL.Syntax.Stmt` (or, for G9, by a `Flow` over a
  path), and the export carries FACTS — call sites, capabilities,
  manifests — not statement terms, so there is no shape for a corpus row
  to take. Closing them is an export change, not an oracle change.
  Two rows now *execute* rather than read, because their subject is a run
  and not a text: G7 drives `backends/python/runtime.py` over an
  enumerated teardown corpus, and A8/R4 drive `src/revl/recovery.py` over
  an enumerated WAL corpus.
- **Seven guarantee codes have no theorem at all** (A1, A2, A3, A5, A6,
  A9, T1-T3). Of those, A5 is the one worth naming twice: G7 proves how a
  `compensation` entry is disposed without anything proving one has to
  exist.
- **One row is UNPROVED by construction** (G9 path coverage) and says so
  in the table, rather than being absent.

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
| `RevL.G4.inverse_or_emit` | G4 — inverse-or-emit (Def. 8) | **proved (shape-level)** | none | *weaker*: content is the shape of `Typed`, which has no `raw` constructor, so the same sentence holds of a relation admitting nothing. **Superseded by `RevL.G4Classified.inverse_or_emit_classified`** (item 418 step 4); kept as the syntactic statement |
| `RevL.G5.teardown_registers_nothing` | G5, teardown registers nothing | **proved, and CONTENTLESS** | none | `registrations` ignores its argument (constant zero), so a `sneakyUndo` calling `db.insert` counts 0 and the conclusion holds by definition rather than by any property of undo bodies. `RevL.G5.registrations_ignores_its_argument` proves the review's own probe, so the emptiness is on the record. **Superseded by `RevL.G5Classified.inverse_reaches_no_emission`** (item 418 step 4); kept as the grammar-level statement |
| `RevL.G6.confinement` | G6 — confinement (Def. 48) | **proved** | `propext, Quot.sound` | content is the shape of `TypedIn`/`ReachIn` |
| `RevL.Semantics.replays_or_discharges` | G7 (item 418 step 5) | **proved** | `propext` | replayed and discharged are complements, per kind and per **settling** verdict. Item 443 added the `v.settles = true` hypothesis; `RevL.Semantics.disposition_trichotomy` is the total replacement and `RevL.G7.settles_iff_strands_nothing` proves the hypothesis is exactly "this verdict strands nothing" |
| `RevL.Semantics.phase_lengths_add` | G7 (item 418 step 5) | **proved** | `propext, Quot.sound` | the two phases partition the replaying entries |
| `RevL.Semantics.teardown_length` | G7, length form | **proved** | `propext, Quot.sound` | one replay per entry *the verdict replays*. The pre-step-5 row said "one replay per witnessed effect", which over-counted every commit carrying a transactional entry |
| `RevL.G7.replay_table` | G7 (item 418 step 5) | **proved** | none | the teardown contract's replay rows, computed per kind and verdict |
| `RevL.G7.replayed_complete` | G7, completeness | **proved** | `propext` | every entry the verdict replays is on the replay list |
| `RevL.G7.teardown_replays_all` | G7 (LIFO-completeness, Thm. 16) | **proved** | `propext, Quot.sound` | the same at the inverse level. **Corrected in item 418 step 5**: the pre-step-5 statement had no hypothesis and was therefore false of a committing activation carrying a transactional entry |
| `RevL.G7.replayed_sound` | G7, soundness | **proved** | `propext` | the replay list holds only registered entries this verdict replays |
| `RevL.G7.teardown_only_witnessed` | G7 | **proved** | `propext, Quot.sound` | the same at the inverse level, now also excluding what the verdict discharges |
| `RevL.G7.commit_discharges_transactional` | G7 vs `runtime.py` | **proved** | `propext` | a clean commit never replays a witnessed inverse. This is the row the pre-step-5 G7 contradicted |
| `RevL.G7.commit_discharges_compensation` | G7 vs `runtime.py` (item 247) | **proved** | `propext` | a clean commit never fires a compensation |
| `RevL.G7.commit_replays_only_brackets` | G7 | **proved** | `propext` | a clean commit replays brackets and nothing else |
| `RevL.G7.abort_replays_every_transactional` | G7 vs `runtime.py` | **proved** | `propext` | the other half of the `_Transactional` branch |
| `RevL.G7.bracket_replays_under_every_settling_verdict` | G7 | **proved** | `propext` | releasing an acquired handle is always right *when the activation settles*. Item 443 added the hypothesis and this audit renamed the theorem to say so; `RevL.G7.bracket_replays_exactly_when_settling` is the hypothesis-free equation behind it and `RevL.G7.bracket_is_replayed_or_stranded` the total form |
| `RevL.G7.teardown_eq_reversed_inverses` | G7 | **proved** | `propext` | the LIFO equation, per phase; positions via `List.getElem_reverse` |
| `RevL.G7.compensations_drain_after_the_proof_pass` | G7, the phase split | **proved** | `propext` | the replay is a compensation-free prefix then an all-compensation suffix |
| `RevL.G7.phase1_is_lifo` | G7, order within a phase | **proved** | `propext` | undoing the run order gives back a sub-sequence of the registration order |
| `RevL.G7.phase2_is_lifo` | G7, order within a phase | **proved** | `propext` | the same for the drain |
| `RevL.G7.commit_runs_the_bracket_only` | G7, non-vacuity | **proved** | none | one stack, all three kinds: the commit replay is exactly the bracket inverse |
| `RevL.G7.abort_runs_the_proof_pass_then_the_drain` | G7, non-vacuity | **proved** | none | the same stack on abort: proof pass LIFO, then the compensation that was registered LAST |
| `RevL.G7.verdict_is_load_bearing` | G7, non-vacuity | **proved** | none | the two replays of one stack differ |
| `RevL.G7.commit_discharge_is_not_vacuous` | G7, non-vacuity | **proved** | `propext` | the discharged entry is on the stack the theorem quantifies over |
| `RevL.Semantics.disposition_trichotomy` | G7 / item 443 — total accounting | **proved** | none | every (kind, verdict) pair has EXACTLY one disposition: replayed, discharged or stranded. The hypothesis-free replacement for `replays_or_discharges` |
| `RevL.Semantics.halted_strands_every_kind` | G7 / item 443 — the E-Stop column | **proved** | none | under `.halted` every kind is neither replayed nor discharged, and stranded |
| `RevL.Semantics.replayed_length` | G7 / item 443 | **proved** | `propext, Quot.sound` | one replay-list entry per entry the verdict replays (`teardown_length` before the `map`) |
| `RevL.Semantics.book_lengths_add` | G7 / item 443 — the books balance | **proved** | `propext, Quot.sound` | replayed + discharged + stranded is the whole stack, under **every** verdict. All three terms are witnessed non-zero |
| `RevL.G7.estop_replays_nothing` | G7 / item 443 — the guarantee | **proved** | `propext, Classical.choice, Quot.sound` | a halt runs no inverse at all, not "the brackets and none of the rest" |
| `RevL.G7.estop_discharges_nothing` | G7 / item 443 | **proved** | `propext, Classical.choice, Quot.sound` | discharge releases the inverse and the witness; a halt must keep both for `revl recover` |
| `RevL.G7.estop_strands_everything` | G7 / item 443 — the counterpart of R4 | **proved** | `propext, Quot.sound` | R4 is "no residue"; the E-Stop is "all residue, all of it on the inventory" |
| `RevL.G7.estop_strands_the_bracket` | G7 / item 443 — non-vacuity | **proved** | none | the bracket row really does change in the third column, so the settling hypothesis is load-bearing |
| `RevL.G7.halt_inventory_is_total` | G7 / item 443 — the halt cut | **proved** | `propext` | completed ++ ambiguous ++ unattempted is the interrupted replay order exactly |
| `RevL.G7.halt_ambiguity_is_at_most_one` | G7 / item 443 — item 440's tier | **proved** | `propext, Quot.sound` | a halt makes at most ONE inverse ambiguous, never a fog over the stack |
| `RevL.G7.halt_books_are_total` | G7 / item 443 | **proved** | `propext, Quot.sound` | the five books balance at every cut and under every verdict |
| `RevL.G7.estop_is_load_bearing` | G7 / item 443 — non-vacuity | **proved** | none | one three-kind stack: abort replays 3 and strands 0, the halt replays 0 and strands 3 |
| `RevL.G7.mid_abort_halt_cut_is_not_vacuous` | G7 / item 443 — non-vacuity | **proved** | none | a halt one inverse into an abort: one completed, one ambiguous, one never attempted |
| `RevL.G7.settles_iff_not_halted` | G7 / item 443 — the hypothesis, audited | **proved** | `propext` | `v.settles = true` excludes **exactly** `.halted` and no other verdict |
| `RevL.G7.settles_iff_strands_nothing` | G7 / item 443 — the hypothesis, audited | **proved** | `propext` | a verdict settles precisely when it strands no kind, so `settles` is the property the dichotomy needs and not a side condition picked to rescue a proof |
| `RevL.G7.settling_strands_nothing` | G7 / item 443 — the hypothesis, audited | **proved** | `propext, Classical.choice, Quot.sound` | a settling verdict owes nothing: the inventory is empty for every stack. With `estop_strands_everything` this is the whole of `stranded` |
| `RevL.G7.bracket_replays_exactly_when_settling` | G7 / item 443 — strengthening | **proved** | none | *stronger than the corollary that carries the hypothesis*: the bracket row **equals** `settles`, with no hypothesis at all |
| `RevL.G7.bracket_is_replayed_or_stranded` | G7 / item 443 — the total form | **proved** | `propext` | over every verdict, no hypothesis: a registered bracket is replayed, or the verdict is the E-Stop and the bracket is on the inventory |
| `RevL.G8.boundary_enumerates_emissions` | G8 — boundary enumerable (§6.1) | **proved (shape-level)** | `propext, Quot.sound` | *weaker*: completeness over the syntactic `emit` marker. **Superseded by `RevL.G8Classified.surface_enumerates_reached_crossings`** (item 418 step 4); kept as the marker-level statement |
| `RevL.G8.boundary_only_declared` | G8 | **proved (shape-level)** | `propext, Quot.sound` | *weaker*: rests on `boundaryOf (.effect _ _) = []` **by definition**, so a typed `.effect` carrying an emission has an empty surface. **Superseded by `RevL.G8Classified.surface_only_declared_crossings`** (item 418 step 4); kept as the marker-level statement |
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
| `RevL.A8.revert_on_failure` | TODO 3 / A8 — L-Raise reverts | **proved** | `propext` | over the small-step semantics: the abort restores the world the body inherited |
| `RevL.A8.trace_reads_back_as_abort` | TODO 3 / A8 | **proved** | `propext` | no body step writes a session marker, so a crashed run rolls back |
| `RevL.A8.committed_transaction_is_retained` | TODO 3 / A8 — the central safety claim | **proved** | `propext` | a durable `discharge` record is never rolled back |
| `RevL.A8.commit_replays_no_inverse` | TODO 3 / A8 | **proved** | `propext` | only the abort verdict replays; a commit touches nothing |
| `RevL.A8.outcome_trichotomy` | TODO 3 / A8 — "never mixed" | **proved** | none | *definitional*: `Outcome` has three constructors; the content is in the two rows below |
| `RevL.A8.crash_cut_converges` | TODO 3 / A8 — convergence | **proved** | `propext, Quot.sound` | a decided crash point stays decided at every later cut |
| `RevL.A8.commit_record_is_the_decision` | TODO 3 / A8 — decision point | **proved** | `propext` | *specification agreement* with `recover`'s if-chain |
| `RevL.A8.approved_decides_the_crash_window` | TODO 3 / A8 — item 245 D3 | **proved** | `propext` | terminal marker missing: `commit-approved` alone decides |
| `RevL.A8.fence_before_apply_at_every_cut` | TODO 3 / A8 — item 309 §3a | **proved** | `propext` | crash BETWEEN the durable write and the effect: the window has no interior |
| `RevL.A8.at_most_once_across_crash` | TODO 3 / A8 — item 309 §3a | **proved** | `propext, Quot.sound` | an undeclared inverse is never applied twice, across abort-then-crash |
| `RevL.A8.declared_idempotent_replay_free` | TODO 3 / A8 — item 309 §3a | **proved** | `propext` | `recover` is idempotent over the declared subset (`DictWorld` pops) |
| `RevL.A8.double_apply_observable` | TODO 3 / A8 — non-vacuity | **proved** | none | a non-idempotent inverse's second apply IS observable |
| `RevL.A8.crash_cut_witness` | TODO 3 / A8 — non-vacuity | **proved** | `propext` | four cuts at one seq: clean abort, abort-then-crash, completed abort, committed |
| `RevL.A8.commit_witness` | TODO 3 / A8 — non-vacuity | **proved** | `propext` | a witnessed inverse does NOT replay on clean commit (cf. item 418 on G7) |
| `RevL.A8.mixed_disposition_admitted` | TODO 3 / A8 — non-vacuity | **proved** | `propext` | committed-and-retained beside rolled-back in one verdict, by design |
| `RevL.A8.revert_witness` | TODO 3 / A8 — non-vacuity | **proved** | none | the step relation genuinely takes the run; the log is its trace |
| `RevL.A8.revert_witness_restores` | TODO 3 / A8 — non-vacuity | **proved** | `propext` | replaying that trace empties the world again |
| `RevL.R4.residue_is_exactly_what_remains` | TODO 3 / R4 — soundness + completeness | **proved** | `propext` | after the abort the world is `w₀` plus EXACTLY the reported residue |
| `RevL.R4.abort_leaves_no_residue` | TODO 3 / R4 — headline | **proved** | `propext` | no boundary crossing ⇒ exact revert and an empty residue surface |
| `RevL.R4.residue_complete` | TODO 3 / R4 | **proved** | `propext` | every reported seq really is still out (the report is not padding) |
| `RevL.R4.residue_sound` | TODO 3 / R4 | **proved** | `propext` | nothing the body left behind goes unreported |
| `RevL.R4.txn_run` | TODO 3 / R4 — non-vacuity | **proved** | none | the semantics takes the witnessed-only run |
| `RevL.R4.emit_run` | TODO 3 / R4 — non-vacuity | **proved** | none | and the run that crosses the boundary |
| `RevL.R4.residue_necessary` | TODO 3 / R4 — non-vacuity | **proved** | `propext` | one step apart: one reverts exactly, one leaves seq 2 out and says so |
| `RevL.R4.emission_is_not_replayed` | TODO 3 / R4 — non-vacuity | **proved** | `propext` | an emission has no inverse; it moves to the residue surface |

| `RevL.Lemmas.reach_mono_fuel` | item 418 step 4 — the reach fold | **proved** | `propext, Quot.sound` | more fuel only raises a classification: the fold under-approximates, never over |
| `RevL.Lemmas.reaches_le` | item 418 step 4 — fold soundness | **proved** | `propext, Quot.sound` | every transitively reachable name is bounded by the fold's verdict |
| `RevL.Lemmas.reach_exact` | item 418 step 4 — fold exactness | **proved** | `propext, Quot.sound` | the verdict is attained at a real declaration; nothing is invented |
| `RevL.Lemmas.reach_le_trans` | item 418 step 4 — paths compose | **proved** | `propext, Quot.sound` | one verdict at the declared inverse constrains every call beneath it |
| `RevL.Lemmas.reachCaps_sound` | item 418 step 4 — capability surface | **proved** | `propext, Quot.sound` | every surface entry traces to a reachable crossing declaration |
| `RevL.Lemmas.reachCaps_complete` | item 418 step 4 — capability surface | **proved** | `propext, Quot.sound` | nothing a reachable crossing declares is dropped |
| `RevL.G4Classified.inverse_or_emit_classified` | G4 over the lattice (item 418 step 4) | **proved** | `propext` | the rule is `declOK`, i.e. `lower.py:2573`/`:2744`/`:2614` — not a missing constructor |
| `RevL.G4Classified.program_mutations_carry_inverse_or_marker` | G4 — program level | **proved** | `propext, Quot.sound` | every mutating declaration of an admitted program |
| `RevL.G4Classified.reached_crossing_is_classified` | G4 — the fn wrapper | **proved** | `propext, Quot.sound` | a crossing reached through a `fn` is still classified as crossing |
| `RevL.G4Classified.reached_crossing_carries_inverse_or_marker` | G4 — transitively | **proved** | `propext, Quot.sound` | a reached crossing has a concrete declaration behind it that satisfies G4 |
| `RevL.G4Classified.raw_mutation_is_representable` | G4 — anti-tautology guard | **proved** | none | the `raw` shape IS a term here; the old G4 could not write it |
| `RevL.G4Classified.g4_not_vacuous` | G4 — non-vacuity | **proved** | `propext` | three shapes admitted, two refused (`raw_write`, an emission claiming `undo`); refusal not universal |
| `RevL.G4Classified.fn_wrapper_still_crosses` | G4 — non-vacuity | **proved** | `propext` | `audit_log` is pure at fuel 0 and `emission` at fuel 1 |
| `RevL.G5Classified.registrations_seq` | G5 — the real count | **proved** | `propext` | the count is additive over sequencing |
| `RevL.G5Classified.registrations_zero_iff` | G5 — the real count | **proved** | `propext, Classical.choice, Quot.sound` | zero registrations iff no call crosses |
| `RevL.G5Classified.inverse_reaches_no_emission` | G5 as `_check_witnessed_inverse` | **proved** | `propext, Quot.sound` | NO name transitively reachable from an admitted inverse is `emission` or `witnessed` |
| `RevL.G5Classified.admitted_inverse_registers_nothing` | G5 | **proved** | `propext, Classical.choice, Quot.sound` | the declared inverse itself registers zero crossings |
| `RevL.G5Classified.admitted_inverse_body_registers_nothing` | G5 — whole teardown | **proved** | `propext, Classical.choice, Quot.sound` | every call anywhere beneath the inverse, not only the immediate callee |
| `RevL.G5Classified.pureOnly_run` | G5 — over the step-2 semantics | **proved** | none | a pure-only run moves neither log nor world |
| `RevL.G5Classified.clean_inverse_run_logs_nothing` | G5 — operational | **proved** | `propext, Classical.choice, Quot.sound` | a teardown that registers nothing appends nothing to the WAL |
| `RevL.G5Classified.admitted_inverse_run_logs_nothing` | G5 — end to end | **proved** | `propext, Classical.choice, Quot.sound` | admitted program ⇒ its teardown runs without touching the WAL |
| `RevL.G5Classified.registrations_depends_on_its_argument` | G5 — anti-tautology guard | **proved** | none | refutes the review's probe `∀ u v, registrations u = registrations v` |
| `RevL.G5Classified.registrations_counts` | G5 — non-vacuity | **proved** | none | 0 for the host-local inverse, 1 for the emission inverse and its `fn`-wrapped twin |
| `RevL.G5Classified.sneaky_undo_is_refused` | G5 — non-vacuity (`sneakyUndo`) | **proved** | `propext` | clean program admitted, emission inverse and `fn`-wrapped inverse refused |
| `RevL.G5Classified.fold_must_run_to_stability` | G5 — the fuel caveat, named | **proved** | `propext` | the wrapped escape is admitted at fuel 0 and refused at fuel 1 |
| `RevL.G5Classified.sneaky_inverse_run_emits` | G5 — non-vacuity, operational | **proved** | `propext` | the refused inverse really takes an `emit` step and logs a one-way record |
| `RevL.G5Classified.clean_inverse_run_is_silent` | G5 — non-vacuity, operational | **proved** | `propext, Classical.choice, Quot.sound` | the admitted inverse takes a `pure` step and every run of it is silent |
| `RevL.G5Classified.g5_row_not_vacuous` | G5, oracle U5 row non-vacuity (issue 276) | **proved** | none | the fn-wrapped teardown registers 1, and 0 once the wrapping fn stops calling the emission, so the differential U5 row's count is mutation-sensitive |
| `RevL.G8Classified.surface_enumerates_reached_crossings` | G8 completeness over the lattice | **proved** | `propext, Quot.sound` | nothing a reachable crossing declares is dropped from the audit |
| `RevL.G8Classified.surface_only_declared_crossings` | G8 soundness over the lattice | **proved** | `propext, Quot.sound` | everything on the surface traces to a reachable crossing declaration — **no typing hypothesis** |
| `RevL.G8Classified.surface_implies_crossing` | G8 — the two folds agree | **proved** | `propext, Quot.sound` | a non-empty surface has a crossing classification behind it |
| `RevL.G8Classified.effect_carrying_emission_is_on_the_surface` | G8 — anti-tautology guard | **proved** | `propext, Quot.sound` | the two models disagree in BOTH directions on concrete statements |
| `RevL.G8Classified.surface_agrees_with_an_honest_marker` | G8 — non-vacuity | **proved** | `propext, Quot.sound` | where the `emit` marker is honest the two agree; `witnessed[db]` names its scope |
| `RevL.G8Classified.raw_leak_is_on_the_surface` | G8 — the dropped hypothesis | **proved** | `propext, Quot.sound` | a `raw` leak is enumerated without needing `Typed` to exclude it |
| `RevL.G8Classified.g8_surface_is_not_universal` | G8 — non-vacuity | **proved** | `propext, Quot.sound` | a body on the surface, a non-empty body off it |
| `RevL.G8Classified.witness_surface_traces_to_its_declaration` | G8 — non-vacuity | **proved** | `propext, Quot.sound` | the entry `db_insert` traces through a `fn` to the `emission` that owns it |
| `RevL.G8Classified.g8_row_not_vacuous` | G8, oracle S8 row non-vacuity (issue 276) | **proved** | `propext, Quot.sound` | the fn-wrapped pure statement has surface `db_insert`, and empty once the wrapping fn stops calling the emission, so the differential S8 row is mutation-sensitive |

| `RevL.G1.g1_not_vacuous` | G1, non-vacuity (item 418 step 8) | **proved** | `propext, Classical.choice, Quot.sound` | a component admitted under its own `requires`, and the same body neither admitted nor confined under a smaller manifest |
| `RevL.G3.g3_not_vacuous` | G3, non-vacuity (step 8) | **proved** | `propext, Quot.sound` | a link, a layering, a dependency path with a strict rank drop, and a genuine self-path on the cyclic composition |
| `RevL.G4.g4_shape_not_vacuous` | G4, non-vacuity (step 8) | **proved** | none | both conclusion branches inhabited, and the `raw` branch shown to close because `Typed` lacks a constructor |
| `RevL.G5.registrations_ignores_its_argument` | G5, the FINDING (step 8) | **proved** | none | the review's constant-function probe, proved, plus an undo body that calls an emission and still scores zero |
| `RevL.G6.g6_not_vacuous` | G6, non-vacuity (step 8) | **proved** | `propext, Classical.choice, Quot.sound` | an admitted statement with a non-empty reach surface, and a refused one |
| `RevL.G6.g6_row_not_vacuous` | G6, oracle C row non-vacuity (issue 276) | **proved** | `propext, Classical.choice, Quot.sound` | the confinement check refuses `leakStmt`'s undeclared head and accepts it once the context is extended, so the differential C row's verdict is mutation-sensitive |
| `RevL.G8.g8_marker_level_not_vacuous` | G8, non-vacuity (step 8) | **proved** | `propext, Quot.sound` | a typed body with a non-empty surface, beside the `effect` whose surface is empty by definition |
| `RevL.CrossTier.cross_tier_agreement` | item 133 — cross-tier agreement | **proved** | `propext` | any two `Conformant` profiles lower a `WellAnnotated` IR to the same `Value` |
| `RevL.CrossTier.six_tier_agreement` | item 133 — the six backends | **proved** | `propext` | the corollary over the six-element `Tier` |
| `RevL.CrossTier.annotation_necessary` | item 133 — anti-tautology | **proved** | none | python and typescript disagree on a bare literal, so the annotation hypothesis is load-bearing |
| `RevL.CrossTier.conformance_hypotheses_are_inhabited` | item 133, non-vacuity (step 8) | **proved** | `propext` | two conformant tiers and a well-annotated map IR agreeing on a re-ordered non-empty value |
| `RevL.CapCeilings.capceilings_hypotheses_are_inhabited` | items 294/66/260, non-vacuity (step 8) | **proved** | `propext, Classical.choice, Quot.sound` | a `Lineage` built by `Lineage.spawn` from a real program spawn edge, plus `Descends`, `Resolves`, `NameableEmission`, a counter run and a refused overdraw |
| `RevL.CapCeilings.ceiling_lineage_is_inhabited` | item 260, non-vacuity (step 8) | **proved** | `propext, Quot.sound` | a spawn edge narrowing a `calls` ceiling from 3 to 2, so the lineage and the ceiling side condition hold together |
| `RevL.G9.g9_context_hypotheses_are_inhabited` | G9 + G6, non-vacuity (step 8) | **proved** | `propext, Classical.choice, Quot.sound` | a `TypedIn` crossing with a non-empty head list, an untrusted-free scope and an untrusted one |
| `RevL.R4.r4_side_conditions_are_inhabited` | R4, non-vacuity (step 8) | **proved** | `propext, Quot.sound` | freshness and disjointness hold at both traces, with the emitted set empty on one and not the other |
| `RevL.A8.a8_hypotheses_are_inhabited` | A8, non-vacuity (step 8) | **proved** | `propext, Quot.sound` | `SemLog`, a fork-free log, a discharged seq, and a `WAFrom` run that really fires an undeclared inverse under a unique seq space |
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

### G7 restated over the three-kind teardown stack (roadmap item 418, step 5)

`RevL.Semantics` used to carry one entry kind and define

    teardown log = log.reverse.map (·.inverse)

so `G7.teardown_replays_all` said, with no hypothesis, that every
accumulated inverse is replayed on every teardown. Item 418's C3 recorded
that as *false of revl*, and it was:
`backends/python/runtime.py`'s `_Transactional.__call__` reads the owning
frame's commit bit and, on a clean COMMIT, **discharges** the entry. The
inverse never runs and the witness is dropped, because the mutation is the
deliverable. Only an ABORT replays it.

L0 now carries what `docs/design/teardown-contract.md` specifies, the
table under "the three entry kinds, one stack" and the two-phase algorithm
under "the teardown algorithm":

| | `bracket` | `transactional` (243) | `compensation` (247) |
|---|---|---|---|
| clean commit | replays | discharged | discharged |
| abort | replays, Phase 1 | replays, Phase 1 | runs in Phase 2 |

`EntryKind`, `Verdict`, `EntryKind.replaysUnder`, `phase1`, `phase2`,
`replayed`, `discharged` and `teardown` are the model; `G7.replay_table`
pins the six cells so a change to the rule has to face them. The
guarantees are then stated relative to the kind and the verdict:
completeness and soundness over the entries the verdict replays
(`replayed_complete` / `replayed_sound`, and their inverse-level forms
`teardown_replays_all` / `teardown_only_witnessed`), the per-kind rows
against the runtime (`commit_discharges_transactional`,
`commit_discharges_compensation`, `commit_replays_only_brackets`,
`abort_replays_every_transactional`,
`bracket_replays_under_every_settling_verdict`), the LIFO equation per
phase, the
phase split (`compensations_drain_after_the_proof_pass`, the contract's
"all Phase-1 inverses complete before any compensation starts"), and LIFO
within a phase.

The witness is one activation stack carrying all three kinds, with the
acquisition registered first, the witnessed mutation second and the
compensation LAST. A verdict-blind teardown would replay the mutation on a
clean commit; a single-phase LIFO walk would fire the compensation first.
Neither happens: the commit replay is exactly `[release_handle]` and the
abort replay is exactly `[db_delete, release_handle, send_apology]`.

**Deliberately not modelled**, so the reach of these theorems is not
overstated: Phase-1 continue-and-record and its two residue severities
(`bracket-fault` / `restore-residue`), the Phase-2 budget and its
`compensation-residue` skips, deferred method-registered entries
(`_deferred_transactional`, item 318), escrow under a pending session
verdict (item 245), and cascading abort across activations. This model
says which entries run and in what order, not what happens when one of
them fails. The failure and crash side lives in
`RevL.Theorems.A8_WalDischarge` and `RevL.Theorems.R4_NoResidue`, over the
WAL rather than over the stack.

### G7 grows a third verdict: the operator E-Stop (roadmap item 443)

`docs/design/443-estop.md`. `Verdict` gains `halted`, and it is not a
relabelling of `abort`: it replays nothing, discharges nothing, and leaves
every registered entry **owed**. That is a third disposition, STRANDED —
registered, not run, not dropped — beside replayed and discharged.

| | `bracket` | `transactional` | `compensation` |
|---|---|---|---|
| clean commit | replays | discharged | discharged |
| abort | replays, Phase 1 | replays, Phase 1 | runs in Phase 2 |
| **halted (E-Stop)** | **stranded** | **stranded** | **stranded** |

The accounting is what makes the third column honest rather than a hole:
`disposition_trichotomy` proves every (kind, verdict) pair has EXACTLY one
disposition, and `book_lengths_add` proves replayed + discharged +
stranded is the whole stack, under every verdict. An entry cannot fall off
the books by being halted. The halt can also arrive *during* a teardown
that was already running, and that is a cut into the interrupted replay
order: `halt_inventory_is_total` partitions it into completed / ambiguous
/ unattempted, and `halt_ambiguity_is_at_most_one` bounds the ambiguity at
the single crossing that was in flight (item 440's tier, reached
deliberately rather than by accident).

**Two theorems gained a hypothesis, and this is the audit of it.** Adding
a hypothesis to rescue a proof is the standard way a formal layer stops
meaning what its name says, so the added `v.settles = true` on
`Semantics.replays_or_discharges` and on the theorem now called
`G7.bracket_replays_under_every_settling_verdict` is itself pinned by
theorems rather than by this paragraph:

- `settles_iff_not_halted` — the hypothesis excludes **exactly** the
  E-Stop and no other verdict, so the gap it leaves is a single case.
- `settles_iff_strands_nothing` — and it excludes it for the right
  reason: a verdict settles precisely when it strands no kind. `settles`
  is the property the replay/discharge dichotomy needs, spelled as a
  predicate, not a side condition chosen to make a proof go through.
- `settling_strands_nothing` — under a settling verdict the inventory is
  empty for **every** stack, which with `estop_strands_everything` is the
  whole of `stranded`: empty under `commit` and `abort`, the whole stack
  under `halted`.
- `bracket_replays_exactly_when_settling` — the bracket row is not merely
  *true* under the settling verdicts, it **equals** `settles`, with no
  hypothesis at all. So the restricted corollary is weaker than something
  proved, not weaker than something claimed, and its hypothesis is the
  exact condition its conclusion needs.
- `bracket_is_replayed_or_stranded` — the total form over every verdict,
  hypothesis-free: a registered bracket is replayed, or the verdict is the
  E-Stop and the bracket is on the inventory.

Neither restriction weakened G7 itself. `teardown_replays_all` and
`teardown_only_witnessed` — completeness and soundness, the two statements
G7 *is* — were already relative to `replaysUnder v` and carry no verdict
hypothesis at all; they hold under `.halted` unchanged, with an empty
replay set.

**The E-Stop is the counterpart of R4, not a hole in it.** R4 is "no
residue"; the halt is "all residue, all of it reported"
(`estop_strands_everything`). Deliberately NOT modelled here: what the
operator does with the inventory, the reconciliation path itself (`revl
recover` reading the descriptors back), and whether the latch is observed
promptly. This model says what a halt owes, not how the debt is settled.

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
evidence. Step 8 closed that count for the whole layer, in
`scripts/nonvacuity.tsv`; G9 was already ahead of it, carrying four
guards, each a registered theorem:
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


## The effect-classification lattice — G4/G5/G8 re-proved (item 418, step 4)

`RevL.Lemmas.ClassLemmas` (L1, core only) models what `src/revl` actually
computes, and `RevL.Theorems.G{4,5,8}_Classified*` restate G4, G5 and G8
over it. The old three theorems are kept, marked **proved (shape-level)**
in the table above with the specific weakness spelled out in each Notes
cell. They are not deleted and they are not wrong: they are statements
about the syntax, and the review's finding was that the syntax was doing
all the work.

### The model

`Cls` is the four-point lattice `pure | acquire | witnessed | emission`
(`parser.ExternDecl.classification`, `parser.py:513`), ordered by how much
of the host a declaration can disturb. Two predicates cut it, and both are
read straight off the reference:

- `Cls.crosses` = `witnessed` or `emission` — the seed set of
  `emission_analysis._emitting_capabilities` ("a witnessed extern crosses
  the same boundary as an emission", item 243);
- `Cls.inverseAdmissible` = `pure` or `acquire` — the admissible set of
  `lower._check_witnessed_inverse` ("the declared inverse is a host-LOCAL
  restore, so only `pure`/`acquire` callees are admissible").
  `crosses_eq_not_admissible` proves the two are one predicate.

`reachCls` / `reachCaps` are `_emitting_capabilities`' least fixed point
over the fn call graph: an extern stops the fold (it *is* the boundary),
a `fn` joins what its callees reach. `declOK` is the per-declaration rule
set (`lower.py:2573`, `:2608`, `:2614`, `:2735`, `:2744`) and `inverseOK`
is `_check_witnessed_inverse` read off the fold.

The fold is proved **sound** (`reaches_le`), **exact** (`reach_exact` —
its verdict is attained at a concrete declaration, so nothing is
invented), **monotone in fuel** (`reach_mono_fuel`) and **compositional
along a path** (`reach_le_trans`). Those four are what let a single
verdict taken at a declared inverse constrain every call beneath it.

### What changed for each guarantee

- **G4.** The old proof ends `| raw _ => cases ht`. Here the `raw` shape
  is an ordinary term (`rawWrite : ExternDecl`, an `acquire` with no
  `undo`), and `raw_mutation_is_representable` pins that. The refusal is a
  computation (`declOK`), and `g4_not_vacuous` shows three shapes admitted
  beside two refused, so it is not universal.
- **G5.** The old `registrations` is the constant-zero function, so an
  inverse calling `db.insert` scores clean. The new one counts calls whose
  *reached* classification crosses, and
  `registrations_depends_on_its_argument` refutes the review's probe
  outright. `sneaky_undo_is_refused` exhibits the `sneakyUndo` shape (a
  witnessed extern whose `undo` calls an `emission`), refuses it, refuses
  its one-`fn`-deeper twin, and admits the host-local inverse beside them.
  `sneaky_inverse_run_emits` then RUNS the refused inverse under step 2's
  semantics and shows the one-way boundary record land in the WAL, while
  `clean_inverse_run_is_silent` shows the admitted one take a `pure` step.
- **G8.** `boundaryOf` decides the surface per constructor, which is the
  definitional escape. `stmtSurface` is `stmtHeads` composed with the
  reach fold, uniformly over all four statement forms, so there is no
  per-constructor case to escape through. Both directions are kept, and
  soundness now carries **no typing hypothesis**: a `raw` leak is
  enumerated like anything else (`raw_leak_is_on_the_surface`) rather than
  discharged by `Typed` lacking a constructor.
  `effect_carrying_emission_is_on_the_surface` shows the two models
  disagreeing in both directions — an `effect` carrying an emission is on
  the new surface and not the old one; an `emit` marker on a host-local
  call is on the old surface and not the new one.

### What this does NOT close

- **The fuel bound is real and is named, not hidden.**
  `reachCls p fuel n` unrolls the closure `fuel` times, so it
  under-approximates when `fuel` is smaller than the longest `fn` chain.
  `fold_must_run_to_stability` exhibits exactly that: the `fn`-wrapped
  emission inverse is admitted at fuel 0 and refused at fuel 1. That
  `_emitting_capabilities` iterates to stability is a property of the
  reference implementation, not a theorem here.
- **The call graph is given, not derived.** `FnDecl.calls` stands for
  `_calls_in` over a lowered body. That `_calls_in` finds every call is an
  empirical obligation on the lowering; it is documented, not smuggled in
  as an axiom.
- **First-class dispatch is out of scope.** `_emitting_capabilities` adds
  the unnameable `*` when an emitting callable escapes as a value.
  `RevL.Theorems.CapCeilings` models `*`; nothing in this farm claims to.
- **`compensate` is not modelled as a separate slot's walk.**
  `_check_witnessed_inverse` walks `undo` and `compensate` alike; the
  model carries the `compensate` field and `declOK`'s witnessed rule
  refuses it, but `inverseOK` reads only the `undo` callee.
- No differential-oracle row references these definitions, so item 418's
  C4 applies here as it does to G9: the lattice model is not covered by
  that gate.

## Differential oracle (the proved model against the shipped checker)

`harness/diff_corpus.py` + `harness/Oracle.lean`: parse every corpus
`.rvl` with revl's real parser, export one TSV row per *fact*, then run
`harness/Oracle.lean` over the same TSV and diff its verdicts against a
Python reference. A mismatch fails `make formal`.

**What this checks (roadmap item 418, C4 / step 6 — was FALSE before,
now wired).** Until step 6 the oracle was not connected to anything
proved: `Oracle.lean` imported `RevL.Manifest` and used no definition
from it (deleting the import compiled clean), every verdict came from
private unproved Lean, and `diff_corpus.reference_from_tsv` was a third
re-implementation rather than a call into `src/revl/cap_order.py`. Step 1
of item 418 demonstrated the consequence by accident: `ProvidesDisjoint`
and `LinkOK` were restated over `(key, realm)` slots — a change to what
the model *decides* — and the oracle's output was bit-identical, 343 of
343 agreeing before and after. The G2/`LinkOK` correction later in the
item did it a second time, by construction.

Both sides are now real:

- the **Lean** side `decide`s the proved model. `V … disjoint=` is
  `decide (RevL.Manifest.ProvidesDisjoint comps)`; `V … closed=` is
  `decide (RevL.Manifest.RequiresClosed comps)`; `V … link=` is
  `linkOKB`, with `RevLOracle.linkOKB_iff` proving
  `linkOKB l = true ↔ RevL.Manifest.LinkOK l`; `W … atten=` is
  `attenuatesB`, with `attenuatesB_iff` proving
  `attenuatesB H R = true ↔ RevL.CapCeilings.Attenuates H R` — resource
  half through the proved `RevL.Lemmas.Covers` over `stripCeilings`
  (`coversB_iff`), ceiling half through the proved `budgetOf`
  development, whose unbounded `∀ k` is discharged with
  `RevL.Lemmas.budgetOf_attained`. The components are
  `RevL.Manifest.LComponent` values, so `slots`/`needs` are the model's.
- the **Python** side calls the shipped checker. Capabilities are parsed
  and ordered by `src/revl/cap_order.py` (`parse_cap`, `covers_set`,
  `split_ceilings`) — the one place the `(T, P)` algebra is implemented.
  The harness carries no capability grammar and no `covers` of its own.

The gate therefore bites, and this is the acceptance test for it: change
`RevL.Manifest.needs` to ignore the realm and `lake build` still succeeds
(27/27 targets, every theorem still proved) and the axioms gate is still
clean, but `harness/diff_corpus.py` exits 1 with four mismatches
(`examples/placement/caprealm_app.rvl`, `examples/tenants.rvl`,
`tests/fixtures/canary_tenants.rvl`, `tests/fixtures/erase_realms.rvl`).
The same edit against the pre-step-6 harness produced a bit-identical
verdict file and exit 0.

Facts exported: component manifests (M — with the component's `isolate`
realm map and a `member`/`template` role), require-binding resolutions
(R), provide-key resolutions (C), per-statement classifications (T), call
facts with marker context (U), service-method emission bounds (B) and a
scoped bound's declared entries (Q), the **reachability** facts that let
the model see past the marker — the capabilities a component's `requires`
bindings grant it (K), its activation emit-step surface (A), the emission
capabilities a provide method's body crosses (F), the activation-body
spawn edges (S), and the spawn handles (H) through which
`w.task.run(...)` resolves to the child's provision — the canonical
capability decompositions (Z/Y, straight out of `cap_order.parse_cap`),
parse refusals (X) and componentless files (N).

Two families of facts are of a different kind and are listed apart for
that reason, because neither is extracted from any `.rvl` text:

- **teardown scenarios (E/J)**, for G7. A teardown disposition is a
  property of a RUN, not of a manifest, so the fact is the shape of one
  activation's LIFO stack (E — one row per entry, in registration order,
  carrying its model `EntryKind` and the reference's registration seam)
  and the verdict it unwound under (J).
- **crash-recovery scenarios (L)**, for A8 and R4. A recovery verdict is
  a property of a durable LOG, so the facts are the records of one WAL —
  the constructors of `RevL.Lemmas.Rec`, one row each, in append order —
  plus the re-issue oracle 243 rule 6 makes fallible (`L … fails <seq>`,
  a property of the world rather than a record) and a `L … run` row
  declaring the scenario.

Two extraction facts on the `M` row are what make the corrected G2/G3
rules observable:

- the **realm map**, from the component's `isolate <key> in realm(<r>)`
  clauses. `lower._realm` reads the same table, and the linker's
  `provider_of` is keyed on `(key, realm)`. Without it the exporter could
  not feed the model's slot. (`isolate <key> in realms(...)` — the
  multi-realm ROUTE, item 162 — is a different construct and is not
  folded in; no corpus file uses one.)
- the **template flag**. A spawn target is a runtime instance, not a
  static composition member: `lower._link` excludes it from the G2/G3
  table and from `loadOrder` because each instance gets a fresh local
  realm. Without it two per-tenant worker templates read as one G2
  provision conflict, which is not what revl decides.

Verdicts:

- **V rows (per file, G2/G3)**: provision disjointness over `(key,
  realm)` slots, requirement closure, and linkability. `link=ok` means an
  admission order was found AND `linkOKB` certified it, over the LOCAL
  composition — requirements no in-file component provides are elided,
  because `lower._link` resolves those against the whole composition and
  adds no edge. Elided, not supplied by a fabricated provider: the
  provision surface is untouched, so a component that requires a key it
  provides *itself* keeps that requirement and is refused, which is the
  linker's G3 self-provision refusal, and a cycle is refused because no
  admission order exists.
- **G rows (per component, G4-shaped)**: marker presence must equal the
  interface's declaration — a plain call to a declared emission method,
  or an `emit`'d call to a non-emission method, is refused. Receivers
  include spawn handles, not just `requires` bindings.
- **P rows (per provide method, G4)**: a service declaration is an upper
  bound on its providers. The method's reached capabilities must sit
  inside its declared bound — `plain` admits none, bare `emission` admits
  any, `emission[...]` admits exactly the declared entries.
- **W rows (per activation spawn edge, G4/item 66/294)**:
  `RevL.CapCeilings.Attenuates` — the child's transitively closed reach
  covered by the spawner's held capabilities on ceiling-stripped
  capabilities, plus the ceiling budget check.
- **X rows (per refused file)**: a verdict of RECORD, not a computed one.
  revl's parser refused the file, so there is no manifest to model; the
  row carries the refusal code so the file is counted and diffed rather
  than dropped. Its agreement is tautological (both sides read the same
  parser) and is reported separately from the computed verdicts for
  exactly that reason. If a parser change ever makes one of these files
  parse, it flows into the full model and the buckets move loudly.
- **D rows (per teardown scenario, G7)**: the three columns of
  `RevL.Semantics` — `replayed` (ordered: this is the LIFO claim),
  `discharged` and `stranded`. Unlike every row above, the reference side
  does not recompute a judgment: it RUNS
  `backends/python/runtime.py` over the scenario's stack and reports what
  actually happened, reading each entry's fate off the reference's own
  state (`_Transactional.discharged`, `runtime.estop_residue()`) and the
  replay order off the inverses as they run. So the diff is the model's
  *predicted* disposition against the reference's *observed* one.
- **O rows (per crash-recovery scenario, A8 + R4)**: the model's
  `RevL.Lemmas.outcome` (which of the three verdicts `recover`'s if-chain
  converges to), `replayed` (the seqs a whole recovery run APPLIES) and
  `reported` (the residue surface of a roll-back). Reference side: write
  the records as a real WAL, call `revl.recovery.recover` over it, and
  read the verdict, the applied set and `residue.outstanding` off what it
  did. `replayed` is compared as a SET — the model walks the log in
  append order and `_roll_back` walks each record family newest-first, and
  neither order is a claim the other makes; the ordered LIFO claim is
  G7's and the D row checks it ordered. `reported` is compared only under
  `outcome = rolledBack`, the model's own scope.
- **C rows (per lowered statement, G6, issue 276)**: `Oracle.confinedB` over
  `RevL.Typing.stmtHeads` of the statement reconstructed from its exported
  heads (`exprOfHeads`, non-lossy by `heads_exprOfHeads`), against the
  component's declared context (its require locals from M plus its
  require-held binding roots from K). `confinedB_iff` proves the printed Bool
  is exactly `∀ k ∈ stmtHeads s, k ∈ C`, the surface `RevL.G6.confinement`
  quantifies over. The reference computes the same head-roots membership
  independently from the TSV. Every exported statement is from an admitted
  component, so a leak never appears as an admitted violation (the checker
  refuses those at parse, hence the G6 fixtures carry no I rows); the row is
  kept honest instead by `confinement_coverage`, which fails the gate unless
  the corpus carries both a confined statement over a non-empty reach and a
  caught violation, and by `g6_row_not_vacuous`, which proves the verdict
  flips when a leaking head is accepted. Host builtins and let-bound locals
  count as reach, so a component that uses them is a faithful `fail`, not a
  claim it is unsafe.

### The G7 row, and what it is evidence of

The G7 row exists because the audit's central reading was that a
guarantee with 32 theorems and no oracle row is in a different epistemic
position from one with 9 theorems and a row, and the proved-row count
cannot tell them apart. G7 was the largest such body.

**It is the first row whose reference side executes rather than reads.**
`teardown_observation` builds a real `runtime.Frame`, registers entries
through the reference's own five seams (a bare guarded `lambda` for a
bracket, `transactional` / `compensation` for the activation body,
`transactional_method` / `compensation_method` for a provide method), and
drives the teardown the way the emitted body drives it: `drain` is
yielded last so it is disposed first, the activation-body disposers
unwind newest-first, and `begin` is yielded first so it is disposed last
and runs the Phase-2 drain. Nothing about the outcome is computed by the
harness.

**The corpus is enumerated, not chosen.** Every registration sequence up
to depth 3 over the five seams, constrained to body-before-method because
a provide method runs after its component activated and a stack that
interleaves them is a run that cannot happen, crossed with all three
verdicts: 267 scenarios. An oracle row over cases someone picked is an
oracle row over the cases they thought of.

**What the row does NOT check, said plainly.** The LIFO unwind of the
activation-body disposer stack is cordis's, and the harness stands in for
cordis there — so for body-seam entries the row checks the *dispositions*
(which revl owns, in `_Transactional.__call__`, `_Compensation.__call__`,
`_guard` and `drain`) and not the unwind order. The one ordering the row
checks against revl's own code is `drain`'s `reversed(deferred)` loop
over the method-registered entries (item 369). Phase-1
continue-and-record, the Phase-2 budget, escrow and cascading abort are
outside the model and so outside the row.

**Non-vacuity is enforced, not assumed** (`teardown_coverage`). The audit
found the capability-ceiling half of the `W` row agreeing *vacuously* —
no corpus file declared an integer parameter, so the `ceilingOKB` branch
the theorems are about was never entered (closed since; see the `W` row's
own ratchet, `attenuation_coverage`) — and recorded that this is the same
defect class as roadmap item 429's self-host byte-agreement being green
over a corpus that never reached the logic. So this row ships with
a ratchet that fails the gate unless the corpus still contains a witness
for each of: a replay of length ≥ 2 whose order is not registration order
(LIFO is distinguishable from FIFO), a compensation that ran strictly
after a Phase-1 inverse (the two phases are distinguishable from one
interleaved pass), a non-empty discharge column, a non-empty stranded
column, and a replay set that is a proper subset of its stack. Every
clause is a property some plausible defect would remove.

### The A8/R4 row, and the divergence it found

A8 carried 18 theorems and R4 carried 9, and neither had an oracle row:
both were checked against `docs/` and against `src/revl/recovery.py` read
by hand. The `O` row closes that, and it is the second row whose reference
side executes.

**What the corpus is.** A recovery verdict is a property of a durable
log, so the scenario is a log: every multiset of up to two content
records over nine shapes — an undeclared transactional inverse, a
declared-idempotent one, one whose re-issue FAILS, a compensation, an
in-process effect, a reconstructible boundary effect declared and
undeclared, a closure-only one, and a deferred emission — crossed with a
durable `discharge` set (none / one / all), the at-most-once fences (none
/ all), and the trailing decision record (none / `aborted` /
`activation-complete` / `commit-approved` / `fork-frozen`). 1620
scenarios, enumerated rather than picked, for the same reason G7's are.

**What the reference does.** `recovery_observation` writes the records as
a real JSON-Lines WAL — the schema `revl.wal.read_wal` reads — and calls
`revl.recovery.recover` over it with a `DictWorld` that watches what is
actually applied. That distinction is load-bearing exactly once: a fenced
inverse resolved by a durable `aborted` record lands on
`transactionalRolledBack` and is NOT applied, so reading the applied set
off the report rather than off the world would report an apply that never
happened.

**The re-issue oracle is a fact, not an assumption.** 243 rule 6 makes
the inverse fallible and the model carries that as a parameter
(`ok : Seq → Bool`); the corpus ships `fails` rows and the world raises
on them, which is what puts `Disp.residue .restoreFailed` under the row at
all. Fixing it to `okAll` would have made one whole residue kind
unreachable.

**The divergence it found.** `RevL.Lemmas.dispose` and `reissued` modelled
the legacy `effect` family as "reconstructible ⇒ re-issued", which is what
`_roll_back` did before item 309 §3a was extended to it. The shipped code
refuses an UNDECLARED reconstructible boundary inverse whose fence is
already durable and reports fenced-residue instead
(`test_idempotent_inverse_309.test_undeclared_boundary_inverse_applies_once_then_fenced`
pins it). The model was therefore *weaker than the implementation* in the
dangerous direction: it claimed a clean re-issue and no residue where revl
refuses and reports one. 60 of the 1620 scenarios disagreed. Fixed here by
giving `Rec.effect` the `undoIdempotent` flag the reference reads and
adding the fence branch to both `dispose` and `reissued`; `SemLog` only
ever produces `.effect s true false false`, so no semantic lemma and no L2
theorem moved.

**Non-vacuity is enforced** (`recovery_coverage`), on the reference's own
observations rather than on the model's predictions: all three outcomes
and both roll-forward routes, a run that applied an inverse and a
roll-back that applied none, a committed seq retained and a fenced one
refused, a declared-idempotent inverse applied freely over a durable
fence, a fenced inverse resolved by an `aborted` record, every residue
kind the model can produce, and a clean abort over a non-empty log — R4's
headline, and the one shape a model that reported everything would fail.

Each column was also watched to FAIL before being relied on. Perturbing
the shipped side one invariant at a time — `_replay_tier` stops fencing
(80 mismatches), a committed seq is rolled back (198), a completed
activation rolls back (324), a closure-only inverse is dropped from the
residue (120), a re-issued compensation is reported clean (56), a failed
re-issue is reported clean (52), the `aborted` record stops deciding the
fenced branch (11) — reddens the row every time, and on the column the
invariant belongs to.

**The row was seen to fail before it was trusted.** Three independent
injections, each reverted:

| Injection | Caught by | Result |
|---|---|---|
| `drain` replays method-registered transactional entries FIFO (delete item 369's `reversed`) | the D row | exit 1, **8 mismatches**, e.g. `g7/abort/TT: reference=('e0','e1') formal=('e1','e0')` |
| `_Compensation.__call__` discharges on abort instead of deferring to Phase 2 | the D row | exit 1, **64 mismatches**, the compensation correctly reported as having moved from the replayed column to the discharged column |
| `sorry` in `RevLOracle.row_is_total` | the axioms gate over `harness/Oracle.lean` | exit 1, `depends on sorryAx — unfinished proof` |

Two further injections were caught *earlier* than the row, which is worth
recording because it says where each layer bites. Making the printed
replay column ignore the verdict does not produce a mismatch — it fails
to **elaborate**, because `mem_replayedLabels_iff` is what ties the
column to `RevL.G7.replayed_sound` / `replayed_complete`. Making the
model run compensations inline in Phase 1
(`EntryKind.inPhase1 .compensation := true`) does not produce a mismatch
either — it breaks `lake build`, because the G7 theorem file pins the
phase split. So model-side drift is caught by the theorem layer and
implementation-side drift by the row, and the two are complementary
rather than redundant.

**Bridge theorems** (all inside the axioms gate, `run_gate.sh` step 5):
`mem_replayedLabels_iff` proves the replayed column is exactly the
model's replay set (soundness left-to-right, completeness
right-to-left); `mem_dischargedLabels_iff` and `mem_strandedLabels_iff`
do the same for the other two columns; `row_is_total` proves the three
columns account for the whole stack via
`RevL.Semantics.book_lengths_add`, so an entry cannot fall off the row by
appearing in no column; and `replayedLabels_phase_order` pins the printed
order as the whole Phase-1 pass, LIFO, then the Phase-2 drain, LIFO,
which is what makes the column's *order* checkable.

**What is still a private restatement in `Oracle.lean`**, listed so the
gate's reach is not overstated: `g4OK` (the G row — the G4 model is
indexed by `RevL.Syntax.Stmt` and the export carries call facts, not
statement terms), `methodBoundOK` (the P row — the model states no
declaration-versus-reach bound at all; `RevL.Boundary.bodyBoundary`
enumerates crossing heads but never compares them to a declaration), and
`closeN` (the spawn-surface transitive closure feeding W —
`RevL.CapCeilings.reachIn` *is* that closure, but it is indexed by a
`Comp` carrying `body : List Stmt`, which the export cannot produce;
only the closure is local, the judgment it feeds is the model's).

### Census

Nothing is dropped and nothing is counted without being named. Over the
corpus as of `chore/formal-review`: **305 .rvl files → 189 components →
414 statements = 137 modeled + 140 componentless + 28 refused at parse**,
and **654 verdicts compared (137 files + 189 components + 27 provide
methods + 6 spawn edges + 28 parse refusals + 267 teardown scenarios),
654 agree, 0 mismatches**. (The corpus grows; the shape of the census
does not. The step-6 numbers were 296 / 182 / 403 and 371 verdicts; the
387 before the G7 row.)

- The 28 parse refusals are LISTED by name and code, not counted. The
  previous "(28 parse-error skips, loud)" parenthesis hid
  `examples/rejections/g4_missing_undo.rvl` — literally the shape G4
  forbids, refused with code G4 — and both G6 fixtures
  (`g6_closure_mutates_capture.rvl`, `g6_impure_statement.rvl`, both
  G6), along with a G2, a G8 and an A8 fixture.
- The 138 componentless files were previously in no bucket at all. They
  are now reported by checker code, with every non-`accept` one named:
  that surfaces `g1_template_undeclared.rvl` (a G1 the model never sees)
  and five `g4_extern_*` fixtures whose refusals are extern-declaration
  shapes, not composition shapes. The 83 accepted ones are backend emit
  corpora with no composition in them. Full list in
  `harness/out/no_manifest.txt`.

Checker alignment: each modeled file is compiled with the real checker
and its refusal code compared against the formal verdicts. `missed-G4`
and `missed-G2` are **gate failures** (item 418 step 7), not findings:
the checker refusing where the model sees nothing is the model being
weaker than what revl enforces. `formal-strict` — the model refusing what
the checker accepts — stays informational; it is the safe direction and
names fragment gaps. Current buckets: 93 agree-accept, 2 agree-G2, 1
agree-G3, 7 agree-G4, 34 out-of-fragment, and **0 missed-G4, 0 missed-G2,
0 formal-strict, 0 formal-found-other**.

Movements from the pre-step-6 buckets (52 agree-accept, 2 agree-G2, 6
agree-G4, 36 formal-strict, 13 formal-found-other, 21 out-of-fragment),
each explained:

- **36 formal-strict → agree-accept**, emptying the bucket. 32 of them
  were `V(ok, fail)` from reading the closure column as a checker-visible
  refusal; it is not one. `compile_source` type-checks and links ONE
  file, and `lower._link` reports nothing for a requirement with no
  in-file provider — that key is resolved against the rest of the
  composition at link time. Requirement closure is therefore reported in
  the V row and excluded from the alignment comparison, which uses
  `disjoint` and `link` (both genuinely checker-visible). The other 4 are
  the realm/template fix: `examples/tenants.rvl`,
  `tests/fixtures/canary_tenants.rvl` and `tests/fixtures/erase_realms.rvl`
  were `disjoint=fail` under the bare-key rule and are admitted by the
  slot rule, and `examples/tenant_attenuation.rvl` was `disjoint=fail`
  because its two spawn *templates* both provide `worker` — templates are
  not composition members.
- **13 formal-found-other → out-of-fragment**, emptying the bucket. Every
  one was a closure-only failure on a file the checker refuses for an
  unrelated reason (7 A1, 1 A6, 2 T1, 1 G1, 2 REVL). With closure out of
  the comparison the model is clean on them, and the refusal is honestly
  out of the modeled fragment rather than a phantom "the model found
  something else".
- **1 out-of-fragment → agree-G3**: `examples/rejections/g3_dependency_cycle.rvl`.
  This is the new bite, and it was not predicted by the step-6 brief. The
  harness had no G3 verdict at all before; the `link` column derives no
  admission order for the cycle, and the checker's G3 refusal now has a
  model verdict to agree with. `G3` was added to the alignment's
  guarantee-code arm for it.
- **28 parse refusals and 138 componentless files** enter the census for
  the first time. Verdicts compared rose 343 → 371 purely from the 28
  refusal rows; the 138 componentless files get no verdict (a vacuous
  `ok` over an empty composition would be inflation) and are reported by
  name instead. (The corpus was 292 files with 134 componentless when
  step 6 started; four more componentless fixtures landed on main during
  the work, and no other number moved with them.)
- Every G, P and W verdict is **unchanged**, 182 + 25 + 6 of them,
  bit-for-bit. Routing the coverage relation through the proved `Covers`
  and adding the ceiling half was expected to move nothing: the corpus
  carries no integer-valued capability parameter, so `stripCeilings` is
  the identity and `CeilingOK` is vacuous on it. The ceiling half is
  still wired on both sides, so the first corpus file to declare one is
  compared rather than ignored.

Known fidelity limits of the shaped model, deliberately not papered over:

- An emission reached through a spawn handle, an emission extern, or a
  transitively-emitting named function contributes the unnameable `*`
  capability rather than a resolved boundary. That mirrors the checker's
  own `*`, but it is coarse: `*` is covered only by `*`.
- Capability **ceilings** are modeled on both sides now (the model's
  `CeilingOK`, the checker's `split_ceilings`), but the corpus exercises
  neither: no file declares an integer-valued capability parameter, so
  the two agree on it vacuously.
- The `closed` column of the V row is `RevL.Manifest.RequiresClosed` over
  the file's own components. It is a real model predicate and a real
  question about a composition, but a single `.rvl` is not necessarily a
  composition, so it is reported and not aligned. 51 files carry
  `closed=fail` for that reason and none of them is a finding.

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
   (b) ~~*oracle side*~~ — **done** (item 418 step 6). The W verdict is
   now `RevL.CapCeilings.Attenuates`: the ceiling-blind coverage fold
   over `stripCeilings` plus the separate budget attenuation, on the Lean
   side through `attenuatesB_iff` and on the Python side through
   `cap_order.split_ceilings`/`covers_set`. The corpus still has no
   integer-valued capability parameter, so the two agree vacuously — but
   the comparison exists, so the first one to appear is checked rather
   than ignored.
3. **L3**: `Trusted[T]`/`Secret[T]` non-interference. **Done**, see *G9*
   below. (The WAL half is item 4; these two used to be one entry numbered
   3 twice, with the taint half called done in one and deferred in the
   other.) The taint half turned out **not** to need an
   L0 change: taint is a property of a value flowing along a path, L0's
   `Ctx`/`ReachIn` is a property of which keys a statement touches, and
   the two meet at one bridge (`taint._origin_of`: the origin a crossing
   mints is read off its declared capability scope). So the label algebra
   went into the new L1 farm `RevL.Lemmas.TaintLemmas` and the guarantees
   into the new L2 file `RevL.Theorems.G9_NoAuthorityFromUntrusted`,
   exactly as items 294/66/260 went into `CapLemmas` + `CapCeilings`.
   **L0 was untouched by this work and no pre-existing theorem's axiom set
   moved.** (Item 418 step 5 later did change L0, in `RevL.Semantics`; that
   is scoped to G7 and is described above.) What is proved is the flow
   *rule*; what remains open is
   the *coverage* of the checker's walk, which needs the L0 growth item
   418's ordered exit schedules ahead of it — see the G9 section for the
   named obligation and why it is not statable today.
4. **WAL commit/abort discharge. Done**, as R4 and A8 in the table above.

   This needed an operational semantics, which roadmap item 418 correctly
   said L0 did not have, so it was built additively in the L1 farm
   `RevL.Lemmas.WalLemmas` rather than by editing L0: **L0 was untouched by
   this work**. The farm carries (a) the record set, decision function
   and roll-back walk of `src/revl/wal.py` + `src/revl/recovery.py`, (b)
   a `Run`/`RunStep` model in which a crash is a *prefix*, and (c) a
   five-form small-step relation `SemStep`/`SemSteps` in which taking an
   effect step is what **appends its record and creates its referent** —
   so R4/A8 are stated over the effects that actually ran, not over a
   fabricated log. `Body.done` and `Body.fail` are stuck, and `fail` is
   L-Raise's failing step (418 step 3's prerequisite; the log also
   distinguishes a discharged transactional entry from a replayed one,
   which is 418 step 5's distinction).

   **Crash cuts covered:** between the durable write and the effect
   (`fence_before_apply_at_every_cut`, quantified over every cut);
   during teardown (abort-then-crash, fence durable, no `aborted`
   record); inside the approved-to-discharged window
   (`approved_decides_the_crash_window`).

   **Crash cut NOT covered, and not claimed:** a *witnessed* mutation
   logs its descriptor AFTER the forward extern returns `Ok`
   (`backends/python/emit.py`), so a crash between the mutation and its
   record leaves a mutation with no record at all. Every theorem here is
   relative to the durable log, and `SemStep.witnessed` collapses that
   window into one step. Durability is a floor, not a theorem: `WAFrom`
   orders the fence append before the apply; that `fsync` reaches the
   platter is a host obligation.

   **Also not modelled:** the roll-forward window's `flush-residue`
   surface (R4 is stated for the abort path), cascading abort,
   compensation drain and escrow, and item 250's frozen fork beyond
   being a third `Outcome` the commit/abort dichotomy is hypothesised
   away from.

   **Honest weighting of the rows**, per item 418's finding that a
   statement can be true and empty: `revert_on_failure`,
   `residue_is_exactly_what_remains`, `fence_before_apply_at_every_cut`,
   `at_most_once_across_crash`, `declared_idempotent_replay_free` and
   `crash_cut_converges` carry real content and each has a witness;
   `commit_record_is_the_decision` and
   `approved_decides_the_crash_window` are *specification agreement*
   with `recover`'s if-chain; `outcome_trichotomy` is *definitional* and
   is registered only so the weight of the "never a mixed state" claim
   is visible. "Never mixed" is about the VERDICT: per-seq dispositions
   are heterogeneous by design, and `mixed_disposition_admitted` pins
   that reading.

## Non-vacuity per theorem, and the layering gate (item 418, step 8)

Two things the axioms gate cannot see, now checked by
`scripts/run_gate.sh` before it ever calls `lake`.

### `scripts/nonvacuity.tsv` + `scripts/nonvacuity_gate.py`

`#print axioms` is exactly as clean on a theorem whose hypotheses cannot
all hold as on a load-bearing one. Item 418's review found G4/G5/G6/G8 to
be tautologies over a chosen inductive and counted "only 3 of 25 theorems
carry non-vacuity evidence". So every theorem registered in
`CheckAxioms.lean` now has a row in `scripts/nonvacuity.tsv` naming the
evidence, in one of four kinds:

- **instance** (97 rows): the hypotheses are jointly satisfiable, and the
  named witness theorems exhibit a concrete instance satisfying them.
- **necessity** (8 rows): the theorem refuses, so joint satisfiability is
  precisely what it denies. The witnesses show each hypothesis satisfiable
  on its own and the refusal not universal. `G3.linkOK_no_cycles` and
  `R4.abort_leaves_no_residue` are the shape.
- **concrete** (63 rows): the theorem is itself a computation on concrete
  data, so it has no hypotheses to satisfy. The gate accepts this label
  **only** when some other row cites the theorem as its witness, so it
  cannot be used to opt out.
- **contentless** (2 rows): true by definition rather than by any property
  of the subject. This is a finding, not a pass, and the gate prints both
  rows on every run.

The gate also fails on a row whose witness is not itself a registered
theorem (so witnesses are axiom-checked like everything else), on a
self-witnessing row, on a stale row, on `CheckAxioms.lean` and
`run_gate.sh` disagreeing about which theorems are registered, and — since
`chore/formal-review` — on a registered theorem this file does not name.
That last check was added because the record had already drifted: item
443's thirteen E-Stop theorems and three `CrossTier` theorems were
registered, witnessed and axiom-checked while the table above listed none
of them, and two rows went on describing the pre-443 unrestricted
statements of theorems that had since gained a `v.settles = true`
hypothesis. A proved theorem nobody can find and a record that overstates
what is proved are two halves of the same failure. That last
check is the one item 418's MEDIUM list wanted:
`Semantics.teardown_length` was once listed as proved in this file and
registered in neither.

**The two contentless rows, stated plainly** because they contradict how
the surrounding prose used to read:

- `RevL.G5.teardown_registers_nothing`. `registrations` is the constant
  zero function, so the theorem is true of every undo body including one
  whose only statement calls an emission.
  `RevL.G5.registrations_ignores_its_argument` proves the review's own
  probe, `forall u v, registrations u = registrations v`. The theorem is
  not *vacuous* (it has no hypotheses to be unsatisfiable) but it is
  *empty*: its conclusion follows from the definition and from nothing
  about undo bodies. The load-bearing G5 is `RevL.G5Classified`.
- `RevL.A8.outcome_trichotomy`. `Outcome` has three constructors, so this
  is definitional. It was already marked so in the table; the registry
  makes the marking machine-checked rather than editorial.

One further gap, found by the `chore/formal-review` audit and now closed:
`RevL.Semantics.discharged` appeared in three registered statements, and in
every one of them it was asserted **empty** (`discharged .halted log = []`,
`(discharged .halted stack).length = 0`) or summed inside a length
identity. So `book_lengths_add`'s three-way partition and
`halt_books_are_total`'s five-way one held with their `discharged` term
never exhibited non-zero, while the registry notes on `book_lengths_add`
and `estop_discharges_nothing` claimed a witness that no registered
theorem supplied. `G7.commit_discharge_is_not_vacuous` now computes
`discharged .commit stack` as a length-2 list containing the witnessed
mutation, and both rows cite it. The theorems did not change; what they
are worth did.

Nothing else in the registered set turned out to be vacuous. Writing the
witnesses did surface one real gap, now closed: the derived capability
lineage over the corpus (`wProgGood`) declares **no ceiling anywhere**, so
`lineage_ceiling_le` and `budget_never_exceeds_root_ceiling` had no
instance in which their `Lineage` hypothesis and their ceiling side
condition held together. `RevL.CapCeilings.ceiling_lineage_is_inhabited`
supplies one, a spawn edge narrowing `calls` from 3 to 2. Until it existed
those two theorems were conditioned on a pair of hypotheses this layer had
never exhibited jointly.

### `scripts/layering_gate.py`

This file's opening line used to call the L0/L1/L2 layering "enforced by
imports, not by hope", and item 418 recorded that nothing enforced it. The
script parses every `import` under `formal/RevL/` and fails on an L0 file
importing outside L0, an L1 farm file importing anything but L0 or
importing another farm file, and an L2 file importing another L2 file. It
also fails when an L1 or L2 module is missing from `RevL.lean`, because a
module outside the root import is a module outside the build and therefore
outside `CheckAxioms.lean`. The tree passes today: 5 L0, 7 L1, 16 L2
modules, no upward or sideways import.

### The oracle's own bridge theorems (`chore/formal-review`)

`harness/Oracle.lean` is outside `lakefile.lean`'s `lean_lib` root
(`roots := #[`RevL]`) and outside the directory the layering gate walks, so
`CheckAxioms.lean` could not reach it. Its nine `..B_iff` bridges are
exactly what this file cites when it says "the Lean side `decide`s the
proved model" — `linkOKB_iff`, `coversB_iff`, `attenuatesB_iff` and the
rest — and they were the only proofs under `formal/` outside every gate. A
`sorry` in one of them would have left `lake env lean --run` at exit 0 and
`make formal` green. The file now carries its own `#print axioms` block,
and `scripts/run_gate.sh` elaborates it a second time (without `--run`) and
feeds that block to the same `scripts/axioms_gate.py`. All nine are clean
on the standard three.

## Conventions for worker sessions

- State the theorem first with `sorry`, register it in `CheckAxioms.lean`
  and in this file as *stuck/TODO*, then fill it. The gate stays honest
  because `sorryAx` fails the build — a red build on a stated theorem is
  the system working, not a regression.
- Porting map: DESIGN.md §4 row → paper object → `src/revl/lower.py`
  (checker side) → the `tests/` file that currently witnesses the
  guarantee by execution. The test is the *example suite* for the formal
  model, not a substitute for it.

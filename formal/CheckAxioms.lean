/-
CheckAxioms — the no-smuggled-assumptions gate (`make formal`, CI).

Every theorem in the backbone must be printed here; the output is checked
by formal/scripts/axioms_gate.py, which fails on `sorryAx`, on any
project-defined axiom (Lean's three standard foundation axioms —
propext, Classical.choice, Quot.sound — are whitelisted), and on any
registered theorem missing from the output. This is the machine-checked
form of the repo rule that "a claim gets a command or it gets softened":
a theorem that quietly assumes its conclusion cannot pass the gate.

Add a `#print axioms` line for every new theorem in RevL.Theorems.*, and
add the theorem's name to the gate's argv in the Makefile + ci.yml.
-/

import RevL

-- G1: undeclared access cannot be written (component level).
#print axioms RevL.G1.declared_only_access

-- G2: provision disjointness per (key, realm) + requirement closure from
-- the link judgment, with both sides of the realm distinction witnessed.
#print axioms RevL.G2.linkOK_provision_disjoint
#print axioms RevL.G2.linkOK_requires_closed
#print axioms RevL.G2.realm_separation_admitted
#print axioms RevL.G2.same_realm_conflict_refused

-- G3: the layering certificate excludes dependency cycles, and the link
-- judgment supplies the certificate (so the hypothesis is dischargeable).
#print axioms RevL.G3.depPath_rank_lt
#print axioms RevL.G3.no_dependency_cycles
#print axioms RevL.G3.linkOK_layeredBy_rankOf
#print axioms RevL.G3.linkOK_layered
#print axioms RevL.G3.linkOK_no_cycles
#print axioms RevL.G3.self_provision_refused
#print axioms RevL.G3.mutual_cycle_refused
#print axioms RevL.G3.layering_exists_for_admitted

-- G4: every admitted mutation carries an inverse or an emit marker.
#print axioms RevL.G4.inverse_or_emit

-- G5: teardown cannot register effects.
#print axioms RevL.G5.teardown_registers_nothing

-- G6: confinement — admitted code reaches only declared keys.
#print axioms RevL.G6.confinement

-- G7 (item 418 step 5): LIFO-completeness over the three-kind teardown
-- stack, under the activation's verdict. The replay rule read off
-- docs/design/teardown-contract.md, completeness and soundness relative
-- to it, the per-kind rows against `backends/python/runtime.py`, the LIFO
-- equation, the two-phase split, and the concrete witnesses.
#print axioms RevL.Semantics.replays_or_discharges
#print axioms RevL.Semantics.phase_lengths_add
#print axioms RevL.Semantics.teardown_length
#print axioms RevL.G7.replay_table
#print axioms RevL.G7.replayed_complete
#print axioms RevL.G7.teardown_replays_all
#print axioms RevL.G7.replayed_sound
#print axioms RevL.G7.teardown_only_witnessed
#print axioms RevL.G7.commit_discharges_transactional
#print axioms RevL.G7.commit_discharges_compensation
#print axioms RevL.G7.commit_replays_only_brackets
#print axioms RevL.G7.abort_replays_every_transactional
#print axioms RevL.G7.bracket_replays_under_every_settling_verdict
#print axioms RevL.G7.teardown_eq_reversed_inverses
#print axioms RevL.G7.compensations_drain_after_the_proof_pass
#print axioms RevL.G7.phase1_is_lifo
#print axioms RevL.G7.phase2_is_lifo
#print axioms RevL.G7.commit_runs_the_bracket_only
#print axioms RevL.G7.abort_runs_the_proof_pass_then_the_drain
#print axioms RevL.G7.verdict_is_load_bearing
#print axioms RevL.G7.commit_discharge_is_not_vacuous

-- item 443: the E-Stop verdict (docs/design/443-estop.md) — the third
-- column of the teardown contract's table, and the halt's total accounting.
#print axioms RevL.Semantics.disposition_trichotomy
#print axioms RevL.Semantics.halted_strands_every_kind
#print axioms RevL.Semantics.replayed_length
#print axioms RevL.Semantics.book_lengths_add
#print axioms RevL.G7.estop_replays_nothing
#print axioms RevL.G7.estop_discharges_nothing
#print axioms RevL.G7.estop_strands_everything
#print axioms RevL.G7.estop_strands_the_bracket
#print axioms RevL.G7.halt_inventory_is_total
#print axioms RevL.G7.halt_ambiguity_is_at_most_one
#print axioms RevL.G7.halt_books_are_total
#print axioms RevL.G7.estop_is_load_bearing
#print axioms RevL.G7.mid_abort_halt_cut_is_not_vacuous

-- item 443, audit: what the `v.settles` hypothesis costs. It excludes
-- exactly `.halted`, it excludes it because that is the verdict that
-- strands, the bracket row is an equation with no hypothesis, and the
-- total form over every verdict is proved rather than dropped.
#print axioms RevL.G7.settles_iff_not_halted
#print axioms RevL.G7.settles_iff_strands_nothing
#print axioms RevL.G7.settling_strands_nothing
#print axioms RevL.G7.bracket_replays_exactly_when_settling
#print axioms RevL.G7.bracket_is_replayed_or_stranded

-- The bridge theorems the differential oracle's verdicts rest on live in
-- harness/Oracle.lean, which is outside this library's root import. They
-- are axiom-checked by their own `#print axioms` block at the foot of
-- that file, run as a second pass in scripts/run_gate.sh.

-- G8: the boundary surface is enumerable (completeness + soundness).
#print axioms RevL.G8.boundary_enumerates_emissions
#print axioms RevL.G8.boundary_only_declared

-- Item 133: cross-tier agreement — conformant runtimes agree on a
-- well-annotated IR; six-tier corollary; necessity of the annotation.
#print axioms RevL.CrossTier.cross_tier_agreement
#print axioms RevL.CrossTier.six_tier_agreement
#print axioms RevL.CrossTier.annotation_necessary

-- Item 294/66/260: capability ceilings and budgets — the (T,P) partial
-- order, downward-monotone attenuation along a lineage, budgets that only
-- shrink, the runtime counter, composition with confinement, the
-- unmanufacturable host boundary, and the two non-vacuity witnesses.
#print axioms RevL.CapCeilings.cap_order_partial
#print axioms RevL.CapCeilings.attenuation_monotone
#print axioms RevL.CapCeilings.lineage_ceiling_le
#print axioms RevL.CapCeilings.spend_within_budget
#print axioms RevL.CapCeilings.budget_never_exceeds_root_ceiling
#print axioms RevL.CapCeilings.confinement_within_ceiling
#print axioms RevL.CapCeilings.no_star_amplification
#print axioms RevL.CapCeilings.parameter_widening_refused
#print axioms RevL.CapCeilings.ceiling_check_not_subsumed

-- Item 294/66/260, STATUS.md TODO 2(a): `held` and `reach` DERIVED from
-- the statement fragment instead of taken as given -- the capKeys bridge
-- and the emit-step surface as lemmas, the unnameable receivers named,
-- the five affected guarantees re-stated over `SpawnsAdmitted`, and the
-- four derived non-vacuity witnesses (issue 1142: the capability column
-- and the wiring-key column are separate namespaces, and the witness
-- that separates them is `same_key_different_boundary_refused`, and issue
-- #1265: the same separation where nothing is declared at all, whose
-- witness is `same_key_undeclared_boundary_refused`).
#print axioms RevL.CapCeilings.derived_held_tokens_are_declared_keys
#print axioms RevL.CapCeilings.derived_reach_is_emit_surface
#print axioms RevL.CapCeilings.unnameable_receiver_is_star
#print axioms RevL.CapCeilings.derived_lineage
#print axioms RevL.CapCeilings.derived_attenuation_monotone
#print axioms RevL.CapCeilings.derived_lineage_ceiling_le
#print axioms RevL.CapCeilings.derived_budget_never_exceeds_root_ceiling
#print axioms RevL.CapCeilings.derived_confinement_within_ceiling
#print axioms RevL.CapCeilings.derived_no_star_amplification
#print axioms RevL.CapCeilings.derivation_non_vacuous
#print axioms RevL.CapCeilings.same_key_different_boundary_refused
#print axioms RevL.CapCeilings.same_key_undeclared_boundary_refused
#print axioms RevL.CapCeilings.derivation_refuses_unnameable
#print axioms RevL.CapCeilings.derived_ceiling_check_not_subsumed
-- G9 (items 249/256/329): untrusted data gains no authority — the label is
-- monotone except at an explicit declassifier, an authority sink admits a
-- tainted value only through one that clears that very origin, a bound
-- provider key has no declassifier at all, a `Secret[T]` value needs its
-- declared downgrade, a self-minted declassifier does not count, the origin
-- surface is bounded by the declared context, and the two non-vacuity
-- witnesses.
#print axioms RevL.G9.origin_persists_or_is_declassified
#print axioms RevL.G9.no_authority_from_untrusted
#print axioms RevL.G9.untrusted_gains_no_authority
#print axioms RevL.G9.declassification_is_the_only_escape
#print axioms RevL.G9.secret_persists
#print axioms RevL.G9.secret_confined
#print axioms RevL.G9.confidential_needs_declassification
#print axioms RevL.G9.flow_declassifiers_granted
#print axioms RevL.G9.untrusted_author_needs_granted_declassifier
#print axioms RevL.G9.declassifier_must_be_declared
#print axioms RevL.G9.taint_surface_within_declared_context
#print axioms RevL.G9.no_untrusted_without_a_declared_source
#print axioms RevL.G9.g9_not_vacuous
#print axioms RevL.G9.secret_rules_not_vacuous

-- G9 anti-tautology guards (roadmap item 418): the refusal is not
-- universal, the four sink rules are four different rules, and the two
-- `secret` refusals are load-bearing rather than structural.
#print axioms RevL.G9.authority_refusal_is_not_universal
#print axioms RevL.G9.sink_rules_are_distinct
#print axioms RevL.G9.secret_refusal_is_load_bearing

-- TODO 3 / R4: the abort's residue surface is exactly what the runtime
-- reports, stated over the small-step semantics that accumulates the log
-- (soundness + completeness), plus the two runs that make it non-vacuous.
#print axioms RevL.R4.residue_is_exactly_what_remains
#print axioms RevL.R4.abort_leaves_no_residue
#print axioms RevL.R4.residue_complete
#print axioms RevL.R4.residue_sound
#print axioms RevL.R4.txn_run
#print axioms RevL.R4.emit_run
#print axioms RevL.R4.residue_necessary
#print axioms RevL.R4.emission_is_not_replayed

-- TODO 3 / A8: WAL commit/abort discharge across a crash cut — L-Raise
-- reverts, a commit replays nothing, the commit record is the decision
-- and it converges, and an undeclared inverse is applied at most once
-- however the crash cuts the fence/apply window. Witnesses included.
#print axioms RevL.A8.revert_on_failure
#print axioms RevL.A8.trace_reads_back_as_abort
#print axioms RevL.A8.committed_transaction_is_retained
#print axioms RevL.A8.commit_replays_no_inverse
#print axioms RevL.A8.outcome_trichotomy
#print axioms RevL.A8.crash_cut_converges
#print axioms RevL.A8.commit_record_is_the_decision
#print axioms RevL.A8.approved_decides_the_crash_window
#print axioms RevL.A8.fence_before_apply_at_every_cut
#print axioms RevL.A8.at_most_once_across_crash
#print axioms RevL.A8.declared_idempotent_replay_free
#print axioms RevL.A8.double_apply_observable
#print axioms RevL.A8.crash_cut_witness
#print axioms RevL.A8.commit_witness
#print axioms RevL.A8.mixed_disposition_admitted
#print axioms RevL.A8.revert_witness
#print axioms RevL.A8.revert_witness_restores

-- Item 418 step 4 — the effect-classification lattice (L1 farm
-- RevL.Lemmas.ClassLemmas). The emission-reach fold is sound along a
-- path, exact (its verdict is attained at a real declaration), monotone
-- in fuel, and its capability surface is sound and complete. G4/G5/G8's
-- restatements below all rest on these.
#print axioms RevL.Lemmas.reach_mono_fuel
#print axioms RevL.Lemmas.reaches_le
#print axioms RevL.Lemmas.reach_exact
#print axioms RevL.Lemmas.reach_le_trans
#print axioms RevL.Lemmas.reachCaps_sound
#print axioms RevL.Lemmas.reachCaps_complete

-- Item 418 step 4 — G4 over the classification lattice: the rule is
-- `declOK` (acquire/witnessed must declare an inverse, emission IS the
-- marker), not a missing constructor. The `raw` shape is a representable
-- term that the check refuses, and the refusal is not universal.
#print axioms RevL.G4Classified.inverse_or_emit_classified
#print axioms RevL.G4Classified.program_mutations_carry_inverse_or_marker
#print axioms RevL.G4Classified.reached_crossing_is_classified
#print axioms RevL.G4Classified.reached_crossing_carries_inverse_or_marker
#print axioms RevL.G4Classified.raw_mutation_is_representable
#print axioms RevL.G4Classified.g4_not_vacuous
#print axioms RevL.G4Classified.fn_wrapper_still_crosses

-- Item 418 step 4 — G5 as `_check_witnessed_inverse`: an inverse's
-- TRANSITIVE classification excludes emission and witnessed; the
-- registration count reads its argument (proved); the teardown, run
-- under step 2's semantics, appends nothing to the WAL; and the
-- `sneakyUndo` shape is exhibited, refused, and shown to emit.
#print axioms RevL.G5Classified.registrations_seq
#print axioms RevL.G5Classified.registrations_zero_iff
#print axioms RevL.G5Classified.inverse_reaches_no_emission
#print axioms RevL.G5Classified.admitted_inverse_registers_nothing
#print axioms RevL.G5Classified.admitted_inverse_body_registers_nothing
#print axioms RevL.G5Classified.pureOnly_run
#print axioms RevL.G5Classified.clean_inverse_run_logs_nothing
#print axioms RevL.G5Classified.admitted_inverse_run_logs_nothing
#print axioms RevL.G5Classified.registrations_depends_on_its_argument
#print axioms RevL.G5Classified.registrations_counts
#print axioms RevL.G5Classified.sneaky_undo_is_refused
#print axioms RevL.G5Classified.fold_must_run_to_stability
#print axioms RevL.G5Classified.sneaky_inverse_run_emits
#print axioms RevL.G5Classified.clean_inverse_run_is_silent
#print axioms RevL.G5Classified.g5_row_not_vacuous

-- Item 418 step 4 — G8 computed from the classification instead of from
-- `boundaryOf`'s per-constructor cases: completeness and soundness kept,
-- the typing hypothesis dropped, and the two models shown to disagree in
-- both directions on concrete statements.
#print axioms RevL.G8Classified.surface_enumerates_reached_crossings
#print axioms RevL.G8Classified.surface_only_declared_crossings
#print axioms RevL.G8Classified.surface_implies_crossing
#print axioms RevL.G8Classified.effect_carrying_emission_is_on_the_surface
#print axioms RevL.G8Classified.surface_agrees_with_an_honest_marker
#print axioms RevL.G8Classified.raw_leak_is_on_the_surface
#print axioms RevL.G8Classified.g8_surface_is_not_universal
#print axioms RevL.G8Classified.witness_surface_traces_to_its_declaration
#print axioms RevL.G8Classified.g8_row_not_vacuous

-- Item 418 step 8: the non-vacuity witnesses added so that every
-- registered theorem has a row in formal/scripts/nonvacuity.tsv naming
-- concrete evidence that its hypotheses are satisfiable. The registry is
-- checked by formal/scripts/nonvacuity_gate.py.
#print axioms RevL.G1.g1_not_vacuous
#print axioms RevL.G3.g3_not_vacuous
#print axioms RevL.G4.g4_shape_not_vacuous
#print axioms RevL.G5.registrations_ignores_its_argument
#print axioms RevL.G6.g6_not_vacuous
#print axioms RevL.G6.g6_row_not_vacuous
#print axioms RevL.G8.g8_marker_level_not_vacuous
#print axioms RevL.CrossTier.conformance_hypotheses_are_inhabited
#print axioms RevL.CapCeilings.capceilings_hypotheses_are_inhabited
#print axioms RevL.CapCeilings.ceiling_lineage_is_inhabited
#print axioms RevL.G9.g9_context_hypotheses_are_inhabited
#print axioms RevL.R4.r4_side_conditions_are_inhabited
#print axioms RevL.A8.a8_hypotheses_are_inhabited

-- Issue 1167: A9, every provide block is a declared slot. The installed
-- block keys are modelled beside `LComponent` (no L0 edit); `a9B` is what
-- the differential oracle's `A9` row decides.
#print axioms RevL.A9.a9B_iff
#print axioms RevL.A9.noDoubleInstallB_iff
#print axioms RevL.A9.installed_block_is_slot
#print axioms RevL.A9.undeclared_block_is_no_slot
#print axioms RevL.A9.installed_block_uniquely_provided
#print axioms RevL.A9.installed_slots_nodup
#print axioms RevL.A9.a9_not_vacuous
#print axioms RevL.A9.a9_rules_are_distinct
#print axioms RevL.A9.a9_row_not_vacuous

-- Issue #1172 (PR #1184): the converse of A9. Every declared key is
-- installed by a block or by a `realms(...)` route; `A9OK` is now both
-- directions and the oracle's A9 row decides both.
#print axioms RevL.A9.blocksDeclaredB_iff
#print axioms RevL.A9.declaredInstalledB_iff
#print axioms RevL.A9.declared_uninstalled_refused
#print axioms RevL.A9.unrouted_needs_a_block
#print axioms RevL.A9.routed_installs_without_block
#print axioms RevL.A9.a9_converse_not_vacuous
#print axioms RevL.A9.a9_directions_are_distinct

-- Issue 1166: A2 (no acquisition after a provision) stated over the LIFO
-- stack. The checker's fold (`a2B`), its bridge to the declarative rule,
-- the body's registrations as a `RevL.Semantics` stack, the proof-pass
-- ordering under every settling verdict, and the fixture's converse.
#print axioms RevL.A2.a2Fold_iff
#print axioms RevL.A2.a2B_iff
#print axioms RevL.A2.labels_distinct
#print axioms RevL.A2.stack_all_brackets
#print axioms RevL.A2.phase1_of_brackets
#print axioms RevL.A2.stack_of_no_acquire
#print axioms RevL.A2.stack_shape
#print axioms RevL.A2.proof_pass_is_withdrawals_then_releases
#print axioms RevL.A2.withdrawals_precede_releases
#print axioms RevL.A2.teardown_labels
#print axioms RevL.A2.fixture_refused
#print axioms RevL.A2.fixture_release_before_withdrawal
#print axioms RevL.A2.fixture_opens_the_window
#print axioms RevL.A2.a2_not_vacuous

-- Issue #1742: the G4 deferred-position rule. A `deferred` emission extern is
-- reached only by a call in a component; never called in a `fn` or `test`
-- body, inside an arrow there, or as a function value. The per-reach rule and
-- its decider, the file rule and its decider (the oracle's `DF` row), and the
-- corpus shapes the rule both admits and refuses.
#print axioms RevL.G4Deferred.legalB_iff
#print axioms RevL.G4Deferred.deferredB_iff
#print axioms RevL.G4Deferred.value_never_legal
#print axioms RevL.G4Deferred.body_reach_refused
#print axioms RevL.G4Deferred.refused_of_mem
#print axioms RevL.G4Deferred.fixtures_decided
#print axioms RevL.G4Deferred.deferred_not_vacuous
-- Issue #1455: the approval floor (item 246, Decision 3). The capability
-- token, the approval edge and the refusal, decided per marked crossing by
-- the differential oracle's `AP` row.
#print axioms RevL.G4Approval.coversB_iff
#print axioms RevL.G4Approval.edgeCoversB_iff
#print axioms RevL.G4Approval.crossingB_iff
#print axioms RevL.G4Approval.no_edge_iff_nothing_required
#print axioms RevL.G4Approval.uncovered_required_refused
#print axioms RevL.G4Approval.unrequired_needs_no_edge
#print axioms RevL.G4Approval.covering_edge_admits
#print axioms RevL.G4Approval.globMatch_star_any
#print axioms RevL.G4Approval.approval_not_vacuous
#print axioms RevL.G4Approval.approval_row_not_vacuous
-- Issue #1812: G6 binding uniqueness. A binding may not reuse a name in view,
-- and visibility is block-scoped. The frame-membership decider, the scope
-- decider (the oracle's `BU` row), the three refusal shapes and the corpus
-- shapes the rule both admits and refuses.
#print axioms RevL.G6Binding.okB_iff
#print axioms RevL.G6Binding.bindingB_iff
#print axioms RevL.G6Binding.visibleB_iff
#print axioms RevL.G6Binding.seed_rebind_refused
#print axioms RevL.G6Binding.rebind_refused
#print axioms RevL.G6Binding.inner_shadow_refused
#print axioms RevL.G6Binding.fixtures_decided
#print axioms RevL.G6Binding.binding_not_vacuous
-- Issue #1807: G1 declared access over a component's real heads. Every
-- access root (a call head's root that is no binding, callable, import, host
-- family or constructor) is a declared requirement; decided per component by
-- the oracle's `G1` row.
#print axioms RevL.G1Access.accessB_iff
#print axioms RevL.G1Access.undeclared_refused
#print axioms RevL.G1Access.access_mono
#print axioms RevL.G1Access.declaring_admits
#print axioms RevL.G1Access.fixtures_decided
#print axioms RevL.G1Access.g1_access_not_vacuous
-- Issue #1808: A1 async colour. Reach of an async name through the `fn`
-- graph within a fuel bound, the per-site rule (a sync method, an unawaited
-- step and a teardown slot reach nothing async; an awaited step must), the
-- signature rule, and the corpus shapes; decided by the oracle's `A1`/`A1S`
-- rows.
#print axioms RevL.A1Async.reachB_iff
#print axioms RevL.A1Async.reach_mono
#print axioms RevL.A1Async.reaches_iff
#print axioms RevL.A1Async.siteB_iff
#print axioms RevL.A1Async.teardown_suspension_refused
#print axioms RevL.A1Async.await_without_async_refused
#print axioms RevL.A1Async.await_pairing_exact
#print axioms RevL.A1Async.sigB_iff
#print axioms RevL.A1Async.fixtures_decided
#print axioms RevL.A1Async.a1_not_vacuous
-- Issue #1809: three declaration rules. Prelude ordering (preludes precede
-- every action), intercept target (a requirement, not a provision) and
-- method in service (A6 call-site half); decided by the oracle's `PL`, `IC`
-- and `MS` rows.
#print axioms RevL.Prelude.preludeB_iff
#print axioms RevL.Prelude.prelude_after_action_refused
#print axioms RevL.Prelude.preludes_first_admitted
#print axioms RevL.Prelude.interceptB_iff
#print axioms RevL.Prelude.intercept_provision_refused
#print axioms RevL.Prelude.methodB_iff
#print axioms RevL.Prelude.undeclared_method_refused
#print axioms RevL.Prelude.fixtures_decided
#print axioms RevL.Prelude.prelude_rules_not_vacuous
-- Issue #1811: G-MODEL-PLACE placement. A confidentiality origin is placed on
-- the device only, decided by the oracle's `MPV` row; the model-reach half is
-- decided by the oracle's `MAV` row through the proved `attenuatesB`.
#print axioms RevL.ModelPlace.placeB_iff
#print axioms RevL.ModelPlace.off_device_refused
#print axioms RevL.ModelPlace.open_origin_anywhere
#print axioms RevL.ModelPlace.on_device_admitted
#print axioms RevL.ModelPlace.fixtures_decided
#print axioms RevL.ModelPlace.placement_not_vacuous
-- Issue #1811: G-COUNCIL-SPLIT tie policy. A council may not admit when its
-- members disagree, decided by the oracle's `CTV` row.
#print axioms RevL.ModelCouncil.splitB_iff
#print axioms RevL.ModelCouncil.admitting_refused
#print axioms RevL.ModelCouncil.split_admitted
#print axioms RevL.ModelCouncil.deny_admitted
#print axioms RevL.ModelCouncil.unknown_tie_not_this_row
#print axioms RevL.ModelCouncil.fixtures_decided
#print axioms RevL.ModelCouncil.council_not_vacuous
-- Issue #1811 group 2: G9 / G-SECRET-FLOW. The taint rule is decided by the
-- oracle's `TAINT` row on the sink and label the CHECKER reports, not on the
-- coverage of the checker's own walk; see RevL/Theorems/G9Flow.lean.
#print axioms RevL.G9Flow.admitsB_iff
#print axioms RevL.G9Flow.g9RowB_iff
#print axioms RevL.G9Flow.reported_walk_reaches_the_reported_label
#print axioms RevL.G9Flow.escapeFlow
#print axioms RevL.G9Flow.labelOfString_is_the_label
#print axioms RevL.G9Flow.corpus_rows_decided
#print axioms RevL.G9Flow.escape_moves_the_label
#print axioms RevL.G9Flow.g9_not_vacuous
-- Issue #1811 group 3: G-RETAIN. The retention rule is decided by the oracle's
-- `RETAIN` row on the sink, scope and TWO INSTANTS the CHECKER reports — the
-- harness pins the instant (`REVL_RETENTION_AS_OF`) so the verdict is
-- reproducible — not on the coverage of the checker's own walk; see
-- RevL/Theorems/GRetain.lean.
#print axioms RevL.GRetain.holdsB_iff
#print axioms RevL.GRetain.rowB_iff
#print axioms RevL.GRetain.hold_clears_the_deadline
#print axioms RevL.GRetain.reaches_iff
#print axioms RevL.GRetain.corpus_sink_scope_is_modelled
#print axioms RevL.GRetain.unmodelled_sink_scope_has_no_sink
#print axioms RevL.GRetain.corpus_walk_does_not_reach_another_sink
#print axioms RevL.GRetain.corpus_row_decided
#print axioms RevL.GRetain.retain_not_vacuous

-- Issue #2097: the G4 inverse rule. A bracket's `undo` must be the inverse its
-- acquisition owns: a host family's release on the handle it bound
-- (`lower._check_site_release`, issue #1859), the inverse an `extern acquire`
-- DECLARES on the handle it bound, or a host write's inverse on the same
-- receiver and key (`lower._method_effect_inverse`, issue #1945). The oracle's
-- `INV` row decides the rule at the arm, acquisition, demanded verb and
-- demanded spelling the CHECKER reports — the rule ON THE CORPUS, the site the
-- checker DISCOVERED, NOT coverage of the checker's walk (roadmap item 418
-- step 9 is deliberately unclaimed); see RevL/Theorems/G4Inverse.lean.
#print axioms RevL.G4Inverse.rowB_iff
#print axioms RevL.G4Inverse.requiredInverse_none_off_the_tables
#print axioms RevL.G4Inverse.extern_arm_is_the_declared_inverse
#print axioms RevL.G4Inverse.host_release_table
#print axioms RevL.G4Inverse.write_inverse_table
#print axioms RevL.G4Inverse.kindOfString_is_the_kind
#print axioms RevL.G4Inverse.unmodelled_acquisition_has_no_inverse
#print axioms RevL.G4Inverse.extern_arm_never_falls_off_a_table
#print axioms RevL.G4Inverse.corpus_rows_refused
#print axioms RevL.G4Inverse.corpus_rows_flip_at_the_demanded_spelling
#print axioms RevL.G4Inverse.corpus_host_row_flips_when_the_demand_leaves_the_table
#print axioms RevL.G4Inverse.corpus_write_row_flips_when_the_receiver_moves
#print axioms RevL.G4Inverse.corpus_row_verbs_are_the_tables
#print axioms RevL.G4Inverse.g4_inverse_not_vacuous

-- Issue #2098: the witnessed-extern site-`undo` rule. A `witnessed` extern
-- already declares its own inverse, so its call sites do not spell one, and a
-- site that spells one anyway is refused (G4, category `witnessed`). The rule
-- is decided by the oracle's `SW` row on the site's own columns — the
-- acquisition head's classification and the heads the site's `undo` spells,
-- both already in the export — not on the coverage of the checker's site walk;
-- see RevL/Theorems/G4_WitnessedSiteUndo.lean.
#print axioms RevL.G4Witnessed.legalB_iff
#print axioms RevL.G4Witnessed.swB_iff
#print axioms RevL.G4Witnessed.legalCols_iff
#print axioms RevL.G4Witnessed.unmodelled_classification_is_refused
#print axioms RevL.G4Witnessed.other_head_never_refused
#print axioms RevL.G4Witnessed.witnessed_undo_refused
#print axioms RevL.G4Witnessed.refused_of_mem
#print axioms RevL.G4Witnessed.corpus_columns_decided
#print axioms RevL.G4Witnessed.corpus_sites_decided
#print axioms RevL.G4Witnessed.corpus_flip_is_the_undo_column
#print axioms RevL.G4Witnessed.witnessed_site_undo_not_vacuous

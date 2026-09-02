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

-- G2: provision disjointness + requirement closure from the link judgment.
#print axioms RevL.G2.linkOK_provision_disjoint
#print axioms RevL.G2.linkOK_requires_closed

-- G3: layering certificate excludes dependency cycles.
#print axioms RevL.G3.depPath_rank_lt
#print axioms RevL.G3.no_dependency_cycles

-- G4: every admitted mutation carries an inverse or an emit marker.
#print axioms RevL.G4.inverse_or_emit

-- G5: teardown cannot register effects.
#print axioms RevL.G5.teardown_registers_nothing

-- G6: confinement — admitted code reaches only declared keys.
#print axioms RevL.G6.confinement

-- G7: LIFO-completeness — completeness, soundness, and the LIFO equation.
#print axioms RevL.G7.teardown_replays_all
#print axioms RevL.G7.teardown_only_witnessed
#print axioms RevL.G7.teardown_eq_reversed_inverses

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
-- three derived non-vacuity witnesses.
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
#print axioms RevL.CapCeilings.derivation_refuses_unnameable
#print axioms RevL.CapCeilings.derived_ceiling_check_not_subsumed

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

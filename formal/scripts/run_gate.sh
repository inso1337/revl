#!/bin/sh
# The formal gate (make formal, ci.yml — see formal/STATUS.md).
#
# 1. lake build over the Lean package;
# 2. CheckAxioms.lean's `#print axioms` output through the axioms gate
#    (no sorryAx, no project-defined axioms, no missing theorem);
# 3. the harness census (formal/harness/diff_corpus.py).
#
# A missing elan/lake skips LOUDLY — never a false green — matching the
# pre-merge discipline in the Makefile. This lives in a script, not
# Makefile recipe lines, because each recipe line is its own shell: an
# `exit 0` skip-guard inside the Makefile cannot stop the target.
set -e
cd "$(dirname "$0")/.." # formal/

if ! command -v lake >/dev/null 2>&1; then
  echo "SKIP (loud): elan/lake not installed — the formal proofs were NOT checked"
  exit 0
fi

lake build
lake env lean CheckAxioms.lean > .axioms.out 2>&1 || { cat .axioms.out; exit 1; }
cat .axioms.out
python3 scripts/axioms_gate.py \
  RevL.G1.declared_only_access \
  RevL.G2.linkOK_provision_disjoint \
  RevL.G2.linkOK_requires_closed \
  RevL.G2.realm_separation_admitted \
  RevL.G2.same_realm_conflict_refused \
  RevL.G3.depPath_rank_lt \
  RevL.G3.no_dependency_cycles \
  RevL.G3.linkOK_layeredBy_rankOf \
  RevL.G3.linkOK_layered \
  RevL.G3.linkOK_no_cycles \
  RevL.G3.self_provision_refused \
  RevL.G3.mutual_cycle_refused \
  RevL.G3.layering_exists_for_admitted \
  RevL.G4.inverse_or_emit \
  RevL.G5.teardown_registers_nothing \
  RevL.G6.confinement \
  RevL.G7.teardown_replays_all \
  RevL.G7.teardown_only_witnessed \
  RevL.G7.teardown_eq_reversed_inverses \
  RevL.Semantics.teardown_length \
  RevL.G8.boundary_enumerates_emissions \
  RevL.G8.boundary_only_declared \
  RevL.CrossTier.cross_tier_agreement \
  RevL.CrossTier.six_tier_agreement \
  RevL.CrossTier.annotation_necessary \
  RevL.CapCeilings.cap_order_partial \
  RevL.CapCeilings.attenuation_monotone \
  RevL.CapCeilings.lineage_ceiling_le \
  RevL.CapCeilings.spend_within_budget \
  RevL.CapCeilings.budget_never_exceeds_root_ceiling \
  RevL.CapCeilings.confinement_within_ceiling \
  RevL.CapCeilings.no_star_amplification \
  RevL.CapCeilings.parameter_widening_refused \
  RevL.CapCeilings.ceiling_check_not_subsumed \
  RevL.R4.residue_is_exactly_what_remains \
  RevL.R4.abort_leaves_no_residue \
  RevL.R4.residue_complete \
  RevL.R4.residue_sound \
  RevL.R4.txn_run \
  RevL.R4.emit_run \
  RevL.R4.residue_necessary \
  RevL.R4.emission_is_not_replayed \
  RevL.A8.revert_on_failure \
  RevL.A8.trace_reads_back_as_abort \
  RevL.A8.committed_transaction_is_retained \
  RevL.A8.commit_replays_no_inverse \
  RevL.A8.outcome_trichotomy \
  RevL.A8.crash_cut_converges \
  RevL.A8.commit_record_is_the_decision \
  RevL.A8.approved_decides_the_crash_window \
  RevL.A8.fence_before_apply_at_every_cut \
  RevL.A8.at_most_once_across_crash \
  RevL.A8.declared_idempotent_replay_free \
  RevL.A8.double_apply_observable \
  RevL.A8.crash_cut_witness \
  RevL.A8.commit_witness \
  RevL.A8.mixed_disposition_admitted \
  RevL.A8.revert_witness \
  RevL.A8.revert_witness_restores \
  RevL.CapCeilings.derived_held_tokens_are_declared_keys \
  RevL.CapCeilings.derived_reach_is_emit_surface \
  RevL.CapCeilings.unnameable_receiver_is_star \
  RevL.CapCeilings.derived_lineage \
  RevL.CapCeilings.derived_attenuation_monotone \
  RevL.CapCeilings.derived_lineage_ceiling_le \
  RevL.CapCeilings.derived_budget_never_exceeds_root_ceiling \
  RevL.CapCeilings.derived_confinement_within_ceiling \
  RevL.CapCeilings.derived_no_star_amplification \
  RevL.CapCeilings.derivation_non_vacuous \
  RevL.CapCeilings.derivation_refuses_unnameable \
  RevL.CapCeilings.derived_ceiling_check_not_subsumed \
  RevL.G9.origin_persists_or_is_declassified \
  RevL.G9.no_authority_from_untrusted \
  RevL.G9.untrusted_gains_no_authority \
  RevL.G9.declassification_is_the_only_escape \
  RevL.G9.secret_persists \
  RevL.G9.secret_confined \
  RevL.G9.confidential_needs_declassification \
  RevL.G9.flow_declassifiers_granted \
  RevL.G9.untrusted_author_needs_granted_declassifier \
  RevL.G9.declassifier_must_be_declared \
  RevL.G9.taint_surface_within_declared_context \
  RevL.G9.no_untrusted_without_a_declared_source \
  RevL.G9.g9_not_vacuous \
  RevL.G9.secret_rules_not_vacuous \
  RevL.G9.authority_refusal_is_not_universal \
  RevL.G9.sink_rules_are_distinct \
  RevL.G9.secret_refusal_is_load_bearing \
  RevL.Lemmas.reach_mono_fuel \
  RevL.Lemmas.reaches_le \
  RevL.Lemmas.reach_exact \
  RevL.Lemmas.reach_le_trans \
  RevL.Lemmas.reachCaps_sound \
  RevL.Lemmas.reachCaps_complete \
  RevL.G4Classified.inverse_or_emit_classified \
  RevL.G4Classified.program_mutations_carry_inverse_or_marker \
  RevL.G4Classified.reached_crossing_is_classified \
  RevL.G4Classified.reached_crossing_carries_inverse_or_marker \
  RevL.G4Classified.raw_mutation_is_representable \
  RevL.G4Classified.g4_not_vacuous \
  RevL.G4Classified.fn_wrapper_still_crosses \
  RevL.G5Classified.registrations_seq \
  RevL.G5Classified.registrations_zero_iff \
  RevL.G5Classified.inverse_reaches_no_emission \
  RevL.G5Classified.admitted_inverse_registers_nothing \
  RevL.G5Classified.admitted_inverse_body_registers_nothing \
  RevL.G5Classified.pureOnly_run \
  RevL.G5Classified.clean_inverse_run_logs_nothing \
  RevL.G5Classified.admitted_inverse_run_logs_nothing \
  RevL.G5Classified.registrations_depends_on_its_argument \
  RevL.G5Classified.registrations_counts \
  RevL.G5Classified.sneaky_undo_is_refused \
  RevL.G5Classified.fold_must_run_to_stability \
  RevL.G5Classified.sneaky_inverse_run_emits \
  RevL.G5Classified.clean_inverse_run_is_silent \
  RevL.G8Classified.surface_enumerates_reached_crossings \
  RevL.G8Classified.surface_only_declared_crossings \
  RevL.G8Classified.surface_implies_crossing \
  RevL.G8Classified.effect_carrying_emission_is_on_the_surface \
  RevL.G8Classified.surface_agrees_with_an_honest_marker \
  RevL.G8Classified.raw_leak_is_on_the_surface \
  RevL.G8Classified.g8_surface_is_not_universal \
  RevL.G8Classified.witness_surface_traces_to_its_declaration < .axioms.out
python3 harness/diff_corpus.py

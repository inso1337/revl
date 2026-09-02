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
  RevL.G3.depPath_rank_lt \
  RevL.G3.no_dependency_cycles \
  RevL.G4.inverse_or_emit \
  RevL.G5.teardown_registers_nothing \
  RevL.G6.confinement \
  RevL.G7.teardown_replays_all \
  RevL.G7.teardown_only_witnessed \
  RevL.G7.teardown_eq_reversed_inverses \
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
  RevL.G9.secret_rules_not_vacuous < .axioms.out
python3 harness/diff_corpus.py

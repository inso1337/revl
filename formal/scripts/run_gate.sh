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
  RevL.CapCeilings.ceiling_check_not_subsumed < .axioms.out
python3 harness/diff_corpus.py

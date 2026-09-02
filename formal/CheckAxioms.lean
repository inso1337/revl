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

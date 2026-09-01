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

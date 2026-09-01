"""The no-smuggled-assumptions gate for formal/ (make formal, CI).

Parses `#print axioms` output from CheckAxioms.lean and enforces:

- no theorem may depend on `sorryAx` (an unfinished proof);
- no theorem may depend on any axiom outside Lean's three standard
  foundation axioms (propext, Classical.choice, Quot.sound) — i.e. no
  project-defined axiom, the Cantilune-style discipline this project
  follows;
- every theorem name passed on argv must appear in the output — so a
  theorem deleted from CheckAxioms.lean (or one that stopped compiling
  before printing) fails the gate rather than silently leaving it.

Usage: axioms_gate.py THEOREM [THEOREM...] < .axioms.out
"""

import re
import sys

WHITELIST = {"propext", "Classical.choice", "Quot.sound"}

ANY = re.compile(r"'([^']+)' does not depend on any axioms")
DEPS = re.compile(r"'([^']+)' depends on axioms: \[(.*)\]")


def main() -> int:
    text = sys.stdin.read()
    failures = []
    seen = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        if ANY.search(line):
            seen.add(ANY.search(line).group(1))
            continue
        m = DEPS.search(line)
        if not m:
            failures.append(f"unparseable axioms line: {line!r}")
            continue
        name = m.group(1)
        seen.add(name)
        axioms = [a.strip() for a in m.group(2).split(",") if a.strip()]
        if "sorryAx" in axioms:
            failures.append(f"{name}: depends on sorryAx — unfinished proof")
            continue
        bad = [a for a in axioms if a not in WHITELIST]
        if bad:
            failures.append(
                f"{name}: non-standard axioms {bad} "
                f"(whitelist: {sorted(WHITELIST)})"
            )
    for expected in sys.argv[1:]:
        if expected not in seen:
            failures.append(
                f"{expected}: no '#print axioms' line — drift between "
                "CheckAxioms.lean and the registered theorem list"
            )
    if failures:
        print("axioms gate: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("axioms gate: clean — no sorryAx, no project-defined axioms")
    return 0


if __name__ == "__main__":
    sys.exit(main())

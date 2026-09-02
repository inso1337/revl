#!/usr/bin/env python3
"""The non-vacuity registry gate (roadmap item 418, step 8).

Item 418's adversarial review found G4/G5/G6/G8 to be true statements
whose content came from a chosen inductive rather than from the rule they
name, and counted "only 3 of 25 theorems carry non-vacuity evidence". A
theorem whose hypotheses cannot all hold is true and worthless, and the
axioms gate cannot see the difference: `#print axioms` is just as clean on
a vacuous theorem as on a load-bearing one.

So every registered theorem carries a row in `nonvacuity.tsv` naming the
evidence, and this script enforces that the registry stays complete and
honest. A row is

    <theorem>\\t<kind>\\t<witnesses>\\t<note>

with `kind` one of:

* `instance`:    the theorem's hypotheses are jointly satisfiable, and the
                 named witness theorems exhibit a concrete instance that
                 satisfies them.
* `necessity`:   the theorem REFUSES (its conclusion is `False` or a
                 negation), so joint satisfiability is exactly what it
                 denies. The named witnesses show each hypothesis is
                 satisfiable on its own, and that dropping one makes the
                 statement false.
* `contentless`: the theorem is true by definition rather than by any
                 property of its subject. This is a FINDING, not a pass:
                 the named witnesses record what the theorem is actually
                 worth and where the load-bearing statement lives.
* `concrete`:    the theorem is itself a computation on concrete data, so
                 it has no hypotheses to satisfy. A `concrete` row is only
                 accepted when some other row cites it as its witness, so
                 the label cannot be used to opt out.

Checks:

1. the registry covers exactly the theorems `CheckAxioms.lean` registers
   (nothing missing, nothing stale), and `CheckAxioms.lean` and
   `run_gate.sh` register the same set;
2. every witness named is itself a registered theorem, so the witnesses
   are axiom-checked like everything else;
3. no theorem is its own witness;
4. every `concrete` row is cited by at least one non-`concrete` row;
5. every non-`concrete` row names at least one witness, and every row
   carries a note.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FORMAL = Path(__file__).resolve().parent.parent
REGISTRY = FORMAL / "scripts" / "nonvacuity.tsv"
CHECK = FORMAL / "CheckAxioms.lean"
GATE = FORMAL / "scripts" / "run_gate.sh"
STATUS = FORMAL / "STATUS.md"

KINDS = {"instance", "necessity", "contentless", "concrete"}


def registered_from_check() -> list[str]:
    pat = re.compile(r"^#print axioms (RevL\.[A-Za-z0-9_.]+)\s*$")
    out = []
    for line in CHECK.read_text().splitlines():
        m = pat.match(line.strip())
        if m:
            out.append(m.group(1))
    return out


def registered_from_gate() -> list[str]:
    pat = re.compile(r"^(RevL\.[A-Za-z0-9_.]+)")
    out = []
    for line in GATE.read_text().splitlines():
        if not line.startswith("  RevL."):
            continue
        m = pat.match(line.strip())
        if m:
            out.append(m.group(1))
    return out


def main() -> int:
    errors: list[str] = []

    check = registered_from_check()
    gate = registered_from_gate()
    if sorted(check) != sorted(gate):
        only_check = sorted(set(check) - set(gate))
        only_gate = sorted(set(gate) - set(check))
        for n in only_check:
            errors.append(f"{n}: in CheckAxioms.lean but not in run_gate.sh's argv")
        for n in only_gate:
            errors.append(f"{n}: in run_gate.sh's argv but not in CheckAxioms.lean")

    dupes = sorted({n for n in check if check.count(n) > 1})
    for n in dupes:
        errors.append(f"{n}: registered twice in CheckAxioms.lean")

    rows: dict[str, tuple[str, list[str], str]] = {}
    for lineno, raw in enumerate(REGISTRY.read_text().splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != 4:
            errors.append(f"nonvacuity.tsv:{lineno}: expected 4 tab-separated fields")
            continue
        name, kind, witnesses, note = (p.strip() for p in parts)
        if name in rows:
            errors.append(f"nonvacuity.tsv:{lineno}: {name} listed twice")
            continue
        if kind not in KINDS:
            errors.append(
                f"nonvacuity.tsv:{lineno}: {name} has kind {kind!r}, "
                f"expected one of {sorted(KINDS)}"
            )
            continue
        wl = [] if witnesses == "-" else [w.strip() for w in witnesses.split(",")]
        if not note:
            errors.append(f"nonvacuity.tsv:{lineno}: {name} has an empty note")
        rows[name] = (kind, wl, note)

    known = set(check)
    for name in sorted(known - set(rows)):
        errors.append(
            f"{name}: registered in CheckAxioms.lean with no row in "
            f"nonvacuity.tsv. State what makes its hypotheses satisfiable."
        )
    for name in sorted(set(rows) - known):
        errors.append(f"{name}: in nonvacuity.tsv but no longer registered")

    cited: set[str] = set()
    for name, (kind, wl, _note) in sorted(rows.items()):
        if kind == "concrete":
            if wl:
                errors.append(f"{name}: a 'concrete' row names no witnesses, use '-'")
            continue
        if not wl:
            errors.append(f"{name}: kind {kind!r} needs at least one witness")
        for w in wl:
            cited.add(w)
            if w == name:
                errors.append(f"{name}: cannot be its own non-vacuity witness")
            elif w not in known:
                errors.append(
                    f"{name}: witness {w} is not a registered theorem, so it is "
                    f"not axiom-checked"
                )

    for name, (kind, _wl, _note) in sorted(rows.items()):
        if kind == "concrete" and name not in cited:
            errors.append(
                f"{name}: labelled 'concrete' but no other row cites it as a "
                f"witness. 'concrete' is for the base cases the other rows "
                f"rest on, not an exemption."
            )

    status = STATUS.read_text()
    for name in sorted(known):
        if f"`{name}`" not in status:
            errors.append(
                f"{name}: registered and witnessed, but not named in "
                f"STATUS.md. Add its row so the human-readable record says "
                f"what the layer proves."
            )

    if errors:
        print("non-vacuity registry: FAILED")
        for e in errors:
            print(f"  {e}")
        return 1

    tally = {k: 0 for k in KINDS}
    for _name, (kind, _wl, _note) in rows.items():
        tally[kind] += 1
    print(
        f"non-vacuity registry: clean, {len(rows)} registered theorems "
        f"(all named in STATUS.md), "
        f"{tally['instance']} instance, {tally['necessity']} necessity, "
        f"{tally['concrete']} concrete, {tally['contentless']} contentless"
    )
    if tally["contentless"]:
        print(
            "  contentless (true by definition, kept visible rather than "
            "quietly counted as proof):"
        )
        for name, (kind, _wl, note) in sorted(rows.items()):
            if kind == "contentless":
                print(f"    {name}: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

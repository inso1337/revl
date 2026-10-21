"""The proof-status summary for the `formal` job (issue #2745).

`run_gate.sh` appends this script's markdown to `$GITHUB_STEP_SUMMARY` when
that variable is set, on every exit, so the run page shows the proof status
without opening a log. It reads what the gate already produced:

- `.axioms.out` and `.oracle-axioms.out`, the `#print axioms` blocks, for the
  theorem count per guarantee;
- `harness/out/row_census.json`, written by `harness/diff_corpus.py`, for the
  oracle rows per guarantee and the accepted and refused corpus documents per
  row kind.

An input the run never produced is named as not produced, never shown as zero.

Usage: proof_summary.py [--exit-code N]   (run from formal/)
"""

import json
import re
import sys
from pathlib import Path

FORMAL = Path(__file__).resolve().parents[1]
WHITELIST = {"propext", "Classical.choice", "Quot.sound"}
ANY = re.compile(r"'([^']+)' does not depend on any axioms")
DEPS = re.compile(r"'([^']+)' depends on axioms: \[(.*)\]")


def theorem_groups(text: str) -> dict[str, tuple[int, int]]:
    """Theorem count and not-clean count per guarantee, from one `#print
    axioms` block. `RevL.G6.confinement` is group `G6`; `RevLOracle.x` is
    `Oracle`. Not clean: depends on `sorryAx` or on a non-standard axiom."""
    groups: dict[str, list[int]] = {}
    for line in text.splitlines():
        m = ANY.search(line)
        deps = None if m else DEPS.search(line)
        if not (m or deps):
            continue
        name = (m or deps).group(1)
        parts = name.split(".")
        group = "Oracle" if parts[0] == "RevLOracle" else (
            parts[1] if len(parts) > 2 else parts[0])
        bad = 0
        if deps:
            axioms = [a.strip() for a in deps.group(2).split(",") if a.strip()]
            bad = int(any(a not in WHITELIST for a in axioms))
        entry = groups.setdefault(group, [0, 0])
        entry[0] += 1
        entry[1] += bad
    return {g: (n, b) for g, (n, b) in groups.items()}


def _status(code: int | None) -> str:
    if code is None:
        return "unknown"
    if code == 0:
        return "passed"
    if code == 77:
        return "SKIPPED: lake is not installed, the proofs were NOT checked"
    return f"FAILED (exit {code})"


def render(formal: Path, code: int | None) -> str:
    out = ["## Formal proof status", "", f"Gate: {_status(code)}", ""]
    groups: dict[str, list[int]] = {}
    for name in (".axioms.out", ".oracle-axioms.out"):
        path = formal / name
        if path.is_file():
            for g, (n, b) in theorem_groups(
                    path.read_text(encoding="utf-8")).items():
                entry = groups.setdefault(g, [0, 0])
                entry[0] += n
                entry[1] += b
    if not groups:
        out += ["Theorems: not produced (`lake` did not reach the axioms "
                "step, or printed no theorem).", ""]
    else:
        total = sum(n for n, _ in groups.values())
        bad = sum(b for _, b in groups.values())
        out += [f"Theorems: {total} printed, {bad} not axiom-clean.", "",
                "| Guarantee | Theorems | Not axiom-clean |", "|---|---:|---:|"]
        out += [f"| {g} | {n} | {b} |" for g, (n, b) in sorted(groups.items())]
        out.append("")
    census = formal / "harness" / "out" / "row_census.json"
    if not census.is_file():
        out += ["Oracle rows: not produced (the census did not run).", ""]
        return "\n".join(out) + "\n"
    kinds = json.loads(census.read_text(encoding="utf-8"))["row_kinds"]
    out += ["| Oracle row kind | Rows | Accepted documents | "
            "Refused documents | Note |", "|---|---:|---:|---:|---|"]
    for c in kinds.values():
        ex = c.get("exempt")
        note = f"no {ex['side']} document: {ex['why']}" if ex else ""
        out.append(f"| {c['label']} | {c['rows']} | {c['accepted']} | "
                   f"{c['refused']} | {note} |")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    code = int(argv[argv.index("--exit-code") + 1]) if "--exit-code" in argv \
        else None
    sys.stdout.write(render(FORMAL, code))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

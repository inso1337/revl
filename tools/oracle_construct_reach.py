#!/usr/bin/env python3
"""Named reference constructs, the corpus documents that reach them, and a
SHRINK-ONLY ratchet over the ones nothing reaches.

Differential oracles only say that two implementations agree on their input.
This report supplies the missing second half: each oracle names the constructs
its reference implementation can dispatch and records which corpus documents
actually exhibit each one.  The report is generated from the existing oracle
corpora, so adding a document or a reference dispatch is visible in the diff.

WHY THERE IS A LEDGER (issue #1203). Printing an `UNREACHED` line is not a
gate.  Until this ledger existed `--check` fired only when a report was
VACUOUS, so an oracle that can dispatch five constructs and is reached by none
was green, and 249 named gaps across nine oracles rode in a required job that
could not fail on any of them.  `tests/fixtures/oracle_construct_reach_ledger.json`
now records the currently-unreached set per oracle and `--check` holds the tree
to it in both directions:

  * a construct that becomes unreached and is NOT in the ledger is a RED.  That
    is the regression: a reference dispatch added with no corpus document that
    spells it, or a corpus document deleted out from under one that had it.
  * a ledger entry that is no longer unreached is a RED, and the fix is to
    DELETE the entry.  That is what makes the ledger shrink-only: closing a gap
    is free, and the count can never drift back up behind a stale line.

Every remaining entry is a named hole, which is what turns the number into a
budget.  Triaging those entries into a corpus document to add or a dispatch to
declare out of scope is the other half of roadmap item 533, and deliberately
not done by this file: this one only makes sure the number cannot grow.

The VACUITY check stays, and is not redundant with the ratchet.  The ratchet
compares unreached SETS, so it says exactly nothing about an oracle whose
unreached set is empty: let `gate_census`'s reference table or corpus collapse
to nothing and its unreached set stays the empty set it already is, matching an
empty ledger entry, green.  An empty report is the one failure the ratchet
cannot see, so the two checks cover disjoint ground and both run.

Usage:
    python3 tools/oracle_construct_reach.py            # the report
    python3 tools/oracle_construct_reach.py --check    # the gate (exit 1)
    python3 tools/oracle_construct_reach.py --write    # regenerate the ledger
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "tests" / "fixtures" / "oracle_construct_reach_ledger.json"
sys.path.insert(0, str(ROOT / "src"))


def _load_coverage():
    spec = importlib.util.spec_from_file_location(
        "selfhost_coverage", ROOT / "tools" / "selfhost_coverage.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pairs(value: object, path: str, out: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            label = f"{key}={child}" if isinstance(child, str) else None
            if label:
                out.setdefault(label, set()).add(path)
            elif child is True:
                out.setdefault(f"{key}=<true>", set()).add(path)
            _pairs(child, path, out)
    elif isinstance(value, list):
        for child in value:
            _pairs(child, path, out)


def _ir_reach(documents: list[Path]) -> dict[str, set[str]]:
    from revl import compile_files

    reached: dict[str, set[str]] = {}
    for document in documents:
        _pairs(compile_files([str(document)]), document.name, reached)
    return reached


def _corpus_from_test(tier: str) -> list[Path]:
    coverage = _load_coverage()
    return coverage.corpus_documents(tier)


def _lower_constructs() -> set[str]:
    """Extract the named IR dispatch values in the reference lowerer."""
    tree = ast.parse((ROOT / "src/revl/lower.py").read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        left = node.left
        if (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                and left.func.attr == "get" and left.args
                and isinstance(left.args[0], ast.Constant)
                and left.args[0].value in {"kind", "step", "op", "method"}):
            field = left.args[0].value
            values = node.comparators[0]
            values = values.elts if isinstance(values, (ast.Tuple, ast.List)) else [values]
            for value in values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(f"{field}={value.value}")
    return found


def _source_reach(patterns: dict[str, str], documents: list[Path]) -> dict[str, set[str]]:
    reached: dict[str, set[str]] = {}
    for name, pattern in patterns.items():
        for document in documents:
            if re.search(pattern, document.read_text(encoding="utf-8")):
                reached.setdefault(name, set()).add(document.name)
    return reached


# ------------------------------------------------------------ compile oracle

def _compile_corpus() -> list[Path]:
    """The documents `tests/test_selfhost_compile.py` actually parametrises over.

    Read from the module's own corpus tables rather than scraped out of its
    text.  A `"([^"]+\\.rvl)"` scrape over that file collects every quoted `.rvl`
    literal in it, including the `"*.rvl"` glob and the paths its prose
    mentions, and then has to guess a directory for each name: the old spelling
    crossed 130 scraped names with three fixture subdirectories and surveyed
    the 200-odd that happened to resolve, which is not the corpus this oracle
    runs on and drifts from it silently.
    """
    spec = importlib.util.spec_from_file_location(
        "selfhost_compile_oracle", ROOT / "tests" / "test_selfhost_compile.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    corpus = [*module.NATIVE_CORPUS, *module.COMPONENT_CORPUS]
    return sorted({ROOT / "tests" / "fixtures" / subdir / name
                   for _tier, subdir, name in corpus})


def _section_reach(documents: list[Path]) -> dict[str, set[str]]:
    """Which top-level IR SECTIONS the compile corpus populates.

    The compile oracle's reference constructs are the IR sections its native
    chain has to carry end to end (`functions`, `components`, `externs`,
    `types`, `tests`), which is a different vocabulary from the `field=value`
    discriminants the emitter oracles dispatch on.  Measuring them with
    `_ir_reach` compared section NAMES against keys that all contain an `=`,
    so no section could ever match and the row read 5 of 5 unreached however
    good the corpus was -- unmeasurable dressed as a total gap.  A section is
    reached when some corpus document's IR carries a non-empty value for it.
    """
    from revl import compile_files

    reached: dict[str, set[str]] = {}
    for document in documents:
        for section, value in compile_files([str(document)]).items():
            if value:
                reached.setdefault(section, set()).add(document.name)
    return reached


def survey() -> dict[str, dict]:
    coverage = _load_coverage()
    result: dict[str, dict] = {}
    for tier in coverage.TIERS:
        reference = set(coverage.reference_constructs(
            ROOT / "backends" / coverage.TIERS[tier][0] / "emit.py"))
        documents = coverage.corpus_documents(tier)
        reached = _ir_reach(documents)
        result[f"emit_{tier}"] = {
            "corpus": [str(p.relative_to(ROOT)) for p in documents],
            "reference": sorted(reference),
            "reached": {name: sorted(reached[name]) for name in sorted(reference)
                        if name in reached},
            "unreached": sorted(reference - set(reached)),
        }

    lower_documents = _corpus_from_test("py")
    lower_reference = _lower_constructs()
    lower_reached = _ir_reach(lower_documents)
    result["lower_ir"] = {
        "corpus": [str(p.relative_to(ROOT)) for p in lower_documents],
        "reference": sorted(lower_reference),
        "reached": {name: sorted(lower_reached[name]) for name in sorted(lower_reference)
                    if name in lower_reached},
        "unreached": sorted(lower_reference - set(lower_reached)),
    }

    compile_docs = _compile_corpus()
    compile_reference = {"functions", "components", "externs", "types", "tests"}
    compile_reached = _section_reach(compile_docs)
    result["compile"] = {
        "corpus": [str(p.relative_to(ROOT)) for p in compile_docs],
        "reference": sorted(compile_reference),
        "reached": {name: sorted(compile_reached[name]) for name in sorted(compile_reference)
                    if name in compile_reached},
        "unreached": sorted(compile_reference - set(compile_reached)),
    }

    baseline = json.loads((ROOT / "tools" /
                           "gate_reference_census_baseline.json").read_text())
    gate_reference = set(baseline.get("buckets", {}))
    gate_reached = {name: set(case_ids)
                    for name, case_ids in baseline.get("buckets", {}).items()}
    gate_docs = sorted((ROOT / "examples").rglob("*.rvl"))
    result["gate_census"] = {
        "corpus": [str(p.relative_to(ROOT)) for p in gate_docs],
        "reference": sorted(gate_reference),
        "reached": {name: sorted(gate_reached[name]) for name in sorted(gate_reference)
                    if name in gate_reached},
        "unreached": sorted(gate_reference - set(gate_reached)),
    }
    return result


# ------------------------------------------------------------------- ledger

def _ledger() -> dict[str, list[str]]:
    """The recorded unreached sets, minus the `_`-prefixed prose keys JSON has
    no comment syntax for."""
    raw = json.loads(LEDGER.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _vacuity_problems(data: dict) -> list[str]:
    problems: list[str] = []
    for oracle, report in sorted(data.items()):
        if not report["reference"]:
            problems.append(
                f"{oracle}: the reference construct table is EMPTY, so this "
                f"oracle can dispatch nothing and the report is vacuous. The "
                f"ledger cannot see this: an empty unreached set matches an "
                f"empty ledger entry.")
        if not report["corpus"]:
            problems.append(
                f"{oracle}: the corpus is EMPTY, so this oracle exhibits "
                f"nothing and the report is vacuous.")
    return problems


def check(data: dict) -> list[str]:
    problems = _vacuity_problems(data)
    if not LEDGER.exists():
        problems.append(f"{LEDGER.relative_to(ROOT)} is missing; "
                        f"regenerate it with --write and triage the entries.")
        return problems
    ledger = _ledger()
    for oracle in sorted(set(ledger) - set(data)):
        problems.append(
            f"{oracle}: recorded in {LEDGER.name} but the survey no longer "
            f"produces it. Delete the entry.")
    for oracle, report in sorted(data.items()):
        if oracle not in ledger:
            problems.append(
                f"{oracle}: no entry in {LEDGER.name}. A new oracle joins the "
                f"ratchet with its unreached set written down, not silently.")
            continue
        recorded = ledger[oracle]
        if not isinstance(recorded, list) or any(
                not isinstance(name, str) for name in recorded):
            problems.append(f"{oracle}: ledger entry must be a list of strings.")
            continue
        unreached = set(report["unreached"])
        for construct in sorted(unreached - set(recorded)):
            problems.append(
                f"{oracle}: `{construct}` is NEWLY UNREACHED -- the reference "
                f"can dispatch it and no document in this oracle's corpus "
                f"exhibits it, so the oracle asserts nothing about it. Add a "
                f"corpus document that spells it (see the oracle FAIL on it "
                f"first), or record it in {LEDGER.name} on purpose.")
        for construct in sorted(set(recorded) - unreached):
            problems.append(
                f"{oracle}: `{construct}` is recorded unreached in "
                f"{LEDGER.name} but is not unreached any more (the corpus "
                f"reaches it, or the dispatch is gone). Delete the entry: this "
                f"ledger only shrinks.")
    return problems


def write_ledger(data: dict) -> None:
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    prose = {k: v for k, v in ledger.items() if k.startswith("_")}
    prose.setdefault("_about", _ABOUT)
    recorded = {oracle: sorted(report["unreached"])
                for oracle, report in data.items()}
    LEDGER.write_text(json.dumps({**prose, **recorded}, indent=2,
                                 sort_keys=True) + "\n")


_ABOUT = [
    "The constructs each oracle's reference implementation can dispatch and",
    "NO document in that oracle's corpus exhibits. Generated and gated by",
    "tools/oracle_construct_reach.py --check (issue #1203); see that file's",
    "module docstring for what a construct is and why printing them was not",
    "enough.",
    "",
    "This ledger SHRINKS ONLY. A construct that becomes unreached and is not",
    "listed here fails the gate, and an entry that is no longer unreached",
    "fails it too and must be DELETED. So the count can only go down, and",
    "every line left in it is a named hole someone still owes a corpus",
    "document or an out-of-scope decision (roadmap item 533's other half).",
    "",
    "It records NAMES only -- never counts, totals or line numbers -- so it",
    "is identical under CI's python 3.11 and a 3.14 developer venv, and a",
    "`--write` from either is a safe diff.",
]


def report(data: dict) -> None:
    for oracle, entry in data.items():
        print(f"{oracle}: {len(entry['reference'])} reference constructs, "
              f"{len(entry['unreached'])} unreached")
        for construct in entry["unreached"]:
            print(f"  UNREACHED {construct}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail on a newly unreached construct, a stale "
                             "ledger entry, or a vacuous report")
    parser.add_argument("--write", action="store_true",
                        help="regenerate the unreached ledger from this tree")
    parser.add_argument("--json", type=Path, help="write the report as JSON")
    args = parser.parse_args(argv)
    data = survey()
    if args.write:
        write_ledger(data)
        print(f"wrote {LEDGER.relative_to(ROOT)}")
        return 0
    if args.json:
        args.json.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    else:
        report(data)
    if args.check:
        problems = check(data)
        for problem in problems:
            print(f"FAIL {problem}")
        if problems:
            print(f"\n{len(problems)} construct-reach problem(s). The ledger is "
                  f"{LEDGER.relative_to(ROOT)}.")
            return 1
        print(f"construct reach matches {LEDGER.relative_to(ROOT)}: "
              f"{sum(len(e['unreached']) for e in data.values())} named gaps, "
              f"none new.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

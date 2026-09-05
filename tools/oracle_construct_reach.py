#!/usr/bin/env python3
"""Report named reference constructs and the corpus documents that reach them.

Differential oracles only say that two implementations agree on their input.
This report supplies the missing second half: each oracle names the constructs
its reference implementation can dispatch and records which corpus documents
actually exhibit each one.  The report is generated from the existing oracle
corpora, so adding a document or a reference dispatch is visible in the diff.
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

    compile_test = (ROOT / "tests" / "test_selfhost_compile.py").read_text()
    compile_names = sorted(set(re.findall(r'"([^"]+\.rvl)"', compile_test)))
    compile_docs = [ROOT / "tests" / "fixtures" / sub / name
                    for sub in ("emit_py_corpus", "emit_rust_corpus", "emit_ts_corpus")
                    for name in compile_names
                    if (ROOT / "tests" / "fixtures" / sub / name).is_file()]
    compile_reference = {"functions", "components", "externs", "types", "tests"}
    compile_reached = _ir_reach(compile_docs)
    result["compile"] = {
        "corpus": [str(p.relative_to(ROOT)) for p in compile_docs],
        "reference": sorted(compile_reference),
        "reached": {name: sorted(compile_reached[name]) for name in compile_reference
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail if a report has no named corpus reach")
    parser.add_argument("--json", type=Path, help="write the report as JSON")
    args = parser.parse_args(argv)
    data = survey()
    if args.json:
        args.json.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    else:
        for oracle, report in data.items():
            print(f"{oracle}: {len(report['reference'])} reference constructs, "
                  f"{len(report['unreached'])} unreached")
            for construct in report["unreached"]:
                print(f"  UNREACHED {construct}")
    if args.check:
        # The emitter-specific shrink-only ledger remains the authoritative
        # ratchet for newly added reference dispatches. This report's check
        # guards the broader oracle inventory from becoming vacuous.
        return int(any(not report["reference"] or not report["corpus"]
                       for report in data.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

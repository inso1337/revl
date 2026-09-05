#!/usr/bin/env python3
"""Reproduce the whole-tree self-host differential experiment (item 429).

Run from any directory, without installing revl:
    python3 tools/selfhost_differential_survey.py --tiers ts java --json survey.json
    python3 tools/selfhost_differential_survey.py --tiers py --select-cover
    python3 tools/selfhost_differential_survey.py --tiers rust --documents \
        tests/fixtures/emit_rust_corpus/reserved_names.rvl

Each document is compiled with compile_files([absolute_path]); that reference IR
is handed to reference.emit(ir) and the compiled port's emit entry, exactly as
the oracles do. WASM compares the reference's "functions" output, not unrelated
component modules. A missing functions projection is a reference rejection of
this oracle's slice, not byte agreement.

--select-cover traces both emitters and greedily selects extra AGREEING documents
by their union of reached statements beyond the oracle's own CORPUS list.
For WASM, documents with component output are retained in survey results but
excluded from automatic selection because the oracle compares only ``functions``.
Reference and emitted-port line identities are separate; port source-line
provenance is not available. Suggestions never edit corpora or ledgers.

This is evidence for human triage, NOT proof either side is correct. No generated
program is run. Known compiler/backend refusals and explicit port runtime errors
are recorded; unexpected compiler/reference errors and interrupts abort the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from tools import selfhost_coverage as constructs  # noqa: E402
from tools import selfhost_line_coverage as lines  # noqa: E402

STATUSES = (
    "compile_rejection", "reference_rejection", "port_refusal", "port_error",
    "byte_divergence", "byte_agreement",
)
EXCLUDED_DIRS = {
    "venv", "env", "node_modules", "target", "build", "dist", "__pycache__",
    "_gen", "gen", "out", "real_gen", "real_out", "site-packages",
}
PORT_ERRORS = (AssertionError, AttributeError, IndexError, KeyError,
               TypeError, ValueError, RuntimeError, ArithmeticError)


def documents_in_tree(root: Path = ROOT) -> list[Path]:
    """Stable source-only walk; never follow symlinks or dependency/build trees."""
    found = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d not in EXCLUDED_DIRS
                         and not (Path(directory) / d).is_symlink()
                         and not (Path(directory) / d / "pyvenv.cfg").exists())
        found.extend(Path(directory) / name for name in sorted(files)
                     if name.endswith(".rvl") and not (Path(directory) / name).is_symlink())
    return sorted(found)


def document_paths(documents, root: Path = ROOT) -> list[Path]:
    """Normalize explicit inputs to unique repository-relative identities."""
    normalized = set()
    for document in documents:
        path = Path(document)
        path = (path if path.is_absolute() else root / path).resolve()
        path.relative_to(root.resolve())  # outside-root documents are not reproducible
        if path.suffix != ".rvl" or not path.is_file():
            raise ValueError(f"not an existing .rvl document: {document}")
        normalized.add(path)
    if not normalized:
        raise ValueError("the survey needs at least one .rvl document")
    return sorted(normalized)


def _error(exc: Exception) -> dict:
    return {"type": type(exc).__name__, "message": str(exc).replace(str(ROOT) + "/", "")}


def _fingerprint(text: str) -> dict:
    encoded = text.encode("utf-8")
    return {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}


def compare(ir: dict, tier: str, reference, port) -> dict:
    """One oracle invocation; rejection is never represented as agreement."""
    try:
        want = reference.emit(ir)
    except reference.EmitError as exc:
        return {"status": "reference_rejection", "error": _error(exc)}
    if tier == "wasm":
        if "functions" not in want:
            return {"status": "reference_rejection", "error": {
                "type": "OracleSlice", "message": "WASM reference has no functions module"}}
        want = want["functions"]
    if not isinstance(want, str):
        raise TypeError(f"{tier} reference returned {type(want).__name__}, expected str")
    try:
        got = port(ir)
    except PORT_ERRORS as exc:
        return {"status": "port_error", "reference": _fingerprint(want), "error": _error(exc)}
    if not isinstance(got, str):
        return {"status": "port_error", "reference": _fingerprint(want), "error": {
            "type": "OutputType", "message": f"port returned {type(got).__name__}, expected str"}}
    result = {"reference": _fingerprint(want), "selfhost": _fingerprint(got)}
    if got == want:
        result["status"] = "byte_agreement"
    else:
        markers = sorted(set(re.findall(r"<<UNSUPPORTED[^>]*>>", got)))
        result["status"] = "port_refusal" if markers else "byte_divergence"
        if markers:
            result["markers"] = markers
        a, b = want.encode("utf-8"), got.encode("utf-8")
        offset = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y),
                      min(len(a), len(b)))
        result["first_difference"] = {
            "byte": offset, "reference": a[max(0, offset - 40):offset + 120].decode(
                "utf-8", errors="replace"),
            "selfhost": b[max(0, offset - 40):offset + 120].decode("utf-8", errors="replace"),
        }
    return result


def selection_exclusion(tier: str, ir: dict) -> str | None:
    """Explain why a successful survey row cannot provide selection evidence."""
    if tier == "wasm" and ir.get("components"):
        return "wasm_functions_projection_discards_component_output"
    return None


def select_cover(rows: list[dict], baseline: list[dict]) -> dict:
    """Deterministic greedy set cover, with lexical tie-breaking (not optimal)."""
    def reached(row):
        return {(side, number) for side, numbers in row["reached"].items()
                for number in numbers}

    covered = set().union(*(reached(row) for row in baseline
                            if not row.get("selection_excluded"))) if baseline else set()
    initial = len(covered)
    candidates = {row["document"]: reached(row) for row in rows
                  if row["status"] == "byte_agreement" and not row["in_corpus"]
                  and not row.get("selection_excluded")}
    selected = []
    while candidates:
        name = min(candidates, key=lambda n: (-len(candidates[n] - covered), n))
        gain = candidates.pop(name) - covered
        if not gain:
            break
        selected.append({"document": name, "new_reference_lines": sum(
            side == "reference" for side, _ in gain), "new_selfhost_lines": sum(
                side == "selfhost" for side, _ in gain)})
        covered.update(gain)
    return {"baseline_lines": initial, "selected": selected,
            "additional_lines": len(covered) - initial}


def survey(tiers=None, documents=None, *, select=False) -> dict:
    tiers = list(dict.fromkeys(lines.TIERS if tiers is None else tiers))
    if not tiers or set(tiers) - set(lines.TIERS):
        raise ValueError(f"select one or more known tiers: {', '.join(lines.TIERS)}")
    paths = document_paths(documents_in_tree() if documents is None else documents)
    corpus = {tier: document_paths(constructs.corpus_documents(tier)) for tier in tiers}
    requested = set(paths)
    workloads = {tier: sorted(requested | (set(corpus[tier]) if select else set()))
                 for tier in tiers}
    results = []
    baselines = {tier: [] for tier in tiers}
    with tempfile.TemporaryDirectory(prefix="selfhost-survey-") as temporary, ExitStack() as stack:
        scratch = Path(temporary).resolve()
        reference_paths = {tier: ROOT / "backends" / lines.TIERS[tier] / "emit.py"
                           for tier in tiers}
        port_paths = {tier: scratch / f"selfhost_emit_{tier}.py" for tier in tiers}
        cov = None
        if select:
            import coverage  # noqa: PLC0415
            # Per-document attribution requires dynamic contexts. Use the
            # compatible Python tracer because sysmon does not support
            # switch_context(), and older coverage releases have no ``core`` arg.
            cov = coverage.Coverage(
                data_file=None,
                include=[str(p) for p in [*reference_paths.values(), *port_paths.values()]],
                timid=True,
            )
            cov.start()
            stack.callback(cov.stop)
            cov.switch_context("setup")
        references = {tier: lines.load_reference(tier) for tier in tiers}
        python_reference = references.get("py") or lines.load_reference("py")
        ports = {tier: lines._entry(stack.enter_context(
            lines.selfhost_module(tier, python_reference, scratch))[0], tier)
                 for tier in tiers}
        compiled = {}
        for document in sorted(set().union(*map(set, workloads.values()))):
            try:
                compiled[document] = compile_files([str(document)])
            except RevlError as exc:
                compiled[document] = exc
        contexts = []
        for tier in tiers:
            for document in workloads[tier]:
                identity = document.relative_to(ROOT).as_posix()
                context = f"{tier}:{identity}"
                if cov:
                    cov.switch_context(context)
                ir = compiled[document]
                outcome = ({"status": "compile_rejection", "error": _error(ir)}
                           if isinstance(ir, RevlError)
                           else compare(ir, tier, references[tier], ports[tier]))
                row = {"tier": tier, "document": identity,
                       "in_corpus": document in corpus[tier], **outcome}
                excluded = selection_exclusion(tier, ir) if isinstance(ir, dict) else None
                if excluded:
                    row["selection_excluded"] = excluded
                contexts.append((context, row))
                if document in requested:
                    results.append(row)
                if document in corpus[tier]:
                    baselines[tier].append(row)
        if cov:
            cov.stop()
            measured = cov.get_data()
            port_statements = {}
            reference_statements = {}
            for tier in tiers:
                declared = set(re.findall(r"^\s*(?:pub\s+)?fn\s+(\w+)",
                                         (ROOT / "selfhost" / f"emit_{tier}.rvl").read_text(), re.M))
                owners = lines._owners(port_paths[tier])
                port_statements[tier] = {n for n in cov.analysis2(str(port_paths[tier]))[1]
                                         if owners.get(n) in declared}
                reference_statements[tier] = set(cov.analysis2(str(reference_paths[tier]))[1])
            for context, row in contexts:
                tier = row["tier"]
                def context_lines(path, statements):
                    by_line = measured.contexts_by_lineno(str(path))
                    return sorted(line for line in statements
                                  if context in by_line.get(line, ()))
                row["reached"] = {
                    side: context_lines(path, statements)
                    for side, path, statements in (
                        ("reference", reference_paths[tier], reference_statements[tier]),
                        ("selfhost", port_paths[tier], port_statements[tier]))}
                row["missing"] = {
                    side: sorted(set(statements) - set(row["reached"][side]))
                    for side, statements in (("reference", reference_statements[tier]),
                                              ("selfhost", port_statements[tier]))}
    report = {
        "schema": 1, "scope": "reference IR -> reference and compiled-selfhost emitters",
        "warning": "Byte agreement and line reach are evidence, not correctness or completed triage.",
        "documents": [p.relative_to(ROOT).as_posix() for p in paths],
        "tiers": tiers, "results": results,
        "counts": {tier: {status: sum(r["tier"] == tier and r["status"] == status
                                     for r in results) for status in STATUSES} for tier in tiers},
    }
    if select:
        report["selection"] = {}
        for tier in tiers:
            failures = [row for row in baselines[tier] if row["status"] != "byte_agreement"]
            report["selection"][tier] = {
                **select_cover([r for r in results if r["tier"] == tier], baselines[tier]),
                "baseline_failures": failures,
                "baseline_documents": [row["document"] for row in baselines[tier]],
            }
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tiers", nargs="+", choices=list(lines.TIERS))
    parser.add_argument("--documents", nargs="+", help="explicit repository-relative .rvl paths")
    parser.add_argument("--select-cover", action="store_true", help="suggest extra agreeing corpus documents")
    parser.add_argument("--json", type=Path, help="write the complete evidence report")
    args = parser.parse_args(argv)
    report = survey(args.tiers, args.documents, select=args.select_cover)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    counts = Counter(row["status"] for row in report["results"])
    print("Survey evidence: " + ", ".join(f"{s}={counts[s]}" for s in STATUSES), file=sys.stderr)
    # Divergence is expected survey data, not a failed execution or a green gate.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Count the documents whose component the go tier's pure typed-core path DROPS.

    python3 tools/go_component_drop_census.py            # the whole tree
    python3 tools/go_component_drop_census.py --json OUT # machine-readable

Why this exists
---------------
`backends/go/emit.py::emit` routes an ir_version-3 document to the pure
typed-core path when it carries a top-level `fn`, `type`, `extern` or plain
`test` (and holds no stream, and declares no lifecycle test). That path renders
ordinary Go for the declarations and **none** of the components it routes past.

That is right for an incidental component — scaffolding around the record or
pure-fn shape a corpus case is actually about — and it is a fail-open for every
other one. Issue #721 reached it by migrating a revl-harness route to a
provide-method if-chain: the document declares a helper `fn` beside the
component, so go answered with a package that had no services, no component and
no routes, and raised nothing.

This tool says how many documents are in that state, so the figure in the issue
is reproducible rather than remembered. It reads the SAME predicate `emit()`
branches on (`has_top_level` / `has_lifecycle` / `_document_holds_stream`) off
the go emitter itself rather than restating it, so it cannot drift from the
routing it describes.

Issue #1321 gave the fork the third arm it was missing: a document with an
observable component is carried on `_emit_v3_combined` (the declarations AND
the components, in one package) rather than routed to the pure path. So the
SILENT figure is the regression gate now, not a live count: it was 126 at
ae8533ce3 and it is 0 on a tree that carries. What the tool still answers for
is the question the issue asked: does the go tier ever return Go source with a
declared component missing from it.

A component counts as OBSERVABLE when dropping it loses something the author
wrote: it has an activation body, or a provide step carrying methods. An empty
component, or a `provides` with no methods, renders to nothing anyone can call
and is not counted.

Two figures come out, and the difference between them matters:

  ROUTED PAST  the routing predicate reaches the document and it declares an
               observable component. This is the upper bound: the set of
               documents whose component the pure path would drop.
  SILENT       the emitter additionally RETURNS, and the component's name is
               absent from the Go it returned. This is the fail-open count: a
               caller got an artifact, it compiles, and the component is gone.

The gap between them is documents the go tier refuses anyway for its own
unrelated reason (a missing `@go` extern body, an unlowerable type). Those are
not part of the finding (the tier already says no), so the finding is the
SILENT number.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

#: Directories with no `.rvl` document worth compiling, plus the rejection
#: corpus (every document there is meant to fail the frontend).
SKIP_PARTS = {".git", "node_modules", "target", "__pycache__", ".venv",
              ".venv-lane", "rejections"}


def _go_emitter():
    spec = importlib.util.spec_from_file_location(
        "go_emit_for_census", ROOT / "backends" / "go" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _observable(comp: dict) -> bool:
    if comp.get("body"):
        return True
    return any(step.get("methods") for step in comp.get("provides") or [])


def census(emit_probe: bool = True) -> dict:
    go = _go_emitter()
    seen = routed = with_components = uncompilable = 0
    silent: list[str] = []
    refused_for_another_reason: list[str] = []
    hits: list[str] = []
    for path in sorted(ROOT.rglob("*.rvl")):
        rel = path.relative_to(ROOT)
        if SKIP_PARTS & set(rel.parts):
            continue
        seen += 1
        try:
            ir = compile_files([str(path)])
        except Exception:
            uncompilable += 1
            continue
        if ir.get("ir_version") != 3 or not ir.get("components"):
            continue
        with_components += 1
        has_top_level = bool(ir.get("functions") or ir.get("types")
                             or ir.get("externs") or ir.get("tests"))
        has_lifecycle = any(t.get("lifecycle") for t in (ir.get("tests") or []))
        try:
            holds_stream = go._document_holds_stream(ir)
        except Exception:
            holds_stream = False
        if holds_stream or not has_top_level or has_lifecycle:
            continue
        # this document takes the pure path, which renders no component
        if not any(_observable(comp) for comp in ir["components"]):
            continue
        routed += 1
        hits.append(str(rel))
        if not emit_probe:
            continue
        try:
            out = go.emit(ir)
        except Exception:
            # the tier says no anyway, for its own unrelated reason
            refused_for_another_reason.append(str(rel))
            continue
        if any(comp["name"] not in out for comp in ir["components"]):
            silent.append(str(rel))
    return {
        "documents_walked": seen,
        "uncompilable_skipped": uncompilable,
        "v3_documents_with_components": with_components,
        "routed_past_an_observable_component": routed,
        "silently_dropped": len(silent),
        "refused_for_an_unrelated_reason": len(refused_for_another_reason),
        "routed_documents": hits,
        "silent_documents": silent,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("--no-emit-probe", action="store_true",
                    help="report the routing upper bound only, without calling "
                         "the emitter")
    args = ap.parse_args()
    result = census(emit_probe=not args.no_emit_probe)
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    print(f"{result['documents_walked']} .rvl document(s) walked, "
          f"{result['uncompilable_skipped']} not compilable and skipped")
    print(f"{result['v3_documents_with_components']} ir_version-3 document(s) "
          f"declare a component")
    print(f"{result['routed_past_an_observable_component']} of them declare an "
          f"observable component the pure path would drop (upper bound)")
    print(f"{result['refused_for_an_unrelated_reason']} of those the go tier "
          f"refuses anyway, for its own unrelated reason")
    print(f"{result['silently_dropped']} are SILENT: go returns Go source with "
          f"the component absent and raises nothing")
    for name in result["silent_documents"]:
        print("  ", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

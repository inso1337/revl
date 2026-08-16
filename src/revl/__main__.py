"""CLI: python -m revl compile <files...> [-o out.json]"""

from __future__ import annotations

import argparse
import json
import sys

from .compiler import compile_files
from .errors import RevlError


def _boundary(ir: dict) -> dict:
    """G8: the enumerable boundary surface per component — every emission
    call site (including teardown-position ones), compensation counts, and
    iteration boundaries."""
    report: dict[str, dict] = {}
    for comp in ir.get("components") or []:
        stats = {"emissions": set(), "compensated": 0, "awaits": 0}

        def walk_expr(node, comp=comp, stats=stats):
            if isinstance(node, dict):
                target = node.get("target")
                if node.get("kind") == "call" and isinstance(target, dict) and target.get("kind") == "req":
                    service = (comp.get("requires") or {}).get(target.get("name"))
                    spec = (((ir.get("services") or {}).get(service) or {}).get("methods") or {}).get(node.get("method")) or {}
                    if spec.get("emission"):
                        stats["emissions"].add(f"{target['name']}.{node['method']}")
                for value in node.values():
                    walk_expr(value)
            elif isinstance(node, list):
                for value in node:
                    walk_expr(value)

        def walk_steps(steps, stats=stats):
            for step in steps:
                kind = step.get("step")
                if kind == "await":
                    stats["awaits"] += 1
                if kind == "emit" and step.get("compensate") is not None:
                    stats["compensated"] += 1
                walk_expr(step)
                if kind == "provide":
                    for method in step.get("methods") or []:
                        walk_steps(method.get("body") or [])

        walk_steps(comp.get("body") or [])
        report[comp["name"]] = {
            "emissions": sorted(stats["emissions"]),
            "compensated": stats["compensated"],
            "awaits": stats["awaits"],
        }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="revl")
    sub = parser.add_subparsers(dest="command", required=True)

    cmd = sub.add_parser("compile", help="compile .rvl files to a backend IR document")
    cmd.add_argument("files", nargs="+")
    cmd.add_argument("-o", "--output", default=None, help="output path (default: stdout)")

    audit = sub.add_parser("audit", help="composition manifest + G8 boundary surface")
    audit.add_argument("files", nargs="+")
    audit.add_argument("--json", action="store_true", help="machine-readable output")

    args = parser.parse_args(argv)

    try:
        ir = compile_files(args.files)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.command == "audit":
        boundary = _boundary(ir)
        manifest = ir.get("manifest") or {}
        if args.json:
            print(json.dumps({"manifest": manifest, "boundary": boundary}, indent=2))
            return 0
        print("composition (providers first):", " -> ".join(manifest.get("loadOrder") or []))
        for entry in manifest.get("components") or []:
            name = entry["name"]
            print(f"\ncomponent {name}  ({entry.get('file') or '?'})")
            print(f"  requires: {', '.join(entry.get('inject') or []) or '—'}")
            print(f"  provides: {', '.join(entry.get('provides') or []) or '—'}")
            stats = boundary.get(name)
            if stats is None:
                continue
            if stats["emissions"] or stats["awaits"]:
                detail = []
                if stats["emissions"]:
                    detail.append(f"emissions: {', '.join(stats['emissions'])}"
                                  f" ({stats['compensated']} compensated)")
                if stats["awaits"]:
                    detail.append(f"iteration boundaries: {stats['awaits']}")
                print(f"  boundary: {'; '.join(detail)}")
            else:
                print("  boundary: none — fully revertible (G8)")
        return 0

    rendered = json.dumps(ir, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""CLI: python -m revl compile <files...> [-o out.json]"""

from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path

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


def _run_tests(ir: dict) -> int:
    """Emit the IR to cordis-py and run its `test` units in-process."""
    tests = ir.get("tests") or []
    if not tests:
        print("no tests to run")
        return 0

    backend_dir = str(Path(__file__).resolve().parents[2] / "backends" / "python")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    import emit  # noqa: PLC0415 — backend import happens after path setup

    module = types.ModuleType("revl_test_module")
    source = emit.emit(ir)
    exec(compile(source, "<revl-test>", "exec"), module.__dict__)
    entries = getattr(module, "REVL_TESTS", None) or []
    if not entries:
        print("no tests emitted by the backend")
        return 0

    failures = 0
    for name, test_fn in entries:
        try:
            test_fn()
        except AssertionError as error:
            failures += 1
            message = str(error).strip() or "assertion failed"
            print(f"FAIL {name}: {message}")
        except Exception as error:  # noqa: BLE001 — test runner reports every failure
            failures += 1
            print(f"FAIL {name}: {type(error).__name__}: {error}")
        else:
            print(f"PASS {name}")

    if failures:
        print(f"{failures} of {len(entries)} test(s) failed", file=sys.stderr)
        return 1
    print(f"{len(entries)} test(s) passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="revl")
    sub = parser.add_subparsers(dest="command", required=True)

    cmd = sub.add_parser("compile", help="compile .rvl files to a backend IR document")
    cmd.add_argument("files", nargs="+")
    cmd.add_argument("-o", "--output", default=None, help="output path (default: stdout)")

    audit = sub.add_parser("audit", help="composition manifest + G8 boundary surface")
    audit.add_argument("files", nargs="+")
    audit.add_argument("--json", action="store_true", help="machine-readable output")

    test = sub.add_parser("test", help="compile and run `test` blocks")
    test.add_argument("files", nargs="+")

    args = parser.parse_args(argv)

    try:
        ir = compile_files(args.files)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.command == "test":
        return _run_tests(ir)

    if args.command == "audit":
        boundary = _boundary(ir)
        manifest = ir.get("manifest") or {}
        if args.json:
            print(json.dumps({"manifest": manifest, "boundary": boundary}, indent=2))
            return 0
        print("composition (providers first):", " -> ".join(manifest.get("loadOrder") or []))
        for entry in manifest.get("components") or []:
            name = entry["name"]
            isolate = entry.get("isolate") or {}

            def _decorate(key: str) -> str:
                return f"{key}@{isolate[key]}" if key in isolate else key

            print(f"\ncomponent {name}  ({entry.get('file') or '?'})")
            print(f"  requires: {', '.join(_decorate(k) for k in entry.get('inject') or []) or '—'}")
            print(f"  provides: {', '.join(_decorate(k) for k in entry.get('provides') or []) or '—'}")
            for key, metadata in (entry.get("intercept") or {}).items():
                print(f"  intercept: {key} {metadata}")
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

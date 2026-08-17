"""CLI: python -m revl compile <files...> [-o out.json]"""

from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path

from .compiler import compile_files
from .errors import RevlError
from .fmt import migrate_source


def _fn_call_names(node, out: set) -> None:
    """Collect callable references in an IR tree: component positions use
    `{"kind": "fn", "name"}`, fn bodies use `{"kind": "call", "callee":
    {"kind": "var", ...}}`. Non-callable vars are filtered by the caller
    against known fn/extern names."""
    if isinstance(node, dict):
        if node.get("kind") == "fn" and isinstance(node.get("name"), str):
            out.add(node["name"])
        if node.get("kind") == "call":
            callee = node.get("callee")
            if isinstance(callee, dict) and callee.get("kind") == "var" \
                    and isinstance(callee.get("name"), str):
                out.add(callee["name"])
        for value in node.values():
            _fn_call_names(value, out)
    elif isinstance(node, list):
        for value in node:
            _fn_call_names(value, out)


def _extern_reachability(ir: dict) -> dict[str, set]:
    """fn name -> transitively reachable extern names (host-code surface)."""
    externs = {ext["name"] for ext in ir.get("externs") or []}
    functions = ir.get("functions") or {}
    if not isinstance(functions, dict):
        functions = {fn.get("name"): fn for fn in functions}

    direct: dict[str, set] = {}
    for name, decl in functions.items():
        calls: set = set()
        _fn_call_names(decl, calls)
        direct[name] = calls

    reach: dict[str, set] = {}

    def resolve(name: str, trail: set) -> set:
        if name in reach:
            return reach[name]
        if name in trail:
            return set()
        found: set = set()
        for callee in direct.get(name, set()):
            if callee in externs:
                found.add(callee)
            elif callee in direct:
                found |= resolve(callee, trail | {name})
        reach[name] = found
        return found

    for name in direct:
        resolve(name, set())
    reach["__externs__"] = externs
    return reach


def _boundary(ir: dict) -> dict:
    """G8: the enumerable boundary surface per component — every emission
    call site (including teardown-position ones), compensation counts,
    iteration boundaries, and reachable host code (externs, transitively
    through functions)."""
    reach = _extern_reachability(ir)
    externs = reach["__externs__"]
    extern_class = {ext["name"]: ext for ext in ir.get("externs") or []}

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

        called: set = set()
        _fn_call_names(comp.get("body") or [], called)
        host: set = set()
        for name in called:
            if name in externs:
                host.add(name)
            else:
                host |= reach.get(name, set())

        report[comp["name"]] = {
            "emissions": sorted(stats["emissions"]),
            "compensated": stats["compensated"],
            "awaits": stats["awaits"],
            "externs": [
                {"name": name,
                 "class": extern_class.get(name, {}).get("class"),
                 "backends": sorted((extern_class.get(name, {}).get("bodies") or {}).keys())}
                for name in sorted(host)
            ],
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
    # Register the module before exec: the emitter renders record types as
    # @dataclass, and dataclasses._process_class resolves each field via
    # sys.modules[cls.__module__]. cls.__module__ is this module's name, so an
    # unregistered module makes that lookup return None and raises
    # AttributeError on any file that declares a record type (CPython 3.12+).
    sys.modules[module.__name__] = module
    source = emit.emit(ir)
    try:
        exec(compile(source, "<revl-test>", "exec"), module.__dict__)
    finally:
        sys.modules.pop(module.__name__, None)
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


def _run_fmt(args: argparse.Namespace) -> int:
    """`revl fmt --migrate`: rewrite 1.x `$` interpolation to 2.0 templates."""
    if args.output and len(args.files) != 1:
        print("error: `fmt --migrate -o` expects exactly one input file", file=sys.stderr)
        return 1

    for path_str in args.files:
        path = Path(path_str)
        try:
            original = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            print(f"error: cannot read {path_str}: {error}", file=sys.stderr)
            return 1

        migrated, warnings = migrate_source(original, str(path))
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)

        if args.output:
            try:
                Path(args.output).write_bytes(migrated.encode("utf-8"))
            except OSError as error:
                print(f"error: cannot write {args.output}: {error}", file=sys.stderr)
                return 1
        elif migrated != original:
            try:
                path.write_bytes(migrated.encode("utf-8"))
            except OSError as error:
                print(f"error: cannot write {path_str}: {error}", file=sys.stderr)
                return 1

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

    fmt = sub.add_parser("fmt", help="format .rvl sources (migration §9)")
    fmt.add_argument(
        "--migrate",
        action="store_true",
        required=True,
        help="rewrite 1.x `$` interpolation to backtick templates",
    )
    fmt.add_argument("files", nargs="+")
    fmt.add_argument(
        "-o",
        "--output",
        default=None,
        help="write migrated source to this path instead of in place (single input)",
    )

    test = sub.add_parser("test", help="compile and run `test` blocks")
    test.add_argument("files", nargs="+")

    args = parser.parse_args(argv)

    if args.command == "fmt":
        return _run_fmt(args)

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
        declared_externs = [
            {"name": ext["name"], "class": ext.get("class"),
             "backends": sorted((ext.get("bodies") or {}).keys())}
            for ext in ir.get("externs") or []
        ]
        if args.json:
            print(json.dumps({"manifest": manifest, "boundary": boundary,
                              "externs": declared_externs}, indent=2))
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
            host = stats.get("externs") or []
            if stats["emissions"] or stats["awaits"] or host:
                detail = []
                if stats["emissions"]:
                    detail.append(f"emissions: {', '.join(stats['emissions'])}"
                                  f" ({stats['compensated']} compensated)")
                if stats["awaits"]:
                    detail.append(f"iteration boundaries: {stats['awaits']}")
                if host:
                    rendered = ", ".join(
                        f"{e['name']} ({e['class']}, {'+'.join(e['backends']) or 'no bodies'})"
                        for e in host)
                    detail.append(f"host code: {rendered}")
                print(f"  boundary: {'; '.join(detail)}")
            else:
                print("  boundary: none — fully revertible (G8)")
        if declared_externs:
            print("\nexterns (verbatim host code — unchecked inside, typed at the boundary):")
            for ext in declared_externs:
                print(f"  {ext['name']}  [{ext['class']}]  backends: "
                      f"{', '.join(ext['backends']) or '—'}")
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

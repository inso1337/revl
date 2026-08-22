"""CLI: python -m revl compile <files...> [-o out.json]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compiler import compile_files
from .distribute import distributability
from .diagnostics import explain, obligations, report
from .errors import RevlError
from .fmt import migrate_source
from .holes import render as render_holes
from .run import KNOWN_BACKENDS, run_command
from .test import test_command


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
    call site (including teardown-position ones), the capabilities each of
    those call sites may cross (docs/capabilities.md), compensation counts,
    iteration boundaries, and reachable host code (externs, transitively
    through functions).

    The capability map is what turns "this component emits" into "this
    component reaches *these* boundaries": each emission is annotated with the
    scope its declaration carries, and `*` is an unscoped `emission` — an
    operation whose declaration makes no promise about where it goes."""
    reach = _extern_reachability(ir)
    externs = reach["__externs__"]
    extern_class = {ext["name"]: ext for ext in ir.get("externs") or []}

    report: dict[str, dict] = {}
    for comp in ir.get("components") or []:
        stats = {"emissions": set(), "compensated": 0, "awaits": 0, "capabilities": {}}

        def walk_expr(node, comp=comp, stats=stats):
            if isinstance(node, dict):
                target = node.get("target")
                if node.get("kind") == "call" and isinstance(target, dict) and target.get("kind") == "req":
                    service = (comp.get("requires") or {}).get(target.get("name"))
                    spec = (((ir.get("services") or {}).get(service) or {}).get("methods") or {}).get(node.get("method")) or {}
                    if spec.get("emission"):
                        label = f"{target['name']}.{node['method']}"
                        stats["emissions"].add(label)
                        # `*` = declared bare `emission`: no promise about where
                        declared = spec.get("capabilities")
                        stats["capabilities"][label] = (
                            sorted(declared) if declared is not None else ["*"])
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
            "capabilities": dict(sorted(stats["capabilities"].items())),
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


def _run_mcp(args) -> int:
    """`revl mcp {serve,schema,import}` — the MCP bridge (docs/mcp-bridge.md)."""
    from .mcp.schema import import_tools, tools_from_ir
    from .mcp.server import serve

    if args.mcp_command == "serve":
        return serve()

    if args.mcp_command == "schema":
        try:
            ir = compile_files(args.files)
        except RevlError as error:
            print(json.dumps(report(error), indent=2))
            return 1
        print(json.dumps({"tools": tools_from_ir(ir, composition=args.composition)},
                         indent=2))
        return 0

    # import
    try:
        with open(args.manifest, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: cannot read {args.manifest}: {error}", file=sys.stderr)
        return 1
    source = import_tools(manifest, service=args.service, key=args.key,
                          backend=args.backend)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(source)
    else:
        print(source, end="")
    return 0


def _run_import(args) -> int:
    """`revl import {wit,openapi}` — the import codegen family
    (docs/import-wit.md, docs/import-openapi.md)."""
    try:
        if args.import_command == "openapi":
            from .import_openapi import import_openapi_file
            source = import_openapi_file(args.file, backend=args.backend,
                                         service=args.service, pure=args.pure,
                                         emission=args.emission)
        else:
            from .import_wit import import_wit_file
            source = import_wit_file(args.file, backend=args.backend, pure=args.pure)
    except OSError as error:
        print(f"error: cannot read {args.file}: {error}", file=sys.stderr)
        return 1
    except RevlError as error:
        if args.json_diagnostics:
            print(json.dumps(report(error), indent=2))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1

    if args.output:
        try:
            Path(args.output).write_text(source, encoding="utf-8")
        except OSError as error:
            print(f"error: cannot write {args.output}: {error}", file=sys.stderr)
            return 1
    else:
        print(source, end="")
    return 0


def _run_plan(args) -> int:
    """`revl plan` — what admitting these files would do (docs/plan.md).

    Exit status follows the gate, not the planner: 0 when the candidate is
    admissible, 1 when it is not. A plan is still printed either way, so a
    rejection tells you both why and what you were about to do.
    """
    from .plan import plan as build_plan, render

    running = None
    if args.manifest:
        try:
            with open(args.manifest, encoding="utf-8") as handle:
                running = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            print(f"error: cannot read {args.manifest}: {error}", file=sys.stderr)
            return 1
        if not isinstance(running, dict):
            print(f"error: {args.manifest}: expected a compiled IR document (an object)",
                  file=sys.stderr)
            return 1

    result = build_plan(files=list(args.files), manifest=running,
                        replacing=tuple(args.replacing))
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0 if result["admissible"] else 1


def _run_query(args, ir: dict) -> int:
    """`revl query <question> <target> <files...>` — the composition query
    layer (docs/queries.md). A miss (unknown component/service/key) is a
    non-zero exit with the known names, not a crash."""
    from .query import QUERIES, render

    handler = QUERIES[args.query_command]
    if args.query_command == "drift":
        result = handler(ir, args.target, gains=args.gains, losses=args.loses)
    else:
        result = handler(ir, args.target)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render(result))
    return 0 if result.get("ok") else 1


def _run_explain(args) -> int:
    """`revl explain <code>` — the other half of a structured diagnostic. A
    rejection hands back a code; this turns the code back into the guarantee
    it enforces and the rewrite that satisfies it, without a round trip to
    DESIGN.md."""
    record = explain(args.code)
    if args.json:
        print(json.dumps(record, indent=2))
        return 0 if record["ok"] else 1
    if not record["ok"]:
        print(f"error: {record['message']}", file=sys.stderr)
        print(f"known codes: {', '.join(record['known'])}", file=sys.stderr)
        return 1
    print(f"{record['code']}  {record['guarantee']}")
    if record.get("fix"):
        print(f"  fix: {record['fix']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="revl")
    sub = parser.add_subparsers(dest="command", required=True)

    cmd = sub.add_parser("compile", help="compile .rvl files to a backend IR document")
    cmd.add_argument("files", nargs="+")
    cmd.add_argument("-o", "--output", default=None, help="output path (default: stdout)")
    cmd.add_argument("--json-diagnostics", action="store_true",
                     help="on rejection, print a structured diagnostic (code, guarantee, "
                          "expected/actual, hint) instead of the human rendering")

    exp = sub.add_parser("explain", help="what a diagnostic code means and how to fix it")
    exp.add_argument("code", help="a diagnostic code, e.g. G4 (case-insensitive)")
    exp.add_argument("--json", action="store_true", help="machine-readable output")

    audit = sub.add_parser("audit", help="composition manifest + G8 boundary surface")
    audit.add_argument("files", nargs="+")
    audit.add_argument("--json", action="store_true", help="machine-readable output")

    plan_cmd = sub.add_parser(
        "plan", help="dry run for admission: the delta a swap would produce, without applying it")
    plan_cmd.add_argument("files", nargs="+")
    plan_cmd.add_argument("--manifest", default=None,
                          help="compiled IR document of the RUNNING composition "
                               "(as written by `revl compile -o`); omit for a cold start")
    plan_cmd.add_argument("--replacing", action="append", default=[], metavar="NAME",
                          help="a running component withdrawn in this admission "
                               "(renames); repeatable")
    plan_cmd.add_argument("--json", action="store_true", help="machine-readable output")


    query = sub.add_parser(
        "query", help="ask the composition a question (docs/queries.md)")
    query_sub = query.add_subparsers(dest="query_command", required=True)
    for name, metavar, helptext in (
        ("emits-to", "TARGET",
         "who emits to a service key, `key.method`, service or extern?"),
        ("withdraw", "COMPONENT",
         "what breaks if this component is withdrawn (the reactive cascade)?"),
        ("depends-on", "TARGET", "who depends on a provision key or service?"),
        ("reaches", "COMPONENT",
         "the transitive boundary surface of one component"),
        ("drift", "SERVICE",
         "which providers and call sites a service interface change implicates"),
    ):
        sub_cmd = query_sub.add_parser(name, help=helptext)
        sub_cmd.add_argument("target", metavar=metavar)
        sub_cmd.add_argument("files", nargs="+")
        sub_cmd.add_argument("--json", action="store_true",
                             help="machine-readable output")
        if name == "drift":
            sub_cmd.add_argument("--gains", action="append", default=[],
                                 metavar="METHOD",
                                 help="a method the service would gain (repeatable)")
            sub_cmd.add_argument("--loses", action="append", default=[],
                                 metavar="METHOD",
                                 help="a method the service would lose (repeatable)")

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
    test.add_argument("--backend", default="py",
                      choices=("py", "ts", "rust", "java", "wasm", "go", "all"),
                      help="tier to run the `test` blocks on (default: py); "
                           "`all` runs every tier whose toolchain is present")

    mcp = sub.add_parser("mcp", help="MCP bridge: serve the compiler, or project services <-> tools")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_sub.add_parser("serve", help="run the compiler as an MCP server (stdio)")
    mcp_serve.add_argument("--files", nargs="*", default=None,
                           help="optional default composition for tools called without one")
    mcp_schema = mcp_sub.add_parser("schema",
                                    help="project provided services to MCP tool definitions")
    mcp_schema.add_argument("files", nargs="+")
    mcp_schema.add_argument("--composition", default="revl", help="tool-name prefix")
    mcp_import = mcp_sub.add_parser("import",
                                    help="turn an MCP tools/list manifest into revl source")
    mcp_import.add_argument("manifest", help="JSON file: a tools/list result (or {\"tools\": [...]})")
    mcp_import.add_argument("--service", default="Imported", help="generated service name")
    mcp_import.add_argument("--key", default="imported", help="provision key")
    mcp_import.add_argument("--backend", default="ts", choices=("ts", "py"),
                            help="host block backend for the generated externs")
    mcp_import.add_argument("-o", "--output", default=None, help="output path (default: stdout)")

    imp = sub.add_parser("import",
                         help="import an external interface definition as revl source")
    imp_sub = imp.add_subparsers(dest="import_command", required=True)
    imp_wit = imp_sub.add_parser(
        "wit", help="turn a WIT world/interface into revl source (docs/import-wit.md)")
    imp_wit.add_argument("file", help="a .wit file")
    imp_wit.add_argument("--backend", default="wasm",
                         choices=("wasm", "ts", "py", "rust"),
                         help="host block backend for the generated extern stubs "
                              "(default: wasm)")
    imp_wit.add_argument(
        "--pure", action="append", default=[], metavar="NAME",
        help="assert that `<interface>.<func>` (or `<func>`) is reversible, so it "
             "is emitted as a plain `fn` instead of `emission`. WIT makes no such "
             "claim; this is your assertion and it is recorded in the output. "
             "Repeatable")
    imp_wit.add_argument("-o", "--output", default=None,
                         help="output path (default: stdout)")
    imp_wit.add_argument("--json-diagnostics", action="store_true",
                         help="on rejection, print a structured diagnostic instead "
                              "of the human rendering")

    imp_api = imp_sub.add_parser(
        "openapi",
        help="turn an OpenAPI 3.x document into revl source (docs/import-openapi.md)")
    imp_api.add_argument("file", help="a .json (or .yaml, if PyYAML is importable) OpenAPI 3.x document")
    imp_api.add_argument("--backend", default="ts", choices=("ts", "py", "rust"),
                         help="host block backend for the generated extern stubs "
                              "(default: ts)")
    imp_api.add_argument("--service", default=None,
                         help="generated service name (default: from `info.title`)")
    imp_api.add_argument(
        "--pure", action="append", default=[], metavar="OP",
        help="assert that an operation whose verb HTTP does not call safe (a "
             "`POST /search`, say) changes nothing, so it is emitted as a plain "
             "`fn` instead of `emission`. Name it by generated name, "
             "`operationId`, or \"POST /search\". Repeatable")
    imp_api.add_argument(
        "--emission", action="append", default=[], metavar="OP",
        help="assert that a safe-by-spec operation (a `GET` that writes) is "
             "irreversible after all, overriding the verb. Named the same way. "
             "Repeatable")
    imp_api.add_argument("-o", "--output", default=None,
                         help="output path (default: stdout)")
    imp_api.add_argument("--json-diagnostics", action="store_true",
                         help="on rejection, print a structured diagnostic instead "
                              "of the human rendering")

    run = sub.add_parser("run", help="boot a composition on a Cordis runtime; streams the lifecycle/host trace (hold + REPL, --watch, or --plan)")
    run.add_argument("files", nargs="+")
    run.add_argument("--backend", default="py", choices=KNOWN_BACKENDS,
                     help="target runtime tier (default: py; only py is runnable today; "
                          "a missing runtime prints setup.sh and exits nonzero)")
    run.add_argument("--config", default=None,
                     help="TOML/JSON file of `component-name = { ... }` config tables")
    run.add_argument("--watch", action="store_true",
                     help="watch the sources and recompile on change; a rejected edit is refused, the run keeps going")
    run.add_argument("--record", action="store_true",
                     help="record the effect accumulator so the REPL can step "
                          "backwards over it (`:timeline`, `:back k`) — see docs/replay.md")
    run.add_argument("--plan", action="store_true",
                     help="print the load plan (order, config, callable keys) and exit, without a runtime")
    run.add_argument("--placement", default=None,
                     help="TOML/JSON placement map: split components across processes and wire the seams")
    run.add_argument("--once", action="store_true",
                     help="with --placement: bring the composition up, run probes, then tear down and exit")

    args = parser.parse_args(argv)

    if args.command == "explain":
        return _run_explain(args)

    if args.command == "fmt":
        return _run_fmt(args)

    if args.command == "run":
        return run_command(args)

    if args.command == "mcp":
        return _run_mcp(args)

    if args.command == "import":
        return _run_import(args)

    if args.command == "plan":
        return _run_plan(args)

    try:
        ir = compile_files(args.files)
    except RevlError as error:
        if getattr(args, "json_diagnostics", False):
            print(json.dumps(report(error), indent=2))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1

    # Open obligations go to stderr so a piped/redirected IR document stays
    # exactly the IR document; the same list is in `ir["holes"]` for anything
    # reading the JSON (docs/holes.md).
    open_holes = ir.get("holes") or []
    if open_holes:
        if getattr(args, "json_diagnostics", False):
            print(json.dumps(obligations(open_holes), indent=2), file=sys.stderr)
        else:
            plural = "s" if len(open_holes) > 1 else ""
            print(f"{len(open_holes)} open hole{plural} — this is a draft: it "
                  f"compiles, admission will refuse it (docs/holes.md)",
                  file=sys.stderr)
            for rendered in render_holes(open_holes):
                print(f"  {rendered}", file=sys.stderr)

    if args.command == "test":
        return test_command(ir, args.backend)

    if args.command == "query":
        return _run_query(args, ir)

    if args.command == "audit":
        boundary = _boundary(ir)
        distribution = distributability(ir)
        manifest = ir.get("manifest") or {}
        declared_externs = [
            {"name": ext["name"], "class": ext.get("class"),
             "backends": sorted((ext.get("bodies") or {}).keys())}
            for ext in ir.get("externs") or []
        ]
        if args.json:
            print(json.dumps({"manifest": manifest, "boundary": boundary,
                              "externs": declared_externs,
                              "distributability": distribution}, indent=2))
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
                    caps = stats.get("capabilities") or {}

                    def _scoped(label: str) -> str:
                        scope = caps.get(label) or ["*"]
                        return (f"{label} [{', '.join(scope)}]"
                                if scope != ["*"] else label)

                    # the union is the G8 answer to "where can this component
                    # reach"; `*` in it means some dependency's declaration
                    # makes no promise, which is the thing worth seeing
                    reach = sorted({c for scope in caps.values() for c in scope})
                    detail.append(f"emissions: {', '.join(_scoped(e) for e in stats['emissions'])}"
                                  f" ({stats['compensated']} compensated)")
                    if reach:
                        detail.append(f"capabilities: {', '.join(reach)}")
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
        templates = manifest.get("templates") or []
        if templates:
            # G8: the instance dimension is dynamic (docs/design-v2-instances.md,
            # decision 7). These are spawn targets — runtime instances, not
            # static composition members — so their multiplicity is `× dynamic`.
            print("\ninstance-parametric components (× dynamic — spawned at runtime, "
                  "each in its own local realm):")
            for name in templates:
                stats = boundary.get(name) or {}
                emissions = stats.get("emissions") or []
                surface = (f"emissions: {', '.join(emissions)}"
                           if emissions else "no emissions")
                print(f"  {name} × dynamic  ({surface})")
        if declared_externs:
            print("\nexterns (verbatim host code — unchecked inside, typed at the boundary):")
            for ext in declared_externs:
                print(f"  {ext['name']}  [{ext['class']}]  backends: "
                      f"{', '.join(ext['backends']) or '—'}")
        if distribution:
            print("\ndistributability (interop-bridge §4: which services may cross a process seam):")
            width = max(len(name) for name in distribution)
            for name in sorted(distribution):
                verdict = distribution[name]
                print(f"  {name:<{width}}  {verdict['verdict']:<20} "
                      f"{'; '.join(verdict['reasons'])}")
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

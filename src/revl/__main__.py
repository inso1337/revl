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

# G8 audit: the pseudo-boundary recorded when a component reaches host code
# through a first-class function dispatch (an arrow-typed parameter or
# binding) — the same `*` capability the G4 analysis uses, for the same
# reason: no extern name can be given, because what runs is not statically
# boundable. The concrete names that travel alongside it are reported too.
_UNKNOWN_DISPATCH = "*"


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
    # the capability fixed point (G4's own analysis) — see the note where it
    # joins the walk below; without it a first-class dispatch is invisible
    from .lower import _emitting_capabilities  # noqa: PLC0415 — lazy, like plan

    fns = ir.get("functions") or []
    if isinstance(fns, dict):
        fns = list(fns.values())
    fn_caps_map = _emitting_capabilities(fns, ir.get("externs") or [])

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
        unknown_dispatch = False
        for name in called:
            if name in externs:
                host.add(name)
            else:
                host |= reach.get(name, set())
                # the name-only walk cannot see a first-class dispatch: a fn
                # whose body hands an emitting callable to a dispatcher (`f(x)`
                # through an arrow-typed parameter) reaches boundaries no call
                # name names. The lowerer's capability fixed point tracks that
                # — its concrete extern names join the surface, and `*` marks
                # the dispatch itself so the report can say what is unnameable.
                fn_caps = fn_caps_map.get(name) or set()
                host |= {c for c in fn_caps if c != "*"}
                if "*" in fn_caps:
                    unknown_dispatch = True

        if unknown_dispatch:
            host.add(_UNKNOWN_DISPATCH)

        report[comp["name"]] = {
            "emissions": sorted(stats["emissions"]),
            "capabilities": dict(sorted(stats["capabilities"].items())),
            "compensated": stats["compensated"],
            "awaits": stats["awaits"],
            "externs": [
                {"name": _UNKNOWN_DISPATCH,
                 "class": "first-class dispatch",
                 "backends": []}
                if name == _UNKNOWN_DISPATCH else
                {"name": name,
                 "class": extern_class.get(name, {}).get("class"),
                 "backends": sorted((extern_class.get(name, {}).get("bodies") or {}).keys())}
                for name in sorted(host)
            ],
        }
    return report


def _run_fmt(args: argparse.Namespace) -> int:
    """`revl fmt`: canonical formatter with a self-proving IR-equivalence gate.

    Default mode produces a canonical formatting; `--migrate` rewrites 1.x
    `$` interpolation to 2.0 templates.  Either way the rewrite is admitted
    only when compiling the original and the rewritten text yields
    byte-identical IR (roadmap item 35); a file whose IR would change is
    REFUSED (named, nonzero exit) rather than written.
    """
    from .formatter import format_source, ir_equivalent, FormatError

    if args.output and len(args.files) != 1:
        print("error: `fmt -o` expects exactly one input file", file=sys.stderr)
        return 1

    exit_code = 0
    for path_str in args.files:
        path = Path(path_str)
        try:
            original = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            print(f"error: cannot read {path_str}: {error}", file=sys.stderr)
            return 1

        if args.migrate:
            try:
                rewritten, warnings = migrate_source(original, str(path))
            except RevlError as error:
                print(f"error: cannot migrate {path_str}: {error}", file=sys.stderr)
                exit_code = 1
                continue
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
        else:
            try:
                rewritten = format_source(original, str(path))
            except FormatError as error:
                print(f"error: cannot format {path_str}: {error}", file=sys.stderr)
                exit_code = 1
                continue

        # The self-proving gate: a rewrite ships iff the IR is unchanged.
        # `--migrate` deliberately rewrites tokens, so it forgoes the
        # token-identity fall-back the (whitespace-only) formatter relies on.
        gate = ir_equivalent(original, rewritten, str(path),
                             token_preserving=not args.migrate)
        if not gate.admitted:
            print(f"error: refusing {path_str}: {gate.reason}", file=sys.stderr)
            exit_code = 1
            continue

        if getattr(args, "check", False):
            if rewritten != original:
                print(f"{path_str}: would reformat", file=sys.stderr)
                exit_code = 1
            continue

        if args.output:
            try:
                Path(args.output).write_bytes(rewritten.encode("utf-8"))
            except OSError as error:
                print(f"error: cannot write {args.output}: {error}", file=sys.stderr)
                return 1
        elif rewritten != original:
            try:
                path.write_bytes(rewritten.encode("utf-8"))
            except OSError as error:
                print(f"error: cannot write {path_str}: {error}", file=sys.stderr)
                return 1

    return exit_code


def _run_mcp(args) -> int:
    """`revl mcp {serve,schema,import}` — the MCP bridge (docs/mcp-bridge.md)."""
    from .mcp.schema import import_tools, tools_from_ir
    from .mcp.server import serve

    if args.mcp_command == "serve":
        # composition persistence (docs/persistence.md): a snapshot passed on
        # the command line is re-admitted through the same gate a live restore
        # runs — a component the current checker rejects aborts the boot loudly
        # rather than being smuggled in.
        if getattr(args, "restore", None):
            from .mcp.persist import RestoreError
            from .mcp.server import SESSION

            try:
                with open(args.restore, encoding="utf-8") as handle:
                    snap = json.load(handle)
            except (OSError, json.JSONDecodeError) as error:
                print(f"error: cannot read snapshot {args.restore}: {error}",
                      file=sys.stderr)
                return 1
            try:
                SESSION.restore(snap)
            except RestoreError as error:
                print(f"error: cannot restore {args.restore}: {error}",
                      file=sys.stderr)
                return 1
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


def _run_serve(args) -> int:
    """`revl serve --mcp FILES` — serve a composition's OWN provided operations
    as MCP tools (the fourth quadrant of the bridge, docs/mcp-bridge.md).

    Placement note: this boots one composition and stands it up, so it shares
    `revl run`'s admission-and-config preflight (compile -> refuse holes ->
    load config -> refuse a missing required field) rather than living under
    `revl mcp serve`, whose tool set is the fixed compiler surface. `--mcp`
    names the transport, leaving room for other serve frontends later.
    """
    from .run import _load_config, _required_config_problem  # noqa: PLC0415

    if not getattr(args, "mcp", False):
        print("error: `revl serve` needs a transport — pass --mcp to serve over "
              "the MCP stdio protocol", file=sys.stderr)
        return 2

    from .holes import refuse_admission  # noqa: PLC0415

    try:
        ir = compile_files(args.files)
        # booting is admission: a draft with open obligations may not become a
        # running composition, however it was compiled (docs/holes.md)
        refuse_admission(ir)
        config = _load_config(getattr(args, "config", None))
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: cannot read config: {error}", file=sys.stderr)
        return 1

    if not (ir.get("components") or []):
        print("nothing to serve: no components in the composition", file=sys.stderr)
        return 0

    # config-to-boot preflight: the same rule `revl run` enforces before a
    # runtime is touched — a component admitted with a missing required config
    # field refuses the boot loudly, rather than settling a fiber onto FAILED
    # behind a tool the client can already see advertised.
    problem = _required_config_problem(ir, config)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 1

    from .mcp.composed import serve_composition  # noqa: PLC0415
    from .mcp.session import SessionError  # noqa: PLC0415

    try:
        return serve_composition(ir, config, composition=args.composition)
    except SessionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3


def _run_import(args) -> int:
    """`revl import {wit,openapi,cordis}` — the import codegen family
    (docs/import-wit.md, docs/import-openapi.md, docs/import-cordis.md)."""
    try:
        if args.import_command == "openapi":
            from .import_openapi import import_openapi_file
            source = import_openapi_file(args.file, backend=args.backend,
                                         service=args.service, pure=args.pure,
                                         emission=args.emission)
        elif args.import_command == "cordis":
            from .import_cordis import import_cordis_file
            source = import_cordis_file(args.file, backend=args.backend,
                                        service=args.service, pure=args.pure,
                                        mark_unrecovered=args.mark_unrecovered)
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
                        replacing=tuple(args.replacing),
                        include_ir=bool(args.output))

    if args.output:
        # -o turns the plan into an executable artifact (docs/apply.md). Only an
        # admitted plan can be applied; a rejection is printed, not written.
        from .apply import build_artifact, ApplyError  # noqa: PLC0415

        try:
            artifact = build_artifact(result, running)
        except ApplyError as error:
            print(render(result))
            print(f"\nerror: {error}", file=sys.stderr)
            return 1
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, indent=2)
        ops = len(artifact["operations"])
        print(f"wrote {args.output}: an applyable plan of {ops} operation(s) — "
              f"`revl apply {args.output}` (docs/apply.md)")
        return 0

    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0 if result["admissible"] else 1


def _run_apply(args) -> int:
    """`revl apply change.plan` — boot the plan's pre-state, then execute the
    plan against it: drift-refuse if the composition moved, verify each step
    against its prediction, and roll the applied prefix back on any failure
    (docs/apply.md). A one-shot: it tears the composition down afterwards and
    reports whether the change (or its rollback) left any residue."""
    from .apply import validate_artifact, ApplyError
    from .mcp.session import Session, SessionError

    try:
        with open(args.plan, encoding="utf-8") as handle:
            artifact = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: cannot read {args.plan}: {error}", file=sys.stderr)
        return 1
    try:
        validate_artifact(artifact)
    except ApplyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    running_ir = artifact.get("runningIR") or {}
    if args.against:
        # apply against a DIFFERENT current composition than the plan assumed —
        # the honest way to exercise drift refusal from the CLI.
        try:
            with open(args.against, encoding="utf-8") as handle:
                running_ir = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            print(f"error: cannot read {args.against}: {error}", file=sys.stderr)
            return 1

    session = Session()
    try:
        if running_ir.get("components"):
            session.load(running_ir)
        elif artifact["resultingIR"].get("components"):
            # cold start: nothing was running, so applying is just bringing the
            # resulting composition up.
            state = session.load(artifact["resultingIR"])
            report = {"applied": True, "coldStart": True,
                      "resulting": artifact["resulting"]["components"],
                      "state": state}
            _print_apply(report, args)
            unloaded = session.unload()
            print(f"torn down — no residue: {unloaded['noResidue']}")
            return 0
        report = session.apply(artifact)
    except SessionError as error:
        # a drift refusal lands here — the composition is untouched
        print(f"refused: {error}", file=sys.stderr)
        if session.loaded:
            session.unload()
        return 1

    _print_apply(report, args)
    unloaded = session.unload()
    print(f"\ntorn down — no residue: {unloaded['noResidue']}")
    return 0 if report["applied"] else 1


def _print_apply(report: dict, args) -> None:
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
        return
    if report["applied"]:
        print(f"applied: {len(report.get('steps') or [])} step(s) — resulting "
              f"composition: {', '.join(report.get('resulting') or []) or '(empty)'}")
        for step in report.get("steps") or []:
            print(f"  {step['op']:<8} {step['name']:<16} -> {step['state'] or 'gone'}")
        return
    print(f"FAILED at `{report.get('failedAt')}`: {report['reason']}")
    print(f"rolled back {len(report.get('rolledBack') or [])} step(s) "
          f"(LIFO, derived inverses):")
    for undo in report.get("rolledBack") or []:
        print(f"  {undo['undo']:<8} {undo['name']}")
    print(f"no residue: {report.get('noResidue')} "
          f"(registry {report['registry']['baseline']} -> "
          f"{report['registry']['afterRollback']})")


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
    audit.add_argument(
        "--diff", metavar="PREV.json", default=None,
        help="authority-drift gate: re-audit the files and FAIL (nonzero) if "
             "the new generation ADDS boundary crossings not in PREV.json")
    audit.add_argument(
        "--accept", action="append", default=[], metavar="CROSSING",
        help="acknowledge one added crossing so it no longer fails --diff "
             "(the token printed after `+`; repeatable)")
    audit.add_argument(
        "--accept-all", action="store_true",
        help="acknowledge every added crossing under --diff")

    plan_cmd = sub.add_parser(
        "plan", help="dry run for admission: the delta a swap would produce, without applying it")
    plan_cmd.add_argument("files", nargs="+")
    plan_cmd.add_argument("--manifest", default=None,
                          help="compiled IR document of the RUNNING composition "
                               "(as written by `revl compile -o`); omit for a cold start")
    plan_cmd.add_argument("--replacing", action="append", default=[], metavar="NAME",
                          help="a running component withdrawn in this admission "
                               "(renames); repeatable")
    plan_cmd.add_argument("-o", "--output", default=None, metavar="change.plan",
                          help="serialize an EXECUTABLE plan artifact to this path "
                               "(basis for drift, ordered ops, resulting IR) — "
                               "apply it with `revl apply` (docs/apply.md)")
    plan_cmd.add_argument("--json", action="store_true", help="machine-readable output")

    apply_cmd = sub.add_parser(
        "apply", help="execute a `revl plan -o` artifact against a live composition: "
                      "drift-refuse, verify each step, roll back on failure (docs/apply.md)")
    apply_cmd.add_argument("plan", metavar="change.plan",
                           help="a plan artifact written by `revl plan -o`")
    apply_cmd.add_argument("--against", default=None, metavar="RUNNING.json",
                           help="boot this composition as the live pre-state instead "
                                "of the plan's own — drift is refused if it differs "
                                "from the plan's basis")
    apply_cmd.add_argument("--json", action="store_true", help="machine-readable output")


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

    fmt = sub.add_parser("fmt", help="canonically format .rvl sources (IR-equivalence gated)")
    fmt.add_argument(
        "--migrate",
        action="store_true",
        help="rewrite 1.x `$` interpolation to backtick templates instead of formatting",
    )
    fmt.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit nonzero if any file is not already canonical",
    )
    fmt.add_argument("files", nargs="+")
    fmt.add_argument(
        "-o",
        "--output",
        default=None,
        help="write the result to this path instead of in place (single input)",
    )

    test = sub.add_parser("test", help="compile and run `test` blocks")
    test.add_argument("files", nargs="+")
    test.add_argument("--backend", default="py",
                      choices=("py", "ts", "rust", "java", "wasm", "go", "all"),
                      help="tier to run the `test` blocks on (default: py); "
                           "`all` runs every tier whose toolchain is present")
    test.add_argument("--sweep", action="store_true",
                      help="fault sweep: inject failure at every step of every "
                           "component and check L-Raise / no-residue / LIFO / "
                           "siblings at each (py tier; docs/fault-tests.md)")

    mcp = sub.add_parser("mcp", help="MCP bridge: serve the compiler, or project services <-> tools")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_sub.add_parser("serve", help="run the compiler as an MCP server (stdio)")
    mcp_serve.add_argument("--files", nargs="*", default=None,
                           help="optional default composition for tools called without one")
    # composition persistence (docs/persistence.md): boot the live session
    # from a snapshot so an evolved composition survives a restart. The
    # snapshot is re-admitted through the gate, never trusted blindly.
    mcp_serve.add_argument("--restore", default=None, metavar="SNAPSHOT.json",
                           help="re-admit a revl_snapshot document into the session "
                                "before serving (self-evolution across a restart)")
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

    # `revl import cordis` — a Cordis (TS) plugin's inject/provide surface
    # (docs/import-cordis.md). Own additive block; shared file with a sibling.
    imp_cordis = imp_sub.add_parser(
        "cordis",
        help="turn a Cordis (TS) plugin into revl source (docs/import-cordis.md)")
    imp_cordis.add_argument("file", help="a Cordis plugin .ts (or .js) file")
    imp_cordis.add_argument("--backend", default="ts", choices=("ts", "py", "rust"),
                            help="host block backend for the generated extern stubs "
                                 "(default: ts)")
    imp_cordis.add_argument("--service", default=None,
                            help="generated service name (default: from the "
                                 "provided service key)")
    imp_cordis.add_argument(
        "--pure", action="append", default=[], metavar="OP",
        help="assert that a method changes nothing, so it is emitted as a plain "
             "`fn` instead of `emission`. Untyped TS makes no such claim; this is "
             "your assertion and it is recorded in the output. Name it "
             "`<Service>.<method>` or `<method>`. Repeatable")
    imp_cordis.add_argument(
        "--mark-unrecovered", action="store_true",
        help="instead of refusing an operation whose signature cannot be "
             "recovered, emit a loud `// UNRECOVERED` marker in its place so a "
             "partial surface still compiles (nothing is ever guessed)")
    imp_cordis.add_argument("-o", "--output", default=None,
                            help="output path (default: stdout)")
    imp_cordis.add_argument("--json-diagnostics", action="store_true",
                            help="on rejection, print a structured diagnostic "
                                 "instead of the human rendering")

    serve = sub.add_parser(
        "serve",
        help="serve a composition's OWN provided operations as MCP tools "
             "(the fourth quadrant: hints derived by the compiler)")
    serve.add_argument("files", nargs="+")
    serve.add_argument("--mcp", action="store_true",
                       help="serve over the MCP stdio protocol (required)")
    serve.add_argument("--config", default=None,
                       help="TOML/JSON file of `component-name = { ... }` config "
                            "tables — supplied to each component at boot")
    serve.add_argument("--composition", default="revl",
                       help="tool-name prefix (tools are `<prefix>.<key>.<op>`)")

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

    if args.command == "serve":
        return _run_serve(args)

    if args.command == "mcp":
        return _run_mcp(args)

    if args.command == "import":
        return _run_import(args)

    if args.command == "plan":
        return _run_plan(args)

    if args.command == "apply":
        return _run_apply(args)

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
        return test_command(ir, args.backend, sweep=getattr(args, "sweep", False))

    if args.command == "query":
        return _run_query(args, ir)

    if args.command == "audit" and getattr(args, "diff", None):
        from .audit_diff import audit_report, evaluate, render  # noqa: PLC0415
        with open(args.diff, encoding="utf-8") as handle:
            prev = json.load(handle)
        new = audit_report(ir)
        result = evaluate(prev, new, accepted=set(args.accept),
                          accept_all=args.accept_all)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(render(result, args.diff))
        return 1 if result["widened"] else 0

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
            # The manifest + G8 audit as the versioned interchange format
            # (docs/interchange-format.md, roadmap item 28): `stamp` adds the
            # `schema_version`/`kind` header additively, over the same body
            # earlier consumers already read.
            from .interchange import stamp  # noqa: PLC0415
            print(json.dumps(stamp(
                {"manifest": manifest, "boundary": boundary,
                 "externs": declared_externs,
                 "distributability": distribution}), indent=2))
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
                        f"{e['name']} (reached through first-class function "
                        "dispatch — what runs is not statically boundable)"
                        if e.get("class") == "first-class dispatch" else
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
